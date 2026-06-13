# -*- coding: utf-8 -*-
"""
Nanobot 配置生成器 — 从 .env 生成 ~/.nanobot/config.json。

零配置：读取 QuantDinger 现有 .env，自动生成 Nanobot 配置。
幂等：多次运行结果相同，不覆盖用户手动修改。

用法：
  from app.agent.nanobot_config_gen import ensure_nanobot_config
  config_path = ensure_nanobot_config()
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── .env 字段 → Nanobot provider 映射 ────────────────────────
_PROVIDER_ENV_MAP = {
    # (env_prefix, nanobot_provider_key, default_base_url)
    "OPENROUTER": ("openrouter", "https://openrouter.ai/api/v1"),
    "OPENAI": ("openai", "https://api.openai.com/v1"),
    "DEEPSEEK": ("deepseek", "https://api.deepseek.com/v1"),
    "GOOGLE": ("gemini", None),
    "GROK": ("openai", "https://api.x.ai/v1"),  # xAI 兼容 OpenAI
    "ANTHROPIC": ("anthropic", None),
    "XIAOMI_MIMO": ("xiaomi_mimo", None),
}

# ── 默认配置 ─────────────────────────────────────────────────
_DEFAULT_CONFIG: Dict[str, Any] = {
    "agents": {
        "defaults": {
            "model": "openrouter/deepseek/deepseek-chat",
            "provider": "openrouter",
            "maxTokens": 4096,
            "contextWindowTokens": 32768,
            "maxToolIterations": 15,
            "timezone": "Asia/Shanghai",
            "botName": "QuantDinger",
            "botIcon": "📊",
            "maxToolResultChars": 16000,
            "sessionTtlMinutes": 0,
            "consolidationRatio": 0.5,
            "maxMessages": 30,
            # 禁用不需要的内置 skill，减少上下文（本地模型 prompt 空间宝贵）
            "disabledSkills": [
                "weather",
                "tmux",
                "clawhub",
                "skill-creator",
                "github",
                "cron",
                "summarize",
                "image-generation",
                "long-goal",
                "update-setup",
                "my",
                "memory",
            ],
        }
    },
    "providers": {},
    "tools": {
        "restrictToWorkspace": False,
        "exec": {"sandbox": False},
        "web": {"enabled": False},
        "imageGeneration": {"enabled": False},
        "cliApps": {"enable": False},
    },
    "channels": {},
}


def _load_dotenv_values() -> Dict[str, str]:
    """从 .env 文件读取所有键值对（不依赖 python-dotenv）。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    values: Dict[str, str] = {}
    if not env_path.is_file():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    # 同时读取 os.environ（.env 未设置时 fallback）
    for key in (
        "AGENT_LLM_MODEL", "AGENT_LLM_PROVIDER",
        "AGENT_MAX_STEPS", "AGENT_TIMEOUT_SECONDS",
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
        "GOOGLE_API_KEY", "GROK_API_KEY", "ANTHROPIC_API_KEY",
        "XIAOMI_MIMO_API_KEY", "XIAOMI_MIMO_API_BASE",
        "OLLAMA_BASE_URL", "OLLAMA_MODEL",
    ):
        if key not in values and key in os.environ:
            values[key] = os.environ[key]

    # 兼容旧配置格式：LLM_PROVIDER / OPENAI_MODEL / OPENAI_BASE_URL 等
    # 映射到 AGENT_LLM_* 格式
    if "AGENT_LLM_PROVIDER" not in values:
        _legacy_provider = values.get("LLM_PROVIDER", "").strip().lower()
        if _legacy_provider:
            values["AGENT_LLM_PROVIDER"] = _legacy_provider
    if "AGENT_LLM_MODEL" not in values:
        # 根据 provider 读对应的 MODEL 字段
        _provider = values.get("AGENT_LLM_PROVIDER", values.get("LLM_PROVIDER", "")).strip().lower()
        _model_key_map = {
            "openai": "OPENAI_MODEL",
            "openrouter": "OPENROUTER_MODEL",
            "deepseek": "DEEPSEEK_MODEL",
            "google": "GOOGLE_MODEL",
            "grok": "GROK_MODEL",
            "anthropic": "ANTHROPIC_MODEL",
            "ollama": "OLLAMA_MODEL",
            "xiaomi_mimo": "XIAOMI_MIMO_MODEL",
        }
        _model_key = _model_key_map.get(_provider, "OPENAI_MODEL")
        _model = values.get(_model_key, "").strip()
        if _model:
            values["AGENT_LLM_MODEL"] = _model

    # 兼容 OPENAI_BASE_URL → OPENAI_API_BASE
    if "OPENAI_API_BASE" not in values:
        _base = values.get("OPENAI_BASE_URL", "").strip()
        if _base:
            values["OPENAI_API_BASE"] = _base

    return values


