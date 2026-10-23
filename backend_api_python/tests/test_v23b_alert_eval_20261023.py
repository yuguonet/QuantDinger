# -*- coding: utf-8 -*-
"""告警条件求值器守卫（2026-10-23，离线·不连库）。

钉住三条不可回退的语义：
  1. 命中/未命中判定正确（单子句：field op value）
  2. **不可判定 ⇒ 不命中**（绝不猜：未知字段、未知运算符、值为 NULL、
     条件为空、取数失败，一律 hit=False + error 非空）
  3. 字段字典与运算符集合是白名单（同时充当 SQL 列名白名单，防注入）

为什么必须钉第 2 条：告警的失败模式是**静默**的 —— 若不可判定时退化成
"当它满足"，用户会被无意义轰炸；退化成"当它不满足"，告警永不触发。两者
都比不告警更糟，且都不会报错。

★ 语义边界（2026-10-23 重构后）：condition 是**单子句**结构
   {"symbol": "600519", "field": "last", "op": "<=", "value": 1650}
   工具面 create_alert_job 只暴露 field/op/value 单条，无 all/any 复合需求。
   若未来需要多条件（AND/OR），再按同一协议扩展，不为"看着全面"预留成本。
"""
from __future__ import annotations

import pytest

from app.agent.cron import alert_eval as ae


@pytest.fixture
def snap(monkeypatch):
    """把取数替换成固定快照（现价 1700，涨跌幅 2.5%）。"""

    def _fake(symbol: str):
        if symbol != "600519":
            return None
        return {"last": 1700.0, "open": 1660.0, "high": 1710.0, "low": 1655.0,
                "previousClose": 1658.5, "changePercent": 2.5, "volume": 1234567.0,
                "time": "2026-10-23 10:05:00"}

    monkeypatch.setattr(ae, "_fetch_snapshot", _fake)
    return _fake


def test_hit_when_condition_met(snap):
    r = ae.evaluate({"symbol": "600519", "field": "last", "op": "<=", "value": 1750})
    assert r["hit"] is True
    assert r["error"] is None
    assert r["actual"]["last"] == 1700.0


def test_no_hit_when_not_met(snap):
    r = ae.evaluate({"symbol": "600519", "field": "last", "op": "<=", "value": 1650})
    assert r["hit"] is False
    assert r["error"] is None


def test_eval_one_clause_ok(snap):
    """单子句：一条不满足即不命中，无复合结构。"""
    r = ae.evaluate({"symbol": "600519", "field": "changePercent", "op": ">", "value": 5})
    assert r["hit"] is False
    assert r["error"] is None


@pytest.mark.parametrize("cond,why", [
    ({"symbol": "600519", "field": "ma5", "op": "<", "value": 1},
     "未知字段（技术指标不在字典内）"),
    ({"symbol": "600519", "field": "last", "op": "~=", "value": 1},
     "未知运算符"),
    ({"symbol": "600519", "field": "last", "op": "<"},
     "缺阈值"),
    ({"symbol": "600519", "field": "last", "op": "<", "value": "abc"},
     "阈值非数值"),
    ({"symbol": "600519"},
     "条件为空（缺 field/op/value）"),
    ({"field": "last", "op": "<", "value": 1}, "缺 symbol"),
    ("not-a-dict", "condition 非对象"),
])
def test_undecidable_never_hits(snap, cond, why):
    """不可判定 ⇒ hit=False 且 error 非空（绝不猜，见文件头）。"""
    r = ae.evaluate(cond)
    assert r["hit"] is False, why
    assert r["error"], why


def test_null_field_value_is_undecidable(monkeypatch, snap):
    """快照字段为 NULL ⇒ 不可判定，不能当成 0 或"满足"。"""

    def _null(symbol):
        return {"last": None, "open": 1.0, "high": 1.0, "low": 1.0,
                "previousClose": 1.0, "changePercent": 0.0, "volume": 0.0,
                "time": "t"}

    monkeypatch.setattr(ae, "_fetch_snapshot", _null)
    r = ae.evaluate({"symbol": "600519", "field": "last", "op": ">", "value": 1})
    assert r["hit"] is False
    assert "NULL" in (r["error"] or "")


def test_no_snapshot_is_undecidable(monkeypatch):
    """取不到快照 ⇒ 不可判定（不猜、不静默通过）。"""
    monkeypatch.setattr(ae, "_fetch_snapshot", lambda s: None)
    r = ae.evaluate({"symbol": "600519", "field": "last", "op": ">", "value": 1})
    assert r["hit"] is False
    assert "无" in (r["error"] or "")


def test_fetch_failure_becomes_error_not_crash(monkeypatch):
    """取数抛异常 ⇒ 转 error，不能把异常冒泡成任务崩溃。"""

    def _boom(symbol):
        raise RuntimeError("db down")

    monkeypatch.setattr(ae, "_fetch_snapshot", _boom)
    r = ae.evaluate({"symbol": "600519", "field": "last", "op": ">", "value": 1})
    assert r["hit"] is False
    assert "db down" in (r["error"] or "")


def test_field_dict_is_closed_whitelist():
    """字段字典必须封闭（它同时是 SQL 列名白名单，防止拼进 SQL 的列被污染）。"""
    assert "last" in ae.FIELDS
    assert "previousClose" in ae.FIELDS
    assert "ma5" not in ae.FIELDS
    assert "rsi" not in ae.FIELDS
    # 字典键必须都是合法标识符（拼接进 SQL 的前提）
    for k in ae.FIELDS:
        assert k.replace("_", "").isalnum(), k


def test_ops_are_comparison_only():
    """运算符集合只含比较，不含赋值/执行类。"""
    assert set(ae._OPS) == {">", ">=", "<", "<=", "==", "!="}