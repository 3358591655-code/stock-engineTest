"""AEL Guidance + ATS sidecar.

This module is intentionally isolated from the original AEL Whisper and from
pro_expectation.py.  Management guidance is sourced from official SEC EDGAR
8-K / earnings-release exhibits when available.  If the official filing cannot
be matched to the target fiscal quarter, the UI shows 暂无数据 rather than
borrowing a third-party number or another quarter.
"""
from datetime import datetime, timezone, timedelta
import html
import math
import os
import re
import threading
from time import time

import pandas as pd
import requests
import yfinance as yf

from pro_whisper import analyze_whisper
from pro_ats import ats_source
from sec_cik_fallback import SEC_CIK_FALLBACK

_CACHE = {}
_LOCK = threading.Lock()
_SEC_LOCK = threading.Lock()
_SEC_TICKER_CACHE = {"data": None, "expires_at": 0.0}
TTL = 900
SEC_TTL = 86400
SEC_HEADERS = {
    "User-Agent": os.getenv("SEC_USER_AGENT", "AEL Research/2.6.16"),
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}


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


def _money_token(value, unit=None):
    try:
        v = float(str(value).replace(",", ""))
    except Exception:
        return None
    u = str(unit or "").lower()
    return v * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
                "b": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12}.get(u, 1.0)


def _plain(html_text):
    s = html.unescape(html_text or "")
    s = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _http(url, kind="text", timeout=7):
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=timeout)
        r.raise_for_status()
        if kind == "json":
            return r.json(), None
        return r.text, None
    except Exception as exc:
        return None, str(exc)[:180]


def _sec_cik(symbol):
    """Resolve CIK without making SEC ticker mapping a single point of failure."""
    sym = str(symbol).upper().strip()
    # Offline fallback first for common issuers. The actual filings/facts still
    # come from SEC; this only supplies the stable identifier when mapping.json
    # is blocked from a cloud IP.
    if sym in SEC_CIK_FALLBACK:
        return SEC_CIK_FALLBACK[sym], None
    now = time()
    with _SEC_LOCK:
        if _SEC_TICKER_CACHE.get("data") and now < _SEC_TICKER_CACHE.get("expires_at", 0):
            data = _SEC_TICKER_CACHE["data"]
        else:
            data, err = _http("https://www.sec.gov/files/company_tickers.json", "json", 10)
            if not data:
                return None, err or "SEC ticker mapping unavailable"
            _SEC_TICKER_CACHE.update({"data": data, "expires_at": now + SEC_TTL})
    for row in (data or {}).values():
        if str(row.get("ticker", "")).upper() == sym:
            cik = str(row.get("cik_str") or "").zfill(10)
            return cik, None
    return None, f"SEC 未找到 {sym} 的 CIK"


def _sec_filing_rows(symbol, target_earnings_date=None):
    cik, err = _sec_cik(symbol)
    if not cik:
        return [], err
    js, err = _http(f"https://data.sec.gov/submissions/CIK{cik}.json", "json", 8)
    if not js:
        return [], err or "SEC submissions unavailable"
    recent = (js.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    items = recent.get("items") or []
    try:
        end = pd.Timestamp(target_earnings_date).date() if target_earnings_date else None
    except Exception:
        end = None
    floor = datetime.now(timezone.utc).date() - timedelta(days=180)
    rows = []
    for i, form in enumerate(forms):
        if str(form).upper() != "8-K":
            continue
        fd = str(dates[i] if i < len(dates) else "")
        try:
            d = pd.Timestamp(fd).date()
        except Exception:
            continue
        if d < floor:
            continue
        if end and d > end:
            continue
        item_text = str(items[i] if i < len(items) else "")
        if "2.02" not in item_text:
            continue
        acc = str(accessions[i] if i < len(accessions) else "")
        if not acc:
            continue
        rows.append({"filing_date": fd, "accession": acc, "primary_document": str(docs[i] if i < len(docs) else "")})
    return rows, None


def _filing_index(cik, accession):
    acc0 = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc0}/{accession}-index.html"
    text, err = _http(url, "text", 7)
    return text, url, err


def _document_links(index_html, cik, accession):
    acc0 = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc0}/"
    links = []
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', index_html or "", re.I | re.S):
        lab = _plain(label).upper()
        href2 = html.unescape(href).strip()
        if not href2 or href2.lower().startswith("javascript:"):
            continue
        full = href2 if href2.startswith("http") else base + href2.lstrip("/")
        score = 0
        if "EX-99.1" in lab or "99.1" in lab:
            score += 100
        if any(x in (href2 + " " + lab).lower() for x in ("ex99", "ex-99", "991", "earnings", "press", "release")):
            score += 30
        if score:
            links.append((score, full, lab))
    # Preserve best unique documents first.
    seen = set(); out = []
    for _, url, lab in sorted(links, reverse=True):
        if url in seen:
            continue
        seen.add(url); out.append((url, lab))
    return out[:8]


def _quarter_context_ok(context):
    c = context.lower()
    # Issuers commonly say "September quarter", "June quarter", etc.
    # Those are fiscal-quarter references and must pass the same lock.
    month_q = r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+quarter\b"
    has_quarter = bool(re.search(r"\b(?:next|current|upcoming|first|second|third|fourth|1st|2nd|3rd|4th|fiscal)\s+quarter\b|\bquarter ending\b|\bthree months\b|"+month_q, c))
    annual_only = bool(re.search(r"\bfull[- ]year\b|\bfiscal year\b|\byear ending\b", c)) and not has_quarter
    return has_quarter and not annual_only


