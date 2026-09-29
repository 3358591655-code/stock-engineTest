"""Optional multi-asset Lite data layer.

The asset layer is isolated from stock core/scan. It reuses the same technical
engine as stocks so commodities/crypto receive the same technical vocabulary,
while news is a separate lazy endpoint and can never block the quote response.
"""
from time import time
import math
import threading
import requests
import yfinance as yf
import pandas as pd

from metrics import technical_analysis, fibonacci_levels, pivot_levels, resonance_levels, technical_price_chart

ASSET_INDEX = [
    {"symbol":"XAUUSD","provider_symbol":"GC=F","provider_fallbacks":["XAUUSD=X"],"name":"Gold Futures / USD","cn":"黄金","asset_type":"商品","market":"Spot","currency":"USD","icon":"🥇"},
    {"symbol":"XAGUSD","provider_symbol":"SI=F","name":"Silver Spot / USD","cn":"白银","asset_type":"商品","market":"Spot","currency":"USD","icon":"🥈"},
    {"symbol":"WTI","provider_symbol":"CL=F","name":"WTI Crude Oil","cn":"WTI原油","asset_type":"商品","market":"NYMEX","currency":"USD","icon":"🛢️"},
    {"symbol":"BRENT","provider_symbol":"BZ=F","name":"Brent Crude Oil","cn":"布伦特原油","asset_type":"商品","market":"ICE","currency":"USD","icon":"🛢️"},
    {"symbol":"COPPER","provider_symbol":"HG=F","name":"Copper Futures","cn":"铜","asset_type":"商品","market":"COMEX","currency":"USD","icon":"🔶"},
    {"symbol":"PLATINUM","provider_symbol":"PL=F","name":"Platinum Futures","cn":"铂金","asset_type":"商品","market":"NYMEX","currency":"USD","icon":"⚪"},
    {"symbol":"BTCUSD","provider_symbol":"BTC-USD","name":"Bitcoin / USD","cn":"比特币","asset_type":"加密资产","market":"Crypto","currency":"USD","icon":"₿"},
    {"symbol":"ETHUSD","provider_symbol":"ETH-USD","name":"Ethereum / USD","cn":"以太坊","asset_type":"加密资产","market":"Crypto","currency":"USD","icon":"Ξ"},
    {"symbol":"SOLUSD","provider_symbol":"SOL-USD","name":"Solana / USD","cn":"Solana","asset_type":"加密资产","market":"Crypto","currency":"USD","icon":"S"},
    {"symbol":"DXY","provider_symbol":"DX-Y.NYB","name":"US Dollar Index","cn":"美元指数","asset_type":"外汇/宏观","market":"ICE","currency":"USD","icon":"$"},
]

_CACHE = {}
_NEWS_CACHE = {}
_LOCK = threading.Lock()
TTL = 300
NEWS_TTL = 900


def _finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def get_asset_index():
    return [dict(x) for x in ASSET_INDEX]


def find_asset(query):
    q=str(query or '').strip().lower()
    if not q: return None
    exact=next((x for x in ASSET_INDEX if x['symbol'].lower()==q or x['cn'].lower()==q or x['name'].lower()==q),None)
    if exact:return dict(exact)
    aliases={
      'gold':'XAUUSD','黄金期货':'XAUUSD','黄金现货':'XAUUSD','xau':'XAUUSD','xauusd':'XAUUSD','xauusd=x':'XAUUSD','gc=f':'XAUUSD',
      'btc':'BTCUSD','bitcoin':'BTCUSD','比特币':'BTCUSD','btcusd':'BTCUSD',
      'eth':'ETHUSD','ethereum':'ETHUSD','ethusd':'ETHUSD',
      '原油期货':'WTI','wti':'WTI','oil':'WTI','crude oil':'WTI','石油':'WTI','原油':'WTI',
      'brent':'BRENT','布油':'BRENT','bz=f':'BRENT','silver':'XAGUSD','dxy':'DXY','美元指数':'DXY',
      '白银期货':'XAGUSD','si=f':'XAGUSD','copper':'COPPER','hg=f':'COPPER','platinum':'PLATINUM','pl=f':'PLATINUM',
    }
    s=aliases.get(q)
    return next((dict(x) for x in ASSET_INDEX if x['symbol']==s),None) if s else None



