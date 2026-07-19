"""
Tool 基类 + ToolProvider 统一工具注册表

设计原则：
  - 一个注册表，一次扫描，两种输出（函数 + schema）
  - 工具直接注册，无需中间层
  - 符合 OpenAI Function Calling 标准
"""

import importlib
import inspect
import json
import logging
import typing
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, get_type_hints

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """工具执行结果"""

    success: bool = True
    output: Any = None
    error: Optional[str] = None

    def to_str(self) -> str:
        """转为字符串（用于传回 LLM），按数据形态选择最省 token 的格式"""
        if not self.success:
            return f"[工具执行失败] {self.error}"

        data = self.output

        # 标量直接转
        if not isinstance(data, (dict, list)):
            return str(data)

        # 扁平 dict → "key: value" 每行一条（无引号无括号）
        if isinstance(data, dict) and not _is_nested(data):
            return "\n".join(f"- {k}: {v}" for k, v in data.items())

        # list[dict]（表形数据）→ TSV（最省 token 的表格表示）
        if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
            return _to_tsv(data)

        # 嵌套结构 → 带缩进的 JSON（LLM 更容易阅读，避免 compact JSON 解析门槛）
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _is_nested(d: dict) -> bool:
    """值中是否包含嵌套的 dict 或 list"""
    return any(isinstance(v, (dict, list)) for v in d.values())


def _to_tsv(items: list) -> str:
    """list[dict] → TSV（无冗余字符，最省 token）"""
    headers = list(dict.fromkeys(k for d in items for k in d))
    lines = ["\t".join(headers)]
    for item in items:
        lines.append("\t".join(str(item.get(h, "")) for h in headers))
    return "\n".join(lines)


