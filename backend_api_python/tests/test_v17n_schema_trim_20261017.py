# -*- coding: utf-8 -*-
"""F11/F14 守卫：工具面按需化 + O-14 分级触发改判 schema 总字符数（2026-10-17）。

背景（合并总账 `:179`/`:182`，源报告 `tmp/tool_layer_analysis_20261017.md` `:41-45`）：

  · F11（E1/E2/R2）——实测工具面 full 101 个 / 75,299 字符；**core 15 个 / 11,281
    字符每步重发**，其中 `bash` 单工具 **4,280 字符 = 核心面 38%**，而现场 trace
    全程未调用（E2）。按「提智不降智、总 token 越小越好」把它从必注入层降级到
    按需层：走 `QD_ON_DEMAND_TOOLS` 旋钮（与 edit/read/write/glob/grep 同一机制，
    可拔插），**不**做能力层 admission（D-9：admission doc 全空、无消费方）。

  · F14（E3）——O-14「分级目录」原触发条件写作「工具面破 120 个」，但真实成本
    变量是 **schema 总字符数**（实测工具 92→101 仅 +10%，schema 49.5k→75.3k
    +52%；「工具没涨、描述涨胖」是盲区）。本文件把触发条件钉成 schema 总字符数
    （env `QD_PRESELECT_TIER_CHARS`，默认 60000），并锁住「低于阈值退回全量目录」
    这一可逆开关。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_CONFIG = BACKEND / "app" / "agent" / "qd_config.py"
QD_PRESELECT = BACKEND / "app" / "agent" / "qd_preselect.py"
TOOL_PRESELECT = BACKEND / "app" / "agent" / "preselect" / "tool_preselect.py"


# ── 共用 stand（与 v17h 同款：假注册表 + ToolFaceMixin stand）────────────

def _def(name: str) -> dict:
    return {"function": {"name": name, "description": f"[test] {name}",
                         "parameters": {"properties": {}, "type": "object"}}}


class _Reg:
    def __init__(self, tools: dict):
        self._t = tools

    def list_tools(self):
        return list(self._t)

    def get(self, n):
        return self._t[n]

    def get_function_definitions(self):
        return [_def(n) for n in self._t]


def _fn_tools(*specs):
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位。"""
        return ""

    out = {}
    for name, dom in specs:
        out[name] = object() if dom is None else FnToolAdapter(_f, domain=dom)
    return out


def _face_stand(tools: dict, *, on_demand) -> object:
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(ToolFaceMixin):
        pass

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(
        on_demand_tools=list(on_demand), extra_core_tools=(),
        tool_tier_mode="tiered", max_active_domain_tools=20)
    s._activated = []
    return s


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("QD_TOOL_CONNECT", "QD_ON_DEMAND_TOOLS", "QD_CAPABILITIES",
                "QD_PRESELECT_TIER_CHARS"):
        monkeypatch.delenv(var, raising=False)
    from app.agent import qd_config
    monkeypatch.setattr(qd_config, "_TOOL_CONNECT_INVALID", set(), raising=False)
    yield


# ── F11：bash 默认降级为按需（E2 → core 面 −38%）────────────────────────

def test_default_on_demand_includes_bash():
    """默认 `QD_ON_DEMAND_TOOLS` 名册必须含 bash（否则降级不生效）。"""
    from app.agent.qd_config import QDAgentConfig

    cfg = QDAgentConfig()
    assert "bash" in cfg.on_demand_tools, cfg.on_demand_tools


def test_tool_connect_bash_is_deferred_by_default():
    """分层口径：默认下 bash 的生效态必须是 deferred（不是 resident）。"""
    from app.agent import qd_config

    assert qd_config.tool_connect("bash") == "deferred", \
        "bash 必须默认降级——否则每步白挂 4,280 字符 schema"


def test_bash_not_in_core_face_under_defaults():
    """面视角回归：用**默认** on_demand 建面，bash 不在 core、在 on_demand。"""
    from app.agent.qd_config import QDAgentConfig

    defaults = QDAgentConfig().on_demand_tools
    s = _face_stand(_fn_tools(("bash", None), ("common_tool", "common"),
                              ("meta_tool", "meta"), ("fin_tool", "finance")),
                    on_demand=defaults)
    s._sync_tool_tiers()
    assert "bash" not in s._core_names, s._core_names
    assert "bash" in s._on_demand_names, s._on_demand_names
    assert "bash" in s._defs_by_name, "降级只改分层，defs 必须仍在（可回退执行）"


