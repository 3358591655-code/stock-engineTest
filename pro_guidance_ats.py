"""Independent Guidance Whisper + ATS research layer.

This module is deliberately sidecar-only: it never changes the existing
AEL Whisper, Lite, SINGLE, MARKET SCAN, Factor, Risk or Options calculations.
It compares three observable layers:
  1) sell-side consensus
  2) independent AEL Guidance Whisper (management guidance centered)
  3) ATS/microstructure second opinion

All outputs are research references, not private order-book observations.
"""
from datetime import datetime, timezone
import math
import threading
from time import time

import pandas as pd
import yfinance as yf

from pro_whisper import analyze_whisper
from pro_expectation import _finra_ats_source

_CACHE = {}
_LOCK = threading.Lock()
TTL = 900


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _mid(lo, hi):
    lo, hi = _finite(lo), _finite(hi)
    if lo is None or hi is None or hi < lo:
        return None
    return (lo + hi) / 2.0


def _guidance_whisper(consensus, guidance, nowcast, revisions, kind):
    """Independent management-guidance-centered estimate.

    It is intentionally different from pro_whisper._whisper_estimate:
    management guidance is the anchor when present; consensus is a comparison
    reference, not the final model. Missing components are re-weighted.
    """
    if kind == "eps":
        c = _finite((consensus or {}).get("consensus"))
        g = _mid((guidance or {}).get("eps_low"), (guidance or {}).get("eps_high"))
        n = _finite((nowcast or {}).get("eps"))
    else:
        c = _finite((consensus or {}).get("consensus"))
        g = _mid((guidance or {}).get("revenue_low"), (guidance or {}).get("revenue_high"))
        n = _finite((nowcast or {}).get("revenue"))

    rev = _finite((consensus or {}).get("revision_30d_pct"))
    if rev is None:
        rev = _finite((consensus or {}).get("revision_7d_pct"))
    hist_bias = _finite((revisions or {}).get(f"{kind}_bias_pct"))

    components = []
    if g is not None:
        components.append(("管理层Guidance中值", g, 0.55))
    if c is not None:
        components.append(("卖方Consensus", c, 0.25 if g is not None else 0.55))
    if n is not None:
        components.append(("基本面Nowcast", n, 0.20 if g is not None else 0.30))

    if not components:
        return {"value": None, "components": [], "status": "unavailable"}

    # Weighted base. If weights do not sum to 1, normalize them.
    sw = sum(w for _, _, w in components)
    value = sum(v * w for _, v, w in components) / sw

    # Revisions and company historical bias are deliberately small adjustments.
    adj = 0.0
    adj_parts = []
    if rev is not None:
        a = _clamp(rev, -6.0, 6.0) * 0.20
        adj += a
        adj_parts.append(("30D卖方预测修正", a))
    if hist_bias is not None:
        cap = 5.0 if kind == "eps" else 3.0
        a = _clamp(hist_bias, -cap, cap) * 0.15
        adj += a
        adj_parts.append(("公司历史Consensus偏差", a))

    value *= 1.0 + adj / 100.0
    return {
        "value": value,
        "components": [{"factor": n, "value": v, "weight": w} for n, v, w in components],
        "adjustments": [{"factor": n, "pct": round(v, 3)} for n, v in adj_parts],
        "status": "inferred",
    }


