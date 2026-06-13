# -*- coding: utf-8 -*-
"""
Nanobot 工具适配器 — 将 QuantDinger 的 @tool 函数批量转换为 Nanobot Tool。

核心逻辑：
  1. 遍历 QuantDinger tool_registry 中所有已注册的 @tool 函数
  2. 逐个包装为 Nanobot Tool 子类（同步 → async via run_in_executor）
  3. 注册到 Nanobot ToolRegistry

零改写：不需要逐个修改 80+ 工具文件。

用法：
  from app.agent.nanobot_tools import register_quantdinger_tools
  register_quantdinger_tools(nanobot_registry, max_result_chars=16000)
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry as NanobotToolRegistry

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 通用适配器：同步 @tool 函数 → Nanobot async Tool
# ═══════════════════════════════════════════════════════════════

class QuantDingerToolAdapter(Tool):
    """将 QuantDinger 的同步 @tool 函数包装为 Nanobot async Tool。

    关键设计：
    - execute() 调用 asyncio.get_event_loop().run_in_executor() 避免阻塞事件循环
    - 参数校验由 Nanobot ToolRegistry.prepare_call() 统一处理
    - 返回值截断由调用方控制
    """

    def __init__(
        self,
        name: str,
        description: str,
        fn: Callable,
        parameters: Dict[str, Any],
        max_result_chars: int = 16000,
    ):
        self._name = name
        self._description = description
        self._fn = fn
        self._parameters = parameters
        self._max_result_chars = max_result_chars

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> Dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        """数据获取类工具标记为只读，可并发执行。"""
        _readonly_tools = {
            "get_realtime_quote", "agent_get_kline", "get_stock_info",
            "get_market_indices", "get_sector_rankings", "get_fund_flow",
            "get_chip_distribution", "get_market_overview", "get_hot_sectors",
            "search_stock_by_name", "analyze_trend", "get_indicator_snapshot",
            "calculate_ma", "get_volume_analysis", "analyze_pattern",
            "generate_kline_chart", "search_stock_news", "search_comprehensive_intel",
            "get_dragon_tiger_stocks", "get_polymarket_analysis",
            "list_project_files", "read_project_file", "grep_project",
        }
        return self._name in _readonly_tools

    async def execute(self, **kwargs: Any) -> Any:
        """执行同步工具函数，通过 run_in_executor 避免阻塞。"""
        import time as _time
        t0 = _time.time()
        loop = asyncio.get_event_loop()
        error = None
        try:
            result = await loop.run_in_executor(
                None, lambda: self._fn(**kwargs)
            )
        except TypeError as te:
            if "unexpected keyword argument" in str(te):
                sig = inspect.signature(self._fn)
                valid_params = {k: v for k, v in kwargs.items() if k in sig.parameters}
                result = await loop.run_in_executor(
                    None, lambda: self._fn(**valid_params)
                )
            else:
                error = str(te)
                raise
        except Exception as e:
            error = str(e)
            return f"Error: {self._name} 执行失败: {e}"
        finally:
            elapsed_ms = (_time.time() - t0) * 1000
            # 通知 TraceCollectorHook（追责体系）
            try:
                from app.agent.nanobot_agent import _current_hook
                hook = _current_hook.get()
                if hook:
                    hook.on_tool_call(
                        tool_name=self._name,
                        arguments=kwargs,
                        result=result if error is None else None,
                        elapsed_ms=elapsed_ms,
                        error=error,
                    )
            except Exception:
                pass  # hook 通知失败不影响工具执行

        # 截断过长结果
        if isinstance(result, str) and len(result) > self._max_result_chars:
            result = result[:self._max_result_chars] + f"\n...(截断，共 {len(result)} 字符)"
        elif isinstance(result, dict):
            import json
            result_str = json.dumps(result, ensure_ascii=False)
            if len(result_str) > self._max_result_chars:
                result = result_str[:self._max_result_chars] + "\n...(截断)"
        elif isinstance(result, list) and len(result) > 50:
            result = result[:50]
            if isinstance(result, list):
                result.append(f"...(截断，仅显示前50条)")

        return result


# ═══════════════════════════════════════════════════════════════
# 批量注册
# ═══════════════════════════════════════════════════════════════

def register_quantdinger_tools(
    nanobot_registry: NanobotToolRegistry,
    max_result_chars: int = 16000,
    domain: str = "",
) -> List[str]:
    """将 QuantDinger 的所有 @tool 注册到 Nanobot ToolRegistry。

    Args:
        nanobot_registry: Nanobot 的 ToolRegistry 实例
        max_result_chars: 工具返回值最大字符数
        domain: 领域过滤（空字符串 = 全部）

    Returns:
        已注册的工具名列表
    """
    from app.agent.tools.registry import registry as qd_registry

    qd_registry.discover()
    registered: List[str] = []

    # 获取所有工具规格
    all_specs = qd_registry._tools  # {name: ToolSpec}

    for name, spec in all_specs.items():
        # 领域过滤
        if domain and spec.meta.get("domain"):
            domains = spec.meta["domain"]
            if isinstance(domains, list) and domain not in domains:
                continue

        # 构建 JSON Schema parameters
        parameters = _build_json_schema(spec)

        # 创建适配器
        adapter = QuantDingerToolAdapter(
            name=name,
            description=spec.description,
            fn=spec.fn,
            parameters=parameters,
            max_result_chars=max_result_chars,
        )

        nanobot_registry.register(adapter)
        registered.append(name)

    logger.info("[NanobotTools] 注册了 %d 个 QuantDinger 工具", len(registered))
    return registered


def _build_json_schema(spec) -> Dict[str, Any]:
    """从 QuantDinger ToolSpec 构建 JSON Schema parameters。

    ToolSpec 没有 parameters 字段，JSON Schema 从函数签名动态推断。
    """
    import inspect
    from typing import get_type_hints, Optional, Union

    sig = inspect.signature(spec.fn)
    try:
        hints = get_type_hints(spec.fn)
    except Exception:
        hints = {}

    properties = {}
    required = []

    _TYPE_MAP = {
        str: "string", int: "integer", float: "number",
        bool: "boolean", dict: "object", list: "array",
    }

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        prop: Dict[str, Any] = {}

        # 类型推断
        tp = hints.get(name, param.annotation)
        if tp != inspect.Parameter.empty:
            # 处理 Optional[X] = Union[X, None]
            origin = getattr(tp, "__origin__", None)
            if origin is Union:
                args = [a for a in tp.__args__ if a is not type(None)]
                if args:
                    tp = args[0]
            prop["type"] = _TYPE_MAP.get(tp, "string")
        else:
            prop["type"] = "string"

        # 从 docstring 提取参数描述
        prop["description"] = _extract_param_desc_from_fn(spec.fn, name)

        # 默认值
        if param.default != inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(name)

        properties[name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _extract_param_desc_from_fn(fn: Callable, param_name: str) -> str:
    """从函数 docstring 提取参数描述。"""
    import inspect
    doc = inspect.getdoc(fn) or ""
    in_args = False
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped.lower().rstrip(":") in ("args", "arguments", "parameters"):
            in_args = True
            continue
        if in_args and stripped and not stripped[0].isspace() and stripped.endswith(":"):
            break
        if in_args and ":" in stripped:
            name_part, _, desc_part = stripped.partition(":")
            if name_part.strip().split()[0] == param_name:
                return desc_part.strip()
    return ""


# ═══════════════════════════════════════════════════════════════
# LLM Provider 工厂（进程内缓存，不依赖 smolagents）
# ═══════════════════════════════════════════════════════════════

def _create_llm_provider():
    """创建 Nanobot LLM provider（从 config.json 或环境变量）。"""
    from pathlib import Path
    try:
        from nanobot.providers.factory import make_provider
        from nanobot.config.loader import load_config

        config_path = Path.home() / ".nanobot" / "config.json"
        config = load_config(config_path) if config_path.exists() else None
        if config:
            provider = make_provider(config)
            model = config.resolve_preset().model
            return provider, model
    except Exception as e:
        logger.warning("[NanobotTools] 从 config 创建 provider 失败: %s", e)

    # fallback: 环境变量直接构建
    import os
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    api_key = (
        os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY", "")
    )
    api_base = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    provider = OpenAICompatProvider(api_key=api_key, api_base=api_base)
    model = os.getenv("AGENT_LLM_MODEL", "deepseek-chat")
    return provider, model


# ═══════════════════════════════════════════════════════════════
# CallSkillTool 适配（保留原 call_skill 能力）
# ═══════════════════════════════════════════════════════════════

class CallSkillToolAdapter(Tool):
    """Nanobot 版 call_skill 工具。

    保留原 QuantDinger 的 call_skill 能力：
    Agent 通过 call_skill(skill_name, stock_code) 调用 BaseSkill。
    """

    @property
    def name(self) -> str:
        return "call_skill"

    @property
    def description(self) -> str:
        return (
            "调用分析技能对股票进行专业分析。传入技能名和股票代码，"
            "返回结构化分析报告（评分/方向/信号/因子明细）。"
            "可选技能: technical_agent(技术面), indicator_agent(指标), "
            "intelligence_agent(情报), hot_money_tracker(游资), "
            "lockup_watcher(解禁), market_data_agent(行情), "
            "screening_agent(选股), backtest_agent(回测), "
            "bull_researcher(多头), bear_researcher(空头), "
            "data_agent(数据), trading_agent(交易)"
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "技能名称",
                },
                "stock_code": {
                    "type": "string",
                    "description": "股票代码，如 600519",
                },
                "stock_name": {
                    "type": "string",
                    "description": "股票名称（可选）",
                    "nullable": True,
                },
            },
            "required": ["skill_name", "stock_code"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        """调用 BaseSkill，返回结构化报告。"""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._call_skill_sync(**kwargs)
        )

    # 工具函数缓存（进程内唯一）
    _tool_fn_cache: Dict[str, Callable] = {}
    _tool_fn_ready: bool = False
    # LLM provider 缓存（进程内唯一）
    _cached_provider = None
    _cached_model: str = ""
    # 主事件循环引用（用于 run_coroutine_threadsafe）
    _main_loop = None

    def _call_skill_sync(self, skill_name: str, stock_code: str, stock_name: str = None) -> str:
        """同步调用 BaseSkill（在 executor 线程中运行）。

        不依赖 smolagents：直接从 QuantDinger tool_registry 获取原始函数。
        LLM 调用复用 Nanobot 的 provider（缓存，不每次创建）。
        """
        import time as _time
        from app.agent.skills.registry import skill_registry
        from app.agent.tools.registry import registry as qd_registry

        skill_registry.discover()
        qd_registry.discover()

        sk = skill_registry.get(skill_name)
        if not sk:
            available = ", ".join(skill_registry.all_names)
            logger.error("[CallSkill] 未知技能: %s。可用: %s", skill_name, available)
            return f"未知技能: {skill_name}。可用技能: {available}"

        logger.info("[CallSkill] ═══ 开始 %s(stock=%s %s) ═══",
                    skill_name, stock_code, stock_name or "")

        # 缓存工具函数映射
        if not CallSkillToolAdapter._tool_fn_ready:
            CallSkillToolAdapter._tool_fn_cache = {
                name: spec.fn for name, spec in qd_registry._tools.items()
            }
            CallSkillToolAdapter._tool_fn_ready = True
        tool_fn_map = CallSkillToolAdapter._tool_fn_cache

        # 缓存 LLM provider（进程内唯一）
        if CallSkillToolAdapter._cached_provider is None:
            CallSkillToolAdapter._cached_provider, CallSkillToolAdapter._cached_model = (
                _create_llm_provider()
            )

        def call_llm(prompt: str) -> str:
            """LLM 调用 — 复用主事件循环，不创建新 loop。"""
            import asyncio
            main_loop = CallSkillToolAdapter._main_loop
            if main_loop and main_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    CallSkillToolAdapter._cached_provider.chat_with_retry(
                        messages=[{"role": "user", "content": prompt}],
                        model=CallSkillToolAdapter._cached_model,
                    ),
                    main_loop,
                )
                try:
                    response = future.result(timeout=120)
                    return response.content or ""
                except Exception as e:
                    return f"LLM 调用失败: {e}"
            else:
                # fallback: 主循环不可用时创建临时 loop
                loop = asyncio.new_event_loop()
                try:
                    response = loop.run_until_complete(
                        CallSkillToolAdapter._cached_provider.chat_with_retry(
                            messages=[{"role": "user", "content": prompt}],
                            model=CallSkillToolAdapter._cached_model,
                        )
                    )
                    return response.content or ""
                except Exception as e:
                    return f"LLM 调用失败: {e}"
                finally:
                    loop.close()

        def call_tool_fn(tool_name: str, **kw) -> Any:
            """工具调用 — 直接调用原始函数，不经过 smolagents。"""
            fn = tool_fn_map.get(tool_name)
            if fn is None:
                raise ValueError(f"Unknown tool: {tool_name}")
            return fn(**kw)

        try:
            t0 = _time.time()
            report, eval_node = sk.run(
                stock_code=stock_code,
                stock_name=stock_name or "",
                call_llm=call_llm,
                call_tool_fn=call_tool_fn,
            )
            elapsed = (_time.time() - t0) * 1000
            logger.info("[CallSkill] ─── %s 完成 ─── score=%.1f direction=%s signal=%s 耗时=%.0fms 工具=%s",
                        skill_name, report.score or 0, report.direction, report.signal,
                        elapsed, report.tools_called or [])
        except Exception as e:
            logger.error("[CallSkill] ─── %s 失败 ─── %s", skill_name, e, exc_info=True)
            return f"技能 {skill_name} 执行失败: {e}"

        # 持久化 EvalNode（保留追责能力）
        try:
            from app.agent.chain import store as chain_store
            from datetime import date
            from app.agent.chain.schema import EvalNode, Layer

            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=f"call_skill+{skill_name}",
                exec_date=date.today(),
                stock_code=stock_code,
                stock_name=stock_name or "",
                score=report.score,
                direction=report.direction,
                action="buy" if report.score and report.score >= 60 else
                       "sell" if report.score and report.score <= 40 else "hold",
                signal=report.signal,
                confidence=report.confidence,
            )
            root.add_child(eval_node)
            chain_store.save_tree(root)
        except Exception as e:
            logger.warning("[CallSkill] EvalNode 持久化失败: %s", e)

        # 格式化返回
        return self._format_report(report)

    @staticmethod
    def _format_report(report) -> str:
        lines = [
            f"## {report.skill_name}",
            f"评分: {report.score:.0f} | 方向: {report.direction} | 信号: {report.signal}",
            f"置信: {report.confidence:.2f} | 状态: {report.status}",
        ]
        if report.factors:
            lines.append("### 因子明细")
            for f in report.factors:
                s = f"{f.score:.0f}" if f.score is not None else "—"
                lines.append(f"- {f.name}: {f.value} ({s}分)")
        if report.analysis:
            lines.append(f"\n{report.analysis[:800]}")
        if report.error:
            lines.append(f"\n⚠️ 错误: {report.error}")
        return "\n".join(lines)


def register_call_skill_tool(nanobot_registry: NanobotToolRegistry) -> None:
    """注册 call_skill 工具到 Nanobot。"""
    nanobot_registry.register(CallSkillToolAdapter())
    logger.info("[NanobotTools] 注册 call_skill 工具")
