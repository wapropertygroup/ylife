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

    // MARK: - Markets

    static let noPriceHistory = LocalizedString("No price history", "暂无价格历史")
    static let ytd            = LocalizedString("YTD", "年初至今")

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
}