def _gap_pct(a, b):
    a, b = _finite(a), _finite(b)
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def _ats_whisper_score(ats, whisper_data):
    """Independent ATS second-opinion score.

    ATS itself supplies activity/attention, not buy/sell direction. Directional
    context is therefore taken only from separately observed options/price
    positioning when available. The ATS contribution can never manufacture a
    bullish/bearish direction from ATS volume alone.
    """
    if not ats.get("available"):
        return {"status": "unavailable", "score": None, "direction": "暂无数据", "confidence": None}

    share = _finite(ats.get("ats_share_pct"))
    change = _finite(ats.get("ats_share_change_pct"))
    z = _finite(ats.get("ats_volume_z"))
    parts = []
    # Attention score: 50 neutral; abnormal ATS activity moves it upward.
    if share is not None:
        parts.append(("ATS占比", _clamp((share - 25.0) / 20.0, -1, 1), 0.40))
    if change is not None:
        parts.append(("ATS周变化", _clamp(change / 5.0, -1, 1), 0.25))
    if z is not None:
        parts.append(("ATS成交异常度", _clamp(z / 3.0, -1, 1), 0.35))
    attention = 50.0 + 50.0 * (sum(v * w for _, v, w in parts) / sum(w for _, _, w in parts)) if parts else None

    # Directional context is explicitly separate from ATS and from the original
    # AEL Whisper score. Use only raw market observations already exposed by the
    # data layer: price momentum and IV skew. ATS itself contributes no direction.
    ms = (whisper_data or {}).get("market_signals") or {}
    mom = _finite(ms.get("momentum_20d_pct"))
    skew = _finite(ms.get("iv_skew_pct"))
    directional_parts = []
    if mom is not None:
        directional_parts.append((_clamp(mom / 12.0, -1, 1), 0.60))
    if skew is not None:
        directional_parts.append((_clamp(-skew / 15.0, -1, 1), 0.40))
    direction_score = (sum(v*w for v,w in directional_parts) / sum(w for _,w in directional_parts)) if directional_parts else 0.0
    if not directional_parts:
        direction = "中性/未知"
    elif direction_score >= 0.20:
        direction = "偏正向"
    elif direction_score <= -0.20:
        direction = "偏负向"
    else:
        direction = "中性"

    # Confidence reflects data richness, not historical hit probability.
    confidence = "中"
    if z is not None and change is not None and share is not None:
        confidence = "中高"
    elif sum(1 for x in (share, change, z) if x is not None) <= 1:
        confidence = "低"

    return {
        "status": "inferred",
        "score": round(_clamp(attention, 0, 100), 1) if attention is not None else None,
        "direction": direction,
        "confidence": confidence,
        "ats_share_pct": share,
        "ats_share_change_pct": change,
        "ats_volume_z": z,
        "decomposition": [{"factor": n, "normalized": round(v, 3), "weight": w} for n, v, w in parts],
        "note": "ATS只证明场外交易活动/异常度，不提供可靠买卖方向；方向参考来自独立的期权/价格定位层。",
    }


def _temperature(ats, whisper_data, days_to_earnings):
    """Event expectation temperature: attention, options, volume and proximity."""
    vals = []
    if ats.get("available"):
        z = _finite(ats.get("ats_volume_z"))
        ch = _finite(ats.get("ats_share_change_pct"))
        if z is not None:
            vals.append(("ATS异常", _clamp(abs(z) / 3.0, 0, 1), 0.35))
        if ch is not None:
            vals.append(("ATS变化", _clamp(abs(ch) / 5.0, 0, 1), 0.15))
    ne = (whisper_data or {}).get("next_earnings_dark") or {}
    nd = ne.get("data") or {}
    iv = _finite(nd.get("event_implied_move_pct"))
    if iv is not None:
        vals.append(("事件隐含波动", _clamp(iv / 15.0, 0, 1), 0.20))
    vr = _finite(nd.get("pre_event_volume_ratio"))
    if vr is not None:
        vals.append(("财报前成交量", _clamp(max(0, vr - 1) / 1.5, 0, 1), 0.10))
    if days_to_earnings is not None:
        vals.append(("财报倒计时", _clamp((21 - max(0, days_to_earnings)) / 21.0, 0, 1), 0.20))
    if not vals:
        return {"score": None, "label": "暂无数据", "components": []}
    score = 100 * sum(v * w for _, v, w in vals) / sum(w for _, _, w in vals)
    label = "低" if score < 35 else "中" if score < 65 else "高" if score < 82 else "很高"
    return {"score": round(score, 1), "label": label, "components": [{"factor": n, "normalized": round(v, 3), "weight": w} for n, v, w in vals]}


def _anomaly(ats):
    if not ats.get("available"):
        return {"status": "unavailable", "level": "暂无数据", "z": None, "reason": ats.get("error") or "FINRA ATS数据不可用"}
    z = _finite(ats.get("ats_volume_z"))
    ch = _finite(ats.get("ats_share_change_pct"))
    if z is None and ch is None:
        return {"status": "inferred", "level": "正常/数据不足", "z": z, "reason": "缺少足够历史周度样本"}
    magnitude = max(abs(z or 0), abs(ch or 0) / 2.5)
    level = "正常" if magnitude < 1 else "关注" if magnitude < 2 else "明显异常" if magnitude < 3 else "极端异常"
    return {"status": "inferred", "level": level, "z": z, "share_change_pct": ch,
            "reason": "统计异常仅表示ATS活动偏离自身近期常态，不代表违法交易或买入/卖出方向。"}


def _earnings_dates(symbol, limit=20):
    try:
        t = yf.Ticker(symbol)
        df = t.get_earnings_dates(limit=limit)
        if df is None or df.empty:
            return []
        out = []
        for idx in df.index:
            try:
                ts = pd.Timestamp(idx)
                if ts.tzinfo is not None:
                    ts = ts.tz_convert(None)
                out.append(ts.normalize())
            except Exception:
                continue
        return sorted(set(out))
    except Exception:
        return []


