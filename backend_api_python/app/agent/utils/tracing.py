"""
Agent 运行轨迹记录 + 结构化存储。

统一采集：事件追加到 JSONL，finish() 时从事件流提取结构化字段写入 qd_traces。
一套采集，一路输出（qd_traces），JSONL 作为附属日志。

AGENT_JSONL_ENABLED=false    只关本地 JSONL（agent_runs.jsonl），保留 qd_traces 落库与事件采集
AGENT_TRACE_FILE=traces/agent_runs.jsonl
                            相对路径锚定到本包目录（app/agent/），不随进程 CWD 漂移
AGENT_TRACE_MAX_CHARS=12000
AGENT_EVAL_DUMP=<path>       评测侧车出口（默认不设=关闭）：每次 finish() 追加一行 JSON，
                            含 route(意图路由) / used_tools / 工具观测语料，供
                            tests/evals/runner.py --live 判分取数
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
# 2026-09-21 注记：本表兜底仅限**金融语境**的 verb（选股/分析/对比/金融数据查询）。
# 非金融任务（天气/新闻/生活查询）由意图分类器标 task_type=general，
# general 不在此表 → domain/noun 兜底 unknown → finish 防毒丸逻辑跳过入库。
# 若意图分类器仍把非金融任务标成 query，链名会错记 finance+query+stock（污染酿造候选）。
# 2026-09-25：verb→(domain,noun) 已抽到 domain_registry / tools/<domain>/domain_meta.py，
# 核心不再写死 finance/stock；新领域在 domain_meta 登记即可参与追溯归类。
from domain_registry import classify_verb as _classify_verb


def _now_ms() -> int:
    return int(time.time() * 1000)


def _max_chars() -> int:
    raw = os.getenv("AGENT_TRACE_MAX_CHARS", "12000")
    try:
        return max(100, int(raw))
    except ValueError:
        return 12000


# JSONL 落盘锚点：AGENT_TRACE_FILE 为相对路径时按**本包目录**（app/agent/）解析，
# 而不是按进程 CWD。旧实现直接用 `Path(env)`，同一份配置会因启动位置不同写到两个文件：
#   CLI（cwd=backend_api_python/）              → backend_api_python/traces/agent_runs.jsonl
#   Flask（cwd=backend_api_python/app/agent）   → app/agent/traces/agent_runs.jsonl
# 排查时很容易读到另一个文件里的陈旧 run（2026-09-20 实际踩坑：E2E 新 run 写在
# backend_api_python/traces/，核对脚本却读 app/agent/traces/ 的 6 小时前旧 run）。
_AGENT_DIR = Path(__file__).resolve().parent.parent  # utils/ → agent/
_DEFAULT_TRACE_FILE = "traces/agent_runs.jsonl"


def _trace_file_path() -> Path:
    """解析 AGENT_TRACE_FILE：绝对路径原样使用，相对路径锚定 app/agent/。"""
    raw = (os.getenv("AGENT_TRACE_FILE") or "").strip() or _DEFAULT_TRACE_FILE
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (_AGENT_DIR / p)


def _jsonl_enabled() -> bool:
    """独立控制本地 JSONL 附属日志（agent_runs.jsonl）开关。

    默认开启。设为 false 时只跳过 JSONL 文件写入，事件采集与 qd_traces
    结构化落库不受影响——用于在保留回测闭环的前提下关掉本地磁盘日志。
    """
    return os.getenv("AGENT_JSONL_ENABLED", "true").lower() not in (
        "0", "false", "no", "off",
    )


# ── 评测侧车出口（2026-09-24 提智 · E1 评测集取数通道）────────────────────
# 背景：`tests/evals/runner.py --live` 此前只能从 CLI stdout 正则回收工具名，
# 拿不到 route（意图路由）与工具观测语料 ⇒ 用例里声明的 `expect_route` 恒判失败、
# `min_grounding_rate` 恒被跳过（"声明了没接线"的判分项，基线失真）。
# 这三个量其实**已被本采集器收在内存里**（intent_verb / _tool_calls），只是没有出口：
# JSONL 只写 events、qd_traces 需 DB。这里补一个机器可读侧车——
# 设 AGENT_EVAL_DUMP 时每次 finish() 追加一行 JSON；不设则零开销、零行为变化。
_EVAL_CORPUS_MAX = 200_000  # 观测语料总长上限（grounding 溯源用，防报告失控）


def _eval_dump_path() -> Optional[Path]:
    """解析 AGENT_EVAL_DUMP：未设置返回 None（关闭），相对路径锚定 app/agent/。"""
    raw = (os.getenv("AGENT_EVAL_DUMP") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (_AGENT_DIR / p)


def _eval_route(intent_verb: str) -> str:
    """把意图动词归一到图路由口径（与 nodes.route_after_chat 的 needs_task 同义）。

    chat → 直答（finalize）；cron → 定时任务短路（不走 plan/execute）；
    其余（analysis/screen/compare/query/code/explain/general/空）→ 任务链（plan）。
    """
    v = (intent_verb or "").strip().lower()
    if v == "chat":
        return "chat"
    if v == "cron":
        return "cron"
    return "task"


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


# 方向/动作关键词的否定语境过滤（2026-09-24 提智阶段 0.10，审计 B4）：
# 旧口径"关键词命中即定方向"会把「不建议买入」判成 bullish、风险提示里的「卖出」
# 判成 bearish——标注噪声以"数据"身份进入 T+N correct 与权重/酿造原料（B4）。
# 现只做**否定过滤**（命中词紧邻否定修饰则丢弃该命中），不做反向推断
# （"不建议买" ≠ "建议卖"，宁可落 neutral 不猜方向）。
_NEGATION_RE = re.compile(r"(?:不|别|切勿|勿|暂不|并非|禁|不宜|回避)[^。！？；;\n]{0,6}$")


def _kw_first_hit(answer_lower: str, kws) -> str:
    """返回第一个**未被否定**命中的关键词（列表顺序即优先级）；无命中返回 ""。"""
    for kw in kws:
        start = 0
        while True:
            i = answer_lower.find(kw, start)
            if i < 0:
                break
            if not _NEGATION_RE.search(answer_lower[:i]):
                return kw
            start = i + 1
    return ""


def _extract_direction(answer: str) -> str:
    extracted = _extract_from_json(answer)
    d = extracted.get("direction", "")
    if d:
        return d
    answer_lower = answer.lower()
    if _kw_first_hit(answer_lower, ["买入", "buy", "看多", "bullish", "建议买"]):
        return "bullish"
    if _kw_first_hit(answer_lower, ["卖出", "sell", "看空", "bearish", "建议卖"]):
        return "bearish"
    return "neutral"


def _extract_action(answer: str) -> str:
    extracted = _extract_from_json(answer)
    a = extracted.get("action", "")
    if a:
        return a
    answer_lower = answer.lower()
    if _kw_first_hit(answer_lower, ["买入", "buy", "建议买"]):
        return "buy"
    if _kw_first_hit(answer_lower, ["卖出", "sell", "建议卖"]):
        return "sell"
    if _kw_first_hit(answer_lower, ["跳过", "skip", "回避"]):
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

def _repro_meta(tool_manifest=None) -> dict:
    """run 级可复现字段（2026-09-24 提智 0.8，agent提智 #8）：
    prompt 哈希 + 模型/温度/seed + 工具清单哈希 + 代码版本标识。

    复盘一个决策至少要能回答"这单结论是哪套配置给出的"。代码版本读 .git 文件
    （不执行 git 命令，项目红线）；拿不到就 'unknown'，不阻断。
    """
    import hashlib

    def _sha_file(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except Exception:
            return "missing"

    prompts = Path(__file__).resolve().parent.parent / "prompts"
    meta = {
        "prompt_sha": {p.name: _sha_file(p) for p in (
            prompts / "plan_system.txt", prompts / "code_agent.yaml",
            prompts / "intent_classifier.txt", prompts / "skill_brew.txt",
            prompts / "skill_revise.txt")},
        "model": os.getenv("OPENAI_MODEL", ""),
        "temperature": os.getenv("AGENT_LLM_TEMPERATURE", ""),
        "seed": os.getenv("AGENT_LLM_SEED", ""),
    }
    if tool_manifest:
        _m = ",".join(sorted(tool_manifest)).encode("utf-8", "ignore")
        meta["tools_sha"] = hashlib.sha256(_m).hexdigest()[:16]
        meta["tool_count"] = len(tool_manifest)
    # 代码版本标识：读 .git/HEAD → refs（文件读取，不执行 git 命令）
    try:
        _git = Path(__file__).resolve().parents[3] / ".git"
        _head = (_git / "HEAD").read_text(encoding="utf-8").strip()
        if _head.startswith("ref: "):
            _ref = _git / _head[5:]
            meta["code_version"] = (_ref.read_text(encoding="utf-8").strip()
                                    if _ref.exists() else _head)
        else:
            meta["code_version"] = _head
    except Exception:
        meta["code_version"] = "unknown"
    return meta


class AgentTraceRecorder:
    """统一采集器：事件追加 + finish() 时写入 qd_traces。

    生命周期：
      1. __init__(): 创建，记录 run_start
      2. record(): 各节点追加事件（plan/execute/error 等）
      3. set_stock() / set_skill() / add_tool_call(): 设置上下文
      4. finish(): 写 JSONL + 评测侧车（AGENT_EVAL_DUMP）+ 从 final_answer
         提取结构化字段写入 qd_traces
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

        # 0.9 计数器面板：从事件流汇总（原则 7：校验环自证在工作）。
        try:
            from utils.budget import empty_panel, bump
            self._panel = empty_panel()
            self._panel["runs"] = 1
            self._bump = bump
        except Exception:
            self._panel = {}
            self._bump = lambda p, k, n=1: p

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
        # 0.9 面板：把关键事件计入计数器（trace 是唯一汇总处）
        try:
            if event_type == "verify_done":
                self._bump(self._panel, "verify_seen")
                _r = str((payload or {}).get("route") or "")
                self._bump(self._panel, {"PASS": "verify_pass",
                             "REPAIR_ONCE": "verify_repair",
                             "DEGRADE": "verify_degrade"}.get(_r, "verify_seen"))
            elif event_type == "verify_skipped":
                self._bump(self._panel, "verify_skipped")
            elif event_type == "grounding_reject":
                self._bump(self._panel, "grounding_rejects", int((payload or {}).get("count", 1) or 1))
            elif event_type == "hallucination_blocked":
                self._bump(self._panel, "hallucination_blocks", int((payload or {}).get("count", 1) or 1))
            elif event_type == "budget_exceeded":
                self._bump(self._panel, "budget_exceeded")
            elif event_type == "replan":
                self._bump(self._panel, "replans")
        except Exception:
            pass

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

    def set_tool_manifest(self, names) -> None:
        """登记本 run 的工具清单（0.8 可复现字段：tools_sha 来源）。"""
        self._tool_manifest = sorted(set(names or []))

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

        Args:
            final_answer: CodeAgent 原始输出（execute_node 在格式化前经 state 传入），
                用于提取 score/direction/action 等结构化字段；None 时回退 response.content。
            status: success / error
            response: 附加响应数据（error 时取 response["error"] 落根节点）

        Returns:
            qd_traces root_id，失败返回 None

        注（2026-09-24 提智阶段 0.10，审计 A6）：错误 run 也落一条根节点
        （status='failed' + error），不进回测统计（query_pending_verify 只取 status='ok'）。
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

        # 评测侧车（AGENT_EVAL_DUMP 开启时）：成功/失败 run 都写，供评测判分取数
        self._write_eval_dump()

        # 0.9 周报：面板计数器同步一份到独立 panel.jsonl（供 scripts/weekly_panel.py 聚合）
        try:
            if getattr(self, "_panel", None):
                import os as _os
                _pf = _os.getenv("AGENT_PANEL_FILE", "traces/panel.jsonl")
                _p = Path(_pf).expanduser()
                if not _p.is_absolute():
                    _p = _AGENT_DIR / _p
                _p.parent.mkdir(parents=True, exist_ok=True)
                with _p.open("a", encoding="utf-8") as _f:
                    _f.write(json.dumps({"trace_id": self.trace_id,
                                         "session_id": self.session_id,
                                         "finished_at_ms": _now_ms(),
                                         "panel": self._panel}, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # 写 qd_traces。结构化字段提取源：优先 CodeAgent 原始输出（execute_node 在
        # LLM 格式化之前传入 finish），没有时回退 response.content —— 不再用格式化
        # 后的文本做 regex 提取，避免 LLM 版式变化污染 score/direction（审计 P1-3）。
        # 2026-09-24（提智阶段 0.10，审计 A6）：错误 run 也落根节点——旧实现只在
        # `final_answer and status == "success"` 时写库，fail() docstring 承诺的
        # "留一条带错误信息的根节点（供排查）"零兑现，排查"某天为什么没分析"DB 是空白。
        if status == "success":
            if final_answer:
                self._finished_root_id = self._write_qd_traces(final_answer)
        else:
            err = str((response or {}).get("error") or "")
            self._finished_root_id = self._write_qd_traces(
                final_answer or "", status="failed", error=err)

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
        try:
            from utils.mask import mask_obj as _mask
        except Exception:
            _mask = lambda x: x  # noqa: E731

        trace_file = _trace_file_path()
        trace_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "trace_id": self.trace_id,
            "agent_type": self.agent_type,
            "session_id": self.session_id,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": _now_ms(),
            "panel": (getattr(self, "_panel", None) or None),  # 0.9 计数器面板
            "events": self.events,
        }
        with trace_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_mask(record), ensure_ascii=False) + "\n")

    # ── 评测侧车输出（E1 评测集取数）───────────────────────────

    def _write_eval_dump(self) -> None:
        """把判分所需的三件套追加一行到 AGENT_EVAL_DUMP（未设置则直接返回）。

        字段契约（tests/evals/runner.py 按 session_id 取最后一行）：
          route       意图路由（chat/cron/task），对齐用例的 expect_route
          intent_verb 意图分类原始动词（analysis/screen/... 便于排查）
          used_tools  本次调用的工具名（去重保序，来源 _tool_calls）
          corpus      工具观测语料拼接（grounding 溯源用，超限截断）
        """
        path = _eval_dump_path()
        if path is None:
            return
        try:
            names: List[str] = []
            parts: List[str] = []
            for tc in self._tool_calls:
                name = tc.get("name") or ""
                if name and name not in names:
                    names.append(name)
                res = tc.get("result")
                if res:
                    parts.append(str(res))
            payload = {
                "trace_id": self.trace_id,
                "agent_type": self.agent_type,
                "session_id": self.session_id,
                "finished_at_ms": _now_ms(),
                "route": _eval_route(self.intent_verb),
                "intent_verb": self.intent_verb,
                "intent_domain": self.domain,
                "used_tools": names,
                "corpus": "\n".join(parts)[:_EVAL_CORPUS_MAX],
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            # 附属出口失败不影响主链，但必须发声（不许静默降级）
            logger.warning("[Trace] 评测侧车写入失败 %s: %s", path, e)

    # ── qd_traces 输出 ────────────────────────────────────────

    def _write_qd_traces(self, final_answer: str, status: str = "ok",
                         error: str = "") -> Optional[int]:
        """从 final_answer + 事件流提取结构化字段，构建 EvalNode 写入 qd_traces。

        status/error（2026-09-24，审计 A6）：成功 run 默认 status='ok' 走全量结构化提取；
        错误 run 传 status='failed' + error，只写根节点留痕（决策字段留空，
        不参与 T+N 回测与权重聚合——query_pending_verify 只取 status='ok'）。
        """
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
            _cls = _classify_verb(self.intent_verb)
            if not self.domain and _cls:
                self.domain = _cls.get("domain", "")
            if not self.intent_noun and _cls:
                self.intent_noun = _cls.get("noun", "")
            chain_name = f"{self.domain or 'unknown'}+{self.intent_verb or 'unknown'}+{self.intent_noun or 'unknown'}"

            # 跳过大势/筛选类无标的空链：无 stock_code 且链仍含 unknown（无法归类）→
            # 不参与决策树/回测统计，避免 unknown+screen+unknown 这类毒丸数据入库。
            # 注：空 code 记录本就无法逐股回测，store.query_pending_verify 已将其判为
            # 永久毒丸——此处写入前直接拦截更干净（归类成功则仍写入，符合"归类回测"诉求）。
            # 错误 run 不走毒丸拦截：留痕本身就是目的（status='failed' 已被回测查询排除），
            # 拦掉就回到"排查时 DB 空白"的老问题（审计 A6）。
            if status == "ok" and not stock_code and "unknown" in chain_name:
                logger.info("[Trace] 跳过写入决策树: 链不可归类且无标的 chain=%s (不参与回测)", chain_name)
                return None
            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=chain_name,
                exec_date=date.today(),
                stock_code=stock_code,
                stock_name=stock_name,
                input_params={"user_query": self.user_input,
                              "repro": _repro_meta(getattr(self, "_tool_manifest", None))},
                analysis=final_answer[:2000],
                # §8.3：run 级元数据落库。此前这五列要么有 DDL 无写入（恒空），
                # 要么（plan）压根没有 DDL —— 而 store.py 的 INSERT 一直在写 plan，
                # 缺列会让整条 INSERT 报错 ⇒ qd_traces 一条都写不进去。
                plan=self._plan,
                session_id=self.session_id,
                user_query=self.user_input,
                model=self._model,
                total_tokens=self._total_tokens or None,
                # 成功 run 全量提取；错误 run 决策字段留空（不污染权重/酿造原料）。
                score=_extract_score(final_answer) if status == "ok" else None,
                direction=_extract_direction(final_answer) if status == "ok" else "",
                action=_extract_action(final_answer) if status == "ok" else "",
                signal=_extract_signal(final_answer) if status == "ok" else "",
                confidence=_extract_confidence(final_answer) if status == "ok" else 0.0,
                timeframe=_extract_timeframe(final_answer) if status == "ok" else "",
                status=status,
                error=error,
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
