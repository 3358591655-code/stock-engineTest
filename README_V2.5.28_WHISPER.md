# AEL V2.5.28 — MARKET-IMPLIED WHISPER™

## 本版目标
解决 V2.5.27 的核心问题：Yahoo earningsTrend 失败时，AEL 不再因为缺少 sell-side consensus 而输出空白。

### 数据优先级
1. Earnings Whispers Data API（仅当用户配置合法 `EW_API_KEY` 时，作为授权参考）
2. Financial Modeling Prep（`FMP_API_KEY`）
3. Alpha Vantage Earnings Estimates（`ALPHAVANTAGE_API_KEY`）
4. Finnhub Earnings Calendar（`FINNHUB_API_KEY`）
5. Yahoo Finance earningsTrend
6. AEL 历史季度基准模型（无需 analyst consensus）

## 核心变化
- EPS / Revenue 分字段选择可用数据源，不再要求同一个 API 同时成功。
- 没有 consensus 时，用历史季度季节性 + 最近增长趋势建立 `AEL Base Estimate`。
- 在 Base Estimate 上叠加：历史 earnings surprise、预测修正、财报前价格动量、成交量、事件期权 IV / skew，形成 `AEL Market-Implied Whisper`。
- AEL 的数值始终标记为 inferred，不冒充 Earnings Whispers 私有 Whisper。
- 如果配置 `EW_API_KEY`，同时返回官方 Whisper EPS 作为独立校准参考；不会把它偷偷当成 AEL 自己的模型输出。
- 所有外部请求独立 timeout / fail-open / cache，不进入 Lite 主链。

## Railway 环境变量（可选）
```text
EW_API_KEY=...
FMP_API_KEY=...
ALPHAVANTAGE_API_KEY=...
FINNHUB_API_KEY=...
```

建议至少配置 `FMP_API_KEY` 或 `ALPHAVANTAGE_API_KEY`，这样 sell-side/base layer 的稳定性会明显高于单一 Yahoo。

## 结果定义
- `consensus`: 可验证卖方/市场共识（如果有）
- `base`: AEL 基准；无 consensus 时来自历史季度模型
- `implied`: AEL 当前 Market-Implied Whisper
- `implied_surprise_pct`: 相对于 base/consensus 的 Price-in 门槛
- `market_beat_threshold`: 与 `implied` 相同，表示模型估计市场已经提前要求的业绩水平


### V2.6.1 Quarter Lock
AEL now enforces a strict fiscal-quarter gate before consensus selection. The latest reported fiscal period is used only to derive the target next quarter; provider estimates must expose a matching fiscal period or a matching earnings-report date. Stale estimates from the just-reported quarter are rejected instead of being paired with the next quarter's revenue.
