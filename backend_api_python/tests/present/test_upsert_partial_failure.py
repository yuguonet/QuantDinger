"""test_upsert_partial_failure.py — P2-11 验收（2026-10-10 数据面独立排期）。

方案原文（审计缺陷修复方案.md §P2-11）：
    构造「3 只票、1 只判定异常」，断言该票旧 watch_pending 行**仍在**，
    且报告里有**行级 ERROR**。

病灶回顾
--------
原实现 `store.upsert_scan_signals` 前置无条件
`DELETE ... state='watch_pending' AND trade_date=目标 AND strategy=ANY(scope)`，
再只插本轮 rows ⇒ 判定异常/取数缺失被 `continue` 跳过的票：旧行已删且无新行顶上
= **信号蒸发**（部分失败 = 部分信号消失，违反目标态 §4.3「展示层不得凭空缺行」）。
加重：`_rows_by_record` 原来不回传 `err_by_key` ⇒ record writer 下异常汇总恒空，
连 ERROR 都不打（静默断链同构）。

本文件锁两层（缺一层则修复可被静默回退）：
  1. **writer 层**：3 票 1 异常 ⇒ `skip_rows` 记到行级、`err_by_key` 非空
     （回传链路通，不再恒空）；
  2. **store 层**（fake DB，逐语句取证）：
     a. 前置 DELETE 带 `NOT IN` 豁免 —— 新行键 + 被跳过键都不在删除范围（旧行仍在）；
     b. 被跳过票旧行打 stale 标记（extra.stale=true），`row_errors.action=stale_kept`；
     c. 行级 ERROR 落日志（报告可见，不是只有汇总计数）；
     d. 同键本轮已有新行 ⇒ 不豁免（以新行为准），防豁免被滥用成"永不删"。

易错点：
    · SQL 断言针对**生成语句与参数**（单元级）；PostgreSQL 行构造器
      `(a,b,c) NOT IN ((..),(..))` 的真实语义须在 dry-run + ledger 备份下对生产
      库验证（方案 §P2-11 风险条），本文件不替代那一步。
    · `upsert_scan_signals` 函数体内 `from app.utils.db import get_db_connection`
      （调用期导入）⇒ monkeypatch 必须打在 `app.utils.db` 模块属性上。
"""

from __future__ import annotations

import json

from app.market_cn.auto import scan, store
from app.market_cn.auto.strategies.base import Signal

from .test_projection import _bars

TD = "2026-06-17"
ST = "p211_stub"
GOOD1, GOOD2, BOOM = "600000", "600001", "600002"
ALL_CODES = (GOOD1, GOOD2, BOOM)


class _StubJudge:
    """最小策略替身：两票正常出 1 条信号，一票判定抛异常。

    （类名不带 Strategy 后缀 —— `test_exit_single_source` 的门禁禁止
    strategies/ 之外出现 `class *Strategy`。）
    """

    key = ST
    prefilter_anchor = "signal"
    use_unified_prefilter = False

    def scan_days(self, bars, code, *, lo_date=None, hi_date=None, **params):
        if code == BOOM:
            raise RuntimeError("行情取数缺失: 模拟部分失败")
        return [Signal(code=code, time=lo_date, score=60, price=10.0,
                       extra={"note": "p211"})]


class _NullSampler:
    def observe(self, *a, **k):
        pass


def _run_writer():
    """3 票 1 异常 → (rows, err_by_key, skip_rows)。"""
    active = {ST: _StubJudge()}
    bars = {c: _bars() for c in ALL_CODES}
    info = {c: {"name": f"票{c[-2:]}"} for c in ALL_CODES}
    return scan._rows_by_scan(active, bars, TD, info, _NullSampler())


# ================================================================
# 1. writer 层：行级 skip 回传（err_by_key 不再恒空）
# ================================================================
def test_writer_reports_partial_failure():
    rows, errs, skip = _run_writer()
    assert len(rows) == 2, f"正常两票应各出 1 行, 实得 {[r['code'] for r in rows]}"
    assert errs == {ST: 1}, f"判定异常汇总必须回传非空, 实得 {errs}"
    assert len(skip) == 1 and skip[0]["code"] == BOOM, f"skip_rows 行级记录错误: {skip}"
    assert "行情取数缺失" in skip[0]["error"], "skip 必须带原因（供报告与排障）"
    assert {r["code"] for r in rows} == {GOOD1, GOOD2}


# ================================================================
# 2. store 层：fake DB 逐语句取证
# ================================================================
class _FakeCursor:
    def __init__(self, owner):
        self._owner = owner
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.rowcount = self._owner._on_execute(sql, params)

    def close(self):
        pass


class _FakeConn:
    """只做语句登记 + 按语句类型给出可控 rowcount（不模拟完整 SQL 引擎）。"""

    def __init__(self, existing=(), purge_rowcount=0):
        #: 库中现存 (strategy, code) 旧行 —— stale UPDATE 的 rowcount 事实源
        self.existing = set(existing)
        self.purge_rowcount = purge_rowcount
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _on_execute(self, sql, params):
        self.executed.append((sql, params))
        flat = " ".join(sql.split())
        if flat.startswith("DELETE"):
            return self.purge_rowcount
        if "AND strategy = %s AND code = %s" in flat:
            # stale 标记 UPDATE: params = (json, trade_date, state, strategy, code)
            pair = (params[3], params[4])
            return 1 if pair in self.existing else 0
        if flat.startswith("UPDATE") and "trade_date <" in flat:
            return 0   # supersede: 本用例不构造跨日行
        if flat.startswith("INSERT"):
            return 1
        return 0

    def _find(self, prefix):
        hits = [(s, p) for s, p in self.executed
                if " ".join(s.split()).startswith(prefix)]
        assert hits, f"未执行 {prefix} 语句: " \
                     f"{[' '.join(s.split())[:60] for s, _ in self.executed]}"
        return hits[0]


