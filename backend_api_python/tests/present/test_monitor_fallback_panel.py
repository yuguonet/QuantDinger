"""test_monitor_fallback_panel.py — §13.1 A-4 回退计数观测面板门禁（2026-10-12）。

背景：`monitor_progress` 拿不到 progress 就回退旧三决策 —— 新判定源坏了**没人知道**，
系统照常跑、还以为正常。A-4 要求（只观测不改行为）：
  1. 回退计数落 `tmp/traces/panel.jsonl`（一行/tick）；
  2. 回退率超阈告警（阈值 `_PROG_FALLBACK_RATE_ALERT`，此处钉语义不钉日志文案）；
  3. `monitor_progress.enabled=false` 时**计数显式归零** —— 「关着」与「坏了」可区分。

易错点：
  - 不测日志文案（会漂），测**计数与 JSONL 记录**这两个事实源；
  - `enabled=false` 的归零必须连 stats 带记录都归零，只改一处就是假区分。
"""

from __future__ import annotations

import json
import os

from app.market_cn.auto import monitor


def _rec(path):
    lines = [ln for ln in open(path, encoding="utf-8").read().splitlines() if ln.strip()]
    assert lines, "panel.jsonl 无记录（回退计数没落盘）"
    return json.loads(lines[-1])


def test_panel_writes_jsonl_and_rate(tmp_path):
    """hit/miss/err 计数 → JSONL 一行 + fallback_rate 计算正确。"""
    p = str(tmp_path / "panel.jsonl")
    stats = {"confirm_prog_hit": 3, "confirm_prog_miss": 1, "confirm_prog_err": 0,
             "no_judgment": 1}
    rec = monitor._panel_observe(stats, "2026-10-12", "15:01", path=p, enabled=True)
    assert rec["prog_enabled"] is True
    assert (rec["confirm_prog_hit"], rec["confirm_prog_miss"], rec["confirm_prog_err"]) == (3, 1, 0)
    assert rec["fallback_rate"] == round(1 / 4, 4)
    assert os.path.exists(p), "panel.jsonl 未写盘"
    assert _rec(p)["fallback_rate"] == rec["fallback_rate"]


def test_panel_disabled_zeroes_counters(tmp_path):
    """enabled=false ⇒ 计数**显式归零**（「关着」≠「坏了」，stats 与记录同归零）。"""
    p = str(tmp_path / "panel.jsonl")
    stats = {"confirm_prog_hit": 5, "confirm_prog_miss": 4, "confirm_prog_err": 2,
             "no_judgment": 7}
    rec = monitor._panel_observe(stats, "2026-10-12", "15:01", path=p, enabled=False)
    assert rec["prog_enabled"] is False
    for k in ("confirm_prog_hit", "confirm_prog_miss", "confirm_prog_err", "no_judgment"):
        assert rec[k] == 0, "%s 未归零: %r" % (k, rec[k])
        assert stats[k] == 0, "stats[%s] 未归零: %r —— 只改记录不改 stats 是假区分" % (k, stats[k])
    assert rec["fallback_rate"] == 0.0


def test_panel_rate_counts_errors_as_fallback(tmp_path):
    """err 也算回退（新判定源坏了就是在回退）—— rate=(miss+err)/total。

    ★ 2026-10-13 改判据（测试自身算错，非代码问题）：
      `monitor._panel_observe` 的分母是 `total = hit + miss + err`（monitor.py:483），
      即 **err 本身也是一次已发生的判定尝试**，要计入分母。
      本例 hit=8 / miss=0 / err=2 ⇒ total=10 ⇒ rate=(0+2)/10=0.2。
      原断言写成 `round(2/8, 4)`=0.25，把 8（hit）误当成了 total，
      与本函数 docstring 自述的 `(miss+err)/total` 口径自相矛盾。
      代码口径正确（err 计入分母才有"每 10 次里 2 次回退"的含义），改测试不改代码。
    """
    p = str(tmp_path / "panel.jsonl")
    stats = {"confirm_prog_hit": 8, "confirm_prog_miss": 0, "confirm_prog_err": 2}
    rec = monitor._panel_observe(stats, "2026-10-12", "15:01", path=p, enabled=True)
    assert rec["fallback_rate"] == round((0 + 2) / (8 + 0 + 2), 4)   # = 0.2
    assert stats["prog_fallback_rate"] == rec["fallback_rate"]


def test_panel_write_failure_does_not_break_tick(tmp_path):
    """观测通道不得反噬生产 tick —— 写失败只 WARN, 不抛。"""
    bad = str(tmp_path / "no_such_dir" / "x" / "panel.jsonl")
    # 传一个无法创建的路径（父级是个文件）⇒ makedirs 失败也不许抛
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    rec = monitor._panel_observe({}, "2026-10-12", "15:01",
                                 path=str(blocker / "sub" / "panel.jsonl"), enabled=True)
    assert rec["date"] == "2026-10-12"
    assert bad  # 防未使用告警
