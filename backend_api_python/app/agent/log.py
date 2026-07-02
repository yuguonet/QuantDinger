# -*- coding: utf-8 -*-
"""
Agent Log — 兼容 app.agent.log 的轻量桥接。

工具模块中 `from app.agent.log import logger` 可正常工作。
不依赖 structlog，使用标准 logging。
"""
from __future__ import annotations

import logging
import os
import sys

_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=getattr(logging, _level, logging.INFO),
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("app.agent")
