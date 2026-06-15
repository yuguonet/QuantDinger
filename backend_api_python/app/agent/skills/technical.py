# -*- coding: utf-8 -*-
"""
Technical Skill — 技术分析专家（A股中短线特化）。

合并原 technical_agent + momentum_tracker：
  趋势阶段判断、量价配合分析、均线系统、技术指标、形态识别、
  动量信号识别、突破/回调判断、短线择时。
A股短线定价逻辑下，趋势、量价和动量比基本面更重要。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill


@skill("technical_agent", auto_load=True)
class TechnicalSkill:
    """技术分析专家（含动量追踪）。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法技术面 + 动量分析。

        合并原 technical_agent 和 momentum_tracker 的算法逻辑：
          1. 趋势评分（主权重 40%）— 均线排列 + 偏离度
          2. 动量指标（权重 25%）— MACD柱状图 + RSI + KDJ
          3. 量价分析（权重 20%）— 量价关系确认趋势
          4. 形态识别（权重 10%）— 突破/反转形态
          5. 筹码分布（附加参考）— 支撑/阻力位
        """
        factors = []
        signals = []

        # ── 1. 趋势评分（主权重 40%）──
        trend = tool_results.get("analyze_trend", {})
        trend_score = 50
        if isinstance(trend, dict) and "error" not in trend:
            trend_score = trend.get("trend_score", 50)
            trend_desc = trend.get("trend", "震荡")
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
                name="趋势",
                value=trend_desc,
                score=trend_score,
            ))
        else:
            factors.append(FactorItem(name="趋势", value="数据缺失", score=50))

        # ── 2. 动量指标（权重 25%）──
        indicator = tool_results.get("get_indicator_snapshot", {})
        ind_score = 50
        if isinstance(indicator, dict) and "error" not in indicator:
            rsi_val = indicator.get("rsi6", 50)
            macd_hist = indicator.get("macd_hist", 0)
            kdj_j = indicator.get("kdj_j", 50)

            # RSI 评分
            rsi_score = 50
            if rsi_val >= 80:
                rsi_score = 20
                signals.append(f"RSI{rsi_val:.0f}超买")
            elif rsi_val >= 70:
                rsi_score = 30
                signals.append(f"RSI{rsi_val:.0f}偏高")
            elif rsi_val <= 20:
                rsi_score = 80
                signals.append(f"RSI{rsi_val:.0f}超卖")
            elif rsi_val <= 30:
                rsi_score = 70
                signals.append(f"RSI{rsi_val:.0f}偏低")
            else:
                rsi_score = int(rsi_val)

            # MACD 评分（柱状图方向和大小）
            macd_score = 50
            if macd_hist > 0:
                macd_score = 70 if macd_hist > 0.5 else 60
            elif macd_hist < 0:
                macd_score = 30 if macd_hist < -0.5 else 40

            # KDJ 评分
            kdj_score = 50
            if kdj_j >= 80:
                kdj_score = 25
            elif kdj_j <= 20:
                kdj_score = 75

            ind_score = int(rsi_score * 0.35 + macd_score * 0.40 + kdj_score * 0.25)

            # 金叉/死叉共振信号
            if macd_hist > 0 and rsi_val < 60:
                signals.append("MACD+RSI共振偏多")
            elif macd_hist < 0 and rsi_val > 40:
                signals.append("MACD+RSI共振偏空")

            factors.append(FactorItem(
                name="指标",
                value=f"RSI{rsi_val:.0f}",
                score=ind_score,
            ))
        else:
            factors.append(FactorItem(name="指标", value="数据缺失", score=50))

        # ── 3. 量价分析（权重 20%）──
        volume = tool_results.get("get_volume_analysis", {})
        vol_score = 50
        if isinstance(volume, dict) and "error" not in volume:
            vol_relation = volume.get("vol_price_relation", "")
            volume_ratio = volume.get("volume_ratio", 1.0)

            if "量价齐升" in vol_relation:
                vol_score = 80
            elif "缩量上涨" in vol_relation:
                vol_score = 45
            elif "放量下跌" in vol_relation:
                vol_score = 20
            elif "缩量下跌" in vol_relation:
                vol_score = 55
            elif "放量滞涨" in vol_relation:
                vol_score = 25
            else:
                vol_score = 50

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

        # ── 4. 形态识别（权重 10%）──
        pattern = tool_results.get("analyze_pattern", {})
        pat_score = 50
        if isinstance(pattern, dict) and "error" not in pattern:
            patterns = pattern.get("patterns", [])
            if patterns:
                p_str = str(patterns[0])
                bullish_patterns = ["锤子线", "吞没", "早晨之星", "三连阳", "长下影线", "蜻蜓线", "突破", "大阳"]
                bearish_patterns = ["倒锤子", "墓碑线", "长上影线", "大阴线", "晚星", "三连阴", "跌破", "大阴"]

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
        # 权重：趋势40% + 动量25% + 量价20% + 形态10% + (筹码附加)
        final_score = int(
            trend_score * 0.40 +
            ind_score * 0.25 +
            vol_score * 0.20 +
            pat_score * 0.10
        )
        # 筹码修正（±5）
        if isinstance(chip, dict) and "error" not in chip:
            profit_ratio = chip.get("profit_ratio")
            if profit_ratio is not None:
                if profit_ratio < 20:
                    final_score += 5  # 套牢盘重，抛压小
                elif profit_ratio > 80:
                    final_score -= 5  # 获利盘重，抛压大

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

        # 信号摘要
        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无明显信号"

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
