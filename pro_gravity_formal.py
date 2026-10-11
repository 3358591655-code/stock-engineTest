"""Strict point-in-time double-gravity data engine.

This engine intentionally accepts only explicitly mapped buyer ERP/g and
objective ERP/g. It never treats expected total return as ERP, and it never
fills missing rows. Data tables live under data/gravity/formal/.
"""
from __future__ import annotations

import csv
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

BASE = Path(__file__).resolve().parent
FORMAL_DIR = BASE / "data" / "gravity" / "formal"
BUYER_FILE = FORMAL_DIR / "buyer_expectations.csv"
OBJECTIVE_FILE = FORMAL_DIR / "objective_expectations.csv"
MARKET_FILE = FORMAL_DIR / "market_total_return.csv"
MACRO_CAPACITY_FILE = FORMAL_DIR / "macro_capacity_inputs.csv"

BUYER_FIELDS = [
    "as_of_date", "available_on", "institution", "asset", "horizon_years",
    "buyer_erp_pct", "buyer_g_pct", "buyer_g_definition", "source_name",
    "source_url", "source_field_erp", "source_field_g",
]
OBJECTIVE_FIELDS = [
    "as_of_date", "available_on", "horizon_years", "risk_free_pct",
    "objective_erp_pct", "objective_erp_method", "objective_g_pct", "objective_g_definition",
    "cape", "buffett", "source_name", "source_url",
    "cape_source_name", "cape_source_url", "risk_free_source_name", "risk_free_source_url",
    "objective_g_source_name", "objective_g_source_url", "buffett_source_name", "buffett_source_url",
]
MARKET_FIELDS = ["date", "index", "total_return_index", "source_name", "source_url"]
MACRO_CAPACITY_FIELDS = [
    "as_of_date", "available_on", "alpha", "m1_value", "m1_unit", "velocity_v", "velocity_definition",
    "alpha_source_name", "alpha_source_url", "m1_source_name", "m1_source_url",
    "velocity_source_name", "velocity_source_url",
]
DEFAULT_DATE_TOLERANCE_DAYS = 45
DEFAULT_HORIZON_TOLERANCE_YEARS = 1.0
THETA_DENOMINATOR_GUARD_PP = 0.25
NEGATIVE_GRAVITY_THRESHOLD = 0.0


def _path_label(path: Path) -> str:
    try:
        return str(path.relative_to(BASE))
    except ValueError:
        return str(path)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _float(v: Any) -> float | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        n = float(v)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def _date(v: Any) -> date | None:
    if v is None:
        return None
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except (TypeError, ValueError):
        return None


def _nonempty(row: dict[str, str], key: str) -> bool:
    return bool(str(row.get(key, "")).strip())


def _is_explicit_erp_field(value: Any) -> bool:
    """Accept explicit ERP fields or a same-row equity-minus-cash derivation.

    An expected equity return alone is not ERP. The derivation phrase is accepted
    only when both equity and cash return fields are explicitly named.
    """
    raw = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    if not raw:
        return False
    is_equity_minus_cash = "equity return" in raw and "cash return" in raw and (
        "minus" in raw or "same observation" in raw
    )
    if is_equity_minus_cash:
        return True
    if any(term in raw for term in ("total return", "expected return", "us equity", "equity return")) and not any(term in raw for term in ("premium", "risk premium", "erp")):
        return False
    return ("erp" in raw or ("risk" in raw and "premium" in raw)
            or ("equity" in raw and "premium" in raw)
            or "excess return" in raw)


def _has_headers(path: Path, fields: list[str]) -> tuple[bool, list[str]]:
    rows = _read_csv(path)
    actual = list(rows[0].keys()) if rows else []
    # DictReader preserves header names even for an empty header-only template.
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            actual = reader.fieldnames or []
    return set(fields).issubset(set(actual)), actual


