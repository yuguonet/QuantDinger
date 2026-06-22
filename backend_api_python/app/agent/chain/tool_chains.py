# -*- coding: utf-8 -*-
"""
Tool Chains — 工具链读写接口 + 统计。

数据存储在同目录的 tool_chains.json 中。
agent 可通过 read/write 接口自主维护工具链。

数据格式：
{
    "verb+noun": {
        "steps": [
            {"tool": "tool_name", "desc": "描述", "args": {...}},
            ...
        ],
        "stats": {
            "avg_steps": 2.3,       # Agent 实际平均步数
            "executions": 15,        # 总执行次数
            "success_count": 13,     # 成功次数
            "success_rate": 0.87,    # 成功率
            "last_updated": "2026-06-14"
        }
    }
}
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_JSON_PATH = pathlib.Path(__file__).parent / "tool_chains.json"


def _load() -> Dict[str, Any]:
    """加载工具链配置（兼容旧格式）。"""
    if not _JSON_PATH.exists():
        return {}
    try:
        return json.loads(_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[ToolChains] 加载失败: %s", e)
        return {}


def _save(data: Dict[str, Any]):
    """保存工具链配置。"""
    _JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def _normalize_entry(entry) -> Dict[str, Any]:
    """将旧格式（纯列表）转为新格式（{steps, stats}）。兼容两种格式。"""
    if isinstance(entry, list):
        # 旧格式：纯列表 → 包装为新格式
        return {"steps": entry, "stats": _empty_stats()}
    if isinstance(entry, dict) and "steps" in entry:
        # 新格式
        if "stats" not in entry:
            entry["stats"] = _empty_stats()
        return entry
    # 未知格式 → 空
    return {"steps": [], "stats": _empty_stats()}


def _empty_stats() -> Dict[str, Any]:
    return {
        "avg_steps": 0.0,
        "executions": 0,
        "success_count": 0,
        "success_rate": 0.0,
        "last_updated": "",
    }


def get_tool_chain(verb: str, noun: str) -> List[Dict[str, str]]:
    """获取指定动作+对象的工具链步骤列表（旧格式兼容）。"""
    data = _load()
    entry = data.get(f"{verb}+{noun}")
    if not entry:
        return []
    return _normalize_entry(entry).get("steps", [])


def get_chain_plan(verb: str, noun: str) -> Optional[Dict[str, Any]]:
    """获取完整 plan 结构（新格式）。返回 None 表示无缓存。"""
    data = _load()
    entry = data.get(f"{verb}+{noun}")
    if not entry:
        return None
    if isinstance(entry, dict) and "plan" in entry:
        return entry["plan"]
    return None  # 旧格式，无 plan


def get_chain_stats(verb: str, noun: str) -> Dict[str, Any]:
    """获取指定链路的统计数据。"""
    data = _load()
    entry = data.get(f"{verb}+{noun}")
    if not entry:
        return _empty_stats()
    return _normalize_entry(entry).get("stats", _empty_stats())


def save_tool_chain(verb: str, noun: str, chain: List[Dict[str, str]]):
    """保存工具链（旧格式兼容）。"""
    if not verb or not noun:
        logger.warning("[ToolChains] 拒绝保存残缺键: verb=%s noun=%s", verb, noun)
        return
    data = _load()
    key = f"{verb}+{noun}"
    existing = _normalize_entry(data.get(key, {}))
    existing["steps"] = chain
    data[key] = existing
    _save(data)
    logger.info("[ToolChains] 保存工具链: %s → %s", key, [s["tool"] for s in chain])


def save_chain_plan(verb: str, noun: str, plan: Dict[str, Any]):
    """保存完整 plan 结构（新格式）。"""
    if not verb or not noun:
        logger.warning("[ToolChains] 拒绝保存残缺键: verb=%s noun=%s", verb, noun)
        return
    data = _load()
    key = f"{verb}+{noun}"
    existing = data.get(key, {})
    if not isinstance(existing, dict):
        existing = {}
    if "stats" not in existing:
        existing["stats"] = _empty_stats()
    existing["plan"] = plan
    data[key] = existing
    _save(data)
    phases = plan.get("phases", [])
    logger.info("[ToolChains] 保存 plan: %s → %d phases", key, len(phases))


def update_chain_stats(
    verb: str,
    noun: str,
    steps_taken: int,
    success: bool,
):
    """更新链路统计（每次 Agent 执行后调用）。

    使用增量均值算法，无需存储历史数据。

    Args:
        verb: 动词
        noun: 名词
        steps_taken: Agent 实际步数
        success: 是否成功
    """
    if not verb or not noun:
        return

    data = _load()
    key = f"{verb}+{noun}"
    entry = _normalize_entry(data.get(key, {}))
    stats = entry["stats"]

    n = stats["executions"] + 1
    # 增量均值: new_avg = old_avg + (x - old_avg) / n
    old_avg = stats["avg_steps"]
    stats["avg_steps"] = round(old_avg + (steps_taken - old_avg) / n, 2)
    stats["executions"] = n
    if success:
        stats["success_count"] += 1
    stats["success_rate"] = round(stats["success_count"] / n, 3)
    stats["last_updated"] = date.today().isoformat()

    entry["stats"] = stats
    data[key] = entry
    _save(data)

    logger.info(
        "[ToolChains] 统计更新: %s avg_steps=%.1f executions=%d success_rate=%.2f",
        key, stats["avg_steps"], stats["executions"], stats["success_rate"],
    )


def list_all_chains() -> Dict[str, Any]:
    """列出所有已配置的工具链。"""
    return _load()


# ═══════════════════════════════════════════════════════════════
# 用户反馈惩罚
# ═══════════════════════════════════════════════════════════════

# 轻度负面反馈关键词
_MILD_FEEDBACK_PATTERNS = [
    "不对", "不正确", "不好", "不行", "不准", "不太对",
    "有问题", "有误", "错了", "不太行", "不靠谱", "不靠谱",
]

# 重度负面反馈关键词
_SEVERE_FEEDBACK_PATTERNS = [
    "完全不对", "大错特错", "错得离谱", "离谱", "反了",
    "完全错", "一塌糊涂", "乱七八糟", "瞎扯", "胡说",
    "垃圾", "废了", "没用", "一点用没有",
]


def detect_feedback_severity(message: str) -> Optional[str]:
    """检测用户消息中的负面反馈。

    Returns:
        "severe" — 重度负面（删链路）
        "mild"   — 轻度负面（降信誉）
        None     — 无负面反馈
    """
    if not message:
        return None
    msg = message.strip()

    # 先检测重度（更长的模式优先）
    for pat in _SEVERE_FEEDBACK_PATTERNS:
        if pat in msg:
            return "severe"

    # 再检测轻度
    for pat in _MILD_FEEDBACK_PATTERNS:
        if pat in msg:
            return "mild"

    return None


def penalize_chain(verb: str, noun: str, severity: str) -> bool:
    """对链路施加惩罚，累计 2-4 次后自动删除。

    轻度（不对/不好/不正确）→ success_count -= 1
    重度（完全不对/大错特错/反了/离谱）→ success_count -= 2

    当 success_count <= 0 或 success_rate < 0.2 时，直接删除链路。

    Args:
        verb: 动词（如 "analyze"）
        noun: 名词（如 "stock"）
        severity: "mild" 或 "severe"

    Returns:
        是否成功执行惩罚
    """
    if not verb or not noun:
        return False

    key = f"{verb}+{noun}"
    data = _load()

    if key not in data:
        logger.warning("[ToolChains] 惩罚目标不存在: %s", key)
        return False

    entry = _normalize_entry(data[key])
    stats = entry["stats"]

    # 扣减
    penalty = 2 if severity == "severe" else 1
    stats["success_count"] = max(0, stats["success_count"] - penalty)

    # 重算 success_rate
    if stats["executions"] > 0:
        stats["success_rate"] = round(
            stats["success_count"] / stats["executions"], 3
        )
    stats["last_updated"] = date.today().isoformat()

    # 判断是否该删除：success_count 归零 或 成功率过低
    should_delete = (
        stats["success_count"] <= 0
        or (stats["executions"] >= 3 and stats["success_rate"] < 0.2)
    )

    if should_delete:
        del data[key]
        _save(data)
        logger.info(
            "[ToolChains] 累计惩罚触发删除: %s (penalty=%d, success_count=%d, success_rate=%.3f)",
            key, penalty, stats["success_count"], stats["success_rate"],
        )
        return True

    entry["stats"] = stats
    data[key] = entry
    _save(data)
    logger.info(
        "[ToolChains] 惩罚: %s -%d → success_count=%d success_rate=%.3f",
        key, penalty, stats["success_count"], stats["success_rate"],
    )
    return True
