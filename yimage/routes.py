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


@bp.route("/pdf-protect")
def page_pdf_protect():
    return render_template("pdf_protect.html")


@bp.route("/pdf-pages")
def page_pdf_pages():
    return render_template("pdf_pages.html")


@bp.route("/color-palette")
def page_color_palette():
    return render_template("color_palette.html")


@bp.route("/image-ocr")
def page_image_ocr():
    return render_template("image_ocr.html")


@bp.route("/pdf-stamp")
def page_pdf_stamp():
    return render_template("pdf_stamp.html")


@bp.route("/collage")
def page_collage():
    return render_template("collage.html")


@bp.route("/image-border")
def page_image_border():
    return render_template("image_border.html")


@bp.route("/optimize-image")
def page_optimize_image():
    return render_template("optimize_image.html")


@bp.route("/favicon")
def page_favicon():
    return render_template("favicon.html")


@bp.route("/pdf-metadata")
def page_pdf_metadata():
    return render_template("pdf_metadata.html")


@bp.route("/blur-image")
def page_blur_image():
    return render_template("blur_image.html")


@bp.route("/gif-creator")
def page_gif_creator():
    return render_template("gif_creator.html")


@bp.route("/add-text")
def page_add_text():
    return render_template("add_text.html")


@bp.route("/image-base64")
def page_image_base64():
    return render_template("image_base64.html")


@bp.route("/pdf-rotate")
def page_pdf_rotate():
    return render_template("pdf_rotate.html")


@bp.route("/invert-colors")
def page_invert_colors():
    return render_template("invert_colors.html")


@bp.route("/pdf-watermark")
def page_pdf_watermark():
    return render_template("pdf_watermark.html")


@bp.route("/extract-pdf-images")
def page_extract_pdf_images():
    return render_template("extract_pdf_images.html")


@bp.route("/stitch-images")
def page_stitch_images():
    return render_template("stitch_images.html")


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


