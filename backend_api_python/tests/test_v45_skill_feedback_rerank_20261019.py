# -*- coding: utf-8 -*-
"""阶段 5 守卫：技能效果反馈软重排（Top-K 池内轻量重排 + 旧排序读口摘除）（2026-10-19）。

对应 `tmp/skill_system_rework_implementation_plan_20261018.md` §9 阶段 5 /
§14.3 / §15.4 / §11 回滚：

  ① **唯一反馈来源（§14.3）**：技能候选排序的反馈只来自新链
     `skill_events.weight_of()`（qd_skill_feedback）；旧
     `qd_agent_weights.layer='skill'` 排序读口已摘除（qd_service._rank_skills
     不再消费），避免排序双账本。旧治理链（evaluator 写入 /
     skill_brewer.maybe_revise / weight_hints 其余三层）保留不动。
  ② **冷启动恒等**：无反馈记录 / 低于样本门槛 / DB 不可用 ⇒ 权重恒 1.0 ⇒
     排序与纯词面**逐位一致**（安全性由构造保证：门槛 + 限幅 + 只调序
     不过滤，不靠开关）。
  ③ **池内轻量重排**：final = base_score × weight；只改序不过滤——
     eligible 集合、注入门槛（QD_SKILL_MIN_SCORE）、探索位资格全不变；
     并列按 (final, base, name) 确定性裁决（无名字序假象）。
  ④ **可关闭灰度（§11）**：QD_SKILL_FEEDBACK_RANK=0 ⇒ 回退纯词面（与
     阶段 3 逐位一致），meta 仍记「若启用会怎样」（would_reorder）——
     启用前后排序差异可对照。
  ⑤ **排序 trace（§14.3）**：rank_metadata / report / 事件快照记基础分、
     反馈权重、样本数、版本、最终分——新排序链可按 ranker/policy 版本复现。

测试纪律：monkeypatch（skill_events.feedback_detail / registry / env），
不碰真库、不写真技能目录。
"""
from __future__ import annotations

import ast
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_SERVICE = BACKEND / "app" / "agent" / "qd_service.py"
TOOL_PRESELECT = BACKEND / "app" / "agent" / "preselect" / "tool_preselect.py"

from app.agent.chain import skill_events as se
from app.agent.chain import skill_registry as sr
from app.agent.preselect import candidate_index as ci

# 与 v43 同款查询：含 6 位代码过域闸门、无域词不过快路由、词面高相关。
_QUERY = "分析600519"


# ── 夹具（与 v43 同型）────────────────────────────────────────────

def _skill(name: str, desc: str, tools=()) -> dict:
    return {"name": name, "type": "markdown", "description": desc,
            "tools": list(tools)}


def _eligible_desc(tag: str = "个股") -> str:
    return f"{tag}资金面与筹码分布分析：K线走势复盘"


class _FakeSkills:
    def __init__(self, items):
        self._items = [dict(it) for it in items]

    def list_skills(self):
        return [dict(it) for it in self._items]

    def get(self, name):
        for it in self._items:
            if it.get("name") == name:
                return SimpleNamespace(name=name)
        return None

    def load_body(self, name):
        return "## 步骤\n1. 用工具取数\n2. 出结论\n"


