# V2.6.11 — Lite Scan + Morningstar Strict Repair

- Pro code/routes are read-only and unchanged.
- Lite Market Scan: US sector metadata now comes from public S&P 500/Nasdaq-100 GICS datasets with fallback to existing index source.
- Lite scan history reduced to 1y (enough for 52-week/250-day technical context; missing long-window fields are not fabricated).
- Bulk Yahoo screener enrichment replaces most per-symbol Ticker.info calls.
- Batch history timeout reduced to 8s; empty-batch return shape fixed.
- Morningstar lookup remains explicit-source-only, with shorter timeouts and negative-result cache; no rating is inferred.

- Morningstar: added direct public `www.morningstar.com/api/v2/search` resolver for explicit `starRating`, before Yahoo/public quote fallbacks.
