from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid
from time import time
from io import BytesIO
from datetime import datetime, timezone
import requests
import yfinance as yf
import pandas as pd
from metrics import build_dashboard, technical_analysis, fibonacci_levels, pivot_levels, morningstar_stock_rating
from pro_options import router as pro_options_router
from pro_factor import analyze_factor
from pro_risk import analyze_risk, analyze_portfolio
from pro_macro import analyze_macro
from pro_backtest import run_backtest
from pro_expectation import analyze_expectation
from pro_whisper import analyze_whisper, invalidate_whisper_cache
from pro_whisper_backtest import run_whisper_backtest
from ael_treasury import analyze_treasury
from asset_data import get_asset, get_asset_index, get_asset_news

BASE = Path(__file__).resolve().parent
APP_VERSION = '2.6.11-LITE-SCAN-MORNINGSTAR-REPAIR'
app = FastAPI(title='AEL 股票基本面驾驶舱', version=APP_VERSION)
# Pro is an extension layer. It has independent routes and never changes Lite scan/core logic.
app.include_router(pro_options_router)

# MARKET SCAN is deliberately separated from SINGLE. The scanner only pulls
# lightweight market-directory metadata plus batched daily history; it never
# calls build_dashboard() for thousands of symbols.
DEFAULT_SCAN_UNIVERSE = [
    'AAPL','MSFT','NVDA','GOOGL','AMZN','META','AVGO','AMD','TSLA','JPM','V','BRK-B',
    '0700.HK','9988.HK','3690.HK','1299.HK','0941.HK','0883.HK','1810.HK','9618.HK','0005.HK','0388.HK','2318.HK','2269.HK',
    '600519.SS','601318.SS','600036.SS','600900.SS','601888.SS','600276.SS','000858.SZ','000333.SZ','002594.SZ','300750.SZ','601398.SS','601288.SS'
]

# V2.5.4: filter the universe before history download, then scan history in
# bounded parallel batches. Four workers avoids the latency of serial scanning
# without turning the Yahoo session into an uncontrolled request fan-out.
SCAN_BATCH_SIZE = max(50, min(250, int(os.getenv('AEL_SCAN_BATCH_SIZE', '200'))))
SCAN_WORKERS = max(1, min(6, int(os.getenv('AEL_SCAN_WORKERS', '4'))))
SCAN_CACHE_TTL = int(os.getenv('AEL_SCAN_CACHE_TTL', '900'))
UNIVERSE_CACHE_TTL = int(os.getenv('AEL_UNIVERSE_CACHE_TTL', '86400'))
INDEX_UNIVERSE_CACHE_TTL = int(os.getenv('AEL_INDEX_UNIVERSE_CACHE_TTL', '86400'))
SCREENER_PAGE_SIZE = 250
# 0 = follow Yahoo's reported total dynamically; no artificial 12,000-symbol cap.
# A positive value remains available as an emergency operator override.
SCREENER_MAX_SYMBOLS = int(os.getenv('AEL_SCAN_MAX_SYMBOLS', '0'))

# Primary MARKET SCAN market-cap gates. Yahoo's screener values are scoped
# to the selected regional market, so the thresholds below are expressed in
# that market's local currency: USD / HKD / CNY respectively.
MARKET_CAP_MIN = {'us': 20_000_000_000, 'hk': 30_000_000_000, 'cn': 30_000_000_000}
MARKET_CAP_CURRENCY = {'us': 'USD', 'hk': 'HKD', 'cn': 'CNY'}
# Yahoo exchange codes used for OTC/Pink Sheet listings. These are isolated
# from the primary US scan and therefore cannot consume TOP20 or scan time.
US_OTC_EXCHANGES = {'PNK', 'OQB', 'OQX', 'OTC'}

# V2.5.5: independent sector/industry scan presets. Values are Yahoo Finance
# screener industry names; one preset may map to multiple industries.
INDEX_UNIVERSES = {
    'us': {
        'label': '道琼斯 + 纳斯达克100 + 标普500',
        'codes': ['dowjones','nasdaq100','sp500'],
    },
    'hk': {'label': '恒生指数成分股', 'codes': ['hsi']},
    'cn': {'label': '中证500 + 科创板', 'codes': ['csi500'], 'star_from_yahoo': True},
}
INDEX_CONSTITUENT_BASE = 'https://yfiua.github.io/index-constituents/constituents-{code}.csv'
US_SECTOR_SOURCES = [
    ('sp500', 'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv'),
    ('nasdaq100', 'https://raw.githubusercontent.com/Gary-Strauss/NASDAQ100_Constituents/main/data/nasdaq100_constituents.csv'),
]
SECTOR_CACHE_TTL = int(os.getenv('AEL_SECTOR_CACHE_TTL', '86400'))
_SECTOR_CACHE = {}

SCAN_GROUPS = {
    'all': {'label': '核心指数池', 'industries': []},
    'semiconductors': {'label': '半导体', 'industries': ['Semiconductors']},
    'semiconductor_equipment': {'label': '半导体设备', 'industries': ['Semiconductor Equipment & Materials']},
    'software': {'label': '软件', 'industries': ['Software—Application', 'Software—Infrastructure']},
    'banks': {'label': '银行', 'industries': ['Banks—Diversified', 'Banks—Regional']},
    'insurance': {'label': '保险', 'industries': ['Insurance—Life', 'Insurance—Diversified', 'Insurance—Property & Casualty', 'Insurance—Specialty']},
    'biotechnology': {'label': '生物科技', 'industries': ['Biotechnology']},
    'pharmaceuticals': {'label': '制药', 'industries': ['Drug Manufacturers—General', 'Drug Manufacturers—Specialty & Generic']},
}

_SCAN_CACHE = {}
_UNIVERSE_CACHE = {}
_FUNDAMENTAL_CACHE = {}
_FUNDAMENTAL_CACHE_TTL = int(os.getenv('AEL_FUNDAMENTAL_CACHE_TTL', '21600'))
ROE_MIN_PCT = float(os.getenv('AEL_ROE_MIN_PCT', '10'))
FUNDAMENTAL_WORKERS = max(1, min(10, int(os.getenv('AEL_FUNDAMENTAL_WORKERS', '8'))))
# Fast-first quality validation: check a smaller technical shortlist first; only
# expand when too few names qualify. This cuts hundreds of slow per-symbol
# fundamental requests on normal scans without weakening the final TOP20 gate.
FUNDAMENTAL_CANDIDATES = max(60, min(200, int(os.getenv('AEL_FUNDAMENTAL_CANDIDATES', '160'))))
FUNDAMENTAL_FIRST_PASS = max(40, min(FUNDAMENTAL_CANDIDATES, int(os.getenv('AEL_FUNDAMENTAL_FIRST_PASS', '80'))))
FUNDAMENTAL_MIN_PASSED = max(20, min(20, int(os.getenv('AEL_FUNDAMENTAL_MIN_PASSED', '20'))))
SCAN_HISTORY_PERIOD = os.getenv('AEL_SCAN_HISTORY_PERIOD', '1y')
# Lite quality controls: keep hard gates conservative, then rank by business quality
# so technical heat alone cannot push speculative/junk names into TOP20.
SPECULATION_HARD_LIMIT = float(os.getenv('AEL_LITE_SPECULATION_HARD_LIMIT', '80'))
_SCAN_JOBS = {}
_SCAN_LOCK = threading.Lock()

app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

@app.get('/')
def index():
    return FileResponse(BASE / 'static' / 'index.html')

