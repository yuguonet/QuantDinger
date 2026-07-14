# -*- coding: utf-8 -*-
"""
Agent Log — 兼容 app.agent.log 的轻量级桥接。

工具模块中 `from app.agent.log import logger` 可正常工作。
同时将日志写入 logs/app.log，与应用其他模块共享同一个文件。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
_log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# ── 仅当 root logger 尚无 handler 时初始化 ──
# 避免重复 basicConfig 覆盖掉主应用已配好的文件/控制台 handler
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        format=_log_format,
        level=getattr(logging, _level, logging.INFO),
        handlers=[logging.StreamHandler(sys.stdout)],
    )

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
