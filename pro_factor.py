from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

import yfinance as yf


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _pct(v):
    x = _num(v)
    return x * 100 if x is not None and abs(x) <= 5 else x


def _clamp(x, lo=0.0, hi=100.0):
    if x is None:
        return None
    return max(lo, min(hi, float(x)))


def _score_higher(x, low, high):
    if x is None:
        return None
    return _clamp((x - low) / (high - low) * 100)


def _score_lower(x, low, high):
    if x is None:
        return None
    return _clamp((high - x) / (high - low) * 100)


def _score_mid(x, ideal, tolerance):
    if x is None:
        return None
    return _clamp(100 - abs(x - ideal) / tolerance * 100)


def _latest_row(df, names):
    if df is None or getattr(df, 'empty', True):
        return None
    for name in names:
        if name in df.index:
            row = df.loc[name]
            for value in row.tolist():
                x = _num(value)
                if x is not None:
                    return x
    return None


def _growth_from_row(df, names, periods=4):
    if df is None or getattr(df, 'empty', True):
        return None
    for name in names:
        if name not in df.index:
            continue
        vals = []
        for value in df.loc[name].tolist():
            x = _num(value)
            if x is not None and x > 0:
                vals.append(x)
        if len(vals) >= periods:
            old, new = vals[periods - 1], vals[0]
            if old > 0 and new > 0:
                years = periods - 1
                return ((new / old) ** (1 / years) - 1) * 100
    return None


def _return_pct(hist, days):
    if hist is None or hist.empty or 'Close' not in hist:
        return None
    s = hist['Close'].dropna()
    if len(s) <= days:
        return None
    a, b = _num(s.iloc[-days - 1]), _num(s.iloc[-1])
    if a in (None, 0) or b is None:
        return None
    return (b / a - 1) * 100