def _parse_buyer_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    parsed: list[dict[str, Any]] = []
    counts = {"rows": len(rows), "missing_erp": 0, "missing_g": 0, "bad_dates": 0,
              "missing_source_lineage": 0, "invalid_horizon": 0, "unsupported_asset": 0,
              "erp_source_field_not_explicit": 0}
    for raw in rows:
        asset = (raw.get("asset") or "US_EQUITY").strip().upper()
        # This model version is explicitly scoped to US equity observations only.
        if asset != "US_EQUITY":
            counts["unsupported_asset"] += 1
            continue
        d = _date(raw.get("as_of_date"))
        available = _date(raw.get("available_on"))
        erp, g, horizon = _float(raw.get("buyer_erp_pct")), _float(raw.get("buyer_g_pct")), _float(raw.get("horizon_years"))
        if erp is None:
            counts["missing_erp"] += 1
        if _nonempty(raw, "source_field_erp") and not _is_explicit_erp_field(raw.get("source_field_erp")):
            counts["erp_source_field_not_explicit"] += 1
        if g is None:
            counts["missing_g"] += 1
        if d is None or available is None or available < d:
            counts["bad_dates"] += 1
        if horizon is None or horizon <= 0:
            counts["invalid_horizon"] += 1
        if not all(_nonempty(raw, k) for k in ("institution", "source_name", "source_url", "source_field_erp", "source_field_g", "buyer_g_definition")):
            counts["missing_source_lineage"] += 1
        if (d is None or available is None or available < d or erp is None or g is None
                or horizon is None or horizon <= 0
                or not all(_nonempty(raw, k) for k in ("institution", "source_name", "source_url", "source_field_erp", "source_field_g", "buyer_g_definition"))
                or not _is_explicit_erp_field(raw.get("source_field_erp"))):
            continue
        parsed.append({
            "as_of_date": d, "available_on": available,
            "institution": raw["institution"].strip(), "asset": asset,
            "horizon_years": horizon, "buyer_erp_pct": erp, "buyer_g_pct": g,
            "buyer_g_definition": raw["buyer_g_definition"].strip(),
            "source_name": raw["source_name"].strip(), "source_url": raw["source_url"].strip(),
            "source_field_erp": raw["source_field_erp"].strip(), "source_field_g": raw["source_field_g"].strip(),
        })
    return parsed, counts


def _parse_objective_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    parsed: list[dict[str, Any]] = []
    counts = {"rows": len(rows), "missing_erp": 0, "missing_g": 0, "missing_risk_free": 0,
              "bad_dates": 0, "missing_source_lineage": 0, "invalid_horizon": 0, "missing_erp_method": 0,
              "missing_cape_lineage": 0, "missing_real_yield_lineage": 0, "missing_g_lineage": 0,
              "missing_buffett_lineage": 0}
    for raw in rows:
        d, available = _date(raw.get("as_of_date")), _date(raw.get("available_on"))
        erp, g, rf, horizon = (_float(raw.get("objective_erp_pct")), _float(raw.get("objective_g_pct")),
                               _float(raw.get("risk_free_pct")), _float(raw.get("horizon_years")))
        if erp is None: counts["missing_erp"] += 1
        if g is None: counts["missing_g"] += 1
        if rf is None: counts["missing_risk_free"] += 1
        if d is None or available is None or available < d: counts["bad_dates"] += 1
        if horizon is None or horizon <= 0: counts["invalid_horizon"] += 1
        if not all(_nonempty(raw, k) for k in ("source_name", "source_url", "objective_g_definition")):
            counts["missing_source_lineage"] += 1
        if not all(_nonempty(raw, k) for k in ("cape_source_name", "cape_source_url")):
            counts["missing_cape_lineage"] += 1
        if not all(_nonempty(raw, k) for k in ("risk_free_source_name", "risk_free_source_url")):
            counts["missing_real_yield_lineage"] += 1
        if not all(_nonempty(raw, k) for k in ("objective_g_source_name", "objective_g_source_url")):
            counts["missing_g_lineage"] += 1
        if _float(raw.get("buffett")) is not None and not all(_nonempty(raw, k) for k in ("buffett_source_name", "buffett_source_url")):
            counts["missing_buffett_lineage"] += 1
        if not _nonempty(raw, "objective_erp_method"):
            counts["missing_erp_method"] += 1
        mandatory_lineage = all(_nonempty(raw, k) for k in (
            "source_name", "source_url", "objective_g_definition", "objective_erp_method",
            "cape_source_name", "cape_source_url", "risk_free_source_name", "risk_free_source_url",
            "objective_g_source_name", "objective_g_source_url"))
        buffett_value = _float(raw.get("buffett"))
        if (d is None or available is None or available < d or erp is None or g is None or rf is None
                or horizon is None or horizon <= 0 or not mandatory_lineage
                or (_float(raw.get("cape")) is not None and _float(raw.get("cape")) <= 0)
                or (buffett_value is not None and not all(_nonempty(raw, k) for k in ("buffett_source_name", "buffett_source_url")))):
            continue
        parsed.append({
            "as_of_date": d, "available_on": available, "horizon_years": horizon,
            "risk_free_pct": rf, "objective_erp_pct": erp, "objective_g_pct": g,
            "objective_erp_method": raw["objective_erp_method"].strip(),
            "objective_g_definition": raw["objective_g_definition"].strip(),
            "cape": _float(raw.get("cape")), "buffett": buffett_value,
            "source_name": raw["source_name"].strip(), "source_url": raw["source_url"].strip(),
            "cape_source_name": raw["cape_source_name"].strip(), "cape_source_url": raw["cape_source_url"].strip(),
            "risk_free_source_name": raw["risk_free_source_name"].strip(), "risk_free_source_url": raw["risk_free_source_url"].strip(),
            "objective_g_source_name": raw["objective_g_source_name"].strip(), "objective_g_source_url": raw["objective_g_source_url"].strip(),
            "buffett_source_name": raw.get("buffett_source_name", "").strip(), "buffett_source_url": raw.get("buffett_source_url", "").strip(),
        })
    return parsed, counts


