"""Automatic AEL Pro gravity dashboard.

All valuation inputs are fetched from public sources; the interface has no
manual ERP/g data entry. Buffett's indicator is the US corporate-equity/GDP
ratio. CAPE is Robert Shiller's official US (S&P 500) CAPE series. Because CAPE
is not published there as a comparable Nasdaq/Dow series, the UI keeps it
explicitly labelled as a common US-market benchmark rather than falsely
relabeling it as index-specific.

The institutional expectation archive is fetched in the background from the
public Dahlquist-Ibert Mendeley dataset. Raw field mapping is conservative:
only explicit ERP or same-row equity-minus-cash and clearly forward-looking
growth fields qualify. Other fields are never silently promoted to ERP/g.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import statistics
from calendar import month_abbr, month_name
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import pandas as pd
except Exception:  # a useful status is returned if optional Excel support is missing
    pd = None

BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / "data" / "gravity" / "auto_cache"
SOURCE_ARCHIVE = CACHE_DIR / "dahlquist_ibert_replication.zip"
SOURCE_MANIFEST = CACHE_DIR / "institution_source_status.json"
SHILLER_URLS = [
    "https://www.econ.yale.edu/~shiller/data/ie_data.xls",
    "https://www.econ.yale.edu/~shiller/data/ie_data.xlsx",
]
MENDELEY_META_URL = "https://api.data.mendeley.com/datasets/xtx7tg6jpx/zip?version=2"
MENDELEY_DATASET_URL = "https://data.mendeley.com/datasets/xtx7tg6jpx/2"
FRED_GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
INDEXES = {
    "SP500": {"symbol": "^GSPC", "name": "标普 500"},
    "NASDAQ": {"symbol": "^IXIC", "name": "纳斯达克综合指数"},
    "DOW": {"symbol": "^DJI", "name": "道琼斯工业平均指数"},
}
# Public FRED series; equities are millions of USD and GDP is billions of USD.
FRED_SERIES = {
    "nonfinancial_equities": "NCBEILQ027S",
    "financial_equities": "FBCELLQ027S",
    "nominal_gdp": "GDP",
    "real_yield_10y": "DFII10",
    "nominal_yield_10y": "DGS10",
    "money_stock_m1": "M1SL",
    "money_velocity_m1": "M1V",
    "price_level_gdp_deflator": "GDPDEF",
    "real_gdp": "GDPC1",
    "corporate_profit_after_tax": "CPATAX",
}
TTL_SECONDS = int(os.getenv("AEL_GRAVITY_CACHE_TTL", "21600"))
_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": "Mozilla/5.0 (compatible; AELResearch/2.6.43; +https://github.com/)"})
_LOCK = threading.RLock()
_BUYER_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}
_BUYER_WORKER: threading.Thread | None = None
_BUYER_STATUS: dict[str, Any] = {"status": "NOT_STARTED", "message": "机构预期数据源尚未自动读取。"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() in {"", ".", "NA", "N/A", "nan", "NaN"}:
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _percentile(value: float | None, values: list[float]) -> float | None:
    if value is None or not values:
        return None
    return round(100.0 * sum(1 for x in values if x <= value) / len(values), 2)


def _series_csv(series_id: str, force: bool = False) -> list[dict[str, Any]]:
    key = f"fred:{series_id}"
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not force and time.time() - cached["ts"] < TTL_SECONDS:
            return cached["value"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk = CACHE_DIR / f"fred_{series_id}.json"
    old: list[dict[str, Any]] = []
    if disk.exists():
        try:
            payload = json.loads(disk.read_text(encoding="utf-8"))
            old = payload.get("rows", [])
            if not force and time.time() - float(payload.get("cached_at_epoch", 0)) < TTL_SECONDS:
                with _LOCK:
                    _CACHE[key] = {"ts": float(payload.get("cached_at_epoch", time.time())), "value": old}
                return old
        except Exception:
            old = []
    try:
        response = _HTTP.get(FRED_GRAPH_URL, params={"id": series_id}, timeout=(5, 14))
        response.raise_for_status()
        text = response.content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw in reader:
            dt = str(raw.get("observation_date") or raw.get("DATE") or raw.get("date") or "")[:10]
            value_col = next((k for k in raw if k and k.lower() not in {"observation_date", "date"}), None)
            val = _finite(raw.get(value_col)) if value_col else None
            try:
                date.fromisoformat(dt)
            except ValueError:
                continue
            if val is not None:
                rows.append({"date": dt, "value": val})
        if not rows:
            raise ValueError(f"FRED {series_id} 返回空序列")
        payload = {"series_id": series_id, "source": f"FRED {series_id}", "cached_at": _now_utc(), "cached_at_epoch": time.time(), "rows": rows}
        disk.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with _LOCK:
            _CACHE[key] = {"ts": time.time(), "value": rows}
        return rows
    except Exception as exc:
        if old:
            with _LOCK:
                _CACHE[key] = {"ts": time.time(), "value": old}
            return old
        raise RuntimeError(f"FRED {series_id} 获取失败：{str(exc)[:180]}") from exc


def _parse_shiller_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().replace(day=1)
    if isinstance(value, date):
        return value.replace(day=1)
    num = _finite(value)
    if num is not None and 1800 <= num < 2200:
        # Shiller's historic workbook usually encodes month as 1871.01, 1871.02, ...
        yr = int(num)
        frac = num - yr
        # Shiller workbook commonly uses YYYY.MM; tolerate year-fraction encoding too.
        month_candidates = [int(round(frac * 100)), int(round(frac * 12)) + 1]
        for month in month_candidates:
            if 1 <= month <= 12:
                try:
                    return date(yr, month, 1)
                except ValueError:
                    pass
    text = str(value).strip()
    m = re.match(r"^(\d{4})[./-](\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), 1)
        except ValueError:
            return None
    # Multpl commonly uses formats such as "Oct 2026", "October 1, 2026",
    # or "Oct 1, 2026". The old parser silently rejected all of these.
    for fmt in ("%b %Y", "%B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).date().replace(day=1)
        except ValueError:
            pass
    return None


def _normal_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _read_shiller_bytes(blob: bytes) -> list[dict[str, Any]]:
    if pd is None:
        raise RuntimeError("缺少 pandas，无法读取 Shiller Excel 数据")
    # Shiller's URL may redirect or change file format. Detect the actual payload,
    # instead of forcing xlrd for both .xls and .xlsx downloads.
    engine = "openpyxl" if blob[:2] == b"PK" else "xlrd"
    workbook = pd.ExcelFile(io.BytesIO(blob), engine=engine)
    output: dict[str, dict[str, Any]] = {}
    for sheet in workbook.sheet_names:
        frame = pd.read_excel(io.BytesIO(blob), sheet_name=sheet, header=None, engine=engine)
        frame = frame.replace({"": None})
        if frame.empty:
            continue
        header_row = None
        headers: list[str] = []
        for idx in range(min(35, len(frame))):
            vals = [str(x).strip() if x is not None and not (isinstance(x, float) and math.isnan(x)) else "" for x in frame.iloc[idx].tolist()]
            norm = [_normal_header(x) for x in vals]
            date_idx = next((i for i, h in enumerate(norm) if h in {"date", "month", "observationdate"}), None)
            cape_idx = next((i for i, h in enumerate(norm) if h in {"cape", "shillerpe", "cyclicallyadjustedpe", "cyclicallyadjustedpriceearnings"}), None)
            if date_idx is not None and cape_idx is not None:
                header_row, headers = idx, vals
                break
        if header_row is None:
            continue
        normalized = [_normal_header(x) for x in headers]
        date_idx = next(i for i, h in enumerate(normalized) if h in {"date", "month", "observationdate"})
        cape_idx = next(i for i, h in enumerate(normalized) if h in {"cape", "shillerpe", "cyclicallyadjustedpe", "cyclicallyadjustedpriceearnings"})
        earnings_idx = next((i for i, h in enumerate(normalized) if h in {"realearnings", "realearningsper share".replace(" ", ""), "earningsreal", "realearningsper-share".replace("-", "")}), None)
        # Alternative total-return CAPE columns sometimes coexist with price-only CAPE.
        for _, series in frame.iloc[header_row + 1:].iterrows():
            vals = series.tolist()
            if date_idx >= len(vals) or cape_idx >= len(vals):
                continue
            dt = _parse_shiller_date(vals[date_idx])
            cape = _finite(vals[cape_idx])
            real_earnings = _finite(vals[earnings_idx]) if earnings_idx is not None and earnings_idx < len(vals) else None
            if dt is None or cape is None or cape <= 0:
                continue
            key = dt.isoformat()
            output[key] = {"date": key, "cape": cape, "real_earnings": real_earnings,
                           "source": "Robert Shiller official ie_data.xls", "source_url": SHILLER_URLS[0]}
    rows = [output[k] for k in sorted(output)]
    if len(rows) < 120:
        raise ValueError("Shiller 文件中未找到可识别的 Date/CAPE 月度历史列")
    # Some workbook formats carry earnings on a separate row/column. Do not infer it if absent.
    for i, row in enumerate(rows):
        row["real_earnings_growth_10y_pct"] = None
        if row.get("real_earnings") is None or row["real_earnings"] <= 0:
            continue
        target = date(row["_year"], row["_month"], 1) if "_year" in row else date.fromisoformat(row["date"])
        candidates = []
        for prior in rows:
            if prior["date"] >= row["date"] or prior.get("real_earnings") is None or prior["real_earnings"] <= 0:
                continue
            pdate = date.fromisoformat(prior["date"])
            months = (target.year - pdate.year) * 12 + target.month - pdate.month
            if 118 <= months <= 122:
                candidates.append((abs(months - 120), prior))
        if candidates:
            prior = min(candidates, key=lambda x: x[0])[1]
            years = (target - date.fromisoformat(prior["date"])).days / 365.2425
            if years >= 9.5:
                row["real_earnings_growth_10y_pct"] = (row["real_earnings"] / prior["real_earnings"]) ** (1.0 / years) * 100.0 - 100.0
    return rows


def _cape_history(force: bool = False) -> list[dict[str, Any]]:
    key = "shiller:cape"
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not force and time.time() - cached["ts"] < TTL_SECONDS:
            return cached["value"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk = CACHE_DIR / "shiller_cape.json"
    old: list[dict[str, Any]] = []
    if disk.exists():
        try:
            payload = json.loads(disk.read_text(encoding="utf-8"))
            old = payload.get("rows", [])
            if not force and time.time() - float(payload.get("cached_at_epoch", 0)) < TTL_SECONDS:
                with _LOCK:
                    _CACHE[key] = {"ts": float(payload.get("cached_at_epoch", time.time())), "value": old}
                return old
        except Exception:
            old = []
    errors = []
    for url in SHILLER_URLS:
        try:
            response = _HTTP.get(url, timeout=(6, 16))
            response.raise_for_status()
            rows = _read_shiller_bytes(response.content)
            payload = {"source_url": url, "cached_at": _now_utc(), "cached_at_epoch": time.time(), "rows": rows}
            disk.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with _LOCK:
                _CACHE[key] = {"ts": time.time(), "value": rows}
            return rows
        except Exception as exc:
            errors.append(f"{url}: {str(exc)[:120]}")
    if old:
        with _LOCK:
            _CACHE[key] = {"ts": time.time(), "value": old}
        return old
    # Live-page fallback: still external/source-attributed, never hardcode a value.
    try:
        response = _HTTP.get("https://www.multpl.com/shiller-pe/table/by-month", timeout=(5, 12))
        response.raise_for_status()
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", response.text, re.I | re.S):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)
            if len(cells) < 2:
                continue
            clean = [re.sub(r"<[^>]+>", "", x).replace("&nbsp;", " ").strip() for x in cells]
            dt = _parse_shiller_date(clean[0])
            val = _finite(re.sub(r"[$,\s]", "", clean[1]))
            if dt and val and val > 0:
                rows.append({"date": dt.isoformat(), "cape": val, "real_earnings": None,
                             "real_earnings_growth_10y_pct": None, "source": "Multpl CAPE history fallback",
                             "source_url": "https://www.multpl.com/shiller-pe/table/by-month"})
        if len(rows) >= 120:
            rows.sort(key=lambda r: r["date"])
            disk.write_text(json.dumps({"source_url": rows[-1]["source_url"], "cached_at": _now_utc(), "cached_at_epoch": time.time(), "rows": rows}, ensure_ascii=False), encoding="utf-8")
            with _LOCK:
                _CACHE[key] = {"ts": time.time(), "value": rows}
            return rows
    except Exception as exc:
        errors.append(f"Multpl fallback: {str(exc)[:120]}")
    raise RuntimeError("CAPE 历史数据源不可用；" + "；".join(errors))


def _yahoo_history(symbol: str, start: date = date(1950, 1, 1), force: bool = False) -> list[dict[str, Any]]:
    key = f"yahoo:{symbol}:{start.isoformat()}"
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not force and time.time() - cached["ts"] < TTL_SECONDS:
            return cached["value"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = re.sub(r"[^A-Za-z0-9^.=\-]", "", symbol)
    disk = CACHE_DIR / f"yahoo_{safe_symbol.replace('^','idx_')}_{start.year}.json"
    old = []
    if disk.exists():
        try:
            payload = json.loads(disk.read_text(encoding="utf-8"))
            old = payload.get("rows", [])
            if not force and time.time() - float(payload.get("cached_at_epoch", 0)) < TTL_SECONDS:
                with _LOCK:
                    _CACHE[key] = {"ts": float(payload.get("cached_at_epoch", time.time())), "value": old}
                return old
        except Exception:
            old = []
    try:
        p1 = int(datetime(start.year, 1, 1, tzinfo=timezone.utc).timestamp())
        now = datetime.now(timezone.utc)
        p2 = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()) + 86400
        response = _HTTP.get(YAHOO_CHART_URL.format(symbol=requests.utils.quote(symbol, safe="")),
                             params={"period1": p1, "period2": p2, "interval": "1d", "events": "history", "includeAdjustedClose": "true"}, timeout=(6, 16))
        response.raise_for_status()
        result = ((response.json().get("chart") or {}).get("result") or [None])[0]
        if not result:
            raise ValueError("Yahoo Finance 未返回历史指数数据")
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quote = (indicators.get("quote") or [{}])[0] or {}
        close = quote.get("close") or []
        adj = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
        rows = []
        for i, ts in enumerate(timestamps):
            if i >= len(close):
                continue
            raw = adj[i] if i < len(adj) and adj[i] is not None else close[i]
            v = _finite(raw)
            if v is None or v <= 0:
                continue
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
            rows.append({"date": day, "value": v, "kind": "adjclose" if i < len(adj) and adj[i] is not None else "close"})
        if not rows:
            raise ValueError("Yahoo Finance 指数历史为空")
        payload = {"cached_at_epoch": time.time(), "cached_at": _now_utc(), "symbol": symbol, "rows": rows}
        disk.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with _LOCK:
            _CACHE[key] = {"ts": time.time(), "value": rows}
        return rows
    except Exception as exc:
        if old:
            with _LOCK:
                _CACHE[key] = {"ts": time.time(), "value": old}
            return old
        raise RuntimeError(f"Yahoo {symbol} 历史行情获取失败：{str(exc)[:160]}") from exc


def _build_buffett_history(nf: list[dict[str, Any]], financial: list[dict[str, Any]], gdp: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nmap = {r["date"]: r["value"] for r in nf}
    fmap = {r["date"]: r["value"] for r in financial}
    gmap = {r["date"]: r["value"] for r in gdp}
    common = sorted(set(nmap) & set(fmap) & set(gmap))
    rows = []
    for dt in common:
        # Equity liabilities are millions of USD; nominal GDP is billions USD, annualized.
        cap_m = nmap[dt] + fmap[dt]
        gd_b = gmap[dt]
        if cap_m <= 0 or gd_b <= 0:
            continue
        ratio = cap_m / (gd_b * 1000.0) * 100.0
        if math.isfinite(ratio):
            rows.append({"date": dt, "value": ratio, "market_cap_millions": cap_m,
                         "gdp_billions_annualized": gd_b, "source": "Federal Reserve Z.1/FRED", 
                         "available_date_proxy": (date.fromisoformat(dt) + timedelta(days=75)).isoformat()})
    return rows


def _load_world_bank_buffett(force: bool = False) -> dict[str, Any]:
    """Annual listed-domestic-market-cap/GDP series; a transparent Buffett-type measure.

    World Bank indicator CM.MKT.LCAP.GD.ZS is already expressed as percent of GDP.
    It is annual and not marked-to-market daily, which is preferable to presenting
    a broad Z.1 corporate-equity-liability proxy as a precise current Buffett ratio.
    """
    key = "worldbank:listed_market_cap_gdp"
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not force and time.time() - cached["ts"] < TTL_SECONDS:
            return cached["value"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk = CACHE_DIR / "worldbank_buffett.json"
    if disk.exists() and not force:
        try:
            payload = json.loads(disk.read_text(encoding="utf-8"))
            if time.time() - float(payload.get("cached_at_epoch", 0)) < TTL_SECONDS and payload.get("history"):
                result = payload
                with _LOCK:
                    _CACHE[key] = {"ts": float(payload["cached_at_epoch"]), "value": result}
                return result
        except Exception:
            pass
    url = "https://api.worldbank.org/v2/country/USA/indicator/CM.MKT.LCAP.GD.ZS?format=json&per_page=100"
    response = _HTTP.get(url, timeout=(5, 18), headers={"Accept": "application/json"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        raise ValueError("世界银行上市公司市值/GDP接口返回结构异常")
    history = []
    for row in payload[1]:
        year = str(row.get("date", ""))
        value = _finite(row.get("value"))
        if not re.fullmatch(r"\d{4}", year) or value is None or value <= 0:
            continue
        # Do not invent the exact publication date. The value's observation date is
        # year-end, and the UI labels it as annual World Bank data.
        history.append({"date": f"{year}-12-31", "year": int(year), "value": value,
                        "available_date_proxy": f"{int(year)+2}-01-01",
                        "source": "World Bank CM.MKT.LCAP.GD.ZS",
                        "source_url": "https://data.worldbank.org/indicator/CM.MKT.LCAP.GD.ZS?locations=US"})
    history.sort(key=lambda r: r["date"])
    if len(history) < 10:
        raise ValueError("世界银行上市公司市值/GDP有效历史不足10年")
    latest = history[-1]
    result = {"current": {**latest, "method": "世界银行美国上市国内公司市值/GDP年度指标；不是每日估算"},
              "history": history, "source": "World Bank CM.MKT.LCAP.GD.ZS",
              "definition_warning": "年度上市国内公司市值/GDP指标；公布滞后，不是实时日频指标。",
              "frequency": "annual", "errors": []}
    saved = {**result, "cached_at": _now_utc(), "cached_at_epoch": time.time()}
    disk.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    with _LOCK:
        _CACHE[key] = {"ts": time.time(), "value": result}
    return result


def _load_buffett(force: bool = False) -> dict[str, Any]:
    key = "buffett:series"
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not force and time.time() - cached["ts"] < TTL_SECONDS:
            return cached["value"]
    errors = []
    try:
        return _load_world_bank_buffett(force)
    except Exception as exc:
        errors.append(f"World Bank listed market cap/GDP: {str(exc)[:150]}")
    tasks = {
        "nf": lambda: _series_csv(FRED_SERIES["nonfinancial_equities"], force),
        "financial": lambda: _series_csv(FRED_SERIES["financial_equities"], force),
        "gdp": lambda: _series_csv(FRED_SERIES["nominal_gdp"], force),
        "sp500": lambda: _yahoo_history("^GSPC", date(1945, 1, 1), force),
    }
    outputs = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(future_map, timeout=24):
            name = future_map[future]
            try:
                outputs[name] = future.result()
            except Exception as exc:
                errors.append(f"{name}: {str(exc)[:120]}")
    if not all(k in outputs for k in ("nf", "financial", "gdp", "sp500")):
        raise RuntimeError("巴菲特指标源数据获取失败：" + "；".join(errors))
    history = _build_buffett_history(outputs["nf"], outputs["financial"], outputs["gdp"])
    if len(history) < 25:
        raise RuntimeError("联储市值/GDP共同历史样本不足")
    sp_rows = outputs["sp500"]
    by_date = {r["date"]: r["value"] for r in sp_rows}
    last = history[-1]
    anchor = date.fromisoformat(last["date"])
    anchor_px_rows = [r for r in sp_rows if r["date"] <= last["date"]]
    current_px = sp_rows[-1]
    if not anchor_px_rows:
        raise RuntimeError("无法将标普指数与联储市值数据锚定")
    anchor_px = anchor_px_rows[-1]["value"]
    current_cap = last["market_cap_millions"] * current_px["value"] / anchor_px
    # GDP latest value stays quarterly; only the market-cap estimate is marked to latest S&P close.
    current_value = current_cap / (last["gdp_billions_annualized"] * 1000.0) * 100.0
    current = {"date": current_px["date"], "value": current_value, "anchor_date": last["date"],
               "anchor_price": anchor_px, "current_sp500_close": current_px["value"],
               "market_cap_millions_estimate": current_cap,
               "gdp_billions_annualized": last["gdp_billions_annualized"],
               "source": "Federal Reserve Z.1/FRED equities; S&P 500 close-scaled daily estimate",
               "method": "最新联储企业股权市值按标普 500 从最近季度锚点滚动更新，再除以最新名义 GDP；不是任一单独指数的市值/GDP。"}
    if current_value > 350 or current_value <= 0:
        # Broad Z.1 liabilities are not comparable to listed-market capitalization.
        # Do not surface a wildly out-of-range value as the Buffett indicator.
        current = None
        errors.append(f"Z.1 fallback rejected as current Buffett value: {current_value:.2f}% outside validation bounds")
    result = {"current": current, "history": history, "source": "FRED Z.1 corporate-equity liabilities/GDP proxy (fallback only)", "definition_warning": "Z.1 corporate-equity liabilities cover a broader universe than listed domestic market capitalization; fallback is not a canonical Buffett indicator.", "errors": errors, "frequency": "quarterly_proxy"}
    with _LOCK:
        _CACHE[key] = {"ts": time.time(), "value": result}
    return result


def _load_yield(series_key: str, force: bool = False) -> dict[str, Any]:
    series_id = FRED_SERIES[series_key]
    rows = _series_csv(series_id, force)
    latest = rows[-1]
    label = "十年期实际收益率" if series_key == "real_yield_10y" else "十年期名义收益率"
    return {"date": latest["date"], "value": latest["value"], "history": rows,
            "source": f"FRED {series_id}", "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
            "basis": "real" if series_key == "real_yield_10y" else "nominal", "label": label}


def _load_real_yield(force: bool = False) -> dict[str, Any]:
    return _load_yield("real_yield_10y", force)


def _load_nominal_yield(force: bool = False) -> dict[str, Any]:
    return _load_yield("nominal_yield_10y", force)


def _load_macro_observables(force: bool = False) -> dict[str, Any]:
    mapping = {
        "M1": "money_stock_m1", "V_M1": "money_velocity_m1",
        "P_GDP_DEFLATOR": "price_level_gdp_deflator", "Q_REAL_GDP": "real_gdp",
        "CORPORATE_PROFIT_AFTER_TAX": "corporate_profit_after_tax", "NOMINAL_GDP": "nominal_gdp",
    }
    result: dict[str, Any] = {}
    errors = []
    def fetch(label: str, key: str):
        rows = _series_csv(FRED_SERIES[key], force)
        return label, {"date": rows[-1]["date"], "value": rows[-1]["value"], "history": rows,
                       "source": f"FRED {FRED_SERIES[key]}", "source_url": f"https://fred.stlouisfed.org/series/{FRED_SERIES[key]}"}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch, label, key): label for label, key in mapping.items()}
        for future in as_completed(futures, timeout=20):
            label = futures[future]
            try:
                name, value = future.result()
                result[name] = value
            except Exception as exc:
                key = mapping[label]
                errors.append(f"{label}: {str(exc)[:140]}")
                result[label] = {"date": None, "value": None, "history": [], "error": str(exc)[:180],
                                 "source": f"FRED {FRED_SERIES[key]}", "source_url": f"https://fred.stlouisfed.org/series/{FRED_SERIES[key]}"}
    # Mark unfinished source calls as unavailable instead of failing the whole dashboard.
    for label, key in mapping.items():
        if label not in result:
            result[label] = {"date": None, "value": None, "history": [], "error": "源请求超时",
                             "source": f"FRED {FRED_SERIES[key]}", "source_url": f"https://fred.stlouisfed.org/series/{FRED_SERIES[key]}"}
            errors.append(f"{label}: source timeout")
    result["errors"] = errors
    return result


def _read_frame_from_archive_member(name: str, data: bytes):
    if pd is None:
        return None
    lower = name.lower()
    try:
        if lower.endswith(".csv"):
            return pd.read_csv(io.BytesIO(data), low_memory=False)
        if lower.endswith(".xlsx"):
            return pd.read_excel(io.BytesIO(data), sheet_name=0, engine="openpyxl")
        if lower.endswith(".xls"):
            return pd.read_excel(io.BytesIO(data), sheet_name=0, engine="xlrd")
        if lower.endswith(".dta"):
            return pd.read_stata(io.BytesIO(data), convert_categoricals=False)
        if lower.endswith(".parquet"):
            return pd.read_parquet(io.BytesIO(data))
    except Exception:
        return None
    return None


def _col_norm(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _choose_column(columns: list[str], candidates: list[str], contains: list[str] | None = None,
                   excludes: list[str] | None = None) -> str | None:
    norm_map = {_col_norm(c): c for c in columns}
    for cand in candidates:
        if _col_norm(cand) in norm_map:
            return norm_map[_col_norm(cand)]
    contains = [_col_norm(x) for x in (contains or [])]
    excludes = [_col_norm(x) for x in (excludes or [])]
    for col in columns:
        normalized = _col_norm(col)
        if contains and all(piece in normalized for piece in contains) and not any(piece in normalized for piece in excludes):
            return col
    return None


def _frame_buyer_observations(frame, source_file: str) -> list[dict[str, Any]]:
    if pd is None or frame is None or frame.empty:
        return []
    cols = [str(c) for c in frame.columns]
    # Do not accept raw data without an observation date, provider, and a horizon column.
    date_col = _choose_column(cols, ["Date", "ObservationDate", "as_of_date", "CMA_Date", "SurveyDate", "YearMonth"])
    provider_col = _choose_column(cols, ["Provider", "Institution", "InstitutionName", "Manager", "Firm", "Respondent"])
    horizon_col = _choose_column(cols, ["Horizon_adj", "Horizon", "HorizonYears", "ForecastHorizon", "Maturity"])
    if not date_col or not provider_col or not horizon_col:
        return []
    equity_real_col = _choose_column(cols, ["US_Equity_r", "ExpectedRealEquityReturn", "RealEquityReturn", "EquityReturnReal"])
    equity_col = _choose_column(cols, ["US_Equity", "ExpectedEquityReturn", "EquityReturn", "NominalEquityReturn"])
    cash_real_col = _choose_column(cols, ["US_Cash_r", "ExpectedRealCashReturn", "RealCashReturn", "CashReturnReal"])
    cash_col = _choose_column(cols, ["US_Cash", "ExpectedCashReturn", "CashReturn", "NominalCashReturn", "US_Treasury"])
    explicit_erp_col = _choose_column(cols, ["buyer_erp_pct", "ERP", "EquityRiskPremium", "ExpectedEquityPremium", "US_ERP", "RiskPremium"],
                                      contains=["premium"], excludes=["term", "credit", "bond"])
    growth_col = _choose_column(cols,
        ["buyer_g_pct", "gB_expected_pct", "ExpectedEarningsGrowth", "LongRunEarningsGrowth", "ExpectedRealEarningsGrowth", "AnalystLongTermGrowth", "GrowthForecast"],
        contains=["growth"], excludes=["observed", "realized", "actual", "historical", "past", "lagged"])
    # A source's expected real equity return can form an ERP using the same-row expected cash return.
    if not date_col or not provider_col or not (explicit_erp_col or ((equity_real_col or equity_col) and (cash_real_col or cash_col))):
        return []
    result = []
    for _, row in frame.iterrows():
        raw_date = row.get(date_col)
        try:
            if hasattr(raw_date, "to_pydatetime"):
                obs_date = raw_date.to_pydatetime().date()
            elif isinstance(raw_date, (datetime, date)):
                obs_date = raw_date.date() if isinstance(raw_date, datetime) else raw_date
            else:
                text = str(raw_date).strip()
                try:
                    obs_date = date.fromisoformat(text[:10])
                except ValueError:
                    numeric = _finite(text)
                    if numeric and 1800 <= numeric < 2200:
                        yr = int(numeric); mo = int(round((numeric-yr)*100)); obs_date = date(yr, max(1,min(12,mo)), 1)
                    else:
                        continue
        except Exception:
            continue
        provider = str(row.get(provider_col, "")).strip()
        if not provider or provider.lower() in {"nan", "none", ""}:
            continue
        horizon = _finite(row.get(horizon_col)) if horizon_col else None
        if horizon is None or not (5 <= horizon <= 15):
            continue
        erp = _finite(row.get(explicit_erp_col)) if explicit_erp_col else None
        erp_method = f"explicit:{explicit_erp_col}" if explicit_erp_col else None
        erp_basis = "real" if explicit_erp_col and any(x in _col_norm(explicit_erp_col) for x in ("real", "inflationadjusted")) else ("nominal" if explicit_erp_col and any(x in _col_norm(explicit_erp_col) for x in ("nominal",)) else "unknown")
        # Never subtract a nominal return from a real return: same-basis pairs only.
        equity_real_val = _finite(row.get(equity_real_col)) if equity_real_col else None
        cash_real_val = _finite(row.get(cash_real_col)) if cash_real_col else None
        equity_nominal_val = _finite(row.get(equity_col)) if equity_col else None
        cash_nominal_val = _finite(row.get(cash_col)) if cash_col else None
        equity_val = equity_real_val if equity_real_val is not None else equity_nominal_val
        cash_val = cash_real_val if cash_real_val is not None else cash_nominal_val
        if erp is None and equity_real_val is not None and cash_real_val is not None:
            erp = equity_real_val - cash_real_val
            erp_basis = "real"
            erp_method = f"same_row_real_difference:{equity_real_col}-{cash_real_col}"
        elif erp is None and equity_nominal_val is not None and cash_nominal_val is not None:
            erp = equity_nominal_val - cash_nominal_val
            erp_basis = "nominal"
            erp_method = f"same_row_nominal_difference:{equity_col}-{cash_col}"
        growth = _finite(row.get(growth_col)) if growth_col else None
        growth_name = _col_norm(growth_col or "")
        growth_basis = "real" if "real" in growth_name else ("nominal" if "nominal" in growth_name else "unknown")
        if erp is None:
            continue
        result.append({"date": obs_date.isoformat(), "institution": provider,
                       "horizon_years": horizon, "buyer_erp_pct": erp, "buyer_g_pct": growth,
                       "erp_basis": erp_basis, "growth_basis": growth_basis,
                       "source_file": source_file, "source_date_column": date_col,
                       "source_erp_method": erp_method, "source_erp_column": explicit_erp_col,
                       "source_growth_column": growth_col, "expected_equity_return_pct": equity_val,
                       "expected_cash_return_pct": cash_val})
    return result


def _extract_institution_records(path: Path) -> dict[str, Any]:
    usable: list[dict[str, Any]] = []
    inspected_files = []
    skipped_files = []
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"机构研究 ZIP CRC 校验失败：{bad}")
        total_uncompressed = sum(i.file_size for i in archive.infolist() if not i.is_dir())
        if total_uncompressed > 600 * 1024 * 1024:
            raise RuntimeError("机构数据包解压体积超过安全上限")
        for item in archive.infolist():
            if item.is_dir():
                continue
            ext = Path(item.filename).suffix.lower()
            if ext not in {".csv", ".xlsx", ".xls", ".dta", ".parquet"}:
                continue
            if item.file_size > 250 * 1024 * 1024:
                skipped_files.append({"file": item.filename, "reason": "file_size_limit"})
                continue
            try:
                frame = _read_frame_from_archive_member(item.filename, archive.read(item))
                records = _frame_buyer_observations(frame, item.filename)
                inspected_files.append({"file": item.filename, "rows": int(len(frame)) if frame is not None else 0,
                                        "columns": [str(c) for c in frame.columns] if frame is not None else [],
                                        "usable_buyer_rows": len(records)})
                usable.extend(records)
            except Exception as exc:
                skipped_files.append({"file": item.filename, "reason": str(exc)[:160]})
    # ERP and g must come from the same date, same institution row, same horizon and same basis.
    joint = [r for r in usable if r.get("buyer_g_pct") is not None and r.get("erp_basis") in {"real", "nominal"}
             and r.get("growth_basis") == r.get("erp_basis")]
    if joint:
        max_date = max(r["date"] for r in joint)
        same_date = [r for r in joint if r["date"] == max_date]
        # Select the most represented horizon closest to 10 years; never pool disparate horizons.
        horizon_counts: dict[float, int] = {}
        for row in same_date:
            h = round(float(row["horizon_years"]), 2)
            horizon_counts[h] = horizon_counts.get(h, 0) + 1
        chosen_horizon = sorted(horizon_counts, key=lambda h: (-horizon_counts[h], abs(h - 10.0), h))[0]
        matched = [r for r in same_date if round(float(r["horizon_years"]), 2) == chosen_horizon]
        basis = matched[0]["erp_basis"]
        summary = {
            "as_of_date": max_date,
            "horizon_years": chosen_horizon,
            "return_basis": basis,
            "institution_count": len({r["institution"] for r in matched}),
            "row_count": len(matched),
            "buyer_erp_pct": statistics.median(r["buyer_erp_pct"] for r in matched),
            "buyer_g_pct": statistics.median(r["buyer_g_pct"] for r in matched),
            "buyer_g_count": len(matched),
            "growth_method": "explicit forward-looking source field; same return basis and horizon",
            "source_file_count": len(inspected_files),
            "matched_horizons_years": [chosen_horizon],
        }
    else:
        usable_erp = [r for r in usable if r.get("buyer_erp_pct") is not None]
        max_date = max((r["date"] for r in usable_erp), default=None)
        latest = [r for r in usable_erp if r["date"] == max_date] if max_date else []
        summary = {"as_of_date": max_date, "horizon_years": None, "return_basis": None,
                   "institution_count": len({r["institution"] for r in latest}), "row_count": len(latest),
                   "buyer_erp_pct": statistics.median(r["buyer_erp_pct"] for r in latest) if latest else None,
                   "buyer_g_pct": None, "buyer_g_count": 0, "growth_method": None,
                   "source_file_count": len(inspected_files), "matched_horizons_years": []}
    if summary["buyer_erp_pct"] is not None and summary["buyer_g_pct"] is not None:
        status = "USABLE_ERP_AND_G"
    elif summary["buyer_erp_pct"] is not None:
        status = "ERP_G_BASIS_OR_SOURCE_MISSING"
    else:
        status = "SOURCE_DOWNLOADED_BUT_FIELDS_UNMAPPED"
    return {"status": status, "summary": summary, "usable_row_count": len(usable), "records": usable,
            "inspected_files": inspected_files, "skipped_files": skipped_files,
            "message": "机构 ERP/g 已在相同日期、期限与回报口径匹配" if status == "USABLE_ERP_AND_G" else
                       "已解析部分机构数据，但 ERP/g 的预测期限、回报口径或前瞻增长字段不完整；不会生成不合格 Theta。"}


def _download_institution_archive() -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if SOURCE_ARCHIVE.exists() and zipfile.is_zipfile(SOURCE_ARCHIVE):
        try:
            source = _extract_institution_records(SOURCE_ARCHIVE)
            SOURCE_MANIFEST.write_text(json.dumps(source, ensure_ascii=False, default=str), encoding="utf-8")
            return source
        except Exception:
            pass
    try:
        meta_response = _HTTP.get(MENDELEY_META_URL, timeout=(8, 20))
        meta_response.raise_for_status()
        meta = meta_response.json()
        download_url = meta.get("url")
        if not download_url:
            raise RuntimeError(f"Mendeley ZIP API 未返回下载地址：{str(meta)[:240]}")
        response = _HTTP.get(download_url, timeout=(10, 120), stream=True)
        response.raise_for_status()
        temp = SOURCE_ARCHIVE.with_suffix(".part")
        total = 0
        with temp.open("wb") as stream:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 120 * 1024 * 1024:
                    raise RuntimeError("下载大小超过预期上限 120 MB")
                stream.write(chunk)
        if not zipfile.is_zipfile(temp):
            temp.unlink(missing_ok=True)
            raise RuntimeError("官方下载内容不是 ZIP")
        with zipfile.ZipFile(temp) as archive:
            bad = archive.testzip()
            total_uncompressed = sum(i.file_size for i in archive.infolist() if not i.is_dir())
            if bad or total_uncompressed > 600 * 1024 * 1024:
                temp.unlink(missing_ok=True)
                raise RuntimeError("原始机构 ZIP 校验失败或解压体积超限")
            root = CACHE_DIR.resolve()
            for member in archive.infolist():
                if not (root / member.filename).resolve().is_relative_to(root):
                    temp.unlink(missing_ok=True)
                    raise RuntimeError("ZIP 中存在越界路径，已拒绝")
        temp.replace(SOURCE_ARCHIVE)
        source = _extract_institution_records(SOURCE_ARCHIVE)
        source["download"] = {"size_bytes": total, "sha256": _sha256(SOURCE_ARCHIVE), "source_url": MENDELEY_DATASET_URL}
        SOURCE_MANIFEST.write_text(json.dumps(source, ensure_ascii=False, default=str), encoding="utf-8")
        return source
    except Exception as exc:
        # If no remote download is possible, provide an explicit status rather than fabricating ERP/g.
        return {"status": "DOWNLOAD_FAILED", "summary": {"as_of_date": None, "institution_count": 0, "row_count": 0,
                "buyer_erp_pct": None, "buyer_g_pct": None, "buyer_g_count": 0, "growth_method": None},
                "usable_row_count": 0, "records": [], "inspected_files": [], "skipped_files": [],
                "message": f"机构原始研究包自动下载失败：{str(exc)[:220]}", "source_url": MENDELEY_DATASET_URL}


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _get_buyer_status(start_if_missing: bool = True) -> dict[str, Any]:
    global _BUYER_WORKER, _BUYER_STATUS
    if SOURCE_MANIFEST.exists():
        try:
            manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
            if manifest.get("status") in {"USABLE_ERP_AND_G", "ERP_G_BASIS_OR_SOURCE_MISSING", "SOURCE_DOWNLOADED_BUT_FIELDS_UNMAPPED"}:
                _BUYER_STATUS = manifest
            elif _BUYER_STATUS.get("status") == "NOT_STARTED":
                _BUYER_STATUS = manifest
        except Exception:
            pass
    if start_if_missing and not SOURCE_ARCHIVE.exists() and _BUYER_STATUS.get("status") in {"NOT_STARTED", "DOWNLOAD_FAILED"}:
        with _BUYER_LOCK:
            if _BUYER_WORKER is None or not _BUYER_WORKER.is_alive():
                _BUYER_STATUS = {"status": "DOWNLOADING", "message": "正在后台获取论文官方机构预期原始数据，不需要手动输入。", "source_url": MENDELEY_DATASET_URL}
                def worker():
                    global _BUYER_STATUS
                    result = _download_institution_archive()
                    _BUYER_STATUS = result
                _BUYER_WORKER = threading.Thread(target=worker, name="ael-gravity-institution-data", daemon=True)
                _BUYER_WORKER.start()
    elif SOURCE_ARCHIVE.exists() and _BUYER_STATUS.get("status") in {"NOT_STARTED", "DOWNLOAD_FAILED"}:
        with _BUYER_LOCK:
            if _BUYER_WORKER is None or not _BUYER_WORKER.is_alive():
                def worker_existing():
                    global _BUYER_STATUS
                    _BUYER_STATUS = _download_institution_archive()
                _BUYER_WORKER = threading.Thread(target=worker_existing, name="ael-gravity-institution-parse", daemon=True)
                _BUYER_WORKER.start()
    return dict(_BUYER_STATUS)


def _latest_le(rows: list[dict[str, Any]], as_of: str) -> dict[str, Any] | None:
    eligible = [r for r in rows if r.get("date", "") <= as_of]
    return eligible[-1] if eligible else None


def _forward_growth_from_shiller(cape_rows: list[dict[str, Any]]) -> float | None:
    rows = [r for r in cape_rows if r.get("real_earnings") is not None and r.get("real_earnings") > 0]
    if len(rows) < 121:
        return None
    current = rows[-1]
    current_date = date.fromisoformat(current["date"])
    target = date(current_date.year - 10, current_date.month, 1)
    candidates = [r for r in rows if r["date"] <= target.isoformat()]
    if not candidates:
        return None
    base = candidates[-1]
    years = (current_date - date.fromisoformat(base["date"])).days / 365.2425
    if years < 9.5:
        return None
    return round(((current["real_earnings"] / base["real_earnings"]) ** (1.0 / years) - 1) * 100.0, 4)


def _institution_latest_real_return(buyer_status: dict[str, Any]) -> dict[str, Any] | None:
    records = buyer_status.get("records") or []
    real_return_fields = [r for r in records if r.get("expected_equity_return_pct") is not None]
    if not real_return_fields:
        return None
    latest_date = max(r["date"] for r in real_return_fields)
    rows = [r for r in real_return_fields if r["date"] == latest_date]
    # Prefer explicitly real return field/source method when present; records preserve their raw columns.
    values = [r["expected_equity_return_pct"] for r in rows if r.get("expected_equity_return_pct") is not None]
    if not values:
        return None
    return {"date": latest_date, "expected_real_equity_return_pct": statistics.median(values),
            "institution_count": len({r["institution"] for r in rows}),
            "source": "Dahlquist-Ibert institutional expected equity return fields", "raw_rows": len(rows)}


def _safe_get_data(force: bool = False) -> dict[str, Any]:
    errors = []
    keys = ["buffett", "cape", "real_yield", "index_sp500"]
    with _LOCK:
        cached = _CACHE.get("gravity:bundle")
        if cached and not force and time.time() - cached["ts"] < TTL_SECONDS:
            return cached["value"]
    results = {}
    funcs = {
        "buffett": lambda: _load_buffett(force),
        "cape": lambda: _cape_history(force),
        "real_yield": lambda: _load_real_yield(force),
        "nominal_yield": lambda: _load_nominal_yield(force),
        "macro_observables": lambda: _load_macro_observables(force),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(func): key for key, func in funcs.items()}
        for future in as_completed(futures, timeout=30):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                errors.append(f"{name}: {str(exc)[:220]}")
    results["buyer"] = _get_buyer_status(start_if_missing=True)
    results["errors"] = errors
    with _LOCK:
        _CACHE["gravity:bundle"] = {"ts": time.time(), "value": results}
    return results


def _latest_row_on_or_before(rows: list[dict[str, Any]], as_of: str) -> dict[str, Any] | None:
    eligible = [r for r in rows if str(r.get("date", ""))[:10] <= as_of and _finite(r.get("value")) is not None]
    return eligible[-1] if eligible else None


def _macro_model_values(macro_obs: dict[str, Any]) -> dict[str, Any]:
    """Common-date M/V/P/Q and transparent corporate-profit-share alpha proxy."""
    keys = ("M1", "V_M1", "P_GDP_DEFLATOR", "Q_REAL_GDP", "CORPORATE_PROFIT_AFTER_TAX", "NOMINAL_GDP")
    series = {k: (macro_obs.get(k) or {}).get("history") or [] for k in keys}
    if any(not rows for rows in series.values()):
        return {"as_of_date": None, "alpha_proxy": None, "M1": None, "V": None, "P": None, "Q": None,
                "A_over_M_objective": None, "A_over_M_buyer": None, "source_status": "PARTIAL_MACRO_SERIES",
                "source_notes": "M1/V/P/Q 或企业利润/GDP 序列不齐。"}
    # Match CPATAX and GDP on a common quarter, then use no later observation for other series.
    common = sorted(set(r["date"] for r in series["CORPORATE_PROFIT_AFTER_TAX"]) & set(r["date"] for r in series["NOMINAL_GDP"]))
    other_latest = min(series[k][-1]["date"] for k in ("M1", "V_M1", "P_GDP_DEFLATOR", "Q_REAL_GDP"))
    eligible = [d for d in common if d <= other_latest]
    if not eligible:
        return {"as_of_date": None, "alpha_proxy": None, "M1": None, "V": None, "P": None, "Q": None,
                "A_over_M_objective": None, "A_over_M_buyer": None, "source_status": "NO_COMMON_OBSERVATIONS",
                "source_notes": "企业利润与 GDP 没有共同季度观测日期。"}
    as_of = eligible[-1]
    obs = {k: _latest_row_on_or_before(v, as_of) for k, v in series.items()}
    if any(obs[k] is None for k in keys):
        return {"as_of_date": as_of, "alpha_proxy": None, "M1": None, "V": None, "P": None, "Q": None,
                "A_over_M_objective": None, "A_over_M_buyer": None, "source_status": "NO_COMMON_OBSERVATIONS",
                "source_notes": "找不到共同可用的宏观观测日期。"}
    profit = float(obs["CORPORATE_PROFIT_AFTER_TAX"]["value"])
    gdp = float(obs["NOMINAL_GDP"]["value"])
    alpha = profit / gdp if gdp > 0 else None
    if alpha is not None and (not math.isfinite(alpha) or not 0 <= alpha <= 1):
        alpha = None
    return {"as_of_date": as_of, "alpha_proxy": alpha,
            "alpha_definition": "企业税后利润 CPATAX / 名义 GDP；透明公开数据代理，不等同于理论 α 的直接观测",
            "M1": obs["M1"]["value"], "M1_date": obs["M1"]["date"],
            "V": obs["V_M1"]["value"], "V_date": obs["V_M1"]["date"],
            "P": obs["P_GDP_DEFLATOR"]["value"], "P_date": obs["P_GDP_DEFLATOR"]["date"],
            "Q": obs["Q_REAL_GDP"]["value"], "Q_date": obs["Q_REAL_GDP"]["date"],
            "corporate_profit_after_tax": profit, "corporate_profit_date": obs["CORPORATE_PROFIT_AFTER_TAX"]["date"],
            "nominal_gdp": gdp, "nominal_gdp_date": obs["NOMINAL_GDP"]["date"],
            "M1_unit": "USD billions (10^9 USD)", "V_unit": "dimensionless ratio", "P_unit": "GDP deflator index (2017=100)", "Q_unit": "Billions of chained 2017 USD, SAAR",
            "A_over_M_objective": None, "A_over_M_buyer": None,
            "source_status": "READY" if alpha is not None else "ALPHA_PROXY_INVALID",
            "source_notes": "M1、V、P、Q 来自 FRED；α 以企业税后利润/GDP作为操作性代理，界面将明确标注。"}


def _build_objective_gravity_history(cape_rows: list[dict[str, Any]], real_yield_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for cap in cape_rows:
        cape_value = _finite(cap.get("cape"))
        obs_date = str(cap.get("date", ""))[:10]
        if cape_value is None or cape_value <= 0 or not obs_date:
            continue
        y = _latest_real_yield_known(real_yield_rows, obs_date)
        g = _growth_at_date(cape_rows, obs_date)
        if y is None or g is None:
            continue
        erp = math.log1p(1.0 / cape_value) * 100.0 - y
        r = y + erp - g
        rows.append({"date": obs_date, "value": r, "erp_objective_pct": erp,
                     "g_objective_pct": g, "real_yield_pct": y,
                     "source": "Shiller CAPE + FRED DFII10 + trailing real-earnings growth"})
    return rows


def _metrics_bundle(index: str, raw: dict[str, Any]) -> dict[str, Any]:
    today = date.today().isoformat()
    buffett = raw.get("buffett") or {}
    cape_rows = raw.get("cape") or []
    real_yield = raw.get("real_yield") or {}
    nominal_yield = raw.get("nominal_yield") or {}
    cape_row = cape_rows[-1] if cape_rows else None
    if not cape_row:
        cape_latest = None
        cape_pctl = None
        cape_hist = []
    else:
        cape_latest = cape_row.get("cape")
        cape_pctl = _percentile(cape_latest, [r["cape"] for r in cape_rows if r.get("cape") is not None])
        cape_hist = [{"date": r["date"], "value": r["cape"]} for r in cape_rows if r.get("cape") is not None]
    bcurrent = buffett.get("current") if buffett else None
    # A broad Z.1 equity-liability proxy is not the listed-market-cap Buffett
    # indicator. Keep it out of the canonical value/history chart when the
    # World Bank series is unavailable; report source status instead.
    bhistory = (buffett.get("history") or []) if buffett and buffett.get("frequency") != "quarterly_proxy" else []
    bvalue = bcurrent.get("value") if bcurrent else None
    bpctl = _percentile(bvalue, [r["value"] for r in bhistory if r.get("value") is not None])
    y10_real = real_yield.get("value")
    y10_nominal = nominal_yield.get("value")
    macro_obs = raw.get("macro_observables") or {}
    macro_model = _macro_model_values(macro_obs)
    obj_g = _forward_growth_from_shiller(cape_rows) if cape_rows else None
    obj_erp = None
    r_obj = None
    cape_asof = cape_row.get("date") if cape_row else None
    objective_yield_row = _latest_row_on_or_before(real_yield.get("history") or [], cape_asof) if cape_asof else None
    objective_yield = objective_yield_row.get("value") if objective_yield_row else y10_real
    objective_yield_date = objective_yield_row.get("date") if objective_yield_row else (real_yield.get("date") if real_yield else None)
    if cape_latest is not None and objective_yield is not None:
        obj_erp = math.log1p(1.0 / cape_latest) * 100.0 - objective_yield
        if obj_g is not None:
            r_obj = objective_yield + obj_erp - obj_g
    buyer = raw.get("buyer") or {}
    summary = buyer.get("summary") or {}
    erp_buyer = _finite(summary.get("buyer_erp_pct"))
    g_buyer = _finite(summary.get("buyer_g_pct"))
    buyer_date = summary.get("as_of_date")
    buyer_basis = summary.get("return_basis")
    buyer_horizon = _finite(summary.get("horizon_years"))
    buyer_source_age_days = None
    try:
        buyer_source_age_days = (date.fromisoformat(today) - date.fromisoformat(str(buyer_date)[:10])).days
    except Exception:
        pass
    # Current buyer-side value is suppressed if the source is stale or bases do not match.
    # Historical buyer values can still be used at their original date in year-backtests.
    cape_buyer_gap = None
    try:
        if buyer_date and cape_asof:
            cape_buyer_gap = abs((date.fromisoformat(str(buyer_date)[:10]) - date.fromisoformat(str(cape_asof)[:10])).days)
    except Exception:
        pass
    # R_objective is defined on a real-return basis (real 10Y yield + real ERP - real g).
    # Buyer-side R must therefore use the same real basis and approximately the same 10Y horizon.
    # A nominal ERP/g pair is not mixed with a real objective R just to manufacture Theta.
    buyer_horizon_match = buyer_horizon is not None and abs(buyer_horizon - 10.0) <= 1.0
    buyer_basis_match = buyer_basis == "real"
    buyer_date_fresh = buyer_source_age_days is not None and 0 <= buyer_source_age_days <= 180
    buyer_date_aligned = cape_buyer_gap is not None and cape_buyer_gap <= 45
    buyer_fresh = buyer_date_fresh and buyer_date_aligned and buyer_horizon_match and buyer_basis_match
    buyer_yield_rows = (real_yield.get("history") or []) if buyer_basis_match else []
    buyer_yield_row = _latest_row_on_or_before(buyer_yield_rows, str(buyer_date)[:10]) if buyer_date else None
    buyer_yield = buyer_yield_row.get("value") if buyer_yield_row else None
    buyer_yield_date = buyer_yield_row.get("date") if buyer_yield_row else None
    buyer_formula = None
    r_buyer = None
    theta = None
    if erp_buyer is not None and g_buyer is not None and buyer_yield is not None and buyer_fresh:
        r_buyer = buyer_yield + erp_buyer - g_buyer
        buyer_formula = "R_actual = matched real 10Y yield + institutional real ERP - forward-looking real g"
        if r_obj is not None and abs(r_buyer) >= 0.25:
            theta = r_obj / r_buyer
    r_objective_history = _build_objective_gravity_history(cape_rows, real_yield.get("history") or [])
    matched_buyer_history = _build_point_in_time_gravity_history(
        cape_rows, real_yield.get("history") or [], buyer.get("records") or [], date.today())
    r_actual_history = [{"date": x["signal_date"], "value": x["R_actual"],
                         "source_observation_date": x["buyer_observation_date"]} for x in matched_buyer_history]
    theta_history = [{"date": x["signal_date"], "value": x["Theta"],
                      "source_observation_date": x["buyer_observation_date"]}
                     for x in matched_buyer_history if x.get("Theta") is not None]
    latest_verified_gravity = matched_buyer_history[-1] if matched_buyer_history else None
    latest_verified_age_days = None
    if latest_verified_gravity:
        try:
            latest_verified_age_days = (date.today() - date.fromisoformat(latest_verified_gravity["signal_date"])).days
        except Exception:
            latest_verified_age_days = None
    r_objective_values = [x["value"] for x in r_objective_history if x.get("value") is not None]
    r_actual_values = [x["value"] for x in r_actual_history if x.get("value") is not None]
    theta_values = [x["value"] for x in theta_history if x.get("value") is not None]
    if raw.get("buyer") is not None:
        buyer_status = buyer.get("status", "UNKNOWN")
        if erp_buyer is not None and g_buyer is not None:
            if not buyer_basis_match:
                buyer_status = "RETURN_BASIS_MISMATCH"
            elif not buyer_horizon_match:
                buyer_status = "HORIZON_MISMATCH"
            elif not buyer_date_fresh:
                buyer_status = "STALE_SOURCE"
            elif not buyer_date_aligned:
                buyer_status = "SOURCE_DATE_MISMATCH"
            elif buyer_yield is None:
                buyer_status = "RISK_FREE_YIELD_UNAVAILABLE"
    else:
        buyer_status = "NOT_STARTED"
    if macro_model.get("alpha_proxy") is not None and macro_model.get("M1") is not None and macro_model.get("V") is not None:
        numerator = macro_model["alpha_proxy"] * macro_model["M1"] * macro_model["V"]
        macro_model["numerator_alpha_M1_V"] = numerator
        macro_model["A_over_M_objective"] = numerator / (r_obj / 100.0) if r_obj is not None and r_obj > 0 else None
        macro_model["A_over_M_buyer"] = numerator / (r_buyer / 100.0) if r_buyer is not None and r_buyer > 0 else None
        macro_model["A_over_M_unit"] = "USD billions (10^9 USD; model-implied capitalization proxy, not a dimensionless ratio)"
        macro_model["A_over_M_formula"] = "(alpha_proxy_fraction * M1_USD_billions * V) / (R_percent/100); R must be positive; output unit USD billions"
    index_meta = INDEXES.get(index, INDEXES["SP500"])
    return {
        "ok": True, "module": "AEL_PRO_AUTOMATIC_DOUBLE_GRAVITY", "version": "2.6.45-DATA-UNITS-FIX",
        "as_of": _now_utc(), "requested_index": index, "index_name": index_meta["name"], "index_symbol": index_meta["symbol"],
        "metrics": {
            "buffett_indicator_pct_gdp": round(bvalue, 3) if bvalue is not None else None,
            "buffett_percentile": bpctl, "buffett_observation_date": bcurrent.get("date") if bcurrent else None,
            "buffett_definition": (bcurrent.get("method") or bcurrent.get("source") or buffett.get("definition_warning") if bcurrent else "无通过校验的当前巴菲特型指标"),
            "buffett_frequency": buffett.get("frequency", "unknown"),
            "buffett_quality_warning": ("数值超过 350%，超出可比口径校验范围，已阻断显示。" if bvalue is not None and bvalue > 350 else (buffett.get("definition_warning") if bcurrent and buffett.get("frequency") == "quarterly_proxy" else None)),
            "buffett_anchor_date": bcurrent.get("anchor_date") if bcurrent else None,
            "cape": round(cape_latest, 3) if cape_latest is not None else None,
            "cape_percentile": cape_pctl, "cape_observation_date": cape_row.get("date") if cape_row else None,
            "cape_scope": "Shiller 官方 CAPE 为美国标普 500 市场估值基准；选纳指/道指时作为共同市场参考，不冒充指数专属 CAPE。",
            "risk_free_real_10y_pct": round(y10_real, 4) if y10_real is not None else None,
            "risk_free_observation_date": real_yield.get("date") if real_yield else None,
            "risk_free_yield_for_objective_pct": round(objective_yield, 4) if objective_yield is not None else None,
            "objective_yield_observation_date": objective_yield_date,
            "buyer_to_cape_date_gap_days": cape_buyer_gap,
            "alpha_proxy_pct": round(macro_model["alpha_proxy"] * 100.0, 4) if macro_model.get("alpha_proxy") is not None else None,
            "alpha_proxy_date": macro_model.get("as_of_date"),
            "alpha_proxy_definition": macro_model.get("alpha_definition"),
            "M1_money_stock": macro_model.get("M1"), "M1_observation_date": macro_model.get("M1_date"),
            "V_M1_velocity": macro_model.get("V"), "V_observation_date": macro_model.get("V_date"),
            "P_GDP_deflator": macro_model.get("P"), "P_observation_date": macro_model.get("P_date"),
            "Q_real_gdp_billions": macro_model.get("Q"), "Q_observation_date": macro_model.get("Q_date"),
            "A_over_M_objective_billions": round(macro_model["A_over_M_objective"], 4) if macro_model.get("A_over_M_objective") is not None else None,
            "A_over_M_buyer_billions": round(macro_model["A_over_M_buyer"], 4) if macro_model.get("A_over_M_buyer") is not None else None,
            "A_over_M_unit": macro_model.get("A_over_M_unit"), "A_over_M_source_status": macro_model.get("source_status"),
            "erp_objective_pct": round(obj_erp, 4) if obj_erp is not None else None,
            "g_objective_pct": round(obj_g, 4) if obj_g is not None else None,
            "g_objective_definition": "Shiller 通胀调整后实际盈利的过去 10 年 CAGR；这是客观后向增长代理，不是未来增长预测。",
            "R_objective_pct": round(r_obj, 4) if r_obj is not None else None,
            "R_objective_percentile": _percentile(r_obj, r_objective_values),
            "R_actual_percentile": _percentile(r_buyer if r_buyer is not None else (latest_verified_gravity.get("R_actual") if latest_verified_gravity else None), r_actual_values),
            "Theta_percentile": _percentile(theta if theta is not None else (latest_verified_gravity.get("Theta") if latest_verified_gravity else None), theta_values),
            "latest_verified_history_date": latest_verified_gravity.get("signal_date") if latest_verified_gravity else None,
            "latest_verified_R_actual_pct": round(latest_verified_gravity["R_actual"], 4) if latest_verified_gravity else None,
            "latest_verified_R_objective_pct": round(latest_verified_gravity["R_objective"], 4) if latest_verified_gravity else None,
            "latest_verified_Theta": round(latest_verified_gravity["Theta"], 6) if latest_verified_gravity and latest_verified_gravity.get("Theta") is not None else None,
            "latest_verified_history_age_days": latest_verified_age_days,
            "latest_verified_institution_count": latest_verified_gravity.get("institution_count") if latest_verified_gravity else None,
            "latest_verified_horizon_years": latest_verified_gravity.get("horizon_years_median") if latest_verified_gravity else None,
            "latest_verified_cape_gap_days": latest_verified_gravity.get("cape_date_gap_days") if latest_verified_gravity else None,
            "R_objective_history_count": len(r_objective_history),
            "R_actual_history_count": len(r_actual_history),
            "Theta_history_count": len(theta_history),
            "erp_buyer_pct": round(erp_buyer, 4) if erp_buyer is not None else None,
            "g_buyer_pct": round(g_buyer, 4) if g_buyer is not None else None,
            "R_actual_pct": round(r_buyer, 4) if r_buyer is not None else None,
            "Theta_actual_over_objective": round(theta, 6) if theta is not None else None,
            "Theta_objective_over_actual": round(theta, 6) if theta is not None else None,
            "buyer_gravity_state": ("NEGATIVE_GRAVITY_CANDIDATE" if r_buyer is not None and r_buyer < 0 else
                                    "NEAR_ZERO" if r_buyer is not None and abs(r_buyer) < 0.25 else
                                    "POSITIVE_GRAVITY" if r_buyer is not None else "UNAVAILABLE"),
            "buyer_source_as_of": buyer_date,
            "buyer_source_age_days": buyer_source_age_days,
            "buyer_source_fresh": buyer_fresh,
            "buyer_horizon_years": buyer_horizon,
            "buyer_return_basis": buyer_basis,
            "buyer_yield_observation_date": buyer_yield_date,
            "buyer_institution_count": summary.get("institution_count"),
            "buyer_source_status": buyer_status,
        },
        "sources": [
            {"name": "World Bank listed domestic market capitalization / GDP (annual)", "url": "https://data.worldbank.org/indicator/CM.MKT.LCAP.GD.ZS?locations=US", "available": bool(bhistory) and buffett.get("frequency") == "annual", "last_observation": bhistory[-1]["date"] if bhistory else None},
            {"name": "FRED Z.1 corporate-equity liabilities/GDP (proxy; not canonical Buffett indicator)", "url": "https://fred.stlouisfed.org/series/NCBEILQ027S", "available": False, "last_observation": None, "status": "仅诊断，不参与巴菲特指标展示"},
            {"name": "Robert Shiller official CAPE data", "url": cape_row.get("source_url") if cape_row else SHILLER_URLS[0], "available": bool(cape_rows), "last_observation": cape_row.get("date") if cape_row else None},
            {"name": "FRED 10-year real Treasury yield DFII10", "url": "https://fred.stlouisfed.org/series/DFII10", "available": y10_real is not None, "last_observation": real_yield.get("date") if real_yield else None},
            {"name": "FRED 10-year nominal Treasury yield DGS10", "url": "https://fred.stlouisfed.org/series/DGS10", "available": y10_nominal is not None, "last_observation": nominal_yield.get("date") if nominal_yield else None},
            {"name": "FRED M1 money stock M1SL", "url": "https://fred.stlouisfed.org/series/M1SL", "available": (macro_obs.get("M1") or {}).get("value") is not None, "last_observation": (macro_obs.get("M1") or {}).get("date")},
            {"name": "FRED M1 velocity M1V", "url": "https://fred.stlouisfed.org/series/M1V", "available": (macro_obs.get("V_M1") or {}).get("value") is not None, "last_observation": (macro_obs.get("V_M1") or {}).get("date")},
            {"name": "FRED GDP deflator GDPDEF", "url": "https://fred.stlouisfed.org/series/GDPDEF", "available": (macro_obs.get("P_GDP_DEFLATOR") or {}).get("value") is not None, "last_observation": (macro_obs.get("P_GDP_DEFLATOR") or {}).get("date")},
            {"name": "FRED real GDP GDPC1", "url": "https://fred.stlouisfed.org/series/GDPC1", "available": (macro_obs.get("Q_REAL_GDP") or {}).get("value") is not None, "last_observation": (macro_obs.get("Q_REAL_GDP") or {}).get("date")},
            {"name": "FRED corporate profits after tax CPATAX (alpha proxy)", "url": "https://fred.stlouisfed.org/series/CPATAX", "available": (macro_obs.get("CORPORATE_PROFIT_AFTER_TAX") or {}).get("value") is not None, "last_observation": (macro_obs.get("CORPORATE_PROFIT_AFTER_TAX") or {}).get("date")},
            {"name": "Dahlquist-Ibert institutional return expectations", "url": MENDELEY_DATASET_URL, "available": erp_buyer is not None, "last_observation": buyer_date, "status": buyer_status},
        ],
        "series": {"buffett": [{"date": r["date"], "value": r["value"], "available_date_proxy": r.get("available_date_proxy")} for r in bhistory],
                   "cape": cape_hist,
                   "R_objective": r_objective_history,
                   "R_actual": r_actual_history,
                   "theta": theta_history},
        "buyer_source": {k: v for k, v in buyer.items() if k not in {"records"}},
        "errors": raw.get("errors", []),
        "data_semantics": {
            "buffett": "US total corporate equity value / annualized nominal GDP; the latest value is updated daily using the S&P 500 as a scaling proxy.",
            "cape": "Official Shiller S&P 500 CAPE; not an index-specific Nasdaq/Dow CAPE.",
            "objective_erp": "100*ln(1+1/CAPE)-10Y real yield, percentage points.",
            "objective_g": "Trailing ten-year real earnings CAGR, backward-looking objective proxy.",
            "R_objective": "real 10Y yield + objective ERP - objective g.",
            "buyer": "Only explicit ERP or same-row equity-minus-cash expectation and explicitly forward-looking g fields with matching return basis/horizon qualify. Historical/observed growth is not relabelled as an expectation.",
            "M1_V_P_Q": "FRED M1SL, M1V, GDPDEF and GDPC1 are aligned to a common as-of date. alpha proxy = CPATAX/GDP; this is an explicitly labelled operational proxy, not a direct observation of the theoretical alpha.",
            "date_matching": "当前 Theta 只用实际回报口径、约 10 年期限的 ERP/g，机构数据不超过 180 天且观测日与 CAPE 相差不超过 45 天；历史回测按观测日+30天保守可用日期，并只用同口径、同期限、无未来信息的配对。",
            "A_over_M": "(alpha_proxy * M1 * V) / (R/100); A/M is suppressed if R<=0 or an input is missing; result is a USD-billions proxy capitalization, not a price target.",
            "Theta": "Theta = R_objective / R_actual. If |R_actual| < 0.25 percentage points, Theta is suppressed. A negative R_actual / Theta is a negative-gravity warning candidate, not automatic proof of a bubble.",
            "no_manual_inputs": True,
        },
        "status": "READY" if bvalue is not None and cape_latest is not None and r_obj is not None and theta is not None else "PARTIAL_SOURCE_DATA",
        "double_gravity_status": "READY" if theta is not None else "WAITING_FOR_CURRENT_MATCHED_BUYER_ERP_G",
        "message": "巴菲特/CAPE 与客观重力自动取数；买方 ERP/g 必须是同为实际口径、约 10 年期限、来源日期新鲜且与 CAPE 相差不超过 45 天的机构数据，否则阻断 R实际/Theta，不允许手填或旧值伪装当前数据。" if theta is None else "双重重力已自动更新；买方与客观端均按实际回报口径和约 10 年期限匹配。Theta 是两项 R 的比值，不是上涨概率。",
    }


def automatic_dashboard(index: str = "SP500", force: bool = False) -> dict[str, Any]:
    idx = index.upper()
    if idx not in INDEXES:
        idx = "SP500"
    try:
        raw = _safe_get_data(force)
        # The macro bundle can be cached while the institutional ZIP is downloading.
        # Refresh the lightweight source status separately so the UI sees completion without a 6h wait.
        raw["buyer"] = _get_buyer_status(start_if_missing=True)
        return _metrics_bundle(idx, raw)
    except Exception as exc:
        buyer = _get_buyer_status(start_if_missing=True)
        return {"ok": False, "module": "AEL_PRO_AUTOMATIC_DOUBLE_GRAVITY", "version": "2.6.45-DATA-UNITS-FIX",
                "requested_index": idx, "status": "SOURCE_ERROR", "message": "自动指标源暂不可用，正在保留可追溯的错误信息；不需要也不允许手动填数。",
                "error": str(exc)[:300], "buyer_source": {k:v for k,v in buyer.items() if k != "records"},
                "sources": [{"name":"Mendeley institutional expectations", "url": MENDELEY_DATASET_URL}]}


def _build_point_in_time_gravity_history(
    cape_rows: list[dict[str, Any]],
    real_yield_rows: list[dict[str, Any]],
    buyer_records: list[dict[str, Any]],
    cutoff: date,
    *,
    cape_release_lag_days: int = 30,
    buyer_release_lag_days: int = 30,
    max_match_gap_days: int = 45,
) -> list[dict[str, Any]]:
    """Build matched historical R/Theta only from rows plausibly available by cutoff.

    This is deliberately conservative because the archived survey date is not always
    a separately recorded publication timestamp. A 30-day availability proxy is used
    for both monthly CAPE data and institutional source observations; the source dates
    and the proxy availability dates are returned for audit.
    """
    objective_rows: list[dict[str, Any]] = []
    for row in cape_rows:
        try:
            obs_date = date.fromisoformat(str(row.get("date", ""))[:10])
        except Exception:
            continue
        available = obs_date + timedelta(days=cape_release_lag_days)
        if available > cutoff or _finite(row.get("cape")) is None:
            continue
        y = _latest_real_yield_known(real_yield_rows, obs_date.isoformat())
        g = _growth_at_date(cape_rows, obs_date.isoformat())
        if y is None or g is None:
            continue
        erp = math.log1p(1.0 / float(row["cape"])) * 100.0 - y
        r_obj = y + erp - g
        objective_rows.append({"date": obs_date.isoformat(), "available_date_proxy": available.isoformat(),
                               "R_objective": r_obj, "ERP_objective": erp, "g_objective": g,
                               "real_yield": y, "cape": float(row["cape"])})
    objective_rows.sort(key=lambda x: (x["date"], x["available_date_proxy"]))

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in buyer_records:
        try:
            obs_date = date.fromisoformat(str(row.get("date", ""))[:10])
        except Exception:
            continue
        available = obs_date + timedelta(days=buyer_release_lag_days)
        horizon = _finite(row.get("horizon_years"))
        if (available > cutoff or row.get("erp_basis") != "real" or row.get("growth_basis") != "real"
                or horizon is None or abs(horizon - 10.0) > 1.0
                or _finite(row.get("buyer_erp_pct")) is None or _finite(row.get("buyer_g_pct")) is None):
            continue
        grouped.setdefault(obs_date.isoformat(), []).append({**row, "available_date_proxy": available.isoformat()})

    result = []
    for buyer_date, rows in sorted(grouped.items()):
        bdate = date.fromisoformat(buyer_date)
        obj_candidates = [o for o in objective_rows if o["date"] <= buyer_date
                          and (bdate - date.fromisoformat(o["date"])).days <= max_match_gap_days
                          and date.fromisoformat(o["available_date_proxy"]) <= date.fromisoformat(rows[0]["available_date_proxy"])]
        if not obj_candidates:
            continue
        obj = obj_candidates[-1]
        rf = _latest_real_yield_known(real_yield_rows, buyer_date)
        if rf is None:
            continue
        erp_b = statistics.median(float(r["buyer_erp_pct"]) for r in rows)
        g_b = statistics.median(float(r["buyer_g_pct"]) for r in rows)
        rb = rf + erp_b - g_b
        if abs(rb) >= 0.25:
            theta = obj["R_objective"] / rb
        else:
            theta = None
        result.append({"signal_date": max(date.fromisoformat(rows[0]["available_date_proxy"]),
                                           date.fromisoformat(obj["available_date_proxy"])).isoformat(),
                       "buyer_observation_date": buyer_date,
                       "objective_observation_date": obj["date"],
                       "buyer_available_date_proxy": rows[0]["available_date_proxy"],
                       "objective_available_date_proxy": obj["available_date_proxy"],
                       "cape_date_gap_days": (bdate - date.fromisoformat(obj["date"])).days,
                       "horizon_years_median": statistics.median(float(r["horizon_years"]) for r in rows),
                       "institution_count": len({r.get("institution") for r in rows}),
                       "R_actual": rb, "R_objective": obj["R_objective"], "Theta": theta,
                       "ERP_buyer": erp_b, "g_buyer": g_b, "risk_free_real": rf,
                       "CAPE": obj["cape"], "g_objective": obj["g_objective"]})
    return sorted(result, key=lambda x: x["signal_date"])


def automatic_year_backtest(year: int, index: str = "SP500", metric: str = "cape", force: bool = False) -> dict[str, Any]:
    idx = index.upper()
    if idx not in INDEXES:
        return {"ok": False, "error": "指数仅支持 SP500、NASDAQ、DOW"}
    if metric not in {"buffett", "cape", "R_actual", "R_objective", "theta"}:
        return {"ok": False, "error": "指标仅支持 buffett、cape、R_actual、R_objective、theta"}
    today = date.today()
    if year < 1900 or year > today.year:
        return {"ok": False, "error": f"年份须在 1900 至 {today.year} 之间"}
    raw = _safe_get_data(force)
    data = _metrics_bundle(idx, raw)
    chosen = INDEXES[idx]
    price_rows = _yahoo_history(chosen["symbol"], date(max(1900, year - 1), 1, 1), force)
    target = date(year, 12, 31)
    candidates = [r for r in price_rows if r["date"] <= target.isoformat()]
    year_rows = [r for r in price_rows if r["date"][:4] == str(year)]
    if not year_rows:
        return {"ok": False, "error": f"{chosen['name']} 在 {year} 年没有可用的指数价格。"}
    anchor = year_rows[-1]
    anchor_date = date.fromisoformat(anchor["date"])
    if not candidates:
        return {"ok": False, "error": f"没有找到 {year} 年末之前的价格锚点。"}
    # Values known at that year end (CAPE; Buffett uses only quarterly observations available by then).
    cape_hist = raw.get("cape") or []
    cands_cape = []
    for r in cape_hist:
        try:
            cape_day = date.fromisoformat(str(r.get("date", ""))[:10])
        except Exception:
            continue
        if cape_day + timedelta(days=30) <= target and _finite(r.get("cape")) is not None:
            cands_cape.append(r)
    cape_row = cands_cape[-1] if cands_cape else None
    buffett_hist = ((raw.get("buffett") or {}).get("history") or [])
    known_buffett = [r for r in buffett_hist if r.get("available_date_proxy", r["date"]) <= target.isoformat()]
    buffett_row = known_buffett[-1] if known_buffett else None
    value = None
    if metric == "cape" and cape_row:
        value = cape_row.get("cape")
    elif metric == "buffett" and buffett_row:
        value = buffett_row.get("value")
    # Build only source/date/horizon-matched historical gravity signals available by year-end.
    buyer_records = (raw.get("buyer") or {}).get("records") or []
    matched_gravity_history = _build_point_in_time_gravity_history(
        cape_hist, raw.get("real_yield", {}).get("history", []), buyer_records, target)
    eligible_gravity = [r for r in matched_gravity_history if r["signal_date"] <= target.isoformat()]
    gravity_row = eligible_gravity[-1] if eligible_gravity else None
    obj_rows = []
    for r in cape_hist:
        try:
            obs_date = date.fromisoformat(str(r.get("date", ""))[:10])
        except Exception:
            continue
        # Shiller monthly rows are treated as available 30 days after their observation month.
        if obs_date + timedelta(days=30) > target or r.get("cape") is None:
            continue
        y = _latest_real_yield_known(raw.get("real_yield", {}).get("history", []), obs_date.isoformat())
        g = _growth_at_date(cape_hist, obs_date.isoformat())
        if y is not None and g is not None:
            erp = math.log1p(1.0 / r["cape"]) * 100 - y
            rr = y + erp - g
            obj_rows.append({"date": r["date"], "available_date_proxy": (obs_date + timedelta(days=30)).isoformat(),
                             "R_objective": rr, "cape": r["cape"], "g": g, "real_yield": y})
    obj_row = obj_rows[-1] if obj_rows else None
    buyer_row = ({"date": gravity_row["buyer_observation_date"],
                  "available_date_proxy": gravity_row["buyer_available_date_proxy"],
                  "R_actual": gravity_row["R_actual"], "ERP": gravity_row["ERP_buyer"],
                  "g": gravity_row["g_buyer"], "rf": gravity_row["risk_free_real"],
                  "horizon_years": gravity_row["horizon_years_median"],
                  "basis": "real", "institution_count": gravity_row["institution_count"]}
                 if gravity_row else None)
    # For year-specific R/Theta, use the latest genuinely matched joint signal.
    selected_obj_row = ({"date": gravity_row["objective_observation_date"],
                         "R_objective": gravity_row["R_objective"], "cape": gravity_row["CAPE"],
                         "g": gravity_row["g_objective"]} if gravity_row else obj_row)
    if metric == "R_objective" and selected_obj_row:
        value = selected_obj_row["R_objective"]
    elif metric == "R_actual" and buyer_row:
        value = buyer_row["R_actual"]
    elif metric == "theta" and gravity_row and gravity_row.get("Theta") is not None:
        value = gravity_row["Theta"]
    # forward returns use adjusted close when Yahoo supplies it; index series may only have close.
    horizons = []
    for years in (1, 3, 5, 10):
        end_year = anchor_date.year + years
        end_month = anchor_date.month
        end_day = min(anchor_date.day, [31, 29 if end_year % 4 == 0 and (end_year % 100 != 0 or end_year % 400 == 0) else 28,31,30,31,30,31,31,30,31,30,31][end_month-1])
        target_h = date(end_year, end_month, end_day)
        end_rows = [r for r in price_rows if r["date"] >= target_h.isoformat()]
        if target_h > today or not end_rows:
            horizons.append({"years": years, "target_date": target_h.isoformat(), "actual_date": None,
                             "return_pct": None, "status": "NOT_MATURED_OR_MISSING", "message": "期限尚未到期或来源没有覆盖该日期"})
            continue
        end = end_rows[0]
        end_date = date.fromisoformat(end["date"])
        elapsed = (end_date - anchor_date).days / 365.2425
        ret = (end["value"] / anchor["value"] - 1) * 100
        cagr = ((end["value"] / anchor["value"]) ** (1 / elapsed) - 1) * 100 if elapsed > 0 else None
        horizons.append({"years": years, "target_date": target_h.isoformat(), "actual_date": end["date"],
                         "return_pct": round(ret, 4), "cagr_pct": round(cagr, 4) if cagr is not None else None,
                         "status": "OBSERVED", "price_series_used": end["kind"]})
    metric_values = {
        "buffett": buffett_row.get("value") if buffett_row else None,
        "cape": cape_row.get("cape") if cape_row else None,
        "R_actual": gravity_row.get("R_actual") if gravity_row else None,
        "R_objective": gravity_row.get("R_objective") if gravity_row else (obj_row.get("R_objective") if obj_row else None),
        "theta": gravity_row.get("Theta") if gravity_row else None,
    }
    hist_for_metric = []
    if metric == "cape": hist_for_metric = [r["cape"] for r in cape_hist if r["date"] <= target.isoformat() and r.get("cape") is not None]
    elif metric == "buffett": hist_for_metric = [r["value"] for r in buffett_hist if r.get("available_date_proxy", r["date"]) <= target.isoformat()]
    elif metric == "R_objective": hist_for_metric = [r["R_objective"] for r in obj_rows]
    elif metric == "R_actual": hist_for_metric = [r["R_actual"] for r in matched_gravity_history if r["signal_date"] <= target.isoformat()]
    elif metric == "theta": hist_for_metric = [r["Theta"] for r in matched_gravity_history if r["signal_date"] <= target.isoformat() and r.get("Theta") is not None]
    percentile = _percentile(value, hist_for_metric) if value is not None else None
    selected_observation_date = ((cape_row or {}).get("date") if metric == "cape" else
                                 ((buffett_row or {}).get("date") if metric == "buffett" else
                                  ((gravity_row or {}).get("buyer_observation_date") if metric in {"R_actual", "theta"} else
                                   ((selected_obj_row or {}).get("date") if metric == "R_objective" else None))))
    return {
        "ok": True, "module": "AEL_PRO_AUTOMATIC_YEAR_BACKTEST", "selected_year": year,
        "index": idx, "index_name": chosen["name"], "index_symbol": chosen["symbol"],
        "metric": metric, "metric_value": round(value, 5) if value is not None else None,
        "metric_percentile_at_year": percentile,
        "metric_observation_date": selected_observation_date,
        "indicator_values": {k: round(v, 5) if v is not None else None for k, v in metric_values.items()},
        "anchor_date": anchor["date"], "anchor_value": anchor["value"],
        "price_series_used": "adjusted close when supplied by Yahoo; otherwise index close proxy",
        "horizons": horizons,
        "formal_theta_signal_available": bool(gravity_row and gravity_row.get("Theta") is not None),
        "matched_theta_history_count": sum(1 for r in matched_gravity_history if r.get("Theta") is not None),
        "buyer_historical_match": ({"buyer_observation_date": gravity_row.get("buyer_observation_date"),
                                    "objective_observation_date": gravity_row.get("objective_observation_date"),
                                    "signal_date": gravity_row.get("signal_date"),
                                    "available_date_proxy": gravity_row.get("buyer_available_date_proxy"),
                                    "horizon_years": gravity_row.get("horizon_years_median"),
                                    "institution_count": gravity_row.get("institution_count"),
                                    "cape_date_gap_days": gravity_row.get("cape_date_gap_days")}
                                   if gravity_row else None),
        "status": "INDICATOR_AND_FORWARD_RETURNS" if value is not None else "INDICATOR_VALUE_UNAVAILABLE",
        "warning": "历史指标只使用所选年份末及之前可用的观测；机构源使用观测日期+30天作为保守可用日期；巴菲特季度数据按观测日+75天作为保守可用日期。R实际只接受 real ERP + real forward g、9–11 年期限，且与客观 CAPE 日期相差不超过 45 天。指数后续表现是实际市场回报，不等于策略收益；Theta 只有在匹配的机构 ERP/g 历史信号可识别时才计算。",
        "sources": data.get("sources", []),
    }


def _latest_real_yield_known(rows: list[dict[str, Any]], as_of: str) -> float | None:
    eligible = [r for r in rows if r.get("date", "") <= as_of and r.get("value") is not None]
    return eligible[-1]["value"] if eligible else None


def _growth_at_date(cape_rows: list[dict[str, Any]], as_of: str) -> float | None:
    eligible = [r for r in cape_rows if r.get("date", "") <= as_of and r.get("real_earnings_growth_10y_pct") is not None]
    return eligible[-1]["real_earnings_growth_10y_pct"] if eligible else None



def charter_theory_history(force: bool = False, limit: int = 320) -> dict[str, Any]:
    """Long-run macro analogues for the user's chartered-tax-company identities.

    Uses FRED quarterly observations. The identities are tested as a mapping:
    Q/V = M2/P and 1/P = Q/(M2*V), with P expressed as the GDP-deflator
    multiplier (GDPDEF/100). V is derived as nominal GDP / M2. This is a
    macroeconomic proxy series, not observed total listed-share count or a
    stock-index price series; revised history is not point-in-time vintage data.
    """
    limit = max(40, min(600, int(limit)))
    ids = {"M2": "M2SL", "GDP": "GDP", "P": "GDPDEF", "Q": "GDPC1"}
    fetched: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for key, series_id in ids.items():
        try:
            fetched[key] = _series_csv(series_id, force=force)
        except Exception as exc:
            errors.append(f"{series_id}: {str(exc)[:120]}")
            fetched[key] = []
    if any(not fetched[k] for k in ids):
        return {"ok": False, "status": "SOURCE_INCOMPLETE", "message": "长期趋势数据源不完整；不会用缺失值补齐。", "errors": errors, "series": {}}

    def latest_at(rows: list[dict[str, Any]], d: str) -> dict[str, Any] | None:
        eligible = [r for r in rows if r.get("date", "") <= d and _finite(r.get("value")) is not None]
        return eligible[-1] if eligible else None

    output = []
    for qrow in fetched["Q"]:
        d = qrow["date"]
        m2row, gdprow, prow = latest_at(fetched["M2"], d), latest_at(fetched["GDP"], d), latest_at(fetched["P"], d)
        q, m2, gdp, p_index = (_finite(qrow.get("value")), _finite(m2row.get("value")) if m2row else None,
                               _finite(gdprow.get("value")) if gdprow else None, _finite(prow.get("value")) if prow else None)
        if None in (q, m2, gdp, p_index) or min(q, m2, gdp, p_index) <= 0:
            continue
        p = p_index / 100.0
        v = gdp / m2
        # Both identities are independently calculated, not forced equal.
        q_over_v = q / v
        m2_over_p = m2 / p
        inv_p = 1.0 / p
        q_over_mv = q / (m2 * v)
        output.append({"date": d, "Q": q, "M2": m2, "GDP_nominal": gdp,
                       "P_deflator_index": p_index, "P_multiplier": p, "V": v,
                       "Q_over_V_billions": q_over_v, "M2_over_P_billions": m2_over_p,
                       "identity_gap_pct": ((q_over_v / m2_over_p) - 1.0) * 100.0,
                       "one_over_P": inv_p, "Q_over_MV": q_over_mv,
                       "price_identity_gap_pct": ((inv_p / q_over_mv) - 1.0) * 100.0,
                       "source_dates": {"Q": d, "M2": m2row["date"], "GDP": gdprow["date"], "P": prow["date"]}})
    if len(output) < 40:
        return {"ok": False, "status": "INSUFFICIENT_HISTORY", "message": "共同口径历史不足40个季度。", "errors": errors, "series": {}}
    # Keep quarterly history and a concise set of display metadata.
    output = output[-limit:]
    latest = output[-1]
    return {"ok": True, "status": "READY", "as_of": latest["date"], "count": len(output),
            "start_date": output[0]["date"], "end_date": latest["date"], "series": output,
            "metrics": latest,
            "summary": {"mean_identity_gap_pct": statistics.mean(r["identity_gap_pct"] for r in output),
                        "max_abs_identity_gap_pct": max(abs(r["identity_gap_pct"]) for r in output),
                        "mean_price_identity_gap_pct": statistics.mean(r["price_identity_gap_pct"] for r in output),
                        "max_abs_price_identity_gap_pct": max(abs(r["price_identity_gap_pct"]) for r in output)},
            "sources": [{"name": "FRED M2SL", "url": "https://fred.stlouisfed.org/series/M2SL", "available": bool(fetched["M2"]), "last_observation": fetched["M2"][-1]["date"]},
                        {"name": "FRED GDP（名义GDP）", "url": "https://fred.stlouisfed.org/series/GDP", "available": bool(fetched["GDP"]), "last_observation": fetched["GDP"][-1]["date"]},
                        {"name": "FRED GDPDEF（GDP平减指数）", "url": "https://fred.stlouisfed.org/series/GDPDEF", "available": bool(fetched["P"]), "last_observation": fetched["P"][-1]["date"]},
                        {"name": "FRED GDPC1（实际GDP）", "url": "https://fred.stlouisfed.org/series/GDPC1", "available": bool(fetched["Q"]), "last_observation": fetched["Q"][-1]["date"]}],
            "warning": "这是把宏观数据映射到你的费雪公式股票字典后的长期趋势代理，不是直接观测到的美股总股数/单股价格。FRED历史值可能被修订，不是严格实时点时序列。V=名义GDP/M2；P=GDPDEF/100。由于V由名义GDP/M2构造，且名义GDP与Q、P之间存在国民核算关系，两组等式偏差主要是代数/数据口径审计，不构成独立预测检验。"}

def automatic_history(index: str = "SP500", limit: int = 240, force: bool = False) -> dict[str, Any]:
    dashboard = automatic_dashboard(index, force)
    if not dashboard.get("ok"):
        return dashboard
    series = dashboard.get("series") or {}
    b = series.get("buffett") or []
    c = series.get("cape") or []
    r_obj = series.get("R_objective") or []
    r_actual = series.get("R_actual") or []
    theta = series.get("theta") or []
    limit = max(12, min(600, int(limit)))
    return {"ok": True, "index": index, "count_buffett": len(b), "count_cape": len(c),
            "count_R_objective": len(r_obj), "count_R_actual": len(r_actual), "count_theta": len(theta),
            "buffett": b[-limit:], "cape": c[-limit:], "R_objective": r_obj[-limit:],
            "R_actual": r_actual[-limit:], "theta": theta[-limit:], "current": dashboard.get("metrics"),
            "sources": dashboard.get("sources", []), "warning": dashboard.get("data_semantics", {})}
