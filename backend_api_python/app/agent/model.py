# -*- coding: utf-8 -*-
"""
Model Adapter — 桥接 QuantDinger LLM 配置到 smolagents OpenAIModel。

读取 LLMService 的 provider/API key/base URL，零配置重复。

被调用方：
  agent.py → build_model() → get_smolagent() 构建 Agent 实例
  planner.py → build_model() → Planner LLM 推理

公开接口：
  build_model(model, provider, temperature, **kwargs) → OpenAIModel
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from smolagents import OpenAIModel
import re as _re
import json as _json

from app.services.llm import LLMService, LLMProvider, PROVIDER_CONFIGS

logger = logging.getLogger(__name__)


def _fix_llm_output(text: str) -> str:
    """修复本地模型不遵循 final_answer() 格式的问题。
    如果 LLM 输出了裸 JSON (```json 块)，自动包装成 final_answer() 调用。"""
    if not text or "final_answer" in text:
        return text
    # 匹配 ```json ... ``` 块
    m = _re.search(r'```json\s*\n?(.*?)\n?\s*```', text, _re.DOTALL)
    if m:
        json_str = m.group(1).strip()
        try:
            _json.loads(json_str)  # 验证是合法 JSON
            # 替换为 final_answer() 调用
            prefix = text[:m.start()].rstrip()
            if prefix:
                prefix += "\n"
            return f'{prefix}<code>\nfinal_answer({json_str.replace("</code>", "<\\/code>")})\n</code>'
        except _json.JSONDecodeError:
            pass
    return text


class _WrappedOpenAIModel(OpenAIModel):
    """包装 OpenAIModel，在 generate 后自动修复输出格式。"""

    def generate(self, *args, **kwargs):
        result = super().generate(*args, **kwargs)
        if hasattr(result, 'content') and isinstance(result.content, str):
            result.content = _fix_llm_output(result.content)
        return result


def build_model(
    model: str = None,
    provider: str = None,
    temperature: float = 0.05,
    **kwargs,
) -> OpenAIModel:
    """Build a smolagents OpenAIModel using QuantDinger's LLM config.

    Args:
        model: Model ID (e.g. "gpt-4o", "deepseek-chat").
               If None, uses the provider's default model.
        provider: Provider name override (openrouter/openai/google/deepseek/grok).
        temperature: Sampling temperature. Default 0.05 for deterministic analysis.
                     (Previously 0.3 caused inconsistent BUY/SELL/HOLD on same input.)
        **kwargs: Extra kwargs passed to OpenAIModel.

    Returns:
        Configured OpenAIModel instance.
    """
    svc = LLMService(provider=provider)
    active_provider = svc.provider
    api_key = svc.get_api_key(active_provider)

    if not api_key:
        raise ValueError(
            f"No API key configured for {active_provider.value}. "
            f"Please set {active_provider.value.upper()}_API_KEY in .env"
        )

    base_url = svc.get_base_url(active_provider)
    model_id = model or os.getenv("AGENT_LLM_MODEL", "").strip() or svc.get_default_model(active_provider)

    # Normalize model name: strip provider prefix if it matches
    # e.g. "openai/gpt-4o" -> "gpt-4o" for OpenAI provider
    if "/" in model_id and active_provider != LLMProvider.OPENROUTER:
        prefix, actual = model_id.split("/", 1)
        prefix_map = {
            "openai": LLMProvider.OPENAI,
            "google": LLMProvider.GOOGLE,
            "deepseek": LLMProvider.DEEPSEEK,
            "x-ai": LLMProvider.GROK,
            "xai": LLMProvider.GROK,
        }
        if prefix_map.get(prefix.lower()) == active_provider:
            model_id = actual

    logger.info(
        "[ModelAdapter] provider=%s model=%s base_url=%s",
        active_provider.value, model_id, base_url,
    )

    return _WrappedOpenAIModel(
        model_id=model_id,
        api_base=base_url,
        api_key=api_key,
        temperature=temperature,
        **kwargs,
    )
