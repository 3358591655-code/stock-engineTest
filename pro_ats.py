"""AEL ATS sidecar.

Strict isolation rules:
- This file is the only FINRA ATS adapter used by the new Guidance+ATS layer.
- It is never imported by pro_expectation.py or the original AEL Whisper.
- FINRA ATS data is activity/volume evidence, not a buy/sell order book.
- Current state uses the rolling 12-month weeklySummary dataset.
- Backtest uses every returned week, not just the last 12 weeks.

The module deliberately fails closed: missing credentials/data => unavailable,
never a fabricated score.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import time
import math
import os
import threading
import requests
import pandas as pd
import yfinance as yf

_FINRA_TOKEN_CACHE = {"token": None, "expires_at": 0.0}
_FINRA_TOKEN_LOCK = threading.Lock()
_CACHE = {}
_LOCK = threading.Lock()
TTL = 900


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _token(force_refresh=False):
    client_id = os.getenv("FINRA_CLIENT_ID", "").strip()
    client_secret = os.getenv("FINRA_CLIENT_SECRET", "").strip()
    legacy = os.getenv("FINRA_API_TOKEN", "").strip()
    if not client_id or not client_secret:
        if legacy:
            return legacy, "legacy_bearer_token", None
        return None, "oauth2_client_credentials", "未配置 FINRA_CLIENT_ID / FINRA_CLIENT_SECRET"
    now = time()
    with _FINRA_TOKEN_LOCK:
        if not force_refresh and _FINRA_TOKEN_CACHE.get("token") and now < float(_FINRA_TOKEN_CACHE.get("expires_at") or 0):
            return _FINRA_TOKEN_CACHE["token"], "oauth2_client_credentials", None
        try:
            r = requests.post(
                "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token",
                params={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
                timeout=6,
            )
            r.raise_for_status()
            js = r.json() if r.content else {}
            tok = str(js.get("access_token") or "").strip()
            if not tok:
                return None, "oauth2_client_credentials", "FINRA OAuth 未返回 access_token"
            exp = _finite(js.get("expires_in")) or 1800.0
            _FINRA_TOKEN_CACHE.update({"token": tok, "expires_at": now + max(60.0, min(1800.0, exp - 60.0))})
            return tok, "oauth2_client_credentials", None
        except Exception as exc:
            return None, "oauth2_client_credentials", f"FINRA OAuth 认证失败：{str(exc)[:160]}"


def _query_weekly(token, symbol, summary_type):
    url = "https://api.finra.org/data/group/OTCMarket/name/weeklySummary"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Data-API-Version": "1"}
    fields = [
        "issueSymbolIdentifier", "issueName", "weekStartDate", "summaryStartDate",
        "summaryTypeCode", "tierIdentifier", "totalWeeklyShareQuantity",
        "totalWeeklyTradeCount", "lastUpdateDate",
    ]
    payload = {
        "limit": 1000,
        "fields": fields,
        "compareFilters": [
            {"compareType": "equal", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol},
            {"compareType": "equal", "fieldName": "tierIdentifier", "fieldValue": "T1"},
            {"compareType": "equal", "fieldName": "summaryTypeCode", "fieldValue": summary_type},
        ],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=7)
    if r.status_code == 401:
        raise PermissionError("FINRA access token 已失效")
    r.raise_for_status()
    js = r.json()
    return js if isinstance(js, list) else []


def _source(symbol):
    symbol = str(symbol or "").strip().upper()
    out = {
        "available": False, "weeks": [], "ats_share_pct": None,
        "ats_share_change_pct": None, "ats_volume_z": None,
        "block_or_flow_direction": "unknown", "error": None,
        "source": "FINRA OTC Transparency / Weekly Summary",
        "dataset": "weeklySummary", "lookback": "rolling 12 months",
    }
    if not symbol:
        out["error"] = "股票代码为空"
        return out
    now = time()
    with _LOCK:
        cached = _CACHE.get(symbol)
        if cached and now - cached[0] < TTL:
            return cached[1]
    token, auth_mode, err = _token()
    out["auth"] = auth_mode
    if not token:
        out["error"] = err
        return out
    try:
        try:
            ats_rows, otc_rows = _query_weekly(token, symbol, "ATS_W_SMBL"), _query_weekly(token, symbol, "OTC_W_SMBL")
        except PermissionError:
            token, auth_mode, err = _token(True)
            if not token:
                out["error"] = err or "FINRA OAuth 刷新失败"
                return out
            out["auth"] = auth_mode
            ats_rows, otc_rows = _query_weekly(token, symbol, "ATS_W_SMBL"), _query_weekly(token, symbol, "OTC_W_SMBL")
        weekly = {}
        for row in ats_rows:
            w = row.get("weekStartDate") or row.get("summaryStartDate")
            if w:
                b = weekly.setdefault(str(w), {"ats": 0.0, "otc": 0.0, "ats_trades": 0.0, "otc_trades": 0.0})
                b["ats"] += _finite(row.get("totalWeeklyShareQuantity")) or 0.0
                b["ats_trades"] += _finite(row.get("totalWeeklyTradeCount")) or 0.0
        for row in otc_rows:
            w = row.get("weekStartDate") or row.get("summaryStartDate")
            if w:
                b = weekly.setdefault(str(w), {"ats": 0.0, "otc": 0.0, "ats_trades": 0.0, "otc_trades": 0.0})
                b["otc"] += _finite(row.get("totalWeeklyShareQuantity")) or 0.0
                b["otc_trades"] += _finite(row.get("totalWeeklyTradeCount")) or 0.0
        ordered = []
        for week, b in sorted(weekly.items()):
            total = b["ats"] + b["otc"]
            if total <= 0:
                continue
            ordered.append({
                "week": week, "ats_shares": b["ats"], "non_ats_shares": b["otc"],
                "ats_trades": b["ats_trades"], "non_ats_trades": b["otc_trades"],
                "ats_share_pct": b["ats"] / total * 100.0,
            })
        if not ordered:
            out["error"] = "FINRA 未返回该标的 ATS/OTC 周度数据"
            return out
        latest = ordered[-1]
        out.update({"weeks": ordered, "ats_share_pct": latest["ats_share_pct"]})
        if len(ordered) >= 2:
            out["ats_share_change_pct"] = latest["ats_share_pct"] - ordered[-2]["ats_share_pct"]
        vals = [float(x["ats_shares"]) for x in ordered[-9:-1] if float(x["ats_shares"]) > 0]
        if len(vals) >= 4:
            mu = sum(vals) / len(vals)
            sd = (sum((v - mu) ** 2 for v in vals) / max(1, len(vals) - 1)) ** 0.5
            out["ats_volume_z"] = (latest["ats_shares"] - mu) / sd if sd > 0 else 0.0
        out["available"] = True
        with _LOCK:
            _CACHE[symbol] = (now, out)
        return out
    except Exception as exc:
        out["error"] = str(exc)[:180]
        return out


def ats_source(symbol):
    """Public sidecar adapter used by pro_guidance_ats."""
    return _source(symbol)
