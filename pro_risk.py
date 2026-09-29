from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any
import pandas as pd
import yfinance as yf


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _series(hist):
    if hist is None or getattr(hist, 'empty', True):
        return None
    if isinstance(hist.columns, pd.MultiIndex):
        if 'Close' not in hist.columns.get_level_values(0):
            return None
        s = hist['Close']
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
    elif 'Close' in hist:
        s = hist['Close']
    else:
        return None
    s = pd.to_numeric(s, errors='coerce').dropna()
    return s if not s.empty else None


def _returns(s):
    if s is None:
        return None
    r = s.pct_change().dropna()
    return r if not r.empty else None


def _vol(r, n):
    if r is None or len(r) < max(20, min(n, 60)):
        return None
    return float(r.tail(n).std() * math.sqrt(252) * 100)


def _max_dd(s, n=None):
    if s is None or len(s) < 2:
        return None
    x = s.tail(n) if n else s
    return float((x / x.cummax() - 1).min() * 100)


def _beta_corr(r, b):
    if r is None or b is None:
        return None, None
    df = pd.concat([r.rename('r'), b.rename('b')], axis=1).dropna()
    if len(df) < 30:
        return None, None
    cov = df['r'].cov(df['b'])
    var = df['b'].var()
    beta = cov / var if var and math.isfinite(var) else None
    corr = df['r'].corr(df['b'])
    return _num(beta), _num(corr)


def _var_cvar(r, alpha=.95):
    if r is None or len(r) < 30:
        return None, None
    q = float(r.quantile(1-alpha))
    tail = r[r <= q]
    cvar = float(tail.mean()) if len(tail) else None
    return q * 100, cvar * 100 if cvar is not None else None


def _sharpe(r, rf_annual=0.0):
    if r is None or len(r) < 30:
        return None
    daily_rf = rf_annual / 252.0
    ex = r - daily_rf
    sd = ex.std()
    if not sd or not math.isfinite(sd) or sd < 1e-10:
        return None
    return float(ex.mean() / sd * math.sqrt(252))


def _downside(r):
    if r is None or len(r) < 30:
        return None
    neg = r[r < 0]
    if len(neg) < 10:
        return None
    return float(neg.std() * math.sqrt(252) * 100)


def _risk_metrics(s, benchmark_s):
    r = _returns(s)
    br = _returns(benchmark_s)
    beta, corr = _beta_corr(r, br)
    var95, cvar95 = _var_cvar(r)
    return {
        'beta_2y': beta,
        'correlation_2y': corr,
        'volatility_20d_pct': _vol(r, 20),
        'volatility_60d_pct': _vol(r, 60),
        'volatility_252d_pct': _vol(r, 252),
        'max_drawdown_252d_pct': _max_dd(s, 252),
        'max_drawdown_2y_pct': _max_dd(s, 504),
        'historical_var_95_1d_pct': var95,
        'historical_cvar_95_1d_pct': cvar95,
        'downside_volatility_annualized_pct': _downside(r),
        'sharpe_0rf_2y': _sharpe(r, 0.0),
    }


def _history(symbol, period):
    try:
        return yf.Ticker(symbol).history(period=period, auto_adjust=False)
    except Exception:
        return None


def analyze_risk(symbol: str, benchmark: str = 'SPY', period: str = '2y') -> dict[str, Any]:
    symbol = symbol.strip().upper()
    benchmark = benchmark.strip().upper() or 'SPY'
    if not symbol:
        raise ValueError('请输入股票代码')
    errors=[]
    hs = _history(symbol, period)
    if hs is None or hs.empty:
        errors.append('标的历史行情暂无数据')
    hb = _history(benchmark, period)
    if hb is None or hb.empty:
        errors.append('基准历史行情暂无数据')
    s=_series(hs); b=_series(hb)
    result={
      'symbol':symbol,'benchmark':benchmark,'as_of':datetime.now(timezone.utc).isoformat(),
      'metrics':_risk_metrics(s,b),
      'data_quality':{
        'symbol_history_rows':int(len(hs)) if hs is not None else 0,
        'benchmark_history_rows':int(len(hb)) if hb is not None else 0,
        'errors':errors,
      },
      'method':'historical_risk_v1',
      'method_note':'历史风险统计；VaR/CVaR为历史分位法，不是预测；Sharpe 暂按0%年化无风险利率计算。缺失数据不估算。'
    }
    result['missing_metrics']=[k for k,v in result['metrics'].items() if v is None]
    return result


