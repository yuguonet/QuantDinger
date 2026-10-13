# -*- coding: utf-8 -*-
"""cron — 定时任务模块。

cron_tools.py  — CRUD 函数（create/list/remove/toggle）
cron_worker.py — 后台调度执行器（Timer 自调度）

────────────────────────────────────────────────────────────
★ 本包是对外能力出口（D-2，2026-10-14 记录）——依赖方向是单向的：

    外部消费方 ──► app.agent.cron        （允许，且是预期形态）
                     · app/__init__.py          启动 worker（start_cron_worker）
                     · app/routes/cron.py       CRUD + 手工执行
                     · app/routes/health.py     健康检查 / SSE 订阅

    app.agent.cron ──X─► app.routes / app.services   （禁止：会与消费方成环）

【为什么记录这个】设计文档 D-2 原记「cron/ 被旧系统反向依赖，删旧系统会断掉
定时任务」。复核后结论：方向本身符合"方案代码只在 app/agent"（agent 是提供方），
真正的缺口是**契约没写下来** —— 谁在消费、删旧系统时该把启动点搬到哪，全靠猜。
现在：契约写在上面，方向红线与导出名由
`tests/test_v6_cron_boundary.py` 钉住（改导出名 / 反向 import / 删契约注释都会红）。

【删旧系统时怎么办】`start_cron_worker()` 的调用点在 `app/__init__.py`；
迁移时把它搬到新宿主即可，本包不需要动。搬完请同步本 docstring 与上面那个
测试里的消费方清单（测试会先红，不会静默）。

【已知历史包袱】`app/routes/cron.py` 直接借用了两个**私有名**
（`_execute_prompt_job` / `_execute_function_job`）。未在此次收口内改名，
已登记在测试的 `_EXPORTS` 表里；将来收敛为公开名时须两侧同步。
────────────────────────────────────────────────────────────
"""

from .cron_tools import create_cron_job, list_cron_jobs, remove_cron_job, toggle_cron_job
from .cron_worker import (
    start_cron_worker, stop_cron_worker,
    schedule_job_from_db, unschedule_job,
    get_scheduled_jobs, subscribe, unsubscribe,
)
