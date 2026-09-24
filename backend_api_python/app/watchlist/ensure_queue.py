# -*- coding: utf-8 -*-
"""app/watchlist/ensure_queue.py — 自选股 system 标签**后台补算队列**

场景：批量入自选（一次最多 500）不能在 HTTP 里同步算 label（取 K+筹码秒级/票），
也不能干等盘后 job。做法：**入队后立刻返回**，后台线程合并批次调用
`write_system_facts(pairs=...)` 全量补算。

- 进程内队列（单 worker）：合并去重，失败仅告警
- 与盘后 job 同一写路径（grade=1），无第二条写通道
- 失败不阻断添加；空白票由盘后 job / 下次入队兜住

纪律：本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Iterable, List, Optional, Sequence, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)

Pair = Tuple[str, str]

_q: "queue.Queue[Optional[Pair]]" = queue.Queue()
_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
#: 合并窗口（秒）：短时间大批量插入攒一拨再算
_BATCH_WINDOW_S = 0.8
#: 单批上限（K 线/筹码串行，500 票可接受；再大则分多批）
_MAX_BATCH = 80


def enqueue_ensure(pairs: Iterable[Tuple[str, str]]) -> int:
    """把 (market, symbol) 推入后台补算队列。返回实际入队条数。"""
    n = 0
    for market, symbol in pairs:
        if market and symbol:
            _q.put((market, symbol))
            n += 1
    if n:
        _ensure_worker()
    return n


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="label-ensure-queue", daemon=True)
        _worker.start()


def _drain(max_n: int) -> List[Pair]:
    """在短窗口内攒批，按 (market,symbol) 去重。"""
    seen = set()
    out: List[Pair] = []
    deadline = time.time() + _BATCH_WINDOW_S
    while len(out) < max_n:
        timeout = max(0.0, deadline - time.time())
        try:
            item = _q.get(timeout=timeout if timeout > 0 else 0.05)
        except queue.Empty:
            break
        if item is None:
            break
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _loop() -> None:
    while True:
        batch = _drain(_MAX_BATCH)
        if not batch:
            # 队列空 → 休眠等下一次 put（用哨兵外的短 poll 保持简单）
            try:
                item = _q.get(timeout=30.0)
            except queue.Empty:
                continue
            if item is None:
                return
            batch = [item]
            batch.extend(_drain(_MAX_BATCH - 1))
        try:
            from app.watchlist.api import write_system_facts
            stats = write_system_facts(pairs=batch)
            logger.info("[label.queue] 后台补算完成: %s", stats)
        except Exception as e:
            logger.warning("[label.queue] 后台补算失败 n=%d: %s", len(batch), e)


def shutdown(timeout: float = 1.0) -> None:
    """进程退出前可调用；daemon 线程通常无需显式停。"""
    _q.put(None)
    t = _worker
    if t is not None:
        t.join(timeout=timeout)
