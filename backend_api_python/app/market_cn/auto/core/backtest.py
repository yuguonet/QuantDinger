#!/usr/bin/env python3
"""auto/backtest.py — 框架内全市场回测流水线 (薄编排: 全市场循环 → 取数 → 结算统计)

日线主路径 (daily_close 类): `run_all` 逐票调 `StrategyBase.backtest_stock`
(薄壳 → core.replay 事件流折叠), 与生产/调试/golden 同源 —— 编排层无规则
(门/去重/出场都在策略折叠契约内)。

设计点:
  - 数据走 hub.daily / fetch_klines_batch (同窗口+同 qfq+同 as-of, 已验证等价);
  - run_all_intraday 已是**薄适配器**（2026-10-09 偏离2）并已 **canonical 化**（P6-1）:
    委托 `IntradayFeed` + `core.replay` 折叠，直接产出 canonical trade（与日线 `run_all`
    同格式, `trade_map.build_trade`）—— 不再有第二套时间线编排, 也不再还原旧 trade 形状。

易错点:
  - 枚举终点 n-1: 最后一根无 D+1, 不能做 D0 (约定在策略侧循环内);
  - run 输出 trades 为 canonical 字段 (trade_map.build_trade), 与基线 JSON 对齐供逐笔对数。
"""
from __future__ import annotations

import os
import time


# ================================================================
# 全市场流水线 (编排层: 经注册表分发, 无策略名分支)
# ================================================================

def _run_meta(strat, days, start_date, end_date):
    """回测元信息 (2026-09-10 P2-2): 实验溯源用 — git 版本 + 实际生效参数 + 窗口。

    git_sha 取不到 (非 git 环境/无 git) 时为 "unknown", 绝不因溯源失败影响回测本身。
    """
    import subprocess
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=3, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        sha = "unknown"
    try:
        # 参数事实源 (2026-10-09 终态②/B-D3): 有 yaml 宏的策略 = **yaml params**
        # (判定/出场真正消费的那份); 无 yaml 的遗留策略退回 self.params()。
        # 原实现只读 self.params() ⇒ 对日线策略记录的是 py 常量, 与实际生效值脱节
        # (溯源元信息失真的静默陷阱)。
        try:
            from app.market_cn.auto.core.runtime.evaluate import load_strategy
            params = dict(load_strategy(strat.key).params)
        except Exception:
            params = strat.params(None)
    except Exception:
        params = {}
    return {"git_sha": sha, "strategy": strat.key, "days": days,
            "start_date": start_date, "end_date": end_date,
            "effective_params": params}


