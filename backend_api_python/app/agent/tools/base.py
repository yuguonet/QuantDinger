"""
Tool 基类定义

定义工具的统一接口，支持 OpenAI Function Calling 格式。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


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
