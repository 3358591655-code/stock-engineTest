from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import StringIO
import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import pandas as pd
import requests

FRED_BASE = 'https://fred.stlouisfed.org/graph/fredgraph.csv'
FED_FUNDS_RATE_URL = 'https://www.federalreserve.gov/FOMC/fundsrate.htm'
FOMC_CALENDAR_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
FED_PRESS_RELEASES_URL = 'https://www.federalreserve.gov/newsevents/pressreleases/2026-press-fomc.htm'
TREASURY_XML = 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml'
NYFED_LATEST = 'https://markets.newyorkfed.org/api/rates/all/latest.json'
BLS_API = 'https://api.bls.gov/publicAPI/v1/timeseries/data/'
POLY_SEARCH = 'https://gamma-api.polymarket.com/public-search'
POLY_FED_PAGE = 'https://polymarket.com/economy/fed'

# FRED remains the long-history fallback. Critical market data have official
# direct-source fallbacks so a FRED outage does not blank the whole dashboard.
SERIES = {
    'fedfunds': ('FEDFUNDS', '联邦基金有效利率', '%', 'daily'),
    'dgs2': ('DGS2', '美国2年期国债收益率', '%', 'daily'),
    'dgs10': ('DGS10', '美国10年期国债收益率', '%', 'daily'),
    'curve_10y2y': ('T10Y2Y', '10Y-2Y期限利差', '%', 'daily'),
    'cpi': ('CPIAUCSL', 'CPI消费者价格指数', 'index', 'monthly'),
    'unemployment': ('UNRATE', '失业率', '%', 'monthly'),
    'payrolls': ('PAYEMS', '非农就业人数', 'thousand', 'monthly'),
    'industrial_production': ('INDPRO', '工业生产指数', 'index', 'monthly'),
    'hy_spread': ('BAMLH0A0HYM2', '美国高收益债利差', '%', 'daily'),
    'broad_usd': ('DTWEXBGS', '美元广义指数', 'index', 'daily'),
}

BLS_SERIES = {
    'cpi': ('CUSR0000SA0', 'CPI消费者价格指数', 'index', 'monthly'),
    'unemployment': ('LNS14000000', '失业率', '%', 'monthly'),
    'payrolls': ('CES0000000001', '非农就业人数', 'thousand', 'monthly'),
}

_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 900
_HTTP_TIMEOUT = 8
_HEADERS = {'User-Agent': 'AEL-Pro-Macro/2.5.19.1'}


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _cached_get(key: str, loader, ttl: int = _CACHE_TTL):
    now = time.time()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and now - item['ts'] < ttl:
            return item['value']
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = {'ts': now, 'value': value}
    return value


