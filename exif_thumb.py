#!/usr/bin/env python3
"""
exif_thumb.py — Review and modify embedded EXIF thumbnails in JPEG files.

Usage:
    python exif_thumb.py info       <image.jpg>              # Show thumbnail info + extract it
    python exif_thumb.py regenerate <image.jpg> [--size 160] # Regenerate thumbnail from actual image
    python exif_thumb.py strip      <image.jpg>              # Remove the embedded thumbnail entirely
    python exif_thumb.py compare    <image.jpg>              # Side-by-side diff image saved to disk
    python exif_thumb.py inject     <image.jpg> <thumb.jpg>  # Inject a custom thumbnail from a file
    python exif_thumb.py resize      <image.jpg> --pixels 2000  # Upsample pixels to 2000px long edge
                                    [--ifd0 iphone|samsung|pixel|canon|minimal]

Options:
    --out <path>     Output JPEG path (default: timestamped JPEG in current dir)
    --size <int>     Thumbnail max dimension (default: 160)
    --quality <int>  JPEG quality for thumbnail (default: 85)
    --no-backup      Skip creating .bak file
    --kb <int>       [fat] Target file size in KB (default: 512)
    --ifd0 <preset>  [fat] Inject realistic camera IFD0: iphone, samsung, pixel, canon, minimal
    --no-regen       [fat] Skip thumbnail regeneration, only pad + inject IFD0
"""

import sys
import io
import argparse
import shutil
import struct
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageDraw
    import piexif
    import numpy as np
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install Pillow piexif numpy")
    sys.exit(1)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_exif(path: str) -> dict:
    try:
        return piexif.load(path)
    except Exception as e:
        print(f"[ERROR] Could not read EXIF from {path}: {e}")
        sys.exit(1)


def save_output(src: str, dst: str | None, exif_bytes: bytes, no_backup: bool):
    """Insert patched EXIF bytes into a copy of src, writing to dst."""
    target = dst or f"{Path(src).stem}_{ts()}.jpg"
    in_place = (dst == src)
    if in_place and not no_backup:
        bak = src + ".bak"
        shutil.copy2(src, bak)
        print(f"  Backup saved → {bak}")
    piexif.insert(exif_bytes, src, target)
    print(f"  Saved → {target}")


def thumb_from_exif(exif_dict: dict) -> Image.Image | None:
    raw = exif_dict.get("thumbnail")
    if not raw:
        return None
    return Image.open(io.BytesIO(raw))


def encode_thumb(img: Image.Image, size: int, quality: int) -> bytes:
    """
    Encode a thumbnail matching Photoshop CC's JPEG structure:
      SOI → APP13 (Adobe_CM) → APP14 (Adobe, ColorTransform=1/YCbCr) → DQT → SOF → DHT → SOS

    Pillow adds a JFIF APP0 and omits APP14. Strict readers on iOS, Android MTP/PTP,
    and Windows Photos use APP14's ColorTransform byte to decide YCbCr vs RGB. Missing
    it causes silent rejection or colour corruption on those platforms.
    """
    thumb = img.copy().convert("RGB")
    thumb.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=quality)
    raw = buf.getvalue()

    # Strip JFIF APP0 (FFE0 + "JFIF") that Pillow adds
    if raw[2:4] == b"\xff\xe0" and raw[6:10] == b"JFIF":
        seg_len = struct.unpack(">H", raw[4:6])[0]
        raw = raw[:2] + raw[2 + seg_len:]

    # APP13 Adobe_CM — Photoshop colour management marker
    app13_p = b"Adobe_CM\x00\x01"
    app13   = b"\xff\xed" + struct.pack(">H", len(app13_p) + 2) + app13_p

    # APP14 Adobe — DCTEncodeVersion=100, Flags0=0x8000, Flags1=0, ColorTransform=1 (YCbCr)
    # Payload must be exactly 12 bytes: "Adobe"(5) + version(2) + flags0(2) + flags1(2) + transform(1)
    app14_p = b"Adobe" + struct.pack(">HHH", 100, 0x8000, 0) + b"\x01"
    app14   = b"\xff\xee" + struct.pack(">H", len(app14_p) + 2) + app14_p

    return raw[:2] + app13 + app14 + raw[2:]


