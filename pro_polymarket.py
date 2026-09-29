"""Polymarket earnings sidecar for AEL.

This module is deliberately isolated from every existing AEL calculation. It only
reads public Polymarket market metadata/prices and returns an optional earnings
signal. Missing/insufficient markets are represented as unavailable; no values are
fabricated.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import time
from typing import Any

import requests

BASE = "https://gamma-api.polymarket.com"
TTL = 120
_TIMEOUT = 3.5
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _parse_jsonish(v):
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return None


def _flatten_markets(obj, context=None):
    """Flatten Gamma search responses while preserving parent event text.

    Polymarket search returns events containing child markets. The child market
    question is often a template such as ``above __``; the actual threshold is
    carried by fields such as groupItemTitle/groupItemThreshold.
    """
    context = context or {}
    out = []
    if isinstance(obj, dict):
        local = dict(context)
        for k in ("title", "subtitle", "description", "ticker", "category", "subcategory"):
            if obj.get(k) and not local.get(f"event_{k}"):
                local[f"event_{k}"] = obj.get(k)
        if any(k in obj for k in ("question", "conditionId", "clobTokenIds")):
            item = dict(obj)
            for k, v in local.items():
                item.setdefault(k, v)
            out.append(item)
        for key in ("markets", "events", "data", "results"):
            if key in obj:
                out.extend(_flatten_markets(obj[key], local))
    elif isinstance(obj, list):
        for x in obj:
            out.extend(_flatten_markets(x, context))
    return out

def _get(url, params):
    r = requests.get(url, params=params, timeout=_TIMEOUT, headers={"User-Agent": "AEL/2.6.36"})
    r.raise_for_status()
    return r.json()


def _market_question(m):
    return str(m.get("question") or m.get("title") or "").strip()


def _active(m):
    if m.get("closed") is True or m.get("archived") is True:
        return False
    return m.get("active", True) is not False


def _yes_probability(m):
    outcomes = _parse_jsonish(m.get("outcomes")) or m.get("outcomes")
    prices = _parse_jsonish(m.get("outcomePrices")) or m.get("outcomePrices")
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    for i, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() in {"yes", "是"} and i < len(prices):
            p = _num(prices[i])
            if p is not None and 0 <= p <= 1:
                return p
    return None


def _parse_threshold_value(v, metric):
    if v is None or v == "":
        return None
    text = str(v).strip().replace(",", "")
    # Keep the raw numeric field useful even when Gamma gives a bare number.
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    val = float(m.group(0))
    low = text.lower()
    if metric == "revenue":
        if re.search(r"\b(b|bn|billion)\b", low):
            return val * 1e9
        if re.search(r"\b(m|mm|million)\b", low):
            return val * 1e6
        # Gamma's groupItemThreshold for revenue can already be absolute USD.
        if abs(val) >= 1e6:
            return val
        return val * 1e9 if val < 1e5 else val
    if metric == "eps":
        if re.search(r"\b(b|bn|billion|m|mm|million)\b", low):
            return None
        return val
    return None

def _threshold(question: str, metric: str, market=None):
    market = market or {}
    # Multi-outcome earnings events commonly store the strike here instead of
    # putting it in the question (the question literally says "above __").
    for key in ("groupItemThreshold", "groupItemTitle", "xAxisValue", "lowerBound", "upperBound"):
        parsed = _parse_threshold_value(market.get(key), metric)
        if parsed is not None:
            return parsed
    q = question.lower().replace(",", "")
    patterns = [
        r"(?:above|over|greater than|at least|exceed|>)\s*\$?\s*(\d+(?:\.\d+)?)\s*(b|bn|billion|m|mm|million)?",
        r"\$\s*(\d+(?:\.\d+)?)\s*(b|bn|billion|m|mm|million)?",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if not m:
            continue
        return _parse_threshold_value((m.group(1) + (m.group(2) or "")), metric)
    return None

def _classify(question: str):
    q = question.lower()
    if re.search(r"\beps\b|earnings per share|\bprofit per share\b|non[- ]gaap eps|gaap eps", q):
        return "eps"
    if re.search(r"revenue|sales|total revenue|quarterly revenue", q):
        return "revenue"
    return None


def _outcome_probabilities(m):
    outcomes = _parse_jsonish(m.get("outcomes")) or m.get("outcomes")
    prices = _parse_jsonish(m.get("outcomePrices")) or m.get("outcomePrices")
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return []
    out = []
    for i, outcome in enumerate(outcomes):
        if i >= len(prices):
            continue
        p = _num(prices[i])
        if p is None or p < 0 or p > 1:
            continue
        out.append({"outcome": str(outcome), "probability": p})
    return out


def _yes_probability(m):
    for x in _outcome_probabilities(m):
        if x["outcome"].strip().lower() in {"yes", "是"}:
            return x["probability"]
    return None


def _market_matches_symbol(m, symbol):
    q = _market_question(m)
    context_text = " ".join(str(m.get(k) or "") for k in (
        "event_title", "event_subtitle", "event_description", "event_category",
        "event_subcategory", "event_ticker", "ticker", "slug", "title"
    ))
    combined = (q + " " + context_text).strip()
    # Match ticker as a token, not as a substring of another word.
    symbol_re = re.compile(r"(?<![A-Z0-9])" + re.escape(symbol) + r"(?![A-Z0-9])", re.I)
    if symbol_re.search(combined):
        return True
    # For common company aliases, permit company-name matches as well.
    aliases = [symbol] + _ALIASES.get(symbol, [])
    low = combined.lower()
    return any(a.lower() in low for a in aliases if a)


def _classify_any(question, event_title="", event_category=""):
    q = (str(question or "") + " " + str(event_title or "") + " " + str(event_category or "")).lower()
    if re.search(r"\beps\b|earnings per share|profit per share|non[- ]gaap eps|gaap eps", q):
        return "eps"
    if re.search(r"revenue|sales|total revenue|quarterly revenue", q):
        return "revenue"
    if re.search(r"beat|earnings|quarterly results|report.*earnings|earnings.*report|profit", q):
        return "earnings"
    if re.search(r"price|hit|reach|dip|above \$|below \$|close at|trade at|stock", q):
        return "price"
    return "other"


def _clean_market(m, symbol, query_specific=False):
    q = _market_question(m)
    event_title = str(m.get("event_title") or "")
    event_category = str(m.get("event_category") or "")
    if not _market_matches_symbol(m, symbol) and not m.get("_query_event_match"):
        return None
    probs = _outcome_probabilities(m)
    if not probs:
        return None
    metric = _classify_any(q, event_title, event_category)
    threshold_metric = metric if metric in {"eps", "revenue"} else None
    threshold = _threshold(q + " " + event_title, threshold_metric, m) if threshold_metric else None
    return {
        "symbol": symbol,
        "question": q,
        "event_title": event_title or None,
        "category": metric,
        "threshold": threshold,
        "yes_probability": _yes_probability(m),
        "outcomes": probs,
        "volume": _num(m.get("volumeNum") if m.get("volumeNum") is not None else m.get("volume")),
        "liquidity": _num(m.get("liquidityNum") if m.get("liquidityNum") is not None else m.get("liquidity")),
        "end_date": m.get("endDate") or m.get("end_date"),
        "slug": m.get("slug"),
        "group_item_title": m.get("groupItemTitle"),
        "group_item_threshold": m.get("groupItemThreshold"),
        "url": ("https://polymarket.com/event/" + str(m.get("slug"))) if m.get("slug") else None,
    }

def _query(symbol, query):
    """Use the same public-search contract that the working macro module uses.

    The previous implementation passed optional/legacy search parameters. A
    rejected parameter caused the exception to be swallowed and the stock card
    silently became ``暂无数据``. Macro works because it uses the minimal public
    search contract. Keep this sidecar aligned with that contract.
    """
    try:
        raw = _get(f"{BASE}/public-search", {
            "q": query,
            "events_status": "active",
            "limit_per_type": 50,
            "page": 1,
            "search_profiles": "false",
        })
        rows = _flatten_markets(raw)
        rows.extend(_event_markets(raw))
        # Gamma /markets supports full-text q as a second discovery path. It is
        # useful when public-search returns a parent event but not its children.
        if not rows:
            try:
                market_raw = _get(f"{BASE}/markets", {
                    "q": query, "active": "true", "closed": "false", "limit": 100,
                })
                if isinstance(market_raw, list):
                    rows.extend(market_raw)
                elif isinstance(market_raw, dict):
                    rows.extend(market_raw.get("markets") or market_raw.get("data") or [])
            except Exception:
                pass
        return rows
    except Exception:
        return []


def _event_markets(raw):
    """Flatten search response with event context, without requiring ticker text
    to be repeated in every child market.
    """
    out=[]
    if not isinstance(raw, dict):
        return out
    events=raw.get('events') or []
    for event in events:
        if not isinstance(event, dict):
            continue
        ctx={f"event_{k}":event.get(k) for k in ('title','subtitle','description','ticker','category','subcategory') if event.get(k)}
        for m in event.get('markets') or []:
            if isinstance(m, dict):
                x=dict(m)
                x.update({k:v for k,v in ctx.items() if k not in x})
                x['_query_event_match']=True
                out.append(x)
    for m in raw.get('markets') or []:
        if isinstance(m, dict):
            x=dict(m); x['_query_event_match']=True; out.append(x)
    return out

def _infer(metric_rows):
    rows = [x for x in metric_rows if x.get("threshold") is not None and x.get("yes_probability") is not None]
    rows.sort(key=lambda x: x["threshold"])
    # Deduplicate thresholds, keeping the most liquid observation.
    dedup = {}
    for x in rows:
        k = round(float(x["threshold"]), 8)
        old = dedup.get(k)
        if old is None or (x.get("liquidity") or 0) > (old.get("liquidity") or 0):
            dedup[k] = x
    rows = list(dedup.values())
    if len(rows) < 3:
        return {"available": False, "thresholds": rows, "reason": "有效门槛不足，至少需要 3 个可验证门槛才能推导中枢。"}

    # Find the 50% crossing by linear interpolation. Probabilities should decline
    # as thresholds rise; sort is retained but do not force monotonicity.
    pairs = [(float(x["threshold"]), float(x["yes_probability"])) for x in rows]
    crossing = None
    for (x1, p1), (x2, p2) in zip(pairs, pairs[1:]):
        if (p1 - 0.5) * (p2 - 0.5) <= 0 and p1 != p2:
            crossing = x1 + (0.5 - p1) * (x2 - x1) / (p2 - p1)
            break
    if crossing is None:
        return {"available": False, "thresholds": rows, "reason": "已有多个门槛，但概率曲线没有形成可验证的 50% 中枢。"}

    lo = next((float(x["threshold"]) for x in rows if float(x["yes_probability"]) >= 0.75), None)
    hi = next((float(x["threshold"]) for x in reversed(rows) if float(x["yes_probability"]) <= 0.25), None)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return {
        "available": True,
        "implicit_median": crossing,
        "range_low": lo,
        "range_high": hi,
        "thresholds": rows,
        "method": "多个财报门槛市场的 YES 概率曲线线性插值；不是公司指引，也不是卖方一致预期。",
    }


_ALIASES = {
    "AAPL": ["Apple", "Apple Inc"], "MSFT": ["Microsoft"], "NVDA": ["NVIDIA", "Nvidia"],
    "AMZN": ["Amazon"], "META": ["Meta", "Facebook"], "GOOGL": ["Google", "Alphabet"], "GOOG": ["Google", "Alphabet"],
    "TSLA": ["Tesla"], "AVGO": ["Broadcom"], "NFLX": ["Netflix"], "MU": ["Micron"], "AMD": ["AMD", "Advanced Micro Devices"],
    "COST": ["Costco"], "ADBE": ["Adobe"], "PEP": ["PepsiCo"], "CSCO": ["Cisco"], "CMCSA": ["Comcast"],
    "INTC": ["Intel"], "QCOM": ["Qualcomm"], "TXN": ["Texas Instruments"], "AMAT": ["Applied Materials"],
    "INTU": ["Intuit"], "ISRG": ["Intuitive Surgical"], "BKNG": ["Booking"], "ADP": ["ADP", "Automatic Data Processing"],
    "GILD": ["Gilead"], "PANW": ["Palo Alto Networks"], "VRTX": ["Vertex"], "LRCX": ["Lam Research"],
    "REGN": ["Regeneron"], "HON": ["Honeywell"], "AMGN": ["Amgen"], "SBUX": ["Starbucks"], "MDLZ": ["Mondelez"],
    "ADI": ["Analog Devices"], "MELI": ["MercadoLibre"], "PDD": ["PDD", "Pinduoduo"], "CRWD": ["CrowdStrike"],
    "KLAC": ["KLA", "KLA Corporation"], "SNPS": ["Synopsys"], "CDNS": ["Cadence"], "MRVL": ["Marvell"],
    "CEG": ["Constellation Energy"], "MAR": ["Marriott"], "ORLY": ["O'Reilly"], "CTAS": ["Cintas"],
}

def analyze_polymarket(symbol: str) -> dict[str, Any]:
    symbol = str(symbol or "").strip().upper()
    with _LOCK:
        hit = _CACHE.get(symbol)
        if hit and time() - hit[0] < TTL:
            return hit[1]

    # Broad discovery: ticker + company aliases + earnings/price phrasing. We
    # intentionally keep all active markets tied to the requested stock rather
    # than restricting the overview to EPS/revenue markets.
    queries = [symbol, f"{symbol} earnings", f"{symbol} revenue", f"{symbol} EPS", f"{symbol} price", f"{symbol} stock"]
    queries += _ALIASES.get(symbol, [])
    queries += [f"{a} earnings" for a in _ALIASES.get(symbol, [])]
    queries = list(dict.fromkeys(queries))[:12]
    markets = []
    errors = []
    with ThreadPoolExecutor(max_workers=min(8, len(queries) or 1)) as ex:
        futures = [ex.submit(_query, symbol, q) for q in queries]
        for fut in as_completed(futures):
            try:
                markets.extend(fut.result())
            except Exception as exc:
                errors.append(str(exc)[:120])

    cleaned = []
    seen = set()
    for m in markets:
        if not isinstance(m, dict) or not _active(m):
            continue
        x = _clean_market(m, symbol)
        if not x:
            continue
        key = (x.get("question"), x.get("event_title"), x.get("slug"), tuple((o["outcome"], round(o["probability"], 6)) for o in x.get("outcomes", [])))
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(x)

    # Highest-information ordering for the overview: category, liquidity,
    # volume, then question. No predictive ranking is implied.
    cleaned.sort(key=lambda x: (
        {"earnings": 0, "eps": 1, "revenue": 2, "price": 3, "other": 4}.get(x.get("category"), 9),
        -(x.get("liquidity") or 0),
        -(x.get("volume") or 0),
        x.get("question") or "",
    ))

    eps = _infer([x for x in cleaned if x["category"] == "eps"])
    revenue = _infer([x for x in cleaned if x["category"] == "revenue"])
    beat = None
    for x in cleaned:
        q = (x["question"] + " " + (x.get("event_title") or "")).lower()
        if ("beat" in q or "above consensus" in q or "exceed" in q) and x.get("yes_probability") is not None:
            if beat is None or (x.get("liquidity") or 0) > (beat.get("liquidity") or 0):
                beat = x

    result = {
        "ok": True,
        "symbol": symbol,
        "available": bool(cleaned),
        "source": "Polymarket 链上预测市场",
        "source_url": "https://polymarket.com/",
        "eps": eps,
        "revenue": revenue,
        "beat_probability": beat.get("yes_probability") if beat else None,
        "beat_question": beat.get("question") if beat else None,
        "markets": cleaned[:100],
        "market_count": len(cleaned),
        "market_types": {k: sum(1 for x in cleaned if x.get("category") == k) for k in ("earnings", "eps", "revenue", "price", "other")},
        "evidence_note": "概览展示的是当前检索到的、与该股票直接相关且有可验证概率价格的活跃 Polymarket 市场；EPS/营收中枢只有在足够门槛形成可验证概率曲线时才反演。",
        "reason": (
            "存在当前可验证的个股 Polymarket 市场；财报中枢按证据强度单独判断。" if cleaned
            else "当前没有找到可验证的、与该股票直接相关的活跃 Polymarket 市场。"
        ),
        "errors": errors[:3],
    }
    with _LOCK:
        _CACHE[symbol] = (time(), result)
    return result

