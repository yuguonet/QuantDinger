"""window_cache.py — 全市场日线「滑动窗口」缓存 (生产链取数 17s → 数秒)。

背景 (2026-10-06 实测, 5236 票 / days=320):
  _query_batch_raw 拉 164 万行 = 9.1s, unadj_to_qfq 复权 5236 票 = 8.0s ⇒ 17.1s/天。
  其中 **163.5 万行是昨天已经拉过的历史** —— 每天重复付一次, 这就是"全量"的代价。

滑动的定义 (与 kline._window_bounds 同源, 不是另立一套):
  窗口 = [anchor - days*1.5 日历日, anchor + 1 日历日], anchor = now (as_of=None)。
  今天窗口相对昨天**整体右移** ⇒ 只需
      ① 删掉 < start_new 的行 (头部过期)
      ② 追加 > end_old 的行 (尾部新增, 一天只有 ~5236 行)
  其余 163 万行从缓存来, 一行都不碰 DB。

⚠ 只服务 as_of=None 的「今日窗口」:
  回测 / 历史重建 (as_of=某历史日) 是一次性全量语义, 走原路径: 既不进缓存,
  也不读缓存 (否则会被今天的数据污染, 反之亦然)。days > BASE_DAYS 同理。

⚠ 前复权是**会改写历史**的 (除权/送股 ⇒ 全窗口 qfq 值整体变), 因此缓存不能无
  条件追加。每票存一份**复权因子指纹**: 指纹变 ⇒ 该票整票全量重拉重算。
  指纹不变 ⇒ 历史行的 qfq 值逐位不变 (unadj_to_qfq 是逐行独立计算), 追加安全。

自愈 (不是降级): 缓存缺失 / 版本变 / 文件损坏 / 票不在缓存 / 指纹变
  ⇒ 该票(或全部)**全量重拉**, 与无缓存时的取数结果**逐行逐字段相等**。
  任何环节抛异常 ⇒ 整批回落 fetch_klines_batch 且**不写坏缓存**。

存储: 单文件 pickle —— {code: (dates int32[n], ohlcv float64[n,5])} + meta。
  numpy 紧凑存储 (约 80MB @5236×343), 比 167 万个 dict 的 pickle 小一个量级。
  取用时还原成 list[dict] (与 fetch_klines_batch 返回结构逐字一致)。
"""

from __future__ import annotations

import os
import pickle
import time
from datetime import date, datetime, timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)

CACHE_VER = 3

# 缓存按 BASE_DAYS 的窗口存一份; 请求 days <= BASE_DAYS 时用 window_start(days)
# 切片派生 (kline.window_start 的语义: 长窗口内 time >= start 的部分 == 短窗口)。
BASE_DAYS = 320

# 缓存中 N 天未在请求集中出现 ⇒ 淘汰 (退市/长期停牌)。缓存是共享资产,
# 不能按"本次没请求"直接删 —— 那会让子集调用清掉全市场缓存。
STALE_DAYS = 30

# 缓存最长存活天数 —— 到期整批全量重建。
# ⚠ 增量只能看见「窗口尾部新增」的行。历史行被数据订正 (日期 <= end_old, 如
#   盘后修正/补录) 增量**看不见** ⇒ 缓存会一直留着错值。故周期性全量刷新兜底:
#   每 MAX_AGE_DAYS 天付一次全量代价 (~17s), 换取缓存不会长期陈旧。
MAX_AGE_DAYS = 3

# 缓存目录: QD_WINDOW_CACHE_DIR 覆盖, 默认 backend_api_python/cache/kline_window
_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..",
    "cache", "kline_window")

# 关闭开关 (排障用): =0 时恒走全量路径
_ENABLED_ENV = "QD_WINDOW_CACHE"


def _cache_dir() -> str:
    return os.getenv("QD_WINDOW_CACHE_DIR") or os.path.abspath(_DEFAULT_DIR)


def _path(days: int) -> str:
    return os.path.join(_cache_dir(), "window_%d_v%d.pkl" % (int(days), CACHE_VER))


def enabled() -> bool:
    return str(os.getenv(_ENABLED_ENV, "1")).strip() != "0"


# ================================================================
# 因子指纹 (除权检测)
# ================================================================

# fp 快路径是否可用 (读 adjustment 的进程内因子表)。不可用 ⇒ 回落公开 API,
# **慢但正确** —— 绝不因拿不到指纹就返回恒定值 (fp 恒定 = 除权永不触发重拉
# = 历史被改写却无人知道, 属静默降级)。
_FP_FAST = None


