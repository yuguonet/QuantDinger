# -*- coding: utf-8 -*-
"""预选「空的兜底BOSS」观测计数器 —— 先看概率，再决定要不要救。

背景（2026-10-01 用户裁定）：
    旧系统 `agents/task_agent.py` 的第三层兜底是「空域不许静默」：域选择为空时
    打 warning 并落 trace 标 `plan_domain_empty_degraded`。本系统迁移后这层没了，
    但我和用户都**不确定它实际发生的概率**——如果万分之一，加修复逻辑是过度设计；
    如果百分之几，那是每天几十次的静默降级，必须救。

    ⇒ 先装计数器跑一段，**用数据决定**，不猜。

如何使用（三步走）：
    Step 1（现在）：集成计数器，读日志 / 调 `report()` 看占比。
    Step 2（数据说话）：按下面的阈值表判断。
    Step 3（达标才动手）：按 `DECISION_NOTE` 里的处置方法改。

升级阈值（**达到任一**才值得动手，别提前优化）：
    * empty_rate > 2%          → 域词典 `DATA_DOMAIN_TOOLS` 缺口，补词（成本最低，先做这个）
    * empty_rate > 5%          → 闸门 `QD_PRESELECT_GATE` 关键词需放宽，把问法加到 hints
    * empty_rate > 10%         → 预选 prompt 或模型有问题，先看 Route A/B/C 哪条在漏
    * 单 domain 连续 N>=20 命中 → 该域该单独考察（可能是某个具体问法没覆盖）

注意：计数器是**进程内**的，重启归零。要看长期趋势得落库或进日志聚合，
短期内以 warning 日志为准（`grep "preselect-empty-face"`）。
"""

import time
from collections import Counter, deque
from typing import Any, Deque, Dict, List

logger = __import__("logging").getLogger(__name__)

# 最近样例保留条数（够看规律，又不占内存）
_SAMPLE_LIMIT = 50

_LATENCY = {"total": 0, "empty": 0}
_ROUTES: Counter = Counter()          # 触发原因 → 次数
_DOMAINS: Counter = Counter()         # 兜底也没命中的 #domain → 次数
_SAMPLES: Deque[Dict[str, str]] = deque(maxlen=_SAMPLE_LIMIT)
_STARTED = time.time()


def record_selected(route: str = "ok") -> None:
    """记一次「预选有结果」（分母）。

    Args:
        route: 来源标记，便于事后看是哪条路径产出的（ok / lint_add / fallback）。
    """
    _LATENCY["total"] += 1
    _ROUTES[route or "ok"] += 1


def record_empty_face(query: str = "", route: str = "", domain_count: int = 0) -> Dict[str, Any]:
    """记一次「工具面跑空」——预选空手且域兜底也没补上。

    这里是第三层兜底的替身：**不静默**。当前只报警+计数，不自动救，
    因为还没证明值不值得救（见模块 docstring 的阈值表）。

    Args:
        query: 触发的用户问句（截断入库，够定位问法就行）。
        route: 走到空手的原因（gate / no_pick / lint_void / fallback_miss …）。
        domain_count: 当时可用候选工具数（0 说明分级本身就是空的，另回事）。

    Returns:
        本次统计摘要（同时写 warning 日志）。
    """
    _LATENCY["total"] += 1
    _LATENCY["empty"] += 1
    _ROUTES[route or "unknown"] += 1
    sample = {"ts": time.strftime("%H:%M:%S"), "route": route or "unknown",
              "q": (query or "")[:60]}
    _SAMPLES.append(sample)

    rate = empty_rate()
    # 只在比例**抬头过阈值**时刷 warning，避免每条都刷把日志淹了
    level = "warning" if rate > 0.02 else "info"
    getattr(logger, level)(
        "[preselect-empty-face] 工具面跑空 route=%s rate=%.2f%% (%d/%d) q=%s",
        route, rate * 100, _LATENCY["empty"], _LATENCY["total"], sample["q"])
    return {"empty": _LATENCY["empty"], "total": _LATENCY["total"],
            "rate": rate, "sample": sample}


def empty_rate() -> float:
    """空域占比 0~1。"""
    return (_LATENCY["empty"] / _LATENCY["total"]) if _LATENCY["total"] else 0.0


def report() -> Dict[str, Any]:
    """一次性健康视图：占比 / 分原因 / 最近样例 / 当前该不该动手。"""
    rate = empty_rate()
    return {
        "total": _LATENCY["total"],
        "empty": _LATENCY["empty"],
        "empty_rate": round(rate, 4),
        "by_route": dict(_ROUTES.most_common()),
        "by_domain_miss": dict(_DOMAINS.most_common()),
        "recent_samples": list(_SAMPLES)[-10:],
        "uptime_sec": int(time.time() - _STARTED),
        "verdict": _verdict(rate),
    }


def _verdict(rate: float) -> str:
    """按阈值表给结论（**只解释，不改变行为**）。"""
    if _LATENCY["total"] < 50:
        return "样本不足(<%d)：继续观测，别下结论" % 50
    if rate > 0.10:
        return "严重(>10%)：查预选 prompt / 模型调用本身"
    if rate > 0.05:
        return "偏高(>5%)：放宽 QD_PRESELECT_GATE 关键词"
    if rate > 0.02:
        return "轻微(>2%)：补 DATA_DOMAIN_TOOLS 词典"
    return "健康(<=2%)：保持现状，无需处置"


DECISION_NOTE = """
【达到阈值后怎么改】（按性价比从高到低）
  1. 补词典 `tool_preselect.DATA_DOMAIN_TOOLS`：零成本，先补这里。命中少了自然会漏。
  2. 放宽闸门 `QD_PRESELECT_GATE` hints：闸门是省钱的成本闸，不是"不需要工具"的判决，
     关键词覆盖不到会造成"本该给工具面却没给"。
  3. 给该域加 fallback 条目：`fallback_domain_tools` 只补核心 4 个，够用即可，别贪。
  4. 真要恢复旧系统的"整域注入"——**不要**。本系统 tool-calling 下整域 ≈15k tok/轮。
【禁做的事】为了让 rate 好看而放宽 intake_gate（那是 v1 的另一回事，混进来会污染样本）。
"""


def reset() -> None:
    """清零（仅供测试）。"""
    _LATENCY.update(total=0, empty=0)
    _ROUTES.clear()
    _DOMAINS.clear()
    _SAMPLES.clear()


__all__ = ["record_selected", "record_empty_face", "empty_rate", "report",
           "reset", "DECISION_NOTE"]
