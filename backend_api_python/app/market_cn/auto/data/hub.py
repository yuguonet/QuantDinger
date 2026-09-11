#!/usr/bin/env python3
"""data/hub.py — auto/ 唯一数据出口 (统一数据层, D1 2026-09-09, 设计见 tmp/自动策略架构评估与改进方案.md §3)

用途: 策略/扫描/监控/回测一律经本模块取数, 禁止直连物理源
     (kline 1D 分区表 / kline_1m_YYYY / realtime_snapshot_YYYY)。
接口形态说明: 设计稿画的是 DataHub 类, 落地改为模块级函数 (与 data/kline.py 现有
风格一致, 调用点改动最小); 函数名即接口, 语义不变。

数据通道与时效硬事实 (用户 09-09 说明):
  - daily:      1D 分区表, qfq, 最大 5 年 —— 盘后更新;
  - minute_1m:  kline_1m_YYYY, 不复权原始价, 最大 ~100 工作日 —— 盘后回填;
  - 盘中唯一通道: realtime_snapshot_YYYY (60s/拍, 保留 5 工作日, 可能丢拍) ——
    quote/market_snapshot/day_series/minute_live/daily_live 合成段全部来自它;
  - minute_live 输出按 mi 标准化并丢弃无效槽位 (缺口容忍), 调用方按槽位数自判数据是否足够;
  - lhb: 只读引用 market_cn/dragon_tiger_store (名单型事件数据, 不 OHLVC 化);
  - index_daily: 指数日线 (M3 环境特征通道, 09-11), 只读引用 market_cn/index.py
    降级链, hub 侧加磁盘缓存 —— L2 盘后补数语义: 缓存新鲜零外网, 过期才刷新。

关键设计点:
  - as_of 全线贯通: daily/minute_1m 只返回 as_of 当日及以前 —— 数据层兜底防未来函数,
    与回测引擎 as_of+ctx 同一语义;
  - daily_live = 历史日线 + 今日快照合成 bar (逐字收编 dragon_monitor._bars_with_synth
    的合成段口径); reconcile_daily_live 每日对账合成口径 vs 真日线回填, 微差超阈值告警;
  - 外部源 (data_sources/coordinator / cn_stock 等) 是 L2 盘后补数通道, 本模块盘中路径零触外网;
  - 股票索引/工作日历经 utils/basicinfo_db 与 trading_calendar, 策略不感知。

易错点:
  - writer.query 1m 传 start==end 单日区间可能返回空 → minute_1m 内部自动扩窗一天再按日期过滤;
  - 快照 open/high/low 是当日累计值 (非分钟 bar), volume 是累计量 → minute_live 内部差分;
  - 1m 为不复权原始价, daily 为 qfq: 跨层混算的除权偏差是已知限度, 换算用
    adjustment.unadj_to_qfq 由调用方显式做, hub 不静默换算;
  - snapshot 表按年分表 (realtime_snapshot_YYYY), 跨年对账/回看需注意。
"""
from __future__ import annotations

import json as _json
import os as _os
import time as _time

from app.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "daily", "daily_live", "minute_1m", "minute_live",
    "quote", "market_snapshot", "day_series",
    "stock_info", "all_codes", "lhb", "index_daily",
    "reconcile_daily_live",
]


# ================================================================
# 内部: 快照通道 (realtime_snapshot_YYYY)
# ================================================================

def _snapshot_pool():
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()._get_pool("CNStock")


def _snapshot_table(year=None):
    from datetime import datetime
    return f"realtime_snapshot_{year or datetime.now().year}"


def _rows(cur):
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_snapshots_by_date(codes, date=None):
    """指定日期的当日快照序列 {code: [rows]} (time 升序)。date=None 取今天。"""
    if not codes:
        return {}
    from datetime import datetime
    d = date or datetime.now().strftime("%Y-%m-%d")
    year = int(d[:4])
    try:
        pool = _snapshot_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT symbol, time, open, high, low, "last", "previousClose", volume '
                    f'FROM "{_snapshot_table(year)}" '
                    f"WHERE symbol = ANY(%s) AND time >= %s AND time < %s "
                    f"ORDER BY symbol, time",
                    (list(codes), f"{d} 09:00:00", f"{d} 16:00:00"),
                )
                rows = _rows(cur)
    except Exception as e:
        logger.warning("[hub] 快照读取失败 (%s): %s", d, e)
        return {}
    out = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append(r)
    return out


