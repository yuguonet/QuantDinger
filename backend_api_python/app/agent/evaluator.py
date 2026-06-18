# -*- coding: utf-8 -*-
"""
Agent Evaluator — 在线执行质量评估 + 工具链学习。

⚠️ 注意区分：
  本文件（evaluator.py）   — 在线评估，每次 agent.run() 后立即执行
  chain/evaluator.py       — 离线评估，T+N 验证决策准确性，定时运行

触发时机：
  agent.py → _post_evaluate() → evaluate() + learn_from_execution()
  每次 Agent 执行完毕后自动调用，不消耗 Agent 步数。

评估维度（纯规则，<1ms，无 LLM 调用）：
  1. 工具调用成功率 — 成功/失败比率
  2. 工具链遵循度 — 实际调用 vs 预期 chain 的匹配度
  3. 步骤效率 — 实际步数 vs chain 长度的比值
  4. final_answer 是否生成
  5. 响应是否包含实际数据（数字/代码块/表格）

闭环动作：
  success → _writeback_chain() — 更新 tool_chains.json（学习新链 or 强化旧链）
  failure → _record_failure()  — 写入 tool_chain_failures.json（下次避开）
  grey    → 不操作

领域专用评估：
  coding  — 代码修改质量、lint/diagnostics 使用、读→改工作流
  finance — 数据获取完整性、分析链路完整度、实际市场数据
  trading — 同 finance + 交易执行工具检查

公开接口：
  evaluate(agent_result, tool_chain, verb, noun, domain) → EvalResult
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
    verdict: str = "unknown"          # "success" | "failure" | "grey"
    score: int = 0                    # 综合得分
    tool_success_rate: float = 0.0    # 工具成功率 (0~1)
    chain_coverage: float = 0.0       # chain 工具被调用的比例 (0~1)
    step_efficiency: float = 0.0      # 步骤效率 (实际步数 / chain长度)
    has_final_answer: bool = False    # 是否有 final_answer
    has_real_data: bool = False       # 响应是否包含实际数据
    actual_tools: List[str] = field(default_factory=list)  # 实际调用的工具
    chain_tools: List[str] = field(default_factory=list)   # chain 中的工具
    steps_taken: int = 0              # 实际步数
    chain_length: int = 0             # chain 长度
    details: str = ""                 # 评估说明
    _tool_calls_log: List[Dict] = field(default_factory=list)  # 原始工具调用日志


# ═══════════════════════════════════════════════════════════════
# 2. 核心评估逻辑
# ═══════════════════════════════════════════════════════════════

def _evaluate_coding(agent_result, result, tool_chain, verb, noun) -> EvalResult:
    """Coding domain 专用评估逻辑。

    评估信号：
      1. 工具调用成功率（同通用逻辑）
      2. 代码修改质量 — 是否使用了 lint/diagnostics 验证
      3. 工作流完整度 — 读→改→验证 的流程是否合理
      4. final_answer 是否生成
      5. 错误恢复 — 遇到错误后是否重试成功
    """
    actual_tools = []
    tool_successes = 0
    tool_failures = 0
    lint_used = False
    diagnostics_used = False
    edit_count = 0
    read_count = 0
    error_then_success = False
    had_error = False

    if agent_result.tool_calls_log:
        for i, tc in enumerate(agent_result.tool_calls_log):
            tool_name = tc.get("tool", "")
            if tool_name and tool_name != "final_answer":
                actual_tools.append(tool_name)
                if tc.get("success", True):
                    tool_successes += 1
                    if had_error:
                        error_then_success = True
                else:
                    tool_failures += 1
                    had_error = True

                # Track coding-specific patterns
                if tool_name in ("code_lint",):
                    lint_used = True
                if tool_name in ("lsp_diagnostics",):
                    diagnostics_used = True
                if tool_name in ("workspace_edit_file", "workspace_write_file"):
                    edit_count += 1
                if tool_name in ("workspace_read_file", "read_lines"):
                    read_count += 1

    result.actual_tools = actual_tools
    result.steps_taken = agent_result.total_steps or 0
    result.has_final_answer = bool(agent_result.content and agent_result.content.strip())
    result._tool_calls_log = list(agent_result.tool_calls_log or [])

    # ── 信号 1: 工具调用成功率 ────────────────────────────────
    total_calls = tool_successes + tool_failures
    if total_calls > 0:
        result.tool_success_rate = tool_successes / total_calls
    else:
        result.tool_success_rate = 1.0

    score_tool_success = 0
    if result.tool_success_rate >= 0.8:
        score_tool_success = 2
    elif result.tool_success_rate >= 0.5:
        score_tool_success = 0
    else:
        score_tool_success = -2

    # ── 信号 2: 代码验证 ──────────────────────────────────────
    # 用了 lint 或 diagnostics 验证 → 加分
    score_validation = 0
    if lint_used or diagnostics_used:
        score_validation = 2
    elif edit_count > 0 and not lint_used:
        score_validation = -1  # 改了代码但没验证

    # ── 信号 3: 工作流完整度 ──────────────────────────────────
    # 读→改 是基本模式
    score_workflow = 0
    if edit_count > 0 and read_count > 0:
        score_workflow = 2  # 先读后改，流程正确
    elif edit_count > 0 and read_count == 0:
        score_workflow = -1  # 没读就改，可能出错
    elif edit_count == 0 and read_count > 0:
        score_workflow = 1  # 只读不改，可能是分析任务

    # ── 信号 4: final_answer ──────────────────────────────────
    score_answer = 2 if result.has_final_answer else -2

    # ── 信号 5: 错误恢复 ──────────────────────────────────────
    score_recovery = 1 if error_then_success else 0

    # ── 综合评分 ──────────────────────────────────────────────
    result.score = score_tool_success + score_validation + score_workflow + score_answer + score_recovery

    # ── 判定 ─────────────────────────────────────────────────
    if result.score >= 3:
        result.verdict = "success"
    elif result.score <= -2:
        result.verdict = "failure"
    else:
        result.verdict = "grey"

    result.details = (
        f"tools={score_tool_success} validation={score_validation} "
        f"workflow={score_workflow} answer={score_answer} recovery={score_recovery} "
        f"→ total={result.score} (edits={edit_count} reads={read_count} lint={lint_used})"
    )

    logger.info(
        "[Eval] coding/%s+%s verdict=%s score=%d steps=%d tools=%d/%d %s",
        verb, noun, result.verdict, result.score,
        result.steps_taken, tool_successes, total_calls,
        result.details,
    )
    return result


def evaluate(
    agent_result,
    tool_chain: List[Dict[str, str]],
    verb: str = "",
    noun: str = "",
    domain: str = "",
) -> EvalResult:
    """评估 agent 执行结果。

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
    result.chain_length = len(tool_chain)

    # ── 领域专用评估 ──────────────────────────────────────────
    if domain == "coding":
        return _evaluate_coding(agent_result, result, tool_chain, verb, noun)
    if domain in ("finance", "trading"):
        return _evaluate_finance(agent_result, result, tool_chain, verb, noun, domain)

    # ── 通用评估（无领域或未知领域） ──────────────────────────
    return _evaluate_generic(agent_result, result, tool_chain, verb, noun)


