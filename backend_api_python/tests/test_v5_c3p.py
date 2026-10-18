"""v5 P2 **接线**：把 C3P 的 ②③ 段接进生产链路（TDD，2026-10-13）

## 本文件测的是什么

不是测 `aggregate()` 的数学（那是 `test_v5_aggregator.py`），而是测**接线**：
`evidence.build_evidence()` / `aggregator.aggregate()` / `aggregator.record_pair()`
这三个**已建好但零生产调用方**的孤岛，现在有没有真被跑起来。

## ★ Δp 为 0 仍然可能是**设计内的预期行为**（P3 已接线，但仍在冷启动期）

2026-10-14 P3 已接线：`coefs` 由 `chain/calib_feed.coefs_for(ck)` 供给。
但**样本不足时它返回 None**（独立簇 < min_clusters 只出先验）⇒ `Δlogit=0`
⇒ `p_final == p_base`。依据 v3 §15.1：「无经验时 β₀=0 ⇒ p_final=p_base」。
⇒ **默认路径仍不测「Δp 非零」**，只测「pair 在落、condition_key 对、evidence 有回传」；
「Δp 非零」单独在**注入假系数**的用例里测（见本文件末尾 P3 接线守卫那组）。

## 两个反向的 fail-open 语义（别搞混，搞混就产生假数据）

| 情形 | 处置 | 为什么 |
|---|---|---|
| evidence 取不到（LLM 挂了/无 falsifiers） | **仍记 pair**，标 `degraded=True` | G20 的监控需要「降级比例」这个分母 —— 不记就永远分不清「没经验」还是「没产出」 |
| `p_base` 取不到 | **不记 pair** | 对照臂缺失的 Δp 是**假数据**，不是降级数据 |

## 易错处

· ★ `chain.X` 与 `app.agent.chain.X` **不是同一模块对象**（`app/agent` 被单独加进 sys.path）。
  ⇒ 测试一律 patch **`c3p` 模块对象上的属性**（`c3p._evidence` / `c3p.p_base_of`），
  不要去 patch `app.agent.chain.evidence.*` —— 那样打不到，会**误触真源网络**。
· `predict_next_day` 要的 K 线形状：`time` 必须是 **int 时间戳**（`predict._by_day` 用
  `datetime.fromtimestamp`），不是日期串。
· `predict.MIN_BARS = 60` ⇒ 取数根数必须 ≥ 60，否则模型直接返回 None。
"""
from __future__ import annotations

import json

import pytest

from app.agent.chain import c3p
from app.agent.chain import aggregator as ag


# ═══════════════════════════════════════════════════════════════
#  fixture：把落盘目录与两条 seam 打到 tmp
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """默认「全绿」环境：开关开、pair 落 tmp、两条 seam 给可用返回值。"""
    monkeypatch.setenv("QD_C3P_ENABLED", "1")
    monkeypatch.setenv("QD_PAIR_DIR", str(tmp_path))
    monkeypatch.setenv("QD_C3P_MAX_CLAIMS", "2")
    monkeypatch.setattr(c3p, "p_base_of", lambda code: 0.55)
    monkeypatch.setattr(
        c3p, "extract_evidence",
        lambda **kw: {"ev_ver": "ev1", "condition_key": "down|low",
                      "falsifiers": [{"key": "vol_ratio", "op": "<", "threshold": 1.2}],
                      "discriminators": [{"key": "放量", "computable": True}]})
    return {"tmp": tmp_path}


def _claim(subject="002031", kind="stock", horizon="T+3"):
    return {"claim_type": "direction", "subject": subject, "subject_kind": kind,
            "horizon": horizon, "predicted": {"direction": "up"},
            "confidence": 0.6, "source_quote": "看多", "seq": 1}


def _run(**kw):
    kw.setdefault("user_query", "002031 明天怎么走")
    kw.setdefault("answer", "看多，目标价 12 元")
    kw.setdefault("tracked", True)
    kw.setdefault("decision_id", 7)
    kw.setdefault("claim_ids", [11, 12])
    kw.setdefault("claims", [_claim(), _claim()])
    kw.setdefault("as_of", "2026-10-12")
    return c3p.run_for_decision(**kw)


