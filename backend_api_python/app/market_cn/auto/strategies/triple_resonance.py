#!/usr/bin/env python3
"""strategies/triple_resonance.py — 三金叉共振策略 (2026-09-13 网上调研落地)

规则来源 (2026-09-13 网络调研, 多来源口径一致的经典"三金叉共振"/"三线金叉"):
  均线金叉 + 均量线金叉 + MACD 金叉三者在 3~5 个交易日内共振出现, 是市场由弱转强
  的启动信号 (央视财经"三金叉见底"/腾讯 ima"三线金叉选股"/新浪"MACD+KDJ+均线共振")。

本策略落地版 (D0 收盘可知 → D1 开盘买, 与全项目 daily 类口径一致):
  三金叉 (近 resonance_win 日内各发生一次, 且 D0 当日三线均保持金叉后多头状态):
    ① 均线金叉: MA5 上穿 MA10
    ② 均量金叉: VMA5 上穿 VMA10
    ③ MACD 金叉: DIF 上穿 DEA
  D0 确认 (当日必须成立):
    - 收盘 > close_above_ma 期均线 (默认 MA10, 0=关)
    - 当日量 ≥ VMA5 × vol_mult (默认 1.1, 放量确认)
    - require_align (默认 False): MA5>MA10>MA20 多头排列 (严格趋势版, 见底期常不满足)
    - macd_above_zero (默认 False): DIF>0 才算有效 (0 轴上方金叉更强, 消融开关)
  出场: 止损 stop / 追踪止损 trail (自峰值, low 触及+跳空开盘成交) / 到期 hold 天
        — 复用 v1 通用出场引擎 _run_backtest (is_v1=False, T+1/跌停顺延语义)。

设计点:
  - 指标全序列 O(n) 预计算 (_calc_ind), backtest_stock 逐日 O(win) 判定 — 避免
    dragon 当年 426s→48s 那种逐日重算 MACD 的平方复杂度坑;
  - 实盘 scan_signals 与回测 backtest_stock 共享同一判定核心 _signal_core
    (两路径同一规则函数, 不是两份代码);
  - 门级归因 (2026-09-13): _signal_core_dbg 全门求值版产出 gates 门向量,
    供 tools/rule_audit.py 做"每门拦截比例/通过组 vs 被拦组统计/换序稳定性";
    fail 日也采样走 _probe_day_light 轻量行 (labels+gates, 无 win 窗口——
    全市场 140 万行时 win 数组占 90% 体积, 1.6GB@92万行实证);
  - 峰值口径 (2026-09-13 用户裁定, 让"大肉被拦了多少"可见): peak_exit=
    出场模拟为准 (stop/trail/hold, v1 引擎, D1 当日 high 起追踪);
    peak60/120/180 = 不知道出场位置时的粗估上限 (短线60/中线120/长线180 日
    最高, **D1 入场日起算**, 旧 peak5 D2 起漏当日冲高); 滑窗 O(n) 预计算;
    主路径 _signal_core 短路版逐字保留 (速度/等价性优先), 两版判定等价由
    tmp/_dbg_equiv.py 单测锁定 — 改规则必须两处同步并重跑单测;
  - 参数一律 self.merged_params(None) 取 (2026-09-13 接线纪律: 禁硬编码 kwargs
    压过实例覆写, param_scan 实例覆写即生效);
  - 未加 config.json 段: 不进实盘 schedule (只挂 dragon_scan/knife_scan/dragon_monitor),
    autodiscover 注册仅供回测; 回测基准=代码默认值。

易错点:
  - SMA 前 k-1 日为 None, _cross_within/_signal_core 全程防 None (上市不足期数);
  - 金叉窗口判定用"上穿发生过"而非"当前多头" — 当前多头只是前置必要条件,
    否则金叉日距今超过 win 的旧信号会被误放;
  - require_align=True 与"三金叉见底"语义冲突 (见底时 MA20 常压在上方), 仅作消融;
  - MACD 用 common.indicators.calc_macd (EMA12/26/9), 与 v1 同源 — 勿另写算法;
  - _signal_core_dbg 与 _signal_core 判定等价: dbg 版 fail 后不短路继续求值
    剩余门 (门向量是换序分析的前提), 判定结果仍 = AND 全同 — 单测锁定。
"""
from __future__ import annotations

from app.market_cn.auto.common.indicators import calc_macd
from app.market_cn.auto.common.market import get_board_type
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "triple_resonance"
STRATEGY_LABEL = "三金叉"

