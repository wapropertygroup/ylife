import Foundation

/// Every piece of UI copy in the app, in both languages.
///
/// One table rather than a `.xcstrings` catalogue because the language is a *product*
/// choice here, not a system one — the reader toggles it, exactly as on the web app,
/// and `Text("key")` resolving against the system locale would ignore that toggle.
///
/// Grouped by screen. Anything the server sends (role names, team names, decision
/// text, report bodies) is deliberately absent: those are already bilingual in the
/// payload, and re-translating them here would be a second source of truth that drifts
/// from what the report actually says.
enum S {
    // MARK: - Navigation

    static let agents    = LocalizedString("Agents", "智能体")
    static let markets   = LocalizedString("Markets", "市场")
    static let rates     = LocalizedString("Rates", "利率")
    static let sentiment = LocalizedString("Sentiment", "情绪")
    static let settings  = LocalizedString("Settings", "设置")
    static let holdings13f = LocalizedString("13F", "13F")
    static let fed         = LocalizedString("Fed", "美联储")
    static let valuation   = LocalizedString("Valuation", "估值")
    static let dca         = LocalizedString("DCA", "定投")

    // MARK: - 13F

    static let f13Title     = LocalizedString("Institutional holdings", "机构持仓")
    static let f13Portfolio = LocalizedString("of portfolio", "占组合")
    static let f13Filed     = LocalizedString("Filed", "申报日")
    static let f13Period    = LocalizedString("Quarter end", "报告期")
    static let f13Positions = LocalizedString("Positions", "持仓数")
    static let f13Value     = LocalizedString("Reported value", "申报市值")
    static let f13Top       = LocalizedString("Largest positions", "最大持仓")
    static let f13New       = LocalizedString("new", "新建仓")
    static let f13Added     = LocalizedString("added", "加仓")
    static let f13Trimmed   = LocalizedString("trimmed", "减仓")
    static let f13Unchanged = LocalizedString("unchanged", "未变动")
    static let f13Lag       = LocalizedString(
        "A 13F is filed up to 45 days after quarter end, so this is a snapshot of what was held then — not now.",
        "13F 最晚在季度结束后 45 天申报，因此这是当时的持仓快照，并非当前持仓。")

    // MARK: - Fed

    static let fedTitle   = LocalizedString("Balance sheet", "资产负债表")
    static let fedTotal   = LocalizedString("Total assets", "总资产")
    static let fedNote    = LocalizedString(
        "Weekly H.4.1 from FRED. Balance-sheet lines are in billions of dollars.",
        "来自 FRED 的每周 H.4.1 数据。资产负债表项目单位为十亿美元。")
    static let fedSeriesWALCL   = LocalizedString("Total assets", "总资产")
    static let fedSeriesTREAST  = LocalizedString("Treasuries held", "持有国债")
    static let fedSeriesWSHOMCB = LocalizedString("Mortgage-backed", "抵押贷款支持证券")
    static let fedSeriesWRESBAL = LocalizedString("Reserve balances", "准备金余额")
    static let fedSeriesRRPONTSYD = LocalizedString("Reverse repo", "逆回购")
    static let fedSeriesWTREGEN = LocalizedString("Treasury account", "财政部一般账户")

    // MARK: - Valuation

    static let valTitle    = LocalizedString("Index multiples", "指数估值倍数")
    static let valCape     = LocalizedString("S&P CAPE", "标普 CAPE")
    static let valCapePct  = LocalizedString("CAPE percentile", "CAPE 历史分位")
    static let valFwdSpx   = LocalizedString("S&P realised fwd", "标普实际远期")
    static let valFwdQqq   = LocalizedString("QQQ forward", "纳指 100 远期")
    static let valFwdSox   = LocalizedString("SOX forward", "费城半导体远期")
    static let valFwdN225  = LocalizedString("Nikkei forward", "日经 225 远期")
    static let valBanked   = LocalizedString("Banked forward P/E", "已记录的远期市盈率")
    static let valBankedNote = LocalizedString(
        "One row per day. Nothing sells back yesterday's consensus, so this series can only grow forward.",
        "每日一行。没有任何数据源会回卖昨天的一致预期，因此该序列只能向前累积。")

