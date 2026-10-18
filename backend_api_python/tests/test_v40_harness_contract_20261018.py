# -*- coding: utf-8 -*-
"""F22 A 口径对照落地：harness 契约一致性断言 T1–T8（2026-10-18）。

出处：`tmp/F22_harness口径对照方案_20261018.md` §3 —— 用户 2026-10-18 裁定选
**A 口径对照**且 **T8 同批做**。上游事实源（mimoagent 0.1.0，2026-10-18 实测）：
`run/utils/save.py:30 save_traj` / `agents/blackbox/claude_code.py:391 _fold_usage`
（codex.py:426 同族）/ `environments/rubric_judge.py:259 aggregate_rubric_scores`。

原则（方案 §3）：断言只钉**口径不变量**（范围/含内关系/round-trip），不重实现
上游逻辑；纯函数级、零网络零外部依赖，可进 CI。

不变量清单：
  T1 根节点 cached_tokens ≤ total_tokens（非 None 时）——"含在 total 内"防被写成增量
  T2 cached/total ∈ [0,1]（total>0）——抓住"增量 vs 含内"错配
  T3 total/cached 非负；None 与 0 不互转（null≠0，F10 防偏）
  T4 to_dict→from_dict round-trip 幂等（date iso / factors / children 递归）
  T5 layer∈{chain,skill,tool}；direction/action/timeframe/status 取值域合法
  T6 score_answer total/dims∈[0,100]；agreement_rate 四元组口径
  T7 VERDICT_SCORE∈[0,1] 且键集==COUNTED_VERDICTS（键集与 test_v5_calib_feed 合并）
  T8 to_dict 带 schema_version/trajectory_format；from_dict 认旧形状、
     外来 format / 更高 version / 非 int version 一律 fail-loud

⚠️ 导入纪律（4e 教训）：qd_bench 等入口脚本模块级把 `app/agent` 插进 sys.path，
   收集期 import 会污染进程全局判据（qd_smoke test_29/30 曾被带红）⇒ 本文件
   全部**函数内延迟导入**，模块级只留 stdlib/pytest。
"""
from __future__ import annotations

from datetime import date

import pytest


# ── T1 cached ≤ total（"含在 total 内"，不是可叠加增量）────────────────

def test_t1_cached_within_total_gateway_shape():
    """网关回填形状（v17m 实测 9357/20000）经 upstream_compat 落 EvalNode。"""
    from types import SimpleNamespace

    from app.agent.chain.schema import EvalNode
    from app.agent.upstream_compat import read_cache_tokens, read_total_tokens

    model = SimpleNamespace(token_stats=SimpleNamespace(
        total_tokens=20000, cache_read_tokens=9357))

    node = EvalNode(name="chain", layer="chain",
                    total_tokens=read_total_tokens(model),
                    cached_tokens=read_cache_tokens(model))
    assert node.total_tokens == 20000 and node.cached_tokens == 9357
    assert node.cached_tokens <= node.total_tokens


def test_t1_relation_survives_serialization():
    """含内关系过 to_dict/from_dict 不变（防回归成"可叠加增量"）。"""
    from app.agent.chain.schema import EvalNode

    root = EvalNode(name="chain", total_tokens=20000, cached_tokens=9357)
    d = root.to_dict()
    assert d["cached_tokens"] <= d["total_tokens"]
    back = EvalNode.from_dict(d)
    assert back.cached_tokens <= back.total_tokens


# ── T2 缓存率 ∈ [0,1] ────────────────────────────────────────────────

@pytest.mark.parametrize("total,cached", [(20000, 9357), (20000, 0), (20000, 20000)])
def test_t2_cache_ratio_within_unit_interval(total, cached):
    """total>0 时 cached/total ∈ [0,1]——缓存率不可能 >100%。"""
    from app.agent.chain.schema import EvalNode

    node = EvalNode(name="t", total_tokens=total, cached_tokens=cached)
    d = node.to_dict()
    assert d["total_tokens"] > 0
    assert 0.0 <= d["cached_tokens"] / d["total_tokens"] <= 1.0


def test_t2_no_ratio_when_total_zero_or_none():
    """total=0/None 时不存在比率——不能除零，也不能拿 cached 造数。"""
    from app.agent.chain.schema import EvalNode

    assert EvalNode(name="t").total_tokens is None
    assert EvalNode(name="t", total_tokens=0).cached_tokens is None


# ── T3 非负 + None≠0 ─────────────────────────────────────────────────

def test_t3_none_and_zero_not_interchanged():
    """None（未上报）与 0（真实零命中）是不同事实，round-trip 不互转。"""
    from app.agent.chain.schema import EvalNode

    zero = EvalNode(name="t", total_tokens=0, cached_tokens=0).to_dict()
    missing = EvalNode(name="t").to_dict()
    assert zero["total_tokens"] == 0 and zero["cached_tokens"] == 0
    assert missing["total_tokens"] is None and missing["cached_tokens"] is None
    assert EvalNode.from_dict(zero).cached_tokens == 0
    assert EvalNode.from_dict(missing).cached_tokens is None