PARAMS = {
    "resonance_win": 3,       # 三金叉共振窗口 (交易日, 近 win 日内各发生一次上穿)
    "close_above_ma": 10,     # D0 收盘须站上的均线期数 (0=关)
    "vol_mult": 1.1,          # D0 量 / VMA5 下限 (放量确认)
    "require_align": False,   # MA5>MA10>MA20 多头排列 (严格趋势版开关)
    "macd_above_zero": False, # DIF>0 才算有效金叉 (0 轴上方开关)
    # 出场 (v1 通用引擎语义)
    "stop": -8.0,
    "trail": -5.0,
    "hold": 7,
}


def _sma(vals, k):
    """滚动简单均线 O(n); 前 k-1 日为 None。"""
    out = [None] * len(vals)
    s = 0.0
    for j, v in enumerate(vals):
        s += v
        if j >= k:
            s -= vals[j - k]
        if j >= k - 1:
            out[j] = s / k
    return out


def _calc_ind(bars, gate_k=0):
    """全序列指标预计算 (一次 O(n)): ma5/10/20, vma5/10, dif/dea, gate(可选)。"""
    closes = [float(b["close"]) for b in bars]
    vols = [float(b["volume"]) for b in bars]
    dif, dea, _hist = calc_macd(closes)
    return {
        "ma5": _sma(closes, 5), "ma10": _sma(closes, 10), "ma20": _sma(closes, 20),
        "vma5": _sma(vols, 5), "vma10": _sma(vols, 10),
        "dif": dif, "dea": dea,
        "gate": _sma(closes, gate_k) if gate_k else None,
    }


def _cross_within(fast, slow, i, win):
    """近 win 日 (j∈[i-win+1, i]) 内 fast 上穿 slow 发生过 → 金叉日索引, 否则 None。

    防 None (SMA 前段/MACD 热身期); f1>s1 且 f0<=s0 严格上穿。
    """
    for j in range(max(1, i - win + 1), i + 1):
        f0, s0, f1, s1 = fast[j - 1], slow[j - 1], fast[j], slow[j]
        if None not in (f0, s0, f1, s1) and f1 > s1 and f0 <= s0:
            return j
    return None


def _signal_core(bars, i, p, ind):
    """三金叉共振判定 @ 第 i 根 (D0) → 信号 dict (trades/Signal.extra 共享形态) 或 None。

    主路径 (实盘/回测): 逐门短路 — 零开销。
    """
    ma5, ma10, ma20 = ind["ma5"], ind["ma10"], ind["ma20"]
    vma5, vma10, dif, dea = ind["vma5"], ind["vma10"], ind["dif"], ind["dea"]
    if i < 1:
        return None
    c = float(bars[i]["close"])
    v = float(bars[i]["volume"])

    # 前置必要: D0 三线均为金叉后多头状态 (上穿判定在下面逐叉做)
    if ma5[i] is None or ma10[i] is None or ma5[i] <= ma10[i]:
        return None
    if vma5[i] is None or vma10[i] is None or vma5[i] <= vma10[i]:
        return None
    if dif[i] is None or dea[i] is None or dif[i] <= dea[i]:
        return None

    # 三金叉: 近 win 日内各发生一次上穿
    win = int(p["resonance_win"])
    j_ma = _cross_within(ma5, ma10, i, win)
    if j_ma is None:
        return None
    j_vol = _cross_within(vma5, vma10, i, win)
    if j_vol is None:
        return None
    j_macd = _cross_within(dif, dea, i, win)
    if j_macd is None:
        return None

    # D0 确认: 收盘站上均线
    gate = ind.get("gate")
    if gate is not None and (gate[i] is None or c <= gate[i]):
        return None

    # D0 确认: 放量
    vol_r = v / vma5[i] if vma5[i] else 0.0
    if vol_r < p["vol_mult"]:
        return None

    # 消融开关: 多头排列 / 0 轴上方
    if p["require_align"] and (ma20[i] is None or not (ma5[i] > ma10[i] > ma20[i])):
        return None
    if p["macd_above_zero"] and dif[i] <= 0:
        return None

    return {
        "path": "triple_resonance", "path_label": "三金叉",
        "d0_date": bars[i]["time"],
        "d0_close": round(c, 3),
        "vol_r": round(vol_r, 2),
        "j_ma": i - j_ma, "j_vol": i - j_vol, "j_macd": i - j_macd,  # 各金叉距 D0 天数
        "dif": round(dif[i], 4), "dea": round(dea[i], 4),
        "buy_mode": "next_open",
    }