def _run_upsert(monkeypatch, conn, rows, skip_rows=()):
    import app.utils.db as db_mod

    monkeypatch.setattr(db_mod, "get_db_connection", lambda: conn)
    return store.upsert_scan_signals(
        TD, rows, strategies=(ST,), skip_rows=list(skip_rows))


def _row(code, style="a"):
    return {"strategy": ST, "code": code, "name": f"票{code[-2:]}", "style": style,
            "score": 60, "signal_date": TD, "signal_price": 10.0,
            "extra": {"note": "p211"}}


def test_delete_exempts_skipped_and_new_keys(monkeypatch):
    """① 旧行仍在：DELETE 同时豁免「本轮新行键」与「被跳过键」。"""
    conn = _FakeConn(existing={(ST, GOOD1), (ST, GOOD2), (ST, BOOM)},
                     purge_rowcount=99)   # 若豁免失效, 这 99 行 = 信号蒸发
    result = _run_upsert(monkeypatch, conn,
                         rows=[_row(GOOD1), _row(GOOD2)],
                         skip_rows=[{"strategy": ST, "code": BOOM,
                                     "error": "行情取数缺失: 模拟部分失败"}])

    del_sql, del_params = conn._find("DELETE")
    assert del_sql.count("NOT IN %s") == 2, (
        "前置 DELETE 必须带两层豁免: 新行键 (行值三元组) + 跳过键 (二元组)")
    # 参数序: trade_date, state, scope, new_keys, skip_pairs
    assert del_params[0] == TD and del_params[2] == [ST]
    assert (ST, GOOD1, "a") in del_params[3] and (ST, GOOD2, "a") in del_params[3]
    assert (ST, BOOM) in del_params[4], "被跳过票必须豁免删除 —— 否则旧行蒸发"
    assert result["purged"] == 99   # rowcount 来自真删除的"其余键"; 本例保留语义靠 NOT IN


def test_skipped_row_marked_stale_and_reported(monkeypatch, caplog):
    """② 被跳过票：旧行标 stale + row_errors 行级报告 + ERROR 日志可见。"""
    conn = _FakeConn(existing={(ST, GOOD1), (ST, GOOD2), (ST, BOOM)})
    with caplog.at_level("ERROR", logger="app.market_cn.auto.store"):
        result = _run_upsert(monkeypatch, conn,
                             rows=[_row(GOOD1), _row(GOOD2)],
                             skip_rows=[{"strategy": ST, "code": BOOM,
                                         "error": "行情取数缺失: 模拟部分失败"}])

    # stale 标记 UPDATE 精确打在 (ST, BOOM) 上
    upd_hits = [(s, p) for s, p in conn.executed
                if " ".join(s.split()).startswith("UPDATE")
                and "extra = COALESCE" in s and "trade_date <" not in s]
    assert len(upd_hits) == 1, f"应恰好 1 条 stale 标记 UPDATE, 实得 {len(upd_hits)}"
    upd_sql, upd_params = upd_hits[0]
    assert upd_params[3:5] == (ST, BOOM), "stale 标记必须精确命中被跳过票"
    assert json.loads(upd_params[0]).get("stale") is True, "stale 标记缺 extra.stale"

    assert result["stale"] == 1, "旧行保留后必须被标记 stale"
    assert result["row_errors"] == [
        {"strategy": ST, "code": BOOM, "error": "行情取数缺失: 模拟部分失败",
         "action": "stale_kept"}]
    assert any("行级 ERROR" in r.message and BOOM in r.message for r in caplog.records), (
        "报告必须有行级 ERROR 日志（不是只有汇总计数）—— 消失的票要有人喊")


def test_skipped_without_old_row_is_no_old_row(monkeypatch):
    """③ 库中无旧行 ⇒ action=no_old_row（诚实报告, 不冒充 stale_kept）。"""
    conn = _FakeConn(existing=set())
    result = _run_upsert(monkeypatch, conn,
                         rows=[_row(GOOD1), _row(GOOD2)],
                         skip_rows=[{"strategy": ST, "code": BOOM, "error": "boom"}])
    assert result["stale"] == 0
    assert result["row_errors"][0]["action"] == "no_old_row"


def test_same_key_new_row_wins_over_skip(monkeypatch):
    """④ 同键本轮已有新行 ⇒ 不进豁免集合（以新行为准, 豁免不被滥用成"永不删"）。"""
    conn = _FakeConn(existing={(ST, GOOD1), (ST, GOOD2), (ST, BOOM)})
    _run_upsert(monkeypatch, conn,
                rows=[_row(GOOD1), _row(GOOD2), _row(BOOM)],
                skip_rows=[{"strategy": ST, "code": BOOM, "error": "上一轮曾异常"}])
    _del_sql, del_params = conn._find("DELETE")
    # 只有 new_keys 一层豁免; skip_pairs 不得再豁免 BOOM
    assert len(del_params) == 4, f"同键已有新行时不应再挂 skip 豁免: {del_params}"
    assert all((ST, BOOM) != p for p in del_params[3])
    stale_upd = [p for s, p in conn.executed
                 if " ".join(s.split()).startswith("UPDATE")
                 and "extra = COALESCE" in s and "trade_date <" not in s]
    assert not stale_upd, "本轮已产出新行的键不应被标 stale"
