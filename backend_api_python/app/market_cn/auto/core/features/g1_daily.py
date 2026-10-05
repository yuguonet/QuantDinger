# -*- coding: utf-8 -*-
"""g1_daily.py — G1 预处理的**日常推进入口**（编排层；不持有任何状态语义）。

分层约定（三层各管一件事，混在一起就没法单测）：
  · `cross_section` —— 特征语义（G1 门、增量状态机），不知道数据从哪来
  · `g1_ledger`     —— 台账语义（seed / advance / rebuild / 池历史），**不碰数据源**
  · `g1_daily`(本文件) —— 把上面两层接到真实数据源上：取数 → 检测 → 推进 → 落盘

★ 为什么单独一层：台账只认"喂进来的 bar"。一旦在里面 import 取数器，就没人能在无库
  环境下测它（现有 12 个台账门禁全部得连库）。编排外置 ⇒ 语义层保持纯函数。

════════════════════════════════════════════════════════════════
一天要做的四件事，**顺序不能改**
════════════════════════════════════════════════════════════════
  ① seed    新票（次新 / 之前停牌）⇒ 必须**先**做：seed 决定 `age`，而暖机门看 age
  ② detect  改写/除权检测，命中的票 `rebuild`（rebuild 用的历史**已含当日**）
  ③ advance 其余票喂当日 1 根
  ④ 判池 → 写池日级历史 → 落盘

⚠ ② 与 ③ 的顺序不是随意的：rebuild 之后状态日期**已经是当日**，再喂当日 bar 会被
  `advance` 判为重发而抛错 —— 这是设计意图（重发必须显式），不是 bug。
  所以 rebuilt 的票要**排除**在 advance 之外。

════════════════════════════════════════════════════════════════
★ 为什么 seed 必须取 200 根（不能"够窗口就行"）
════════════════════════════════════════════════════════════════
`_g1_mask` 的暖机看 **age**（这票累计有多少根历史），不是"这次传进来几根"：
    cut = G1_WARMUP - (age - n)     # G1_WARMUP = 68
若 seed 只取 win+1 = 36 根，age=36 ⇒ cut = 68-(36-35) = 67 ⇒ 整个窗口被暖机砍光，
**永远不出信号且不报错**（最典型的静默降级）。⇒ seed 必须用与回测**同一口径**
（`hub.daily(code, 200, as_of=...)` 的批量等价物），之后靠 advance 逐日 +1。

════════════════════════════════════════════════════════════════
收益观 / 边界（用户 2026-10-05 裁定）
════════════════════════════════════════════════════════════════
除展示层实时外，速度不是第一诉求 ⇒ 本入口**默认开启全部正确性检查**（改写检测、
`skip_rebuilt` 保守不出信号），宁可慢也要可自证。
⚠ 本模块**未接管任何生产路径**：`scan.py` 日常扫描仍走全量。切过来的前提是池日级
  历史已经连续落盘（否则每次开机都要重算全历史，收益归零）—— 属待拍板事项。
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.market_cn.auto.core.features.cross_section import G1_WIN_MIN
from app.market_cn.auto.core.features.g1_pool_source import DEFAULT_WIN

#: ★ 默认窗口为什么是 35 而不是下界 `G1_WIN_MIN`(21)：
#:   21 只是"锚播种后指标等价"的**理论下界**；默认必须与既有产物
#:   `ledger_win35.json` 及 `g1_pool_source.DEFAULT_WIN` 三者一致 ——
#:   默认值不一致会让 CLI 静默建出一本**新的空台账**，而消费方(prewarm)
#:   读的是 win35 ⇒ 表现为"池历史 0 天、看似没跑过"，排查方向完全跑偏。
from app.market_cn.auto.core.features.g1_ledger import G1Ledger, LEDGER_STATS
from app.market_cn.auto.core.market import get_board_type

logger = logging.getLogger(__name__)

#: seed / rebuild 的取数天数 —— 与回测 `hub.daily(code, 200, as_of=)` **同口径**。
#: ⚠ 改动等于改 age ⇒ 改暖机 ⇒ 改全市场信号。要改必须连同 `G1_WARMUP` 一起复核。
SEED_DAYS = 200

#: 参与 G1 的板块。`ATR_Q5` 只有这两档，其它板块走 `_g1_mask` 会 KeyError。
BOARD_OK = ("main", "gem_star")

#: 补跑能回溯的自然日上限（`trading_dates` 按年分表遍历 ⇒ 400 天 ≈ 2 张年表）。
BACKFILL_LOOKBACK = 400


# ================================================================
# 取数原语（批量 + 前复权，与 fetch_klines_batch 同一 qfq 实现）
# ================================================================

def _to_bars(rows: Sequence, code: str) -> List[dict]:
    """原始 tuple 行 → bars dict（过 unadj_to_qfq，与 fetch_klines_batch 同源）。"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    raw = [{
        "time": str(r[0])[:10],
        "open": float(r[1]), "high": float(r[2]),
        "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
    } for r in rows]
    return unadj_to_qfq(raw, code)