def build_clean_exif_with_thumb(exif_dict: dict, ps_thumb: bytes) -> bytes:
    """
    piexif.dump() validates the thumbnail JPEG and rejects non-standard APP segments
    (like APP13/APP14). Strategy: let piexif dump with a plain Pillow thumbnail as a
    placeholder, then raw-splice ps_thumb in by locating it via the IFD1 offset tag.
    """
    # Generate a plain piexif-compatible thumbnail at the same pixel dimensions
    ps_img = Image.open(io.BytesIO(ps_thumb))
    plain_buf = io.BytesIO()
    ps_img.convert("RGB").save(plain_buf, "JPEG", quality=85)
    exif_dict["thumbnail"] = plain_buf.getvalue()

    # Only the minimal valid IFD1 tags for a JPEG thumbnail
    exif_dict["1st"] = {
        piexif.ImageIFD.Compression:    6,
        piexif.ImageIFD.XResolution:    (72, 1),
        piexif.ImageIFD.YResolution:    (72, 1),
        piexif.ImageIFD.ResolutionUnit: 2,
    }

    exif_bytes = piexif.dump(exif_dict)

    # ── Raw splice: find thumbnail via IFD1 tag 513 offset, replace with ps_thumb ──
    tiff_off = 6  # after 'Exif\x00\x00'
    bo_mark  = exif_bytes[tiff_off:tiff_off + 2]
    bo       = ">" if bo_mark == b"MM" else "<"

    ifd0_start    = tiff_off + struct.unpack_from(f"{bo}I", exif_bytes, tiff_off + 4)[0]
    ifd0_count    = struct.unpack_from(f"{bo}H", exif_bytes, ifd0_start)[0]
    ifd1_ptr_pos  = ifd0_start + 2 + ifd0_count * 12
    ifd1_start_r  = struct.unpack_from(f"{bo}I", exif_bytes, ifd1_ptr_pos)[0]
    ifd1_start    = tiff_off + ifd1_start_r

    ifd1_count    = struct.unpack_from(f"{bo}H", exif_bytes, ifd1_start)[0]
    thumb_off_r   = None
    thumb_len_pos = None

    for i in range(ifd1_count):
        ep  = ifd1_start + 2 + i * 12
        tag = struct.unpack_from(f"{bo}H", exif_bytes, ep)[0]
        if tag == 0x0201:
            thumb_off_r   = struct.unpack_from(f"{bo}I", exif_bytes, ep + 8)[0]
        elif tag == 0x0202:
            thumb_len_pos = ep + 8

    if thumb_off_r is None:
        raise ValueError("IFD1 has no JPEGInterchangeFormat tag")

    thumb_abs    = tiff_off + thumb_off_r
    old_len      = struct.unpack_from(f"{bo}I", exif_bytes, thumb_len_pos)[0]
    patched      = bytearray(
        exif_bytes[:thumb_abs] + ps_thumb + exif_bytes[thumb_abs + old_len:]
    )
    struct.pack_into(f"{bo}I", patched, thumb_len_pos, len(ps_thumb))
    return bytes(patched)


def pixel_diff_summary(a: Image.Image, b: Image.Image) -> dict:
    a_arr = np.array(a.convert("RGB").resize((160, 160), Image.LANCZOS)).astype(float)
    b_arr = np.array(b.convert("RGB").resize((160, 160), Image.LANCZOS)).astype(float)
    diff  = np.abs(a_arr - b_arr)
    dark_a = (a_arr < 150).any(axis=2).mean() * 100
    dark_b = (b_arr < 150).any(axis=2).mean() * 100
    return {
        "mean_diff":      diff.mean(),
        "max_diff":       diff.max(),
        "dark_pct_thumb": dark_a,
        "dark_pct_full":  dark_b,
        "mismatch":       diff.mean() > 15 or abs(dark_a - dark_b) > 5,
    }


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_info(args):
    path      = args.image
    exif_dict = load_exif(path)
    full      = Image.open(path)
    thumb     = thumb_from_exif(exif_dict)

    print(f"\n{'═'*56}")
    print(f"  FILE:  {Path(path).name}")
    print(f"  FULL:  {full.size[0]}×{full.size[1]}  mode={full.mode}")
    print(f"{'─'*56}")

    if thumb is None:
        print("  THUMBNAIL:  ✗ None embedded")
    else:
        raw_size = len(exif_dict["thumbnail"])
        raw      = exif_dict["thumbnail"]
        has_app14 = b"Adobe" in raw[:60]
        has_app13 = b"Adobe_CM" in raw[:60]
        has_jfif  = b"JFIF"   in raw[:20]
        print(f"  THUMBNAIL:  ✓ {thumb.size[0]}×{thumb.size[1]}  ({raw_size:,} bytes)")
        print(f"  MARKERS:    APP13={has_app13}  APP14/ColorTransform={has_app14}  JFIF={has_jfif}")

        stats = pixel_diff_summary(thumb, full)
        print(f"  MEAN DIFF:  {stats['mean_diff']:.1f}  MAX: {stats['max_diff']:.0f}")
        print(f"  DARK PX %:  thumb={stats['dark_pct_thumb']:.1f}%  full={stats['dark_pct_full']:.1f}%")
        status = "STALE" if stats["mismatch"] else "✓  OK"
        print(f"  STATUS:     {status}")

        out_thumb = args.out or f"{Path(path).stem}_exif_thumb_{ts()}.jpg"
        thumb.save(out_thumb)
        print(f"\n  Thumbnail extracted → {out_thumb}")

    print(f"{'═'*56}\n")