def _signal_core_dbg(bars, i, p, ind):
    """全门求值版 (仅离线归因用, tools/rule_audit.py 数据源): → (sig, gates)。

    gates = {门名: True/False/"off"} 门向量 — "off"=开关关闭, 不参与拦截;
    fail 后不短路, 继续求值剩余门 (门向量是"换序排列统计"的前提);
    判定结果与 _signal_core 等价 (AND 门全同, 无顺序耦合), 由 tmp/_dbg_equiv.py 锁定。
    """
    ma5, ma10, ma20 = ind["ma5"], ind["ma10"], ind["ma20"]
    vma5, vma10, dif, dea = ind["vma5"], ind["vma10"], ind["dif"], ind["dea"]
    if i < 1:
        return None, {}
    c = float(bars[i]["close"])
    v = float(bars[i]["volume"])

    g = {}
    g["ma_pre"] = bool(ma5[i] is not None and ma10[i] is not None and ma5[i] > ma10[i])
    g["vol_pre"] = bool(vma5[i] is not None and vma10[i] is not None
                        and vma5[i] > vma10[i])
    g["macd_pre"] = bool(dif[i] is not None and dea[i] is not None and dif[i] > dea[i])

    win = int(p["resonance_win"])
    j_ma = _cross_within(ma5, ma10, i, win)
    j_vol = _cross_within(vma5, vma10, i, win)
    j_macd = _cross_within(dif, dea, i, win)
    g["cross_ma"] = j_ma is not None
    g["cross_vol"] = j_vol is not None
    g["cross_macd"] = j_macd is not None

    gate = ind.get("gate")
    g["gate_ma"] = "off" if gate is None else bool(gate[i] is not None and c > gate[i])

    vol_r = v / vma5[i] if vma5[i] else 0.0
    g["vol_mult"] = bool(vol_r >= p["vol_mult"])

    g["align"] = "off" if not p["require_align"] else bool(
        ma20[i] is not None and ma5[i] > ma10[i] > ma20[i])
    g["zero"] = "off" if not p["macd_above_zero"] else bool(dif[i] > 0)

    if not all(val is not False for val in g.values()):
        return None, g
    return {
        "path": "triple_resonance", "path_label": "三金叉",
        "d0_date": bars[i]["time"],
        "d0_close": round(c, 3),
        "vol_r": round(vol_r, 2),
        "j_ma": i - j_ma, "j_vol": i - j_vol, "j_macd": i - j_macd,
        "dif": round(dif[i], 4), "dea": round(dea[i], 4),
        "buy_mode": "next_open",
    }, g


def _win_extreme(bars, k, mode="max"):
    """[i+1, i+k] 窗口极值对 entry=D1 开盘 的百分比; 视野不足 (i+k 越界) = None。

    滑窗单调队列 O(n) — peak5/60/120/180 (mode=max, high) 与 mae5 (mode=min, low)
    共用; 旧 peak5 从 D2 起漏 D1 当日冲高, 本函数一律从 D1 (入场日) 起。
    """
    from collections import deque
    n = len(bars)
    out = [None] * n
    if n < 2:
        return out
    vals = [float(b["high"] if mode == "max" else b["low"]) for b in bars]
    opens = [float(b["open"]) for b in bars]
    dq = deque()
    for r in range(1, n):
        while dq and dq[0] < r - k + 1:
            dq.popleft()
        if mode == "max":
            while dq and vals[dq[-1]] <= vals[r]:
                dq.pop()
        else:
            while dq and vals[dq[-1]] >= vals[r]:
                dq.pop()
        dq.append(r)
        i = r - k                    # 该右端点服务的行 (窗口 [i+1, i+k])
        if 1 <= i < n - 1:
            entry = opens[i + 1]     # D1 开盘
            if entry > 0:
                out[i] = round((vals[dq[0]] / entry - 1) * 100, 2)
    return out