def _parse_market_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    parsed: list[dict[str, Any]] = []
    counts = {"rows": len(rows), "bad_dates": 0, "bad_total_return_index": 0, "missing_source_lineage": 0}
    for raw in rows:
        d, value = _date(raw.get("date")), _float(raw.get("total_return_index"))
        if d is None: counts["bad_dates"] += 1
        if value is None or value <= 0: counts["bad_total_return_index"] += 1
        if not all(_nonempty(raw, k) for k in ("index", "source_name", "source_url")):
            counts["missing_source_lineage"] += 1
        if (d is None or value is None or value <= 0
                or not all(_nonempty(raw, k) for k in ("index", "source_name", "source_url"))):
            continue
        parsed.append({"date": d, "index": raw["index"].strip().upper(), "total_return_index": value,
                       "source_name": raw["source_name"].strip(), "source_url": raw["source_url"].strip()})
    parsed.sort(key=lambda r: (r["index"], r["date"]))
    return parsed, counts


def _parse_macro_capacity_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    parsed: list[dict[str, Any]] = []
    counts = {"rows": len(rows), "bad_dates": 0, "bad_alpha": 0, "bad_m1": 0,
              "bad_velocity": 0, "missing_source_lineage": 0}
    for raw in rows:
        d, available = _date(raw.get("as_of_date")), _date(raw.get("available_on"))
        alpha, m1, velocity = _float(raw.get("alpha")), _float(raw.get("m1_value")), _float(raw.get("velocity_v"))
        if d is None or available is None or available < d:
            counts["bad_dates"] += 1
        if alpha is None or alpha < 0 or alpha > 1:
            counts["bad_alpha"] += 1
        if m1 is None or m1 <= 0 or not _nonempty(raw, "m1_unit"):
            counts["bad_m1"] += 1
        if velocity is None or velocity <= 0 or not _nonempty(raw, "velocity_definition"):
            counts["bad_velocity"] += 1
        source_fields = ("alpha_source_name", "alpha_source_url", "m1_source_name", "m1_source_url",
                         "velocity_source_name", "velocity_source_url")
        if not all(_nonempty(raw, k) for k in source_fields):
            counts["missing_source_lineage"] += 1
        if (d is None or available is None or available < d or alpha is None or alpha < 0 or alpha > 1
                or m1 is None or m1 <= 0 or not _nonempty(raw, "m1_unit")
                or velocity is None or velocity <= 0 or not _nonempty(raw, "velocity_definition")
                or not all(_nonempty(raw, k) for k in source_fields)):
            continue
        parsed.append({"as_of_date": d, "available_on": available, "alpha": alpha, "m1_value": m1,
                       "m1_unit": raw["m1_unit"].strip(), "velocity_v": velocity,
                       "velocity_definition": raw["velocity_definition"].strip(),
                       "alpha_source_name": raw["alpha_source_name"].strip(), "alpha_source_url": raw["alpha_source_url"].strip(),
                       "m1_source_name": raw["m1_source_name"].strip(), "m1_source_url": raw["m1_source_url"].strip(),
                       "velocity_source_name": raw["velocity_source_name"].strip(), "velocity_source_url": raw["velocity_source_url"].strip()})
    parsed.sort(key=lambda r: r["as_of_date"])
    return parsed, counts


def _match_macro_capacity(target_date: date, macro_rows: list[dict[str, Any]], tolerance_days: int) -> dict[str, Any] | None:
    candidates = [r for r in macro_rows if r["as_of_date"] <= target_date
                  and (target_date - r["as_of_date"]).days <= tolerance_days]
    return candidates[-1] if candidates else None


