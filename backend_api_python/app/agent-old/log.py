# -*- coding: utf-8 -*-
"""
Agent Log — structlog 结构化日志（轻量桥接）。

用法：
  from app.agent.log import logger
  logger.info("tool_called", tool="get_kline", stock="600519", duration=0.3)

自动输出 JSON 格式（生产）或彩色控制台（开发），带时间戳 + 调用位置。
不依赖 asgi_correlation_id / settings 等外部模块，agent 模块自包含。
"""
from __future__ import annotations

import logging
import os
import sys

# ── structlog 初始化（只执行一次）────────────────────────────
_initialized = False

def _setup():
    global _initialized
    if _initialized:
        return
    _initialized = True

    try:
        import structlog
    except ImportError:
        # structlog 未安装，降级标准 logging
        return

    log_format = os.getenv("LOG_FORMAT", "console").strip().lower()
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, log_level, logging.INFO)

    # 标准 logging 基础配置
    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # 共享处理器
    shared_processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        # 调用位置
        structlog.processors.CallsiteParameterAdder({
            structlog.processors.CallsiteParameter.FILENAME,
            structlog.processors.CallsiteParameter.FUNC_NAME,
            structlog.processors.CallsiteParameter.LINENO,
        }),
    ]

    if log_format == "json":
        # 生产：JSON 格式
        structlog.configure(
            processors=[*shared_processors, structlog.processors.JSONRenderer()],
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        # 开发：彩色控制台
        structlog.configure(
            processors=[*shared_processors, structlog.dev.ConsoleRenderer()],
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

_setup()

# ── 导出 logger ─────────────────────────────────────────────
try:
    import structlog
    logger = structlog.get_logger()
    # 调试：确认 logger 可用
    logger.info("agent_logger_ready", backend="structlog")
except ImportError:
    # 降级标准 logging
    import logging
    logger = logging.getLogger("app.agent")
    logger.info("agent_logger_ready", backend="stdlib")