@app.get('/api/health')
def health():
    return {'ok': True, 'service': 'stock-fundamental-dashboard', 'version': APP_VERSION}

@app.get('/api/assets/index')
def assets_index():
    # Static local metadata only; never touches the stock core path.
    return {'assets': get_asset_index(), 'count': len(get_asset_index())}


@app.get('/api/asset/core/{symbol}')
def asset_core(symbol: str):
    try:
        data = get_asset(symbol)
        if not data.get('ok'):
            raise HTTPException(status_code=404, detail=data.get('error') or '暂无数据')
        return data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'多资产行情获取失败：{str(exc)[:180]}')


@app.get('/api/asset/news/{symbol}')
def asset_news(symbol: str):
    # Optional/lazy only. News never blocks the multi-asset quote or stock core.
    try:
        return get_asset_news(symbol)
    except Exception as exc:
        return {'ok': False, 'symbol': symbol.upper(), 'news': [], 'error': str(exc)[:180]}


@app.get('/api/pro/expectation/{symbol}')
def pro_expectation(symbol: str):
    # Optional, on-demand Pro research only. Never called by Lite.
    try:
        return analyze_expectation(symbol)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'买方预期研究失败：{str(exc)[:180]}')

@app.get('/api/pro/whisper/backtest/{symbol}')
def pro_whisper_backtest(
    symbol: str,
    quarters: int = Query(20, ge=8, le=40),
):
    # Optional validation layer. It never runs on the normal Whisper request and
    # never participates in Lite/SINGLE/MARKET SCAN. A completed run updates the
    # in-process calibration cache consumed by the live Confidence field.
    try:
        result = run_whisper_backtest(symbol, quarters)
        if result.get("ok"):
            invalidate_whisper_cache(symbol)
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'AEL Whisper 回测失败：{str(exc)[:220]}')

@app.get('/api/pro/whisper/{symbol}')
def pro_whisper(symbol: str):
    # Explicit numerical Market-Implied Whisper layer. Independent from Lite.
    try:
        return analyze_whisper(symbol)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'AEL暗盘预期研究失败：{str(exc)[:180]}')


@app.get('/api/stock/core/{symbol}')
def stock_core(symbol: str):
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail='请输入股票代码')
    try:
        return build_dashboard(symbol, include_slow=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'核心数据获取失败：{exc}')

@app.get('/api/pro/factors/analyze/{symbol}')
def pro_factor_analyze(
    symbol: str,
    value_weight: float = Query(20, ge=0, le=100),
    quality_weight: float = Query(25, ge=0, le=100),
    growth_weight: float = Query(15, ge=0, le=100),
    momentum_weight: float = Query(20, ge=0, le=100),
    risk_weight: float = Query(10, ge=0, le=100),
    liquidity_weight: float = Query(10, ge=0, le=100),
):
    try:
        weights = {
            'value': value_weight, 'quality': quality_weight, 'growth': growth_weight,
            'momentum': momentum_weight, 'risk': risk_weight, 'liquidity': liquidity_weight,
        }
        return analyze_factor(symbol, weights)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Factor Lab 数据获取失败：{exc}')


@app.get('/api/pro/risk/analyze/{symbol}')
def pro_risk_analyze(symbol: str, benchmark: str = Query('SPY'), period: str = Query('2y', pattern='^(1y|2y|5y|max)$')):
    try:
        return analyze_risk(symbol, benchmark, period)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Risk Lab 数据获取失败：{exc}')


@app.get('/api/pro/risk/portfolio')
def pro_risk_portfolio(
    symbols: str = Query(..., min_length=1),
    weights: str = Query('', description='逗号分隔权重；留空则等权'),
    benchmark: str = Query('SPY'),
    period: str = Query('2y', pattern='^(1y|2y|5y|max)$'),
    capital: float | None = Query(None, gt=0),
):
    try:
        syms=[x.strip() for x in symbols.split(',') if x.strip()]
        ws=None if not weights.strip() else [float(x.strip()) for x in weights.split(',') if x.strip()]
        return analyze_portfolio(syms, ws, benchmark, period, capital)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'组合风险分析失败：{exc}')


@app.get('/api/pro/backtest')
def pro_backtest(
    symbols: str = Query(..., min_length=3),
    benchmark: str = Query('SPY'),
    strategy: str = Query('momentum', pattern='^(equal_weight|momentum|trend|low_vol)$'),
    period: str = Query('5y', pattern='^(3y|5y|10y|max)$'),
    rebalance: str = Query('monthly', pattern='^(monthly|quarterly|semiannual)$'),
    top_k: int = Query(5, ge=1, le=20),
    lookback: int = Query(120, ge=20, le=504),
    cost_bps: float = Query(10.0, ge=0, le=200),
    validation: str = Query('standard', pattern='^(standard|strict)$'),
):
    try:
        syms = [x.strip() for x in symbols.split(',') if x.strip()]
        return run_backtest(syms, benchmark, strategy, period, rebalance, top_k, lookback, cost_bps, validation)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'回测数据获取失败：{exc}')


@app.get('/api/pro/treasury')
def pro_treasury():
    # Independent, lazy Pro module. Failures never affect macro or stock core.
    try:
        return analyze_treasury()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'美国国债研究失败：{str(exc)[:180]}')

@app.get('/api/pro/macro')
def pro_macro():
    """Macro/Fed research endpoint. It is read-only and never changes Lite scores."""
    try:
        return analyze_macro()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'宏观/Fed 数据获取失败：{exc}')


@app.get('/api/pro/universe')
def pro_universe(
    market: str = Query('us', pattern='^(us|hk|cn)$'),
):
    """Read-only Pro universe view built from the same cached core-index pool.
    It never broadens the pool and never mutates Lite scan rules.
    """
    try:
        rows = _fetch_index_universe(market)
        cfg = INDEX_UNIVERSES[market]
        return {
            'market': market,
            'label': cfg['label'],
            'count': len(rows),
            'source': 'AEL core-index constituent cache',
            'cache_ttl_seconds': INDEX_UNIVERSE_CACHE_TTL,
            'as_of': datetime.now(timezone.utc).isoformat(),
            'symbols': [
                {
                    'symbol': r.get('symbol'),
                    'company': r.get('company') or r.get('symbol'),
                    'market': market,
                }
                for r in rows
            ],
            'data_quality': {
                'missing_company_names': sum(1 for r in rows if not r.get('company')),
                'empty_universe': len(rows) == 0,
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Universe 数据获取失败：{exc}')


@app.get('/api/stock/morningstar/{symbol}')
def stock_morningstar(symbol: str, exchange: str = Query('')):
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail='请输入股票代码')
    try:
        return morningstar_stock_rating(symbol, exchange)
    except Exception as exc:
        # This route is optional/lazy; never turn a stock page into an error.
        return {'symbol': symbol.upper(), 'rating': None, 'available': False, 'status': 'source_error',
                'source': 'Morningstar公开股票报价页', 'note': str(exc)[:180]}


@app.get('/api/stock/details/{symbol}')
def stock_details(symbol: str):
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail='请输入股票代码')
    try:
        return build_dashboard(symbol, include_slow=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'补充数据获取失败：{exc}')


def market_of(symbol: str):
    u = str(symbol).upper()
    if u.endswith('.HK'): return 'hk'
    if u.endswith('.SS') or u.endswith('.SZ'): return 'cn'
    return 'us'


def _logo_domain_from_info(info):
    raw=str((info or {}).get('website') or '').strip()
    if not raw: return None
    m=re.search(r'https?://(?:www\.)?([^/]+)', raw, flags=re.I)
    return m.group(1).lower() if m else None


