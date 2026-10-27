# -*- coding: utf-8 -*-
"""交易日历（可替换数据源版）

=== 为什么重写 ===
原实现有两个结构性缺陷（2026-10-26 审计）：
  1. **永不刷新**：`_load()` 只要 pickle 存在就直接返回并置 `_loaded`，
     之后整个进程生命周期都不再校验覆盖范围。实测缓存停在 2026-12-31
     （2026-09-08 抓取）⇒ 2027-01-01 起 `is_trading_day` 对所有新日期返回
     False，`next_trading_day` 会一路走到兜底分支逐天扫描。
  2. **单源无回退**：只有 akshare 一个源，且 `_fetch_and_save` 抛错会直接
     冒泡到 23 个调用点（调度器、回补、K线闸门都依赖它）。

=== 架构位置（按 v3 的分层）===
    业务层（scheduler / backfill / cn_stock / evaluator …）→ 只调本模块公共 API
        ↓  签名完全冻结（8 个函数一个没改）
    数据源适配层（本文件）—— 持有"日历"这一能力的口径
        ↓  按顺序尝试候选源
    源 1: akshare（远端权威）     源 2: 持久化快照（离线兜底）
        ↓
    缓存：单文件，带 fetched_at / coverage_end 元数据，原子写

=== 三态（对齐 v3 §3.2）===
  · 命中且新鲜           → 正常返回
  · 刷新失败但有覆盖     → 用快照 + degraded=True + 告警一次（**不静默**）
  · 刷新失败且无覆盖     → 抛 CalendarUnavailable（显式失败，不假装"非交易日"）

公共 API 保持原样；新增 `calendar_status()` 供调度层观测，不影响任何旧调用。
"""

from __future__ import annotations

import logging
import os
import pickle
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── 缓存位置与策略 ──────────────────────────────────────────────────
_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trading_calendar"
)
_CACHE_FILE = os.path.join(_DIR, "trading_days.pkl")
_CACHE_VERSION = 2

#: 缓存必须覆盖到"今天 + 这么多天"，否则视为不可用（调度要往后看）。
#: 这是**主触发条件**——也是原实现真正的 bug（缓存停在 2026-12-31，元旦后失效）。
_LOOKAHEAD_DAYS = 30
#: 覆盖足够但太久没更新时的兜底刷新（半年）。
#: 不用短周期：akshare 是联网调用，原先设计就是为了避开启动时联网；
#: 每 7 天刷一次会让每次重启多一次网络依赖，是可用性回归。
_REFRESH_AFTER_DAYS = 180

# ── 进程内状态 ──────────────────────────────────────────────────────
_cached_dates: Set[str] = set()
_sorted_dates: List[str] = []
_loaded = False
_status: Dict[str, object] = {
    "source": "", "fetched_at": "", "coverage_end": "", "degraded": False,
}


class CalendarUnavailable(RuntimeError):
    """所有日历源都不可用且无可用快照。

    调用方（调度器/回补）应把它当"基础设施不可用"处理，而不是"今天不是交易日"。
    """


# ── 源 1：akshare（远端）──────────────────────────────────────────

def _from_akshare() -> Set[str]:
    """从 akshare 拉取沪深交易日（2000 年起）。

    注意：`akshare` 在测试里被 conftest mock 成空模块 ⇒ 调用会抛 AttributeError，
    这正是我们想测的"源失败"路径。
    """
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    dates: Set[str] = set()
    for val in df["trade_date"]:
        s = str(val).strip()
        if len(s) == 8 and s.isdigit():
            s = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if s >= "2000-01-01":
            dates.add(s)
    if not dates:
        raise CalendarUnavailable("akshare 返回空交易日历")
    return dates


# ── 源 2：持久化快照（离线兜底）───────────────────────────────────

