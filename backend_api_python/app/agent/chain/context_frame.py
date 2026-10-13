# -*- coding: utf-8 -*-
"""C 类情境档案：每日盘后落一帧（v5 阶段 D1，2026-10-13）。

【为什么必须现在开始记，而不是先设计表】
v5 §19.3 / v4 §21.2：市场 L0、板块 L1 L2 只能按天积累，**不能着急**。
而 C 类数据是**不可重建**的 —— 今天没记，明天就永远补不回来（不像行情能回拉）。
所以本模块的第一原则是「**先落盘，后进统计**」：先保证数据开始流且不丢，
不做任何分析、不建表。等攒够一天一帧，再谈条件格。

【落盘位置为什么不是 tmp/】
计划原文写「走项目既有 tmp/ 约定」，**此处有意偏离**：tmp/ 是临时目录、会被清理，
而 C 类不可重建 ⇒ 放在会被清的地方等于白记。改落 `data/context_frames/`。
（`QD_CF_DIR` 可覆盖；运维若要另行挂载持久卷，改这个 env 即可。）

【字段与数据源（均为 2026-10-13 实测，勿凭记忆改）】
    as_of          最近**已收盘**交易日（见下方"未开盘陷阱"）
    index_ret      上证当日涨跌幅   ← index_tools.get_index_quote.change_percent
    turnover       上证成交额(元)   ← index_tools.get_index_quote.amount
    limit_up_cnt   涨停家数         ← dragon_tools.get_limit_pool(date=).zt.count
    vol_bucket     20日年化波动分档 ← index_tools.get_index_kline 的收盘序列
    regime         市场状态分档     ← 收盘 vs MA20 + 20日涨跌方向
    theme_heat     题材热度分档     ← sector_analysis_tools.get_hot_sectors 头部涨幅均值

【★ 未开盘陷阱（实测，2026-10-13 01:15）】
凌晨跑时 `get_limit_pool` / `get_hot_rank` / `get_dragon_tiger` **全部返回 count=0**
（今天还没开盘）。若拿「今天」当 as_of，档案里就会记下「某日 0 只涨停」，
那是个**假的**事实 —— 真相是「当日尚未开盘」。
⇒ 默认 as_of 取**最近已收盘交易日**：当日为交易日且已过收盘时刻才用今天，
  否则回退到上一交易日。收盘时刻走 `QD_CF_CLOSE_HM`（默认 15:05，留落地余量）。

【★ 红线：取不到就是 None + reason，绝不填 0】
把「没取到」写成 0，下游会把「今天 0 只涨停」当事实 —— 那是**另一个**事实。
所有缺失字段会在 frame['missing'] 里写明原因，可事后区分「真为 0」与「没取到」。

【易误解易错处】
· 分档阈值全部走 env（禁止硬编码），改 env 必须改结果。
· `sources` 参数仅供测试注入（离线、可控）；生产走 `_default_sources()`。
· 同日重跑会**覆盖**同 as_of 的旧帧，不是追加 —— 盘后任务会被重跑/补跑。
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..log import logger

SCHEMA_VER = "cf1"

# 落盘目录：默认 <backend_api_python>/data/context_frames（不用 tmp/，见模块头）
# ★ parents 级数：context_frame.py 在 app/agent/chain/ 下 ⇒
#   [0]=chain [1]=agent [2]=app [3]=backend_api_python。**别数错成 parents[2]**，
#   那会落到 app/data/（与既有的 backend_api_python/data/ 不是一个目录）。
FRAMES_DIR = os.getenv("QD_CF_DIR") or str(
    Path(__file__).resolve().parents[3] / "data" / "context_frames")

# 必需字段（缺一个就少一个条件维度）
REQUIRED_FIELDS: Tuple[str, ...] = (
    "as_of", "regime", "theme_heat", "vol_bucket",
    "index_ret", "limit_up_cnt", "turnover",
)

# ── 默认阈值（全部可被 env 覆盖；这里的数字只是"没配时的起点"，不是业务常量）
_VOL_LOW = float(os.getenv("QD_CF_VOL_LOW", "0.12"))     # 年化波动 ≤ 低档 ⇒ low
_VOL_HIGH = float(os.getenv("QD_CF_VOL_HIGH", "0.22"))   # 年化波动 ≥ 高档 ⇒ high
_HEAT_LOW = float(os.getenv("QD_CF_HEAT_LOW", "0.5"))    # 头部板块均涨幅(pct)
_HEAT_HIGH = float(os.getenv("QD_CF_HEAT_HIGH", "1.5"))
_INDEX_CODE = os.getenv("QD_CF_INDEX_CODE", "000001")    # 上证
_KLINE_DAYS = int(os.getenv("QD_CF_KLINE_DAYS", "30"))
_CLOSE_HM = os.getenv("QD_CF_CLOSE_HM", "15:05")         # 收盘时刻（留 5 分钟落地余量）
_TRADING_DAYS_PER_YEAR = 243                             # A 股年交易日（年化用）


def _env_f(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def frame_path(as_of: date) -> Path:
    """按月分文件：context_frames_YYYY-MM.jsonl（一年 12 个文件，不至于太碎）。"""
    return Path(FRAMES_DIR) / f"context_frames_{as_of.year}-{as_of.month:02d}.jsonl"


# ═══════════════════════════════════════════════════════════════
#  as_of：最近已收盘交易日
# ═══════════════════════════════════════════════════════════════
def _parse_hm(s: str) -> int:
    try:
        h, m = str(s or "15:05").split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError, IndexError):
        return 15 * 60 + 5


def last_closed_trading_day(now: Optional[datetime] = None) -> str:
    """返回最近**已收盘**的交易日（ISO 日期字符串）。

    当日是交易日且当前已过 `QD_CF_CLOSE_HM` ⇒ 用今天；否则回退上一交易日。
    """
    from app.utils import trading_calendar as tc
    now = now or datetime.now()
    today = now.date().isoformat()
    try:
        is_td = tc.is_trading_day(today)
    except Exception:
        is_td = True                      # 日历不可用 ⇒ 不因此拒绝落帧
    if is_td and (now.hour * 60 + now.minute) >= _parse_hm(_CLOSE_HM):
        return today
    try:
        return tc.prev_trading_day(today, 1)
    except Exception:
        return today


# ═══════════════════════════════════════════════════════════════
#  数据源（生产）
# ═══════════════════════════════════════════════════════════════
def _default_sources() -> Dict[str, Callable[..., Any]]:
    """生产数据源。返回结构已做容错解析（真源有 `data` 包裹层，测试假源没有）。"""
    # ⚠ 2026-10-13 事故修复：此处原本写 `app.agent.tools.finance`（改名前 app/agent
    # 就是本包，改名后 app/agent 变成新版骨架且**没有 finance 子包**）⇒ ImportError
    # 且 `_default_sources()` 没有 try/except ⇒ 整条 context_frame 链路直接抛异常、
    # 全量测试零覆盖所以无人发现。必须用相对导入锚回本包自己的 tools/finance。
    from ..tools.finance import (dragon_tools, index_tools,
                                 sector_analysis_tools)

    def _quote(code: str) -> Optional[dict]:
        try:
            r = index_tools.get_index_quote(code, scope="single")
        except Exception as e:
            logger.debug("[context_frame] 指数行情失败: %s", e)
            return None
        return _unwrap(r, code)

    def _kline(code: str, days: int) -> Optional[dict]:
        try:
            r = index_tools.get_index_kline(code, days)
        except Exception as e:
            logger.debug("[context_frame] 指数K线失败: %s", e)
            return None
        node = _unwrap(r, code) or {}
        bars = node.get("bars") or []
        return {"bars": bars} if bars else None

    def _limit(d: str) -> Optional[dict]:
        try:
            r = dragon_tools.get_limit_pool(date=d, pool_type="zt")
        except Exception as e:
            logger.debug("[context_frame] 涨停池失败: %s", e)
            return None
        if not isinstance(r, dict):
            return None
        zt = r.get("zt") or {}
        if zt.get("count") is None:
            return None               # 拿不到家数 ⇒ None，绝不当 0
        return {"zt": {"count": zt.get("count")}}

    def _hot() -> Optional[dict]:
        try:
            r = sector_analysis_tools.get_hot_sectors()
        except Exception as e:
            logger.debug("[context_frame] 热门板块失败: %s", e)
            return None
        if not isinstance(r, dict):
            return None
        if not (r.get("industry") or r.get("concept")):
            return None
        return r

    return {"index_quote": _quote, "index_kline": _kline,
            "limit_pool": _limit, "hot_sectors": _hot}


def _unwrap(r: Any, code: str) -> Optional[dict]:
    """真源有时是 {data:{code:{...}}} 有时是扁平 dict —— 两种都吃掉。"""
    if not isinstance(r, dict):
        return None
    if "data" in r and isinstance(r.get("data"), dict):
        inner = r["data"].get(code)
        if isinstance(inner, dict):
            return inner
        for v in r["data"].values():
            if isinstance(v, dict):
                return v
    return r


# ═══════════════════════════════════════════════════════════════
#  构建一帧
# ═══════════════════════════════════════════════════════════════
def build_frame(as_of: Optional[date] = None,
                sources: Optional[Dict[str, Callable[..., Any]]] = None,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """构建 C 类情境帧。**不做任何网络 I/O 之外的判断**，取不到就记 None + reason。"""
    src = sources or _default_sources()
    as_of_s = as_of.isoformat() if isinstance(as_of, date) else \
        (str(as_of)[:10] if as_of else last_closed_trading_day(now))
    missing: Dict[str, str] = {}

    def _call(key: str, *a):
        fn = src.get(key)
        if fn is None:
            return None
        try:
            return fn(*a)
        except Exception as e:
            missing[key] = f"{type(e).__name__}: {e}"
            return None

    # ── 指数当日
    q = _call("index_quote", _INDEX_CODE)
    index_ret = _f(q, "change_percent")
    turnover = _f(q, "amount")
    if index_ret is None:
        missing["index_ret"] = missing.get("index_quote") or "指数行情不可用"
    if turnover is None:
        missing["turnover"] = missing.get("index_quote") or "指数行情无成交额"

    # ── 涨停家数
    lp = _call("limit_pool", as_of_s)
    cnt = None
    if isinstance(lp, dict):
        zt = lp.get("zt") or {}
        _raw = _f(zt, "count")
        # 家数是**计数**，不是度量：转 int，别让它以 46.0 的形式进档案
        cnt = int(_raw) if _raw is not None else None
    if cnt is None:
        missing["limit_up_cnt"] = missing.get("limit_pool") or "涨停池不可用"

    # ── K 线 → 波动分档 + 市场状态
    kl = _call("index_kline", _INDEX_CODE, _KLINE_DAYS)
    closes = _closes(kl)
    vol_bucket = _vol_bucket(closes) if len(closes) >= 3 else None
    regime = _regime(closes) if len(closes) >= 3 else None
    if vol_bucket is None:
        missing["vol_bucket"] = missing.get("index_kline") or "K线不足，无法估波动"
    if regime is None:
        missing["regime"] = missing.get("index_kline") or "K线不足，无法判状态"

    # ── 题材热度
    hs = _call("hot_sectors")
    theme_heat = _theme_heat(hs)
    if theme_heat is None:
        missing["theme_heat"] = missing.get("hot_sectors") or "热门板块不可用"

    return {
        "schema_ver": SCHEMA_VER,
        "as_of": as_of_s,
        "collected_at": (now or datetime.now()).isoformat(timespec="seconds"),
        # 语义：该帧来自**已收盘**交易日，可进统计。若将来支持盘中落帧，此处用于区分。
        "market_closed": True,
        "regime": regime,
        "theme_heat": theme_heat,
        "vol_bucket": vol_bucket,
        "index_ret": index_ret,
        "limit_up_cnt": cnt,
        "turnover": turnover,
        "index_code": _INDEX_CODE,
        "missing": missing,
    }


def _f(d: Any, key: str) -> Optional[float]:
    """取数值；非数值/缺失一律 None（**绝不当 0**）。"""
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _closes(kl: Any) -> List[float]:
    if not isinstance(kl, dict):
        return []
    out = []
    for b in (kl.get("bars") or []):
        if isinstance(b, dict):
            v = _f(b, "close")
            if v is not None:
                out.append(v)
    return out


def _vol_bucket(closes: List[float]) -> Optional[str]:
    """20 日收益率标准差 → 年化 → 分档（阈值 env）。"""
    rets = [(closes[i] / closes[i - 1] - 1.0)
            for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    ann = math.sqrt(var * _TRADING_DAYS_PER_YEAR)
    lo, hi = _env_f("QD_CF_VOL_LOW", str(_VOL_LOW)), _env_f("QD_CF_VOL_HIGH", str(_VOL_HIGH))
    if ann >= hi:
        return "high"
    if ann <= lo:
        return "low"
    return "mid"


def _regime(closes: List[float]) -> Optional[str]:
    """收盘 vs MA20 与 20 日涨跌方向 → up / down / chop。"""
    if len(closes) < 3:
        return None
    win = closes[-20:] if len(closes) >= 20 else closes
    ma = sum(win) / len(win)
    last = closes[-1]
    base = closes[-min(21, len(closes))]
    if not base:
        return None
    ret20 = last / base - 1.0
    above, up = last > ma, ret20 > 0
    if above and up:
        return "up"
    if (not above) and (not up):
        return "down"
    return "chop"


def _theme_heat(hs: Any) -> Optional[str]:
    """头部板块涨幅均值 → hot / warm / cold（阈值 env）。"""
    if not isinstance(hs, dict):
        return None
    vals: List[float] = []
    for key in ("industry", "concept"):
        for row in (hs.get(key) or []):
            if isinstance(row, dict):
                v = _f(row, "change_pct")
                if v is not None:
                    vals.append(v)
    if not vals:
        return None
    mean = sum(vals) / len(vals)
    lo, hi = _env_f("QD_CF_HEAT_LOW", str(_HEAT_LOW)), _env_f("QD_CF_HEAT_HIGH", str(_HEAT_HIGH))
    if mean >= hi:
        return "hot"
    if mean <= lo:
        return "cold"
    return "warm"


# ═══════════════════════════════════════════════════════════════
#  落盘（同日覆盖，不追加重复帧）
# ═══════════════════════════════════════════════════════════════
def append_frame(frame: Dict[str, Any]) -> Path:
    """按月 JSONL 落盘；**同 as_of 覆盖**（盘后任务会被重跑/补跑，重复帧会污染计数）。"""
    try:
        as_of = date.fromisoformat(str(frame.get("as_of") or "")[:10])
    except (ValueError, TypeError):
        raise ValueError(f"frame 缺合法 as_of: {frame.get('as_of')!r}")
    path = frame_path(as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in _read(path) if str(r.get("as_of")) != as_of.isoformat()]
    rows.append(frame)
    rows.sort(key=lambda r: str(r.get("as_of") or ""))
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return path


def load_frames(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取全部帧（默认整个 FRAMES_DIR 下的 jsonl，按文件名排序）。"""
    d = Path(path or FRAMES_DIR)
    if not d.exists():
        return []
    files = sorted(d.glob("context_frames_*.jsonl")) if d.is_dir() else [d]
    out: List[Dict[str, Any]] = []
    for fp in files:
        out.extend(_read(fp))
    return out


def _read(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                logger.warning("[context_frame] 跳过损坏行: %s", path)
    return out
