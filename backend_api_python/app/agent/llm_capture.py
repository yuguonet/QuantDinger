#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app/agent/llm_capture.py — LLM 调用录制探针（默认关闭，env 驱动）。

【定位】**纯观测**，不改任何调用行为。把 agent 每一轮 LLM 调用的输入
（messages + tools + tool_choice）、输出（正文 + 工具调用 + 推理标记）、
耗时、服务端 usage **原模原样**落 JSONL，供「独立复放 / 单步计时基准」
离线使用——即录制-复放-优化三段式里的第一段。

【为什么要有这个模块】
用户侧可观测到的只有一行 `[httpx2] INFO: HTTP Request: POST ... 200 OK`，
无法回答「哪一步慢、为什么慢」。实测（2026-10-19）已确认：
    · TTFB 仅 2.36s 而总耗时 144.8s ⇒ 输入处理不是瓶颈；
    · 同一 payload 三轮冷调用耗时 4.2s / 16s / 300s(超时)，
      completion_tokens 246~2476 ⇒ **慢的是模型推理长度，且不可控**。
要定位并优化它，必须先把每步真实 payload 录下来，离线逐条复放对比，
不能靠在 agent 里反复跑整条链路（一次 10 分钟且不可控）。

【两级观测】
  · Level 1（逻辑级）：patch `OpenAIChatModel.query` —— 每次 LLM 调用的
    messages 深拷贝 + 总耗时 + 结果/异常 + 调用方归因 + 输出内容。
  · Level 2（HTTP 尝试级）：patch openai SDK `Completions.create` ——
    tenacity 每次重试的计时 + 异常 + **服务端 usage 真值**（对拍计费）。
  两級用 threading.local 的 seq 关联：Level1 进入时压栈，Level2 直接读栈顶，
  ⇒ 重试可与逻辑调用一一对应（原 tmp 探针靠时间邻近配对，不可靠）。

【为什么增量 flush】
cli.py 单消息模式结尾 `os._exit(0)` 会跳过 atexit/finally ⇒ 必须逐条
flush + fsync，进程被强杀也不丢已捕获数据。

【开关（全部默认关闭，零开销）】
  QD_LLM_CAPTURE=<路径>        开启录制；未设 ⇒ 本模块完全 no-op
  QD_LLM_CAPTURE_LEVEL=12      1=仅逻辑级 / 2=仅 HTTP 级 / 12=两者（默认）
  QD_LLM_CAPTURE_MAXCHARS=8000 单条输出内容截断长度（防 JSONL 爆炸）

【用法】
  # cli.py / flask 已内置开关，直接：
  QD_LLM_CAPTURE=tmp/llm_capture_x.jsonl python -m app.agent.cli "分析郑州煤电"

  # 其它入口（cron / 脚本）显式安装：
  from app.agent.llm_capture import maybe_install
  maybe_install()          # 读 env，未开启则 no-op

【不做什么】不重放、不改 payload、不碰 mimoagent 内部（类级 patch 仅包一层
计时+拷贝，原函数原样调用）。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CALL_SEQ = 0
# ★ 逻辑调用栈（thread-local）：Level2 读它把 HTTP 尝试挂到具体 seq 上。
#   用 list 而非单值：一次逻辑调用内若有并发/嵌套调用也能各归各位。
_TLS = threading.local()

# 单条输出内容截断（防超大正文把 JSONL 撑爆；默认 8000 字符 ≈ 正文全量）
_MAX_CHARS = int(os.getenv("QD_LLM_CAPTURE_MAXCHARS", "8000") or 8000)


def _append(out_path: str, rec: dict) -> None:
    """增量落盘（线程安全 + fsync，防 os._exit 丢数据）。"""
    rec["ts"] = datetime.now().isoformat(timespec="milliseconds")
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _LOCK:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def _caller_frame() -> str:
    """取栈里第一个 app/agent 业务帧（跳过 llm.py 自身），用于归因到具体步骤。"""
    import traceback as _tb
    try:
        for fr in reversed(_tb.extract_stack()[:-2]):
            fn = fr.filename.replace("\\", "/")
            if "/app/agent/" in fn and not fn.endswith("llm.py"):
                return "%s:%d:%s" % (fn.split("/app/agent/")[-1], fr.lineno, fr.name)
    except Exception:
        pass
    return ""


