"""Gunicorn configuration for QuantDinger backend.

Background workers (strategy restore, portfolio monitor, etc.) are started
inside ``create_app()`` which is called once per worker.  We use gthread
(threads in a single worker) by default to keep a familiar single-process
model while still allowing concurrent I/O.  Increase ``workers`` for
higher throughput — background tasks are idempotent and use DB locks to
coordinate, so duplicate work is minimal.
"""
import os
import sys

# Make app/nanobot/ shadow any pip-installed nanobot-ai package.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

bind = f"{os.getenv('PYTHON_API_HOST', '0.0.0.0')}:{os.getenv('PYTHON_API_PORT', '5000')}"

# Default: 1 worker + 4 threads — same concurrency model as Flask dev server
# but with better stability and connection handling.
# !! 单进程假设（2026-09-11 写入，与 app/agent/DESIGN.md 对应）：
# agent 子系统的以下状态均为进程内单例，多 worker 会直接失效——
#   feedback 会话->root 映射 / ToolProvider 单例 / trace 模块级状态 / message_queue 队列与 worker 线程。
# 扩 worker 前必须先完成这四处的进程安全改造，否则请保持 GUNICORN_WORKERS=1，
# 并发吞吐用 GUNICORN_THREADS（gthread）扩展。
workers = int(os.getenv("GUNICORN_WORKERS", 1))
threads = int(os.getenv("GUNICORN_THREADS", 4))

worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 5

# Do NOT preload — background threads in create_app() rely on being in
# the actual worker process.  preload would start them in master then
# lose them after fork.
preload_app = False

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

limit_request_line = 8190
limit_request_fields = 100