def day_series(codes, date=None):
    """当日(或指定日)快照序列 {code: [raw rows]} —— 收编 dragon_monitor.fetch_day_snapshots。"""
    return _fetch_snapshots_by_date(codes, date)


def market_snapshot(codes, date=None):
    """全市场(或给定 codes)最新一拍 {code: row} —— 收编 dragon_monitor.latest_snapshot。"""
    series = _fetch_snapshots_by_date(codes, date)
    return {code: rows[-1] for code, rows in series.items() if rows}


def quote(code, snaps=None):
    """单股最新一拍实时行情 (无K线语义)。snaps 可注入已拉取的快照池避免重复查询。"""
    if snaps is not None:
        return snaps.get(code)
    return market_snapshot([code]).get(code)


# ================================================================
# 日线通道 (1D qfq) 与实时日线合成
# ================================================================

def daily(code, days=300, as_of=None):
    """历史日线 (qfq, list[dict] time/open/high/low/close/volume)。
    as_of: 只返回该交易日(含)以前 —— 数据层兜底防未来函数。"""
    from app.market_cn.auto.data.kline import fetch_kline_db
    bars = fetch_kline_db(code, days)
    if as_of:
        bars = [b for b in bars if str(b["time"])[:10] <= str(as_of)[:10]]
    return bars


def _synth_bar_from_series(series, date):
    """快照序列 → 当日合成 bar (逐字收编 _bars_with_synth 合成段口径)。"""
    day_open = series[0]["open"]
    day_high = max(float(r["high"] or day_open) for r in series)
    day_low = min(float(r["low"] or day_open) for r in series)
    last_r = series[-1]
    return {"time": date, "open": float(day_open),
            "high": float(day_high), "low": float(day_low),
            "close": float(last_r["last"] or day_open),
            "volume": float(last_r["volume"] or 0)}


def daily_live(code, days=200, series=None):
    """实时日线 = 历史日线 + 今日合成 bar (若 1D 尚未回填今日)。

    series 可注入已拉取的快照序列 (monitor/scan 已有) 避免重复查询。
    返回 bars | None: bars 为空或无快照序列 → None (与 monitor._bars_with_synth 语义一致,
    entry_idx 定位仍由调用方完成)。
    """
    from app.market_cn.auto.data.kline import fetch_kline_db
    bars = fetch_kline_db(code, days)
    if not bars:
        return None
    if series is None:
        from datetime import datetime
        series = _fetch_snapshots_by_date([code]).get(code)
    if not series:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    if bars[-1]["time"] < today:
        bars.append(_synth_bar_from_series(series, today))
    return bars


# ================================================================
# 1m 通道 (kline_1m_YYYY, 不复权原始价)
# ================================================================