def run_all(strategy="dragon", days=300, codes=None, stock_info=None,
            use_prefilter=True, progress_every=500, start_date=None, end_date=None,
            probe=None, exec_engine=None):
    """全市场回测 (策略经注册表分发, 按 scan_spec.kind 选路径)。

    strategy: 任意已注册策略 key。
      - daily_close 类: 逐票调 ``StrategyBase.backtest_stock``（薄壳 → core.replay 折叠,
        与生产/调试/golden 同源）。``use_prefilter`` 与 ``probe`` 对此路径均为 no-op
        (U1~U4/门已内联在策略 evaluate; 采样已迁实盘侧 sampler.LiveSampler)。
      - intraday_window 类 (tail/knife): **薄适配器 → IntradayFeed + core.replay**
        (与实盘/展示同判定路径, 见 run_all_intraday)。
    probe: 调试探针 (probe.Probe, None=关闭)。仅 intraday_window 路径使用, daily_close 忽略。
    exec_engine (P4, 2026-09-21): **回测成交口径** (入口参数, 不污染 scan_spec.kind):
      - ``None`` / ``"daily"`` = 日线腿 (replay 零行为; 过渡钩子=旧枚举);
      - ``"intraday"`` / ``"auto"`` = 日线初筛 + **1m 真实腿精修出场** (分段式), 逐笔标注
        ``trade["exec_basis"] = "1m" | "daily"``; ``auto`` 额外限制入口日须在常量 ``DEFAULT_MINUTE_DAYS`` 个交易日内 (近端)。
    返回 {"trades": [...], "stats": {...}}; trades 直接可 json.dump 与基线对数。
    """
    from app.market_cn.auto import strategies as strat_reg

    strat_reg.autodiscover()
    strat = strat_reg.get_strategy(strategy)
    if strat is None:
        raise ValueError(f"strategy={strategy} 未注册 (可用: {sorted(strat_reg.all_strategies())})")

    if strat.scan_spec.kind == "intraday_window":
        res = run_all_intraday(strat, days=days, codes=codes,
                               start_date=start_date, end_date=end_date,
                               probe=probe)
        res["meta"] = _run_meta(strat, days, start_date, end_date)
        return res

    from app.market_cn.auto.core.data.hub import all_codes, daily
    from app.market_cn.auto.core.data.hub import stock_info as _hub_stock_info

    if codes is None:
        codes = all_codes()
    if stock_info is None:
        try:
            stock_info = _hub_stock_info()  # U1~U3 依赖 (缺失则跳过, 会放行)
        except Exception:
            stock_info = {}
    t0 = time.time()
    # 批量预取日线 (2026-09-20 提速): 逐票 hub.daily 是 N 次 DB 往返, 实测占全流程
    # ~85% (5223 票 days=300: 逐票中位 32.2s vs 批量 13.6s = 2.4x; 全流程 38s→19s)。
    # fetch_klines_batch 与逐票 fetch_kline_db **行内容逐字等价**(同窗口/同 qfq/同 as-of,
    # 抽检 80/80 逐字段一致), 故此处只换取数方式, 不改判定语义。
    # 批量未覆盖 (空/异常) 的票回落 hub.daily —— 单票失败不拖垮全市场循环, 与逐票行为一致。
    try:
        from app.market_cn.auto.core.data.kline import fetch_klines_batch
        _bars_batch = fetch_klines_batch(codes, days=days)
    except Exception:                                         # noqa: BLE001
        _bars_batch = {}                                     # 批量失败 → 全部回落逐票

    trades = []
    n_ok = 0
    # ---- 主路径 = 事件流折叠 (终态② Step 2, 2026-10-09): 统一走 backtest_stock(薄壳→replay) ----
    # 不再走 run_backtest（第二条判定+出场编排，Step 3 退役）。等价性:
    # run_backtest == backtest_stock(薄壳→replay) 已由 _break/_g56_equivalence 逐笔验证。
    # ⚠ probe 参数对 daily_close 已是 no-op（采样已迁 sampler.LiveSampler 实盘侧，
    #   回测侧 probe 分支是死代码 —— rule_stats/rule_audit 的 run_all(probe=) 已改道）。
    for k, code in enumerate(codes, 1):
        bars = _bars_batch.get(code) or daily(code, days)
        if not bars:
            continue
        trades.extend(strat.backtest_stock(
            bars, code,
            stock_info=stock_info.get(code) if stock_info else None,
            use_prefilter=use_prefilter) or [])
        n_ok += 1
        if progress_every and k % progress_every == 0:
            print(f"[{k}/{len(codes)}] trades={len(trades)} "
                  f"({time.time() - t0:.0f}s)", flush=True)

    out = {"trades": trades, "stats": _summary(trades), "codes_ok": n_ok,
           "elapsed": round(time.time() - t0, 1),
           "meta": _run_meta(strat, days, start_date, end_date)}
    out["engine"] = "replay"
    _eng = str(exec_engine or "daily").lower()
    if _eng in ("intraday", "auto") and trades:
        out["exec_engine"] = _eng
        out["exec_basis_counts"] = _refine_intraday(
            strat, trades, _bars_batch, daily, days=days,
            start_date=start_date, end_date=end_date, engine=_eng)
    return out


