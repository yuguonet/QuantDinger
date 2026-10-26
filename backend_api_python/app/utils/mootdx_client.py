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
import os
import threading
import time
from typing import Optional, Tuple

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

# ── 免扫描直连状态（2026-10-25 L0）────────────────────────────
# 背景：`Quotes.factory(bestip=True)` 会**无条件**串行扫描 146 个 host
# （142 HQ + 3 EX + 1 GP，socket timeout 0.7s）≈ 74s/进程冷启，且扫描结果
# 不跳过重扫。下面两级候选让"已知可用/已知已死"的服务器跳过全扫：
#   _last_server  进程内上次**数据面验证可用**的 (host, port)
#   _bad_servers  进程内已验证"协议通但数据面死"的集合（不重复试）
# 跨进程复用走 ~/.mootdx/config.json 的 BESTIP.HQ——该文件由 mootdx 自己
# 写（StdQuotes.__init__ 传 server 时 / bestip() 扫描后），项目零维护。
_last_server: Optional[Tuple[str, int]] = None
_bad_servers: set = set()


def _live_servers() -> list:
    """获取 provider.tdx_ex 已探测的可用 HQ 服务器列表。"""
    try:
        from app.data_sources.provider.tdx_ex import TdxExDataSource
        provider = TdxExDataSource()
        return [(h, p) for h, p, proto in provider._live_servers if proto == "hq"]
    except Exception as e:
        logger.debug("[mootdx] 获取 provider 服务器失败: %s", e)
        return []


def _daemonize_heartbeat() -> None:
    """把 TDX 客户端的心跳线程设为 daemon（2026-10-12 实测修复，见 tmp/_screener_hang.py）

    现象：run_skill(market-screener) 恒定 600s 超时，但流水线本体只要 ~55s。
    真因：`Quotes.factory(heartbeat=True)` 会起一条 tdxpy.heartbeat.HeartBeatThread，
    它是**非 daemon** 线程（10s 周期 stop_event.wait() 无限循环，只有 disconnect
    才停）。主线程结束后 threading._shutdown() 会 join 它 ⇒ 永不返回 ⇒
    ① subprocess.run(timeout=600) 必然超时；② concurrent.futures 的池线程靠
    threading._register_atexit 唤醒，而该回调排在 join 之后 ⇒ 连唤醒信号都收不到，
    于是「mkline-timeout_*」这类线程也一起卡住。

    心跳只为长连接保活，进程要退出时不值得等它 —— 设为 daemon 即可随进程退出。
    两条可能的来源库（mootdx 2.x 走 tdxpy，1.x 走 pytdx）都覆盖，幂等。
    """
    import importlib

    for mod_name, cls_name in (("tdxpy.heartbeat", "HeartBeatThread"),
                               ("pytdx.heartbeat", "HqHeartBeatThread")):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        cls = getattr(mod, cls_name, None)
        if cls is None or getattr(cls, "_qd_daemonized", False):
            continue
        _orig_init = cls.__init__

        def _init(self, *args, __orig=_orig_init, **kwargs):
            __orig(self, *args, **kwargs)
            self.daemon = True       # 在 start() 之前改写，daemon 只有在启动前可设

        cls.__init__ = _init
        cls._qd_daemonized = True
        logger.debug("[mootdx] %s.%s 已改为 daemon 线程", mod_name, cls_name)


def _read_config_bestip() -> Optional[Tuple[str, int]]:
    """跨进程复用：读 mootdx 自己维护的 ~/.mootdx/config.json 的 BESTIP.HQ。

    该文件由库自动写（`StdQuotes.__init__` 传 server 时 `config.set`、
    `bestip()` 扫描后 dump），无需项目维护；文件缺失/损坏时静默跳过。
    """
    try:
        import json as _json

        from mootdx.utils import get_config_path

        path = get_config_path("config.json")
        if not path or not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        val = (data.get("BESTIP") or {}).get("HQ")
        if isinstance(val, (list, tuple)) and len(val) == 2:
            return (str(val[0]), int(val[1]))
    except Exception as e:
        logger.debug("[mootdx] 读取 config BESTIP 失败: %s", e)
    return None