def _date_mentions_target(text, target_end):
    if not target_end:
        return False
    try:
        d = pd.Timestamp(target_end)
        names = [d.strftime("%B %Y"), d.strftime("%b %Y"), d.strftime("%m/%d/%Y"), d.strftime("%-m/%-d/%Y")]
        low=text.lower()
        if any(x.lower() in low for x in names):
            return True
        # Earnings releases often identify the target only as "September quarter"
        # rather than printing the exact period-end date. The target fiscal month
        # is still a valid quarter-lock signal when the filing itself is the next-quarter
        # earnings release and is temporally before the target earnings date.
        month=d.strftime("%B").lower()
        return bool(re.search(r"\b"+re.escape(month)+r"\s+quarter\b", low))
    except Exception:
        return False


def _extract_eps_candidates(text):
    patterns = [
        r"(?:adjusted|non[- ]gaap|gaap)?\s*(?:diluted\s+)?(?:earnings\s+per\s+share|eps)\s+(?:of\s+)?\$?(-?\d+(?:\.\d+)?)\s*(?:to|-|–)\s*\$?(-?\d+(?:\.\d+)?)",
        r"(?:adjusted|non[- ]gaap|gaap)?\s*(?:diluted\s+)?(?:earnings\s+per\s+share|eps).*?(?:between|range\s+of|from)\s+\$?(-?\d+(?:\.\d+)?)\s*(?:and|to|-)\s*\$?(-?\d+(?:\.\d+)?)",
    ]
    out=[]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            lo, hi = _finite(m.group(1)), _finite(m.group(2))
            if lo is None or hi is None or hi < lo or hi > 1000:
                continue
            ctx=text[max(0,m.start()-300):min(len(text),m.end()+300)]
            out.append((lo,hi,ctx,m.start()))
    return out


def _extract_revenue_candidates(text):
    unit=r"(?:thousand|million|billion|trillion|K|M|B|T)?"
    patterns=[
        rf"(?:revenue|sales)\s+(?:of\s+)?\$?([\d,.]+)\s*({unit})\s*(?:to|-|–)\s*\$?([\d,.]+)\s*({unit})",
        rf"(?:revenue|sales).*?(?:between|range\s+of|from|expected\s+to\s+be)\s+\$?([\d,.]+)\s*({unit})\s*(?:and|to|-)\s*\$?([\d,.]+)\s*({unit})",
    ]
    out=[]
    for pat in patterns:
        for m in re.finditer(pat,text,re.I):
            # Empty unit groups are valid; because the nested optional group can
            # be empty, the captured groups remain stable at 1..4.
            lo=_money_token(m.group(1),m.group(2)); hi=_money_token(m.group(3),m.group(4))
            if lo is None or hi is None or hi < lo or hi > 1e15:
                continue
            ctx=text[max(0,m.start()-300):min(len(text),m.end()+300)]
            out.append((lo,hi,ctx,m.start()))
    return out


def _extract_revenue_qualitative_candidates(text):
    """Extract non-numeric quarterly revenue guidance without inventing a number.
    Examples: down low single digits; down low to mid-single digits;
    up mid-single digits.
    """
    patterns=[
        r"(?:revenue|sales|total\s+(?:company\s+)?revenue).*?(?:expect|expects|outlook|guidance).*?(?:to\s+be\s+)?(?P<phrase>up|down)\s+(?P<band>low|mid|high)(?:\s+to\s+(?P<band2>low|mid|high))?[- ]single[- ]digits",
        r"(?:revenue|sales|total\s+(?:company\s+)?revenue).*?(?P<phrase>up|down)\s+(?P<band>low|mid|high)(?:\s+to\s+(?P<band2>low|mid|high))?[- ]single[- ]digits",
    ]
    out=[]
    for pat in patterns:
        for m in re.finditer(pat,text,re.I|re.S):
            ctx=text[max(0,m.start()-350):min(len(text),m.end()+350)]
            out.append((m.group(0),ctx,m.start()))
    return out


def _extract_revenue_growth_candidates(text):
    """Extract management revenue YoY growth guidance such as +9% to +11%.
    This is still official guidance, but it is not a company-disclosed dollar range.
    """
    patterns = [
        r"(?:revenue|sales|total\s+(?:company\s+)?revenue).*?(?:grow|growth).*?(?:between|from)\s+([+-]?\d+(?:\.\d+)?)\s*%\s*(?:and|to|-|–)\s*([+-]?\d+(?:\.\d+)?)\s*%",
        r"(?:revenue|sales|total\s+(?:company\s+)?revenue).*?(?:grow|growth).*?([+-]?\d+(?:\.\d+)?)\s*%\s*(?:to|-|–)\s*([+-]?\d+(?:\.\d+)?)\s*%",
    ]
    out=[]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I|re.S):
            lo,hi=_finite(m.group(1)),_finite(m.group(2))
            if lo is None or hi is None or hi < lo or abs(lo)>100 or abs(hi)>100:
                continue
            ctx=text[max(0,m.start()-350):min(len(text),m.end()+350)]
            out.append((lo,hi,ctx,m.start()))
    return out