def test_t3_recorded_values_non_negative():
    """网关 usage 非负 ⇒ read_* 原值透传（不裁切、不加料、不变号）。"""
    from types import SimpleNamespace

    from app.agent.upstream_compat import read_cache_tokens, read_total_tokens

    hit = SimpleNamespace(token_stats=SimpleNamespace(
        total_tokens=20000, cache_read_tokens=9357))
    zero = SimpleNamespace(token_stats=SimpleNamespace(
        total_tokens=0, cache_read_tokens=0))

    assert read_total_tokens(hit) == 20000
    assert read_cache_tokens(hit) == 9357
    assert read_total_tokens(zero) == 0
    assert read_cache_tokens(zero) == 0


# ── T4 round-trip 幂等 ───────────────────────────────────────────────

def test_t4_roundtrip_idempotent_full_node():
    """全字段 round-trip：dataclass eq + date iso 往返 + 二次 round-trip 幂等。"""
    from app.agent.chain.schema import EvalNode, FactorItem

    node = EvalNode(
        name="chain_x", layer="chain", step_order=3,
        exec_date=date(2026, 10, 18), exit_date=date(2026, 10, 21),
        stock_code="600519", stock_name="贵州茅台",
        score=72.5, direction="bullish", action="buy", signal="逢低关注",
        confidence=0.8, timeframe="T+1",
        factors=[FactorItem(name="ma", value="20 日均线", score=80.0, weight=1.5)],
        output_data={"k": [1, 2]}, analysis="结论文字", plan="计划",
        session_id="s1", user_query="分析600519", model="mimo-x",
        total_tokens=20000, cached_tokens=9357,
        tools_called=["get_kline"], missing_data=[], data_source="sina",
        status="ok", error="", elapsed_ms=1234.5,
        exit_reason="take_profit", pnl_pct=2.5, hold_days=3, correct=True,
        calibration=1.02, human_reviewed=False, human_verdict="",
    )
    d = node.to_dict()
    assert d["exec_date"] == "2026-10-18" and d["exit_date"] == "2026-10-21"
    back = EvalNode.from_dict(d)
    assert back == node
    assert back.exec_date == date(2026, 10, 18)
    assert EvalNode.from_dict(back.to_dict()) == back


def test_t4_roundtrip_children_recursive():
    """children 递归序列化；from_dict 不含 children（契约明示，store.load_tree 负责）。"""
    from app.agent.chain.schema import EvalNode

    root = EvalNode(name="chain", layer="chain", exec_date=date(2026, 10, 18))
    child = EvalNode(name="market-screener", layer="skill", score=61.0)
    root.add_child(child)
    d = root.to_dict()
    assert len(d["children"]) == 1 and d["children"][0]["name"] == "market-screener"
    assert EvalNode.from_dict(d).children == []
    back_child = EvalNode.from_dict(d["children"][0])
    assert back_child.name == "market-screener" and back_child.score == 61.0
    assert EvalNode.from_dict(back_child.to_dict()) == back_child


# ── T5 树结构与取值域 ────────────────────────────────────────────────

def test_t5_layer_and_value_domains():
    """layer 三值；direction/action/timeframe/status 取值域合法。

    timeframe 域唯一定源 `chain/label_spec.HOLDS`（标签→持有交易日数），
    空串 = 未标注（v17m 时代存量根节点 1256 条全空）。
    """
    from app.agent.chain.label_spec import HOLDS
    from app.agent.chain.schema import (Action, Direction, EvalNode, Layer,
                                        Status, VETO_SCORE)

    assert {l.value for l in Layer} == {"chain", "skill", "tool"}
    ok = EvalNode(layer="chain", score=88.0, direction="bullish", action="buy",
                  timeframe="T+1", status="ok", confidence=0.8)
    assert ok.direction in {d.value for d in Direction}
    assert ok.action in {a.value for a in Action}
    assert ok.status in {s.value for s in Status}
    assert ok.timeframe in set(HOLDS) | {""}

    # score 域：[0,100] 或否决哨兵（VETO_SCORE=-1000），None=缺失
    veto = EvalNode(layer="chain", status="veto", score=VETO_SCORE)
    assert veto.score == VETO_SCORE
    for n in (ok, veto, EvalNode(name="x")):
        assert n.score is None or 0.0 <= n.score <= 100.0 or n.score == VETO_SCORE


# ── T6 评分尺度不越界 ────────────────────────────────────────────────

