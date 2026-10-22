# -*- coding: utf-8 -*-
"""ToolProvider —— 统一工具注册表（一次扫描，两种输出：函数 + schema）。

★ 来源：从 `tools/_base.py`（1025 行）拆出（2026-10-13，P4）。
  依赖方向：provider → schema_gen → （无内部依赖）。单向，无环。

★ 为什么这份注册表不能随便 new
  `get_or_build()` 里固化了**唯一构建口径**（扫顶层 domain=common + 扫子目录
  domain=子目录名 + 装配元工具表）。此前 qd_agent._scan_tool_provider 另写了一份，
  消费方各自 `ToolProvider()` 现造就拿不到元工具 —— 历史事故：web_search 因只在
  元工具表、注册表恒 None 而整体不可用。消费方一律走 get_or_build()。

★ 两张表，别混用（2026-09-21 事故根因）
  - `_tools`：注册表。get() / get_functions() / get_schemas() 走这里。
  - `_meta_tools`：元工具表（`_MUST_HAVE` 模块的公开函数），只由 get_meta() 取。
  元工具（web_search / format_result）必须**每个阶段都在场**，包括 planner 显式
  声明 0 工具的阶段，所以走独立通道而不能并进注册表。
  消费方误用 get() 取元工具 ⇒ 恒 None ⇒ 联网搜索整体不可用。

★ 包前缀推导
  `cls.__module__.rsplit(".", 1)[0]` 兼容两种跑法：生产以 backend_api_python 为根
  （app.agent.tools）与 cli 把 app/agent 放进 sys.path（tools）。
"""
from __future__ import annotations

import importlib
import inspect
import logging
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ._schema_gen import _is_tool_function, func_to_openai_schema

logger = logging.getLogger(__name__)


# ── 扫描规则：**目录即契约**，零名单 ──────────────────────────────
#
# ★ 2026-10-13 P10：此前的 `_SKIP_FILES` 已整体删除。
#   它是"非工具模块住在自发现目录里"的补丁，代价是要手工维护（曾有两份，分别在
#   provider.py 与 mcp_bridge.py，且**实测已漂移**：mcp 侧漏登记 availability ⇒
#   启动期自检函数被注册成 MCP 工具暴露出去）。
#
#   现在的规则只有一条：**文件名以 `_` 开头的模块不是工具模块**，扫描器天然跳过。
#   非工具模块（`_base` / `_provider` / `_schema_gen` / `_serialize` / `_fn_adapter`
#   / `_pagination` / `_availability`）全部改成下划线前缀即可 —— 零名单、零同步、
#   新增内核模块只要按下划线命名就自动退出扫描。
#   `__init__.py` 也以 `_` 开头，同一规则覆盖，不需要单独登记。

# 元工具模块（通过 smolagents tools=[] 注入，provider **注册表**不扫描）。
#
# 【易错点·2026-09-21】"不扫描"的后果是这两个模块的函数**不在注册表里**：
#   provider.get("web_search") / provider.get("format_result") 恒为 None。
# 消费方（task_agent.py 的 _WebSearchTool / _FormatResultTool）**只能**用
# provider.get_meta(name) —— 它查的是 _load_meta_tools 装出来的元工具表。
# 2026-09-21 事故正是消费方误用了 get()：web_search 直接返回
# {"error": "web_search 工具不可用"}、format_result 静默降级成 str(result)[:2000]。
# 注：format_utils 模块已于 2026-10-22 移出（其 LLM 版 format_result 由 mimo 原生
# 元工具 + 各工具内联 TSV 取代，见 del/format_utils.py），此处不再列入必加载。
_MUST_HAVE = {"web_search_tools"}

