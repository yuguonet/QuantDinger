#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 独立调试入口。

用法：
  python -m app.agent.run                          # 交互式聊天
  python -m app.agent.run "帮我分析一下贵州茅台"    # 单次调用
  python -m app.agent.run --session test "600519"   # 指定 session
  python -m app.agent.run --info                    # 查看 agent 结构
  python -m app.agent.run --stream "600519 技术面"  # 流式输出

环境变量：
  LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL — LLM 配置
  AGENT_TYPE=code|tool                     — Agent 类型
  AGENT_MAX_STEPS=6                       — 最大步数
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# 确保项目根目录在 sys.path 中
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_project_root, ".env"), override=False)
except ImportError:
    pass


def _print_agent_info():
    """打印 agent 结构信息（不依赖 smolagents）"""
    print("=" * 60)
    print("QuantDinger Agent — 结构信息")
    print("=" * 60)

    # Tools
    try:
        from app.agent.tools import registry as tool_registry
        tool_registry.discover()
        total = len(tool_registry)
        for name in sorted(tool_registry._tools.keys()):
            spec = tool_registry.get(name)
            desc = spec.description[:60] if spec else ""
            print(f"    - {name}: {desc}")
        print(f"\n  总计: {total} 个工具")
    except Exception as e:
        print(f"\n  [!] 工具加载失败: {e}")

    # Session store
    try:
        from app.agent.session_store import get_session_store
        store = get_session_store()
        sessions = store.list_sessions(10)
        print(f"\n  Session store: {type(store).__name__}")
        print(f"  已有 sessions: {len(sessions)}")
        for s in sessions[:5]:
            print(f"    - {s['session_id']} (messages: {len(s.get('messages', []))})")
    except Exception as e:
        print(f"\n  [!] Session store 失败: {e}")

    # Domains (Phase 3: 从 persona.md 读取)
    try:
        from app.agent.semantics import get_persona
        persona = get_persona()
        if persona and persona.behaviors:
            domain_keys = [k for k in persona.behaviors.keys()
                           if k in ("finance", "trading", "coding", "system")]
            print(f"\n  领域行为规范: {len(domain_keys)} 个")
            for key in domain_keys:
                print(f"    - {key}: {len(persona.behaviors[key])} 条规则")
        else:
            print("\n  领域行为规范: 未加载")
    except Exception as e:
        print(f"\n  [!] 领域行为规范失败: {e}")

    # LLM config
    try:
        from app.services.llm import LLMService
        svc = LLMService()
        provider = svc.provider
        base_url = svc.get_base_url(provider)
        model = svc.get_default_model(provider)
        api_key = svc.get_api_key(provider)
        masked = (api_key[:8] + "..." + api_key[-4:]) if api_key and len(api_key) > 12 else ("***" if api_key else "(未设置)")
        print(f"\n  LLM Provider: {provider.value}")
        print(f"  Base URL:     {base_url}")
        print(f"  Model:        {model}")
        print(f"  API Key:      {masked}")
    except Exception as e:
        print(f"\n  [!] LLM 配置读取失败: {e}")

    print("\n" + "=" * 60)


def _run_single(message: str, session_id: str = None, skills: list = None):
    """单次 agent 调用"""
    from app.agent.agent import build_agent_executor
    from app.agent.session_store import get_session_store

    store = get_session_store()
    if not session_id:
        session_id = f"cli-{int(time.time())}"

    session = store.get_session(session_id)
    if not session:
        session = store.create_session(session_id, {})

    executor = build_agent_executor(
        user_id=1,
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "6")),
        timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "180")),
    )

    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print("-" * 50)

    result = executor.chat(
        message=message,
        session_id=session_id,
        context={},
        user_id=1,
    )

    print(f"\n{'✅ 成功' if result.success else '❌ 失败'}")
    if result.content:
        print(f"\n{result.content}")
    if result.error:
        print(f"\n⚠️ 错误: {result.error}")
    print(f"\n📊 Steps: {result.total_steps} | Tokens: {result.total_tokens} | Model: {result.model}")

    if result.tool_calls_log:
        print(f"\n🔧 工具调用 ({len(result.tool_calls_log)}):")
        for tc in result.tool_calls_log:
            print(f"  - {tc.get('tool', '?')}: {tc.get('output', '')[:100]}")

    return result