def _factor_fp(code: str) -> int:
    """前复权因子指纹。None/空 ⇒ 0 (该票无复权, 历史不会被因子改写)。

    ⚠ 不能每票走 `fetch_qfq_factors`: 它对未命中票会打**远端** (_fetch_remote),
      实测 5236 票 = 7.0s —— 比滑动省下的取数还贵。快路径直接读 adjustment 的
      进程内因子表 `_mem` (只读), 未命中视作 0: 与建缓存时用同一判据, 故自洽
      (因子从无到有时 fp 由 0 变非 0 ⇒ 触发重拉)。
    """
    global _FP_FAST
    fs = None
    if _FP_FAST is not False:
        try:
            from app.data_sources.provider import adjustment as _adj
            sina = _adj._to_sina_code(code)
            with _adj._mem_lock:
                fs = _adj._mem.get(sina) if sina else None
            _FP_FAST = True
        except Exception:
            _FP_FAST = False                    # 私有结构变了 ⇒ 回落公开 API
            fs = None
    if _FP_FAST is False or fs is None:
        try:
            from app.data_sources.provider.adjustment import fetch_qfq_factors
            fs = fetch_qfq_factors(code)
        except Exception:
            return 0
    if not fs:
        return 0
    return hash(tuple((str(d), float(f)) for d, f in fs)) & 0x7FFFFFFFFFFFFFFF


# ================================================================
# 行 <-> 数组
# ================================================================

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ord(d: str) -> int:
    return date(int(d[:4]), int(d[5:7]), int(d[8:10])).toordinal()


def _dstr(o: int) -> str:
    return date.fromordinal(int(o)).isoformat()


def _to_arrays(bars: list[dict]):
    import numpy as np
    n = len(bars)
    ds = np.empty(n, dtype=np.int32)
    arr = np.empty((n, 5), dtype=np.float64)
    for i, b in enumerate(bars):
        ds[i] = _ord(str(b["time"])[:10])
        arr[i] = (float(b["open"]), float(b["high"]), float(b["low"]),
                  float(b["close"]), float(b["volume"]))
    return ds, arr


def _to_bars(ds, arr) -> list[dict]:
    """还原成 fetch_klines_batch 的同构 list[dict]。"""
    return [{"time": _dstr(int(ds[i])),
             "open": float(arr[i][0]), "high": float(arr[i][1]),
             "low": float(arr[i][2]), "close": float(arr[i][3]),
             "volume": float(arr[i][4])} for i in range(len(ds))]


# ================================================================
# 缓存读写
# ================================================================

# 进程内 memo (2026-10-06): 73MB 缓存文件每票读一次 = 185ms/票, monitor 50 票
# ⇒ 9.2s/tick, 比原 fetch_kline_db (7.9ms/票) 慢 23 倍。故进程内只加载一次,
# 后续用 (mtime_ns, size) 签名校验: 文件被本进程 scan 更新或外部改动 ⇒ 重读。
# ⚠ 就地修改 data 前**必须**先浅拷贝 (见 _slide), 否则异常路径会留下脏 memo。
_MEMO: dict = {}


def _load_cache(days: int):
    """读缓存 → (data, meta) 或 (None, None)。损坏/版本不符 ⇒ None (调用方全量重建)。"""
    p = _path(days)
    if not os.path.isfile(p):
        _MEMO.pop(days, None)
        return None, None
    try:
        st = os.stat(p)
        sig = (st.st_mtime_ns, st.st_size)
        hit = _MEMO.get(days)
        if hit is not None and hit[0] == sig:
            return hit[1], hit[2]
        with open(p, "rb") as f:
            blob = pickle.load(f)
        if not isinstance(blob, dict) or blob.get("_ver") != CACHE_VER:
            return None, None
        data = blob.get("data") or {}
        meta = blob.get("meta") or {}
        _MEMO[days] = (sig, data, meta)
        return data, meta
    except Exception as e:
        logger.warning("[window_cache] 缓存损坏(%s) → 全量重建", e)
        return None, None


def _save_cache(days: int, data: dict, meta: dict):
    p = _path(days)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"_ver": CACHE_VER, "data": data, "meta": meta}, f,
                    protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, p)
    try:                                    # 刷新 memo 签名, 免得下一票又读一次 73MB
        st = os.stat(p)
        _MEMO[days] = ((st.st_mtime_ns, st.st_size), data, meta)
    except Exception:
        _MEMO.pop(days, None)


# ================================================================
# 对外: 滑动取数
# ================================================================

