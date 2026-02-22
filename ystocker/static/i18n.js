/**
 * yStocker i18n — client-side EN / ZH language switcher
 *
 * Usage:
 *   Add  data-i18n="key"            for element textContent
 *   Add  data-i18n-placeholder="key" for input placeholder
 *   Add  data-i18n-title="key"       for title / tooltip attributes
 *
 * Call  I18n.apply()  after any dynamic DOM insertion.
 */
const I18n = (() => {

  const LANGS = {

    // ── base.html ──────────────────────────────────────────────────────
    'nav.home':           { en: 'Home',      zh: '首页' },
    'nav.sectors':        { en: 'Sectors',   zh: '板块' },
    'nav.peer_groups':    { en: 'Peer Groups', zh: '同类组' },
    'nav.view_all':       { en: 'View all sectors ↓', zh: '查看全部板块 ↓' },
    'nav.lookup':         { en: '🔍 Lookup', zh: '🔍 查询' },
    'nav.groups':         { en: '⚙ Groups',  zh: '⚙ 分组' },
    'nav.contact':        { en: '✉ Contact', zh: '✉ 联系' },
    'nav.refresh':        { en: '↻ Refresh', zh: '↻ 刷新' },
    'nav.refresh_title':  { en: 'Refresh data', zh: '刷新数据' },
    'nav.refresh_body':   { en: 'Clears the in-memory cache and re-fetches live prices, PE ratios, and analyst targets for all tickers from Yahoo Finance.',
                            zh: '清除内存缓存，从 Yahoo Finance 重新获取所有股票的最新价格、市盈率及分析师目标价。' },
    'footer.text':        { en: 'yStocker — data via Yahoo Finance', zh: 'yStocker — 数据来源：Yahoo Finance' },
    'footer.contact':     { en: 'Contact Us', zh: '联系我们' },

    // ── index.html ─────────────────────────────────────────────────────
    'index.hero_title':   { en: 'Stock Valuation', zh: '股票估值' },
    'index.hero_sub':     { en: 'Dashboard',       zh: '仪表盘' },
    'index.hero_desc':    { en: 'PE ratios, PEG ratios & analyst targets across', zh: '市盈率、PEG 及分析师目标价，覆盖' },
    'index.peer_groups':  { en: 'peer groups.', zh: '个同类组。' },
    'index.data_as_of':   { en: 'Data as of', zh: '数据截至' },
    'index.refreshing':   { en: 'refreshing now…', zh: '正在刷新…' },
    'index.next_refresh': { en: 'next refresh', zh: '下次刷新' },
    'index.sector_overview': { en: 'Sector Overview', zh: '板块概览' },

    'index.valuation_map':      { en: 'Valuation Map', zh: '估值地图' },
    'index.valuation_map_desc': { en: 'Forward PE vs Analyst Upside — top-left = cheap with upside',
                                   zh: '预测市盈率 vs 分析师上涨空间 — 左上角 = 低估且有上涨空间' },
    'index.hide_tickers':       { en: 'Hide tickers', zh: '隐藏股票' },
    'index.show_tickers':       { en: 'Show tickers', zh: '显示股票' },
    'index.hide_labels':        { en: 'Hide labels',  zh: '隐藏标签' },
    'index.show_labels':        { en: 'Show labels',  zh: '显示标签' },
    'index.hide_values':        { en: 'Hide values',  zh: '隐藏数值' },
    'index.show_values':        { en: 'Show values',  zh: '显示数值' },
    'index.expand':             { en: 'Expand', zh: '展开' },

    'index.peg_map':       { en: 'PEG Valuation Map', zh: 'PEG 估值地图' },
    'index.peg_under':     { en: '< 1 — undervalued', zh: '< 1 — 低估' },
    'index.peg_moderate':  { en: '1–2 — moderate',    zh: '1–2 — 合理' },
    'index.peg_expensive': { en: '> 2 — expensive',   zh: '> 2 — 高估' },

    'index.heatmap':       { en: 'PE & PEG Heatmap',         zh: '市盈率 & PEG 热力图' },
    'index.heatmap_desc':  { en: 'Colour intensity = ratio magnitude', zh: '颜色深浅代表比率大小' },
    'index.etf_heatmap':      { en: 'ETF PE Heatmap',     zh: 'ETF 市盈率热力图' },
    'index.etf_heatmap_desc': { en: 'PE ratios for ETFs — PEG and growth metrics are not applicable',
                                 zh: 'ETF 市盈率 — PEG 及增长指标不适用' },
    'index.day_chg_sort':  { en: 'Day Chg',    zh: '日涨跌' },
    'index.heat_low':      { en: 'Low',     zh: '低' },
    'index.heat_high':     { en: 'High PE', zh: '高市盈率' },
    'index.all_sectors':   { en: 'All sectors', zh: '全部板块' },
    'index.search':        { en: 'Search…',     zh: '搜索…' },
    'index.search_ph':     { en: 'Search…',     zh: '搜索股票或名称…' },
    'index.no_results':    { en: 'No results',  zh: '无结果' },
    'index.analyst_upside': { en: 'analyst upside %', zh: '分析师上涨空间 %' },
    'index.fwd_pe':         { en: 'forward PE',        zh: '预测市盈率' },

    'th.ticker':    { en: 'Ticker',       zh: '代码' },
    'th.name':      { en: 'Name',         zh: '名称' },
    'th.sector':    { en: 'Sector',       zh: '板块' },
    'th.pe_ttm':    { en: 'PE (TTM)',     zh: '市盈率(TTM)' },
    'th.pe_fwd':    { en: 'PE (Fwd)',     zh: '市盈率(预测)' },
    'th.peg':       { en: 'PEG',          zh: 'PEG' },
    'th.upside':    { en: 'Upside',       zh: '上涨空间' },
    'th.mkt_cap':   { en: 'Mkt Cap',      zh: '市值' },
    'th.price':     { en: 'Price',        zh: '股价' },
    'th.target':    { en: 'Target',       zh: '目标价' },
    'th.day_chg':       { en: 'Day Chg',      zh: '日涨跌' },
    'th.eps_growth_ttm':{ en: 'EPS Gr TTM',   zh: 'EPS增长TTM' },
    'th.eps_growth_q':  { en: 'EPS Gr Q',     zh: 'EPS增长Q' },

    // ── sector.html ────────────────────────────────────────────────────
    'sector.dashboard':     { en: '← Dashboard',  zh: '← 仪表盘' },
    'sector.subtitle':      { en: 'Valuation analysis for this peer group', zh: '本同类组估值分析' },
    'sector.tab_day_change':{ en: 'Day Change',   zh: '日涨跌' },
    'sector.tab_pe':        { en: 'PE & PEG',     zh: '市盈率 & PEG' },
    'sector.tab_prices':    { en: 'Prices',        zh: '价格' },
    'sector.tab_upside':    { en: 'Upside',        zh: '上涨空间' },
    'sector.tab_peg':       { en: 'PEG Map',       zh: 'PEG 地图' },
    'sector.tab_growth':    { en: 'Growth',        zh: '增长' },
    'sector.tab_table':     { en: 'Data Table',    zh: '数据表格' },

    'sector.day_change_title': { en: 'Last Close — Day Change', zh: '上次收盘 — 日涨跌' },
    'sector.day_change_desc':  { en: 'Percentage change from previous close. Green = up, red = down.',
                                  zh: '相较前日收盘的涨跌幅。绿色=上涨，红色=下跌。' },
    'sector.growth_title':  { en: 'EPS Growth', zh: 'EPS 增长' },
    'sector.growth_desc':   { en: 'Year-over-year earnings growth — TTM (trailing 12 months) and most recent quarter.',
                               zh: '同比盈利增长 — 近12个月（TTM）及最近一季度。' },

    'sector.pe_title':      { en: 'PE Ratios (TTM & Forward) + PEG', zh: '市盈率（TTM 及预测）+ PEG' },
    'sector.pe_desc':       { en: 'Left axis: PE ratios. Right axis (green bars): PEG ratio. Dashed line = PEG 1.0.',
                               zh: '左轴：市盈率。右轴（绿色柱）：PEG。虚线 = PEG 1.0。' },
    'sector.sort':          { en: 'Sort',    zh: '排序' },
    'sector.hide_values':   { en: 'Hide values', zh: '隐藏数值' },
    'sector.show_values':   { en: 'Show values', zh: '显示数值' },

    'sector.prices_title':  { en: 'Current Price vs Analyst 12-month Target', zh: '当前股价 vs 分析师12个月目标价' },
    'sector.prices_desc':   { en: 'Bars = current price. Diamond markers = analyst consensus target.',
                               zh: '柱形 = 当前价格。菱形标记 = 分析师一致目标价。' },
    'sector.sort_az':       { en: 'A→Z', zh: 'A→Z' },
    'sector.sort_za':       { en: 'Z→A', zh: 'Z→A' },

    'sector.upside_title':  { en: 'Implied Analyst Upside (%)', zh: '分析师隐含上涨空间 (%)' },
    'sector.upside_desc':   { en: 'Implied return if stock reaches the analyst consensus 12-month target.',
                               zh: '若股价达到分析师一致目标价的隐含回报率。' },
    'sector.sort_lohi':     { en: 'Lo→Hi', zh: '低→高' },
    'sector.sort_hilo':     { en: 'Hi→Lo', zh: '高→低' },

    'sector.peg_title':     { en: 'PEG Valuation Map', zh: 'PEG 估值地图' },
    'sector.peg_desc':      { en: 'PEG = PE ÷ EPS growth rate.', zh: 'PEG = 市盈率 ÷ EPS 增长率。' },
    'sector.peg_under':     { en: '<1 undervalued', zh: '<1 低估' },
    'sector.peg_moderate':  { en: '1-2 moderate',  zh: '1-2 合理' },
    'sector.peg_expensive': { en: '>2 expensive',  zh: '>2 高估' },
    'sector.hide_tickers':  { en: 'Hide tickers',  zh: '隐藏股票' },
    'sector.show_tickers':  { en: 'Show tickers',  zh: '显示股票' },

    'sector.table_title':   { en: 'Data Table', zh: '数据表格' },

    // ── history.html ───────────────────────────────────────────────────
    'history.back':         { en: '← Back', zh: '← 返回' },
    'history.subtitle':     { en: '1-year historical PE ratio & price (weekly)', zh: '1年历史市盈率及价格（周频）' },

    'history.current_pe':   { en: 'Current PE',     zh: '当前市盈率' },
    'history.fwd_pe':       { en: 'Forward PE',     zh: '预测市盈率' },
    'history.peg_ratio':    { en: 'PEG Ratio',      zh: 'PEG 比率' },
    'history.target_price': { en: 'Target Price',   zh: '目标价' },
    'history.ttm_eps':      { en: 'TTM EPS',        zh: '近12月EPS' },
    'history.upside':       { en: 'Analyst Upside', zh: '分析师上涨空间' },
    'history.eps_growth_ttm': { en: 'EPS Growth TTM', zh: 'EPS增长TTM' },
    'history.eps_growth_q':   { en: 'EPS Growth Q',   zh: 'EPS增长Q' },
    'history.tradingview':    { en: 'TradingView',     zh: 'TradingView' },

    'history.pe_title':     { en: 'PE Ratio — 52 Weeks', zh: '市盈率 — 52周' },
    'history.pe_desc':      { en: 'Estimated from weekly closing price ÷ TTM EPS', zh: '由每周收盘价 ÷ 近12月EPS 估算' },
    'history.pe_legend':    { en: 'PE (TTM)',    zh: '市盈率(TTM)' },
    'history.current_pe_legend': { en: 'Current PE', zh: '当前市盈率' },

    'history.price_title':  { en: 'Price — 52 Weeks',     zh: '股价 — 52周' },
    'history.price_desc':   { en: 'Weekly closing price (USD)', zh: '每周收盘价（美元）' },

    'history.peg_title':    { en: 'PEG Ratio — 52 Weeks', zh: 'PEG 比率 — 52周' },
    'history.peg_desc':     { en: 'Estimated from weekly PE ÷ annual earnings growth rate',
                               zh: '由每周市盈率 ÷ 年度盈利增长率估算' },
    'history.peg_legend':   { en: 'PEG',    zh: 'PEG' },
    'history.peg1_legend':  { en: 'PEG = 1', zh: 'PEG = 1' },
    'history.loading':      { en: 'Loading PE history…', zh: '加载市盈率历史数据…' },
    'history.failed':       { en: 'Failed to load data.', zh: '数据加载失败。' },

    // ── lookup.html ────────────────────────────────────────────────────
    'lookup.title':         { en: 'Ticker Lookup', zh: '股票查询' },
    'lookup.subtitle':      { en: 'Search any stock symbol for instant valuation metrics, or discover tickers by sector.',
                               zh: '搜索任意股票代码获取估值指标，或按板块发现股票。' },
    'lookup.search_title':  { en: 'Search a Ticker',  zh: '搜索股票代码' },
    'lookup.search_ph':     { en: 'e.g. AAPL, WMT, TSM …', zh: '例如 AAPL、WMT、TSM …' },
    'lookup.look_up':       { en: 'Look up',  zh: '查询' },
    'lookup.discover_title':{ en: 'Discover by Sector / Industry', zh: '按板块/行业发现' },
    'lookup.discover_desc': { en: 'Browse top companies from Yahoo Finance (or built-in list). Click a chip to look it up.',
                               zh: '浏览 Yahoo Finance 或内置列表的主要公司。点击标签即可查询。' },
    'lookup.sector':        { en: 'Sector',   zh: '板块' },
    'lookup.industry':      { en: 'Industry', zh: '行业' },
    'lookup.discover_ph':   { en: 'technology, semiconductors, biotech …', zh: '科技、半导体、生物技术…' },
    'lookup.discover_btn':  { en: 'Discover', zh: '发现' },

    'lookup.market_cap':    { en: 'Market Cap',   zh: '市值' },
    'lookup.target_price':  { en: 'Target Price', zh: '目标价' },
    'lookup.pe_ttm':        { en: 'PE (TTM)',      zh: '市盈率(TTM)' },
    'lookup.pe_fwd':        { en: 'PE (Forward)',  zh: '市盈率(预测)' },
    'lookup.peg_ratio':     { en: 'PEG Ratio',     zh: 'PEG 比率' },
    'lookup.pe_comparison': { en: 'PE comparison', zh: '市盈率对比' },
    'lookup.add_to_group':  { en: 'Add to group:', zh: '添加到分组：' },
    'lookup.add_btn':       { en: '+ Add',         zh: '+ 添加' },
    'lookup.upside':        { en: 'upside',         zh: '上涨空间' },
    'lookup.cheap':         { en: '✓ cheap', zh: '✓ 低估' },
    'lookup.fair':          { en: ' fair',   zh: ' 合理' },
    'lookup.rich':          { en: ' rich',   zh: ' 高估' },

    // ── groups.html ────────────────────────────────────────────────────
    'groups.title':         { en: 'Manage Peer Groups', zh: '管理同类组' },
    'groups.subtitle':      { en: 'Add or remove sectors and tickers. Changes clear the cache automatically — the next page load re-fetches updated data from Yahoo Finance.',
                               zh: '添加或删除板块和股票。更改将自动清除缓存 — 下次页面加载时将从 Yahoo Finance 重新获取数据。' },
    'groups.delete':        { en: '✕ Delete',     zh: '✕ 删除' },
    'groups.no_tickers':    { en: 'No tickers yet', zh: '暂无股票' },
    'groups.add_btn':       { en: '+ Add',          zh: '+ 添加' },
    'groups.ticker_ph':     { en: 'e.g. TSLA',      zh: '例如 TSLA' },
    'groups.new_group':     { en: 'Create new group', zh: '创建新分组' },
    'groups.group_ph':      { en: 'e.g. Biotech',    zh: '例如 生物科技' },
    'groups.create_btn':    { en: 'Create',           zh: '创建' },
    'groups.hint':          { en: 'Then use + Add inside the card to add tickers.', zh: '然后在卡片中使用 + 添加 来添加股票。' },

    // ── contact.html ───────────────────────────────────────────────────
    'contact.title':        { en: 'Contact Us',  zh: '联系我们' },
    'contact.subtitle':     { en: 'Have a question, suggestion, or found a bug? We\'d love to hear from you.',
                               zh: '有问题、建议或发现了 Bug？欢迎联系我们。' },
    'contact.or':           { en: 'Or send a message directly:', zh: '或直接发送消息：' },
    'contact.subject':      { en: 'Subject',  zh: '主题' },
    'contact.subject_ph':   { en: 'e.g. Bug report, feature request…', zh: '例如 Bug 反馈、功能建议…' },
    'contact.message':      { en: 'Message',  zh: '消息内容' },
    'contact.message_ph':   { en: 'Describe your question or feedback…', zh: '描述您的问题或反馈…' },
    'contact.send_btn':     { en: 'Open in mail app', zh: '在邮件应用中打开' },

    // ── error.html ─────────────────────────────────────────────────────
    'error.title':          { en: 'Something went wrong', zh: '出了点问题' },
    'error.hint1':          { en: 'This usually means Yahoo Finance could not be reached.', zh: '通常是无法访问 Yahoo Finance。' },
    'error.hint2':          { en: 'Check your internet connection then try refreshing.', zh: '请检查网络连接后刷新重试。' },
    'error.retry':          { en: 'Retry', zh: '重试' },

    // ── warming.html ───────────────────────────────────────────────────
    'warming.title':        { en: 'Warming up the cache…', zh: '正在预热缓存…' },
    'warming.desc':         { en: 'Fetching live data from Yahoo Finance for all tickers.', zh: '正在从 Yahoo Finance 获取所有股票的实时数据。' },
    'warming.note':         { en: 'This only happens on first load — the page will refresh automatically.', zh: '仅首次加载时出现 — 页面将自动刷新。' },
    'warming.checking':     { en: 'Checking again in', zh: '将在' },
    'warming.seconds':      { en: 's…', zh: '秒后重新检查…' },

    // ── modal (index.html) ─────────────────────────────────────────────
    'modal.peg_under':      { en: 'PEG < 1 — undervalued', zh: 'PEG < 1 — 低估' },
    'modal.peg_moderate':   { en: '1–2 — moderate',        zh: '1–2 — 合理' },
    'modal.peg_expensive':  { en: '> 2 — expensive',       zh: '> 2 — 高估' },
  };

  let current = localStorage.getItem('ystocker_lang') || 'en';

  function t(key) {
    const entry = LANGS[key];
    if (!entry) return null;
    return entry[current] ?? entry['en'];
  }

  function apply(root) {
    root = root || document;
    // textContent
    root.querySelectorAll('[data-i18n]').forEach(el => {
      const v = t(el.dataset.i18n);
      if (v != null) el.textContent = v;
    });
    // placeholder
    root.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const v = t(el.dataset.i18nPlaceholder);
      if (v != null) el.placeholder = v;
    });
    // title attribute
    root.querySelectorAll('[data-i18n-title]').forEach(el => {
      const v = t(el.dataset.i18nTitle);
      if (v != null) el.title = v;
    });
    // html (for elements that contain inline markup)
    root.querySelectorAll('[data-i18n-html]').forEach(el => {
      const v = t(el.dataset.i18nHtml);
      if (v != null) el.innerHTML = v;
    });
  }

  function setLang(lang) {
    current = lang;
    localStorage.setItem('ystocker_lang', lang);
    apply();
    // Update toggle button text
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = lang === 'zh' ? 'EN' : '中文';
    });
  }

  function toggle() {
    setLang(current === 'en' ? 'zh' : 'en');
  }

  function getLang() { return current; }

  // Auto-apply on DOMContentLoaded
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = current === 'zh' ? 'EN' : '中文';
    });
  });

  return { t, apply, toggle, setLang, getLang };
})();
