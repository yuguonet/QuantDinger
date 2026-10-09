"""core/replay/intraday.py — IntradayFeed（改进方案 §4-P1.5）。

盘中策略（knife_catch / tail_oversold）的 evaluate 需要 ``ctx["series"]`` 快照序列；
纯日线 feed 给不了 ⇒ 它们**不会触发**（不是折叠缺陷，是 feed 缺口，§6-11）。

本 feed = 日线折叠 + 当日分钟快照注入：
  - 折叠仍复用 `fold_range`（唯一编排），只通过 ctx_provider 补 ctx；
  - 快照由 `core.data.frames.snap_series` 生成，与 `MinuteFrame.snap` **逐字段同形**
    （生产链走 frame.snap，回放走 snap_series，口径必须一致）。
  - 市场门注入两层：**逐槽** `ctx["mkt_series"]`（as-of 正解）+ 日频 `ctx["mkt_gain"]`（回退）。

取数策略（重要）:
  - **不走** ``build_frame(date)``：那是**全市场单日**帧，单票回放逐日调用 = N 个
    交易日 × 全市场查询，无缓存日数十秒/日 ⇒ 全历史回放必超时（§6-4）。
  - 改为 ``load_code_minutes(code, start, end)`` **一次 SQL/年**取该票全区间分钟线，
    内存按日切分。实测单票 300 日 ≈ 4 万行，秒级。
  - qfq 对整段一次完成（复权因子是时间序列连续量，按日切片做会有段间断点）。

易错点:
  - 快照窗口必须覆盖策略硬编码的判定区间（knife/tail 是 14:56~15:00），窗口开窄了
    会静默零触发；默认取 ("14:50","15:00") 宽覆盖。
  - ``previousClose`` 取日线自身前一根 close，**不调** ``fr.prev_closes(date)``：
    那是一次全市场查询，按天调用会让回测退化到超时。
"""

from __future__ import annotations

import logging

from app.market_cn.auto.core.data import frames as fr
from app.market_cn.auto.core.replay import DailyFeed

log = logging.getLogger(__name__)

#: 默认快照窗口 = **全日**（09:31~15:00）。
#:
#: ⚠ 不得按策略 scan_spec.windows 截断：evaluate 里确有 ``hh < min_hhmm: continue`` 的
#: 判定窗口过滤，但快照还被**回看类特征**消费（tail 的 ``_tail_ret_v2`` 需前 20 分钟、
#: knife 的 ``_tail_ret(..., minutes=20)`` 与 ``_vw_frac`` 同）。截断到 14:50~15:00
#: 只剩 11 行 ⇒ tail_ret 恒 None ⇒ ``data`` 门静默拒（实测 tail 1164/1243 次被拒，
#: 库内有真实信号的票全部复现不出来）。生产链 scan.py:806 给的就是全日序列。
DEFAULT_WINDOW = ("09:31", "15:00")


class IntradayFeed(DailyFeed):
    """盘中 feed（单票）：日线折叠 + 当日快照序列。

    Args:
        bars:   该票日线（折叠主轴）。
        code:   股票代码（快照取数需要）。
        window: 快照窗口 (起,止) "HH:MM"，含端点。
        preload: 构造时即加载全区间分钟线（默认 True）。False = 首次 series() 时惰性加载。
    """

    def __init__(self, bars: list[dict], code: str, *,
                 window: tuple[str, str] = DEFAULT_WINDOW,
                 lo: int = 0, hi: int | None = None, preload: bool = True,
                 mkt_map: dict[str, float] | None = None,
                 mkt_slots: dict[str, dict[str, float]] | None = None):
        super().__init__(bars, lo=lo, hi=hi, mkt_map=mkt_map)
        self.code = str(code)
        self.window = window
        self.mkt_slots = mkt_slots or {}   # {date:{HH:MM:值}} 逐槽市场门(as-of); 缺省退回 mkt_map
        self._mins: dict[str, list[dict]] = {}
        self._loaded = False
        self.minute_days = 0
        if preload:
            self._load()

    @property
    def exec_basis(self) -> str:
        return "1m"

    def _load(self) -> None:
        """一次性加载该票全区间分钟线，按日切分（幂等）。"""
        if self._loaded:
            return
        self._loaded = True
        bars = self.bars
        if not bars:
            return
        # ⚠ hi 默认 None = 到最后一根（与 fold_range 的 i1 语义一致）；直接判 None 会
        #   让默认构造的 feed 静默不加载分钟线 → 盘中策略零触发且看不出原因
        hi = len(bars) - 1 if self.hi is None else self.hi
        start = str(bars[max(self.lo, 0)].get("time"))[:10]
        end = str(bars[min(hi, len(bars) - 1)].get("time"))[:10]
        if not start or not end or start > end:
            return
        try:
            self._mins = fr.load_code_minutes(self.code, start, end) or {}
        except Exception as e:                       # 不静默：取数失败=该票无盘中判定
            log.warning("[replay] %s 分钟线加载失败(%s~%s): %s", self.code, start, end, e)
            self._mins = {}
        self.minute_days = len(self._mins)

    def series(self, date: str, pc: float = 0.0) -> list[dict]:
        """当日快照序列（按分钟槽升序；无分钟数据返回空列表）。"""
        self._load()
        rows = self._mins.get(str(date)[:10])
        if not rows:
            return []
        return fr.snap_series(date, rows, pc, self.window[0], self.window[1])

    def _ctx_for(self, k: int, bars: list[dict]) -> dict:
        """快照序列 + 市场门（逐槽 as-of 优先；缺 mkt_slots 时退回日频标量 mkt_gain）。

        市场门 fail-closed（knife `kc_mkt` 读 ctx.mkt_gain）：mkt_slots 缺该日 → 不注入
        mkt_series → 策略回退标量。逐槽值按快照行的 "HH:MM" 对齐（缺槽 None ⇒ 该槽拒）。
        """
        pc = float(bars[k - 1].get("close") or 0) if k > 0 else 0.0
        ctx = super()._ctx_for(k, bars)
        date = str(bars[k].get("time"))[:10]
        rows = self.series(date, pc)
        ctx["series"] = rows
        day = self.mkt_slots.get(date) if self.mkt_slots else None
        if day:
            ctx["mkt_series"] = [day.get(str(r.get("time") or "")[11:16]) for r in rows]
        return ctx


def make_feed(strategy, code: str, bars: list[dict], *,
              mkt_map: dict[str, float] | None = None, **kw) -> IntradayFeed:
    """按策略 scan_spec 取窗口构造 IntradayFeed。

    Args:
        mkt_map: {date: 全市场均涨幅%}——**日频 close 回退**。盘中策略的市场门
            ``mkt_gate`` **fail-closed**，缺它则零触发（不报错）。
        **kw:    透传 IntradayFeed（如 ``mkt_slots``=逐槽 as-of 市场门，见 mkt_slots.py）。
    """
    return IntradayFeed(bars, code, window=DEFAULT_WINDOW, mkt_map=mkt_map, **kw)
