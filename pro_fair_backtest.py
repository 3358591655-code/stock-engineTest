"""AEL vs ATS fair forecast backtest.

This is a NEW comparison layer. It does not modify the original AEL Whisper
backtest or the original ATS event/volatility backtest.

Fair benchmark:
- same earnings events
- same pre-event information cutoff
- sell-side consensus is the baseline
- compare absolute forecast error for revenue and EPS
- score each model by how much it reduces error versus consensus

Historical ATS replay uses only ATS weekly activity available before the event
plus pre-event price direction. Historical option snapshots are not available
from the current free data chain, so they are not backfilled.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf

from pro_ats import ats_source
from pro_whisper_backtest import run_whisper_backtest


def _finite(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _abs_error(pred, actual):
    p,a=_finite(pred),_finite(actual)
    if p is None or a is None or a==0:return None
    return abs(p-a)/abs(a)*100.0


def _improvement(baseline_error, model_error):
    b,m=_finite(baseline_error),_finite(model_error)
    if b is None or m is None or b<=0:return None
    return (1.0-m/b)*100.0


def _earnings_history(symbol, limit=50):
    try:
        d=yf.Ticker(symbol).get_earnings_dates(limit=limit)
        if d is None or d.empty:return []
        out=[]
        for idx in d.index:
            try:
                ts=pd.Timestamp(idx)
                if ts.tzinfo is not None:ts=ts.tz_convert(None)
                out.append(ts.normalize())
            except Exception:pass
        return sorted(set(out))
    except Exception:
        return []


def _price_frame(symbol):
    try:
        h=yf.Ticker(symbol).history(period="2y",interval="1d",auto_adjust=False)
        if h is None or h.empty:return None
        c=pd.to_numeric(h.get("Close"),errors="coerce").dropna()
        if c.empty:return None
        if getattr(c.index,"tz",None) is not None:c.index=c.index.tz_localize(None)
        return c
    except Exception:
        return None


def _historical_ats_signal(weeks, event_date, close):
    """Replay ATS signal using only weeks and prices available before event."""
    if not weeks:return None
    ed=pd.Timestamp(event_date).normalize()
    eligible=[]
    for w in weeks:
        try:
            wt=pd.Timestamp(w.get("week")).normalize()
            if wt<=ed:eligible.append((wt,w))
        except Exception:pass
    if not eligible:return None
    week_ts,w=eligible[-1]
    # FINRA weeklySummary is keyed by week-start, while earnings dates are
    # event dates.  Use the latest completed/pre-event week instead of requiring
    # an exact calendar match.  A strict 14-day gate can incorrectly turn a
    # valid pre-event observation into zero common samples when a week is absent.
    staleness_days = (ed-week_ts).days
    if staleness_days < 0 or staleness_days > 28:
        return None

    # ATS intensity: current share quantity versus the preceding 8 weeks.
    hist=[_finite(x[1].get("ats_shares")) for x in eligible[:-1]]
    hist=[x for x in hist if x is not None and x>0][-8:]
    current=_finite(w.get("ats_shares"))
    z=None
    if current is not None and len(hist)>=4:
        mu=sum(hist)/len(hist)
        sd=(sum((x-mu)**2 for x in hist)/max(1,len(hist)-1))**0.5
        z=(current-mu)/sd if sd>0 else 0.0
    prev=_finite(eligible[-2][1].get("ats_share_pct")) if len(eligible)>=2 else None
    share=_finite(w.get("ats_share_pct"))
    change=(share-prev) if share is not None and prev is not None else None

    parts=[]
    if z is not None:parts.append(_clamp(z/3,-1,1)*0.60)
    if change is not None:parts.append(_clamp(change/5,-1,1)*0.40)
    intensity=sum(parts)/sum([0.60,0.40][:len(parts)]) if parts else 0.0

    # Pre-event price direction. This is explicitly not post-event information.
    direction=0.0
    if close is not None and not close.empty:
        pre=close[close.index<=ed]
        if len(pre)>=21:
            p0=_finite(pre.iloc[-21]);p1=_finite(pre.iloc[-1])
            if p0 and p1:direction=_clamp((p1/p0-1)*100/12,-1,1)

    # ATS activity controls intensity; price direction supplies the sign.
    signed=direction*max(0.25,abs(intensity)) if direction!=0 else 0.0
    score=50+50*_clamp(signed,-1,1)
    return {
        "week":week_ts.date().isoformat(),
        "ats_data_staleness_days":staleness_days,
        "ats_share_pct":share,
        "ats_z":z,
        "ats_share_change_pct":change,
        "pre_event_price_direction_pct":None if close is None else (float(direction)*12 if direction else 0.0),
        "ats_signal_score":score,
        "direction":"偏正向" if signed>=0.20 else "偏负向" if signed<=-0.20 else "中性",
        "lookahead_free":True,
    }


def _ats_estimate(consensus, signal, kind):
    c=_finite(consensus)
    if c is None or signal is None:return None
    score=_finite(signal.get("ats_signal_score"))
    direction=str(signal.get("direction") or "")
    if score is None:return None
    sign=1 if "正" in direction else -1 if "负" in direction else 0
    intensity=_clamp(abs(score-50)/50,0,1)
    cap=2.5 if kind=="revenue" else 4.0
    adj=sign*intensity*cap
    return c*(1+adj/100.0)


def _metric_summary(rows, metric):
    # Only quarters with all three forecasts/errors enter the metric comparison.
    paired=[]
    for r in rows:
        b=_finite(r.get(f"{metric}_consensus_error_pct")); ae=_finite(r.get(f"{metric}_ael_error_pct")); at=_finite(r.get(f"{metric}_ats_error_pct"))
        if b is not None and ae is not None and at is not None:
            paired.append((b,ae,at))
    if not paired:
        return {"有效样本":0,"卖方平均绝对误差":None,"AEL平均绝对误差":None,"ATS平均绝对误差":None,"AEL相对卖方误差改善":None,"ATS相对卖方误差改善":None}
    be=sum(x[0] for x in paired)/len(paired); ae=sum(x[1] for x in paired)/len(paired); at=sum(x[2] for x in paired)/len(paired)
    return {
        "有效样本":len(paired),
        "卖方平均绝对误差":be,
        "AEL平均绝对误差":ae,
        "ATS平均绝对误差":at,
        "AEL相对卖方误差改善":_improvement(be,ae),
        "ATS相对卖方误差改善":_improvement(be,at),
    }


def _score(improvements):
    vals=[_finite(x) for x in improvements if _finite(x) is not None]
    if not vals:return None
    # 50 = same error as sell-side; 100 = 100% error reduction; 0 = 100% worse.
    return round(_clamp(50+sum(vals)/len(vals)*0.5,0,100),1)


def run_fair_backtest(symbol: str, periods: int = 8) -> Dict[str,Any]:
    symbol=str(symbol or "").strip().upper()
    try:periods=int(periods or 8)
    except Exception:periods=8
    periods=max(4,min(12,periods))
    if not symbol:return {"ok":False,"error":"缺少标的"}
    try:
        ael=run_whisper_backtest(symbol,max(8,periods+4))
        ael_rows=ael.get("rows") or []
        ats=ats_source(symbol)
        weeks=ats.get("weeks") or []
        close=_price_frame(symbol)
        if not weeks:
            return {"ok":True,"symbol":symbol,"status":"insufficient","periods_requested":periods,"valid_samples":0,"reason":"当前可用 ATS 周度历史不足，无法建立公平回测；不伪造结果。","rows":[]}

        # AEL rows are the common event spine. Only events for which ATS has a
        # pre-event weekly observation can enter the fair comparison.
        candidates=[]
        for r in ael_rows:
            try:ed=pd.Timestamp(r.get("event_date")).normalize()
            except Exception:continue
            sig=_historical_ats_signal(weeks,ed,close)
            if not sig:continue
            if not any(_finite(r.get(k)) is not None for k in ("eps_consensus","eps_actual","revenue_consensus","revenue_actual")):continue
            rr=dict(r)
            rr["ats_signal"]=sig
            rr["ats_eps"]=_ats_estimate(r.get("eps_consensus"),sig,"eps")
            rr["ats_revenue"]=_ats_estimate(r.get("revenue_consensus"),sig,"revenue")
            for m in ("eps","revenue"):
                rr[f"{m}_consensus_error_pct"]=_abs_error(r.get(f"{m}_consensus"),r.get(f"{m}_actual"))
                rr[f"{m}_ael_error_pct"]=_abs_error(r.get(f"{m}_whisper"),r.get(f"{m}_actual"))
                rr[f"{m}_ats_error_pct"]=_abs_error(rr.get(f"ats_{m}"),r.get(f"{m}_actual"))
                rr[f"{m}_ael_improvement_pct"]=_improvement(rr.get(f"{m}_consensus_error_pct"),rr.get(f"{m}_ael_error_pct"))
                rr[f"{m}_ats_improvement_pct"]=_improvement(rr.get(f"{m}_consensus_error_pct"),rr.get(f"{m}_ats_error_pct"))
            candidates.append(rr)

        # One row per actual earnings event.  This prevents duplicate Yahoo
        # snapshots for the same event from inflating the comparison.
        deduped={}
        for rr in candidates:
            key=str(rr.get("event_date") or "")[:10]
            if key and key not in deduped:
                deduped[key]=rr
        candidates=sorted(deduped.values(),key=lambda r:r.get("event_date") or "")[-periods:]
        if not candidates:
            return {"ok":True,"symbol":symbol,"status":"insufficient","periods_requested":periods,"valid_samples":0,"reason":"没有同时具备卖方历史预期、实际财报和财报前 ATS 数据的共同财报季度。","rows":[]}

        eps=_metric_summary(candidates,"eps");rev=_metric_summary(candidates,"revenue")
        improvements=[eps.get("AEL相对卖方误差改善"),rev.get("AEL相对卖方误差改善")]
        ael_score=_score(improvements)
        improvements=[eps.get("ATS相对卖方误差改善"),rev.get("ATS相对卖方误差改善")]
        ats_score=_score(improvements)
        available_scores=[x for x in (ael_score,ats_score) if x is not None]
        conclusion="暂无足够共同样本"
        if ael_score is not None and ats_score is not None:
            gap=ael_score-ats_score
            if abs(gap)<3:conclusion="两者接近"
            elif gap>0:conclusion="AEL"
            else:conclusion="ATS"
        return {
            "ok":True,"symbol":symbol,"status":"backtested" if len(candidates)>=4 else "low_sample",
            "as_of":datetime.now(timezone.utc).isoformat(),"periods_requested":periods,"valid_samples":len(candidates),
            "ael_score":ael_score,"ats_score":ats_score,"conclusion":conclusion,
            "eps":eps,"revenue":rev,
            "rows":candidates,
            "matching_diagnostics": {
                "ael_event_rows_considered": len(ael_rows),
                "common_events_found": len(candidates),
                "ats_matching_rule": "latest FINRA pre-event weekStartDate <= earnings event date; maximum 28 calendar days stale; no post-event ATS data used",
                "ats_week_count": len(weeks),
            },
            "fair_rule":"相同财报事件 + 相同信息截止点 + 卖方共识作为基准 + 同一绝对误差公式；分数仅用于AEL与ATS横向比较。",
            "point_in_time":"AEL使用历史公开估计重演；ATS只使用财报前ATS周度活动与财报前价格方向，不使用财报后的数据。",
            "ats_limitation":"当前免费ATS历史为滚动12个月；历史期权快照不可验证，因此公平ATS回放不使用今天的期权数据倒填过去。",
            "score_definition":"50分=与卖方误差相同；每改善卖方误差2个百分点，比较分数提高1分；100分封顶。",
        }
    except Exception as exc:
        return {"ok":False,"symbol":symbol,"status":"error","error":str(exc)[:220],"rows":[]}