def cmd_regenerate(args):
    path      = args.image
    exif_dict = load_exif(path)
    full      = Image.open(path)

    ps_thumb   = encode_thumb(full, size=args.size, quality=args.quality)
    exif_bytes = build_clean_exif_with_thumb(exif_dict, ps_thumb)

    t = Image.open(io.BytesIO(ps_thumb))
    print(f"\n  Thumbnail: {t.size[0]}×{t.size[1]}  ({len(ps_thumb):,} bytes)  APP13=✓ APP14=✓  quality={args.quality}")
    save_output(path, args.out, exif_bytes, args.no_backup)
    print("  Done.\n")


def cmd_strip(args):
    path      = args.image
    exif_dict = load_exif(path)

    if not exif_dict.get("thumbnail"):
        print(f"\n  No thumbnail in {Path(path).name} — nothing to strip.\n")
        return

    old_size = len(exif_dict["thumbnail"])
    exif_dict["thumbnail"] = b""
    for tag in (piexif.ImageIFD.JPEGInterchangeFormat,
                piexif.ImageIFD.JPEGInterchangeFormatLength):
        exif_dict["1st"].pop(tag, None)

    exif_bytes = piexif.dump(exif_dict)
    print(f"\n  Stripping thumbnail ({old_size:,} bytes removed)")
    save_output(path, args.out, exif_bytes, args.no_backup)
    print("  Done.\n")


def cmd_compare(args):
    path      = args.image
    exif_dict = load_exif(path)
    full      = Image.open(path)
    thumb     = thumb_from_exif(exif_dict)

    if thumb is None:
        print(f"\n  No thumbnail embedded in {Path(path).name}\n")
        return

    SCALE, W, H = 4, 160 * 4, 160 * 4
    PAD, LABEL_H = 16, 28
    canvas = Image.new("RGB", (W * 2 + PAD * 3, H + LABEL_H + PAD * 2), (18, 18, 18))

    thumb_up = thumb.resize((W, H), Image.NEAREST)
    full_up  = full.convert("RGB").resize((160, 160), Image.LANCZOS).resize((W, H), Image.NEAREST)
    canvas.paste(thumb_up, (PAD, LABEL_H + PAD))
    canvas.paste(full_up,  (W + PAD * 2, LABEL_H + PAD))

    draw = ImageDraw.Draw(canvas)
    draw.text((PAD + 4, 6),         "EXIF Thumbnail (gallery preview)", fill=(255, 90, 90))
    draw.text((W + PAD * 2 + 4, 6), "Full image (actual data)",         fill=(90, 220, 90))
    stats = pixel_diff_summary(thumb, full)
    label = (f"mean diff={stats['mean_diff']:.1f}  "
             f"dark: thumb={stats['dark_pct_thumb']:.1f}%  full={stats['dark_pct_full']:.1f}%  "
             f"{'STALE' if stats['mismatch'] else '✓ OK'}")
    draw.text((PAD + 4, H + LABEL_H + PAD + 4), label, fill=(200, 200, 200))

    out = args.out or f"{Path(path).stem}_thumb_compare_{ts()}.jpg"
    canvas.save(out, quality=92)
    print(f"\n  Comparison saved → {out}\n")


