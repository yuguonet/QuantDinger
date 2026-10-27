"""
中国A股数据源

═══════════════════════════════════════════════════════════════
  设计思路：

  1. 分时线（1m/5m/15m/30m/1H/2H/4H）：
     - 本地优先（盘中/盘后同构）：DB 直读 / 5m 及以上由 1m 聚合；DB 缺当日时用
       realtime_snapshot 合成当日尾段拼接 —— 双闸门（首必须有数据 + 同段中间
       最多连续缺 QD_RT_MAX_GAP_SLOTS=5 根 + 尾鲜度盘中≤120s/盘后=15:00），
       闸门不过或 snapshot 不可用才回落远端 coordinate 兜底；
     - 2026-10-24 前盘中一律走远端（coordinate 多源串行、timeout=20s）；
       实测 rt 合成 3.2ms 且口径与远端逐根一致后取消盘中特判（用户拍板）。
     - 复权统一收口在 _get_kline_intraday：kline_1m 与远端 provider 均为
       **不复权原始价**，出口统一 unadj_to_qfq —— 否则除权股会盘中/盘后口径漂移

  2. 日/周线（1D / 1W）走 DB + TTL 混合流程：
     - 1W 先转成 1D 的 count，走完 1D 流程后再聚合回周线。

  3. Ticker（实时行情）：
     - 盘中（交易日 9:15~15:01）每次必拉，保证实时性
     - 非盘中优先走 TTL 缓存，未命中再拉远端
     - 合并当前股 + TTL 已有 symbols（归一化，最大 500）
     - 拉取结果写入 TTL 内存（无有效期，最旧先丢弃）

  4. Kline（1D / 1W）：
     ① lastbar = realtime_snapshot 最近一拍转 OHLCV（rt 每拍 open/high/low
        即当日累计值，单拍=全日 ⇒ 只需尾鲜度闸门：盘中 now-末拍≤120s /
        盘后期望末拍=15:00 / 午休≈11:30；不过闸 ⇒ 不伪造今日 bar）；
     ② 读 DB 1D bar（快路径 SQL 下推最近 limit 条+时间窗；慢路径重读全量
        供合并去重）；
     ③ 快速路径：DB 够新（≥前一交易日）：
        · 非盘中+DB 已有今日 → 直读（已落库态）；
        · 非盘中+盘后 15:01 后未落库+rt 末拍新鲜 → 拼 lastbar 直返
          （2026-10-24 新增，此前盘后一律绕远端慢路径）；
        · 盘中 → 去今日旧 bar + 拼 lastbar 直返；
     ④ 慢路径（DB 不够新 / 盘中 lastbar 拼不出）：从远端拉 1D
        → 合并 DB + 远端（远端优先覆盖）→ 归一化去重 → 写 DB
        → 根据 source 是否含今日 / lastbar 是否命中，三分支构造 out_kline
     ⑤ 1D 直接返回；1W 用日线聚合为 ISO 周线后返回

  5. before_time / after_time（Unix 秒）对**全部周期**生效：
     1D/1W 由 _finalize_1d 收尾统一处理，顺序恒为「先按时间窗过滤 → 再按
     limit 截断 → 前复权」；分钟线由 get_kline 在出口 filter_and_limit。
     若把截断提到过滤之前，历史窗口回补（before_time 指向较早区间）会被
     截成"最近 limit 条"后整段滤空。

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import threading
import time as _time
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.data_sources.base import BaseDataSource
from app.data_sources.coordinator import get_coordinator
from app.data_sources.normalizer import normalize_chart_timeframe, strip_market_prefix
from app.data_sources.provider.adjustment import unadj_to_qfq
from app.utils.logger import get_logger
from app.utils.trading_calendar import is_trading_day, prev_trading_day

logger = get_logger(__name__)


# ================================================================
# rt（realtime_snapshot）闸门口径 —— 用户 2026-10-24 拍板，单行常量顶部定义
# ================================================================
# QD_RT_MAX_GAP_SLOTS：1m 合成序列的防空防漏阈值。口径 = 「首必须有数据，
#   同段中间最多连续缺 5 根」：上午段首个段内 tick 必须落在开盘 5 槽内
#   （09:30-09:35），同一交易时段内相邻 tick 缺失根数 >5 判 rt 不可信、
#   交远端 coordinate 兜底。按**缺失根数**判，不按间隔分钟数判
#   （09:30:30→09:36:10 间隔 5.67 分钟但只缺 5 根，放行）。
#   午休（11:30→13:00）/ 跨夜 / 09:25 竞价拍为结构空档，不判。
#   已知边界语义（非缺陷）：采集器死于 11:25-11:29 或 14:55-14:59（距收盘
#   ≤5 根）时按「最多缺 5 根」口径不兜底。
QD_RT_MAX_GAP_SLOTS = 5
# _RT_STALE_SECONDS：尾鲜度。连续竞价期 now-末拍 >120s（丢了 2 根）判 rt
#   不可信；盘后期望末拍=15:00（早于 14:55 判采集器早退）；午休末拍≈11:30；
#   盘前（<09:15）/ 异日 / 非交易日不拼。实测：相邻拍间隔 1min×643 /
#   2min×190（4 个交易日），120s 阈值对自然抖动零误判。
_RT_STALE_SECONDS = 120

# 交易时段（分钟数，北京时间）：用于 rt 序列槽判据与新鲜度判据
_RT_AM_START, _RT_AM_END = 9 * 60 + 30, 11 * 60 + 30   # 09:30-11:30
_RT_PM_START, _RT_PM_END = 13 * 60, 15 * 60            # 13:00-15:00


# ================================================================
# TTL 快照缓存（进程级单例，无有效期）
#
# 为什么需要 TTL：
#   盘中 ticker 是实时价格，但远端接口有调用频率限制。
#   TTL 缓存让同一批 symbols 的行情可以在多次 get_kline 调用间复用，
#   避免每个股票单独拉一次 ticker。
#
# 淘汰策略：
#   最大 500 条，超限按写入时间丢弃最旧的。
#   没有固定有效期——盘中每次 get_tickers 会刷新，非盘中直接用缓存。
# ================================================================

class _SnapshotCache:
    """批量行情快照缓存，无有效期（TTL），最大 500 条，最旧先丢弃。"""

    _MAX_SIZE = 500

    def __init__(self):
        # 同时存 pure symbol 和原始 symbol 两种 key，方便查找
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._ts: Dict[str, float] = {}  # 每个 key 的写入时间，用于淘汰
        self._lock = threading.Lock()

    def symbols(self) -> List[str]:
        """返回 TTL 中所有去重后的 symbol（去市场前缀的纯代码）。"""
        with self._lock:
            seen = set()
            result = []
            for key in self._quotes:
                pure = strip_market_prefix(key)
                if pure not in seen:
                    seen.add(pure)
                    result.append(pure)
            return result

    def write(self, quotes: List[Dict[str, Any]]) -> None:
        """将行情数据写入 TTL 内存，超 500 条按最旧时间丢弃。"""
        now = _time.time()
        with self._lock:
            for q in quotes:
                sym = q.get("symbol", "")
                if not sym:
                    continue
                pure = strip_market_prefix(sym)
                # 同时写入 pure 和原始两个 key，查哪个都能命中
                for key in (pure, sym):
                    self._quotes[key] = q
                    self._ts[key] = now

            # 超限淘汰：按写入时间排序，最旧的先删
            if len(self._quotes) > self._MAX_SIZE:
                sorted_keys = sorted(self._ts, key=lambda k: self._ts[k])
                to_remove = len(self._quotes) - self._MAX_SIZE
                for k in sorted_keys[:to_remove]:
                    self._quotes.pop(k, None)
                    self._ts.pop(k, None)

    def get(self, symbol: str, max_age: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """从 TTL 内存中查找指定 symbol（pure 和原始 key 都尝试）。

        max_age: 秒。指定时仅当缓存写入时间距今 <= max_age 才返回（盘中单股行情
        的实时性要求；None 表示不限 —— 非盘中读收盘价的场景用）。
        返回缓存对象的浅拷贝：调用方（get_tickers/get_ticker）会改写返回值的
        symbol 字段，直接返回引用会沿双 key 别名污染缓存原始对象。"""
        pure = strip_market_prefix(symbol) if symbol else symbol
        with self._lock:
            q = self._quotes.get(pure) or self._quotes.get(symbol)
            if q is None:
                return None
            if max_age is not None:
                ts = self._ts.get(pure) or self._ts.get(symbol) or 0
                if _time.time() - ts > max_age:
                    return None
            return dict(q)


# 进程级单例，全局共享同一份 TTL 缓存
_snapshot_cache = _SnapshotCache()


# ================================================================
# 工具函数
# ================================================================

def _is_in_trading_hours() -> bool:
    """判断当前是否在交易时段内（交易日 9:15 < t <= 15:01）。

    为什么是 15:01：
      A 股 15:00 收盘，但收盘瞬间最后一笔成交可能延迟几秒，
      给 1 分钟缓冲确保能抓到收盘价。
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if not is_trading_day(today_str):
        return False
    t = now.time()
    return dtime(9, 15) < t <= dtime(15, 1)


