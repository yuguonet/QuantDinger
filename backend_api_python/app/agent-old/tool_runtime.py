# -*- coding: utf-8 -*-
"""
Tool Runtime — 工具执行中间层（Agent-Template 模式）。

职责：
  - Schema Validation：校验工具参数是否符合 JSON Schema
  - Permission Check：检查工具是否允许调用
  - Result Compression：压缩过长的工具结果
  - Error Classification：按错误类型分级处理
  - Context Write-back：将结果写回上下文

Agent-Template 的 Tool Calling 状态机：
  Tool Discovery → Tool Selection → Argument Generation →
  Schema Validation → Permission Check → Tool Execution →
  Result Validation → Result Compression → Context Write-back

我们只做中间层（Validation → Permission → Compression），
工具发现和选择由 Planner/Agent 完成。

用法：
  from app.agent.tool_runtime import ToolRuntime
  runtime = ToolRuntime()
  result = runtime.execute("get_kline", {"code": "600519"}, tool_fn)
"""
from __future__ import annotations

import json
import time
from app.agent.log import logger
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


# ── 配置 ──────────────────────────────────────────────────────
# 工具结果最大字符数（超过则压缩）
MAX_RESULT_CHARS = int(__import__("os").getenv("TOOL_RESULT_MAX_CHARS", "5000"))
# 压缩后目标字符数
COMPRESS_TARGET_CHARS = int(__import__("os").getenv("TOOL_COMPRESS_TARGET", "1500"))
# 危险工具列表（需要额外确认）
DANGEROUS_TOOLS: Set[str] = set()
# 禁止的工具列表
BLOCKED_TOOLS: Set[str] = set()


@dataclass
class ToolResult:
    """工具执行结果。"""
    success: bool = False
    tool: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    result_str: str = ""
    compressed: bool = False
    error: str = ""
    error_type: str = ""  # schema_error / permission_denied / execution_error / timeout
    retryable: bool = False
    duration_ms: float = 0.0


# ── 错误分类 ──────────────────────────────────────────────────

_ERROR_CLASSIFICATIONS = {
    "timeout": {"retryable": True, "backoff": "exponential"},
    "rate_limit": {"retryable": True, "backoff": "wait"},
    "permission_denied": {"retryable": False, "action": "stop"},
    "schema_error": {"retryable": False, "action": "regenerate"},
    "empty_result": {"retryable": False, "action": "report"},
    "internal_error": {"retryable": True, "backoff": "exponential"},
    "connection_error": {"retryable": True, "backoff": "exponential"},
}


def classify_error(error: Exception) -> str:
    """将异常分类为错误类型。"""
    err_str = str(error).lower()
    err_type = type(error).__name__.lower()

    if "timeout" in err_str or "timeout" in err_type:
        return "timeout"
    if "rate" in err_str and "limit" in err_str:
        return "rate_limit"
    if "permission" in err_str or "denied" in err_str or "403" in err_str:
        return "permission_denied"
    if "schema" in err_str or "validation" in err_str or "422" in err_str:
        return "schema_error"
    if "connection" in err_str or "connect" in err_str:
        return "connection_error"
    if "500" in err_str or "502" in err_str or "503" in err_str:
        return "internal_error"

    return "execution_error"


def is_retryable(error_type: str) -> bool:
    """判断错误是否可重试。"""
    info = _ERROR_CLASSIFICATIONS.get(error_type, {})
    return info.get("retryable", False)


# ── Schema Validation ─────────────────────────────────────────

def _validate_schema(arguments: Dict[str, Any], schema: Dict[str, Any]) -> Optional[str]:
    """校验参数是否符合 JSON Schema。返回 None 表示通过，否则返回错误描述。"""
    if not schema:
        return None

    required = schema.get("required", [])
    properties = schema.get("properties", {})

    # 检查必填字段
    for field_name in required:
        if field_name not in arguments or arguments[field_name] is None:
            return f"缺少必填参数: {field_name}"

    # 检查类型
    for field_name, value in arguments.items():
        if field_name not in properties:
            continue
        expected_type = properties[field_name].get("type", "")
        if expected_type == "string" and value is not None and not isinstance(value, str):
            return f"参数 {field_name} 应为 string，实际为 {type(value).__name__}"
        if expected_type == "integer" and value is not None and not isinstance(value, int):
            return f"参数 {field_name} 应为 integer，实际为 {type(value).__name__}"
        if expected_type == "number" and value is not None and not isinstance(value, (int, float)):
            return f"参数 {field_name} 应为 number，实际为 {type(value).__name__}"
        if expected_type == "boolean" and value is not None and not isinstance(value, bool):
            return f"参数 {field_name} 应为 boolean，实际为 {type(value).__name__}"

    return None


# ── Permission Check ──────────────────────────────────────────

