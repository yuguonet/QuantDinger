# -*- coding: utf-8 -*-
"""
LLM 工厂

根据配置自动创建对应的 LLM 实例。

默认使用 QuantDinger 的 LLMService（读取 .env 中的 LLM_PROVIDER 等配置），
也保留了原始 DashScope / OpenAI Provider 以兼容独立运行场景。

使用方式：
    from app.agent.llm import create_llm

    # 默认：走 QuantDinger LLMService（推荐）
    llm = create_llm()

    # 指定 provider
    llm = create_llm({"provider": "openrouter", "model": "gpt-4o"})

    # 使用原始 DashScope（独立运行场景）
    llm = create_llm({"provider": "dashscope", "model": "qwen-max", "api_key": "***"})
"""
import logging
import os
from typing import Union

from .base import LLMBase

logger = logging.getLogger(__name__)

# Provider 注册表（原始实现，独立运行时使用）
_PROVIDER_REGISTRY: dict[str, type[LLMBase]] = {}


def register_provider(name: str, cls: type[LLMBase]):
    """注册 LLM Provider"""
    _PROVIDER_REGISTRY[name] = cls
    logger.debug(f"注册 LLM Provider: {name} -> {cls.__name__}")


def _ensure_default_providers():
    """确保默认 Provider 已注册"""
    if not _PROVIDER_REGISTRY:
        try:
            from .dashscope_llm import DashScopeLLM
            register_provider("dashscope", DashScopeLLM)
        except ImportError:
            pass
        try:
            from .openai_llm import OpenAILLM
            register_provider("openai", OpenAILLM)
        except ImportError:
            pass


# ── QuantDinger LLMService Provider 名称映射 ──────────────────
_QD_PROVIDER_MAP = {
    "openrouter": "openrouter",
    "openai": "openai",
    "google": "google",
    "deepseek": "deepseek",
    "grok": "grok",
}


def _is_qd_available() -> bool:
    """检查 QuantDinger LLMService 是否可用。"""
    try:
        from .qd_llm import _load_llm_service
        _load_llm_service()
        return True
    except Exception:
        return False


def create_llm(config: Union[dict, object, None] = None) -> LLMBase:
    """
    根据配置创建 LLM 实例。

    优先级：
      1. 如果 config 中指定的 provider 是 dashscope/openai 且有 api_key → 原始实现
      2. 如果 QuantDinger LLMService 可用 → QDLLM（推荐）
      3. 降级到原始 Provider

    Args:
        config: 配置字典或配置对象，可选字段：
            - provider: LLM 提供商
            - model: 模型名称
            - api_key: API 密钥（QD 模式下可省略，由 .env 管理）
            - base_url / temperature / max_tokens / top_p / timeout
        None 时使用默认配置（走 .env）。
    """
    if config is None:
        config = {}

    _ensure_default_providers()

    # 解析配置
    if isinstance(config, dict):
        provider = config.get("provider", "")
        model = config.get("model", "")
        api_key = config.get("api_key", "")
        extra_kwargs = {
            k: v for k, v in config.items()
            if k not in ("provider", "model", "api_key") and v is not None
        }
    else:
        provider = getattr(config, "provider", "")
        model = getattr(config, "model", "")
        api_key = getattr(config, "api_key", "")
        extra_kwargs = {}
        for attr in ("base_url", "temperature", "max_tokens", "top_p", "timeout"):
            val = getattr(config, attr, None)
            if val is not None:
                extra_kwargs[attr] = val

    # ── 策略 1: 指定了 api_key + 原始 provider → 直接用原始实现 ──
    if api_key and provider in _PROVIDER_REGISTRY:
        cls = _PROVIDER_REGISTRY[provider]
        logger.info(f"创建原始 LLM: provider={provider}, model={model}")
        return cls(model=model, api_key=api_key, **extra_kwargs)

    # ── 策略 2: QuantDinger LLMService 可用 → QDLLM ──────────
    if _is_qd_available():
        from .qd_llm import QDLLM
        qd_provider = _QD_PROVIDER_MAP.get(provider, None) if provider else None
        logger.info(
            f"创建 QD LLM: provider={qd_provider or 'auto'}, model={model or 'default'}"
        )
        return QDLLM(
            model=model,
            api_key=api_key,
            provider=qd_provider,
            **extra_kwargs,
        )

    # ── 策略 3: 降级到原始 Provider ──────────────────────────
    if provider in _PROVIDER_REGISTRY:
        cls = _PROVIDER_REGISTRY[provider]
        logger.warning(f"QD LLMService 不可用，降级到原始 provider={provider}")
        return cls(model=model or "qwen-turbo", api_key=api_key, **extra_kwargs)

    available = list(_PROVIDER_REGISTRY.keys()) + ["qd (via LLMService)"]
    raise ValueError(
        f"无法创建 LLM: provider='{provider}' 不可用。"
        f"可用: {available}"
    )


def list_providers() -> list[str]:
    """列出所有可用的 Provider"""
    _ensure_default_providers()
    providers = list(_PROVIDER_REGISTRY.keys())
    if _is_qd_available():
        providers.append("qd (via LLMService)")
    return providers


class LLMFactory:
    """
    兼容 README 和 examples 的工厂类写法。

    新代码建议直接使用 create_llm(config)。
    """

    @staticmethod
    def create(**kwargs) -> LLMBase:
        """通过关键字参数创建 LLM 实例"""
        return create_llm(kwargs)

    @staticmethod
    def from_config(config: Union[dict, object]) -> LLMBase:
        """通过配置字典或配置对象创建 LLM 实例"""
        return create_llm(config)
