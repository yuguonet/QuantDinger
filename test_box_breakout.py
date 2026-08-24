#!/usr/bin/env python3
"""
底部震荡 + 弱转强 策略 - 独立回测

═══════════════════════════════════════════════════════════════════
  弱转强形态
═══════════════════════════════════════════════════════════════════

  底部震荡期          弱转强信号           大行情启动
  ┌─────────┐      ┌─────────────┐      ┌──────────────┐
  │ 反复洗盘 │  →   │ 放量上涨     │  →   │  持续上涨     │
  │ 量能萎缩 │      │ 缩量回调     │      │              │
  │ 买卖平衡 │      │ 缩量上涨     │      │              │
  └─────────┘      └─────────────┘      └──────────────┘

  ① 底部震荡: 前20日箱体整理，量能逐步萎缩
  ② 平衡确认: 连续缩量，不再创新低
  ③ 弱转强: 放量上涨→缩量回调→缩量上涨
  ④ 买入: 弱转强确认日次日开盘买

═══════════════════════════════════════════════════════════════════
  使用方法
═══════════════════════════════════════════════════════════════════

  python test_box_breakout.py
  python test_box_breakout.py --box-days 20 --vol-expand 1.3
  python test_box_breakout.py --max-hold 30 --stop-loss 8
"""
from __future__ import annotations
import json, time, argparse, os, sys
from datetime import datetime, timedelta
from collections import defaultdict

# ================================================================
# 路径初始化
# ================================================================
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

# ================================================================
# 斜率工具函数
# ================================================================
def calc_ma_slope(closes, idx, ma_period=5, slope_days=3):
    """MA斜率: 线性回归角度 (% / 天)"""
    if idx < ma_period + slope_days - 1:
        return 0
    ma_vals = []
    for i in range(idx - slope_days + 1, idx + 1):
        ma_vals.append(sum(closes[i - ma_period + 1:i + 1]) / ma_period)
    if not ma_vals or ma_vals[0] <= 0:
        return 0
    n = len(ma_vals)
    sx = n * (n - 1) / 2
    sy = sum(ma_vals)
    sxy = sum(i * ma_vals[i] for i in range(n))
    sx2 = n * (n - 1) * (2 * n - 1) / 6
    denom = n * sx2 - sx * sx
    if denom == 0: return 0
    slope = (n * sxy - sx * sy) / denom
    return slope / ma_vals[-1] * 100

def calc_ma_slope_accel(closes, idx, ma_period=5, slope_days=3):
    """MA斜率加速度 = 当前斜率 - 前一段斜率"""
    return calc_ma_slope(closes, idx, ma_period, slope_days) - calc_ma_slope(closes, idx - slope_days, ma_period, slope_days)

_circ_cache = None
def _load_circ():
    global _circ_cache
    if _circ_cache is not None: return _circ_cache
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db(); pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT symbol, circ_shares, total_shares FROM stock_basic_info WHERE status='active' AND circ_shares > 0")
            _circ_cache = {row[0]: {'circ_shares': float(row[1]), 'total_shares': float(row[2]) if row[2] else 0} for row in cur.fetchall()}
    except: _circ_cache = {}
    return _circ_cache

def get_turnover(code, volume):
    circ = _load_circ().get(code, {})
    circ_shares = circ.get('circ_shares', 0) if isinstance(circ, dict) else 0
    return volume / circ_shares * 100 if circ_shares > 0 else 0

def get_circ_mcap(code, price):
    circ = _load_circ().get(code, {})
    circ_shares = circ.get('circ_shares', 0) if isinstance(circ, dict) else 0
    return circ_shares * price / 1e8 if circ_shares > 0 else 0

def get_total_mcap(code, price):
    circ = _load_circ().get(code, {})
    total_shares = circ.get('total_shares', 0) if isinstance(circ, dict) else 0
    return total_shares * price / 1e8 if total_shares > 0 else 0

# ================================================================
# DB 数据加载
# ================================================================
_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

def get_all_codes_db():
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []

