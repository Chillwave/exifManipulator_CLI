# exifManipulator_CLI
A command-line tool for inspecting and replacing embedded EXIF thumbnails in JPEG files.

## Context

JPEG files can contain a small thumbnail image embedded in their EXIF metadata (IFD1). Gallery apps on Windows, iOS, and Android use this thumbnail for fast grid previews instead of decoding the full image. When the thumbnail is stale or malformed — common after editing in Photoshop, cropping, or resizing — the gallery shows the wrong image in preview but the correct one when opened.

This tool fixes that, and also handles edge cases like images too small for phones to bother reading EXIF from.

## Install

```bash
pip install Pillow piexif numpy
```

## Commands

### `info` — Inspect the current thumbnail

```bash
python exif_thumb.py info photo.jpg
```

Shows thumbnail dimensions, file size, whether APP13/APP14 color markers are present, and a pixel diff score indicating if the thumbnail is stale. Also extracts the thumbnail to a timestamped JPEG for manual inspection.

```
══════════════════════════════════════════════════════
  FILE:  photo.jpg
  FULL:  2000×2000  mode=RGB
────────────────────────────────────────────────────────
  THUMBNAIL:  ✓ 160×160  (3,307 bytes)
  MARKERS:    APP13=True  APP14/ColorTransform=True  JFIF=False
  MEAN DIFF:  11.4  MAX: 244
  DARK PX %:  thumb=6.6%  full=14.6%
  STATUS:     STALE — thumbnail does not match the full image
```

---

### `regenerate` — Fix a stale thumbnail

```bash
python exif_thumb.py regenerate photo.jpg
```

Regenerates the embedded thumbnail from the full image. The output is written to a timestamped file by default so the original is never overwritten.

```bash
python exif_thumb.py regenerate photo.jpg --out fixed.jpg
python exif_thumb.py regenerate photo.jpg --size 256 --quality 90
```

**Options:**
- `--out` — output path (default: `photo_YYYYMMDD_HHMMSS.jpg`)
- `--size` — thumbnail max dimension in pixels (default: `160`)
- `--quality` — JPEG quality for the thumbnail (default: `85`)
- `--no-backup` — skip creating a `.bak` before in-place edits

---

### `compare` — Visual diff

```bash
python exif_thumb.py compare photo.jpg
```

Saves a side-by-side comparison image showing the embedded thumbnail on the left and a scaled-down version of the actual image on the right, with a pixel diff score. Useful for confirming a thumbnail is stale before fixing it.

---

### `strip` — Remove the thumbnail entirely

```bash
python exif_thumb.py strip photo.jpg
```

Removes the embedded thumbnail and cleans up the IFD1 offset tags. The gallery app will generate its own preview from the full image data.

---

### `inject` — Embed a custom image as the thumbnail

```bash
python exif_thumb.py inject photo.jpg thumbnail_source.jpg
```

Encodes `thumbnail_source.jpg` and embeds it as the EXIF thumbnail inside `photo.jpg`. The source image is resized to fit within `--size` pixels before embedding.

```bash
python exif_thumb.py inject photo.jpg other.jpg --size 200
```

---

### `resize` — Upsample image pixels

```bash
python exif_thumb.py resize photo.jpg --pixels 2000
```

Resamples the full image to a larger pixel size and regenerates the thumbnail to match. Useful when the source image is too small for Android or iOS to bother reading its EXIF thumbnail — both platforms have a minimum pixel dimension threshold below which they decode the full JPEG directly and ignore the embedded thumbnail.

```bash
# Upsample to 2000px on the long edge (default)
python exif_thumb.py resize small.jpg --pixels 2000

# Also inject camera metadata so IFD0 looks like a real device photo
python exif_thumb.py resize small.jpg --pixels 2000 --ifd0 iphone

# Downsample is also allowed with --force
python exif_thumb.py resize huge.jpg --pixels 1000 --force
```

**Options:**
- `--pixels` — target long-edge size in pixels (default: `2000`)
- `--force` — allow downsampling (default: only upsamples)
- `--ifd0` — inject a camera IFD0 preset (see below)
- `--quality` — JPEG quality for the output image (default: `85`)

---

### `fat` — Pad file size

```bash
python exif_thumb.py fat photo.jpg --kb 512
```

Pads the file to a target size by injecting JPEG COM (comment) segments filled with null bytes. COM segments are ignored by all decoders but count toward file size. Also regenerates the thumbnail at the same time.

The `--ifd0` option is particularly useful here if the source file has an empty IFD0 (no Make, Model, DateTime) — some gallery apps treat missing camera metadata as a signal to skip EXIF thumbnail reading.

```bash
python exif_thumb.py fat photo.jpg --kb 600 --ifd0 samsung
python exif_thumb.py fat photo.jpg --kb 512 --no-regen   # pad only, don't touch thumbnail
```

**Options:**
- `--kb` — target file size in KB (default: `512`)
- `--ifd0` — inject a camera IFD0 preset (see below)
- `--no-regen` — skip thumbnail regeneration, only pad

---

## IFD0 Presets

Several commands accept `--ifd0` to populate the main image metadata block with realistic camera-style tags. This matters because gallery apps on Android and iOS use Make/Model presence to decide whether EXIF data is trustworthy.

| Preset | Make | Model |
|---|---|---|
| `iphone` | Apple | iPhone 15 Pro |
| `samsung` | samsung | SM-S918B |
| `pixel` | Google | Pixel 8 Pro |
| `canon` | Canon | Canon EOS R6 |
| `minimal` | Camera | Generic |

All presets include Orientation, XResolution, YResolution, ResolutionUnit, Software, and DateTime.

---

## Common Options

All commands accept these flags:

| Flag | Default | Description |
|---|---|---|
| `--out <path>` | timestamped JPEG | Output file path |
| `--size <px>` | `160` | Thumbnail max dimension |
| `--quality <1-95>` | `85` | JPEG quality for thumbnail |
| `--no-backup` | off | Skip `.bak` creation on in-place edits |

---

## How the thumbnail encoding works

Pillow's JPEG encoder writes a JFIF APP0 header but omits the APP14 Adobe color transform marker. Strict readers on iOS, Android MTP/PTP, and Windows Photos use the APP14 `ColorTransform` byte to decide whether to interpret pixel channels as YCbCr or RGB. Without it, thumbnails may be silently rejected or render with incorrect colors.

This tool emulates Photoshop CC's thumbnail structure exactly:

```
SOI → APP13 (Adobe_CM) → APP14 (Adobe, ColorTransform=1/YCbCr) → DQT → SOF → DHT → SOS
```

Since `piexif.dump()` validates thumbnail JPEGs and rejects non-standard APP segments, the tool uses a raw TIFF IFD splice: piexif writes a clean placeholder, then the tool locates the thumbnail by reading the IFD1 offset pointer and swaps in the correctly-structured bytes.

---

## Typical workflows

**Fix a stale Photoshop export (the main use case):**
```bash
python exif_thumb.py info shirt.jpg          # confirm it's stale
python exif_thumb.py regenerate shirt.jpg    # fix it
```

**Fix a small web image that phones ignore:**
```bash
python exif_thumb.py resize small.jpg --pixels 2000 --ifd0 iphone
```

**Embed a completely different image as the preview:**
```bash
python exif_thumb.py inject photo.jpg cover.jpg
```

**Nuke the thumbnail and let the gallery regenerate:**
```bash
python exif_thumb.py strip photo.jpg
```
