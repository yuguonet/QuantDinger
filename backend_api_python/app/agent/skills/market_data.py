# -*- coding: utf-8 -*-
"""
Market Data skill — 行情数据专家（A股板块轮动特化）。

负责：实时行情、K线数据、指数、板块排名、资金流向、概念板块。
A股板块轮动是核心特征，行情分析必须关注板块和概念维度。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill


@skill(
    name="market_data_agent",
    description="行情数据专家。负责实时行情、K线数据、大盘指数、板块排名、概念板块热度、资金流向。A股板块轮动是核心特征。当用户问行情、报价、指数、板块、资金流向时调用。",
    instructions=(
        "你是A股行情数据专家。\n\n"
        "数据获取流程：\n"
        "1. **大盘环境** — 用 get_market_indices 看大盘指数，用 get_index_etf_quote 获取更多指数+ETF行情（支持沪深300/创业板/上证50ETF等）。\n"
        "   - 大盘方向决定仓位上限（下跌市轻仓，上涨市可重仓）\n"
        "2. **板块轮动** — 用 get_hot_sectors 获取实时热门板块（涨停数/领涨股/强度标签/情绪判断），\n"
        "   用 get_sector_trend_analysis 查板块趋势分析（持续走强/走弱+季节性规律），\n"
        "   用 get_sector_history_data 获取板块历史排名走势。\n"
        "   - 今日领涨板块 = 短期资金偏好\n"
        "   - 连续领涨板块 = 中期主线\n"
        "3. **概念热度** — 关注概念板块的涨停数量和连板高度。\n"
        "4. **资金流向** — 用 get_fund_flow / get_sector_fund_flow / get_concept_fund_flow。\n"
        "   - 主力净流入方向 = 聪明钱态度\n"
        "   - 板块资金流向 = 轮动方向\n"
        "5. **个股行情** — 用 get_realtime_quote 获取实时报价，agent_get_kline 获取K线。\n\n"
        "A股特别注意：\n"
        "- 两市成交额 < 8000 亿 = 缩量，短线难做\n"
        "- 两市成交额 > 1.5 万亿 = 放量，活跃度高\n"
        "- 北向资金流向是重要参考指标\n"
        "- 涨停家数/跌停家数比 = 市场情绪温度计\n\n"
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
        "get_realtime_quote", "agent_get_kline", "get_stock_info",
        "search_stock_by_name",
        "get_market_indices", "get_sector_rankings",
        "get_market_overview",
        "get_fund_flow", "get_sector_fund_flow", "get_concept_fund_flow",
    ],
    priority=10,
    default_weight=0.9,
)
class MarketDataSkill:
    """行情数据专家子 Agent。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法行情数据汇总。

        核心逻辑：
          1. 大盘指数涨跌 → 市场环境评分
          2. 板块排名 → 板块强度
          3. 资金流向 → 主力态度
          4. 实时报价 → 个股状态
        """
        factors = []
        signals = []

        # ── 1. 大盘指数（权重 30%）──
        indices = tool_results.get("get_market_indices", {})
        idx_score = 50
        if isinstance(indices, dict) and "error" not in indices:
            # 取上证指数涨跌幅
            sh_index = None
            for idx in indices.get("indices", []):
                if "上证" in str(idx.get("name", "")):
                    sh_index = idx
                    break
            if not sh_index and indices.get("indices"):
                sh_index = indices["indices"][0]

            if sh_index:
                change_pct = float(sh_index.get("change_pct", 0) or 0)
                if change_pct > 1.0:
                    idx_score = 75
                    signals.append(f"大盘涨{change_pct:.1f}%")
                elif change_pct > 0.3:
                    idx_score = 60
                elif change_pct < -1.0:
                    idx_score = 25
                    signals.append(f"大盘跌{change_pct:.1f}%")
                elif change_pct < -0.3:
                    idx_score = 40
                else:
                    idx_score = 50

                factors.append(FactorItem(
                    name="大盘",
                    value=f"{sh_index.get('name', '')} {change_pct:+.2f}%",
                    score=idx_score,
                    status="ok",
                ))
            else:
                factors.append(FactorItem(name="大盘", value="数据缺失", score=50, status="missing"))
        else:
            factors.append(FactorItem(name="大盘", value="数据缺失", score=50, status="missing"))

        # ── 2. 板块排名（权重 25%）──
        sectors = tool_results.get("get_sector_rankings", {})
        sec_score = 50
        if isinstance(sectors, dict) and "error" not in sectors:
            sector_list = sectors.get("sectors", [])
            if sector_list:
                top_sector = sector_list[0] if isinstance(sector_list, list) else sector_list
                sec_name = top_sector.get("name", "")
                sec_change = float(top_sector.get("change_pct", 0) or 0)
                if sec_change > 2:
                    sec_score = 70
                    signals.append(f"领涨板块{sec_name}")
                elif sec_change > 0:
                    sec_score = 55
                else:
                    sec_score = 40

                factors.append(FactorItem(
                    name="板块",
                    value=f"{sec_name} {sec_change:+.2f}%",
                    score=sec_score,
                    status="ok",
                ))
            else:
                factors.append(FactorItem(name="板块", value="无数据", score=50, status="missing"))
        else:
            factors.append(FactorItem(name="板块", value="数据缺失", score=50, status="missing"))

        # ── 3. 资金流向（权重 25%）──
        fund_flow = tool_results.get("get_fund_flow", {})
        flow_score = 50
        if isinstance(fund_flow, dict) and "error" not in fund_flow:
            net_inflow = float(fund_flow.get("net_inflow", 0) or 0)
            if net_inflow > 0:
                flow_score = 65
                signals.append(f"主力净流入{net_inflow/10000:.1f}万")
            elif net_inflow < 0:
                flow_score = 35
                signals.append(f"主力净流出{abs(net_inflow)/10000:.1f}万")

            factors.append(FactorItem(
                name="资金",
                value=f"净{'流入' if net_inflow > 0 else '流出'}{abs(net_inflow)/10000:.1f}万",
                score=flow_score,
                status="ok",
            ))
        else:
            factors.append(FactorItem(name="资金", value="数据缺失", score=50, status="missing"))

        # ── 4. 个股行情（权重 20%）──
        quote = tool_results.get("get_realtime_quote", {})
        quote_score = 50
        if isinstance(quote, dict) and "error" not in quote:
            change_pct = float(quote.get("change_pct", 0) or 0)
            volume_ratio = float(quote.get("volume_ratio", 1) or 1)

            if change_pct > 5:
                quote_score = 80
                signals.append(f"涨{change_pct:.1f}%")
            elif change_pct > 0:
                quote_score = 60
            elif change_pct < -5:
                quote_score = 20
                signals.append(f"跌{change_pct:.1f}%")
            elif change_pct < 0:
                quote_score = 40

            if volume_ratio > 3:
                signals.append(f"量比{volume_ratio:.1f}异动")

            factors.append(FactorItem(
                name="行情",
                value=f"{change_pct:+.2f}% 量比{volume_ratio:.1f}",
                score=quote_score,
                status="ok",
            ))
        else:
            factors.append(FactorItem(name="行情", value="数据缺失", score=50, status="missing"))

        # ── 综合 ──
        final_score = int(
            idx_score * 0.30 +
            sec_score * 0.25 +
            flow_score * 0.25 +
            quote_score * 0.20
        )
        final_score = max(0, min(100, final_score))

        if final_score >= 60:
            direction = "bullish"
        elif final_score <= 40:
            direction = "bearish"
        else:
            direction = "neutral"

        valid_count = sum(1 for f in factors if f.status == "ok")
        confidence = round(min(valid_count / 4, 1.0), 2)

        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "市场平稳"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"行情综合评分:{final_score}/100。大盘:{idx_score} 板块:{sec_score} 资金:{flow_score} 行情:{quote_score}",
            status="ok",
        )
