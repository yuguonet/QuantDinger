# -*- coding: utf-8 -*-
"""差距 K（工具预选可学习）——共现权重的退化 / 排序 / held-out 召回率逻辑测试。

覆盖三块验收逻辑：
  1. 冷启动退化：无权重 / 样本不足 / 零覆盖 → `rank_candidates` 与现状原序一致；
  2. 有统计时排序变化：共现问法能把目标工具顶到候选面首位；
  3. held-out 召回率计算：「实际调用工具 ∈ 预选面」的命中率口径 + ≥95% 判定。

约定：与生产同款**裸名导入**（app/agent 先进 sys.path），避免同一模块双实例。
"""
import importlib
import inspect
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_AGENT = os.path.join(_BACKEND, "app", "agent")
for _p in (_BACKEND, _AGENT):          # 与 app/agent/__init__ 同序：agent 在前
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

from tools.preselect_cooc import (  # noqa: E402
    build_weights, get_weights, heldout_recall, load_pairs, load_weights,
    rank_candidates, save_weights, weights_path,
)
from tools import preselect_stats as PS  # noqa: E402


def _pairs_repeat(query: str, tools: list, n: int) -> list:
    return [{"query": query, "tools": list(tools)} for _ in range(n)]


def test_cold_start_degrades_to_heuristic(tmp_path, monkeypatch):
    """冷启动：权重文件不存在 / 样本不足 / 零覆盖 → 原序（bit 级一致）。"""
    names = ["b_tool", "a_tool", "c_tool"]

    # ① 无权重（冷启动常态）
    assert rank_candidates(names, "打板", None) == names
    assert rank_candidates(names, "打板", {}) == names

    # ② 样本不足 → build_weights 直接不产出（线上等价于无权重）
    few = _pairs_repeat("明天打板抓涨停", ["get_limit_pool"], 5)
    assert build_weights(few, min_pairs=50) is None
    assert build_weights(few, min_pairs=10) is None      # 5 < 10 仍不足

    # ③ 权重有效但这次问法零覆盖 → 原序不动
    pairs = _pairs_repeat("明天打板抓涨停", ["get_limit_pool"], 60)
    w = build_weights(pairs, min_pairs=10)
    assert w is not None
    assert rank_candidates(names, "今天天气怎么样", w) == names

    # ④ 权重文件不存在 / 损坏 → get_weights/load_weights 返回 None（= 退化）
    monkeypatch.setenv("QD_PRESELECT_COOC_FILE", str(tmp_path / "none.json"))
    assert load_weights() is None
    assert get_weights() is None
    bad = tmp_path / "bad.json"
    bad.write_text("{broken json", encoding="utf-8")
    monkeypatch.setenv("QD_PRESELECT_COOC_FILE", str(bad))
    assert load_weights() is None

    # ⑤ 版本不符 / 样本不足的权重文件同样拒绝
    import json as _json
    old = tmp_path / "old.json"
    old.write_text(_json.dumps({"version": 99, "total_pairs": 999,
                                "min_pairs": 1, "token_tool": {"打板": {"x": 1}}}),
                   encoding="utf-8")
    monkeypatch.setenv("QD_PRESELECT_COOC_FILE", str(old))
    assert load_weights() is None


def test_ranking_changes_with_stats():
    """有统计时：共现问法把目标工具顶到首位，其余候选保持原相对顺序。"""
    names = ["search_stocks", "get_hot_rank", "get_limit_pool", "get_market_overview"]
    pairs = _pairs_repeat("明天打板抓涨停", ["get_limit_pool"], 60)
    w = build_weights(pairs, min_pairs=10)
    assert w is not None

    ranked = rank_candidates(names, "今天打板吗", w)
    assert ranked != names, "有共现权重时排序应发生变化"
    assert ranked[0] == "get_limit_pool", ranked
    # 同分保持原序（稳定排序）：非共现工具相对顺序不变
    rest_names = [n for n in names if n != "get_limit_pool"]
    rest_ranked = [n for n in ranked if n != "get_limit_pool"]
    assert rest_ranked == rest_names

    # 权重样本不足门槛（min_pairs 大于现有量）→ 即便 build 出了 dict 也退化
    w_low = dict(w)
    w_low["min_pairs"] = 10_000
    assert rank_candidates(names, "今天打板吗", w_low) == names


