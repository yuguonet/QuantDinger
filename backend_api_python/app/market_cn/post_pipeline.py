"""盘后串行流水线（任务二，2026-11-20 用户裁定，方案见 docs/盘后调度重构方案_任务一循环任务二流水线_20261120.md）。

一条到点启动、串行、跑完退出的流水线，取代原「4 个各自为政任务 + daily_scan 60s 轮询等事件」：
  ① 1m+1d 收敛 → ② 中段（指数5m/资金流/日档/北向）→ ③ fund_flow → ④ 龙虎榜（只拉一次）
  → ⑤ 盘后整理（板块/watchlabel）→ ⑥ 发两条非阻塞指令

关键设计（易误解点）：
- 每步是独立接口 PipelineStep(name, run, not_before)；编排器统一管等待/完成标记/异常/日志，步骤只管业务。
- 重启续跑：每步完成标记落 qd_scheduler_done（前缀 post_pipeline@），重启后已完成跳过、未完成从断点继续；
  target 用 last_finish_trading_day()，15:00 后返回当日 ⇒ 19:00 重启 target 不漂移。
- not_before：步骤最早启动时刻，前序提前完成也必须等到点；用一次 sleep 到点（非 60s 轮询），零 DB 查询。
  龙虎榜 not_before=17:00，取消 3 次重试后这是「唯一拉取机会落在发布后」的保证。
- 每步必须幂等：半途崩溃重启后重跑该步不产生重复写入（复用底层均为 upsert/缺口回填）。
- ⑥ 发两条 fire-and-forget 指令（daily_scan / eval），各自一次性线程跑完退出；失败时
  延时重试 QD_PIPELINE_{SCAN,EVAL}_RETRY 次（默认 2，间隔 600s）后放弃——有限次数、
  跑完退出，非 60s 常驻轮询；进程崩溃丢指令由 scheduler 启动补扫 / agent catchup 兜底。

本模块被 scheduler.py 延迟 import（避免循环依赖：scheduler 调本模块，本模块调 scheduler 的公共函数）。
"""
from __future__ import annotations

import os
import time as _time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional


def _get_logger():
    from app.market_cn.scheduler import logger
    return logger


_LHB_NOT_BEFORE_DEFAULT = "17:00"


def _parse_hm(raw: str, default: str) -> tuple[int, int]:
    """'HH:MM' → (h, m)；非法回退 default 并打 warning。"""
    logger = _get_logger()
    try:
        h, m = (int(x) for x in str(raw).strip().split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, TypeError):
        pass
    logger.warning("[pipeline] not_before=%r 非法，回退 %s", raw, default)
    h, m = (int(x) for x in default.split(":"))
    return h, m


def lhb_not_before() -> tuple[int, int]:
    """龙虎榜步骤最早启动时刻 (QD_PIPELINE_LHB_NOT_BEFORE, 默认 17:00)。"""
    return _parse_hm(os.getenv("QD_PIPELINE_LHB_NOT_BEFORE", _LHB_NOT_BEFORE_DEFAULT),
                     _LHB_NOT_BEFORE_DEFAULT)


@dataclass
class PipelineStep:
    name: str
    run: Callable[[str], dict]
    not_before: Optional[tuple[int, int]] = None


def _step_kline(target: str) -> dict:
    from app.market_cn.scheduler import _run_kline_settlement
    ok = _run_kline_settlement(target)
    return {"status": "ok" if ok else "error", "kline_complete": ok}


def _step_mid(target: str) -> dict:
    from app.market_cn.scheduler import (
        _sync_index_minute, _sync_index_fflow, _refresh_daily,
        _refresh_post_market, _mark_event,
    )
    _sync_index_minute()
    _sync_index_fflow()
    _mark_event("index_fflow", target)
    _refresh_daily()
    _refresh_post_market()
    # 北向/龙虎榜到位检测沿用 _post_market_batch 的既有逻辑（见 4.1②），
    # P0 只做编排搬迁，检测口径不改；未到位记 warning 不阻塞（后续步骤不依赖）。
    _check_mid_data(target)
    return {"status": "ok"}


