"""strategies/break_buy.py — 断板接力策略 (StrategyBase 插件实现, Phase 2 迁移)

实现已迁移至本文件; dragon_core.break_today_d0_signals / _break_signal_at 为 facade 转发。

入场 (D0 盘后扫描 → D1 竞价):
  连板≥2 → 断板期(≤max_break_gap天) → 确认日=断板期最后一天 → D1 开盘买入
  断板期检查 5a~5f: 低点不破涨停日开盘 / 缩量1.2~2.0x / 首断日涨跌+gap 区间 /
  回撤不破限 / 确认日增强过滤(三通道OR: 企稳[0,2) | 均量比≥1.4 | 前20日涨幅≥30)
  竞价: 无 gap 过滤 (恒可买, gap 判定交给 D1 数据)
  U1~U4: prefilter_anchor='signal' (锚定确认日=末根bar; 连板≥2已隐含U4)

出场 (收盘价判定, monitor break 分支 / run_backtest_breakbuy 语义):
  止损 main-8%/gem-10% / 追踪止损(自入场峰值, 需 ret>0) / 峰值逃顶(ret>10%+上影>40%+收盘<high*0.98) / 到期 main20/gem15天

易错点:
  - 确认日 = 断板期最后一天 (break_idx+break_days-1 == D0), 不是首断板日;
  - _break_signal_at 的 5c/5d 上界 (+8%/+5%) 是硬编码, 与 BOARD_PARAMS 无关 — 勿"配置化";
  - exit 是收盘价口径 (close 判定), 与 v1 的 low 触及口径不同 — 勿混用;
  - 追踪止损要求 ret>0 (盈利中才追踪), 与止损分支互斥由 ret<=stop 先拦。
"""
from __future__ import annotations

from app.market_cn.auto.common.market import get_board_name, get_board_type, is_limit_up
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "break"
STRATEGY_LABEL = "断板"

# 板块参数 (与 dragon_core.BOARD_PARAMS 同源; config.json params 可覆盖其键)
BOARD_PARAMS = {
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 20,
             "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10,
             "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0,
             "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
             "first_break_gap_min": 0, "first_break_chg_min": 0.0},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15,
                 "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15,
                 "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0,
                 "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
                 "first_break_gap_min": 0, "first_break_chg_min": 0.0},
}

DEFAULT_PARAMS = dict(min_streak=2, max_break_gap=5)


# ================================================================
# 断板期判定 (原 dragon_core._break_signal_at, 原样移植)
# ================================================================

