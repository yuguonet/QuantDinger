# -*- coding: utf-8 -*-
"""
Chart pattern recognition — 经典图表形态 + 缠论 + OBV 量价分析。

基于枢轴点（局部极值）检测，分析多K线构成的结构性形态。
纯 Python 计算，无外部依赖。
"""
from __future__ import annotations

from app.agent.log import logger
from typing import Any, Dict, List, Optional, Tuple

from app.agent.tools._analysis_utils import (
    _fetch_ohlcv,
    _safe_round,
    _calc_obv,
)

# ═══════════════════════════════════════════════════════════════
# 枢轴点与线性拟合
# ═══════════════════════════════════════════════════════════════

def _find_pivots(highs: List[float], lows: List[float],
                 left: int = 5, right: int = 5) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """寻找局部极值点（枢轴点）。

    返回 (peaks, troughs)，每个元素为 (index, price)。
    peak: highs[i] 是 [i-left, i+right] 范围内的最高点
    trough: lows[i] 是 [i-left, i+right] 范围内的最低点
    """
    n = len(highs)
    peaks, troughs = [], []
    for i in range(left, n - right):
        is_peak = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if 0 <= j < n and highs[j] > highs[i]:
                is_peak = False
                break
        if is_peak:
            peaks.append((i, highs[i]))

        is_trough = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if 0 <= j < n and lows[j] < lows[i]:
                is_trough = False
                break
        if is_trough:
            troughs.append((i, lows[i]))

    return peaks, troughs
def _fit_line(points: List[Tuple[int, float]]) -> Tuple[float, float]:
    """对一组 (index, price) 做最小二乘线性拟合，返回 (slope, intercept)。"""
    if len(points) < 2:
        return 0.0, points[0][1] if points else 0.0
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] ** 2 for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept
def _line_value(slope: float, intercept: float, x: int) -> float:
    return slope * x + intercept
def _price_near(price: float, target: float, tolerance_pct: float = 3.0) -> bool:
    """判断 price 是否在 target 的 tolerance_pct% 范围内。"""
    if target == 0:
        return False
    return abs(price - target) / abs(target) * 100 <= tolerance_pct
# ═══════════════════════════════════════════════════════════════
# 缠论基础：顶底分型 + 笔
# ═══════════════════════════════════════════════════════════════

