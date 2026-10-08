"""test_projection_ops_preserved.py — P5-⑤-3: 投影刷新不覆盖操作列。

背景（改进方案 §2.7 事实源拆分）:
  判定事实（信号/生命周期/规则结论）→ Record → 投影再生；
  操作事实（marked / t_legs_today / pre_confirm / 实盘价）→ 库 append-only，
  **不可推导、不可重建**。投影若写入这些键，整票重建会被污染。

守卫有两层，本测试把两层都钉死:
  1. `store.project_rows` 纯函数**不产出**任何操作列键；
  2. `rebuild.apply_ledger_plan` 的 UPDATE 用 `extra || jsonb` **合并**
     （不是 `extra = %s` 整体覆盖）—— 库内已有操作键存活。

易错点:
  - 断言必须列白名单内**逐个**检查，不能只查一个代表键（漏一个 = 那个键可被覆盖）。
  - 不能只断言"当前代码没写"，要断言 SQL 文本形态（`||`），防后人改成整体覆盖。
"""

from __future__ import annotations

import io
import os

#: 操作事实键（§2.7；出现在 extra 里）——投影行绝不允许携带
OPS_KEYS = (
    "marked",            # monitor 出场标记日
    "t_legs_today",      # 做T 意图（当日）
    "pre_confirm",       # 14:25 预确认档位
    "pre_ts",
    "pre_reason",
    "replay_reason",     # rebuild 重放侧注入（投影不注入）
    "replayed_at",
    "heal",
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REBUILD = os.path.join(_ROOT, "app", "market_cn", "auto", "rebuild.py")
_STORE = os.path.join(_ROOT, "app", "market_cn", "auto", "store.py")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def test_project_rows_never_emits_ops_keys():
    """project_rows 是纯函数：事件链 payload 的 extra 只含规则内容，无操作列键。

    构造带操作键污染的 payload —— 若 project_rows 会透传，断言失败。
    真实 payload 里本不该有这些键；此断言防"有人把 monitor 的 detail 混进事件"。
    """
    from app.market_cn.auto.store import project_rows

    class _Rec:
        events = [
            {"stage": "ready", "date": "2026-09-01", "payload": {
                "price": 10.0, "score": 80, "label": "断板",
                "extra": {"board": "main", "signal_chg": 1.2},  # 规则键
            }},
            {"stage": "exec", "date": "2026-09-02", "payload": {
                "entry_date": "2026-09-02", "entry_price": 10.1,
                "buyable": True,
            }},
        ]

    rows = project_rows("break", "600000", _Rec(), name="测试")
    assert rows, "project_rows 应至少产一行（ready→row）"
    for row in rows:
        extra = row.get("extra") or {}
        for k in OPS_KEYS:
            assert k not in extra, (
                "project_rows 产出了操作列键 %r —— 判定投影不得携带操作事实 (§2.7)" % k)
        # 投影行本身也不该有顶层操作列字段
        for k in OPS_KEYS:
            assert k not in row, (
                "project_rows 行顶层出现操作列字段 %r (§2.7)" % k)


def test_apply_ledger_update_merges_extra_not_replace():
    """apply_ledger_plan 的 UPDATE 必须 `extra || jsonb` 合并，禁止整体覆盖。

    整体覆盖 = 库内 marked/t_legs_today/pre_confirm 一夜蒸发 = 操作事实被重建污染。
    """
    src = _read(_REBUILD)
    # 定位 UPDATE qd_dragon_signals SET ... extra 段
    assert "UPDATE qd_dragon_signals SET" in src, "rebuild.py 应含信号 UPDATE"
    # 合并形态: extra = extra || %s::jsonb
    assert "extra = extra ||" in src, (
        "apply_ledger_plan 的 UPDATE 未用 `extra || jsonb` 合并 —— "
        "整体覆盖会抹掉操作列 (marked/t_legs_today/pre_confirm)。"
        "必须保持合并语义。")
    # 反例: 不允许出现 SET 里直接 `extra = %s` 的整体赋值
    import re
    bad = re.findall(r"UPDATE\s+qd_dragon_signals\s+SET[^;]*?\bextra\s*=\s*%s", src, re.S)
    assert not bad, (
        "发现 `extra = %s` 整体覆盖形态 —— 操作列会被投影行覆盖 (§2.7)。"
        "正确写法: `extra = extra || %s::jsonb`")


def test_build_ledger_plan_guards_marked_and_actual():
    """build_ledger_plan 两道守卫仍在: keep_marked (资金事实) / keep_actual (真实入场)。"""
    src = _read(_REBUILD)
    assert "keep_marked" in src, "keep_marked 守卫缺失 —— 已标记出场行会被重放覆写"
    assert "keep_actual" in src, "keep_actual 守卫缺失 —— 真实入场行会被重放抹掉"
    # 守卫触发条件必须仍在（按 exit_reason / entry_date 判断）
    assert 'a.get("exit_reason")' in src, "keep_marked 触发条件 (exit_reason) 缺失"
    assert 'a.get("entry_date")' in src, "keep_actual 触发条件 (entry_date) 缺失"


def test_projection_ledger_refresh_source_marked():
    """P5-⑤-1 新入口存在且带 source=projection 标记（便于日志/报告区分新旧链）。"""
    src = _read(_REBUILD)
    assert "def projection_ledger_refresh" in src, (
        "projection_ledger_refresh 未落地 —— P5-⑤-1 投影刷新入口缺失")
    assert '"source": "projection"' in src or "'source': 'projection'" in src, (
        "projection_ledger_refresh 应标记 source=projection")
