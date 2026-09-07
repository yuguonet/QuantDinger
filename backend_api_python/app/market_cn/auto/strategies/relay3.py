"""strategies/relay3.py — 3板接力策略 (StrategyBase 插件实现, Phase 2 迁移)

策略来源: 2026-09-06 全量回测 (150天/9996接力样本/112笔1m出场模拟)。
本文件是 relay3 的**唯一实现**; 旧 auto/relay3.py 已改为 facade 转发到本模块。

入场 (D0 盘后扫描 → D1 竞价):
  池子: 昨日恰为 3 连板的票 (主板 9.8%/创科 19.8% 阈值, 排除 ST/北交所)
  特征: MA 多头排列 (MA5>MA10>MA20>MA60, 截至 D0 收盘)
  竞价: D1 开盘 gap ∈ (-2%, +9%) — 不追一字板 (>9% 无法成交), 低开超 -2% 放弃
  名额: 每日最多 2 只 (config.json strategies.relay3.daily_limit)

出场 (D1 盘中 S4+止损, D2+ 延续):
  - D1 触及涨停 → 持有 (涨停日豁免一切卖出判定)
  - 封板后炸板 (最新价 < 涨停价*99.5%) → 立即卖出 (S4 核心)
  - 盘中硬止损 -5% (monitor 主循环, 不在本策略)
  - D1 收盘未封板 → 尾盘卖; D1 封板守住 → D2 起转 holding:
    止损-5% / 追踪-8%(自D1高点) / 到期3天强制离场

易错点:
  - 连续涨停数判定以**收盘价**比前收, 含当日 → board_height=3 表示"昨日至今恰好3连板";
  - eval_exit_day_close 的 seg 索引约定: seg[0]=买入日, seg[1]=D1 —— 勿改成 seg[-1];
  - 涨停价按 entry_price (非 signal_price) 推算, 与回测口径一致。
"""
from __future__ import annotations

from datetime import datetime

from app.market_cn.auto.common.market import get_board_type, is_limit_up
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "relay3"
STRATEGY_LABEL = "3板接力"

# 回测口径参数 (与 rally_backtest_report.html 一致; config.json params 可覆盖)
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

# 用于 dragon_store 注册 (extra 落库白名单)
SIGNAL_EXTRA_KEYS = ("board_height", "ma_bull", "lu_vol_ratio", "rsi", "gap_hint")


# ================================================================
# 特征计算 (纯函数, 无 IO)
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
    """D0 收盘后的日线特征 (供扫描判定与 extra 落库)。"""
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
# 三决策辅助 (独立函数, 类方法与 facade 共用)
# ================================================================

def gap_buyable(gap: float) -> bool:
    """D1 开盘 gap 过滤: (-2%, +9%)。"""
    return PARAMS["gap_min"] <= gap <= PARAMS["gap_max"]


def entry_stop(entry_price: float, code: str) -> float:
    """-5% 盘中硬止损价。"""
    return round(entry_price * (1 + PARAMS["stop_pct"] / 100), 3)


def eval_exit_live(row, snap_rows, entry_price: float):
    """盘中/收盘出场判定 (S4+止损)。

    snap_rows: realtime_snapshot 当日序列 (时间升序)。
    返回 (reason, exit_price) 或 (None, None)。
    """
    if not snap_rows or entry_price <= 0:
        return None, None
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
    return None, None


def eval_exit_day_close(bars, entry_idx: int, entry_price: float, code: str, d1_held_since):
    """收盘出场重放 (D1: 未封板→尾盘卖; 封板→持有到到期/追踪)。

    bars: 日K+当日合成bar; entry_idx: 买入日索引; d1_held_since: D1 日期 str。
    返回 (reason, exit_price) 或 (None, None)。
    """
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


# ================================================================
# StrategyBase 插件实现
# ================================================================

def _signal_to_legacy_dict(sig: Signal) -> dict:
    """Signal → 旧 relay3_today_d0_signals 的 dict 形态 (facade 兼容层)。"""
    ex = sig.extra or {}
    return {
        "strategy": STRATEGY_KEY,
        "style": "r3",
        "code": sig.code,
        "signal_date": sig.time,
        "signal_price": sig.price,
        "lu_date": ex.get("lu_date"),
        "board_height": ex.get("board_height"),
        "ma_bull": ex.get("ma_bull"),
        "lu_vol_ratio": ex.get("lu_vol_ratio"),
        "rsi": ex.get("rsi"),
        "score": sig.score,
    }