def _scan_universe_override(markets: str):
    raw = os.getenv('AEL_SCAN_UNIVERSE', '').strip()
    if not raw:
        return None
    selected = {x.strip().lower() for x in markets.split(',') if x.strip()} if markets else {'us','hk','cn'}
    rows=[]
    for s in raw.split(','):
        symbol=s.strip()
        if symbol and market_of(symbol) in selected:
            rows.append({'symbol':symbol,'company':symbol,'sector':'未分类','industry':'未分类','exchange':'','currency':'','market_cap':None})
    return rows


def _yahoo_screener_page(region: str, offset: int = 0, size: int = SCREENER_PAGE_SIZE, industry: str | None = None):
    """Fetch one Yahoo equity-screener page through yfinance's managed session.

    Do not call query2.finance.yahoo.com directly here. Yahoo binds crumb to
    the cookie/session that obtained it; yfinance's YfData manages that pair.
    This is especially important on stateless Railway containers, where a
    manually assembled crumb/cookie request can return HTTP 401 Invalid Crumb.
    """
    if size < 1 or size > 250:
        raise ValueError('Yahoo screener page size must be between 1 and 250')
    try:
        # EquityQuery is already an equity screener; `quoteType` is not a
        # valid EquityQuery field in current yfinance and causes Railway to
        # fail before the request is sent. Region is sufficient to scope the
        # equity universe, while the screener endpoint itself returns equity
        # quotes.
        conditions=[yf.EquityQuery('eq', ['region', region.lower()])]
        if industry:
            conditions.append(yf.EquityQuery('eq', ['industry', industry]))
        query = conditions[0] if len(conditions) == 1 else yf.EquityQuery('and', conditions)
        result = yf.screen(
            query,
            offset=int(offset),
            size=int(size),
            sortField='ticker',
            sortAsc=True,
        )
    except Exception as exc:
        raise RuntimeError(f'Yahoo screener authentication/query failed: {exc}') from exc
    if not isinstance(result, dict):
        raise RuntimeError('Yahoo screener returned an invalid response')
    quotes = result.get('quotes') or []
    if not isinstance(quotes, list):
        raise RuntimeError('Yahoo screener returned invalid quotes')
    return result

def _extract_symbols_from_table(df, market):
    symbols=[]
    for col in df.columns:
        vals=df[col].astype(str)
        for v in vals:
            v=v.strip()
            if market=='hk':
                import re
                m=re.search(r'(?<!\d)(\d{1,5})(?:\.0)?(?!\d)', v)
                if m:
                    symbols.append(m.group(1).zfill(4)+'.HK')
            else:
                import re
                m=re.search(r'(?<!\d)([A-Z]{1,5})(?:\.0)?(?![A-Z])', v.upper())
                if m and m.group(1) not in {'SEHK','NYSE','NASDAQ','SYMBOL','TICKER'}:
                    symbols.append(m.group(1))
    return symbols


def _fetch_sector_metadata_us():
    cache=_SECTOR_CACHE.get('us')
    if cache and time()-cache['ts'] < SECTOR_CACHE_TTL:
        return cache['data']
    data={}
    for source,url in US_SECTOR_SOURCES:
        try:
            r=requests.get(url,headers={'User-Agent':'AEL-Lite/2.6.11'},timeout=5)
            r.raise_for_status()
            df=pd.read_csv(BytesIO(r.content),dtype=str)
            cols={str(c).strip().lower():c for c in df.columns}
            symcol=next((cols[k] for k in ('symbol','ticker') if k in cols),None)
            sec_col=next((cols[k] for k in ('gics sector','sector','gics_sector') if k in cols),None)
            sub_col=next((cols[k] for k in ('gics sub-industry','sub_industry','gics_sub_industry') if k in cols),None)
            name_col=next((cols[k] for k in ('security','company','name') if k in cols),None)
            if not symcol: continue
            for _,row in df.iterrows():
                sym=str(row.get(symcol) or '').strip().upper().replace('.', '-')
                if not sym: continue
                sec=str(row.get(sec_col) or '').strip() if sec_col else ''
                sub=str(row.get(sub_col) or '').strip() if sub_col else ''
                name=str(row.get(name_col) or '').strip() if name_col else ''
                if sec or sub or name:
                    data[sym]={'sector':sec or '未分类','industry':sub or '未分类','company':name or sym,'sector_source':source}
        except Exception:
            continue
    _SECTOR_CACHE['us']={'ts':time(),'data':data}
    return data


def _fetch_index_universe(market):
    cache=_UNIVERSE_CACHE.get(('INDEX',market))
    if cache and time()-cache['ts'] < INDEX_UNIVERSE_CACHE_TTL:
        return cache['rows']
    import re
    symbols=set(); names={}; source_parts=[]
    cfg=INDEX_UNIVERSES[market]
    headers={'User-Agent':'AEL-Lite/2.6.11 index-universe'}
    # US: use current public constituent tables with sector metadata first;
    # yfiua remains a fallback for membership if either table is temporarily unavailable.
    if market=='us':
        secmeta=_fetch_sector_metadata_us()
        for sym,meta in secmeta.items():
            if meta.get('sector') or meta.get('industry'):
                symbols.add(sym); names[sym]=meta.get('company') or sym
        if symbols:
            source_parts.extend(['S&P500/GICS','NASDAQ100/GICS'])
    for code in cfg.get('codes',[]):
        if market=='us' and symbols and code in {'sp500','nasdaq100'}:
            continue
        url=INDEX_CONSTITUENT_BASE.format(code=code)
        try:
            r=requests.get(url,headers=headers,timeout=5)
            r.raise_for_status()
            df=pd.read_csv(BytesIO(r.content),dtype=str)
            sym_col=next((c for c in df.columns if str(c).lower() in {'symbol','ticker','code'}),df.columns[0])
            name_col=next((c for c in df.columns if str(c).lower() in {'name','company','security'}),None)
            for _,row in df.iterrows():
                sym=str(row.get(sym_col) or '').strip().upper()
                if not sym: continue
                symbols.add(sym)
                if name_col: names[sym]=str(row.get(name_col) or sym)
            source_parts.append(code)
        except Exception:
            continue
    if market=='cn' and cfg.get('star_from_yahoo'):
        try:
            cn_rows=_discover_market('cn', industries=None)
            for r in cn_rows:
                sym=str(r.get('symbol') or '').upper()
                if re.fullmatch(r'688\d{3}\.SH',sym):
                    symbols.add(sym); names[sym]=r.get('company') or sym
            source_parts.append('STAR')
        except Exception:
            pass
    secmeta=_fetch_sector_metadata_us() if market=='us' else {}
    rows=[]
    for sym in sorted(symbols):
        meta=secmeta.get(sym,{})
        rows.append({'symbol':sym,'company':meta.get('company') or names.get(sym,sym),
                     'sector':meta.get('sector') or '未分类','industry':meta.get('industry') or '未分类',
                     'exchange':'','currency':'','market_cap':None,'market':market,
                     'index_universe':INDEX_UNIVERSES[market]['label'],
                     'sector_source':meta.get('sector_source') or ('index constituent source' if source_parts else 'unknown')})
    _UNIVERSE_CACHE[('INDEX',market)]={'ts':time(),'rows':rows}
    return rows