def load_validated_data() -> dict[str, Any]:
    b_rows = _read_csv(BUYER_FILE)
    o_rows = _read_csv(OBJECTIVE_FILE)
    m_rows = _read_csv(MARKET_FILE)
    c_rows = _read_csv(MACRO_CAPACITY_FILE)
    buyers, b_counts = _parse_buyer_rows(b_rows)
    objectives, o_counts = _parse_objective_rows(o_rows)
    markets, m_counts = _parse_market_rows(m_rows)
    capacity, c_counts = _parse_macro_capacity_rows(c_rows)
    return {"buyers": buyers, "objectives": objectives, "markets": markets, "capacity": capacity,
            "buyer_counts": b_counts, "objective_counts": o_counts, "market_counts": m_counts, "capacity_counts": c_counts}


def _match_objective(buyer: dict[str, Any], objectives: list[dict[str, Any]],
                     date_tolerance_days: int, horizon_tolerance_years: float) -> dict[str, Any] | None:
    # Objective observation itself must not be later than the buyer observation.
    candidates = [o for o in objectives
                  if o["as_of_date"] <= buyer["as_of_date"]
                  and 0 <= (buyer["as_of_date"] - o["as_of_date"]).days <= date_tolerance_days
                  and abs(o["horizon_years"] - buyer["horizon_years"]) <= horizon_tolerance_years]
    if not candidates:
        return None
    candidates.sort(key=lambda o: (o["as_of_date"], o["available_on"]))
    return candidates[-1]


