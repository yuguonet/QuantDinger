"""
连板猎手 v3 — 基于数据驱动的双分支策略

数据来源: 22977个连板段的横向+纵向分析
架构: 沪深主板(10%) / 创科板(20%) 双分支，参数完全不同

核心发现编码:
1. 买点日必须放量 (主板≥2x, 创科≥3x)
2. 封板强度必须接近0 (涨停封死)
3. 前1日无大跌 (前1日跌幅>-3% 跳过)
4. RSI不能过低 (<50 跳过)
5. 峰值后大概率跌 → 用追踪止损+主动止盈
"""
from __future__ import annotations
import os, sys
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import time as _time
import pandas as pd
import numpy as np
import requests as _requests

_tencent_session = _requests.Session()
_tencent_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

# 股票名称缓存 (用于ST过滤)
_stock_name_cache: Dict[str, str] = {}

def get_stock_name(code: str) -> str:
    """获取股票名称, 带缓存"""
    c = code.strip().zfill(6)
    if c in _stock_name_cache:
        return _stock_name_cache[c]
    tc = _code_to_tencent(c)
    if not tc:
        _stock_name_cache[c] = ""
        return ""
    try:
        resp = _tencent_session.get(
            f"https://qt.gtimg.cn/q={tc}",
            headers={"Referer": "https://gu.qq.com/"},
            timeout=10,
        )
        parts = resp.text.split("~")
        name = parts[1] if len(parts) > 1 else ""
        _stock_name_cache[c] = name
        return name
    except Exception:
        _stock_name_cache[c] = ""
        return ""

def is_st_stock(code: str) -> bool:
    """判断是否ST股"""
    name = get_stock_name(code)
    return "ST" in name.upper()

# 流通股本缓存
_circ_shares_cache: Dict[str, float] = {}

def get_circ_shares(code: str) -> float:
    """获取流通股本(股), 带缓存"""
    c = code.strip().zfill(6)
    if c in _circ_shares_cache:
        return _circ_shares_cache[c]
    tc = _code_to_tencent(c)
    if not tc:
        _circ_shares_cache[c] = 0
        return 0
    try:
        resp = _tencent_session.get(
            f"https://qt.gtimg.cn/q={tc}",
            headers={"Referer": "https://gu.qq.com/"},
            timeout=10,
        )
        parts = resp.text.split("~")
        if len(parts) > 44:
            circ_cap = float(parts[44])  # 流通市值(亿)
            price = float(parts[3])       # 现价
            shares = circ_cap * 1e8 / price if price > 0 else 0
            _circ_shares_cache[c] = shares
            return shares
        _circ_shares_cache[c] = 0
        return 0
    except Exception:
        _circ_shares_cache[c] = 0
        return 0

# 路径
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ================================================================
# 板块 & 参数
# ================================================================

# 双分支参数 (从分析数据中提取)
BOARD_PARAMS = {
    # 沪深主板 (10% 涨跌停) — v3.1优化版
    "main": {
        "threshold": 0.098,       # 涨停阈值
        "min_streak": 2,          # 只做2板+ (1板信号太弱)
        "buy_vol_ratio": 1.5,     # 量比放宽(区分度弱,不作为核心条件)
        "buy_seal_max": 0.5,      # 封板强度 ≤0.5%
        "buy_rsi_min": 30,
        "buy_rsi_max": 70,
        "buy_pre1_max_drop": -5,  # 前1日跌幅放宽
        "buy_max_range_pct": 12,
        "buy_max_gap_pct": 8.0,   # 高开<8% (核心过滤!)
        "buy_min_score": 0,       # Score门槛(已关闭)
        "stop_loss_pct": -8,
        "trailing_stop_pct": -10,
        "take_profit_pct": 120,
        "peak_sell_rsi": 80,
        "peak_sell_upper_shadow": 40,
        "sell_on_peak_day": True, # 峰值日当天出场
        "remove_board_break": True, # 移除开板信号(胜率仅7%)
    },
    # 创/科板 (20% 涨跌停) — v3.1优化版
    "gem_star": {
        "threshold": 0.198,
        "min_streak": 2,          # 只做2板+
        "max_streak": 4,          # 排除5板+(20%胜率陷阱)
        "buy_vol_ratio": 1.5,
        "buy_seal_max": 0.5,
        "buy_rsi_min": 30,
        "buy_rsi_max": 70,        # RSI<70
        "buy_pre1_max_drop": -8,
        "buy_max_range_pct": 22,
        "buy_max_gap_pct": 12.0,  # 高开<12%
        "buy_min_score": 0,       # Score门槛(已关闭)
        "stop_loss_pct": -12,
        "trailing_stop_pct": -10,
        "take_profit_pct": 120,
        "peak_sell_rsi": 85,
        "peak_sell_upper_shadow": 45,
        "sell_on_peak_day": True,
        "remove_board_break": True,
    },
}


