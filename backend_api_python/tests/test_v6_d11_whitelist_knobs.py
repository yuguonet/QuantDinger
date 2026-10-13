# -*- coding: utf-8 -*-
"""D-11 残余：元工具名单下沉 + 执行型白名单单一真源收敛（2026-10-14）。

【背景】设计文档 D-11 记「`run_skill` 白名单硬编码，违反"规则不在代码里"」，
2026-10-13 复核已订正：原指控的那份名单已不存在（真源 = `scripts/skill_run.py`
的 `EXECUTABLE_SKILLS`，由 `qd_service._executable_skills()` 动态读取）。
**残余两处**由 O-11 打包收口，本文件把它们钉住：

  ① `chain/skill_brewer._META_TOOLS`：写盘前剔除的"元工具"名单是**硬编码集合**。
     工程红线（阈值/口径走 env 或配置层）⇒ 下沉为 `QD_META_TOOLS`，默认沿用原 6 个。
  ② `qd_service._executable_skills()`：用 `spec_from_file_location` 按文件路径
     加载 skill_run，会造出**第二个模块实例**（`_qd_skill_run_wl`）—— 与 P5 根治的
     "双模块实例"是同一类隐患。P5 后 `app/agent` 已不进 sys.path、全包统一
     `app.agent.*`，包内相对导入即可，不再需要按路径加载这套机械。

【为什么 ① 不能"推导"出来而只能配置】
元工具 = "查工具的工具"（list_tools/search_tools）与沙箱内置（final_answer /
python_interpreter），它们不由 `ToolProvider` 注册 ⇒ `provider.get_meta_names()`
只覆盖 format_result/web_search 这类，**盖不全**。所以只能显式配置，不能假装推导。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from app.agent import qd_service as qs
from app.agent.chain import skill_brewer as sb

DEFAULT_META = {"list_tools", "search_tools", "format_result",
                "final_answer", "python_interpreter", "web_search"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("QD_META_TOOLS", raising=False)
    yield


# ── ① 元工具名单走 env ────────────────────────────────────────
def test_meta_tools_default_keeps_original_six():
    """默认必须等于原硬编码的那 6 个 —— 下沉不是改名，别悄悄换口径。"""
    assert sb._meta_tools() == DEFAULT_META, (
        f"默认元工具名单变了：{sorted(sb._meta_tools() ^ DEFAULT_META)}")


def test_meta_tools_is_read_from_env(monkeypatch):
    monkeypatch.setenv("QD_META_TOOLS", "aaa,bbb")
    assert sb._meta_tools() == {"aaa", "bbb"}


def test_meta_tools_env_drops_empty_tokens(monkeypatch):
    """脏 token（空串/纯空格）不能落进名单，否则会误伤同名空串工具。"""
    monkeypatch.setenv("QD_META_TOOLS", "aaa,, ,bbb,  ")
    assert sb._meta_tools() == {"aaa", "bbb"}


@pytest.mark.parametrize("dirty", ["", "   ", ",,", " , "])
def test_meta_tools_empty_env_falls_back_to_default(monkeypatch, dirty):
    """空 env = 未配置 ⇒ 回落默认（不是"空名单"，空名单会让元工具写进白名单）。"""
    monkeypatch.setenv("QD_META_TOOLS", dirty)
    assert sb._meta_tools() == DEFAULT_META


class _FakeProvider:
    def __init__(self, names):
        self._names = list(names)

    def get_tool_names(self):
        return self._names


_SKMD = """---
name: demo
description: d
tools: [get_stock_quote, list_tools, bogus_tool]
---
正文
"""


def test_validate_tools_strips_meta_and_fabricated():
    """行为守卫：元工具 + 编造名都被剔除，真实工具留下。"""
    out = sb._validate_tools(_SKMD, _FakeProvider(
        ["get_stock_quote", "list_tools", "search_tools"]))
    assert "tools: [get_stock_quote]" in out, f"应只留真实工具：{out!r}"
    assert "list_tools" not in out, "元工具必须剔除（写进白名单只是噪音）"
    assert "bogus_tool" not in out, "编造名必须剔除（执行期必踩坑）"


def test_validate_tools_honors_env_override(monkeypatch):
    """env 改了名单，剔除行为必须跟着变（否则 env 是摆设）。"""
    monkeypatch.setenv("QD_META_TOOLS", "get_stock_quote")
    out = sb._validate_tools(_SKMD, _FakeProvider(["get_stock_quote", "list_tools"]))
    assert "get_stock_quote" not in out, "被 env 指定为元工具后必须剔除"
    assert "list_tools" in out, "不再是元工具且已注册 ⇒ 保留"


def test_validate_tools_provider_none_is_noop():
    """provider 缺失时原样返回（原有 fail-open 行为不变）。"""
    assert sb._validate_tools(_SKMD, None) == _SKMD


# ── ② 白名单单一真源：包内相对导入，不造第二个模块实例 ──────────
@pytest.fixture()
def _reset_cache():
    qs._EXECUTABLE_SKILLS_CACHE = None
    # 同时清掉包内模块：否则别的用例先 import 过，本用例就变成"恒真"的空守卫
    sys.modules.pop("app.agent.scripts.skill_run", None)
    yield
    qs._EXECUTABLE_SKILLS_CACHE = None
    sys.modules.pop("app.agent.scripts.skill_run", None)


def test_executable_skills_matches_single_source(_reset_cache):
    from app.agent.scripts.skill_run import EXECUTABLE_SKILLS as WL
    got = qs._executable_skills()
    assert got == dict(WL), "取到的白名单必须等于真源（不是抄一份）"
    assert "market_screener" in got and "strategy_debug" in got


def test_executable_skills_creates_no_duplicate_module(_reset_cache):
    """按文件路径加载会在 sys.modules 里塞一个影子模块 ⇒ 双实例隐患。"""
    qs._executable_skills()
    # ⚠ 必须按**末段模块名**精确匹配，不能用 `"skill_run" in k` 子串：
    #   全量跑时 sys.modules 里有 `tests.test_skill_run_exits` 这个测试模块，
    #   子串匹配会把它当成影子模块 ⇒ 假红（本用例单独跑是绿的，全量才红）。
    shadows = [k for k in sys.modules
               if (k == "skill_run" or k.endswith(".skill_run")) and not k.startswith("app.")]
    assert not shadows, f"出现影子模块实例（应按包路径导入）: {shadows}"
    assert "app.agent.scripts.skill_run" in sys.modules, "应走包内相对导入"


def test_no_spec_from_file_location_in_qd_service():
    """源码守卫：不许回退到按文件路径加载。

    ⚠ 必须是 **AST 找调用**，不能用 `in src` 全文匹配：本节 docstring 里
    恰好写了 "spec_from_file_location" 这几个字（解释为什么废弃它）⇒
    全文匹配会恒红。教训同 P3 的 c3p 守卫（全文匹配会被注释骗成假绿/假红）。
    """
    tree = ast.parse(Path(qs.__file__).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "spec_from_file_location":
            bad.append(node.lineno)
        elif isinstance(fn, ast.Name) and fn.id == "spec_from_file_location":
            bad.append(node.lineno)
    assert not bad, (
        f"qd_service.py 第 {bad} 行又在按文件路径加载模块（spec_from_file_location）—— "
        "P5 后包内相对导入即可，按路径加载会造第二个模块实例（D-11 回归）")
