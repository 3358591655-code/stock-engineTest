from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/pro/options", tags=["AEL Pro Options"])

DATA_BASE = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets")
TRADING_BASE = os.getenv(
    "ALPACA_TRADING_BASE_URL",
    "https://paper-api.alpaca.markets" if os.getenv("ALPACA_PAPER_TRADE", "true").lower() in {"1","true","yes"} else "https://api.alpaca.markets",
)
FEED = os.getenv("ALPACA_OPTIONS_FEED", "indicative")
TIMEOUT = float(os.getenv("AEL_PRO_OPTIONS_TIMEOUT", "12"))
MAX_SNAPSHOTS = max(100, min(5000, int(os.getenv("AEL_PRO_OPTIONS_MAX_SNAPSHOTS", "2500"))))
MAX_CONTRACTS = max(100, min(10000, int(os.getenv("AEL_PRO_OPTIONS_MAX_CONTRACTS", "5000"))))


def _headers() -> dict[str, str]:
    key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise HTTPException(
            status_code=503,
            detail="Pro 期权数据源未配置 Alpaca API Key。Lite 不受影响；请在 Railway 环境变量中配置，不要把密钥发到聊天里。",
        )
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"期权数据请求失败：{str(exc)[:180]}")
    if not r.ok:
        msg = ""
        try:
            msg = (r.json() or {}).get("message") or (r.json() or {}).get("error") or ""
        except Exception:
            pass
        raise HTTPException(status_code=r.status_code, detail=f"Alpaca 返回 {r.status_code}：{msg or r.text[:180]}")
    try:
        return r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="期权数据返回不是有效 JSON")


