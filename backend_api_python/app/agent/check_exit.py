# -*- coding: utf-8 -*-
"""最简验证：跑完任务后进程是否退出"""
import sys, os, time, threading, queue, concurrent.futures

# 把 agent 目录加入 sys.path
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

def run_agent():
    """在独立线程里跑 agent，主线程监控进程是否退出"""
    import asyncio
    from agent import agent

    async def _task():
        # 最简单的任务：让 LLM 输出一个 final_answer
        return await agent.chat("写一行Python代码: print('hello')，然后调用 final_answer 输出结果。", session_id="test_exit")

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_task())
        print(f"[check_exit] agent.chat 完成，result={str(result)[:100]}")
        return str(result)
    finally:
        loop.close()

# 在独立线程跑 agent
t0 = time.time()
thread = threading.Thread(target=run_agent, name="agent-worker")
thread.start()
thread.join(timeout=60)

elapsed = time.time() - t0
if thread.is_alive():
    print(f"[check_exit] ❌ 60s 后线程仍在运行，进程不退")
    # 列出所有活着线程
    for st in threading.enumerate():
        print(f"  存活线程: {st.name} daemon={st.daemon}")
    sys.exit(1)
else:
    print(f"[check_exit] ✅ 线程正常退出，耗时 {elapsed:.1f}s")
    sys.exit(0)
