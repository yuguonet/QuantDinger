# -*- coding: utf-8 -*-
"""
formatters — 结果格式化/汇总模块

职责：将 CodeAgent 的原始输出汇总为结构化报告。

设计：
  - selected_skill 有值时跳过（SKILL.md 已定义输出规范）
  - 无 skill 时，按 domain（优先）→ entity_type 选择 formatter
  - 无匹配 formatter 时，使用 default formatter

多领域扩展（2026-09-13）：
  新增一个领域的标准化输出 = 在本目录放 `formatters/<domain>.py`，用
  `@register_formatter("<domain>")` 装饰即可，**无需修改本文件**——本文件导入时
  会自动发现并加载目录下所有模块以触发注册。

目录：
  base.py     — BaseFormatter 基类
  default.py  — 通用兜底（纯 LLM 自适应）
  finance.py  — 金融领域模板
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import BaseFormatter, get_formatter, register_formatter

logger = logging.getLogger(__name__)

# 不在自动发现范围内：base 是注册表自身；default 由 get_formatter 兜底时按需导入
_AUTOLOAD_SKIP = frozenset({"base", "default"})


def _autoload_formatters():
    """导入本包下所有 formatter 模块以触发 @register_formatter 注册。

    易错点（2026-09-13 修复）：此处原先是一行被注释掉的 `from . import finance`
    —— finance.py 的注册从未执行，_REGISTRY 恒为空，get_formatter() 恒返回
    DefaultFormatter，finalize 的领域格式化段形同虚设（且每次仍多花一次 LLM
    调用）。改为自动发现后，"新增领域忘记加 import" 这类断链不会再现。

    Returns:
        (loaded, failed)：failed 项只记录不影响其余模块加载。
    """
    loaded, failed = [], []
    for info in pkgutil.iter_modules(__path__):
        name = info.name
        if name.startswith("_") or name in _AUTOLOAD_SKIP:
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
            loaded.append(name)
        except Exception as e:  # 单个 formatter 不可用不应拖垮整个 finalize
            failed.append(f"{name}({type(e).__name__}: {e})")
    return loaded, failed


_loaded_modules, _failed_modules = _autoload_formatters()
if _failed_modules:
    logger.warning("[Formatter] %d 个 formatter 模块导入失败（已跳过）: %s",
                   len(_failed_modules), "；".join(_failed_modules))
elif _loaded_modules:
    logger.debug("[Formatter] 已自动加载领域 formatter: %s", ", ".join(_loaded_modules))

__all__ = ["BaseFormatter", "get_formatter", "register_formatter"]