def _yahoo_chart_history(provider_symbol, days=730):
    """Fetch real daily OHLCV from Yahoo Chart API as a multi-asset fallback."""
    end=int(time())
    start=end-int(max(30, int(days or 730))*86400)
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(str(provider_symbol), safe='')}"
    r=requests.get(url, params={
        'period1':start,'period2':end,'interval':'1d','events':'history',
        'includeAdjustedClose':'true',
    }, headers={'User-Agent':'Mozilla/5.0 (AEL; multi-asset chart fallback)'}, timeout=5)
    r.raise_for_status()
    data=r.json() or {}
    result=((data.get('chart') or {}).get('result') or [None])[0]
    if not result:
        raise RuntimeError('Yahoo Chart returned no result')
    ts=result.get('timestamp') or []
    quote=((result.get('indicators') or {}).get('quote') or [None])[0] or {}
    rows=[]
    for i,t in enumerate(ts):
        try:
            row={c:quote.get(c.lower(), [None]*len(ts))[i] for c in ['Open','High','Low','Close','Volume']}
            if row['Close'] is None:
                continue
            dt=pd.to_datetime(int(t), unit='s', utc=True).tz_convert(None)
            rows.append((dt,row))
        except Exception:
            continue
    if not rows:
        raise RuntimeError('Yahoo Chart returned no usable OHLC rows')
    frame=pd.DataFrame([x[1] for x in rows], index=pd.DatetimeIndex([x[0] for x in rows]))
    for col in ['Open','High','Low','Close','Volume']:
        frame[col]=pd.to_numeric(frame[col], errors='coerce')
    return frame.dropna(subset=['Close']).sort_index()

def _calc(symbol, meta, h):
    close=pd.to_numeric(h['Close'],errors='coerce').dropna()
    if len(close)<30:return None
    px=float(close.iloc[-1]); prev=float(close.iloc[-2]) if len(close)>=2 else None
    ret1=(px/prev-1)*100 if prev else None
    ret20=(px/float(close.iloc[-21])-1)*100 if len(close)>=21 else None
    ret60=(px/float(close.iloc[-61])-1)*100 if len(close)>=61 else None
    lo=float(close.tail(min(252,len(close))).min()); hi=float(close.tail(min(252,len(close))).max())
    pos=((px-lo)/(hi-lo)*100) if hi>lo else None
    daily=close.pct_change().dropna()
    vol20=float(daily.tail(min(20,len(daily))).std()*math.sqrt(252)*100) if len(daily)>=10 else None
    dd=(px/float(close.cummax().iloc[-1])-1)*100

    # Same technical engine as stocks. No ROE/PE/fundamental gate is applied.
    fib=fibonacci_levels(h)
    pivots=pivot_levels(h)
    tech=technical_analysis(h, fib, pivots)
    resonance=resonance_levels(px, pivots, tech.get('indicators', {}))
    price_chart=technical_price_chart(h, fib)
    tech['fibonacci']=fib
    tech['pivots']=pivots
    tech['resonance']=resonance
    tech['price_chart']=price_chart

    return {
      **meta,'available':True,'price':px,'change_1d_pct':ret1,'momentum_20d_pct':ret20,
      'momentum_60d_pct':ret60,'position_52w_pct':pos,'volatility_20d_annualized_pct':vol20,
      'max_drawdown_from_history_pct':dd,'history_rows':int(len(close)),
      'latest_trade_date':str(close.index[-1].date()) if hasattr(close.index[-1],'date') else str(close.index[-1]),
      'technical':tech,
      'data_quality':{'history':True,'fundamentals':'not_applicable','reason':'商品/加密资产沿用股票技术分析引擎，但不套用股票ROE/PE/现金流硬门槛'},
    }


