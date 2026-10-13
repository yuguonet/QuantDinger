# -*- coding: utf-8 -*-
"""统一 LLM 出口 —— 由 mimoagent `models/` 驱动（P1：去重 + 换轮子）。

为什么有这个文件
────────────────
smolagents 时代自建了 `app/agent/llm/`（base / factory / openai_llm /
dashscope_llm / qd_llm，共 908 行）做多 provider 抽象。切到 mimoagent 之后，
mimo 自带 `models/`（chat / anthropic / responses 三种协议）已经覆盖了同一件事，
而且更完整：tenacity 重试、流式聚合、token 统计、reasoning_signature 保链
（部分网关 withholding 推理明文，丢了会切断思维链）。

`qd_agent.py` 文件头早就写了「被本模块取代的旧件（P4 删除）：… llm/* …」，
但那套代码一直活着，还被 chain/ 的 7 处业务引用 —— 文档与代码分裂。
本文件是 **agent 链路的唯一出口**：自建层退役，调用点改用这里。

★ 与 `app/services/llm.py` 的关系（2026-10-13 用户裁定：**两套并存，各用各的**）
──────────────────────────────────────────────────────────────────────────
`app/services/llm.py`（723 行，`LLMService`，requests 直连）服务的是**旧分析/指标/
策略链路**（`routes/indicator`、`routes/settings`、`routes/strategy`、
`services/experiment/runner`、`services/fast_analysis`、`services/polymarket_*`）；
本文件服务 **agent 链路**（`chain/`、`skills/`、`capabilities/func_overlap`、
`cli`、`app/market_cn/auto/tools/chat_why`）。

两者**不合并、不互调**，能力集不同：
  · services 那套独有：JSON mode、模型名归一化（`openai/gpt-4o` → provider）、
    Google Gemini 原生协议、fallback model 链 + 403/402 换 provider。
  · 本套独有：mimo 的 tenacity 5 次指数退避重试、reasoning_signature 保链、
    流式聚合、Model 按 (provider, base_url, model, timeout) 缓存。
需要对方的能力时**做能力平移**，不要直接换出口 —— 换成本套丢 mimo 的重试/保链，
换成 services 那套等于把 P1「mimo models 替代自建 LLM 层」的轮子退回去。

设计定调
────────
· 消息格式就是 OpenAI 原生 `list[dict]`，不再有自建 `ChatMessage` 这一层。
  mimo 的 Model.query() 吃的就是这个，多一层转换只是磨损。
· 协议统一走 `chat`（OpenAI 兼容）。五个 provider 的端点都是 OpenAI 兼容的，
  其中 **Google 必须落到 `/v1beta/openai`** —— 原生 `/v1beta` 是 Gemini 协议，
  不是 OpenAI 的，直接填会 404。
· 同步 `complete()` 是主干，`acomplete()` 只是 `asyncio.to_thread` 包装。
  mimo 的 Model 是同步的，而 chain/ 里的调用点原本是 async，这层包装让调用点
  改动最小。**注意**：to_thread 的 wait_for 杀不掉线程，超时只能"放弃等待"，
  线程会跑完 —— 所以超时必须靠 `timeout` 参数从内部收紧，不要依赖 asyncio。
· 失败**抛异常**，不返回 `finish_reason='error'` 的静默降级件（旧 QDLLM 的行为）。
  chain/ 的调用点本来就 try/except fail-open，抛出来反而让失败可见。

⚠ 易错点
────────
1. mimo `get_model()` 内部会 `deepcopy(config)` 并强制 `config["model_name"]`，
   所以 model 名必须同时出现在 `model_name` 字段里，只靠位置参数不够稳。
2. mimo `OpenAIChatModelConfig.timeout` 默认 **3600 秒**。chain/ 的证据提取、
   judge 都是轻量调用，必须显式收紧，否则一次网络挂起就吊死整个闭环。
3. mimo `_query` 上的 tenacity 是硬编码 5 次指数退避（4~60s），改不了。
   单次 timeout 收紧后最坏是 5×timeout，这是"重试提高成功率"的既定代价。
4. Model 实例按 (provider, model, timeout) 缓存：每次 `get_model()` 都会 new 一个
   OpenAI client，chain/ 里一轮闭环要调几十次，不缓存等于每调用一次建一次连接。

用法
────
    from app.agent.llm import complete, acomplete

    resp = complete([{"role": "user", "content": "hi"}])
    print(resp.content)

    resp = await acomplete(msgs, temperature=0.2, timeout=20.0)
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from mimoagent.models import get_model as _mimo_get_model

logger = logging.getLogger(__name__)

__all__ = [
    "ProviderSpec",
    "LLMResponse",
    "PROVIDERS",
    "resolve_provider",
    "get_model",
    "complete",
    "acomplete",
]


# ── provider 表 ────────────────────────────────────────────────
# base_url 一律是 **OpenAI 兼容**端点。Google 那条的 `/openai` 后缀不是笔误：
# Gemini 原生 /v1beta 走的是 generateContent，接 OpenAI 协议会 404。
PROVIDERS: dict[str, tuple[str, str]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o"),
    "openai": ("https://api.openai.com/v1", "gpt-4o"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-1.5-flash"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "grok": ("https://api.x.ai/v1", "grok-beta"),
}

# 自动探测顺序，沿用 app/services/llm.py 的原优先级，避免切换后选到不同 provider。
_AUTO_ORDER = ("deepseek", "grok", "openai", "google", "openrouter")

# provider → API key 的 env 名（与 app/config/api_keys.py 对齐）
_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "grok": "GROK_API_KEY",
}


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _key_of(provider: str) -> str:
    return _env(_KEY_ENV.get(provider, ""))


def _default_timeout() -> float:
    """单次调用超时（秒）。默认 60，可用 QD_LLM_TIMEOUT 调。"""
    raw = _env("QD_LLM_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("[llm] QD_LLM_TIMEOUT 非数值，忽略: %r", raw)
    return 60.0


@dataclass
class ProviderSpec:
    """解析后的 provider 三要素。"""
    name: str
    base_url: str
    api_key: str
    model: str


@dataclass
class LLMResponse:
    """一次调用的结果。字段沿用旧 llm.base.LLMResponse，让调用点改动最小。"""
    content: str = ""
    tool_calls: list = field(default_factory=list)
    model: str = ""
    finish_reason: str = "stop"
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.finish_reason == "error"


def resolve_provider(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> ProviderSpec:
    """解析 provider / base_url / api_key / model。

    优先级：
      provider —— 显式参数 > `LLM_PROVIDER` > 自动探测（按 key 是否配置）
      base_url —— 显式参数 > `{PROVIDER}_BASE_URL` > provider 默认
      api_key  —— 显式参数 > `{PROVIDER}_API_KEY`
      model    —— 显式参数 > `AGENT_LLM_MODEL` > `{PROVIDER}_MODEL` > provider 默认

    显式 base_url/api_key 是给**自带网关**的调用方准备的（`app/market_cn` 的
    chat_why 走 dashscope 兼容端点，跟 agent 主链不是同一套配置）。传了就以传的
    为准 —— 不要用"provider 名"去猜一个自定义网关长什么样。

    未知 provider 直接抛 ValueError：静默回退到某个默认 provider 只会让"配置写错了"
    伪装成"模型答得不好"，排查成本高得多。
    """
    name = (provider or _env("LLM_PROVIDER")).lower()
    if not name:
        name = next((p for p in _AUTO_ORDER if _key_of(p)), "openrouter")
    if name not in PROVIDERS:
        raise ValueError(
            f"未知 LLM provider: {name!r}（可选：{', '.join(PROVIDERS)}）"
        )

    base_default, model_default = PROVIDERS[name]
    resolved_base = (base_url or "").strip().rstrip("/") or (
        _env(f"{name.upper()}_BASE_URL").rstrip("/") or base_default)
    resolved_key = (api_key or "").strip() or _key_of(name)
    resolved_model = (
        (model or "").strip()
        or _env("AGENT_LLM_MODEL")
        or _env(f"{name.upper()}_MODEL")
        or model_default
    )
    return ProviderSpec(
        name=name,
        base_url=resolved_base,
        api_key=resolved_key,
        model=resolved_model,
    )


# ── Model 缓存 ─────────────────────────────────────────────────
# 键含 timeout：不同调用点给的超时不同（c3p 证据提取 vs skill 酿造），
# 复用一个实例会让 timeout 形同虚设。
_MODEL_CACHE: dict[tuple, Any] = {}
_CACHE_LOCK = threading.Lock()


def get_model(
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    **extra: Any,
):
    """取一个 mimo Model 实例（带缓存）。

    缓存是**必须的**：`mimoagent.models.get_model()` 每次都会 new 一个 OpenAI
    client，chain/ 一轮闭环要调几十次 LLM，不缓存等于每次调用重建连接。

    ⚠ api_key 只以 hash 形式进缓存键 —— 明文 key 不该出现在任何可被 dump 的
    结构里（进程内 dict 也算）。
    """
    spec = resolve_provider(provider, model, base_url=base_url, api_key=api_key)
    tmo = timeout if timeout is not None else _default_timeout()
    key = (spec.name, spec.base_url, spec.model, tmo,
           hash(spec.api_key), tuple(sorted(extra.items())))
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        cfg = {
            "model_name": spec.model,
            "protocol": "chat",
            "api_key": spec.api_key or "EMPTY",
            "base_url": spec.base_url,
            # ⚠ 不填就是 3600 秒，闭环里一次网络挂起会吊死整条链。
            "timeout": tmo,
            "model_kwargs": {},
        }
        cfg.update(extra)
        m = _mimo_get_model(spec.model, cfg)
        _MODEL_CACHE[key] = m
        return m


def clear_model_cache() -> None:
    """测试/配置热更新用：清空 Model 缓存。"""
    with _CACHE_LOCK:
        _MODEL_CACHE.clear()


def _to_response(payload: dict, model_name: str = "") -> LLMResponse:
    """mimo 的 payload → LLMResponse。

    mimo 在 content / reasoning_content / tool_calls 全空时会抛 ValueError，
    所以能走到这里说明至少有一项非空。content 为空串是合法的（纯工具调用轮）。
    """
    payload = payload or {}
    tool_calls = payload.get("tool_calls") or []
    return LLMResponse(
        content=payload.get("content") or "",
        tool_calls=list(tool_calls),
        model=model_name,
        finish_reason="tool_calls" if tool_calls else "stop",
        metadata={k: v for k, v in payload.items()
                  if k not in ("content", "tool_calls")},
    )


def complete(
    messages: Sequence[dict],
    *,
    tools: Optional[list] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    **extra: Any,
) -> LLMResponse:
    """一次 LLM 调用（同步主干）。

    messages 是 OpenAI 原生 `list[dict]`。失败抛异常，不静默降级。
    """
    m = get_model(provider=provider, model=model, timeout=timeout,
                  base_url=base_url, api_key=api_key)
    kwargs: dict[str, Any] = {}
    # 不带 tools 时**不要**塞 None 或空列表：部分网关（含 openrouter 某些模型）
    # 见到空的 tools 字段会 400。
    if tools:
        kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    kwargs.update(extra)

    payload = m.query(list(messages), **kwargs)
    return _to_response(payload, model_name=getattr(m, "config", None) and getattr(m.config, "model_name", "") or "")


async def acomplete(messages: Sequence[dict], **kwargs: Any) -> LLMResponse:
    """`complete()` 的 async 包装，兼容 chain/ 里原有的 async 调用点。

    ⚠ `asyncio.wait_for` 取消不了已经在线程里阻塞的同步 HTTP 调用 —— 超时只会让
    调用方放弃等待，线程仍会跑完。所以超时一定要走 `timeout=` 参数，别指望 wait_for。
    """
    return await asyncio.to_thread(complete, messages, **kwargs)
