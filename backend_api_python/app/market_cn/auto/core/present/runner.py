"""runner.py — 生命周期（展示层全部职责）。

每日 D+1 收盘、数据齐全后跑一步折叠；状态可疑 → 切片重建（唯一恢复手段，
没有回退/降级/迁移）。重建条件（设计要点④ + 同日幂等）：

    同日重跑（bar 日期 == 切片日期）且无异常 → 幂等 no-op（不重复出事件）
    ① 日期错位：bar 日期 < 切片日期（乱序/旧数据重发）
    ② 日期断档：切片不在 bars[-2]（昨根）上（漏推进/停牌复牌补录/来源切换）
    ③ 除权/订正：probe(state) 锚点与当前 bars 严格相等比对失败（锚窗口两端：
       首根防历史整体改写，末根防同日修正）
    ④ 策略文件被修改：state 里存的策略源码 sha256 ≠ 当前

重建 = 只重建数据切片 state；规则进度 current 与事件流水 events 是
"发生过的事实"（设计要点②的展示记录），跨重建保留——持仓阶段不会因数据
修复被静默丢弃（否则"永不过期/信号丢失"类 bug 复现）。

⚠ 保留的代价: 旧结构 payload 会被新 evaluate 当 prev 消费 ⇒ **改 stage 键 / payload
  结构 = 视同改规则**，但 ④ 的 sha 重建**兜不住**（旧 payload 保留）⇒ 走重的一档:
  删状态目录重建，或 evaluate 对未知结构的 prev 明确忽略（contract.py 顶部同一条）。

fold 三分支共用：回测 = 循环调 run_day；预处理 = 每天调一次并落盘。

====================================================================
落盘与缓存（2026-10-06）—— 滑动展示必须配读写缓存，否则全耗在磁盘
====================================================================
逐票单文件 + 每步立即落盘的代价（实测 Windows/NTFS, per 票）:
    save 6.7~7.3 ms (makedirs 1.5 + dump/open 1.9 + replace 3.2)、load 0.3 ms
  ⇒ 一步 advance ≈ **7 ms 几乎全是 save**；5236 票 ≈ 36 s/日, 比现有批量全量
    重算 25 s **更慢** —— 不加缓存就让生产改滑动是净负收益。

目标态（本文件现状，只有两件事）:
  1. **进程内共享缓存**: 同 root 的 StateStore 共享一份后端, 实例重建不清空
     (否则"save 后新建实例读"必然落空 ⇒ 只能每次落盘 ⇒ 缓存白加)。
  2. **一个策略 key 一个文件, 一轮只写一次**: 写 = 只更新缓存 + 标脏；落盘只在
     flush()（batching() 退出 / 显式调用 / atexit 兜底）。单票 advance 不落盘。

为什么**不**分片（2026-10-06 实测推翻同日上午的分片方案）:
  5236 票 state 仅 1.9~16.6 MB ⇒ **dump 不是瓶颈，IO 次数才是**：每次
  open+os.replace 固定成本 3.2 ms × 片数 = 纯劣势，全程无拐点（write ms）:
    片数:      1      4     16     64    256
    knife    353    392    497    804   1921   （读同向 85 → 2271）
    dragon  2454   2488   2710   3030   4338   （读同向 505 → 3224）
  ⇒ 1 片（每 key 单文件）写读双胜，且少一层 crc32 分片映射。
  ⚠ 教训: 上午扫到"越少越快"后却取 16，理由是"单文件太大"这个**无数据的主观顾虑**
    —— 扫描必须走到极值再收手。
  ⚠ 单文件代价: ① 崩溃丢失面变大 —— 但 tmp+os.replace 原子替换 ⇒ 旧文件完好，下次
    由重建条件②检出重建；② 实时 tick 点查首读整文件（dragon 16.6 MB ≈ 505 ms）——
    后端进程内缓存只 load 一次，长驻进程每天只付一次。

端到端实测（5236 票，seed 一次 + 连续 3 日滑动，合成日线，同机；改前=16 分片）:
    knife 0.91→0.243 s (3.7x) | tail 0.87→0.260 s (3.3x) | dragon 3.67→1.007 s (3.6x)
    g56   5.86→4.571 s (1.3x —— 瓶颈是 begin_day 横截面池计算, 不是 IO)
    落盘次数 16 次/日 → 1 次/日

⚠ **落盘出口唯一**: 策略文件不得自己开文件写盘。跨票的**策略级共享状态**（如 g56
  的横截面池台账）经 `init_shared`/`shared_snapshot` 契约与本策略每票 state 存在
  **同一文件**、同一轮落盘 ⇒ 生命周期一致（旧 g56 台账自带文件且默认不落盘 ⇒ 重启
  后池静默丢失、分位照算不报错）。⇒ 四策略每日 ≈ 6.1 s（改前 11.3 s），低于全量重算 25 s。

⚠ 第二个 IO 源 (曾占滑动 43%): `strategy_source_hash` 每票读一次策略源文件算
  sha256 = 0.157 ms × 5236 = **0.82 s/日**。现单条缓存 (上次校验时刻, mtime, size,
  sha): TTL(1s) 内直接回、过期才 stat 且 (mtime,size) 未变就不读文件 ⇒ 3.5 ms/日，
  且 ④"改策略文件就重建" 语义保持。

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


#: path -> (上次校验时刻, mtime, size, sha256) —— **一个条目一种失效语义**。
#: ⚠ 不缓存的代价（实测）: 每票每天重读策略源文件算 sha256 = 0.157 ms/次
#:   × 5236 票 = **0.82 s/日**，与落盘同量级，曾占滑动总耗时的 43%。
#:   ⇒ 3.5 ms/日，且 ④ 语义保持: 改策略文件后下一轮（≥1s）必然检出并重建。
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
    """flush 落盘失败。

    ⚠ 必须显式抛出并登记，不得静默：静默的落盘失败 = 展示层"看起来在推进、
    实际每次都从头重建"，是最难发现的一类缺陷。
    """


class _Backend:
    """一个 state 根目录对应的**进程内共享**缓存 + 单文件落盘后端。

    为什么是进程内共享（而不是每个 StateStore 实例一份）:
        调用方/测试会**重复构造** StateStore(root)。若缓存随实例走，
        "save 之后新建实例读"必然落空 ⇒ 只能退化成每次 save 都落盘 ⇒ 缓存白加。
        共享后缓存生命周期 = 进程，与实例个数无关，落盘时机才解放出来。

    为什么一个 key 一个文件（而不是逐票单文件、也不是分片）:
        见文件头实测。落盘代价 = 每次 open+os.replace 的**固定成本**(3.2 ms)
        + 与总字节成正比的 dump 成本；5236 票 state 仅 1.9~16.6 MB，dump 不是
        瓶颈，**IO 次数才是** ⇒ 直接取极值：每 key 一次 IO。
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

        ⚠ 未知格式 = 视同损坏（不解析、不迁移）：可重建缓存不为旧格式背兼容债，此处按
          "无记录"装载，下轮 flush 覆写 ⇒ 格式处置只剩"删状态目录重建"（打 stderr，不静默）。
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
                # 每次 flush 无条件 makedirs: 每天 ~4-8 次 flush，毫秒级成本，换掉
                # `_dir_ready` 标志"目录被外部删除后写失败"的坑。
                os.makedirs(self.root, exist_ok=True)
                # 紧凑分隔符: 体积与 dump 时间都降约 1/3（state 是不透明 blob，
                # 不靠人读，可读性让步给 IO）
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
    """进程退出兜底：把还没落盘的推进写出去。失败必须吵出来（不静默）。"""
    for root, b in list(_BACKENDS.items()):
        try:
            b.flush()
        except Exception as e:                      # noqa: BLE001 - 退出路径不能二次抛
            print(f"[StateStore] atexit flush 失败 root={root}: {e}", file=sys.stderr)