def _slope_feats(bars, ind, i):
    """D0 可知斜率特征 (2026-09-13 用户: 金叉须有斜率要求; 判别力实测用)。

    仅 probe 链路消费 (extra.sl), 不参与判定 — 主路径/门向量/等价性锁定零影响。
    全部 D0 收盘可知, 按价格归一:
      ma_sl3  = MA5 三日斜率 % — 金叉陡峭度 (走平粘滞叉 vs 陡升叉);
      dif_sl3 = DIF 三日增量/D0收盘*100 — MACD 金叉力度;
      gap_sl3 = (MA5-MA10) 喇叭口三日扩张量/D0收盘*100 — 快慢线开口速度。
    视野不足缺键 (warmup 保护)。
    """
    out = {}
    ma5, ma10, dif = ind["ma5"], ind["ma10"], ind["dif"]
    c = float(bars[i]["close"])
    if c <= 0 or i < 3:
        return out
    if ma5[i] is not None and ma5[i - 3] is not None and ma5[i - 3] > 0:
        out["ma_sl3"] = round((ma5[i] / ma5[i - 3] - 1) * 100, 3)
    if dif[i] is not None and dif[i - 3] is not None:
        out["dif_sl3"] = round((dif[i] - dif[i - 3]) / c * 100, 4)
    if (ma5[i] is not None and ma10[i] is not None
            and ma5[i - 3] is not None and ma10[i - 3] is not None):
        out["gap_sl3"] = round(((ma5[i] - ma10[i]) - (ma5[i - 3] - ma10[i - 3]))
                               / c * 100, 4)
    return out