def _batch(codes: Sequence[str], start: str, end: str) -> Dict[str, List[dict]]:
    """区间批量取数 → {code: bars}（每票可能多于 1 根，按 time 升序）。"""
    from app.market_cn.auto.core.data.hub import _query_batch_raw
    codes = [c for c in codes if c]
    if not codes:
        return {}
    raw = _query_batch_raw("CNStock", list(codes), "1D", start_time=start, end_time=end)
    return {c: b for c, b in ((c, _to_bars(rows, c)) for c, rows in raw.items()) if b}


def _day_bars(codes: Sequence[str], date: str) -> Dict[str, dict]:
    """当日每票**一根** bar → {code: bar}。缺票不出现（停牌，不是错误）。"""
    got = _batch(codes, date, date + " 23:59:59")
    return {c: b[-1] for c, b in got.items() if b}


def _probe_bars(probe: Dict[str, str]) -> Dict[str, dict]:
    """{code: 窗口首日} → {code: 该日那一根}。**按日分组**，每组一次 SQL。

    ⚠ 为什么必须分组而不是取 [min,max] 一个区间：各票窗口首日可以差很远
      （长期停牌票的首日可能是几个月前），一个区间会让**所有**票都返回区间内
      的全部行 ⇒ IO 从"1 根/票"膨胀到"几十根/票"。
    """
    groups: Dict[str, List[str]] = {}
    for code, d in (probe or {}).items():
        if d:
            groups.setdefault(str(d)[:10], []).append(code)
    out: Dict[str, dict] = {}
    for d, cs in groups.items():
        for c, b in _day_bars(cs, d).items():
            out[c] = b
    return out


def _hist_bars(codes: Sequence[str], days: int = SEED_DAYS,
               as_of: Optional[str] = None) -> Dict[str, List[dict]]:
    """seed / rebuild 的历史窗口（与回测同口径，见模块 docstring）。"""
    from app.market_cn.auto.core.data.kline import fetch_klines_batch
    return fetch_klines_batch(list(codes), days=int(days), as_of=as_of)


def resolve_date(date: Optional[str] = None, lookback: int = 12) -> str:
    """目标交易日。不给 ⇒ 日线表里最近一个有记录的日子。

    ⚠ 不用 `datetime.now()`：非交易日/数据未回填时会得到一个没有数据的日期，
      然后"0 推进"被当成正常 ⇒ 静默降级。以**库里真有数据的日子**为准。
    """
    if date:
        return str(date)[:10]
    ds = _dates(lookback)
    return str(ds[-1])[:10]


#: 交易日列表缓存（**仅进程内**）。`trading_dates` 要按年分表遍历，补跑 250 天
#: 会调 250 次 ⇒ 不缓存的话光查日历就几分钟。
#: ⚠ 只适合**批处理进程**（一次跑完就退出）；常驻服务请调 `clear_dates_cache()`。
_DATES_CACHE: Dict[int, List[str]] = {}


