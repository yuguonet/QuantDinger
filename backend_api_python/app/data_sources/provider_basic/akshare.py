# -*- coding: utf-8 -*-
"""provider_basic / akshare —— 基本面族。

`fetch_cn_*` 系从 `app/data_sources/cn_hk_fundamentals.py` AST 原样抽取
（生产在 data_sources 包内，但为将来可替换仍复制进本层）。
"""
from __future__ import annotations

import math
import os
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

from app.data_sources.rate_limiter import get_akshare_limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)


logger = get_logger(__name__)
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_TD_TIMEOUT = 15
_TD_MAX_ATTEMPTS = 2
_TD_BACKOFF_SEC = 2.0

def ak_a_code_from_tencent(tencent_code: str) -> str:
    """Tencent 代码 → AkShare A 股代码（剥掉 sh/sz 前缀）。"""
    c = (tencent_code or "").strip().lower()
    if len(c) >= 8 and c[:2] in ("sh", "sz"):
        return c[2:]
    return c

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
    """PE/PB/PS, market cap, ROE proxy, EPS for A-share (best-effort)."""
    sym6 = ak_a_code_from_tencent(tencent_code)
    if not sym6:
        return {}
    result: Dict[str, Any] = {"source": "akshare_em"}
    info = _individual_info_map(sym6)
    if info:
        result["market_cap"] = _float_clean(info.get("总市值"))
        result["float_market_cap"] = _float_clean(info.get("流通市值"))
        ind = info.get("行业")
        if ind is not None and str(ind).strip():
            result["industry"] = str(ind).strip()
        result["total_shares"] = _float_clean(info.get("总股本"))
        result["float_shares"] = _float_clean(info.get("流通股"))

    em_sym = _eastmoney_a_em_symbol(tencent_code)
    try:
        import akshare as ak  # type: ignore
        with _bypass_proxy():
            vdf = ak.stock_zh_valuation_comparison_em(symbol=em_sym)
    except Exception as e:
        logger.debug("stock_zh_valuation_comparison_em failed %s: %s", em_sym, e)
        vdf = None

    if vdf is not None and not vdf.empty and "代码" in vdf.columns:
        hit = vdf[vdf["代码"].astype(str).str.replace(".0", "", regex=False).str.zfill(6) == sym6.zfill(6)]
        if not hit.empty:
            r = hit.iloc[0]
            pe = _float_clean(r.get("市盈率-TTM"))
            if pe is not None:
                result["pe_ratio"] = pe
            pb = _float_clean(r.get("市净率-MRQ"))
            if pb is not None:
                result["pb_ratio"] = pb
            ps = _float_clean(r.get("市销率-TTM"))
            if ps is not None:
                result["ps_ratio"] = ps
            peg = _float_clean(r.get("PEG"))
            if peg is not None:
                result["peg"] = peg

    return result

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

def _parse_ths_value(v):
    """解析同花顺数值: '1.47亿'/'23.38%'/'False'/float → float 或 None"""
    if v is None or v is False or v is True:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s == "False" or s == "True" or s == "--":
        return None
    try:
        # 百分比: "23.38%" → 23.38
        if s.endswith("%"):
            return float(s.rstrip("%"))
        # 中文单位: "1.47亿" → 147000000, "2.16万" → 21600
        if s.endswith("亿"):
            return float(s[:-1]) * 1e8
        if s.endswith("万"):
            return float(s[:-1]) * 1e4
        return float(s)
    except (ValueError, TypeError):
        return None


