"""Management guidance backtest sidecar.

Purpose: validate management's own forward guidance against the next reported
fiscal quarter. This is independent from AEL Whisper/ATS native backtests.

Sources:
- SEC Company Facts for historical reported revenue/EPS (US issuers only).
- StockAnalysis earnings-call transcripts for management guidance when the
  official SEC earnings release does not contain the call guidance. Such
  transcript-derived guidance is explicitly labeled as third-party transcript
  cross-check, not official SEC text.

No estimates are fabricated. A quarter is included only when guidance and the
next actual result can both be matched.
"""
from __future__ import annotations
from datetime import datetime, timezone
import html
import math
import os
import re
import threading
import time
from typing import Any

import pandas as pd
import requests

from pro_guidance_ats import (
    _sec_cik,
    _http,
    _plain,
    _extract_eps_candidates,
    _extract_revenue_candidates,
    _extract_revenue_growth_candidates,
    _quarter_context_ok,
    _date_mentions_target,
)

_CACHE = {}
_LOCK = threading.Lock()
TTL = 21600


def _finite(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _safe_pct(a, b):
    a, b = _finite(a), _finite(b)
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def _stockanalysis_index(symbol):
    s = str(symbol or '').strip().lower()
    if not s or any(x in s for x in ('.hk', '.ss', '.sz')):
        return None, '仅支持有公开电话会文字稿的美国股票'
    slug = s.replace('.', '-').replace('/', '-')
    url = f'https://stockanalysis.com/stocks/{slug}/transcripts/'
    text, err = _http(url, 'text', 8)
    if not text:
        return None, err or '电话会文字稿索引不可用'
    return text, None


def _transcript_links(index_html):
    out = []
    # StockAnalysis exposes transcript URLs in ordinary anchor tags. Keep only
    # earnings-call links, and retain newest first as presented by the page.
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', index_html or '', re.I | re.S):
        lab = _plain(label)
        if not re.search(r'Earnings Call:\s*Q\d', lab, re.I):
            continue
        full = href if href.startswith('http') else 'https://stockanalysis.com' + href
        # quarter/year from link text, e.g. Earnings Call: Q3 2026
        m = re.search(r'Earnings Call:\s*Q([1-4])\s+(20\d{2})', lab, re.I)
        if not m:
            continue
        out.append({'url': full, 'label': lab, 'quarter': int(m.group(1)), 'year': int(m.group(2))})
    seen = set(); uniq = []
    for x in out:
        if x['url'] in seen:
            continue
        seen.add(x['url']); uniq.append(x)
    return uniq


def _extract_guidance_from_transcript(text, target_end=None):
    """Extract the strongest forward-quarter management guidance in a transcript."""
    t = _plain(text)
    if len(t) < 500:
        return None
    eps = [x for x in _extract_eps_candidates(t) if _quarter_context_ok(x[2])]
    rev = [x for x in _extract_revenue_candidates(t) if _quarter_context_ok(x[2])]
    growth = [x for x in _extract_revenue_growth_candidates(t) if _quarter_context_ok(x[2])]

    def score(c):
        ctx = c[2].lower()
        s = 0
        if 'next quarter' in ctx: s += 80
        if any(k in ctx for k in ('outlook', 'guidance', 'expect', 'expects')): s += 30
        if target_end and _date_mentions_target(ctx, target_end): s += 100
        # Avoid selecting historical actuals merely because they occur near
        # guidance text.
        if re.search(r'last quarter|reported|actual results|this quarter', ctx): s -= 20
        return s

    eps = sorted(eps, key=score, reverse=True)
    rev = sorted(rev, key=score, reverse=True)
    growth = sorted(growth, key=score, reverse=True)
    best_eps = eps[0] if eps and score(eps[0]) > 20 else None
    best_rev = rev[0] if rev and score(rev[0]) > 20 else None
    best_growth = growth[0] if growth and score(growth[0]) > 20 else None
    if not (best_eps or best_rev or best_growth):
        return None
    return {
        'eps_low': best_eps[0] if best_eps else None,
        'eps_high': best_eps[1] if best_eps else None,
        'revenue_low': best_rev[0] if best_rev else None,
        'revenue_high': best_rev[1] if best_rev else None,
        'revenue_growth_low': best_growth[0] if best_growth else None,
        'revenue_growth_high': best_growth[1] if best_growth else None,
        'eps_text': best_eps[2] if best_eps else None,
        'revenue_text': best_rev[2] if best_rev else None,
        'revenue_growth_text': best_growth[2] if best_growth else None,
    }


def _companyfacts_quarters(symbol):
    cik, err = _sec_cik(symbol)
    if not cik:
        return [], err or 'SEC CIK unavailable'
    url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
    data, err = _http(url, 'json', 12)
    if not data:
        return [], err or 'SEC Company Facts unavailable'
    facts = data.get('facts') or {}

    def units(tags, unit='USD'):
        for taxonomy, tag in tags:
            um = ((facts.get(taxonomy) or {}).get(tag) or {}).get('units') or {}
            if unit in um:
                return um[unit]
            if um:
                return next(iter(um.values()))
        return []

    rev_items = units([
        ('us-gaap','RevenueFromContractWithCustomerExcludingAssessedTax'),
        ('us-gaap','Revenues'),
        ('us-gaap','SalesRevenueNet'),
    ])
    eps_items = units([
        ('us-gaap','EarningsPerShareDiluted'),
        ('us-gaap','EarningsPerShareBasic'),
    ], 'USD/shares')
    if not eps_items:
        eps_items = units([
            ('us-gaap','EarningsPerShareDiluted'),
            ('us-gaap','EarningsPerShareBasic'),
        ])

    by_end = {}
    for item in rev_items:
        st, en, val = item.get('start'), item.get('end'), _finite(item.get('val'))
        if not st or not en or val is None:
            continue
        try: days = (pd.Timestamp(en) - pd.Timestamp(st)).days
        except Exception: continue
        if not 70 <= days <= 110:
            continue
        form = item.get('form')
        if form not in ('10-Q','10-K'):
            continue
        key = str(en)
        row = by_end.setdefault(key, {'end': key, 'revenue': None, 'eps': None, 'filed': item.get('filed')})
        # Prefer latest-filed quarterly value for the same period, but do not
        # allow future filings relative to the actual period's first report.
        row['revenue'] = val
        row['filed'] = item.get('filed') or row.get('filed')
    for item in eps_items:
        st, en, val = item.get('start'), item.get('end'), _finite(item.get('val'))
        if not en or val is None:
            continue
        try: days = (pd.Timestamp(en) - pd.Timestamp(st)).days if st else 0
        except Exception: days = 0
        if st and not 70 <= days <= 110:
            continue
        if item.get('form') not in ('10-Q','10-K'):
            continue
        key = str(en)
        row = by_end.setdefault(key, {'end': key, 'revenue': None, 'eps': None, 'filed': item.get('filed')})
        row['eps'] = val
        row['filed'] = item.get('filed') or row.get('filed')
    rows = []
    for x in by_end.values():
        try: pd.Timestamp(x['end'])
        except Exception: continue
        if x.get('revenue') is None and x.get('eps') is None: continue
        rows.append(x)
    rows.sort(key=lambda x: x['end'])
    return rows, None


def _next_actual(actuals, call_date):
    try: d = pd.Timestamp(call_date)
    except Exception: return None
    cand = []
    for x in actuals:
        try: end = pd.Timestamp(x['end'])
        except Exception: continue
        if end <= d: continue
        # The next fiscal quarter should normally report within ~120 days;
        # allow 180 days for irregular calendars.
        if (end - d).days <= 180:
            cand.append(x)
    return cand[0] if cand else None


def _prior_year_actual(actuals, end):
    try: target = pd.Timestamp(end)
    except Exception: return None
    cand=[]
    for x in actuals:
        try: d=pd.Timestamp(x['end'])
        except Exception: continue
        if abs((d-(target-pd.DateOffset(years=1))).days) <= 100 and x.get('revenue') is not None:
            cand.append(x)
    return min(cand, key=lambda x: abs((pd.Timestamp(x['end'])-(target-pd.DateOffset(years=1))).days)) if cand else None


def _score_guidance(rows):
    # Revenue: guidance midpoint error in YoY percentage points, plus range hit.
    rev_err=[]; eps_err=[]; rev_hit=0; rev_n=0; eps_hit=0; eps_n=0
    for r in rows:
        g=r.get('guidance') or {}; a=r.get('actual') or {}
        if g.get('revenue_growth_low') is not None and a.get('revenue_growth_pct') is not None:
            lo,hi=g['revenue_growth_low'],g['revenue_growth_high']; act=a['revenue_growth_pct']; mid=(lo+hi)/2
            rev_err.append(abs(act-mid)); rev_n+=1
            if lo <= act <= hi: rev_hit+=1
        elif g.get('revenue_low') is not None and a.get('revenue') is not None:
            lo,hi=g['revenue_low'],g['revenue_high']; act=a['revenue']; mid=(lo+hi)/2
            rev_err.append(abs(act-mid)/abs(act)*100 if act else None); rev_n+=1
            if lo <= act <= hi: rev_hit+=1
        if g.get('eps_low') is not None and a.get('eps') is not None:
            lo,hi=g['eps_low'],g['eps_high']; act=a['eps']; mid=(lo+hi)/2
            if act is not None and act != 0:
                eps_err.append(abs(act-mid)/abs(act)*100)
                eps_n+=1
                if lo <= act <= hi: eps_hit+=1
    rev_err=[x for x in rev_err if x is not None]; eps_err=[x for x in eps_err if x is not None]
    # Convert lower error into a simple 0-100 evidence score. This score is
    # only for the guidance card, not a comparison with AEL/ATS native scores.
    rev_score=round(max(0,min(100,100-(sum(rev_err)/len(rev_err))*10)),1) if rev_err else None
    eps_score=round(max(0,min(100,100-(sum(eps_err)/len(eps_err))*10)),1) if eps_err else None
    scores=[x for x in (rev_score,eps_score) if x is not None]
    overall=round(sum(scores)/len(scores),1) if scores else None
    return {
        'revenue_guidance_score':rev_score,'eps_guidance_score':eps_score,'overall_score':overall,
        'revenue_avg_error':round(sum(rev_err)/len(rev_err),3) if rev_err else None,
        'eps_avg_error':round(sum(eps_err)/len(eps_err),3) if eps_err else None,
        'revenue_hit_rate':round(rev_hit/rev_n*100,1) if rev_n else None,
        'eps_hit_rate':round(eps_hit/eps_n*100,1) if eps_n else None,
        'revenue_samples':rev_n,'eps_samples':eps_n,
    }


def run_guidance_backtest(symbol, quarters=20):
    symbol=str(symbol or '').strip().upper()
    quarters=max(1,min(int(quarters or 20),40))
    cache_key=f'{symbol}:{quarters}'
    now=time.time()
    with _LOCK:
        c=_CACHE.get(cache_key)
        if c and now-c[0] < TTL: return c[1]
    index, err=_stockanalysis_index(symbol)
    if not index:
        return {'ok':True,'symbol':symbol,'status':'unavailable','valid_samples':0,'rows':[],'score':None,'reason':err or '暂无电话会文字稿来源'}
    links=_transcript_links(index)
    if not links:
        return {'ok':True,'symbol':symbol,'status':'insufficient','valid_samples':0,'rows':[],'score':None,'reason':'未找到可用财报电话会文字稿'}
    actuals, aerr=_companyfacts_quarters(symbol)
    if not actuals:
        return {'ok':True,'symbol':symbol,'status':'unavailable','valid_samples':0,'rows':[],'score':None,'reason':aerr or 'SEC实际财报数据不可用'}
    rows=[]
    # Process newest first, but return chronological rows for readability.
    for link in links[:max(quarters*2, 20)]:
        body, berr=_http(link['url'],'text',8)
        if not body: continue
        # Call date: use page publication metadata if available; fallback to
        # the actual quarter calendar from title is not safe, so use the first
        # ISO date visible near the page title.
        m=re.search(r'(20\d{2}-\d{2}-\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+20\d{2})', _plain(body)[:8000], re.I)
        if not m: continue
        try: call_date=pd.Timestamp(m.group(1)).normalize()
        except Exception: continue
        guidance=_extract_guidance_from_transcript(body)
        if not guidance: continue
        actual=_next_actual(actuals, call_date)
        if not actual: continue
        prior=_prior_year_actual(actuals, actual['end'])
        actual2=dict(actual)
        if actual.get('revenue') is not None and prior and prior.get('revenue'):
            actual2['revenue_growth_pct']=_safe_pct(actual['revenue'],prior['revenue'])
        row={'guidance':guidance,'actual':actual2,'call_date':call_date.date().isoformat(),'target_period':actual.get('end'),'transcript_url':link['url'],'transcript_label':link['label'],'source_type':'transcript_crosscheck'}
        # Keep only rows with at least one evaluable metric.
        if (guidance.get('revenue_growth_low') is not None and actual2.get('revenue_growth_pct') is not None) or (guidance.get('revenue_low') is not None and actual2.get('revenue') is not None) or (guidance.get('eps_low') is not None and actual2.get('eps') is not None):
            rows.append(row)
        if len(rows) >= quarters: break
    rows=sorted(rows,key=lambda r:r['target_period'])
    score=_score_guidance(rows)
    result={'ok':True,'symbol':symbol,'status':'backtested' if rows else 'insufficient','valid_samples':len(rows),'rows':rows,'score':score.get('overall_score'),'summary':score,'lookback':f'最多{quarters}个季度','source_type':'transcript_crosscheck + SEC Company Facts','reason':'回测管理层针对下一财季的原始指引与下一财季实际结果。缺失或无法严格匹配的数据不回填。'}
    with _LOCK: _CACHE[cache_key]=(now,result)
    return result
