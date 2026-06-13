/**
 * yImage i18n — English + Simplified Chinese translations
 */
const I18n = (() => {
  const LANGS = {
    en: {
      // Nav
      "nav.tools":    "Tools",
      "nav.pdf":      "PDF",
      "nav.image":    "Image",
      "nav.passport": "Passport",
      "nav.qr":       "QR Code",
      "nav.all_tools":"All Tools",

      // Index
      "hero.title":    "Image & PDF Tools",
      "hero.subtitle": "Free browser-based tools for images and PDFs. No uploads to third parties — all processing happens on our server.",

      // Tool names (used in nav + index cards)
      "tool.compress_pdf":      "Compress PDF",
      "tool.pdf_to_image":      "PDF to Image",
      "tool.image_to_pdf":      "Image to PDF",
      "tool.crop_image":        "Crop Image",
      "tool.passport_photo":    "Passport Photo",
      "tool.pdf_to_text":       "PDF to Text",
      "tool.trim_transparency": "Trim Transparency",
      "tool.layer_analysis":    "Layer Analysis",
      "tool.resize_image":      "Resize & Convert",
      "tool.rotate_flip":       "Rotate & Flip",
      "tool.exif_data":         "EXIF Data",
      "tool.merge_pdf":         "Merge PDFs",
      "tool.split_pdf":         "Split PDF",
      "tool.qr_code":           "QR Code",
      "tool.watermark":         "Add Watermark",
      "tool.filters":           "Image Filters",
      "tool.pdf_protect":       "PDF Protect & Unlock",
      "tool.pdf_pages":         "PDF Page Editor",
      "tool.color_palette":     "Color Palette",
      "tool.image_ocr":         "Image OCR",
      "tool.pdf_stamp":         "PDF Page Numbers",
      "tool.collage":           "Image Collage",
      "tool.image_border":      "Image Border",
      "tool.optimize_image":    "Image Optimizer",
      "tool.favicon":           "Favicon Generator",
      "tool.pdf_metadata":      "PDF Metadata",

      // Image Optimizer
      "optimize.title":      "⚡ Image Optimizer",
      "optimize.desc":       "Reduce image file size without changing dimensions. Great for web images.",
      "optimize.quality":    "Quality",
      "optimize.smaller":    "Smaller file",
      "optimize.better":     "Better quality",
      "optimize.format":     "Output Format",
      "optimize.strip_meta": "Strip metadata (smaller file)",
      "optimize.button":     "Optimize & Download",
      "optimize.done":       "✓ Optimized image downloaded!",

      // Favicon Generator
      "favicon.title":        "⭐ Favicon Generator",
      "favicon.desc":         "Generate favicon.ico and PNG icons at all standard sizes from any image.",
      "favicon.hint":         "Square images work best. Non-square images are auto-cropped from the centre.",
      "favicon.output_label": "Files included in the ZIP:",
      "favicon.button":       "Generate Favicons (ZIP)",
      "favicon.done":         "✓ Favicon ZIP downloaded!",

      // PDF Metadata
      "pdfmeta.title":          "📋 PDF Metadata Editor",
      "pdfmeta.desc":           "View and edit the title, author, subject and keywords stored inside a PDF.",
      "pdfmeta.read_btn":       "Read Metadata",
      "pdfmeta.pages":          "Pages",
      "pdfmeta.creator":        "Creator App",
      "pdfmeta.producer":       "Producer",
      "pdfmeta.created":        "Created",
      "pdfmeta.title_field":    "Title",
      "pdfmeta.author_field":   "Author",
      "pdfmeta.subject_field":  "Subject",
      "pdfmeta.keywords_field": "Keywords",
      "pdfmeta.save_btn":       "Save & Download",
      "pdfmeta.strip_btn":      "Strip All Metadata",

      // Round 6 tool names
      "tool.blur_image":   "Blur / Pixelate",
      "tool.gif_creator":  "GIF Creator",
      "tool.add_text":     "Add Text",
      "tool.image_base64": "Image → Base64",
      "tool.pdf_rotate":   "PDF Rotate",

      // Blur / Pixelate
      "blur.title":    "🫥 Blur / Pixelate Image",
      "blur.desc":     "Apply Gaussian blur or pixelate effect. Great for hiding faces, license plates, or sensitive information.",
      "blur.mode":     "Effect",
      "blur.gaussian": "Gaussian Blur",
      "blur.pixelate": "Pixelate",
      "blur.strength": "Strength",
      "blur.button":   "Apply Effect & Download",
      "blur.done":     "✓ Effect applied and downloaded!",

      // GIF Creator
      "gif.title":         "🎞️ Animated GIF Creator",
      "gif.desc":          "Turn multiple images into an animated GIF. Control frame speed and size.",
      "gif.hint":          "2–30 frames · Order determines animation sequence",
      "gif.delay":         "Frame Delay",
      "gif.max_size":      "Max Size",
      "gif.loop":          "Loop",
      "gif.loop_infinite": "Infinite",
      "gif.loop_once":     "Once",
      "gif.loop_3":        "3 Times",
      "gif.button":        "Create GIF & Download",
      "gif.need_two":      "Add at least 2 frames to create a GIF.",

      // Add Text
      "addtext.title":      "✏️ Add Text to Image",
      "addtext.desc":       "Overlay custom text on your image. Set position, font size, color, opacity, and stroke outline.",
      "addtext.text":       "Text",
      "addtext.click_hint": "Click on the image to set text position",
      "addtext.stroke":     "Stroke",
      "addtext.position":   "Position",
      "addtext.button":     "Apply Text & Download",
      "addtext.done":       "✓ Text added and downloaded!",

      // Image → Base64
      "b64.title":    "🔢 Image → Base64",
      "b64.desc":     "Convert an image to a Base64 data URL for embedding in HTML, CSS, or JavaScript.",
      "b64.new_image":"New image",
      "b64.data_url": "Data URL",
      "b64.raw_b64":  "Raw Base64",
      "b64.img_tag":  "HTML <img>",
      "b64.css_bg":   "CSS bg",

      // PDF Rotate
      "pdfrotate.title":       "🔃 PDF Rotate Pages",
      "pdfrotate.desc":        "Rotate all pages or specific pages in a PDF by 90°, 180°, or 270°.",
      "pdfrotate.angle":       "Rotation Angle",
      "pdfrotate.which_pages": "Apply to",
      "pdfrotate.all_pages":   "All pages",
      "pdfrotate.odd_pages":   "Odd pages",
      "pdfrotate.even_pages":  "Even pages",
      "pdfrotate.button":      "Rotate & Download",
      "pdfrotate.done":        "✓ Rotated PDF downloaded!",

      // Round 7 tool names
      "tool.invert_colors":      "Invert Colors",
      "tool.pdf_watermark":      "PDF Watermark",
      "tool.extract_pdf_images": "Extract PDF Images",
      "tool.stitch_images":      "Stitch Images",

      // Invert Colors
      "invert.title":    "🔲 Invert Colors",
      "invert.desc":     "Create a photographic negative by inverting every pixel color channel.",
      "invert.original": "Original",
      "invert.preview":  "Inverted (preview)",
      "invert.button":   "Invert & Download",
      "invert.done":     "✓ Inverted image downloaded!",

      // PDF Watermark
      "pdfwm.title":  "💧 PDF Watermark",
      "pdfwm.desc":   "Stamp a diagonal semi-transparent text watermark on every page of a PDF.",
      "pdfwm.text":   "Watermark Text",
      "pdfwm.angle":  "Angle",
      "pdfwm.button": "Add Watermark & Download",
      "pdfwm.done":   "✓ Watermarked PDF downloaded!",

      // Extract PDF Images
      "extractimg.title":  "🗃️ Extract Images from PDF",
      "extractimg.desc":   "Pull out all embedded images from a PDF and download them as a ZIP archive.",
      "extractimg.info":   "Images are extracted at their original resolution and saved as JPEG or PNG.",
      "extractimg.button": "Extract Images (ZIP)",
      "extractimg.done":   "✓ Images ZIP downloaded!",

      // Stitch Images
      "stitch.title":      "⛓️ Stitch Images",
      "stitch.desc":       "Join multiple images side-by-side or top-to-bottom to create a panorama or comparison strip.",
      "stitch.hint":       "2–20 images · Order determines stitch sequence",
      "stitch.direction":  "Direction",
      "stitch.horizontal": "Horizontal",
      "stitch.vertical":   "Vertical",
      "stitch.align":      "Align",
      "stitch.start":      "Start",
      "stitch.center":     "Center",
      "stitch.end":        "End",
      "stitch.button":     "Stitch & Download",
      "stitch.need_two":   "Add at least 2 images to stitch.",

      // Round 8 tool names (EN)
      "tool.round_corners":     "Round Corners",
      "tool.remove_bg_color":   "Remove BG Color",
      "tool.duotone":           "Duotone Effect",
      "tool.pdf_extract_pages": "PDF Extract Pages",
      "tool.photo_caption":     "Photo Caption",

      // Round Corners
      "roundcorners.title":   "⬛ Round Corners",
      "roundcorners.desc":    "Add rounded corners to any image. PNG (transparent) or JPEG (solid background).",
      "roundcorners.radius":  "Corner Radius",
      "roundcorners.less":    "Less",
      "roundcorners.more":    "More rounded",
      "roundcorners.use_bg":  "Output as JPEG with background color",
      "roundcorners.preview": "Live preview (CSS approximation)",
      "roundcorners.button":  "Round Corners & Download",
      "roundcorners.done":    "✓ Image downloaded with rounded corners!",

      // Remove BG Color
      "removebg.title":     "🪄 Remove Background Color",
      "removebg.desc":      "Replace a solid background color with transparency. Great for logos and icons.",
      "removebg.bg_color":  "Background Color to Remove",
      "removebg.tolerance": "Tolerance",
      "removebg.exact":     "Exact match",
      "removebg.fuzzy":     "Broader match",
      "removebg.button":    "Remove Background & Download PNG",
      "removebg.done":      "✓ Transparent PNG downloaded!",

      // Duotone
      "duotone.title":     "🎭 Duotone Effect",
      "duotone.desc":      "Apply a two-color gradient map — shadows get one color, highlights get another.",
      "duotone.shadow":    "Shadow Color",
      "duotone.highlight": "Highlight Color",
      "duotone.presets":   "Presets",
      "duotone.button":    "Apply Duotone & Download",
      "duotone.done":      "✓ Duotone image downloaded!",

      // PDF Extract Pages
      "extractpages.title":  "📄 PDF Extract Pages",
      "extractpages.desc":   "Extract a specific range of pages from a PDF and save as a new document.",
      "extractpages.from":   "From page",
      "extractpages.to":     "To page",
      "extractpages.hint":   "1-based page numbers. Same start and end = single page.",
      "extractpages.button": "Extract Pages & Download",
      "extractpages.done":   "✓ Extracted PDF downloaded!",

      // Photo Caption
      "caption.title":      "💬 Photo Caption",
      "caption.desc":       "Add a caption bar to the top or bottom of your photo with custom colors and font size.",
      "caption.text":       "Caption Text",
      "caption.position":   "Position",
      "caption.top":        "↑ Top",
      "caption.bottom":     "↓ Bottom",
      "caption.bg":         "Background",
      "caption.text_color": "Text Color",
      "caption.button":     "Add Caption & Download",
      "caption.done":       "✓ Captioned image downloaded!",

      // Round 9 tool names (EN)
      "tool.vignette":        "Vignette",
      "tool.grayscale":       "Grayscale",
      "tool.sharpen_denoise": "Sharpen / Denoise",
      "tool.placeholder":     "Placeholder Image",

      // Vignette
      "vignette.title":    "🌑 Vignette Effect",
      "vignette.desc":     "Darken (or lighten) the edges of a photo to draw the eye toward the centre.",
      "vignette.strength": "Strength",
      "vignette.color":    "Edge Color",
      "vignette.button":   "Apply Vignette & Download",
      "vignette.done":     "✓ Vignette image downloaded!",

      // Grayscale
      "gray.title":        "⬜ Grayscale",
      "gray.desc":         "Convert a photo to grayscale. Optionally add a sepia, cool, or custom color tint.",
      "gray.add_tint":     "Add color tint",
      "gray.tint_presets": "Tint Presets",
      "gray.custom_tint":  "Custom:",
      "gray.button":       "Convert & Download",
      "gray.done":         "✓ Grayscale image downloaded!",

      // Sharpen / Denoise
      "sharpen.title":        "🔪 Sharpen / Denoise",
      "sharpen.desc":         "Sharpen soft images, enhance edges, or reduce noise. Up to 5 passes.",
      "sharpen.mode":         "Mode",
      "sharpen.sharpen":      "Sharpen",
      "sharpen.edge":         "Edge Enhance",
      "sharpen.denoise":      "Denoise",
      "sharpen.passes":       "Passes",
      "sharpen.mild":         "Mild",
      "sharpen.strong":       "Strong",
      "sharpen.button":       "Apply & Download",
      "sharpen.done":         "✓ Image processed and downloaded!",

      // Placeholder
      "placeholder.title":      "🟪 Placeholder Image",
      "placeholder.desc":       "Generate a solid-colour placeholder image at any size. Great for UI mockups.",
      "placeholder.size":       "Dimensions",
      "placeholder.bg_color":   "Background",
      "placeholder.text_color": "Text Color",
      "placeholder.label":      "Custom Label",
      "placeholder.preview":    "Preview",
      "placeholder.button":     "Generate Placeholder",

      // Round 10 tool names (EN)
      "tool.crop_aspect":      "Crop to Aspect",
      "tool.drop_shadow":      "Drop Shadow",
      "tool.color_swap":       "Color Swap",
      "tool.pdf_compress_pro": "PDF Compress Pro",
      "tool.tile_image":       "Tile Image",

      // Crop to Aspect Ratio
      "aspect.title":    "📐 Crop to Aspect Ratio",
      "aspect.desc":     "Crop an image to a target aspect ratio without resizing.",
      "aspect.ratio":    "Aspect Ratio",
      "aspect.anchor":   "Crop Anchor",
      "aspect.original": "Original",
      "aspect.result":   "Result",
      "aspect.button":   "Crop & Download",
      "aspect.done":     "✓ Cropped image downloaded!",

      // Drop Shadow
      "shadow.title":    "🌤 Drop Shadow",
      "shadow.desc":     "Add a soft drop shadow behind an image. Works best with PNG with transparency.",
      "shadow.offset_x": "Offset X",
      "shadow.offset_y": "Offset Y",
      "shadow.blur":     "Blur",
      "shadow.opacity":  "Opacity",
      "shadow.color":    "Color",
      "shadow.button":   "Add Shadow & Download PNG",
      "shadow.done":     "✓ Shadow PNG downloaded!",

      // Color Swap
      "colorswap.title":  "🔵 Color Swap",
      "colorswap.desc":   "Replace one color in your image with another.",
      "colorswap.source": "Replace this color",
      "colorswap.target": "With this color",
      "colorswap.button": "Swap Color & Download",
      "colorswap.done":   "✓ Color-swapped PNG downloaded!",

      // PDF Compress Pro
      "compresspro.title":  "⚡ PDF Compress Pro",
      "compresspro.desc":   "Aggressive structural PDF compression — removes unused objects and linearizes for web.",
      "compresspro.info":   "Structural compression: removes redundant objects, linearizes for faster web loading. No image quality reduction.",
      "compresspro.button": "Compress Pro & Download",
      "compresspro.done":   "✓ Compressed PDF downloaded!",

      // Tile Image
      "tile.title":       "🔲 Tile Image",
      "tile.desc":        "Tile a small image to fill a large canvas — create seamless backgrounds and patterns.",
      "tile.canvas_size": "Canvas Size",
      "tile.custom_size": "Custom tile size",
      "tile.button":      "Create Tiled Image & Download",
      "tile.done":        "✓ Tiled image downloaded!",

      // PDF Stamp
      "stamp.title":    "🔢 PDF Page Numbers",
      "stamp.desc":     "Stamp page numbers or custom text on every page. Use {n} for current page and {total} for total.",
      "stamp.template": "Text Template",
      "stamp.position": "Position",
      "stamp.font_size":"Font Size",
      "stamp.color":    "Color",
      "stamp.button":   "Add Page Numbers & Download",
      "stamp.done":     "✓ Stamped PDF downloaded!",

      // Collage
      "collage.title":      "🖼️ Image Collage",
      "collage.desc":       "Arrange multiple photos into a grid. Perfect for family photo montages or mood boards.",
      "collage.multi_hint": "Multiple files · Max 20 images",
      "collage.columns":    "Columns",
      "collage.gap":        "Gap",
      "collage.cell_height":"Cell Height",
      "collage.bg_color":   "Background",
      "collage.button":     "Create Collage & Download",
      "collage.need_two":   "Add at least 2 images to create a collage.",

      // Image Border
      "border.title":       "🖼 Image Border & Frame",
      "border.desc":        "Add a solid-colour border around your image. Optionally round the corners.",
      "border.width":       "Border Width",
      "border.color":       "Border Color",
      "border.radius":      "Corner Radius",
      "border.radius_hint": "(0 = square)",
      "border.button":      "Add Border & Download",
      "border.done":        "✓ Bordered image downloaded!",

      // Common
      "common.upload":    "Drop a file here or",
      "common.browse":    "browse",
      "common.max_size":  "Max 50 MB",
      "common.processing":"Processing...",
      "common.download":  "Download",
      "common.change":    "Change file",
      "common.all_tools": "All Tools",
      "common.open_tool": "Open tool",

      // Compress PDF
      "compress.quality":     "Compression Quality",
      "compress.low":         "Low",
      "compress.medium":      "Medium",
      "compress.high":        "High",
      "compress.button":      "Compress PDF",
      "compress.done":        "Compression complete!",
      "compress.hint_low":    "Maximum compression, some quality loss",
      "compress.hint_medium": "Balanced compression and quality",
      "compress.hint_high":   "Minimal compression, best quality",

      // PDF to Image
      "p2i.format": "Format",
      "p2i.dpi":    "DPI",
      "p2i.button": "Convert to Images",
      "p2i.done":   "Download started!",

      // Image to PDF
      "i2p.hint":    "JPEG, PNG, WebP — multiple files allowed",
      "i2p.reorder": "Drag to reorder. Click ✕ to remove.",
      "i2p.button":  "images → Merge into PDF",
      "i2p.done":    "PDF created and downloaded!",

      // Crop
      "crop.hint":   "Click and drag to select crop area",
      "crop.button": "Download Cropped Image",

      // Passport
      "passport.title":     "Passport Photo",
      "passport.size":      "Photo Size",
      "passport.bg":        "Background Color",
      "passport.print":     "Print Layout",
      "passport.detect":    "Detecting face...",
      "passport.generate":  "Generate Passport Photo",
      "passport.print_btn": "Generate Print Sheet",

      // PDF to Text
      "p2t.extract":      "Extracting text...",
      "p2t.copy":         "Copy All",
      "p2t.copied":       "Copied!",
      "p2t.download_txt": "Download .txt",
      "p2t.clear":        "Clear",

      // Trim
      "trim.replace_bg": "Replace transparency with solid color",
      "trim.button":     "Trim Transparency",
      "trim.hint":       "PNG with transparency (alpha channel)",
      "trim.done":       "Trimmed and downloaded!",

      // Layers
      "layer.mode":          "Analysis Mode",
      "layer.channels":      "RGB Channels",
      "layer.colors":        "Color Clusters",
      "layer.both":          "Both",
      "layer.button":        "Analyze Layers",
      "layer.hint":          "JPEG, PNG, or WebP",
      "layer.channels_desc": "Separate into Red, Green, Blue channels",
      "layer.colors_desc":   "Detect 5 dominant color regions with K-means clustering",
      "layer.both_desc":     "RGB channels + color cluster separation",
      "layer.done":          "Layer ZIP downloaded!",

      // Watermark
      "wm.title":    "💧 Add Watermark",
      "wm.desc":     "Add a text watermark to your image. Control position, opacity, font size, and color.",
      "wm.text":     "Watermark Text",
      "wm.position": "Position",
      "wm.opacity":  "Opacity",
      "wm.size":     "Font Size",
      "wm.color":    "Color",
      "wm.button":   "Add Watermark & Download",
      "wm.done":     "✓ Watermarked image downloaded!",

      // Image Filters
      "filter.title":      "🎚️ Image Filters",
      "filter.desc":       "Adjust brightness, contrast, and saturation. Apply grayscale or sepia tone.",
      "filter.brightness": "Brightness",
      "filter.contrast":   "Contrast",
      "filter.saturation": "Saturation",
      "filter.sharpness":  "Sharpness",
      "filter.preset":     "Preset",
      "filter.button":     "Apply & Download",
      "filter.done":       "✓ Filtered image downloaded!",

      // PDF Protect & Unlock
      "protect.title":        "🔒 PDF Protect & Unlock",
      "protect.desc":         "Add a password to lock a PDF, or enter the password to remove protection.",
      "protect.protect_tab":  "Lock PDF",
      "protect.unlock_tab":   "Unlock PDF",
      "protect.password":     "Password",
      "protect.unlock_hint":  "Leave blank if the PDF uses an empty password.",
      "protect.protect_btn":  "Lock PDF & Download",
      "protect.unlock_btn":   "Unlock PDF & Download",
      "protect.done_protect": "✓ Password-protected PDF downloaded!",
      "protect.done_unlock":  "✓ Unlocked PDF downloaded!",

      // PDF Page Editor
      "pages.title":     "📋 PDF Page Editor",
      "pages.desc":      "Drag to reorder pages or click ✕ to delete them. Then download the edited PDF.",
      "pages.loading":   "Loading pages...",
      "pages.pages_of":  " pages remaining",
      "pages.change":    "Change PDF",
      "pages.apply":     "Download Edited PDF",

      // Color Palette
      "palette.title":       "🎨 Color Palette Extractor",
      "palette.desc":        "Extract the dominant colours from any image. Click a swatch to copy its hex code.",
      "palette.n_colors":    "Number of colors",
      "palette.extract_btn": "Extract Palette",
      "palette.copied":      "Copied!",
      "palette.copy_all":    "Copy All Hex",
      "palette.all_copied":  "Copied!",
      "palette.download_css":"Download CSS",
      "palette.new_image":   "New image",

      // Image OCR
      "ocr.title":   "🔡 Image OCR",
      "ocr.desc":    "Extract text from photos, screenshots, and scanned documents. Powered by Tesseract.",
      "ocr.running": "Running OCR...",
      "ocr.chars":   " characters extracted",
      "ocr.no_text": "No text was detected. Try a clearer photo or ensure the image contains readable text.",

      // Resize & Convert
      "resize.title":       "↔️ Resize & Convert Image",
      "resize.desc":        "Resize images to exact dimensions or scale by percentage. Convert between JPEG, PNG, and WebP.",
      "resize.dimensions":  "New Dimensions",
      "resize.lock_aspect": "Lock aspect ratio",
      "resize.width":       "Width (px)",
      "resize.height":      "Height (px)",
      "resize.format":      "Output Format",
      "resize.button":      "Resize & Download",

      // Rotate & Flip
      "rotate.title":  "🔄 Rotate & Flip Image",
      "rotate.desc":   "Rotate by any angle or flip horizontally and vertically. Preserves original format and quality.",
      "rotate.quick":  "Quick Rotate",
      "rotate.cw90":   "90° CW",
      "rotate.180":    "180°",
      "rotate.ccw90":  "90° CCW",
      "rotate.custom": "Custom Angle",
      "rotate.flip":   "Flip",
      "rotate.flip_h": "Horizontal ↔",
      "rotate.flip_v": "Vertical ↕",
      "rotate.button": "Download Rotated Image",

      // EXIF Data
      "exif.title":       "🔍 EXIF Data Viewer",
      "exif.desc":        "View metadata embedded in your photos — camera model, GPS location, exposure settings — then strip it all for privacy.",
      "exif.extract_btn": "Read EXIF Data",
      "exif.image_info":  "Image Info",
      "exif.format":      "Format",
      "exif.dimensions":  "Dimensions",
      "exif.mode":        "Color Mode",
      "exif.metadata":    "Metadata Tags",
      "exif.no_exif":     "No EXIF or metadata found in this image.",
      "exif.strip_btn":   "Strip All Metadata & Download",
      "exif.strip_done":  "✓ Clean image downloaded — all metadata removed.",

      // Merge PDFs
      "merge.title":      "🔗 Merge PDFs",
      "merge.desc":       "Combine multiple PDF files into one. Reorder with the arrow buttons before merging.",
      "merge.multi_hint": "You can add multiple PDFs at once · Max 50 MB each",
      "merge.clear_all":  "Clear all",
      "merge.add_more":   "Add more PDFs",
      "merge.button":     "Merge into One PDF",
      "merge.need_two":   "Add at least 2 PDFs to merge.",
      "merge.done":       "✓ Merged PDF downloaded!",

      // Split PDF
      "split.title":     "✂️ Split PDF",
      "split.desc":      "Split a multi-page PDF into individual single-page PDF files, delivered as a ZIP archive.",
      "split.info":      "Each page will be saved as a separate PDF. All pages are bundled in a ZIP file.",
      "split.button":    "Split into Pages (ZIP)",
      "split.done":      "✓ Split complete — ZIP downloaded!",
      "split.done_hint": "Each page is a separate PDF inside the archive.",

      // QR Code
      "qr.title":             "📱 QR Code Generator",
      "qr.desc":              "Turn any text or URL into a scannable QR code. Customize size, colors, and error correction.",
      "qr.content":           "Content",
      "qr.size":              "Size",
      "qr.error_correction":  "Error Correction",
      "qr.fg_color":          "Foreground",
      "qr.bg_color":          "Background",
      "qr.generate":          "Generate QR Code",
      "qr.download":          "Download PNG",

      // Footer
      "footer.text": "yImage — Free Image & PDF Tools",
    },

    zh: {
      "nav.tools":    "工具",
      "nav.pdf":      "PDF",
      "nav.image":    "图片",
      "nav.passport": "证件照",
      "nav.qr":       "二维码",
      "nav.all_tools":"全部工具",

      "hero.title":    "图片 & PDF 工具",
      "hero.subtitle": "免费的在线图片和PDF处理工具。所有处理在我们的服务器上完成，不会上传至第三方。",

      "tool.compress_pdf":      "压缩 PDF",
      "tool.pdf_to_image":      "PDF 转图片",
      "tool.image_to_pdf":      "图片转 PDF",
      "tool.crop_image":        "裁剪图片",
      "tool.passport_photo":    "证件照",
      "tool.pdf_to_text":       "PDF 提取文字",
      "tool.trim_transparency": "去除透明边框",
      "tool.layer_analysis":    "图层分析",
      "tool.resize_image":      "调整尺寸 & 转换",
      "tool.rotate_flip":       "旋转 & 翻转",
      "tool.exif_data":         "EXIF 数据",
      "tool.merge_pdf":         "合并 PDF",
      "tool.split_pdf":         "拆分 PDF",
      "tool.qr_code":           "二维码生成",

      "common.upload":    "拖拽文件到此处或",
      "common.browse":    "浏览",
      "common.max_size":  "最大 50 MB",
      "common.processing":"处理中...",
      "common.download":  "下载",
      "common.change":    "更换文件",
      "common.all_tools": "所有工具",
      "common.open_tool": "打开工具",

      "compress.quality":     "压缩质量",
      "compress.low":         "低",
      "compress.medium":      "中",
      "compress.high":        "高",
      "compress.button":      "压缩 PDF",
      "compress.done":        "压缩完成！",
      "compress.hint_low":    "最大压缩，质量略有损失",
      "compress.hint_medium": "压缩与质量均衡",
      "compress.hint_high":   "最小压缩，最佳质量",

      "p2i.format": "格式",
      "p2i.dpi":    "分辨率",
      "p2i.button": "转换为图片",
      "p2i.done":   "下载已开始！",

      "i2p.hint":    "JPEG、PNG、WebP——支持多文件",
      "i2p.reorder": "拖拽排序，点击 ✕ 移除。",
      "i2p.button":  "张图片合并为 PDF",
      "i2p.done":    "PDF 已创建并下载！",

      "crop.hint":   "点击并拖动选择裁剪区域",
      "crop.button": "下载裁剪后的图片",

      "passport.title":     "证件照",
      "passport.size":      "照片尺寸",
      "passport.bg":        "背景颜色",
      "passport.print":     "打印版式",
      "passport.detect":    "正在检测人脸...",
      "passport.generate":  "生成证件照",
      "passport.print_btn": "生成打印排版",

      "p2t.extract":      "正在提取文字...",
      "p2t.copy":         "复制全部",
      "p2t.copied":       "已复制！",
      "p2t.download_txt": "下载 .txt",
      "p2t.clear":        "清除",

      "trim.replace_bg": "用纯色替换透明背景",
      "trim.button":     "去除透明边框",
      "trim.hint":       "PNG 透明图片（带 alpha 通道）",
      "trim.done":       "已裁剪并下载！",

      "layer.mode":          "分析模式",
      "layer.channels":      "RGB 通道",
      "layer.colors":        "颜色聚类",
      "layer.both":          "全部",
      "layer.button":        "分析图层",
      "layer.hint":          "JPEG、PNG 或 WebP",
      "layer.channels_desc": "分离为红、绿、蓝三个通道",
      "layer.colors_desc":   "用 K-means 聚类检测 5 种主色调区域",
      "layer.both_desc":     "RGB 通道 + 颜色聚类分离",
      "layer.done":          "图层 ZIP 已下载！",

      // 水印
      "wm.title":    "💧 添加水印",
      "wm.desc":     "为图片添加文字水印，可控制位置、透明度、字体大小和颜色。",
      "wm.text":     "水印文字",
      "wm.position": "位置",
      "wm.opacity":  "透明度",
      "wm.size":     "字体大小",
      "wm.color":    "颜色",
      "wm.button":   "添加水印并下载",
      "wm.done":     "✓ 已下载添加水印的图片！",

      // 图片滤镜
      "filter.title":      "🎚️ 图片滤镜",
      "filter.desc":       "调整亮度、对比度和饱和度，或应用灰度/复古棕褐色效果。",
      "filter.brightness": "亮度",
      "filter.contrast":   "对比度",
      "filter.saturation": "饱和度",
      "filter.sharpness":  "锐度",
      "filter.preset":     "预设",
      "filter.button":     "应用并下载",
      "filter.done":       "✓ 已下载滤镜图片！",

      // PDF 加密 & 解密
      "protect.title":        "🔒 PDF 加密 & 解密",
      "protect.desc":         "为 PDF 添加密码保护，或输入密码去除保护。",
      "protect.protect_tab":  "加密 PDF",
      "protect.unlock_tab":   "解密 PDF",
      "protect.password":     "密码",
      "protect.unlock_hint":  "如果 PDF 使用空密码，请留空。",
      "protect.protect_btn":  "加密并下载",
      "protect.unlock_btn":   "解密并下载",
      "protect.done_protect": "✓ 加密后的 PDF 已下载！",
      "protect.done_unlock":  "✓ 已解密的 PDF 已下载！",

      // PDF 页面编辑
      "pages.title":    "📋 PDF 页面编辑",
      "pages.desc":     "拖动缩略图调整页面顺序，点击 ✕ 删除页面，然后下载编辑后的 PDF。",
      "pages.loading":  "正在加载页面...",
      "pages.pages_of": " 页剩余",
      "pages.change":   "更换 PDF",
      "pages.apply":    "下载编辑后的 PDF",

      // 色板提取
      "palette.title":       "🎨 提取色板",
      "palette.desc":        "从任意图片中提取主色调，点击色块复制 Hex 色值。",
      "palette.n_colors":    "颜色数量",
      "palette.extract_btn": "提取色板",
      "palette.copied":      "已复制！",
      "palette.copy_all":    "复制全部 Hex",
      "palette.all_copied":  "已复制！",
      "palette.download_css":"下载 CSS",
      "palette.new_image":   "换图片",

      // 图片 OCR
      "ocr.title":   "🔡 图片文字识别",
      "ocr.desc":    "从照片、截图和扫描文件中提取文字，由 Tesseract OCR 驱动。",
      "ocr.running": "正在识别文字...",
      "ocr.chars":   " 个字符已提取",
      "ocr.no_text": "未检测到文字。请尝试更清晰的图片，或确保图片包含可读文字。",

      // 水印
      "wm.title":    "💧 添加水印",
      "wm.desc":     "为图片添加文字水印，可控制位置、透明度、字体大小和颜色。",
      "wm.text":     "水印文字",
      "wm.position": "位置",
      "wm.opacity":  "透明度",
      "wm.size":     "字体大小",
      "wm.color":    "颜色",
      "wm.button":   "添加水印并下载",
      "wm.done":     "✓ 已下载添加水印的图片！",

      // 图片滤镜
      "filter.title":      "🎚️ 图片滤镜",
      "filter.desc":       "调整亮度、对比度和饱和度，或应用灰度/复古棕褐色效果。",
      "filter.brightness": "亮度",
      "filter.contrast":   "对比度",
      "filter.saturation": "饱和度",
      "filter.sharpness":  "锐度",
      "filter.preset":     "预设",
      "filter.button":     "应用并下载",

      // 工具名称新增
      "tool.watermark":     "添加水印",
      "tool.filters":       "图片滤镜",
      "tool.pdf_protect":   "PDF 加密 & 解密",
      "tool.pdf_pages":     "PDF 页面编辑",
      "tool.color_palette": "提取色板",
      "tool.image_ocr":     "图片文字识别",
      "tool.pdf_stamp":     "PDF 页码标注",
      "tool.collage":       "照片拼贴",
      "tool.image_border":  "图片边框",
      "tool.optimize_image":"图片优化压缩",
      "tool.favicon":       "网站图标生成",
      "tool.pdf_metadata":  "PDF 元数据",

      // 图片优化压缩
      "optimize.title":      "⚡ 图片优化压缩",
      "optimize.desc":       "在不改变尺寸的前提下减小图片文件大小，非常适合网页图片使用。",
      "optimize.quality":    "质量",
      "optimize.smaller":    "文件更小",
      "optimize.better":     "质量更好",
      "optimize.format":     "输出格式",
      "optimize.strip_meta": "去除元数据（文件更小）",
      "optimize.button":     "优化并下载",
      "optimize.done":       "✓ 已下载优化后的图片！",

      // 网站图标生成
      "favicon.title":        "⭐ 网站图标生成器",
      "favicon.desc":         "从任意图片生成 favicon.ico 和各标准尺寸 PNG 图标。",
      "favicon.hint":         "方形图片效果最佳，非方形图片将自动从中心裁剪。",
      "favicon.output_label": "ZIP 包含的文件：",
      "favicon.button":       "生成图标 (ZIP)",
      "favicon.done":         "✓ 图标 ZIP 已下载！",

      // PDF 元数据
      "pdfmeta.title":          "📋 PDF 元数据编辑器",
      "pdfmeta.desc":           "查看和编辑 PDF 内存储的标题、作者、主题和关键词，或一键清除所有元数据保护隐私。",
      "pdfmeta.read_btn":       "读取元数据",
      "pdfmeta.pages":          "页数",
      "pdfmeta.creator":        "创建程序",
      "pdfmeta.producer":       "生成工具",
      "pdfmeta.created":        "创建日期",
      "pdfmeta.title_field":    "标题",
      "pdfmeta.author_field":   "作者",
      "pdfmeta.subject_field":  "主题",
      "pdfmeta.keywords_field": "关键词",
      "pdfmeta.save_btn":       "保存并下载",
      "pdfmeta.strip_btn":      "清除全部元数据",

      // Round 6 工具名称
      "tool.blur_image":   "模糊 / 像素化",
      "tool.gif_creator":  "GIF 动图创建",
      "tool.add_text":     "添加文字",
      "tool.image_base64": "图片转 Base64",
      "tool.pdf_rotate":   "PDF 旋转页面",

      // 模糊 / 像素化
      "blur.title":    "🫥 模糊 / 像素化图片",
      "blur.desc":     "应用高斯模糊或像素化效果，非常适合隐藏人脸、车牌或敏感信息。",
      "blur.mode":     "效果",
      "blur.gaussian": "高斯模糊",
      "blur.pixelate": "像素化",
      "blur.strength": "强度",
      "blur.button":   "应用效果并下载",
      "blur.done":     "✓ 效果已应用并下载！",

      // GIF 动图
      "gif.title":         "🎞️ GIF 动图创建器",
      "gif.desc":          "将多张图片合成为 GIF 动图，控制帧速和画面大小。",
      "gif.hint":          "2–30 帧 · 顺序决定动画播放顺序",
      "gif.delay":         "帧延迟",
      "gif.max_size":      "最大尺寸",
      "gif.loop":          "循环",
      "gif.loop_infinite": "无限循环",
      "gif.loop_once":     "播放一次",
      "gif.loop_3":        "循环 3 次",
      "gif.button":        "创建 GIF 并下载",
      "gif.need_two":      "请至少添加 2 帧图片来创建 GIF。",

      // 添加文字
      "addtext.title":      "✏️ 添加文字到图片",
      "addtext.desc":       "在图片上叠加自定义文字，设置位置、字体大小、颜色、透明度和描边。",
      "addtext.text":       "文字内容",
      "addtext.click_hint": "点击图片设置文字位置",
      "addtext.stroke":     "描边",
      "addtext.position":   "位置",
      "addtext.button":     "添加文字并下载",
      "addtext.done":       "✓ 文字已添加并下载！",

      // 图片转 Base64
      "b64.title":    "🔢 图片转 Base64",
      "b64.desc":     "将图片转换为 Base64 数据 URL，直接嵌入 HTML、CSS 或 JavaScript，无需文件托管。",
      "b64.new_image":"换图片",
      "b64.data_url": "Data URL",
      "b64.raw_b64":  "纯 Base64",
      "b64.img_tag":  "HTML &lt;img&gt;",
      "b64.css_bg":   "CSS 背景",

      // PDF 旋转
      "pdfrotate.title":       "🔃 PDF 旋转页面",
      "pdfrotate.desc":        "将 PDF 所有页面或指定页面旋转 90°、180° 或 270°。",
      "pdfrotate.angle":       "旋转角度",
      "pdfrotate.which_pages": "应用到",
      "pdfrotate.all_pages":   "所有页面",
      "pdfrotate.odd_pages":   "奇数页",
      "pdfrotate.even_pages":  "偶数页",
      "pdfrotate.button":      "旋转并下载",
      "pdfrotate.done":        "✓ 已下载旋转后的 PDF！",

      // Round 7 工具名称
      "tool.invert_colors":      "颜色反转",
      "tool.pdf_watermark":      "PDF 水印",
      "tool.extract_pdf_images": "提取 PDF 图片",
      "tool.stitch_images":      "拼接图片",

      // 颜色反转
      "invert.title":    "🔲 颜色反转",
      "invert.desc":     "通过反转每个像素的颜色通道，创建照片底片效果。",
      "invert.original": "原图",
      "invert.preview":  "反转后（预览）",
      "invert.button":   "反转并下载",
      "invert.done":     "✓ 已下载颜色反转后的图片！",

      // PDF 水印
      "pdfwm.title":  "💧 PDF 水印",
      "pdfwm.desc":   "在 PDF 每一页上斜向叠加半透明文字水印。",
      "pdfwm.text":   "水印文字",
      "pdfwm.angle":  "角度",
      "pdfwm.button": "添加水印并下载",
      "pdfwm.done":   "✓ 已下载带水印的 PDF！",

      // 提取 PDF 图片
      "extractimg.title":  "🗃️ 提取 PDF 图片",
      "extractimg.desc":   "从 PDF 中提取所有嵌入图片，以 ZIP 压缩包形式下载。",
      "extractimg.info":   "图片以原始分辨率提取，根据 PDF 内部存储方式保存为 JPEG 或 PNG。",
      "extractimg.button": "提取图片 (ZIP)",
      "extractimg.done":   "✓ 图片 ZIP 已下载！",

      // 拼接图片
      "stitch.title":      "⛓️ 拼接图片",
      "stitch.desc":       "将多张图片横向或纵向拼接，制作全景图或前后对比图。",
      "stitch.hint":       "2–20 张图片 · 顺序决定拼接顺序",
      "stitch.direction":  "方向",
      "stitch.horizontal": "横向",
      "stitch.vertical":   "纵向",
      "stitch.align":      "对齐方式",
      "stitch.start":      "起始对齐",
      "stitch.center":     "居中对齐",
      "stitch.end":        "末端对齐",
      "stitch.button":     "拼接并下载",
      "stitch.need_two":   "请至少添加 2 张图片进行拼接。",

      // Round 8 工具名称 (ZH)
      "tool.round_corners":     "圆角处理",
      "tool.remove_bg_color":   "去除背景色",
      "tool.duotone":           "双色调效果",
      "tool.pdf_extract_pages": "PDF 提取页面",
      "tool.photo_caption":     "照片配文",

      // 圆角处理
      "roundcorners.title":   "⬛ 圆角处理",
      "roundcorners.desc":    "为图片添加圆角效果。PNG（透明）或 JPEG（纯色背景）输出。",
      "roundcorners.radius":  "圆角半径",
      "roundcorners.less":    "较小",
      "roundcorners.more":    "更圆",
      "roundcorners.use_bg":  "输出为带背景色的 JPEG",
      "roundcorners.preview": "实时预览（CSS 近似）",
      "roundcorners.button":  "添加圆角并下载",
      "roundcorners.done":    "✓ 已下载圆角图片！",

      // 去除背景色
      "removebg.title":     "🪄 去除背景色",
      "removebg.desc":      "将纯色背景（如白色）替换为透明，非常适合处理 Logo 和图标。",
      "removebg.bg_color":  "要去除的背景颜色",
      "removebg.tolerance": "容差",
      "removebg.exact":     "精确匹配",
      "removebg.fuzzy":     "宽泛匹配",
      "removebg.button":    "去除背景并下载 PNG",
      "removebg.done":      "✓ 已下载透明 PNG！",

      // 双色调
      "duotone.title":     "🎭 双色调效果",
      "duotone.desc":      "将图片转换为双色调——暗部用一种颜色，亮部用另一种颜色，打造艺术感。",
      "duotone.shadow":    "暗部颜色",
      "duotone.highlight": "亮部颜色",
      "duotone.presets":   "预设",
      "duotone.button":    "应用双色调并下载",
      "duotone.done":      "✓ 已下载双色调图片！",

      // PDF 提取页面
      "extractpages.title":  "📄 PDF 提取页面",
      "extractpages.desc":   "从 PDF 中提取指定页码范围，保存为新文档。",
      "extractpages.from":   "起始页",
      "extractpages.to":     "结束页",
      "extractpages.hint":   "页码从 1 开始。起止页相同可提取单页。",
      "extractpages.button": "提取页面并下载",
      "extractpages.done":   "✓ 已下载提取的 PDF！",

      // 照片配文
      "caption.title":      "💬 照片配文",
      "caption.desc":       "在照片顶部或底部添加配文栏，可自定义颜色和字体大小。",
      "caption.text":       "配文内容",
      "caption.position":   "位置",
      "caption.top":        "↑ 顶部",
      "caption.bottom":     "↓ 底部",
      "caption.bg":         "背景色",
      "caption.text_color": "文字颜色",
      "caption.button":     "添加配文并下载",
      "caption.done":       "✓ 已下载添加配文的图片！",

      // Round 9 工具名称 (ZH)
      "tool.vignette":        "暗角效果",
      "tool.grayscale":       "灰度转换",
      "tool.sharpen_denoise": "锐化 / 降噪",
      "tool.placeholder":     "占位图生成",

      // 暗角效果
      "vignette.title":    "🌑 暗角效果",
      "vignette.desc":     "将照片四周边缘变暗（或变亮），引导视线聚焦中心，营造电影感。",
      "vignette.strength": "强度",
      "vignette.color":    "边缘颜色",
      "vignette.button":   "应用暗角并下载",
      "vignette.done":     "✓ 已下载暗角图片！",

      // 灰度转换
      "gray.title":        "⬜ 灰度转换",
      "gray.desc":         "将照片转换为灰度，可选添加棕褐色、冷调或自定义色彩染色。",
      "gray.add_tint":     "添加色彩染色",
      "gray.tint_presets": "染色预设",
      "gray.custom_tint":  "自定义：",
      "gray.button":       "转换并下载",
      "gray.done":         "✓ 已下载灰度图片！",

      // 锐化 / 降噪
      "sharpen.title":        "🔪 锐化 / 降噪",
      "sharpen.desc":         "锐化模糊图片、增强边缘轮廓，或对低光噪点图片降噪。最多 5 次。",
      "sharpen.mode":         "模式",
      "sharpen.sharpen":      "锐化",
      "sharpen.edge":         "边缘增强",
      "sharpen.denoise":      "降噪",
      "sharpen.passes":       "处理次数",
      "sharpen.mild":         "轻微",
      "sharpen.strong":       "强烈",
      "sharpen.button":       "应用并下载",
      "sharpen.done":         "✓ 已处理并下载！",

      // 占位图生成
      "placeholder.title":      "🟪 占位图生成器",
      "placeholder.desc":       "生成任意尺寸的纯色占位图，适用于 UI 原型设计和布局测试。",
      "placeholder.size":       "尺寸",
      "placeholder.bg_color":   "背景色",
      "placeholder.text_color": "文字颜色",
      "placeholder.label":      "自定义标签",
      "placeholder.preview":    "预览",
      "placeholder.button":     "生成占位图",

      // Round 10 工具名称 (ZH)
      "tool.crop_aspect":      "裁剪比例",
      "tool.drop_shadow":      "投影效果",
      "tool.color_swap":       "颜色替换",
      "tool.pdf_compress_pro": "PDF 深度压缩",
      "tool.tile_image":       "图片平铺",

      // 裁剪比例
      "aspect.title":    "📐 裁剪为指定比例",
      "aspect.desc":     "将图片裁剪为目标宽高比，不改变分辨率。",
      "aspect.ratio":    "宽高比",
      "aspect.anchor":   "裁剪锚点",
      "aspect.original": "原图",
      "aspect.result":   "效果",
      "aspect.button":   "裁剪并下载",
      "aspect.done":     "✓ 已下载裁剪后的图片！",

      // 投影效果
      "shadow.title":    "🌤 投影效果",
      "shadow.desc":     "在图片后面添加柔和投影，PNG 透明图片效果最佳。",
      "shadow.offset_x": "X 偏移",
      "shadow.offset_y": "Y 偏移",
      "shadow.blur":     "模糊半径",
      "shadow.opacity":  "透明度",
      "shadow.color":    "颜色",
      "shadow.button":   "添加投影并下载 PNG",
      "shadow.done":     "✓ 已下载带投影的 PNG！",

      // 颜色替换
      "colorswap.title":  "🔵 颜色替换",
      "colorswap.desc":   "将图片中的一种颜色替换为另一种颜色，适合重新着色 Logo 和图标。",
      "colorswap.source": "替换此颜色",
      "colorswap.target": "替换为此颜色",
      "colorswap.button": "替换颜色并下载",
      "colorswap.done":   "✓ 已下载颜色替换后的 PNG！",

      // PDF 深度压缩
      "compresspro.title":  "⚡ PDF 深度压缩",
      "compresspro.desc":   "激进结构压缩——移除无用对象、重建交叉引用并线性化，适合网页快速加载。",
      "compresspro.info":   "结构压缩：移除冗余对象，线性化以加快网页加载。不降低图片质量。",
      "compresspro.button": "深度压缩并下载",
      "compresspro.done":   "✓ 已下载深度压缩的 PDF！",

      // 图片平铺
      "tile.title":       "🔲 图片平铺",
      "tile.desc":        "将小图片重复平铺填满大画布，用于创建无缝背景和重复图案。",
      "tile.canvas_size": "画布大小",
      "tile.custom_size": "自定义单格大小",
      "tile.button":      "创建平铺图片并下载",
      "tile.done":        "✓ 已下载平铺图片！",

      // PDF 页码标注
      "stamp.title":    "🔢 PDF 页码标注",
      "stamp.desc":     "在每一页上加盖页码或自定义文字。{n} 为当前页码，{total} 为总页数。",
      "stamp.template": "文字模板",
      "stamp.position": "位置",
      "stamp.font_size":"字体大小",
      "stamp.color":    "颜色",
      "stamp.button":   "添加页码并下载",
      "stamp.done":     "✓ 已下载带页码的 PDF！",

      // 照片拼贴
      "collage.title":      "🖼️ 照片拼贴",
      "collage.desc":       "将多张照片排列成网格，非常适合制作家庭相册拼贴或情绪板。",
      "collage.multi_hint": "支持多文件 · 最多 20 张图片",
      "collage.columns":    "列数",
      "collage.gap":        "间距",
      "collage.cell_height":"格高",
      "collage.bg_color":   "背景色",
      "collage.button":     "创建拼贴并下载",
      "collage.need_two":   "请至少添加 2 张图片来创建拼贴。",

      // 图片边框
      "border.title":       "🖼 图片边框",
      "border.desc":        "为图片添加纯色边框，可选圆角效果。",
      "border.width":       "边框宽度",
      "border.color":       "边框颜色",
      "border.radius":      "圆角半径",
      "border.radius_hint": "（0 = 直角）",
      "border.button":      "添加边框并下载",
      "border.done":        "✓ 已下载添加边框的图片！",

      // 调整尺寸 & 转换
      "resize.title":       "↔️ 调整图片尺寸 & 格式转换",
      "resize.desc":        "将图片调整为精确的像素尺寸，或按比例缩放。支持 JPEG、PNG、WebP 格式互转。",
      "resize.dimensions":  "新尺寸",
      "resize.lock_aspect": "锁定宽高比",
      "resize.width":       "宽度 (px)",
      "resize.height":      "高度 (px)",
      "resize.format":      "输出格式",
      "resize.button":      "调整并下载",

      // 旋转 & 翻转
      "rotate.title":  "🔄 旋转 & 翻转图片",
      "rotate.desc":   "按任意角度旋转，或水平/垂直翻转。保留原始格式和质量。",
      "rotate.quick":  "快速旋转",
      "rotate.cw90":   "顺时针 90°",
      "rotate.180":    "180°",
      "rotate.ccw90":  "逆时针 90°",
      "rotate.custom": "自定义角度",
      "rotate.flip":   "翻转",
      "rotate.flip_h": "水平翻转 ↔",
      "rotate.flip_v": "垂直翻转 ↕",
      "rotate.button": "下载旋转后的图片",

      // EXIF 数据
      "exif.title":       "🔍 EXIF 数据查看器",
      "exif.desc":        "查看照片中嵌入的元数据——相机型号、GPS 位置、曝光参数——一键清除保护隐私。",
      "exif.extract_btn": "读取 EXIF 数据",
      "exif.image_info":  "图片信息",
      "exif.format":      "格式",
      "exif.dimensions":  "尺寸",
      "exif.mode":        "颜色模式",
      "exif.metadata":    "元数据标签",
      "exif.no_exif":     "此图片中未找到 EXIF 或元数据。",
      "exif.strip_btn":   "清除所有元数据并下载",
      "exif.strip_done":  "✓ 已下载干净图片——所有元数据已移除。",

      // 合并 PDF
      "merge.title":      "🔗 合并 PDF",
      "merge.desc":       "将多个 PDF 文件合并为一个。合并前可用箭头按钮调整顺序。",
      "merge.multi_hint": "可同时添加多个 PDF · 每个最大 50 MB",
      "merge.clear_all":  "清空全部",
      "merge.add_more":   "继续添加 PDF",
      "merge.button":     "合并为一个 PDF",
      "merge.need_two":   "请至少添加 2 个 PDF 文件。",
      "merge.done":       "✓ 合并后的 PDF 已下载！",

      // 拆分 PDF
      "split.title":     "✂️ 拆分 PDF",
      "split.desc":      "将多页 PDF 拆分为单独的单页 PDF 文件，以 ZIP 压缩包形式下载。",
      "split.info":      "每页将保存为独立的 PDF 文件，所有页面打包在一个 ZIP 中。",
      "split.button":    "拆分为单页 (ZIP)",
      "split.done":      "✓ 拆分完成——ZIP 已下载！",
      "split.done_hint": "压缩包中每个文件对应原 PDF 的一页。",

      // 二维码
      "qr.title":            "📱 二维码生成器",
      "qr.desc":             "将任意文字或网址生成可扫描的二维码，支持自定义尺寸、颜色和容错级别。",
      "qr.content":          "内容",
      "qr.size":             "尺寸",
      "qr.error_correction": "容错级别",
      "qr.fg_color":         "前景色",
      "qr.bg_color":         "背景色",
      "qr.generate":         "生成二维码",
      "qr.download":         "下载 PNG",

      "footer.text": "yImage — 免费图片 & PDF 工具",
    },
  };

  let lang = localStorage.getItem('yimage_lang') || 'en';

  function t(key) {
    return (LANGS[lang] || LANGS.en)[key] || (LANGS.en)[key] || key;
  }

  function apply(root) {
    (root || document).querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      const val = t(key);
      if (val && val !== key) el.textContent = val;
    });
    (root || document).querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const val = t(el.getAttribute('data-i18n-placeholder'));
      if (val) el.placeholder = val;
    });
  }

  function toggle() {
    lang = lang === 'en' ? 'zh' : 'en';
    localStorage.setItem('yimage_lang', lang);
    apply();
    // Update toggle button text
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = lang === 'en' ? '中文' : 'EN';
    });
  }

  function init() {
    apply();
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = lang === 'en' ? '中文' : 'EN';
    });
  }

  document.addEventListener('DOMContentLoaded', init);

  return { t, apply, toggle, init, get lang() { return lang; } };
})();