def _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, params):
    """给定连板区间[streak_start,streak_end], 计算断板期并执行 5a-5f 确认。

    返回信号dict(含 break_date/break_days/break_chg/break_gap/break_vol_r)或 None。
    """
    bt = get_board_type(code)
    streak_len = streak_end - streak_start + 1
    if streak_len < min_streak:
        return None

    # 断板期: 涨停日后连续非涨停的天数
    break_idx = streak_end + 1
    if break_idx >= len(bars):
        return None
    limit_bar = bars[streak_end]
    limit_open = float(limit_bar["open"])
    limit_close = float(limit_bar["close"])
    limit_vol = float(limit_bar["volume"])
    break_days = 0
    for j in range(break_idx, min(break_idx + max_break_gap + 1, len(bars))):
        if is_limit_up(bars[j]["close"], bars[j - 1]["close"], bt):
            break  # 遇到新涨停, 断板期结束
        break_days += 1

    if break_days == 0:
        # 涨停后直接又是涨停 → 连板加速, 不是断板
        return None

    # 5. 断板期各项检查 (与回测 strategy_break_buy 完全一致)
    break_bars = bars[break_idx:break_idx + break_days]
    first_break = break_bars[0]

    # 5a. 断板期低点不能跌破涨停日开盘价 (支撑有效)
    break_low = min(float(b["low"]) for b in break_bars)
    if break_low < limit_open:
        return None

    # 5b. 断板期缩量检查 (vs 涨停日量)
    break_vol_avg = sum(float(b["volume"]) for b in break_bars) / len(break_bars)
    break_vol_r = break_vol_avg / limit_vol if limit_vol > 0 else 0
    if break_vol_r < params["vol_min"] or break_vol_r >= params["vol_max"]:
        return None

    # 5c. 第一个断板日涨跌过滤: vs 涨停日收盘, 允许 first_break_chg_min ~ +8%
    first_break_chg = (first_break["close"] / limit_close - 1) * 100
    if first_break_chg < params.get("first_break_chg_min", -5) or first_break_chg >= 8:
        return None

    # 5d. 第一个断板日开盘过滤: 高开不超过 5%, 低开不低于 first_break_gap_min
    first_break_gap = (first_break["open"] / limit_close - 1) * 100
    if first_break_gap < params.get("first_break_gap_min", -3) or first_break_gap >= 5:
        return None

    # 5e. 回撤检查
    break_drawdown = (break_low / limit_close - 1) * 100
    if break_drawdown < params["drawdown_max"]:
        return None

    # 5f. 确认日特征 + 增强过滤 (三通道OR, 满足其一即可)
    confirm_bar = break_bars[-1]
    confirm_prev = break_bars[-2] if len(break_bars) >= 2 else limit_bar
    _c_prev_close = float(confirm_prev["close"])
    confirm_chg = (float(confirm_bar["close"]) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    confirm_gap = (float(confirm_bar["open"]) / _c_prev_close - 1) * 100 if _c_prev_close > 0 else 0.0
    pre20_gain = None
    if streak_start >= 20:
        _pre_ref = float(bars[streak_start - 20]["close"])
        if _pre_ref > 0:
            pre20_gain = (limit_close / _pre_ref - 1) * 100
    if params.get("enhance_filter", True):
        # 通道1: 确认日涨跌 [confirm_chg_min, confirm_chg_max)
        _pass_chg = params.get("confirm_chg_min", 0.0) <= confirm_chg < params.get("confirm_chg_max", 2.0)
        # 通道2: 断板期均量比 >= vol_r_or_min (换手充分)
        _pass_vol = break_vol_r >= params.get("vol_r_or_min", 1.4)
        # 通道3: 连板前20日涨幅 >= pre20_min (前期热度)
        _pass_hot = pre20_gain is not None and pre20_gain >= params.get("pre20_min", 30.0)
        if not (_pass_chg or _pass_vol or _pass_hot):
            return None

    # 5g. 均线多头排列 (确认日 MA5>MA10>MA20): 剔除断板期处于均线纠缠/空头的弱信号
    ma_bull = _ma_bull_at(bars, break_idx + break_days - 1)
    if params.get("ma_bull_filter", True) and ma_bull is False:
        return None

    return {
        "break_idx": break_idx, "break_days": break_days,
        "break_date": bars[break_idx]["time"],
        "streak_len": streak_len, "streak_start": bars[streak_start]["time"], "streak_end": bars[streak_end]["time"],
        "break_chg": round(first_break_chg, 2),
        "break_gap": round(first_break_gap, 2),
        "break_vol_r": round(break_vol_r, 2),
        "confirm_chg": round(confirm_chg, 2),
        "confirm_gap": round(confirm_gap, 2),
        "pre20_gain": round(pre20_gain, 2) if pre20_gain is not None else None,
        "ma_bull": ma_bull,
    }


def _ma_bull_at(bars, idx):
    """确认日均线多头排列: MA5>MA10>MA20 (idx=确认日索引); 数据不足(上市<20日)返回None。"""
    if idx + 1 < 20:
        return None
    c = [float(b["close"]) for b in bars[idx - 19:idx + 1]]
    ma5 = sum(c[-5:]) / 5
    ma10 = sum(c[-10:]) / 10
    ma20 = sum(c) / 20
    return ma5 > ma10 > ma20


def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → 旧 break_today_d0_signals 的 dict 形态 (facade 兼容层)。

    **易错点**: 必须显式列字段 — 旧输出不含 break_idx (内部变量), 全量透传 extra
    会让回测 trades 多键, 破坏逐笔对数。streak_start/streak_end 是日期字符串。
    """
    ex = sig.extra or {}
    return {
        "code": code,
        "board": get_board_name(code),
        "path": "break_buy",
        "path_label": "断板",
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
        "entry_price": None,
        "buy_mode": "next_open",
    }


@register
class BreakStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"        # 锚定确认日(末根bar); 连板≥2已隐含U4
    entry_style = "brk"
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DEFAULT_PARAMS)

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None, **params):
        """今日是否为断板期确认日 → Signal (至多1笔)。as_of=k: 只用 bars[:k+1]。

        limit_ups: 预计算的涨停日索引列表 (回测/扫描复用, None 则现算 bars[:as_of])。"""
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

        # 寻找所有连板结构, 要求断板期最后一天 == 今日(i)
        for lu_idx in (limit_ups if limit_ups is not None else _find_limit_ups(bars[:i], bt)):
            # 连板第一板确认 (lu_idx 前一日非涨停)
            is_first = True
            for k in range(1, min(11, lu_idx + 1)):
                if lu_idx - k - 1 >= 0 and is_limit_up(bars[lu_idx - k]["close"], bars[lu_idx - k - 1]["close"], bt):
                    is_first = False
                    break
            if not is_first:
                continue
            # 连板结束位置
            streak_start = lu_idx
            streak_end = lu_idx
            while streak_end < i - 1 and is_limit_up(bars[streak_end + 1]["close"], bars[streak_end]["close"], bt):
                streak_end += 1
            sig = _break_signal_at(bars, code, streak_start, streak_end, min_streak, max_break_gap, board_params)
            if not sig:
                continue
            if sig["break_idx"] + sig["break_days"] - 1 != i:
                continue
            circ = float((params.get("stock_info") or {}).get("circ_shares") or 0)
            total = float((params.get("stock_info") or {}).get("total_shares") or 0)
            extra = dict(sig)
            extra.update({
                "turnover_anchor": round(float(bars[streak_end]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_sig": round(float(bars[i]["volume"]) / circ * 100, 2) if circ > 0 else None,
                "turnover_anchor_total": round(float(bars[streak_end]["volume"]) / total * 100, 2) if total > 0 else None,
                "turnover_sig_total": round(float(bars[i]["volume"]) / total * 100, 2) if total > 0 else None,
            })
            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=int(sig.get("confirm_chg", 0) or 0) + 10,   # 与 dragon_scan 后处理口径一致
                price=0.0,                                        # 断板信号日不定价 (entry=D1开盘)
                label="断板",
                extra=extra,
            ))
            break  # 只取一个信号
        return result

    # ---- D1 竞价处置 ----
    def entry_decision(self, row, snap=None, **params):
        """break 无开盘 gap 过滤 (恒可买); 快照缺失不可买 (与 monitor skip 一致)。"""
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        return EntryDecision(True, "断板无gap过滤, 开盘可买")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """break 无确认步骤 (确认已在 D0 断板期判定完成)。"""
        return ConfirmDecision(True, "break无确认步骤")

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘价口径 (monitor break 分支 / run_backtest_breakbuy 语义):
        止损 / 追踪止损(ret>0) / 峰值逃顶 / 到期。live 模式 → hold (硬止损在 monitor 主循环)。"""
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


def _find_limit_ups(bars, bt):
    """涨停日索引 (is_limit_up vs 前收; 第0根无前收跳过)。与 common find_limit_ups 同语义。"""
    from app.market_cn.auto.common.market import find_limit_ups
    return find_limit_ups(bars, bt)