def _detect_provider_and_model(env: Dict[str, str]) -> tuple[str, str]:
    """从 .env 推断 provider 和 model。"""
    provider = env.get("AGENT_LLM_PROVIDER", "").strip().lower()
    model = env.get("AGENT_LLM_MODEL", "").strip()

    # 如果指定了 provider
    if provider:
        if not model:
            # provider 默认 model
            _defaults = {
                "openrouter": "openrouter/deepseek/deepseek-chat",
                "openai": "gpt-4o",
                "deepseek": "deepseek-chat",
                "google": "gemini-2.0-flash",
                "grok": "grok-3",
                "anthropic": "claude-sonnet-4-20250514",
                "xiaomi_mimo": "mimo-v2.5-pro",
            }
            model = _defaults.get(provider, "deepseek-chat")
        return provider, model

    # 自动检测：有哪个 API key
    for prefix, (nb_key, _) in _PROVIDER_ENV_MAP.items():
        key_name = f"{prefix}_API_KEY"
        if env.get(key_name):
            _defaults = {
                "openrouter": "openrouter/deepseek/deepseek-chat",
                "openai": "gpt-4o",
                "deepseek": "deepseek-chat",
                "gemini": "gemini-2.0-flash",
                "anthropic": "claude-sonnet-4-20250514",
                "xiaomi_mimo": "mimo-v2.5-pro",
            }
            return nb_key, _defaults.get(nb_key, "deepseek-chat")

    # Ollama fallback
    if env.get("OLLAMA_BASE_URL") or env.get("OLLAMA_MODEL"):
        return "ollama", env.get("OLLAMA_MODEL", "qwen2.5:14b")

    # 最终 fallback
    return "openrouter", "openrouter/deepseek/deepseek-chat"


def _build_providers(env: Dict[str, str]) -> Dict[str, Any]:
    """从 .env 构建 providers 配置。"""
    providers: Dict[str, Any] = {}

    for prefix, (nb_key, default_base) in _PROVIDER_ENV_MAP.items():
        api_key = env.get(f"{prefix}_API_KEY", "").strip()
        if not api_key:
            continue
        cfg: Dict[str, Any] = {"apiKey": api_key}
        base = env.get(f"{prefix}_API_BASE", "").strip()
        # 兼容 OPENAI_BASE_URL 格式（旧配置常用）
        if not base and prefix == "OPENAI":
            base = env.get("OPENAI_BASE_URL", "").strip()
        if base:
            cfg["apiBase"] = base
        elif default_base:
            cfg["apiBase"] = default_base
        providers[nb_key] = cfg

    # Ollama 特殊处理
    ollama_base = env.get("OLLAMA_BASE_URL", "").strip()
    if ollama_base:
        providers["ollama"] = {"apiBase": ollama_base}

    return providers


def generate_nanobot_config(
    config_path: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Path:
    """从 .env 生成 Nanobot config.json。

    Args:
        config_path: 输出路径，默认 ~/.nanobot/config.json
        env: 环境变量字典，默认从 .env 读取

    Returns:
        配置文件路径
    """
    if config_path is None:
        config_path = Path.home() / ".nanobot" / "config.json"
    if env is None:
        env = _load_dotenv_values()

    config = json.loads(json.dumps(_DEFAULT_CONFIG))  # deep copy

    # provider + model
    provider, model = _detect_provider_and_model(env)
    config["agents"]["defaults"]["model"] = model
    config["agents"]["defaults"]["provider"] = provider

    # max steps（默认 12，与 agent-old 的 max_steps=10 接近）
    max_steps = env.get("AGENT_MAX_STEPS", "").strip()
    if max_steps and max_steps.isdigit():
        config["agents"]["defaults"]["maxToolIterations"] = int(max_steps)
    else:
        config["agents"]["defaults"]["maxToolIterations"] = 12

    # providers
    config["providers"] = _build_providers(env)

    # workspace
    workspace = str(Path(__file__).resolve().parent.parent)
    config["agents"]["defaults"]["workspace"] = workspace

    # 写入
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("[ConfigGen] 生成 Nanobot 配置: %s (provider=%s model=%s)",
                config_path, provider, model)
    return config_path


def ensure_nanobot_config(force: bool = False) -> Path:
    """确保 Nanobot 配置存在。

    Args:
        force: 强制重新生成（覆盖用户修改）

    Returns:
        配置文件路径
    """
    config_path = Path.home() / ".nanobot" / "config.json"
    if config_path.exists() and not force:
        logger.debug("[ConfigGen] 配置已存在，跳过生成: %s", config_path)
        return config_path
    return generate_nanobot_config(config_path)