def _detect_chan_fractals(highs: List[float], lows: List[float],
                          closes: List[float]) -> Optional[Dict[str, Any]]:
    """缠论基础分析：顶底分型识别 + 笔的划分。

    分型规则（严格缠论定义）：
    - 顶分型：中间K线的高点是三根中最高，且中间K线低点也是三根中最高
    - 底分型：中间K线的低点是三根中最低，且中间K线高点也是三根中最低
    - 笔：连接相邻顶分型和底分型，至少包含 5 根 K 线（含分型共用的 K 线）
    """
    n = len(closes)
    if n < 7:
        return None

    # 1. 识别所有分型
    fractals = []  # (index, type, price)  type: 'top' / 'bottom'
    for i in range(1, n - 1):
        if (highs[i] > highs[i - 1] and highs[i] > highs[i + 1] and
                lows[i] > lows[i - 1] and lows[i] > lows[i + 1]):
            fractals.append((i, "top", highs[i]))
        elif (lows[i] < lows[i - 1] and lows[i] < lows[i + 1] and
              highs[i] < highs[i - 1] and highs[i] < highs[i + 1]):
            fractals.append((i, "bottom", lows[i]))

    if len(fractals) < 2:
        return None

    # 2. 过滤：顶底交替，同类型保留更极端的
    filtered = [fractals[0]]
    for f in fractals[1:]:
        if f[1] == filtered[-1][1]:
            if f[1] == "top" and f[2] > filtered[-1][2]:
                filtered[-1] = f
            elif f[1] == "bottom" and f[2] < filtered[-1][2]:
                filtered[-1] = f
        else:
            if f[0] - filtered[-1][0] >= 4:
                filtered.append(f)
            else:
                if f[1] == "top" and f[2] > filtered[-1][2]:
                    filtered[-1] = f
                elif f[1] == "bottom" and f[2] < filtered[-1][2]:
                    filtered[-1] = f

    if len(filtered) < 2:
        return None

    # 3. 构建笔
    strokes = []
    for i in range(len(filtered) - 1):
        start = filtered[i]
        end = filtered[i + 1]
        direction = "上" if end[1] == "top" else "下"
        strokes.append({
            "from": {"index": start[0], "type": start[1], "price": _safe_round(start[2])},
            "to": {"index": end[0], "type": end[1], "price": _safe_round(end[2])},
            "direction": direction,
            "length": abs(end[2] - start[2]),
            "bars": end[0] - start[0],
        })

    # 4. 分析当前笔的状态
    patterns = []
    signals = []

    if len(strokes) >= 2:
        last_stroke = strokes[-1]
        prev_stroke = strokes[-2]

        if last_stroke["direction"] == "上" and prev_stroke["direction"] == "上":
            if last_stroke["to"]["price"] > prev_stroke["to"]["price"]:
                if last_stroke["length"] < prev_stroke["length"] * 0.8:
                    patterns.append("缠论顶背驰（新高但笔力度减弱，可能见顶）")
                    signals.append("缠论顶背驰：上涨力度衰减")
        elif last_stroke["direction"] == "下" and prev_stroke["direction"] == "下":
            if last_stroke["to"]["price"] < prev_stroke["to"]["price"]:
                if last_stroke["length"] < prev_stroke["length"] * 0.8:
                    patterns.append("缠论底背驰（新低但笔力度减弱，可能见底）")
                    signals.append("缠论底背驰：下跌力度衰减")

        last_fractal = filtered[-1]
        if last_fractal[1] == "top":
            patterns.append(f"缠论最近顶分型（位置{_safe_round(last_fractal[2])}）")
        else:
            patterns.append(f"缠论最近底分型（位置{_safe_round(last_fractal[2])}）")

    stroke_dirs = [s["direction"] for s in strokes[-5:]] if len(strokes) >= 3 else []
    if stroke_dirs:
        if all(d == "上" for d in stroke_dirs[-2:]):
            signals.append("笔序列：连续上涨笔，多头趋势")
        elif all(d == "下" for d in stroke_dirs[-2:]):
            signals.append("笔序列：连续下跌笔，空头趋势")

    return {
        "patterns": patterns,
        "signals": signals,
        "fractal_count": len(filtered),
        "stroke_count": len(strokes),
        "recent_strokes": strokes[-3:] if len(strokes) >= 3 else strokes,
    }
# ═══════════════════════════════════════════════════════════════
# Tool 函数
# ═══════════════════════════════════════════════════════════════

