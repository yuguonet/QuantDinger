"""core/replay/mkt_slots.py — 市场门**逐槽 as-of** 取数（单一职责，2026-10-09）。

为什么单独一件（`docs/市场门口径评估_20261009.md`）:
  盘中市场门原吃 `replay.market_gain` 的**日线 close 横截面**（日频、全天恒定）⇒
  用「15:00 收盘」去门控一个 **14:56** 的入场 = 单向**前视**。修复 = 市场门读
  **决策槽当时**的横截面（口径同生产 `scan._mkt_gain(snaps)`）。

  与 `replay.market_gain` / `load_market_gain`（日频 close，现降级为**回退**）并列；
  本模块只做「窗口 → {date: {HH:MM: 值}}」这一件事，故独立成件而非塞进
  `replay/__init__.py`（后者受 `tests/present/test_kernel_size.py` 体积门禁约束）。

易错点:
  - 取数逐日 `frames.build_frame(date)`（npz 缓存，旧时间线引擎同路径）——**成本随窗口
    长度线性增长**，这是 as-of 的代价（见评估报告 §4）。
  - pc 种子（previousClose）只用**一次** `frames.prev_closes(start)` 后按日滚动
    （对齐 `tools/replay.py`）；逐日重新全量查询 pc 是 ~分钟级，会拖垮回测。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def load_market_slots(start: str, end: str, *, days: int = 300,
                      min_n: int = 200) -> dict[str, dict[str, float]]:
    """窗口内**逐日逐槽**全市场均涨幅 → ``{date: {HH:MM: 值%}}``（市场门 as-of 输入）。

    口径: 与生产 `scan._mkt_gain(snaps)` / `frames.MinuteFrame.mkt_gain(pos, pc_map)`
      一致（**该槽 open** 的全市场等权均值）—— 即旧时间线引擎的逐槽口径。

    **失败返回 {}**（调用方据此回退日频标量 / fail-closed）—— 不静默伪造。
    """
    out: dict[str, dict[str, float]] = {}
    try:
        from app.market_cn.auto.core.data import frames as fr
        lo, hi = str(start)[:10], str(end)[:10]
        ds = [d for d in fr.trading_dates(days_back=days, end=end) if lo <= d <= hi]
        if not ds:
            return {}
        pc_map = fr.prev_closes(ds[0])          # 唯一一次全量 pc 查询 (npz 缓存)
    except Exception as e:                       # noqa: BLE001
        log.warning("[mkt_slots] 逐槽市场门取数失败(%s~%s): %s", start, end, e)
        return {}

    for d in ds:
        try:
            frame = fr.build_frame(d)
        except Exception as e:                   # noqa: BLE001
            log.warning("[mkt_slots] %s 建帧失败: %s", d, e)
            continue
        if frame is None or not getattr(frame, "codes", None) or not len(frame):
            continue
        n_pos = int(frame.counts.max())          # 该日有 bar 的最大槽位数
        slots: dict[str, float] = {}
        for pos in range(min(n_pos, len(fr.MI_HHMM))):
            if int((frame.counts > pos).sum()) < min_n:   # 样本不足 → 该槽不给值
                continue
            slots[fr.MI_HHMM[pos]] = round(frame.mkt_gain(pos, pc_map), 3)
        if slots:
            out[d] = slots
        for c in frame.codes:                    # pc_map 滚到次日 (对齐 tools/replay.py)
            lc = frame.last_close(c)
            if lc > 0:
                pc_map[c] = lc
    return out
