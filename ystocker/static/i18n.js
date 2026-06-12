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
    'nav.home':           { en: 'Valuation', zh: '估值' },
    'nav.sectors':        { en: 'Sectors',   zh: '板块' },
    'nav.peer_groups':    { en: 'Peer Groups', zh: '同类组' },
    'nav.view_all':       { en: 'View all sectors ↓', zh: '查看全部板块 ↓' },
    'nav.commodities':    { en: 'Commodities', zh: '大宗商品' },
    'nav.commodities_futures': { en: 'Futures', zh: '期货合约' },
    'nav.lookup':         { en: 'Lookup',   zh: '查询' },
    'nav.thirteenf':      { en: '13F',      zh: '13F' },
    'nav.fed':            { en: 'Fed',      zh: '美联储' },
    'nav.groups':         { en: 'Groups',   zh: '分组' },
    'nav.daily':          { en: 'Daily',    zh: '每日报告' },
    'nav.contact':        { en: 'Contact',  zh: '联系' },
    'nav.guide':          { en: 'Guide',    zh: '使用指南' },
    'nav.videos':         { en: 'Videos',   zh: '视频' },
    'nav.search_ticker':  { en: 'Search Ticker', zh: '搜索股票' },
    'nav.sign_in':        { en: 'Sign In',  zh: '登录' },
    'nav.logout':         { en: 'Logout',   zh: '退出登录' },
    'nav.refresh':        { en: '↻ Refresh', zh: '↻ 刷新' },
    'nav.refresh_title':  { en: 'Refresh data', zh: '刷新数据' },
    'nav.refresh_body':   { en: 'Clears the in-memory cache and re-fetches live prices, PE ratios, and analyst targets for all tickers from Yahoo Finance.',
                            zh: '清除内存缓存，从 Yahoo Finance 重新获取所有股票的最新价格、市盈率及分析师目标价。' },
    'footer.text':        { en: 'yStocker — data via Yahoo Finance', zh: 'yStocker — 数据来源：Yahoo Finance' },
    'footer.contact':     { en: 'Contact Us', zh: '联系我们' },
    'footer.copyright':   { en: '© 2025 yStocker. All rights reserved.', zh: '© 2025 yStocker. 保留所有权利。' },

    // ── login.html ─────────────────────────────────────────────────────
    'login.subtitle':         { en: 'Sign in to personalize your stock dashboard',
                                 zh: '登录以定制您的股票仪表盘' },
    'login.or':               { en: 'or', zh: '或' },
    'login.guest':            { en: 'Continue as Guest', zh: '以访客身份继续' },
    'login.not_configured':   { en: 'Google Sign-In is not configured. Set GOOGLE_CLIENT_ID in your .env file to enable login.',
                                 zh: 'Google 登录尚未配置。请在 .env 文件中设置 GOOGLE_CLIENT_ID 以启用登录功能。' },
    'login.why_sign_in':      { en: 'Why sign in?', zh: '登录后可享受' },
    'login.feature_watchlist':{ en: 'Save tickers to a personal watchlist',
                                 zh: '将股票加入个人自选清单' },
    'login.feature_daily':    { en: 'Get the daily report delivered by email',
                                 zh: '每日报告发送至您的邮箱' },
    'login.feature_alerts':   { en: 'Set price & valuation alerts (coming soon)',
                                 zh: '设置价格与估值提醒（即将推出）' },
    'login.error':            { en: 'Login failed. Please try again.',
                                 zh: '登录失败，请重试。' },

    // ── index.html ─────────────────────────────────────────────────────
    'index.hero_title':   { en: 'Stock Valuation', zh: '股票估值' },
    'index.hero_sub':     { en: 'Dashboard',       zh: '仪表盘' },
    'index.hero_desc':    { en: 'PE ratios, PEG ratios & analyst targets across', zh: '市盈率、PEG 及分析师目标价，覆盖' },
    'index.peer_groups':  { en: 'peer groups.', zh: '个同类组。' },
    'index.data_as_of':   { en: 'Data as of', zh: '数据截至' },
    'index.refreshing':   { en: 'refreshing now…', zh: '正在刷新…' },
    'index.next_refresh': { en: 'next refresh', zh: '下次刷新' },
    'index.sector_overview': { en: 'Sector Overview', zh: '板块概览' },
    'index.jump_stock_pe':  { en: '↓ Stock PE',  zh: '↓ 个股市盈率' },
    'index.jump_etf_pe':    { en: '↓ ETF PE',    zh: '↓ ETF市盈率' },
    'index.jump_sectors':   { en: '↓ Sectors',   zh: '↓ 板块概览' },

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
    'th.ev_ebitda':     { en: 'EV/EBITDA',    zh: '企业价值倍数' },
    'th.ev':            { en: 'EV ($B)',       zh: '企业价值（十亿美元）' },
    'th.ebitda':        { en: 'EBITDA ($B)',   zh: 'EBITDA（十亿美元）' },

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
    'sector.tab_ev_ebitda': { en: 'EV/EBITDA',     zh: '企业价值倍数' },
    'sector.ev_ebitda_title': { en: 'EV/EBITDA', zh: '企业价值倍数 (EV/EBITDA)' },
    'sector.ev_ebitda_desc':  { en: 'Enterprise Value ÷ EBITDA — lower = cheaper relative to operating earnings (capital-structure neutral).',
                                 zh: '企业价值 ÷ 息税折旧摊销前利润 — 越低说明相对运营盈利越便宜（不受资本结构影响）。' },
    'sector.ev_title':        { en: 'Enterprise Value ($B)', zh: '企业价值（十亿美元）' },
    'sector.ev_desc':         { en: 'Total enterprise value in billions USD.', zh: '企业总价值，单位：十亿美元。' },
    'sector.ebitda_title':    { en: 'EBITDA ($B)', zh: '息税折旧摊销前利润（十亿美元）' },
    'sector.ebitda_desc':     { en: 'Earnings before interest, taxes, depreciation & amortisation, in billions USD.',
                                 zh: '息税折旧摊销前利润，单位：十亿美元。' },

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
    'history.subtitle':     { en: 'Historical PE ratio & price', zh: '历史市盈率及价格' },

    'history.current_pe':   { en: 'Current PE',     zh: '当前市盈率' },
    'history.fwd_pe':       { en: 'Forward PE',     zh: '预测市盈率' },
    'history.peg_ratio':    { en: 'PEG Ratio',      zh: 'PEG 比率' },
    'history.target_price': { en: 'Target Price',   zh: '目标价' },
    'history.ttm_eps':      { en: 'TTM EPS',        zh: '近12月EPS' },
    'history.upside':       { en: 'Analyst Upside', zh: '分析师上涨空间' },
    'history.eps_growth_ttm': { en: 'EPS Growth TTM', zh: 'EPS增长TTM' },
    'history.eps_growth_q':   { en: 'EPS Growth Q',   zh: 'EPS增长Q' },
    'history.ev_ebitda':      { en: 'EV/EBITDA',      zh: '企业价值倍数' },
    'history.ev_ebitda_title':{ en: 'EV / EBITDA',    zh: '企业价值倍数 (EV/EBITDA)' },
    'history.ev_ebitda_desc': { en: 'Enterprise Value ÷ EBITDA — capital-structure-neutral valuation multiple.',
                                 zh: '企业价值 ÷ 息税折旧摊销前利润 — 不受资本结构影响的估值倍数。' },
    'history.ev':             { en: 'EV ($B)',         zh: '企业价值（十亿美元）' },
    'history.ebitda':         { en: 'EBITDA ($B)',     zh: 'EBITDA（十亿美元）' },
    'history.put_call_ratio': { en: 'P/C Ratio',       zh: '认沽/认购比率' },
    'history.pc_title':       { en: 'Put/Call Ratio by Expiration', zh: '到期日认沽/认购比率' },
    'history.pc_desc':        { en: 'Put OI ÷ Call OI per expiration — above 1.0 = bearish hedging, below 0.7 = bullish', zh: '各到期日认沽持仓量 ÷ 认购持仓量，高于1.0为看空对冲，低于0.7为看多' },
    'history.pc_bearish':     { en: 'Bearish (>1.2)',  zh: '看空 (>1.2)' },
    'history.pc_neutral':     { en: 'Neutral',         zh: '中性' },
    'history.pc_bullish':     { en: 'Bullish (<0.7)',  zh: '看多 (<0.7)' },
    'history.pc_chart_title': { en: 'Put/Call Ratio by Expiration', zh: '各到期日认沽/认购比率' },
    'history.pc_chart_desc':  { en: 'Total put OI ÷ call OI per expiry — >1.2 bearish · <0.7 bullish', zh: '各到期日总认沽持仓量÷总认购持仓量 — >1.2看空 · <0.7看多' },
    'history.financials_title': { en: 'Annual Financials', zh: '年度财务数据' },
    'history.financials_desc':  { en: '3-year actuals · 2-year forward estimates · values in $B except EPS', zh: '近3年实际数据 · 未来2年预测 · 单位十亿美元（EPS除外）' },
    'history.group_valuation':{ en: 'Valuation',       zh: '估值' },
    'history.group_earnings': { en: 'Earnings',        zh: '盈利' },
    'history.group_sentiment':{ en: 'Price & Sentiment', zh: '价格与情绪' },
    'history.tradingview':    { en: 'TradingView',     zh: 'TradingView' },

    'history.pe_title':     { en: 'PE Ratio — 52 Weeks', zh: '市盈率 — 52周' },
    'history.pe_desc':      { en: 'Estimated from weekly closing price ÷ TTM EPS', zh: '由每周收盘价 ÷ 近12月EPS 估算' },
    'history.pe_legend':    { en: 'PE (TTM)',    zh: '市盈率(TTM)' },
    'history.current_pe_legend': { en: 'Current PE', zh: '当前市盈率' },
    'history.avg_pe_legend':     { en: 'Avg PE',     zh: '平均市盈率' },

    'history.fwdpe_title':  { en: 'Forward PE — 52 Weeks', zh: '预测市盈率 — 52周' },
    'history.fwdpe_desc':   { en: 'Price ÷ consensus forward EPS — forward valuation multiple over time', zh: '股价 ÷ 共识预测EPS — 预测估值倍数随时间变化' },
    'history.fwdpe_legend': { en: 'Forward PE', zh: '预测市盈率' },
    'history.current_fwdpe_legend': { en: 'Current Fwd PE', zh: '当前预测市盈率' },

    'history.price_title':  { en: 'Price — 52 Weeks',     zh: '股价 — 52周' },
    'history.price_desc':   { en: 'Weekly closing price (USD)', zh: '每周收盘价（美元）' },
    'history.price_legend': { en: 'Price',  zh: '股价' },
    'history.delta_title':  { en: 'Price Change (Δ)', zh: '价格变动 (Δ)' },
    'history.delta_desc':   { en: 'Period-over-period price delta (USD)', zh: '逐期价格变动（美元）' },
    'history.delta_up':     { en: 'Up',   zh: '上涨' },
    'history.delta_down':   { en: 'Down', zh: '下跌' },
    'history.delta_ma':     { en: 'MA',   zh: '均线' },
    'history.target_legend':   { en: 'Target',    zh: '目标价' },
    'history.call_wall_legend':{ en: 'Call Wall', zh: '认购墙' },
    'history.put_wall_legend': { en: 'Put Wall',  zh: '认沽墙' },

    'history.tip_target':    { en: 'Analyst consensus 12-month price target from covering analysts.',
                                zh: '分析师对未来12个月股价的一致预测目标价。' },
    'history.tip_call_wall': { en: 'Call Wall — the option strike with the highest total call open interest across all expirations. Acts as overhead resistance: market makers delta-hedge by selling shares as the price rises toward this level, capping near-term upside.',
                                zh: '认购墙 — 所有到期日中总认购未平仓量最高的行权价。作为上方阻力位：当股价接近该水平时，做市商通过卖出股票进行Delta对冲，压制短期上涨空间。' },
    'history.tip_put_wall':  { en: 'Put Wall — the option strike with the highest total put open interest across all expirations. Acts as a price floor: market makers delta-hedge by buying shares as the price falls toward this level, providing near-term support.',
                                zh: '认沽墙 — 所有到期日中总认沽未平仓量最高的行权价。作为价格支撑位：当股价接近该水平时，做市商通过买入股票进行Delta对冲，提供短期支撑。' },

    'history.peg_title':    { en: 'PEG Ratio — 52 Weeks', zh: 'PEG 比率 — 52周' },
    'history.peg_desc':     { en: 'Estimated from weekly PE ÷ annual earnings growth rate',
                               zh: '由每周市盈率 ÷ 年度盈利增长率估算' },
    'history.peg_legend':   { en: 'PEG',    zh: 'PEG' },
    'history.peg1_legend':  { en: 'PEG = 1', zh: 'PEG = 1' },
    'history.loading':      { en: 'Loading PE history…', zh: '加载市盈率历史数据…' },
    'history.failed':       { en: 'Failed to load data.', zh: '数据加载失败。' },

    'history.inst_title':   { en: 'Institutional Holdings (13F)', zh: '机构持仓（13F）' },
    'history.inst_desc':    { en: 'Multi-quarter position history across tracked funds — sorted by latest position size',
                               zh: '跨季度持仓历史，覆盖已跟踪机构基金，按最新持仓规模排序' },
    'history.inst_fund':    { en: 'Fund',             zh: '基金' },
    'history.inst_rank':    { en: 'Portfolio Rank',   zh: '持仓排名' },
    'history.inst_shares':  { en: 'Shares',           zh: '股数' },
    'history.inst_value':   { en: 'Value',            zh: '市值' },
    'history.inst_pct':     { en: '% of Portfolio',   zh: '占基金比例' },
    'history.inst_change':  { en: 'Change',           zh: '变化' },

    'history.tab_charts':   { en: 'Charts & Holdings', zh: '图表与持仓' },
    'history.tab_news':     { en: 'News',               zh: '新闻' },
    'history.tab_videos':   { en: 'Videos',             zh: '视频' },
    'history.news_translating': { en: 'Translating headlines…', zh: '正在翻译标题…' },
    'history.news_translated':  { en: 'Translated by AI', zh: 'AI 翻译' },
    'history.news_trans_fail':  { en: 'Translation unavailable', zh: '翻译不可用' },

    'history.news_loading': { en: 'Fetching news…',    zh: '加载新闻…' },
    'history.news_empty':   { en: 'No news found.',    zh: '暂无新闻。' },
    'history.news_error':   { en: 'Failed to load news.', zh: '新闻加载失败。' },
    'history.news_refresh':  { en: '↻ Refresh',         zh: '↻ 刷新' },
    'history.news_timeline': { en: 'Timeline',           zh: '时间轴' },
    'history.news_important': { en: 'Important',       zh: '重要' },
    'history.news_just_now':  { en: 'just now',        zh: '刚刚' },
    'history.news_min_ago':   { en: 'm ago',           zh: '分钟前' },
    'history.news_hr_ago':    { en: 'h ago',           zh: '小时前' },
    'history.news_day_ago':   { en: 'd ago',           zh: '天前' },
    'history.videos_loading': { en: 'Fetching videos…', zh: '加载视频…' },
    'history.videos_empty':   { en: 'No videos found.', zh: '暂无视频。' },
    'history.videos_error':   { en: 'Failed to load videos.', zh: '视频加载失败。' },
    'history.videos_open_yt': { en: 'Open on YouTube ↗', zh: '在 YouTube 打开 ↗' },

    'history.explain':        { en: '✦ Explain',        zh: '✦ AI 解读' },
    'history.explain_hide':   { en: '✦ Hide',           zh: '✦ 收起' },
    'history.explain_thinking':{ en: '✦ Generating…',  zh: '✦ 生成中…' },

    // Price Z-Score (history.html)
    'history.zscore':              { en: 'Z-Score',              zh: 'Z分数' },
    'history.zscore_title':        { en: 'Price Z-Score',        zh: '价格Z分数' },
    'history.zscore_desc':         { en: 'How far the current price is from the 52-week average, measured in standard deviations',
                                      zh: '当前价格偏离52周均值的标准差数' },
    'history.zscore_high':         { en: '>2σ Overbought',       zh: '>2σ 超买' },
    'history.zscore_elevated':     { en: '>1σ Above Mean',       zh: '>1σ 高于均值' },
    'history.zscore_normal':       { en: '-1σ to 1σ Normal',     zh: '-1σ到1σ 正常' },
    'history.zscore_depressed':    { en: '<-1σ Below Mean',      zh: '<-1σ 低于均值' },
    'history.zscore_low':          { en: '<-2σ Oversold',        zh: '<-2σ 超卖' },
    'history.zscore_current':      { en: 'Current Price',        zh: '当前价格' },
    'history.zscore_mean':         { en: '52w Mean',             zh: '52周均值' },
    'history.zscore_stdev':        { en: '52w Std Dev',          zh: '52周标准差' },
    'history.zscore_range':        { en: '52w Range',            zh: '52周范围' },

    // ── 13F page ───────────────────────────────────────────────────────
    'thirteenf.title':       { en: '13F Institutional Holdings', zh: '13F 机构持仓' },
    'thirteenf.subtitle':    { en: 'Latest quarterly 13F-HR filings from SEC EDGAR · Top 50 equity positions per fund',
                                zh: 'SEC EDGAR 最新季度 13F-HR 文件 · 每支基金前50大权益持仓' },
    'thirteenf.data_as_of':  { en: 'Data as of', zh: '数据截至' },
    'thirteenf.next_refresh':{ en: 'next refresh', zh: '下次刷新' },
    'thirteenf.refreshing':  { en: 'refreshing now…', zh: '正在刷新…' },
    'thirteenf.refresh':     { en: '↻ Refresh', zh: '↻ 刷新' },
    'thirteenf.load':        { en: '↻ Load holdings', zh: '↻ 加载持仓' },
    'thirteenf.loading':     { en: 'Fetching holdings from SEC EDGAR — this may take up to 5 minutes on first load…',
                                zh: '正在从 SEC EDGAR 获取持仓数据，首次加载最长可能需要约5分钟…' },
    'thirteenf.no_data':     { en: 'No data yet.', zh: '暂无数据。' },
    'thirteenf.fetch_failed':{ en: 'Failed to fetch:', zh: '获取失败：' },
    'thirteenf.retry':       { en: '↻ Retry', zh: '↻ 重试' },
    'thirteenf.no_data':        { en: 'No data yet.',     zh: '暂无数据。' },
    'thirteenf.refresh_to_load':{ en: 'Refresh to load',  zh: '点击刷新加载' },
    'thirteenf.retry':          { en: '↻ Retry',          zh: '↻ 重试' },
    'thirteenf.cik':         { en: 'CIK:', zh: 'CIK：' },
    'thirteenf.filing_date': { en: 'Filing date:', zh: '申报日期：' },
    'thirteenf.period':      { en: 'Period:', zh: '报告期：' },
    'thirteenf.aum':         { en: 'Reported AUM:', zh: '披露AUM：' },
    'thirteenf.shown':       { en: 'Holdings shown:', zh: '显示持仓：' },
    'thirteenf.th_rank':     { en: '#',                zh: '#' },
    'thirteenf.th_ticker':   { en: 'Ticker',          zh: '代码' },
    'thirteenf.th_company':  { en: 'Company',         zh: '公司' },
    'thirteenf.th_52w':      { en: '52-week',         zh: '52周' },
    'thirteenf.th_shares':   { en: 'Shares',          zh: '股数' },
    'thirteenf.th_value':    { en: 'Value ($M)',       zh: '市值（百万$）' },
    'thirteenf.th_pct':      { en: '% Portfolio',     zh: '占比' },
    'thirteenf.th_change':   { en: 'Change',          zh: '变化' },
    'thirteenf.legend_new':  { en: 'New position this quarter',            zh: '本季度新开仓' },
    'thirteenf.legend_added':{ en: 'Shares increased vs prior quarter',    zh: '较上季度加仓' },
    'thirteenf.legend_reduced':{ en: 'Shares decreased vs prior quarter',  zh: '较上季度减仓' },
    'thirteenf.legend_closed':{ en: 'Position exited (prior quarter only)', zh: '已清仓（仅上季度持有）' },
    'thirteenf.source':      { en: 'Data source:', zh: '数据来源：' },
    'thirteenf.shown_value': { en: 'Shown value:', zh: '显示持仓总值：' },
    'thirteenf.total_shown': { en: 'Total (shown)', zh: '合计（已显示）' },
    'thirteenf.aum_history': { en: 'AUM by Quarter', zh: '按季度持仓规模' },
    'thirteenf.explain_aum': { en: 'AI Explain',      zh: 'AI 解读' },
    'thirteenf.analyzing':   { en: 'Analyzing AUM trend…', zh: '正在分析持仓规模趋势…' },
    'thirteenf.ai_analysis': { en: 'AI Analysis',     zh: 'AI 分析' },

    // ── change badge labels ────────────────────────────────────────────
    'inst.change_new':     { en: 'New',       zh: '新建' },
    'inst.change_added':   { en: '▲ Added',   zh: '▲ 加仓' },
    'inst.change_reduced': { en: '▼ Reduced', zh: '▼ 减仓' },
    'inst.change_closed':  { en: 'Closed',    zh: '清仓' },

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
    'lookup.ev_ebitda':     { en: 'EV/EBITDA',     zh: '企业价值倍数' },
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
    'groups.persist_note':  { en: 'Note: changes are not persisted and will reset to defaults if the server restarts.',
                               zh: '注意：更改不会持久化，服务器重启后将恢复默认设置。' },

    // ── fed.html ───────────────────────────────────────────────────────
    'fed.title':           { en: 'Fed Balance Sheet (H.4.1)', zh: '美联储资产负债表 (H.4.1)' },
    'fed.subtitle':        { en: 'Federal Reserve weekly balance-sheet data · assets, liabilities, reserve balances, reverse repos & more',
                              zh: '美联储每周资产负债表数据 · 资产、负债、存款准备金、逆回购等' },
    'fed.data_as_of':      { en: 'Data as of',      zh: '数据截至' },
    'fed.next_refresh':    { en: 'next refresh',    zh: '下次刷新' },
    'fed.refreshing':      { en: 'refreshing now…', zh: '正在刷新…' },
    'fed.refresh':         { en: '↻ Refresh',       zh: '↻ 刷新' },
    'fed.load':            { en: '↻ Load data',     zh: '↻ 加载数据' },
    'fed.loading':         { en: 'Fetching data from Federal Reserve — this may take a moment…',
                              zh: '正在从美联储获取数据，请稍候…' },
    'fed.loading_hint':    { en: 'This page will refresh automatically when data is ready.',
                             zh: '数据准备就绪后页面将自动刷新。' },
    'fed.stat_total':      { en: 'Total Assets',        zh: '总资产' },
    'fed.stat_treasury':   { en: 'Treasury Securities', zh: '美国国债' },
    'fed.stat_bills':      { en: 'Bills (Short-Term)',  zh: '短期国库券' },
    'fed.stat_mbs':        { en: 'MBS Holdings',        zh: 'MBS持仓' },
    'fed.stat_reserves':   { en: 'Reserve Balances',    zh: '存款准备金' },
    'fed.stat_other_assets': { en: 'Other Assets',      zh: '其他资产' },
    'fed.tip_other_assets': { en: 'Repos, central bank liquidity swaps, premium on securities (QE-era amortization), foreign currency assets, gold/SDR/coin certificates, and other Federal Reserve assets. Computed as: Total Assets − Treasuries − MBS − Loans.',
                              zh: '回购协议、央行流动性互换、QE时期债券溢价摊销、外币资产、黄金/SDR/硬币凭证及其他美联储资产。计算公式：总资产 − 国债 − MBS − 贷款。' },
    'fed.as_of':           { en: 'As of', zh: '截至' },
    'fed.tip_treasury':    { en: 'U.S. Treasury notes and bonds held outright on the Fed\'s balance sheet as part of quantitative easing / tightening (QE/QT).',
                             zh: '美联储资产负债表上直接持有的美国国债（中长期），是量化宽松/收紧（QE/QT）的核心工具。' },
    'fed.tip_bills':       { en: 'Short-term U.S. Treasury bills (maturity ≤1 year) held outright. An increase often signals temporary liquidity operations.',
                             zh: '美联储持有的1年内到期短期国库券，增加通常反映临时流动性操作。' },
    'fed.tip_mbs':         { en: 'Mortgage-backed securities guaranteed by Fannie Mae, Freddie Mac, or Ginnie Mae. Purchased to support the housing market during QE.',
                             zh: '由房利美、房地美或吉利美担保的抵押贷款支持证券，QE期间购入以支撑房地产市场。' },
    'fed.tip_reserves':    { en: 'Deposits held by commercial banks at the Federal Reserve. High reserves indicate loose monetary conditions; draining reserves tightens liquidity.',
                             zh: '商业银行在美联储的存款准备金，余额高表示货币宽松；准备金减少意味着流动性收紧。' },
    'fed.treasury_title':  { en: 'U.S. Treasury Securities Held Outright',  zh: '持有的美国国债（直接持有）' },
    'fed.treasury_desc':   { en: 'Weekly, $ billions — Federal Reserve balance sheet', zh: '每周，十亿美元 — 美联储资产负债表' },
    'fed.bills_title':     { en: '↳ Treasury Bills (T-Bills, ≤1Y)', zh: '↳ 短期国库券（T-Bills，≤1年）' },
    'fed.bills_desc':      { en: 'Short-term T-bills maturing within 1 year — a subset of the Treasury holdings above', zh: '1年内到期的短期国库券 — 上方国债持仓的子集' },
    'fed.bills_loading':   { en: 'Loading Bills data…', zh: '加载短期国库券数据…' },
    'fed.wow_title':       { en: 'Week-over-Week Change ($B)', zh: '周环比变化（十亿美元）' },
    'fed.chart_treasuries': { en: 'Treasuries ($B)',     zh: '国债（十亿美元）' },
    'fed.chart_bills':     { en: 'Bills ($B)',            zh: '短期国库券（十亿美元）' },
    'fed.chart_wow':       { en: 'WoW Change ($B)',       zh: '周环比（十亿美元）' },
    'fed.balance_title':   { en: 'Balance Sheet Overview',    zh: '资产负债表概览' },
    'fed.balance_desc':    { en: 'Total assets, MBS, and reserve balances ($B) — weekly', zh: '总资产、MBS及存款准备金（十亿美元）— 每周' },
    'fed.pct_title':       { en: 'Treasury as % of Total Assets', zh: '国债占总资产比例' },
    'fed.pct_desc':        { en: 'How much of the Fed\'s balance sheet is US Treasuries — higher = more concentrated in government debt',
                              zh: '美联储资产负债表中美国国债的占比 — 越高说明政府债务集中度越高' },
    'fed.loading_chart':   { en: 'Loading data…', zh: '加载中…' },
    'fed.source_note':     { en: 'Data source:', zh: '数据来源：' },
    'fed.updated_weekly':  { en: 'Updated weekly (Thursdays)', zh: '每周四更新' },
    'fed.range_all':       { en: 'All', zh: '全部' },
    'fed.explain':         { en: '✦ Explain', zh: '✦ AI 解读' },
    'fed.stat_rrp':        { en: 'ON RRP',                    zh: '隔夜逆回购' },
    'fed.stat_tga':        { en: 'Treasury Gen. Account',     zh: '财政部一般账户' },
    'fed.stat_currency':   { en: 'Currency in Circulation',   zh: '流通中货币' },
    'fed.stat_loans':      { en: 'Fed Loans (incl. BTFP)',    zh: '美联储贷款（含BTFP）' },
    'fed.tip_rrp':         { en: 'Overnight reverse repos — cash absorbed from money-market funds; high RRP = excess liquidity parked at Fed.',
                              zh: '隔夜逆回购——美联储从货币市场基金吸收资金；RRP高说明系统内流动性过剩，资金停泊在美联储。' },
    'fed.tip_tga':         { en: "Treasury's cash balance at the Fed (TGA). Large drawdowns inject liquidity into the banking system.",
                              zh: '财政部在美联储的现金余额（TGA）。大幅动用TGA会向银行体系注入流动性。' },
    'fed.tip_currency':    { en: 'Physical currency outstanding — the largest Fed liability. Grows slowly in line with the economy.',
                              zh: '流通中的纸币总量——美联储最大的负债项目，随经济缓慢增长。' },
    'fed.tip_loans':       { en: 'Emergency loans from Federal Reserve Banks — spiked during the SVB crisis via the Bank Term Funding Program (BTFP).',
                              zh: '美联储紧急贷款，2023年硅谷银行危机期间通过银行定期融资计划（BTFP）大幅攀升。' },
    'fed.liab_title':      { en: 'Fed Liabilities: ON RRP & Treasury General Account', zh: '美联储负债：隔夜逆回购与财政部一般账户' },
    'fed.liab_desc':       { en: 'Key drains on reserve liquidity — overnight reverse repos (money-market parking) and Treasury\'s cash balance at the Fed',
                              zh: '准备金流动性的主要吸收项：隔夜逆回购（货币市场基金停泊资金）和财政部在美联储的现金余额' },
    'fed.liab_loading':    { en: 'Loading liability data…',          zh: '加载负债数据…' },
    'fed.currloan_title':  { en: 'Currency in Circulation & Fed Emergency Loans', zh: '流通中货币与美联储紧急贷款' },
    'fed.currloan_desc':   { en: 'Physical currency outstanding (largest Fed liability) alongside emergency lending — BTFP spike visible post-SVB 2023',
                              zh: '流通中货币（最大负债）与紧急贷款对比——2023年硅谷银行危机后BTFP飙升清晰可见' },
    'fed.currloan_loading': { en: 'Loading currency & loans data…', zh: '加载货币与贷款数据…' },

    'fed.chart_onrrp':     { en: 'ON RRP ($B)',                       zh: '隔夜逆回购（十亿美元）' },
    'fed.chart_tga':       { en: 'TGA ($B)',                          zh: '财政部一般账户（十亿美元）' },
    'fed.chart_currency':  { en: 'Currency in Circulation ($B)',      zh: '流通中货币（十亿美元）' },
    'fed.chart_fedloans':  { en: 'Fed Loans incl. BTFP ($B)',         zh: '美联储贷款含BTFP（十亿美元）' },

    // ── fed.html: Balance Sheet Identity panel ─────────────────────────
    'fed.balance_id_title': { en: 'Balance Sheet Identity', zh: '资产负债表恒等式' },
    'fed.balance_id_desc':  { en: 'Assets must equal Liabilities + Capital. Items not in our cards are grouped as "Other".',
                               zh: '资产必须等于负债加资本。未单独列出的项目归入"其他"。' },
    'fed.balance_assets':   { en: 'Assets',                         zh: '资产' },
    'fed.balance_liab':     { en: 'Liabilities + Capital',          zh: '负债 + 资本' },
    'fed.balance_li_treas': { en: '• Treasury Securities',           zh: '• 美国国债' },
    'fed.balance_li_mbs':   { en: '• MBS Holdings',                  zh: '• MBS持仓' },
    'fed.balance_li_loans': { en: '• Fed Loans (BTFP, etc.)',        zh: '• 美联储贷款（BTFP等）' },
    'fed.balance_li_othera':{ en: '• Other (Repos, swaps, premium, gold...)',
                               zh: '• 其他（回购、互换、债券溢价、黄金…）' },
    'fed.balance_li_res':   { en: '• Reserve Balances',              zh: '• 存款准备金' },
    'fed.balance_li_curr':  { en: '• Currency in Circulation',       zh: '• 流通中货币' },
    'fed.balance_li_tga':   { en: '• Treasury General Account',      zh: '• 财政部一般账户' },
    'fed.balance_li_rrp':   { en: '• Overnight Reverse Repos',       zh: '• 隔夜逆回购' },
    'fed.balance_li_otherl':{ en: '• Other liab. + Capital (~$45B)', zh: '• 其他负债 + 资本（约450亿美元）' },
    'fed.balance_why_label':{ en: '💡 Why it matters:',              zh: '💡 这有什么意义：' },
    'fed.balance_why':      { en: 'Total Assets can drop while Treasuries rise — for example when the Fed lets repos roll off, swap lines unwind, or as QE-era bond premiums amortize. The "Other" lines capture these moves.',
                               zh: '总资产可以在国债增加的同时下降——例如当美联储让回购到期、互换额度解除，或QE时期债券溢价摊销时。"其他"行捕捉这些变动。' },

    // ── fed.html: Other Assets explainer ──────────────────────────────
    'fed.other_assets_title': { en: 'What\'s in "Other Assets"?',   zh: '"其他资产"包含什么？' },
    'fed.other_assets_intro': { en: 'The Fed\'s balance sheet has more line items than the cards above. The "Other Assets" total is the sum of everything we don\'t break out individually. Here\'s what\'s typically in it:',
                                  zh: '美联储资产负债表的项目比上方卡片更多。"其他资产"的总和涵盖未单独列出的所有项目。常见组成如下：' },
    'fed.other_repos':        { en: 'Repurchase Agreements',         zh: '回购协议' },
    'fed.other_repos_desc':   { en: 'Fed lends cash overnight, takes Treasuries as collateral. Currently small (~$0–$50B), historically up to $500B.',
                                  zh: '美联储隔夜借出现金，以国债为担保。当前规模很小（约0–500亿美元），历史峰值曾达5000亿美元。' },
    'fed.other_swaps':        { en: 'Central Bank Liquidity Swaps',  zh: '央行流动性互换' },
    'fed.other_swaps_desc':   { en: 'USD lent to foreign central banks (ECB, BoJ, BoE, SNB, BoC). Spikes during global dollar shortages.',
                                  zh: '向外国央行（欧央行、日本央行、英国央行、瑞士国民银行、加拿大央行）借出美元。全球美元短缺时会激增。' },
    'fed.other_premium':      { en: 'Unamortized Premium on Securities', zh: '债券未摊销溢价' },
    'fed.other_premium_desc': { en: 'QE-era bonds bought above face value. Amortizes down each week (~$300–$400B currently).',
                                  zh: 'QE期间高于面值买入的债券，每周逐步摊销（当前约3000–4000亿美元）。' },
    'fed.other_fx':           { en: 'Foreign Currency Assets',       zh: '外币资产' },
    'fed.other_fx_desc':      { en: 'EUR, JPY, and other foreign currency holdings (~$20B).',
                                  zh: '欧元、日元等外币持仓（约200亿美元）。' },
    'fed.other_gold':         { en: 'Gold & SDR Certificates',       zh: '黄金与SDR凭证' },
    'fed.other_gold_desc':    { en: 'Gold certificate account (~$11B) + SDR certificates (~$5B). Mostly historical, doesn\'t change.',
                                  zh: '黄金凭证账户（约110亿美元）+ SDR凭证（约50亿美元）。基本是历史遗留，几乎不变。' },
    'fed.other_misc':         { en: 'Other Federal Reserve Assets',  zh: '其他美联储资产' },
    'fed.other_misc_desc':    { en: 'Accrued interest, premises (Fed offices), other items. Usually a few billion.',
                                  zh: '应计利息、Fed办公场所、其他项目，通常仅几十亿美元。' },

    // ── fed.html: Treasury Bills Outstanding (market-wide) ────────────
    'fed.tbills_outstanding_title': { en: 'U.S. Treasury Bills Outstanding', zh: '美国短期国库券（T-Bills）发行总量' },
    'fed.tbills_outstanding_desc':  { en: 'Total short-term Treasury bills issued market-wide (≤1 year maturity). NOT Fed-held — held by everyone (banks, MMFs, foreign investors, retail).',
                                        zh: '市场上流通的全部短期国库券（≤1年期）。这不是美联储持仓——而是所有人（银行、货币市场基金、外国投资者、散户）持有的。' },
    'fed.tbills_total':       { en: 'Total Outstanding',             zh: '发行总量' },
    'fed.tbills_wow':         { en: 'Week-over-Week',                zh: '周环比' },
    'fed.tbills_wow_desc':    { en: 'Last 7 days',                   zh: '最近7天' },
    'fed.tbills_yoy':         { en: 'Year-over-Year',                zh: '年同比' },
    'fed.tbills_yoy_desc':    { en: 'vs 52 weeks ago',               zh: '相比52周前' },
    'fed.tbills_pct_assets':  { en: '% of Fed Assets',               zh: '占美联储资产比例' },
    'fed.tbills_pct_desc':    { en: 'If Fed held them all',          zh: '若美联储全部持有' },
    'fed.tbills_why_label':   { en: '💡 Why it matters:',            zh: '💡 这有什么意义：' },
    'fed.tbills_why':         { en: 'Rapid growth in T-bills outstanding = Treasury financing more deficit short-term (cheaper but rate-sensitive). Money market funds absorb most of this supply.',
                                  zh: '短期国库券发行量快速增长 = 财政部更多通过短期债务融资赤字（成本较低但对利率敏感）。货币市场基金吸收了大部分此类供给。' },

    // ── fed.html: Currency in Circulation — Growth Speed metrics ──────
    'fed.curr_speed_title':       { en: 'Currency in Circulation — Growth Speed', zh: '流通中货币 — 增长速度' },
    'fed.curr_speed_desc':        { en: 'How fast physical currency (banknotes & coins) is being injected into the economy. Rapid growth often coincides with cash-hoarding episodes; flat or negative deltas signal de-circulation.',
                                     zh: '实物货币（纸币和硬币）注入经济的速度。快速增长通常伴随现金囤积；持平或负值则表明现金回笼。' },
    'fed.curr_speed_total':       { en: 'Current Level',     zh: '当前水平' },
    'fed.curr_speed_total_desc':  { en: 'Outstanding',       zh: '存量' },
    'fed.curr_speed_wow':         { en: 'Δ Week (WoW)',      zh: '周环比 (Δ)' },
    'fed.curr_speed_wow_desc':    { en: 'Last 7 days',       zh: '最近7天' },
    'fed.curr_speed_4w':          { en: 'Δ 4-Week',          zh: '4周变化' },
    'fed.curr_speed_4w_desc':     { en: '~Last month',       zh: '约近1个月' },
    'fed.curr_speed_13w':         { en: 'Δ 13-Week',         zh: '13周变化' },
    'fed.curr_speed_13w_desc':    { en: '~Last quarter',     zh: '约近1季度' },
    'fed.curr_speed_yoy':         { en: 'Δ 52-Week (YoY)',   zh: '52周变化（年同比）' },
    'fed.curr_speed_yoy_desc':    { en: 'vs 52w ago',        zh: '相比52周前' },
    'fed.curr_speed_yoy_pct':     { en: 'YoY %',             zh: '年同比 %' },
    'fed.curr_speed_yoy_pct_desc':{ en: 'Annualized speed',  zh: '年化速度' },
    'fed.curr_speed_why_label':   { en: '💡 Why it matters:', zh: '💡 这有什么意义：' },
    'fed.curr_speed_why':         { en: 'Currency in circulation grows slowly with nominal GDP (~5-7%/yr historically). Sudden spikes (COVID 2020, banking stress 2023) signal flight-to-cash; sustained slowdowns can indicate digital-payment displacement or weakening cash demand.',
                                     zh: '流通中货币通常与名义GDP同步缓慢增长（历史约每年5-7%）。突发性激增（如2020年新冠、2023年银行业危机）反映避险囤现金；持续放缓可能反映数字支付替代或现金需求疲弱。' },
    // ── fed.html: Currency in Circulation — Growth-Speed TREND chart ──
    'fed.curr_speed_trend_title':   { en: 'Growth-Speed Trend (annualized %)', zh: '增长速度趋势（年化 %）' },
    'fed.curr_speed_trend_desc':    { en: 'Year-over-year (52w), quarter (13w), and month (4w) growth rates plotted as annualized percentages. The historical baseline is roughly 5-7%/yr — sharp upward moves flag flight-to-cash, persistent dips below baseline flag de-circulation.',
                                       zh: '将52周（年同比）、13周（季度）、4周（月度）增长率统一换算为年化百分比绘制。历史基准约为每年 5-7%——突发上行反映避险囤现金，长期低于基准则提示去现金化。' },
    'fed.curr_speed_trend_loading': { en: 'Loading growth-rate history…', zh: '正在加载增长率历史…' },
    'fed.curr_speed_trend_4w':      { en: '4W ann. %',   zh: '4周年化 %' },
    'fed.curr_speed_trend_13w':     { en: '13W ann. %',  zh: '13周年化 %' },
    'fed.curr_speed_trend_yoy':     { en: 'YoY %',       zh: '年同比 %' },

    // ── fed.html: Other Assets — Detailed Breakdown (live numbers) ────
    'fed.other_detail_title':   { en: 'Other Assets — Live Numbers', zh: '其他资产 — 实时数据' },
    'fed.other_detail_desc':    { en: 'Selected line items inside the "Other Assets" bucket — Central Bank Liquidity Swaps (USD lent to foreign central banks), and the legacy Gold & SDR certificate accounts.',
                                   zh: '"其他资产"中的关键明细——央行流动性互换（向外国央行借出的美元），以及历史遗留的黄金与SDR凭证账户。' },
    'fed.other_detail_why_label':{ en: '💡 Why it matters:', zh: '💡 这有什么意义：' },
    'fed.other_detail_why':     { en: 'Liquidity swaps spike when foreign banks struggle to fund USD assets — early-warning signal for global dollar stress. Gold & SDR certificates are mostly inert legacy items, but they sit on the asset side and round out the balance-sheet identity.',
                                   zh: '当外国银行难以为美元资产融资时，流动性互换会激增——是全球美元紧张的预警信号。黄金与SDR凭证基本上是惰性的历史遗留项目，但它们位于资产端，构成资产负债表恒等式的一部分。' },
    // Central Bank Liquidity Swaps
    'fed.swaps_title':          { en: 'Central Bank Liquidity Swaps', zh: '央行流动性互换' },
    'fed.swaps_desc':           { en: 'USD lent to foreign central banks (ECB, BoJ, BoE, SNB, BoC). Spikes during global dollar shortages.',
                                   zh: '向外国央行（欧央行、日本央行、英国央行、瑞士国民银行、加拿大央行）借出的美元。全球美元短缺时会激增。' },
    'fed.swaps_current':        { en: 'Current',         zh: '当前' },
    'fed.swaps_wow':            { en: 'Δ Week (WoW)',    zh: '周环比 (Δ)' },
    'fed.swaps_high52':         { en: '52w High',        zh: '52周高点' },
    'fed.swaps_yoy':            { en: 'YoY Δ',           zh: '年同比 Δ' },
    // Gold Certificate Account
    'fed.gold_title':           { en: 'Gold Certificate Account', zh: '黄金凭证账户' },
    'fed.gold_desc':            { en: 'Treasury-issued certificates backed by U.S. gold reserves at $42.22/oz statutory price. Mostly historical — barely changes week-to-week.',
                                   zh: '财政部依法以每盎司42.22美元的法定价格、以美国黄金储备为抵押发行的凭证。基本是历史遗留——几乎不会逐周变化。' },
    'fed.gold_current':         { en: 'Current',         zh: '当前' },
    'fed.gold_wow':             { en: 'Δ Week (WoW)',    zh: '周环比 (Δ)' },
    'fed.gold_high52':          { en: '52w High',        zh: '52周高点' },
    'fed.gold_low52':           { en: '52w Low',         zh: '52周低点' },
    // SDR Certificate Account
    'fed.sdr_title':            { en: 'SDR Certificate Account', zh: 'SDR凭证账户' },
    'fed.sdr_desc':             { en: 'Special Drawing Rights certificates issued by the Treasury against the IMF\'s reserve asset. Stable balance, rarely adjusted.',
                                   zh: '财政部以IMF储备资产（特别提款权）为抵押发行的凭证。余额稳定，极少调整。' },
    'fed.sdr_current':          { en: 'Current',         zh: '当前' },
    'fed.sdr_wow':              { en: 'Δ Week (WoW)',    zh: '周环比 (Δ)' },
    'fed.sdr_high52':           { en: '52w High',        zh: '52周高点' },
    'fed.sdr_low52':            { en: '52w Low',         zh: '52周低点' },
    'fed.sdr_unavailable':      { en: 'FRED does not publish this line item as a standalone series. The balance is roughly $5.2B and is included inside "Other Assets" above.',
                                   zh: 'FRED 未将此项目作为独立序列发布。该余额约为 52 亿美元，已包含在上方的"其他资产"中。' },

    // ── contact.html ───────────────────────────────────────────────────
    'contact.title':        { en: 'Contact Us',  zh: '联系我们' },

    // ── guide.html ─────────────────────────────────────────────────────
    'guide.title':          { en: 'User Guide',        zh: '使用指南' },
    'guide.subtitle':       { en: 'Everything you need to know about yStocker — features, pages, and how to get the most out of each tool.',
                               zh: '关于 yStocker 的完整介绍——所有功能、页面及使用技巧。' },
    'guide.toc':            { en: 'On this page', zh: '本页导航' },

    'guide.s1_title':       { en: '1. Home — Sector Overview', zh: '1. 首页 — 板块概览' },
    'guide.s1_desc':        { en: 'The home page displays all peer groups as sortable cards. Each card shows the tickers in that group with live price, P/E ratio, analyst target, and % upside. Use the column headers to sort.',
                               zh: '首页以可排序卡片展示所有同类组。每张卡片显示该组的股票代码、实时价格、市盈率、分析师目标价及上涨空间。点击列标题可排序。' },
    'guide.s1_tip1':        { en: 'Green rows = upside to analyst target. Red = trading above target.', zh: '绿色行 = 距分析师目标有上涨空间；红色 = 已高于目标价。' },
    'guide.s1_tip2':        { en: 'Click ↻ Refresh to get the latest prices from Yahoo Finance.', zh: '点击"↻ 刷新"可从 Yahoo Finance 获取最新价格。' },
    'guide.s1_tip3':        { en: 'Click any ticker symbol to open its detail chart page.', zh: '点击任意股票代码可打开其详情图表页。' },

    'guide.s2_title':       { en: '2. Sector Detail — Charts & Table', zh: '2. 板块详情 — 图表与数据表' },
    'guide.s2_desc':        { en: 'Click a peer group name or any sector card to see full charts: P/E history, price history, analyst targets, and a detailed data table for every ticker in the group.',
                               zh: '点击同类组名称或板块卡片，可查看完整图表：市盈率历史、价格历史、分析师目标价，以及组内每只股票的详细数据表。' },
    'guide.s2_tip1':        { en: 'Use the range buttons (1M 3M 6M 1Y 2Y) to zoom in/out on charts.', zh: '使用范围按钮（1M 3M 6M 1Y 2Y）缩放图表。' },
    'guide.s2_tip2':        { en: 'Click ✦ Explain on any chart to get an AI-powered analysis of recent trends.', zh: '点击任意图表上的"✦ AI 解读"，即可获得 AI 驱动的近期趋势分析。' },

    'guide.s3_title':       { en: '3. Ticker Lookup', zh: '3. 股票查询' },
    'guide.s3_desc':        { en: 'Search any US stock ticker to see a detailed view: price, P/E, EPS, analyst target, 52-week range, market cap, dividend yield, and a 1-year price + P/E history chart.',
                               zh: '搜索任意美股代码，查看详情：价格、市盈率、EPS、分析师目标价、52周范围、市值、股息率，以及1年价格和市盈率历史图表。' },
    'guide.s3_tip1':        { en: 'Type a ticker (e.g. AAPL, NVDA, TSLA) and press Enter or click Look up.', zh: '输入股票代码（如 AAPL、NVDA、TSLA），按回车或点击"查询"。' },
    'guide.s3_tip2':        { en: 'The page also shows institutional ownership from recent 13F filings.', zh: '页面还会显示最近 13F 文件中的机构持仓信息。' },

    'guide.s4_title':       { en: '4. 13F — Institutional Holdings', zh: '4. 13F — 机构持仓' },
    'guide.s4_desc':        { en: 'View the latest quarterly 13F-HR filings from major funds (Berkshire, Bridgewater, etc.) sourced directly from SEC EDGAR. See each fund\'s top 50 equity positions with shares, value, and quarter-over-quarter change.',
                               zh: '查看来自 SEC EDGAR 的主要基金（伯克希尔、桥水等）最新季度 13F-HR 文件。查看每支基金前50大权益持仓的股数、市值及季环比变化。' },
    'guide.s4_tip1':        { en: 'Use the fund tabs at the top to switch between institutions.', zh: '使用顶部的基金标签切换机构。' },
    'guide.s4_tip2':        { en: 'Green/red arrows in the Change column show quarter-over-quarter position changes.', zh: '"变化"列中的绿/红箭头显示季环比持仓变化。' },

    'guide.s5_title':       { en: '5. Fed — Balance Sheet (H.4.1)', zh: '5. 美联储 — 资产负债表 (H.4.1)' },
    'guide.s5_desc':        { en: 'Weekly Federal Reserve balance sheet data from FRED. Tracks total assets, Treasury holdings, MBS, reserve balances, overnight reverse repos (ON RRP), the Treasury General Account (TGA), currency in circulation, and emergency loans (BTFP).',
                               zh: '来自 FRED 的美联储每周资产负债表数据。追踪总资产、国债持仓、MBS、存款准备金、隔夜逆回购（ON RRP）、财政部一般账户（TGA）、流通货币及紧急贷款（BTFP）。' },
    'guide.s5_tip1':        { en: 'ON RRP high = excess liquidity in system. TGA drawdown = Treasury spending reserves into banking system.', zh: 'ON RRP 高 = 系统内流动性过剩；TGA 减少 = 财政部将储备资金注入银行系统。' },
    'guide.s5_tip2':        { en: 'Click ✦ Explain on any chart for an AI-powered macro interpretation.', zh: '点击任意图表的"✦ AI 解读"，获取 AI 驱动的宏观解读。' },
    'guide.s5_tip3':        { en: 'Data updates weekly on Thursdays when the Fed publishes the H.4.1 release.', zh: '数据每周四更新，与美联储 H.4.1 发布同步。' },

    'guide.s6_title':       { en: '6. Groups — Manage Peer Groups', zh: '6. 分组 — 管理同类组' },
    'guide.s6_desc':        { en: 'Create and manage custom peer groups. Add any ticker to a group and it will appear on the home page sector cards and sector detail pages.',
                               zh: '创建和管理自定义同类组。将任意股票加入分组后，它将出现在首页板块卡片和板块详情页中。' },
    'guide.s6_tip1':        { en: 'Click "+ Add Group" to create a new peer group with a custom name.', zh: '点击"+ 新建分组"，使用自定义名称创建新的同类组。' },
    'guide.s6_tip2':        { en: 'Add tickers to any group by typing the symbol in the group\'s input field.', zh: '在分组的输入框中输入股票代码，即可添加到该组。' },

    'guide.s7_title':       { en: '7. Language & Refresh', zh: '7. 语言与刷新' },
    'guide.s7_desc':        { en: 'yStocker supports English and Chinese. Click 中文/EN in the top-right corner to toggle. The ↻ Refresh button re-fetches live data for the current page.',
                               zh: 'yStocker 支持中英文切换。点击右上角"中文/EN"按钮即可切换。↻ 刷新按钮将重新获取当前页面的实时数据。' },
    'guide.s7_tip1':        { en: 'Refresh is rate-limited to once every 10 minutes to avoid API overuse.', zh: '刷新操作每10分钟限制一次，以避免过度调用 API。' },

    'guide.screenshot_soon': { en: 'Screenshot coming soon', zh: '截图即将添加' },
    'guide.back_to_top':    { en: '↑ Back to top', zh: '↑ 返回顶部' },
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

    // ── sector names (peer groups) ─────────────────────────────────────
    'sector.name.Tech':              { en: 'Tech',               zh: '科技' },
    'sector.name.Software':          { en: 'Software',           zh: '软件' },
    'sector.name.Cloud / SaaS':      { en: 'Cloud / SaaS',       zh: '云计算 / SaaS' },
    'sector.name.Semiconductors':    { en: 'Semiconductors',     zh: '半导体' },
    'sector.name.Financials':        { en: 'Financials',         zh: '金融' },
    'sector.name.Healthcare':        { en: 'Healthcare',         zh: '医疗健康' },
    'sector.name.Biotech':           { en: 'Biotech',            zh: '生物科技' },
    'sector.name.Retail':            { en: 'Retail',             zh: '零售' },
    'sector.name.E-commerce':        { en: 'E-commerce',         zh: '电商' },
    'sector.name.Streaming / Media': { en: 'Streaming / Media',  zh: '流媒体 / 媒体' },
    'sector.name.Real Estate':       { en: 'Real Estate',        zh: '房地产' },
    'sector.name.Metals & Mining':   { en: 'Metals & Mining',    zh: '金属与矿业' },
    'sector.name.Energy / Oil & Gas':{ en: 'Energy / Oil & Gas', zh: '能源 / 石油天然气' },
    'sector.name.Industrials':       { en: 'Industrials',        zh: '工业' },
    'sector.name.Apparel & Footwear':{ en: 'Apparel & Footwear', zh: '服装与鞋类' },
    'sector.name.Consumer Staples':  { en: 'Consumer Staples',   zh: '必需消费品' },
    'sector.name.Airlines & Travel': { en: 'Airlines & Travel',  zh: '航空与旅游' },
    'sector.name.Communication':     { en: 'Communication',      zh: '通信服务' },
    'sector.name.Utilities':         { en: 'Utilities',          zh: '公用事业' },
    'sector.name.AI / Robotics':     { en: 'AI / Robotics',      zh: 'AI / 机器人' },
    'sector.name.US Broad ETFs':     { en: 'US Broad ETFs',      zh: '美国宽基ETF' },
    'sector.name.Sector ETFs':       { en: 'Sector ETFs',        zh: '行业ETF' },
    'sector.name.International ETFs':{ en: 'International ETFs', zh: '国际ETF' },
    'sector.name.Commodities ETFs':  { en: 'Commodities ETFs',   zh: '大宗商品ETF' },
    'sector.name.China Tech':        { en: 'China Tech',         zh: '中概科技股' },

    // ── daily_report.html ─────────────────────────────────────────────
    'daily.title':            { en: 'Daily Markets Report',      zh: '每日市场报告' },
    'daily.loading':          { en: 'Loading market data…',      zh: '加载市场数据…' },
    'daily.refresh':          { en: 'Refresh',                   zh: '刷新' },
    'daily.card_us':          { en: 'US Indices',                zh: '美国指数' },
    'daily.card_intl':        { en: 'Asia / Europe',             zh: '亚洲 / 欧洲' },
    'daily.card_sentiment':   { en: 'Sentiment',                 zh: '市场情绪' },
    'daily.card_sectors':     { en: 'Sector Performance (Day Change)', zh: '板块表现（日涨跌）' },
    'daily.card_commodities': { en: 'Commodity Ratios',          zh: '商品比率' },
    'daily.card_vix':         { en: 'VIX',                       zh: 'VIX' },
    'daily.card_pcr':         { en: 'Put/Call Ratio',            zh: '看跌/看涨比率' },
    'daily.card_fg':          { en: 'Fear & Greed',              zh: '恐慌与贪婪' },
    'daily.card_events':      { en: 'Economic Events',           zh: '经济事件' },
    'daily.card_gainers':     { en: 'Top Gainers',               zh: '涨幅榜' },
    'daily.card_losers':      { en: 'Top Losers',                zh: '跌幅榜' },
    'daily.card_ai':          { en: 'AI Market Commentary',      zh: 'AI市场解读' },
    'daily.card_gold_ratios': { en: 'Gold / Silver & Gold / Copper Ratio', zh: '金银比 & 金铜比' },
    'daily.card_indices_chg': { en: 'Indices — Day Change',      zh: '指数 — 今日涨跌' },
    'daily.translate':        { en: 'Translate',                 zh: '翻译' },
    'daily.generate':         { en: 'Generate',                  zh: '生成' },
    'daily.regenerate':       { en: 'Regenerate',                zh: '重新生成' },
    'daily.ai_hint':          { en: 'Click "Generate" for an AI-written market summary.',
                                 zh: '点击"生成"获取AI撰写的市场摘要。' },
    'daily.generating':       { en: 'Generating…',               zh: '生成中…' },
    'daily.thinking':         { en: 'Thinking…',                 zh: '思考中…' },
    'daily.aaii_bull':        { en: 'AAII Bull',                 zh: 'AAII 看多' },
    'daily.aaii_bear':        { en: 'AAII Bear',                 zh: 'AAII 看空' },
    'daily.aaii_spread':      { en: 'Bull-Bear Spread',          zh: '牛熊差' },
    'daily.gold':             { en: 'Gold',                      zh: '黄金' },
    'daily.silver':           { en: 'Silver',                    zh: '白银' },
    'daily.gold_silver':      { en: 'Gold / Silver',             zh: '金银比' },
    'daily.gold_copper':      { en: 'Gold / Copper',             zh: '金铜比' },
    'daily.today_label':      { en: 'Today',                     zh: '今日' },
    'daily.today_chg':        { en: 'today',                     zh: '今日' },
    'daily.no_events':        { en: 'No upcoming events found.', zh: '暂无即将发布的事件。' },
    'daily.next_high_impact': { en: 'Next high-impact events:',  zh: '下一批高影响事件：' },
    'daily.no_data':          { en: 'No data',                   zh: '暂无数据' },
    'daily.unavailable':      { en: 'Unavailable',               zh: '不可用' },
    'daily.loading_short':    { en: 'Loading…',                  zh: '加载中…' },
    'daily.day_chg_label':    { en: 'Day change:',               zh: '日涨跌：' },
    'daily.ma20_label':       { en: '20-day MA:',                zh: '20日均线：' },
    'daily.pcr_label':        { en: 'PCR',                       zh: '看跌/看涨比' },
    'daily.fg_label':         { en: 'Fear & Greed',              zh: '恐慌与贪婪' },
    'daily.gs_avg':           { en: '2Y Avg',                    zh: '2年均值' },
    'daily.gc_avg':           { en: '2Y Avg',                    zh: '2年均值' },
    'daily.email_title':      { en: 'Email This Report',         zh: '发送/订阅每日报告' },
    'daily.email_desc':       { en: 'Send today\'s report to your inbox, or subscribe for future daily emails.', zh: '发送今日报告，或订阅未来每日邮件。' },
    'daily.email_ph':         { en: 'your@email.com',            zh: '您的邮箱地址' },
    'daily.email_send':       { en: 'Send',                      zh: '发送' },
    'daily.email_sending':    { en: 'Sending…',                  zh: '发送中…' },
    'daily.email_sent':       { en: '✓ Sent! Check your inbox.', zh: '✓ 已发送！请查收邮件。' },
    'daily.email_error':      { en: 'Invalid email.',            zh: '邮箱地址无效。' },
    'daily.subscribe':        { en: 'Subscribe',                 zh: '订阅' },
    'daily.subscribing':      { en: 'Subscribing…',              zh: '订阅中…' },
    'daily.subscribed':       { en: '✓ Subscribed! You\'ll receive future daily reports.', zh: '✓ 订阅成功！您将收到未来的每日报告。' },
    'daily.already_subscribed': { en: '✓ Already subscribed.',  zh: '✓ 您已订阅。' },
    'daily.subscribe_error':  { en: 'Subscribe failed.',         zh: '订阅失败。' },

    'daily.tab_us':           { en: 'US Market',                zh: '美国市场' },
    'daily.tab_cn':           { en: 'CN / Asia',                zh: '中国 / 亚洲' },
    'daily.date_picker_label':{ en: 'Browse past reports',      zh: '查看历史报告' },
    'daily.no_history':       { en: 'No summary available for this date.', zh: '该日期暂无摘要。' },
    'daily.history_us':       { en: 'US Market (historical)',   zh: '美国市场（历史）' },
    'daily.history_cn':       { en: 'CN / Asia (historical)',   zh: '中国 / 亚洲（历史）' },

    // Economic Events — country code localization
    'events.country.US': { en: 'US',  zh: '美国' },
    'events.country.CN': { en: 'CN',  zh: '中国' },
    'events.country.EU': { en: 'EU',  zh: '欧元区' },
    'events.country.JP': { en: 'JP',  zh: '日本' },
    'events.country.GB': { en: 'GB',  zh: '英国' },
    'events.country.AU': { en: 'AU',  zh: '澳大利亚' },
    'events.country.CA': { en: 'CA',  zh: '加拿大' },
    'events.country.DE': { en: 'DE',  zh: '德国' },
    'events.country.FR': { en: 'FR',  zh: '法国' },
    'events.country.KR': { en: 'KR',  zh: '韩国' },
    'events.country.TW': { en: 'TW',  zh: '台湾' },
    'events.country.NZ': { en: 'NZ',  zh: '新西兰' },
    'events.country.CH': { en: 'CH',  zh: '瑞士' },
    'events.country.IN': { en: 'IN',  zh: '印度' },
    'events.country.MX': { en: 'MX',  zh: '墨西哥' },

    // ── unsubscribe page ──────────────────────────────────────────────────
    'unsub.title_success': { en: 'Unsubscribed',                       zh: '已退订' },
    'unsub.body_success':  { en: 'You\'ve been removed from the daily report mailing list.', zh: '您已从每日报告邮件列表中退订。' },
    'unsub.resubscribe_hint': { en: 'You can re-subscribe any time from the Daily page.', zh: '您可随时在每日报告页面重新订阅。' },
    'unsub.title_error':   { en: 'Could not unsubscribe',             zh: '退订失败' },
    'unsub.back':          { en: 'Back to Daily Report',              zh: '返回每日报告' },

    // ── forecast section (history.html) ───────────────────────────────
    'forecast.title':      { en: 'Price Forecast — 6 Months', zh: '价格预测 — 6个月' },
    'forecast.desc':       { en: 'Statistical projections from 3 independent models trained on 3 years of weekly data. Not investment advice.',
                              zh: '基于3年周线数据训练的3个独立模型的统计预测。不构成投资建议。' },
    'forecast.prophet':    { en: 'Prophet', zh: 'Prophet' },
    'forecast.arima':      { en: 'ARIMA', zh: 'ARIMA' },
    'forecast.linear':     { en: 'Linear', zh: '线性回归' },
    'forecast.actual':     { en: 'Actual price', zh: '实际价格' },
    'forecast.ci':         { en: '80% CI', zh: '80% 置信区间' },
    'forecast.loading':    { en: 'Running models…', zh: '运行模型中…' },
    'forecast.failed':     { en: 'Forecast unavailable.', zh: '预测不可用。' },
    'forecast.disclaimer': { en: '⚠ Statistical projections only — not financial advice. Past patterns do not guarantee future results.',
                              zh: '⚠ 仅为统计预测，不构成投资建议。历史规律不代表未来结果。' },
    'forecast.model_error':{ en: 'model unavailable', zh: '模型不可用' },

    // ── market index page ──────────────────────────────────────────────
    'markets.title':       { en: 'Market Overview', zh: '市场概览' },
    'markets.subtitle':    { en: 'S&P 500 · Nasdaq · Dow Jones · FTSE 100 · Nikkei · Shanghai · Taiwan · KOSPI — live snapshot & historical charts',
                             zh: '标普500 · 纳斯达克 · 道琼斯 · 富时100 · 日经225 · 上证指数 · 台湾加权 · 韩国综合 — 实时快照与历史图表' },
    'markets.spx':         { en: 'S&P 500', zh: '标普500' },
    'markets.ixic':        { en: 'Nasdaq Composite', zh: '纳斯达克综合指数' },
    'markets.dji':         { en: 'Dow Jones', zh: '道琼斯工业平均指数' },
    'markets.price':       { en: 'Price', zh: '指数点位' },
    'markets.day_chg':     { en: 'Day Change', zh: '日涨跌' },
    'markets.ytd':         { en: 'YTD', zh: '年初至今' },
    'markets.52w_high':    { en: '52W High', zh: '52周最高' },
    'markets.52w_low':     { en: '52W Low', zh: '52周最低' },
    'markets.pe':          { en: 'P/E Ratio', zh: '市盈率' },
    'markets.volume':      { en: 'Volume', zh: '成交量' },
    'markets.fear_greed':  { en: 'Fear & Greed', zh: '恐慌与贪婪' },
    'markets.fear_greed_desc': { en: 'CNN Fear & Greed Index — composite sentiment', zh: 'CNN恐慌与贪婪指数 — 综合情绪' },
    'markets.vix':         { en: 'VIX (Volatility)', zh: 'VIX（波动率）' },
    'markets.vix_desc':    { en: 'CBOE Volatility Index — fear gauge of the S&P 500', zh: 'CBOE波动率指数 — 标普500恐慌指标' },
    'markets.fg_extreme_fear':  { en: 'Extreme Fear',  zh: '极度恐慌' },
    'markets.fg_fear':          { en: 'Fear',          zh: '恐慌' },
    'markets.fg_neutral':       { en: 'Neutral',       zh: '中性' },
    'markets.fg_greed':         { en: 'Greed',         zh: '贪婪' },
    'markets.fg_extreme_greed': { en: 'Extreme Greed', zh: '极度贪婪' },
    'markets.fg_prev_close':    { en: 'Prev Close',    zh: '昨收' },
    'markets.fg_prev_week':     { en: 'Prev Week',     zh: '上周' },
    'markets.fg_prev_month':    { en: 'Prev Month',    zh: '上月' },
    'markets.fg_prev_year':     { en: 'Prev Year',     zh: '去年' },
    'markets.breadth':     { en: 'Market Breadth', zh: '市场宽度' },
    'markets.adv_dec':     { en: 'Advance / Decline', zh: '上涨 / 下跌' },
    'markets.sector_perf': { en: 'Sector Performance', zh: '板块表现' },
    'markets.tab_overview':{ en: 'Overview', zh: '概览' },
    'markets.tab_spx':     { en: 'S&P 500',    zh: '标普500' },
    'markets.tab_ixic':    { en: 'Nasdaq',     zh: '纳斯达克' },
    'markets.tab_dji':     { en: 'Dow Jones',  zh: '道琼斯' },
    'markets.tab_ftse':    { en: 'FTSE 100',   zh: '富时100' },
    'markets.tab_n225':    { en: 'Nikkei 225', zh: '日经225' },
    'markets.tab_sse':     { en: 'Shanghai',   zh: '上证指数' },
    'markets.tab_twii':    { en: 'Taiwan',     zh: '台湾加权' },
    'markets.tab_kospi':   { en: 'KOSPI',      zh: '韩国综合' },
    'markets.tab_gold':    { en: 'Gold',       zh: '黄金' },
    'markets.tab_silver':  { en: 'Silver',     zh: '白银' },
    'markets.tab_oil':     { en: 'Crude Oil',  zh: '原油' },
    'markets.tab_natgas':  { en: 'Nat Gas',    zh: '天然气' },
    'markets.tab_copper':  { en: 'Copper',     zh: '铜' },
    'markets.idx_spx':     { en: 'S&P 500',              zh: '标普500' },
    'markets.idx_ixic':    { en: 'Nasdaq',               zh: '纳斯达克' },
    'markets.idx_dji':     { en: 'Dow Jones',             zh: '道琼斯' },
    'markets.idx_ftse':    { en: 'FTSE 100',              zh: '英国富时100' },
    'markets.idx_n225':    { en: 'Nikkei 225',            zh: '日经225' },
    'markets.idx_sse':     { en: 'Shanghai Composite',    zh: '上证综合指数' },
    'markets.idx_csi500':  { en: 'CSI 500',               zh: '中证500' },
    'markets.idx_twii':    { en: 'Taiwan Weighted',       zh: '台湾加权指数' },
    'markets.idx_kospi':   { en: 'KOSPI',                 zh: '韩国综合指数' },
    'markets.idx_gold':    { en: 'Gold (COMEX GC=F)',     zh: '黄金 (COMEX GC=F)' },
    'markets.idx_silver':  { en: 'Silver (COMEX SI=F)',   zh: '白银 (COMEX SI=F)' },
    'markets.idx_oil':     { en: 'WTI Crude Oil (CL=F)',  zh: 'WTI 原油 (CL=F)' },
    'markets.idx_brent':   { en: 'Brent Crude Oil (BZ=F)', zh: '布伦特原油 (BZ=F)' },
    'markets.idx_natgas':  { en: 'Natural Gas (NG=F)',    zh: '天然气 (NG=F)' },
    'markets.idx_copper':  { en: 'Copper (HG=F)',         zh: '铜 (HG=F)' },
    'markets.idx_dxy':     { en: 'US Dollar Index (DXY)', zh: '美元指数 (DXY)' },
    'markets.hist_title':  { en: 'Price History', zh: '价格历史' },
    'markets.hist_desc':   { en: 'Weekly closing level', zh: '每周收盘点位' },
    'markets.rsi':         { en: 'RSI (14)', zh: 'RSI（14日）' },
    'markets.ma50':        { en: '50-day MA', zh: '50日均线' },
    'markets.ma200':       { en: '200-day MA', zh: '200日均线' },
    'markets.golden_cross':{ en: 'Golden Cross', zh: '金叉' },
    'markets.death_cross': { en: 'Death Cross', zh: '死叉' },
    'markets.above_ma200': { en: 'Above 200MA', zh: '高于200日均线' },
    'markets.below_ma200': { en: 'Below 200MA', zh: '低于200日均线' },
    'markets.loading':     { en: 'Loading market data…', zh: '加载市场数据…' },
    'markets.nav':         { en: 'Markets', zh: '市场' },

    // AAII Sentiment
    'markets.aaii_title':   { en: 'AAII Sentiment',         zh: 'AAII投资者情绪' },
    'markets.aaii_desc':    { en: 'AAII Investor Sentiment Survey — weekly', zh: 'AAII投资者情绪调查 — 每周更新' },
    'markets.aaii_bullish': { en: 'Bullish',                zh: '看多' },
    'markets.aaii_neutral': { en: 'Neutral',                zh: '中性' },
    'markets.aaii_bearish': { en: 'Bearish',                zh: '看空' },
    'markets.aaii_spread':  { en: 'Bull-Bear',              zh: '牛熊差' },

    // Put/Call Ratio
    'markets.pcr_title':    { en: 'Put/Call Ratio',         zh: '期权看跌/看涨比' },
    'markets.pcr_desc':     { en: 'CBOE Equity Put/Call Ratio — options sentiment', zh: 'CBOE股票期权看跌/看涨比 — 期权情绪' },
    'markets.pcr_ma20':     { en: '20-day MA',              zh: '20日均线' },
    'markets.pcr_bearish':  { en: 'Bearish (high put buying)', zh: '看跌（大量买入看跌期权）' },
    'markets.pcr_neutral':  { en: 'Neutral',                zh: '中性' },
    'markets.pcr_bullish':  { en: 'Bullish (low put buying)', zh: '看涨（看跌期权买入减少）' },

    // Gold Ratios
    'markets.gs_title':      { en: 'Gold / Silver Ratio',   zh: '金银比' },
    'markets.gs_desc':       { en: 'oz of silver per oz of gold — higher = gold relatively expensive', zh: '每盎司黄金兑银盎司数 — 越高代表黄金相对昂贵' },
    'markets.gc_title':      { en: 'Gold / Copper Ratio',   zh: '金铜比' },
    'markets.gc_desc':       { en: 'gold ($/oz) ÷ copper ($/lb) — economic risk indicator', zh: '黄金（美元/盎司）÷ 铜（美元/磅）— 经济风险指标' },

    // Treasury Yield Curve
    'markets.us_yield_title': { en: 'US Treasury Yield Curve',  zh: '美国国债收益率曲线' },
    'markets.us_yield_desc':  { en: 'Current yields by maturity — inverted curve signals recession risk', zh: '美债各期限实时收益率，曲线倒挂预示经济衰退风险' },
    'markets.cn_yield_title': { en: 'CN Treasury Yield Curve',   zh: '中国国债收益率曲线' },
    'markets.cn_yield_desc':  { en: 'Current CN government bond yields by maturity', zh: '中国国债各期限实时收益率' },
    'markets.yield_inverted': { en: '⚠ Inverted',               zh: '⚠ 倒挂' },
    'markets.yield_normal':   { en: 'Normal',                   zh: '正常' },
    'markets.yield_spread':   { en: 'Spread',                   zh: '利差' },

    // Economic Events
    'markets.econ_events_title': { en: 'Economic Events',         zh: '经济事件日历' },
    'markets.econ_events_desc':  { en: 'Key macro releases & central bank decisions this week', zh: '本周重要宏观数据发布及央行决议' },
    'markets.econ_translate':    { en: 'Translate to Chinese',    zh: '翻译为中文' },
    'markets.econ_no_events':    { en: 'No upcoming events.',     zh: '暂无即将发布的事件。' },
    'markets.econ_filter_all':   { en: 'All',         zh: '全部' },
    'markets.econ_filter_high':  { en: 'High Impact', zh: '高影响' },
    'markets.econ_hide_past':    { en: 'Hide past',   zh: '隐藏已过' },
    'markets.econ_col_date':     { en: 'Date',                    zh: '日期' },
    'markets.econ_col_time':     { en: 'Time',                    zh: '时间' },
    'markets.econ_col_country':  { en: 'Country',                 zh: '国家' },
    'markets.econ_col_event':    { en: 'Event',                   zh: '事件' },
    'markets.econ_col_impact':   { en: 'Impact',                  zh: '影响' },
    'markets.econ_col_actual':   { en: 'Actual',                  zh: '实际值' },
    'markets.econ_col_forecast': { en: 'Forecast',                zh: '预测值' },
    'markets.econ_col_previous': { en: 'Previous',                zh: '前值' },

    // markets.html — technical labels
    'markets.moving_avg':       { en: 'Moving Averages',  zh: '均线' },
    'markets.macd_signal':      { en: 'Signal',           zh: '信号线' },
    'markets.macd_histogram':   { en: 'Histogram',        zh: '柱状图' },
    'markets.rsi_overbought':   { en: '70 Overbought',    zh: '70 超买' },
    'markets.rsi_oversold':     { en: '30 Oversold',      zh: '30 超卖' },
    'markets.vix_calm':         { en: 'Calm',             zh: '平静' },
    'markets.vix_normal':       { en: 'Normal',           zh: '正常' },
    'markets.vix_elevated':     { en: 'Elevated',         zh: '偏高' },
    'markets.vix_extreme':      { en: 'Extreme',          zh: '极端' },
    'markets.rsi_zone_ob':      { en: 'Overbought',       zh: '超买' },
    'markets.rsi_zone_bullish': { en: 'Bullish',          zh: '看多' },
    'markets.rsi_zone_neutral': { en: 'Neutral',          zh: '中性' },
    'markets.rsi_zone_bearish': { en: 'Bearish',          zh: '看空' },
    'markets.rsi_zone_os':      { en: 'Oversold',         zh: '超卖' },
    'markets.no_data':          { en: 'No data',          zh: '暂无数据' },
    'markets.unavailable':      { en: 'Unavailable',      zh: '不可用' },
    'markets.forecast_arrow':   { en: 'Forecast →',       zh: '预测 →' },
    'markets.aaii_avg':         { en: 'Avg 37.5%',        zh: '均值 37.5%' },

    // ── heatmap.html ───────────────────────────────────────────────────
    'heatmap.nav':         { en: 'Heatmap',           zh: '热力图' },

    // ── commodities.html ───────────────────────────────────────────────
    'nav.commodities_overview': { en: '📊 Overview & Detail', zh: '📊 概览与详情' },
    'commodities.title':       { en: 'Commodities', zh: '大宗商品' },
    'commodities.subtitle':    { en: 'Live futures prices, returns, and cross-commodity ratios across precious & industrial metals, energy, and agriculture',
                                 zh: '实时期货价格、收益率与跨品类比率：贵金属、工业金属、能源与农产品' },
    'commodities.data_source': { en: 'Source:',         zh: '数据来源：' },
    'commodities.cache_note':  { en: '15-minute cache', zh: '15分钟缓存' },
    'commodities.loading':     { en: 'Fetching commodities data…', zh: '正在加载大宗商品数据…' },
    'commodities.group_metals':     { en: 'Precious Metals',   zh: '贵金属' },
    'commodities.group_industrial': { en: 'Industrial Metals', zh: '工业金属' },
    'commodities.group_energy':     { en: 'Energy',            zh: '能源' },
    'commodities.group_agri':       { en: 'Agriculture',       zh: '农产品' },
    'commodities.range_label':      { en: 'Range', zh: '区间' },
    'commodities.range_5y':         { en: '5Y',    zh: '5年' },
    'commodities.range_1m':         { en: '1M',    zh: '1个月' },
    'commodities.range_3m':         { en: '3M',    zh: '3个月' },
    'commodities.range_6m':         { en: '6M',    zh: '6个月' },
    'commodities.range_1y':         { en: '1Y',    zh: '1年' },
    'commodities.range_2y':         { en: '2Y',    zh: '2年' },
    'commodities.card_rsi_label':   { en: 'RSI:',  zh: 'RSI:' },
    'commodities.detail_today':     { en: 'today', zh: '今日' },
    'commodities.detail_52w':       { en: '52w',   zh: '52周' },
    'commodities.detail_ma50':      { en: '50d MA', zh: '50日均' },
    'commodities.detail_ma200':     { en: '200d MA', zh: '200日均' },
    'commodities.ratio_current':    { en: 'current', zh: '当前' },
    'commodities.error_load':       { en: 'Failed to load commodities data', zh: '加载大宗商品数据失败' },
    'commodities.metric_1m':        { en: '1M',     zh: '1个月' },
    'commodities.metric_3m':        { en: '3M',     zh: '3个月' },
    'commodities.metric_6m':        { en: '6M',     zh: '6个月' },
    'commodities.metric_ytd':       { en: 'YTD',    zh: '年初至今' },
    'commodities.metric_1y':        { en: '1Y',     zh: '1年' },
    'commodities.metric_rsi':       { en: 'RSI-14', zh: 'RSI（14日）' },
    'commodities.detail_ma_note':   { en: 'Dashed lines:', zh: '虚线说明：' },
    'commodities.ratios_title':     { en: 'Cross-Commodity Ratios', zh: '跨品类比率' },
    'commodities.ratios_desc':      { en: 'Macro indicators built from commodity ratios — sentiment, inflation, and risk-on/off signals.',
                                       zh: '由商品比率构建的宏观指标——情绪、通胀及风险偏好信号。' },
    'commodities.table_title':      { en: 'All Commodities — Sortable Table', zh: '全部商品 — 可排序表格' },
    'commodities.table_desc':       { en: 'Click any column to sort. Click a row to load the chart above.',
                                       zh: '点击列标题排序，点击行加载上方详情图表。' },
    'commodities.col_name':         { en: 'Commodity', zh: '商品' },
    'commodities.col_last':         { en: 'Last',      zh: '最新价' },
    'commodities.col_day':          { en: 'Day',       zh: '日涨跌' },
    'commodities.col_1m':           { en: '1M',        zh: '1个月' },
    'commodities.col_3m':           { en: '3M',        zh: '3个月' },
    'commodities.col_ytd':          { en: 'YTD',       zh: '年初至今' },
    'commodities.col_1y':           { en: '1Y',        zh: '1年' },
    'commodities.col_rsi':          { en: 'RSI',       zh: 'RSI' },
    'commodities.col_52w':          { en: '52w Range', zh: '52周区间' },
    'commodities.notes_title':      { en: '💡 What these signals tell you', zh: '💡 这些信号意味着什么' },
    'commodities.note_gold':        { en: 'Gold rises during inflation fears, currency debasement, and geopolitical risk. The 50d/200d MA cross is a common momentum signal.',
                                       zh: '黄金在通胀担忧、货币贬值和地缘风险中走强。50日/200日均线交叉是常见的动量信号。' },
    'commodities.note_silver':      { en: 'Silver moves with both gold (precious) and copper (industrial). Higher beta than gold — bigger swings up and down.',
                                       zh: '白银既有贵金属属性（跟黄金）也有工业属性（跟铜）。贝塔高于黄金，涨跌幅更大。' },
    'commodities.note_oil':         { en: 'Crude oil tracks global growth and OPEC+ policy. Brent–WTI spread reflects shipping arbitrage; widens during Middle East stress.',
                                       zh: '原油价格反映全球增长与OPEC+政策。布伦特-WTI价差体现运输套利，中东紧张时通常扩大。' },
    'commodities.note_natgas':      { en: 'Nat gas is highly seasonal (winter heating) and volatile. Spikes during supply shocks (e.g. 2022 Europe crisis).',
                                       zh: '天然气季节性极强（冬季供暖）且波动剧烈，供应冲击（如2022年欧洲危机）时会暴涨。' },
    'commodities.note_copper':      { en: '"Dr Copper" — its price reflects global manufacturing demand. Falling copper while gold rises = recession signal.',
                                       zh: '"铜博士"——价格反映全球制造业需求。铜跌而金涨往往是衰退信号。' },
    'commodities.note_agri':        { en: 'Agricultural commodities depend on weather, plantings, and trade flows. Wheat especially sensitive to geopolitics.',
                                       zh: '农产品价格取决于天气、播种与贸易流向，小麦尤其对地缘政治敏感。' },
    // Commodity names (used by emojis + display)
    'commodities.name.gold':        { en: 'Gold',         zh: '黄金' },
    'commodities.name.silver':      { en: 'Silver',       zh: '白银' },
    'commodities.name.platinum':    { en: 'Platinum',     zh: '铂金' },
    'commodities.name.palladium':   { en: 'Palladium',    zh: '钯金' },
    'commodities.name.copper':      { en: 'Copper',       zh: '铜' },
    'commodities.name.oil_wti':     { en: 'WTI Crude',    zh: 'WTI原油' },
    'commodities.name.oil_brent':   { en: 'Brent Crude',  zh: '布伦特原油' },
    'commodities.name.natgas':      { en: 'Natural Gas',  zh: '天然气' },
    'commodities.name.gasoline':    { en: 'Gasoline',     zh: '汽油' },
    'commodities.name.heating':     { en: 'Heating Oil',  zh: '取暖油' },
    'commodities.name.wheat':       { en: 'Wheat',        zh: '小麦' },
    'commodities.name.corn':        { en: 'Corn',         zh: '玉米' },
    'commodities.name.soybeans':    { en: 'Soybeans',     zh: '大豆' },
    'commodities.name.coffee':      { en: 'Coffee',       zh: '咖啡' },
    'commodities.name.sugar':       { en: 'Sugar',        zh: '糖' },

    // Sector performance bar labels (from SPDR ETFs)
    'markets.sector.Tech':          { en: 'Tech',             zh: '科技' },
    'markets.sector.Financials':    { en: 'Financials',       zh: '金融' },
    'markets.sector.Energy':        { en: 'Energy',           zh: '能源' },
    'markets.sector.Healthcare':    { en: 'Healthcare',       zh: '医疗健康' },
    'markets.sector.Industrials':   { en: 'Industrials',      zh: '工业' },
    'markets.sector.Consumer Disc.':{ en: 'Consumer Disc.',   zh: '非必需消费' },
    'markets.sector.Consumer Stap.':{ en: 'Consumer Stap.',   zh: '必需消费' },
    'markets.sector.Utilities':     { en: 'Utilities',        zh: '公用事业' },
    'markets.sector.Materials':     { en: 'Materials',        zh: '材料' },
    'markets.sector.Real Estate':   { en: 'Real Estate',      zh: '房地产' },
    'heatmap.title':       { en: 'Sector Heatmap',    zh: '板块热力图' },
    'heatmap.subtitle':    { en: 'S&P 500 top stocks — color-coded by day change. Click any tile to open the stock.',
                             zh: '标普500主要个股 — 按日涨跌幅着色。点击色块查看详情。' },
    'heatmap.loading':     { en: 'Loading heatmap…',  zh: '加载热力图中…' },
    'heatmap.date_label':  { en: 'Date',              zh: '日期' },
    'heatmap.today':       { en: 'Today',             zh: '今日' },
    'heatmap.snapshot':    { en: 'Save Snapshot',     zh: '保存快照' },
    'heatmap.no_data':     { en: 'Only today\'s data can be fetched live. Past dates require a prior snapshot.',
                             zh: '仅今日数据支持实时获取，历史日期需预先保存快照。' },
    'heatmap.no_data_title':{ en: 'No snapshot for this date', zh: '该日期暂无快照' },
    'heatmap.advancing':   { en: 'Advancing',     zh: '上涨' },
    'heatmap.declining':   { en: 'Declining',     zh: '下跌' },
    'heatmap.avg_chg':     { en: 'Avg Change',    zh: '平均涨跌' },
    'heatmap.best_worst':  { en: 'Best / Worst',  zh: '最强 / 最弱' },

    // ── videos.html ────────────────────────────────────────────────────
    'videos.title':         { en: 'Market Videos',     zh: '市场视频' },
    'videos.subtitle':      { en: 'Latest videos from curated finance channels — search by ticker or topic.',
                               zh: '精选财经频道最新视频 — 按股票代码或主题搜索。' },
    'videos.search_ph':     { en: 'e.g. NVDA, AAPL, Fed, earnings…', zh: '如 NVDA、AAPL、美联储、财报…' },
    'videos.search_btn':    { en: 'Search',            zh: '搜索' },
    'videos.empty':         { en: 'No videos found.',  zh: '未找到视频。' },
    'videos.loading':       { en: 'Fetching videos…',  zh: '正在加载视频…' },
    'videos.error':         { en: 'Failed to load videos.', zh: '视频加载失败。' },
    'videos.all_channels':  { en: 'All channels',      zh: '全部频道' },
  };

  let current = (() => {
    // 1. URL param takes top priority: ?lang=zh or ?lang=en
    const urlLang = new URLSearchParams(window.location.search).get('lang');
    if (urlLang === 'zh' || urlLang === 'en') {
      localStorage.setItem('ystocker_lang', urlLang);
      return urlLang;
    }
    // 2. Fallback to localStorage
    return localStorage.getItem('ystocker_lang') || 'en';
  })();

  // ── stock name translations (ticker → Chinese name) ─────────────────
  // Used by stockName(ticker, fallback) — English falls back to the
  // shortName from Yahoo Finance; Chinese uses this curated map.
  const STOCK_NAMES_ZH = {
    // Tech
    MSFT: '微软', AAPL: '苹果', GOOGL: '谷歌', META: 'Meta', NVDA: '英伟达', AMZN: '亚马逊',
    // Cloud / SaaS
    CRM: 'Salesforce', NOW: 'ServiceNow', ORCL: '甲骨文',
    // Semiconductors
    AMD: 'AMD', INTC: '英特尔', QCOM: '高通', TSM: '台积电', AVGO: '博通', ASML: 'ASML',
    // Financials
    JPM: '摩根大通', BAC: '美国银行', GS: '高盛', MS: '摩根士丹利',
    BLK: '贝莱德', COF: '第一资本', 'BRK-B': '伯克希尔·哈撒韦', AXP: '美国运通',
    // Healthcare
    UNH: '联合健康', JNJ: '强生', LLY: '礼来', ABBV: '艾伯维', MRK: '默克', ISRG: '直觉外科',
    // Retail
    WMT: '沃尔玛', COST: '好市多', TGT: '塔吉特', HD: '家得宝',
    // Real Estate
    AMT: '美国铁塔', PLD: '普洛斯', EQIX: '艾可迪', SPG: '西蒙地产', O: '瑞尔地产', HLT: '希尔顿',
    // Metals & Mining
    FCX: '自由港', NEM: '纽蒙特', AA: '美国铝业', MP: 'MP材料',
    // Metals ETFs (in Sector ETFs group)
    COPX: '铜矿ETF', GDX: '黄金矿业ETF', SIL: '白银矿业ETF', SLX: '钢铁ETF',
    // Apparel & Footwear
    NKE: '耐克', LULU: 'lululemon', UAA: '安德玛', VFC: 'VF集团',
    // US Broad ETFs
    SPY: '标普500ETF', QQQ: '纳斯达克100ETF', IWM: '罗素2000ETF', DIA: '道琼斯ETF', VTI: '全市场ETF',
    // Sector ETFs
    XLK: '科技ETF', XLF: '金融ETF', XLE: '能源ETF', XLV: '医疗ETF', XLI: '工业ETF',
    XLY: '非必需消费ETF', XLP: '必需消费ETF', XLU: '公用事业ETF', XLB: '材料ETF', XLRE: '房地产ETF',
    // International ETFs
    FLJP: '日本ETF', FLJH: '日本对冲ETF', FLKR: '韩国ETF', FLTW: '台湾ETF',
    FLCA: '加拿大ETF', IXUS: '国际股票ETF', VXUS: '全球除美ETF', FLEE: '欧洲ETF',
    ASHS: 'A股小盘ETF', FLBR: '巴西ETF', FLCH: '中国ETF', FLGR: '德国ETF',
    FLMX: '墨西哥ETF', FLAX: '澳大利亚ETF', FLSW: '瑞士ETF',
  };

  function t(key) {
    const entry = LANGS[key];
    if (!entry) return null;
    return entry[current] ?? entry['en'];
  }

  /** Localized sector/peer-group name. Falls back to the raw English key. */
  function tSector(name) {
    const v = t('sector.name.' + name);
    return v != null ? v : name;
  }

  /** Localized stock company name.
   *  @param {string} ticker   - e.g. "AAPL"
   *  @param {string} fallback - English shortName from Yahoo Finance
   */
  function stockName(ticker, fallback) {
    if (current === 'zh') {
      return STOCK_NAMES_ZH[ticker] || fallback || ticker;
    }
    return fallback || ticker;
  }

  /** Localized market index name. Falls back to the raw English key.
   *  @param {string} key - e.g. "spx", "n225"
   */
  function tIdx(key) {
    const v = t('markets.idx_' + key);
    return v != null ? v : key.toUpperCase();
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
    // Update URL so the link is shareable
    const url = new URL(window.location.href);
    url.searchParams.set('lang', lang);
    window.history.replaceState({}, '', url.toString());
    // Update toggle button text
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = lang === 'zh' ? 'EN' : '中文';
    });
    // Re-inject lang into all internal links
    _injectLangLinks(lang);
  }

  function toggle() {
    setLang(current === 'en' ? 'zh' : 'en');
  }

  function getLang() { return current; }

  // Inject ?lang= into all internal links so language survives navigation
  function _injectLangLinks(lang) {
    document.querySelectorAll('a[href]').forEach(a => {
      try {
        const url = new URL(a.href, window.location.origin);
        if (url.origin !== window.location.origin) return; // skip external
        if (lang === 'zh') {
          url.searchParams.set('lang', 'zh');
        } else {
          url.searchParams.delete('lang');
        }
        a.href = url.pathname + (url.search || '') + (url.hash || '');
      } catch (_) {}
    });
  }

  // Auto-apply on DOMContentLoaded
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelectorAll('.lang-toggle-btn').forEach(btn => {
      btn.textContent = current === 'zh' ? 'EN' : '中文';
    });
    _injectLangLinks(current);
  });

  return { t, tSector, tIdx, stockName, apply, toggle, setLang, getLang };
})();
