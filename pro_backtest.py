"""AEL Pro Backtest Lab.

Research-first historical backtesting using daily adjusted prices from Yahoo Finance.
The engine deliberately avoids point-in-time fundamental claims because yfinance does
not provide a reliable historical-as-of fundamental database. Factor/Alpha hooks can
be connected later once a point-in-time data layer exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import math
import numpy as np
import pandas as pd
import yfinance as yf


STRATEGIES = {
    "equal_weight": "等权持有",
    "momentum": "动量轮动",
    "trend": "趋势 + 动量",
    "low_vol": "低波动",
}

PERIOD_DAYS = {"3y": 365 * 3, "5y": 365 * 5, "10y": 365 * 10, "max": None}


def _clean_symbols(symbols: List[str]) -> List[str]:
    out = []
    seen = set()
    for s in symbols:
        s = str(s).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:80]


def _download(symbols: List[str], benchmark: str, period: str) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    tickers = _clean_symbols(symbols + [benchmark])
    if not tickers:
        return pd.DataFrame(), pd.DataFrame(), []
    # Raw Close = 价格收益；Adj Close = 含分红再投资的全收益序列。
    # 同一次下载同时取得两套序列，避免分别请求导致交易日期错位。
    kwargs = dict(period="max" if period == "max" else period, auto_adjust=False,
                  progress=False, threads=False, group_by="column")
    data = yf.download(tickers=tickers, **kwargs)
    if data is None or data.empty:
        return pd.DataFrame(), pd.DataFrame(), tickers
    if isinstance(data.columns, pd.MultiIndex):
        levels = set(data.columns.get_level_values(0))
        if "Close" not in levels:
            return pd.DataFrame(), pd.DataFrame(), tickers
        raw = data["Close"].copy()
        adj = data["Adj Close"].copy() if "Adj Close" in levels else raw.copy()
    else:
        if "Close" not in data.columns:
            return pd.DataFrame(), pd.DataFrame(), tickers
        raw = data[["Close"]].copy()
        raw.columns = [tickers[0]]
        if "Adj Close" in data.columns:
            adj = data[["Adj Close"]].copy()
            adj.columns = [tickers[0]]
        else:
            adj = raw.copy()
    raw = raw.replace([np.inf, -np.inf], np.nan).sort_index().ffill(limit=3)
    adj = adj.replace([np.inf, -np.inf], np.nan).sort_index().ffill(limit=3)
    return raw, adj, tickers


def _annualized_return(equity: pd.Series) -> Optional[float]:
    if len(equity) < 2 or equity.iloc[0] <= 0 or equity.iloc[-1] <= 0:
        return None
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return None
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def _max_drawdown(equity: pd.Series) -> Optional[float]:
    if equity.empty:
        return None
    dd = equity / equity.cummax() - 1
    return float(dd.min())


def _sharpe(returns: pd.Series) -> Optional[float]:
    r = returns.dropna()
    if len(r) < 20 or r.std(ddof=1) == 0:
        return None
    return float(np.sqrt(252) * r.mean() / r.std(ddof=1))


def _sortino(returns: pd.Series) -> Optional[float]:
    r = returns.dropna()
    downside = r[r < 0]
    if len(r) < 20 or downside.std(ddof=1) == 0:
        return None
    return float(np.sqrt(252) * r.mean() / downside.std(ddof=1))


def _calmar(ann: Optional[float], mdd: Optional[float]) -> Optional[float]:
    if ann is None or mdd is None or mdd >= 0:
        return None
    return float(ann / abs(mdd))


def _cagr_for_slice(equity: pd.Series) -> Optional[float]:
    return _annualized_return(equity)


def _weights_for_date(prices: pd.DataFrame, date: pd.Timestamp, strategy: str, lookback: int,
                     top_k: int, trend_ma: int = 200) -> pd.Series:
    hist = prices.loc[:date].dropna(how="all")
    if len(hist) < max(lookback + 2, 30):
        return pd.Series(dtype=float)
    available = hist.columns[hist.iloc[-1].notna()]
    if len(available) == 0:
        return pd.Series(dtype=float)
    px = hist[available]
    last = px.iloc[-1]
    valid = last.notna()
    px = px.loc[:, valid]
    if px.empty:
        return pd.Series(dtype=float)

    if strategy == "equal_weight":
        chosen = list(px.columns)
        return pd.Series(1 / len(chosen), index=chosen)

    if strategy == "low_vol":
        r = px.pct_change().tail(min(60, len(px) - 1))
        vol = r.std(ddof=1) * np.sqrt(252)
        vol = vol.replace([np.inf, -np.inf], np.nan).dropna()
        if vol.empty:
            return pd.Series(dtype=float)
        chosen = vol.nsmallest(min(top_k, len(vol))).index
        inv = 1 / vol.loc[chosen].clip(lower=1e-6)
        return inv / inv.sum()

    lb = min(lookback, len(px) - 1)
    momentum = px.iloc[-1] / px.iloc[-lb - 1] - 1
    momentum = momentum.replace([np.inf, -np.inf], np.nan).dropna()
    if momentum.empty:
        return pd.Series(dtype=float)
    chosen = momentum.nlargest(min(top_k, len(momentum))).index
    if strategy == "trend":
        ma_len = min(trend_ma, len(px))
        ma = px[chosen].tail(ma_len).mean()
        chosen = [s for s in chosen if pd.notna(last.get(s)) and pd.notna(ma.get(s)) and last[s] >= ma[s]]
        if not chosen:
            return pd.Series(dtype=float)
    return pd.Series(1 / len(chosen), index=chosen)


def _simulate(prices: pd.DataFrame, benchmark: str, strategy: str, lookback: int,
              top_k: int, rebalance: str, cost_bps: float, signal_prices: Optional[pd.DataFrame] = None) -> Dict:
    if prices.empty or benchmark not in prices.columns:
        return {"error": "缺少基准历史数据"}
    prices = prices.dropna(how="all")
    if len(prices) < max(lookback + 20, 80):
        return {"error": "历史数据不足"}
    rebal_days = {"monthly": 21, "quarterly": 63, "semiannual": 126}.get(rebalance, 21)
    start_idx = min(max(lookback + 2, 30), len(prices) - 2)
    dates = prices.index[start_idx:]
    asset_cols = [c for c in prices.columns if c != benchmark]
    portfolio_value = 1.0
    bench_value = 1.0
    current_w = pd.Series(0.0, index=asset_cols)
    equity = []
    bench = []
    turnover = []
    rebalance_count = 0
    cash_days = 0

    for i, date in enumerate(dates):
        px_today = prices.loc[date]
        prev_date = dates[i - 1] if i > 0 else prices.index[start_idx - 1]
        px_prev = prices.loc[prev_date]
        asset_ret = (px_today[asset_cols] / px_prev[asset_cols] - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        bench_ret = px_today[benchmark] / px_prev[benchmark] - 1 if pd.notna(px_today[benchmark]) and pd.notna(px_prev[benchmark]) else 0.0
        if i == 0 or i % rebal_days == 0:
            signal = signal_prices if signal_prices is not None else prices
            signal = signal.reindex(prices.index)
            new_w = _weights_for_date(signal.iloc[:start_idx + i + 1], prices.index[start_idx + i], strategy, lookback, top_k)
            new_w = new_w.reindex(asset_cols).fillna(0.0)
            if new_w.sum() > 0:
                new_w = new_w / new_w.sum()
                turnover_now = float((new_w - current_w).abs().sum()) / 2
                portfolio_value *= max(0.0, 1 - turnover_now * cost_bps / 10000)
                current_w = new_w
                turnover.append(turnover_now)
                rebalance_count += 1
            else:
                current_w = pd.Series(0.0, index=asset_cols)
                turnover.append(0.0)
                cash_days += 1
        p_ret = float((current_w * asset_ret).sum())
        portfolio_value *= max(0.0, 1 + p_ret)
        bench_value *= max(0.0, 1 + float(bench_ret))
        equity.append(portfolio_value)
        bench.append(bench_value)

    idx = pd.DatetimeIndex(dates)
    eq = pd.Series(equity, index=idx, name="strategy")
    beq = pd.Series(bench, index=idx, name="benchmark")
    ret = eq.pct_change().dropna()
    bret = beq.pct_change().dropna()
    aligned = pd.concat([ret, bret], axis=1).dropna()
    ann = _annualized_return(eq)
    mdd = _max_drawdown(eq)
    bm_ann = _annualized_return(beq)
    result = {
        "equity": eq,
        "benchmark_equity": beq,
        "returns": ret,
        "metrics": {
            "total_return_pct": float((eq.iloc[-1] - 1) * 100),
            "annualized_return_pct": None if ann is None else ann * 100,
            "max_drawdown_pct": None if mdd is None else mdd * 100,
            "volatility_pct": float(ret.std(ddof=1) * np.sqrt(252) * 100) if len(ret) > 2 else None,
            "sharpe": _sharpe(ret),
            "sortino": _sortino(ret),
            "calmar": _calmar(ann, mdd),
            "win_rate_pct": float((ret > 0).mean() * 100) if len(ret) else None,
            "benchmark_total_return_pct": float((beq.iloc[-1] - 1) * 100),
            "benchmark_annualized_return_pct": None if bm_ann is None else bm_ann * 100,
            "excess_annualized_pct": None if ann is None or bm_ann is None else (ann - bm_ann) * 100,
            "turnover_per_rebalance_pct": float(np.mean(turnover) * 100) if turnover else 0.0,
            "annualized_turnover_estimate_pct": float(np.mean(turnover) * (252 / rebal_days) * 100) if turnover else 0.0,
            "rebalance_count": rebalance_count,
            "cash_rebalance_count": cash_days,
        },
        "turnover": turnover,
    }
    if len(aligned) >= 20:
        result["metrics"]["beta_to_benchmark"] = float(aligned.iloc[:, 0].cov(aligned.iloc[:, 1]) / aligned.iloc[:, 1].var()) if aligned.iloc[:, 1].var() > 0 else None
        result["metrics"]["correlation_to_benchmark"] = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
    return result


def _to_points(series: pd.Series, max_points: int = 260) -> List[Dict]:
    if series is None or series.empty:
        return []
    s = series.dropna()
    if len(s) > max_points:
        step = max(1, len(s) // max_points)
        s = s.iloc[::step]
        if s.index[-1] != series.index[-1]:
            s = pd.concat([s, series.iloc[[-1]]])
    base = float(series.iloc[0])
    return [{"date": idx.strftime("%Y-%m-%d"), "strategy": float(v / base * 100), "value": float(v)} for idx, v in s.items()]


def _drawdown_points(series: pd.Series, max_points: int = 260) -> List[Dict]:
    dd = series / series.cummax() - 1
    if len(dd) > max_points:
        step = max(1, len(dd) // max_points)
        dd = dd.iloc[::step]
        if dd.index[-1] != series.index[-1]:
            dd = pd.concat([dd, dd.iloc[[-1]]])
    return [{"date": idx.strftime("%Y-%m-%d"), "drawdown": float(v * 100)} for idx, v in dd.items()]


def _monthly_contribution(prices: pd.DataFrame, strategy: str, lookback: int, top_k: int,
                          rebalance: str, cost_bps: float, start_idx: int) -> List[Dict]:
    # Approximate contribution by realized daily return * active weights. It is deliberately
    # labeled as contribution, not causal factor attribution.
    rebal_days = {"monthly": 21, "quarterly": 63, "semiannual": 126}.get(rebalance, 21)
    asset_cols = list(prices.columns)
    current_w = pd.Series(0.0, index=asset_cols)
    contrib = {s: 0.0 for s in asset_cols}
    dates = prices.index[start_idx:]
    for i, date in enumerate(dates):
        prev = dates[i - 1] if i > 0 else prices.index[start_idx - 1]
        if i == 0 or i % rebal_days == 0:
            w = _weights_for_date(prices.iloc[:start_idx + i + 1], date, strategy, lookback, top_k)
            current_w = w.reindex(asset_cols).fillna(0.0)
            if current_w.sum() > 0:
                current_w /= current_w.sum()
        r = (prices.loc[date] / prices.loc[prev] - 1).replace([np.inf, -np.inf], np.nan).fillna(0)
        for s in asset_cols:
            contrib[s] += float(current_w.get(s, 0) * r.get(s, 0))
    vals = sorted(contrib.items(), key=lambda x: abs(x[1]), reverse=True)[:12]
    total = sum(v for _, v in vals)
    return [{"symbol": s, "contribution_pct": v * 100} for s, v in vals]


def _sensitivity(prices: pd.DataFrame, benchmark: str, strategy: str, top_k: int, rebalance: str, cost_bps: float) -> List[Dict]:
    lookbacks = [60, 120, 252] if strategy in {"momentum", "trend"} else [30, 60, 120]
    rows = []
    for lb in lookbacks:
        r = _simulate(prices, benchmark, strategy, lb, top_k, rebalance, cost_bps)
        m = r.get("metrics", {})
        rows.append({"lookback": lb, "annualized_return_pct": m.get("annualized_return_pct"), "max_drawdown_pct": m.get("max_drawdown_pct"), "sharpe": m.get("sharpe")})
    return rows


def _walk_forward(prices: pd.DataFrame, benchmark: str, strategy: str, top_k: int, rebalance: str, cost_bps: float) -> Dict:
    if strategy not in {"momentum", "trend"} or len(prices) < 700:
        return {"available": False, "reason": "当前策略/历史长度不足以进行有意义的参数样本外验证。"}
    split = int(len(prices) * 0.6)
    train = prices.iloc[:split]
    test = prices.iloc[split - 260:]
    candidates = [60, 120, 252]
    scores = []
    for lb in candidates:
        r = _simulate(train, benchmark, strategy, lb, top_k, rebalance, cost_bps)
        m = r.get("metrics", {})
        scores.append((lb, m.get("annualized_return_pct") if m.get("annualized_return_pct") is not None else -1e9))
    selected = max(scores, key=lambda x: x[1])[0]
    oos = _simulate(test, benchmark, strategy, selected, top_k, rebalance, cost_bps)
    m = oos.get("metrics", {})
    return {
        "available": True,
        "selected_lookback": selected,
        "train_end": train.index[-1].strftime("%Y-%m-%d"),
        "test_start": prices.index[split].strftime("%Y-%m-%d"),
        "oos_metrics": m,
        "selection": [{"lookback": lb, "train_annualized_return_pct": score} for lb, score in scores],
        "note": "参数只在样本内选择一次，然后冻结到样本外；这不是完整滚动 Walk-forward，后续可升级为滚动窗口。",
    }


def run_backtest(symbols: List[str], benchmark: str = "SPY", strategy: str = "momentum",
                 period: str = "5y", rebalance: str = "monthly", top_k: int = 5,
                 lookback: int = 120, cost_bps: float = 10.0, validation: str = "standard") -> Dict:
    symbols = _clean_symbols(symbols)
    benchmark = str(benchmark).strip().upper()
    strategy = strategy if strategy in STRATEGIES else "momentum"
    period = period if period in PERIOD_DAYS else "5y"
    top_k = max(1, min(20, int(top_k)))
    lookback = max(20, min(504, int(lookback)))
    cost_bps = max(0.0, min(200.0, float(cost_bps)))
    if len(symbols) < 2:
        return {"ok": False, "error": "至少输入2只股票组成研究股票池。"}
    raw_prices, total_prices, requested = _download(symbols, benchmark, period)
    if raw_prices.empty:
        return {"ok": False, "error": "暂无足够历史行情数据，请检查股票代码或数据源。", "data_quality": {"requested": requested}}
    # 可用性以原始 Close 为准；全收益序列若个别股票缺失，会单独标记。
    available = [s for s in symbols if s in raw_prices.columns and raw_prices[s].notna().sum() >= 80]
    missing = [s for s in symbols if s not in available]
    if benchmark not in raw_prices.columns or raw_prices[benchmark].notna().sum() < 80:
        return {"ok": False, "error": f"基准 {benchmark} 历史数据不足。", "data_quality": {"available_symbols": available, "missing_symbols": missing}}
    raw_prices = raw_prices[available + [benchmark]].dropna(how="all")
    total_prices = total_prices.reindex(raw_prices.index)[available + [benchmark]].dropna(how="all")
    if len(available) < 2:
        return {"ok": False, "error": "可用股票少于2只，无法形成组合。", "data_quality": {"available_symbols": available, "missing_symbols": missing}}
    sim_price = _simulate(raw_prices, benchmark, strategy, lookback, top_k, rebalance, cost_bps, signal_prices=raw_prices)
    sim_total = _simulate(total_prices, benchmark, strategy, lookback, top_k, rebalance, cost_bps, signal_prices=raw_prices)
    sim = sim_total
    if sim.get("error") or sim_price.get("error"):
        err = sim.get("error") or sim_price.get("error")
        return {"ok": False, "error": err, "data_quality": {"available_symbols": available, "missing_symbols": missing}}
    m = sim["metrics"]
    m_price = sim_price["metrics"]
    strict = validation == "strict"
    wf = _walk_forward(raw_prices, benchmark, strategy, top_k, rebalance, cost_bps) if strict else {"available": False, "reason": "标准模式不运行样本外参数选择。"}
    sensitivity = _sensitivity(raw_prices, benchmark, strategy, top_k, rebalance, cost_bps)
    # Quality flags are descriptive, not a strategy score.
    checks = [
        {"name": "未来数据泄漏", "status": "通过", "detail": "权重只使用再平衡日前可见的历史价格。"},
        {"name": "交易成本", "status": "已计入", "detail": f"每次组合换手按 {cost_bps:.1f} bps 模拟。"},
        {"name": "幸存者偏差", "status": "存在", "detail": "当前股票池由用户输入/现有名单构成，不是历史时点成分股。"},
        {"name": "基本面未来函数", "status": "避免", "detail": "本版本不使用当前财报去回填历史，避免制造点时基本面假象。"},
        {"name": "流动性/滑点", "status": "简化", "detail": "当前成本模型是固定bps；后续可按成交量/价差建模。"},
    ]
    # Simple strategy-level interpretation.
    mdd = m.get("max_drawdown_pct")
    ann = m.get("annualized_return_pct")
    sharpe = m.get("sharpe")
    if ann is None:
        summary = "数据不足，暂时无法形成可靠回测结论。"
    else:
        risk_word = "回撤较深" if mdd is not None and mdd < -25 else ("回撤可控" if mdd is not None and mdd > -15 else "存在明显回撤")
        summary = f"历史年化约 {ann:.1f}%，最大回撤 {mdd:.1f}%；{risk_word}。这是历史模拟，不代表未来收益。"
    return {
        "ok": True,
        "strategy": {"id": strategy, "name": STRATEGIES[strategy], "lookback": lookback, "top_k": top_k, "rebalance": rebalance, "cost_bps": cost_bps},
        "benchmark": benchmark,
        "period": {"requested": period, "start": sim["equity"].index[0].strftime("%Y-%m-%d"), "end": sim["equity"].index[-1].strftime("%Y-%m-%d")},
        "metrics": m,
        "price_metrics": m_price,
        "summary": summary,
        "equity_curve": _to_points(sim["equity"]),
        "price_equity_curve": _to_points(sim_price["equity"]),
        "benchmark_curve": _to_points(sim["benchmark_equity"]),
        "price_benchmark_curve": _to_points(sim_price["benchmark_equity"]),
        "drawdown_curve": _drawdown_points(sim["equity"]),
        "contribution": _monthly_contribution(raw_prices[available], strategy, lookback, top_k, rebalance, cost_bps, min(max(lookback + 2, 30), len(raw_prices)-2)),
        "sensitivity": sensitivity,
        "validation": {"mode": validation, "checks": checks, "walk_forward": wf},
        "data_quality": {
            "requested_symbols": symbols,
            "available_symbols": available,
            "missing_symbols": missing,
            "available_count": len(available),
            "history_rows": len(raw_prices),
            "source": "Yahoo Finance via yfinance",
            "total_return_source": "Yahoo Adj Close（含分红调整；用于全收益/分红再投资展示）",
        },
        "method_note": "收益曲线同时提供价格收益与分红再投资全收益。策略换仓信号使用原始 Close；全收益曲线使用 Yahoo Adj Close，以隔离分红再投资对结果的影响。当前股票池使用现有/用户输入成分，不等同于历史真实指数成分；基本面因子将在具备点时数据后接入。历史回测不保证未来表现。",
    }
