"""contract.py — 策略文件契约（唯一接口面）。

设计要点①②：断点如何切片、保存哪些信息，由策略文件决定（state 是策略自定义 JSON，
展示层原样存取、不解释）；展示点在哪里也由策略文件定义（stages 表）。

内核折叠序（预处理/回测/实时三分支共用，逐位一致）：

    progress_events = strategy.evaluate(state, day_input, prev_progress)
    state           = strategy.step(state, day_input.bar)

evaluate 在 step **之前**跑：state 的语义 = "截至昨日收盘" 的切片。
盘中判定（如 14:56 触发）依赖的历史特征恰好是"截至昨日"，与旧版口径天然对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class InsufficientHistory(Exception):
    """init_state 收到的 bars 不足（框架按 SEED_BARS 统一喂，策略自判够不够）。"""


@dataclass(frozen=True)
class Stage:
    """展示点声明。realtime = 该阶段需要实时确认的时刻锚。

    - realtime = "HH:MM"         点时刻（如 D1 开盘 09:31）
    - realtime = "HH:MM-HH:MM"   滚动窗口（如 14:50-15:00），窗口内每 tick 可触发
    - realtime = None            纯日线判定，预处理落盘即可（如 D1 收盘结算）
    - visible = False            内部状态（如"候选观察"），不进展示
    """

    key: str
    label: str
    realtime: str | None = None
    visible: bool = True


@dataclass
class Progress:
    """规则进度。payload 内容策略自定，展示层只按 stage 查 stages 表取文案。"""

    stage: str
    date: str
    payload: dict = field(default_factory=dict)
    next_realtime: str | None = None   # 下一个需要实时确认的时刻锚（None=无需实时）
    source: str = "preprocess"         # preprocess | realtime


@dataclass
class DayInput:
    """折叠一步的当日输入。三套分支只在"数据来源"上有差别，结构相同。

    - bar: 当日日线 {time,open,high,low,close,volume}；实时分支为部分 bar
      （快照累计值：close=last、volume=当日累计量、open=当日开盘）
    - ctx: 盘中数据 {"latest": 快照, "series": [快照行], "mkt_gain": float}；
      纯日线判定时为 None
    """

    code: str
    bar: dict
    ctx: dict | None = None

class StrategyProtocol(Protocol):
    """展示层对策略的**结构化契约**（core → strategies 不产生 import 依赖）。

    生产 `strategies.base.StrategyBase` 实现本 Protocol（折叠契约已并入，
    全部策略单继承），展示层只按本 Protocol 取用，不感知任何策略细节（设计要点①）。
    """

    key: str
    name: str
    stages: tuple
    default_params: dict
    SEED_BARS: int

    def init_state(self, code: str, bars: list[dict]) -> dict:
        """seed：用截至昨日的全量历史 bars 建初始切片。"""
        ...

    def step(self, state: dict, bar: dict) -> dict:
        """每日推进一根（O(1)~O(window)）。纯函数：返回新 state，不改入参。"""
        ...

    def probe(self, state: dict) -> list[tuple[str, float]]:
        """除权探针：切片里若干"历史某日 (date, close)"锚点，严格相等比对。"""
        ...

    def evaluate(self, state: dict, inp: "DayInput",
                 prev: "Progress | None") -> list["Progress"]:
        """返回本日产出的进度事件（0~2 条，按时间序；末条 = 当前进度）。"""
        ...

    def init_shared(self, shared: dict | None) -> None:
        """用持久化的策略级状态恢复内部共享对象（每轮开头调用，幂等）。"""
        ...

    def shared_snapshot(self) -> dict | None:
        """返回需持久化的策略级状态（JSON 可序列化）；None = 无。"""
        ...

    def begin_day(self, date: str, states: dict, bars: dict) -> dict | None:
        """每日折叠前调用一次，返回日级上下文（经 ctx["_day"] 注入）。"""
        ...

    def realtime_shortlist(self, codes: list[str], snaps: dict,
                           mkt_gain: float | None = None,
                           stage: str | None = None) -> list[str]:
        """实时旁支的便宜预筛（默认全过）。"""
        ...