def test_heldout_recall_computation():
    """held-out 召回率口径：按 (任务,实际调用工具) 计命中；≥95% 判定。"""
    pairs = [
        {"query": "q1", "tools": ["a", "b"]},
        {"query": "q2", "tools": ["c"]},
        {"query": "q3", "tools": []},          # 无工具任务不进分母
    ]
    faces = {"q1": ["a", "b"], "q2": []}
    r = heldout_recall(pairs, lambda q: faces.get(q, []))
    assert r["tasks"] == 2 and r["calls"] == 3 and r["hits"] == 2
    assert r["recall"] == 0.6667               # 2/3
    assert r["task_all_rate"] == 0.5           # q1 全中、q2 全漏
    assert r["pass_95"] is False
    assert r["misses"] and r["misses"][0]["tool"] == "c"

    # 达标场景：40 次调用中 39 次在面内 = 97.5% ≥ 95%
    ok_pairs = [{"query": "q", "tools": ["t1", "t2"]} for _ in range(20)]
    r2 = heldout_recall(ok_pairs, lambda q: ["t1"] if q else [])
    # 20 任务 × 2 调用，面只含 t1 → 20/40 = 50%
    assert r2["recall"] == 0.5 and r2["pass_95"] is False
    r3 = heldout_recall(ok_pairs, lambda q: ["t1", "t2"])
    assert r3["recall"] == 1.0 and r3["pass_95"] is True


def test_heldout_recall_with_weighted_face():
    """端到端逻辑：训练段攒权重 → 加权面 → held-out 真实调用工具全在面内。"""
    train = (_pairs_repeat("打板选股", ["get_limit_pool", "search_stocks"], 30)
             + _pairs_repeat("看看资金流", ["get_fund_flow"], 30))
    w = build_weights(train, min_pairs=10)
    assert w is not None
    pool = ["search_stocks", "get_limit_pool", "get_fund_flow", "get_hot_rank"]
    heldout = [
        {"query": "我要打板", "tools": ["get_limit_pool"]},
        {"query": "主力资金流如何", "tools": ["get_fund_flow"]},
    ]
    r = heldout_recall(heldout, lambda q: rank_candidates(pool, q, w)[:2])
    assert r["recall"] == 1.0 and r["pass_95"] is True, r


def test_record_task_calls_roundtrip(tmp_path, monkeypatch):
    """配对通道：record_task_calls 落 JSONL ↔ load_pairs 读回（同一通道）。"""
    monkeypatch.setenv("QD_PRESELECT_PAIRS_FILE", str(tmp_path / "pairs.jsonl"))
    PS.reset()
    out = PS.record_task_calls("帮我看看茅台的资金流",
                               ["get_fund_flow", "get_fund_flow", ""])
    assert out and out["pairs"] == 1
    # 空问题 / 空工具链不记
    assert PS.record_task_calls("", ["x"]) is None
    assert PS.record_task_calls("q", []) is None
    # 坏行跳过不毁整份
    with open(str(tmp_path / "pairs.jsonl"), "a", encoding="utf-8") as f:
        f.write("not-json\n")
    pairs = load_pairs()
    assert pairs == [{"query": "帮我看看茅台的资金流", "tools": ["get_fund_flow"]}]
    assert PS.report()["pairs_written"] == 1


def test_weights_save_load_roundtrip(tmp_path, monkeypatch):
    """权重落盘/读回 + get_weights 按 mtime 失效。"""
    monkeypatch.setenv("QD_PRESELECT_COOC_FILE", str(tmp_path / "w.json"))
    pairs = _pairs_repeat("打板战法", ["get_limit_pool"], 60)
    w = build_weights(pairs, min_pairs=10)
    assert save_weights(w) is True
    assert weights_path() == tmp_path / "w.json"
    loaded = load_weights()
    assert loaded and loaded["total_pairs"] == 60
    assert get_weights()["total_pairs"] == 60
    # 文件被换 → mtime 变 → 缓存失效读新值
    w2 = dict(w)
    w2["total_pairs"] = 120
    save_weights(w2)
    os.utime(str(tmp_path / "w.json"), (1_900_000_000, 1_900_000_000))
    assert get_weights()["total_pairs"] == 120
    # 空权重不落盘
    assert save_weights(None) is False


def test_not_tools_coverage():
    """新模块公开函数必须全部退出工具注册（否则静默多一份必注入 schema）。"""
    cooc = importlib.import_module("tools.preselect_cooc")
    pub = {n for n, o in vars(cooc).items()
           if inspect.isfunction(o) and getattr(o, "__module__", "") == cooc.__name__
           and not n.startswith("_")}
    assert pub <= set(cooc._NOT_TOOLS), sorted(pub - set(cooc._NOT_TOOLS))

    # preselect_stats：既有 5 个计数器函数保持原状（token 基线不动），
    # 但新增的配对口必须登记退出注册
    stats = importlib.import_module("tools.preselect_stats")
    assert {"record_task_calls", "pairs_path"} <= set(stats._NOT_TOOLS)
