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
from typing import Any, Dict, List


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

def _safe_round(v: float, n: int = 4) -> float:
    """安全四舍五入，处理 NaN/Inf。"""
    if v is None or math.isnan(v) or math.isinf(v):
        return 0.0
    return round(v, n)


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