def _evaluate_finance(agent_result, result, tool_chain, verb, noun, domain) -> EvalResult:
    """Finance / Trading 域专用评估逻辑。

    评估信号：
      1. 工具调用成功率
      2. 数据获取完整性 — 是否调用了行情/指标类工具获取真实数据
      3. 分析链路完整度 — 数据→分析→决策 的流程是否合理
      4. final_answer 是否生成
      5. 响应是否包含实际市场数据
    """
    actual_tools = []
    tool_successes = 0
    tool_failures = 0
    data_tools_used = set()
    analysis_tools_used = set()
    trading_tools_used = set()

    # 金融域工具分类
    _DATA_TOOLS = {
        "get_realtime_quote", "agent_get_kline", "get_stock_info",
        "get_market_indices", "get_sector_rankings", "get_fund_flow",
        "get_chip_distribution", "get_market_overview", "get_hot_sectors",
        "search_stock_by_name", 
    }
    _ANALYSIS_TOOLS = {
        "analyze_trend", "get_indicator_snapshot", "calculate_ma",
        "get_volume_analysis", "analyze_pattern",
        "search_comprehensive_intel",
        "get_dragon_tiger_stocks", "get_polymarket_analysis",
    }
    _TRADING_TOOLS = {
        "execute_trade", "place_order", "cancel_order",
        "get_positions", "get_account_balance", "get_order_status",
        "run_backtest", "run_quick_backtest",
    }

    if agent_result.tool_calls_log:
        for tc in agent_result.tool_calls_log:
            tool_name = tc.get("tool", "")
            if tool_name and tool_name != "final_answer":
                actual_tools.append(tool_name)
                if tc.get("success", True):
                    tool_successes += 1
                else:
                    tool_failures += 1
                if tool_name in _DATA_TOOLS:
                    data_tools_used.add(tool_name)
                if tool_name in _ANALYSIS_TOOLS:
                    analysis_tools_used.add(tool_name)
                if tool_name in _TRADING_TOOLS:
                    trading_tools_used.add(tool_name)

    result.actual_tools = actual_tools
    result.steps_taken = agent_result.total_steps or 0
    result.has_final_answer = bool(agent_result.content and agent_result.content.strip())
    result._tool_calls_log = list(agent_result.tool_calls_log or [])

    # ── 信号 1: 工具调用成功率 ────────────────────────────────
    total_calls = tool_successes + tool_failures
    if total_calls > 0:
        result.tool_success_rate = tool_successes / total_calls
    else:
        result.tool_success_rate = 1.0

    score_tool_success = 0
    if result.tool_success_rate >= 0.8:
        score_tool_success = 2
    elif result.tool_success_rate >= 0.5:
        score_tool_success = 0
    else:
        score_tool_success = -2

    # ── 信号 2: 数据获取完整性 ────────────────────────────────
    # 金融分析必须先拿到行情数据
    score_data = 0
    if data_tools_used:
        score_data = 2  # 拿到了行情数据
    elif total_calls > 0 and not data_tools_used:
        score_data = -1  # 有工具调用但没拿数据

    # ── 信号 3: 分析链路完整度 ────────────────────────────────
    # 数据→分析 是基本模式；交易域还需要 分析→执行
    score_workflow = 0
    if data_tools_used and analysis_tools_used:
        score_workflow = 2  # 拿数据+做分析，流程完整
    elif data_tools_used and not analysis_tools_used:
        score_workflow = 1  # 拿了数据但没分析（可能简单查询）
    elif analysis_tools_used and not data_tools_used:
        score_workflow = -1  # 没数据就分析，可能用的假数据
    if domain == "trading" and trading_tools_used:
        score_workflow += 1  # 交易域有执行动作，加分

    # ── 信号 4: final_answer ──────────────────────────────────
    score_answer = 2 if result.has_final_answer else -2

    # ── 信号 5: 响应是否包含实际市场数据 ──────────────────────
    content = agent_result.content or ""
    result.has_real_data = _contains_real_data(content)
    score_real = 1 if result.has_real_data else 0

    # ── 综合评分 ──────────────────────────────────────────────
    result.score = score_tool_success + score_data + score_workflow + score_answer + score_real

    # ── 判定 ─────────────────────────────────────────────────
    if result.score >= 3:
        result.verdict = "success"
    elif result.score <= -2:
        result.verdict = "failure"
    else:
        result.verdict = "grey"

    result.details = (
        f"tools={score_tool_success} data={score_data} workflow={score_workflow} "
        f"answer={score_answer} real={score_real} → total={result.score} "
        f"(data_tools={data_tools_used} analysis_tools={analysis_tools_used} "
        f"trading_tools={trading_tools_used})"
    )

    logger.info(
        "[Eval] %s/%s+%s verdict=%s score=%d steps=%d tools=%d/%d %s",
        domain, verb, noun, result.verdict, result.score,
        result.steps_taken, tool_successes, total_calls,
        result.details,
    )
    return result


