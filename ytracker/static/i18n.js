/**
 * yTracker i18n — EN/ZH bilingual support.
 */
const I18n = {
  lang: 'en',

  LANGS: {
    'nav.brand':            { en: 'yTracker',              zh: 'yTracker' },
    'nav.dashboard':        { en: 'Dashboard',             zh: '仪表盘' },
    'nav.toggle_theme':     { en: 'Toggle Theme',          zh: '切换主题' },

    'hero.title':           { en: 'Price Tracker',         zh: '价格追踪' },
    'hero.subtitle':        { en: 'Track prices from your favorite stores. Get notified when prices drop. Never miss a deal.', zh: '追踪你喜欢的商店价格。降价时收到通知，永不错过优惠。' },

    'add.placeholder':      { en: 'Paste a product URL from any supported store...', zh: '粘贴任意支持商店的商品链接...' },
    'add.button':           { en: 'Track Item',            zh: '追踪商品' },
    'add.success':          { en: 'Item added successfully!', zh: '商品添加成功！' },

    'dashboard.tracked':    { en: 'Tracked Items',         zh: '追踪中的商品' },
    'dashboard.check_all':  { en: '🔄 Check All Prices', zh: '🔄 检查所有价格' },

    'card.current':         { en: 'Current Price',         zh: '当前价格' },
    'card.record_low':      { en: 'Record Low!',           zh: '历史最低！' },
    'card.last_check':      { en: 'Last checked',          zh: '上次检查' },

    'empty.title':          { en: 'No items tracked yet',  zh: '还没有追踪任何商品' },
    'empty.subtitle':       { en: 'Paste a product URL from any supported store above to start tracking prices.', zh: '在上方粘贴任意支持商店的商品链接开始追踪价格。' },

    'detail.back':          { en: 'Back to Dashboard',     zh: '返回仪表盘' },
    'detail.view_on':       { en: 'View on',               zh: '在...查看' },
    'detail.check_now':     { en: '🔄 Check Price Now', zh: '🔄 立即查价' },
    'detail.delete':        { en: 'Delete',                zh: '删除' },
    'detail.price_history': { en: 'Price History',         zh: '价格历史' },
    'detail.notifications': { en: '🔔 Price Drop Alerts', zh: '🔔 降价提醒' },
    'detail.enable_alerts': { en: 'Enable email alerts',   zh: '启用邮件提醒' },
    'detail.save':          { en: 'Save',                  zh: '保存' },
    'detail.ai_analysis':   { en: '🤖 AI Price Analysis', zh: '🤖 AI价格分析' },
    'detail.analyze':       { en: 'Analyze Price Trends',  zh: '分析价格趋势' },
    'detail.analyzing':     { en: 'Analyzing...',          zh: '分析中...' },
    'detail.record_low_banner': { en: 'This is the lowest price ever recorded!', zh: '这是有记录以来的最低价格！' },
    'detail.fetch_live':      { en: 'Fetch Current Info',    zh: '获取实时信息' },
    'detail.fetching':        { en: 'Fetching…',             zh: '获取中…' },
    'detail.live_price':      { en: '✓ Live Price from Store', zh: '✓ 商店实时价格' },
    'detail.no_price':        { en: 'Price not available — the store may have blocked the initial scrape.', zh: '价格暂不可用 — 商店可能阻止了初次抓取。' },
    'detail.compare':         { en: '📊 Compare Prices',    zh: '📊 比较价格' },

    'stats.current':        { en: 'Current Price',         zh: '当前价格' },
    'stats.record_low':     { en: 'Record Low',            zh: '历史最低' },
    'stats.record_high':    { en: 'Record High',           zh: '历史最高' },
    'stats.average':        { en: 'Average',               zh: '平均价格' },

    'detail.tracking_history': { en: '📋 Tracking History', zh: '📋 追踪记录' },
    'history.empty':        { en: 'No price checks recorded yet. Click "Check Price Now" above to start.', zh: '暂无价格检查记录。点击上方"立即查价"开始。' },
    'history.date':         { en: 'Date & Time',           zh: '日期时间' },
    'history.snapshot':     { en: 'Snapshot',              zh: '快照' },
    'history.price':        { en: 'Price',                 zh: '价格' },
    'history.change':       { en: 'Change',                zh: '变动' },
    'history.note':         { en: 'Note',                  zh: '备注' },
    'history.record_low':   { en: 'Record Low',            zh: '历史最低' },
    'history.big_drop':     { en: 'Big Drop',              zh: '大幅降价' },
    'history.first_check':  { en: 'First check',           zh: '首次记录' },
    'history.show_less':    { en: 'Show less',             zh: '收起' },

    'footer.text':          { en: 'yTracker — Multi-Store Price Tracker', zh: 'yTracker — 多商店价格追踪' },
    'footer.copyright':     { en: '© 2025 yTracker',  zh: '© 2025 yTracker' },
  },

  init() {
    this.lang = localStorage.getItem('ytracker_lang') || 'en';
    this.apply();
  },

  toggle() {
    this.lang = this.lang === 'en' ? 'zh' : 'en';
    localStorage.setItem('ytracker_lang', this.lang);
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
