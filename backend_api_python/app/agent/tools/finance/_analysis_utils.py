# -*- coding: utf-8 -*-
"""
分析工具共享函数。

所有 tools/*.py 中的数据获取、格式化、指标计算辅助函数统一放这里。
文件名以 _ 开头，registry 自动跳过，不注册为 tool。

─── 输出格式约定 (output 参数) ───────────────────────────────

  markdown / 单股 ── 紧凑中文字符串，面向 LLM/Agent，省 token。
  markdown / 多股 ── TSV 表格（制表符分隔），面向 LLM/Agent，省 token。
  json             ── 完整 dict，面向代码层调用（bull_bear_research 等）。

设计原则：
  - markdown/tsv 是给人和 LLM 看的，核心目标是省 token，用中文 label，紧凑拼接。
  - json 是给代码用的，保留完整结构，不做格式化。
  - 单股 markdown 允许用 str 直接返回（数据量极低时无需结构化）。
  - 多股 markdown 必须用 TSV 表格（一行一股，避免重复 header 浪费 token）。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
# 数据源
# ═══════════════════════════════════════════════════════════════

def _get_ds(market: str = "CNStock"):
    """获取数据源实例。"""
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)


def _fetch_klines(stock_code: str, days: int = 120) -> List[Dict[str, Any]]:
    """获取原始K线数据（含 OHLCV）。"""
    ds = _get_ds("CNStock")
    return ds.get_kline(stock_code, "1D", days) or []


def _fetch_closes(stock_code: str, days: int = 120) -> List[float]:
    """获取收盘价序列。"""
    klines = _fetch_klines(stock_code, days)
    return [float(k.get("close", 0)) for k in klines if k.get("close")]


def _fetch_ohlcv(stock_code: str, days: int = 120) -> Dict[str, Any]:
    """获取 OHLCV 五组数据序列 + 末根K线日期/时间戳（last_date/last_ts）。

    last_date/last_ts 供 `volume_ratio_summary` 判断「末根是否今天」（盘中量比
    归一化要用）；数据源没给则为 None，消费方按「判不出」处理，不猜。
    """
    klines = _fetch_klines(stock_code, days)
    o, h, l, c, v = [], [], [], [], []
    for k in klines:
        o.append(float(k.get("open", 0)))
        h.append(float(k.get("high", 0)))
        l.append(float(k.get("low", 0)))
        c.append(float(k.get("close", 0)))
        v.append(float(k.get("volume", 0)))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "last_date": (klines[-1].get("date") if klines else None),
            "last_ts": (klines[-1].get("time") if klines else None)}


# ═══════════════════════════════════════════════════════════════
# 数值工具
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 量比口径统一接口（2026-10-22 盘中归一化修复）
# ═══════════════════════════════════════════════════════════════
#
# 【修什么】量比 = 末根量 / 近5日均量。盘中调用时末根是**未走完的当日**
#   （10:13 仅完成约 43/240 分钟），除以 5 日**全天**均量 ⇒ 系统性低估：
#   实测 603067 2026-10-22 10:13 给出 0.227，模型被迫在输出里手工换算约 1.27。
# 【怎么修】末根是「今天且盘中」：今日量 ÷ 已交易时间占比 = 预估全天量，
#   再与**今天之前 5 根完整日线**均量比；置 vol_ratio_normalized=True 供识别。
# 【边界】仅日线（daily=True）归一化：分钟线末根本身完整、周线「未走完」语义
#   不同，均不套用（保持原式并标 vol_ratio_normalized=False）。
#   末根日期解析不出 → 不归一化（判不出就承认判不出，不猜）。

_CN_SESSION_MIN = 240  # 9:30-11:30 + 13:00-15:00


def _cn_now():
    """当前中国时间；zoneinfo 不可用时退回本机本地时间（显式降级）。"""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return datetime.now()


def _trading_elapsed_fraction(now=None) -> float:
    """A股已交易分钟数占全天 240 分钟的比例：盘前 0.0 / 盘后 1.0 / 盘中 (0,1)。"""
    now = now or _cn_now()
    m = now.hour * 60 + now.minute
    if m <= 9 * 60 + 30:
        return 0.0
    if m >= 15 * 60:
        return 1.0
    if m < 11 * 60 + 30:
        elapsed = float(m - (9 * 60 + 30))
    elif m <= 13 * 60:
        elapsed = 120.0
    else:
        elapsed = 120.0 + (m - 13 * 60)
    return min(max(elapsed / _CN_SESSION_MIN, 0.0), 1.0)


def _bar_is_today(last_bar_date=None, last_bar_ts=None, now=None) -> bool:
    """末根K线是否「今天」。时间戳优先；日期串宽松解析，解析不出 → False。

    日期串兼容 "2026-10-22" / "20261022" / "2026/10/22" / "10-22" / 带时间尾巴。
    只有月日（%m-%d，来自 data_tools._ts_to_date）时比月日——250 日窗口的
    最后一根不可能是上年同月日，误判风险可忽略。
    """
    now = now or _cn_now()
    if last_bar_ts:
        try:
            from datetime import datetime
            ts = int(float(last_bar_ts))
            try:
                from zoneinfo import ZoneInfo
                d = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Shanghai"))
            except Exception:
                d = datetime.fromtimestamp(ts)
            return (d.year, d.month, d.day) == (now.year, now.month, now.day)
        except Exception:
            pass
    if not last_bar_date:
        return False
    raw = re.findall(r"\d+", str(last_bar_date))
    if not raw:
        return False
    nums = [int(x) for x in raw]
    if len(raw[0]) == 8 and len(raw) == 1:            # 20261022
        v = raw[0]
        y, mo, d = int(v[:4]), int(v[4:6]), int(v[6:8])
    elif len(raw[0]) == 4 and len(raw) >= 3:          # 2026-10-22（可带时间尾巴）
        y, mo, d = nums[0], nums[1], nums[2]
    elif len(nums) >= 2:                              # 10-22（无年份）
        return (nums[0], nums[1]) == (now.month, now.day)
    else:
        return False
    return (y, mo, d) == (now.year, now.month, now.day)


def volume_ratio_summary(vols: List[float], last_bar_date=None, last_bar_ts=None,
                         daily: bool = True, now=None) -> Dict[str, Any]:
    """近5日均量 + 量比，盘中自动做交易时间归一化（口径见本节头注释）。

    返回键：vol_ma5 / vol_ratio / vol_ratio_normalized；窗口不足 → **不给键**
    （缺就是缺，不回落 0）。盘中模式基准取今天之前 5 根**完整**日线（
    vol_ma5 语义 = 近5个完整交易日均量），完整日不足 5 根则不给量比。
    """
    out: Dict[str, Any] = {}
    n = len(vols)
    if n < 2:
        return out
    frac = _trading_elapsed_fraction(now)
    intraday_partial = (daily
                        and _bar_is_today(last_bar_date, last_bar_ts, now)
                        and 0.0 < frac < 1.0)
    if intraday_partial:
        base = vols[-6:-1] if n >= 6 else []      # 今天之前的 5 根完整日线
        if len(base) < 5 or sum(base) <= 0:
            return out
        avg5 = sum(base) / 5.0
        out["vol_ma5"] = round(avg5, 0)
        out["vol_ratio"] = round((vols[-1] / frac) / avg5, 3)
        out["vol_ratio_normalized"] = True
    else:
        if n < 5:
            return out
        avg5 = sum(vols[-5:]) / 5.0
        out["vol_ma5"] = round(avg5, 0)
        if avg5 > 0:
            out["vol_ratio"] = round(vols[-1] / avg5, 3)
            out["vol_ratio_normalized"] = False
    return out


# ═══════════════════════════════════════════════════════════════
# K线聚合摘要（2026-10-21）
# ═══════════════════════════════════════════════════════════════

def _kline_summary(bars: List[Dict[str, Any]], ma_windows=(5, 10, 20, 60, 120),
                   timeframe: str = "1D", last_bar_ts=None) -> Dict[str, Any]:
    """把原始 OHLCV 序列压成**聚合摘要**（均线 / 关键高低点 / 量能）。

    【为什么需要】`agent_get_kline` 返回的是逐日 OHLCV 明细。实测一只票 120 天
    = 4,212 字符，而模型最终输出只引用 4 个日期（最近 6–20 个交易日内的前高/前低/
    套牢区/顶底分型）—— 明细表里绝大多数行是**冗余**。更要紧的是：模型为了写
    "均���/量比/关键位"这类结论，得**自己从长表逐行算**，这既费 token 又容易算错
    （实测输出里出现过"剔除某日后合计 -2.40 亿（算式：-1.01-0.33-1.06+0.003）"
    这种把明细当账本翻的用法）。

    【给什么】只给模型做判断真正需要的聚合量，缺一档就少一档（不补零、不编造）：
      · ma5/ma10/ma20/ma60/ma120 —— 窗口不足该档则**不给该键**（不是给 0；
        与项目「缺失值 None 不回落 0」铁律一致）；
      · high_N / low_N + 对应日期 —— 近 N 日关键高低点（模型写支撑阻力的原料）；
      · recent_vol / vol_ma5 / vol_ratio —— 近 5 日均量与量比（缩量/放量判断；
        盘中自动做交易时间归一化，见 volume_ratio_summary，带 vol_ratio_normalized）；
      · total_days —— 实际返回天数（模型据此判断窗口够不够）。

    【不做什么】不预测、不给买卖建议、不替模型下结论——只是把"从长表算"变成
    "读现成值"。详细数值仍在明细表里，摘要**不替代**明细，两者是互补关系。
    """
    n = len(bars)
    out: Dict[str, Any] = {"total_days": n}
    if n == 0:
        return out

    closes = [float(b.get("c", 0) or 0) for b in bars]
    highs = [float(b.get("h", 0) or 0) for b in bars]
    lows = [float(b.get("l", 0) or 0) for b in bars]
    vols = [float(b.get("v", 0) or 0) for b in bars]

    # 均线：窗口不足则**不给该键**（缺就是缺，不回落 0）
    for w in ma_windows:
        if n >= w:
            out["ma%d" % w] = round(sum(closes[-w:]) / w, 3)

    # 近 N 日关键高低点（模型写支撑阻力的直接原料）
    for w in (20, 60):
        if n >= w:
            seg_h, seg_l = highs[-w:], lows[-w:]
            out["high_%dd" % w] = round(max(seg_h), 2)
            out["low_%dd" % w] = round(min(seg_l), 2)
            out["high_%dd_date" % w] = bars[-w + seg_h.index(max(seg_h))].get("t")
            out["low_%dd_date" % w] = bars[-w + seg_l.index(min(seg_l))].get("t")

    # 量能：近 5 日均量 + 量比（缩量/放量判断）—— 统一走 volume_ratio_summary：
    # 盘中末根是未走完的当日时按已交易时间归一化（2026-10-22），非日线不归一化。
    out.update(volume_ratio_summary(
        vols,
        last_bar_date=(bars[-1].get("t") if n else None),
        last_bar_ts=last_bar_ts,
        daily=(timeframe == "1D"),
    ))
    return out


# ═══════════════════════════════════════════════════════════════
# 数值工具
# ═══════════════════════════════════════════════════════════════

def _safe_round(v: float, n: int = 4) -> float:
    """安全四舍五入，处理 NaN/Inf。"""
    if v is None or math.isnan(v) or math.isinf(v):
        return 0.0
    return round(v, n)


# ═══════════════════════════════════════════════════════════════
# 量价关系四分支判定（2026-10-22 冗余收敛）
# ═══════════════════════════════════════════════════════════════
# 收敛前：analysis_tools 量能分析（盘中/非盘中双套）+ analysis_tools 批量快诊
# （简版）+ bull_bear_research（警惕版）各写一遍 if/elif，阈值口径相同
# （放量 >1.3× / 缩量 <0.7×）但标签文案三处不同。
# **分支逻辑在此唯一**；标签是模型可见输出，各调用方历史文案逐字保留
# （四套标签常量在下方，改输出文案需单独拍板，不在此偷偷统一）。

_VPR_LABELS = {          # 非盘中标准口径（量能分析 / bull_bear 默认同款）
    "up_strong": "量价齐升（健康上涨）",
    "up_weak": "缩量上涨（上涨乏力）",
    "down_strong": "放量下跌（恐慌抛售）",
    "down_weak": "缩量下跌（动能减弱）",
    "flat": "量价配合一般",
}
_VPR_LABELS_INTRADAY = {  # 盘中口径（量比实时，各档带（盘中）标注）
    "up_strong": "放量上涨（盘中）",
    "up_weak": "缩量上涨（盘中）",
    "down_strong": "放量下跌（盘中）",
    "down_weak": "缩量下跌（盘中）",
    "flat": "量价一般（盘中）",
}
_VPR_LABELS_BRIEF = {     # 简版（批量快诊，无后缀）
    "up_strong": "放量上涨",
    "up_weak": "缩量上涨",
    "down_strong": "放量下跌",
    "down_weak": "缩量下跌",
    "flat": "量价一般",
}
_VPR_LABELS_ALERT = {     # 警惕口径（bull_bear：缩量上涨带「需警惕」）
    "up_strong": "量价齐升（健康上涨）",
    "up_weak": "缩量上涨（上涨乏力，需警惕）",
    "down_strong": "放量下跌（恐慌抛售）",
    "down_weak": "缩量下跌（下跌动能减弱）",
    "flat": "量价配合一般",
}


def classify_vol_price(price_change: float, volume_ratio: float,
                       labels: Optional[Dict[str, str]] = None) -> str:
    """量价关系四分支判定（统一阈值口径：放量 >1.3× / 缩量 <0.7×）。

    labels 缺省 = 非盘中标准口径（_VPR_LABELS）；传入另一套标签常量
    保留各调用方历史文案（见上方四套常量的口径注释）。
    """
    L = labels or _VPR_LABELS
    if price_change > 0 and volume_ratio > 1.3:
        return L["up_strong"]
    if price_change > 0 and volume_ratio < 0.7:
        return L["up_weak"]
    if price_change < 0 and volume_ratio > 1.3:
        return L["down_strong"]
    if price_change < 0 and volume_ratio < 0.7:
        return L["down_weak"]
    return L["flat"]


# ═══════════════════════════════════════════════════════════════
# 指标计算
# ═══════════════════════════════════════════════════════════════

def _calc_obv(closes: List[float], volumes: List[float]) -> Dict[str, Any]:
    """OBV（能量潮）指标计算。"""
    if len(closes) < 3 or len(closes) != len(volumes):
        return {"error": "数据不足"}

    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    # OBV 趋势：近 5 日斜率
    window = min(5, len(obv))
    if window >= 2:
        obv_slope = (obv[-1] - obv[-window]) / (window - 1)
    else:
        obv_slope = 0

    # OBV 与价格背离检测
    divergence = "无"
    if len(closes) >= 10:
        price_up = closes[-1] > closes[-10]
        obv_up = obv[-1] > obv[-10]
        if price_up and not obv_up:
            divergence = "顶背离（价格新高但OBV未新高，量价背离看空）"
        elif not price_up and obv_up:
            divergence = "底背离（价格新低但OBV未新低，量价背离看多）"

    # OBV 均线
    obv_ma = sum(obv[-10:]) / min(10, len(obv))
    obv_vs_ma = "OBV在均线上方（多头量能）" if obv[-1] > obv_ma else "OBV在均线下方（空头量能）"

    signals = []
    if divergence != "无":
        signals.append(divergence)
    if obv_slope > 0 and closes[-1] > closes[-2]:
        signals.append("OBV上升+价格上涨，量价配合良好")
    elif obv_slope < 0 and closes[-1] < closes[-2]:
        signals.append("OBV下降+价格下跌，空头量能释放")
    elif obv_slope > 0 and closes[-1] < closes[-2]:
        signals.append("OBV上升但价格下跌，可能有资金吸筹")
    elif obv_slope < 0 and closes[-1] > closes[-2]:
        signals.append("OBV下降但价格上涨，上涨缺乏量能支撑")

    return {
        "obv": _safe_round(obv[-1], 0),
        "obv_prev": _safe_round(obv[-2], 0),
        "obv_slope": _safe_round(obv_slope, 0),
        "obv_trend": "上升" if obv_slope > 0 else "下降",
        "obv_vs_ma": obv_vs_ma,
        "divergence": divergence,
        "signals": signals,
    }


# ═══════════════════════════════════════════════════════════════
# 实时量比（盘中专用，不走 data_sources 层）
# ═══════════════════════════════════════════════════════════════

def _fetch_realtime_volume_ratio(codes: List[str], timeout: int = 5) -> Dict[str, float]:
    """批量获取实时量比，通过搜狐行情接口直接 HTTP 请求。

    仅盘中调用，返回 {code: volume_ratio}，失败返回空 dict。
    不走 DataSourceFactory，避免破坏架构分层。
    """
    import re
    import urllib.request

    # 构建 sohu biz_code
    biz_codes = []
    for c in codes:
        c = c.strip()
        if not c:
            continue
        digits = re.sub(r"\D", "", c)
        if digits:
            biz_codes.append(f"cn_{digits}")

    if not biz_codes:
        return {}

    code_str = ",".join(biz_codes)
    url = f"https://hqm.stock.sohu.com/getqjson?code={code_str}&cb=_vr_cb"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return {}

    # 解析 JSONP: _vr_cb({...})
    m = re.search(r"\{.*\}", text)
    if not m:
        return {}

    try:
        import json
        data = json.loads(m.group())
    except Exception:
        return {}

    result: Dict[str, float] = {}
    for biz_code, arr in data.items():
        if not isinstance(arr, list) or len(arr) < 10:
            continue
        digits = re.sub(r"\D", "", biz_code)
        try:
            vr = float(arr[9])
            if vr > 0:
                result[digits] = round(vr, 2)
        except (ValueError, TypeError, IndexError):
            continue

    return result