@register
class Relay3Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "limit_up"      # U1~U4 锚定最近涨停日 (3板日; Phase 3 顺手修复旧扫描漏过滤)
    entry_style = "r3"
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(PARAMS)

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        """昨日恰3连板 + MA多头 → Signal。

        as_of=k: 只用 bars[:k+1] 判定 (回测防未来函数); None 与旧 *_today_d0_signals 语义一致。
        """
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        if not bars or len(bars) < 67:
            return []
        # 排除北交所/ST (ST 由上层过滤, 与 dragon_scan 主循环口径一致)
        if code.startswith(("8", "4", "92")):
            return []
        feats = calc_features(bars, code)
        if feats.get("board_height") != p["board_height"]:
            return []
        if p["ma_bull"] and not feats.get("ma_bull"):
            return []
        last = bars[-1]
        score = 60 + int(min(20, max(0, (feats.get("rsi") or 0) - 50) / 2))  # 60~80 简单质量分
        return [Signal(
            code=code,
            time=last["time"],
            score=score,
            price=float(last["close"]),
            label="3板接力",
            extra={
                "style": "r3",
                "lu_date": last["time"],
                "board_height": feats.get("board_height"),
                "ma_bull": feats.get("ma_bull"),
                "lu_vol_ratio": feats.get("lu_vol_ratio"),
                "rsi": feats.get("rsi"),
            },
        )]

    # ---- D1 竞价处置 (monitor ~09:25) ----
    def entry_decision(self, row, snap=None, **params):
        """gap = (open / prev_close - 1)*100, prev_close 取快照 previousClose, 兜底 signal_price。

        snap=None (快照缺失) → 不可买 (与 monitor 中 open_px<=0 → skip 一致, 不强买)。
        """
        p = self.merged_params(params or None)
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = (open_px / prev_close - 1) * 100
        if p["gap_min"] <= gap <= p["gap_max"]:
            return EntryDecision(True, f"gap={gap:.2f}% 可买")
        return EntryDecision(False, f"gap={gap:.2f}% 越界({p['gap_min']}~{p['gap_max']}%)")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """D1 收盘确认: 封板守住 → holding; 未封板 → 尾盘卖兜底 (S4)。

        snap={"series":[...当日快照序列]}; d1_chg 按 entry_price 基准 (涨停价推算口径)。
        返回 None = 无法判定, monitor 不转移。
        """
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        entry = float(row.get("entry_price") or 0)
        if entry <= 0:
            return None
        th = 0.198 if get_board_type(row.get("code", "")) == "gem_star" else 0.098
        limit_price = round(entry * (1 + th), 2)
        sealed = any(float(x.get("high") or 0) >= limit_price - 0.001 for x in series)
        last_px = float(series[-1].get("last") or 0)
        d1_chg = round((last_px / entry - 1) * 100, 2) if last_px > 0 else None
        if sealed and last_px >= limit_price * 0.995:
            return ConfirmDecision(True, "sealed_hold", d1_chg=d1_chg,
                                   detail={"confirm": "sealed_hold"})
        return ConfirmDecision(False, "S4未封板尾盘卖", d1_chg=d1_chg,
                               exit_price=round(last_px, 3) if last_px > 0 else None)

    def quality_key(self, row):
        """3板接力无额外质量分 (旧 else 分支 confirm_chg 口径 → 恒 0, 保持原排序行为)。"""
        return super().quality_key(row)

    def initial_stop(self, code, entry_price):
        """盘中硬止损 -5% (stop_pct)。"""
        return round(entry_price * (1 + self.merged_params()["stop_pct"] / 100), 3)

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """snap=None: hold (盘中硬止损兜底在 monitor 主循环);
        snap={"mode":"live","series":[...]}: 盘中炸板判定;
        snap={"mode":"day_close","bars":[...],"entry_idx":int}: 收盘重放 (回测/盘后复盘)。"""
        if not isinstance(snap, dict):
            return ExitDecision("hold")
        entry_price = float(row.get("entry_price") or 0)
        mode = snap.get("mode")
        if mode == "live":
            reason, price = eval_exit_live(row, snap.get("series") or [], entry_price)
        elif mode == "day_close":
            reason, price = eval_exit_day_close(
                snap.get("bars"), snap.get("entry_idx"), entry_price,
                row.get("code"), row.get("entry_date"))
        else:
            return ExitDecision("hold")
        if reason:
            return ExitDecision("exit", reason=reason, price=float(price or 0))
        return ExitDecision("hold")
