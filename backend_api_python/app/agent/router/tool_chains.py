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
    """获取指定动作+对象的工具链步骤列表。

    Returns:
        [{"tool": "tool_name", "desc": "描述", "args": {...}}, ...]
        未配置时返回空列表。
    """
    data = _load()
    entry = data.get(f"{verb}+{noun}")
    if not entry:
        return []
    return _normalize_entry(entry).get("steps", [])


def get_chain_stats(verb: str, noun: str) -> Dict[str, Any]:
    """获取指定链路的统计数据。"""
    data = _load()
    entry = data.get(f"{verb}+{noun}")
    if not entry:
        return _empty_stats()
    return _normalize_entry(entry).get("stats", _empty_stats())


def save_tool_chain(verb: str, noun: str, chain: List[Dict[str, str]]):
    """保存工具链（agent 自主学习后调用）。"""
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