def _refine_intraday(strat, trades, bars_batch, daily_fn, *, days, start_date,
                     end_date, engine="auto"):
    """P4: 对日线枚举候选, **按候选窗口最小必要取 1m** 精修出场 (分段式)。

    纪律 (与设计文档 §4.3 / §3 原则 8 一致):
      - **逐笔只选一档** (``exec_basis`` = "1m" | "daily"): 持仓窗口内 1m 完整 → 走 1m 腿;
        否则**整笔**保留日线腿 —— 禁止半段 1m / 半段日线 (口径混合会污染阈值结论);
      - ``auto``: 入口日早于"最近 ``DEFAULT_MINUTE_DAYS`` 个交易日"起点 → 固定日线腿
        (即"近端 1m + 远端 1D"的分界, 常量单点定义);
      - 取数按**每票候选窗口的并集**一次取 (`window_minutes`), 绝不为几笔交易拉整窗 1m。

    返回 ``{"1m": n, "daily": n}`` (逐笔口径计数, 供报告标注)。
    """
    from app.market_cn.auto.core.data.frames import trading_dates
    from app.market_cn.auto.core.features.minute_composite import (
        DEFAULT_MINUTE_DAYS, window_minutes)
    from app.market_cn.auto.core.features.quality import grade_minute_window
    from app.market_cn.auto.strategies.base import StrategyBase as _SB

    counts = {"1m": 0, "daily": 0}
    if type(strat).intraday_replay is _SB.intraday_replay:
        for t in trades:                       # 策略未实现 1m 重放 → 整批日线腿 (显式标注)
            t["exec_basis"] = "daily"
        counts["daily"] = len(trades)
        return counts

    all_dates = trading_dates(days_back=days, end=end_date)
    if start_date:
        all_dates = [d for d in all_dates if d >= str(start_date)[:10]]
    boundary = None
    if all_dates and engine == "auto":
        boundary = all_dates[-int(DEFAULT_MINUTE_DAYS):][0]

    by_code = {}
    for t in trades:
        by_code.setdefault(t["code"], []).append(t)

    def _idx(bars, date_str):
        ds = str(date_str)[:10]
        return next((j for j, b in enumerate(bars) if str(b["time"])[:10] == ds), None)

    _HOLD_FLOOR, _TAIL_BUF = 20, 10            # 持有天数下限 / 顺延缓冲 (覆盖末日顺延)
    for code, ts in by_code.items():
        bars = bars_batch.get(code) or daily_fn(code, days)
        if not bars:
            for t in ts:
                t["exec_basis"] = "daily"
            counts["daily"] += len(ts)
            continue
        # 该票候选窗口并集 → 一次取数
        lo, hi = None, None
        for t in ts:
            ei = _idx(bars, t["entry_date"])
            if ei is None:
                continue
            hold = max(int(t.get("exit_day") or 0), _HOLD_FLOOR) + _TAIL_BUF
            end_i = min(ei + hold, len(bars) - 1)
            ed = str(t["entry_date"])[:10]
            lo = ed if lo is None or ed < lo else lo
            hi = bars[end_i]["time"][:10] if hi is None or bars[end_i]["time"][:10] > hi \
                else hi
        if lo is None or (boundary is not None and lo < boundary):
            for t in ts:
                t["exec_basis"] = "daily"
            counts["daily"] += len(ts)
            continue
        mbd = window_minutes(code, lo, hi)
        if not mbd:
            for t in ts:
                t["exec_basis"] = "daily"
            counts["daily"] += len(ts)
            continue

        for t in ts:
            ei = _idx(bars, t["entry_date"])
            if ei is None:
                t["exec_basis"] = "daily"
                counts["daily"] += 1
                continue
            hold = max(int(t.get("exit_day") or 0), _HOLD_FLOOR) + _TAIL_BUF
            win = [b["time"][:10] for b in bars[ei:min(ei + hold, len(bars))]]
            if not win or any(d not in mbd for d in win):
                t["exec_basis"] = "daily"      # 窗口内有日缺 1m → 整笔回落 (不混合口径)
                counts["daily"] += 1
                continue
            # 2026-10-07: 补传 `entry_gate` —— break 的 intraday_replay 靠它区分
            #   核心/高板通道的甜点阈值 (100) 与其余 (95); 不传 ⇒ 恒取 95 ⇒ 1m 腿与
            #   日线腿出场点分叉。(`params` 本路径不可得, 由策略侧回退 BOARD_PARAMS。)
            # 2026-10-07: 入场价改取该笔记录的 `t["entry_price"]` —— 原写 `bars[ei]["open"]`
            #   ⇒ close 模式入场 (如 dragon D0 收盘买) 的票在 1m 腿上被**静默改锚到当日
            #   开盘价**, 与日线腿同一笔的入场价不一致 (收益不可比, peak/止损阈值全部
            #   按错基准计算)。1m 腿只是"出场重放", 入场价必须与日线腿逐笔同一。
            res = strat.intraday_replay(bars, ei, float(t["entry_price"]), code=code,
                                        board_type=None, minute_by_date=mbd,
                                        entry_gate=t.get("entry_gate"))
            if not res:
                t["exec_basis"] = "daily"
                counts["daily"] += 1
                continue
            for k in ("exit_price", "exit_day", "return_pct", "peak_return_pct"):
                if res.get(k) is not None:
                    t[k] = res[k]
            t["exec_basis"] = "1m"
            counts["1m"] += 1
            # P5: 1m 窗口数据完整度标注 — 只在该笔 1m 腿写入, 日线腿保持无标记
            # (语义: 1m 腿才有"分钟数据是否完整"问题, 日线腿不评估)。
            win_bars: list = []
            touched_set: set = set()
            for d in win:
                arr = mbd.get(d) or []
                if not arr:
                    continue
                base = len(win_bars)
                win_bars.extend(arr)
                if d == str(t.get("entry_date", ""))[:10]:
                    touched_set.add(base)
            if win_bars:
                from app.market_cn.auto.core.market import get_board_type as _gbt
                _bt = _gbt(code, strat.market_spec) if hasattr(strat, "market_spec") else "default"
                g = grade_minute_window(win_bars, board_type=_bt,
                                        touched=sorted(touched_set))
                t["data_quality"] = g.get("level", "ok")
                t["data_quality_reasons"] = g.get("reasons", [])
    return counts


