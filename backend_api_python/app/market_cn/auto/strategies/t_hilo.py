#!/usr/bin/env python3
"""strategies/t_hilo.py — 示例做T策略「持仓高抛低吸」(2026-09-26 T15)

定位 (IDE 架构 §4.3):
  做T = **已持仓**上的仓位管理腿, 与入场/出场并存。本文件是**可运行的教学示例**,
  演示三件事:
    1. 策略 = 入场 + 出场 + (可选) t_legs — 不是把做T写成第二个出场引擎;
    2. t_legs 约束由 MarketSpec 推出 (A股先卖后买/净头寸不变, 不特判);
    3. TradeIntent 只产出意图, 不成交 — 执行层 (模拟/实盘) 另接。

做T 腿设计 (高抛低吸, 单日净头寸不变):
  - 冲高减半: 当日涨幅 ≥ t_gain_pct (相对昨收) → 卖出 sell_pct 仓位
  - 回落接回: 回落自当日高点 ≤ t_pullback_pct 且今日已有卖出 → 买回 buy_back_pct
  A股 (intraday_t0=false): sell 在前 buy_back 在后; 买回量受今日已卖量约束 (net_zero)。
  HK/US (t0=true): 同腿声明自动解锁当日新仓回转, 无需改代码。

⚠️ 示例规则**未经两段稳定性验证**, config.json 默认 enabled=false, 不进实盘。
   上线前须走 param_scan 两段 + pool_check (见 docs/策略开发指南.md §7)。

字段口径:
  - day_gain%  = (last/prev_close - 1) * 100
  - pullback%  = (last/day_high - 1) * 100   (≤0, 越小跌越深)
  - 触发只用已发生快照/日线, 无未来函数。
"""
from __future__ import annotations

from app.market_cn.auto.core.market import get_board_type
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "t_hilo"
STRATEGY_LABEL = "持仓做T·高抛低吸"

# ---- 阈值全部进 params (config.json 可覆盖; 禁硬编码) ----
PARAMS = dict(
    # 入场 (演示用简化规则: 强势后缩量回踩)
    entry_gain_min=2.0,        # 近5日累计涨幅下限 %
    pullback_max=-2.0,         # 近3日自高点回撤上限 % (负数)
    vol_shrink_max=0.8,        # 当日量 / 5日均量 上限 (缩量)
    # 出场 (走默认 exit_decision: stop/trail/hold)
    stop=-6.0,
    trail=-5.0,
    hold=5,
    # 做T 腿
    t_gain_pct=3.0,            # 冲高阈值: 当日涨幅 ≥ 3% → 减半
    t_pullback_pct=-2.0,       # 回落阈值: 自当日高点 ≤ -2% → 接回
    t_sell_pct=50.0,           # 减仓比例 %
    t_buy_back_pct=50.0,       # 买回比例 %
    t_max_legs=2,              # 单日最多腿数
)


def _day_stats_from_snap(snap):
    """快照 → 当日统计 dict (无则 None)。只读已发生字段, as-of 安全。"""
    if not snap:
        return None
    try:
        last = float(snap.get("last") or 0)
        high = float(snap.get("high") or 0)
        low = float(snap.get("low") or 0)
        pc = float(snap.get("previousClose") or 0)
    except (TypeError, ValueError):
        return None
    if last <= 0 or pc <= 0 or high <= 0:
        return None
    return {
        "last": last, "high": high, "low": low, "prev_close": pc,
        "day_gain_pct": (last / pc - 1) * 100,
        "pullback_from_high_pct": (last / high - 1) * 100 if high > 0 else 0.0,
    }


