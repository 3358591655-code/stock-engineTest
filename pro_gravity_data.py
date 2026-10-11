"""Read-only historical proxy and exploratory backtest data for AEL Pro.

This module deliberately keeps proxy research separate from formal Theta.
It uses only packaged, audited CSVs; no interpolation or fabricated rows.
"""
from __future__ import annotations
import csv
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data" / "gravity"
PANEL_FILE = DATA_DIR / "historical_proxy_panel_1997_2024.csv"
SIGNAL_FILE = DATA_DIR / "existing_signal_comparison.csv"
FORWARD_FILE = DATA_DIR / "existing_forward_return_observations.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _num(value: Any):
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (ValueError, TypeError):
        return None


def proxy_history(start_year: int = 1997, end_year: int = 2024) -> dict:
    rows = _read_csv(PANEL_FILE)
    result = []
    for row in rows:
        year = int(row["year"])
        if not start_year <= year <= end_year:
            continue
        result.append({
            "year": year,
            "institution_records": _num(row.get("institution_records")),
            "institution_count": _num(row.get("institution_count")),
            "institution_expected_total_return_median_pct": _num(row.get("institution_expected_total_return_median_pct")),
            "analyst_growth_proxy_pct": _num(row.get("analyst_growth_proxy_pct")),
            "institution_minus_analyst_growth_proxy_median_pct": _num(row.get("institution_minus_analyst_growth_proxy_median_pct")),
            "institution_decomposition_RB_proxy_median_pct": _num(row.get("institution_decomposition_RB_proxy_median_pct")),
            "formal_theta": _num(row.get("formal_theta")),
            "formal_theta_status": row.get("formal_theta_status") or "NOT_IDENTIFIABLE",
            "annual_proxy_status": row.get("annual_proxy_status") or "UNKNOWN",
            "cma_proxy_status": row.get("cma_proxy_status") or "UNKNOWN",
            "source_quality_note": row.get("source_quality_note") or "",
        })
    return {
        "ok": True,
        "data_type": "HISTORICAL_PROXY_PANEL",
        "formal_theta_rows": sum(r["formal_theta"] is not None for r in result),
        "row_count": len(result),
        "start_year": start_year,
        "end_year": end_year,
        "rows": result,
        "warning": "年度/跨来源代理面板；不是正式Theta序列。空值保持为空，不进行插补。",
        "source_file": "data/gravity/historical_proxy_panel_1997_2024.csv",
        "source_note": "机构预期数据集：Dahlquist & Ibert, Institutions Return Expectations Across Assets and Time, DOI 10.17632/xtx7tg6jpx.2；具体字段来源与限制以源CSV为准。",
    }


def exploratory_backtest(horizon_years: int = 1, min_institutions: int = 1) -> dict:
    rows = _read_csv(SIGNAL_FILE)
    horizon_rows = []
    for row in rows:
        try:
            horizon = int(float(row.get("horizon_years", "")))
            min_n = int(float(row.get("min_institutions_same_date", "")))
        except (ValueError, TypeError):
            continue
        if horizon == horizon_years and min_n == min_institutions:
            horizon_rows.append(row)
    clean = []
    for row in horizon_rows:
        clean.append({
            "signal_definition": row.get("signal_definition"),
            "min_institutions_same_date": int(float(row["min_institutions_same_date"])),
            "horizon_years": int(float(row["horizon_years"])),
            "n_dates_total": int(float(row["n_dates_total"])),
            "n_signal_dates": int(float(row["n_signal_dates"])),
            "n_control_dates": int(float(row["n_control_dates"])),
            "signal_mean_cagr_pct": _num(row.get("signal_mean_cagr_pct")),
            "control_mean_cagr_pct": _num(row.get("control_mean_cagr_pct")),
            "signal_median_cagr_pct": _num(row.get("signal_median_cagr_pct")),
            "control_median_cagr_pct": _num(row.get("control_median_cagr_pct")),
            "mean_difference_pp": _num(row.get("mean_difference_pp")),
            "signal_max_drawdown_mean_pct": _num(row.get("signal_max_drawdown_mean_pct")),
            "signal_share_drawdown_le_minus20pct": _num(row.get("signal_share_drawdown_le_minus20pct")),
            "descriptive_exact_permutation_p_if_n_le22": _num(row.get("descriptive_exact_permutation_p_if_n_le22")),
            "signal_dates": [x for x in (row.get("signal_dates") or "").split(";") if x],
        })
    return {
        "ok": True,
        "data_type": "EXPLORATORY_PROXY_BACKTEST",
        "formal_theta_backtest": False,
        "horizon_years": horizon_years,
        "min_institutions": min_institutions,
        "result_count": len(clean),
        "results": clean,
        "warning": "继承的探索性代理比较，不是正式Theta策略回测；小样本、覆盖不均、非完整事件样本，不能证明预测能力或因果关系。",
        "source_file": "data/gravity/existing_signal_comparison.csv",
        "source_note": "既有探索性对照结果，基于已打包的机构预期代理和前瞻收益观测；不等于正式Theta策略回测。",
    }


def proxy_data_status() -> dict:
    panel = _read_csv(PANEL_FILE)
    signals = _read_csv(SIGNAL_FILE)
    forward = _read_csv(FORWARD_FILE)
    formal_rows = sum(bool((r.get("formal_theta") or "").strip()) for r in panel)
    return {
        "ok": True,
        "formal_theta_history_rows": formal_rows,
        "proxy_panel_available": bool(panel),
        "proxy_panel_rows": len(panel),
        "proxy_year_min": min((int(r["year"]) for r in panel), default=None),
        "proxy_year_max": max((int(r["year"]) for r in panel), default=None),
        "exploratory_signal_comparison_rows": len(signals),
        "forward_return_observations": len(forward),
        "formal_backtest_ready": False,
        "formal_backtest_status": "WAITING_FOR_DATE_AND_HORIZON_MATCHED_VALIDATED_INPUTS",
        "message": "正式Theta历史记录为0条；已加载年度历史代理面板和既有探索性收益对照，可用于研究展示，不可标记为正式回测。",
        "proxy_panel_note": "代理数据与正式数据分层展示；不插补、不伪造，不把分析师增长预测当ERP，也不把机构总回报当ERP。",
        "no_imputation": True,
        "no_live_data_claim": True,
    }