def cmd_inject(args):
    path      = args.image
    exif_dict = load_exif(path)

    try:
        src_img = Image.open(args.thumb_file).convert("RGB")
    except Exception as e:
        print(f"[ERROR] Cannot open {args.thumb_file}: {e}")
        sys.exit(1)

    ps_thumb   = encode_thumb(src_img, size=args.size, quality=args.quality)
    exif_bytes = build_clean_exif_with_thumb(exif_dict, ps_thumb)

    t = Image.open(io.BytesIO(ps_thumb))
    print(f"\n  Injecting thumbnail from {Path(args.thumb_file).name}  "
          f"{t.size[0]}×{t.size[1]}  ({len(ps_thumb):,} bytes)  APP13=✓ APP14=✓")
    save_output(path, args.out, exif_bytes, args.no_backup)
    print("  Done.\n")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Review and modify embedded EXIF thumbnails in JPEG files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("image",       help="Path to JPEG file")
    shared.add_argument("--out",       default=None, help="Output path (default: timestamped JPEG)")
    shared.add_argument("--no-backup", action="store_true", help="Skip .bak creation")
    shared.add_argument("--size",      type=int, default=160, help="Thumbnail max dimension (default: 160)")
    shared.add_argument("--quality",   type=int, default=85,  help="JPEG quality (default: 85)")
    sub.add_parser("info",       parents=[shared], help="Show thumbnail info and extract it")
    sub.add_parser("regenerate", parents=[shared], help="Regenerate thumbnail from full image")
    sub.add_parser("strip",      parents=[shared], help="Remove the embedded thumbnail")
    sub.add_parser("compare",    parents=[shared], help="Save a side-by-side comparison image")

    p_inj = sub.add_parser("inject", parents=[shared], help="Inject a custom thumbnail from file")
    p_inj.add_argument("thumb_file", help="Path to image to use as thumbnail")

    p_rsz = sub.add_parser("resize", parents=[shared],
                           help="Upsample image pixels to target long-edge size (tricks Android dimension check)")
    p_rsz.add_argument("--pixels", type=int, default=2000,
                       help="Target long-edge pixel size (default: 2000)")
    p_rsz.add_argument("--force",  action="store_true",
                       help="Allow downsampling too (default: only upsample)")
    p_rsz.add_argument("--ifd0",   default=None, choices=list(IFD0_PRESETS.keys()),
                       help="Also inject camera IFD0 preset")

    p_fat = sub.add_parser("fat", parents=[shared],
                           help="Pad file to target KB + optionally inject camera IFD0 metadata")
    p_fat.add_argument("--kb",       type=int, default=512,
                       help="Target file size in KB (default: 512)")
    p_fat.add_argument("--ifd0",     default=None,
                       choices=list(IFD0_PRESETS.keys()),
                       help="Inject realistic camera IFD0: iphone, samsung, pixel, canon, minimal")
    p_fat.add_argument("--no-regen", action="store_true",
                       help="Skip thumbnail regeneration — only pad + inject IFD0")

    args = parser.parse_args()
    {"info": cmd_info, "regenerate": cmd_regenerate, "strip": cmd_strip,
     "compare": cmd_compare, "inject": cmd_inject, "fat": cmd_fat,
     "resize": cmd_resize}[args.command](args)




# ─── Fat + IFD helpers ────────────────────────────────────────────────────────

