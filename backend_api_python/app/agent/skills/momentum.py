# -*- coding: utf-8 -*-
"""
Momentum Tracker skill — A股动量追踪师。

负责：趋势强度评估、动量信号识别、突破/回调判断、短线择时。
A股短线赚钱靠动量，不是靠价值发现。动量分析是中短线交易的核心。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill


@skill(
    name="momentum_tracker",
    description="A股动量追踪师。负责趋势强度评估、动量信号识别、突破/回调判断、短线择时。A股短线赚钱靠动量。当用户问动量、趋势强度、突破、择时、买入时机时调用。",
    instructions=(
        "你是A股动量追踪师。A股短线赚钱靠动量，不是靠价值发现。\n\n"
        "分析框架：\n"
        "1. **趋势强度评估** — 用 analyze_trend + calculate_ma 判断：\n"
        "   - 均线多头排列程度（5>10>20>60 → 强趋势）\n"
        "   - 股价与各均线的距离（偏离度越大，短期回调概率越高）\n"
        "   - 趋势持续天数（已走 N 天上涨趋势 → 还能走多久？）\n"
        "2. **动量信号** — 用 get_indicator_snapshot 获取指标：\n"
        "   - MACD 金叉 + 柱状图放大 = 动量增强\n"
        "   - RSI > 70 = 超买（但A股强势股可维持超买）\n"
        "   - RSI < 30 = 超卖（但A股弱势股可维持超卖）\n"
        "   - KDJ 金叉 + J 值拐头 = 短线买入信号\n"
        "3. **量价配合** — 用 get_volume_analysis：\n"
        "   - 放量上涨 = 趋势健康\n"
        "   - 缩量上涨 = 动量衰减，警惕回调\n"
        "   - 放量下跌 = 趋势转弱\n"
        "   - 缩量下跌 = 洗盘概率大\n"
        "4. **突破判断** — 关键位置突破的有效性：\n"
        "   - 前高突破 + 放量 = 有效突破\n"
        "   - 均线突破 + 缩量 = 假突破概率高\n"
        "   - 整数关口突破（心理价位）\n"
        "5. **短线择时** — 综合判断买入/卖出时机：\n"
        "   - 回调到支撑位 + 缩量 + 指标金叉 = 买入时机\n"
        "   - 上涨到压力位 + 放量滞涨 = 卖出时机\n\n"
        "输出格式：\n"
        "- 动量评级：极强/强/中性/弱/极弱\n"
        "- 趋势阶段及持续性评估\n"
        "- 关键支撑位和压力位\n"
        "- 买入/卖出时机建议\n"
        "- 建议持有周期（N 个交易日）\n\n"
        "必须调用工具获取真实数据，绝不编造。"
        "\n\n## 输出格式（必须遵守）\n"
        "你的 final_answer 必须包含以下JSON结构（嵌在正文中即可）：\n"
        "\n"
        "```json\n"
        "{\n"
        "  \"direction\": \"bullish/bearish/neutral\",\n"
        "  \"confidence\": 0.0-1.0,\n"
        "  \"score\": 0-100,\n"
        "  \"signal\": \"一句话信号摘要\",\n"
        "  \"factors\": [\n"
        "    {\"name\": \"因子名\", \"value\": \"值\", \"score\": 0-100, \"status\": \"ok\"}\n"
        "  ]\n"
        "}\n"
        "```\n"
        "\n"
        "规则：\n"
        "- score: 0=极度看空, 50=中性, 100=极度看多。基于数据客观打分。\n"
        "- confidence: 数据充分程度（0=完全没数据, 1=数据非常充分）。不是方向确定性。\n"
        "- direction: 基于score判断。score>=60=bullish, score<=40=bearish, 其余=neutral。\n"
        "- status: ok=有数据, missing=数据缺失。缺失的因子必须标missing，不能编造。\n"
        "- signal: 一句话总结关键信号。\n"
        "- factors: 每个分析维度一行。包含你调用工具获取的所有关键数据点。",
    ),
    tools=[
        "analyze_trend", "calculate_ma", "get_volume_analysis",
        "get_indicator_snapshot", "analyze_pattern",
        "get_realtime_quote", "agent_get_kline",
        "generate_kline_chart",
    ],
    priority=9,
    default_weight=1.1,
)
class MomentumTrackerSkill:
    """A股动量追踪师子 Agent。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
    ) -> Optional[SkillReport]:
        """纯算法动量分析。

        核心逻辑：
          1. 趋势强度（均线排列 + 偏离度）
          2. 动量指标（MACD柱状图 + RSI + KDJ）
          3. 量价配合（量价关系确认动量）
          4. 突破判断（前高/均线突破有效性）
        """
        factors = []
        signals = []

        # ── 1. 趋势强度（权重 35%）──
        trend = tool_results.get("analyze_trend", {})
        trend_score = 50
        if isinstance(trend, dict) and "error" not in trend:
            trend_score = trend.get("trend_score", 50)
            ma_align = trend.get("ma_alignment", "")
            bias_ma20 = trend.get("bias_ma20", 0)

            # 偏离度修正：偏离过大 → 短期回调风险
            if bias_ma20 > 10:
                signals.append(f"偏离MA20达{bias_ma20:.1f}%，回调风险")
                trend_score = max(trend_score - 10, 0)
            elif bias_ma20 < -10:
                signals.append(f"偏离MA20达{bias_ma20:.1f}%，超跌反弹")

            if ma_align:
                signals.append(ma_align)

            factors.append(FactorItem(
                name="趋势强度",
                value=ma_align or trend.get("trend", "震荡"),
                score=trend_score,
            ))
        else:
            factors.append(FactorItem(name="趋势强度", value="数据缺失", score=50))

        # ── 2. 动量指标（权重 30%）──
        indicator = tool_results.get("get_indicator_snapshot", {})
        ind_score = 50
        if isinstance(indicator, dict) and "error" not in indicator:
            rsi_val = indicator.get("rsi6", 50)
            macd_hist = indicator.get("macd_hist", 0)
            kdj_j = indicator.get("kdj_j", 50)

            # RSI 动量
            if rsi_val >= 70:
                rsi_momentum = 30  # 超买，动量衰减
                signals.append(f"RSI{rsi_val:.0f}超买")
            elif rsi_val <= 30:
                rsi_momentum = 70  # 超卖，可能反弹
                signals.append(f"RSI{rsi_val:.0f}超卖")
            else:
                rsi_momentum = int(rsi_val)

            # MACD 动量（柱状图方向和大小）
            if macd_hist > 0:
                macd_momentum = 70 if macd_hist > 0.5 else 60
            elif macd_hist < 0:
                macd_momentum = 30 if macd_hist < -0.5 else 40
            else:
                macd_momentum = 50

            # KDJ 动量
            if kdj_j >= 80:
                kdj_momentum = 30  # 超买
            elif kdj_j <= 20:
                kdj_momentum = 70  # 超卖
            else:
                kdj_momentum = int(kdj_j)

            ind_score = int(rsi_momentum * 0.35 + macd_momentum * 0.40 + kdj_momentum * 0.25)

            # 金叉/死叉信号
            if macd_hist > 0 and rsi_val < 60:
                signals.append("MACD+RSI共振偏多")
            elif macd_hist < 0 and rsi_val > 40:
                signals.append("MACD+RSI共振偏空")

            factors.append(FactorItem(
                name="动量指标",
                value=f"RSI{rsi_val:.0f}",
                score=ind_score,
            ))
        else:
            factors.append(FactorItem(name="动量指标", value="数据缺失", score=50))

        # ── 3. 量价配合（权重 20%）──
        volume = tool_results.get("get_volume_analysis", {})
        vol_score = 50
        if isinstance(volume, dict) and "error" not in volume:
            vol_relation = volume.get("vol_price_relation", "")
            volume_ratio = volume.get("volume_ratio", 1.0)

            if "量价齐升" in vol_relation:
                vol_score = 80  # 动量健康
            elif "缩量上涨" in vol_relation:
                vol_score = 45  # 动量衰减
            elif "放量下跌" in vol_relation:
                vol_score = 20  # 动量反转
            elif "缩量下跌" in vol_relation:
                vol_score = 55  # 洗盘可能
            elif "放量滞涨" in vol_relation:
                vol_score = 25  # 顶部特征
            else:
                vol_score = 50

            if volume_ratio > 2.0:
                signals.append(f"量比{volume_ratio}")

            factors.append(FactorItem(
                name="量价配合",
                value=vol_relation or volume.get("status", "平量"),
                score=vol_score,
            ))
        else:
            factors.append(FactorItem(name="量价配合", value="数据缺失", score=50))

        # ── 4. 突破判断（权重 15%）──
        pattern = tool_results.get("analyze_pattern", {})
        pat_score = 50
        if isinstance(pattern, dict) and "error" not in pattern:
            patterns = pattern.get("patterns", [])
            if patterns:
                p_str = str(patterns[0])
                if "突破" in p_str or "大阳" in p_str:
                    pat_score = 75
                    signals.append(p_str.split("（")[0])
                elif "跌破" in p_str or "大阴" in p_str:
                    pat_score = 25
                    signals.append(p_str.split("（")[0])
                elif "十字星" in p_str:
                    pat_score = 50
                    signals.append("十字星犹豫")
            factors.append(FactorItem(
                name="突破形态",
                value=patterns[0].split("（")[0] if patterns else "无突破",
                score=pat_score,
            ))
        else:
            factors.append(FactorItem(name="突破形态", value="数据缺失", score=50))

        # ── 综合评分 ──
        final_score = int(
            trend_score * 0.35 +
            ind_score * 0.30 +
            vol_score * 0.20 +
            pat_score * 0.15
        )
        final_score = max(0, min(100, final_score))

        if final_score >= 60:
            direction = "bullish"
        elif final_score <= 40:
            direction = "bearish"
        else:
            direction = "neutral"

        valid_count = sum(1 for f in factors if "缺失" not in str(f.value))
        confidence = round(min(valid_count / 4, 1.0), 2)

        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无明显动量信号"

        # 动量评级
        if final_score >= 80:
            momentum_rating = "极强"
        elif final_score >= 65:
            momentum_rating = "强"
        elif final_score >= 45:
            momentum_rating = "中性"
        elif final_score >= 30:
            momentum_rating = "弱"
        else:
            momentum_rating = "极弱"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"动量评级:{momentum_rating} 综合评分:{final_score}/100。"
                     f"趋势:{trend_score} 动量:{ind_score} 量价:{vol_score} 形态:{pat_score}",
            status="ok",
        )
