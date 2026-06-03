# -*- coding: utf-8 -*-
"""
cn_stock_info.py — A股个股基本面数据（纯 HTTP，双源互补）

═══════════════════════════════════════════════════════════════════════════════
  数据源：新浪财经 + 腾讯财经（均无需 API Key）
═══════════════════════════════════════════════════════════════════════════════

  覆盖内容：
    1. 个股信息    名称、行业、概念、总市值、流通市值、PE、PB
    2. 财务指标    ROE、毛利率、净利率、EPS、每股净资产、资产负债率、流动比率
    3. 利润表      营业收入、净利润、扣非净利润、毛利
    4. 资产负债表  总资产、总负债、净资产、流动资产、流动负债
    5. 现金流量表  经营/投资/筹资三大现金流净额
    6. 杜邦分析    净利率、总资产周转率、权益乘数（自动计算）
    7. 估值指标    PE、PB、PS、股息率（当前值）
    8. 十大流通股东 股东名称、持股数、持股比例
    9. 机构持仓    基金持仓明细（如有）

  不可用字段标记 None，上层可识别。

═══════════════════════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════════════════════

    from app.utils.cn_stock_info import get_cn_stock_info
    info = get_cn_stock_info("600519")
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}


# ═════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════════════════════════════════

def _detect_market(code: str) -> str:
    c = (code or "").strip()
    if not c.isdigit() or len(c) != 6:
        return ""
    if c.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if c.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "SZ"
    if c.startswith(("43", "82", "83", "87", "88")):
        return "BJ"
    return ""


def _sina_sym(code: str) -> str:
    m = _detect_market(code)
    return f"{m.lower()}{code}" if m else ""


def _tencent_sym(code: str) -> str:
    m = _detect_market(code)
    return f"{m.lower()}{code}" if m else ""


def _f(val: Any) -> Optional[float]:
    """安全转 float。"""
    if val is None or val == "" or val == "--" or val == "-":
        return None
    try:
        v = float(val)
        return v if v == v else None
    except (ValueError, TypeError):
        return None


def _http_get(url: str, params: dict = None, timeout: int = 10,
              encoding: str = None) -> Optional[str]:
    import urllib.request, urllib.parse
    try:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if encoding:
                return raw.decode(encoding, errors="replace")
            for enc in ("utf-8", "gbk", "gb2312", "gb18030"):
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug("GET %s failed: %s", url, e)
        return None


def _http_get_json(url: str, params: dict = None, timeout: int = 10) -> Optional[Any]:
    text = _http_get(url, params=params, timeout=timeout)
    if not text:
        return None
    m = re.search(r'[{\[].*[}\]]', text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _merge(base: dict, extra: dict) -> dict:
    """合并：extra 只填充 base 中为空/None 的字段。"""
    for k, v in extra.items():
        if v is not None and v != "" and v != [] and v != {}:
            if k not in base or base[k] is None or base[k] == "" or base[k] == 0:
                base[k] = v
    return base


def _re(text: str, *patterns: str) -> Optional[str]:
    """正则匹配，返回第一个捕获组。"""
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return None


# ═════════════════════════════════════════════════════════════════════════════
# 新浪财经 — 数据拉取
# ═════════════════════════════════════════════════════════════════════════════

def _sina_realtime(code: str) -> Dict[str, Any]:
    """新浪实时行情：价格、市值、PE、PB、股本。"""
    r: Dict[str, Any] = {}
    sym = _sina_sym(code)
    if not sym:
        return r

    # 行情数据
    text = _http_get(f"https://hq.sinajs.cn/list={sym}", encoding="gbk", timeout=8)
    if text and "=" in text:
        m = re.search(r'"([^"]*)"', text)
        if m:
            fields = m.group(1).split(",")
            if len(fields) >= 32:
                r["name"] = fields[0]
                r["price"] = _f(fields[3])
                r["high"] = _f(fields[4])
                r["low"] = _f(fields[5])
                r["volume"] = _f(fields[8])
                r["turnover"] = _f(fields[9])

    # 市值等指标页
    html = _http_get(f"https://finance.sina.com.cn/realstock/company/{sym}/nc.shtml", timeout=10)
    if html:
        for key, pats in {
            "pe_ratio": [r'市盈率[（(]动[)）][：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
            "pb_ratio": [r'市净率[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
            "market_cap": [r'总市值[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
            "float_market_cap": [r'流通市值[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
            "industry": [r'所属行业[：:]\s*(?:</?\w[^>]*>)?([^<]+)',
                         r'行业分类[：:]\s*(?:</?\w[^>]*>)?([^<]+)'],
            "list_date": [r'上市日期[：:]\s*(?:</?\w[^>]*>)?([^<]+)',
                          r'上市时间[：:]\s*(?:</?\w[^>]*>)?([^<]+)'],
        }.items():
            for pat in pats:
                m = re.search(pat, html)
                if m:
                    val = m.group(1).strip()
                    if val and val != "--":
                        if key in ("pe_ratio", "pb_ratio", "market_cap", "float_market_cap"):
                            r[key] = _f(val)
                        else:
                            r[key] = val
                    break

    # 概念板块
    try:
        text2 = _http_get(
            f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_StockInfo/stockid/{code}/displaytype/4.phtml",
            timeout=10,
        )
        if text2:
            concepts = []
            for cm in re.finditer(r'概念题材[^<]*<[^>]*>([^<]+)', text2):
                c = cm.group(1).strip()
                if c and c != "--":
                    concepts.append(c)
            if concepts:
                r["concepts"] = concepts
    except Exception:
        pass

    return r


def _sina_finance(code: str) -> Dict[str, Any]:
    """新浪财经 — 财务指标（ROE、EPS、每股净资产等）。"""
    r: Dict[str, Any] = {}
    text = _http_get(
        f"https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/{code}/ctrl/2019/displaytype/4.phtml",
        timeout=10,
    )
    if not text:
        return r

    for key, pat in {
        "roe": r'净资产收益率.*?<td[^>]*>([\d.]+)',
        "eps": r'(?:基本)?每股收益.*?<td[^>]*>([\d.]+)',
        "bvps": r'每股净资产.*?<td[^>]*>([\d.]+)',
        "gross_margin": r'销售毛利率.*?<td[^>]*>([\d.]+)',
        "net_margin": r'销售净利率.*?<td[^>]*>([\d.]+)',
        "debt_ratio": r'资产负债率.*?<td[^>]*>([\d.]+)',
        "current_ratio": r'流动比率.*?<td[^>]*>([\d.]+)',
    }.items():
        m = re.search(pat, text, re.DOTALL)
        if m:
            r[key] = _f(m.group(1))

    return r


def _parse_sina_val(text: str, *patterns: str) -> Optional[float]:
    """从新浪财务报表提取数值（单位：万元 → 转换为元）。"""
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            raw = m.group(1).replace(",", "").strip()
            val = _f(raw)
            if val is not None:
                return val * 10000  # 新浪报表单位是万元
    return None


def _sina_income(code: str) -> Dict[str, Any]:
    """新浪财经 — 利润表（单位已转为元）。"""
    r: Dict[str, Any] = {}
    text = _http_get(
        f"https://money.finance.sina.com.cn/corp/go.php/vFD_ProfitStatement/stockid/{code}/ctrl/part/displaytype/4.phtml",
        timeout=10,
    )
    if not text:
        return r

    r["revenue"] = _parse_sina_val(text, r'一、营业总收入.*?<td[^>]*>([\d.,]+)')
    r["operating_cost"] = _parse_sina_val(text, r'二、营业总成本.*?<td[^>]*>([\d.,]+)')
    r["net_profit"] = _parse_sina_val(text, r'净利润.*?<td[^>]*>([\d.,]+)')
    r["operating_profit"] = _parse_sina_val(text, r'三、营业利润.*?<td[^>]*>([\d.,]+)')
    r["non_recurring_net_profit"] = _parse_sina_val(text, r'扣除非经常性损益后的净利润.*?<td[^>]*>([\d.,]+)')

    if r.get("revenue") and r.get("operating_cost"):
        r["gross_profit"] = r["revenue"] - r["operating_cost"]

    return r


def _sina_balance(code: str) -> Dict[str, Any]:
    """新浪财经 — 资产负债表（单位已转为元）。"""
    r: Dict[str, Any] = {}
    text = _http_get(
        f"https://money.finance.sina.com.cn/corp/go.php/vFD_BalanceSheet/stockid/{code}/ctrl/part/displaytype/4.phtml",
        timeout=10,
    )
    if not text:
        return r

    r["total_assets"] = _parse_sina_val(text, r'资产总计.*?<td[^>]*>([\d.,]+)')
    r["total_liabilities"] = _parse_sina_val(text, r'负债合计.*?<td[^>]*>([\d.,]+)')
    r["current_assets"] = _parse_sina_val(text, r'流动资产合计.*?<td[^>]*>([\d.,]+)')
    r["current_liabilities"] = _parse_sina_val(text, r'流动负债合计.*?<td[^>]*>([\d.,]+)')
    r["total_equity"] = _parse_sina_val(text, r'所有者权益.*?合计.*?<td[^>]*>([\d.,]+)')

    if not r.get("total_equity") and r.get("total_assets") and r.get("total_liabilities"):
        r["total_equity"] = r["total_assets"] - r["total_liabilities"]

    return r


def _sina_cashflow(code: str) -> Dict[str, Any]:
    """新浪财经 — 现金流量表（单位已转为元）。"""
    r: Dict[str, Any] = {}
    text = _http_get(
        f"https://money.finance.sina.com.cn/corp/go.php/vFD_CashFlow/stockid/{code}/ctrl/part/displaytype/4.phtml",
        timeout=10,
    )
    if not text:
        return r

    r["cf_operating"] = _parse_sina_val(text, r'经营活动产生.*?现金流量净额.*?<td[^>]*>([\d.,]+)')
    r["cf_investing"] = _parse_sina_val(text, r'投资活动产生.*?现金流量净额.*?<td[^>]*>([\d.,]+)')
    r["cf_financing"] = _parse_sina_val(text, r'筹资活动产生.*?现金流量净额.*?<td[^>]*>([\d.,]+)')

    return r


def _sina_top10(code: str) -> Dict[str, Any]:
    """新浪财经 — 十大流通股东 + 基金持仓。"""
    r: Dict[str, Any] = {}
    shareholders: List[Dict[str, Any]] = []

    # ── 十大流通股东 ──
    text = _http_get(
        f"https://money.finance.sina.com.cn/corp/go.php/vCI_CirculateStockHolder/stockid/{code}/displaytype/4.phtml",
        timeout=10,
    )
    if text:
        # 表头: 编号 | 股东名称 | 持股数量(股) | 占流通股比例(%) | 股本性质 | 持股市值
        # td 内可能有 <div align="center"> 包装
        rows = re.findall(
            r"<tr[^>]*>\s*<td[^>]*>(?:<[^>]*>)*\d+(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([^<]+)(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([\d,]+)(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([\d.]+)(?:<[^>]*>)*</td>",
            text,
        )
        for row in rows:
            name = row[0].strip()
            if not name or name == "股东名称":
                continue
            shares = _f(row[1].replace(",", ""))
            pct = _f(row[2])
            shareholders.append({"name": name, "shares": shares, "pct": pct})
            if len(shareholders) >= 10:
                break

    if shareholders:
        r["top10_shareholders"] = shareholders

    # ── 基金持仓 ──
    fund_text = _http_get(
        f"https://money.finance.sina.com.cn/corp/go.php/vCI_FundStockHolder/stockid/{code}/displaytype/4.phtml",
        timeout=10,
    )
    if fund_text:
        funds: List[Dict[str, Any]] = []
        # 表头: 基金名称 | 基金代码 | 持仓数量(股) | 占流通股比例(%) | 持股市值 | 占净值比例
        fund_rows = re.findall(
            r"<tr[^>]*>\s*<td[^>]*>(?:<[^>]*>)*([^<]+)(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([^<]+)(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([\d,]+)(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([\d.]+)(?:<[^>]*>)*</td>",
            fund_text,
        )
        for row in fund_rows:
            name = row[0].strip()
            code_str = row[1].strip()
            if not name or name in ("基金名称", "基金简称") or not code_str.replace(",", "").isdigit():
                continue
            funds.append({
                "name": name,
                "code": code_str,
                "shares": _f(row[2].replace(",", "")),
                "pct": _f(row[3]),
            })
        if funds:
            r["fund_holdings"] = funds

    return r


def fetch_sina(code: str) -> Dict[str, Any]:
    """新浪财经 — 聚合。"""
    r: Dict[str, Any] = {"stock_code": code}
    _merge(r, _sina_realtime(code))
    _merge(r, _sina_finance(code))
    _merge(r, _sina_income(code))
    _merge(r, _sina_balance(code))
    _merge(r, _sina_cashflow(code))
    _merge(r, _sina_top10(code))
    return r


# ═════════════════════════════════════════════════════════════════════════════
# 腾讯财经 — 数据拉取
# ═════════════════════════════════════════════════════════════════════════════

def _tencent_realtime(code: str) -> Dict[str, Any]:
    """腾讯实时行情：价格、市值、PE、PB。"""
    r: Dict[str, Any] = {}
    sym = _tencent_sym(code)
    if not sym:
        return r

    text = _http_get(f"https://qt.gtimg.cn/q={sym}", encoding="gbk", timeout=8)
    if not text or "=" not in text:
        return r

    m = re.search(r'"([^"]*)"', text)
    if not m:
        return r
    f = m.group(1).split("~")
    if len(f) < 50:
        return r

    r["name"] = f[1] if len(f) > 1 else ""
    r["price"] = _f(f[3]) if len(f) > 3 else None
    r["pe_ratio"] = _f(f[39]) if len(f) > 39 else None
    r["pb_ratio"] = _f(f[46]) if len(f) > 46 else None
    r["market_cap"] = _f(f[45]) if len(f) > 45 else None
    r["float_market_cap"] = _f(f[44]) if len(f) > 44 else None
    r["eps"] = _f(f[38]) if len(f) > 38 else None

    # 市值单位是"亿"，转为元
    if r.get("market_cap"):
        r["market_cap"] *= 1e8
    if r.get("float_market_cap"):
        r["float_market_cap"] *= 1e8

    return r


def _tencent_company(code: str) -> Dict[str, Any]:
    """腾讯 — 公司概况 + 行业 + 上市日期 + 财务指标。"""
    r: Dict[str, Any] = {}

    # 公司资料页
    market = _detect_market(code)
    url = f"https://stock.finance.qq.com/corp1/{market.lower()}{code}.php"
    text = _http_get(url, timeout=10)
    if not text:
        return r

    for key, pats in {
        "industry": [r'所属行业[：:]\s*(?:</?\w[^>]*>)?([^<]+)', r'行业[：:]\s*(?:</?\w[^>]*>)?([^<]+)'],
        "list_date": [r'上市日期[：:]\s*(?:</?\w[^>]*>)?([^<]+)', r'上市时间[：:]\s*(?:</?\w[^>]*>)?([^<]+)'],
        "main_business": [r'主营业务[：:]\s*(?:</?\w[^>]*>)?([^<]+)'],
        "eps": [r'每股收益[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
        "bvps": [r'每股净资产[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
        "roe": [r'净资产收益率[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
        "gross_margin": [r'毛利率[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
        "net_margin": [r'净利率[：:]\s*(?:</?\w[^>]*>)?([\d.]+)', r'销售净利率[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
        "debt_ratio": [r'资产负债率[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
        "current_ratio": [r'流动比率[：:]\s*(?:</?\w[^>]*>)?([\d.]+)'],
    }.items():
        for pat in pats:
            m = re.search(pat, text)
            if m:
                val = m.group(1).strip()
                if val and val != "--":
                    if key in ("industry", "list_date", "main_business"):
                        r[key] = val
                    else:
                        r[key] = _f(val)
                break

    # 财务数据（利润表/资产负债表/现金流）
    for data_type, field_map in {
        # 利润表
        "income": {
            "revenue": [r'营业收入[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "net_profit": [r'净利润[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "non_recurring_net_profit": [r'扣非净利润[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
        },
        # 资产负债表
        "balance": {
            "total_assets": [r'总资产[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "total_liabilities": [r'总负债[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "total_equity": [r'净资产[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "current_assets": [r'流动资产[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "current_liabilities": [r'流动负债[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
        },
        # 现金流
        "cashflow": {
            "cf_operating": [r'经营活动现金流[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "cf_investing": [r'投资活动现金流[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
            "cf_financing": [r'筹资活动现金流[：:]\s*(?:</?\w[^>]*>)?([\d.,]+)'],
        },
    }.items():
        for key, pats in field_map.items():
            for pat in pats:
                m = re.search(pat, text)
                if m:
                    r[key] = _f(m.group(1).replace(",", ""))
                    break

    return r


def _tencent_shareholders(code: str) -> Dict[str, Any]:
    """腾讯 — 十大流通股东（备用源）。"""
    r: Dict[str, Any] = {}
    market = _detect_market(code)
    text = _http_get(f"https://stock.finance.qq.com/corp1/{market.lower()}{code}.php", timeout=10)
    if not text:
        return r

    shareholders: List[Dict[str, Any]] = []
    # 匹配股东表格：编号 | 股东名称 | 持股数 | 比例
    section = re.search(r'流通股东.*?(?=</table>)', text, re.DOTALL)
    if section:
        rows = re.findall(
            r"<tr[^>]*>\s*<td[^>]*>(?:<[^>]*>)*\d+(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([^<]+)(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([\d,]+)(?:<[^>]*>)*</td>\s*"
            r"<td[^>]*>(?:<[^>]*>)*([\d.]+)(?:<[^>]*>)*</td>",
            section.group(0),
        )
        for row in rows:
            name = row[0].strip()
            if not name or name in ("股东名称", "持股股东"):
                continue
            shareholders.append({
                "name": name,
                "shares": _f(row[1].replace(",", "")),
                "pct": _f(row[2]),
            })
            if len(shareholders) >= 10:
                break

    if shareholders:
        r["top10_shareholders"] = shareholders

    return r


def fetch_tencent(code: str) -> Dict[str, Any]:
    """腾讯 — 聚合。"""
    r: Dict[str, Any] = {"stock_code": code}
    _merge(r, _tencent_realtime(code))
    _merge(r, _tencent_company(code))
    _merge(r, _tencent_shareholders(code))
    return r


# ═════════════════════════════════════════════════════════════════════════════
# 杜邦分析（自动计算）
# ═════════════════════════════════════════════════════════════════════════════

def _dupont(data: dict) -> None:
    """从已有数据计算杜邦三因子，就地写入 data。"""
    net_profit = data.get("net_profit")
    revenue = data.get("revenue")
    total_assets = data.get("total_assets")
    total_equity = data.get("total_equity")

    # 净利率 = 净利润 / 营业收入 (%)
    if net_profit and revenue and revenue != 0:
        data["dupont_net_margin"] = round(net_profit / revenue * 100, 2)

    # 总资产周转率 = 营业收入 / 总资产
    if revenue and total_assets and total_assets != 0:
        data["dupont_asset_turnover"] = round(revenue / total_assets, 4)

    # 权益乘数 = 总资产 / 净资产
    if total_assets and total_equity and total_equity != 0:
        data["dupont_equity_multiplier"] = round(total_assets / total_equity, 4)

    # ROE = 净利率(%) × 周转率 × 权益乘数 / 100
    nm = data.get("dupont_net_margin")
    at = data.get("dupont_asset_turnover")
    em = data.get("dupont_equity_multiplier")
    if nm is not None and at is not None and em is not None:
        data["dupont_roe"] = round(nm / 100 * at * em * 100, 2)


# ═════════════════════════════════════════════════════════════════════════════
# 聚合入口
# ═════════════════════════════════════════════════════════════════════════════

def get_cn_stock_info(code: str) -> Dict[str, Any]:
    """获取 A 股个股全面基本面数据（双源互补）。

    Args:
        code: 6 位数字股票代码

    Returns:
        统一格式字典，不可用字段为 None。至少包含 stock_code 和 source。
    """
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return {"stock_code": code, "error": f"无效的股票代码: {code}"}

    market = _detect_market(code)
    if not market:
        return {"stock_code": code, "error": f"无法识别交易所: {code}"}

    # ── 双源拉取 ──
    sina = {}
    tencent = {}
    try:
        sina = fetch_sina(code)
    except Exception as e:
        logger.debug("fetch_sina(%s) failed: %s", code, e)
    try:
        tencent = fetch_tencent(code)
    except Exception as e:
        logger.debug("fetch_tencent(%s) failed: %s", code, e)

    # ── 合并：新浪优先，腾讯补空 ──
    result: Dict[str, Any] = {"stock_code": code}
    result.update(sina)
    _merge(result, tencent)

    # ── 从市值/价格反推股本 ──
    if not result.get("total_shares") and result.get("market_cap") and result.get("price"):
        try:
            result["total_shares"] = round(result["market_cap"] / result["price"])
        except (ZeroDivisionError, TypeError):
            pass
    if not result.get("circ_shares") and result.get("float_market_cap") and result.get("price"):
        try:
            result["circ_shares"] = round(result["float_market_cap"] / result["price"])
        except (ZeroDivisionError, TypeError):
            pass

    # ── 杜邦分析 ──
    _dupont(result)

    # ── 数据源标记 ──
    sources = []
    if sina and not sina.get("error"):
        sources.append("sina")
    if tencent and not tencent.get("error"):
        sources.append("tencent")
    result["source"] = "+".join(sources) if sources else "none"

    if not any(result.get(k) for k in ("name", "pe_ratio", "market_cap", "industry")):
        result["error"] = f"两个源均未能获取 {code} 的基本面数据"

    return result


# ═════════════════════════════════════════════════════════════════════════════
# CLI 测试
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    codes = sys.argv[1:] if len(sys.argv) > 1 else ["600519"]
    for c in codes:
        print(f"\n{'='*60}\n  {c}\n{'='*60}")
        info = get_cn_stock_info(c)
        for k, v in sorted(info.items()):
            if isinstance(v, list):
                print(f"  {k}: [{len(v)} items]")
                for item in v[:3]:
                    print(f"    {item}")
            elif isinstance(v, float) and abs(v) > 1e6:
                print(f"  {k}: {v/1e8:.2f}亿")
            else:
                print(f"  {k}: {v}")