# ---------------------------------------------------------------------------
# API: PDF Protect & Unlock
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-protect", methods=["POST"])
def api_pdf_protect():
    """Add password protection to a PDF."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    password = request.form.get("password", "").strip()
    if not password:
        return jsonify(error="Please provide a password"), 400
    if len(password) > 128:
        return jsonify(error="Password too long (max 128 characters)"), 400

    log.info("PDF protect: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import protect_pdf
        result = protect_pdf(data, password)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=f"protected_{base}.pdf",
        )
    except Exception as exc:
        log.exception("PDF protect failed")
        return jsonify(error=str(exc)), 500


@bp.route("/api/pdf-unlock", methods=["POST"])
def api_pdf_unlock():
    """Remove password protection from a PDF."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    password = request.form.get("password", "").strip()

    log.info("PDF unlock: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import unlock_pdf
        result = unlock_pdf(data, password)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=f"unlocked_{base}.pdf",
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    except Exception as exc:
        log.exception("PDF unlock failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: PDF Page Editor
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-pages/thumbnails", methods=["POST"])
def api_pdf_page_thumbnails():
    """Return thumbnail data for each page of a PDF."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    log.info("PDF page thumbnails: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import get_pdf_page_thumbnails
        pages = get_pdf_page_thumbnails(data)
        return jsonify({"pages": pages, "count": len(pages), "filename": filename})
    except Exception as exc:
        log.exception("PDF page thumbnails failed")
        return jsonify(error=str(exc)), 500


@bp.route("/api/pdf-pages/apply", methods=["POST"])
def api_pdf_pages_apply():
    """Apply a new page order (and deletions) to a PDF."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    import json as _json
    try:
        order_raw = request.form.get("order", "[]")
        order = _json.loads(order_raw)
        if not isinstance(order, list) or not order:
            return jsonify(error="Invalid page order"), 400
        order = [int(i) for i in order]
    except (ValueError, TypeError):
        return jsonify(error="Invalid page order format"), 400

    log.info("PDF page reorder: %s → %s", filename, order)

    try:
        from yimage.processing import apply_pdf_page_order
        result = apply_pdf_page_order(data, order)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=f"edited_{base}.pdf",
        )
    except Exception as exc:
        log.exception("PDF page reorder failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Color Palette Extractor
# ---------------------------------------------------------------------------

@bp.route("/api/color-palette", methods=["POST"])
def api_color_palette():
    """Extract the dominant colour palette from an image."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp"]
    )
    if err:
        return err

    try:
        n = int(request.form.get("n_colors", "8"))
        n = max(2, min(16, n))
    except (ValueError, TypeError):
        n = 8

    log.info("Color palette: %s (%d bytes, n=%d)", filename, len(data), n)

    try:
        from yimage.processing import extract_color_palette
        colors = extract_color_palette(data, n)
        return jsonify({"colors": colors, "count": len(colors)})
    except Exception as exc:
        log.exception("Color palette extraction failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Image OCR
# ---------------------------------------------------------------------------

@bp.route("/api/image-ocr", methods=["POST"])
def api_image_ocr():
    """Extract text from an image using Tesseract OCR."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif"]
    )
    if err:
        return err

    log.info("Image OCR: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import ocr_image
        result = ocr_image(data)
        return jsonify(result)
    except ImportError as exc:
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        log.exception("Image OCR failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: PDF Page Numbers / Stamp
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-stamp", methods=["POST"])
def api_pdf_stamp():
    """Stamp page numbers (or any text) on every PDF page."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    template  = request.form.get("template",  "Page {n} of {total}").strip() or "Page {n} of {total}"
    position  = request.form.get("position",  "bottom_center")
    color_hex = request.form.get("color",     "#444444")

    try:
        font_size = int(float(request.form.get("font_size", "11")))
        margin    = int(float(request.form.get("margin",    "20")))
    except (ValueError, TypeError):
        font_size, margin = 11, 20

    font_size = max(6, min(72, font_size))
    margin    = max(5, min(100, margin))

    log.info("PDF stamp: %s (template=%r, pos=%s, size=%d)",
             filename, template, position, font_size)

    try:
        from yimage.processing import stamp_pdf_pages
        result = stamp_pdf_pages(data, template, position, font_size, margin, color_hex)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=f"stamped_{base}.pdf",
        )
    except Exception as exc:
        log.exception("PDF stamp failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Image Collage
# ---------------------------------------------------------------------------

@bp.route("/api/collage", methods=["POST"])
def api_collage():
    """Arrange multiple images into a grid collage."""
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files uploaded"), 400

    images_data = []
    for f in files:
        if f and f.filename:
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
            if ext not in ("jpg", "jpeg", "png", "webp", "bmp", "gif"):
                return jsonify(error=f"Unsupported image type: {f.filename}"), 400
            chunk = f.read()
            if chunk:
                images_data.append(chunk)

    if not images_data:
        return jsonify(error="No valid images"), 400
    if len(images_data) > 20:
        return jsonify(error="Too many images (max 20)"), 400

    try:
        cols       = int(request.form.get("cols",        "2"))
        gap        = int(request.form.get("gap",         "10"))
        cell_h     = int(request.form.get("cell_height", "300"))
    except (ValueError, TypeError):
        cols, gap, cell_h = 2, 10, 300

    bg_color = request.form.get("bg_color", "#ffffff")
    cols     = max(1, min(6,    cols))
    gap      = max(0, min(100,  gap))
    cell_h   = max(50, min(1000, cell_h))

    log.info("Collage: %d images, cols=%d, gap=%d, cell_h=%d", len(images_data), cols, gap, cell_h)

    try:
        from yimage.processing import make_collage
        result, mime = make_collage(images_data, cols, gap, bg_color, cell_h)
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name="collage.jpg",
        )
    except Exception as exc:
        log.exception("Collage failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Image Border / Frame
# ---------------------------------------------------------------------------

@bp.route("/api/image-border", methods=["POST"])
def api_image_border():
    """Add a solid-colour border around an image."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp"]
    )
    if err:
        return err

    border_color = request.form.get("border_color", "#000000")

    try:
        border_width = int(float(request.form.get("border_width", "20")))
        radius       = int(float(request.form.get("radius",       "0")))
    except (ValueError, TypeError):
        border_width, radius = 20, 0

    border_width = max(1, min(200, border_width))
    radius       = max(0, min(500, radius))

    log.info("Image border: %s (w=%d, r=%d, color=%s)", filename, border_width, radius, border_color)

    try:
        from yimage.processing import add_border
        result, mime = add_border(data, border_width, border_color, radius)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "png")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"bordered_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Image border failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Image Optimizer
# ---------------------------------------------------------------------------

