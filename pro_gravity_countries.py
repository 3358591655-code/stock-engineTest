"""AEL cross-country long-run gravity research adapters.

The United States keeps the strict Shiller/FRED objective series. Other markets
use a clearly-labelled exploratory proxy because free cross-country historic
nominal EPS + total-return-index coverage is not uniform: local index return
(proxy, often excluding dividends) minus local short-rate return, combined with
a nominal-GDP growth proxy and local 10Y nominal government yield.
No missing values are filled with fabricated numbers.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / "data" / "gravity" / "country_cache"
TTL_SECONDS = int(os.getenv("AEL_GRAVITY_COUNTRY_CACHE_TTL", "21600"))
WB_API = "https://api.worldbank.org/v2/country/{code}/indicator/{indicator}"
SPF_MEDIAN_URL = "https://www.philadelphiafed.org/-/media/frbp/assets/surveys-and-data/survey-of-professional-forecasters/historical-data/medianlevel.xlsx"
SPF_INFLATION_URL = "https://www.philadelphiafed.org/-/media/frbp/assets/surveys-and-data/survey-of-professional-forecasters/historical-data/inflation.xlsx"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": "Mozilla/5.0 (compatible; AELResearch/2.6.58; +https://github.com/)"})
_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] = {}

# Latest World Bank nominal-GDP top-eight baseline plus user-requested Russia
# and South Korea as explicitly-added markets (GDP ranks change over time).
COUNTRIES: dict[str, dict[str, Any]] = {
    "USA": {"name":"美国", "iso2":"US", "wb":"USA", "currency":"USD", "symbol":"^GSPC", "rf":"DGS10", "cash":"TB3MS", "group":"us", "strict_us":True, "return_label":"Shiller 月度价格+股息总回报", "rf_url":"https://fred.stlouisfed.org/series/DGS10"},
    "CHN": {"name":"中国", "iso2":"CN", "wb":"CHN", "currency":"CNY", "symbol":"000001.SS", "rf":"IRLTLT01CNM156N", "cash":"IRSTCI01CNM156N", "group":"em", "return_label":"上证综指历史回报代理；指数分红再投资口径未统一", "rf_url":"https://yield.chinabond.com.cn/"},
    "DEU": {"name":"德国", "iso2":"DE", "wb":"DEU", "currency":"EUR", "symbol":"^GDAXI", "rf":"IRLTLT01DEM156N", "cash":"IR3TIB01DEM156N", "group":"dm_ex_us", "return_label":"DAX Performance 指数（含再投资分红的指数口径）", "rf_url":"https://fred.stlouisfed.org/series/IRLTLT01DEM156N"},
    "JPN": {"name":"日本", "iso2":"JP", "wb":"JPN", "currency":"JPY", "symbol":"^N225", "rf":"IRLTLT01JPM156N", "cash":"IR3TIB01JPM156N", "group":"dm_ex_us", "return_label":"日经225历史回报代理；指数分红再投资口径未统一", "rf_url":"https://fred.stlouisfed.org/series/IRLTLT01JPM156N"},
    "IND": {"name":"印度", "iso2":"IN", "wb":"IND", "currency":"INR", "symbol":"^BSESN", "rf":"INDIRLTLT01STM", "cash":"IRSTCI01INM156N", "group":"em", "return_label":"Sensex 历史回报代理；指数分红再投资口径未统一", "rf_url":"https://fred.stlouisfed.org/series/INDIRLTLT01STM"},
    "GBR": {"name":"英国", "iso2":"GB", "wb":"GBR", "currency":"GBP", "symbol":"^FTSE", "rf":"IRLTLT01GBM156N", "cash":"IR3TIB01GBM156N", "group":"dm_ex_us", "return_label":"FTSE 100 历史回报代理；指数分红再投资口径未统一", "rf_url":"https://fred.stlouisfed.org/series/IRLTLT01GBM156N"},
    "FRA": {"name":"法国", "iso2":"FR", "wb":"FRA", "currency":"EUR", "symbol":"^FCHI", "rf":"IRLTLT01FRM156N", "cash":"IR3TIB01FRM156N", "group":"dm_ex_us", "return_label":"CAC 40 历史回报代理；指数分红再投资口径未统一", "rf_url":"https://fred.stlouisfed.org/series/IRLTLT01FRM156N"},
    "ITA": {"name":"意大利", "iso2":"IT", "wb":"ITA", "currency":"EUR", "symbol":"FTSEMIB.MI", "rf":"IRLTLT01ITM156N", "cash":"IR3TIB01ITM156N", "group":"dm_ex_us", "return_label":"FTSE MIB 历史回报代理；指数分红再投资口径未统一", "rf_url":"https://fred.stlouisfed.org/series/IRLTLT01ITM156N"},
    "RUS": {"name":"俄罗斯", "iso2":"RU", "wb":"RUS", "currency":"RUB", "symbol":"IMOEX.ME", "rf":"IRLTLT01RUM156N", "cash":"IR3TIB01RUM156N", "group":"em", "return_label":"MOEX 指数历史回报代理；指数分红再投资口径未统一", "rf_url":"https://fred.stlouisfed.org/series/IRLTLT01RUM156N", "rf_stale_note":"OECD/FRED 俄罗斯长期利率序列仅到 2018 年，趋势止于最后可靠观测。"},
    "KOR": {"name":"韩国", "iso2":"KR", "wb":"KOR", "currency":"KRW", "symbol":"^KS11", "rf":"IRLTLT01KRM156N", "cash":"IRSTCI01KRM156N", "group":"em", "return_label":"KOSPI 历史回报代理；指数分红再投资口径未统一", "rf_url":"https://fred.stlouisfed.org/series/IRLTLT01KRM156N"},
}


def country_catalog() -> dict[str, Any]:
    rows=[]
    for code, c in COUNTRIES.items():
        rows.append({"code":code,"name":c["name"],"default":code=="USA",
                     "group":c["group"],"market_symbol":c["symbol"],"currency":c.get("currency","LCU"),
                     "objective_mode":"US_STRICT" if c.get("strict_us") else "CROSS_COUNTRY_PROXY",
                     "rf_source":c.get("rf_url"),"return_basis":c["return_label"]})
    return {"ok":True,"version":"2.6.58","default_country":"USA","count":len(rows),
            "scope_note":"名义 GDP 前八基准经济体（美国、中国、德国、日本、印度、英国、法国、意大利）+ 指定扩展俄罗斯、韩国；GDP 排名随年份变化。",
            "countries":rows}


def _finite(x: Any) -> float | None:
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _cache_read(key: str) -> Any | None:
    with _LOCK:
        hit=_CACHE.get(key)
        if hit and time.time()-hit["ts"] < TTL_SECONDS:
            return hit["value"]
    p=CACHE_DIR / (re.sub(r"[^a-zA-Z0-9_.-]","_",key)+".json")
    if p.exists():
        try:
            payload=json.loads(p.read_text(encoding="utf-8"))
            if time.time()-float(payload.get("ts",0)) < TTL_SECONDS:
                value=payload["value"]
                with _LOCK: _CACHE[key]={"ts":payload["ts"],"value":value}
                return value
        except Exception:
            pass
    return None


def _cache_write(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True,exist_ok=True)
    ts=time.time()
    with _LOCK: _CACHE[key]={"ts":ts,"value":value}
    try: (CACHE_DIR/(re.sub(r"[^a-zA-Z0-9_.-]","_",key)+".json")).write_text(json.dumps({"ts":ts,"value":value},ensure_ascii=False),encoding="utf-8")
    except Exception: pass


def _fetch_wb_gdp(code: str, force: bool=False) -> list[dict[str,Any]]:
    key=f"wb_gdp_lcu_{code}"
    if not force:
        c=_cache_read(key)
        if c is not None: return c
    url=WB_API.format(code=code,indicator="NY.GDP.MKTP.CN")
    r=_HTTP.get(url,params={"format":"json","per_page":2000,"date":"1960:2026"},timeout=(5,18))
    r.raise_for_status(); body=r.json()
    if not isinstance(body,list) or len(body)<2 or not isinstance(body[1],list): raise RuntimeError("世界银行本币名义 GDP API 返回格式异常")
    rows=[]
    for x in body[1]:
        v=_finite(x.get("value")); y=str(x.get("date") or "")
        if v is not None and v>0 and y.isdigit(): rows.append({"date":f"{y}-12-31","year":int(y),"value":v})
    rows.sort(key=lambda x:x["year"])
    if len(rows)<12: raise RuntimeError(f"世界银行名义 GDP 历史不足 12 年（{len(rows)} 个年度点）")
    _cache_write(key,rows); return rows


def _fetch_wb_indicator(country_code: str, indicator: str, force: bool=False) -> list[dict[str,Any]]:
    """Fetch real official World Bank WDI history for one indicator, local units."""
    key=f"wb_indicator_{country_code}_{indicator}"
    if not force:
        cached=_cache_read(key)
        if cached is not None:return cached
    url=WB_API.format(code=country_code,indicator=indicator)
    r=_HTTP.get(url,params={"format":"json","per_page":2000,"date":"1960:2026"},timeout=(5,18))
    r.raise_for_status();body=r.json()
    if not isinstance(body,list) or len(body)<2 or not isinstance(body[1],list):
        raise RuntimeError(f"世界银行 {indicator} 返回格式异常")
    out=[]
    for x in body[1]:
        v=_finite(x.get("value"));yr=str(x.get("date") or "")
        if v is None or not yr.isdigit():continue
        out.append({"date":f"{int(yr):04d}-12-31","year":int(yr),"value":v})
    out.sort(key=lambda x:x["year"])
    if not out:raise RuntimeError(f"世界银行 {country_code} {indicator} 没有可用历史数据")
    _cache_write(key,out);return out


def _annualize_last_observation(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Convert monthly/annual observations to one latest observation per calendar year."""
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        d = str(row.get("date") or "")[:10]
        try:
            yr = int(d[:4]); date.fromisoformat(d)
        except Exception:
            continue
        value = _finite(row.get("value"))
        if value is None or value <= 0:
            continue
        old = latest.get(yr)
        if old is None or d > str(old.get("observation_date") or old.get("date") or ""):
            latest[yr] = {"date": f"{yr}-12-31", "observation_date": d, "year": yr,
                          "value": value, "source": source}
    return [latest[y] for y in sorted(latest)]


