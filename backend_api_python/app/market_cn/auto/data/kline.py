#!/usr/bin/env python3
"""K线与股票基础信息加载 (auto/data) —— 从 dragon_scan.py 迁入 (Phase 1, 2026-09-07)

用途: 盘后扫描/盘中监控/手动工具共用的数据读取; dragon_scan.py 改为 from 本模块 import
(名字原样 re-export, 外部 import 路径与行为不变)。
关键设计点:
  - fetch_kline_db: DB 1D + 前复权, 与 test_dragon.fetch_kline_db 同口径 (对数基准);
  - fetch_stock_info_db: 全量 stock_basic_info → {symbol: {name, circ_shares, ...}};
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
