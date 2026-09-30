# -*- coding: utf-8 -*-
"""事实权威层测试（2026-09-30 提智批，AGENT_DESIGN §8.1）。"""
import sys
from datetime import date
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
sys.path.insert(0, str(AGENT_DIR))

from utils.facts import build_facts, detect_fact_conflict, render_facts_block  # noqa: E402


def test_build_and_render():
    facts = build_facts()
    assert facts["ref_date"] and facts["weekday"].startswith("周")
    block = render_facts_block(facts)
    assert "【时间口径·权威" in block
    assert facts["ref_date"] in block
    assert "不得改写" in block


def test_conflict_detected():
    facts = {"ref_date": "2026-09-30"}
    hits = detect_fact_conflict("今天是2026-10-01，市场高开。", facts)
    assert hits and "2026-10-01" in hits[0]
    assert detect_fact_conflict("今日 2026年10月1日 收盘", facts)
    assert detect_fact_conflict("当前日期: 2026-10-01", facts)


def test_no_false_positive():
    facts = {"ref_date": "2026-09-30"}
    assert not detect_fact_conflict("今天是2026-09-30，市场平稳。", facts)
    assert not detect_fact_conflict("截至 2026-09-28 收盘，区间涨幅 5%。", facts)  # 窗口边界非今日断言
    assert not detect_fact_conflict("目标价 12.5 元，止损 11 元。", facts)
    assert not detect_fact_conflict("", facts)
