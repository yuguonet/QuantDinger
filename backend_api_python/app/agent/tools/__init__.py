# -*- coding: utf-8 -*-
"""
Agent tools — self-mounting tool discovery.

Each tool file defines a TOOL_SPEC list at module level:
    TOOL_SPEC = [
        {"fn": callable, "name": str, "description": str, "parameters": dict},
        ...
    ]

This __init__.py auto-discovers all TOOL_SPEC from sibling modules,
so adding/removing a tool file requires zero config changes.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import pathlib
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def discover_tools() -> List[Dict[str, Any]]:
    """Scan this package for TOOL_SPEC declarations and return flat list."""
    tools: List[Dict[str, Any]] = []
    pkg_dir = pathlib.Path(__file__).parent

    for importer, module_name, is_pkg in pkgutil.iter_modules([str(pkg_dir)]):
        if module_name.startswith("_") or is_pkg:
            continue
        # Skip non-tool modules (e.g. labels.py)
        if module_name in ("labels",):
            continue

        try:
            mod = importlib.import_module(f".{module_name}", __package__)
        except Exception as e:
            logger.warning("[tools] Failed to import %s: %s", module_name, e)
            continue

        spec = getattr(mod, "TOOL_SPEC", None)
        if spec is None:
            continue

        if isinstance(spec, list):
            tools.extend(spec)
        elif isinstance(spec, dict):
            tools.append(spec)
        else:
            logger.warning("[tools] %s.TOOL_SPEC has unexpected type: %s", module_name, type(spec))

    logger.info("[tools] Discovered %d tools from %s", len(tools),
                [t.get("name", "?") for t in tools])
    return tools