def _yahoo_quarter_revenue(symbol, target_end):
    """Find the closest prior-year same-fiscal-quarter revenue from Yahoo quarterly financials.
    Used only to quantify an official percentage guidance; it never changes original Whisper.
    """
    try:
        t=yf.Ticker(symbol)
        df=getattr(t,"quarterly_income_stmt",None)
        if df is None or df.empty:
            df=t.quarterly_financials
        if df is None or df.empty or not target_end:
            return None, None
        rows=[]
        for dt in list(df.columns):
            rev=None
            for k in ("Total Revenue","Operating Revenue"):
                if k in df.index:
                    rev=_finite(df.loc[k,dt])
                    if rev is not None: break
            if rev is not None:
                d=pd.Timestamp(dt).normalize()
                rows.append((d,rev))
        if not rows: return None,None
        target=pd.Timestamp(target_end).normalize()
        prior=target-pd.DateOffset(years=1)
        # Same fiscal quarter usually has the closest period end around prior year.
        d,rev=min(rows,key=lambda x: abs((x[0]-prior).days))
        if abs((d-prior).days)>100:
            return None,None
        return rev,d.date().isoformat()
    except Exception:
        return None,None


def _quantify_revenue_growth_guidance(symbol, target_end, growth_low, growth_high):
    base,base_period=_yahoo_quarter_revenue(symbol,target_end)
    if base is None:
        return {"revenue_low":None,"revenue_high":None,"base_revenue":None,"base_period":base_period,"status":"unavailable"}
    return {
        "revenue_low":base*(1+growth_low/100.0),
        "revenue_high":base*(1+growth_high/100.0),
        "base_revenue":base,"base_period":base_period,"status":"derived"
    }


def _sec_guidance(symbol, target_period, target_earnings_date):
    """Return only official, quarter-matched management guidance."""
    out={
        "available":False,"eps_low":None,"eps_high":None,"revenue_low":None,"revenue_high":None,
        "source":"SEC EDGAR 8-K / earnings release","source_type":"official_filing",
        "filing_date":None,"filing_url":None,"document_url":None,"period_end":None,
        "period_match":None,"eps_basis":None,"revenue_basis":None,"revenue_growth_low":None,"revenue_growth_high":None,"revenue_growth_basis":None,"revenue_growth_source_text":None,"revenue_growth_quantified":None,"revenue_guidance_text":None,"error":None,
    }
    # Resolve the fiscal target before any provider call. A provider failure
    # must never leave target_end unbound and must never take ATS down with it.
    target_end=(target_period or {}).get("target_end") if isinstance(target_period,dict) else target_period
    rows, err = _sec_filing_rows(symbol, target_earnings_date)
    if err:
        out["error"]=err
        fallback=_transcript_guidance_fallback(symbol,target_end,target_earnings_date)
        return fallback or out
    cik, cik_err = _sec_cik(symbol)
    if not cik:
        out["error"]=cik_err or "SEC CIK unavailable"
        fallback=_transcript_guidance_fallback(symbol,target_end,target_earnings_date)
        return fallback or out
    # Newest relevant earnings releases first. We inspect several because the
    # latest 8-K may be a correction/amendment without a guidance sentence.
    for row in rows[:10]:
        idx_html, filing_url, idx_err = _filing_index(cik,row["accession"])
        if not idx_html:
            continue
        docs=_document_links(idx_html,cik,row["accession"])
        # If EX-99.1 is not linked in a parseable table, also try the primary 8-K.
        primary=row.get("primary_document")
        if primary:
            base=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{row['accession'].replace('-','')}/"
            purl=base+primary
            if all(x[0] != purl for x in docs): docs.append((purl,"8-K"))
        for doc_url, doc_label in docs:
            body, body_err=_http(doc_url,"text",7)
            if not body:
                continue
            text=_plain(body)
            if len(text)<300 or not re.search(r"guidance|outlook|expects?|forecast|projected|target",text,re.I):
                continue
            eps_cands=_extract_eps_candidates(text)
            rev_cands=_extract_revenue_candidates(text)
            rev_growth_cands=_extract_revenue_growth_candidates(text)
            rev_qual_cands=_extract_revenue_qualitative_candidates(text)
            # Require a quarter context. This prevents full-year guidance from
            # being displayed as a quarterly guide.
            eps_valid=[x for x in eps_cands if _quarter_context_ok(x[2])]
            rev_valid=[x for x in rev_cands if _quarter_context_ok(x[2])]
            rev_growth_valid=[x for x in rev_growth_cands if _quarter_context_ok(x[2])]
            target_hit=_date_mentions_target(text,target_end)
            next_q_eps=[x for x in eps_valid if re.search(r"\bnext\s+quarter\b", x[2], re.I)]
            next_q_rev=[x for x in rev_valid if re.search(r"\bnext\s+quarter\b", x[2], re.I)]
            next_q_rev_growth=[x for x in rev_growth_valid if re.search(r"\bnext\s+quarter\b", x[2], re.I)]
            if target_hit:
                period_match="target_period_date"
            elif next_q_eps or next_q_rev:
                # The strongest text-only quarter lock available when the
                # issuer does not print an exact period-end date is an explicit
                # 'next quarter' statement in the earnings release.
                eps_valid, rev_valid = next_q_eps, next_q_rev
                rev_growth_valid = next_q_rev_growth
                period_match="explicit_next_quarter"
            else:
                # Do not accept 'current quarter' or a generic 'quarter' here:
                # those can refer to the just-reported period and would violate
                # AEL's fiscal-period lock.
                continue
            if not eps_valid and not rev_valid and not rev_growth_valid:
                continue
            # Prefer candidates whose context actually mentions next/current
            # quarter; otherwise first quarter-context match is used.
            def rank(c):
                ctx=c[2].lower()
                return (100 if target_hit else 0) + (20 if "next quarter" in ctx else 0) + (10 if "outlook" in ctx or "guidance" in ctx else 0)
            eps=sorted(eps_valid,key=rank,reverse=True)[0] if eps_valid else None
            rev=sorted(rev_valid,key=rank,reverse=True)[0] if rev_valid else None
            rev_growth=sorted(rev_growth_valid,key=rank,reverse=True)[0] if rev_growth_valid else None
            if not eps and not rev and not rev_growth:
                continue
            qgrowth=_quantify_revenue_growth_guidance(symbol,target_end,rev_growth[0],rev_growth[1]) if rev_growth else {"status":"unavailable"}
            out.update({
                "available":True,
                "eps_low":eps[0] if eps else None,"eps_high":eps[1] if eps else None,
                "revenue_low":rev[0] if rev else qgrowth.get("revenue_low"),"revenue_high":rev[1] if rev else qgrowth.get("revenue_high"),
                "revenue_growth_low":rev_growth[0] if rev_growth else None,"revenue_growth_high":rev_growth[1] if rev_growth else None,
                "revenue_growth_basis":"YoY growth guidance" if rev_growth else None,
                "revenue_growth_source_text":rev_growth[2] if rev_growth else None,
                "revenue_growth_quantified":qgrowth if rev_growth else None,
                "filing_date":row["filing_date"],"filing_url":filing_url,"document_url":doc_url,
                "period_end":target_end,"period_match":period_match,
                "eps_basis":"management disclosed range" if eps else None,
                "revenue_basis":"management disclosed range" if rev else ("AEL quantified from official management YoY growth guidance" if rev_growth and qgrowth.get("status")=="derived" else None),
                "error":None,
            })
            return out
    # SEC earnings releases frequently omit verbal CFO guidance. Use a separate
    # transcript cross-check only after official filing parsing fails. It is
    # explicitly labeled as third-party evidence and never called official.
    fallback=_transcript_guidance_fallback(symbol,target_end,target_earnings_date)
    if fallback:
        return fallback
    out["error"]="未找到与目标财季严格匹配的可验证管理层季度指引"
    return out



