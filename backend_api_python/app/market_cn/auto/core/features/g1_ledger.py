"""g1_ledger.py — G1 预处理台账：把「中断切片」持久化，次日只做 D+1 的延续处理。

目标形态（用户 2026-10-05）：**预处理与回测是同一套系统和数据流**，二者唯一区别是
预处理要"记录中断切片"。实时（盘中）不是第三条路径，而是预处理的**临时分支**：
拿当日一根未收盘 bar 到状态副本上试探，**不落盘**，收盘结算时才正式推进。

    ┌── 回测/全量 ────────────────────────────────────────────────┐
    │  bars(全序列) → _g1_arrays → _g1_mask → _aggregate(池)      │
    └────────────────────────────────────────────────────────────┘
    ┌── 预处理/日常 ──────────────────────────────────────────────┐
    │  seed(一次) → state ──advance(每日1根)──→ state'            │
    │                        └→ g1_state_features ─┐             │
    │  同一批 _g1_mask / _aggregate ◄───────────────┘            │
    └────────────────────────────────────────────────────────────┘
    ┌── 实时(临时分支) ───────────────────────────────────────────┐
    │  preview(state副本 + 当日未收盘bar) → 特征 → 丢弃           │
    └────────────────────────────────────────────────────────────┘

三条路最终都汇到**同一个** `_g1_arrays / _g1_mask / _aggregate` ⟹ "同一套数据流"是
代码事实而不是口号（`test_g1_ledger.py` 用浮点等价锁住）。

★ 每条票一份状态，体积 = 3 个 float 锚 + win 根 OHLC 微缩窗口 + 2 个标量 ≈ 1~2 KB，
  全市场 ~5236 票 ⇒ 单份 JSON 5~10 MB（速度不是第一诉求，可读性/可核对优先；
  真要压缩再换列式/分片，接口不变）。

════════════════════════════════════════════════════════════════════
★ 除权 / 数据修正：本模块**不做任何事后纠偏**
════════════════════════════════════════════════════════════════════
用户 2026-10-05 裁定：除权归入**预处理重建**。价格体系一变，锚就作废 ⇒ 只能在新口径下
`rebuild()` 整票重来；重建完成前**该票不产出信号**（`STALE` 标记，不是"照旧出信号"）。
发现不一致一律 **fail-fast + 计数**，绝不静默降级（项目铁律）。

════════════════════════════════════════════════════════════════════
易错点（都真踩过）
════════════════════════════════════════════════════════════════════
1. 状态必须 `keep_window=True`：ATR 要 high/low，光有 closes 队列算不出特征，
   那时"每天只喂一根"就是假的（还得回库取历史）。
2. `advance` 遇到 `bar 日期 <= state 日期` 必须**抛错**而不是跳过：这类数据是修正/重发，
   跳过会让状态停在旧值且不报错（= 静默降级）。要处理就显式 `rebuild`。
3. 缺票（停牌）**不是错误**，不传即可；各票各走自己的时间轴。
4. `preview` 是纯函数式推进（`g1_state_step` 返回新 dict，不改原状态）⇒ 天然不污染，
   ⚠ 但**别**把它的结果塞回 `put_state`，那是把未收盘价固化进历史。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.market_cn.auto.core._paths import CACHE_ROOT
from app.market_cn.auto.core.features.cross_section import (
    G1_STATE_STATS, G1_WIN_MIN, _g1_arrays, _g1_mask,
    g1_state_features, g1_state_init, g1_state_step, g1_state_window_bars)
from app.market_cn.auto.core.market import get_board_type

#: 台账格式版本。读到的版本与自身不符 ⇒ **抛错**（宁可停机也不要读错的历史状态）。
G1_LEDGER_VERSION = 1

#: 台账可观测计数（静默降级是头号敌人）
LEDGER_STATS: Dict[str, int] = {
    "seed": 0, "advance": 0, "rebuild": 0, "skip": 0,
    "preview": 0, "save": 0, "load": 0, "reject": 0,
    "rewrite": 0, "pool": 0, "pool_reject": 0,
}

STATE_ROOT = os.path.join(CACHE_ROOT, "g1_state")

# ★ 池的**日级历史**（按年分片 jsonl）。没有它，预处理的收益归零：
#   池若只活在内存里，每次开机都要把全历史重算一遍，等于回到全量路径。
#   ⚠ 目录**跟随台账路径**（见 `G1Ledger.pool_dir`），不是固定全局目录 ——
#     否则测试写一次就把真实池历史污染了，而"重发保护"还会因此拒绝真实写入。


def _norm_state(st: dict) -> dict:
    """状态规范化 —— ★ JSON 往返会把 tuple 变成 list，必须拉回来。

    ⚠ 为什么值得纠结类型: `head` 是一组不可再分的锚值，本该是 tuple；序列化回来变 list 后
      `_anchor_step` 照解包，功能不受影响 ⇒ **不会报错**，直到某天有人做 `state == other`
      或拿它当 dict key，才发现同一份状态有两种样子。这类"看起来没事"的类型漂移正是
      静默降级的温床 ⇒ 在**唯一入口** `put_state` 与 `load` 处拉回。
    """
    if st.get("head") is not None:
        st["head"] = tuple(float(x) for x in st["head"])
    return st


def default_ledger_path(win: int = G1_WIN_MIN) -> str:
    """默认台账路径。**窗口宽度进文件名** —— 换 win 等于换一套状态，串着用会算错。"""
    return os.path.join(STATE_ROOT, "ledger_win%d.json" % int(win))


def _day(bar) -> str:
    return str(bar["time"])[:10]


def _tol(v: float) -> float:
    """浮动等价判据（同 test_g1_seed）：相对 + 绝对双下限。"""
    return max(1e-6, 1e-9 * abs(float(v)))


class G1Ledger:
    """per-code 状态台账。用法::

        lg = G1Ledger(win=35)
        lg.seed({code: bars})                 # 首次 / 全量重建
        lg.advance("2026-10-05", {code: [bar]})   # D+1 延续: 每票只喂 1 根
        lg.save()
        f = lg.features("000001")             # 不需要再取历史 bars
        # 盘中: 临时分支
        pf = lg.preview_features("000001", intraday_bar)   # 不落盘

    ⚠ `board_of` 用于 G1 门的 ATR 分位（main / gem_star 阈值不同）；默认按代码推断。
    """

    def __init__(self, path: Optional[str] = None, win: int = G1_WIN_MIN,
                 board_of: Optional[Callable[[str], str]] = None):
        win = int(win)
        # ⚠ 低于下界时 `big20` 的回看都够不到 ⇒ 特征值恒 NaN 且暖机门照样"通过"
        #   (NaN 比较为 False 的另一面是"全 False"也是合法结果) ⇒ 必须挡在建这一层。
        if win < G1_WIN_MIN:
            raise ValueError("win=%d < G1_WIN_MIN=%d" % (win, G1_WIN_MIN))
        self.win = win
        self.path = path or default_ledger_path(self.win)
        self._board_of = board_of or get_board_type
        self._book: Dict[str, dict] = {}
        #: ★ 每票**播种起点**（= seed/rebuild 时 bars[0] 的日期）。旁路存放而不是塞进
        #:   state：state 是 `g1_state_step` 重建的新 dict，塞进去会被静默丢掉。
        #:   有了它，`audit` 才能取回**同一区间**做 apples-to-apples 对账 ——
        #:   否则拿"滑动窗口重取的一批"比"播种窗口推出的一批"，dif0 差 ~1e-6
        #:   （实测 2026-10-05），那是**播种起点不同**的残差，不是增量算错。
        self._since: Dict[str, str] = {}
        #: 上一次 `advance` 中**被跳过的票** {code: 原因}（仅 strict=False 时可能有值）
        self.last_failed: Dict[str, str] = {}
        self._dirty = False

    # ---------------- 持久化 ----------------
    def load(self) -> "G1Ledger":
        """读盘。文件不存在 ⇒ 空台账（首次）；**版本不符 ⇒ 抛错**，禁止静默当空处理。"""
        if not os.path.isfile(self.path):
            return self
        with open(self.path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        v = blob.get("v")
        if v != G1_LEDGER_VERSION:
            raise ValueError("台账版本 %r != %d: %s" % (v, G1_LEDGER_VERSION, self.path))
        if int(blob.get("win", 0)) != self.win:
            raise ValueError("台账 win=%r != %d: %s（换窗往来必需换文件）"
                             % (blob.get("win"), self.win, self.path))
        self._book = {c: _norm_state(s) for c, s in dict(blob.get("book", {})).items()}
        self._since = {c: str(d)[:10] for c, d in dict(blob.get("since", {})).items()}
        self._dirty = False
        LEDGER_STATS["load"] += 1
        return self

    def save(self) -> None:
        """原子写（tmp + os.replace）：中断/崩溃不会留下半截文件。

        ⚠ 为什么必须原子：台账损坏后所有票都要重建＝预处理全部失效，这是最贵的事故。
        """
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        blob = {"v": G1_LEDGER_VERSION, "win": self.win, "book": self._book,
                "since": self._since}
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".lg", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(blob, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self._dirty = False
        LEDGER_STATS["save"] += 1

    def __enter__(self):
        self.load()
        return self

    def __exit__(self, *exc):
        # ⚠ 只在**没抛异常**时落盘：带着半成品状态写入比不写更危险。
        if exc == (None, None, None) and self._dirty:
            self.save()
        return False

    # ---------------- 查询 ----------------
    def codes(self) -> List[str]:
        return sorted(self._book)

    def has(self, code: str) -> bool:
        return code in self._book

    def state(self, code: str) -> dict:
        """★ 返回**引用**（纯读语义）。要临时推演用 `preview_*`，别就地改。"""
        try:
            return self._book[code]
        except KeyError:
            LEDGER_STATS["reject"] += 1
            raise KeyError("台账里没有 %s —— 没 seed 还是被当作停牌跳过了？" % code)

    def put_state(self, code: str, st: dict, since: Optional[str] = None) -> None:
        if int(st.get("win", 0)) != self.win:
            raise ValueError("状态 win=%r != 台账 win=%d" % (st.get("win"), self.win))
        if not st.get("window"):
            raise ValueError("状态未携带 window —— 建时必须 keep_window=True")
        self._book[code] = _norm_state(st)
        if since:
            self._since[code] = str(since)[:10]
        self._dirty = True

    def since_of(self, code: str) -> Optional[str]:
        """该票的播种起点（None = 未知，通常是旧版台账 ⇒ 只能靠重建补上）。"""
        return self._since.get(code)

    # ---------------- 三个动作: seed / advance / rebuild ----------------
    def seed(self, bars_by_code: Dict[str, list], force: bool = False) -> int:
        """批量建状态（== 回测侧的全量路径，共用 `_g1_arrays` 的同一批数据）。

        force=False: 已存在的票**跳过**（幂等，适合每日增量补新票）。
        ⚠ 这里的"跳过"与 `advance` 的"日期不前进就抛错"是两回事：
          这里是同一批历史重复 seed（无害），那是数据修正/重发（有害）。
        """
        n = 0
        for code, bars in bars_by_code.items():
            if not bars:
                continue
            if not force and code in self._book:
                continue
            if len(bars) < self.win + 1:
                LEDGER_STATS["skip"] += 1
                continue
            self.put_state(code, g1_state_init(bars, win=self.win, keep_window=True),
                           since=str(bars[0]["time"])[:10])
            n += 1
        LEDGER_STATS["seed"] += n
        return n

    def rebuild(self, code: str, bars: list) -> None:
        """**除权/数据修正的唯一入口**：在新价格口径下整票重建。

        ⚠ 必须重建而不是"继续推进" —— 锚由历史价格推出，口径一变锚就作废，
          沿用会让后续所有 MACD 类读数错得毫无规律（永不报错）。
        ⚠ 重建期间该票**不应产出信号**：调用方负责（本账本只保证状态正确）。
        """
        if len(bars) < self.win + 1:
            raise ValueError("%s bars=%d < win+1=%d，无法重建" % (code, len(bars), self.win + 1))
        self.put_state(code, g1_state_init(bars, win=self.win, keep_window=True),
                       since=str(bars[0]["time"])[:10])
        LEDGER_STATS["rebuild"] += 1

    def advance(self, date: str, bars_by_code: Dict[str, list],
                strict: bool = True) -> Dict[str, dict]:
        """D+1 的**延续处理**：每票只喂当日的新 bar ⇒ 状态前进一格。

        ⚠ fail-fast 语义（对应"静默降级是头号敌人"）：
          · `bar 日期 <= state["date"]` ⇒ ValueError 并计数。这类数据是**重发/修正**，
            跳过会让状态停在旧价格上且不报错。要处理请显式 `rebuild`。
          · 字典里没出现的票（停牌）**不是错误**，直接不动。
          · 台账里没有的新票 ⇒ 忽略（`seed` 负责），因为缺历史建不出状态。

        strict:
          True (默认) ⇒ 第一票撞上就抛错（单票语义，老行为）。
          False       ⇒ **跳过该票**并记入 `self.last_failed`，其余照常推进。
            ★★ 为什么需要 False：日常跑批是 5000+ 票一批，个别票数据源重发/乱序
               是**个例事故**；抛错会让**整批当天跑不成**，且已推进的票因未 `save`
               全部作废（白跑一整天）。个例隔离 ≠ 静默降级 —— 前提是被跳过的票
               **显式登记**在 `last_failed` 且调用方必须把它写进 report/告警。
               ⚠ 用 False 却不看 `last_failed`，那就是静默降级。
        """
        self.last_failed = {}
        out: Dict[str, dict] = {}
        for code, new in bars_by_code.items():
            if not new or code not in self._book:
                continue
            st0 = self._book[code]
            d0 = _day(new if isinstance(new, dict) else new[0])
            if d0 <= st0["date"]:
                LEDGER_STATS["reject"] += 1
                msg = ("%s: 新 bar 日期 %s <= 状态日期 %s（重发/修正/除权？请走 rebuild）"
                       % (code, d0, st0["date"]))
                if strict:
                    raise ValueError(msg)
                self.last_failed[code] = msg
                continue
            st1 = g1_state_step(st0, [new] if isinstance(new, dict) else list(new))
            self.put_state(code, st1)
            out[code] = st1
        LEDGER_STATS["advance"] += len(out)
        return out

    # ---------------- 产出: 特征 / 门判定 / 实时分支 ----------------
    def features(self, code: str) -> Dict[str, Any]:
        """由**台账自带状态**算窗口特征（不需要任何历史 bars）。"""
        return g1_state_features(self.state(code))

    def board_of(self, code: str) -> str:
        """该票的板块（G1 的 ATR 分位按板块取阈值）。构造时注入，缺省按代码推断。"""
        return self._board_of(code)

    def g1_pass(self, code: str) -> bool:
        """末位是否进 G1 池。⚠ 暖机按 **age**（逻辑年龄），不是窗口下标。"""
        st = self.state(code)
        f = g1_state_features(st)
        board = self._board_of(code)
        return bool(_g1_mask(f, board, age=int(st["age"]))[-1])

    def day_report(self, date: str, bars_by_code: Dict[str, list]) -> Dict[str, Any]:
        """一天的闭环：推进 + 产出当日 G1 名单。便于跑批与端到端比对。"""
        adv = self.advance(date, bars_by_code)
        hit = sorted(c for c in adv if self.g1_pass(c))
        return {"date": date, "advanced": len(adv), "missing": len(self._book) - len(adv),
                "g1": hit}

    # ---------------- 池的日级历史 (预处理收益的**唯一**来源) ----------------
    def pool_dir(self) -> str:
        """池历史目录 = 台账同目录下的 `pool_{台账名}_win{W}/`。

        ⚠ 两个"跟随"都不能少（各自踩过一次）：
          · **跟随台账**（不放固定全局目录）：否则测试一跑就把真实池历史写脏，
            真实写入还会被"末条日期 >= 当日"的重发保护挡住 —— 测试反过来破坏生产。
          · **跟随台账文件名**（不止于目录）：同目录下放两份台账（如 win35 的
            正式版与演练版）时，只按目录+win 分会把两本账的池写进同一个文件，
            于是"今天是 09-30、你却要写 09-29"被判成重发（2026-10-05 实测撞到）。
        """
        stem = os.path.splitext(os.path.basename(self.path))[0] or "ledger"
        return os.path.join(os.path.dirname(self.path) or ".",
                            "pool_%s_win%d" % (stem, self.win))

    def pool_path(self, year: int) -> str:
        """池历史文件路径（按年分片）。win 进目录名 —— 换窗等于换一套池。"""
        return os.path.join(self.pool_dir(), "%d.jsonl" % int(year))

    def pool_days(self, date_from: Optional[str] = None,
                  date_to: Optional[str] = None) -> List[dict]:
        """读回池日级历史（日期升序）。跨年自动拼；缺年份文件 ⇒ 静默跳过（合法：那年没跑）。"""
        out: List[dict] = []
        y0 = int(str(date_from)[:4]) if date_from else 1990
        y1 = int(str(date_to)[:4]) if date_to else 2100
        a = str(date_from)[:10] if date_from else None
        b = str(date_to)[:10] if date_to else None
        for y in range(y0, y1 + 1):
            p = self.pool_path(y)
            if not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    r = json.loads(ln)
                    if int(r.get("win", 0)) != self.win:
                        continue          # 同文件里混了别的 win？按 win 过滤而不是报错
                    d = str(r.get("date", ""))[:10]
                    if a and d < a:
                        continue
                    if b and d > b:
                        continue
                    out.append(r)
        out.sort(key=lambda r: str(r.get("date", "")))
        return out

    def pool_last(self) -> Optional[dict]:
        """最近一条池记录（判重发用）。从今年往下回溯，最多 6 年。"""
        y = datetime.now().year
        for k in range(6):
            p = self.pool_path(y - k)
            if not os.path.isfile(p):
                continue
            last = None
            with open(p, "r", encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln:
                        last = json.loads(ln)
            if last is not None:
                return last
        return None

    def pool_append(self, rec: dict) -> None:
        """追加一条当日池记录（jsonl 行式，单行写入 ≈ 原子）。

        ★ 重发保护：末条日期 **>=** 当日 ⇒ **抛错**并计数。
          当日重复跑是"重发"；要覆盖必须显式重建当日（先删该年文件再写），
          不能靠"覆盖末行"糊过去 —— 那会让人分不清池里到底是哪一版。
        """
        d = str(rec.get("date", ""))[:10]
        if not d:
            raise ValueError("池记录缺 date")
        last = self.pool_last()
        if last is not None and str(last.get("date", ""))[:10] >= d:
            LEDGER_STATS["pool_reject"] += 1
            raise ValueError("池历史末条日期 %s >= 当日 %s（重发？请显式重建当日）"
                             % (last.get("date"), d))
        body = dict(rec)
        body["date"] = d
        body["win"] = self.win
        p = self.pool_path(int(d[:4]))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")
        LEDGER_STATS["pool"] += 1

    # ---------------- 除权 / 数据改写检测 ----------------
    def probe_dates(self) -> Dict[str, str]:
        """每票**窗口首日**的日期 —— 改写检测的探针点（调用方据此去取 1 根 bar）。"""
        return {c: st["window"][0][0]
                for c, st in self._book.items() if st.get("window")}

    def detect_rewrite(self, probe_by_code: Dict[str, dict]) -> Dict[str, str]:
        """改写/除权检测：拿库里的「窗口首日那根」与状态里的微缩窗口比对。

        ★★ 为什么**只比首日**就完备（2026-10-05 推导，不是拍脑袋）：
          前复权以最新价为基准回推 —— 除权日 D **当天及之后**因子 = 1.0（价格不变），
          **D 之前**的价格整体缩放。于是只有两种情形：
            · D >  窗口首日 ⇒ 首日落在 D 之前 ⇒ **首日必变** ⇒ 检出
              （覆盖除权日落在窗口内的全部情形）
            · D <= 窗口首日 ⇒ 窗口内每根都在 D 当天或之后 ⇒ **全不变** ⇒ 无需重建
              （这不是漏检，是真的没有影响）
          同一探针还顺带覆盖「数据源补发/修正历史 bar」这类非除权改写。

        ⚠ 判据用**严格相等**而不是容差：探针与状态同源同算法（都过 `unadj_to_qfq`），
          正常情况逐位相等；给容差等于给真实改写留后门 —— 静默降级的温床。

        Returns:
            {code: 原因} —— 非空者需走 `rebuild`（新口径整票重来）。
        """
        bad: Dict[str, str] = {}
        for code, bar in (probe_by_code or {}).items():
            st = self._book.get(code)
            if not st or not st.get("window") or bar is None:
                continue
            w0 = st["window"][0]
            d = str(bar.get("time"))[:10]
            if d != w0[0]:
                # 探针取错日子（停牌日库里没有该日行）不算改写，但也必须知道
                bad[code] = "探针日期 %s != 窗口首日 %s" % (d, w0[0])
                continue
            for j, k in ((1, "high"), (2, "low"), (3, "close")):
                v = float(bar.get(k))
                if v != float(w0[j]):
                    bad[code] = "%s %s: 库 %r != 状态 %r（除权/改写）" % (d, k, v, w0[j])
                    break
        LEDGER_STATS["rewrite"] += len(bad)
        return bad

    # ······ 实时 = 预处理的**临时分支** ······
    def preview_step(self, code: str, bar: dict) -> dict:
        """用当日（未收盘）bar 推一份**临时状态**。`g1_state_step` 是纯函数 ⇒ 不动台账。"""
        st = self.state(code)
        LEDGER_STATS["preview"] += 1
        return g1_state_step(st, [bar])

    def preview_features(self, code: str, bar: dict) -> Dict[str, Any]:
        """未收盘 bar 的临时特征序列（末位=盘中读数）。**不落盘，多次调用可重复**。"""
        return g1_state_features(self.preview_step(code, bar))

    def preview_pass(self, code: str, bar: dict) -> bool:
        st = self.preview_step(code, bar)
        f = g1_state_features(st)
        board = self._board_of(code)
        return bool(_g1_mask(f, board, age=int(st["age"]))[-1])

    # ---------------- 自检: 预处理是不是 == 回测 ----------------
    def verify(self, code: str, bars_full: list, keys: Optional[Iterable[str]] = None,
               strict_age: bool = True) -> List[str]:
        """拿全量 bars（回测口径）逐项对账，返回问题清单（空 = 完全一致）。

        ★ 这是整套机制的**存在理由**：预处理与回测若要共用一套数据流，就必须能
          随时证明二者等价。`verify` 可以随时跑（慢，但正确性优先）。

        ⚠⚠ `strict_age`（2026-10-05 冒烟实测才看清）：**默认必须关掉**才好用。
          `age` 是"逻辑年龄"（seed 时 = 取数窗口行数，之后每天 +1，单调递增）；
          而 `hub.daily(code, 200, as_of=D)` 取的是**滑动窗口**（右端前进 k 天，
          左端也移出 k 天 ⇒ 长度几乎恒定 ~200）。两者**必然**不等：
              seed(as_of=D-1)=201 → 推进 2 天 → age=203；重取 as_of=D 仍是 201。
          这不是错位，是两种量的定义不同 ⇒ 硬判相等只会让 `audit` 天天报假警，
          把一个"可自证工具"变成噪声源（和静默降级一样有害）。
          ★ 真正要锁的是**特征值等价**（下面逐项比对），那才是门判定所依赖的。
          `age` 的唯一用途是暖机 `cut = 68-(age-n)`：高估无害（cut 已是 0）、
          **低估有害**（会把末位砍掉 ⇒ 漏信号）⇒ 由 `seed` 取满 200 根来兜底。
        """
        st = self.state(code)
        if _day(bars_full[-1]) != st["date"]:
            return ["日期不齐: 全量末日 %s != 状态日期 %s" % (_day(bars_full[-1]), st["date"])]
        if len(bars_full) < self.win + 1:
            return ["全量仅 %d 根 < win+1=%d，无法对账" % (len(bars_full), self.win + 1)]
        if strict_age and len(bars_full) != st["age"]:
            return ["数据年龄不齐: 全量 %d 根 != state.age %d（滑动窗口 vs 逻辑年龄，"
                    "见 docstring；常规对账应传 strict_age=False）"
                    % (len(bars_full), st["age"])]
        a = _g1_arrays(bars_full)                       # 回测侧
        b = g1_state_features(st)                       # 预处理侧
        keys = list(keys) if keys else [k for k in a if k != "dates"]
        bad = []
        for k in keys:
            va, vb = a[k][-1], b[k][-1]
            if va != vb and abs(float(va) - float(vb)) > _tol(va):
                bad.append("%s: 全量 %r vs 增量 %r" % (k, va, vb))
        return bad

    def stats(self) -> Dict[str, int]:
        """合并本身的动作计数与状态机的计数（一处看全）。"""
        d = dict(LEDGER_STATS)
        d.update({("g1state_" + k): v for k, v in G1_STATE_STATS.items()})
        d["n_codes"] = len(self._book)
        return d
