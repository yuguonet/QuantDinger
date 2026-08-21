#!/usr/bin/env python3
"""
箱体突破 + 站上MA60 策略 - 独立回测

═══════════════════════════════════════════════════════════════════
  箱体突破形态
═══════════════════════════════════════════════════════════════════

          箱体上沿 ─ ─ ─ ─ ─ ─ ─ 突破↑
         /─────────\\         /──────\\  → 上涨
        /           \\       /
       /             \\     /
      /───────────────\\───/
          箱体下沿

  ① 箱体: 最近N天内价格在 [下沿, 上沿] 区间震荡
  ② 窄幅: 振幅 <= box_max_range%
  ③ 突破: 收盘价 > 箱体上沿，收阳线
  ④ MA60: 突破日收盘站上60日均线
  ⑤ 放量: 突破日量 >= 区间均量 x vol_expand_min
  ⑥ (可选) MACD柱过滤 / RSI过滤 / MA20向上
  ⑦ 买入: 确认后次日开盘买

═══════════════════════════════════════════════════════════════════
  使用方法
═══════════════════════════════════════════════════════════════════

  python test_box_breakout.py --require-ma60
  python test_box_breakout.py --require-ma60 --min-macd-hist 0.5
  python test_box_breakout.py --require-ma60 --min-rsi 50 --max-rsi 80
  python test_box_breakout.py --require-ma60 --pullback-confirm
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
# 斜率 + 换手率 工具函数
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
            cur.execute("SELECT symbol, circ_shares FROM stock_basic_info WHERE status='active' AND circ_shares > 0")
            _circ_cache = {row[0]: float(row[1]) for row in cur.fetchall()}
    except: _circ_cache = {}
    return _circ_cache

def get_turnover(code, volume):
    circ = _load_circ().get(code, 0)
    return volume / circ * 100 if circ > 0 else 0

def get_circ_mcap(code, price):
    circ = _load_circ().get(code, 0)
    return circ * price / 1e8 if circ > 0 else 0

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
    简化回测: 买入后跟踪峰值，从峰值回撤 peak_drop_pct% 出场，最多持有 max_hold_days 天。
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

        # 从峰值回撤 peak_drop_pct% 出场
        drop_from_peak = (peak - b['low']) / peak * 100
        if drop_from_peak >= stop_loss_pct:
            drop_price = peak * (1 - stop_loss_pct / 100)
            if b['open'] < drop_price:
                exit_p = b['open']
            else:
                exit_p = drop_price
            exit_d = d
            exit_reason = "peak_drop"
            break

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
# 箱体突破 + 站上MA60 策略
# ================================================================
def strategy_peak_breakout(bars, code,
                           box_days=25, box_max_range=15.0, box_min_range=0.0, box_min_bars=10,
                           vol_expand_min=1.5, vol_expand_max=3.0,
                           stop_loss_pct=12.0, trailing_pct=5.0,
                           trailing_activate_pct=5.0, take_profit_pct=15.0,
                           max_hold_days=10, top_per_day=2,
                           require_ma60=True, require_ma20_up=False,
                           min_rsi=0, max_rsi=100,
                           min_macd_hist=0.0, max_macd_hist=100.0,
                           pullback_confirm=False, pullback_days=3,
                           pullback_max_pct=3.0):
    """
    箱体突破 + 站上MA60 入场策略（优化版）

    入场条件:
      ① 箱体: 最近 box_days 天内，价格在 [下沿, 上沿] 区间震荡
      ② 窄幅: 振幅 <= box_max_range%
      ③ 不破底: 低点 >= 箱体下沿
      ④ 突破: 收盘价 > 箱体上沿，收阳线
      ⑤ MA60: 突破日收盘站上60日均线
      ⑥ 放量: 突破日量 >= 区间均量 x vol_expand_min
      ⑦ (可选) MA20向上 / RSI过滤 / MACD柱过滤
      ⑧ (可选) 回踩确认: 突破后N天内回踩不破箱体上沿
      ⑨ 买入: 确认后次日开盘买
    """
    if len(bars) < box_days + 65:
        return []

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]

    # ── 预计算 MACD(12,26,9) ──
    ema12 = [0.0] * len(closes)
    ema26 = [0.0] * len(closes)
    dif = [0.0] * len(closes)
    dea = [0.0] * len(closes)
    macd_hist = [0.0] * len(closes)
    if len(closes) >= 26:
        ema12[0] = closes[0]
        ema26[0] = closes[0]
        for t in range(1, len(closes)):
            ema12[t] = ema12[t-1] + 2/13 * (closes[t] - ema12[t-1])
            ema26[t] = ema26[t-1] + 2/27 * (closes[t] - ema26[t-1])
            dif[t] = ema12[t] - ema26[t]
            dea[t] = dea[t-1] + 2/10 * (dif[t] - dea[t-1]) if t > 0 else dif[t]
            macd_hist[t] = 2 * (dif[t] - dea[t])

    candidates = []

    for i in range(box_days + 60, len(bars)):
        ma60 = sum(closes[i-59:i+1]) / 60
        ma20 = sum(closes[i-19:i+1]) / 20
        ma20_prev = sum(closes[i-20:i]) / 20 if i >= 20 else ma20

        # RSI(14)
        if i >= 15:
            gains = [max(closes[j] - closes[j-1], 0) for j in range(i-13, i+1)]
            loss_list = [max(closes[j-1] - closes[j], 0) for j in range(i-13, i+1)]
            avg_g = sum(gains) / 14
            avg_l = sum(loss_list) / 14
            rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100
        else:
            rsi = 50

        box_start = i - box_days
        box_high = max(highs[box_start:i])
        box_low = min(lows[box_start:i])
        if box_low <= 0:
            continue

        box_range_pct = (box_high - box_low) / box_low * 100
        if box_range_pct > box_max_range:
            continue
        if box_range_pct < box_min_range:
            continue
        if lows[i] < box_low:
            continue
        if closes[i] <= box_high:
            continue
        if i >= 1 and closes[i] <= closes[i-1]:
            continue
        if require_ma60 and closes[i] < ma60:
            continue
        if require_ma20_up and ma20 <= ma20_prev:
            continue
        if rsi < min_rsi or rsi > max_rsi:
            continue

        # MACD柱过滤
        if min_macd_hist > 0 and macd_hist[i] < min_macd_hist:
            continue
        if macd_hist[i] > max_macd_hist:
            continue

        # MA5/MA10 斜率
        ma5_slope = calc_ma_slope(closes, i, 5, 3)
        ma10_slope = calc_ma_slope(closes, i, 10, 3)
        ma5_accel = calc_ma_slope_accel(closes, i, 5, 3)

        # 流通市值过滤 (<50亿 或 >2000亿 排除)
        circ_mcap = get_circ_mcap(code, closes[i])
        if circ_mcap > 0 and (circ_mcap < 50 or circ_mcap > 2000):
            continue

        box_avg_vol = sum(volumes[box_start:i]) / box_days
        vol_ratio = volumes[i] / box_avg_vol if box_avg_vol > 0 else 0
        if vol_ratio < vol_expand_min or vol_ratio > vol_expand_max:
            continue

        # 换手率 (有数据才检查)
        turnover = get_turnover(code, volumes[i])
        if circ_mcap > 0 and turnover > 0:
            import math
            scale = math.sqrt(100 / circ_mcap)
            tr_min = 1.0 * scale
            tr_max = 20.0 * scale
            if turnover < tr_min or turnover > tr_max:
                continue

        breakout_idx = i
        breakout_close = closes[i]

        # 回踩确认模式
        if pullback_confirm:
            confirmed = False
            for j in range(i + 1, min(i + pullback_days + 1, len(bars))):
                if lows[j] < box_high:
                    confirmed = False
                    break
                if lows[j] >= box_high and closes[j] > closes[j-1]:
                    confirmed = True
                    i = j
                    break
            if not confirmed:
                continue

        entry_idx = i + 1
        if entry_idx >= len(bars):
            continue
        entry_price = bars[entry_idx]["open"]
        if entry_price <= 0:
            continue

        candidates.append({
            "idx": i, "signal_date": bars[i]["time"],
            "entry_price": entry_price, "entry_idx": entry_idx,
            "entry_date": bars[entry_idx]["time"],
            "box_high": round(box_high, 3), "box_low": round(box_low, 3),
            "box_range_pct": round(box_range_pct, 2), "box_days": box_days,
            "breakout_close": round(breakout_close, 3),
            "vol_ratio": round(vol_ratio, 2),
            "ma5_slope": round(ma5_slope, 3),
            "ma10_slope": round(ma10_slope, 3),
            "ma5_accel": round(ma5_accel, 3),
            "turnover": round(turnover, 2),
            "circ_mcap": round(circ_mcap, 1),
            "ma60": round(ma60, 3),
            "rsi": round(rsi, 1),
            "macd_hist": round(macd_hist[i], 4),
            "close_vs_ma60": round((breakout_close / ma60 - 1) * 100, 2),
            "pullback_confirmed": pullback_confirm,
        })

    daily_candidates = defaultdict(list)
    for c in candidates:
        daily_candidates[c["signal_date"]].append(c)

    filtered = []
    for date, cands in daily_candidates.items():
        cands.sort(key=lambda c: (c["box_range_pct"], -c["vol_ratio"], abs(c.get("rsi", 50) - 50)))
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
            "path": "box_breakout",
            "path_label": "箱体回踩确认" if pullback_confirm else "箱体突破+MA60",
            "signal_date": c["signal_date"], "signal_close": closes[c["idx"]],
            "box_high": c["box_high"], "box_low": c["box_low"],
            "box_range_pct": c["box_range_pct"], "box_days": c["box_days"],
            "breakout_close": c["breakout_close"],
            "vol_ratio_vs_pullback": c["vol_ratio"],
            "ma5_slope": c.get("ma5_slope", 0),
            "ma10_slope": c.get("ma10_slope", 0),
            "ma5_accel": c.get("ma5_accel", 0),
            "turnover": c.get("turnover", 0),
            "circ_mcap": c.get("circ_mcap", 0),
            "ma60": c["ma60"], "rsi": c.get("rsi", 0),
            "macd_hist": c.get("macd_hist", 0),
            "close_vs_ma60": c["close_vs_ma60"],
            "entry_date": c["entry_date"],
            "entry_price": round(c["entry_price"], 3),
            "buy_mode": "pullback_confirm_next_open" if pullback_confirm else "box_breakout_next_open",
            "neckline_high": c["box_high"], "neckline_gain_pct": c["box_range_pct"],
            "first_trough_low": c["box_low"], "second_trough_low": c["box_low"],
            "peak_high": c["box_high"], "peak_gain_pct": c["box_range_pct"],
            "pullback_low": c["box_low"], "pullback_pct": 0,
            "breakout_pct": round((c["breakout_close"] / c["box_high"] - 1) * 100, 2),
            "accel_gain": 0, "accel_days": 0, "pullback_days": 0,
            "wave1_days": 0, "wave2_days": 0, "wave3_days": 0, "wave_total_days": 0,
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
    # ── 创业板 (300/301) ── 少量活跃股
    "300003","300009","300012","300014","300015","300017","300024",
    "300027","300033","300037","300042","300044","300058","300059",
    "300070","300072","300073","300078","300088","300098","300115",
    "300118","300122","300124","300130","300133","300136","300140",
    "300142","300144","300146","300152","300166","300168","300170",
    "300171","300176","300182","300188","300197","300207","300212",
    "300223","300226","300233","300236","300244","300251","300253",
    "300257","300271","300274","300284","300285","300296","300308",
    "300315","300316","300323","300324","300327","300347","300357",
    "300363","300373","300376","300383","300390","300394","300395",
    "300398","300408","300413","300418","300433","300438","300442",
    "300450","300454","300457","300459","300474","300482","300487",
    "300496","300498","300502","300529","300558","300568","300595",
    "300601","300618","300628","300630","300661","300676","300699",
    "300724","300750","300760","300763","300769","300773","300782",
    "300832","300841","300861","300866","300888","300896","301269",
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
    parser = argparse.ArgumentParser(description="箱体突破 + 站上MA60 策略 独立回测")
    parser.add_argument("--codes", default="", help="逗号分隔的股票代码")
    parser.add_argument("--days", type=int, default=300, help="加载K线天数 (默认300)")
    parser.add_argument("--source", choices=["manual", "db"], default="manual",
                        help="数据源: manual(默认), db")

    # 突破参数
    parser.add_argument("--vol-expand-min", type=float, default=1.5,
                        help="突破放量下限 (默认1.5)")
    parser.add_argument("--vol-expand-max", type=float, default=3.0,
                        help="突破放量上限 (默认3.0)")

    # 箱体参数
    parser.add_argument("--box-days", type=int, default=20,
                        help="箱体整理天数 (默认20)")
    parser.add_argument("--box-max-range", type=float, default=15.0,
                        help="箱体最大振幅%% (默认15.0)")
    parser.add_argument("--box-min-range", type=float, default=0.0,
                        help="箱体最小振幅%% (默认0, 建议8)")
    parser.add_argument("--box-min-bars", type=int, default=10,
                        help="箱体内最少K线数 (默认10)")

    # 过滤参数
    parser.add_argument("--require-ma60", action="store_true", default=False,
                        help="要求站上60日均线 (默认关闭)")
    parser.add_argument("--require-ma20-up", action="store_true", default=False,
                        help="要求MA20向上 (默认关闭)")
    parser.add_argument("--min-rsi", type=float, default=0,
                        help="RSI下限 (默认0)")
    parser.add_argument("--max-rsi", type=float, default=100,
                        help="RSI上限 (默认100)")
    parser.add_argument("--min-macd-hist", type=float, default=0,
                        help="MACD柱最小值过滤 (默认0, 建议0.5)")
    parser.add_argument("--pullback-confirm", action="store_true", default=False,
                        help="启用突破后回踩确认模式 (默认关闭)")
    parser.add_argument("--pullback-days", type=int, default=3,
                        help="回踩确认天数 (默认3)")
    parser.add_argument("--mid-term", action="store_true", default=False,
                        help="中线模式: MA5/10斜率+换手率+MACD1~2+温和突破")

    # 出场参数
    parser.add_argument("--stop-loss", type=float, default=12.0,
                        help="固定止损%% (默认5.0, 主板自动用8%%)")
    parser.add_argument("--trailing-pct", type=float, default=5.0,
                        help="跟踪止损回撤%% (默认5.0)")
    parser.add_argument("--trailing-activate", type=float, default=5.0,
                        help="跟踪止损激活门槛%% (默认5.0)")
    parser.add_argument("--take-profit", type=float, default=15.0,
                        help="止盈%% (默认15.0)")
    parser.add_argument("--max-hold", type=int, default=10,
                        help="最大持仓天数 (默认15)")
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

    # 中线模式参数 (基于1422笔原版数据分析)
    if args.mid_term:
        args.box_days = 15           # 箱体缩短
        args.box_max_range = 15.0    # 振幅15% (数据:8~12%最佳)
        args.box_min_range = 5.0     # 最小5%
        args.require_ma60 = True
        args.require_ma20_up = False
        args.min_macd_hist = 1.0     # MACD柱1~2: 58.8%胜率
        args.max_macd_hist = 2.0     # 上限2 (避免暴量)
        args.min_rsi = 0
        args.max_rsi = 100           # 不用RSI过滤
        args.vol_expand_min = 1.5    # 放量1.5~2.5x (温和)
        args.vol_expand_max = 2.5
        args.max_hold = 10           # 持仓上限10天
        args.stop_loss = 8.0         # 主板止损8%
        args.trailing_pct = 5.0
        args.take_profit = 15.0

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else TEST_CODES
    use_db = args.source == "db"

    if use_db:
        print("  DB模式: 从数据库加载全市场...")
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")

    print(f"{'=' * 80}")
    print(f"箱体突破 + 站上MA60 策略 独立回测")
    print(f"{'=' * 80}")
    print(f"入场条件:")
    print(f"  ① 箱体: {args.box_days}日内价格在区间震荡, 振幅{args.box_min_range}%~{args.box_max_range}%")
    print(f"  ② 突破: 收盘价>箱体上沿, 收阳线")
    if args.require_ma60:
        print(f"  ③ MA60: 突破日收盘站上60日均线")
    print(f"  ④ 放量: 突破日量>=区间均量x{args.vol_expand_min}")
    if args.min_macd_hist > 0:
        print(f"  ⑤ MACD柱>={args.min_macd_hist}")
    if args.require_ma20_up:
        print(f"  ⑥ MA20向上")
    if args.min_rsi > 0 or args.max_rsi < 100:
        print(f"  ⑦ RSI {args.min_rsi:.0f}-{args.max_rsi:.0f}")
    if args.pullback_confirm:
        print(f"  ⑧ 回踩确认: 突破后{args.pullback_days}天内不破箱体上沿")
    print(f"出场条件:")
    print(f"  ① 止盈: {args.take_profit}%")
    print(f"  ② 止损: {args.stop_loss}%")
    print(f"  ③ 跟踪止损: {args.trailing_pct}%（{args.trailing_activate}%后激活）")
    print(f"  ④ 持仓上限: {args.max_hold}天")
    print(f"股票: {len(codes)}只\n")

    all_trades = []
    success = 0

    for i, code in enumerate(codes):
        bars = fetch_kline_db(code, args.days) if use_db else fetch_kline(code, args.days)
        if not bars:
            continue

        # 板块自适应参数
        board = get_board_name(code)
        if args.board_adaptive and board in ('沪主板', '深主板'):
            _stop_loss = 8.0
        else:
            _stop_loss = args.stop_loss

        trades = strategy_peak_breakout(
            bars, code,
            box_days=args.box_days,
            box_max_range=args.box_max_range,
            box_min_range=args.box_min_range,
            box_min_bars=args.box_min_bars,
            vol_expand_min=args.vol_expand_min,
            vol_expand_max=args.vol_expand_max,
            stop_loss_pct=_stop_loss,
            trailing_pct=args.trailing_pct,
            trailing_activate_pct=args.trailing_activate,
            take_profit_pct=args.take_profit,
            max_hold_days=args.max_hold,
            top_per_day=args.top_per_day,
            require_ma60=True,
            require_ma20_up=args.require_ma20_up,
            min_rsi=args.min_rsi,
            max_rsi=args.max_rsi,
            min_macd_hist=args.min_macd_hist,
            max_macd_hist=getattr(args, "max_macd_hist", 100),
            pullback_confirm=args.pullback_confirm,
            pullback_days=args.pullback_days,
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

        # 按策略路径统计
        print(f"\n--- 策略路径统计 ---")
        for path_label in sorted(set(t.get('path_label', t.get('path', '')) for t in all_trades)):
            seg = [t for t in all_trades if t.get('path_label', t.get('path', '')) == path_label]
            if seg:
                print_stats(seg, path_label)

        # 按出场原因统计
        print(f"\n--- 出场原因统计 ---")
        from collections import Counter
        for reason, cnt in Counter(t['exit_reason'] for t in all_trades).most_common():
            seg = [t for t in all_trades if t['exit_reason'] == reason]
            print_stats(seg, reason)

        # 按突破幅度分段
        print(f"\n--- 突破幅度分段 ---")
        for lo, hi in [(0, 1), (1, 3), (3, 5), (5, 100)]:
            seg = [t for t in all_trades if lo <= t['breakout_pct'] < hi]
            if seg:
                print_stats(seg, f"突破[{lo},{hi})%")

        # 按洗盘天数分段
        print(f"\n--- 洗盘天数分段 ---")
        for lo, hi in [(3, 8), (8, 12), (12, 20), (20, 100)]:
            seg = [t for t in all_trades if lo <= t['pullback_days'] < hi]
            if seg:
                print_stats(seg, f"洗盘[{lo},{hi})天")

        # 按放量倍数分段
        print(f"\n--- 放量倍数分段(相对洗盘期间) ---")
        for lo, hi in [(1.0, 1.3), (1.3, 1.5), (1.5, 2.0), (2.0, 3.0)]:
            seg = [t for t in all_trades if lo <= t['vol_ratio_vs_pullback'] < hi]
            if seg:
                print_stats(seg, f"放量[{lo},{hi})x")

        # MA5斜率
        print(f"\n--- MA5斜率 ---")
        for lo,hi,label in [(-99,0,'负'),(0,0.3,'缓'),(0.3,0.6,'中'),(0.6,99,'快')]:
            ts=[t for t in all_trades if lo<=t.get('ma5_slope',0)<hi]
            if ts: print_stats(ts, label)

        # MA5加速度
        print(f"\n--- MA5加速度 ---")
        for lo,hi,label in [(-99,-0.1,'减速'),(-0.1,0.1,'平稳'),(0.1,0.3,'加速'),(0.3,99,'强加速')]:
            ts=[t for t in all_trades if lo<=t.get('ma5_accel',0)<hi]
            if ts: print_stats(ts, label)

        # MA10斜率
        print(f"\n--- MA10斜率 ---")
        for lo,hi,label in [(-99,0,'负'),(0,0.2,'缓'),(0.2,0.5,'中'),(0.5,99,'快')]:
            ts=[t for t in all_trades if lo<=t.get('ma10_slope',0)<hi]
            if ts: print_stats(ts, label)

        # 流通市值
        print(f"\n--- 流通市值 ---")
        for lo,hi,label in [(0,50,'<50亿'),(50,100,'50~100亿'),(100,500,'100~500亿'),(500,2000,'500~2000亿')]:
            ts=[t for t in all_trades if lo<=t.get('circ_mcap',0)<hi]
            if ts: print_stats(ts, label)

        # 换手率
        print(f"\n--- 换手率 ---")
        for lo,hi in [(0,2),(2,5),(5,10),(10,20),(20,100)]:
            ts=[t for t in all_trades if lo<=t.get('turnover',0)<hi]
            if ts: print_stats(ts, f"[{lo},{hi})%")

        # TOP N
        n = min(args.top, len(all_trades))
        print(f"\n--- TOP {n} 最佳交易 ---")
        for t in sorted(all_trades, key=lambda x: -x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"第一底{t['first_trough_low']} 颈线{t['neckline_high']} "
                  f"第二底{t['second_trough_low']} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

        print(f"\n--- TOP {n} 最差交易 ---")
        for t in sorted(all_trades, key=lambda x: x['return_pct'])[:n]:
            print(f"  {t['code']}({t['board']}) {t['signal_date']} "
                  f"第一底{t['first_trough_low']} 颈线{t['neckline_high']} "
                  f"第二底{t['second_trough_low']} "
                  f"→ {t['entry_date']}买{t['entry_price']:>7.2f} "
                  f"收益{t['return_pct']:+.2f}% 持仓{t['exit_day']}天")

    # 保存JSON
    if all_trades:
        out_file = "test_box_breakout_result.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(all_trades, f, ensure_ascii=False, indent=2)
        print(f"\n  {out_file} ({len(all_trades)}笔)")