def build_formal_history(*, date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS,
                         horizon_tolerance_years: float = DEFAULT_HORIZON_TOLERANCE_YEARS) -> dict[str, Any]:
    data = load_validated_data()
    buyers, objectives = data["buyers"], data["objectives"]
    matched = []
    unmatched_buyer = 0
    for b in buyers:
        objective = _match_objective(b, objectives, date_tolerance_days, horizon_tolerance_years)
        if objective is None:
            unmatched_buyer += 1
            continue
        # The core gravity signal is formed only once both report values are available.
        target_date = max(b["as_of_date"], objective["as_of_date"])
        capacity = _match_macro_capacity(target_date, data["capacity"], date_tolerance_days)
        signal_date = max(b["available_on"], objective["available_on"], capacity["available_on"] if capacity else date.min)
        r_buyer = objective["risk_free_pct"] + b["buyer_erp_pct"] - b["buyer_g_pct"]
        r_objective = objective["risk_free_pct"] + objective["objective_erp_pct"] - objective["objective_g_pct"]
        delta = r_objective - r_buyer
        theta = r_objective / r_buyer if r_buyer > THETA_DENOMINATOR_GUARD_PP else None
        # Original A/M model: [A/M] = (alpha * M1 * V) / Ry*. Ry is stored as % points,
        # therefore convert to a decimal fraction before division. No result for nonpositive Ry.
        capacity_numerator = capacity["alpha"] * capacity["m1_value"] * capacity["velocity_v"] if capacity else None
        a_m_objective = capacity_numerator / (r_objective / 100.0) if capacity_numerator is not None and r_objective > 0 else None
        a_m_buyer = capacity_numerator / (r_buyer / 100.0) if capacity_numerator is not None and r_buyer > 0 else None
        matched.append({
            "signal_date": signal_date, "buyer_as_of_date": b["as_of_date"],
            "objective_as_of_date": objective["as_of_date"],
            "buyer_available_on": b["available_on"], "objective_available_on": objective["available_on"],
            "institution": b["institution"], "asset": b["asset"],
            "buyer_horizon_years": b["horizon_years"], "objective_horizon_years": objective["horizon_years"],
            "horizon_gap_years": round(b["horizon_years"] - objective["horizon_years"], 6),
            "risk_free_pct": objective["risk_free_pct"], "buyer_erp_pct": b["buyer_erp_pct"],
            "buyer_g_pct": b["buyer_g_pct"], "buyer_g_definition": b["buyer_g_definition"],
            "objective_erp_pct": objective["objective_erp_pct"], "objective_g_pct": objective["objective_g_pct"],
            "objective_erp_method": objective["objective_erp_method"],
            "objective_g_definition": objective["objective_g_definition"],
            "R_buyer_pct": r_buyer, "R_objective_pct": r_objective,
            "Ry_buyer_pct": r_buyer, "Ry_objective_pct": r_objective,
            "delta_R_pp": delta, "theta": theta,
            "alpha": capacity["alpha"] if capacity else None,
            "M1_value": capacity["m1_value"] if capacity else None,
            "M1_unit": capacity["m1_unit"] if capacity else None,
            "V": capacity["velocity_v"] if capacity else None,
            "A_over_M_objective": a_m_objective, "A_over_M_buyer": a_m_buyer,
            "A_over_M_unit": capacity["m1_unit"] if capacity else None,
            "macro_capacity_as_of_date": capacity["as_of_date"].isoformat() if capacity else None,
            "macro_capacity_available_on": capacity["available_on"].isoformat() if capacity else None,
            "alpha_source_name": capacity["alpha_source_name"] if capacity else None,
            "alpha_source_url": capacity["alpha_source_url"] if capacity else None,
            "m1_source_name": capacity["m1_source_name"] if capacity else None,
            "m1_source_url": capacity["m1_source_url"] if capacity else None,
            "velocity_source_name": capacity["velocity_source_name"] if capacity else None,
            "velocity_source_url": capacity["velocity_source_url"] if capacity else None,
            "negative_gravity_alert": r_buyer <= NEGATIVE_GRAVITY_THRESHOLD,
            "high_gravity_gap_alert": delta >= 1.0,
            "cape": objective.get("cape"), "buffett": objective.get("buffett"),
            "buyer_source_name": b["source_name"], "buyer_source_url": b["source_url"],
            "buyer_source_field_erp": b["source_field_erp"], "buyer_source_field_g": b["source_field_g"],
            "objective_source_name": objective["source_name"], "objective_source_url": objective["source_url"],
            "cape_source_name": objective["cape_source_name"], "cape_source_url": objective["cape_source_url"],
            "risk_free_source_name": objective["risk_free_source_name"], "risk_free_source_url": objective["risk_free_source_url"],
            "objective_g_source_name": objective["objective_g_source_name"], "objective_g_source_url": objective["objective_g_source_url"],
            "buffett_source_name": objective["buffett_source_name"], "buffett_source_url": objective["buffett_source_url"],
        })
    matched.sort(key=lambda r: (r["signal_date"], r["institution"]))
    # Collapse cross-institution rows into same-date signals, retaining source rows in an audit list.
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in matched:
        by_date.setdefault(row["signal_date"].isoformat(), []).append(row)
    history = []
    for signal_date, rows in sorted(by_date.items()):
        rb = [r["R_buyer_pct"] for r in rows]
        ro = [r["R_objective_pct"] for r in rows]
        gap = [r["delta_R_pp"] for r in rows]
        theta = [r["theta"] for r in rows if r["theta"] is not None]
        a_m_obj = [r["A_over_M_objective"] for r in rows if r["A_over_M_objective"] is not None]
        a_m_buyer = [r["A_over_M_buyer"] for r in rows if r["A_over_M_buyer"] is not None]
        cape = [r["cape"] for r in rows if r["cape"] is not None]
        buffett = [r["buffett"] for r in rows if r["buffett"] is not None]
        history.append({
            "signal_date": signal_date, "institution_count": len(rows),
            "institutions": sorted({r["institution"] for r in rows}),
            "R_buyer_median_pct": median(rb), "R_objective_median_pct": median(ro),
            "delta_R_median_pp": median(gap), "theta_median": median(theta) if theta else None,
            "A_over_M_objective_median": median(a_m_obj) if a_m_obj else None,
            "A_over_M_buyer_median": median(a_m_buyer) if a_m_buyer else None,
            "A_over_M_unit": next((r["A_over_M_unit"] for r in rows if r.get("A_over_M_unit")), None),
            "macro_capacity_matched_institutions": sum(1 for r in rows if r.get("A_over_M_objective") is not None or r.get("A_over_M_buyer") is not None),
            "negative_gravity_alert": median(rb) <= NEGATIVE_GRAVITY_THRESHOLD,
            "high_gravity_gap_alert": median(gap) >= 1.0,
            "cape": median(cape) if cape else None, "buffett": median(buffett) if buffett else None,
            "buyer_g_definition": sorted({r["buyer_g_definition"] for r in rows}),
            "objective_erp_method": sorted({r["objective_erp_method"] for r in rows}),
            "objective_g_definition": sorted({r["objective_g_definition"] for r in rows}),
            "source_rows": rows,
        })
    missing_header_fields = {}
    for name, path, fields in (("buyer", BUYER_FILE, BUYER_FIELDS), ("objective", OBJECTIVE_FILE, OBJECTIVE_FIELDS), ("market", MARKET_FILE, MARKET_FIELDS), ("macro_capacity", MACRO_CAPACITY_FILE, MACRO_CAPACITY_FIELDS)):
        ok, headers = _has_headers(path, fields)
        missing_header_fields[name] = {"file_exists": path.exists(), "valid_headers": ok,
                                       "missing_headers": [x for x in fields if x not in headers]}
    benchmark_eligible_possible_dates = 0
    for i, row in enumerate(history):
        cape_prior = sum(1 for prior in history[:i] if prior.get("cape") is not None)
        buffett_prior = sum(1 for prior in history[:i] if prior.get("buffett") is not None)
        if row.get("cape") is not None and row.get("buffett") is not None and cape_prior >= 60 and buffett_prior >= 60:
            benchmark_eligible_possible_dates += 1
    market_indexes = sorted({r["index"] for r in data["markets"] if r["index"] in {"SP500", "NASDAQ", "DOW"}})
    a_m_matched_institution_rows = sum(1 for r in matched if r.get("A_over_M_objective") is not None or r.get("A_over_M_buyer") is not None)
    formal_ready = bool(history) and bool(market_indexes) and benchmark_eligible_possible_dates > 0
    return {
        "ok": True, "module": "AEL_PRO_DOUBLE_GRAVITY_FORMAL", "data_type": "STRICT_POINT_IN_TIME_MATCHED_HISTORY",
        "formal_history_rows": len(history), "formal_matched_institution_rows": len(matched),
        "unmatched_buyer_rows": unmatched_buyer, "benchmark_eligible_possible_dates": benchmark_eligible_possible_dates,
        "A_over_M_matched_institution_rows": a_m_matched_institution_rows,
        "A_over_M_ready": a_m_matched_institution_rows > 0,
        "A_over_M_formula": "(alpha * M1 * V) / Ry*; Ry* = Rf + ERP - g; percentage-point Ry is converted to decimal before division",
        "market_indexes_available": market_indexes, "formal_backtest_ready": formal_ready,
        "formal_backtest_status": "INPUTS_PRESENT_FORWARD_COVERAGE_TO_CHECK" if formal_ready else "WAITING_FOR_VALIDATED_SOURCE_FILES_OR_COMMON_BENCHMARK_HISTORY",
        "date_tolerance_days": date_tolerance_days, "horizon_tolerance_years": horizon_tolerance_years,
        "source_file_status": missing_header_fields,
        "input_rows": {"buyer": data["buyer_counts"], "objective": data["objective_counts"], "market": data["market_counts"], "macro_capacity": data["capacity_counts"]},
        "history": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in row.items() if k != "source_rows"} | {
            "source_rows": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in source_row.items()} for source_row in row["source_rows"]]
        } for row in history],
        "warnings": [
            "仅将明确标注的买方 ERP 与 g、客观 ERP 与 g 纳入正式计算；不从预期总回报直接反推 ERP。",
            "客观观测日期不得晚于买方观测日期；信号生效日期取两边数据实际可用日期的较晚者。",
            "Theta 仅在 R_buyer > 0.25 个百分点时计算；其余情况保持空值并使用独立重力告警。",
            "没有完整数据文件时正式历史保持为空，不使用代理值补足。",
            "原始 A/M 公式保留为 (alpha*M1*V)/Ry*；缺少 alpha、M1、V 或 Ry* 非正时不输出 A/M。",
        ],
    }


