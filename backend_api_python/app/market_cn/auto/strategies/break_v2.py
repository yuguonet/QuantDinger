"""strategies/break_v2.py — 断板接力 V2 (2026-09-11, break_buy 兄弟插件)

与 break (V1) 的唯一差异 = **确认日前置门** (引擎同口径 trail 标签归因反哺,
见 tmp/break_v2_trail归因报告.md / break_v2_敏感性.md):

  放行条件 (单条件否决制, 作用于 v1 会选定的候选):
    A. 确认日换手 turnover_sig <= turnover_max (默认 12.0) —
       低换手=缩量一致/蓄势; confirm 池 14 万样本引擎口径全桶单调递减
       ([0,5)+1.56 → [30+)-0.94), 阈值 5/6/8/10/12 五档全平坦两段同正;
       与 d1c 口径方向相反 (高换手=盘中颠簸被止损线截断, 2026-09-11 反转事故)。
       **必要条件语义 (fail-close)**: circ 缺失=无法确认必要条件 → 否决
       (实证该池 17 笔 52.9%/-0.16)。
    B. (默认关) 大盘深弱 env_ret20 <= env_ret20_max — 池级 alpha 未迁移到
       信号级 (B-only 7 笔 42.9%/-0.01), 接口保留待实盘样本复核;
       若开启, "可测且不达标"才否决 (fail-open, 避免指数数据故障误杀)。
  **门位置双轨** (2026-09-11 定案): live=扫描内 (当日独立, 无跨日状态);
  回测=作用于"v1 会选定的候选" (dedup/used 登记之后, 否决即整个连板段作废)。
  勿把门放回回测的扫描内 — 首确认日被否决后 used 未登记, 同一连板段次日
  重新入场 (实证 11 笔 27.3%/-2.66 次日重入毒药), 破坏 v2 ⊆ v1 严格子集。

入场/出场与 V1 逐字同源 (5a~5f 断板期检查 / BOARD_PARAMS / _run_backtest_breakbuy)。
**铁律**: V1 信号集零改动; V2 是 v1 的子集策略, 终审 = 引擎级 600d/300d A/B。

易错点:
  - env_ret20 与样本库 env_000300_ret_20 同口径: c[-1]/c[-21]-1 (小数, -0.05 即 -5%),
    as_of=确认日 (daily_close kind 环境特征 as-of 边界), _env_ret20 进程内 memo;
  - turnover_sig 口径同 V1 (=D0成交量/流通股本*100, circ 缺失记 None 走 fail-open);
  - 出场引擎 import 自 break_buy (单一定义, 勿复制); exit 行为与 V1 完全一致。
"""
from __future__ import annotations

from app.market_cn.auto.common.market import (
    find_limit_ups, get_board_name, get_board_type, is_limit_up,
)
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)
from app.market_cn.auto.strategies.break_buy import (
    BOARD_PARAMS, _break_signal_at, _run_backtest_breakbuy,
)

STRATEGY_KEY = "break_v2"
STRATEGY_LABEL = "断板V2"

DEFAULT_PARAMS = dict(min_streak=2, max_break_gap=5,
                      # V2 前置门 (2026-09-11 trail 引擎口径归因 + 引擎级 A/B 终审):
                      # turnover_max=确认日换手上界 (低换手好, 池级全桶单调+五档平坦;
                      # 信号级子集语义 600d 65.1%/+4.52, 300d 72.7%/+6.22 vs v1
                      # 63.0%/+3.28 / 71.6%/+4.41, 两段全正; 8/12 双档全优=平坦;
                      # 终版上线取 turnover_max=12 (默认即此值, 与验收证据一致)。
                      # env_ret20_max=None 默认关 — 池级深弱 alpha 未迁移到信号级
                      # (B-only 7 笔 42.9%/-0.01), 接口保留待实盘样本攒厚复核。
                      # None=该条件关闭; 条件不可知 (circ/指数缺失) 不否决 (fail-open)。
                      turnover_max=12.0, env_ret20_max=None, env_index="000300",
                      turnover_min=None)


# ================================================================
# V2 前置门: 大盘 20 日收益 (进程内 memo, 与样本库 env_000300_ret_20 同口径)
# ================================================================

_ENV_RET20_MEMO: dict = {}