def fetch_cn_financial_indicators(tencent_code: str) -> Dict[str, Any]:
    """
    Fetch revenue growth, debt/equity, current ratio, FCF, margins.

    优先: stock_financial_abstract_ths（同花顺摘要，不走东财 push2）
    回退: stock_*_sheet_by_report_em（东财三大表，本环境常被出口拦截）

    2026-10-26: 东财三大表接口在本环境返回空（ProxyError/静默失败），
    改为同花顺摘要优先，东财仅作回退。
    """
    sym6 = ak_a_code_from_tencent(tencent_code)
    if not sym6:
        return {}

    result: Dict[str, Any] = {}

    try:
        import akshare as ak  # type: ignore

        # --- 同花顺财务摘要（优先，不走东财） ---
        try:
            ths_df = ak.stock_financial_abstract_ths(symbol=sym6, indicator="按报告期")
            if ths_df is not None and not ths_df.empty:
                # 取最新一期（最后行），THS 返回按日期升序
                _last = len(ths_df) - 1

                def _ths(col):
                    """直接取原始值 + _parse_ths_value（跳过 _safe_iloc 的 float 过滤）"""
                    if col not in ths_df.columns:
                        return None
                    try:
                        return _parse_ths_value(ths_df.iloc[_last][col])
                    except Exception:
                        return None

                # 增长率（THS 返回 "46.84%" 格式）
                if "营业总收入同比增长率" in ths_df.columns:
                    result["revenue_growth"] = _ths("营业总收入同比增长率")
                if "净利润同比增长率" in ths_df.columns:
                    result["earnings_growth"] = _ths("净利润同比增长率")

                # 利润率
                if "销售净利率" in ths_df.columns:
                    result["profit_margin"] = _ths("销售净利率")
                if "销售毛利率" in ths_df.columns:
                    result["gross_margin"] = _ths("销售毛利率")

                # 资产负债 / 流动性
                if "资产负债率" in ths_df.columns:
                    result["debt_ratio"] = _ths("资产负债率")
                if "流动比率" in ths_df.columns:
                    result["current_ratio"] = _ths("流动比率")
                if "速动比率" in ths_df.columns:
                    result["quick_ratio"] = _ths("速动比率")

                # ROE
                if "净资产收益率" in ths_df.columns:
                    result["roe"] = _ths("净资产收益率")

                # 现金流
                if "每股经营现金流" in ths_df.columns:
                    result["cf_per_share"] = _ths("每股经营现金流")

                if result:
                    logger.debug("[THS] cn_financial %s (报告期=%s): %d fields",
                                sym6, ths_df.iloc[_last].get("报告期", "?"), len(result))
        except Exception as e:
            logger.debug("THS financial abstract failed %s: %s", sym6, e)

        with _bypass_proxy():
            # --- profit sheet (利润表) for operating margin ---
            if "operating_margin" not in result:
                try:
                    profit_df = ak.stock_profit_sheet_by_report_em(symbol=sym6)
                    if profit_df is not None and not profit_df.empty:
                        rev_curr = _safe_iloc(profit_df, 0, "营业总收入")
                        gross_curr = _safe_iloc(profit_df, 0, "营业利润")
                        if rev_curr and rev_curr > 0 and gross_curr is not None:
                            result["operating_margin"] = round(gross_curr / rev_curr * 100, 2)
                except Exception as e:
                    logger.debug("EM profit sheet failed %s: %s", sym6, e)

            # --- balance sheet (资产负债表) for debt/equity, current ratio ---
            try:
                balance_df = ak.stock_balance_sheet_by_report_em(symbol=sym6)
                if balance_df is not None and not balance_df.empty:
                    total_debt = _safe_iloc(balance_df, 0, "负债合计") or _safe_iloc(balance_df, 0, "流动负债合计")
                    total_equity = _safe_iloc(balance_df, 0, "股东权益合计") or _safe_iloc(balance_df, 0, "所有者权益合计")
                    if total_debt is not None and total_equity and total_equity > 0:
                        result["debt_to_equity"] = round(total_debt / total_equity, 4)

                    current_assets = _safe_iloc(balance_df, 0, "流动资产合计")
                    current_liab = _safe_iloc(balance_df, 0, "流动负债合计")
                    if current_assets is not None and current_liab and current_liab > 0:
                        result["current_ratio"] = round(current_assets / current_liab, 4)

                    total_assets = _safe_iloc(balance_df, 0, "资产总计")
                    if total_assets is not None:
                        result["total_assets"] = total_assets
                    if total_debt is not None:
                        result["total_debt"] = total_debt
            except Exception as e:
                logger.debug("EM balance sheet failed %s: %s", sym6, e)

            # --- cash flow (现金流量表) for FCF ---
            try:
                cashflow_df = ak.stock_cash_flow_sheet_by_report_em(symbol=sym6)
                if cashflow_df is not None and not cashflow_df.empty:
                    op_cf = _safe_iloc(cashflow_df, 0, "经营活动产生的现金流量净额")
                    capex = _safe_iloc(cashflow_df, 0, "购建固定资产、无形资产和其他长期资产支付的现金")
                    if op_cf is not None:
                        result["operating_cash_flow"] = op_cf
                        if capex is not None:
                            result["free_cash_flow"] = round(op_cf - abs(capex), 2)
                    invest_cf = _safe_iloc(cashflow_df, 0, "投资活动产生的现金流量净额")
                    finance_cf = _safe_iloc(cashflow_df, 0, "筹资活动产生的现金流量净额")
                    if invest_cf is not None:
                        result["investing_cash_flow"] = invest_cf
                    if finance_cf is not None:
                        result["financing_cash_flow"] = finance_cf
            except Exception as e:
                logger.debug("EM cash flow failed %s: %s", sym6, e)

    except ImportError:
        logger.warning("akshare not installed, A-share financial data unavailable")

    if result:
        logger.debug("A-share financial indicators for %s: %d fields", sym6, len(result))
    return result


def _rows_object(d: Dict[str, Any]) -> Dict[str, Any]:
    return {"object": d}


def fetch_stock_basic() -> Dict[str, Any]:
    """A股代码-名称清单（stock_basic）。"""
    get_akshare_limiter().wait()
    df = _ak().stock_info_a_code_name()
    rows = [] if df is None or getattr(df, "empty", True) else df.to_dict("records")
    return {"rows": [{"code": _ss(r.get("code")), "name": _ss(r.get("name"))} for r in rows]}


def _ss(v, default="") -> str:
    if v is None:
        return default
    return str(v).strip() if v else default


def _ak():
    import akshare as ak
    return ak


from app.data_sources.provider_basic import register   # noqa: E402


@register
class AkshareBasicProvider:
    name = "akshare"
    priority = 50
    markets = {"CNStock"}
    capabilities = {
        "cn_fundamental": lambda **kw: _rows_object(fetch_cn_fundamental_akshare(kw["code"])),
        "cn_financial": lambda **kw: _rows_object(fetch_cn_financial_indicators(kw["code"])),
        "stock_basic": lambda **kw: fetch_stock_basic(),
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