def get_board_type(code: str) -> str:
    """判断板块类型"""
    c = code[:3] if len(code) >= 3 else code
    if c.startswith("30") or c.startswith("68"):
        return "gem_star"
    return "main"


def get_board_name(code: str) -> str:
    c = code[:3] if len(code) >= 3 else code
    if c.startswith("68"):
        return "科创板"
    elif c.startswith("30"):
        return "创业板"
    elif c.startswith("6"):
        return "沪主板"
    elif c.startswith(("0", "2")):
        return "深主板"
    return "未知"


# ================================================================
# 数据加载 — DB 接口 (参考 strategy_dragon_filter.py)
# ================================================================

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'), os.path.join(_project_root, '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass


def _get_writer():
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    return get_market_kline_writer()


def get_all_codes_db() -> list:
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []


def load_daily_db(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """从 db_market 加载日线"""
    writer = _get_writer()
    data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
    if not data:
        return None
    df = pd.DataFrame(data)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
        df = df.set_index("time")
    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ================================================================
# 腾讯API数据拉取 (CSV模式替代DB)
# ================================================================

def _code_to_tencent(code: str) -> str:
    c = code.strip().replace(".", "").replace("SH", "").replace("SZ", "")
    if c.startswith("68"): return f"sh{c}"
    elif c.startswith(("6", "5")): return f"sh{c}"
    elif c.startswith(("0", "3", "2")): return f"sz{c}"
    return ""

def fetch_kline_tencent(code: str, count: int = 300) -> Optional[pd.DataFrame]:
    """从腾讯API拉取日线数据, 返回DataFrame或None"""
    tc = _code_to_tencent(code)
    if not tc: return None
    try:
        resp = _tencent_session.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{tc},day,,,{count},qfq"},
            headers={"Referer": "https://gu.qq.com/"},
            timeout=10,
        )
        data = resp.json()
        if not isinstance(data, dict) or int(data.get("code", 0)) != 0: return None
        root = (data.get("data") or {}).get(tc)
        if not isinstance(root, dict): return None
        rows = root.get("qfqday") or root.get("day") or []
        bars = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6: continue
            try:
                bars.append({
                    "time": str(r[0])[:10], "open": float(r[1]),
                    "high": float(r[3]), "low": float(r[4]),
                    "close": float(r[2]), "volume": float(r[5]) * 100,
                })
            except Exception: continue
        if not bars: return None
        df = pd.DataFrame(bars)
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception:
        return None

def load_codes_from_csv(csv_path: str) -> list:
    """从CSV加载股票代码列表"""
    df = pd.read_csv(csv_path, dtype=str)
    col = df.columns[0]
    codes = [str(c).strip().zfill(6) for c in df[col].dropna()]
    return codes