def _stack_push(seq) -> None:
    stack = getattr(_TLS, "seq_stack", None)
    if stack is None:
        stack = []
        _TLS.seq_stack = stack
    stack.append(seq)


def _stack_pop():
    stack = getattr(_TLS, "seq_stack", None)
    if stack:
        return stack.pop()
    return None


def _stack_top():
    stack = getattr(_TLS, "seq_stack", None)
    return stack[-1] if stack else None


def _env_snapshot() -> dict:
    """运行期生效的 LLM/预算配置快照（复放脚本要对齐这些值）。"""
    keys = [
        "LLM_PROVIDER", "OPENAI_BASE_URL", "OPENAI_MODEL",
        "QD_LLM_TIMEOUT", "AGENT_MAX_STEPS", "QD_STEP_LIMIT", "QD_STEP_SCALE",
        "AGENT_RUN_TOKEN_BUDGET", "AGENT_RUN_TOOL_CALL_BUDGET",
        "QD_PRESELECT_SKILL_TOPK", "QD_PRESELECT_TOOL_TOPK",
        "CLI_RUN_TIMEOUT",
    ]
    snap = {}
    for k in keys:
        v = os.environ.get(k)
        if v is not None:
            # API key 只留长度，防泄进日志/交付物
            snap[k] = ("<len:%d>" % len(v)) if "KEY" in k else v
    return snap