# ═══════════════════════════════════════════════════════════════
#  触发条件（任一不满足即跳过，并给 reason）
# ═══════════════════════════════════════════════════════════════

def test_disabled_skips(env, monkeypatch):
    monkeypatch.setenv("QD_C3P_ENABLED", "0")
    r = _run()
    assert r["ran"] is False and r["reason"] == "disabled"
    assert r["n_pairs"] == 0


def test_not_tracked_skips(env):
    r = _run(tracked=False)
    assert r["ran"] is False and r["reason"] == "not_tracked"


def test_no_claim_skips(env):
    r = _run(claim_ids=[], claims=[])
    assert r["ran"] is False and r["reason"] == "no_claim"


def test_no_subject_skips(env):
    """subject 不是个股（指数/板块/空）⇒ 取不到 p_base ⇒ 不该硬跑。"""
    for subj, kind in ((None, None), ("000001", "index"), ("sector:光伏", "sector")):
        r = _run(claims=[_claim(subject=subj, kind=kind)])
        assert r["ran"] is False, f"subject={subj!r} 不该触发 C3P"
        assert r["reason"] == "no_subject"


# ═══════════════════════════════════════════════════════════════
#  fail-open 的两个方向（最容易写反的地方）
# ═══════════════════════════════════════════════════════════════

def test_no_p_base_means_no_pair(env, monkeypatch):
    """★ p_base 取不到 ⇒ **不记 pair**。

    对照臂缺失的 Δp 是**假数据**（不是"降级数据"）——
    记了会让人误以为"这次经验贡献为 0"，实际是根本没算出来。
    """
    monkeypatch.setattr(c3p, "p_base_of", lambda code: None)
    r = _run()
    assert r["n_pairs"] == 0, "没有对照臂的 Δp 不许落库"
    assert r["reason"] == "no_p_base"
    assert c3p.load_recent_pairs("2026-10-12") == []


def test_evidence_missing_still_records_degraded_pair(env, monkeypatch):
    """evidence 取不到 ⇒ p_final == p_base，**但仍记一条**并标 degraded=True。"""
    monkeypatch.setattr(c3p, "extract_evidence", lambda **kw: None)
    r = _run(claim_ids=[11], claims=[_claim()])
    assert r["n_pairs"] == 1, "降级也要记，否则 G20 的降级比例没有分母"
    rows = c3p.load_recent_pairs("2026-10-12")
    assert rows[0]["p_base"] == pytest.approx(0.55)
    assert rows[0]["p_final"] == pytest.approx(0.55), "无系数 + 无证据 ⇒ 必须等于基线"
    assert rows[0]["extra"]["degraded"] is True
    assert r["degraded"] is True


