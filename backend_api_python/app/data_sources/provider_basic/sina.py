# -*- coding: utf-8 -*-
"""provider_basic / sina —— 基本面族新浪源（仅 cn_financial_report）。

`stock_basic` 已于 2026-10-28 摘除（全量分页 22~23s 超该 kind 的 8s 预算，
且名称被截断成 6 字），实现与实测证据见 del/provider_basic_sina_stock_basic_20261028/。

- `cn_financial_report`：新浪三表页面（vFD_BalanceSheet / vFD_ProfitStatement）
  的**绝对金额 + 报告期**，单位**万元**（页面表头明写「单位：万元」）。

⚠ `cn_financial_report` 与 `cn_financial` **是两个契约，不可互相替代**：
  `cn_financial` 给比率指标（增长率 / 利润率 / 资产负债率 / ROE / 每股现金流，来自
  同花顺摘要）；本能力给三大报表原始科目的绝对值（总资产 / 负债 / 净资产 / 货币资金 /
  营收 / 净利）+ `report_date`。历史上两者都由外围各自直拉（`app/utils/cn_stock_info.py`
  与 `app/agent/tools/finance/data_tools.py`），2026-10-28 Phase 4 把这条新浪源上移到这里，
  外围改调 `coord_basic("cn_financial_report")`。
"""
from __future__ import annotations

from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

# 三表页面（displaytype/4 = 完整报表视图，不是摘要）
_REPORT_URL = ("https://money.finance.sina.com.cn/corp/go.php/{path}/stockid/{code}"
               "/ctrl/part/displaytype/4.phtml")
_REPORT_BS = "vFD_BalanceSheet"
_REPORT_PS = "vFD_ProfitStatement"


# ── cn_financial_report：三大报表原始科目（万元口径）────────────────

def _report_table(code_num: str, path: str):
    """抓新浪三表页面 → 数据表 DataFrame（行数最多的那张）。

    页面上除数据表外都是 2~5 行的导航小表，故取 `shape[0]` 最大者。
    单元格是带千分位逗号的字符串（object dtype），数值化交给 `to_number`。
    """
    from io import StringIO

    import pandas as pd
    import requests

    r = requests.get(_REPORT_URL.format(path=path, code=code_num),
                     headers=dict(_HTTP_HEADERS), timeout=15)
    r.encoding = "gbk"
    dfs = pd.read_html(StringIO(r.text))
    return max(dfs, key=lambda d: d.shape[0]) if dfs else None


def _latest_cell(df, keywords, exclude=None):
    """取最新报告期（列 1）中首个匹配关键词的原始单元格；无匹配 → None。

    只取列 1（最新期）：历史期同理可用，但 Agent 只需要最新一期摘要。
    """
    if df is None or df.shape[1] < 2:
        return None
    exclude = exclude or []
    for _, row in df.iterrows():
        item_name = str(row.iloc[0])
        if any(k in item_name for k in keywords) and not any(x in item_name for x in exclude):
            return row.iloc[1]     # NaN 由 to_number 归一为 None
    return None


def fetch_cn_financial_report(code: str) -> Dict[str, Any]:
    """三大报表关键科目摘要（新浪 资产负债表 + 利润表，**万元**口径）。

    参数:
        code: A 股代码，接受 "600519" / "600519.SH" / "sh600519"

    返回: {"object": {code, report_date, financial_unit,
                      total_assets_wan, total_liabilities_wan, equity_wan,
                      cash_wan, revenue_wan, net_profit_wan}}

    ⚠ 键名即口径：所有金额键带 `_wan` 后缀，值是页面原值（万元），**不做换算**。
      单位另给 `financial_unit="万元"`，缺报告期时 `report_date` 为 None
      —— 不带报告期的金额同样是裸数字。

    失败语义: 两张表**都**抓不到 ⇒ raise（由 coord 记录原因）；
    页面存在但六个科目全 None（未知代码/退市，新浪给的是自选股挂件页）⇒ 也 raise；
    只有一张表成功 ⇒ 返回部分字段（缺的键为 None），不伪造 0。
    """
    from app.data_sources.normalizer import strip_market_prefix, to_number

    code_num = strip_market_prefix(code or "")
    if not (len(code_num) == 6 and code_num.isdigit()):
        raise ValueError("cn_financial_report 需要 6 位 A 股代码，收到 %r" % (code,))

    df_bs = df_ps = None
    errors = []
    for attr, path in (("bs", _REPORT_BS), ("ps", _REPORT_PS)):
        try:
            df = _report_table(code_num, path)
        except Exception as e:
            errors.append("%s: %s" % (path, e))
            logger.warning("[sina] %s %s 抓取失败: %s", code_num, path, e)
            df = None
        if attr == "bs":
            df_bs = df
        else:
            df_ps = df

    if df_bs is None and df_ps is None:
        raise RuntimeError("新浪三大报表均取数失败（%s）" % "; ".join(errors))

    obj: Dict[str, Any] = {
        "code": code_num,
        # 资产负债表
        "total_assets_wan": to_number(_latest_cell(df_bs, ["资产总计", "总资产"])),
        "total_liabilities_wan": to_number(_latest_cell(df_bs, ["负债合计", "总负债"])),
        "equity_wan": to_number(
            _latest_cell(df_bs, ["股东权益合计", "所有者权益合计", "归属母公司"])),
        "cash_wan": to_number(_latest_cell(df_bs, ["货币资金"])),
        # 利润表
        "revenue_wan": to_number(_latest_cell(df_ps, ["营业总收入", "营业收入"])),
        "net_profit_wan": to_number(_latest_cell(df_ps, ["净利润"], exclude=["扣非"])),
    }

    # 未知代码/退市：新浪返回的是"自选股"挂件页，`max(shape[0])` 选中的不是报表，
    # 于是六个科目全 None。这不是"合法为空"（真实公司必有资产总计），必须报失败
    # —— 否则 `is_object` 会判成功，Agent 拿到 8 个 None 还以为是"数据缺失"。
    money_keys = ("total_assets_wan", "total_liabilities_wan", "equity_wan",
                  "cash_wan", "revenue_wan", "net_profit_wan")
    if all(obj[k] is None for k in money_keys):
        raise RuntimeError(
            "新浪未返回 %s 的报表数据（两张表均无匹配科目，可能代码错误或已退市）" % code_num)

    # 报告期：第 0 行是日期行，列 1 = 最新期（实测 600519 → 2026-06-30）
    report_date = None
    for df in (df_bs, df_ps):
        if df is not None and df.shape[1] >= 2:
            try:
                report_date = str(df.iloc[0, 1])[:10]
                break
            except Exception:
                continue
    obj["report_date"] = report_date
    obj["financial_unit"] = "万元"
    return {"object": obj}


from app.data_sources.provider_basic import register  # noqa: E402


@register
class SinaBasicProvider:
    name = "sina"
    priority = 10
    markets = {"CNStock"}
    capabilities = {
        "cn_financial_report": fetch_cn_financial_report,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
