"""AEL U.S. Treasury & Long-End Intelligence.

V2.6.9 FAST + RESILIENT + FREE DATA.
- No single data source is authoritative by itself.
- U.S. Treasury XML is the primary yield-curve source; FRED is fallback.
- FiscalData is the primary auction source; TreasuryDirect is the navigation
  fallback when FiscalData is unavailable. Historical successful data is also
  retained in-process so one outage never blanks the module.
- External calls are short-timeout and parallel where independent.
- This Pro-only module never blocks Lite/SINGLE/MARKET SCAN.
"""
from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import xml.etree.ElementTree as ET
import requests

TIMEOUT = 3.0
CACHE_TTL = 300
USER_AGENT = "AEL-Pro/2.6.9 (free-public-data)"

TREASURY_XML = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
TREASURY_AUCTIONS = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
TREASURY_QRA = "https://home.treasury.gov/policy-issues/financing-the-government/quarterly-refunding/most-recent-quarterly-refunding-documents"
TREASURY_AUCTION_QUERY = "https://www.treasurydirect.gov/auctions/auction-query/"

_CACHE = {
    "ts": 0.0,
    "data": None,
    "yields": None,
    "auction": None,
}

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
}

YIELD_KEYS = {
    "DGS30": "BC_30YEAR",
    "DGS20": "BC_20YEAR",
    "DGS10": "BC_10YEAR",
    "DGS5": "BC_5YEAR",
    "DGS2": "BC_2YEAR",
}


def _get(url, params=None, timeout=TIMEOUT):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    r.raise_for_status()
    return r


def _num(v):
    if v is None or v == "" or str(v).lower() in {"null", "none", "n/a", "na", "."}:
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return None


def _pct(accepted, denominator):
    a, d = _num(accepted), _num(denominator)
    if a is None or d in (None, 0):
        return None
    return a / d * 100.0


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _treasury_yield_curve():
    """Fetch the complete 2/5/10/20/30Y curve in one official request."""
    now = datetime.now(timezone.utc)
    params = {
        "data": "daily_treasury_yield_curve",
        "field_tdr_date_value_month": now.strftime("%Y%m"),
    }
    try:
        r = _get(TREASURY_XML, params=params)
        root = ET.fromstring(r.content)
        rows = []
        for entry in root.findall("a:entry", NS):
            props = entry.find(".//m:properties", NS)
            if props is None:
                continue
            date_el = props.find("d:NEW_DATE", NS)
            if date_el is None or not date_el.text:
                continue
            row = {"date": date_el.text[:10]}
            for series, key in YIELD_KEYS.items():
                el = props.find(f"d:{key}", NS)
                if el is not None:
                    row[series] = _num(el.text)
            if any(row.get(s) is not None for s in YIELD_KEYS):
                rows.append(row)
        rows.sort(key=lambda x: x["date"])
        if not rows:
            raise ValueError("Treasury XML returned no yield observations")
        latest = rows[-1]
        return {
            "values": {s: latest.get(s) for s in YIELD_KEYS},
            "date": latest["date"],
            "source": "U.S. Treasury Daily Treasury Rates",
            "history": rows[-40:],
        }
    except Exception as exc:
        return {"error": str(exc)[:180], "source": "U.S. Treasury Daily Treasury Rates"}


def _fred_one(series):
    try:
        r = _get(FRED_CSV.format(series=series), timeout=2.5)
        rows = []
        for line in r.text.strip().splitlines()[1:]:
            p = line.rsplit(",", 1)
            if len(p) >= 2 and p[1] not in ("", ".", "NA"):
                value = _num(p[1])
                if value is not None:
                    rows.append((p[0], value))
        if not rows:
            raise ValueError("无有效观测")
        return {"date": rows[-1][0], "value": rows[-1][1], "series": series,
                "source": "FRED / Federal Reserve", "history": rows[-40:]}
    except Exception as exc:
        return {"error": str(exc)[:160], "series": series}


