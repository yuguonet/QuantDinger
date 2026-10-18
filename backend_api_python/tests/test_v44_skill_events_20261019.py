# -*- coding: utf-8 -*-
"""阶段 4 守卫：技能选择事件链 + 版本级反馈 + 归因纪律（2026-10-19）。

对应 `tmp/skill_system_rework_implementation_plan_20261018.md` §6/§8/§14.2：

  ① **事件分状态**：候选/选中/注入/执行/标签各自独立——不把"只被召回"记作
     使用；注入 ≠ 执行（trace 出现该技能节点才算跑过）。
  ② **版本冻结**：选中版本在选择时刻定格（registry active_version），后续
     文件变更不改写历史事件。
  ③ **归因纪律**：唯一注入 + 实际执行 + 单技能节点才计样本；多技能协作 ⇒
     `unattributable`（排除，不分摊）；pending/undecidable/data_missing/
     中断/基础设施失败**不计失败**；零曝光/曝光未选中不是负向样本（§14.2）。
  ④ **幂等聚合**：按事件全量重算（event_id 天然去重），worker 重跑不重复
     计数；低于样本门槛权重恒 1.0；有样本后带收缩、限幅 [0.9, 1.1]；
     新版本无样本回中性，不继承旧版本。
  ⑤ **唯一读口**：`weight_of` TTL 缓存 + 中性兜底；本阶段**只写影子统计**，
     生产排序不变（阶段 5 才消费）。
  ⑥ **fail-soft**：DB 不可用/表未建 ⇒ 全链 no-op 返回，不抛、不阻塞请求。

测试纪律：内存假 DB（`_FakeDB` 按 SQL 关键字路由，实现状态机语义）经
`skill_events._db` 注入；不碰真库、不写真文件。接线不变量用 AST 静态守卫。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_SERVICE = BACKEND / "app" / "agent" / "qd_service.py"
QD_AFTERCARE = BACKEND / "app" / "agent" / "qd_aftercare.py"
EVALUATOR = BACKEND / "app" / "agent" / "chain" / "evaluator.py"

from app.agent.chain import skill_events as se


# ── 内存假 DB（按 SQL 关键字路由；实现事件状态机语义）─────────────

class _FakeCur:
    def __init__(self, db):
        self._db = db
        self._one = None
        self._rows = []
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())
        p = list(params or [])
        db = self._db
        if s.startswith("INSERT INTO qd_skill_selection_events"):
            db.events[p[0]] = {
                "event_id": p[0], "request_id": p[1], "session_id": p[2],
                "trace_root_id": None, "query_fingerprint": p[3],
                "index_version": p[4], "ranker_version": p[5],
                "candidates": json.loads(p[6]) if isinstance(p[6], str) else p[6],
                "selected_skill_id": p[7], "selected_version": p[8],
                "injected_skill_id": "", "injected_version": "",
                "injected": False, "executed": False, "execution_status": "",
                "label": "", "exclusion_reason": "",
            }
            self.rowcount = 1
        elif s.startswith("INSERT INTO qd_skill_feedback"):
            key = (p[0], p[1])
            db.feedback[key] = {
                "skill_id": p[0], "version": p[1], "eligible_count": p[2],
                "correct_count": p[3], "partial_count": p[4],
                "incorrect_count": p[5], "excluded_counts": p[6],
                "weight": p[7], "uncertainty": p[8], "policy_version": p[9],
            }
            self.rowcount = 1
        elif "SET injected = TRUE" in s:
            row = db.events.get(p[2])
            if row and not row["injected"]:
                row.update(injected=True, injected_skill_id=p[0],
                           injected_version=p[1])
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif "SET trace_root_id" in s:
            row = db.events.get(p[2])
            if row:
                row.update(trace_root_id=p[0], executed=bool(p[1]))
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif "SET label" in s:
            # 忠实代理生产 SQL：WHERE 带 first-wins 守卫（label = '' OR
            # label = %s）才阻止异值改写；守卫被摘（变异）⇒ 无条件允许
            # 改写。假 DB 不自带业务规则——否则生产 SQL 变异穿不透假 DB
            # （守卫摘除也无人红 = 假绿）。
            row = db.events.get(p[2])
            guarded = "label = ''" in s
            ok = row and (not guarded or row["label"] == ""
                          or row["label"] == p[0])
            if ok:
                row.update(label=p[0], exclusion_reason=p[1])
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif s.startswith("SELECT injected_skill_id FROM qd_skill_selection_events"):
            row = db.events.get(p[0])
            self._one = {"injected_skill_id": row["injected_skill_id"]} if row else None
        elif "GROUP BY injected_skill_id" in s:      # 聚合扫描
            # 忠实代理生产 SQL：WHERE injected = TRUE AND executed = TRUE
            # 在文本里才过滤；被摘（变异）⇒ 不过滤，未执行事件也进扫描。
            # 同上：假 DB 不自带过滤，让生产变异能穿透。
            filtered = "injected = TRUE AND executed = TRUE" in s
            agg: dict = {}
            for r in db.events.values():
                if filtered and not (r["injected"] and r["executed"]
                                     and r["injected_skill_id"]):
                    continue
                k = (r["injected_skill_id"], r["injected_version"], r["label"])
                agg[k] = agg.get(k, 0) + 1
            self._rows = [{"skill_id": k[0], "version": k[1], "label": k[2], "n": n}
                          for k, n in sorted(agg.items())]
        elif s.startswith("SELECT skill_id, version, weight"):
            # feedback_detail 读口（weight_of 委托后的唯一数据源）：
            # 带版本 ⇒ 精确命中；空版本 ⇒ 该 skill 最新一行。
            if len(p) == 2:
                row = db.feedback.get((p[0], p[1]))
            else:
                cand = [r for r in db.feedback.values() if r["skill_id"] == p[0]]
                row = cand[-1] if cand else None
            self._one = dict(row) if row else None
        elif "FROM qd_skill_feedback" in s and "ORDER BY updated_at DESC" in s:
            self._rows = list(db.feedback.values())
        elif s.startswith("SELECT event_id FROM qd_skill_selection_events"):
            root = p[0]
            sid = p[1] if len(p) > 1 else None
            self._rows = [{"event_id": r["event_id"]} for r in db.events.values()
                          if r["trace_root_id"] == root
                          and (sid is None or r["injected_skill_id"] == sid)
                          and r["injected"] and r["executed"]
                          and r["label"] == ""]
        elif s.startswith("SELECT candidates, selected_skill_id"):
            self._rows = [{"candidates": json.dumps(r["candidates"],
                                                    ensure_ascii=False),
                           "selected_skill_id": r["selected_skill_id"],
                           "injected": r["injected"],
                           "injected_skill_id": r["injected_skill_id"],
                           "executed": r["executed"], "label": r["label"]}
                          for r in db.events.values()]
        else:
            raise AssertionError(f"_FakeCur 未路由的 SQL: {s[:80]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, db):
        self._db = db

    def cursor(self):
        return _FakeCur(self._db)

    def commit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDB:
    def __init__(self):
        self.events: dict = {}
        self.feedback: dict = {}
        self.boom = False

    def __call__(self):
        if self.boom:
            raise RuntimeError("no db")
        return _FakeConn(self)


@pytest.fixture
def db(monkeypatch):
    d = _FakeDB()
    monkeypatch.setattr(se, "_db", d)
    return d


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("QD_SKILL_EVENTS", "QD_SKILL_FEEDBACK_MIN_SAMPLES",
                "QD_SKILL_FEEDBACK_CACHE_TTL"):
        monkeypatch.delenv(var, raising=False)
    with se._CACHE_LOCK:
        se._CACHE.clear()
    yield


def _cands(*specs):
    """specs: (skill_id, exposed) → 候选快照条目。"""
    return [{"skill_id": s, "version": "v-" + s, "base_score": 3.0,
             "exposed": e, "reasons": ["词面命中: 分析"]}
            for s, e in specs]


def _mk_event(db, *, selected="", injected="", executed=False, label="",
              trace_root=None, cands=None):
    ev = se.record_selection(
        request_id="req-1", session_id="s1", query="分析600519",
        candidates=cands if cands is not None else _cands(("auto-a", True),
                                                          ("auto-b", False)),
        selected_skill_id=selected,
        selected_version="v-" + selected if selected else "")
    assert ev
    if injected:
        se.mark_injected(ev, injected, "v-" + injected)
    if trace_root is not None:
        se.mark_run_outcome(ev, trace_root_id=trace_root,
                            skill_node_names=[injected] if executed else [])
    if label:
        se.finalize_label(ev, label)
    return ev


# ═══════════════════════════════════════════════════════════════════
# 一、事件写入：候选快照 + 版本冻结 + 开关 + fail-soft
# ═══════════════════════════════════════════════════════════════════

def test_record_selection_snapshots_candidates_and_freezes_version(db):
    ev = se.record_selection(
        request_id="req-1", session_id="s1", query="分析600519",
        candidates=_cands(("auto-a", True), ("auto-b", False)),
        selected_skill_id="auto-a", selected_version="v-frozen")
    row = db.events[ev]
    assert row["selected_version"] == "v-frozen", "选中版本必须选择时冻结"
    assert [c["skill_id"] for c in row["candidates"]] == ["auto-a", "auto-b"]
    assert row["candidates"][0]["exposed"] is True
    assert row["candidates"][1]["exposed"] is False, \
        "落选 eligible 也要进快照（漏斗 eligible_but_not_exposed 的判据）"
    assert row["query_fingerprint"] and row["index_version"] == se.INDEX_VERSION
    assert row["ranker_version"] == se.RANKER_VERSION


def test_query_fingerprint_deterministic_and_normalized():
    a = se.query_fingerprint("分析 600519！")
    b = se.query_fingerprint("分析600519")
    assert a == b, "标点/空白差异不改变指纹"
    assert a == se.query_fingerprint("分析600519")


def test_record_selection_disabled_returns_none(db, monkeypatch):
    monkeypatch.setenv("QD_SKILL_EVENTS", "0")
    assert se.enabled() is False
    ev = se.record_selection(request_id="r", session_id="s", query="q",
                             candidates=[])
    assert ev is None and not db.events, "关闭 ⇒ 全写侧 no-op"


def test_record_selection_db_failure_fail_soft(monkeypatch):
    def _boom():
        raise RuntimeError("table missing")

    monkeypatch.setattr(se, "_db", _boom)
    ev = se.record_selection(request_id="r", session_id="s", query="q",
                             candidates=[], selected_skill_id="auto-a")
    assert ev is None, "表未建/DB 不可用 ⇒ None（不抛、不阻塞请求）"


# ═══════════════════════════════════════════════════════════════════
# 二、状态机：注入 ≠ 执行；标签 first-wins
# ═══════════════════════════════════════════════════════════════════

def test_injected_and_executed_are_separate_states(db):
    ev = _mk_event(db, selected="auto-a", injected="auto-a")
    row = db.events[ev]
    assert row["injected"] is True and row["injected_skill_id"] == "auto-a"
    assert row["executed"] is False, "注入 ≠ 执行（§6.2）"
    se.mark_run_outcome(ev, trace_root_id=42, skill_node_names=["auto-a"])
    assert db.events[ev]["executed"] is True
    assert db.events[ev]["trace_root_id"] == 42


def test_executed_requires_matching_skill_node(db):
    """注入了 A，trace 只有 B 的技能节点 ⇒ executed=False（没真跑 A）。"""
    ev = _mk_event(db, injected="auto-a")
    se.mark_run_outcome(ev, trace_root_id=7, skill_node_names=["auto-b"])
    assert db.events[ev]["executed"] is False


def test_mark_injected_idempotent(db):
    ev = _mk_event(db, injected="auto-a")
    assert se.mark_injected(ev, "auto-a", "v1") is True
    assert se.mark_injected(ev, "auto-a", "v1") is True
    assert db.events[ev]["injected_skill_id"] == "auto-a"


def test_mark_run_outcome_missing_event(db):
    assert se.mark_run_outcome("nope", trace_root_id=1,
                               skill_node_names=["x"]) is False


def test_finalize_label_first_wins(db):
    """first-wins：成熟标签不被重跑翻转（幂等第一入口）。"""
    ev = _mk_event(db, injected="auto-a", executed=True)
    assert se.finalize_label(ev, "correct") is True
    assert se.finalize_label(ev, "incorrect") is False, "异值不得改写已成熟标签"
    assert db.events[ev]["label"] == "correct"
    assert se.finalize_label(ev, "correct") is True, "同值 no-op 成功"


def test_finalize_label_rejects_unknown(db):
    ev = _mk_event(db)
    assert se.finalize_label(ev, "banana") is False
    assert db.events[ev]["label"] == ""


# ═══════════════════════════════════════════════════════════════════
# 三、归因纪律（§6.3）
# ═══════════════════════════════════════════════════════════════════

def test_ingest_labels_correct_and_incorrect(db):
    e1 = _mk_event(db, injected="auto-a", executed=True, trace_root=101)
    e2 = _mk_event(db, injected="auto-b", executed=True, trace_root=102)
    assert se.ingest_trace_labels(101, "auto-a", True) is True
    assert se.ingest_trace_labels(102, "auto-b", False) is True
    assert db.events[e1]["label"] == "correct"
    assert db.events[e2]["label"] == "incorrect"


def test_ingest_pending_is_not_failure(db):
    """correct=None（未成熟）⇒ pending：不计样本也不计失败（§6.3.3）。"""
    ev = _mk_event(db, injected="auto-a", executed=True, trace_root=101)
    assert se.ingest_trace_labels(101, "auto-a", None) is True
    assert db.events[ev]["label"] == "pending"
    assert db.events[ev]["exclusion_reason"] == "awaiting_mature"


def test_ingest_multi_skill_root_is_unattributable(db):
    """多技能协作 ⇒ unattributable 排除，不分摊、不猜测（§6.3.4）。"""
    ev = _mk_event(db, injected="auto-a", executed=True, trace_root=101)
    assert se.ingest_trace_labels(101, "auto-a", True, skill_node_count=2) is True
    assert db.events[ev]["label"] == "unattributable"
    assert db.events[ev]["exclusion_reason"] == "multi_skill_root"


def test_ingest_requires_injected_and_executed(db):
    """只召回/只注入未执行的事件不产生效果样本（§6.2/§6.3.2）。"""
    ev = _mk_event(db, injected="auto-a", trace_root=101)   # executed=False
    assert se.ingest_trace_labels(101, "auto-a", True) is False
    assert db.events[ev]["label"] == ""
    ev2 = _mk_event(db, selected="auto-a", trace_root=102)  # 未注入
    assert se.ingest_trace_labels(102, "auto-a", True) is False


def test_ingest_reprocess_is_idempotent(db):
    """重复成熟不重复计数（事件标签 first-wins + 聚合全量重算）。"""
    _mk_event(db, injected="auto-a", executed=True, trace_root=101)
    se.ingest_trace_labels(101, "auto-a", True)
    se.ingest_trace_labels(101, "auto-a", True)
    se.ingest_trace_labels(101, "auto-a", True)
    res = se.aggregate_feedback()
    assert res["auto-a"]["eligible"] == 1, "重跑三次仍只计 1 个样本"


# ═══════════════════════════════════════════════════════════════════
# 四、聚合：中性 / 限幅 / 版本隔离 / 排除分离
# ═══════════════════════════════════════════════════════════════════

def _fill(db, n, *, skill="auto-a", version="v1", label="correct"):
    for i in range(n):
        ev = _mk_event(db, injected=skill, executed=True,
                       trace_root=1000 + i, label=label)
        db.events[ev]["injected_version"] = version


def test_aggregate_below_threshold_is_neutral(db, monkeypatch):
    monkeypatch.setenv("QD_SKILL_FEEDBACK_MIN_SAMPLES", "15")
    _fill(db, 14, label="correct")
    res = se.aggregate_feedback()
    assert res["auto-a"]["eligible"] == 14
    assert res["auto-a"]["weight"] == 1.0, "低于样本门槛 ⇒ 恒中性 1.0"
    row = db.feedback[("auto-a", "v1")]
    assert row["correct_count"] == 14 and row["weight"] == 1.0
    assert row["policy_version"] == se.POLICY_VERSION


def test_aggregate_above_threshold_bounded_and_shrunk(db, monkeypatch):
    monkeypatch.setenv("QD_SKILL_FEEDBACK_MIN_SAMPLES", "15")
    _fill(db, 20, skill="auto-good", version="v1", label="correct")
    _fill(db, 20, skill="auto-bad", version="v1", label="incorrect")
    res = se.aggregate_feedback()
    wg, wb = res["auto-good"]["weight"], res["auto-bad"]["weight"]
    assert 1.0 < wg <= se._WEIGHT_HI, "全对 ⇒ 高于中性但不越限幅"
    assert se._WEIGHT_LO <= wb < 1.0, "全错 ⇒ 低于中性但不越限幅"


def test_aggregate_separates_excluded_not_failures(db):
    """排除态（pending/unattributable…）进 excluded_counts，不计失败样本。"""
    ev = _mk_event(db, injected="auto-a", executed=True, trace_root=101,
                   label="unattributable")
    db.events[ev]["injected_version"] = "v1"
    ev2 = _mk_event(db, injected="auto-a", executed=True, trace_root=102,
                    label="pending")
    db.events[ev2]["injected_version"] = "v1"
    res = se.aggregate_feedback()
    assert res["auto-a"]["eligible"] == 0, "排除态不是有效样本"
    assert res["auto-a"]["excluded"] == {"unattributable": 1, "pending": 1}
    assert db.feedback[("auto-a", "v1")]["weight"] == 1.0


def test_aggregate_ignores_unmatured_and_unexecuted(db):
    """未成熟（label=''）/ 未执行事件不进任何桶（等成熟）。"""
    _mk_event(db, injected="auto-a", executed=True, trace_root=101)   # 无标签
    _mk_event(db, injected="auto-a", trace_root=102)                  # 未执行
    res = se.aggregate_feedback()
    assert res == {} or res["auto-a"]["eligible"] == 0


def test_aggregate_excludes_labeled_but_unexecuted(db):
    """注入+已标签但**未执行** ⇒ 不进样本（生产 SQL WHERE 是唯一防线）。

    finalize_label 不校验 executed（只 first-wins）；防呆靠聚合 SQL 的
    `injected = TRUE AND executed = TRUE` 过滤。本测试专门让摘除该过滤
    的变异穿透假 DB（忠实代理语义），否则「未执行事件被算进样本」无人红。
    """
    ev = _mk_event(db, injected="auto-a", trace_root=101)   # executed=False
    assert se.finalize_label(ev, "correct") is True
    res = se.aggregate_feedback()
    assert res == {} or res["auto-a"]["eligible"] == 0


def test_aggregate_version_isolated(db, monkeypatch):
    """新版本没有样本回到中性，不盲目继承旧版本权重（§6.4）。"""
    monkeypatch.setenv("QD_SKILL_FEEDBACK_MIN_SAMPLES", "15")
    _fill(db, 20, skill="auto-a", version="v-old", label="correct")
    _fill(db, 3, skill="auto-a", version="v-new", label="incorrect")
    se.aggregate_feedback()
    assert se.weight_of("auto-a", "v-new") == 1.0, "新版本样本不足 ⇒ 中性"
    assert se.weight_of("auto-a", "v-old") > 1.0


def test_aggregate_is_recomputable(db):
    """全量重算幂等：跑两次结果一致（无重复计数）。"""
    _fill(db, 5, label="correct")
    se.aggregate_feedback()
    first = dict(db.feedback[("auto-a", "v1")])
    se.aggregate_feedback()
    assert db.feedback[("auto-a", "v1")] == first


# ═══════════════════════════════════════════════════════════════════
# 五、weight_of：唯一读口 + TTL + 中性兜底
# ═══════════════════════════════════════════════════════════════════

def test_weight_of_reads_feedback_with_ttl_cache(db, monkeypatch):
    monkeypatch.setenv("QD_SKILL_FEEDBACK_MIN_SAMPLES", "15")
    _fill(db, 20, label="correct")
    se.aggregate_feedback()
    w1 = se.weight_of("auto-a", "v1")
    assert w1 > 1.0
    db.boom = True                    # DB 挂掉：缓存仍应命中（TTL 内）
    assert se.weight_of("auto-a", "v1") == w1, "TTL 缓存命中不打 DB"


def test_weight_of_db_failure_is_neutral(monkeypatch):
    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(se, "_db", _boom)
    assert se.weight_of("auto-a", "v1") == 1.0, "读失败 ⇒ 中性（不抛）"


def test_weight_of_no_row_is_neutral(db):
    assert se.weight_of("auto-unknown", "vX") == 1.0


def test_weight_of_empty_id_is_neutral(db):
    assert se.weight_of("") == 1.0


def test_feedback_detail_reports_samples_and_policy(db, monkeypatch):
    """诊断读口：权重 + 样本数 + policy 版本（阶段 5 排序 trace 数据源）。"""
    monkeypatch.setenv("QD_SKILL_FEEDBACK_MIN_SAMPLES", "15")
    _fill(db, 20, label="correct")
    se.aggregate_feedback()
    d = se.feedback_detail("auto-a", "v1")
    assert d is not None
    assert d["eligible_count"] == 20 and d["weight"] > 1.0
    assert d["policy_version"] == se.POLICY_VERSION
    assert se.feedback_detail("auto-unknown", "vX") is None, "无行 ⇒ None"


def test_weight_of_clamps_out_of_range_row(db):
    """读侧限幅（纵深防御）：DB 行被写飞也不得跳出 [0.9,1.1]。"""
    db.feedback[("auto-a", "v1")] = {
        "skill_id": "auto-a", "version": "v1", "eligible_count": 99,
        "correct_count": 99, "partial_count": 0, "incorrect_count": 0,
        "excluded_counts": "{}", "weight": 5.0, "uncertainty": "{}",
        "policy_version": se.POLICY_VERSION}
    assert se.weight_of("auto-a", "v1") == se._WEIGHT_HI
    with se._CACHE_LOCK:
        se._CACHE.clear()
    db.feedback[("auto-a", "v1")]["weight"] = 0.01
    assert se.weight_of("auto-a", "v1") == se._WEIGHT_LO


# ═══════════════════════════════════════════════════════════════════
# 六、漏斗：六阶段分开统计，零曝光不是负向（§14.2）
# ═══════════════════════════════════════════════════════════════════

def test_funnel_stages_counted_separately(db):
    # ① eligible 未曝光 1 个（auto-b）；曝光未选中 1 个（auto-c）
    _mk_event(db, cands=_cands(("auto-c", True), ("auto-b", False)))
    # ② 选中未注入
    _mk_event(db, selected="auto-a", cands=_cands(("auto-a", True)))
    # ③ 注入未执行
    _mk_event(db, injected="auto-a", cands=_cands(("auto-a", True)))
    # ④ 执行未成熟 / 执行已标签
    _mk_event(db, injected="auto-a", executed=True, trace_root=901,
              cands=_cands(("auto-a", True)))
    _mk_event(db, injected="auto-a", executed=True, trace_root=902,
              label="correct", cands=_cands(("auto-a", True)))
    f = se.funnel_report()
    assert f["eligible_but_not_exposed"] == 1
    assert f["exposed_not_selected"] == 1
    assert f["selected_not_injected"] == 1
    assert f["injected_not_executed"] == 1
    assert f["executed_pending"] == 1
    assert f["executed_labeled"] == 1
    assert f["events"] == 5


def test_funnel_excluded_reasons_grouped(db):
    ev = _mk_event(db, injected="auto-a", executed=True, trace_root=901,
                   label="unattributable")
    db.events[ev]["exclusion_reason"] = "multi_skill_root"
    f = se.funnel_report()
    assert f["excluded"] == {"unattributable": 1}


def test_funnel_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("QD_SKILL_EVENTS", "0")
    assert se.funnel_report()["events"] == 0


# ═══════════════════════════════════════════════════════════════════
# 七、接线不变量（AST：调用真实性，非字样）
# ═══════════════════════════════════════════════════════════════════

def _func_node(src: str, name: str):
    tree = ast.parse(src)
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
        stack.extend(ast.iter_child_nodes(node))
    raise AssertionError(f"找不到函数 {name}")


def _live_calls(src: str, func: str, name: str) -> list:
    """收集 func 内对 name 的调用（Call 节点 + 祖先链）。

    为什么不能只查字样：`None and f(...)` 的文本仍在但永不执行——纯文本
    断言对这种「静默摘除」恒绿。祖先链用于短路位/条件位检测。
    """
    hits: list = []

    def walk(node, parents):
        if isinstance(node, ast.Call):
            f = node.func
            fname = getattr(f, "id", "") or getattr(f, "attr", "")
            if fname == name:
                hits.append((node, parents))
        for ch in ast.iter_child_nodes(node):
            walk(ch, parents + (node,))

    walk(_func_node(src, func), ())
    return hits


def _assert_live(hits: list, name: str) -> None:
    """接线必须真实生效：存在 + 不在短路表达式内 + 是 Expr/Assign 语句值。"""
    assert hits, f"{name} 接线被摘 ⇒ 影子统计静默失效"
    for _call, parents in hits:
        assert not any(isinstance(p, ast.BoolOp) for p in parents), \
            f"{name} 不得位于短路表达式内（None and … = 永不执行）"
        stmts = [p for p in parents if isinstance(p, ast.stmt)]
        assert stmts and isinstance(stmts[-1], (ast.Expr, ast.Assign)), \
            f"{name} 必须是生效语句（Expr/Assign），不得只出现在条件判位"


def test_service_records_selection_and_injection():
    src = QD_SERVICE.read_text(encoding="utf-8")
    _assert_live(_live_calls(src, "_run_sync", "record_selection"),
                 "record_selection")
    _assert_live(_live_calls(src, "_run_sync", "mark_injected"),
                 "mark_injected")
    body = ast.unparse(_func_node(src, "_run_sync"))
    assert "_skill_event_id" in body, "event_id 必须跨 prefetch/收尾传递"


def test_aftercare_marks_run_outcome():
    src = QD_AFTERCARE.read_text(encoding="utf-8")
    _assert_live(_live_calls(src, "_intake_accountability", "mark_run_outcome"),
                 "mark_run_outcome")


def test_evaluator_ingests_labels_and_aggregates():
    src = EVALUATOR.read_text(encoding="utf-8")
    _assert_live(_live_calls(src, "update_weights", "ingest_trace_labels"),
                 "ingest_trace_labels")
    _assert_live(_live_calls(src, "update_weights", "aggregate_feedback"),
                 "aggregate_feedback")


def test_evaluator_query_carries_root_and_node_count():
    """归因判据（root_id + 技能节点数）必须进查询，否则多技能检测失效。"""
    src = EVALUATOR.read_text(encoding="utf-8")
    node = _func_node(src, "update_weights")
    seg = ast.get_source_segment(src, node) or ""
    assert "root_id" in seg and "skill_node_count" in seg