def _transcript_guidance_fallback(symbol, target_end=None, target_earnings_date=None):
    """Cross-check management guidance from a public earnings-call transcript.

    SEC EX-99.1 often contains the earnings release but not the CFO's verbal
    forward guidance. This fallback is deliberately labeled third-party
    transcript cross-check and never presented as an SEC/official filing.
    """
    s=str(symbol or '').strip().lower()
    if not s or any(x in s for x in ('.hk','.ss','.sz')):
        return None
    slug=s.replace('.','-').replace('/','-')

    # StockAnalysis is intentionally NOT a hard dependency.  It can return 403
    # to cloud/server IPs, which used to leak directly into the AEL card.
    # Prefer predictable public transcript pages and fail silently to the next
    # provider.  These are cross-check sources only; SEC remains first.
    now_year=datetime.now(timezone.utc).year
    candidates=[]
    for year in range(now_year, max(now_year-4, 2018), -1):
        for q in (4,3,2,1):
            candidates.append((
                f'https://tickertrends.io/transcripts/{slug.upper()}/Q{q}-earnings-transcript-{year}',
                f'Earnings Call: Q{q} {year} · TickerTrends'
            ))
            candidates.append((
                f'https://goodmoat.com/stocks/{slug.lower()}/transcripts/{year}-year/{q}-quarter',
                f'Earnings Call: Q{q} {year} · GoodMoat'
            ))

    seen=set()
    for url,label in candidates:
        if url in seen:
            continue
        seen.add(url)
        body,berr=_http(url,'text',5)
        if not body: continue
        text=_plain(body)
        if len(text)<500: continue
        eps=[x for x in _extract_eps_candidates(text) if _quarter_context_ok(x[2])]
        rev=[x for x in _extract_revenue_candidates(text) if _quarter_context_ok(x[2])]
        growth=[x for x in _extract_revenue_growth_candidates(text) if _quarter_context_ok(x[2])]
        qualitative=_extract_revenue_qualitative_candidates(text)
        def rank(c):
            ctx=c[2].lower(); score=0
            if target_end and _date_mentions_target(ctx,target_end): score+=100
            if 'next quarter' in ctx: score+=60
            if any(k in ctx for k in ('guidance','outlook','expects','expect')): score+=25
            if 'last quarter' in ctx or 'reported' in ctx: score-=20
            return score
        eps=sorted(eps,key=rank,reverse=True); rev=sorted(rev,key=rank,reverse=True); growth=sorted(growth,key=rank,reverse=True)
        e=eps[0] if eps and rank(eps[0])>=25 else None
        r=rev[0] if rev and rank(rev[0])>=25 else None
        g=growth[0] if growth and rank(growth[0])>=25 else None
        qtext=None
        if qualitative:
            qs=sorted(qualitative,key=lambda x: (100 if target_end and _date_mentions_target(x[1],target_end) else 0)+(60 if 'next quarter' in x[1].lower() else 0)+(25 if any(k in x[1].lower() for k in ('guidance','outlook','expect')) else 0), reverse=True)
            if qs and ((100 if target_end and _date_mentions_target(qs[0][1],target_end) else 0)+(60 if 'next quarter' in qs[0][1].lower() else 0)+(25 if any(k in qs[0][1].lower() for k in ('guidance','outlook','expect')) else 0))>=25:
                qtext=qs[0][0]
        if not (e or r or g or qtext): continue
        return {
            'available':True,
            'eps_low':e[0] if e else None,'eps_high':e[1] if e else None,
            'revenue_low':r[0] if r else None,'revenue_high':r[1] if r else None,
            'revenue_growth_low':g[0] if g else None,'revenue_growth_high':g[1] if g else None,
            'revenue_growth_basis':'YoY growth guidance' if g else None,
            'revenue_growth_source_text':g[2] if g else None,
            'revenue_growth_quantified':_quantify_revenue_growth_guidance(symbol,target_end,g[0],g[1]) if g else None,
            'revenue_guidance_text':qtext,
            'filing_date':None,'filing_url':None,'document_url':url,
            'period_end':target_end,'period_match':'transcript_target_period' if target_end else 'transcript_forward_quarter',
            'eps_basis':'management disclosed range (transcript)' if e else None,
            'revenue_basis':'management disclosed range (transcript)' if r else ('AEL quantified from management YoY growth guidance (transcript)' if g else None),
            'source':'Earnings call transcript · third-party cross-check',
            'source_type':'transcript_crosscheck',
            'official_vs_crosscheck':'crosscheck',
            'error':None,
        }
    return None