def _request_json(url: str, *, params=None, method='get', json_body=None):
    if method == 'post':
        r = requests.post(url, params=params, json=json_body, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
    else:
        r = requests.get(url, params=params, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
    r.raise_for_status()
    return r.json()


def _clean_html(text: str) -> str:
    text = re.sub(r'<script.*?</script>', ' ', text, flags=re.S | re.I)
    text = re.sub(r'<style.*?</style>', ' ', text, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _latest_pair(df: pd.DataFrame, col: str):
    x = df[['DATE', col]].dropna()
    if x.empty:
        return None
    last = x.iloc[-1]
    prev = x.iloc[-2] if len(x) >= 2 else None
    return {
        'date': str(last['DATE'].date()),
        'value': _num(last[col]),
        'previous_date': str(prev['DATE'].date()) if prev is not None else None,
        'previous': _num(prev[col]) if prev is not None else None,
    }


def _period_change(df: pd.DataFrame, col: str, periods: int):
    x = df[['DATE', col]].dropna()
    if len(x) <= periods:
        return None
    return _num(x.iloc[-1][col] - x.iloc[-1 - periods][col])


def _yoy(df: pd.DataFrame, col: str, observations: int):
    x = df[['DATE', col]].dropna()
    if len(x) <= observations:
        return None
    old = _num(x.iloc[-1 - observations][col])
    new = _num(x.iloc[-1][col])
    if old in (None, 0) or new is None:
        return None
    return (new / old - 1) * 100


def _payload_from_df(key: str, df: pd.DataFrame, source: str, source_url: str) -> dict[str, Any]:
    sid, name, unit, frequency = SERIES[key]
    latest = _latest_pair(df, 'VALUE')
    if latest is None:
        raise RuntimeError(f'{name}无有效观测值')
    if frequency == 'monthly':
        yoy = _yoy(df, 'VALUE', 12)
        change_3m = _period_change(df, 'VALUE', 3)
    else:
        yoy = _period_change(df, 'VALUE', 252)
        change_3m = _period_change(df, 'VALUE', 63)
    return {
        'series_id': sid,
        'name': name,
        'unit': unit,
        'frequency': frequency,
        'latest': latest,
        'change_3m': change_3m,
        'change_yoy': yoy,
        'observations': int(len(df)),
        'source': source,
        'source_url': source_url,
    }


def _fred_series(series_id: str, limit_years: int = 12) -> pd.DataFrame:
    def load():
        params = {'id': series_id, 'cosd': f'{datetime.now().year - limit_years}-01-01'}
        r = requests.get(FRED_BASE, params=params, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        if 'DATE' not in df.columns or series_id not in df.columns:
            raise RuntimeError(f'FRED {series_id}返回字段不完整')
        df['DATE'] = pd.to_datetime(df['DATE'], errors='coerce')
        df[series_id] = pd.to_numeric(df[series_id], errors='coerce')
        df = df.dropna(subset=['DATE', series_id]).sort_values('DATE')
        if df.empty:
            raise RuntimeError(f'FRED {series_id}无有效数据')
        return df[['DATE', series_id]].rename(columns={series_id: 'VALUE'}).reset_index(drop=True)
    return _cached_get(f'fred:{series_id}:{limit_years}', load)


def _fred_payload(key: str) -> dict[str, Any]:
    sid = SERIES[key][0]
    df = _fred_series(sid)
    return _payload_from_df(key, df, 'FRED', f'{FRED_BASE}?id={sid}')


def _bls_payloads(keys: list[str]) -> dict[str, dict[str, Any]]:
    def load():
        ids = [BLS_SERIES[k][0] for k in keys]
        now_year = datetime.now().year
        body = {'seriesid': ids, 'startyear': str(now_year - 4), 'endyear': str(now_year)}
        data = _request_json(BLS_API, method='post', json_body=body)
        if data.get('status') != 'REQUEST_SUCCEEDED':
            raise RuntimeError('BLS API未成功返回数据')
        out = {}
        for series in data.get('Results', {}).get('series', []):
            sid = series.get('seriesID')
            key = next((k for k, v in BLS_SERIES.items() if v[0] == sid), None)
            if not key:
                continue
            rows = []
            for item in series.get('data', []):
                period = item.get('period', '')
                if not re.fullmatch(r'M(?:0[1-9]|1[0-2])', period):
                    continue
                dt = pd.Timestamp(year=int(item['year']), month=int(period[1:]), day=1)
                val = _num(item.get('value'))
                if val is not None:
                    rows.append({'DATE': dt, 'VALUE': val})
            if rows:
                out[key] = _payload_from_df(key, pd.DataFrame(rows).sort_values('DATE'), 'BLS', 'https://www.bls.gov/developers/api_signature_v2.htm')
        return out
    return _cached_get('bls:core', load, ttl=1800)


def _treasury_curve() -> dict[str, dict[str, Any]]:
    def load():
        year = datetime.now().year
        url = f'{TREASURY_XML}?data=daily_treasury_yield_curve&field_tdr_date_value={year}'
        r = requests.get(url, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        rows = []
        for entry in root.iter():
            if entry.tag.rsplit('}', 1)[-1] != 'entry':
                continue
            vals = {}
            for child in entry.iter():
                local = child.tag.rsplit('}', 1)[-1]
                if local in {'NEW_DATE', 'BC_2YEAR', 'BC_10YEAR'}:
                    vals[local] = (child.text or '').strip()
            if vals.get('NEW_DATE'):
                rows.append(vals)
        if not rows:
            raise RuntimeError('Treasury收益率XML无有效记录')
        df = pd.DataFrame(rows)
        df['DATE'] = pd.to_datetime(df['NEW_DATE'], errors='coerce')
        out = {}
        for col, key in [('BC_2YEAR', 'dgs2'), ('BC_10YEAR', 'dgs10')]:
            if col not in df.columns:
                continue
            x = df[['DATE', col]].rename(columns={col: 'VALUE'}).copy()
            x['VALUE'] = pd.to_numeric(x['VALUE'], errors='coerce')
            x = x.dropna(subset=['DATE', 'VALUE']).sort_values('DATE')
            if not x.empty:
                out[key] = _payload_from_df(key, x, '美国财政部', url)
        if 'dgs2' in out and 'dgs10' in out:
            # Build a spread series from the same Treasury observations.
            a = df[['DATE', 'BC_2YEAR', 'BC_10YEAR']].copy()
            a['BC_2YEAR'] = pd.to_numeric(a['BC_2YEAR'], errors='coerce')
            a['BC_10YEAR'] = pd.to_numeric(a['BC_10YEAR'], errors='coerce')
            a['VALUE'] = a['BC_10YEAR'] - a['BC_2YEAR']
            a = a.dropna(subset=['DATE', 'VALUE']).sort_values('DATE')[['DATE', 'VALUE']]
            if not a.empty:
                p = _payload_from_df('curve_10y2y', a, '美国财政部', url)
                out['curve_10y2y'] = p
        return out
    return _cached_get('treasury:curve', load, ttl=900)


def _nyfed_effr() -> dict[str, Any]:
    def load():
        data = _request_json(NYFED_LATEST)
        rows = [x for x in data.get('refRates', []) if x.get('type') == 'EFFR']
        if not rows:
            raise RuntimeError('纽约联储未返回EFFR')
        rows = sorted(rows, key=lambda x: x.get('effectiveDate', ''))
        latest = rows[-1]
        return {
            'series_id': 'EFFR', 'name': '联邦基金有效利率', 'unit': '%', 'frequency': 'daily',
            'latest': {'date': latest.get('effectiveDate'), 'value': _num(latest.get('percentRate'))},
            'source': '纽约联储', 'source_url': 'https://www.newyorkfed.org/markets/reference-rates/effr',
            'observations': len(rows),
        }
    return _cached_get('nyfed:effr', load, ttl=300)


def _fraction_number(token: str):
    token = str(token).strip()
    simple = {'1/4': 0.25, '1/2': 0.5, '3/4': 0.75}
    if token in simple:
        return simple[token]
    if '-' in token:
        whole, frac = token.split('-', 1)
        if frac in simple:
            return float(whole) + simple[frac]
    return _num(token)


def _parse_target_range(text: str):
    pat = r'target range.*?to\s+([0-9]+(?:\.[0-9]+)?(?:-(?:1/4|1/2|3/4))?)\s+to\s+([0-9]+(?:\.[0-9]+)?(?:-(?:1/4|1/2|3/4))?)\s+percent'
    m = re.search(pat, text, flags=re.I)
    if not m:
        pat2 = r'federal funds rate.*?to\s+([0-9]+(?:\.[0-9]+)?(?:-(?:1/4|1/2|3/4))?)\s+to\s+([0-9]+(?:\.[0-9]+)?(?:-(?:1/4|1/2|3/4))?)\s+percent'
        m = re.search(pat2, text, flags=re.I)
    if not m:
        return None
    lo, hi = _fraction_number(m.group(1)), _fraction_number(m.group(2))
    if lo is None or hi is None:
        return None
    return f'{lo:.2f}–{hi:.2f}%'


def _fed_policy() -> dict[str, Any]:
    def load():
        errors=[]
        # Use the official FOMC calendar to identify the latest completed
        # meeting, then parse that meeting's official statement. This avoids
        # using today's date as the decision date when the funds-rate page
        # changes its HTML layout.
        try:
            cal = _fomc_calendar()
            today = datetime.now(timezone.utc).date()
            past = [m for m in cal.get('meetings', []) if datetime.fromisoformat(m['end']).date() <= today]
            if not past:
                raise RuntimeError('没有已完成的FOMC会议')
            meeting = past[-1]
            stamp = meeting['end'].replace('-', '')
            url = f'https://www.federalreserve.gov/newsevents/pressreleases/monetary{stamp}a.htm'
            r = requests.get(url, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
            r.raise_for_status()
            body = _clean_html(r.text)
            level = _parse_target_range(body)
            if not level:
                raise RuntimeError('FOMC官方声明未解析到目标利率区间')
            inc = re.search(r'raise(?:d)?\s+the target range.*?by\s+((?:\d+(?:\.\d+)?(?:-(?:1/4|1/2|3/4))?)|(?:1/4|1/2|3/4))\s+(?:percentage point|percentage points)', body, flags=re.I)
            dec = re.search(r'lower(?:ed)?\s+the target range.*?by\s+((?:\d+(?:\.\d+)?(?:-(?:1/4|1/2|3/4))?)|(?:1/4|1/2|3/4))\s+(?:percentage point|percentage points)', body, flags=re.I)
            action = '维持不变'
            inc_bp = None; dec_bp = None
            if inc:
                inc_bp = int(round((_fraction_number(inc.group(1)) or 0)*100)); action = f'加息 {inc_bp}bp'
            elif dec:
                dec_bp = int(round((_fraction_number(dec.group(1)) or 0)*100)); action = f'降息 {dec_bp}bp'
            return {
                'latest': {'date': meeting['end'], 'level': level, 'increase_bp': inc_bp, 'decrease_bp': dec_bp, 'action': action},
                'source': 'Federal Reserve', 'source_url': url,
            }
        except Exception as exc:
            errors.append(str(exc))
        # Secondary parser: current official funds-rate page.
        try:
            r = requests.get(FED_FUNDS_RATE_URL, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
            r.raise_for_status()
            text = _clean_html(r.text)
            level = _parse_target_range(text)
            if level:
                return {'latest': {'date': None, 'level': level, 'increase_bp': None, 'decrease_bp': None, 'action': '暂无数据'}, 'source': 'Federal Reserve', 'source_url': FED_FUNDS_RATE_URL}
        except Exception as exc:
            errors.append(str(exc))
        raise RuntimeError('；'.join(errors)[:320])
    return _cached_get('fed:policy', load, ttl=900)

def _fomc_calendar() -> dict[str, Any]:
    def load():
        r = requests.get(FOMC_CALENDAR_URL, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
        r.raise_for_status()
        text = _clean_html(r.text)
        year = datetime.now().year
        start_marker = f'{year} FOMC Meetings'
        year_pos = text.find(start_marker)
        scope = text[year_pos:] if year_pos >= 0 else text[:20000]
        # The page includes only the current year's table before the next year block.
        next_year_marker = f'{year + 1} FOMC Meetings'
        next_pos = scope.find(next_year_marker)
        if next_pos > 0:
            scope = scope[:next_pos]
        months = ['January','February','March','April','May','June','July','August','September','October','November','December']
        pattern = r'(' + '|'.join(months) + r')\s+(\d{1,2})(?:-(\d{1,2}))?'
        meetings = []
        for mon, d1, d2 in re.findall(pattern, scope):
            dt1 = datetime(year, months.index(mon)+1, int(d1)).date()
            dt2 = datetime(year, months.index(mon)+1, int(d2 or d1)).date()
            meetings.append({'start': dt1.isoformat(), 'end': dt2.isoformat(), 'label': f'{_zh_month(mon)}{d1}日' + (f'-{d2}日' if d2 else ''), 'month_en': mon})
        unique=[]; seen=set()
        for m in meetings:
            if m['start'] not in seen:
                seen.add(m['start']); unique.append(m)
        unique=sorted(unique, key=lambda x:x['start'])
        today=datetime.now(timezone.utc).date()
        upcoming=next((m for m in unique if datetime.fromisoformat(m['end']).date() >= today), None)
        return {'year':year,'meetings':unique,'next_meeting':upcoming,'source':'Federal Reserve FOMC calendar','source_url':FOMC_CALENDAR_URL}
    return _cached_get('fed:fomc-calendar', load, ttl=21600)


def _parse_json_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try: return json.loads(value)
        except Exception: return []
    return []


def _zh_month(name: str) -> str:
    return {'January':'一月','February':'二月','March':'三月','April':'四月','May':'五月','June':'六月','July':'七月','August':'八月','September':'九月','October':'十月','November':'十一月','December':'十二月'}.get(name, name)


def _zh_poly_outcome(text: str) -> str:
    t=str(text or '').strip()
    low=t.lower()
    if low in {'yes','true'}: return '是'
    if low in {'no','false'}: return '否'
    if 'decrease' in low or 'lower' in low or 'cut' in low:
        m=re.search(r'(\d+(?:\.\d+)?)\s*(?:bps|basis points)', low)
        return f'降息{m.group(1)}个基点' if m else '降息'
    if 'increase' in low or 'raise' in low or 'hike' in low:
        m=re.search(r'(\d+(?:\.\d+)?)\s*(?:bps|basis points)', low)
        return f'加息{m.group(1)}个基点' if m else '加息'
    if 'no change' in low or 'unchanged' in low or 'maintain' in low:
        return '维持不变'
    return t


def _zh_poly_question(text: str) -> str:
    t=' '.join(str(text or '').split())
    low=t.lower()
    if 'there be no change' in low or 'no change in fed' in low:
        return '10月美联储会议：利率是否维持不变？'
    if 'decrease interest rates by 50 bps' in low or 'decrease the target rate by 50' in low:
        return '10月美联储会议：是否降息50个基点？'
    if 'decrease interest rates by 25 bps' in low or 'decrease the target rate by 25' in low:
        return '10月美联储会议：是否降息25个基点？'
    if 'increase interest rates by 50 bps' in low or 'increase the target rate by 50' in low:
        return '10月美联储会议：是否加息50个基点？'
    if 'increase interest rates by 25 bps' in low or 'increase the target rate by 25' in low:
        return '10月美联储会议：是否加息25个基点？'
    if 'increase interest rates' in low:
        return '10月美联储会议：是否加息？'
    if 'decrease interest rates' in low or 'cut interest rates' in low:
        return '10月美联储会议：是否降息？'
    if 'fed decision' in low:
        return '美联储下一次利率决定'
    return t


def _polymarket_fed() -> dict[str, Any]:
    def load():
        queries = ['Fed Decision in October', 'Fed Decision']
        candidates=[]
        errors=[]
        for q in queries:
            try:
                params={'q': q, 'events_status': 'active', 'limit_per_type': 20, 'page': 1, 'search_profiles': 'false'}
                data=_request_json(POLY_SEARCH, params=params)
                candidates.extend(data.get('events') or [])
            except Exception as exc:
                errors.append(str(exc))
        # De-duplicate events and keep active Fed decision events closest to the next FOMC.
        uniq={str(e.get('id')):e for e in candidates if e.get('id')}
        fed_events=[]
        for e in uniq.values():
            title=str(e.get('title') or '')
            if 'fed' in title.lower() and ('decision' in title.lower() or 'rate' in title.lower()):
                if e.get('active') is not False and e.get('closed') is not True:
                    fed_events.append(e)
        fed_events.sort(key=lambda e: float(e.get('volume24hr') or e.get('volume') or 0), reverse=True)
        if not fed_events:
            raise RuntimeError('Polymarket未找到正在进行的Fed决策市场' + (f'：{errors[0]}' if errors else ''))
        event=fed_events[0]
        markets=event.get('markets') or []
        rows=[]
        for m in markets:
            outcomes=_parse_json_list(m.get('outcomes'))
            prices=_parse_json_list(m.get('outcomePrices'))
            if not outcomes or not prices:
                continue
            probs=[]
            for i,outcome in enumerate(outcomes):
                p=_num(prices[i]) if i < len(prices) else None
                if p is not None:
                    probs.append({'outcome': str(outcome), 'probability': p*100})
            if probs:
                rows.append({'question':m.get('question'), 'question_zh':_zh_poly_question(m.get('question')), 'slug':m.get('slug'), 'url':f"https://polymarket.com/event/{m.get('slug')}" if m.get('slug') else None, 'probabilities':[dict(x, outcome_zh=_zh_poly_outcome(x.get('outcome'))) for x in probs], 'volume':_num(m.get('volume')), 'volume_24h':_num(m.get('volume24hr')), 'liquidity':_num(m.get('liquidity')), 'active':m.get('active'), 'updated_at':m.get('updatedAt')})
        return {'event_title':event.get('title'), 'event_title_zh':'美联储利率决定市场预期', 'event_slug':event.get('slug'), 'event_url':f"https://polymarket.com/event/{event.get('slug')}" if event.get('slug') else POLY_FED_PAGE, 'markets':rows[:8], 'source':'Polymarket', 'source_url':POLY_FED_PAGE, 'as_of':datetime.now(timezone.utc).isoformat(), 'errors':errors}
    return _cached_get('polymarket:fed', load, ttl=180)


def _regime(series: dict[str, Any]) -> dict[str, Any]:
    def val(k):
        try: return series[k]['latest']['value']
        except Exception: return None
    def yoy(k):
        try: return series[k]['change_yoy']
        except Exception: return None
    cpi_yoy=yoy('cpi'); unemp=val('unemployment'); curve=val('curve_10y2y'); hy=val('hy_spread')
    return {
        'inflation': '通胀偏高' if cpi_yoy is not None and cpi_yoy >= 3 else ('通胀温和' if cpi_yoy is not None and cpi_yoy >= 2 else ('通胀偏低' if cpi_yoy is not None else '暂无数据')),
        'labor': '就业偏弱' if unemp is not None and unemp >= 5 else ('就业正常' if unemp is not None and unemp >= 4 else ('就业偏紧' if unemp is not None else '暂无数据')),
        'yield_curve': '曲线倒挂' if curve is not None and curve < 0 else ('曲线正斜率' if curve is not None else '暂无数据'),
        'credit': '信用利差偏高' if hy is not None and hy >= 5 else ('信用利差中性' if hy is not None and hy >= 3 else ('信用利差较低' if hy is not None else '暂无数据')),
    }


def analyze_macro() -> dict[str, Any]:
    errors=[]; data={}; source_status={}

    # Direct official sources first for critical series.
    try:
        data.update(_treasury_curve()); source_status['美国财政部']='正常'
    except Exception as exc:
        source_status['美国财政部']='异常'; errors.append(f'美国财政部: {str(exc)[:180]}')
    try:
        data['fedfunds']=_nyfed_effr(); source_status['纽约联储']='正常'
    except Exception as exc:
        source_status['纽约联储']='异常'; errors.append(f'纽约联储: {str(exc)[:180]}')
    try:
        bls=_bls_payloads(['cpi','unemployment','payrolls']); data.update(bls)
        source_status['BLS']=f'正常（{len(bls)}/3）'
    except Exception as exc:
        source_status['BLS']='异常'; errors.append(f'BLS: {str(exc)[:180]}')

    # FRED is still used for long-history / niche series and as a fallback for
    # anything the direct source did not provide.
    fred_keys=['fedfunds','dgs2','dgs10','curve_10y2y','cpi','unemployment','payrolls','industrial_production','hy_spread','broad_usd']
    def fetch(key): return key, _fred_payload(key)
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix='ael-fred') as pool:
        futures={pool.submit(fetch,k):k for k in fred_keys if k not in data or k in {'industrial_production','hy_spread','broad_usd'}}
        for future in as_completed(futures):
            key=futures[future]
            try:
                k,p=future.result()
                if k not in data:
                    data[k]=p
                source_status.setdefault('FRED','正常')
            except Exception as exc:
                source_status.setdefault('FRED','部分异常'); errors.append(f'FRED/{key}: {str(exc)[:180]}')

    policy=None; calendar=None; poly=None
    try: policy=_fed_policy(); source_status['Federal Reserve']='正常'
    except Exception as exc: source_status['Federal Reserve']='异常'; errors.append(f'Federal Reserve: {str(exc)[:180]}')
    try: calendar=_fomc_calendar()
    except Exception as exc: errors.append(f'FOMC日历: {str(exc)[:180]}')
    try: poly=_polymarket_fed(); source_status['Polymarket']='正常'
    except Exception as exc: source_status['Polymarket']='异常'; errors.append(f'Polymarket: {str(exc)[:180]}')

    missing=[k for k in SERIES if k not in data]
    return {
        'as_of':datetime.now(timezone.utc).isoformat(),
        'policy':policy,
        'fomc_calendar':calendar,
        'series':data,
        'polymarket':poly,
        'regime':_regime(data),
        'data_quality':{
            'series_available':len([k for k in SERIES if k in data]), 'series_total':len(SERIES),
            'missing_series':missing, 'errors':errors[:30], 'source_status':source_status,
        },
        'method_note':'宏观数据优先使用官方直连源；FRED作为统一历史序列与备用源；Polymarket仅展示市场隐含概率，不等同于美联储官方预测。任何源失败都不会用估算值补齐，也不会修改Lite股票评分。',
    }