def _run_interactive(session_id: str = None):
    """交互式聊天"""
    from app.agent.agent import build_agent_executor
    from app.agent.session_store import get_session_store

    store = get_session_store()
    if not session_id:
        session_id = f"cli-{int(time.time())}"

    session = store.get_session(session_id)
    if not session:
        session = store.create_session(session_id, {})

    executor = build_agent_executor(
        user_id=1,
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "6")),
        timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "180")),
    )

    print(f"\n🤖 QuantDinger Agent 交互模式")
    print(f"📎 Session: {session_id}")
    print(f"💡 输入 /quit 退出, /clear 清空历史, /info 查看信息\n")

    while True:
        try:
            message = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见!")
            break

        if not message:
            continue
        if message == "/quit":
            print("👋 再见!")
            break
        if message == "/clear":
            store.clear_history(session_id)
            print("🗑️ 历史已清空")
            continue
        if message == "/info":
            _print_agent_info()
            continue

        result = executor.chat(
            message=message,
            session_id=session_id,
            context={},
            user_id=1,
        )

        if result.content:
            print(f"\nAgent> {result.content}\n")
        if result.error:
            print(f"\n⚠️ {result.error}\n")

        print(f"[steps={result.total_steps} tokens={result.total_tokens} model={result.model}]\n")


def main():
    parser = argparse.ArgumentParser(description="QuantDinger Agent 独立调试入口")
    parser.add_argument("message", nargs="?", help="单次消息 (省略则进入交互模式)")
    parser.add_argument("--session", "-s", help="Session ID")
    parser.add_argument("--skills", help="逗号分隔的 skill/indicator ID")
    parser.add_argument("--info", action="store_true", help="显示 agent 结构信息")
    parser.add_argument("--stream", action="store_true", help="流式输出 (单次模式)")

    args = parser.parse_args()

    if args.info:
        _print_agent_info()
        return

    skills = [s.strip() for s in args.skills.split(",")] if args.skills else None

    if args.message:
        if args.stream:
            _run_single_stream(args.message, args.session, skills)
        else:
            _run_single(args.message, args.session, skills)
    else:
        _run_interactive(args.session)


def _run_single_stream(message: str, session_id: str = None, skills: list = None):
    """单次 agent 调用 — 流式输出"""
    from app.agent.agent import build_agent_executor
    from app.agent.session_store import get_session_store

    store = get_session_store()
    if not session_id:
        session_id = f"cli-{int(time.time())}"

    session = store.get_session(session_id)
    if not session:
        session = store.create_session(session_id, {})

    executor = build_agent_executor(
        user_id=1,
        max_steps=int(os.getenv("AGENT_MAX_STEPS", "6")),
        timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "180")),
    )

    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print("-" * 50)

    for ev in executor.chat_stream(
        message=message,
        session_id=session_id,
        context={},
        user_id=1,
    ):
        etype = ev.get("type", "")
        if etype == "token":
            print(ev.get("content", ""), end="", flush=True)
        elif etype == "step":
            step = ev.get("step", {})
            print(f"\n  [Step {step.get('step_number', '?')}] {step.get('tool_calls', [{}])[0].get('tool_name', '') if step.get('tool_calls') else 'thinking'}")
        elif etype == "tool_call":
            print(f"\n  🔧 {ev.get('tool_name', '?')}({ev.get('arguments', '')})")
        elif etype == "done":
            print(f"\n\n✅ 完成 | steps={ev.get('total_steps', 0)} tokens={ev.get('total_tokens', 0)}")
        elif etype == "error":
            print(f"\n\n❌ 错误: {ev.get('message', '')}")

    print()


if __name__ == "__main__":
    main()
