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

# gbk 控制台编码保护（与 run.py 一致）：模型输出可能含 emoji/特殊符号，
# 若 stdout 为 gbk 控制台会因无法编码崩溃。统一重配置为 utf-8 + replace。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 路径设置（**直接**运行 cli.py 时需要；`python -m app.agent.cli` 由 Python 自带）──
# 只挂 backend_api_python 一层（保证 `import app.*` 可用）。
#
# ★ 2026-10-14 修正（对照 P5 红线）：原实现还把 `app/agent` 本身也 insert 到
#   sys.path[0]，注释写着"agent 包内一律用裸名"——那是 **P5 之前**的旧口径。
#   P5 起全包统一 `app.agent.*`、活代码裸名 import 0 处，这段既无必要又有害：
#   实证（`tmp/probe_cli_syspath_20261014.py`）显示它执行后裸名 `tools`/`memory`/
#   `rag` 均可被 import，且 **`tools` 裸名与 `app.agent.tools` 是两个不同的
#   module 对象**（同一份文件加载两遍）= 事故 M-1（模块级单例双份、状态互不可见）
#   在 CLI 进程里的活跃风险敞口。
#   ⚠ 残留（解释器行为，改不掉）：直接跑 `python app/agent/cli.py` 时 Python 会
#   自动把脚本所在目录放进 sys.path[0]，裸名仍然可达 —— 所以**统一用
#   `python -m app.agent.cli`**（docstring 里的用法），别直接跑脚本文件。
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)
except ImportError:
    pass


def _import_agent():
    """延迟导入 agent 组件（避免包命名空间冲突）。"""
    # 【2026-10-13 P1】原来这里还导出一个进程级 `llm` 实例（旧自建 llm/ 包的
    # create_llm()），交互循环退出时 `await llm.close()`。LLM 出口统一到
    # `app.agent.llm` 后没有这个对象了 —— mimo 的 Model 没有 async close，
    # 连接随进程退出回收即可。
    from app.agent.agent import agent, settings, skills, DEFAULT_SESSION_ID
    return agent, settings, skills, DEFAULT_SESSION_ID


# ═══════════════════════════════════════════════════════════════
#  核心：统一 Agent 对话（组件从 agent.py 导入）
# ═══════════════════════════════════════════════════════════════

import concurrent.futures as concurrent_futures_timeout


async def _run_chat(message: str, session_id: str = "cli", _future_sink: list = None):
    """统一对话入口：通过消息队列执行，和 Flask/Cron 同一条链路。

    _future_sink: 可选列表，调用方传入后本次任务的 Future 会被 append 进去，
    供 SIGINT handler / KeyboardInterrupt 分支置取消位（Ctrl+C 协作中止）。
    """
    from app.agent.message_queue import submit

    print(f"\n[会话] Session: {session_id}")
    print(f"[对话] Message: {message}")
    print(f"[wrench] 模式: task | 技能: {len(_import_agent()[2])} 个")
    print("-" * 50)

    # 2026-09-14：原先 300s 是硬编码，多阶段任务跑到一半就被掐断
    # （实测 phase #1 单阶段就 188s，4 阶段必然超时，日志里只看到 TimeoutError，
    #   极易被误判成 agent 崩溃）。改为可配置，默认不变。
    _timeout = int(os.getenv("CLI_RUN_TIMEOUT", "900"))  # 2026-09-15：300→900（用户定调，多阶段任务完整跑完）
    future = submit(message, session_id=session_id, timeout=_timeout)
    if _future_sink is not None:
        _future_sink.append(future)

    # 2026-09-17 三修：future.result(900) 是一次 900s 的长等待——Windows 上
    # SIGINT handler 执行完返回后 wait 继续等、不抛 KeyboardInterrupt，
    # KeyboardInterrupt 分支永远不执行（用户体感"Ctrl+C 无效"）。
    # 改为 0.5s 分片轮询：每个分片醒来查一次停止位；停止位由 SIGINT handler /
    # KeyboardInterrupt 分支置上（message_queue.request_stop）——
    # worker 的中断探针在工具/步边界中止 agent.run，这里随即收尾。
    from app.agent.message_queue import request_stop as _req_stop, _is_stop_requested

    _stop_asked = False
    deadline = time.time() + _timeout
    content = None
    while True:
        try:
            content = future.result(timeout=0.5)
            break  # 正常完成
        except concurrent_futures_timeout.TimeoutError:
            pass
        except KeyboardInterrupt:
            _stop_asked = True
        if _stop_asked or _is_stop_requested(str(session_id)):
            if not _stop_asked:
                pass
            print("\n⚠ 已请求停止，等 worker 在当前工具/步边界停下…（再按 Ctrl+C 强制退出）")
            # 等 worker 收尾（探针中止 run → worker set_result('') → result 返回）
            try:
                content = future.result(timeout=30)
            except BaseException:
                content = "[run_error] 用户中断"
            break
        if time.time() > deadline:
            future.cancel()
            content = f"[run_error] 超过 {_timeout}s 未完成"
            break
    print(f"\n{content}")
    return content


