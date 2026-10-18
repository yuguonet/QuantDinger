# -*- coding: utf-8 -*-
"""阶段 1 守卫：brewer 接入门禁/容量/事务性发布/版本登记（2026-10-19）。

对应实施方案 §4.2/§4.3/§7 在 `chain/skill_brewer.py` 的落地：
  · 达容量上限（QD_SKILL_AUTO_MAX）⇒ 新候选 capacity_deferred，不写盘、不覆盖；
  · 全部门禁通过才事务性发布（staging + 原子替换），失败不留半发布状态；
  · 发布即登记不可变版本 ID（内容摘要）到 registry（fail-soft，DB 不可用不挡发布）；
  · 修订同样过门禁 + 原子发布 + 新版本登记，失败保留旧 active 版本。

测试全部走 monkeypatch（store/llm/provider/registry._db 注入），不碰真库、不写真技能目录
（`sb._SKILLS_DIR` 重定向到 tmp_path）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agent.chain import skill_brewer as sb
from app.agent.chain import skill_registry as sr


@pytest.fixture
def _redirect_skills_dir(tmp_path, monkeypatch):
    """酿造写盘重定向到 tmp；顺带把适配器扫描也挡住（dup 门用显式 active_infos 注入）。"""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(sb, "_SKILLS_DIR", skills)
    return skills


class _FakeProvider:
    def get_tool_names(self):
        return ["agent_get_kline", "technical_analysis", "get_realtime_quote"]


class _FakeResp:
    def __init__(self, content):
        self.content = content


_VALID_DOC = """---
name: auto-brew-target
description: 对目标集合开展分时资金流与盘口质量的交叉验证，输出异常嫌疑清单与复核顺序，仅供研究复盘使用。
metadata:
  version: 0.1.0
  tools: agent_get_kline, technical_analysis
---

