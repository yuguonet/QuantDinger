# -*- coding: utf-8 -*-
"""v5 阶段 D2：分层配额选股 `pick_daily_universe` 的守卫（TDD RED，2026-10-13）

【为什么必须配额，不能"多活跃的股就多采"】
v5 §19.1 的统计第一性：名义案例数涨到 10 万，**独立簇数一分不涨**
（同日样本相关 ⇒ DEFF_day≈33.6，150 只/日只折合约 4.5 个独立观察）。
若某天活跃股多就多采，采集分布会随行情漂移 —— 涨得好的日子样本全是强势股，
跌的日子全是弱势股，**条件格会被行情状态污染**，而那正是我们想测的东西。
⇒ 配额**按 date 强制**：每天固定 60/25/15，与当日活跃度无关。

【本文件守什么】
1. 三层比例恒为 0.6/0.25/0.15（容差 ±1）
2. **低热层非空** —— 最容易在实现时被"价值低"为由跳过，但它是无偏基线，缺了它
   所有条件效应都失去参照（这就是对照组被自己拆掉）
3. 三层**互不重叠**（同一标的被采两次 = 名义样本虚高，实际信息量为 1）
4. 配额按 date 强制：候选池再大也不超额
5. 某层源缺失 ⇒ **不把缺额挪给其他层**（挪了就看不出采集质量退化了）

【易错处】
· 测试全部注入假 `sources`，不碰网络 —— 真源可用性由 `tmp/` 下的探测脚本单独验。
· 比例用 floor 分配 + cold 吃余额，所以 150 ⇒ 90/37/23，不是 90/38/22。
"""
from __future__ import annotations

import pytest

from app.agent.chain import sampler


def _fake_sources():
    """三层都有货的假源：hot 200 只、warm 200 只、全市场 3000 只。"""
    return {
        # 活跃池四个来源（涨停/炸板/龙虎榜/热榜）
        "limit_pool": lambda d, t: {"zt": {"stocks": [{"code": f"60{i:04d}"} for i in range(100)]}}
        if t == "zt" else {"broken": {"stocks": [{"code": f"60{i:04d}"} for i in range(100, 150)]}},
        "dragon_tiger": lambda d: {"stocks": [{"code": f"00{i:04d}"} for i in range(150, 200)]},
        "hot_rank": lambda n: {"stocks": [{"code": f"30{i:04d}"} for i in range(50)]},
        # 中热：热门板块成分股
        "hot_sectors": lambda: {"industry": [{"code": "bk_a", "name": "板块A"}]},
        # ★ limit 必须带默认值：实现侧只按 board_code 调用，两个必填参数会 TypeError
        "sector_stocks": lambda bc, limit=10: {"stocks": [{"code": f"90{i:04d}"} for i in range(200)]},
        # 全市场（低热随机池）
        "all_stocks": lambda: [{"symbol": f"80{i:04d}"} for i in range(3000)],
    }


def _dead_sources():
    """全部取不到。"""
    def _n(*a, **k):
        return None
    return {k: _n for k in ("limit_pool", "dragon_tiger", "hot_rank",
                            "hot_sectors", "sector_stocks", "all_stocks")}


# ═══════════════════════════════════════════════════════════════

def test_layer_ratios_are_enforced():
    """比例必须是 0.6/0.25/0.15（floor 分配，cold 吃余额 ⇒ 90/37/23）。"""
    out = sampler.pick_daily_universe("2026-10-12", 150, sources=_fake_sources())
    got = {k: sum(1 for it in out["items"] if it["sample_layer"] == k)
           for k in ("hot", "warm", "cold")}
    assert got["hot"] == 90, f"hot 应为 90，实际 {got['hot']}"
    assert abs(got["warm"] - 37) <= 1, f"warm 应为 37±1，实际 {got['warm']}"
    assert abs(got["cold"] - 23) <= 1, f"cold 应为 23±1，实际 {got['cold']}"
    assert sum(got.values()) == 150, f"总数应为 150，实际 {sum(got.values())}"


