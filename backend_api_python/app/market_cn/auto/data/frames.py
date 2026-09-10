#!/usr/bin/env python3
"""frames.py — 历史盘中"快照帧"重建层 (统一回测内核 P1, 2026-09-10)

用途:
  把 kline_1m 重建为**逐分钟全市场快照帧**, 让 intraday_window 策略 (tail/knife 及
  未来同类) 在历史区间内以与实盘**完全相同**的 scan_signals(ctx) 路径回测 ——
  回测与实盘共用同一份判定代码, 差异只剩数据通道 (kline_1m 重建 vs realtime_snapshot)。

触发语义 (与 test_v2_tail_buy 基线逐字段对齐, "bar 开盘触发"):
  - 触发时刻 = 某分钟 bar 的开盘边界 p → snap.last = bar[p].open (即本次触发的可成交价);
  - snap.high/low = 当日累计极值 (截至 p-1 收盘), snap.previousClose = 前一 1m 交易日
    该股最后一根 close (qfq, 由引擎的 pc_map 注入);
  - series = bar[0..p-1] (已收盘分钟), 行结构模拟快照: {time, open, high, low, last, volume(累计)};
  - 时间标签按**位置→分钟槽**重标 (MI_HHMM[pos]), 与基线的位置索引口径一致 (稀疏票的
    位置≈槽位近似, 基线本身即如此); 实盘路径不受影响。
  - 复权: 1m 原始价 → qfq (unadj_to_qfq, 失败退回原始价), 与基线 fetch_1m 同处理。

缓存:
  每交易日一 npz (data/market_cn_cache/frames/YYYYMMDD.npz)。首次构建全市场单日约
  10~20s (一次性), 之后加载秒级; 目录超 CACHE_BUDGET_GB 按_mtime 淘汰最旧。

易错点:
  - kline_1m 按**年分表** (kline_1m_YYYY), 单日查询只命中一张表;
  - 快照帧的 codes = 当日 1m 有 bar 的股票 (停牌股自然缺席, 与基线 group_days 同效果);
  - 本模块**零策略规则** — 预筛/判定全部在策略插件 (intraday_shortlist/scan_signals)。
"""
import os
import glob
import numpy as np

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 缓存目录: backend_api_python/data/market_cn_cache/frames
CACHE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "data", "market_cn_cache", "frames"))
CACHE_BUDGET_GB = 2.0
CACHE_VER = "v2"        # 帧结构版本 (v1=跨股 cumsum 量纲污染, 已弃用; 旧文件由淘汰机制自然清理)

# 位置(=分钟槽) → HH:MM, 与基线 _mi_to_hhmm 一致: 0↔09:31, 119↔11:30, 120↔13:01, 235↔14:56
MI_HHMM = []
for _mi in range(240):
    _minutes = 571 + _mi if _mi <= 119 else 661 + _mi
    MI_HHMM.append(f"{_minutes // 60:02d}:{_minutes % 60:02d}")


def hhmm_to_pos(hhmm):
    """'HH:MM' → 分钟槽位 (MI_HHMM 的逆映射); 不在交易时段返回 -1。"""
    try:
        return MI_HHMM.index(str(hhmm)[:5])
    except ValueError:
        return -1


