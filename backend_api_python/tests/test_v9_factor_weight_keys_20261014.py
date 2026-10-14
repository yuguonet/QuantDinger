# -*- coding: utf-8 -*-
r"""A-05 / 设计 D-1：因子权重的**键名对齐**守卫（2026-10-14）。

实测结论先行（`tmp/probe_factor_weights_20261014.py`，查库不是猜）：
  · `qd_agent_weights WHERE layer='factor'` → **0 行** ⇒ 两个消费方当前都走回退常量。
    这层会随样本自愈，**getter 实现本身是对的**（不是"恒空的实现问题"）。
  · `technical_analysis` **键自洽**：它生产的 factors 名 = 趋势/指标/量价/形态/筹码/
    流通盘，与自己的 `_DEFAULT_WEIGHTS` 键**同名** ⇒ 攒够样本自动命中，无需改键。
  · `bull_bear_research` **键不自洽**：生产的是"多头:xxx"/"空头:xxx"（**论据**），
    加权用的却是**五维评分** ma/macd/rsi/boll/kdj —— 两套不同的东西
    （趋势≠ma；量价、情报在五维里无对应项）⇒ 不是改命名就能对齐，待设计裁定。

本文件的职责：
  ① 用 AST 钉死 `technical_analysis` 的键自洽（一旦有人改了 factors 名或权重键
     而忘了另一边，这里立刻报红 —— 键名漂移正是 D-1 的成因）。
  ② 把 `bull_bear_research` 的缺口做成 **xfail(strict=True) 台账**：
     今天它"确实不匹配"（xfail 通过）；哪天被修好了会变成 XPASS，strict 会**报错**
     ⇒ 强制回来同步设计文档 D-1 与本文件注释，避免"修了但设计还写着未做"。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_TA = _BACKEND / "app" / "agent" / "tools" / "finance" / "technical_analysis.py"
_BB = _BACKEND / "app" / "agent" / "tools" / "finance" / "bull_bear_research.py"
_STORE = _BACKEND / "app" / "agent" / "chain" / "store.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _dict_keys(node: ast.Dict) -> set:
    return {k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _weight_keys(tree: ast.Module, var_name: str) -> set:
    """取 `_DEFAULT_WEIGHTS = {...}` / `_BB_DEFAULT_WEIGHTS = {...}` 的字面量键。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == var_name and isinstance(node.value, ast.Dict):
                    return _dict_keys(node.value)
    raise AssertionError(f"未找到 {var_name} 的字典字面量（源码结构变了？需同步本守卫）")


def _factor_names(tree: ast.Module) -> set:
    """取所有 `factors.append({"name": <字面量>, ...})` 里的 name。"""
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "factors" or not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.Dict):
            continue
        for k, v in zip(arg.keys, arg.values):
            if (isinstance(k, ast.Constant) and k.value == "name"
                    and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                names.add(v.value)
    return names


# ── ① technical_analysis：键必须自洽（D-1 里它是"只等样本"的那一半）──

def test_technical_analysis_factor_names_match_its_weight_keys():
    tree = _parse(_TA)
    produced = _factor_names(tree)
    weighted = _weight_keys(tree, "_DEFAULT_WEIGHTS")
    assert produced, "没抽到 factors 的 name（源码结构变了？需同步本守卫）"
    assert weighted, "没抽到 _DEFAULT_WEIGHTS"
    # ① 每个加权键都必须有同名因子在产出 ⇒ 否则攒够样本也命中不了
    missing = weighted - produced
    assert not missing, (
        f"technical_analysis 的权重键 {sorted(missing)} 没有同名因子产出 ⇒ "
        f"攒够样本也命中不了（D-1 键名漂移）。生产端={sorted(produced)}")
    # ② 反向也必须成立：产出的因子名**不许**落在权重键之外。
    #    ★ 为什么两个方向都要：只查 ① 时，**只改一处**的漂移抓不到 ——
    #      "趋势" 在两处 append（正常分支 + 数据缺失分支），改其中一处后
    #      另一处仍在产出 "趋势" ⇒ ① 依旧成立，但被改那一支的因子永远校准不到
    #      （变异 M1-ta-name 实测就是这么漏过去的）。
    extra = produced - weighted
    assert not extra, (
        f"产出了权重键里没有的因子名 {sorted(extra)} ⇒ 这些因子的校准结果永远读不到"
        f"（可能是只改了一处命名）。权重键={sorted(weighted)}")


def test_technical_analysis_weight_keys_are_not_silently_renamed():
    """反向对照：权重键集合本身要有稳定契约（防止有人只改一侧）。"""
    weighted = _weight_keys(_parse(_TA), "_DEFAULT_WEIGHTS")
    assert weighted == {"趋势", "指标", "量价", "形态", "筹码", "流通盘"}, (
        f"权重键集合变了：{sorted(weighted)} —— 若是有意调整，请同步 factors 产出端"
        f"与本用例的契约断言")


# ── ② bull_bear：已知缺口台账（修好后 strict 会报错，逼你回来改设计）──

@pytest.mark.xfail(
    strict=True,
    reason="D-1 已知缺口：bull_bear 产出的是论据名(多头:xxx)，加权用的是五维评分键"
           "(ma/macd/rsi/boll/kdj)，两套不同东西，非改命名可对齐。若本用例 XPASS，"
           "说明已对齐 ⇒ 必须回头改 AGENT_DESIGN_v3.md 的 D-1 与本文件头注释。",
)
def test_bull_bear_factor_names_should_match_its_dimension_keys():
    tree = _parse(_BB)
    produced = _factor_names(tree)          # 论据名是 f-string ⇒ 抽不到字面量
    weighted = _weight_keys(tree, "_BB_DEFAULT_WEIGHTS")
    assert weighted == {"ma", "macd", "rsi", "boll", "kdj"}
    assert produced & weighted, (
        "bull_bear 产出的因子名与五维评分键无交集 ⇒ 因子校准永不命中")


def test_bull_bear_dimension_keys_are_stable():
    """五维键本身是契约：改了就必须同步上面的 xfail 台账与设计文档。"""
    assert _weight_keys(_parse(_BB), "_BB_DEFAULT_WEIGHTS") == {
        "ma", "macd", "rsi", "boll", "kdj"}


# ── ③ store：getter 的层与样本门槛（防止被人顺手改歪）──

def test_get_factor_weights_queries_the_factor_layer():
    src = _STORE.read_text(encoding="utf-8")
    tree = _parse(_STORE)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "get_factor_weights")
    body = ast.get_source_segment(src, fn) or ""
    assert body, "没取到 get_factor_weights 源码（结构变了？需同步本守卫）"
    # ★ 必须**逐处**查，不能只查"出现过一次"：函数里有两条 SELECT（带 skill_name /
    #   不带），只改其中一条的 layer 时，"出现过 factor" 仍成立 ⇒ 另一条分支静默
    #   查错层（变异 M3-store-layer 就是这么漏过去的）。
    layers = set(re.findall(r"layer\s*=\s*'([a-z_]+)'", body))
    assert layers == {"factor"}, f"get_factor_weights 查的层是 {sorted(layers)}（应只有 factor）"
    assert "sample_count" in body, "丢了最小样本门槛 ⇒ 一两条样本就当校准值用（会误判）"
