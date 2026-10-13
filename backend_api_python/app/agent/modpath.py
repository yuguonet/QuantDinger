# -*- coding: utf-8 -*-
"""包内**动态**模块名解析 —— 单一真源。

【为什么需要它】
P5 把包内静态导入改成了相对导入，运行时模块名统一为 `app.agent.*`。
但有一类导入是**字符串驱动**的，静态改写碰不到：
  · `importlib.import_module(f"tools.{module_name}")`        （手工拼的裸名）
  · admission.json / DB 定时任务里存的 `"cron.cron_tools.xxx"`（**存量数据**）
这些字符串在改造前是裸名，改造后必须解析成绝对名，否则全部 ImportError。
更麻烦的是存量数据（admission.json、DB 里用户已配置的定时任务）**不会跟着代码
改** —— 所以这里做的是**兼容解析**，新旧写法都认。

【规则】
  已以 `app.` 开头（包外绝对名，如 `app.market_cn.auto.store`）→ 原样
  历史全名 `app.agent_legacy.*`（2026-10-13 P6 改名前的本包名）→ 归一化为现包名
  其余（本包内裸名，如 `tools.finance.xxx`）→ 前置 `app.agent.`

【改名友好】包根取自 `__package__`，目录重命名时本文件零改动。

★ 为什么要认 `app.agent_legacy.*` 这个历史名
  本包先后叫过 `app.agent`（最初）→ `app.agent_legacy`（给新版让位）
  → `app.agent`（P6：新版迁 agentcore，名字归还本包）。
  代码可以整体替换，但**存量数据不会跟着改**：DB 定时任务表
  （`qd_agent_cron_jobs.function_path`）、admission.json 里已经存下的
  `app.agent_legacy.*` 字符串，改名后就指向一个不存在的包。
  这种断裂只在运行到那一行时才炸，测试全绿也照炸 —— 所以必须在
  解析层归一化，而不是指望"以后不会有这种数据"。
"""
import importlib

__all__ = ["PKG", "resolve", "import_module", "resolve_function_path"]

# 本包的绝对名（`app.agent`）。顶层模块，`__package__` 即包根。
PKG = __package__ or "app.agent"

# 本包的**历史名**：P6 改名前的包根。认它只为兜住存量数据，
# 新写的数据一律用现包名，这条别名在存量数据清空后可删。
_ALIAS_PKGS = ("app.agent_legacy",)


def resolve(module_name: str) -> str:
    """把"可能的裸名"解析成绝对模块名。

    >>> resolve("tools.finance.kline_tools")     # 本包裸名
    'app.agent.tools.finance.kline_tools'
    >>> resolve("app.market_cn.auto.store")      # 包外绝对名，原样
    'app.market_cn.auto.store'
    >>> resolve("app.agent_legacy.cron.cron_tools")   # 历史全名 → 归一化
    'app.agent.cron.cron_tools'
    """
    name = (module_name or "").strip()
    if not name:
        return name
    if name.startswith(f"{PKG}.") or name == PKG:
        return name
    # 历史包名 → 现包名（存量数据兼容，见模块 docstring）
    for alias in _ALIAS_PKGS:
        if name == alias or name.startswith(f"{alias}."):
            return PKG + name[len(alias):]
    if name.startswith("app."):
        return name
    return f"{PKG}.{name}"


def import_module(module_name: str):
    """`importlib.import_module` 的兼容版：裸名自动补包前缀。"""
    return importlib.import_module(resolve(module_name))


def resolve_function_path(path: str) -> tuple[str, str]:
    """拆分 `"module.path.func_name"` → `(绝对模块名, 函数名)`。

    用于 DB 里存的定时任务函数路径（存量是裸名，新写的可以是绝对名）。
    """
    module_path, _, func_name = (path or "").rpartition(".")
    if not module_path:
        raise ImportError(f"非法函数路径（缺模块名）: {path!r}")
    return resolve(module_path), func_name
