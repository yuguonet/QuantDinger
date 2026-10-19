# -*- coding: utf-8 -*-
"""scheduler.py — APScheduler 触发器封装（cron 重实现 2026-10-19）。

职责（薄封装，只管「怎么算触发」，不含调度策略/DB/执行）：
  - trigger_from_expr(): 5 段 cron（支持 `|` 多段）→ APScheduler Trigger
  - next_fire_time():    下一触发时刻（委托 trigger.get_next_fire_time）
  - make_scheduler():    BackgroundScheduler 工厂（统一默认值）

⚠ 最大陷阱（方案 §3 / 附录 A3-9）：**星期字段语义不同**——标准 cron 0=周日，
  APScheduler 3.x 的 day_of_week 0=周一（2026-10-19 实测：day_of_week=0 落在
  Monday，6 落在 Sunday）。故一律传**星期名字**（'sun'..'sat'）给 CronTrigger，
  绕开数字歧义；黄金用例（周日≠周一）钉在 tests/test_v38_cron_scheduler_20261019.py。

⚠ 语义约定：
  - next_fire_time() 严格晚于 `after`（与旧实现契约一致：`after` 恰为触发点时返回
    下一次，不返回自身）。
  - 表达式非法 / 任一字段空集 → 返回 None（**绝不假调度**——旧事故：非法表达式
    退化成每分钟跑一遍 agent）。
  - max_search_days 窗口内无匹配 → None（如 `0 0 29 2 *` 平年永不触发）。

用法:
    >>> t = trigger_from_expr("30 18 * * 1-5")      # 工作日 18:30
    >>> next_fire_time("0 9 * * 0|30 18 * * 1-5", after)   # 多段取最早
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from .guards import parse_cron_fields

logger = logging.getLogger(__name__)

#: 时区固定中国标准时间（与 cron_worker.TZ_CN 一致；服务器时区无关）
TZ_NAME = "Asia/Shanghai"

#: 标准 cron 星期数字（0=周日）→ APScheduler 星期名（避开其 0=周一 的数字歧义）
_WD_NAMES = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat"}

#: 补跑宽限全局默认（秒）；per-job misfire_grace_time 可覆盖（附录 A1-3）
DEFAULT_MISFIRE_GRACE = int(os.getenv("QD_CRON_MISFIRE_DEFAULT", "300"))


def _field_str(values, wildcard_when_full: bool = True, full_size: int = 0) -> str:
    """数字集合 → APScheduler 字段表达式串；满集合回退 '*'。"""
    vals = sorted(values)
    if wildcard_when_full and full_size and len(vals) == full_size:
        return "*"
    return ",".join(str(v) for v in vals)


def trigger_from_expr(cron_expr: str, timezone: str = TZ_NAME):
    """5 段 cron（可 `|` 多段）→ APScheduler Trigger；非法返回 None。

    多段 → OrTrigger（语义=任一段命中即触发；下一时刻自然取最早）。

    ⚠ 单段内日+周同时受限 = **交集(AND)**：APScheduler CronTrigger 的 day 与
      day_of_week 同时约束才触发。标准 Vixie cron 为并集(OR，任一命中即触发)；
      旧自研实现同为 AND，本实现保持一致（无回归）。如需 OR 语义须拆多段 `|`。
      钉板：tests/test_v38_cron_scheduler_20261019.py
            test_day_and_weekday_is_intersection_not_union
    """
    from zoneinfo import ZoneInfo

    sub_exprs = [e.strip() for e in (cron_expr or "").split("|") if e.strip()]
    if not sub_exprs:
        return None
    triggers = []
    for seg in sub_exprs:
        fields = parse_cron_fields(seg)     # (分,时,日,月,周) 已展开的数字集合
        if not fields or any(not f for f in fields):
            logger.error("[CronSched] cron 段无法解析/空集，拒绝调度: %r", seg)
            return None
        minutes, hours, days, months, weekdays = fields
        try:
            t = CronTrigger(
                minute=_field_str(minutes, full_size=60),
                hour=_field_str(hours, full_size=24),
                day=_field_str(days, full_size=31),
                month=_field_str(months, full_size=12),
                # ★ 星期：传名字（标准 cron 0=周日 ⇒ sun），绕开数字歧义
                day_of_week=",".join(_WD_NAMES[w] for w in sorted(weekdays)),
                timezone=ZoneInfo(timezone),
            )
        except Exception as e:               # APScheduler 对非法表达式抛 ValueError
            logger.error("[CronSched] CronTrigger 构造失败 %r: %s", seg, e)
            return None
        triggers.append(t)
    return OrTrigger(triggers) if len(triggers) > 1 else triggers[0]


def next_fire_time(cron_expr: str, after: datetime,
                   max_search_days: int = 62) -> Optional[datetime]:
    """`after` 之后的下一触发时刻；非法/窗口内无匹配返回 None。

    委托 trigger.get_next_fire_time（附录 A2-6：表达式是唯一真源，本函数是
    唯一推导入口；DB 的 next_run 列仅展示）。

    时间口径（附录 B3-13）：内部一律 aware；naive 的 `after` 按本模块时区补正。
    """
    from zoneinfo import ZoneInfo
    if after.tzinfo is None:            # naive/aware 混用防御：统一 aware
        after = after.replace(tzinfo=ZoneInfo(TZ_NAME))
    trigger = trigger_from_expr(cron_expr)
    if trigger is None:
        return None
    nxt = trigger.get_next_fire_time(None, after)
    if nxt is not None and nxt <= after:
        # 边界：after 恰为触发点 ⇒ 严格向后取下一次（旧实现契约）
        nxt = trigger.get_next_fire_time(nxt, after)
    if nxt is None:
        logger.error("[CronSched] cron 表达式 %r 无任何匹配，不调度", cron_expr)
        return None
    if (nxt - after).total_seconds() > max_search_days * 86400:
        logger.error("[CronSched] cron 表达式 %r 在 %d 天内无匹配，不调度",
                     cron_expr, max_search_days)
        return None
    return nxt


def make_scheduler() -> BackgroundScheduler:
    """统一默认值的调度器工厂（附录 §3）：

    - timezone=Asia/Shanghai（服务器时区无关）
    - coalesce=True：积压触发合并为一次
    - max_instances=1：同任务不并发（进程内；跨进程由 claim 互斥兜底）
    - misfire_grace_time：全局默认，per-job 可覆盖
    """
    return BackgroundScheduler(
        timezone=TZ_NAME,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": DEFAULT_MISFIRE_GRACE,
        },
    )