def _finite(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _underlying_symbol(raw: str) -> str:
    s = str(raw or "").strip().upper().replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", s):
        raise HTTPException(status_code=400, detail="Pro 期权驾驶舱目前仅支持美股股票代码")
    return s


def _date_window(dte_min: int, dte_max: int) -> tuple[str, str]:
    today = date.today()
    return (
        (today + timedelta(days=dte_min)).isoformat(),
        (today + timedelta(days=dte_max)).isoformat(),
    )


def _contracts(symbol: str, exp_gte: str, exp_lte: str) -> dict[str, dict[str, Any]]:
    params = {
        "underlying_symbols": symbol,
        "status": "active",
        "expiration_date_gte": exp_gte,
        "expiration_date_lte": exp_lte,
        "limit": MAX_CONTRACTS,
    }
    data = _get(f"{TRADING_BASE}/v2/options/contracts", params)
    items = data.get("option_contracts") or data.get("contracts") or []
    return {str(x.get("symbol")): x for x in items if x.get("symbol")}


def _snapshots(
    symbol: str,
    exp_gte: str,
    exp_lte: str,
    option_type: str | None,
) -> tuple[dict[str, Any], str]:
    params: dict[str, Any] = {
        "feed": FEED,
        "limit": min(1000, MAX_SNAPSHOTS),
        "expiration_date_gte": exp_gte,
        "expiration_date_lte": exp_lte,
    }
    if option_type in {"call", "put"}:
        params["type"] = option_type

    all_snapshots: dict[str, Any] = {}
    token = None
    pages = 0
    while len(all_snapshots) < MAX_SNAPSHOTS and pages < 10:
        if token:
            params["page_token"] = token
        data = _get(f"{DATA_BASE}/v1beta1/options/snapshots/{symbol}", params)
        snaps = data.get("snapshots") or {}
        all_snapshots.update(snaps)
        token = data.get("next_page_token")
        pages += 1
        if not token or not snaps:
            break
    return all_snapshots, FEED


def _normalize(
    snapshots: dict[str, Any],
    contracts: dict[str, dict[str, Any]],
    underlying_price: float | None,
) -> list[dict[str, Any]]:
    today = date.today()
    out = []
    for contract_symbol, snap in snapshots.items():
        meta = contracts.get(contract_symbol) or {}
        exp = meta.get("expiration_date")
        strike = _finite(meta.get("strike_price"))
        typ = str(meta.get("type") or "").lower()
        if not exp or strike is None or typ not in {"call", "put"}:
            m = re.match(r"^([A-Z0-9.-]+?)(\d{6})([CP])(\d{8})$", contract_symbol)
            if not m:
                continue
            ds, typ_code, strike_code = m.group(2), m.group(3), m.group(4)
            try:
                exp = datetime.strptime(ds, "%y%m%d").date().isoformat()
            except ValueError:
                continue
            typ = "call" if typ_code == "C" else "put"
            strike = int(strike_code) / 1000.0
        try:
            dte = (date.fromisoformat(str(exp)) - today).days
        except Exception:
            continue
        if dte < 0:
            continue

        q = snap.get("latestQuote") or snap.get("latest_quote") or {}
        t = snap.get("latestTrade") or snap.get("latest_trade") or {}
        g = snap.get("greeks") or {}
        bid = _finite(q.get("bp") if q.get("bp") is not None else q.get("bid_price"))
        ask = _finite(q.get("ap") if q.get("ap") is not None else q.get("ask_price"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None and bid >= 0 and ask >= bid else None
        delta = _finite(g.get("delta"))
        gamma = _finite(g.get("gamma"))
        theta = _finite(g.get("theta"))
        vega = _finite(g.get("vega"))
        rho = _finite(g.get("rho"))
        iv = _finite(g.get("impliedVolatility") if g.get("impliedVolatility") is not None else g.get("iv"))
        oi = _finite(meta.get("open_interest"))
        volume = _finite(t.get("s") if t.get("s") is not None else t.get("size"))
        spread = (ask - bid) if bid is not None and ask is not None else None
        spread_pct = (spread / mid * 100) if spread is not None and mid and mid > 0 else None
        capital = strike * 100 if typ == "put" else ((underlying_price or strike) * 100)
        premium = mid * 100 if mid is not None else None
        premium_yield = (mid / strike * 100) if mid is not None and strike > 0 and typ == "put" else (
            (mid / (underlying_price or strike) * 100) if mid is not None and (underlying_price or strike) > 0 else None
        )
        annualized = (premium_yield * 365 / dte) if premium_yield is not None and dte > 0 else None
        out.append({
            "contract": contract_symbol,
            "type": typ,
            "expiration": exp,
            "dte": dte,
            "strike": strike,
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega,
            "rho": rho,
            "iv": iv * 100 if iv is not None and iv <= 3 else iv,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
            "open_interest": oi,
            "volume": volume,
            "premium": premium,
            "capital_required": capital,
            "premium_yield": premium_yield,
            "annualized_yield": annualized,
            "underlying_price": underlying_price,
        })
    return out


def _underlying_price(symbol: str) -> float | None:
    data = _get(
        f"{DATA_BASE}/v2/stocks/{symbol}/snapshot",
        {"feed": os.getenv("ALPACA_STOCK_FEED", "iex")},
    )
    snap = data or {}
    trade = snap.get("latestTrade") or {}
    quote = snap.get("latestQuote") or {}
    return _finite(trade.get("p")) or (
        ((_finite(quote.get("bp")) or 0) + (_finite(quote.get("ap")) or 0)) / 2
        if _finite(quote.get("bp")) is not None and _finite(quote.get("ap")) is not None
        else None
    )


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def _money(v: Any) -> float | None:
    x = _finite(v)
    return x if x is not None and x >= 0 else None


def _personalized_delta_profile(
    strategy: str,
    risk_profile: str = "balanced",
    market_view: str = "neutral",
    assignment_tolerance: str = "medium",
) -> dict[str, Any]:
    """Generate a user-facing research Delta target from explicit preferences.

    This is a parameter recommendation, not a probability estimate and not a
    trading instruction. The calculation is deliberately deterministic and
    transparent so the user can reproduce it.
    """
    risk = str(risk_profile or "balanced").lower()
    view = str(market_view or "neutral").lower()
    assignment = str(assignment_tolerance or "medium").lower()
    if risk not in {"conservative", "balanced", "aggressive"}:
        risk = "balanced"
    if view not in {"bullish", "neutral", "bearish"}:
        view = "neutral"
    if assignment not in {"low", "medium", "high"}:
        assignment = "medium"

    base = {"conservative": 0.18, "balanced": 0.25, "aggressive": 0.32}[risk]
    if strategy == "CSP":
        view_adj = {"bullish": 0.035, "neutral": 0.0, "bearish": -0.035}[view]
    else:
        view_adj = {"bullish": -0.035, "neutral": 0.0, "bearish": 0.035}[view]
    assignment_adj = {"low": -0.05, "medium": 0.0, "high": 0.05}[assignment]
    width = {"conservative": 0.04, "balanced": 0.05, "aggressive": 0.06}[risk]
    center = max(0.10, min(0.45, base + view_adj + assignment_adj))
    lo = max(0.05, center - width)
    hi = min(0.50, center + width)
    signed_center = -center if strategy == "CSP" else center
    signed_lo = -hi if strategy == "CSP" else lo
    signed_hi = -lo if strategy == "CSP" else hi
    return {
        "strategy": strategy,
        "risk_profile": risk,
        "market_view": view,
        "assignment_tolerance": assignment,
        "target_abs": round(center, 3),
        "target_delta": round(signed_center, 3),
        "range_abs": [round(lo, 3), round(hi, 3)],
        "range_delta": [round(signed_lo, 3), round(signed_hi, 3)],
        "width": round(width, 3),
        "method": "风险偏好 + 标的观点 + 被指派/行权接受度；DTE作为时间过滤，不直接奖励更短期限。",
        "note": "仅为个性化研究参数，不代表真实概率、保证金要求或交易建议。",
    }


def _strategy_score(row: dict[str, Any], strategy: str, delta_target_abs: float | None = None) -> tuple[float, dict[str, Any]]:
    """Transparent research score; DTE is a filter, not a reward.

    Priority follows the original cockpit: capital-return quality + IV first,
    then Delta, liquidity and spread. Missing factors are excluded from the
    denominator instead of being silently converted to zero.
    """
    target_abs = _finite(delta_target_abs)
    if target_abs is None:
        target_abs = 0.25
    target_abs = _clamp(target_abs, 0.05, 0.80)
    target_delta = -target_abs if strategy == "CSP" else target_abs
    components: list[tuple[str, float, float | None]] = []

    net_ann = _finite(row.get("net_annualized_yield"))
    iv = _finite(row.get("iv"))
    delta = _finite(row.get("delta"))
    oi = _finite(row.get("open_interest"))
    volume = _finite(row.get("volume"))
    spread_pct = _finite(row.get("spread_pct"))

    annual_target = max(1.0, float(os.getenv("AEL_OPTIONS_SCORE_ANNUAL_TARGET", "30")))
    iv_target = max(1.0, float(os.getenv("AEL_OPTIONS_SCORE_IV_TARGET", "40")))

    if net_ann is not None:
        components.append(("资金回报", 30.0, _clamp(net_ann / annual_target * 100)))
    if iv is not None:
        components.append(("IV", 25.0, _clamp(iv / iv_target * 100)))
    if delta is not None:
        components.append(("Delta", 15.0, _clamp(100 - abs(abs(delta) - abs(target_delta)) * 400)))
    if oi is not None or volume is not None:
        liq = 0.0
        if oi is not None:
            liq += min(70.0, math.log10(max(1.0, oi)) / 4.0 * 70.0)
        if volume is not None:
            liq += min(30.0, math.log10(max(1.0, volume)) / 3.0 * 30.0)
        components.append(("流动性", 15.0, _clamp(liq)))
    if spread_pct is not None:
        components.append(("买卖价差", 15.0, _clamp(100 - spread_pct / 8.0 * 100)))

    weight_total = sum(w for _, w, _ in components)
    score = sum(w * (v or 0) / 100.0 for _, w, v in components) / weight_total * 100 if weight_total else None
    breakdown = {
        "weights": {k: w for k, w, _ in components},
        "normalized": {k: round(v, 1) for k, _, v in components if v is not None},
        "formula": "资金回报30% + IV25% + Delta15% + 流动性15% + 买卖价差15%；缺失因子按剩余权重归一化。Delta目标由个性化引擎提供，DTE仅作时间过滤。",
        "weight_coverage": round(weight_total, 1),
    }
    return (round(score, 1) if score is not None else None), breakdown


def _derive_strategy_metrics(
    row: dict[str, Any],
    strategy: str,
    fee_open: float,
    fee_close: float,
    margin_mode: str,
    margin_value: float,
) -> dict[str, Any]:
    gross = _finite(row.get("premium"))
    nominal = _finite(row.get("nominal_value")) or _finite(row.get("capital_required"))
    dte = _finite(row.get("dte"))
    fees = max(0.0, fee_open) + max(0.0, fee_close)
    net = gross - fees if gross is not None else None
    if margin_mode == "fixed":
        occupied = min(nominal, max(0.0, margin_value)) if nominal is not None else None
    elif margin_mode == "cash":
        occupied = nominal
    else:
        ratio = max(0.0, min(1.0, margin_value / 100.0))
        occupied = nominal * ratio if nominal is not None else None
    gross_yield = gross / nominal * 100 if gross is not None and nominal and nominal > 0 else None
    net_yield = net / nominal * 100 if net is not None and nominal and nominal > 0 else None
    capital_yield = net / occupied * 100 if net is not None and occupied and occupied > 0 else None
    monthly = capital_yield * 30.4375 / dte if capital_yield is not None and dte and dte > 0 else None
    annual = capital_yield * 365 / dte if capital_yield is not None and dte and dte > 0 else None
    fee_burden = fees / gross * 100 if gross and gross > 0 else None
    row.update({
        "strategy": strategy,
        "fee_open": round(fee_open, 4),
        "fee_close": round(fee_close, 4),
        "fees_total": round(fees, 4),
        "gross_premium": gross,
        "nominal_value": nominal,
        "simulated_capital": occupied,
        "margin_mode": margin_mode,
        "margin_value": margin_value,
        "gross_yield": gross_yield,
        "net_profit": net,
        "net_yield": net_yield,
        "capital_yield": capital_yield,
        "monthly_simple_yield": monthly,
        "net_annualized_yield": annual,
        "fee_burden_pct": fee_burden,
    })
    return row


def _opening_status(row: dict[str, Any], strategy: str) -> tuple[str, list[str]]:
    reasons = []
    annual_min = float(os.getenv("AEL_OPTIONS_OPEN_MIN_ANNUAL", "10"))
    oi_min = float(os.getenv("AEL_OPTIONS_OPEN_MIN_OI", "100"))
    vol_min = float(os.getenv("AEL_OPTIONS_OPEN_MIN_VOLUME", "5"))
    spread_max = float(os.getenv("AEL_OPTIONS_OPEN_MAX_SPREAD_PCT", "8"))
    fee_max = float(os.getenv("AEL_OPTIONS_OPEN_MAX_FEE_BURDEN", "8"))
    annual = _finite(row.get("net_annualized_yield")); oi = _finite(row.get("open_interest")); vol = _finite(row.get("volume")); spread = _finite(row.get("spread_pct")); fee = _finite(row.get("fee_burden_pct"))
    if annual is None or annual < annual_min: reasons.append(f"净年化收益低于{annual_min:g}%")
    if oi is None or oi < oi_min: reasons.append(f"未平仓量低于{oi_min:g}")
    if vol is None or vol < vol_min: reasons.append(f"今日成交量低于{vol_min:g}")
    if spread is None or spread > spread_max: reasons.append(f"买卖价差超过{spread_max:g}%")
    if fee is not None and fee > fee_max: reasons.append(f"手续费磨损超过{fee_max:g}%")
    if row.get("delta") is None: reasons.append("Delta缺失")
    if not reasons:
        return "OPEN", ["满足当前研究规则"]
    return "WAIT", reasons


@router.get("/health")
def pro_options_health():
    configured = bool(
        (os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID"))
        and (os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY"))
    )
    return {
        "ok": True,
        "configured": configured,
        "feed": FEED,
        "module": "AEL Pro Options",
        "execution": False,
        "message": "仅行情/研究，不包含下单执行",
    }


@router.get("/chain/{symbol}")
def pro_options_chain(
    symbol: str,
    dte_min: int = Query(7, ge=0, le=365),
    dte_max: int = Query(60, ge=1, le=730),
    option_type: str = Query("all"),
    delta_min: float | None = Query(None, ge=-1, le=1),
    delta_max: float | None = Query(None, ge=-1, le=1),
    limit: int = Query(200, ge=10, le=500),
):
    if dte_max < dte_min:
        raise HTTPException(status_code=400, detail="DTE 上限必须大于等于下限")
    sym = _underlying_symbol(symbol)
    exp_gte, exp_lte = _date_window(dte_min, dte_max)
    typ = option_type.lower()
    if typ not in {"all", "call", "put"}:
        raise HTTPException(status_code=400, detail="type 只能是 all / call / put")
    underlying = _underlying_price(sym)
    contracts = _contracts(sym, exp_gte, exp_lte)
    snaps, feed = _snapshots(sym, exp_gte, exp_lte, None if typ == "all" else typ)
    rows = _normalize(snaps, contracts, underlying)

    def keep(x):
        if delta_min is not None and (x["delta"] is None or x["delta"] < delta_min):
            return False
        if delta_max is not None and (x["delta"] is None or x["delta"] > delta_max):
            return False
        return True

    rows = [x for x in rows if keep(x)]
    rows.sort(key=lambda x: (
        x["dte"],
        abs((x["delta"] if x["delta"] is not None else 99) - (-0.25 if x["type"] == "put" else 0.25)),
        abs((x["strike"] / underlying - 1) if underlying else 99),
    ))
    rows = rows[:limit]
    return {
        "ok": True,
        "module": "AEL Pro Options",
        "symbol": sym,
        "underlying_price": underlying,
        "feed": feed,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "dte_min": dte_min, "dte_max": dte_max,
            "type": typ, "delta_min": delta_min, "delta_max": delta_max,
            "limit": limit,
        },
        "count": len(rows),
        "results": rows,
        "data_quality": {
            "real_quotes": True,
            "missing_greeks_not_filled": True,
            "missing_values": "暂无数据",
            "note": "Greeks/IV 由数据源提供；缺失时不自行估算。",
        },
    }


@router.get("/scanner/{symbol}")
def pro_options_scanner(
    symbol: str,
    strategy: str = Query("CSP"),
    dte_min: int = Query(20, ge=1, le=365),
    dte_max: int = Query(45, ge=1, le=730),
    delta_abs_min: float = Query(0.15, ge=0, le=1),
    delta_abs_max: float = Query(0.35, ge=0, le=1),
    fee_open: float = Query(0.0, ge=0, le=1000),
    fee_close: float = Query(0.0, ge=0, le=1000),
    margin_mode: str = Query("ratio"),
    margin_value: float = Query(25.0, ge=0, le=100),
    limit: int = Query(20, ge=1, le=100),
    delta_target_abs: float | None = Query(None, ge=0.05, le=0.80),
    risk_profile: str = Query("balanced"),
    market_view: str = Query("neutral"),
    assignment_tolerance: str = Query("medium"),
):
    strategy = strategy.upper()
    if strategy not in {"CSP", "CC"}:
        raise HTTPException(status_code=400, detail="strategy 只能是 CSP / CC")
    delta_profile = _personalized_delta_profile(strategy, risk_profile, market_view, assignment_tolerance)
    if delta_target_abs is None:
        delta_target_abs = float(delta_profile["target_abs"])
    margin_mode = margin_mode.lower()
    if margin_mode not in {"ratio", "fixed", "cash"}:
        raise HTTPException(status_code=400, detail="margin_mode 只能是 ratio / fixed / cash")
    if margin_mode == "fixed" and margin_value <= 0:
        raise HTTPException(status_code=400, detail="固定金额模式需要大于0的资金占用金额")
    typ = "put" if strategy == "CSP" else "call"
    delta_lo = -delta_abs_max if strategy == "CSP" else delta_abs_min
    delta_hi = -delta_abs_min if strategy == "CSP" else delta_abs_max
    data = pro_options_chain(
        symbol=symbol, dte_min=dte_min, dte_max=dte_max, option_type=typ,
        delta_min=delta_lo, delta_max=delta_hi, limit=500,
    )
    rows = data["results"]
    for row in rows:
        # For CSP the nominal capital is strike*100. For CC it is the
        # underlying value of 100 shares; this is a simulation, not broker
        # margin requirement.
        row["nominal_value"] = row.get("strike", 0) * 100 if strategy == "CSP" else (row.get("underlying_price") or row.get("strike", 0)) * 100
        _derive_strategy_metrics(row, strategy, fee_open, fee_close, margin_mode, margin_value)
        status, reasons = _opening_status(row, strategy)
        row["opening_status"] = status
        row["opening_reasons"] = reasons
        score, breakdown = _strategy_score(row, strategy, delta_target_abs)
        row["strategy_score"] = score
        row["score_breakdown"] = breakdown

    rows.sort(key=lambda x: (
        -(x.get("strategy_score") if x.get("strategy_score") is not None else -1),
        -(x.get("capital_yield") if x.get("capital_yield") is not None else -1),
        x["dte"],
    ))
    data["results"] = rows[:limit]
    data["strategy"] = strategy
    data["delta_profile"] = {**delta_profile, "target_abs": round(float(delta_target_abs), 3), "target_delta": (-float(delta_target_abs) if strategy == "CSP" else float(delta_target_abs)), "personalized": True}
    data["strategy_formula"] = "资金回报30% + IV25% + Delta15% + 流动性15% + 买卖价差15%；缺失因子按剩余权重归一化。Delta与用户个性化目标的贴合度占15%，DTE仅作时间过滤。"
    data["fee_model"] = {"fee_open": fee_open, "fee_close": fee_close, "fees_total": fee_open + fee_close}
    data["capital_model"] = {
        "mode": margin_mode,
        "value": margin_value,
        "note": "模拟资金占用，仅用于收益率研究，不代表券商实际保证金要求。",
    }
    data["opening_rules"] = {
        "min_net_annualized_yield": float(os.getenv("AEL_OPTIONS_OPEN_MIN_ANNUAL", "10")),
        "min_open_interest": float(os.getenv("AEL_OPTIONS_OPEN_MIN_OI", "100")),
        "min_volume": float(os.getenv("AEL_OPTIONS_OPEN_MIN_VOLUME", "5")),
        "max_spread_pct": float(os.getenv("AEL_OPTIONS_OPEN_MAX_SPREAD_PCT", "8")),
        "max_fee_burden_pct": float(os.getenv("AEL_OPTIONS_OPEN_MAX_FEE_BURDEN", "8")),
    }
    data["diagnostics"] = {
        "contracts_returned": len(rows),
        "iv_available": sum(1 for r in rows if r.get("iv") is not None),
        "delta_available": sum(1 for r in rows if r.get("delta") is not None),
        "quotes_available": sum(1 for r in rows if r.get("bid") is not None and r.get("ask") is not None),
        "greeks_available": sum(1 for r in rows if any(r.get(k) is not None for k in ("delta","gamma","theta","vega","rho"))),
        "iv_source_note": "IV/Greeks 仅采用 Alpaca 返回值；缺失不估算。",
    }
    return data