    // MARK: - DCA

    static let dcaTitle   = LocalizedString("Contribution sizing", "定投金额调节")
    static let dcaScored  = LocalizedString("scored", "已评分")
    static let dcaCheap   = LocalizedString("Cheapest against own history", "相对自身历史最便宜")
    static let dcaDear    = LocalizedString("Dearest against own history", "相对自身历史最贵")
    static let dcaPerName = LocalizedString("per name", "每只金额")
    static let dcaNotRank = LocalizedString(
        "V is not a ranking across companies. Each is scored on its own model, so 70 means cheap for this company — not cheaper than the row above.",
        "V 不是公司之间的横向排名。每家公司按各自的模型评分，因此 70 表示「相对该公司自身便宜」，而不是比上一行更便宜。")
    static let dcaUnscored = LocalizedString("not scorable", "无法评分")

    // MARK: - Shared

    static let retry        = LocalizedString("Retry", "重试")
    static let checkAgain   = LocalizedString("Check again", "稍后重试")
    static let refresh      = LocalizedString("Refresh", "刷新")
    static let loading      = LocalizedString("Loading", "加载中")
    static let warming      = LocalizedString("The server is building this data. Try again shortly.",
                                              "服务器正在生成该数据，请稍后重试。")
    static let live         = LocalizedString("Live", "实时")
    static let sessionClose = LocalizedString("At session close", "收盘价")
    static let stale        = LocalizedString("Stale", "数据陈旧")
    static let noDecision   = LocalizedString("No decision", "无结论")

    /// Cache age, formatted here rather than taken from `meta.age_label`.
    ///
    /// The server sends that label already rendered — "4m ago" — which is English
    /// on a Chinese screen and the one bit of chrome no toggle could reach. The
    /// same payload carries `age_seconds`, so the app can say it in the reader's
    /// language from the same fact. Falls back to the server's string when the
    /// seconds are absent, since a stale label beats no freshness at all.
    static let ageJustNow = LocalizedString("just now", "刚刚")
    static func ageMinutes(_ n: Int) -> LocalizedString {
        LocalizedString("\(n)m ago", "\(n) 分钟前")
    }
    static func ageHours(_ n: Int) -> LocalizedString {
        LocalizedString("\(n)h ago", "\(n) 小时前")
    }
    static func ageDays(_ n: Int) -> LocalizedString {
        LocalizedString("\(n)d ago", "\(n) 天前")
    }

    // MARK: - Agents

    static let loadingReports = LocalizedString("Loading reports", "正在加载报告")
    static let newRun         = LocalizedString("New run", "新建分析")
    static let sampleOff      = LocalizedString("The public sample is switched off",
                                                "公开样本已关闭")
    static let sampleOffHint  = LocalizedString(
        "Finished reports are still readable on the web app when signed in.",
        "登录网页版后仍可查看已完成的报告。")
    static let noReports      = LocalizedString("No sampled reports yet", "暂无样本报告")
    static let noReportsHint  = LocalizedString("A run has to finish before it can appear here.",
                                                "分析完成后才会出现在这里。")
    static let readingSample  = LocalizedString("Reading the public sample", "正在浏览公开样本")
    static let readingHint    = LocalizedString(
        "Your own runs and reports need sign-in, which is not wired up yet.",
        "查看自己的分析记录需要登录，该功能尚未接入。")
    static let degraded       = LocalizedString("Degraded", "降级运行")
    static let recovered      = LocalizedString("Recovered", "已恢复")

    // MARK: - Report

    static let loadingReport = LocalizedString("Loading report", "正在加载报告")
    static let noSections    = LocalizedString("This report has no readable sections",
                                               "该报告没有可读内容")
    static let noSectionsHint = LocalizedString("The run finished but produced no transcript.",
                                                "分析已完成，但未生成记录。")
    static let summary       = LocalizedString("Summary", "摘要")
    static let finished      = LocalizedString("Finished", "完成于")
    static let took          = LocalizedString("Took", "耗时")
    static let turns         = LocalizedString("Turns", "发言数")
    static let thinkingLabel = LocalizedString("thinking", "思考深度")

