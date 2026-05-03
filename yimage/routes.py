"""
yimage.routes
~~~~~~~~~~~~~
URL routes for the yImage image/PDF tools app.
8 tools: compress PDF, PDF↔image, crop, passport photo, PDF→text,
trim transparency, layer analysis.
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


# ---------------------------------------------------------------------------
# Helper: validate upload
# ---------------------------------------------------------------------------

def _get_upload(name: str = "file", allowed_types: list[str] | None = None) -> tuple:
    """Validate and read an uploaded file. Returns (data_bytes, filename, error_response)."""
    f = request.files.get(name)
    if not f or not f.filename:
        return None, None, (jsonify(error="No file uploaded"), 400)

    if allowed_types:
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in allowed_types:
            return None, None, (jsonify(error=f"Unsupported file type '.{ext}'. Allowed: {', '.join(allowed_types)}"), 400)

    data = f.read()
    if not data:
        return None, None, (jsonify(error="Uploaded file is empty"), 400)

    return data, f.filename, None


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
    """Generate a passport photo."""
    data, filename, err = _get_upload(allowed_types=["jpg", "jpeg", "png", "webp"])
    if err:
        return err

    size = request.form.get("size", "us_2x2")
    bg_color = request.form.get("bg_color", "#ffffff")

    log.info("Passport photo: %s (size=%s, bg=%s)", filename, size, bg_color)

    try:
        from yimage.processing import make_passport_photo
        result = make_passport_photo(data, size, bg_color)
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