def _discover_market(region: str, industries=None):
    industries=tuple(industries or ())
    cache_key=(region, industries)
    cache=_UNIVERSE_CACHE.get(cache_key)
    if cache and time()-cache['ts'] < UNIVERSE_CACHE_TTL:
        return cache['rows']
    rows=[]
    query_industries=industries or (None,)
    for industry in query_industries:
        offset=0; total=None
        while True:
            result=_yahoo_screener_page(region, offset, SCREENER_PAGE_SIZE, industry=industry)
            quotes=result.get('quotes') or []
            total=int(result.get('total') or 0)
            if not quotes: break
            for q in quotes:
                symbol=str(q.get('symbol') or '').strip().upper()
                if not symbol: continue
                rows.append({
                    'symbol':symbol,
                    'company':q.get('longName') or q.get('shortName') or symbol,
                    'sector':q.get('sector') or '未分类',
                    'industry':q.get('industry') or '未分类',
                    'exchange':q.get('exchange') or '',
                    'currency':q.get('currency') or '',
                    'market_cap':q.get('marketCap'),
                })
            offset += len(quotes)
            if len(quotes) < SCREENER_PAGE_SIZE or (total and offset >= total): break
            if SCREENER_MAX_SYMBOLS > 0 and offset >= SCREENER_MAX_SYMBOLS: break
    dedup={r['symbol']:r for r in rows}
    rows=list(dedup.values())
    if SCREENER_MAX_SYMBOLS > 0: rows=rows[:SCREENER_MAX_SYMBOLS]
    _UNIVERSE_CACHE[cache_key]={'ts':time(),'rows':rows}
    return rows


def _is_us_otc(row):
    return str(row.get('exchange') or '').upper().strip() in US_OTC_EXCHANGES


def _passes_market_cap(row, market):
    value=row.get('market_cap')
    try:
        cap=float(value)
    except (TypeError, ValueError):
        return False
    return cap >= MARKET_CAP_MIN[market]


def _filter_primary_universe(rows, market):
    primary=[]
    otc=[]
    rejected_cap=0
    for raw in rows:
        row=dict(raw)
        if market == 'us' and _is_us_otc(row):
            otc.append(row)
            continue
        if not _passes_market_cap(row, market):
            rejected_cap += 1
            continue
        row['market']=market
        primary.append(row)
    return primary, otc, rejected_cap


def _enrich_rows_from_screener(rows):
    """Best-effort bulk enrichment. One/two Yahoo screener pages per market, never per symbol."""
    by_market={}
    for r in rows:
        by_market.setdefault(r.get('market') or market_of(r.get('symbol','')),[]).append(r)
    for mk,items in by_market.items():
        wanted={str(r.get('symbol') or '').upper() for r in items}
        if not wanted: continue
        region={'us':'us','hk':'hk','cn':'cn'}.get(mk,mk)
        # The screener is already bounded to large caps for Lite. Keep pages small
        # and stop once enough symbols have been found; missing fields remain missing.
        try:
            query=yf.EquityQuery('and',[yf.EquityQuery('eq',['region',region]),yf.EquityQuery('gte',['intradaymarketcap',MARKET_CAP_MIN.get(mk,0)])])
            result=yf.screen(query,offset=0,size=250,sortField='intradaymarketcap',sortAsc=False)
            quotes=result.get('quotes') or []
            meta={str(q.get('symbol') or '').upper():q for q in quotes if str(q.get('symbol') or '').upper() in wanted}
            for r in items:
                q=meta.get(str(r.get('symbol') or '').upper())
                if not q: continue
                r['sector']=q.get('sector') or r.get('sector') or '未分类'
                r['industry']=q.get('industry') or r.get('industry') or '未分类'
                r['company']=q.get('longName') or q.get('shortName') or r.get('company') or r.get('symbol')
                r['market_cap']=q.get('marketCap') or q.get('intradayMarketCap') or r.get('market_cap')
                r['screener_info']={k:q.get(k) for k in ('returnOnEquity','profitMargins','operatingMargins','freeCashflow','operatingCashflow','debtToEquity','totalStockholderEquity','trailingEps','totalRevenue','netIncomeToCommon','revenueGrowth','earningsGrowth','beta','marketCap') if q.get(k) is not None}
        except Exception:
            continue
    return rows


def _scan_universe(markets: str, group: str = 'all'):
    override=_scan_universe_override(markets)
    if override is not None and group == 'all':
        return override, 'environment override', {'us':0,'hk':0,'cn':0}, {'us':0,'hk':0,'cn':0}
    selected=[m.strip().lower() for m in markets.split(',') if m.strip() in {'us','hk','cn'}]
    if not selected: selected=['us']
    group_cfg=SCAN_GROUPS.get(group, SCAN_GROUPS['all'])
    industries=group_cfg['industries']
    rows=[]; otc_counts={'us':0,'hk':0,'cn':0}; cap_rejected={'us':0,'hk':0,'cn':0}
    for mk in selected:
        if industries:
            # Sector scans are still bounded by the selected core-index universe.
            # This prevents a 'semiconductor' scan from silently expanding back
            # to thousands of unrelated small-cap/OTC names.
            core=_fetch_index_universe(mk)
            core_symbols={r['symbol'] for r in core}
            market_rows=[r for r in _discover_market(mk, industries=industries) if r.get('symbol') in core_symbols]
            primary, otc, rejected_cap = _filter_primary_universe(market_rows, mk)
            otc_counts[mk]=len(otc); cap_rejected[mk]=rejected_cap
            rows.extend(primary)
        else:
            # Fixed core-index universe: membership itself is the primary
            # filter. We intentionally do NOT run the broad market-cap
            # directory here; that would undo the speed benefit of the index
            # universe. Index membership is refreshed/cached daily.
            market_rows=_fetch_index_universe(mk)
            for r in market_rows:
                if mk=='us' and _is_us_otc(r):
                    otc_counts[mk]+=1; continue
                x=dict(r); x['market']=mk; rows.append(x)
    if industries:
        source='Yahoo Finance industry screener → market-cap filter → OTC isolation'
    else:
        source='固定核心指数成分池 → 批量历史行情 → ROE + 市值硬门槛'
    return rows, source, otc_counts, cap_rejected

def _quality_score(data):
    """Bounded 0-100 business-quality score used only after hard eligibility gates."""
    parts=[]
    roe=data.get('roe')
    if roe is not None:
        parts.append((max(0,min(100,(roe-5)/20*100)),35,'ROE'))
    margin=data.get('profit_margin')
    if margin is not None:
        parts.append((max(0,min(100,(margin+0.02)/0.22*100)),20,'利润率'))
    if data.get('cash_quality') is not None:
        parts.append((float(data['cash_quality']),25,'现金流'))
    de=data.get('debt_to_equity')
    if de is not None:
        parts.append((100 if de<=50 else 80 if de<=100 else 60 if de<=200 else 35 if de<=300 else 10,20,'负债水平'))
    if not parts:
        return None
    return round(sum(v*w for v,w,_ in parts)/sum(w for _,w,_ in parts))


def _speculation_risk(tech):
    """Detect extreme price/volume heat without penalizing ordinary momentum."""
    ind=tech.get('indicators') or {}
    rsi=ind.get('rsi14')
    ret20=ind.get('momentum_20d')
    pos52=ind.get('52w_position')
    vol_ratio=ind.get('volume_ratio_20d')
    risk=0.0
    if rsi is not None and rsi>75: risk += min(30,(rsi-75)*2.0)
    if rsi is not None and rsi>82: risk += 15
    if ret20 is not None and ret20>20: risk += min(25,(ret20-20)*0.5)
    if ret20 is not None and ret20>40: risk += 15
    if pos52 is not None and pos52>90: risk += min(15,(pos52-90)*0.3)
    if vol_ratio is not None and vol_ratio>2: risk += min(20,(vol_ratio-2)*8)
    return round(min(100,risk),1)