def test_t6_score_answer_ranges():
    """score_answer total/dims ∈ [0,100]，且确定性（同输入恒同输出）。"""
    from app.agent.scripts.qd_bench import score_answer

    task = {
        "id": "t1", "category": "个股分析", "weight": 1.0,
        "dims": ["coverage", "grounding", "claims", "structure"],
        "query": "分析600519",
        "baseline": {"must": ["has_direction", "has_numbers",
                              "has_horizon", "has_risk_note"]},
    }
    answer = ("结论：600519 短线看多。均线金叉，MACD 柱放大，预计涨幅 3%~5%。"
              "风险提示：若跌破 20 日均线则离场。")
    r = score_answer(task, answer)
    assert 0.0 <= r["total"] <= 100.0
    for k, v in r["dims"].items():
        assert 0.0 <= v <= 100.0, k
    assert r["must"]["total"] == 4
    assert score_answer(task, answer) == r


def test_t6_agreement_rate_contract():
    """agreement_rate → (rate∈[0,1], mad≥0, agree≤n, n≥0)；无交集 n=0。"""
    from app.agent.scripts.qd_bench import agreement_rate

    a = {"t1": {"score": 80.0}, "t2": {"score": 60.0}}
    b = {"t1": {"score": 82.0}, "t2": {"score": 40.0}}
    rate, mad, agree, n = agreement_rate(a, b, tol=5)
    assert n == 2 and agree == 1
    assert rate == 0.5 and mad == 11.0
    assert 0.0 <= rate <= 1.0 and mad >= 0.0 and agree <= n and n >= 0

    rate, mad, agree, n = agreement_rate(a, {}, tol=5)
    assert (rate, mad, agree, n) == (0.0, 0.0, 0, 0)


# ── T7 判定计分单一真源 ──────────────────────────────────────────────

def test_t7_verdict_score_domain_and_whitelist():
    """VERDICT_SCORE ∈ [0,1] 且键集==COUNTED_VERDICTS（单一真源不漂移）。

    键集断言与 test_v5_calib_feed 合并（那边钉 `wf._VERDICT_SCORE is
    sch.VERDICT_SCORE` 同一对象 + 键集），本文件补**值域**与逐项分值。
    """
    from app.agent.chain import schema as sch

    assert set(sch.VERDICT_SCORE) == set(sch.COUNTED_VERDICTS)
    for k, v in sch.VERDICT_SCORE.items():
        assert 0.0 <= v <= 1.0, k
    assert sch.VERDICT_SCORE == {"hit": 1.0, "partial": 0.5,
                                 "miss": 0.0, "delisted": 0.0}


# ── T8 序列化契约锚点（schema_version / trajectory_format）────────────

def test_t8_to_dict_carries_format_anchors():
    """to_dict/to_json 都带格式锚点（上游 save_traj 有 trajectory_format，我们对齐）。"""
    import json

    from app.agent.chain.schema import SCHEMA_VERSION, TRAJECTORY_FORMAT, EvalNode

    d = EvalNode(name="chain").to_dict()
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["trajectory_format"] == TRAJECTORY_FORMAT
    payload = json.loads(EvalNode(name="chain").to_json())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["trajectory_format"] == TRAJECTORY_FORMAT


def test_t8_from_dict_accepts_pre_anchor_shape():
    """向后兼容：T8 以前的 dump 无两键 ⇒ 按当前版解析（不是错误）。"""
    from app.agent.chain.schema import SCHEMA_VERSION, EvalNode

    d = EvalNode(name="chain", score=50.0).to_dict()
    d.pop("schema_version")
    d.pop("trajectory_format")
    back = EvalNode.from_dict(d)
    assert back.score == 50.0
    assert back.to_dict()["schema_version"] == SCHEMA_VERSION


def test_t8_from_dict_rejects_foreign_format():
    """外来 trajectory_format（如上游 "mimoagent" 形状）fail-loud，不静默错读。"""
    from app.agent.chain.schema import EvalNode

    d = EvalNode(name="chain").to_dict()
    d["trajectory_format"] = "mimoagent"
    with pytest.raises(ValueError, match="trajectory_format"):
        EvalNode.from_dict(d)


def test_t8_from_dict_rejects_newer_schema_version():
    """高于本模块支持的 schema_version ⇒ 拒绝用旧解析器读更新形状。"""
    from app.agent.chain.schema import SCHEMA_VERSION, EvalNode

    d = EvalNode(name="chain").to_dict()
    d["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="schema_version"):
        EvalNode.from_dict(d)


def test_t8_from_dict_rejects_non_int_version():
    """schema_version 非 int（含 bool）⇒ ValueError，不猜不转。"""
    from app.agent.chain.schema import EvalNode

    d = EvalNode(name="chain").to_dict()
    d["schema_version"] = "1"
    with pytest.raises(ValueError, match="schema_version"):
        EvalNode.from_dict(d)