def analyze_chart_patterns(codes: str) -> Dict[str, Any]:
    """图表形态识别：头肩顶/底、双顶/双底、三角形、旗形、楔形、矩形、杯柄等经典形态。

    基于枢轴点（局部极值）检测，分析多K线构成的结构性形态。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes, highs, lows = data["close"], data["high"], data["low"]
            if len(closes) < 30:
                return {"error": "K线数据不足（至少需要30根）", "retriable": True}

            latest = closes[-1]
            patterns = []
            signals = []

            peaks_l, troughs_l = _find_pivots(highs, lows, left=8, right=8)
            peaks_s, troughs_s = _find_pivots(highs, lows, left=3, right=3)

            # ── 头肩顶 ──
            if len(peaks_l) >= 3:
                for i in range(len(peaks_l) - 2):
                    p1, p2, p3 = peaks_l[i], peaks_l[i + 1], peaks_l[i + 2]
                    h1, h2, h3 = p1[1], p2[1], p3[1]
                    if h2 > h1 and h2 > h3:
                        shoulder_diff = abs(h1 - h3) / max(h1, h3) * 100
                        if shoulder_diff < 10:
                            troughs_between = [t for t in troughs_l if p1[0] < t[0] < p3[0]]
                            if len(troughs_between) >= 2:
                                neckline_pts = [(troughs_between[0][0], troughs_between[0][1]),
                                                (troughs_between[-1][0], troughs_between[-1][1])]
                                slope, intercept = _fit_line(neckline_pts)
                                neckline_at_end = _line_value(slope, intercept, len(closes) - 1)
                                if latest < neckline_at_end:
                                    patterns.append("头肩顶（已跌破颈线，强烈看空）")
                                    target_drop = h2 - neckline_at_end
                                    signals.append(f"头肩顶目标位: {_safe_round(neckline_at_end - target_drop)}")
                                else:
                                    patterns.append("头肩顶雏形（尚未跌破颈线）")
                                break

            # ── 头肩底 ──
            if len(troughs_l) >= 3:
                for i in range(len(troughs_l) - 2):
                    t1, t2, t3 = troughs_l[i], troughs_l[i + 1], troughs_l[i + 2]
                    l1, l2, l3 = t1[1], t2[1], t3[1]
                    if l2 < l1 and l2 < l3:
                        shoulder_diff = abs(l1 - l3) / max(abs(l1), abs(l3)) * 100
                        if shoulder_diff < 10:
                            peaks_between = [p for p in peaks_l if t1[0] < p[0] < t3[0]]
                            if len(peaks_between) >= 2:
                                neckline_pts = [(peaks_between[0][0], peaks_between[0][1]),
                                                (peaks_between[-1][0], peaks_between[-1][1])]
                                slope, intercept = _fit_line(neckline_pts)
                                neckline_at_end = _line_value(slope, intercept, len(closes) - 1)
                                if latest > neckline_at_end:
                                    patterns.append("头肩底（已突破颈线，强烈看多）")
                                    target_rise = neckline_at_end - l2
                                    signals.append(f"头肩底目标位: {_safe_round(neckline_at_end + target_rise)}")
                                else:
                                    patterns.append("头肩底雏形（尚未突破颈线）")
                                break

            # ── 双顶 / 双底 ──
            if len(peaks_l) >= 2:
                for i in range(len(peaks_l) - 1):
                    p1, p2 = peaks_l[i], peaks_l[i + 1]
                    if _price_near(p1[1], p2[1], 5):
                        troughs_between = [t for t in troughs_l if p1[0] < t[0] < p2[0]]
                        if troughs_between:
                            valley = min(t[1] for t in troughs_between)
                            depth = (p1[1] + p2[1]) / 2 - valley
                            if latest < valley:
                                patterns.append("双顶/M顶（已跌破颈线，看空）")
                                signals.append(f"双顶目标位: {_safe_round(valley - depth)}")
                            else:
                                patterns.append("双顶/M顶雏形（颈线未破）")
                            break

            if len(troughs_l) >= 2:
                for i in range(len(troughs_l) - 1):
                    t1, t2 = troughs_l[i], troughs_l[i + 1]
                    if _price_near(t1[1], t2[1], 5):
                        peaks_between = [p for p in peaks_l if t1[0] < p[0] < t2[0]]
                        if peaks_between:
                            peak_val = max(p[1] for p in peaks_between)
                            depth = peak_val - (t1[1] + t2[1]) / 2
                            if latest > peak_val:
                                patterns.append("双底/W底（已突破颈线，看多）")
                                signals.append(f"双底目标位: {_safe_round(peak_val + depth)}")
                            else:
                                patterns.append("双底/W底雏形（颈线未破）")
                            break

            # ── 三角形整理 ──
            if len(peaks_s) >= 2 and len(troughs_s) >= 2:
                rp = peaks_s[-4:] if len(peaks_s) >= 4 else peaks_s[-2:]
                rt = troughs_s[-4:] if len(troughs_s) >= 4 else troughs_s[-2:]
                if len(rp) >= 2 and len(rt) >= 2:
                    p_slope, _ = _fit_line(rp)
                    t_slope, _ = _fit_line(rt)
                    lp_y = _line_value(p_slope, _fit_line(rp)[1], rp[-1][0])
                    lt_y = _line_value(t_slope, _fit_line(rt)[1], rt[-1][0])
                    spread = abs(lp_y - lt_y)
                    avg_p = (lp_y + lt_y) / 2
                    spread_pct = spread / avg_p * 100 if avg_p else 0
                    if spread_pct < 8:
                        if abs(p_slope) < abs(t_slope) * 0.3 and t_slope > 0:
                            patterns.append("上升三角形（水平阻力+上升支撑，偏多突破）")
                        elif abs(t_slope) < abs(p_slope) * 0.3 and p_slope < 0:
                            patterns.append("下降三角形（水平支撑+下降阻力，偏空突破）")
                        elif p_slope < 0 and t_slope > 0:
                            patterns.append("对称三角形（收敛整理，等待方向选择）")

            # ── 旗形 / 三角旗 ──
            if len(closes) >= 30:
                for start_i in range(max(0, len(closes) - 40), len(closes) - 10):
                    pole_end = start_i + 10
                    if pole_end >= len(closes):
                        break
                    pole_change = (closes[pole_end] - closes[start_i]) / closes[start_i] * 100
                    if abs(pole_change) > 15:
                        flag_h = highs[pole_end:]
                        flag_l = lows[pole_end:]
                        if len(flag_h) >= 5:
                            f_peaks = _find_pivots(flag_h, flag_l, 2, 2)[0]
                            f_troughs = _find_pivots(flag_h, flag_l, 2, 2)[1]
                            if len(f_peaks) >= 1 and len(f_troughs) >= 1:
                                fp_s, _ = _fit_line(f_peaks) if len(f_peaks) >= 2 else (0, f_peaks[0][1])
                                ft_s, _ = _fit_line(f_troughs) if len(f_troughs) >= 2 else (0, f_troughs[0][1])
                                d = "上涨" if pole_change > 0 else "下跌"
                                if pole_change > 0 and fp_s < 0 and ft_s < 0:
                                    if abs(fp_s - ft_s) < abs(fp_s) * 0.5:
                                        patterns.append(f"上升旗形（{d}旗杆+下行旗面，中继看多）")
                                    else:
                                        patterns.append(f"上升三角旗（{d}旗杆+收敛旗面，中继看多）")
                                    break
                                elif pole_change < 0 and fp_s > 0 and ft_s > 0:
                                    if abs(fp_s - ft_s) < abs(fp_s) * 0.5:
                                        patterns.append(f"下降旗形（{d}旗杆+上行旗面，中继看空）")
                                    else:
                                        patterns.append(f"下降三角旗（{d}旗杆+收敛旗面，中继看空）")
                                    break

            # ── 楔形 ──
            if len(peaks_s) >= 2 and len(troughs_s) >= 2:
                rp = peaks_s[-3:]
                rt = troughs_s[-3:]
                if len(rp) >= 2 and len(rt) >= 2:
                    p_slope, _ = _fit_line(rp)
                    t_slope, _ = _fit_line(rt)
                    if p_slope > 0 and t_slope > 0 and t_slope > p_slope:
                        patterns.append("上升楔形（看空，支撑上升快于阻力，终将破位）")
                    elif p_slope < 0 and t_slope < 0 and p_slope < t_slope:
                        patterns.append("下降楔形（看多，阻力下降快于支撑，终将突破）")

            # ── 矩形整理 ──
            if len(peaks_s) >= 2 and len(troughs_s) >= 2:
                rp = peaks_s[-3:]
                rt = troughs_s[-3:]
                if len(rp) >= 2 and len(rt) >= 2:
                    p_slope, _ = _fit_line(rp)
                    t_slope, _ = _fit_line(rt)
                    avg = (sum(p[1] for p in rp) + sum(t[1] for t in rt)) / (len(rp) + len(rt))
                    if abs(p_slope) / avg * 100 < 0.5 and abs(t_slope) / avg * 100 < 0.5:
                        resistance = sum(p[1] for p in rp) / len(rp)
                        support = sum(t[1] for t in rt) / len(rt)
                        box_h = (resistance - support) / support * 100 if support else 0
                        if 3 < box_h < 20:
                            if latest >= resistance * 0.98:
                                patterns.append(f"矩形整理（接近上沿阻力{_safe_round(resistance)}，关注突破）")
                            elif latest <= support * 1.02:
                                patterns.append(f"矩形整理（接近下沿支撑{_safe_round(support)}，关注破位）")
                            else:
                                patterns.append(f"矩形整理（箱体震荡，区间{_safe_round(support)}-{_safe_round(resistance)}）")

            # ── 杯柄形态 ──
            if len(troughs_l) >= 1 and len(closes) >= 40:
                deepest = min(troughs_l, key=lambda t: t[1])
                cup_idx = deepest[0]
                if 15 <= cup_idx <= len(closes) - 15:
                    left_peak = max(highs[:cup_idx]) if cup_idx > 0 else 0
                    right_area = highs[cup_idx:]
                    if len(right_area) >= 10:
                        right_peak = max(right_area[:len(right_area) // 2]) if len(right_area) >= 10 else 0
                        if left_peak > 0 and right_peak > 0 and _price_near(left_peak, right_peak, 8):
                            cup_depth = (left_peak + right_peak) / 2 - deepest[1]
                            cup_depth_pct = cup_depth / ((left_peak + right_peak) / 2) * 100
                            if 12 < cup_depth_pct < 40:
                                handle_area = closes[cup_idx + len(right_area) // 2:]
                                if len(handle_area) >= 3:
                                    handle_drop = (max(handle_area) - min(handle_area)) / max(handle_area) * 100
                                    if handle_drop < cup_depth_pct * 0.5:
                                        patterns.append("杯柄形态（底部反转看多，突破杯口即确认）")
                                        signals.append(f"杯口阻力: {_safe_round((left_peak + right_peak) / 2)}")

            # ── 缠论分型 ──
            chan_result = _detect_chan_fractals(highs, lows, closes)
            if chan_result:
                patterns.extend(chan_result.get("patterns", []))
                signals.extend(chan_result.get("signals", []))

            if not patterns:
                patterns.append("无明显图表形态")

            bullish_kw = ["看多", "突破", "底部反转", "W底", "头肩底", "下降楔形", "杯柄"]
            bearish_kw = ["看空", "破位", "顶部反转", "M顶", "头肩顶", "上升楔形"]
            bull_c = sum(1 for p in patterns for k in bullish_kw if k in p)
            bear_c = sum(1 for p in patterns for k in bearish_kw if k in p)
            pattern_score = max(0, min(100, 50 + (bull_c - bear_c) * 15))

            return {
                "stock_code": stock_code,
                "latest_close": _safe_round(latest),
                "patterns": patterns,
                "pattern_count": len([p for p in patterns if "无明显" not in p]),
                "pattern_score": pattern_score,
                "direction": "看多" if pattern_score >= 60 else ("看空" if pattern_score <= 40 else "中性"),
                "signals": signals,
            }
        except Exception as e:
            logger.error("analyze_chart_patterns(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
def get_obv_analysis(codes: str) -> Dict[str, Any]:
    """OBV（能量潮）量价分析：返回 OBV 值、趋势、与价格的背离检测。

    OBV 是累计成交量指标，价格上涨日加上成交量，价格下跌日减去成交量。
    用于验证趋势是否得到量能支撑。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes = data["close"]
            volumes = data["volume"]
            if len(closes) < 10:
                return {"error": "K线数据不足（至少需要10根）", "retriable": True}

            obv_result = _calc_obv(closes, volumes)
            if "error" in obv_result:
                return obv_result

            vol = volumes[-1]
            avg_vol = sum(volumes[-5:]) / 5
            vol_ratio = _safe_round(vol / avg_vol, 2) if avg_vol else 0
            price_change_pct = _safe_round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if closes[-2] else 0

            if price_change_pct > 0 and obv_result["obv_trend"] == "上升":
                vol_price_assessment = "量价齐升，多头量能充足"
            elif price_change_pct < 0 and obv_result["obv_trend"] == "下降":
                vol_price_assessment = "量价齐跌，空头量能释放"
            elif price_change_pct > 0 and obv_result["obv_trend"] == "下降":
                vol_price_assessment = "价涨量缩，上涨缺乏量能支撑，注意回调"
            elif price_change_pct < 0 and obv_result["obv_trend"] == "上升":
                vol_price_assessment = "价跌量增，可能有资金低位吸筹"
            else:
                vol_price_assessment = "量价关系中性"

            return {
                "stock_code": stock_code,
                "latest_close": _safe_round(closes[-1]),
                **obv_result,
                "volume_ratio": vol_ratio,
                "price_change_pct": price_change_pct,
                "vol_price_assessment": vol_price_assessment,
            }
        except Exception as e:
            logger.error("get_obv_analysis(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