@register
class THiloStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(PARAMS)
    data_needs = ("daily", "quote", "minute_live")
    # 做T 腿: 触发在 t_leg_intents 里用 t_gain_pct / t_pullback_pct 求值
    # (演示「策略内算触发」; 若用门表 DSL 可写 trigger 表达式, 见 IDE §4.3)
    t_legs = {
        "enabled": True,
        "max_legs_per_day": 2,
        "legs": [],   # 空 = 完全走 t_leg_intents() 覆盖 (本示例做法)
    }

    # ================================================================
    # 入场 (演示规则: 强势后缩量回踩) — 非荐股, 仅为让策略可独立回测
    # ================================================================
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        n = len(bars)
        if n < 10:
            return []
        i = n - 1
        closes = [float(b["close"]) for b in bars]
        vols = [float(b["volume"]) for b in bars]
        if closes[-6] <= 0:
            return []
        gain5 = (closes[-1] / closes[-6] - 1) * 100
        hi3 = max(closes[-3:])
        pullback = (closes[-1] / hi3 - 1) * 100 if hi3 > 0 else 0
        vol5 = sum(vols[-6:-1]) / 5
        vol_r = (vols[-1] / vol5) if vol5 > 0 else 99.0
        if gain5 < float(p["entry_gain_min"]):
            return []
        if pullback < float(p["pullback_max"]):     # 回撤过深 = 趋势坏, 不接
            return []
        if pullback > -0.1:                         # 几乎新高 = 不算回踩
            return []
        if vol_r > float(p["vol_shrink_max"]):
            return []
        d0 = bars[i]
        score = int(min(90, max(10, 50 + gain5 * 2 + (vol_r < 0.5) * 10)))
        return [Signal(
            code=code, time=str(d0["time"])[:10], score=score,
            price=float(d0["close"]),
            label=f"{STRATEGY_LABEL},g5={gain5:.1f},vr={vol_r:.2f}",
            extra={
                "gain5": round(gain5, 2), "pullback": round(pullback, 2),
                "vol_r": round(vol_r, 3), "board": get_board_type(code),
            },
        )]

    # ================================================================
    # 做T 腿 (核心演示)
    # ================================================================
    def t_leg_intents(self, position, ctx, *, hold_day, spec=None, already=None):
        """已持仓日的做T 意图 — 高抛低吸, A股自动先卖后买。

        ctx 约定 (dict 或对象, 缺键则该腿不触发):
          - snap / day: 当日快照或 _day_stats_from_snap 结果
          - sold_today_pct: 今日已卖出比例 (执行层回填; 缺省 0)
        返回 list[TradeIntent]; 不成交。
        """
        from app.market_cn.auto.core.t_legs import (
            TLegsConfig, TradeIntent, eval_t_legs, t_constraints,
        )
        p = self.merged_params(None)
        if not position or not position.get("code"):
            return []
        c = t_constraints(spec)
        if hold_day < c["min_hold_day"]:
            return []

        # 归一日统计
        ctx = ctx or {}
        stats = ctx.get("day") or _day_stats_from_snap(ctx.get("snap"))
        if not stats:
            return []
        sold_pct = float(ctx.get("sold_today_pct") or 0.0)

        intents = []
        gain = float(stats.get("day_gain_pct") or 0)
        pull = float(stats.get("pullback_from_high_pct") or 0)

        # 1) 冲高减半 (先卖 — A股 net_zero 也依赖先有卖出)
        if gain >= float(p["t_gain_pct"]):
            intents.append(TradeIntent(
                kind="t_sell", code=position["code"], side="sell",
                qty_pct=float(p["t_sell_pct"]),
                price_hint=stats.get("last"),
                label="冲高减半",
                reason=f"day_gain>={p['t_gain_pct']}%",
                meta={"day_gain_pct": round(gain, 2), "hold_day": hold_day},
            ))
            sold_pct += float(p["t_sell_pct"])

        # 2) 回落接回 (须今日已卖, 且触发回落阈值)
        if pull <= float(p["t_pullback_pct"]) and sold_pct > 0:
            intents.append(TradeIntent(
                kind="t_buy_back", code=position["code"], side="buy",
                qty_pct=float(p["t_buy_back_pct"]),
                price_hint=stats.get("last"),
                label="回落接回",
                reason=f"pullback<={p['t_pullback_pct']}%",
                meta={"pullback_from_high_pct": round(pull, 2),
                      "sold_today_pct": sold_pct, "hold_day": hold_day},
            ))

        # 封顶腿数 (与 TLegsConfig.max_legs_per_day 同语义)
        max_legs = int(p.get("t_max_legs") or 2)
        return intents[:max_legs]