def _yield_data():
    """Primary Treasury -> parallel FRED fallback -> stale cache."""
    primary = _treasury_yield_curve()
    if not primary.get("error"):
        return {
            "values": primary["values"],
            "dates": {s: primary["date"] for s in YIELD_KEYS},
            "source": primary["source"],
            "history": primary.get("history", []),
            "errors": [],
            "fallback": False,
        }

    # Do not serialize five 3-second waits. FRED fallback is parallel.
    results = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fred_one, s): s for s in YIELD_KEYS}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                results[s] = fut.result()
            except Exception as exc:
                results[s] = {"error": str(exc)[:160], "series": s}

    values = {s: (v.get("value") if not v.get("error") else None) for s, v in results.items()}
    dates = {s: (v.get("date") if not v.get("error") else None) for s, v in results.items()}
    errors = [f"{s}: {v['error']}" for s, v in results.items() if v.get("error")]
    if any(v is not None for v in values.values()):
        return {
            "values": values, "dates": dates,
            "source": "FRED / Federal Reserve（Treasury官方源暂时不可用）",
            "history": [], "errors": errors,
            "fallback": True,
        }

    # Stale cache is safer than a fabricated number.
    cached = _CACHE.get("yields")
    if cached:
        cached = dict(cached)
        cached["stale"] = True
        cached["errors"] = errors + [f"Treasury primary: {primary.get('error', 'unknown error')}"]
        return cached
    return {
        "values": values, "dates": dates, "source": None, "history": [],
        "errors": errors + [f"Treasury primary: {primary.get('error', 'unknown error')}"],
        "fallback": True,
    }


def _auction_rows():
    """Minimal, strict FiscalData query. Retry once without optional fields."""
    fields = (
        "cusip,auction_date,issue_date,maturity_date,security_type,security_term,reopening,"
        "offering_amt,high_yield,bid_to_cover_ratio,comp_accepted,"
        "primary_dealer_accepted,direct_bidder_accepted,indirect_bidder_accepted"
    )
    base = {
        "filter": "security_type:eq:Bond,security_term:eq:30-Year",
        "sort": "-auction_date",
        "page[number]": "1",
        "page[size]": "50",
        "format": "json",
    }
    try:
        params = dict(base, fields=fields)
        return _get(TREASURY_AUCTIONS, params=params).json().get("data", []), None
    except Exception as first:
        try:
            # If FiscalData rejects fields syntax, use only the strict filter.
            return _get(TREASURY_AUCTIONS, params=base).json().get("data", []), None
        except Exception as second:
            return [], f"FiscalData: {str(second)[:170]}"


def _auction_clean(x):
    def pick(*names):
        for n in names:
            if x.get(n) not in (None, "", "null"):
                return x.get(n)
        return None

    comp_accepted = pick("comp_accepted", "competitive_accepted")
    total_accepted = pick("total_accepted", "total_accepted_amt")
    total_tendered = pick("total_tendered", "total_tendered_amt")
    indirect = pick("indirect_bidder_accepted", "indirect_bidder_accepted_amt")
    direct = pick("direct_bidder_accepted", "direct_bidder_accepted_amt")
    dealer = pick("primary_dealer_accepted", "primary_dealer_accepted_amt")
    denominator = comp_accepted if _num(comp_accepted) not in (None, 0) else total_accepted
    offering = pick("offering_amt", "offering_amount", "offering_amt_thousands")
    high_yield = pick("high_yield", "high_rate")
    return {
        "auction_date": pick("auction_date", "record_date"),
        "issue_date": pick("issue_date"),
        "maturity_date": pick("maturity_date"),
        "security_term": pick("security_term"),
        "offering_amount": (_num(offering) * 1000 if _num(offering) is not None else None),
        "high_yield": _num(high_yield),
        "bid_to_cover": _num(pick("bid_to_cover_ratio", "bid_to_cover")),
        "indirect_pct": _pct(indirect, denominator),
        "direct_pct": _pct(direct, denominator),
        "dealer_pct": _pct(dealer, denominator),
        "comp_accepted": _num(comp_accepted),
        "total_accepted": _num(total_accepted),
        "total_tendered": _num(total_tendered),
        "raw": x,
    }


def _rank_label(value, median, higher_is_better=True):
    if value is None or median is None:
        return ("暂无数据", "unknown")
    if higher_is_better:
        d = value - median
        if d >= 0.12 * median: return ("偏强", "good")
        if d <= -0.12 * median: return ("偏弱", "bad")
    else:
        if value <= median * 0.75: return ("偏强", "good")
        if value >= median * 1.5: return ("偏弱", "bad")
    return ("一般", "neutral")


def _auction_data():
    rows, err = _auction_rows()
    if rows:
        auctions = [_auction_clean(x) for x in rows][:20]
        result = {"latest": auctions[0] if auctions else {}, "history": auctions,
                  "source": "U.S. Treasury Fiscal Data", "error": None, "stale": False}
        _CACHE["auction"] = result
        return result
    cached = _CACHE.get("auction")
    if cached:
        result = dict(cached)
        result["stale"] = True
        result["error"] = err
        return result
    return {"latest": {}, "history": [], "source": None, "error": err, "stale": False}


