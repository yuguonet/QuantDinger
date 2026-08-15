#!/usr/bin/env python3
"""每日盘后扫描: 游资偏好股票预筛选

扫描全市场A股，筛选符合游资D0前20天技术形态的股票:
  基础条件:
    1. 均线多头排列 (MA5 > MA10 > MA20)
    2. 位置在20天高位 (>80%)
    3. 前20天涨幅 > 10%
    4. 前5天缩量 (前5天均量/前20天均量 < 1)
  增强条件:
    5. RSI(14) > 80 (超强动量)
    6. KDJ K > 80 或 J > 100 (超买区)
    7. 10天不破MA10 (强势支撑)
    8. 5天低点抬高 (趋势延续)

用法:
  python daily_prescan.py                            # 精选模式
  python daily_prescan.py --mode entry               # 入门模式
  python daily_prescan.py --mode balance             # 平衡模式
  python daily_prescan.py --mode elite               # 精英模式(RSI+KDJ)
  python daily_prescan.py --mode custom --min-rsi 80 # 自定义
  python daily_prescan.py --out watchlist.json       # 输出到文件
"""
from __future__ import annotations
import json, argparse, os, sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ================================================================
# 环境初始化
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

_load_env()

# ================================================================
# 数据库
# ================================================================
_writer_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache

_pool_cache = None
def _get_cnstock_pool():
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    _pool_cache = mgr._get_pool("CNStock")
    return _pool_cache


