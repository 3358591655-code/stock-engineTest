# AEL V2.6.6 — MARKET-IMPLIED WHISPER

本版在 V2.6.2 Smart Quarter Resolver 基础上加入公司级 Analyst Bias 校准。

三层：SELL-SIDE CONSENSUS → AEL WHISPER → AEL MARKET-IMPLIED。

AEL Whisper 不声称读取私人 Earnings Whispers 数据；它用公开 consensus、近期 revision、guidance、经收缩的历史共识偏差和 fundamental nowcast 模拟 analyst-like expectation。
# AEL V2.5.19.2｜Pro 宏观 / Fed / 市场预期版

## 这一版做什么
- 修复 Pro Macro/Fed 的单一数据源故障隔离。
- 关键数据增加官方直连：美国财政部、纽约联储、BLS、Federal Reserve。
- FRED 保留为历史序列与备用源，不再作为整个宏观页面的单点依赖。
- 新增 Polymarket Fed 市场预期：只展示市场隐含概率，不等同于 Federal Reserve 官方预测，并将事件与结果中文化。
- Macro/Fed UI 改为中文、移动端优先、先看结论再看细节；FOMC 月份/会议日历/Polymarket 事件全部中文展示。
- 技术错误默认折叠，不再把 HTTPS/traceback 直接铺在主页面。
- Risk Lab 重构为“风险体检”：单股体检 + 组合体检，加入最大回撤、历史单日VaR、Beta/相关性、集中度与可选组合金额换算。
- Lite 冻结：`metrics.py`、`requirements.txt` SHA256 与基线一致。

## 上传顺序
ZIP 已按上传便利性排序：
1. `app.py`
2. `requirements.txt`
3. `metrics.py`
4. `pro_macro.py`
5. `pro_factor.py`
6. `pro_risk.py`
7. `pro_options.py`
8. `static/index.html`
9. 文档

## 关键接口
- `GET /api/health`
- `GET /api/pro/macro`
- `GET /api/pro/factors/analyze/{symbol}`
- `GET /api/pro/risk/analyze/{symbol}`
- `GET /api/pro/risk/portfolio`

## 数据原则
- 真实数据优先。
- 缺失 = `暂无数据`。
- 不用旧值冒充最新值。
- Polymarket 只作为市场预期层，不写入 Lite 股票评分。
- Macro Regime 只描述环境，不自动给股票加减分。

## 部署后首测
1. `/api/health`
2. Pro → 宏观 → 刷新宏观数据
3. 检查 Federal Reserve / Treasury / NY Fed / BLS / Polymarket 数据源状态
4. 检查 Factor / Risk
5. 切回 Lite，确认 SINGLE 与 MARKET SCAN 不受影响

## Pro Buy-Side Expectation / ATS Evidence

- `GET /api/pro/expectation/{symbol}` remains on-demand and isolated from Lite.
- Pro 买方预期优先使用免费/公开源：Yahoo/yfinance、SEC 13F、CFTC COT、xStocks；可选 `FINNHUB_API_KEY` / `ALPHAVANTAGE_API_KEY` 增强盈利数据；FINRA ATS 需要 `FINRA_API_TOKEN`，不配置时不会伪造暗池数据。
- FINRA data is weekly/delayed and does not reveal trade direction; AEL therefore exposes ATS share/activity and an `inferred` activity score, never a claim of net dark-pool buying.
- Missing token, timeout, empty data, or source failure affects only the ATS evidence card; other Pro/Lite data continues normally.

- V2.5.24: Buy-Side Expectation expanded with free/public earnings-trend, SEC 13F quarterly evidence, optional free-key Alpha Vantage/Finnhub earnings evidence, explicit not-applicable states, and source-status diagnostics. These sources are isolated from Lite and are not used to fabricate private buy-side order books.

## V2.6.4 — Whisper Backtest & Calibration

The Whisper module now has an isolated historical validation layer:

- `GET /api/pro/whisper/backtest/{symbol}?quarters=20`
- Consensus vs AEL Whisper vs Actual
- EPS / Revenue MAE, MAPE, RMSE
- Whisper Edge (error reduction versus consensus)
- Historical Calibration score (not a probability)
- Point-in-time replay guard and explicit exclusion of missing historical estimates
- Historical Market-Implied is not fabricated when verifiable historical option snapshots are unavailable

The backtest is optional and never runs on Lite, SINGLE, or MARKET SCAN requests.


## V2.6.6 Confidence
The Whisper confidence field is now uncalibrated until the user explicitly runs the historical Whisper Backtest Lab. Historical Calibration is a score, not a probability. The completed backtest feeds an isolated calibration cache; the live Whisper endpoint reads that cache without launching a historical backtest, preserving Lite/SINGLE/MARKET SCAN latency.

---

## V2.6.43 双重重力历史代理数据与探索性对照

版本标记：`2.6.43-DOUBLE-GRAVITY-DATA`。

新增独立 Pro 研究接口：
- `GET /api/pro/gravity/status`
- `GET /api/pro/gravity/history?start_year=1997&end_year=2024`
- `GET /api/pro/gravity/backtest?horizon_years=1&min_institutions=1`
- `GET /api/pro/gravity/calculate`（手动情景计算）

`data/gravity/` 中的年度面板和信号比较仅是历史代理/探索性数据。正式Theta仍为0条；正式数据未通过日期、来源、期限匹配与验证闸门前，网站必须显示“暂无数据”，不能把代理对照宣传为正式回测结论。Lite、Whisper、ATS、Options等现有业务逻辑保持独立。
