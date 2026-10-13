"""v5 P0 闭环守卫（TDD RED 阶段，2026-10-13）

背景：v5 §14 把 P0 定为**阻断级** —— 标签不可靠时建经验表，只会得到一张漂亮的空表。

2026-10-13 实测存量（本测试的立项依据）：
    qd_agent_resolutions  16 条 → undecidable 9 / hit 4 / miss 3（**有效仅 7 条**）
    qd_agent_weights      58 行 → 仅 1 行 sample_count>0（domain/finance, n=7, win_rate 0.5714）
    qd_agent_cases       633 条；qd_agent_messages 125 条

⇒ 由此得到两条**与 v4 §27.12 不同的实测结论**，写在这里以免后人重复排查：
    ① **feed 链路其实是通的**（collect_domain_stats 能聚合出 domain/finance n=7，
       weights 表也确实写入了 sample_count=7）。v4 §27.12 记的「weight_feed 没喂数」不成立。
    ② **真正的瓶颈是分子太小且被吃掉**：16 条里 9 条 undecidable（56%）被
       `WHERE verdict IN ('hit','partial','miss')` 排除，且**没有 delisted 分支**
       ⇒ 最差的那条尾巴被系统性丢弃（v4 G18 幸存者偏差）。

本文件两类测试（不要混为一谈）：
    RED（当前**必失败**，是修复目标）  —— test_delisted_* 三条
    守卫（当前**应通过**，防改动改坏）  —— test_normal_stock_not_delisted /
                                          test_historical_return_is_reproducible /
                                          test_feed_end_to_end

易错处（改本文件时先读）：
    · 测试 4 若两次都返回 None 会造成**假绿**，故首次为 None 时 skip，不算通过。
    · delisted 的判据必须是「重试耗尽 **且** 最后 K 线过旧」两条同时成立：
      只满足前者是数据缺口（应保持 undecidable 重试），只满足后者可能是源延迟。
      误判代价不对称 —— 把活票判成退市会污染战绩。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

# ── 判据用到的口径（与实现共享，不许在测试里另写一份阈值）────────────────
# 若实现侧改了 env 名/默认值，这里要同步，否则守卫失效。
STALE_EXIT_DATE = "2025-01-01"      # 远早于任何合理阈值 ⇒ 必判「过旧」
RETRY_EXHAUSTED = 99                # 远大于 QD_CLAIM_MAX_RETRY（默认 5）


def _stale_actual() -> dict:
    """长期无数据（退市/长期停牌）的 actual 形态。"""
    return {
        "pnl_pct": 0.0, "hold_days": 0, "hold_days_expected": 3,
        "direction": "flat", "exit_date": STALE_EXIT_DATE, "truncated": True,
    }


def _fresh_actual() -> dict:
    """正常标的：窗口走完、数据是新的。"""
    recent = (date.today() - timedelta(days=3)).isoformat()
    return {
        "pnl_pct": 1.5, "hold_days": 3, "hold_days_expected": 3,
        "direction": "up", "exit_date": recent, "truncated": False,
    }


# ═══════════════════════════════════════════════════════════════
#  RED：修复目标（当前必失败）
# ═══════════════════════════════════════════════════════════════

def test_delisted_verdict_exists(monkeypatch):
    """退市/长期停牌必须单列 delisted，而不是混进 data_missing（v4 G18）。"""
    from app.agent.chain import resolver

    monkeypatch.setattr(resolver, "fetch_actual", lambda *a, **k: _stale_actual())
    claim = {
        "subject": "999999", "subject_kind": "stock", "horizon": "T+3",
        "verify_fail_count": RETRY_EXHAUSTED,
    }
    out = resolver._resolve_finance(claim, date.today(), False)

    assert out.get("verdict") == "delisted", (
        "退市/长期停牌仍被归为 "
        f"{out.get('verdict')!r}（attribution={out.get('attribution')!r}）："
        "data_missing 在 weight_feed 里不计入 ⇒ 最差的尾巴被系统性抹掉 ⇒ "
        "经验库下限被抬高。修法见 v5 实施计划 任务 B1。"
    )
    # verdict_score 必须是 0.0 而不是 None：None 在 weight_feed 同样不计入，等于没修。
    assert out.get("verdict_score") == 0.0, (
        f"delisted 的 verdict_score 必须是 0.0（按最差路径计入），当前为 "
        f"{out.get('verdict_score')!r}；给 None 会被 weight_feed 跳过，等于白修。"
    )


def test_delisted_counted_in_weight_feed():
    """_VERDICT_SCORE 必须给 delisted 一个分数，否则它进不了权重。"""
    from app.agent.chain import weight_feed

    assert weight_feed._VERDICT_SCORE.get("delisted") == 0.0, (
        f"_VERDICT_SCORE 缺少 delisted: 0.0（当前 {weight_feed._VERDICT_SCORE}）。"
        "undecidable/data_missing 不计入是设计，但 delisted 是**确定的最差结果**，必须计入。"
    )


def test_delisted_in_counted_verdicts_whitelist():
    """★ 第三条（易漏）：光改 _VERDICT_SCORE 不够，聚合 SQL 的白名单也要放行 delisted。

    collect_domain_stats 的 SQL 是 `WHERE r.verdict IN ('hit','partial','miss')`，
    若不放行 delisted，前面两处改了也进不了聚合 —— 这是典型的「改了三处漏一处」。
    ⇒ 要求把白名单抽成模块常量 _COUNTED_VERDICTS，测试与 SQL 共用同一份。
    """
    from app.agent.chain import weight_feed

    wl = getattr(weight_feed, "_COUNTED_VERDICTS", None)
    assert wl is not None, (
        "weight_feed 缺少 _COUNTED_VERDICTS 常量：聚合 SQL 的 verdict 白名单目前硬编码在 "
        "collect_domain_stats() 里，测试无法守住它。请把白名单抽成模块级常量，SQL 与测试共用。"
    )
    assert "delisted" in wl, f"_COUNTED_VERDICTS 未包含 delisted（当前 {tuple(wl)}）"


# ═══════════════════════════════════════════════════════════════
#  RED：G24 —— 一 claim 多条 resolution 不得重复计票（2026-10-13 新发现）
#
#  立项依据（实测，不是推断）：
#    qd_agent_resolutions 共 16 条，却只对应 **10 个 claim** —— claim 89~94 各有 2 条。
#    成因：account_store.save_resolution() 是纯 INSERT（无 UPSERT），同一 claim
#    每次重新判定都**新增一行**（= 判定留痕，这个语义本身要保留）。
#    但 weight_feed.collect_domain_stats() 的 SQL 直接 `FROM qd_agent_resolutions r`
#    全表 JOIN + GROUP BY ⇒ **同一 claim 的每一条留痕都被当成一票**。
#
#  为什么现在没爆、但必须现在修：
#    当前 n=7 侥幸正确，只因为那 6 条重复行恰好都是 undecidable（不在白名单里）。
#    一旦对历史行做重算、或 claim 重试后从 undecidable 翻成 hit/miss，
#    同一 claim 就会被数两次 ⇒ 唯一的 finance/n=7 基线当场虚涨。
#    ⇒ **统计口径必须先于重算修好**，否则重算就是主动制造污染。
#
#  裁决（2026-10-13）：保留「一 claim 多条 = 判定留痕」的语义，
#  只把统计口径改成「每 claim 只取最新一条」，不动 save_resolution。
# ═══════════════════════════════════════════════════════════════

def _rows_dup() -> list:
    """同 claim 两条留痕（旧 undecidable + 新 hit）+ 另一个 claim 一条 miss。"""
    return [
        {"claim_id": 89, "res_id": 13, "domain": "finance", "verdict": "undecidable"},
        {"claim_id": 89, "res_id": 19, "domain": "finance", "verdict": "hit"},
        {"claim_id": 90, "res_id": 20, "domain": "finance", "verdict": "miss"},
    ]


def test_dedup_keeps_latest_resolution_per_claim():
    """去重函数：同一 claim 多条留痕时，只保留最新一条（res_id 最大）。"""
    from app.agent.chain import weight_feed

    dedup = getattr(weight_feed, "_dedup_latest", None)
    assert dedup is not None, (
        "weight_feed 缺少 _dedup_latest()：一 claim 多条 resolution 目前没有去重口径，"
        "聚合会把每次重新判定都当成一票。请把去重抽成纯函数，便于离线守口径。"
    )
    out = dedup(_rows_dup())
    ids = sorted(r["res_id"] for r in out)
    assert ids == [19, 20], (
        f"去重后应只保留每个 claim 最新的一条（期望 res_id=[19, 20]），实际 {ids}。"
        "注意不能按 verdict 过滤来「凑」结果 —— 去重的依据是**时间顺序**，"
        "不是哪条看起来更有用。"
    )


def test_domain_stats_one_vote_per_claim():
    """★ 核心：3 行明细里 claim 89 出现两次 ⇒ 聚合 n 必须是 2，不是 3。"""
    from app.agent.chain import weight_feed

    agg = getattr(weight_feed, "_aggregate_domain", None)
    dedup = getattr(weight_feed, "_dedup_latest", None)
    assert agg is not None, (
        "weight_feed 缺少 _aggregate_domain()：聚合逻辑仍埋在 SQL 的 GROUP BY 里，"
        "既无法离线测，也无法在将来按 condition_key 分组（P2/P3 需要）。"
        "请把「计数 + 计分」抽成纯函数，SQL 只负责取明细。"
    )
    if dedup is None:
        pytest.fail("缺少 _dedup_latest()：见上一条测试的说明")
    stats = agg(dedup(_rows_dup()))
    fin = next((s for s in stats if s.get("domain") == "finance"), None)
    assert fin is not None, f"未聚合出 finance：{stats}"
    assert fin["n"] == 2, (
        f"重复计票：2 个 claim 的明细被算成 n={fin['n']}（应为 2）。"
        "claim 89 有两条留痕（旧 undecidable + 新 hit），只能算一票，"
        f"否则 finance/n 会随重试次数虚涨。聚合结果：{fin}"
    )
    # hit 1 票 + miss 1 票 ⇒ (1 + 0)/2 = 0.5
    assert fin["win_rate"] == 0.5, (
        f"win_rate 应为 0.5（hit 1 票 / miss 1 票 / n=2），实际 {fin['win_rate']}。"
        f"完整结果：{fin}"
    )


def test_domain_stats_counts_delisted_in_denominator():
    """delisted 必须计入分母并拉低胜率（v4 G18 的另一半：不能只是「有这个枚举」）。"""
    from app.agent.chain import weight_feed

    agg = getattr(weight_feed, "_aggregate_domain", None)
    dedup = getattr(weight_feed, "_dedup_latest", None)
    if agg is None or dedup is None:
        pytest.skip("_aggregate_domain/_dedup_latest 尚未抽出（见前两条 RED）")
    rows = [
        {"claim_id": 1, "res_id": 1, "domain": "finance", "verdict": "hit"},
        {"claim_id": 2, "res_id": 2, "domain": "finance", "verdict": "miss"},
        {"claim_id": 3, "res_id": 3, "domain": "finance", "verdict": "delisted"},
        {"claim_id": 4, "res_id": 4, "domain": "finance", "verdict": "undecidable"},
    ]
    fin = next(s for s in agg(dedup(rows)) if s["domain"] == "finance")
    assert fin["n"] == 3, (
        f"n 应为 3（hit + miss + delisted，undecidable 不计入），实际 {fin['n']}。"
        "delisted 是**确定的最差结果**，不是「不知道」，必须占分母。"
    )
    assert fin["win_rate"] == round(1.0 / 3, 4), (
        f"win_rate 应为 {round(1/3, 4)}（1 个 hit / 3 票），实际 {fin['win_rate']}。"
        "若 delisted 没被拉进来，胜率会被系统性抬高 —— 这正是幸存者偏差的样子。"
    )


# ═══════════════════════════════════════════════════════════════
#  守卫：当前应通过（防改坏）
# ═══════════════════════════════════════════════════════════════

def test_normal_stock_not_delisted(monkeypatch):
    """反向守卫：正常标的不得被误判为 delisted（误判会污染战绩）。"""
    from app.agent.chain import resolver

    monkeypatch.setattr(resolver, "fetch_actual", lambda *a, **k: _fresh_actual())
    claim = {
        "subject": "600519", "subject_kind": "stock", "horizon": "T+3",
        "verify_fail_count": 0,
    }
    out = resolver._resolve_finance(claim, date.today(), False)

    assert out.get("verdict") != "delisted", (
        "正常标的被误判为 delisted：退市判据过宽。判据必须同时要求"
        "「重试耗尽」与「最后 K 线过旧」，只满足其一不得定论。"
    )


def test_historical_return_is_reproducible():
    """历史日期的收益必须可复现（v4 §28.5 / v5 G21）。

    ⚠️ 若两次都返回 None 会是**假绿**，故首次为 None 时 skip 而非通过。
    """
    from app.agent.chain import evaluator

    target = date.today() - timedelta(days=45)
    try:
        first = evaluator._get_actual_return("600519", target, 3)
    except Exception as e:  # 数据源不可用 ⇒ 无法验证，不算失败也不算通过
        pytest.skip(f"取数异常，无法验证可复现性：{type(e).__name__}: {e}")

    if first is None:
        pytest.skip(
            "取不到历史数据（返回 None），无法验证可复现性。"
            "若线上确实表现为 None，那本身就是标签不可用的证据，应另行排查。"
        )

    second = evaluator._get_actual_return("600519", target, 3)
    assert first == second, (
        f"同一历史日期 {target} 两次取值不一致：\n  第一次 {first}\n  第二次 {second}\n"
        "疑似取数窗口不足（get_kline 只取 hold_days+10 根）导致 base_idx 定位漂移，"
        "或走了实时源。修法见 v5 实施计划 任务 B3。"
    )


def test_exit_date_matches_hold_days():
    """★ B3 的真问题（2026-10-13 实测新发现）：exit_date 必须与「from_date + hold_days 个交易日」对齐。

    实测：from_date=2026-08-29、hold_days=3 ⇒ 期望 exit ≈ 2026-09-01，
    但实际返回 **exit_date=2026-09-21**（偏了 20 天）。
    两次调用**确实一致**（可复现性没问题，故 G21 的「不可复现」表述不成立），
    但**定位是错的** —— 疑似 get_kline 只取 hold_days+10 根导致 base_idx 找不到
    from_date，从而落到了别的锚点。这类「稳定地算错」比「每次不一样」更危险，
    因为它不会报错，只会安静地污染每一个 T+N 标签。
    """
    from app.agent.chain import evaluator
    from app.utils import trading_calendar as tc

    # ★ from_date 必须取**确定的交易日**。若取到周末/节假日，则
    #   「base_idx 指向其后第一个交易日 + hold_days」与「next_trading_day(from_date, hold_days)」
    #   两种口径会天然差一天 —— 那样本测试就变成在争论口径，而不是在测定位 bug。
    target = date.fromisoformat(tc.prev_trading_day(n=45))
    try:
        got = evaluator._get_actual_return("600519", target, 3)
    except Exception as e:
        pytest.skip(f"取数异常，无法验证：{type(e).__name__}: {e}")
    if got is None:
        pytest.skip("取不到历史数据（None），无法验证定位正确性")

    expected = tc.next_trading_day(target.isoformat(), 3)
    assert got.get("exit_date") == expected, (
        f"exit_date 与 hold_days 不对齐：from_date={target} + 3 个交易日 应为 "
        f"{expected}，实际 {got.get('exit_date')}。\n"
        f"完整返回：{got}\n"
        "这会让每一个 T+N 标签都算在错误的窗口上，且因为结果稳定而不会报错。"
    )


def test_feed_end_to_end():
    """端到端守卫：collect_domain_stats 必须能聚合出有效 domain。

    ⚠️ 本条在 pytest 下**默认 skip**：tests/conftest.py 会把 psycopg2 换成 mock
    （`sys.modules['psycopg2'] = _pg`），DB 不可用是测试环境的既定设计，不是链路故障。
    要真验这条，可在能连真库的环境中单独跑，或用手工脚本验证
    （2026-10-13 手工实测：finance / n=7 / win_rate 0.5714，链路通）。
    """
    from app.agent.chain import weight_feed

    try:
        from app.utils.db import get_db_connection

        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("select 1")
            cur.fetchall()
            cur.close()
    except Exception as e:
        pytest.skip(f"测试环境无真实 DB（conftest 把 psycopg2 换成了 mock）：{type(e).__name__}: {e}")

    stats = weight_feed.collect_domain_stats()
    assert stats, "collect_domain_stats 返回空：聚合链路断了（JOIN 或 verdict 白名单出问题）"
    assert any(int(r.get("n") or 0) > 0 for r in stats), f"所有 domain 的 n 都为 0：{stats}"
