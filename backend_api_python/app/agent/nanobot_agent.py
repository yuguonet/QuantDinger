# -*- coding: utf-8 -*-
"""
Nanobot Agent 桥接层 — 方案 B：持久事件循环 + 线程。

核心职责：
  1. 管理 Nanobot AgentLoop 的生命周期（单例，进程内唯一）
  2. 提供同步 API 给 Flask 路由调用（chat / chat_stream）
  3. 注入 QuantDinger 工具 + Skill + 追责体系
  4. 从 .env 自动生成 Nanobot 配置

架构：
  Flask Route (sync) → nanobot_agent.chat() → asyncio.run_coroutine_threadsafe() → Nanobot AgentLoop (async)
                                                                                       ↑ 持久事件循环 + 守护线程

零进程开销：同一进程内函数调用，无 fork 延迟
会话可复用：跨请求保持上下文，多用户会话隔离
资源共享：与 Flask 共用连接池

用法：
  from app.agent.nanobot_agent import get_nanobot_agent
  agent = get_nanobot_agent()
  result = agent.chat("分析贵州茅台", session_id="user_123")
  print(result.content)
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# ── 当前请求的 TraceCollectorHook（通过 contextvars 传递给工具层）──
_current_hook: ContextVar["TraceCollectorHook | None"] = ContextVar(
    "qd_trace_hook", default=None
)


# ═══════════════════════════════════════════════════════════════
# 结果容器（与原 AgentResult 兼容）
# ═══════════════════════════════════════════════════════════════

@dataclass
class NanobotResult:
    """与原 smolagents AgentResult 兼容的结果容器。"""
    success: bool = False
    content: str = ""
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)
    total_steps: int = 0
    total_tokens: int = 0
    model: str = ""
    error: Optional[str] = None
    charts: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# AgentHook — 追责体系集成
# ═══════════════════════════════════════════════════════════════

class TraceCollectorHook:
    """将 Nanobot 的 AgentHook 事件桥接到 QuantDinger 的 TraceCollector。

    监听：
    - before_run → 创建 TraceCollector
    - after_iteration → 记录工具调用
    - after_run → 构建 EvalNode 树 + 存库
    """

    def __init__(self, session_id: str, user_query: str):
        self._session_id = session_id
        self._user_query = user_query
        self._collector = None
        self._tools_used: List[str] = []
        self._start_time = time.time()

    def setup_collector(self, domain: str = "finance",
                        stock_code: str = "", stock_name: str = ""):
        """创建 TraceCollector（仅金融领域）。"""
        if domain != "finance":
            return
        from app.agent.trace_collector import TraceCollector
        self._collector = TraceCollector(
            session_id=self._session_id,
            user_query=self._user_query,
        )
        self._collector.domain = domain
        self._collector.stock_code = stock_code
        self._collector.stock_name = stock_name

    @property
    def collector(self):
        return self._collector

    def on_tool_call(self, tool_name: str, arguments: dict, result: Any,
                     elapsed_ms: float, error: str = None):
        """记录工具调用。"""
        self._tools_used.append(tool_name)
        if self._collector:
            self._collector.on_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                elapsed_ms=elapsed_ms,
                error=error,
            )

    def on_agent_finish(self, final_content: str, total_steps: int,
                        total_tokens: int, model: str):
        """Agent 完成，构建 EvalNode 树并存库。"""
        if not self._collector:
            return None
        return self._collector.on_agent_finish(
            final_answer=final_content,
            total_steps=total_steps,
            total_tokens=total_tokens,
            model=model,
        )


# ═══════════════════════════════════════════════════════════════
# 核心桥接层
# ═══════════════════════════════════════════════════════════════

class NanobotAgent:
    """Nanobot Agent 桥接层（单例模式）。

    管理持久事件循环 + 守护线程，提供同步 API。
    """

    _instance: Optional["NanobotAgent"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_path: Optional[str] = None, force_config: bool = False):
        if self._initialized:
            return
        self._initialized = True

        # ── 1. 生成 Nanobot 配置 ─────────────────────────────
        from app.agent.nanobot_config_gen import ensure_nanobot_config
        self._config_path = ensure_nanobot_config(force=force_config)

        # ── 2. 创建持久事件循环 + 守护线程 ───────────────────
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="nanobot-event-loop",
        )
        self._thread.start()
        logger.info("[NanobotAgent] 事件循环已启动 (线程: %s)", self._thread.name)

        # ── 3. 在事件循环中初始化 Nanobot ────────────────────
        self._nanobot = self._run_async(self._init_nanobot())
        self._agent_loop = self._nanobot._loop

        # ── 4. 注入 QuantDinger 工具 ─────────────────────────
        self._run_async(self._inject_quantdinger_tools())

        # ── 5. 生成 Nanobot Skill 目录 + 人格文件 ───────────
        from app.agent.nanobot_skills import ensure_nanobot_skills
        workspace = Path(self._agent_loop.workspace)
        ensure_nanobot_skills(workspace)
        self._inject_preamble(workspace)

        logger.info("[NanobotAgent] 初始化完成")

    def _inject_preamble(self, workspace: Path):
        """将 agent_preamble.md 注入 Nanobot workspace 的 AGENTS.md。"""
        preamble_path = Path(__file__).resolve().parent / "agent_preamble.md"
        if not preamble_path.is_file():
            return
        agents_md = workspace / "AGENTS.md"
        existing = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
        preamble = preamble_path.read_text(encoding="utf-8").strip()
        if preamble and preamble not in existing:
            content = f"{preamble}\n\n---\n\n{existing}" if existing.strip() else preamble
            agents_md.write_text(content, encoding="utf-8")
            logger.info("[NanobotAgent] 注入 agent_preamble.md 到 AGENTS.md")

    def _run_async(self, coro):
        """在持久事件循环中执行 async 协程，同步等待结果。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=60)

    async def _init_nanobot(self):
        """在事件循环中创建 Nanobot 实例。"""
        from nanobot import Nanobot
        nanobot = Nanobot.from_config(config_path=self._config_path)
        return nanobot

    async def _inject_quantdinger_tools(self):
        """注入 QuantDinger 的工具到 Nanobot ToolRegistry。"""
        from app.agent.nanobot_tools import (
            register_quantdinger_tools,
            register_call_skill_tool,
        )

        registry = self._agent_loop.tools

        # 注册 QuantDinger 的 80+ 工具
        registered = register_quantdinger_tools(
            registry, max_result_chars=self._agent_loop.max_tool_result_chars
        )

        # 注册 call_skill 工具（传递主事件循环引用）
        from app.agent.nanobot_tools import CallSkillToolAdapter
        CallSkillToolAdapter._main_loop = self._loop
        register_call_skill_tool(registry)

        logger.info("[NanobotAgent] 注入了 %d 个工具 + call_skill", len(registered))

    # ═════════════════════════════════════════════════════════
    # 公开 API（同步，供 Flask 路由调用）
    # ═════════════════════════════════════════════════════════

    def chat(
        self,
        message: str,
        session_id: str = "default",
        context: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None,
        user_id: int = 1,
    ) -> NanobotResult:
        """同步聊天接口（阻塞直到完成）。

        与原 smolagents _AgentExecutor.chat() 接口兼容。

        Args:
            message: 用户消息
            session_id: 会话 ID（映射到 Nanobot session_key）
            context: 上下文（stock_code, stock_name 等）
            progress_callback: 进度回调
            user_id: 用户 ID

        Returns:
            NanobotResult（兼容原 AgentResult）
        """
        # session_id → Nanobot session_key
        session_key = f"qd:{session_id}"

        # 拼接上下文到消息
        enriched = self._enrich_message(message, context)

        # 追责 Hook
        hook = TraceCollectorHook(session_id=session_id, user_query=message)
        domain = self._detect_domain(message, context)
        stock_code = (context or {}).get("stock_code", "")
        stock_name = (context or {}).get("stock_name", "")
        hook.setup_collector(domain=domain, stock_code=stock_code, stock_name=stock_name)

        # 设置 contextvar，让工具层能访问 hook
        _current_hook.set(hook)

        # 在持久事件循环中执行
        try:
            result = self._run_async(
                self._agent_loop.process_direct(
                    enriched,
                    session_key=session_key,
                )
            )
        except Exception as e:
            logger.error("[NanobotAgent] chat 失败: %s", e, exc_info=True)
            return NanobotResult(success=False, error=str(e))

        content = result.content if result else ""

        # 金融领域：DecisionCard 格式化
        if domain == "finance" and content:
            content = self._maybe_format_decision_card(content, context)

        # 追责：存库
        if hook.collector and content:
            try:
                hook.on_agent_finish(
                    final_content=content,
                    total_steps=0,  # Nanobot 不暴露 step 数
                    total_tokens=0,
                    model=self._agent_loop.model,
                )
            except Exception as e:
                logger.warning("[NanobotAgent] TraceCollector 存库失败: %s", e)

        return NanobotResult(
            success=bool(content),
            content=content,
            tool_calls_log=hook._tools_used,
            model=self._agent_loop.model,
        )

    def chat_stream(
        self,
        message: str,
        session_id: str = "default",
        context: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None,
        user_id: int = 1,
    ) -> Generator[Dict[str, Any], None, None]:
        """流式聊天接口（生成 SSE 事件）。

        与原 smolagents _AgentExecutor.chat_stream() 接口兼容。
        """
        session_key = f"qd:{session_id}"
        enriched = self._enrich_message(message, context)
        domain = self._detect_domain(message, context)

        # 追责 Hook（流式也需要）
        hook = TraceCollectorHook(session_id=session_id, user_query=message)
        stock_code = (context or {}).get("stock_code", "")
        stock_name = (context or {}).get("stock_name", "")
        hook.setup_collector(domain=domain, stock_code=stock_code, stock_name=stock_name)
        _current_hook.set(hook)

        # 流式输出缓冲区
        _stream_buf = []
        _stream_done = threading.Event()

        async def _on_stream(delta: str):
            if delta:
                _stream_buf.append({"type": "generating", "message": delta})

        async def _on_stream_end(*, resuming: bool = False):
            pass

        # 在事件循环中异步执行
        async def _run_stream():
            try:
                result = await self._agent_loop.process_direct(
                    enriched,
                    session_key=session_key,
                    on_stream=_on_stream,
                    on_stream_end=_on_stream_end,
                )
                content = result.content if result else ""
                if domain == "finance" and content:
                    content = self._maybe_format_decision_card(content, context)

                # 追责：存库
                if hook.collector and content:
                    try:
                        hook.on_agent_finish(
                            final_content=content,
                            total_steps=0,
                            total_tokens=0,
                            model=self._agent_loop.model,
                        )
                    except Exception as e:
                        logger.warning("[NanobotAgent] 流式 TraceCollector 存库失败: %s", e)

                _stream_buf.append({
                    "type": "done",
                    "success": bool(content),
                    "content": content,
                    "model": self._agent_loop.model,
                    "session_id": session_id,
                })
            except Exception as e:
                _stream_buf.append({"type": "error", "message": str(e)})
            finally:
                _stream_done.set()

        future = asyncio.run_coroutine_threadsafe(_run_stream(), self._loop)

        # 生成事件
        while not _stream_done.is_set() or _stream_buf:
            while _stream_buf:
                yield _stream_buf.pop(0)
            if not _stream_done.is_set():
                time.sleep(0.05)

        # 确保异常被捕获
        try:
            future.result(timeout=1)
        except Exception as e:
            yield {"type": "error", "message": str(e)}

    # ═════════════════════════════════════════════════════════
    # 内部方法
    # ═════════════════════════════════════════════════════════

    def _enrich_message(self, message: str, context: Optional[Dict[str, Any]]) -> str:
        """拼接上下文信息到消息。"""
        parts = []
        if context:
            if context.get("stock_code"):
                parts.append(f"股票代码: {context['stock_code']}")
            if context.get("stock_name"):
                parts.append(f"股票名称: {context['stock_name']}")
            if context.get("realtime_quote"):
                import json
                parts.append(f"[实时行情]\n{json.dumps(context['realtime_quote'], ensure_ascii=False)[:2000]}")
        parts.append(message)
        return "\n".join(parts)

    def _detect_domain(self, message: str, context: Optional[Dict[str, Any]]) -> str:
        """简单领域检测（基于关键词，不调用 LLM）。"""
        if context and context.get("stock_code"):
            return "finance"
        finance_keywords = ["股票", "行情", "分析", "买入", "卖出", "涨", "跌",
                           "K线", "均线", "MACD", "RSI", "选股", "回测"]
        if any(kw in message for kw in finance_keywords):
            return "finance"
        code_keywords = ["代码", "函数", "bug", "重构", "git", "commit", "文件"]
        if any(kw in message for kw in code_keywords):
            return "coding"
        trade_keywords = ["持仓", "交易", "下单", "策略", "启停"]
        if any(kw in message for kw in trade_keywords):
            return "trading"
        return "chat"

    def _maybe_format_decision_card(self, content: str,
                                     context: Optional[Dict[str, Any]]) -> str:
        """如果内容包含结构化决策数据，格式化为 DecisionCard。"""
        import json
        import re

        # 尝试从内容中提取 JSON
        patterns = [
            r'```json\s*\n?(.*?)\n?\s*```',
            r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',
        ]
        data = None
        for pat in patterns:
            m = re.search(pat, content, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1).strip())
                    if isinstance(data, dict) and "action" in data:
                        break
                except (json.JSONDecodeError, TypeError):
                    data = None

        if data and isinstance(data, dict) and "action" in data:
            return _format_decision_card(data)

        return content

    def close(self):
        """关闭事件循环和线程。"""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread.is_alive():
                self._thread.join(timeout=5)
            logger.info("[NanobotAgent] 事件循环已关闭")


