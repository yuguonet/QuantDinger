# -*- coding: utf-8 -*-
"""F10 守卫：cached_tokens（命中前缀缓存 input token）落库全链路（2026-10-17）。

数据源（已实测证实）：网关 g2claw 回填 `usage.prompt_tokens_details.cached_tokens`
⇒ mimo `token_stats.cache_read_tokens`（`tmp/llm_probe/**` 大量非零：9357/10208/…）。

口径铁律：
  · cached_tokens **含在** total_tokens 之内（不是增量，不能相加）；
  · 网关未上报 ⇒ 写 **NULL**（与「真实 0」区分开，否则缓存率会算成 0% 误导）。

四道闸（本文件锁的不变量）：
  ① `EvalNode` 字段存在且 to_dict/from_dict 可往返，缺省 None；
  ② `chain/store.py` 的 UPDATE 与 INSERT 都带 cached_tokens，且
     **列数 / 占位符数 / 参数个数三者对齐**（加列最易踩的错位 → 整条写库炸）；
  ③ 补列是幂等 ALTER：应用内 `_ensure_trace_columns`（存量库）+
     `migrations/qd_traces.sql`（docker 首启）双备，且 fail-open；
  ④ 落盘侧 `qd_aftercare` 真的把 `cache_read_tokens` 透传下去。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
STORE = BACKEND / "app" / "agent" / "chain" / "store.py"
SCHEMA = BACKEND / "app" / "agent" / "chain" / "schema.py"
COLLECTOR = BACKEND / "app" / "agent" / "trace_collector.py"
AFTERCARE = BACKEND / "app" / "agent" / "qd_aftercare.py"
MIGRATION = BACKEND / "migrations" / "qd_traces.sql"


# ── ① EvalNode 字段（数据契约）────────────────────────────────────────

def test_evalnode_cached_tokens_roundtrip():
    from app.agent.chain.schema import EvalNode

    n = EvalNode(name="t", total_tokens=20000, cached_tokens=9357)
    assert n.to_dict()["cached_tokens"] == 9357, "to_dict 必须带 cached_tokens"
    assert EvalNode.from_dict(n.to_dict()).cached_tokens == 9357, "from_dict 必须能还原"

    assert EvalNode(name="t").cached_tokens is None, (
        "缺省必须是 None ⇒ 写库为 NULL（未上报 ≠ 真实 0）")
    assert EvalNode(name="t", total_tokens=20000).cached_tokens is None


# ── ② store 写库 SQL：列 / 占位符 / 参数 三对齐 ───────────────────────

def test_store_declares_cached_tokens_in_both_statements():
    src = STORE.read_text(encoding="utf-8")
    assert "cached_tokens=%s" in src, "UPDATE 必须写 cached_tokens 列"
    assert "total_tokens, cached_tokens," in src, \
        "INSERT 列清单必须含 cached_tokens（紧跟 total_tokens）"
    assert src.count("node.cached_tokens") >= 2, \
        "UPDATE 与 INSERT 两个参数元组都要传 node.cached_tokens"


def test_insert_column_placeholder_param_counts_align():
    """INSERT 列数 == 占位符数 == 参数个数（F10 加列最危险的错位）。

    任一不等 ⇒ 整条 INSERT 抛 `arguments for column ... do not match`，
    qd_agent_traces **一条都写不进去**（声明了却静默断链，见 qd_traces.sql:85）。
    """
    src = STORE.read_text(encoding="utf-8")
    m = re.search(
        r"INSERT INTO qd_agent_traces\s*\((?P<cols>.*?)\)\s*VALUES\s*\((?P<phs>.*?)\)\s*RETURNING id",
        src, re.S)
    assert m, "store.py 里找不到 INSERT qd_agent_traces 模板"

    cols = [c.strip() for c in m.group("cols").split(",") if c.strip()]
    phs = [p.strip() for p in m.group("phs").split(",") if p.strip()]
    assert len(cols) == len(phs), f"列数={len(cols)} vs 占位符={len(phs)}"

    pm = re.search(r'""",\s*\((?P<args>.*?)\)\s*\)', src[m.end():], re.S)
    assert pm, "取不到 INSERT 的参数元组"
    args = [a.strip() for a in pm.group("args").split(",") if a.strip()]
    assert len(cols) == len(args), f"列数={len(cols)} vs 参数={len(args)}: {args}"

    i = cols.index("cached_tokens")
    assert cols[i - 1] == "total_tokens" and phs[i] and args[i] == "node.cached_tokens", (
        f"cached_tokens 必须紧跟 total_tokens 并一一对应: "
        f"cols[i-1]={cols[i-1]} args[i]={args[i]}")


