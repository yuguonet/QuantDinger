# -*- coding: utf-8 -*-
r"""mimo_boot 必须把 mimoagent 的 stdout 日志 handler 收口到 stderr（2026-10-15）。

背景（实测 `python -m app.agent.scripts.exp_grind --batch 2`，stdout 捕获）：
  mimoagent 的 `utils/log.py` 在 **import 时**执行 `_setup_root_logger()`：
  给 `"mimoagent"` logger 挂一个 **RichHandler**，且 `propagate=False`
  （记录不到 root）。rich 的 `RichHandler.__init__` 是
  `self.console = console or get_console()` —— 用**全局单例 console**，
  未指定流时回落 `sys.stdout`。于是 `mimoagent.environments.local` /
  `mimoagent.tools.ripgrep` 等子 logger 的 INFO（"ripgrep 已就绪并前置
  PATH…" 三条）全打到 **stdout**。

  `app/agent/log.py` 的 `ensure_console_on_stderr()` 本就是干这个的，但它只在
  `app.agent.log` **被 import 时**扫一次已存在的 handler；mimoagent 的 handler
  是执行核装配时才挂上的（`import mimoagent` → `utils/log` 模块级副作用），
  **永远扫不到** ⇒ 污染取决于 import 顺序的玄学。

修复：`mimo_boot.py`（所有入口解析执行核的共享引导）在 `ensure_mimoagent()`
之后补一次 `ensure_console_on_stderr()`：第三方朝 stdout 的输出，在共享引导层
统一收口，覆盖每一个入口。

★ 本测试的现场构造（踩过的坑，2026-10-15）：
  RichHandler 用全局 console ⇒ 「reload mlog 造一个新 handler」**复现不了**
  污染现场（新 handler 仍指向已被收口的全局 console）。正确做法是直接给
  handler 换一个朝 stdout 的新 `Console(file=sys.stdout)` —— 等价于一个
  全新进程的初始状态，且不污染全局 console。

★ 变异自检（防恒绿）：删掉 mimo_boot.py 里那次 `ensure_console_on_stderr()`
调用，本测试的「console.file 必须朝 stderr」断言必须转红。
"""
from __future__ import annotations

import importlib
import logging
import sys

import pytest


def _console_handlers():
    """`"mimoagent"` logger 上所有带 console 的 handler。"""
    lg = logging.getLogger("mimoagent")
    return [h for h in lg.handlers
            if getattr(h, "console", None) is not None]


@pytest.fixture
def _stdout_polluted_handler():
    """把现场复位成「mimoagent 的 RichHandler 朝 stdout 写」。

    给每个 console handler 换一个 `Console(file=sys.stdout)`（全新进程的
    初始态）；结束后用 mimoagent 自己的 `_setup_root_logger()` 还原原生
    handler（挂回全局 console），不留下测试痕迹。

    ★ 第二个掩蔽通道（2026-10-15 实测，变异单跑假绿过）：测试体
      `import app.agent.mimo_boot` 若在本进程是**首次**，会连带首次导入
      `app.agent.log` —— 它模块级就有一次 `ensure_console_on_stderr()`，
      把变异（摘掉 boot 里的调用）的后果全掩盖了。故这里必须先导入
      `app.agent.log`（副作用先跑完、一无所获），再造污染现场。
    """
    import app.agent.log  # noqa: F401 — 先导入：其 import 时收口已跑完
    from rich.console import Console
    import mimoagent  # noqa: F401 — 确保 utils/log 的 _setup_root_logger 已跑过
    lg = logging.getLogger("mimoagent")
    targets = _console_handlers()
    assert targets, "mimoagent logger 上没有 console handler（现场构造失败？）"
    saved = [(h, h.console) for h in targets]
    try:
        for h, _ in saved:
            h.console = Console(file=sys.stdout)
        yield
    finally:
        import mimoagent.utils.log as mlog
        mlog._setup_root_logger()      # 重建原生 handler（全局 console）


def test_boot_redirects_mimoagent_console_to_stderr(_stdout_polluted_handler):
    """执行核解析完成后，mimoagent 的控制台 handler 必须朝 stderr。"""
    # 前置断言：现场确实是「朝 stdout」（否则下面的通过是假的）
    for h in _console_handlers():
        assert h.console.file is sys.stdout, (
            "前置现场应为 stdout —— 若已指向 stderr，本测试的通过就不构成"
            "对 mimo_boot 的证明"
        )

    import app.agent.mimo_boot as boot
    importlib.reload(boot)          # 重跑顶层：ensure_mimoagent + 收口

    after = _console_handlers()
    assert after, "reload 后 console handler 不见了"
    for h in after:
        assert h.console.file is sys.stderr, (
            "mimoagent 的 RichHandler 仍朝 stdout ⇒ 其 INFO 会污染 stdout 结果通道"
        )


def test_boot_redirect_is_idempotent(_stdout_polluted_handler):
    """重复 reload 不报错、不换流（收口可安全重入）。"""
    import app.agent.mimo_boot as boot
    importlib.reload(boot)
    importlib.reload(boot)
    for h in _console_handlers():
        assert h.console.file is sys.stderr


def test_boot_keeps_file_handlers_untouched(_stdout_polluted_handler):
    """只收口「朝 stdout 写」的 handler；mimoagent 的文件 handler 不许动。

    `add_file_handler()` 挂的 FileHandler 是 run 级日志的关键路径，
    收口若误删它 ⇒ mimoagent 的运行日志丢失（静默）。
    """
    import mimoagent.utils.log as mlog
    fh = logging.FileHandler("NUL", delay=True)   # Windows 空设备；delay=True 不真建文件
    mlog.logger.addHandler(fh)
    try:
        import app.agent.mimo_boot as boot
        importlib.reload(boot)
        assert fh in mlog.logger.handlers, "FileHandler 被收口误删"
        assert not hasattr(fh, "console"), "FileHandler 不该有 console 属性"
    finally:
        mlog.logger.removeHandler(fh)
        fh.close()
