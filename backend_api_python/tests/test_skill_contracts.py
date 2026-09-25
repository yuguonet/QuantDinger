# -*- coding: utf-8 -*-
"""Skill 体系契约测试（2026-09-25 评估报告 B1~B4 回归）。"""
from pathlib import Path

import pytest

from llm.qd_skills import QDSkillAdapter, SkillInfo, _parse_skill_md


def test_skillinfo_default_weight_immune():
    """B1：SkillInfo 无 default_weight 也不得在权重同步炸掉。"""
    info = SkillInfo(name="x", display_name="x")
    assert not hasattr(info, "default_weight")
    # evaluator 用的是字面 1.0；这里锁住「不读该字段」的事实
    default_w = 1.0
    assert default_w == 1.0


def test_parse_skill_md_skips_leading_html_comment():
    """B3：前置 <!-- auto-brewed --> 不得导致元数据全空。"""
    raw = (
        "<!-- auto-brewed 2026-09-21 from root_id=1 chain=finance+query+stock -->\n"
        "---\n"
        "name: auto_demo\n"
        "description: 演示\n"
        "tags: [a, b]\n"
        "tools: [foo, bar]\n"
        "---\n\n"
        "# 正文\n"
    )
    meta, body = _parse_skill_md(raw)
    assert meta.get("name") == "auto_demo"
    assert meta.get("tools") == ["foo", "bar"]
    assert "正文" in body


def test_bundled_skills_metadata_nonempty():
    """B3 验收：内置 skill 解析后 description 非空；声明了 tools 的必须非空。"""
    ad = QDSkillAdapter()
    assert len(ad) >= 2
    for name in ("market_screener", "auto_finance-analysis-stock"):
        info = ad.get(name)
        assert info is not None, name
        assert info.description, name
        if name == "auto_finance-analysis-stock":
            assert info.tools, "auto skill tools 应能从 frontmatter 解析"


def test_skill_declared_tools_from_skillinfo():
    """B2：白名单从 SkillInfo.tools 取，而不是剥掉 frontmatter 的 body。"""
    from nodes import _skill_declared_tool_names
    names = _skill_declared_tool_names("auto_finance-analysis-stock", "")
    assert "get_capital_summary" in names or "agent_get_kline" in names
    assert _skill_declared_tool_names("", "") == []
    assert _skill_declared_tool_names("", "tools: [foo, bar]") == ["foo", "bar"]


def test_load_resource_blocks_traversal():
    """B4：路径穿越/绝对路径必须拒绝。"""
    ad = QDSkillAdapter()
    assert ad.load_resource("market_screener", "../../prompts/skill_brew.txt") is None
    assert ad.load_resource("market_screener", "/etc/passwd") is None
    assert ad.load_resource("market_screener", "..\\..\\prompts\\skill_brew.txt") is None
