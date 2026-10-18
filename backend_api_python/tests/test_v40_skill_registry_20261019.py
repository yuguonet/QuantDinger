# -*- coding: utf-8 -*-
"""阶段 1 守卫：skill registry / 版本化 / 生命周期 / 验证门禁 / 事务性发布（2026-10-19）。

对应 `tmp/skill_system_rework_implementation_plan_20261018.md`：
  §4.1 正文存储与稳定身份（skill_id + 内容摘要版本 ID，SemVer 仅显示，§15.3）
  §4.2 自动生成状态机 generated→candidate→validated→active（+rejected/superseded/disabled/archived）
  §4.3 容量（§15.1：QD_SKILL_AUTO_MAX 默认 10，v1 无家族配额）、去重、软退役
  §7   生成验证关卡（格式/契约/通用性/重复/事务性发布/验证报告）
  §14.5 稳定身份与显示名归一（resolve_skill_id/display_name/version）

纪律：TDD 先红后绿；DB 访问经 skill_registry._db 注入假连接（conftest 已 mock psycopg2，
真库探针另跑）；阈值全部走 env（QD_SKILL_AUTO_MAX / QD_SKILL_DUP_JACCARD）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agent.chain import skill_registry as sr


# ═══════════════════════════════════════════════════════════════
# 一、稳定身份：skill_id 归一 + 内容摘要版本 ID（§4.1 / §15.3）
# ═══════════════════════════════════════════════════════════════

def test_normalize_skill_id_kebab():
    assert sr.normalize_skill_id("auto_Finance Analysis") == "auto-finance-analysis"
    assert sr.normalize_skill_id("Auto--Stock__Analyze") == "auto-stock-analyze"
    assert sr.normalize_skill_id("  market-screener  ") == "market-screener"
    assert sr.normalize_skill_id("") == ""


def test_content_digest_deterministic_and_normalized():
    """同一内容（CRLF/BOM/行尾空白差异）⇒ 同一版本 ID（§15.3 规范化规则固定并测试）。"""
    a = "---\nname: x\ndescription: d\n---\n\nbody line\n"
    b = "\ufeff---\r\nname: x\r\ndescription: d\r\n---\r\n\r\nbody line   \r\n"
    assert sr.content_digest(a) == sr.content_digest(b)
    assert len(sr.content_digest(a)) == 16          # 版本 ID 用 16 hex（可读性）


def test_content_digest_changes_with_content():
    a = "---\nname: x\ndescription: d\n---\nbody v1\n"
    b = "---\nname: x\ndescription: d\n---\nbody v2\n"
    assert sr.content_digest(a) != sr.content_digest(b)


def test_is_auto_managed():
    assert sr.is_auto_managed("auto-finance-query-stock")
    assert not sr.is_auto_managed("market-screener")
    assert not sr.is_auto_managed("strategy-debug")


# ═══════════════════════════════════════════════════════════════
# 二、生命周期状态机（§4.2）
# ═══════════════════════════════════════════════════════════════

def test_lifecycle_legal_transitions():
    assert sr.can_transition("generated", "candidate")
    assert sr.can_transition("candidate", "validated")
    assert sr.can_transition("validated", "active")
    assert sr.can_transition("candidate", "rejected")
    assert sr.can_transition("validated", "rejected")
    assert sr.can_transition("active", "superseded")
    assert sr.can_transition("active", "disabled")
    assert sr.can_transition("active", "archived")
    assert sr.can_transition("disabled", "active")     # 恢复须重新过门禁（调用方保证）
    assert sr.can_transition("archived", "active")


def test_lifecycle_illegal_transitions():
    assert not sr.can_transition("active", "generated")
    assert not sr.can_transition("rejected", "active")  # 拒绝不得直接复活
    assert not sr.can_transition("generated", "active")  # 不得跳过验证
    assert not sr.can_transition("archived", "candidate")
    assert not sr.can_transition("whatever", "active")   # 未知状态一律拒


# ═══════════════════════════════════════════════════════════════
# 三、容量配置（§4.3 / §15.1：默认 10，env 可调，非法回落）
# ═══════════════════════════════════════════════════════════════

def test_auto_skill_cap_default(monkeypatch):
    monkeypatch.delenv("QD_SKILL_AUTO_MAX", raising=False)
    assert sr.auto_skill_cap() == 10


def test_auto_skill_cap_env_override(monkeypatch):
    monkeypatch.setenv("QD_SKILL_AUTO_MAX", "3")
    assert sr.auto_skill_cap() == 3


def test_auto_skill_cap_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("QD_SKILL_AUTO_MAX", "abc")
    assert sr.auto_skill_cap() == 10
    monkeypatch.setenv("QD_SKILL_AUTO_MAX", "-2")
    assert sr.auto_skill_cap() == 0        # 负值夹紧到 0（容量关闭），不回落默认


# ═══════════════════════════════════════════════════════════════
# 四、分面解析（§5.1：可选元数据，缺失允许）
# ═══════════════════════════════════════════════════════════════

def test_parse_facets_optional_and_normalized():
    meta = {"metadata": {"facets": '{"domain_family": "finance", "object_type": "个股"}'}}
    f = sr.parse_facets(meta)
    assert f == {"domain_family": "finance", "object_type": "个股"}


def test_parse_facets_missing_ok():
    assert sr.parse_facets({"name": "x"}) == {}
    assert sr.parse_facets({}) == {}


# ═══════════════════════════════════════════════════════════════
# 五、验证门禁（§7）
# ═══════════════════════════════════════════════════════════════

_GOOD_DOC = """---
name: auto-test-skill
description: 测试技能：对 A 股目标股票开展技术面综合诊断，输出短期走势参考结论。
metadata:
  version: 0.1.0
  tools: agent_get_kline, technical_analysis
