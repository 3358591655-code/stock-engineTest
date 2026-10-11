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


def source_audit() -> dict:
    """Audit whether packaged rows can support a source/date/horizon-matched formal Theta.

    This is intentionally a coverage audit, not a synthetic data builder.
    """
    institutions = _read_csv(DATA_DIR / "institution_CMA_source_panel.csv")
    panel = _read_csv(PANEL_FILE)
    def coverage(rows, field):
        n = len(rows)
        present = sum(bool(str(r.get(field, "")).strip()) for r in rows)
        return {"available_rows": present, "total_rows": n, "coverage_pct": round(present * 100 / n, 2) if n else None}
    fields = {
        "institution_expected_equity_return": "US_Equity",
        "institution_expected_cash_return": "US_Cash",
        "institution_explicit_erp_field": "buyer_erp_pct",
        "institution_real_return_field": "US_Equity_r",
        "institution_inflation_assumption": "US_Inflation",
        "institution_real_earnings_growth": "gB_real_earnings_observed_pct",
        "institution_nominal_earnings_growth_derived": "gB_nominal_earnings_derived_pct",
        "institution_gravity_derived": "RB_derived_pct",
    }
    coverage_map = {label: coverage(institutions, field) for label, field in fields.items()}
    dates = sorted({str(r.get("Date", "")).strip() for r in institutions if str(r.get("Date", "")).strip()})
    formal_rows = sum(bool(str(r.get("formal_theta", "")).strip()) for r in panel)
    erp_g_derivable_rows = sum(
        bool(str(r.get("US_Equity", "")).strip()) and bool(str(r.get("US_Cash", "")).strip())
        and bool(str(r.get("gB_real_earnings_observed_pct", "")).strip())
        for r in institutions
    )
    # A formal observation needs matched buyer ERP/g and objective ERP/g, on a common
    # as-of date and comparable horizon. The packaged table does not contain this full tuple.
    required = ["buyer_erp_pct", "buyer_g_pct", "objective_erp_pct", "objective_g_pct", "risk_free_pct", "horizon_years"]
    return {
        "ok": True,
        "audit_type": "SOURCE_COVERAGE_AUDIT",
        "institution_source": {
            "name": "Dahlquist & Ibert, Institutions’ Return Expectations Across Assets and Time",
            "doi": "10.17632/xtx7tg6jpx.2",
            "rows": len(institutions),
            "providers": len({str(r.get("Provider", "")).strip() for r in institutions if str(r.get("Provider", "")).strip()}),
            "date_min": dates[0] if dates else None,
            "date_max": dates[-1] if dates else None,
            "field_coverage": coverage_map,
            "same_row_equity_minus_cash_plus_g_rows": erp_g_derivable_rows,
            "mapping_blockers": [
                "US_Cash is absent from the packaged institution_CMA_source_panel.csv; expected equity return alone cannot be treated as ERP.",
                "The source panel does not contain an explicit buyer_erp_pct field; full replication archive schema must be reviewed before mapping.",
                "Institution growth decomposition remains missing on a large share of rows; missing g is not imputed."
            ],
        },
        "objective_source": {
            "candidate_method": "Dahlquist-Ibert objective ERP: ln(1 + 1/CAPE) minus matched 10-year real yield",
            "required_sources": ["Shiller monthly CAPE history", "matched-date 10-year real Treasury yield"],
            "matched_objective_observations_loaded": 0,
        },
        "formal_theta_history_rows": formal_rows,
        "formal_backtest_ready": False,
        "missing_requirements": required,
        "why_not_formal": [
            "The packaged institution_CMA_source_panel.csv has no US_Cash field or explicit ERP field, so a same-row equity-minus-cash buyer ERP cannot be derived from this panel alone.",
            "Institution expected total return is not itself ERP.",
            "Institutional growth decomposition exists only for a subset and is not date/horizon matched to an objective-side ERP/g series.",
            "The packaged files contain no validated, matched objective g series and no full as-of input tuples.",
            "A formal backtest requires historical as-of signals plus forward returns; proxy signal comparisons are not a substitute.",
        ],
        "next_data_tasks": [
            "Import the full published institutional subjective ERP panel, not just expected total-return fields.",
            "Build objective ERP history from Shiller CAPE and real Treasury yields with a documented same-date rule.",
            "Define and source objective g independently, with a fixed horizon and no post-date revisions.",
            "Join only on valid as-of date and horizon; publish row-level source lineage before running out-of-sample tests.",
        ],
        "no_imputation": True,
        "no_fabricated_values": True,
    }
