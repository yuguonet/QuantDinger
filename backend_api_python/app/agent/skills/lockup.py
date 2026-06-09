# -*- coding: utf-8 -*-
"""
Lockup Watcher skill — A股解禁监控师。

负责：限售股解禁监控、大股东减持预警、股权质押风险评估。
解禁是A股特有的供给冲击因素，大规模解禁前后股价承压概率高。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill


@skill(
    name="lockup_watcher",
    description="A股解禁监控师。负责限售股解禁监控、大股东减持预警、股权质押风险评估。解禁是A股特有的供给冲击。当用户问解禁、限售股、减持、质押、供给压力时调用。",
    instructions=(
        "你是A股解禁监控师。解禁是A股特有的供给冲击因素。\n\n"
        "分析框架：\n"
        "1. **解禁日历** — 用 get_lockup_expiry 直接获取解禁数据（历史+未来90天待解禁），\n"
        "   解禁日期、解禁数量、解禁比例、限售股类型。配合 search_stock_news 搜索解禁相关新闻补充。\n"
        "   - 解禁日期、解禁数量、解禁市值\n"
        "   - 解禁股东类型：IPO 原始股（冲击最大）/ 定增（次之）/ 股权激励（较小）\n"
        "   - 解禁市值占流通市值比例：> 30% = 高风险，> 50% = 极高风险\n"
        "   - 解禁前 5 个交易日通常开始承压\n"
        "2. **减持动态** — 搜索大股东减持公告：\n"
        "   - 减持方式：集中竞价（直接砸盘）/ 大宗交易（间接影响）\n"
        "   - 减持比例和进度\n"
        "   - 是否提前公告（预告减持比突然减持更确定）\n"
        "3. **质押风险** — 搜索股权质押信息：\n"
        "   - 质押比例 > 50% = 高风险\n"
        "   - 接近平仓线 = 极高风险\n"
        "4. **综合评估** — 结合解禁规模、减持意愿、质押压力，给出供给端风险评级。\n\n"
        "输出格式：\n"
        "- 供给端风险评级：极高/高/中/低/无明显风险\n"
        "- 近期解禁事件及影响评估\n"
        "- 减持动态汇总\n"
        "- 质押风险提示（如有）\n"
        "- 操作建议（回避/轻仓观望/可参与）\n\n"
        "必须用 search_stock_news 获取真实数据，绝不编造解禁和减持信息。"
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
        "search_stock_news", "search_comprehensive_intel", "get_lockup_expiry",
        "get_realtime_quote", "agent_get_kline",
        "search_stock_by_name",
    ],
    priority=6,
    default_weight=0.8,
)
class LockupWatcherSkill:
    """A股解禁监控师子 Agent。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法解禁监控。

        核心逻辑：
          1. get_lockup_expiry → 解禁数据（日期/数量/比例/类型）
          2. 按阈值打分（解禁比例 > 30% = 高风险）
          3. 搜索新闻补充减持/质押信息（需要 LLM，跳过）
        """
        factors = []
        signals = []

        # ── 解禁数据（纯算法）──
        lockup = tool_results.get("get_lockup_expiry", {})
        lockup_score = 50
        if isinstance(lockup, dict) and "error" not in lockup:
            upcoming = lockup.get("upcoming", [])
            recent = lockup.get("recent", [])

            if upcoming:
                # 最近一次解禁
                next_lockup = upcoming[0] if isinstance(upcoming, list) else upcoming
                unlock_date = next_lockup.get("unlock_date", "")
                unlock_ratio = float(next_lockup.get("unlock_ratio", 0) or 0)
                unlock_type = next_lockup.get("lock_type", "")

                # 解禁比例评分
                if unlock_ratio >= 50:
                    lockup_score = 10  # 极高风险
                    signals.append(f"解禁{unlock_ratio:.0f}%极高风险")
                elif unlock_ratio >= 30:
                    lockup_score = 25  # 高风险
                    signals.append(f"解禁{unlock_ratio:.0f}%高风险")
                elif unlock_ratio >= 10:
                    lockup_score = 40  # 中等风险
                    signals.append(f"解禁{unlock_ratio:.0f}%")
                else:
                    lockup_score = 55  # 低风险

                # 解禁类型修正
                if "IPO" in str(unlock_type) or "原始" in str(unlock_type):
                    lockup_score = max(lockup_score - 10, 0)
                    signals.append("IPO原始股解禁")
                elif "定增" in str(unlock_type):
                    lockup_score = max(lockup_score - 5, 0)

                factors.append(FactorItem(
                    name="解禁",
                    value=f"{unlock_date} 解禁{unlock_ratio:.0f}%",
                    score=lockup_score,
                    status="ok",
                ))
            else:
                lockup_score = 60  # 无解禁 = 偏利好
                factors.append(FactorItem(
                    name="解禁",
                    value="近期无解禁",
                    score=60,
                    status="ok",
                ))
                signals.append("无解禁压力")
        else:
            factors.append(FactorItem(name="解禁", value="数据缺失", score=50, status="missing"))

        # ── 新闻搜索（需要 LLM 解读，algo 跳过）──
        # search_stock_news 和 search_comprehensive_intel 的结果需要 LLM 解读
        # algo_analyze 不处理这部分，交给 LLM 补位
        news = tool_results.get("search_stock_news", {})
        if isinstance(news, dict) and "error" not in news:
            news_list = news.get("news", [])
            if news_list:
                # 简单关键词匹配
                risk_keywords = ["减持", "质押", "解禁", "违规", "处罚", "暴跌"]
                for item in news_list[:5]:
                    title = str(item.get("title", ""))
                    if any(kw in title for kw in risk_keywords):
                        signals.append(f"⚠️{title[:20]}")
                        lockup_score = min(lockup_score, 35)

        # ── 综合 ──
        final_score = max(0, min(100, lockup_score))

        if final_score >= 60:
            direction = "bullish"
        elif final_score <= 40:
            direction = "bearish"
        else:
            direction = "neutral"

        valid_count = sum(1 for f in factors if f.status == "ok")
        confidence = round(min(valid_count / 2, 1.0), 2)

        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "无明显供给压力"

        # 风险评级
        if final_score <= 20:
            risk_rating = "极高"
        elif final_score <= 35:
            risk_rating = "高"
        elif final_score <= 50:
            risk_rating = "中"
        elif final_score <= 60:
            risk_rating = "低"
        else:
            risk_rating = "无明显风险"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"供给端风险评级:{risk_rating}，综合评分:{final_score}/100",
            status="ok",
        )