def _fundamental_gate(symbol, market=None, market_cap_hint=None, sector=None, industry=None, tech=None):
    """Lite quality gate: real Yahoo business metrics + market-cap + anti-speculation.
    Financials use a sector-aware cash-flow rule because FCF is not a meaningful
    screening metric for banks/insurers. Missing critical data never passes.
    """
    cached=_FUNDAMENTAL_CACHE.get(symbol)
    cache_market_ok=(cached and cached.get('market')==market and cached.get('market_cap_hint')==market_cap_hint and cached.get('sector')==sector)
    if cache_market_ok and time()-cached['ts'] < _FUNDAMENTAL_CACHE_TTL:
        return cached['ok'], cached['data']
    try:
        hint = tech if isinstance(tech, dict) else {}
        # Yahoo screener rows may already contain the key fields; use them before
        # falling back to a per-symbol Ticker.info request. This is the main Lite
        # speed fix: the scanner should not turn 500 names into 500 serial API calls.
        info_hint = hint.get('screener_info') if isinstance(hint.get('screener_info'), dict) else {}
        if info_hint:
            info=dict(info_hint)
        else:
            t=yf.Ticker(symbol)
            info=t.info or {}
        def num(key):
            try:
                v=info.get(key)
                return float(v) if v is not None and pd.notna(v) else None
            except Exception:
                return None
        raw_roe=num('returnOnEquity'); roe=raw_roe*100 if raw_roe is not None else None
        market_cap=market_cap_hint if market_cap_hint is not None else num('marketCap')
        profit_margin=num('profitMargins')
        op_margin=num('operatingMargins')
        fcf=num('freeCashflow')
        ocf=num('operatingCashflow')
        debt_to_equity=num('debtToEquity')
        total_equity=num('totalStockholderEquity')
        trailing_eps=num('trailingEps')
        revenue=num('totalRevenue')
        net_income=num('netIncomeToCommon')
        revenue_growth=num('revenueGrowth')
        earnings_growth=num('earningsGrowth')
        beta=num('beta')
        roe_ok=roe is not None and roe >= ROE_MIN_PCT
        cap_ok=True if market not in MARKET_CAP_MIN else (market_cap is not None and market_cap >= MARKET_CAP_MIN[market])
        financial_sector=str(sector or '').lower() in {'financial services','financial','banks','insurance'} or str(industry or '').lower().startswith(('banks','insurance'))
        revenue_ok=(revenue is not None and revenue>0) if not financial_sector else True
        earnings_ok=((net_income is not None and net_income>0) or (trailing_eps is not None and trailing_eps>0))
        equity_ok=(total_equity is None or total_equity>0)
        if financial_sector:
            cash_quality=70 if ocf is not None and ocf>0 else None
            cash_ok=True
        else:
            cash_quality=100 if fcf is not None and fcf>0 else (70 if ocf is not None and ocf>0 else 0)
            cash_ok=(fcf is not None and fcf>0) or (ocf is not None and ocf>0)
        margin_ok=(profit_margin is None or profit_margin>0)
        # Extreme growth/heat combinations are treated as speculative rather than
        # as quality. We only hard-reject the most obvious blow-off patterns.
        speculation=_speculation_risk(tech or {})
        hard_heat=(speculation>=SPECULATION_HARD_LIMIT)
        ok=roe_ok and cap_ok and revenue_ok and earnings_ok and equity_ok and cash_ok and margin_ok and not hard_heat
        data={'roe':roe,'roe_min_pct':ROE_MIN_PCT,'market_cap':market_cap,
              'market_cap_min':MARKET_CAP_MIN.get(market),'market_cap_currency':MARKET_CAP_CURRENCY.get(market),
              'logo_url':info.get('logo_url') or info.get('logoUrl') or info.get('companyLogoUrl'), 'logo_domain':_logo_domain_from_info(info),
              'roe_ok':roe_ok,'market_cap_ok':cap_ok,'revenue':revenue,'net_income':net_income,
              'profit_margin':profit_margin,'operating_margin':op_margin,'free_cashflow':fcf,
              'operating_cashflow':ocf,'debt_to_equity':debt_to_equity,'trailing_eps':trailing_eps,
              'revenue_growth':revenue_growth,'earnings_growth':earnings_growth,'beta':beta,
              'financial_sector':financial_sector,'revenue_ok':revenue_ok,'earnings_ok':earnings_ok,'equity_ok':equity_ok,'total_equity':total_equity,
              'cash_ok':cash_ok,'cash_quality':cash_quality,'margin_ok':margin_ok,
              'speculation_risk':speculation,'speculation_ok':not hard_heat}
        data['quality_score']=_quality_score(data)
        _FUNDAMENTAL_CACHE[symbol]={'ts':time(),'ok':ok,'data':data,'market':market,'market_cap_hint':market_cap_hint,'sector':sector}
        return ok, data
    except Exception as exc:
        data={'roe':None,'roe_min_pct':ROE_MIN_PCT,'market_cap':None,
              'market_cap_min':MARKET_CAP_MIN.get(market),'market_cap_currency':MARKET_CAP_CURRENCY.get(market),
              'logo_url':None, 'logo_domain':None,
              'roe_ok':False,'market_cap_ok':False,'revenue_ok':False,'earnings_ok':False,
              'cash_ok':False,'margin_ok':False,'speculation_ok':False,'quality_score':None,'error':str(exc)[:180]}
        _FUNDAMENTAL_CACHE[symbol]={'ts':time(),'ok':False,'data':data,'market':market,'market_cap_hint':market_cap_hint,'sector':sector}
        return False, data


def _extract_history(frame, symbol):
    if frame is None or getattr(frame,'empty',True): return None
    try:
        if isinstance(frame.columns, pd.MultiIndex):
            # group_by=ticker normally gives (ticker, field); tolerate the
            # alternative (field, ticker) layout as well.
            if symbol in frame.columns.get_level_values(0):
                frame=frame[symbol]
            elif symbol in frame.columns.get_level_values(-1):
                frame=frame.xs(symbol, axis=1, level=-1)
        if 'Close' not in frame.columns: return None
        return frame.dropna(subset=['Close']).copy()
    except Exception:
        return None


def _scan_history_batch(symbols):
    if not symbols: return {}, {}, []
    try:
        data=yf.download(symbols, period=SCAN_HISTORY_PERIOD, interval='1d', auto_adjust=False,
                         group_by='ticker', threads=False, progress=False, repair=False, timeout=8)
        histories={s:_extract_history(data,s) for s in symbols}
        errors={s:'历史行情为空或字段不完整' for s,h in histories.items() if h is None}
        return histories, errors
    except Exception as exc:
        msg=f'批量历史行情请求失败：{str(exc)[:180]}'
        return {s:None for s in symbols}, {s:msg for s in symbols}


