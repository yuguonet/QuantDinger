# -*- coding: utf-8 -*-
"""
Hot Money Tracker skill — A股游资追踪师。

负责：龙虎榜分析、大单流向、主力资金动态、游资席位追踪。
游资是A股短线定价的核心力量，追踪游资 = 追踪短线alpha。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill


@skill(
    name="hot_money_tracker",
    description="A股游资追踪师。负责龙虎榜分析、大单流向、主力资金动态、游资席位行为追踪。游资是A股短线定价核心力量。当用户问游资、主力、龙虎榜、大单、资金流向时调用。",
    instructions=(
        "你是A股游资追踪师。游资是A股短线定价的核心力量。\n\n"
        "分析框架：\n"
        "1. **龙虎榜解读** — 用 get_dragon_tiger 获取龙虎榜基础数据，用 get_dragon_tiger_detail 获取席位TOP5+机构专用席位动向（更详细）：\n"
        "   - 买入席位是机构还是游资营业部？\n"
        "   - 知名游资席位（如华鑫上海分、中信淮海路等）是否出现？\n"
        "   - 买卖金额对比，净买入/净卖出力度\n"
        "   - 机构专用席位出现 = 机构态度（中期信号）\n"
        "   - 游资席位出现 = 短线态度（1-3天信号）\n"
        "2. **资金流向** — 用 get_fund_flow 查个股资金流，用 get_fund_flow_minute 查盘中分钟级实时资金流，用 get_sector_fund_flow / get_concept_fund_flow 查板块资金：\n"
        "   - 主力净流入/净流出趋势\n"
        "   - 大单、超大单占比（超大单占比高 = 机构行为）\n"
        "   - 连续多日净流入 = 持续看好\n"
        "3. **涨停/跌停池** — 用 get_zt_pool / get_limit_down：\n"
        "   - 涨停家数 > 50 = 市场情绪高涨\n"
        "   - 跌停家数 > 20 = 恐慌情绪\n"
        "   - 连板高度 = 市场投机强度\n"
        "4. **热榜排名** — 用 get_hot_rank 看市场关注度。\n\n"
        "输出格式：\n"
        "- 游资态度：大举做多/小幅做多/观望/小幅撤退/大举撤退\n"
        "- 核心席位动向\n"
        "- 资金流向趋势\n"
        "- 短线操作建议（1-3 个交易日）\n\n"
        "必须调用工具获取真实数据，绝不编造龙虎榜和资金流向数据。"
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
        "get_dragon_tiger", "get_fund_flow", "get_sector_fund_flow",
        "get_concept_fund_flow", "get_zt_pool", "get_limit_down",
        "get_hot_rank", "get_broken_board", "get_market_overview",
        "get_realtime_quote", "agent_get_kline",
        "search_stock_by_name",
    ],
    priority=7,
    default_weight=0.7,
)
class HotMoneyTrackerSkill:
    """A股游资追踪师。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法游资分析。

        权重分配：
          龙虎榜 35% — 净买入力度、机构/游资席位
          资金流向 30% — 主力净流入方向和强度
          涨停情绪 20% — 涨停家数、连板高度
          市场热度 15% — 热榜排名、关注度
        """
        factors = []
        signals = []

        # ── 1. 龙虎榜分析（权重 35%）──
        dragon_score = 50
        dragon = tool_results.get("get_dragon_tiger", {})
        if isinstance(dragon, dict) and "error" not in dragon:
            records = dragon.get("records", dragon.get("stocks", []))
            if records:
                # 汇总净买入
                total_buy = 0
                total_sell = 0
                for r in records:
                    buy_amt = r.get("buy_amount", r.get("l_buy", 0)) or 0
                    sell_amt = r.get("sell_amount", r.get("l_sell", 0)) or 0
                    total_buy += float(buy_amt)
                    total_sell += float(sell_amt)

                net_buy = total_buy - total_sell
                if total_buy + total_sell > 0:
                    net_ratio = net_buy / (total_buy + total_sell)
                else:
                    net_ratio = 0

                # 净买入评分
                if net_ratio > 0.3:
                    dragon_score = 80
                    signals.append(f"龙虎榜净买入{net_ratio:.0%}")
                elif net_ratio > 0.1:
                    dragon_score = 65
                    signals.append(f"龙虎榜小幅净买入")
                elif net_ratio < -0.3:
                    dragon_score = 20
                    signals.append(f"龙虎榜净卖出{abs(net_ratio):.0%}")
                elif net_ratio < -0.1:
                    dragon_score = 35
                    signals.append(f"龙虎榜小幅净卖出")
                else:
                    dragon_score = 50

                # 机构席位检测
                has_institution = False
                for r in records:
                    seats = str(r.get("buy_seats", "") or r.get("reason", ""))
                    if "机构" in seats:
                        has_institution = True
                        break
                if has_institution:
                    dragon_score = min(dragon_score + 10, 100)
                    signals.append("机构席位现身")

                factors.append(FactorItem(
                    name="龙虎榜",
                    value=f"净{'买入' if net_buy > 0 else '卖出'}{abs(net_buy)/10000:.0f}万",
                    score=dragon_score,
                ))
            else:
                factors.append(FactorItem(name="龙虎榜", value="无近期数据", score=50))
        else:
            factors.append(FactorItem(name="龙虎榜", value="数据缺失", score=50))

        # ── 2. 资金流向（权重 30%）──
        flow_score = 50
        flow = tool_results.get("get_fund_flow", {})
        if isinstance(flow, dict) and "error" not in flow:
            main_flow = flow.get("main_flow", 0) or 0
            net_flow = flow.get("net_flow", 0) or 0

            # 主力资金评分
            if main_flow > 5000:
                flow_score = 85
                signals.append(f"主力净流入{main_flow/10000:.1f}亿")
            elif main_flow > 1000:
                flow_score = 70
                signals.append(f"主力净流入{main_flow:.0f}万")
            elif main_flow < -5000:
                flow_score = 15
                signals.append(f"主力净流出{abs(main_flow)/10000:.1f}亿")
            elif main_flow < -1000:
                flow_score = 30
                signals.append(f"主力净流出{abs(main_flow):.0f}万")
            else:
                flow_score = 50

            factors.append(FactorItem(
                name="资金流向",
                value=f"主力{'流入' if main_flow > 0 else '流出'}{abs(main_flow):.0f}万",
                score=flow_score,
            ))
        else:
            factors.append(FactorItem(name="资金流向", value="数据缺失", score=50))

        # ── 3. 涨停情绪（权重 20%）──
        sentiment_score = 50
        zt = tool_results.get("get_zt_pool", {})
        if isinstance(zt, dict) and "error" not in zt:
            zt_stocks = zt.get("stocks", [])
            zt_count = len(zt_stocks)

            # 连板高度
            max_continuous = 0
            for s in zt_stocks:
                days = int(s.get("continuous_zt_days", 0) or 0)
                max_continuous = max(max_continuous, days)

            # 涨停家数评分
            if zt_count >= 80:
                sentiment_score = 90
                signals.append(f"涨停{zt_count}家，极度亢奋")
            elif zt_count >= 50:
                sentiment_score = 75
                signals.append(f"涨停{zt_count}家，情绪高涨")
            elif zt_count >= 20:
                sentiment_score = 55
            elif zt_count >= 5:
                sentiment_score = 40
                signals.append(f"涨停仅{zt_count}家，情绪低迷")
            else:
                sentiment_score = 25
                signals.append(f"涨停{zt_count}家，冰点")

            # 连板高度修正
            if max_continuous >= 5:
                sentiment_score = min(sentiment_score + 10, 100)
                signals.append(f"最高连板{max_continuous}天，投机活跃")
            elif max_continuous >= 3:
                sentiment_score = min(sentiment_score + 5, 100)

            factors.append(FactorItem(
                name="涨停情绪",
                value=f"{zt_count}家涨停，最高{max_continuous}连板",
                score=sentiment_score,
            ))
        else:
            factors.append(FactorItem(name="涨停情绪", value="数据缺失", score=50))

        # ── 4. 市场热度（权重 15%）──
        heat_score = 50
        hot = tool_results.get("get_hot_rank", {})
        if isinstance(hot, dict) and "error" not in hot:
            stocks = hot.get("stocks", [])
            if stocks and stock_code:
                # 查找目标股票在热榜中的排名
                target_code = stock_code.strip().replace(".", "").upper()
                rank = None
                for i, s in enumerate(stocks):
                    code = str(s.get("code", s.get("stock_code", ""))).replace(".", "").upper()
                    if code == target_code or target_code in code:
                        rank = i + 1
                        break

                if rank is not None:
                    if rank <= 10:
                        heat_score = 85
                        signals.append(f"热榜第{rank}名，高关注")
                    elif rank <= 30:
                        heat_score = 65
                    else:
                        heat_score = 55
                    factors.append(FactorItem(
                        name="市场热度",
                        value=f"热榜第{rank}名",
                        score=heat_score,
                    ))
                else:
                    factors.append(FactorItem(name="市场热度", value="未上榜", score=40))
            else:
                factors.append(FactorItem(name="市场热度", value="无数据", score=50))
        else:
            factors.append(FactorItem(name="市场热度", value="数据缺失", score=50))

        # ── 跌停情绪修正 ──
        limit_down = tool_results.get("get_limit_down", {})
        if isinstance(limit_down, dict) and "error" not in limit_down:
            ld_stocks = limit_down.get("stocks", [])
            ld_count = len(ld_stocks)
            if ld_count >= 20:
                sentiment_score = max(sentiment_score - 20, 0)
                signals.append(f"跌停{ld_count}家，恐慌")
            elif ld_count >= 10:
                sentiment_score = max(sentiment_score - 10, 0)

        # ── 综合评分 ──
        final_score = int(
            dragon_score * 0.35 +
            flow_score * 0.30 +
            sentiment_score * 0.20 +
            heat_score * 0.15
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

        # 游资态度
        if final_score >= 80:
            attitude = "大举做多"
        elif final_score >= 60:
            attitude = "小幅做多"
        elif final_score >= 40:
            attitude = "观望"
        elif final_score >= 25:
            attitude = "小幅撤退"
        else:
            attitude = "大举撤退"

        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无明显信号"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"游资态度:{attitude} 综合评分:{final_score}/100。"
                     f"龙虎榜:{dragon_score} 资金流向:{flow_score} "
                     f"涨停情绪:{sentiment_score} 市场热度:{heat_score}",
            status="ok",
        )