def _check_mid_data(target: str):
    """中段数据到位检测（从 _post_market_batch 内联段等价抽出）。

    北向资金自 2024-08 起停止披露（index.py:265），末条恒为 2024-08-16，
    以 nb_date >= target 作就绪判据永远为假 ⇒ northbound event 永不 mark。
    停更即视为结构性到位：不再查北向，直接 mark；龙虎榜 dt 仍作观测。
    """
    logger = _get_logger()
    dt_date = ""
    try:
        from app.market_cn.dragon_limit import get_dragon_tiger
        dt = get_dragon_tiger()
        if dt and isinstance(dt, list) and dt[0]:
            dt_date = dt[0].get("date", "") if isinstance(dt[0], dict) else ""
    except Exception:
        pass
    from app.market_cn.scheduler import _mark_event
    _mark_event("northbound", target)
    if dt_date >= target:
        logger.info("[pipeline] 中段数据到位 (dt=%s, 北向=停披露视为到位, 目标=%s)",
                    dt_date, target)
    else:
        logger.warning("[pipeline] 龙虎榜未到 (dt=%s, 北向=停披露视为到位, 目标≥%s)，"
                       "不重试 (由下一日幂等补)", dt_date, target)


def _step_fundflow(target: str) -> dict:
    from app.market_cn.scheduler import _save_fund_flow_daily
    _save_fund_flow_daily()
    return {"status": "ok"}


def _step_lhb(target: str) -> dict:
    from app.market_cn.scheduler import _save_dragon_hot_daily
    _save_dragon_hot_daily(retries=1)
    # 未拉到已在 _save_dragon_hot_daily 打 warning 报告，本步仍返回 ok 不阻塞后续。
    # ⑥ 的一次性 scan worker 直接调 run_scan（内部 wait_data 只等 1D bar），不查 lhb 事件门；
    # 策略按 hub 纪律消费 D-1 榜，故拉空时扫描照跑、以报告记载缺失，不影响后续步骤。
    return {"status": "ok"}


def _step_sector(target: str) -> dict:
    from app.market_cn.scheduler import _refresh_sector_daily
    _refresh_sector_daily()
    try:
        from app.watchlist.job import run_daily as _label_run_daily
        _stats = _label_run_daily()
        _get_logger().info("[pipeline] 标签刷新: %s", _stats)
    except Exception:
        _get_logger().exception("[pipeline] 标签刷新失败 (不影响流水线)")
    return {"status": "ok"}


def _dispatch_scan_enabled() -> bool:
    """日线扫描指令子开关（QD_PIPELINE_DISPATCH_SCAN，默认 1）。单独关可退回旧触发。"""
    return (os.getenv("QD_PIPELINE_DISPATCH_SCAN", "1") or "1").strip() != "0"


def _dispatch_eval_enabled() -> bool:
    """评估指令子开关（QD_PIPELINE_DISPATCH_EVAL，默认 1）。"""
    return (os.getenv("QD_PIPELINE_DISPATCH_EVAL", "1") or "1").strip() != "0"


_SCAN_RETRY_DEFAULT = 2
_SCAN_RETRY_DELAY_DEFAULT = 600


def _scan_retry_config() -> tuple[int, int]:
    """scan 派发失败的运行时重试配置 (QD_PIPELINE_SCAN_RETRY / _DELAY_SEC)。

    次数 = 首次之外的额外重试（0 = 不重试）；延时 = 每次重试前的等待秒数。
    上限防误配（次数 <=10、延时 <=3600s）；非法或越界回退默认并打 warning。
    """
    logger = _get_logger()

    def _env_int(name: str, default: int, lo: int, hi: int) -> int:
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return default
        try:
            val = int(str(raw).strip())
        except (ValueError, TypeError):
            logger.warning("[pipeline] %s=%r 非法, 回退 %d", name, raw, default)
            return default
        if not lo <= val <= hi:
            logger.warning("[pipeline] %s=%d 越界[%d,%d], 回退 %d", name, val, lo, hi, default)
            return default
        return val

    return (_env_int("QD_PIPELINE_SCAN_RETRY", _SCAN_RETRY_DEFAULT, 0, 10),
            _env_int("QD_PIPELINE_SCAN_RETRY_DELAY_SEC", _SCAN_RETRY_DELAY_DEFAULT, 0, 3600))


