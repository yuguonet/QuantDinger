"""
A-share / HK share fundamentals — multi-tier fallback.

Priority (when TWELVE_DATA_API_KEY configured):
  Twelve Data /statistics + /profile  →  AkShare (Eastmoney, fragile overseas)

Without API key:
  AkShare only (may fail from overseas servers)

Keys are aligned with MarketDataCollector expectations (pe_ratio, pb_ratio, etc.).
"""

from __future__ import annotations

import math
import os
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 符号/密钥工具 — 从已废弃的 app.data_sources.asia_stock_kline 内联
# (2026-10-16 该文件移除: 这 4 个函数全库仅本文件消费, 内联而非另开共享模块)
# ---------------------------------------------------------------------------


def ak_a_code_from_tencent(tencent_code: str) -> str:
    """Tencent 代码 → AkShare A 股代码（剥掉 sh/sz 前缀）。"""
    c = (tencent_code or "").strip().lower()
    if len(c) >= 8 and c[:2] in ("sh", "sz"):
        return c[2:]
    return c


def ak_hk_code_from_tencent(tencent_code: str) -> str:
    """Tencent 代码 → AkShare 港股代码（5 位数字）。"""
    c = (tencent_code or "").strip().upper().replace(".HK", "")
    if c.startswith("HK"):
        num = c[2:]
    else:
        num = c
    if num.isdigit():
        return num.zfill(5)
    return num


def _get_twelve_data_api_key() -> str:
    """Twelve Data API 密钥：附加配置优先，回落环境变量。"""
    try:
        from app.utils.config_loader import load_addon_config
        key = load_addon_config().get("twelve_data", {}).get("api_key", "")
        if key:
            return key
    except Exception:
        pass
    return (os.getenv("TWELVE_DATA_API_KEY") or "").strip()


def _td_symbol_and_exchange(tencent_code: str, is_hk: bool) -> tuple[str, str]:
    """Convert Tencent code to Twelve Data (symbol, exchange).

    Twelve Data time_series requires exchange *name* (SSE / SZSE / HKEX / BSE),
    not the MIC code (XSHG / XSHE / XHKG).
    """
    c = (tencent_code or "").strip().upper()
    if is_hk:
        num = c.replace("HK", "")
        if num.isdigit():
            num = str(int(num)).zfill(4)
        return num, "HKEX"
    digits = c.lstrip("SHSZBJ")
    # 北证判据必须先于「6/9 开头 = SSE」——92 段以 9 开头，排在后面会被当成
    # 沪 B 股判成 SSE（实测修复前 920682 → SSE）。北证段见 normalizer.CN_BJ_PREFIXES。
    from app.data_sources.normalizer import is_bj_code
    if c.startswith("BJ") or is_bj_code(digits):
        return digits, "BSE"
    if c.startswith("SH") or digits.startswith(("6", "9")):
        return digits, "SSE"
    return digits, "SZSE"


_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@contextmanager
def _bypass_proxy() -> Generator[None, None, None]:
    """
    Temporarily clear proxy env vars so that AkShare / requests talks to
    Chinese domestic sites (Eastmoney, Sina, etc.) directly, not through
    an overseas SOCKS proxy.  Restored after the block exits.
    """
    saved = {}
    for key in _PROXY_KEYS:
        val = os.environ.pop(key, None)
        if val is not None:
            saved[key] = val
    try:
        yield
    finally:
        for key, val in saved.items():
            os.environ[key] = val

_TD_TIMEOUT = 15
_TD_MAX_ATTEMPTS = 2
_TD_BACKOFF_SEC = 2.0


