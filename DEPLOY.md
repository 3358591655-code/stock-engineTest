V2.5.10-PRO

# AEL V2.5.13-PRO 部署说明

## Railway
1. 解压本项目。
2. 将文件覆盖 GitHub 当前项目对应文件。
3. Commit / Push。
4. Railway 自动部署。
5. 检查 `/api/health`。
6. 打开首页测试单股查询。
7. 再测试 MARKET SCAN。
8. 测试“美股 + 半导体”和“美股 + 软件”，确认板块独立扫描。
9. 检查“今日板块表现”日期、平均涨跌幅、中位数和涨跌宽度。

## Railway 启动命令
如果项目设置了 Start Command，使用：

`uvicorn app:app --host 0.0.0.0 --port $PORT`

## 可选环境变量
`AEL_SCAN_UNIVERSE`：覆盖/扩充 MARKET SCAN 的扫描股票池，具体格式以 `app.py` 当前实现为准。

## 部署后验收顺序
1. `/api/health` 返回 ok。
2. AAPL 单股查询。
3. 检查 RSI14 / MACD / KDJ / MA / BOLL。
4. 搜索候选 Logo。
5. 点击 MARKET SCAN。
6. 分别测试 A股、港股、美股。
7. 检查每市场 TOP 20。
8. 检查综合评分排序。
9. 展开评分明细并手工复算。

## Pro 首版额外环境变量
- `ALPACA_API_KEY`：Alpaca API Key（不要写入代码，不要发到聊天）。
- `ALPACA_SECRET_KEY`：Alpaca Secret Key。
- `ALPACA_OPTIONS_FEED`：默认 `indicative`。
- `ALPACA_TRADING_BASE_URL`：可显式指定 options contracts API 基地址；未指定时由 `ALPACA_PAPER_TRADE` 决定 paper/live。
- `ALPACA_PAPER_TRADE`：默认 true；仅影响读取 options contract 元数据的 Trading API 地址，不会执行订单。
- `AEL_PRO_OPTIONS_MAX_SNAPSHOTS`：默认 2500。
- `AEL_PRO_OPTIONS_MAX_CONTRACTS`：默认 5000。

## Pro V2.5.13 验收
1. Options 页面手机端候选应使用卡片，不得出现页面级横向溢出。
2. CSP / CC 都能扫描。
3. 手续费、资金占用模式、排序均可改变结果展示。
4. OPEN / WAIT 原因与规则显示。
5. 数据状态显示 IV / Delta / Quotes / Greeks 可用数量。
6. 缺失 IV/Greeks 不估算。

## Pro 验收
1. Lite 首页默认打开且无需触发任何 Pro 请求。
2. 切换 Pro 不触发 MARKET SCAN。
3. Pro Overview 可以只读加载 Lite 核心数据。
4. Options → AAPL → CSP/CC 可以在配置 Alpaca 后请求真实链。
5. 无 Alpaca 密钥时仅 Pro Options 报配置错误，Lite 正常。
6. Pro 不存在下单接口；首版只做研究/行情。

### Optional FINRA ATS evidence

Set `FINRA_API_TOKEN` as a Railway environment variable if you have an authorized FINRA API token. This is optional and does not affect Lite or the rest of Pro. If omitted, the ATS evidence layer stays `暂无数据` rather than using a third-party proxy.


### V2.5.24 免费数据源增强

买方预期模块现在优先使用公开/免费数据：
- Yahoo/yfinance：价格、成交量、期权、下一份财报、盈利趋势、卖方目标价参考。
- SEC Form 13F：最新季度机构持仓证据；首次查询可能需要下载约 96 MB 的 SEC 季度数据集，随后本地缓存。SEC 13F 是滞后披露，不是实时仓位。
- CFTC COT：适用于期货标的；股票显示“不适用”。CFTC 当前公开 API 不要求 token。
- xStocks：公开资产元数据。
- 可选免费 Key：`ALPHAVANTAGE_API_KEY`、`FINNHUB_API_KEY`，用于增强盈利预测/财报数据；缺少 Key 不影响其他 Pro 模块。
- 可选 FINRA：`FINRA_API_TOKEN` 用于 ATS/OTC 周度证据；FINRA 当前 API 文档要求 Bearer token。未配置时保持“需免费令牌/不可用”，绝不使用第三方代理冒充暗池。

所有新增源均只在 `/api/pro/expectation/*` 按用户主动点击时加载，不进入 Lite 或 MARKET SCAN。