def load_windows(codes, days: int = BASE_DAYS, logger_=None) -> dict:
    """全市场日线滑动取数 → {code: bars}, 与 fetch_klines_batch 逐行逐字段相等。

    days <= BASE_DAYS 时从 BASE_DAYS 缓存按 window_start(days) 切片派生;
    days > BASE_DAYS 或缓存不可用 ⇒ 走 fetch_klines_batch (结果同构)。
    """
    lg = logger_ or logger
    from app.market_cn.auto.core.data.kline import fetch_klines_batch, _window_bounds
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    if not enabled():
        return fetch_klines_batch(codes, days=days, as_of=None)

    base = days if days > BASE_DAYS else BASE_DAYS
    t0 = time.time()
    try:
        out = _slide(codes, base, lg)
    except Exception as e:                      # 滑动任何环节出错 ⇒ 全量, 不写坏缓存
        lg.warning("[window_cache] 滑动失败(%s) → 全量重取", e)
        return fetch_klines_batch(codes, days=days, as_of=None)
    lg.info("[window_cache] 滑动取数 %d 票 (days=%d, %.1fs)",
            len(out), days, time.time() - t0)

    if days >= base:
        return {c: out[c] for c in codes if c in out}
    # 短窗口: 按 window_start(days) 从长窗口切片 (kline.window_start 的同源语义)
    lo = _window_bounds(days, None)[0]
    return {c: [b for b in out[c] if b["time"] >= lo]
            for c in codes if c in out}


def peek_windows(codes, days: int = BASE_DAYS) -> dict:
    """**只读**缓存 (不写、不补拉) → {code: bars}, 未命中的票不出现在结果里。

    monitor 盘中每票每 tick 用: 缓存由 scan.py 盘后维护, 盘中只消费。若允许
    monitor 补拉缺失票, 每票都会触发一次全量缓存落盘 (~80MB) —— 比省下的取数
    贵几个量级。取不到 ⇒ 调用方回落原路径 (daily_live), 语义不变。
    """
    from app.market_cn.auto.core.data.kline import _window_bounds
    if not enabled():
        return {}
    base = days if days > BASE_DAYS else BASE_DAYS
    data, _meta = _load_cache(base)
    if not data:
        return {}
    lo = _window_bounds(days, None)[0]
    lo_o = _ord(lo)
    out = {}
    for c in codes or ():
        item = data.get(c)
        if item is None:
            continue
        ds_arr, arr = item
        if len(ds_arr) and int(ds_arr[0]) < lo_o:
            k = int((ds_arr >= lo_o).argmax())
            ds_arr, arr = ds_arr[k:], arr[k:]
        if len(ds_arr):
            out[c] = _to_bars(ds_arr, arr)
    return out


