# -*- coding: utf-8 -*-
"""
Agent Evaluator — 工具链学习闭环。

⚠️ 注意区分：
  本文件（evaluator.py）   — 在线学习，每次 agent.run() 后立即执行
  chain/evaluator.py       — 离线评估，T+N 验证决策准确性，定时运行

触发时机：
  agent.py → _post_evaluate() → learn_from_execution()

闭环动作：
  agent_result.success + all_phases_completed → _writeback_chain() → tool_chains.json
  !agent_result.success → _record_failure() → tool_chain_failures.json

公开接口：
  learn_from_execution(agent_result, verb, noun, chain_def, all_phases_completed) → None
  get_failure_record(verb, noun) → Optional[Dict]
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 存储路径 ─────────────────────────────────────────────────
_ROUTER_DIR = pathlib.Path(__file__).resolve().parent / "router"
_FAILURES_PATH = _ROUTER_DIR / "tool_chain_failures.json"


# ═══════════════════════════════════════════════════════════════
# 学习闭环
# ═══════════════════════════════════════════════════════════════

def learn_from_execution(
    agent_result,
    verb: str,
    noun: str,
    chain_def=None,
    all_phases_completed=None,
):
    """根据 agent 执行结果执行学习闭环。

    - 每次执行 → 更新统计（avg_steps, executions, success_rate）
    - success + all_phases_completed → 写回 tool_chains.json
    - failure → 记录到 tool_chain_failures.json

    Args:
        agent_result: AgentResult 实例（success, content, tool_calls_log, total_steps）
        verb: 意图动词
        noun: 意图对象
        chain_def: ChainDef 对象（可选）
        all_phases_completed: None=不适用（自由执行），True=全部phase完成，False=phase被中断
    """
    if not verb or not noun:
        logger.debug("[Learn] verb 或 noun 为空，跳过学习: verb=%s noun=%s", verb, noun)
        return

    # ── 从 agent_result 提取信息 ─────────────────────────────
    success = bool(agent_result.success)
    steps_taken = agent_result.total_steps or 0
    tool_calls_log = list(agent_result.tool_calls_log or [])

    actual_tools = []
    tool_successes = 0
    tool_failures = 0
    for tc in tool_calls_log:
        tool_name = tc.get("tool", "")
        if tool_name and tool_name != "final_answer":
            actual_tools.append(tool_name)
            if tc.get("success", True):
                tool_successes += 1
            else:
                tool_failures += 1

    total_calls = tool_successes + tool_failures
    tool_success_rate = (tool_successes / total_calls) if total_calls > 0 else 1.0

    logger.info(
        "[Learn] %s+%s success=%s steps=%d tools=%d/%d tool_rate=%.1f%%",
        verb, noun, success, steps_taken,
        tool_successes, total_calls, tool_success_rate * 100,
    )

    # ── 每次都更新统计 ──
    try:
        from app.agent.chain.tool_chains import update_chain_stats
        update_chain_stats(
            verb=verb,
            noun=noun,
            steps_taken=steps_taken,
            success=success,
        )
    except Exception as e:
        logger.warning("[Learn] 统计更新失败: %s", e)

    if success:
        if all_phases_completed is False:
            logger.info(
                "[Learn] %s+%s: 拦截 — 未完成全部 phase（被错误退出），不写入 chain",
                verb, noun,
            )
            return
        _writeback_chain(actual_tools, tool_success_rate, verb, noun, chain_def=chain_def)
    else:
        _record_failure(actual_tools, tool_calls_log, verb, noun)


# ═══════════════════════════════════════════════════════════════
# 写回链 / 记录失败
# ═══════════════════════════════════════════════════════════════

def _writeback_chain(
    actual_tools: List[str],
    tool_success_rate: float,
    verb: str,
    noun: str,
    chain_def=None,
):
    """成功 → 写回 tool_chains.json（带质量门）。"""
    from app.agent.chain.tool_chains import get_tool_chain, save_tool_chain, save_chain_plan, get_chain_stats

    if not actual_tools:
        return

    # ── 质量门 ──
    MAX_STEPS_PER_PHASE = 5

    # phase 步数 > 5 → 拦截
    if chain_def and chain_def.steps:
        for step in chain_def.steps:
            if hasattr(step, 'steps_taken') and step.steps_taken and step.steps_taken > MAX_STEPS_PER_PHASE:
                logger.info("[Learn] %s+%s: 拦截 — phase %d 步数 %d > %d", verb, noun, step.order, step.steps_taken, MAX_STEPS_PER_PHASE)
                return

    # 工具成功率 < 50% → 拦截
    if tool_success_rate < 0.5:
        logger.info("[Learn] %s+%s: 拦截 — 工具成功率 %.1f%% < 50%%", verb, noun, tool_success_rate * 100)
        return

    # 旧链已验证 → 拦截
    actual_set = set(actual_tools)
    chain_set = set()
    if chain_def and chain_def.steps:
        chain_set = {s.agent for s in chain_def.steps if s.agent}
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

    # agent 完全遵循 chain → 不需要改
    if chain_set and actual_set >= chain_set:
        logger.info("[Learn] %s+%s: chain 已验证有效，保持不变", verb, noun)
        return

    # ── 质量门全部通过，写入 ──

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
        logger.info("[Learn] %s+%s: 保存完整 plan → %d phases", verb, noun, len(plan["phases"]))
    else:
        seen = set()
        new_chain = []
        for t in actual_tools:
            if t not in seen:
                seen.add(t)
                new_chain.append({"tool": t, "desc": ""})
        if new_chain:
            save_tool_chain(verb, noun, new_chain)
            logger.info("[Learn] %s+%s: 学习新链（旧格式） → %s", verb, noun, [s["tool"] for s in new_chain])


def _record_failure(actual_tools: List[str], tool_calls_log: List[Dict], verb: str, noun: str):
    """失败 → 记录到 failures.json。"""
    data = _load_failures()
    key = f"{verb}+{noun}"

    failed_tools = []
    for tc in tool_calls_log:
        if not tc.get("success", True):
            failed_tools.append(tc.get("tool", ""))

    entry = data.get(key, {"chain": actual_tools, "fail_count": 0, "failed_tools": []})
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    entry["last_fail_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["chain"] = actual_tools
    entry["failed_tools"] = failed_tools or entry.get("failed_tools", [])
    data[key] = entry

    _save_failures(data)
    logger.info("[Learn] %s+%s: 记录失败 (count=%d, failed=%s)", verb, noun, entry["fail_count"], failed_tools or "unknown")


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
    _FAILURES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
