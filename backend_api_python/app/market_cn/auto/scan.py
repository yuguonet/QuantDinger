"""scan.py (原 dragon_scan.py) — 自动策略组盘后全市场扫描

触发: scheduler Task "daily_scan" (事件驱动分发, 2026-09-29 取代 17:25 硬编码)。
      各策略 ScanSpec.after_events/fire_at 声明依赖 (auto/sched.py::daily_fire_ready),
      数据任务 mark_event 后就绪即跑 keys 子集; 事件齐/过 DAILY_FALLBACK_FIRE 放行。
职责:
  1. 数据就绪检测 (当日 1D bar 是否已回填, 未就绪则轮询等待)
  2. 全市场逐股跑策略判定 (与回测同一份判定, core facade)。
     策略清单以注册表为准 (autodiscover + config enabled 且 kind=daily_close):
     现网 dragon_callback / break / g56 / knife_catch / tail_oversold 等; 盘中窗口类走 run_scan_knife
  3. 结果写 qd_dragon_signals (state=watch_pending, 待次日 D1 开盘处置)
  4. 历史清理 + 组对账 (组内活跃集不变, 防漂移)

手动运行:
  python -m app.market_cn.auto.scan --run [--days 320]
  python -m app.market_cn.auto.scan --run --keys g56,v1
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 手动独立运行时加载 .env (应用内运行由 app 初始化加载, 幂等无害)
try:
    from dotenv import load_dotenv
    for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '.env'),
               os.path.join(os.getcwd(), '.env')):
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

_BACKEND_ROOT_DEFAULT = None  # 由 app 包上下文提供


# ================================================================
# 全市场扫描互斥 (2026-09-29 P1-2)
# 问题: startup `_rebuild_worker` 与调度 `daily_scan` 可并发 run_scan →
#       2026-09-18 同款 DELETE+INSERT 死锁丢信号; Probe 文件名也互踩。
# 解法: 进程内 threading.Lock + 跨进程 DB 行锁 (qd_scan_lock, 连接池安全;
#       advisory lock 会随 putconn 泄漏, 故不用)。抢不到 → status="busy" 由调用方重试。
# ================================================================

_scan_thread_lock = threading.Lock()
_SCAN_LOCK_NAME = "run_scan"
#: ⚠ stale 必须配合**续约**使用 (2026-10-07): run_scan 内部 wait_data 最长 3600s,
#:   远大于 900s ⇒ 合法持有者在等数据时会被第二个进程当成 dead 持有者 DELETE 掉
#:   ⇒ 双进程并发 run_scan, 恰是 2026-09-18 DELETE+INSERT 死锁丢信号的复现条件。
#:   故等待循环每轮 `_db_touch_scan_lock` 把 acquired_at 拨到 NOW() (周期 300s
#:   ≪ 900s): **只有真卡死的进程才被回收, 合法等待永不被摘**, 短 stale 得以保留。
_SCAN_LOCK_STALE_SEC = 900


def _scan_holder() -> str:
    return f"{os.getpid()}:{threading.get_ident()}"


def _db_try_scan_lock(holder: str) -> bool:
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS qd_scan_lock (
                    lock_name   VARCHAR(32) PRIMARY KEY,
                    holder      VARCHAR(64) NOT NULL,
                    acquired_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute(
                "DELETE FROM qd_scan_lock WHERE lock_name = %s "
                "AND acquired_at < NOW() - (%s * INTERVAL '1 second')",
                (_SCAN_LOCK_NAME, _SCAN_LOCK_STALE_SEC),
            )
            cur.execute(
                "INSERT INTO qd_scan_lock (lock_name, holder) VALUES (%s, %s) "
                "ON CONFLICT (lock_name) DO NOTHING",
                (_SCAN_LOCK_NAME, holder),
            )
            acquired = bool(cur.rowcount and cur.rowcount > 0)
            db.commit()
            cur.close()
        return acquired
    except Exception as e:
        logger.warning("[scan_lock] DB 锁失败(退化为仅进程内锁): %s", e)
        return True   # DB 不可用时不挡扫描 —— 单 worker 模型下 threading 锁已够


def _db_touch_scan_lock(holder: str) -> None:
    """跨进程锁**续约** (2026-10-07 P1): 把本持有者的 acquired_at 拨到 NOW()。

    没有它 ⇒ wait_data 等到 900s 时锁行"过期" ⇒ 后来的进程 DELETE+INSERT 抢到锁
    ⇒ 两个进程同时 run_scan (注释头部要防的那件事)。失败不挡扫描: 最坏退回原状
    (靠 stale 回收), 且打一条 warning —— 静默失效不再无声。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "UPDATE qd_scan_lock SET acquired_at = NOW() "
                "WHERE lock_name = %s AND holder = %s",
                (_SCAN_LOCK_NAME, holder),
            )
            db.commit()
            cur.close()
    except Exception as e:
        logger.warning("[scan_lock] 续约失败(超 stale 后可能被他人回收): %s", e)


def _db_release_scan_lock(holder: str):
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "DELETE FROM qd_scan_lock WHERE lock_name = %s AND holder = %s",
                (_SCAN_LOCK_NAME, holder),
            )
            db.commit()
            cur.close()
    except Exception as e:
        logger.warning("[scan_lock] 释放 DB 锁失败(按超时回收): %s", e)


#: 本进程当前持有的跨进程锁标识 (**供 wait_data 续约用**, 2026-10-07)。
#: 只在 `_scan_mutex` 持有期间非空 —— 与其自己 ctx 传递 holder 相比, 不改 API
#: (`yield True/False` 的调用点全仓若干处), 续约方只读这一个模块级变量。
_HELD_LOCK_HOLDER: str | None = None


@contextmanager
def _scan_mutex():
    """抢全市场扫描互斥。yield True=持有; False=他人在扫, 调用方应 status=busy 重试。"""
    global _HELD_LOCK_HOLDER
    if not _scan_thread_lock.acquire(blocking=False):
        yield False
        return
    holder = _scan_holder()
    try:
        if not _db_try_scan_lock(holder):
            yield False
            return
        _HELD_LOCK_HOLDER = holder
        yield True
    finally:
        _HELD_LOCK_HOLDER = None
        try:
            _db_release_scan_lock(holder)
        finally:
            _scan_thread_lock.release()


