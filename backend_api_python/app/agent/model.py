# -*- coding: utf-8 -*-
"""
Model adapter — bridges QuantDinger's LLMService config to smolagents OpenAIModel.

Reads provider/API key/base URL from the existing LLMService configuration
so there's zero config duplication.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from smolagents import OpenAIModel

from app.services.llm import LLMService, LLMProvider, PROVIDER_CONFIGS

logger = logging.getLogger(__name__)


def build_model(
    model: str = None,
    provider: str = None,
    temperature: float = 0.3,
    **kwargs,
) -> OpenAIModel:
    """Build a smolagents OpenAIModel using QuantDinger's LLM config.

    Args:
        model: Model ID (e.g. "gpt-4o", "deepseek-chat").
               If None, uses the provider's default model.
        provider: Provider name override (openrouter/openai/google/deepseek/grok).
        temperature: Sampling temperature.
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

    return OpenAIModel(
        model_id=model_id,
        api_base=base_url,
        api_key=api_key,
        temperature=temperature,
        **kwargs,
    )
