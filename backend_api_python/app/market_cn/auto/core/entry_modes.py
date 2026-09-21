"""ide/entry_modes.py — 入场模式模块（M2）。

入场 = 「成交时点 + 成交价 + 次日过滤」。本模块是 IDE 侧**唯一**的入场入口：
策略 YAML 只写 `entry.mode` 与过滤配置，engine 据此决定入场点位；策略文件不含 python。

支持的模式（与 docs/自动策略IDE架构设计.md §4.1 对应）：
    close : 信号日收盘买入（D0 尾盘, dragon_callback）
    open  : 次日(D1)开盘买入 + gap 过滤（v1 / relay3）
    intraday : 盘中触发买入（日线近似腿: 当日 high 触 trigger 即按线价成交; 1m 真实腿见 P4）

`open` 模式的过滤项（全部可选，缺省不拦截）：
    gap_min           : 低于此 gap% → 淘汰（可为 {board: 值} 分板块）
    gap_max           : 高于/等于此 gap% → 淘汰（分板块）
    require_d1_close_up: true → D1 收阴(close<D0.close)即淘汰（镜像 v1 的 d1_change<0）
    gap_exclude_band  : [lo, hi) 区间内的 gap 淘汰（分板块；镜像 v1 主板高开 3~5% 不入场）

配置值可以是**参数名（字符串）**，运行时从 `params` 解析 —— 这样阈值仍可被网格扫描 / 敏感性分析
（`entry:` 块只引用参数名，不写死字面量）。解析规则见 `_resolve`。

返回 (site, None) 或 (None, reason)：
    site = {"entry_idx","entry_price","entry_date","diag"}；diag 供出场模块使用。
    reason 仅用于调试/归因（不参与等价判定）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from app.market_cn.auto.core.exec import fill_intraday  # 盘中成交价 (复用 P1 框架原语)

# 板块类型取值：main（沪深主板）/ gem_star（创业板 / 科创板）
_BOARD_KEYS = ("main", "gem_star")


def _resolve(v: Any, board_type: str, params: Dict[str, Any]) -> Any:
    """把配置值解析为具体数值/列表。

    - 字符串 → params[名]（参数引用，保网格可扫）
    - dict   → 按 board_type 取（支持 "default" 兜底）；缺板块 → None（该板块不设此门）
    - list   → 逐元素解析
    """
    if v is None:
        return None
    if isinstance(v, str):
        return params.get(v)
    if isinstance(v, dict):
        if board_type in v:
            return _resolve(v[board_type], board_type, params)
        if "default" in v:
            return _resolve(v["default"], board_type, params)
        return None
    if isinstance(v, list):
        return [_resolve(x, board_type, params) for x in v]
    return v


def resolve_entry(cfg: Dict[str, Any], bars: List[Dict[str, Any]], i: int,
                  board_type: str, params: Dict[str, Any]):
    """解析入场点位与过滤。返回 (site|None, reason)。"""
    mode = str((cfg or {}).get("mode", "open")).lower()

    if mode == "close":
        # 信号日(D0)收盘买入：入场日 = 决策日 i 本身
        if i < 0 or i >= len(bars):
            return None, "no_bar"
        px = _f(bars[i], "close")
        if px <= 0:
            return None, "bad_price"
        return {"entry_idx": i, "entry_price": px, "entry_date": bars[i]["time"],
                "diag": {}}, None

    if mode == "open":
        # 次日(D1)开盘买入
        if i + 1 >= len(bars):
            return None, "no_d1"
        d0, d1 = bars[i], bars[i + 1]
        c0 = _f(d0, "close")
        px = _f(d1, "open")
        if c0 <= 0 or px <= 0:
            return None, "bad_price"

        gap = (px / c0 - 1) * 100
        chg = (_f(d1, "close") / c0 - 1) * 100

        gmin = _resolve(cfg.get("gap_min"), board_type, params)
        gmax = _resolve(cfg.get("gap_max"), board_type, params)
        band = _resolve(cfg.get("gap_exclude_band"), board_type, params)

        if gmin is not None and gap < gmin:
            return None, "gap_min"
        if cfg.get("require_d1_close_up") and chg < 0:
            return None, "d1_down"
        if gmax is not None and gap >= gmax:
            return None, "gap_max"
        if band and len(band) == 2 and band[0] is not None and band[1] is not None \
                and band[0] <= gap < band[1]:
            return None, "gap_band"

        return {"entry_idx": i + 1, "entry_price": px, "entry_date": d1["time"],
                "diag": {"d1_gap": gap, "d1_change": chg, "intraday": chg - gap}}, None

    if mode == "intraday":
        return _resolve_entry_intraday(cfg, bars, i, board_type, params)

    raise KeyError(f"未实现的入场模式 entry.mode={mode!r}（支持: close / open / intraday）")


def _resolve_entry_intraday(cfg: Dict[str, Any], bars: List[Dict[str, Any]], i: int,
                           board_type: str, params: Dict[str, Any]):
    """盘中触发入场 (日线近似腿; 1m 真实腿由 run_all 时间线引擎在 P4 接线)。

    - 入场日 = 决策日 i 本身 (offset 0, 与 close 同);
    - 触发价 trigger 来自 entry.trigger (字符串→params 引用 / 数字 / {board:值});
    - 日线近似腿: 复用 P1 原语 ``exec.fill_intraday(bar, trigger, side='buy')`` ——
      当日 high >= trigger 即触发, 成交价 = max(open, trigger) (开盘已在线上的按开盘),
      涨停不可买 → filled=False; 未触线 → (None, False) (与其余引擎同一成交语义)。
    - 1m 真实腿 (P4): 在 run_all_intraday 槽位序列里找首个满足触发条件的槽位,
      成交价 = 该槽位 open —— 届时由引擎调用本模式并把 1m bar 传入, 判定逻辑同构。
    """
    if i < 0 or i >= len(bars):
        return None, "no_bar"
    bar = bars[i]
    trigger = _resolve(cfg.get("trigger"), board_type, params)
    if trigger is None or trigger <= 0:
        return None, "no_trigger_cfg"
    px, filled = fill_intraday(bar, trigger, side="buy")
    if px is None:
        return None, "no_trigger"
    if not filled:
        return None, "limit_up_blocked"
    if px <= 0:
        return None, "bad_price"
    return {"entry_idx": i, "entry_price": px, "entry_date": bar["time"],
            "diag": {"intraday_leg": "daily_approx"}}, None


def _f(bar: Dict[str, Any], key: str) -> float:
    """安全取 float（缺值/坏值 → 0.0）。与 functions.Ctx._f 同口径。"""
    if not bar:
        return 0.0
    v = bar.get(key)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