@bp.route("/api/optimize-image", methods=["POST"])
def api_optimize_image():
    """Compress an image without resizing it."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp"]
    )
    if err:
        return err

    output_format  = request.form.get("format",         "original")
    strip_metadata = request.form.get("strip_metadata", "true").lower() != "false"

    try:
        quality = int(float(request.form.get("quality", "75")))
    except (ValueError, TypeError):
        quality = 75
    quality = max(1, min(95, quality))

    log.info("Optimize image: %s (%d bytes, q=%d, fmt=%s, strip=%s)",
             filename, len(data), quality, output_format, strip_metadata)

    try:
        from yimage.processing import optimize_image
        result, mime = optimize_image(data, quality, output_format, strip_metadata)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"optimized_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Image optimize failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Favicon Generator
# ---------------------------------------------------------------------------

@bp.route("/api/favicon", methods=["POST"])
def api_favicon():
    """Generate favicon.ico and PNG sizes from an image."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp", "svg"]
    )
    if err:
        return err

    log.info("Favicon: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import generate_favicons
        result = generate_favicons(data)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/zip",
            as_attachment=True, download_name=f"{base}_favicons.zip",
        )
    except Exception as exc:
        log.exception("Favicon generation failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: PDF Metadata
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-metadata", methods=["POST"])
def api_pdf_metadata_get():
    """Return PDF document metadata as JSON."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    log.info("PDF metadata read: %s", filename)

    try:
        from yimage.processing import get_pdf_metadata
        return jsonify(get_pdf_metadata(data))
    except Exception as exc:
        log.exception("PDF metadata read failed")
        return jsonify(error=str(exc)), 500


@bp.route("/api/pdf-metadata/save", methods=["POST"])
def api_pdf_metadata_save():
    """Write updated metadata to a PDF."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    strip_all = request.form.get("strip_all", "false").lower() == "true"
    title    = request.form.get("title",    "").strip()
    author   = request.form.get("author",   "").strip()
    subject  = request.form.get("subject",  "").strip()
    keywords = request.form.get("keywords", "").strip()

    log.info("PDF metadata save: %s (strip=%s)", filename, strip_all)

    try:
        from yimage.processing import set_pdf_metadata
        result = set_pdf_metadata(data, title, author, subject, keywords, strip_all)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        dl_name = f"stripped_{base}.pdf" if strip_all else f"edited_{base}.pdf"
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=dl_name,
        )
    except Exception as exc:
        log.exception("PDF metadata save failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Blur / Pixelate Image
# ---------------------------------------------------------------------------

@bp.route("/api/blur-image", methods=["POST"])
def api_blur_image():
    """Apply blur or pixelate to an image."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp"]
    )
    if err:
        return err

    mode = request.form.get("mode", "gaussian")
    try:
        strength = int(float(request.form.get("strength", "10")))
    except (ValueError, TypeError):
        strength = 10
    strength = max(1, min(100, strength))

    log.info("Blur image: %s (mode=%s, strength=%d)", filename, mode, strength)

    try:
        from yimage.processing import blur_image
        result, mime = blur_image(data, mode, strength)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"blurred_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Blur image failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Animated GIF Creator
# ---------------------------------------------------------------------------

@bp.route("/api/gif-creator", methods=["POST"])
def api_gif_creator():
    """Create an animated GIF from multiple images."""
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files uploaded"), 400

    images_data = []
    for f in files:
        if f and f.filename:
            chunk = f.read()
            if chunk:
                images_data.append(chunk)

    if len(images_data) < 2:
        return jsonify(error="Please upload at least 2 images"), 400
    if len(images_data) > 30:
        return jsonify(error="Too many images (max 30)"), 400

    try:
        delay    = int(float(request.form.get("delay",    "500")))
        max_size = int(float(request.form.get("max_size", "400")))
        loop     = int(float(request.form.get("loop",     "0")))
    except (ValueError, TypeError):
        delay, max_size, loop = 500, 400, 0

    delay    = max(50, min(5000, delay))
    max_size = max(100, min(800,  max_size))
    loop     = max(0,  min(100,  loop))

    log.info("GIF creator: %d images, delay=%d, size=%d", len(images_data), delay, max_size)

    try:
        from yimage.processing import create_gif
        result = create_gif(images_data, delay, loop, max_size)
        return send_file(
            BytesIO(result), mimetype="image/gif",
            as_attachment=True, download_name="animated.gif",
        )
    except Exception as exc:
        log.exception("GIF creation failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Add Text to Image
# ---------------------------------------------------------------------------

@bp.route("/api/add-text", methods=["POST"])
def api_add_text():
    """Overlay custom text on an image."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp"]
    )
    if err:
        return err

    text = request.form.get("text", "").strip()
    if not text:
        return jsonify(error="Text cannot be empty"), 400

    color        = request.form.get("color",        "#ffffff")
    stroke_color = request.form.get("stroke_color", "#000000")

    try:
        x_pct        = float(request.form.get("x_pct",        "0.5"))
        y_pct        = float(request.form.get("y_pct",        "0.9"))
        font_size    = int(float(request.form.get("font_size",    "40")))
        opacity      = int(float(request.form.get("opacity",      "90")))
        stroke_width = int(float(request.form.get("stroke_width", "0")))
    except (ValueError, TypeError):
        x_pct, y_pct, font_size, opacity, stroke_width = 0.5, 0.9, 40, 90, 0

    x_pct        = max(0.0, min(1.0, x_pct))
    y_pct        = max(0.0, min(1.0, y_pct))
    font_size    = max(8,  min(250, font_size))
    opacity      = max(10, min(100, opacity))
    stroke_width = max(0,  min(10,  stroke_width))

    log.info("Add text: %s (text=%r, pos=(%.2f,%.2f))", filename, text, x_pct, y_pct)

    try:
        from yimage.processing import add_text_to_image
        result, mime = add_text_to_image(
            data, text, x_pct, y_pct, font_size, color, opacity, stroke_width, stroke_color
        )
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"text_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Add text failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Image to Base64
# ---------------------------------------------------------------------------