def _evaluate_generic(agent_result, result, tool_chain, verb, noun) -> EvalResult:
    """通用评估逻辑（无特定领域）。"""
    actual_tools = []
    tool_successes = 0
    tool_failures = 0

    if agent_result.tool_calls_log:
        for tc in agent_result.tool_calls_log:
            tool_name = tc.get("tool", "")
            if tool_name and tool_name not in ("final_answer",):
                actual_tools.append(tool_name)
                if tc.get("success", True):
                    tool_successes += 1
                else:
                    tool_failures += 1

    result.actual_tools = actual_tools
    result.steps_taken = agent_result.total_steps or 0
    result.has_final_answer = bool(agent_result.content and agent_result.content.strip())
    result._tool_calls_log = list(agent_result.tool_calls_log or [])

    # ── 信号 1: 工具调用成功率 ────────────────────────────────
    total_calls = tool_successes + tool_failures
    if total_calls > 0:
        result.tool_success_rate = tool_successes / total_calls
    else:
        result.tool_success_rate = 1.0

    score_tool_success = 0
    if result.tool_success_rate >= 0.8:
        score_tool_success = 2
    elif result.tool_success_rate >= 0.5:
        score_tool_success = 0
    else:
        score_tool_success = -2

    # ── 信号 2: 工具链遵循度 ──────────────────────────────────
    chain_set = set(result.chain_tools)
    actual_set = set(actual_tools)
    if chain_set:
        covered = chain_set & actual_set
        result.chain_coverage = len(covered) / len(chain_set)
    else:
        result.chain_coverage = 1.0

    score_chain = 0
    if not chain_set:
        score_chain = 0
    elif result.chain_coverage >= 0.8:
        score_chain = 1
    elif result.chain_coverage >= 0.5:
        score_chain = 0
    else:
        score_chain = -1

    # ── 信号 3: 步骤效率（绝对步数评判）────────────────────
    # 最佳 2-4 步：Agent 高效完成，chain 建议精准
    # 1 步：可能跳过了必要的分析维度
    # 5-6 步：偏多但可接受
    # 7+ 步：低效，chain 建议不佳或 Agent 迷路
    if result.chain_length > 0:
        result.step_efficiency = result.steps_taken / result.chain_length
    else:
        result.step_efficiency = 0

    score_steps = 0
    if 2 <= result.steps_taken <= 4:
        score_steps = 2    # 最佳区间
    elif result.steps_taken == 1:
        score_steps = 0    # 可能过于简单
    elif 5 <= result.steps_taken <= 6:
        score_steps = 0    # 偏多但可接受
    elif result.steps_taken >= 7:
        score_steps = -2   # 低效，chain 无价值
    # steps_taken == 0 通常是 agent 未执行，不加分

    # ── 信号 4: final_answer ──────────────────────────────────
    score_answer = 2 if result.has_final_answer else -2

    # ── 信号 5: 响应内容是否包含实际数据 ──────────────────────
    content = agent_result.content or ""
    result.has_real_data = _contains_real_data(content)
    score_data = 1 if result.has_real_data else 0

    # ── 综合评分 ──────────────────────────────────────────────
    result.score = score_tool_success + score_chain + score_steps + score_answer + score_data

    # ── 判定 ─────────────────────────────────────────────────
    if result.score >= 3:
        result.verdict = "success"
    elif result.score <= -2:
        result.verdict = "failure"
    else:
        result.verdict = "grey"

    result.details = (
        f"tools={score_tool_success} chain={score_chain} steps={score_steps} "
        f"answer={score_answer} data={score_data} → total={result.score}"
    )

    logger.info(
        "[Eval] %s/%s verdict=%s score=%d steps=%d/%d tools=%d/%d %s",
        verb, noun, result.verdict, result.score,
        result.steps_taken, result.chain_length,
        tool_successes, total_calls,
        result.details,
    )
    return result


