# -*- coding: utf-8 -*-
"""dispatch — 三模式分发（**唯一实现**，五族共用）。

【为什么单独一个文件】
  方案原稿要求"每个协调器文件各自实现顺序/race/分页"。那等于把同一套语义写五遍：
  超时分配、单源短路、空信封判定、NotSupported 跳过、失败计数 —— 五份拷贝必然漂移
  （本仓 171 处 except-pass 就是这么长出来的）。用户裁定（2026-10-26）：允许一个
  dispatch.py。于是调度语义只有这一份，五个 `coord_*.py` 只声明"哪个 kind 用哪个模式"。

【三条语义（方案 §7）】
  1. **空信封 `{"rows": []}` 是成功** —— 当天确实没有榜单，不是取数失败。
  2. **NotSupported 在 validator 之前判掉** —— 不计失败、不换源代价、不打熔断。
  3. **单源短路** —— 候选只有 1 个时不进候选循环、不 race，直接一次调用。

【失败语义】
  provider 抛异常 / 信封不合协议 ⇒ 记该源一次失败，换下一个；全部失败才返回 ok=False。
  失败不会被伪装成空数据（这是与原 `dragon_limit._ak_*` 的关键差别）。
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_TIMEOUT = 8.0

#: 共享执行池：所有族共用，不为每个 kind 建池（避免了 N 个池互相争抢）。
_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="qd-dispatch"
)
_SLOTS = threading.BoundedSemaphore(256)


class NotSupported:
    """provider 表示"这个 kind 我不支持"的一等返回值（不是异常）。"""

    __slots__ = ("kind",)

    def __init__(self, kind: str = ""):
        self.kind = kind

    def __bool__(self) -> bool:  # 与 legacy NotSupportedResult 行为一致
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return "NotSupported(%s)" % self.kind


@dataclass
class Outcome:
    ok: bool
    data: Any = None
    source: str = ""
    error: str = ""
    mode: str = ""
    attempts: int = 0
    skipped: int = 0


# ── 信封校验器（族文件可覆写）─────────────────────────────────────

def is_rows(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("rows"), list)


def is_object(v: Any) -> bool:
    """object 型信封：必须带 "object" 键；值可为 dict（有数据）或 None（确实无盘口）。

    允许 None 是因为 tape 的两个上游都可能合理地拿不到盘口
    （非交易时段 / 停牌），那不是取数失败。
    """
    return isinstance(v, dict) and "object" in v and (
        v["object"] is None or isinstance(v["object"], dict))


# ── 单次调用 ────────────────────────────────────────────────────

def _call_once(provider: Any, kind: str, params: dict,
               validate: Callable[[Any], bool], budget: float):
    """返回 (data, error, skipped)。data 非 None 即成功。"""
    if not _SLOTS.acquire(timeout=max(0.0, min(budget, 1.0))):
        return None, "调度槽位耗尽", False
    try:
        future = _POOL.submit(provider.fetch, kind, **params)
    except Exception as e:  # pragma: no cover - submit 极少失败
        _SLOTS.release()
        return None, "%s: %s" % (type(e).__name__, e), False
    future.add_done_callback(lambda _: _SLOTS.release())

    try:
        res = future.result(timeout=max(0.05, budget))
    except concurrent.futures.TimeoutError:
        return None, "超时(%.1fs)" % budget, False
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e), False

    if res is None or isinstance(res, NotSupported) or res is False:
        return None, "", True                      # 跳过，不算失败
    if not validate(res):
        return None, "信封不合协议: %r" % (type(res).__name__), False
    return res, "", False


# ── 三模式 ──────────────────────────────────────────────────────

def _sequential(cands, kind, params, validate, timeout) -> Outcome:
    deadline = time.monotonic() + timeout
    errors, attempts, skipped = [], 0, 0

    for p in cands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("预算耗尽")
            break
        attempts += 1
        # 单源短路：只有 1 个候选时把整段预算给它，不做递减分配
        budget = remaining if len(cands) == 1 else min(remaining, timeout / max(1, len(cands)))
        data, err, skip = _call_once(p, kind, params, validate, budget)
        if data is not None:
            return Outcome(True, data, p.name, mode="sequential",
                           attempts=attempts, skipped=skipped)
        if skip:
            skipped += 1
            continue
        errors.append("%s: %s" % (p.name, err))

    return Outcome(False, None, "", "; ".join(errors) or "无可用源",
                   mode="sequential", attempts=attempts, skipped=skipped)


def _race(cands, kind, params, validate, timeout) -> Outcome:
    deadline = time.monotonic() + timeout
    if len(cands) == 1:                            # 单源短路：不 race
        return _sequential(cands, kind, params, validate, timeout)

    futs = {}
    errors, skipped = [], 0
    for p in cands:
        if not _SLOTS.acquire(timeout=0.2):
            errors.append("%s: 调度槽位耗尽" % p.name)
            continue
        try:
            f = _POOL.submit(p.fetch, kind, **params)
        except Exception as e:
            _SLOTS.release()
            errors.append("%s: %s" % (p.name, e))
            continue
        f.add_done_callback(lambda _: _SLOTS.release())
        futs[f] = p

    try:
        for f in concurrent.futures.as_completed(list(futs), timeout=max(0.05, timeout)):
            p = futs[f]
            try:
                res = f.result()
            except Exception as e:
                errors.append("%s: %s" % (p.name, e))
                continue
            if res is None or isinstance(res, NotSupported) or res is False:
                skipped += 1
                continue
            if not validate(res):
                errors.append("%s: 信封不合协议" % p.name)
                continue
            return Outcome(True, res, p.name, mode="race",
                           attempts=len(futs), skipped=skipped)
    except concurrent.futures.TimeoutError:
        errors.append("超时(%.1fs)" % timeout)
    finally:
        for f in futs:
            f.cancel()

    return Outcome(False, None, "", "; ".join(errors) or "无可用源",
                   mode="race", attempts=len(futs), skipped=skipped)


def _paged(cands, kind, params, validate, timeout, page_size, max_pages) -> Outcome:
    """同源翻页合并：**不跨源拼页**（不同源的页码语义不同）。"""
    deadline = time.monotonic() + timeout
    for p in cands:
        rows: List[dict] = []
        ok_any = False
        for page in range(int(max_pages)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            page_params = dict(params, page=page, page_size=page_size)
            data, _err, skip = _call_once(p, kind, page_params, validate, remaining)
            if data is None:
                if skip:
                    break
                rows = []
                break
            ok_any = True
            got = list(data.get("rows") or [])
            rows.extend(got)
            if len(got) < int(page_size):
                break
        if ok_any:
            return Outcome(True, {"rows": rows}, p.name, mode="paged", attempts=1)
    return Outcome(False, None, "", "无可用源", mode="paged")


def run(mode: str, candidates: Sequence[Any], kind: str, params: Dict[str, Any],
        validate: Callable[[Any], bool], timeout: float = DEFAULT_TIMEOUT,
        page_size: int = 0, max_pages: int = 1) -> Outcome:
    """统一入口。`mode ∈ {sequential, race, paged}`。"""
    cands = list(candidates or ())
    if not cands:
        return Outcome(False, None, "", "本族无可用源", mode=mode)
    if mode == "sequential":
        return _sequential(cands, kind, params, validate, timeout)
    if mode == "race":
        return _race(cands, kind, params, validate, timeout)
    if mode == "paged":
        return _paged(cands, kind, params, validate, timeout, page_size, max_pages)
    raise ValueError("未知调度模式: %r" % mode)