def _run_scan_with_retry(keys, target: str, sleep_fn=None) -> dict:
    """一次性 scan 的执行体：run_scan 判失败时延时重试至上限, 跑完退出。

    成功判据 = run_scan 返回 status=="ok"（成功路径内已写 daily_scan@ 标记）。
    失败态 busy/data_not_ready/异常均触发重试；有限次数、非 60s 常驻轮询。
    run_scan 的跨进程锁 + 幂等完成标记保证重试安全。sleep_fn 仅供验证注入。
    """
    from app.market_cn.auto.scan import run_scan
    logger = _get_logger()
    retries, delay = _scan_retry_config()
    sleep = sleep_fn or _time.sleep
    last = None
    for attempt in range(1, retries + 2):
        try:
            last = run_scan(keys=tuple(keys), target=target)
            status = last.get("status") if isinstance(last, dict) else last
            if status == "ok":
                if attempt > 1:
                    logger.info("[pipeline] scan 第 %d 次重试成功: keys=%s target=%s", attempt, keys, target)
                else:
                    logger.info("[pipeline] scan worker 完成: keys=%s target=%s", keys, target)
                return last
            logger.warning("[pipeline] scan 第 %d/%d 次未成功(status=%s): keys=%s target=%s",
                           attempt, retries + 1, status, keys, target)
        except Exception:
            logger.exception("[pipeline] scan 第 %d/%d 次执行异常: keys=%s target=%s",
                             attempt, retries + 1, keys, target)
        if attempt <= retries:
            logger.info("[pipeline] scan %.0fs 后重试 (第 %d/%d 次)", delay, attempt, retries)
            sleep(delay)
    logger.error("[pipeline] scan 重试 %d 次后仍未成功: keys=%s target=%s → %s",
                 retries, keys, target, last)
    return last if isinstance(last, dict) else {"status": "error"}


def dispatch_daily_scan(target: str = None) -> dict:
    """发一条非阻塞日线扫描指令（P1）：一次性线程跑全量 enabled keys，跑完退出。

    取代已删除的 daily_scan 60s 轮询。一次触发覆盖原轮询全部语义：
    - keys = 全部 enabled daily_close 策略（4 个依赖完全相同，均 daily_1d+lhb）；
    - 完成标记沿用 auto/scan.py::_mark_daily_scanned（run_scan 成功路径内部唯一写入点）；
    - run_scan 内部 wait_data 只等 1D bar，数据齐即可跑；流水线跑到⑥时 1d 已在① mark。
    可靠性：指令若因进程崩溃丢失（流水线 done 但 scan 未完成），由 scheduler 启动补扫兜底
    （catchup_dispatch_scan_if_needed，判据只用 qd_scheduler_done，不查事件表）。
    """
    import threading
    from app.market_cn.auto.sched import enabled_daily_keys
    from app.utils.trading_calendar import last_finish_trading_day
    from app.market_cn.scheduler import scheduler_done_names

    logger = _get_logger()
    target = target or last_finish_trading_day()
    keys = list(enabled_daily_keys())
    if not keys:
        logger.info("[pipeline] daily_scan 指令跳过: 无 enabled 日线策略")
        return {"status": "no_active_strategy", "target": target}
    done = set(scheduler_done_names("daily_scan@", target))
    todo = [k for k in keys if f"daily_scan@{k}" not in done]
    if not todo:
        logger.info("[pipeline] daily_scan 指令跳过: 全部已标完成 target=%s", target)
        return {"status": "all_done", "target": target}

    def _run():
        _run_scan_with_retry(todo, target)

    threading.Thread(target=_run, daemon=True, name="dispatch-daily-scan").start()
    logger.info("[pipeline] daily_scan 指令已发出 (fire-and-forget): keys=%s target=%s", todo, target)
    return {"status": "dispatched", "target": target, "keys": todo}


def catchup_dispatch_scan_if_needed() -> dict:
    """启动补扫兜底（仅开关=1 时由 scheduler 调用）：流水线整条完成但当日 scan 未完成 ⇒ 补发一次。

    判据只用 qd_scheduler_done：post_pipeline 总标记已完成（流水线走完⑥）+ 有 enabled key
    未标 daily_scan@key。不用事件表、不查 lhb，避免与正在跑的流水线抢触发。
    """
    import threading
    from app.market_cn.auto.sched import enabled_daily_keys
    from app.utils.trading_calendar import last_finish_trading_day, is_trading_day
    from app.market_cn.scheduler import scheduler_done_names, scheduler_task_done

    logger = _get_logger()
    today = last_finish_trading_day()
    if not is_trading_day(today):
        return {"status": "skip", "reason": "non_trading_day"}
    if not scheduler_task_done("post_pipeline", today):
        return {"status": "skip", "reason": "pipeline_not_done"}
    keys = list(enabled_daily_keys())
    done = set(scheduler_done_names("daily_scan@", today))
    todo = [k for k in keys if f"daily_scan@{k}" not in done]
    if not todo:
        return {"status": "skip", "reason": "all_done"}
    logger.warning("[pipeline] 启动补扫: 流水线已完成但 scan 未标全，补发一次 keys=%s", todo)

    def _run():
        _run_scan_with_retry(todo, today)

    threading.Thread(target=_run, daemon=True, name="catchup-daily-scan").start()
    return {"status": "dispatched", "keys": todo}