def test_bash_still_activatable_by_name():
    """降级不等于删能力：点名仍能激活（可拔插）。"""
    from app.agent.qd_config import QDAgentConfig

    s = _face_stand(_fn_tools(("bash", None), ("common_tool", "common")),
                    on_demand=QDAgentConfig().on_demand_tools)
    s._sync_tool_tiers()
    act = s.activate_tools(["bash"])
    assert act["activated"] == ["bash"], act
    names = [d["function"]["name"] for d in s._active_tool_definitions()]
    assert "bash" in names, names


def test_reverse_knob_restores_bash_to_core():
    """回退开关：把 QD_ON_DEMAND_TOOLS 显式设为不含 bash 的名单 ⇒ bash 回 core。

    非 FnToolAdapter（mimo 原生）在 resident 分支恒必注入 ⇒ 从名册移除即恢复。
    """
    from app.agent.qd_config import QDAgentConfig

    env_backed = QDAgentConfig().on_demand_tools
    assert "bash" in env_backed
    import os
    os.environ["QD_ON_DEMAND_TOOLS"] = "actor,run_skill"
    try:
        cfg2 = QDAgentConfig()
        assert "bash" not in cfg2.on_demand_tools
        s = _face_stand(_fn_tools(("bash", None), ("fin_tool", "finance")),
                        on_demand=cfg2.on_demand_tools)
        s._sync_tool_tiers()
        assert "bash" in s._core_names, s._core_names
    finally:
        os.environ.pop("QD_ON_DEMAND_TOOLS", None)


# ── F14：O-14 触发条件 = schema 总字符数（E3）───────────────────────────

def test_schema_chars_of_counts_serialized_defs():
    """成本度量口径：defs 全量 JSON 字符数（与探针 75,299 同口径）。"""
    from app.agent.preselect.tool_preselect import schema_chars_of

    defs = {"a": _def("a"), "b": _def("b")}
    assert schema_chars_of(defs) == len(json.dumps(defs, ensure_ascii=False))
    assert schema_chars_of(defs) == schema_chars_of(defs)


def test_tier_trigger_compares_against_char_threshold(monkeypatch):
    """触发判据：schema 字符数 > 阈值 ⇒ 启用分级；≤ ⇒ 退回全量目录。"""
    from app.agent.preselect.tool_preselect import tier_catalog_enabled

    defs = {"a": _def("a")}                      # 小面
    small = schema_chars_of_import(defs)
    monkeypatch.setenv("QD_PRESELECT_TIER_CHARS", str(small + 1))
    assert tier_catalog_enabled(defs) is False, "低于阈值 ⇒ 不分级（全量目录够便宜）"
    monkeypatch.setenv("QD_PRESELECT_TIER_CHARS", str(max(1, small - 1)))
    assert tier_catalog_enabled(defs) is True, "高于阈值 ⇒ 启用分级目录"


def test_tier_trigger_zero_means_always_on(monkeypatch):
    """0/负 = 恒分级（等价改造前行为，回退开关）。"""
    from app.agent.preselect.tool_preselect import tier_catalog_enabled

    monkeypatch.setenv("QD_PRESELECT_TIER_CHARS", "0")
    assert tier_catalog_enabled({"a": _def("a")}) is True


def test_default_threshold_is_60k(monkeypatch):
    """默认阈值 60000 字符 ⇒ 当前工具面（实测 75,299）必然触发（与现状一致）。"""
    from app.agent.preselect.tool_preselect import _preselect_tier_chars

    monkeypatch.delenv("QD_PRESELECT_TIER_CHARS", raising=False)
    assert _preselect_tier_chars() == 60000


def test_shrink_is_gated_on_tier_trigger():
    """接线不变量：qd_preselect 必须经 tier 触发判据门控 shrink_catalog。"""
    src = QD_PRESELECT.read_text(encoding="utf-8")
    body = ast.unparse(_func_node(src, "_maybe_preselect_tools"))
    assert "tier_catalog_enabled" in body, \
        "分级目录必须由 schema 字符数触发（E3），不得无条件生效"


def schema_chars_of_import(defs):
    from app.agent.preselect.tool_preselect import schema_chars_of
    return schema_chars_of(defs)


def _func_node(src: str, name: str):
    tree = ast.parse(src)
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
        stack.extend(ast.iter_child_nodes(node))
    raise AssertionError(f"找不到函数 {name}")
