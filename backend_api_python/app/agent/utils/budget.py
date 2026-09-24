# -*- coding: utf-8 -*-
"""
成本硬预算 + 校验环计数器面板（提智方案 阶段 0.9，原则 7「校验环必须自证在工作」）。

三条预算线（超限优雅收尾：已完成的阶段产物交出去，不硬崩）：
  ① 单 run token 预算 —— AGENT_RUN_TOKEN_BUDGET（默认 200_000）
  ② 单 run 工具调用预算 —— AGENT_RUN_TOOL_CALL_BUDGET（默认 60）
  ③ 单用户日 token 配额 —— AGENT_DAY_TOKEN_QUOTA（默认 2_000_000，按 user/session 记账）

设计取舍：
- 阈值全部走 env（可覆盖），代码内零硬编码；默认值取「足够跑完正常任务、能拦住
  失控复读」的量级（正常盘面分析 run 约 5~40K token，见 2026-09 实测）。
- 预算**只降级不静默**：超限时置 state 标记 + 记 trace（`budget_exceeded`），
  执行侧读到标记即注入「立即收尾」提示并停止新探索（软收尾），不 raise（避免
  像 900s 硬超时那样把已成型的交付物整份丢掉）。
- 计数器面板：把校验环（verify/grounding）与预算事件的计数汇总，供 run trace 与
  周报脚本（scripts/weekly_panel.py）读取 —— 没有流量的校验环 = 坏掉的校验环。
"""

import os
from typing import Dict, Optional


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


# ── 三条预算线（env 可覆盖）──
def run_token_budget() -> int:
    return max(1000, _int_env("AGENT_RUN_TOKEN_BUDGET", 200_000))


def run_tool_call_budget() -> int:
    return max(1, _int_env("AGENT_RUN_TOOL_CALL_BUDGET", 60))


def day_token_quota() -> int:
    return max(1000, _int_env("AGENT_DAY_TOKEN_QUOTA", 2_000_000))


# ── 记账（进程内，按 key；多 worker 各自记账，不做跨进程聚合）──
_DAY_USAGE: Dict[str, int] = {}
_DAY_STAMP: str = ""


def _today() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _rollover() -> None:
    """日切自动重置（不依赖外部调度器）。"""
    global _DAY_STAMP
    d = _today()
    if d != _DAY_STAMP:
        _DAY_USAGE.clear()
        _DAY_STAMP = d


def add_day_usage(key: str, tokens: int) -> int:
    """累加某记账键（user:<id> / session:<id>）的当日 token 用量，返回值。"""
    if not key:
        return 0
    _rollover()
    try:
        _DAY_USAGE[key] = int(_DAY_USAGE.get(key, 0)) + int(tokens or 0)
    except (TypeError, ValueError):
        pass
    return _DAY_USAGE.get(key, 0)


def get_day_usage(key: str) -> int:
    _rollover()
    return int(_DAY_USAGE.get(key, 0))


def reset_day_usage() -> None:
    """日切重置（由调度/cron 每日调用）。"""
    _DAY_USAGE.clear()


def check_budget(*, run_tokens: int = 0, tool_calls: int = 0,
                 day_tokens: int = 0) -> Optional[str]:
    """返回超限原因字符串（None=未超限）。按 ①→②→③ 顺序判，取首个命中。"""
    if run_tokens and run_tokens > run_token_budget():
        return "run_token:%d/%d" % (run_tokens, run_token_budget())
    if tool_calls and tool_calls > run_tool_call_budget():
        return "run_tool_calls:%d/%d" % (tool_calls, run_tool_call_budget())
    if day_tokens and day_tokens > day_token_quota():
        return "day_token:%d/%d" % (day_tokens, day_token_quota())
    return None


def budget_wrapup_hint(reason: str) -> str:
    """超限时的软收尾提示（注入 observations / 任务书）。"""
    return (
        "\n[系统·成本预算] 本次任务已触及成本上限（%s）。请**立即收尾**："
        "用 `final_answer(...)` 交出目前已经取到 / 算出的成果，"
        "不要再开始新的工具探索或重写。若有关键数据未取到，"
        "在结论中如实标注为 missing_data（宁可缺也不编）。" % reason
    )


