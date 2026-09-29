"""AEL Pro Buy-Side Expectation Engine.

Optional research layer only. It is never imported by Lite core scoring or scans.
Every source is isolated: source failure => None + diagnostics, never a whole-request
failure unless the primary symbol history itself is unavailable.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import time
import math
import threading
import os
import re
import requests
import zipfile
import tempfile
from pathlib import Path
import yfinance as yf
import pandas as pd

_CACHE = {}
_LOCK = threading.Lock()
TTL = 900


def _finite(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _safe_history(symbol):
    try:
        h = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return None, "历史行情为空"
        h = h.dropna(subset=["Close"])
        if len(h) < 30:
            return None, "历史行情不足30个交易日"
        return h, None
    except Exception as exc:
        return None, str(exc)[:180]


def _price_signal(h):
    close = pd.to_numeric(h["Close"], errors="coerce").dropna()
    vol = pd.to_numeric(h.get("Volume"), errors="coerce").reindex(close.index).fillna(0)
    if len(close) < 30:
        return None
    px = float(close.iloc[-1])
    ret20 = (px / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else None
    ret60 = (px / float(close.iloc[-61]) - 1) * 100 if len(close) >= 61 else None
    ma200 = float(close.tail(min(200, len(close))).mean())
    vol20 = float(vol.tail(min(20, len(vol))).mean())
    vol_prev = float(vol.tail(min(60, len(vol))).head(max(1, min(40, len(vol)-20))).mean())
    vol_ratio = vol20 / vol_prev if vol_prev > 0 else None
    dd = (px / float(close.cummax().iloc[-1]) - 1) * 100
    return {
        "price": px, "momentum_20d_pct": ret20, "momentum_60d_pct": ret60,
        "above_ma200": px >= ma200 if ma200 else None,
        "volume_ratio_20d": vol_ratio, "drawdown_from_high_pct": dd,
    }



def _finra_ats_source(symbol):
    """Optional FINRA OTC/ATS evidence. Requires FINRA_API_TOKEN.

    FINRA publishes weekly ATS/non-ATS aggregate data with a delay. This is
    evidence about reported off-exchange activity, not a real-time dark-pool
    order book and not proof of buy/sell direction.
    """
    out = {"available": False, "weeks": [], "ats_share_pct": None,
           "ats_share_change_pct": None, "ats_volume_z": None,
           "block_or_flow_direction": "unknown", "error": None,
           "source": "FINRA OTC Transparency / Weekly Summary"}
    token = os.getenv("FINRA_API_TOKEN", "").strip()
    if not token:
        out["error"] = "未配置 FINRA_API_TOKEN；不会使用第三方代理冒充暗池数据"
        return out
    try:
        url = "https://api.finra.org/data/group/OTCMarket/name/weeklySummary"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "Data-API-Version": "1"}
        fields = ["issueSymbolIdentifier", "issueName", "weekStartDate",
                  "summaryStartDate", "summaryTypeCode", "reportTypeCode", "tierIdentifier",
                  "totalWeeklyShareQuantity", "totalTradesCountSum", "lastUpdateDate"]
        # Ask for the recent 12 weeks for this symbol in one request.
        payload = {"limit": 200, "fields": fields, "compareFilters": [
            {"compareType": "equal", "fieldName": "issueSymbolIdentifier", "fieldValue": symbol},
            {"compareType": "equal", "fieldName": "tierIdentifier", "fieldValue": "T1"},
        ]}
        r = requests.post(url, headers=headers, json=payload, timeout=3.0)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            out["error"] = "FINRA 未返回该标的 ATS/OTC 周度数据"
            return out
        weekly = {}
        for row in rows:
            week = row.get("weekStartDate") or row.get("summaryStartDate")
            if not week:
                continue
            code = str(row.get("summaryTypeCode") or row.get("reportTypeCode") or row.get("reportType") or "").upper()
            shares = _finite(row.get("totalWeeklyShareQuantity")) or 0.0
            if code.startswith("ATS_W_") or code.startswith("ATS"):
                bucket = "ats"
            elif code.startswith("OTC_W_") or "NON" in code or code.startswith("OTC"):
                bucket = "non_ats"
            else:
                continue
            weekly.setdefault(str(week), {"ats": 0.0, "non_ats": 0.0})[bucket] += shares
        ordered = []
        for week, x in sorted(weekly.items()):
            total = x["ats"] + x["non_ats"]
            if total <= 0:
                continue
            ordered.append({"week": week, "ats_shares": x["ats"],
                            "non_ats_shares": x["non_ats"],
                            "ats_share_pct": x["ats"] / total * 100})
        if not ordered:
            out["error"] = "FINRA 周度记录缺少可计算的 ATS/Non-ATS 成交量"
            return out
        latest = ordered[-1]
        out["weeks"] = ordered[-12:]
        out["ats_share_pct"] = latest["ats_share_pct"]
        if len(ordered) >= 2:
            out["ats_share_change_pct"] = latest["ats_share_pct"] - ordered[-2]["ats_share_pct"]
        vals = [x["ats_shares"] for x in ordered[-8:]]
        if len(vals) >= 4:
            mu = sum(vals[:-1]) / max(1, len(vals)-1)
            sd = (sum((v-mu)**2 for v in vals[:-1]) / max(1, len(vals)-2)) ** 0.5 if len(vals) > 2 else 0
            out["ats_volume_z"] = (latest["ats_shares"] - mu) / sd if sd > 0 else 0.0
        out["available"] = True
        return out
    except Exception as exc:
        out["error"] = str(exc)[:180]
        return out


def _dark_expectation(ats, options, analyst):
    """Infer an expectation *gap* only from observable inputs.

    Direction is intentionally unknown when ATS data alone cannot identify
    whether trades were buys or sells. The result is a pressure/attention
    measure, not a directional trading signal.
    """
    if not ats.get("available"):
        return {"status": "unavailable", "data": {"score": None, "direction": "unknown"},
                "reason": ats.get("error") or "ATS数据不可用"}
    share = ats.get("ats_share_pct")
    change = ats.get("ats_share_change_pct")
    z = ats.get("ats_volume_z")
    # Activity/attention score: higher means stronger reported ATS activity,
    # not bullishness. Keep it bounded and fully decomposable.
    raw = 0.0
    parts = []
    if share is not None:
        p = max(-1.0, min(1.0, (share - 25.0) / 20.0))
        raw += p * 0.5; parts.append(("ATS占比", p, 0.5))
    if change is not None:
        p = max(-1.0, min(1.0, change / 5.0))
        raw += p * 0.3; parts.append(("ATS占比变化", p, 0.3))
    if z is not None:
        p = max(-1.0, min(1.0, z / 3.0))
        raw += p * 0.2; parts.append(("ATS成交异常度", p, 0.2))
    score = round(50 + 50 * raw, 1)
    return {"status": "inferred", "data": {
        "activity_score": score, "direction": "unknown",
        "ats_share_pct": share, "ats_share_change_pct": change,
        "ats_volume_z": z,
        "decomposition": [{"factor": n, "normalized": round(v, 3), "weight": w} for n,v,w in parts],
        "interpretation": "分数越高代表报告的ATS活动/异常度越高，不代表买入或卖出方向。"
    }, "reason": "由FINRA报告的ATS/Non-ATS周度成交痕迹推断；不是实时暗池订单簿。"}


def _analyst_source(symbol):
    out = {"available": False, "target_mean": None, "target_median": None,
           "recommendation_mean": None, "number_of_analysts": None, "cusip": None,
           "target_low": None, "target_high": None, "error": None}
    try:
        info = yf.Ticker(symbol).info or {}
        out["target_mean"] = _finite(info.get("targetMeanPrice"))
        out["target_median"] = _finite(info.get("targetMedianPrice"))
        out["target_low"] = _finite(info.get("targetLowPrice"))
        out["target_high"] = _finite(info.get("targetHighPrice"))
        out["cusip"] = info.get("cusip") or info.get("cusipNumber")
        out["recommendation_mean"] = _finite(info.get("recommendationMean"))
        out["number_of_analysts"] = _finite(info.get("numberOfAnalystOpinions"))
        out["available"] = any(out[k] is not None for k in ("target_mean", "target_median", "recommendation_mean"))
    except Exception as exc:
        out["error"] = str(exc)[:180]
    return out


def _options_source(symbol):
    out = {"available": False, "implied_move_pct": None, "atm_iv_pct": None,
           "put_call_oi_ratio": None, "error": None}
    try:
        t = yf.Ticker(symbol)
        expiries = list(t.options or [])
        if not expiries:
            out["error"] = "期权到期日不可用"
            return out
        # Keep this bounded: nearest expiry only, one chain request per call.
        exp = expiries[0]
        chain = t.option_chain(exp)
        calls, puts = chain.calls, chain.puts
        if calls is None or calls.empty or puts is None or puts.empty:
            out["error"] = "期权链为空"
            return out
        px = _finite((t.fast_info or {}).get("last_price"))
        if px is None:
            px = _finite(t.info.get("currentPrice"))
        if px is None:
            out["error"] = "标的现价不可用"
            return out
        calls = calls.assign(dist=(pd.to_numeric(calls["strike"], errors="coerce")-px).abs()).sort_values("dist")
        puts = puts.assign(dist=(pd.to_numeric(puts["strike"], errors="coerce")-px).abs()).sort_values("dist")
        c = calls.iloc[0]; p = puts.iloc[0]
        civ = _finite(c.get("impliedVolatility")); piv = _finite(p.get("impliedVolatility"))
        ivs = [x for x in (civ, piv) if x is not None and x > 0]
        if ivs:
            iv = sum(ivs)/len(ivs)
            out["atm_iv_pct"] = iv * 100
            # Approximate one-expiry expected move using IV * sqrt(T). This is a
            # market-implied statistic, not a forecast; use calendar days.
            try:
                dte = max(1, (pd.Timestamp(exp) - pd.Timestamp.utcnow().tz_localize(None)).days)
            except Exception:
                dte = 1
            out["implied_move_pct"] = iv * math.sqrt(dte/365) * 100
        coi = _finite(c.get("openInterest")) or 0
        poi = _finite(p.get("openInterest")) or 0
        if coi > 0:
            out["put_call_oi_ratio"] = poi / coi
        out["available"] = any(out[k] is not None for k in ("implied_move_pct", "atm_iv_pct", "put_call_oi_ratio"))
    except Exception as exc:
        out["error"] = str(exc)[:180]
    return out



def _crypto_onchain_source(symbol):
    """Optional crypto on-chain dark-flow evidence.

    This is deliberately limited to observable exchange/entity flows. It does
    not claim to see private OTC orders or identify the owner of an address.
    CryptoQuant access is optional and isolated behind CRYPTOQUANT_API_KEY.
    """
    out = {"available": False, "asset": symbol, "direction": "unknown",
           "netflow_latest": None, "netflow_7d_avg": None,
           "reserve_change_7d_pct": None, "whale_inflow_ratio_pct": None,
           "activity_score": None, "error": None,
           "source": "CryptoQuant on-chain exchange/entity flows"}
    token = os.getenv("CRYPTOQUANT_API_KEY", "").strip()
    if not token:
        out["error"] = "未配置 CRYPTOQUANT_API_KEY；链上暗盘仅在接入可验证链上数据源后显示"
        return out
    asset = {"BTCUSD":"btc", "ETHUSD":"eth", "SOLUSD":"sol"}.get(symbol.upper())
    if not asset:
        out["error"] = "当前链上暗盘仅对支持的加密资产启用；商品/股票不适用"
        return out
    base = "https://api.cryptoquant.com/v1"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_metric(path, params):
        r = requests.get(base + path, headers=headers, params=params, timeout=3.0)
        r.raise_for_status()
        data = r.json() or {}
        if _finite(data.get("status", {}).get("code")) not in (None, 200):
            raise RuntimeError(str(data.get("status", {}).get("message") or "CryptoQuant返回错误"))
        return (data.get("result") or {}).get("data") or []

    try:
        # Keep to three small daily series. No block-level fan-out in the Pro page.
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_net = ex.submit(get_metric, f"/{asset}/exchange-flows/netflow", {"exchange":"all_exchange","window":"day","limit":14})
            f_res = ex.submit(get_metric, f"/{asset}/exchange-flows/reserve", {"exchange":"all_exchange","window":"day","limit":14})
            f_in = ex.submit(get_metric, f"/{asset}/exchange-flows/inflow", {"exchange":"all_exchange","window":"day","limit":14})
            net = f_net.result(); reserve = f_res.result(); inflow = f_in.result()
        net_vals=[_finite(x.get("netflow_total")) for x in net if _finite(x.get("netflow_total")) is not None]
        if net_vals:
            out["netflow_latest"] = net_vals[-1]
            out["netflow_7d_avg"] = sum(net_vals[-7:]) / min(7, len(net_vals))
        res_vals=[_finite(x.get("reserve")) for x in reserve if _finite(x.get("reserve")) is not None]
        if len(res_vals)>=2 and res_vals[0] not in (None,0):
            # API returns oldest -> newest in normal usage; compare last 7 points.
            old=res_vals[max(0,len(res_vals)-7)]
            new=res_vals[-1]
            if old:
                out["reserve_change_7d_pct"]=(new/old-1)*100
        latest_in=inflow[-1] if inflow else {}
        it=_finite(latest_in.get("inflow_total")); top=_finite(latest_in.get("inflow_top10"))
        if it and it>0 and top is not None:
            out["whale_inflow_ratio_pct"] = top/it*100

        # Evidence score is activity/pressure, not a buy/sell oracle.
        parts=[]; raw=0.0
        if out["whale_inflow_ratio_pct"] is not None:
            p=max(0,min(1,out["whale_inflow_ratio_pct"]/60.0)); raw += p*0.4; parts.append(("Top10大额流入占比",p,0.4))
        if out["netflow_latest"] is not None and out["netflow_7d_avg"] is not None:
            base_abs=max(abs(out["netflow_7d_avg"]),1e-9); p=max(-1,min(1,out["netflow_latest"]/base_abs)); raw += ((p+1)/2)*0.35; parts.append(("最新净流入相对7日均值",(p+1)/2,0.35))
        if out["reserve_change_7d_pct"] is not None:
            p=max(-1,min(1,out["reserve_change_7d_pct"]/5.0)); raw += ((-p+1)/2)*0.25; parts.append(("交易所储备变化",(-p+1)/2,0.25))
        out["activity_score"]=round(raw*100,1) if parts else None
        if out["netflow_latest"] is not None and out["reserve_change_7d_pct"] is not None:
            if out["netflow_latest"] < 0 and out["reserve_change_7d_pct"] < 0:
                out["direction"]="偏积累"
            elif out["netflow_latest"] > 0 and (out["whale_inflow_ratio_pct"] or 0) >= 30:
                out["direction"]="偏分配"
            else:
                out["direction"]="混合/中性"
        out["decomposition"]=[{"factor":n,"normalized":round(v,3),"weight":w} for n,v,w in parts]
        out["available"]=bool(net_vals or res_vals or inflow)
        return out
    except Exception as exc:
        out["error"]=str(exc)[:180]
        return out


def _next_earnings_dark_source(symbol, ats=None):
    """Infer *buy-side/event positioning* around the next earnings event.

    IMPORTANT: this function deliberately excludes sell-side analyst targets,
    EPS consensus revisions, and analyst recommendation fields. It is a proxy
    for observable market positioning before the next report, built from:
    event-dated options, call/put activity, IV skew, pre-event price/volume,
    and (when available) FINRA ATS activity. It never claims access to private
    buy-side books or investment-committee expectations.
    """
    out={"available":False,"status":"unavailable","next_earnings_date":None,
         "event_implied_move_pct":None,"historical_abs_move_median_pct":None,
         "implied_vs_history_ratio":None,"atm_iv_pct":None,
         "call_put_volume_ratio":None,"put_call_oi_ratio":None,
         "otm_skew_proxy_pct":None,"call_volume_oi_ratio":None,
         "put_volume_oi_ratio":None,"pre_event_momentum_5d_pct":None,
         "pre_event_volume_ratio":None,"ats_share_pct":None,
         "ats_share_change_pct":None,"expectation_score":None,
         "positioning_bias":"混合/中性","decomposition":[],"error":None,
         "source":"event-dated listed options + price/volume + optional FINRA ATS"}
    try:
        t=yf.Ticker(symbol)
        dates=t.get_earnings_dates(limit=16)
        if dates is None or dates.empty:
            out["error"]="无法取得下一份财报日期"
            return out
        now=pd.Timestamp.now(tz="UTC")
        future=[]
        past=[]
        for idx,row in dates.iterrows():
            try: dt=pd.Timestamp(idx)
            except Exception: continue
            if dt.tzinfo is None: dt=dt.tz_localize("UTC")
            else: dt=dt.tz_convert("UTC")
            (future if dt>now else past).append((dt,row))
        if not future:
            out["error"]="当前没有可验证的下一份未来财报日期"
            return out
        event_dt,_=future[0]
        out["next_earnings_date"]=event_dt.isoformat()

        # One bounded historical request; never blocks the core Lite path.
        h=t.history(period="1y",interval="1d",auto_adjust=False)
        if h is not None and not h.empty:
            close=pd.to_numeric(h.get("Close"),errors="coerce").dropna()
            vol=pd.to_numeric(h.get("Volume"),errors="coerce").reindex(close.index).fillna(0)
            if len(close)>=30:
                out["pre_event_momentum_5d_pct"]=(float(close.iloc[-1])/float(close.iloc[-6])-1)*100
                base=float(vol.tail(60).head(max(1,min(40,len(vol)-20))).mean())
                cur=float(vol.tail(20).mean())
                if base>0: out["pre_event_volume_ratio"]=cur/base

        # Historical post-earnings absolute move, only from observable prices.
        try:
            moves=[]
            d0=close.index.tz_localize(None) if getattr(close.index,"tz",None) is not None else close.index
            for dt,_ in past[:10]:
                dn=dt.tz_convert(None)
                pos=int(d0.searchsorted(dn))
                if pos<1 or pos>=len(close): continue
                before=float(close.iloc[pos-1]); after=float(close.iloc[pos])
                if before: moves.append(abs(after/before-1)*100)
            if moves: out["historical_abs_move_median_pct"]=float(pd.Series(moves).median())
        except Exception:
            pass

        # Choose the first listed expiry on/after the event date. This is the
        # critical difference from generic options data: we study the actual
        # earnings event window, not the nearest weekly expiry.
        exps=list(t.options or [])
        chosen=None
        for ex in exps:
            try:
                ed=pd.Timestamp(ex).tz_localize("UTC")
                if ed>=event_dt: chosen=ex; break
            except Exception: continue
        if chosen is None and exps:
            chosen=exps[0]
        if chosen:
            chain=t.option_chain(chosen); calls=chain.calls; puts=chain.puts
            px=_finite((t.fast_info or {}).get("last_price"))
            if px is None and h is not None and not h.empty: px=float(close.iloc[-1])
            if px and calls is not None and puts is not None and not calls.empty and not puts.empty:
                for df in (calls,puts):
                    for col in ("volume","openInterest","impliedVolatility","strike"):
                        if col in df.columns: df[col]=pd.to_numeric(df[col],errors="coerce")
                cv=float(calls["volume"].fillna(0).sum()); pv=float(puts["volume"].fillna(0).sum())
                co=float(calls["openInterest"].fillna(0).sum()); po=float(puts["openInterest"].fillna(0).sum())
                if pv>0: out["call_put_volume_ratio"]=cv/pv
                if co>0: out["put_call_oi_ratio"]=po/co
                if co>0: out["call_volume_oi_ratio"]=cv/co
                if po>0: out["put_volume_oi_ratio"]=pv/po

                calls["dist"]=(calls["strike"]-px).abs(); puts["dist"]=(puts["strike"]-px).abs()
                c_atm=calls.sort_values("dist").iloc[0]; p_atm=puts.sort_values("dist").iloc[0]
                ivs=[_finite(c_atm.get("impliedVolatility")),_finite(p_atm.get("impliedVolatility"))]
                ivs=[v for v in ivs if v and v>0]
                if ivs:
                    iv=sum(ivs)/len(ivs); out["atm_iv_pct"]=iv*100
                    try:
                        dte=max(1,(pd.Timestamp(chosen)-now.tz_convert(None)).days)
                    except Exception: dte=1
                    out["event_implied_move_pct"]=iv*math.sqrt(dte/365)*100
                # 5% OTM IV skew proxy. This is deliberately called a proxy,
                # because free chains do not reliably expose option delta.
                c5=calls.iloc[(calls["strike"]-px*1.05).abs().argsort()[:1]]
                p5=puts.iloc[(puts["strike"]-px*0.95).abs().argsort()[:1]]
                if len(c5) and len(p5):
                    civ=_finite(c5.iloc[0].get("impliedVolatility")); piv=_finite(p5.iloc[0].get("impliedVolatility"))
                    if civ is not None and piv is not None: out["otm_skew_proxy_pct"]=(piv-civ)*100

        if ats and ats.get("available"):
            out["ats_share_pct"]=ats.get("ats_share_pct")
            out["ats_share_change_pct"]=ats.get("ats_share_change_pct")

        if out["historical_abs_move_median_pct"] and out["event_implied_move_pct"]:
            out["implied_vs_history_ratio"]=out["event_implied_move_pct"]/out["historical_abs_move_median_pct"]

        # Buy-side/event positioning score. No analyst or EPS-consensus inputs.
        parts=[]; score=50.0
        def add(name,val,weight):
            nonlocal score
            if val is None:return
            p=max(-1,min(1,val)); score += p*weight; parts.append((name,p,weight))
        # Call volume pressure: ratio 1 means neutral, 2+ increasingly call-heavy.
        if out["call_put_volume_ratio"] is not None:
            r=out["call_put_volume_ratio"]; add("财报期Call/Put成交压力", max(-1,min(1,(r-1)/1.0)), 25)
        if out["otm_skew_proxy_pct"] is not None:
            # negative put-minus-call skew => relatively stronger call demand.
            add("5%OTM IV偏斜代理", max(-1,min(1,-out["otm_skew_proxy_pct"]/15.0)), 20)
        if out["pre_event_momentum_5d_pct"] is not None:
            add("财报前5D价格动量", max(-1,min(1,out["pre_event_momentum_5d_pct"]/10.0)), 15)
        if out["pre_event_volume_ratio"] is not None:
            # Volume acceleration is attention, not direction; center it at 1x.
            add("财报前成交量异常", max(-1,min(1,(out["pre_event_volume_ratio"]-1)/1.5)), 10)
        if out["ats_share_change_pct"] is not None:
            # ATS share change = attention/off-exchange activity, not direction.
            add("ATS活动变化", max(-1,min(1,out["ats_share_change_pct"]/5.0)), 10)
        if out["implied_vs_history_ratio"] is not None:
            # Magnitude only; never converts high implied move into bullishness.
            parts.append(("财报隐含/历史波动", max(-1,min(1,(out["implied_vs_history_ratio"]-1))), 20))
        out["expectation_score"]=round(max(0,min(100,score)),1) if parts else None
        bullish=sum(w for n,v,w in parts if v>0.25 and n not in ("财报隐含/历史波动","财报前成交量异常"))
        bearish=sum(w for n,v,w in parts if v<-0.25 and n not in ("财报隐含/历史波动","财报前成交量异常"))
        out["positioning_bias"]="偏正向" if bullish>=bearish+15 else ("偏负向" if bearish>=bullish+15 else "混合/中性")
        out["decomposition"]=[{"factor":n,"normalized":round(v,3),"weight":w} for n,v,w in parts]
        out["available"]=bool(out["next_earnings_date"] and any(out[k] is not None for k in ("event_implied_move_pct","call_put_volume_ratio","otm_skew_proxy_pct","pre_event_momentum_5d_pct")))
        out["status"]="inferred" if out["available"] else "unavailable"
        return out
    except Exception as exc:
        out["error"]=str(exc)[:180]
        return out



# ---------------------------------------------------------------------------
# Free/public evidence sources
# ---------------------------------------------------------------------------
_SEC_13F_URL = os.getenv(
    "AEL_SEC_13F_URL",
    "https://dcm.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip",
)
_SEC_13F_CACHE = os.getenv("AEL_SEC_13F_CACHE", "/tmp/ael_13f_2026_jun_aug.zip")
_SEC_13F_LOCK = threading.Lock()


def _yahoo_earnings_revision_source(symbol):
    """Use Yahoo/yfinance's public earnings-trend table when available.

    This is a *revision/consensus observation*, not a private buy-side forecast.
    It is deliberately separate from the dark-expectation score.
    """
    out = {"available": False, "rows": [], "current_eps": None,
           "eps_revision_7d_pct": None, "eps_revision_30d_pct": None,
           "eps_revision_90d_pct": None, "analyst_count": None,
           "next_earnings_date": None, "error": None,
           "source": "Yahoo Finance / yfinance earnings trend"}
    try:
        t = yf.Ticker(symbol)
        # yfinance exposes earnings_trend as a DataFrame on versions that
        # support the endpoint. Keep this defensive because Yahoo occasionally
        # changes the response shape.
        trend = getattr(t, "earnings_trend", None)
        if trend is not None and hasattr(trend, "copy"):
            df = trend.copy()
            if not df.empty:
                if "period" in df.columns:
                    df = df.reset_index(drop=True)
                for _, row in df.iterrows():
                    period = str(row.get("period") or row.get("index") or "")
                    cur = _finite(row.get("current"))
                    c7 = _finite(row.get("7daysAgo"))
                    c30 = _finite(row.get("30daysAgo"))
                    c90 = _finite(row.get("90daysAgo"))
                    analysts = _finite(row.get("numberOfAnalysts"))
                    if cur is not None:
                        rec = {"period": period, "current": cur,
                               "7daysAgo": c7, "30daysAgo": c30,
                               "90daysAgo": c90, "numberOfAnalysts": analysts}
                        out["rows"].append(rec)
                        if not out["current_eps"] and period in ("0q", "+0q", "0y", "+0y"):
                            out["current_eps"] = cur
                        if out["analyst_count"] is None and analysts is not None:
                            out["analyst_count"] = analysts
                        if period in ("0q", "+0q"):
                            for key, base in (("eps_revision_7d_pct", c7), ("eps_revision_30d_pct", c30), ("eps_revision_90d_pct", c90)):
                                if cur is not None and base not in (None, 0):
                                    out[key] = (cur / base - 1) * 100
        # Earnings dates are also useful if the trend endpoint is sparse.
        dates = t.get_earnings_dates(limit=8)
        if dates is not None and not dates.empty:
            now = pd.Timestamp.now(tz="UTC")
            future = []
            for idx in dates.index:
                try:
                    dt = pd.Timestamp(idx)
                    dt = dt.tz_localize("UTC") if dt.tzinfo is None else dt.tz_convert("UTC")
                    if dt > now:
                        future.append(dt)
                except Exception:
                    continue
            if future:
                out["next_earnings_date"] = min(future).isoformat()
        out["available"] = bool(out["rows"] or out["next_earnings_date"])
        return out
    except Exception as exc:
        out["error"] = str(exc)[:180]
        return out


def _alphavantage_earnings_estimates_source(symbol):
    """Optional free-key Alpha Vantage consensus estimates.

    Alpha Vantage offers EARNINGS_ESTIMATES with a free API key subject to its
    free-tier limits. The key is optional; failure never blocks the Pro page.
    """
    out = {"available": False, "estimates": [], "error": None,
           "source": "Alpha Vantage EARNINGS_ESTIMATES (optional free key)"}
    key = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
    if not key:
        out["error"] = "未配置 ALPHAVANTAGE_API_KEY；已使用 Yahoo 免费公开数据替代"
        return out
    try:
        r = requests.get("https://www.alphavantage.co/query", params={
            "function": "EARNINGS_ESTIMATES", "symbol": symbol, "apikey": key,
        }, timeout=3.5, headers={"User-Agent": "AEL/2.5 Pro"})
        r.raise_for_status(); d = r.json() or {}
        if d.get("Note") or d.get("Information"):
            out["error"] = str(d.get("Note") or d.get("Information"))[:180]
            return out
        rows = d.get("estimates") or d.get("data") or []
        if isinstance(rows, list):
            for x in rows[:12]:
                if not isinstance(x, dict):
                    continue
                out["estimates"].append({k: x.get(k) for k in (
                    "symbol", "horizon", "fiscalDateEnding", "epsAvg", "epsHigh", "epsLow",
                    "revenueAvg", "revenueHigh", "revenueLow", "analystCount", "growth"
                ) if k in x})
        out["available"] = bool(out["estimates"])
        return out
    except Exception as exc:
        out["error"] = str(exc)[:180]
        return out


def _finnhub_free_earnings_source(symbol):
    """Optional Finnhub free-tier earnings calendar/surprise evidence."""
    out = {"available": False, "next_earnings_date": None,
           "eps_surprises": [], "revenue_surprises": [], "error": None,
           "source": "Finnhub free earnings calendar / earnings surprises"}
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        out["error"] = "未配置 FINNHUB_API_KEY；该免费扩展保持关闭"
        return out
    try:
        today = pd.Timestamp.now(tz="UTC").date()
        start = (today - pd.Timedelta(days=365)).isoformat()
        end = (today + pd.Timedelta(days=45)).isoformat()
        base = "https://finnhub.io/api/v1"
        cal = requests.get(base + "/calendar/earnings", params={
            "from": start, "to": end, "symbol": symbol, "international": "false", "token": key,
        }, timeout=3.5, headers={"User-Agent": "AEL/2.5 Pro"})
        cal.raise_for_status(); cd = cal.json() or {}
        events = cd.get("earningsCalendar") or []
        future = [x for x in events if str(x.get("date") or "") >= today.isoformat()]
        if future:
            out["next_earnings_date"] = sorted(future, key=lambda x: str(x.get("date")))[0].get("date")
        hist = requests.get(base + "/stock/earnings", params={"symbol": symbol, "limit": 8, "token": key},
                            timeout=3.5, headers={"User-Agent": "AEL/2.5 Pro"})
        hist.raise_for_status(); hd = hist.json() or []
        if isinstance(hd, list):
            for x in hd[:8]:
                if x.get("surprisePercent") is not None:
                    out["eps_surprises"].append({"period": x.get("period"), "surprise_pct": x.get("surprisePercent"), "actual": x.get("actual"), "estimate": x.get("estimate")})
        out["available"] = bool(out["next_earnings_date"] or out["eps_surprises"])
        return out
    except Exception as exc:
        out["error"] = str(exc)[:180]
        return out


def _sec_13f_source(symbol, cusip=None):
    """Free SEC 13F quarterly evidence, lazily cached.

    SEC publishes flattened 13F datasets quarterly. We download the latest
    dataset only on-demand, cache it locally, and scan the infotable for the
    requested CUSIP. This is intentionally labeled quarterly/lagged evidence;
    it is not real-time institutional positioning.
    """
    out = {"available": False, "report_date": None, "holder_count": None,
           "total_value_usd": None, "top_holders": [], "change": None,
           "error": None, "source": "SEC Form 13F quarterly dataset"}
    if not cusip:
        out["error"] = "缺少 CUSIP，无法从 SEC 13F 数据集精确匹配证券"
        return out
    target = re.sub(r"[^0-9A-Za-z]", "", str(cusip)).upper()
    if not target:
        out["error"] = "CUSIP 无效"
        return out
    cache_path = Path(_SEC_13F_CACHE)
    try:
        with _SEC_13F_LOCK:
            if not cache_path.exists() or cache_path.stat().st_size < 1_000_000:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_suffix(".tmp")
                with requests.get(_SEC_13F_URL, stream=True, timeout=(4, 25), headers={
                    "User-Agent": "AEL research contact@example.com",
                    "Accept": "application/zip,*/*",
                }) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        total = 0
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > 140 * 1024 * 1024:
                                raise RuntimeError("SEC 13F 数据集超过安全下载上限")
                            f.write(chunk)
                tmp.replace(cache_path)
        rows = []
        with zipfile.ZipFile(cache_path, "r") as zf:
            names = zf.namelist()
            info_name = next((n for n in names if "infotable" in n.lower() and n.lower().endswith((".tsv", ".csv"))), None)
            if not info_name:
                out["error"] = "SEC 13F ZIP 未找到 infotable 数据文件"
                return out
            with zf.open(info_name) as fh:
                sep = "\t" if info_name.lower().endswith(".tsv") else ","
                for chunk in pd.read_csv(fh, sep=sep, dtype=str, chunksize=120_000, on_bad_lines="skip", low_memory=False):
                    norm = {c: re.sub(r"[^a-z0-9]", "", str(c).lower()) for c in chunk.columns}
                    cus_col = next((c for c,n in norm.items() if n in ("cusip", "cusipnumber")), None)
                    if not cus_col:
                        continue
                    m = chunk[cus_col].astype(str).str.replace(r"[^0-9A-Za-z]", "", regex=True).str.upper() == target
                    if m.any():
                        rows.append(chunk.loc[m].copy())
        if not rows:
            out["error"] = "最新 SEC 13F 季度数据集中未找到该 CUSIP"
            return out
        df = pd.concat(rows, ignore_index=True)
        norm = {c: re.sub(r"[^a-z0-9]", "", str(c).lower()) for c in df.columns}
        def col(*names):
            wanted=set(names)
            return next((c for c,n in norm.items() if n in wanted), None)
        holder_col=col("filingmanagername", "managername", "nameoffilingmanager")
        value_col=col("value", "marketvalue")
        shares_col=col("sshprnamt", "shares")
        date_col=col("reportdate", "filingdate")
        issuer_col=col("nameofissuer", "issuername")
        if value_col:
            df["_value"] = pd.to_numeric(df[value_col], errors="coerce")
            # SEC 13F value is reported in thousands of dollars.
            df["_value_usd"] = df["_value"] * 1000
        else:
            df["_value_usd"] = None
        if date_col:
            dates=pd.to_datetime(df[date_col], errors="coerce")
            out["report_date"] = dates.max().date().isoformat() if dates.notna().any() else None
        out["holder_count"] = int(df[holder_col].nunique()) if holder_col else int(len(df))
        out["total_value_usd"] = float(df["_value_usd"].sum()) if df["_value_usd"].notna().any() else None
        if holder_col:
            top=df.sort_values("_value_usd", ascending=False).head(10)
            out["top_holders"]=[{"holder": str(r.get(holder_col) or "未知"),
                                  "value_usd": _finite(r.get("_value_usd")),
                                  "shares": _finite(r.get(shares_col)) if shares_col else None,
                                  "issuer": str(r.get(issuer_col) or "") if issuer_col else ""} for _,r in top.iterrows()]
        out["available"] = bool(out["holder_count"])
        return out
    except Exception as exc:
        out["error"] = str(exc)[:180]
        return out

def _xstock_source(symbol):
    """Optional public xStocks metadata for an on-chain US equity token.

    Public endpoint; no API key required. This is metadata validation only and
    is isolated from the underlying US-equity research chain.
    """
    out={"available":False,"symbol":symbol,"underlying_symbol":None,
         "name":None,"logo":None,"trading_halted":None,"error":None,
         "source":"xStocks public Assets API"}
    try:
        r=requests.get(f"https://api.xstocks.fi/api/v2/public/assets/{requests.utils.quote(symbol, safe='')}", timeout=2.5,
                       headers={"User-Agent":"AEL/2.5 Pro Onchain Equity"})
        if r.status_code==404:
            out["error"]="该链上美股资产不存在或未公开"
            return out
        r.raise_for_status(); d=r.json() or {}
        # v2 public assets may return the asset directly or wrap it in data/node.
        node=d.get("data") if isinstance(d.get("data"),dict) else (d.get("node") if isinstance(d.get("node"),dict) else d)
        under=node.get("underlying") if isinstance(node.get("underlying"),dict) else {}
        out["symbol"]=node.get("symbol") or symbol
        out["name"]=node.get("name")
        out["logo"]=node.get("logo") or node.get("logoUrl")
        out["underlying_symbol"]=under.get("symbol") or under.get("ticker") or node.get("underlyingSymbol") or node.get("underlyingTicker")
        out["trading_halted"]=node.get("isTradingHalted")
        out["listing_country"] = under.get("listingCountry")
        out["available"]=bool(out["underlying_symbol"])
        return out
    except Exception as exc:
        out["error"]=str(exc)[:180]
        return out

def analyze_expectation(symbol: str):
    requested_symbol = str(symbol or "").strip().upper()
    if not requested_symbol:
        return {"ok": False, "symbol": requested_symbol, "error": "缺少标的"}
    onchain_equity = {"available":False,"symbol":requested_symbol}
    # xStocks uses an x-suffixed token symbol such as AAPLx. If the user
    # searches the on-chain version, research the underlying stock. If the user
    # searches the normal US ticker, the xStock metadata is discovered in
    # parallel so the extension card can still verify AAPL -> AAPLx without
    # adding latency to the stock core/Lite chain.
    if requested_symbol.endswith("X") and len(requested_symbol)>1:
        token_symbol = requested_symbol[:-1] + "x"
        base_symbol = requested_symbol[:-1]
        symbol = base_symbol.upper()
    else:
        token_symbol = (requested_symbol + "x") if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", requested_symbol) and not requested_symbol.endswith((".HK",".SS",".SZ")) else None
        symbol = requested_symbol
    now = time()
    with _LOCK:
        cached = _CACHE.get(symbol)
        if cached and now - cached[0] < TTL:
            return cached[1]

    h, hist_err = _safe_history(symbol)
    if h is None:
        return {"ok": False, "symbol": symbol, "error": hist_err or "暂无足够历史数据",
                "data_quality": {"primary_history": False}}

    # Optional sources are parallel and independently fail-safe.
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {
            ex.submit(_analyst_source, symbol): "analyst",
            ex.submit(_options_source, symbol): "options",
            ex.submit(_finra_ats_source, symbol): "ats_dark_pool",
            ex.submit(_next_earnings_dark_source, symbol): "next_earnings_dark",
            ex.submit(_crypto_onchain_source, symbol): "onchain_dark",
            ex.submit(_yahoo_earnings_revision_source, symbol): "earnings_revision_yahoo",
            ex.submit(_alphavantage_earnings_estimates_source, symbol): "earnings_revision_av",
            ex.submit(_finnhub_free_earnings_source, symbol): "earnings_revision_finnhub",
        }
        if token_symbol:
            futs[ex.submit(_xstock_source, token_symbol)] = "onchain_equity"
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                results[k] = fut.result()
            except Exception as exc:
                results[k] = {"available": False, "error": str(exc)[:180]}

    price = _price_signal(h)
    analyst = results.get("analyst", {"available": False})
    options = results.get("options", {"available": False})
    ats = results.get("ats_dark_pool", {"available": False})
    next_earnings = results.get("next_earnings_dark", {"available": False, "status":"unavailable"})
    earnings_yahoo = results.get("earnings_revision_yahoo", {"available": False})
    earnings_av = results.get("earnings_revision_av", {"available": False})
    earnings_finnhub = results.get("earnings_revision_finnhub", {"available": False})
    sec_13f = _sec_13f_source(symbol, analyst.get("cusip"))
    onchain = results.get("onchain_dark", {"available": False, "status":"unavailable"})
    if token_symbol:
        onchain_equity = results.get("onchain_equity", onchain_equity)
    dark = _dark_expectation(ats, options, analyst)
    current = _finite(price.get("price")) if price else None
    target = _finite(analyst.get("target_mean"))
    target_gap = ((target/current)-1)*100 if current and target else None

    # Explicitly keep unavailable institutional sources separate. No proxy is
    # presented as actual dark-pool/ATS/13F/CFTC observation.
    sources = {
        "earnings_revision": {
            "status": "observed" if any(x.get("available") for x in (earnings_yahoo, earnings_av, earnings_finnhub)) else "unavailable",
            "data": {"yahoo": earnings_yahoo if earnings_yahoo.get("available") else {},
                     "alphavantage": earnings_av if earnings_av.get("available") else {},
                     "finnhub": earnings_finnhub if earnings_finnhub.get("available") else {}},
            "reason": "Yahoo 免费公开盈利趋势；可选 Alpha Vantage/Finnhub 免费 API 增强。"
                      if any(x.get("available") for x in (earnings_yahoo, earnings_av, earnings_finnhub))
                      else "未取得盈利预测趋势数据"},
        "ats_dark_pool": {"status": "observed" if ats.get("available") else "unavailable", "data": ats if ats.get("available") else {}, "reason": ats.get("error") or "FINRA ATS数据可用"},
        "dark_expectation": dark,
        "institutional_13f": {"status": "observed" if sec_13f.get("available") else "unavailable",
                              "data": sec_13f if sec_13f.get("available") else {},
                              "reason": sec_13f.get("error") or "SEC 13F 最新季度披露；存在报告滞后，不代表实时仓位"},
        "cftc_positioning": {"status": "not_applicable" if not str(symbol).upper().endswith("=F") else "unavailable",
                                "reason": "CFTC COT 仅适用于相应期货品种；股票不适用" if not str(symbol).upper().endswith("=F") else "该期货品种的 COT 源需按合约映射"},
        "onchain_dark": {"status": "observed" if onchain.get("available") else "unavailable", "data": onchain if onchain.get("available") else {}, "reason": onchain.get("error") or "链上交易所/大额流量数据可用"},
        "next_earnings_dark": {"status": next_earnings.get("status", "unavailable"), "data": next_earnings if next_earnings.get("available") else {}, "reason": next_earnings.get("error") or "下一份财报的买方事件预期推断可用"},
        "onchain_equity": {"status": "observed" if onchain_equity.get("available") else "unavailable", "data": onchain_equity if onchain_equity.get("available") else {}, "reason": onchain_equity.get("error") or "xStocks公开资产元数据可用"},
        "analyst": {"status": "observed" if analyst.get("available") else "unavailable", "data": analyst},
        "options": {"status": "observed" if options.get("available") else "unavailable", "data": options},
        "price_volume": {"status": "observed", "data": price},
    }
    available = [k for k,v in sources.items() if v.get("status") in ("observed", "inferred")]
    out = {
        "ok": True, "symbol": symbol, "requested_symbol": requested_symbol, "as_of": pd.Timestamp.utcnow().isoformat(),
        "current_price": current,
        "analyst_target_gap_pct": target_gap,
        "sources": sources,
        "observed_count": len(available),
        "inferred_count": sum(1 for v in sources.values() if v.get("status") == "inferred"),
        "data_quality": {
            "primary_history": True,
            "optional_sources": {k:v.get("status") for k,v in sources.items()},
            "errors": {k:v.get("data",{}).get("error") for k,v in sources.items() if isinstance(v.get("data"), dict) and v.get("data",{}).get("error")},
        },
        "method_note": "AEL Pro 的买方暗盘预期只使用公开可验证痕迹：上市期权、价格/成交量、财报事件日期、可用 FINRA ATS、SEC 13F 季度披露；卖方目标价/评级与盈利预测只作为独立参考，不进入暗盘核心分数。SEC 13F 是季度滞后披露，FINRA ATS 是周度聚合且需要授权令牌；两者都不等同私人买方订单簿。加密资产链上卡使用可验证公开链上/市场数据，失败只影响本卡。",
    }
    with _LOCK:
        _CACHE[symbol] = (now, out)
    return out
