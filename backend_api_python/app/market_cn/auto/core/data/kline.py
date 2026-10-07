#!/usr/bin/env python3
"""K线数据加载 (auto/data) —— 从 dragon_scan.py 迁入 (Phase 1, 2026-09-07)

用途: 盘后扫描/盘中监控/手动工具共用的日K数据读取; 调用方一律经 hub.daily 取数,
     本模块是 hub 的内部实现层 (2026-09-10 起 fetch_stock_info_db 已归位 hub.stock_info)。
关键设计点:
  - fetch_kline_db: DB 1D + 前复权, 与 test_dragon.fetch_kline_db 同口径 (对数基准);
  - 加载失败返回空值 + debug 日志, 让上层全市场循环继续 (单股失败不拖垮扫描)。
易错点: basicinfo pool 返回元组行; circ_shares 为 0/None 时上层 U2/U3 自动跳过, 这里不补默认值。
"""
from __future__ import annotations

from datetime import timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _window_bounds(days=300, as_of=None):
    """取数窗口 (start, end) = [anchor - days*1.5 日历日, anchor + 1 日历日]。

    anchor = as_of (给了就锚在它) 否则 now。
    2026-09-28 审计 A1: 窗口原先**恒锚 now**, 于是 `hub.daily(code, days, as_of=历史日)`
    的 as_of 过滤会把窗口内的根**全部**裁掉 ⇒ 静默空集 (命中 tools/pool_check、
    strategies/lead_chase、g56.scan_signals 逐日枚举、tools/debug、tools/replay)。
    as_of=None 时与旧版逐字节一致 ⇒ 生产链行为不变。

    ⚠ 返回语义是"窗口内**全部**行", 不是"最后 days 根"。该约定被 `present/pipeline.py`
    的「300 窗口按 window_start(200) 切片 == fetch_kline_db(code, 200)」依赖,
    故**不可**改成尾部精确截断 (改了会让切片等价性破功)。
    """
    from datetime import datetime as _dt
    anchor = _dt.now()
    if as_of:
        try:
            anchor = _dt.strptime(str(as_of)[:10], "%Y-%m-%d")
        except ValueError:
            anchor = _dt.now()          # as_of 形态异常时退回旧行为 (保持宽松, 不抛)
    return ((anchor - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d"),
            (anchor + timedelta(days=1)).strftime("%Y-%m-%d"))


def fetch_kline_db(code, days=300, as_of=None):
    """从 DB 加载日K (前复权), 返回 list[dict] (time/open/high/low/close/volume)。

    as_of: 取数窗口锚点 + as-of 截断 (只保留 <= as_of 的 bar, 与 fetch_klines_batch
           同语义; 2026-09-29 审计修复: 原先仅靠 end=as_of+1天 裁剪, writer.query
           端点含等号, as_of+1 为交易日时会多一根"未来"bar, 与 batch 版口径不一致)。
    """
    start, end = _window_bounds(days, as_of)
    try:
        from app.utils.db_market import get_market_kline_writer
        from app.data_sources.provider.adjustment import unadj_to_qfq
        writer = get_market_kline_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        if as_of is not None:
            cutoff = str(as_of)[:10]
            data = [r for r in data if str(r["time"])[:10] <= cutoff]
        return unadj_to_qfq([{
            "time": str(r["time"])[:10],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
        } for r in data], code)
    except Exception as e:
        logger.debug("[data.kline] %s 加载失败: %s", code, e)
        return []


def fetch_klines_batch(codes, days=300, as_of=None, shards=6):
    """批量加载多票日线 (前复权) → {code: bars}。

    与逐票 `fetch_kline_db` **行内容完全一致** (同窗口/同 qfq/同 as-of 截断),差别在
    DB 取数由 N 次往返降为少数几次: `hub._query_batch_raw` 用 `symbol = ANY(...)` 一次取一批,
    并按 `shards` 把代码**分片并发**查询 (psycopg2 在 C 层释放 GIL, 分片真并行)。

    未加载到 (或加载失败) 的 code 不出现在返回 dict 中 → 调用方按空处理 (与
    fetch_kline_db 返回 [] 等价)。as_of 非空时**窗口锚在 as_of** 并只保留 ≤as_of 的 bar
    (与 `fetch_kline_db(code, days, as_of)` 逐行一致, 见 _window_bounds)。

    shards: 并发分片数 (<=1 = 单条 SQL)。分片只改变"哪条 SQL 取哪些代码", 合并后
    结果与单条完全一致 (各片代码互斥, 片内顺序保持)。
    """
    codes = [c for c in codes if c]
    if not codes:
        return {}
    start, end = _window_bounds(days, as_of)
    try:
        from app.data_sources.provider.adjustment import unadj_to_qfq
        from app.market_cn.auto.core.data.hub import _query_batch_raw
        if shards and shards > 1 and len(codes) >= shards * 64:
            from concurrent.futures import ThreadPoolExecutor
            chunks = [codes[i::shards] for i in range(shards)]
            with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
                parts = list(ex.map(
                    lambda cs: _query_batch_raw("CNStock", cs, "1D",
                                                start_time=start, end_time=end),
                    chunks))
            raw = {}
            for p in parts:
                raw.update(p)
        else:
            raw = _query_batch_raw("CNStock", codes, "1D",
                                   start_time=start, end_time=end)
    except Exception as e:
        logger.debug("[data.kline] 批量加载失败: %s", e)
        return {}
    a = str(as_of)[:10] if as_of else None
    out = {}
    for code, rows in raw.items():
        if not rows:
            continue
        # rows 为原始 tuple (time, o, h, l, c, v) —— 与 query() 列序一致; 组装成 bars dict
        # 后统一走 unadj_to_qfq (与逐票 fetch_kline_db 同一复权实现, 单一事实源)。
        bars = unadj_to_qfq([{
            "time": str(r[0])[:10],
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        } for r in rows], code)
        if a:
            bars = [b for b in bars if b["time"] <= a]
        if bars:
            out[code] = bars
    return out


def window_start(days=300, as_of=None):
    """加载窗口的日历下界 (fetch_kline_db / fetch_klines_batch 的 start)。

    `fetch_kline_db(code, days)` 的定义就是 "time ∈ [anchor - days*1.5, anchor + 1d]"
    (anchor=as_of 或 now), 因此 **days=200 的行集 == days=300 行集中 time ≥
    window_start(200, 同一 anchor) 的部分**。需要更短窗口派生量 (如 g56 横截面池的
    200 根) 时, 可据此从共享长窗口缓存**切片**得到, 免去再加载一遍全市场
    (切片与逐票同口径, 非近似)。

    传 as_of 时必须与取数方用同一个 as_of, 否则切片基准错位 (审计 A1 同源)。
    """
    return _window_bounds(days, as_of)[0]


#: 「判定用 as-of 序列」的最小根数（数据充分性门槛）。
#: **唯一来源** —— 判定循环 (`scan._run_scan_locked`) 与切片落盘 (`present_daily`)
#: 都显式引用本常量，禁各写一份 30（策略侧另有更严的窗口下界，与本门槛无关）。
ASOF_MIN_BARS = 30


def asof_bars(bars, target, min_bars):
    """把一票的日线收敛成「判定用的 as-of 序列」：截断到 ≤ target，且末根必须 == target。

    返回可用序列，或 None（缺票 / 过短 / **target 日无 bar**）。

    为什么必须有这个函数（而不是各调用方各写四行）：
      这是"今天这票算不算有数据"的**唯一口径**。判定循环 (`scan._run_scan_locked`) 与
      切片落盘 (`auto/present_daily`) 都要它 —— 两处各写一份，一侧漏掉末根复检就会
      带着 target-N 的旧 bar 进入判定，产出 **trade_date=target / 判定日=T-N 的错日
      幽灵**（2026-09-29 审计只修了一半，2026-10-07 补齐；详见 scan 主循环注释）。

    ⚠ `min_bars` 是**调用方**的数据充分性门槛（core 不留业务默认），故必传。

    比较用原始字符串（`bar["time"]` 为 "YYYY-MM-DD"），与历史实现逐字一致。
    """
    if not bars:
        return None
    if bars[-1]["time"] > target:
        bars = [b for b in bars if b["time"] <= target]
    if not bars or len(bars) < min_bars or bars[-1]["time"] != target:
        return None
    return bars


def fetch_stock_info_db():
    """全量 stock_basic_info: {symbol: {name, circ_shares, ...}} (换手率/市值/ST过滤用)。"""
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    pool = db._get_pool()
    with pool.cursor() as cur:   # 注意: 该 pool 返回元组行 (与 test_dragon 原实现一致)
        cur.execute(
            "SELECT symbol, name, circ_shares, total_shares FROM stock_basic_info WHERE status='active'"
        )
        rows = cur.fetchall()
    out = {}
    for row in rows:
        out[row[0]] = {"name": row[1] or "", "circ_shares": float(row[2] or 0),
                       "total_shares": float(row[3] or 0)}
    return out


def all_codes():
    """全市场活跃代码表 (扫描 universe)。"""
    from app.utils.basicinfo_db import get_stock_basic_db
    return get_stock_basic_db().market_all_codes(status="active")
