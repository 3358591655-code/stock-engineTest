"""AEL Pro public expectation-source catalog and strict R/Theta calculations.

This module intentionally does not relabel public proxies as institutional buy-side data.
It is an auditable helper layer; production integration must preserve observation and
availability dates and use point-in-time vintages for backtests.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, Any
import math

@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str
    url: str
    fields: tuple[str, ...]
    frequency: str
    earliest_approx: str
    access: str
    role: str
    limitations: str

SOURCE_CATALOG = [
    SourceSpec('fred_dfii10','FRED DFII10 — 10-Year TIPS real yield','https://fred.stlouisfed.org/series/DFII10',('risk_free_real_pct',),'daily','2003','Public web series; API key for official API','risk_free_real','Daily real yield proxy; not a full term structure.'),
    SourceSpec('damodaran_histimpl','Damodaran historical implied ERP','https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/histimpl.html',('erp_fcfe_pct','smoothed_growth_pct','treasury_rate_pct'),'annual','1960','Public HTML/XLS download','market_implied_erp_proxy','Annual model-implied ERP, not direct buy-side survey responses; methodology can change.'),
    SourceSpec('fed_fsr_erp','Federal Reserve staff expected ERP measure','https://www.federalreserve.gov/publications/may-2021-asset-valuations.htm',('forward_earnings_yield_pct','risk_free_real_pct'),'monthly','1995-01','Public research report/chart; full reproducible downloadable series not guaranteed','market_implied_erp_proxy','ERP proxy = forward S&P earnings yield minus 10-year real Treasury yield; report sample ends 2021-02.'),
    SourceSpec('philadelphia_spf_rgdp10','Philadelphia Fed SPF 10-year real GDP growth','https://www.philadelphiafed.org/surveys-and-data/data-files',('g_real_pct',),'quarterly','2009 approx.','Public spreadsheet downloads','objective_growth_proxy','Professional forecasters are not identical to institutional equity buy-side portfolio managers.'),
    SourceSpec('philadelphia_spf_prod10','Philadelphia Fed SPF 10-year productivity growth','https://www.philadelphiafed.org/surveys-and-data/data-files',('productivity_growth_pct',),'quarterly','2009 approx.','Public spreadsheet downloads','objective_growth_proxy','Productivity growth is not the same as corporate earnings growth.'),
    SourceSpec('philadelphia_spf_stock10','Philadelphia Fed SPF 10-year stock return','https://www.philadelphiafed.org/surveys-and-data/data-files',('stock_return_pct',),'quarterly','2009 approx.','Public spreadsheet downloads','cross_check_only','Expected total stock return is not ERP unless matched risk-free return is subtracted consistently.'),
    SourceSpec('cbo_long_term','CBO long-term budget/economic projections','https://www.cbo.gov/data/budget-economic-data',('potential_gdp_growth_pct',),'annual','varies by release','Public downloadable reports/data','objective_growth_proxy','Projection horizon and variable definition vary by release.'),
    SourceSpec('fred_alfred_vintages','FRED/ALFRED vintage metadata','https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html',('observation_vintage_date',),'as released','varies by series','Public documentation/API; API key for official API','point_in_time_audit','Use to prevent revised macro data leaking into historical signals.'),
    SourceSpec('mendeley_dahlquist_ibert','Dahlquist–Ibert institutional expectations dataset','https://data.mendeley.com/',('buyer_erp_pct','buyer_g_pct','horizon_years','basis'),'dataset-dependent','dataset-dependent','Public dataset download subject to dataset terms','buyer_expectation_candidate','Only records with source, date, horizon and real/nominal basis explicitly available qualify.'),
]

def source_catalog() -> list[dict[str, Any]]:
    return [asdict(x) for x in SOURCE_CATALOG]

def _finite(x: Any) -> Optional[float]:
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def calculate_gravity(*, rf_nominal_pct: Any = None, rf_real_pct: Any = None, erp_pct: Any, growth_pct: Any,
                      basis: str, horizon_years: Any, source_role: str,
                      observation_date: str, available_date: str,
                      expected_basis: str = 'nominal', expected_horizon_years: float = 10.0,
                      max_horizon_gap_years: float = 1.0) -> dict[str, Any]:
    """Calculate R only when required inputs are explicitly matched and traceable.

    source_role must be 'buyer_observation' or 'objective_baseline'. A proxy is
    accepted only as a proxy-labelled diagnostic, never as formal buyer evidence.
    """
    # rf_nominal_pct is the canonical input. rf_real_pct remains a deprecated
    # compatibility alias only; it is never silently accepted for nominal basis.
    rf_input = rf_nominal_pct if rf_nominal_pct is not None else rf_real_pct
    rf, erp, g, h = map(_finite, (rf_input, erp_pct, growth_pct, horizon_years))
    issues=[]
    if None in (rf, erp, g, h): issues.append('required_input_missing_or_non_numeric')
    if basis != expected_basis: issues.append('real_nominal_basis_mismatch')
    if h is not None and abs(h-expected_horizon_years)>max_horizon_gap_years: issues.append('horizon_mismatch')
    if not observation_date or not available_date: issues.append('missing_observation_or_availability_date')
    if source_role not in {'buyer_observation','objective_baseline','market_implied_erp_proxy','objective_growth_proxy'}:
        issues.append('unrecognized_source_role')
    if issues:
        return {'ok':False,'status':'UNAVAILABLE','issues':issues,'R_pct':None,'source_role':source_role}
    r=rf+erp-g
    if abs(r)<0.25:
        return {'ok':False,'status':'UNAVAILABLE','issues':['denominator_or_rate_too_close_to_zero'],'R_pct':None,'source_role':source_role}
    formal = source_role in {'buyer_observation','objective_baseline'}
    return {'ok':True,'status':'FORMAL_CANDIDATE' if formal else 'PROXY_DIAGNOSTIC',
            'R_pct':round(r,6),'rf_nominal_pct':rf,'erp_pct':erp,'growth_pct':g,
            'basis':basis,'horizon_years':h,'observation_date':observation_date,
            'available_date':available_date,'source_role':source_role,
            'formal_eligible':formal,'formula':'R = nominal risk-free rate + nominal ERP - nominal growth'}

def calculate_theta(r_objective: dict[str, Any], r_actual: dict[str, Any]) -> dict[str, Any]:
    if not (r_objective.get('ok') and r_actual.get('ok')):
        return {'ok':False,'status':'UNAVAILABLE','Theta':None,'reason':'R_objective_or_R_actual_unavailable'}
    for key in ('basis','horizon_years'):
        if r_objective.get(key) != r_actual.get(key):
            return {'ok':False,'status':'UNAVAILABLE','Theta':None,'reason':f'{key}_mismatch'}
    if r_objective.get('available_date') != r_actual.get('available_date'):
        return {'ok':False,'status':'UNAVAILABLE','Theta':None,'reason':'availability_date_mismatch'}
    ro, ra = _finite(r_objective.get('R_pct')), _finite(r_actual.get('R_pct'))
    if ro is None or ra is None or abs(ra)<0.25:
        return {'ok':False,'status':'UNAVAILABLE','Theta':None,'reason':'invalid_R_values'}
    formal = bool(r_objective.get('formal_eligible') and r_actual.get('formal_eligible'))
    return {'ok':True,'status':'FORMAL_CANDIDATE' if formal else 'PROXY_DIAGNOSTIC',
            'Theta':round(ro/ra,6),'delta_R_pct_points':round(ro-ra,6),
            'formal_eligible':formal,'definition':'Theta = R_objective / R_actual'}
