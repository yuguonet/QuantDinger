# -*- coding: utf-8 -*-
r"""stdout 卫生守卫（2026-10-14，cli 实测第①项缺陷）。

背景（实测）：`python -m app.agent.cli "问题"` 的 stdout 前 30 行全是
mimoagent / httpx / 本项目的 INFO 日志 ⇒ stdout 是**结果通道**
（`... | jq` 之类要能直接消费），被日志污染后结果不可解析。

契约：**控制台 handler 一律朝 stderr**。

为什么必须扫「全部 logger」而不只扫 root（两条，都踩过）：
1. `basicConfig` 只在 root 尚无 handler 时生效 ⇒ 第三方若**先**配到 stdout，
   是否被污染就变成「取决于 import 顺序」的玄学；
2. mimoagent 用的是 **具名 logger + `propagate=False`**
   （`.venv/.../mimoagent/utils/log.py::_setup_root_logger`），完全绕开 root
   ⇒ 只扫 root **永远看不到它**。
   且它挂的是 `rich.logging.RichHandler` —— **不是** `logging.StreamHandler`
   子类，输出流在 `.console.file` 上 ⇒ 只判 StreamHandler 同样漏掉它。

★ 反向对照不可省：只测「能被收口」的话，一个恒真的假实现也能全绿。
"""
from __future__ import annotations

import logging
import sys

import pytest

from app.agent.log import (
    _console_writes_to_stdout,
    ensure_console_on_stderr,
)


# ── 测试替身：模拟 rich 系 handler（RichHandler 不是 StreamHandler 子类）──

class _FakeConsole:
    """rich Console 的最小替身：只有可写的 `file` 属性。"""

    def __init__(self, file):
        self.file = file


class _RichLikeHandler(logging.Handler):
    """RichHandler 形态：没有 `.stream`，输出流藏在 `.console.file`。"""

    def __init__(self, file=None):
        super().__init__()
        self.console = _FakeConsole(file if file is not None else sys.stdout)

    def emit(self, record):  # pragma: no cover - 守卫只关心流归属，不真写
        pass


@pytest.fixture
def _isolated_logger():
    """造一个干净的具名 logger，用完还原（不污染全局日志配置）。"""
    name = "test.stdout_hygiene.isolated"
    lg = logging.getLogger(name)
    saved_handlers = list(lg.handlers)
    saved_propagate = lg.propagate
    saved_level = lg.level
    lg.handlers.clear()
    lg.propagate = False          # ★ 复刻 mimoagent 的形态：绕开 root
    lg.setLevel(logging.DEBUG)
    yield lg
    lg.handlers.clear()
    lg.handlers.extend(saved_handlers)
    lg.propagate = saved_propagate
    lg.setLevel(saved_level)


# ── 探测器自检（阳性样本必须检出，否则守卫是假的）──

def test_detector_catches_stream_handler_on_stdout():
    h = logging.StreamHandler(sys.stdout)
    assert _console_writes_to_stdout(h) is True


def test_detector_catches_rich_like_handler_on_stdout():
    assert _console_writes_to_stdout(_RichLikeHandler()) is True


def test_detector_ignores_stream_handler_already_on_stderr():
    assert _console_writes_to_stdout(logging.StreamHandler(sys.stderr)) is False


def test_detector_ignores_file_handler():
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        path = f.name
    h = logging.FileHandler(path)
    try:
        # FileHandler 是 StreamHandler 子类，但它的 stream 是文件 ⇒ 不该被改指
        assert _console_writes_to_stdout(h) is False
    finally:
        h.close()


# ── 收口行为 ──

def test_stream_handler_on_stdout_is_redirected(_isolated_logger):
    lg = _isolated_logger
    h = logging.StreamHandler(sys.stdout)
    lg.addHandler(h)
    assert ensure_console_on_stderr() >= 1
    assert h.stream is sys.stderr


def test_rich_like_handler_on_stdout_is_redirected(_isolated_logger):
    """★ mimoagent 的真实形态：RichHandler + propagate=False。"""
    lg = _isolated_logger
    h = _RichLikeHandler()
    lg.addHandler(h)
    ensure_console_on_stderr()
    assert h.console.file is sys.stderr


def test_named_logger_with_propagate_false_is_covered(_isolated_logger):
    """`propagate=False` 的具名 logger 也必须被扫到（只扫 root 会漏）。"""
    lg = _isolated_logger
    assert lg.propagate is False
    h = logging.StreamHandler(sys.stdout)
    lg.addHandler(h)
    ensure_console_on_stderr()
    assert h.stream is sys.stderr


def test_handler_already_on_stderr_is_untouched(_isolated_logger):
    lg = _isolated_logger
    h = logging.StreamHandler(sys.stderr)
    lg.addHandler(h)
    ensure_console_on_stderr()
    assert h.stream is sys.stderr


def test_ensure_is_idempotent(_isolated_logger):
    lg = _isolated_logger
    lg.addHandler(logging.StreamHandler(sys.stdout))
    assert ensure_console_on_stderr() >= 1
    assert ensure_console_on_stderr() == 0   # 第二次没有可改指的


def test_ensure_leaves_no_stdout_console_handler_globally():
    """全局不变式：收口后**任何** logger 都不再有朝 stdout 的控制台 handler。"""
    ensure_console_on_stderr()
    left = [
        (name, type(h).__name__)
        for name, lg in list(logging.Logger.manager.loggerDict.items())
        if isinstance(lg, logging.Logger)
        for h in lg.handlers
        if _console_writes_to_stdout(h)
    ]
    root_left = [type(h).__name__ for h in logging.getLogger().handlers
                 if _console_writes_to_stdout(h)]
    assert not left and not root_left, f"仍有朝 stdout 的 handler: {left} / {root_left}"


# ── CLI 侧的接线（真 import mimoagent，确认它那条支路被收口）──

def test_cli_pin_mimo_log_covers_the_real_mimoagent_logger():
    from app.agent.cli import _pin_mimo_log_to_stderr

    _pin_mimo_log_to_stderr()
    lg = logging.getLogger("mimoagent")
    assert lg.handlers, "mimoagent 未配置 handler —— 本用例前提不成立，需重查版本"
    assert all(not _console_writes_to_stdout(h) for h in lg.handlers), (
        "mimoagent 的 handler 仍在朝 stdout 写 ⇒ CLI 的 stdout 会被日志污染"
    )