def _guidance_whisper(consensus, guidance, nowcast, revisions, kind):
    if kind == "eps":
        c=_finite((consensus or {}).get("consensus")); g=_mid((guidance or {}).get("eps_low"),(guidance or {}).get("eps_high")); n=_finite((nowcast or {}).get("eps"))
    else:
        c=_finite((consensus or {}).get("consensus")); g=_mid((guidance or {}).get("revenue_low"),(guidance or {}).get("revenue_high")); n=_finite((nowcast or {}).get("revenue"))
    rev=_finite((consensus or {}).get("revision_30d_pct"))
    if rev is None: rev=_finite((consensus or {}).get("revision_7d_pct"))
    hist_bias=_finite((revisions or {}).get(f"{kind}_bias_pct"))
    if g is None:
        # AEL Buy-Side may still form an independent model estimate when management
        # does not publish a numeric metric. This is explicitly NOT company guidance.
        comps=[]
        if c is not None: comps.append(("卖方Consensus",c,0.65))
        if n is not None: comps.append(("基本面Nowcast",n,0.35))
        if not comps:
            return {"value":None,"components":[],"status":"unavailable","reason":"无可验证管理层数值指引，且缺少足够公开数据建立AEL买方模型"}
        sw=sum(w for _,_,w in comps); value=sum(v*w for _,v,w in comps)/sw
        return {"value":value,"components":comps,"status":"inferred_no_direct_guidance","reason":"本财季未识别到管理层明确数值指引；AEL买方预期仅由卖方Consensus与基本面Nowcast独立推导，不冒充公司Guidance。","adjustments":[]}
    comps=[]
    if g is not None: comps.append(("管理层Guidance中值",g,0.55))
    if c is not None: comps.append(("卖方Consensus",c,0.25))
    if n is not None: comps.append(("基本面Nowcast",n,0.20))
    sw=sum(w for _,_,w in comps); value=sum(v*w for _,v,w in comps)/sw
    adj=0.0; parts=[]
    if rev is not None:
        a=_clamp(rev,-6,6)*0.20; adj+=a; parts.append(("30D卖方预测修正",a))
    if hist_bias is not None:
        cap=5 if kind=="eps" else 3; a=_clamp(hist_bias,-cap,cap)*0.15; adj+=a; parts.append(("公司历史Consensus偏差",a))
    value*=1+adj/100
    return {"value":value,"components":[{"factor":n,"value":v,"weight":w} for n,v,w in comps],"adjustments":[{"factor":n,"pct":round(v,3)} for n,v in parts],"status":"inferred"}


def _gap_pct(a,b):
    a,b=_finite(a),_finite(b)
    if a is None or b is None or b==0:return None
    return (a/b-1)*100


def _ats_whisper_score(ats, whisper_data):
    if not ats.get("available"):
        return {"status":"unavailable","score":None,"direction":"暂无数据","confidence":None}
    share=_finite(ats.get("ats_share_pct")); change=_finite(ats.get("ats_share_change_pct")); z=_finite(ats.get("ats_volume_z")); parts=[]
    if share is not None: parts.append(("ATS占比",_clamp((share-25)/20,-1,1),0.40))
    if change is not None: parts.append(("ATS周变化",_clamp(change/5,-1,1),0.25))
    if z is not None: parts.append(("ATS成交异常度",_clamp(z/3,-1,1),0.35))
    attention=50+50*(sum(v*w for _,v,w in parts)/sum(w for _,_,w in parts)) if parts else None
    ms=(whisper_data or {}).get("market_signals") or {}; mom=_finite(ms.get("momentum_20d_pct")); skew=_finite(ms.get("iv_skew_pct")); dp=[]
    if mom is not None: dp.append((_clamp(mom/12,-1,1),0.60))
    if skew is not None: dp.append((_clamp(-skew/15,-1,1),0.40))
    ds=(sum(v*w for v,w in dp)/sum(w for _,w in dp)) if dp else 0
    direction="中性/未知" if not dp else ("偏正向" if ds>=.20 else "偏负向" if ds<=-.20 else "中性")
    confidence="中高" if share is not None and change is not None and z is not None else ("中" if sum(x is not None for x in (share,change,z))>=2 else "低")
    return {"status":"inferred","score":round(_clamp(attention,0,100),1) if attention is not None else None,"direction":direction,"confidence":confidence,"ats_share_pct":share,"ats_share_change_pct":change,"ats_volume_z":z,"decomposition":[{"factor":n,"normalized":round(v,3),"weight":w} for n,v,w in parts],"note":"ATS只证明场外交易活动/异常度，不提供可靠买卖方向；方向参考来自独立的期权/价格定位层。"}



