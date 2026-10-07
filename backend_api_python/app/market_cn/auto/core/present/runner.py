"""runner.py — 生命周期（展示层全部职责）。

每日 D+1 收盘、数据齐全后跑一步折叠；状态可疑 → 切片重建（唯一恢复手段，没有回退/
降级/迁移；四个重建条件与同日幂等见 `_rebuild_reason`）。重建 = 只重建数据切片 state；
规则进度 current 与事件流水 events 是"发生过的事实"（设计要点②的展示记录），**跨重建
保留** —— 持仓阶段不会因数据修复被静默丢弃（否则"永不过期/信号丢失"类 bug 复现）。

⚠ 保留的代价: 旧结构 payload 会被新 evaluate 当 prev 消费 ⇒ **改 stage 键 / payload
  结构 = 视同改规则**，但 ④ 的 sha 重建**兜不住**（旧 payload 保留）⇒ 走重的一档:
  删状态目录重建，或 evaluate 对未知结构的 prev 明确忽略（contract.py 顶部同一条）。

fold 三分支共用：回测 = 循环调 run_day；预处理 = 每天调一次并落盘。

====================================================================
落盘与缓存 —— 滑动展示必须配读写缓存，否则全耗在磁盘
====================================================================
当前态（实测数据与决策过程见 .workbuddy/memory / 改进方案 §5.4）:
  1. **进程内共享缓存**（同 root 共享一份后端）: 否则"save 后新建实例读"必然落空
     ⇒ 只能每次落盘 ⇒ 缓存白加。
  2. **一个策略 key 一个文件，一轮只写一次**: 写 = 更新缓存 + 标脏；落盘只在
     flush()（batching() 退出 / 显式 / atexit 兜底）。单票 advance 不落盘。
     ⚠ **不分片**: dump 不是瓶颈、IO 次数才是（每次 open+os.replace 固定 3.2 ms
     × 片数 ⇒ 片数越多越慢，全程无拐点）⇒ 1 片写读双胜。
     ⚠ 单文件代价: 崩溃丢失面变大（靠 tmp+os.replace 原子替换 + 重建条件②兜）；
     实时点查首读整文件（dragon 16.6 MB ≈ 505 ms，长驻进程每天只付一次）。

⚠ **落盘出口唯一**: 策略文件不得自己开文件写盘。跨票的**策略级共享状态**（如 g56
  的横截面池台账）经 `init_shared`/`shared_snapshot` 契约与本策略每票 state 存在
  **同一文件**、同一轮落盘 ⇒ 生命周期一致（旧 g56 台账自带文件且默认不落盘 ⇒ 重启
  后池静默丢失、分位照算不报错）。

⚠ 第二个 IO 源: `strategy_source_hash` 每票读一次策略源算 sha256 = 0.82 s/日（曾占
  滑动 43%）。现单条缓存 (上次校验时刻, mtime, size, sha): TTL 内直接回、过期才 stat
  且 (mtime,size) 未变就不读文件 ⇒ 3.5 ms/日，④ 语义保持。

⚠ 切片格式变更的处置只有一条: **删状态目录重建**。文件里读不出 `codes` 段就视同
  损坏（按无记录装载，下轮 flush 整文件覆写），不做旧格式兼容分支。

⚠ 崩溃语义: flush 前进程死 ⇒ 本轮未落盘的推进丢失，**不做补偿**（下次由重建
  条件②date_gap 检出整票重建）；flush 失败**抛异常 + 登记**, 不静默。
"""

from __future__ import annotations

import atexit
import hashlib
import inspect
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.market_cn.auto.core.present.contract import (
    DayInput, InsufficientHistory, Progress, StrategyProtocol,
)


class RebuildNeedsHistory(Exception):
    """重建触发但调用方未提供全量历史（1 日延伸接口的硬要求：重建必须能重seed）。"""


