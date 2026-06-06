# -*- coding: utf-8 -*-
"""
Chain Executor — 链路执行器。

负责：
1. 按链路定义依次调度子 Agent
2. 记录每步执行结果到数据库
3. 支持中途失败处理和跳过
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional

from app.agent.chain.chains import ChainDef, ChainStep, get_chain

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 执行结果数据结构
# ═══════════════════════════════════════════════════════════════

class StepResult:
    """单步执行结果。"""

    def __init__(self, step: ChainStep):
        self.step = step
        self.success = False
        self.conclusion = ""
        self.direction = "neutral"  # bullish / bearish / neutral
        self.confidence = 0.0
        self.tools_called: List[str] = []
        self.tools_detail: List[Dict[str, Any]] = []  # [{name, success, latency_ms, error}]
        self.raw_output = ""
        self.error = ""
        self.elapsed_ms = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_name": self.step.name,
            "agent_name": self.step.agent,
            "step_order": self.step.order,
            "success": self.success,
            "conclusion": self.conclusion,
            "direction": self.direction,
            "confidence": self.confidence,
            "tools_called": self.tools_called,
            "tools_detail": self.tools_detail,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


class ChainResult:
    """整条链路执行结果。"""

    def __init__(self, chain_def: ChainDef, stock_code: str, stock_name: str = ""):
        self.chain_def = chain_def
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.step_results: List[StepResult] = []
        self.final_direction = "neutral"
        self.final_confidence = 0.0
        self.summary = ""
        self.success = False
        self.execution_id: Optional[int] = None
        self.elapsed_ms = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_def.chain_id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "final_direction": self.final_direction,
            "final_confidence": self.final_confidence,
            "summary": self.summary,
            "success": self.success,
            "elapsed_ms": self.elapsed_ms,
            "steps": [s.to_dict() for s in self.step_results],
        }


# ═══════════════════════════════════════════════════════════════
# 结论提取器
# ═══════════════════════════════════════════════════════════════

def _extract_direction(text: str) -> str:
    """从 agent 输出中提取方向判断。"""
    text = text.lower()
    bullish_words = ["看多", "看涨", "利多", "利好", "买入", "上涨", "强势",
                     "bullish", "buy", "positive", "金叉", "突破", "放量"]
    bearish_words = ["看空", "看跌", "利空", "利淡", "卖出", "下跌", "弱势",
                     "bearish", "sell", "negative", "死叉", "破位", "缩量"]

    bull_count = sum(1 for w in bullish_words if w in text)
    bear_count = sum(1 for w in bearish_words if w in text)

    if bull_count > bear_count:
        return "bullish"
    elif bear_count > bull_count:
        return "bearish"
    return "neutral"


def _extract_confidence(text: str) -> float:
    """从 agent 输出中提取置信度。"""
    import re
    # 尝试匹配 "置信度: 0.8" 或 "confidence: 80%" 等模式
    patterns = [
        r'置信度[：:]\s*(\d+\.?\d*)%?',
        r'confidence[：:]\s*(\d+\.?\d*)%?',
        r'(\d+\.?\d*)%',
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            val = float(match.group(1))
            if val > 1:
                val = val / 100
            return min(val, 1.0)
    return 0.5  # 默认中等置信度


def _parse_tool_details(text: str) -> List[Dict[str, Any]]:
    """从 agent 输出中解析工具调用详情。

    支持多种 agent 输出格式：
    - smolagents: "工具: xxx" 或 "Calling tool: xxx"
    - 通用格式: "```tool_call\nxxx\n```"
    - JSON 块: {"tool": "xxx", "success": true}
    """
    import re
    details = []
    seen = set()

    # 模式1: JSON 工具调用块
    for m in re.finditer(r'\{[^{}]*"tool"\s*:\s*"([^"]+)"[^{}]*\}', text):
        try:
            # 尝试解析完整 JSON
            start = text.rfind('{', 0, m.start())
            end = text.find('}', m.end()) + 1
            obj = json.loads(text[start:end])
            name = obj.get("tool", "")
            if name and name not in seen:
                seen.add(name)
                details.append({
                    "name": name,
                    "ok": obj.get("success", obj.get("ok", True)),
                    "ms": obj.get("ms", obj.get("latency_ms", 0)),
                })
        except (json.JSONDecodeError, ValueError):
            pass

    # 模式2: "工具: xxx" 或 "Calling tool: xxx" 或 "Used tool: xxx"
    if not details:
        for m in re.finditer(
            r'(?:工具|Calling tool|Used tool|调用工具)[：:]\s*(\w+)', text
        ):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                details.append({"name": name, "ok": True, "ms": 0})

    # 模式3: 从 tools_called 列表推断（兜底）
    if not details:
        for m in re.finditer(r'`(\w+)`', text):
            name = m.group(1)
            # 只接受看起来像工具名的（包含下划线或以 get/run 开头）
            if ('_' in name or name.startswith(('get_', 'run_', 'list_', 'search'))) and name not in seen:
                seen.add(name)
                details.append({"name": name, "ok": True, "ms": 0})

    return details


# ═══════════════════════════════════════════════════════════════
# 链路执行器
# ═══════════════════════════════════════════════════════════════

class ChainExecutor:
    """链路执行器。

    按链路定义依次调度子 Agent，记录执行结果到数据库。

    Usage:
        executor = ChainExecutor(chain_id="evaluate+stock", stock_code="600519")
        result = executor.execute(run_agent_fn=my_run_agent)
        # result.step_results 包含每步结果
        # result.execution_id 是数据库记录 ID
    """

    def __init__(
        self,
        chain_id: str,
        stock_code: str,
        stock_name: str = "",
        user_id: int = 1,
    ):
        self.chain_id = chain_id
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.user_id = user_id
        self.chain_def = get_chain(chain_id)
        if not self.chain_def:
            raise ValueError(f"Unknown chain: {chain_id}")
        # 历史准确率权重（从评估系统加载）
        self._step_weights = self._load_step_weights()

    def _load_step_weights(self) -> Dict[str, float]:
        """从评估系统加载历史步骤准确率权重。"""
        try:
            from app.agent.chain.evaluator import get_step_weights
            weights = get_step_weights(self.chain_id)
            if weights:
                logger.info("[ChainExecutor] 加载历史权重 %s: %s", self.chain_id, weights)
            return weights
        except Exception:
            return {}

    def execute(
        self,
        run_agent_fn,
        context: Dict[str, Any] = None,
    ) -> ChainResult:
        """执行链路。

        Args:
            run_agent_fn: 调用子 Agent 的函数。
                签名: run_agent_fn(agent_name: str, message: str, context: dict) -> str
                返回 agent 的文本输出。
            context: 传递给每个 agent 的上下文（如用户原始查询等）。

        Returns:
            ChainResult 包含每步结果和最终结论。
        """
        result = ChainResult(self.chain_def, self.stock_code, self.stock_name)
        t0 = time.time()
        context = context or {}

        logger.info("[ChainExecutor] 开始执行链路 %s | 股票=%s",
                     self.chain_id, self.stock_code)

        step_outputs = []  # 收集各步输出，传给后续步骤做上下文

        for step in sorted(self.chain_def.steps, key=lambda s: s.order):
            step_result = StepResult(step)
            step_t0 = time.time()

            try:
                # 构造给子 agent 的消息
                message = self._build_step_message(step, step_outputs, context)

                # 调用子 agent
                output = run_agent_fn(step.agent, message, context)

                # 提取结论
                step_result.success = True
                step_result.conclusion = output[:500]  # 截断保存
                step_result.direction = _extract_direction(output)
                step_result.confidence = _extract_confidence(output)
                step_result.raw_output = output

                # 解析工具调用详情
                tool_details = _parse_tool_details(output)
                step_result.tools_detail = tool_details
                step_result.tools_called = [t["name"] for t in tool_details]

            except Exception as e:
                logger.warning("[ChainExecutor] 步骤 %s 失败: %s", step.name, e)
                step_result.error = str(e)
                if step.required:
                    # 必须步骤失败，终止链路
                    result.step_results.append(step_result)
                    break

            step_result.elapsed_ms = (time.time() - step_t0) * 1000
            result.step_results.append(step_result)
            step_outputs.append(step_result)

        # 汇总最终结论
        self._summarize(result)
        result.elapsed_ms = (time.time() - t0) * 1000
        result.success = any(s.success for s in result.step_results)

        # 持久化到数据库
        result.execution_id = self._save_to_db(result)

        logger.info("[ChainExecutor] 链路 %s 完成 | %s %s | %.0fms",
                     self.chain_id, result.final_direction,
                     self.stock_code, result.elapsed_ms)

        return result

    def _build_step_message(
        self,
        step: ChainStep,
        previous_outputs: List[StepResult],
        context: Dict[str, Any],
    ) -> str:
        """构造给子 agent 的消息。"""
        parts = []

        # 基本指令
        parts.append(f"请分析 {self.stock_name or self.stock_code}（{self.stock_code}）的{step.description}。")

        # 如果有前序步骤结果，注入上下文
        if previous_outputs:
            parts.append("\n前序分析结果供参考：")
            for prev in previous_outputs:
                if prev.success:
                    parts.append(f"- {prev.step.description}: {prev.conclusion[:200]}")

        # 用户原始查询
        if context.get("user_query"):
            parts.append(f"\n用户原始问题：{context['user_query']}")

        return "\n".join(parts)

    def _summarize(self, result: ChainResult):
        """汇总各步骤结论，生成最终判断。"""
        successful = [s for s in result.step_results if s.success]
        if not successful:
            result.final_direction = "neutral"
            result.final_confidence = 0
            result.summary = "所有步骤均失败，无法给出判断。"
            return

        # 加权投票：历史准确率 × 置信度
        bull_score = 0.0
        bear_score = 0.0
        total_weight = 0.0

        for s in successful:
            hist_weight = self._step_weights.get(s.step.name, 0.5)
            weight = hist_weight * s.confidence
            if s.direction == "bullish":
                bull_score += weight
            elif s.direction == "bearish":
                bear_score += weight
            total_weight += weight

        if total_weight == 0:
            result.final_direction = "neutral"
            result.final_confidence = 0
        else:
            if bull_score > bear_score:
                result.final_direction = "bullish"
                result.final_confidence = bull_score / total_weight
            elif bear_score > bull_score:
                result.final_direction = "bearish"
                result.final_confidence = bear_score / total_weight
            else:
                result.final_direction = "neutral"
                result.final_confidence = 0.5

        # 生成摘要
        direction_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
        conclusions = [f"{s.step.description}: {s.conclusion[:100]}" for s in successful]
        result.summary = (
            f"综合判断: {direction_cn.get(result.final_direction, '中性')}"
            f"（置信度 {result.final_confidence:.0%}）\n"
            + "\n".join(conclusions)
        )

    def _save_to_db(self, result: ChainResult) -> Optional[int]:
        """保存执行结果到数据库。"""
        try:
            from app.utils.db import get_db_connection

            with get_db_connection() as conn:
                cur = conn.cursor()

                # UPSERT 主记录
                cur.execute("""
                    INSERT INTO qd_chain_executions
                        (exec_date, stock_code, stock_name, chain_id, user_id,
                         final_direction, final_confidence, summary)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (exec_date, stock_code, chain_id)
                    DO UPDATE SET
                        final_direction = EXCLUDED.final_direction,
                        final_confidence = EXCLUDED.final_confidence,
                        summary = EXCLUDED.summary,
                        updated_at = NOW()
                    RETURNING id
                """, (
                    date.today(), self.stock_code, self.stock_name,
                    self.chain_id, self.user_id,
                    result.final_direction, result.final_confidence,
                    result.summary,
                ))
                execution_id = cur.fetchone()[0]

                # 删除旧步骤（覆盖更新）
                cur.execute(
                    "DELETE FROM qd_chain_steps WHERE execution_id = %s",
                    (execution_id,)
                )

                # 插入步骤详情
                for sr in result.step_results:
                    cur.execute("""
                        INSERT INTO qd_chain_steps
                            (execution_id, step_name, step_order, agent_name,
                             conclusion, direction, confidence, tools_called, tools_detail)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        execution_id, sr.step.name, sr.step.order,
                        sr.step.agent, sr.conclusion, sr.direction,
                        sr.confidence,
                        json.dumps(sr.tools_called, ensure_ascii=False),
                        json.dumps(sr.tools_detail, ensure_ascii=False),
                    ))

                conn.commit()
                logger.info(
                    "[ChainExecutor] 已保存到数据库: execution_id=%d chain=%s stock=%s steps=%d",
                    execution_id, self.chain_id, self.stock_code, len(result.step_results),
                )
                return execution_id

        except Exception as e:
            logger.error("[ChainExecutor] 保存执行记录失败: %s", e, exc_info=True)
            return None