def _probe_data_plane(cli) -> bool:
    """数据面验证：拉 1 根K线判定"能真拿到数据"。

    ⚠ 协议探活不可信：2026-10-25 实测 123.125.108.14:7709 的
    `get_security_count` 0.03s 返回 27711（协议层活着），但 `bars`
    恒返回空——该服务器 K 线数据面已死。若用轻量协议包验证，会把烂客户端
    当好客户端放进单例，之后每次 bars 白等 ~4.4s。故验证必须用 bars 本身。
    """
    try:
        df = cli.bars(symbol="600519", frequency=9, offset=1)
        if df is None:
            return False
        empty = getattr(df, "empty", None)   # DataFrame
        return not (empty if empty is not None else not df)
    except Exception:
        return False


def _direct_server_candidates() -> list:
    """免扫描直连候选（去重、去进程内已判死）：进程内 _last_server 优先，
    其次 ~/.mootdx/config.json 的 BESTIP.HQ（跨进程上次结果）。"""
    out: list = []
    for srv in (_last_server, _read_config_bestip()):
        if srv and srv not in out and srv not in _bad_servers:
            out.append(srv)
    return out


def _create_client(servers: Optional[list] = None) -> Optional[object]:
    """创建并连接一个 mootdx Quotes 客户端（provider 服务器优先，bestip 回退）。

    Quotes.factory(timeout=10) → pytdx connect(socket).settimeout(10)：
    socket 层自带 10s 超时，单次 recv 停滞会在超时后抛异常，不会永久挂死。

    Args:
        servers: 依次尝试的 (host, port) 列表；None 时按默认顺序取 live 服务器。
    """
    from mootdx.quotes import Quotes
    global _last_server

    # 必须早于 Quotes.factory(heartbeat=True)：心跳线程在 connect() 里 start，
    # daemon 属性只能在 start 之前改写。
    _daemonize_heartbeat()

    # 策略1: 从 provider.tdx_ex 获取已探测的可用 HQ 服务器
    #   （tdx_ex 退役后本策略自然消失，见下方策略2 接棒）
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
                _last_server = (host, port)
                return cli
            except Exception:
                continue
        logger.warning("[mootdx] 服务器列表全部失败, 回退免扫描直连")

    # 策略2（2026-10-25 L0）: 免扫描直连 —— 跳过 bestip 全扫（74s/进程冷启）。
    #   直连 0.15s 建成后必须过数据面验证；本环境实测"协议通但 bars 恒空"
    #   的僵尸服务器，判死 ~4.4s 后标记 _bad_servers 不再重试。
    for host, port in _direct_server_candidates():
        try:
            cli = Quotes.factory(
                market='std', timeout=10, heartbeat=True,
                server=(host, port),
            )
        except Exception:
            continue
        if _probe_data_plane(cli):
            _last_server = (host, port)
            logger.info("[mootdx] 免扫描直连成功 %s:%d（跳过 bestip 全扫）", host, port)
            return cli
        _bad_servers.add((host, port))
        logger.info("[mootdx] %s:%d 协议通但数据面不可用（bars 空），"
                    "标记坏服务器、回落下一级", host, port)
        try:
            cli.close()
        except Exception:
            pass

    # 策略3: bestip 自动探测（最后手段，~74s）
    #   同样必须过数据面验证——本环境实测 bestip 扫出的 IP "TCP 通但 bars
    #   恒空"，不过验证会把僵尸客户端放进单例（每次 bars 白等 4.4s）。
    try:
        cli = Quotes.factory(market='std', bestip=True, timeout=10, heartbeat=True)
    except Exception as e:
        logger.error("[mootdx] 连接失败: %s", e)
        return None
    if cli is None:
        return None
    srv = getattr(cli, "server", None)
    if isinstance(srv, (list, tuple)) and len(srv) == 2:
        srv = (str(srv[0]), int(srv[1]))
    if not _probe_data_plane(cli):
        if srv:
            _bad_servers.add(srv)
        logger.info("[mootdx] bestip 结果 %s 数据面不可用（bars 空），放弃入单例", srv)
        try:
            cli.close()
        except Exception:
            pass
        return None
    if srv:
        _last_server = srv
    logger.info("[mootdx] bestip 探测连接成功 %s（数据面验证通过）", srv)
    return cli


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
            import traceback as _tb
            with open('tmp/_ts_write_stack.txt', 'a', encoding='utf-8') as _f:
                _f.write('=== ts write @ %.3f ===\n%s\n' % (time.time(), ''.join(_tb.format_stack())))
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