def _env_ret20(index_code, d0_date):
    """指数 20 日收益 c[-1]/c[-21]-1 (小数; as_of=确认日, 防未来函数)。

    失败/数据不足 → None (fail-open: 该条件视为不可知)。memo 键=(指数,日期)。
    """
    k = (index_code, d0_date)
    if k in _ENV_RET20_MEMO:
        return _ENV_RET20_MEMO[k]
    v = None
    try:
        from app.market_cn.auto.data.hub import index_daily
        bars = index_daily(index_code, days=60, as_of=d0_date) or []
        c = [float(b["close"]) for b in bars]
        if len(c) >= 21 and c[-21] > 0:
            v = c[-1] / c[-21] - 1
    except Exception:
        v = None
    _ENV_RET20_MEMO[k] = v
    return v


def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → 旧 dict 形态 (与 break_buy 同构; path/path_label 标 V2)。"""
    ex = sig.extra or {}
    return {
        "code": code,
        "board": get_board_name(code),
        "path": "break_v2",
        "path_label": "断板V2",
        "mode": "streak_break",
        "streak_len": ex.get("streak_len"),
        "streak_start": ex.get("streak_start"),
        "streak_end": ex.get("streak_end"),
        "break_date": ex.get("break_date"),
        "signal_date": sig.time,
        "break_days": ex.get("break_days"),
        "break_chg": ex.get("break_chg"),
        "break_gap": ex.get("break_gap"),
        "break_vol_r": ex.get("break_vol_r"),
        "confirm_chg": ex.get("confirm_chg"),
        "confirm_gap": ex.get("confirm_gap"),
        "pre20_gain": ex.get("pre20_gain"),
        "ma_bull": ex.get("ma_bull"),
        "turnover_anchor": ex.get("turnover_anchor"),
        "turnover_sig": ex.get("turnover_sig"),
        "turnover_anchor_total": ex.get("turnover_anchor_total"),
        "turnover_sig_total": ex.get("turnover_sig_total"),
        "env_ret20": ex.get("env_ret20"),
        "v2_gate": ex.get("v2_gate"),
        "entry_price": None,
        "buy_mode": "next_open",
    }


@register
class BreakV2Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    entry_style = "brk"                # 出场引擎与 V1 同分支 (monitor break 语义)
    family = "break"                   # 版本链: break_v2 ⊆ break, 展示归一优先于 V1
    family_version = 2
    data_needs = ("daily", "index_daily")   # §3.4 声明制: env_ret20 门消费 hub.index_daily (审计: 2026-09-12 偏差 A1)
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DEFAULT_PARAMS)
    PROBE_STAGE_RANK = {"confirm": 1, "align": 2, "dedup": 3, "prefilter": 4,
                        "engine_skip": 5, "signal": 6}

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     probe=None, **params):
        """与 BreakStrategy.scan_signals 同构; 差异仅前置门 (A∨B)。"""
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < 3:
            return result
        i = n - 1
        if i < 2:
            return result
        bt = get_board_type(code)
        board_params = BOARD_PARAMS.get(bt, BOARD_PARAMS["main"])
        board_params = {**board_params, **{k: v for k, v in p.items() if k in board_params}}
        min_streak, max_break_gap = p["min_streak"], p["max_break_gap"]

        for lu_idx in (limit_ups if limit_ups is not None else _find_limit_ups(bars[:i], bt)):
            is_first = True
            for k in range(1, min(11, lu_idx + 1)):
                if lu_idx - k - 1 >= 0 and is_limit_up(bars[lu_idx - k]["close"], bars[lu_idx - k - 1]["close"], bt):
                    is_first = False
                    break
            if not is_first:
                continue
            streak_start = lu_idx
            streak_end = lu_idx
            while streak_end < i - 1 and is_limit_up(bars[streak_end + 1]["close"], bars[streak_end]["close"], bt):
                streak_end += 1
            sig = _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, board_params)
            if not sig:
                if probe is not None:
                    probe.trace("confirm", code=code, d0_date=str(bars[i]["time"])[:10],
                                streak_start=str(bars[streak_start]["time"])[:10],
                                streak_end=str(bars[streak_end]["time"])[:10],
                                streak_len=streak_end - streak_start + 1)
                continue
            if sig["break_idx"] + sig["break_days"] - 1 != i:
                if probe is not None:
                    probe.trace("align", code=code, d0_date=str(bars[i]["time"])[:10],
                                break_days=sig.get("break_days"))
                continue
            # ---- V2 前置门 (live 扫描内; 与回测侧同语义) ----
            # A (换手): 必要条件语义, circ 缺失 fail-close; B (env): fail-open
            circ = float((params.get("stock_info") or {}).get("circ_shares") or 0)
            _to_sig = float(bars[i]["volume"]) / circ * 100 if circ > 0 else None
            _d0 = str(bars[i]["time"])[:10]
            _r20 = _env_ret20(p.get("env_index") or "000300", _d0)
            _tmax, _rmax = p.get("turnover_max"), p.get("env_ret20_max")
            veto_a = _tmax is not None and (_to_sig is None or _to_sig > _tmax)
            veto_b = _r20 is not None and _rmax is not None and _r20 > _rmax
            if veto_a or veto_b:
                if probe is not None:
                    probe.trace("prefilter", code=code, gate="v2_lowconv_env",
                                d0_date=_d0,
                                turnover_sig=round(_to_sig, 2) if _to_sig is not None else None,
                                env_ret20=round(_r20, 4) if _r20 is not None else None,
                                turnover_max=_tmax, env_ret20_max=_rmax)
                break   # 当日放弃 (非 continue): 勿顺延其它连板候选
            gate_a = (not veto_a) if _tmax is not None else None
            gate_b = (not veto_b) if _rmax is not None else None
            _g = (("A" if gate_a else "") + ("B" if gate_b else "")) or "?"
            total = float((params.get("stock_info") or {}).get("total_shares") or 0)
            extra = dict(sig)
            extra.update({
                "turnover_anchor": round(float(bars[streak_end]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_sig": round(float(bars[i]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_anchor_total": round(float(bars[streak_end]["volume"]) / total * 100, 2) if total > 0 else None,
                "turnover_sig_total": round(float(bars[i]["volume"]) / total * 100, 2) if total > 0 else None,
                "env_ret20": round(_r20, 4) if _r20 is not None else None,
                "v2_gate": _g,
            })
            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=int(sig.get("confirm_chg", 0) or 0) + 10,
                price=0.0,
                label=STRATEGY_LABEL,
                extra=extra,
            ))
            break  # 只取一个信号
        return result

    # ---- D1 竞价处置 ----
    def entry_decision(self, row, snap=None, **params):
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        return EntryDecision(True, "断板V2无gap过滤, 开盘可买")

    # ---- 15:00 收盘确认 ----
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
        gem = get_board_type(code) == "gem_star"
        return round(entry_price * (1 + (-10.0 if gem else -8.0) / 100), 3)

    # ---- 出场判定 (与 V1 逐字同源) ----
    def exit_decision(self, row, snap=None, **params):
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        bt = get_board_type(row.get("code", ""))
        bp = BOARD_PARAMS["gem_star" if bt == "gem_star" else "main"]
        stop, trail, hold = bp["stop_loss"], bp["trailing_stop"], bp["hold_days"]
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
        if ret > 10:
            bar_range = last_bar["high"] - last_bar["low"]
            upper = (last_bar["high"] - max(last_bar["open"], last_bar["close"])) / bar_range * 100 if bar_range > 0 else 0
            if upper > 40 and last_bar["close"] < last_bar["high"] * 0.98:
                return ExitDecision("exit", reason="峰值逃顶", price=float(last_bar["close"]))
        if held >= hold:
            return ExitDecision("exit", reason=f"持仓到期{hold}天", price=float(last_bar["close"]))
        return ExitDecision("hold")

    # ---- 回测钩子 (与 break_buy.backtest_stock 同构; 门参数从 config 透传) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                      probe=None):
        from app.market_cn.auto.common.filters import unified_prefilter
        from app.market_cn.auto.probe import DayTrace
        min_streak, max_break_gap = 2, 5
        # V2 门参数 (2026-09-11): config 覆盖 > 代码默认。**门在回测侧应用于
        # "v1 会选定的候选"** (dedup/used 登记之后) — 扫描内门会造成 dedup 泄漏:
        # 首确认日被门否决后 used 未登记, 同一连板段次日重新入场 (实证 11 笔
        # 27.3%/-2.66 的次日重入毒药), 破坏 v2 ⊆ v1 严格子集语义。
        from app.market_cn.auto import strategies as _strat_reg
        try:
            _ov = _strat_reg.params_override(self.key)
        except Exception:
            _ov = {}
        _gate_kw = {k: _ov[k] for k in ("turnover_max", "env_ret20_max")
                    if _ov.get(k) is not None}
        _mp = self.merged_params()
        _tmax_g = _gate_kw.get("turnover_max", _mp.get("turnover_max"))
        _rmax_g = _gate_kw.get("env_ret20_max", _mp.get("env_ret20_max"))
        _env_ix = _mp.get("env_index") or "000300"
        bt_type = get_board_type(code)
        params = dict(BOARD_PARAMS[bt_type])
        stop_loss, trailing_stop = params["stop_loss"], params["trailing_stop"]
        hold_days = params["hold_days"]
        n = len(bars)
        if n < 6:
            return []
        lu_all = find_limit_ups(bars, bt_type)
        lu_set = set(lu_all)
        trades = []
        used = set()

        for i in range(4, n - 1):
            if is_limit_up(bars[i]["close"], bars[i - 1]["close"], bt_type):
                continue
            if not any(j in lu_set for j in range(max(1, i - max_break_gap), i)):
                continue
            day_tr = DayTrace() if probe is not None else None
            sigs = [_signal_to_legacy_dict(s, code) for s in self.scan_signals(
                bars[:i + 1], code,
                min_streak=min_streak, max_break_gap=max_break_gap,
                turnover_max=None, env_ret20_max=None,   # 回测侧关扫描内门 (见下)
                limit_ups=[j for j in lu_all if j < i],
                stock_info=stock_info, probe=day_tr)]
            if not sigs:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info)
                continue
            sig = sigs[0]

            key = (sig["streak_start"], sig["break_date"])
            if key in used:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="dedup", sig=sig)
                continue
            used.add(key)

            if use_prefilter:
                ok, fails = unified_prefilter(bars, i, code, stock_info)
                if not ok:
                    if probe is not None:
                        self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                        stage="prefilter", sig=sig, u_fails=fails)
                    continue

            # ---- V2 前置门 (回测侧, 作用于 v1 选定的候选; used 已登记 →
            #      否决即整个连板段作废, v2 ⊆ v1 严格子集) ----
            # A (换手): 必要条件语义 — 可测且达标才放行; circ 缺失=无法确认
            #   必要条件 → 否决 (fail-close; 2026-09-11 实证该池 17 笔 52.9%/-0.16)
            # B (env, 默认关): 可测且不达标才否决 (fail-open; 池级证据弱, 避免误杀)
            _to_sig = sig.get("turnover_sig")
            _r20 = _env_ret20(_env_ix, str(bars[i]["time"])[:10])
            veto_a = _tmax_g is not None and (_to_sig is None or _to_sig > _tmax_g)
            veto_b = _r20 is not None and _rmax_g is not None and _r20 > _rmax_g
            if veto_a or veto_b:
                if probe is not None:
                    probe.trace("engine_skip", code=code, gate="v2_lowconv_env",
                                d0_date=str(bars[i]["time"])[:10],
                                turnover_sig=_to_sig, env_ret20=_r20,
                                turnover_max=_tmax_g, env_ret20_max=_rmax_g)
                continue
            _g = (("A" if not veto_a else "") + ("B" if not veto_b else "")) or "?"

            entry_price = bars[i + 1]["open"]
            if entry_price <= 0:
                continue
            result = _run_backtest_breakbuy(bars, i + 1, entry_price, hold_days,
                                           stop_loss, trailing_stop, bt_type)
            if not result:
                if probe is not None:
                    self._probe_day(probe, day_tr, bars, i, code, stock_info,
                                    stage="engine_skip", sig=sig)
                continue

            if probe is not None:
                self._probe_day(
                    probe, day_tr, bars, i, code, stock_info, stage="signal",
                    sig=sig, extra={"engine": {k: result.get(k) for k in
                                               ("return_pct", "peak_return_pct",
                                                "exit_reason", "exit_day")}})
            prev_close = bars[i]["close"]
            trades.append({
                **sig,
                "v2_gate": _g,          # 回测侧门归因 (覆盖扫描内占位 '?')
                "signal_date": bars[i]["time"],
                "entry_date": bars[i + 1]["time"],
                "entry_price": round(entry_price, 3),
                "buy_mode": "next_open",
                "d1_change": round((bars[i + 1]["close"] / bars[i + 1]["open"] - 1) * 100, 2)
                if bars[i + 1]["open"] > 0 else 0,
                "d1_gap": round((bars[i + 1]["open"] / prev_close - 1) * 100, 2)
                if prev_close > 0 else 0,
                "intraday": round((bars[i + 1]["close"] - bars[i + 1]["open"]) / prev_close * 100, 2)
                if prev_close > 0 else 0,
                **result,
            })

        return trades


def _find_limit_ups(bars, bt):
    from app.market_cn.auto.common.market import find_limit_ups as _fl
    return _fl(bars, bt)