class MinuteFrame:
    """单交易日全市场分钟帧 (CSR 按股分段, 位置=分钟槽近似口径)。"""

    __slots__ = ("date", "codes", "code_idx", "offsets", "counts",
                 "o", "h", "l", "c", "vcum", "hcum", "lcum")

    def __init__(self, date, codes, offsets, counts, o, h, l, c, vcum):
        self.date = date
        self.codes = codes                      # list[str], 升序
        self.code_idx = {c: i for i, c in enumerate(codes)}
        self.offsets, self.counts = offsets, counts
        self.o, self.h, self.l, self.c, self.vcum = o, h, l, c, vcum
        # 分段累计极值 (截至各位置(含)之前 → 用 exclusive cummax)
        self.hcum = np.empty_like(h)
        self.lcum = np.empty_like(l)
        for i in range(len(codes)):
            s, e = offsets[i], offsets[i] + counts[i]
            if e > s:
                self.hcum[s:e] = np.maximum.accumulate(h[s:e])
                self.lcum[s:e] = np.minimum.accumulate(l[s:e])

    def __len__(self):
        return len(self.codes)

    def _seg(self, code):
        i = self.code_idx.get(code)
        if i is None:
            return None, None, None
        return i, self.offsets[i], self.counts[i]

    def snap(self, code, pos, pc):
        """位置 pos 的快照 dict (last=bar[pos].open); 位置不存在返回 None。"""
        i, off, n = self._seg(code)
        if i is None or pos >= n:
            return None
        s = off
        last = float(self.o[s + pos])
        if pos > 0:
            high = float(self.hcum[s + pos - 1])
            low = float(self.lcum[s + pos - 1])
        else:
            high = low = last                   # 日初无累计极值 (退化, 预筛会拒)
        # 时间串带秒 (与 realtime_snapshot 同格式): knife._tail_ret strptime("%H:%M:%S") 依赖
        return {"time": f"{self.date} {MI_HHMM[pos]}:00", "open": last, "high": high,
                "low": low, "last": last,
                "previousClose": float(pc) if pc else 0,
                "volume": float(self.vcum[s + pos])}

    def snaps_at(self, pos, pc_map, codes=None):
        """触发位置 pos 的全市场快照 {code: snap} (只含有 bar 的股票; dict 即建即用)。"""
        out = {}
        for i, code in enumerate(self.codes):
            if codes is not None and code not in codes:
                continue
            n = int(self.counts[i])
            if pos >= n:
                continue
            snap = self.snap(code, pos, pc_map.get(code))
            if snap is not None:
                out[code] = snap
        return out

    def series(self, code, upto_pos):
        """bar[0..upto_pos-1] → 快照形行的序列 (模拟快照累计量; 位置→时间重标)。"""
        i, off, n = self._seg(code)
        if i is None:
            return []
        e = min(off + upto_pos, off + n)
        date = self.date
        return [{"time": f"{date} {MI_HHMM[j - off]}:00",
                 "open": float(self.o[j]), "high": float(self.h[j]),
                 "low": float(self.l[j]), "last": float(self.c[j]),
                 "volume": float(self.vcum[j])}
                for j in range(off, e)]

    def mkt_gain(self, pos, pc_map):
        """全市场均涨幅% (与 scan._mkt_gain 同口径: mean(last/pc-1); 无样本返回 0.0)。"""
        gains, idxs = [], []
        for i, code in enumerate(self.codes):
            if int(self.counts[i]) > pos:
                pc = pc_map.get(code)
                if pc and pc > 0:
                    idxs.append(int(self.offsets[i]) + pos)
                    gains.append(pc)
        if not gains:
            return 0.0
        lasts = self.o[idxs]
        pcs = np.asarray(gains, dtype=float)
        ok = lasts > 0
        if not ok.any():
            return 0.0
        return float(np.mean((lasts[ok] / pcs[ok] - 1.0) * 100.0))

    def first_open(self, code):
        """该股当日第一根 bar 的 open (次日出场价用)。"""
        i, off, n = self._seg(code)
        if i is None or n == 0:
            return 0.0
        return float(self.o[off])

    def last_close(self, code):
        """该股当日最后一根 bar 的 close (pc_map 结转用)。"""
        i, off, n = self._seg(code)
        if i is None or n == 0:
            return 0.0
        return float(self.c[off + n - 1])


# ------------------------------------------------------------------
# 构建 / 缓存
# ------------------------------------------------------------------

def _cache_path(date):
    return os.path.join(CACHE_DIR, f"{CACHE_VER}_{date}.npz")


