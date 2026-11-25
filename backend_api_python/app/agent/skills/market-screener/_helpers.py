# -*- coding: utf-8 -*-
"""
market-screener/_helpers.py — 事实与证据层（不做选股决策）

职责三条：
    1. 取数与计算：日线、量价结构、资金流、指标快照、T+1 标定预测
    2. deep 层证据加工：tech_summary / seal_summary / flow_summary /
       same_time_stats / intraday_shape / fetch_rt_map + apply_rt_overlay
    3. 选人/_deep 共用件：market_calib_stats / location_tier / cross_verdict /
       _mood_regime / prewarm_market_klines

决策（选谁、留几只、要不要折价、如何叙述）由 LLM 依据 SKILL.md 的分析框架完成；
选人链路唯一为 run._run_topdown 三层漏斗（_topdown.py）。

设计红线：
    · 本模块禁止根据「策略偏好」剔除候选；只标注客观事实与不可执行项
    · 所有回退必须留痕（missing / errors），不静默吞异常 —— 否则 LLM 会把
      数据缺口当成无信号
"""

import json
import os
import threading
from datetime import date, datetime, time
from concurrent.futures import ThreadPoolExecutor

from ...log import logger
from typing import Any, Dict, List, Optional, Tuple

# 横截面只读查询层（唯一取数实现）：snapshot 同刻面板 / 指数同刻涨跌等
# 全部下沉到 data_sources.screen_query，本模块不再直接写 SQL / 直连连接池。
from app.data_sources import screen_query as _sq

# 单批取数等待上限：DB/网络抖动时不拖垮整轮选股（超时项计入 missing）
_FETCH_TIMEOUT_S = 20.0

# 错误分类字典（用于统一错误信息格式）
ERROR_CATEGORIES = {
    "DATA_SOURCE": "数据源异常",
    "TIMEOUT": "超时未返回",
    "INSUFFICIENT_DATA": "数据不足",
    "NETWORK": "网络问题",
    "PARSING": "解析失败",
    "API": "API 调用失败",
    "THREAD": "线程异常",
    "SYNTAX": "语法错误",
    "CUSTOM": "自定义错误",
    # 业务级错误分类（用于帮助 LLM 定位决策问题，非系统异常）
    "PATTERN_SIGNAL": "形态信号（技术形态定性）",
    "FLOW_RISK": "资金风险（拉高出货/异常流向）",
    "POSITION_WARN": "位置提醒（距高点/支撑阻力）",
}


def select_strategy() -> str:
    """根据当前时间选择交易日策略。"""
    now = datetime.now()
    # 非交易日 → 盘后
    if now.weekday() >= 5:
        return "post_market"
    t = now.time()
    if time(9, 30) <= t < time(14, 30):
        return "intraday"
    if time(14, 30) <= t < time(15, 0):
        return "eod"
    return "post_market"


def resolve_names(code_list: List[str]) -> Dict[str, str]:
    """批量解析股票名称。返回 {code: name}。"""
    if not code_list:
        return {}
    try:
        from ...tools.finance.data_tools import get_realtime_quote
        q = get_realtime_quote(",".join(code_list))
        name_map = {}
        if isinstance(q, dict):
            data = q.get("data", q)
            if isinstance(data, dict):
                for code, info in data.items():
                    if isinstance(info, dict) and info.get("name"):
                        name_map[code] = info["name"]
        return name_map
    except Exception:
        return {}


def analyze_batch(items: list, fn, max_candidates: int = 8, errors: list = None) -> list:
    """批量分析，逐项调用分析函数。

    Args:
        items: 待分析项列表，每项会作为 fn 的参数
        fn: 分析函数，接收一项 item，返回分析结果 dict 或 None
        max_candidates: 最多分析数量，默认 8
        errors: 可选，收集 {"code":..., "error":...}；不留痕会让 LLM 把失败误读为「无信号」

    Returns:
        非 None 的分析结果列表
    """
    results = []
    for i, item in enumerate(items):
        if i >= max_candidates:
            break
        code = item.get("code", "") if isinstance(item, dict) else ""
        try:
            r = fn(item)
            if r is not None:
                results.append(r)
            elif isinstance(errors, list):
                errors.append({"code": code, "error": "分析返回 None（多为数据不足，非无信号）"})
        except Exception as e:
            if isinstance(errors, list):
                errors.append({"code": code, "error": f"{type(e).__name__}: {e}"})
            logger.debug("[MktScreen] analyze_batch %s 失败: %s", code, e)
    return results


# ═══════════════════════════════════════════════════════════════
#  客观事实：硬标记 / 技术画像 / 资金流
# ═══════════════════════════════════════════════════════════════

def _f(v: Any, default: float = 0.0) -> float:
    """容错转 float。"""
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _format_error(code: Optional[str], error_type: str, message: str) -> str:
    """统一格式化错误信息，返回带代码前缀的中文错误描述。
    
    Args:
        code: 股票代码
        error_type: 错误类型（对应 ERROR_CATEGORIES 键）
        message: 异常原始消息
    
    Returns:
        格式化后的错误字符串，如 "数据源异常: 600519 - ConnectionError: ..."
    """
    category = ERROR_CATEGORIES.get(error_type, error_type)
    code_prefix = f"{code} - " if code else ""
    return f"{category}: {code_prefix}{message}"


def _num_or_none(v: Any) -> Optional[float]:
    """容错转 float；None/空串/非数 → None（缺失语义），不做 0.0 归一。

    与 _f 的分工：_f 适合「有缺省值」的数值字段；本函数用于 **0 与缺失语义
    不同的字段**（涨跌停价 / 涨跌幅）——契约上 0/-1 是「无涨跌幅限制」哨兵、
    None 是「源未给」（腾讯增补偶发失败，见 data_tools.get_realtime_quote），
    _f(None)=0.0 会把「未知」伪造成「0 价」→ _parse_limit_px 判 no_limit →
    封板票 buyable=True 进可买表（C1，2026-10-21；missing≠no_limit，
    见 references/limit-rules.md）。
    """
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _last(seq: List[Optional[float]]) -> Optional[float]:
    return seq[-1] if seq else None


def _open_gap_floor(bars: List[Dict], lookback: int = 30) -> Optional[float]:
    """最近一个**向上跳空且未被回补**的缺口下沿（= 跳空前一日最高价）。

    向上跳空 low[i] > high[i-1] 形成的缺口，回踩到下沿常获支撑。只认现价之上
    还有缺口的（`bars[i:]` 全部最低价仍 > 下沿）：已回补的缺口不再有支撑意义。
    取最近的一个 —— 越近的缺口对当下越有意义。
    """
    n = len(bars)
    for i in range(n - 1, max(1, n - lookback), -1):
        floor = bars[i - 1]["high"]
        if not floor or bars[i]["low"] <= floor:
            continue
        if min(b["low"] for b in bars[i:]) > floor:
            return float(floor)
    return None


def _volume_poc(bars: List[Dict], window: int = 20, bins: int = 10) -> Optional[float]:
    """近 N 日**成交密集区**中心价（筹码成本带，成交量价格分布的 POC 简化版）。

    把 window 根 bar 的典型价 (H+L+C)/3 按价格等分 bins 档、累计成交量，取最重
    那一档的中心价 —— 该处换手最充分，回踩常获支撑。峰值量占窗口总量不足 8%
    视为无显著密集区（分布太散，给出来反而误导），返回 None。
    """
    w = min(window, len(bars))
    if w < 5:
        return None
    seg = bars[-w:]
    lo = min(b["low"] for b in seg)
    hi = max(b["high"] for b in seg)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    buckets = [0.0] * bins
    for b in seg:
        p = (b["high"] + b["low"] + b["close"]) / 3
        buckets[min(bins - 1, int((p - lo) / step))] += float(b["volume"] or 0)
    total = sum(buckets)
    if total <= 0:
        return None
    peak = max(range(bins), key=lambda i: buckets[i])
    if buckets[peak] / total < 0.08:
        return None
    return lo + step * (peak + 0.5)


