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
import json
import logging
import os
import threading
import time as _time
from time import perf_counter as _perf_counter
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from mimoagent.models import get_model as _mimo_get_model

from .upstream_compat import read_token_stats

logger = logging.getLogger(__name__)

__all__ = [
    "ProviderSpec",
    "LLMResponse",
    "PROVIDERS",
    "resolve_provider",
    "get_model",
    "complete",
    "acomplete",
    "complete_direct",
    "set_profile_sink",
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


def _freeze(obj: Any) -> Any:
    """把任意嵌套值转为可哈希的稳定表示（供缓存键用）。

    `get_model(**extra)` 的 extra 值可以是 dict（如 `model_kwargs`）/list/set，
    直接塞进缓存键会触发 `unhashable type`（F5 收敛后首次触发：主链传入
    `model_kwargs={...}`）。递归冻结保证键恒可哈希，且同值不同插入顺序得到
    同一键（dict 按键排序）。
    """
    if isinstance(obj, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in obj.items()))
    if isinstance(obj, (list, tuple)):
        return tuple(_freeze(v) for v in obj)
    if isinstance(obj, set):
        return frozenset(_freeze(v) for v in obj)
    return obj


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
           hash(spec.api_key), _freeze(extra))
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


# ── 调用级埋点（诊断用 / 2026-10-16）──────────────────────────
# 用户实测：最后一次 LLM 调用 token 巨大（16k→60k）、耗时极长，但当天分不清是
# 主循环 / 溯源重写 / C3P 证据提取。这里用 mimo 自带 token_stats 的**调用前后差值**
# 精确归因。默认关闭；`QD_LLM_PROFILE=1` 开，`QD_LLM_PROFILE_TRACE=1` 另落 trace。
def _llm_profile_enabled() -> bool:
    return (os.getenv("QD_LLM_PROFILE") or "").strip() not in ("", "0", "false", "False")


# 进程级 trace sink：`QD_LLM_PROFILE_TRACE=1` 时把调用画像也写进 trace。
# 【为什么需要设置器】`_profile_call` 里 `getattr(TraceAdapter, "_profile_sink", None)`
# 原先是**死代码**——该属性从未被赋值 ⇒ 即使开了 TRACE 也写不进 trace
# （2026-10-16 偏移审计实测）。由 `QDAgent.__init__`（trace 已建）挂上。
_PROFILE_SINK = None


def set_profile_sink(adapter) -> None:
    """显式设置 trace 落点（传 None 取消；一般不必要，见 `_resolve_profile_sink`）。"""
    global _PROFILE_SINK
    _PROFILE_SINK = adapter


def _resolve_profile_sink():
    """惰性解析 trace 落点（仅 `QD_LLM_PROFILE_TRACE=1` 时）。

    【为什么不从 QDAgent 注入】`qd_agent.py` 有 ≤420 行守卫，注入块把它顶超；
    而 `TraceAdapter()` 默认就写 `app/agent/traces/qd_agent_runs.jsonl`
    —— 与 agent 的默认路径**同一个文件**，本模块自行构造即可对齐。
    `QD_TRACE_DIR` 可覆盖目录（与 config.trace_dir 同义）。
    """
    global _PROFILE_SINK
    if _PROFILE_SINK is not None:
        return _PROFILE_SINK
    try:
        from .audit.trace_adapter import TraceAdapter
        out = (os.getenv("QD_TRACE_DIR") or "").strip() or None
        _PROFILE_SINK = TraceAdapter(out)
    except Exception:
        _PROFILE_SINK = False          # 用 False 记住"试过且失败"，避免反复重试
    return _PROFILE_SINK or None


def _profile_call(model: Any, messages: Sequence[dict], t0: float, err: str = "") -> None:
    if not _llm_profile_enabled():
        return
    try:
        st = read_token_stats(model)
        n = int(getattr(model, "n_calls", 0) or 0)
        inp = int(getattr(st, "input_tokens", 0) or 0)
        out = int(getattr(st, "output_tokens", 0) or 0)
        # 差值口径：token_stats 是**模型实例累计**（实例按 (provider,base_url,model,timeout)
        # 缓存 ⇒ 同一调用点连续多次调用会累加）。埋点只报累计值与调用数，
        # 归因由"调用前后两次读数的差"在分析脚本里算 —— 这样不会因实例复用而失真。
        # 调用方归因：取栈帧里**第一个 app/agent 下的业务帧**（跳过 llm/profile 自身）。
        # 为什么需要：用户看到"4 次调用"，但说不出是哪 4 处发起的 ⇒ 无法判断是否重复。
        caller = ""
        try:
            import traceback as _tb
            for fr in reversed(_tb.extract_stack()[:-2]):
                fn = fr.filename.replace("\\", "/")
                if "/app/agent/" in fn and not fn.endswith(("llm.py", "trace_collector.py")):
                    caller = "%s:%d:%s" % (fn.split("/app/agent/")[-1], fr.lineno, fr.name)
                    break
        except Exception:
            pass
        rec = {
            "event": "llm_call",
            "caller": caller,
            "model": str(getattr(getattr(model, "config", None), "model_name", "") or ""),
            "timeout": getattr(getattr(model, "config", None), "timeout", None),
            "n_calls": n,
            "cum_input_tokens": inp,
            "cum_output_tokens": out,
            "cum_total_tokens": inp + out,
            "msg_count": len(list(messages or [])),
            "in_chars": sum(len(str(m.get("content") or ""))
                            for m in (messages or []) if isinstance(m, dict)),
            "elapsed_ms": round((_perf_counter() - t0) * 1000, 1),
            "error": err,
        }
        logger.warning("[LLM-PROFILE] %s", json.dumps(rec, ensure_ascii=False))
        if (os.getenv("QD_LLM_PROFILE_TRACE") or "").strip() not in ("", "0", "false", "False"):
            try:
                _sink = _resolve_profile_sink()
                if _sink is not None:
                    _sink.emit("llm_call", **rec)
            except Exception:
                pass
    except Exception:
        pass


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

    _t0 = _perf_counter()
    try:
        payload = m.query(list(messages), **kwargs)
    except Exception as _e:
        _profile_call(m, messages, _t0, err="%s: %s" % (type(_e).__name__, _e))
        raise
    _profile_call(m, messages, _t0)
    return _to_response(payload, model_name=getattr(m, "config", None) and getattr(m.config, "model_name", "") or "")


