# -*- coding: utf-8 -*-
"""
Tool Chains — 工具链读写接口。

数据存储在同目录的 tool_chains.json 中。
agent 可通过 read/write 接口自主维护工具链。
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_JSON_PATH = pathlib.Path(__file__).parent / "tool_chains.json"


def _load() -> Dict[str, List[Dict[str, str]]]:
    """加载工具链配置。"""
    if not _JSON_PATH.exists():
        return {}
    try:
        return json.loads(_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[ToolChains] 加载失败: %s", e)
        return {}


def _save(data: Dict[str, List[Dict[str, str]]]):
    """保存工具链配置。"""
    _JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def get_tool_chain(verb: str, noun: str) -> List[Dict[str, str]]:
    """获取指定动作+对象的工具链。

    Returns:
        [{"tool": "tool_name", "desc": "描述"}, ...]
        未配置时返回空列表。
    """
    data = _load()
    return data.get(f"{verb}+{noun}", [])


def save_tool_chain(verb: str, noun: str, chain: List[Dict[str, str]]):
    """保存工具链（agent 自主学习后调用）。"""
    data = _load()
    key = f"{verb}+{noun}"
    data[key] = chain
    _save(data)
    logger.info("[ToolChains] 保存工具链: %s → %s", key, [s["tool"] for s in chain])


def list_all_chains() -> Dict[str, List[Dict[str, str]]]:
    """列出所有已配置的工具链。"""
    return _load()
