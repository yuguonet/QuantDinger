#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent CLI — LangGraph 版本。

用法：
  python -m app.agent.run "帮我分析一下贵州茅台"
  python -m app.agent.run --session test "600519"
  python -m app.agent.run --info
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_project_root, ".env"), override=False)
except ImportError:
    pass


def _run_single(message: str, session_id: str = None):
    """单次调用。"""
    from app.agent.graph import chat

    if not session_id:
        session_id = f"cli-{int(time.time())}"

    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print("-" * 50)

    result = chat(message=message, session_id=session_id)

    print(f"\n{'✅ 成功' if result.success else '❌ 失败'}")
    if result.content:
        # 尝试格式化 JSON
        try:
            data = json.loads(result.content)
            print(f"\n{json.dumps(data, ensure_ascii=False, indent=2)}")
        except (json.JSONDecodeError, TypeError):
            print(f"\n{result.content}")
    if result.error:
        print(f"\n⚠️ 错误: {result.error}")

    print(f"\n📊 Steps: {result.total_steps} | Tokens: {result.total_tokens}")

    if result.tool_calls_log:
        print(f"\n🔧 工具调用 ({len(result.tool_calls_log)}):")
        for tc in result.tool_calls_log:
            print(f"  - {tc.get('tool', '?')}: success={tc.get('success', '?')}")

    return result


def _run_stream(message: str, session_id: str = None):
    """流式调用。"""
    from app.agent.graph import chat_stream

    if not session_id:
        session_id = f"cli-{int(time.time())}"

    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print("-" * 50)

    for ev in chat_stream(message=message, session_id=session_id):
        etype = ev.get("type", "")
        if etype == "tool_info":
            print(f"\n  {ev.get('message', '')}")
        elif etype == "tool_start":
            print(f"\n  🔧 {ev.get('tool', '?')}()")
        elif etype == "tool_done":
            status = "✅" if ev.get("success", True) else "❌"
            print(f"  {status} {ev.get('tool', '?')}")
        elif etype == "done":
            content = ev.get("content", "")
            if content:
                try:
                    data = json.loads(content)
                    print(f"\n{json.dumps(data, ensure_ascii=False, indent=2)}")
                except (json.JSONDecodeError, TypeError):
                    print(f"\n{content}")
            print(f"\n✅ 完成")
        elif etype == "error":
            print(f"\n❌ 错误: {ev.get('message', '')}")


def _print_info():
    """显示 agent 结构信息。"""
    from app.agent.graph import build_graph
    print("=" * 60)
    print("QuantDinger Agent — LangGraph 版本")
    print("=" * 60)

    # 图结构
    graph = build_graph()
    print(f"\n  节点: {list(graph.nodes.keys()) if hasattr(graph, 'nodes') else 'N/A'}")
    print(f"\n  图结构:")
    print(f"    prepare → planner")
    print(f"    planner → (skip: finalize | run: agent)")
    print(f"    agent → finalize")
    print(f"    finalize → (loop: prepare | end: END)")

    # Tools
    try:
        from app.agent.tools import registry as tool_registry
        tool_registry.discover()
        print(f"\n  工具: {len(tool_registry._tools)} 个")
    except Exception as e:
        print(f"\n  工具加载失败: {e}")

    # Skills
    try:
        from app.agent.semantics import get_all_skill_metas
        metas = get_all_skill_metas()
        print(f"  技能: {len(metas)} 个")
    except Exception:
        pass

    # LLM
    try:
        from app.services.llm import LLMService
        svc = LLMService()
        print(f"\n  LLM Provider: {svc.provider.value}")
    except Exception:
        pass

    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="QuantDinger Agent (LangGraph)")
    parser.add_argument("message", nargs="?", help="消息")
    parser.add_argument("--session", "-s", help="Session ID")
    parser.add_argument("--info", action="store_true", help="显示结构信息")
    parser.add_argument("--stream", action="store_true", help="流式输出")

    args = parser.parse_args()

    if args.info:
        _print_info()
        return

    if args.message:
        if args.stream:
            _run_stream(args.message, args.session)
        else:
            _run_single(args.message, args.session)
    else:
        # 交互模式
        session_id = args.session or f"cli-{int(time.time())}"
        print(f"\n🤖 QuantDinger Agent (LangGraph)")
        print(f"📎 Session: {session_id}")
        print(f"💡 输入 /quit 退出\n")

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

            _run_single(message, session_id)


if __name__ == "__main__":
    main()
