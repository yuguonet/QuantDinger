# -*- coding: utf-8 -*-
"""
app/services/strategy_loader.py — YAML 策略加载器
从 strategies/ 目录加载 .yaml 策略文件，供 Agent 使用。
移植自 daily_stock_analysis 项目。
"""
import glob
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 项目根目录下的 strategies/ 目录
_DEFAULT_STRATEGY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "strategies",
)

# 进程级缓存
_loaded_strategies: Optional[List[Dict[str, Any]]] = None
_loaded_map: Optional[Dict[str, Dict[str, Any]]] = None


def _parse_yaml_file(filepath: str) -> Optional[Dict[str, Any]]:
    """解析单个 YAML 策略文件。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning("策略文件 %s 格式异常，跳过", filepath)
            return None

        # 必填字段
        name = data.get("name")
        display_name = data.get("display_name")
        if not name or not display_name:
            logger.warning("策略文件 %s 缺少 name/display_name，跳过", filepath)
            return None

        return {
            "id": name,
            "name": display_name,
            "description": data.get("description", ""),
            "category": data.get("category", "general"),
            "core_rules": data.get("core_rules", []),
            "required_tools": data.get("required_tools", []),
            "aliases": data.get("aliases", []),
            "default_active": data.get("default_active", False),
            "default_router": data.get("default_router", False),
            "default_priority": data.get("default_priority", 999),
            "market_regimes": data.get("market_regimes", []),
            "instructions": data.get("instructions", ""),
        }
    except Exception as e:
        logger.error("解析策略文件 %s 失败: %s", filepath, e, exc_info=True)
        return None


def load_strategies(
    strategy_dir: Optional[str] = None,
    custom_dir: Optional[str] = None,
    force_reload: bool = False,
) -> List[Dict[str, Any]]:
    """
    加载所有 YAML 策略文件。

    Args:
        strategy_dir: 内置策略目录（默认 strategies/）。
        custom_dir: 自定义策略目录（环境变量 AGENT_SKILL_DIR）。
        force_reload: 强制重新加载，忽略缓存。

    Returns:
        按 default_priority 升序排列的策略列表。
    """
    global _loaded_strategies, _loaded_map

    if _loaded_strategies is not None and not force_reload:
        return _loaded_strategies

    dirs: List[str] = []

    # 1) 内置策略目录
    builtin = strategy_dir or _DEFAULT_STRATEGY_DIR
    if os.path.isdir(builtin):
        dirs.append(builtin)
        logger.info("内置策略目录: %s", builtin)
    else:
        logger.warning("内置策略目录不存在: %s", builtin)

    # 2) 自定义策略目录（环境变量）
    if custom_dir is None:
        custom_dir = os.environ.get("AGENT_SKILL_DIR")
    if custom_dir and os.path.isdir(custom_dir):
        dirs.append(custom_dir)
        logger.info("自定义策略目录: %s", custom_dir)

    # 3) 扫描 YAML 文件
    seen_names: set = set()
    strategies: List[Dict[str, Any]] = []

    for d in dirs:
        pattern = os.path.join(d, "*.yaml")
        for filepath in sorted(glob.glob(pattern)):
            parsed = _parse_yaml_file(filepath)
            if parsed is None:
                continue
            name = parsed["id"]
            if name in seen_names:
                # 自定义策略覆盖内置策略
                strategies = [s for s in strategies if s["id"] != name]
                logger.info("策略 '%s' 被自定义版本覆盖: %s", name, filepath)
            seen_names.add(name)
            strategies.append(parsed)

    # 4) 按优先级排序
    strategies.sort(key=lambda s: s.get("default_priority", 999))

    _loaded_strategies = strategies
    _loaded_map = {s["id"]: s for s in strategies}

    logger.info("共加载 %d 个策略: %s", len(strategies), [s["id"] for s in strategies])
    return strategies


def get_strategy_by_id(strategy_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 获取单个策略。"""
    if _loaded_map is None:
        load_strategies()
    return _loaded_map.get(strategy_id)


def get_strategy_names() -> List[Dict[str, str]]:
    """
    返回策略列表（仅 id + name + description + category），
    用于前端下拉菜单。
    """
    strategies = load_strategies()
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "description": s["description"],
            "category": s["category"],
        }
        for s in strategies
    ]