def install_hooks(out_path: str, level: str = "12") -> bool:
    """装两级观测补丁。

    ★ 必须赶在**首次 LLM 调用之前**；patch 的是类方法，对后续所有实例生效。
    返回 True 表示至少装上了一级。失败不静默：抛错由调用方决定处置
    （cli.py 侧会告警后继续——观测失败不该阻断主任务）。
    """
    global _CALL_SEQ
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    installed = []

    # ── Level 1：逻辑级（OpenAIChatModel.query）──────────────────
    if "1" in level:
        from mimoagent.models.openai_chat import OpenAIChatModel
        _orig_query = OpenAIChatModel.query

        def patched_query(self, messages, **kwargs):
            global _CALL_SEQ
            with _LOCK:
                _CALL_SEQ += 1
                seq = _CALL_SEQ
            rec = {
                "event": "llm_logical_call",
                "seq": seq,
                "thread": threading.current_thread().name,
                "caller": _caller_frame(),
                "model": getattr(getattr(self, "config", None), "model_name", ""),
                "timeout_cfg": getattr(getattr(self, "config", None), "timeout", None),
                "msg_count": len(list(messages or [])),
                "in_chars": sum(len(str(m.get("content") or ""))
                                for m in (messages or []) if isinstance(m, dict)),
                # ★ 原模原样深拷贝：agent 循环会在 query 返回后往 messages 追加
                #   assistant 消息，存引用会被后续变异污染（复放时 payload 失真）。
                "messages": copy.deepcopy(list(messages or [])),
                "kwargs_keys": sorted(kwargs.keys()),
                # ★ tools 经 API 参数下发（DefaultAgent.get_model_query_kwargs
                #   ⇒ {"tools": _tool_definitions, "tool_choice": "auto"}），
                #   复放要忠实复现服务端计算必须原样带上。
                "kwargs_tools": copy.deepcopy(kwargs.get("tools")) if kwargs.get("tools") else None,
                "kwargs_tool_choice": kwargs.get("tool_choice"),
            }
            _stack_push(seq)
            t0 = time.perf_counter()
            try:
                payload = _orig_query(self, messages, **kwargs)
                rec["elapsed_s"] = round(time.perf_counter() - t0, 3)
                rec["outcome"] = "ok"
                p = payload or {}
                rec["resp_keys"] = sorted(p.keys())
                rec["resp_content_chars"] = len(str(p.get("content") or ""))
                rec["resp_tool_calls"] = len(p.get("tool_calls") or [])
                # ★ 推理标记：慢的根因指向隐藏 reasoning（实测 completion_tokens
                #   是正文的 10 倍），复放侧要据此判断"慢在想还是慢在写"。
                rec["resp_has_reasoning"] = bool(p.get("reasoning_content"))
                rec["resp_reasoning_chars"] = len(str(p.get("reasoning_content") or ""))
                # 输出内容（截断保护）：独立模式要拿它做质量对拍，不只是长度
                _c = p.get("content")
                if isinstance(_c, str) and _c:
                    rec["resp_content"] = _c[:_MAX_CHARS]
                if p.get("tool_calls"):
                    rec["resp_tool_call_names"] = [
                        (tc.get("function") or {}).get("name") if isinstance(tc, dict)
                        else getattr(getattr(tc, "function", None), "name", None)
                        for tc in p["tool_calls"]
                    ]
                return payload
            except BaseException as e:
                rec["elapsed_s"] = round(time.perf_counter() - t0, 3)
                rec["outcome"] = "error"
                rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:500])
                raise
            finally:
                _stack_pop()
                _append(out_path, rec)

        OpenAIChatModel.query = patched_query
        installed.append("L1")

    # ── Level 2：HTTP 尝试级（SDK Completions.create）────────────
    # 每次 tenacity 尝试 = 一次 create 调用；主循环非流式 ⇒ 计时覆盖
    # 完整请求-响应周期，可与服务端计费时刻对拍。
    if "2" in level:
        from openai.resources.chat.completions import Completions
        _orig_create = Completions.create

        def patched_create(self, *args, **kwargs):
            model = kwargs.get("model") or (args[0] if args else "")
            n_msgs = len(kwargs.get("messages") or [])
            stream = bool(kwargs.get("stream"))
            rec = {
                "event": "llm_http_attempt",
                "seq": _stack_top(),          # ★ 与逻辑调用一一对应（重试可见）
                "thread": threading.current_thread().name,
                "model": model,
                "stream": stream,
                "msg_count": n_msgs,
                "max_tokens": kwargs.get("max_tokens"),
            }
            t0 = time.perf_counter()
            try:
                resp = _orig_create(self, *args, **kwargs)
                rec["elapsed_s"] = round(time.perf_counter() - t0, 3)
                rec["outcome"] = "ok"
                # 服务端 usage 真值（对拍计费）
                try:
                    usage = getattr(resp, "usage", None)
                    if usage is not None:
                        rec["usage"] = {
                            "prompt_tokens": getattr(usage, "prompt_tokens", None),
                            "completion_tokens": getattr(usage, "completion_tokens", None),
                            "total_tokens": getattr(usage, "total_tokens", None),
                        }
                    rec["resp_id"] = getattr(resp, "id", None)
                    choices = getattr(resp, "choices", None) or []
                    if choices:
                        rec["finish_reason"] = getattr(choices[0], "finish_reason", None)
                except Exception:
                    pass
                return resp
            except BaseException as e:
                rec["elapsed_s"] = round(time.perf_counter() - t0, 3)
                rec["outcome"] = "error"
                rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
                raise
            finally:
                _append(out_path, rec)

        Completions.create = patched_create
        installed.append("L2")

    if installed:
        logger.info("[llm_capture] hooks %s -> %s", "+".join(installed), out_path)
    return bool(installed)


def maybe_install(environ=None) -> str:
    """按 env 决定是否装探针；返回 out_path（未开启返回空串）。

    cli.py / flask_app 入口调用；未设 QD_LLM_CAPTURE 时完全 no-op（零开销）。
    装失败只告警不阻断——观测不是主链路，不能因为探针问题让任务跑不了。
    """
    env = environ if environ is not None else os.environ
    out_path = (env.get("QD_LLM_CAPTURE") or "").strip()
    if not out_path:
        return ""
    level = (env.get("QD_LLM_CAPTURE_LEVEL") or "12").strip() or "12"
    try:
        install_hooks(out_path, level)
        _append(out_path, {"event": "meta", "env": _env_snapshot(),
                           "level": level, "pid": os.getpid()})
        return out_path
    except Exception as e:
        # 不静默：探针装不上意味着这轮运行没有可复放数据，事后无法归因
        logger.warning("[llm_capture] 安装失败（本次运行无录制数据）: %s: %s",
                       type(e).__name__, e)
        return ""