def _slide(codes, days: int, lg) -> dict:
    """滑动主体: 读缓存 → 增量 → 写缓存。"""
    from app.market_cn.auto.core.data.kline import _window_bounds
    from app.market_cn.auto.core.data.hub import _query_batch_raw
    from app.data_sources.provider.adjustment import unadj_to_qfq

    start, end = _window_bounds(days, None)
    data, meta = _load_cache(days)

    # ① 缓存不可用 ⇒ 全量建缓存 (首次 / 损坏 / 版本变)
    if not data:
        raw = _query_batch_raw("CNStock", codes, "1D",
                               start_time=start, end_time=end)
        data, fps = {}, {}
        for code, rows in raw.items():
            bars = _rows_to_bars(rows, code, unadj_to_qfq)
            if bars:
                data[code] = _to_arrays(bars)
                fps[code] = _factor_fp(code)
        _save_cache(days, data, {"start": start, "end": end, "fps": fps,
                                "seen": {c: _today() for c in data},
                                "built_at": _today()})
        lg.info("[window_cache] 建缓存 %d 票 (%s~%s)", len(data), start, end)
        return {c: _to_bars(*data[c]) for c in codes if c in data}

    # ⚠ 就地修改前先浅拷贝: memo 里的 data 是进程内共享对象, 若中途异常而 save 未
    #    执行, 直接改会把半截状态留在 memo 里 (下次 peek 读到脏窗口)。
    #    arrays 由 concatenate/vstack 产生新对象, 故浅拷贝足够。
    data = dict(data)

    # ② 滑动: 窗口只前进 (start/end 都右移) 才可增量; 否则重建
    if str(meta.get("start")) > start or str(meta.get("end")) > end:
        lg.warning("[window_cache] 窗口回退(%s~%s → %s~%s) ⇒ 重建",
                   meta.get("start"), meta.get("end"), start, end)
        return _rebuild(codes, days, start, end, unadj_to_qfq, lg)
    # ②b 到期全量刷新: 增量看不见「历史行被订正」, 靠周期性重建兜底
    _bi = str(meta.get("built_at") or "")
    if _bi and _bi <= (datetime.now()
                       - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d"):
        lg.info("[window_cache] 缓存建于 %s (>%d 天) ⇒ 全量刷新",
                _bi, MAX_AGE_DAYS)
        return _rebuild(codes, days, start, end, unadj_to_qfq, lg)

    old_fps = meta.get("fps") or {}
    todo = [c for c in codes if c not in data]        # 新股 / 缓存未覆盖

    # ③ 尾部新增: 只拉 > end_old 的行 (~5236 行, 而非 164 万行)
    new_rows = {}
    if str(meta.get("end")) < end:
        new_rows = _query_batch_raw("CNStock", codes, "1D",
                                    start_time=meta.get("end"), end_time=end)
    fps = dict(old_fps)
    n_new = n_rb = 0
    for i, code in enumerate(codes):
        ds_arr = data.get(code)
        if ds_arr is None:
            continue
        # 除权检测: 因子指纹变 ⇒ 整票重拉 (历史被改写, 缓存不可信)
        fp = _factor_fp(code)
        if fp != old_fps.get(code):
            todo.append(code)
            fps[code] = fp
            continue
        rows = new_rows.get(code)
        if not rows:
            continue
        # ⚠ 复权必须**只对新增行**做, 且用与全量路径同一个 unadj_to_qfq
        nb = _rows_to_bars(rows, code, unadj_to_qfq)
        last_o = int(ds_arr[0][-1]) if len(ds_arr[0]) else 0
        nb = [b for b in nb if _ord(str(b["time"])[:10]) > last_o]
        if not nb:
            continue
        import numpy as np
        nds, narr = _to_arrays(nb)
        data[code] = (np.concatenate([ds_arr[0], nds]),
                      np.vstack([ds_arr[1], narr]))
        n_new += len(nb)

    # ④ 全量补拉: 新股 + 除权票
    if todo:
        raw = _query_batch_raw("CNStock", todo, "1D",
                               start_time=start, end_time=end)
        for code, rows in raw.items():
            bars = _rows_to_bars(rows, code, unadj_to_qfq)
            if bars:
                data[code] = _to_arrays(bars)
                fps[code] = _factor_fp(code)
                n_rb += 1

    # ⑤ 头部过期: 裁掉 < start 的行
    # ⚠ **不得**删除"本次没请求"的票 —— 缓存是跨调用方的共享资产 (scan 全市场建,
    #   monitor 按持仓子集消费)。曾因删非请求票 ⇒ 一次子集调用把 73MB 全市场缓存
    #   清成 1 票。退市/长期停牌票改由 seen 日期淘汰 (STALE_DAYS 天未出现)。
    lo_o = _ord(start)
    seen = dict(meta.get("seen") or {})
    _now = datetime.now()
    today_s = _now.strftime("%Y-%m-%d")
    for c in codes:
        if c in data:
            seen[c] = today_s
    cutoff = (_now - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")
    for c in [c for c, s in seen.items() if str(s) < cutoff]:
        data.pop(c, None)
        fps.pop(c, None)
        seen.pop(c, None)
    for code, (ds_arr, arr) in list(data.items()):
        if len(ds_arr) and int(ds_arr[0]) < lo_o:
            k = int((ds_arr >= lo_o).argmax())
            data[code] = (ds_arr[k:], arr[k:])

    _save_cache(days, data, {"start": start, "end": end, "fps": fps,
                            "seen": seen,
                            "built_at": meta.get("built_at") or _today()})
    return {c: _to_bars(*data[c]) for c in codes if c in data}


def _rebuild(codes, days, start, end, unadj_to_qfq, lg) -> dict:
    from app.market_cn.auto.core.data.hub import _query_batch_raw
    raw = _query_batch_raw("CNStock", codes, "1D", start_time=start, end_time=end)
    data, fps = {}, {}
    for code, rows in raw.items():
        bars = _rows_to_bars(rows, code, unadj_to_qfq)
        if bars:
            data[code] = _to_arrays(bars)
            fps[code] = _factor_fp(code)
    _save_cache(days, data, {"start": start, "end": end, "fps": fps,
                            "seen": {c: _today() for c in data},
                            "built_at": _today()})
    lg.info("[window_cache] 重建缓存 %d 票", len(data))
    return {c: _to_bars(*data[c]) for c in codes if c in data}


def _rows_to_bars(rows, code, unadj_to_qfq) -> list[dict]:
    """原始 tuple 行 → 前复权 bars dict。

    与 fetch_klines_batch 的转换**逐字同源** (同列序 / 同 str[:10] / 同 float /
    同 unadj_to_qfq) —— 这是"滑动结果 == 全量结果"的构造性保证。
    """
    if not rows:
        return []
    return unadj_to_qfq([{
        "time": str(r[0])[:10],
        "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
        "close": float(r[4]), "volume": float(r[5]),
    } for r in rows], code)


def invalidate(days: int = BASE_DAYS) -> bool:
    """删除缓存 (排障 / 强制全量重建)。"""
    p = _path(days)
    try:
        if os.path.isfile(p):
            os.remove(p)
            return True
    except Exception:
        pass
    return False