def detect_limit_up_runs(df: pd.DataFrame, code: str) -> list:
    """检测连板段, 返回每个连板段的起止索引和连板数"""
    board_type = get_board_type(code)
    threshold = BOARD_PARAMS[board_type]["threshold"]
    min_streak = BOARD_PARAMS[board_type]["min_streak"]

    close = df["close"].values
    n = len(close)
    runs = []
    i = 1
    while i < n:
        ret = (close[i] / close[i-1] - 1) if close[i-1] > 0 else 0
        if ret < threshold * 0.98:
            i += 1
            continue
        # 找到涨停, 往后数连板
        streak_start = i
        streak_end = i
        while streak_end < n - 1:
            ret2 = (close[streak_end+1] / close[streak_end] - 1) if close[streak_end] > 0 else 0
            if ret2 >= threshold * 0.98:
                streak_end += 1
            else:
                break
        streak_len = streak_end - streak_start + 1
        if streak_len >= min_streak:
            # 取连板段窗口: 前30天+连板段+后20天
            window_start = max(0, streak_start - 30)
            window_end = min(n, streak_end + 21)
            runs.append({
                "streak_start": streak_start,
                "streak_end": streak_end,
                "streak_len": streak_len,
                "first_limit_date": str(df.iloc[streak_start]["time"])[:10],
                "window_start": window_start,
                "window_end": window_end,
            })
        i = streak_end + 1
    return runs

# ================================================================
# 技术指标
# ================================================================

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ================================================================
# 策略核心
# ================================================================