def _return_metrics(signal_dates: list[date], market: list[dict[str, Any]], horizon_years: int,
                    lag_days: int, max_drawdown_pct: float = -20.0) -> dict[str, Any]:
    ordered = sorted(market, key=lambda r: r["date"])
    records = []
    for signal_date in signal_dates:
        entry_target = signal_date + timedelta(days=lag_days)
        candidates = [r for r in ordered if r["date"] >= entry_target]
        if not candidates:
            continue
        start = candidates[0]
        end_target = start["date"] + timedelta(days=round(horizon_years * 365.2425))
        end_candidates = [r for r in ordered if r["date"] >= end_target]
        if not end_candidates:
            continue
        end = end_candidates[0]
        elapsed = (end["date"] - start["date"]).days / 365.2425
        if elapsed <= 0:
            continue
        cagr = ((end["total_return_index"] / start["total_return_index"]) ** (1 / elapsed) - 1) * 100
        window = [r for r in ordered if start["date"] <= r["date"] <= min(end_target, start["date"] + timedelta(days=366))]
        peaks, max_dd = [], 0.0
        peak = None
        for r in window:
            val = r["total_return_index"]
            peak = val if peak is None else max(peak, val)
            drawdown = (val / peak - 1) * 100
            max_dd = min(max_dd, drawdown)
        records.append({"signal_date": signal_date.isoformat(), "entry_date": start["date"].isoformat(),
                        "end_date": end["date"].isoformat(), "cagr_pct": cagr,
                        "max_drawdown_first_12m_pct": max_dd, "drawdown_over_threshold": max_dd <= max_drawdown_pct})
    return _summarize_returns(records, horizon_years, max_drawdown_pct)