def _mark_daily_scanned(keys, target):
    """成功扫描后写 qd_scheduler_done (daily_scan@<key>@target) —— 任何调用方同口径,
    含 startup 补扫, 杜绝「startup 扫了但调度器以为没扫」再扫一遍。"""
    if not keys or not target:
        return
    try:
        from app.market_cn.scheduler import mark_scheduler_task_done
        for k in keys:
            mark_scheduler_task_done(f"daily_scan@{k}", target)
    except Exception as e:
        logger.warning("[dragon_scan] 写完成标记失败(调度侧进程内缓存兜底): %s", e)


# ================================================================
# 数据加载 (已迁 data/, 此处 import 保持调用点名字不变)
# 2026-09-10: stock_info 改走 hub (fetch_stock_info_db 已归位 data/hub.py)
# ================================================================
from app.market_cn.auto.core.data.hub import all_codes, stock_info as _stock_info  # noqa: E402,F401
from app.market_cn.auto.core.data.kline import (  # noqa: E402,F401
    ASOF_MIN_BARS, asof_bars, fetch_kline_db,
)


# ══════════════════════════════════════════════════════════════════════
# 全市场取数加速 (2026-10-05)
#
# ★ 这是生产扫描**最大的一笔**开销, 定位证据见 tmp/_hotspot04~06.py:
#     逐票 fetch_kline_db × 5236  = 26.5ms/票 ⇒ 139s  (85% 墙钟)
#     批量 fetch_klines_batch     =  3.0ms/票 ⇒  16s  (8.7x)
#
# ⚠ 等价性 (为什么不传 as_of): `fetch_kline_db(code, days)` 的窗口锚是 **now**,
#   批量若传 `as_of=target` 则窗口锚变 target ⇒ 左缘早 (now-target) 天 ⇒ 比逐票版
#   多出开头几根 (实测 320 → 323 根)。虽然实证对判定 **0 影响** (447 组逐字段一致),
#   但**不传 as_of** 可让两者同窗口, 等价性是"构造性成立"而非"依赖实测"。
#   截 target 仍走调用方原有的 `bars[-1] > target` 分支 ⇒ 与旧路径逐行一致。
#
# 开关: `QD_SCAN_BATCH=0` 可整段关闭 (回落逐票), 用于线上快速回滚。

# 判定驱动 = `scan_days` 折叠内核 (**唯一**路径, 2026-10-07 P5-③ 切换)。
#   · 与 rebuild / 回测同一条驱动路径 (内核 seed 一次 + 当日 evaluate), 判定循环只有一份实现。
#   · 等价性: 全市场 5236 票 × 30 交易日 逐字段差异 0 (tmp/shadow_scan_fold3.py);
#     回放对账 (投影 Record.ready vs scan_days) 双向 0 (tools/projection_shadow --replay)。
#   · 旧的 `scan_signals` 直连分支与 `QD_SCAN_FOLD` 回滚开关已删除 (禁并存式过渡):
#     该旁路曾因「折叠判定不产门级 trace ⇒ M1 采集归零」而保留; 阻塞已由独立采样器
#     (auto/sampler.py, independent=True 自跑取 trace) 解除 ⇒ 不再需要第二份驱动。

# ══════════════════════════════════════════════════════════════════════

def _prefetch_bars(codes, days, logger=None):
    """全市场日线**批量**预取 → {code: bars}; 关闭/失败返回 {} (调用方回落逐票)。"""
    if str(os.getenv("QD_SCAN_BATCH", "1")).strip() == "0":
        return {}
    if not codes:
        return {}
    # 2026-10-06: 改走**滑动窗口缓存** (window_cache) —— 每天只拉窗口尾部新增的
    # ~5236 行, 163 万行历史不再重复取数 (实测 17.1s → 数秒)。等价性是构造性的:
    # 同一 _window_bounds 窗口 + 同一 unadj_to_qfq, 且除权会改写历史 ⇒ 每票存复权
    # 因子指纹, 指纹变则整票全量重拉。缓存不可用/任何异常 ⇒ 回落全量 (不静默降级)。
    try:
        from app.market_cn.auto.core.data.window_cache import load_windows
        t0 = time.time()
        got = load_windows(list(codes), days=days, logger_=logger) or {}
        if logger:
            logger.info("[prefetch] 滑动取数 %d/%d 票 (days=%d, %.1fs)",
                        len(got), len(codes), days, time.time() - t0)
        return got
    except Exception as e:                      # 取数降级不能拖垮扫描: 回落批量全量
        if logger:
            logger.warning("[prefetch] 滑动取数失败(%s) → 回落批量全量", e)
    try:
        from app.market_cn.auto.core.data.kline import fetch_klines_batch
        t0 = time.time()
        got = fetch_klines_batch(list(codes), days=days, as_of=None) or {}
        if logger:
            logger.info("[prefetch] 批量取数 %d/%d 票 (days=%d, %.1fs)",
                        len(got), len(codes), days, time.time() - t0)
        return got
    except Exception as e:                      # 取数降级不能拖垮扫描: 回落逐票
        if logger:
            logger.warning("[prefetch] 批量取数失败(%s) → 回落逐票", e)
        return {}


def _prewarm_pools(active, bars_by_code, target, logger=None):
    """给**声明了** prewarm 的策略一次建好横截面池 (声明制: 不硬编码策略 key)。

    不做这件事的代价 (实测): g56.scan_signals 内部 `_ensure_pool_daily(pool_target)`
    **不传 bars_batch** ⇒ 走逐票 `hub.daily(code, 200, as_of=)` 全市场 ⇒
    **5224 次单票 SQL / 25.9s**, 而批量路径只需 3.9s 计算 (取数已被 _prefetch_bars 覆盖)。

    ⚠ 锚必须对齐: g56 用 `str(bars[-1]["time"])[:10]` 当池锚, 而进入判定的票末根
     恒 == target ⇒ 这里用 target 预热即可命中; 若预热锚与判定锚不同 ⇒ 单槽缓存
     失效 ⇒ 全市场池被反复重建 (26s/次 thrash)。
     该前提由主循环 `_run_scan_locked` 的**单一不变量** (`bars[-1]["time"] != target
     即 continue`) 保证 —— ★ 2026-10-07 前此不变量**未生效**: 只有"原始末根早于
     target"被跳过, 截断后不复检 ⇒ target 日无 bar 的票带着 target-N 的锚进入判定
     ⇒ 锚漂移 + 该票被"不含自己"的池评估 (百分位失真)。今后改动本预热的前提时,
     务必回主循环核对那条不变量是否还在。
    """
    if not bars_by_code:
        return
    try:
        from app.market_cn.auto.core.data.kline import window_start
    except Exception:
        return
    lo = str(window_start(200, target))[:10]
    hi = str(target)[:10]
    pool_bars = {}
    for c, bs in bars_by_code.items():
        sl = [b for b in bs if lo <= str(b["time"])[:10] <= hi]
        if sl:
            pool_bars[c] = sl
    if not pool_bars:
        return
    for key, strat in active.items():
        pw = getattr(strat, "prewarm", None)
        if pw is None:
            continue
        try:
            t0 = time.time()
            pw(pool_bars, hi)
            if logger:
                logger.info("[prewarm] %s 横截面池 %d 票 (%.1fs)",
                            key, len(pool_bars), time.time() - t0)
        except Exception as e:                  # 预热失败不应阻断扫描
            if logger:
                logger.warning("[prewarm] %s 失败(%s) → 池改由逐票路径自建", key, e)