def _ats_backtest(symbol, ats):
    """Backtest ATS event-intensity signal against post-earnings absolute move.

    This is intentionally not a directional hit-rate: FINRA ATS aggregation does
    not identify trade direction. A valid sample requires an ATS week preceding
    an earnings event and enough price history after that event.
    """
    if not ats.get("available"):
        return {"status": "unavailable", "valid_samples": 0, "score": None,
                "label": "暂无数据", "rows": [], "reason": ats.get("error") or "ATS数据不可用"}
    weeks = ats.get("weeks") or []
    earnings = _earnings_dates(symbol, limit=24)
    if not weeks or not earnings:
        return {"status": "insufficient", "valid_samples": 0, "score": None, "label": "样本不足", "rows": []}
    try:
        h = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return {"status": "insufficient", "valid_samples": 0, "score": None, "label": "行情不足", "rows": []}
        close = pd.to_numeric(h["Close"], errors="coerce").dropna()
        rows = []
        for ed in earnings:
            pre = [w for w in weeks if pd.Timestamp(w.get("week")) <= ed]
            if not pre:
                continue
            w = pre[-1]
            week_ts = pd.Timestamp(w.get("week"))
            # Require the ATS observation to be plausibly before the event.
            if (ed - week_ts).days < 0 or (ed - week_ts).days > 14:
                continue
            # Find nearest trading close on/before earnings and 3 trading days after.
            before = close[close.index.tz_localize(None) <= ed] if getattr(close.index, "tz", None) else close[close.index <= ed]
            after = close[close.index.tz_localize(None) >= ed] if getattr(close.index, "tz", None) else close[close.index >= ed]
            if len(before) < 21 or len(after) < 4:
                continue
            p0 = float(before.iloc[-1])
            p3 = float(after.iloc[3])
            move = abs(p3 / p0 - 1) * 100 if p0 else None
            # Baseline: median absolute 3-day move over prior 20 trading windows.
            arr = close.values
            # Use the index position of p0 to estimate a local baseline.
            idx_pos = close.index.get_loc(before.index[-1])
            if isinstance(idx_pos, slice):
                idx_pos = idx_pos.stop - 1
            prior_moves = []
            start = max(20, int(idx_pos) - 40)
            end = min(int(idx_pos) - 1, len(close) - 4)
            for j in range(start, end + 1):
                b = float(arr[j]); f = float(arr[j + 3])
                if b:
                    prior_moves.append(abs(f / b - 1) * 100)
            baseline = float(pd.Series(prior_moves[-20:]).median()) if prior_moves else None
            z = _finite(w.get("ats_share_pct"))
            az = None
            # Recreate an event score from the available weekly history only.
            prior = [float(x.get("ats_shares") or 0) for x in weeks if pd.Timestamp(x.get("week")) < week_ts]
            cur = float(w.get("ats_shares") or 0)
            if len(prior) >= 4:
                mu = sum(prior[-8:]) / len(prior[-8:])
                sd = (sum((x - mu) ** 2 for x in prior[-8:]) / max(1, len(prior[-8:]) - 1)) ** 0.5
                az = (cur - mu) / sd if sd > 0 else 0.0
            score = 50 + 50 * _clamp((az or 0) / 3, -1, 1)
            rows.append({"event_date": ed.date().isoformat(), "ats_share_pct": z,
                         "ats_z": az, "ats_score": round(score, 1),
                         "post_3d_abs_move_pct": round(move, 3) if move is not None else None,
                         "baseline_3d_abs_move_pct": round(baseline, 3) if baseline is not None else None,
                         "event_vol_ratio": round(move / baseline, 3) if move is not None and baseline and baseline > 0 else None})
        if not rows:
            return {"status": "insufficient", "valid_samples": 0, "score": None, "label": "样本不足", "rows": []}
        ratios = [r["event_vol_ratio"] for r in rows if r.get("event_vol_ratio") is not None]
        high = [r for r in rows if r.get("ats_score", 50) >= 65 and r.get("event_vol_ratio") is not None]
        low = [r for r in rows if r.get("ats_score", 50) < 65 and r.get("event_vol_ratio") is not None]
        high_mean = sum(r["event_vol_ratio"] for r in high) / len(high) if high else None
        low_mean = sum(r["event_vol_ratio"] for r in low) / len(low) if low else None
        edge = ((high_mean / low_mean) - 1) * 100 if high_mean and low_mean else None
        # Calibration score rewards sample size and positive incremental event-volatility edge.
        base = 50 + (_clamp(edge, -30, 30) * 1.0 if edge is not None else 0)
        sample_bonus = min(20, len(rows) * 2)
        score = round(_clamp(base + sample_bonus, 0, 100), 1)
        return {"status": "backtested", "valid_samples": len(rows), "score": score,
                "label": "有历史支持" if score >= 60 else "证据有限",
                "event_vol_edge_pct": round(edge, 2) if edge is not None else None,
                "high_signal_mean_event_vol": round(high_mean, 3) if high_mean is not None else None,
                "low_signal_mean_event_vol": round(low_mean, 3) if low_mean is not None else None,
                "rows": rows[-12:],
                "reason": "回测检验ATS异常与财报后绝对波动之间的历史关系；不把ATS周度汇总伪装成买卖方向命中率。"}
    except Exception as exc:
        return {"status": "error", "valid_samples": 0, "score": None, "label": "回测失败", "rows": [], "reason": str(exc)[:180]}


