"""Year-anchored forward performance for AEL Pro gravity research.

Uses Yahoo Finance chart observations for actual index-price performance after a
user-selected calendar year. This is a benchmark-performance lookup, not proof
that Buffett/CAPE/Theta signals predicted that performance. Indicator history is
reported unavailable until validated time-series inputs are packaged.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from calendar import monthrange
import math
import requests

INDEX_MAP = {
    "SP500": {"symbol": "^GSPC", "name": "标普500"},
    "NASDAQ": {"symbol": "^IXIC", "name": "纳斯达克综合指数"},
    "DOW": {"symbol": "^DJI", "name": "道琼斯工业平均指数"},
}
ALLOWED_HORIZONS = (1, 3, 5, 10)


def _anniversary(year: int, anchor: date, horizon: int) -> date:
    target_year = anchor.year + horizon
    day = min(anchor.day, monthrange(target_year, anchor.month)[1])
    return date(target_year, anchor.month, day)


def _load_chart(symbol: str, year: int, today: date):
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    # Request data through today so future horizons are naturally reported unavailable.
    end = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol, safe='')}"
    response = requests.get(
        url,
        params={"period1": start, "period2": end, "interval": "1d", "events": "history", "includeAdjustedClose": "true"},
        headers={"User-Agent": "Mozilla/5.0 (AEL historical research)"},
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json() or {}
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        error = ((payload.get("chart") or {}).get("error") or {}).get("description") or "Yahoo Finance 没有返回历史行情"
        raise RuntimeError(error)
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0] or {}
    closes = quote.get("close") or []
    adj = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes):
            continue
        raw = adj[i] if i < len(adj) and adj[i] is not None else closes[i]
        if raw is None:
            continue
        try:
            value = float(raw)
            if not math.isfinite(value) or value <= 0:
                continue
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
            rows.append((day, value, "adjclose" if i < len(adj) and adj[i] is not None else "close"))
        except (TypeError, ValueError, OverflowError):
            continue
    rows.sort(key=lambda r: r[0])
    return rows


def year_forward_performance(year: int, index: str = "SP500", metric: str = "gravity_ratio", today: date | None = None) -> dict:
    today = today or date.today()
    if index not in INDEX_MAP:
        return {"ok": False, "error": "index 仅支持 SP500、NASDAQ、DOW"}
    metric_labels = {"buffett": "巴菲特指标", "cape": "席勒市盈率（CAPE）", "R_actual": "R实际", "R_objective": "R客观", "gravity_ratio": "重力比值 Θ"}
    if metric not in metric_labels:
        return {"ok": False, "error": "metric 仅支持 buffett、cape、R_actual、R_objective、gravity_ratio"}
    if year < 1900 or year > today.year:
        return {"ok": False, "error": f"年份须在 1900 至 {today.year} 之间"}
    meta = INDEX_MAP[index]
    base = {
        "ok": True,
        "module": "AEL_PRO_YEAR_FORWARD_BACKTEST",
        "selected_year": year,
        "selected_metric": metric,
        "selected_metric_label": metric_labels[metric],
        "selected_metric_value": None,
        "index": index,
        "index_name": meta["name"],
        "provider_symbol": meta["symbol"],
        "data_type": "OBSERVED_INDEX_FORWARD_PERFORMANCE",
        "indicator_signal_backtest_ready": False,
        "indicator_values": {
            "buffett_indicator_pct_gdp": None,
            "shiller_cape": None,
            "R_actual": None,
            "R_objective": None,
            "gravity_ratio": None,
        },
        "indicator_data_status": f"{metric_labels[metric]}：该年份的可验证历史指标值暂无数据，暂不能形成该指标的正式信号回测。下表只展示对应指数此后的实际表现。",
        "horizons": [],
        "source": "Yahoo Finance Chart API",
        "warning": "展示所选年份年末基准点之后的指数表现；仅在可用时使用调整收盘价，否则使用收盘价。该结果是历史市场表现，不代表巴菲特指标、CAPE 或 AEL 重力比值的预测回测。未到期或缺失的期限显示暂无数据。",
    }
    try:
        rows = _load_chart(meta["symbol"], year, today)
    except Exception as exc:
        base.update({"ok": False, "error": f"历史行情获取失败：{str(exc)[:220]}", "data_status": "SOURCE_UNAVAILABLE"})
        return base
    year_rows = [r for r in rows if r[0].year == year]
    if not year_rows:
        base.update({"ok": False, "error": f"{year} 年没有可用的指数历史价格", "data_status": "NO_ANCHOR_PRICE"})
        return base
    anchor_day, anchor_price, anchor_kind = year_rows[-1]
    all_kinds = {r[2] for r in rows}
    kind_label = "调整收盘价（Yahoo 提供）" if anchor_kind == "adjclose" else "收盘价（未提供调整收盘价时的价格收益代理）"
    base.update({
        "data_status": "OK",
        "anchor_date": anchor_day.isoformat(),
        "anchor_value": round(anchor_price, 6),
        "price_series_used": kind_label,
        "history_first_date": rows[0][0].isoformat() if rows else None,
        "history_last_date": rows[-1][0].isoformat() if rows else None,
    })
    for horizon in ALLOWED_HORIZONS:
        target = _anniversary(year, anchor_day, horizon)
        candidates = [r for r in rows if r[0] >= target]
        if target > today or not candidates:
            base["horizons"].append({"years": horizon, "target_date": target.isoformat(), "actual_date": None, "return_pct": None, "status": "NOT_MATURED_OR_MISSING", "message": "暂无数据（期限尚未到期或源数据缺失）"})
            continue
        actual_day, actual_price, actual_kind = candidates[0]
        ret = (actual_price / anchor_price - 1.0) * 100.0
        base["horizons"].append({
            "years": horizon,
            "target_date": target.isoformat(),
            "actual_date": actual_day.isoformat(),
            "start_value": round(anchor_price, 6),
            "end_value": round(actual_price, 6),
            "return_pct": round(ret, 4),
            "status": "OBSERVED",
            "price_series_used": "adjusted_close" if actual_kind == "adjclose" and anchor_kind == "adjclose" else "close_price_proxy",
        })
    return base
