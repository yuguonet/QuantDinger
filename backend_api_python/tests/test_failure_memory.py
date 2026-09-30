# -*- coding: utf-8 -*-
"""失败记忆 · 通用错误指纹测试（2026-09-30 提智批 5，AGENT_DESIGN §8.4 落点）。"""
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
sys.path.insert(0, str(AGENT_DIR))

from utils.failure_memory import FailureMemory, _fingerprint  # noqa: E402


def test_fingerprint_normalizes_volatile_parts():
    a = _fingerprint("KeyError: 'main_net' at line 12")
    b = _fingerprint("KeyError: 'total' at line 87")
    assert a == b


def test_fingerprint_counts_and_warns_at_rhythm():
    fm = FailureMemory()
    for i in range(3):
        fm.record_fingerprint(f"TypeError: unsupported operand at {i}")
    w = fm.consume_fingerprint_warnings()
    assert len(w) == 1 and "3 次" in w[0]
    assert fm.consume_fingerprint_warnings() == []   # 同水位只告警一次
    for i in range(3):
        fm.record_fingerprint(f"TypeError: unsupported operand at {i + 10}")
    w2 = fm.consume_fingerprint_warnings()
    assert len(w2) == 1 and "6 次" in w2[0]


def test_render_with_fp_lines():
    fm = FailureMemory()
    fm.record("wrong_column", "把 list 当 dict 切片")
    txt = fm.render(["同类错误已重复 3 次：KeyError: #"])
    assert "【失败记忆】" in txt and "【重复错误】" in txt
    fm2 = FailureMemory()
    assert fm2.render(["同类错误已重复 3 次：x"]).startswith("【重复错误】")
    assert FailureMemory().render() == ""
