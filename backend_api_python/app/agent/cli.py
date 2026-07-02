#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent CLI — 基于 agent 模板的命令行入口。

用法（从 backend_api_python/ 目录运行）：
  # 交互模式
  python -m app.agent.cli

  # 单次对话
  python -m app.agent.cli "帮我分析一下贵州茅台"

  # 带工具调用（ReAct 循环）
  python -m app.agent.cli --tools "查询贵州茅台的最新行情"

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
import json
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
    """确保 app/agent/ 在 sys.path 头部（防止 app/__init__.py 污染）。"""
    if sys.path[0] != _agent_dir:
        try:
            sys.path.remove(_agent_dir)
        except ValueError:
            pass
        sys.path.insert(0, _agent_dir)


# ═══════════════════════════════════════════════════════════════
#  工具模式：ReAct 循环
# ═══════════════════════════════════════════════════════════════

async def _run_with_tools(message: str, session_id: str = "cli"):
    """带工具调用的 ReAct 循环。"""
    _ensure_agent_path()
    from llm import create_llm, QDToolAdapter, QDSkillAdapter, run_with_tools
    from utils.prompt_loader import load_prompt

    llm = create_llm()
    adapter = QDToolAdapter()
    skills = QDSkillAdapter()

    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print(f"🔧 工具: {len(adapter)} 个 | 🎯 技能: {len(skills)} 个")
    print("-" * 50)

    tool_names = adapter.list_tools()
    tool_catalog = ", ".join(tool_names[:30])
    if len(tool_names) > 30:
        tool_catalog += f" ... 共 {len(tool_names)} 个"

    system_prompt = load_prompt(
        "tool_system.txt",
        tool_count=len(adapter),
        tool_catalog=tool_catalog,
        skill_catalog=skills.get_catalog_text(),
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    answer = await run_with_tools(llm, messages, adapter)

    print(f"\n{answer}")
    return answer


# ═══════════════════════════════════════════════════════════════
#  普通对话模式
# ═══════════════════════════════════════════════════════════════

async def _run_chat(message: str, session_id: str = "cli"):
    """普通对话（无工具）。"""
    _ensure_agent_path()
    from llm import create_llm, run_with_tools
    from utils.prompt_loader import load_prompt

    llm = create_llm()

    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print("-" * 50)

    system_prompt = load_prompt("chat_system.txt")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]
    answer = await run_with_tools(llm, messages, adapter=None)
    print(f"\n{answer}")
    return answer


# ═══════════════════════════════════════════════════════════════
#  信息显示
# ═══════════════════════════════════════════════════════════════

def _print_info():
    """显示配置信息。"""
    _ensure_agent_path()
    from app.agent.config.loader import get_settings
    settings = get_settings()

    print("=" * 60)
    print("QuantDinger Agent — CLI")
    print("=" * 60)
    print(f"\n  环境: {settings.env}")
    print(f"  版本: {settings.version}")

    print(f"\n  ── LLM 配置 ──")
    print(f"  Provider: {settings.llm.provider or '(自动检测)'}")
    print(f"  QD Provider: {settings.llm.qd_provider or '(自动检测)'}")
    print(f"  Model: {settings.llm.model or '(默认)'}")

    try:
        from app.agent.llm.qd_llm import _load_llm_service
        LLMService_cls, _, _ = _load_llm_service()
        svc = LLMService_cls()
        print(f"\n  ── QuantDinger LLMService ──")
        print(f"  Provider: {svc.provider.value}")
        print(f"  Model: {svc.get_default_model()}")
        print(f"  API Key: {'已配置' if svc.get_api_key() else '未配置'}")
    except Exception as e:
        print(f"\n  LLMService: {e}")

    print("\n" + "=" * 60)


def _list_tools():
    """列出所有工具。"""
    _ensure_agent_path()
    from llm import QDToolAdapter

    adapter = QDToolAdapter()
    print(f"\n🔧 可用工具 ({len(adapter)} 个):\n")
    for name in adapter.list_tools():
        schema = adapter.get_schema(name)
        desc = schema.get("function", {}).get("description", "")[:80] if schema else ""
        print(f"  {name:40s} {desc}")


def _list_skills():
    """列出所有技能。"""
    _ensure_agent_path()
    from llm import QDSkillAdapter

    adapter = QDSkillAdapter()
    print(f"\n🎯 可用技能 ({len(adapter)} 个):\n")
    for info in adapter.list_skills():
        tags = ", ".join(info["tags"]) if info["tags"] else ""
        print(f"  {info['name']:30s} {info['description'][:60]}")
        if tags:
            print(f"  {'':30s} [{tags}]")


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="QuantDinger Agent CLI")
    parser.add_argument("message", nargs="?", help="消息内容")
    parser.add_argument("--session", "-s", default=None, help="Session ID")
    parser.add_argument("--tools", "-t", action="store_true", help="启用工具调用（ReAct 循环）")
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
        # 检查 API Key
        _ensure_agent_path()
        try:
            from app.agent.llm.qd_llm import _load_llm_service
            LLMService_cls, _, _ = _load_llm_service()
            svc = LLMService_cls()
            if not svc.get_api_key():
                print(f"⚠️  警告: {svc.provider.value} API Key 未配置，工具调用将无法工作")
                print(f"   请在 backend_api_python/.env 中设置 {svc.provider.value.upper()}_API_KEY")
        except Exception:
            pass

        if args.tools:
            asyncio.run(_run_with_tools(args.message, session_id))
        else:
            asyncio.run(_run_chat(args.message, session_id))
    else:
        # 交互模式
        print(f"\n🤖 QuantDinger Agent CLI")
        print(f"📎 Session: {session_id}")
        print(f"💡 /quit 退出 | /tools 切换工具模式\n")

        use_tools = args.tools

        while True:
            try:
                prompt = "Tool> " if use_tools else "You> "
                message = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 再见!")
                break

            if not message:
                continue
            if message == "/quit":
                print("👋 再见!")
                break
            if message == "/tools":
                use_tools = not use_tools
                print(f"🔄 工具模式: {'开启' if use_tools else '关闭'}")
                continue

            try:
                if use_tools:
                    asyncio.run(_run_with_tools(message, session_id))
                else:
                    asyncio.run(_run_chat(message, session_id))
            except Exception as e:
                print(f"\n❌ 异常: {e}")
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    main()