# ── ③ 补列：幂等 + fail-open + 双入口 ─────────────────────────────────

def test_migration_declares_column_and_idempotent_alter():
    mig = MIGRATION.read_text(encoding="utf-8")
    assert re.search(r"cached_tokens\s+INTEGER", mig, re.I), \
        "migrations/qd_traces.sql 建表体必须声明 cached_tokens（docker 首启）"
    assert "ADD COLUMN IF NOT EXISTS cached_tokens" in mig, \
        "存量库必须有幂等补列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）"


def test_app_side_ensure_runs_and_is_once_per_process():
    """应用侧补列（存量库真源）：save_tree 前置调用，进程内只跑一次，失败不抛。"""
    from app.agent.chain import store

    src = STORE.read_text(encoding="utf-8")
    assert "_ensure_trace_columns(cur)" in src, "save_tree 必须先补列再写"

    class _Cur:
        def __init__(self):
            self.sqls = []

        def execute(self, sql):
            self.sqls.append(sql)

    store._TRACE_COLUMNS_ENSURED = False
    try:
        c1 = _Cur()
        store._ensure_trace_columns(c1)
        assert len(c1.sqls) == 1 and "ADD COLUMN IF NOT EXISTS cached_tokens" in c1.sqls[0], \
            c1.sqls

        c2 = _Cur()
        store._ensure_trace_columns(c2)
        assert c2.sqls == [], "进程级 flag 必须保证只补一次（避免每次写库都 ALTER）"

        class _Bad:
            def execute(self, sql):
                raise RuntimeError("no db")

        store._ensure_trace_columns(_Bad())   # 必须 fail-open，不抛
        assert store._TRACE_COLUMNS_ENSURED is True
    finally:
        store._TRACE_COLUMNS_ENSURED = False


# ── ④ 落盘侧：qd_aftercare → on_agent_finish 透传 ─────────────────────

def test_collector_signature_is_backward_compatible():
    import inspect

    from app.agent.trace_collector import TraceCollector

    sig = inspect.signature(TraceCollector.on_agent_finish)
    assert "cached_tokens" in sig.parameters, "on_agent_finish 必须收 cached_tokens"
    assert sig.parameters["cached_tokens"].default == 0, \
        "缺省 0 ⇒ 既有调用方（positional/keyword）不破坏"

    c = TraceCollector(session_id="s1", user_query="买什么股")
    root = c.on_agent_finish("答", total_steps=3, total_tokens=20000,
                             model="m", cached_tokens=9357)
    assert root.cached_tokens == 9357 and root.total_tokens == 20000, (
        "cached 必须与分母 total_tokens 同源落库，否则比率算不出来")
    assert root.model == "m", "model 形参此前只收不用（主链恒 NULL）"
    assert root.input_params.get("total_steps") == 3, (
        "total_steps 此前只收不用 ⇒ 问责读不到步数")

    root2 = c.on_agent_finish("答", total_steps=1, total_tokens=20000, model="m")
    assert root2.cached_tokens is None, "不传 ⇒ NULL（不把未上报误记成 0）"


def test_aftercare_passes_cache_read_tokens():
    """三条 cost 快照路径都必须透传 cached（F15b 后改经 upstream_compat）。

    F15b（2026-10-17）：`getattr(stats, "cache_read_tokens", 0)` 形态被
    `read_cache_tokens(self.model) or 0` 取代（上游契约收口）——
    这里同步改断言，但**意图不变**：三处快照都要带 cached。
    """
    src = AFTERCARE.read_text(encoding="utf-8")
    assert "from .upstream_compat import read_cache_tokens" in src, \
        "qd_aftercare 必须经 upstream_compat 读 cached（F15b 收口）"
    n = src.count("read_cache_tokens(self.model) or 0")
    assert n >= 3, f"qd_aftercare 应有 3 处 token 快照透传 cached，实际 {n}"
    assert 'cached_tokens=_snap["cached"]' in src, "同步路径必须带 cached_tokens"
    assert "cached_tokens=read_cache_tokens(self.model) or 0" in src, \
        "异步/直呼路径也要带"
    assert '"cached": read_cache_tokens(self.model) or 0' in src, \
        "案例成本载荷（经验库闭环）必须含 cached"
