# -*- coding: utf-8 -*-
"""auto overlay 读钩子单测（§5.7 桥，2026-10-15）。

红线：env 未设 ⇒ 行为与今日逐字节相同（回归用例钉住）。
注意：_YAML_DOC_CACHE 是模块级缓存，fixture 每用例前后清空，防跨用例串缓存。
"""
import sys
import textwrap

import pytest

from app.market_cn.auto import strategies as S

_OVERLAY_ENV = "QD_STRATEGY_YAML_OVERLAY_DIR"


@pytest.fixture(autouse=True)
def _clean_yaml_cache():
    S._YAML_DOC_CACHE.clear()
    yield
    S._YAML_DOC_CACHE.clear()


@pytest.fixture(autouse=True)
def _registry_cleanup():
    """overlay 测试会往模块级 _REGISTRY 塞探测策略，用例后清掉防泄漏。

    C1 教训：只清 `agentbt_` 命名空间（AST 预检保证 overlay key 必带此前缀）。
    autodiscover 顺带注册的**包内策略**绝不能 pop —— 其模块已留在 sys.modules，
    pop 后后续 import 命中缓存不再执行 @register ⇒ 注册表在本 session 永久空。
    同时回收用例期间新增的 `qd_overlay_` 模块，防跨用例命中
    `mod_name in sys.modules` 跳过重载（同 stem 换 tmp_path 的场景）。
    """
    pre_overlay_keys = {k for k in S._REGISTRY if k.startswith("agentbt_")}
    pre_overlay_mods = {m for m in sys.modules if m.startswith("qd_overlay_")}
    yield
    for k in set(S._REGISTRY):
        if k.startswith("agentbt_") and k not in pre_overlay_keys:
            S._REGISTRY.pop(k, None)
    for m in set(sys.modules):
        if m.startswith("qd_overlay_") and m not in pre_overlay_mods:
            sys.modules.pop(m, None)


def test_yaml_doc_overlay_wins(tmp_path, monkeypatch):
    (tmp_path / "agentbt_t1.yaml").write_text(
        "meta: {enabled: false}\nparams: {marker: 42}\n", encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    doc, mt = S._yaml_doc("agentbt_t1")
    assert doc["params"]["marker"] == 42
    assert mt is not None


def test_yaml_doc_env_unset_never_reads_overlay(tmp_path, monkeypatch):
    # overlay 里**故意放了同 key yaml**（M3 释疑）：证明 env 未设时即便文件存在
    # 也绝不读 overlay —— 而非"目录为空没得读"的平凡情形；断言只看活树。
    (tmp_path / "agentbt_t1.yaml").write_text("params: {marker: 42}\n", encoding="utf-8")
    monkeypatch.delenv(_OVERLAY_ENV, raising=False)
    assert S._yaml_doc("agentbt_t1") == (None, None)   # 只看活树（无此策略）


_MIN_STRATEGY = textwrap.dedent("""
    from app.market_cn.auto.strategies import register
    from app.market_cn.auto.strategies.base import StrategyBase

    @register
    class OverlayProbe(StrategyBase):
        key = "agentbt_t2_probe"
""")

# I1 钉子用模块：AST 可解析、key 合法（预检通过），但**先 raise 后 register** ——
# 模拟 exec 期失败。raise 必须在 @register 之前，否则 key 已注册、断言失真。
_BOOM_STRATEGY = textwrap.dedent("""
    raise RuntimeError("boom: overlay exec 期失败钉子")

    from app.market_cn.auto.strategies import register
    from app.market_cn.auto.strategies.base import StrategyBase

    @register
    class OverlayProbe(StrategyBase):
        key = "agentbt_t2_boom"
""")


def test_autodiscover_overlay_registers_new_strategy(tmp_path, monkeypatch):
    (tmp_path / "agentbt_t2_probe.py").write_text(_MIN_STRATEGY, encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    S.autodiscover()
    assert S.get_strategy("agentbt_t2_probe") is not None


def test_autodiscover_overlay_rejects_non_agentbt_key(tmp_path, monkeypatch):
    """AST 静态预检：key 不带 agentbt_ 前缀的模块拒绝注册（防撞生产 key）。"""
    (tmp_path / "evil.py").write_text(
        _MIN_STRATEGY.replace("agentbt_t2_probe", "evil_key"), encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    S.autodiscover()
    assert S.get_strategy("evil_key") is None


def test_autodiscover_overlay_per_module_fault_isolation(tmp_path, monkeypatch):
    """单模块损坏只跳过自身（与包内 autodiscover 同款 L3 隔离）。"""
    # 坏文件名不带 _ 前缀：带 _ 会被前缀过滤提前跳过，走不到 per-module 容错分支
    (tmp_path / "broken.py").write_text("this is {{{ not python", encoding="utf-8")
    (tmp_path / "agentbt_t2_ok.py").write_text(
        _MIN_STRATEGY.replace("agentbt_t2_probe", "agentbt_t2_ok"), encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    S.autodiscover()
    assert S.get_strategy("agentbt_t2_ok") is not None


def test_autodiscover_overlay_exec_failure_no_half_state(tmp_path, monkeypatch):
    """I1 钉子：模块可解析、AST 预检通过（key 合法），但模块级 raise ——
    ① 该 key 不注册；② sys.modules 不留半初始化 `qd_overlay_` 缓存（标准 importlib
    语义：加载失败不留缓存，同进程再次 autodiscover 可重试）；③ 同目录正常策略不受影响。"""
    (tmp_path / "agentbt_t2_boom.py").write_text(_BOOM_STRATEGY, encoding="utf-8")
    (tmp_path / "agentbt_t2_after.py").write_text(
        _MIN_STRATEGY.replace("agentbt_t2_probe", "agentbt_t2_after"), encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    S.autodiscover()
    assert S.get_strategy("agentbt_t2_boom") is None
    assert "qd_overlay_agentbt_t2_boom" not in sys.modules
    assert S.get_strategy("agentbt_t2_after") is not None


def test_load_strategy_overlay_params(tmp_path, monkeypatch):
    key = "agentbt_t3"
    (tmp_path / f"{key}.yaml").write_text(textwrap.dedent("""
        meta: {market: A}
        params: {marker: 7}
        entry: {}
        signal: {}
        exit: {}
        gates: []
    """), encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    from app.market_cn.auto.core.runtime import evaluate
    spec = evaluate.load_strategy(key)
    assert spec.params.get("marker") == 7


def test_yaml_doc_cache_keyed_by_path(tmp_path, monkeypatch):
    """同 key 先 overlay 后回退，缓存不得串槽（改进 D 的配套语义）。"""
    (tmp_path / "agentbt_t1.yaml").write_text("params: {marker: 1}\n", encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    doc1, _ = S._yaml_doc("agentbt_t1")
    monkeypatch.delenv(_OVERLAY_ENV, raising=False)
    doc2, _ = S._yaml_doc("agentbt_t1")
    assert doc1["params"]["marker"] == 1
    assert doc2 is None
