#!/usr/bin/env python3
"""core/exec.py — 成交语义原语（框架不变量，**唯一实现**）。

用途: 把散落在各出场引擎里的执行约束**原语**收敛为框架不变量。
     引擎/策略禁止再手写这些判定 (代码审查项), 显式例外须注明回测依据。

框架不变量 (语义声明, 引擎结构保证):
  1. 只能做多 — 全部引擎无做空路径 (由 MarketSpec `direction` 约束);
  2. 买入当日不可卖 (A 股的 T+1 交易限制) — 引擎从 d>=1 的下一根 bar 起判定卖出。
     ⚠️ 该间隔由 `MarketSpec.intraday_t0` 决定, **不是**写死的"次日": `intraday_t0=true`
     的市场 (HK/US) 当日即可回转。引擎侧读 spec 维度, 不特判市场;
  3. 跳空穿越按开盘成交 — 触发日开盘优于触发价时, 成交价=开盘价 (可实现);
  4. 跌停无法卖出 — 一字跌停整日跳过; 触发成交触及跌停 → 顺延次日开盘强平
     (连续一字逐日顺延); 到期日无法成交同样顺延。
     价格边界与容差来自 `MarketSpec` (`price_band`), 无涨跌停市场 (HK/US) 自动不生效;
  5. 涨停无法买入 — 涨停 buy 不追 (由判定/入场过滤保证, 如 relay3 gap<=9%)。

**市场差异外置**: 原 `exec_cn.py` 把"跌停 10%/20%""容差 0.002"写死在代码里。
现全部来自 `adapters/markets/*.yaml` (经 `core.market`)。本文件只保留**与市场无关**的
原子判定, 因此接港股/美股不需要改这里。

易错点:
  - 判定容差 (A 股 0.002 / 0.2%) 用于吸收 qfq 复权微差, 勿"修正"为 0;
  - 原语只做原子判定, 状态机 (pending_dn/last_unfilled/顺延指针) 仍归各引擎——
    因为各引擎的顺延语义不同 (V1 动量清仓/断板收盘判定/龙回头分段追踪), 硬抽象会造出 bug。
  - `spec=None` → 默认市场 (冻结 `.py` 插件调用点零改动)。保留 2 参数签名同理。
"""

from __future__ import annotations

from typing import Optional

from app.market_cn.auto.core.market import (
    MarketSpec,
    limit_dn_tol as _limit_dn_tol,
)
# 跌停价是"价格边界"原语 → 唯一实现在 `core.market`（它需要 MarketSpec）。
# 此处**转出**而非重新定义：曾写成一个薄包装函数，结果 `path_parity` A1
# "唯一实现" 闸门报 `limit_dn_price` 出现 2 份（exec.py + market.py）。
# 转出后全树只剩 1 份 `def`，且冻结 `.py` 插件 `from ...core.exec import limit_dn_price`
# 的调用点零改动 —— 语义与调用点都没变，只是不再有第二个函数体。
from app.market_cn.auto.core.market import limit_dn_price  # noqa: F401  (兼容转出)


def is_one_word_limit_dn(bar: dict, dn: Optional[float],
                         spec: Optional[MarketSpec] = None) -> bool:
    """一字跌停: 全天 low==high 且贴住跌停价 (±容差) → 整日无成交可能。"""
    tol = _limit_dn_tol(spec)
    return dn is not None and bar["low"] == bar["high"] \
        and abs(bar["low"] - dn) <= dn * tol


def fill_on_gap(bar_open: float, trigger: float) -> float:
    """卖出跳空穿越按开盘成交: 开盘低于触发价 → 成交价=开盘 (否则=触发价)。"""
    return bar_open if bar_open < trigger else trigger


def fill_blocked_by_limit_dn(fill: float, dn: Optional[float],
                             spec: Optional[MarketSpec] = None) -> bool:
    """卖出成交价触及跌停 (±容差) → 卖不出, 引擎置 pending 顺延次日开盘。"""
    tol = _limit_dn_tol(spec)
    return dn is not None and fill <= dn * (1 + tol)