    // MARK: - Run composer

    static let run           = LocalizedString("Run", "开始分析")
    static let submitting    = LocalizedString("Submitting…", "提交中…")
    static let close         = LocalizedString("Close", "关闭")
    static let ticker        = LocalizedString("Ticker", "代码")
    static let analysisDate  = LocalizedString("Analysis date", "分析日期")
    static let modelNote     = LocalizedString(
        "Uses the deployment's default models. Choosing a model needs a server endpoint "
        + "that exposes the model table; duplicating it here would drift from what "
        + "actually runs.",
        "使用服务器的默认模型。若要选择模型，需要服务端提供模型列表接口；"
        + "在客户端复制一份会与实际运行的配置产生偏差。")
    static let queuedAs      = LocalizedString("Queued as", "已排队，任务号")

    // MARK: - Settings

    static let account       = LocalizedString("Account", "账户")
    static let signedInAs    = LocalizedString("Signed in as", "已登录")
    static let signOut       = LocalizedString("Sign out", "退出登录")
    static let signingOut    = LocalizedString("Signing out…", "正在退出…")
    static let status        = LocalizedString("Status", "状态")
    static let notSignedIn   = LocalizedString("Not signed in", "未登录")
    static let signInBlocked = LocalizedString(
        "Native sign-in needs a Google iOS OAuth client ID, which has to be created in "
        + "the Google Cloud console. The server side already works — /api/auth/google "
        + "accepts an ID token and returns a session cookie.",
        "原生登录需要在 Google Cloud 控制台创建 iOS OAuth 客户端 ID。"
        + "服务端已经就绪 —— /api/auth/google 接受 ID token 并返回会话 cookie。")
    static let backend       = LocalizedString("Backend", "后端")
    static let backendNote   = LocalizedString(
        "Both hosts serve the same Flask app — trade-agents.com proxies every path to it "
        + "rather than redirecting. Switching is for diagnosing a DNS or certificate "
        + "problem, not for changing what you see.",
        "两个域名指向同一个 Flask 应用 —— trade-agents.com 以反向代理方式转发所有路径，"
        + "而非重定向。切换仅用于排查 DNS 或证书问题，不会改变你看到的内容。")
    static let language      = LocalizedString("Language", "语言")
    static let languageNote  = LocalizedString(
        "Also the language a new report is written in: the choice is sent with the run "
        + "and frozen onto it, so a finished report keeps the language it was asked for.",
        "同时决定新报告的撰写语言：该选择会随分析请求一起提交并固定在任务上，"
        + "因此已完成的报告会保持当初请求的语言。")
    static let about         = LocalizedString("About", "关于")
    static let version       = LocalizedString("Version", "版本")
    static let platform      = LocalizedString("Platform", "平台")

    // MARK: - Rates

    static let targetRange   = LocalizedString("Target range", "目标利率区间")
    static let effective     = LocalizedString("Effective", "有效利率")
    static let midpoint      = LocalizedString("Midpoint", "中值")
    static let curveAsOf     = LocalizedString("Curve as of", "曲线日期")
    static let cut           = LocalizedString("Cut", "降息")
    static let hold          = LocalizedString("Hold", "维持")
    static let hike          = LocalizedString("Hike", "加息")
    static let noBreakdown   = LocalizedString("No probability breakdown", "无概率分布")
    static let whereRange    = LocalizedString("Where the range lands", "利率区间落点")
    static let noChange      = LocalizedString("No change", "维持不变")
    static let noMeetings    = LocalizedString("No meetings priced", "尚无会议定价")
    static let noMeetingsHint = LocalizedString("The futures curve was too sparse to derive a path.",
                                                "期货曲线数据不足，无法推导路径。")

    // MARK: - Sentiment

