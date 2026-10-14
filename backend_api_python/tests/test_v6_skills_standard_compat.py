# -*- coding: utf-8 -*-
"""S3/S4 守卫：SKILL.md frontmatter 对齐 Anthropic Agent Skills 规范。

背景（2026-10-14 用户裁定"只做 metadata+校验，名字不动"）：
  - S3：version/tags/tools 从**顶层**迁入 `metadata:`（规范仅认 name/description
    置顶，其余属 metadata map<string,string>）。适配器读取须**优先 metadata、
    回落顶层**（兼容存量文件）；酿造器读写 tools/version 也要认两种位置。
  - S4：加轻量 frontmatter 校验（name 字符集/长度/是否=目录名、description 长度），
    **fail-open 只告警不阻断**（历史技能存在 name≠目录名、目录含下划线的已知偏差）。
"""
from __future__ import annotations

from app.agent import skills_adapter as sa


# ── S3：metadata 优先 + legacy 回落 ─────────────────────────────
def test_meta_field_prefers_metadata_then_falls_back():
    meta = {"tags": ["legacy"], "metadata": {"tags": "a, b"}}
    assert sa._as_str_list(sa._meta_field(meta, "tags", [])) == ["a", "b"]
    assert sa._as_str_list(sa._meta_field({"tags": ["legacy"]}, "tags", [])) == ["legacy"]
    assert sa._meta_field({}, "tags", ["d"]) == ["d"]


def test_as_str_list_handles_str_list_and_chinese_comma():
    assert sa._as_str_list("a, b,c") == ["a", "b", "c"]
    assert sa._as_str_list("a，b") == ["a", "b"]
    assert sa._as_str_list(["x", " y "]) == ["x", "y"]
    assert sa._as_str_list(None) == []


def test_as_int_falls_back_on_bad_value():
    assert sa._as_int("7", 5) == 7
    assert sa._as_int(None, 5) == 5
    assert sa._as_int("x", 5) == 5


# ── S4：校验器（fail-open，返回告警列表）────────────────────────
def test_validate_frontmatter_flags_violations():
    bad = sa._validate_frontmatter({"name": "Bad_Name", "version": "1.0"}, "other-dir")
    assert any("非法字符" in i for i in bad)
    assert any("不一致" in i for i in bad)
    assert any("非标准顶层字段" in i for i in bad)


def test_validate_frontmatter_clean_doc_has_no_issue():
    ok = sa._validate_frontmatter({"name": "good-name", "description": "d"}, "good-name")
    assert ok == []


# ── 端到端：全量技能迁移后 tags/tools 仍从 metadata 成功解析 ──────
def test_real_skills_parse_from_metadata_and_no_legacy_toplevel():
    adapter = sa.QDSkillAdapter()
    assert len(adapter) >= 6
    ms = adapter.get("market-screener")          # 走 display→name 映射
    assert ms is not None and ms.name == "market-screener"
    assert ms.tags == ["market", "screener", "short_term", "a_share"]
    assert len(ms.tools) == 12
    # 迁移完成后：任何技能都不应再报"非标准顶层字段"
    assert all(
        not any("非标准顶层字段" in i for i in issues)
        for issues in adapter._skill_issues.values()
    )


