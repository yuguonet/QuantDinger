# -*- coding: utf-8 -*-
r"""CLI 入口必须默认关闭 mimoagent 的 LLM-INPUT 调试打印（2026-10-14 实测缺陷）。

背景（实测 `python -m app.agent.cli "问题"`）：
  mimoagent 的 `models/openai_chat.py::_debug_dump_messages` 默认
  `QD_LLM_DEBUG="1"`，开启时**裸 print()** 一行
  `[LLM-INPUT::<model>] TYPE=... n_msgs=... -> llm_inputs.log` 摘要直冲 stdout，
  并把完整 LLM 输入落盘 CWD 下的 `llm_inputs.log`。

  这条 print 走 stdout 而非 logging handler —— 上轮修的「logger handler 一律朝
  stderr」收口**管不到它**，CLI 的「stdout = 结果通道」（`... | jq` 之类要能直接
  消费）契约被再次打破。

修复：cli.py 入口 `os.environ.setdefault("QD_LLM_DEBUG", "0")`。
  （setdefault 不覆盖 .env / shell 已设的值 —— 开发者仍可用 `QD_LLM_DEBUG=1` 开启）

★ 变异自检（防恒绿）：删掉 cli.py 里那行 setdefault，本测试的「默认关」断言
  必须转红；否则守卫是假的。
"""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def _reload_cli_without_env():
    """在「QD_LLM_DEBUG 未设置」的受控 env 下重新加载 cli 模块顶层（不触重 import）。"""
    saved = os.environ.pop("QD_LLM_DEBUG", None)
    try:
        import app.agent.cli as cli
        importlib.reload(cli)  # 只重跑顶层：setdefault 应把缺失的键补成 "0"
        yield cli
    finally:
        # 还原，避免污染其它用例 / 会话
        if saved is None:
            os.environ.pop("QD_LLM_DEBUG", None)
        else:
            os.environ["QD_LLM_DEBUG"] = saved


def test_cli_defaults_qd_llm_debug_off(_reload_cli_without_env):
    """未显式设置时，CLI 入口必须把调试打印默认关掉 ⇒ 不污染 stdout。"""
    assert os.environ.get("QD_LLM_DEBUG") == "0", (
        "CLI 未默认关闭 QD_LLM_DEBUG ⇒ mimoagent 的裸 print 会污染 stdout"
    )


def test_cli_respects_explicit_qd_llm_debug_on():
    """显式开启（.env / shell）时不被覆盖——开发者仍需落盘调试能力。"""
    os.environ["QD_LLM_DEBUG"] = "1"
    try:
        import app.agent.cli as cli
        importlib.reload(cli)
        assert os.environ["QD_LLM_DEBUG"] == "1"
    finally:
        os.environ.pop("QD_LLM_DEBUG", None)