    static let fearGreed     = LocalizedString("Fear & Greed", "恐慌与贪婪")
    static let fearGreedHint = LocalizedString("0 is Extreme Fear, 100 is Extreme Greed.",
                                               "0 为极度恐慌，100 为极度贪婪。")
    static let comparedWith  = LocalizedString("Compared with", "历史对比")
    static let prevClose     = LocalizedString("Prev close", "前一交易日")
    static let oneWeek       = LocalizedString("1 week", "一周前")
    static let oneMonth      = LocalizedString("1 month", "一月前")
    static let oneYear       = LocalizedString("1 year", "一年前")
    static let history       = LocalizedString("History", "历史走势")
    static let noReadings    = LocalizedString("No readings available", "暂无数据")

    // MARK: - Menu bar

    static let open          = LocalizedString("Open", "打开")
    static let quit          = LocalizedString("Quit", "退出")
    static let noFinished    = LocalizedString("No finished reports", "暂无已完成的报告")
    static let unreachable   = LocalizedString("Could not reach trade-agents.",
                                               "无法连接 trade-agents。")

    // MARK: - Sign in

    static let signIn        = LocalizedString("Sign in", "登录")
    static let signInTitle   = LocalizedString("Sign in to trade-agents", "登录 trade-agents")
    static let signInIntro   = LocalizedString(
        "Signing in lets you run your own analyses and read your own reports. The public "
        + "sample is readable without an account.",
        "登录后可以运行自己的分析并查看自己的报告。公开样本无需账户即可阅读。")
    static let signInWeb     = LocalizedString("Continue with the web sign-in",
                                               "使用网页登录")
    static let openInBrowser = LocalizedString("Open in browser", "在浏览器中打开")
    static let cancel        = LocalizedString("Cancel", "取消")
    static let signedIn      = LocalizedString("Signed in", "已登录")
    static let signInFailed  = LocalizedString("Sign-in did not complete", "登录未完成")
    static let signInEmbedded = LocalizedString(
        "Google blocks its sign-in inside an embedded web view. If the page below refuses, "
        + "open it in a browser — but note that a browser session does not carry back into "
        + "this app, which is why native sign-in needs an iOS OAuth client ID.",
        "Google 禁止在内嵌网页视图中完成登录。若下方页面拒绝加载，请在浏览器中打开 —— "
        + "但浏览器中的会话无法带回本应用，这正是原生登录需要 iOS OAuth 客户端 ID 的原因。")
    static let signInHost    = LocalizedString("Signing in to", "登录到")
    static let reload        = LocalizedString("Reload page", "重新加载")

    // MARK: - Markets

    static let noPriceHistory = LocalizedString("No price history", "暂无价格历史")
    static let ytd            = LocalizedString("YTD", "年初至今")

    // Volatility. `vixTermTip` explains the one number on this screen whose
    // direction is not self-evident: the ratio is normally above 1, and below 1
    // is the stressed reading, which is the opposite of the intuition that a
    // bigger number is worse.
    static let volatility   = LocalizedString("Volatility", "波动率")
    static let vixTerm      = LocalizedString("Term", "期限结构")
    static let vixTermTip   = LocalizedString(
        "VIX3M ÷ VIX. Above 1 is the normal upward-sloping curve; below 1 means near-term fear exceeds three-month, which is the stressed reading.",
        "VIX3M ÷ VIX。大于 1 为正常的向上倾斜曲线；小于 1 表示近月恐慌高于三个月，属于紧张状态。")
    static let vixContango  = LocalizedString("Normal curve", "曲线正常")
    static let vixInverted  = LocalizedString("Inverted — near-term fear", "倒挂 — 近月恐慌")
    static let vvix         = LocalizedString("VVIX", "VVIX")
    static let twoYearRange = LocalizedString("2-year weekly", "两年周线")

    // Sector rotation.
    static let sectors      = LocalizedString("Sectors", "板块")
    static let sectorsToday = LocalizedString("Today", "今日")
    static let sectorsWeek  = LocalizedString("Week", "本周")

