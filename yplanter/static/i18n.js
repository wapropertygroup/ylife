/**
 * yPlanter i18n — EN/ZH bilingual support.
 */
const I18n = {
  lang: 'en',

  LANGS: {
    'nav.brand':          { en: 'yPlanter',            zh: 'yPlanter' },
    'nav.browse':         { en: 'Browse Plants',       zh: '浏览植物' },
    'nav.calendar':       { en: 'Planting Calendar',   zh: '种植日历' },
    'nav.yard':           { en: 'Yard Ideas',          zh: '庭院方案' },
    'nav.resources':      { en: 'Resources',           zh: '资源链接' },
    'nav.toggle_theme':   { en: 'Toggle Theme',        zh: '切换主题' },

    'hero.title':         { en: 'Seattle Garden Guide',        zh: '西雅图种植指南' },
    'hero.subtitle':      { en: 'What to plant, when to plant it, and how to grow it in the Pacific Northwest. Vegetables, herbs, fruit, houseplants & yard ideas for USDA Zone 8b.', zh: '在太平洋西北地区种什么、什么时候种、怎么种。蔬菜、香草、水果、室内植物和庭院方案（USDA 8b区）。' },

    'search.placeholder': { en: 'Search plants, vegetables, herbs...', zh: '搜索植物、蔬菜、香草...' },

    'filter.all_types':   { en: 'All Types',           zh: '所有类型' },
    'filter.vegetable':   { en: 'Vegetables',          zh: '蔬菜' },
    'filter.fruit':       { en: 'Fruit',               zh: '水果' },
    'filter.herb':        { en: 'Herbs',               zh: '香草' },
    'filter.houseplant':  { en: 'Houseplants',         zh: '室内植物' },
    'filter.perennial':   { en: 'Perennials',          zh: '多年生植物' },
    'filter.annual':      { en: 'Annuals',             zh: '一年生植物' },
    'filter.shrub':       { en: 'Shrubs',              zh: '灌木' },
    'filter.tree':        { en: 'Trees',               zh: '乔木' },
    'filter.all_levels':  { en: 'All Levels',          zh: '所有难度' },
    'filter.easy':        { en: 'Easy',                zh: '简单' },
    'filter.moderate':    { en: 'Moderate',            zh: '中等' },
    'filter.hard':        { en: 'Hard',                zh: '困难' },

    'section.vegetables': { en: 'Vegetables & Edibles for Seattle', zh: '适合西雅图的蔬菜' },
    'section.fruit':      { en: 'Fruit for PNW Gardens',           zh: '太平洋西北水果' },
    'section.herbs':      { en: 'Herbs',                            zh: '香草植物' },
    'section.houseplants':{ en: 'Indoor Plants for PNW Homes',     zh: '适合PNW室内的植物' },
    'section.perennials': { en: 'Perennial Flowers for PNW',      zh: '太平洋西北多年生花卉' },
    'section.annuals':    { en: 'Annual Flowers for Seattle',     zh: '适合西雅图的一年生花卉' },
    'section.shrubs':     { en: 'Shrubs & Bushes for PNW',       zh: '太平洋西北灌木' },
    'section.trees':      { en: 'Ornamental Trees for Seattle',  zh: '西雅图观赏乔木' },
    'section.calendar':   { en: 'Seattle Planting Calendar',        zh: '西雅图种植日历' },
    'section.yard':       { en: 'Yard Ideas for Seattle',           zh: '西雅图庭院方案' },
    'section.collection': { en: 'My Collection',                    zh: '我的收藏' },
    'section.collection_note': { en: '(saved locally in your browser)', zh: '（保存在浏览器本地）' },
    'section.view_all':   { en: 'View all →',                       zh: '查看全部 →' },

    'collection.empty':   { en: 'Browse plants above and click "Add to Collection" on any plant detail page to track your garden.', zh: '浏览上方植物，在详情页点击"添加到收藏"来追踪你的花园。' },

    'calendar.title':     { en: 'Seattle Planting Calendar',        zh: '西雅图种植日历' },
    'calendar.subtitle':  { en: 'Month-by-month guide for USDA Zone 8b (Seattle / Puget Sound region). Last frost: ~April 15 · First frost: ~November 15', zh: '逐月指南（USDA 8b区，西雅图/普吉特湾地区）。末次霜冻：约4月15日 · 初霜：约11月15日' },

    'yard.title':         { en: 'Yard Ideas for Seattle Gardens',   zh: '西雅图庭院设计方案' },
    'yard.subtitle':      { en: 'Garden design ideas that work with Seattle\'s climate, soil, and rainfall. All suggestions use plants suited to USDA Zone 8b.', zh: '适合西雅图气候、土壤和降雨的庭院设计。所有建议都使用适合USDA 8b区的植物。' },

    'resources.title':    { en: 'PNW Gardening Resources',          zh: '太平洋西北园艺资源' },
    'resources.subtitle': { en: 'Useful websites, tools, and local organizations for Seattle gardeners.', zh: '对西雅图园丁有用的网站、工具和本地组织。' },

    'nav.history':        { en: 'Chat History',          zh: '对话历史' },

    'chat.title':         { en: 'Garden AI',              zh: '园艺 AI' },
    'chat.hint':          { en: 'Ask me anything about gardening in Seattle / Pacific Northwest!', zh: '问我任何关于西雅图/太平洋西北地区园艺的问题！' },

    'footer.text':        { en: 'yPlanter — Seattle / PNW Garden Guide (USDA Zone 8b)', zh: 'yPlanter — 西雅图/太平洋西北种植指南（USDA 8b区）' },
    'footer.copyright':   { en: '\u00a9 2025 yPlanter', zh: '\u00a9 2025 yPlanter' },
  },

  init() {
    this.lang = localStorage.getItem('yplanter_lang') || 'en';
    this.apply();
  },

  toggle() {
    this.lang = this.lang === 'en' ? 'zh' : 'en';
    localStorage.setItem('yplanter_lang', this.lang);
    this.apply();
  },

  apply() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      const entry = this.LANGS[key];
      if (entry) el.textContent = entry[this.lang] || entry.en;
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      const entry = this.LANGS[key];
      if (entry) el.placeholder = entry[this.lang] || entry.en;
    });
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = this.lang === 'en' ? '中文' : 'EN';
    });
  }
};

document.addEventListener('DOMContentLoaded', () => I18n.init());
