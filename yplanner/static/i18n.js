/**
 * yPlanner i18n — EN/ZH bilingual support.
 */
const I18n = {
  lang: 'en',

  LANGS: {
    'nav.planner':       { en: 'Trip Planner',     zh: '行程规划' },
    'nav.houses':        { en: 'Houses',           zh: '房产' },
    'nav.toggle_theme':  { en: 'Toggle Theme',     zh: '切换主题' },
    'footer.text':       { en: 'yPlanner — powered by Google Maps', zh: 'yPlanner — 由 Google Maps 提供支持' },
    'footer.copyright':  { en: '\u00a9 2025 yPlanner', zh: '\u00a9 2025 yPlanner' },

    'user.label':        { en: 'Your Name',        zh: '你的名字' },
    'user.placeholder':  { en: 'Enter your name to save trips...', zh: '输入名字以保存行程...' },
    'user.set':          { en: 'Set',              zh: '确定' },
    'user.change':       { en: 'Change',           zh: '更换' },

    'auth.label':        { en: 'Sign In',          zh: '登录' },
    'auth.desc':         { en: 'Sign in to save and sync your trips', zh: '登录以保存和同步你的行程' },
    'auth.google':       { en: 'Sign in with Google', zh: '使用 Google 登录' },
    'auth.apple':        { en: 'Sign in with Apple',  zh: '使用 Apple 登录' },
    'auth.signout':      { en: 'Sign out',         zh: '退出' },
    'auth.not_configured': { en: 'OAuth not configured. Set GOOGLE_CLIENT_ID in .env', zh: 'OAuth 未配置，请在 .env 中设置 GOOGLE_CLIENT_ID' },

    'search.label':      { en: 'Search Places',    zh: '搜索地点' },
    'search.placeholder':{ en: 'Search for a place...', zh: '搜索地点...' },

    'mode.label':        { en: 'Travel Mode',      zh: '出行方式' },
    'mode.driving':      { en: 'Drive',            zh: '驾车' },
    'mode.walking':      { en: 'Walk',             zh: '步行' },
    'mode.transit':      { en: 'Transit',          zh: '公交' },

    'departure.label':   { en: 'Departure Time',   zh: '出发时间' },

    'nearby.label':      { en: 'Explore Nearby',   zh: '探索附近' },
    'nearby.restaurant': { en: 'Restaurants',      zh: '餐厅' },
    'nearby.cafe':       { en: 'Cafes',            zh: '咖啡' },
    'nearby.attraction': { en: 'Attractions',      zh: '景点' },
    'nearby.kids':       { en: 'Kids',             zh: '亲子' },
    'nearby.gas':        { en: 'Gas Stations',     zh: '加油站' },
    'nearby.hotel':      { en: 'Hotels',           zh: '酒店' },
    'nearby.shopping':   { en: 'Shopping',         zh: '购物' },
    'nearby.loading':    { en: 'Searching...',     zh: '搜索中...' },

    'stops.label':       { en: 'Trip Stops',       zh: '行程站点' },
    'stops.count':       { en: 'stops',            zh: '个站点' },
    'stops.empty':       { en: 'Search above to add places to your trip', zh: '在上方搜索以添加地点到行程' },

    'stop.pass_through': { en: 'Pass through',     zh: '途经' },
    'stop.custom':       { en: 'Custom...',        zh: '自定义...' },

    'summary.label':     { en: 'Trip Summary',     zh: '行程概览' },
    'summary.distance':  { en: 'Distance',         zh: '总距离' },
    'summary.travel_time':{ en: 'Travel Time',     zh: '行驶时间' },
    'summary.stop_time': { en: 'Stop Time',        zh: '停留时间' },
    'summary.total_time':{ en: 'Total Time',       zh: '总时间' },
    'summary.arrival':   { en: 'Estimated End',    zh: '预计结束' },

    'action.navigate':   { en: 'Navigate',         zh: '导航' },
    'action.save':       { en: 'Save Trip',        zh: '保存行程' },
    'action.share':      { en: 'Share',            zh: '分享' },
    'action.clear':      { en: 'Clear',            zh: '清除' },

    'mytrips.label':     { en: 'My Trips',         zh: '我的行程' },
    'mytrips.refresh':   { en: 'Refresh',          zh: '刷新' },
    'mytrips.empty':     { en: 'No saved trips yet', zh: '暂无保存的行程' },

    'save.title':        { en: 'Save Trip',        zh: '保存行程' },
    'save.placeholder':  { en: 'Trip name...',     zh: '行程名称...' },
    'save.btn':          { en: 'Save',             zh: '保存' },
    'save.cancel':       { en: 'Cancel',           zh: '取消' },

    'share.title':       { en: 'Share Trip',       zh: '分享行程' },
    'share.desc':        { en: 'Anyone with this link can view your trip (expires in 30 days)', zh: '任何拥有此链接的人都可以查看你的行程（30天后过期）' },
    'share.copy':        { en: 'Copy',             zh: '复制' },
    'share.copied':      { en: 'Copied!',          zh: '已复制!' },
    'share.close':       { en: 'Close',            zh: '关闭' },

    'shared.banner':     { en: 'Shared by',        zh: '分享自' },
    'shared.save':       { en: 'Save to My Trips', zh: '保存到我的行程' },
    'shared.signin_to_save': { en: 'Sign in to save this trip', zh: '登录以保存此行程' },

    'layers.btn':        { en: 'Layers',           zh: '图层' },
    'layers.roadmap':    { en: 'Roadmap',          zh: '路线图' },
    'layers.satellite':  { en: 'Satellite',        zh: '卫星' },
    'layers.terrain':    { en: 'Terrain',          zh: '地形' },
    'layers.hybrid':     { en: 'Hybrid',           zh: '混合' },
    'layers.traffic':    { en: 'Traffic',          zh: '交通状况' },

    'houses.search':     { en: 'Search Address',   zh: '搜索地址' },
    'houses.placeholder':{ en: 'Enter an address...', zh: '输入地址...' },
    'houses.loading':    { en: 'Loading property details...', zh: '加载房产详情...' },
    'houses.estimate':   { en: 'Redfin Estimate',  zh: 'Redfin 估价' },
    'houses.last_sold':  { en: 'Last sold:',       zh: '上次成交:' },
    'houses.beds':       { en: 'Beds',             zh: '卧' },
    'houses.baths':      { en: 'Baths',            zh: '卫' },
    'houses.sqft':       { en: 'Sq Ft',            zh: '平方英尺' },
    'houses.price_sqft': { en: 'Price/Sq Ft:',     zh: '每平方英尺:' },
    'houses.scores':     { en: 'Neighborhood Scores', zh: '社区评分' },
    'houses.walk':       { en: 'Walk',             zh: '步行' },
    'houses.bike':       { en: 'Bike',             zh: '骑行' },
    'houses.transit':    { en: 'Transit',          zh: '公交' },
    'houses.view_redfin':{ en: 'View on Redfin',   zh: '在 Redfin 查看' },
    'houses.similar':    { en: 'Similar',          zh: '类似房源' },
    'houses.similar_listings': { en: 'Similar Listings', zh: '类似房源' },
    'houses.empty':      { en: 'Search for an address to see property details', zh: '搜索地址以查看房产详情' },
  },

  get(key) {
    const entry = this.LANGS[key];
    if (!entry) return key;
    return entry[this.lang] || entry.en || key;
  },

  toggle() {
    this.lang = this.lang === 'en' ? 'zh' : 'en';
    document.cookie = `yplanner_lang=${this.lang};path=/;max-age=31536000`;
    this.apply();
    this.updateToggleBtn();
  },

  apply() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      const text = this.get(key);
      if (text !== key) el.textContent = text;
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      const text = this.get(key);
      if (text !== key) el.placeholder = text;
    });
    const searchInput = document.getElementById('place-search');
    if (searchInput) searchInput.placeholder = this.get('search.placeholder');
  },

  updateToggleBtn() {
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = this.lang === 'en' ? '中文' : 'EN';
    });
  },

  init() {
    const match = document.cookie.match(/yplanner_lang=(en|zh)/);
    if (match) this.lang = match[1];
    this.apply();
    this.updateToggleBtn();
  }
};

document.addEventListener('DOMContentLoaded', () => I18n.init());