def _scan_batch(rows):
    symbols=[r['symbol'] for r in rows]
    histories, errors=_scan_history_batch(symbols)
    daily_moves=[]
    for row in rows:
        symbol=row['symbol']; history=histories.get(symbol)
        if history is None or len(history)<2: continue
        try:
            closes=pd.to_numeric(history['Close'], errors='coerce').dropna()
            if len(closes)<2: continue
            latest=float(closes.iloc[-1]); prev=float(closes.iloc[-2])
            if prev == 0: continue
            latest_date=str(pd.Timestamp(closes.index[-1]).date())
            daily_moves.append({
                'symbol':symbol,'market':row.get('market') or market_of(symbol),
                'sector':row.get('sector') or '未分类','industry':row.get('industry') or '未分类',
                'change_pct':(latest/prev-1.0)*100.0,'latest_trade_date':latest_date
            })
        except Exception:
            continue
    out=[]
    for row in rows:
        symbol=row['symbol']; history=histories.get(symbol)
        if history is None or len(history)<30:
            errors.setdefault(symbol, '有效历史数据不足30个交易日')
            continue
        try:
            fib=fibonacci_levels(history)
            pivots=pivot_levels(history)
            tech=technical_analysis(history, fib, pivots)
            if tech.get('composite_score') is None:
                continue
            out.append({
                'symbol':symbol,'company':row.get('company') or symbol,
                'exchange':row.get('exchange') or '','currency':row.get('currency') or '',
                'market':row.get('market') or market_of(symbol),
                'sector':row.get('sector') or '未分类','industry':row.get('industry') or '未分类',
                'market_cap':row.get('market_cap'),
                'screener_info':row.get('screener_info') or {},
                'fundamental_ok':None,
                'fundamental_status':'待候选池验证',
                'roe':None,'revenue':None,'net_income':None,'fcf':None,
                'strength':tech.get('score'),'value_score':tech.get('value_score'),
                'pullback_score':tech.get('pullback_score'),'composite_score':tech.get('composite_score'),
                'state':tech.get('state') or '暂无数据','value_state':tech.get('value_state') or '暂无数据',
                'pullback_state':tech.get('pullback_state') or '暂无数据',
                'score_breakdown':tech.get('score_breakdown') or {},
                'value_breakdown':tech.get('value_breakdown') or {},
                'pullback_breakdown':tech.get('pullback_breakdown') or {},
                'composite_breakdown':tech.get('composite_breakdown') or {},
            })
        except Exception as exc:
            errors[symbol]=f'技术指标计算失败：{str(exc)[:180]}'
            continue
    return out, errors, daily_moves



def _apply_fundamental_gate(rows, errors):
    """Quality-first Lite gate with an adaptive fast path.

    Pass 1 validates only the top technical shortlist per market. If a market
    does not produce enough qualified names for a TOP20 result, Pass 2 expands
    into the remaining technical candidates. This preserves the hard quality
    gates while materially reducing slow per-symbol Yahoo fundamental calls.
    """
    groups={'us':[],'hk':[],'cn':[]}
    for row in rows:
        mk=row.get('market') or market_of(row.get('symbol',''))
        groups.setdefault(mk,[]).append(row)

    def sort_key(x):
        return (float(x.get('composite_score',-1)), float(x.get('value_score',-1)),
                float(x.get('strength',-1)), str(x.get('symbol','')))

    # Technical pre-filter: obvious blow-off names never consume a slow
    # fundamental request. This is only a gate, not a score contribution.
    ordered={}
    for mk,items in groups.items():
        items=sorted(items, key=sort_key, reverse=True)
        clean=[]
        hot=[]
        for row in items:
            risk=_speculation_risk(row)
            if risk >= SPECULATION_HARD_LIMIT:
                errors[row.get('symbol','')]='技术层反过热过滤：短期价格/成交异常'
                hot.append(row)
            else:
                clean.append(row)
        ordered[mk]=(clean, hot)

    def check(row):
        mk=row.get('market') or market_of(row.get('symbol',''))
        ok,data=_fundamental_gate(row.get('symbol',''), mk, row.get('market_cap'), row.get('sector'), row.get('industry'), row)
        return row,ok,data

    def validate(batch, passed):
        if not batch: return
        with ThreadPoolExecutor(max_workers=FUNDAMENTAL_WORKERS, thread_name_prefix='ael-fund') as pool:
            futures=[pool.submit(check,row) for row in batch]
            for future in as_completed(futures):
                row,ok,data=future.result(); symbol=row.get('symbol','')
                if not ok:
                    reasons=[]
                    if not data.get('roe_ok'): reasons.append(f'ROE < {ROE_MIN_PCT:g}% 或暂无数据')
                    if not data.get('market_cap_ok'):
                        cap_min=float(data.get('market_cap_min') or 0)/1e9
                        reasons.append(f'市值 < {cap_min:g}B {data.get("market_cap_currency") or ""} 或暂无数据')
                    if not data.get('revenue_ok'): reasons.append('营收无效')
                    if not data.get('earnings_ok'): reasons.append('盈利为负或暂无数据')
                    if not data.get('equity_ok'): reasons.append('股东权益异常')
                    if not data.get('cash_ok'): reasons.append('经营现金流/自由现金流质量不足')
                    if not data.get('margin_ok'): reasons.append('利润率为负')
                    if not data.get('speculation_ok'): reasons.append('短期价格/成交过热')
                    errors[symbol]='质量资格过滤未通过：'+'；'.join(reasons)
                    continue
                row=dict(row)
                row.update({'fundamental_ok':True,'fundamental_status':'基本面合格','roe':data.get('roe'),
                            'revenue':data.get('revenue'),'net_income':data.get('net_income'),'fcf':data.get('free_cashflow'),
                            'market_cap':data.get('market_cap'),'quality_score':data.get('quality_score'),
                            'logo_url':data.get('logo_url'),
                            'speculation_risk':data.get('speculation_risk') or 0,'quality_breakdown':data})
                passed.append(row)

    passed=[]
    candidates_count=0
    for mk,(clean,_hot) in ordered.items():
        first=clean[:FUNDAMENTAL_FIRST_PASS]
        candidates_count += len(first)
        validate(first, passed)
        # If fewer than TOP20 qualified names survive, expand only this market.
        if len([x for x in passed if (x.get('market') or market_of(x.get('symbol',''))) == mk]) < FUNDAMENTAL_MIN_PASSED:
            extra=clean[FUNDAMENTAL_FIRST_PASS:FUNDAMENTAL_CANDIDATES]
            candidates_count += len(extra)
            validate(extra, passed)

    passed.sort(key=lambda x:(float(x.get('composite_score',-1)),float(x.get('value_score',-1)),
                              float(x.get('strength',-1)),float(x.get('quality_score',-1)),str(x.get('symbol',''))), reverse=True)
    return passed, candidates_count, len(passed)


