"""
yimage.processing
~~~~~~~~~~~~~~~~~
Server-side image and PDF processing pipelines.
All functions take bytes in, return bytes out — no Flask dependency.

Tools:
  1. compress_pdf       — shrink PDF by recompressing images
  2. pdf_to_images      — render PDF pages to JPEG/PNG
  3. images_to_pdf      — merge images into a single PDF
  4. crop_image         — crop image using canvas coordinates
  5. detect_face        — face detection for passport photos
  6. make_passport_photo — crop + resize + background for passport
  7. pdf_to_text        — extract text from PDF (+ OCR fallback)
  8. trim_transparency  — remove transparent borders from PNG
  9. analyze_layers     — separate RGB channels + K-means color clusters
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