def _last_zt_low(bars: List[Dict], limit_pct: float, lookback: int = 20) -> Optional[float]:
    """最近一次涨停那根 bar 的最低价（首板起点；跌回此处通常是最后一道防线）。

    涨停判据与 tech_summary 的连板循环同口径（`limit_pct - 0.8` 容差），
    limit_pct<=0（新股无涨跌幅）时不给。
    """
    if limit_pct <= 0:
        return None
    n = len(bars)
    for i in range(n - 1, max(1, n - lookback), -1):
        pc = bars[i - 1]["close"]
        if pc and (bars[i]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
            return float(bars[i]["low"])
    return None


def _key_levels(bars: List[Dict], close: float, ma_map: Dict[str, Optional[float]],
                limit_pct: float) -> Dict[str, List[Dict[str, Any]]]:
    """客观关键价位候选（均线 / 近端低高点 / 跳空缺口 / 成交密集区 / 涨停起点）。

    只出「价格 + 类型 + 距离」，**不判断能不能撑住** —— 挑哪个位置当买点、
    止损放哪一档，由调用方结合大盘/板块/资金环境决定（2026-10-31 用户裁定：
    不该固定 MA5/MA10/MA20）。
    支撑与压力按现价分侧：同一价位在现价上方时是压力不是支撑。旧实现只取
    |dist|≤3% 且不分上下，会把上方均线也报成「支撑」。
    同价去重（差 <0.8% 视为同一位置）保留先出现的，优先序即 cands 的构造顺序。
    """
    lows = [b["low"] for b in bars]
    cands: List[tuple] = []          # (kind, px)
    for k in ("ma5", "ma10", "ma20", "ma60"):
        v = ma_map.get(k)
        if v:
            cands.append((k, float(v)))
    for w in (10, 20, 60):
        if len(bars) >= w:
            cands.append(("lo%d" % w, float(min(lows[-w:]))))
    if len(bars) >= 20:
        cands.append(("hi20", float(max(b["high"] for b in bars[-20:]))))
    gap = _open_gap_floor(bars)
    if gap:
        cands.append(("gap", gap))
    poc = _volume_poc(bars)
    if poc:
        cands.append(("poc", poc))
    zt_low = _last_zt_low(bars, limit_pct)
    if zt_low:
        cands.append(("zt", zt_low))

    sup: List[Dict[str, Any]] = []
    res: List[Dict[str, Any]] = []
    for kind, px in cands:
        if not px or px <= 0 or not close:
            continue
        if any(abs(o["px"] - px) / close * 100 < 0.8 for o in sup + res):
            continue                  # 同价只留优先序靠前的
        item = {"k": kind, "px": round(px, 2), "d": round((px - close) / close * 100, 2)}
        (res if px > close else sup).append(item)
    return {"supports": sorted(sup, key=lambda x: -x["d"])[:3],
            "resists": sorted(res, key=lambda x: x["d"])[:2]}


def regime_tag(run5: Optional[float], d0: float, d1: Optional[float],
               vr: Optional[float], cpos: Optional[float],
               rs_d0: Optional[float] = None) -> str:
    """位置标签（纯函数，无 IO）：高位/高位分歧/跳水回踩/弱转强/低位启动/低位蓄势/低位走弱。

    只描述「位置 + 跨日方向」。一鼓作气/再而衰/三而竭是**分时概念**（逐波拉升与力度衰减
    需要 1m 序列），日线收盘价无法表达，已整体移除；高位当日上涨或跑输大盘大跌一律只报
    「高位」（=仅位置，能不能买归盘中形态判定）。跳水回踩保留：高位大跌且 rs_d0≥-1.5
    （跌主要来自大盘带崩）= 跨日事实。标签是**描述性事实**（非准入闸）。
    vr/cpos 保留形参以兼容既有调用方，不参与本层判定。
    """
    hi = run5 is not None and run5 > 10
    if hi:
        if d0 < -2 and rs_d0 is not None and rs_d0 >= -1.5:
            return "跳水回踩"
        if -2 <= d0 <= 2:
            return "高位分歧"
        return "高位"
    if d0 > 2:
        return "弱转强" if (d1 is not None and d1 < 0) else "低位启动"
    if d0 < -2:
        return "低位走弱"
    return "低位蓄势"


_BENCH_CACHE = {"t": None, "val": None}


def _bench_index() -> dict:
    """上证基准涨跌幅：d0=当日涨跌%、d5=近5日涨跌%；进程内 60s TTL；失败返回 {}。

    rs（个股相对大盘超额）的基准。IO 走 market_cn 门面（同 flow_summary/zt_index
    先例）。失败语义 = 缺键（rs 不进输出），不抛、不重试轰炸（失败也缓存 60s）。
    """
    now = datetime.now().timestamp()
    v = _BENCH_CACHE.get("val")
    if v is not None and _BENCH_CACHE.get("t") and now - _BENCH_CACHE["t"] < 60:
        return v
    out: dict = {}
    try:
        from app.market_cn.index import get_index_realtime, get_index_daily_kline
        rows = get_index_realtime(["000001"]) or []
        if rows and rows[0].get("change_percent") is not None:
            out["d0"] = float(rows[0]["change_percent"])
        kl = get_index_daily_kline("000001", days=8) or []
        closes = [float(b.get("close")) for b in kl if b.get("close")]
        if len(closes) >= 6 and closes[-6]:
            out["d5"] = round((closes[-1] / closes[-6] - 1) * 100, 2)
    except Exception as e:
        logger.warning("[MktScreen] 大盘基准获取失败: %s", e)
        out = {}
    _BENCH_CACHE["t"], _BENCH_CACHE["val"] = now, out
    return out


_MKT_CALIB_TTL_S = float(os.getenv("QD_SCREENER_MKT_CALIB_TTL", "60") or 0)
_MKT_CALIB_CACHE: Dict[str, Any] = {"key": None, "t": 0.0, "val": {}}
# 单飞锁：deep 一次 16 并发要同一份同刻基准，首算约 6s（6 个交易日 × 全市场
# snapshot 聚合）。无锁时每个线程都看到 cache miss 各自再算一遍，PG 侧十几份同表
# 扫描互相挤，总耗时反而翻倍。锁内二次确认（双检），命中即走。
_MKT_CALIB_LOCK = threading.Lock()

_ST_LOOKBACK = 5      # 同刻基准回看的交易日数


def _now_hhmm() -> str:
    """此刻的分时截断时刻：交易时段内取当前时刻，否则取收盘 15:00。

    非交易时段一律用 15:00 才安全：盘前当日没有 tick ⇒ 自动落到上一交易日**全量**
    （用当前时刻会只取到上一交易日的同一时刻，把「昨天收盘」算成「昨天 9:20」）；
    午休取 15:00 也无害，当日数据本来就只到 11:30；非交易日取 15:00 才能拿到上一
    交易日的整天。调用方显式传 same_t 时以调用方为准（回测/人工指定时刻）。
    """
    try:
        from app.utils.trading_calendar import is_trading_day_today
        if not is_trading_day_today():
            return "15:00"
    except Exception as e:      # 日历不可用：保守取全量，勿退回「半日」
        logger.warning("[MktScreen] 交易日判断失败，同刻时刻回落 15:00: %s", e)
        return "15:00"
    hm = datetime.now().strftime("%H:%M")
    return hm if "09:31" <= hm <= "15:00" else "15:00"


def _snap_st_query(same_t: Optional[str], symbols: Optional[List[str]] = None,
                   day: Optional[str] = None,
                   lookback_days: int = 6) -> list:
    """snapshot 同刻聚合的取数入口：一次查询返回「按交易日（多票时按 symbol+日）」的原始行。

    **取数实现已下沉到 `data_sources.screen_query.snap_panel`**（分层铁律：skill 层不写
    SQL、不直连连接池）。本函数只做「面板 → 扁平元组」的形状适配，保留既有返回形状是
    为了让 `_snap_rec` / `_st_calc` 的位置取值继续成立；窗口与量价净额公式在新层唯一定义。

    symbols=None → 全市场（逐票聚合后按日求和，行 = d,v,net,amt）；
    非空 → 该批票（行 = symbol,d,v,net,amt,last,prev_close）。
    多票走 `= ANY(...)` 而不是逐票 N 次查询：deep 45 只票 16 并发时逐票查会打满连接池，
    实测丢掉 9/45 只票；批量一次约 60ms。

    净额必须覆盖整个窗口（full_net=True）：调用方都要「今日 vs 前 N 日同刻」对比，
    只算今天会让历史日的 net/amt 恒为 None。

    参数:
        same_t: 同刻时间（HH:MM）
        symbols: 股票代码列表；None 表示全市场
        day: 目标交易日（YYYY-MM-DD）
        lookback_days: 回看窗口大小（默认6）。
            较大值保证高波动期数据充足；较小值提升速度。
            可通过环境变量 QD_SCREENER_LOOKBACK_DAYS 覆盖默认值。

    **备注**：窗口大小由调用方决定，不在本函数内部查询市场状态（避免慢路径阻塞）。
    """
    # 环境变量覆盖默认值（优先级：显式参数 > 环境变量 > 默认 6）
    _env_lb = int(os.getenv("QD_SCREENER_LOOKBACK_DAYS", "6"))
    _lb = lookback_days if lookback_days > 0 else _env_lb

    try:
        panel = _sq.snap_panel(as_of=same_t, codes=symbols, days=_lb,
                               with_net=True, full_net=True, end_day=day,
                               with_last=bool(symbols))
    except Exception as e:
        logger.warning("[MktScreen] snapshot 同刻聚合失败: %s", e)
        return []
    rows_map = panel.get("rows") or {}
    if not rows_map:
        return []

    if not symbols:
        agg: Dict[str, List[float]] = {}
        for per_day in rows_map.values():
            for d, v in per_day.items():
                a = agg.setdefault(d, [0.0, 0.0, 0.0])
                a[0] += v.get("vol") or 0.0
                a[1] += v.get("net") or 0.0
                a[2] += v.get("amt") or 0.0
        return [(d, a[0], a[1], a[2]) for d, a in sorted(agg.items())]

    out: list = []
    for code in sorted(rows_map):
        for d, v in sorted(rows_map[code].items()):
            out.append((code, d, v.get("vol") or 0.0, v.get("net") or 0.0,
                        v.get("amt") or 0.0, v.get("last"), v.get("prev_close")))
    return out


def _snap_rec(row, multi: bool) -> Dict[str, Any]:
    """一行 SQL 结果 → 统一记录（多票行多出 symbol/last/prev_close 三列）。"""
    off = 1 if multi else 0
    vol = float(row[off + 1] or 0)
    net = float(row[off + 2] or 0)
    amt = float(row[off + 3] or 0)
    rec: Dict[str, Any] = {"day": str(row[off])[:10], "volume": vol,
                           "fp_pct": (round(net / amt * 100, 2) if amt else None)}
    if multi:
        rec["code"] = str(row[0]).split(".")[0]
        rec["last"] = row[off + 4]
        rec["prev_close"] = row[off + 5]
    return rec


def _snap_same_t(same_t: Optional[str], code: Optional[str] = None,
                 day: Optional[str] = None) -> List[Dict[str, Any]]:
    """按交易日聚合「截至 HH:MM 的全市场/单票」分时事实，供同刻校准用。

    code=None → 全市场（5569 只逐票聚合后再按日求和）；code 非空 → 单票。
    same_t=None 时用 _now_hhmm()（盘前=当日无 tick ⇒ 自动落到上一交易日全天）。

    为什么走 realtime_snapshot 而不是 kline_1m（口径根源，勿改回）：
      · `kline_1m` 是**盘后回填**，盘中用 SQL 直查当日为空，旧实现只能拿 kline_1D
        的日期做 target ⇒ 盘中所谓同刻基准实际算的是「昨天同刻」。
      · `kline_1D_2026` 同样是盘后更新，target 取不到今天，叠加上一个问题。
      · snapshot 每 60s 一拍、盘中实时写入，今日与历史同表同公式，天然同源。
        volume 与 1m 同单位（股）：单票 600519 全天 5,992,529 vs 5,992,729（0.003%）。
      · 全市场同刻量与 1m 差约 7%（1m 侧含竞价/盘后 bar），所以**两端必须同源**：
        今日取 snapshot、历史取 1m 会让「今日÷前5日」凭空背上 7% 偏差。

    聚合规则（与 fund_flow_api._flow_from_1m_day 同构，量价方向净额，非主力口径）：
      · 累计量 = 该日 max(volume)（累计量单调，末拍即最大，比按时间倒序取更稳）
      · 方向净额 = Σ 相邻拍 last 变化 × 该拍增量额；首拍无前值记 0
      · fp_pct = 净额 ÷ 成交额 × 100
    返回 [{day, volume, fp_pct, last?, prev_close?}]（last/prev_close 仅单票）；
    失败返回 []，不抛（下游缺键 = 未校准）。
    """
    if code:
        rows = _snap_st_query(same_t, symbols=[str(code).split(".")[0]], day=day)
        return [_snap_rec(r, True) for r in rows]
    return [_snap_rec(r, False) for r in _snap_st_query(same_t, day=day)]


_IDX_ST_CACHE: Dict[str, Any] = {"key": None, "t": 0.0, "val": None}


def _idx_ret_same_t(day: str, t: str) -> Optional[float]:
    """上证指数在 day 日「截至 t」的涨跌%（rs_d0 / rs_same_t 的同刻版基准）。

    本地部分走 `data_sources.screen_query.index_same_t`（kline_index_5m，9 指数 ×
    130+ 交易日，5 分钟对齐，毫秒级，零外网）；目标日 5m 未同步（盘中当日 5m 要盘后
    才刷）时回落 get_index_realtime 的 price/last_close —— 那只对「此刻」成立，故仅当
    day=今天才回落；历史日缺数据返回 None（缺键=未校准，不伪造 0）。
    60s 进程内 TTL：deep 45 只票共用一次。
    """
    key = (str(day)[:10], str(t)[:5])
    now = datetime.now().timestamp()
    if (_IDX_ST_CACHE.get("key") == key and _IDX_ST_CACHE.get("t")
            and now - _IDX_ST_CACHE["t"] < 60):
        return _IDX_ST_CACHE["val"]
    val = _sq.index_same_t(day=key[0], t=key[1])
    if val is None and key[0] == datetime.now().strftime("%Y-%m-%d"):
        try:
            from app.market_cn.index import get_index_realtime
            rows = get_index_realtime(["000001"]) or []
            px, lc = (rows[0].get("price"), rows[0].get("last_close")) if rows else (None, None)
            if px and lc:
                val = round((float(px) / float(lc) - 1) * 100, 2)
        except Exception as e:
            logger.warning("[MktScreen] 指数实时涨跌失败: %s", e)
    _IDX_ST_CACHE.update({"key": key, "t": now, "val": val})
    return val


def _calib_same_t(same_t: str, day: Optional[str]) -> Dict[str, Any]:
    """分时口径的大盘基准：目标日 T 前累计 ÷ 前 5 个交易日同刻均值（用户 9:50 口径）。"""
    if str(same_t)[:5] >= "15:00":
        # 收盘后同刻 ≡ 全天：走日线口径——权威聚合且便宜（全量 snapshot 聚合 22s vs 7s）
        return _calib_day(day)
    rows = [r for r in _snap_same_t(same_t, day=day) if r.get("volume")]
    if len(rows) < 2:
        return {}
    target = rows[-1]
    prev = rows[-(_ST_LOOKBACK + 1):-1]
    base = [r["volume"] for r in prev if r["volume"] > 0]
    fps = [r["fp_pct"] for r in prev if r.get("fp_pct") is not None]
    vr = round(target["volume"] / (sum(base) / len(base)), 2) if base else None
    return {
        "ok": True,
        "caliber": "intraday",
        "t": str(same_t)[:5],
        "day": target["day"],
        "days_used": len(prev),
        "vr": vr,
        "fp_pct": target.get("fp_pct"),
        "fp_prev_pct": round(sum(fps) / len(fps), 2) if fps else None,
        "idx_ret": _idx_ret_same_t(target["day"], str(same_t)[:5]),
    }


def _calib_day(day: Optional[str]) -> Dict[str, Any]:
    """日线口径的大盘基准：当日全天 ÷ 前 5 日均量；净占比读派生表，缺日现算 1m。"""
    from app.market_cn import fund_flow_api as _ffa
    # 全市场日量走 screen_query（唯一取数实现）；窗口与旧实现一致（近 45 自然日、不按 day 截）
    vols = _sq.market_day_volume(back_days=45)
    dlist = [d for d in sorted(vols) if vols[d] > 0]
    if day:
        dlist = [d for d in dlist if d <= str(day)[:10]]
    if len(dlist) < 2:
        return {}
    target, prev5 = dlist[-1], dlist[-6:-1]
    base = [vols[d] for d in prev5 if vols.get(d)]
    vr = round(vols[target] / (sum(base) / len(base)), 2) if vols.get(target) and base else None
    fp_map: Dict[str, Optional[float]] = dict(_sq.market_day_flow_pct(since=dlist[0]))
    for d in [target] + prev5:
        if fp_map.get(d) is None:
            r = _ffa._flow_from_1m_day(d)
            if r and r.get("main_pct") is not None:
                fp_map[d] = float(r["main_pct"])
    fp_base = [fp_map[d] for d in prev5 if fp_map.get(d) is not None]
    return {
        "ok": True,
        "caliber": "day",
        "t": None,
        "day": target,
        "days_used": len(prev5),
        "vr": vr,
        "fp_pct": fp_map.get(target),
        "fp_prev_pct": (round(sum(fp_base) / len(fp_base), 2) if fp_base else None),
        "idx_ret": _idx_ret_same_t(target, "15:00"),
    }


def market_calib_stats(same_t: Optional[str] = None, day: Optional[str] = None) -> Dict[str, Any]:
    """大盘两轴校准基准：vr=全市场量比、fp_pct=全市场净占比%（vr_rel/flow_rel 的分母）。

    两种口径由 same_t 决定，**不可混用、不可跨口径比大小**（返回里 `caliber` 键即口径）：
      · same_t="HH:MM" → 分时同刻（盘中主用）：目标日 T 前全市场累计量 ÷ 前 5 个
        交易日同一时刻均值；净占比同规则。数据全走 realtime_snapshot（见 _snap_same_t：
        kline_1m/kline_1D 盘中都不含当日，用它们会把「今天同刻」算成「昨天同刻」）。
        另给 idx_ret=上证同期涨跌（rs_same_t 的基准）。
      · same_t=None → 日线（回测/盘后对账）：当日全天 ÷ 前 5 日均量；净占比优先读
        qd_fund_flow_market，缺日现算 1m（该表写入任务有断档，缺行须现算）。
    day=个股日线末日，把基准日截到该日（stale_bar 对齐）。失败/缺数据返回 {}，
    下游相对字段缺键 = 未校准。
    进程内 TTL QD_SCREENER_MKT_CALIB_TTL 秒（0=关，每次直查）+ 单飞锁：分时口径
    首次约 6s，deep 16 并发必须只算一次（同 _bench_index 先例的加强版）。

    可改进点（未做）：目前只有全市场（大盘）一根校准基准，缺板块/概念维度——
    个股对所属板块/概念的 vr_rel/flow_rel（“板块弱个股强”才是板块内的真强）。
    逐票取板块归属再聚合板块量能/净额成本过高，故不做；待板块归属表或板块
    行情数据就绪后可在此函数旁加板块版，下游字段加 board_rel 同型扩展即可。
    """
    same_t = (str(same_t)[:5] if same_t else None)
    key = (same_t or "", str(day or "")[:10], "st" if same_t else "day")

    def _hit() -> Optional[Dict[str, Any]]:
        if (_MKT_CALIB_TTL_S > 0 and _MKT_CALIB_CACHE["val"]
                and _MKT_CALIB_CACHE["key"] == key
                and datetime.now().timestamp() - _MKT_CALIB_CACHE["t"] < _MKT_CALIB_TTL_S):
            return _MKT_CALIB_CACHE["val"]
        return None

    cached = _hit()
    if cached is not None:
        return cached
    with _MKT_CALIB_LOCK:
        cached = _hit()          # 等锁期间别的线程可能已算完（双检）
        if cached is not None:
            return cached
        try:
            out = _calib_same_t(same_t, day) if same_t else _calib_day(day)
        except Exception as e:
            logger.warning("[MktScreen] 大盘校准基准失败: %s", e)
            out = {}
        _MKT_CALIB_CACHE.update({"key": key, "t": datetime.now().timestamp(), "val": out})
        return out


def tech_summary(bars: List[Dict], code: str = "", name: str = "") -> Dict[str, Any]:
    """从日线算出「位置 / 量能 / 强度 / 涨停史 / 形态标签 / 相对大盘超额」的客观事实（不含评分）。

    之所以不含评分：评分语义已统一为 P(T+1 涨)×100（见 attach_prediction），
    这里的字段只是让 LLM 能解释「为什么」，或自行推翻自带的模型分。
    regime（2026-10-31）：形态标签，见 regime_tag；rs_d0/rs_5d = 个股涨跌 − 上证
    同期涨跌（大盘弱个股强=真强，用户口径），基准经 _bench_index（60s TTL，失败缺键）。
    vr_rel = 个股量比÷大盘量比（market_calib_stats 全市场口径，缺基准缺键）。
    """
    if not bars or len(bars) < 2:
        return {"ok": False, "reason": "日线不足"}

    from .common import compute_ma, compute_rsi, get_limit_pct, is_limit_locked

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]

    today, prev = bars[-1], bars[-2]
    close = today["close"]
    chg = (close - prev["close"]) / prev["close"] * 100 if prev["close"] else 0.0

    prev_vols = vols[-6:-1]
    vr = (today["volume"] / (sum(prev_vols) / len(prev_vols))) if prev_vols and sum(prev_vols) > 0 else None

    ma5, ma10, ma20 = compute_ma(closes, 5), compute_ma(closes, 10), compute_ma(closes, 20)
    ma20_v = _last(ma20)
    dist_ma20 = ((close - ma20_v) / ma20_v * 100) if ma20_v else None

    win20 = min(20, len(bars))
    hi20, lo20 = max(highs[-win20:]), min(lows[-win20:])
    pos20 = ((close - lo20) / (hi20 - lo20) * 100) if hi20 > lo20 else None

    ret5 = ((close / closes[-6]) - 1) * 100 if len(closes) > 6 else None
    # ret20 与 ret5 同口径（N 个交易日间隔）：closes[-win20-1] 才是 20 根前的收盘，
    # closes[-win20] 只有 19 个间隔（2026-11-25 审查修正，与 ret_5d_pct 对齐）
    ret20 = ((close / closes[-win20 - 1]) - 1) * 100 if len(closes) > win20 else None

    # 连续涨停（用涨跌停幅度近似，尾部倒序）；limit_pct<=0（新股无涨跌幅）时恒 0
    limit_pct = get_limit_pct(code, name)
    streak = 0
    if limit_pct > 0:
        for i in range(len(bars) - 1, 0, -1):
            pc = bars[i - 1]["close"]
            if pc and (bars[i]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
                streak += 1
            else:
                break

    zt_10d = 0
    if limit_pct > 0:
        win = min(10, len(bars) - 1)
        for i in range(len(bars) - win, len(bars)):
            pc = bars[i - 1]["close"]
            if pc and (bars[i]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
                zt_10d += 1

    def _dist(ma_v):
        return round((close - ma_v) / ma_v * 100, 2) if ma_v and close else None

    dist_ma5 = _dist(_last(ma5))
    dist_ma10 = _dist(_last(ma10))
    # 关键价位候选（供 LLM 直接用，无需 read_skill / 拉 K 线）：不再只认三条均线
    ma60_v = _last(compute_ma(closes, 60))
    levels = _key_levels(bars, close,
                         {"ma5": _last(ma5), "ma10": _last(ma10),
                          "ma20": ma20_v, "ma60": ma60_v}, limit_pct)
    nearest = levels["supports"][0] if levels["supports"] else None

    intraday_pos = None
    rng = today["high"] - today["low"]
    if rng > 0:
        intraday_pos = (close - today["low"]) / rng * 100

    # 近5日平均振幅%（止损宽度基准：贴身止损=噪声成本，见 strategy-rules §六；
    # 2026-11-24 回测调研发现规则缺数据支撑，补此字段）
    amps = [(bars[j]["high"] - bars[j]["low"]) / bars[j - 1]["close"] * 100
            for j in range(max(1, len(bars) - 5), len(bars))
            if bars[j - 1].get("close")]
    amp_avg5 = round(sum(amps) / len(amps), 2) if amps else None

    d1_chg = None
    if len(bars) >= 3 and bars[-3]["close"]:
        d1_chg = (prev["close"] - bars[-3]["close"]) / bars[-3]["close"] * 100
    bench = _bench_index()
    rs_d0 = round(chg - bench["d0"], 2) if bench.get("d0") is not None else None
    rs_5d = (round(ret5 - bench["d5"], 2)
             if (ret5 is not None and bench.get("d5") is not None) else None)
    mkt = market_calib_stats(day=(str(today.get("time") or today.get("date") or "")[:10] or None))
    vr_rel = round(vr / mkt["vr"], 2) if (vr and mkt.get("vr")) else None

    location = location_tier(
        round(dist_ma20, 2) if dist_ma20 is not None else None,
        round(pos20, 1) if pos20 is not None else None,
        round(ret5, 2) if ret5 is not None else None,
        streak,
    )

    return {
        "ok": True,
        "bars": len(bars),
        "close": round(close, 2),
        "change_pct": round(chg, 2),
        "chg_prev_pct": round(d1_chg, 2) if d1_chg is not None else None,
        "amplitude_pct": round(rng / prev["close"] * 100, 2) if prev["close"] and rng else None,
        "amp_avg5": amp_avg5,
        "close_pos_in_day": round(intraday_pos, 1) if intraday_pos is not None else None,
        "vol_ratio": round(vr, 2) if vr else None,
        "vr_rel": vr_rel,
        "rsi14": round(compute_rsi(closes)[-1], 1),
        "ma5": round(_last(ma5), 2) if _last(ma5) else None,
        "ma10": round(_last(ma10), 2) if _last(ma10) else None,
        "ma20": round(ma20_v, 2) if ma20_v else None,
        "dist_ma5_pct": dist_ma5,
        "dist_ma10_pct": dist_ma10,
        "dist_ma20_pct": round(dist_ma20, 2) if dist_ma20 is not None else None,
        "support": {
            # 只出候选列表 + 是否贴支撑：近端位置 = supports[0]，均线在 tech 顶层
            # （2026-10-31 去重：原 ma5/ma10/ma20 + nearest 三件套与这两处逐字重复，
            #  满卡每张省 ~285 字符，等于把第 6 张满卡换回来）
            "near_support": bool(nearest and abs(nearest["d"]) <= 3.0),  # |dist|<=3% 视为贴支撑
            "supports": levels["supports"],   # 现价下方候选，由近到远，≤3 条
            "resists": levels["resists"],     # 现价上方候选，由近到远，≤2 条
            # 支撑-压力间距%（过窄→操作性弱，见 SKILL.md 维度4）
            "sr_gap_pct": round((levels["resists"][0]["px"] - levels["supports"][0]["px"])
                                / close * 100, 2)
            if (levels["resists"] and levels["supports"] and close) else None,
        },
        "ma_bull_stack": bool(_last(ma5) and _last(ma10) and ma20_v and _last(ma5) > _last(ma10) > ma20_v),
        "hi20": round(hi20, 2),
        "lo20": round(lo20, 2),
        "hi_today": round(today["high"], 2),
        "pos_in_20d_pct": round(pos20, 1) if pos20 is not None else None,
        "is_20d_high": bool(pos20 is not None and pos20 >= 99),
        "location": location,
        "ret_5d_pct": round(ret5, 2) if ret5 is not None else None,
        "ret5_base_close": round(closes[-6], 2) if len(closes) > 6 else None,
        "ret_20d_pct": round(ret20, 2) if ret20 is not None else None,
        "limit_up": bool(is_limit_locked(code, name, close, prev["close"])),
        "zt_streak": streak,
        "zt_10d": zt_10d,
        "prev_zt_streak": _prev_streak(bars, limit_pct, streak),
        "regime": regime_tag(ret5, chg, d1_chg, vr, intraday_pos, rs_d0),
        "rs_d0": rs_d0,
        "rs_5d": rs_5d,
    }


def _prev_streak(bars: List[Dict], limit_pct: float, streak: int) -> int:
    """截至上一交易日结束时的连板高度（判断「连板后首阴」等形态用）。

    语义 =「今日之前」的连板数（analysis-framework.md 维度 4：prev 高 + 当日
    收阴 = 首阴；common.py 渲染「连板X/前Y」）：
    · 今日仍涨停（streak>=1）→ streak-1（3 连板 → 前 2 板）；
    · 今日未涨停（首阴/断板）→ 从昨日（len-2）向前数（前 4 板首阴 → 4）。
    【易错】旧实现 idx=len-1-streak 恰落在主 streak 循环「因涨幅不足 break 的
    那根 bar」上，while 首查必败 → 恒 0（C3，2026-10-21 修）。判定阈值与
    tech_summary 主循环同口径（limit_pct-0.8 容差），勿单独改动。
    """
    if limit_pct <= 0:
        return 0
    if streak >= 1:
        return streak - 1
    idx = len(bars) - 2
    n = 0
    while idx > 0:
        pc = bars[idx - 1]["close"]
        if pc and (bars[idx]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
            n += 1
            idx -= 1
        else:
            break
    return n


# ── 开盘脉冲（拉高出货）识别阈值（2026-10-30 用户缺陷②）──────────────────
# 「开盘几千万拉升后抛盘」的客观形态：开盘窗大额净流入 → 其后转净流出 → 冲高回落。
# 阈值是**经验值**（只产 warnings，不进 hard）——形态是经验判断不是客观不可交易，
# 取舍仍归 LLM（设计原则见 SKILL.md「你的角色与自由度」）。
_OPEN_WINDOW_START = "09:30"   # 开盘窗起点（集合竞价后的连续竞价）
_OPEN_WINDOW_END = "10:00"     # 开盘窗截止时刻（早盘定调窗口，见 strategy-rules）
_PUMP_OPEN_MIN_WAN = 2000.0    # 开盘窗净流入下限（万元）≈「几千万级」脉冲
_PUMP_OPEN_SHARE_PCT = 60.0    # 开盘窗净额占全天净额比例上限（全天净流入为正时）
_PUMP_FADE_PCT = 3.0           # 现价距日内高点回落幅度（%）——冲高回落佐证
_PUMP_FADE_POS = 35.0          # 缺 rt 时用 close_pos_in_day ≤ 此值近似冲高回落


def _intraday_shape(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    """分钟净额 → 日内时段结构（开盘窗 vs 其后）。纯求和，无判断。

    开盘窗 = 09:30 ≤ t < `_OPEN_WINDOW_END`（左闭右开：bar 时间是**起始**时刻，
    10:00 起的 bar 属盘中段；strategy-rules「10:00 前基本定调」）。
    points 来自 tape.get_fund_flow_realtime 的 data：[{time:"YYYY-MM-DD HH:MM", main_net:元}]。
    open_share_pct = 开盘窗净额 ÷ 全天净额；其后净流出时可 >100%（脉冲全部被回吐），
    不是计算错误。
    """
    open_net = after_net = 0.0
    n = 0
    for p in points or []:
        t = str(p.get("time") or "")
        if len(t) < 16:
            continue
        v = _f(p.get("main_net"))
        if _OPEN_WINDOW_START <= t[11:16] < _OPEN_WINDOW_END:
            open_net += v
        else:
            after_net += v
        n += 1
    total = open_net + after_net
    return {
        "ok": n > 0,
        "open_net_wan": round(open_net / 1e4, 1),
        "after_net_wan": round(after_net / 1e4, 1),
        "open_share_pct": round(open_net / total * 100, 1) if total > 0 else None,
        "points": n,
    }


def pump_dump_risk(flow: Dict[str, Any], tech: Dict[str, Any] = None,
                   rt: Dict[str, Any] = None) -> List[str]:
    """开盘脉冲拉升后抛盘（拉高出货）风险线索 —— 经验型软提示（进 warnings，可被否决）。

    判据（2026-10-30 用户缺陷②；阈值见 _PUMP_* 常量，经验值非硬规则）：
      ① 开盘窗净流入 ≥ _PUMP_OPEN_MIN_WAN 且其后净额转负 —— 资金脉冲后撤；
      ② 开盘窗净额占全天 ≥ _PUMP_OPEN_SHARE_PCT 且其后净额为负 —— 早盘对倒特征；
      ③ 现价距日内高点回落 ≥ _PUMP_FADE_PCT（rt.high/price 优先；缺 rt 用
         tech.close_pos_in_day ≤ _PUMP_FADE_POS 近似）—— 冲高回落佐证；
      ④ 今日大额净流入但近 5 日累计净流出 —— 单日脉冲与多日趋势背离。
    ①/② 命中后再叠加 ③/④ 升级为「疑似拉高出货」；只描述形态，取舍归 LLM。

    Returns:
        list[str]: 风险线索（空 = 未命中；字段缺失按「判不了」处理，不硬凑）
    """
    out: List[str] = []
    if not isinstance(flow, dict) or not flow.get("ok"):
        return out
    intr = flow.get("intraday") if isinstance(flow.get("intraday"), dict) else {}
    if not intr.get("ok"):
        return out
    open_wan = _f(intr.get("open_net_wan"))
    after_wan = _f(intr.get("after_net_wan"))
    share = intr.get("open_share_pct")

    pulse = (open_wan >= _PUMP_OPEN_MIN_WAN or
             (share is not None and _f(share) >= _PUMP_OPEN_SHARE_PCT))
    if not (pulse and after_wan < 0):
        return out
    out.append(f"开盘脉冲：{_OPEN_WINDOW_END} 前净流入 {open_wan:.0f} 万，其后净流出 {abs(after_wan):.0f} 万")

    # ③ 冲高回落佐证
    fade_note = None
    if rt:
        high, price = _f(rt.get("high")), _f(rt.get("price"))
        if high > 0 and price > 0:
            fade = (high - price) / high * 100
            if fade >= _PUMP_FADE_PCT:
                fade_note = f"冲高回落 {fade:.1f}%（较日内高点）"
    elif isinstance(tech, dict) and tech.get("close_pos_in_day") is not None \
            and _f(tech.get("close_pos_in_day")) <= _PUMP_FADE_POS:
        fade_note = f"收于日内低侧 {tech['close_pos_in_day']}%（冲高回落近似）"
    if fade_note:
        out.append(fade_note + " → 疑似拉高出货")

    # ④ 单日脉冲与多日趋势背离
    rec = flow.get("recent") if isinstance(flow.get("recent"), dict) else {}
    net5 = rec.get("net_5d_wan")
    if net5 is not None and _f(net5) < 0 and _f(flow.get("net_wan")) >= _PUMP_OPEN_MIN_WAN:
        out.append(f"今日脉冲与近5日净流出 {_f(net5):.0f} 万背离（趋势未转向）")
    return out


# ── 封板结构（客观事实 + 硬标签，**不打分、不排序**）─────────────────────
# 2026-10-30 用户裁定：盘后/尾盘的决策场景是「次日开盘按涨停价挂单」，买不到不是
#   决策问题 ⇒ 不做「可买性」维度（原 buy_ease 摩擦分已删）。封板结构改为只给
#   事实与标签，由 LLM 估算次日强弱。
# 标签边界来自次日表现实证（最近 8 个已收盘交易日 426 只涨停票，本地日线算
#   次日开盘/收盘相对当日收盘，均值，括号内为次日收盘中位）：
#     0 开板    +1.79%（+1.94%）  n=192     1-2 开板  +1.11%（+0.82%）  n=149
#     3-5 开板  +0.82%（-0.28%）  n=55      6+ 开板   -0.57%（-2.18%）  n=30
#   封单÷成交额同样单调：≤5% -0.62% / 5-15% +1.20% / 15-40% +1.84% / 40%+ +3.51%。
#   连板高度非单调：2 板最弱（中位 -1.69%，n=61），4+ 板最强（中位 +7.56%，n=16，
#   样本小不可当定论）。⇒ 开板多=差、封单厚=好，与「可买性」正好相反。
# 复现：tmp/probe_open_count_nextday_20261030.py、tmp/probe_ease_inputs_20261030.py
# 标签命名刻意**不含价值判断**（曾是 solid/reseal/weak，实测 000568 出现「0 开板但
#   封单仅 0.38%」——叫 solid 会把最可疑的组合读成最扎实）：tag 只回答开板次数，
#   seal_tier 只回答封单厚薄，两个正交事实交给 LLM 组合。
_SEAL_TAG_ENV = "QD_SCREENER_SEAL_TAG"
_SEAL_TAG_DEFAULT: Dict[str, Any] = {
    "reseal_max": 2,          # 开板 ≤2 次 → open1_2（实证 1-2 次档）
    "loose_max": 5,           # 开板 ≤5 次 → open3_5（实证 3-5 次档）；>5 → open6p
    "seal_tiers": [5, 15, 40],  # 封单÷成交额 ≤n% → thin / mid / thick / vthick
}
_ZT_INDEX_TTL_S = 60.0
_ZT_INDEX_CACHE: Dict[str, Any] = {"day": "", "at": datetime.now(), "map": None}


def _seal_tag_rules() -> Dict[str, Any]:
    """封板标签边界：缺省 dict 被 env JSON 局部覆盖（未知键忽略，坏 JSON 告警不改行为）。"""
    rules = dict(_SEAL_TAG_DEFAULT)
    raw = (os.getenv(_SEAL_TAG_ENV, "") or "").strip()
    if not raw:
        return rules
    try:
        extra = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("[MktScreen] %s 非法 JSON，按缺省: %s", _SEAL_TAG_ENV, e)
        return rules
    if isinstance(extra, dict):
        rules.update({k: v for k, v in extra.items() if k in rules})
    return rules


def _empty_pool_reason(day: str) -> str:
    """池为空的成因判据（2026-10-31）。

    三种成因的处置**完全不同**，旧文案一律写「进程未热?」是错的归因：
      · 非交易日        —— 该日根本没有池，属日期口径错（周末提问的典型症状）；
      · 交易日·盘中     —— 池未产出或仍在变动，属正常，不必告警级别；
      · 交易日·已收盘   —— akshare 失败也表现为 []，是真失败，须进 missing。
    """
    try:
        from app.utils.trading_calendar import is_trading_day, is_in_quote_session
    except Exception as e:            # 日历不可用：归因不明就照实说，不猜
        return f"日历不可用，无法判定: {type(e).__name__}"
    try:
        if not is_trading_day(day):
            return "非交易日：该日无池"
        if day == datetime.now().strftime("%Y-%m-%d") and is_in_quote_session():
            return "交易日·盘中：池未产出或仍在变动"
        return "交易日·非报价时段（开盘前/午休/收盘后）：源返回空（源故障亦表现为 []）"
    except Exception as e:
        return f"日历判定异常: {type(e).__name__}: {e}"


def zt_index(date: str = "") -> Dict[str, Dict[str, Any]]:
    """涨停池 → {code: 池行}（含 open_count/seal_amount/amount/turnover_rate）。

    ★ 2026-10-31：默认日期由「今天」改为**目标交易日** `common._data_day()`
      （今日非交易日 → 最近一个交易日）。原口径在周末/节假日恒取到空池，
      而同一时刻日线/资金流/画像用的都是上一交易日的数据 ⇒ 口径错位，
      封板结构（open_count/seal_amount）恒 unknown。实测对照（2026-10-31）：
      `get_zt_pool("2026-10-30")` = 47 只带 open_count/seal_amount，
      `get_zt_pool("2026-10-31")`（周六）= 0。

    取数：历史日走 coord_pool 按日拉取 + 落盘缓存；当日盘中走实时接口。
    60s 内存 memo：run() 内 profile 会调多次，池本身由源侧刷新。
    空池按「开板/封单未知」降级，不得把空 map 当成「该日没涨停」。
    """
    from .common import fetch_zt_pool, _data_day
    day = (date or "").strip() or _data_day()
    now = datetime.now()
    if _ZT_INDEX_CACHE.get("day") == day and _ZT_INDEX_CACHE.get("map") is not None:
        age = (now - _ZT_INDEX_CACHE["at"]).total_seconds()
        if age < _ZT_INDEX_TTL_S:
            return _ZT_INDEX_CACHE["map"]
    try:
        pool = fetch_zt_pool(day) or []
    except Exception as e:
        logger.warning("[MktScreen] 涨停池不可用（封板结构降级）: %s", e)
        return {}
    if not pool:
        logger.warning("[MktScreen] 涨停池 %s 为空（%s）: 封板结构只剩连板数",
                       day, _empty_pool_reason(day))
    out = {str(r.get("stock_code") or ""): r for r in pool if r.get("stock_code")}
    _ZT_INDEX_CACHE.update({"day": day, "at": now, "map": out})
    return out


def seal_summary(sealed: Optional[bool], zt_streak: int = 0,
                 zt_row: Dict[str, Any] = None, rt: Dict[str, Any] = None,
                 turnover_pct: Optional[float] = None) -> Dict[str, Any]:
    """封板结构的客观事实 + 硬标签（只描述「今天封得怎么样」，**不打分、不排序**）。

    输入全部是客观值，无新取数：`zt_row` 来自涨停池（开板次数/封单额/成交额/换手/
    连板数），`rt` 给一字判定（low ≈ 涨停价），`zt_streak` 是日线连板兜底。

    标签（边界见 `_SEAL_TAG_DEFAULT`，来自次日表现实证，见上方常量块注释）。
    `tag` 只切开板次数，`seal_tier` 只切封单厚薄——**两个正交事实，都不含价值判断**
    （实测存在「0 开板但封单仅 0.38%」这种组合，任何单向命名都会误导）：
      · `tag=none`      未封板（其余字段照实给，不受影响）
      · `tag=one_word`  日内最低价 = 涨停价（一字/全天封死，实证次日最强档）
      · `tag=open0`     0 次开板但非一字（盘中封住后未再打开）
      · `tag=open1_2`   1~2 次开板后回封
      · `tag=open3_5`   3~5 次开板（反复开合）
      · `tag=open6p`    ≥6 次开板（尾盘勉强封住，实证次日唯一负收益档）
      · `tag=unknown`   实时涨跌停未知（缺 rt），或封板但涨停池缺该票（开板次数未知）
      · `seal_tier`     thin(≤5%) / mid(≤15%) / thick(≤40%) / vthick(>40%)
                        ——按实证单调性，thin 最弱（次日 -0.62%）、vthick 最强（+3.51%）

    为什么给标签而不给分数：开板次数 / 封单厚度 / 连板高度三项高度共线（一字板三项
    同时极端），合成会重复计权；权重怎么拍都缺样本支撑（4+ 板仅 16 只）。给事实比给
    权重更利于 LLM 估算，也不违反「不做主观评分梯形」。

    Args:
        sealed: 是否涨停封板（rt.limit_up）；None = 缺 rt，不猜
        zt_streak: 日线连板数（涨停池缺该票时的兜底）
        zt_row: 涨停池该票一行（open_count/seal_amount/amount/turnover_rate/
            continuous_zt_days）
        rt: 实时快照（要 low 与 limit_up_price 判一字）
        turnover_pct: 换手率（传了优先，否则取池里的 turnover_rate）

    Returns:
        dict: {tag, seal_tier, open_count, seal_ratio_pct（封单÷成交额 %）, zt_days,
            turnover_pct}；取不到的一律 None，不回落 0。
    """
    rules = _seal_tag_rules()
    row = zt_row if isinstance(zt_row, dict) else {}
    _rt = rt if isinstance(rt, dict) else {}

    days = max(int(_f(zt_streak)), int(_f(row.get("continuous_zt_days"))))
    open_count = None if row.get("open_count") is None else int(_f(row.get("open_count")))
    seal_pct = None
    _seal, _amt = _f(row.get("seal_amount")), _f(row.get("amount"))
    if _seal > 0 and _amt > 0:
        seal_pct = round(_seal / _amt * 100, 2)
    turn = turnover_pct if turnover_pct is not None else row.get("turnover_rate")
    turn = round(_f(turn), 2) if turn is not None else None

    seal_tier = None
    if seal_pct is not None:
        _cuts = [float(x) for x in (rules.get("seal_tiers") or [])]
        seal_tier = ("thin" if seal_pct <= _cuts[0] else
                     "mid" if seal_pct <= _cuts[1] else
                     "thick" if seal_pct <= _cuts[2] else "vthick")

    one_word = None
    _lo, _up = _f(_rt.get("low")), _f(_rt.get("limit_up_price"))
    if _lo > 0 and _up > 0:
        one_word = _lo >= _up - 0.01

    if sealed is None:
        tag = "unknown"
    elif not sealed:
        tag = "none"
    elif one_word:
        tag = "one_word"
    elif open_count is None:
        tag = "unknown"
    elif open_count <= 0:
        tag = "open0"
    elif open_count <= int(rules["reseal_max"]):
        tag = "open1_2"
    elif open_count <= int(rules["loose_max"]):
        tag = "open3_5"
    else:
        tag = "open6p"

    # 未封板 / 缺 rt：封单厚薄没有意义（涨停池里若残留该票的旧封单会造出
    # 「未封板 + vthick」这种矛盾组合），一律置空。
    if sealed is not True:
        seal_tier = None

    return {"tag": tag, "seal_tier": seal_tier, "open_count": open_count,
            "seal_ratio_pct": seal_pct, "zt_days": days or None, "turnover_pct": turn}


def _st_calc(rows: List[Dict[str, Any]], t: str,
             mkt: Dict[str, Any]) -> Dict[str, Any]:
    """同刻序列 + 大盘基准 → 单票同刻块（纯计算，无 IO）。单票/批量两个入口共用。"""
    rows = [r for r in rows if r.get("volume")]
    if len(rows) < 2:
        return {"ok": False, "reason": "snapshot 不足两日"}
    target = rows[-1]
    prevs = rows[-(_ST_LOOKBACK + 1):-1]
    base = [r["volume"] for r in prevs if r["volume"] > 0]
    if not base:
        return {"ok": False, "reason": "前日同时刻量缺失"}
    ret = None
    if target.get("last") and target.get("prev_close"):
        ret = round((float(target["last"]) / float(target["prev_close"]) - 1) * 100, 2)
    out: Dict[str, Any] = {
        "ok": True,
        "t": t,
        "day": target["day"],
        "days_used": len(base),
        "vr_same_t": round(target["volume"] / (sum(base) / len(base)), 2),
        "ret_same_t": ret,
        "fp_same_t": target.get("fp_pct"),
    }
    if mkt and mkt.get("day") == target["day"]:
        if out.get("fp_same_t") is not None and mkt.get("fp_pct") is not None:
            out["flow_rel_same_t"] = round(out["fp_same_t"] - mkt["fp_pct"], 2)
        if mkt.get("vr"):
            out["vr_rel_same_t"] = round(out["vr_same_t"] / mkt["vr"], 2)
        out["mkt_vr"] = mkt.get("vr")
        out["mkt_fp_pct"] = mkt.get("fp_pct")
        out["mkt_fp_prev_pct"] = mkt.get("fp_prev_pct")
        if mkt.get("idx_ret") is not None:
            out["idx_ret_same_t"] = mkt["idx_ret"]
            if ret is not None:
                out["rs_same_t"] = round(ret - mkt["idx_ret"], 2)
    return out


def same_time_stats(code: str, same_t: Optional[str] = None) -> Dict[str, Any]:
    """同时刻横向比对（盘中直接参考）：今日截至 T 的量能与相对大盘超额，对比前几日同一时刻。

    用户口径：大盘弱个股强=真强、大盘强个股弱=真弱；「同一时刻」（而非全天）的量比
    才有盘中直接参考价值。个股与大盘**同走 realtime_snapshot**（口径见
    market_calib_stats / _snap_same_t），单票 6 个交易日一次约 20~30ms。
    T = same_t 或 _now_hhmm()（交易时段=此刻，否则 15:00）；盘后调用即≈全天。
    vr_same_t = 目标日 T 前累计量 ÷ 前 N 日(≤5)同一时刻累计量均值；ret_same_t =
    昨收→T 涨幅；idx_ret_same_t = 上证同期涨跌；rs_same_t = 前者−后者。
    fp_same_t = 目标日 T 前量价方向净占比%（与大盘同公式）；vr_rel_same_t /
    flow_rel_same_t = 个股同刻量比÷大盘同刻量比、个股同刻净占比−大盘同刻净占比。
    【易错】相对量只在个股与大盘**同一目标日**时给出：停牌/无当日 tick 的票 target
    会落在旧日期，跨日相除/相减没有意义，此时宁可缺键（=未校准）。
    失败语义 = ok:False + reason，不抛（inspect 超时留痕由调用方负责）。
    """
    code = str(code or "").split(".")[0].strip()
    t = str(same_t)[:5] if same_t else _now_hhmm()
    return _st_calc(_snap_same_t(t, code=code), t, market_calib_stats(same_t=t))


def same_time_batch(codes: List[str], same_t: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """same_time_stats 的批量版：一次 snapshot 查询出全部票，再逐票算相对量。

    deep 45 只票逐票查会把连接池打满（实测丢 9/45 只票返回），故盘中改批量：
    一条 `symbol = ANY(...)` 约 60ms + 一次大盘基准（TTL），总成本与票数几乎无关。
    返回 {code: 同刻块}（不含 ok），只含成功项 —— 缺的票下游按「无分时校准」处理。
    """
    uni: List[str] = []
    for c in codes or []:
        s = str(c or "").split(".")[0].strip()
        if s and s not in uni:
            uni.append(s)
    if not uni:
        return {}
    t = str(same_t)[:5] if same_t else _now_hhmm()
    mkt = market_calib_stats(same_t=t)
    bucket: Dict[str, List[Dict[str, Any]]] = {}
    for row in _snap_st_query(t, symbols=uni):
        rec = _snap_rec(row, True)
        bucket.setdefault(rec.pop("code"), []).append(rec)
    out: Dict[str, Dict[str, Any]] = {}
    for c in uni:
        st = _st_calc(bucket.get(c) or [], t, mkt)
        if st.get("ok"):
            st.pop("ok", None)
            out[c] = st
    return out


# ═══════════════════════════════════════════════════════════════
#  分时形态：弱转强 / 弱转分歧 / 强转弱 / 强转分歧（买卖两个关键节点）
# ═══════════════════════════════════════════════════════════════
# 为什么必须是分时：这四个是「盘中多空换手的那一刻」，日线只看到结果（收红/收绿），
# 看不到过程。同一个「-1% 开盘 → +2% 收盘」的日线，可能是低开一路走高（弱转强，次日
# 有承接），也可能是冲高砸下来又拉回（强转分歧，次日两侧都有人）。短线的买卖点就
# 建立在这个区别上，所以判据全部取自当日分钟序列：累计 VWAP 关系 + 极值先后 + 量能配合。
# 阈值走 env（禁硬编码），缺省值来自「四态在真实分时上语义可辨」这一条，不是收益拟合。
_SHAPE_VWAP_TOL = float(os.getenv("QD_SHAPE_VWAP_TOL", "0.2"))        # 站上/跌破均价线
_SHAPE_ABOVE_RECENT = float(os.getenv("QD_SHAPE_ABOVE_RECENT", "0.7"))  # 近端站上占比
_SHAPE_ABOVE_WEAK = float(os.getenv("QD_SHAPE_ABOVE_WEAK", "0.3"))
_SHAPE_EXTREME_RET = float(os.getenv("QD_SHAPE_EXTREME_RET", "1.0"))  # 极值幅度门槛%
_SHAPE_DD = float(os.getenv("QD_SHAPE_DD", "0.8"))                    # 距高点回撤%
_SHAPE_MIN_BARS = int(os.getenv("QD_SHAPE_MIN_BARS", "10"))
_SHAPE_RECENT = int(os.getenv("QD_SHAPE_RECENT", "30"))               # 近端窗口（分钟）
_SHAPE_SKIP_OPEN = int(os.getenv("QD_SHAPE_SKIP_OPEN", "3"))          # 定极值时跳过的开盘根数
_SHAPE_LOCK_TOL = float(os.getenv("QD_SHAPE_LOCK_TOL", "0.5"))        # 判封死：距涨跌停价的容差

_LOC_HOT_DIST = float(os.getenv("QD_LOC_HOT_DIST", "6"))
_LOC_HOT_POS = float(os.getenv("QD_LOC_HOT_POS", "76"))
_LOC_HOT_RET5 = float(os.getenv("QD_LOC_HOT_RET5", "7"))
_LOC_COOL_DIST = float(os.getenv("QD_LOC_COOL_DIST", "2"))
_LOC_COOL_POS = float(os.getenv("QD_LOC_COOL_POS", "30"))
_LOC_COOL_RET5 = float(os.getenv("QD_LOC_COOL_RET5", "-1"))


def location_tier(dist_ma20_pct: Optional[float], pos20: Optional[float],
                  ret5_pct: Optional[float], zt_streak: Optional[int] = None) -> str:
    """位置透支分档（纯函数，无 IO）→ "cool" / "normal" / "hot" / "unknown"。

    判据（三件套 dist_ma20_pct / pos20 / ret5_pct 表达同一维度「位置透支度」）：
      hot  = 三件套全高 且 zt_streak != 1；cool = 三件套全低；其余 normal。
      任一项缺失 → "unknown"（缺键无法判定，不猜档）。
    阈值（env，默认值为样本内分位数；单位：% / 0~100 / %）：
      QD_LOC_HOT_DIST=6、QD_LOC_HOT_POS=76、QD_LOC_HOT_RET5=7
      QD_LOC_COOL_DIST=2、QD_LOC_COOL_POS=30、QD_LOC_COOL_RET5=-1
    边界为闭区间（恰好等于阈值归该档）。
    ★ 本函数是**风险提示**，不是准入闸：hot 表示「位置透支、谨慎/降仓」，
      cool 表示「位置干净、可关注」，都不构成买/不买的硬规则（低位不必然反转，
      高位也未必跌）。hot 排除 zt_streak=1 的实测依据（zt+dragon 池 1106 笔）
      与换池复核要求，见 docs/market_screener_deep位置透支设计_20261102.md。
    """
    if dist_ma20_pct is None or pos20 is None or ret5_pct is None:
        return "unknown"
    hot = (dist_ma20_pct >= _LOC_HOT_DIST
           and pos20 >= _LOC_HOT_POS
           and ret5_pct >= _LOC_HOT_RET5
           and zt_streak != 1)
    if hot:
        return "hot"
    cool = (dist_ma20_pct <= _LOC_COOL_DIST
            and pos20 <= _LOC_COOL_POS
            and ret5_pct <= _LOC_COOL_RET5)
    return "cool" if cool else "normal"


_CROSS_STRONG_SHAPES = ("weak_to_strong", "weak_to_divergence",
                        "sustained_strong", "limit_up_locked")
_CROSS_WEAK_SHAPES = ("strong_to_weak", "strong_to_divergence",
                      "sustained_weak", "limit_down_locked")
_CROSS_HIGH_REGIMES = ("高位", "高位分歧")
_CROSS_RISING_REGIMES = ("弱转强", "低位启动")


def cross_verdict(regime: Optional[str], shape: Optional[str]) -> Dict[str, Any]:
    """日线 regime × 分时 shape 的叠加判定 —— 专抓「假强 / 假弱」（纯函数）。

    日线与分时是**两套独立读法**（前者看跨日位置，后者看当日盘中换手），叠加才有意义：
    单看任一侧都会被骗。实测（zt+dragon 池 1106 笔，2026-09-01~10-30，D+1 开盘买）：
      · fake_strong 高位/高位分歧 × 分时偏强 → **133 笔 −3.02%**，深亏 45 只，
        其中 114 只是「高位 + 涨停封死」，**全样本 67 只深亏的 67% 落在这一格**。
        位置已透支，盘中越漂亮越是出货，次日开盘接就是深亏主力。
      · fake_weak 弱转强/低位启动 × 分时偏弱 → **51 笔 +1.70%、胜率 65%、深亏仅 1 只**。
        跨日刚转强、当日盘中回踩，是洗盘不是走坏 —— 别把这种票杀掉。
      · double_strong 两边都强 → 106 笔 +0.82%（正常，但不如 fake_weak 格）。
      · oversold_rebound 低位走弱 × 分时偏强 → 26 笔 +2.50%，**不算假强**（超跌反弹）。
      · high_weak 高位 × 分时偏弱 → 21 笔 −1.88%（位置差且日内也弱，一致但都不好）。
    任一侧缺失返回 verdict=None（不猜）。结论来自涨停/连板来源的池，换池要重新验证。
    """
    if not regime or not shape:
        return {"verdict": None, "day": regime, "min": shape}
    st = shape in _CROSS_STRONG_SHAPES
    wk = shape in _CROSS_WEAK_SHAPES
    if regime in _CROSS_HIGH_REGIMES and st:
        v = "fake_strong"
    elif regime in _CROSS_RISING_REGIMES and wk:
        v = "fake_weak"
    elif regime in _CROSS_RISING_REGIMES and st:
        v = "double_strong"
    elif regime == "低位走弱" and st:
        v = "oversold_rebound"
    elif regime in _CROSS_HIGH_REGIMES and wk:
        v = "high_weak"
    else:
        v = "neutral"
    return {"verdict": v, "day": regime, "min": shape}


def _mean_of(seq) -> float:
    """简单均值（空序列返回 0）；避免为一行引入 statistics 依赖。"""
    seq = list(seq)
    return (sum(seq) / len(seq)) if seq else 0.0


def intraday_shape(ticks: List[tuple], prev_close: Optional[float],
                   t: str = "", limit_pct: float = 0.0) -> Dict[str, Any]:
    """分钟序列 → 分时形态四态之一（纯计算，零 IO）。

    输入 ticks = [(HH:MM, last, cum_volume)] 按时间升序，cum_volume 是当日累计量。
    逐分钟算「截至该分钟的 VWAP」并记录价格是否在其上方（这才是「站上均价线」的本义；
    拿全天最终 VWAP 回头比对会造出「早盘一直在均价上、收盘一算却在下」的假象）。

    四态（`first_extreme` 说明先见低点还是先见高点，是「转」的方向依据）：
      · weak_to_strong        先破低（≤-门槛）→ 收复 VWAP 且近端站稳、当前翻红 ⇒ 买点
      · weak_to_divergence    先破低 → 有过收复但近端没站住（VWAP 附近反复）⇒ 待确认
      · strong_to_weak        先冲高（≥+门槛）→ 跌破 VWAP 且近端压不回 ⇒ 卖点/不接
      · strong_to_divergence  先冲高 → 回撤后卡在 VWAP 附近（未破未创新高）⇒ 多空分歧
      · sustained_weak/strong 全程单边（无转折）
      · flat                  其余（振幅小或数据太短）
    量能配合 vol_up_down = 上涨分钟均量 ÷ 下跌分钟均量（>1=涨放跌缩）。
    """
    n = len(ticks)
    if n < _SHAPE_MIN_BARS or not prev_close or prev_close <= 0:
        return {"ok": False, "reason": "分时不足"}
    prices: List[float] = []
    hours: List[str] = []
    dvols: List[float] = []
    aboves: List[int] = []
    amt = vol = 0.0
    prev_cum = 0.0
    for hh, px, cum in ticks:
        dv = float(cum) - prev_cum
        prev_cum = float(cum)
        if dv < 0:            # 累计量回退（修正/重采）：按 0 计，不造负额
            dv = 0.0
        amt += float(px) * dv
        vol += dv
        vwap_now = (amt / vol) if vol > 0 else float(px)
        aboves.append(1 if float(px) >= vwap_now else 0)
        prices.append(float(px))
        hours.append(str(hh))
        dvols.append(dv)
    vwap = (amt / vol) if vol > 0 else prices[-1]
    cur = prices[-1]
    # 极值要跳过开盘前几根：开盘价天然是大量票的「全天最低」，直接取 argmin 会把
    # 「平开→冲高→回落」误判成「先跌破→再拉高」（实测 10-30 的 437 只里这类占多数，
    # strong_to_divergence 一只都判不出来）。跳过后拿到的是盘中真实的高低点位置。
    k0 = min(max(0, _SHAPE_SKIP_OPEN), max(0, n - 1))
    body = range(k0, n)
    hi_i = max(body, key=lambda i: prices[i])
    lo_i = min(body, key=lambda i: prices[i])
    peak_ret = (prices[hi_i] / prev_close - 1) * 100
    trough_ret = (prices[lo_i] / prev_close - 1) * 100
    cur_ret = (cur / prev_close - 1) * 100
    vwap_dist = (cur / vwap - 1) * 100
    dd = (cur / prices[hi_i] - 1) * 100
    above_ratio = sum(aboves) / n
    recent = min(n, max(1, _SHAPE_RECENT))
    above_recent = sum(aboves[-recent:]) / recent
    lp = float(limit_pct or 0)      # 已是百分数（get_limit_pct 契约：10.0 = ±10%），勿再 ×100
    # 先弱/先强 = 极值出现在**前半段**且幅度够（不是「谁先谁后」）：回落末端的价格
    # 天然低于冲高点，用位置先后会把「平开→冲高→回落」误判成「先跌后涨」——实测
    # 10-30 的 437 只里 strong_to_divergence 一只都判不出来，根因在此。
    half = max(1, n // 2)
    weak_first = bool(trough_ret <= -_SHAPE_EXTREME_RET and lo_i <= half)
    strong_first = bool(peak_ret >= _SHAPE_EXTREME_RET and hi_i <= half)
    if weak_first and strong_first:      # 前半段两头都摸过：幅度大的那头算起点
        weak_first = abs(trough_ret) < abs(peak_ret)
        strong_first = not weak_first
    low_first = lo_i < hi_i
    if lp > 0 and cur_ret >= lp - _SHAPE_LOCK_TOL:
        # 贴在涨停价上（盘中开过板又回封也算）——「转」已无意义，买卖含义变成
        # 「次日看开板」；开板过程由 dd_from_peak / above_ratio 描述，不在这里分叉
        shape = "limit_up_locked"
    elif lp > 0 and cur_ret <= -(lp - _SHAPE_LOCK_TOL):
        shape = "limit_down_locked"
    elif weak_first and vwap_dist > _SHAPE_VWAP_TOL and above_recent >= _SHAPE_ABOVE_RECENT \
            and cur > prev_close:
        shape = "weak_to_strong"
    elif weak_first and above_ratio >= _SHAPE_ABOVE_WEAK and vwap_dist > -_SHAPE_VWAP_TOL:
        shape = "weak_to_divergence"
    elif strong_first and vwap_dist < -_SHAPE_VWAP_TOL \
            and above_recent <= (1 - _SHAPE_ABOVE_RECENT):
        shape = "strong_to_weak"
    elif strong_first and dd <= -_SHAPE_DD and vwap_dist > -_SHAPE_VWAP_TOL:
        shape = "strong_to_divergence"
    elif vwap_dist <= -_SHAPE_VWAP_TOL and above_ratio < _SHAPE_ABOVE_WEAK:
        shape = "sustained_weak"
    elif vwap_dist > _SHAPE_VWAP_TOL and above_recent >= _SHAPE_ABOVE_RECENT:
        shape = "sustained_strong"
    else:
        shape = "flat"
    up = [d for i, d in enumerate(dvols) if i and prices[i] > prices[i - 1]]
    dn = [d for i, d in enumerate(dvols) if i and prices[i] < prices[i - 1]]
    mu, md = _mean_of(up), _mean_of(dn)
    vd = (mu / md) if (up and dn and md > 0) else None
    return {
        "ok": True,
        "t": str(t)[:5],
        "shape": shape,
        "first_extreme": ("low" if low_first else "high"),
        "vwap_dist": round(vwap_dist, 2),
        "above_ratio": round(above_ratio, 2),
        "above_recent": round(above_recent, 2),
        "cur_ret": round(cur_ret, 2),
        "peak_ret": round(peak_ret, 2),
        "trough_ret": round(trough_ret, 2),
        "dd_from_peak": round(dd, 2),
        "vol_up_down": (round(vd, 2) if vd else None),
    }


def intraday_shape_batch(codes: List[str], same_t: Optional[str] = None,
                         day: Optional[str] = None,
                         names: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """批量取当日分钟序列并算四态形态 → {code: 形态块}（只含成功项）。

    day 缺省用 `current_data_day()`（交易日且已过 09:15 = 今天，否则上一交易日）：
    盘前做次日决策时，要看的是**最近那个完整交易日**的分时过程，不是空数据。
    same_t 缺省用 `_now_hhmm()`（交易时段=此刻，否则收盘）。
    一条 `symbol = ANY(...)` 约 45 只票 × 240 拍 ≈ 1 万行、60ms，与票数近似无关。
    """
    uni: List[str] = []
    for c in codes or []:
        s = str(c or "").split(".")[0].strip()
        if s and s not in uni:
            uni.append(s)
    if not uni:
        return {}
    t = str(same_t)[:5] if same_t else _now_hhmm()
    if day:
        d = str(day)[:10]
    else:
        try:
            from app.utils.trading_calendar import current_data_day
            d = current_data_day()
        except Exception as e:
            logger.warning("[MktScreen] 目标交易日缺失，分时形态回落今日: %s", e)
            d = datetime.now().strftime("%Y-%m-%d")
    # 取数走 screen_query.ticks（唯一实现：批量 `= ANY(...)` + 09:30 起 + last/volume>0）
    try:
        got = _sq.ticks(uni, day=d, t_to=t)
        if not got and d == datetime.now().strftime("%Y-%m-%d"):
            # 09:15~09:30：current_data_day 已切到今天，但今天还没有第一笔 tick。
            # 盘前/开盘前决策看的是**上一完整交易日**的分时过程，不是空数据。
            from app.utils.trading_calendar import last_finish_trading_day
            d_prev = str(last_finish_trading_day())[:10]
            if d_prev and d_prev != d:
                got = _sq.ticks(uni, day=d_prev, t_to=t)
    except Exception as e:
        logger.warning("[MktScreen] 分时形态取数失败: %s", e)
        return {}
    bucket: Dict[str, Dict[str, Any]] = {
        code: {"ticks": b["ticks"], "prev_close": b["prev_close"]}
        for code, b in got.items()
    }
    out: Dict[str, Dict[str, Any]] = {}
    from .common import get_limit_pct
    for code, b in bucket.items():
        try:
            # names 缺省 ""：N/C 新股无法识别 → 仍走板块幅度（与旧行为一致，
            # 不退化）；传入 name 才能识别新股无涨跌幅，避免误标 limit_up_locked。
            _nm = (names or {}).get(code, "") if names else ""
            lp = get_limit_pct(code, _nm)
        except Exception as e:      # 涨跌停幅度取不到 ⇒ 只影响「封死」标签，其余照算
            logger.warning("[MktScreen] 涨跌停幅度缺失 %s: %s", code, e)
            lp = 0.0
        sh = intraday_shape(b["ticks"], b["prev_close"], t, lp)
        if sh.get("ok"):
            sh.pop("ok", None)
            out[code] = sh
    return out


def flow_summary(code: str) -> Dict[str, Any]:
    """个股资金流客观事实（单位：万元）：当日 + 近5/20日总体 + 日内时段结构。

    2026-10-30 扩展（用户缺陷①②：只看当天看不出资金「总体」，也防不住开盘几千万
    拉升后抛盘）—— 两个既有工具各取所需，不做重复聚合：
      · `get_fund_flow_realtime`                当日分钟净额 → `intraday`（开盘/其后分段）
      · `get_fund_flow(scope="stock_daily", days=30)` 日线聚合 → `recent`（5/20日累计、
        连续方向、逐日符号、剔除最大单日、上一完整交易日、资金趋势判定）
    失败返回 ok=False + 原因，不静默；recent / intraday 各自带 ok，单块失败不拖垮另一块。

    口径提醒（勿按「主力资金」读取）：net_wan 是**量价方向净额**（按分钟涨跌方向
    累计成交额，见 fund_flow_tools 头注），不是东财四档主力净额，两者不可换算；
    `caliber`/`caliber_note` 两个键把这件事写进载荷本身，键名不再自称 main_net。
    跨票比较用 *_pct，跨日看方向序列，绝对额只作门槛。

    Returns:
        {ok, net_wan, caliber, caliber_note, net_pct_of_amount,
         recent: {ok, as_of_day, last_day_net_wan, net_5d_wan, net_20d_wan, ratio_5d_pct, ratio_20d_pct,
                  streak_dir, streak_days, sign_5d, max_day, net_20d_ex_max_wan,
                  prev_day_net_wan, trend},
         intraday: {ok, open_net_wan, after_net_wan, open_share_pct, points},
         reason?}
    """
    out: Dict[str, Any] = {}
    # ── 当日：分钟净额（realtime_snapshot 量价派生）──
    try:
        from .common import call_tool
        ff = call_tool("get_fund_flow_realtime", code=code)
        if not isinstance(ff, dict) or ff.get("error"):
            out["reason"] = (ff or {}).get("error", "无返回")
        else:
            # 三对账修正（2026-09-30）：tape 返回的真实键是 total_main_net（单位元）——
            # 旧读 main_net_inflow 恒 None，主力净流入被算成恒 0。2026-10-30 补：占比
            # 一直为 None 的根因是键名取错——该返回带 main_pct / total_amount，直接读。
            raw_net = ff.get("total_main_net")
            if raw_net is None:
                raw_net = ff.get("main_net_inflow")
            raw_net = _num_or_none(raw_net)
            pct = _num_or_none(ff.get("main_pct"))
            if raw_net is not None and pct is None:
                amount = _f(ff.get("total_amount") or ff.get("amount") or ff.get("turnover"))
                pct = round(raw_net / amount * 100, 2) if amount else None
            if raw_net is None:
                out["reason"] = "当日资金净额缺失"
            else:
                out["net_wan"] = round(raw_net / 1e4, 1)
            out["net_pct_of_amount"] = pct
            out["intraday"] = _intraday_shape(ff.get("data") or [])
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"

    # ── 近5/20日总体：日线聚合（含资金趋势判定，stock_daily dispatch 已附）──
    fp_today = None
    fp_day = ""
    fp_last_net_wan = None
    try:
        from ...tools.finance.fund_flow_tools import get_fund_flow
        df = get_fund_flow(scope="stock_daily", codes=code, days=30) or {}
        one = (df.get("data") or {}).get(code) or {}
        if isinstance(one, dict) and not one.get("error") and one.get("data"):
            rows = one.get("data") or []
            today = datetime.now().strftime("%Y-%m-%d")
            prev_wan = None
            for r in reversed(rows):
                if str(r.get("date") or "")[:10] < today and r.get("main_net") is not None:
                    prev_wan = round(_f(r.get("main_net")) / 1e4, 1)
                    break
            fp_day = max((str(r.get("date") or "")[:10] for r in rows), default="")
            for r in rows:
                if str(r.get("date") or "")[:10] != fp_day:
                    continue
                last_net = _num_or_none(r.get("main_net"))
                if last_net is not None:
                    fp_last_net_wan = last_net / 1e4
                v = r.get("main_pct")
                if v is None and r.get("main_net") is not None and _f(r.get("turnover")):
                    v = _f(r["main_net"]) / _f(r["turnover"]) * 100
                if v is not None:
                    fp_today = float(v)
                    break
            net_5d = _num_or_none(one.get("recent_5d_main_net"))
            net_20d = _num_or_none(one.get("recent_20d_main_net"))
            net_20d_ex_max = _num_or_none(one.get("recent_20d_main_net_ex_max"))
            out["recent"] = {
                "ok": True,
                "as_of_day": fp_day or None,
                "last_day_net_wan": fp_last_net_wan,
                "net_5d_wan": round(net_5d / 1e4, 1) if net_5d is not None else None,
                "net_20d_wan": round(net_20d / 1e4, 1) if net_20d is not None else None,
                "ratio_5d_pct": one.get("recent_5d_net_ratio"),
                "ratio_20d_pct": one.get("recent_20d_net_ratio"),
                "streak_dir": one.get("main_net_streak_dir"),
                "streak_days": one.get("main_net_streak"),
                "sign_5d": one.get("recent_5d_sign"),
                "max_day": one.get("max_main_net_day"),
                "net_20d_ex_max_wan": round(net_20d_ex_max / 1e4, 1) if net_20d_ex_max is not None else None,
                "prev_day_net_wan": prev_wan,
                "trend": one.get("资金趋势判定"),
            }
        else:
            out["recent"] = {"ok": False, "reason": (one or {}).get("error") or "资金日线数据为空"}
    except Exception as e:
        out["recent"] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    if fp_today is not None:
        _mkt = market_calib_stats(day=(fp_day or None))
        if _mkt.get("fp_pct") is not None:
            out["flow_rel"] = round(fp_today - _mkt["fp_pct"], 2)

    out["caliber"] = "price_direction"
    out["caliber_note"] = "量价方向净额（非主力）"
    out["ok"] = "net_wan" in out
    return out


# ═══════════════════════════════════════════════════════════════
#  实时行情覆盖层（bar vs rt 对账，2026-10-14）
# ═══════════════════════════════════════════════════════════════
# 【为什么必须有】deep_analyze 的 tech 只吃 fetch_kline（本地日线）。盘后本地库
# 未刷当日 bar 时，整份证据是旧数据 —— 实测 2026-10-14：600408 bar close 3.69
# / +4.53% / limit_up=false，实时 4.06 / +10.03% / 涨停封板。主 agent 闻到矛盾
# 就会在主对话逐只重拉 quote/flow/tech（一轮烧十几个 LLM 步）。
# 【对账原则】历史指标（MA/RSI/20 日位置）仍基于日线（改了就不自洽）；
# 可执行性字段（当日收盘 / 涨跌幅 / 涨跌停）以 rt 为准，并显式标 stale_bar。

# bar close 与 rt last 相对偏差超过该阈值 ⇒ 判定日线滞后
_STALE_BAR_REL_TOL = 0.005


def fetch_rt_map(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量实时行情 → {code: rt事实}；2026-10-20 改分块绕过 20 只截断。

    失败块只丢该块并 logger.warning，不整批作废；返回 map 只含成功票，失败票缺席。
    【三态契约（2026-10-21 C1）】change_pct / limit_up_price / limit_down_price
    缺失时保留 **None**（源未给 ≠ 0）：0/-1 是「无涨跌幅限制」哨兵、None 是
    「未知」，_f(None)=0.0 会把未知伪造成 0 价 → 下游判 no_limit → 封板票
    buyable=True 进可买表、涨跌幅显示 +0.00%（与 _facts.fill_quote 的
    「0 是合法哨兵、仅排 None」同一口径）。
    """
    if not codes:
        return {}
    from .common import batch_realtime_quotes
    try:
        quotes, failed = batch_realtime_quotes(codes)
    except Exception as e:
        logger.warning("[MktScreen] 实时行情分块失败: %s", e)
        return {}
    if failed:
        logger.warning("[MktScreen] 实时行情分块失败 %d 块，涉及 %d 只",
                       len(failed), sum(len(c) for c in failed))
    out = {}
    for code in codes:
        info = quotes.get(code)
        if not isinstance(info, dict):
            continue
        last_raw = info.get("last") or info.get("price")
        if last_raw is None:
            continue
        last = _f(last_raw)
        out[code] = {
            "price": last,
            "change_pct": _num_or_none(info.get("changePercent")),
            "previous_close": _f(info.get("previousClose")),
            "limit_up_price": _num_or_none(info.get("limit_up")),
            "limit_down_price": _num_or_none(info.get("limit_down")),
            "high": _f(info.get("high")),
            "low": _f(info.get("low")),
            "time": info.get("time"),
            "name": info.get("name"),
        }
    return out


def apply_rt_overlay(row: Dict[str, Any], rt: Optional[Dict[str, Any]],
                     code: str = "", name: str = "") -> None:
    """把实时行情对账进证据行（原地改 row）。

    · row["rt"]：实时快照（缺 rt 时不写，不塞 None 壳）；含 high/low（2026-10-30 补，
      供一字判定与冲高回落——此前 rt 无 high，pump_dump_risk 的该分支从未命中）
    · row["stale_bar"]：bar close 与 rt last 相对偏差 > 0.5% ⇒ True
    · tech.limit_up / 顶层可执行价：以 rt 为准修正（涨停买不进是 hard 条件）
    · tech 的 MA/RSI/位置类指标不动（历史几何，rt 改不了也不该改）
    """
    if not rt:
        return
    from .common import get_limit_pct, is_limit_locked

    tech = row.get("tech") if isinstance(row.get("tech"), dict) else {}
    bar_close = _f(tech.get("close")) if tech else None
    rt_price = _f(rt.get("price"))
    stale = False
    if bar_close and rt_price and bar_close > 0:
        stale = abs(rt_price - bar_close) / bar_close > _STALE_BAR_REL_TOL

    prev_close = _f(rt.get("previous_close"))
    if prev_close is None and tech:
        # bar 的 prev 不在 tech 里；用 bar close 反推不到就留 None，limit_up 用涨跌停价判
        prev_close = None

    # 涨跌停真值：优先「现价 vs 涨停价」。quote 侧哨兵：
    #   >0  = 有效涨跌停价
    #   <=0 = 明确「无涨跌停」（新股上市首日/无限制）→ 不得回退公式，否则 N 联亚会被误判涨停
    #   缺失 = 源无此字段 → 才用 get_limit_pct 近似
    # 2026-10-15 实测：N联亚药业(301569) -1/-1；*ST天成(600112) -1/-1；ST 且 quote 价与 get_limit_pct 不一致。
    def _parse_limit_px(v) -> tuple:
        if v is None or v == "":
            return None, "missing"
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None, "missing"
        if x > 0:
            return x, "price"
        return None, "no_limit"

    limit_up_price, up_kind = _parse_limit_px(rt.get("limit_up_price"))
    limit_down_price, _dn_kind = _parse_limit_px(rt.get("limit_down_price"))
    if up_kind == "price" and rt_price:
        limit_up = bool(rt_price >= float(limit_up_price) - 0.01)
    elif up_kind == "no_limit":
        limit_up = False
    elif rt_price and prev_close:
        limit_up = bool(is_limit_locked(code or row.get("code", ""), name or row.get("name", ""),
                                        rt_price, prev_close))
    else:
        limit_up = None

    # 跌停封死（无流动性）：与涨停对称，供「不可买」判定
    limit_down = None
    if limit_down_price and rt_price:
        limit_down = bool(rt_price <= float(limit_down_price) + 0.01)
    elif _dn_kind == "no_limit":
        limit_down = False

    row["rt"] = {
        "price": rt.get("price"),
        "change_pct": round(rt["change_pct"], 2) if rt.get("change_pct") is not None else None,
        "previous_close": rt.get("previous_close"),
        # high/low 2026-10-30 补：① 一字判定（low≈涨停价 → 买不到）；② pump_dump_risk
        # 的冲高回落分支读 rt.high，此前 rt 从不带 high ⇒ 该分支恒不命中（静默失效）。
        "high": rt.get("high"),
        "low": rt.get("low"),
        "limit_up": limit_up,
        "limit_down": limit_down,
        "limit_up_price": limit_up_price,
        "limit_down_price": limit_down_price,
        "limit_band": up_kind,  # price | no_limit | missing（跌停侧同 kind 语义，无限制时两价皆空）
        "time": rt.get("time"),
    }
    # 可买性：封板（涨/跌）当日不可买 —— 写进 rt_risks，LLM 不得放入可买表。
    # 三态（2026-10-21 C1）：任一侧涨跌停状态未知（missing 且无昨收可近似）→
    # buyable=None（待定，不可确认可买），**绝不给 True** —— 旧逻辑「一侧已知
    # False 即 True」会在另一侧状态不明时漏掉封板票；limit_band="missing" 即
    # 「未知」的可读标注（missing≠no_limit，见 references/limit-rules.md）。
    if limit_up is True or limit_down is True:
        row["rt"]["buyable"] = False
    elif limit_up is None or limit_down is None:
        row["rt"]["buyable"] = None
    else:
        row["rt"]["buyable"] = True
    row["stale_bar"] = stale

    if tech:
        if limit_up is not None:
            tech["limit_up"] = limit_up
        if stale and rt_price:
            # 可执行性口径以 rt 为准；历史指标保留 bar 几何
            tech["close"] = round(float(rt_price), 2)
            if rt.get("change_pct") is not None:
                tech["change_pct"] = round(float(rt["change_pct"]), 2)
            tech["bar_close"] = round(float(bar_close), 2) if bar_close else None
            # 位置类指标依赖现价：bar 落后时按 rt 重算，否则 loc 会卡在旧收盘价分档
            # （仅当锚点齐备才重算；缺锚点保持 bar 值，不猜）。
            _ma20 = _f(tech.get("ma20"))
            _hi20 = _f(tech.get("hi20"))
            _lo20 = _f(tech.get("lo20"))
            _base = _f(tech.get("ret5_base_close"))
            if _ma20:
                tech["dist_ma20_pct"] = round((rt_price - _ma20) / _ma20 * 100, 2)
            if _hi20 and _lo20 and _hi20 > _lo20:
                tech["pos_in_20d_pct"] = round(
                    (rt_price - _lo20) / (_hi20 - _lo20) * 100, 1)
            if _base:
                tech["ret_5d_pct"] = round((rt_price / _base - 1) * 100, 2)
            tech["location"] = location_tier(
                tech.get("dist_ma20_pct"), tech.get("pos_in_20d_pct"),
                tech.get("ret_5d_pct"), tech.get("zt_streak"))
        tech["data_asof"] = "rt" if (stale or limit_up is not None) else "bar"

    if name and not row.get("name"):
        row["name"] = name
    if row.get("tech") is not None and limit_up:
        # 买不进：客观事实标注（deep_analyze 不做淘汰，只标事实）。
        # 不再要求 limit_up_price 有值（2026-10-21 C1）：价缺失但按公式近似判出
        # 封板时，同样要留痕，否则 rt.buyable=false 与 rt_risks 口径分裂。
        risks = row.setdefault("rt_risks", [])
        if "涨停封板-当日买不进" not in risks:
            risks.append("涨停封板-当日买不进")
    if limit_down is True:
        risks = row.setdefault("rt_risks", [])
        if "跌停封死-无流动性" not in risks:
            risks.append("跌停封死-无流动性")
    if stale:
        risks = row.setdefault("rt_risks", [])
        note = f"日线滞后(bar_close={bar_close}, rt={rt_price})"
        if note not in risks:
            risks.append(note)


# ═══════════════════════════════════════════════════════════════
#  T+1 标定预测（P(次日涨)×100）
# ═══════════════════════════════════════════════════════════════
# 复用 app.watchlist.predict 的已标定模型（AUC≈0.61），而非手拍技术分。
# 2026-09-29：旧 enrich_predictive 会按情绪分桶**替 LLM 裁剪掉**候选（门槛 + 条数上限），
# 现改为「只给分数和建议」，裁剪权交还 LLM（详见 SKILL.md 的评分语义段）。

_MKT_KLINES_CACHE = {"data": None}
# 冷启互斥（2026-10-25 P1-①）：后台预热线程与画像期首票 predict_one 可能
# 并发触发 _market_klines()。无锁时双方都 miss ⇒ 各建一次 mootdx 客户端
# （bestip 扫描 ~74s/次），纯浪费。双重检查锁保证整个进程只取一次。
_MKT_KLINES_LOCK = threading.Lock()


def _market_klines():
    if _MKT_KLINES_CACHE["data"] is None:
        with _MKT_KLINES_LOCK:
            if _MKT_KLINES_CACHE["data"] is None:
                try:
                    from app.watchlist.predict import load_market_klines
                    _MKT_KLINES_CACHE["data"] = load_market_klines(120) or []
                except Exception:
                    _MKT_KLINES_CACHE["data"] = []
    return _MKT_KLINES_CACHE["data"]


def prewarm_market_klines() -> None:
    """后台预热指数日线（2026-10-25 P1-①）。粗筛/细筛两轮入口都调，内容相同。

    根因（主探针 probe_screener_timing_20261024 实锤）：画像期
    attach_prediction 的首个 predict_one 触发 _market_klines() 冷启 →
    get_index_daily_kline("000001",120) → mootdx 客户端创建（冷进程里
    provider.tdx_ex 无 live 服务器 ⇒ bestip 全扫 ~74s）+ 腾讯兜底，
    单点 78.35s，占 run() 总耗时 44%。该取数与选人层取数无依赖 ⇒ run() 入口
    起后台线程预热，重叠后单点基本隐藏。失败仅记 warning：主流程到时按原
    路径自取，行为与无预热时一致（不静默吞，见模块头纪律）。

    ★ 预热内容只有指数 K 线（predict_one 的唯一外部依赖）：个股 K 线走
      fetch_kline → CNStockDataSource 门面，无进程级缓存可填充，按票预热
      无从谈起（2026-11-25 审查：删除曾在此假挂的 selected_codes 参数——
      它只进日志文案、不改变预热内容，「细筛轮只预热目标股票」从未成立）。
    """
    def _bg():
        try:
            _market_klines()
            logger.info("[market-screener] 指数K线后台预热完成")
        except Exception as e:
            logger.warning("[market-screener] 指数K线后台预热失败: %s", e)

    threading.Thread(target=_bg, name="mktkl-prewarm", daemon=True).start()


def _norm_bars(bars: list) -> list:
    """fetch_kline 的 time 是日期串；predict._by_day 要 int 时间戳。"""
    from datetime import datetime as _dt
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        x = dict(b)
        tm = x.get("time")
        if isinstance(tm, str):
            try:
                x["time"] = int(_dt.strptime(tm[:10], "%Y-%m-%d").timestamp())
            except Exception:
                continue
        out.append(x)
    return out


def _mood_regime(market: dict) -> str:
    """情绪分桶：strong / neutral / weak（只作上下文标签，不再用于裁剪）。

    取**大盘档**（market_mood_score）——它回答"指数与资金强不强"，才是决定持仓
    姿态的维度；个股档（stock_mood_score）衡量"钱好不好赚"，供 `_hints` 另作收紧，
    不参与本分桶以免两者背离时互相抵消。
    """
    market = market or {}
    mood = str(market.get("market_mood") or "")
    try:
        ms = float(market.get("market_mood_score", 50) or 50)
    except Exception:
        ms = 50.0
    if mood in ("弱势", "偏弱") or ms < 40:
        return "weak"
    if mood in ("偏强",) or ms >= 70:
        return "strong"
    return "neutral"


def predict_one(code: str, bars: List[Dict], regime: str = "neutral") -> Dict[str, Any]:
    """给单只票打 P(T+1 涨) —— 客观模型输出 + 可选的弱势折价标记（折价不自动生效）。

    Returns:
        {ok, p_up, exp_ret_bp, model:{beta,regime,zhuang,...}, note}
    """
    try:
        from app.watchlist.predict import predict_next_day
    except Exception as e:
        return {"ok": False, "reason": f"预测模块不可用: {e}"}
    bars = _norm_bars(bars or [])
    if not bars:
        return {"ok": False, "reason": "日线为空"}
    try:
        pred = predict_next_day(bars, _market_klines())
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    if not pred or pred.get("score") is None:
        return {"ok": False, "reason": "模型未给出分数"}
    p_up = float(pred.get("p_up") or 0.5)
    return {
        "ok": True,
        "p_up": round(p_up, 4),
        "exp_ret_bp": pred.get("exp_ret_bp"),
        "model": {
            "beta": (pred.get("beta") or {}).get("beta"),
            "regime": (pred.get("beta") or {}).get("regime"),
            "zhuang": (pred.get("zhuang") or {}).get("score"),
            "market_p_up": (pred.get("market") or {}).get("p_up"),
        },
        "mood_regime": regime,
        "suggest_dir": "bullish" if p_up >= 0.55 else ("bearish" if p_up <= 0.45 else "neutral"),
    }


def attach_prediction(items: List[Dict], market: dict = None) -> List[Dict]:
    """批量挂 P(T+1 涨) 分数。不改变成员，不裁剪，不重排。"""
    if not items:
        return []
    regime = _mood_regime(market)
    from .common import fetch_kline
    out = []
    for it in items:
        item = dict(it)
        item.setdefault("missing", [])
        # 保留原始分（对照用），再让 score 统一为 P(次日涨)×100
        item.setdefault("tech_score", item.get("score", 50))
        bars = item.get("_bars") or []
        if not bars and item.get("code"):
            bars = fetch_kline(item["code"], days=120)
        pr = predict_one(item.get("code", ""), bars, regime)
        item["has_pred"] = bool(pr.get("ok"))
        if pr.get("ok"):
            item["score"] = round(pr["p_up"] * 100, 1)
            item["direction"] = pr["suggest_dir"]
            item["p_up"] = pr["p_up"]
            item["exp_ret_bp"] = pr["exp_ret_bp"]
            item["pred_model"] = pr["model"]
            item["suggest_dir"] = pr["suggest_dir"]
        else:
            item["missing"].append(f"预测缺失:{pr.get('reason')}")
        item["_pred"] = pr
        out.append(item)
    return out


def enrich_predictive(results: list, market: dict = None) -> list:
    """兼容旧名：打分 + 按情绪门槛标记 selected + 重排（⚠ 原本的契约用例
    test_market_screener_pred 已删除——2026-10-14 核实 tests/ 下只剩 __pycache__
    字节码，无 .py；此处契约改为由 qd_smoke 的选股项与基线对拍守卫覆盖）。

    2026-09-29：主流程改用 attach_prediction()（只打分不裁剪，取舍交给 LLM）。
    本函数保留「门槛裁剪 + selected 标记」语义，供既有调用方/回归测试使用，请勿在新代码中调用。
    """
    if not results:
        return []
    regime = _mood_regime(market)
    thr = {"weak": 0.55, "neutral": 0.50, "strong": 0.48}[regime]
    cap = {"weak": 5, "neutral": 8, "strong": 10}[regime]

    items = attach_prediction(list(results), market=market)
    for it in items:
        it.pop("_pred", None)
        p = it.get("p_up")
        if p is None:
            continue
        # 情绪弱时对涨停活跃源折价：连板/炸板题材在弱势市次日溢价差
        src = str(it.get("source") or "")
        if regime == "weak" and any(k in src for k in ("连板", "4IN1", "龙回头", "涨停")):
            p = max(0.05, p * 0.92)
            it["p_up"] = round(p, 4)
            it["score"] = round(p * 100, 1)
            it["mood_haircut"] = 0.92
            it["direction"] = "bullish" if p >= 0.55 else ("bearish" if p <= 0.45 else "neutral")

    kept = [x for x in items if x.get("has_pred") and (x.get("p_up") or 0) >= thr]
    kept.sort(key=lambda x: x.get("score") or 0, reverse=True)
    kept = kept[:cap]
    kept_codes = [x.get("code") for x in kept]
    for x in items:
        x["selected"] = x.get("code") in kept_codes and x.get("has_pred")
    dropped = [x for x in items if not x.get("selected")]
    dropped.sort(key=lambda x: x.get("score") or x.get("tech_score") or 0, reverse=True)
    return kept + dropped


# ═══════════════════════════════════════════════════════════════
#  候选画像（证据卡）
# ═══════════════════════════════════════════════════════════════

def build_report(results: list, selected_codes: List[str] = None):
    """从分析结果列表构建 SkillReport。

    selected_codes 非空时只汇总指名标的；为空时按 Top5（保持旧口径，向后兼容）。
    注意：这里是「汇总」不是「筛选」——真正的取舍在 LLM 侧完成。
    """
    from .common import SkillReport

    valid = [r for r in results if r is not None and isinstance(r, dict)]
    if not valid:
        return SkillReport(
            skill_name="market-screener",
            score=50.0,
            signal="无有效分析结果",
        )

    picked = [v for v in valid if (not selected_codes) or str(v.get("code")) in {str(c) for c in selected_codes}]
    if not picked:
        picked = valid

    _ranked = sorted(picked, key=lambda v: v.get("score") or 0, reverse=True)
    scores = [v.get("score", 50) for v in _ranked[:5]] or [50]
    directions = [v.get("direction", "neutral") for v in picked]
    avg_score = sum(scores) / len(scores)

    bullish = directions.count("bullish")
    bearish = directions.count("bearish")
    if bullish > bearish and bullish > len(picked) * 0.3:
        direction = "bullish"
    elif bearish > bullish and bearish > len(picked) * 0.3:
        direction = "bearish"
    else:
        direction = "neutral"

    confs = [v.get("confidence", 0.5) for v in picked]
    avg_conf = sum(confs) / len(confs) if confs else 0.5

    return SkillReport(
        skill_name="market-screener",
        score=round(avg_score, 1),
        direction=direction,
        confidence=round(avg_conf, 2),
        signal=f"分析 {len(picked)} 只（候选 {len(valid)} 只）",
        output_data={"analyzed": valid},
    )


# ═══════════════════════════════════════════════════════════════
#  旧流程兼容层
# ═══════════════════════════════════════════════════════════════