def test_llm_exception_is_failed_open(env, monkeypatch):
    """LLM 抛异常 ⇒ 不许抛给用户，整条旁路静默降级。"""
    def boom(**kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(c3p, "extract_evidence", boom)
    r = _run(claim_ids=[11], claims=[_claim()])   # 不许抛
    assert r["n_pairs"] == 1
    assert r["degraded"] is True


# ═══════════════════════════════════════════════════════════════
#  正常路径
# ═══════════════════════════════════════════════════════════════

def test_happy_path_records_pair(env):
    r = _run(claim_ids=[11], claims=[_claim()])
    assert r["ran"] is True and r["n_pairs"] == 1
    rows = c3p.load_recent_pairs("2026-10-12")
    assert len(rows) == 1
    row = rows[0]
    assert row["claim_id"] == 11
    assert row["p_base"] == pytest.approx(0.55)
    assert row["condition_key"] == "down|low"
    assert row["delta_p"] == pytest.approx(0.0)     # ★ P3 未上线 ⇒ 恒 0 是预期


def test_missing_frame_yields_unknown_condition_key(env, monkeypatch):
    """情境帧取不到 ⇒ condition_key 全 unknown，**不猜**。

    猜出来的条件键会把样本分进错误的格子，且事后无法识别。
    """
    monkeypatch.setattr(c3p, "current_frame", lambda *a, **k: None)
    monkeypatch.setattr(
        c3p, "extract_evidence",
        lambda **kw: {"ev_ver": "ev1", "condition_key": "unknown|unknown"})
    r = _run(claim_ids=[11], claims=[_claim()])
    assert r["condition_key"] == "unknown|unknown"
    assert c3p.load_recent_pairs("2026-10-12")[0]["condition_key"] == "unknown|unknown"


def test_no_frame_and_no_evidence_yields_unknown_key(env, monkeypatch):
    """★ 情境帧缺失 **且** 证据缺失 ⇒ 条件键必须是 `unknown|unknown`。

    `evidence.condition_key()` 取不到情境就标 `unknown`（**不猜**）——
    猜出来的键会把样本分进错误的格子，而且事后无法识别哪些是猜的。
    """
    monkeypatch.setattr(c3p, "current_frame", lambda *a, **k: None)
    monkeypatch.setattr(c3p, "extract_evidence", lambda **kw: None)
    r = _run(claim_ids=[11], claims=[_claim()])
    assert r["condition_key"] == "unknown|unknown", (
        f"情境取不到就必须标 unknown，实得 {r['condition_key']!r}（猜出来的键事后无法识别）")
    assert c3p.load_recent_pairs("2026-10-12")[0]["condition_key"] == "unknown|unknown"


def test_evidence_is_returned_for_case_memory(env):
    """★ A-4 的写侧：evidence 必须回传给 `_record_case`（落 `outcome.evidence`）。

    读侧（`format_case_injection`）早就就绪，但写侧没人传 ⇒ 库里没有一条真 EV-1。
    """
    r = _run(claim_ids=[11], claims=[_claim()])
    assert isinstance(r["evidence"], dict)
    assert r["evidence"]["ev_ver"] == "ev1"
    assert r["evidence"]["falsifiers"], "没有证伪条件 ⇒ 该次应降级，不该冒充有效证据"


def test_max_claims_caps_cost(env, monkeypatch):
    """一次最多处理 `QD_C3P_MAX_CLAIMS` 条 claim —— 控成本，不是控正确性。"""
    monkeypatch.setenv("QD_C3P_MAX_CLAIMS", "2")
    r = _run(claim_ids=[1, 2, 3, 4, 5],
             claims=[_claim() for _ in range(5)])
    assert r["n_pairs"] == 2


def test_pair_is_idempotent_on_rerun(env):
    """同 (as_of, claim_id) 覆盖 —— 补跑不重复计票（复用 aggregator 的纪律）。"""
    _run(claim_ids=[11], claims=[_claim()])
    _run(claim_ids=[11], claims=[_claim()])
    assert len(c3p.load_recent_pairs("2026-10-12")) == 1


# ═══════════════════════════════════════════════════════════════
#  p_base 接线：必须走冻结模型，不许自己算
# ═══════════════════════════════════════════════════════════════

def test_p_base_of_uses_frozen_model(tmp_path, monkeypatch):
    """`p_base_of` 必须真去调 `predict_next_day`，且喂进去的 K 线 `time` 是 int 时间戳。"""
    seen = {}

    def fake_bars(code, n=None):
        seen["bars"] = (code, n)
        return [{"t": "2026-10-12", "time": 1760000000 + i,
                 "open": 10.0, "high": 10.5, "low": 9.5,
                 "close": 10.2, "volume": 1000} for i in range(120)]

    def fake_market(n=None):
        return [{"time": 1760000000 + i, "close": 3000.0 + i,
                 "open": 3000.0, "high": 3010.0, "low": 2990.0} for i in range(120)]

    def fake_predict(stock_klines, market_klines, chip_core=None):
        seen["n_stock"] = len(stock_klines)
        seen["time_type"] = type(stock_klines[0]["time"]).__name__
        return {"p_up": 0.6123, "score": 61.23}

    monkeypatch.setattr(c3p, "_kf", type("KF", (), {
        "daily_bars": staticmethod(fake_bars),
        "market_bars": staticmethod(fake_market),
        "enough": staticmethod(lambda bars, minimum=60: bool(bars) and len(bars) >= minimum)}))
    monkeypatch.setattr(c3p, "_predict_next_day", fake_predict)

    p = c3p.p_base_of("002031")
    assert p == pytest.approx(0.6123)
    assert seen["bars"][0] == "002031"
    assert seen["time_type"] == "int", "predict._by_day 要 int 时间戳，不是日期串"


def test_p_base_of_returns_none_when_model_silent(monkeypatch):
    """`predict_next_day` 返回 None（数据不足）⇒ `p_base_of` 必须返回 None（不许兜底成 0.5）。"""
    monkeypatch.setattr(c3p, "_kf", type("KF", (), {
        "daily_bars": staticmethod(lambda code, n=None: []),
        "market_bars": staticmethod(lambda n=None: [])}))
    monkeypatch.setattr(c3p, "_predict_next_day", lambda *a, **k: None)
    assert c3p.p_base_of("002031") is None


def test_p_base_path_is_free_of_feedback_tables():
    """红线 R2：`p_base` 路径禁止接入反馈信号表。

    扫描范围：**排除模块头 docstring** —— 那里必须能写出「禁什么、为什么禁」，
    否则后人看不懂禁令；但**代码里**出现就是违规。
    """
    src = (c3p.__file__ and open(c3p.__file__, encoding="utf-8").read()) or ""
    body = src.split('"""', 2)[-1]          # 去掉模块头 docstring
    for bad in ("qd_agent_weights", "exp_cond_calib", "exp_pair"):
        assert bad not in body, f"c3p 代码里出现 {bad} ⇒ 违反红线 R2"


# ═══════════════════════════════════════════════════════════════
#  阈值纪律
# ═══════════════════════════════════════════════════════════════

def test_thresholds_come_from_env(monkeypatch):
    """禁止硬编码：所有阈值必须可从 env 改（脏值回落默认，不崩）。"""
    monkeypatch.setenv("QD_C3P_MAX_CLAIMS", "5")
    monkeypatch.setenv("QD_C3P_BARS", "150")
    monkeypatch.setenv("QD_C3P_LLM_TIMEOUT", "7")
    assert c3p.max_claims() == 5
    assert c3p.bars_n() == 150
    assert c3p.llm_timeout() == pytest.approx(7.0)

    monkeypatch.setenv("QD_C3P_MAX_CLAIMS", "not-a-number")
    assert c3p.max_claims() == c3p._DEFAULT_MAX_CLAIMS


def test_bars_n_never_below_model_minimum(monkeypatch):
    """取数根数必须 ≥ 模型下限，否则 `predict_next_day` 直接返回 None（白取一次网络）。"""
    from app.watchlist.predict import MIN_BARS
    assert c3p._MIN_BARS == MIN_BARS, (
        "c3p._MIN_BARS 与 app.watchlist.predict.MIN_BARS 必须同步 —— "
        "改了模型下限却忘了改这里，会静默取回不够长的 K 线（不报错，只返回 None）")
    monkeypatch.setenv("QD_C3P_BARS", "10")
    assert c3p.bars_n() >= MIN_BARS


# ═══════════════════════════════════════════════════════════════
#  P3 接线守卫（2026-10-14）：coefs 不许再是常量 None
# ═══════════════════════════════════════════════════════════════

def test_aggregate_coefs_is_not_constant_none():
    """★ `aggregate(coefs=...)` 的实参**不许是常量 None**（P3 接线回退守卫）。

    用 **AST** 而不是全文匹配：全文匹配会被注释 / docstring 里的 `coefs=None`
    骗过去（假绿），AST 只认**真实调用点**。
    变异检验：把调用点改回 `coefs=None` ⇒ 本用例 FAIL。
    """
    import ast

    src = (c3p.__file__ and open(c3p.__file__, encoding="utf-8").read()) or ""
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else "")
        if name != "aggregate":
            continue
        for kw in node.keywords or []:
            if kw.arg == "coefs":
                found.append(kw.value)
    assert found, "c3p 里找不到 aggregate(coefs=...) 调用 ⇒ P3 接线被摘掉了"
    for v in found:
        assert not (isinstance(v, ast.Constant) and v.value is None), (
            "aggregate(coefs=None) ⇒ 系数恒空、Δp 永远为 0（P3 接线回退）")


