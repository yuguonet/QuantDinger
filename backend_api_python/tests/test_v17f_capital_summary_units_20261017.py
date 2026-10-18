"""get_capital_summary 金额单位自描述守卫（2026-10-17，审计 O1）。

**背景（一次系统性 ÷100 幻觉）**：600519 复跑报告内部自相矛盾——
结论段写「当前价位 13.29 万元」（真值 1329 元）、K线表 BOLL 上轨「13.13」、
分红「28.06 元/10股」（工具给 2806.172 元/10股），而估值段又写对
「13,329 元」。根因：money 字段是万元口径但键名裸名
（`rz_balance`/`revenue`…），模型猜对 money 后把「万元」**过度泛化**
到价格/派息等非 money 字段。

**修复（与 T2 分红自描述 / volume_unit 同一模式，键名即口径）**：
- `_get_margin_trading`：`rz_balance`→`rz_balance_wan`、
  `rq_balance`→`rq_balance_wan`（万元）
- `_get_financial_statements`：`revenue`→`revenue_wan` 等 6 键（万元）
  + `financial_unit:"万元"` + `report_date`；`_latest_value` 经 `_num`
  数值化（千分位串→float，None 不回落 0）
- `_get_holder_count`：修死字段——上游 `RPT_HOLDERNUMLATEST` 没有
  `AVG_AMOUNT` 列（恒 0.0），真实列是 `AVG_MARKET_CAP`（户均持股市值，
  元）/ `AVG_HOLD_NUM`（户均持股，股）

**实测口径（2026-10-17）**：
- 新浪 vFD_BalanceSheet/vFD_ProfitStatement 页面表头明写「单位：万元」
  （600519：营业总收入 9,227,807.21 = 922.78 亿；资产总计
  30,905,078.46 = 3090.51 亿；列1 = 最新报告期 2026-06-30）
- EM RPTA_WEB_RZRQ_GGMX：RZYE=16,450,780,103 元 → /1e4 = 1645078.0 万元
- EM RPT_HOLDERNUMLATEST：AVG_MARKET_CAP=4,999,794.99 元、
  AVG_HOLD_NUM=4217.49 股

本测试把上述固化为不变量：键名必须带单位后缀、裸 money 键不得复活、
口径 docstring 不得被删（防"好心改回去"）。
"""
import ast
import inspect
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app.agent.tools.finance import data_tools  # noqa: E402
from app.agent.tools.finance.data_tools import (  # noqa: E402
    _get_financial_statements, _get_holder_count, _get_margin_trading, _num,
)

# ── 实测数据（2026-10-17，600519）────────────────────────────────────
_RZRQ_ROWS = [
    {"DATE": "2026-10-16 00:00:00", "RZYE": 16450780103, "RQYE": 274236492},
    {"DATE": "2026-10-15 00:00:00", "RZYE": 16607349199, "RQYE": 267646308.56},
    {"DATE": "2026-10-14 00:00:00", "RZYE": 16580000000, "RQYE": 270000000},
    {"DATE": "2026-10-13 00:00:00", "RZYE": 16500000000, "RQYE": 268000000},
    {"DATE": "2026-10-10 00:00:00", "RZYE": 16000000000, "RQYE": 265000000},
    {"DATE": "2026-10-09 00:00:00", "RZYE": 15950000000, "RQYE": 264000000},
]
_HOLDER_ROWS = [
    {"END_DATE": "2026-06-30 00:00:00", "HOLDER_NUM": 296404,
     "AVG_MARKET_CAP": 4999794.99996454, "AVG_HOLD_NUM": 4217.49234490762,
     "AVG_AMOUNT": 12345.0},  # 上游无此列；若代码误取会露馅
    {"END_DATE": "2026-03-31 00:00:00", "HOLDER_NUM": 243159,
     "AVG_MARKET_CAP": 4800000.0, "AVG_HOLD_NUM": 4000.0},
]

# 新浪三表页面样式：表头「单位：万元」+ 第0行报表日期 + 千分位数字符串
# （pandas 对带逗号数字解析为 str，_num 负责剥逗号转 float）
_PS_HTML = """<html><body><table>
<thead><tr><th colspan="3">贵州茅台(600519) 利润表 单位：万元</th></tr></thead>
<tbody>
<tr><td>报表日期</td><td>2026-06-30</td><td>2026-03-31</td></tr>
<tr><td>一、营业总收入</td><td>9,227,807.21</td><td>5,470,291.24</td></tr>
<tr><td>净利润</td><td>4,603,340.00</td><td>2,600,000.00</td></tr>
</tbody></table></body></html>"""

