# -*- coding: utf-8 -*-
"""exec.job_runner —— 子进程作业的统一执行器（F26-D1，2026-10-18）。

来源与边界（总账 F26 / 批4_剩余项实施总方案 §5）：
  · `kill_tree` 从 `chain/backtest_bridge.py` **原样移交**（该处 2026-10-15 建成
    终态，行为不回归；bridge 只留委托 seam，测试 monkeypatch `B._kill_tree` 的
    接缝不变）。skill_run 路径由此**首次获得**杀树能力——此前
    `subprocess.run(timeout=600)` 超时只杀直接子进程，孙进程残留。
  · 停滞看门狗是**新增**：作业目录有进度信号（`last_progress` 戳优先，否则目录内
    最新产物 mtime）且超过 `stall_s` 未更新 ⇒ 判死杀树抛 `JobStalled`。
    ⚠ 无进度信号时**明示退回墙钟**（logger.info 一句），不静默——"没有观测件"
    与"没有进展"是两件事，不许假装在看。
  · env `QD_JOB_STALL_SECONDS`：全局提前杀旋钮；**缺省沿用调用方 timeout_s**
    （= 改造前墙钟语义，不提前杀；要提前杀必须显式设，后果自负）。
    ⚠ 提前杀的前提是执行方**频繁产出**：目录内任何文件 mtime 都算信号（manifest
    也是），单产物长作业（技能流水线只在收尾写 result.json）若把 stall_s 设得比
    预期时长还小，会在产物落定前被误杀 —— 这类作业保持默认（stall=timeout）。
  · 墙钟 `timeout_s` 到 ⇒ 杀树抛 `TimeoutExpired`（尾部输出附在异常里，与 bridge 同口径）。

契约（调用方）：
  · 非 0 退出抛 `RuntimeError`（诚实失败，不吞退出码）；
  · 执行方产生进展时调 `touch_progress(job_dir)`（幂等、观测件写失败不判死）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: 模块级别名（测试 monkeypatch jr.Popen 的接缝；同 bridge B.Popen 惯例）
Popen = subprocess.Popen

#: 看门狗轮询粒度（秒）。测试可注入小值；生产 1s 足够（判死粒度不要求亚秒）。
POLL_S = 1.0

#: 进度戳文件名（作业目录内）
PROGRESS_FILE = "last_progress"


class JobStalled(RuntimeError):
    """作业停滞：有进度信号但超过 stall_s 未更新（已杀树）。"""


def kill_tree(proc) -> None:
    """杀整棵进程树。Windows: taskkill /T /F；posix: killpg（孙进程残留史见 bridge 模块头）。

    taskkill 失败（权限/竞态退出）必须回落 proc.kill()：后续还有 communicate()
    等管道收尾，进程若没死会挂到自然退出，超时语义被破坏。
    """
    if os.name == "nt":
        tk = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                            capture_output=True)
        if tk.returncode != 0:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _stall_default() -> Optional[int]:
    """env `QD_JOB_STALL_SECONDS`：正数生效；未设/0/负/非法一律 None（沿用调用方 timeout）。"""
    raw = os.getenv("QD_JOB_STALL_SECONDS", "") or ""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _progress_stamp(job_dir: Path) -> Optional[float]:
    """作业目录的进度信号时间戳：`last_progress` 优先，否则目录内最新文件 mtime。

    返回 None = 无任何进度信号（调用方据此明示退回墙钟）。
    """
    lp = job_dir / PROGRESS_FILE
    if lp.is_file():
        try:
            return lp.stat().st_mtime
        except OSError:
            return None
    stamps = []
    try:
        for p in job_dir.iterdir():
            try:
                if p.is_file():
                    stamps.append(p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return None
    return max(stamps) if stamps else None


def touch_progress(job_dir) -> None:
    """写/更新进度戳（执行方产生进展时调用；幂等）。

    观测件写失败**不判死**：墙钟仍在兜底，进度戳只是争取"提前判死"的观测源。
    """
    try:
        p = Path(job_dir) / PROGRESS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    except OSError:
        logger.warning("[job_runner] 进度戳写入失败（%s），看门狗将只有墙钟", job_dir)


def _kill_and_drain(proc, *, wait_s: float = 5.0) -> Tuple[str, str]:
    """杀树后收干管道（防 finally 里 communicate 挂住）；返回尾部输出。"""
    kill_tree(proc)
    try:
        out, err = proc.communicate(timeout=wait_s)
        return out or "", err or ""
    except Exception:
        return "", ""


def run_job(cmd, *, job_dir=None, timeout_s: int, stall_s: Optional[int] = None,
            env=None, cwd=None, poll_s: float = POLL_S,
            check_rc: bool = True) -> Tuple[str, str, int]:
    """子进程执行 → (stdout, stderr, returncode)。

    参数：
      cmd:        argv 列表
      job_dir:    作业目录（进度信号源；None = 纯墙钟，无看门狗）
      timeout_s:  墙钟超时（秒，必须为正）
      stall_s:    停滞判死阈值；None ⇒ env `QD_JOB_STALL_SECONDS`，再缺省 timeout_s
      env/cwd:    透传 Popen
      poll_s:     看门狗轮询粒度（测试注入小值）
      check_rc:    True（缺省）= 非 0 退出抛 RuntimeError（尾部输出在消息里）；
                   False = 不抛，把 rc 交还调用方——供"stdout 结构化错误通道"型
                   子进程用（如 skill_run：rc≠0 时 stdout 仍是给模型的 ok=False
                   JSON，含重试指引；父进程必须照常解析，见 run_skill）。

    异常：
      TimeoutExpired  墙钟到（已杀树，尾部输出在异常里）
      JobStalled      进度信号停滞超过 stall_s（已杀树）
      RuntimeError    非 0 退出且 check_rc=True（尾部输出在消息里）
    """
    if timeout_s <= 0:
        raise ValueError(f"timeout_s 必须为正: {timeout_s}")
    stall = stall_s if stall_s is not None else (_stall_default() or timeout_s)
    job = Path(job_dir) if job_dir else None
    posix_kwargs = {"start_new_session": True} if os.name != "nt" else {}
    proc = Popen(cmd, cwd=str(cwd) if cwd else None, env=env,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, encoding="utf-8", errors="replace",
                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                 **posix_kwargs)
    started = time.monotonic()
    fallback_logged = False
    killed = False
    out = err = ""
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                pass
            if job is not None:
                stamp = _progress_stamp(job)
                if stamp is None:
                    if not fallback_logged:
                        logger.info(
                            "[job_runner] %s 无进度信号，停滞看门狗退回墙钟 timeout_s=%ss",
                            job, timeout_s)
                        fallback_logged = True
                elif time.time() - stamp > stall:
                    tail_out, tail_err = _kill_and_drain(proc)
                    killed = True
                    raise JobStalled(
                        f"作业停滞：{job} 进度 {time.time() - stamp:.0f}s 未更新"
                        f"（stall_s={stall}s），已杀树；尾部输出: "
                        f"{(tail_err or tail_out or '')[-500:]}")
            if time.monotonic() - started > timeout_s:
                tail_out, tail_err = _kill_and_drain(proc)
                killed = True
                raise subprocess.TimeoutExpired(
                    cmd=cmd, timeout=timeout_s,
                    stderr=(tail_err or tail_out or "")[-2000:])
    except BaseException:
        # 仅防"循环体内未杀树就异常退出"（如 _progress_stamp 抛错）时的进程泄漏；
        # 停滞/墙钟两条路径上面已杀过（killed=True），不得重复杀。
        if not killed and proc.poll() is None:
            _kill_and_drain(proc)
        raise
    if check_rc and proc.returncode != 0:
        raise RuntimeError(f"子进程退出码 {proc.returncode}，尾部输出:\n"
                           f"{(err or out or '')[-2000:]}")
    return out, err, proc.returncode
