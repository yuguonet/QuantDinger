#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent CLI — 命令行入口（壳）。

用法（从 backend_api_python/ 目录运行）：
  # 交互模式
  python -m app.agent.cli

  # 单次对话
  python -m app.agent.cli "分析603466"

  # 显示配置信息
  python -m app.agent.cli --info

  # 列出可用工具
  python -m app.agent.cli --list-tools

  # 列出可用技能
  python -m app.agent.cli --list-skills
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

# ── 路径设置 ──────────────────────────────────────────────────
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_agent_dir = os.path.abspath(os.path.dirname(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_project_root, ".env"), override=False)
except ImportError:
    pass


def _ensure_agent_path():
    """确保 app/agent/ 在 sys.path 头部。"""
    if sys.path[0] != _agent_dir:
        try:
            sys.path.remove(_agent_dir)
        except ValueError:
            pass
        sys.path.insert(0, _agent_dir)


# ═══════════════════════════════════════════════════════════════
#  核心：统一 Agent 对话（组件从 agent.py 导入）
# ═══════════════════════════════════════════════════════════════

async def _run_chat(message: str, session_id: str = "cli"):
    """统一对话入口。"""
    _ensure_agent_path()
    from agent import agent, registry, skills

    mode = "task" if len(registry) > 0 else "chat"
    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print(f"🔧 模式: {mode} | 工具: {len(registry)} 个 | 技能: {len(skills)} 个")
    print("-" * 50)

    response = await agent.chat(message, session_id=session_id)

    print(f"\n{response.content}")
    if response.tool_calls:
        print(f"\n📊 工具调用: {len(response.tool_calls)} 个")
    return response.content


# ═══════════════════════════════════════════════════════════════
#  信息显示
# ═══════════════════════════════════════════════════════════════

def _print_info():
    """显示配置信息。"""
    _ensure_agent_path()
    from agent import settings, registry, skills

    print("=" * 60)
    print("QuantDinger Agent — CLI")
    print("=" * 60)
    print(f"\n  环境: {settings.env}")
    print(f"  版本: {settings.version}")
    print(f"  工具: {len(registry)} 个")
    print(f"  技能: {len(skills)} 个")

    try:
        from app.agent.llm.qd_llm import _load_llm_service
        LLMService_cls, _, _ = _load_llm_service()
        svc = LLMService_cls()
        print(f"\n  ── LLMService ──")
        print(f"  Provider: {svc.provider.value}")
        print(f"  Model: {svc.get_default_model()}")
        print(f"  API Key: {'已配置' if svc.get_api_key() else '未配置'}")
    except Exception as e:
        print(f"\n  LLMService: {e}")

    print("\n" + "=" * 60)


def _list_tools():
    """列出所有工具。"""
    _ensure_agent_path()
    from agent import registry

    print(f"\n🔧 可用工具 ({len(registry)} 个):\n")
    for name in registry.list_tools():
        tool = registry.get(name)
        desc = tool.description[:80] if tool else ""
        print(f"  {name:40s} {desc}")


def _list_skills():
    """列出所有技能。"""
    _ensure_agent_path()
    from agent import skills

    print(f"\n🎯 可用技能 ({len(skills)} 个):\n")
    for info in skills.list_skills():
        print(f"  {info['name']:30s} {info['description'][:60]}")


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="QuantDinger Agent CLI")
    parser.add_argument("message", nargs="?", help="消息内容")
    parser.add_argument("--session", "-s", default=None, help="Session ID")
    parser.add_argument("--info", action="store_true", help="显示配置信息")
    parser.add_argument("--list-tools", action="store_true", help="列出可用工具")
    parser.add_argument("--list-skills", action="store_true", help="列出可用技能")

    args = parser.parse_args()

    if args.info:
        _print_info()
        return
    if args.list_tools:
        _list_tools()
        return
    if args.list_skills:
        _list_skills()
        return

    session_id = args.session or f"cli-{int(time.time())}"

    if args.message:
        _ensure_agent_path()
        try:
            from app.agent.llm.qd_llm import _load_llm_service
            LLMService_cls, _, _ = _load_llm_service()
            svc = LLMService_cls()
            if not svc.get_api_key():
                print(f"⚠️  警告: {svc.provider.value} API Key 未配置")
        except Exception:
            pass

        asyncio.run(_run_chat(args.message, session_id))
    else:
        # 交互模式（单个事件循环，避免 PostgresMemory 连接池随循环销毁重建）
        print(f"\n🤖 QuantDinger Agent CLI")
        print(f"📎 Session: {session_id}")
        print(f"💡 /quit 退出\n")

        async def _interactive_loop():
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

                try:
                    await _run_chat(message, session_id)
                except Exception as e:
                    print(f"\n❌ 异常: {e}")
                    import traceback
                    traceback.print_exc()

        asyncio.run(_interactive_loop())


if __name__ == "__main__":
    main()