def clear_dates_cache() -> None:
    _DATES_CACHE.clear()


def _dates(lookback: int = 30) -> List[str]:
    hit = _DATES_CACHE.get(int(lookback))
    if hit is not None:
        return hit
    from app.market_cn.auto.core.data.frames import trading_dates
    ds = trading_dates(lookback)
    if not ds:
        raise RuntimeError("近 %d 个自然日内查不到任何交易日 —— 日线表空了？" % lookback)
    out = [str(x)[:10] for x in ds]
    _DATES_CACHE[int(lookback)] = out
    return out


def prev_trade_date(date: str, lookback: int = BACKFILL_LOOKBACK) -> str:
    """D 的前一交易日 —— **seed / rebuild 的 as_of**。

    ★★ 为什么 seed 必须建到 D-1 而不是 D（冒烟实测撞出来的，不是理论推演）：
      若 seed 的窗口含当日，新票 seed 完状态日期就已经是 D；再喂当日 bar 会被
      `advance` 判为"重发"而抛错 —— 于是"新票永远进不了池"，且报错信息指向
      完全不相干的"除权？"。建到 D-1 ⇒ **所有票走同一条 advance 路径**，
      新票/老票/重建票不再有三条分支（分支越多越容易静默错位）。
    """
    d = str(date)[:10]
    prior = [x for x in _dates(lookback) if x < d]
    if not prior:
        raise RuntimeError("查不到 %s 之前的交易日（lookback=%d 太短？）" % (d, lookback))
    return prior[-1]


def universe(codes: Optional[Sequence[str]] = None,
             board_of: Optional[Callable[[str], str]] = None):
    """参与 G1 的代码表：限 `BOARD_OK`（`ATR_Q5` 只认这两档）。

    ⚠⚠ **已知事实**（2026-10-05 实测，非本模块引入）：`get_board_type` 对**不认识**
      的代码会**兜底返回 `main`** —— 北交所 8/43/83/92 开头、乃至 `"8xxxxx"` 这种
      乱码，一律判成 main。所以本过滤**挡不住北交所**，只挡得住将来新增的未知板块。
      这与生产 `_ensure_pool_daily`（同样 `board not in buckets` 判定）完全一致，
      ⇒ **不擅自修正**（改了就与回测口径分叉）。要真排除北交所必须按前缀另做，
      且那属于"改全市场口径"，需单独拍板 + 复核信号差异。

    Returns:
        (codes, dropped) —— dropped = {code: board}（非目标板块，**显式登记**而不是丢掉）
    """
    if codes is None:
        from app.market_cn.auto.core.data.kline import all_codes
        codes = all_codes()
    bof = board_of or get_board_type
    keep, dropped = [], {}
    for c in codes:
        if not c:
            continue
        try:
            b = bof(c)
        except Exception:
            dropped[c] = "?"
            continue
        if b in BOARD_OK:
            keep.append(c)
        else:
            dropped[c] = b
    return keep, dropped


# ================================================================
# 日常推进
# ================================================================

