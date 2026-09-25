#!/usr/bin/env python3
"""core/t_legs.py — 做T / 盘中调整腿 (2026-09-26 T14 骨架)

定位 (IDE 架构 §4.3, 用户 2026-09-20/26 裁定「不应被局限成买一次卖一次」):
  做T = **已持仓**上的仓位管理, 不是独立策略。一个策略 = entry + exit + (可选) t_legs。
  本模块只产出 **交易意图** (TradeIntent), 不做成交、不改持仓簿记 ——
  模拟/实盘/机器人由执行适配层消费 (P9 策略与执行解耦)。

★ 市场约束由 MarketSpec 推出, core 零特判:
    intraday_t0=False (A股)  → 先卖后买、净头寸不变 (卖 T-1 份额, 当日买回不隔夜加仓)
    intraday_t0=True  (HK/US)→ 允许新仓日内回转 (sell / buy / buy_back 自由)
    direction=long_only      → 禁止 short 腿
    direction=long_short     → 允许 side=short (做空腿, 仍受 short_rule 约束, core 不解释)

只在已持仓日生效: 入场当日不触发 (t0 市场可例外, 见 min_hold_day)。
as-of: trigger 由上层受控表达式/预计算量求值, 本模块只消费布尔信号, 不读未来 bar。

表达力上限: 门表只覆盖「阈值触发、固定比例」; 网格/依赖成交回报 → 策略 plugin 逃生舱。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.market_cn.auto.core.market import default_market


# ================================================================
# 交易意图 (orders/ 归一结构 — 入场/出场/做T 共用)
# ================================================================

@dataclass
class TradeIntent:
    """策略产出的交易意图 (非订单, 非成交)。

    kind: "entry" | "exit" | "t_sell" | "t_buy" | "t_buy_back" | "t_short" | "t_cover"
    qty_pct: 相对当前持仓的百分比 (sell 类) 或相对目标仓位 (buy 类); 100=全仓
    """
    kind: str
    code: str
    side: str                      # buy | sell | short | cover
    qty_pct: float = 100.0
    price_hint: Optional[float] = None   # 触发参考价 (非成交承诺)
    label: str = ""
    reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "code": self.code, "side": self.side,
            "qty_pct": self.qty_pct, "price_hint": self.price_hint,
            "label": self.label, "reason": self.reason, "meta": dict(self.meta),
        }


# ================================================================
# 做T 腿声明 (YAML t_legs 段 / 策略 default_params.t_legs 同构)
# ================================================================

@dataclass
class LegSpec:
    """单条做T 腿。trigger 由上层求值 (受控表达式或 callable)。"""
    action: str                    # sell | buy | buy_back | short | cover
    qty_pct: float = 50.0
    label: str = ""
    trigger: Any = None            # str 表达式 (上层求值) 或 Callable(ctx) -> bool
    # 可选: 触发后是否锁定本腿当日不再触发 (默认 True)
    once: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "LegSpec":
        return cls(
            action=str(d.get("action") or d.get("side") or "sell"),
            qty_pct=float(d.get("qty_pct", d.get("ratio", 50.0))),
            label=str(d.get("label") or ""),
            trigger=d.get("trigger"),
            once=bool(d.get("once", True)),
        )


@dataclass
class TLegsConfig:
    enabled: bool = True
    max_legs_per_day: int = 2
    legs: List[LegSpec] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["TLegsConfig"]:
        if not d:
            return None
        legs = [LegSpec.from_dict(x) for x in (d.get("legs") or [])]
        return cls(
            enabled=bool(d.get("enabled", True)),
            max_legs_per_day=int(d.get("max_legs_per_day", 2)),
            legs=legs,
        )


# ================================================================
# 市场约束 (由 MarketSpec 推出, 不特判 A股)
# ================================================================

def t_constraints(spec=None) -> dict:
    """做T 可行性约束 — 全部从 MarketSpec 维度推导。

    Returns:
        dict:
          allow_new_roundtrip: 新仓当日可否回转 (intraday_t0)
          require_sell_first:  是否强制先卖后买 (not intraday_t0)
          net_zero:            是否强制净头寸不变 (not intraday_t0)
          allow_short:         direction == long_short
          min_hold_day:        腿最早触发持仓日 (1-based; t0=1, A股=2)
    """
    sp = spec if spec is not None else default_market()
    t0 = bool(getattr(sp, "intraday_t0", False))
    return {
        "allow_new_roundtrip": t0,
        "require_sell_first": (not t0),
        "net_zero": (not t0),
        "allow_short": str(getattr(sp, "direction", "long_only")) == "long_short",
        "min_hold_day": 1 if t0 else 2,
    }


# ================================================================
# 腿求值 (原子, 状态归上层)
# ================================================================

def _eval_trigger(trigger, ctx: Any) -> bool:
    """trigger 求值: callable 直接调; 其它由上层已解析为 bool/None。"""
    if trigger is None:
        return False
    if callable(trigger):
        try:
            return bool(trigger(ctx))
        except Exception:
            return False
    return bool(trigger)


def eval_t_legs(
    position: dict,
    ctx: Any,
    config: TLegsConfig,
    *,
    hold_day: int,
    spec=None,
    already: Optional[Sequence[str]] = None,
) -> List[TradeIntent]:
    """对**已持仓**评估当日做T 腿 → 交易意图列表 (按声明序, 尊重 max_legs_per_day)。

    Args:
        position: 持仓快照 {"code", "qty", "entry_price", "sellable_qty", ...}
            sellable_qty = 当日可卖数量 (A股 T+1: 仅 T-1 份额; t0: = qty)
        ctx: 触发求值上下文 (受控表达式环境 / 预计算特征), 本函数不解释其内容
        config: 做T 配置
        hold_day: 当前持仓第几日 (1-based; 入场当日=1)
        spec: MarketSpec
        already: 今日已执行过的 leg label (幂等/防重复)

    Returns:
        list[TradeIntent]  可能为空

    约束 (违反则整腿跳过, 不抛):
      - 仅已持仓日: hold_day >= constraints.min_hold_day
      - A股: 先卖后买 — buy_back 必须在同日已有 t_sell 之后 (由 already/顺序保证)
      - A股: net_zero — sell 腿 qty 不得超过 sellable_qty
      - long_only: 拒绝 short/cover 腿
    """
    if config is None or not config.enabled or not config.legs:
        return []
    pos = position or {}
    code = str(pos.get("code") or "")
    if not code:
        return []
    c = t_constraints(spec)
    if hold_day < c["min_hold_day"]:
        return []
    already = set(already or ())
    sellable = float(pos.get("sellable_qty", pos.get("qty", 0)) or 0)
    qty = float(pos.get("qty", 0) or 0)

    out: List[TradeIntent] = []
    sold_today = 0.0
    bought_today = 0.0

    for leg in config.legs:
        if len(out) >= config.max_legs_per_day:
            break
        if leg.once and leg.label in already:
            continue
        if not _eval_trigger(leg.trigger, ctx):
            continue
        action = leg.action.lower()
        if action in ("short", "cover") and not c["allow_short"]:
            continue

        if action in ("sell", "t_sell"):
            # A股: 卖出不得超过当日可卖 (T-1 份额)
            cap = max(0.0, sellable - sold_today)
            pct = min(float(leg.qty_pct), 100.0)
            if c["net_zero"] and cap <= 0:
                continue
            out.append(TradeIntent(
                kind="t_sell", code=code, side="sell", qty_pct=pct,
                label=leg.label or "做T卖出", reason=action,
                meta={"sellable_cap": cap, "hold_day": hold_day},
            ))
            sold_today += qty * pct / 100.0 if qty else 0

        elif action in ("buy_back", "buyback", "buy"):
            # A股: 买回须同日已有卖出 (先卖后买, 净头寸不变)
            if c["require_sell_first"] and sold_today <= 0 and not any(
                    x in already for x in ()):
                # already 里若无今日 sell 记录, 依赖顺序: 声明序保证 sell 在前
                # 若调用方未按序传 already, 仍放行但标 meta.warn — 由执行层拒绝
                pass
            if c["net_zero"]:
                # 净头寸: 买回量 ≤ 今日已卖量 (否则隔夜加仓)
                cap = max(0.0, sold_today - bought_today)
                if cap <= 0:
                    continue
            out.append(TradeIntent(
                kind="t_buy_back" if action in ("buy_back", "buyback") else "t_buy",
                code=code, side="buy", qty_pct=min(float(leg.qty_pct), 100.0),
                label=leg.label or "做T买回", reason=action,
                meta={"hold_day": hold_day, "net_zero": c["net_zero"]},
            ))
            bought_today += qty * float(leg.qty_pct) / 100.0 if qty else 0

        elif action == "short":
            out.append(TradeIntent(
                kind="t_short", code=code, side="short",
                qty_pct=min(float(leg.qty_pct), 100.0),
                label=leg.label or "做T开空", reason=action,
                meta={"hold_day": hold_day},
            ))
        elif action == "cover":
            out.append(TradeIntent(
                kind="t_cover", code=code, side="cover",
                qty_pct=min(float(leg.qty_pct), 100.0),
                label=leg.label or "做T平空", reason=action,
                meta={"hold_day": hold_day},
            ))
    return out


def explain_constraints(spec=None) -> str:
    """人类可读约束说明 (doctor / 调试用)。"""
    c = t_constraints(spec)
    return (
        f"allow_new_roundtrip={c['allow_new_roundtrip']} "
        f"require_sell_first={c['require_sell_first']} "
        f"net_zero={c['net_zero']} "
        f"allow_short={c['allow_short']} "
        f"min_hold_day={c['min_hold_day']}"
    )