def analyze_portfolio(symbols: list[str], weights: list[float] | None = None, benchmark: str = 'SPY', period: str = '2y', capital: float | None = None) -> dict[str, Any]:
    clean=[]
    for s in symbols:
        s=str(s).strip().upper()
        if s and s not in clean:
            clean.append(s)
    if not clean:
        raise ValueError('请输入至少1只股票')
    if len(clean)>12:
        raise ValueError('组合体检最多支持12只股票')
    if weights is None:
        w=[1/len(clean)]*len(clean)
    else:
        if len(weights)!=len(clean):
            raise ValueError('持仓数量与权重数量不一致')
        w=[float(x) for x in weights]
        if any((not math.isfinite(x) or x<0) for x in w) or sum(w)<=0:
            raise ValueError('权重必须为非负数字，且总和大于0')
        total=sum(w); w=[x/total for x in w]
    errors=[]
    series={}
    for sym in clean:
        h=_history(sym, period)
        s=_series(h)
        if s is None:
            errors.append(f'{sym}历史行情暂无数据')
        else:
            series[sym]=s
    hb=_history(benchmark, period); bs=_series(hb)
    if bs is None:
        errors.append(f'{benchmark}基准历史行情暂无数据')
    if not series:
        raise ValueError('组合没有可用的历史行情')
    df=pd.concat([x.pct_change().rename(k) for k,x in series.items()], axis=1).dropna(how='all')
    used=[s for s in clean if s in df.columns]
    used_w={s:w[clean.index(s)] for s in used}
    if used_w:
        sw=sum(used_w.values())
        used_w={s:x/sw for s,x in used_w.items()}
    pr=df[list(used_w)].mul(pd.Series(used_w)).sum(axis=1).dropna()
    price=(1+pr).cumprod()
    br=_returns(bs)
    beta,corr=_beta_corr(pr,br)
    var95,cvar95=_var_cvar(pr)
    hhi=sum(x*x for x in used_w.values()) if used_w else None
    eff_n=(1/hhi) if hhi and hhi>0 else None
    metrics={
        'beta':beta,'correlation':corr,
        'volatility_20d_pct':_vol(pr,20),'volatility_60d_pct':_vol(pr,60),'volatility_252d_pct':_vol(pr,252),
        'max_drawdown_252d_pct':_max_dd(price,252),'max_drawdown_full_pct':_max_dd(price,None),
        'historical_var_95_1d_pct':var95,'historical_cvar_95_1d_pct':cvar95,
        'downside_volatility_annualized_pct':_downside(pr),'sharpe_0rf':_sharpe(pr,0.0),
        'concentration_hhi':hhi,'effective_holdings':eff_n,'available_holdings':len(used),
    }
    capital_num=_num(capital)
    if capital_num is not None and capital_num>0:
        metrics['historical_var_95_1d_amount']=abs(var95)/100*capital_num if var95 is not None else None
        metrics['historical_cvar_95_1d_amount']=abs(cvar95)/100*capital_num if cvar95 is not None else None
    return {
        'symbols':clean,'weights':w,'used_weights':used_w,'benchmark':benchmark,'period':period,
        'as_of':datetime.now(timezone.utc).isoformat(),'metrics':metrics,
        'data_quality':{'requested_holdings':len(clean),'available_holdings':len(used),'benchmark_history_rows':int(len(hb)) if hb is not None else 0,'errors':errors},
        'method':'portfolio_historical_risk_v1',
        'method_note':'组合风险由各持仓历史日收益按输入权重合成；VaR/CVaR为历史分位法，不是未来损失预测。缺失持仓不会用估算值补齐；若部分持仓缺失，剩余可用持仓会重新归一化并明确标注。'
    }