# ================================================================
# 盘中回测辅助 (intraday_window: 成交槽展开 / 薄适配器)
# ================================================================

def _exec_trigger_mis(spec):
    """ScanSpec → 成交触发槽位列表。

    扫描窗口 = ``[entry_at 或 windows[0], windows[1]]``, 按 ``interval_sec`` 逐槽展开;
    窗口内**首个触发槽**即成交 —— 与生产 ``scan._scan_cycle`` 的 rolling_preview 同语义
    (每分钟一轮, "14:50 起触发即买入", 先到先得)。

    ⚠️ 易错点 (2026-10-09 D4 修正): 原实现对 ``entry_at`` 非空的策略**只回该单个槽位**
    (旧措辞 "终审语义: 只回该时刻")。这与生产口径矛盾 —— ``entry_at`` 是 **最早可成交
    时刻** (窗口起点), 非"唯一成交时刻" (见 ScanSpec 注释 + tail 2026-09-26 "14:50 起即可
    买入")。只判单点会**系统性漏掉 entry_at 之后才触发的信号**: 实证 tail 000993 于 14:58
    才触发, 折叠/生产均命中而时间线引擎漏计 ⇒ 回测少计信号。现统一为"整窗逐槽扫描取首个
    触发", 与折叠 ``evaluate`` 的 ``[min_hhmm, 15:00]`` 回扫同构。
    """
    from app.market_cn.auto.core.data.frames import hhmm_to_pos
    from app.market_cn.auto.sched import expand_times
    if not spec.windows or len(spec.windows) < 2:
        return []
    first = spec.windows[0]
    if spec.entry_at and spec.entry_at > first:
        first = spec.entry_at          # entry_at = 最早可成交时刻 (不早于窗口起点)
    if first > spec.windows[1]:
        return []                      # entry_at 晚于窗口终点 ⇒ 无成交槽
    mis = []
    for t in expand_times((first, spec.windows[1]), spec.interval_sec):
        mi = hhmm_to_pos(t)
        if mi >= 0 and mi not in mis:
            mis.append(mi)
    return sorted(mis)


# ================================================================
# 盘中回测 —— 主干折叠 (intraday_window: 委托 IntradayFeed + core.replay, 产 canonical trade)
# ================================================================