def _evict_cache():
    """目录超预算 → 按 mtime 淘汰最旧 (保留当前不删)。"""
    try:
        files = sorted(glob.glob(os.path.join(CACHE_DIR, "*.npz")),
                       key=os.path.getmtime)
        total = sum(os.path.getsize(f) for f in files)
        limit = CACHE_BUDGET_GB * (1 << 30)
        for f in files:
            if total <= limit:
                break
            total -= os.path.getsize(f)
            os.remove(f)
            logger.debug("[frames] 缓存淘汰: %s", f)
    except OSError as e:
        logger.debug("[frames] 缓存淘汰失败: %s", e)


def _seg_cumsum(v_l, off_l, cnt_l):
    """逐股分段累计量 (快照口径 volume=当日累计; **段内** cumsum, 跨股必须重置)。"""
    v = np.asarray(v_l, dtype=np.float64)
    out = np.empty_like(v)
    for i in range(len(off_l)):
        s_, n_ = int(off_l[i]), int(cnt_l[i])
        if n_ > 0:
            out[s_:s_ + n_] = np.cumsum(v[s_:s_ + n_])
    return out


def _qfq_bars(code, bars):
    """1m 原始价 → qfq (与基线 fetch_1m 同处理; 失败退回原始价)。"""
    try:
        from app.data_sources.provider.adjustment import unadj_to_qfq
        return unadj_to_qfq(bars, code)
    except Exception:
        return bars


def build_frame(date, codes=None):
    """构建单日全市场分钟帧 (先查缓存)。date='YYYY-MM-DD'。

    codes=None: 全市场 (结果落缓存); codes=[...]: 子集查询 (不落缓存, 防污染全量缓存)。
    """
    date = str(date)[:10]
    if codes is None:
        cached = _load_cache(date)
        if cached is not None:
            return cached
    else:
        codes = sorted(codes)

    from app.market_cn.auto.data.hub import all_codes
    universe = codes if codes is not None else all_codes()
    year = date[:4]
    table = f"kline_1m_{year}"

    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")

    per_code = {}                            # code -> list[(mi,o,h,l,c,v)]
    CHUNK = 800
    for s in range(0, len(universe), CHUNK):
        chunk = universe[s:s + CHUNK]
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f'SELECT symbol, time, open, high, low, close, volume '
                        f'FROM "{table}" WHERE symbol = ANY(%s) '
                        f'AND time >= %s AND time < %s ORDER BY symbol, time',
                        (chunk, f"{date} 09:00:00", f"{date} 15:01:00"))
                    cols = [d[0] for d in cur.description]
                    for row in cur.fetchall():
                        r = dict(zip(cols, row))
                        per_code.setdefault(str(r["symbol"]), []).append(r)
        except Exception as e:
            logger.warning("[frames] %s 分块查询失败: %s", date[:10], e)

    # 分段展平 (qfq → float 数组)
    sym_list, o_l, h_l, l_l, c_l, v_l, off_l, cnt_l = [], [], [], [], [], [], [], []
    off = 0
    for code in sorted(per_code):
        rows = per_code[code]
        if not rows:
            continue
        bars = _qfq_bars(code, [
            {"time": str(r["time"]), "open": float(r["open"] or 0),
             "high": float(r["high"] or 0), "low": float(r["low"] or 0),
             "close": float(r["close"] or 0), "volume": float(r["volume"] or 0)}
            for r in rows])
        o_l.extend(float(b["open"]) for b in bars)
        h_l.extend(float(b["high"]) for b in bars)
        l_l.extend(float(b["low"]) for b in bars)
        c_l.extend(float(b["close"]) for b in bars)
        v_l.extend(float(b["volume"]) for b in bars)
        off_l.append(off)
        cnt_l.append(len(bars))
        off += len(bars)
        sym_list.append(code)

    frame = MinuteFrame(
        date, sym_list,
        np.asarray(off_l, dtype=np.int64),
        np.asarray(cnt_l, dtype=np.int64),
        np.asarray(o_l, dtype=np.float64), np.asarray(h_l, dtype=np.float64),
        np.asarray(l_l, dtype=np.float64), np.asarray(c_l, dtype=np.float64),
        _seg_cumsum(v_l, off_l, cnt_l))
    if codes is None:
        _save_cache(frame)          # 子集查询不落缓存 (防污染全量缓存)
    return frame