# ═══════════════════════════════════════════════════════════════
# DecisionCard 格式化（从 agent.py 提取，无 smolagents 依赖）
# ═══════════════════════════════════════════════════════════════

_TIMEFRAME_CN = {
    "T+1": "1天", "T+3": "3天", "T+5": "5天",
    "1W": "1周", "1M": "1月", "3M": "3月", "1Y": "1年",
}


def _format_decision_card(data: dict) -> str:
    """将 agent 输出的 JSON 格式化为用户可见的标准卡片。"""
    action_cn = {"buy": "买入", "sell": "卖出", "hold": "观望", "skip": "跳过"}
    conf_cn = {"high": "高", "medium": "中", "low": "低"}
    dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    tf = _TIMEFRAME_CN.get(data.get("timeframe", ""), data.get("timeframe", ""))

    lines = [
        f"**{action_cn.get(data['action'], '观望')}** {data.get('stock_name', '')}({data.get('stock_code', '')})",
        f"维度:{tf} 评分:{data['score']:.0f} 方向:{dir_cn.get(data['direction'], '中性')} 置信:{conf_cn.get(data['confidence'], '中')}",
    ]

    # 因子明细
    if data.get("factors"):
        parts = []
        for f in data["factors"]:
            s = f"{f['score']:.0f}" if f.get("score") is not None else "—"
            parts.append(f"{f['name']}:{s}")
        lines.append(" | ".join(parts))

    # 信号
    if data.get("signal"):
        lines.append(f"信号: {data['signal']}")

    # 详细分析（折叠）
    if data.get("analysis"):
        lines.append(f"\n<details><summary>详细分析</summary>\n\n{data['analysis']}\n</details>")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 单例获取
# ═══════════════════════════════════════════════════════════════

def get_nanobot_agent(
    config_path: Optional[str] = None,
    force_config: bool = False,
) -> NanobotAgent:
    """获取 NanobotAgent 单例。

    首次调用时自动初始化（生成配置、创建事件循环、注入工具）。
    后续调用返回同一实例。

    Args:
        config_path: Nanobot 配置文件路径（默认 ~/.nanobot/config.json）
        force_config: 强制重新生成配置

    Returns:
        NanobotAgent 实例
    """
    return NanobotAgent(config_path=config_path, force_config=force_config)


def shutdown_nanobot_agent():
    """关闭 NanobotAgent 单例。"""
    if NanobotAgent._instance:
        NanobotAgent._instance.close()
        NanobotAgent._instance = None