def analyze_guidance_ats(symbol: str):
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return {"ok": False, "error": "缺少标的"}
    now = time()
    with _LOCK:
        c = _CACHE.get(symbol)
        if c and now - c[0] < TTL:
            return c[1]
    try:
        whisper = analyze_whisper(symbol)
        ats = _finra_ats_source(symbol)
        guidance = whisper.get("guidance") or {}
        eps_cons = (whisper.get("eps") or {}).get("consensus")
        rev_cons = (whisper.get("revenue") or {}).get("consensus")
        eps_guid = _guidance_whisper((whisper.get("eps") or {}), guidance, whisper.get("fundamental_nowcast") or {}, whisper.get("historical_surprise") or {}, "eps")
        rev_guid = _guidance_whisper((whisper.get("revenue") or {}), guidance, whisper.get("fundamental_nowcast") or {}, whisper.get("historical_surprise") or {}, "revenue")
        ats_model = _ats_whisper_score(ats, whisper)
        days = _finite(whisper.get("days_to_earnings"))
        temp = _temperature(ats, whisper, int(days) if days is not None else None)
        anomaly = _anomaly(ats)
        bt = _ats_backtest(symbol, ats)
        result = {
            "ok": True, "symbol": symbol, "as_of": datetime.now(timezone.utc).isoformat(),
            "independent": True,
            "sell_side": {
                "eps": eps_cons, "revenue": rev_cons,
                "earnings_date": whisper.get("next_earnings_date"),
                "days_to_earnings": whisper.get("days_to_earnings"),
            },
            "guidance_whisper": {
                "eps": eps_guid["value"], "revenue": rev_guid["value"],
                "eps_gap_vs_sell_side_pct": _gap_pct(eps_guid["value"], eps_cons),
                "revenue_gap_vs_sell_side_pct": _gap_pct(rev_guid["value"], rev_cons),
                "eps_components": eps_guid["components"], "revenue_components": rev_guid["components"],
                "eps_adjustments": eps_guid.get("adjustments", []), "revenue_adjustments": rev_guid.get("adjustments", []),
                "status": "inferred" if eps_guid["value"] is not None or rev_guid["value"] is not None else "unavailable",
                "method": "Management Guidance-centered independent blend: Guidance midpoint + Sell-side Consensus + Fundamental Nowcast + small revision/history adjustments.",
            },
            "ats": ats,
            "ats_whisper": ats_model,
            "ats_backtest": bt,
            "anomaly_scanner": anomaly,
            "expectation_temperature": temp,
            "comparison": {
                "guidance_vs_sell_side": "高于" if ((_finite(eps_guid["value"]) or 0) > (_finite(eps_cons) or 0) and eps_guid["value"] is not None and eps_cons is not None) else ("低于" if eps_guid["value"] is not None and eps_cons is not None and eps_guid["value"] < eps_cons else "无法判断"),
                "ats_direction": ats_model.get("direction"),
                "ats_vs_guidance": "独立第二意见，不覆盖Guidance Whisper",
            },
            "method_note": "本模块是独立研究层：卖方Consensus、AEL Guidance Whisper、ATS Whisper三者并列。原AEL Whisper算法不被替换、不改权重。ATS只提供活动/异常度，方向仅在有独立期权/价格定位证据时参考；回测检验的是ATS异常与财报后事件波动的历史关系。",
        }
        with _LOCK:
            _CACHE[symbol] = (now, result)
        return result
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "error": str(exc)[:220], "independent": True}
