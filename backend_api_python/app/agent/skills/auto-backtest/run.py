# -*- coding: utf-8 -*-
"""skills/auto-backtest/run.py — 全盘量级回测技能入口（backtest_bridge_plan.md Task 6，
§5.3/§5.7，2026-10-15）。

与工具面的分界（§5.6）：工具 = 一次确定性调用（个股/小集合，分钟级）；技能 = 多步
编排 + 大预算（scope=all 全市场约 5k 票，十分钟级），走 skill_run.py 子进程白名单，
产物落盘 + 预览回传，主上下文 O(摘要)。

易错点：
  - 本模块由 skill_run.py 经 modpath.import_module("skills.auto-backtest.run") 按
    路径解析执行（目录名带连字符，不可 `import` 语句导入）；目录的 __init__.py
    必须存在（market-screener 同款，包导入的锚点）；
  - skill_run.py 的调用形态是 `fn(**spec["kwargs"])`（scripts/skill_run.py L66/L97），
    故签名必须是**关键字参数**而非 run(args: dict)——计划 Task 6 Step 1.5 预填结论
    与现网不符，实施核对后按 market-screener 同款修正（计划明文授权"以核对为准"）；
  - 未知 kwargs 会被 skill_run.py 按 inspect.signature 拒绝并回 hint —— 新增入参
    必须同步本签名与 SKILL.md 参数表；
  - 载体分发/入参规整**不在此处**：统一走桥 parse_carrier 单源（批次C质量审查 I4，
    此前本文件的降级副本导致半③档静默路由成 existing、畸形 JSON 裸异常逃逸）；
  - 失败返回**裸 {"error": ...}**（finance 域约定）；BridgeBusy 必须先于 BridgeError
    接（前者是后者的子类，顺序颠倒丢「回测桥忙」前缀）。
"""
from __future__ import annotations

from app.agent.chain.backtest_bridge import (
    BridgeBusy,
    BridgeError,
    gc_jobs,
    parse_carrier,
    run_backtest_job,
)


def _preview(summary: dict) -> str:
    """O(摘要) 预览：主 agent 只读这一段；明细在 report_path/summary_path。"""
    if not summary.get("ok"):
        return f"回测失败: {summary.get('error', '')[:500]}"
    s = summary.get("stats") or {}
    return (f"策略 {summary.get('strategy_key')} | {summary.get('scope')} "
            f"days={summary.get('days')} | 样本 {s.get('n', 0)} "
            f"(有效 {s.get('n_valid', '?')}) | 胜率 {s.get('winrate', '?')}% "
            f"| 盈亏比 {s.get('pl_ratio', '?')} | 耗时 {summary.get('elapsed_seconds')}s\n"
            f"全量报告: {summary.get('report_path')}")


def run(scope: str = "all", days: int = 365, strategy_key: str = "",
        proposal_json: str = "", strategy_py: str = "", strategy_yaml: str = "",
        codes: str = "") -> dict:
    """skill_run.py 白名单入口（fn=run）。入参由 run_skill 的 spec["kwargs"] 展开。

    载体三选一：proposal_json（①/②档，schema 真源 app/market_cn/auto/tools/proposal.py）/
    strategy_py+strategy_yaml（③档成对，yaml 为 JSON 字符串）/ 只传 strategy_key
    （existing 对照组）。成功 = 桥 summary 全键 + preview（O(摘要)，明细留在
    report_path）；失败 = 裸 {"error": ...}。
    """
    gc_jobs()                                  # 顺手 TTL 归档（幂等、廉价）
    try:
        carrier, code_list, days_int = parse_carrier(
            proposal_json=proposal_json, strategy_py=strategy_py,
            strategy_yaml=strategy_yaml, strategy_key=strategy_key,
            codes=codes, days=days)
        summary = run_backtest_job(
            carrier=carrier, scope=scope, codes=code_list,
            days=days_int, strategy_key=strategy_key)
        return {**summary, "preview": _preview(summary)}
    except BridgeBusy as e:
        return {"error": f"回测桥忙: {e}"}
    except BridgeError as e:
        return {"error": str(e)}
