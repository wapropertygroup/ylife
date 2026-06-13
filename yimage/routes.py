"""
yimage.routes
~~~~~~~~~~~~~
URL routes for the yImage image/PDF tools app.
14 tools: compress PDF, PDF↔image, crop, passport photo, PDF→text,
trim transparency, layer analysis, resize/convert, rotate/flip,
EXIF viewer/stripper, merge PDFs, split PDF, QR code.
"""
from __future__ import annotations

import logging
from io import BytesIO

from flask import (
    Blueprint, render_template, request, jsonify, send_file,
)

bp = Blueprint("image", __name__, template_folder="templates", static_folder="static")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/compress-pdf")
def page_compress_pdf():
    return render_template("compress_pdf.html")


@bp.route("/pdf-to-image")
def page_pdf_to_image():
    return render_template("pdf_to_image.html")


@bp.route("/image-to-pdf")
def page_image_to_pdf():
    return render_template("image_to_pdf.html")


@bp.route("/crop-image")
def page_crop_image():
    return render_template("crop_image.html")


@bp.route("/passport-photo")
def page_passport_photo():
    return render_template("passport_photo.html")


@bp.route("/pdf-to-text")
def page_pdf_to_text():
    return render_template("pdf_to_text.html")


@bp.route("/trim-transparency")
def page_trim_transparency():
    return render_template("trim_transparency.html")


@bp.route("/layer-analysis")
def page_layer_analysis():
    return render_template("layer_analysis.html")


@bp.route("/resize-image")
def page_resize_image():
    return render_template("resize_image.html")


@bp.route("/rotate-flip")
def page_rotate_flip():
    return render_template("rotate_flip.html")


@bp.route("/exif-data")
def page_exif_data():
    return render_template("exif_data.html")


@bp.route("/merge-pdf")
def page_merge_pdf():
    return render_template("merge_pdf.html")


@bp.route("/split-pdf")
def page_split_pdf():
    return render_template("split_pdf.html")


@bp.route("/qr-code")
def page_qr_code():
    return render_template("qr_code.html")


@bp.route("/watermark")
def page_watermark():
    return render_template("watermark.html")


@bp.route("/image-filters")
def page_image_filters():
    return render_template("image_filters.html")


# ---------------------------------------------------------------------------
# Helper: validate upload
# ---------------------------------------------------------------------------

def _get_upload(name: str = "file", allowed_types: list[str] | None = None) -> tuple:
    """Validate and read an uploaded file. Returns (data_bytes, filename, error_response)."""
    f = request.files.get(name)
    if not f or not f.filename:
        return None, None, (jsonify(error="No file uploaded"), 400)

    # Sanitize filename: keep only safe ASCII chars
    import re as _re
    safe_name = _re.sub(r'[^\w.\-]', '_', f.filename)
    if not safe_name or safe_name.startswith('.'):
        safe_name = "upload" + ("." + f.filename.rsplit(".", 1)[-1] if "." in f.filename else "")

    if allowed_types:
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in allowed_types:
            return None, None, (jsonify(error=f"Unsupported file type '.{ext}'. Allowed: {', '.join(allowed_types)}"), 400)

    data = f.read()
    if not data:
        return None, None, (jsonify(error="Uploaded file is empty"), 400)

    return data, safe_name, None


# ---------------------------------------------------------------------------
# API: Compress PDF
# ---------------------------------------------------------------------------

