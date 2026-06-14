# -*- coding: utf-8 -*-
"""
Screening skill — 选股专家（A股动量+概念筛选特化）。

负责：条件选股、动量筛选、概念选股、龙虎榜、涨停池、热榜。
A股选股核心：先看概念热度和资金方向，再用技术指标验证。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill

logger = logging.getLogger(__name__)


@skill(
    name="screening_agent",
    description="选股专家。负责条件选股、动量筛选、概念选股、龙虎榜、涨停池、热榜、指标验证。A股选股先看概念和资金，再验证技术面。当用户要求选股、筛选股票时调用。",
    instructions=(
        "你是A股选股专家。\n\n"
        "选股策略（按A股有效性排序）：\n"
        "1. **概念/题材选股** — 先确定当前热门概念，再找概念内的强势股。\n"
        "   - 关注连续涨停的龙头股（辨识度高、资金共识强）\n"
        "   - 概念内补涨股（涨幅落后但逻辑一致）\n"
        "2. **动量选股** — 短线强势股筛选：\n"
        "   - 近 N 日涨幅排名\n"
        "   - 连续放量上涨\n"
        "   - 突破关键均线或前高\n"
        "3. **资金选股** — 跟踪聪明钱：\n"
        "   - 龙虎榜机构席位净买入（get_dragon_tiger / get_dragon_tiger_detail 看机构专用席位）\n"
        "   - 主力资金净流入（get_fund_flow / get_fund_flow_minute 看盘中实时 / get_fund_flow_120d 看中长期趋势）\n"
        "   - 涨停池（get_zt_pool）看市场最强股\n"
        "4. **条件选股** — 用 search_stocks 按自然语言条件筛选。\n"
        "5. **指标验证** — 用 run_indicator_signal 验证筛选结果的技术信号。\n\n"
        "用 get_hot_rank 看市场关注度排名，用 get_limit_down / get_broken_board 看情绪面。\n"
        "   用 get_valuation_metrics 获取PE/PB/市值做估值筛选，用 get_holder_count 看筹码集中度。\n"
        "   用 get_stock_sector_info 查个股所属行业/概念，配合热门板块做概念选股。\n\n"
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
        "search_stocks", "get_screener_presets",
        "get_zt_pool", "get_dragon_tiger", "get_hot_rank",
        "get_limit_down", "get_broken_board", "get_market_overview",
        "list_indicators",
        "get_realtime_quote", "agent_get_kline",
        "search_stock_by_name",
    ],
    priority=8,
    default_weight=1.0,
)
class ScreeningSkill:
    """选股专家。"""

    def analyze(
        self,
        stock_code: str,
        stock_name: str,
        context: Dict[str, Any],
        call_llm: Callable = None,
        call_tool_fn: Callable = None,
        _tool_calls: List[str] = None,
        _tool_nodes: List = None,
        _missing_data: List[str] = None,
    ) -> SkillReport:
        """覆盖默认 analyze：选股是市场级扫描，不依赖 stock_code。"""
        if not call_tool_fn:
            return SkillReport(skill_name=self.name, status="failed", error="call_tool_fn 未提供")

        # Step 1: 调用市场级工具（不传 stock_code）
        tool_results = {}
        market_tools = ["get_zt_pool", "get_dragon_tiger", "get_hot_rank", "get_limit_down"]
        for tool_name in market_tools:
            try:
                result = self.call_tool(
                    tool_name=tool_name,
                    call_tool_fn=call_tool_fn,
                    stock_code="",  # 空 = 全市场
                    _tool_calls=_tool_calls,
                    _tool_nodes=_tool_nodes,
                    _missing_data=_missing_data,
                )
                if result is not None:
                    tool_results[tool_name] = result
            except Exception as e:
                logger.warning("[Skill:%s] 工具 %s 调用失败: %s", self.name, tool_name, e)

        if not tool_results:
            return SkillReport(
                skill_name=self.name, status="missing",
                signal="所有工具均无数据",
                missing_data=market_tools[:],
            )

        # Step 2: 纯算法选股
        algo_report = self.algo_analyze(stock_code, stock_name, tool_results)
        if algo_report is not None:
            algo_report.tools_called = list(tool_results.keys())
            algo_report.missing_data = list(_missing_data or [])
            return algo_report

        # Step 3: algo 覆盖不到时走 LLM（不应走到这里）
        return SkillReport(
            skill_name=self.name, status="ok",
            score=50, direction="neutral",
            signal="算法未覆盖，请查看原始数据",
            confidence=0.3,
        )

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法选股。

        逻辑：从涨停池 + 龙虎榜 + 热榜中提取股票，交叉验证后评分排名。

        权重分配：
          涨停池 40% — 连板龙头、涨停强度
          龙虎榜 35% — 机构/游资净买入
          热榜 15% — 市场关注度
          跌停情绪 10% — 市场恐慌程度（负向）
        """
        factors = []
        signals = []

        # 收集各来源的股票集合
        zt_stocks = {}   # code → {name, continuous_days, seal_amount}
        dragon_stocks = {}  # code → {name, net_buy}
        hot_stocks = {}  # code → {rank}

        # ── 1. 涨停池 ──
        zt = tool_results.get("get_zt_pool", {})
        zt_score = 50
        if isinstance(zt, dict) and "error" not in zt:
            stocks = zt.get("stocks", [])
            for s in stocks:
                code = str(s.get("code", s.get("stock_code", ""))).strip()
                if not code:
                    continue
                zt_stocks[code] = {
                    "name": s.get("name", ""),
                    "continuous_days": int(s.get("continuous_zt_days", 0) or 0),
                    "seal_amount": float(s.get("seal_amount", 0) or 0),
                }

            zt_count = len(zt_stocks)
            max_continuous = max((v["continuous_days"] for v in zt_stocks.values()), default=0)

            if zt_count >= 50:
                zt_score = 85
                signals.append(f"涨停{zt_count}家，情绪亢奋")
            elif zt_count >= 20:
                zt_score = 65
            elif zt_count >= 5:
                zt_score = 45
            else:
                zt_score = 25
                signals.append(f"涨停仅{zt_count}家，冰点")

            if max_continuous >= 5:
                signals.append(f"最高{max_continuous}连板")

            factors.append(FactorItem(
                name="涨停池",
                value=f"{zt_count}家，最高{max_continuous}连板",
                score=zt_score,
            ))
        else:
            factors.append(FactorItem(name="涨停池", value="数据缺失", score=50))

        # ── 2. 龙虎榜 ──
        dragon = tool_results.get("get_dragon_tiger", {})
        dragon_score = 50
        if isinstance(dragon, dict) and "error" not in dragon:
            records = dragon.get("records", dragon.get("stocks", []))
            for r in records:
                code = str(r.get("stock_code", r.get("code", ""))).strip()
                if not code:
                    continue
                buy_amt = float(r.get("buy_amount", r.get("l_buy", 0)) or 0)
                sell_amt = float(r.get("sell_amount", r.get("l_sell", 0)) or 0)
                net = buy_amt - sell_amt
                dragon_stocks[code] = {
                    "name": r.get("name", r.get("stock_name", "")),
                    "net_buy": net,
                }

            if dragon_stocks:
                total_net = sum(v["net_buy"] for v in dragon_stocks.values())
                if total_net > 0:
                    dragon_score = min(60 + int(total_net / 10000), 90)
                    signals.append(f"龙虎榜整体净买入")
                else:
                    dragon_score = max(40 - int(abs(total_net) / 10000), 10)

                factors.append(FactorItem(
                    name="龙虎榜",
                    value=f"{len(dragon_stocks)}只上榜",
                    score=dragon_score,
                ))
            else:
                factors.append(FactorItem(name="龙虎榜", value="无数据", score=50))
        else:
            factors.append(FactorItem(name="龙虎榜", value="数据缺失", score=50))

        # ── 3. 热榜 ──
        hot = tool_results.get("get_hot_rank", {})
        heat_score = 50
        if isinstance(hot, dict) and "error" not in hot:
            stocks = hot.get("stocks", [])
            for i, s in enumerate(stocks[:30]):
                code = str(s.get("code", s.get("stock_code", ""))).strip()
                if code:
                    hot_stocks[code] = {"rank": i + 1}

            if hot_stocks:
                heat_score = 60
                factors.append(FactorItem(
                    name="市场热度",
                    value=f"热榜{len(hot_stocks)}只",
                    score=heat_score,
                ))
            else:
                factors.append(FactorItem(name="市场热度", value="无数据", score=50))
        else:
            factors.append(FactorItem(name="市场热度", value="数据缺失", score=50))

        # ── 4. 跌停情绪修正 ──
        limit_down = tool_results.get("get_limit_down", {})
        ld_penalty = 0
        if isinstance(limit_down, dict) and "error" not in limit_down:
            ld_stocks = limit_down.get("stocks", [])
            ld_count = len(ld_stocks)
            if ld_count >= 20:
                ld_penalty = 20
                signals.append(f"跌停{ld_count}家，恐慌")
            elif ld_count >= 10:
                ld_penalty = 10

            factors.append(FactorItem(
                name="跌停情绪",
                value=f"{ld_count}家跌停",
                score=max(50 - ld_count, 0),
            ))
        else:
            factors.append(FactorItem(name="跌停情绪", value="数据缺失", score=50))

        # ── 交叉验证：出现在多个来源的股票加分 ──
        all_codes = set(zt_stocks.keys()) | set(dragon_stocks.keys()) | set(hot_stocks.keys())
        cross_scored = []
        for code in all_codes:
            score = 0
            reasons = []
            if code in zt_stocks:
                cd = zt_stocks[code]["continuous_days"]
                score += 30 + cd * 10
                reasons.append(f"{cd + 1}连板" if cd > 0 else "涨停")
            if code in dragon_stocks:
                nb = dragon_stocks[code]["net_buy"]
                if nb > 0:
                    score += 25
                    reasons.append("龙虎榜净买入")
                else:
                    score += 10
                    reasons.append("龙虎榜上榜")
            if code in hot_stocks:
                rank = hot_stocks[code]["rank"]
                score += max(20 - rank, 5)
                reasons.append(f"热榜{rank}")

            if score > 0:
                cross_scored.append({
                    "code": code,
                    "name": zt_stocks.get(code, {}).get("name",
                           dragon_stocks.get(code, {}).get("name", "")),
                    "score": score,
                    "reasons": reasons,
                })

        # 按分数排序
        cross_scored.sort(key=lambda x: -x["score"])
        top_stocks = cross_scored[:10]

        # ── 综合评分 ──
        final_score = int(
            zt_score * 0.40 +
            dragon_score * 0.35 +
            heat_score * 0.15 +
            (50 - ld_penalty) * 0.10
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

        # 选股摘要
        if top_stocks:
            stock_lines = []
            for s in top_stocks[:5]:
                reasons_str = "+".join(s["reasons"])
                stock_lines.append(f"{s['name']}({s['code']}) {reasons_str}")
            signal_text = "选股: " + " | ".join(stock_lines)
        else:
            signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无明显信号"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"综合评分:{final_score}/100。"
                     f"涨停池:{zt_score} 龙虎榜:{dragon_score} "
                     f"市场热度:{heat_score} 跌停修正:-{ld_penalty}。"
                     f"筛选出{len(top_stocks)}只候选股。",
            status="ok",
        )