def _daily_sector_performance(moves, selected_markets, scan_group='all', group_label='全市场'):
    """Latest trading-day breadth/performance from all market-cap-eligible names.
    For full-market scans, rank Yahoo sectors; for an explicit group, show the
    selected group as one independent performance bucket. No fundamental gate
    is applied here, so this describes the actual eligible sector universe, not
    only the quality-screened names.
    """
    buckets={}
    for x in moves:
        market=x.get('market')
        if market not in selected_markets: continue
        key=(market, (x.get('sector') or '未分类') if scan_group=='all' else group_label)
        buckets.setdefault(key,[]).append(x)
    result={m:[] for m in selected_markets}
    for (market,name), items in buckets.items():
        changes=[float(x['change_pct']) for x in items if x.get('change_pct') is not None]
        if not changes: continue
        changes.sort()
        n=len(changes); med=changes[n//2] if n%2 else (changes[n//2-1]+changes[n//2])/2
        up=sum(1 for v in changes if v>0); down=sum(1 for v in changes if v<0); flat=n-up-down
        dates=[x.get('latest_trade_date') for x in items if x.get('latest_trade_date')]
        result[market].append({
            'sector':name,'latest_trade_date':max(dates) if dates else None,
            'average_change_pct':round(sum(changes)/n,2),'median_change_pct':round(med,2),
            'up_pct':round(up/n*100,1),'down_pct':round(down/n*100,1),'flat_pct':round(flat/n*100,1),
            'stocks_with_data':n
        })
    for market in result:
        result[market].sort(key=lambda x:(x['average_change_pct'],x['median_change_pct'],x['sector']),reverse=True)
        for i,x in enumerate(result[market],1): x['rank']=i
    return result


def _rank_results(rows, limit=20):
    groups={'us':[],'hk':[],'cn':[]}
    for row in rows:
        groups.setdefault(row.get('market') or market_of(row.get('symbol','')),[]).append(row)
    ranked={}
    for mk in ('us','hk','cn'):
        group=groups.get(mk,[])
        # Stable multi-pass sort keeps the documented priority while making
        # the final ticker tie-break ascending (A -> Z), not descending.
        group.sort(key=lambda x: str(x.get('symbol','')))
        group.sort(key=lambda x: float(x.get('quality_score',-1)), reverse=True)
        group.sort(key=lambda x: float(x.get('value_score',-1)), reverse=True)
        group.sort(key=lambda x: float(x.get('strength',-1)), reverse=True)
        group.sort(key=lambda x: float(x.get('composite_score',-1)), reverse=True)
        for idx,row in enumerate(group[:limit],1):
            row['rank']=idx
        ranked[mk]=group[:limit]
    return ranked


def _sector_rank(rows, limit=10):
    by={}
    for row in rows:
        sector=row.get('sector') or '未分类'
        by.setdefault(sector,[]).append(row)
    result=[]
    for sector,items in by.items():
        scores=[float(x['composite_score']) for x in items if x.get('composite_score') is not None]
        pullbacks=[float(x['pullback_score']) for x in items if x.get('pullback_score') is not None]
        strengths=[float(x['strength']) for x in items if x.get('strength') is not None]
        if not scores: continue
        # Sector rotation is breadth-aware: average score + breadth of
        # high-quality pullbacks, rather than one superstar stock dominating.
        avg=sum(scores)/len(scores)
        breadth=sum(1 for x in pullbacks if x>=70)/max(1,len(pullbacks))*100
        strength_breadth=sum(1 for x in strengths if x>=70)/max(1,len(strengths))*100
        rotation=avg*0.50 + breadth*0.30 + strength_breadth*0.20
        result.append({'sector':sector,'stocks_scanned':len(items),'average_score':round(avg,1),
                       'pullback_breadth':round(breadth,1),'strength_breadth':round(strength_breadth,1),
                       'rotation_score':round(rotation,1)})
    result.sort(key=lambda x:(x['rotation_score'],x['average_score'],x['sector']),reverse=True)
    for i,x in enumerate(result[:limit],1): x['rank']=i
    return result[:limit]


def _job_snapshot(job):
    with _SCAN_LOCK:
        partial=job.get('rows',[])
        selected=job.get('selected_markets',[])
        return {
            'job_id':job['id'],'status':job['status'],'cancelled':job.get('cancelled',False),
            'started_at':job.get('started_at'),'updated_at':job.get('updated_at'),
            'total':job.get('total',0),'completed':job.get('completed',0),
            'current_market':job.get('current_market'),'current_sector':job.get('current_sector'),
            'current_sector_completed':job.get('current_sector_completed',0),'current_sector_total':job.get('current_sector_total',0),
            'otc_counts':job.get('otc_counts',{}),'cap_rejected':job.get('cap_rejected',{}),
            'scan_config':{'batch_size':SCAN_BATCH_SIZE,'workers':SCAN_WORKERS,'fundamental_workers':FUNDAMENTAL_WORKERS,'fundamental_candidates_per_market':FUNDAMENTAL_CANDIDATES,'fundamental_first_pass':FUNDAMENTAL_FIRST_PASS,'history_period':SCAN_HISTORY_PERIOD,'market_cap_min':MARKET_CAP_MIN,'fundamental_gate':f'ROE >= {ROE_MIN_PCT:g}% + 市值 + 盈利/现金流 + 反过热质量门槛'},
            'fundamental_candidates':job.get('fundamental_candidates',0),'fundamental_passed':job.get('fundamental_passed',0),
            'scan_group':job.get('scan_group','all'),'scan_group_label':SCAN_GROUPS.get(job.get('scan_group','all'),SCAN_GROUPS['all'])['label'],
            'universe_source':job.get('universe_source'),
            'results_by_market':_rank_results(partial,20),
            'sector_rotation':{mk:_sector_rank([x for x in partial if x.get('market')==mk],10) for mk in selected},
            'daily_sector_performance':_daily_sector_performance(job.get('daily_moves',[]), selected, job.get('scan_group','all'), SCAN_GROUPS.get(job.get('scan_group','all'),SCAN_GROUPS['all'])['label']),
            'universe_size':job.get('total',0),'selected_markets':selected,
            'error':job.get('error'),'error_count':len(job.get('scan_errors',{})),'recent_errors':list(job.get('scan_errors',{}).items())[-8:]
        }


def _run_scan_job(job):
    try:
        rows,source,otc_counts,cap_rejected=_scan_universe(job['markets'], job.get('scan_group','all'))
        rows=_enrich_rows_from_screener(rows)
        selected=job['selected_markets']
        rows=[r for r in rows if r.get('market') in selected]
        with _SCAN_LOCK:
            job['total']=len(rows); job['universe_source']=source
            job['otc_counts']=otc_counts; job['cap_rejected']=cap_rejected
            job['updated_at']=time()
        # Process market -> sector -> bounded batches. This makes sector
        # progress visible and limits peak data-source pressure.
        for mk in selected:
            if job['cancel_event'].is_set(): break
            market_rows=[r for r in rows if r.get('market')==mk]
            sectors={}
            for r in market_rows: sectors.setdefault(r.get('sector') or '未分类',[]).append(r)
            for sector, sector_rows in sorted(sectors.items(), key=lambda kv:(kv[0] != '未分类',kv[0])):
                if job['cancel_event'].is_set(): break
                with _SCAN_LOCK:
                    job['current_market']=mk; job['current_sector']=sector
                    job['current_sector_completed']=0; job['current_sector_total']=len(sector_rows); job['updated_at']=time()
                batches=[sector_rows[start:start+SCAN_BATCH_SIZE] for start in range(0,len(sector_rows),SCAN_BATCH_SIZE)]
                # Run a bounded number of history batches concurrently. Each
                # batch itself keeps yfinance threads disabled, preventing
                # nested fan-out. Results are merged as soon as a batch ends.
                with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix='ael-batch') as pool:
                    futures={}
                    for batch in batches:
                        if job['cancel_event'].is_set(): break
                        futures[pool.submit(_scan_batch, batch)]=(len(batch), batch)
                    for future in as_completed(futures):
                        batch_size,batch_rows=futures[future]
                        if job['cancel_event'].is_set():
                            continue
                        try:
                            batch_results,batch_errors,batch_moves=future.result()
                        except Exception as exc:
                            batch_results=[]
                            batch_errors={r['symbol']:f'批次扫描失败：{str(exc)[:180]}' for r in batch_rows}
                        with _SCAN_LOCK:
                            job['rows'].extend(batch_results)
                            job['daily_moves'].extend(batch_moves)
                            job['scan_errors'].update(batch_errors)
                            job['completed'] += batch_size
                            job['current_sector_completed'] += batch_size
                            job['updated_at']=time()
        with _SCAN_LOCK:
            cancelled=job['cancel_event'].is_set()
            if cancelled:
                job['status']='cancelled'; job['cancelled']=True
            else:
                job['status']='fundamental'
                job['current_market']='all'
                job['current_sector']='基本面候选验证'
                job['updated_at']=time()
        if not cancelled:
            passed, fund_candidates, fund_passed = _apply_fundamental_gate(job['rows'], job['scan_errors'])
            with _SCAN_LOCK:
                job['rows']=passed
                job['fundamental_candidates']=fund_candidates
                job['fundamental_passed']=fund_passed
                job['status']='completed'
                job['current_sector']=None
                job['updated_at']=time()
        with _SCAN_LOCK:
            cancelled=job['cancel_event'].is_set()
            if cancelled:
                job['status']='cancelled'; job['cancelled']=True
            job['updated_at']=time()
            ranked=_rank_results(job['rows'],20)
            cache_key=job['cache_key']
            # Only a fully completed scan may populate the reusable cache.
            # Cancelled/failed/partial jobs are never presented as complete.
            if not cancelled:
                _SCAN_CACHE[cache_key]={'ts':time(),'data':{
                    'ok':True,'scan':{'markets':job['selected_markets'],'universe_size':job['total'],'matched':sum(len(v) for v in ranked.values()),
                                     'top_n':20,'rules':'核心指数成分池→批量历史行情→技术候选→ROE/市值/盈利/现金流/反过热质量门槛→质量调整后排名各市场独立TOP20；缺失数据不估算','ttl_seconds':SCAN_CACHE_TTL,
                                     'scanner':'background filtered + parallel batched scan + fundamental gate','scan_group':job.get('scan_group','all'),'scan_group_label':SCAN_GROUPS.get(job.get('scan_group','all'),SCAN_GROUPS['all'])['label'],'scan_config':{'batch_size':SCAN_BATCH_SIZE,'workers':SCAN_WORKERS,'fundamental_workers':FUNDAMENTAL_WORKERS,'fundamental_candidates_per_market':FUNDAMENTAL_CANDIDATES,'fundamental_first_pass':FUNDAMENTAL_FIRST_PASS,'history_period':SCAN_HISTORY_PERIOD,'market_cap_min':MARKET_CAP_MIN,'fundamental_gate':f'ROE >= {ROE_MIN_PCT:g}% + 市值 + 盈利/现金流 + 反过热质量门槛'},
            'fundamental_candidates':job.get('fundamental_candidates',0),'fundamental_passed':job.get('fundamental_passed',0),
            'otc_counts':job.get('otc_counts',{}),'cap_rejected':job.get('cap_rejected',{})},
                    'results_by_market':ranked,'sector_rotation':{mk:_sector_rank([x for x in job['rows'] if x.get('market')==mk],10) for mk in job['selected_markets']},
                    'daily_sector_performance':_daily_sector_performance(job.get('daily_moves',[]), job['selected_markets'], job.get('scan_group','all'), SCAN_GROUPS.get(job.get('scan_group','all'),SCAN_GROUPS['all'])['label']),
                    'results':[row for mk in job['selected_markets'] for row in ranked.get(mk,[])], 'cached':False}}
    except Exception as exc:
        with _SCAN_LOCK:
            job['status']='failed'; job['error']=str(exc)[:500]; job['updated_at']=time()