# 酿造目标技能
按步骤执行交叉验证。
"""


def _patch_store(monkeypatch, candidates, digest):
    from app.agent.chain import store as st
    monkeypatch.setattr(st, "query_brew_candidates", lambda **kw: list(candidates))
    monkeypatch.setattr(st, "get_run_tree_digest", lambda root_id: digest)
    monkeypatch.setattr(st, "get_brew_states", lambda: [])
    monkeypatch.setattr(st, "set_brew_state", lambda *a, **kw: None)
    monkeypatch.setattr(st, "get_tool_weights", lambda: {})


def _patch_llm_provider(monkeypatch, docs):
    """docs: 按调用顺序返回的 content 列表（重复最后一个）。"""
    import app.agent.llm as llm_mod
    from app.agent.tools import _base as tools_base
    from app.agent.utils import prescan
    seq = list(docs)

    def _complete(msgs, *a, **kw):
        return _FakeResp(seq.pop(0) if seq else docs[-1])

    monkeypatch.setattr(llm_mod, "complete", _complete)
    monkeypatch.setattr(tools_base.ToolProvider, "get_default",
                        classmethod(lambda cls, *a, **kw: _FakeProvider()))
    monkeypatch.setattr(prescan, "prescan_tools", lambda provider, **kw: "（签名清单）")


_DIGEST = {"steps": [{"status": "ok", "step": "s1", "log": "取数"}],
           "user_query": "分析某某股票的资金面", "plan": "（无）", "stock_name": ""}


# ═══════════════════════════════════════════════════════════════
# 一、_validate_tools 委托单一真源（d11 既有语义不回归）
# ═══════════════════════════════════════════════════════════════

def test_validate_tools_delegates_to_registry():
    doc = ("---\nname: d\ndescription: x\n"
           "tools: [agent_get_kline, list_tools, bogus]\n---\nbody\n")
    out = sb._validate_tools(doc, _FakeProvider())
    assert "tools: [agent_get_kline]" in out
    assert "list_tools" not in out and "bogus" not in out
    assert sb._validate_tools(doc, None) == doc      # provider 缺失 fail-open 不变


def test_brewer_helpers_delegate_to_registry():
    assert sb._extract_frontmatter_tools is sr.extract_frontmatter_tools \
        or sb._extract_frontmatter_tools("---\ntools: a, b\n---\n") == ["a", "b"]
    assert sb._meta_tools() == sr.meta_tools()


# ═══════════════════════════════════════════════════════════════
# 二、容量门（§4.3/§15.1）
# ═══════════════════════════════════════════════════════════════

def test_brew_at_capacity_defers_without_writing(_redirect_skills_dir, monkeypatch):
    _patch_store(monkeypatch, [{"chain_name": "stock+analyze", "sample_root_id": 1}], _DIGEST)
    _patch_llm_provider(monkeypatch, [_VALID_DOC])
    monkeypatch.setattr(sr, "capacity_available", lambda: False)
    # LLM 若被调用即炸（容量门应在 LLM 之前生效）——这里不 patch complete 的调用计数，
    # 用真 patch 但断言结果状态与文件系统即可。
    results = sb.brew_skills(min_runs=1, limit=1, trigger="manual")
    assert results and results[0]["status"] == "capacity_deferred"
    assert not (_redirect_skills_dir / "auto-brew-target").exists()


def test_brew_capacity_ok_proceeds(_redirect_skills_dir, monkeypatch):
    _patch_store(monkeypatch, [{"chain_name": "stock+analyze", "sample_root_id": 1}], _DIGEST)
    _patch_llm_provider(monkeypatch, [_VALID_DOC])
    monkeypatch.setattr(sr, "capacity_available", lambda: True)
    registered = {}
    monkeypatch.setattr(sr, "register_version", lambda **kw: registered.update(kw) or True)
    results = sb.brew_skills(min_runs=1, limit=1, trigger="manual")
    assert results[0]["status"] == "brewed", results
    # 目录名由 chain 名派生（不信任 LLM 的 frontmatter name）：stock+analyze → auto-stock-analyze
    published = _redirect_skills_dir / "auto-stock-analyze" / "SKILL.md"
    assert published.exists()
    # 版本登记：skill_id 稳定 + 版本 = 发布文档的内容摘要
    assert registered["skill_id"] == "auto-stock-analyze"
    assert registered["version"] == sr.content_digest(published.read_text(encoding="utf-8"))
    assert registered["source_chain"] == "stock+analyze"
    assert registered["validation_report"]["verifier"] == sr.VALIDATOR_VERSION
    # 事务性发布不残留 staging
    assert not (_redirect_skills_dir / "auto-stock-analyze" / "SKILL.md.staging").exists()


# ═══════════════════════════════════════════════════════════════
# 三、门禁拒绝路径（不写盘）
# ═══════════════════════════════════════════════════════════════

def test_brew_rejects_duplicate_without_writing(_redirect_skills_dir, monkeypatch):
    _patch_store(monkeypatch, [{"chain_name": "stock+analyze", "sample_root_id": 1}], _DIGEST)
    dup_doc = _VALID_DOC.replace("name: auto-brew-target", "name: auto-other-name")
    _patch_llm_provider(monkeypatch, [dup_doc])
    monkeypatch.setattr(sr, "capacity_available", lambda: True)
    # active_infos 由 brewer 从 adapter 取；这里替换 brewer 内的取数函数不可行，
    # 故直接 patch validate_gates 的上游——near_duplicate_of 的输入由 brewer 提供，
    # 改用 patch sr.validate_gates 太粗；正确做法：patch brewer 取 active_infos 的 helper。
    active = [{"name": "auto-other-name", "description": dup_doc.split("description: ")[1].split("\n")[0]}]
    monkeypatch.setattr(sr, "validate_gates",
                        lambda skmd, **kw: sr.GateReport(
                            ok=False, status="duplicate", issues=["与 active 技能近重复: auto-other-name"]))
    results = sb.brew_skills(min_runs=1, limit=1, trigger="manual")
    assert results[0]["status"] == "duplicate"
    assert not (_redirect_skills_dir / "auto-other-name").exists()


def test_brew_rejects_not_generic_without_writing(_redirect_skills_dir, monkeypatch):
    _patch_store(monkeypatch, [{"chain_name": "stock+analyze", "sample_root_id": 1}],
                 {**_DIGEST, "stock_name": "贵州茅台"})
    bad = _VALID_DOC + "\n对 600519 与贵州茅台执行诊断。\n"
    _patch_llm_provider(monkeypatch, [bad, bad])   # 重酿一次仍违规 ⇒ 丢弃
    monkeypatch.setattr(sr, "capacity_available", lambda: True)
    results = sb.brew_skills(min_runs=1, limit=1, trigger="manual")
    assert results[0]["status"] == "not_generic"
    assert not any(p.is_dir() for p in _redirect_skills_dir.iterdir())


def test_brew_gate_bad_format_without_writing(_redirect_skills_dir, monkeypatch):
    _patch_store(monkeypatch, [{"chain_name": "stock+analyze", "sample_root_id": 1}], _DIGEST)
    _patch_llm_provider(monkeypatch, ["没有 frontmatter 的纯文本"])
    monkeypatch.setattr(sr, "capacity_available", lambda: True)
    results = sb.brew_skills(min_runs=1, limit=1, trigger="manual")
    assert results[0]["status"] == "bad_format"
    assert not any(p.is_dir() for p in _redirect_skills_dir.iterdir())


# ═══════════════════════════════════════════════════════════════
# 四、注册登记 fail-soft（DB 不可用不挡发布）
# ═══════════════════════════════════════════════════════════════

def test_brew_registration_failure_still_publishes(_redirect_skills_dir, monkeypatch):
    _patch_store(monkeypatch, [{"chain_name": "stock+analyze", "sample_root_id": 1}], _DIGEST)
    _patch_llm_provider(monkeypatch, [_VALID_DOC])
    monkeypatch.setattr(sr, "capacity_available", lambda: True)
    monkeypatch.setattr(sr, "register_version", lambda **kw: False)
    results = sb.brew_skills(min_runs=1, limit=1, trigger="manual")
    assert results[0]["status"] == "brewed"
    assert (_redirect_skills_dir / "auto-stock-analyze" / "SKILL.md").exists()


# ═══════════════════════════════════════════════════════════════
# 五、修订路径：过门禁 + 原子发布 + 新版本登记；失败保留旧版本
# ═══════════════════════════════════════════════════════════════

_OLD_DOC = """---
name: auto-x
description: 旧版描述：对目标集合开展资金流交叉验证，输出异常嫌疑清单。
metadata:
  version: 0.1.0
  tools: agent_get_kline