@bp.route("/api/image-base64", methods=["POST"])
def api_image_base64():
    """Return image as base64 data URL."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "gif", "bmp", "svg"]
    )
    if err:
        return err

    log.info("Image to base64: %s (%d bytes)", filename, len(data))

    try:
        from yimage.processing import image_to_base64
        return jsonify(image_to_base64(data, filename))
    except Exception as exc:
        log.exception("Image to base64 failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: PDF Rotate Pages
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-rotate", methods=["POST"])
def api_pdf_rotate():
    """Rotate PDF pages by 90/180/270 degrees."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err

    try:
        angle = int(float(request.form.get("angle", "90")))
    except (ValueError, TypeError):
        angle = 90

    angle = (angle // 90 * 90) % 360  # snap to 0/90/180/270

    import json as _json
    page_indices = None
    pages_raw = request.form.get("pages", "all").strip()
    if pages_raw == "all":
        page_indices = None  # all pages
    elif pages_raw == "odd":
        # Odd-numbered pages (1-based): indices 0, 2, 4, …
        import pikepdf as _pik
        src_doc = _pik.open(BytesIO(data))
        n = len(src_doc.pages)
        src_doc.close()
        page_indices = list(range(0, n, 2))   # page 1, 3, 5 … (0-based even = 1-based odd)
    elif pages_raw == "even":
        import pikepdf as _pik
        src_doc = _pik.open(BytesIO(data))
        n = len(src_doc.pages)
        src_doc.close()
        page_indices = list(range(1, n, 2))   # page 2, 4, 6 … (0-based odd = 1-based even)
    else:
        try:
            page_indices = [int(i) for i in _json.loads(pages_raw)]
        except (ValueError, TypeError):
            page_indices = None

    log.info("PDF rotate: %s (angle=%d, pages=%s)", filename, angle, page_indices or "all")

    try:
        from yimage.processing import rotate_pdf_pages
        result = rotate_pdf_pages(data, angle, page_indices)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=f"rotated_{base}.pdf",
        )
    except Exception as exc:
        log.exception("PDF rotate failed")
        return jsonify(error=str(exc)), 500

# ---------------------------------------------------------------------------
# API: Invert Colors
# ---------------------------------------------------------------------------

