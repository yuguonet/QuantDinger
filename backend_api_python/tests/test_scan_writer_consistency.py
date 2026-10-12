"""test_scan_writer_consistency.py — §13.1 A-3 writer 收口门禁（2026-10-12）。

背景：行源三 writer（record / scan_day / scan）并存 = 多源判定风险 —— 文档曾对
`scan_writer` 现值自相矛盾（一说 scan_day、一说 record），文档内部漂移本身就是
多源风险的证据。A-3 收口：**单一生产主线 = record**；scan_day/scan 降级为对拍/回滚位。

判据（不做静默降级）：
  1. writer 全集与主线常量存在且自洽（WRITER_MAINLINE ∈ WRITERS）。
  2. **config 现值 = 生产主线** —— 翻到 scan_day/scan 是显式对拍/回滚动作，
     必须让门禁红一次（“临时回滚忘了翻回来”不能静默变成常态生产配置）。
  3. 缺键/非法值的缺省解析 = 主线（`scan_writer()` 的 fallback 与常量同源）。

易错点：
  - 本门禁**故意**对 config 现值硬断言：它就是 A-3 的「writer 一致性断言」。
    回滚/对拍时翻 config 后本测试会 FAIL —— 那是提醒，不是误报；用完请翻回。
  - 别用 importorskip：静默 skip = 假绿。
"""

from __future__ import annotations

import json
import os

from app.market_cn.auto.strategies import WRITER_MAINLINE, WRITERS, scan_writer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(_ROOT, "app", "market_cn", "auto", "config.json")


def test_writer_set_and_mainline_are_self_consistent():
    """writer 全集固定、主线在全集内且唯一（三选一收口前的登记状态）。"""
    assert WRITERS == ("scan_day", "record", "scan"), (
        "writer 全集变了: %r —— 收口/增删 writer 必须同步本门禁与文档 §6.2/§8" % (WRITERS,))
    assert WRITER_MAINLINE in WRITERS, "主线 %r 不在全集 %r" % (WRITER_MAINLINE, WRITERS)


def test_config_scan_writer_is_production_mainline():
    """config 现值必须 = 生产主线（A-3 writer 一致性断言）。

    scan_day/scan 已降级为对拍/回滚位；config 停在非主线 = 多源判定风险显式化 ——
    门禁红一次逼人确认「这是有意回滚/对拍，不是忘了翻回来」。
    """
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    v = cfg.get("scan_writer")
    assert v == WRITER_MAINLINE, (
        "config.json scan_writer=%r ≠ 生产主线 %r —— 非主线 writer 只允许对拍/临时回滚，"
        "用完必须翻回（§13.1 A-3）" % (v, WRITER_MAINLINE))


def test_scan_writer_fallback_is_mainline():
    """缺键/非法值的缺省解析 = 主线（fallback 不得悄悄指向对拍位）。"""
    import app.market_cn.auto.strategies as strat_reg
    orig = strat_reg.load_config
    try:
        strat_reg.load_config = lambda: {}
        assert scan_writer() == WRITER_MAINLINE
        strat_reg.load_config = lambda: {"scan_writer": "garbage"}
        assert scan_writer() == WRITER_MAINLINE
    finally:
        strat_reg.load_config = orig
