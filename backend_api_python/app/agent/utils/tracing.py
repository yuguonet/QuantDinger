"""
Agent 运行轨迹记录 + 结构化存储。

统一采集：事件追加到 JSONL，finish() 时从事件流提取结构化字段写入 qd_traces。
一套采集，一路输出（qd_traces），JSONL 作为附属日志。

AGENT_TRACE_ENABLED=false   关闭
AGENT_TRACE_FILE=traces/agent_runs.jsonl
AGENT_TRACE_MAX_CHARS=12000
"""

import json
import logging
import os
import re
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm.base import ChatMessage, LLMResponse

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _trace_enabled() -> bool:
    return os.getenv("AGENT_TRACE_ENABLED", "true").lower() not in (
        "0", "false", "no", "off",
    )


def _max_chars() -> int:
    raw = os.getenv("AGENT_TRACE_MAX_CHARS", "12000")
    try:
        return max(100, int(raw))
    except ValueError:
        return 12000


def _truncate(value: Any, limit: Optional[int] = None) -> Any:
    """递归裁剪过长字段，避免 trace 文件失控。"""
    limit = limit or _max_chars()
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"\n...[truncated, original_length={len(value)}]"
    if isinstance(value, list):
        return [_truncate(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _truncate(item, limit) for key, item in value.items()}
    return value


def messages_to_dict(messages: list[ChatMessage]) -> list[dict]:
    return [msg.to_dict() for msg in messages]


def llm_response_to_dict(response: LLMResponse) -> dict:
    return {
        "content": response.content,
        "tool_calls": response.tool_calls,
        "model": response.model,
        "finish_reason": response.finish_reason,
        "tokens_used": response.tokens_used,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "metadata": response.metadata,
    }


# ═══════════════════════════════════════════════════════════════
#  结构化字段提取（从 final_answer 提取 score/direction/action 等）
# ═══════════════════════════════════════════════════════════════

def _extract_from_json(answer: str) -> dict:
    """从 Agent 输出的 JSON 中提取字段。返回空 dict 表示未命中。"""
    from utils.json_parser import extract_decision
    result = extract_decision(answer)
    return result if result else {}


def _extract_score(answer: str) -> Optional[float]:
    extracted = _extract_from_json(answer)
    score = extracted.get("score")
    if score is not None:
        return max(0, min(100, float(score)))
    m = re.search(r'(?:评分|score)[：:\s]*(\d+(?:\.\d+)?)', answer, re.I)
    if m:
        return max(0, min(100, float(m.group(1))))
    return None


def _extract_direction(answer: str) -> str:
    extracted = _extract_from_json(answer)
    d = extracted.get("direction", "")
    if d:
        return d
    answer_lower = answer.lower()
    if any(kw in answer_lower for kw in ["买入", "buy", "看多", "bullish", "建议买"]):
        return "bullish"
    if any(kw in answer_lower for kw in ["卖出", "sell", "看空", "bearish", "建议卖"]):
        return "bearish"
    return "neutral"


def _extract_action(answer: str) -> str:
    extracted = _extract_from_json(answer)
    a = extracted.get("action", "")
    if a:
        return a
    answer_lower = answer.lower()
    if any(kw in answer_lower for kw in ["买入", "buy", "建议买"]):
        return "buy"
    if any(kw in answer_lower for kw in ["卖出", "sell", "建议卖"]):
        return "sell"
    if any(kw in answer_lower for kw in ["跳过", "skip", "回避"]):
        return "skip"
    return "hold"


def _extract_signal(answer: str) -> str:
    extracted = _extract_from_json(answer)
    s = extracted.get("signal", "")
    if s:
        return s
    m = re.search(r'(?:signal|信号)[：:\s]*(.+?)(?:\n|$)', answer, re.I)
    return m.group(1).strip()[:200] if m else ""


def _extract_confidence(answer: str) -> float:
    extracted = _extract_from_json(answer)
    c = extracted.get("confidence")
    if isinstance(c, (int, float)):
        return max(0.0, min(1.0, float(c)))
    if isinstance(c, str):
        return {"high": 0.8, "medium": 0.5, "low": 0.3}.get(c, 0.5)
    answer_lower = answer.lower()
    if any(kw in answer_lower for kw in ["高度确信", "非常确定", "high confidence"]):
        return 0.8
    if any(kw in answer_lower for kw in ["不太确定", "有风险", "low confidence"]):
        return 0.3
    return 0.5


def _extract_timeframe(answer: str) -> str:
    extracted = _extract_from_json(answer)
    return extracted.get("timeframe", "")


def _extract_stock_from_answer(answer: str) -> tuple[str, str]:
    """从 final_answer 中提取 stock_code 和 stock_name。"""
    extracted = _extract_from_json(answer)
    return extracted.get("stock_code", ""), extracted.get("stock_name", "")


# ═══════════════════════════════════════════════════════════════
#  AgentTraceRecorder
# ═══════════════════════════════════════════════════════════════

class AgentTraceRecorder:
    """统一采集器：事件追加 + finish() 时写入 qd_traces。

    生命周期：
      1. __init__(): 创建，记录 run_start
      2. record(): 各节点追加事件（plan/execute/error 等）
      3. set_stock() / set_skill() / add_tool_call(): 设置上下文
      4. finish(): 写 JSONL + 从 final_answer 提取结构化字段写入 qd_traces
    """

    def __init__(
        self,
        agent_type: str,
        session_id: str,
        user_input: str,
        metadata: Optional[dict] = None,
    ):
        self.enabled = _trace_enabled()
        self.trace_id = str(uuid.uuid4())
        self.agent_type = agent_type
        self.session_id = session_id
        self.user_input = user_input
        self.started_at_ms = _now_ms()
        self.events: list[dict] = []
        self.metadata = metadata or {}

        # ── 上下文（由节点通过 set_* / add_* 设置）──
        self.stock_code: str = ""
        self.stock_name: str = ""
        self.domain: str = ""
        self.intent_verb: str = ""
        self.intent_noun: str = ""
        self._skill_name: str = ""
        self._tool_calls: List[Dict[str, Any]] = []  # [{name, args, result, elapsed_ms, error}]

        if self.enabled:
            self.record(
                "run_start",
                {
                    "agent_type": agent_type,
                    "session_id": session_id,
                    "user_input": user_input,
                    "metadata": self.metadata,
                },
            )

    # ── 事件追加 ──────────────────────────────────────────────

    def record(self, event_type: str, payload: Optional[dict] = None):
        if not self.enabled:
            return
        self.events.append({
            "type": event_type,
            "timestamp_ms": _now_ms(),
            "elapsed_ms": _now_ms() - self.started_at_ms,
            "payload": _truncate(payload or {}),
        })

    # ── 上下文设置（由 nodes 调用）────────────────────────────

    def set_stock(self, code: str = "", name: str = ""):
        """设置标的信息。"""
        if code:
            self.stock_code = str(code).strip()
        if name:
            self.stock_name = str(name).strip()

    def set_skill(self, skill_name: str):
        """标记当前执行的技能。"""
        self._skill_name = skill_name

    def add_tool_call(self, tool_name: str, arguments: dict = None,
                      result: Any = None, elapsed_ms: float = 0,
                      error: str = ""):
        """记录一次工具调用。"""
        self._tool_calls.append({
            "name": tool_name,
            "args": arguments or {},
            "result": str(result)[:2000] if result else "",
            "elapsed_ms": elapsed_ms,
            "error": error or "",
        })

    # ── 结束 + 双写 ──────────────────────────────────────────

    def finish(self, final_answer: str = "", status: str = "success",
               response: Optional[dict] = None) -> Optional[int]:
        """结束追踪：写 JSONL + 写 qd_traces。

        Args:
            final_answer: CodeAgent 原始输出（用于提取结构化字段）
            status: success / error
            response: 附加响应数据

        Returns:
            qd_traces root_id，失败返回 None
        """
        self.record("run_end", {"status": status, "response": response or {}})

        # 写 JSONL
        root_id = None
        if self.enabled:
            self._write_jsonl()

        # 写 qd_traces
        if final_answer and status == "success":
            root_id = self._write_qd_traces(final_answer)

        return root_id

    def fail(self, error: Exception):
        self.record("run_error", {
            "error_type": type(error).__name__,
            "error": str(error),
        })
        self._write_jsonl()

    # ── JSONL 输出 ────────────────────────────────────────────

    def _write_jsonl(self):
        trace_file = Path(os.getenv("AGENT_TRACE_FILE", "traces/agent_runs.jsonl"))
        trace_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "trace_id": self.trace_id,
            "agent_type": self.agent_type,
            "session_id": self.session_id,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": _now_ms(),
            "events": self.events,
        }
        with trace_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ── qd_traces 输出 ────────────────────────────────────────

    def _write_qd_traces(self, final_answer: str) -> Optional[int]:
        """从 final_answer + 事件流提取结构化字段，构建 EvalNode 写入 qd_traces。"""
        try:
            from chain.schema import EvalNode, Layer

            # 提取 stock_code（优先上下文，降级从答案提取）
            stock_code = self.stock_code
            stock_name = self.stock_name
            if not stock_code:
                stock_code, stock_name = _extract_stock_from_answer(final_answer)

            # 从工具调用中尝试提取 stock_code
            if not stock_code:
                for tc in self._tool_calls:
                    for key in ("stock_code", "stock", "symbol", "code", "codes"):
                        val = tc.get("args", {}).get(key, "")
                        if val:
                            stock_code = str(val).split(",")[0].strip()
                            break
                    if stock_code:
                        break

            # 构建根节点
            chain_name = f"{self.domain}+{self.intent_verb}+{self.intent_noun}" if self.intent_verb else "agent"
            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=chain_name,
                exec_date=date.today(),
                stock_code=stock_code,
                stock_name=stock_name,
                input_params={"user_query": self.user_input},
                analysis=final_answer[:2000],
                score=_extract_score(final_answer),
                direction=_extract_direction(final_answer),
                action=_extract_action(final_answer),
                signal=_extract_signal(final_answer),
                confidence=_extract_confidence(final_answer),
                timeframe=_extract_timeframe(final_answer),
                elapsed_ms=_now_ms() - self.started_at_ms,
            )

            # skill 子节点
            if self._skill_name:
                skill_node = EvalNode(
                    layer=Layer.SKILL.value,
                    name=self._skill_name,
                    tools_called=[tc["name"] for tc in self._tool_calls],
                )
                # skill 下的 tool 子节点
                for tc in self._tool_calls:
                    tool_node = EvalNode(
                        layer=Layer.TOOL.value,
                        name=tc["name"],
                        input_params=tc["args"],
                        output_data={"result": tc["result"]} if tc["result"] else {},
                        elapsed_ms=tc["elapsed_ms"],
                        status="failed" if tc["error"] else "ok",
                        error=tc["error"],
                    )
                    skill_node.add_child(tool_node)
                root.add_child(skill_node)
            else:
                # 无 skill，tool 直接挂根节点
                for tc in self._tool_calls:
                    tool_node = EvalNode(
                        layer=Layer.TOOL.value,
                        name=tc["name"],
                        input_params=tc["args"],
                        output_data={"result": tc["result"]} if tc["result"] else {},
                        elapsed_ms=tc["elapsed_ms"],
                        status="failed" if tc["error"] else "ok",
                        error=tc["error"],
                    )
                    root.add_child(tool_node)

            root.tools_called = [tc["name"] for tc in self._tool_calls]

            # 写入 qd_traces
            from chain import store
            execution_id = store.save_tree(root)
            if execution_id:
                root.id = execution_id
                logger.info("[Trace] qd_traces 写入: root_id=%d stock=%s chain=%s children=%d",
                            execution_id, stock_code, chain_name, len(root.children))
            return execution_id

        except Exception as e:
            logger.warning("[Trace] qd_traces 写入失败: %s", e)
            return None
