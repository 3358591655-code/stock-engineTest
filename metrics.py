import math
import os
import re
import time
from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf

SEC_UA = os.getenv(
    "SEC_USER_AGENT",
    "stock-fundamental-dashboard/2.2 contact@example.com",
)
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
_SEC_TICKERS = None


def clean_symbol(raw: str) -> str:
    s = str(raw or "").strip().upper().replace(" ", "")
    if re.fullmatch(r"\d{6}", s):
        return s + (".SS" if s.startswith(("5", "6", "68", "9")) else ".SZ")
    if re.fullmatch(r"\d{1,5}", s):
        return s.zfill(4) + ".HK"
    # Yahoo uses hyphens for share classes such as BRK.B.
    if "." in s and re.fullmatch(r"[A-Z]{1,6}\.[A-Z]", s):
        s = s.replace(".", "-")
    return s


def is_us_symbol(symbol: str) -> bool:
    return not symbol.endswith((".HK", ".SS", ".SZ")) and bool(re.fullmatch(r"[A-Z0-9-]+", symbol))


def finite(v):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def pct(v):
    x = finite(v)
    return None if x is None else x * 100


def _names(names):
    return [names] if isinstance(names, str) else list(names)


def latest_row(df, names):
    if df is None or getattr(df, "empty", True):
        return None
    for name in _names(names):
        if name in df.index:
            try:
                s = df.loc[name]
                for v in list(s):
                    x = finite(v)
                    if x is not None:
                        return x
            except Exception:
                pass
    return None


def series_value(df, names, col):
    if df is None or getattr(df, "empty", True):
        return None
    try:
        for row in _names(names):
            if row in df.index:
                return finite(df.loc[row, col])
    except Exception:
        pass
    return None


def safe_ratio(a, b):
    a, b = finite(a), finite(b)
    if a is None or b in (None, 0):
        return None
    return a / b


def _safe_get(obj, attr, errors, default=None, *args, **kwargs):
    try:
        value = getattr(obj, attr)
        if callable(value):
            value = value(*args, **kwargs)
        return value
    except Exception as exc:
        errors[attr] = str(exc)[:240]
        return default


def normalize_history(df):
    if df is None or getattr(df, "empty", True):
        return None
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        # yf.download can return (Price, Ticker) columns.
        if out.columns.nlevels == 2:
            first = out.columns.get_level_values(0)
            if "Close" in first:
                out.columns = first
            else:
                out.columns = out.columns.get_level_values(-1)
    if "Close" not in out.columns:
        return None
    out = out.dropna(subset=["Close"])
    return out if not out.empty else None