def complete_direct(
    messages: Sequence[dict],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    attempts: int = 2,
    gap_s: float = 6.0,
    provider: Optional[str] = None,
) -> LLMResponse:
    """**独立 HTTP 通道**：绕过 mimoagent 的 tenacity 重试，自己掌控超时与尝试次数。

    【为什么需要它·2026-10-16 实测】mimo `OpenAIChatModel._query` 上的 tenacity 是
    **硬编码 5 次**（4~60s 退避，改不了）⇒ 一次 `complete()` 的墙钟 =
    5 × (单次 timeout + 退避)。实测 `timeout=5.0` 实际耗时 **52.9s**；
    C3P 默认 `QD_C3P_LLM_TIMEOUT=20` ⇒ 最坏 ≈125s 且 5 次全超时（网关对本任务
    本身就慢：成功调用实测 15.4 / 77.5 / 81.3 / 189.7 秒）。

    本函数把三个旋钮交回调用方：
      · `timeout` —— **读取超时**（秒），给足（C3P 场景建议 ≥120s，默认 QD_LLM_TIMEOUT）；
      · `attempts` —— 总尝试次数（默认 2，**不是 5**）；
      · `max_tokens` —— 显式限制输出（C3P 只要一段 JSON）。
    并返回**精确 usage**（网关 `usage` 字段 ⇒ LLMResponse 的 prompt/completion tokens）。

    ⚠ 与 `complete()` 的分工：主链路、其余 6 个调用点**继续用 `complete()`**
      （它们依赖 mimo 的保链/流式聚合）；只有"需要精确控时 + 要 usage"的
      旁路（C3P 证据提取）走本通道。两个出口并存是有意为之。
    """
    import requests

    spec = resolve_provider(provider=provider, model=model)
    tmo = timeout if timeout is not None else _default_timeout()
    url = spec.base_url.rstrip("/") + "/chat/completions"
    body: dict[str, Any] = {"model": spec.model, "messages": list(messages)}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    last_err: Optional[BaseException] = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        t0 = _perf_counter()
        try:
            resp = requests.post(
                url, json=body,
                headers={"Authorization": "Bearer " + (spec.api_key or ""),
                         "Content-Type": "application/json"},
                timeout=(10.0, float(tmo)),      # (连接, 读取)
            )
            if resp.status_code != 200:
                raise RuntimeError("HTTP %s: %s" % (resp.status_code, resp.text[:200]))
            data = resp.json()
            choice = ((data.get("choices") or [{}])[0].get("message") or {})
            usage = data.get("usage") or {}
            out = LLMResponse(
                content=choice.get("content") or "",
                model=spec.model,
                finish_reason=str((data.get("choices") or [{}])[0].get("finish_reason") or "stop"),
                tokens_used=int(usage.get("total_tokens") or 0),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                metadata={"attempt": attempt, "transport": "direct"},
            )
            _log_direct_call(spec, messages, out, (_perf_counter() - t0) * 1000.0, attempt, "")
            return out
        except BaseException as e:              # noqa: BLE001 —— fail-open 由调用方负责
            last_err = e
            _log_direct_call(spec, messages, None, (_perf_counter() - t0) * 1000.0,
                             attempt, "%s: %s" % (type(e).__name__, str(e)[:160]))
            if attempt < max(1, int(attempts)):
                _time.sleep(float(gap_s))
    raise last_err if last_err else RuntimeError("complete_direct: 无尝试")


def _log_direct_call(spec: ProviderSpec, messages: Sequence[dict],
                     resp: Optional[LLMResponse], elapsed_ms: float,
                     attempt: int, err: str) -> None:
    """独立通道的调用留痕（与 complete() 的 [LLM-PROFILE] 同前缀，便于一次 grep）。"""
    if not _llm_profile_enabled():
        return
    try:
        rec = {
            "event": "llm_call",
            "transport": "direct",
            "model": spec.model,
            "attempt": attempt,
            "msg_count": len(list(messages or [])),
            "in_chars": sum(len(str(m.get("content") or ""))
                            for m in (messages or []) if isinstance(m, dict)),
            "elapsed_ms": round(elapsed_ms, 1),
            "input_tokens": (resp.prompt_tokens if resp else None),
            "output_tokens": (resp.completion_tokens if resp else None),
            "error": err,
        }
        logger.warning("[LLM-PROFILE] %s", json.dumps(rec, ensure_ascii=False))
    except Exception:
        pass


async def acomplete(messages: Sequence[dict], **kwargs: Any) -> LLMResponse:
    """`complete()` 的 async 包装，兼容 chain/ 里原有的 async 调用点。

    ⚠ `asyncio.wait_for` 取消不了已经在线程里阻塞的同步 HTTP 调用 —— 超时只会让
    调用方放弃等待，线程仍会跑完。所以超时一定要走 `timeout=` 参数，别指望 wait_for。
    """
    return await asyncio.to_thread(complete, messages, **kwargs)