def fetch_kline_db(code: str, days: int = 60) -> List[Dict]:
    """从数据库加载K线(前复权)"""
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        bars = []
        for r in data:
            bars.append({
                "time": str(r["time"])[:10],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
        return unadj_to_qfq(bars, code)
    except Exception:
        return []


def get_all_stock_codes() -> List[str]:
    """获取全市场A股代码列表"""
    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT code FROM cnstock_kline_1d "
                "WHERE time >= %s ORDER BY code",
                ((datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"),)
            )
            rows = cur.fetchall()
            cur.close()
            return [r[0] for r in rows]
    except Exception:
        try:
            pool = _get_cnstock_pool()
            with pool.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT stock_code FROM cnd_dragon_tiger_list ORDER BY stock_code")
                rows = cur.fetchall()
                cur.close()
                return [r[0] for r in rows]
        except Exception as e:
            print(f"  [DB] 获取股票列表失败: {e}", file=sys.stderr)
            return []


def get_board_name(code: str) -> str:
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"


# ================================================================
# 技术指标计算
# ================================================================

def _calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def _calc_kdj(bars: List[Dict], n: int = 9) -> tuple:
    if len(bars) < n:
        return None, None, None
    rsv_list = []
    for i in range(len(bars)):
        start = max(0, i - n + 1)
        high_n = max(b['high'] for b in bars[start:i+1])
        low_n = min(b['low'] for b in bars[start:i+1])
        if high_n == low_n:
            rsv_list.append(50)
        else:
            rsv_list.append((bars[i]['close'] - low_n) / (high_n - low_n) * 100)
    k, d = 50, 50
    for rsv in rsv_list:
        k = (2/3) * k + (1/3) * rsv
        d = (2/3) * d + (1/3) * k
    j = 3 * k - 2 * d
    return round(k, 1), round(d, 1), round(j, 1)


def _calc_ma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_features(bars: List[Dict]) -> Optional[Dict]:
    """计算20天技术特征 (含RSI/KDJ/均线支撑)

    Returns:
        特征字典, 数据不足返回None
    """
    if len(bars) < 20:
        return None

    recent = bars[-20:]
    closes = [b['close'] for b in recent]
    volumes = [b['volume'] for b in recent]

    # === 均线 ===
    ma5 = _calc_ma(closes[-5:], 5)
    ma10 = _calc_ma(closes[-10:], 10)
    ma20 = _calc_ma(closes, 20)
    ma_bull = ma5 > ma10 > ma20 if (ma5 and ma10 and ma20) else False

    # === 趋势 ===
    pre20_open = recent[0]['open']
    pre20_close = recent[-1]['close']
    pre20_trend = (pre20_close / pre20_open - 1) * 100 if pre20_open > 0 else 0

    # === 位置 ===
    pre20_high = max(b['high'] for b in recent)
    pre20_low = min(b['low'] for b in recent)
    pre20_position = (pre20_close - pre20_low) / (pre20_high - pre20_low) * 100 if pre20_high > pre20_low else 50

    # === 量能 ===
    avg_vol = sum(volumes) / len(volumes)
    last5_vol = sum(volumes[-5:]) / 5
    vol_ratio_5_20 = last5_vol / avg_vol if avg_vol > 0 else 1
    last_vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

    # === RSI ===
    rsi14 = _calc_rsi(closes, 14)
    rsi6 = _calc_rsi(closes, 6)

    # === KDJ ===
    kdj_k, kdj_d, kdj_j = _calc_kdj(recent, 9)
    kdj_golden = (kdj_k and kdj_d and kdj_k > kdj_d)

    # === 不破MA10: 最近10天收盘都在MA10之上(允许1天破) ===
    never_below_ma10 = False
    if ma10:
        ma10_arr = []
        for i in range(max(0, len(closes)-10), len(closes)):
            start = max(0, i - 9)
            ma10_arr.append(sum(closes[start:i+1]) / (i - start + 1))
        above_count = sum(1 for i, c in enumerate(closes[-10:]) if c >= ma10_arr[i] * 0.995)
        never_below_ma10 = above_count >= 9

    # === 5天低点抬高: 最近5天低点都在前15天低点之上 ===
    pre15_low = min(b['low'] for b in recent[:15])
    last5_low_rising = all(b['low'] >= pre15_low for b in recent[-5:])

    # === 涨天数 ===
    up_days = sum(1 for b in recent if (b['close'] / b['open'] - 1) * 100 > 0)

    # === 收盘相对MA20偏离 ===
    close_vs_ma20 = (pre20_close / ma20 - 1) * 100 if ma20 > 0 else 0

    return {
        # 基础
        "ma5": round(ma5, 3) if ma5 else 0,
        "ma10": round(ma10, 3) if ma10 else 0,
        "ma20": round(ma20, 3) if ma20 else 0,
        "ma_bull": ma_bull,
        "pre20_trend": round(pre20_trend, 2),
        "pre20_position": round(pre20_position, 1),
        "vol_ratio_5_20": round(vol_ratio_5_20, 2),
        "last_vol_ratio": round(last_vol_ratio, 2),
        "up_days": up_days,
        "close_vs_ma20": round(close_vs_ma20, 2),
        "last_close": pre20_close,
        "last_volume": int(volumes[-1]),
        # 增强
        "rsi14": round(rsi14, 1) if rsi14 else None,
        "rsi6": round(rsi6, 1) if rsi6 else None,
        "kdj_k": kdj_k,
        "kdj_d": kdj_d,
        "kdj_j": kdj_j,
        "kdj_golden": kdj_golden,
        "never_below_ma10": never_below_ma10,
        "last5_low_rising": last5_low_rising,
    }


def calc_score(f: Dict) -> float:
    """计算综合评分 (0~100)"""
    score = 0
    # 多头排列 (30分)
    if f['ma_bull']:
        score += 30
    # 位置 (20分)
    if f['pre20_position'] > 80:
        score += 20
    elif f['pre20_position'] > 60:
        score += 10
    # 趋势 (20分)
    if f['pre20_trend'] > 10:
        score += 20
    elif f['pre20_trend'] > 5:
        score += 10
    # 缩量 (10分)
    if f['vol_ratio_5_20'] < 0.8:
        score += 10
    elif f['vol_ratio_5_20'] < 1:
        score += 5
    # RSI (10分)
    if f['rsi14'] and f['rsi14'] > 80:
        score += 10
    elif f['rsi14'] and f['rsi14'] > 70:
        score += 5
    # KDJ (10分)
    if f['kdj_k'] and f['kdj_k'] > 80:
        score += 5
    if f['kdj_j'] and f['kdj_j'] > 100:
        score += 5
    # 不破MA10 (5分)
    if f['never_below_ma10']:
        score += 5
    # 低点抬高 (5分)
    if f['last5_low_rising']:
        score += 5
    return min(100, score)


# ================================================================
# 筛选条件
# ================================================================

def apply_filter(features: Dict, conditions: Dict) -> bool:
    """检查是否满足筛选条件"""
    # 均线
    if conditions.get('ma_bull') and not features['ma_bull']:
        return False
    # 趋势
    if 'min_trend' in conditions and features['pre20_trend'] < conditions['min_trend']:
        return False
    if 'max_trend' in conditions and features['pre20_trend'] > conditions['max_trend']:
        return False
    # 位置
    if 'min_position' in conditions and features['pre20_position'] < conditions['min_position']:
        return False
    # 量能
    if 'max_vol_5_20' in conditions and features['vol_ratio_5_20'] >= conditions['max_vol_5_20']:
        return False
    if 'min_vol_5_20' in conditions and features['vol_ratio_5_20'] < conditions['min_vol_5_20']:
        return False
    # RSI
    if 'min_rsi14' in conditions:
        if features['rsi14'] is None or features['rsi14'] < conditions['min_rsi14']:
            return False
    if 'max_rsi14' in conditions:
        if features['rsi14'] is not None and features['rsi14'] > conditions['max_rsi14']:
            return False
    if 'min_rsi6' in conditions:
        if features['rsi6'] is None or features['rsi6'] < conditions['min_rsi6']:
            return False
    # KDJ
    if conditions.get('kdj_overbought'):
        if not features['kdj_k'] or features['kdj_k'] <= 80:
            return False
    if conditions.get('kdj_j_extreme'):
        if not features['kdj_j'] or features['kdj_j'] <= 100:
            return False
    if conditions.get('kdj_golden'):
        if not features['kdj_golden']:
            return False
    # 均线支撑
    if conditions.get('never_below_ma10'):
        if not features['never_below_ma10']:
            return False
    # 低点抬高
    if conditions.get('last5_low_rising'):
        if not features['last5_low_rising']:
            return False
    return True


# ================================================================
# 预设模式
# ================================================================

MODES = {
    "entry": {
        "name": "入门模式",
        "conditions": {
            "ma_bull": True,
            "min_trend": 10,
            "min_position": 80,
        },
        "description": "多头+位置>80%+涨>10% | ~250只",
    },
    "balance": {
        "name": "平衡模式",
        "conditions": {
            "ma_bull": True,
            "min_trend": 5,
            "min_position": 80,
            "max_vol_5_20": 1,
        },
        "description": "多头+位置>80%+涨>5%+缩量 | ~100只",
    },
    "select": {
        "name": "精选模式",
        "conditions": {
            "ma_bull": True,
            "min_trend": 10,
            "min_position": 80,
            "max_vol_5_20": 1,
        },
        "description": "多头+位置>80%+涨>10%+缩量 | ~50只",
    },
    "elite": {
        "name": "精英模式",
        "conditions": {
            "ma_bull": True,
            "min_trend": 10,
            "min_position": 80,
            "max_vol_5_20": 1,
            "min_rsi14": 80,
        },
        "description": "基础+RSI14>80 | ~20只 | 均涨+23.6%",
    },
    "elite_kdj": {
        "name": "精英KDJ模式",
        "conditions": {
            "ma_bull": True,
            "min_trend": 10,
            "min_position": 80,
            "max_vol_5_20": 1,
            "kdj_overbought": True,
        },
        "description": "基础+KDJ K>80 | ~50只 | 均涨+21.6%",
    },
    "elite_full": {
        "name": "全维度精英模式",
        "conditions": {
            "ma_bull": True,
            "min_trend": 10,
            "min_position": 80,
            "max_vol_5_20": 1,
            "never_below_ma10": True,
            "last5_low_rising": True,
        },
        "description": "基础+不破MA10+低点抬高 | ~30只 | 均涨+20.3%",
    },
}


# ================================================================
# 主扫描
# ================================================================

def scan(mode: str = "select", top: int = 100,
         custom_conditions: Dict = None, exclude_st: bool = True) -> List[Dict]:
    """全市场扫描"""

    if custom_conditions:
        conditions = custom_conditions
        mode_name = "自定义模式"
    else:
        mode_cfg = MODES.get(mode, MODES['select'])
        conditions = mode_cfg['conditions']
        mode_name = mode_cfg['name']

    desc = MODES.get(mode, {}).get('description', '')
    print(f"📊 扫描模式: {mode_name}", file=sys.stderr)
    if desc:
        print(f"   {desc}", file=sys.stderr)
    print(f"   条件: {conditions}", file=sys.stderr)

    print(f"📋 获取股票列表...", file=sys.stderr)
    codes = get_all_stock_codes()
    print(f"   共 {len(codes)} 只", file=sys.stderr)

    if not codes:
        print("❌ 无股票数据", file=sys.stderr)
        return []

    results = []
    errors = 0
    scanned = 0

    for i, code in enumerate(codes):
        if (i + 1) % 500 == 0:
            print(f"   进度: {i+1}/{len(codes)} 已找到{len(results)}只", file=sys.stderr)

        bars = fetch_kline_db(code, 60)
        if not bars or len(bars) < 20:
            errors += 1
            continue

        scanned += 1
        features = calc_features(bars)
        if not features:
            continue

        if not apply_filter(features, conditions):
            continue

        score = calc_score(features)
        results.append({
            "code": code,
            "board": get_board_name(code),
            "score": score,
            "last_close": features['last_close'],
            "pre20_trend": features['pre20_trend'],
            "pre20_position": features['pre20_position'],
            "vol_ratio_5_20": features['vol_ratio_5_20'],
            "up_days": features['up_days'],
            "close_vs_ma20": features['close_vs_ma20'],
            "rsi14": features['rsi14'],
            "rsi6": features['rsi6'],
            "kdj_k": features['kdj_k'],
            "kdj_j": features['kdj_j'],
            "kdj_golden": features['kdj_golden'],
            "never_below_ma10": features['never_below_ma10'],
            "last5_low_rising": features['last5_low_rising'],
            "features": features,
        })

    results.sort(key=lambda x: -x['score'])
    print(f"\n✅ 扫描完成: {scanned}只 → 命中{len(results)}只", file=sys.stderr)
    return results[:top]


# ================================================================
# 主函数
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="每日盘后扫描: 游资偏好股票预筛选")
    parser.add_argument("--mode", type=str, default="select",
                        choices=["entry", "balance", "select", "elite", "elite_kdj", "elite_full", "custom"],
                        help="筛选模式")
    parser.add_argument("--top", type=int, default=100, help="显示前N只")
    parser.add_argument("--out", type=str, default="", help="输出JSON文件")
    # 自定义条件
    parser.add_argument("--min-trend", type=float, default=None, help="最小20天涨幅%%")
    parser.add_argument("--min-pos", type=float, default=None, help="最小位置(0~100)")
    parser.add_argument("--max-vol", type=float, default=None, help="最大5/20量比")
    parser.add_argument("--min-rsi", type=float, default=None, help="最小RSI14")
    parser.add_argument("--max-rsi", type=float, default=None, help="最大RSI14")
    parser.add_argument("--min-rsi6", type=float, default=None, help="最小RSI6")
    parser.add_argument("--ma-bull", action="store_true", help="要求多头排列")
    parser.add_argument("--kdj-overbought", action="store_true", help="KDJ K>80")
    parser.add_argument("--kdj-j", action="store_true", help="KDJ J>100")
    parser.add_argument("--kdj-golden", action="store_true", help="KDJ金叉")
    parser.add_argument("--hold-ma10", action="store_true", help="不破MA10")
    parser.add_argument("--low-rising", action="store_true", help="5天低点抬高")
    parser.add_argument("--all", action="store_true", help="不过滤ST")
    args = parser.parse_args()

    print("=" * 80)
    print("🔍 游资偏好股票每日扫描")
    print("=" * 80)

    # 自定义条件
    custom_conditions = None
    if args.mode == "custom":
        custom_conditions = {}
        if args.ma_bull:
            custom_conditions['ma_bull'] = True
        if args.min_trend is not None:
            custom_conditions['min_trend'] = args.min_trend
        if args.min_pos is not None:
            custom_conditions['min_position'] = args.min_pos
        if args.max_vol is not None:
            custom_conditions['max_vol_5_20'] = args.max_vol
        if args.min_rsi is not None:
            custom_conditions['min_rsi14'] = args.min_rsi
        if args.max_rsi is not None:
            custom_conditions['max_rsi14'] = args.max_rsi
        if args.min_rsi6 is not None:
            custom_conditions['min_rsi6'] = args.min_rsi6
        if args.kdj_overbought:
            custom_conditions['kdj_overbought'] = True
        if args.kdj_j:
            custom_conditions['kdj_j_extreme'] = True
        if args.kdj_golden:
            custom_conditions['kdj_golden'] = True
        if args.hold_ma10:
            custom_conditions['never_below_ma10'] = True
        if args.low_rising:
            custom_conditions['last5_low_rising'] = True
        if not custom_conditions:
            print("❌ custom模式需要指定至少一个条件", file=sys.stderr)
            return

    results = scan(
        mode=args.mode,
        top=args.top,
        custom_conditions=custom_conditions,
        exclude_st=not args.all,
    )

    if not results:
        print("\n❌ 无符合条件的股票")
        return

    mode_info = MODES.get(args.mode, {})
    print(f"\n{'=' * 80}")
    print(f"📊 {mode_info.get('name', '自定义模式')} — {len(results)}只")
    print(f"   {mode_info.get('description', '')}")
    print(f"{'=' * 80}")

    # 表头
    header = (f"  {'排名':>4} {'代码':>8} {'板块':>6} {'评分':>4} "
              f"{'收盘':>8} {'20天涨':>7} {'位置':>6} {'量比':>5} "
              f"{'RSI14':>6} {'KDJ_K':>6} {'涨天':>4} {'MA10':>4} {'低点':>4}")
    print(f"\n{header}")
    print(f"  {'-' * 90}")

    for rank, r in enumerate(results, 1):
        ma10_flag = "✓" if r['never_below_ma10'] else ""
        low_flag = "↑" if r['last5_low_rising'] else ""
        rsi_str = f"{r['rsi14']:.0f}" if r['rsi14'] else "-"
        kdj_str = f"{r['kdj_k']:.0f}" if r['kdj_k'] else "-"

        print(f"  {rank:>4} {r['code']:>8} {r['board']:>6} {r['score']:>4.0f} "
              f"{r['last_close']:>8.2f} {r['pre20_trend']:>+6.1f}% "
              f"{r['pre20_position']:>5.1f}% {r['vol_ratio_5_20']:>4.2f}x "
              f"{rsi_str:>6} {kdj_str:>6} {r['up_days']:>4} {ma10_flag:>4} {low_flag:>4}")

    print(f"\n  列说明: MA10=✓不破10日线 | 低点=↑5天低点抬高")
    print(f"\n  操作建议:")
    print(f"  1. 以上为技术面预筛选, D0盘中需确认涨停/大单信号")
    print(f"  2. 有实时行情则监控盘中异动")
    print(f"  3. 确认D0后, D1按策略入场(高开买/低开等收盘)")

    # 导出
    if args.out:
        output = {
            "meta": {
                "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": args.mode,
                "conditions": custom_conditions or MODES.get(args.mode, {}).get('conditions', {}),
                "total_found": len(results),
            },
            "data": [{k: v for k, v in r.items() if k != 'features'} for r in results],
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"\n💾 已保存到 {args.out}")


if __name__ == "__main__":
    main()