def _read_cache() -> Tuple[Optional[Set[str]], str, str]:
    """读取缓存，返回 (dates, fetched_at, coverage_end)。

    兼容 v1 的裸 `set` 格式：此时 fetched_at 取文件 mtime、coverage_end 取
    最大日期 —— 老部署升级上来能直接用，不必等一次联网。
    """
    if not os.path.isfile(_CACHE_FILE):
        return None, "", ""
    try:
        with open(_CACHE_FILE, "rb") as f:
            payload = pickle.load(f)
    except Exception as e:  # 损坏等同于没有
        logger.warning("[calendar] 缓存读取失败，视为无缓存: %s", e)
        return None, "", ""

    if isinstance(payload, dict) and payload.get("version") == _CACHE_VERSION:
        dates = set(payload.get("dates") or ())
        if not dates:
            return None, "", ""
        return dates, str(payload.get("fetched_at") or ""), str(payload.get("coverage_end") or max(dates))

    if isinstance(payload, (set, frozenset, list, tuple)):
        dates = {str(x) for x in payload}
        if not dates:
            return None, "", ""
        mtime = datetime.fromtimestamp(os.path.getmtime(_CACHE_FILE)).strftime("%Y-%m-%d")
        return dates, mtime, max(dates)

    logger.warning("[calendar] 缓存格式无法识别（%s），视为无缓存", type(payload).__name__)
    return None, "", ""


def _write_cache(dates: Set[str]) -> None:
    """原子写：先写 .tmp 再 replace，避免进程被杀时留下半截文件。"""
    os.makedirs(_DIR, exist_ok=True)
    payload = {
        "version": _CACHE_VERSION,
        "dates": dates,
        "fetched_at": datetime.now().strftime("%Y-%m-%d"),
        "coverage_end": max(dates),
    }
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, _CACHE_FILE)


# ── 选源与刷新 ─────────────────────────────────────────────────────

def _candidates() -> List[Tuple[str, Callable[[], Optional[Set[str]]]]]:
    """候选源，按优先级。新增源只改这里，不动调用方。"""
    return [("akshare", _from_akshare)]


def _needs_refresh(fetched_at: str, coverage_end: str) -> bool:
    today = datetime.now().date()
    if not coverage_end:
        return True
    try:
        if datetime.strptime(coverage_end, "%Y-%m-%d").date() < today + timedelta(days=_LOOKAHEAD_DAYS):
            return True
    except ValueError:
        return True
    if fetched_at:
        try:
            age = (today - datetime.strptime(fetched_at, "%Y-%m-%d").date()).days
            return age > _REFRESH_AFTER_DAYS
        except ValueError:
            return True
    return True


def _install(dates: Set[str], source: str, fetched_at: str, degraded: bool) -> None:
    global _cached_dates, _sorted_dates, _loaded, _status
    _cached_dates = dates
    _sorted_dates = sorted(dates)
    _loaded = True
    _status = {
        "source": source,
        "fetched_at": fetched_at,
        "coverage_end": max(dates) if dates else "",
        "degraded": degraded,
    }