def _step_dispatch(target: str) -> dict:
    """⑥ 发两条非阻塞指令（P1 scan / P2 eval），各自 fire-and-forget、跑完退出。

    子开关可单独关闭某条指令退回旧触发（scan 轮询 / eval 常驻，仅总开关=0 时有旧触发）。
    单条派发异常记录后不影响另一条，⑥ 整体仍标完成（派发即职责完成，worker 内部兜异常）。
    """
    logger = _get_logger()
    out = {"target": target}
    if _dispatch_scan_enabled():
        try:
            out["scan"] = dispatch_daily_scan(target)
        except Exception:
            logger.exception("[pipeline] ⑥ daily_scan 指令派发失败")
            out["scan"] = {"status": "error"}
    else:
        out["scan"] = {"status": "disabled"}
    if _dispatch_eval_enabled():
        try:
            from app.agent.chain.evaluator import dispatch_eval_once
            out["eval"] = dispatch_eval_once()
        except Exception:
            logger.exception("[pipeline] ⑥ eval 指令派发失败")
            out["eval"] = {"status": "error"}
    else:
        out["eval"] = {"status": "disabled"}
    logger.info("[pipeline] ⑥ 指令派发完成: %s", out)
    return {"status": "ok", **out}


def build_steps() -> list[PipelineStep]:
    """构造六步（工厂函数，便于测试注入替身）。"""
    return [
        PipelineStep("kline", _step_kline),
        PipelineStep("mid", _step_mid),
        PipelineStep("fundflow", _step_fundflow),
        PipelineStep("lhb", _step_lhb, not_before=lhb_not_before()),
        PipelineStep("sector", _step_sector),
        PipelineStep("dispatch", _step_dispatch),
    ]


def _wait_not_before(not_before: tuple[int, int], now: datetime) -> float:
    """返回需等待秒数（>=0）：未到本步时刻则等到点，已过则 0。"""
    h, m = not_before
    due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return max(0.0, (due - now).total_seconds())


def run_post_pipeline(steps: Optional[list[PipelineStep]] = None) -> dict:
    """编排六步：完成标记跳过 → not_before 等待 → 执行 → 成功写标记。

    异常策略：单步抛异常记 error 并继续后续步骤（不让一个坏步吞掉整条链）；
    但若 K线未收敛（① status=error），后续步骤照跑（幂等、数据到位检测会记 warning），
    整体返回 error ⇒ scheduler._worker 不写「post_pipeline」总完成标记 ⇒ 重启补跑兜底。
    分步完成标记按 target 记：重启后 run_post_pipeline 重新进入，已标步骤跳过（断点续跑）。
    """
    logger = _get_logger()
    from app.utils.trading_calendar import last_finish_trading_day
    from app.market_cn.scheduler import (
        scheduler_done_names, mark_scheduler_task_done,
    )

    target = last_finish_trading_day()
    steps = steps if steps is not None else build_steps()
    done = scheduler_done_names("post_pipeline@", target)
    logger.info("[pipeline] 启动 target=%s 已完成步骤=%s", target, sorted(done))

    failed = []
    for step in steps:
        tag = f"post_pipeline@{step.name}"
        if tag in done:
            logger.info("[pipeline] 跳过已完成步骤: %s", tag)
            continue
        if step.not_before:
            wait = _wait_not_before(step.not_before, datetime.now())
            if wait > 0:
                logger.info("[pipeline] %s not_before=%02d:%02d, 等待 %.0fs 到点",
                            tag, step.not_before[0], step.not_before[1], wait)
                _time.sleep(wait)
        logger.info("[pipeline] → 步骤 %s", tag)
        try:
            outcome = step.run(target)
            status = outcome.get("status") if isinstance(outcome, dict) else None
        except Exception as e:
            status = "error"
            outcome = {"status": "error", "error": str(e)}
            logger.exception("[pipeline] 步骤 %s 执行异常 (记录后继续后续步骤)", tag)
        if status in (None, "ok", "no_active_strategy"):
            mark_scheduler_task_done(tag, target)
            logger.info("[pipeline] ✓ 步骤完成 %s → %s", tag, outcome)
        else:
            failed.append(step.name)
            logger.error("[pipeline] ✗ 步骤未完成 %s → %s (不标完成, 重启续跑)", tag, outcome)

    result = {"status": "error" if failed else "ok", "target": target, "failed": failed}
    logger.info("[pipeline] 结束 %s", result)
    return result
