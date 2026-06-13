"""
yimage.processing
~~~~~~~~~~~~~~~~~
Server-side image and PDF processing pipelines.
All functions take bytes in, return bytes out — no Flask dependency.

Tools:
  1.  compress_pdf        — shrink PDF by recompressing images
  2.  pdf_to_images       — render PDF pages to JPEG/PNG
  3.  images_to_pdf       — merge images into a single PDF
  4.  crop_image          — crop image using canvas coordinates
  5.  detect_face         — face detection for passport photos
  6.  make_passport_photo — crop + resize + background for passport
  7.  pdf_to_text         — extract text from PDF (+ OCR fallback)
  8.  trim_transparency   — remove transparent borders from PNG
  9.  analyze_layers      — separate RGB channels + K-means color clusters
  10. resize_convert_image — resize and/or convert image format
  11. rotate_flip_image   — rotate by angle and/or flip horizontally/vertically
  12. extract_exif        — read EXIF/metadata tags from image
  13. strip_exif          — remove all metadata from image
  14. merge_pdfs          — combine multiple PDFs into one
  15. split_pdf           — split a PDF into one PDF per page (ZIP)
  16. generate_qr         — generate a QR code PNG
"""
from __future__ import annotations

import io
import logging
import zipfile
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Compress PDF
# ---------------------------------------------------------------------------

_QUALITY_MAP = {"low": 30, "medium": 55, "high": 75}


def compress_pdf(data: bytes, quality: str = "medium") -> bytes:
    """Compress a PDF by recompressing embedded images and stripping metadata."""
    import pikepdf

    jpeg_quality = _QUALITY_MAP.get(quality, 55)
    src = pikepdf.open(io.BytesIO(data))

    for page in src.pages:
        _compress_page_images(page, jpeg_quality)

    # Strip metadata
    if hasattr(src, "docinfo"):
        try:
            del src.docinfo
        except Exception:
            pass

    buf = io.BytesIO()
    src.save(buf, linearize=True, compress_streams=True,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)
    src.close()
    return buf.getvalue()