def _ats_version_expectation(consensus, aw, kind):
    """Independent ATS-version model estimate.

    ATS does not disclose reliable buy/sell direction.  Therefore this is NOT
    raw ATS data: ATS activity supplies intensity, while the independent
    price/options positioning already used by the ATS sidecar supplies the
    directional sign.  The adjustment is deliberately capped and is exposed
    as Model-Implied.
    """
    c = _finite(consensus)
    score = _finite((aw or {}).get("score"))
    direction = str((aw or {}).get("direction") or "")
    if c is None or score is None:
        return {"value": None, "adjustment_pct": None, "status": "unavailable", "method": "ATS intensity + independent market direction"}
    sign = 1 if "正" in direction else -1 if "负" in direction else 0
    intensity = _clamp(abs(score - 50.0) / 50.0, 0, 1)
    # Conservative, capped model adjustment.  It is intentionally smaller
    # than the original AEL Market-Implied layer so the two remain distinct.
    cap = 2.5 if kind == "revenue" else 4.0
    adj = sign * intensity * cap
    return {
        "value": c * (1 + adj / 100.0),
        "adjustment_pct": adj,
        "status": "inferred",
        "method": "ATS activity intensity + independent price/options direction; capped model adjustment",
        "label": "ATS Model-Implied",
    }


def _four_layer_expectation(whisper, ss, gw, aw):
    """Build the four parallel expectation layers requested by AEL UI."""
    rev_cons = _finite(ss.get("revenue")); eps_cons = _finite(ss.get("eps"))
    ats_rev = _ats_version_expectation(rev_cons, aw, "revenue")
    ats_eps = _ats_version_expectation(eps_cons, aw, "eps")
    return {
        "sell_side_official": {
            "revenue": rev_cons, "eps": eps_cons,
            "label": "Sell-side Consensus / 卖方正版预期", "status": "observed" if (rev_cons is not None or eps_cons is not None) else "unavailable"
        },
        "ael_buy_side_dark": {
            "revenue": gw.get("revenue"), "eps": gw.get("eps"),
            "label": "AEL Buy-Side Dark Expectation / AEL买方暗盘预期",
            "status": gw.get("status", "unavailable"),
            "revenue_gap_vs_sell_side_pct": gw.get("revenue_gap_vs_sell_side_pct"),
            "eps_gap_vs_sell_side_pct": gw.get("eps_gap_vs_sell_side_pct"),
            "method": gw.get("method")
        },
        "ats_version": {
            "revenue": ats_rev.get("value"), "eps": ats_eps.get("value"),
            "label": "ATS Version Expectation / ATS版本预期",
            "status": "inferred" if (ats_rev.get("value") is not None or ats_eps.get("value") is not None) else "unavailable",
            "revenue_adjustment_pct": ats_rev.get("adjustment_pct"),
            "eps_adjustment_pct": ats_eps.get("adjustment_pct"),
            "method": ats_rev.get("method")
        },
        "ael_implied": {
            "revenue": (whisper.get("revenue") or {}).get("implied"),
            "eps": (whisper.get("eps") or {}).get("implied"),
            "label": "AEL Market-Implied / AEL隐含预期",
            "status": "inferred" if ((whisper.get("revenue") or {}).get("implied") is not None or (whisper.get("eps") or {}).get("implied") is not None) else "unavailable",
            "revenue_pricein_pct": (whisper.get("revenue") or {}).get("pricein_pct"),
            "eps_pricein_pct": (whisper.get("eps") or {}).get("pricein_pct"),
            "method": "Original AEL Whisper Market-Implied layer; unchanged"
        }
    }

def _temperature(ats, whisper_data, days):
    vals=[]
    if ats.get("available"):
        z=_finite(ats.get("ats_volume_z")); ch=_finite(ats.get("ats_share_change_pct"))
        if z is not None: vals.append(("ATS异常",_clamp(abs(z)/3,0,1),.35))
        if ch is not None: vals.append(("ATS变化",_clamp(abs(ch)/5,0,1),.15))
    nd=((whisper_data or {}).get("next_earnings_dark") or {}).get("data") or {}
    iv=_finite(nd.get("event_implied_move_pct")); vr=_finite(nd.get("pre_event_volume_ratio"))
    if iv is not None: vals.append(("事件隐含波动",_clamp(iv/15,0,1),.20))
    if vr is not None: vals.append(("财报前成交量",_clamp(max(0,vr-1)/1.5,0,1),.10))
    if days is not None: vals.append(("财报倒计时",_clamp((21-max(0,days))/21,0,1),.20))
    if not vals:return {"score":None,"label":"暂无数据","components":[]}
    score=100*sum(v*w for _,v,w in vals)/sum(w for _,_,w in vals)
    return {"score":round(score,1),"label":"低" if score<35 else "中" if score<65 else "高" if score<82 else "很高","components":[{"factor":n,"normalized":round(v,3),"weight":w} for n,v,w in vals]}