# ═══════════════════════════════════════════════════════════════════════════
#  计数器面板（原则 7：校验环自证在工作）
# ═══════════════════════════════════════════════════════════════════════════
# 汇总来源：trace 事件（verify_done / verify_skipped / budget_exceeded /
# hallucination_blocked / replan）+ grounding 拒收。键名稳定，供周报脚本聚合。
PANEL_KEYS = (
    "runs",                 # 总 run 数
    "verify_seen",          # 进入 verify 的次数
    "verify_skipped",       # 直通（无数字）次数
    "verify_pass",          # PASS
    "verify_repair",        # REPAIR_ONCE
    "verify_degrade",       # DEGRADE
    "grounding_rejects",    # 数字溯源拒收次数
    "hallucination_blocks", # 幻觉调用拦截次数
    "budget_exceeded",      # 预算超限次数
    "replans",              # replan 次数
)


def empty_panel() -> Dict[str, int]:
    return {k: 0 for k in PANEL_KEYS}


def bump(panel: dict, key: str, n: int = 1) -> dict:
    """安全自增（未知键忽略，防拼写漂移污染面板）。"""
    if key in PANEL_KEYS:
        panel[key] = int(panel.get(key, 0)) + int(n or 0)
    return panel


def panel_from_trace(events) -> Dict[str, int]:
    """从一串 trace 事件（dict 列表 [{type,payload}]）汇总面板计数。"""
    p = empty_panel()
    for ev in (events or []):
        if not isinstance(ev, dict):
            continue
        t = ev.get("type") or ev.get("kind") or ""
        pl = ev.get("payload") or {}
        if t == "verify_done":
            bump(p, "verify_seen")
            r = str(pl.get("route") or "")
            bump(p, {"PASS": "verify_pass", "REPAIR_ONCE": "verify_repair",
                     "DEGRADE": "verify_degrade"}.get(r, "verify_seen"))
        elif t == "verify_skipped":
            bump(p, "verify_skipped")
        elif t == "grounding_reject":
            bump(p, "grounding_rejects", int(pl.get("count", 1) or 1))
        elif t == "hallucination_blocked":
            bump(p, "hallucination_blocks", int(pl.get("count", 1) or 1))
        elif t == "budget_exceeded":
            bump(p, "budget_exceeded")
        elif t == "replan":
            bump(p, "replans")
    return p


def render_panel(panel: dict) -> str:
    """渲染面板为可读文本（周报/日志用）。"""
    p = dict(empty_panel()); p.update({k: v for k, v in (panel or {}).items() if k in PANEL_KEYS})
    p["runs"] = p.get("runs", 0)
    lines = ["═══ 校验环 / 成本计数器面板 ═══"]
    for k in PANEL_KEYS:
        lines.append("  %-20s %d" % (k + ":", p.get(k, 0)))
    # 自证警示：校验环有 run 但零流量＝断了
    if p.get("runs", 0) > 0 and p.get("verify_seen", 0) == 0 and p.get("verify_skipped", 0) == 0:
        lines.append("  ⚠ 校验环零流量（有 run 但 verify 从未触发）——校验环可能已断链")
    return "\n".join(lines)


# ── 运行中从 smolagents memory 现算用量（供每步预算钩子）──
def tokens_from_agent(agent) -> int:
    """从 agent.memory.steps 累加 input+output token（与 nodes._extract_token_usage 同口径）。"""
    total = 0
    try:
        from smolagents.memory import ActionStep, PlanningStep
        for step in getattr(getattr(agent, "memory", None), "steps", []) or []:
            if not isinstance(step, (ActionStep, PlanningStep)):
                continue
            u = getattr(step, "token_usage", None)
            if u is None:
                continue
            total += (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0)
    except Exception:
        return 0
    return total


def tool_calls_from_agent(agent) -> int:
    """统计已发生的真实工具调用数（ActionStep.tool_calls，去掉伪工具名）。"""
    n = 0
    try:
        from smolagents.memory import ActionStep
        for step in getattr(getattr(agent, "memory", None), "steps", []) or []:
            if not isinstance(step, ActionStep):
                continue
            for tc in (getattr(step, "tool_calls", None) or []):
                nm = getattr(tc, "name", "") or ""
                if nm in ("python_interpreter", "final_answer", ""):
                    continue
                n += 1
    except Exception:
        return 0
    return n