def test_injected_coefs_move_p_final_and_are_auditable(env, monkeypatch):
    """给入系数 ⇒ `p_final` 必须真的动，且**来源层**写进 pair（可审计）。"""
    monkeypatch.setattr(
        c3p._cfd, "coefs_for",
        lambda ck, refresh=False: {"b0": 0.4, "source_level": "cell",
                                   "n_clusters": 40, "pooled_n_clusters": 40,
                                   "pooled_from": None})
    r = _run(claim_ids=[11], claims=[_claim()])
    row = c3p.load_recent_pairs("2026-10-12")[0]
    assert row["p_final"] > row["p_base"], (
        f"b0=0.4 ⇒ p_final 必须高于基线，实得 {row['p_final']} vs {row['p_base']}")
    assert r["calib"]["used"] is True and r["calib"]["source"] == "cell"
    assert row["extra"]["calib_source"] == "cell", "来源层必须留痕（否则分不清是不是借来的）"
    assert row["extra"]["calib_n"] == 40


def test_calibration_failure_is_failed_open(env, monkeypatch):
    """校准链路抛异常 ⇒ **不许**带崩推理，退回无系数（p_final == p_base）。"""

    def boom(*a, **k):
        raise RuntimeError("calib down")

    monkeypatch.setattr(c3p._cfd, "coefs_for", boom)
    r = _run(claim_ids=[11], claims=[_claim()])          # 不许抛
    assert r["n_pairs"] == 1
    assert r["calib"]["used"] is False and r["calib"]["source"] == "none"
    row = c3p.load_recent_pairs("2026-10-12")[0]
    assert row["p_final"] == pytest.approx(row["p_base"])


