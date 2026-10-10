"""test_ledger_ops_columns.py — P2-12 验收（2026-10-10 数据面独立排期）。

方案原文（审计缺陷修复方案.md §P2-12）：
    构造「库中 entry_price=实价、重放算出理论价」，断言 UPDATE 语句**不含**
    `entry_price`（可测生成的 SQL/参数字典），且 `vanish_settled` 在 error 模式下阻断。

病灶回顾
--------
`apply_ledger_plan` 的 UPDATE 原为**整列覆盖**
`state/entry_date/entry_price/stop_price/exit_date/exit_price/exit_reason/confirm_date`
⇒ monitor 按快照实价记的 `entry_price`（操作事实）被重放的**理论 D1 开盘价**覆写。
`vanish_settled` 只在报告里警告、不拦截（设计 §2.2「操作事实不可重建」被破）。

本文件锁三层：
  1. **SQL 形态**：操作事实列必须是 `COALESCE(库列, 重放值)`（库有则保留），
     判定列（state/entry_date/score）仍可整写 —— 两类语义不得混淆；
  2. **error 拦截**：`rebuild.vanish_settled=error` + 存在 vanish_settled 条目
     ⇒ 非 dry-run 的 apply **抛错且一条语句都不执行**（fail-fast，不是只打日志）；
  3. **dry-run 与 warn 缺省**：dry-run 不抛、标 `vanish_would_block`；
     缺省 warn 行为与历史一致（不改变既有生产行为）。

易错点：
    · fake DB 只登记语句、不执行 SQL —— COALESCE 的"库值优先"语义由 SQL 形态
      断言锁定（真语义须在 PostgreSQL dry-run 下验证，见方案 §P2-12 风险条）；
    · `_vanish_settled_mode` 走 `strategies.load_config()`（调用期导入）⇒
      monkeypatch 打在 `app.market_cn.auto.strategies.load_config`。
"""

from __future__ import annotations

import re

import pytest

from app.market_cn.auto import rebuild, registry, store

TD = "2026-06-17"
ST = "p212_stub"
K = (TD, ST, "600000", "a")
REAL_ENTRY_PRICE = 10.5      # 库中: monitor 快照实价 (操作事实)
THEORY_ENTRY_PRICE = 10.1    # 重放: 理论 D1 开盘价 (判定口径)

#: 操作事实列 —— UPDATE 里必须全部以 COALESCE(库列, ...) 形态出现
OPS_COLS = ("entry_price", "stop_price", "exit_date",
            "exit_price", "exit_reason", "confirm_date")


# ================================================================
# fake DB (同 test_upsert_partial_failure 的取证思路: 只登记语句)
# ================================================================
class _FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        owner = self

        class _Cur:
            rowcount = 1

            def execute(self_, sql, params=None):
                owner.executed.append((sql, params))

            def close(self_):
                pass

        return _Cur()

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_db(monkeypatch, conn):
    import app.utils.db as db_mod
    monkeypatch.setattr(db_mod, "get_db_connection", lambda: conn)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "strategy_keys", lambda: [ST])
    monkeypatch.setattr(registry, "enabled_keys", lambda: [ST])
    monkeypatch.setattr(store, "retire_unfilled", lambda **kw: [])


def _patch_mode(monkeypatch, mode):
    import app.market_cn.auto.strategies as strat_reg
    if mode is None:
        # 缺省: config 里没有 rebuild 段
        monkeypatch.setattr(strat_reg, "load_config",
                            lambda refresh=False: {})
    else:
        monkeypatch.setattr(strat_reg, "load_config",
                            lambda refresh=False: {"rebuild": {"vanish_settled": mode}})


def _mk_plan():
    """库中实价 + 重放理论价 → 含 upsert 与 vanish_settled 的计划。"""
    replay = {
        K: {"trade_date": TD, "strategy": ST, "code": "600000", "style": "a",
            "name": "南玻A", "board": "main", "score": 60, "state": "closed",
            "signal_date": TD, "signal_price": 10.0,
            "entry_date": TD, "entry_price": THEORY_ENTRY_PRICE,
            "stop_price": 9.6,
            "exit_date": TD, "exit_price": 10.4, "exit_reason": "day_close",
            "confirm_date": TD, "extra": {}},
    }
    actual = {
        K: {"id": 77, "trade_date": TD, "strategy": ST, "code": "600000",
            "style": "a", "state": "holding", "entry_date": TD,
            "entry_price": REAL_ENTRY_PRICE, "stop_price": 9.7,
            "exit_date": None, "exit_price": None, "exit_reason": None,
            "confirm_date": None},
    }
    plan = rebuild.build_ledger_plan(replay, actual, {"strategies": [ST]})
    return plan