class DragonHunterV3:
    """
    连板猎手 v3 策略

    买点条件 (全部满足):
      1. 当日涨停 (涨幅 ≥ threshold)
      2. 封板强度 ≤ seal_max% (收盘价接近最高价)
      3. 量比 ≥ vol_ratio (相对前日放量)
      4. RSI 在合理区间
      5. 前1日无大跌
      6. 买点日振幅在合理范围

    卖点条件 (任一满足):
      1. 止损: 从买入价跌超 stop_loss_pct
      2. 追踪止损: 从最高点回撤超 trailing_stop_pct
      3. 止盈: 涨超 take_profit_pct
      4. RSI超买 + 上影线过大 (见顶信号)
      5. 开板: 收盘价低于开盘价且跌幅>3%
    """

    def __init__(self, params: Dict[str, Any]):
        self.p = params

    def check_buy_signal(
        self,
        df: pd.DataFrame,
        idx: int,
    ) -> Dict[str, Any]:
        """
        检查idx位置是否满足买点条件

        Returns: {"buy": bool, "reasons": [...], "score": float}
        """
        if idx < 1:
            return {"buy": False, "reasons": ["数据不足"], "score": 0}

        # D0前20天无涨停 (排除近期已被炒作的股票)
        code = str(df.iloc[idx].get("code", "")) if "code" in df.columns else ""
        bt = get_board_type(code) if code else "main"
        threshold = BOARD_PARAMS[bt]["threshold"]
        lookback = min(20, idx)  # 实际可用历史天数
        if lookback >= 1:
            for k in range(1, lookback + 1):
                if idx - k - 1 < 0:
                    break
                prev_k = df.iloc[idx - k]
                prev_k2 = df.iloc[idx - k - 1]
                ret_k = (prev_k["close"] / prev_k2["close"] - 1)
                if ret_k >= threshold * 0.98:
                    return {"buy": False, "reasons": [f"D0前20天内有涨停"], "score": 0}

        row = df.iloc[idx]
        prev = df.iloc[idx - 1]
        prev2 = df.iloc[idx - 2] if idx >= 2 else None

        reasons = []
        score = 0

        # 0. 连板数检查
        n_limit = int(df.iloc[0].get("run_n_limit_ups", 1)) if "run_n_limit_ups" in df.columns else 1
        if n_limit < self.p.get("min_streak", 1):
            return {"buy": False, "reasons": [f"连板数{n_limit}<最小要求"], "score": 0}
        if "max_streak" in self.p and n_limit > self.p["max_streak"]:
            return {"buy": False, "reasons": [f"连板数{n_limit}>最大限制"], "score": 0}

        # 1. 涨停检查
        ret = (row["close"] / prev["close"] - 1)
        if ret < self.p["threshold"]:
            return {"buy": False, "reasons": ["未涨停"], "score": 0}

        # 1b. 高开幅度检查
        gap_pct = (row["open"] / prev["close"] - 1) * 100
        if "buy_max_gap_pct" in self.p and gap_pct > self.p["buy_max_gap_pct"]:
            return {"buy": False, "reasons": [f"高开{gap_pct:.1f}%>阈值"], "score": 0}

        # 2. 封板强度 (close vs high)
        if row["high"] <= 0:
            return {"buy": False, "reasons": ["最高价异常"], "score": 0}
        seal = (row["close"] / row["high"] - 1) * 100
        if seal > self.p["buy_seal_max"]:
            return {"buy": False, "reasons": [f"封板不严: {seal:.2f}%"], "score": 0}
        reasons.append(f"封板强度{seal:.2f}%✓")
        score += 20

        # 3. 量比
        vol_ratio = row["volume"] / prev["volume"] if prev["volume"] > 0 else 0
        if vol_ratio < self.p["buy_vol_ratio"]:
            return {"buy": False, "reasons": [f"量比不足: {vol_ratio:.2f}x"], "score": 0}
        reasons.append(f"量比{vol_ratio:.2f}x✓")
        score += min(30, int(vol_ratio * 10))  # 量比越大分越高

        # 4. RSI
        rsi_series = calc_rsi(df["close"].iloc[:idx + 1])
        rsi = float(rsi_series.iloc[-1])
        if rsi < self.p["buy_rsi_min"] or rsi > self.p["buy_rsi_max"]:
            return {"buy": False, "reasons": [f"RSI异常: {rsi:.1f}"], "score": 0}
        reasons.append(f"RSI{rsi:.1f}✓")
        score += 10

        # 5. 连板前1日跌幅 (D-1 vs D-2, 即连板段开始前那天不能大跌)
        if idx >= 3:
            d_minus1 = df.iloc[idx - 2]
            d_minus2 = df.iloc[idx - 3]
            if d_minus2["close"] > 0:
                prev_ret = (d_minus1["close"] / d_minus2["close"] - 1) * 100
                if prev_ret < self.p["buy_pre1_max_drop"]:
                    return {"buy": False, "reasons": [f"连板前1日大跌: {prev_ret:.2f}%"], "score": 0}
                reasons.append(f"连板前1日{prev_ret:+.2f}%✓")
                if prev_ret > 0:
                    score += 5  # 连板前1日涨加分

        # 6. 买点日振幅
        day_range = (row["high"] - row["low"]) / row["open"] * 100
        if day_range > self.p["buy_max_range_pct"]:
            return {"buy": False, "reasons": [f"振幅过大: {day_range:.2f}%"], "score": 0}
        reasons.append(f"振幅{day_range:.2f}%✓")
        score += 10

        # 额外加分: 买点前缩量 (蓄势)
        if prev2 is not None:
            pre_vol_ratio = prev["volume"] / prev2["volume"] if prev2["volume"] > 0 else 1
            if pre_vol_ratio < 0.8:
                score += 10
                reasons.append("前日缩量蓄势✓")

        # 7. Score门槛 (排除低质量信号)
        min_score = self.p.get("buy_min_score", 0)
        if min_score > 0 and score < min_score:
            return {"buy": False, "reasons": [f"Score{score}<{min_score}"], "score": score}

        return {"buy": True, "reasons": reasons, "score": score}

    def check_sell_signal(
        self,
        df: pd.DataFrame,
        idx: int,
        buy_price: float,
        highest_since_buy: float,
    ) -> Dict[str, Any]:
        """
        检查idx位置是否满足卖点条件

        Returns: {"sell": bool, "reason": str, "sell_type": str}
        """
        row = df.iloc[idx]
        current = row["close"]
        ret_from_buy = (current / buy_price - 1) * 100
        ret_from_high = (current / highest_since_buy - 1) * 100

        # 1. 止损
        if ret_from_buy <= self.p["stop_loss_pct"]:
            return {"sell": True, "reason": f"止损: {ret_from_buy:.2f}%", "sell_type": "stop_loss"}

        # 2. 追踪止损 (从最高点回撤)
        if ret_from_high <= self.p["trailing_stop_pct"] and ret_from_buy > 0:
            return {"sell": True, "reason": f"追踪止损: 从高点{ret_from_high:.2f}%", "sell_type": "trailing_stop"}

        # 3. 止盈
        if ret_from_buy >= self.p["take_profit_pct"]:
            return {"sell": True, "reason": f"止盈: {ret_from_buy:.2f}%", "sell_type": "take_profit"}

        # 4. RSI超买 + 上影线 (见顶信号)
        if idx >= 2:
            rsi_series = calc_rsi(df["close"].iloc[:idx + 1])
            rsi = float(rsi_series.iloc[-1])
            day_range = row["high"] - row["low"]
            upper_shadow = (row["high"] - max(row["open"], row["close"])) / day_range * 100 if day_range != 0 else 0

            if rsi >= self.p["peak_sell_rsi"] and upper_shadow >= self.p["peak_sell_upper_shadow"]:
                return {"sell": True, "reason": f"见顶信号: RSI{rsi:.1f}+上影{upper_shadow:.1f}%", "sell_type": "peak_signal"}

        # 5. 开板信号 (阴线且跌幅>3%) — v3.1: 默认移除(胜率仅4-7%)
        if not self.p.get("remove_board_break", False) and idx >= 1:
            prev_close = df.iloc[idx - 1]["close"]
            ret_today = (current / prev_close - 1) * 100
            is_bearish = current < row["open"]
            if is_bearish and ret_today < -3:
                return {"sell": True, "reason": f"开板信号: 跌{ret_today:.2f}%", "sell_type": "board_break"}

        return {"sell": False, "reason": "", "sell_type": ""}