# ═══════════════════════════════════════════════════════════════
#  C3P 输入压缩（2026-10-16）：输入截断 / 输出上限 / 尝试次数全可配
# ═══════════════════════════════════════════════════════════════

def test_extract_evidence_payload_caps_follow_env(monkeypatch):
    """输入 payload 的 user_query/answer 截断长度必须来自 env。"""
    seen = {}
    monkeypatch.setattr(c3p, "_call_llm",
                        lambda system, user, timeout: seen.update(user=user) or "{}")
    monkeypatch.setenv("QD_C3P_MAX_QUERY_CHARS", "10")
    monkeypatch.setenv("QD_C3P_MAX_ANSWER_CHARS", "20")
    c3p.extract_evidence(user_query="Q" * 100, answer="A" * 100,
                         subject="002031", claim={"claim_type": "direction"})
    payload = json.loads(seen["user"])
    assert payload["user_query"] == "Q" * 10
    assert payload["answer"] == "A" * 20


def test_extract_evidence_payload_defaults_are_tight(monkeypatch):
    """默认截断长度已收紧（≤400 / ≤1500），不再是无脑 600/2000。"""
    monkeypatch.delenv("QD_C3P_MAX_QUERY_CHARS", raising=False)
    monkeypatch.delenv("QD_C3P_MAX_ANSWER_CHARS", raising=False)
    seen = {}
    monkeypatch.setattr(c3p, "_call_llm",
                        lambda system, user, timeout: seen.update(user=user) or "{}")
    c3p.extract_evidence(user_query="Q" * 9999, answer="A" * 9999, subject="002031")
    payload = json.loads(seen["user"])
    assert len(payload["user_query"]) <= 400
    assert len(payload["answer"]) <= 1500


def test_call_llm_knobs_from_env(monkeypatch):
    """`_call_llm` 必须传 max_tokens 与 attempts；默认收紧（800 / 1），env 可覆盖。"""
    import app.agent.llm as llm
    captured = {}

    def fake(messages, **kw):
        captured.clear()
        captured.update(kw)
        return type("R", (), {"content": "{}"})()

    monkeypatch.setattr(llm, "complete_direct", fake)
    monkeypatch.delenv("QD_C3P_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("QD_C3P_LLM_ATTEMPTS", raising=False)
    c3p._call_llm("sys", "user", 5.0)
    assert captured["max_tokens"] == 800
    assert captured["attempts"] == 1

    monkeypatch.setenv("QD_C3P_MAX_OUTPUT_TOKENS", "333")
    monkeypatch.setenv("QD_C3P_LLM_ATTEMPTS", "3")
    c3p._call_llm("sys", "user", 5.0)
    assert captured["max_tokens"] == 333
    assert captured["attempts"] == 3