# ================================================================
# 保活 daemon（2026-10-25 L1）
# ================================================================
# 解决"全局单例 TTL 到期重建撞上业务调用"：CLIENT_TTL=3600s，过期后第一个
# 业务调用会等重建（L0 后最坏 ~5s，本环境 TDX 数据面死）。保活线程在单例
# 年龄达到 TTL×80% 时提前重建，业务任何时刻都命中热连接。
#
# 落点说明：由 app/market_cn/scheduler.py::start() 调用（常驻调度器进程），
# 不放在 app/__init__.py —— 后者是 `import app.*` 的必经之路，skill_run
# 子进程与 pytest 全体进程都会执行，起线程会污染测试与短命进程。

_KEEPALIVE_STARTED = False
_KEEPALIVE_AGE_RATIO = 0.8          # 单例年龄达 TTL 的 80% 即提前重建
_KEEPALIVE_DEFAULT_SEC = 2880       # 循环周期 = TTL×0.8；env 可调，≤0 关闭


def _keepalive_should_rebuild(now: Optional[float] = None) -> bool:
    """单例年龄是否达到 TTL×_KEEPALIVE_AGE_RATIO（该提前重建）。纯判定无 IO。

    从未建过客户端（_client_ts=0）时视为 infinitely stale——保活线程有责任
    把首个连接建好（本环境 TDX 数据面死时快速失败，业务无感）。
    """
    now = time.time() if now is None else now
    ts = _client_ts
    age = (now - ts) if ts else float("inf")
    return age >= CLIENT_TTL * _KEEPALIVE_AGE_RATIO


def _keepalive_loop(interval_sec: int, sleep_fn=time.sleep) -> None:
    """周期检查单例年龄，临近 TTL 主动重建（L0 免扫描直连，~0.2~5s）。

    sleep_fn 可注入（单测用假 sleep，避免 patch 全局 time.sleep 唤醒
    调度器主循环等无关线程）。持锁 double-check：判定与置空间隙内可能有
    业务线程已完成重建（_client_ts 刷新），不能把新连接误清掉。
    """
    global _client

    while True:
        sleep_fn(interval_sec)
        try:
            if not _keepalive_should_rebuild():
                continue
            with _client_lock:
                if not _keepalive_should_rebuild():   # double-check
                    continue
                _client = None      # 强制下次 get_client 重建
            cli = get_client()          # 持锁重建；业务并发最坏等这一次
            if cli is None:
                logger.info("[mootdx-keepalive] 已重建：客户端不可用"
                            "（TDX 数据面死/无服务器），已快速失败，不影响业务降级链")
            else:
                logger.info("[mootdx-keepalive] 单例已提前重建并保活")
        except Exception as e:
            logger.warning("[mootdx-keepalive] 保活失败（不影响业务）: %s", e)


def start_mootdx_keepalive(interval_sec: Optional[int] = None) -> bool:
    """启动 mootdx 全局单例保活 daemon（幂等，重复调用只起一条）。

    Args:
        interval_sec: 循环周期秒；None 时读 env QD_MOOTDX_KEEPALIVE_SEC，
            缺省 2880；≤0 或非法非数字时关闭不报错。

    Returns:
        True=线程已起（或已在跑）；False=按配置关闭。
    """
    global _KEEPALIVE_STARTED

    if interval_sec is None:
        raw = os.getenv("QD_MOOTDX_KEEPALIVE_SEC", "") or ""
        try:
            interval_sec = int(raw) if raw else _KEEPALIVE_DEFAULT_SEC
        except ValueError:
            interval_sec = _KEEPALIVE_DEFAULT_SEC
    if interval_sec <= 0:
        logger.info("[mootdx-keepalive] 已关闭（QD_MOOTDX_KEEPALIVE_SEC≤0）")
        return False
    if _KEEPALIVE_STARTED:
        return True
    _KEEPALIVE_STARTED = True
    t = threading.Thread(target=_keepalive_loop, args=(interval_sec,),
                         daemon=True, name="mootdx-keepalive")
    t.start()
    logger.info("[mootdx-keepalive] 保活线程已启动（周期 %ds，TTL %ds，"
                "年龄 %.0f%% 提前重建）", interval_sec, CLIENT_TTL,
                _KEEPALIVE_AGE_RATIO * 100)
    return True