# ================================================================
# 展示归一 (2026-09-11): 同族版本链去重, 高版本优先
# 背景: break_v2 ⊆ break 严格子集, 并行扫描同 (code, style) 双版本重复落库,
# 前端列表/自选组出现重复行。落库前按 (code, family, style) 归一, family 内
# 取 family_version 最高版本; 跨族 (dragon vs break) 与无重叠低版本信号照常
# 落库, v1 实盘台账只在真正重叠处被高版本替代。
# 版本身份声明 (2026-09-11 改版): 策略类属性 family/family_version 声明默认
# (break_v2: family="break", family_version=2), config.json
# strategies.<key>.family/family_version 可覆盖 — 加 break_v3 只需在插件里
# 声明 family="break", family_version=3, 扫描器零改动 (版本号自动识别)。
# ================================================================


def _family_maps():
    """全注册表 {key: (family, version)} (每轮现取; config mtime 缓存兜底)。"""
    from app.market_cn.auto import strategies as strat_reg
    return {k: (strat_reg.family_of(k), strat_reg.family_version(k))
            for k in strat_reg.all_strategies()}


def _dedupe_family(rows):
    """同族版本链去重: 同 (code, family, style) 取 family_version 最高者。"""
    if not rows:
        return rows
    fmap = _family_maps()
    best = {}
    for r in rows:
        fam, ver = fmap.get(r["strategy"], (r["strategy"], 1))
        k = (r["code"], fam, r.get("style"))
        cur = best.get(k)
        if cur is None or ver > cur[0]:
            best[k] = (ver, r)
    out = []
    for r in rows:
        fam, _ver = fmap.get(r["strategy"], (r["strategy"], 1))
        if best[(r["code"], fam, r.get("style"))][1] is r:
            out.append(r)
    return out


# ================================================================
# 数据就绪检测
# ================================================================

def _target_date():
    from app.utils.trading_calendar import last_finish_trading_day
    return last_finish_trading_day()


def _data_ready(target: str) -> bool:
    """参考股 (000001) 最新 1D bar 是否已到 target 日。"""
    bars = fetch_kline_db("000001", days=20)
    return bool(bars) and bars[-1]["time"] >= target


def _anchor_idx(bars, sig, strat):
    """U1~U4 锚定日索引: 'signal'=末根bar; 'limit_up'=信号 extra lu_date, 兜底最近涨停日。

    run_scan(盘后) 与 run_scan_knife(盘中窗口) 共用 —— 两处必须同源, 否则同一策略在
    两条路径上的 U1~U4 口径会分叉 (逐笔等价性以实盘路径为基准)。
    """
    from app.market_cn.auto.core.market import get_board_type, is_limit_up
    n = len(bars)
    if strat.prefilter_anchor == "limit_up":
        lu_date = (sig.extra or {}).get("lu_date")
        if lu_date:
            j = next((j for j, b in enumerate(bars) if b["time"] == lu_date), None)
            if j is not None:
                return j
        board_type = get_board_type(sig.code)
        for j in range(n - 1, 0, -1):
            if is_limit_up(bars[j]["close"], bars[j - 1]["close"], board_type):
                return j
        return None
    return n - 1


def apply_unified_prefilter(sigs, bars, code, code_info, strat):
    """U1~U4 应用循环 (2026-09-26 P0-4 唯一实现)。

    对每条 Signal 取锚点 → unified_prefilter; use_unified_prefilter=False 的策略
    原样放行 (knife/tail/g56 与回测口径一致)。run_scan / run_scan_knife /
    rebuild / core.backtest 四处共用, 改过滤逻辑只改这里。

    Returns:
        (kept, last_fails): kept=通过的 Signal 列表; last_fails=最后一次失败明细
        (供探针 stage=prefilter 归因; 无失败则 None)。
    """
    if not sigs:
        return [], None
    if not getattr(strat, "use_unified_prefilter", True):
        return list(sigs), None
    from app.market_cn.auto.core.filters import unified_prefilter
    kept = []
    last_u_fails = None
    for s in sigs:
        idx = _anchor_idx(bars, s, strat)
        if idx is None:
            continue
        ok, fails = unified_prefilter(bars, idx, code, code_info)
        if ok:
            kept.append(s)
        else:
            last_u_fails = fails
    return kept, last_u_fails