class G1DailyRunner:
    """一天闭环的编排器。用法::

        r = G1DailyRunner(win=35)
        rep = r.run()               # 取最新交易日, 走完整四步
        print(rep["g1"][:10])       # 当日 G1 池名单
        rep = r.run("2026-10-05")   # 补跑某天(日线已回填)

    ⚠ `run` 是**幂等之外的第二道保险**：同一天跑第二次会在 `advance`（日期不前进）
      或 `pool_append`（末条日期 >= 当日）处抛错，绝不静默覆盖。
    """

    def __init__(self, win: int = DEFAULT_WIN, path: Optional[str] = None,
                 ledger: Optional[G1Ledger] = None,
                 codes: Optional[Sequence[str]] = None,
                 board_of: Optional[Callable[[str], str]] = None):
        self.win = int(win)
        self.ledger = ledger or G1Ledger(path=path, win=self.win, board_of=board_of)
        self._codes = list(codes) if codes is not None else None
        self._board_of = board_of or get_board_type

    # ---------------- universe ----------------
    def target_codes(self) -> List[str]:
        if self._codes is None:
            self._codes, _ = universe(board_of=self._board_of)
        return self._codes

    # ---------------- 主流程 ----------------
    def run(self, date: Optional[str] = None, do_seed: bool = True,
            do_detect: bool = True, skip_rebuilt: bool = True,
            save: bool = True) -> Dict[str, Any]:
        """走完一天。返回 report（含**失败登记**，静默降级是头号敌人）。

        Args:
            date: 目标交易日；None ⇒ `resolve_date()`
            do_seed: 补種新票（成本 = 新票数 × 200 根）
            do_detect: 改写/除权检测（成本 = 1 根/票）
            skip_rebuilt: 当日重建过的票**不计入** G1 名单（保守，见下）
            save: 落盘（False 用于演练/测试）

        ★ `skip_rebuilt` 为什么默认开：重建是"换了一套价格口径重新开始"，首日读数
          尚未被 `verify` 验证过；把它混进当日池等于拿未经对账的读数去下注。
          保守起见单列到 `g1_skipped`，人看过再决定。
        """
        lg = self.ledger
        lg.load()
        d = resolve_date(date)
        d0 = prev_trade_date(d)          # ★ seed/rebuild 建到 D-1，当日统一由 advance 走
        codes = self.target_codes()
        errors: List[str] = []

        # ⚠ **整批重发**前置检查：不能只靠 advance 抛错 —— advance 在 strict=False 下
        #   会**跳过**这些票（个例隔离），于是"同一天跑了第二次"这种整批事故会被
        #   当成 N 个个例悄悄跳掉。
        # ★ 判据用**多数**而不是 `max(日期)`：后者只要有一票领先（脏数据/乱序）就拒
        #   绝整批，正好把"个例隔离"抵消掉（2026-10-05 实测撞到）。
        #   整批重发 = 绝大多数票都已 >= d；个例领先 = 少数票，交给 advance 隔离登记。
        lead = [c for c in lg.codes() if str(lg.state(c)["date"])[:10] >= d]
        if lead and len(lead) > 0.5 * len(lg.codes()):
            raise ValueError("台账中 %d/%d 票状态日期 >= %s —— 整批重发，拒绝"
                             "（要覆盖请先显式重建台账）" % (len(lead), len(lg.codes()), d))

        # ---- ① seed 新票 ----
        seeded: List[str] = []
        short: List[str] = []          # 历史不足(次新股)：**正常现象**，不是事故
        if do_seed:
            need = [c for c in codes if not lg.has(c)]
            if need:
                hist = _hist_bars(need, days=SEED_DAYS, as_of=d0)
                before = set(lg.codes())
                n = lg.seed(hist)                       # 幂等: 已存在的跳过
                seeded = sorted(set(lg.codes()) - before)
                short = sorted(set(hist) - set(seeded))
                # ⚠ 判据要能区分"次新股不够"与"真的种不上"（实测调过一次）：
                #   次新股历史恒 < win+1 根，会**每天都**出现 ⇒ 若也算 error，
                #   每天一条噪声会训练人忽略 errors，等于把 fail-fast 通道废掉。
                #   只有"取到了足够长的历史却仍然没种上"才是异常。
                if n == 0 and hist:
                    enough = [c for c, b in hist.items() if len(b) >= self.win + 1]
                    if enough:
                        errors.append("seed 0/%d 成功，其中 %d 票历史充足却未种上 —— 异常"
                                      % (len(hist), len(enough)))

        # ---- ② 改写/除权检测 → rebuild ----
        rewritten: Dict[str, str] = {}
        rebuilt: List[str] = []
        if do_detect:
            probe = lg.probe_dates()
            if probe:
                pb = _probe_bars(probe)
                if len(pb) < len(probe):
                    miss = sorted(set(probe) - set(pb))
                    errors.append("探针缺 %d/%d 票（停牌日无该行）: %s..."
                                  % (len(miss), len(probe), ",".join(miss[:5])))
                rewritten = lg.detect_rewrite(pb)
            if rewritten:
                hist = _hist_bars(sorted(rewritten), days=SEED_DAYS, as_of=d0)
                for c in sorted(rewritten):
                    bars = hist.get(c) or []
                    if len(bars) < self.win + 1:
                        errors.append("%s 需重建但历史仅 %d 根 ⇒ 状态作废待补"
                                      % (c, len(bars)))
                        continue
                    lg.rebuild(c, bars)                 # 建到 D-1，随后照常 advance 当日
                    rebuilt.append(c)

        # ---- ③ advance（新票 / 老票 / 重建票 **同一条路径**）----
        day = _day_bars([c for c in codes if lg.has(c)], d)
        # ★ strict=False: 个例脏数据**隔离**而不是拖垮整批（整批重发已在上面挡掉）。
        #   被跳过的票必须出现在 report["failed"] + errors —— 不看它就是静默降级。
        adv = lg.advance(d, day, strict=False)
        if lg.last_failed:
            errors.append("advance 跳过 %d 票（日期不前进，需显式 rebuild）: %s..."
                          % (len(lg.last_failed), ",".join(sorted(lg.last_failed)[:5])))

        if not adv and not rebuilt:
            errors.append("当日 %s 没有任何推进（%d 票均无当日 bar —— 日线未回填？）"
                          % (d, len(codes)))

        # ---- ④ 判池 ----
        # ★ 一次循环同时产出「当日桶」与「G1 名单」(池源与名单是同一次判断的两面)。
        #   桶 = 池历史的持久化形态(per-board 四元组), 没有它池就只活在内存里。
        hit, skipped = [], []
        rs = set(rebuilt)
        quad: Dict[str, Any] = {}
        try:
            from app.market_cn.auto.core.features import g1_pool_source as PS
            hit_all: set = set()
            bk = PS.day_buckets(lg, date=d, hit_out=hit_all)
            quad = {b: PS.quad_of(v.get(d, [])) for b, v in bk.items()}
            passed = sorted(hit_all)
            hit = [c for c in passed if not (skip_rebuilt and c in rs)]
            skipped = [c for c in passed if (skip_rebuilt and c in rs)]
        except Exception as e:
            errors.append("判池失败(池历史将缺当日): %s" % e)
            for c in sorted(adv):                 # 退化: 仍产出名单, 只是没有四元组
                try:
                    if lg.g1_pass(c):
                        (skipped if (skip_rebuilt and c in rs) else hit).append(c)
                except Exception as e2:
                    errors.append("g1_pass(%s) 异常: %s" % (c, e2))

        if save:
            lg.save()
            try:
                lg.pool_append({"date": d, "n_adv": len(adv), "n_miss": len(lg.codes()) - len(adv),
                                "n_new": len(seeded), "rewritten": sorted(rewritten),
                                "rebuilt": rebuilt, "g1": hit, "g1_skipped": skipped,
                                "quad": quad})
            except ValueError as e:      # 重发
                errors.append("pool_append 失败: %s" % e)
                raise

        return {"date": d, "win": self.win, "n_codes": len(codes),
                "seeded": seeded, "short": short,
                "rewritten": rewritten, "rebuilt": rebuilt,
                "advanced": len(adv), "missing": len(lg.codes()) - len(adv),
                "failed": dict(lg.last_failed),
                "g1": hit, "g1_skipped": skipped, "errors": errors,
                "stats": lg.stats()}

    # ---------------- 全量对账 ----------------
    def audit(self, codes: Optional[Sequence[str]] = None,
              limit: Optional[int] = None, strict_age: bool = True) -> Dict[str, List[str]]:
        """批量 `verify`：台账状态 vs **同一区间的全量重算**逐项对账（慢，但正确性优先）。

        ★★ 取数口径必须是「**播种起点 → 状态日期**」这个**确切区间**，不能是
          `hub.daily(code, 200, as_of=...)` 那种滑动窗口（2026-10-05 实测）：
          滑动窗口的起点随 as_of 一起前移 ⇒ 全量侧重新朴素播种的位置与增量侧不同
          ⇒ `dif0` 差 ~1e-6，看上去像"增量算错了"，其实是**播种起点差 2 天**的残差。
          按台账记录的 `since` 取同一区间 ⇒ 两侧播种位置一致 ⇒ 差异回到 1e-13 级。
          顺带：`advance` 只对真有当日 bar 的票 +1（停牌票不推进），故区间行数
          **应当**等于 age ⇒ `strict_age` 可以放心开着，对不上就是真错位。

        Returns:
            {code: [问题, ...]}；空 dict = 全部一致。
        """
        lg = self.ledger
        cs = list(codes) if codes is not None else lg.codes()
        if limit:
            cs = cs[:int(limit)]
        bad: Dict[str, List[str]] = {}
        groups: Dict[tuple, List[str]] = {}
        for c in cs:
            try:
                sd = lg.state(c)["date"]
                s0 = lg.since_of(c)
            except Exception as e:
                bad[c] = ["取状态失败: %s" % e]
                continue
            if not s0:
                bad[c] = ["缺播种起点 since（旧版台账）⇒ 需 rebuild 才能精确对账"]
                continue
            groups.setdefault((s0, sd), []).append(c)
        for (s0, sd), sub in sorted(groups.items()):
            got = _batch(sub, s0, sd + " 23:59:59")
            for c in sub:
                bars = got.get(c) or []
                if not bars:
                    bad[c] = ["区间 %s..%s 取数为空" % (s0, sd)]
                    continue
                try:
                    iss = lg.verify(c, bars, strict_age=strict_age)
                except Exception as e:
                    bad[c] = ["verify 异常: %s" % e]
                    continue
                if iss:
                    bad[c] = iss
        return bad

    def ledger_date(self) -> Optional[str]:
        """台账里最新状态日期（空台账 ⇒ None）。补跑的起点判据。"""
        return max((self.ledger.state(c)["date"] for c in self.ledger.codes()),
                   default=None)

    def backfill(self, start: Optional[str] = None, until: Optional[str] = None,
                 max_days: Optional[int] = None, **kw) -> Dict[str, Any]:
        """补跑一段交易日（**首次接入必须跑**；之后每天只需 `run()`）。

        ⚠ 只跑**台账最新日期之后**的日子 —— `start` 只是下界，不是"从这天强行重来"。
          强行重来意味着丢弃既有状态，那要先显式删台账，本方法**不做静默覆盖**。

        Returns:
            {"days": [...], "reps": [每日 report...], "errors": 汇总, "g1_total": n}
        """
        # ★★ 必须先 load: `ledger_date()` 读的是内存 `_book`, 而新建的 Runner
        #    尚未 load ⇒ 返回 None ⇒ 下面 "只跑最新日期之后" 的过滤**完全失效**,
        #    于是拿历史日期去 `run` ⇒ 撞"整批重发"拒绝 (2026-10-05 调度接线实测撞到:
        #    台账已推进到 09-30, backfill(until=09-30) 却报"状态日期 >= 09-16")。
        #    ⚠ 表现为"不该跑的日子被跑了", 排查时极易误判成日期逻辑错。
        self.ledger.load()
        ds = _dates(BACKFILL_LOOKBACK)
        cur = self.ledger_date()
        if start:
            ds = [x for x in ds if x >= str(start)[:10]]
        if until:
            ds = [x for x in ds if x <= str(until)[:10]]
        if cur:
            ds = [x for x in ds if x > cur]
        if max_days:
            # ★ 取**最近** N 天（语义："把最近这段补上"），不是最早 N 天 ——
            #   首跑时最早那几天的 D-1 可能落在日历表之外，会直接报"查不到前一交易日"。
            ds = ds[-int(max_days):]
        reps = [self.run(date=x, **kw) for x in ds]
        return {"days": [r["date"] for r in reps], "reps": reps,
                "errors": [e for r in reps for e in r["errors"]],
                "failed": {c: m for r in reps for c, m in r.get("failed", {}).items()},
                "g1_total": sum(len(r["g1"]) for r in reps)}

    # ---------------- 池历史回填 (首次接入的硬前提) ----------------
    def backfill_pool(self, target: Optional[str] = None, force: bool = False,
                      days: int = 200) -> int:
        """用**全量路径**把池历史一次性补满 —— 增量池的冷启动。

        为什么必须先回填: `score_r` 是滚动分位(w=20)，冷启动的头 20 天没有足够
        历史 ⇒ 值不可信。回填后，日常才只需算当日一格。

        ⚠ 池历史非空时默认拒绝(force=True 才覆盖)：覆盖历史 = 丢弃既有连续记录，
          那必须先显式删文件，本方法**不做静默覆盖**。
        """
        from app.market_cn.auto.core.data.kline import fetch_klines_batch, window_start
        from app.market_cn.auto.core.features import g1_pool_source as PS
        self.ledger.load()
        d = resolve_date(target)
        lo = str(window_start(int(days), d))[:10]
        raw = fetch_klines_batch(self.target_codes(), days=int(days), as_of=d)
        pool_bars = {}
        for c, bs in (raw or {}).items():
            sl = [b for b in bs if lo <= str(b["time"])[:10] <= d]
            if sl:
                pool_bars[c] = sl
        return PS.backfill(self.ledger, d, pool_bars, force=force)

    # ---------------- 池对账 (日常可跑的自证) ----------------
    def audit_pool(self, target: Optional[str] = None, tol: float = 1e-9,
                   days: int = 200) -> Dict[str, List[str]]:
        """**台账源池 vs 全量池**逐日逐 board 比对 —— 日常巡检的自证手段。

        ⚠ 判据用**浮点等价**而不是逐位相等: 增量状态与全量窗口的递推起点不同,
          rmed 有 ~1e-8 残差(实测), 这是既有属性不是错误; 但 score_r 与**信号
          集合**必须一致(实测 8 天 0 差异)。所以这里报出来的差异要人看过再定论。

        Returns: {board: [问题, ...]}；空 dict = 一致。
        """
        from app.market_cn.auto.core.data.kline import fetch_klines_batch, window_start
        from app.market_cn.auto.core.features import cross_section as CS
        from app.market_cn.auto.core.features import g1_pool_source as PS
        self.ledger.load()
        d = resolve_date(target)
        lo = str(window_start(int(days), d))[:10]
        raw = fetch_klines_batch(self.target_codes(), days=int(days), as_of=d)
        pb = {}
        for c, bs in (raw or {}).items():
            sl = [b for b in bs if lo <= str(b["time"])[:10] <= d]
            if sl:
                pb[c] = sl
        CS._POOL["target"] = None
        full = CS._ensure_pool_daily(d, bars_batch=pb)
        CS._POOL["target"] = None
        led = PS.try_ledger_pool(self.ledger, d)
        if led is None:
            return {"_": ["台账源不可用, 未比对: %s" % PS.LAST_REASON.get("try")]}
        bad: Dict[str, List[str]] = {}
        for b in ("main", "gem_star"):
            fb, lb = full.get(b, {}), led.get(b, {})
            iss = []
            for k in sorted(set(fb) | set(lb)):
                x, y = fb.get(k), lb.get(k)
                if x is None or y is None:
                    iss.append("%s 单侧缺失" % k)
                    continue
                for f in ("rmed", "score_r"):
                    xv, yv = x.get(f), y.get(f)
                    if xv is None and yv is None:
                        continue
                    if xv is None or yv is None:
                        iss.append("%s %s 单侧 None" % (k, f))
                    elif abs(float(xv) - float(yv)) > float(tol):
                        iss.append("%s %s %.10f vs %.10f" % (k, f, xv, yv))
            if iss:
                bad[b] = iss
        return bad

    def stats(self) -> Dict[str, int]:
        d = dict(LEDGER_STATS)
        d.update(self.ledger.stats())
        return d


