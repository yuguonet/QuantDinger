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


@skill("lockup_watcher", auto_load=True)
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
        # search_comprehensive_intel 的结果需要 LLM 解读
        # algo_analyze 不处理这部分，交给 LLM 补位
        news = tool_results.get("search_comprehensive_intel", {})
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
