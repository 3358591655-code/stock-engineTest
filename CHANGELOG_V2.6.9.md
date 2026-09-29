# AEL V2.6.9 — TREASURY FAST + RESILIENT + FREE DATA

## 修复
- 修复 FRED DGS30/DGS20/DGS10/DGS5/DGS2 串行 8 秒超时导致页面卡顿的问题。
- 收益率主源改为美国财政部官方 Daily Treasury Rates XML：一次请求取得 2Y/5Y/10Y/20Y/30Y。
- FRED 改为并行备用源；单一 FRED 失败不会阻塞页面。
- FiscalData 30Y Auction 改为严格 `Bond + 30-Year` 查询，并减少字段；若字段查询被拒绝，自动用最小参数重试。
- 拍卖数据独立于收益率数据；任一数据源失败不会拖垮另一模块。
- 引入最近成功数据缓存；实时源暂时不可用时显示缓存并明确标注，不伪造数据。
- 外部请求超时缩短为约 3 秒；FRED 备用单请求约 2.5 秒，并行执行。
- 前端不再向用户展示 `HTTPSConnectionPool` 等原始异常，只显示“部分数据源异常，已自动隔离”。
- 保持 `/api/pro/treasury` Pro 模块隔离，不改变 Lite / SINGLE / MARKET SCAN。

## 数据原则
主源 + 备用源 + 缓存；公开免费数据；期间/口径不混用；缺失时不估算、不伪造。