def _summarize_returns(records: list[dict[str, Any]], horizon_years: int, max_drawdown_pct: float = -20.0) -> dict[str, Any]:
    returns = [r["cagr_pct"] for r in records]
    drawdowns = [r["max_drawdown_first_12m_pct"] for r in records]
    n = len(records)
    return {"n": n, "horizon_years": horizon_years,
            "mean_cagr_pct": mean(returns) if returns else None,
            "median_cagr_pct": median(returns) if returns else None,
            "mean_max_drawdown_first_12m_pct": mean(drawdowns) if drawdowns else None,
            "drawdown_over_threshold_count": sum(x <= max_drawdown_pct for x in drawdowns),
            "drawdown_over_threshold_rate_pct": 100 * sum(x <= max_drawdown_pct for x in drawdowns) / n if n else None,
            "max_drawdown_threshold_pct": max_drawdown_pct,
            "observations": records,
            "sample_status": "DESCRIPTIVE_ONLY" if n < 30 else "ELIGIBLE_FOR_FURTHER_VALIDATION"}


def _expanding_threshold(rows: list[dict[str, Any]], field: str, idx: int, min_prior: int = 60,
                         quantile: float = 0.8) -> float | None:
    prior = [_float(r.get(field)) for r in rows[:idx]]
    prior = [x for x in prior if x is not None]
    if len(prior) < min_prior:
        return None
    prior.sort()
    pos = (len(prior) - 1) * quantile
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return prior[lo]
    return prior[lo] + (prior[hi] - prior[lo]) * (pos - lo)