def run_all_intraday(strat, days=120, codes=None, start_date=None, end_date=None,
                     progress_every=1, probe=None):
    """intraday_window 策略全市场回测 —— 主干折叠（委托 IntradayFeed + core.replay）。

    2026-10-09 偏离2: 旧独立时间线引擎已退役。P6-1: 旧 trade 形状还原层
    （``_ReadyBag`` / ``_legacy_intraday_trade`` / ``trigger`` 载荷槽 /
    ``exit_reason="d1_open"`` 文案）一并删除 —— 本函数直接产出 **canonical trade**
    （``trade_map.build_trade`` 字段，与日线 ``run_all`` 同格式）。
    判定/入场/出场全在策略 ``evaluate``（与展示/实时/生产同源，单主干）。

    窗口语义: 折叠从「窗口起点前一交易日」起（避免首日 as-of 含当日=未来函数），
    向后多留 ``_SETTLE_BUF`` 个交易日供 D1 出场结算，最后按 ``d0_date∈窗口`` 收口
    （等价旧引擎「只在窗口日入场」）。

    市场门 (knife 的 ``kc_mkt``): **逐槽 as-of** —— ``load_market_slots(lo_d, hi_d)``
      逐日建全市场分钟帧、按槽取横截面均涨幅，与生产 ``scan._mkt_gain`` 同口径
      （消除「用当日收盘门控 14:56 入场」的前视）；缺该日则回退日频 ``mkt_map``
      （旧口径，仅兜底）。见 ``docs/市场门口径评估_20261009.md``。
      成本: 每窗口交易日 1 次全市场建帧（npz 缓存），随窗口长度线性增长。

    probe: 兼容旧签名（旧引擎 DayTrace 采样）；折叠路径采样改走 ``TraceCollector`` /
      ``GateDebugCollector``（见 core/replay），本参数不再消费（保留=零破坏）。
    """
    from app.market_cn.auto.core.data import frames as fr
    from app.market_cn.auto.core.data.hub import all_codes, daily
    from app.market_cn.auto.core.replay import TradesCollector, market_gain, replay
    from app.market_cn.auto.core.replay.intraday import IntradayFeed

    _SEED_BARS = 10        # 折叠起点: 窗口起点前 N 根日线 (供 init_state/as-of 切片)
    _SETTLE_BUF = 15       # 窗口终点后 N 个交易日 (供 D1 出场结算; 覆盖长假)

    t0 = time.time()
    all_dates = fr.trading_dates(days_back=days, end=end_date)
    first_1m = fr.first_1m_date()
    dates = [d for d in all_dates if d >= first_1m] if first_1m else list(all_dates)
    if start_date:
        dates = [d for d in dates if d >= str(start_date)[:10]]
    if not dates:
        return {"trades": [], "stats": _summary([]), "codes_ok": 0, "elapsed": 0}
    lo_d, hi_d = dates[0], dates[-1]
    code_list = list(codes) if codes else all_codes()

    # 日线 (窗口 + 历史; 对齐旧引擎 `_daily_asof` 的 daily(code, 300))
    bars_by_code = {}
    for c in code_list:
        try:
            b = daily(c, 300)
        except Exception:                               # noqa: BLE001
            b = None
        if b:
            bars_by_code[c] = b
    # 市场门 (knife 的 mkt_gate) —— **两层**（2026-10-09 评估后收口）:
    #   ① **逐槽 as-of（正解）** = `load_market_slots`: 每个决策槽读**当时**的全市场
    #      横截面（与生产 `scan._mkt_gain(snaps)` / `MinuteFrame.mkt_gain` 同口径）——
    #      消除「用当日收盘门控 14:56 入场」的单向**前视**（见 docs/市场门口径评估_20261009.md）。
    #   ② **日频 close 横截面（回退）** = `mkt_map`: 仅当 ① 缺该日（取数失败/无分钟帧）时
    #      回落 —— 与旧口径一致，但保留前视（只作兜底，勿再当主源）。
    # ⚠ mkt_map 必须是**全市场**横截面 —— 票池为子集时按子集算会因样本不足 (<min_n)
    #   让 mkt_gain 缺失 ⇒ knife 的 kc_mkt 门 fail-closed ⇒ 静默零 trade (实测 36 票子集复现)。
    from app.market_cn.auto.core.replay import load_market_gain
    from app.market_cn.auto.core.replay.mkt_slots import load_market_slots
    mkt_map = market_gain(bars_by_code) if not codes else load_market_gain(lo_d, hi_d)
    mkt_slots = load_market_slots(lo_d, hi_d)

    trades, n_ok = [], 0
    for i, code in enumerate(code_list, 1):
        bars = bars_by_code.get(code)
        if not bars:
            continue
        idx = {str(b["time"])[:10]: j for j, b in enumerate(bars)}
        win_pos = [j for d, j in idx.items() if lo_d <= d <= hi_d]
        if not win_pos:
            continue
        lo = max(min(win_pos) - _SEED_BARS, 0)
        hi = min(max(win_pos) + _SETTLE_BUF, len(bars) - 1)
        feed = IntradayFeed(bars, code, lo=lo, hi=hi,
                            mkt_map=mkt_map, mkt_slots=mkt_slots)
        coll = TradesCollector(code, strat.key, exec_basis="1m")
        res = replay(strat, code, feed, collectors=[coll])
        for t in (res.trades or []):
            d0 = str(t.get("d0_date") or "")[:10]
            if not (lo_d <= d0 <= hi_d):                # 只收口窗口内入场 (对齐旧引擎)
                continue
            trades.append(t)
        n_ok += 1
        if progress_every and i % progress_every == 0:
            print(f"[{i}/{len(code_list)}] trades={len(trades)} ({time.time() - t0:.0f}s)",
                  flush=True)
    return {"trades": trades, "stats": _summary(trades), "codes_ok": n_ok,
            "elapsed": round(time.time() - t0, 1), "engine": "replay"}