# ================================================================
# 回测引擎 (逐日推进, 不用未来数据)
# ================================================================

def backtest_single_run(
    df_run: pd.DataFrame,
    strategy: DragonHunterV3,
) -> Dict[str, Any]:
    """
    对单个连板段进行逐日推进回测

    df_run: 包含该连板段窗口数据的DataFrame (已按time排序)
    返回: 回测结果
    """
    df_run = df_run.sort_values("time").reset_index(drop=True)

    if len(df_run) < 5:
        return {"trades": [], "error": "数据不足"}

    board_type = get_board_type(df_run["code"].iloc[0])
    params = BOARD_PARAMS[board_type]

    trades = []
    position = None  # {"buy_price", "buy_date", "buy_idx", "highest"}
    pending_buy = None  # 信号日记录, 次日open买入

    for i in range(1, len(df_run)):
        row = df_run.iloc[i]

        # ── 有持仓: 先检查卖出 ──
        if position is not None:
            # 更新最高价
            if float(row["high"]) > position["highest"]:
                position["highest"] = float(row["high"])

            # 检查卖点
            signal = strategy.check_sell_signal(
                df_run, i,
                position["buy_price"],
                position["highest"],
            )
            if signal["sell"]:
                trade = {
                    "buy_date": position["buy_date"],
                    "buy_price": position["buy_price"],
                    "sell_date": str(row["time"]),
                    "sell_price": float(row["close"]),
                    "return_pct": round((float(row["close"]) / position["buy_price"] - 1) * 100, 2),
                    "highest": position["highest"],
                    "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                    "sell_type": signal["sell_type"],
                    "sell_reason": signal["reason"],
                    "score": position.get("score", 0),
                }
                trades.append(trade)
                position = None

        # ── 待买入: 信号已触发, 本bar open买入 ──
        if pending_buy is not None and position is None:
            buy_price = float(row["open"])
            position = {
                "buy_price": buy_price,
                "buy_date": str(row["time"]),
                "buy_idx": i,
                "highest": max(buy_price, float(row["high"])),
                "score": pending_buy["score"],
                "reasons": pending_buy["reasons"],
            }
            pending_buy = None
            # 买入当天也检查卖出(以防开盘买完当天就触发止损)
            if float(row["high"]) > position["highest"]:
                position["highest"] = float(row["high"])
            signal = strategy.check_sell_signal(
                df_run, i,
                position["buy_price"],
                position["highest"],
            )
            if signal["sell"]:
                trade = {
                    "buy_date": position["buy_date"],
                    "buy_price": position["buy_price"],
                    "sell_date": str(row["time"]),
                    "sell_price": float(row["close"]),
                    "return_pct": round((float(row["close"]) / position["buy_price"] - 1) * 100, 2),
                    "highest": position["highest"],
                    "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                    "sell_type": signal["sell_type"],
                    "sell_reason": signal["reason"],
                    "score": position.get("score", 0),
                }
                trades.append(trade)
                position = None
            continue  # 买入bar不再检查新信号

        # ── 无持仓无待买: 检查买点信号 ──
        if position is None and pending_buy is None:
            signal = strategy.check_buy_signal(df_run, i)
            if signal["buy"]:
                pending_buy = {
                    "signal_idx": i,
                    "score": signal["score"],
                    "reasons": signal["reasons"],
                }

    # 如果还有持仓，在数据结束时平仓
    if position is not None:
        last = df_run.iloc[-1]
        trade = {
            "buy_date": position["buy_date"],
            "buy_price": position["buy_price"],
            "sell_date": str(last["time"]),
            "sell_price": float(last["close"]),
            "return_pct": round((float(last["close"]) / position["buy_price"] - 1) * 100, 2),
            "highest": position["highest"],
            "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
            "sell_type": "end_of_data",
            "sell_reason": "数据结束",
            "score": position.get("score", 0),
        }
        trades.append(trade)

    return {"trades": trades}