def formal_backtest(*, index: str = "SP500", horizon_years: int = 1, lag_days: int = 30,
                    max_drawdown_pct: float = -20.0, min_prior_benchmark_rows: int = 60) -> dict[str, Any]:
    if horizon_years not in (1, 3, 5):
        return {"ok": False, "error": "horizon_years 仅支持 1、3、5 年。"}
    data = load_validated_data()
    history_payload = build_formal_history()
    history = history_payload.get("history", [])
    market = [r for r in data["markets"] if r["index"] == index.upper()]
    if not history or not market:
        return {"ok": True, "formal_backtest": False, "status": "NOT_RUN_MISSING_VALIDATED_INPUTS",
                "index": index.upper(), "horizon_years": horizon_years, "lag_days": lag_days,
                "history_rows": len(history), "market_rows": len(market),
                "required_files": {"buyer_expectations": _path_label(BUYER_FILE),
                                   "objective_expectations": _path_label(OBJECTIVE_FILE),
                                   "market_total_return": _path_label(MARKET_FILE)},
                "message": "正式回测未运行：缺少通过字段、日期、期限与来源审计的买方/客观数据及总回报指数序列。没有将代理对照伪装成正式回测。"}
    # Common evaluation dates: AEL and both baseline indicators must be present; thresholds use only earlier observations.
    benchmark_rows = []
    for i, row in enumerate(history):
        cape_thr = _expanding_threshold(history, "cape", i, min_prior_benchmark_rows, 0.8)
        buff_thr = _expanding_threshold(history, "buffett", i, min_prior_benchmark_rows, 0.8)
        try:
            d = date.fromisoformat(row["signal_date"])
        except ValueError:
            continue
        if row.get("cape") is None or row.get("buffett") is None or cape_thr is None or buff_thr is None:
            continue
        # Each method's signal is calculated only from current/past information.
        benchmark_rows.append({"signal_date": d, "ael_signal": bool(row.get("negative_gravity_alert") or row.get("high_gravity_gap_alert")),
                               "cape_signal": float(row["cape"]) >= cape_thr,
                               "buffett_signal": float(row["buffett"]) >= buff_thr})
    if not benchmark_rows:
        return {"ok": True, "formal_backtest": False, "status": "NOT_RUN_NO_COMMON_BENCHMARK_HISTORY",
                "index": index.upper(), "horizon_years": horizon_years, "lag_days": lag_days,
                "history_rows": len(history), "market_rows": len(market),
                "benchmark_eligible_dates": 0,
                "message": "买方/客观重力与指数行情虽有输入，但无法在至少 60 条历史先验观测上形成 CAPE 与巴菲特同口径基准样本；本次未报告比较胜负。"}
    # Calculate return outcomes on the common benchmark-eligible calendar.
    common_dates = [x["signal_date"] for x in benchmark_rows]
    outcomes = _return_metrics(common_dates, market, horizon_years, lag_days, max_drawdown_pct)
    if not outcomes["observations"]:
        return {"ok": True, "formal_backtest": False, "status": "NOT_RUN_NO_FORWARD_RETURN_COVERAGE",
                "index": index.upper(), "horizon_years": horizon_years, "lag_days": lag_days,
                "history_rows": len(history), "market_rows": len(market),
                "benchmark_eligible_dates": len(benchmark_rows),
                "message": "没有足够的后续总回报指数数据覆盖所选预测期限；本次未生成回测结果。"}
    outcome_by_date = {r["signal_date"]: r for r in outcomes["observations"]}
    comparisons = {}
    for signal_name in ("ael_signal", "cape_signal", "buffett_signal"):
        sig_dates, control_dates = [], []
        for row in benchmark_rows:
            if row["signal_date"].isoformat() not in outcome_by_date:
                continue
            (sig_dates if row[signal_name] else control_dates).append(row["signal_date"])
        sig = _return_metrics(sig_dates, market, horizon_years, lag_days, max_drawdown_pct)
        ctrl = _return_metrics(control_dates, market, horizon_years, lag_days, max_drawdown_pct)
        comparisons[signal_name] = {
            "signal": sig, "control": ctrl,
            "mean_cagr_difference_pp": (sig["mean_cagr_pct"] - ctrl["mean_cagr_pct"])
                if sig["mean_cagr_pct"] is not None and ctrl["mean_cagr_pct"] is not None else None,
            "same_calendar_sample": True,
            "threshold_policy": "Expanding 80th percentile using prior observations only" if signal_name != "ael_signal" else "Predeclared fixed rule: median R_buyer <= 0 or median delta_R >= 1.0 pp",
        }
    target_by_date = {r["signal_date"]: r["max_drawdown_first_12m_pct"] <= max_drawdown_pct for r in outcomes["observations"]}
    alert_metrics = {}
    for name in ("ael_signal", "cape_signal", "buffett_signal"):
        TP = FP = FN = TN = 0
        for row in benchmark_rows:
            d = row["signal_date"].isoformat()
            if d not in target_by_date:
                continue
            alert, event = bool(row[name]), bool(target_by_date[d])
            if alert and event: TP += 1
            elif alert and not event: FP += 1
            elif not alert and event: FN += 1
            else: TN += 1
        precision = TP / (TP + FP) if TP + FP else None
        recall = TP / (TP + FN) if TP + FN else None
        alert_metrics[name] = {"true_positive": TP, "false_positive": FP, "false_negative": FN, "true_negative": TN,
                               "precision_pct": precision * 100 if precision is not None else None,
                               "recall_pct": recall * 100 if recall is not None else None,
                               "target": f"Forward drawdown <= {max_drawdown_pct}% in first 12 months"}
    return {"ok": True, "formal_backtest": True, "status": "DESCRIPTIVE_SMALL_SAMPLE" if outcomes["n"] < 60 else "RUN_COMPLETE_NEEDS_OOS_REVIEW",
            "index": index.upper(), "horizon_years": horizon_years, "lag_days": lag_days,
            "common_sample_n": outcomes["n"], "common_sample_start": min(common_dates).isoformat() if common_dates else None,
            "common_sample_end": max(common_dates).isoformat() if common_dates else None,
            "return_comparison": comparisons, "drawdown_alert_metrics": alert_metrics,
            "baseline_method": "Shiller CAPE and Buffett indicator use an expanding prior-only 80th percentile; require at least 60 earlier observations.",
            "warning": "即便运行成功，样本仍需做时序外检验、重叠期限稳健性、交易延迟和修订风险审查。不得把描述性差异解释为因果关系。",
            "data_sources": {"buyer": sorted({r["buyer_source_name"] for row in history for r in row["source_rows"]}),
                             "objective": sorted({r["objective_source_name"] for row in history for r in row["source_rows"]}),
                             "cape": sorted({r["cape_source_name"] for row in history for r in row["source_rows"]}),
                             "real_yield": sorted({r["risk_free_source_name"] for row in history for r in row["source_rows"]}),
                             "objective_g": sorted({r["objective_g_source_name"] for row in history for r in row["source_rows"]}),
                             "market": sorted({r["source_name"] for r in market})}}


def formal_status() -> dict[str, Any]:
    history = build_formal_history()
    return {k: v for k, v in history.items() if k != "history"} | {
        "formal_history_rows": history.get("formal_history_rows", 0),
        "formal_backtest_ready": bool(history.get("formal_backtest_ready")),
        "data_dir": _path_label(FORMAL_DIR),
        "required_schema": {"buyer_expectations.csv": BUYER_FIELDS,
                            "objective_expectations.csv": OBJECTIVE_FIELDS,
                            "market_total_return.csv": MARKET_FIELDS,
                            "macro_capacity_inputs.csv": MACRO_CAPACITY_FIELDS},
        "message": "正式双重重力只有在买方 ERP/g、客观 ERP/g、可用日期、期限与市场总回报序列全部通过匹配后才计算。目前数据文件为空时会明确显示未就绪。",
    }