class Tool(ABC):
    """
    工具基类
    所有自定义工具必须继承此类并实现 execute() 方法。
    使用示例：
        class WeatherTool(Tool):
            name = "get_weather"
            description = "查询城市天气"
            parameters = {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名"},
                },
                "required": ["city"],
            }

            async def execute(self, city: str) -> ToolResult:
                # 调用天气 API
                return ToolResult(success=True, output={"city": city, "temp": "25C"})
    """

    # 子类必须定义这三个属性
    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict) if False else {}

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """
        执行工具

        :param kwargs: 工具参数（由 LLM Function Calling 传入）
        :return: ToolResult
        """
        ...

    def get_function_schema(self) -> dict:
        """
        生成 OpenAI Function Calling 格式的 Schema
        :return: Function Schema 字典
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }

    async def safe_execute(self, **kwargs) -> ToolResult:
        """
        安全执行（自动捕获异常）
        :param kwargs: 工具参数
        :return: ToolResult
        """
        try:
            return await self.execute(**kwargs)
        except Exception as e:
            return ToolResult(success=False, error=f"{self.name} 执行失败: {str(e)}")


# ═══════════════════════════════════════════════════════════════
#  OpenAI Function Calling Schema 生成
# ═══════════════════════════════════════════════════════════════

_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json(tp) -> Dict[str, Any]:
    """Python 类型 → JSON Schema type。"""
    origin = getattr(tp, "__origin__", None)
    if origin is type(None):
        return {"type": "string"}
    if origin is dict:
        return {"type": "object"}
    if origin is list:
        args = getattr(tp, "__args__", None)
        if args:
            return {"type": "array", "items": _python_type_to_json(args[0])}
        return {"type": "array"}
    if origin is typing.Union:
        args = getattr(tp, "__args__", ())
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _python_type_to_json(non_none[0])
        return {"type": "string"}
    return {"type": _TYPE_MAP.get(tp, "string")}


def _parse_docstring_params(doc: str) -> Dict[str, str]:
    """解析 docstring 中的参数描述。"""
    if not doc:
        return {}
    param_descs: Dict[str, str] = {}
    in_args = False
    for line in doc.strip().split("\n"):
        stripped = line.strip()
        if stripped.lower().rstrip(":") in ("args", "parameters", "参数"):
            in_args = True
            continue
        if in_args:
            if stripped and not line[0].isspace() and ":" not in stripped:
                in_args = False
                continue
            if stripped and ":" in stripped:
                parts = stripped.split(":", 1)
                pname = parts[0].strip().split("(")[0].strip()
                pdesc = parts[1].strip()
                if pname:
                    param_descs[pname] = pdesc
                continue
    return param_descs


def func_to_openai_schema(func: Callable) -> Dict[str, Any]:
    """函数 → OpenAI Function Calling JSON Schema。"""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""

    # 解析描述（排除 Args 段落）
    desc_lines = []
    in_args = False
    for line in doc.strip().split("\n") if doc else []:
        stripped = line.strip()
        if stripped.lower().rstrip(":") in ("args", "parameters", "参数"):
            in_args = True
            continue
        if in_args:
            if stripped and not line[0].isspace() and ":" not in stripped:
                in_args = False
                desc_lines.append(line)
                continue
            if stripped and ":" in stripped:
                continue
        if not in_args:
            desc_lines.append(line)
    description = "\n".join(desc_lines).strip()
    if not description:
        description = doc.split("\n")[0][:500] if doc else func.__name__

    param_descs = _parse_docstring_params(doc)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    properties = {}
    required = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if pname.startswith("_"):
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue

        ptype = hints.get(pname, str)
        prop = _python_type_to_json(ptype)
        if pname in param_descs:
            prop["description"] = param_descs[pname]
        # 枚举值检测（Literal 注解）
        origin = getattr(ptype, "__origin__", None)
        if origin is typing.Literal:
            prop["enum"] = list(getattr(ptype, "__args__", ()))
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    parameters = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description[:1024],
            "parameters": parameters,
        },
    }


# ═══════════════════════════════════════════════════════════════
#  ToolProvider — 统一工具注册表
# ═══════════════════════════════════════════════════════════════

# 跳过的文件（框架文件，非工具）
_SKIP_FILES = {
    "__init__", "base", "em_utils", "pagination",
}

# 必选工具（通过 smolagents tools=[] 注入，provider 不扫描）
_MUST_HAVE = {"format_utils", "web_search_tools"}


class ToolProvider:
    """统一工具注册表。

    注册一次，两种输出：
      - get_functions() → executor 用（name → callable）
      - get_schemas()   → planning 用（OpenAI Function Calling schema）
    """

    # 模块级单例，由 init_tools() 设置
    _default: Optional["ToolProvider"] = None

    @classmethod
    def set_default(cls, provider: "ToolProvider"):
        """设置全局默认 provider。"""
        cls._default = provider

    @classmethod
    def get_default(cls) -> Optional["ToolProvider"]:
        """获取全局默认 provider。"""
        return cls._default

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._domains: Dict[str, str] = {}       # name → domain
        self._schema_cache: Optional[List[dict]] = None

    # ── 扫描注册 ──────────────────────────────────────────────

    def scan_directory(self, tools_dir: Path, domain: str = "common",
                       package_prefix: str = "tools"):
        """扫描目录下所有 .py，自动注册公开函数。

        Args:
            tools_dir: 目录路径
            domain: 领域名（默认 common）
            package_prefix: 导入包前缀
        """
        for py_file in sorted(tools_dir.glob("*.py")):
            module_name = py_file.stem
            if module_name.startswith("_") or module_name in _SKIP_FILES or module_name in _MUST_HAVE:
                continue
            try:
                mod = importlib.import_module(f"{package_prefix}.{module_name}")
            except Exception:
                logger.debug("[ToolProvider] 跳过模块 %s", module_name, exc_info=True)
                continue
            self._register_module_functions(mod, domain)

    def scan_subdirectories(self, tools_dir: Path, package_prefix: str = "tools"):
        """扫描子目录，子目录名即 domain。"""
        for sub in sorted(tools_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("_") or sub.name == "__pycache__":
                continue
            self.scan_directory(sub, domain=sub.name,
                                package_prefix=f"{package_prefix}.{sub.name}")

    def register(self, name: str, func: Callable, domain: str = "common"):
        """手动注册单个函数。"""
        self._tools[name] = func
        self._domains[name] = domain
        self._schema_cache = None

    def register_module(self, module, domain: str = "common"):
        """注册模块中所有公开函数。"""
        self._register_module_functions(module, domain)

    def _register_module_functions(self, module, domain: str):
        """扫描模块公开函数并注册。"""
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if not callable(obj) or not inspect.isfunction(obj):
                continue
            if getattr(obj, "__module__", "") != module.__name__:
                continue
            if not inspect.getdoc(obj):
                continue
            self._tools[attr_name] = obj
            self._domains[attr_name] = domain
        self._schema_cache = None

    # ── 输出 ──────────────────────────────────────────────────

    def get_functions(self) -> Dict[str, Callable]:
        """executor 用：name → callable。"""
        return dict(self._tools)

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
        """按关键词搜索工具。返回 [(name, description)]。支持 domain 过滤。"""
        q = query.lower()
        results = []
        for name, func in self._tools.items():
            if domain and self._domains.get(name, "common") != domain:
                continue
            desc = (inspect.getdoc(func) or "").split("\n")[0][:120]
            if q in name.lower() or q in desc.lower():
                results.append((name, desc))
        return results

    def list_by_domain(self, domain: str = "") -> List[str]:
        """按 domain 列出工具名。"""
        if not domain:
            return sorted(self._tools.keys())
        return sorted(n for n, d in self._domains.items() if d == domain)

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
