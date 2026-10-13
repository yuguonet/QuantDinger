r"""数据流端到端模拟报告的两扇「P3 前置门」（TDD RED，2026-10-14）

来源：`app/agent/数据流模拟_20261014.md` S-1 / S-3（其余 7 项已核实属实，
处置顺序另议）。这两项报告列为 **P3 前必修**，理由一致：

    **当前 `coefs=None` 恰好让它们无症状** —— 校准一接线就立刻爆，
    而且爆的方式是「污染校准样本」（标签/样本错了，β 拟合出来的是噪声）。

## S-1 视界抽取：显式窗口必须压过口语词

`chain/claims.py::_HORIZON_RULES` 里 `短线|短期`→T+1 排在 `T\+?3`→T+3 **之前**，
而 `guess_horizon` 是「首个命中即返回」。于是「短线看多,T+3 目标价 1900」
被判成 **T+1** —— 分析者**明示**的窗口被口语词压掉。
标签窗口与声明窗口错位（T+1 与 T+3 的方向可以相反）⇒ 直接污染 T+N 判定。
修法：**两遍扫描**，显式（`T+N` / `N 个交易日` / `三天` 这类带具体期限的）优先。

## S-3 降级门：degraded ⇒ p_final 强制回 p_base

v3 §19.1 红线：「缺证伪条件 ⇒ 本次预测降级，**p_final = p_base**（不给经验加成）」。
原实现里 `degraded` 只是 extra 里的一个 flag，`aggregate()` 照常跑 ⇒
`ev=None` + `coefs={"b0":0.25}` 时 `p_final≈0.61 ≠ p_base`，**降级样本照样吃加成**。

## 易错处（本文件的用例为什么这么写）

· ★ 两条**反向对照**（`test_colloquial_only_*` / `test_healthy_pair_*`）不是凑数：
  它们证明门「只挡降级样本」，而不是一律把 `p_final` 压成 `p_base`。
  少了这两条，一个「恒真守卫」（把 pf 无条件设成 pb）也能全绿。
· ★ 测 S-3 **必须注入假系数**：默认 `coefs=None` ⇒ aggregate 本就 fail-open
  （`p_final == p_base`），不注入的话降级门「有没有都一样」，用例恒绿。
· 一律 patch **`c3p` 模块对象上的属性**（`c3p.extract_evidence` / `c3p.p_base_of`），
  patch `app.agent.chain.evidence.*` 打不到，会误触真源网络。
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from app.agent.chain import c3p
from app.agent.chain import calib_feed as cf
from app.agent.chain import claims as CL
from app.agent.utils import case_memory


# ═══════════════════════════════════════════════════════════════
#  S-1：视界抽取 —— 两遍扫描，显式窗口优先
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    # ★ 核心缺陷：口语词「短线」在规则表里排在 T+3 之前，但不该压过显式 T+3
    ("短线看多,T+3 目标价 1900", "T+3"),
    ("短线看多,三天目标价 1900", "T+3"),
    ("短期偏强,5 个交易日内到位", "T+5"),
    ("短线反弹,T+5 再看", "T+5"),
    ("短线看多,两周内到 12 元", "2W"),
    # 显式与口语指向同一格时，结果不变（不能有回归）
    ("T+1 冲高回落", "T+1"),
    ("T+3 目标价 12 元", "T+3"),
])
def test_explicit_horizon_beats_colloquial(text, expected):
    """★ 显式期限（`T+N` / `N 个交易日` / 中文数字天周月）必须压过口语词。

    修之前：`_HORIZON_RULES` 首个命中即返回 ⇒ 「短线」把明示的 T+3 压成 T+1。
    """
    assert CL.guess_horizon(text) == expected, (
        f"{text!r} 的**明示**窗口是 {expected}，被口语词压掉了 ⇒ "
        f"标签窗口与声明窗口错位（T+1 与 T+3 方向可相反），会污染 T+N 判定")


@pytest.mark.parametrize("text,expected", [
    ("短线看多", "T+1"),
    ("短期偏强", "T+1"),
    ("明天会涨", "T+1"),
    ("本周会涨", "T+3"),
    ("中期看涨", "1M"),
    ("月内到 15 元", "1M"),
])
def test_colloquial_only_still_uses_first_hit(text, expected):
    """反向对照：**只有口语词**时仍按原行为（规则表首个命中）。

    证明两遍扫描没有把所有口语词都判成"不显式"而退化成永远返回 default。
    """
    assert CL.guess_horizon(text) == expected


def test_no_hit_falls_back_to_default():
    """一个词都没命中 ⇒ 用域默认（默认 T+3），不许返回空/None。"""
    assert CL.guess_horizon("") == "T+3"
    assert CL.guess_horizon("完全没有期限表述") == "T+3"
    assert CL.guess_horizon("看多", default="1M") == "1M"


# ═══════════════════════════════════════════════════════════════
#  S-3：降级门 —— degraded ⇒ p_final = p_base（不给经验加成）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """全绿环境（与 `test_v5_c3p.py::env` 同构）：开关开、pair 落 tmp、
    p_base 给固定值、evidence 带证伪条件（⇒ **不降级**）。"""
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
    kw.setdefault("claim_ids", [11])
    kw.setdefault("claims", [_claim()])
    kw.setdefault("as_of", "2026-10-12")
    return c3p.run_for_decision(**kw)


def _inject_coefs(monkeypatch, b0=0.25):
    """注入假系数 —— **没有它，S-3 的门有没有都一样**（coefs=None ⇒ 本就 fail-open）。"""
    monkeypatch.setattr(
        c3p._cfd, "coefs_for",
        lambda ck, refresh=False: {"b0": b0, "source_level": "cell",
                                   "n_clusters": 40, "pooled_n_clusters": 40,
                                   "pooled_from": None})


def test_degraded_pair_gets_no_experience_bonus(env, monkeypatch):
    """★ 核心缺陷：evidence 取不到 ⇒ degraded ⇒ **p_final 必须回 p_base**。

    即使 coefs 给了正 b0（本该把 p 推高），降级样本也不许吃这份加成。
    修之前：`degraded` 只是 extra 里的 flag，aggregate 照跑 ⇒ p_final 被推高。
    """
    monkeypatch.setattr(c3p, "extract_evidence", lambda **kw: None)   # ⇒ degraded
    _inject_coefs(monkeypatch, b0=0.25)
    r = _run()
    assert r["degraded"] is True
    row = c3p.load_recent_pairs("2026-10-12")[0]
    assert row["p_base"] == pytest.approx(0.55)
    assert row["p_final"] == pytest.approx(row["p_base"]), (
        f"降级样本吃了经验加成（p_final={row['p_final']} > p_base={row['p_base']}）⇒ "
        f"违反 v3 §19.1：缺证伪条件 ⇒ p_final = p_base")


def test_evidence_without_falsifiers_is_also_gated(env, monkeypatch):
    """证据抽到了、但**没有证伪条件** ⇒ 同样是降级，同样不给加成。

    与「evidence 整个取不到」是两种情形，设计上都是 `degraded`
    （`degraded = not bool(ev.falsifiers)`），两条都要挡住。
    """
    monkeypatch.setattr(
        c3p, "extract_evidence",
        lambda **kw: {"ev_ver": "ev1", "condition_key": "down|low",
                      "falsifiers": [],                       # ★ 空
                      "discriminators": [{"key": "放量", "computable": True}]})
    _inject_coefs(monkeypatch, b0=0.25)
    r = _run()
    assert r["degraded"] is True
    row = c3p.load_recent_pairs("2026-10-12")[0]
    assert row["p_final"] == pytest.approx(row["p_base"])


def test_healthy_pair_still_gets_experience_bonus(env, monkeypatch):
    """★ 反向对照：**没降级**（有证伪条件）时，系数必须照常生效。

    缺了这条，一个「无条件 pf = pb」的恒真实现也能让上面两条全绿 ——
    那等于把 Δp 永远关掉，比原 bug 更糟（静默、且看不出来）。
    """
    _inject_coefs(monkeypatch, b0=0.25)
    r = _run()
    assert r["degraded"] is False, "env 的 evidence 带 falsifiers ⇒ 不该降级"
    row = c3p.load_recent_pairs("2026-10-12")[0]
    assert row["p_final"] > row["p_base"], (
        f"b0=0.25 且未降级 ⇒ p_final 必须高于基线，实得 "
        f"{row['p_final']} vs {row['p_base']}（降级门误伤了正常样本）")


def test_degraded_gate_does_not_drop_the_pair(env, monkeypatch):
    """降级门**只压 p_final**，不许顺手把 pair 也丢了。

    G20 的「降级比例」需要这个分母 —— 不记就永远分不清「没经验」还是「没产出」。
    """
    monkeypatch.setattr(c3p, "extract_evidence", lambda **kw: None)
    _inject_coefs(monkeypatch)
    r = _run()
    assert r["n_pairs"] == 1
    assert c3p.load_recent_pairs("2026-10-12")[0]["extra"]["degraded"] is True


def test_degraded_pair_is_not_labelled_as_calibrated(env, monkeypatch):
    """★ 降级 ⇒ 留痕不许写「用了本格系数」：`source=degraded` 且 `used=False`。

    门只压 `p_final` 却不动留痕的话，降级样本会被记成
    「`calib_source=cell`（用了本格系数）但 Δp=0」 —— 事后按来源层分组统计时
    出现自相矛盾的行（把没用的当成用了 = 静默放大）。
    """
    monkeypatch.setattr(c3p, "extract_evidence", lambda **kw: None)
    _inject_coefs(monkeypatch)
    r = _run()
    assert r["calib"]["used"] is False, "降级 ⇒ 系数拿来了也没用，不许记成 used"
    assert r["calib"]["source"] == "degraded"
    row = c3p.load_recent_pairs("2026-10-12")[0]
    assert row["extra"]["calib_source"] == "degraded"


# ═══════════════════════════════════════════════════════════════
#  证据契约：`evidence` 必须可 JSON 序列化（cli 实测抓到的静默断裂）
# ═══════════════════════════════════════════════════════════════
#  2026-10-14 跑 cli 真实对话时，`case_memory.record_case` 抛
#  `TypeError: Object of type date is not JSON serializable` ⇒ **案例一条都进不了库**
#  （fail-open，只有一条 warn）。根因：LLM 没产出 claims 时 `extract_evidence`
#  直接拿原始 claim 兜底，而 `claim["due_date"]` 是 `date` 对象
#  （`claims.due_date_of()` 有意返回 date —— §9：不改列类型、不改扫描 SQL）。

def test_evidence_from_date_bearing_claim_is_json_serializable(monkeypatch):
    """★ `due_date` 是 `date` 对象 ⇒ evidence **仍必须可 JSON 序列化**。

    ⚠ 为什么这个 bug 能潜伏：`append_evidence` 落盘用的是 `default=str`，
      JSONL 里看到的是规规矩矩的 `"2026-10-20"` —— 落盘看不出问题，
      只有走 `record_case` 那条路才炸。所以这里**不许**用 `default=str` 断言，
      要断言"裸 dumps 不抛"。
    """
    monkeypatch.setattr(
        c3p, "_call_llm",
        lambda *a, **k: json.dumps({"claims": [],       # ⇒ 空 ⇒ 走原始 claim 兜底
                                    "discriminators": [], "falsifiers": []}))
    monkeypatch.setattr(c3p._evidence, "append_evidence", lambda ev: None)
    claim = {"claim_type": "direction", "subject": "600519", "subject_kind": "stock",
             "horizon": "T+3", "predicted": {"direction": "up"},
             "confidence": 0.6, "source_quote": "看多", "seq": 1,
             "due_date": date(2026, 10, 20)}            # ★ date，不是 str
    ev = c3p.extract_evidence(user_query="茅台明天怎么走", answer="看多",
                              subject="600519", frame={"as_of": "2026-10-13"},
                              claim=claim)
    assert ev is not None
    json.dumps(ev, ensure_ascii=False)                  # 裸 dumps：不许抛
    # 入库那一跳（outcome）也不许抛 —— 案例库为空就是死在这一行
    json.dumps(case_memory._build_outcome(0.6, ev), ensure_ascii=False)


def test_json_safe_is_idempotent_on_plain_values():
    """`_json_safe` 只动日期类型，其余原样返回（不许把数字/字符串也转掉）。"""
    assert c3p._json_safe({"a": 1, "b": "x", "c": None, "d": [1, "y"]}) == {
        "a": 1, "b": "x", "c": None, "d": [1, "y"]}
    assert c3p._json_safe(date(2026, 10, 20)) == "2026-10-20"
    assert c3p._json_safe(datetime(2026, 10, 20, 9, 30)) == "2026-10-20T09:30:00"


# ═══════════════════════════════════════════════════════════════
#  S-3 的另一侧：拟合侧（calib_feed.build_samples 不收降级样本）
# ═══════════════════════════════════════════════════════════════
#  c3p 侧管的是「这次预测吃不吃加成」，拟合侧管的是「这条样本能不能去决定
#  **给别人**的加成」。缺任何一侧，冷启动期都没症状（coefs=None ⇒ Δp 恒 0），
#  但格子攒够 30 簇那天起，降级样本会**既进拟合又吃加成**。

@pytest.fixture()
def calib_env(monkeypatch):
    monkeypatch.setenv("QD_CALIB_HORIZON", "T+1")      # 与 _cf_pair 的 horizon 对齐
    monkeypatch.setenv("QD_CALIB_EXCLUDE_DEGRADED", "1")
    cf.reset_cache()


def _cf_pair(cid, ck="down|low", day="2026-10-01", pb=0.5, hz="T+1",
             degraded=False):
    return {"as_of": day, "claim_id": cid, "p_base": pb, "p_final": pb,
            "condition_key": ck,
            "extra": {"horizon": hz, "subject": "000001", "degraded": degraded}}


def _cf_res(cid, rid, verdict="hit"):
    return {"claim_id": cid, "res_id": rid, "verdict": verdict,
            "verdict_score": None}


def test_build_samples_drops_degraded_pairs(calib_env):
    """★ 拟合侧降级门：`extra.degraded` 的 pair **不许进拟合样本**。

    §19.1 的同一条纪律：降级样本自己的预测都不给加成，也不该让它参与决定
    别人的 β₀ —— 它的证伪条件缺失，这条经验没经过对照。
    """
    s = cf.build_samples(
        [_cf_pair(1, degraded=True), _cf_pair(2, degraded=False)],
        [_cf_res(1, 10), _cf_res(2, 20)])
    assert [x["claim_id"] for x in s] == [2], (
        f"降级样本进了拟合：{s} ⇒ 格子攒够那天起会污染 β")


def test_build_samples_keeps_healthy_pairs(calib_env):
    """反向对照：不降级的照收 —— 门不是「把样本全滤掉」的恒真实现。"""
    s = cf.build_samples([_cf_pair(1), _cf_pair(2)],
                         [_cf_res(1, 10), _cf_res(2, 20)])
    assert sorted(x["claim_id"] for x in s) == [1, 2]


def test_degraded_filter_is_env_toggleable(calib_env, monkeypatch):
    """★ 口径必须是 env 开关，不许硬编码。

    反直觉但重要：降级样本的 `p_final` 恒 = `p_base` ⇒ 它们是**不含自我修正**
    的最干净样本，而 β₀ 的 estimand 正是「实际胜率 − p_base 均值」。
    ⇒ 若日后格子因样本量迟迟达不到 30 簇，关掉开关让降级样本回流即可，
    不必改代码。
    """
    monkeypatch.setenv("QD_CALIB_EXCLUDE_DEGRADED", "0")
    s = cf.build_samples([_cf_pair(1, degraded=True)], [_cf_res(1, 10)])
    assert [x["claim_id"] for x in s] == [1], (
        "开关关掉后降级样本必须能回流（否则样本不够时只能改代码）")
