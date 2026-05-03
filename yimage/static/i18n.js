/**
 * yImage i18n — English + Simplified Chinese translations
 */
const I18n = (() => {
  const LANGS = {
    en: {
      // Nav
      "nav.tools": "Tools",
      "nav.pdf": "PDF",
      "nav.image": "Image",
      "nav.passport": "Passport",

      // Index
      "hero.title": "Image & PDF Tools",
      "hero.subtitle": "Free browser-based tools for images and PDFs. No uploads to third parties — all processing happens on our server.",

      // Tools
      "tool.compress_pdf": "Compress PDF",
      "tool.pdf_to_image": "PDF to Image",
      "tool.image_to_pdf": "Image to PDF",
      "tool.crop_image": "Crop Image",
      "tool.passport_photo": "Passport Photo",
      "tool.pdf_to_text": "PDF to Text",
      "tool.trim_transparency": "Trim Transparency",
      "tool.layer_analysis": "Layer Analysis",

      // Common
      "common.upload": "Drop a file here or",
      "common.browse": "browse",
      "common.max_size": "Max 50 MB",
      "common.processing": "Processing...",
      "common.download": "Download",
      "common.change": "Change file",
      "common.all_tools": "All Tools",
      "common.open_tool": "Open tool",

      // Compress PDF
      "compress.quality": "Compression Quality",
      "compress.low": "Low",
      "compress.medium": "Medium",
      "compress.high": "High",
      "compress.button": "Compress PDF",
      "compress.done": "Compression complete!",

      // PDF to Image
      "p2i.format": "Format",
      "p2i.dpi": "DPI",
      "p2i.button": "Convert to Images",

      // Image to PDF
      "i2p.reorder": "Drag to reorder. Click ✕ to remove.",
      "i2p.button": "Merge into PDF",
      "i2p.done": "PDF created and downloaded!",

      // Crop
      "crop.hint": "Click and drag to select crop area",
      "crop.button": "Download Cropped Image",

      // Passport
      "passport.title": "Passport Photo",
      "passport.size": "Photo Size",
      "passport.bg": "Background Color",
      "passport.print": "Print Layout",
      "passport.detect": "Detecting face...",
      "passport.generate": "Generate Passport Photo",
      "passport.print_btn": "Generate Print Sheet",

      // PDF to Text
      "p2t.extract": "Extracting text...",
      "p2t.copy": "Copy All",
      "p2t.copied": "Copied!",
      "p2t.download_txt": "Download .txt",

      // Trim
      "trim.replace_bg": "Replace transparency with solid color",
      "trim.button": "Trim Transparency",

      // Layers
      "layer.mode": "Analysis Mode",
      "layer.channels": "RGB Channels",
      "layer.colors": "Color Clusters",
      "layer.both": "Both",
      "layer.button": "Analyze Layers",

      // Footer
      "footer.text": "yImage — Free Image & PDF Tools",
    },

    zh: {
      "nav.tools": "工具",
      "nav.pdf": "PDF",
      "nav.image": "图片",
      "nav.passport": "证件照",

      "hero.title": "图片 & PDF 工具",
      "hero.subtitle": "免费的在线图片和PDF处理工具。所有处理在我们的服务器上完成，不会上传至第三方。",

      "tool.compress_pdf": "压缩 PDF",
      "tool.pdf_to_image": "PDF 转图片",
      "tool.image_to_pdf": "图片转 PDF",
      "tool.crop_image": "裁剪图片",
      "tool.passport_photo": "证件照",
      "tool.pdf_to_text": "PDF 提取文字",
      "tool.trim_transparency": "去除透明边框",
      "tool.layer_analysis": "图层分析",

      "common.upload": "拖拽文件到此处或",
      "common.browse": "浏览",
      "common.max_size": "最大 50 MB",
      "common.processing": "处理中...",
      "common.download": "下载",
      "common.change": "更换文件",
      "common.all_tools": "所有工具",
      "common.open_tool": "打开工具",

      "compress.quality": "压缩质量",
      "compress.low": "低",
      "compress.medium": "中",
      "compress.high": "高",
      "compress.button": "压缩 PDF",
      "compress.done": "压缩完成！",

      "p2i.format": "格式",
      "p2i.dpi": "分辨率",
      "p2i.button": "转换为图片",

      "i2p.reorder": "拖拽排序，点击 ✕ 移除。",
      "i2p.button": "合并为 PDF",
      "i2p.done": "PDF 已创建并下载！",

      "crop.hint": "点击并拖动选择裁剪区域",
      "crop.button": "下载裁剪后的图片",

      "passport.title": "证件照",
      "passport.size": "照片尺寸",
      "passport.bg": "背景颜色",
      "passport.print": "打印版式",
      "passport.detect": "正在检测人脸...",
      "passport.generate": "生成证件照",
      "passport.print_btn": "生成打印排版",

      "p2t.extract": "正在提取文字...",
      "p2t.copy": "复制全部",
      "p2t.copied": "已复制！",
      "p2t.download_txt": "下载 .txt",

      "trim.replace_bg": "用纯色替换透明背景",
      "trim.button": "去除透明边框",

      "layer.mode": "分析模式",
      "layer.channels": "RGB 通道",
      "layer.colors": "颜色聚类",
      "layer.both": "全部",
      "layer.button": "分析图层",

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
