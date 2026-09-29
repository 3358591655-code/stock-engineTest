"""Guidance Price-In Fair Backtest sidecar.

The fair backtest is NOT an AEL-vs-ATS winner score. It asks the correct
question: after management speaks, how much of the management-intent delta
was already embedded in the market, and how well do the AEL and ATS versions
reconstruct the eventual reported result without look-ahead?

Historical options snapshots are not available in the free data chain, so the
original AEL Market-Implied options layer is never backfilled with today's
options. The historical AEL/ATS price-in replay therefore uses only verified
pre-event inputs available to the sidecar.
"""
from __future__ import annotations
from datetime import datetime, timezone
import math
import pandas as pd
try:
    import yfinance as yf
except Exception:
    yf = None

from pro_ats import ats_source
from pro_guidance_backtest import run_guidance_backtest
from pro_whisper_backtest import run_whisper_backtest

# Keep the import surface simple; duplicate the tiny replay helper rather than
# importing private functions from the older fair-backtest module.

def _finite(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except Exception: return None

def _clamp(x,lo,hi): return max(lo,min(hi,x))

def _pct(a,b):
    a,b=_finite(a),_finite(b)
    if a is None or b in (None,0): return None
    return (a/b-1)*100.0

def _error(p,a):
    p,a=_finite(p),_finite(a)
    if p is None or a in (None,0): return None
    return abs(p-a)/abs(a)*100.0


def _marketbeat_history(symbol):
    """Public quarterly consensus/actual cross-check from MarketBeat.
    Returns quarter-labeled revenue estimates/actuals when the public page is reachable.
    """
    import requests, re
    sym=str(symbol or '').strip().upper()
    if not sym or any(x in sym for x in ('.HK','.SS','.SZ')): return []
    headers={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36','Accept':'text/html,application/xhtml+xml'}
    text=None
    for ex in ('NASDAQ','NYSE','AMEX'):
        try:
            u=f'https://www.marketbeat.com/stocks/{ex}/{sym}/earnings/'
            r=requests.get(u,headers=headers,timeout=8)
            if r.ok and len(r.text)>5000:
                from pro_guidance_ats import _plain
                text=_plain(r.text); break
        except Exception: pass
    if not text:return []
    rows=[]
    pat=re.compile(r'(\d{1,2}/\d{1,2}/20\d{2})\s+(Q[1-4]\s+20\d{2})\s+\$?(-?\d+(?:\.\d+)?)\s+\$?(-?\d+(?:\.\d+)?)\s+\+?\$?(-?\d+(?:\.\d+)?)\s+\$?(-?\d+(?:\.\d+)?)\s+\$?(-?\d+(?:\.\d+)?)\s*B\s+\$?(-?\d+(?:\.\d+)?)\s*B',re.I)
    for m in pat.finditer(text):
        try:
            rows.append({'report_date':m.group(1),'quarter':m.group(2),'eps_consensus':float(m.group(3)),'eps_actual':float(m.group(4)),'revenue_consensus':float(m.group(7))*1e9,'revenue_actual':float(m.group(8))*1e9})
        except Exception: pass
    # More permissive row parser for pages where Beat/Miss or GAAP columns differ.
    if not rows:
        for m in re.finditer(r'(\d{1,2}/\d{1,2}/20\d{2})\s+(Q[1-4]\s+20\d{2})\s+([^\n]{0,220})',text,re.I):
            line=m.group(3)
            nums=re.findall(r'(-?\d+(?:\.\d+)?)\s*B',line)
            if len(nums)>=2:
                rows.append({'report_date':m.group(1),'quarter':m.group(2),'revenue_consensus':float(nums[-2])*1e9,'revenue_actual':float(nums[-1])*1e9})
    return rows


def _period_to_quarter_label(target_period, history_rows):
    try: end=pd.Timestamp(target_period)
    except Exception:return None
    # Match the target fiscal period to the closest reported quarter date using
    # the MarketBeat report date. This avoids assuming calendar-quarter ends.
    cand=[]
    for r in history_rows:
        try:
            rd=pd.Timestamp(r['report_date'])
            if rd> end and (rd-end).days<=70: cand.append((abs((rd-end).days),r['quarter']))
        except Exception: pass
    return min(cand)[1] if cand else None

def _normalize_close_series(close):
    """Normalize historical close index to tz-naive dates for safe event matching."""
    if close is None:
        return None
    try:
        idx = pd.DatetimeIndex(close.index)
        if idx.tz is not None:
            idx = idx.tz_convert(None)
        idx = idx.normalize()
        out = pd.Series(pd.to_numeric(close, errors="coerce").to_numpy(), index=idx)
        return out.dropna().sort_index()
    except Exception:
        return None

def _load_history_close(symbol, years=3):
    """Yahoo chart API first, yfinance fallback; return tz-naive daily closes."""
    import requests, time as _time
    sym=str(symbol or '').strip().upper()
    end=int(_time.time()); start=end-int(years*366*86400)
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={start}&period2={end}&interval=1d&events=history"
    headers={'User-Agent':'Mozilla/5.0 AEL/2.6.31'}
    try:
        r=requests.get(url,headers=headers,timeout=8)
        js=r.json() if r.ok else None
        res=((js or {}).get('chart') or {}).get('result') or []
        if res:
            q=res[0]; ts=q.get('timestamp') or []; vals=((q.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
            pairs=[]
            for t,v in zip(ts,vals):
                if v is not None:
                    pairs.append((pd.to_datetime(t,unit='s',utc=True).tz_convert(None).normalize(),float(v)))
            if pairs:
                return pd.Series([v for _,v in pairs],index=[d for d,_ in pairs]).sort_index()
    except Exception:
        pass
    try:
        if yf is not None:
            h=yf.Ticker(sym).history(period=f'{years}y',interval='1d',auto_adjust=False)
            if h is not None and not h.empty and 'Close' in h:
                return _normalize_close_series(h['Close'])
    except Exception:
        pass
    return None

def _historical_ats_signal(weeks,event_date,close):
    if not weeks:return None
    ed=pd.Timestamp(event_date).normalize(); eligible=[]
    for w in weeks:
        try:
            wt=pd.Timestamp(w.get('week')).normalize()
            if wt<=ed: eligible.append((wt,w))
        except Exception: pass
    if not eligible:return None
    wt,w=eligible[-1]; stale=(ed-wt).days
    if stale<0 or stale>28:return None
    hist=[_finite(x[1].get('ats_shares')) for x in eligible[:-1]]
    hist=[x for x in hist if x is not None and x>0][-8:]
    cur=_finite(w.get('ats_shares')); z=None
    if cur is not None and len(hist)>=4:
        mu=sum(hist)/len(hist); sd=(sum((x-mu)**2 for x in hist)/max(1,len(hist)-1))**0.5
        z=(cur-mu)/sd if sd>0 else 0.0
    prev=_finite(eligible[-2][1].get('ats_share_pct')) if len(eligible)>=2 else None
    share=_finite(w.get('ats_share_pct')); change=(share-prev) if share is not None and prev is not None else None
    parts=[]
    if z is not None: parts.append(_clamp(z/3,-1,1)*.60)
    if change is not None: parts.append(_clamp(change/5,-1,1)*.40)
    intensity=sum(parts)/sum([.60,.40][:len(parts)]) if parts else 0.0
    direction=0.0
    if close is not None and not close.empty:
        pre=close[close.index<=ed]
        if len(pre)>=21:
            p0=_finite(pre.iloc[-21]); p1=_finite(pre.iloc[-1])
            if p0 and p1: direction=_clamp((p1/p0-1)*100/12,-1,1)
    signed=direction*max(.25,abs(intensity)) if direction else 0.0
    score=50+50*_clamp(signed,-1,1)
    return {'week':wt.date().isoformat(),'staleness_days':stale,'score':score,'direction':'up' if signed>=.20 else 'down' if signed<=-.20 else 'neutral'}

def _ats_estimate(consensus,signal,kind):
    c=_finite(consensus); s=_finite((signal or {}).get('score'))
    if c is None or s is None:return None
    d=str((signal or {}).get('direction') or '')
    sign=1 if d=='up' else -1 if d=='down' else 0
    intensity=_clamp(abs(s-50)/50,0,1); cap=2.5 if kind=='revenue' else 4.0
    return c*(1+sign*intensity*cap/100)

def _pricein(base,intent,market):
    b,i,m=_finite(base),_finite(intent),_finite(market)
    if None in (b,i,m):return None
    delta=i-b
    if abs(delta)<max(abs(b)*.001,1e-12):return None
    return (m-b)/delta*100.0

def _match_whisper(actual, rows, call_date=None):
    if not rows:return None
    ar=_finite(actual.get('revenue')); ae=_finite(actual.get('eps')); cd=pd.Timestamp(call_date) if call_date else None
    cand=[]
    for r in rows:
        if cd is not None:
            try:
                ed=pd.Timestamp(r.get('event_date'))
                if ed<=cd: continue
                if (ed-cd).days>150: continue
            except Exception: continue
        score=0
        rr=_finite(r.get('revenue_actual')); ee=_finite(r.get('eps_actual'))
        if ar is not None and rr is not None:
            rel=abs(rr-ar)/max(abs(ar),1); score += 100 if rel<0.0005 else 50 if rel<0.01 else 0
        if ae is not None and ee is not None:
            rel=abs(ee-ae)/max(abs(ae),.01); score += 100 if rel<0.001 else 50 if rel<0.03 else 0
        if score: cand.append((score,r))
    return max(cand,key=lambda x:x[0])[1] if cand else None

def _management_bias_prior(rows, idx, metric):
    errs=[]
    for r in rows[:idx]:
        g=r.get('guidance') or {}; a=r.get('actual') or {}
        if metric=='revenue' and g.get('revenue_growth_low') is not None and a.get('revenue_growth_pct') is not None:
            mid=(g['revenue_growth_low']+g['revenue_growth_high'])/2; errs.append(a['revenue_growth_pct']-mid)
        elif metric=='eps' and g.get('eps_low') is not None and a.get('eps') is not None:
            mid=(g['eps_low']+g['eps_high'])/2; errs.append(a['eps']-mid)
    return sum(errs[-8:])/len(errs[-8:]) if errs else 0.0

def run_fair_backtest(symbol:str,periods:int=8):
    symbol=str(symbol or '').strip().upper(); periods=max(4,min(40,int(periods or 8)))
    if not symbol:return {'ok':False,'error':'缺少标的'}
    try:
        gb=run_guidance_backtest(symbol,max(periods+4,12)); grows=gb.get('rows') or []
        if not grows:
            return {'ok':True,'symbol':symbol,'status':'insufficient','valid_samples':0,'periods_requested':periods,'rows':[],'reason':'没有可严格匹配的历史管理层下一财季指引。'}
        wb=run_whisper_backtest(symbol,max(periods+6,14)); wrows=wb.get('rows') or []
        mb_rows=_marketbeat_history(symbol)
        ats=ats_source(symbol); weeks=ats.get('weeks') or []
        close=_load_history_close(symbol, years=3)
        candidates=[]
        grows=sorted(grows,key=lambda r:r.get('target_period') or '')
        for i,g in enumerate(grows):
            actual=g.get('actual') or {}; call_date=g.get('call_date')
            wr=_match_whisper(actual,wrows,call_date)
            # Revenue consensus can be sourced independently from MarketBeat's
            # public quarterly earnings history. Do not require the legacy
            # Whisper backtest to succeed just to build a fair Price-in sample.
            if wr is None and mb_rows:
                # Match the historical actual result first; this is safer than
                # assuming a calendar quarter from a company's fiscal period.
                ar=_finite(actual.get('revenue')); ae=_finite(actual.get('eps'))
                mbc=[]
                for x in mb_rows:
                    rr=_finite(x.get('revenue_actual')); ee=_finite(x.get('eps_actual'))
                    score=0.0
                    if ar is not None and rr is not None:
                        score += max(0.0,100.0-abs(rr-ar)/max(abs(ar),1.0)*1000.0)
                    if ae is not None and ee is not None:
                        score += max(0.0,50.0-abs(ee-ae)*50.0)
                    if score>0: mbc.append((score,x))
                if mbc:
                    hit=max(mbc,key=lambda z:z[0])[1]
                    wr={'event_date':hit.get('report_date'),'revenue_consensus':hit.get('revenue_consensus'),'revenue_actual':hit.get('revenue_actual'),'eps_consensus':hit.get('eps_consensus'),'eps_actual':hit.get('eps_actual')}
            if not wr: continue
            sig=_historical_ats_signal(weeks,wr.get('event_date'),close) if weeks else None
            # Revenue intent is the cleanest common metric because management
            # often gives revenue guidance while EPS guidance is absent.
            rev_cons=_finite(wr.get('revenue_consensus')); rev_actual=_finite(wr.get('revenue_actual'))
            eps_cons=_finite(wr.get('eps_consensus')); eps_actual=_finite(wr.get('eps_actual'))
            rev_g=g.get('guidance') or {}; rev_mid=None; eps_mid=None
            if rev_g.get('revenue_growth_low') is not None and actual.get('revenue_growth_pct') is not None:
                midg=(rev_g['revenue_growth_low']+rev_g['revenue_growth_high'])/2
                prior=actual.get('revenue')/(1+actual.get('revenue_growth_pct')/100) if actual.get('revenue_growth_pct') is not None else None
                if prior is not None: rev_mid=prior*(1+(midg+_management_bias_prior(grows,i,'revenue')*.50)/100)
            elif rev_g.get('revenue_low') is not None:
                midg=(rev_g['revenue_low']+rev_g['revenue_high'])/2; rev_mid=midg*(1+_management_bias_prior(grows,i,'revenue')*.002)
            if rev_mid is None: continue
            # AEL historical buy-side interpretation = management midpoint +
            # a shrunk company-specific management conservatism correction.
            ael_market=rev_cons
            if sig is not None: ael_market=_ats_estimate(rev_cons,sig,'revenue') if _finite(rev_cons) is not None else None
            # Without historical options snapshots, use the pre-event market/ATS
            # reconstruction as the ATS market-implied expectation. The AEL
            # native options layer is explicitly not backfilled.
            ats_market=ael_market
            # For AEL price-in replay, use pre-event price direction as a
            # separate market signal applied to the AEL intent delta.
            ael_adj=0.0
            if close is not None:
                pre=close[close.index<=pd.Timestamp(wr.get('event_date'))]
                if len(pre)>=21:
                    p0=_finite(pre.iloc[-21]);p1=_finite(pre.iloc[-1])
                    if p0 and p1:ael_adj=_clamp((p1/p0-1)*100/12,-1,1)*2.5
            ael_market=rev_cons*(1+ael_adj/100) if rev_cons is not None else None
            ael_pi=_pricein(rev_cons,rev_mid,ael_market); ats_pi=_pricein(rev_cons,rev_mid,ats_market)
            candidates.append({
                'financial_period':g.get('target_period'),'event_date':wr.get('event_date'),'sell_side_revenue':rev_cons,
                'management_guidance':{'low':rev_g.get('revenue_growth_low'),'high':rev_g.get('revenue_growth_high'),'midpoint_growth':(rev_g.get('revenue_growth_low')+rev_g.get('revenue_growth_high'))/2 if rev_g.get('revenue_growth_low') is not None else None},
                'ael_buy_side_revenue':rev_mid,'ael_market_implied_revenue':ael_market,'ats_market_implied_revenue':ats_market,
                'actual_revenue':rev_actual,'ael_pricein_pct':ael_pi,'ats_pricein_pct':ats_pi,
                'ael_error_pct':_error(ael_market,rev_actual),'ats_error_pct':_error(ats_market,rev_actual),
                'consensus_error_pct':_error(rev_cons,rev_actual),'ats_signal':sig,'lookahead_free':True
            })
        candidates=sorted(candidates,key=lambda r:r.get('financial_period') or '')[-periods:]
        if not candidates:
            return {'ok':True,'symbol':symbol,'status':'insufficient','valid_samples':0,'periods_requested':periods,'rows':[],'reason':'没有同时具备管理层指引、卖方共识、实际营收及财报前ATS/市场数据的共同季度。'}
        def mean(key):
            xs=[_finite(r.get(key)) for r in candidates]; xs=[x for x in xs if x is not None]; return sum(xs)/len(xs) if xs else None
        return {'ok':True,'symbol':symbol,'status':'backtested' if len(candidates)>=4 else 'low_sample','periods_requested':periods,'valid_samples':len(candidates),
                'ael_pricein_avg_pct':mean('ael_pricein_pct'),'ats_pricein_avg_pct':mean('ats_pricein_pct'),
                'ael_mae_pct':mean('ael_error_pct'),'ats_mae_pct':mean('ats_error_pct'),'consensus_mae_pct':mean('consensus_error_pct'),
                'rows':candidates,'definition':'管理层官方指引 → AEL买方解读 → 市场已经Price-in多少；AEL/ATS只比较其市场隐含预期的历史重演，不比较原有ATS异常分数。','limitation':'免费数据没有可验证的逐财报历史期权快照，因此历史AEL Market-Implied不倒填今天的期权数据；ATS使用财报前可得ATS活动与价格方向。'}
    except Exception as exc:
        return {'ok':False,'symbol':symbol,'status':'error','valid_samples':0,'rows':[],'error':str(exc)[:220]}