---

# 测试技能
按步骤执行诊断。
"""


def _gate(doc, **kw):
    kw.setdefault("skill_id", "auto-test-skill")
    kw.setdefault("provider_tools", {"agent_get_kline", "technical_analysis", "get_realtime_quote"})
    return sr.validate_gates(doc, **kw)


def test_gate_format_missing_name_fails():
    doc = "---\ndescription: 只有描述\n---\nbody\n"
    g = _gate(doc)
    assert not g.ok and g.status == "bad_format"
    assert any("name" in i for i in g.issues)


def test_gate_format_missing_description_fails():
    doc = "---\nname: auto-x\n---\nbody\n"
    g = _gate(doc)
    assert not g.ok and g.status == "bad_format"


def test_gate_format_illegal_name_charset_fails():
    doc = "---\nname: Auto_X\ndescription: 描述\n---\nbody\n"
    g = _gate(doc)
    assert not g.ok and g.status == "bad_format"


def test_gate_format_description_too_long_fails():
    doc = "---\nname: auto-x\ndescription: " + "长" * 1100 + "\n---\nbody\n"
    g = _gate(doc)
    assert not g.ok and g.status == "bad_format"


def test_gate_clean_doc_passes():
    g = _gate(_GOOD_DOC)
    assert g.ok, g.issues
    assert g.status == "validated"
    assert g.stripped_tools == []


def test_gate_contract_strips_unknown_tools_and_records():
    """未知工具名剔除（既有 _validate_tools 语义保留）+ 报告留痕，不硬失败。"""
    doc = ("---\nname: auto-x\ndescription: 描述\n"
           "metadata:\n  tools: agent_get_kline, fake_tool_xyz\n---\nbody\n")
    g = _gate(doc)
    assert g.ok, g.issues
    assert g.stripped_tools == ["fake_tool_xyz"]
    assert "agent_get_kline" in g.skmd


def test_gate_contract_meta_tools_stripped():
    """元工具（list_tools 等）声明即噪音，剔除并留痕。"""
    doc = ("---\nname: auto-x\ndescription: 描述\n"
           "metadata:\n  tools: agent_get_kline, list_tools\n---\nbody\n")
    g = _gate(doc)
    assert g.ok
    assert "list_tools" in g.stripped_tools


def test_gate_genericity_entity_code_fails():
    doc = _GOOD_DOC + "\n对 600519 执行诊断。\n"
    g = _gate(doc)
    assert not g.ok and g.status == "not_generic"
    assert any("600519" in i for i in g.issues)


def test_gate_genericity_sample_name_fails():
    doc = _GOOD_DOC + "\n对贵州茅台执行诊断。\n"
    g = _gate(doc, sample_name="贵州茅台")
    assert not g.ok and g.status == "not_generic"


def test_gate_near_duplicate_by_name():
    active = [{"name": "auto-test-skill", "description": "随便什么"}]
    g = _gate(_GOOD_DOC, active_infos=active)
    assert not g.ok and g.status == "duplicate"


def test_gate_near_duplicate_by_description_jaccard(monkeypatch):
    monkeypatch.setenv("QD_SKILL_DUP_JACCARD", "0.5")
    d = "对 A 股目标股票开展技术面综合诊断输出短期走势参考结论适用于分析诊断场景"
    active = [{"name": "auto-other", "description": d}]
    doc = ("---\nname: auto-other\ndescription: " + d + "\n---\nbody\n")
    g = _gate(doc, active_infos=active)
    assert not g.ok and g.status == "duplicate"


def test_gate_near_duplicate_disabled_at_zero(monkeypatch):
    monkeypatch.setenv("QD_SKILL_DUP_JACCARD", "0")
    d = "对 A 股目标股票开展技术面综合诊断输出短期走势参考结论"
    active = [{"name": "auto-other", "description": d}]
    doc = ("---\nname: auto-other\ndescription: " + d + "\n---\nbody\n")
    g = _gate(doc, active_infos=active)
    assert g.ok


def test_gate_report_has_verifier_and_duration():
    g = _gate(_GOOD_DOC)
    assert g.report["verifier"] == sr.VALIDATOR_VERSION
    assert "duration_ms" in g.report and g.report["duration_ms"] >= 0
    assert g.report["skill_id"] == "auto-test-skill"


# ═══════════════════════════════════════════════════════════════
# 六、事务性发布（§7.6：失败不留半发布状态）
# ═══════════════════════════════════════════════════════════════

def test_publish_skill_doc_atomic_and_no_staging_left(tmp_path):
    target = tmp_path / "SKILL.md"
    sr.publish_skill_doc(target, "---\nname: x\n---\nbody\n")
    assert target.read_text(encoding="utf-8").endswith("body\n")
    assert not (tmp_path / "SKILL.md.staging").exists()
    # 覆盖发布同样原子
    sr.publish_skill_doc(target, "---\nname: x\n---\nbody v2\n")
    assert "body v2" in target.read_text(encoding="utf-8")
    assert not (tmp_path / "SKILL.md.staging").exists()


# ═══════════════════════════════════════════════════════════════
# 七、registry DB 薄层（假连接注入；fail-soft 语义）
# ═══════════════════════════════════════════════════════════════

class _FakeCur:
    def __init__(self, rows=None, one=None):
        self._rows, self._one = rows or [], one
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_register_version_writes_row(monkeypatch):
    cur = _FakeCur(one={"skill_id": "auto-x"})
    conn = _FakeConn(cur)
    monkeypatch.setattr(sr, "_db", lambda: conn)
    out = sr.register_version(
        skill_id="auto-x", version="abc123", description="d",
        facets={}, declared_tools=["t"], body_uri="/x/SKILL.md",
        validation_state="validated", validation_report={"ok": True},
        source_chain="stock+analyze", semver="0.1.0")
    assert out is True
    assert conn.committed
    assert any("qd_skill_versions" in sql for sql, _ in cur.executed)


def test_register_version_db_failure_fail_soft(monkeypatch):
    """DB 不可用 ⇒ False（不抛）——发布不因登记失败而回滚已写文件。"""
    def _boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(sr, "_db", _boom)
    assert sr.register_version(skill_id="auto-x", version="v", description="",
                               facets={}, declared_tools=[], body_uri="",
                               validation_state="validated", validation_report={},
                               source_chain="", semver="") is False


def test_count_auto_managed(monkeypatch):
    cur = _FakeCur(one={"n": 5})
    monkeypatch.setattr(sr, "_db", lambda: _FakeConn(cur))
    assert sr.count_auto_managed() == 5


def test_count_auto_managed_db_failure_returns_none(monkeypatch):
    monkeypatch.setattr(sr, "_db", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert sr.count_auto_managed() is None


def test_capacity_available(monkeypatch):
    monkeypatch.setattr(sr, "count_auto_managed", lambda: 10)
    monkeypatch.setenv("QD_SKILL_AUTO_MAX", "10")
    assert sr.capacity_available() is False
    monkeypatch.setattr(sr, "count_auto_managed", lambda: 9)
    assert sr.capacity_available() is True
    # 计数失败（None）⇒ 保守放行（宁松勿卡，与 run_lock 同哲学）
    monkeypatch.setattr(sr, "count_auto_managed", lambda: None)
    assert sr.capacity_available() is True


# ═══════════════════════════════════════════════════════════════
# 八、解析统一入口（§14.5：resolve_skill_id / display_name / version）
# ═══════════════════════════════════════════════════════════════

def test_resolve_skill_id_uses_adapter_alias(monkeypatch):
    class _Info:
        name = "market-screener"

    class _Ad:
        def get(self, name):
            return _Info() if name in ("market-screener", "market_screener") else None

    assert sr.resolve_skill_id(_Ad(), "market_screener") == "market-screener"
    assert sr.resolve_skill_id(_Ad(), "unknown-name") == "unknown-name"   # 未知原样返回


def test_resolve_skill_id_without_adapter():
    assert sr.resolve_skill_id(None, "auto-x") == "auto-x"
