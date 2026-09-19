"""
Agent 运行轨迹记录 + 结构化存储。

统一采集：事件追加到 JSONL，finish() 时从事件流提取结构化字段写入 qd_traces。
一套采集，一路输出（qd_traces），JSONL 作为附属日志。

AGENT_JSONL_ENABLED=false    只关本地 JSONL（agent_runs.jsonl），保留 qd_traces 落库与事件采集
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

# 意图动词 → 兜底归类（2026-09-14）。
# domain/noun 当前无独立来源，set_intent 只接 verb，缺省位恒落 unknown，
# 导致链名退化成 unknown+verb+unknown：既无法按链聚合/回测，又让 unknown+screen+unknown
# 这类空链（无 stock_code）污染决策树。这里按 verb 兜底归类到已知域，使 stock 任务
# 可被正确归类（finance 域，参与回测统计）；归类失败的（chat/general/cron 等）仍落 unknown。
_VERB_CLASSIFY = {
    "screen":   {"domain": "finance", "noun": "stock"},
    "analysis": {"domain": "finance", "noun": "stock"},
    "compare":  {"domain": "finance", "noun": "stock"},
    "query":    {"domain": "finance", "noun": "stock"},
    "code":     {"domain": "finance", "noun": "strategy"},
    "explain":  {"domain": "finance", "noun": "indicator"},
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _max_chars() -> int:
    raw = os.getenv("AGENT_TRACE_MAX_CHARS", "12000")
    try:
        return max(100, int(raw))
    except ValueError:
        return 12000


def _jsonl_enabled() -> bool:
    """独立控制本地 JSONL 附属日志（agent_runs.jsonl）开关。

    默认开启。设为 false 时只跳过 JSONL 文件写入，事件采集与 qd_traces
    结构化落库不受影响——用于在保留回测闭环的前提下关掉本地磁盘日志。
    """
    return os.getenv("AGENT_JSONL_ENABLED", "true").lower() not in (
        "0", "false", "no", "off",
    )


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

        # finish 幂等状态
        self._finished: bool = False
        self._finished_root_id: Optional[int] = None
        # 原始 CodeAgent 输出缓存（LLM 格式化前），供结构化字段提取
        self._raw_agent_output: str = ""

        # run 级运行元数据（2026-09-17，设计文档 §8.3）：qd_traces 的
        # session_id / user_query / model / total_tokens / plan 五列此前要么缺 DDL、
        # 要么有 DDL 无写入，这里负责采集，finish() 时随根节点落库。
        self._model: str = ""
        self._total_tokens: int = 0
        self._plan: str = ""

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
        payload = payload or {}
        # 顺路汇总 run 级元数据（§8.3）：这些信息散落在事件里，此前无人采集落库。
        # 多批次执行会记多次 token_usage（每批一次），累加才是整轮真实消耗。
        if event_type == "token_usage":
            try:
                self._total_tokens += int(payload.get("total") or 0)
            except (TypeError, ValueError):
                pass
        _plan = payload.get("agent_plan")
        if isinstance(_plan, str) and _plan:
            self._plan = _plan
        _model = payload.get("model")
        if isinstance(_model, str) and _model and not self._model:
            self._model = _model
        self.events.append({
            "type": event_type,
            "timestamp_ms": _now_ms(),
            "elapsed_ms": _now_ms() - self.started_at_ms,
            "payload": _truncate(payload),
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

    def set_intent(self, domain: str = "", verb: str = "", noun: str = ""):
        """记录意图三元组，供 chain_name（feedback 按链匹配、按链统计）使用。

        旧版三元组无数据源，根节点 name 恒为 "agent"，按链聚合全部失效（审计 P1-5）。
        当前 chat_node 的意图分类只产出 task_type（≈verb 位），domain/noun 暂无
        独立来源，先接已有的，缺省位落 unknown。
        """
        if domain:
            self.domain = domain
        if verb:
            self.intent_verb = verb
        if noun:
            self.intent_noun = noun

    def set_model(self, model: str):
        """记录本次 run 使用的模型名（§8.3）。

        model 没有事件源（没有任何节点把模型名写进 trace 事件），只能由调用方在
        收尾处显式设置——finalize 的 ctx.llm.model 即是。
        """
        if model:
            self._model = str(model)

    def set_plan(self, plan: str):
        """记录 smolagents 最终规划（§8.3）。事件里已带 agent_plan 时可不必调用。"""
        if plan and not self._plan:
            self._plan = str(plan)

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

    def finish(self, final_answer: Optional[str] = None, status: str = "success",
               response: Optional[dict] = None) -> Optional[int]:
        """结束追踪：写 JSONL + 写 qd_traces。

        final_answer: CodeAgent 原始输出（execute_node 在格式化前经 state 传入），
        用于提取 score/direction/action 等结构化字段；None 时回退 response.content。
        """
        """结束追踪：写 JSONL + 写 qd_traces。

        Args:
            final_answer: CodeAgent 原始输出（用于提取结构化字段）
            status: success / error
            response: 附加响应数据

        Returns:
            qd_traces root_id，失败返回 None
        """
        # 幂等保护：finalize_node 与 _chat_plan_graph 会对同一次 run 各调一次 finish，
        # 重复执行会双写 JSONL + 双写 qd_traces（审计 P1-3）。第二次调用直接返回首写结果。
        if self._finished:
            return self._finished_root_id
        self._finished = True
        self._finished_root_id = None

        self.record("run_end", {"status": status, "response": response or {}})

        # 写 JSONL（受独立开关 AGENT_JSONL_ENABLED 控制；qd_traces 落库不受影响）
        root_id = None
        if _jsonl_enabled():
            self._write_jsonl()

        # 写 qd_traces。结构化字段提取源：优先 CodeAgent 原始输出（execute_node 在
        # LLM 格式化之前传入 finish），没有时回退 response.content —— 不再用格式化
        # 后的文本做 regex 提取，避免 LLM 版式变化污染 score/direction（审计 P1-3）。
        if final_answer and status == "success":
            self._finished_root_id = self._write_qd_traces(final_answer)

        return self._finished_root_id

    def fail(self, error: Exception):
        """异常路径收口：与 finish() 走同一条链路（幂等 + 双写一致）。

        旧实现只写 JSONL、不写 qd_traces——调用方（_chat_plan_graph except 分支）
        与 finalize_node 的 finish() 是二选一执行，谁后执行谁决定落库形态。
        现在 run_error 事件照记，然后统一走 finish(status="error")：
        qd_traces 会留一条带错误信息的根节点（供排查），但不参与回测统计。
        """
        self.record("run_error", {
            "error_type": type(error).__name__,
            "error": str(error),
        })
        self.finish(
            status="error",
            response={"error": str(error)[:500]},
        )

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
            # chain_name 用真实 intent 三元组；缺省段填 unknown 而非统一 "agent"，
            # 否则按 chain 聚合/匹配（负面反馈、统计）全部失效（审计 P1-5）。
            # 归类优先（2026-09-14）：domain/noun 无独立来源、恒落 unknown，导致
            # unknown+screen+unknown 等空链既无法按链聚合/回测，又污染决策树。按 verb
            # 兜底归类（选股/分析等 stock 任务归入 finance 域），使其可被正确归类。
            # 2026-09-19（重设计 V1）：domain/noun 兜底解耦——domain 可由 plan_node
            # 补写（set_intent(domain=...)），noun 缺失时无论 domain 来源如何都按
            # verb 归类补齐；否则 domain 有值会抑制 noun 兜底，链名残留 unknown 段。
            _cls = _VERB_CLASSIFY.get(self.intent_verb)
            if not self.domain and _cls:
                self.domain = _cls.get("domain", "")
            if not self.intent_noun and _cls:
                self.intent_noun = _cls.get("noun", "")
            chain_name = f"{self.domain or 'unknown'}+{self.intent_verb or 'unknown'}+{self.intent_noun or 'unknown'}"

            # 跳过大势/筛选类无标的空链：无 stock_code 且链仍含 unknown（无法归类）→
            # 不参与决策树/回测统计，避免 unknown+screen+unknown 这类毒丸数据入库。
            # 注：空 code 记录本就无法逐股回测，store.query_pending_verify 已将其判为
            # 永久毒丸——此处写入前直接拦截更干净（归类成功则仍写入，符合"归类回测"诉求）。
            if not stock_code and "unknown" in chain_name:
                logger.info("[Trace] 跳过写入决策树: 链不可归类且无标的 chain=%s (不参与回测)", chain_name)
                return None
            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=chain_name,
                exec_date=date.today(),
                stock_code=stock_code,
                stock_name=stock_name,
                input_params={"user_query": self.user_input},
                analysis=final_answer[:2000],
                # §8.3：run 级元数据落库。此前这五列要么有 DDL 无写入（恒空），
                # 要么（plan）压根没有 DDL —— 而 store.py 的 INSERT 一直在写 plan，
                # 缺列会让整条 INSERT 报错 ⇒ qd_traces 一条都写不进去。
                plan=self._plan,
                session_id=self.session_id,
                user_query=self.user_input,
                model=self._model,
                total_tokens=self._total_tokens or None,
                score=_extract_score(final_answer),
                direction=_extract_direction(final_answer),
                action=_extract_action(final_answer),
                signal=_extract_signal(final_answer),
                confidence=_extract_confidence(final_answer),
                timeframe=_extract_timeframe(final_answer),
                elapsed_ms=_now_ms() - self.started_at_ms,
            )

            # skill 子节点。
            # direction/score 等评估字段必须回填：evaluator 的权重聚合按
            # layer='skill' AND correct IS NOT NULL 扫描，而 update_skill_verify
            # 对 direction 为空的行直接跳过 → skill 行 correct 永远 NULL，
            # qd_agent_weights 永远不会被更新（审计 P0-1 断点B）。
            # 无独立技能报告时继承根节点决策（同源、可解释），与根节点自证等价。
            if self._skill_name:
                skill_node = EvalNode(
                    layer=Layer.SKILL.value,
                    name=self._skill_name,
                    direction=root.direction,
                    score=root.score,
                    action=root.action,
                    signal=root.signal,
                    confidence=root.confidence,
                    timeframe=root.timeframe,
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
