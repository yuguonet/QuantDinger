# -*- coding: utf-8 -*-
"""
Technical Skill — 技术分析专家（A股中短线特化）。

职责：趋势阶段判断、量价配合分析、均线系统、技术指标、形态识别。
A股短线定价逻辑下，趋势和量价比基本面更重要。

工具调用记录：
  每次 call_tool 自动记录入参出参到 EvalNode 子树，
  供回溯时验证数据准确率（同 tool 同参数不同时间偏差 > 3σ → 数据源异常）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from app.agent.chain.schema import FactorItem, SkillReport
from app.agent.skills.base import BaseSkill

logger = logging.getLogger(__name__)


class TechnicalSkill(BaseSkill):
    """技术分析专家。"""

    name = "technical_agent"
    description = "技术面综合分析（趋势/量价/均线/指标/形态/筹码）"
    tools = [
        "analyze_trend", "calculate_ma", "get_volume_analysis",
        "analyze_pattern", "get_chip_distribution",
        "get_indicator_snapshot", "generate_kline_chart",
    ]
    priority = 9

    def build_prompt(self, stock_code: str, stock_name: str, context: Dict[str, Any]) -> str:
        return (
            f"你是A股技术分析专家，专注中短线（1-20个交易日）分析。\n\n"
            f"请分析 {stock_name or stock_code}（{stock_code}）的技术面。\n\n"
            "分析流程：\n"
            "1. 趋势阶段判断 — 当前处于哪个阶段（底部吸筹/主升浪/顶部派发/下跌趋势）\n"
            "2. 量价配合度 — 放量突破/缩量回调/高位放量不涨/低位放量不跌\n"
            "3. 均线系统 — 5/10/20/60日均线排列\n"
            "4. 指标验证 — MACD/RSI/BOLL/KDJ 至少2个相互验证\n"
            "5. K线形态 — 突破/反转/整理形态\n\n"
            "A股特别注意：涨停板是极强信号，连板高度代表市场情绪强度，"
            "换手率>15%要警惕，量比>3说明有异动。\n\n"
            "必须调用工具获取真实数据，绝不编造。\n\n"
            "## 输出格式（必须遵守）\n"
            "你的 final_answer 必须包含以下JSON结构：\n\n"
            "```json\n"
            "{\n"
            '  "direction": "bullish/bearish/neutral",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "score": 0-100,\n'
            '  "signal": "一句话信号摘要",\n'
            '  "factors": [\n'
            '    {"name": "因子名", "value": "值", "score": 0-100, "status": "ok"}\n'
            "  ]\n"
            "}\n"
            "```\n\n"
            "规则：\n"
            "- score: 0=极度看空, 50=中性, 100=极度看多\n"
            "- confidence: 数据充分程度（0=完全没数据, 1=数据非常充分）\n"
            "- direction: score>=60=bullish, score<=40=bearish, 其余=neutral\n"
            "- status: ok=有数据, missing=数据缺失\n"
            "- factors: 每个分析维度一行"
        )

    async def analyze(
        self,
        stock_code: str,
        stock_name: str,
        context: Dict[str, Any],
        call_llm: Callable = None,
        call_tool_fn: Callable = None,
    ) -> SkillReport:
        """执行技术面分析。

        流程：
          1. 调用工具获取原始数据（自动记录到 EvalNode 子树）
          2. 将数据注入 prompt
          3. 调用 LLM 生成分析
          4. 解析 LLM 输出为 SkillReport
        """
        if not call_tool_fn or not call_llm:
            return SkillReport(
                skill_name=self.name, status="failed",
                error="call_tool_fn 或 call_llm 未提供",
            )

        # ── Step 1: 调用工具获取数据 ──
        tool_results = {}

        for tool_name in self.tools:
            try:
                result = await self.call_tool(
                    tool_name=tool_name,
                    call_tool_fn=call_tool_fn,
                    stock_code=stock_code,
                )
                if result is not None:
                    tool_results[tool_name] = result
            except Exception as e:
                logger.warning("[Technical] 工具 %s 调用失败: %s", tool_name, e)

        if not tool_results:
            return SkillReport(
                skill_name=self.name, status="missing",
                signal="所有技术分析工具均无数据",
                missing_data=self.tools[:],
            )

        # ── Step 2: 构造含数据的 prompt ──
        prompt = self.build_prompt(stock_code, stock_name, context)
        prompt += "\n\n## 工具返回数据\n\n"
        for tool_name, data in tool_results.items():
            # 截断过长数据
            data_str = json.dumps(data, ensure_ascii=False, default=str)
            if len(data_str) > 3000:
                data_str = data_str[:3000] + "...(截断)"
            prompt += f"### {tool_name}\n```json\n{data_str}\n```\n\n"

        # ── Step 3: 调用 LLM ──
        try:
            raw_output = call_llm(prompt)
        except Exception as e:
            return SkillReport(
                skill_name=self.name, status="failed",
                error=f"LLM 调用失败: {e}",
            )

        # ── Step 4: 解析输出 ──
        from app.agent.chain.contract import parse_skill_output, extract_tools_called
        report = parse_skill_output(raw_output, skill_name=self.name)

        # 补充工具调用记录
        extra_tools = extract_tools_called(raw_output)
        for t in extra_tools:
            if t not in self._tool_calls:
                self._tool_calls.append(t)

        # 补充分析文字
        if not report.analysis:
            report.analysis = raw_output[:2000]

        return report