def _prev_trading_day_ts() -> int:
    """前一交易日的 1D 时间戳（当天 15:00:00）。

    用途：判断 DB 数据是否足够新——如果 DB 最后一条 ≥ 前一交易日，
    说明 DB 已经包含了上一个交易日的收盘数据，可以走快速路径。
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    prev_td = prev_trading_day(today_str)
    return _day_close_ts(prev_td)


def _today_ts() -> int:
    """今日的 1D 时间戳（15:00:00），用于过滤/拼接今日数据。"""
    return _day_close_ts(datetime.now().strftime("%Y-%m-%d"))


def _day_close_ts(date_str: str) -> int:
    """指定日期 15:00:00 的 epoch（1D bar 的 time 口径）。

    R6 收口：`_bar_from_ticker` / `_read_snapshot_lastbar` 曾内联重算
    「strptime + replace(hour=15) + timestamp」。`_today_ts()` 固定取
    datetime.now()，**不适用于 now 注入场景**（lastbar 的 today_str 来自
    调用方统一时刻）——故提日期入参变体而非直接调 _today_ts。
    """
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(hour=15).timestamp())


def _rows_to_bars(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DB K 线行 → bar 列表（epoch 秒 time + float 五字段），顺序保持。

    R1 收口：`_read_db_1m` 与 `_read_db` 曾各写一份逐位同型的转换循环，
    连带 tz 口径这种最易错点双实现——漂移一处即盘中/盘后差 8 小时。现收成
    单实现，两处调用。

    time 口径（DB time 列是 naive 北京时间，与 db_market 写入路径一致）：
      · naive datetime → 补 Asia/Shanghai tz 再取 epoch。**不能按 UTC 解**
        （会偏 8 小时；db_market._ensure_datetime 的 UTC 推导只用于 SQL 入窗
        参数，不用于行转换——两处口径故意不同，勿"统一"）；
      · tz-aware datetime → 直接 timestamp()；
      · 非 datetime（int/float/str）→ int() 直转（兼容直接给 epoch 的调用方）。

    五字段缺失给 0（DB NOT NULL 约束下不可达，防御兜底非契约承诺）。
    """
    _beijing = ZoneInfo("Asia/Shanghai")
    bars = []
    for row in rows:
        t = row.get("time")
        if isinstance(t, datetime):
            # DB timestamp is naive Beijing time; compute epoch accordingly
            ts = int(t.replace(tzinfo=_beijing).timestamp()) if t.tzinfo is None else int(t.timestamp())
        else:
            ts = int(t)
        bars.append({
            "time": ts,
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "close": float(row.get("close", 0)),
            "volume": float(row.get("volume", 0)),
        })
    return bars


def _query_snapshot_rows(
    symbol: str, sql: str, params: tuple, ctx: str,
) -> List[Dict[str, Any]]:
    """realtime_snapshot 取数（tuple→dict 收口），读失败记 warning 返回 []。

    R2 收口：`_read_snapshot_lastbar` 与 `_read_snapshot_series` 曾各写一份
    同型 preamble（剥前缀 → 取 CNStock 池 → cursor → tuple 按 description 转
    dict）。tuple→dict 这个坑两处各修过一次——`_read_snapshot_lastbar` 曾因
    直接 .get() 抛 AttributeError 被 except 吞掉而静默失效。现收成一处：
    **调用点不得再自行转换**，直接消费 dict 行。

    symbol 剥市场前缀在 helper 内完成；sql 的第一个 %s 固定为 symbol，
    params 为其余参数（顺序 = SQL 占位符顺序）。
    ctx：warning 文案里的场景标识（如「最近一拍」「2026-10-24 序列」）。
    """
    db_symbol = strip_market_prefix(symbol) if symbol else symbol
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # "last"/"previousClose" 是 PG 保留字, SQL 中必须加双引号
                cur.execute(sql, (db_symbol,) + tuple(params))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"[snapshot] 读取 {db_symbol} {ctx} 失败: {e}")
        return []


# ---------------------------------------------------------------------------
# rt 闸门（防空防漏，口径见文件顶部常量区注释）
# ---------------------------------------------------------------------------

def _rt_hm(t: datetime) -> int:
    """t 的当日分钟数（hour*60+minute）。

    R8 收口：`_rt_in_session` / `_rt_slot_index` / `_rt_series_gap_ok`
    三处曾各算一遍。
    """
    return t.hour * 60 + t.minute


def _rt_in_session(t: datetime) -> bool:
    """t 是否落在连续竞价时段内（09:25 竞价拍/15:00 收盘拍属段外边界拍）。"""
    hm = _rt_hm(t)
    return _RT_AM_START <= hm < _RT_AM_END or _RT_PM_START <= hm < _RT_PM_END


def _rt_slot_index(t: datetime) -> int:
    """t 的分钟槽号：09:30→0 … 11:29→119；13:00→120 … 14:59→239。"""
    hm = _rt_hm(t)
    return hm - _RT_AM_START if hm < _RT_AM_END else hm - _RT_PM_START + 120


def _rt_freshness_ok(snap_time: datetime, now: datetime) -> bool:
    """rt 末拍新鲜度判据（尾）：末拍旧到「不该这么旧」即判 rt 不可信。

    · 连续竞价期（09:30-11:30 / 13:00-15:00）：now-末拍 ≤ _RT_STALE_SECONDS
      （120s 极限容差，只允许丢 1 根；实测自然间隔最大 2min，零误判）；
    · 午休（11:30-13:00）：末拍应=上午收盘拍（≈11:30，容 1min 时钟差）；
    · 盘后（15:00 之后）：末拍应=15:00 收盘拍（早于 14:55 判采集器早退）；
    · 09:15-09:30 竞价段：只接受 09:25 集合竞价出价那拍；
    · 盘前（<09:15）/ 末拍异日 ⇒ 今日不该有拍，False。
    """
    if snap_time.date() != now.date():
        return False
    hm_now = now.hour * 60 + now.minute
    hm_snap = snap_time.hour * 60 + snap_time.minute
    if hm_now < 9 * 60 + 15:                       # 盘前
        return False
    if hm_now < 9 * 60 + 30:                       # 09:15-09:30 竞价段
        return hm_snap >= 9 * 60 + 25
    if hm_now <= _RT_AM_END:                       # 上午连续竞价
        return (now - snap_time).total_seconds() <= _RT_STALE_SECONDS
    if hm_now < _RT_PM_START:                      # 午休：末拍≈11:30
        return hm_snap >= 11 * 60 + 29
    if hm_now < _RT_PM_END:                        # 下午连续竞价
        return (now - snap_time).total_seconds() <= _RT_STALE_SECONDS
    return hm_snap >= 14 * 60 + 55                 # 盘后：末拍=15:00（容 5 槽早退）