def _volatility(hist, days=60):
    if hist is None or hist.empty or 'Close' not in hist:
        return None
    s = hist['Close'].dropna().pct_change().dropna()
    if len(s) < max(20, days // 2):
        return None
    return float(s.tail(days).std() * math.sqrt(252) * 100)


def _max_drawdown(hist, days=252):
    if hist is None or hist.empty or 'Close' not in hist:
        return None
    s = hist['Close'].dropna().tail(days)
    if s.empty:
        return None
    dd = s / s.cummax() - 1
    return float(dd.min() * 100)


def _dollar_volume(hist, days=20):
    if hist is None or hist.empty or 'Close' not in hist or 'Volume' not in hist:
        return None
    x = hist[['Close', 'Volume']].dropna().tail(days)
    if x.empty:
        return None
    return float((x['Close'] * x['Volume']).mean())


def _factor(name: str, score, metrics, definition, limitations=None):
    return {
        'name': name,
        'score': None if score is None else round(float(score), 2),
        'metrics': metrics,
        'definition': definition,
        'limitations': limitations or [],
    }


def analyze_factor(symbol: str, weights: dict[str, float] | None = None) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError('请输入股票代码')

    # Data sources are isolated deliberately: one failed Yahoo endpoint must
    # not erase otherwise valid factors. Missing data remains None/暂无数据.
    errors = []
    try:
        t = yf.Ticker(symbol)
    except Exception as exc:
        raise ValueError(f'无法创建数据对象：{exc}')

    info = {}
    try:
        info = t.info or {}
    except Exception as exc:
        errors.append(f'基本面接口：{exc}')

    hist = None
    try:
        hist = t.history(period='2y', auto_adjust=False)
    except Exception as exc:
        errors.append(f'历史行情接口：{exc}')

    financials = None
    try:
        financials = t.financials
    except Exception as exc:
        errors.append(f'财务报表接口：{exc}')

    roe = _pct(info.get('returnOnEquity'))
    gross_margin = _pct(info.get('grossMargins'))
    debt_to_equity = _num(info.get('debtToEquity'))
    pe = _num(info.get('trailingPE'))
    pb = _num(info.get('priceToBook'))
    revenue_growth = _pct(info.get('revenueGrowth'))
    earnings_growth = _pct(info.get('earningsGrowth'))
    fcf = _num(info.get('freeCashflow'))
    revenue = _num(info.get('totalRevenue'))
    fcf_margin = (fcf / revenue * 100) if fcf is not None and revenue not in (None, 0) else None

    if financials is not None:
        if revenue_growth is None:
            revenue_growth = _growth_from_row(financials, ['Total Revenue', 'Operating Revenue'])
        if earnings_growth is None:
            earnings_growth = _growth_from_row(financials, ['Net Income', 'Net Income Common Stockholders'])

    r20 = _return_pct(hist, 20) if hist is not None else None
    r120 = _return_pct(hist, 120) if hist is not None else None
    r252 = _return_pct(hist, 252) if hist is not None else None
    vol60 = _volatility(hist, 60) if hist is not None else None
    dd252 = _max_drawdown(hist, 252) if hist is not None else None
    dv20 = _dollar_volume(hist, 20) if hist is not None else None

    value_scores = [x for x in [_score_lower(pe, 8, 45), _score_lower(pb, 0.6, 8)] if x is not None]
    quality_scores = [x for x in [_score_higher(roe, 0, 30), _score_higher(gross_margin, 10, 70), _score_higher(fcf_margin, -10, 30), _score_lower(debt_to_equity, 0, 250)] if x is not None]
    growth_scores = [x for x in [_score_higher(revenue_growth, -20, 30), _score_higher(earnings_growth, -30, 40)] if x is not None]
    momentum_scores = [x for x in [_score_higher(r20, -20, 20), _score_higher(r120, -35, 50), _score_higher(r252, -50, 100)] if x is not None]
    risk_scores = [x for x in [_score_lower(vol60, 10, 80), _score_higher(dd252, -70, 0)] if x is not None]
    liquidity_score = _score_higher(math.log10(dv20) if dv20 and dv20 > 0 else None, 5, 10)

    factors = {
        'value': _factor('Value', sum(value_scores)/len(value_scores) if value_scores else None,
                         {'PE': pe, 'PB': pb}, 'PE/PB 越低，价值分越高；当前为有界绝对分，不是同业排名。'),
        'quality': _factor('Quality', sum(quality_scores)/len(quality_scores) if quality_scores else None,
                           {'ROE_pct': roe, 'gross_margin_pct': gross_margin, 'FCF_margin_pct': fcf_margin, 'debt_to_equity': debt_to_equity}, 'ROE、毛利率、FCF利润率越高越有利；负债权益比越低越有利。'),
        'growth': _factor('Growth', sum(growth_scores)/len(growth_scores) if growth_scores else None,
                          {'revenue_growth_pct': revenue_growth, 'earnings_growth_pct': earnings_growth}, '收入增长与盈利增长越高越有利；缺失项不补值。'),
        'momentum': _factor('Momentum', sum(momentum_scores)/len(momentum_scores) if momentum_scores else None,
                            {'return_20d_pct': r20, 'return_120d_pct': r120, 'return_252d_pct': r252}, '20/120/252交易日价格动量的有界绝对分。'),
        'risk': _factor('Risk', sum(risk_scores)/len(risk_scores) if risk_scores else None,
                        {'volatility_60d_pct': vol60, 'max_drawdown_252d_pct': dd252}, '波动率越低、最大回撤越小，风险分越高。'),
        'liquidity': _factor('Liquidity', liquidity_score,
                             {'avg_dollar_volume_20d': dv20}, '20日平均成交额越高，流动性分越高；当前为有界绝对分。'),
    }

    default_weights = {'value': 20, 'quality': 25, 'growth': 15, 'momentum': 20, 'risk': 10, 'liquidity': 10}
    weights = weights or default_weights
    clean = {k: max(0.0, float(weights.get(k, default_weights[k]))) for k in default_weights}
    used = {k: v for k, v in clean.items() if factors[k]['score'] is not None and v > 0}
    total_w = sum(used.values())
    alpha = (sum(factors[k]['score'] * w for k, w in used.items()) / total_w) if total_w else None
    for k, f in factors.items():
        f['weight_pct'] = round(clean[k], 2)
        f['contribution'] = round((f['score'] * clean[k] / total_w), 2) if f['score'] is not None and total_w else None

    missing_metrics = []
    for key, factor in factors.items():
        for metric, value in factor['metrics'].items():
            if value is None:
                missing_metrics.append(f'{key}.{metric}')

    return {
        'symbol': symbol,
        'company': info.get('longName') or info.get('shortName') or symbol,
        'exchange': info.get('exchange'),
        'currency': info.get('currency'),
        'logo_url': info.get('logo_url') or info.get('logoUrl') or info.get('companyLogoUrl'),
        'logo_domain': str(info.get('website') or '').replace('https://','').replace('http://','').split('/')[0] or None,
        'as_of': datetime.now(timezone.utc).isoformat(),
        'method': 'bounded_absolute_v1',
        'method_note': '首版 Factor Lab 使用透明的有界绝对分；不冒充横截面 Z-score/行业中性化。后续 Universe 模块接入后再提供真实横截面标准化。',
        'alpha_research_score': round(alpha, 2) if alpha is not None else None,
        'weights_used': {k: round(v, 2) for k, v in clean.items()},
        'factors': factors,
        'data_quality': {
            'missing_factors': [k for k, f in factors.items() if f['score'] is None],
            'missing_metrics': missing_metrics,
            'errors': errors,
            'history_rows': int(len(hist)) if hist is not None else 0,
            'financial_statement_available': bool(financials is not None and not financials.empty),
            'basic_info_available': bool(info),
        },
    }

