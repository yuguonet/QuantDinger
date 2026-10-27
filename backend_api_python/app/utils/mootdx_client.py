"""
mootdx_client.py — 全局共享 mootdx Quotes 客户端（单例）

═══════════════════════════════════════════════════════════════
  所有需要 mootdx TCP 连接的模块统一从此处获取客户端
═══════════════════════════════════════════════════════════════

设计原则:
  1. 全局单例: 整个进程只维护一个 mootdx TCP 连接（含保活线程，见下）
  2. 线程安全: 加锁保护首次创建
  3. 自动重连: 连接断开/超时后自动重建
  4. 服务器选择: 免扫描直连（_last_server / config BESTIP.HQ）优先，
     回退 HQ 主机扫描榜（并发 TCP 探测 ~1-3s）逐台过数据面验证
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
  - app/data_sources/provider/mootdx.py（coordinate_market_kline 经此拉 K线）

初始化收敛（2026-10-27）：本文件是**唯一**初始化点。原线程级客户端
（get_thread_client，backfill_db 并行抓取用）已随 backfill_db 归档删除——
每线程一条 TCP+心跳的第二套初始化机制无人消费；原策略1（provider.tdx_ex
已探测服务器）随 tdx_ex 模块退役删除。现存两条策略：免扫描直连 → bestip。
"""

from __future__ import annotations

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
FAIL_TTL = 300     # 失败缓存: 5分钟内不重试 bestip 扫描（避免每次 84s 重扫）

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


_hq_scan_cache: Optional[list] = None


def _scan_hq_candidates(limit: int = 8) -> list:
    """TCP 存活扫描 mootdx HQ 主机清单，返回按延迟排序的 (host, port) 榜（进程内缓存）。

    只回答"哪些机器 TCP 活着"，**不**代表数据面可用 —— 由调用方逐台过
    _probe_data_plane（协议通 ≠ bars 有数据）。并发探测 ~1-3s，替代原
    Quotes.factory(bestip=True) 的 74s 串行全扫 + 只取第一名的黑盒行为
    （2026-10-27 日志：bestip 两轮选出的"最快"都是 bars 恒空的僵尸）。
    """
    global _hq_scan_cache
    if _hq_scan_cache:
        return _hq_scan_cache
    import socket
    import concurrent.futures as cf

    hq: list = []
    try:
        # importlib 取真子模块：`import mootdx.server` 会被包属性上的同名函数遮蔽
        import importlib
        _srv = importlib.import_module("mootdx.server")
        hq = [(h["addr"], int(h["port"])) for h in (_srv.hosts or {}).get("HQ", [])]
    except Exception as e:
        logger.warning("[mootdx] HQ 主机清单获取失败: %s", e)
    ranked = []

    def _ping(ap):
        a, p = ap
        t0 = time.perf_counter()
        try:
            s = socket.create_connection((a, p), timeout=0.7)
            s.close()
            return (time.perf_counter() - t0, a, p)
        except Exception:
            return None

    try:
        with cf.ThreadPoolExecutor(max_workers=32) as ex:
            for r in ex.map(_ping, hq):
                if r:
                    ranked.append(r)
    except Exception as e:
        logger.warning("[mootdx] HQ 扫描失败: %s", e)
    ranked.sort()
    _hq_scan_cache = [(a, p) for _, a, p in ranked[:limit]]
    logger.info("[mootdx] HQ 扫描: %d/%d 台 TCP 通，取前 %d 作数据面候选",
                len(ranked), len(hq), len(_hq_scan_cache))
    return _hq_scan_cache


def _create_client() -> Optional[object]:
    """创建并连接一个 mootdx Quotes 客户端（免扫描直连优先 → HQ 榜逐台验数据面）。

    Quotes.factory(timeout=10) → pytdx connect(socket).settimeout(10)：
    socket 层自带 10s 超时，单次 recv 停滞会在超时后抛异常，不会永久挂死。

    每台候选都必须过数据面验证（bars 真能出数）；僵尸机进 _bad_servers
    不再重试。全部候选判死 ⇒ 返 None，由 get_client 的 FAIL_TTL 退避。
    """
    from mootdx.quotes import Quotes
    global _last_server

    # 必须早于 Quotes.factory(heartbeat=True)：心跳线程在 connect() 里 start，
    # daemon 属性只能在 start 之前改写。
    _daemonize_heartbeat()

    # 候选: 免扫描直连（_last_server/config，命中即 0.15s）优先；
    # 直连全废才跑 HQ 扫描榜（懒触发，命中零扫描成本）。
    tried = 0

    def _try_candidates(cands) -> Optional[object]:
        """逐台建连 + 数据面把关；返回活客户端，全废返回 None（计数进 tried）。"""
        nonlocal tried
        global _last_server
        for host, port in cands:
            if (host, port) in _bad_servers:
                continue
            tried += 1
            if tried > 10:                   # 数据面判死一台 ~4.4s，上限兜住最坏耗时
                return None
            try:
                cli = Quotes.factory(
                    market='std', timeout=10, heartbeat=True,
                    server=(host, port),
                )
            except Exception:
                continue
            if _probe_data_plane(cli):
                _last_server = (host, port)
                logger.info("[mootdx] 直连成功 %s:%d（数据面验证通过，第 %d 候选）",
                            host, port, tried)
                return cli
            _bad_servers.add((host, port))
            logger.info("[mootdx] %s:%d 协议通但数据面不可用（bars 空），"
                        "标记坏服务器、试下一台", host, port)
            try:
                cli.close()
            except Exception:
                pass
        return None

    cli = _try_candidates(_direct_server_candidates())
    if cli is not None:
        return cli
    cli = _try_candidates(_scan_hq_candidates())
    if cli is not None:
        return cli

    logger.warning("[mootdx] %d 台候选全部数据面不可用（进程内已判死 %d 台），本周期放弃",
                   tried, len(_bad_servers))
    return None


def get_client() -> Optional[object]:
    """获取全局共享的 mootdx Quotes 客户端（单例，**唯一**取数入口）。

    策略:
      1. 已有连接且未过期且未关闭 → 直接复用
      2. 免扫描直连（_last_server / config BESTIP.HQ）+ 数据面验证
      3. HQ 扫描榜逐台验数据面（协议通 ≠ bars 活，僵尸进 _bad_servers）

    失败缓存（2026-10-26）: _create_client 失败时也刷新 _client_ts，
    避免每次调用都重扫（扫描虽已并发化，数据面判死仍有代价）。
    失败结果缓存 FAIL_TTL 秒。

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

    # 失败缓存: 近期失败过 → 直接返回 None（不重扫）
    if _client is None and _client_ts > 0 and (time.time() - _client_ts) < FAIL_TTL:
        return None

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

        # double-check 失败缓存
        if _client is None and _client_ts > 0 and (time.time() - _client_ts) < FAIL_TTL:
            return None

        _client = _create_client()
        _client_ts = time.time()  # 成功和失败都刷新时间戳
        return _client


def reset_client() -> None:
    """强制重置连接（下次 get_client() 会自动重建）。"""
    global _client, _client_ts
    with _client_lock:
        _client = None
        _client_ts = 0


# 原「线程级客户端」段（_thread_local / get_thread_client / reset_thread_client，
# 每线程一条 TCP+心跳的第二套初始化机制）已删除，2026-10-27：
# 唯一消费者 backfill_db 归档进 del/backfill_db_20261027/ 后生产零调用，
# 并行抓取现走 coordinator.coordinate_market_kline（其 provider 复用 get_client 单例）。
# mootdx 初始化收敛为本文件单例 + 保活 daemon 一条路径。


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
