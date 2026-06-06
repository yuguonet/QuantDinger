# -*- coding: utf-8 -*-
"""
mootdx 统一连接管理

自动选择最优服务器，提供 StdQuotes / ExtQuotes 单例。
内部封装 Quotes.factory()，其他模块统一从这里拿 client。
"""

from __future__ import annotations

import threading
from typing import Optional

from mootdx.quotes import Quotes

from app.utils.logger import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_std_client = None
_ext_client = None


def get_std_client(**kwargs) -> Quotes:
    """获取 A 股行情客户端（StdQuotes 单例）"""
    global _std_client
    if _std_client is not None:
        return _std_client
    with _lock:
        if _std_client is not None:
            return _std_client
        logger.info("[Mootdx] 正在连接 A 股行情服务器...")
        _std_client = Quotes.factory(market="std", **kwargs)
        logger.info("[Mootdx] A 股行情服务器已连接")
        return _std_client


def get_ext_client(**kwargs) -> Quotes:
    """获取扩展行情客户端（ExtQuotes 单例）"""
    global _ext_client
    if _ext_client is not None:
        return _ext_client
    with _lock:
        if _ext_client is not None:
            return _ext_client
        logger.info("[Mootdx] 正在连接扩展行情服务器...")
        _ext_client = Quotes.factory(market="ext", **kwargs)
        logger.info("[Mootdx] 扩展行情服务器已连接")
        return _ext_client


def reset():
    """重置所有连接（用于测试或连接异常时）"""
    global _std_client, _ext_client
    with _lock:
        if _std_client:
            try:
                _std_client.close()
            except Exception:
                pass
            _std_client = None
        if _ext_client:
            try:
                _ext_client.close()
            except Exception:
                pass
            _ext_client = None
        logger.info("[Mootdx] 所有连接已重置")