# 工具模块导入失败登记表（2026-10-02）：`_load_domain_tools` 遇到 ImportError 只
# warning 后 `continue`，属于**静默降级**——日志没人盯时工具面悄悄缺一大块，而
# 能力层会自动补位把缺口盖住。这里登记一份，供 `qd_smoke.py::
# test_30_toolface_import_health` 断言为空；也便于运行时自检直接读出。
#
# ★ 这是**可变共享状态**：`tools/_base.py` 用 `from ._provider import ...` 原样
#   re-export（不是重建 list），否则登记与读取落到两个对象 ⇒ 登记表永远为空
#   ⇒ 工具模块导入失败再也不会被发现。
_TOOL_MODULE_IMPORT_FAILURES: list = []

# 【零名单纪律·2026-10-22 AP.8】tools/ 目录（含子目录）**全部自发现**，不再有
# 任何"模块级/函数级屏蔽名单"。旧工具归组化（2026-09-27）中被聚合工具取代的
# 18 个实现（get_market_fund_flow/search_stock_intel/analyze_trend 等）已降级为
# `_` 前缀私有函数（_get_market_fund_flow/_search_stock_intel/_analyze_trend 等），
# 天然退出 scan_directory 自发现；其公开入口由 get_fund_flow/search_intel/
# technical_analysis 等统一工具承接。能力层同名让位判定继续依赖这些私有实现
# 存在（loader.py 先注册者胜出），故实现保留、仅不注册。
# 历史沿革（indicator_tools/backtest_tools 下线与恢复，附录 AN）见 AGENT_DESIGN_v3.md。

# ★ 工具归并的权重迁移表已整体删除（2026-10-22 用户裁定）。
#   原 `TOOL_ALIAS` / `TOOL_WEIGHT_MIGRATION` 映射（18 个旧工具名 → 新工具名）不再维护：
#   - 数据库实测 `qd_agent_weights(layer='tool')` 旧工具名行 = 0，映射表空转从未命中；
#   - evaluator 对「工具面已消失的旧名行」直接 DELETE 清理，不做权重迁移；
#   - 未来工具归并时，旧名权重行由人工一次性 UPDATE 改名，不靠常驻映射表。


