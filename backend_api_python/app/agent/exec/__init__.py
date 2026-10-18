# -*- coding: utf-8 -*-
"""exec —— 子进程作业执行件（F26-D1，2026-10-18）。

单一事实源在 `job_runner`：杀树 / 墙钟超时 / 停滞看门狗 / 作业目录进度戳。
bridge 的 `_kill_tree` 与 skill_run 路径都经此出口（不各写一份）。
"""

from .job_runner import JobStalled, kill_tree, run_job, touch_progress

__all__ = ["JobStalled", "kill_tree", "run_job", "touch_progress"]
