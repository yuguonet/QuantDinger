# -*- coding: utf-8 -*-
"""
Agent Runner — shared ReAct LLM ↔ tool execution loop.

This is the *single authoritative implementation* of the agent loop.
Both the legacy single-agent executor and future multi-agent runners
should delegate here.

Design goals:
- Stateless: all mutable state lives in the caller
- Pluggable callbacks for progress, streaming
- Tool timeout + non-retriable cache
- Parallel tool execution when multiple tools called in one step
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Tool name → friendly label for progress messages
_TOOL_LABELS: Dict[str, str] = {
    "get_realtime_quote": "获取实时行情",
    "get_daily_history": "获取历史K线",
    "get_stock_info": "获取基本面",
    "get_chip_distribution": "筹码分布分析",
    "analyze_trend": "技术趋势分析",
    "calculate_ma": "均线计算",
    "get_volume_analysis": "量能分析",
    "analyze_pattern": "K线形态识别",
    "search_stock_news": "新闻搜索",
    "search_comprehensive_intel": "综合情报搜索",
    "get_market_indices": "大盘指数",
    "get_sector_rankings": "板块排名",
}


@dataclass
class RunLoopResult:
    """Output produced by run_agent_loop."""
    success: bool = False
    content: str = ""
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)
    total_steps: int = 0
    total_tokens: int = 0
    provider: str = ""
    models_used: List[str] = field(default_factory=list)
    error: Optional[str] = None
    messages: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def model(self) -> str:
        return ", ".join(dict.fromkeys(m for m in self.models_used if m))


def serialize_tool_result(result: Any) -> str:
    """Serialize tool result to JSON string for LLM consumption."""
    if result is None:
        return json.dumps({"result": None})
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)
    if hasattr(result, "__dict__"):
        try:
            d = {k: v for k, v in result.__dict__.items() if not k.startswith("_")}
            return json.dumps(d, ensure_ascii=False, default=str)
        except Exception:
            return str(result)
    return str(result)


def _is_non_retriable(result: Any) -> bool:
    """Return True when a tool result says 'do not retry'."""
    return (
        isinstance(result, dict)
        and bool(result.get("error"))
        and result.get("retriable") is False
    )


def _remaining_timeout(start: float, budget: Optional[float]) -> Optional[float]:
    if budget is None or budget <= 0:
        return None
    return max(0.0, float(budget) - (time.time() - start))


# ── Core loop ─────────────────────────────────────────────────

def run_agent_loop(
    *,
    messages: List[Dict[str, Any]],
    tool_registry: ToolRegistry,
    call_with_tools_fn: Callable,   # (messages, tools) -> {"content", "tool_calls", "usage"}
    max_steps: int = 10,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_wall_clock_seconds: Optional[float] = None,
    tool_timeout_seconds: Optional[float] = 60.0,
) -> RunLoopResult:
    """Execute the ReAct LLM ↔ tool loop.

    Args:
        messages: Initial message list (system + user + optional history).
                  **Mutated in-place** — tool results are appended.
        tool_registry: Registry of callable tools.
        call_with_tools_fn: Callable(messages, tools) -> dict with
                            "content", "tool_calls", "usage".
        max_steps: Maximum number of LLM round-trips.
        progress_callback: Optional callback receiving progress dicts.
        max_wall_clock_seconds: Overall timeout budget.
        tool_timeout_seconds: Per-tool-batch timeout.

    Returns:
        RunLoopResult with final content, stats, and mutated messages list.
    """
    labels = _TOOL_LABELS
    tool_decls = tool_registry.to_openai_tools()

    start_time = time.time()
    tool_calls_log: List[Dict[str, Any]] = []
    non_retriable_cache: Dict[str, str] = {}
    total_tokens = 0
    provider_used = ""
    models_used: List[str] = []

    _MIN_STEP_BUDGET = 8.0

    for step in range(max_steps):
        remaining = _remaining_timeout(start_time, max_wall_clock_seconds)
        if remaining is not None and remaining <= 0:
            logger.warning("Agent timed out before step %d", step + 1)
            return RunLoopResult(
                success=False, tool_calls_log=tool_calls_log,
                total_steps=step, total_tokens=total_tokens,
                provider=provider_used, models_used=models_used,
                error=f"Agent timed out after {time.time()-start_time:.1f}s",
                messages=messages,
            )
        if remaining is not None and step > 0 and remaining <= _MIN_STEP_BUDGET:
            logger.warning("Agent budget too low for step %d (%.1fs remaining)", step + 1, remaining)
            return RunLoopResult(
                success=False, tool_calls_log=tool_calls_log,
                total_steps=step, total_tokens=total_tokens,
                provider=provider_used, models_used=models_used,
                error=f"Insufficient budget ({remaining:.1f}s) for next step",
                messages=messages,
            )

        logger.info("Agent step %d/%d", step + 1, max_steps)

        # ── progress: thinking ──
        if progress_callback:
            if not tool_calls_log:
                thinking_msg = "正在分析问题，制定数据获取策略..."
            else:
                last_tool = tool_calls_log[-1].get("tool", "")
                label = labels.get(last_tool, last_tool)
                thinking_msg = f"「{label}」已完成，继续深入分析..."
            progress_callback({"type": "thinking", "step": step + 1, "message": thinking_msg})

        # ── LLM call (with retry) ──
        _LLM_MAX_RETRIES = 3
        _LLM_RETRY_BASE_DELAY = 2.0
        response = None
        for _attempt in range(_LLM_MAX_RETRIES):
            try:
                response = call_with_tools_fn(messages, tool_decls)
                break
            except Exception as e:
                err_str = str(e)
                is_retriable = any(s in err_str for s in ("429", "500", "502", "503", "529", "timeout", "Rate"))
                if is_retriable and _attempt < _LLM_MAX_RETRIES - 1:
                    delay = _LLM_RETRY_BASE_DELAY * (2 ** _attempt)
                    logger.warning("LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                                   _attempt + 1, _LLM_MAX_RETRIES, delay, e)
                    time.sleep(delay)
                    continue
                logger.error("LLM call failed at step %d: %s", step + 1, e)
                return RunLoopResult(
                    success=False, tool_calls_log=tool_calls_log,
                    total_steps=step + 1, total_tokens=total_tokens,
                    provider=provider_used, models_used=models_used,
                    error=f"LLM call failed: {e}", messages=messages,
                )
        if response is None:
            return RunLoopResult(
                success=False, tool_calls_log=tool_calls_log,
                total_steps=step + 1, total_tokens=total_tokens,
                provider=provider_used, models_used=models_used,
                error="LLM call failed after retries", messages=messages,
            )

        usage = response.get("usage", {}) or {}
        total_tokens += usage.get("total_tokens", 0)

        tool_calls = response.get("tool_calls", []) or []

        if tool_calls:
            # ── tool execution branch ──
            logger.info("Agent requesting %d tool call(s): %s",
                        len(tool_calls), [tc["name"] for tc in tool_calls])

            # Append assistant message with tool_calls
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": response.get("content"),
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            # Execute tools (single inline, multiple parallel)
            results = _execute_tools(
                tool_calls, tool_registry, step + 1,
                progress_callback, tool_calls_log, non_retriable_cache,
                timeout_seconds=tool_timeout_seconds,
            )

            # Append tool results preserving order
            tc_order = {tc["id"]: i for i, tc in enumerate(tool_calls)}
            results.sort(key=lambda x: tc_order.get(x["tc_id"], 0))
            for tr in results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tc_id"],
                    "name": tr["tool_name"],
                    "content": tr["result_str"],
                })

            remaining = _remaining_timeout(start_time, max_wall_clock_seconds)
            if remaining is not None and remaining <= 0:
                return RunLoopResult(
                    success=False, tool_calls_log=tool_calls_log,
                    total_steps=step + 1, total_tokens=total_tokens,
                    provider=provider_used, models_used=models_used,
                    error="Timed out after tool execution", messages=messages,
                )
        else:
            # ── final answer branch ──
            logger.info("Agent completed in %d steps (%.1fs, %d tokens)",
                        step + 1, time.time() - start_time, total_tokens)
            if progress_callback:
                progress_callback({"type": "generating", "step": step + 1, "message": "正在生成最终分析..."})

            final_content = response.get("content") or ""
            return RunLoopResult(
                success=bool(final_content),
                content=final_content,
                tool_calls_log=tool_calls_log,
                total_steps=step + 1,
                total_tokens=total_tokens,
                provider=provider_used,
                models_used=models_used,
                messages=messages,
            )

    # Max steps exceeded
    logger.warning("Agent hit max steps (%d)", max_steps)
    return RunLoopResult(
        success=False, tool_calls_log=tool_calls_log,
        total_steps=max_steps, total_tokens=total_tokens,
        provider=provider_used, models_used=models_used,
        error=f"Exceeded max steps ({max_steps})",
        messages=messages,
    )


# ── Tool execution ────────────────────────────────────────────

def _exec_single(tc: Dict, registry: ToolRegistry, cache: Dict[str, str]) -> tuple:
    """Execute one tool call, return (tc, result_str, success, duration, cached)."""
    tool_name = tc["name"]
    args = tc["arguments"]
    cache_key = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"

    # Non-retriable cache
    if cache_key in cache:
        return tc, cache[cache_key], False, 0.0, True

    t0 = time.time()
    try:
        result = registry.execute(tool_name, **args)
        result_str = serialize_tool_result(result)
        if _is_non_retriable(result):
            cache[cache_key] = result_str
    except Exception as e:
        result_str = json.dumps({"error": str(e)})
    dur = round(time.time() - t0, 2)
    return tc, result_str, "error" not in result_str, dur, False


def _execute_tools(
    tool_calls: List[Dict],
    registry: ToolRegistry,
    step: int,
    progress_callback: Optional[Callable],
    tool_calls_log: List[Dict],
    cache: Dict[str, str],
    timeout_seconds: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Execute tool calls — single inline, multiple parallel."""
    results = []

    if len(tool_calls) == 1:
        tc = tool_calls[0]
        if progress_callback:
            progress_callback({"type": "tool_start", "step": step, "tool": tc["name"],
                               "display_name": _TOOL_LABELS.get(tc["name"], tc["name"])})
        tc_item, result_str, success, dur, cached = _exec_single(tc, registry, cache)
        if progress_callback:
            progress_callback({"type": "tool_done", "step": step, "tool": tc["name"],
                               "display_name": _TOOL_LABELS.get(tc["name"], tc["name"]),
                               "success": success, "duration": dur})
        tool_calls_log.append({
            "step": step, "tool": tc["name"], "arguments": tc["arguments"],
            "success": success, "duration": dur, "result_length": len(result_str), "cached": cached,
        })
        results.append({"tc_id": tc["id"], "tool_name": tc["name"], "result_str": result_str})
    else:
        # Notify all starts
        for tc in tool_calls:
            if progress_callback:
                progress_callback({"type": "tool_start", "step": step, "tool": tc["name"],
                                   "display_name": _TOOL_LABELS.get(tc["name"], tc["name"])})

        max_workers = min(len(tool_calls), 5)
        pool = ThreadPoolExecutor(max_workers=max_workers)
        try:
            batch_start = time.time()
            futures = {pool.submit(_exec_single, tc, registry, cache): tc for tc in tool_calls}
            for future in as_completed(futures):
                # Calculate remaining budget across the whole batch
                elapsed = time.time() - batch_start
                if timeout_seconds and timeout_seconds > 0:
                    per_future_timeout = max(0.1, timeout_seconds - elapsed)
                else:
                    per_future_timeout = None
                try:
                    tc_item, result_str, success, dur, cached = future.result(
                        timeout=per_future_timeout
                    )
                except FuturesTimeoutError:
                    tc_item = futures[future]
                    result_str = json.dumps({"error": f"Tool timed out after {per_future_timeout:.1f}s", "timeout": True})
                    success = False
                    dur = round(per_future_timeout or 0, 2)
                    cached = False

                if progress_callback:
                    progress_callback({"type": "tool_done", "step": step, "tool": tc_item["name"],
                                       "display_name": _TOOL_LABELS.get(tc_item["name"], tc_item["name"]),
                                       "success": success, "duration": dur})
                tool_calls_log.append({
                    "step": step, "tool": tc_item["name"], "arguments": tc_item["arguments"],
                    "success": success, "duration": dur, "result_length": len(result_str), "cached": cached,
                })
                results.append({"tc_id": tc_item["id"], "tool_name": tc_item["name"], "result_str": result_str})
        finally:
            pool.shutdown(wait=False)

    return results


# ── JSON parsing helpers ──────────────────────────────────────

def parse_dashboard_json(content: str) -> Optional[Dict[str, Any]]:
    """Extract and parse a Decision Dashboard JSON from agent text.
    
    Strategies:
    1. Markdown code blocks (```json ... ```)
    2. Raw JSON parse
    3. Brace-delimited substring
    4. json_repair fallback
    """
    if not content:
        return None

    try:
        from json_repair import repair_json
    except ImportError:
        repair_json = None

    # Strategy 1: markdown code blocks
    blocks = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
    for block in blocks:
        parsed = _try_parse(block, repair_json)
        if parsed:
            return parsed

    # Strategy 2: raw parse
    parsed = _try_parse(content, repair_json)
    if parsed:
        return parsed

    # Strategy 3: brace-delimited
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        candidate = content[start:end + 1]
        parsed = _try_parse(candidate, repair_json)
        if parsed:
            return parsed

    logger.warning("Failed to parse dashboard JSON from agent response")
    return None


def _try_parse(text: str, repair_fn=None) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    if repair_fn:
        try:
            repaired = repair_fn(text)
            obj = json.loads(repaired)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None
