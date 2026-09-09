#!/usr/bin/env python3
"""exec_cn.py — A股执行约束内建 (C 阶段, 2026-09-09)

用途: 把散落在三个出场引擎里的 A股执行约束**原语**收敛为框架不变量。
     引擎/策略禁止再手写这些判定 (代码审查项), 显式例外须注明回测依据。

框架不变量 (语义声明, 引擎结构保证):
  1. 只能做多 — 全部引擎无做空路径;
  2. T+1 — 买入当日 (d=1 持有首日) 不可卖, 所有引擎从 d>=1 的下一根 bar 起判定卖出;
  3. 跳空穿越按开盘成交 — 触发日开盘优于触发价时, 成交价=开盘价 (可实现);
  4. 跌停无法卖出 — 一字跌停整日跳过; 触发成交触及跌停 → 顺延次日开盘强平
     (连续一字逐日顺延); 到期日无法成交同样顺延。
  5. 涨停无法买入 — 涨停 buy 不追 (由判定/入场过滤保证, 如 relay3 gap<=9%)。

易错点:
  - 判定容差 0.002 (0.2%) 用于吸收 qfq 复权微差, 勿"修正"为 0;
  - 原语只做原子判定, 状态机 (pending_dn/last_unfilled/顺延指针) 仍归各引擎——
    因为三引擎的顺延语义不同 (V1 动量清仓/断板收盘判定/龙回头分段追踪), 硬抽象会造出 bug。
"""
from __future__ import annotations

LIMIT_DN_TOL = 0.002   # 跌停判定相对容差


def limit_dn_price(prev_close, board_type):
    """跌停价: main 10% / 其余(创业板科创板) 20%。

    收编自 dragon_core._limit_dn_price (与 strategies/dragon_callback 同款)。
    """
    return prev_close * ((1 - 0.10) if board_type == "main" else (1 - 0.20))


def is_one_word_limit_dn(bar, dn):
    """一字跌停: 全天 low==high 且贴住跌停价 (±0.2%容差) → 整日无成交可能。"""
    return dn is not None and bar["low"] == bar["high"] \
        and abs(bar["low"] - dn) <= dn * LIMIT_DN_TOL


def fill_on_gap(bar_open, trigger):
    """卖出跳空穿越按开盘成交: 开盘低于触发价 → 成交价=开盘 (否则=触发价)。"""
    return bar_open if bar_open < trigger else trigger


def fill_blocked_by_limit_dn(fill, dn):
    """卖出成交价触及跌停 (±0.2%容差) → 卖不出, 引擎置 pending 顺延次日开盘。"""
    return dn is not None and fill <= dn * (1 + LIMIT_DN_TOL)