def finalize_signal_rows(rows, logger=None, env_mode=None):
    """落库前归一: 同族去重 → (策略层环境门) → daily_limit 截断。

    2026-09-28: 环境门**不挡主干** — 只作用于 market_env="trend" 的策略;
    counter/off 策略在弱市照常出信号 (反市场策略弱市可能更优)。

    Args:
        rows: store.signal_row 产出的 dict 列表
        logger: 可选
        env_mode: 大盘环境档 full|reduce|halt (由 run_scan 传入)

    Returns:
        list[dict]
    """
    if not rows:
        return []
    rows = _dedupe_family(rows)
    from app.market_cn.auto import strategies as strat_reg
    capped = []
    keys = []
    for r in rows:
        k = r["strategy"]
        if k not in keys:
            keys.append(k)
    for key in keys:
        grp = [r for r in rows if r["strategy"] == key]
        # 策略层环境门 (仅 trend 策略)
        mode_str = env_mode or "full"
        try:
            me = strat_reg.market_env_of(key) or "off"
        except Exception:
            me = "off"
        if me == "trend" and mode_str == "halt":
            if logger is not None:
                logger.info("[postfilter] %s trend×env=halt → 丢弃 %d 笔",
                            key, len(grp))
            continue
        cap = strat_reg.daily_limit(key)
        if me == "trend" and mode_str == "reduce":
            try:
                from app.market_cn.auto.core.market_env import apply_env_to_limit
                cap2 = apply_env_to_limit(cap, "reduce")
                if logger is not None and cap2 != cap:
                    logger.info("[postfilter] %s trend×reduce: limit %s → %s",
                                key, cap, cap2)
                cap = cap2
            except Exception:
                pass
        if cap and len(grp) > cap:
            if logger is not None:
                logger.info("[postfilter] %s 信号 %d 笔超限额, 截断至 %d (score降序)",
                            key, len(grp), cap)
            grp = sorted(grp, key=lambda r: r["score"], reverse=True)[:cap]
        capped.extend(grp)
    return capped


# ================================================================
# 主扫描
# ================================================================

def run_scan(days=320, wait_data=True, max_wait_sec=3600, keys=None, target=None):
    """盘后全市场扫描 (Phase 3: 注册表分发)。带跨调用方互斥, 抢不到返回 busy。

    Args:
        target: 交易日 (默认 last_finish_trading_day)。调度侧应传入自己用于
            完成标记的同一 target, 避免 15:00 翻转边界「标了旧日、扫了新日」。
    """
    with _scan_mutex() as _got:
        if not _got:
            logger.warning("[dragon_scan] 他人正在全市场扫描, 本次跳过 (keys=%s)", keys)
            return {"status": "busy", "target": target or _target_date(),
                    "keys": list(keys or [])}
        return _run_scan_locked(days=days, wait_data=wait_data,
                                max_wait_sec=max_wait_sec, keys=keys, target=target)


