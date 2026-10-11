"""AEL Pro Double-Gravity scenario calculator.

Manual scenario inputs only. This module intentionally does not fetch or fabricate
historical data and never labels user-entered values as a validated formal Theta.
Rates are percentage points (4.5 means 4.5%).
"""
from __future__ import annotations
import math
from typing import Optional

THETA_GUARD_PP = 0.25

def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False

def calculate_scenario(rf_pct: Optional[float], erp_objective_pct: Optional[float],
                       g_objective_pct: Optional[float], erp_buyer_pct: Optional[float],
                       g_buyer_pct: Optional[float], as_of_date: Optional[str] = None,
                       horizon_years: Optional[float] = None) -> dict:
    names = {
        "rf_pct": rf_pct,
        "erp_objective_pct": erp_objective_pct,
        "g_objective_pct": g_objective_pct,
        "erp_buyer_pct": erp_buyer_pct,
        "g_buyer_pct": g_buyer_pct,
    }
    missing = [key for key, value in names.items() if not _finite(value)]
    base = {
        "ok": True,
        "module": "AEL_PRO_DOUBLE_GRAVITY",
        "calculation_type": "USER_ENTERED_SCENARIO",
        "formal_theta": None,
        "formal_backtest_status": "NOT_VALIDATED",
        "formal_backtest_observations": 0,
        "input_status": "MISSING_INPUTS" if missing else "SCENARIO_CALCULATED",
        "missing_inputs": missing,
        "as_of_date": as_of_date or None,
        "horizon_years": float(horizon_years) if _finite(horizon_years) else None,
        "rate_unit": "percentage points; e.g. 4.5 means 4.5%",
        "formula": {
            "R_objective": "Rf + (ERP_objective - g_objective)",
            "R_buyer": "Rf + (ERP_buyer - g_buyer)",
            "delta_R": "R_objective - R_buyer",
            "Theta": "R_objective / R_buyer; suppressed when |R_buyer| < 0.25 pp",
        },
        "data_warning": "这是手动输入的情景计算，不是已验证的历史正式Theta，也不代表实时市场数据。",
    }
    if missing:
        base["message"] = "请填写全部五项数据；系统不会估算或自动补齐。"
        return base
    rf, eo, go, eb, gb = (float(names[k]) for k in names)
    ro = rf + eo - go
    rb = rf + eb - gb
    delta = ro - rb
    if rb < 0:
        state = "NEGATIVE_GRAVITY_CANDIDATE"
        interpretation = "买方重力为负：可列为预警候选，但不能单凭此项认定泡沫。"
    elif abs(rb) < THETA_GUARD_PP:
        state = "NEAR_ZERO"
        interpretation = "买方重力接近零，Theta 分母不稳定；不输出Theta。"
    else:
        state = "POSITIVE_GRAVITY"
        interpretation = "买方重力为正；仍需结合历史回测与其他证据判断。"
    theta = None if abs(rb) < THETA_GUARD_PP else ro / rb
    if rb <= 0:
        theta_warning = "R_buyer <= 0 时，Theta的符号/绝对值不能作为单调泡沫评分。"
    else:
        theta_warning = "Theta仅为情景比值；未经过历史预测能力验证，不可当作概率或评分。"
    base.update({
        "R_objective_scenario_pct": round(ro, 8),
        "R_buyer_scenario_pct": round(rb, 8),
        "delta_R_scenario_pp": round(delta, 8),
        "theta_scenario": round(theta, 8) if theta is not None and math.isfinite(theta) else None,
        "buyer_gravity_state": state,
        "interpretation": interpretation,
        "theta_warning": theta_warning,
        "input_values": {k: float(v) for k, v in names.items()},
        "formal_status": "NOT_FORMALLY_IDENTIFIED",
        "formal_reason": "Manual scenario inputs have no source-level/date/horizon validation attached.",
    })
    return base

def data_status() -> dict:
    return {
        "ok": True,
        "module": "AEL_PRO_DOUBLE_GRAVITY",
        "formal_theta_history_rows": 0,
        "proxy_panel_available": True,
        "proxy_panel_note": "现有历史数据主要为年度/跨来源代理，不能直接充当正式Theta序列。",
        "formal_backtest_ready": False,
        "formal_backtest_status": "WAITING_FOR_DATE_AND_HORIZON_MATCHED_VALIDATED_INPUTS",
        "message": "正式Theta历史记录为0条；目前仅开放手动情景计算与数据质量状态。",
        "no_imputation": True,
        "no_live_data_claim": True,
    }