def fetch_kline_db(code, days=300):
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        from app.data_sources.provider.adjustment import unadj_to_qfq
        bars = []
        for row in data:
            bars.append({
                'time': str(row.get('time', ''))[:10],
                'open': float(row.get('open', 0)),
                'high': float(row.get('high', 0)),
                'low': float(row.get('low', 0)),
                'close': float(row.get('close', 0)),
                'volume': float(row.get('volume', 0)),
            })
        bars = unadj_to_qfq(bars, code)
        return bars[-days:] if len(bars) > days else bars
    except Exception:
        return []

def fetch_kline(code, days=300):
    from kline_cache import fetch_kline as _fetch_kline
    return _fetch_kline(code, days)

def get_board_name(code):
    if code.startswith('688'):
        return '科创板'
    elif code.startswith('300'):
        return '创业板'
    elif code.startswith('60'):
        return '沪主板'
    elif code.startswith('00') or code.startswith('001') or code.startswith('002'):
        return '深主板'
    return '未知'

# ================================================================
# 回测引擎
# ================================================================
def run_backtest(bars, entry_idx, entry_price, stop_loss_pct=5.0,
                 trailing_pct=5.0, max_hold_days=15,
                 take_profit_pct=15.0, trailing_activate_pct=5.0):
    """
    回测出场规则:
      峰值回撤12%出场
      持仓上限出场
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None

    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    exit_reason = "data_end"
    max_d = len(bars) - entry_idx - 1

    if max_d <= 0:
        return None

    for d in range(1, max_d + 1):
        idx = entry_idx + d
        if idx >= len(bars):
            break
        b = bars[idx]
        if b['high'] > peak:
            peak = b['high']

        # 峰值回撤12%出场
        drop_pct = (peak - b['low']) / peak * 100
        if drop_pct >= 12:
            exit_p = peak * 0.88  # 峰值-12%
            if b['open'] < exit_p:
                exit_p = b['open']
            exit_d = d
            exit_reason = "peak_drop_12"
            break

        # 持仓上限
        if max_hold_days > 0 and d >= max_hold_days:
            exit_p = b['close']
            exit_d = d
            exit_reason = "max_hold"
            break

        exit_p = b['close']
        exit_d = d
        exit_reason = "data_end"

    return {
        'exit_price': round(exit_p, 3),
        'exit_day': exit_d,
        'exit_reason': exit_reason,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }

# ================================================================
# 底部震荡 + 弱转强 策略
# ================================================================
def strategy_bottom_reversal(bars, code,
                              box_days=20, box_max_range=15.0, box_min_range=0.0,
                              vol_expand=1.3, vol_shrink_ratio=0.8,
                              min_ma10_slope=0.3, min_ma60_slope=-0.3, max_ma60_slope=0.3,
                              max_ma_dispersion=5.0, max_chg_20d=12.0,
                              stop_loss_pct=12.0, trailing_pct=5.0,
                              trailing_activate_pct=5.0, take_profit_pct=15.0,
                              max_hold_days=30, top_per_day=2):
    """
    底部震荡 + 弱转强 入场策略

    入场条件:
      ① 底部震荡: 前20日箱体整理，振幅<15%
      ② 平衡确认: 前20日涨幅<12%，底部区域
      ③ 弱转强信号:
         - D-2: 放量上涨 (量>1.3x均量，收阳)
         - D-1: 缩量回调 (量<D-2的80%，收阴)
         - D0: 缩量上涨 (量<D-2的80%，收阳，价格>D-1)
      ④ MA趋势: MA10斜率>0.3%且递增，MA60走平(-0.3%~0.3%)
      ⑤ 三线粘合: (MA10-MA60)/MA60<=5%
      ⑥ 买入: 弱转强确认日次日开盘买
    """
    if len(bars) < 80:
        return []

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    opens = [b["open"] for b in bars]

    candidates = []

    for i in range(30, len(bars) - 1):
        # ── ① 弱转强信号检测 ──
        # D-2: 放量上涨
        avg_vol_before = sum(volumes[max(0,i-7):i-2]) / min(7, i-2) if i > 7 else volumes[i-2]
        d2_vol_expand = volumes[i-2] > avg_vol_before * vol_expand
        d2_bullish = closes[i-2] > opens[i-2]

        if not (d2_vol_expand and d2_bullish):
            continue

        # D-1: 缩量回调
        d1_vol_shrink = volumes[i-1] < volumes[i-2] * vol_shrink_ratio
        d1_bearish = closes[i-1] < closes[i-2]

        if not (d1_vol_shrink and d1_bearish):
            continue

        # D0: 上涨，量在0.8-1.2倍D-1量之间
        d0_vol_ratio = volumes[i] / volumes[i-1] if volumes[i-1] > 0 else 1.0
        d0_bullish = closes[i] > opens[i]
        d0_recover = closes[i] > closes[i-1]

        if not (d0_bullish and d0_recover and 0.8 <= d0_vol_ratio <= 1.2):
            continue

        # ── ② 底部确认: 前20日涨幅<阈值 且 前20日振幅<阈值 ──
        chg_20d = (closes[i] / closes[i-20] - 1) * 100 if i >= 20 else 0
        if chg_20d > max_chg_20d:
            continue

        # 前20日振幅检查
        high_20d = max(highs[max(0,i-19):i+1])
        low_20d = min(lows[max(0,i-19):i+1])
        box_range_20d = (high_20d - low_20d) / low_20d * 100 if low_20d > 0 else 0
        if box_range_20d > box_max_range:
            continue

        # ── 换手率过滤 ──
        circ_data = _load_circ().get(code, {})
        circ_shares = circ_data.get('circ_shares', 0) if isinstance(circ_data, dict) else 0
        total_shares = circ_data.get('total_shares', 0) if isinstance(circ_data, dict) else 0
        if circ_shares > 0:
            turnover = volumes[i] / circ_shares * 100
            if turnover < 2.5 or turnover > 6:  # 换手率2.5~6%最佳区间
                continue
        else:
            turnover = 0

        # ── ③ MA趋势检查 ──
        ma10_now = sum(closes[i-9:i+1]) / 10
        ma10_prev = sum(closes[i-10:i]) / 10
        ma10_slope = (ma10_now - ma10_prev) / ma10_prev * 100 if ma10_prev > 0 else 0

        # MA10前一段斜率
        ma10_prev2 = sum(closes[i-11:i-1]) / 10 if i >= 11 else ma10_prev
        ma10_slope_prev = (ma10_prev - ma10_prev2) / ma10_prev2 * 100 if ma10_prev2 > 0 else 0

        if ma10_slope < min_ma10_slope:
            continue

        # MA20斜率
        ma20_now = sum(closes[i-19:i+1]) / 20 if i >= 19 else closes[i]
        ma20_prev = sum(closes[i-20:i]) / 20 if i >= 20 else ma20_now
        ma20_slope = (ma20_now - ma20_prev) / ma20_prev * 100 if ma20_prev > 0 else 0

        ma20_prev2 = sum(closes[i-21:i-1]) / 20 if i >= 21 else ma20_prev
        ma20_slope_prev = (ma20_prev - ma20_prev2) / ma20_prev2 * 100 if ma20_prev2 > 0 else 0

        if ma20_slope <= 0 or ma20_slope <= ma20_slope_prev:
            continue

        # MA60斜率
        ma60_now = sum(closes[i-59:i+1]) / 60 if i >= 59 else closes[i]
        ma60_prev = sum(closes[i-60:i]) / 60 if i >= 60 else ma60_now
        ma60_slope = (ma60_now - ma60_prev) / ma60_prev * 100 if ma60_prev > 0 else 0

        if ma60_slope < min_ma60_slope or ma60_slope > max_ma60_slope:
            continue

        # 三线离散度 (已屏蔽)
        ma_dispersion = (ma10_now - ma60_now) / ma60_now * 100 if ma60_now > 0 else 0

        # ── ④ 记录指标 ──
        if i >= 15:
            gains = [max(closes[j] - closes[j-1], 0) for j in range(i-13, i+1)]
            loss_list = [max(closes[j-1] - closes[j], 0) for j in range(i-13, i+1)]
            avg_g = sum(gains) / 14
            avg_l = sum(loss_list) / 14
            rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100
        else:
            rsi = 50

        ma5_slope = calc_ma_slope(closes, i, 5, 3)
        ma5_accel = calc_ma_slope_accel(closes, i, 5, 3)
        circ_mcap = get_circ_mcap(code, closes[i])
        total_mcap = get_total_mcap(code, closes[i])
        # turnover already calculated above

        # D-2/D-1/D0 量价数据
        d2_volume = volumes[i-2]
        d1_volume = volumes[i-1]
        d0_volume = volumes[i]
        d2_chg = (closes[i-2] / closes[i-3] - 1) * 100 if i >= 3 else 0
        d1_chg = (closes[i-1] / closes[i-2] - 1) * 100
        d0_chg = (closes[i] / closes[i-1] - 1) * 100
        vol_ratio_d0_d2 = volumes[i] / volumes[i-2] if volumes[i-2] > 0 else 1.0

        # 前20日量价数据
        low_20d = min(lows[max(0,i-19):i+1])
        high_20d = max(highs[max(0,i-19):i+1])
        avg_vol_20 = sum(volumes[max(0,i-19):i+1]) / min(20, i+1)
        d2_vol_expand = volumes[i-2] / avg_vol_20 if avg_vol_20 > 0 else 1.0

        # 箱体振幅
        box_high = high_20d
        box_low = low_20d
        box_range = (box_high - box_low) / box_low * 100 if box_low > 0 else 0

        # ── ⑤ 买入: 次日开盘 ──
        entry_idx = i + 1
        if entry_idx >= len(bars):
            continue
        entry_price = bars[entry_idx]["open"]
        if entry_price <= 0:
            continue

        candidates.append({
            "idx": i,
            "signal_date": bars[i]["time"],
            "entry_price": entry_price,
            "entry_idx": entry_idx,
            "entry_date": bars[entry_idx]["time"],
            "d2_date": bars[i-2]["time"],
            "d1_date": bars[i-1]["time"],
            "d0_date": bars[i]["time"],
            "d2_volume": d2_volume,
            "d1_volume": d1_volume,
            "d0_volume": d0_volume,
            "d2_chg": round(d2_chg, 2),
            "d1_chg": round(d1_chg, 2),
            "d0_chg": round(d0_chg, 2),
            "vol_ratio_d0_d2": round(vol_ratio_d0_d2, 2),
            "chg_20d": round(chg_20d, 2),
            "low_20d": round(low_20d, 3),
            "high_20d": round(high_20d, 3),
            "avg_vol_20": avg_vol_20,
            "d2_vol_expand": round(d2_vol_expand, 2),
            "box_range": round(box_range, 2),
            "ma5_slope": round(ma5_slope, 3),
            "ma10_slope": round(ma10_slope, 3),
            "ma5_accel": round(ma5_accel, 3),
            "turnover": round(turnover, 2),
            "circ_mcap": round(circ_mcap, 1),
            "total_mcap": round(total_mcap, 1),
            "circ_shares": circ_shares,
            "total_shares": total_shares,
            "ma60": round(ma60_now, 3),
            "ma60_slope": round(ma60_slope, 3),
            "ma_dispersion": round(ma_dispersion, 2),
            "rsi": round(rsi, 1),
            "close_vs_ma60": round((closes[i] / ma60_now - 1) * 100, 2) if ma60_now > 0 else 0,
        })

    daily_candidates = defaultdict(list)
    for c in candidates:
        daily_candidates[c["signal_date"]].append(c)

    filtered = []
    for date, cands in daily_candidates.items():
        cands.sort(key=lambda c: (-c["ma10_slope"], c["chg_20d"]))
        filtered.extend(cands[:top_per_day])

    trades = []
    for c in filtered:
        result = run_backtest(bars, c["entry_idx"], c["entry_price"],
                              stop_loss_pct, trailing_pct, max_hold_days,
                              take_profit_pct, trailing_activate_pct)
        if not result:
            continue

        trades.append({
            "code": code, "board": get_board_name(code),
            "path": "bottom_reversal",
            "path_label": "底部弱转强",
            "signal_date": c["signal_date"], "signal_close": closes[c["idx"]],
            # 弱转强三日数据
            "d2_date": c["d2_date"], "d1_date": c["d1_date"], "d0_date": c["d0_date"],
            "d2_volume": c["d2_volume"], "d1_volume": c["d1_volume"], "d0_volume": c["d0_volume"],
            "d2_chg": c["d2_chg"], "d1_chg": c["d1_chg"], "d0_chg": c["d0_chg"],
            "vol_ratio_d0_d2": c["vol_ratio_d0_d2"],
            # 前20日数据
            "chg_20d": c["chg_20d"],
            "low_20d": c["low_20d"], "high_20d": c["high_20d"],
            "avg_vol_20": c["avg_vol_20"], "d2_vol_expand": c["d2_vol_expand"],
            "box_high": c["high_20d"], "box_low": c["low_20d"], "box_range_pct": c["box_range"],
            # MA指标
            "ma5_slope": c.get("ma5_slope", 0),
            "ma10_slope": c.get("ma10_slope", 0),
            "ma5_accel": c.get("ma5_accel", 0),
            "ma60": c["ma60"], "ma60_slope": c.get("ma60_slope", 0), "ma_dispersion": c.get("ma_dispersion", 0),
            "rsi": c.get("rsi", 0),
            "close_vs_ma60": c["close_vs_ma60"],
            # 市值和换手率
            "turnover": c.get("turnover", 0),
            "circ_mcap": c.get("circ_mcap", 0),
            "total_mcap": c.get("total_mcap", 0),
            "circ_shares": c.get("circ_shares", 0),
            "total_shares": c.get("total_shares", 0),
            # 入场和出场
            "entry_date": c["entry_date"],
            "entry_price": round(c["entry_price"], 3),
            "buy_mode": "weak_to_strong_next_open",
            **result,
        })

    return trades


# ================================================================
# 测试股票列表
# ================================================================
TEST_CODES = [
    # ── 沪主板 (60) ── 科技/制造/消费/医药
    "600031","600048","600056","600066","600085","600089","600100",
    "600104","600109","600111","600115","600132","600143","600150",
    "600160","600161","600170","600176","600183","600184","600196",
    "600201","600206","600216","600219","600233","600256","600260",
    "600271","600276","600298","600309","600316","600329","600332",
    "600346","600352","600362","600366","600372","600388","600392",
    "600406","600418","600426","600436","600438","600460","600487",
    "600489","600498","600507","600519","600521","600529","600557",
    "600566","600570","600580","600584","600585","600588","600600",
    "600660","600663","600690","600703","600737","600741","600745",
    "600760","600765","600782","600809","600845","600862","600867",
    "600885","600886","600893","600900","600918","601012","601066",
    "601100","601111","601138","601155","601162","601168","601200",
    "601211","601225","601231","601236","601238","601318","601336",
    "601360","601390","601555","601577","601615","601618","601628",
    "601633","601668","601669","601688","601698","601700","601766",
    "601788","601799","601800","601808","601816","601818","601838",
    "601858","601865","601868","601877","601881","601888","601899",
    "601901","601916","601919","601933","601939","601958","601966",
    "601985","601988","601989","601992","601998","603019","603056",
    "603077","603087","603160","603185","603198","603228","603233",
    "603259","603260","603288","603290","603345","603369","603392",
    "603444","603486","603501","603515","603517","603568","603583",
    "603596","603605","603613","603658","603659","603688","603707",
    "603712","603719","603737","603799","603806","603816","603833",
    "603858","603882","603883","603885","603893","603899","603960",
    "603986","603993",
    # ── 深主板 (00) ──
    "000009","000012","000021","000027","000031","000039","000049",
    "000060","000063","000066","000069","000078","000088","000100",
    "000157","000333","000338","000400","000401","000408","000425",
    "000513","000519","000528","000536","000537","000539","000547",
    "000553","000559","000568","000581","000591","000596","000598",
    "000601","000612","000623","000625","000630","000636","000651",
    "000656","000661","000671","000683","000703","000709","000723",
    "000725","000727","000733","000738","000768","000776","000778",
    "000783","000786","000800","000807","000810","000811","000822",
    "000825","000830","000831","000848","000858","000860","000876",
    "000877","000878","000883","000893","000895","000898","000902",
    "000905","000917","000930","000932","000938","000960","000963",
    "000969","000970","000975","000977","000983","000987","000988",
    "000998","001914","001979","002001","002002","002007","002008",
    "002010","002013","002019","002024","002025","002027","002028",
    "002030","002032","002035","002038","002044","002049","002050",
    "002055","002056","002060","002064","002065","002074","002078",
    "002080","002081","002092","002100","002110","002120","002127",
    "002129","002131","002138","002142","002146","002152","002155",
    "002156","002157","002163","002166","002170","002171","002174",
    "002176","002179","002180","002185","002190","002191","002195",
    "002196","002202","002203","002212","002214","002218","002221",
    "002223","002227","002230","002233","002234","002236","002238",
    "002241","002244","002249","002250","002252","002254","002255",
    "002258","002261","002263","002266","002268","002270","002271",
    "002273","002274","002276","002281","002285","002292","002294",
    "002299","002304","002311","002312","002340","002352","002353",
    "002371","002372","002375","002382","002385","002390","002399",
    "002405","002407","002408","002409","002414","002415","002416",
    "002419","002421","002430","002432","002436","002438","002439",
    "002444","002456","002460","002463","002466","002468","002470",
    "002475","002493","002497","002500","002505","002507","002511",
    "002531","002555","002557","002568","002572","002594","002595",
    "002600","002601","002602","002607","002624","002625","002643",
    "002648","002653","002670","002673","002683","002709","002714",
    "002736","002739","002745","002756","002761","002791","002797",
    "002812","002821","002831","002841","002850","002867","002916",
    "002920","002926","002938","002945","002966","002984","003816",
    # ── 创业板/科创板已移除，仅保留主板 ──
]

# ================================================================
# 统计输出
# ================================================================
def print_stats(trades, label):
    if not trades:
        print(f"  {label}: 无信号")
        return
    rets = [t['return_pct'] for t in trades if t['return_pct'] is not None]
    if not rets:
        print(f"  {label}: 无收益数据")
        return
    n = len(rets)
    win = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]
    wr = len(win) / n * 100
    avg = sum(rets) / n
    avg_pk = sum(t.get('peak_return_pct', 0) for t in trades) / n
    avg_w = sum(win) / len(win) if win else 0
    avg_l = abs(sum(loss)) / len(loss) if loss else 0
    pl = avg_w / avg_l if avg_l > 0 else 999
    print(f"  {label}: {n}笔 胜率{wr:.1f}% 均收{avg:+.2f}% 均峰{avg_pk:+.2f}% 盈亏比{pl:.2f}")

# ================================================================
# 主流程
# ================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="底部震荡 + 弱转强 策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")

    # 弱转强参数
    parser.add_argument("--vol-expand", type=float, default=1.3,
                        help="D-2放量倍数 (默认1.3)")
    parser.add_argument("--vol-shrink-ratio", type=float, default=0.8,
                        help="D0/D1缩量比例 (默认0.8)")

    # 箱体参数
    parser.add_argument("--box-days", type=int, default=20,
                        help="箱体整理天数 (默认20)")
    parser.add_argument("--box-max-range", type=float, default=15.0,
                        help="箱体最大振幅%% (默认15.0)")
    parser.add_argument("--box-min-range", type=float, default=0.0,
                        help="箱体最小振幅%% (默认0)")

    # MA参数
    parser.add_argument("--min-ma10-slope", type=float, default=0.3,
                        help="MA10斜率下限%%/天 (默认0.3)")
    parser.add_argument("--min-ma60-slope", type=float, default=-0.3,
                        help="MA60斜率下限%% (默认-0.3)")
    parser.add_argument("--max-ma60-slope", type=float, default=0.3,
                        help="MA60斜率上限%% (默认0.3)")
    parser.add_argument("--max-ma-dispersion", type=float, default=5.0,
                        help="三线离散度上限%% (默认5.0)")
    parser.add_argument("--max-chg20d", type=float, default=12.0,
                        help="20日涨幅上限%% (默认12.0)")

    # 出场参数
    parser.add_argument("--stop-loss", type=float, default=12.0,
                        help="跟踪止损回撤%% (默认12.0)")
    parser.add_argument("--trailing-pct", type=float, default=5.0,
                        help="跟踪止损回撤%% (默认5.0)")
    parser.add_argument("--trailing-activate", type=float, default=5.0,
                        help="跟踪止损激活门槛%% (默认5.0)")
    parser.add_argument("--take-profit", type=float, default=15.0,
                        help="止盈%% (默认15.0)")
    parser.add_argument("--max-hold", type=int, default=30,
                        help="最大持仓天数 (默认30)")
    parser.add_argument("--board-adaptive", action="store_true", default=True,
                        help="板块自适应参数 (默认开启)")
    parser.add_argument("--no-board-adaptive", action="store_false", dest="board_adaptive",
                        help="禁用板块自适应")

    # 其他
    parser.add_argument("--top-per-day", type=int, default=2,
                        help="每天最多选前N个 (默认2)")
    parser.add_argument("--top", type=int, default=10, help="显示TOP N (默认10)")
    parser.add_argument("--all-trades", action="store_true", help="打印全部交易明细")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    print(f"{'=' * 80}")
    print(f"底部震荡 + 弱转强 策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① 弱转强信号: D-2放量上涨(>{args.vol_expand}x) → D-1缩量回调(<0.8x) → D0上涨(0.8-1.2x D0/D-1)")
    print(f"  ② 底部确认: 前20日涨幅<{args.max_chg20d}%, 前20日振幅<{args.box_max_range}%")
    print(f"  ③ MA10加速: 斜率>={args.min_ma10_slope}%")
    print(f"  ④ MA20加速: 斜率>0且递增")
    print(f"  ⑤ MA60走平: {args.min_ma60_slope}%~{args.max_ma60_slope}%")
    print(f"  ⑥ 换手率: 2.5%~6%")
    print(f"出场条件:")
    print(f"  ① 峰值回撤12%出场")
    print(f"  ② 持仓上限: {args.max_hold}天")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        # 屏蔽创业板/科创板
        if code.startswith('300') or code.startswith('301') or code.startswith('688'):
            continue

        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        # 板块自适应参数
        board = get_board_name(code)
        if args.board_adaptive and board in ('沪主板', '深主板'):
            _stop_loss = 8.0
        else:
            _stop_loss = args.stop_loss

        trades = strategy_bottom_reversal(
            bars, code,
            box_days=args.box_days,
            box_max_range=args.box_max_range,
            box_min_range=args.box_min_range,
            vol_expand=args.vol_expand,
            vol_shrink_ratio=args.vol_shrink_ratio,
            min_ma10_slope=args.min_ma10_slope,
            min_ma60_slope=args.min_ma60_slope,
            max_ma60_slope=args.max_ma60_slope,
            max_ma_dispersion=args.max_ma_dispersion,
            max_chg_20d=args.max_chg20d,
            stop_loss_pct=_stop_loss,
            trailing_pct=args.trailing_pct,
            trailing_activate_pct=args.trailing_activate,
            take_profit_pct=args.take_profit,
            max_hold_days=args.max_hold,
            top_per_day=args.top_per_day,
        )

        all_trades.extend(trades)

        if trades:
            print(f"[{i+1}/{len(codes)}] {code} ({get_board_name(code)}) "
                  f"{len(trades)}个信号")
            success += 1

    # ---- 汇总统计 ----
    print(f"\n{'=' * 80}")
    print(f"回测完成: {success}/{len(codes)} 只股票有信号, 共 {len(all_trades)} 笔交易")
    print(f"{'=' * 80}")

    if all_trades:
        print_stats(all_trades, "全部")

        # 按板块统计
        print(f"\n--- 板块统计 ---")
        for board in ['沪主板', '深主板', '创业板', '科创板']:
            seg = [t for t in all_trades if t['board'] == board]
            if seg:
                print_stats(seg, board)

        # 按出场原因统计
        print(f"\n--- 出场原因统计 ---")
        from collections import Counter
        for reason, cnt in Counter(t['exit_reason'] for t in all_trades).most_common():
            seg = [t for t in all_trades if t['exit_reason'] == reason]
            print_stats(seg, reason)

        # 按20日涨幅分段
        print(f"\n--- 20日涨幅分段 ---")
        for lo, hi in [(-99, 0), (0, 5), (5, 10), (10, 15)]:
            seg = [t for t in all_trades if lo <= t['chg_20d'] < hi]
            if seg:
                print_stats(seg, f"20日涨幅[{lo},{hi})%")

        # 按放量倍数分段
        print(f"\n--- D0/D-2量比 ---")
        for lo, hi in [(0, 0.5), (0.5, 0.8), (0.8, 1.0), (1.0, 1.5), (1.5, 2.0)]:
            seg = [t for t in all_trades if lo <= t['vol_ratio_d0_d2'] < hi]
            if seg:
                print_stats(seg, f"量比[{lo},{hi})")

        # MA10斜率
        print(f"\n--- MA10斜率 ---")
        for lo,hi,label in [(-99,0,'负'),(0,0.2,'缓'),(0.2,0.5,'中'),(0.5,99,'快')]:
            ts=[t for t in all_trades if lo<=t.get('ma10_slope',0)<hi]
            if ts: print_stats(ts, label)

        # MA60斜率
        print(f"\n--- MA60斜率 ---")
        for lo,hi,label in [(-99,-0.3,'弱下'),(-0.3,0,'走平'),(0,0.3,'弱上'),(0.3,99,'强上')]:
            ts=[t for t in all_trades if lo<=t.get('ma60_slope',0)<hi]
            if ts: print_stats(ts, label)

        # 三线离散度
        print(f"\n--- 三线离散度 ---")
        for lo,hi,label in [(-99,0,'MA10<MA60'),(0,2,'粘合'),(2,5,'轻微'),(5,10,'中等')]:
            ts=[t for t in all_trades if lo<=t.get('ma_dispersion',0)<hi]
            if ts: print_stats(ts, label)

        # 换手率
        print(f"\n--- 换手率 ---")
        for lo,hi in [(2.5,3),(3,4),(4,5),(5,6)]:
            ts=[t for t in all_trades if lo<=t.get('turnover',0)<hi]
            if ts: print_stats(ts, f"[{lo},{hi})%")

        # 流通市值
        print(f"\n--- 流通市值 ---")
        for lo,hi,label in [(0,30,'<30亿'),(30,100,'30~100亿'),(100,500,'100~500亿'),(500,9999,">500亿")]:
            ts=[t for t in all_trades if lo<=t.get('circ_mcap',0)<hi]
            if ts: print_stats(ts, label)

        # 总市值
        print(f"\n--- 总市值 ---")
        for lo,hi,label in [(0,50,'<50亿'),(50,200,'50~200亿'),(200,1000,'200~1000亿'),(1000,9999,">1000亿")]:
            ts=[t for t in all_trades if lo<=t.get('total_mcap',0)<hi]
            if ts: print_stats(ts, label)

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n--- TOP {n} 最佳交易 ---")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 均峰{t['peak_return_pct']:+.2f}% "
                  f"持仓{t['exit_day']}天 {t['exit_reason']}")

        print(f"\n--- TOP {n} 最差交易 ---")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 均峰{t['peak_return_pct']:+.2f}% "
                  f"持仓{t['exit_day']}天 {t['exit_reason']}")

    # 保存JSON
    if all_trades:
        out_file = "test_box_breakout_result.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")
