# V2.6.9 Test Report

- Python compile: PASS
- Inline frontend JavaScript syntax: PASS
- Treasury XML yield-curve parser: PASS (mocked official XML schema)
- 2Y/5Y/10Y/20Y/30Y mapping: PASS
- FiscalData 30Y strict filter: implemented
- FiscalData minimal-field retry: implemented
- Offering amount conversion: PASS (mocked official schema)
- Bidder percentage derivation: PASS (accepted amount / competitive accepted)
- Independent yield/auction execution: PASS by ThreadPoolExecutor design
- Stale-cache fallback: implemented
- Raw provider exception hidden from frontend: PASS by code inspection
- No paid API key required: PASS

Runtime network access from the build container is unavailable, so live Treasury/FiscalData endpoints were not called during local tests. Railway runtime should make the actual public-data requests.