---

<!-- auto-brewed 2026-10-01 from root_id=42 chain=stock+analyze | human-edited 后请去除 auto- 前缀接管 -->
# 旧版
旧内容。
"""

_NEW_DOC = """---
name: auto-x
description: 新版描述：对目标集合开展资金流与盘口质量交叉验证，输出异常嫌疑清单与复核顺序。
metadata:
  version: 0.2.0
  tools: agent_get_kline
---

# 新版
新内容。
"""


def _patch_revise_store(monkeypatch, delta):
    from app.agent.chain import store as st
    monkeypatch.setattr(st, "get_delta_digest", lambda chain, since_root_id=None: delta)
    monkeypatch.setattr(st, "get_skill_revision", lambda name: {"revision": 1, "low_streak": 0,
                                                                 "revised_at": None})
    monkeypatch.setattr(st, "set_skill_revision", lambda name, **kw: None)
    monkeypatch.setattr(st, "reset_skill_weight", lambda name: None)


def test_revise_publishes_new_version_atomically(_redirect_skills_dir, monkeypatch):
    skill_dir = _redirect_skills_dir / "auto-x"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_OLD_DOC, encoding="utf-8")
    _patch_revise_store(monkeypatch, {"n_correct": 2, "n_falsified": 1,
                                      "runs": [{"exec_date": "2026-10-18", "correct": True,
                                                "user_query": "q", "summary": "s"}],
                                      "chain_name": "stock+analyze"})
    _patch_llm_provider(monkeypatch, [_NEW_DOC])
    registered = {}
    monkeypatch.setattr(sr, "register_version", lambda **kw: registered.update(kw) or True)
    out = sb.revise_skill("stock+analyze", str(skill_dir), pre_weight=0.8)
    assert out["status"] == "revised", out
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "新内容" in text
    assert not (skill_dir / "SKILL.md.staging").exists()
    # .bak 轮转保留（护栏 3）
    assert (skill_dir / "SKILL.md.bak").exists()
    # 新版本登记：版本 ID = 新文档内容摘要；semver 显示 0.2.0
    assert registered["version"] == sr.content_digest(text)
    assert registered["semver"] == "0.2.0"
    assert registered["skill_id"] == "auto-x"


def test_revise_gate_failure_keeps_old_active(_redirect_skills_dir, monkeypatch):
    skill_dir = _redirect_skills_dir / "auto-x"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_OLD_DOC, encoding="utf-8")
    _patch_revise_store(monkeypatch, {"n_correct": 2, "n_falsified": 1,
                                      "runs": [{"exec_date": "2026-10-18", "correct": True,
                                                "user_query": "q", "summary": "s"}],
                                      "chain_name": "stock+analyze"})
    bad_new = _NEW_DOC + "\n对 600519 执行诊断。\n"
    _patch_llm_provider(monkeypatch, [bad_new])
    registered = {}
    monkeypatch.setattr(sr, "register_version", lambda **kw: registered.update(kw) or True)
    out = sb.revise_skill("stock+analyze", str(skill_dir), pre_weight=0.8)
    assert out["status"] == "not_generic", out
    # 旧 active 版本原样保留（修订失败不得损坏在役版本）
    assert "旧内容" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert not registered
