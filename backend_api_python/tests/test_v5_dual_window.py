"""v5 阶段 D3：双窗口标签（T+1 / T+3）守卫（TDD，2026-10-13）

裁决依据（见 `agent扩展设计-v5-实施计划.md` §3.5 任务 D3 的 2026-10-13 裁决）：
    **方案 D** —— 同一条 resolution 挂双窗口，claim **不翻倍**。

为什么必须「一次取数算多窗口」而不是「调两次」：
    `evaluator._get_actual_return` 每次调用都会重新 `CNStockDataSource().get_kline(...)`
    ⇒ 拆两条 claim / 调两次 = **网络取数真翻倍**。
    但它一次就拉回整段 K 线（`_need = _span + hold_days + 10`），
    T+1 的 exit 就是 `base_idx + 1` ⇒ **同一份数据里本来就躺着 T+1**。
    ⇒ 本测试用「get_kline 调用次数 == 1」把这条省钱性质钉死，防止后人改回两次调用。

语义红线（实现不得偏离）：
    · claim 的 `horizon` 仍**单值**，主判据仍 **T+3**；
    · `verdict` / `verdict_score` 列**只取 T+3** ⇒ `weight_feed` 计票口径不变；
    · **T+1 只报告、不进权重**（否则同一预测记两票，与 G24 同一类错误）；
    · 窗口集合走 env `QD_LABEL_WINDOWS`，**禁止硬编码**。

易错处（改本文件时先读）：
    · 假 K 线的 `date` 字段必须是**完整 ISO 日期**（`%Y-%m-%d`）。evaluator 的
      `base_idx` 靠 `date.fromisoformat` 定位，给 `"09-01"` 会恒 ValueError
      ⇒ base_idx=None ⇒ 函数返回 None ⇒ 测试**假绿**。
    · 本文件所有测试**不碰数据库**（pytest 下 psycopg2 被 conftest 换成 mock，
      是既定设计）。故只测 `_resolve_finance`，**不测** `resolve_claim`（后者写库）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List

import pytest

# 假 K 线起始日 —— 也是本文件所有测试的 from_date（决策日）
BASE_DAY = date(2026, 9, 1)


def _klines(n: int = 40) -> List[dict]:
    """构造 n 根连续日历日 K 线，收盘价**严格递增**（10.0, 10.1, 10.2 …）。

    递增是为了让 T+1 与 T+3 的收益**必然不同**，从而能断言「两个窗口不是同一份数据」。
    """
    out = []
    for i in range(n):
        d = BASE_DAY + timedelta(days=i)
        c = round(10.0 + i * 0.1, 2)
        out.append({"date": d.isoformat(), "open": c, "high": round(c + 0.05, 2),
                    "low": round(c - 0.05, 2), "close": c, "volume": 1000})
    return out


def _patch_kline(monkeypatch, rows=None, counter=None):
    """把 `CNStockDataSource.get_kline` 换成假源。

    counter 传入 `{"n": 0}` 时可统计**取数次数** —— 这是方案 D 的核心断言依据。
    """
    from app.data_sources import cn_stock

    def _fake(self, code, period, limit, *a, **k):
        if counter is not None:
            counter["n"] = int(counter.get("n") or 0) + 1
        src = rows if rows is not None else _klines()
        return src[:limit] if isinstance(limit, int) and limit > 0 else src

    monkeypatch.setattr(cn_stock.CNStockDataSource, "get_kline", _fake, raising=True)


# ═══════════════════════════════════════════════════════════════
#  RED：方案 D 的修复目标（实现前必失败）
# ═══════════════════════════════════════════════════════════════

def test_get_actual_return_returns_extra_windows(monkeypatch):
    """一次取数要能同时给出主窗口(T+3)与附加窗口(T+1)的结果。"""
    from app.agent.chain.evaluator import _get_actual_return

    _patch_kline(monkeypatch)
    out = _get_actual_return("600000", BASE_DAY, 3, with_extremes=True,
                             extra_holds=[1])
    assert out is not None, "假源下不应返回 None（若 None 说明 base_idx 定位失败 ⇒ 假绿）"
    wins = out.get("windows")
    assert isinstance(wins, dict), f"缺少 windows 键，实际键={sorted(out)}"
    assert "1" in wins and "3" in wins, f"windows 应含 1 与 3，实际={sorted(wins)}"
    # 收盘价递增 ⇒ T+1 收益必须小于 T+3，且都不为 0（防「两个窗口其实同一份」）
    assert wins["1"]["pnl_pct"] < wins["3"]["pnl_pct"]
    assert wins["1"]["pnl_pct"] != 0.0 and wins["3"]["pnl_pct"] != 0.0


def test_extra_windows_cost_exactly_one_kline_fetch(monkeypatch):
    """★ 方案 D 的核心省钱断言：加附加窗口**不得**多取一次数。"""
    from app.agent.chain.evaluator import _get_actual_return

    counter = {"n": 0}
    _patch_kline(monkeypatch, counter=counter)
    _get_actual_return("600000", BASE_DAY, 3, with_extremes=True, extra_holds=[1])
    assert counter["n"] == 1, (
        f"get_kline 被调用 {counter['n']} 次：附加窗口若各取一次，网络成本会翻倍，"
        f"这正是方案 D 要避免的（方案 B/D′ 的代价）")


def test_short_window_complete_while_main_truncated(monkeypatch):
    """主窗口(T+3)数据没走完时，T+1 仍完整 ⇒ 可用于快速诊断。

    视界门②要求主窗口截断**不得定论**（保持 undecidable），
    但短窗口已可得、应如实记 truncated=False，不能跟着主窗口一起被抹掉。
    """
    from app.agent.chain.evaluator import _get_actual_return

    _patch_kline(monkeypatch, rows=_klines(2))   # 只有 2 根 ⇒ T+3 必截断
    out = _get_actual_return("600000", BASE_DAY, 3, with_extremes=True,
                             extra_holds=[1])
    assert out is not None
    assert out.get("truncated") is True, "只有 2 根 K 线，主窗口 T+3 必须标记截断"
    wins = out.get("windows") or {}
    assert wins.get("1", {}).get("truncated") is False, (
        "T+1 数据已完整（第 1 根就在），不该跟着主窗口一起被判截断")


def test_fetch_actual_labels_windows_with_horizon(monkeypatch):
    """`fetch_actual` 输出的 windows 键必须是 **horizon 标签**（T+1/T+3），不是裸数字。"""
    from app.agent.chain import resolver

    _patch_kline(monkeypatch)
    a = resolver.fetch_actual("600000", "stock", "T+3", BASE_DAY)
    assert a is not None
    wins = a.get("windows") or {}
    assert set(wins) >= {"T+1", "T+3"}, f"windows 键应为 horizon 标签，实际={sorted(wins)}"
    # 每个窗口都要是可独立判定的 actual 形态（与主 actual 同构）
    for k in ("T+1", "T+3"):
        w = wins[k]
        assert "pct" in w and "dir" in w and "truncated" in w, f"{k} 窗口形态不全: {sorted(w)}"
    assert wins["T+1"]["pct"] != wins["T+3"]["pct"]


def test_deviation_has_both_windows(monkeypatch):
    """判定偏差要按窗口分列记（T+1 与 T+3 各一份），供后续分层诊断。"""
    from app.agent.chain import resolver

    _patch_kline(monkeypatch)
    claim = {"id": 1, "domain": "finance", "claim_type": "direction",
             "subject": "600000", "subject_kind": "stock", "horizon": "T+3",
             "predicted": {"dir": "bullish"}, "verify_fail_count": 0}
    res = resolver._resolve_finance(claim, BASE_DAY, False)
    assert res.get("verdict") != "undecidable", f"假源下不该无法判定: {res}"
    dv = res.get("deviation") or {}
    wins = dv.get("windows") or {}
    assert set(wins) >= {"T+1", "T+3"}, f"deviation.windows 缺失，实际键={sorted(dv)}"
    assert "direction" in wins["T+1"] and "direction" in wins["T+3"]


def test_verdict_uses_main_window_only(monkeypatch):
    """★ 红线：verdict 只取主窗口 T+3，T+1 不得参与定论。

    构造：T+1 跌、T+3 涨（预测 bullish）⇒ 若 verdict 被 T+1 带偏就会判 miss，
    正确行为是 hit。这条同时锁死「T+1 只报告不进权重」。
    """
    from app.agent.chain import resolver

    # 第 1 根跌（相对决策日），第 3 根涨回并超过
    rows = _klines()
    rows[1]["close"] = 9.5          # T+1: -5.0%
    rows[2]["close"] = 9.8
    rows[3]["close"] = 10.4         # T+3: +4.0%
    _patch_kline(monkeypatch, rows=rows)

    claim = {"id": 1, "domain": "finance", "claim_type": "direction",
             "subject": "600000", "subject_kind": "stock", "horizon": "T+3",
             "predicted": {"dir": "bullish"}, "verify_fail_count": 0}
    res = resolver._resolve_finance(claim, BASE_DAY, False)
    a_wins = (res.get("actual") or {}).get("windows") or {}
    assert a_wins.get("T+1", {}).get("dir") == "bearish", "前提：T+1 必须是跌"
    assert a_wins.get("T+3", {}).get("dir") == "bullish", "前提：T+3 必须是涨"
    assert res.get("verdict") == "hit", (
        f"verdict 被 T+1 带偏了（实际 {res.get('verdict')}）：主判据必须取 T+3")


def test_windows_come_from_env(monkeypatch):
    """窗口集合走 env `QD_LABEL_WINDOWS`，禁止硬编码窗口列表。"""
    from app.agent.chain import resolver

    _patch_kline(monkeypatch)
    monkeypatch.setenv("QD_LABEL_WINDOWS", "T+5")
    a = resolver.fetch_actual("600000", "stock", "T+3", BASE_DAY)
    wins = (a or {}).get("windows") or {}
    assert "T+5" in wins and "T+1" not in wins, (
        f"env 未生效：QD_LABEL_WINDOWS=T+5 时窗口应为 {{T+3,T+5}}，实际={sorted(wins)}")


# ═══════════════════════════════════════════════════════════════
#  守卫：防改动改坏既有行为（改完实现后必须仍绿）
# ═══════════════════════════════════════════════════════════════

def test_main_window_unchanged_without_extra_holds(monkeypatch):
    """不加 extra_holds 时行为与改动前**完全一致**（B3 可复现性不得被破坏）。"""
    from app.agent.chain.evaluator import _get_actual_return

    _patch_kline(monkeypatch)
    out = _get_actual_return("600000", BASE_DAY, 3, with_extremes=True)
    assert out is not None
    for k in ("pnl_pct", "hold_days", "direction", "exit_date", "high", "low"):
        assert k in out, f"既有返回字段 {k} 丢了 ⇒ 旧链路会炸"
    # 两次调用结果必须一致（B3）
    out2 = _get_actual_return("600000", BASE_DAY, 3, with_extremes=True)
    assert out == out2, "同一历史日期两次调用结果不一致（B3 回归）"


def test_legacy_resolution_without_windows_is_tolerated():
    """老 resolution 行没有 windows 键 ⇒ 读取侧**不得炸**（向前兼容）。"""
    from app.agent.chain import resolver

    legacy = {"pct": 1.2, "dir": "bullish", "high": 1.0, "low": 0.9,
              "close": 1.0, "as_of": None, "source": "x", "truncated": False}
    dev = resolver.compute_deviation(
        {"claim_type": "direction", "predicted": {"dir": "bullish"}}, legacy)
    assert dev["direction"]["hit"] is True
    assert resolver.verdict_by_rule(
        {"claim_type": "direction", "predicted": {"dir": "bullish"}}, legacy, dev
    )["verdict"] == "hit"


def test_weight_feed_unaffected_by_windows():
    """★ G24 守卫：resolution 多了 windows 键，计票口径**不变**（仍按 verdict 列一 claim 一票）。"""
    from app.agent.chain.weight_feed import _dedup_latest, _aggregate_domain

    rows = [
        {"claim_id": 1, "res_id": 11, "domain": "finance", "verdict": "hit"},
        {"claim_id": 2, "res_id": 12, "domain": "finance", "verdict": "miss"},
        {"claim_id": 2, "res_id": 13, "domain": "finance", "verdict": "partial"},
    ]
    agg = _aggregate_domain(_dedup_latest(rows))
    assert len(agg) == 1
    assert agg[0]["n"] == 2, f"一 claim 一票被破坏: n={agg[0]['n']}"


def test_t1_not_counted_in_weight_stats():
    """T+1 不进权重：`_aggregate_domain` 只读 verdict 列，windows 里的 T+1 不参与。"""
    from app.agent.chain.weight_feed import _aggregate_domain

    rows = [{"claim_id": 1, "res_id": 11, "domain": "finance", "verdict": "miss",
             "windows": {"T+1": {"verdict": "hit"}, "T+3": {"verdict": "miss"}}}]
    agg = _aggregate_domain(rows)
    assert agg[0]["n"] == 1 and agg[0]["win_rate"] == 0.0, (
        "windows 里的 T+1 被计进权重了 ⇒ 同一预测记了两票（G24 同类错误）")