# ── 酿造/修订提示词：必须携带 Anthropic frontmatter 纪律（S3/S4 文案）──
def test_brew_prompt_and_revise_template_encode_anthropic_frontmatter_contract():
    """锁住 prompts/skill_brew.txt 与 skill_revise.md：
    ① 仅 name/description 置顶、version/tags/tools 进 metadata；
    ② name 字符集/长度/目录一致、description 长度；
    ③ 修订走 prompts/skill_revise.md（外置，不再内联）。
    """
    from pathlib import Path
    from app.agent.prompts import load_prompt
    brew = load_prompt("skill_brew")
    rev = load_prompt("skill_revise")
    for txt, label in ((brew, "brew"), (rev, "revise")):
        assert "metadata:" in txt, label
        assert "name / description" in txt or "name/description" in txt, label
        assert "小写字母/数字/连字符" in txt or "a-z0-9" in txt, label
        assert "1024" in txt, label
        assert "禁止" in txt and "顶层" in txt, label
    assert "{skill_name}" in brew
    assert "{old_doc}" in rev and "{runs_txt}" in rev
    prompts_dir = Path(__file__).resolve().parents[1] / "app" / "agent" / "prompts"
    assert not (prompts_dir / "skill_revise.txt").exists()
    assert (prompts_dir / "skill_revise.md").exists()
    import inspect
    from app.agent.chain import skill_brewer as sb
    src = inspect.getsource(sb.revise_skill)
    assert "_REVISE_TEMPLATE" in src


# ── 提示词外置：模块内不再硬编码长 prompt ─────────────────────────
def test_hardcoded_prompts_are_externalized_to_prompts_dir():
    """tool_preselect / wrapup / evidence / judge / skill_engineer / func_overlap
    的提示词必须来自 prompts/，禁止写回 .py 常量字面量。"""
    from app.agent.prompts import load_prompt, load_prompt_doc
    # 可加载
    assert len(load_prompt("wrapup_summary")) > 50
    assert len(load_prompt("evidence_extract")) > 50
    assert len(load_prompt("verdict_judge")) > 50
    assert len(load_prompt("skill_revise")) > 50
    pre = load_prompt_doc("tool_preselect")
    assert "外部规划器" in pre["system"] and "{query}" in pre["user"]
    eng = load_prompt_doc("skill_engineer")
    assert "技能工程师" in eng["brew_system"]
    fo = load_prompt_doc("func_overlap")
    assert "{pairs}" in fo["judge_pairs"]
    eng = load_prompt_doc("skill_engineer")
    assert "generic_retry_suffix" in eng and "目标股票" in eng["generic_retry_suffix"]
    assert "{reason}" in load_prompt("wrapup_budget_hint")
    # 源码侧：这些模块不得再出现原先的长字面量锚点
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "agent"
    anchors = {
        root / "preselect" / "tool_preselect.py": "工具面标准（两条同时成立",
        root / "qd_agent.py": "请**只依据上面已经拿到的工具输出**",
        root / "chain" / "c3p.py": "禁止输出任何概率、胜率、置信度字段",
        root / "chain" / "resolver.py": "宁可判宽不判严",
        root / "capabilities" / "func_overlap.py": "把握不大一律 false",
        root / "chain" / "skill_brewer.py": "【重申】正文与参数示例禁止",
        root / "utils" / "budget.py": "下面是目前已经取到",
    }
    for path, frag in anchors.items():
        src = path.read_text(encoding="utf-8")
        assert frag not in src, f"{path.name} 仍硬编码提示词片段: {frag[:20]}"


# ── 酿造器：tools/version 读写认 metadata 与 legacy 两种位置 ─────
def test_brewer_reads_and_rewrites_metadata_and_legacy_tools():
    from app.agent.chain import skill_brewer as sb

    class _P:
        def get_tool_names(self):
            return ["get_stock_quote", "search_tools"]

    new = ("---\nname: d\ndescription: x\nmetadata:\n  version: 0.1.0\n"
           "  tools: get_stock_quote, bogus\n---\nbody\n")
    assert sb._extract_frontmatter_tools(new) == ["get_stock_quote", "bogus"]
    out = sb._validate_tools(new, _P())
    assert "  tools: get_stock_quote" in out and "bogus" not in out

    legacy = "---\nname: d\ndescription: x\ntools: [get_stock_quote, bogus]\n---\nbody\n"
    out2 = sb._validate_tools(legacy, _P())
    assert "tools: [get_stock_quote]" in out2, out2   # 保留 legacy 方括号写法
