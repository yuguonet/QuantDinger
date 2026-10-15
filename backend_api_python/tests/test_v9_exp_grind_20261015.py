# -*- coding: utf-8 -*-
"""v9 「小量刷经验」驱动器 `scripts/exp_grind.py` 的守卫测试（TDD，2026-10-15）。

## 这个脚本是什么（测之前先对齐认知）

`exp_grind` 是追责-校准闭环的**经验水龙头**：挑一小批股票走**真实** agent
分析（`run_agent` → `_finish_collector` → intake/c3p 自动落 claims + pairs），
再驱动一次盘后闭环（`auto_evaluate` → T+N 真值判定 → domain 权重），
前后对比 `calib_feed.build_report()`。设计依据 AGENT_DESIGN_v3.md §21.3
（经验来源/分层配额）、§21.4（P3 接线细则）、数据流模拟报告 S-5
（`daily_universe` 只写不读的产线缺环）。

★ 它**不写任何合成数据** —— pairs/claims/resolutions 全部由生产链路产出。
  本测试因此只守**驱动器的编排纪律**，不守闭环数学（那是
  `test_v5_calib_feed.py` 的职责，那边已用合成 fixture 全覆盖）。

## 守哪四条（对应最容易写错的地方）

| 纪律 | 写错的后果 |
|---|---|
| 分层轮采（hot/warm/cold 都要有） | 样本集中在活跃层 ⇒ 情境维近常数 ⇒ 条件化失效（§21.3） |
| 同日确定性（date 作种子） | 同一天两次跑选出不同票 ⇒ 不可复现、无法对账 |
| batch 上限钳制 | 手滑 `--batch 100` ⇒ 一晚上烧掉一个月 token 预算 |
| dry-run 不碰执行核 | "只看看计划" 也触发 LLM ⇒ 观测动作本身在花钱 |

## 易错处

· 本文件**不 import 执行核**：`exp_grind` 把 `run_agent` 的 import 放在
  函数内（dry-run 不付装配成本），测试才能裸跑不依赖 LLM/DB。
· 每个用例前重置 env（monkeypatch 自动还原），防止 env 泄漏串味。
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def mod(monkeypatch):
    """导入被测模块；env 一律经 monkeypatch 设（用完自动还原）。"""
    import app.agent.scripts.exp_grind as m
    importlib.reload(m)          # 防同进程内 env 缓存串味
    return m


@pytest.fixture()
def universe(monkeypatch):
    """把 collector.load_universe 换成可控 fixture（两层结构同真实文件）。"""
    import app.agent.chain.collector as col

    def _install(records):
        monkeypatch.setattr(col, "load_universe", lambda as_of=None: list(records))

    def _rec(as_of, items):
        return {"as_of": as_of, "n": len(items),
                "universe": {"as_of": as_of, "items": list(items)}}

    return _install, _rec


# ═══════════════════════════════════════════════════════════════
#  分层轮采（§21.3：小批量也要撑开情境维变异）
# ═══════════════════════════════════════════════════════════════

def test_pick_batch_is_stratified_across_layers(mod, universe):
    """★ 3 只的批次也必须三层都有 —— 全抽 hot 层等于主动放弃情境维变异。"""
    _install, _rec = universe
    _install([
        _rec("2026-10-14", [
            {"code": f"6{i:05d}", "sample_layer": "hot"} for i in range(30)
        ] + [
            {"code": f"3{i:05d}", "sample_layer": "warm"} for i in range(10)
        ] + [
            {"code": f"0{i:05d}", "sample_layer": "cold"} for i in range(10)
        ]),
    ])
    batch = mod.pick_batch(3, universe_date="2026-10-14")
    layers = {b["sample_layer"] for b in batch}
    assert layers == {"hot", "warm", "cold"}, f"分层轮采失效: {batch}"
    assert len(batch) == 3


def test_pick_batch_is_deterministic_per_date(mod, universe):
    """★ 同一天两次跑必须选出**同一批**（date 作种子）—— 不可复现就没法对账。"""
    _install, _rec = universe
    items = ([{"code": f"6{i:05d}", "sample_layer": "hot"} for i in range(30)]
             + [{"code": f"3{i:05d}", "sample_layer": "warm"} for i in range(10)])
    _install([_rec("2026-10-14", items)])
    b1 = [b["code"] for b in mod.pick_batch(4, universe_date="2026-10-14")]
    b2 = [b["code"] for b in mod.pick_batch(4, universe_date="2026-10-14")]
    assert b1 == b2 and len(set(b1)) == 4, f"同日两次结果不同: {b1} vs {b2}"


def test_pick_batch_uses_latest_universe_day_by_default(mod, universe):
    """不显式给日期 ⇒ 取最新一天的 universe（旧日期的样本不重复消费）。"""
    _install, _rec = universe
    _install([
        _rec("2026-10-12", [{"code": "111111", "sample_layer": "hot"}]),
        _rec("2026-10-14", [{"code": "222222", "sample_layer": "hot"},
                            {"code": "333333", "sample_layer": "warm"}]),
    ])
    batch = mod.pick_batch(2)
    assert {b["code"] for b in batch} == {"222222", "333333"}, (
        f"应只从最新一天(10-14)抽: {batch}")


def test_pick_batch_codes_override_skips_universe(mod, universe):
    """`--codes` 显式指定 ⇒ 不走抽样（运维想补刷特定票）。"""
    _install, _rec = universe
    _install([_rec("2026-10-14", [{"code": "999999", "sample_layer": "hot"}])])
    batch = mod.pick_batch(3, codes="600519, 000001.SH")
    assert [b["code"] for b in batch] == ["600519", "000001.SH"]
    assert all(b["sample_layer"] == "explicit" for b in batch)


def test_pick_batch_empty_universe_returns_empty(mod, universe):
    """universe 空（没跑过盘后采集）⇒ 返回空列表，由调用方出可读错误。"""
    _install, _rec = universe
    _install([])
    assert mod.pick_batch(3) == []


def test_pick_batch_never_duplicates_a_code(mod, universe):
    """同一票在一批里只出现一次（重复分析 = 重复计票，污染一 claim 一票）。"""
    _install, _rec = universe
    _install([_rec("2026-10-14", [{"code": "600519", "sample_layer": "hot"}] * 5)])
    batch = mod.pick_batch(3)
    assert len({b["code"] for b in batch}) == len(batch) == 1


# ═══════════════════════════════════════════════════════════════
#  成本护栏：batch 上限钳制（手滑大数字不许烧穿预算）
# ═══════════════════════════════════════════════════════════════

def test_batch_is_clamped_to_max(mod, monkeypatch):
    """★ `--batch 100` 必须被 QD_EXP_GRIND_MAX 钳住 —— 驱动器自己就是成本闸。"""
    monkeypatch.setenv("QD_EXP_GRIND_MAX", "10")
    assert mod.resolve_batch(requested=100) == 10
    assert mod.resolve_batch(requested=5) == 5


def test_batch_defaults_from_env(mod, monkeypatch):
    """不传 --batch ⇒ 取 env 默认（小量！默认值必须是小的）。"""
    monkeypatch.setenv("QD_EXP_GRIND_BATCH", "4")
    assert mod.resolve_batch(requested=0) == 4


def test_batch_default_is_small(mod, monkeypatch):
    """★ 什么都不配时默认必须小（"小量"是本脚本的存在理由）。"""
    monkeypatch.delenv("QD_EXP_GRIND_BATCH", raising=False)
    assert mod.resolve_batch(requested=0) <= 5


# ═══════════════════════════════════════════════════════════════
#  dry-run：只列计划，不碰执行核（观测动作本身不许花钱）
# ═══════════════════════════════════════════════════════════════

def test_dry_run_never_calls_run_agent(mod, universe, monkeypatch, capsys):
    """★ dry-run 只许打印计划：一旦误触 run_agent，LLM 账单就为"看看"付费了。

    必须驱动 `main()` 而不是只调 `build_plan` —— 早返回分支在 main 里，
    绕过它测等于恒绿（变异检验会证明这一点）。
    """
    _install, _rec = universe
    _install([_rec("2026-10-14", [{"code": "600519", "sample_layer": "hot"}])])

    def _boom(*a, **k):
        raise AssertionError("dry-run 不许触发执行核")

    monkeypatch.setattr(mod, "run_one", _boom)
    monkeypatch.setattr(mod, "drive_eval", _boom)
    rc = mod.main(["--dry-run", "--batch", "1", "--universe-date", "2026-10-14"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "600519" in out, f"dry-run 应把计划打到 stdout: {out!r}"


def test_session_is_per_stock(mod, universe):
    """★ 每票独立 session —— 同 session 多票会让后一票引用前一票上下文，
    污染 claim 归属（A-01 per-session 锁的正确用法）。"""
    _install, _rec = universe
    _install([_rec("2026-10-14", [{"code": "600519", "sample_layer": "hot"},
                                  {"code": "000858", "sample_layer": "warm"}])])
    plan = mod.build_plan(batch=2, universe_date="2026-10-14")
    sids = [p["session"] for p in plan]
    assert len(set(sids)) == 2, f"session 必须每票一个: {sids}"


# ═══════════════════════════════════════════════════════════════
#  snapshot：每个数据源独立 fail-open（一个源挂了不许带崩整份报告）
# ═══════════════════════════════════════════════════════════════

def test_snapshot_survives_all_sources_down(mod, monkeypatch):
    """DB/文件全挂 ⇒ snapshot 仍要返回带 err 标记的字典（不是抛异常）。"""

    def _boom(*a, **k):
        raise RuntimeError("db down")

    import app.agent.chain.aggregator as ag
    import app.agent.chain.account_store as acs
    import app.agent.chain.calib_feed as cf
    monkeypatch.setattr(ag, "load_pairs", _boom)
    monkeypatch.setattr(acs, "query_due_claims", _boom)
    monkeypatch.setattr(acs, "load_latest_resolutions", _boom)
    monkeypatch.setattr(cf, "build_report", _boom)
    snap = mod.snapshot()
    assert isinstance(snap, dict)
    assert snap.get("pairs_err") or snap.get("pairs") is None


def test_snapshot_reports_calib_fields(mod, monkeypatch):
    """快照必须含校准闭环的四个关键数（§21.4 指定的观测入口字段）。"""
    import app.agent.chain.calib_feed as cf
    monkeypatch.setattr(cf, "build_report", lambda **k: {
        "n_samples": 7, "n_cells": 2, "n_clusters_total": 41, "n_usable": 1,
        "cells": {"down|low": {"n_clusters": 35, "n_obs": 60, "win_rate": 0.62}}})
    snap = mod.snapshot()
    assert snap["n_samples"] == 7
    assert snap["n_cells"] == 2
    assert snap["n_clusters_total"] == 41
    assert snap["usable_cells"] == 1
    assert snap["cells"]["down|low"]["n_clusters"] == 35