def yahoo_chart_history(symbol, errors):
    """Direct Yahoo chart endpoint fallback; avoids yfinance history-cache failures."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2y", "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        r.raise_for_status()
        payload = r.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise RuntimeError(((payload.get("chart") or {}).get("error") or {}).get("description", "Yahoo chart returned no result"))
        result = result[0]
        ts = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        if not ts or not quote.get("close"):
            raise RuntimeError("Yahoo chart returned no daily prices")
        idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(None)
        data = {"Open": quote.get("open"), "High": quote.get("high"), "Low": quote.get("low"), "Close": quote.get("close"), "Volume": quote.get("volume")}
        return pd.DataFrame(data, index=idx).dropna(subset=["Close"])
    except Exception as exc:
        errors["yahoo_chart"] = str(exc)[:240]
        return None


def get_history(ticker, symbol, errors):
    try:
        h = ticker.history(period="2y", interval="1d", auto_adjust=False, repair=False, raise_errors=False)
        h = normalize_history(h)
        if h is not None and len(h) >= 30:
            return h
    except Exception as exc:
        errors["history"] = str(exc)[:240]
    try:
        h = yf.download(symbol, period="2y", interval="1d", auto_adjust=False, progress=False, threads=False, repair=False)
        h = normalize_history(h)
        if h is not None and len(h) >= 30:
            return h
    except Exception as exc:
        errors["download"] = str(exc)[:240]
    return yahoo_chart_history(symbol, errors)


def annual_roe(inc, bs):
    if inc is None or inc.empty or bs is None or bs.empty:
        return []
    bs_cols = list(bs.columns)
    rows = []
    for col in list(inc.columns):
        year = getattr(col, "year", None)
        if year is None:
            continue
        net_income = series_value(inc, ["Net Income", "Net Income Common Stockholders"], col)
        equity_end = series_value(bs, ["Stockholders Equity", "Common Stock Equity", "Stockholders Equity Including Minority Interest"], col)
        prior = [c for c in bs_cols if getattr(c, "year", None) == year - 1]
        equity_begin = None
        if prior:
            equity_begin = series_value(bs, ["Stockholders Equity", "Common Stock Equity", "Stockholders Equity Including Minority Interest"], prior[0])
        if net_income is None or equity_begin is None or equity_end is None:
            continue
        avg_equity = (equity_begin + equity_end) / 2
        if avg_equity == 0:
            continue
        roe = net_income / avg_equity * 100
        if math.isfinite(roe):
            rows.append({"year": int(year), "net_income": net_income, "equity_begin": equity_begin, "equity_end": equity_end, "roe": roe})
    rows.sort(key=lambda x: x["year"])
    return rows[-15:]


def stats(values):
    vals = [finite(x) for x in values if finite(x) is not None]
    if not vals:
        return {"count": 0, "average": None, "median": None, "std_dev": None, "range": None, "max": None, "min": None}
    arr = np.array(vals, dtype=float)
    return {"count": len(vals), "average": float(arr.mean()), "median": float(np.median(arr)), "std_dev": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
            "range": float(arr.max() - arr.min()), "max": float(arr.max()), "min": float(arr.min())}


def dividend_metrics(divs, cf, price, fcf, net_income, dividend_error=None):
    out = {
        "status": "unavailable" if dividend_error else "no_dividend",
        "ttm_dividend_per_share": 0.0 if not dividend_error else None,
        "dividend_yield": 0.0 if not dividend_error else None,
        "history": [], "cagr_3y": None, "cagr_5y": None, "cagr_10y": None,
        "consecutive_years": 0, "dividend_payout_ratio": None,
        "fcf_payout_ratio": None, "buybacks": None,
        "shareholder_payout": None, "shareholder_payout_ratio": None,
        "trend": "无现金股息" if not dividend_error else "暂无数据",
        "latest_complete_year": None,
        "latest_period": None,
    }
    if divs is not None and not getattr(divs, "empty", True):
        try:
            divs = divs.dropna().astype(float)
            if not isinstance(divs.index, pd.DatetimeIndex):
                divs.index = pd.to_datetime(divs.index)
            annual = divs.groupby(divs.index.year).sum().sort_index()
            out["status"] = "has_dividend"

            idx_tz = getattr(divs.index, "tz", None)
            now = pd.Timestamp.now(tz=idx_tz) if idx_tz is not None else pd.Timestamp.now()
            current_year = int(now.year)
            complete_years = [int(y) for y in annual.index if int(y) < current_year]
            latest_complete = max(complete_years) if complete_years else None
            out["latest_complete_year"] = latest_complete
            out["history"] = [
                {"year": int(y), "dividend_per_share": float(v),
                 "complete": int(y) < current_year}
                for y, v in annual.items()
            ]

            cutoff = now - pd.Timedelta(days=365)
            ttm = float(divs[divs.index >= cutoff].sum())
            if ttm <= 0 and latest_complete is not None:
                ttm = float(annual.loc[latest_complete])
            out["ttm_dividend_per_share"] = ttm
            if price:
                out["dividend_yield"] = ttm / price * 100

            if annual.index.max() == current_year:
                out["latest_period"] = f"{current_year} YTD"
            elif annual.index.max() is not None:
                out["latest_period"] = str(int(annual.index.max()))

            # CAGR must use completed annual observations only. This prevents
            # a partial current year (e.g. 2026 YTD) from distorting CAGR.
            completed = annual.loc[annual.index.astype(int) < current_year]
            def cagr(years):
                if completed.empty:
                    return None
                end_year = int(completed.index[-1]); target = end_year - years
                eligible = [int(y) for y in completed.index if int(y) <= target]
                if not eligible:
                    return None
                start_year = max(eligible); actual = end_year - start_year
                if actual <= 0:
                    return None
                start, endv = float(completed.loc[start_year]), float(completed.loc[end_year])
                if start <= 0 or endv <= 0:
                    return None
                return ((endv / start) ** (1 / actual) - 1) * 100
            out["cagr_3y"], out["cagr_5y"], out["cagr_10y"] = cagr(3), cagr(5), cagr(10)

            count = 0
            if latest_complete is not None:
                for y in range(latest_complete, latest_complete - 30, -1):
                    if y in completed.index and float(completed.loc[y]) > 0:
                        count += 1
                    else:
                        break
            out["consecutive_years"] = count
            if out["cagr_5y"] is not None:
                out["trend"] = "上升" if out["cagr_5y"] > 1 else ("下降" if out["cagr_5y"] < -1 else "基本稳定")
        except Exception as exc:
            out["history_error"] = str(exc)[:240]

    dividends_paid = latest_row(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid", "Common Stock Payments", "Payment Of Dividends"])
    buybacks = latest_row(cf, ["Repurchase Of Capital Stock", "Repurchase Of Capital Stock Issuance", "Common Stock Payments"])
    dividends_paid = abs(dividends_paid) if dividends_paid is not None else (0.0 if out["status"] == "no_dividend" else None)
    buybacks = abs(buybacks) if buybacks is not None else None
    out["buybacks"] = buybacks
    if dividends_paid is not None and net_income not in (None, 0):
        out["dividend_payout_ratio"] = dividends_paid / abs(net_income) * 100
    if dividends_paid is not None and fcf not in (None, 0):
        out["fcf_payout_ratio"] = dividends_paid / abs(fcf) * 100
    if dividends_paid is not None or buybacks is not None:
        total = (dividends_paid or 0) + (buybacks or 0)
        out["shareholder_payout"] = total
        if fcf not in (None, 0):
            out["shareholder_payout_ratio"] = total / abs(fcf) * 100
    return out


def pivot_levels(history):
    """Calculate weekly pivot points in the same convention as the reference quote app.

    Reference-app convention observed for the AAPL example:
    - The displayed daily "轴点" table is based on the *previous completed week*.
    - Weekly H/L/C are aggregated from the daily candles of Monday-Friday.
    - For the week Sep 21-25, 2026, the pivot table therefore uses Sep 14-18,
      whose weekly H/L/C are 338.49 / 328.35 / 336.13.
    - Classic and Fibonacci levels are then calculated from that same weekly H/L/C.

    This is deliberately separate from Fibonacci retracement, which uses the
    252-trading-day swing high/low elsewhere in the dashboard.
    """
    out = {
        "available": False, "date": None, "period": "previous_week",
        "high": None, "low": None, "close": None,
        "classic": {}, "fibonacci": {},
        "fibonacci_ratios": {"R1": 0.382, "R2": 0.618, "R3": 1.0},
    }
    if history is None or getattr(history, "empty", True):
        return out
    try:
        cols = {str(c).lower(): c for c in history.columns}
        if not all(k in cols for k in ("high", "low", "close")):
            return out
        hcol, lcol, ccol = cols["high"], cols["low"], cols["close"]

        df = history[[hcol, lcol, ccol]].copy()
        idx = pd.to_datetime(df.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert(None)
        df.index = idx.normalize()
        df = df.dropna(subset=[hcol, lcol, ccol])
        if df.empty:
            return out

        # Build Monday-Friday trading weeks from the daily candles.
        weekly = df.resample("W-FRI", label="right", closed="right").agg({
            hcol: "max", lcol: "min", ccol: "last"
        }).dropna()
        if weekly.empty:
            return out

        # A quote app's daily pivot table stays fixed for the whole current
        # trading week, so it uses the immediately preceding completed week.
        # If the latest data belongs to an older week (e.g. weekend/holiday),
        # that latest completed week is already the appropriate reference week.
        today = pd.Timestamp.now().normalize()
        current_week_start = today - pd.Timedelta(days=today.weekday())
        last_week_end = pd.Timestamp(weekly.index[-1]).normalize()
        if last_week_end >= current_week_start and len(weekly) >= 2:
            ref = weekly.iloc[-2]
            ref_date = pd.Timestamp(weekly.index[-2]).normalize()
        else:
            ref = weekly.iloc[-1]
            ref_date = last_week_end

        high, low, close = float(ref[hcol]), float(ref[lcol]), float(ref[ccol])
        if not all(math.isfinite(x) for x in (high, low, close)) or high <= low:
            return out

        rng = high - low
        # Match the reference quote app: round the central pivot to 2 decimals
        # before deriving every support/resistance level.
        pp = round((high + low + close) / 3.0, 2)
        classic = {
            "R3": round(high + 2 * (pp - low), 2),
            "R2": round(pp + rng, 2),
            "R1": round(2 * pp - low, 2),
            "轴心点": round(pp, 2),
            "S1": round(2 * pp - high, 2),
            "S2": round(pp - rng, 2),
            "S3": round(low - 2 * (high - pp), 2),
        }
        fibonacci = {
            "R3": round(pp + 1.000 * rng, 2),
            "R2": round(pp + 0.618 * rng, 2),
            "R1": round(pp + 0.382 * rng, 2),
            "轴心点": round(pp, 2),
            "S1": round(pp - 0.382 * rng, 2),
            "S2": round(pp - 0.618 * rng, 2),
            "S3": round(pp - 1.000 * rng, 2),
        }
        out.update({
            "available": True,
            "date": str(ref_date.date()),
            "high": high,
            "low": low,
            "close": close,
            "classic": classic,
            "fibonacci": fibonacci,
        })
    except Exception:
        pass
    return out


def fibonacci_levels(history):
    out = {"available": False, "swing_high": None, "swing_low": None, "levels": {}, "nearest_support": None, "nearest_resistance": None}
    if history is None or getattr(history, "empty", True) or "Close" not in history:
        return out
    try:
        h = history.tail(252)
        close = h["Close"].dropna().astype(float)
        if len(close) < 30:
            return out
        swing_high = float(h["High"].dropna().astype(float).max()) if "High" in h else float(close.max())
        swing_low = float(h["Low"].dropna().astype(float).min()) if "Low" in h else float(close.min())
        if swing_high <= swing_low:
            return out
        current = float(close.iloc[-1])
        diff = swing_high - swing_low
        ratios = {"0.0%": 0.0, "23.6%": 0.236, "38.2%": 0.382, "50.0%": 0.5, "61.8%": 0.618, "78.6%": 0.786, "100.0%": 1.0}
        levels = {label: swing_high - diff * ratio for label, ratio in ratios.items()}
        below = [(label, value) for label, value in levels.items() if value < current]
        above = [(label, value) for label, value in levels.items() if value > current]
        support = max(below, key=lambda x: x[1]) if below else None
        resistance = min(above, key=lambda x: x[1]) if above else None
        out.update({"available": True, "swing_high": swing_high, "swing_low": swing_low,
                    "levels": levels,
                    "nearest_support": {"level": support[0], "price": support[1]} if support else None,
                    "nearest_resistance": {"level": resistance[0], "price": resistance[1]} if resistance else None})
    except Exception:
        pass
    return out


def resonance_levels(current, pivots, indicators):
    """Find Pivot Point / Bollinger Band resonance using the reference-app rule.

    Resonance is evaluated only between the matching structural levels:
    - R1/R2/R3 (classic or Fibonacci Pivot) vs Bollinger upper band
    - central Pivot vs Bollinger middle band
    - S1/S2/S3 (classic or Fibonacci Pivot) vs Bollinger lower band

    A match is considered overlapping when the absolute relative difference
    is <= 1%. This replaces the old Fibonacci-retracement + Bollinger
    clustering logic. It is descriptive market structure, not a trade signal.
    """
    out = {
        "available": False, "tolerance_pct": 1.0,
        "support": [], "resistance": [], "matches": []
    }
    current = finite(current)
    if current is None or not pivots or not pivots.get("available"):
        return out

    indicators = indicators or {}
    bb = {
        "upper": finite(indicators.get("bb_upper")),
        "mid": finite(indicators.get("bb_mid")),
        "lower": finite(indicators.get("bb_lower")),
    }
    if not any(v is not None for v in bb.values()):
        return out

    specs = [
        ("upper", ["R1", "R2", "R3"], "布林上轨"),
        ("mid", ["轴心点"], "布林中轨"),
        ("lower", ["S1", "S2", "S3"], "布林下轨"),
    ]
    matches = []
    for method_key, method_name in (("classic", "经典"), ("fibonacci", "斐波纳契")):
        levels = pivots.get(method_key) or {}
        for bb_key, labels, bb_name in specs:
            bb_value = bb.get(bb_key)
            if bb_value is None or bb_value == 0:
                continue
            for label in labels:
                pivot_value = finite(levels.get(label))
                if pivot_value is None:
                    continue
                distance_pct = (pivot_value / bb_value - 1.0) * 100.0
                if abs(distance_pct) <= 1.0:
                    matches.append({
                        "price": round((pivot_value + bb_value) / 2.0, 2),
                        "pivot_price": pivot_value,
                        "bollinger_price": bb_value,
                        "pivot": f"{method_name} {label}",
                        "bollinger": bb_name,
                        "distance_pct": distance_pct,
                        "count": 2,
                        "sources": [f"{method_name} {label}", bb_name],
                        "type": "support" if bb_key == "lower" or (bb_key == "mid" and pivot_value < current) else "resistance",
                    })

    # Sort closest matches first; keep every valid pair so the user can see
    # exactly which Pivot level overlaps which Bollinger band.
    matches.sort(key=lambda x: abs(x["distance_pct"]))
    out["matches"] = matches
    out["support"] = [x for x in matches if x["type"] == "support"][:6]
    out["resistance"] = [x for x in matches if x["type"] == "resistance"][:6]
    out["available"] = bool(matches)
    return out


def technical_price_chart(history, fib):
    """Build OHLC candles + Bollinger + pivot support/resistance lines.

    MA values are deliberately NOT plotted on the K-line. They remain available
    to the technical score and the MA cards below.
    """
    out={"available":False,"points":[],"bollinger_points":[],"signals":[],
         "current":None,"support":None,"resistance":None,"ma_points":[],"volume_max":None,"timeframe":"1D"}
    if history is None or getattr(history,"empty",True) or "Close" not in history:
        return out
    try:
        cols=[c for c in ("Open","High","Low","Close","Volume") if c in history.columns]
        h=history[cols].copy()
        for c in cols: h[c]=pd.to_numeric(h[c],errors="coerce")
        h=h.dropna(subset=["Close"]).copy()
        if len(h)<40: return out
        for c in ("Open","High","Low"):
            if c not in h.columns: h[c]=h["Close"]
        h["Open"]=h["Open"].fillna(h["Close"])
        h["High"]=h["High"].fillna(h[["Open","Close"]].max(axis=1))
        h["Low"]=h["Low"].fillna(h[["Open","Close"]].min(axis=1))
        if "Volume" not in h.columns: h["Volume"]=np.nan
        close=h["Close"]
        mid=close.rolling(20).mean(); sd=close.rolling(20).std(); upper,lower=mid+2*sd,mid-2*sd
        view=h.tail(180)
        support=fib.get("nearest_support") if fib else None
        resistance=fib.get("nearest_resistance") if fib else None
        out["points"]=[{"date":str(idx.date()),"open":finite(row["Open"]),"high":finite(row["High"]),"low":finite(row["Low"]),"close":finite(row["Close"]),"volume":finite(row["Volume"])} for idx,row in view.iterrows()]
        out["bollinger_points"]=[{"date":str(idx.date()),"upper":finite(upper.loc[idx]),"mid":finite(mid.loc[idx]),"lower":finite(lower.loc[idx])} for idx in view.index]
        vols=view["Volume"].dropna(); out["volume_max"]=float(vols.max()) if not vols.empty else None
        out["current"]=float(close.iloc[-1]); out["support"]=support; out["resistance"]=resistance; out["available"]=True
    except Exception:
        pass
    return out

def _score_0_100(value, low, high):
    v=finite(value)
    if v is None or high == low:
        return None
    return max(0.0, min(100.0, (v-low)/(high-low)*100.0))


def _distance_bonus(price, level, full_pct=5.0):
    """100 near a support level; fades to 0 at full_pct away."""
    p, lv = finite(price), finite(level)
    if p is None or lv in (None, 0): return None
    d=abs(p/lv-1.0)*100.0
    return max(0.0, min(100.0, (1.0-d/full_pct)*100.0))


def _technical_value_score(rsi, kdj_j, macd_hist, macd_hist_delta, fib_s3, bb_lower, price):
    # Technical Value is a transparent 5-factor, 20% each score.
    # Missing inputs are not estimated; the final score is only calculated
    # from available components, while the breakdown records exactly what
    # contributed.
    components=[]
    if rsi is not None:
        if 30 <= rsi <= 40: rsi_s=100
        elif 20 <= rsi < 30: rsi_s=92 + (rsi-20)*0.8
        elif rsi < 20: rsi_s=75 + max(0,rsi)*0.85
        elif 40 < rsi <= 50: rsi_s=100-(rsi-40)*3.0
        elif 50 < rsi <= 60: rsi_s=70-(rsi-50)*3.0
        elif 60 < rsi <= 70: rsi_s=40-(rsi-60)*3.0
        else: rsi_s=max(0,20-(rsi-70)*1.0)
        components.append({"name":"RSI","score":float(rsi_s),"weight":20,"available":True})
    if kdj_j is not None:
        if kdj_j < 0: j_s=100
        elif kdj_j <= 20: j_s=95
        elif kdj_j <= 40: j_s=75
        elif kdj_j <= 60: j_s=50
        elif kdj_j <= 80: j_s=30
        else: j_s=15
        components.append({"name":"KDJ J","score":float(j_s),"weight":20,"available":True})
    if macd_hist is not None:
        scale=max(abs(finite(price) or 1)*0.01, 1e-9)
        neg=max(0.0,min(1.0,(-macd_hist)/scale))
        improve=1.0 if macd_hist_delta is not None and macd_hist_delta>0 else 0.0
        macd_s=45 + 35*neg + 20*improve if macd_hist<0 else 25 + 20*improve
        components.append({"name":"MACD","score":float(min(100,macd_s)),"weight":20,"available":True})
    if fib_s3 is not None:
        b=_distance_bonus(price,fib_s3,full_pct=5.0)
        if b is not None: components.append({"name":"Fib S3","score":float(b),"weight":20,"available":True})
    if bb_lower is not None:
        b=_distance_bonus(price,bb_lower,full_pct=5.0)
        if b is not None: components.append({"name":"BOLL下轨","score":float(b),"weight":20,"available":True})
    if not components: return None, {}
    total=sum(x["score"]*x["weight"] for x in components)/sum(x["weight"] for x in components)
    return round(max(0,min(100,total))), {
        "weights":{"RSI":20,"KDJ J":20,"MACD":20,"Fib S3":20,"BOLL下轨":20},
        "components":[{"name":x["name"],"score":round(x["score"],1),"weight":x["weight"]} for x in components],
        "formula":"技术价值分 = RSI×20% + KDJ J×20% + MACD×20% + Fib S3×20% + BOLL下轨×20%"
    }



def _technical_pullback_score(close, rsi_series, hist, hist_delta, mas, bb_lower, fib_s3, pivots):
    """Transparent 0-100 score for healthy pullbacks inside intact trends.

    The score is deliberately different from Technical Strength: it rewards
    intact trend + reasonable retracement + nearby support + momentum repair,
    while penalising overheated/high-chase conditions.
    """
    try:
        latest = finite(close.iloc[-1])
        if latest is None:
            return None, {}
        components = []

        # 1) Trend integrity: prefer a pullback while the medium-term structure
        # remains intact. This is not a prediction; it only describes current
        # price/MA structure.
        m20, m60, m120 = mas.get(20), mas.get(60), mas.get(120)
        trend_bits = []
        if m20 is not None: trend_bits.append(100.0 if latest >= m20 else 35.0)
        if m60 is not None: trend_bits.append(100.0 if latest >= m60 else 10.0)
        if m120 is not None: trend_bits.append(100.0 if latest >= m120 else 10.0)
        if m20 is not None and m60 is not None:
            trend_bits.append(100.0 if m20 >= m60 else 20.0)
        if m60 is not None and m120 is not None:
            trend_bits.append(100.0 if m60 >= m120 else 20.0)
        if trend_bits:
            components.append({"name":"趋势完整度","score":sum(trend_bits)/len(trend_bits),"weight":25})

        # 2) Healthy retracement depth from the recent 60-session high.
        if len(close) >= 20:
            high60 = finite(close.tail(60).max())
            dd = (latest / high60 - 1.0) * 100.0 if high60 else None
            if dd is not None:
                drop = max(0.0, -dd)
                if 4.0 <= drop <= 10.0:
                    retrace = 100.0
                elif drop < 4.0:
                    # No meaningful pullback should not receive a full bonus.
                    retrace = 20.0 + drop * 20.0
                elif drop <= 15.0:
                    retrace = 100.0 - (drop-10.0)*12.0
                else:
                    retrace = max(0.0, 40.0 - (drop-15.0)*8.0)
                components.append({"name":"回撤幅度","score":max(0.0,min(100.0,retrace)),"weight":20})

        # 3) Support confluence: closeness to several independent levels.
        support_scores=[]
        for name, level, full_pct in [
            ("MA20", m20, 5.0), ("MA60", m60, 7.0),
            ("BOLL下轨", bb_lower, 5.0), ("Fib S3", fib_s3, 5.0)
        ]:
            lv=finite(level)
            # A level above current price is resistance, not support. Do not
            # let proximity to a resistance inflate the pullback score.
            if lv is not None and lv <= latest:
                b=_distance_bonus(latest, lv, full_pct=full_pct)
                if b is not None: support_scores.append(b)
        pivot_levels_map=(pivots or {}).get("classic", {}) if isinstance(pivots, dict) else {}
        if pivot_levels_map:
            for key in ("S1","S2","S3"):
                lv=finite(pivot_levels_map.get(key))
                if lv is not None and lv <= latest:
                    b=_distance_bonus(latest, lv, full_pct=4.0)
                    if b is not None: support_scores.append(b)
        if support_scores:
            # Reward confluence rather than a single accidental nearby level.
            support_scores.sort(reverse=True)
            top=support_scores[:3]
            support = sum(top)/len(top)
            if len(support_scores) >= 2:
                support = min(100.0, support + 8.0)
            components.append({"name":"支撑共振","score":support,"weight":25})

        # 4) Momentum repair: improving MACD/RSI/KDJ is preferred to simply
        # being oversold. This is the key anti-value-trap component.
        repair=[]
        if hist_delta is not None:
            scale=max(abs(latest)*0.01,1e-9)
            repair.append(max(0.0,min(100.0,50.0 + 50.0*math.tanh(hist_delta/scale*8.0))))
        if rsi_series is not None and len(rsi_series.dropna()) >= 6:
            rsi_now=finite(rsi_series.iloc[-1]); rsi_prev=finite(rsi_series.iloc[-6])
            if rsi_now is not None and rsi_prev is not None:
                repair.append(max(0.0,min(100.0,50.0 + (rsi_now-rsi_prev)*5.0)))
        if repair:
            components.append({"name":"动能修复","score":sum(repair)/len(repair),"weight":20})

        # 5) Anti-chase / heat penalty. A stock near a 52-week high is not
        # automatically a bad stock; it simply should not receive a pullback
        # bonus unless price has actually returned to a reasonable zone.
        heat=[]
        if len(close) >= 60:
            h252=finite(close.tail(252).max())
            pos52=(latest-(finite(close.tail(252).min()) or latest))/(h252-(finite(close.tail(252).min()) or latest))*100 if h252 and h252 != (finite(close.tail(252).min()) or latest) else None
            if pos52 is not None:
                if pos52 >= 97: heat.append(0.0)
                elif pos52 >= 93: heat.append(25.0)
                elif pos52 >= 90: heat.append(55.0)
                else: heat.append(100.0)
        if m20:
            dist20=(latest/m20-1.0)*100.0
            if dist20 >= 10: heat.append(0.0)
            elif dist20 >= 6: heat.append(35.0)
            elif dist20 >= 3: heat.append(70.0)
            else: heat.append(100.0)
        if rsi_series is not None:
            r=finite(rsi_series.iloc[-1])
            if r is not None:
                heat.append(0.0 if r >= 80 else 25.0 if r >= 75 else 65.0 if r >= 70 else 100.0)
        if heat:
            components.append({"name":"反追高修正","score":sum(heat)/len(heat),"weight":10})

        if not components:
            return None, {}
        total=sum(x["score"]*x["weight"] for x in components)/sum(x["weight"] for x in components)
        # This scanner is explicitly a pullback finder, not a breakout-chaser.
        # If price has barely pulled back from the recent high, cap the
        # pullback score even when the trend itself is excellent.
        try:
            high60=finite(close.tail(60).max())
            drop60=(latest/high60-1.0)*100.0 if high60 else None
            if drop60 is not None and drop60 > -2.0:
                total=min(total,55.0)
        except Exception:
            pass
        meta={
            "weights": {x["name"]:x["weight"] for x in components},
            "components":[{"name":x["name"],"score":round(x["score"],1),"weight":x["weight"]} for x in components],
            "formula":"回踩质量分 = 趋势完整度×25% + 回撤幅度×20% + 支撑共振×25% + 动能修复×20% + 反追高修正×10%",
            "purpose":"优先识别强趋势中的合理回踩；高位追涨不会因52周位置高而获得额外回踩分。"
        }
        return round(max(0,min(100,total))), meta
    except Exception:
        return None, {}

def technical_analysis(history, fib=None, pivots=None):
    out = {"score": None, "state": "暂无数据", "value_score": None, "value_state":"暂无数据", "pullback_score": None, "pullback_state":"暂无数据", "score_breakdown":{}, "value_breakdown":{}, "pullback_breakdown":{}, "signals": [], "indicators": {}, "history": [], "indicator_errors": {}}
    if history is None or getattr(history, "empty", True) or "Close" not in history:
        return out
    try:
        close = history["Close"].dropna().astype(float)
        if len(close) < 30: return out
        volume = history["Volume"].dropna().astype(float) if "Volume" in history else None
        latest = float(close.iloc[-1])
        # Each indicator family is isolated: one bad series must not blank the
        # other indicators. Errors are exposed for diagnostics instead of swallowed.
        rsi_series = None; rsi = None
        try:
            delta = close.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi_series = 100 - 100/(1+rs)
            rsi = finite(rsi_series.iloc[-1])
        except Exception as exc:
            out["indicator_errors"]["RSI"] = str(exc)[:180]

        hist = None; hist_delta = None; macd_val = None; signal_val = None; hist_val = None
        try:
            ema12, ema26 = close.ewm(span=12, adjust=False).mean(), close.ewm(span=26, adjust=False).mean()
            macd, signal = ema12-ema26, (ema12-ema26).ewm(span=9, adjust=False).mean()
            hist = macd-signal
            macd_val, signal_val, hist_val = float(macd.iloc[-1]), float(signal.iloc[-1]), float(hist.iloc[-1])
            hist_prev=finite(hist.iloc[-2]) if len(hist)>1 else None
            hist_delta=hist_val-hist_prev if hist_prev is not None else None
        except Exception as exc:
            out["indicator_errors"]["MACD"] = str(exc)[:180]

        k_val=d_val=j_val=None
        try:
            # KDJ(9,3,3): RSV -> K/D -> J. J<0 is an explicit short-term
            # oversold bonus for the technical value score.
            hh=history["High"].astype(float).rolling(9).max() if "High" in history else close.rolling(9).max()
            ll=history["Low"].astype(float).rolling(9).min() if "Low" in history else close.rolling(9).min()
            denom=(hh-ll).replace(0,np.nan)
            rsv=(close-ll)/denom*100
            k=rsv.ewm(alpha=1/3, adjust=False).mean()
            d=k.ewm(alpha=1/3, adjust=False).mean()
            j=3*k-2*d
            k_val,d_val,j_val=finite(k.iloc[-1]),finite(d.iloc[-1]),finite(j.iloc[-1])
        except Exception as exc:
            out["indicator_errors"]["KDJ"] = str(exc)[:180]

        # MA cards: calculated for display/strength scoring, but deliberately
        # never plotted on the K-line.
        ma_periods=(20,60,120,250)
        mas={n:None for n in ma_periods}; ma_slopes={n:None for n in ma_periods}
        try:
            mas={n:finite(close.rolling(n).mean().iloc[-1]) for n in ma_periods}
            ma_slopes={n:(finite(close.rolling(n).mean().iloc[-1])-finite(close.rolling(n).mean().iloc[-6])) if len(close)>=n+5 else None for n in ma_periods}
        except Exception as exc:
            out["indicator_errors"]["MA"] = str(exc)[:180]

        bb_mid=bb_upper=bb_lower=bb_pos=bb_width=None
        try:
            mid, sd = close.rolling(20).mean(), close.rolling(20).std()
            upper, lower = mid+2*sd, mid-2*sd
            bb_mid = finite(mid.iloc[-1]); bb_upper = finite(upper.iloc[-1]); bb_lower = finite(lower.iloc[-1])
            bb_pos = safe_ratio(latest-bb_lower, bb_upper-bb_lower) if bb_upper is not None and bb_lower is not None else None
            bb_width = safe_ratio(bb_upper-bb_lower, bb_mid) * 100 if bb_mid not in (None, 0) and bb_upper is not None and bb_lower is not None else None
        except Exception as exc:
            out["indicator_errors"]["BOLL"] = str(exc)[:180]

        ret20 = None
        try:
            ret20 = (latest/float(close.iloc[-21])-1)*100 if len(close)>21 else None
        except Exception as exc:
            out["indicator_errors"]["20日动量"] = str(exc)[:180]
        vol_ratio = None
        try:
            if volume is not None and len(volume)>=20:
                av=float(volume.rolling(20).mean().iloc[-1]); vol_ratio=float(volume.iloc[-1]/av) if av else None
        except Exception as exc:
            out["indicator_errors"]["成交量"] = str(exc)[:180]
        high52=low52=pos52=None
        try:
            high52, low52 = float(close.tail(252).max()), float(close.tail(252).min()); pos52 = (latest-low52)/(high52-low52)*100 if high52 != low52 else None
        except Exception as exc:
            out["indicator_errors"]["52周位置"] = str(exc)[:180]

        # ---------------- Strong-strength score (100) ----------------
        # Fixed weights, each component normalized to 0-100.
        components=[]
        trend_parts=[]
        for n,w in ((20,8),(60,7),(120,5),(250,5)):
            m=mas[n]
            if m is not None: trend_parts.append((100 if latest>m else 0,w))
        if trend_parts: components.append((sum(v*w for v,w in trend_parts)/sum(w for _,w in trend_parts),25,"均线结构"))
        if hist_val is not None:
            scale=max(abs(latest)*0.01,1e-9); macd_s=50+50*math.tanh(hist_val/scale)
            components.append((macd_s,20,"MACD动能"))
        if rsi is not None:
            # Strength is strongest in 55-70; over 80 is penalized as overheated.
            if 55<=rsi<=70: rsi_s=100
            elif 50<=rsi<55: rsi_s=80+(rsi-50)*4
            elif 70<rsi<=80: rsi_s=100-(rsi-70)*5
            elif rsi>80: rsi_s=max(0,50-(rsi-80)*2.5)
            else: rsi_s=max(0,50-(50-rsi)*1.5)
            components.append((rsi_s,15,"RSI动能"))
        if ret20 is not None: components.append((_score_0_100(ret20,-15,30),15,"20日动量"))
        if pos52 is not None: components.append((pos52,15,"52周位置"))
        if vol_ratio is not None: components.append((max(0,min(100,50+(vol_ratio-1)*25)),10,"量能"))
        if components:
            score=round(sum(v*w for v,w,_ in components)/sum(w for _,w,_ in components))
            score=max(0,min(100,score))
        else: score=None
        state="强势" if score is not None and score>=75 else ("偏强" if score is not None and score>=60 else ("中性" if score is not None and score>=45 else ("偏弱" if score is not None and score>=30 else "弱势")))

        # ---------------- Technical value score (100) ----------------
        fib_s3=(pivots or {}).get("fibonacci",{}).get("S3") if pivots else None
        value_score,value_meta=_technical_value_score(rsi,j_val,hist_val,hist_delta,fib_s3,bb_lower,latest)
        value_state=("高性价比" if value_score is not None and value_score>=75 else ("较有性价比" if value_score is not None and value_score>=60 else ("中性" if value_score is not None and value_score>=45 else ("性价比较低" if value_score is not None else "暂无数据"))))

        # ---------------- Bullish-pullback score (100) ----------------
        pullback_score, pullback_meta = _technical_pullback_score(
            close, rsi_series, hist, hist_delta, mas, bb_lower, fib_s3,
            (pivots or {})
        )
        pullback_state=("高质量回踩" if pullback_score is not None and pullback_score>=75 else
                        "较好回踩" if pullback_score is not None and pullback_score>=60 else
                        "中性" if pullback_score is not None and pullback_score>=45 else
                        "回踩质量较低" if pullback_score is not None else "暂无数据")

        # ---------------- Composite score (100) ----------------
        # V2.5.8 Lite selection formula: value-for-money + momentum.
        # Pullback quality remains an independent diagnostic factor.
        composite_score = None
        composite_state = "暂无数据"
        if score is not None and value_score is not None:
            # Lite selection model: value-for-money + momentum. Pullback is
            # displayed separately and does not distort the core ranking.
            composite_score = round(value_score * 0.50 + score * 0.50)
            composite_score = max(0, min(100, composite_score))
            composite_state = (
                "强" if composite_score >= 75 else
                "偏强" if composite_score >= 60 else
                "中性" if composite_score >= 45 else
                "偏弱"
            )

        signals=[]
        if macd_val is not None and signal_val is not None:
            signals.append("MACD强于信号线" if macd_val>signal_val else "MACD弱于信号线")
        if j_val is not None and j_val<0: signals.append("KDJ J<0：短线超卖")
        if rsi is not None and rsi<30: signals.append("RSI<30：超卖")
        if fib_s3 is not None and _distance_bonus(latest,fib_s3,5) is not None and _distance_bonus(latest,fib_s3,5)>=80: signals.append("接近斐波纳契S3")
        if bb_lower is not None and _distance_bonus(latest,bb_lower,5) is not None and _distance_bonus(latest,bb_lower,5)>=80: signals.append("接近布林下轨")

        out.update({
            "score":score,"state":state,"value_score":value_score,"value_state":value_state,
            "pullback_score":pullback_score,"pullback_state":pullback_state,
            "composite_score":composite_score,"composite_state":composite_state,
            "score_breakdown":{"weights":{"均线结构":25,"MACD动能":20,"RSI动能":15,"20日动量":15,"52周位置":15,"量能":10},"components":[{"name":name,"score":round(v,1),"weight":w} for v,w,name in components],"formula":"技术强势分 = 均线结构×25% + MACD动能×20% + RSI动能×15% + 20日动量×15% + 52周位置×15% + 量能×10%"},
            "value_breakdown":value_meta,
            "pullback_breakdown":pullback_meta,
            "composite_breakdown":{"weights":{"高性价比分":50,"动能分":50},"formula":"综合评分 = 高性价比分×50% + 动能分×50%；回踩质量分独立展示；ROE门槛在排名前硬过滤"},
            "signals":signals,
            "indicator_errors":out.get("indicator_errors",{}),
            "indicators":{"rsi14":rsi,"macd":macd_val,"macd_signal":signal_val,"macd_hist":hist_val,"macd_hist_delta":hist_delta,"kdj_k":k_val,"kdj_d":d_val,"kdj_j":j_val,"bollinger_position":bb_pos,"bb_mid":bb_mid,"bb_upper":bb_upper,"bb_lower":bb_lower,"bb_width_pct":bb_width,"momentum_20d":ret20,"volume_ratio_20d":vol_ratio,"52w_high":high52,"52w_low":low52,"52w_position":pos52,"ma20":mas[20],"ma60":mas[60],"ma120":mas[120],"ma250":mas[250]},
            "history":[{"date":str(i.date()),"close":float(v)} for i,v in close.tail(180).items()]})
    except Exception as exc:
        out["error"] = str(exc)[:240]
    return out


_MORNINGSTAR_CACHE = {}
_MORNINGSTAR_CACHE_TTL = 6 * 60 * 60
_MORNINGSTAR_NEGATIVE_TTL = 30 * 60


def _morningstar_quote_url(symbol: str, exchange: str | None = None):
    """Build a public Morningstar stock quote URL without an extra lookup."""
    raw = clean_symbol(symbol)
    ex = str(exchange or '').upper()
    if raw.endswith('.HK'):
        mic = 'xhkg'; ticker = raw[:-3].zfill(5)
    elif raw.endswith('.SS'):
        mic = 'xshg'; ticker = raw[:-3]
    elif raw.endswith('.SZ'):
        mic = 'xshe'; ticker = raw[:-3]
    else:
        # Yahoo exchange values: NMS/NAS/NASDAQ -> XNAS; NYQ/NYSE -> XNYS.
        mic = 'xnys' if ex in {'NYQ','NYSE','XNYS'} else 'xnas'
        ticker = raw.replace('-', '.')
    return f'https://www.morningstar.com/stocks/{mic}/{ticker.upper()}/quote.html'


def _parse_morningstar_rating(html: str):
    """Best-effort parse of an actually exposed Morningstar stock star rating.

    The public quote page is dynamic. Try visible text first, then JSON/script
    payloads used by the page. We only accept explicit Morningstar-specific
    fields (never a generic analyst rating) and never infer from valuation.
    """
    raw=str(html or '')
    text = re.sub(r'\s+', ' ', raw)
    patterns = [
        r'Morningstar\s+Rating(?:\s+for\s+Stocks)?[^0-9]{0,120}([1-5])\s*(?:-?star|stars?)',
        r'Morningstar\s+Rating(?:\s+for\s+Stocks)?[^0-9]{0,80}(★{1,5})',
        r'"(?:morningstarRating|morningStarOverallRating|starRating)"\s*:\s*([1-5])(?:\.0)?(?:,|})',
        r'"(?:morningstarRating|morningStarOverallRating|starRating)"\s*:\s*"([1-5])"',
    ]
    for pat in patterns:
        m=re.search(pat, text, flags=re.I)
        if not m: continue
        value=m.group(1)
        if value.startswith('★'): return len(value)
        try:
            n=int(float(value))
            if 1 <= n <= 5: return n
        except Exception: pass
    # Search script payloads for explicit Morningstar keys. This handles Next.js
    # / hydration JSON where the visible HTML is only a shell.
    for keypat in ['morningstarRating','morningStarOverallRating','starRating']:
        for m in re.finditer(r'"'+re.escape(keypat)+r'"\s*:\s*(?:\{\s*"raw"\s*:\s*)?(\d+(?:\.0)?)', raw, flags=re.I):
            try:
                n=int(float(m.group(1)))
                if 1 <= n <= 5: return n
            except Exception: pass
    return None


def _parse_morningstar_uncertainty(html: str):
    text = re.sub(r'\s+', ' ', str(html or ''))
    for pat in [
        r'Morningstar\s+Uncertainty\s+Rating[^A-Za-z]{0,80}(Very\s+High|High|Medium|Low)',
        r'"uncertainty(?:Rating)?"\s*:\s*"(Very\s+High|High|Medium|Low)"',
    ]:
        m=re.search(pat,text,flags=re.I)
        if m:
            return m.group(1).title()
    return None


def _extract_nested_field(obj, names):
    wanted={str(x).lower() for x in names}
    if isinstance(obj, dict):
        for k,v in obj.items():
            if str(k).lower() in wanted:
                if isinstance(v,dict) and 'raw' in v: return v.get('raw')
                return v
            found=_extract_nested_field(v,names)
            if found is not None:return found
    elif isinstance(obj,list):
        for v in obj:
            found=_extract_nested_field(v,names)
            if found is not None:return found
    return None


def _yahoo_morningstar_fields(symbol: str):
    """Fast optional Yahoo quoteSummary fallback for Morningstar fields."""
    try:
        url=f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{requests.utils.quote(clean_symbol(symbol))}'
        r=requests.get(url,params={'modules':'defaultKeyStatistics,summaryDetail'},headers={'User-Agent':'Mozilla/5.0 (AEL; Morningstar fallback)'},timeout=2.0)
        r.raise_for_status(); data=r.json() or {}
        overall=_extract_nested_field(data,['morningStarOverallRating','morningstarOverallRating'])
        risk=_extract_nested_field(data,['morningStarRiskRating','morningstarRiskRating'])
        return finite(overall),finite(risk)
    except Exception:
        return None,None

def _morningstar_search_rating(symbol: str, exchange: str | None = None):
    """Fast public Morningstar search endpoint fallback.

    This endpoint is used only to retrieve an explicitly exposed starRating.
    No rating is reconstructed from price/fair value or other AEL metrics.
    """
    try:
        fields='ticker,name,starRating,StarRatingM255,uncertaintyRating,exchangeCode'
        r=requests.get(
            'https://www.morningstar.com/api/v2/search',
            params={'q':clean_symbol(symbol),'fields':fields,'limit':10},
            headers={'User-Agent':'Mozilla/5.0 (AEL Lite Morningstar)','Accept':'application/json, text/plain, */*','Referer':'https://www.morningstar.com/'},
            timeout=2.5,
        )
        if r.status_code in (202,403,429):
            return None,None,'waf_or_rate_limited'
        r.raise_for_status()
        payload=r.json() or {}
        results=payload.get('results') or []
        target=str(clean_symbol(symbol)).upper().replace('-','.')
        for item in results:
            meta=item.get('meta') or {}
            fields_obj=item.get('fields') or {}
            ticker=str(meta.get('ticker') or fields_obj.get('ticker',{}).get('value') or '').upper().replace('-','.')
            if ticker and ticker != target: continue
            def fv(name):
                v=fields_obj.get(name)
                if isinstance(v,dict): v=v.get('value')
                try:
                    return int(float(v)) if v is not None and str(v) != '' else None
                except Exception:
                    return None
            rating=fv('starRating')
            if rating is None: rating=fv('StarRatingM255')
            if rating is not None and 1 <= rating <= 5:
                return rating,None,'morningstar_search_api'
        return None,None,'not_exposed'
    except Exception:
        return None,None,'source_error'


def morningstar_stock_rating(symbol: str, exchange: str | None = None):
    """Lazy, cached public-page lookup for Morningstar's stock star rating.

    This is intentionally separate from build_dashboard(): a slow/blocked
    Morningstar page must never make Lite core/detail data become unavailable.
    """
    key=(clean_symbol(symbol), str(exchange or '').upper())
    now=time.time() if 'time' in globals() else pd.Timestamp.utcnow().timestamp()
    cached=_MORNINGSTAR_CACHE.get(key)
    if cached and now-cached.get('ts',0) < cached.get('ttl', _MORNINGSTAR_CACHE_TTL):
        return cached['data']
    url=_morningstar_quote_url(symbol, exchange)
    data={'symbol':clean_symbol(symbol),'rating':None,'uncertainty':None,'available':False,'status':'not_exposed',
          'as_of':None,'url':url,'source':'Morningstar公开股票报价页',
          'note':'仅在公开页面实际暴露1–5星时显示；未暴露时不推断、不补值。'}
    # First: Morningstar's own public search endpoint. It can expose starRating
    # even when the quote page is rendered dynamically.
    ms_rating, ms_risk, ms_status = _morningstar_search_rating(symbol, exchange)
    if ms_rating is not None:
        data.update({'rating':ms_rating,'uncertainty':ms_risk,'available':True,'status':ms_status,'source':'Morningstar公开搜索接口'})
        _MORNINGSTAR_CACHE[key]={'ts':now,'ttl':_MORNINGSTAR_CACHE_TTL,'data':data}
        return data
    # Second: fast Yahoo quoteSummary fallback. It is independent of the Lite core.
    yo,yr=_yahoo_morningstar_fields(symbol)
    if yo is not None or yr is not None:
        data.update({'rating':yo,'uncertainty':yr,'available':yo is not None,'status':'yahoo_morningstar_fields','source':'Yahoo Finance Morningstar字段'})
    if data['available']:
        _MORNINGSTAR_CACHE[key]={'ts':now,'ttl':_MORNINGSTAR_CACHE_TTL,'data':data}
        return data
    try:
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0 (AEL; stock research)'},timeout=2.5)
        r.raise_for_status()
        html=r.text
        rating=_parse_morningstar_rating(html)
        uncertainty=_parse_morningstar_uncertainty(html)
        if rating is not None or uncertainty is not None:
            data.update({'rating':rating,'uncertainty':uncertainty,'available':rating is not None,'status':'available' if rating is not None else 'uncertainty_only','as_of':None})
        else:
            data['status']='page_reachable_rating_not_exposed'
    except Exception as exc:
        data['status']='source_unavailable'
        data['note']=f'公开页面暂不可访问：{str(exc)[:160]}'
    _MORNINGSTAR_CACHE[key]={'ts':now,'ttl':(_MORNINGSTAR_CACHE_TTL if data.get('available') else _MORNINGSTAR_NEGATIVE_TTL),'data':data}
    return data

def analyst_view(ticker, info=None):
    """Analyst consensus plus optional Morningstar fields exposed by Yahoo.

    Morningstar data is not guaranteed for individual equities. Never infer or
    substitute another rating; when the source does not provide it, return None.
    """
    out={"available":False,"rating":{},"targets":{},"earnings":{},"revenue":{},"changes":[],
         "morningstar":{"overall":None,"risk":None,"available":False,"source":"Yahoo Finance quoteSummary / Morningstar字段"}}
    info = info or {}
    try:
        overall=finite(info.get("morningStarOverallRating")); risk=finite(info.get("morningStarRiskRating"))
        if overall is not None or risk is not None:
            out["morningstar"]={"overall":overall,"risk":risk,"available":True,"source":"Yahoo Finance / Morningstar"}
    except Exception: pass
    try:
        rec=ticker.get_recommendations()
        if rec is not None and not rec.empty:
            row=rec.iloc[-1]; out["rating"]={str(k):finite(v) for k,v in row.to_dict().items() if finite(v) is not None}; out["available"]=True
    except Exception: pass
    try:
        x=ticker.get_analyst_price_targets() or {}; out["targets"]={k:finite(v) for k,v in x.items() if finite(v) is not None}; out["available"]|=bool(out["targets"])
    except Exception: pass
    for attr,key,index in [("get_earnings_estimate","earnings","0y"),("get_revenue_estimate","revenue","0y")]:
        try:
            df=getattr(ticker,attr)()
            if df is not None and not df.empty:
                row=df.loc[index] if index in df.index else df.iloc[0]; out[key]={str(k):finite(v) for k,v in row.to_dict().items() if finite(v) is not None}; out["available"]=True
        except Exception: pass
    try:
        df=ticker.get_upgrades_downgrades()
        if df is not None and not df.empty:
            out["changes"]=df.tail(8).reset_index().to_dict("records"); out["available"]=True
    except Exception: pass
    return out


def sec_cik_for_ticker(ticker):
    global _SEC_TICKERS
    if _SEC_TICKERS is None:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_UA}, timeout=12)
        r.raise_for_status()
        data = r.json()
        _SEC_TICKERS = {str(v["ticker"]).upper(): str(v["cik_str"]).zfill(10) for v in data.values() if v.get("ticker")}
    return _SEC_TICKERS.get(ticker.upper())


def _sec_fact(facts, tags):
    for taxonomy, tag in tags:
        unit = facts.get(taxonomy, {}).get(tag)
        if not unit:
            continue
        # Prefer USD facts; then first available unit.
        if "USD" in unit: return unit["USD"]
        first = next(iter(unit.values()), None)
        if first: return first
    return []


def sec_annual_roe(symbol, errors):
    """Build up to 15 annual ROEs from SEC Company Facts.

    SEC duration facts (net income) and instant facts (equity) are matched by
    fiscal-period end dates, not merely by the SEC ``fy`` field. This avoids
    the common Apple-style fiscal-year/context mismatch where an equity fact
    from a prior balance sheet is accidentally paired with the current year.
    """
    if not is_us_symbol(symbol):
        return None
    try:
        cik = sec_cik_for_ticker(symbol)
        if not cik:
            errors["sec"] = "ticker not found in SEC company_tickers"
            return None

        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        facts = data.get("facts", {})

        def pick_units(taxonomy, tag_candidates):
            for tag in tag_candidates:
                unit_map = facts.get(taxonomy, {}).get(tag, {}).get("units", {})
                if not unit_map:
                    continue
                if "USD" in unit_map:
                    return unit_map["USD"]
                first = next(iter(unit_map.values()), None)
                if first:
                    return first
            return []

        # Prefer consolidated net income and total shareholders' equity.
        ni_items = pick_units("us-gaap", [
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "NetIncomeLossAvailableToCommonStockholdersDiluted",
        ])
        eq_items = pick_units("us-gaap", [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "CommonStockholdersEquity",
            "PartnersCapital",
        ])
        if not ni_items or not eq_items:
            errors["sec"] = "SEC facts missing usable net income or equity tags"
            return []

        forms = {"10-K", "20-F", "40-F"}

        def annual_flows(items):
            """Return one duration fact per fiscal year, preserving period dates."""
            candidates = []
            for x in items:
                val = finite(x.get("val"))
                start, end = x.get("start"), x.get("end")
                form, fy = x.get("form"), x.get("fy")
                if val is None or not start or not end or form not in forms:
                    continue
                try:
                    days = (pd.Timestamp(end) - pd.Timestamp(start)).days
                except Exception:
                    continue
                # Annual 10-K duration; allow 52/53-week fiscal years.
                if not 250 <= days <= 400:
                    continue
                try:
                    year = int(fy) if fy else pd.Timestamp(end).year
                except Exception:
                    year = pd.Timestamp(end).year
                score = (
                    1 if x.get("fp") == "FY" else 0,
                    1 if form == "10-K" else 0,
                    str(x.get("filed", "")),
                )
                candidates.append((year, str(end), score, x))

            by_year = {}
            for year, end, score, x in candidates:
                old = by_year.get(year)
                # Prefer the filing with the strongest annual designation and
                # latest filing date, while retaining its exact fiscal end date.
                if old is None or score > old[0]:
                    by_year[year] = (score, end, x)
            return {y: item for y, (_, _, item) in by_year.items()}

        def instant_equity(items):
            """Return the best equity fact for each exact balance-sheet end date."""
            by_end = {}
            for x in items:
                val = finite(x.get("val"))
                end = x.get("end")
                form = x.get("form")
                if val is None or not end or form not in forms:
                    continue
                try:
                    end_date = str(pd.Timestamp(end).date())
                except Exception:
                    continue
                # Instant facts must not require a start date.
                score = (
                    1 if x.get("fp") == "FY" else 0,
                    1 if form == "10-K" else 0,
                    str(x.get("filed", "")),
                )
                old = by_end.get(end_date)
                if old is None or score > old[0]:
                    by_end[end_date] = (score, x)
            return {end: item for end, (_, item) in by_end.items()}

        ni_by_year = annual_flows(ni_items)
        eq_by_end = instant_equity(eq_items)
        eq_dates = sorted(eq_by_end.keys())

        def equity_on_or_before(target_date, max_days=450):
            """Find the latest balance-sheet equity at/before target date."""
            if not target_date:
                return None
            try:
                target = pd.Timestamp(target_date)
            except Exception:
                return None
            eligible = [d for d in eq_dates if pd.Timestamp(d) <= target]
            if not eligible:
                return None
            chosen = eligible[-1]
            if (target - pd.Timestamp(chosen)).days > max_days:
                return None
            return eq_by_end[chosen]

        rows = []
        for year in sorted(ni_by_year):
            ni = ni_by_year[year]
            n = finite(ni.get("val"))
            end = ni.get("end")
            start = ni.get("start")
            if n is None or not start or not end:
                continue

            # Exact fiscal-period matching first. Beginning equity is the
            # balance immediately preceding the fiscal-period start.
            cur = eq_by_end.get(str(pd.Timestamp(end).date()))
            prev = equity_on_or_before(start)
            if cur is None or prev is None:
                continue

            eb = finite(prev.get("val"))
            ee = finite(cur.get("val"))
            if eb is None or ee is None:
                continue
            avg_equity = (eb + ee) / 2
            if avg_equity == 0:
                continue

            rows.append({
                "year": int(year),
                "fiscal_year": f"FY{int(year)}",
                "fiscal_period_end": str(pd.Timestamp(end).date()),
                "net_income": n,
                "equity_begin": eb,
                "equity_begin_date": str(pd.Timestamp(prev.get("end")).date()) if prev.get("end") else None,
                "equity_end": ee,
                "equity_end_date": str(pd.Timestamp(cur.get("end")).date()) if cur.get("end") else str(pd.Timestamp(end).date()),
                "roe": n / avg_equity * 100,
            })

        rows.sort(key=lambda x: x["year"])
        if not rows:
            errors["sec"] = "SEC facts found, but no matching annual income/equity pairs"
            return []
        return rows[-15:]
    except Exception as exc:
        errors["sec"] = str(exc)[:240]
        return None


_TRANSLATE_CACHE = {}

_PUBLISHER_ZH = {
    "Reuters": "路透社",
    "Reuters Breakingviews": "路透 Breakingviews",
    "Yahoo Finance": "雅虎财经",
    "Bloomberg": "彭博",
    "CNBC": "CNBC",
    "The Wall Street Journal": "华尔街日报",
    "Financial Times": "金融时报",
    "MarketWatch": "MarketWatch",
    "Benzinga": "Benzinga",
    "Seeking Alpha": "Seeking Alpha",
    "Barron's": "巴伦周刊",
    "The Motley Fool": "Motley Fool",
    "Associated Press": "美联社",
    "AP News": "美联社",
}

def _split_translate_chunks(text, max_chars=450):
    chunks=[]; remaining=str(text)[:max_chars*20]
    while remaining:
        cut=min(max_chars,len(remaining))
        if cut < len(remaining):
            candidates=[remaining.rfind('. ',0,cut),remaining.rfind('; ',0,cut),remaining.rfind(', ',0,cut),remaining.rfind('。',0,cut)]
            best=max(candidates)
            if best>120: cut=best+1
        chunks.append(remaining[:cut]); remaining=remaining[cut:]
    return chunks

def _translate_google(chunk):
    r=requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client":"gtx","sl":"auto","tl":"zh-CN","dt":"t","q":chunk},
        headers={"User-Agent":"Mozilla/5.0"}, timeout=8
    )
    r.raise_for_status()
    data=r.json()
    return "".join(part[0] for part in (data[0] or []) if part and part[0]).strip()

def _translate_mymemory(chunk):
    # Free fallback; no API key required for short public requests.
    r=requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q":chunk,"langpair":"en|zh-CN"},
        headers={"User-Agent":"AEL-Stock-Dashboard/2.4.7"}, timeout=10
    )
    r.raise_for_status()
    data=r.json() or {}
    translated=((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not translated: raise RuntimeError("MyMemory returned empty translation")
    return translated

def translate_to_chinese(text, errors, max_chars=5000):
    """Translate source text to Simplified Chinese with two free fallbacks.
    The dashboard never intentionally displays an English fallback as the Chinese field.
    """
    if not text:
        return None
    text=str(text).strip()
    if not text:
        return None
    # Already Chinese: keep it.
    if re.search(r"[\u4e00-\u9fff]", text) and not re.search(r"[A-Za-z]{4,}", text):
        return text
    key=text[:max_chars]
    if key in _TRANSLATE_CACHE: return _TRANSLATE_CACHE[key]
    chunks=_split_translate_chunks(text,min(max_chars,450))
    providers=[("google",_translate_google),("mymemory",_translate_mymemory)]
    last_error=None
    for name,fn in providers:
        try:
            out=[]
            for chunk in chunks:
                tr=fn(chunk)
                if not tr: raise RuntimeError("empty translation")
                out.append(tr)
            result="".join(out).strip()
            if result and "QUERY LENGTH LIMIT EXCEEDED" not in result.upper() and "MAX ALLOWED QUERY" not in result.upper():
                _TRANSLATE_CACHE[key]=result
                return result
            raise RuntimeError("translation provider returned a query-length error")
        except Exception as exc:
            last_error=f"{name}: {exc}"
            continue
    if last_error:
        errors.setdefault("translation", last_error[:240])
    # Do not surface the English source in a field explicitly labelled Chinese.
    return None


def publisher_to_chinese(publisher):
    p=(publisher or "").strip()
    if not p: return "新闻来源"
    if p in _PUBLISHER_ZH: return _PUBLISHER_ZH[p]
    for en,zh in _PUBLISHER_ZH.items():
        if en.lower() in p.lower(): return zh
    # Keep acronyms / short brand names as-is; otherwise label it as source name.
    return p if len(p) <= 24 else "新闻媒体"

def _news_sentiment(title: str) -> str:
    """Title-only heuristic. Labels are indicative, not investment recommendations."""
    t=(title or "").lower()
    positive=["beat","beats","strong","growth","record","surge","jump","rises","rise","gain","upgrade","launch","wins","approved","agreement","deal","expands","sales up","profit up","revenue up","超预期","增长","上涨","创纪录","推出","获批","协议","合作","扩张"]
    negative=["miss","misses","weak","decline","drop","falls","fall","cuts","downgrade","lawsuit","fine","penalty","delay","recall","shortage","warning","slump","risk","disappoint","disappoints","profit down","revenue down","下滑","下降","诉讼","罚款","延迟","召回","短缺","风险","不及预期","警告"]
    ps=sum(1 for w in positive if w in t)
    ns=sum(1 for w in negative if w in t)
    if ps>ns and ps>0: return "利好"
    if ns>ps and ns>0: return "利空"
    return "中性"


def company_news(symbol, errors, limit=8):
    """Fetch current company news from Yahoo Finance search, preserving original article links."""
    items=[]
    try:
        r=requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q":symbol,"quotesCount":0,"newsCount":limit},
            headers={"User-Agent":"Mozilla/5.0"}, timeout=12
        )
        r.raise_for_status()
        data=r.json() or {}
        for x in data.get("news") or []:
            title=(x.get("title") or "").strip()
            link=(x.get("link") or x.get("canonicalUrl",{}).get("url") or "").strip()
            if not title or not link: continue
            ts=x.get("providerPublishTime")
            dt=None
            if ts:
                try: dt=pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).strftime("%Y-%m-%d %H:%M")
                except Exception: dt=None
            items.append({
                "title": title,
                "title_zh": translate_to_chinese(title, errors, max_chars=800) or "（新闻标题中文翻译暂不可用）",
                "publisher": publisher_to_chinese(x.get("publisher") or ""),
                "link": link,
                "published_at": dt,
                "sentiment": _news_sentiment(title),
            })
    except Exception as exc:
        errors["news"] = str(exc)[:240]
    # de-duplicate by URL/title
    seen=set(); out=[]
    for x in items:
        k=x["link"] or x["title"]
        if k in seen: continue
        seen.add(k); out.append(x)
    return out[:limit]

def build_dashboard(raw_symbol: str, include_slow: bool = True) -> dict[str, Any]:
    symbol=clean_symbol(raw_symbol); t=yf.Ticker(symbol); errors={}
    history=get_history(t,symbol,errors)
    price=None
    if history is not None and not history.empty:
        try: price=finite(history["Close"].dropna().iloc[-1])
        except Exception: pass
    if price is None:
        fi=_safe_get(t,"fast_info",errors,{}) or {}
        try: price=finite(fi.get("last_price"))
        except Exception: pass

    inc=_safe_get(t,"financials",errors,None); bs=_safe_get(t,"balance_sheet",errors,None); cf=_safe_get(t,"cashflow",errors,None)
    info=_safe_get(t,"info",errors,{}) or {}
    revenue=latest_row(inc,["Total Revenue","Operating Revenue"]); net_income=latest_row(inc,["Net Income","Net Income Common Stockholders"])
    gross_profit=latest_row(inc,["Gross Profit"]); gross_margin=(gross_profit/revenue*100 if gross_profit is not None and revenue not in (None,0) else pct(info.get("grossMargins")))
    assets=latest_row(bs,["Total Assets"]); liabilities=latest_row(bs,["Total Liabilities Net Minority Interest","Total Liabilities"]); equity=latest_row(bs,["Stockholders Equity","Common Stock Equity","Stockholders Equity Including Minority Interest"])
    debt_ratio=safe_ratio(liabilities,assets); debt_ratio=debt_ratio*100 if debt_ratio is not None else None
    roe_info=finite(info.get("returnOnEquity")); current_roe=roe_info*100 if roe_info is not None else (net_income/equity*100 if net_income is not None and equity not in (None,0) else None)
    ocf=latest_row(cf,["Operating Cash Flow","Total Cash From Operating Activities"]); capex=latest_row(cf,["Capital Expenditure","Capital Expenditures"]); fcf=ocf+capex if ocf is not None and capex is not None else None
    shares=latest_row(bs,["Ordinary Shares Number","Share Issued","Common Stock Shares Outstanding"])
    pe=finite(info.get("trailingPE")); pb=finite(info.get("priceToBook"))
    if pe is None and price is not None and net_income is not None and shares not in (None,0): pe=safe_ratio(price,net_income/shares)
    if pb is None and price is not None and equity is not None and shares not in (None,0): pb=safe_ratio(price,equity/shares)

    dividend_error=None
    divs=None
    # yfinance supports get_dividends(period="max"), but some Yahoo responses
    # return timezone-aware indices while others return naive indices. Normalize
    # here so TTM filtering never silently falls back to 0.
    try:
        divs=t.get_dividends(period="max")
    except Exception as exc:
        dividend_error=str(exc)[:240]
        errors["dividends"] = dividend_error
        try:
            divs=getattr(t, "dividends", None)
            if divs is not None and not getattr(divs, "empty", True):
                dividend_error=None
                errors.pop("dividends", None)
        except Exception as exc2:
            errors["dividends_fallback"] = str(exc2)[:240]
    dividends=dividend_metrics(divs,cf,price,fcf,net_income,dividend_error)
    fib=fibonacci_levels(history); pivots=pivot_levels(history); tech=technical_analysis(history, fib, pivots); tech["fibonacci"]=fib; tech["pivots"]=pivots; tech["resonance"]=resonance_levels(price, pivots, tech.get("indicators", {})); tech["price_chart"]=technical_price_chart(history,fib); analysts=analyst_view(t, info)

    # US: SEC/EDGAR is authoritative for long-history ROE; fallback to Yahoo only if SEC unavailable.
    sec_roe=sec_annual_roe(symbol,errors) if is_us_symbol(symbol) else None
    if sec_roe is not None and len(sec_roe) >= 2:
        roe15=sec_roe; roe_source="SEC EDGAR / XBRL Company Facts"
    else:
        roe15=annual_roe(inc,bs); roe_source="Yahoo Finance via yfinance"

    # Headline ROE must correspond to the same latest complete fiscal year as
    # revenue/net income shown above. Do not blindly use the last SEC row: a
    # newly listed/spun-off company can have SEC historical contexts that do
    # not line up with Yahoo's latest financial statement columns.
    def latest_fiscal_roe_from_statements(inc_df, bs_df):
        if inc_df is None or getattr(inc_df, "empty", True) or bs_df is None or getattr(bs_df, "empty", True):
            return None
        try:
            inc_cols = list(inc_df.columns)
            bs_cols = list(bs_df.columns)
            for col in inc_cols:
                year = getattr(col, "year", None)
                if year is None:
                    continue
                ni = series_value(inc_df, ["Net Income", "Net Income Common Stockholders"], col)
                if ni is None:
                    continue
                same = [c for c in bs_cols if getattr(c, "year", None) == year]
                prev = [c for c in bs_cols if getattr(c, "year", None) == year - 1]
                if not same or not prev:
                    continue
                ee = series_value(bs_df, ["Stockholders Equity", "Common Stock Equity", "Stockholders Equity Including Minority Interest"], same[0])
                eb = series_value(bs_df, ["Stockholders Equity", "Common Stock Equity", "Stockholders Equity Including Minority Interest"], prev[0])
                if eb is None or ee is None or (eb + ee) == 0:
                    continue
                return {"year": int(year), "net_income": ni, "equity_begin": eb, "equity_end": ee, "roe": ni / ((eb + ee) / 2) * 100}
        except Exception:
            return None
        return None

    latest_statement_roe = latest_fiscal_roe_from_statements(inc, bs)
    if latest_statement_roe is not None:
        current_roe = finite(latest_statement_roe.get("roe"))
        # Keep the long-history table synchronized with the headline figure.
        # If the latest financial-statement fiscal year is newer than (or
        # conflicts with) the last SEC/Yahoo row, replace that year's row.
        if roe15:
            target_year = latest_statement_roe["year"]
            roe15 = [r for r in roe15 if int(r.get("year", -1)) != target_year]
            roe15.append(latest_statement_roe)
            roe15 = sorted(roe15, key=lambda r: int(r.get("year", 0)))[-15:]
        else:
            roe15 = [latest_statement_roe]

    company=info.get("longName") or info.get("shortName") or symbol
    exchange=info.get("exchange") or ""; currency=info.get("currency") or ""
    # 非核心慢数据（公司简介翻译、新闻、分析师数据）与核心行情解耦。
    # include_slow=False 时，核心接口不会等待这些外部请求。
    if include_slow:
        company_description_en=info.get("longBusinessSummary") or None
        company_description_zh=translate_to_chinese(company_description_en, errors, max_chars=9000) if company_description_en else None
        news=company_news(symbol, errors, limit=8)
        slow_analysts=analysts
    else:
        company_description_en=None
        company_description_zh=None
        news=[]
        slow_analysts={"available": False, "rating": {}, "targets": {}, "earnings": {}, "revenue": {}, "note": "核心数据已先返回；分析师数据异步加载。"}
    source_status={"history":bool(history is not None and not getattr(history,"empty",True)),"financials":bool(inc is not None and not getattr(inc,"empty",True)),"balance_sheet":bool(bs is not None and not getattr(bs,"empty",True)),"cashflow":bool(cf is not None and not getattr(cf,"empty",True)),"dividends": dividends["status"],"analysts":slow_analysts["available"],"roe_long_history": len(roe15) >= 10,"news":bool(news)}
    logo_url = info.get("logo_url") or info.get("logoUrl") or info.get("companyLogoUrl") or None
    return {"query":raw_symbol,"symbol":symbol,"company":company,"company_description":company_description_zh,"company_description_en":company_description_en,"exchange":exchange,"currency":currency,"market":info.get("market"),"logo_url":logo_url,"logo_domain":(re.sub(r"^https?://(?:www\.)?([^/]+).*$", r"\1", str(info.get("website"))).lower() if info.get("website") else None),"market_data":{"price":price,"market_cap":finite(info.get("marketCap"))},
            "valuation":{"pe":pe,"pb":pb,"roe":current_roe,"roe_pb":safe_ratio(current_roe,pb),"pe_roe":safe_ratio(pe,current_roe)},
            "fundamentals":{"revenue":revenue,"net_income":net_income,"gross_margin":gross_margin,"free_cash_flow":fcf,"debt_ratio":debt_ratio,"debt_to_equity":finite(info.get("debtToEquity"))},
            "dividends":dividends,"roe_15y":{"years":roe15,"stats":stats([r["roe"] for r in roe15]),"definition":"ROE = 年度净利润 / ((期初股东权益 + 期末股东权益) / 2)","source":roe_source},
            "technical":tech,"analysts":slow_analysts,"news":news,"source":{"provider":"免费数据源：Yahoo Finance/yfinance + SEC EDGAR（美股长期ROE）+ Yahoo Finance News","status":source_status,"errors":errors,"note":"行情、财报、分红、技术面、分析师、公司简介、新闻及长期ROE独立取数；单模块失败不会让整个页面失效。新闻的利好/利空标签仅按标题关键词自动归类，不构成投资建议。"}}
