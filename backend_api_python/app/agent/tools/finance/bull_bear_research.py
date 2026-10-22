# -*- coding: utf-8 -*-
"""多空研究员 — 同时构建多头和空头论据，综合判断方向。"""
from typing import Any, Dict, List

# 2026-09-28 修复：本模块的 4 个内部分析函数（_analyze_trend/_get_volume_analysis/
# _analyze_pattern/_get_indicator_snapshot）由 analysis_tools.py 内联而来，但内联时
# **漏带了被内联代码所依赖的辅助函数**（_safe_round / _fetch_ohlcv / _calc_macd /
# _calc_rsi / _calc_boll / _calc_kdj / logger / get_realtime_quote），导致这些函数
# 一旦被调用即 NameError（被内层 except 吞掉后只返回 {"error": ...}，静默退化）。
# 按项目"复用优于重定义"约定改为导入单一实现，不在此复制第三份。
from ...log import logger
from ._analysis_utils import _fetch_ohlcv, _safe_round, classify_vol_price, _VPR_LABELS_ALERT
from .analysis_tools import (
    _calc_boll, _calc_kdj, _calc_macd, _calc_rsi,
)
from .data_tools import get_realtime_quote
from .news_search_tools import _search_stock_intel, _search_policy_intel
# 与 technical_analysis 的分工细节（2026-10-22 自 docstring 移出，注入瘦身）：
# · 本工具已自行取数并计算：实时行情 + 120 日 K 线 + 五维技术快照
#   （score_model=bb5_weighted(...)）+ 个股情报；与 get_realtime_quote/
#   agent_get_kline/technical_analysis 同批调用时后三者的取数与技术快照部分
#   是重复劳动（费 token，不影响正确性）；仅当需要它们独有输出（如
#   technical_analysis 的六维加权分与 score_model=trend_composite(...) 复合
#   共振口径）时才值得同批调。
# · 多空论据完整保留在 output_data.bull_case/bear_case。
def bull_bear_research(codes: str, stock_name: str = "") -> dict:
    """多空研究：对单只股票做技术面+筹码+情报综合分析，返回多空评分和方向判断。
    已自行取数并计算（实时行情 + 120 日 K 线 + 五维技术快照 ma/macd/rsi/boll/kdj
    + 个股情报），与 get_realtime_quote/agent_get_kline/technical_analysis 同批调用
    属重复取数；仅当需要它们独有输出时才值得同批调。

    ⚠ score 是五维加权（score_model=bb5_weighted），与 technical_analysis 的六维加权
    （tech6_weighted）是两套不同评分模型，分歧是设计使然；引用时必须带 score_model
    身份，不得互相当作同一指标对比或对冲。

    Args:
        codes: 股票代码，如 "600066"
        stock_name: 股票名称，可选

    Returns:
        dict — 多空综合报告。核心键: score(0-100), direction(bullish|bearish|neutral),
        confidence(float), factors(list), analysis(str), hit_rate(float|None),
        missing_data(list[str]), falsifiable_conditions(str),
        output_data(bull_case/bear_case/verdict 嵌套)。
        factors 同 technical_analysis 格式: list[dict{"name","score","direction"}]，
        因子名 = 五维评分键 ma/macd/rsi/boll/kdj（与 _BB_DEFAULT_WEIGHTS 键同名，
        校准闭环靠此对齐）；失败 → {"error": str, "retriable": bool}。
    """
        
    # ── 获取数据 ──
    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(codes)),
        ("trend", lambda: _analyze_trend(codes)),
        ("volume", lambda: _get_volume_analysis(codes)),
        ("indicator", lambda: _get_indicator_snapshot(codes)),
        ("intel", lambda: _search_stock_intel(codes, stock_name or "")),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    # ── 构建多头论据 ──
    bull_factors = []
    bull_score = 50
    bull_signals = []

    trend = data.get("trend", {})
    if isinstance(trend, dict) and "error" not in trend:
        trend_score = trend.get("trend_score", 50)
        if trend_score >= 60:
            bull_factors.append(f"趋势偏多({trend_score})")
            bull_signals.append(f"趋势:{trend_score}")
        bull_score = trend_score

    indicator = data.get("indicator", {})
    # ⚠️ 生产者 `_get_indicator_snapshot`（本模块 :664）返回的是**嵌套**结构：
    #   {"codes", "latest_close", "ma5"…,
    #    "macd": {dif, dea, macd, bar_trend, position, signals},
    #    "rsi":  {rsi6, rsi6_zone, rsi12, …}, "boll": {…}, "kdj": {…}}
    # 早期在**顶层**平取 `rsi6` / `macd_hist` ⇒ 恒取到默认值 ⇒ RSI 超卖/超买与
    # MACD 多头/空头这四条论据**从未生成**，多空研究员实际只靠趋势 + 量价打分。
    # 另注：MACD 柱值的键名是 `macd`（见 `_calc_macd` 返回），不是 macd_hist。
    _ind = indicator if (isinstance(indicator, dict) and "error" not in indicator) else {}
    _rsi_d = _ind.get("rsi") if isinstance(_ind.get("rsi"), dict) else {}
    _macd_d = _ind.get("macd") if isinstance(_ind.get("macd"), dict) else {}
    _rsi6 = _rsi_d.get("rsi6", 50)
    _macd_hist = _macd_d.get("macd", 0)
    if isinstance(indicator, dict) and "error" not in indicator:
        rsi = _rsi6
        macd_hist = _macd_hist
        if rsi < 30:
            bull_factors.append(f"RSI{rsi:.0f}超卖")
            bull_signals.append(f"RSI超卖:{rsi:.0f}")
        if macd_hist > 0:
            bull_factors.append("MACD多头")

    volume = data.get("volume", {})
    if isinstance(volume, dict) and "error" not in volume:
        vol_relation = volume.get("vol_price_relation", "")
        if "量价齐升" in vol_relation:
            bull_factors.append("量价齐升")
            bull_signals.append("量价齐升")

    intel = data.get("intel", {})
    if isinstance(intel, dict) and "error" not in intel:
        composite = intel.get("composite_score", 0)
        if composite > 1:
            bull_factors.append(f"情报偏多({composite:.1f})")

    # ── 构建空头论据 ──
    bear_factors = []
    bear_score = 50
    bear_signals = []

    if isinstance(trend, dict) and "error" not in trend:
        trend_score = trend.get("trend_score", 50)
        if trend_score <= 40:
            bear_factors.append(f"趋势偏空({trend_score})")
            bear_signals.append(f"趋势:{trend_score}")
        bear_score = 100 - trend_score

    if isinstance(indicator, dict) and "error" not in indicator:
        # 见多头论据处的结构说明：从嵌套子结构取（rsi.rsi6 / macd.macd），
        # 不在顶层平取 —— 顶层没有这两个键，平取只会拿到默认值。
        rsi = _rsi6
        macd_hist = _macd_hist
        if rsi >= 70:
            bear_factors.append(f"RSI{rsi:.0f}超买")
            bear_signals.append(f"RSI超买:{rsi:.0f}")
        if macd_hist < 0:
            bear_factors.append("MACD空头")

    if isinstance(volume, dict) and "error" not in volume:
        vol_relation = volume.get("vol_price_relation", "")
        if "放量下跌" in vol_relation:
            bear_factors.append("放量下跌")
            bear_signals.append("放量下跌")

    if isinstance(intel, dict) and "error" not in intel:
        composite = intel.get("composite_score", 0)
        if composite < -1:
            bear_factors.append(f"情报偏空({composite:.1f})")

    # ── 综合判断 ──
    # 多头得分 vs 空头得分
    bull_total = bull_score + len(bull_factors) * 5
    bear_total = bear_score + len(bear_factors) * 5

    if bull_total > bear_total + 10:
        verdict = "bullish"
        final_score = min(100, 50 + (bull_total - bear_total))
        direction = "bullish"
    elif bear_total > bull_total + 10:
        verdict = "bearish"
        final_score = max(0, 50 - (bear_total - bull_total))
        direction = "bearish"
    else:
        verdict = "neutral"
        final_score = 50
        direction = "neutral"

    confidence = min(1.0, max(0.3, (len(bull_factors) + len(bear_factors)) / 8))

    # ── A1c 校准命中率（score→P(方向正确)）──
    # 有校准数据时 confidence 直接取 hit_rate（P(方向正确)），无则回退原因子数逻辑。
    try:
        from ...utils.calibration import apply as _cal_apply
        hit_rate = _cal_apply("bull_bear_research", final_score)
    except Exception:
        hit_rate = None
    if hit_rate is not None:
        confidence = hit_rate

    signal_parts = []
    if bull_signals:
        signal_parts.append(f"多:{','.join(bull_signals[:2])}")
    if bear_signals:
        signal_parts.append(f"空:{','.join(bear_signals[:2])}")
    signal = " | ".join(signal_parts) if signal_parts else "多空均衡"

    # ── 校准因子（与 `_BB_DEFAULT_WEIGHTS` 键同名，闭合 factor 闭环）──
    # 早期这里塞的是"多头:xxx"/"空头:xxx"**论据**名（f-string 动态生成），与上方
    # 加权用的五维评分键 ma/macd/rsi/boll/kdj **两套不同的东西** ⇒ evaluator 从
    # traces 取名写 `layer='factor'` 后 `get_factor_weights` 按同名读回永远 miss
    # （见 AGENT_DESIGN_v3.md D-1 键名层）。现统一为五维评分键：攒够样本即命中校准值。
    # 多空论据仍完整保留在 output_data.bull_case/bear_case（不丢信息）。
    # ★ 用 append（非列表字面量）写，因子名才是字面量常量 —— 守卫
    #   `test_v9_factor_weight_keys` 靠 `factors.append({"name": <字面量>})` 抽取键名。
    _t = trend if (isinstance(trend, dict) and "error" not in trend) else {}
    factors = []
    factors.append({"name": "ma", "score": int(_t.get("ma_score", 50)), "direction": direction})
    factors.append({"name": "macd", "score": int(_t.get("macd_score", 50)), "direction": direction})
    factors.append({"name": "rsi", "score": int(_t.get("rsi_score", 50)), "direction": direction})
    factors.append({"name": "boll", "score": int(_t.get("boll_score", 50)), "direction": direction})
    factors.append({"name": "kdj", "score": int(_t.get("kdj_score", 50)), "direction": direction})

    dir_map = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    all_signals = (bull_signals or []) + (bear_signals or [])
    md = f"多空分析 {final_score:.0f}分 {dir_map.get(direction, direction)}"
    if hit_rate is not None:
        md += f" 历史命中率{hit_rate*100:.0f}%"
    if factors:
        md += "\n" + " ".join(f"{f['name']}:{f['score']}" for f in factors)
    if all_signals:
        md += "\n" + " ".join(all_signals[:3])
    md += f"\n{verdict}"
    analysis = md

    # ── A3 缺数据声明 ──
    # data 各数据源若含 "error" 键则视为缺失，统一收集到 missing_data。
    missing_data = [k for k, v in data.items()
                    if isinstance(v, dict) and "error" in v]

    # ── A4 可证伪条件（若 X 发生则本判断失效）──
    _trend = data.get("trend", {}) if isinstance(data.get("trend"), dict) else {}
    _ma5 = _trend.get("ma5")
    _ma20 = _trend.get("ma20")
    _falsifiable = []
    if direction == "bullish":
        if _ma5 and _ma20:
            _falsifiable.append(f"MA5({_ma5:.2f})跌破MA20({_ma20:.2f})")
        _falsifiable.append(f"多空分({final_score:.0f})跌破55")
    elif direction == "bearish":
        if _ma5 and _ma20:
            _falsifiable.append(f"MA5({_ma5:.2f})突破MA20({_ma20:.2f})")
        _falsifiable.append(f"多空分({final_score:.0f})突破45")
    else:
        _falsifiable.append(f"多空分({final_score:.0f})突破60或跌破40")
    falsifiable_conditions = "；".join(_falsifiable) if _falsifiable else "数据不足"

    _r = {
        "score": final_score,
        "direction": direction,
        "confidence": confidence,
        "signal": signal,
        "factors": factors,
        "analysis": analysis,
        "status": "ok",
        # A1a：权重是否来自 qd_agent_weights 校准（透传 _analyze_trend 的 calibrated）
        "calibrated": data.get("trend", {}).get("calibrated", False) if isinstance(data.get("trend"), dict) else False,
        # A1c：score→P(方向正确)，无校准数据时为 None
        "hit_rate": hit_rate,
        # A3：缺失的数据源名列表
        "missing_data": missing_data,
        # A4：可证伪条件
        "falsifiable_conditions": falsifiable_conditions,
        "output_data": {
            "bull_case": {
                "score": bull_score,
                "factors": bull_factors,
                "signals": bull_signals,
            },
            "bear_case": {
                "score": bear_score,
                "factors": bear_factors,
                "signals": bear_signals,
            },
            "verdict": verdict,
            "data": data,
        },
    }
    return _r