def _load() -> Set[str]:
    """加载交易日集合：新鲜则用缓存，否则按候选顺序刷新，失败则降级。"""
    global _loaded

    if _loaded:
        return _cached_dates

    cached, fetched_at, coverage_end = _read_cache()
    if cached and not _needs_refresh(fetched_at, coverage_end):
        _install(cached, "cache", fetched_at, degraded=False)
        logger.info("[calendar] 命中缓存（抓取于 %s，覆盖至 %s，共 %d 天）",
                    fetched_at or "?", coverage_end, len(cached))
        return _cached_dates

    errors = []
    for name, fetch in _candidates():
        try:
            dates = fetch()
        except Exception as e:  # 源故障不冒泡，交给下一源
            errors.append("%s: %s" % (name, e))
            logger.warning("[calendar] 源 %s 失败: %s", name, e)
            continue
        if not dates:
            errors.append("%s: 空结果" % name)
            continue
        try:
            _write_cache(dates)
        except Exception as e:  # 写盘失败不影响本次使用
            logger.warning("[calendar] 缓存写入失败（本次仍可用）: %s", e)
        _install(dates, name, datetime.now().strftime("%Y-%m-%d"), degraded=False)
        logger.info("[calendar] 已从 %s 刷新，共 %d 天，覆盖至 %s",
                    name, len(dates), max(dates))
        return _cached_dates

    # 所有源都失败 → 用快照（若覆盖够）
    if cached:
        covered = coverage_end >= datetime.now().strftime("%Y-%m-%d")
        _install(cached, "cache", fetched_at, degraded=True)
        logger.warning(
            "[calendar] 全部源失败，降级使用 %s 的快照（覆盖至 %s，%s）: %s",
            fetched_at or "?", coverage_end,
            "覆盖当前日期" if covered else "★已过期，超出范围的日期会判为 False",
            "; ".join(errors),
        )
        return _cached_dates

    raise CalendarUnavailable(
        "交易日历不可用：无缓存且所有源失败（%s）" % "; ".join(errors)
    )


def calendar_status() -> Dict[str, object]:
    """日历状态（供调度层/健康检查观测）。不改变任何既有行为。"""
    _load()
    return dict(_status)


# ─── 公共 API（签名与返回类型保持 2026-10-26 之前完全一致）────────────


def is_trading_day(date: str) -> bool:
    """判断是否为交易日 (YYYY-MM-DD 或 YYYYMMDD)"""
    if len(date) == 8 and date.isdigit():
        date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return date in _load()


def is_trading_day_today() -> bool:
    return is_trading_day(datetime.now().strftime("%Y-%m-%d"))


def prev_trading_day(date: Optional[str] = None, n: int = 1) -> str:
    """前 n 个交易日"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    _load()
    result: List[str] = []
    for d in reversed(_sorted_dates):          # 已排序缓存，避免每次 sorted()
        if d < date:
            result.append(d)
            if len(result) == n:
                return result[-1]

    # 数据不够（极端情况），逐天回退
    dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
    while len(result) < n:
        s = dt.strftime("%Y-%m-%d")
        if s in _cached_dates:
            result.append(s)
        dt -= timedelta(days=1)
    return result[-1]


def next_trading_day(date: Optional[str] = None, n: int = 1) -> str:
    """后 n 个交易日"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    _load()
    result: List[str] = []
    for d in _sorted_dates:
        if d > date:
            result.append(d)
            if len(result) == n:
                return result[-1]

    dt = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
    while len(result) < n:
        s = dt.strftime("%Y-%m-%d")
        if s in _cached_dates:
            result.append(s)
        dt += timedelta(days=1)
    return result[-1]


def last_finish_trading_day(ref_dt: Optional[str] = None) -> str:
    """返回已经结束的最近一个交易日。

    以 ref_dt 为参考点（默认当前时间）：
      - ref_dt 所在日期是交易日，且时间 >= 15:00:00 → 返回该日期
      - 否则（非交易日，或交易日但 < 15:00:00）→ 返回上一个交易日
    """
    if ref_dt is None:
        dt = datetime.now()
    else:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ref_dt, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"无法解析日期字符串: {ref_dt}")

    ref_date = dt.strftime("%Y-%m-%d")
    cutoff = dt.replace(hour=15, minute=0, second=0, microsecond=0)

    if is_trading_day(ref_date) and dt >= cutoff:
        return ref_date
    return prev_trading_day(ref_date)


def trade_date_range(start_date: str, end_date: str) -> List[str]:
    """范围内的交易日列表"""
    _load()
    return [d for d in _sorted_dates if start_date <= d <= end_date]


def trading_days_count(start_date: str, end_date: str) -> int:
    return len(trade_date_range(start_date, end_date))


def is_business_day(date: str) -> bool:
    return is_trading_day(date)