def fill_intraday(bar: dict, trigger: float, *, side: str = "sell",
                  dn: Optional[float] = None, up: Optional[float] = None,
                  spec: Optional[MarketSpec] = None) -> tuple:
    """bar 内按触发价成交 (盘中口径原子判定, **唯一实现**)。

    返回 ``(fill_price, filled)``:
      - 触线: ``side='sell'`` → ``bar['low'] <= trigger``; ``side='buy'`` → ``bar['high'] >= trigger``;
      - 成交价: 卖 → ``fill_on_gap(open, trigger)`` (开盘已在线下按开盘, 否则按线价);
                买 → ``open if open > trigger else trigger`` (开盘已在线上的按开盘);
      - 不可成交 (filled=False, 引擎据此置 pending_dn 顺延 / 跳过):
        卖 → 成交价贴跌停 (fill_blocked_by_limit_dn); 买 → 涨停不可买;
      - 未触线 → ``(None, False)``。

    设计纪律 (与 core/exec 其他原语一致):
      - **仅做原子判定**, 状态机 (pending_dn / 顺延指针) 仍归各引擎;
      - **买/卖对称复用** ``fill_on_gap`` / ``fill_blocked_by_limit_dn``, 不复制逻辑;
      - **追踪类触发线若需"开盘时已知峰值"防日内先视**, 由引擎先把 trigger 算好
        (如 ``peak_prev*(1+trail/100)``) 再传入, 本原语不读 peak;
      - ``spec=None`` → 默认市场 (冻结 ``.py`` 插件调用点零改动)。
    """
    if side == "sell":
        if bar["low"] <= trigger:
            fill = fill_on_gap(bar["open"], trigger)
            return fill, (not fill_blocked_by_limit_dn(fill, dn, spec))
        return None, False
    if side == "buy":
        if bar["high"] >= trigger:
            fill = bar["open"] if bar["open"] > trigger else trigger
            # 涨停不可买 (up 为涨停价; 无 up 时不判)
            if up is not None and fill >= up * (1 - _limit_dn_tol(spec)):
                return fill, False
            return fill, True
        return None, False
    raise KeyError(f"未实现的成交方向 side={side!r} (支持: sell / buy)")


def _slot_ohlc(slot: dict) -> tuple:
    """槽位取价: 兼容两种序列形态 —— 帧/拼接槽位 (``o/h/l/c``) 与 bar 形态 (``open/...``)。"""
    if "o" in slot:
        return float(slot["o"]), float(slot["h"]), float(slot["l"]), float(slot["c"])
    return (float(slot["open"]), float(slot["high"]),
            float(slot["low"]), float(slot["close"]))


def replay_sell_intraday(minute_bars, *, entry_price: Optional[float] = None,
                         stop_line: Optional[float] = None,
                         trail_pct: Optional[float] = None,
                         require_profit: Optional[float] = None,
                         dn: Optional[float] = None, peak: float = 0.0,
                         spec: Optional[MarketSpec] = None) -> tuple:
    """在**当日分钟序列**上按时间顺序重放卖出腿 (硬止损 → 追踪), 返回首个可成交槽位。

    与 ``fill_intraday`` 的分工: 后者 = **单 bar** 的原子成交判定; 本原语 = 把该判定
    **按分钟顺序扫完一整天**, 从而知道日内先后 —— 消除日线近似腿的结构性失真
    ("low 触线但不知 先跌穿后收回 / 先冲高后跌穿")。成交价计算**完全复用**
    ``fill_intraday`` (→ 同一份 ``fill_on_gap`` / ``fill_blocked_by_limit_dn``),
    不新增第二份成交语义。

    参数:
      - ``stop_line``: 硬止损线 (D1 起恒存在) → ``槽位.low <= stop_line`` 即触发;
      - ``trail_pct``: 追踪止损百分比 (负值, 如 -6.0) → 线 = **该槽位开始时已知的峰值**
        × ``(1+trail_pct/100)`` (峰值逐槽推进 ⇒ 天然"开盘时已知", 无日内先视伪影);
      - ``require_profit``: 给定则追踪腿成交价须 ``> require_profit`` (镜像日线 ``ret>0`` 门);
      - ``dn``: 跌停价; 触线但成交价贴跌停 → 该槽位不可成交, **继续扫后续槽位**
        (跌停打开即可卖; 全天贴死 → 返回 None, 顺延语义仍归引擎);
      - ``peak``: 进入本日之前已知的峰值 (引擎的 ``peak_prev``)。

    返回 ``(fill, mi, peak_after)``; 未触发 → ``(None, None, peak_after)``。
    ``peak_after`` = ``max(peak, 扫过槽位的最高价)``, 供引擎结转 (与日线口径的
    ``max(peak, 当日high)`` 在分钟完整时一致)。

    设计纪律 (同 ``fill_intraday``): **只做原子扫描 + 成交判定**;
    状态机 (pending_dn / 顺延指针 / 到期) 仍归各引擎; 同槽位内**先止损后追踪**
    (与日线分支的判定顺序一致, 避免两腿优先级分叉)。
    """
    peak_cur = float(peak or 0.0)
    if not minute_bars:
        return None, None, peak_cur
    for k, slot in enumerate(minute_bars):
        o, h, l, _c = _slot_ohlc(slot)
        if l <= 0:
            continue
        bar = {"open": o, "high": h, "low": l, "close": _c}
        if stop_line is not None and l <= stop_line:
            fill, ok = fill_intraday(bar, stop_line, side="sell", dn=dn, spec=spec)
            if ok:
                return fill, k, max(peak_cur, h)
        if trail_pct is not None and peak_cur > 0:
            line = peak_cur * (1 + float(trail_pct) / 100.0)
            if l <= line:
                fill, ok = fill_intraday(bar, line, side="sell", dn=dn, spec=spec)
                if ok and (require_profit is None or fill > float(require_profit)):
                    return fill, k, max(peak_cur, h)
        if h > peak_cur:
            peak_cur = h
    return None, None, peak_cur