@register
class TripleResonanceStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"        # D0 即信号日, U1~U4 锚定信号日评估
    entry_style = "tres"
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(PARAMS)
    # 门级归因 (tools/rule_audit.py): RULE_DEFS 序 = 判定短路序 = 基准序;
    # PROBE_STAGE_RANK 供 day-stage 归属 (rank 越大越接近信号)。
    RULE_DEFS = [
        ("ma_pre",     "前置: MA5 > MA10 (D0 金叉后多头状态)"),
        ("vol_pre",    "前置: VMA5 > VMA10"),
        ("macd_pre",   "前置: DIF > DEA"),
        ("cross_ma",   "均线金叉: 近 resonance_win 日内 MA5 上穿 MA10"),
        ("cross_vol",  "均量金叉: 近 win 日内 VMA5 上穿 VMA10"),
        ("cross_macd", "MACD金叉: 近 win 日内 DIF 上穿 DEA"),
        ("gate_ma",    "D0 收盘 > close_above_ma 期均线 (0=off)"),
        ("vol_mult",   "D0 量 ≥ VMA5 × vol_mult (放量确认)"),
        ("align",      "MA5>MA10>MA20 多头排列 (require_align 开关, 默认 off)"),
        ("zero",       "DIF > 0 0轴上方金叉 (macd_above_zero 开关, 默认 off)"),
    ]
    PROBE_STAGE_RANK = {
        "ma_pre": 1, "vol_pre": 2, "macd_pre": 3,
        "cross_ma": 4, "cross_vol": 5, "cross_macd": 6,
        "gate_ma": 7, "vol_mult": 8, "align": 9, "zero": 10,
        "prefilter": 11, "engine_skip": 12, "signal": 13,
    }

    # ---- 信号判定 (实盘口径: 单股单日现算指标) ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        """D0 三金叉共振 → Signal (至多1笔)。as_of=k: 只用 bars[:k+1]。"""
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        if len(bars) < 35:
            return []
        i = len(bars) - 1
        ind = _calc_ind(bars, gate_k=int(p["close_above_ma"]))
        sig = _signal_core(bars, i, p, ind)
        if not sig:
            return []
        return [Signal(
            code=code,
            time=bars[i]["time"],
            score=int(min(99, max(0, sig["vol_r"] * 10))),   # 量比映射 (与 dragon 后处理口径同量级)
            price=sig["d0_close"],
            label=STRATEGY_LABEL,
            extra=sig,
        )]

    # ---- D1 竞价处置 (无 gap 过滤: 开盘可买, 与"共振次日跟进"语义一致) ----
    def entry_decision(self, row, snap=None, **params):
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        return EntryDecision(True, "三金叉次日开盘可买")

    # ---- 15:00 收盘确认 (无确认步骤: D1 开盘已买入, D1 收盘仅记录) ----
    def confirm_decision(self, row, snap=None, **params):
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        prev_close = float(row.get("signal_price") or 0)
        if prev_close <= 0:
            return None
        d1_chg = (float(series[-1]["last"] or 0) / prev_close - 1) * 100
        return ConfirmDecision(True, "ok", d1_chg=round(d1_chg, 2),
                               detail={"confirm": "ok", "confirm_strong": False})

    def initial_stop(self, code, entry_price):
        """按 stop 参数 (default -8%)。"""
        return round(entry_price * (1 + self.merged_params()["stop"] / 100), 3)

    # ---- 出场判定 (monitor live 语义: 收盘口径, 与回测引擎同参数) ----
    def exit_decision(self, row, snap=None, **params):
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        p = self.merged_params(None)
        stop, trail, hold = p["stop"], p["trail"], int(p["hold"])
        today_idx = len(bars) - 1
        held = today_idx - entry_idx + 1
        entry_seg = bars[entry_idx:today_idx + 1]
        peak = max(float(b["high"]) for b in entry_seg)
        last_bar = bars[-1]
        ret = (last_bar["close"] / entry_price - 1) * 100
        ret_from_high = (last_bar["close"] / peak - 1) * 100 if peak > 0 else 0
        if ret <= stop:
            return ExitDecision("exit", reason=f"止损{stop}%", price=entry_price * (1 + stop / 100))
        if ret_from_high <= trail and ret > 0:
            return ExitDecision("exit", reason=f"追踪止损{trail}%", price=peak * (1 + trail / 100))
        if held >= hold:
            return ExitDecision("exit", reason=f"持仓到期{hold}天", price=float(last_bar["close"]))
        return ExitDecision("hold")

    def _probe_day_light(self, probe, bars, i, code, stock_info, stage,
                         gates=None, sig=None, extra=None, peak_maps=None):
        """门级审计轻量采样 (2026-09-13): labels 自算 (前瞻假设收益, 判别力统计用),
        丢 30 日 win 窗口特征 — 全市场 fail 日也采样时 ~140 万行, win 数组占 90%
        体积 (1.6GB@92万行实证), rule_audit 不消费。
        框架 _probe_day 的 rule_trace 亦省略 (本策略门信息全在 gates 向量)。

        峰值口径 (2026-09-13 用户裁定):
          peak_exit  = 出场模拟为准 (stop/trail/hold, v1 引擎, D1 当日 high 起追踪)
                       — 知道出场位置时以它为准;
          peak5/60/120/180 = 不知道出场位置时的粗估上限 (短线60/中线120/长线180
                       日最高, D1 入场日起算); 视野不足不记 = censored;
          mae5       = D1 起 5 日窗口最低 (对称口径);
          ret_d1c/ret_d5o/ret_d10o = sample_feats 同式 (entry=D1 开盘)。
        peak_maps: {k: _win_extreme 结果数组} backtest 循环前一次预计算; None=现场算
          (慢, 仅单日 scan 场景)。
        """
        n = len(bars)
        labels = {}
        if i + 1 < n:
            entry = float(bars[i + 1]["open"] or 0)
            if entry > 0:
                labels["ret_d1c"] = round(
                    (float(bars[i + 1]["close"]) / entry - 1) * 100, 2)
                if i + 6 < n:
                    labels["ret_d5o"] = round(
                        (float(bars[i + 6]["open"]) / entry - 1) * 100, 2)
                if i + 11 < n:
                    labels["ret_d10o"] = round(
                        (float(bars[i + 11]["open"]) / entry - 1) * 100, 2)
                for k in (5, 60, 120, 180):
                    arr = (peak_maps or {}).get(k)
                    if arr is not None:
                        v = arr[i]
                    else:
                        j_end = min(i + k, n - 1)
                        v = (round((max(float(bars[j]["high"])
                                        for j in range(i + 1, j_end + 1))
                                    / entry - 1) * 100, 2)
                             if j_end >= i + 1 else None)
                    if v is not None:
                        labels[f"peak{k}"] = v
                mae_arr = (peak_maps or {}).get("mae5")
                if mae_arr is not None and mae_arr[i] is not None:
                    labels["mae5"] = mae_arr[i]
                # 出场模拟 (知道出场位置以出场为准; fail 行=假设 D1 开盘入场的模拟)
                from app.market_cn.auto.strategies.v1 import _run_backtest as _run_exit
                p = self.merged_params(None)
                bt = _run_exit(bars, i + 1, entry, int(p["hold"]), p["stop"],
                               p["trail"], get_board_type(code))
                if bt:
                    labels["peak_exit"] = bt["peak_return_pct"]
                    labels["ret_exit"] = bt["return_pct"]
        rec = {"code": code, "d0_date": str(bars[i]["time"])[:10], "stage": stage,
               "labels": labels}
        if gates is not None:
            rec["gates"] = gates
        if sig is not None:
            rec["sig"] = sig
        if extra:
            rec.update(extra)
        probe.sample(**rec)

    # ---- 回测钩子 (指标预计算 O(n), 逐日 O(win); 参数经 merged_params 接线) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                       probe=None):
        """单股三金叉全历史回测: D0 信号 → D1 开盘买 → v1 通用出场引擎。

        probe 模式: 判定走 _signal_core_dbg (全门求值), 且 **fail 日也采样**
        (stage=声明序第一个 False 门 + extra.gates 门向量) — 门级拦截归因的数据源;
        非 probe 模式: 主路径短路版, 判定/性能与改造前逐字等价。
        """
        from app.market_cn.auto.common.filters import unified_prefilter
        # 参数接线 (2026-09-13 纪律): 一律 merged_params(None), 禁硬编码 kwargs
        p = self.merged_params(None)
        n = len(bars)
        if n < 35:
            return []
        board_type = get_board_type(code)
        ind = _calc_ind(bars, gate_k=int(p["close_above_ma"]))
        # 峰值/MAE 滑窗预计算 (O(n)/窗口; 仅 probe 模式消费, 主路径零影响)
        peak_maps = ({5: _win_extreme(bars, 5), 60: _win_extreme(bars, 60),
                      120: _win_extreme(bars, 120), 180: _win_extreme(bars, 180),
                      "mae5": _win_extreme(bars, 5, mode="min")}
                     if probe is not None else None)
        trades = []
        used = set()          # 同一 D0 只出一笔 (金叉窗口重叠时防重)

        for i in range(30, n - 1):
            if probe is not None:
                sig, gates = _signal_core_dbg(bars, i, p, ind)
                slopes = _slope_feats(bars, ind, i)
            else:
                sig = _signal_core(bars, i, p, ind)
                gates = None
            if not sig:
                if probe is not None:
                    # fail 日也采样: 归因门=声明序第一个 False 门; gates 向量供换序分析
                    stage = next((g for g, val in gates.items() if val is False),
                                 "no_gate")
                    self._probe_day_light(probe, bars, i, code, stock_info,
                                          stage=stage, gates=gates,
                                          extra={"sl": slopes},
                                          peak_maps=peak_maps)
                continue
            # 去重: 同 D0 日只取一笔 (三叉窗口滑过时同一共振段可能连续多日成信号)
            key = sig["d0_date"]
            if key in used:
                continue
            used.add(key)

            # U1~U4 (信号日 D0 收盘可知)
            if use_prefilter:
                ok, fails = unified_prefilter(bars, i, code, stock_info)
                if not ok:
                    if probe is not None:
                        self._probe_day_light(probe, bars, i, code, stock_info,
                                              stage="prefilter", sig=sig, gates=gates,
                                              extra={"u_fails": list(fails),
                                                     "sl": slopes},
                                              peak_maps=peak_maps)
                    continue

            # 入场: 次日(D1)开盘价
            d1 = bars[i + 1]
            entry_price = d1["open"]
            if entry_price <= 0:
                continue
            d0_close = float(bars[i]["close"])
            d1_change = (float(d1["close"]) / d0_close - 1) * 100
            d1_gap = (float(d1["open"]) / d0_close - 1) * 100

            # 出场: v1 通用引擎 (T+1 / low 触及+跳空开盘成交 / 跌停顺延)
            from app.market_cn.auto.strategies.v1 import _run_backtest as _run_exit
            bt = _run_exit(bars, i + 1, entry_price, int(p["hold"]), p["stop"],
                           p["trail"], board_type)
            if not bt:
                if probe is not None:
                    self._probe_day_light(probe, bars, i, code, stock_info,
                                          stage="engine_skip", sig=sig, gates=gates,
                                          extra={"sl": slopes},
                                          peak_maps=peak_maps)
                continue

            if probe is not None:
                self._probe_day_light(
                    probe, bars, i, code, stock_info, stage="signal",
                    sig=sig, gates=gates, peak_maps=peak_maps,
                    extra={"engine": {k: bt.get(k) for k in
                                      ("return_pct", "peak_return_pct",
                                       "exit_reason", "exit_day")},
                           "sl": slopes})
            trades.append({
                **sig,
                "signal_date": bars[i]["time"],
                "entry_date": d1["time"],
                "entry_price": round(entry_price, 3),
                "d1_change": round(d1_change, 2),
                "d1_gap": round(d1_gap, 2),
                "intraday": round(d1_change - d1_gap, 2),
                **bt,
            })

        return trades