def _anomaly(ats):
    if not ats.get("available"):
        return {"status":"unavailable","level":"暂无数据","z":None,"reason":ats.get("error") or "FINRA ATS数据不可用"}
    z=_finite(ats.get("ats_volume_z")); ch=_finite(ats.get("ats_share_change_pct"))
    if z is None and ch is None:return {"status":"inferred","level":"正常/数据不足","z":z,"reason":"缺少足够历史周度样本"}
    mag=max(abs(z or 0),abs(ch or 0)/2.5)
    return {"status":"inferred","level":"正常" if mag<1 else "关注" if mag<2 else "明显异常" if mag<3 else "极端异常","z":z,"share_change_pct":ch,"reason":"统计异常表示ATS活动偏离自身近期常态，不代表违法交易或买入/卖出方向。"}


def _earnings_dates(symbol, limit=40):
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
    except Exception:return []


def _ats_backtest(symbol, ats):
    if not ats.get("available"):
        return {"status":"unavailable","valid_samples":0,"score":None,"label":"暂无数据","rows":[],"reason":ats.get("error") or "ATS数据不可用"}
    weeks=ats.get("weeks") or []; today=pd.Timestamp.now().normalize(); earnings=[e for e in _earnings_dates(symbol,40) if pd.Timestamp(e).normalize()<=today]
    if not weeks or not earnings:return {"status":"insufficient","valid_samples":0,"score":None,"label":"样本不足","rows":[],"reason":"ATS周度数据或历史财报日期不足"}
    try:
        # FINRA weeklySummary is rolling 12 months. The backtest therefore has a
        # clean, honest 12-month horizon instead of pretending 12 retained weeks
        # represent four years of history.
        h=yf.Ticker(symbol).history(period="2y",interval="1d",auto_adjust=False)
        if h is None or h.empty:return {"status":"insufficient","valid_samples":0,"score":None,"label":"行情不足","rows":[]}
        close=pd.to_numeric(h["Close"],errors="coerce").dropna()
        rows=[]
        for ed in earnings:
            pre=[w for w in weeks if pd.Timestamp(w.get("week")).normalize()<=ed]
            if not pre:continue
            w=pre[-1]; week_ts=pd.Timestamp(w.get("week")).normalize(); delta=(ed-week_ts).days
            if delta<0 or delta>14:continue
            idx=close.index.tz_localize(None) if getattr(close.index,"tz",None) else close.index
            before=close[idx<=ed]; after=close[idx>=ed]
            if len(before)<21 or len(after)<4:continue
            p0=float(before.iloc[-1]); p3=float(after.iloc[3]); move=abs(p3/p0-1)*100 if p0 else None
            arr=close.values; pos=close.index.get_loc(before.index[-1]); pos=pos.stop-1 if isinstance(pos,slice) else int(pos)
            prior=[]
            for j in range(max(20,pos-40),min(pos-1,len(close)-4)+1):
                b=float(arr[j]); f=float(arr[j+3]);
                if b:prior.append(abs(f/b-1)*100)
            baseline=float(pd.Series(prior[-20:]).median()) if prior else None
            # Signal z is calculated from all prior ATS weeks available inside
            # the rolling 12-month window, not from the last 12 displayed rows.
            hist=[float(x.get("ats_shares") or 0) for x in weeks if pd.Timestamp(x.get("week")).normalize()<week_ts]
            hist=hist[-8:]
            mu=sum(hist)/len(hist) if len(hist)>=4 else None
            sd=(sum((x-mu)**2 for x in hist)/max(1,len(hist)-1))**0.5 if mu is not None else None
            az=((float(w.get("ats_shares") or 0)-mu)/sd) if sd and sd>0 else (0.0 if mu is not None else None)
            score=50+50*_clamp((az or 0)/3,-1,1)
            rows.append({"event_date":ed.date().isoformat(),"ats_share_pct":_finite(w.get("ats_share_pct")),"ats_z":az,"ats_score":round(score,1),"post_3d_abs_move_pct":round(move,3) if move is not None else None,"baseline_3d_abs_move_pct":round(baseline,3) if baseline is not None else None,"event_vol_ratio":round(move/baseline,3) if move is not None and baseline and baseline>0 else None})
        if not rows:return {"status":"insufficient","valid_samples":0,"score":None,"label":"样本不足","rows":[],"reason":"过去12个月没有足够的ATS周度→财报事件匹配样本"}
        ratios=[r["event_vol_ratio"] for r in rows if r.get("event_vol_ratio") is not None]
        high=[r for r in rows if r.get("ats_score",50)>=65 and r.get("event_vol_ratio") is not None]; low=[r for r in rows if r.get("ats_score",50)<65 and r.get("event_vol_ratio") is not None]
        hm=sum(r["event_vol_ratio"] for r in high)/len(high) if high else None; lm=sum(r["event_vol_ratio"] for r in low)/len(low) if low else None
        edge=((hm/lm)-1)*100 if hm is not None and lm not in (None,0) else None
        n=len(rows)
        # A numeric calibration score is only exposed at >=4 valid events. It is
        # an evidence score, not a prediction probability. Below that => no score.
        if n<4:
            score=None; label="低样本"; status="low_sample"
        else:
            base=50+(_clamp(edge,-30,30) if edge is not None else 0); sample_bonus=min(20,n*2); score=round(_clamp(base+sample_bonus,0,100),1); label="证据有限" if n<8 else ("有历史支持" if score>=60 else "证据有限"); status="backtested"
        return {"status":status,"valid_samples":n,"score":score,"label":label,"event_vol_edge_pct":round(edge,2) if edge is not None else None,"high_signal_mean_event_vol":round(hm,3) if hm is not None else None,"low_signal_mean_event_vol":round(lm,3) if lm is not None else None,"rows":rows[-20:],"lookback":"rolling 12 months","reason":"回测检验ATS异常与财报后绝对波动的历史关系；不把ATS周度汇总伪装成买卖方向命中率。样本不足时不生成分数。"}
    except Exception as exc:
        return {"status":"error","valid_samples":0,"score":None,"label":"回测失败","rows":[],"reason":str(exc)[:180]}