def _float_clean(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Twelve Data fundamentals (globally stable, paid)
# ---------------------------------------------------------------------------

def _td_request(endpoint: str, symbol: str, exchange: str) -> Optional[Dict[str, Any]]:
    """Generic Twelve Data GET with retry."""
    api_key = _get_twelve_data_api_key()
    if not api_key:
        return None
    url = f"https://api.twelvedata.com{endpoint}"
    params = {"symbol": symbol, "exchange": exchange, "apikey": api_key}
    for attempt in range(_TD_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, timeout=_TD_TIMEOUT)
            data = resp.json()
            if data.get("status") == "error":
                code = data.get("code", "")
                msg = (data.get("message") or "")[:120]
                if code == 429 or "API credits" in msg or "minute limit" in msg:
                    logger.warning("TwelveData rate limit on %s %s/%s: %s", endpoint, symbol, exchange, msg)
                else:
                    logger.debug("TwelveData %s error %s/%s: %s", endpoint, symbol, exchange, msg)
                return None
            return data
        except Exception as e:
            if attempt + 1 < _TD_MAX_ATTEMPTS:
                time.sleep(_TD_BACKOFF_SEC)
                continue
            logger.warning("TwelveData %s request failed %s/%s: %s", endpoint, symbol, exchange, e)
    return None


def fetch_twelvedata_fundamental(tencent_code: str, is_hk: bool) -> Dict[str, Any]:
    """Fetch PE/PB/PS/PEG/ROE/margin/market_cap/52w from Twelve Data /statistics."""
    symbol, exchange = _td_symbol_and_exchange(tencent_code, is_hk)
    data = _td_request("/statistics", symbol, exchange)
    if not data or "statistics" not in data:
        return {}

    stats = data["statistics"]
    result: Dict[str, Any] = {"source": "twelvedata"}

    vm = stats.get("valuations_metrics") or {}
    result["market_cap"] = _float_clean(vm.get("market_capitalization"))
    result["pe_ratio"] = _float_clean(vm.get("trailing_pe"))
    result["forward_pe"] = _float_clean(vm.get("forward_pe"))
    result["pb_ratio"] = _float_clean(vm.get("price_to_book_mrq"))
    result["ps_ratio"] = _float_clean(vm.get("price_to_sales_ttm"))
    result["peg"] = _float_clean(vm.get("peg_ratio"))
    result["enterprise_value"] = _float_clean(vm.get("enterprise_value"))

    fin = stats.get("financials") or {}
    result["profit_margin"] = _float_clean(fin.get("profit_margin"))
    result["gross_margin"] = _float_clean(fin.get("gross_margin"))
    result["operating_margin"] = _float_clean(fin.get("operating_margin"))
    result["roe"] = _float_clean(fin.get("return_on_equity_ttm"))
    result["roa"] = _float_clean(fin.get("return_on_assets_ttm"))

    # Nested income_statement / balance_sheet / cash_flow inside financials
    fin_is = fin.get("income_statement") or {}
    qrg = _float_clean(fin_is.get("quarterly_revenue_growth"))
    if qrg is not None:
        result["revenue_growth"] = round(qrg * 100, 2) if abs(qrg) < 10 else qrg
    qeg = _float_clean(fin_is.get("quarterly_earnings_growth_yoy"))
    if qeg is not None:
        result["earnings_growth"] = round(qeg * 100, 2) if abs(qeg) < 10 else qeg
    result["revenue_ttm"] = _float_clean(fin_is.get("revenue_ttm"))
    result["ebitda"] = _float_clean(fin_is.get("ebitda"))
    result["eps"] = _float_clean(fin_is.get("diluted_eps_ttm"))

    fin_bs = fin.get("balance_sheet") or {}
    result["debt_to_equity"] = _float_clean(fin_bs.get("total_debt_to_equity_mrq"))
    if result.get("debt_to_equity") is not None:
        result["debt_to_equity"] = round(result["debt_to_equity"] / 100, 4)
    result["current_ratio"] = _float_clean(fin_bs.get("current_ratio_mrq"))
    result["total_debt"] = _float_clean(fin_bs.get("total_debt_mrq"))
    result["total_cash"] = _float_clean(fin_bs.get("total_cash_mrq"))

    fin_cf = fin.get("cash_flow") or {}
    result["operating_cash_flow"] = _float_clean(fin_cf.get("operating_cash_flow_ttm"))
    result["free_cash_flow"] = _float_clean(fin_cf.get("levered_free_cash_flow_ttm"))

    ss = stats.get("stock_statistics") or {}
    result["total_shares"] = _float_clean(ss.get("shares_outstanding"))
    result["float_shares"] = _float_clean(ss.get("float_shares"))

    sp = stats.get("stock_price_summary") or {}
    result["52w_high"] = _float_clean(sp.get("fifty_two_week_high"))
    result["52w_low"] = _float_clean(sp.get("fifty_two_week_low"))
    result["beta"] = _float_clean(sp.get("beta"))

    div = stats.get("dividends_and_splits") or {}
    result["dividend_yield"] = _float_clean(div.get("trailing_annual_dividend_yield"))
    result["dividend_rate"] = _float_clean(div.get("trailing_annual_dividend_rate"))

    non_null = sum(1 for v in result.values() if v is not None and v != "twelvedata")
    logger.debug("TwelveData /statistics %s/%s: %d non-null fields", symbol, exchange, non_null)
    return result


def fetch_twelvedata_statements(tencent_code: str, is_hk: bool) -> Dict[str, Any]:
    """
    Fetch structured financial statements from Twelve Data
    /income_statement, /balance_sheet, /cash_flow endpoints.

    Returns a dict with keys: income_statement, balance_sheet, cash_flow.
    Also extracts top-level growth/ratio indicators when available.
    Works globally — ideal for overseas servers where AkShare is unreliable.
    """
    symbol, exchange = _td_symbol_and_exchange(tencent_code, is_hk)
    result: Dict[str, Any] = {}
    statements: Dict[str, Any] = {}

    # --- income statement ---
    try:
        is_data = _td_request("/income_statement", symbol, exchange)
        items = (is_data or {}).get("income_statement") or []
        if items:
            curr = items[0]
            statements["income_statement"] = {
                "latest_date": curr.get("fiscal_date"),
                "total_revenue": _float_clean(curr.get("sales")),
                "cost_of_goods": _float_clean(curr.get("cost_of_goods")),
                "gross_profit": _float_clean(curr.get("gross_profit")),
                "operating_income": _float_clean(curr.get("operating_income")),
                "net_income": _float_clean(curr.get("net_income")),
                "ebitda": _float_clean(curr.get("ebitda")),
                "eps_diluted": _float_clean(curr.get("eps_diluted")),
            }
            if len(items) >= 5:
                prev_rev = _float_clean(items[4].get("sales"))
                curr_rev = _float_clean(curr.get("sales"))
                if prev_rev and curr_rev and prev_rev > 0:
                    result["revenue_growth"] = round((curr_rev - prev_rev) / abs(prev_rev) * 100, 2)
                prev_ni = _float_clean(items[4].get("net_income"))
                curr_ni = _float_clean(curr.get("net_income"))
                if prev_ni and curr_ni and prev_ni > 0:
                    result["earnings_growth"] = round((curr_ni - prev_ni) / abs(prev_ni) * 100, 2)
    except Exception as e:
        logger.debug("TwelveData /income_statement failed %s/%s: %s", symbol, exchange, e)

    # --- balance sheet ---
    try:
        bs_data = _td_request("/balance_sheet", symbol, exchange)
        items = (bs_data or {}).get("balance_sheet") or []
        if items:
            curr = items[0]
            assets = curr.get("assets") or {}
            liabilities = curr.get("liabilities") or {}
            equity = curr.get("shareholders_equity") or {}
            ca = assets.get("current_assets") or {}
            cl = liabilities.get("current_liabilities") or {}
            statements["balance_sheet"] = {
                "latest_date": curr.get("fiscal_date"),
                "total_assets": _float_clean(assets.get("total_assets")),
                "total_liabilities": _float_clean(liabilities.get("total_liabilities")),
                "stockholders_equity": _float_clean(equity.get("total_shareholders_equity")),
                "current_assets": _float_clean(ca.get("total_current_assets")),
                "current_liabilities": _float_clean(cl.get("total_current_liabilities")),
            }
            total_liab = _float_clean(liabilities.get("total_liabilities"))
            total_eq = _float_clean(equity.get("total_shareholders_equity"))
            if total_liab is not None and total_eq and total_eq > 0:
                result.setdefault("debt_to_equity", round(total_liab / total_eq, 4))
            t_ca = _float_clean(ca.get("total_current_assets"))
            t_cl = _float_clean(cl.get("total_current_liabilities"))
            if t_ca is not None and t_cl and t_cl > 0:
                result.setdefault("current_ratio", round(t_ca / t_cl, 4))
    except Exception as e:
        logger.debug("TwelveData /balance_sheet failed %s/%s: %s", symbol, exchange, e)

    # --- cash flow ---
    try:
        cf_data = _td_request("/cash_flow", symbol, exchange)
        items = (cf_data or {}).get("cash_flow") or []
        if items:
            curr = items[0]
            op = curr.get("operating_activities") or {}
            inv = curr.get("investing_activities") or {}
            fin = curr.get("financing_activities") or {}
            statements["cash_flow"] = {
                "latest_date": curr.get("fiscal_date"),
                "operating_cash_flow": _float_clean(op.get("operating_cash_flow")),
                "investing_cash_flow": _float_clean(inv.get("investing_cash_flow")),
                "financing_cash_flow": _float_clean(fin.get("financing_cash_flow")),
                "free_cash_flow": _float_clean(curr.get("free_cash_flow")),
                "capital_expenditures": _float_clean(inv.get("capital_expenditures")),
            }
            result.setdefault("operating_cash_flow", _float_clean(op.get("operating_cash_flow")))
            result.setdefault("free_cash_flow", _float_clean(curr.get("free_cash_flow")))
    except Exception as e:
        logger.debug("TwelveData /cash_flow failed %s/%s: %s", symbol, exchange, e)

    if statements:
        result["financial_statements"] = statements
    logger.debug("TwelveData statements %s/%s: %d statement sections, %d indicators",
                 symbol, exchange, len(statements), len(result))
    return result


def fetch_twelvedata_profile(tencent_code: str, is_hk: bool) -> Dict[str, Any]:
    """Fetch company info from Twelve Data /profile."""
    symbol, exchange = _td_symbol_and_exchange(tencent_code, is_hk)
    data = _td_request("/profile", symbol, exchange)
    if not data or not data.get("name"):
        return {}

    out: Dict[str, Any] = {"source": "twelvedata"}
    for src, dst in (
        ("name", "name"),
        ("industry", "industry"),
        ("sector", "sector"),
        ("website", "website"),
        ("description", "description"),
        ("employees", "employees"),
        ("name", "full_name"),
    ):
        v = data.get(src)
        if v is not None and str(v).strip():
            out[dst] = str(v).strip() if isinstance(v, str) else v

    country = data.get("country")
    if country:
        out["country"] = country

    logger.debug("TwelveData /profile %s/%s: name=%s industry=%s", symbol, exchange, out.get("name"), out.get("industry"))
    return out


def fetch_twelvedata_earnings(tencent_code: str, is_hk: bool) -> Dict[str, Any]:
    """
    Fetch quarterly earnings history from Twelve Data /earnings endpoint.
    Returns an 'earnings' dict compatible with the fast_analysis prompt format:
      { "history": [...], "quarterly": {...} }
    """
    symbol, exchange = _td_symbol_and_exchange(tencent_code, is_hk)
    data = _td_request("/earnings", symbol, exchange)
    if not data:
        return {}

    earnings_list = data.get("earnings") or []
    if not earnings_list:
        return {}

    result: Dict[str, Any] = {}
    history = []
    for item in earnings_list[:8]:
        eps_actual = _float_clean(item.get("eps"))
        eps_estimate = _float_clean(item.get("eps_estimate"))
        surprise = None
        if eps_actual is not None and eps_estimate is not None and eps_estimate != 0:
            surprise = round((eps_actual - eps_estimate) / abs(eps_estimate) * 100, 1)
        history.append({
            "date": item.get("date", item.get("period", "N/A")),
            "eps_actual": eps_actual,
            "eps_estimate": eps_estimate,
            "surprise": surprise,
            "revenue": _float_clean(item.get("revenue")),
            "revenue_estimate": _float_clean(item.get("revenue_estimate")),
        })

    if history:
        result["history"] = history
        latest = history[0]
        result["quarterly"] = {
            "latest_quarter": latest.get("date"),
            "revenue": latest.get("revenue"),
            "earnings": latest.get("eps_actual"),
        }

    logger.debug("TwelveData /earnings %s/%s: %d quarters", symbol, exchange, len(history))
    return result


# ---------------------------------------------------------------------------
# AkShare fundamentals (Eastmoney — fragile overseas, used as fallback)
# ---------------------------------------------------------------------------

def _eastmoney_a_em_symbol(tencent_code: str) -> str:
    c = ak_a_code_from_tencent(tencent_code)
    c = (c or "").zfill(6)
    if c.startswith("6"):
        return "SH" + c
    return "SZ" + c


def _individual_info_map(symbol_6: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import akshare as ak  # type: ignore
        with _bypass_proxy():
            df = ak.stock_individual_info_em(symbol=symbol_6)
    except Exception as e:
        logger.debug("stock_individual_info_em failed %s: %s", symbol_6, e)
        return out
    if df is None or getattr(df, "empty", True) or len(df.columns) < 2:
        return out
    kcol, vcol = df.columns[0], df.columns[1]
    for _, row in df.iterrows():
        try:
            k = str(row[kcol]).strip()
            if k:
                out[k] = row[vcol]
        except Exception:
            continue
    return out


def fetch_cn_fundamental_akshare(tencent_code: str) -> Dict[str, Any]:
    """PE/PB/PS, market cap, ROE proxy, EPS for A-share.

    2026-10-26 Phase C: 函数体已搬进 provider_basic/akshare.py，
    本函数改走 coord_basic 多源调度。
    """
    from app.data_sources.coord_basic import coordinate
    out = coordinate("cn_fundamental", code=tencent_code)
    if out.get("ok") and out.get("data", {}).get("object"):
        return out["data"]["object"]
    return {}


def fetch_hk_fundamental_akshare(tencent_code: str) -> Dict[str, Any]:
    hk5 = ak_hk_code_from_tencent(tencent_code)
    if not hk5:
        return {}
    result: Dict[str, Any] = {"source": "akshare_em"}
    try:
        import akshare as ak  # type: ignore
        with _bypass_proxy():
            df = ak.stock_hk_financial_indicator_em(symbol=hk5)
    except Exception as e:
        logger.debug("stock_hk_financial_indicator_em failed %s: %s", hk5, e)
        return result
    if df is None or df.empty:
        return result
    r = df.iloc[0]
    result["pe_ratio"] = _float_clean(r.get("市盈率"))
    result["pb_ratio"] = _float_clean(r.get("市净率"))
    result["eps"] = _float_clean(r.get("基本每股收益(元)"))
    result["roe"] = _float_clean(r.get("股东权益回报率(%)"))
    result["profit_margin"] = _float_clean(r.get("销售净利率(%)"))
    mcap = _float_clean(r.get("总市值(港元)")) or _float_clean(r.get("港股市值(港元)"))
    if mcap is not None:
        result["market_cap"] = mcap
    result["dividend_yield"] = _float_clean(r.get("股息率TTM(%)"))
    return result


def fetch_cn_company_extras(tencent_code: str) -> Dict[str, Any]:
    sym6 = ak_a_code_from_tencent(tencent_code)
    if not sym6:
        return {}
    info = _individual_info_map(sym6)
    out: Dict[str, Any] = {}
    if info.get("行业"):
        out["industry"] = str(info["行业"]).strip()
    if info.get("上市时间"):
        out["ipo_date"] = str(info["上市时间"]).strip()
    return out


def fetch_hk_company_extras(tencent_code: str) -> Dict[str, Any]:
    hk5 = ak_hk_code_from_tencent(tencent_code)
    if not hk5:
        return {}
    out: Dict[str, Any] = {}
    try:
        import akshare as ak  # type: ignore
        with _bypass_proxy():
            df = ak.stock_hk_company_profile_em(symbol=hk5)
    except Exception as e:
        logger.debug("stock_hk_company_profile_em failed %s: %s", hk5, e)
        return out
    if df is None or df.empty:
        return out
    r = df.iloc[0]
    for key, col in (
        ("industry", "所属行业"),
        ("ipo_date", "公司成立日期"),
        ("website", "公司网址"),
        ("full_name", "公司名称"),
    ):
        v = r.get(col)
        if v is not None and str(v).strip():
            out[key] = str(v).strip()
    return out


# ---------------------------------------------------------------------------
# A-share financial statements & growth metrics via AkShare (Eastmoney)
# ---------------------------------------------------------------------------

def _safe_iloc(df, row: int, col: str) -> Optional[float]:
    """Safely extract a float from a DataFrame cell."""
    try:
        if df is None or getattr(df, "empty", True):
            return None
        if col not in df.columns:
            return None
        return _float_clean(df.iloc[row][col])
    except Exception:
        return None


def _pct_change(curr: Optional[float], prev: Optional[float]) -> Optional[float]:
    if curr is None or prev is None or prev == 0:
        return None
    return round((curr - prev) / abs(prev) * 100, 2)


def fetch_cn_financial_indicators(tencent_code: str) -> Dict[str, Any]:
    """Fetch revenue growth, debt/equity, current ratio, FCF, margins.

    2026-10-26 Phase C: 函数体已搬进 provider_basic/akshare.py，
    本函数改走 coord_basic 多源调度。
    """
    from app.data_sources.coord_basic import coordinate
    out = coordinate("cn_financial", code=tencent_code)
    if out.get("ok") and out.get("data", {}).get("object"):
        return out["data"]["object"]
    return {}


def fetch_cn_financial_statements(tencent_code: str) -> Dict[str, Any]:
    """Build structured financial_statements dict for A-shares (latest report)."""
    sym6 = ak_a_code_from_tencent(tencent_code)
    if not sym6:
        return {}
    statements: Dict[str, Any] = {}

    try:
        import akshare as ak  # type: ignore

        with _bypass_proxy():
            try:
                profit_df = ak.stock_profit_sheet_by_report_em(symbol=sym6)
                if profit_df is not None and not profit_df.empty:
                    r = profit_df.iloc[0]
                    date_col = next((c for c in ("报告日期", "REPORT_DATE_NAME", "REPORT_DATE") if c in profit_df.columns), None)
                    statements["income_statement"] = {
                        "latest_date": str(r[date_col])[:10] if date_col and r.get(date_col) is not None else None,
                        "total_revenue": _float_clean(r.get("营业总收入")),
                        "operating_income": _float_clean(r.get("营业利润")),
                        "net_income": _float_clean(r.get("净利润")),
                    }
            except Exception as e:
                logger.debug("A-share income statement failed %s: %s", sym6, e)

            try:
                balance_df = ak.stock_balance_sheet_by_report_em(symbol=sym6)
                if balance_df is not None and not balance_df.empty:
                    r = balance_df.iloc[0]
                    date_col = next((c for c in ("报告日期", "REPORT_DATE_NAME", "REPORT_DATE") if c in balance_df.columns), None)
                    statements["balance_sheet"] = {
                        "latest_date": str(r[date_col])[:10] if date_col and r.get(date_col) is not None else None,
                        "total_assets": _float_clean(r.get("资产总计")),
                        "total_liabilities": _float_clean(r.get("负债合计")),
                        "stockholders_equity": _float_clean(r.get("股东权益合计")) or _float_clean(r.get("所有者权益合计")),
                        "current_assets": _float_clean(r.get("流动资产合计")),
                        "current_liabilities": _float_clean(r.get("流动负债合计")),
                    }
            except Exception as e:
                logger.debug("A-share balance sheet for statements failed %s: %s", sym6, e)

            try:
                cashflow_df = ak.stock_cash_flow_sheet_by_report_em(symbol=sym6)
                if cashflow_df is not None and not cashflow_df.empty:
                    r = cashflow_df.iloc[0]
                    date_col = next((c for c in ("报告日期", "REPORT_DATE_NAME", "REPORT_DATE") if c in cashflow_df.columns), None)
                    op_cf = _float_clean(r.get("经营活动产生的现金流量净额"))
                    capex = _float_clean(r.get("购建固定资产、无形资产和其他长期资产支付的现金"))
                    statements["cash_flow"] = {
                        "latest_date": str(r[date_col])[:10] if date_col and r.get(date_col) is not None else None,
                        "operating_cash_flow": op_cf,
                        "investing_cash_flow": _float_clean(r.get("投资活动产生的现金流量净额")),
                        "financing_cash_flow": _float_clean(r.get("筹资活动产生的现金流量净额")),
                        "free_cash_flow": round(op_cf - abs(capex), 2) if op_cf is not None and capex is not None else None,
                    }
            except Exception as e:
                logger.debug("A-share cash flow for statements failed %s: %s", sym6, e)

    except ImportError:
        pass

    return statements if statements else {}


def fetch_hk_financial_indicators(tencent_code: str) -> Dict[str, Any]:
    """
    Fetch growth & debt metrics for HK stocks from Eastmoney financial indicators.
    """
    hk5 = ak_hk_code_from_tencent(tencent_code)
    if not hk5:
        return {}
    result: Dict[str, Any] = {}

    try:
        import akshare as ak  # type: ignore
        with _bypass_proxy():
            df = ak.stock_hk_financial_indicator_em(symbol=hk5)
        if df is None or df.empty:
            return result

        curr = df.iloc[0]
        prev = df.iloc[1] if len(df) > 1 else None

        rev_curr = _float_clean(curr.get("营业总收入(元)")) or _float_clean(curr.get("营业收入(元)"))
        if prev is not None:
            rev_prev = _float_clean(prev.get("营业总收入(元)")) or _float_clean(prev.get("营业收入(元)"))
            result["revenue_growth"] = _pct_change(rev_curr, rev_prev)

            net_curr = _float_clean(curr.get("净利润(元)"))
            net_prev = _float_clean(prev.get("净利润(元)"))
            result["earnings_growth"] = _pct_change(net_curr, net_prev)

        de = _float_clean(curr.get("资产负债率(%)"))
        if de is not None:
            result["debt_to_equity"] = round(de / (100 - de), 4) if de < 100 else None

        result["current_ratio"] = _float_clean(curr.get("流动比率"))
        result["quick_ratio"] = _float_clean(curr.get("速动比率"))

        op_cf = _float_clean(curr.get("每股经营现金流(元)"))
        shares = _float_clean(curr.get("总股本(股)"))
        if op_cf is not None and shares and shares > 0:
            result["operating_cash_flow"] = round(op_cf * shares, 2)

    except Exception as e:
        logger.debug("HK financial indicators failed %s: %s", hk5, e)

    if result:
        logger.debug("HK financial indicators for %s: %d fields", hk5, len(result))
    return result


def fetch_hk_financial_statements(tencent_code: str) -> Dict[str, Any]:
    """Build structured financial_statements dict for H-shares using Eastmoney financial indicators."""
    hk5 = ak_hk_code_from_tencent(tencent_code)
    if not hk5:
        return {}
    statements: Dict[str, Any] = {}

    try:
        import akshare as ak  # type: ignore
        with _bypass_proxy():
            df = ak.stock_hk_financial_indicator_em(symbol=hk5)
        if df is None or df.empty:
            return {}

        curr = df.iloc[0]
        date_col = next(
            (c for c in ("报告期", "REPORT_DATE", "截止日期") if c in df.columns),
            None,
        )
        report_date = str(curr[date_col])[:10] if date_col and curr.get(date_col) is not None else None

        rev = _float_clean(curr.get("营业总收入(元)")) or _float_clean(curr.get("营业收入(元)"))
        net_income = _float_clean(curr.get("净利润(元)"))
        total_assets = _float_clean(curr.get("总资产(元)"))
        de_pct = _float_clean(curr.get("资产负债率(%)"))

        statements["income_statement"] = {
            "latest_date": report_date,
            "total_revenue": rev,
            "net_income": net_income,
            "operating_income": None,
            "gross_profit": None,
            "eps_diluted": _float_clean(curr.get("基本每股收益(元)")),
        }

        total_liab = None
        stockholders_equity = None
        if total_assets and de_pct is not None:
            total_liab = round(total_assets * de_pct / 100, 2)
            stockholders_equity = round(total_assets - total_liab, 2)

        statements["balance_sheet"] = {
            "latest_date": report_date,
            "total_assets": total_assets,
            "total_liabilities": total_liab,
            "stockholders_equity": stockholders_equity,
            "current_assets": None,
            "current_liabilities": None,
        }

        op_cf_per_share = _float_clean(curr.get("每股经营现金流(元)"))
        shares = _float_clean(curr.get("总股本(股)"))
        op_cf = round(op_cf_per_share * shares, 2) if op_cf_per_share is not None and shares and shares > 0 else None
        statements["cash_flow"] = {
            "latest_date": report_date,
            "operating_cash_flow": op_cf,
            "free_cash_flow": None,
            "investing_cash_flow": None,
            "financing_cash_flow": None,
        }

    except Exception as e:
        logger.debug("HK financial statements (AkShare) failed %s: %s", hk5, e)

    if statements:
        logger.debug("HK financial statements for %s: %d sections", hk5, len(statements))
    return statements
