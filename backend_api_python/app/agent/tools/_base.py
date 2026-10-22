# -*- coding: utf-8 -*-
"""Tool 基类 + 结果封装 —— 以及 `tools/_base.py` 历史导出面的**兼容入口**。

★ P4 拆分（2026-10-13）：本文件从 1025 行拆成 4 个模块，这里只留「工具抽象 +
  结果封装」，其余按职责各归各位：

  | 模块                  | 职责                                        |
  |-----------------------|---------------------------------------------|
  | `tools/_serialize.py`  | 工具结果序列化（TSV / 紧凑 JSON / 空值剥离） |
  | `tools/_schema_gen.py` | OpenAI schema 生成 + 「什么是工具函数」判据 |
  | `tools/_provider.py`   | ToolProvider 注册表（扫描/注册/查询/元数据）|
  | `tools/_base.py`（本） | Tool ABC + ToolResult + 统一信封 + re-export|

  依赖方向（单向，无环）：
      base → provider → schema_gen →（无内部依赖）
      base → serialize

★ 为什么保留 re-export 而不是改 20+ 个消费方
  `tools/__init__.py`、`capabilities/loader.py`、`resolvers/*`、`tools/finance/*`、
  `qd_tool_face.py`、`chain/evaluator.py` 等都以 `from tools.base import X` 取用。
  一次性改全部调用点会让本次改动不可审；先收敛实现、保住导入面，等 legacy 退役时
  再统一换成直连新模块。

★⚠ 最重要的 re-export 约束：可变共享状态必须是**同一个对象**
  `_TOOL_MODULE_IMPORT_FAILURES`（list）一律用 `from ._provider import X` 原样引用，
  **绝不重建**。否则登记与读取落到两个对象 ⇒ 导入失败登记表永远为空 ⇒ 工具面
  悄悄缺一大块而无人知（历史事故：工具面从 101 掉到 91，能力层补位掩盖缺口）。
  `tests/test_v6_tools_base_split.py` 用 `is` 断言守着这一点。
  （`_SUPERSEDED_BY_MERGE` 名单已于 2026-10-22 AP.8 整体删除，不再 re-export。）
  （`TOOL_WEIGHT_MIGRATION` 映射表已于 2026-10-22 用户裁定整体删除：数据库实测
    `qd_agent_weights(layer='tool')` 旧工具名行 = 0，映射表空转从未命中；evaluator
    对旧名行直接 DELETE。不再 re-export。）

设计原则：
  - 一个注册表，一次扫描，两种输出（函数 + schema）
  - 工具直接注册，无需中间层
  - 符合 OpenAI Function Calling 标准
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ── 序列化（拆分后搬到 tools/_serialize.py，此处原样引用保持导出面）─────────
from ._serialize import (  # noqa: F401
    _MAX_OBSERVATION_ROWS,
    _flatten_mixed,
    _has_record,
    _is_direct_record,
    _is_leaf_dict,
    _json,
    _scalar,
    _scalar_list,
    _serialize,
    _serialize_dict,
    _serialize_list,
    _strip_nulls,
    _to_tsv,
)

# ── schema 生成与工具判据（tools/_schema_gen.py）──────────────────────────
from ._schema_gen import (  # noqa: F401
    _is_tool_function,
    _parse_docstring_params,
    _python_type_to_json,
    _TYPE_MAP,
    func_to_openai_schema,
)

# ── 注册表（tools/_provider.py）──────────────────────────────────────────
# ⚠ 下面这批**必须原样引用**：`_TOOL_MODULE_IMPORT_FAILURES` 是可变 list，
#   重建即静默失效（详见文件头）。
#   （`_SUPERSEDED_BY_MERGE` 名单已于 2026-10-22 AP.8 整体删除，不再 re-export。）
from ._provider import (  # noqa: F401
    _MUST_HAVE,
    _TOOL_MODULE_IMPORT_FAILURES,
    ToolProvider,
)

logger = logging.getLogger(__name__)


def as_code_envelope(code: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    """统一单码工具返回信封（幻象结构治理）。

    背景：`calculate_ma` 单码返回扁平 dict，多码返回 `{count, data:{code}}`；
    模型按 `get_realtime_quote` 习惯写 `ma['data'][code]` → KeyError。

    契约：**始终**带 `count`/`data{code}`；单码时顶层再镜像一份扁平键（双通道可读）。
    error 形态（`{"error":...}`）保持原样，不包信封。
    """
    if not isinstance(obj, dict):
        obj = {"value": obj}
    if "error" in obj and not any(k not in ("error", "retriable") for k in obj):
        return obj
    out = {"count": 1, "data": {code: obj}}
    for k, v in obj.items():
        out.setdefault(k, v)
    return out


@dataclass
class ToolResult:
    """工具执行结果"""

    success: bool = True
    output: Any = None
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, result: Any, metadata: Optional[dict] = None):
        return cls(output=result, metadata=dict(metadata or {}))

    def to_str(self) -> str:
        """转为字符串（用于传回 LLM），按数据形态选择最省 token 的格式。

        统一口径：含「表」（list[dict] / dict-of-records）的部分压成 TSV，纯键值
        嵌套保留紧凑 JSON（实测 TSV 在那里反而更膨胀），标量直接转。
        LLM 对格式无感，只认「键名 → 值」；命中率由「所有非空叶子值都可通过键路径
        找回、绝不丢有效信息」保证（仅 null/空容器这种无信息量内容被剥离）。
        """
        if not self.success:
            return f"[工具执行失败] {self.error}"

        data = self.output
        if not isinstance(data, (dict, list)):
            return str(data)

        # 先剥 null/空（无信息量）并截断超长列表，再选最省表达。
        cleaned = _strip_nulls(data)
        return _serialize(cleaned)


class Tool(ABC):
    """
    工具基类
    所有自定义工具必须继承此类并实现 execute() 方法。
    使用示例（示例名仅作模板，不要在对话里调用它）：
        class MyExampleTool(Tool):
            name = "my_example_tool"
            description = "（文档示例）按参数查询一条记录"
            parameters = {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "记录标识"},
                },
                "required": ["code"],
            }

            async def execute(self, code: str) -> ToolResult:
                # 调用真实数据源
                return ToolResult(success=True, output={"code": code, "value": "..."})
    """

    # 子类必须定义这三个属性
    name: str = ""
    description: str = ""
    # ★ 必须用 field(default_factory=dict)，不能写 {}——类属性 {} 是可变对象，
    #   所有未覆盖 parameters 的子类会共享同一个 dict 实例（一个子类修改会污染其他）。
    #   旧写法 `field(default_factory=dict) if False else {}` 是错的：if False 分支
    #   永远不执行，实际值就是 {}，等于没修。
    parameters: dict = field(default_factory=dict)

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


__all__ = [
    # 本模块自有
    "Tool", "ToolResult", "as_code_envelope",
    # re-export（保持历史导入面）
    "ToolProvider", "func_to_openai_schema",
    "_MUST_HAVE",
    "_TOOL_MODULE_IMPORT_FAILURES", "_is_tool_function",
    "_serialize", "_to_tsv", "_strip_nulls",
]
