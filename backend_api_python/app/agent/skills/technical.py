# -*- coding: utf-8 -*-
"""
Technical Skill — 技术分析专家（A股中短线特化）。

趋势阶段判断、量价配合分析、均线系统、技术指标、形态识别。
A股短线定价逻辑下，趋势和量价比基本面更重要。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill


@skill(
    name="technical_agent",
    description="技术面综合分析（趋势/量价/均线/指标/形态/筹码）",
    tools=[
        "analyze_trend", "calculate_ma", "get_volume_analysis",
        "analyze_pattern", "get_chip_distribution",
        "get_indicator_snapshot", "generate_kline_chart",
    ],
    priority=9,
    default_weight=1.2,
    instructions=(
        "你是A股技术分析专家，专注中短线（1-20个交易日）分析。\n\n"
        "分析流程：\n"
        "1. 趋势阶段判断 — 当前处于哪个阶段（底部吸筹/主升浪/顶部派发/下跌趋势）\n"
        "2. 量价配合度 — 放量突破/缩量回调/高位放量不涨/低位放量不跌\n"
        "3. 均线系统 — 5/10/20/60日均线排列\n"
        "4. 指标验证 — MACD/RSI/BOLL/KDJ 至少2个相互验证\n"
        "5. K线形态 — 突破/反转/整理形态\n\n"
        "A股特别注意：涨停板是极强信号，连板高度代表市场情绪强度，"
        "换手率>15%要警惕，量比>3说明有异动。\n\n"
        "必须调用工具获取真实数据，绝不编造。\n\n"
        "## ⚠️ 职责边界\n"
        "你是分析层，只描述事实，不给操作建议。\n"
        "❌ 禁止说：建议买入/卖出/持有/观望/建仓/减仓\n"
        "✅ 应该说：当前趋势为XX、指标显示XX、均线排列为XX\n\n"
        "## 输出格式（必须遵守）\n"
        "只输出JSON，不要输出分析文字。数据都在JSON里体现。\n\n"
        "```json\n"
        "{\n"
        '  "direction": "bullish/bearish/neutral",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "score": 0-100,\n'
        '  "signal": "一句话事实描述，如：空头排列,RSI29超卖,触及布林下轨",\n'
        '  "factors": [\n'
        '    {"name": "趋势", "value": "下跌趋势|主升浪|底部吸筹|顶部派发", "score": 0-100},\n'
        '    {"name": "量价", "value": "缩量下跌|放量突破|...", "score": 0-100},\n'
        '    {"name": "均线", "value": "空头排列|多头排列|...", "score": 0-100},\n'
        '    {"name": "指标", "value": "RSI29超卖|MACD死叉|...", "score": 0-100},\n'
        '    {"name": "形态", "value": "破位|支撑|...", "score": 0-100}\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "规则：\n"
        "- score: 0=极度看空, 50=中性, 100=极度看多（方向强度，不是操作信号）\n"
        "- direction: score>=60=bullish, score<=40=bearish, 其余=neutral\n"
        "- signal: 用逗号分隔的关键事实，不超过30字\n"
        "- factors.value: 用最简短的词组描述状态，不要写句子"
    ),
)
class TechnicalSkill:
    """技术分析专家。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
    ) -> Optional[SkillReport]:
        """纯算法技术面分析。

        核心逻辑：
          1. analyze_trend 的 trend_score 作为主评分（已有加权算法）
          2. 量价分析作为确认信号
          3. 形态识别作为附加因子
          4. 筹码分布作为支撑/阻力参考

        不需要 LLM，全部是规则引擎。
        """
        factors = []
        signals = []

        # ── 1. 趋势评分（主权重 50%）──
        trend = tool_results.get("analyze_trend", {})
        if isinstance(trend, dict) and "error" not in trend:
            trend_score = trend.get("trend_score", 50)
            trend_desc = trend.get("trend", "震荡")
            ma_align = trend.get("ma_alignment", "")
            factors.append(FactorItem(
                name="趋势",
                value=trend_desc,
                score=trend_score,
            ))
            if ma_align:
                signals.append(ma_align)
        else:
            trend_score = 50
            factors.append(FactorItem(name="趋势", value="数据缺失", score=50))

        # ── 2. 量价分析（权重 20%）──
        volume = tool_results.get("get_volume_analysis", {})
        vol_score = 50
        if isinstance(volume, dict) and "error" not in volume:
            vol_relation = volume.get("vol_price_relation", "")
            volume_ratio = volume.get("volume_ratio", 1.0)

            # 量价关系评分
            if "量价齐升" in vol_relation:
                vol_score = 75
            elif "缩量上涨" in vol_relation:
                vol_score = 60
            elif "放量下跌" in vol_relation:
                vol_score = 20
            elif "缩量下跌" in vol_relation:
                vol_score = 40
            elif "放量滞涨" in vol_relation:
                vol_score = 25
            else:
                vol_score = 50

            # 量比修正
            if volume_ratio > 3.0:
                signals.append(f"量比{volume_ratio}异动")
            elif volume_ratio > 2.0:
                signals.append(f"量比{volume_ratio}放量")

            factors.append(FactorItem(
                name="量价",
                value=vol_relation or volume.get("status", "平量"),
                score=vol_score,
            ))
        else:
            factors.append(FactorItem(name="量价", value="数据缺失", score=50))

        # ── 3. 指标验证（权重 20%）──
        indicator = tool_results.get("get_indicator_snapshot", {})
        ind_score = 50
        if isinstance(indicator, dict) and "error" not in indicator:
            # 从 indicator_snapshot 提取关键指标
            rsi_val = indicator.get("rsi6", 50)
            macd_hist = indicator.get("macd_hist", 0)
            kdj_j = indicator.get("kdj_j", 50)

            # RSI 评分
            rsi_score = 50
            if rsi_val >= 80:
                rsi_score = 20  # 严重超买
                signals.append(f"RSI{rsi_val:.0f}超买")
            elif rsi_val >= 70:
                rsi_score = 30  # 超买
                signals.append(f"RSI{rsi_val:.0f}偏高")
            elif rsi_val <= 20:
                rsi_score = 80  # 严重超卖
                signals.append(f"RSI{rsi_val:.0f}超卖")
            elif rsi_val <= 30:
                rsi_score = 70  # 超卖
                signals.append(f"RSI{rsi_val:.0f}偏低")

            # MACD 评分
            macd_score = 50
            if macd_hist > 0:
                macd_score = 65
            elif macd_hist < 0:
                macd_score = 35

            # KDJ 评分
            kdj_score = 50
            if kdj_j >= 100:
                kdj_score = 25
            elif kdj_j <= 0:
                kdj_score = 75

            ind_score = int(rsi_score * 0.4 + macd_score * 0.35 + kdj_score * 0.25)
            factors.append(FactorItem(
                name="指标",
                value=f"RSI{rsi_val:.0f}",
                score=ind_score,
            ))
        else:
            factors.append(FactorItem(name="指标", value="数据缺失", score=50))

        # ── 4. 形态识别（权重 10%）──
        pattern = tool_results.get("analyze_pattern", {})
        pat_score = 50
        if isinstance(pattern, dict) and "error" not in pattern:
            patterns = pattern.get("patterns", [])
            if patterns:
                # 根据形态类型评分
                bullish_patterns = ["锤子线", "吞没", "早晨之星", "三连阳", "长下影线", "蜻蜓线"]
                bearish_patterns = ["倒锤子", "墓碑线", "长上影线", "大阴线", "晚星", "三连阴"]

                for p in patterns:
                    p_str = str(p)
                    if any(bp in p_str for bp in bullish_patterns):
                        pat_score = max(pat_score, 70)
                        signals.append(p_str.split("（")[0])
                    elif any(bp in p_str for bp in bearish_patterns):
                        pat_score = min(pat_score, 30)
                        signals.append(p_str.split("（")[0])

                factors.append(FactorItem(
                    name="形态",
                    value=patterns[0].split("（")[0] if patterns else "无明显形态",
                    score=pat_score,
                ))
            else:
                factors.append(FactorItem(name="形态", value="无明显形态", score=50))
        else:
            factors.append(FactorItem(name="形态", value="数据缺失", score=50))

        # ── 5. 筹码分布（附加参考，不参与主评分）──
        chip = tool_results.get("get_chip_distribution", {})
        if isinstance(chip, dict) and "error" not in chip:
            concentration = chip.get("concentration", "")
            if concentration:
                signals.append(f"筹码{concentration}")

        # ── 综合评分 ──
        # 权重：趋势50% + 量价20% + 指标20% + 形态10%
        final_score = int(
            trend_score * 0.50 +
            vol_score * 0.20 +
            ind_score * 0.20 +
            pat_score * 0.10
        )
        final_score = max(0, min(100, final_score))

        # 方向
        if final_score >= 60:
            direction = "bullish"
        elif final_score <= 40:
            direction = "bearish"
        else:
            direction = "neutral"

        # 置信度：基于数据完整度
        valid_count = sum(1 for f in factors if "缺失" not in str(f.value))
        confidence = round(min(valid_count / 4, 1.0), 2)

        # 信号摘要
        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无明显信号"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"技术面综合评分 {final_score}/100，方向 {direction}。"
                     f"趋势:{trend_score} 量价:{vol_score} 指标:{ind_score} 形态:{pat_score}",
            status="ok",
        )