_BS_HTML = """<html><body><table>
<thead><tr><th colspan="3">贵州茅台(600519) 资产负债表 单位：万元</th></tr></thead>
<tbody>
<tr><td>报表日期</td><td>2026-06-30</td><td>2026-03-31</td></tr>
<tr><td>资产总计</td><td>30,905,078.46</td><td>31,991,884.49</td></tr>
<tr><td>负债合计</td><td>2,000,000.00</td><td>2,100,000.00</td></tr>
<tr><td>归属母公司股东权益合计</td><td>28,000,000.00</td><td>29,000,000.00</td></tr>
<tr><td>货币资金</td><td>10,000,000.00</td><td>9,000,000.00</td></tr>
</tbody></table></body></html>"""


class _FakeResp:
    def __init__(self, text):
        self.text = text
        self.encoding = None


def _patch_em(monkeypatch, rows):
    """em_datacenter 在函数体内 import，按模块属性打桩。"""
    import app.agent.tools.finance._em_utils as em_utils
    monkeypatch.setattr(em_utils, "em_datacenter",
                        lambda *a, **k: list(rows))


# ── AST 键名不变量（防裸名复活；docstring 里出现旧名不算，见 MEMORY 教训）──
def _return_dict_keys(fn_name: str) -> set:
    src = textwrap.dedent(inspect.getsource(getattr(data_tools, fn_name)))
    keys = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant):
                    keys.add(k.value)
    return keys


def _summary_assign_keys(fn_name: str) -> set:
    src = textwrap.dedent(inspect.getsource(getattr(data_tools, fn_name)))
    keys = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                    and t.value.id == "summary"
                    and isinstance(t.slice, ast.Constant)):
                keys.add(t.slice.value)
    return keys


def test_margin_return_keys_carry_wan_suffix():
    keys = _return_dict_keys("_get_margin_trading")
    assert "rz_balance_wan" in keys and "rq_balance_wan" in keys, keys
    assert "rz_balance" not in keys and "rq_balance" not in keys, (
        f"裸名余额键复活了：{sorted(keys)}")
    # ★ 双单位并列（2026-10-17，run8 §3.1）：只给万元键时模型会自己 ÷1e4 再自己
    #   起单位名——实测把「164.51 亿元」写成「164.51 万万元」。亿元键让它照抄。
    assert "rz_balance_yi" in keys and "rq_balance_yi" in keys, (
        f"缺亿元键（键名即口径，模型不该自己做换算）：{sorted(keys)}")
    assert keys <= {"rz_balance_wan", "rz_balance_yi", "rq_balance_wan",
                    "rq_balance_yi", "rz_5d_change_pct", "signal"}, keys


def test_financials_summary_keys_carry_wan_suffix():
    keys = _summary_assign_keys("_get_financial_statements")
    money_wan = {"revenue_wan", "net_profit_wan", "total_assets_wan",
                 "total_liabilities_wan", "equity_wan", "cash_wan"}
    assert money_wan <= keys, f"缺 _wan 键：{sorted(money_wan - keys)}"
    assert "financial_unit" in keys and "report_date" in keys, keys
    bare = {"revenue", "net_profit", "total_assets", "total_liabilities",
            "equity", "cash"}
    assert not (bare & keys), f"裸 money 键复活了：{sorted(bare & keys)}"


# ── 行为测试（monkeypatch 掉上游，锤口径与数值）────────────────────
def test_margin_behavior_wan_unit(monkeypatch):
    _patch_em(monkeypatch, _RZRQ_ROWS)
    r = _get_margin_trading("600519", days=60)
    # RZYE=16,450,780,103 元 → 1645078.0 万元；较 5 日前 160 亿 +2.8%
    assert r["rz_balance_wan"] == 1645078.0, r
    assert r["rq_balance_wan"] == 27423.6, r
    assert r["rz_5d_change_pct"] == 2.8, r
    assert r["signal"] == "融资净流入", r