def _run_scan_locked(days=320, wait_data=True, max_wait_sec=3600, keys=None, target=None):
    """已持有 _scan_mutex 的扫描主体。返回摘要 dict。成功路径写 daily_scan@* 完成标记。

    - trade_date = target 或 last_finish_trading_day()
    - 遍历注册表中 enabled 且 kind=daily_close 的策略 (keys 非空则只跑该子集,
      事件驱动分发用: 不同策略 after_events 就绪时刻不同, 分批触发), 统一:
      判定 → U1~U4 预过滤 (锚点=策略 prefilter_anchor) → daily_limit 截断(score降序)
      → 标准化行落库
    - 组对账 (活跃集不变时无操作, 防漂移)
    """
    from app.market_cn.auto import store
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.core.filters import unified_prefilter
    from app.market_cn.auto.core.market import is_limit_up, get_board_type

    strat_reg.autodiscover()
    want = set(keys) if keys else None
    active = {k: s for k, s in strat_reg.all_strategies().items()
              if strat_reg.is_enabled(k) and s.scan_spec.kind == "daily_close"
              and (want is None or k in want)}
    if want is not None and not active:
        logger.info("[dragon_scan] keys=%s 无启用 daily_close 策略, 跳过", sorted(want))
        return {"status": "no_active_strategy", "keys": sorted(want), "target": _target_date()}
    logger.info("[dragon_scan] 活跃策略: %s", sorted(active))

    # P5-③ (2026-10-07) **写路径切换点**: `present_persist.enabled` 一个开关决定行从哪来。
    #   false (缺省) = 旧 writer —— 判定循环内逐票 `scan_days` + `signal_row`, 逐字保留,
    #                是**迁移期单点回滚位** (回滚 = 翻回 false), P6 随残余一并删除。
    #   true        = 新 writer —— `present_daily.persist_days` **转主线** (fold 是唯一判定;
    #                判定期失败**按策略退回直判并打 ERROR**, 不静默吞 —— 静默的部分写入
    #                会连带把当日 watch_pending purge 掉), 行源改为当日 `Record.ready`;
    #                后处理链 (U1~U4 → finalize) 与 DB 变异 (`upsert_scan_signals`) 完全照旧
    #                ⇒ 门禁「signals 零差异」可直接比。
    from app.market_cn.auto import present_daily
    WRITER = strat_reg.scan_writer()
    logger.info("[dragon_scan] 行源 = %s (scan_writer=%s, warmup=%d 日)",
                {"scan_day": "门表 scan_day", "record": "折叠 Record.ready",
                 "scan": "scan 直判(回滚位)"}[WRITER],
                WRITER, present_daily.warmup_days())

    # M1 实盘采集 (2026-09-11): 每策略一份探针存档, 判定落点非空的 (code,day) 产 sample。
    # 2026-10-07 (P5 前置): 采集**剥离**为独立采样器 (auto/sampler.py) —— 它自跑
    #   `scan_signals(probe=)` 取 trace, 与判定走哪条路径无关 ⇒ 两条 writer 下采样同一份实现。
    from app.market_cn.auto.sampler import LiveSampler
    sampler = LiveSampler(active, strat_reg.params_override)

    store.ensure_tables()
    target = target or _target_date()

    # 数据就绪等待 (仿 post_market_batch)
    if wait_data:
        waited = 0
        while not _data_ready(target):
            if waited >= max_wait_sec:
                logger.warning("[dragon_scan] 数据未就绪, 放弃本次 (target=%s)", target)
                return {"status": "data_not_ready", "target": target}
            # 持有期间每轮续约: 不续 ⇒ 等到 900s 时他人可回收本锁 ⇒ 并发 run_scan。
            if _HELD_LOCK_HOLDER:
                _db_touch_scan_lock(_HELD_LOCK_HOLDER)
            time.sleep(300)
            waited += 300
        logger.info("[dragon_scan] 数据就绪 (target=%s)", target)

    codes = all_codes()
    try:
        stock_info = _stock_info()
    except Exception as e:
        logger.warning("[dragon_scan] stock_basic_info 加载失败(%s), 换手/市值过滤降级", e)
        stock_info = {}

    # 批量预取 + 横截面池预热 (2026-10-05): 逐票取数 5236 次 SQL ⇒ 少数几次往返;
    # g56 池改由批量结果切片建, 不再逐票 hub.daily 全市场。两者失败都自动回落原路径。
    bars_by_code = _prefetch_bars(codes, days, logger)
    _prewarm_pools(active, bars_by_code, target, logger)

    rows = []
    t0 = time.time()
    # ---- 取数收敛 (两条 writer 共用) ----
    # ⇒ **唯一不变量: 序列末根必须是 target 日**。收敛口径 = `kline.asof_bars`
    #   (「今天这票算不算有数据」的**唯一实现**; 判定循环与切片落盘 `present_daily`
    #   共用同一函数 —— 各写一份必漂, 漏检那一侧产错日幽灵)。
    #   2026-09-29 审计修复 (P1) 只做了一半: `elif bars[-1] < target: continue`
    #   仅挡"原始末根早于 target"; 2026-10-07 补齐另一半 ——
    #    该股 **target 日无 bar** (回填缺口 / 停牌跨过 target 但后面还有更晚 bar) 时,
    #   截断让末根退到 target-N 且不再复检 ⇒ 在旧 bar 上判定, 产出
    #    trade_date=target / 判定日=T-N 的**错日幽灵信号**, 恰是声称已修的 D+1 白天补扫场景。
    #    连锁: 该票 g56 池锚 = target-N ≠ 预热锚 target ⇒ 单槽 _POOL 反复重建
    #    (26s/次 thrash); 且被"不含自己"的横截面池判定 ⇒ 百分位失真。
    #   残留限度不变: 被跳过的股若本轮后仍不回填, 不会补扫 (宜由数据就绪闸门保证)。
    # ⚠ 回写: 兜底逐票取的 bars 也进 bars_by_code ⇒ 判定与切片落盘是**同一份对象**
    #   (present_daily 只按 as-of 再截到更早日期, 不再取一次数 —— 取两次会漂)。
    for i, code in enumerate(codes):
        bars = bars_by_code.get(code)
        if bars is None:                    # 批量未覆盖 (新股/停牌/取数缺失) → 逐票兜底
            bars = fetch_kline_db(code, days)
        bars = asof_bars(bars, target, ASOF_MIN_BARS)
        if bars is None:
            bars_by_code.pop(code, None)
            continue
        bars_by_code[code] = bars
        if (i + 1) % 1000 == 0:
            logger.info("[dragon_scan] 取数收敛 %d/%d (%.0fs)",
                        i + 1, len(codes), time.time() - t0)

    err_by_key: dict[str, int] = {}      # {策略: 本轮判定异常票数} → 见下方 finally 汇总
    try:
        if WRITER == "scan_day":
            rows, err_by_key = _rows_by_scan_day(active, bars_by_code, target, stock_info, sampler)
        elif WRITER == "record":
            rows = _rows_by_record(active, bars_by_code, target, stock_info, sampler)
        else:
            rows, err_by_key = _rows_by_scan(active, bars_by_code, target, stock_info, sampler)
    finally:
        sampler.close()
        if err_by_key:
            # 汇总必有: 降级 DEBUG 之后仍留一条可告警的痕迹 ⇒ 单策略异常不再是暗账
            logger.error("[dragon_scan] 单票判定异常共 %d 次 (该策略信号可能缺失): %s",
                         sum(err_by_key.values()),
                         ", ".join(f"{k}={v}" for k, v in sorted(err_by_key.items())))

    # 2026-09-28 A: 大盘资金流环境门 —— **先于限额截断** (reduce 砍名额)
    env_info = {}
    try:
        from app.market_cn.auto.core.market_env import market_flow_gate
        env_info = market_flow_gate(as_of=target)
        logger.info("[dragon_scan] 环境门 mode=%s | %s",
                    env_info.get("mode"), env_info.get("reason"))
    except Exception as e:
        logger.warning("[dragon_scan] 环境门失败(忽略): %s", e)

    # 展示归一 + 每日限额 (env=reduce → 名额减半)
    _n_raw = len(rows)
    rows = finalize_signal_rows(rows, logger=logger,
                                env_mode=env_info.get("mode"))
    if len(rows) != _n_raw:
        logger.info("[dragon_scan] 同族去重+限额截断: %d → %d", _n_raw, len(rows))

    # M12: 显式声明本批策略范围 —— 前置 DELETE 只清这些策略的 watch_pending
    # 2026-09-28 B: 个股 LHB/资金流 —— **纸面记录**, 不改落库
    if rows:
        try:
            from app.market_cn.auto.core.stock_env import stock_event_gate
            n_red = n_halt = 0
            for row in rows[:20]:
                g = stock_event_gate(row.get("code"), date=target)
                if g.get("mode") == "halt":
                    n_halt += 1
                elif g.get("mode") == "reduce":
                    n_red += 1
            logger.info("[dragon_scan] 个股事件门(纸面) halt=%d reduce=%d / %d",
                        n_halt, n_red, min(20, len(rows)))
        except Exception as e:
            logger.warning("[dragon_scan] 个股事件门失败(忽略): %s", e)

    # 2026-09-28: 环境门只作用于 market_env="trend" 策略 (见 finalize_signal_rows);
    # counter/off 策略在弱市仍出信号 — 不在主干全局拦截。
    result = store.upsert_scan_signals(target, rows, strategies=tuple(active))
    store.sync_watchlist_group(store.get_active_signals())
    store.cleanup_old(days=15)
    logger.info("[dragon_scan] 完成: 全市场 %d 只, 信号 %d 笔 (%.0fs) env=%s",
                len(codes), result.get("written", 0), time.time() - t0,
                env_info.get("mode", "-"))

    # P1-2: 任何调用方 (调度 daily_scan / startup 补扫 / CLI) 成功后同口径落完成标记
    _mark_daily_scanned(active.keys(), target)
    return {"status": "ok", "target": target, "codes": len(codes),
            "signals": result.get("written", 0), "env": env_info}