def _cleanup_scan_jobs_locked(max_age_seconds=3600):
    now=time()
    keep_status={'running','cancelling'}
    stale=[jid for jid,j in _SCAN_JOBS.items()
           if j.get('status') not in keep_status and now-float(j.get('updated_at') or now)>max_age_seconds]
    for jid in stale:
        _SCAN_JOBS.pop(jid,None)


@app.post('/api/market-scan/start')
def market_scan_start(markets: str = Query('us,hk,cn'), group: str = Query('all'), force: bool = Query(False)):
    selected=[m.strip().lower() for m in markets.split(',') if m.strip() in {'us','hk','cn'}]
    group=group if group in SCAN_GROUPS else 'all'
    if not selected: raise HTTPException(status_code=400, detail='至少选择一个市场')
    cache_key=f"{','.join(selected)}|{group}"
    now=time()
    with _SCAN_LOCK:
        _cleanup_scan_jobs_locked()
        cached=_SCAN_CACHE.get(cache_key)
    if cached and not force and now-cached['ts']<SCAN_CACHE_TTL:
        return {'ok':True,'cached':True,'done':True,'data':cached['data']}
    with _SCAN_LOCK:
        _cleanup_scan_jobs_locked()
        for job in _SCAN_JOBS.values():
            if job['status']=='running' and job['cache_key']==cache_key:
                return {'ok':True,'job_id':job['id'],'status':'running'}
        job={'id':uuid.uuid4().hex[:12],'markets':','.join(selected),'selected_markets':selected,'scan_group':group,'status':'running','cancelled':False,
             'started_at':datetime.now(timezone.utc).isoformat(),'updated_at':time(),'total':0,'completed':0,'rows':[],
             'current_market':None,'current_sector':None,'current_sector_completed':0,'current_sector_total':0,
             'otc_counts':{'us':0,'hk':0,'cn':0},'cap_rejected':{'us':0,'hk':0,'cn':0},
             'scan_errors':{},'daily_moves':[],'fundamental_candidates':0,'fundamental_passed':0,
             'cancel_event':threading.Event(),'cache_key':cache_key,'error':None}
        _SCAN_JOBS[job['id']]=job
    threading.Thread(target=_run_scan_job,args=(job,),daemon=True,name=f'ael-scan-{job["id"]}').start()
    return {'ok':True,'job_id':job['id'],'status':'running'}


@app.get('/api/market-scan/status/{job_id}')
def market_scan_status(job_id: str):
    with _SCAN_LOCK:
        job=_SCAN_JOBS.get(job_id)
    if not job: raise HTTPException(status_code=404, detail='扫描任务不存在')
    return {'ok':True,**_job_snapshot(job)}


@app.post('/api/market-scan/cancel/{job_id}')
def market_scan_cancel(job_id: str):
    with _SCAN_LOCK:
        job=_SCAN_JOBS.get(job_id)
    if not job: raise HTTPException(status_code=404, detail='扫描任务不存在')
    job['cancel_event'].set()
    return {'ok':True,'job_id':job_id,'status':'cancelling'}


@app.get('/api/market-scan')
def market_scan_legacy(markets: str = Query('us,hk,cn'), limit: int = Query(20, ge=1, le=50), group: str = Query('all'), force: bool = Query(False)):
    """Compatibility endpoint. New UI uses start/status so scanning never blocks HTTP."""
    selected=[m.strip().lower() for m in markets.split(',') if m.strip() in {'us','hk','cn'}]
    group=group if group in SCAN_GROUPS else 'all'
    cache_key=f"{','.join(selected)}|{group}"
    with _SCAN_LOCK:
        cached=_SCAN_CACHE.get(cache_key)
    if cached and not force and time()-cached['ts']<SCAN_CACHE_TTL:
        data=dict(cached['data']); data['results_by_market']={m:(data.get('results_by_market') or {}).get(m,[])[:limit] for m in selected}; return data
    started=market_scan_start(markets=markets,group=group,force=force)
    if started.get('done'): return started['data']
    return JSONResponse(status_code=202,content={'ok':True,'status':'running','job_id':started['job_id'],'message':'扫描已在后台启动，请查询 status；不会阻塞单股查询'})


@app.get('/api/stock/{symbol}')
def stock_legacy(symbol: str):
    """兼容旧前端；新前端使用 core + details 两阶段加载。"""
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail='请输入股票代码')
    try:
        return build_dashboard(symbol, include_slow=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'数据获取失败：{exc}')
