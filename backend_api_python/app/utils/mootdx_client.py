"""
mootdx_client.py — 全局共享 mootdx Quotes 客户端（单例）

═══════════════════════════════════════════════════════════════
  所有需要 mootdx TCP 连接的模块统一从此处获取客户端
═══════════════════════════════════════════════════════════════

设计原则:
  1. 全局单例: 整个进程只维护一个 mootdx TCP 连接
  2. 线程安全: 加锁保护首次创建
  3. 自动重连: 连接断开/超时后自动重建
  4. 服务器选择: 优先复用 provider.tdx_ex 已探测的可用 HQ 服务器,
     回退到 bestip 自动探测
  5. TTL: 连接超过 CLIENT_TTL 秒自动重建, 避免长时间空闲断开

使用方式:
    from app.utils.mootdx_client import get_client
    cli = get_client()
    if cli:
        df = cli.bars(symbol="000001", frequency=4, offset=800)

替代原先各模块的 _get_client():
  - app/market_cn/index.py
  - app/market_cn/finance.py
  - app/market_cn/tape.py
  - app/data_sources/backfill_db.py
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ================================================================
# 全局单例
# ================================================================

_client = None
_client_ts = 0
_client_lock = threading.Lock()

CLIENT_TTL = 3600  # 连接有效期: 1小时


def get_client() -> Optional[object]:
    """获取全局共享的 mootdx Quotes 客户端（单例）。

    策略:
      1. 已有连接且未过期且未关闭 → 直接复用
      2. 从 provider.tdx_ex 获取已探测的可用 HQ 服务器, 逐个尝试
      3. 回退到 bestip 自动探测

    Returns:
        mootdx.quotes.Quotes 实例, 或 None（连接失败时）
    """
    global _client, _client_ts

    # 快速路径: 连接可用 → 直接返回
    if _client is not None and (time.time() - _client_ts) < CLIENT_TTL:
        try:
            if not _client.closed:
                return _client
        except Exception:
            pass
        _client = None

    # 慢路径: 需要创建/重建连接
    with _client_lock:
        # double-check: 其他线程可能已经创建好了
        if _client is not None and (time.time() - _client_ts) < CLIENT_TTL:
            try:
                if not _client.closed:
                    return _client
            except Exception:
                pass
            _client = None

        from mootdx.quotes import Quotes

        # 策略1: 从 provider.tdx_ex 获取已探测的 HQ 服务器
        try:
            from app.data_sources.provider.tdx_ex import TdxExDataSource
            provider = TdxExDataSource()
            servers = [(h, p) for h, p, proto in provider._live_servers if proto == "hq"]
            if servers:
                for host, port in servers:
                    try:
                        _client = Quotes.factory(
                            market='std', timeout=10, heartbeat=True,
                            server=(host, port),
                        )
                        _client_ts = time.time()
                        logger.info("[mootdx:shared] 连接成功 %s:%d", host, port)
                        return _client
                    except Exception:
                        continue
                logger.warning("[mootdx:shared] provider 服务器列表全部失败, 回退 bestip")
        except Exception as e:
            logger.debug("[mootdx:shared] 获取 provider 服务器失败: %s, 回退 bestip", e)

        # 策略2: bestip 自动探测
        try:
            _client = Quotes.factory(market='std', bestip=True, timeout=10, heartbeat=True)
            _client_ts = time.time()
            logger.info("[mootdx:shared] bestip 探测连接成功")
            return _client
        except Exception as e:
            logger.error("[mootdx:shared] 连接失败: %s", e)
            _client = None
            return None


def reset_client() -> None:
    """强制重置连接（下次 get_client() 会自动重建）。"""
    global _client, _client_ts
    with _client_lock:
        _client = None
        _client_ts = 0
