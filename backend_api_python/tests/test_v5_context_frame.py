# -*- coding: utf-8 -*-
"""v5 阶段 D1：C 类情境档案（每日一帧）的守卫（TDD RED，2026-10-13）

【为什么是 C 类、为什么必须现在开始记】
v5 §19.3 / v4 §21.2：市场 L0 / 板块 L1 L2 只能按天积累，**不能着急**。
更关键的是 **C 类不可重建** —— 今天没记，明天就永远补不回来（不像行情可以回拉）。
所以本阶段的原则是「**先落盘，后进统计**：先保证数据开始流且不丢，不做任何分析」。

【本文件守什么】
1. 必需字段齐全（缺一个字段，将来条件格就少一个维度）
2. **同日连跑不重复追加**（盘后任务可能被重跑/补跑，重复帧会污染计数）
3. **取不到就是 null + reason，绝不填 0 / 编造**（v5 红线：拒绝兜底）
   —— 这是最重要的一条：把「没有数据」伪装成 0，会让下游把「无涨停」
   当成「涨停家数为 0」，那是个**完全不同**的事实。
4. `as_of` 必须是**最近已收盘交易日**
   —— 实测（2026-10-13 凌晨）：涨停池/热榜/龙虎榜在未开盘时全返回 count=0。
   若拿今天当 as_of，记下的是「今天有 0 只涨停」，而真相是「今天还没开盘」。

【易错处（改本文件前先读）】
· 测试**不碰网络**：全部用 monkeypatch 注入 `_SOURCES`，既快又稳定。
  真数据源的可用性由 `tmp/probe_d1_sources.py` 单独实测，不混进单测。
· 分档阈值必须走 env（禁止硬编码），所以测分档时要 monkeypatch.setenv。
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

from app.agent.chain import context_frame as cf

REQUIRED = ("as_of", "regime", "theme_heat", "vol_bucket",
            "index_ret", "limit_up_cnt", "turnover")


# ── 假数据源（离线，可控）───────────────────────────────────────
def _ok_sources():
    """一切正常的源。"""
    return {
        "index_quote": lambda code: {"change_percent": -0.43, "amount": 7.149e11},
        "index_kline": lambda code, days: {
            "bars": [{"close": 3800.0 + i * 2.0} for i in range(30)]},
        "limit_pool": lambda d: {"zt": {"count": 42}},
        "hot_sectors": lambda: {"industry": [{"change_pct": 2.5}, {"change_pct": 1.5}],
                                "concept": [{"change_pct": 3.0}]},
    }


def _dead_sources():
    """全部取不到：必须落 null + reason，而不是 0。"""
    def _none(*a, **k):
        return None
    return {k: _none for k in ("index_quote", "index_kline",
                               "limit_pool", "hot_sectors")}


@pytest.fixture()
def frames_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "FRAMES_DIR", str(tmp_path))
    return tmp_path


# ═══════════════════════════════════════════════════════════════
#  RED：字段齐全 + 不编造 + 同日去重
# ═══════════════════════════════════════════════════════════════

def test_frame_has_all_required_fields(frames_dir, monkeypatch):
    """必需字段一个都不能少 —— 少一个维度，将来条件格就少一份判别力。"""
    f = cf.build_frame(as_of=date(2026, 10, 10), sources=_ok_sources())
    missing = [k for k in REQUIRED if k not in f]
    assert not missing, f"情境帧缺字段 {missing}：当前 {sorted(f)}"
    assert f["as_of"] == "2026-10-10"
    assert f["index_ret"] == -0.43
    assert f["limit_up_cnt"] == 42


def test_unavailable_source_is_null_not_zero(frames_dir):
    """★ 核心：取不到必须落 None + reason，**绝不能是 0**。

    把「没取到」写成 0，下游会把「今天 0 只涨停」当事实 —— 那是**另一个**事实。
    """
    f = cf.build_frame(as_of=date(2026, 10, 10), sources=_dead_sources())
    for k in ("index_ret", "limit_up_cnt", "turnover"):
        assert f[k] is None, (
            f"{k} 在源不可用时应为 None，实际 {f[k]!r}。"
            "填 0 / -1 / 空串都是**编造**，会让下游把「无数据」当成「数据为 0」，"
            "v5 红线明确禁止兜底。")
    assert f["missing"], (
        "取不到的字段必须在 frame['missing'] 里写明原因，"
        "否则事后无法区分「真为 0」与「没取到」。")
    assert set(f["missing"]) >= {"index_ret", "limit_up_cnt", "turnover"}


def test_same_day_not_appended_twice(frames_dir):
    """同日连跑两次，文件里只能有一帧（盘后任务会被重跑/补跑）。"""
    for _ in range(2):
        cf.append_frame(cf.build_frame(as_of=date(2026, 10, 10),
                                       sources=_ok_sources()))
    path = cf.frame_path(date(2026, 10, 10))
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, (
        f"同日写了 {len(lines)} 帧：重复帧会让「按天计数」整体虚高。"
        "必须按 as_of 去重（覆盖同日旧帧，而不是追加）。")


def test_different_days_are_both_kept(frames_dir):
    """不同日期必须各自保留（去重不能误伤）。"""
    cf.append_frame(cf.build_frame(as_of=date(2026, 10, 9), sources=_ok_sources()))
    cf.append_frame(cf.build_frame(as_of=date(2026, 10, 10), sources=_ok_sources()))
    rows = cf.load_frames()
    assert {r["as_of"] for r in rows} == {"2026-10-09", "2026-10-10"}


# ═══════════════════════════════════════════════════════════════
#  RED：as_of 必须是「最近已收盘交易日」
# ═══════════════════════════════════════════════════════════════

def test_default_as_of_is_last_closed_trading_day(frames_dir, monkeypatch):
    """不传 as_of 时，必须取**最近已收盘交易日**，不能是「今天」。

    实测（2026-10-13 01:15）：今日未开盘，涨停池/热榜/龙虎榜全返回 count=0。
    若默认用 today，档案里会记下「2026-10-13 有 0 只涨停」—— 这是假的。
    """
    f = cf.build_frame(sources=_ok_sources())
    today = date.today().isoformat()
    assert f["as_of"] != today or f["market_closed"] is True, (
        f"默认 as_of 取到了今天 {today}，但今天尚未收盘："
        "此时实时类接口（涨停池/热榜/龙虎榜）返回的 0 是「未开盘」而非「真的 0」，"
        "记进档案就是一条脏帧。应默认用最近**已收盘**交易日。"
    )
    # 必须是个合法日期，且能标注该日是否已收盘
    assert "market_closed" in f, "帧里必须带 market_closed 标记（供下游判断可否用于统计）"


# ═══════════════════════════════════════════════════════════════
#  分档：阈值走 env，不许硬编码
# ═══════════════════════════════════════════════════════════════

def test_vol_bucket_respects_env_thresholds(frames_dir, monkeypatch):
    """波动分档阈值必须来自 env（改 env 就得改结果 ⇒ 证明没有硬编码）。"""
    # ★ 用一个荒谬但确定的值：任何波动率都 >= -1.0 ⇒ 分档必为 high。
    #   （不能用 0.01 这类"看着合理"的值：本例假数据的收益是恒定的，
    #    波动率算出来就是 0，0 >= 0.01 不成立，测试会假失败。）
    monkeypatch.setenv("QD_CF_VOL_HIGH", "-1.0")
    f = cf.build_frame(as_of=date(2026, 10, 10), sources=_ok_sources())
    assert f["vol_bucket"] == "high", (
        f"阈值 env 改了但分档没跟着变（得到 {f['vol_bucket']}）⇒ 说明阈值被硬编码了。"
        "v5 红线：阈值一律走 env 或模块常量+注释。")

    # 反向再验一次：把高档阈值拉到不可能达到 ⇒ 同样的输入应落到 low
    monkeypatch.setenv("QD_CF_VOL_HIGH", "999.0")
    monkeypatch.setenv("QD_CF_VOL_LOW", "999.0")
    f2 = cf.build_frame(as_of=date(2026, 10, 10), sources=_ok_sources())
    assert f2["vol_bucket"] == "low", (
        f"同一份数据在另一组阈值下应得 low，实际 {f2['vol_bucket']} ⇒ env 未生效。")


def test_sources_returns_jsonl_roundtrip(frames_dir):
    """落盘后能原样读回（JSONL 一行一帧，字段不丢）。"""
    src = cf.build_frame(as_of=date(2026, 10, 10), sources=_ok_sources())
    cf.append_frame(src)
    got = cf.load_frames()[0]
    for k in REQUIRED:
        assert got.get(k) == src.get(k), f"落盘回读后字段 {k} 不一致：{got.get(k)} != {src.get(k)}"