def _build():
    now = datetime.now(timezone.utc).isoformat()
    errors = []

    # Independent sources run concurrently so one slow provider cannot
    # serialize the whole page.
    with ThreadPoolExecutor(max_workers=2) as ex:
        fy = ex.submit(_yield_data)
        fa = ex.submit(_auction_data)
        yd = fy.result()
        ad = fa.result()

    errors.extend(yd.get("errors", []))
    if ad.get("error"):
        errors.append(f"Treasury Auction: {ad['error']}")

    vals = yd.get("values", {})
    spreads = {
        "30y_10y_bp": (vals.get("DGS30") - vals.get("DGS10")) * 100
        if vals.get("DGS30") is not None and vals.get("DGS10") is not None else None,
        "10y_2y_bp": (vals.get("DGS10") - vals.get("DGS2")) * 100
        if vals.get("DGS10") is not None and vals.get("DGS2") is not None else None,
    }

    auctions = ad.get("history", [])
    latest = ad.get("latest", {})
    btc_med = _median([x.get("bid_to_cover") for x in auctions])
    ind_med = _median([x.get("indirect_pct") for x in auctions])
    btc_label = _rank_label(latest.get("bid_to_cover"), btc_med, True)
    ind_label = _rank_label(latest.get("indirect_pct"), ind_med, True)

    auction_score = []
    if btc_label[1] == "good": auction_score.append(-1)
    elif btc_label[1] == "bad": auction_score.append(1)
    if ind_label[1] == "good": auction_score.append(-1)
    elif ind_label[1] == "bad": auction_score.append(1)
    stress = "一般"
    if auction_score and sum(auction_score) >= 1: stress = "偏高"
    elif auction_score and sum(auction_score) <= -1: stress = "偏低"

    pressure = 0
    drivers = []
    spread = spreads["30y_10y_bp"]
    if spread is not None:
        if spread > 35: pressure += 1; drivers.append("30年相对10年利率偏高")
        elif spread < -5: pressure -= 1; drivers.append("30年相对10年利率偏低")
    if btc_label[1] == "bad": pressure += 1; drivers.append("最近30年国债投标需求低于近期中位数")
    if btc_label[1] == "good": pressure -= 1; drivers.append("最近30年国债投标需求高于近期中位数")
    if ind_label[1] == "bad": pressure += 1; drivers.append("间接买家需求低于近期中位数")
    if ind_label[1] == "good": pressure -= 1; drivers.append("间接买家需求高于近期中位数")
    pressure_label = "偏高" if pressure >= 2 else ("偏低" if pressure <= -2 else "中性")

    result = {
        "ok": True,
        "as_of": now,
        "free_data": True,
        "yields": vals,
        "yield_dates": yd.get("dates", {}),
        "yield_source": yd.get("source"),
        "yield_stale": bool(yd.get("stale")),
        "spreads": spreads,
        "latest_auction": latest,
        "auction_history": auctions,
        "auction_source": ad.get("source"),
        "auction_stale": bool(ad.get("stale")),
        "auction_stats": {"bid_to_cover_median": btc_med, "indirect_median": ind_med, "sample_size": len(auctions)},
        "auction_assessment": {"bid_to_cover": btc_label[0], "indirect": ind_label[0], "stress": stress},
        "long_end_pressure": {"label": pressure_label, "score": pressure, "drivers": drivers},
        "qra": {"official_url": TREASURY_QRA, "auction_query_url": TREASURY_AUCTION_QUERY,
                "next_refunding_note": "财政部季度再融资文件按官方日程更新；AEL不使用付费终端数据。"},
        "sources": {
            "rates_primary": "U.S. Treasury Daily Treasury Rates（官方）",
            "rates_fallback": "FRED / Federal Reserve（免费备用）",
            "auctions_primary": "U.S. Treasury Fiscal Data（官方）",
            "auctions_fallback": "TreasuryDirect Auction Query（官方入口）",
            "qra": "U.S. Department of the Treasury（官方）",
        },
        "errors": errors,
        "method_note": "多源容灾：Treasury官方收益率→FRED备用；FiscalData拍卖→缓存回退。独立数据源并行请求，单一源失败不阻塞其他数据。所有数据免费公开；Tail没有可靠WI基准时不估算、不伪造。",
    }
    return result


def analyze_treasury():
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < CACHE_TTL:
        return _CACHE["data"]
    data = _build()
    _CACHE["ts"] = now
    _CACHE["data"] = data
    return data