# ================================================================
# CLI —— 手动 / 外部 cron 用
# ================================================================
# ⚠ 为什么不做成注册进 `auto/sched.py` 的定时任务：那要改禁改区，且预处理的
#   触发点与失败重试策略尚未定。先给一个**可独立调用**的入口，挂不挂、怎么挂
#   由人决定 —— 退出码非 0 表示当天有问题（便于 cron 告警）。

def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="G1 预处理日常推进（未接生产调度；手动或外部 cron 调用）")
    ap.add_argument("--date", help="目标交易日（默认库内最近交易日）")
    ap.add_argument("--backfill", action="store_true", help="补跑一段交易日")
    ap.add_argument("--start", help="补跑下界")
    ap.add_argument("--until", help="补跑上界")
    ap.add_argument("--max-days", type=int, help="补跑最多几天")
    ap.add_argument("--win", type=int, default=DEFAULT_WIN)
    ap.add_argument("--path", help="台账路径（默认 data/market_cn_cache/g1_state/）")
    ap.add_argument("--audit", type=int, default=0, metavar="N",
                    help="推进后抽样 N 票做全量对账")
    ap.add_argument("--no-save", action="store_true", help="演练：不落盘")
    ap.add_argument("--backfill-pool", action="store_true",
                    help="用全量路径一次性回填池历史（冷启动必做；非空需 --force）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已有池历史")
    ap.add_argument("--audit-pool", action="store_true",
                    help="台账源池 vs 全量池 逐日对账（日常巡检）")
    ap.add_argument("--tol", type=float, default=1e-9, help="池对账浮点容差")
    a = ap.parse_args(argv)

    if a.audit_pool:
        bad = G1DailyRunner(win=a.win, path=a.path).audit_pool(target=a.date, tol=a.tol)
        n = sum(len(v) for v in bad.values())
        print("[audit-pool] 不一致 %d 项" % n)
        for b, iss in bad.items():
            for it in iss[:5]:
                print("   %s: %s" % (b, it))
        return 1 if n else 0

    r = G1DailyRunner(win=a.win, path=a.path)
    if a.backfill_pool:
        n = r.backfill_pool(target=a.date, force=a.force)
        print("[backfill-pool] 写入 %d 天" % n)
        return 0
    save = not a.no_save
    if a.backfill:
        out = r.backfill(start=a.start, until=a.until, max_days=a.max_days, save=save)
        print("[backfill] %d 天: %s" % (len(out["days"]), out["days"][:3]))
        print("[backfill] G1 累计 %d, 跳过票 %d, errors %d"
              % (out["g1_total"], len(out["failed"]), len(out["errors"])))
        errors = out["errors"]
        reps = out["reps"]
    else:
        reps = [r.run(date=a.date, save=save)]
        errors = reps[0]["errors"]

    for rep in reps[-3:]:
        print("  %s advanced=%d missing=%d rebuilt=%d g1=%d skipped=%d err=%d"
              % (rep["date"], rep["advanced"], rep["missing"], len(rep["rebuilt"]),
                 len(rep["g1"]), len(rep["g1_skipped"]), len(rep["errors"])))
    for e in errors[:5]:
        print("  [error] %s" % e)

    if a.audit:
        bad = r.audit(limit=a.audit)
        print("[audit %d 票] 不一致 %d" % (a.audit, len(bad)))
        for c, iss in list(bad.items())[:5]:
            print("   %s: %s" % (c, iss[:2]))
        if bad:
            errors.append("audit 不一致 %d 票" % len(bad))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(_main())