def _fetch_m1_rows(country: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
    """Prefer OECD narrow-money series on FRED, then World Bank IMF-IFS M1 proxy.

    OECD series MANMM101{ISO2}M189N is nominal narrow money in local currency.
    If its local series is missing or stale/empty, never substitute M2: use WDI
    FM.LBL.MONY.CN only as a separately disclosed fallback.
    """
    # FRED mirrors OECD's country IDs as ISO3 (for example MANMM101USA189N).
    # Do not construct an ISO2+"M" ID: that can resolve to the wrong/empty series.
    iso3 = str(country.get("wb") or "")
    errors = []
    if iso3:
        series_id = f"MANMM101{iso3}189N"
        try:
            rows = _fetch_fred_rows(series_id, force)
            annual = _annualize_last_observation(rows, f"OECD Main Economic Indicators via FRED {series_id}")
            if annual:
                # Keep this source's last observation date in each year to reveal stale series.
                return annual
        except Exception as exc:
            errors.append(f"{series_id}: {str(exc)[:100]}")
    try:
        rows = _fetch_wb_indicator(str(country.get("wb") or ""), "FM.LBL.MONY.CN", force)
        annual = _annualize_last_observation(rows, "World Bank WDI / IMF-IFS FM.LBL.MONY.CN (narrow-money proxy)")
        if annual:
            return annual
    except Exception as exc:
        errors.append(f"WDI M1: {str(exc)[:100]}")
    raise RuntimeError("无可核验的本国M1历史序列；未用M2替代。" + ("来源错误：" + "; ".join(errors) if errors else ""))


def _make_macro_panel(nominal_rows: list[dict[str,Any]], real_rows: list[dict[str,Any]],
                      deflator_rows: list[dict[str,Any]], m1_rows: list[dict[str,Any]],
                      m2_rows: list[dict[str,Any]], *, frequency: str,
                      currency_basis: str, tax_rows: list[dict[str,Any]] | None = None,
                      tax_source: str = "World Bank WDI GC.TAX.YPKG.CN") -> dict[str,Any]:
    """Build theory variables from real public observations; missing values stay missing.

    Alpha operational proxy is income/profits/capital-gains tax revenue divided by
    nominal GDP. This WDI aggregate can include personal as well as corporate taxes,
    so it is explicitly NOT called a corporate effective tax rate. A true corporate-tax
    only series can replace it when data are consistently mapped for that country.
    """
    tax_rows = tax_rows or []
    input_sets = (nominal_rows, real_rows, deflator_rows, m1_rows, m2_rows, tax_rows)
    maps = []
    for rows in input_sets:
        maps.append({int(r["year"]): _finite(r.get("value")) for r in rows if r.get("year") is not None})
    m1_dates = {int(r["year"]): str(r.get("observation_date") or r.get("date") or "") for r in m1_rows if r.get("year") is not None}
    m1_sources = {int(r["year"]): str(r.get("source") or "本国M1公开序列") for r in m1_rows if r.get("year") is not None}
    years = sorted(set().union(*(set(x) for x in maps)))
    history = []
    for yr in years:
        ng, rg, p, m1, m2, tax = (mp.get(yr) for mp in maps)
        v_m1 = ng / m1 if ng is not None and m1 is not None and ng > 0 and m1 > 0 else None
        v_m2 = ng / m2 if ng is not None and m2 is not None and ng > 0 and m2 > 0 else None
        alpha_raw = tax / ng if tax is not None and ng is not None and tax >= 0 and ng > 0 else None
        # Taxes on income/profits/capital gains as a share of GDP can be a broad proxy,
        # but values above 50% of GDP almost certainly indicate a series/unit mismatch.
        alpha = alpha_raw if alpha_raw is not None and 0.0 <= alpha_raw <= 0.50 else None
        alpha_issue = ("税收/GDP 比例超出 0–50% 合理校验范围，疑似数据/单位不匹配；α和分子已停算。"
                       if alpha_raw is not None and not (0.0 <= alpha_raw <= 0.50) else None)
        # User's macro-Fisher analogy uses V=nominal GDP/M2 for these valuation identities.
        q_over_v = rg / v_m2 if rg is not None and v_m2 is not None and v_m2 > 0 else None
        m2_over_p = m2 / (p / 100.0) if m2 is not None and p is not None and p > 0 else None
        one_over_p = 100.0 / p if p is not None and p > 0 else None
        q_over_m2v = rg / (m2 * v_m2) if rg is not None and m2 is not None and v_m2 is not None and m2 > 0 and v_m2 > 0 else None
        row = {"date":f"{yr}-12-31", "year":yr, "nominal_gdp_lcu":ng, "real_gdp_lcu":rg,
               "gdp_deflator_index":p, "M1":m1, "m2_broad_lcu":m2, "alpha_tax_revenue_lcu":tax,
               "alpha_proxy":alpha, "alpha_raw_ratio":alpha_raw, "alpha_issue":alpha_issue,
               "alpha_source":tax_source if alpha is not None else None,
               "M1_observation_date":m1_dates.get(yr), "M1_source":m1_sources.get(yr),
               "V_M1":v_m1, "v_m2_proxy":v_m2,
               "alpha_M1_V":alpha * m1 * v_m1 if alpha is not None and m1 is not None and v_m1 is not None else None,
               "Q_over_V":q_over_v, "M2_over_P":m2_over_p, "one_over_P":one_over_p, "Q_over_M2V":q_over_m2v,
               "source_basis":currency_basis, "frequency":frequency}
        history.append(row)

    def last_nonnull(key):
        return next((r for r in reversed(history) if r.get(key) is not None), None)
    definitions = [
        ("nominal_gdp_lcu", "名义GDP（本币现价）"), ("real_gdp_lcu", "实际GDP（本币不变价）"),
        ("gdp_deflator_index", "GDP平减指数"), ("M1", "M1窄口径货币（本币）"),
        ("m2_broad_lcu", "M2/广义货币（本币）"), ("V_M1", "名义GDP/M1 流通速度代理"),
        ("v_m2_proxy", "名义GDP/M2 流通速度代理"), ("alpha_tax_revenue_lcu", "所得/利润/资本利得税收入（本币）"),
        ("alpha_proxy", "α税收强度代理（所得/利润/资本利得税收入÷名义GDP）"),
        ("alpha_M1_V", "α×M1×V 分子（代理）"), ("Q_over_V", "Q/V 整体估值代理"),
        ("M2_over_P", "M2/P 整体估值代理"), ("one_over_P", "1/P 普通股价格代理"),
        ("Q_over_M2V", "Q/(M2V) 普通股价格代理")]
    fields = {}
    for key, label in definitions:
        r = last_nonnull(key)
        fields[key] = {"label":label,"available":r is not None,"date":(r.get("M1_observation_date") if key == "M1" and r else r.get("date") if r else None),
                       "value":r.get(key) if r else None}
        if key == "M1" and r:
            fields[key]["source"] = r.get("M1_source")
        if key in {"alpha_proxy", "alpha_M1_V"} and r:
            fields[key]["definition"] = "广义税收代理；并非企业实际有效税率，税收口径可能包括个人所得税。"
            fields[key]["source"] = r.get("alpha_source")
    alpha_row = last_nonnull("alpha_proxy")
    alpha_problem = next((r.get("alpha_issue") for r in reversed(history) if r.get("alpha_issue")), None)
    fields["alpha_proxy"]["reason"] = ("当前公开通用序列是所得/利润/资本利得税收入占名义GDP代理，可能包括个人所得税，不能解释为企业有效税率。"
         if fields["alpha_proxy"]["available"] else (alpha_problem or "该国没有可核验的税收历史，或税收/GDP 比例未通过合理性校验；α与分子不计算。"))
    fields["alpha_M1_V"]["reason"] = ("使用通过范围校验的税收收入/GDP代理计算；这是研究性宏观代理，并非直接观测的企业抽成参数。"
         if fields["alpha_M1_V"]["available"] else (alpha_problem or "α、M1或V任一关键输入缺失或未通过合理性校验，不用其他变量替代。"))
    latest = next((r for r in reversed(history) if r.get("nominal_gdp_lcu") is not None), None)
    return {"history":history,"latest":latest,"fields":fields,"frequency":frequency,"currency_basis":currency_basis,
            "warning":"实际GDP采用 WDI NY.GDP.MKTP.KN（本币不变价），避免误把 constant USD 当成本币。M1优先采用OECD/FRED窄口径序列（使用三位国家代码），缺失时退回WDI/IMF-IFS；M2独立显示，不冒充M1。V=名义GDP/M1或/M2为宏观速度代理，不等于股票换手率。α使用所得/利润/资本利得税收入÷GDP作为跨国公开代理，可能包括个人所得税；超过GDP的50%会被判为疑似单位错配并停止计算。数据发布日期与GDP发布滞后有差异，不能视为无前视偏差回测。"}

def _fetch_fred_rows(series_id: str, force: bool=False) -> list[dict[str,Any]]:
    if not series_id: return []
    key=f"fred_country_{series_id}"
    if not force:
        c=_cache_read(key)
        if c is not None: return c
    r=_HTTP.get(FRED_CSV,params={"id":series_id},timeout=(5,18)); r.raise_for_status()
    import csv,io
    reader=csv.DictReader(io.StringIO(r.content.decode("utf-8-sig",errors="replace")))
    rows=[]
    for raw in reader:
        dt=str(raw.get("observation_date") or raw.get("DATE") or raw.get("date") or "")[:10]
        val_col=next((k for k in raw if k and k.lower() not in {"observation_date","date"}),None)
        v=_finite(raw.get(val_col)) if val_col else None
        try: date.fromisoformat(dt)
        except ValueError: continue
        if v is not None: rows.append({"date":dt,"value":v})
    if not rows: raise RuntimeError(f"FRED/OECD {series_id} 返回空序列")
    _cache_write(key,rows); return rows


def _fetch_yahoo_monthly(symbol: str, force: bool=False) -> list[dict[str,Any]]:
    key=f"yahoo_monthly_{symbol}"
    if not force:
        c=_cache_read(key)
        if c is not None: return c
    url=YAHOO_CHART.format(symbol=requests.utils.quote(symbol,safe=""))
    r=_HTTP.get(url,params={"range":"max","interval":"1mo","events":"div,splits"},timeout=(5,20)); r.raise_for_status()
    payload=r.json().get("chart",{}); err=payload.get("error")
    results=payload.get("result") or []
    if err or not results: raise RuntimeError(f"Yahoo Finance 指数 {symbol} 无数据：{str(err or 'empty')[:120]}")
    item=results[0]; stamps=item.get("timestamp") or []
    quote=((item.get("indicators") or {}).get("quote") or [{}])[0]
    close=quote.get("close") or []
    adj=((item.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or close
    rows=[]
    for i,ts in enumerate(stamps):
        if i>=len(close): break
        v=_finite(adj[i]) if i<len(adj) else None
        if v is None: v=_finite(close[i])
        if v is None or v<=0: continue
        dt=datetime.fromtimestamp(ts,timezone.utc).date().replace(day=1).isoformat()
        rows.append({"date":dt,"value":v,"close":_finite(close[i])})
    # Yahoo can emit duplicate observations around changes; preserve the last per month.
    bydate={r["date"]:r for r in rows}; rows=[bydate[d] for d in sorted(bydate)]
    if len(rows)<121: raise RuntimeError(f"{symbol} 指数月度历史不足 10 年（{len(rows)} 个点）")
    _cache_write(key,rows); return rows


def _latest_le(rows: list[dict[str,Any]], dt: str, max_gap_days: int=550) -> dict[str,Any] | None:
    out=None
    for r in rows:
        if r.get("date","")[:10] <= dt: out=r
        else: break
    if out is None: return None
    try:
        if (date.fromisoformat(dt[:10])-date.fromisoformat(out["date"][:10])).days > max_gap_days: return None
    except Exception: return None
    return out


def _gdp_cagr(gdp_rows: list[dict[str,Any]], end_year: int, years: int=10) -> float | None:
    byyear={int(x["year"]):_finite(x.get("value")) for x in gdp_rows}
    end=byyear.get(end_year); start=byyear.get(end_year-years)
    if end is None or start is None or start<=0 or end<=0: return None
    return (end/start)**(1.0/years)*100.0-100.0


def _annual_equity_cagr(index_rows: list[dict[str,Any]], end_date: str, years: float=10.0) -> tuple[float|None,dict[str,Any]|None]:
    target=date.fromisoformat(end_date[:10]); candidates=[]
    for x in index_rows:
        try: d=date.fromisoformat(x["date"])
        except Exception: continue
        months=abs((target.year-d.year)*12+(target.month-d.month))
        if x.get("value") and 118<=months<=122: candidates.append((abs(months-120),d,x))
    if not candidates: return None,None
    _,d0,base=min(candidates,key=lambda v:v[0])
    cur=_latest_le(index_rows,end_date, max_gap_days=62)
    if not cur or _finite(base.get("value")) is None: return None,None
    months=(target.year-d0.year)*12+(target.month-d0.month)
    if months<118 or months>122: return None,None
    return ((float(cur["value"])/float(base["value"]))**(12.0/months)-1.0)*100.0,base


def _cash_cagr(cash_rows: list[dict[str,Any]], start_date: str, end_date: str) -> float|None:
    start=date.fromisoformat(start_date[:10]); end=date.fromisoformat(end_date[:10])
    selected=[r for r in cash_rows if start < date.fromisoformat(r["date"][:10]) <= end]
    if len(selected)<30: return None
    # Monthly/annualized quote rates compounded at monthly frequency with limited carry-forward.
    factor=1.0; n=0; cursor=start; last=None
    while cursor<end:
        cursor=(cursor.replace(day=1)+timedelta(days=32)).replace(day=1)
        if cursor>end: break
        hit=_latest_le(cash_rows,cursor.isoformat(),max_gap_days=100)
        if hit is None: return None
        rate=_finite(hit.get("value"))
        if rate is None or rate<=-100: return None
        factor *= (1.0+rate/100.0)**(1.0/12.0); n+=1; last=hit
    years=(end-start).days/365.2425
    return (factor**(1.0/years)-1.0)*100.0 if n>=110 and years>=9.5 else None



def _download_spf_workbook(url: str, cache_key: str, force: bool=False) -> bytes:
    """Download official Philadelphia Fed SPF workbook with bounded timeouts."""
    # Store parsed values in JSON caches; workbook bytes are fetched only on cache miss.
    if not force:
        cached=_cache_read(cache_key)
        if isinstance(cached,dict) and isinstance(cached.get("base64"),str):
            import base64
            return base64.b64decode(cached["base64"])
    r=_HTTP.get(url,timeout=(6,25))
    r.raise_for_status()
    blob=r.content
    if len(blob)<2000 or not (blob[:2]==b"PK" or blob[:4]==b"\xd0\xcf\x11\xe0"):
        raise RuntimeError(f"费城联储 SPF 文件不是可识别的 Excel 文件（{len(blob)} bytes）")
    import base64
    # Use a separate short TTL for workbooks; file size is bounded.
    _cache_write(cache_key,{"base64":base64.b64encode(blob).decode("ascii")})
    return blob


def _resolve_spf_median_workbook_url(variable: str) -> str:
    """Resolve the official variable page's current Median Responses XLSX link.

    Philadelphia Fed sometimes changes file layouts or filenames; discovering the
    link from the official page is safer than inventing a guessed workbook URL.
    """
    from html.parser import HTMLParser
    from urllib.parse import urljoin
    page_map={"STOCK10":"stock10","BOND10":"bond10","RGDP10":"rgdp10","CPI10":"cpi10"}
    slug=page_map.get(variable.upper())
    if not slug: raise RuntimeError(f"未知 SPF 变量：{variable}")
    page_url=f"https://www.philadelphiafed.org/surveys-and-data/{slug}"
    r=_HTTP.get(page_url,timeout=(6,18));r.raise_for_status()
    class Anchors(HTMLParser):
        def __init__(self): super().__init__();self.rows=[];self.href=None;self.text=[]
        def handle_starttag(self,tag,attrs):
            if tag.lower()=="a": self.href=dict(attrs).get("href");self.text=[]
        def handle_data(self,data):
            if self.href is not None:self.text.append(data)
        def handle_endtag(self,tag):
            if tag.lower()=="a" and self.href is not None:
                self.rows.append((self.href," ".join(self.text)));self.href=None;self.text=[]
    parser=Anchors();parser.feed(r.text)
    candidates=[]
    for href,label in parser.rows:
        full=urljoin(page_url,href); low=(full+" "+label).lower()
        is_xlsx=bool(re.search(r"\.xlsx?(?:\?|$)",full,re.I))
        if not is_xlsx or not full.startswith("https://www.philadelphiafed.org/"):continue
        median="median" in low
        if median:candidates.append((0 if "median" in label.lower() else 1,full))
    if not candidates:
        raise RuntimeError(f"SPF {variable} 官方页面未发现可识别的 Median XLSX 下载链接")
    return sorted(candidates,key=lambda x:x[0])[0][1]


def _parse_spf_annual_series(blob: bytes, sheet_hint: str, value_hint: str | None=None,
                             fallback_last_value: bool=False) -> list[dict[str,Any]]:
    """Parse one long-horizon SPF worksheet/table while tolerating official workbook layouts."""
    try:
        import io
        import pandas as pd
        book=pd.read_excel(io.BytesIO(blob),sheet_name=None,header=None)
    except Exception as exc:
        raise RuntimeError(f"SPF Excel 解析失败：{str(exc)[:150]}") from exc
    norm=lambda x:re.sub(r"[^a-z0-9]","",str(x).lower())
    wanted=norm(sheet_hint);vh=norm(value_hint or "")
    choices=[]
    for sheet,frame in book.items():
        ns=norm(sheet)
        name_match=bool(wanted and (wanted==ns or wanted in ns or ns in wanted))
        contains_variable=False
        if value_hint and frame is not None:
            for i in range(min(15,len(frame))):
                cells=[norm(v) for v in frame.iloc[i].tolist()]
                if vh in cells:
                    contains_variable=True;break
        if name_match or contains_variable:
            choices.append((0 if contains_variable else 1,sheet,frame))
    if not choices and fallback_last_value and book:
        choices=[(2,*next(iter(book.items())))]
    if not choices:
        raise RuntimeError(f"SPF 文件中找不到 {sheet_hint}/{value_hint} 的表或列；可用工作表：{', '.join(map(str,list(book)[:10]))}")
    choices.sort(key=lambda x:x[0]);sheet,raw=choices[0][1],choices[0][2]
    if raw is None or raw.empty: raise RuntimeError(f"SPF {sheet_hint} 工作表为空")
    header_idx=None
    # Some official workbooks put an explanatory title above the table.
    for i in range(min(20,len(raw))):
        vals=[norm(v) for v in raw.iloc[i].tolist()]
        has_year=any(v in {"year","surveyyear","yr"} or v.endswith("year") for v in vals)
        has_q=any(v in {"quarter","qtr","surveyquarter","q"} or v.endswith("quarter") for v in vals)
        if has_year and has_q:
            header_idx=i;break
    if header_idx is None:
        # Recognize a single survey-period column such as 1992:Q1 / 1992Q1.
        for i in range(min(20,len(raw))):
            vals=[norm(v) for v in raw.iloc[i].tolist()]
            if any(v in {"date","surveydate","period","surveyperiod","quarter"} for v in vals):
                header_idx=i;break
    if header_idx is None: raise RuntimeError(f"SPF {sheet_hint} 缺少可识别的调查年份/季度表头")
    headers=[]
    for j,v in enumerate(raw.iloc[header_idx].tolist()):
        name=str(v).strip() if v is not None and str(v).strip() and str(v).lower()!="nan" else f"col_{j}"
        headers.append(name)
    df=raw.iloc[header_idx+1:].copy();df.columns=headers
    cols=[str(c) for c in df.columns];normcols={norm(c):c for c in cols}
    year_col=next((normcols[k] for k in ("year","surveyyear","yr") if k in normcols),None)
    quarter_col=next((normcols[k] for k in ("quarter","qtr","surveyquarter","q") if k in normcols),None)
    period_col=next((normcols[k] for k in ("date","surveydate","period","surveyperiod") if k in normcols),None)
    value_col=None
    if value_hint:
        value_col=next((c for c in cols if norm(value_hint)==norm(c)),None)
    if value_col is None:
        # Long-horizon pages may deliver a variable-specific workbook with a plain
        # 'Median' column rather than the variable code in the header.
        median_cols=[c for c in cols if norm(c) in {"median","medianforecast","medianvalue","forecastmedian"}]
        if median_cols:value_col=median_cols[-1]
    if value_col is None:
        excluded={norm(x) for x in (year_col,quarter_col,period_col) if x}
        candidates=[c for c in cols if norm(c) not in excluded and norm(c) not in {"date","surveydate","variable","description","units"}]
        if not candidates: raise RuntimeError(f"SPF {sheet_hint} 无数值预测列")
        # A matched variable label should beat arbitrary columns. For the inflation
        # workbook the official documentation specifies the 10-year CPI series last.
        value_col=candidates[-1] if fallback_last_value else candidates[0]
    def period_to_year_quarter(raw_period):
        if hasattr(raw_period,"to_pydatetime"):
            raw_period=raw_period.to_pydatetime()
        if isinstance(raw_period,(datetime,date)):
            return raw_period.year,((raw_period.month-1)//3)+1
        txt=str(raw_period).strip().upper()
        m=re.search(r"(\d{4})\s*[:\-/ ]?\s*Q([1-4])",txt)
        if m:return int(m.group(1)),int(m.group(2))
        m=re.search(r"(\d{4})Q([1-4])",txt)
        if m:return int(m.group(1)),int(m.group(2))
        return None,None
    rows=[]
    for _,row in df.iterrows():
        if year_col and quarter_col:
            yy=_finite(row.get(year_col));qq=_finite(row.get(quarter_col))
            year=int(yy) if yy is not None else None;quarter=int(qq) if qq is not None else None
        elif period_col:
            year,quarter=period_to_year_quarter(row.get(period_col))
        else:
            year=quarter=None
        v=_finite(row.get(value_col))
        if year is None or quarter is None or v is None:continue
        if not (1968<=year<=2100 and 1<=quarter<=4):continue
        # Long-term STOCK10/BOND10/RGDP10 forecasts are reported on first-quarter surveys.
        if quarter!=1:continue
        dt=f"{year:04d}-03-01"
        rows.append({"year":year,"quarter":quarter,"date":dt,"value":v,"survey_date_proxy":dt,"source_sheet":str(sheet),"source_column":str(value_col)})
    by_date={r["date"]:r for r in rows};rows=[by_date[d] for d in sorted(by_date)]
    if not rows:raise RuntimeError(f"SPF {sheet_hint} 没有有效的第一季度长期预测记录（表 {sheet}，列 {value_col}）")
    return rows


def _build_spf_buyer_history(stock_rows: list[dict[str,Any]], bond_rows: list[dict[str,Any]],
                             rgdp_rows: list[dict[str,Any]], cpi_rows: list[dict[str,Any]]) -> list[dict[str,Any]]:
    """Build transparent USA professional-forecaster Rbuyer from official SPF 10-year forecasts.

    ERP proxy = SPF expected 10-year S&P500 annualized return - SPF expected 10-year
    Treasury yield. g proxy = compounded nominal-GDP growth = (1+real RGDP10)*(1+CPI10)-1.
    This is a professional-forecaster consensus proxy, not a country-specific private fund survey
    and not a direct forecast of corporate EPS growth.
    """
    maps={name:{r["date"]:r for r in rows} for name,rows in (("stock",stock_rows),("bond",bond_rows),("rgdp",rgdp_rows),("cpi",cpi_rows))}
    dates=sorted(set(maps["stock"]) & set(maps["bond"]) & set(maps["rgdp"]) & set(maps["cpi"]))
    out=[]
    for dt in dates:
        stock=_finite(maps["stock"][dt].get("value"));bond=_finite(maps["bond"][dt].get("value"))
        rgdp=_finite(maps["rgdp"][dt].get("value"));cpi=_finite(maps["cpi"][dt].get("value"))
        if None in (stock,bond,rgdp,cpi): continue
        g=((1.0+rgdp/100.0)*(1.0+cpi/100.0)-1.0)*100.0
        erp=stock-bond
        rval=bond+erp-g
        if not all(math.isfinite(v) for v in (g,erp,rval)): continue
        out.append({"date":dt,"available_date_proxy":dt,"value":round(rval,5) if rval>0 else None,
                    "R_buyer":round(rval,5),"raw_formula_result_pct":round(rval,5),
                    "rf_nominal_pct":round(bond,5),"erp_buyer_pct":round(erp,5),
                    "expected_stock_return_10y_pct":round(stock,5),"expected_10y_bond_yield_pct":round(bond,5),
                    "g_buyer_pct":round(g,5),"g_buyer_method":"(1 + SPF RGDP10) × (1 + SPF CPI10) - 1; nominal GDP growth proxy, not corporate EPS growth",
                    "real_gdp_growth_expectation_pct":round(rgdp,5),"cpi_inflation_expectation_pct":round(cpi,5),
                    "horizon_years":10,"asset_group":"us","source":"Philadelphia Fed Survey of Professional Forecasters (median long-term forecasts)",
                    "source_url":"https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts",
                    "basis":"nominal","quality":"US_PROFESSIONAL_FORECASTER_PROXY" if rval>0 else "OUT_OF_DOMAIN_NONPOSITIVE_R",
                    "valid_for_gravity":rval>0,"note":"R买方专业预测者代理；股票回报、名义国债收益率、实际GDP增长和CPI通胀来自同一SPF长期预测框架。"})
    return out


def _fetch_spf_buyer_history(force: bool=False) -> list[dict[str,Any]]:
    if not force:
        cached=_cache_read("spf_buyer_history_parsed")
        if isinstance(cached,list): return cached
    try:
        median_blob=_download_spf_workbook(SPF_MEDIAN_URL,"spf_medianlevel_bytes",force=force)
    except Exception as exc:
        median_blob=None; median_error=str(exc)
    series={}
    variable_pages={"STOCK10":"stock10","BOND10":"bond10","RGDP10":"rgdp10"}
    def get_series(variable: str, sheet_hint: str, value_hint: str, fallback_last: bool=False):
        errors=[]
        if median_blob is not None:
            try:return _parse_spf_annual_series(median_blob,sheet_hint,value_hint,fallback_last_value=fallback_last)
            except Exception as exc:errors.append(f"综合历史工作簿：{str(exc)[:100]}")
        if variable in variable_pages:
            try:
                url=_resolve_spf_median_workbook_url(variable)
                blob=_download_spf_workbook(url,f"spf_{variable.lower()}_median_bytes",force=force)
                return _parse_spf_annual_series(blob,variable,value_hint,fallback_last_value=True)
            except Exception as exc:errors.append(f"变量专属官方文件：{str(exc)[:130]}")
        raise RuntimeError("；".join(errors) or f"没有找到 SPF {variable} 数据")
    stocks=get_series("STOCK10","STOCK10","STOCK10")
    bonds=get_series("BOND10","BOND10","BOND10")
    rgdp=get_series("RGDP10","RGDP10","RGDP10")
    inflation_blob=_download_spf_workbook(SPF_INFLATION_URL,"spf_inflation_bytes",force=force)
    try:
        cpi=_parse_spf_annual_series(inflation_blob,"INFLATION","INFCPI10YR",fallback_last_value=True)
    except Exception:
        # The dedicated official CPI10 data page is a second parser/source path.
        url=_resolve_spf_median_workbook_url("CPI10")
        cpi_blob=_download_spf_workbook(url,"spf_cpi10_median_bytes",force=force)
        cpi=_parse_spf_annual_series(cpi_blob,"CPI10","INFCPI10YR",fallback_last_value=True)
    result=_build_spf_buyer_history(stocks,bonds,rgdp,cpi)
    if len(result)<10:
        raise RuntimeError(f"SPF 多字段共同历史仅 {len(result)} 点，少于10年，不作为长期买方趋势")
    _cache_write("spf_buyer_history_parsed",result)
    return result


def _buyer_history(country: dict[str,Any], rf_rows: list[dict[str,Any]], records: list[dict[str,Any]], limit: int) -> tuple[list[dict[str,Any]],str]:
    """Only build country/region R_buyer where same-row ERP/g are nominal and 10y."""
    group=country.get("group","us")
    if group=="us": scope_label="美国分组的机构/资产管理人预期"
    elif group=="dm_ex_us": scope_label="发达海外市场（ex-US）区域预期代理，不是单一国家调查"
    else: scope_label="新兴市场区域预期代理，不是单一国家调查"
    out=[]; skipped_basis=0; skipped_growth=0; skipped_rf=0
    for row in records or []:
        asset_group=row.get("asset_group") or "us"
        if asset_group!=group: continue
        try: dt=date.fromisoformat(str(row.get("date",""))[:10])
        except Exception: continue
        h=_finite(row.get("horizon_years")); erp=_finite(row.get("buyer_erp_pct")); g=_finite(row.get("buyer_g_pct"))
        if h is None or abs(h-10.0)>1.0: continue
        if row.get("erp_basis")!="nominal" or row.get("growth_basis")!="nominal": skipped_basis+=1; continue
        if erp is None or g is None: skipped_growth+=1; continue
        rf=_latest_le(rf_rows,dt.isoformat(),max_gap_days=45)
        if rf is None or _finite(rf.get("value")) is None: skipped_rf+=1; continue
        available=(dt+timedelta(days=30)).isoformat()
        raw_r=float(rf["value"])+erp-g
        if raw_r<=0: 
            skipped_growth+=1
            continue
        out.append({"date":dt.isoformat(),"available_date_proxy":available,"value":raw_r,
                    "R_buyer":raw_r,"rf_nominal_pct":float(rf["value"]),
                    "rf_date":rf["date"],"erp_buyer_pct":erp,"g_buyer_pct":g,"horizon_years":h,
                    "institution":row.get("institution"),"asset_group":asset_group,"scope_label":scope_label,
                    "source":row.get("source_file","Dahlquist-Ibert institutional return expectations"),
                    "basis":"nominal","quality":"INSTITUTIONAL_MATCHED"})
    out.sort(key=lambda x:x["date"])
    if out: return out[-limit:],f"合格 R买方记录 {len(out)} 条；范围为{scope_label}，按同口径、约10年期限和本国名义Rf匹配，不应解读为逐国机构调查。"
    if skipped_growth: note=f"机构记录存在，但 {skipped_growth} 条缺少同口径前瞻 g；不补造。"
    elif skipped_basis: note=f"机构候选记录存在，但 {skipped_basis} 条回报口径不匹配名义口径。"
    elif skipped_rf: note=f"机构 ERP/g 候选存在，但 {skipped_rf} 条日期附近找不到合格名义无风险利率。"
    else: note="当前公开机构档案未提供该市场分组足够的同日期、名义口径、约10年 ERP 与前瞻 g；R买方趋势不造数。"
    return [],note


def country_gravity_history(country_code: str="USA", limit: int=600, force: bool=False) -> dict[str,Any]:
    code=str(country_code or "USA").upper()
    if code not in COUNTRIES: code="USA"
    c=COUNTRIES[code]; limit=max(12,min(1200,int(limit)))
    cache_key=f"history_v2659_{code}"
    if not force:
        cached=_cache_read(cache_key)
        if cached is not None: return cached
    if c.get("strict_us"):
        # USA uses the strict Shiller/FRED objective definition; buyer trend prefers
        # matched institution rows, with official SPF professional-forecaster data
        # kept as a clearly labelled reference proxy rather than mislabeled as buy-side.
        from pro_gravity_live import automatic_dashboard, _series_csv, _get_buyer_status
        dash=automatic_dashboard(index="SP500",force=force)
        if not dash.get("ok"):
            return {"ok":False,"country":code,"name":c["name"],"status":"SOURCE_ERROR","message":dash.get("message"),"error":dash.get("error")}
        series=dash.get("series") or {}; metrics=dash.get("metrics") or {}
        objective_raw_source=(series.get("R_objective") or [])[-limit:]
        objective=[]; invalid=[]; objective_raw=[]
        for row in objective_raw_source:
            item=dict(row)
            raw=_finite(row.get("raw_formula_result_pct"))
            if raw is None: raw=_finite(row.get("R_objective"))
            if raw is None: raw=_finite(row.get("value"))
            if raw is None: continue
            item["raw_formula_result_pct"]=round(raw,6)
            item["valid_for_gravity"]=raw>0
            item["value"]=round(raw,6) if raw>0 else None
            objective_raw.append(item)
            if raw>0: objective.append(item)
            else:
                invalid.append({"date":item.get("date"),"raw_formula_result_pct":round(raw,6),
                                "Rf_nominal_pct":item.get("rf_nominal_pct"),
                                "ERP_proxy_pct":item.get("erp_realized_10y_pct"),
                                "g_nominal_GDP_proxy_pct":item.get("g_nominal_earnings_10y_pct"),
                                "reason":"公式原始结果非正，按模型适用域不作为有效 R 重力点；保留用于诊断，不截断为 0。"})
        try: rf_rows=_series_csv(c["rf"],force)
        except Exception: rf_rows=[]
        buyer_state=_get_buyer_status(start_if_missing=True,force_retry=force)
        buyer,buyer_note=_buyer_history(c,rf_rows,buyer_state.get("records") or [],limit)
        buyer_reference=[]; buyer_reference_note=""
        buyer_mode="MATCHED_INSTITUTIONAL"
        if not buyer:
            try:
                buyer_reference=_fetch_spf_buyer_history(force=force)[-limit:]
                buyer_mode="SPF_PROFESSIONAL_FORECASTER_PROXY"
                buyer_reference_note="费城联储 Survey of Professional Forecasters：STOCK10、BOND10、RGDP10、CPI10 年度第一季度调查记录。它是专业预测者长期共识代理，不是私人基金/资产管理人逐家买方调查；g 使用名义 GDP 增长代理，不是公司 EPS 增长。"
                buyer_note="没有足够的合格机构 ERP/g 匹配历史；另提供官方 SPF 专业预测者参考曲线，已明确标注为代理，不能冒充机构买方实测。"
            except Exception as exc:
                buyer_reference_note=f"费城联储 SPF 参考线下载/解析失败：{str(exc)[:160]}"
                buyer_note=buyer_note or buyer_reference_note
        if buyer:
            buyer_mode="MATCHED_INSTITUTIONAL"
        source_rows=[s for s in (dash.get("sources") or []) if "DGS10" in str(s.get("name")) or "Shiller" in str(s.get("name")) or "TB3MS" in str(s.get("name")) or "Dahlquist" in str(s.get("name"))]
        source_rows.append({"name":"费城联储 SPF 长期专业预测者数据（参考代理）","url":"https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts","available":bool(buyer_reference),"count":len(buyer_reference),"last_observation":buyer_reference[-1].get("date") if buyer_reference else None,"definition":"STOCK10、BOND10、RGDP10、CPI10；不是私人买方/基金经理预期"})
        macro_panel=None
        try:
            from pro_gravity_live import charter_theory_history
            macro_dash=charter_theory_history(force=force,limit=min(600,limit))
            if macro_dash.get("ok"):
                numerator_series=(series.get("alpha_M1_V") or [])
                num_map={str(x.get("date"))[:10]:x for x in numerator_series}
                mh=[]
                for x in macro_dash.get("series") or []:
                    nrow=num_map.get(str(x.get("date"))[:10])
                    q = _finite(x.get("Q")); m2v = _finite(x.get("M2")); pidx = _finite(x.get("P_deflator_index")); vel = _finite(x.get("V"))
                    qv = q / vel if q is not None and vel is not None and vel > 0 else None
                    m2p = m2v / (pidx / 100.0) if m2v is not None and pidx is not None and pidx > 0 else None
                    mh.append({"date":x.get("date"),"nominal_gdp_lcu":x.get("GDP_nominal"),"real_gdp_lcu":q,
                               "gdp_deflator_index":pidx,"m2_broad_lcu":m2v,"v_m2_proxy":vel,
                               "alpha_proxy":nrow.get("alpha_proxy") if nrow else None,"M1":nrow.get("M1") if nrow else None,
                               "V_M1":nrow.get("V") if nrow else None,"alpha_M1_V":nrow.get("value") if nrow else None,
                               "alpha_source":"FRED CPATAX / 名义 GDP（美国操作性代理）" if nrow else None,
                               "Q_over_V":qv,"M2_over_P":m2p,"one_over_P":(100.0/pidx if pidx is not None and pidx > 0 else None),
                               "Q_over_M2V":(q/(m2v*vel) if q is not None and m2v is not None and vel is not None and m2v > 0 and vel > 0 else None),
                               "identity_gap_pct":((qv/m2p-1.0)*100.0 if qv is not None and m2p is not None and m2p != 0 else None),
                               "frequency":"季度","source_basis":"USD billions / FRED"})
                latest_macro=mh[-1] if mh else {}
                macro_panel={"history":mh,"latest":latest_macro,"frequency":"季度","currency_basis":"USD billions / FRED",
                             "fields":{key:{"label":label,"available":latest_macro.get(key) is not None,"value":latest_macro.get(key),"date":latest_macro.get("date")} for key,label in (
                                       ("alpha_proxy","α=CPATAX/名义 GDP 代理（非企业税专属）"),("M1","FRED M1SL 窄口径货币存量"),
                                       ("V_M1","FRED M1V 货币流通速度"),("alpha_M1_V","α×M1×V 分子代理"),
                                       ("nominal_gdp_lcu","名义GDP"),("real_gdp_lcu","实际GDP"),("gdp_deflator_index","GDP平减指数"),
                                       ("m2_broad_lcu","M2广义货币"),("v_m2_proxy","名义 GDP / M2 速度代理"),
                                       ("Q_over_V","Q/V 估值代理"),("M2_over_P","M2/P 估值代理"),("one_over_P","1/P 价格代理"),("Q_over_M2V","Q/(M2V) 价格代理"))},
                             "warning":macro_dash.get("warning")}
        except Exception as exc:
            macro_panel={"history":[],"latest":None,"fields":{},"warning":f"美国 FRED 理论变量历史暂不可用：{str(exc)[:140]}"}
        result={"ok":True,"country":code,"name":c["name"],"status":"READY" if objective else "PARTIAL_DATA",
                "as_of":metrics.get("as_of"),"objective_mode":"US_STRICT","objective_label":"R客观预期重力（美国严格历史口径）",
                "objective_count":len(objective),"objective_raw_count":len(objective_raw),"out_of_domain_count":len(invalid),
                "buyer_count":len(buyer),"buyer_reference_count":len(buyer_reference),
                "buyer_scope_label":"美国分组机构/资产管理人预期" if buyer else "美国 SPF 专业预测者代理",
                "R_objective":objective,"R_objective_raw":objective_raw,"invalid_objective":invalid,
                "R_buyer":buyer,"R_buyer_reference":buyer_reference,"buyer_mode":buyer_mode,
                "metrics":{"R_objective_pct":objective[-1].get("value") if objective else None,
                            "R_buyer_expected_pct":buyer[-1].get("value") if buyer else (buyer_reference[-1].get("value") if buyer_reference else None),
                            "R_objective_history_count":len(objective),"R_buyer_source_status":buyer_state.get("status")},
                "sources":source_rows,"buyer_note":buyer_note,"buyer_reference_note":buyer_reference_note,"return_basis":c["return_label"],
                "coverage":{"objective_start":objective[0].get("date") if objective else None,"objective_end":objective[-1].get("date") if objective else None,
                            "objective_raw_start":objective_raw[0].get("date") if objective_raw else None,"objective_raw_end":objective_raw[-1].get("date") if objective_raw else None,
                            "buyer_start":buyer[0].get("date") if buyer else None,"buyer_end":buyer[-1].get("date") if buyer else None,
                            "buyer_reference_start":buyer_reference[0].get("date") if buyer_reference else None,"buyer_reference_end":buyer_reference[-1].get("date") if buyer_reference else None},
                "method_note":"美国 R客观采用 Shiller 月度价格/股息总回报、FRED TB3MS 同窗口已实现现金回报、DGS10 名义十年国债收益率及同窗口名义每股盈利 CAGR：Rf 名义 + ERP已实现 − g名义。非正公式结果仅进入诊断，不画作合格正重力。",
                "buyer_scope_note":"R买方优先采用可逐条核验、同一日期/约10年期限/名义口径的机构 ERP 与前瞻 g 记录；缺少时只展示独立 SPF 专业预测者代理，g 为名义GDP增长，不等同于企业 EPS 增长。",
                "macro_panel":macro_panel}
        _cache_write(cache_key,result);return result

    errors=[]; data={}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs={pool.submit(_fetch_yahoo_monthly,c["symbol"],force):"index",
              pool.submit(_fetch_wb_gdp,c["wb"],force):"gdp",
              pool.submit(_fetch_wb_indicator,c["wb"],"NY.GDP.MKTP.KN",force):"real_gdp",
              pool.submit(_fetch_wb_indicator,c["wb"],"NY.GDP.DEFL.ZS",force):"deflator",
              pool.submit(_fetch_m1_rows,c,force):"m1",
              pool.submit(_fetch_wb_indicator,c["wb"],"FM.LBL.BMNY.CN",force):"m2",
              pool.submit(_fetch_wb_indicator,c["wb"],"GC.TAX.YPKG.CN",force):"tax"}
        if c.get("rf"): futs[pool.submit(_fetch_fred_rows,c["rf"],force)]="rf"
        if c.get("cash"): futs[pool.submit(_fetch_fred_rows,c["cash"],force)]="cash"
        try:
            for fut in as_completed(futs,timeout=55):
                key=futs[fut]
                try:data[key]=fut.result()
                except Exception as exc: errors.append(f"{key}: {str(exc)[:180]}")
        except FuturesTimeoutError:
            for fut,key in futs.items():
                if not fut.done(): errors.append(f"{key}: 数据源请求超时；未生成模拟数据")
    index_rows=data.get("index") or [];gdp_rows=data.get("gdp") or [];rf_rows=data.get("rf") or [];cash_rows=data.get("cash") or []
    macro_panel=_make_macro_panel(gdp_rows,data.get("real_gdp") or [],data.get("deflator") or [],
                                  data.get("m1") or [],data.get("m2") or [],frequency="年度",
                                  currency_basis=f"本币；{c.get('currency', 'LCU')} / World Bank WDI + OECD/FRED M1",
                                  tax_rows=data.get("tax") or [],tax_source="World Bank WDI GC.TAX.YPKG.CN（所得、利润及资本利得税收入；非企业税专属）")
    # Use one market observation per year around June. By then the prior fiscal year's
    # GDP has usually been published; GDP g is therefore computed through year-1 to reduce
    # look-ahead. These are trend observations, not ALFRED-vintage-perfect backtest inputs.
    year_endpoints={}
    for row in index_rows:
        try: yy=int(str(row.get("date",""))[:4])
        except Exception: continue
        # Keep the last observed month of each year, but prefer June for consistent GDP availability.
        mm=int(str(row.get("date","0000-00-00"))[5:7])
        if mm==6 or yy not in year_endpoints:
            year_endpoints[yy]=row
        elif int(str(year_endpoints[yy].get("date","0000-00-00"))[5:7])!=6 and mm>int(str(year_endpoints[yy].get("date","0000-00-00"))[5:7]):
            year_endpoints[yy]=row
    history=[]; raw_history=[]; out_of_domain=[]
    if index_rows and gdp_rows and rf_rows and cash_rows:
        for y in sorted(year_endpoints):
            row=year_endpoints[y]; dt=row.get("date","")
            # The g window ends with the latest completed GDP year generally available by date.
            g=_gdp_cagr(gdp_rows,y-1,10)
            eq,base=_annual_equity_cagr(index_rows,dt,10)
            if g is None or eq is None or base is None: continue
            cash=_cash_cagr(cash_rows,base["date"],dt)
            rf=_latest_le(rf_rows,dt,max_gap_days=120)
            if cash is None or rf is None: continue
            erp=eq-cash
            rval=float(rf["value"])+erp-g
            if not math.isfinite(rval): continue
            entry={"date":dt,"value":round(rval,5) if rval>0 else None,"raw_formula_result_pct":round(rval,5),"R_objective":round(rval,5),
                            "rf_nominal_pct":round(float(rf["value"]),5),"rf_date":rf["date"],
                            "equity_index_cagr_10y_pct":round(eq,5),"cash_return_cagr_10y_pct":round(cash,5),
                            "erp_price_index_proxy_10y_pct":round(erp,5),"g_nominal_gdp_cagr_10y_pct":round(g,5),
                            "window_start":base["date"],"window_end":dt,"horizon_years":10,
                            "basis":"nominal","quality":"CROSS_COUNTRY_PROXY" if rval>0 else "OUT_OF_DOMAIN_NONPOSITIVE_R",
                            "valid_for_gravity":rval>0,
                            "available_date_proxy":(date.fromisoformat(dt)+timedelta(days=180)).isoformat(),
                            "source":f"Yahoo Finance {c['symbol']} + World Bank NY.GDP.MKTP.CN + FRED/OECD {c['rf']} / {c['cash']}"}
            raw_history.append(entry)
            if rval>0: history.append(entry)
            else: out_of_domain.append({"date":dt,"raw_formula_result_pct":round(rval,5),"Rf_nominal_pct":round(float(rf["value"]),5),"ERP_proxy_pct":round(erp,5),"g_nominal_GDP_proxy_pct":round(g,5),"reason":"原始公式结果非正，超出 R 重力的显示适用域；不按正常 R 显示，也不截断为 0。"})
    history.sort(key=lambda x:x["date"]);history=history[-limit:]
    raw_history.sort(key=lambda x:x["date"]);raw_history=raw_history[-limit:]
    # The public institution file is checked independently; a region group only appears
    # when the source itself labels that group and has matching nominal ERP/g observations.
    buyer_records=[]
    try:
        from pro_gravity_live import _get_buyer_status
        buyer_state=_get_buyer_status(start_if_missing=True,force_retry=force)
        buyer_records=buyer_state.get("records") or []
    except Exception as exc:
        errors.append(f"机构预期数据状态读取失败：{str(exc)[:140]}")
    buyer,buyer_note=_buyer_history(c,rf_rows,buyer_records,limit)
    if not buyer: buyer_note="公开机构源没有该国家（或可明确映射区域）的同日期、名义口径、约10年 ERP 与前瞻 g 合格记录；不把美国数据冒充成本国买方预期。"
    if not rf_rows: errors.append(f"缺少合格的本币10年名义国债收益率历史：{c.get('rf')}。该国 R客观不生成假数。")
    if not cash_rows: errors.append(f"缺少合格的本币短期现金收益率历史：{c.get('cash')}。该国 R客观不生成假数。")
    if not history: errors.append("尚未形成覆盖至少10年市场、现金利率、名义 GDP 的共同日期序列。")
    source_urls=[{"name":"World Bank 名义GDP（本币现价）","url":f"https://api.worldbank.org/v2/country/{c['wb']}/indicator/NY.GDP.MKTP.CN?format=json&per_page=2000","available":bool(gdp_rows),"count":len(gdp_rows),"last_observation":gdp_rows[-1].get("date") if gdp_rows else None},
                 {"name":f"Yahoo Finance 股指 {c['symbol']}","url":f"https://finance.yahoo.com/quote/{c['symbol']}/history/","available":bool(index_rows),"count":len(index_rows),"last_observation":index_rows[-1].get("date") if index_rows else None,"definition":c["return_label"]},
                 {"name":f"FRED/OECD 本币10年名义国债收益率 {c.get('rf')}","url":c.get("rf_url"),"available":bool(rf_rows),"count":len(rf_rows),"last_observation":rf_rows[-1].get("date") if rf_rows else None},
                 {"name":f"FRED/OECD 本币短期现金利率 {c.get('cash')}","url":f"https://fred.stlouisfed.org/series/{c.get('cash')}","available":bool(cash_rows),"count":len(cash_rows),"last_observation":cash_rows[-1].get("date") if cash_rows else None},
                 {"name":"World Bank WDI 实际GDP（本币不变价，NY.GDP.MKTP.KN）","url":f"https://api.worldbank.org/v2/country/{c['wb']}/indicator/NY.GDP.MKTP.KN?format=json&per_page=2000","available":bool(data.get('real_gdp')),"count":len(data.get('real_gdp') or []),"last_observation":(data.get('real_gdp') or [{}])[-1].get('date')},
                 {"name":"World Bank WDI GDP平减指数","url":f"https://api.worldbank.org/v2/country/{c['wb']}/indicator/NY.GDP.DEFL.ZS?format=json&per_page=2000","available":bool(data.get('deflator')),"count":len(data.get('deflator') or []),"last_observation":(data.get('deflator') or [{}])[-1].get('date')},
                 {"name":"OECD 窄口径 M1（FRED 代理；缺失时退回 WDI/IMF-IFS）","url":f"https://fred.stlouisfed.org/series/MANMM101{c['wb']}189N","available":bool(data.get('m1')),"count":len(data.get('m1') or []),"last_observation":(data.get('m1') or [{}])[-1].get('observation_date') or (data.get('m1') or [{}])[-1].get('date'),"definition":"本国窄口径货币历史；各国定义仍有差异。M1和M2不互相替代"},
                 {"name":"World Bank WDI / IMF-IFS Broad money（M2/广义货币）","url":f"https://api.worldbank.org/v2/country/{c['wb']}/indicator/FM.LBL.BMNY.CN?format=json&per_page=2000","available":bool(data.get('m2')),"count":len(data.get('m2') or []),"last_observation":(data.get('m2') or [{}])[-1].get('date'),"definition":"Broad money，本币现价；不替代 M1"},
                 {"name":"所得/利润/资本利得税收入（α广义税收代理）","url":f"https://api.worldbank.org/v2/country/{c['wb']}/indicator/GC.TAX.YPKG.CN?format=json&per_page=2000","available":bool(data.get('tax')),"count":len(data.get('tax') or []),"last_observation":(data.get('tax') or [{}])[-1].get('date'),"definition":"税收口径可能含个人所得税；只作为α的广义税收强度代理，不等同于企业税率"}]
    # Optional US long-run buyer-reference line is explicitly not relabelled as local-market Rbuyer.
    buyer_reference=[]; buyer_reference_note=""
    try:
        buyer_reference=_fetch_spf_buyer_history(force=force)[-limit:]
        buyer_reference_note="美国费城联储 SPF 专业预测者长期预期参考（1992 年起；不是所选国家的本国机构预期）。"
    except Exception as exc:
        buyer_reference_note=f"美国 SPF 参考线暂不可用：{str(exc)[:120]}"
    result={"ok":True,"country":code,"name":c["name"],"status":"READY" if history else "PARTIAL_DATA",
            "objective_mode":"CROSS_COUNTRY_PROXY","objective_label":"R客观预期重力（跨国宏观比较代理，仅合格正值点）",
            "as_of":history[-1]["date"] if history else None,"objective_count":len(history),"raw_objective_count":len(raw_history),"out_of_domain_count":len(out_of_domain),"buyer_count":len(buyer),
            "R_objective":history,"R_objective_raw":raw_history,"invalid_objective":out_of_domain,"R_buyer":buyer,"R_buyer_reference":buyer_reference,
            "coverage":{"objective_start":history[0]["date"] if history else None,
             "objective_end":history[-1]["date"] if history else None,"objective_raw_start":raw_history[0]["date"] if raw_history else None,
             "objective_raw_end":raw_history[-1]["date"] if raw_history else None,"buyer_start":buyer[0]["date"] if buyer else None,"buyer_end":buyer[-1]["date"] if buyer else None,
             "buyer_reference_start":buyer_reference[0]["date"] if buyer_reference else None,"buyer_reference_end":buyer_reference[-1]["date"] if buyer_reference else None,
             "gdp_start":gdp_rows[0].get("date") if gdp_rows else None,"index_start":index_rows[0].get("date") if index_rows else None,
             "rf_start":rf_rows[0].get("date") if rf_rows else None,"rf_end":rf_rows[-1].get("date") if rf_rows else None},
            "metrics":{"R_objective_pct":history[-1]["value"] if history else None,"R_buyer_expected_pct":buyer[-1]["value"] if buyer else None},
            "return_basis":c["return_label"],"buyer_note":buyer_note,"buyer_reference_note":buyer_reference_note,
            "buyer_scope_label":"发达海外市场（ex-US）区域预期代理" if c.get("group")=="dm_ex_us" else "新兴市场区域预期代理",
            "sources":source_urls,"errors":errors,"invalid_objective_examples":out_of_domain[-5:],
            "macro_panel":macro_panel,
            "method_note":"跨国探索性代理，不等于美国严格 EPS 口径：ERP代理=本国股指10年年化回报（指数有些含分红，有些仅价格）−本币短期现金利率同窗口复利年化回报；g代理=世界银行本币现价名义GDP过去10个完整年度CAGR，约落后市场日期一个财年；Rf=本币10年期名义国债收益率。历史GDP是现行修订版，不是完整ALFRED点时版本；因此这张图是长周期趋势研究，不是可直接用于无前视偏差的正式历史交易回测。负值原始公式点被归入适用域诊断区，不作为合格R显示。",
            "buyer_scope_note":"R买方仅在真实记录能匹配国家/明确区域、同日期、约10年期限、名义ERP与前瞻g时显示。若本国序列不存在，另列美国SPF专业预测者长期预期作为参照线，但不会冒充本国R买方。"}
    _cache_write(cache_key,result);return result