def get_asset(symbol):
    symbol=str(symbol or '').strip().upper()
    meta=next((dict(x) for x in ASSET_INDEX if x['symbol'].upper()==symbol),None)
    if not meta:return {'ok':False,'symbol':symbol,'error':'AEL当前多资产索引未收录该标的'}
    now=time()
    with _LOCK:
        c=_CACHE.get(symbol)
        if c and now-c[0]<TTL:return c[1]
    provider_symbol=meta.get('provider_symbol') or symbol
    provider_candidates=[provider_symbol] + [x for x in (meta.get('provider_fallbacks') or []) if x and x != provider_symbol]
    h=None
    source=None
    first_error=None
    # Try each real provider symbol in order. Gold intentionally uses GC=F first
    # because Yahoo exposes the gold futures history there; XAUUSD=X is fallback.
    for candidate in provider_candidates:
        try:
            h=yf.Ticker(candidate).history(period='2y',interval='1d',auto_adjust=False)
            if h is not None and not h.empty:
                provider_symbol=candidate
                source='yfinance'
                break
        except Exception as exc:
            if not first_error:
                first_error=str(exc)[:180]
        h=None

        try:
            h=_yahoo_chart_history(candidate, days=730)
            if h is not None and not h.empty:
                provider_symbol=candidate
                source='Yahoo Chart API'
                break
        except Exception as exc:
            if not first_error:
                first_error=str(exc)[:180]


    try:
        if h is None or h.empty:
            out={'ok':False,'symbol':symbol,'asset_type':meta['asset_type'],
                 'provider_symbol':provider_symbol,'data_source':None,
                 'error':'暂无足够行情数据'}
        else:
            out=_calc(symbol,meta,h)
            if not out:
                out={'ok':False,'symbol':symbol,'asset_type':meta['asset_type'],
                     'provider_symbol':provider_symbol,'data_source':source,
                     'error':'暂无足够历史数据'}
            else:
                out['ok']=True
                out['provider_symbol']=provider_symbol
                out['data_source']=source
                out['method_note']='多资产复用股票技术分析引擎：MA20/60/120/250、MACD、RSI、KDJ、BOLL、Pivot、Fibonacci、20D动量、52周位置、技术强势/高性价比/回踩质量全部保持；股票基本面字段对商品/加密资产标记为不适用。'
    except Exception as exc:
        out={'ok':False,'symbol':symbol,'asset_type':meta['asset_type'],
             'provider_symbol':provider_symbol,'data_source':source,
             'error':str(exc)[:180]}
    if first_error and not out.get('ok'):
        out['data_source_error']=first_error
    with _LOCK:_CACHE[symbol]=(now,out)
    return out


def _asset_news_query(meta):
    # Search by human-readable asset name, not provider ticker, for better news coverage.
    if meta['symbol']=='XAUUSD': return 'gold spot gold price'
    if meta['symbol']=='WTI': return 'WTI crude oil'
    if meta['symbol']=='BTCUSD': return 'Bitcoin BTC'
    if meta['symbol']=='ETHUSD': return 'Ethereum ETH'
    if meta['symbol']=='SOLUSD': return 'Solana SOL'
    return meta.get('name') or meta.get('cn') or meta['symbol']


def get_asset_news(symbol, limit=8):
    symbol=str(symbol or '').strip().upper()
    meta=next((dict(x) for x in ASSET_INDEX if x['symbol'].upper()==symbol),None)
    if not meta:
        return {'ok':False,'symbol':symbol,'news':[],'error':'未收录多资产'}
    limit=max(1,min(12,int(limit or 8)))
    key=f'{symbol}:{limit}'
    now=time()
    with _LOCK:
        c=_NEWS_CACHE.get(key)
        if c and now-c[0]<NEWS_TTL:return c[1]
    out={'ok':True,'symbol':symbol,'news':[],'source':'Yahoo Finance News','error':None}
    try:
        r=requests.get('https://query1.finance.yahoo.com/v1/finance/search',
                       params={'q':_asset_news_query(meta),'quotesCount':0,'newsCount':limit},
                       headers={'User-Agent':'Mozilla/5.0 (AEL; multi-asset news)'},timeout=4)
        r.raise_for_status(); data=r.json() or {}
        seen=set()
        for x in data.get('news') or []:
            title=(x.get('title') or '').strip(); link=(x.get('link') or x.get('canonicalUrl',{}).get('url') or '').strip()
            if not title or not link: continue
            k=link or title
            if k in seen: continue
            seen.add(k)
            ts=x.get('providerPublishTime'); dt=None
            if ts:
                try: dt=pd.to_datetime(ts,unit='s',utc=True).tz_convert(None).strftime('%Y-%m-%d %H:%M')
                except Exception: pass
            out['news'].append({'title':title,'publisher':str(x.get('publisher') or ''),'link':link,'published_at':dt})
    except Exception as exc:
        out['error']=str(exc)[:180]
    out['news']=out['news'][:limit]
    with _LOCK:_NEWS_CACHE[key]=(now,out)
    return out