def _rows_by_scan(active, bars_by_code, target, stock_info, sampler):
    """**旧 writer** (迁移期回滚位, P6 删): 循环内逐票 `scan_days` 判定 → `signal_row`。

    即 P5-③ 之前的生产实现, 逐字保留 —— 它是回滚时唯一要回到的那条路。
    返回 `(rows, err_by_key)`。
    """
    from app.market_cn.auto import store, strategies as strat_reg

    rows = []
    err_by_key: dict[str, int] = {}
    for i, (code, bars) in enumerate(bars_by_code.items()):
        name = (stock_info.get(code) or {}).get("name", "")
        for key, strat in active.items():
            try:
                # 判定委托 `scan_days` 折叠内核 (驱动路径唯一)。
                # lo=hi=target ⇒ 只判当日, 语义 == scan_signals(bars[:target+1])。
                sigs = strat.scan_days(bars, code, lo_date=target, hi_date=target,
                                       **strat_reg.params_override(key))
            except Exception as e:
                # 2026-10-07 (P2): 原为 DEBUG —— 生产 INFO 级别下**策略整静默**
                #   (无一行日志、无计数、无告警, 信号凭空少一批), 是本项目点名的
                #   "静默断链"同构。改: 每策略前 3 次打 WARNING (带 code/原因, 便于
                #   定位), 之后降级 DEBUG 防日志洪水 (单策略 bug 可命中全 5236 票);
                #   无论多少, 末尾必有 **一行汇总** (ERROR) —— 兜住"降级后没人看"。
                _n = err_by_key.get(key, 0) + 1
                err_by_key[key] = _n
                if _n <= 3:
                    logger.warning("[dragon_scan] %s %s 判定异常(%s), 跳过该票", code, key, e)
                else:
                    logger.debug("[dragon_scan] %s %s 判定异常(%s), 跳过该票", code, key, e)
                continue
            # U1~U4 统一预过滤 (锚点由策略声明; 易错点: 龙回头不能用缩量信号日评估, 会误杀)
            kept, _ = apply_unified_prefilter(sigs, bars, code, stock_info.get(code), strat)
            sampler.observe(key, strat, code, bars, stock_info)
            rows.extend(store.signal_row(key, s, name) for s in kept)
        if (i + 1) % 500 == 0:
            logger.info("[dragon_scan] 判定 %d/%d, 信号 %d", i + 1, len(bars_by_code), len(rows))
    return rows, err_by_key


def _rows_by_scan_day(active, bars_by_code, target, stock_info, sampler):
    """**门表 writer** (P2): 判定走门表引擎 `scan_day`（门表唯一规则源）。

    行源 = `evaluate.scan_day`（门表单日 → Signal）。后处理链（U1~U4 → signal_row）照旧。
    三个日线策略（break/g56/dragon）均已注册 `_scan_one`；v1/relay3 已于 2026-10-09 退役。
    """
    from app.market_cn.auto import store
    from app.market_cn.auto.core.runtime.evaluate import load_strategy, scan_day

    rows = []
    err_by_key: dict[str, int] = {}
    specs = {key: load_strategy(key) for key in active}
    for i, (code, bars) in enumerate(bars_by_code.items()):
        name = (stock_info.get(code) or {}).get("name", "")
        for key, strat in active.items():
            try:
                sigs = scan_day(specs[key], bars, code, target,
                                stock_info=stock_info.get(code))
            except Exception as e:
                _n = err_by_key.get(key, 0) + 1
                err_by_key[key] = _n
                if _n <= 3:
                    logger.warning("[dragon_scan] %s %s 判定异常(%s), 跳过该票", code, key, e)
                else:
                    logger.debug("[dragon_scan] %s %s 判定异常(%s), 跳过该票", code, key, e)
                continue
            kept, _ = apply_unified_prefilter(sigs, bars, code, stock_info.get(code), strat)
            sampler.observe(key, strat, code, bars, stock_info)
            rows.extend(store.signal_row(key, s, name) for s in kept)
        if (i + 1) % 500 == 0:
            logger.info("[dragon_scan] 判定 %d/%d, 信号 %d", i + 1, len(bars_by_code), len(rows))
    return rows, err_by_key


def _rows_by_record(active, bars_by_code, target, stock_info, sampler, root=None):
    """**新 writer** (P5-③): fold 是唯一判定 (由 `present_daily.persist_days` 转主线完成),
    行源 = 当日 `Record.ready`。**只有"行从哪来"变了** —— U1~U4 / `signal_row` 照旧。

    Args:
        root: 切片根 (缺省 = 生产 `_paths.PRESENT_STATE_DIR`); 测试用临时根注入。

    ⚠ 判定期异常 = **整日整策略失败**（`advance_all` 不在票级兜异常，与旧路径逐票 try 不等价）。
      而 `upsert_scan_signals` 是「先 DELETE 当日 watch_pending 再 INSERT」⇒ 拿残缺行去写会把
      没判出来的那部分信号**删掉**。故失败策略**整体退回直判**（与「无折叠契约」同一条出口）：
      既不写残缺、也不让整个扫描挂掉（那会一份信号都不落）—— 两条都是不可接受的结局。
      回退必打 ERROR，不是静默降级。
    """
    from app.market_cn.auto import present_daily, store, strategies as strat_reg

    pstat = present_daily.persist_days(
        active, [target], bars_by_code, root=root,
        warmup=present_daily.warmup_days(), logger_=logger)
    logger.info("[dragon_scan] 切片落盘完成: root=%s 日期=%s 跳过=%s 明细=%s",
                pstat["root"], pstat["dates"], pstat["skipped"] or "-",
                {k: {"天": v["days"], "有事件": v["advanced"], "暖机": v["warmed"],
                     "错误": len(v["errors"])} for k, v in pstat["strategies"].items()})
    #: 本策略行源不可用（无折叠契约 / 切片推进失败）⇒ 走下面的逐票直判分支
    direct = {k: ("无折叠契约" if k in pstat["skipped"] else "切片推进失败")
              for k in active if k in pstat["skipped"]
              or (pstat["strategies"].get(k) or {}).get("errors")}
    if direct:
        logger.error("[dragon_scan] 行源退回 scan 直判 (不静默): %s", direct)

    rows = []
    for key, strat in active.items():
        params = strat_reg.params_override(key)
        if key in direct:
            for code, bars in bars_by_code.items():
                try:
                    sigs = strat.scan_days(bars, code, lo_date=target, hi_date=target, **params)
                except Exception as e:              # noqa: BLE001 - 与旧路径同级容忍
                    logger.warning("[dragon_scan] %s %s 判定异常(%s), 跳过该票", code, key, e)
                    continue
                kept, _ = apply_unified_prefilter(
                    sigs, bars, code, stock_info.get(code), strat)
                sampler.observe(key, strat, code, bars, stock_info)
                rows.extend(store.signal_row(key, s, (stock_info.get(code) or {}).get("name", ""))
                            for s in kept)
            continue
        ready = pstat["ready"].get(key) or {}
        for code, bars in bars_by_code.items():
            sampler.observe(key, strat, code, bars, stock_info)
            sigs = ready.get(code)
            if not sigs:
                continue
            kept, _ = apply_unified_prefilter(sigs, bars, code, stock_info.get(code), strat)
            name = (stock_info.get(code) or {}).get("name", "")
            rows.extend(store.signal_row(key, s, name) for s in kept)
    return rows