def _rt_series_gap_ok(times: List[datetime]) -> bool:
    """rt 全日 tick 序列闸门（首+中）：1m 合成入口的防空防漏判据。

    判「首必须有数据 + 同段中间最多连续缺 QD_RT_MAX_GAP_SLOTS 根」：
      · 首：上午段第一个段内 tick 必须落在开盘 5 槽内（09:30-09:35）——
        开盘起连续空 >5 根 ⇒ 采集器开盘即缺位，判不可信；
      · 中：同段相邻 tick 缺失根数（槽号差-1）> QD_RT_MAX_GAP_SLOTS ⇒ 判不可信；
      · 午休/跨夜为结构空档（不在槽体系内），不判；
      · 尾鲜度不在此函数（由 `_rt_freshness_ok` 判）。
    空序列 / 段内无 tick（仅 09:25 竞价拍）⇒ False。
    """
    seg = [t for t in times if _rt_in_session(t)]
    if not seg:
        return False
    seg.sort()
    if _rt_slot_index(seg[0]) > QD_RT_MAX_GAP_SLOTS:
        return False                                # 首：开盘 5 槽内无数据
    for prev, cur in zip(seg, seg[1:]):
        same_am = (_rt_hm(prev) < _RT_AM_END and _rt_hm(cur) < _RT_AM_END)
        same_pm = (_rt_hm(prev) >= _RT_PM_START and _rt_hm(cur) >= _RT_PM_START)
        if same_am or same_pm:
            if _rt_slot_index(cur) - _rt_slot_index(prev) - 1 > QD_RT_MAX_GAP_SLOTS:
                return False                        # 中：连续缺 >5 根
    return True


def _normalize_1d_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """1D 归一化：每天只留一条 bar，time 统一归一到当天 15:00:00，按时间排序。

    规则：同一日期出现多条时，后面的覆盖前面的。
    这样当 DB 旧数据和远端新数据合并时，远端（排在后面）会覆盖 DB。
    time 归一到 15:00 后，后续 _merge_bars 用精确时间戳做 key 也不会出现同一天多条的问题。
    """
    if not bars:
        return bars
    by_date: Dict[str, Dict[str, Any]] = {}
    for bar in bars:
        dt = datetime.fromtimestamp(bar["time"])
        date_str = dt.strftime("%Y-%m-%d")
        # 归一化 time 到当天 15:00:00
        bar["time"] = _day_close_ts(date_str)
        by_date[date_str] = bar
    return sorted(by_date.values(), key=lambda b: b["time"])