def analyze_guidance_ats(symbol: str):
    symbol=str(symbol or "").strip().upper()
    if not symbol:return {"ok":False,"error":"缺少标的"}
    now=time()
    with _LOCK:
        c=_CACHE.get("result:"+symbol)
        if c and now-c[0]<TTL:return c[1]
    try:
        # Read-only dependency: the original Whisper object is consumed as data;
        # this sidecar never writes to its cache or changes its code/weights.
        whisper=analyze_whisper(symbol)
        target_period={"target_end":whisper.get("target_period"),"last_actual_end":whisper.get("last_actual_period")}
        target_earnings_date=whisper.get("next_earnings_date")
        guidance=_sec_guidance(symbol,target_period,target_earnings_date)
        ats=ats_source(symbol)
        eps_cons=(whisper.get("eps") or {}).get("consensus"); rev_cons=(whisper.get("revenue") or {}).get("consensus")
        eps_g=_guidance_whisper((whisper.get("eps") or {}),guidance,whisper.get("fundamental_nowcast") or {},whisper.get("historical_surprise") or {},"eps")
        rev_g=_guidance_whisper((whisper.get("revenue") or {}),guidance,whisper.get("fundamental_nowcast") or {},whisper.get("historical_surprise") or {},"revenue")
        aw=_ats_whisper_score(ats,whisper); days=_finite(whisper.get("days_to_earnings")); temp=_temperature(ats,whisper,int(days) if days is not None else None); anomaly=_anomaly(ats); bt=_ats_backtest(symbol,ats)
        four_layer=_four_layer_expectation(whisper, {"revenue":rev_cons,"eps":eps_cons}, {"revenue":rev_g["value"],"eps":eps_g["value"],"status":"inferred" if eps_g["value"] is not None or rev_g["value"] is not None else "unavailable","revenue_gap_vs_sell_side_pct":_gap_pct(rev_g["value"],rev_cons),"eps_gap_vs_sell_side_pct":_gap_pct(eps_g["value"],eps_cons),"method":"Management Guidance-centered independent blend: official management guidance midpoint + Sell-side Consensus + Fundamental Nowcast + small revision/history adjustments."}, aw)
        result={
            "ok":True,"symbol":symbol,"as_of":datetime.now(timezone.utc).isoformat(),"independent":True,
            "sell_side":{"eps":eps_cons,"revenue":rev_cons,"earnings_date":target_earnings_date,"days_to_earnings":whisper.get("days_to_earnings")},
            "management_guidance":guidance,
            "guidance_whisper":{
                "eps":eps_g["value"],"revenue":rev_g["value"],
                "status":"inferred" if eps_g["value"] is not None or rev_g["value"] is not None else "unavailable",
                "reason":eps_g.get("reason") or rev_g.get("reason"),
                "eps_gap_vs_sell_side_pct":_gap_pct(eps_g["value"],eps_cons),"revenue_gap_vs_sell_side_pct":_gap_pct(rev_g["value"],rev_cons),
                "eps_components":eps_g["components"],"revenue_components":rev_g["components"],"eps_adjustments":eps_g.get("adjustments",[]),"revenue_adjustments":rev_g.get("adjustments",[]),
                "method":"Management Guidance-centered independent blend: official management guidance midpoint + Sell-side Consensus + Fundamental Nowcast + small revision/history adjustments.",
            },
            "ats":ats,"ats_whisper":aw,"ats_backtest":bt,"anomaly_scanner":anomaly,"expectation_temperature":temp,
            "four_layer_expectation":four_layer,
            "comparison":{"guidance_vs_sell_side":"高于" if eps_g["value"] is not None and eps_cons is not None and eps_g["value"]>eps_cons else ("低于" if eps_g["value"] is not None and eps_cons is not None and eps_g["value"]<eps_cons else "无法判断"),"ats_direction":aw.get("direction"),"ats_vs_guidance":"独立第二意见，不覆盖Guidance Whisper"},
            "method_note":"本模块是独立研究层。管理层指引只接受SEC EDGAR 8-K/正式业绩发布中的可验证季度指引；未能严格匹配目标财季时显示暂无数据。ATS使用独立pro_ats.py，原AEL Whisper与pro_expectation.py不被修改。",
        }
        with _LOCK:_CACHE["result:"+symbol]=(now,result)
        return result
    except Exception as exc:
        return {"ok":False,"symbol":symbol,"error":str(exc)[:220],"independent":True}
