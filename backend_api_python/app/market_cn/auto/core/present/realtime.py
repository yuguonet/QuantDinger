"""realtime.py — 实时处理：预处理在切片点上的临时分支（信息量极小）。

只做三件事：
  1. 看预处理落盘的规则进度（Progress.next_realtime），挑出此刻需要实时确认的票
     （"实时处理只需要看预处理中规则进度来进行是否要实时处理这只股票"）；
  2. 在 state **副本** 上调用同一个 evaluate（未收盘 bar + 盘中 ctx）；
  3. 把进度事件交给展示（source="realtime"）。

⚠️ 不写回 fold：切片 state 与权威 progress 只由预处理（内核）推进——
实时分支丢副本、不落盘。盘中没跑到也没关系：当晚预处理用同一 evaluate
会算出同样的事件（自愈）。展示层拿实时事件提前显示，晚上传预处理事件覆盖确认。
"""

from __future__ import annotations

import copy

from app.market_cn.auto.core.present.contract import (
    DayInput, Progress, StrategyProtocol,
)
from app.market_cn.auto.core.present.runner import Record, StateStore


def _in_anchor(hhmm: str, anchor: str) -> bool:
    """时刻锚匹配："HH:MM" 或 "HH:MM-HH:MM"（含端点）。"""
    if not anchor:
        return False
    if "-" in anchor:
        lo, hi = anchor.split("-", 1)
        return lo <= hhmm <= hi
    return anchor <= hhmm   # 点锚: 到点及之后（错过的 tick 可补触发）


def bar_from_snapshot(snap: dict) -> dict:
    """实时分支的当日部分 bar（快照累计口径：open=当日开盘、close=last、volume=累计）。"""
    t = str(snap.get("time") or "")
    return {"time": t[:10], "open": float(snap.get("open") or 0),
            "high": float(snap.get("high") or 0), "low": float(snap.get("low") or 0),
            "close": float(snap.get("last") or 0), "volume": float(snap.get("volume") or 0)}


class RealtimeBranch:
    def __init__(self, store: StateStore, strategies: dict[str, StrategyProtocol]):
        self.store = store
        self.strategies = strategies

    def tick(self, hhmm: str, codes: list[str], snaps: dict,
             series_by_code: dict[str, list] | None = None,
             mkt_gain: float | None = None) -> list[tuple[str, Progress]]:
        """一次实时 tick。snaps: {code: 最新快照}; series_by_code: {code: 快照序列}。

        返回 [(code, progress)] 供展示；不写任何盘上状态。
        """
        series_by_code = series_by_code or {}
        out: list[tuple[str, Progress]] = []
        # 单策略演示：逐策略跑（多策略时同一 code 可多策略各自出事件）
        for key, strategy in self.strategies.items():
            cand = [c for c in codes if self._due(key, c, hhmm)]
            kept = []
            for code in cand:
                rec = self.store.load(key, code)
                if rec is None or rec.current is None:
                    continue
                # 预筛只对触发扫描（watch）生效；阶段转换票直接放行
                picked = strategy.realtime_shortlist(
                    [code], snaps, mkt_gain, stage=rec.current.stage)
                if picked:
                    kept.append((code, rec))
            for code, rec in kept:
                snap = snaps.get(code)
                if not snap:
                    continue
                ctx = {"latest": snap, "series": series_by_code.get(code) or [snap],
                       "mkt_gain": mkt_gain}
                # 临时分支：副本试推，用完即弃
                state_copy = copy.deepcopy(rec.state)
                events = strategy.evaluate(
                    state_copy, DayInput(code, bar_from_snapshot(snap), ctx), rec.current)
                for e in events:
                    e.source = "realtime"
                    out.append((code, e))
        return out

    def _due(self, key: str, code: str, hhmm: str) -> bool:
        rec = self.store.load(key, code)
        if rec is None or rec.current is None:
            return False
        anchor = rec.current.next_realtime
        return _in_anchor(hhmm, anchor or "")