def _contains_real_data(content: str) -> bool:
    """判断响应是否包含实际数据（数字、代码块、表格等）。"""
    import re
    if not content:
        return False
    # 包含数字（价格、百分比、指标值等）
    if re.search(r'\d+[.,]?\d*[%元万亿手]', content):
        return True
    # 包含代码块
    if '```' in content:
        return True
    # 包含表格
    if '|' in content and '-|-' in content:
        return True
    # 内容足够长（超过 200 字通常有实质内容）
    if len(content) > 200:
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# 3. 闭环动作：写回链 / 记录失败
# ═══════════════════════════════════════════════════════════════

def learn_from_execution(
    eval_result: EvalResult,
    verb: str,
    noun: str,
):
    """根据评估结果执行闭环动作。

    - 每次执行 → 更新统计（avg_steps, executions, success_rate）
    - success → 写回/强化 tool_chains.json
    - failure → 记录到 tool_chain_failures.json
    - grey → 不操作
    """
    # verb 或 noun 为空时跳过学习，避免生成残缺键（如 "analyze+", "+stock"）
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
        _writeback_chain(eval_result, verb, noun)
    elif eval_result.verdict == "failure":
        _record_failure(eval_result, verb, noun)
    # grey → 不操作


def _writeback_chain(eval_result: EvalResult, verb: str, noun: str):
    """成功 → 写回工具链（带 5 道质量门）。

    策略：
    - 如果 agent 完全遵循了 chain → 不需要更新（chain 已验证有效）
    - 如果 agent 偏离了 chain 但仍然成功 → 质量门检查 → 通过后才写入
    """
    from app.agent.chain.tool_chains import get_tool_chain, save_tool_chain, get_chain_stats

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

    # ── 质量门（5 道拦截） ──
    # 1. 步数 > 5 → 拦截（太低效）
    if eval_result.steps_taken > 5:
        logger.info("[Learn] %s+%s: 拦截 — 步数 %d > 5", verb, noun, eval_result.steps_taken)
        return

    # 2. 新链长度 > 5 → 拦截（太臃肿）
    new_chain_len = len(dict.fromkeys(eval_result.actual_tools))  # 去重后长度
    if new_chain_len > 5:
        logger.info("[Learn] %s+%s: 拦截 — 新链长度 %d > 5", verb, noun, new_chain_len)
        return

    # 3. 评分 < 60 → 拦截（质量不足）
    if eval_result.score < 60:
        logger.info("[Learn] %s+%s: 拦截 — 评分 %d < 60", verb, noun, eval_result.score)
        return

    # 4. 工具成功率 < 50% → 拦截（工具不可靠）
    if eval_result.tool_success_rate < 0.5:
        logger.info("[Learn] %s+%s: 拦截 — 工具成功率 %.1f%% < 50%%", verb, noun, eval_result.tool_success_rate * 100)
        return

    # 5. 旧链执行 ≥ 10 次且成功率 ≥ 80% → 拦截（旧链已验证，不轻易替换）
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

    # ── 质量门全部通过，写入新链 ──
    seen = set()
    new_chain = []
    for t in eval_result.actual_tools:
        if t not in seen:
            seen.add(t)
            new_chain.append({"tool": t, "desc": ""})

    if new_chain:
        save_tool_chain(verb, noun, new_chain)
        logger.info(
            "[Learn] %s+%s: 学习新链 → %s",
            verb, noun, [s["tool"] for s in new_chain],
        )


def _record_failure(eval_result: EvalResult, verb: str, noun: str):
    """失败 → 记录到 failures.json。

    记录内容：
    - 失败的工具链
    - 哪些工具失败了
    - 失败次数（同一链累计）
    """
    data = _load_failures()
    key = f"{verb}+{noun}"

    # 找出失败的工具（从 tool_calls_log 中提取 success=False 的）
    failed_tools = []
    if hasattr(eval_result, '_tool_calls_log') and eval_result._tool_calls_log:
        for tc in eval_result._tool_calls_log:
            if not tc.get("success", True):
                failed_tools.append(tc.get("tool", ""))

    entry = data.get(key, {"chain": eval_result.actual_tools, "fail_count": 0, "failed_tools": []})
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    entry["last_fail_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["chain"] = eval_result.actual_tools
    entry["score"] = eval_result.score
    entry["failed_tools"] = failed_tools or entry.get("failed_tools", [])
    data[key] = entry

    _save_failures(data)
    logger.info(
        "[Learn] %s+%s: 记录失败 (count=%d, score=%d, failed=%s)",
        verb, noun, entry["fail_count"], eval_result.score,
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
