# -*- coding: utf-8 -*-
"""定时任务工具 —— Agent 会话入口（薄封装）。

────────────────────────────────────────────────────────────
★ 为什么本文件是"薄封装"而不是 re-export（2026-10-23）

实现在 `app/agent/cron/cron_tools.py`，但它在 `cron/` 包下，而工具扫描只覆盖
`tools/`（`_provider.get_or_build()` 的构建口径）⇒ 那些函数**从未进入工具面**，
LLM 在会话里根本看不见（实测：工具面 53 个工具，cron 相关 0 个）。

不能直接 `from ..cron.cron_tools import create_cron_job` 了事：
`_schema_gen._is_tool_function` 第 184 行要求 `obj.__module__ == module.__name__`，
re-export 的函数 `__module__` 仍是 `app.agent.cron.cron_tools` ⇒ 判据直接拒绝。
所以这里必须**定义同名函数**，内部委托给 cron 包 —— 顺带让 docstring 可以
针对 LLM 重写（原实现面向程序调用，缺少"什么时候该用"的指引）。

依赖方向：`app.agent.tools` → `app.agent.cron`（外部消费方 → cron，符合
`cron/__init__.py` 记录的单向契约）。实现内部延迟导入，避免工具扫描期
触发 cron_worker 加载。

★ 域归属（2026-10-23 用户裁定）：本文件在 `tools/schedule/` 子目录下 ⇒
domain = schedule（子目录名即域，`_provider.scan_subdirectories` 登记），
**不在必注入层**。诉求：5 个窄场景工具每轮白占 ~3.8k 字符 schema，改走按需
预选（域词典见 `preselect/tool_preselect.py::DATA_DOMAINS` 的 schedule 条目）。
────────────────────────────────────────────────────────────

用户典型说法 → 本文件的调用：
  "每5分钟分析一次300129"        → create_cron_job(at="开盘期间,每5分钟")
  "明天9:25提醒我看牙医"          → create_cron_job(at="tomorrow 09:25")
  "开盘后监视茅台，到1650提醒我"  → create_cron_job(at="开盘期间,每5分钟", prompt="...")
  "我有哪些定时任务"              → list_cron_jobs()
  "把那个监视任务停掉"            → toggle_cron_job(job_id, enabled=False)
"""
from __future__ import annotations

from typing import Any, Dict


def _impl():
    """延迟导入 cron 实现（工具扫描期不触发 cron_worker 加载）。

    ⚠ 相对导入层级：本文件在 `tools/schedule/` 子目录（比原来深一层），
    `..cron` 会解析到 `app.agent.tools.cron`（不存在）⇒ 必须三个点。
    """
    from ...cron import cron_tools
    return cron_tools


def create_cron_job(
    name: str,
    prompt: str,
    cron_expr: str = "",
    at: str = "",
    one_shot: bool = False,
    description: str = "",
) -> Dict[str, Any]:
    """创建定时任务：到点自动执行一段指令（可用于定时分析、盘中监视、到期提醒）。

    当用户要求「每天/每周/明天X点/盘中每N分钟」做任何事时，用这个工具把要求
    落成任务。任务到点后会把 `prompt` 当作一条新消息交给 Agent 执行，执行结果
    通过 SSE 推到当前会话，并记入 `qd_agent_cron_runs` 表。

    触发方式二选一（优先用 `at`，自然语言不用手算 cron）：
      - at（推荐）："14:45" / "tomorrow 09:25" / "2026-10-24 09:25" /
        "每天 9:00" / "每周一 9:00" / "每月1号 9:00" / "开盘期间,每5分钟"
      - cron_expr：5 段式，多段用 | 分隔（如 "*/5 10 * * 1-5|*/5 13-14 * * 1-5"）

    Args:
        name: 任务名，简短（如"监视600519"）
        prompt: 到点时执行的内容，写成一句完整指令（如"检查600519现价，若≤1650
            则提醒我可以买入"）。写清标的、条件、要做什么，别只写"检查一下"
        cron_expr: 5 段式 cron 表达式，与 at 二选一
        at: 自然语言时间，与 cron_expr 二选一
        one_shot: 是否只执行一次（at 指定具体时刻时自动为 True）
        description: 任务详细说明（可选）

    Returns:
        {"job_id": int, "name": str, "cron_expr": str, "next_run": str,
         "one_shot": bool, "status": "created"}；失败时 {"error": str}
    """
    return _impl().create_cron_job(
        name=name, prompt=prompt, cron_expr=cron_expr, at=at,
        one_shot=one_shot, description=description,
    )