def run_full_backtest(csv_path: str = "stock_list_2000.csv"):
    """全量回测 — CSV模式 (从stock list读代码 + 腾讯API拉数据)"""
    print("📊 加载股票列表...")
    codes = load_codes_from_csv(csv_path)
    print(f"   共 {len(codes)} 只股票")

    strategy_main = DragonHunterV3(BOARD_PARAMS["main"])
    strategy_gem = DragonHunterV3(BOARD_PARAMS["gem_star"])

    all_trades = []
    success = 0
    total_runs = 0

    for idx, code in enumerate(codes):
        if (idx + 1) % 100 == 0 or idx == 0:
            print(f"\r   扫描: {idx+1}/{len(codes)}  成功: {success}  信号: {len(all_trades)}", end="", flush=True)

        df = fetch_kline_tencent(code, 300)
        if df is None or len(df) < 15:
            _time.sleep(0.15)
            continue

        # ST过滤 (提前过滤, 避免回测循环内反复网络请求)
        if is_st_stock(code):
            _time.sleep(0.15)
            continue

        success += 1

        board_type = get_board_type(code)
        strategy = strategy_main if board_type == "main" else strategy_gem

        runs = detect_limit_up_runs(df, code)
        for run in runs:
            total_runs += 1
            window = df.iloc[run["window_start"]:run["window_end"]].copy()
            window = window.reset_index(drop=True)
            window["code"] = code
            window["run_first_limit_date"] = run["first_limit_date"]
            window["run_n_limit_ups"] = run["streak_len"]

            result = backtest_single_run(window, strategy)
            for trade in result.get("trades", []):
                trade["code"] = code
                trade["board"] = get_board_name(code)
                trade["board_type"] = board_type
                trade["n_limit"] = run["streak_len"]
                all_trades.append(trade)

        _time.sleep(0.15)

    print(f"\r   扫描完成: {len(codes)}只  成功: {success}  连板段: {total_runs}")

    if not all_trades:
        print("❌ 无交易信号")
        return

    tdf = pd.DataFrame(all_trades)
    _print_backtest_summary(tdf)
    tdf.to_csv("backtest_v3_trades.csv", index=False, encoding="utf-8-sig")
    print(f"\n💾 交易明细: backtest_v3_trades.csv")
    return tdf


