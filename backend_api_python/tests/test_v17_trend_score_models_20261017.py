# -*- coding: utf-8 -*-
r"""V17：趋势评分**双模型身份标识**守卫（2026-10-17）。

★ 背景（tmp/llm_four_dimension_audit_20261017.md 审计实锤）
  「分析600519」实测：`technical_analysis` 综合 34 分看空（趋势维 17），
  `bull_bear_research` 多空 50 分中性（trend_score 52）——同一只股票、同一天
  数据，两个工具给出矛盾结论，最终报告把矛盾误诊为"多空因子对冲"。
  根因**不是**某一方算错，而是两套评分模型输出**同名键 `trend_score` 却无
  身份标识**，下游无法区分口径：
    · analysis_tools.analyze_trend   → 复合共振模型（多指标加权+共振过滤+
      背离+量能，trend_composite）
    · bull_bear_research._analyze_trend → 五维加权模型（ma/macd/rsi/boll/kdj
      × _BB_DEFAULT_WEIGHTS，校准命名空间 bull_bear_research，bb5_weighted）
  两模型并存是**设计使然**（各自服务不同消费方与校准闭环，2026-10-14
  D-1 键名对齐即有定论，见 test_v9_factor_weight_keys_20261014.py）。

★ 修复方式（最小改动、零降智）
  不做"统一"（那会删掉 bull_bear 的校准模型，违反禁止降智红线），而是给两个
  输出各加 `score_model` 自证身份 + docstring 交叉声明。本守卫钉死三件事：
    ① 两个模型的 `score_model` 键必须存在且取值不同（身份不丢）；
    ② bull_bear 不得被改成转调 analysis_tools（防"好心合并"删模型）；
    ③ 数值行为用合成数据对拍（不联网），确认两边都能正常出分。

★ 测试纪律
  合成 OHLCV（同 test_v7 惯例）：网络抖会让测试假红，而我们要守的是
  "算法与身份是否在位"，与数据真假无关。
"""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_BB_PATH = _BACKEND / "app" / "agent" / "tools" / "finance" / "bull_bear_research.py"

# 两个模型的身份证（与源码中的字面量一致；改名必须同步本文件，属 consciously breaking）
_TAG_COMPOSITE = "trend_composite(共振+背离+量能+波动)"
_TAG_BB5 = "bb5_weighted(ma/macd/rsi/boll/kdj)"


def _ohlcv(n: int = 130):
    """合成 OHLCV：趋势 + 双周期波动 + 微噪（复刻 test_v7 的生成器口径）。"""
    highs, lows, closes, volumes = [], [], [], []
    v = 10.0
    for i in range(n):
        v += math.sin(i / 5.0) * 0.35 + math.cos(i / 13.0) * 0.2
        v *= 1.001
        c = round(v, 4)
        closes.append(c)
        highs.append(round(c + 0.3 + 0.1 * math.sin(i / 3.0), 4))
        lows.append(round(max(0.5, c - 0.3 - 0.1 * math.cos(i / 4.0)), 4))
        volumes.append(int(1_000_000 + 200_000 * math.sin(i / 7.0)))
    return {"high": highs, "low": lows, "close": closes, "volume": volumes}


@pytest.fixture()
def _patched_ohlcv(monkeypatch):
    """把两个模块的 _fetch_ohlcv 都换成合成数据（隔离网络）。"""
    from app.agent.tools.finance import analysis_tools, bull_bear_research
    data = _ohlcv()
    monkeypatch.setattr(analysis_tools, "_fetch_ohlcv", lambda code, days: data)
    monkeypatch.setattr(bull_bear_research, "_fetch_ohlcv", lambda code, days: data)
    return analysis_tools, bull_bear_research


def test_both_models_self_identify_and_differ(_patched_ohlcv):
    """① 两个模型都必须带 score_model 且取值不同（身份标识不丢）。"""
    analysis_tools, bull_bear_research = _patched_ohlcv
    a = analysis_tools.analyze_trend("600519")
    b = bull_bear_research._analyze_trend("600519")

    assert "error" not in a, f"canonical 模型异常: {a}"
    assert "error" not in b, f"bb 模型异常: {b}"

    assert a.get("score_model") == _TAG_COMPOSITE, (
        f"canonical 模型身份证被改/丢了: {a.get('score_model')!r}")
    assert b.get("score_model") == _TAG_BB5, (
        f"bb 模型身份证被改/丢了: {b.get('score_model')!r}")
    assert a["score_model"] != b["score_model"], "两个模型身份不允许相同"


def test_models_both_produce_scores(_patched_ohlcv):
    """③ 合成数据下两边都能正常出分（0-100，含五维/复合各自的关键键）。"""
    analysis_tools, bull_bear_research = _patched_ohlcv
    a = analysis_tools.analyze_trend("600519")
    b = bull_bear_research._analyze_trend("600519")

    for name, r in (("canonical", a), ("bb5", b)):
        s = r.get("trend_score")
        assert isinstance(s, (int, float)) and 0 <= s <= 100, f"{name} 分数异常: {s}"
        assert r.get("trend"), f"{name} 缺趋势判定"
    # canonical 的复合共振证据与 bb 的五维键各自在位（不互相要求）
    assert "resonance" in a, "canonical 模型丢了共振结构"
    assert "ma_score" in b and "kdj_score" in b, "bb 模型丢了五维分项"


def test_bb_not_delegated_to_canonical():
    """② bull_bear_research 不得被改成转调 analysis_tools.analyze_trend
    （"好心统一"会静默删掉五维校准模型 = 降智，必须 consciously 决策）。"""
    tree = ast.parse(_BB_PATH.read_text(encoding="utf-8"))
    calls = [
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    attrs = [
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "analyze_trend" not in calls, (
        "bull_bear_research 出现了对裸名 analyze_trend 的调用——若这是"
        "「统一到 canonical」的改造，请先读 test_v9_factor_weight_keys_20261014.py "
        "与禁止降智红线：五维加权 + bull_bear_research 校准命名空间必须保留")
    assert "analyze_trend" not in attrs, (
        "bull_bear_research 出现了对 analysis_tools.analyze_trend 的属性调用"
        "（转调化），同样受上一条约束")


def test_bb_five_dim_weights_intact():
    """②-B _BB_DEFAULT_WEIGHTS 五维键仍在（校准闭环的锚，v9 守卫的补充断言）。"""
    tree = ast.parse(_BB_PATH.read_text(encoding="utf-8"))
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_BB_DEFAULT_WEIGHTS":
                    found = {k.value for k in node.value.keys
                             if isinstance(k, ast.Constant)}
    assert found == {"ma", "macd", "rsi", "boll", "kdj"}, (
        f"_BB_DEFAULT_WEIGHTS 键集合被改: {found}")