#: path -> (上次校验时刻, mtime, size, sha256) —— **一个条目一种失效语义**（见文件头
#: 「第二个 IO 源」）。TTL 内直接回；过期 stat 且 (mtime,size) 未变则不读文件。
_HASH_CACHE: dict[str, tuple[float, float, int, str]] = {}
_HASH_TTL = 1.0     # 秒


def strategy_source_hash(strategy: StrategyProtocol) -> str:
    """策略文件内容 sha256（④ 改规则 = 该策略切片自动重建，无版本号/迁移）。

    失效只有一条线索：条目里的"上次校验时刻"。TTL 内直接回（连 stat 都不做）；
    过期才 stat，(mtime,size) 没变 ⇒ 不读文件，只把校验时刻拨到 now。
    """
    src_file = inspect.getfile(type(strategy))
    now = time.monotonic()
    hit = _HASH_CACHE.get(src_file)
    if hit is not None and now - hit[0] < _HASH_TTL:
        return hit[3]                                      # ① 节流命中
    try:
        st = os.stat(src_file)
        sig: tuple | None = (st.st_mtime, st.st_size)
    except OSError:
        sig = None
    if sig is not None:
        if hit is not None and hit[1] == sig[0] and hit[2] == sig[1]:
            _HASH_CACHE[src_file] = (now, sig[0], sig[1], hit[3])   # ② 内容未变，复用
            return hit[3]
    with open(src_file, "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    if sig is not None:
        _HASH_CACHE[src_file] = (now, sig[0], sig[1], h)
    return h


def _progress_to_dict(p: Progress | None) -> dict | None:
    if p is None:
        return None
    return {"stage": p.stage, "date": p.date, "payload": p.payload,
            "next_realtime": p.next_realtime, "source": p.source}


def _progress_from_dict(d: dict | None) -> Progress | None:
    if d is None:
        return None
    return Progress(stage=d["stage"], date=d["date"], payload=d.get("payload") or {},
                    next_realtime=d.get("next_realtime"), source=d.get("source", "preprocess"))


@dataclass
class Record:
    """每 (strategy, code) 一份：切片 state + 规则进度 current + 事件流水。

    state 是策略自定义 JSON，本层不解释（设计要点①）。
    """

    date: str
    state: dict
    current: Progress | None = None
    events: list = field(default_factory=list)   # append-only 进度事件流水（展示事实）
    strategy_hash: str = ""

    def to_json(self) -> dict:
        return {"date": self.date, "state": self.state,
                "current": _progress_to_dict(self.current),
                "events": self.events, "strategy_hash": self.strategy_hash}

    @classmethod
    def from_json(cls, d: dict) -> "Record":
        return cls(date=d["date"], state=d["state"],
                   current=_progress_from_dict(d.get("current")),
                   events=list(d.get("events") or []),
                   strategy_hash=d.get("strategy_hash", ""))


class StateStoreFlushError(OSError):
    """flush 落盘失败。⚠ 必须显式抛出并登记：静默的落盘失败 = 展示层"看起来在推进、
    实际每次都从头重建"，最难发现的一类缺陷。"""


class _Backend:
    """一个 state 根目录对应的**进程内共享**缓存 + 单文件落盘后端（见文件头「落盘与缓存」）。

    ⚠ 共享而非随实例: 调用方/测试会重复构造 StateStore(root)，缓存随实例走则
      "save 之后新建实例读"必然落空 ⇒ 只能每次 save 都落盘 ⇒ 缓存白加。
    """

    def __init__(self, root: str):
        self.root = root
        self._recs: dict[str, dict[str, Record]] = {}   # key -> {code: Record}
        self._meta: dict[str, dict] = {}                # key -> 策略级共享状态
        self._loaded: set[str] = set()                  # 已尝试装载的 key
        self._dirty: set[str] = set()                   # 待落盘的 key
        self.last_flush_error: str | None = None
        self.stats = {"loads": 0, "puts": 0, "flushes": 0,
                      "file_reads": 0, "file_writes": 0}

    def path(self, key: str) -> str:
        return os.path.join(self.root, f"{key}.json")

    def _ensure(self, key: str) -> None:
        """首次访问某 key 时**整文件一次读入**（天然批量预取，无分片映射）。

        ⚠ 未知格式 = 视同损坏（不解析、不迁移）: 按"无记录"装载，下轮 flush 覆写
          ⇒ 格式处置只剩"删状态目录重建"（打 stderr，不静默）。
        """
        if key in self._loaded:
            return
        self._loaded.add(key)
        p = self.path(key)
        if not os.path.exists(p):
            return
        self.stats["file_reads"] += 1
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and "codes" in raw:   # 现行格式：每票 state + 策略级共享状态
            self._recs[key] = {code: Record.from_json(d)
                               for code, d in (raw["codes"] or {}).items()}
            self._meta[key] = raw.get("meta") or {}
        else:
            print(f"[StateStore] 切片格式未知，视同损坏: {p}", file=sys.stderr)

    def get(self, key: str, code: str) -> Record | None:
        self.stats["loads"] += 1
        self._ensure(key)
        r = self._recs.get(key, {}).get(code)
        if r is None:
            return None
        # 返回**浅拷贝**：读方改动不应污染缓存（否则"改了没 save"会变成幽灵状态）
        return Record(r.date, dict(r.state), r.current, list(r.events), r.strategy_hash)

    def get_meta(self, key: str) -> dict:
        """策略级共享状态（跨票，如横截面池台账）—— 与本 key 的 state 同文件。"""
        self._ensure(key)
        return self._meta.get(key) or {}

    def set_meta(self, key: str, snap: dict) -> None:
        """登记策略级共享状态；随本 key 的切片一起落盘（同一轮仍是 1 次 IO）。"""
        self._ensure(key)
        self._meta[key] = snap
        self._dirty.add(key)

    def put(self, key: str, code: str, rec: Record) -> None:
        self.stats["puts"] += 1
        self._ensure(key)                    # 必须先载入：flush 是整文件覆写
        self._recs.setdefault(key, {})[code] = rec
        self._dirty.add(key)

    def flush(self) -> int:
        """把脏 key 各写一次。返回落盘的记录条数；失败抛 StateStoreFlushError。"""
        n = 0
        err = None
        for key in sorted(self._dirty):
            payload = {code: r.to_json() for code, r in (self._recs.get(key) or {}).items()}
            p = self.path(key)
            tmp = p + ".tmp"
            try:
                # 无条件 makedirs: 每天几次 flush，毫秒级成本，换掉 `_dir_ready` 标志
                # "目录被外部删除后写失败"的坑。紧凑分隔符: 体积/dump 时间各降 ~1/3
                # （state 是不透明 blob，不靠人读）。
                os.makedirs(self.root, exist_ok=True)
                s = json.dumps({"codes": payload, "meta": self._meta.get(key) or {}},
                               ensure_ascii=False, separators=(",", ":"))
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(s)
                self._replace_retry(tmp, p)
            except OSError as e:
                err = f"{key}: {type(e).__name__}: {e}"
                break
            self.stats["file_writes"] += 1
            n += len(payload)
        self._dirty.clear()
        self.stats["flushes"] += 1
        self.last_flush_error = err
        if err:
            raise StateStoreFlushError(err)
        return n

    @staticmethod
    def _replace_retry(tmp: str, dst: str, tries: int = 3) -> None:
        """os.replace 在 Windows 上偶发 WinError5（杀软/索引器占用）⇒ 重试。"""
        import time as _t
        for i in range(tries):
            try:
                os.replace(tmp, dst)
                return
            except OSError:
                if i == tries - 1:
                    raise
                _t.sleep(0.02 * (i + 1))


#: root(绝对路径) -> 共享后端。同进程内 StateStore(root) 重复构造仍命中同一份缓存。
_BACKENDS: dict[str, _Backend] = {}


def _flush_all_at_exit() -> None:
    """进程退出兜底：把未落盘的推进写出去。失败必须吵出来（不静默）。"""
    for root, b in list(_BACKENDS.items()):
        try:
            b.flush()
        except Exception as e:                      # noqa: BLE001 - 退出路径不能二次抛
            print(f"[StateStore] atexit flush 失败 root={root}: {e}", file=sys.stderr)


atexit.register(_flush_all_at_exit)


class StateStore:
    """切片状态存取（展示层把 state 当不透明 blob）。

    读 = 进程内共享缓存（首次按 key 整文件载入）；写 = 只更新缓存并标脏；落盘 =
    `flush()` / `batching()` 退出 / atexit。**不要指望 save 已落盘**。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._b = _BACKENDS.get(self.root)
        if self._b is None:
            self._b = _BACKENDS[self.root] = _Backend(self.root)
        self._depth = 0

    # ---- 基本存取 ----
    def load(self, key: str, code: str) -> Record | None:
        return self._b.get(key, code)

    def exists(self, key: str) -> bool:
        """该 key 是否已有切片（冷启动判定；不装载文件，也不让外部拼路径）。"""
        return os.path.exists(self._b.path(key))

    def save(self, key: str, code: str, rec: Record) -> None:
        self._b.put(key, code, rec)

    # ---- 策略级共享状态（随本 key 的切片文件一起落盘） ----
    def get_meta(self, key: str) -> dict:
        return self._b.get_meta(key)

    def set_meta(self, key: str, snap: dict) -> None:
        self._b.set_meta(key, snap)

    def flush(self) -> int:
        return self._b.flush()

    @property
    def stats(self) -> dict:
        return self._b.stats

    @property
    def last_flush_error(self) -> str | None:
        return self._b.last_flush_error

    # ---- 批量事务：进入=只写内存，退出=整批落盘 ----
    @contextmanager
    def batching(self):
        """一轮推进的写事务（**唯一**批量入口；可嵌套；最外层退出时才 flush）。

            with store.batching():
                for ...: runner.advance_all(...)

        ⚠ 不再提供 `with store:`（同一套 depth 逻辑的第二份拷贝）：不回答"哪个权威"。
        """
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            if self._depth <= 0:
                self._depth = 0
                self.flush()


def evaluate_day(strategy: StrategyProtocol, code: str, state: dict, bar: dict,
                 ctx: dict | None, prev: Progress | None
                 ) -> tuple[list[Progress], Progress | None]:
    """**展示工作表「一日事件」的唯一判定处**（2026-10-07 口径裁定）。

    裁定: **判定(ready) 恒 stateless，生命周期(exec/exit 闭合) 恒 stateful**。
    ⇒ 本函数 = `evaluate(state, inp, prev)` 的全量（结算链 + 状态机，`rec.current`
    的来源）+ **stateless ready 补齐**。理由与实测证据见改进方案 §2.6d；一句话：
    stateful 在持仓/入场日「先结算、提前返回」⇒ 当天不判 ready，而生产 `scan_signals`
    是 stateless ⇒ 生产落库、切片却没有的「持仓期重合信号日」（回放实测: 判定日集合的
    唯一分歧就是它，只生产 15/15 全落在投影持仓区间内）。不补 = 切 writer 后历史信号
    凭空消失，那不是等价切换而是改行为。

    ⚠ 补齐的事件**追加在当日生命周期事件之后**（exec 09:30 在前、ready 收盘在后）——
      链配对的前提: 「开启交易的 ready」= exec 之前最后一条 ready（见 `store.project_rows`）；
      顺序写反会把当日那条信号误认成入场信号。
    ⚠ 只补 (stage,日期) 未出现过的 ready（平仓日两路同一条 ⇒ 去重）；`prev is None`
      时 stateful ≡ stateless ⇒ 只算一次，其余日子跑两遍（evaluate 无副作用，正确性优先）。
    """
    inp = DayInput(code, bar, ctx)
    events = list(strategy.evaluate(state, inp, prev) or [])
    if prev is None:
        return events, (events[-1] if events else None)
    head = events[-1] if events else None          # ← 生命周期头，绝不取补齐的那条
    seen = {(e.stage, str(e.date)[:10]) for e in events}
    for e in (strategy.evaluate(state, inp, None) or []):
        if e.stage == "ready" and (e.stage, str(e.date)[:10]) not in seen:
            events.append(e)
    return events, head


def _fold_one(strategy: StrategyProtocol, code: str, state: dict, bar: dict,
              ctx: dict | None, prev: Progress | None, *,
              dual: bool = False) -> tuple[dict, list[Progress], Progress | None]:
    """折叠一步 —— **推进序的唯一定义处**（evaluate 在 step 之前）。

    prev 由调用方的模式给出（stateless=None / stateful=rec.current），见 contract
    顶部「两种推进模式」。InsufficientHistory **不在此吞**: stateless 视为本日无事件
    但照常 step，stateful 冒泡 —— 一个函数抹平两种语义比两处循环更危险。

    dual=True ⇒ 事件走 `evaluate_day`（工作表口径），只有 `_fold_step`（切片落盘）用；
    `fold_range`/回放枚举恒 False —— 回测链语义不能被补齐事件污染。
    返回 ``(state, events, head)``；head = `rec.current` 的来源。dual 下 head 取
    **stateful 那一路**（补齐的 ready 不许顶替生命周期头，否则次日双开仓）。
    """
    if dual:
        events, head = evaluate_day(strategy, code, state, bar, ctx, prev)
    else:
        events = strategy.evaluate(state, DayInput(code, bar, ctx), prev) or []
        head = events[-1] if events else None
    return strategy.step(state, bar), events, head


def fold_range(strategy: StrategyProtocol, code: str, bars: list[dict],
               i0: int = 0, i1: int | None = None, *,
               stages: tuple[str, ...] | None = ("ready",),
               ctx_provider=None,
               stateful: bool = False) -> list[tuple[int, Progress]]:
    """**唯一**折叠实现：seed 一次 + 逐日 (`_fold_one`)。两种推进模式在此切换。

    返回 [(bar 下标, Progress)]。与 DailyRunner._fold_step 共用同一折叠序/门实现，
    差别只有 prev 来源（契约见 contract「两种推进模式」）。

    Args:
        stages: 收集哪些阶段。``None``=全收（回测/调试 ready→exec→exit 完整链）；
            默认 ``("ready",)``=只收 ready（生产投影口径）。
        ctx_provider: 可选 ``callable(idx, bars)->dict|None``。intraday 策略（knife/
            tail）的 evaluate 需 ``ctx["series"]`` 快照序列，由 feed 侧供给。
        stateful: ``False``（默认）=stateless，prev 恒 None（不结算/不抑制，生产投影
            口径）；``True``=prev=上一步末事件（回测/调试必须，否则 exec/exit 不闭合）。

    ⚠ 批量枚举调用方（scan_days / 诊断工具）必须走本函数，不得自写折叠循环；
    init_state 未实现的旧策略抛 NotImplementedError，由调用方退化到 scan_signals。
    """
    i1 = len(bars) - 1 if i1 is None else i1
    state, j = None, i0
    while j <= i1:                                  # seed：起点尽量早，不足则后滑
        try:
            state = strategy.init_state(code, bars[:j])
            break
        except InsufficientHistory:
            j += 1
    if state is None:
        return []
    out: list[tuple[int, Progress]] = []
    prev: Progress | None = None
    for k in range(j, i1 + 1):
        ctx = ctx_provider(k, bars) if ctx_provider is not None else None
        try:
            state, events, head = _fold_one(strategy, code, state, bars[k], ctx, prev)
        except InsufficientHistory:
            state, events, head = strategy.step(state, bars[k]), [], None
        if head is not None and stateful:
            prev = head                     # 与 DailyRunner.rec.current 同语义
        for e in events:
            if stages is None or e.stage in stages:
                out.append((k, e))
    return out


class DailyRunner:
    """生命周期 runner（策略无关）。"""

    def __init__(self, store: StateStore):
        self.store = store

    # ---- 重建判定（1 日延伸口径：只看昨/今两根 + 探针锚） ----
    def _rebuild_reason(self, strategy: StrategyProtocol, rec: Record | None,
                        today: dict, yesterday: dict | None,
                        probe_bars: dict | None) -> str | None:
        """返回重建原因；None=正常推进；"noop"=同日重跑幂等拒绝。

        四条件（设计要点④）：① 日期错位（重发/乱序）② 日期断档（切片不在昨根上）
        ③ 除权/订正（probe 锚严格比对）④ 策略文件被修改。"""
        if rec is None:
            return "seed"
        if rec.strategy_hash != strategy_source_hash(strategy):
            return "strategy_changed"          # ④
        if today["time"] < rec.date:
            return "reordered"                  # ① 乱序/旧数据
        if probe_bars:
            for d, c in strategy.probe(rec.state):  # ③（锚窗口两端）
                b = probe_bars.get(d)
                if b is not None and float(b["close"]) != float(c):
                    return "probe_mismatch"     # 锚缺失=无法校验，跳过；不等=改写
        if today["time"] == rec.date:
            return "noop"                       # 同日重跑：幂等拒绝（probe 已通过）
        if yesterday is not None and yesterday["time"] != rec.date:
            return "date_gap"                   # ② 切片不在昨根上 = 时间线不连续
        return None

    def _ensure_shared(self, strategy: StrategyProtocol) -> None:
        """把持久化的策略级共享状态灌回策略实例（幂等；每轮/每票开头调用）。

        ⚠ 不能按 key 去重：同 key 可有多个策略实例（测试里两个 G56Strategy），
        去重会让新实例拿到空台账。从持久状态恢复是幂等的，每次调是安全的。
        """
        strategy.init_shared(self.store.get_meta(strategy.key))

    def probe_anchors(self, strategy: StrategyProtocol, code: str) -> list[tuple[str, float]]:
        """数据层按需取探针锚的历史 bar（每天 2 根）→ 传给 advance 的 probe_bars。"""
        rec = self.store.load(strategy.key, code)
        return strategy.probe(rec.state) if rec else []

    def advance(self, strategy: StrategyProtocol, code: str, today: dict, *,
                yesterday: dict | None = None, ctx: dict | None = None,
                probe_bars: dict | None = None,
                history: list[dict] | None = None) -> tuple[Record, list[Progress]]:
        """D+1 的 **1 日延伸处理**（预处理主入口）。

        日常输入只有：today（当日新 bar）+ yesterday（昨根，断档校验）+
        probe_bars（探针锚历史 bar，除权校验）+ ctx（当日盘中）。全量 history
        **只在重建时才需要**（init_state 重 seed）；缺 → RebuildNeedsHistory。
        """
        self._ensure_shared(strategy)
        rec = self.store.load(strategy.key, code)
        reason = self._rebuild_reason(strategy, rec, today, yesterday, probe_bars)
        if reason == "noop":
            return rec, []
        if reason is not None:
            if not history:
                raise RebuildNeedsHistory(f"{code}: 重建({reason})需要全量历史")
            state = strategy.init_state(code, history[:-1])   # 切片 = 截至昨日
            rec = Record(date="", state=state,
                         current=rec.current if rec else None,
                         events=rec.events if rec else [],
                         strategy_hash=strategy_source_hash(strategy))
        return self._fold_step(strategy, code, rec, rec.state, today, ctx)

    def run_day(self, strategy: StrategyProtocol, code: str, bars: list[dict],
                ctx: dict | None = None) -> tuple[Record, list[Progress]]:
        """全量 bars 入口（回测分支/兼容包装）：内部走 advance 同一实现。"""
        if not bars:
            raise ValueError("bars 为空")
        return self.advance(
            strategy, code, bars[-1],
            yesterday=bars[-2] if len(bars) >= 2 else None,
            ctx=ctx, probe_bars={b["time"]: b for b in bars}, history=bars)

    def _fold_step(self, strategy: StrategyProtocol, code: str, rec: Record,
                   state: dict, bar: dict, ctx: dict | None):
        """一日推进 + 落盘。**事件流走 `dual=True`**（ready 恒 stateless，2026-10-07 裁定）
        —— 本方法是 `Record.events` 的**唯一**生产者，故裁定只需落在这里；
        `rec.current` 取生命周期头（head），补齐的 ready 不参与推进。"""
        rec.state, events, head = _fold_one(strategy, code, state, bar, ctx,
                                            rec.current, dual=True)
        rec.date = bar["time"]
        if head is not None:
            rec.current = head
        for e in events:
            rec.events.append(_progress_to_dict(e))
        self.store.save(strategy.key, code, rec)
        return rec, events

    def advance_all(self, strategy: StrategyProtocol, date: str,
                    day_inputs: dict[str, dict]) -> dict[str, list[Progress]]:
        """跨票 **1 日延伸**（预处理主入口；含 begin_day 池聚合）。

        day_inputs[code] = {today, yesterday?, ctx?, probe_bars?, history?}；
        同日无 bar（停牌）的票不出现在输入里即可（天然不推进）。重建需 history。
        """
        with self.store.batching():
            # 一轮 = 一个写事务: 整文件一次载入 + 结束一次落盘
            self._ensure_shared(strategy)
            resolved: dict[str, tuple[Record, dict, dict, dict | None]] = {}
            for code, di in day_inputs.items():
                today = di.get("today")
                if not today or today["time"] != date:
                    continue
                try:
                    rec = self.store.load(strategy.key, code)
                    reason = self._rebuild_reason(
                        strategy, rec, today, di.get("yesterday"), di.get("probe_bars"))
                    if reason == "noop":
                        continue
                    if reason is not None:
                        history = di.get("history")
                        if not history:
                            raise RebuildNeedsHistory(f"{code}: 重建({reason})需要全量历史")
                        state = strategy.init_state(code, history[:-1])
                        rec = Record(date="", state=state,
                                     current=rec.current if rec else None,
                                     events=rec.events if rec else [],
                                     strategy_hash=strategy_source_hash(strategy))
                except InsufficientHistory:
                    continue
                resolved[code] = (rec, rec.state, today, di.get("ctx"))
            day_ctx = None
            if resolved:
                day_ctx = strategy.begin_day(
                    date, {c: s for c, (_, s, _, _) in resolved.items()},
                    {c: b for c, (_, _, b, _) in resolved.items()})
                snap = strategy.shared_snapshot()
                if snap is not None:
                    self.store.set_meta(strategy.key, snap)
            out: dict[str, list[Progress]] = {}
            for code, (rec, state, today, ctx) in resolved.items():
                ctx2 = dict(ctx or {})
                if day_ctx is not None:
                    ctx2["_day"] = day_ctx
                _, events = self._fold_step(strategy, code, rec, state, today, ctx2)
                if events:
                    out[code] = events
            return out

    def run_day_all(self, strategy: StrategyProtocol, date: str,
                    bars_by_code: dict[str, list[dict]],
                    ctx_by_code: dict[str, dict] | None = None) -> dict[str, list[Progress]]:
        """全量 bars 入口（回测分支/兼容包装）：内部走 advance_all 同一实现。"""
        day_inputs = {}
        for code, bars in bars_by_code.items():
            if not bars or bars[-1]["time"] != date:
                continue
            day_inputs[code] = {
                "today": bars[-1],
                "yesterday": bars[-2] if len(bars) >= 2 else None,
                "ctx": (ctx_by_code or {}).get(code),
                "probe_bars": {b["time"]: b for b in bars},
                "history": bars,
            }
        return self.advance_all(strategy, date, day_inputs)