    /// SPDR sector names, keyed by **ticker** rather than by the English label the
    /// server sends. The label is server copy and can be reworded — "Comm." was
    /// "Communications" at one point — whereas XLK is the fund and will not
    /// change. Matching on the English string would also mean an app translating
    /// its own copy against text it does not own.
    ///
    /// An unknown ticker falls back to the server's label, so a sector added
    /// upstream appears in English rather than disappearing.
    static let sectorNames: [String: LocalizedString] = [
        "XLK":  LocalizedString("Tech", "科技"),
        "XLF":  LocalizedString("Financials", "金融"),
        "XLE":  LocalizedString("Energy", "能源"),
        "XLV":  LocalizedString("Healthcare", "医疗健康"),
        "XLI":  LocalizedString("Industrials", "工业"),
        "XLY":  LocalizedString("Consumer Disc.", "非必需消费"),
        "XLP":  LocalizedString("Consumer Stap.", "必需消费"),
        "XLU":  LocalizedString("Utilities", "公用事业"),
        "XLB":  LocalizedString("Materials", "材料"),
        "XLRE": LocalizedString("Real Estate", "房地产"),
        "XLC":  LocalizedString("Comm.", "通信"),
        "XTL":  LocalizedString("Telecom", "电信"),
    ]

    // Per-instrument context, all of it already in the payload.
    static let range52  = LocalizedString("52-week range", "52 周区间")
    static let ma50     = LocalizedString("50d", "50 日均线")
    static let ma200    = LocalizedString("200d", "200 日均线")
    static let tfDaily   = LocalizedString("1D", "日线")
    static let tfWeekly  = LocalizedString("1W", "周线")
    static let tfMonthly = LocalizedString("1M", "月线")

    // MARK: - Sentiment (empty state)

    static let noSentiment     = LocalizedString("No sentiment data", "暂无情绪数据")
    static let noSentimentHint = LocalizedString(
        "CNN returned nothing and there is no stored history yet.",
        "CNN 未返回数据，且尚无已保存的历史记录。")
    static let dailyReadings   = LocalizedString("daily readings", "个交易日数据")

    // MARK: - Launch at login (macOS)

    static let launchAtLogin     = LocalizedString("Open at login", "登录时启动")
    static let launchAtLoginNote = LocalizedString(
        "Starts the app when you log in, so the menu-bar item is there without opening "
        + "the window. Nothing runs in the background before that — alerts are only "
        + "checked while the app is open.",
        "登录 Mac 时自动启动，菜单栏图标无需打开主窗口即可使用。"
        + "在此之前不会有任何后台活动 —— 提醒仅在应用运行时检查。")
    static let launchNeedsApproval = LocalizedString(
        "Approve it in System Settings ▸ General ▸ Login Items.",
        "请在「系统设置 ▸ 通用 ▸ 登录项」中允许。")
    static let launchFailed      = LocalizedString(
        "Could not register. An app has to be signed and in /Applications for macOS to "
        + "launch it at login; a Debug build run from the build folder usually cannot.",
        "注册失败。macOS 要求应用已签名且位于 /Applications 才能开机启动；"
        + "直接从 build 目录运行的 Debug 版本通常无法注册。")
    static let openLoginItems    = LocalizedString("Open Login Items", "打开登录项设置")

    // MARK: - Notifications

    static let notifications      = LocalizedString("Notifications", "通知")
    static let notificationsNote  = LocalizedString(
        "Checked while the app is open. A report finishing is the event worth knowing "
        + "about; the others are noticed only because the app was already polling.",
        "仅在应用打开时检查。最值得关注的是分析完成；其余事件只是轮询时顺带发现的。")
    static let notifyNewReport    = LocalizedString("New report finished", "有新报告完成")
    static let notifyBigMove      = LocalizedString("Large index move", "指数大幅波动")
    static let notifyStale        = LocalizedString("Market data went stale", "行情数据已过期")
    static let dismissAll         = LocalizedString("Dismiss all", "全部忽略")
    static let noAlerts           = LocalizedString("Nothing to report", "暂无提醒")

