# -*- coding: utf-8 -*-
"""
Agent Log — 轻量级桥接（agent 包子模块的统一 logger 出口）。

工具模块中 `from log import logger` 可正常工作（agent 包内统一裸名导入）。
同时将日志写入 logs/app.log，与应用其他模块共享同一个文件。
输出经 MASK 脱敏（常设 6）。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


class _MaskFilter(logging.Filter):
    """日志脱敏：api_key/phone/id 等不进 JSONL 与 log。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from .utils.mask import mask_text
            record.msg = mask_text(str(record.msg))
            if record.args:
                record.args = tuple(
                    mask_text(a) if isinstance(a, str) else a for a in record.args
                )
        except Exception:
            pass
        return True


_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
_log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# ── 仅当 root logger 尚无 handler 时初始化 ──
# 避免重复 basicConfig 覆盖掉主应用已配好的文件/控制台 handler
# ★ 2026-10-14：控制台 handler 一律走 **stderr**，不许走 stdout。
#   CLI 的 stdout 是**结果通道**（`python -m app.agent.cli "问题" | ...` 要能
#   直接拿到答案）；日志混进去会把结果污染成不可解析的混合物 —— 实测跑 cli
#   时 stdout 前 30 行全是 mimoagent / httpx / 本项目的 INFO 日志。
#   日志仍需可见 ⇒ stderr（终端照样显示，且可被 2>/dev/null 单独丢弃）。
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        format=_log_format,
        level=getattr(logging, _level, logging.INFO),
        handlers=[logging.StreamHandler(sys.stderr)],
    )

def _console_writes_to_stdout(handler: logging.Handler) -> bool:
    """判断 handler 的控制台输出是否指向 stdout。

    两类形态都要认：
    · `logging.StreamHandler` 系 —— 看 `.stream`；
    · rich 系（`rich.logging.RichHandler`）—— **不是** StreamHandler 子类，
      输出流在 `.console.file`（未显式设置时 rich 回落到 `sys.stdout`）。
      ★ 2026-10-14 实测：mimoagent 在 import 期给具名 logger `mimoagent`
      挂的就是这种 handler，所以只判 StreamHandler 会漏掉它。
    """
    stream = getattr(handler, "stream", None)
    if stream is not None:
        return stream is sys.stdout
    console = getattr(handler, "console", None)
    return getattr(console, "file", None) is sys.stdout


def _redirect_handler_to_stderr(handler: logging.Handler) -> bool:
    """把单个 handler 的输出改指 stderr，成功返回 True。"""
    if getattr(handler, "stream", None) is sys.stdout:
        setter = getattr(handler, "setStream", None)
        if callable(setter):
            setter(sys.stderr)  # setStream 会先把旧流 flush 掉，比直接赋值安全
        else:
            handler.stream = sys.stderr
        return True
    console = getattr(handler, "console", None)
    if console is not None and getattr(console, "file", None) is sys.stdout:
        console.file = sys.stderr  # rich 的 Console.file 是可写属性
        return True
    return False


def ensure_console_on_stderr() -> int:
    """把**全进程**所有「朝 stdout 写」的日志 handler 收口到 stderr。

    返回被改指的 handler 数量（幂等，重复调用第二次为 0）。

    为什么必须扫全部 logger 而不只扫 root：
    · basicConfig 只在「root 尚无 handler」时才跑 ⇒ 第三方若**先**配到了
      stdout，那段不执行，是否被污染就变成「取决于 import 顺序」的玄学；
    · 更要命的是 **具名 logger + propagate=False**（mimoagent 就是这么干的）：
      它完全绕开 root，既不吃 root 的 handler，也不理任何 root 级配置 ⇒
      只扫 root **永远看不到它**，而它的输出恰好落在 stdout。
    ⇒ 不猜是谁配的、也不依赖 import 顺序，只按「控制台 handler 一律朝 stderr」
      这条契约统一收口。第三方 logger 的 level / propagate / 格式一律不动。
    """
    n = 0
    try:
        known = list(logging.Logger.manager.loggerDict.values())
    except Exception:  # 极端情况下 manager 不可用，至少别让日志配置炸掉 CLI
        known = []
    for lg in [logging.getLogger()] + known:
        if not isinstance(lg, logging.Logger):  # PlaceHolder 不是真 logger
            continue
        for h in list(lg.handlers):
            if _console_writes_to_stdout(h) and _redirect_handler_to_stderr(h):
                n += 1
    return n


# ── 兜底：把**任何**指向 stdout 的控制台 handler 改指 stderr ──
# 本模块 import 时先收口一次（覆盖在此之前已完成配置的第三方 logger）。
# 之后才 import 的（mimoagent 走 cli 的延迟导入）由调用方再调一次 —— 见
# `cli._import_agent()`。
ensure_console_on_stderr()

# ── 添加 RotatingFileHandler → logs/app.log ──
# 与 app/utils/logger.py 写入同一个文件
_log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
_app_log_path = os.path.abspath(os.path.join(_log_dir, "app.log"))

root = logging.getLogger()
_file_handler = None
for h in root.handlers:
    if isinstance(h, RotatingFileHandler) and hasattr(h, "baseFilename"):
        if os.path.abspath(h.baseFilename) == _app_log_path:
            _file_handler = h
            break

if _file_handler is None:
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _app_log_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter(_log_format))
    _file_handler.setLevel(getattr(logging, _level, logging.INFO))
    root.addHandler(_file_handler)

logger = logging.getLogger("app.agent")

# ⚠️ filter 挂 logger 只覆盖「直接打到 app.agent 这条 logger」的记录。
# 子 logger（logging.getLogger("app.agent.xxx")，agent 包内绝大多数模块都是这种）
# 的 record 是 **propagate** 上来的：Python 不会让它经过父 logger 的 filter，
# 只经过父 logger 的 **handler** 的 filter。
# ⇒ 只挂 logger.filters 时，脱敏对 app.agent.* 全部失效，api_key / 手机号等
#   会明文落进 logs/app.log。必须挂到 handler 上才真正生效。
_mask = _MaskFilter()
if not any(isinstance(f, _MaskFilter) for f in logger.filters):
    logger.addFilter(_mask)
for _h in list(root.handlers):
    if not any(isinstance(f, _MaskFilter) for f in _h.filters):
        _h.addFilter(_mask)