class ToolProvider:
    """统一工具注册表。

    注册一次，两种输出：
      - get_functions() → executor 用（name → callable）
      - get_schemas()   → planning 用（OpenAI Function Calling schema）
    """

    # 模块级单例，由 init_tools() 设置
    _default: Optional["ToolProvider"] = None
    _default_lock = threading.Lock()

    @classmethod
    def set_default(cls, provider: "ToolProvider"):
        """设置全局默认 provider。"""
        cls._default = provider

    @classmethod
    def get_default(cls) -> Optional["ToolProvider"]:
        """获取全局默认 provider。"""
        return cls._default

    @classmethod
    def get_or_build(cls) -> "ToolProvider":
        """取全局默认 provider；不存在则**按统一口径**扫描构建并缓存。

        【为什么要有这个方法·2026-10-01】provider 的构建口径（扫顶层 domain=common
        + 扫子目录 domain=子目录名 + 元工具表）此前在 qd_agent._scan_tool_provider
        里**另写了一份**。工具分层需要「元工具（web_search/format_result）也必须拿到」，
        消费方若各自 `ToolProvider()` 现造一个，就拿不到 get_meta() 里的元工具
        （历史事故：web_search 因只在元工具表、注册表恒 None 而整体不可用）。
        单一口径收敛到此处，消费方一律 get_or_build()。

        包前缀从本模块的 __module__ 推导（app.agent.tools / tools），
        兼容"生产以 backend_api_python 为根"与"cli 把 app/agent 放进 sys.path"两种跑法。
        """
        provider = cls.get_default()
        if provider is not None:
            return provider
        with cls._default_lock:
            provider = cls.get_default()
            if provider is not None:
                return provider
            tools_dir = Path(__file__).resolve().parent
            package_prefix = cls.__module__.rsplit(".", 1)[0]  # app.agent.tools | tools
            provider = cls()
            provider.scan_directory(tools_dir, domain="common", package_prefix=package_prefix)
            provider.scan_subdirectories(tools_dir, package_prefix=package_prefix)
            cls.set_default(provider)
            return provider

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._domains: Dict[str, str] = {}       # name → domain
        # 元工具（_MUST_HAVE 模块的公开函数）：**不进注册表**、不进 planner schema，
        # 仅供 task_agent 的包装层按工具名取实现。见 _load_meta_tools 的说明。
        self._meta_tools: Dict[str, Callable] = {}
        # 可被 selected_domain 选中的域，只由 tools/<子目录> 推导（见 scan_subdirectories）
        self._selectable_domains: set = set()
        self._schema_cache: Optional[List[dict]] = None

    # ── 扫描注册 ──────────────────────────────────────────────

    # ★ 2026-10-19 修：package_prefix 默认值原为硬编码 "tools"（sys.path 时代
    #   口径）——P5 起 app/agent 不进 sys.path，生产/CLI 都只有 app.agent.tools
    #   可达，调用方一旦省略该参数即全员 ModuleNotFoundError（evaluator/skill_brewer
    #   三处自建扫描就是这样挂的，工具面同步静默失效）。
    #   默认改为从本模块 __name__ 推导（app.agent.tools | tools 两种加载法均正确），
    #   与 get_or_build() 同源；显式传参的调用方不受影响。
    def scan_directory(self, tools_dir: Path, domain: str = "common",
                       package_prefix: str = __name__.rsplit(".", 1)[0],
                       load_meta: bool = True):
        """扫描目录下所有 .py，自动注册公开函数。

        `_MUST_HAVE` 里的模块**不进注册表**（下方 continue 跳过），但它们的公开函数
        会被装进独立的元工具表 —— 两件事同源，故都在本方法内完成，避免新增调用点
        被遗漏（元工具表漏装配 ⇒ 联网搜索/格式化整体不可用，2026-09-21 事故）。

        Args:
            tools_dir: 目录路径
            domain: 领域名（默认 common）
            package_prefix: 导入包前缀。**默认从本模块 __name__ 推导**
                （app.agent.tools）——勿改回硬编码 "tools"，见上方 2026-10-19 注释。
            load_meta: 是否顺带装配元工具表。子目录扫描传 False —— 元工具模块只在
                tools/ 顶层，对每个子目录都找一遍会刷出一堆"模块缺失"假告警。
        """
        if load_meta:
            self._load_meta_tools(tools_dir, package_prefix)
        for py_file in sorted(tools_dir.glob("*.py")):
            module_name = py_file.stem
            if module_name.startswith("_") or module_name in _MUST_HAVE:
                continue
            try:
                mod = importlib.import_module(f"{package_prefix}.{module_name}")
            except Exception as _mod_err:
                # 2026-09-22（缺陷清单 Bug2A）：debug→warning。新工具文件 import 挂了
                # 只有 debug 能看到 = 静默消失，工具少注册无人知。warning 不影响流程。
                # 2026-10-02：warning 仍不够——日志没人盯时依旧是**静默降级**
                # （曾因 sys.path 错序导致 9 个 finance 模块一起挂掉，工具面从 101
                #  掉到 91，而能力层补位掩盖了缺口）。故同时落进模块级列表，
                #  由 `qd_smoke.py::test_30_toolface_import_health` 断言为空。
                logger.warning("[ToolProvider] 模块 %s 导入失败，跳过注册（%s: %s）",
                               module_name, type(_mod_err).__name__, _mod_err)
                try:
                    _TOOL_MODULE_IMPORT_FAILURES.append(
                        f"{module_name}: {type(_mod_err).__name__}: {_mod_err}")
                except NameError:      # 兜底：列表未定义时不因护栏打断注册
                    pass
                continue
            self._register_module_functions(mod, domain)

    # ── 元工具表（_MUST_HAVE）──────────────────────────────────
    #
    # 【为什么与注册表分开·2026-09-21】元工具（web_search / format_result）必须
    # **每个阶段都在场**，包括 planner 显式声明 0 工具的阶段 —— 而 phase 白名单分支
    # （task_agent.py）只认白名单、不与 common 域求并集，注册进注册表反而会被丢掉。
    # 故它们走 `tools=[]`（smolagents）通道；本表只做"工具名 → 实现"的绑定。
    #
    # 【易错点】消费方**只能**用 get_meta()，不能用 get()：get() 查的是注册表，
    # 这两个模块被 scan_directory 跳过 ⇒ 恒为 None。2026-09-21 事故即由此而来。
    # 好处是模块改名/迁移只改本文件的 _MUST_HAVE，不波及消费方（消费方只认工具名）。
    def _load_meta_tools(self, tools_dir: Path, package_prefix: str) -> None:
        for module_name in sorted(_MUST_HAVE):
            py_file = tools_dir / f"{module_name}.py"
            if not py_file.exists():
                logger.warning("[ToolProvider] 元工具模块缺失：%s", py_file)
                continue
            try:
                mod = importlib.import_module(f"{package_prefix}.{module_name}")
            except Exception:
                logger.error("[ToolProvider] 元工具模块导入失败：%s（该元工具不可用）",
                             module_name, exc_info=True)
                continue
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if _is_tool_function(obj, mod):
                    self._meta_tools[attr_name] = obj

    def scan_subdirectories(self, tools_dir: Path,
                            package_prefix: str = __name__.rsplit(".", 1)[0]):
        """扫描子目录，子目录名即 domain（同时登记为"可选域"）。

        可选域只来自目录：手动注册的来源层（如 capabilities）虽带 domain 标记，
        但不进 _selectable_domains，因此不会出现在 planner 的可用域清单里——
        "工具集域"与"工具来源层"不再混淆（2026-09-13，见 get_domains）。

        package_prefix 默认从本模块 __name__ 推导，理由同 scan_directory
        （2026-10-19：勿硬编码 "tools"）。
        """
        for sub in sorted(tools_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("_") or sub.name == "__pycache__":
                continue
            self._selectable_domains.add(sub.name)
            self.scan_directory(sub, domain=sub.name,
                                package_prefix=f"{package_prefix}.{sub.name}",
                                load_meta=False)  # 元工具只在 tools/ 顶层装配

    def register(self, name: str, func: Callable, domain: str = "common"):
        """手动注册单个函数。"""
        self._tools[name] = func
        self._domains[name] = domain
        self._schema_cache = None

    def register_module(self, module, domain: str = "common"):
        """注册模块中所有公开函数。"""
        self._register_module_functions(module, domain)

    def _register_module_functions(self, module, domain: str):
        """扫描模块公开函数并注册（判据见 schema_gen._is_tool_function）。

        零名单（2026-10-22 AP.8）：本方法不再有任何名单判定——`_is_tool_function`
        是唯一判据（`_` 前缀函数天然不注册），tools/ 目录（含子目录）全部自发现。
        """
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if not _is_tool_function(obj, module):
                continue
            self._tools[attr_name] = obj
            self._domains[attr_name] = domain
        self._schema_cache = None

    # ── 输出 ──────────────────────────────────────────────────

    def get_functions(self) -> Dict[str, Callable]:
        """executor 用：name → callable。"""
        return dict(self._tools)

    def get_meta(self, name: str) -> Optional[Callable]:
        """取元工具实现（`_MUST_HAVE` 模块的公开函数）；未装配返回 None。

        注意：与 get() 查的是**两张不同的表**——元工具只在 get_meta() 里，
        注册表工具只在 get() 里，互相取不到（2026-09-21 事故根因）。
        """
        return self._meta_tools.get(name)

    def get_meta_functions(self) -> Dict[str, Callable]:
        """全部元工具 {name: func}（2026-10-01）。

        【为什么需要】mimo 执行核的工具面由 `get_functions()` 一次性装配；元工具
        （web_search / format_result）不在注册表里 ⇒ 只取 get_functions() 会**整体漏掉
        web_search**，而 prompt 却硬性要求实时信息必须调用它（模型撞墙后只能瞎答）。
        装配工具面时必须是 get_functions() + get_meta_functions() 两表并集。
        """
        return dict(self._meta_tools)

    def get_schemas(self) -> List[dict]:
        """planning 用：OpenAI Function Calling schema 列表（带缓存）。"""
        if self._schema_cache is None:
            self._schema_cache = [func_to_openai_schema(f) for f in self._tools.values()]
        return list(self._schema_cache)

    def get_tool_names(self, limit: int = 0) -> List[str]:
        """工具名列表。limit>0 时截断。"""
        names = sorted(self._tools.keys())
        if limit > 0:
            names = names[:limit]
        return names

    def get_tool_descriptions(self, limit: int = 30) -> str:
        """生成工具名+简短描述的文本，用于注入 planning prompt。"""
        lines = []
        for name in sorted(self._tools.keys())[:limit]:
            func = self._tools[name]
            desc = (inspect.getdoc(func) or "").split("\n")[0][:80]
            lines.append(f"- {name}: {desc}")
        remaining = len(self._tools) - limit
        if remaining > 0 and limit > 0:
            lines.append(f"... 及其他 {remaining} 个工具")
        return "\n".join(lines)

    def get_schemas_text(self, limit: int = 0, names_filter: set = None) -> str:
        """生成工具 schema 文本，用于注入 planning YAML 模板的 {{tool_list}}。

        Args:
            limit: 最大工具数（0=不限）。
            names_filter: 仅包含这些工具名（None=全部）。
        """
        lines = []
        schemas = self.get_schemas()
        if names_filter is not None:
            schemas = [s for s in schemas if s.get("function", {}).get("name", "") in names_filter]
        if limit > 0:
            schemas = schemas[:limit]
        for s in schemas:
            fn = s.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")[:80]
            params = fn.get("parameters", {}).get("properties", {})
            param_str = ", ".join(f"{p}: {info.get('type', 'string')}" for p, info in params.items())
            lines.append(f"  {name}({param_str}) — {desc}")
        return "\n".join(lines)

    # ── 查询 ──────────────────────────────────────────────────

    def get(self, name: str) -> Optional[Callable]:
        """按名取工具函数。"""
        return self._tools.get(name)

    def get_domain(self, name: str) -> str:
        """获取工具的 domain。"""
        return self._domains.get(name, "common")

    def search(self, query: str, domain: str = "") -> List[tuple]:
        """按关键词搜索工具。返回 [(name, description)]。支持 domain 过滤。

        2026-10-12 修复（CLI 实测暴露）：原实现是**整串子串匹配**——
        `"run_skill 执行技能流水线" in "run_skill"` 恒 False，
        模型用自然语言短语检索必零命中，于是判定"工具不存在"并转去
        bash 占位探测（run7/本次跑 11~15 步零产出的近因之一）。

        改为两段式，**向后兼容**：
          ① 先按原语义做整串匹配（精确优先，排序在前）；
          ② 仅当①为空时，降级为「空格/中英标点分词后 OR 匹配」兜底。
        既有精确检索行为不变，只是不再把短语查询判成"无此工具"。
        """
        import re as _re

        q = (query or "").strip().lower()
        if not q:
            return []

        # ① 原语义：整串匹配
        results = []
        for name, func in self._tools.items():
            if domain and self._domains.get(name, "common") != domain:
                continue
            desc = (inspect.getdoc(func) or "").split("\n")[0][:120]
            if q in name.lower() or q in desc.lower():
                results.append((name, desc))
        if results:
            return results

        # ② 兜底：分词 OR 匹配（短语 → 词元）
        terms = [t for t in _re.split(r"[\s,，、;；/|\-]+", q) if t]
        if not terms:
            terms = [q]
        # 中文短语没有分隔符时，退化为按整体再试一次（已由①覆盖），
        # 此处额外把纯中文长串按 2~4 字滑窗切词，提升命中率
        if len(terms) == 1 and len(terms[0]) > 4 and _re.search(r"[\u4e00-\u9fff]", terms[0]):
            w = terms[0]
            terms = list({w[i:i + 3] for i in range(len(w) - 2)})
        seen = set()
        for name, func in self._tools.items():
            if domain and self._domains.get(name, "common") != domain:
                continue
            if name in seen:
                continue
            desc = (inspect.getdoc(func) or "").split("\n")[0][:120]
            hay = name.lower() + " " + desc.lower()
            if any(t in hay for t in terms):
                seen.add(name)
                results.append((name, desc))
        return results

    def list_by_domain(self, domain: str = "") -> List[str]:
        """按 domain 列出工具名。"""
        if not domain:
            return sorted(self._tools.keys())
        return sorted(n for n, d in self._domains.items() if d == domain)

    def get_domains(self) -> List[str]:
        """可选工具域（由 tools/<子目录> 推导，不含 common）。

        与 _domains 的区别：_domains 记录每个工具的归属（含手动注册的来源层，
        如 capabilities 的 CAPABILITY_DOMAIN），本方法只返回 planner 能用
        selected_domain 选择的"工具集域"——两者语义不再混淆（2026-09-13）。
        """
        return sorted(d for d in self._selectable_domains if d != "common")

    # ── LLM 面向接口 ────────────────────────────────────────

    def list_tools(self, domain: str = "") -> str:
        """列出可用工具（LLM 调用）。

        Args:
            domain: 领域过滤。空=通用工具，指定领域=领域+通用，"all"=全部。

        Returns:
            格式化的工具列表字符串。
        """
        if domain == "all":
            names = self.get_tool_names()
        elif domain:
            names = self.list_by_domain("common") + self.list_by_domain(domain)
            names = sorted(set(names))
        else:
            names = self.list_by_domain("common")

        if not names:
            return f"无可用工具（domain='{domain}'）"

        lines = [f"可用工具 ({len(names)})："]
        for name in names:
            func = self._tools.get(name)
            if not func:
                continue
            desc = (inspect.getdoc(func) or "").split("\n")[0][:100]
            try:
                sig = inspect.signature(func)
                params = []
                for p in sig.parameters.values():
                    if p.name.startswith("_") or p.name in ("self", "cls"):
                        continue
                    if p.annotation != inspect.Parameter.empty:
                        ann = p.annotation
                        params.append(f"{p.name}: {ann.__name__}" if hasattr(ann, '__name__') else f"{p.name}: {ann}")
                    else:
                        params.append(p.name)
                sig_str = ", ".join(params)
            except Exception:
                sig_str = ""
            if sig_str:
                lines.append(f"  - {name}({sig_str}) — {desc}")
            else:
                lines.append(f"  - {name}() — {desc}")
        return "\n".join(lines)

    def search_tools(self, query: str, domain: str = "") -> str:
        """按关键词搜索工具（LLM 调用）。

        Args:
            query: 搜索关键词。
            domain: 领域过滤。空=通用工具，指定领域=领域+通用，"all"=全部。

        Returns:
            匹配的工具列表字符串。
        """
        if not query:
            return "请提供搜索关键词。"

        effective_domain = "" if domain == "all" else domain
        matched = self.search(query, domain=effective_domain)

        if not matched:
            return f"未找到匹配 '{query}' 的工具。请使用 list_tools() 查看所有可用工具。"

        lines = [f"找到 {len(matched)} 个工具："]
        for name, desc in matched:
            func = self._tools.get(name)
            if not func:
                continue
            try:
                sig = inspect.signature(func)
                params = [p.name for p in sig.parameters.values()
                          if not p.name.startswith("_") and p.name not in ("self", "cls")]
                sig_str = ", ".join(params)
            except Exception:
                sig_str = ""
            if sig_str:
                lines.append(f"  - {name}({sig_str}) — {desc}")
            else:
                lines.append(f"  - {name}() — {desc}")
        return "\n".join(lines)

    def __len__(self):
        return len(self._tools)

    def __contains__(self, name: str):
        return name in self._tools