def create_alert_job(
    name: str,
    symbol: str,
    field: str,
    op: str,
    value: float,
    at: str = "",
    cron_expr: str = "",
    one_shot: bool = True,
    message: str = "",
    channels: str = "browser",
) -> Dict[str, Any]:
    """创建**条件告警**：按节拍检查某个参数，达到阈值才提醒（不达不提醒、不跑分析）。

    当用户说"到X提醒我""跌破X告诉我""涨到X通知我""盘中监视XX，符合条件叫我"
    时，用这个工具 —— 不要用 create_cron_job（那是到点必跑一次分析，不管条件）。

    可用参数（field）—— 目前开放实时行情快照字段：
      last          现价
      open          今开
      high          最高
      low           最低
      previousClose 昨收
      changePercent 涨跌幅(%)
      volume        当日累计成交量(股)
    技术指标（MA/RSI/MACD 等）暂不支持——它们需现场计算K线，成本高且口径易漂移。

    Args:
        name: 告警名，简短（如"茅台跌到1650"）
        symbol: 标的代码（如 "600519"）
        field: 要监测的参数，见上面的可用参数清单
        op: 比较符：> >= < <= == !=
        value: 阈值（数字）
        at: 检查节拍，同 create_cron_job（推荐"开盘期间,每5分钟"）
        cron_expr: 5 段式 cron，与 at 二选一
        one_shot: 命中一次后是否结束（默认 True，即"响一次就停"）
        message: 命中时附带的说明（可选）
        channels: 推送通道，逗号分隔（browser/telegram/email/webhook/discord）

    Returns:
        {"job_id": int, "name": str, "mode": "alert", "next_run": str,
         "one_shot": bool, "condition": {...}, "status": "created"}；
        失败时 {"error": str}
    """
    return _impl().create_alert_job(
        name=name, symbol=symbol, field=field, op=op, value=value,
        at=at, cron_expr=cron_expr, one_shot=one_shot,
        message=message, channels=channels,
    )


def list_cron_jobs(enabled_only: bool = False) -> Dict[str, Any]:
    """列出当前所有定时任务（含下次执行时间、上次运行状态、累计执行次数、错误）。

    用户问"我有哪些定时任务""那个任务还在跑吗"时用这个。

    Args:
        enabled_only: 是否只看启用中的任务

    Returns:
        {"jobs": [...], "total": int, "enabled": int}；失败时 {"error": str}
    """
    return _impl().list_cron_jobs(enabled_only=enabled_only)


def remove_cron_job(job_id: int) -> Dict[str, Any]:
    """彻底删除一个定时任务。

    比"禁用"更彻底：任务记录一并消失，不可恢复。用户明确说"删掉/移除"时用它；
    只是暂时不想跑，用 toggle_cron_job 禁用即可。

    Args:
        job_id: 任务 ID（从 list_cron_jobs 获取）

    Returns:
        {"status": "deleted", "job_id": int, "name": str}；失败时 {"error": str}
    """
    return _impl().remove_cron_job(job_id=job_id)


def toggle_cron_job(job_id: int, enabled: bool) -> Dict[str, Any]:
    """启用或禁用一个定时任务（任务记录保留，可再启用）。

    Args:
        job_id: 任务 ID（从 list_cron_jobs 获取）
        enabled: True 启用，False 禁用

    Returns:
        {"status": "toggled", "job_id": int, "name": str, "enabled": bool}；
        失败时 {"error": str}
    """
    return _impl().toggle_cron_job(job_id=job_id, enabled=enabled)