def test_cold_layer_is_not_empty():
    """★ 低热层必须非空：它是无偏基线，缺了它所有条件效应都失去参照。"""
    out = sampler.pick_daily_universe("2026-10-12", 150, sources=_fake_sources())
    cold = [it for it in out["items"] if it["sample_layer"] == "cold"]
    assert cold, (
        "低热层为空：实现者常以『价值低』跳过它，但 v5 的 Δp 估计需要一个"
        "不随活跃度漂移的基线，否则条件效应与行情状态共线，永远分不开。")


def test_layers_do_not_overlap():
    """同一标的不许出现在两层 —— 采两次名义上是 2 条，信息量仍是 1 条。"""
    out = sampler.pick_daily_universe("2026-10-12", 150, sources=_fake_sources())
    seen: dict[str, str] = {}
    for it in out["items"]:
        c, lay = it["code"], it["sample_layer"]
        assert c not in seen, (
            f"标的 {c} 同时出现在 {seen[c]} 与 {lay} 层：同一天被采两次，"
            "名义样本虚高而独立信息量不变 —— 正是 §19.1 警告的假样本。")
        seen[c] = lay


def test_every_item_carries_sample_layer():
    out = sampler.pick_daily_universe("2026-10-12", 150, sources=_fake_sources())
    bad = [it for it in out["items"] if it.get("sample_layer") not in ("hot", "warm", "cold")]
    assert not bad, f"{len(bad)} 条缺合法 sample_layer：无法分层记账 ⇒ 事后无法做分层加权"


def test_quota_is_capped_regardless_of_pool_size():
    """候选池再大也不超额（配额按 date 强制，不随当日活跃度漂移）。"""
    big = _fake_sources()
    # ★ 号段必须与其他层不冲突：_norm_code 取**后 6 位**，若用 f"99{i:05d}"（7 位）
    #   归一后会变成 900000+，与 warm 池的 90xxxx 撞车 ⇒ warm 被去重成 0，
    #   测试就变成在查号段而不是查配额。用 700000+ 这个独立号段。
    big["limit_pool"] = lambda d, t: {
        "zt": {"stocks": [{"code": str(700000 + i)} for i in range(5000)]}}
    out = sampler.pick_daily_universe("2026-10-12", 150, sources=big)
    assert len(out["items"]) == 150, (
        f"候选池放大到 5000 后采了 {len(out['items'])} 只：配额没被强制。"
        "任由活跃度决定采集量 ⇒ 采集分布随行情漂移 ⇒ 条件格被行情状态污染。")


def test_shortage_is_not_redistributed():
    """★ 某层源挂了 ⇒ 该层留空并记 shortage，**不**把名额挪给别的层。

    挪了名额，表面看采集量达标，实际上采集质量退化被藏起来了。
    """
    src = _fake_sources()
    src["hot_sectors"] = lambda: None      # 中热源挂掉
    out = sampler.pick_daily_universe("2026-10-12", 150, sources=src)
    warm = [it for it in out["items"] if it["sample_layer"] == "warm"]
    assert not warm, "中热源已置空，warm 层不应还有标的"
    assert out["shortage"].get("warm"), (
        "中热层缺额必须记进 shortage：否则事后无法区分『今天没有中热股』与『中热源挂了』")
    # hot 仍按原配额，不得因为 warm 空了就多吃
    got_hot = sum(1 for it in out["items"] if it["sample_layer"] == "hot")
    assert got_hot == 90, f"warm 缺额被挪给了 hot（hot={got_hot}），采集质量退化被藏起来了"


def test_all_sources_dead_is_honest():
    """源全挂：结果为空 + missing 写明，绝不返回一批伪造标的。"""
    out = sampler.pick_daily_universe("2026-10-12", 150, sources=_dead_sources())
    assert out["items"] == [], "源全不可用时应返回空列表，而不是编一批标的出来"
    assert out["missing"], "必须在 missing 里写明哪些源不可用"