@bp.route("/api/invert-colors", methods=["POST"])
def api_invert_colors():
    """Invert image colors (negative effect)."""
    data, filename, err = _get_upload(
        allowed_types=["jpg", "jpeg", "png", "webp", "bmp"]
    )
    if err:
        return err
    log.info("Invert colors: %s", filename)
    try:
        from yimage.processing import invert_colors
        result, mime = invert_colors(data)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, filename.rsplit(".", 1)[-1] if "." in filename else "png")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name=f"inverted_{base}.{out_ext}",
        )
    except Exception as exc:
        log.exception("Invert colors failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: PDF Watermark
# ---------------------------------------------------------------------------

@bp.route("/api/pdf-watermark", methods=["POST"])
def api_pdf_watermark():
    """Add a diagonal text watermark to every PDF page."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err
    text = request.form.get("text", "CONFIDENTIAL").strip() or "CONFIDENTIAL"
    if len(text) > 100:
        return jsonify(error="Watermark text too long (max 100 chars)"), 400
    color_hex = request.form.get("color", "#888888")
    try:
        opacity   = int(float(request.form.get("opacity",   "20")))
        angle     = int(float(request.form.get("angle",     "45")))
        font_size = int(float(request.form.get("font_size", "60")))
    except (ValueError, TypeError):
        opacity, angle, font_size = 20, 45, 60
    opacity   = max(5,  min(100, opacity))
    angle     = max(0,  min(90,  angle))
    font_size = max(12, min(200, font_size))
    log.info("PDF watermark: %s (text=%r, opacity=%d)", filename, text, opacity)
    try:
        from yimage.processing import stamp_pdf_watermark
        result = stamp_pdf_watermark(data, text, opacity, angle, font_size, color_hex)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/pdf",
            as_attachment=True, download_name=f"watermarked_{base}.pdf",
        )
    except Exception as exc:
        log.exception("PDF watermark failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Extract Images from PDF
# ---------------------------------------------------------------------------

@bp.route("/api/extract-pdf-images", methods=["POST"])
def api_extract_pdf_images():
    """Extract all embedded images from a PDF as a ZIP."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err:
        return err
    log.info("Extract PDF images: %s (%d bytes)", filename, len(data))
    try:
        from yimage.processing import extract_pdf_images
        result = extract_pdf_images(data, filename)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(
            BytesIO(result), mimetype="application/zip",
            as_attachment=True, download_name=f"{base}_images.zip",
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    except Exception as exc:
        log.exception("Extract PDF images failed")
        return jsonify(error=str(exc)), 500


# ---------------------------------------------------------------------------
# API: Stitch Images
# ---------------------------------------------------------------------------

@bp.route("/api/stitch-images", methods=["POST"])
def api_stitch_images():
    """Join multiple images side-by-side or top-to-bottom."""
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files uploaded"), 400
    images_data = []
    for f in files:
        if f and f.filename:
            chunk = f.read()
            if chunk:
                images_data.append(chunk)
    if len(images_data) < 2:
        return jsonify(error="Please upload at least 2 images"), 400
    if len(images_data) > 20:
        return jsonify(error="Too many images (max 20)"), 400
    direction = request.form.get("direction", "horizontal")
    align     = request.form.get("align",     "center")
    gap_color = request.form.get("gap_color", "#ffffff")
    try:
        gap = int(float(request.form.get("gap", "0")))
    except (ValueError, TypeError):
        gap = 0
    gap = max(0, min(200, gap))
    log.info("Stitch images: %d files, dir=%s, gap=%d", len(images_data), direction, gap)
    try:
        from yimage.processing import stitch_images
        result, mime = stitch_images(images_data, direction, gap, gap_color, align)
        return send_file(
            BytesIO(result), mimetype=mime,
            as_attachment=True, download_name="stitched.jpg",
        )
    except Exception as exc:
        log.exception("Stitch images failed")
        return jsonify(error=str(exc)), 500

# ---------------------------------------------------------------------------
# Round 8 Page Routes
# ---------------------------------------------------------------------------

@bp.route("/round-corners")
def page_round_corners():
    return render_template("round_corners.html")

@bp.route("/remove-bg-color")
def page_remove_bg_color():
    return render_template("remove_bg_color.html")

@bp.route("/duotone")
def page_duotone():
    return render_template("duotone.html")

@bp.route("/pdf-extract-pages")
def page_pdf_extract_pages():
    return render_template("pdf_extract_pages.html")

@bp.route("/photo-caption")
def page_photo_caption():
    return render_template("photo_caption.html")


# ---------------------------------------------------------------------------
# Round 8 API Routes
# ---------------------------------------------------------------------------

@bp.route("/api/round-corners", methods=["POST"])
def api_round_corners():
    """Apply rounded corners to an image."""
    data, filename, err = _get_upload(allowed_types=["jpg","jpeg","png","webp","bmp"])
    if err: return err
    bg_color = request.form.get("bg_color") or None
    try:
        radius = int(float(request.form.get("radius", "30")))
    except (ValueError, TypeError):
        radius = 30
    radius = max(1, min(500, radius))
    log.info("Round corners: %s (r=%d)", filename, radius)
    try:
        from yimage.processing import round_corners
        result, mime = round_corners(data, radius, bg_color)
        _ext = {"image/jpeg": "jpg", "image/png": "png"}
        out_ext = _ext.get(mime, "png")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(BytesIO(result), mimetype=mime, as_attachment=True,
                         download_name=f"rounded_{base}.{out_ext}")
    except Exception as exc:
        log.exception("Round corners failed"); return jsonify(error=str(exc)), 500


@bp.route("/api/remove-bg-color", methods=["POST"])
def api_remove_bg_color():
    """Replace solid background color with transparency."""
    data, filename, err = _get_upload(allowed_types=["jpg","jpeg","png","webp","bmp"])
    if err: return err
    bg_color = request.form.get("bg_color", "#ffffff")
    try:
        tolerance = int(float(request.form.get("tolerance", "30")))
    except (ValueError, TypeError):
        tolerance = 30
    tolerance = max(0, min(128, tolerance))
    log.info("Remove BG color: %s (color=%s, tol=%d)", filename, bg_color, tolerance)
    try:
        from yimage.processing import remove_bg_color
        result = remove_bg_color(data, bg_color, tolerance)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(BytesIO(result), mimetype="image/png", as_attachment=True,
                         download_name=f"nobg_{base}.png")
    except Exception as exc:
        log.exception("Remove BG color failed"); return jsonify(error=str(exc)), 500


@bp.route("/api/duotone", methods=["POST"])
def api_duotone():
    """Apply duotone (two-color gradient map) effect."""
    data, filename, err = _get_upload(allowed_types=["jpg","jpeg","png","webp","bmp"])
    if err: return err
    shadow_color    = request.form.get("shadow_color",    "#0d1b6e")
    highlight_color = request.form.get("highlight_color", "#f7941d")
    log.info("Duotone: %s (shadow=%s, highlight=%s)", filename, shadow_color, highlight_color)
    try:
        from yimage.processing import apply_duotone
        result, mime = apply_duotone(data, shadow_color, highlight_color)
        _ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        out_ext = _ext.get(mime, "jpg")
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(BytesIO(result), mimetype=mime, as_attachment=True,
                         download_name=f"duotone_{base}.{out_ext}")
    except Exception as exc:
        log.exception("Duotone failed"); return jsonify(error=str(exc)), 500


@bp.route("/api/pdf-extract-pages", methods=["POST"])
def api_pdf_extract_pages():
    """Extract a page range from a PDF."""
    data, filename, err = _get_upload(allowed_types=["pdf"])
    if err: return err
    try:
        start = int(float(request.form.get("start_page", "1")))
        end   = int(float(request.form.get("end_page",   "1")))
    except (ValueError, TypeError):
        return jsonify(error="Invalid page numbers"), 400
    start = max(1, start); end = max(start, end)
    log.info("PDF extract pages: %s (pp. %d–%d)", filename, start, end)
    try:
        from yimage.processing import extract_pdf_pages
        result, total = extract_pdf_pages(data, start, end, filename)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(BytesIO(result), mimetype="application/pdf", as_attachment=True,
                         download_name=f"{base}_pp{start}-{end}.pdf")
    except Exception as exc:
        log.exception("PDF extract pages failed"); return jsonify(error=str(exc)), 500


@bp.route("/api/photo-caption", methods=["POST"])
def api_photo_caption():
    """Add a caption strip to an image."""
    data, filename, err = _get_upload(allowed_types=["jpg","jpeg","png","webp","bmp"])
    if err: return err
    caption    = request.form.get("caption", "").strip()
    if not caption: return jsonify(error="Caption cannot be empty"), 400
    position   = request.form.get("position",   "bottom")
    bg_color   = request.form.get("bg_color",   "#000000")
    text_color = request.form.get("text_color", "#ffffff")
    try:
        font_size = int(float(request.form.get("font_size", "24")))
        padding   = int(float(request.form.get("padding",   "12")))
    except (ValueError, TypeError):
        font_size, padding = 24, 12
    font_size = max(10, min(120, font_size)); padding = max(4, min(60, padding))
    log.info("Photo caption: %s (pos=%s, text=%r)", filename, position, caption)
    try:
        from yimage.processing import add_caption
        result, mime = add_caption(data, caption, position, bg_color, text_color, font_size, padding)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        return send_file(BytesIO(result), mimetype=mime, as_attachment=True,
                         download_name=f"captioned_{base}.jpg")
    except Exception as exc:
        log.exception("Photo caption failed"); return jsonify(error=str(exc)), 500
