# -*- coding: utf-8 -*-
"""
Market Data Skill — 行情数据专家（A股板块轮动 + 概念追踪特化）。

合并原 market_data_agent + concept_tracker：
  实时行情、K线数据、指数、板块排名、资金流向、概念板块热度、
  涨停池、热榜、题材生命周期判断、龙头识别。
A股板块轮动是核心特征，行情分析必须关注板块和概念维度。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.registry import skill


@skill("market_data_agent", auto_load=True)

class MarketDataSkill:
    """行情数据专家（含概念追踪）。"""

    def algo_analyze(
        self,
        stock_code: str,
        stock_name: str,
        tool_results: Dict[str, Any],
        **kwargs,
    ) -> Optional[SkillReport]:
        """纯算法行情数据 + 概念追踪汇总。

        核心逻辑：
          1. 大盘指数涨跌 → 市场环境评分
          2. 板块排名 → 板块强度
          3. 资金流向 → 主力态度
          4. 涨停池/热榜 → 情绪面 + 概念热度
          5. 实时报价 → 个股状态
        """
        factors = []
        signals = []

        # ── 1. 大盘指数（权重 25%）──
        indices = tool_results.get("get_market_indices", {})
        idx_score = 50
        if isinstance(indices, dict) and "error" not in indices:
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
                    score=idx_score, status="ok",
                ))
            else:
                factors.append(FactorItem(name="大盘", value="数据缺失", score=50, status="missing"))
        else:
            factors.append(FactorItem(name="大盘", value="数据缺失", score=50, status="missing"))

        # ── 2. 板块排名（权重 20%）──
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
                    score=sec_score, status="ok",
                ))
            else:
                factors.append(FactorItem(name="板块", value="无数据", score=50, status="missing"))
        else:
            factors.append(FactorItem(name="板块", value="数据缺失", score=50, status="missing"))

        # ── 3. 资金流向（权重 20%）──
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
                score=flow_score, status="ok",
            ))
        else:
            factors.append(FactorItem(name="资金", value="数据缺失", score=50, status="missing"))

        # ── 4. 涨停池/情绪面（权重 15%）──
        zt_pool = tool_results.get("get_limit_pool", {})
        emotion_score = 50
        if isinstance(zt_pool, dict) and "error" not in zt_pool:
            zt_data = zt_pool.get("zt", zt_pool)
            zt_count = zt_data.get("count", 0) or len(zt_data.get("stocks", []))
            if zt_count > 50:
                emotion_score = 80
                signals.append(f"涨停{zt_count}家情绪高涨")
            elif zt_count > 20:
                emotion_score = 65
                signals.append(f"涨停{zt_count}家")
            elif zt_count > 5:
                emotion_score = 50
            else:
                emotion_score = 35

            factors.append(FactorItem(
                name="情绪",
                value=f"涨停{zt_count}家",
                score=emotion_score, status="ok",
            ))
        else:
            factors.append(FactorItem(name="情绪", value="数据缺失", score=50, status="missing"))

        # ── 5. 个股行情（权重 20%）──
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
                score=quote_score, status="ok",
            ))
        else:
            factors.append(FactorItem(name="行情", value="数据缺失", score=50, status="missing"))

        # ── 综合 ──
        final_score = int(
            idx_score * 0.25 +
            sec_score * 0.20 +
            flow_score * 0.20 +
            emotion_score * 0.15 +
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
        confidence = round(min(valid_count / 5, 1.0), 2)

        signal_text = ",".join(str(s) for s in signals[:5]) if signals else "市场平稳"

        return SkillReport(
            skill_name=self.name,
            score=float(final_score),
            direction=direction,
            signal=signal_text,
            confidence=confidence,
            factors=factors,
            analysis=f"行情综合评分:{final_score}/100。大盘:{idx_score} 板块:{sec_score} 资金:{flow_score} 情绪:{emotion_score} 行情:{quote_score}",
            status="ok",
        )