# Realistic IFD0 presets — phones use Make/Model presence to decide if EXIF is trustworthy
IFD0_PRESETS = {
    "iphone":  {
        piexif.ImageIFD.Make:             b"Apple",
        piexif.ImageIFD.Model:            b"iPhone 15 Pro",
        piexif.ImageIFD.Orientation:      1,
        piexif.ImageIFD.XResolution:      (72, 1),
        piexif.ImageIFD.YResolution:      (72, 1),
        piexif.ImageIFD.ResolutionUnit:   2,
        piexif.ImageIFD.Software:         b"17.4.1",
        piexif.ImageIFD.DateTime:         datetime.now().strftime("%Y:%m:%d %H:%M:%S").encode(),
        piexif.ImageIFD.YCbCrPositioning: 1,
    },
    "samsung": {
        piexif.ImageIFD.Make:             b"samsung",
        piexif.ImageIFD.Model:            b"SM-S918B",
        piexif.ImageIFD.Orientation:      1,
        piexif.ImageIFD.XResolution:      (72, 1),
        piexif.ImageIFD.YResolution:      (72, 1),
        piexif.ImageIFD.ResolutionUnit:   2,
        piexif.ImageIFD.Software:         b"S918BXXS4CXC2",
        piexif.ImageIFD.DateTime:         datetime.now().strftime("%Y:%m:%d %H:%M:%S").encode(),
        piexif.ImageIFD.YCbCrPositioning: 1,
    },
    "pixel": {
        piexif.ImageIFD.Make:             b"Google",
        piexif.ImageIFD.Model:            b"Pixel 8 Pro",
        piexif.ImageIFD.Orientation:      1,
        piexif.ImageIFD.XResolution:      (72, 1),
        piexif.ImageIFD.YResolution:      (72, 1),
        piexif.ImageIFD.ResolutionUnit:   2,
        piexif.ImageIFD.Software:         b"Pixel Experience",
        piexif.ImageIFD.DateTime:         datetime.now().strftime("%Y:%m:%d %H:%M:%S").encode(),
        piexif.ImageIFD.YCbCrPositioning: 1,
    },
    "canon": {
        piexif.ImageIFD.Make:             b"Canon",
        piexif.ImageIFD.Model:            b"Canon EOS R6",
        piexif.ImageIFD.Orientation:      1,
        piexif.ImageIFD.XResolution:      (72, 1),
        piexif.ImageIFD.YResolution:      (72, 1),
        piexif.ImageIFD.ResolutionUnit:   2,
        piexif.ImageIFD.Software:         b"Firmware 1.8.0",
        piexif.ImageIFD.DateTime:         datetime.now().strftime("%Y:%m:%d %H:%M:%S").encode(),
        piexif.ImageIFD.YCbCrPositioning: 2,
    },
    "minimal": {
        piexif.ImageIFD.Make:             b"Camera",
        piexif.ImageIFD.Model:            b"Generic",
        piexif.ImageIFD.Orientation:      1,
        piexif.ImageIFD.XResolution:      (72, 1),
        piexif.ImageIFD.YResolution:      (72, 1),
        piexif.ImageIFD.ResolutionUnit:   2,
        piexif.ImageIFD.DateTime:         datetime.now().strftime("%Y:%m:%d %H:%M:%S").encode(),
    },
}


def pad_jpeg(jpeg_bytes: bytes, target_kb: int) -> bytes:
    """
    Inflate a JPEG to at least target_kb by injecting a JPEG COM (comment) segment
    filled with null bytes immediately after SOI. This is the cleanest padding
    method — COM segments are ignored by all decoders but count toward file size,
    which is what gallery apps use to decide whether to read EXIF thumbnails.
    COM segment max payload is 65533 bytes (64KB - 2 for length field).
    Multiple COM segments are injected if needed.
    """
    target_bytes = target_kb * 1024
    current     = len(jpeg_bytes)
    if current >= target_bytes:
        return jpeg_bytes

    needed = target_bytes - current
    chunks = []
    while needed > 0:
        chunk_size = min(needed, 65533)
        # COM marker: FF FE + 2-byte length (includes length field) + payload
        payload  = b"\x00" * chunk_size
        segment  = b"\xff\xfe" + struct.pack(">H", chunk_size + 2) + payload
        chunks.append(segment)
        needed  -= chunk_size

    # Insert all COM segments right after SOI (first 2 bytes)
    result = jpeg_bytes[:2] + b"".join(chunks) + jpeg_bytes[2:]
    return result


def cmd_fat(args):
    path      = args.image
    target_kb = args.kb
    preset    = args.ifd0

    with open(path, "rb") as f:
        jpeg_bytes = f.read()

    original_kb = len(jpeg_bytes) / 1024
    exif_dict   = load_exif(path)

    # Optionally inject IFD0 preset
    if preset:
        if preset not in IFD0_PRESETS:
            print(f"[ERROR] Unknown preset '{preset}'. Available: {', '.join(IFD0_PRESETS)}")
            sys.exit(1)
        exif_dict["0th"] = dict(IFD0_PRESETS[preset])
        # Update PixelXDimension / PixelYDimension in Exif IFD to match actual image
        full = Image.open(path)
        if "Exif" not in exif_dict or exif_dict["Exif"] is None:
            exif_dict["Exif"] = {}
        exif_dict["Exif"][piexif.ExifIFD.PixelXDimension] = full.width
        exif_dict["Exif"][piexif.ExifIFD.PixelYDimension] = full.height
        print(f"  IFD0 preset '{preset}' injected  "
              f"(Make={IFD0_PRESETS[preset][piexif.ImageIFD.Make].decode()}  "
              f"Model={IFD0_PRESETS[preset][piexif.ImageIFD.Model].decode()})")

    # Re-encode thumbnail while we're at it (also regenerates with PS-style markers)
    if not args.no_regen:
        full      = Image.open(path)
        ps_thumb  = encode_thumb(full, size=args.size, quality=args.quality)
        exif_bytes_new = build_clean_exif_with_thumb(exif_dict, ps_thumb)
        t = Image.open(io.BytesIO(ps_thumb))
        print(f"  Thumbnail regenerated: {t.size[0]}×{t.size[1]}  APP13=✓ APP14=✓")
    else:
        exif_bytes_new = piexif.dump(exif_dict)

    # Write EXIF back first
    out_tmp = io.BytesIO()
    piexif.insert(exif_bytes_new, path, out_tmp)
    jpeg_with_exif = out_tmp.getvalue()

    # Now pad to target size
    padded = pad_jpeg(jpeg_with_exif, target_kb)
    actual_kb = len(padded) / 1024

    target_str = f"out or"
    out = args.out or f"{Path(path).stem}_fat_{ts()}.jpg"
    with open(out, "wb") as f:
        f.write(padded)

    print(f"  Padded: {original_kb:.1f} KB → {actual_kb:.1f} KB  (target: {target_kb} KB)")
    print(f"  Saved → {out}")
    print()

