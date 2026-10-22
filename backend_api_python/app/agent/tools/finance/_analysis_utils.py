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


def _fetch_ohlcv(stock_code: str, days: int = 120) -> Dict[str, List[float]]:
    """获取 OHLCV 五组数据序列。"""
    klines = _fetch_klines(stock_code, days)
    o, h, l, c, v = [], [], [], [], []
    for k in klines:
        o.append(float(k.get("open", 0)))
        h.append(float(k.get("high", 0)))
        l.append(float(k.get("low", 0)))
        c.append(float(k.get("close", 0)))
        v.append(float(k.get("volume", 0)))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


# ═══════════════════════════════════════════════════════════════
# 数值工具
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# K线聚合摘要（2026-10-21）
# ═══════════════════════════════════════════════════════════════

def _kline_summary(bars: List[Dict[str, Any]], ma_windows=(5, 10, 20, 60, 120)) -> Dict[str, Any]:
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
      · recent_vol / vol_ma5 / vol_ratio —— 近 5 日均量与量比（缩量/放量判断）；
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

    # 量能：近 5 日均量 + 量比（缩量/放量判断；量比 = 近1日 / 5日均量）
    if n >= 5:
        vol_ma5 = sum(vols[-5:]) / 5.0
        out["vol_ma5"] = round(vol_ma5, 0)
        if n >= 2 and vol_ma5 > 0:
            out["vol_ratio"] = round(vols[-1] / vol_ma5, 3)
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
