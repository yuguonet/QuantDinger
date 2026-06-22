"""
scheduler 内存监控 — 定位 20GB 内存增长的来源

使用方法:
  1. 在 Flask 启动前 import 并 start:
     from scripts.mem_monitor import start_mem_monitor
     start_mem_monitor()
  2. 或者直接 python -m scripts.mem_monitor (独立运行)

监控维度:
  - 每 10 秒采样 RSS 内存
  - 每次 refresh_* 前后采样
  - 超过阈值自动 dump top 对象
"""

import tracemalloc
import threading
import time
import os
import logging
import psutil

logger = logging.getLogger(__name__)

# ── 配置 ──
_SNAPSHOT_INTERVAL = 10       # 秒
_RSS_ALERT_MB = 2000          # RSS 超过 2GB 告警
_RSS_ALERT_DELTA_MB = 500     # 10秒内增长超过 500MB 告警
_TOP_N = 20                   # dump top N 个分配点

_process = psutil.Process(os.getpid())
_monitoring = False
_last_rss_mb = 0


def _get_rss_mb() -> float:
    return _process.memory_info().rss / 1024 / 1024


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _dump_top_allocations():
    """dump 当前 top 内存分配点"""
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics('lineno')
    logger.error("=== [MEM] Top %d 内存分配 ===", _TOP_N)
    for s in stats[:_TOP_N]:
        logger.error("  %s: %s", _format_size(s.size), s)
    logger.error("=== [MEM] Top 按文件 ===")
    stats_file = snapshot.statistics('filename')
    for s in stats_file[:10]:
        logger.error("  %s: %s (%d blocks)", _format_size(s.size), s.filename, s.count)


def _monitor_loop():
    """后台监控线程"""
    global _last_rss_mb
    _last_rss_mb = _get_rss_mb()
    logger.info("[MEM] 内存监控启动, RSS=%.0fMB, 告警阈值=%dMB, 增量阈值=%dMB",
                _last_rss_mb, _RSS_ALERT_MB, _RSS_ALERT_DELTA_MB)

    while _monitoring:
        time.sleep(_SNAPSHOT_INTERVAL)
        rss_mb = _get_rss_mb()
        delta = rss_mb - _last_rss_mb

        # 增量告警
        if delta > _RSS_ALERT_DELTA_MB:
            logger.error("[MEM] ⚠️ 内存暴涨! RSS: %.0fMB → %.0fMB (+%.0fMB in %ds)",
                         _last_rss_mb, rss_mb, delta, _SNAPSHOT_INTERVAL)
            _dump_top_allocations()
        elif rss_mb > _RSS_ALERT_MB:
            logger.warning("[MEM] ⚠️ RSS 超限: %.0fMB (阈值 %dMB)", rss_mb, _RSS_ALERT_MB)
            _dump_top_allocations()
        elif delta > 50:
            logger.warning("[MEM] RSS 增长: %.0fMB → %.0fMB (+%.0fMB)",
                           _last_rss_mb, rss_mb, delta)

        _last_rss_mb = rss_mb


def start_mem_monitor():
    """启动内存监控"""
    global _monitoring
    tracemalloc.start(25)  # 25 帧回溯
    _monitoring = True
    t = threading.Thread(target=_monitor_loop, daemon=True, name="mem-monitor")
    t.start()
    logger.info("[MEM] tracemalloc + RSS 监控已启动")


def stop_mem_monitor():
    """停止内存监控"""
    global _monitoring
    _monitoring = False
    tracemalloc.stop()
    logger.info("[MEM] 内存监控已停止")


# ── 装饰器: 包装 refresh 函数，自动采样 ──

def trace_refresh(fn):
    """装饰器: 在 refresh 函数前后采样内存"""
    def wrapper(*args, **kwargs):
        rss_before = _get_rss_mb()
        snap_before = tracemalloc.take_snapshot()
        t0 = time.time()

        result = fn(*args, **kwargs)

        elapsed = time.time() - t0
        rss_after = _get_rss_mb()
        delta_rss = rss_after - rss_before

        if delta_rss > 10:  # 超过 10MB 才告警
            snap_after = tracemalloc.take_snapshot()
            stats = snap_after.compare_to(snap_before, 'lineno')
            logger.error("[MEM] %s: RSS +%.0fMB (%.0f→%.0f), 耗时%.1fs",
                         fn.__name__, delta_rss, rss_before, rss_after, elapsed)
            for s in stats[:5]:
                if s.size_diff > 0:
                    logger.error("  + %s: %s", _format_size(s.size_diff), s)
        elif delta_rss > 1:
            logger.info("[MEM] %s: RSS +%.1fMB, 耗时%.1fs",
                        fn.__name__, delta_rss, elapsed)

        return result
    return wrapper


# ── 独立运行 ──

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start_mem_monitor()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_mem_monitor()
