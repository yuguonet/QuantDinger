# -*- coding: utf-8 -*-
"""F25 run 阶段状态机守卫（2026-10-17）。

背景：`_preselect_prerun`（一次性 bool）+ `reset_run_activation` + service finally
清标记，三者构成**隐式时序合同**，跨 `qd_preselect.py` / `qd_agent.py` /
`qd_service.py` 三文件互相约定，无一处集中声明。`_run_inner` 无 try/finally，
清理只在 `qd_service._run_sync` 的 finally 里兜底。

本文件锁：把隐式 bool 升级为**显式阶段机**
    idle → primed（service 预跑成功）→ running（`_run_inner` 已消费）→ idle（收尾/兜底）
且保持三个既有 API 不变（`_preselect_prerun` 属性 / `_preselect_prerun_done()`），
⇒ 调用点零改动、`qd_agent.py` 零增行。

stand：`PreselectMixin + ToolFaceMixin`，不建真 agent（`_preselect_prerun_done`
只读 phase，无需 config/model）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_AGENT = BACKEND / "app" / "agent" / "qd_agent.py"


def _stand() -> object:
    from app.agent.qd_preselect import PreselectMixin
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(PreselectMixin, ToolFaceMixin):
        pass

    return _S()


# ── ① 完整迁移链 ────────────────────────────────────────────────────────

def test_phase_transition_chain():
    """idle → primed（写 True）→ running（done 消费）→ idle（写 False）。"""
    s = _stand()
    assert s._qd_run_phase == "idle", "新建 ⇒ idle"

    s._preselect_prerun = True               # 等价 qd_preselect.py:193
    assert s._qd_run_phase == "primed"

    assert s._preselect_prerun_done() is True
    assert s._qd_run_phase == "running", "done 消费后进入 running"

    s._preselect_prerun = False              # 等价 qd_service.py:797
    assert s._qd_run_phase == "idle"


# ── ② done 仅从 primed 返 True ──────────────────────────────────────────

def test_done_true_only_from_primed():
    """idle 时消费 ⇒ False 且不抛（原实现的语义）。"""
    s = _stand()
    assert s._qd_run_phase == "idle"
    assert s._preselect_prerun_done() is False


# ── ③ 旧 bool 是派生的只读视图 ──────────────────────────────────────────

def test_legacy_bool_is_derived_view():
    """`_preselect_prerun` 与 `_qd_run_phase` 双向一致（防两个真源漂移）。"""
    s = _stand()
    s._preselect_prerun = True
    assert getattr(s, "_preselect_prerun") is True and s._qd_run_phase == "primed"
    s._preselect_prerun = False
    assert getattr(s, "_preselect_prerun") is False and s._qd_run_phase == "idle"


# ── ④ 重复消费告警不抛 ──────────────────────────────────────────────────

def test_double_consume_warns_not_raises(caplog):
    """连读两次：第二次返 False 且产生 warning（不抛，fail-loud 记痕）。"""
    s = _stand()
    s._preselect_prerun = True
    assert s._preselect_prerun_done() is True
    with caplog.at_level(logging.WARNING):
        assert s._preselect_prerun_done() is False
    assert any("running" in r.getMessage() or "phase" in r.getMessage().lower()
               for r in caplog.records), caplog.text


# ── ⑤ service finally 兜底等价 ──────────────────────────────────────────

def test_service_finally_clears_to_idle():
    """模拟 service finally 的 `agent._preselect_prerun = False` ⇒ phase 回 idle。"""
    s = _stand()
    s._preselect_prerun = True
    s._preselect_prerun_done()               # running
    s._preselect_prerun = False              # qd_service.py:797
    assert s._qd_run_phase == "idle"


# ── ⑥ qd_agent 行数守卫（F25 必须零增行）───────────────────────────────

def test_qd_agent_line_guard():
    n = QD_AGENT.read_text(encoding="utf-8").count("\n") + 1
    assert n <= 420, f"qd_agent.py {n} 行 > 420（F25 应零增行）"
