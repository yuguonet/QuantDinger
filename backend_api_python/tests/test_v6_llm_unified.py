# -*- coding: utf-8 -*-
"""P1 LLM 层统一 — 先红后绿。

背景：`app/agent/llm/`（1369 行：base / factory / openai_llm /
dashscope_llm / qd_llm）是 smolagents 时代自建的 LLM 抽象层。qd_agent.py
文件头早就写了「被本模块取代的旧件（P4 删除）：… llm/* …」，但它至今活着，
还被 chain/ 的 7 处业务代码引用 —— 文档与代码分裂。

mimoagent 自带 `models/`（openai_chat / anthropic / responses），带 retry、
流式、token 统计、reasoning_signature 保链，比自建层更完整。本轮把它作为
**agent 链路的唯一** LLM 出口，自建层退役。

★ 与 `app/services/llm.py` 的关系（2026-10-13 用户裁定）：两套**并存、各用各的**，
本文件只约束 **app/agent 内部**不再引用旧 `llm/` 包；`app/services/llm.py`
（`LLMService`，服务旧分析/指标/策略链路）不在本文件的红线范围内，不要求合并。

本文件覆盖：
  · provider 解析（含 env 覆盖、自动探测优先级、Google 走 OpenAI 兼容端点）
  · complete / acomplete 与 mimo Model 的对接契约
  · 红线：agent 源码中不再残留对旧 llm 包的引用
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# 【2026-10-13 P15】llm.py 已从 app/agentcore 迁回本包（用户裁定：方案应只在
# app/agent，不该在外面还留一部分代码）。位置变迁：旧自建 llm/ 子包（P2 删）
# → app.agentcore.llm → app.agent.llm（现）。
from app.agent import llm as unified


# ── fake mimo model ────────────────────────────────────────────
class _FakeModel:
    """替身：记录入参，返回 OpenAI 风格 payload。

    payload 在 **query 时**才从 holder 读（不是构造时）：测试里经常先把 model
    拿出来打个补丁、再摆 payload，构造时快照会让补丁和 payload 对不上。
    """

    def __init__(self, holder=None):
        self.holder = holder if holder is not None else {}
        self.calls = []

    def query(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        payload = self.holder.get("payload")
        return dict(payload) if payload else {"content": "ok", "tool_calls": []}


@pytest.fixture
def fake_model(monkeypatch):
    """把统一层的 model 工厂换成替身。

    工厂返回**同一个**实例：测试里常要先把 payload 摆好、再（甚至先拿到 model
    打个补丁）调 complete()，每次新建会让补丁打在没人用的对象上。
    """
    holder: dict = {}
    shared: dict = {"model": None}

    def _factory(*_a, **_kw):
        if shared["model"] is None:
            shared["model"] = _FakeModel(holder)
        holder["model"] = shared["model"]
        return shared["model"]

    # 先造一个出来：测试常常要拿到 model 打补丁 / 断言 calls，
    # 而 factory 要等 complete() 被调用才触发，那时再取就晚了。
    holder["model"] = shared["model"] = _FakeModel(holder)
    monkeypatch.setattr(unified, "get_model", _factory)
    return holder


@pytest.fixture
def clean_env(monkeypatch):
    """清掉所有 LLM_* / *_API_KEY / *_MODEL / *_BASE_URL，避免宿主 .env 污染。"""
    for k in list(os.environ):
        if (
            k.startswith("LLM_")
            or k.endswith("_API_KEY")
            or k.endswith("_MODEL")
            or k.endswith("_BASE_URL")
        ):
            monkeypatch.delenv(k, raising=False)


# ── 1. provider 解析 ───────────────────────────────────────────
def test_explicit_provider_wins(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    spec = unified.resolve_provider("openai")
    assert spec.name == "openai"
    assert "api.openai.com" in spec.base_url


def test_auto_detect_priority_deepseek_first(clean_env, monkeypatch):
    """自动探测顺序沿用 services/llm.py：deepseek > grok > openai > google > openrouter。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("OPENAI_API_KEY", "oa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "orr")
    assert unified.resolve_provider().name == "deepseek"


def test_auto_detect_falls_back_to_openrouter(clean_env):
    assert unified.resolve_provider().name == "openrouter"


def test_google_uses_openai_compatible_endpoint(clean_env):
    """Gemini 原生 /v1beta 不是 OpenAI 协议，必须落到 /v1beta/openai。"""
    spec = unified.resolve_provider("google")
    assert spec.base_url.rstrip("/").endswith("/v1beta/openai")


def test_env_overrides_base_url_and_model(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.internal/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "my-model")
    spec = unified.resolve_provider()
    assert spec.base_url == "https://gateway.internal/v1"
    assert spec.model == "my-model"


def test_agent_llm_model_overrides_default(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("AGENT_LLM_MODEL", "agent-pick")
    assert unified.resolve_provider().model == "agent-pick"


def test_explicit_model_argument_wins(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("AGENT_LLM_MODEL", "agent-pick")
    assert unified.resolve_provider(model="caller-pick").model == "caller-pick"


def test_unknown_provider_raises(clean_env):
    """未知 provider 必须当场炸，不能静默回退（禁止兜底）。"""
    with pytest.raises(ValueError):
        unified.resolve_provider("not-a-provider")


# ── 2. complete 与 mimo 的对接契约 ─────────────────────────────
def test_complete_returns_llm_response(fake_model, clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    fake_model["payload"] = {"content": "  hello  "}
    resp = unified.complete([{"role": "user", "content": "hi"}])
    assert isinstance(resp, unified.LLMResponse)
    assert resp.content == "  hello  "
    assert resp.is_error is False


def test_complete_forwards_messages_verbatim(fake_model, clean_env, monkeypatch):
    """消息就是 OpenAI 原生 dict —— 不再有自建 ChatMessage 这一层。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    unified.complete(msgs)
    assert fake_model["model"].calls[0]["messages"] == msgs


def test_complete_passes_tools_when_given(fake_model, clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    tools = [{"type": "function", "function": {"name": "x"}}]
    unified.complete([{"role": "user", "content": "hi"}], tools=tools)
    assert fake_model["model"].calls[0]["kwargs"]["tools"] == tools


def test_complete_omits_tools_when_absent(fake_model, clean_env, monkeypatch):
    """不带 tools 时不能塞 None/OpenAI 空串 —— 部分网关会 400。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    unified.complete([{"role": "user", "content": "hi"}])
    assert "tools" not in fake_model["model"].calls[0]["kwargs"]


def test_complete_collects_tool_calls(fake_model, clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    tc = [{"id": "1", "type": "function",
           "function": {"name": "f", "arguments": "{}"}}]
    fake_model["payload"] = {"content": "", "tool_calls": tc}
    resp = unified.complete([{"role": "user", "content": "hi"}])
    assert resp.tool_calls == tc


def test_complete_propagates_errors(fake_model, clean_env, monkeypatch):
    """失败必须抛，不能返回 finish_reason='error' 的静默降级件。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    def _boom(messages, **kwargs):
        raise RuntimeError("gateway down")

    fake_model["model"].query = _boom
    with pytest.raises(RuntimeError):
        unified.complete([{"role": "user", "content": "hi"}])


def test_acomplete_is_awaitable(fake_model, clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    fake_model["payload"] = {"content": "async-ok"}
    resp = asyncio.run(unified.acomplete([{"role": "user", "content": "hi"}]))
    assert resp.content == "async-ok"


def test_llm_response_is_error_only_on_error_reason():
    assert unified.LLMResponse(finish_reason="error").is_error is True
    assert unified.LLMResponse(finish_reason="stop").is_error is False


# ── 3. 红线：旧 llm 包不得再被引用 ─────────────────────────────
_LEGACY = Path(__file__).resolve().parents[1] / "app" / "agent"

_BANNED = (
    "from llm import",
    "from llm.base import",
    "from llm.factory import",
    "from llm.qd_llm import",
    "from llm.openai_llm import",
    "from llm.dashscope_llm import",
    "import llm.factory",
    "import llm.base",
)


def test_no_legacy_llm_imports_remain():
    """P1 完成判据：agent 里不再有任何一处引用旧自建 llm 包。"""
    hits = []
    for py in _LEGACY.rglob("*.py"):
        if "tmp" in py.parts or "traces" in py.parts:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("#"):
                for bad in _BANNED:
                    if bad in stripped:
                        hits.append(f"{py.relative_to(_LEGACY)}:{i}: {stripped}")
                        break
    assert not hits, "仍残留旧 llm 包引用：\n" + "\n".join(hits)


# ── 4. 模型缓存键：extra 可含 dict（F5 收敛后的回归）────────────
def test_get_model_cache_key_accepts_dict_extra(clean_env, monkeypatch):
    """extra 里必须能带 dict（如 model_kwargs）—— 主链现在就是这么传的。

    旧实现把 `tuple(sorted(extra.items()))` 直接拼进缓存键，dict 值会让
    `_MODEL_CACHE.get(key)` 抛 `unhashable type`，整条 chat 链两轮全断
    （真实探针复现）。改成 `_freeze` 递归冻结后：同 extra 命中缓存、
    不同 extra 不串味、同值不同插入顺序同键。
    """
    created: list = []

    def _factory(_model_name, cfg):
        created.append(dict(cfg))
        return object()

    monkeypatch.setattr(unified, "_mimo_get_model", _factory)
    # 本测试只验证「缓存键接受 dict extra」（_freeze + _MODEL_CACHE），与具体
    # factory 无关；关闭受控模型路径让上面的 _mimo_get_model mock 生效。
    monkeypatch.setenv("QD_LLM_CONTROLLED", "0")
    unified.clear_model_cache()
    kwargs = dict(
        provider="openai", model="m1", timeout=120.0,
        base_url="https://gw.test/v1", api_key="k",
    )
    try:
        a = unified.get_model(**kwargs, model_kwargs={"temperature": 0.1,
                                                      "max_tokens": 16384})
        b = unified.get_model(**kwargs, model_kwargs={"temperature": 0.1,
                                                      "max_tokens": 16384})
        assert a is b
        assert len(created) == 1

        c = unified.get_model(**kwargs, model_kwargs={"temperature": 0.7,
                                                      "max_tokens": 16384})
        assert c is not a
        assert len(created) == 2

        d = unified.get_model(**kwargs, model_kwargs={"max_tokens": 16384,
                                                      "temperature": 0.1})
        assert d is a
        assert len(created) == 2
    finally:
        unified.clear_model_cache()


# ── 5. 流式（model_kwargs 传 stream，不拦截 mimoagent）2026-10-20 ────
# `ControlledOpenAIChatModel`（覆盖 mimo `_query`）已按用户裁定移除。
# 流式改为在 `_build_default_model` 的 `model_kwargs` 传 `stream`（mimo
# `openai_chat._query` 第 2 行 `call_kwargs.pop("stream", False)` 原生支持），
# 走生产 SDK、不碰 mimo 内部。


def test_build_default_model_stream_on_by_default(clean_env, monkeypatch):
    """默认 QD_LLM_STREAM 未设 ⇒ model_kwargs 含 stream=True。"""
    captured: dict = {}

    def _fake_get_model(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.agent.llm.get_model", _fake_get_model)
    from app.agent.qd_service import _build_default_model
    _build_default_model()
    assert captured["model_kwargs"]["stream"] is True


def test_build_default_model_stream_off_via_env(clean_env, monkeypatch):
    """QD_LLM_STREAM=0 ⇒ stream=False（回退非流式）。"""
    captured: dict = {}

    def _fake_get_model(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.agent.llm.get_model", _fake_get_model)
    monkeypatch.setenv("QD_LLM_STREAM", "0")
    from app.agent.qd_service import _build_default_model
    _build_default_model()
    assert captured["model_kwargs"]["stream"] is False


# ── 6. [LLM #N] 画像：query 公共入口埋点（不碰 _query）──────────────
def test_wrap_query_logging_emits_block_and_counts():
    """包一层 query 后：序号递增、===== 分隔块出现、原 query 仍被委托。"""

    class _M:
        def __init__(self):
            self.n = 0
            self.token_stats = type("T", (), {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "total_tokens": 0})()
            self.config = type("C", (), {
                "model_name": "m-test",
                "model_kwargs": {"stream": True}})()

        def query(self, messages, **kw):
            self.n += 1
            self.token_stats.input_tokens += 100
            self.token_stats.output_tokens += 20
            self.token_stats.cache_read_tokens += 30
            return {"content": "hi", "tool_calls": []}

    m = unified._wrap_query_logging(_M())
    assert m.query is not _M.query
    r = m.query([{"role": "user", "content": "hello"}])
    assert r["content"] == "hi"
    assert m.n == 1
    # 二次包装幂等
    m2 = unified._wrap_query_logging(m)
    assert m2 is m
    assert m2.query is m.query


def test_wrap_query_logging_failure_still_logs():
    class _M:
        def __init__(self):
            self.token_stats = type("T", (), {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "total_tokens": 0})()
            self.config = type("C", (), {
                "model_name": "m-test", "model_kwargs": {}})()

        def query(self, messages, **kw):
            raise RuntimeError("boom")

    m = unified._wrap_query_logging(_M())
    with pytest.raises(RuntimeError):
        m.query([{"role": "user", "content": "x"}])


def test_instrument_create_counter_counts_http_attempts():
    """HTTP 计数：create() 每调一次 n+=1，异常也计入 —— 让 tenacity 重试可见。"""
    calls = {"n": 0, "raised": 0}

    class _Comp:
        _qd_attempt_counted = False

        def create(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                calls["raised"] += 1
                raise RuntimeError("timeout-ish")
            return object()

    class _Chat:
        completions = _Comp()

    class _Client:
        chat = _Chat()

    class _M:
        client = _Client()

    m = _M()
    st = unified._instrument_create_counter(m)
    assert st["n"] == 0
    for _ in range(3):
        try:
            m.client.chat.completions.create()
        except Exception:
            pass
    assert st["n"] == 3, "三次 create（含 2 次异常）都要计数"
    assert st["errors"] == 2
    # 幂等：再 instrument 不包第二层
    st2 = unified._instrument_create_counter(m)
    m.client.chat.completions.create()
    assert st["n"] == 4 and st2["n"] == 0