def _compress_page_images(page, jpeg_quality: int) -> None:
    """Recompress images on a PDF page to the given JPEG quality."""
    import pikepdf

    try:
        resources = page.get("/Resources", {})
        xobjects = resources.get("/XObject", {})

        for key in list(xobjects.keys()):
            xobj = xobjects[key]
            if not isinstance(xobj, pikepdf.Stream):
                continue
            subtype = xobj.get("/Subtype")
            if str(subtype) != "/Image":
                continue

            try:
                width = int(xobj.get("/Width", 0))
                height = int(xobj.get("/Height", 0))
                if width < 10 or height < 10:
                    continue

                # Read raw image data and recompress as JPEG
                raw = xobj.read_raw_bytes()
                pil_img = Image.open(io.BytesIO(raw))
                pil_img = pil_img.convert("RGB")

                jpg_buf = io.BytesIO()
                pil_img.save(jpg_buf, format="JPEG", quality=jpeg_quality, optimize=True)

                # Only replace if smaller
                if len(jpg_buf.getvalue()) < len(raw):
                    xobj.write(jpg_buf.getvalue(), filter=pikepdf.Name("/DCTDecode"))
                    xobj["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
                    xobj["/BitsPerComponent"] = 8
            except Exception:
                continue  # Skip images that can't be recompressed
    except Exception as exc:
        log.debug("Could not process page images: %s", exc)


# ---------------------------------------------------------------------------
# 2. PDF to Image
# ---------------------------------------------------------------------------

def pdf_to_images(data: bytes, fmt: str = "jpeg", dpi: int = 150,
                  filename: str = "document.pdf") -> tuple[bytes, str, str]:
    """Convert PDF pages to images. Returns (result_bytes, content_type, download_name)."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    page_count = len(doc)
    base = filename.rsplit(".", 1)[0] if "." in filename else filename

    pil_format = "JPEG" if fmt in ("jpeg", "jpg") else "PNG"
    ext = "jpg" if fmt in ("jpeg", "jpg") else "png"
    mime = "image/jpeg" if pil_format == "JPEG" else "image/png"

    images: list[tuple[bytes, str]] = []
    for i, page in enumerate(doc):
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)

        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format=pil_format, quality=85 if pil_format == "JPEG" else None)
        images.append((buf.getvalue(), f"{base}_page{i + 1}.{ext}"))

    doc.close()

    if page_count == 1:
        return images[0][0], mime, images[0][1]

    # Multi-page: bundle into ZIP
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for img_data, img_name in images:
            zf.writestr(img_name, img_data)

    return zip_buf.getvalue(), "application/zip", f"{base}_images.zip"


# ---------------------------------------------------------------------------
# 3. Image to PDF
# ---------------------------------------------------------------------------

def images_to_pdf(images_data: list[bytes]) -> bytes:
    """Merge multiple images into a single PDF."""
    pil_images = []
    for data in images_data:
        img = Image.open(io.BytesIO(data))
        if img.mode == "RGBA":
            # Flatten alpha onto white background for PDF
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        pil_images.append(img)

    if not pil_images:
        raise ValueError("No valid images provided")

    buf = io.BytesIO()
    if len(pil_images) == 1:
        pil_images[0].save(buf, format="PDF")
    else:
        pil_images[0].save(buf, format="PDF", save_all=True,
                           append_images=pil_images[1:])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 4. Crop Image
# ---------------------------------------------------------------------------

_FORMAT_MIME = {
    "JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
    "GIF": "image/gif", "BMP": "image/bmp",
}


def crop_image(data: bytes, x: float, y: float, w: float, h: float,
               canvas_w: float, canvas_h: float) -> tuple[bytes, str]:
    """Crop image using canvas coordinates. Returns (result_bytes, mime_type)."""
    img = Image.open(io.BytesIO(data))
    orig_fmt = img.format or "PNG"

    # Scale canvas coordinates to actual image dimensions
    scale_x = img.width / canvas_w
    scale_y = img.height / canvas_h
    left = int(x * scale_x)
    upper = int(y * scale_y)
    right = int((x + w) * scale_x)
    lower = int((y + h) * scale_y)

    # Clamp to image bounds
    left = max(0, left)
    upper = max(0, upper)
    right = min(img.width, right)
    lower = min(img.height, lower)

    cropped = img.crop((left, upper, right, lower))

    buf = io.BytesIO()
    save_fmt = orig_fmt if orig_fmt in _FORMAT_MIME else "PNG"
    if save_fmt == "JPEG" and cropped.mode == "RGBA":
        cropped = cropped.convert("RGB")
    cropped.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)

    return buf.getvalue(), _FORMAT_MIME.get(save_fmt, "image/png")


# ---------------------------------------------------------------------------
# 5. Passport Photo — Face Detection
# ---------------------------------------------------------------------------

_PASSPORT_SIZES = {
    "us_2x2":  (600, 600),    # 2x2 inches at 300 DPI
    "eu_35x45": (413, 531),   # 35x45mm at 300 DPI
    "cn_33x48": (390, 567),   # 33x48mm at 300 DPI
    "uk_35x45": (413, 531),   # same as EU
}

# Print sheet sizes in pixels at 300 DPI
_PRINT_SHEETS = {
    "4x6": (1800, 1200),   # 6x4 inches at 300 DPI (landscape)
    "5x7": (2100, 1500),   # 7x5 inches at 300 DPI (landscape)
    "a4":  (3508, 2480),   # A4 landscape at 300 DPI
}


def detect_face(data: bytes) -> Optional[dict]:
    """Detect face in image, return bounding box as fractions of image dimensions."""
    try:
        import mediapipe as mp
    except ImportError:
        # Fallback: return center-weighted default crop
        log.warning("mediapipe not installed, using center crop fallback")
        return {"x": 0.25, "y": 0.1, "w": 0.5, "h": 0.7, "confidence": 0, "method": "fallback"}

    img = Image.open(io.BytesIO(data)).convert("RGB")
    import numpy as np
    img_array = np.array(img)

    with mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=0.5
    ) as face_det:
        results = face_det.process(img_array)

    if not results.detections:
        return None

    det = results.detections[0]
    bbox = det.location_data.relative_bounding_box
    return {
        "x": float(bbox.xmin),
        "y": float(bbox.ymin),
        "w": float(bbox.width),
        "h": float(bbox.height),
        "confidence": float(det.score[0]),
        "method": "mediapipe",
    }


def make_passport_photo(data: bytes, size: str = "us_2x2",
                        bg_color: str = "#ffffff",
                        crop_rect: tuple | None = None,
                        print_layout: str = "single") -> bytes:
    """Generate a passport photo with optional manual crop and print sheet layout.

    Args:
        crop_rect: (x, y, w, h) as fractions of image dimensions (from canvas UI)
        print_layout: 'single', '4x6', '5x7', or 'a4'
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    target_w, target_h = _PASSPORT_SIZES.get(size, (600, 600))
    bg_rgb = tuple(int(bg_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    if crop_rect and crop_rect[2] > 0 and crop_rect[3] > 0:
        # Use manual crop from canvas UI
        cx, cy, cw, ch = crop_rect
        left = int(cx * img.width)
        upper = int(cy * img.height)
        right = int((cx + cw) * img.width)
        lower = int((cy + ch) * img.height)
        cropped = img.crop((max(0, left), max(0, upper), min(img.width, right), min(img.height, lower)))
    else:
        # Auto-detect face and crop
        face = detect_face(data)
        if not face:
            face = {"x": 0.25, "y": 0.1, "w": 0.5, "h": 0.7}

        face_cx = (face["x"] + face["w"] / 2) * img.width
        face_h = face["h"] * img.height

        frame_h = face_h / 0.55
        frame_w = frame_h * (target_w / target_h)

        head_top = face["y"] * img.height
        top = head_top - frame_h * 0.15
        left = face_cx - frame_w / 2

        left = max(0, min(img.width - frame_w, left))
        top = max(0, min(img.height - frame_h, top))

        cropped = img.crop((int(left), int(top), int(left + frame_w), int(top + frame_h)))

    # Resize to target passport dimensions
    photo = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # Apply background color
    canvas = Image.new("RGB", (target_w, target_h), bg_rgb)
    canvas.paste(photo, (0, 0))
    photo = canvas

    # Generate print sheet if requested
    if print_layout in _PRINT_SHEETS:
        photo = _make_print_sheet(photo, print_layout, bg_rgb)

    buf = io.BytesIO()
    photo.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _make_print_sheet(photo: Image.Image, layout: str, bg_rgb: tuple) -> Image.Image:
    """Tile a passport photo onto a print sheet (4x6, 5x7, A4)."""
    sheet_w, sheet_h = _PRINT_SHEETS[layout]
    pw, ph = photo.size

    # Add small gap between photos (2mm ~ 24px at 300 DPI)
    gap = 24

    # Calculate grid
    cols = (sheet_w + gap) // (pw + gap)
    rows = (sheet_h + gap) // (ph + gap)

    if cols < 1:
        cols = 1
    if rows < 1:
        rows = 1

    # Center the grid on the sheet
    total_w = cols * pw + (cols - 1) * gap
    total_h = rows * ph + (rows - 1) * gap
    offset_x = (sheet_w - total_w) // 2
    offset_y = (sheet_h - total_h) // 2

    sheet = Image.new("RGB", (sheet_w, sheet_h), bg_rgb)
    for r in range(rows):
        for c in range(cols):
            x = offset_x + c * (pw + gap)
            y = offset_y + r * (ph + gap)
            sheet.paste(photo, (x, y))

    log.info("Print sheet %s: %dx%d grid (%d photos) on %dx%d",
             layout, cols, rows, cols * rows, sheet_w, sheet_h)
    return sheet


# ---------------------------------------------------------------------------
# 6. PDF to Text
# ---------------------------------------------------------------------------

def pdf_to_text(data: bytes) -> dict:
    """Extract text from PDF. Uses direct extraction first, OCR as fallback."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    pages = []
    method = "direct"
    total_text = []

    for i, page in enumerate(doc):
        text = page.get_text("text").strip()

        # If no text found, try OCR
        if not text:
            ocr_text = _ocr_page(page)
            if ocr_text:
                text = ocr_text
                method = "ocr"

        pages.append({
            "page": i + 1,
            "text": text,
            "chars": len(text),
        })
        total_text.append(text)

    doc.close()
    full_text = "\n\n--- Page Break ---\n\n".join(total_text)

    return {
        "text": full_text,
        "pages": pages,
        "page_count": len(pages),
        "total_chars": len(full_text),
        "method": method,
    }


def _ocr_page(page) -> str:
    """OCR a single PDF page using Tesseract (if available)."""
    try:
        import pytesseract
    except ImportError:
        return ""

    try:
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        text = pytesseract.image_to_string(img, lang="eng")
        return text.strip()
    except Exception as exc:
        log.debug("OCR failed for page: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# 7. Trim Transparency
# ---------------------------------------------------------------------------

def trim_transparency(data: bytes, bg_color: Optional[str] = None) -> bytes:
    """Trim transparent borders from a PNG. Optionally replace transparency with a solid color."""
    img = Image.open(io.BytesIO(data)).convert("RGBA")

    # Get bounding box of non-transparent pixels
    alpha = img.split()[3]
    bbox = alpha.getbbox()

    if not bbox:
        raise ValueError("Image is fully transparent — nothing to trim")

    trimmed = img.crop(bbox)

    if bg_color:
        # Replace transparency with solid color
        bg_rgb = tuple(int(bg_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        canvas = Image.new("RGB", trimmed.size, bg_rgb)
        canvas.paste(trimmed, mask=trimmed.split()[3])
        trimmed = canvas

    buf = io.BytesIO()
    fmt = "PNG" if trimmed.mode == "RGBA" else "PNG"
    trimmed.save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 8. Layer Analysis
# ---------------------------------------------------------------------------

def analyze_layers(data: bytes, mode: str = "both") -> bytes:
    """Separate image into RGB channels and/or color clusters. Returns ZIP."""
    img = Image.open(io.BytesIO(data)).convert("RGB")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if mode in ("channels", "both"):
            _add_channel_layers(img, zf)
        if mode in ("colors", "both"):
            _add_color_layers(img, zf)

    return zip_buf.getvalue()


def _add_channel_layers(img: Image.Image, zf: zipfile.ZipFile) -> None:
    """Add RGB channel separation to the ZIP."""
    r, g, b = img.split()

    for channel, name, color in [(r, "red", (255, 0, 0)), (g, "green", (0, 255, 0)), (b, "blue", (0, 0, 255))]:
        # Create colored version of the channel
        import numpy as np
        arr = np.array(channel)
        colored = np.zeros((*arr.shape, 3), dtype=np.uint8)
        for i, c in enumerate(color):
            if c > 0:
                colored[:, :, i] = arr
        layer_img = Image.fromarray(colored)

        buf = io.BytesIO()
        layer_img.save(buf, format="PNG")
        zf.writestr(f"channel_{name}.png", buf.getvalue())

    # Also add grayscale versions
    for channel, name in [(r, "red"), (g, "green"), (b, "blue")]:
        buf = io.BytesIO()
        channel.save(buf, format="PNG")
        zf.writestr(f"channel_{name}_gray.png", buf.getvalue())


def _add_color_layers(img: Image.Image, zf: zipfile.ZipFile, n_clusters: int = 5) -> None:
    """Add K-means color cluster layers to the ZIP."""
    import numpy as np

    arr = np.array(img)
    h, w, _ = arr.shape
    pixels = arr.reshape(-1, 3).astype(np.float32)

    try:
        from sklearn.cluster import MiniBatchKMeans
        kmeans = MiniBatchKMeans(n_clusters=n_clusters, random_state=42, n_init=3)
        labels = kmeans.fit_predict(pixels)
        centers = kmeans.cluster_centers_.astype(np.uint8)
    except ImportError:
        # Fallback: simple manual k-means with numpy
        centers, labels = _simple_kmeans(pixels, n_clusters)

    for i in range(n_clusters):
        mask = (labels == i).reshape(h, w)
        # Create RGBA image: original pixels where mask is True, transparent elsewhere
        layer = np.zeros((h, w, 4), dtype=np.uint8)
        layer[mask] = np.concatenate([arr[mask], np.full((mask.sum(), 1), 255, dtype=np.uint8)], axis=1)
        layer_img = Image.fromarray(layer, "RGBA")

        color_hex = "".join(f"{c:02x}" for c in centers[i])

        buf = io.BytesIO()
        layer_img.save(buf, format="PNG")
        zf.writestr(f"color_layer_{i + 1}_{color_hex}.png", buf.getvalue())

    # Add a color palette summary
    palette_info = "\n".join(
        f"Layer {i + 1}: #{(''.join(f'{c:02x}' for c in centers[i]))} ({int((labels == i).sum() / len(labels) * 100)}%)"
        for i in range(n_clusters)
    )
    zf.writestr("color_palette.txt", palette_info)


def _simple_kmeans(pixels, k: int, max_iter: int = 20):
    """Simple K-means implementation using only numpy (fallback if sklearn unavailable)."""
    import numpy as np
    n = len(pixels)
    indices = np.random.choice(n, k, replace=False)
    centers = pixels[indices].copy()

    for _ in range(max_iter):
        # Assign each pixel to nearest center
        dists = np.sqrt(((pixels[:, np.newaxis] - centers[np.newaxis]) ** 2).sum(axis=2))
        labels = dists.argmin(axis=1)

        # Update centers
        new_centers = np.zeros_like(centers)
        for i in range(k):
            mask = labels == i
            if mask.sum() > 0:
                new_centers[i] = pixels[mask].mean(axis=0)
            else:
                new_centers[i] = centers[i]

        if np.allclose(centers, new_centers):
            break
        centers = new_centers

    return centers.astype(np.uint8), labels


# ---------------------------------------------------------------------------
# 10. Resize & Convert Image
# ---------------------------------------------------------------------------

def resize_convert_image(
    data: bytes,
    width: int | None,
    height: int | None,
    fmt: str = "jpeg",
    keep_aspect: bool = True,
) -> tuple[bytes, str]:
    """Resize an image and/or convert its format.

    Args:
        width: Target width in pixels (None = derive from height or keep original).
        height: Target height in pixels (None = derive from width or keep original).
        fmt: Output format key: 'jpeg', 'png', 'webp', or 'original'.
        keep_aspect: When both dimensions are given, scale to fit inside the box.
    """
    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "PNG").upper()

    # Resolve output format
    target_fmt = fmt.upper()
    if target_fmt in ("JPG",):
        target_fmt = "JPEG"
    if target_fmt in ("ORIGINAL", ""):
        target_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP", "BMP", "GIF") else "JPEG"

    # Resize if requested
    if width or height:
        ow, oh = img.size
        if keep_aspect:
            if width and not height:
                height = max(1, round(oh * width / ow))
            elif height and not width:
                width = max(1, round(ow * height / oh))
            else:
                ratio = min(width / ow, height / oh)
                width = max(1, round(ow * ratio))
                height = max(1, round(oh * ratio))
        width = max(1, width or ow)
        height = max(1, height or oh)
        img = img.resize((width, height), Image.Resampling.LANCZOS)

    # Mode conversion for JPEG
    if target_fmt == "JPEG":
        if img.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            src = img.convert("RGBA") if img.mode == "P" else img
            if src.mode == "RGBA":
                bg.paste(src, mask=src.split()[3])
            else:
                bg.paste(src)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

    _mime_map = {
        "JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
        "BMP": "image/bmp", "GIF": "image/gif",
    }
    mime = _mime_map.get(target_fmt, "image/png")

    buf = io.BytesIO()
    img.save(buf, format=target_fmt, quality=92 if target_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 11. Rotate & Flip Image
# ---------------------------------------------------------------------------

def rotate_flip_image(
    data: bytes,
    angle: int = 0,
    flip_h: bool = False,
    flip_v: bool = False,
) -> tuple[bytes, str]:
    """Rotate and/or flip an image, preserving its original format."""
    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "PNG").upper()

    if flip_h:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip_v:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if angle % 360:
        img = img.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP", "BMP", "GIF") else "PNG"
    mime = _FORMAT_MIME.get(save_fmt, "image/png")

    if save_fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 12. EXIF Data — Extract & Strip
# ---------------------------------------------------------------------------

def extract_exif(data: bytes) -> dict:
    """Extract EXIF/metadata tags from an image. Returns a structured dict."""
    img = Image.open(io.BytesIO(data))
    result: dict = {
        "format": img.format or "Unknown",
        "mode": img.mode,
        "width": img.width,
        "height": img.height,
        "exif": [],
        "has_exif": False,
    }

    # Try JPEG/TIFF EXIF
    try:
        from PIL.ExifTags import TAGS, GPSTAGS
        raw = img._getexif()  # type: ignore[attr-defined]
        if raw:
            result["has_exif"] = True
            for tag_id, value in sorted(
                raw.items(), key=lambda kv: TAGS.get(kv[0], str(kv[0]))
            ):
                tag = TAGS.get(tag_id, f"Tag_{tag_id}")
                if tag == "GPSInfo" and isinstance(value, dict):
                    parts = [
                        f"{GPSTAGS.get(k, k)}: {v}" for k, v in value.items()
                    ]
                    result["exif"].append(
                        {"key": "GPS Info", "value": " | ".join(parts), "type": "gps"}
                    )
                    continue
                if isinstance(value, bytes):
                    vstr = f"[Binary: {len(value)} bytes]" if len(value) > 32 else value.hex()
                elif isinstance(value, tuple):
                    vstr = " / ".join(str(v) for v in value)
                else:
                    vstr = str(value)
                result["exif"].append({"key": tag, "value": vstr[:500], "type": "text"})
    except (AttributeError, Exception):
        pass

    # Fallback: image info dict (PNG text chunks, WEBP metadata, etc.)
    if not result["has_exif"]:
        for k, v in (img.info or {}).items():
            if isinstance(v, bytes):
                vstr = f"[Binary: {len(v)} bytes]"
            elif not isinstance(v, str):
                vstr = str(v)
            else:
                vstr = v
            result["exif"].append({"key": str(k), "value": vstr[:500], "type": "info"})
        if result["exif"]:
            result["has_exif"] = True

    return result


def strip_exif(data: bytes) -> tuple[bytes, str]:
    """Remove all metadata from an image. Returns clean bytes."""
    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "JPEG").upper()

    # Rebuild from raw pixel data — strips all attached metadata
    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    if save_fmt == "JPEG" and clean.mode not in ("RGB", "L"):
        clean = clean.convert("RGB")

    buf = io.BytesIO()
    clean.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 13. Merge PDFs
# ---------------------------------------------------------------------------

def merge_pdfs(pdfs_data: list[bytes]) -> bytes:
    """Merge multiple PDFs into a single PDF in order."""
    import fitz

    merged = fitz.open()
    for pdf_bytes in pdfs_data:
        src = fitz.open(stream=pdf_bytes, filetype="pdf")
        merged.insert_pdf(src)
        src.close()

    buf = io.BytesIO()
    merged.save(buf)
    merged.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 14. Split PDF
# ---------------------------------------------------------------------------

def split_pdf(data: bytes, filename: str = "document.pdf") -> bytes:
    """Split a PDF into one PDF per page. Returns a ZIP archive."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    page_count = len(doc)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(page_count):
            single = fitz.open()
            single.insert_pdf(doc, from_page=i, to_page=i)
            pg_buf = io.BytesIO()
            single.save(pg_buf)
            single.close()
            zf.writestr(f"{base}_page{i + 1:03d}.pdf", pg_buf.getvalue())

    doc.close()
    return zip_buf.getvalue()


# ---------------------------------------------------------------------------
# 15. QR Code
# ---------------------------------------------------------------------------

def generate_qr(
    text: str,
    size: str = "medium",
    error_correction: str = "M",
    fg_color: str = "#000000",
    bg_color: str = "#ffffff",
) -> bytes:
    """Generate a QR code and return PNG bytes.

    Args:
        text: Content to encode (text, URL, vCard, etc.)
        size: 'small', 'medium', or 'large' — controls pixel dimensions.
        error_correction: 'L' (7%), 'M' (15%), 'Q' (25%), or 'H' (30%).
        fg_color: Foreground (module) colour as hex string.
        bg_color: Background colour as hex string.
    """
    import qrcode
    from qrcode.constants import (
        ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H,
    )

    ec_map = {
        "L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M,
        "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H,
    }
    box_size_map = {"small": 6, "medium": 10, "large": 16}
    border_map   = {"small": 2, "medium": 4,  "large": 6}

    qr = qrcode.QRCode(
        version=None,
        error_correction=ec_map.get(error_correction.upper(), ERROR_CORRECT_M),
        box_size=box_size_map.get(size, 10),
        border=border_map.get(size, 4),
    )
    qr.add_data(text)
    qr.make(fit=True)

    img = qr.make_image(fill_color=fg_color, back_color=bg_color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 16. Watermark
# ---------------------------------------------------------------------------

_WM_POSITIONS = {
    "center":       (0.5, 0.5),
    "top_left":     (0.05, 0.05),
    "top_right":    (0.95, 0.05),
    "bottom_left":  (0.05, 0.95),
    "bottom_right": (0.95, 0.95),
}


def add_watermark(
    data: bytes,
    text: str,
    position: str = "bottom_right",
    opacity: int = 50,
    font_size: int = 36,
    color: str = "#ffffff",
) -> tuple[bytes, str]:
    """Overlay a semi-transparent text watermark on an image.

    Args:
        text:      Watermark string.
        position:  One of: center, top_left, top_right, bottom_left, bottom_right.
        opacity:   0–100 (percentage).
        font_size: Font size in points.
        color:     Watermark text colour as hex string (e.g. '#ffffff').
    """
    from PIL import ImageDraw, ImageFont

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    orig_fmt = (img.format or "PNG").upper()

    # Build a transparent overlay
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Parse colour + apply opacity
    r = int(color.lstrip("#")[0:2], 16)
    g = int(color.lstrip("#")[2:4], 16)
    b = int(color.lstrip("#")[4:6], 16)
    a = int(opacity / 100 * 255)

    # Try a system font, fall back to PIL default
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

    # Measure text
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # Place watermark
    px, py = _WM_POSITIONS.get(position, (0.95, 0.95))
    x = int(img.width * px - tw / 2)
    y = int(img.height * py - th / 2)
    x = max(4, min(img.width - tw - 4, x))
    y = max(4, min(img.height - th - 4, y))

    # Add a subtle shadow for readability
    shadow_offset = max(1, font_size // 20)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0, a // 2))
    draw.text((x, y), text, font=font, fill=(r, g, b, a))

    # Composite
    out = Image.alpha_composite(img, overlay)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "PNG"
    if save_fmt == "JPEG":
        out = out.convert("RGB")
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/png")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 17. Image Filters & Adjustments
# ---------------------------------------------------------------------------

def apply_filters(
    data: bytes,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    sharpness: float = 1.0,
    preset: str = "none",
) -> tuple[bytes, str]:
    """Apply brightness/contrast/saturation/sharpness adjustments and optional presets.

    Args:
        brightness:  1.0 = original; <1.0 = darker; >1.0 = brighter.
        contrast:    1.0 = original; <1.0 = flat; >1.0 = punchy.
        saturation:  1.0 = original; 0.0 = grayscale; >1.0 = vivid.
        sharpness:   1.0 = original; 0.0 = blurred; >1.0 = sharper.
        preset:      'none', 'grayscale', 'sepia', 'vivid', 'cool', 'warm'.
    """
    from PIL import ImageEnhance

    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "PNG").upper()

    # Apply preset first (overrides individual sliders)
    if preset == "grayscale":
        img = img.convert("L").convert("RGB")
    elif preset == "sepia":
        img = img.convert("RGB")
        import numpy as np
        arr = np.array(img, dtype=np.float32)
        r = arr[:, :, 0] * 0.393 + arr[:, :, 1] * 0.769 + arr[:, :, 2] * 0.189
        g = arr[:, :, 0] * 0.349 + arr[:, :, 1] * 0.686 + arr[:, :, 2] * 0.168
        b = arr[:, :, 0] * 0.272 + arr[:, :, 1] * 0.534 + arr[:, :, 2] * 0.131
        sepia = np.stack([r, g, b], axis=2).clip(0, 255).astype(np.uint8)
        img = Image.fromarray(sepia, "RGB")
    elif preset == "vivid":
        brightness, contrast, saturation, sharpness = 1.05, 1.2, 1.4, 1.2
    elif preset == "cool":
        # Slightly desaturate + blue tint
        img = img.convert("RGB")
        import numpy as np
        arr = np.array(img, dtype=np.int16)
        arr[:, :, 2] = np.clip(arr[:, :, 2] + 15, 0, 255)
        arr[:, :, 0] = np.clip(arr[:, :, 0] - 10, 0, 255)
        img = Image.fromarray(arr.astype(np.uint8), "RGB")
        saturation = 0.85
    elif preset == "warm":
        # Orange/yellow tint
        img = img.convert("RGB")
        import numpy as np
        arr = np.array(img, dtype=np.int16)
        arr[:, :, 0] = np.clip(arr[:, :, 0] + 20, 0, 255)
        arr[:, :, 1] = np.clip(arr[:, :, 1] + 10, 0, 255)
        arr[:, :, 2] = np.clip(arr[:, :, 2] - 10, 0, 255)
        img = Image.fromarray(arr.astype(np.uint8), "RGB")
        saturation = 1.15

    # Apply individual adjustments (if not preset that sets them)
    if preset not in ("grayscale", "sepia"):
        img = img.convert("RGB")

    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    if sharpness != 1.0:
        img = ImageEnhance.Sharpness(img).enhance(sharpness)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    buf = io.BytesIO()
    img.save(buf, format=save_fmt, quality=93 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 18. PDF Protect & Unlock
# ---------------------------------------------------------------------------

def protect_pdf(data: bytes, password: str) -> bytes:
    """Add password protection to a PDF."""
    import pikepdf

    src = pikepdf.open(io.BytesIO(data))
    buf = io.BytesIO()
    src.save(
        buf,
        encryption=pikepdf.Encryption(owner=password, user=password, R=4),
    )
    src.close()
    return buf.getvalue()


def unlock_pdf(data: bytes, password: str) -> bytes:
    """Remove password protection from a PDF. Raises ValueError on wrong password."""
    import pikepdf

    try:
        src = pikepdf.open(io.BytesIO(data), password=password)
    except pikepdf.PasswordError as exc:
        raise ValueError("Incorrect password — could not open PDF") from exc

    buf = io.BytesIO()
    src.save(buf)  # save without encryption
    src.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 19. PDF Page Editor (thumbnails + reorder/delete)
# ---------------------------------------------------------------------------

def get_pdf_page_thumbnails(data: bytes) -> list[dict]:
    """Render each PDF page as a small JPEG thumbnail.  Returns list of dicts."""
    import fitz
    import base64

    doc = fitz.open(stream=data, filetype="pdf")
    pages: list[dict] = []
    for i, page in enumerate(doc):
        # ~120 px wide thumbnail
        scale = 120 / max(page.rect.width, 1)
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=55)
        b64 = base64.b64encode(buf.getvalue()).decode()
        pages.append({
            "index": i,
            "page":  i + 1,
            "w": pix.width,
            "h": pix.height,
            "thumb": f"data:image/jpeg;base64,{b64}",
        })

    doc.close()
    return pages


def apply_pdf_page_order(data: bytes, order: list[int]) -> bytes:
    """Build a new PDF from the given 0-based page indices in the given order."""
    import fitz

    src = fitz.open(stream=data, filetype="pdf")
    out = fitz.open()
    for idx in order:
        if 0 <= idx < len(src):
            out.insert_pdf(src, from_page=idx, to_page=idx)
    buf = io.BytesIO()
    out.save(buf)
    out.close()
    src.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 20. Color Palette Extractor
# ---------------------------------------------------------------------------

def extract_color_palette(data: bytes, n_colors: int = 8) -> list[dict]:
    """Extract the dominant colours from an image using K-means.

    Returns a list of colour dicts sorted by dominance (most frequent first).
    """
    import numpy as np

    img = Image.open(io.BytesIO(data)).convert("RGB")
    # Down-sample for speed (max 300 px on longest side)
    img.thumbnail((300, 300), Image.Resampling.LANCZOS)
    arr = np.array(img)
    pixels = arr.reshape(-1, 3).astype(np.float32)

    try:
        from sklearn.cluster import MiniBatchKMeans
        km = MiniBatchKMeans(n_clusters=n_colors, random_state=42, n_init=3)
        labels = km.fit_predict(pixels)
        centers = km.cluster_centers_.astype(np.uint8)
    except ImportError:
        centers, labels = _simple_kmeans(pixels, n_colors)

    total = len(labels)
    colors: list[dict] = []
    for i in range(n_colors):
        count = int((labels == i).sum())
        r, g, b = int(centers[i][0]), int(centers[i][1]), int(centers[i][2])
        hex_c = f"#{r:02x}{g:02x}{b:02x}"
        # Choose readable text colour (W3C luminance formula)
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        colors.append({
            "hex":        hex_c,
            "r": r, "g": g, "b": b,
            "percentage": round(count / total * 100, 1),
            "text_color": "#000000" if luma > 128 else "#ffffff",
        })

    colors.sort(key=lambda c: c["percentage"], reverse=True)
    return colors


# ---------------------------------------------------------------------------
# 21. Image OCR
# ---------------------------------------------------------------------------

def ocr_image(data: bytes) -> dict:
    """Extract text from an image using Tesseract OCR (pytesseract).

    Returns {text, char_count, method}.
    Raises ImportError if pytesseract is not available.
    """
    try:
        import pytesseract
    except ImportError as exc:
        raise ImportError(
            "pytesseract is not installed — OCR is unavailable on this server"
        ) from exc

    img = Image.open(io.BytesIO(data))
    # Convert to RGB for best Tesseract compatibility
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    text: str = pytesseract.image_to_string(img, lang="eng").strip()
    return {
        "text":       text,
        "char_count": len(text),
        "method":     "tesseract",
    }


# ---------------------------------------------------------------------------
# 22. PDF Page Numbers / Stamp
# ---------------------------------------------------------------------------

def stamp_pdf_pages(
    data: bytes,
    text_template: str = "Page {n} of {total}",
    position: str = "bottom_center",
    font_size: int = 11,
    margin: int = 20,
    color_hex: str = "#444444",
) -> bytes:
    """Stamp page numbers (or any text template) on every page of a PDF.

    Template variables:
        {n}     – current page number (1-based)
        {total} – total page count

    position: bottom_center, bottom_right, bottom_left,
              top_center, top_right, top_left
    """
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    total = len(doc)

    # Parse color
    r = int(color_hex.lstrip("#")[0:2], 16) / 255
    g = int(color_hex.lstrip("#")[2:4], 16) / 255
    b = int(color_hex.lstrip("#")[4:6], 16) / 255

    for i, page in enumerate(doc):
        label = text_template.replace("{n}", str(i + 1)).replace("{total}", str(total))
        pw, ph = page.rect.width, page.rect.height

        # Measure approximate text width (0.5 * font_size * chars is a rough heuristic)
        approx_tw = len(label) * font_size * 0.5

        # Determine (x, y) of text anchor
        if "bottom" in position:
            y = ph - margin
        else:
            y = margin + font_size

        if "center" in position:
            x = (pw - approx_tw) / 2
        elif "right" in position:
            x = pw - approx_tw - margin
        else:
            x = margin

        page.insert_text(
            point=fitz.Point(x, y),
            text=label,
            fontsize=font_size,
            color=(r, g, b),
        )

    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 23. Image Collage / Grid
# ---------------------------------------------------------------------------

def make_collage(
    images_data: list[bytes],
    cols: int = 2,
    gap: int = 10,
    bg_color: str = "#ffffff",
    cell_height: int = 300,
) -> tuple[bytes, str]:
    """Arrange multiple images in a uniform grid.

    All cells are resized to *cell_height* pixels tall (width scales proportionally).
    """
    if not images_data:
        raise ValueError("No images provided")

    # Parse background colour
    bg_rgb = tuple(int(bg_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    # Open & resize each image
    cells: list[Image.Image] = []
    for raw in images_data:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        ratio = cell_height / img.height
        new_w = max(1, int(img.width * ratio))
        cells.append(img.resize((new_w, cell_height), Image.Resampling.LANCZOS))

    cols = max(1, cols)
    rows = (len(cells) + cols - 1) // cols

    # Find max cell width in each column
    col_widths = [0] * cols
    for idx, cell in enumerate(cells):
        col = idx % cols
        col_widths[col] = max(col_widths[col], cell.width)

    total_w = sum(col_widths) + gap * (cols + 1)
    total_h = rows * cell_height + gap * (rows + 1)

    canvas = Image.new("RGB", (total_w, total_h), bg_rgb)

    for idx, cell in enumerate(cells):
        row = idx // cols
        col = idx % cols
        x = gap + sum(col_widths[:col]) + col * gap
        y = gap + row * (cell_height + gap)
        # Centre the cell within its column width
        x_offset = (col_widths[col] - cell.width) // 2
        canvas.paste(cell, (x + x_offset, y))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


# ---------------------------------------------------------------------------
# 24. Image Border / Frame
# ---------------------------------------------------------------------------

def add_border(
    data: bytes,
    border_width: int = 20,
    border_color: str = "#000000",
    radius: int = 0,
) -> tuple[bytes, str]:
    """Add a solid-colour border around an image.

    Args:
        border_width: Width in pixels.
        border_color: Hex colour string.
        radius:       Corner radius in pixels (0 = square corners).
    """
    from PIL import ImageDraw

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    orig_fmt = (img.format or "PNG").upper()

    bw = max(0, border_width)
    bg_rgb = tuple(int(border_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    new_w = img.width + 2 * bw
    new_h = img.height + 2 * bw

    # Create canvas filled with border colour
    canvas = Image.new("RGBA", (new_w, new_h), (*bg_rgb, 255))

    if radius > 0:
        # Apply rounded corners to the original image first
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), (img.width - 1, img.height - 1)], radius=radius, fill=255)
        img.putalpha(mask)
    canvas.paste(img, (bw, bw), img if img.mode == "RGBA" else None)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "PNG"
    if save_fmt == "JPEG":
        canvas = canvas.convert("RGB")
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/png")

    buf = io.BytesIO()
    canvas.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 25. Image Optimizer (compress without resizing)
# ---------------------------------------------------------------------------

def optimize_image(
    data: bytes,
    quality: int = 75,
    output_format: str = "original",
    strip_metadata: bool = True,
) -> tuple[bytes, str]:
    """Reduce image file size without changing dimensions.

    Args:
        quality:        JPEG/WebP quality (1–95).
        output_format:  'original', 'jpeg', 'png', or 'webp'.
        strip_metadata: Remove EXIF/ICC profile for smaller files.
    """
    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "JPEG").upper()

    target = output_format.upper()
    if target in ("", "ORIGINAL"):
        target = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    elif target == "JPG":
        target = "JPEG"

    if strip_metadata:
        clean = Image.new(img.mode if img.mode not in ("P", "LA") else "RGBA", img.size)
        clean.putdata(list(img.getdata()))
        img = clean

    if target == "JPEG" and img.mode not in ("RGB", "L"):
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        else:
            img = img.convert("RGB")

    quality = max(1, min(95, quality))
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(target, "image/jpeg")

    buf = io.BytesIO()
    if target == "PNG":
        img.save(buf, format="PNG", optimize=True)
    elif target == "WEBP":
        img.save(buf, format="WEBP", quality=quality, method=4)
    else:
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 26. Favicon Generator
# ---------------------------------------------------------------------------

def generate_favicons(data: bytes) -> bytes:
    """Generate favicon.ico and PNG sizes. Returns ZIP."""
    img = Image.open(io.BytesIO(data)).convert("RGBA")

    # Square-crop from centre
    if img.width != img.height:
        side = min(img.width, img.height)
        x = (img.width  - side) // 2
        y = (img.height - side) // 2
        img = img.crop((x, y, x + side, y + side))

    sizes = [16, 32, 48, 64, 128, 256]
    png_images: dict[int, bytes] = {}

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for size in sizes:
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            png_data = buf.getvalue()
            png_images[size] = png_data
            zf.writestr(f"favicon_{size}x{size}.png", png_data)

        # Build multi-size ICO (16, 32, 48)
        ico_data = _build_ico([png_images[s] for s in [16, 32, 48]])
        zf.writestr("favicon.ico", ico_data)

        # Apple touch icon (180x180 RGB)
        touch = img.resize((180, 180), Image.Resampling.LANCZOS).convert("RGB")
        tbuf = io.BytesIO()
        touch.save(tbuf, format="PNG")
        zf.writestr("apple-touch-icon.png", tbuf.getvalue())

    return zip_buf.getvalue()


def _build_ico(png_list: list[bytes]) -> bytes:
    """Build a minimal multi-size .ico file from PNG images."""
    import struct

    n = len(png_list)
    header = struct.pack("<HHH", 0, 1, n)
    data_offset = 6 + n * 16
    dir_entries = b""
    image_data  = b""

    for png_bytes in png_list:
        img_tmp = Image.open(io.BytesIO(png_bytes))
        w = img_tmp.width  if img_tmp.width  < 256 else 0
        h = img_tmp.height if img_tmp.height < 256 else 0
        size = len(png_bytes)
        dir_entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, size, data_offset)
        data_offset += size
        image_data  += png_bytes

    return header + dir_entries + image_data


# ---------------------------------------------------------------------------
# 27. PDF Metadata Viewer / Editor
# ---------------------------------------------------------------------------

def get_pdf_metadata(data: bytes) -> dict:
    """Return PDF document metadata fields."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    meta = doc.metadata or {}
    page_count = len(doc)
    doc.close()

    return {
        "title":        meta.get("title",        ""),
        "author":       meta.get("author",       ""),
        "subject":      meta.get("subject",      ""),
        "keywords":     meta.get("keywords",     ""),
        "creator":      meta.get("creator",      ""),
        "producer":     meta.get("producer",     ""),
        "creationDate": meta.get("creationDate", ""),
        "modDate":      meta.get("modDate",      ""),
        "page_count":   page_count,
    }


def set_pdf_metadata(
    data: bytes,
    title: str    = "",
    author: str   = "",
    subject: str  = "",
    keywords: str = "",
    strip_all: bool = False,
) -> bytes:
    """Write updated metadata to a PDF, or strip all metadata."""
    import pikepdf

    src = pikepdf.open(io.BytesIO(data))

    if strip_all:
        try:
            del src.docinfo
        except Exception:
            pass
    else:
        with src.open_metadata() as meta_rw:
            if title:    meta_rw["dc:title"]       = title
            if author:   meta_rw["dc:creator"]     = [author]
            if subject:  meta_rw["dc:description"] = subject
            if keywords: meta_rw["pdf:Keywords"]   = keywords

    buf = io.BytesIO()
    src.save(buf)
    src.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 28. Blur / Pixelate Image
# ---------------------------------------------------------------------------

def blur_image(
    data: bytes,
    mode: str = "gaussian",
    strength: int = 10,
) -> tuple[bytes, str]:
    """Apply blur or pixelate effect to an image.

    Args:
        mode:     'gaussian' or 'pixelate'.
        strength: Gaussian radius (1-50) or pixel-block size (2-50).
    """
    from PIL import ImageFilter

    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "PNG").upper()

    strength = max(1, min(100, strength))

    if mode == "pixelate":
        # Downscale then upscale with nearest-neighbour
        block = max(2, strength)
        small = img.resize(
            (max(1, img.width // block), max(1, img.height // block)),
            Image.Resampling.BOX,
        )
        img = small.resize(img.size, Image.Resampling.NEAREST)
    else:
        img = img.filter(ImageFilter.GaussianBlur(radius=strength))

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "PNG"
    if save_fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/png")

    buf = io.BytesIO()
    img.save(buf, format=save_fmt, quality=93 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 29. Animated GIF Creator
# ---------------------------------------------------------------------------

def create_gif(
    images_data: list[bytes],
    delay: int = 500,
    loop: int = 0,
    max_size: int = 400,
) -> bytes:
    """Create an animated GIF from a list of images.

    Args:
        delay:    Frame delay in milliseconds.
        loop:     Number of loops (0 = infinite).
        max_size: Max width or height in pixels (GIFs are palette-limited).
    """
    if not images_data:
        raise ValueError("No images provided")

    frames: list[Image.Image] = []
    for raw in images_data:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        # Resize to fit max_size while keeping aspect ratio
        if max(img.width, img.height) > max_size:
            ratio = max_size / max(img.width, img.height)
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        # Convert to palette (P mode) with dithering for GIF
        frames.append(img.convert("P", palette=Image.Palette.ADAPTIVE, dither=1))

    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        loop=loop,
        duration=delay,
        disposal=2,
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 30. Add Text to Image
# ---------------------------------------------------------------------------

def add_text_to_image(
    data: bytes,
    text: str,
    x_pct: float = 0.5,
    y_pct: float = 0.9,
    font_size: int = 40,
    color: str = "#ffffff",
    opacity: int = 90,
    stroke_width: int = 0,
    stroke_color: str = "#000000",
) -> tuple[bytes, str]:
    """Overlay text at a proportional position on an image.

    Args:
        x_pct, y_pct: Position as fraction of image dimensions (0.0–1.0).
        stroke_width: Outline width in pixels (0 = no outline).
    """
    from PIL import ImageDraw, ImageFont

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    orig_fmt = (img.format or "PNG").upper()

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    r = int(color.lstrip("#")[0:2], 16)
    g = int(color.lstrip("#")[2:4], 16)
    b = int(color.lstrip("#")[4:6], 16)
    a = int(opacity / 100 * 255)

    sr = int(stroke_color.lstrip("#")[0:2], 16)
    sg = int(stroke_color.lstrip("#")[2:4], 16)
    sb = int(stroke_color.lstrip("#")[4:6], 16)

    # Try to load a bold font
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    x = int(img.width * x_pct - tw / 2)
    y = int(img.height * y_pct - th / 2)
    x = max(0, min(img.width - tw, x))
    y = max(0, min(img.height - th, y))

    if stroke_width > 0:
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx != 0 or dy != 0:
                    draw.text((x + dx, y + dy), text, font=font, fill=(sr, sg, sb, a))
    draw.text((x, y), text, font=font, fill=(r, g, b, a))

    out = Image.alpha_composite(img, overlay)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "PNG"
    if save_fmt == "JPEG":
        out = out.convert("RGB")
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/png")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 31. Image to Base64
# ---------------------------------------------------------------------------

def image_to_base64(data: bytes, filename: str = "image") -> dict:
    """Return base64-encoded image and data URL."""
    import base64

    img = Image.open(io.BytesIO(data))
    fmt = img.format or "PNG"
    mime_map = {
        "JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
        "GIF": "image/gif", "BMP": "image/bmp",
    }
    mime = mime_map.get(fmt.upper(), "image/png")

    b64 = base64.b64encode(data).decode()
    data_url = f"data:{mime};base64,{b64}"

    return {
        "base64":   b64,
        "data_url": data_url,
        "mime":     mime,
        "format":   fmt,
        "width":    img.width,
        "height":   img.height,
        "size":     len(data),
        "b64_size": len(b64),
    }


# ---------------------------------------------------------------------------
# 32. PDF Rotate Pages
# ---------------------------------------------------------------------------

def rotate_pdf_pages(
    data: bytes,
    angle: int = 90,
    page_indices: list[int] | None = None,
) -> bytes:
    """Rotate PDF pages by 90, 180, or 270 degrees.

    Args:
        angle:        Rotation in degrees (must be a multiple of 90).
        page_indices: 0-based page indices to rotate (None = all pages).
    """
    import fitz

    angle = (angle // 90 * 90) % 360  # snap to nearest 90

    doc = fitz.open(stream=data, filetype="pdf")
    target = set(page_indices) if page_indices is not None else set(range(len(doc)))

    for i, page in enumerate(doc):
        if i in target:
            current = page.rotation
            page.set_rotation((current + angle) % 360)

    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 33. Invert / Negate Image Colors
# ---------------------------------------------------------------------------

def invert_colors(data: bytes) -> tuple[bytes, str]:
    """Invert every pixel channel (creates a negative/negate effect)."""
    from PIL import ImageOps

    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "PNG").upper()

    if img.mode == "RGBA":
        r, g, b, a = img.split()
        inv = ImageOps.invert(Image.merge("RGB", (r, g, b)))
        out = Image.merge("RGBA", (*inv.split(), a))
    else:
        mode = img.mode if img.mode in ("RGB", "L") else "RGB"
        out = ImageOps.invert(img.convert(mode))

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "PNG"
    if save_fmt == "JPEG" and out.mode != "RGB":
        out = out.convert("RGB")
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/png")

    buf2 = io.BytesIO()
    out.save(buf2, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf2.getvalue(), mime


# ---------------------------------------------------------------------------
# 34. PDF Watermark (diagonal text on every page)
# ---------------------------------------------------------------------------

def stamp_pdf_watermark(
    data: bytes,
    text: str = "CONFIDENTIAL",
    opacity: int = 20,
    angle: int = 45,
    font_size: int = 60,
    color_hex: str = "#888888",
) -> bytes:
    """Stamp a diagonal text watermark on every page of a PDF."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    r_raw = int(color_hex.lstrip("#")[0:2], 16) / 255
    g_raw = int(color_hex.lstrip("#")[2:4], 16) / 255
    b_raw = int(color_hex.lstrip("#")[4:6], 16) / 255
    alpha = max(0.01, min(1.0, opacity / 100))

    # Blend color with white to approximate opacity (TextWriter opacity support varies)
    r = r_raw * alpha + (1 - alpha)
    g = g_raw * alpha + (1 - alpha)
    b = b_raw * alpha + (1 - alpha)

    font = fitz.Font("helv")

    for page in doc:
        pw, ph = page.rect.width, page.rect.height
        cx, cy = pw / 2, ph / 2

        # Use TextWriter with morph matrix for arbitrary rotation
        writer = fitz.TextWriter(page.rect)
        # Place text anchor at centre then rotate around it
        writer.append(fitz.Point(cx, cy), text, font=font, fontsize=font_size)

        rotate_mat = fitz.Matrix(angle)   # rotation matrix for given angle
        writer.write_text(page, color=(r, g, b), morph=(fitz.Point(cx, cy), rotate_mat))


    buf2 = io.BytesIO()
    doc.save(buf2)
    doc.close()
    return buf2.getvalue()


# ---------------------------------------------------------------------------
# 35. Extract Images from PDF
# ---------------------------------------------------------------------------

def extract_pdf_images(data: bytes, filename: str = "document.pdf") -> bytes:
    """Extract all embedded images from a PDF. Returns a ZIP of image files."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    extracted: list[tuple[str, bytes]] = []
    seq = 0

    for page_num, page in enumerate(doc, start=1):
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                base_img = doc.extract_image(xref)
                img_bytes = base_img["image"]
                ext = base_img.get("ext", "png")
                seq += 1
                extracted.append((f"{base}_p{page_num:02d}_img{seq:03d}.{ext}", img_bytes))
            except Exception:
                continue

    doc.close()

    if not extracted:
        raise ValueError("No images found in this PDF")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, img_data in extracted:
            zf.writestr(name, img_data)
    return zip_buf.getvalue()


# ---------------------------------------------------------------------------
# 36. Image Stitch (horizontal or vertical join)
# ---------------------------------------------------------------------------

def stitch_images(
    images_data: list[bytes],
    direction: str = "horizontal",
    gap: int = 0,
    gap_color: str = "#ffffff",
    align: str = "center",
) -> tuple[bytes, str]:
    """Join multiple images side-by-side or top-to-bottom."""
    if not images_data:
        raise ValueError("No images provided")

    bg = tuple(int(gap_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    imgs = [Image.open(io.BytesIO(d)).convert("RGB") for d in images_data]

    if direction == "horizontal":
        max_h   = max(im.height for im in imgs)
        total_w = sum(im.width for im in imgs) + gap * (len(imgs) - 1)
        canvas  = Image.new("RGB", (total_w, max_h), bg)
        x = 0
        for im in imgs:
            y = (max_h - im.height) // 2 if align == "center" else (max_h - im.height if align == "end" else 0)
            canvas.paste(im, (x, y))
            x += im.width + gap
    else:
        max_w   = max(im.width  for im in imgs)
        total_h = sum(im.height for im in imgs) + gap * (len(imgs) - 1)
        canvas  = Image.new("RGB", (max_w, total_h), bg)
        y = 0
        for im in imgs:
            x = (max_w - im.width) // 2 if align == "center" else (max_w - im.width if align == "end" else 0)
            canvas.paste(im, (x, y))
            y += im.height + gap

    buf2 = io.BytesIO()
    canvas.save(buf2, format="JPEG", quality=92)
    return buf2.getvalue(), "image/jpeg"


# ---------------------------------------------------------------------------
# 37. Round Corners
# ---------------------------------------------------------------------------

def round_corners(
    data: bytes,
    radius: int = 30,
    bg_color: str | None = None,
) -> tuple[bytes, str]:
    """Apply rounded corners to an image.

    Args:
        radius:   Corner radius in pixels.
        bg_color: If set, fill corners with this solid colour instead of transparency
                  (required for JPEG output since JPEG has no alpha).
    """
    from PIL import ImageDraw

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    radius = max(1, min(radius, min(img.width, img.height) // 2))

    # Rounded corner mask
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), (img.width - 1, img.height - 1)], radius=radius, fill=255)
    img.putalpha(mask)

    if bg_color:
        r = int(bg_color.lstrip("#")[0:2], 16)
        g = int(bg_color.lstrip("#")[2:4], 16)
        b = int(bg_color.lstrip("#")[4:6], 16)
        canvas = Image.new("RGBA", img.size, (r, g, b, 255))
        canvas.paste(img, mask=img)
        out = canvas.convert("RGB")
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=95)
        return buf.getvalue(), "image/jpeg"
    else:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"


# ---------------------------------------------------------------------------
# 38. Remove Solid Background Color
# ---------------------------------------------------------------------------

def remove_bg_color(
    data: bytes,
    bg_color: str = "#ffffff",
    tolerance: int = 30,
) -> bytes:
    """Replace pixels within `tolerance` of `bg_color` with transparency.

    Returns PNG with alpha channel.
    """
    import numpy as np

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    arr = np.array(img, dtype=np.int32)

    bg = [int(bg_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    diff = np.abs(arr[:, :, :3] - np.array(bg, dtype=np.int32))
    mask = np.all(diff <= tolerance, axis=2)
    arr[:, :, 3][mask] = 0
    arr = arr.clip(0, 255).astype(np.uint8)

    out = Image.fromarray(arr, "RGBA")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 39. Duotone Effect
# ---------------------------------------------------------------------------

def apply_duotone(
    data: bytes,
    shadow_color: str = "#0d1b6e",
    highlight_color: str = "#f7941d",
) -> tuple[bytes, str]:
    """Apply a duotone (two-colour gradient map) effect to an image.

    Shadows map to `shadow_color`; highlights map to `highlight_color`.
    """
    import numpy as np

    img = Image.open(io.BytesIO(data)).convert("RGB")
    orig_fmt = (img.format or "JPEG").upper()

    gray = np.array(img.convert("L"), dtype=np.float32) / 255.0  # 0.0 → 1.0

    c1 = [int(shadow_color.lstrip("#")[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    c2 = [int(highlight_color.lstrip("#")[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]

    h, w = gray.shape
    result = np.zeros((h, w, 3), dtype=np.float32)
    for ch in range(3):
        result[:, :, ch] = c1[ch] * (1 - gray) + c2[ch] * gray

    out = Image.fromarray((result * 255).clip(0, 255).astype(np.uint8), "RGB")

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=92 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 40. PDF Extract Page Range
# ---------------------------------------------------------------------------

def extract_pdf_pages(
    data: bytes,
    start_page: int,
    end_page: int,
    filename: str = "document.pdf",
) -> tuple[bytes, int]:
    """Extract a contiguous page range from a PDF (1-based page numbers).

    Returns (pdf_bytes, total_pages_in_source).
    """
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    total = len(doc)
    start = max(0, start_page - 1)
    end   = min(total - 1, end_page - 1)

    out = fitz.open()
    out.insert_pdf(doc, from_page=start, to_page=end)
    doc.close()

    buf = io.BytesIO()
    out.save(buf)
    out.close()
    return buf.getvalue(), total


# ---------------------------------------------------------------------------
# 41. Photo Caption Strip
# ---------------------------------------------------------------------------

def add_caption(
    data: bytes,
    caption: str,
    position: str = "bottom",
    bg_color: str = "#000000",
    text_color: str = "#ffffff",
    font_size: int = 24,
    padding: int = 12,
) -> tuple[bytes, str]:
    """Add a caption strip to the top or bottom of an image."""
    from PIL import ImageDraw, ImageFont

    img = Image.open(io.BytesIO(data)).convert("RGB")

    # Determine strip height
    strip_h = font_size + padding * 2

    # Create canvas
    canvas = Image.new("RGB", (img.width, img.height + strip_h), (255, 255, 255))

    if position == "top":
        canvas.paste(img, (0, strip_h))
        strip_y = 0
    else:
        canvas.paste(img, (0, 0))
        strip_y = img.height

    # Fill strip
    draw = ImageDraw.Draw(canvas)
    bg = tuple(int(bg_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    fg = tuple(int(text_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    draw.rectangle([(0, strip_y), (canvas.width, strip_y + strip_h)], fill=bg)

    # Load font
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

    # Centre text in strip
    bbox = draw.textbbox((0, 0), caption, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = max(padding, (canvas.width - tw) // 2)
    ty = strip_y + (strip_h - th) // 2

    draw.text((tx, ty), caption, font=font, fill=fg)

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=95)
    return buf.getvalue(), "image/jpeg"


# ---------------------------------------------------------------------------
# 42. Vignette Effect
# ---------------------------------------------------------------------------

def apply_vignette(
    data: bytes,
    strength: float = 0.7,
    color: str = "#000000",
) -> tuple[bytes, str]:
    """Apply a radial vignette (darkened edges) to an image.

    Args:
        strength: 0.0 = no effect, 1.0 = very strong darkening.
        color:    Vignette colour (usually black or white for glow).
    """
    import numpy as np

    img = Image.open(io.BytesIO(data)).convert("RGB")
    orig_fmt = (img.format or "JPEG").upper()
    arr = np.array(img, dtype=np.float32)
    h, w = arr.shape[:2]

    # Create radial gradient (1 at centre, 0 at edges)
    y_idx = np.linspace(-1, 1, h)
    x_idx = np.linspace(-1, 1, w)
    xx, yy = np.meshgrid(x_idx, y_idx)
    dist = np.sqrt(xx ** 2 + yy ** 2)
    dist = np.clip(dist, 0, 1)
    vignette = 1 - dist * strength

    r_v = int(color.lstrip("#")[0:2], 16) / 255
    g_v = int(color.lstrip("#")[2:4], 16) / 255
    b_v = int(color.lstrip("#")[4:6], 16) / 255

    vig = np.dstack([vignette] * 3)
    # Blend: pixel = pixel * vignette + vignette_color * (1 - vignette)
    inv_vig = 1 - vig
    result = arr * vig + np.array([r_v * 255, g_v * 255, b_v * 255]) * inv_vig
    result = result.clip(0, 255).astype(np.uint8)

    out = Image.fromarray(result, "RGB")
    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=93 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 43. Convert to Grayscale (with toning option)
# ---------------------------------------------------------------------------

def to_grayscale(
    data: bytes,
    tint_color: str | None = None,
) -> tuple[bytes, str]:
    """Convert image to grayscale, with optional warm/cool toning.

    Args:
        tint_color: If set, tint the grayscale result with this hex color.
                    Produces a duotone-like sepia or cool effect.
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    orig_fmt = (img.format or "JPEG").upper()

    gray = img.convert("L")

    if tint_color:
        import numpy as np
        r = int(tint_color.lstrip("#")[0:2], 16) / 255
        g = int(tint_color.lstrip("#")[2:4], 16) / 255
        b = int(tint_color.lstrip("#")[4:6], 16) / 255
        arr = np.array(gray, dtype=np.float32) / 255.0
        out_arr = np.dstack([arr * r, arr * g, arr * b])
        out = Image.fromarray((out_arr * 255).clip(0, 255).astype(np.uint8), "RGB")
    else:
        out = gray.convert("RGB")

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=93 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 44. Image Sharpen / Denoise
# ---------------------------------------------------------------------------

def sharpen_denoise_image(
    data: bytes,
    mode: str = "sharpen",
    strength: int = 2,
) -> tuple[bytes, str]:
    """Apply sharpening or noise reduction to an image.

    Args:
        mode:     'sharpen', 'denoise', or 'edge_enhance'.
        strength: 1-5 passes of the filter.
    """
    from PIL import ImageFilter

    img = Image.open(io.BytesIO(data)).convert("RGB")
    orig_fmt = (img.format or "JPEG").upper()
    strength = max(1, min(5, strength))

    for _ in range(strength):
        if mode == "sharpen":
            img = img.filter(ImageFilter.SHARPEN)
        elif mode == "edge_enhance":
            img = img.filter(ImageFilter.EDGE_ENHANCE)
        else:  # denoise
            img = img.filter(ImageFilter.MedianFilter(size=3))

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    buf = io.BytesIO()
    img.save(buf, format=save_fmt, quality=93 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 45. Generate Placeholder Image
# ---------------------------------------------------------------------------

def generate_placeholder(
    width: int,
    height: int,
    bg_color: str = "#cccccc",
    text_color: str = "#666666",
    label: str = "",
) -> bytes:
    """Generate a solid-colour placeholder image with optional text label.

    Returns PNG bytes.
    """
    from PIL import ImageDraw, ImageFont

    width  = max(1, min(4096, width))
    height = max(1, min(4096, height))

    bg  = tuple(int(bg_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    fg  = tuple(int(text_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    img  = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Default label: "WIDTHxHEIGHT"
    text = label or f"{width}×{height}"

    # Choose font size proportional to image
    font_size = max(12, min(width, height) // 8)
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((width - tw) // 2, (height - th) // 2), text, font=font, fill=fg)

    # Subtle border
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=fg, width=max(1, font_size // 10))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 46. Crop to Aspect Ratio
# ---------------------------------------------------------------------------

def crop_to_aspect(
    data: bytes,
    ratio_w: int = 1,
    ratio_h: int = 1,
    anchor: str = "center",
) -> tuple[bytes, str]:
    """Crop an image to a target aspect ratio without resizing.

    Args:
        ratio_w, ratio_h: Target aspect ratio (e.g. 16, 9).
        anchor: 'center', 'top', 'bottom', 'left', or 'right'.
    """
    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "JPEG").upper()
    iw, ih = img.size

    target_ratio = ratio_w / ratio_h
    current_ratio = iw / ih

    if current_ratio > target_ratio:
        # Too wide → crop sides
        new_w = int(ih * target_ratio)
        new_h = ih
        if anchor == "left":
            left = 0
        elif anchor == "right":
            left = iw - new_w
        else:
            left = (iw - new_w) // 2
        box = (left, 0, left + new_w, ih)
    else:
        # Too tall → crop top/bottom
        new_w = iw
        new_h = int(iw / target_ratio)
        if anchor == "top":
            top = 0
        elif anchor == "bottom":
            top = ih - new_h
        else:
            top = (ih - new_h) // 2
        box = (0, top, iw, top + new_h)

    cropped = img.crop(box)
    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    if save_fmt == "JPEG" and cropped.mode not in ("RGB", "L"):
        cropped = cropped.convert("RGB")
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    buf = io.BytesIO()
    cropped.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 47. Add Drop Shadow to PNG
# ---------------------------------------------------------------------------

def add_drop_shadow(
    data: bytes,
    offset_x: int = 8,
    offset_y: int = 8,
    blur: int = 12,
    shadow_color: str = "#000000",
    shadow_opacity: int = 60,
    padding: int = 20,
) -> bytes:
    """Add a drop shadow to an image. Returns PNG with alpha channel.

    Works best with PNG inputs that have transparency.
    """
    import numpy as np
    from PIL import ImageFilter, ImageDraw

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    sw = img.width + abs(offset_x) + padding * 2
    sh = img.height + abs(offset_y) + padding * 2

    r = int(shadow_color.lstrip("#")[0:2], 16)
    g = int(shadow_color.lstrip("#")[2:4], 16)
    b = int(shadow_color.lstrip("#")[4:6], 16)
    a = int(shadow_opacity / 100 * 255)

    # Create shadow layer: use alpha channel of original
    shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    # Place shadow mask at offset position
    sx = padding + max(0, offset_x)
    sy = padding + max(0, offset_y)
    alpha_mask = img.split()[3]
    shadow_layer = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    colored_shadow = Image.new("RGBA", img.size, (r, g, b, a))
    colored_shadow.putalpha(alpha_mask)
    shadow_layer.paste(colored_shadow, (sx, sy))

    # Blur the shadow
    if blur > 0:
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=blur))

    # Composite: shadow + original
    ox = padding + max(0, -offset_x)
    oy = padding + max(0, -offset_y)
    result = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    result = Image.alpha_composite(result, shadow_layer)
    result.paste(img, (ox, oy), img)

    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 48. Color Swap (replace one color with another)
# ---------------------------------------------------------------------------

def swap_color(
    data: bytes,
    source_color: str = "#ff0000",
    target_color: str = "#0000ff",
    tolerance: int = 40,
) -> bytes:
    """Replace all pixels close to `source_color` with `target_color`.

    Returns PNG to preserve exact color replacement.
    """
    import numpy as np

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    arr = np.array(img, dtype=np.int32)

    src = [int(source_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    tgt = [int(target_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]

    diff = np.abs(arr[:, :, :3] - np.array(src, dtype=np.int32))
    mask = np.all(diff <= tolerance, axis=2)

    arr[mask, 0] = tgt[0]
    arr[mask, 1] = tgt[1]
    arr[mask, 2] = tgt[2]
    arr = arr.clip(0, 255).astype(np.uint8)

    out = Image.fromarray(arr, "RGBA")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 49. PDF Compress (aggressive — remove embedded fonts subset, linearize)
# ---------------------------------------------------------------------------

def compress_pdf_aggressive(data: bytes) -> bytes:
    """More aggressive PDF compression using PyMuPDF's garbage collector.

    Removes unused objects, cleans cross-references, and linearizes.
    """
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    buf = io.BytesIO()
    # garbage=4: aggressive GC + xref rebuild; deflate=True: recompress streams
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 50. Image Tiling (repeat image as tiles on a canvas)
# ---------------------------------------------------------------------------

def tile_image(
    data: bytes,
    canvas_width: int = 1920,
    canvas_height: int = 1080,
    tile_width: int | None = None,
    tile_height: int | None = None,
) -> tuple[bytes, str]:
    """Tile an image repeatedly to fill a canvas of the given dimensions."""
    img = Image.open(io.BytesIO(data)).convert("RGB")

    tw = tile_width  or img.width
    th = tile_height or img.height
    tw = max(10, min(canvas_width,  tw))
    th = max(10, min(canvas_height, th))
    canvas_width  = max(10, min(8192, canvas_width))
    canvas_height = max(10, min(8192, canvas_height))

    tile = img.resize((tw, th), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (canvas_width, canvas_height))

    for y in range(0, canvas_height, th):
        for x in range(0, canvas_width, tw):
            canvas.paste(tile, (x, y))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


# ---------------------------------------------------------------------------
# 51. Add Image Watermark (logo overlay)
# ---------------------------------------------------------------------------

def add_image_watermark(
    base_data: bytes,
    wm_data: bytes,
    position: str = "bottom_right",
    opacity: int = 70,
    scale: int = 20,
    padding: int = 20,
) -> tuple[bytes, str]:
    """Overlay a logo/watermark image onto the base image.

    Args:
        scale:    Watermark size as % of base image's shortest dimension.
        opacity:  0–100.
        position: top_left, top_right, center, bottom_left, bottom_right.
    """
    base = Image.open(io.BytesIO(base_data)).convert("RGBA")
    wm   = Image.open(io.BytesIO(wm_data)).convert("RGBA")
    orig_fmt = (Image.open(io.BytesIO(base_data)).format or "PNG").upper()

    # Scale watermark
    max_side = min(base.width, base.height) * scale // 100
    ratio = max_side / max(wm.width, wm.height)
    wm_w = max(1, int(wm.width  * ratio))
    wm_h = max(1, int(wm.height * ratio))
    wm = wm.resize((wm_w, wm_h), Image.Resampling.LANCZOS)

    # Apply opacity
    if opacity < 100:
        r, g, b, a = wm.split()
        a = a.point(lambda x: int(x * opacity / 100))
        wm = Image.merge("RGBA", (r, g, b, a))

    positions = {
        "top_left":     (padding, padding),
        "top_right":    (base.width - wm_w - padding, padding),
        "center":       ((base.width - wm_w) // 2, (base.height - wm_h) // 2),
        "bottom_left":  (padding, base.height - wm_h - padding),
        "bottom_right": (base.width - wm_w - padding, base.height - wm_h - padding),
    }
    x, y = positions.get(position, positions["bottom_right"])
    x = max(0, min(base.width - wm_w, x))
    y = max(0, min(base.height - wm_h, y))

    out = base.copy()
    out.paste(wm, (x, y), wm)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "PNG"
    if save_fmt == "JPEG":
        out = out.convert("RGB")
    mime_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_map.get(save_fmt, "image/png")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 52. Flip / Mirror Image (convenience single-step)
# ---------------------------------------------------------------------------

def flip_image(data: bytes, direction: str = "horizontal") -> tuple[bytes, str]:
    """Flip an image horizontally or vertically (mirror)."""
    img = Image.open(io.BytesIO(data))
    orig_fmt = (img.format or "PNG").upper()

    if direction == "vertical":
        out = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    else:
        out = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP", "BMP", "GIF") else "PNG"
    if save_fmt == "JPEG" and out.mode not in ("RGB", "L"):
        out = out.convert("RGB")
    mime_map = {"JPEG":"image/jpeg","PNG":"image/png","WEBP":"image/webp","BMP":"image/bmp","GIF":"image/gif"}
    mime = mime_map.get(save_fmt, "image/png")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=95 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime


# ---------------------------------------------------------------------------
# 53. Image to Icon Set (macOS / iOS / Windows sizes)
# ---------------------------------------------------------------------------

def generate_icon_set(data: bytes, platform: str = "macos") -> bytes:
    """Generate a complete icon set for macOS, iOS, or Windows from a single image.

    Returns a ZIP of PNG files at all required sizes.
    """
    img = Image.open(io.BytesIO(data)).convert("RGBA")

    # Square-crop if needed
    if img.width != img.height:
        side = min(img.width, img.height)
        x = (img.width  - side) // 2
        y = (img.height - side) // 2
        img = img.crop((x, y, x + side, y + side))

    if platform == "ios":
        sizes = [20, 29, 40, 58, 60, 76, 80, 87, 120, 152, 167, 180, 1024]
        names = {s: f"AppIcon-{s}@1x.png" for s in sizes}
    elif platform == "windows":
        sizes = [16, 24, 32, 48, 64, 128, 256]
        names = {s: f"icon-{s}x{s}.png" for s in sizes}
    else:  # macOS
        sizes = [16, 32, 64, 128, 256, 512, 1024]
        names = {s: f"icon_{s}x{s}.png" for s in sizes}

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for size in sizes:
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            zf.writestr(names[size], buf.getvalue())

    return zip_buf.getvalue()


# ---------------------------------------------------------------------------
# 54. Posterize Effect
# ---------------------------------------------------------------------------

def apply_posterize(data: bytes, levels: int = 4) -> tuple[bytes, str]:
    """Reduce colour depth to `levels` per channel (posterize/solarize effect).

    Lower levels = more dramatic cartoon-like look.
    """
    from PIL import ImageOps

    img = Image.open(io.BytesIO(data)).convert("RGB")
    orig_fmt = (img.format or "JPEG").upper()
    levels = max(2, min(8, levels))
    out = ImageOps.posterize(img, bits=levels)

    save_fmt = orig_fmt if orig_fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
    mime_map = {"JPEG":"image/jpeg","PNG":"image/png","WEBP":"image/webp"}
    mime = mime_map.get(save_fmt, "image/jpeg")

    buf = io.BytesIO()
    out.save(buf, format=save_fmt, quality=93 if save_fmt == "JPEG" else None)
    return buf.getvalue(), mime