# ================================================================
# 1. 计划层: 实价行进 upsert 且被登记为 vanish_settled
# ================================================================
def test_plan_flags_state_rewrite_and_keeps_upsert(monkeypatch):
    _patch_registry(monkeypatch)
    plan = _mk_plan()
    assert len(plan["upsert"]) == 1, "实价已推进行应进 upsert (COALESCE 保护下可写)"
    assert plan["keep_marked"] == [], "actual 无 exit_reason ⇒ 不该走 keep_marked"
    assert plan["keep_actual"] == [], "重放也带 entry_date ⇒ 不触发 keep_actual"
    assert len(plan["vanish_settled"]) == 1, (
        "已推进行状态被改写必须登记 vanish_settled (供 warn/error 处置)")


# ================================================================
# 2. SQL 形态: 操作列 COALESCE 库值优先, 判定列整写
# ================================================================
def test_update_sql_preserves_op_columns(monkeypatch):
    """「库中 entry_price=实价、重放算出理论价」⇒ UPDATE 不得整写 entry_price。"""
    _patch_registry(monkeypatch)
    _patch_mode(monkeypatch, None)          # 缺省 warn
    plan = _mk_plan()
    conn = _FakeConn()
    _patch_db(monkeypatch, conn)

    stat = rebuild.apply_ledger_plan(plan, dry_run=False)
    assert stat["upserted"] == 1

    upd = [(s, p) for s, p in conn.executed
           if " ".join(s.split()).startswith("UPDATE qd_dragon_signals SET")]
    assert len(upd) == 1, f"应恰好 1 条 UPDATE, 实得 {len(upd)}"
    sql = " ".join(upd[0][0].split())

    for col in OPS_COLS:
        assert not re.search(rf"\b{col} = %s", sql), (
            f"操作事实列 {col} 仍是整写 `= %s` —— 重放会覆写操作事实 (P2-12)")
        assert f"{col} = COALESCE(qd_dragon_signals.{col}, %s)" in sql, (
            f"操作事实列 {col} 必须是 COALESCE(库列, 重放值): 库有则保留、无则补")
    # 判定列仍可整写 (重放口径生效)
    assert "state = %s" in sql and "entry_date = %s" in sql
    # extra 仍是合并形态 (既有 P5-⑤-3 门禁, 不回归)
    assert "extra = extra ||" in sql
    # 参数序与 SQL 占位一致: 重放的理论价仍在参数里 (供 COALESCE 兜底补空)
    params = upd[0][1]
    assert THEORY_ENTRY_PRICE in params, "重放值应作为 COALESCE 的兜底参数保留"


# ================================================================
# 3. vanish_settled: warn 缺省 / error 拦截 / dry-run 只标记
# ================================================================
def test_vanish_default_warn_writes(monkeypatch):
    _patch_registry(monkeypatch)
    _patch_mode(monkeypatch, None)
    plan = _mk_plan()
    conn = _FakeConn()
    _patch_db(monkeypatch, conn)
    stat = rebuild.apply_ledger_plan(plan, dry_run=False)
    assert stat["upserted"] == 1 and conn.executed, "缺省 warn 不得改变既有写库行为"
    assert stat.get("vanish_mode") == "warn"


def test_vanish_error_blocks_write(monkeypatch):
    """error 模式: 非 dry-run 必须抛错, 且**零语句**执行 (不是先写再报错)。"""
    _patch_registry(monkeypatch)
    _patch_mode(monkeypatch, "error")
    plan = _mk_plan()
    conn = _FakeConn()
    _patch_db(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="vanish_settled"):
        rebuild.apply_ledger_plan(plan, dry_run=False)
    assert not conn.executed, "拦截必须发生在任何写语句之前"
    assert plan["vanish_settled"], "前置: 场景必须真的含 vanish_settled 条目"


def test_vanish_error_dry_run_reports_block(monkeypatch):
    """dry-run 不抛 —— 预演要给人看全貌; 但必须标出将被拦截。"""
    _patch_registry(monkeypatch)
    _patch_mode(monkeypatch, "error")
    plan = _mk_plan()
    stat = rebuild.apply_ledger_plan(plan, dry_run=True)
    assert stat["dry_run"] is True
    assert stat.get("vanish_would_block") is True
    assert stat.get("vanish_mode") == "error"


def test_no_vanish_error_mode_writes_normally(monkeypatch):
    """error 模式但**无** vanish_settled 条目 ⇒ 正常写库 (拦截只针对该风险)。"""
    _patch_registry(monkeypatch)
    _patch_mode(monkeypatch, "error")
    plan = _mk_plan()
    plan["vanish_settled"] = []     # 模拟守卫已把状态差异消掉
    conn = _FakeConn()
    _patch_db(monkeypatch, conn)
    stat = rebuild.apply_ledger_plan(plan, dry_run=False)
    assert stat["upserted"] == 1 and conn.executed