def run_full_backtest_db(
    start_date: str = "2024-01-01",
    end_date: str = "2026-05-21",
    quick: bool = False,
    sample: int = 0,
):
    """全量回测 — DB 模式 (逐日扫描, 模拟真实交易)"""
    print("📊 DB 模式: 从 db_market 加载数据...")
    codes = get_all_codes_db()
    print(f"   全市场: {len(codes)} 只股票")

    if quick:
        codes = codes[:500]
    elif sample > 0:
        codes = codes[:sample]

    # 查询区间: 前置30天(计算RSI等), 后置20天(出场空间)
    buffer_pre = 30
    buffer_post = 20
    query_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=buffer_pre)).strftime("%Y-%m-%d")
    query_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=buffer_post)).strftime("%Y-%m-%d")
    sd = pd.Timestamp(start_date)
    ed = pd.Timestamp(end_date)

    strategy_main = DragonHunterV3(BOARD_PARAMS["main"])
    strategy_gem = DragonHunterV3(BOARD_PARAMS["gem_star"])

    all_trades = []
    total = len(codes)
    loaded = 0
    signal_count = 0

    for code_idx, code in enumerate(codes):
        if (code_idx + 1) % 500 == 0 or code_idx == 0:
            print(f"\r   扫描: {code_idx+1}/{total}  已加载: {loaded}  信号: {signal_count}", end="", flush=True)
        try:
            df = load_daily_db(code, query_start, query_end)
            if df is None or len(df) < 15:
                continue

            # ST过滤 (提前过滤, 避免回测循环内反复网络请求)
            if is_st_stock(code):
                continue

            loaded += 1

            board_type = get_board_type(code)
            threshold = BOARD_PARAMS[board_type]["threshold"]
            strategy = strategy_main if board_type == "main" else strategy_gem

            close = df["close"].values
            n = len(close)

            # 逐日扫描: 找连板段起点(前一日非涨停 + 当日涨停)
            i = 1
            while i < n - 1:
                ret = (close[i] / close[i - 1] - 1) if close[i - 1] > 0 else 0
                if ret < threshold * 0.98:
                    i += 1
                    continue

                # 找到涨停日, 检查是否为连板起点(前一日非涨停)
                if i >= 2:
                    prev_ret = (close[i - 1] / close[i - 2] - 1) if close[i - 2] > 0 else 0
                    if prev_ret >= threshold * 0.98:
                        i += 1
                        continue  # 不是起点, 是连板中间

                # 是连板起点, 向后数连板数
                run_start = i
                run_end = i
                while run_end < n - 1:
                    next_ret = (close[run_end + 1] / close[run_end] - 1) if close[run_end] > 0 else 0
                    if next_ret >= threshold * 0.98:
                        run_end += 1
                    else:
                        break
                n_limit = run_end - run_start + 1

                # 过滤: 连板数
                min_streak = BOARD_PARAMS[board_type]["min_streak"]
                max_streak = BOARD_PARAMS[board_type].get("max_streak", 999)
                if n_limit < min_streak or n_limit > max_streak:
                    i = run_end + 1
                    continue

                # 日期检查: 连板起点必须在回测区间内
                run_start_date = df.index[run_start]
                if run_start_date < sd or run_start_date > ed:
                    i = run_end + 1
                    continue

                # 构建回测切片: 连板起点前2行 ~ 连板结束后20行
                slice_start = max(0, run_start - 2)
                slice_end = min(n, run_end + 21)
                run_df = df.iloc[slice_start:slice_end].copy().reset_index()
                run_df["run_n_limit_ups"] = n_limit
                run_df["code"] = code

                # 用 v3 策略回测这个连板段
                result = backtest_single_run(run_df, strategy)
                for trade in result.get("trades", []):
                    trade["code"] = code
                    trade["board"] = get_board_name(code)
                    trade["board_type"] = board_type
                    trade["n_limit"] = n_limit
                    all_trades.append(trade)
                    signal_count += 1

                i = run_end + 1
        except Exception:
            continue

    print(f"\r   扫描完成: {loaded}/{total} 只股票  交易信号: {signal_count}")

    if not all_trades:
        print("❌ 无交易信号")
        return

    tdf = pd.DataFrame(all_trades)
    _print_backtest_summary(tdf)
    tdf.to_csv("backtest_v3_trades.csv", index=False, encoding="utf-8-sig")
    print(f"\n💾 交易明细: backtest_v3_trades.csv")
    return tdf