def test_holder_count_uses_real_em_fields(monkeypatch):
    _patch_em(monkeypatch, _HOLDER_ROWS)
    r = _get_holder_count("600519")
    assert r["holder_count"] == 296404, r
    assert r["change_pct"] == 21.9, r
    assert r["latest_date"] == "2026-06-30", r
    # 真实列：户均持股市值（元）/ 户均持股（股）；死字段不得复活
    assert r["avg_hold_market_cap_rmb"] == 4999795.0, r
    assert r["avg_hold_num_shares"] == 4217.5, r
    assert "avg_amount" not in r, r


def test_holder_count_missing_fields_are_none_not_zero(monkeypatch):
    """null≠0：上游缺列时必须 None，不得回落 0 虚构"零市值"。"""
    _patch_em(monkeypatch, [{"END_DATE": "2026-06-30 00:00:00",
                             "HOLDER_NUM": 296404}])
    r = _get_holder_count("600519")
    assert r["avg_hold_market_cap_rmb"] is None, r
    assert r["avg_hold_num_shares"] is None, r


def test_holder_count_uses_pre_holder_num_for_single_row(monkeypatch):
    """单股查询只返回 1 行：上期数必须取同行 PRE_HOLDER_NUM。

    旧版只认 data[1] ⇒ 单股查询恒取到自己，change_pct 恒 0、
    signal 恒"持平"（把 +21.9% 的筹码分散读成"无变化"）。
    """
    _patch_em(monkeypatch, [{
        "END_DATE": "2026-06-30 00:00:00", "HOLDER_NUM": 296404,
        "PRE_HOLDER_NUM": 243159,
        "AVG_MARKET_CAP": 4999794.99, "AVG_HOLD_NUM": 4217.49,
    }])
    r = _get_holder_count("600519")
    assert r["change_pct"] == 21.9, r
    assert r["signal"] == "筹码分散", r


def test_financials_behavior_wan_unit_and_report_date(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda url, **k: _FakeResp(
                            _PS_HTML if "ProfitStatement" in url else _BS_HTML))
    r = _get_financial_statements("600519")
    # 千分位字符串 → float；值 = 页面原值（万元口径，不做单位换算）
    assert r["revenue_wan"] == 9227807.21, r
    assert r["net_profit_wan"] == 4603340.0, r
    assert r["total_assets_wan"] == 30905078.46, r
    assert r["total_liabilities_wan"] == 2000000.0, r
    assert r["equity_wan"] == 28000000.0, r
    assert r["cash_wan"] == 10000000.0, r
    assert isinstance(r["revenue_wan"], float), r
    assert r["financial_unit"] == "万元", r
    assert r["report_date"] == "2026-06-30", r
    bare = {"revenue", "net_profit", "total_assets", "total_liabilities",
            "equity", "cash"}
    assert not (bare & set(r)), f"裸 money 键复活了：{sorted(bare & set(r))}"


def test_financials_missing_field_is_none(monkeypatch):
    """利润表缺净利润行 → None（不得 0.0 虚构"零利润"）。"""
    import requests
    ps_no_np = _PS_HTML.replace(
        '<tr><td>净利润</td><td>4,603,340.00</td><td>2,600,000.00</td></tr>', "")
    monkeypatch.setattr(requests, "get",
                        lambda url, **k: _FakeResp(
                            ps_no_np if "ProfitStatement" in url else _BS_HTML))
    r = _get_financial_statements("600519")
    assert r["net_profit_wan"] is None, r
    assert r["revenue_wan"] == 9227807.21, r


# ── docstring 防删 + _num 单测 ──────────────────────────────────────
def test_docstrings_still_document_units():
    """口径说明是不变量的一部分：删它的人下一步就会"统一"键名。"""
    cases = [
        ("_get_margin_trading", "万元"),
        ("_get_financial_statements", "单位：万元"),
        ("_get_holder_count", "AVG_MARKET_CAP"),
        ("get_capital_summary", "_wan"),
    ]
    for fn_name, needle in cases:
        doc = inspect.getdoc(getattr(data_tools, fn_name)) or ""
        assert needle in doc, f"{fn_name} 的 docstring 口径说明被删了？"


def test_num_helper_strips_commas_and_preserves_none():
    assert _num("9,227,807.21") == 9227807.21
    assert _num(9227807.21) == 9227807.21
    assert _num(None) is None
    assert _num("") is None
    assert _num(float("nan")) is None
    assert _num("abc") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
