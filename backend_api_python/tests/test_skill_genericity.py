# -*- coding: utf-8 -*-
"""技能通用性回归测试（2026-09-30，技能通用性修复配套）。

背景：chain_name（domain+verb+noun）历史上把股票代码写进了 noun 槽
（stock+analyze+600929），酿造按链名聚合 → 每只股票各酿一个技能
（skills/auto_stock-analyze-600929），违背 AGENT_DESIGN §3.16 泛化纪律。

三道防线各配断言：
  A. 链名归一（domain_registry.normalize_chain_name / strip_entity）
  B. 技能目录名与 auto_ 技能正文不得绑定六位股票代码
  C. 酿造通用性质量门（skill_brewer._find_entity_codes）能抓违规产物
"""
import re
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
sys.path.insert(0, str(AGENT_DIR))

from domain_registry import normalize_chain_name, strip_entity  # noqa: E402

_CODE6 = re.compile(r"(?<!\d)\d{6}(?!\d)")


# ── A. 链名归一 ─────────────────────────────────────────────

def test_normalize_strips_stock_code_from_chain():
    assert normalize_chain_name("stock+analyze+600929") == "stock+analyze"
    assert normalize_chain_name("stock+analyze+300497") == "stock+analyze"


def test_normalize_merges_same_shape_chains():
    assert (normalize_chain_name("stock+analyze+600929")
            == normalize_chain_name("stock+analyze+300497"))


def test_normalize_keeps_generic_chain():
    assert normalize_chain_name("finance+analysis+stock") == "finance+analysis+stock"
    assert normalize_chain_name("finance+query+unknown") == "finance+query+unknown"


def test_normalize_idempotent_and_empty():
    once = normalize_chain_name("stock+analyze+600929")
    assert normalize_chain_name(once) == once
    assert normalize_chain_name("") == ""
    assert normalize_chain_name("++") == ""


def test_strip_entity():
    assert strip_entity("600929") == ""
    assert strip_entity("stock") == "stock"
    assert strip_entity("2026") == ""
    assert strip_entity(".sh") == ""            # 市场后缀残留一并清（600519.SH 用例）


# ── B. 技能存量不绑定标的 ──────────────────────────────────

def _skill_dirs():
    skills = AGENT_DIR / "skills"
    return [d for d in sorted(skills.iterdir())
            if d.is_dir() and not d.name.startswith("_") and d.name != "__pycache__"]


def test_no_skill_dir_name_binds_stock_code():
    for d in _skill_dirs():
        assert not _CODE6.search(d.name), f"技能目录名绑定股票代码: {d.name}"


def test_no_auto_skill_body_binds_stock_code():
    for d in _skill_dirs():
        if not d.name.startswith("auto_"):
            continue
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)   # 日期
        text = re.sub(r"root_id=\d+", "", text)         # 溯源 id
        codes = _CODE6.findall(text)
        assert not codes, f"{d.name} 正文绑定股票代码: {codes[:5]}"


# ── C. 酿造质量门可用 ─────────────────────────────────────

def test_brew_gate_detects_entity_codes():
    from chain.skill_brewer import _find_entity_codes
    bad = "codes=\"600929\" 调用工具分析"
    assert _find_entity_codes(bad) == ["600929"]
    clean = ("<!-- auto-brewed 2026-09-29 from root_id=992 chain=stock+analyze -->\n"
             "对目标股票调用工具，codes=\"目标代码\"，回看 120 天。")
    assert _find_entity_codes(clean) == []


def test_brew_gate_detects_sample_name():
    """名称级泛化检查（2026-09-30 提智）：写“贵州茅台”同样绑定单股。"""
    from chain.skill_brewer import _generic_violation
    assert any(r.startswith("name:")
               for r in _generic_violation("分析贵州茅台的目标价", "贵州茅台"))
    assert not _generic_violation("对目标股票做多空研判", "贵州茅台")
    assert not _generic_violation("对目标股票做多空研判", "")