def _bar_from_ticker(quote: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将 ticker/快照数据转为 1D OHLCV bar。

    转换逻辑：
      - close = 最新价（优先 last → close → price）
      - open/high/low 如果快照里有就用，没有就用 close 填充
      - time 固定为今日 00:00（1D 粒度）
      - 价格为 0 或缺失则返回 None（无效数据）
    """
    if not quote:
        return None
    price = (
        quote.get("last")
        or quote.get("close")
        or quote.get("price")
        or 0
    )
    if not price or float(price) <= 0:
        return None

    price = float(price)
    today_str = datetime.now().strftime("%Y-%m-%d")
    # 非交易日没有"今日bar"语义: 周末/节假日的 ticker 是上一交易日收盘价,
    # 若转成"今日15:00"的bar, 慢路径追加后会产生一根假日线
    if not is_trading_day(today_str):
        return None
    bar_ts = _day_close_ts(today_str)

    return {
        "time": bar_ts,
        "open": float(quote.get("open") or price),
        "high": float(quote.get("high") or price),
        "low": float(quote.get("low") or price),
        "close": price,
        "volume": float(quote.get("volume") or 0),
    }


def _merge_bars_group(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    """将一组 bar 聚合为一根（open=首根open, high=最大, low=最小, close=末根close, volume=求和）。"""
    return {
        "time": group[0]["time"],
        "open": group[0]["open"],
        "high": max(b["high"] for b in group),
        "low": min(b["low"] for b in group),
        "close": group[-1]["close"],
        "volume": round(sum(b["volume"] for b in group), 2),
    }


# 单次批量拉取的 symbol 上限（与 provider 批量接口的承载量对齐）
_BATCH_FETCH_LIMIT = 500


def _merge_batch_symbols(
    requested: List[str],
    extra: List[str],
    max_n: int = _BATCH_FETCH_LIMIT,
) -> List[str]:
    """合并「本次请求 symbols」+「TTL 已有 symbols」：保序、归一化去重、优先保留请求。

    为什么不能用 ``list(set(requested + extra))[:max_n]``：
      a. **set 迭代顺序随 hash 随机** —— 每次批量请求的 symbol 顺序都不同，
         配额边界抖动，日志/结果不可复现；
      b. **截断会切掉本次请求的 symbol** —— set 无序 + 尾部截断 ⇒ 被切掉的
         是哪几支不可控，调用方可能拿不到自己明确要的行情（真实丢股，非理论风险）；
      c. **未归一化** —— 同一支股以 "600519" 与 "SH600519" 两种形态各占一个配额，
         且 TTL 里存的是 pure key，混排后重复拉同一支。

    规则：请求 symbols 按原顺序先入，再用 extra（TTL 已有）补到 max_n；
    去重 key 统一取 ``strip_market_prefix`` 归一化后的纯代码。
    """
    merged: List[str] = []
    seen = set()
    # R5 收口：requested / extra 两段循环曾逐位同型各写一遍（差异仅在达限后
    # return vs break——两者最终都返到此处的 merged，合一等价）。请求项先入
    # ⇒ 被 max_n 截断的只会是 extra（缓存项）。
    for raw in list(requested) + list(extra or []):
        s = strip_market_prefix(raw) if raw else raw
        if not s or s in seen:
            continue
        seen.add(s)
        merged.append(s)
        if len(merged) >= max_n:
            break               # 请求列表本身就超限：绝不为了塞缓存而挤掉请求项
    return merged


def _fetch_quotes_into_cache(request_symbols: List[str]) -> List[Dict[str, Any]]:
    """合并 TTL 已有 symbols 一起批量拉取行情并写 TTL；空结果返回 []。

    R3 收口：get_tickers / get_ticker 曾各写一份同型信封（merge →
    coordinate_tickers → write）。降级分支（空结果后返什么）**留在调用方**
    ——两处语义不同（批量非盘中返已有缓存 / 单股返 {"last": 0}），提不进来
    也不该提。
    """
    all_symbols = _merge_batch_symbols(request_symbols, _snapshot_cache.symbols())
    quotes_list = get_coordinator().coordinate_tickers(
        symbols=all_symbols, market="CNStock", timeout=10,
    )
    if quotes_list:
        # 写入 TTL（后续 get_kline 会用到 lastbar）
        _snapshot_cache.write(quotes_list)
    return quotes_list


# ================================================================
# 主数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源: 1D/1W 走 DB + TTL, 1m 及 5m/15m/30m/1H/2H/4H 盘中/盘后同构本地优先（DB 直读 / 1m 聚合），不可用才远端。"""

    name = "CNStock/multi-source"

    # ── get_tickers: 批量行情（自选股列表）──

    def get_tickers(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """批量获取实时行情，写入 TTL 缓存。

        流程：
          盘中 → 每次必拉（保证实时性），合并 TTL 已有一起拉
          非盘中 → 先查 TTL，全命中直接返回；有缺失才拉远端

        拉取时会把当前股 + TTL 已有 symbols 合并（归一化，最大 500），
        一次批量拉取，结果写入 TTL，返回全部结果。

        Args:
            symbols: 自选股列表（已归一化的 symbol）
        Returns:
            行情列表，每项含 symbol/open/high/low/close/volume/last 等
        """
        symbols = [s.strip() for s in symbols if s and s.strip()]
        if not symbols:
            return []

        # ── 非盘中：先查 TTL，全命中直接返回（省一次远端调用）──
        if not _is_in_trading_hours():
            cached_results: List[Dict[str, Any]] = []
            all_hit = True
            for sym in symbols:
                cached = _snapshot_cache.get(sym)
                if cached:
                    cached["symbol"] = sym
                    cached_results.append(cached)
                else:
                    all_hit = False
            if all_hit:
                return cached_results

        # ── 盘中必拉 / 非盘中有缺失 → 合并 TTL 已有 symbols 一起拉 ──
        # 合并的目的是：一次批量请求同时刷新自选股 + 之前缓存过的股票
        # 保序去重且请求项优先（勿改回 set + 尾部截断，理由见 _merge_batch_symbols）
        quotes_list = _fetch_quotes_into_cache(symbols)
        if not quotes_list:
            logger.warning(f"[行情] 批量拉取失败: {symbols}")
            # 非盘中降级：返回已有的缓存（有总比没有好）
            if not _is_in_trading_hours():
                return [q for q in [_snapshot_cache.get(s) for s in symbols] if q]
            return []

        return quotes_list

    # ── get_ticker: 单股实时行情 ──

    def get_ticker(self, symbol) -> Dict[str, Any]:
        """获取单股实时行情。

        优先走 TTL 缓存，未命中时合并 TTL 已有 symbols 一起拉（顺带刷新缓存）。
        """
        if not symbol:
            return {"last": 0, "symbol": ""}

        sym = symbol.strip() if isinstance(symbol, str) else str(symbol)
        if not sym:
            return {"last": 0, "symbol": ""}

        # ── 先查 TTL ──
        # 盘中要求实时性: 仅接受 10s 内的新鲜缓存, 过期视为未命中走拉取;
        # 非盘中价格已定格(收盘价), 任意旧缓存直接可用
        _in_trading = _is_in_trading_hours()
        cached = _snapshot_cache.get(sym, max_age=10 if _in_trading else None)
        if cached:
            cached["symbol"] = sym
            return cached

        # ── TTL 没有，合并 TTL 已有 symbols 一起拉（顺带刷新缓存）──
        # 本次请求的 sym 必须排在首位（保序 + 请求优先），避免被配额截断丢掉
        quotes_list = _fetch_quotes_into_cache([sym])
        if not quotes_list:
            logger.warning(f"[行情] 所有数据源均失败: {sym}")
            return {"last": 0, "symbol": sym}

        # 从 TTL 取当前股（write 时已按 pure/symbol 双 key 存入）
        result = _snapshot_cache.get(sym)
        if result:
            result["symbol"] = sym
            return result

        logger.warning(f"[行情] 拉取成功但未找到当前股: {sym}")
        return {"last": 0, "symbol": sym}

    # ── get_kline: K 线数据入口 ──

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据。

        分流逻辑：
          1D / 1W → 走 DB + TTL 混合流程（有本地缓存，响应快）
          1m → 本地优先（DB 直读；DB 缺当日时 rt 合成拼接），不可用才远端
          5m/15m/30m/1H/2H/4H → 本地优先（DB 1m 聚合；缺当日时 rt 合成拼接）
          盘中/盘后同构——2026-10-24 前盘中一律走远端（coordinate 多源串行、
          timeout=20s），实测 rt 合成 3.2ms 且口径与远端逐根一致后取消特判。

        before_time / after_time 对全部分支生效：1D/1W 交由其内部收尾统一过滤
        （顺序必须是「先按时间窗过滤、再按 limit 截断」，否则历史窗口回补会被
        先截断成"最近 limit 条"而永远取不到旧区间）。
        """
        tf = normalize_chart_timeframe(timeframe)
        limit = max(int(limit or 300), 1)

        if tf == "1D":
            return self._get_kline_1d(symbol, limit, before_time, after_time)
        if tf == "1W":
            return self._get_kline_weekly(symbol, limit, before_time, after_time)

        # 1m/5m/15m/30m/1H/2H/4H — 本地优先（DB 直读 / 1m 聚合；DB 缺当日时
        # realtime_snapshot 合成拼接，双闸门不过自动落远端 coordinate 兜底）
        # 大小写必须与 normalize_chart_timeframe 的输出严格一致:
        #   1h/60m → "1H"（大写）, 4h/240m → "4H"（大写）, 2h → "2H"（大写）
        if tf in ("1m", "5m", "15m", "30m", "2H", "1H", "4H"):
            result = self._get_kline_intraday(symbol, tf, limit)
            if result:
                return self.filter_and_limit(
                    result, limit=limit,
                    before_time=before_time, after_time=after_time,
                    truncate=(after_time is None),
                )

        # DB 未命中 → 走远端
        return self._get_kline_remote(symbol, tf, limit, before_time, after_time)

    # ================================================================
    # 1D 核心流程
    #
    # 两阶段策略：
    #   DB 够新（≥ 前一交易日）→ 快速路径，不走远端
    #   DB 不够新 → 慢路径，从远端拉取后合并写 DB
    # ================================================================

    def _get_kline_1d(
        self,
        symbol: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """1D: DB + 远端补满 + lastbar。

        所有出口统一经 `_finalize_1d` 收尾 —— 先按 before_time/after_time 过滤，
        再按 limit 截断并前复权；调用方（get_kline / _get_kline_weekly）传 None
        时行为与旧版一致（等价于取最近 limit 条）。
        """
        in_trading = _is_in_trading_hours()
        now = datetime.now()
        today_ts = _today_ts()
        prev_td_ts = _prev_trading_day_ts()
        logger.debug(f"[kline_1d] {symbol} start, in_trading={in_trading}, limit={limit}")

        # ── 步骤 1：lastbar = realtime_snapshot 最近一拍（优先），TTL 行情兜底 ──
        # snapshot 带 time 可校验新鲜度（陈旧快照不会伪造今日 bar）、OHLCV 完整、持久化；
        # snapshot 不可用（非交易日 / 当日未拍 / 读失败 / 末拍不新鲜）
        # → 回落 _bar_from_ticker；再没有 ⇒ 慢路径 coordinate 兜底
        lastbar = self._read_snapshot_lastbar(symbol, now)
        if lastbar is None:
            quote = _snapshot_cache.get(symbol)
            if quote:
                lastbar = _bar_from_ticker(quote)
        logger.debug(f"[kline_1d] {symbol} lastbar={'有' if lastbar else 'None'}")

        # ── 步骤 2：读 DB 1D bar（快路径只需最近 limit 条 ⇒ SQL 下推 limit/
        #    时间窗；慢路径合并去重需要完整历史，步骤 4 前重读全量） ──
        db_bars = self._read_db(symbol, limit=limit,
                                before_time=before_time, after_time=after_time)
        db_last_ts = db_bars[-1]["time"] if db_bars else 0
        logger.debug(f"[kline_1d] {symbol} DB读取 {len(db_bars)} 条, "
                      f"db_last_ts={db_last_ts}, prev_td_ts={prev_td_ts}")

        # ── 步骤 3：DB 够新（≥ 前一交易日）──
        if db_last_ts >= prev_td_ts:
            # 3a: 非盘中 + DB 已有今日数据 → 直接用 DB，不走远端
            if not in_trading and db_last_ts >= today_ts:
                out = self._finalize_1d(db_bars, symbol, limit, before_time, after_time)
                logger.debug(f"[kline_1d] {symbol} 非盘中+DB有今日, 直接返回 {len(out)} 条")
                return out

            # 3b: 非盘中 + DB 最新就是前一交易日 → 直接返回
            #   盘前：今日未开盘，DB 数据已最新
            #   注意：盘后（交易日 15:01 后）不算——由 3b' 处理
            _today_str = now.strftime("%Y-%m-%d")
            _post_close = (is_trading_day(_today_str) and now.time() > dtime(15, 1))
            if not in_trading and db_last_ts == prev_td_ts and not _post_close:
                out = self._finalize_1d(db_bars, symbol, limit, before_time, after_time)
                logger.debug(f"[kline_1d] {symbol} 非盘中+DB为前一交易日, 直接返回 {len(out)} 条")
                return out

            # 3b': 非盘中 + 盘后 15:01 后 DB 未落库 + rt 末拍新鲜 ⇒ 拼 lastbar 直返
            #   （用户口径：非盘中未落库 = 1D 历史 + rt 末拍转换；拼不出才
            #    coordinate 兜底。此前 _post_close 一律绕进远端慢路径，
            #    盘后等 ETL 的 30 分钟窗口内每次读都是网络级延迟。）
            #   本块内 db_last_ts ≥ prev_td_ts 且未达 today_ts（3a 已挡），
            #   DB 尾条必为前一交易日 ⇒ 剔今日残留 + 拼 rt 末拍即完整。
            if not in_trading and _post_close and lastbar is not None:
                db_bars = [b for b in db_bars if b["time"] < today_ts]
                db_bars.append(lastbar)
                out = self._finalize_1d(db_bars, symbol, limit, before_time, after_time)
                logger.debug(f"[kline_1d] {symbol} 盘后未落库+rt末拍, 拼lastbar返回 {len(out)} 条")
                return out

            # 3c: 盘中 → 用 lastbar 拼出今日实时 bar
            if in_trading:
                if lastbar is None:
                    # lastbar 没命中 → 调 get_ticker 走完整 ticker 流程
                    self.get_ticker(symbol)
                    quote = _snapshot_cache.get(symbol)
                    if quote:
                        lastbar = _bar_from_ticker(quote)

                if lastbar is not None:
                    # 去掉今日旧 bar，追加 lastbar
                    db_bars = [b for b in db_bars if b["time"] < today_ts]
                    db_bars.append(lastbar)
                    out = self._finalize_1d(db_bars, symbol, limit, before_time, after_time)
                    logger.debug(f"[kline_1d] {symbol} 去今日+lastbar, 返回 {len(out)} 条")
                    return out
                # lastbar 也没有 ⇒ 不返回缺当日的陈旧数据（用户口径：拼不出
                # 走完整 coordinate 兜底），落入步骤 4 慢路径。

        # ── 步骤 4：DB 不够新 / 盘中拼不出 → 慢路径，从远端取 1D ──
        # 慢路径合并去重需要完整历史（步骤 2 只读了最近 limit 条）⇒ 重读全量
        db_bars = self._read_db(symbol)
        source = self._fetch_remote_1d(symbol, limit)
        source_last_ts = source[-1]["time"] if source else 0
        logger.debug(f"[kline_1d] {symbol} 远端返回 {len(source)} 条, "
                      f"source_last_ts={source_last_ts}")

        # ── 步骤 5：合并 DB + 远端，归一化去重，远端覆盖 DB，写 DB ──
        if source:
            db_bars = self._merge_bars(db_bars, source)
            db_bars = _normalize_1d_bars(db_bars)
            self._save_to_db(symbol, db_bars)
            logger.debug(f"[kline_1d] {symbol} 合并后 {len(db_bars)} 条")

        # ── 步骤 6：三分支构造 out_kline ──
        if source_last_ts >= today_ts:
            # 远端包含今日数据 → 直接用合并后的 db_kline
            out_kline = db_bars
            logger.debug(f"[kline_1d] {symbol} source含今日, 直接用 db_kline")
        elif lastbar is not None:
            # 远端不含今日，但 lastbar 命中 → 追加 lastbar
            out_kline = db_bars + [lastbar]
            logger.debug(f"[kline_1d] {symbol} 追加 lastbar")
        else:
            # 都没有 → 调 get_ticker 走完整 ticker 流程
            self.get_ticker(symbol)
            quote = _snapshot_cache.get(symbol)
            ticker_bar = _bar_from_ticker(quote) if quote else None
            if ticker_bar:
                out_kline = db_bars + [ticker_bar]
                logger.debug(f"[kline_1d] {symbol} ticker 转 bar 追加")
            else:
                out_kline = db_bars
                logger.debug(f"[kline_1d] {symbol} ticker 也无数据, 只用 db")

        # 过滤时间窗 + 取最新 limit 条 + 前复权（统一收尾，勿在别处提前截断）
        out = self._finalize_1d(out_kline, symbol, limit, before_time, after_time)
        logger.debug(f"[kline_1d] {symbol} 最终返回 {len(out)} 条")
        return out

    def _finalize_1d(
        self,
        bars: List[Dict[str, Any]],
        symbol: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """1D 统一收尾：时间窗过滤 → 截断 → 前复权。

        顺序不可换：DB 读到的是**全量**日线，若先 `[-limit:]` 再过滤 before_time，
        历史窗口回补（如 before_time=两年前）会被截成"最近 limit 条"后整段滤空。
        """
        out = self.filter_and_limit(
            bars, limit=limit,
            before_time=before_time, after_time=after_time,
            truncate=(after_time is None),
        )
        return unadj_to_qfq(out, symbol) if out else out

    # ================================================================
    # 1W: limit 转 1D count → 走 1D 流程 → ISO 周聚合
    #
    # 为什么先走 1D 再聚合：
    #   1W 的数据本质是 5 个交易日的 OHLCV 聚合。
    #   直接从远端取周线可能不含今日盘中数据，
    #   但走 1D 流程可以利用 lastbar 拼出今日数据，聚合后周线也实时。
    # ================================================================

    def _get_kline_weekly(
        self,
        symbol: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """1W: 转成 1D count → 走 1D 流程 → ISO 周聚合。

        before_time / after_time 向下透传给 1D（保证聚合前的日线本就落在时间窗内，
        不会因"先截最近 N 根日线"而缺左侧），聚合后按周线粒度再过滤一次 ——
        过滤是幂等的，两次不会互相抵消（日线已复权，周线由前复权价聚合而来）。
        """
        # 每周约 5 个交易日，×7 留冗余保证聚合后数量充足
        daily_count = max(limit * 7, 60)

        # 复用 1D 流程（含 DB + lastbar 逻辑）
        daily_bars = self._get_kline_1d(symbol, daily_count, before_time, after_time)
        logger.debug(f"[kline_weekly] {symbol} 获取日线 {len(daily_bars)} 条")

        # 按 ISO 周聚合为周线
        out_kline = self._aggregate_weekly(daily_bars)

        # 周线粒度再收口（before 过滤 + limit 截断；周线已复权，无需再转）
        out = self.filter_and_limit(
            out_kline, limit=limit,
            before_time=before_time, after_time=after_time,
            truncate=(after_time is None),
        )
        logger.debug(f"[kline_weekly] {symbol} 聚合后返回 {len(out)} 条")
        return out

    # ================================================================
    # 分钟级 K 线（1m/5m/15m/30m/1H/2H/4H）——盘中/盘后同构
    #
    # 数据来源优先级：
    #   1. DB 1m 数据充足 → 直读（1m）或按需聚合（5m/15m/30m/1H/2H/4H）
    #   2. DB 缺当日（盘中任何时候 / 盘后 15:01 后 ETL ~15:30 未回填完）→ 用
    #      realtime_snapshot 合成当日 1m 段，与 DB 历史拼接后直读/聚合
    #      （rt 双闸门：首 5 槽有数据 + 中段连续缺 ≤QD_RT_MAX_GAP_SLOTS 根 +
    #       尾鲜度 ≤120s；不过闸不拼）
    #   3. snapshot 不可用（停牌 / 调度未跑 / 读失败 / 闸门不过）→ 远端兜底
    #      （coordinator）
    #   4. DB 1m 不足（超 4-5 个月）→ 单股远端
    #
    # 2026-10-24 前盘中一律走远端（分时线盘中拿不到完整 OHLCV 的旧论断已被
    # rt 合成实测证伪：口径与 mootdx 回填逐根一致，合成 3.2ms）；现取消盘中
    # 特判，本地优先 + 远端兜底（用户拍板）。
    #
    # 聚合规则：
    #   1m  → 直读
    #   5m  → 每 5 根 1m 聚合
    #   15m → 每 15 根 1m 聚合
    #   30m → 每 30 根 1m 聚合
    #   1h  → 每 60 根 1m 聚合
    #   2H  → 每 120 根 1m 聚合
    #   4h  → 每 240 根 1m 聚合
    # ================================================================

    # 目标周期对应的 1m bar 数
    # key 必须与 normalize_chart_timeframe 输出一致: "1H"/"2H"/"4H" 均大写
    _TF_1M_BAR_COUNT = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1H": 60,
        "2H": 120,
        "4H": 240,
    }

    def _get_kline_intraday(
        self, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """分钟线统一入口：取不复权原始价 → 统一前复权后返回（盘中/盘后同构）。

        为什么复权必须收口在这一层（同 symbol 同周期口径一致红线）：
          · DB 的 `kline_1m_YYYY` 存的是**不复权原始价**（原由 backfill_db/mootdx 直写，
            现由 cn_stock_putdb.sync_stock_kline 经 coordinate_market_kline 覆写，复权因子另存）；
          · 远端 provider 返回的同样是不复权价；
          ⇒ 若只在远端分支复权，除权股会出现「盘中查是前复权价 / 盘后查是原始价」
            的同周期价格跳变（同一支股跨时段口径漂移）。
        快照合成的当日段：前复权锚定最新日，当日因子为 1，此处转换等于原值，不引入偏差。
        """
        bars = self._get_kline_intraday_raw(symbol, tf, limit)
        return unadj_to_qfq(bars, symbol) if bars else bars

    def _get_kline_intraday_raw(
        self, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """1m 走 DB 直读，5m/15m/30m/1H/2H/4H 从 1m DB 按需聚合（返回**不复权原始价**）。

        DB 1m 数据只有 4-5 个月，超出范围的请求：
          - 单股：从远端获取（通过 coordinator）
          - 大批量：缩减到 DB 范围内

        DB 缺当日时（盘中任何时候 / 盘后 15:01 后 ETL ~15:30 未回填完），用
        realtime_snapshot 合成当日 1m 段与历史拼接 —— 替代此前"返回空 / 直走
        远端"；snapshot 也不可用（或 rt 闸门不过）时才回落远端。
        """
        # ── 盘后当日数据新鲜度守卫(并入主查询, 不额外多查一次) ──
        # 交易日 09:30 起即要求"当日必须有数据"：DB 最新 bar 不是今日 ⇒ 当日
        # 1m 尚未回填(约 15:30 ETL 完成)，直读/聚合都会把昨日数据当"最新"
        # 返回 → 视为 DB 过期, 触发 rt 合成；rt 不可用才走远端。
        # 盘前(<09:30)不触发：DB 的"昨日"数据即最新，今日分钟线本就未产生。
        # （旧口径仅盘后 15:01+ 触发 ⇒ 盘中永远拿不到当日合成段，只能走远端。）
        now = datetime.now()
        _fresh_required = (
            is_trading_day(now.strftime("%Y-%m-%d")) and now.time() >= dtime(9, 30)
        )

        def _db_stale(bars: List[Dict[str, Any]]) -> bool:
            if not _fresh_required or not bars:
                return False
            lt = bars[-1].get("time")
            ld = datetime.fromtimestamp(lt).date() if isinstance(lt, (int, float)) else lt.date()
            return ld != now.date()

        today_str = now.strftime("%Y-%m-%d")

        def _stitch_today(
            raw_bars: List[Dict[str, Any]], today_bars: List[Dict[str, Any]],
        ) -> List[Dict[str, Any]]:
            """剔除 DB 当日残留/部分回填 + 拼 rt 合成当日段（升序）。

            R4 收口：1m 直读分支与聚合分支曾各写一遍同型 history 过滤。
            """
            history = [
                b for b in raw_bars
                if datetime.fromtimestamp(b["time"]).strftime("%Y-%m-%d") != today_str
            ]
            return history + today_bars

        def _today_1m_from_snapshot() -> List[Dict[str, Any]]:
            """当日 1m 尾部（realtime_snapshot 合成）；不可用返回 []。

            双闸门（用户 2026-10-24 口径，见文件顶部常量区）：
              · `_rt_series_gap_ok`：首必须有数据、同段中间最多连续缺
                QD_RT_MAX_GAP_SLOTS 根（防空：合成序列出现看不见的洞）；
              · `_rt_freshness_ok`：尾鲜度 ≤120s / 盘后=15:00 末拍（防漏：
                采集器死掉后不把死数据当今日合成出去）。
            不过闸 ⇒ 返回 []，调用方走远端 coordinate 兜底（慢但完整）。
            """
            rows = self._read_snapshot_series(symbol, today_str)
            if not rows:
                return []
            times = [r.get("time") for r in rows
                     if isinstance(r.get("time"), datetime)]
            if not _rt_series_gap_ok(times):
                logger.debug(
                    f"[kline_1m] {symbol} rt 序列缺口超 "
                    f"QD_RT_MAX_GAP_SLOTS={QD_RT_MAX_GAP_SLOTS} 或首拍缺失，回落远端")
                return []
            if not _rt_freshness_ok(max(times), now):
                logger.debug(f"[kline_1m] {symbol} rt 末拍不新鲜，回落远端")
                return []
            return self._snapshot_ticks_to_1m(rows)

        # 1m 直读
        if tf == "1m":
            raw = self._read_db_1m(symbol, limit)
            if _db_stale(raw):
                today_bars = _today_1m_from_snapshot()
                if not today_bars:
                    return []                      # 无当日数据 → 维持不返回滞后数据
                return _stitch_today(raw, today_bars)[-limit:]
            return raw

        # 5m/15m/30m/1H/2H/4H → 从 1m 聚合
        bar_count = self._TF_1M_BAR_COUNT.get(tf, 15)
        # 计算需要多少 1m bar：limit 组 × 每组 bar_count 根，多留 1 天余量（240 根）
        need_bars = limit * bar_count + 240

        # 读取 1m bar
        raw_bars = self._read_db_1m(symbol, need_bars)

        if raw_bars:
            if _db_stale(raw_bars):
                today_bars = _today_1m_from_snapshot()
                if today_bars:
                    # DB 历史（剔除当日残留/部分回填）+ snapshot 当日 → 聚合
                    merged = _stitch_today(raw_bars, today_bars)
                    logger.debug(
                        f"[kline_{tf}] {symbol} DB 当日未回填, snapshot 合成 {len(today_bars)} 根"
                    )
                    return self._aggregate_from_1m(merged, bar_count)[-limit:]
                # snapshot 不可用 → 维持原行为: 不返回滞后聚合, 单股走远端(含当日)
                return self._fetch_intraday_remote(symbol, tf, limit)
            return self._aggregate_from_1m(raw_bars, bar_count)[-limit:]

        # DB 无数据 → 单股从远端获取
        return self._fetch_intraday_remote(symbol, tf, limit)

    def _read_db_1m(self, symbol: str, limit: int) -> List[Dict[str, Any]]:
        """从 DB 读取 1m K 线（不复权原始价），按时间升序返回（最多 limit 条）。

        读失败记 warning 并返回 []（不抛异常）：调用方 `_get_kline_intraday_raw`
        据此回落远端，属预期降级路径；但**不静默** —— 否则 DB 连接/表缺失会被
        当成"该股当天没有分钟数据"，排查时无迹可循。
        """
        db_symbol = strip_market_prefix(symbol) if symbol else symbol
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
            rows = writer.query(
                "CNStock", db_symbol, "1m",
                start_time=None, end_time=None, limit=limit,
            )
        except Exception as e:
            logger.warning(f"[DB读] {db_symbol}/1m 查询失败, 回落远端: {e}")
            return []

        return _rows_to_bars(rows)

    def _read_snapshot_lastbar(
        self, symbol: str, now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """从 realtime_snapshot 读最近一拍，转今日 1D bar（无效/无数据返回 None）。

        为什么优先于 TTL 行情快照（_bar_from_ticker）：
          a. 带 time 字段可校验日期 —— 陈旧快照（如周一拿着上周五的缓存）
             不会再被伪造成"今日 bar"（get_ticker 有 max_age=10 而 get_kline 的
             lastbar 链路此前没有，同一入口两种口径）；
          b. open/high/low 为当日累计值，完整可得（provider quote 的 HL 可能缺失）；
          c. 持久化 —— 收盘后 / 进程重启后仍可取到当日最后一拍。

        数据口径：盘中每 60s 一拍 UPSERT 全市场行情（PK symbol+time，保留 8 天），
        volume 单位"股"，与 kline 表一致（实测 600519 于 2026-10-15 与 kline_1D
        尾差仅 22 股，为 ETL 回填修正），拼接无需换算。

        有效性条件（全部满足才返 bar，否则 None ⇒ 调用方走 coordinate 兜底）：
          1. 今日是交易日；
          2. 最近一拍日期 == 今日；
          3. price > 0；
          4. **尾鲜度过闸**（`_rt_freshness_ok`，口径见文件顶部常量区）——
             盘中 now-末拍 ≤120s / 盘后末拍=15:00 / 午休末拍≈11:30。
             采集器死掉后末拍会停在死亡时刻，无此判据会把死数据当"今日最新"
             拼出去（防漏的核心：宁走远端兜底，不返陈旧当日）。
        rt 每拍 open/high/low 即当日累计值 ⇒ 单拍 = 全日 OHLCV，**无需缺口检查**
        （1m 全日序列才需要，见 `_rt_series_gap_ok`）。

        bar 的 time 归一化到当日 15:00，与 _normalize_1d_bars / _bar_from_ticker
        保持同一粒度。now 参数仅供调用方统一时刻注入（测试/多步流程），
        None 时取 datetime.now()。
        """
        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if not is_trading_day(today_str):
            return None
        # "last"/"previousClose" 是 PG 保留字, SQL 中必须加双引号
        rows = _query_snapshot_rows(
            symbol,
            'SELECT time, "last", open, high, low, volume '
            "FROM realtime_snapshot WHERE symbol = %s "
            "ORDER BY time DESC LIMIT 1",
            (), "最近一拍",
        )
        row = rows[0] if rows else None

        if not row:
            return None
        snap_time = row.get("time")
        if snap_time is None:
            return None
        snap_date = (
            snap_time.strftime("%Y-%m-%d")
            if isinstance(snap_time, datetime) else str(snap_time)[:10]
        )
        if snap_date != today_str:
            return None

        if not _rt_freshness_ok(snap_time, now):
            logger.debug(f"[snapshot] {strip_market_prefix(symbol)} 末拍 "
                         f"{snap_time} 不新鲜（now={now}），不伪造今日 bar")
            return None

        price = float(row.get("last") or 0)
        if price <= 0:
            return None
        bar_ts = _day_close_ts(today_str)
        return {
            "time": bar_ts,
            "open": float(row.get("open") or price),
            "high": float(row.get("high") or price),
            "low": float(row.get("low") or price),
            "close": price,
            "volume": float(row.get("volume") or 0),
        }


    def _fetch_intraday_remote(
        self, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """单股从远端获取分钟级 K 线（DB 无数据时的 fallback），返回**不复权原始价**。

        复权不在这里做：`_get_kline_intraday` 已对全部分支（DB 直读 / 聚合 / 远端）
        统一收口，此处再转一次会变成双重复权（因子 != 1 的除权股价格被二次压低）。
        """
        coord_result = get_coordinator().coordinate_kline(
            symbol=symbol,
            timeframe=tf,
            limit=limit,
            market="CNStock",
            timeout=20,
        )
        return coord_result.get("bars", []) if coord_result else []

    @staticmethod
    def _aggregate_from_1m(
        bars: List[Dict[str, Any]], bar_count: int,
    ) -> List[Dict[str, Any]]:
        """将 1m bar 按日期和固定数量聚合为更大周期。

        按日期分组后，每 bar_count 根合并为一根。
        注意: 1m 有 ±1 根/天容差，按日期分组比按数量分组更稳（规避缺 bar 偏移）。

        bar_count: 每组 1m bar 数（5m=5, 15m=15, 30m=30, 1H=60, 2H=120, 4H=240）
        """
        if not bars or bar_count <= 1:
            return bars

        result = []
        cur_date = None
        group: List[Dict[str, Any]] = []

        for bar in bars:
            d = datetime.fromtimestamp(bar["time"]).strftime("%Y-%m-%d")
            if d != cur_date:
                # 日期切换：先 emit 上一组剩余
                if group:
                    result.append(_merge_bars_group(group))
                cur_date = d
                group = [bar]
            else:
                group.append(bar)
            # 凑满一组就 emit
            if len(group) == bar_count:
                result.append(_merge_bars_group(group))
                group = []

        # 收尾
        if group:
            result.append(_merge_bars_group(group))

        return result

    # ================================================================
    # 远端直读（非 1D/1W 周期）—— 兜底路径
    #
    # 仅当本地全链路不可用（DB 读失败 / DB 1m 不足 4-5 个月 / rt 闸门不过）
    # 才走到这里。2026-10-24 前盘中一律走此路（旧论断「分时线盘中无法获取
    # 完整 OHLCV」已被 rt 合成实测证伪，见分钟级区块头）。
    # ================================================================

    def _get_kline_remote(
        self,
        symbol: str,
        tf: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """直接走 Coordinator 远程拉取（不做 DB 缓存）。"""
        remote_tf = normalize_chart_timeframe(tf)
        coord_result = get_coordinator().coordinate_kline(
            symbol=symbol,
            timeframe=remote_tf,
            limit=limit,
            market="CNStock",
            timeout=20,
        )
        bars = coord_result.get("bars", []) if coord_result else []
        if bars:
            bars = unadj_to_qfq(bars, symbol)

        return self.filter_and_limit(
            bars, limit=limit,
            before_time=before_time, after_time=after_time,
            truncate=(after_time is None),
        )

    # ================================================================
    # DB 操作（仅 1D 使用）
    # ================================================================




    def _read_snapshot_series(
        self, symbol: str, date_str: str,
    ) -> List[Dict[str, Any]]:
        """读 realtime_snapshot 指定日期全量 ticks（time 升序），返回 dict 行。

        行字段: time(datetime, naive 北京) / "last" / open / high / low / volume。
        时间窗取 09:00-16:00（覆盖 9:26 竞价拍至 15:01 收盘后补拍），走 PK
        (symbol, time) 索引；读失败 warning 并返回 []（不静默，区别于 DB 读的静默）。

        tuple→dict 转换收口在 `_query_snapshot_rows`（勿在调用点重复转换——
        `_read_snapshot_lastbar` 曾因此静默失效，见该 helper 注释）。

        ⚠️ 封装破损（审计登记，未处置）：`intraday_tools._fetch_snapshot_1m_day`
        跨模块调用本私有方法（单文件改动约束下不提公共 API）。若未来门面加
        缓存/限流，此处需同步评估——改签名前先查该调用方。
        """
        return _query_snapshot_rows(
            symbol,
            'SELECT time, "last", open, high, low, volume '
            "FROM realtime_snapshot WHERE symbol = %s "
            "AND time >= %s AND time < %s ORDER BY time",
            (f"{date_str} 09:00:00", f"{date_str} 16:00:00"),
            f"{date_str} 序列",
        )


    @staticmethod
    def _snapshot_ticks_to_1m(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """realtime_snapshot ticks → 当日 1m bar 序列（time 升序，与 kline_1m 同口径）。

        ── 数据口径（600519@2026-10-15 实测实证）──
          · 每 ~60s 一拍但秒位不齐（:56/:29/:26），首拍 9:26 竞价、末拍 15:01；
          · open = 当日开盘价（全日恒定）；high / low / volume = **当日累计值**，
            不是每拍区间，故不能照抄 auto hub.prep_minutes 的 o/h/l 直取；
          · kline_1m 标签惯例（mootdx 回填）：全日 240 根，上午 09:31..11:29
            + 下午 13:00..15:00；本实现统一按「标签 = 所属分钟 + 1」生成
            09:31..11:30 + 13:01..15:00（上午/下午首尾各差 1 个标签位，
            属收盘/开盘边界口径差，量价不受影响 —— 成交额与 DB 实测 0 偏差）。

        ── 转换规则 ──
          · 标签 = tick 分钟 + 1（≤09:30 → 09:31；11:30 → 11:30；
            午休 11:31-13:00 丢弃；15:00/15:01 → 15:00）；同标签多拍取最后一拍；
          · volume = 累计差分（与 auto hub.prep_minutes(volume_cumulative=True) 一致）；
          · o = 前收（首根取当日 open）；h/l 在累计极值刷新日高/低时取该极值
            （此时即为该分钟真实极值），否则退化为 max/min(o, c) ——
            60s 拍无法还原分钟内完整区间，此规则与 mootdx 回填逐根比对一致；
          · 缺拍槽位不补（与 auto gap-tolerant 口径一致，聚合按组容错）。

        Returns:
            [{"time": unix_ts, "open":, "high":, "low":, "close":, "volume":}, ...]
            无效（无 price / 全停牌）返回 []。
        """
        if not rows:
            return []

        day_open = float(rows[0].get("open") or 0) or None

        # 第一遍: tick → 分钟标签分组（time 升序，同标签后者覆盖前者 = 取末拍）
        slots: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        for r in rows:
            t = r.get("time")
            if not isinstance(t, datetime):
                continue
            m = t.hour * 60 + t.minute
            if m <= 570:                      # ≤09:30（含 9:26 竞价拍）→ 首根 09:31
                lm = 571
            elif m <= 689:                    # 09:31-11:29 → +1（09:32..11:30）
                lm = m + 1
            elif m <= 779:                    # 11:30 收盘后拍并入上午末根; 午休丢弃
                lm = 690 if m == 690 else None
            elif m == 780:                    # 13:00 是下午**开盘分钟**(非午休!) → 13:01
                lm = 781                      # 易错: 归入午休会整拍丢弃, 少一根且量被并到下一根
            elif m <= 899:                    # 13:01-14:59 → +1（13:02..15:00）
                lm = m + 1
            else:                             # 15:00 / 15:01 收盘拍 → 末根 15:00
                lm = 900
            if lm is None:
                continue
            slots[f"{lm // 60:02d}:{lm % 60:02d}"] = (lm, r)

        if not slots:
            return []

        # 第二遍: 按标签顺序合成 bar
        _beijing = ZoneInfo("Asia/Shanghai")
        bars: List[Dict[str, Any]] = []
        prev_close = day_open
        prev_cum_high = 0.0
        prev_cum_low = float("inf")
        prev_cum_vol = 0.0

        for _key in sorted(slots):            # "HH:MM" 零填充，字典序即时间序
            lm, r = slots[_key]
            t: datetime = r["time"]
            price = float(r.get("last") or 0)
            if price <= 0:
                continue                      # 无效拍（停牌等）
            cum_vol = float(r.get("volume") or 0)
            cum_high = float(r.get("high") or 0)
            cum_low = float(r.get("low") or 0)

            o = prev_close if prev_close else price
            c = price
            v = max(0.0, cum_vol - prev_cum_vol)
            h = cum_high if cum_high > prev_cum_high else max(o, c)
            l = cum_low if cum_low < prev_cum_low else min(o, c)

            bar_ts = int(
                t.replace(
                    hour=lm // 60, minute=lm % 60, second=0, microsecond=0,
                ).replace(tzinfo=_beijing).timestamp()
            )
            bars.append({
                "time": bar_ts,
                "open": o, "high": h, "low": l, "close": c, "volume": v,
            })
            prev_close = c
            prev_cum_vol = cum_vol
            prev_cum_high = max(prev_cum_high, cum_high)
            prev_cum_low = min(prev_cum_low, cum_low)

        return bars

    def _read_db(
        self,
        symbol: str,
        limit: Optional[int] = None,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """从 DB 读取 1D K 线（不复权原始价），按时间升序返回。

        下推参数（2026-10-24 提速；调用方不传 = 旧行为全量读）：
          · limit：快路径只需最近 limit 条（+lastbar 拼接）⇒ SQL 每表
            DESC LIMIT 下推；慢路径（远端合并去重）需完整历史，传 None；
          · before_time / after_time：Unix 秒（K 线 time 口径），下推为
            SQL 时间窗，与 `_finalize_1d` 收尾的过滤语义精确等价：
            - after_time 是闭区间（`>=`）⇒ start_time 直传；
            - before_time 是开区间（`<`）而 query 的 end_time 是闭区间，
              直接传会多取 before 那一根——LIMIT 下推后 finalize 再滤掉它
               ⇒ 只剩 n-1 根（漏数据）。故 end_time 传 before-1s
               （bar time 为整秒，1D 间隔远大于 1s，不误伤前一根）；
            - after_time 非空 ⇒ 不下推 limit（回测要 [after, before) 整段，
              截断会丢左端——与 filter_and_limit 的 truncate=False 同语义）。

        两处失败均记 warning 后返回 []（不抛异常）：调用方 `_get_kline_1d` 据此
        判定"DB 无数据"走远端慢路径 —— 是预期降级，但不可静默，否则 DB 不可用会
        表现为"日线一直很旧"，与"远端也没数据"无法区分。
        """
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
        except Exception as e:
            logger.warning(f"[DB读] {symbol}/1D writer 初始化失败: {e}")
            return []

        # epoch 秒（北京语义）→ naive 北京 datetime：DB time 列是 naive 北京时间，
        # 不能走 db_market._ensure_datetime 的 UTC 推导（会偏 8 小时）。
        _beijing = ZoneInfo("Asia/Shanghai")

        def _bj_naive(ts: int) -> datetime:
            return datetime.fromtimestamp(ts, tz=_beijing).replace(tzinfo=None)

        start_dt = _bj_naive(after_time) if after_time is not None else None
        end_dt = (
            _bj_naive(before_time) - timedelta(seconds=1)
            if before_time is not None else None
        )
        # after_time 场景要整段 ⇒ 不下推 limit（truncate=False 语义）
        qlimit = None if after_time is not None else limit

        db_symbol = strip_market_prefix(symbol) if symbol else symbol
        try:
            rows = writer.query(
                "CNStock", db_symbol, "1D",
                start_time=start_dt, end_time=end_dt, limit=qlimit,
            )
        except Exception as e:
            logger.warning(f"[DB读] {db_symbol}/1D 查询失败, 走远端慢路径: {e}")
            return []

        return _rows_to_bars(rows)

    def _save_to_db(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        """写入 1D DB（upsert 覆盖同时间 bar）。"""
        if not bars:
            return
        try:
            from app.utils.db_market import get_market_kline_writer, normalize_1d_time
            writer = get_market_kline_writer()
            db_symbol = strip_market_prefix(symbol) if symbol else symbol
            _beijing = ZoneInfo("Asia/Shanghai")
            db_bars = []
            for b in bars:
                bar = dict(b)
                t = bar.get("time")
                if isinstance(t, (int, float)):
                    # Convert epoch to naive Beijing time for DB storage
                    bar["time"] = datetime.fromtimestamp(t, tz=_beijing).replace(tzinfo=None)
                # 写库统一为 15:00:00（1D 收盘时间），与 kline_1D 其它写入路径一致
                bar["time"] = normalize_1d_time(bar["time"])
                db_bars.append(bar)
            writer.upsert("CNStock", db_symbol, "1D", db_bars)
            logger.debug(f"[DB写入] {db_symbol}/1D 写入 {len(db_bars)} 条")
        except Exception as e:
            logger.debug(f"[DB写入] {symbol}/1D 失败: {e}")

    def _fetch_remote_1d(self, symbol: str, count: int) -> List[Dict[str, Any]]:
        """从远端拉取 1D K 线，归一化 time 为 int 时间戳。"""
        coord_result = get_coordinator().coordinate_kline(
            symbol=symbol,
            timeframe="1D",
            limit=count,
            market="CNStock",
            timeout=30,
        )
        bars = coord_result.get("bars", []) if coord_result else []
        if not bars:
            return []

        # 归一化 time 为 int 时间戳（远端返回格式不统一，可能是 str/datetime/int）
        for bar in bars:
            t = bar.get("time")
            if isinstance(t, str):
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        bar["time"] = int(datetime.strptime(t.strip(), fmt).timestamp())
                        break
                    except ValueError:
                        continue
            elif isinstance(t, datetime):
                bar["time"] = int(t.timestamp())

        bars.sort(key=lambda b: b["time"])
        bars = _normalize_1d_bars(bars)
        return bars

    # ================================================================
    # 合并去重（远端优先覆盖 DB）
    #
    # 策略：以 time 字段为 key 做 dict 去重
    #   - 先放 DB 数据
    #   - 再放远端数据（同 time 的 bar 会被远端覆盖）
    #   - 这样远端的修正会自动覆盖 DB 中的误差数据
    # ================================================================

    @staticmethod
    def _merge_bars(
        db_bars: List[Dict[str, Any]],
        remote_bars: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """合并 DB + 远端 K 线，远端优先，按时间去重。"""
        by_time: Dict[int, Dict[str, Any]] = {}
        for bar in db_bars:
            by_time[bar["time"]] = bar
        for bar in remote_bars:
            by_time[bar["time"]] = bar  # 远端覆盖 DB
        return sorted(by_time.values(), key=lambda b: b["time"])

    # ================================================================
    # 聚合（周线专用）
    #
    # ISO 周聚合规则：
    #   - 按 isocalendar() 的 (year, week) 分组
    #   - open  = 该周第一条的 open
    #   - high  = 该周所有 high 的最大值
    #   - low   = 该周所有 low 的最小值
    #   - close = 该周最后一条的 close
    #   - volume = 该周所有 volume 之和
    #   - time  = 该周周一 00:00 的时间戳
    # ================================================================

    @staticmethod
    def _aggregate_weekly(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 ISO 周聚合日线为周线。"""
        if not bars:
            return []

        weeks: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for bar in bars:
            dt = datetime.fromtimestamp(bar["time"])
            iso = dt.isocalendar()
            key = (iso[0], iso[1])
            weeks.setdefault(key, []).append(bar)

        result = []
        for key in sorted(weeks.keys()):
            group = weeks[key]
            group.sort(key=lambda b: b["time"])
            year, week = key
            monday = datetime.fromisocalendar(year, week, 1)
            monday_ts = int(monday.replace(hour=0, minute=0, second=0).timestamp())
            result.append({
                "time": monday_ts,
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": round(sum(b["volume"] for b in group), 2),
            })
        return result
