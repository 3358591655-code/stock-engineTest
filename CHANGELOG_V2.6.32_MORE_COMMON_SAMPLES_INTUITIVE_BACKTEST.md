# AEL V2.6.32 — More Common Samples + Intuitive Fair Backtest

## 1. More honest common samples
- Fair backtest now supplements the production FINRA weeklySummary rolling 12-month data with FINRA weeklySummaryHistoric, the official rolling 4-year historical dataset.
- Historical data is requested only for months containing matched earnings events and cached, rather than downloading the entire history.
- Historical ATS evidence is filtered to information available before the earnings event; no post-event ATS data is allowed.
- The fair-backtest selector still allows up to 40 quarters, but the verifiable common sample is bounded by the historical ATS coverage and other source availability.

## 2. Backtest display is now intuitive
The primary result no longer presents naked Price-in percentages as if they were self-explanatory scores. It shows:
- common verifiable quarters
- AEL average deviation from the eventual reported revenue
- ATS average deviation from the eventual reported revenue
- direction: overestimate / underestimate
- absolute deviation in billions and signed percentage

The table shows, for each quarter:
management guidance → AEL revenue already priced in → ATS revenue already priced in → actual revenue → AEL deviation → ATS deviation.

## 3. Existing algorithms unchanged
- `pro_expectation.py` unchanged.
- `pro_whisper.py` unchanged.
- Current ATS runtime calculation remains unchanged; historical ATS retrieval is an isolated fair-backtest sidecar.
