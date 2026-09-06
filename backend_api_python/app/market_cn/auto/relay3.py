"""relay3.py — 3板接力策略 (自动策略组第4条腿)

策略来源: 2026-09-06 全量回测 (150天/9996接力样本/112笔1m出场模拟)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

入场 (D0 盘后扫描 → D1 竞价):
  池子: 昨日恰为 3 连板的票 (主板 9.8%/创科 19.8% 阈值, 排除 ST/北交所)
  特征: MA 多头排列 (MA5>MA10>MA20>MA60, 截至 D0 收盘)
  竞价: D1 开盘 gap ∈ (-2%, +9%)  — 不追一字板 (>9% 无法成交),
        低开超过 -2% 放弃 (回测中低开 3 板接力为负期望)
  名额: 每日最多 2 只 (信号稀少, n≈0.7只/日)

出场 (D1 盘中 S4+止损, D2+ 延续):
  - D1 触及涨停 → 持有 (涨停日豁免一切卖出判定)
  - 封板后炸板 (最新价 < 涨停价*99.5%) → 立即卖出 (exit_today, S4 核心)
  - 盘中硬止损 -5% (跌破 entry*0.95)
  - D1 收盘未封板且未止损 → 当日尾盘卖 (14:58 出场重放)
  - D1 封板守住 → D2 起转 holding, 改用: 止损-5% / 追踪-8%(自D1高点) / 到期3天
    (3板接力的肉在 D1-D2, 到期3天强制离场)

回测依据 (详见 rally_backtest_report.html 第15/16章):
  入场: 3板+MA多头 n=116, 胜率53.4%, 均+1.40% (开盘卖口径)
  出场: S4+止损5% → 均+2.50%, 盈亏比1.95, 最差单笔-5%
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ================================================================
# 策略常量 (dragon_store.STRATEGIES 注册第4条腿)
# ================================================================

STRATEGY_KEY = "relay3"
STRATEGY_LABEL = "3板接力"

# 回测口径参数 (与 rally_backtest_report.html 一致)
PARAMS = {
    "board_height": 3,          # 昨日恰为3连板
    "ma_bull": True,            # MA多头排列硬门槛
    "gap_min": -2.0,            # D1开盘 gap 下限 %
    "gap_max": 9.0,             # D1开盘 gap 上限 % (排除一字板)
    "daily_limit": 2,           # 每日买入名额
    "stop_pct": -5.0,           # 盘中硬止损 %
    "break_sell_ratio": 0.995,  # 封板后炸板判定: last < limit*0.995
    "hold_days_max": 3,         # 封板延续后最长持有天数
    "trail_after_limit": -8.0,  # 封板延续期的追踪止损 (自D1高点) %
}

# 用于 dragon_store 注册
SIGNAL_EXTRA_KEYS = ("board_height", "ma_bull", "lu_vol_ratio", "rsi", "gap_hint")


# ================================================================
# 特征计算 (复用 dragon_scan.fetch_kline_db 的日K加载)
# ================================================================

def _ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def ma_bull_arrangement(bars) -> bool:
    """MA5>MA10>MA20>MA60 (bars: 截至当日的日K list[dict])"""
    closes = [float(b["close"]) for b in bars]
    m5, m10, m20, m60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)
    if not (m5 and m10 and m20 and m60):
        return False
    return m5 > m10 > m20 > m60


def consecutive_limit_ups(bars, code) -> int:
    """截至最后一根bar的连续涨停天数 (板高)。"""
    from app.market_cn.auto.dragon_core import is_limit_up, get_board_type
    bt = get_board_type(code)
    h = 0
    i = len(bars) - 1
    while i >= 1:
        if is_limit_up(float(bars[i]["close"]), float(bars[i - 1]["close"]), bt):
            h += 1
            i -= 1
        else:
            break
    return h


def calc_features(bars, code) -> dict:
    """D0 收盘后的日线特征 (供扫描与 extra 落库)。"""
    closes = [float(b["close"]) for b in bars]
    vols = [float(b["volume"]) for b in bars]
    feats = {
        "board_height": consecutive_limit_ups(bars, code),
        "ma_bull": 1 if ma_bull_arrangement(bars) else 0,
    }
    # 涨停日量比 (昨日量 / 前5日均量)
    if len(vols) >= 6:
        avg5 = sum(vols[-6:-1]) / 5
        feats["lu_vol_ratio"] = round(vols[-1] / avg5, 2) if avg5 > 0 else None
    # RSI14
    if len(closes) >= 15:
        gains, losses = [], []
        for k in range(len(closes) - 15, len(closes)):
            dd = closes[k] - closes[k - 1]
            gains.append(max(dd, 0))
            losses.append(max(-dd, 0))
        ag, al = sum(gains) / 14, sum(losses) / 14
        feats["rsi"] = round(100 - 100 / (1 + ag / al), 1) if al > 0 else 100.0
    return feats


# ================================================================
# D0 扫描: 昨日3板 + MA多头 → watch_pending
# ================================================================

def relay3_today_d0_signals(bars, code) -> list:
    """判定 target 日收盘后是否产生 3板接力信号。

    bars: 截至当日(含)的日K list[dict] (dragon_scan.fetch_kline_db 输出)。
    返回 list[dict], 字段与 dragon_cb_today_d0_signals 对齐 + strategy=relay3。
    """
    if not bars or len(bars) < 67:
        return []
    # 排除北交所/ST (与 dragon_scan 主循环的过滤保持一致; ST 由上层过滤)
    if code.startswith(("8", "4", "92")):
        return []
    feats = calc_features(bars, code)
    if feats.get("board_height") != PARAMS["board_height"]:
        return []
    if PARAMS["ma_bull"] and not feats.get("ma_bull"):
        return []
    last = bars[-1]
    return [{
        "strategy": STRATEGY_KEY,
        "style": "r3",
        "code": code,
        "signal_date": last["time"],
        "signal_price": float(last["close"]),
        "lu_date": last["time"],
        "board_height": feats.get("board_height"),
        "ma_bull": feats.get("ma_bull"),
        "lu_vol_ratio": feats.get("lu_vol_ratio"),
        "rsi": feats.get("rsi"),
        "score": 60 + int(min(20, max(0, (feats.get("rsi") or 0) - 50) / 2)),  # 60~80 简单质量分
    }]


# ================================================================
# D1 竞价过滤与止损价 (dragon_monitor 调用)
# ================================================================

def gap_buyable(gap: float) -> bool:
    """D1 开盘 gap 过滤: (-2%, +9%)。"""
    return PARAMS["gap_min"] <= gap <= PARAMS["gap_max"]


def entry_stop(entry_price: float, code: str) -> float:
    """-5% 盘中硬止损价。"""
    return round(entry_price * (1 + PARAMS["stop_pct"] / 100), 3)


# ================================================================
# D1+ 出场判定 (dragon_monitor._eval_exit_day_close 的 relay3 分支)
# ================================================================

def eval_exit_live(row, snap_rows, entry_price: float):
    """盘中/收盘出场判定 (relay3)。

    snap_rows: realtime_snapshot 当日序列 (时间升序, dragon_monitor.fetch_day_snapshots 输出)。
    返回 (reason, exit_price) 或 (None, None)。

    规则 (S4+止损):
      1. 盘中任意时刻 last <= entry*(1+stop%)        → 盘中止损 (monitor 主循环已做, 此处兜底)
      2. 当日曾触及涨停价 → 封板状态; last < limit*0.995 → 炸板卖出
      3. 收盘(14:58重放): 当日未封板 → 尾盘卖 "S4未封板尾盘卖"
      4. 收盘: 当日封板守住 → 继续持有 (返回 None, D2 起 holding)
    """
    if not snap_rows or entry_price <= 0:
        return None, None
    from app.market_cn.auto.dragon_core import get_board_type
    th = 0.198 if get_board_type(row["code"]) == "gem_star" else 0.098
    limit_price = round(entry_price * (1 + th), 2)
    break_th = limit_price * PARAMS["break_sell_ratio"]

    touched_limit = False
    for r in snap_rows:
        hi = float(r.get("high") or 0)
        last = float(r.get("last") or 0)
        if hi >= limit_price - 0.001:
            touched_limit = True
        if touched_limit and 0 < last < break_th:
            return "炸板卖出(S4)", last
    # 收盘重放时 (14:58): 未封板 → 尾盘卖
    return None, None


def eval_exit_day_close(bars, entry_idx: int, entry_price: float, code: str, d1_held_since):
    """收盘出场重放 (relay3 分支, dragon_monitor._eval_exit_day_close 调用)。

    bars: 日K+当日合成bar; entry_idx: 买入日索引; d1_held_since: D1 日期 str。
    返回 (reason, exit_price) 或 (None, None)。
    """
    from app.market_cn.auto.dragon_core import get_board_type
    if bars is None or entry_idx is None or entry_price <= 0:
        return None, None
    th = 0.198 if get_board_type(code) == "gem_star" else 0.098
    limit_price = round(entry_price * (1 + th), 2)
    today_idx = len(bars) - 1
    today = bars[-1]["time"]
    last_bar = bars[-1]

    # 持有超过到期天数 (从 D1 算起) → 到期卖
    try:
        d1 = datetime.strptime(str(d1_held_since or today), "%Y-%m-%d")
        held_days = (datetime.strptime(str(today), "%Y-%m-%d") - d1).days + 1
    except Exception:
        held_days = 1
    if held_days > PARAMS["hold_days_max"]:
        return f"到期{PARAMS['hold_days_max']}天", last_bar["close"]

    # 当日(及D1以来)是否封板住
    seg = bars[entry_idx:today_idx + 1]
    d1_bar = seg[1] if len(seg) >= 2 else (seg[0] if seg else None)
    if d1_bar is None:
        return None, None
    d1_touched = float(d1_bar["high"]) >= limit_price - 0.001
    # 昨日(D1)封板但今日炸板/走弱: 追踪止损
    if d1_touched and len(seg) >= 3:
        d1_high = max(float(b["high"]) for b in seg[1:])
        ret_from_high = (float(last_bar["close"]) / d1_high - 1) * 100 if d1_high > 0 else 0
        if ret_from_high <= PARAMS["trail_after_limit"]:
            return f"追踪止损{PARAMS['trail_after_limit']}%", last_bar["close"]
        if held_days > PARAMS["hold_days_max"]:
            return f"到期{PARAMS['hold_days_max']}天", last_bar["close"]
        return None, None  # 封板延续 → 继续持有
    # D1 未封板: 理论上应在 D1 尾盘已卖 (S4), 此处兜底 (漏网/停牌复牌等)
    if not d1_touched:
        return "S4未封板尾盘卖(补)", last_bar["close"]
    return None, None
