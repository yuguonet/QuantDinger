# -*- coding: utf-8 -*-
"""
QuantDinger LLM 桥接层

将 app.services.llm.LLMService 包装为 agent 模板的 LLMBase 接口。
LLMService 是同步的，通过 asyncio.to_thread 桥接为 async。

被调用方：
  factory.py → create_llm() → QDLLM
  agents/chat_agent.py → llm.generate()
  agents/task_agent.py → llm.generate() + tool_calls
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from typing import AsyncIterator, Optional

from .base import LLMBase, LLMResponse, ChatMessage

logger = logging.getLogger(__name__)

# ── LLMService 懒加载（避免触发 app/__init__.py 污染 sys.path）──
_LLMService_cls = None
_LLMProvider_cls = None
_PROVIDER_CONFIGS = None


def _load_llm_service():
    """
    加载 LLMService，跳过 app.services.__init__（避免 yfinance 等重依赖）
    和 app/__init__.py（避免插入 app/ 到 sys.path 污染 config 命名空间）。

    直接用 importlib.util 加载 services/llm.py 文件。
    """
    global _LLMService_cls, _LLMProvider_cls, _PROVIDER_CONFIGS
    if _LLMService_cls is not None:
        return _LLMService_cls, _LLMProvider_cls, _PROVIDER_CONFIGS

    # 尝试正常 import（仅当 app 已经被完全加载，如 Flask 环境）
    if "app" in sys.modules and "app.services" in sys.modules:
        try:
            from app.services.llm import LLMService, LLMProvider, PROVIDER_CONFIGS
            _LLMService_cls = LLMService
            _LLMProvider_cls = LLMProvider
            _PROVIDER_CONFIGS = PROVIDER_CONFIGS
            return _LLMService_cls, _LLMProvider_cls, _PROVIDER_CONFIGS
        except (ImportError, ModuleNotFoundError):
            pass

    # 降级：直接加载文件，不触发 app/__init__.py
    llm_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "services", "llm.py"
    )
    llm_path = os.path.abspath(llm_path)
    if not os.path.exists(llm_path):
        raise ImportError(f"LLMService 文件不存在: {llm_path}")

    spec = importlib.util.spec_from_file_location("_qd_llm_svc", llm_path)
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LLMService_cls = mod.LLMService
        _LLMProvider_cls = mod.LLMProvider
        _PROVIDER_CONFIGS = mod.PROVIDER_CONFIGS
        return _LLMService_cls, _LLMProvider_cls, _PROVIDER_CONFIGS

    raise ImportError("无法加载 app.services.llm.LLMService")


class QDLLM(LLMBase):
    """
    QuantDinger LLM 实现

    通过 app.services.llm.LLMService 调用 LLM API，
    支持 OpenRouter / OpenAI / Google / DeepSeek / Grok 等多 Provider。

    配置来自 backend_api_python/.env（通过 dotenv 加载）：
      LLM_PROVIDER=openrouter
      OPENROUTER_API_KEY=***
      OPENROUTER_MODEL=openai/gpt-4o
    """

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        provider: str = None,
        **kwargs,
    ):
        super().__init__(model=model or "default", api_key=api_key or "", **kwargs)
        self._provider_override = provider
        self._svc = None

    def _get_service(self):
        """懒加载 LLMService。"""
        if self._svc is None:
            LLMService_cls, _, _ = _load_llm_service()
            self._svc = LLMService_cls(provider=self._provider_override)
        return self._svc

    def _resolve_model(self, svc) -> str:
        """解析实际模型名。"""
        if self.model and self.model != "default":
            return self.model
        agent_model = os.getenv("AGENT_LLM_MODEL", "").strip()
        if agent_model:
            return agent_model
        return svc.get_default_model()

    async def generate(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        """非流式生成（桥接 LLMService 同步调用）。"""
        svc = self._get_service()
        model = self._resolve_model(svc)
        temp = temperature if temperature is not None else self.temperature
        msg_dicts = [m.to_dict() for m in messages]

        try:
            if tools:
                result = await asyncio.to_thread(
                    svc.call_with_tools, msg_dicts, tools,
                    temperature=temp, model=model,
                )
                tc = result.get("tool_calls", [])
                logger.info("[QDLLM] call_with_tools 返回: content=%s, tool_calls=%d",
                           (result.get("content") or "")[:80], len(tc))
                return LLMResponse(
                    content=result.get("content") or "",
                    tool_calls=tc,
                    model=model,
                    finish_reason="tool_calls" if tc else "stop",
                    tokens_used=result.get("usage", {}).get("total_tokens", 0),
                    prompt_tokens=result.get("usage", {}).get("prompt_tokens", 0),
                    completion_tokens=result.get("usage", {}).get("completion_tokens", 0),
                )
            else:
                content = await asyncio.to_thread(
                    svc.call_llm_api, msg_dicts,
                    model=model, temperature=temp, use_json_mode=False,
                )
                return LLMResponse(content=content, model=model, finish_reason="stop")
        except Exception as e:
            logger.error("[QDLLM] generate 失败: %s", e, exc_info=True)
            return LLMResponse(content=f"LLM 调用异常: {e}", model=model, finish_reason="error")

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """流式生成（当前：非流式后逐块 yield，待 LLMService 支持原生 stream 替换）。"""
        response = await self.generate(messages, tools, temperature, max_tokens, **kwargs)
        if response.is_error:
            yield response.content
            return
        content = response.content
        for i in range(0, len(content), 100):
            yield content[i : i + 100]