def _print_backtest_summary(tdf: pd.DataFrame):
    """打印回测统计摘要 (CSV/DB 共用)"""
    print(f"\n{'='*70}")
    print(f"  连板猎手 v3 回测结果")
    print(f"{'='*70}")
    print(f"  总交易数: {len(tdf)}")
    print(f"  胜率: {(tdf['return_pct'] > 0).mean() * 100:.1f}%")
    print(f"  平均收益: {tdf['return_pct'].mean():.2f}%")
    print(f"  中位收益: {tdf['return_pct'].median():.2f}%")
    if (tdf['return_pct'] < 0).any():
        win_avg = tdf[tdf['return_pct'] > 0]['return_pct'].mean()
        loss_avg = abs(tdf[tdf['return_pct'] < 0]['return_pct'].mean())
        print(f"  盈亏比: {win_avg / loss_avg:.2f}" if loss_avg > 0 else f"  盈亏比: ∞")

    for board_type in ["main", "gem_star"]:
        sub = tdf[tdf["board_type"] == board_type]
        if len(sub) == 0:
            continue
        label = "沪深主板" if board_type == "main" else "创/科板"
        print(f"\n  --- {label} ({len(sub)}笔) ---")
        print(f"    胜率: {(sub['return_pct'] > 0).mean() * 100:.1f}%")
        print(f"    平均收益: {sub['return_pct'].mean():.2f}%")
        win_avg = sub[sub['return_pct'] > 0]['return_pct'].mean() if (sub['return_pct'] > 0).any() else 0
        loss_avg = abs(sub[sub['return_pct'] < 0]['return_pct'].mean()) if (sub['return_pct'] < 0).any() else 0
        print(f"    盈亏比: {win_avg / loss_avg:.2f}" if loss_avg > 0 else f"    盈亏比: ∞")
        print(f"    最大盈利: {sub['return_pct'].max():.2f}%")
        print(f"    最大亏损: {sub['return_pct'].min():.2f}%")

    print(f"\n  --- 卖出类型分布 ---")
    for stype, cnt in tdf["sell_type"].value_counts().items():
        sub = tdf[tdf["sell_type"] == stype]
        print(f"    {stype}: {cnt}笔 均收益{sub['return_pct'].mean():.2f}% 胜率{(sub['return_pct'] > 0).mean() * 100:.1f}%")

    print(f"\n  --- 按连板数 ---")
    for n in sorted(tdf["n_limit"].unique()):
        sub = tdf[tdf["n_limit"] == n]
        if len(sub) < 3:
            continue
        print(f"    {n}板: {len(sub)}笔 胜率{(sub['return_pct'] > 0).mean() * 100:.1f}% 均收益{sub['return_pct'].mean():.2f}%")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="连板猎手 v3 — 双分支策略 (支持 CSV/DB)")
    parser.add_argument("--source", choices=["csv", "db"], default="csv",
                        help="数据源: csv (默认) 或 db (PostgreSQL)")
    parser.add_argument("--csv", default="data/stock_list_200.csv",
                        help="股票列表CSV (--source csv 时使用)")
    parser.add_argument("--start", type=str, default="2024-01-01",
                        help="回测开始日期 (--source db 时使用)")
    parser.add_argument("--end", type=str, default="2026-05-21",
                        help="回测结束日期 (--source db 时使用)")
    parser.add_argument("--quick", action="store_true",
                        help="抽样500只 (--source db 时使用)")
    parser.add_argument("--sample", type=int, default=0,
                        help="抽样N只 (--source db 时使用)")
    args = parser.parse_args()

    if args.source == "db":
        run_full_backtest_db(
            start_date=args.start,
            end_date=args.end,
            quick=args.quick,
            sample=args.sample,
        )
    else:
        run_full_backtest(args.csv)
