"""AEL Whisper Backtest & Calibration Engine (v2.6.4).

Research-first historical validation for the Whisper layer.  It uses only values
that the public earnings-history endpoint exposes for each historical earnings
event.  It does NOT reconstruct unavailable historical option chains or today's
revised estimates as if they were known in the past.  Rows without a historical
estimate/actual pair are excluded from accuracy metrics and reported separately.
"""
from __future__ import annotations

import math
import json
import os
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

# Calibration cache is deliberately separate from the live Whisper path.
# Persist only completed calibration metadata so a normal page refresh does not
# turn a valid calibration back into "un-calibrated". No raw market/PII data is
# stored here. If the runtime filesystem is unavailable, the in-memory cache is
# still sufficient for the current process.
_CALIBRATION_CACHE = {}
_CACHE_LOCK = threading.RLock()
_CACHE_FILE = Path(os.getenv("AEL_WHISPER_CALIBRATION_CACHE", Path(__file__).with_name(".ael_whisper_calibration.json")))

def _load_cache():
    try:
        if _CACHE_FILE.exists():
            raw=json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw,dict):
                _CALIBRATION_CACHE.update({str(k).upper():v for k,v in raw.items() if isinstance(v,dict)})
    except Exception:
        pass

def _save_cache():
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp=_CACHE_FILE.with_suffix(_CACHE_FILE.suffix+".tmp")
        tmp.write_text(json.dumps(_CALIBRATION_CACHE, ensure_ascii=False, separators=(",",":")), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except Exception:
        pass

_load_cache()


def _finite(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _num(row, *names):
    for n in names:
        if n in row.index:
            v = _finite(row.get(n))
            if v is not None:
                return v
    return None


def _weighted_bias(errors: List[float]) -> float:
    if not errors:
        return 0.0
    # Newer observations carry more weight.  Shrink aggressively when the
    # historical sample is small or unstable; this prevents a single giant beat
    # from becoming a fake deterministic forecast.
    vals = [_clamp(float(x), -30.0, 30.0) for x in errors]
    ws = [0.72 ** i for i in range(len(vals))]
    mean = sum(v * w for v, w in zip(vals, ws)) / sum(ws)
    std = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
    sample_shrink = math.sqrt(len(vals) / (len(vals) + 3.0))
    stability = min(1.0, 8.0 / max(std, 8.0))
    return _clamp(mean * sample_shrink * stability, -7.0, 7.0)


def _ael_historical_estimate(consensus: float, prior_errors: List[float]) -> float:
    """Historical replay of the calibration component available before event."""
    return consensus * (1.0 + _weighted_bias(prior_errors) / 100.0)


def _error_pct(pred: Optional[float], actual: Optional[float]) -> Optional[float]:
    if pred is None or actual is None or actual == 0:
        return None
    # Normalize by |actual| so negative EPS does not invert the meaning of
    # forecast error. This remains a relative error metric, not a probability.
    return (pred - actual) / abs(actual) * 100.0


def _mae(values):
    v = [abs(x) for x in values if x is not None and math.isfinite(x)]
    return sum(v) / len(v) if v else None


def _mape(values):
    return _mae(values)


def _rmse(values):
    v = [x for x in values if x is not None and math.isfinite(x)]
    return math.sqrt(sum(x * x for x in v) / len(v)) if v else None


def _beat_accuracy(predictions, actuals):
    pairs = [(p, a) for p, a in zip(predictions, actuals) if p is not None and a is not None]
    if not pairs:
        return None
    # A 'beat' here means actual > the estimate.  Accuracy measures whether the
    # model's directional relationship to the estimate was consistent with the
    # actual, not whether it guessed the magnitude perfectly.
    # For the consensus itself this is naturally only meaningful as a sign test.
    return None


def _metric_block(preds, actuals):
    errs = [_error_pct(p, a) for p, a in zip(preds, actuals)]
    clean = [e for e in errs if e is not None]
    return {
        "n": len(clean),
        "mae_pct": _mae(clean),
        "mape_pct": _mape(clean),
        "rmse_pct": _rmse(clean),
        "median_abs_error_pct": float(np.median([abs(x) for x in clean])) if clean else None,
    }


def _beat_signal(pred, consensus, actual):
    if pred is None or consensus is None or actual is None:
        return None
    # AEL Whisper signals a beat when its estimate is above Consensus; the
    # realized outcome is a beat when Actual is above Consensus.
    predicted_beat = pred > consensus
    actual_beat = actual > consensus
    return predicted_beat == actual_beat


def _beat_metrics(rows, pred_key, consensus_key, actual_key):
    vals=[_beat_signal(r.get(pred_key), r.get(consensus_key), r.get(actual_key)) for r in rows]
    vals=[x for x in vals if x is not None]
    return {"n":len(vals),"accuracy_pct":(sum(vals)/len(vals)*100.0) if vals else None}


def _safe_dates(symbol: str, limit: int):
    t = yf.Ticker(symbol)
    d = t.get_earnings_dates(limit=limit)
    if d is None or d.empty:
        return pd.DataFrame()
    return d


def _calibration_from_metrics(eps_c, eps_w, rev_c, rev_w, valid_n):
    """Turn historical replay into a calibration score, not a probability.

    The score rewards: (1) enough valid samples, (2) lower Whisper error than
    Consensus, and (3) lower dispersion. It is intentionally capped below 100
    and is never presented as a probability of correctness.
    """
    if valid_n < 8:
        return None, "INSUFFICIENT"
    improvements = [
        x for x in (
            _improvement(eps_c.get("mae_pct"), eps_w.get("mae_pct")),
            _improvement(rev_c.get("mae_pct"), rev_w.get("mae_pct")),
        ) if x is not None and math.isfinite(x)
    ]
    mean_improve = float(np.mean(improvements)) if improvements else 0.0
    dispersions = [
        x for x in (eps_w.get("rmse_pct"), rev_w.get("rmse_pct"))
        if x is not None and math.isfinite(x)
    ]
    dispersion = float(np.mean(dispersions)) if dispersions else None

    # Sample confidence: 8 samples starts the scale; 20+ saturates this term.
    sample_score = 30.0 + 20.0 * min(1.0, (valid_n - 8) / 12.0)
    # Stability: lower RMSE receives more points, with 20% error as a neutral
    # midpoint. This is deliberately conservative.
    stability_score = 30.0 if dispersion is None else _clamp(36.0 - dispersion * 0.75, 8.0, 30.0)
    # Improvement can help or hurt, but cannot dominate the score.
    improvement_score = _clamp(20.0 + mean_improve * 0.45, 5.0, 30.0)
    score = int(round(_clamp(sample_score + stability_score + improvement_score, 0.0, 100.0)))
    label = "HIGH" if score >= 75 else ("MEDIUM" if score >= 55 else "LOW")
    return score, label


def _improvement(base, model):
    if base is None or model is None or base <= 0:
        return None
    return (1.0 - model / base) * 100.0


def get_cached_calibration(symbol: str):
    key=str(symbol or "").strip().upper()
    with _CACHE_LOCK:
        return dict(_CALIBRATION_CACHE[key]) if key in _CALIBRATION_CACHE else None


def run_whisper_backtest(symbol: str, quarters: int = 20) -> Dict[str, Any]:
    requested = str(symbol or "").strip().upper()
    quarters = int(_clamp(int(quarters or 20), 8, 40))
    if not requested:
        return {"ok": False, "error": "缺少标的"}

    try:
        d = _safe_dates(requested, max(quarters + 8, 30))
    except Exception as exc:
        return {"ok": False, "symbol": requested, "error": str(exc)[:220]}

    if d.empty:
        return {"ok": False, "symbol": requested, "error": "公开财报历史不足，无法建立回测样本"}

    # Yahoo returns newest-first. Build a chronological list first, then replay
    # each event using only estimates/errors from strictly OLDER events.
    raw_rows = []
    for idx, r in d.iterrows():
        eps_est = _num(r, "EPS Estimate", "EPS Estimate (GAAP)", "Estimate")
        eps_actual = _num(r, "Reported EPS", "Reported EPS (GAAP)", "Actual")
        rev_est = _num(r, "Revenue Estimate", "Revenue Est.", "Revenue Estimate (GAAP)")
        rev_actual = _num(r, "Reported Revenue", "Revenue", "Actual Revenue")
        try:
            event_ts = pd.Timestamp(idx)
        except Exception:
            event_ts = pd.Timestamp.utcnow()
        raw_rows.append((event_ts, eps_est, eps_actual, rev_est, rev_actual))

    raw_rows.sort(key=lambda x: x[0])
    raw_rows = raw_rows[-quarters:]

    rows = []
    prior_eps_errors: List[float] = []
    prior_rev_errors: List[float] = []

    for event_ts, eps_est, eps_actual, rev_est, rev_actual in raw_rows:
        # Only older events contribute to this event's reconstructed bias.
        eps_whisper = _ael_historical_estimate(eps_est, prior_eps_errors) if eps_est is not None else None
        rev_whisper = _ael_historical_estimate(rev_est, prior_rev_errors) if rev_est is not None else None
        row = {
            "event_date": event_ts.isoformat(),
            "eps_consensus": eps_est,
            "eps_whisper": eps_whisper,
            "eps_actual": eps_actual,
            "eps_consensus_error_pct": _error_pct(eps_est, eps_actual),
            "eps_whisper_error_pct": _error_pct(eps_whisper, eps_actual),
            "revenue_consensus": rev_est,
            "revenue_whisper": rev_whisper,
            "revenue_actual": rev_actual,
            "revenue_consensus_error_pct": _error_pct(rev_est, rev_actual),
            "revenue_whisper_error_pct": _error_pct(rev_whisper, rev_actual),
            "source": "Yahoo Finance earnings history",
            "point_in_time": bool(eps_est is not None or rev_est is not None),
            "market_implied_historical": False,
        }
        rows.append(row)
        if eps_est is not None and eps_actual is not None:
            prior_eps_errors.append(_error_pct(eps_est, eps_actual))
        if rev_est is not None and rev_actual is not None:
            prior_rev_errors.append(_error_pct(rev_est, rev_actual))

    eps_rows = [r for r in rows if r["eps_consensus"] is not None and r["eps_actual"] is not None]
    rev_rows = [r for r in rows if r["revenue_consensus"] is not None and r["revenue_actual"] is not None]
    eps_c = _metric_block([r["eps_consensus"] for r in eps_rows], [r["eps_actual"] for r in eps_rows])
    eps_w = _metric_block([r["eps_whisper"] for r in eps_rows], [r["eps_actual"] for r in eps_rows])
    rev_c = _metric_block([r["revenue_consensus"] for r in rev_rows], [r["revenue_actual"] for r in rev_rows])
    rev_w = _metric_block([r["revenue_whisper"] for r in rev_rows], [r["revenue_actual"] for r in rev_rows])

    valid_n = max(eps_w["n"], rev_w["n"])
    calibration, label = _calibration_from_metrics(eps_c, eps_w, rev_c, rev_w, valid_n)
    eps_improve = _improvement(eps_c["mae_pct"], eps_w["mae_pct"])
    rev_improve = _improvement(rev_c["mae_pct"], rev_w["mae_pct"])
    eps_beat = _beat_metrics(rows, "eps_whisper", "eps_consensus", "eps_actual")
    rev_beat = _beat_metrics(rows, "revenue_whisper", "revenue_consensus", "revenue_actual")

    result = {
        "ok": True,
        "symbol": requested,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "model": "AEL Whisper Backtest v2.6.5",
        "periods_requested": quarters,
        "periods_returned": len(rows),
        "point_in_time_integrity": {
            "status": "chronological-public-history-replay",
            "future_actual_used_for_current_row": False,
            "status_detail": "每个历史事件只使用更早事件的误差来构建历史 Bias；当前事件的 Actual 在预测后才进入下一期。",
            "limitation": "Yahoo earnings-history 是公开历史快照，但并不保证保存所有当时的分析师修订版本，因此这不是完整 sell-side point-in-time 数据库。"
        },
        "eps": {"consensus": eps_c, "ael_whisper": eps_w, "whisper_edge_pct": eps_improve, "beat_miss_accuracy_pct": eps_beat["accuracy_pct"], "beat_miss_samples": eps_beat["n"]},
        "revenue": {"consensus": rev_c, "ael_whisper": rev_w, "whisper_edge_pct": rev_improve, "beat_miss_accuracy_pct": rev_beat["accuracy_pct"], "beat_miss_samples": rev_beat["n"]},
        "historical_market_implied": {
            "available": False,
            "reason": "公开 Yahoo 历史接口未提供可验证的逐财报历史期权 IV/skew 快照；AEL 不用今天的期权数据倒填过去。"
        },
        "calibration": {
            "score": calibration,
            "label": label,
            "valid_samples": valid_n,
            "mean_whisper_edge_pct": float(np.mean([x for x in (eps_improve, rev_improve) if x is not None])) if any(x is not None for x in (eps_improve, rev_improve)) else None,
            "interpretation": "历史校准分数，不是预测正确概率；只有完成本次回测后才允许反哺当前 Whisper 的 Calibrated Confidence。"
        },
        "rows": rows,
        "source_status": {
            "source": "Yahoo Finance earnings history",
            "rows_with_eps_estimate_and_actual": len(eps_rows),
            "rows_with_revenue_estimate_and_actual": len(rev_rows),
            "excluded_rows": len(rows) - valid_n,
        }
    }
    # Cache only a completed calibration result. This is the bridge from the
    # optional Backtest Lab to the live Confidence field; no backtest runs on
    # the normal Whisper request.
    with _CACHE_LOCK:
        _CALIBRATION_CACHE[requested] = {
        "score": calibration,
        "label": label,
        "valid_samples": valid_n,
        "mean_whisper_edge_pct": result["calibration"]["mean_whisper_edge_pct"],
        "as_of": result["as_of"],
        "source": result["source_status"]["source"],
        "point_in_time_status": result["point_in_time_integrity"]["status"],
        "periods_requested": quarters,
    }
        _save_cache()
    return result

