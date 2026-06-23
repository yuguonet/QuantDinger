# -*- coding: utf-8 -*-
"""
Agent Evaluator — 在线执行质量评估 + 工具链学习。

⚠️ 注意区分：
  本文件（evaluator.py）   — 在线评估，每次 agent.run() 后立即执行
  chain/evaluator.py       — 离线评估，T+N 验证决策准确性，定时运行

触发时机：
  agent.py → _post_evaluate() → evaluate() + learn_from_execution()
  每次 Agent 执行完毕后自动调用，不消耗 Agent 步数。

评估逻辑（纯规则，<1ms，无 LLM 调用）：
  1. has_final_answer → verdict = success / failure
  2. tool_success_rate（从 tool_calls_log 直接算）

闭环动作：
  success + all_phases_completed → _writeback_chain() — 更新 tool_chains.json
  failure → _record_failure()  — 写入 tool_chain_failures.json（下次避开）

公开接口：
  evaluate(agent_result, tool_chain, verb, noun) → EvalResult
  learn_from_execution(eval_result, verb, noun) → None
  get_failure_record(verb, noun) → Optional[Dict]
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 存储路径 ─────────────────────────────────────────────────
_ROUTER_DIR = pathlib.Path(__file__).resolve().parent / "router"
_FAILURES_PATH = _ROUTER_DIR / "tool_chain_failures.json"


# ═══════════════════════════════════════════════════════════════
# 1. 评估结果
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvalResult:
    """评估结果。"""
    verdict: str = "unknown"          # "success" | "failure"
    tool_success_rate: float = 0.0    # 工具成功率 (0~1)
    has_final_answer: bool = False    # 是否有 final_answer
    actual_tools: List[str] = field(default_factory=list)  # 实际调用的工具
    chain_tools: List[str] = field(default_factory=list)   # chain 中的工具
    steps_taken: int = 0              # 实际步数
    _tool_calls_log: List[Dict] = field(default_factory=list)  # 原始工具调用日志


# ═══════════════════════════════════════════════════════════════
# 2. 核心评估逻辑
# ═══════════════════════════════════════════════════════════════

def evaluate(
    agent_result,
    tool_chain: List[Dict[str, str]],
    verb: str = "",
    noun: str = "",
    domain: str = "",
) -> EvalResult:
    """评估 agent 执行结果。

    简化逻辑：只看 has_final_answer 和 tool_success_rate。
    domain 参数保留兼容，不再做领域专用评估。

    Args:
        agent_result: AgentResult 实例（来自 _AgentExecutor.chat/chat_stream）
        tool_chain: 本次使用的工具链 [{"tool": "xxx", "desc": "xxx"}, ...]
        verb: 动作类别
        noun: 对象类别

    Returns:
        EvalResult
    """
    result = EvalResult()
    result.chain_tools = [s["tool"] for s in tool_chain] if tool_chain else []

    # ── 从 tool_calls_log 提取信息 ───────────────────────────
    actual_tools = []
    tool_successes = 0
    tool_failures = 0

    if agent_result.tool_calls_log:
        for tc in agent_result.tool_calls_log:
            tool_name = tc.get("tool", "")
            if tool_name and tool_name != "final_answer":
                actual_tools.append(tool_name)
                if tc.get("success", True):
                    tool_successes += 1
                else:
                    tool_failures += 1

    result.actual_tools = actual_tools
    result.steps_taken = agent_result.total_steps or 0
    result.has_final_answer = bool(agent_result.content and agent_result.content.strip())
    result._tool_calls_log = list(agent_result.tool_calls_log or [])

    # ── 工具成功率 ───────────────────────────────────────────
    total_calls = tool_successes + tool_failures
    if total_calls > 0:
        result.tool_success_rate = tool_successes / total_calls
    else:
        result.tool_success_rate = 1.0

    # ── 判定：有 final_answer 就是 success ───────────────────
    result.verdict = "success" if result.has_final_answer else "failure"

    logger.info(
        "[Eval] %s+%s verdict=%s steps=%d tools=%d/%d tool_rate=%.1f%%",
        verb, noun, result.verdict, result.steps_taken,
        tool_successes, total_calls, result.tool_success_rate * 100,
    )
    return result


# ═══════════════════════════════════════════════════════════════
# 3. 闭环动作：写回链 / 记录失败
# ═══════════════════════════════════════════════════════════════

def learn_from_execution(
    eval_result: EvalResult,
    verb: str,
    noun: str,
    chain_def=None,
    all_phases_completed=None,
):
    """根据评估结果执行闭环动作。

    - 每次执行 → 更新统计（avg_steps, executions, success_rate）
    - success + all_phases_completed → 写回 tool_chains.json
    - failure → 记录到 tool_chain_failures.json

    Args:
        all_phases_completed: None=不适用（自由执行），True=全部phase完成，False=phase被中断
    """
    if not verb or not noun:
        logger.debug("[Learn] verb 或 noun 为空，跳过学习: verb=%s noun=%s", verb, noun)
        return

    # ── 每次都更新统计 ──
    try:
        from app.agent.chain.tool_chains import update_chain_stats
        update_chain_stats(
            verb=verb,
            noun=noun,
            steps_taken=eval_result.steps_taken or 0,
            success=(eval_result.verdict == "success"),
        )
    except Exception as e:
        logger.warning("[Learn] 统计更新失败: %s", e)

    if eval_result.verdict == "success":
        if all_phases_completed is False:
            logger.info(
                "[Learn] %s+%s: 拦截 — 未完成全部 phase（被错误退出），不写入 chain",
                verb, noun,
            )
            return
        _writeback_chain(eval_result, verb, noun, chain_def=chain_def)
    elif eval_result.verdict == "failure":
        _record_failure(eval_result, verb, noun)


def _writeback_chain(eval_result: EvalResult, verb: str, noun: str, chain_def=None):
    """成功 → 写回 tool_chains.json（带质量门）。

    新格式：存储完整 plan 结构（phases + progressive + context），
    下次 _try_chain 直接命中，跳过 Planner LLM #2。
    """
    from app.agent.chain.tool_chains import get_tool_chain, save_tool_chain, save_chain_plan, get_chain_stats

    if not eval_result.actual_tools:
        return

    actual_set = set(eval_result.actual_tools)
    chain_set = set(eval_result.chain_tools)

    # agent 完全遵循 chain，chain 已验证有效，不需要改
    if chain_set and actual_set >= chain_set:
        logger.info(
            "[Learn] %s+%s: chain 已验证有效，保持不变", verb, noun,
        )
        return

    # ── 质量门 ──
    MAX_STEPS_PER_PHASE = 5

    # 任意阶段 phase 步数 > 5 → 拦截
    if chain_def and chain_def.steps:
        for step in chain_def.steps:
            if hasattr(step, 'steps_taken') and step.steps_taken and step.steps_taken > MAX_STEPS_PER_PHASE:
                logger.info("[Learn] %s+%s: 拦截 — phase %d 步数 %d > %d", verb, noun, step.order, step.steps_taken, MAX_STEPS_PER_PHASE)
                return

    # 工具成功率 < 50% → 拦截（工具不可靠）
    if eval_result.tool_success_rate < 0.5:
        logger.info("[Learn] %s+%s: 拦截 — 工具成功率 %.1f%% < 50%%", verb, noun, eval_result.tool_success_rate * 100)
        return

    # 旧链执行 ≥ 10 次且成功率 ≥ 80% → 拦截（旧链已验证，不轻易替换）
    if chain_set:
        stats = get_chain_stats(verb, noun)
        executions = stats.get("executions", 0)
        success_rate = stats.get("success_rate", 0.0)
        if executions >= 10 and success_rate >= 0.8:
            logger.info(
                "[Learn] %s+%s: 拦截 — 旧链已验证（%d次, 成功率%.1f%%）",
                verb, noun, executions, success_rate * 100,
            )
            return

    # ── 质量门全部通过，写入 ──

    # 优先存储完整 plan（从 chain_def 重建）
    if chain_def and chain_def.steps:
        plan = {
            "phases": [
                {
                    "skill": step.agent,
                    "description": step.description or "",
                    "tools": [],
                    "rules": step.rules or "",
                }
                for step in sorted(chain_def.steps, key=lambda s: s.order)
            ],
            "progressive": getattr(chain_def, "progressive", True),
            "context": getattr(chain_def, "context", {}) or {},
            "reasoning": chain_def.description or "",
        }
        save_chain_plan(verb, noun, plan)
        logger.info(
            "[Learn] %s+%s: 保存完整 plan → %d phases",
            verb, noun, len(plan["phases"]),
        )
    else:
        # 降级：无 chain_def 时存旧格式工具列表
        seen = set()
        new_chain = []
        for t in eval_result.actual_tools:
            if t not in seen:
                seen.add(t)
                new_chain.append({"tool": t, "desc": ""})
        if new_chain:
            save_tool_chain(verb, noun, new_chain)
            logger.info(
                "[Learn] %s+%s: 学习新链（旧格式） → %s",
                verb, noun, [s["tool"] for s in new_chain],
            )


def _record_failure(eval_result: EvalResult, verb: str, noun: str):
    """失败 → 记录到 failures.json。"""
    data = _load_failures()
    key = f"{verb}+{noun}"

    failed_tools = []
    if eval_result._tool_calls_log:
        for tc in eval_result._tool_calls_log:
            if not tc.get("success", True):
                failed_tools.append(tc.get("tool", ""))

    entry = data.get(key, {"chain": eval_result.actual_tools, "fail_count": 0, "failed_tools": []})
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    entry["last_fail_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["chain"] = eval_result.actual_tools
    entry["failed_tools"] = failed_tools or entry.get("failed_tools", [])
    data[key] = entry

    _save_failures(data)
    logger.info(
        "[Learn] %s+%s: 记录失败 (count=%d, failed=%s)",
        verb, noun, entry["fail_count"],
        failed_tools or "unknown",
    )


def get_failure_record(verb: str, noun: str) -> Optional[Dict]:
    """查询某场景的失败记录（供路由决策参考）。"""
    data = _load_failures()
    return data.get(f"{verb}+{noun}")


def _load_failures() -> Dict:
    if not _FAILURES_PATH.exists():
        return {}
    try:
        return json.loads(_FAILURES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_failures(data: Dict):
    _FAILURES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