atexit.register(_flush_all_at_exit)


class StateStore:
    """切片状态存取（展示层把 state 当不透明 blob）。

    读 = 进程内共享缓存（首次按 key 整文件载入）；写 = 只更新缓存并标脏；
    落盘 = `flush()` / `batching()` 退出 / atexit。**不要指望 save 已落盘**。
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._b = _BACKENDS.get(self.root)
        if self._b is None:
            self._b = _BACKENDS[self.root] = _Backend(self.root)
        self._depth = 0

    # ---- 基本存取 ----
    def load(self, key: str, code: str) -> Record | None:
        return self._b.get(key, code)

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


def _fold_one(strategy: StrategyProtocol, code: str, state: dict, bar: dict,
              ctx: dict | None, prev: Progress | None) -> tuple[dict, list[Progress]]:
    """折叠一步 —— **推进序的唯一定义处**（evaluate 在 step 之前）。

    prev 由调用方的模式给出（stateless=None / stateful=rec.current），见 contract
    顶部「两种推进模式」。InsufficientHistory **不在此吞**: stateless 视为本日无事件
    但照常 step，stateful 冒泡 —— 一个函数抹平两种语义比两处循环更危险。
    """
    events = strategy.evaluate(state, DayInput(code, bar, ctx), prev) or []
    return strategy.step(state, bar), events


def fold_range(strategy: StrategyProtocol, code: str, bars: list[dict],
               i0: int = 0, i1: int | None = None) -> list[tuple[int, Progress]]:
    """stateless 模式的**唯一**折叠实现：seed 一次 + 逐日 (`_fold_one`)。

    返回 [(bar 下标, ready 事件)]。与 stateful（DailyRunner._fold_step）共用同一
    折叠序、同一门实现，差别**只有** prev 恒 None —— 契约见 contract 顶部。

    ⚠ 批量枚举调用方（策略基类 scan_days / 诊断工具）必须走本函数，不得自写折叠
    循环：那等于第二份编排，两套口径只能靠对账维持一致（要点③否定的模式）。

    ⚠ init_state 未实现（未迁移折叠契约的旧策略）抛 NotImplementedError，由调用方
    退化到其 scan_signals —— 那份 scan_signals 本身就是唯一门实现。
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
    for k in range(j, i1 + 1):
        try:
            state, events = _fold_one(strategy, code, state, bars[k], None, None)
        except InsufficientHistory:
            state, events = strategy.step(state, bars[k]), []
        out.extend((k, e) for e in events if e.stage == "ready")
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
            by_date = probe_bars
            for d, c in strategy.probe(rec.state):  # ③（锚窗口两端）
                b = by_date.get(d)
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
        rec.state, events = _fold_one(strategy, code, state, bar, ctx, rec.current)
        rec.date = bar["time"]
        if events:
            rec.current = events[-1]
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
