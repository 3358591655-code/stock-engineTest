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


def _clean_market(m, symbol):
    q = _market_question(m)
    context_text = " ".join(str(m.get(k) or "") for k in ("event_title", "event_subtitle", "event_description", "event_category", "event_subcategory", "slug", "ticker"))
    combined = (q + " " + context_text).strip()
    # Search results can include adjacent markets. Keep only markets that
    # actually reference the requested ticker, while allowing ticker-bearing
    # event metadata to identify child threshold markets.
    symbol_re = re.compile(r"(?<![A-Z0-9])" + re.escape(symbol) + r"(?![A-Z0-9])", re.I)
    if not symbol_re.search(combined):
        return None
    metric = _classify(combined)
    if not metric:
        return None
    p = _yes_probability(m)
    threshold = _threshold(combined, metric, m)
    return {
        "symbol": symbol,
        "question": q,
        "metric": metric,
        "threshold": threshold,
        "yes_probability": p,
        "volume": _num(m.get("volumeNum") if m.get("volumeNum") is not None else m.get("volume")),
        "liquidity": _num(m.get("liquidityNum") if m.get("liquidityNum") is not None else m.get("liquidity")),
        "slug": m.get("slug"),
        "group_item_title": m.get("groupItemTitle"),
        "group_item_threshold": m.get("groupItemThreshold"),
        "url": ("https://polymarket.com/event/" + str(m.get("slug"))) if m.get("slug") else None,
    }

def _query(symbol, query):
    try:
        raw = _get(f"{BASE}/public-search", {
            "q": query,
            "events_status": "active",
            "keep_closed_markets": 0,
            "limit_per_type": 50,
            "search_tags": "true",
            "optimized": "true",
        })
        return _flatten_markets(raw)
    except Exception:
        return []

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


def analyze_polymarket(symbol: str) -> dict[str, Any]:
    symbol = str(symbol or "").strip().upper()
    with _LOCK:
        hit = _CACHE.get(symbol)
        if hit and time() - hit[0] < TTL:
            return hit[1]

    # Keep the request set tiny and parallel. This sidecar must never block AEL.
    # One small public-search request is intentional: this module is a UI sidecar,
    # so a slow prediction-market endpoint must never become page latency.
    queries = [symbol, f"{symbol} earnings", f"{symbol} revenue", f"{symbol} EPS"]
    markets = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_query, symbol, q) for q in queries]
        for fut in as_completed(futures):
            try:
                markets.extend(fut.result())
            except Exception:
                pass

    cleaned = []
    seen = set()
    for m in markets:
        if not isinstance(m, dict) or not _active(m):
            continue
        x = _clean_market(m, symbol)
        if not x:
            continue
        key = (x["question"], x.get("metric"))
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(x)

    eps = _infer([x for x in cleaned if x["metric"] == "eps"])
    revenue = _infer([x for x in cleaned if x["metric"] == "revenue"])
    # Beat probability is only reported when an explicit beat/above-consensus
    # market is identifiable. Do not invent it from unrelated price markets.
    beat = None
    for x in cleaned:
        q = x["question"].lower()
        if ("beat" in q or "above consensus" in q or "exceed" in q) and x.get("yes_probability") is not None:
            if beat is None or (x.get("liquidity") or 0) > (beat.get("liquidity") or 0):
                beat = x

    result = {
        "ok": True,
        "symbol": symbol,
        "available": bool(cleaned),
        "source": "Polymarket 链上预测市场",
        "source_url": "https://polymarket.com/predictions/earnings",
        "eps": eps,
        "revenue": revenue,
        "beat_probability": beat.get("yes_probability") if beat else None,
        "beat_question": beat.get("question") if beat else None,
        "markets": cleaned[:20],
        "reason": "；".join([x["reason"] for x in (eps, revenue) if not x.get("available")]) if cleaned else "暂无可验证的个股财报预测市场。",
    }
    with _LOCK:
        _CACHE[symbol] = (time(), result)
    return result