    // Fear & Greed bands. Held here rather than as literals on the enum so the chip
    // under a Chinese headline is not the one English word on the screen.
    static let fgExtremeFear  = LocalizedString("Extreme Fear", "极度恐慌")
    static let fgFear         = LocalizedString("Fear", "恐慌")
    static let fgNeutral      = LocalizedString("Neutral", "中性")
    static let fgGreed        = LocalizedString("Greed", "贪婪")
    static let fgExtremeGreed = LocalizedString("Extreme Greed", "极度贪婪")
    static let fgReadings     = LocalizedString("daily readings", "个每日读数")

    // Basis points. "bp" is not universal notation the way "%" is — the web app
    // writes 基点 throughout, and an app reading "+12 bp" beside a page reading
    // "+12 基点" is the same figure in two vocabularies.
    static let basisPoints    = LocalizedString("bp", "基点")

    // Yield curves
    static let yieldCurve     = LocalizedString("Yield curve", "收益率曲线")
    static let curveUS        = LocalizedString("United States", "美国")
    static let curveCN        = LocalizedString("China", "中国")
    static let curveJP        = LocalizedString("Japan", "日本")
    static let spread10y3m    = LocalizedString("10Y − 3M", "10年 − 3月")
    // 10Y−2Y, which is what /api/yield-spread computes (DGS10 − DGS2). Not the same
    // pair as the curve card's own `spread_10y_3m` above it, and the two disagree by
    // half a point — so the label has to say which is which or the card reads as a
    // contradiction of the one directly above it.
    static let yieldSpread    = LocalizedString("10Y − 2Y spread", "10年期与2年期利差")
    static let inverted       = LocalizedString("Inverted", "倒挂")
    static let recessionShade = LocalizedString("Shaded: NBER recession", "阴影为 NBER 衰退期")

    // Breadth
    static let breadth        = LocalizedString("Breadth", "市场宽度")
    static let breadthAboveMa = LocalizedString("S&P 500 above moving average",
                                                "标普500成分股位于均线上方比例")
    static let breadthLatest  = LocalizedString("Above MA today", "今日位于均线上方")
    static let rspSpy         = LocalizedString("Equal weight vs cap weight (RSP/SPY)",
                                                "等权重对市值加权（RSP/SPY）")
    static let dayMa          = LocalizedString("day", "日")
    static let ofNames        = LocalizedString("names", "只成分股")

    // Options sentiment
    static let putCall        = LocalizedString("Put / call ratio", "认沽认购比")
    static let putCall20d     = LocalizedString("20-day average", "20日均值")
    static let skewIndex      = LocalizedString("SKEW index", "SKEW 指数")
    static let skewVsVix      = LocalizedString("SKEW against VIX", "SKEW 与 VIX 对比")
    static let percentileLbl  = LocalizedString("Percentile", "历史分位")
    static let skewNote       = LocalizedString(
        "SKEW prices the tail. A high reading means crash protection is dear relative "
        + "to VIX, not that a fall is expected.",
        "SKEW 反映尾部风险定价。读数偏高表示相对 VIX 而言崩盘保护更贵，并不代表预期下跌。")

    // SKEW bands, as the endpoint names them.
    static let skewLow        = LocalizedString("Low", "偏低")
    static let skewNormal     = LocalizedString("Normal", "正常")
    static let skewElevated   = LocalizedString("Elevated", "偏高")
    static let skewExtreme    = LocalizedString("Extreme", "极高")

    // Chart range buttons. Short by necessity — six of these sit in a row under
    // a chart, and a translated word would wrap the row onto two lines.
    static let range1M        = LocalizedString("1M", "1月")
    static let range3M        = LocalizedString("3M", "3月")
    static let range6M        = LocalizedString("6M", "6月")
    static let range1Y        = LocalizedString("1Y", "1年")
    static let range5Y        = LocalizedString("5Y", "5年")
    static let rangeAll       = LocalizedString("All", "全部")
    static let chartReset     = LocalizedString("Reset", "重置")
}