def minute_1m(code, start=None, end=None, as_of=None, limit=100000):
    """1m K线 (list[dict], time='YYYY-MM-DD HH:MM' 字符串)。

    易错点消化: start==end 单日区间可能整表返回空 → 内部自动把起点扩一天再按日期过滤。
    as_of: 只保留该交易日(含)以前。
    """
    from datetime import datetime, timedelta
    from app.utils.db_market import get_market_kline_writer
    if end is not None and len(str(end)) == 10:
        end = str(end) + " 23:59:59"          # 防日期型 end 被 DB 层按 00:00 截掉最后一日
    if end is None:
        end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if start is None:
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    start_wide = start
    if str(start)[:10] == str(end)[:10]:     # 单日区间 → 扩窗防空表
        start_wide = (datetime.strptime(str(end)[:10], "%Y-%m-%d")
                      - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        writer = get_market_kline_writer()
        rows = writer.query("CNStock", code, "1m",
                            start_time=start_wide, end_time=end, limit=limit)
    except Exception as e:
        logger.debug("[hub] %s 1m 加载失败: %s", code, e)
        return []
    out = []
    end_d = str(end)[:10]
    for r in rows:
        t = r["time"]
        ts = t.strftime("%Y-%m-%d %H:%M") if hasattr(t, "strftime") else str(t)[:16]
        d = ts[:10]
        if str(start)[:10] and d < str(start)[:10]:
            continue                          # 扩窗多拉的剔除
        if end_d and d > end_d:
            continue
        if as_of and d > str(as_of)[:10]:
            continue
        out.append({"time": ts, "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r["volume"])})
    return out


# ---- 分钟序列标准化 (原 tail_oversold 内联版上收, 唯一实现) ----

def _trading_minute_index(ts: str) -> int:
    """时间串 'YYYY-MM-DD HH:MM' → 0~239 (09:30起)。非法返回 -1。"""
    try:
        hm = ts.split(" ")[1][:5] if " " in ts else ts[-5:]
        h, m = int(hm[:2]), int(hm[3:5])
        minutes = h * 60 + m
        if minutes <= 570:      # <=09:30
            return 0
        if 570 < minutes <= 690:    # 09:31-11:30
            return minutes - 571
        if 690 < minutes <= 780:    # 11:31-13:00 午休 → 归 11:30
            return 119
        if 780 < minutes <= 900:    # 13:01-15:00
            return 120 + (minutes - 781)
        return 239
    except Exception:
        return -1


def prep_minutes(rows, volume_cumulative=False):
    """原始行 → 标准分钟序列。rows: [{time, open, high, low, close, volume}]

    volume_cumulative=True 表示 volume 是当日累计量 (snapshot), 先差分。
    输出按 mi 升序去重 (保留每分钟最后一根); 无效槽位 (c<=0) 丢弃 —— 缺口容忍,
    调用方按返回槽位数自判数据是否足够 (如 tail_ret 要求 mi 199~219 至少 15 槽)。
    """
    tmp = {}
    for r in rows:
        ts = str(r.get("time", ""))
        mi = _trading_minute_index(ts)
        if mi < 0:
            continue
        v = float(r.get("volume") or 0)
        # close 字段兼容: 1m K线用 close, 快照表用 last
        c = float(r.get("close") or r.get("last") or 0)
        tmp[mi] = {"mi": mi, "ts": ts, "o": float(r.get("open") or 0),
                   "h": float(r.get("high") or 0), "l": float(r.get("low") or 0),
                   "c": c, "v": v}
    out = [tmp[k] for k in sorted(tmp)]
    if volume_cumulative:
        prev = 0.0
        for b in out:
            b["v"] = max(0.0, b["v"] - prev)
            prev = b["v"] + prev
            b["cv"] = prev
    else:
        cv = 0.0
        for b in out:
            cv += b["v"]
            b["cv"] = cv
    out = [b for b in out if b["c"] > 0]
    return out


def minute_live(code, series=None):
    """盘中实时1m = 当日快照序列差分密集化 (标准化槽位序列)。"""
    if series is None:
        from datetime import datetime
        series = _fetch_snapshots_by_date([code]).get(
            code, None) or []
    return prep_minutes(series, volume_cumulative=True)


# ================================================================
# 基础数据 / 龙虎榜
# ================================================================

def stock_info():
    """全量 stock_basic_info: {symbol: {name, circ_shares, ...}} (换手率/市值/ST过滤用)。

    2026-09-10 自 data/kline.py 归位 (该函数本就不属 K线通道, 且曾被调用点绕过 hub 直连)。
    """
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
    """全市场活跃代码表 (转 data/kline.py)。"""
    from app.market_cn.auto.data.kline import all_codes
    return all_codes()


def lhb(stock_code=None, trade_date="", days=30):
    """龙虎榜事件 (名单型数据, 不 OHLVC 化)。只读引用 market_cn/dragon_tiger_store。"""
    try:
        from app.market_cn.dragon_tiger_store import query_dragon_tiger
        return query_dragon_tiger(trade_date=trade_date, stock_code=stock_code, days=days)
    except Exception as e:
        logger.warning("[hub] 龙虎榜读取失败: %s", e)
        return []


# ================================================================
# 指数日线 (M3 环境特征通道, 2026-09-11)
# ================================================================

# 易错: 相对 __file__ 需回退 4 级 auto/data → market_cn → app → backend_api_python
# (与 frames.CACHE_DIR 同款路径推导, 缓存放 data/market_cn_cache 不进源码树)
INDEX_CACHE_DIR = _os.path.normpath(_os.path.join(
    _os.path.dirname(__file__), "..", "..", "..", "..",
    "data", "market_cn_cache", "index"))

_INDEX_FETCH_CAP = 800   # mootdx TDX 协议单次上限 (index.py 同款约束)


def _index_cache_path(code):
    return _os.path.join(INDEX_CACHE_DIR, f"{code}_1d.json")


def _index_norm_bars(raw):
    """index.py 返回行 → 规范 bar dict (date 升序, 数值化, 缺 date 丢弃)。"""
    out = []
    for r in raw or []:
        d = str(r.get("date", ""))[:10]
        if len(d) != 10:
            continue
        try:
            bar = {"date": d}
            for f in ("open", "high", "low", "close", "volume", "amount"):
                v = r.get(f)
                bar[f] = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            continue
        out.append(bar)
    out.sort(key=lambda b: b["date"])
    return out


def _index_load_cache(code):
    try:
        with open(_index_cache_path(code), "r", encoding="utf-8") as f:
            payload = _json.load(f)
        return payload.get("bars") or []
    except (OSError, ValueError):
        return []


def _index_save_cache(code, bars):
    try:
        _os.makedirs(INDEX_CACHE_DIR, exist_ok=True)
        tmp = _index_cache_path(code) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"code": code, "fetched_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
                        "bars": bars}, f, ensure_ascii=False)
        _os.replace(tmp, _index_cache_path(code))    # 原子落盘
    except OSError as e:
        logger.warning("[hub] 指数缓存写盘失败 (%s): %s", code, e)


def index_daily(code="000001", days=800, as_of=None, force=False):
    """指数日线 (list[dict] date/open/high/low/close/volume/amount, date 升序)。

    code 默认 "000001"=上证指数 (index.py 约定, 非个股); 沪深300 用 "000300"。
    缓存策略 (L2 盘后补数语义): 磁盘缓存 {code}_1d.json, 末根 date < 今日 视为过期
    → force/缺失/过期才触远端降级链 (mootdx→tencent→sina→baostock), 命中缓存零外网;
    远端失败但有旧缓存时回退旧缓存 (告警, 不空手)。
    as_of: 只返回该交易日(含)以前 —— 与 daily() 同语义, 离线重放防未来函数。
    注意: 远端只保证最近 ~800 根 (TDX 上限), 更长窗口不可用。
    """
    from datetime import datetime
    cached = _index_load_cache(code)
    fetch_days = min(max(days, 400), _INDEX_FETCH_CAP)
    stale = (not cached) or cached[-1]["date"] < datetime.now().strftime("%Y-%m-%d")
    if force or stale or len(cached) < days:
        try:
            from app.market_cn.index import get_index_daily_kline
            fresh = _index_norm_bars(get_index_daily_kline(code, fetch_days, force=True))
        except Exception as e:
            fresh = []
            logger.warning("[hub] 指数日线远端失败 (%s): %s", code, e)
        if fresh:
            if cached and cached[0]["date"] < fresh[0]["date"]:
                merged = {b["date"]: b for b in cached}      # 远端窗口覆盖不到的旧根保留
                merged.update({b["date"]: b for b in fresh})
                fresh = [merged[d] for d in sorted(merged)]
            _index_save_cache(code, fresh)
            cached = fresh
        elif not cached:
            return []                                        # 无缓存且远端失败
        else:
            logger.warning("[hub] 指数日线远端失败, 回退旧缓存 (%s, 末根 %s)",
                           code, cached[-1]["date"])
    # as_of 必须先于尾部切片 (2026-09-11 修): 先 [-days:] 再过滤会把历史 as_of
    # 之前的全部"未来"根裁掉 → 恒空 (break_v2 回测 env 门失效根因)。
    # 正确语义 = "该交易日(含)以前" 的最后 days 根。
    bars = cached
    if as_of:
        bars = [b for b in bars if b["date"] <= str(as_of)[:10]]
    bars = bars[-days:] if days and len(bars) > days else bars
    return bars


def index_minute(code="000300", days=800, as_of=None):
    """指数 5m K线 (list[dict] time/open/high/low/close/volume/up_count/down_count,
    time 升序, "YYYY-MM-DD HH:MM" 字符串)。读独立表 kline_index_5m
    (scripts/sync_index_minute.py 盘后落库, 2026-09-11 起; 无磁盘缓存 — DB 即存储)。

    code 用 6 位指数码 ("000300"=沪深300, "399006"=创业板指), 存储符号按
    399*→.SZ / 其余→.SH 映射 (与 sync_index_daily.INDICES 同键)。
    up/down_count = 当根 bar 涨/跌家数 (市场宽度)。
    as_of: 只返回该交易日(含)以前 —— 与 daily()/index_daily() 同语义;
    **必须先过滤后尾切** (先 [-days:] 再过滤会把历史 as_of 的全部未来根裁掉,
    2026-09-11 hub.index_daily 同型 bug 的教训)。
    """
    sym = f"{code}.{'SZ' if str(code).startswith('399') else 'SH'}"
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    pool = mgr._get_pool("CNStock")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if as_of:
                cur.execute(
                    f'SELECT time, open, high, low, close, volume, up_count, down_count '
                    f'FROM "kline_index_5m" WHERE symbol = %s AND time::date <= %s '
                    f'ORDER BY time ASC',
                    (sym, str(as_of)[:10]))
            else:
                cur.execute(
                    f'SELECT time, open, high, low, close, volume, up_count, down_count '
                    f'FROM "kline_index_5m" WHERE symbol = %s ORDER BY time ASC',
                    (sym,))
            rows = cur.fetchall()
    bars = [{"time": r[0].strftime("%Y-%m-%d %H:%M"), "open": r[1], "high": r[2],
             "low": r[3], "close": r[4], "volume": r[5],
             "up_count": r[6], "down_count": r[7]} for r in rows]
    return bars[-days:] if days and len(bars) > days else bars


# ================================================================
# daily_live 收盘对账 (微差持续监控; 调度接入在 S 阶段, 先提供函数+CLI)
# ================================================================

CLOSE_DIFF_WARN_PCT = 0.3   # 收盘合成价 vs 真日线差异告警阈值 (%)


def reconcile_daily_live(target=None, codes=None, sample=50):
    """对账: target 日"快照合成 bar" vs "1D 真日线回填值"。

    target 默认=上一完结交易日 (快照表只留 5 工作日, 只能对最近几日跑, 每日跑昨日)。
    codes 默认从活跃股票随机抽 sample 只。
    返回报告 dict: {target, checked, diffs: [{code, field, synth, real, diff_pct}], warned}
    """
    import random
    from datetime import datetime
    from app.market_cn.auto.data.kline import fetch_kline_db
    if target is None:
        try:
            from app.utils.trading_calendar import last_finish_trading_day
            target = last_finish_trading_day()
        except Exception:
            target = datetime.now().strftime("%Y-%m-%d")
    if codes is None:
        cs = all_codes()
        random.seed(int(_time.time()))
        codes = random.sample(cs, min(sample, len(cs)))
    series_map = _fetch_snapshots_by_date(codes, target)
    diffs, checked = [], 0
    for code in codes:
        series = series_map.get(code)
        if not series:
            continue
        real_bars = [b for b in daily(code, days=15) if str(b["time"])[:10] == target]
        if not real_bars:
            continue                       # 1D 未回填 target (周末跑) → 跳过
        checked += 1
        synth = _synth_bar_from_series(series, target)
        real = real_bars[0]
        for f in ("open", "high", "low", "close"):
            try:
                sv, rv = float(synth[f]), float(real[f])
            except (TypeError, ValueError):
                continue
            if rv <= 0:
                continue
            pct = (sv / rv - 1) * 100
            if abs(pct) > (CLOSE_DIFF_WARN_PCT if f == "close" else 1.0):
                diffs.append({"code": code, "field": f,
                              "synth": round(sv, 3), "real": round(rv, 3),
                              "diff_pct": round(pct, 3)})
    warned = bool(diffs)
    if warned:
        logger.warning("[hub] daily_live 对账 %s: %d/%d 只超阈值 (close>%.2f%%/高低开>1%%): %s",
                       target, len(diffs), checked, CLOSE_DIFF_WARN_PCT, diffs[:10])
    else:
        logger.info("[hub] daily_live 对账 %s: %d 只全部一致 (阈值 close %.2f%%)",
                    target, checked, CLOSE_DIFF_WARN_PCT)
    return {"target": target, "checked": checked, "diffs": diffs, "warned": warned}


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="daily_live 收盘对账")
    ap.add_argument("--target", default=None, help="对账交易日 (默认上一完结交易日)")
    ap.add_argument("--sample", type=int, default=50)
    args = ap.parse_args()
    rep = reconcile_daily_live(target=args.target, sample=args.sample)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
