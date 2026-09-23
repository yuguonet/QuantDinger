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

⚠️ M2 过渡说明：以下 mode 目前**复用已通过逐笔等价验收的策略出场引擎**——
    combo       → dragon_callback.run_backtest_dragon_callback（trail+stop+max_hold+peak+signal）
    v1_combo    → v1._run_backtest（含 V1 动量 D2 清仓）
    relay3_s4   → relay3.run_backtest_relay3（日线近似 S4 炸板）
这样做是为了在不改生产引擎的前提下，先把"出场由 YAML 选择"这条链路打通（零回归）。
M2.x 会把三者收敛为参数化通用 `combo`/`signal`（成交语义只留一份），届时 mode 名合并。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# break 是 Python 关键字: `from ...strategies.break import X` 为 SyntaxError,
# 故走 importlib 动态导入 (2026-09-23 由 break_buy.py 更名而来)。
from importlib import import_module as _import_module
_run_backtest_breakbuy = _import_module(
    "app.market_cn.auto.strategies.break")._run_backtest_breakbuy
from app.market_cn.auto.strategies.dragon_callback import run_backtest_dragon_callback
from app.market_cn.auto.strategies.relay3 import run_backtest_relay3
from app.market_cn.auto.strategies.v1 import _run_backtest as _v1_run_backtest

# mode → 适配器
EXIT_MODES: Dict[str, Callable[..., Optional[Dict[str, Any]]]] = {}


def register_exit(mode: str, fn: Callable[..., Optional[Dict[str, Any]]]) -> None:
    """注册一个出场模式实现（同名覆盖）。"""
    EXIT_MODES[mode] = fn


def run_exit(mode: str, *, bars: List[Dict[str, Any]], entry_idx: int, entry_price: float,
             code: str, board_type: str, params: Dict[str, Any],
             diag: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """按 exit.mode 分派出场引擎。未注册的 mode → KeyError（fail-fast，不静默兜底）。"""
    fn = EXIT_MODES.get(mode)
    if fn is None:
        raise KeyError(f"未注册的出场模式 exit.mode={mode!r}（已注册: {sorted(EXIT_MODES)}）")
    return fn(bars, entry_idx, entry_price, code=code, board_type=board_type,
              params=params, diag=diag or {})


# ================================================================
# 适配器实现（M2 过渡：复用已验收的策略引擎）
# ================================================================
def _dragon_combo(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """龙回头：反转日收盘买入的 combo 出场（追踪/止损/到期/峰值逃顶/分段信号）。"""
    return run_backtest_dragon_callback(bars, entry_idx, entry_price, board_type=board_type)


def _v1_combo(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """V1：止损/追踪/到期 + V1 日内动量 D2 清仓。d1_* 由入场侧传入（口径与回测一致）。"""
    return _v1_run_backtest(
        bars, entry_idx, entry_price,
        params["hold"], params["stop"], params["trail"], board_type,
        is_v1=True,
        d1_limit_up=diag.get("d1_limit_up"),
        d1_change=diag.get("d1_change"),
        d1_gap=diag.get("d1_gap"),
    )


def _relay3_s4(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """relay3：日线近似 S4（D1 未/未封板尾盘卖；封板延续 → 追踪-8% / 到期 3 天）。"""
    return run_backtest_relay3(bars, entry_idx, entry_price, code, params)


def _bp(params, board_type, name):
    """板块感知参数取值（值可为 {board: 值} 或标量）。与 strategy_funcs.pk 同语义。"""
    v = params.get(name)
    if isinstance(v, dict):
        if board_type in v:
            return v[board_type]
        return v.get("default")
    return v


def _break_combo(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """断板：combo（止损 / 追踪(需 ret>0) / 峰值逃顶 / 到期）——分板块阈值。

    fill_mode (2026-09-21): 成交时点透传 `close`(默认) / `intraday_stop` / `intraday`；
    门表未写该参数 → None → 引擎回落 "close"（与 .py 生产链同默认, 逐笔等价不破）。
    """
    return _run_backtest_breakbuy(
        bars, entry_idx, entry_price,
        _bp(params, board_type, "hold_days"),
        _bp(params, board_type, "stop_loss"),
        _bp(params, board_type, "trailing_stop"),
        board_type,
        _bp(params, board_type, "fill_mode"),
    )


register_exit("combo", _dragon_combo)
register_exit("v1_combo", _v1_combo)
register_exit("relay3_s4", _relay3_s4)
register_exit("break_combo", _break_combo)


def _g56_no_trail(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """g56：无追踪纯 7d/-8%（2026-09-17 出场研究定稿）—— 复用 g56._exit_no_trail。

    (2026-09-23): 出场阈值改由 params 注入 (g56.yaml 的 hold_days / stop_loss)；
    门表未写 -> None -> 引擎回落 g56.py 模块常量，逐笔等价不破。
    """
    from app.market_cn.auto.strategies.g56 import _exit_no_trail
    return _exit_no_trail(bars, entry_idx, entry_price,
                          _bp(params, board_type, "hold_days"),
                          _bp(params, board_type, "stop_loss"))


def _d1_open(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """盘中窗口策略出场：次交易日(D1)开盘卖 —— 逐字镜像 base.StrategyBase.intraday_exit 默认。

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


register_exit("g56_no_trail", _g56_no_trail)
register_exit("d1_open", _d1_open)