# ── 内联自 analysis_tools.py ──

def _analyze_trend(codes: str) -> Dict[str, Any]:
    """技术趋势（内部复用）：五维加权模型（ma/macd/rsi/boll/kdj × _BB_DEFAULT_WEIGHTS），
    仅供 bull_bear_research 内部调用，外部技术面分析请用 technical_analysis。

    ⚠️ 与 analysis_tools._analyze_trend 的关系（2026-10-17 澄清）：
    这不是"过期副本"，而是**独立评分模型** —— 五维加权、校准命名空间
    `bull_bear_research`（因子名 ma/macd/rsi/boll/kdj 与 _BB_DEFAULT_WEIGHTS
    同名，A1a 闭环靠此对齐，见 test_v9_factor_weight_keys_20261014.py）。
    analysis_tools 那份是复合共振模型（共振过滤+背离+量能），**同股同数据
    给出不同分是设计使然**（各自服务不同消费方）。历史上两个模型输出同名
    键 `trend_score` 却无身份标识，导致下游报告把 52 vs 17 的矛盾误诊为
    "因子对冲"——现以 `score_model` 键自证身份修复。禁止为"统一"把本函数
    改成转调 analysis_tools：那会删掉 bull_bear 的校准模型（降智）。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(codes: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(codes, 120)
            closes = data["close"]
            if len(closes) < 5:
                return {"error": "K线数据不足（至少需要5根）", "retriable": True}

            latest = closes[-1]
            prev = closes[-2] if len(closes) >= 2 else latest

            # ── 均线 ──
            ma5 = _safe_round(sum(closes[-5:]) / 5)
            ma10 = _safe_round(sum(closes[-10:]) / 10) if len(closes) >= 10 else ma5
            ma20 = _safe_round(sum(closes[-20:]) / 20) if len(closes) >= 20 else ma10
            ma60 = _safe_round(sum(closes[-60:]) / 60) if len(closes) >= 60 else _safe_round(sum(closes) / len(closes))
            ma120 = _safe_round(sum(closes[-120:]) / 120) if len(closes) >= 120 else ma60

            # 均线排列评分（0-100）
            ma_score = 50
            if ma5 > ma10 > ma20 > ma60:
                ma_desc = "强多头排列"
                ma_score = 90
            elif ma5 > ma10 > ma20:
                ma_desc = "多头排列"
                ma_score = 75
            elif ma5 > ma20:
                ma_desc = "弱势多头"
                ma_score = 60
            elif ma5 < ma10 < ma20 < ma60:
                ma_desc = "强空头排列"
                ma_score = 10
            elif ma5 < ma10 < ma20:
                ma_desc = "空头排列"
                ma_score = 25
            elif ma5 < ma20:
                ma_desc = "弱势空头"
                ma_score = 40
            else:
                ma_desc = "均线缠绕/震荡"
                ma_score = 50

            # 乖离率
            bias_ma5 = _safe_round((latest - ma5) / ma5 * 100, 2) if ma5 else 0
            bias_ma20 = _safe_round((latest - ma20) / ma20 * 100, 2) if ma20 else 0

            # ── MACD ──
            macd_result = _calc_macd(closes)
            macd_score = 50
            if isinstance(macd_result, dict) and "error" not in macd_result:
                if macd_result.get("dif", 0) > macd_result.get("dea", 0):
                    macd_score = 70 if macd_result.get("macd", 0) > 0 else 60
                else:
                    macd_score = 30 if macd_result.get("macd", 0) < 0 else 40
                if "金叉" in str(macd_result.get("signals", [])):
                    macd_score += 15
                elif "死叉" in str(macd_result.get("signals", [])):
                    macd_score -= 15

            # ── RSI ──
            rsi_result = _calc_rsi(closes, [6, 12, 24])
            rsi_score = 50
            if isinstance(rsi_result, dict) and "error" not in rsi_result:
                rsi6 = rsi_result.get("rsi6", 50)
                if rsi6 >= 70:
                    rsi_score = 25
                elif rsi6 <= 30:
                    rsi_score = 75
                else:
                    rsi_score = int(rsi6)

            # ── 布林带 ──
            boll_result = _calc_boll(closes)
            boll_score = 50
            if isinstance(boll_result, dict) and "error" not in boll_result:
                pos = boll_result.get("position_pct", 50)
                if pos >= 80:
                    boll_score = 30
                elif pos <= 20:
                    boll_score = 70
                else:
                    boll_score = int(50 + (50 - pos) * 0.4)

            # ── KDJ ──
            kdj_result = _calc_kdj(data["high"], data["low"], closes)
            kdj_score = 50
            if isinstance(kdj_result, dict) and "error" not in kdj_result:
                j = kdj_result.get("j", 50)
                if j >= 100:
                    kdj_score = 25
                elif j <= 0:
                    kdj_score = 75
                elif "金叉" in str(kdj_result.get("signals", [])):
                    kdj_score = 70
                elif "死叉" in str(kdj_result.get("signals", [])):
                    kdj_score = 30

            # ── 综合评分 ──
            # A1a 因子权重接线（2026-09-26）：五维权重优先从 qd_agent_weights 取校准值，
            # 无数据回退启发式常量 + calibrated=false（缺口显式 > 硬凑数字，项目红线）。
            #
            # ★ 2026-10-14 复核（审计 A-05 / 设计 D-1）：这里**恒回退有二层原因**，
            #   别再笼统写成"get_factor_weights 恒空"——那样会误导人去改 getter：
            #   ① 样本层：`qd_agent_weights WHERE layer='factor'` **实测 0 行**
            #      （`tmp/probe_factor_weights_20261014.py`）⇒ 闭环样本还没攒够，
            #      这层会随时间自愈，getter 本身实现是对的（不是恒空的实现问题）。
            #   ② 键名层：**已于 2026-10-14 对齐** —— 外层 `factors` 的因子名现在就是
            #      ma/macd/rsi/boll/kdj（与下方 `_BB_DEFAULT_WEIGHTS` 键同名），与
            #      `technical_analysis` 一样自洽 ⇒ 样本攒够即命中校准值，不再有"两套
            #      东西"的错位（旧实现把"多头:xxx"/"空头:xxx"论据当因子名，与五维评分键
            #      永不相交）。多空论据仍完整保留在 output_data.bull_case/bear_case。
            #   ⚠ 对照：`technical_analysis` 同样自洽（趋势/指标/量价/形态/筹码/流通盘）。
            _BB_DEFAULT_WEIGHTS = {"ma": 0.30, "macd": 0.25, "rsi": 0.20,
                                   "boll": 0.15, "kdj": 0.10}
            try:
                from ...chain.store import get_factor_weights
                _bb_calibrated = get_factor_weights("bull_bear_research")
            except Exception:
                _bb_calibrated = {}
            if _bb_calibrated:
                _bb_w = _bb_calibrated
                _bb_calibrated_flag = True
            else:
                _bb_w = _BB_DEFAULT_WEIGHTS
                _bb_calibrated_flag = False

            total_score = int(
                ma_score * _bb_w.get("ma", 0.30) +
                macd_score * _bb_w.get("macd", 0.25) +
                rsi_score * _bb_w.get("rsi", 0.20) +
                boll_score * _bb_w.get("boll", 0.15) +
                kdj_score * _bb_w.get("kdj", 0.10)
            )
            total_score = max(0, min(100, total_score))

            if total_score >= 75:
                overall = "看多"
                buy_signal = "多指标共振看多"
                signal_score = total_score
            elif total_score >= 60:
                overall = "偏多"
                buy_signal = "偏多但需确认"
                signal_score = total_score
            elif total_score <= 25:
                overall = "看空"
                buy_signal = "多指标共振看空，建议回避"
                signal_score = total_score
            elif total_score <= 40:
                overall = "偏空"
                buy_signal = "偏空，观望为主"
                signal_score = total_score
            else:
                overall = "震荡"
                buy_signal = "多空分歧，观望"
                signal_score = total_score

            all_signals = []
            if isinstance(macd_result, dict):
                all_signals.extend(macd_result.get("signals", []))
            if isinstance(rsi_result, dict):
                all_signals.extend(rsi_result.get("signals", []))
            if isinstance(boll_result, dict):
                all_signals.extend(boll_result.get("signals", []))
            if isinstance(kdj_result, dict):
                all_signals.extend(kdj_result.get("signals", []))

            change_pct = _safe_round((latest - prev) / prev * 100, 2) if prev else 0

            return {
                "codes": codes,
                "latest_close": _safe_round(latest),
                "change_pct": change_pct,
                "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120,
                "ma_alignment": ma_desc,
                "bias_ma5": bias_ma5, "bias_ma20": bias_ma20,
                "macd": macd_result,
                "rsi": rsi_result,
                "boll": boll_result,
                "kdj": kdj_result,
                "trend_score": total_score,
                "trend": overall,
                "buy_signal": buy_signal,
                "signal_score": signal_score,
                # 评分模型自证（2026-10-17）：五维加权独立模型，与 analysis_tools
                # 的复合共振模型（score_model=trend_composite(...)）区分口径。
                "score_model": "bb5_weighted(ma/macd/rsi/boll/kdj)",
                "all_signals": all_signals,
                "data_points": len(closes),
                # A1a：权重是否来自 qd_agent_weights 校准
                "calibrated": _bb_calibrated_flag,
                # 2026-10-14 对齐（D-1 键名层）：把五维评分标量透传给外层，作为
                # 校准因子名（与 `_BB_DEFAULT_WEIGHTS` 键同名），让 factor 闭环能命中。
                "ma_score": ma_score, "macd_score": macd_score,
                "rsi_score": rsi_score, "boll_score": boll_score,
                "kdj_score": kdj_score,
            }
        except Exception as e:
            logger.error("_analyze_trend(%s) failed: %s", codes, e)
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

def _get_volume_analysis(codes: str) -> Dict[str, Any]:
    """量能分析：量比、换手率、成交量趋势。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(codes: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(codes, 30)
            volumes = data["volume"]
            closes = data["close"]
            if len(volumes) < 5:
                return {"error": "K线数据不足"}

            latest_vol = volumes[-1]
            avg_vol_5 = sum(volumes[-5:]) / 5
            avg_vol_20 = sum(volumes) / len(volumes)

            # 量比统一走 volume_ratio_summary（2026-10-22）：盘中末根未走完，
            # 裸除 5 日全天均量会系统性低估（实测 0.227 vs 归一化后约 1.27）。
            from ._analysis_utils import volume_ratio_summary
            _vs = volume_ratio_summary(volumes,
                                      last_bar_date=data.get("last_date"),
                                      last_bar_ts=data.get("last_ts"))
            volume_ratio = _safe_round(_vs["vol_ratio"], 2) if _vs.get("vol_ratio") is not None else None

            if volume_ratio is None:
                status = "量比未知"
                meaning = "完整日线不足5根，量比不可算"
            elif volume_ratio > 2.0:
                status = "显著放量"
                meaning = "放量，需结合价格方向判断"
            elif volume_ratio > 1.5:
                status = "温和放量"
                meaning = "成交活跃度提升"
            elif volume_ratio < 0.5:
                status = "明显缩量"
                meaning = "缩量，抛压减轻或交投清淡"
            elif volume_ratio < 0.8:
                status = "温和缩量"
                meaning = "成交活跃度下降"
            else:
                status = "平量"
                meaning = "成交量维持常态"

            vol_trend = "数据不足"
            if len(volumes) >= 6:
                recent_avg = sum(volumes[-3:]) / 3
                earlier_avg = sum(volumes[-6:-3]) / 3
                vol_trend = "上升" if recent_avg > earlier_avg * 1.1 else ("下降" if recent_avg < earlier_avg * 0.9 else "平稳")

            vol_price_relation = "数据不足"
            if len(closes) >= 2 and latest_vol > 0 and volume_ratio is not None:
                price_change = closes[-1] - closes[-2]
                # 分支判定统一走 _analysis_utils.classify_vol_price（阈值口径唯一）；
                # 本工具用警惕口径标签（缩量上涨带「需警惕」），逐字保留历史文案
                vol_price_relation = classify_vol_price(
                    price_change, volume_ratio, labels=_VPR_LABELS_ALERT)

            return {
                "codes": codes,
                "latest_volume": _safe_round(latest_vol, 0),
                "avg_volume_5d": _safe_round(avg_vol_5, 0),
                "avg_volume_20d": _safe_round(avg_vol_20, 0),
                "volume_ratio": volume_ratio,
                "volume_status": status,
                "volume_meaning": meaning,
                "volume_trend": vol_trend,
                "vol_price_relation": vol_price_relation,
            }
        except Exception as e:
            logger.error("_get_volume_analysis(%s) failed: %s", codes, e)
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

def _analyze_pattern(codes: str) -> Dict[str, Any]:
    """识别K线形态（增强版）：锤子线、十字星、吞没、早晨/晚星、三连阳/阴、长上影/下影、缺口等。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(codes: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(codes, 10)
            if len(data["close"]) < 3:
                return {"error": "K线数据不足"}

            patterns = []
            o, h, l, c = data["open"], data["high"], data["low"], data["close"]
            n = len(c)

            lo, lh, ll, lc = o[-1], h[-1], l[-1], c[-1]
            body = abs(lc - lo)
            candle_range = lh - ll if lh > ll else 0.001
            upper_shadow = lh - max(lo, lc)
            lower_shadow = min(lo, lc) - ll

            if body > 0 and candle_range > 0:
                if lower_shadow >= 2 * body and upper_shadow <= body * 0.3:
                    patterns.append("锤子线（底部反转信号）")
                elif upper_shadow >= 2 * body and lower_shadow <= body * 0.3:
                    patterns.append("倒锤子线（顶部反转信号）")

            if body <= candle_range * 0.1:
                if upper_shadow > candle_range * 0.3 and lower_shadow > candle_range * 0.3:
                    patterns.append("长腿十字星（强烈犹豫信号）")
                elif upper_shadow > candle_range * 0.4 and lower_shadow < candle_range * 0.1:
                    patterns.append("墓碑线（顶部反转）")
                elif lower_shadow > candle_range * 0.4 and upper_shadow < candle_range * 0.1:
                    patterns.append("蜻蜓线（底部反转）")
                else:
                    patterns.append("十字星（犹豫信号）")

            if upper_shadow > body * 2 and upper_shadow > lower_shadow * 2:
                patterns.append("长上影线（上方压力大）")

            if lower_shadow > body * 2 and lower_shadow > upper_shadow * 2:
                patterns.append("长下影线（下方支撑强）")

            if body > candle_range * 0.85:
                if lc > lo:
                    patterns.append("光头光脚大阳线（强势）")
                else:
                    patterns.append("光头光脚大阴线（弱势）")

            if n >= 2:
                po, pc = o[-2], c[-2]
                prev_body = abs(pc - po)
                if prev_body > 0 and body > prev_body:
                    if pc < po and lc > lo and lo <= pc and lc >= po:
                        patterns.append("看涨吞没（底部反转）")
                    elif pc > po and lc < lo and lo >= pc and lc <= po:
                        patterns.append("看跌吞没（顶部反转）")

            if n >= 3:
                o1, c1 = o[-3], c[-3]
                o2, c2 = o[-2], c[-2]
                o3, c3 = o[-1], c[-1]
                body1 = abs(c1 - o1)
                body2 = abs(c2 - o2)
                body3 = abs(c3 - o3)

                if (c1 < o1 and body1 > 0 and
                        body2 < body1 * 0.3 and
                        c3 > o3 and body3 > 0 and
                        c3 > (o1 + c1) / 2):
                    patterns.append("早晨之星（底部反转，看多）")

                if (c1 > o1 and body1 > 0 and
                        body2 < body1 * 0.3 and
                        c3 < o3 and body3 > 0 and
                        c3 < (o1 + c1) / 2):
                    patterns.append("黄昏之星（顶部反转，看空）")

                if c1 > o1 and c2 > o2 and c3 > o3:
                    if body3 > body2 > body1:
                        patterns.append("三连阳递增（强势看多）")
                    else:
                        patterns.append("三连阳（多头排列）")

                if c1 < o1 and c2 < o2 and c3 < o3:
                    if body3 > body2 > body1:
                        patterns.append("三连阴递增（强势看空）")
                    else:
                        patterns.append("三连阴（空头排列）")

                if (c1 > o1 and c2 > o2 and c3 > o3 and
                        c2 > c1 and c3 > c2 and
                        o2 > o1 and o3 > o2):
                    patterns.append("红三兵（强烈看多）")

                if (c1 < o1 and c2 < o2 and c3 < o3 and
                        c2 < c1 and c3 < c2 and
                        o2 < o1 and o3 < o2):
                    patterns.append("黑三鸦（强烈看空）")

            if n >= 2:
                prev_high = h[-2]
                prev_low = l[-2]
                if l[-1] > prev_high:
                    gap_size = _safe_round((l[-1] - prev_high) / prev_high * 100, 2)
                    patterns.append(f"向上跳空缺口（幅度{gap_size}%）")
                elif h[-1] < prev_low:
                    gap_size = _safe_round((prev_low - h[-1]) / prev_low * 100, 2)
                    patterns.append(f"向下跳空缺口（幅度{gap_size}%）")

            if not patterns:
                patterns.append("无明显特殊形态")

            return {
                "codes": codes,
                "latest_candle": {"open": _safe_round(lo), "high": _safe_round(lh),
                                  "low": _safe_round(ll), "close": _safe_round(lc)},
                "body_size": _safe_round(body),
                "upper_shadow": _safe_round(upper_shadow),
                "lower_shadow": _safe_round(lower_shadow),
                "patterns": patterns,
                "pattern_count": len([p for p in patterns if "无明显" not in p]),
            }
        except Exception as e:
            logger.error("_analyze_pattern(%s) failed: %s", codes, e)
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

def _get_chip_distribution(codes: str, lookback_days: int = 120) -> Dict[str, Any]:
    """筹码分布分析 — 委托给 chip_distribution.get_chip_distribution。"""
    from .chip_distribution import get_chip_distribution
    return get_chip_distribution(codes, lookback_days=lookback_days)

def _get_indicator_snapshot(codes: str) -> Dict[str, Any]:
    """单次获取多个技术指标快照（MACD、RSI、BOLL、KDJ等）。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(codes: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(codes, 120)
            closes = data["close"]
            if len(closes) < 10:
                return {"error": "K线数据不足（至少需要10根）", "retriable": True}

            result = {"codes": codes, "latest_close": _safe_round(closes[-1])}

            for p in [5, 10, 20, 60, 120]:
                if len(closes) >= p:
                    result[f"ma{p}"] = _safe_round(sum(closes[-p:]) / p)

            macd = _calc_macd(closes)
            if "error" not in macd:
                result["macd"] = macd

            rsi = _calc_rsi(closes, [6, 12, 24])
            if "error" not in rsi:
                result["rsi"] = rsi

            boll = _calc_boll(closes)
            if "error" not in boll:
                result["boll"] = boll

            kdj = _calc_kdj(data["high"], data["low"], closes)
            if "error" not in kdj:
                result["kdj"] = kdj

            if data["volume"]:
                # 量比统一走 volume_ratio_summary（2026-10-22，口径同上）
                from ._analysis_utils import volume_ratio_summary
                _vs = volume_ratio_summary(data["volume"],
                                          last_bar_date=data.get("last_date"),
                                          last_bar_ts=data.get("last_ts"))
                if _vs.get("vol_ratio") is not None:
                    result["volume_ratio"] = _safe_round(_vs["vol_ratio"], 2)

            return result
        except Exception as e:
            logger.error("_get_indicator_snapshot(%s) failed: %s", codes, e)
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