def cmd_resize(args):
    """
    Resample the full image pixels to a target size, regenerate the thumbnail
    to match, and preserve/fix all EXIF. This tricks Android's dimension check
    since it reads pixel size from the SOF0 marker, not file size.
    """
    path = args.image
    full = Image.open(path)
    orig_w, orig_h = full.size

    # Compute target dimensions preserving AR
    target_long = args.pixels
    if orig_w >= orig_h:
        new_w = target_long
        new_h = int(target_long * orig_h / orig_w)
    else:
        new_h = target_long
        new_w = int(target_long * orig_w / orig_h)

    if new_w <= orig_w and not args.force:
        print(f"\n  Image is already {orig_w}×{orig_h} — larger than target {new_w}×{new_h}.")
        print(f"  Use --force to downsample anyway.\n")
        return

    print(f"\n  Resampling: {orig_w}×{orig_h} → {new_w}×{new_h}  (long edge = {target_long}px)")

    resized = full.resize((new_w, new_h), Image.LANCZOS)

    # Carry EXIF over, updating pixel dimension tags
    exif_dict = load_exif(path)
    if "Exif" not in exif_dict or not exif_dict["Exif"]:
        exif_dict["Exif"] = {}
    exif_dict["Exif"][piexif.ExifIFD.PixelXDimension] = new_w
    exif_dict["Exif"][piexif.ExifIFD.PixelYDimension] = new_h

    # Update IFD0 resolution if present
    if exif_dict.get("0th"):
        exif_dict["0th"][piexif.ImageIFD.XResolution] = (72, 1)
        exif_dict["0th"][piexif.ImageIFD.YResolution] = (72, 1)

    # Optionally inject IFD0 preset
    if args.ifd0:
        if args.ifd0 not in IFD0_PRESETS:
            print(f"[ERROR] Unknown preset '{args.ifd0}'")
            sys.exit(1)
        exif_dict["0th"] = dict(IFD0_PRESETS[args.ifd0])
        exif_dict["0th"][piexif.ImageIFD.XResolution] = (72, 1)
        exif_dict["0th"][piexif.ImageIFD.YResolution] = (72, 1)
        exif_dict["Exif"][piexif.ExifIFD.PixelXDimension] = new_w
        exif_dict["Exif"][piexif.ExifIFD.PixelYDimension] = new_h
        print(f"  IFD0 preset '{args.ifd0}' injected")

    # Regenerate thumbnail from the resized image
    ps_thumb  = encode_thumb(resized, size=args.size, quality=args.quality)
    exif_bytes = build_clean_exif_with_thumb(exif_dict, ps_thumb)

    t = Image.open(io.BytesIO(ps_thumb))
    print(f"  Thumbnail: {t.size[0]}×{t.size[1]}  APP13=✓ APP14=✓"
)

    # Save resized JPEG with new EXIF
    out = args.out or f"{Path(path).stem}_resized_{ts()}.jpg"
    tmp_buf = io.BytesIO()
    resized.save(tmp_buf, format="JPEG", quality=args.quality)
    tmp_bytes = tmp_buf.getvalue()

    # Insert our patched EXIF into the resized JPEG bytes
    out_buf = io.BytesIO()
    piexif.insert(exif_bytes, tmp_bytes, out_buf)
    with open(out, "wb") as f:
        f.write(out_buf.getvalue())

    final_kb = len(out_buf.getvalue()) / 1024
    print(f"  Saved → {out}  ({final_kb:.1f} KB)\n")

if __name__ == "__main__":
    main()
