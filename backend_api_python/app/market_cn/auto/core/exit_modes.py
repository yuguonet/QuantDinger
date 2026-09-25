"""ide/exit_modes.py — 出场模式模块（M2）。

出场 = 「成交语义（T+1 / 跳空成交 / 跌停不可卖）+ 出场规则（止损 / 追踪 / 到期 / 信号）」。
本模块是 IDE 侧**唯一**的出场入口：策略 YAML 只写 `exit.mode: <key>`，engine 按 key 分派，
策略文件不含任何 python，也不得自己实现成交假设。

成交假设的原子实现仍归 `core/exec.py`（框架不变量），本模块只做"模式选择 + 参数接线"。

统一适配器签名（所有 mode 实现必须一致）：
    fn(bars, entry_idx, entry_price, *, code, board_type, params, diag) -> Optional[dict]
- entry_idx / entry_price：入场日索引与成交价（由 entry_modes 决定）。
- diag：入场侧派生量（d1_gap / d1_change / d1_limit_up …），供出场规则使用；无则 {}。
- 返回 dict 会被 `**` 展开进 trade（字段与 python 参考版逐笔一致）；None = 放弃该笔。

⚠️ 2026-09-26 P1-9 层反转收编：本模块**不再 import 任何 strategies.***（core 零策略知识）。
    各策略引擎由策略文件在注册时经 `register_exit(mode, fn)` 挂入 EXIT_MODES（依赖反转）。
    历史 M2 过渡的 import 垫片已移除；未注册 mode → KeyError fail-fast。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# mode → 适配器
EXIT_MODES: Dict[str, Callable[..., Optional[Dict[str, Any]]]] = {}


def register_exit(mode: str, fn: Callable[..., Optional[Dict[str, Any]]]) -> None:
    """注册一个出场模式实现（同名覆盖）。

    由策略模块在 import 尾部调用（strategies→core 方向合法）；core 不反向 import 策略。
    """
    EXIT_MODES[mode] = fn


def run_exit(mode: str, *, bars: List[Dict[str, Any]], entry_idx: int, entry_price: float,
             code: str, board_type: str, params: Dict[str, Any],
             diag: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """按 exit.mode 分派出场引擎。未注册的 mode → KeyError（fail-fast，不静默兜底）。

    调用前须保证策略包已 autodiscover（strategy_cli / evaluate.ensure_gate_init /
    backtest.run_all 均已具备）；否则 mode 未注册会 fail-fast 暴露加载顺序问题。
    """
    fn = EXIT_MODES.get(mode)
    if fn is None:
        # 惰性 autodiscover: 策略模块 import 时会 register_exit; 若调用方尚未
        # 加载策略包, 先补一次 (幂等), 避免加载顺序导致 fail-fast 误报。
        if not EXIT_MODES:
            from app.market_cn.auto.strategies import autodiscover
            autodiscover()
            fn = EXIT_MODES.get(mode)
    if fn is None:
        raise KeyError(f"未注册的出场模式 exit.mode={mode!r}（已注册: {sorted(EXIT_MODES)}）")
    return fn(bars, entry_idx, entry_price, code=code, board_type=board_type,
              params=params, diag=diag or {})


def _bp(params, board_type, name):
    """板块感知参数取值（值可为 {board: 值} 或标量）。与 strategy_funcs.pk 同语义。"""
    v = params.get(name)
    if isinstance(v, dict):
        if board_type in v:
            return v[board_type]
        return v.get("default")
    return v


# ================================================================
# 通用模式（不依赖策略, 留在 core）
# ================================================================

def _d1_open(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """盘中窗口策略出场：次交易日(D1)开盘卖 —— 与 base.StrategyBase.intraday_exit 默认同语义。

    (knife_catch / tail_oversold 的 14:56 尾盘买入 → D1 开盘卖；成交价=次交易日 open)。
    """
    if entry_idx + 1 >= len(bars):
        return None
    nxt = bars[entry_idx + 1]
    px = float(nxt.get("open") or 0)
    if px <= 0:
        return None
    return {"exit_date": str(nxt["time"])[:10], "exit_price": round(px, 3),
            "exit_day": 1, "exit_reason": "d1_open",
            "return_pct": round((px / entry_price - 1) * 100, 2)}


register_exit("d1_open", _d1_open)