def _check_permission(tool_name: str) -> Optional[str]:
    """检查工具是否允许调用。返回 None 表示通过，否则返回拒绝原因。"""
    if tool_name in BLOCKED_TOOLS:
        return f"工具 {tool_name} 已被禁止"
    return None


# ── Result Compression ────────────────────────────────────────

def _compress_result(result_str: str, tool_name: str) -> tuple:
    """压缩过长的工具结果。

    Returns:
        (compressed_str, was_compressed)
    """
    if len(result_str) <= MAX_RESULT_CHARS:
        return result_str, False

    # 策略：保留头尾，中间用摘要替代
    head = result_str[:COMPRESS_TARGET_CHARS // 2]
    tail = result_str[-(COMPRESS_TARGET_CHARS // 4):]
    omitted = len(result_str) - len(head) - len(tail)

    compressed = f"{head}\n\n... [省略 {omitted} 字符] ...\n\n{tail}"
    logger.info(
        "[ToolRuntime] 结果压缩: %s (%d → %d 字符)",
        tool_name, len(result_str), len(compressed),
    )
    return compressed, True


# ── ToolRuntime ───────────────────────────────────────────────

class ToolRuntime:
    """工具执行中间层。

    用法：
        runtime = ToolRuntime()
        result = runtime.execute("get_kline", {"code": "600519"}, kline_fn)
        if result.success:
            print(result.result_str)
    """

    def __init__(
        self,
        max_result_chars: int = MAX_RESULT_CHARS,
        compress_target: int = COMPRESS_TARGET_CHARS,
        blocked_tools: Optional[Set[str]] = None,
    ):
        self.max_result_chars = max_result_chars
        self.compress_target = compress_target
        self._blocked = blocked_tools or BLOCKED_TOOLS

    def validate(self, tool_name: str, arguments: Dict[str, Any],
                 schema: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Schema Validation。返回 None 表示通过。"""
        return _validate_schema(arguments, schema or {})

    def check_permission(self, tool_name: str) -> Optional[str]:
        """Permission Check。返回 None 表示通过。"""
        if tool_name in self._blocked:
            return f"工具 {tool_name} 已被禁止"
        return None

    def compress(self, result_str: str, tool_name: str) -> tuple:
        """Result Compression。返回 (compressed_str, was_compressed)。"""
        if len(result_str) <= self.max_result_chars:
            return result_str, False

        head = result_str[:self.compress_target // 2]
        tail = result_str[-(self.compress_target // 4):]
        omitted = len(result_str) - len(head) - len(tail)
        compressed = f"{head}\n\n... [省略 {omitted} 字符] ...\n\n{tail}"
        logger.info(
            "[ToolRuntime] 结果压缩: %s (%d → %d 字符)",
            tool_name, len(result_str), len(compressed),
        )
        return compressed, True

    def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_fn: Callable,
        schema: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        """完整执行流程：Validation → Permission → Execution → Compression。

        Args:
            tool_name: 工具名
            arguments: 工具参数
            tool_fn: 工具函数
            schema: 可选的 JSON Schema（用于参数校验）

        Returns:
            ToolResult
        """
        result = ToolResult(tool=tool_name, arguments=arguments)
        t0 = time.time()

        # 1. Schema Validation
        if schema:
            schema_err = self.validate(tool_name, arguments, schema)
            if schema_err:
                result.error = schema_err
                result.error_type = "schema_error"
                result.retryable = False
                result.duration_ms = (time.time() - t0) * 1000
                logger.warning("[ToolRuntime] Schema 校验失败: %s — %s", tool_name, schema_err)
                return result

        # 2. Permission Check
        perm_err = self.check_permission(tool_name)
        if perm_err:
            result.error = perm_err
            result.error_type = "permission_denied"
            result.retryable = False
            result.duration_ms = (time.time() - t0) * 1000
            logger.warning("[ToolRuntime] 权限拒绝: %s — %s", tool_name, perm_err)
            return result

        # 3. Execution
        try:
            raw_result = tool_fn(**arguments)

            # 4. Result Validation
            if raw_result is None:
                result.success = True
                result.result = None
                result.result_str = ""
            else:
                result.success = True
                result.result = raw_result
                result.result_str = (
                    json.dumps(raw_result, ensure_ascii=False)
                    if isinstance(raw_result, (dict, list))
                    else str(raw_result)
                )

            # 5. Result Compression
            result.result_str, result.compressed = self.compress(result.result_str, tool_name)

        except Exception as e:
            result.error = str(e)
            result.error_type = classify_error(e)
            result.retryable = is_retryable(result.error_type)
            logger.warning("[ToolRuntime] 执行失败: %s — [%s] %s",
                           tool_name, result.error_type, str(e)[:200])

        result.duration_ms = (time.time() - t0) * 1000
        return result
