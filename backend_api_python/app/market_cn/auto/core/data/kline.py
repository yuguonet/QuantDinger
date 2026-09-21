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


def fetch_kline_db(code, days=300):
    """从 DB 加载日K (前复权), 返回 list[dict] (time/open/high/low/close/volume)。"""
    from datetime import datetime as _dt
    end = (_dt.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (_dt.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        from app.utils.db_market import get_market_kline_writer
        from app.data_sources.provider.adjustment import unadj_to_qfq
        writer = get_market_kline_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
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
    fetch_kline_db 返回 [] 等价)。as_of 非空时只保留 ≤as_of 的 bar (hub.daily 语义)。

    shards: 并发分片数 (<=1 = 单条 SQL)。分片只改变"哪条 SQL 取哪些代码", 合并后
    结果与单条完全一致 (各片代码互斥, 片内顺序保持)。
    """
    codes = [c for c in codes if c]
    if not codes:
        return {}
    from datetime import datetime as _dt
    end = (_dt.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (_dt.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
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


def window_start(days=300):
    """加载窗口的日历下界 (fetch_kline_db / fetch_klines_batch 的 start)。

    `fetch_kline_db(code, days)` 的定义就是 "time ∈ [now - days*1.5, now+1d]" ——
    因此 **days=200 的行集 == days=300 行集中 time ≥ window_start(200) 的部分**。
    需要更短窗口派生量 (如 g56 横截面池的 200 根) 时, 可据此从共享长窗口缓存**切片**
    得到, 免去再加载一遍全市场 (切片与逐票同口径, 非近似)。
    """
    from datetime import datetime as _dt
    return (_dt.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")


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