def _patch_registry(monkeypatch, *, states=None, recent=None):
    if states is not None:
        monkeypatch.setattr(sr, "get_lifecycle_states",
                            lambda ids: {i: states.get(i, "active") for i in ids})
    monkeypatch.setattr(sr, "get_active_versions",
                        lambda ids: {i: "v1" for i in ids})
    monkeypatch.setattr(sr, "recent_published_skill_ids",
                        lambda days: (None if recent is None else set(recent)))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("QD_PRESELECT_SKILL", "QD_PRESELECT_SKILL_TOPK", "QD_SKILL_TOPK",
                "QD_SKILL_MIN_SCORE", "QD_SKILL_EXPLORE_TTL_DAYS",
                "QD_PRESELECT_SKILL_TOOLS_MAX", "QD_PRESELECT_CATALOG_MAX",
                "QD_PRESELECT_TIER_CHARS", "QD_SKILL_FEEDBACK_RANK",
                "QD_SKILL_FEEDBACK_MIN_SAMPLES", "QD_SKILL_FEEDBACK_CACHE_TTL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ci, "_EXPOSURE", OrderedDict())
    with se._CACHE_LOCK:
        se._CACHE.clear()
    yield


@pytest.fixture
def fb(monkeypatch):
    """注入反馈明细读口：{skill_id: {"weight": w, "eligible_count": n}}。"""
    rows: dict = {}

    def _detail(skill_id, version=""):
        return rows.get(str(skill_id or ""))

    monkeypatch.setattr(se, "feedback_detail", _detail)
    return rows


def _mk_cands(specs):
    """specs: (skill_id, name, base) → Candidate（模拟 rank_skills_scored 输出）。"""
    out = []
    for cid, nm, base in specs:
        c = ci.Candidate(kind="skill", id=cid, name=nm, base_score=float(base),
                         skill_version="v1")
        c.rank_metadata = {"reasons": ["词面命中: 分析"], "kind": "skill"}
        out.append(c)
    return out


def _func_node(src: str, name: str):
    tree = ast.parse(src)
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
        stack.extend(ast.iter_child_nodes(node))
    raise AssertionError(f"找不到函数 {name}")


# ═══════════════════════════════════════════════════════════════════
# 一、灰度开关（QD_SKILL_FEEDBACK_RANK）
# ═══════════════════════════════════════════════════════════════════

def test_feedback_rank_switch_default_on_env_off(monkeypatch):
    assert ci.feedback_rank_enabled() is True, "默认开（灰度态）"
    for v in ("0", "false", "no", "off", "OFF", "False"):
        monkeypatch.setenv("QD_SKILL_FEEDBACK_RANK", v)
        assert ci.feedback_rank_enabled() is False, f"{v!r} 应关"
    monkeypatch.setenv("QD_SKILL_FEEDBACK_RANK", "garbage")
    assert ci.feedback_rank_enabled() is True, "脏值回落默认（不抛、不静默改语义）"


# ═══════════════════════════════════════════════════════════════════
# 二、apply_feedback_rerank：冷启动恒等 / 乘子 / 并列 / 不过滤 / 影子
# ═══════════════════════════════════════════════════════════════════

def test_rerank_cold_start_identity(fb):
    """无反馈记录 ⇒ 权重 1.0 ⇒ 序与分量逐位一致（不误伤基线）。"""
    cands = _mk_cands([("auto-a", "auto-a", 5.0), ("auto-b", "auto-b", 3.0)])
    ordered, meta = ci.apply_feedback_rerank(cands)
    assert [c.name for c in ordered] == ["auto-a", "auto-b"], "冷启动恒等"
    for c in ordered:
        assert c.rank_metadata["feedback"] == {
            "weight": 1.0, "samples": 0, "final": c.base_score, "applied": True}
    assert meta["enabled"] is True and meta["would_reorder"] is False


def test_rerank_multiplies_base_by_weight(fb):
    """final = base × weight：低权重高分可被高权重低分反超（乘子语义）。"""
    cands = _mk_cands([("auto-a", "auto-a", 5.0), ("auto-b", "auto-b", 4.6)])
    fb["auto-a"] = {"weight": 0.9, "eligible_count": 20}
    fb["auto-b"] = {"weight": 1.1, "eligible_count": 20}
    ordered, meta = ci.apply_feedback_rerank(cands)
    assert [c.name for c in ordered] == ["auto-b", "auto-a"], \
        "5.0×0.9=4.5 < 4.6×1.1=5.06 ⇒ 反超"
    fm = {c.name: c.rank_metadata["feedback"] for c in ordered}
    assert fm["auto-a"]["final"] == 4.5 and fm["auto-b"]["final"] == 5.06
    assert fm["auto-a"]["samples"] == 20 and fm["auto-a"]["applied"] is True
    assert meta["would_reorder"] is True
    assert meta["weights"] == {"auto-a": 0.9, "auto-b": 1.1}


def test_rerank_tie_break_deterministic(fb):
    """final 并列 ⇒ base 降序 ⇒ name 升序（无名字序假象、可复现）。

    ⚠ 名字序必须与 base 序**反向**：同向时「按名字排」与「按 base 排」
    结果重合，断言无鉴别力（P3 变异：排序键丢 base）——这是本轮实测教训。
    """
    fb["auto-z"] = {"weight": 0.9, "eligible_count": 20}   # 5.0×0.9=4.5
    cands = _mk_cands([("auto-a", "auto-a", 4.5), ("auto-z", "auto-z", 5.0)])
    ordered, _ = ci.apply_feedback_rerank(cands)
    assert [c.name for c in ordered] == ["auto-z", "auto-a"], \
        "final 并列 4.5 ⇒ base 降序（z=5.0 前于 a=4.5），不是名字序"
    # 同名同分同权 ⇒ name 升序（与输入顺序无关）
    cands2 = _mk_cands([("auto-z", "auto-z", 3.0), ("auto-y", "auto-y", 3.0)])
    ordered2, _ = ci.apply_feedback_rerank(cands2)
    assert [c.name for c in ordered2] == ["auto-y", "auto-z"]


def test_rerank_never_filters(fb):
    """只调序不过滤：低权重技能不得被踢出 eligible（§15.4）。"""
    cands = _mk_cands([("auto-a", "auto-a", 5.0), ("auto-b", "auto-b", 3.0),
                       ("auto-c", "auto-c", 2.0)])
    fb["auto-a"] = {"weight": 0.9, "eligible_count": 20}
    fb["auto-b"] = {"weight": 0.9, "eligible_count": 20}
    ordered, _ = ci.apply_feedback_rerank(cands)
    assert {c.name for c in ordered} == {"auto-a", "auto-b", "auto-c"}
    assert [c.name for c in ordered] == ["auto-a", "auto-b", "auto-c"], \
        "同乘子 ⇒ 相对序不变"


def test_rerank_off_keeps_lexical_and_records_shadow(fb, monkeypatch):
    """关闭 ⇒ 纯词面序；影子仍记 weight/final/would_reorder（启用前后可对照）。"""
    monkeypatch.setenv("QD_SKILL_FEEDBACK_RANK", "0")
    cands = _mk_cands([("auto-a", "auto-a", 5.0), ("auto-b", "auto-b", 4.6)])
    fb["auto-a"] = {"weight": 0.9, "eligible_count": 20}
    fb["auto-b"] = {"weight": 1.1, "eligible_count": 20}
    ordered, meta = ci.apply_feedback_rerank(cands)
    assert [c.name for c in ordered] == ["auto-a", "auto-b"], "关闭 ⇒ 词面序"
    assert meta["enabled"] is False and meta["would_reorder"] is True
    f = ordered[1].rank_metadata["feedback"]
    assert f["applied"] is False and f["final"] == 5.06 and f["weight"] == 1.1


def test_rerank_read_failure_is_neutral(monkeypatch):
    """反馈读取异常 ⇒ 中性 1.0、序不变、不抛（fail-soft）。"""
    def _boom(skill_id, version=""):
        raise RuntimeError("no db")

    monkeypatch.setattr(se, "feedback_detail", _boom)
    cands = _mk_cands([("auto-a", "auto-a", 5.0), ("auto-b", "auto-b", 3.0)])
    ordered, meta = ci.apply_feedback_rerank(cands)
    assert [c.name for c in ordered] == ["auto-a", "auto-b"]
    assert meta["would_reorder"] is False


# ═══════════════════════════════════════════════════════════════════
# 三、接线 build_skill_catalog_topk：meta + 候选快照 + 常规位改序
# ═══════════════════════════════════════════════════════════════════

def _four_hot():
    return _FakeSkills([_skill(f"auto-hot{i}", _eligible_desc()) for i in (1, 2, 3, 4)])


def test_topk_meta_and_snapshot_carry_feedback(fb, monkeypatch):
    """meta.feedback + candidates 快照的 fb_weight/fb_samples/final_score/version。"""
    from app.agent.preselect import build_skill_catalog_topk

    _patch_registry(monkeypatch, states={}, recent=None)
    fb["auto-hot1"] = {"weight": 0.9, "eligible_count": 20}
    fb["auto-hot2"] = {"weight": 1.1, "eligible_count": 20}
    _sec, _tmap, meta = build_skill_catalog_topk(_four_hot(), _QUERY)
    fbm = meta["feedback"]
    assert fbm["enabled"] is True and fbm["would_reorder"] is True
    assert set(fbm["weights"]) >= {"auto-hot1", "auto-hot2"}
    # 低权重 hot1（final 2.7）被挤出常规位；仍在 candidates（调序不过滤）
    assert meta["shown"] == ["auto-hot2", "auto-hot3", "auto-hot4"], \
        "final: hot2=3.3 > hot3=hot4=3.0 > hot1=2.7"
    cands = {c["name"]: c for c in meta["candidates"]}
    assert cands["auto-hot1"]["exposed"] is False, "落选仍在快照（漏斗判据）"
    assert cands["auto-hot1"]["fb_weight"] == 0.9
    assert cands["auto-hot1"]["fb_samples"] == 20
    assert cands["auto-hot1"]["version"] == "v1"
    assert cands["auto-hot1"]["final_score"] == \
        round(cands["auto-hot1"]["base_score"] * 0.9, 6), "最终分 = 基础分×权重"


def test_topk_off_matches_lexical_baseline(fb, monkeypatch):
    """开关关 ⇒ 展示序与阶段 3 纯词面逐位一致（回退铁律）。"""
    from app.agent.preselect import build_skill_catalog_topk

    monkeypatch.setenv("QD_SKILL_FEEDBACK_RANK", "0")
    _patch_registry(monkeypatch, states={}, recent=None)
    fb["auto-hot1"] = {"weight": 0.9, "eligible_count": 20}
    fb["auto-hot2"] = {"weight": 1.1, "eligible_count": 20}
    _sec, _tmap, meta = build_skill_catalog_topk(_four_hot(), _QUERY)
    assert meta["shown"] == ["auto-hot1", "auto-hot2", "auto-hot3"], \
        "关闭 ⇒ 纯词面序（同分按名）"
    assert meta["feedback"]["enabled"] is False
    assert meta["feedback"]["would_reorder"] is True, "影子：若启用会改序"


def test_topk_wiring_calls_rerank():
    """build_skill_catalog_topk 必须接 apply_feedback_rerank 并留痕。"""
    src = TOOL_PRESELECT.read_text(encoding="utf-8")
    body = ast.unparse(_func_node(src, "build_skill_catalog_topk"))
    assert "apply_feedback_rerank" in body, "反馈重排接线被摘 ⇒ 新链静默失效"
    assert "final_score" in body and "fb_weight" in body, \
        "候选快照必须记最终分与反馈权重（排序 trace，§14.3）"


# ═══════════════════════════════════════════════════════════════════
# 四、旧 layer='skill' 排序读口摘除（§14.3 验收：排序仅读新反馈 API）
# ═══════════════════════════════════════════════════════════════════

def test_qd_service_no_longer_reads_old_skill_ledger():
    """qd_service 不得再出现 _skill_weight，_rank_skills 不得调任何权重读口。

    判据取**调用级 + 导入级**（非字样）：
      · docstring 说明「读口搬到哪」是文档不是消费，文本断言会把文档误判
        成回接（假绿的反面——假红会逼人删文档）；
      · 只查调用名不够：`from .chain.weight_hints import weight_of as _whw`
        是别名回接（P7 实测穿过调用名黑名单）⇒ 必须同时查导入的
        (module, name)——别名改名改不掉 module 与原名。
    """
    src = QD_SERVICE.read_text(encoding="utf-8")
    assert "_skill_weight" not in src, \
        "旧 layer='skill' 排序读口（_skill_weight）必须摘除（§14.3 双账本）"
    node = _func_node(src, "_rank_skills")
    called, imported = set(), set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            called.add(getattr(f, "id", "") or getattr(f, "attr", ""))
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                imported.add((n.module or "", a.name))
    assert not (called & {"weight_of", "weight_hints", "snapshot",
                          "feedback_detail"}), \
        f"_rank_skills 不得调权重读口（发现: {sorted(called)}）"
    assert not any(m.endswith("weight_hints") or nm == "weight_of"
                   for m, nm in imported), \
        f"_rank_skills 不得导入权重读口（含别名回接）: {sorted(imported)}"


def test_rank_skills_is_pure_dictionary_order(monkeypatch):
    """摘除后 _rank_skills = 纯确定性词典序，2 元组 (分, 名)。"""
    from app.agent import qd_service as qs

    cands = [{"name": "alpha", "description": "d"},
             {"name": "beta", "description": "d"}]
    _raw = {"alpha": 5, "beta": 3}
    monkeypatch.setattr(qs, "_skill_score",
                        lambda hay, keys, msg, ent: next(
                            (v for n, v in _raw.items() if n in hay), 0))
    ranked = qs._rank_skills(cands, set(), "", False)
    assert ranked == [(5, "alpha"), (3, "beta")], "无乘子：分量即总分"


def test_old_governance_chain_retained():
    """旧治理链保留（§14.3）：weight_hints/evaluator/skill_brewer 不因摘除而动。"""
    wh = (BACKEND / "app" / "agent" / "chain" / "weight_hints.py")
    src = wh.read_text(encoding="utf-8")
    assert "def weight_of" in src and "def snapshot" in src, \
        "weight_hints 模块（tool/chain/domain 三层消费）保留"
    ev = (BACKEND / "app" / "agent" / "chain" / "evaluator.py")
    assert "qd_agent_weights" in ev.read_text(encoding="utf-8"), \
        "evaluator 旧权重写入保留（治理/修订信号）"
