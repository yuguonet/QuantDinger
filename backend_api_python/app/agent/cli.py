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

# ── 路径设置（直接运行 cli.py 时需要，-m 方式由 __init__.py 处理）──
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_agent_dir = os.path.abspath(os.path.dirname(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)
except ImportError:
    pass


def _import_agent():
    """延迟导入 agent 组件（避免包命名空间冲突）。"""
    from agent import agent, settings, skills, DEFAULT_SESSION_ID, llm
    return agent, settings, skills, DEFAULT_SESSION_ID, llm


# ═══════════════════════════════════════════════════════════════
#  核心：统一 Agent 对话（组件从 agent.py 导入）
# ═══════════════════════════════════════════════════════════════

async def _run_chat(message: str, session_id: str = "cli"):
    """统一对话入口：通过消息队列执行，和 Flask/Cron 同一条链路。"""
    from message_queue import submit

    print(f"\n📎 Session: {session_id}")
    print(f"💬 Message: {message}")
    print(f"🔧 模式: task | 技能: {len(_import_agent()[2])} 个")
    print("-" * 50)

    future = submit(message, session_id=session_id, timeout=300)
    content = future.result(timeout=300)
    print(f"\n{content}")
    return content


# ═══════════════════════════════════════════════════════════════
#  信息显示
# ═══════════════════════════════════════════════════════════════

def _print_info():
    """显示配置信息。"""
    _, settings, skills, _, _ = _import_agent()

    print("=" * 60)
    print("QuantDinger Agent — CLI")
    print("=" * 60)
    print(f"\n  环境: {settings.env}")
    print(f"  版本: {settings.version}")
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
    print("\n🔧 工具通过 list_tools/search_tools 动态发现。")


def _list_skills():
    """列出所有技能。"""
    _, _, skills, _, _ = _import_agent()

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

    _, _, _, DEFAULT_SESSION_ID, _ = _import_agent()
    session_id = args.session or DEFAULT_SESSION_ID

    if args.message:
        try:
            from app.agent.llm.qd_llm import _load_llm_service
            LLMService_cls, _, _ = _load_llm_service()
            svc = LLMService_cls()
            if not svc.get_api_key():
                print(f"⚠️  警告: {svc.provider.value} API Key 未配置")
        except Exception:
            pass

        from message_queue import init_workers
        init_workers(4)
        asyncio.run(_run_chat(args.message, session_id))
    else:
        # 交互模式（单个事件循环，避免 PostgresMemory 连接池随循环销毁重建）
        import signal
        from message_queue import init_workers
        init_workers(4)

        _ctrl_c_count = 0

        def _force_exit(signum, frame):
            """第二次 Ctrl+C 强制退出"""
            nonlocal _ctrl_c_count
            _ctrl_c_count += 1
            if _ctrl_c_count >= 2:
                print("\n👋 强制退出!")
                os._exit(0)
            print("\n⚠️ 再按一次 Ctrl+C 强制退出")

        print(f"\n🤖 QuantDinger Agent CLI")
        print(f"📎 Session: {session_id}")
        print(f"💡 /quit 退出，Ctrl+C 中断当前任务\n")

        async def _interactive_loop():
            nonlocal _ctrl_c_count
            _, _, _, _, llm = _import_agent()
            while True:
                try:
                    _ctrl_c_count = 0  # 每轮重置
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
                    signal.signal(signal.SIGINT, _force_exit)
                    await _run_chat(message, session_id)
                except KeyboardInterrupt:
                    print("\n⚠️ 已中断")
                except Exception as e:
                    print(f"\n❌ 异常: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    _ctrl_c_count = 0
                    signal.signal(signal.SIGINT, signal.default_int_handler)

            # 退出时关闭 LLM 客户端，释放连接
            if llm and hasattr(llm, 'close'):
                await llm.close()

        asyncio.run(_interactive_loop())


if __name__ == "__main__":
    main()