def frames_hhmm(mi):
    from app.market_cn.auto.core.data.frames import MI_HHMM
    return MI_HHMM[mi] if 0 <= mi < len(MI_HHMM) else ""


def _summary(trades):
    """标准报告 (对齐设计文档 §6.1 + 五高指标映射, 09-09 B-5)。

    字段: 笔数/胜率/均收/盈亏比/收益五分桶/20日峰值分布与均值/月均笔数(可操作性)/
         日均收益(单位时间收益比=均收益÷持有交易日)/前后两段分段稳定性。
    """
    if not trades:
        return {"n": 0}
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    buckets = {"≤-10": 0, "-10~-3": 0, "-3~+3": 0, "+3~+10": 0, ">+10": 0}
    for r in rets:
        k = "≤-10" if r <= -10 else "-10~-3" if r <= -3 else \
            "-3~+3" if r < 3 else "+3~+10" if r < 10 else ">+10"
        buckets[k] += 1
    peaks = [t["peak_return_pct"] for t in trades if t.get("peak_return_pct") is not None]
    months = sorted({str(t.get("entry_date", ""))[:7] for t in trades} - {""})
    n_h = len(trades) // 2
    seg = lambda ts: round(sum(1 for t in ts if t["return_pct"] > 0) / len(ts) * 100, 1) if ts else None
    hold_days_avg = sum(t.get("exit_day") or 0 for t in trades) / len(trades)
    return {
        "n": len(trades),
        "winrate": round(len(wins) / len(trades) * 100, 1),
        "avg_ret": round(sum(rets) / len(rets), 2),
        "pl_ratio": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2)
        if wins and losses else None,
        "ret_buckets": buckets,
        "peak": {"mean": round(sum(peaks) / len(peaks), 2) if peaks else None,
                 "lt10": sum(1 for p in peaks if p < 10),
                 "10_20": sum(1 for p in peaks if 10 <= p < 20),
                 "ge20": sum(1 for p in peaks if p >= 20)},
        "monthly_avg": round(len(trades) / len(months), 1) if months else None,
        "ret_per_day": round(sum(rets) / len(rets) / hold_days_avg, 3) if hold_days_avg else None,
        "winrate_1st_half": seg(trades[:n_h]),
        "winrate_2nd_half": seg(trades[n_h:]),
    }


if __name__ == "__main__":
    import argparse
    import json
    import os

    # CLI 直跑时需自行加载 .env (服务进程已由应用加载, 重复加载无害)
    from app.market_cn.auto.core._paths import load_env_first_found
    load_env_first_found(os.path.join(os.getcwd(), ".env"))

    parser = argparse.ArgumentParser(description="框架内全市场回测流水线 (策略经注册表分发)")
    parser.add_argument("--strategy", default="dragon",
                        help="任意已注册策略 key (dragon/v1/break/...)")
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--codes", default="", help="逗号分隔, 空则全市场")
    parser.add_argument("--start-date", default="", help="窗口起点 (盘中策略精确复现用)")
    parser.add_argument("--end-date", default="", help="窗口终点 (默认今天)")
    parser.add_argument("--out", default="", help="结果JSON输出路径 (对数用)")
    parser.add_argument("--probe", default="",
                        help="开启调试探针并存档 (值=tag; 数据落 tmp/probes/, 供 AI 离线分析)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    probe = None
    if args.probe:
        from app.market_cn.auto.probe import Probe
        probe = Probe(args.strategy, tag=args.probe)
    res = run_all(strategy=args.strategy, days=args.days, codes=codes,
                  start_date=args.start_date or None, end_date=args.end_date or None,
                  probe=probe)
    if probe is not None:
        probe.close()
    print("统计:", res["stats"], "| codes_ok:", res["codes_ok"],
          "| 耗时:", res["elapsed"], "s")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res["trades"], f, ensure_ascii=False)
        # meta sidecar (P2-2): git 版本+生效参数+窗口, 溯源用; --out 本体保持纯 trades 列表
        # (不破坏既有对账脚本对纯列表的假设)
        with open(args.out + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(res.get("meta", {}), f, ensure_ascii=False, indent=2)
        print("已写出:", args.out, "| meta:", args.out + ".meta.json")