@bp.route("/api/compress-pdf", methods=["POST"])
def api_compress_pdf():
    """Compress a PDF file."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    quality = request.form.get("quality", "medium")
    log.info("Compress PDF: %s (%d bytes, quality=%s)", filename, len(data), quality)

    try:
        from yimage.processing import compress_pdf
        result = compress_pdf(data, quality)
        log.info("Compressed: %d → %d bytes (%.0f%% reduction)",
                 len(data), len(result), (1 - len(result) / len(data)) * 100)
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=f"compressed_{filename}",
        )
    except Exception as exc:
        log.exception("Compress PDF failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: PDF to Image
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-to-image", methods=["POST"])
def api_pdf_to_image():
    """Convert PDF pages to images."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    fmt = request.form.get("format", "jpeg").lower()
    dpi = int(request.form.get("dpi", "150"))
    dpi = max(72, min(600, dpi))  # clamp

    log.info("PDF to Image: %s (%d bytes, fmt=%s, dpi=%d)", filename, len(data), fmt, dpi)

    try:
        from yimage.processing import pdf_to_images
        result, content_type, dl_name = pdf_to_images(data, fmt, dpi, filename)
        return send_file(
            BytesIO(result), mimetype=content_type,
            as_attachment=True, download_name=dl_name,
        )
    except Exception as exc:
        log.exception("PDF to Image failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Image to PDF
# ---------------------------------------------------------------------------

@bp.route("/api/image-to-pdf", methods=["POST"])
def api_image_to_pdf():
    """Merge multiple images into a single PDF."""
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files uploaded"), 400

    images_data = []
    for f in files:
        if f and f.filename:
            images_data.append(f.read())

    if not images_data:
        return jsonify(error="No valid image files"), 400

    log.info("Image to PDF: %d images", len(images_data))

    try:
        from yimage.processing import images_to_pdf
        result = images_to_pdf(images_data)
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name="merged.pdf",
        )
    except Exception as exc:
        log.exception("Image to PDF failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Crop Image
# ---------------------------------------------------------------------------

@bp.route("/api/crop-image", methods=["POST"])
def api_crop_image():
    """Crop an image using coordinates from the canvas preview."""
    data, filename, err = _get_upload(allowed_types=["jpg", "jpeg", "png", "webp", "bmp", "gif"])
    if err:
        return err

    try:
        x = float(request.form.get("x", 0))
        y = float(request.form.get("y", 0))
        w = float(request.form.get("w", 0))
        h = float(request.form.get("h", 0))
        canvas_w = float(request.form.get("canvas_w", 0))
        canvas_h = float(request.form.get("canvas_h", 0))
    except (ValueError, TypeError):
        return jsonify(error="Invalid crop coordinates"), 400

    if w <= 0 or h <= 0 or canvas_w <= 0 or canvas_h <= 0:
        return jsonify(error="Invalid crop dimensions"), 400

    log.info("Crop Image: %s crop=(%s,%s,%s,%s) canvas=(%s,%s)", filename, x, y, w, h, canvas_w, canvas_h)

    try:
        from yimage.processing import crop_image
        result, mime = crop_image(data, x, y, w, h, canvas_w, canvas_h)
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "png"
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"cropped_{filename}",
        )
    except Exception as exc:
        log.exception("Crop Image failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Passport Photo
# ---------------------------------------------------------------------------

@bp.route("/api/passport-photo/detect", methods=["POST"])
def api_passport_detect():
    """Detect face in an image, return bounding box."""
    data, filename, err = _get_upload(allowed_types=["jpg", "jpeg", "png", "webp"])
    if err:
        return err

    log.info("Passport detect: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import detect_face
        result = detect_face(data)
        if not result:
            return jsonify(error="No face detected in the image. Please upload a clear portrait photo."), 422
        return jsonify(result)
    except Exception as exc:
        log.exception("Face detection failed")
        return jsonify(error=str(exc)), 500


@bp.route("/api/passport-photo", methods=["POST"])
def api_passport_photo():
    """Generate a passport photo with optional manual crop and print sheet layout."""
    data, filename, err = _get_upload(allowed_types=["jpg", "jpeg", "png", "webp"])
    if err:
        return err

    size = request.form.get("size", "us_2x2")
    bg_color = request.form.get("bg_color", "#ffffff")
    print_layout = request.form.get("print_layout", "single")

    # Manual crop coordinates (fractions 0-1)
    crop_rect = None
    try:
        cx = float(request.form.get("crop_x", ""))
        cy = float(request.form.get("crop_y", ""))
        cw = float(request.form.get("crop_w", ""))
        ch = float(request.form.get("crop_h", ""))
        if cw > 0 and ch > 0:
            crop_rect = (cx, cy, cw, ch)
    except (ValueError, TypeError):
        pass

    log.info("Passport photo: %s (size=%s, bg=%s, layout=%s, crop=%s)",
             filename, size, bg_color, print_layout, crop_rect)

    try:
        from yimage.processing import make_passport_photo
        result = make_passport_photo(data, size, bg_color, crop_rect=crop_rect,
                                     print_layout=print_layout)
        return send_file(
            BytesIO(result), mimetype="image/jpeg",
            as_attachment=True, download_name=f"passport_{size}.jpg",
        )
    except Exception as exc:
        log.exception("Passport photo failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: PDF to Text
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-to-text", methods=["POST"])
def api_pdf_to_text():
    """Extract text from a PDF."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    log.info("PDF to Text: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import pdf_to_text
        result = pdf_to_text(data)
        return jsonify(result)
    except Exception as exc:
        log.exception("PDF to Text failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Trim Transparency
# ---------------------------------------------------------------------------

@bp.route("/api/trim-transparency", methods=["POST"])
def api_trim_transparency():
    """Trim transparent borders from a PNG image."""
    data, filename, err = _get_upload(allowed_types=["png"])
    if err:
        return err

    bg_color = request.form.get("bg_color")  # optional hex color

    log.info("Trim transparency: %s (%d bytes, bg=%s)", filename, len(data), bg_color)

    try:
        from yimage.processing import trim_transparency
        result = trim_transparency(data, bg_color)
        return send_file(
            BytesIO(result), mimetype="image/png",
            as_attachment=True, download_name=f"trimmed_{filename}",
        )
    except Exception as exc:
        log.exception("Trim transparency failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Layer Analysis
# ---------------------------------------------------------------------------

@bp.route("/api/layer-analysis", methods=["POST"])
def api_layer_analysis():
    """Separate image into RGB channels and/or color clusters."""
    data, filename, err = _get_upload(allowed_types=["jpg", "jpeg", "png", "webp", "bmp"])
    if err:
        return err

    mode = request.form.get("mode", "both")  # channels, colors, both

    log.info("Layer analysis: %s (%d bytes, mode=%s)", filename, len(data), mode)

    try:
        from yimage.processing import analyze_layers
        result = analyze_layers(data, mode)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/zip",
            as_attachment=True, download_name=f"layers_{base}.zip",
        )
    except Exception as exc:
        log.exception("Layer analysis failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Resize & Convert Image
# ---------------------------------------------------------------------------

@bp.route("/api/resize-image", methods=["POST"])
def api_resize_image():
    """Resize an image and/or convert its format."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp", "gif"]
    )
    if err:
        return err

    fmt = request.form.get("format", "original").lower()
    keep_aspect = request.form.get("keep_aspect", "true").lower() != "false"

    try:
        width_str = request.form.get("width", "").strip()
        height_str = request.form.get("height", "").strip()
        width = int(width_str) if width_str else None
        height = int(height_str) if height_str else None
    except ValueError:
        return jsonify(error="Invalid width or height"), 400

    if not width and not height and fmt == "original":
        return jsonify(error="Please specify a new size or a different output format"), 400

    log.info("Resize image: %s (w=%s, h=%s, fmt=%s, aspect=%s)",
             filename, width, height, fmt, keep_aspect)

    try:
        from yimage.processing import resize_convert_image
        result, mime = resize_convert_image(data, width, height, fmt, keep_aspect)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                "image/bmp": "bmp", "image/gif": "gif"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"resized_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Resize image failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Rotate & Flip Image
# ---------------------------------------------------------------------------

@bp.route("/api/rotate-flip", methods=["POST"])
def api_rotate_flip():
    """Rotate and/or flip an image."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp", "gif"]
    )
    if err:
        return err

    try:
        angle = int(request.form.get("angle", "0")) % 360
    except ValueError:
        angle = 0

    flip_h = request.form.get("flip_h", "false").lower() == "true"
    flip_v = request.form.get("flip_v", "false").lower() == "true"

    log.info("Rotate/flip: %s (angle=%d, flip_h=%s, flip_v=%s)",
             filename, angle, flip_h, flip_v)

    try:
        from yimage.processing import rotate_flip_image
        result, mime = rotate_flip_image(data, angle, flip_h, flip_v)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                "image/bmp": "bmp", "image/gif": "gif"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "png")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"rotated_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Rotate/flip failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: EXIF Data — Extract & Strip
# ---------------------------------------------------------------------------

@bp.route("/api/exif-data", methods=["POST"])
def api_exif_data():
    """Extract EXIF metadata from an image, return JSON."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "tiff", "tif"]
    )
    if err:
        return err

    log.info("EXIF extract: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import extract_exif
        return jsonify(extract_exif(data))
    except Exception as exc:
        log.exception("EXIF extraction failed")
        return jsonify(error=str(exc)), 500


@bp.route("/api/strip-exif", methods=["POST"])
def api_strip_exif():
    """Remove all EXIF/metadata from an image, return clean file."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp"]
    )
    if err:
        return err

    log.info("EXIF strip: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import strip_exif
        result, mime = strip_exif(data)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"clean_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("EXIF strip failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Merge PDFs
# ---------------------------------------------------------------------------

@bp.route("/api/merge-pdf", methods=["POST"])
def api_merge_pdf():
    """Merge multiple PDFs into one."""
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files uploaded"), 400

    pdfs_data = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext != "pdf":
            return jsonify(error=f"Only PDF files are allowed (got: {f.filename})"), 400
        chunk = f.read()
        if chunk:
            pdfs_data.append(chunk)

    if len(pdfs_data) < 2:
        return jsonify(error="Please upload at least 2 PDF files"), 400

    log.info("Merge PDFs: %d files", len(pdfs_data))

    try:
        from yimage.processing import merge_pdfs
        result = merge_pdfs(pdfs_data)
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name="merged.pdf",
        )
    except Exception as exc:
        log.exception("PDF merge failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Split PDF
# ---------------------------------------------------------------------------

@bp.route("/api/split-pdf", methods=["POST"])
def api_split_pdf():
    """Split a PDF into one PDF per page, returned as a ZIP."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    log.info("Split PDF: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import split_pdf
        result = split_pdf(data, filename)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/zip",
            as_attachment=True, download_name=f"{base}_pages.zip",
        )
    except Exception as exc:
        log.exception("PDF split failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: QR Code
# ---------------------------------------------------------------------------

@bp.route("/api/qr-code", methods=["POST"])
def api_qr_code():
    """Generate a QR code PNG."""
    text = request.form.get("text", "").strip()
    if not text:
        return jsonify(error="Please enter text or a URL"), 400
    if len(text) > 4000:
        return jsonify(error="Text is too long (max 4 000 characters)"), 400

    size             = request.form.get("size", "medium")
    error_correction = request.form.get("error_correction", "M").upper()
    fg_color         = request.form.get("fg_color", "#000000")
    bg_color         = request.form.get("bg_color", "#ffffff")

    log.info("QR code: %d chars, size=%s, ec=%s", len(text), size, error_correction)

    try:
        from yimage.processing import generate_qr
        result = generate_qr(text, size, error_correction, fg_color, bg_color)
        return send_file(
            BytesIO(result), mimetype="image/png",
            as_attachment=False, download_name="qrcode.png",
        )
    except Exception as exc:
        log.exception("QR code generation failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Watermark
# ---------------------------------------------------------------------------

@bp.route("/api/watermark", methods=["POST"])
def api_watermark():
    """Add a text watermark to an image."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp"]
    )
    if err:
        return err

    text     = request.form.get("text", "").strip()
    position = request.form.get("position", "bottom_right")
    color    = request.form.get("color", "#ffffff")

    try:
        opacity   = int(float(request.form.get("opacity",   "50")))
        font_size = int(float(request.form.get("font_size", "36")))
    except (ValueError, TypeError):
        opacity, font_size = 50, 36

    if not text:
        return jsonify(error="Watermark text cannot be empty"), 400
    if len(text) > 200:
        return jsonify(error="Watermark text too long (max 200 characters)"), 400

    opacity   = max(5, min(100, opacity))
    font_size = max(10, min(200, font_size))

    log.info("Watermark: %s (text=%r, pos=%s, size=%d, opacity=%d)",
             filename, text, position, font_size, opacity)

    try:
        from yimage.processing import add_watermark
        result, mime = add_watermark(data, text, position, opacity, font_size, color)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"watermarked_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Watermark failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Image Filters
# ---------------------------------------------------------------------------

@bp.route("/api/image-filters", methods=["POST"])
def api_image_filters():
    """Apply brightness/contrast/saturation/sharpness and preset filters."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp"]
    )
    if err:
        return err

    preset = request.form.get("preset", "none")

    try:
        brightness = float(request.form.get("brightness", "1.0"))
        contrast   = float(request.form.get("contrast",   "1.0"))
        saturation = float(request.form.get("saturation", "1.0"))
        sharpness  = float(request.form.get("sharpness",  "1.0"))
    except (ValueError, TypeError):
        brightness = contrast = saturation = sharpness = 1.0

    # Clamp to sane range
    brightness = max(0.1, min(3.0, brightness))
    contrast   = max(0.1, min(3.0, contrast))
    saturation = max(0.0, min(3.0, saturation))
    sharpness  = max(0.0, min(3.0, sharpness))

    log.info("Image filters: %s (preset=%s, b=%.2f, c=%.2f, s=%.2f, sh=%.2f)",
             filename, preset, brightness, contrast, saturation, sharpness)

    try:
        from yimage.processing import apply_filters
        result, mime = apply_filters(data, brightness, contrast, saturation, sharpness, preset)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"filtered_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Image filters failed")
        return jsonify(error=str(exc)), 500
