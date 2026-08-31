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

import itertools
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

# 线程创建连接时的服务器轮转游标：让不同 worker 落在不同 live 服务器上，
# 避免 8 条连接全压同一台触发通达信限流/封 IP。
_server_rotor = itertools.count()


def _live_servers() -> list:
    """获取 provider.tdx_ex 已探测的可用 HQ 服务器列表。"""
    try:
        from app.data_sources.provider.tdx_ex import TdxExDataSource
        provider = TdxExDataSource()
        return [(h, p) for h, p, proto in provider._live_servers if proto == "hq"]
    except Exception as e:
        logger.debug("[mootdx] 获取 provider 服务器失败: %s", e)
        return []


def _create_client(servers: Optional[list] = None) -> Optional[object]:
    """创建并连接一个 mootdx Quotes 客户端（provider 服务器优先，bestip 回退）。

    Quotes.factory(timeout=10) → pytdx connect(socket).settimeout(10)：
    socket 层自带 10s 超时，单次 recv 停滞会在超时后抛异常，不会永久挂死。

    Args:
        servers: 依次尝试的 (host, port) 列表；None 时按默认顺序取 live 服务器。
    """
    from mootdx.quotes import Quotes

    # 策略1: 从 provider.tdx_ex 获取已探测的可用 HQ 服务器
    if servers is None:
        servers = _live_servers()
    if servers:
        for host, port in servers:
            try:
                cli = Quotes.factory(
                    market='std', timeout=10, heartbeat=True,
                    server=(host, port),
                )
                logger.info("[mootdx] 连接成功 %s:%d", host, port)
                return cli
            except Exception:
                continue
        logger.warning("[mootdx] 服务器列表全部失败, 回退 bestip")

    # 策略2: bestip 自动探测
    try:
        cli = Quotes.factory(market='std', bestip=True, timeout=10, heartbeat=True)
        logger.info("[mootdx] bestip 探测连接成功")
        return cli
    except Exception as e:
        logger.error("[mootdx] 连接失败: %s", e)
        return None


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

        _client = _create_client()
        if _client:
            _client_ts = time.time()
        return _client


def reset_client() -> None:
    """强制重置连接（下次 get_client() 会自动重建）。"""
    global _client, _client_ts
    with _client_lock:
        _client = None
        _client_ts = 0


# ================================================================
# 线程级客户端（用于高并发批量拉取，如 backfill_db 并行抓取）
# ================================================================
# 每个线程持有一条独立 TCP 连接，互不阻塞；单条连接卡死只影响该线程自身，
# 不会像共享单例那样级联拖垮全部分片。

_thread_local = threading.local()


def _thread_servers() -> list:
    """当前线程创建连接使用的服务器顺序：轮转打散，避免全压一台。"""
    servers = _live_servers()
    if servers:
        offset = next(_server_rotor) % len(servers)
        servers = servers[offset:] + servers[:offset]
    return servers


def get_thread_client() -> Optional[object]:
    """获取当前线程专用的 mootdx 客户端（线程级单例）。

    Returns:
        mootdx.quotes.Quotes 实例, 或 None（连接失败时）
    """
    cli = getattr(_thread_local, "client", None)
    ts = getattr(_thread_local, "ts", 0)
    if cli is not None:
        try:
            if not cli.closed and (time.time() - ts) < CLIENT_TTL:
                return cli
        except Exception:
            pass

    cli = _create_client(_thread_servers())
    _thread_local.client = cli
    _thread_local.ts = time.time() if cli else 0
    return cli


def reset_thread_client() -> None:
    """断开并丢弃当前线程的客户端（下次 get_thread_client() 会重建）。

    显式 disconnect 以停掉对应用户态 heartbeat 线程，避免旧连接泄漏。
    """
    cli = getattr(_thread_local, "client", None)
    if cli is not None:
        try:
            cli.client.disconnect()
        except Exception:
            pass
    _thread_local.client = None
    _thread_local.ts = 0