def _flush_and_exit(code: int = 0) -> None:
    """确保输出落盘后直接结束进程。

    2026-09-14：任务跑完（结果已打印、决策树与 qd_agent_traces 已落库）后进程却挂住不退出。
    原因不在 agent：数据源层有**模块级长生命周期线程池**
    （`app/data_sources/coordinator.py` 的 `_timeout_pool` / `_mkline_timeout_pool`），
    而 `concurrent.futures` 自身注册的 atexit 钩子会 **join** 这些 worker——只要有一个
    worker 卡在无超时的网络请求上，进程就永远退不出去（后台 agent 线程都已是 daemon，
    不是它们的锅）。
    CLI 是一次性进程，跑完即退是正确语义，故显式 flush 后直接结束。
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


# ═══════════════════════════════════════════════════════════════
#  信息显示
# ═══════════════════════════════════════════════════════════════

def _print_info():
    """显示配置信息。"""
    # ⚠️ _import_agent() 返回 4 元组 (agent, settings, skills, DEFAULT_SESSION_ID)。
    # 早期版本按 5 个解包，必然 ValueError 崩溃（--info 从未真正工作过）。
    _, settings, skills, _ = _import_agent()

    print("=" * 60)
    print("QuantDinger Agent — CLI")
    print("=" * 60)
    print(f"\n  环境: {settings.env}")
    print(f"  版本: {settings.version}")
    print(f"  技能: {len(skills)} 个")

    try:
        from app.agent.llm import resolve_provider
        spec = resolve_provider()
        print(f"\n  ── LLM（mimoagent models）──")
        print(f"  Provider: {spec.name}")
        print(f"  Base URL: {spec.base_url}")
        print(f"  Model: {spec.model}")
        print(f"  API Key: {'已配置' if spec.api_key else '未配置'}")
    except Exception as e:
        print(f"\n  LLM: {e}")

    print("\n" + "=" * 60)


def _list_tools():
    """列出所有工具。"""
    print("\n[wrench] 工具通过 list_tools/search_tools 动态发现。")


def _list_skills():
    """列出所有技能。"""
    _, _, skills, _ = _import_agent()   # 4 元组，见 _print_info 处的说明

    print(f"\n[skills] 可用技能 ({len(skills)} 个):\n")
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

    _, _, _, DEFAULT_SESSION_ID = _import_agent()
    session_id = args.session or DEFAULT_SESSION_ID

    if args.message:
        try:
            from app.agent.llm import resolve_provider
            spec = resolve_provider()
            if not spec.api_key:
                print(f"⚠  警告: {spec.name} API Key 未配置")
        except Exception:
            pass

        from app.agent.message_queue import init_workers
        init_workers(4)
        asyncio.run(_run_chat(args.message, session_id))
        # 单次消息模式：跑完即退（见 _flush_and_exit 说明——否则会被数据源线程池拖住）
        _flush_and_exit(0)
    else:
        # 交互模式（单个事件循环，避免 PostgresMemory 连接池随循环销毁重建）
        import signal
        from app.agent.message_queue import init_workers
        init_workers(4)

        _ctrl_c_count = 0
        _active_future = []  # 当前在跑的任务 Future（_run_chat 注册，_force_exit 取消）

        def _force_exit(signum, frame):
            """第一次：置协作取消位；第二次：立即强退。

            2026-09-17 修复：旧实现计数被交互循环 finally 清零，强退通道永不可达。
            """
            nonlocal _ctrl_c_count
            _ctrl_c_count += 1
            if _ctrl_c_count >= 2:
                print("\n 强制退出!")
                os._exit(0)
            try:
                from app.agent.message_queue import request_stop
                request_stop(session_id)
            except Exception:
                pass
            print("\n⚠ 已请求停止；再按一次 Ctrl+C 强制退出")

        print(f"\n[机器人] QuantDinger Agent CLI")
        print(f"[会话] Session: {session_id}")
        print(f"[提示] /quit 退出，Ctrl+C 中断当前任务\n")

        async def _interactive_loop():
            nonlocal _ctrl_c_count
            _import_agent()
            while True:
                try:
                    _ctrl_c_count = 0  # 每轮重置
                    message = input("You> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n 再见!")
                    break

                if not message:
                    continue
                _active_future.clear()
                if message == "/quit":
                    print(" 再见!")
                    break

                try:
                    signal.signal(signal.SIGINT, _force_exit)
                    await _run_chat(message, session_id, _future_sink=_active_future)
                except KeyboardInterrupt:
                    # 2026-09-17 Ctrl+C 修复：置取消位让 worker 协作中止；
                    # 连续第二次 Ctrl+C 由 _force_exit 直接 os._exit（计数不再被清零）
                    print("\n⚠ 已请求中断（再按一次 Ctrl+C 强制退出）")
                except Exception as e:
                    print(f"\n 异常: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    signal.signal(signal.SIGINT, signal.default_int_handler)

            # 【2026-10-13 P1】原先在这里 `await llm.close()` 关 LLM 客户端。
            # 统一到 `app.agent.llm` 后没有这个对象：mimo 的 Model 只持一个
            # openai client，没有 async close，连接随进程退出回收。
            # 需要显式释放的场合调 `app.agent.llm.clear_model_cache()`。

        try:
            asyncio.run(_interactive_loop())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass  # 退出期信号，忽略


if __name__ == "__main__":
    main()
