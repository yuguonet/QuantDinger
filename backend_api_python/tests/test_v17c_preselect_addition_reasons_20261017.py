# -*- coding: utf-8 -*-
r"""V17c：预选补充工具**带原因进工具地图**守卫（2026-10-17，审计 T3）。

★ 背景（tmp/llm_four_dimension_audit_20261017.md）
  lint 覆盖补位（R1：检测到任务涉及某数据域但 LLM 没点该域工具 ⇒ 高置信补位）
  会把工具加进已激活列表，但旧版工具地图只列名字。实测后果：预选
  deliverables 明写"估值快照（PE/市值/换手率）"，lint 也正确补了
  `batch_valuation_compare`（返回 pe_ttm/pb/mcap_yi），但模型不知道这个
  工具"为什么在"，全程没调，最终报告自列"未获取 PE/PB/流通市值"——
  **本可避免的自伤**。

★ 修复
  `qd_preselect.preselect_additions(report)` 从 lint.coverage 的 auto_added
  条目派生 {工具: 域中文标签}，qd_agent 每 run 刷新，`_tool_map_text` 在
  已激活段给这些工具加"（预选补充：任务涉及XX）"后缀。

本文件钉死：① 派生函数只认 auto_added ② 地图渲染带原因 ③ 无补充时零附加。
"""
from __future__ import annotations

from app.agent.qd_preselect import preselect_additions, PRESELECT_DOMAIN_CN  # noqa: E402
from app.agent.qd_tool_face import ToolFaceMixin  # noqa: E402

# 中文标签 → 域键（测试夹具用，避免把域键写死在用例里）
_REVERSE_CN = {v: k for k, v in PRESELECT_DOMAIN_CN.items()}


def test_additions_only_from_auto_added():
    report = {
        "lint": {
            "coverage": [
                {"domain": "valuation", "tool": "batch_valuation_compare",
                 "auto_added": True},
                {"domain": "kline", "tool": "agent_get_kline",
                 "auto_added": False},          # 模型自己点的：不算补充
            ],
            "additions": ["batch_valuation_compare"],
        }
    }
    got = preselect_additions(report)
    assert got == {"batch_valuation_compare": "估值/基本面"}


def test_additions_empty_when_no_lint():
    assert preselect_additions({}) == {}
    assert preselect_additions({"lint": {}}) == {}
    assert preselect_additions({"lint": {"coverage": []}}) == {}


def test_additions_unknown_domain_falls_back():
    report = {"lint": {"coverage": [
        {"domain": "some_new_domain", "tool": "x_tool", "auto_added": True}]}}
    assert preselect_additions(report) == {"x_tool": "some_new_domain"}


def _make_face(activated, additions):
    """造一个最小 ToolFaceMixin 实例：activated = 已激活工具，
    additions 以 last_preselect_report 形式喂入（与生产同路径）。"""
    face = ToolFaceMixin.__new__(ToolFaceMixin)
    face._on_demand_names = []
    face._core_names = ["bash"]
    face._activated = list(activated)
    coverage = [{"domain": _REVERSE_CN.get(reason, reason), "tool": tool,
                 "auto_added": True}
                for tool, reason in (additions or {}).items()]
    face.last_preselect_report = {"lint": {"coverage": coverage}} if coverage else {}
    return face


def test_tool_map_annotates_additions():
    face = _make_face(["batch_valuation_compare", "get_realtime_quote"],
                      {"batch_valuation_compare": "估值/基本面"})
    text = face._tool_map_text()
    assert "batch_valuation_compare（预选补充：任务涉及估值/基本面）" in text
    # 模型自己点名的工具不带后缀（不制造噪声）
    assert "get_realtime_quote（" not in text


def test_tool_map_no_annotation_without_additions():
    face = _make_face(["get_realtime_quote"], {})
    text = face._tool_map_text()
    assert "预选补充" not in text
    assert "get_realtime_quote" in text
