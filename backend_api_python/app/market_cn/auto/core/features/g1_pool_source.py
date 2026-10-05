# -*- coding: utf-8 -*-
"""G1 横截面池的**台账源** —— 把"全市场重算"换成"读历史 + 算当日"。

定位 (分层, 不改语义层):
  语义层  cross_section._ensure_pool_daily / _g1_arrays / _g1_mask / _aggregate
  台账层  g1_ledger  (状态推进; 不碰数据源)
  编排层  g1_daily   (取数→推进→判池→落盘)
  池源层  **本文件** (池历史的持久化形态 + 从台账重建池)

================================================================
★ 为什么池可以被"增量" —— `_aggregate` 的全部输入是什么
================================================================
`_aggregate(by_date)` 只用到每 (board, date) 的四个量:
    n    = len(当日 G1 通过票的读数列表)
    rmed = median(rhist_chg)   dmed = median(dif0)   smed = median(rsi)
`score_r` 是这四个**日序列**的滚动分位均值。所以只要按日存下这四元组,
池就能被完整重建 —— 不必保存每票明细, 也不必重算历史。

持久化形态即 per-board 四元组 (`quad`), 一天两行(board)共 8 个 float。
实测 0.6 KB/天 ⇒ 一年 ≈ 0.15 MB, 不需要分片/压缩。

================================================================
★★ 等价性的两个硬前提 (违反 ⇒ 必须回退全量, 不许静默)
================================================================
1. **滚动窗口**: `score_r` 用 `_pctl_roll(w=20, min_hist=5)` ⇒ 当日值只依赖
   前 20 个交易日。池历史必须**连续覆盖到当日**, 且长度 ≥ MIN_HIST。
   ⚠ 全量池的序列随 target 左端滑动(200 根窗口), 增量池历史是累积的 ——
     两者末端的 score_r 一致(滚动只看前 20 天), 但**起点不同**。
     ⇒ 增量池**只服务日常扫描(判末根)**; **回测必须走全量**(逐日枚举需要
     全序列, 且校准基线 `scan.py run_scan(days=320)` 就是全量)。
2. **口径对齐**: 全量循环的两条过滤必须在本源复刻 ——
     `len(bars) < 68 → continue`        ⇒ 台账侧 `age < 68`
     `code.startswith(("8","4","92"))`  ⇒ 台账侧同样前缀排除

================================================================
收益实测 (2026-10-05, 5204 票, target=2026-09-30)
================================================================
  全量池构建(已带 bars_batch)  3.41s
  从台账算当日桶              1.84s
  从池历史读回 + 聚合          ≈0.1s   ← 日常 scan 走这条
  ⚠ 取数 200 根(7.6s) 是信号判定本身需要的, **切池源省不掉**。
    ⇒ 本模块的收益是"池 3.41s → ~0.1s", 不是取数层面的量级差。

依赖: cross_section (_ensure_pool_daily / _g1_mask / _aggregate) / g1_ledger
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from app.market_cn.auto.core.features.cross_section import (
    MIN_HIST, ROLL, _aggregate, _ensure_pool_daily, _g1_mask,
    g1_state_window_bars,
)
from app.market_cn.auto.core.features.g1_ledger import g1_state_features

logger = logging.getLogger(__name__)

#: 对齐 `_ensure_pool_daily` 内 `len(bars) < 68: continue`
POOL_MIN_LEN = 68
#: 对齐 `_ensure_pool_daily` 内北交所/老三板排除
POOL_EXCLUDE_PREFIX = ("8", "4", "92")
#: 池历史至少要有这么多天, score_r 才谈得上可信
POOL_MIN_DAYS = max(ROLL + 1, MIN_HIST)
#: 默认台账窗口 —— 与既有产物 `ledger_win35.json` 对齐（换 win = 换一套状态）
DEFAULT_WIN = 35

#: 可观测计数(静默降级是头号敌人: 走没走台账源、为什么回退, 必须能查)
POOL_SOURCE_STATS: Dict[str, int] = {
    "hit": 0,              # 台账源命中
    "fallback": 0,         # 回退全量(含原因, 见 last_reason)
    "backfill": 0,
    "disabled": 0,
}
LAST_REASON: Dict[str, str] = {}


def _buckets_empty() -> Dict[str, Dict[str, list]]:
    return {"main": {}, "gem_star": {}}


def _excluded(code: str) -> bool:
    return str(code).startswith(POOL_EXCLUDE_PREFIX)


def default_ledger(win: int = 35):
    """默认台账（惰性构造 + 已 load）。台账/池历史不存在 ⇒ 池源自动不可用 ⇒ 回退全量。

    ⚠ 每次调用都新建实例(不缓存单例): 台账是会被 `g1_daily` 进程并发写的,
      这里只做**只读**消费, 持有长期实例会读到过期内容。
    """
    from app.market_cn.auto.core.features.g1_ledger import (
        G1Ledger, default_ledger_path,
    )
    lg = G1Ledger(path=default_ledger_path(win), win=int(win))
    try:
        lg.load()
    except Exception as e:                       # 台账不存在/损坏 ⇒ 不可用, 不是错误
        LAST_REASON["default_ledger"] = "台账不可用: %s" % e
        logger.info("[g1_pool] 台账不可用, 池走全量: %s", e)
        return None
    return lg


def day_buckets(ledger, date: Optional[str] = None,
                codes: Optional[Sequence[str]] = None,
                hit_out: Optional[set] = None) -> Dict[str, Dict[str, list]]:
    """从**台账状态**算当日(每票末根)的 G1 桶 —— 池的"增量"来源。

    每票只做 `g1_state_features(st)`(小窗口 + 锚, 不需要 200 根 bars),
    再走**同一个** `_g1_mask`。与全量路径共用 mask 实现 ⇒ 判据不可能分叉。

    Args:
        hit_out: 可选 set。**顺便**收集通过 G1 的 code(仅限 date 命中者)。
          ★ 为什么合在一次循环里: 桶与名单本是同一次判断的两面, 分开跑等于
            把 5204 票的小窗口计算付两遍(实测各 ~1.8s)。合起来净增 ≈0。

    Returns:
        {board: {date: [ls_rhist, ls_dif0, ls_rsi]}} —— 与 `_ensure_pool_daily`
        内部的 `buckets` **同构**, 可直接喂 `_aggregate`。
    """
    out = _buckets_empty()
    want = str(date)[:10] if date else None
    cs = list(codes) if codes is not None else sorted(ledger.codes())
    for c in cs:
        if _excluded(c):
            continue
        st = ledger.state(c)
        if int(st.get("age", 0)) < POOL_MIN_LEN:
            continue
        board = ledger.board_of(c)
        if board not in out:
            continue
        f = g1_state_features(st)
        hit = _g1_mask(f, board, age=int(st["age"]))
        idx = hit.nonzero()[0] if hasattr(hit, "nonzero") else [
            i for i, v in enumerate(hit) if v]
        if len(idx) == 0:
            continue
        k = int(idx[-1])
        d = str(f["dates"][k])[:10]
        if hit_out is not None and (want is None or d == want):
            hit_out.add(c)
        if want and d != want:
            continue                      # 停牌票末根早于当日 ⇒ 归入它自己的那天
        rec = out[board].setdefault(d, [[], [], []])
        rec[0].append(float(f["rhist_chg"][k]))
        rec[1].append(float(f["dif0"][k]))
        rec[2].append(float(f["rsi"][k]))
    return out


def quad_of(bucket: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """桶 → 四元组持久化形态 `(n, rmed, dmed, smed)`。"""
    import numpy as np
    if not bucket or not bucket[0]:
        return {"n": 0}
    return {"n": int(len(bucket[0])),
            "rmed": float(np.median(bucket[0])),
            "dmed": float(np.median(bucket[1])),
            "smed": float(np.median(bucket[2]))}


def day_quad(ledger, date: Optional[str] = None,
             codes: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
    """per-board 四元组 —— 池历史**一行**的内容。

    date 给定 ⇒ 只要那一天(日常写入用); 不给 ⇒ 全部日期(导出/比对用)。
    """
    bk = day_buckets(ledger, date=date, codes=codes)
    want = str(date)[:10] if date else None
    out: Dict[str, Dict[str, Any]] = {}
    for b, by_date in bk.items():
        if want:
            out[b] = quad_of(by_date.get(want, []))
        else:
            out[b] = {d: quad_of(x) for d, x in by_date.items()}
    return out


# ================================================================
# 一次性回填: 用全量路径把**历史**桶导出来 (池的构造仍只有一处实现)
# ================================================================

def export_full(target: str, pool_bars: Optional[Dict[str, list]] = None
                ) -> Dict[str, Dict[str, list]]:
    """跑一次全量池, 顺便把它的**原始日级桶**拿出来。

    ★ 不复制 `_ensure_pool_daily` 的循环: 通过 `buckets_out=` 让那一处实现把桶
      回填给我们。两套实现必然分叉(铁律), 这里没有第二套。
    """
    out: Dict[str, Dict[str, list]] = {}
    _ensure_pool_daily(target, bars_batch=pool_bars, buckets_out=out)
    return out


def backfill(ledger, target: str, pool_bars: Optional[Dict[str, list]] = None,
             force: bool = False, keep_g1: bool = True,
             include_last: bool = False) -> int:
    """把全量池的**历史**写进池历史 (首次接入的硬前提)。

    为什么必须回填而不是"从今天开始攒": `score_r` 是滚动分位, 冷启动的头
    20 天没有足够历史 ⇒ 值不可信。回填后增量才有意义。

    ★★ `include_last` (默认 False) —— **当日池必须由当日推进产生**:
      回填只补**历史**, target 当天留给 `g1_daily.run` 写。否则池历史末日会
      **超前状态一天** (实测: 回填到 09-30 而状态停在 09-29), 于是
        ① 当日台账源池可用、但单票特征回退 ⇒ 两条路**不同源**;
        ② 之后推进到 09-30 写池会撞"重发"保护 ⇒ 当日永远写不进去。
      池与状态**同源**是这套架构的前提, 故默认排除末日。
      ⚠ 若 target 已是过去某日且状态早已推进过它 (补录场景), 传 `include_last=True`。

    ⚠ 池历史已有内容时默认**拒绝**(`force=True` 才覆盖): 覆盖历史等于丢弃
      既有的连续记录, 那必须先显式删文件, 不做静默覆盖。

    Returns: 写入的天数。
    """
    if ledger.pool_last() is not None and not force:
        raise ValueError("池历史非空 —— 覆盖需显式 force=True (或先删池文件)")
    buckets = export_full(target, pool_bars)
    if not buckets:
        raise RuntimeError("全量池导出为空 (target=%s)" % target)
    dates = sorted({d for v in buckets.values() for d in v})
    if not include_last and len(dates) > 1:
        dates = dates[:-1]                         # 末日留给增量推进, 见 docstring
    # 先把已存在的文件移走, 再逐日 append (复用重发保护, 保证顺序)
    for y in {int(d[:4]) for d in dates}:
        p = ledger.pool_path(y)
        if os.path.isfile(p):
            os.remove(p)
    n = 0
    for d in dates:
        quad = {b: quad_of(v[d]) for b, v in buckets.items() if d in v}
        rec = {"date": d, "quad": quad}
        ledger.pool_append(rec)
        n += 1
    POOL_SOURCE_STATS["backfill"] += n
    logger.info("[g1_pool] 回填池历史 %d 天 (target=%s)", n, target)
    return n


# ================================================================
# 从池历史重建池 (日常 scan 走这条)
# ================================================================

def _bucket_from_quad(q: Dict[str, Any]) -> list:
    """四元组 → `_aggregate` 能吃的 `[ls_r, ls_d, ls_s]`。

    ★ 为什么可以: `_aggregate` 只取 `len()` 与 `median()`。常数数组的 median
      **精确**等于该常数(numpy 对常数数组无插值误差), len 也精确 ⇒ 输出与
      原始桶逐位一致。这样就不必为"重建"再存一份每票明细(那会大 100 倍)。
    """
    n = int(q.get("n", 0))
    if n <= 0:
        return []
    return [[float(q["rmed"])] * n,
            [float(q["dmed"])] * n,
            [float(q["smed"])] * n]


def pool_map_from_history(ledger, target: Optional[str] = None
                          ) -> Optional[Dict[str, Dict[str, list]]]:
    """读池历史 → `_aggregate` 的输入。None = 不可用(调用方应回退全量)。"""
    recs = ledger.pool_days()
    if not recs:
        LAST_REASON["pool_map"] = "池历史为空"
        return None
    if target and str(recs[-1].get("date", ""))[:10] != str(target)[:10]:
        LAST_REASON["pool_map"] = "池历史末日 %s != 目标 %s (当日未推进?)" % (
            recs[-1].get("date"), target)
        return None
    out = _buckets_empty()
    for r in recs:
        d = str(r.get("date", ""))[:10]
        for b, q in (r.get("quad") or {}).items():
            if b not in out or int(q.get("n", 0)) <= 0:
                continue
            out[b][d] = _bucket_from_quad(q)
    if not any(out.values()):
        LAST_REASON["pool_map"] = "池历史无有效四元组"
        return None
    return out


# ================================================================
# 单票特征的台账源 (2026-10-05 第二轮: 信号判定也切到预处理状态)
# ================================================================
#: ⚠ 为什么必须缓存: `scan_signals` 是**逐票**调的(5236 次), 而 `default_ledger`
#:   每次都新建实例 + 重新 load(实测 0.32s) ⇒ 不缓存 = 1676s, 直接爆炸。
#:   失效判据用 (mtime, size): 台账是**原子写** ⇒ 推进完成后 mtime 必变,
#:   日常扫描期间不变 ⇒ 只 load 一次。宁可多 load, 不可读到过期内容。
_LEDGER_CACHE: Dict[str, Any] = {"key": "", "ledger": None}


def cached_ledger(win: int = DEFAULT_WIN):
    """进程内缓存的台账 (按 mtime+size 失效)。不可用 ⇒ None (调用方回退)。"""
    from app.market_cn.auto.core.features.g1_ledger import (
        G1Ledger, default_ledger_path,
    )
    path = default_ledger_path(int(win))
    try:
        stt = os.stat(path)
        key = "%s|%d|%s" % (path, int(stt.st_size), getattr(stt, "st_mtime_ns", 0))
    except OSError as e:
        LAST_REASON["cached_ledger"] = "台账不可 stat: %s" % e
        return None
    if _LEDGER_CACHE.get("key") == key and _LEDGER_CACHE.get("ledger") is not None:
        return _LEDGER_CACHE["ledger"]
    lg = G1Ledger(path=path, win=int(win))
    try:
        lg.load()
    except Exception as e:                       # 不存在/损坏 = 不可用, 不是事故
        LAST_REASON["cached_ledger"] = "台账不可用: %s" % e
        logger.info("[g1_pool] 台账不可用, 单票特征走全量: %s", e)
        return None
    _LEDGER_CACHE.update(key=key, ledger=lg)
    return lg


def try_ledger_features(code: str, board: str, target: str,
                        win: int = DEFAULT_WIN, min_age: int = POOL_MIN_LEN):
    """单票**末位**特征的台账源 → `(f, k, mask, window_bars)`；None = 回退全量。

    ★ 为什么只服务"末位判定": 台账状态是**推进到当日的一份快照**, 没有历史序列。
      回测要逐日枚举全序列 ⇒ 必须走全量 `_g1_arrays`。所以本函数只在
      `scan_signals(as_of=None)`(日常判当日) 下调用, `as_of` 有值时不启用。

    ★★ 必须显式传 `age` 给 `_g1_mask`: 该函数内部暖机按**窗口下标 ≥68** 判,
      而台账窗口只有 win(35) 根 ⇒ 不传 age 则 mask 恒 False ⇒ **永远 0 信号
      且不报错**(2026-10-05 实测撞到, 见 §33.3)。

    Returns:
        (f, k, mask, window_bars) —— 可直接喂 `_g56_gate(f, pool, board, k, ...,
        mask=mask)` 与 `_mk_signal(code, window_bars, k, f, st)`。
    """
    v = str(os.environ.get("G1_SIGNAL_FROM_LEDGER", "")).strip().lower()
    if v in ("0", "off", "no", "false"):
        LAST_REASON["features"] = "开关关闭 (G1_SIGNAL_FROM_LEDGER=%r)" % v
        return None
    try:
        lg = cached_ledger(win)
        if lg is None:
            return None
        if not lg.has(code):
            LAST_REASON["features"] = "台账无此票"
            return None
        stt = lg.state(code)
        if str(stt.get("date", ""))[:10] != str(target)[:10]:
            LAST_REASON["features"] = "状态日期 %s != 目标 %s (当日未推进?)" % (
                stt.get("date"), target)
            return None
        if int(stt.get("age", 0)) < int(min_age):
            LAST_REASON["features"] = "age=%s < %d (数据不足, 对齐 len(bars)>=68)" % (
                stt.get("age"), min_age)
            return None
        f = g1_state_features(stt)
        k = len(f["dates"]) - 1
        mk = _g1_mask(f, board, age=int(stt["age"]))
        return f, k, mk, g1_state_window_bars(stt)
    except Exception as e:                       # 不吞: 登记 + 交回调用方回退
        LAST_REASON["features"] = "异常: %s" % e
        logger.warning("[g1_pool] 台账源取特征失败(%s), 回退全量: %s", code, e)
        return None


def try_ledger_pool(ledger, target: str, min_days: int = POOL_MIN_DAYS,
                    enabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """尝试用台账源建池。返回 None ⇒ **必须回退全量**(调用方不要吞掉)。

    Args:
        enabled: None ⇒ 读环境变量 `G1_POOL_FROM_LEDGER`("0"/"off"/"no" 关闭),
                 未设则默认**开启**(自动模式: 不可用会回退, 不是静默降级)。
    """
    v = str(os.environ.get("G1_POOL_FROM_LEDGER", "")).strip().lower()
    if enabled is None:
        enabled = v not in ("0", "off", "no", "false")
    if not enabled:
        POOL_SOURCE_STATS["disabled"] += 1
        LAST_REASON["try"] = "开关关闭 (G1_POOL_FROM_LEDGER=%r)" % v
        return None
    try:
        # ⚠ 天数按**池历史条数**判, 不按某个 board 桶的日期数 —— 后者会漏掉
        #   "当日该板块无人通过 G1" 的日子(小样本下能把 25 天读成 7 天)。
        n_days = len(ledger.pool_days())
        if n_days < int(min_days):
            POOL_SOURCE_STATS["fallback"] += 1
            LAST_REASON["try"] = "池历史仅 %d 天 < %d (score_r 滚动窗口不足)" % (
                n_days, min_days)
            return None
        pm = pool_map_from_history(ledger, target)
        if pm is None:
            POOL_SOURCE_STATS["fallback"] += 1
            LAST_REASON["try"] = LAST_REASON.get("pool_map", "池历史不可用")
            return None
        POOL_SOURCE_STATS["hit"] += 1
        LAST_REASON["try"] = ""
        return _ensure_pool_daily(target, pool_map=pm)
    except Exception as e:                       # 不吞: 登记 + 交回调用方回退
        POOL_SOURCE_STATS["fallback"] += 1
        LAST_REASON["try"] = "异常: %s" % e
        logger.warning("[g1_pool] 台账源建池失败, 回退全量: %s", e)
        return None
