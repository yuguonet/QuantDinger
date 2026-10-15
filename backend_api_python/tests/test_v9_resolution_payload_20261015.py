# -*- coding: utf-8 -*-
"""v9 resolution 载荷与判定记账守卫（2026-10-15，exp_grind 实证带出的三个真缺陷）。

## 背景：实证跑出来的断裂

`python -m app.agent.scripts.exp_grind --batch 2` 首跑，日志显示
`auto_evaluate: due=86 resolved=86 errors=0`（一片大好），但库里
resolutions 一条没多 —— 全量 stderr 里躺着 **82 条**
`[AccountStore] 写 resolution 失败(忽略): TypeError: Object of type date
is not JSON serializable`。三个缺陷叠出这条静默断裂：

| # | 缺陷 | 根因 | 后果 |
|---|---|---|---|
| B1 | resolution 写不进去 | `_actual_view` 把 `date` 对象放进 `actual`（含 `windows` 嵌套层），`save_resolution` 的 `json.dumps` 抛 TypeError，被 except 吞掉 | 追责闭环**实际是断的**（10-13 D3 双窗口引入至今） |
| B2 | 报表说谎 | `resolve_due_claims` 把 `resolution_id=None`（写库失败）也计成 `resolved` | errors=0 的漂亮报表，零落库 |
| B3 | 退市分支永不可达 | `_resolve_finance` 读 `actual.get("exit_date")`，而 `fetch_actual` 从不放这个键 | G18 防幸存者偏差的 delisted 单列静默失效 |

## 本文件守什么

① `_actual_view` 产物（含 windows 嵌套）必须**可 JSON 序列化**（B1 根因守卫）；
② 从 `_get_actual_return` 原始结果到 `_resolve_finance` 返回值，整条链的载荷
   必须可 `json.dumps`（B1 端到端守卫，mock 点在最上游）；
③ 写库失败必须计 `errors` 而不是 `resolved`（B2 记账诚实守卫）；
④ 重试耗尽 + K 线陈旧 ⇒ 必须判 `delisted`（B3 分支可达性守卫，正反两例）。

## 易错处

· mock 点选 **`evaluator._get_actual_return`**（最上游）而不是 `fetch_actual`：
  这样真实的 `_actual_view` / `fetch_actual` 都在链里，B1 的根因修复
  （生产者的类型）才真正被覆盖 —— mock `fetch_actual` 会绕过根因层，恒绿。
· B2 用例必须 patch `resolver.resolve_claim` 与 `account_store.query_due_claims`
  两个模块属性（`resolve_due_claims` 在函数内 import，运行期查属性）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.agent.chain import resolver as rs


def _raw_kline_result(exit_day: str = "2026-10-09", pnl: float = 1.5,
                      truncated: bool = False) -> dict:
    """造一条 `_get_actual_return` 形态的原始结果（含双窗口 windows）。"""
    bar = {"pnl_pct": pnl, "direction": "up", "exit_date": exit_day,
           "hold_days": 1, "high": 101.0, "low": 99.0, "exit_close": 100.5,
           "truncated": truncated, "hold_days_expected": 1}
    return {**bar, "windows": {"3": dict(bar, hold_days=3)}}


# ═══════════════════════════════════════════════════════════════
#  B1：载荷必须可 JSON 序列化（根因在生产者 _actual_view）
# ═══════════════════════════════════════════════════════════════

def test_actual_view_payload_is_json_serializable():
    """★ `_actual_view` 的产物整个要进 `qd_agent_resolutions.actual`（JSON 列）
    —— 含 `windows` 嵌套层，一处 date 对象就整条写不进去。

    修复前：`as_of` 是 `date` 对象，`json.dumps` 抛 TypeError ⇒ 82/86 条判定
    静默丢失（10-13 D3 双窗口引入至 10-15 实测发现）。
    """
    v = rs._actual_view(_raw_kline_result())
    dumped = json.dumps(v, ensure_ascii=False)      # 不抛即过
    assert isinstance(v["as_of"], str) and v["as_of"] == "2026-10-09", (
        f"as_of 必须是 ISO 字符串（不是 date 对象）: {v['as_of']!r}")
    # 嵌套层同样必须干净（浅层 dict 推导罩不住 windows —— 那正是旧病根）
    for label, w in (v.get("windows") or {}).items():
        assert isinstance(w["as_of"], str), f"windows[{label}] 的 as_of 不是字符串"


def test_resolve_finance_payload_is_json_serializable(monkeypatch):
    """端到端：从最上游原始 K 线结果到 `_resolve_finance` 返回值必须可落库。

    mock 点选 `evaluator._get_actual_return`（最上游）—— 真实的
    `_actual_view` / `fetch_actual` 都在链里，根因层被真正覆盖。
    """
    monkeypatch.setattr("app.agent.chain.evaluator._get_actual_return",
                        lambda *a, **k: _raw_kline_result(pnl=1.5))
    claim = {"id": 42, "subject": "600519", "subject_kind": "stock",
             "horizon": "T+1", "claim_type": "direction",
             "predicted": {"dir": "bullish"}, "domain": "finance",
             "verify_fail_count": 0}
    res = rs._resolve_finance(claim, date(2026, 10, 8), use_judge=False)
    json.dumps(res, ensure_ascii=False)             # 不抛即过
    assert res["verdict"] == "hit", f"方向一致应判 hit: {res}"
    assert res["actual"]["as_of"] == "2026-10-09"


# ═══════════════════════════════════════════════════════════════
#  B2：写库失败必须计 errors，不许计 resolved（报表不许说谎）
# ═══════════════════════════════════════════════════════════════

def test_write_failure_counted_as_error_not_resolved(monkeypatch):
    """★ `resolution_id=None`（save_resolution fail-open）= 判定没落库 ⇒ 计 errors。

    旧代码无条件 `resolved += 1` ⇒ 86 条"已判定"实际零落库，报表一片大好。
    """
    import app.agent.chain.account_store as acs
    monkeypatch.setattr(acs, "query_due_claims",
                        lambda limit=50: [{"id": 7, "decision_id": 3,
                                           "exec_date": date(2026, 10, 1)}])
    monkeypatch.setattr(acs, "refresh_decision_summary", lambda did: None)
    monkeypatch.setattr(rs, "resolve_claim",
                        lambda *a, **k: {"resolution_id": None,
                                         "verdict": "hit"})
    stats = rs.resolve_due_claims(limit=10)
    assert stats["resolved"] == 0 and stats["errors"] == 1, (
        f"写库失败必须计 errors: {stats}")


def test_successful_resolution_counted_as_resolved(monkeypatch):
    """反向对照：真落库（resolution_id 有值）必须计 resolved —— 否则 B2 的修法
    会退化成"永远 errors"，把正常路径也判死（恒红守卫没有意义）。"""
    import app.agent.chain.account_store as acs
    monkeypatch.setattr(acs, "query_due_claims",
                        lambda limit=50: [{"id": 7, "decision_id": 3,
                                           "exec_date": date(2026, 10, 1)}])
    monkeypatch.setattr(acs, "refresh_decision_summary", lambda did: None)
    monkeypatch.setattr(rs, "resolve_claim",
                        lambda *a, **k: {"resolution_id": 99,
                                         "verdict": "hit",
                                         "verdict_score": 0.75})
    stats = rs.resolve_due_claims(limit=10)
    assert stats["resolved"] == 1 and stats["errors"] == 0, f"{stats}"


# ═══════════════════════════════════════════════════════════════
#  B3：退市分支可达性（exit_date → as_of 的死读修复）
# ═══════════════════════════════════════════════════════════════

def _stale_claim(fail_n: int = 5) -> dict:
    return {"id": 9, "subject": "600519", "subject_kind": "stock",
            "horizon": "T+1", "claim_type": "direction",
            "predicted": {"dir": "bullish"}, "domain": "finance",
            "verify_fail_count": fail_n}


def test_delisted_branch_reachable_when_retries_exhausted(monkeypatch):
    """★ 重试耗尽（≥QD_CLAIM_MAX_RETRY）且最后 K 线陈旧（>QD_DELISTED_STALE_DAYS）
    ⇒ 必须判 delisted（score=0.0，计入最差路径，防幸存者偏差 G18）。

    旧代码读 `actual.get("exit_date")`（fetch_actual 从不放该键）⇒ 分支永不可达。
    """
    stale = (date.today() - timedelta(days=40)).isoformat()
    monkeypatch.setattr(rs, "fetch_actual",
                        lambda *a, **k: {"pct": -5.0, "dir": "bearish",
                                         "as_of": stale, "source": "x",
                                         "hold_days": 1, "hold_days_expected": 1,
                                         "truncated": False, "windows": {}})
    res = rs._resolve_finance(_stale_claim(fail_n=5), date.today(), use_judge=False)
    assert res["verdict"] == "delisted" and res["verdict_score"] == 0.0, (
        f"重试耗尽+数据陈旧必须判 delisted: {res}")


def test_delisted_branch_not_taken_when_retries_remain(monkeypatch):
    """反向对照：fail_count 未耗尽（数据可能只是迟到）⇒ 不得判 delisted，
    必须走常规判定（否则每条暂时取不到数的 claim 都会被毒丸化）。"""
    stale = (date.today() - timedelta(days=40)).isoformat()
    monkeypatch.setattr(rs, "fetch_actual",
                        lambda *a, **k: {"pct": -5.0, "dir": "bearish",
                                         "as_of": stale, "source": "x",
                                         "hold_days": 1, "hold_days_expected": 1,
                                         "truncated": False, "windows": {}})
    res = rs._resolve_finance(_stale_claim(fail_n=1), date.today(), use_judge=False)
    assert res["verdict"] != "delisted", f"重试未尽不能判 delisted: {res}"