def run_scan_knife(max_wait_sec=2400, wait_data=True, keys=None):
    """盘中窗口扫描 (kind=intraday_window 策略, 由注册表动态收集: 现为 knife_catch / tail_oversold)。

    调度: scheduler Task "knife_scan" —— **多触发点** (2026-09-29 重做):
      sched 事实源 (all_schedules) 给每个启用策略排一个触发点, 相邻 <=30min 合并一批:
        14:30 → knife_catch + tail_oversold (尾盘批)
      keys = 本批策略; None = 全量 (兼容手动 CLI 与旧调用)。
      只跑本批 ⇒ purge_buy_today / daily_limit 的作用域也只限本批
      (_scan_cycle 按 cycle_strats.keys() 限定, 不会误伤其它策略的持仓行)。
    为什么分批: 旧实现一天只触发一次、时刻取 min(全部策略首拍) ⇒ 早盘 lead_chase
      09:40 把尾盘组拉到 09:40 ⇒ 死等 start_hm=14:50 ⇒ 40min 超时放弃且
      once_per_day 已标记 ⇒ 当日 knife_catch/tail_oversold 全天 0 信号
      (09-28 实证 13:39:28「等待超时, 放弃本次」)。
    流程:
      1. 等待到滚动起点 (有 rolling_preview 策略时取最早者, 现为 14:50; 否则 14:56 保持旧行为)
      2. 滚动判定 (14:50~14:59): 每分钟一轮; 14:50 起触发即买入 (已定价行不被冲掉)
         (幂等 upsert + 本轮落选 buy_today 清理), 前端自选组实时刷新, 用户提前准备
      3. 15:00 收口: 再跑一轮全量; 仅清 entry_price 为空的预览行
      单轮: 全市场最新快照 → 策略 intraday_shortlist 必要条件预筛 →
            候选股补拉当日快照序列+日线 → scan_signals 完整判定 →
            U1~U4 统一预过滤 (use_unified_prefilter=True 的策略, 锚点 prefilter_anchor) →
            落库 state=buy_today, entry_date/price=快照价, 止损价
    幂等: upsert ON CONFLICT (trade_date, strategy, code, entry_style)。
    手动: python -m ...scan --knife [--no-wait]
    """
    from app.market_cn.auto import store
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.core.filters import unified_prefilter

    strat_reg.autodiscover()
    active = {k: s for k, s in strat_reg.all_strategies().items()
              if strat_reg.is_enabled(k) and s.scan_spec.kind == "intraday_window"}
    if keys is not None:
        active = {k: v for k, v in active.items() if k in tuple(keys)}
    if not active:
        return {"status": "no_intraday_strategy", "keys": list(keys or [])}
    logger.info("[knife_scan] 本批策略: %s", ",".join(sorted(active)))

    store.ensure_tables()
    from app.market_cn.auto.monitor import (
        latest_snapshot, fetch_day_snapshots, _today,
    )

    # 滚动预览策略 (14:50 起每分钟重判; 无则等待起点=14:56, 与旧行为一致)
    preview = {k: s for k, s in active.items() if getattr(s, "rolling_preview", False)}
    # 起点与 scheduler 触发同源: config.json schedule 段覆盖优先 (resolve_schedule),
    # ScanSpec 默认兜底 — 否则改 config 窗口后两处事实源分叉
    from app.market_cn.auto.sched import resolve_schedule
    starts = []
    ends = []
    for k in preview:
        decl = resolve_schedule(k)
        if decl and decl.get("windows"):
            starts.append(decl["windows"][0])
        else:
            starts.append(active[k].scan_spec.windows[0])
    if starts:
        start_hm = min(starts)
    elif keys is not None:
        # 分批触发且本批无预览策略 ⇒ 等到**本批窗口末拍**再终审。
        # (早盘单点策略 windows=("09:40","09:40") ⇒ 末拍 09:40 ⇒ 立即执行;
        #  若沿用旧 fallback "14:56" 会从 09:40 死等到 14:56 再 40min 超时放弃 ——
        #  这正是 09-28/09-29 的故障形态)
        for k in active:
            decl = resolve_schedule(k)
            if decl and decl.get("windows"):
                ends.append(decl["windows"][1])
            else:
                ends.append(active[k].scan_spec.windows[1])
        start_hm = max(ends, default="14:56")
    else:
        start_hm = "14:56"
    logger.info("[knife_scan] 等待目标 start_hm=%s (预览策略=%s)",
                start_hm, ",".join(sorted(preview)) or "无")

    # ST / 北交所 通用排除 (knife 回测口径)
    try:
        stock_info = _stock_info()
    except Exception:
        stock_info = {}

    def _st_ok(code):
        nm = (stock_info.get(code) or {}).get("name", "") or ""
        return "ST" not in nm.upper()

    def _mkt_gain(snaps):
        """市场均涨幅 (as-of 最新快照; tail_oversold 仅记录不门控, knife 用作门控)。"""
        gains = []
        for s in snaps.values():
            try:
                last, pc = float(s.get("last") or 0), float(s.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last > 0 and pc > 0:
                gains.append((last / pc - 1) * 100)
        return sum(gains) / len(gains) if gains else 0.0

    def _scan_cycle(cycle_strats, snaps, preview_cycle=False):
        """一轮完整判定+落库。preview_cycle=True 时清该批策略本轮落选的 buy_today 行。"""
        today = _today()
        mkt = _mkt_gain(snaps)
        rows = []
        for key, strat in cycle_strats.items():
            params = strat_reg.params_override(key)
            shortlist = strat.intraday_shortlist(snaps, mkt, **params)
            logger.info("[knife_scan] %s 便宜预筛: %d/%d%s (mkt=%.2f%%)",
                        key, len(shortlist), len(snaps),
                        " [预览]" if preview_cycle else "", mkt)
            # 批量预取本批短名单日线 (盘中延迟敏感: 原逐票 = N 次串行往返)
            bars60 = _prefetch_bars(list(shortlist.keys()), 60, logger)
            for code, snap in shortlist.items():
                if not _st_ok(code):
                    continue
                name = (stock_info.get(code) or {}).get("name", "")
                bars = bars60.get(code)
                if bars is None:
                    bars = fetch_kline_db(code, days=60)
                series = fetch_day_snapshots([code]).get(code) or []
                try:
                    sigs = strat.scan_signals(bars, code, ctx={
                        "latest": snap, "series": series, "mkt_gain": mkt,
                    }, **params)
                except Exception as e:
                    logger.warning("[knife_scan] %s %s 判定异常(已跳过该股): %s",
                                   code, key, e)
                    continue
                # U1~U4 统一预过滤 (与盘后 run_scan 同源; 锚点由策略 prefilter_anchor 声明)。
                # 只有声明 use_unified_prefilter=True 的策略走本段 —— knife/tail 声明 False,
                # 行为逐字不变; 否则盘中实盘会缺 U1~U4 而与其回测口径分叉。
                sigs, _u_fails = apply_unified_prefilter(
                    sigs, bars, code, stock_info.get(code), strat)
                for s in sigs:
                    row = store.signal_row(key, s, name)
                    row["state"] = getattr(strat, "signal_state", "watch_pending")
                    if row["state"] == "buy_today":
                        row["entry_date"] = today
                        row["entry_price"] = float(s.price or 0) or None
                        row["stop_price"] = strat.initial_stop(code, float(s.price or 0))
                    rows.append(row)
            # daily_limit (config.json; 0=不截断 — 用户裁定: 全拿优于Top3截断)
            # 2026-09-26 P0-4: 复用 finalize_signal_rows 的限额段语义 (本处只截当前策略,
            # 不去重 — 盘中窗口无同族版本链问题, 且 finalize 会误伤其它策略行)。
            grp = [r for r in rows if r["strategy"] == key]
            cap = strat_reg.daily_limit(key)
            if cap and len(grp) > cap:
                logger.info("[knife_scan] %s 信号 %d 笔超限额, 截断至 %d", key, len(grp), cap)
                rows = [r for r in rows if r["strategy"] != key] + \
                    sorted(grp, key=lambda r: r["score"], reverse=True)[:cap]
        # 滚动重判: 清掉本批策略上一轮命中本轮落选的 buy_today 行 (防残留误导);
        # 仅清 buy_today 态, 不碰 15:01 确认后已转移的 holding/exit 等状态
        result = store.upsert_scan_signals(
            today, rows, purge_buy_today=tuple(cycle_strats.keys()),
            strategies=tuple(cycle_strats.keys()))
        if not preview_cycle and not rows:
            logger.info("[knife_scan] 终审 0 笔 → purge 预览 buy_today (策略=%s); "
                        "预览曾命中的票不会留在库里",
                        ",".join(cycle_strats.keys()))
        store.sync_watchlist_group(store.get_active_signals())
        return result

    # 等待到滚动起点 (14:30 触发后预热等待; --no-wait 手动立即跑)
    if wait_data:
        deadline = time.time() + max_wait_sec
        while _now_hm_str() < start_hm:
            if time.time() > deadline:
                logger.warning("[knife_scan] 等待超时, 放弃本次")
                return {"status": "timeout"}
            time.sleep(30)

    today = _today()
    all_codes_list = all_codes()

    # ── 滚动预览: 14:50~14:59 每分钟一轮 (仅 preview 策略) — 观察窗 10 分钟 ──
    if wait_data and preview:
        while _now_hm_str() < "15:00":
            snaps = latest_snapshot(all_codes_list)
            if snaps:
                try:
                    r = _scan_cycle(preview, snaps, preview_cycle=True)
                    logger.info("[knife_scan] 预览轮完成: %s 信号 %d 笔",
                                ",".join(preview), r.get("written", 0))
                except Exception as e:
                    logger.warning("[knife_scan] 预览轮异常(下一轮重试): %s", e)
            # 对齐到下一整分钟
            time.sleep(max(5, 60 - time.time() % 60))

    # ── 终审: 15:00 后等待新鲜快照落地 (采集 60s 一拍, 一般 <=15s, 上限 45s) ──
    snaps = latest_snapshot(all_codes_list)
    if wait_data and preview and snaps:
        fresh_cut = f"{today} 15:00"
        deadline = time.time() + 45
        while time.time() < deadline:
            latest_ts = max((str(s.get("time") or "") for s in snaps.values()), default="")
            if latest_ts >= fresh_cut:
                break
            time.sleep(5)
            snaps = latest_snapshot(all_codes_list)
    if not snaps:
        logger.warning("[knife_scan] 无快照数据, 放弃")
        return {"status": "no_snapshot"}

    t0 = time.time()
    result = _scan_cycle(active, snaps)

    logger.info("[knife_scan] 完成: 快照 %d, 信号 %d 笔 (%.0fs) [策略=%s]",
                len(snaps), result.get("written", 0), time.time() - t0,
                ",".join(sorted(active)))
    return {"status": "ok", "target": today, "signals": result.get("written", 0),
            "mkt_gain": round(_mkt_gain(snaps), 3)}


def _now_hm_str():
    from app.market_cn.auto.monitor import _now_hm
    return _now_hm()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘后全市场扫描 (注册表分发, 手动)")
    parser.add_argument("--run", action="store_true", help="执行扫描")
    parser.add_argument("--days", type=int, default=320, help="向前取N个交易日")
    parser.add_argument("--no-wait", action="store_true", help="不等待数据就绪")
    parser.add_argument("--keys", default="", help="只跑指定 daily_close 策略, 逗号分隔 (默认全量)")
    parser.add_argument("--knife", action="store_true", help="执行盘中接刀扫描 (手动)")
    args = parser.parse_args()
    if args.run:
        keys = tuple(k.strip() for k in args.keys.split(",") if k.strip()) or None
        summary = run_scan(days=args.days, wait_data=not args.no_wait, keys=keys)
        print(summary)
    elif args.knife:
        summary = run_scan_knife(wait_data=not args.no_wait)
        print(summary)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