def _save_cache(frame):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        np.savez_compressed(
            _cache_path(frame.date), codes=np.asarray(frame.codes),
            offsets=frame.offsets, counts=frame.counts,
            o=frame.o, h=frame.h, l=frame.l, c=frame.c, vcum=frame.vcum)
        _evict_cache()
    except OSError as e:
        logger.debug("[frames] 缓存写盘失败 (%s): %s", frame.date, e)


def _load_cache(date):
    path = _cache_path(date)
    if not os.path.exists(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as z:
            return MinuteFrame(
                date, [str(x) for x in z["codes"].tolist()],
                z["offsets"], z["counts"],
                z["o"], z["h"], z["l"], z["c"], z["vcum"])
    except Exception as e:
        logger.warning("[frames] 缓存加载失败 (%s): %s", date, e)
        return None


def trading_dates(days_back, end=None):
    """有日线记录的交易日列表 (升序, 窗口=最近 days_back 个自然日), 供引擎遍历。"""
    from datetime import datetime, timedelta
    from app.utils.db_market import get_market_db_manager
    end = str(end or datetime.now().strftime("%Y-%m-%d"))[:10]
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=int(days_back))).strftime("%Y-%m-%d")
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    out = []
    for year in {start[:4], end[:4]}:
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f'SELECT DISTINCT time::date FROM "kline_1D_{year}" '
                        f'WHERE time >= %s AND time < %s ORDER BY 1',
                        (start, end + " 23:59:59"))
                    out.extend(str(r[0]) for r in cur.fetchall())
        except Exception as e:
            logger.warning("[frames] %s 交易日查询失败: %s", year, e)
    return sorted(set(out))


def first_1m_date():
    """1m 数据覆盖的最早日期 (无数据返回 None); 引擎用它裁掉空帧日。"""
    from datetime import datetime
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    year = datetime.now().year
    best = None
    for y in (year, year - 1):
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SELECT MIN(time) FROM "kline_1m_{y}"')
                    row = cur.fetchone()
                    if row and row[0]:
                        d = str(row[0])[:10]
                        best = d if best is None or d < best else best
        except Exception:
            continue
    return best


def prev_closes(before_date):
    """每股在 before_date 之前最近一根 1m close (qfq) → {code: price} (pc_map 种子)。

    全量查询较慢 (~分钟级) → 结果按 before_date 落盘缓存 (data/market_cn_cache/frames)。
    """
    date = str(before_date)[:10]
    path = os.path.join(CACHE_DIR, f"pc_{date}.npz")
    if os.path.exists(path):
        try:
            with np.load(path, allow_pickle=False) as z:
                return {str(c): float(v) for c, v in
                        zip(z["codes"].tolist(), z["vals"].tolist())}
        except Exception as e:
            logger.debug("[frames] pc 缓存加载失败 (%s): %s", date, e)

    from app.market_cn.auto.data.hub import all_codes
    from app.utils.db_market import get_market_db_manager
    universe = all_codes()
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    out = {}
    year = str(before_date)[:4]
    CHUNK = 800
    for s in range(0, len(universe), CHUNK):
        chunk = universe[s:s + CHUNK]
        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f'SELECT DISTINCT ON (symbol) symbol, close '
                        f'FROM "kline_1m_{year}" WHERE symbol = ANY(%s) '
                        f'AND time < %s ORDER BY symbol, time DESC',
                        (chunk, f"{before_date} 09:00:00"))
                    for sym, close in cur.fetchall():
                        out[str(sym)] = float(close or 0)
        except Exception as e:
            logger.warning("[frames] pc 种子查询失败 (%s): %s", before_date, e)
    try:                                    # 落盘缓存 (下次秒级)
        os.makedirs(CACHE_DIR, exist_ok=True)
        np.savez_compressed(path, codes=np.asarray(list(out)),
                            vals=np.asarray([out[c] for c in out]))
    except OSError as e:
        logger.debug("[frames] pc 缓存写盘失败: %s", e)
    return out
