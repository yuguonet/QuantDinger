#!/usr/bin/env python3
"""
盘中实时监控 - 快速拉升选股
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

基于4维度实时评分:
  1. 快速拉升: 与上一分钟比涨幅 ≥2% 连续拉升, 得分 = 涨幅 × 连续分钟数
  2. 放量拉升: 放量 = 当分钟量 / 过去5分钟均量, 得分 = 放量倍数 × 连续放量分钟数
  3. 龙虎榜: 前40日是否在龙虎榜 + 上榜次数
  4. 预估日成交量: 预估当日成交量 / 昨日成交量, ≥1.2倍得高分

用法:
  python realtime_rally_monitor.py                    # 实时监控
  python realtime_rally_monitor.py --codes 000001,600519  # 指定股票
  python realtime_rally_monitor.py --top 20            # 显示前N只
  python realtime_rally_monitor.py --min-score 5       # 最低评分过滤
  python realtime_rally_monitor.py --no-lhb            # 排除龙虎榜要求
"""
from __future__ import annotations
import os, sys, time, argparse, json
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

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
# DB 工具
# ================================================================
def _snapshot_table_name() -> str:
    return f"realtime_snapshot_{datetime.now().year}"

def fetch_realtime_snapshot(codes: List[str]) -> Dict[str, Dict]:
    """从 realtime_snapshot_YYYY 读取最新快照"""
    if not codes:
        return {}
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        table = _snapshot_table_name()
        today = datetime.now().strftime("%Y-%m-%d")
        
        placeholders = ", ".join(["%s"] * len(codes))
        sql = f"""
            SELECT DISTINCT ON (symbol)
                symbol, "last", open, high, low, "previousClose", volume, extras, time
            FROM "{table}"
            WHERE symbol IN ({placeholders})
              AND time >= %s
            ORDER BY symbol, time DESC
        """
        params = list(codes) + [f"{today} 00:00:00"]
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        
        result = {}
        for row in rows:
            sym = row[0]
            last = float(row[1] or 0)
            if last <= 0:
                continue
            extras = row[7]
            if isinstance(extras, str):
                try:
                    extras = json.loads(extras)
                except:
                    extras = {}
            result[sym] = {
                'symbol': sym,
                'last': last,
                'open': float(row[2] or 0),
                'high': float(row[3] or 0),
                'low': float(row[4] or 0),
                'previousClose': float(row[5] or 0),
                'volume': float(row[6] or 0),
                'time': str(row[8]),
            }
            if isinstance(extras, dict):
                for k, v in extras.items():
                    if k not in result[sym] and v is not None:
                        result[sym][k] = v
        return result
    except Exception as e:
        print(f"  ⚠️ 读取快照失败: {e}")
        return {}

def fetch_intraday_series(code: str, minutes: int = 240) -> List[Dict]:
    """从 realtime_snapshot_YYYY 读取今日分时序列"""
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        table = _snapshot_table_name()
        today = datetime.now().strftime("%Y-%m-%d")
        
        sql = f"""
            SELECT time, "last", open, high, low, volume, extras
            FROM "{table}"
            WHERE symbol = %s AND time >= %s
            ORDER BY time ASC
            LIMIT %s
        """
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [code, f"{today} 00:00:00", minutes])
                rows = cur.fetchall()
        
        result = []
        for row in rows:
            last = float(row[1] or 0)
            if last <= 0:
                continue
            extras = row[6]
            if isinstance(extras, str):
                try:
                    extras = json.loads(extras)
                except:
                    extras = {}
            result.append({
                'time': str(row[0]),
                'last': last,
                'open': float(row[2] or 0),
                'high': float(row[3] or 0),
                'low': float(row[4] or 0),
                'volume': float(row[5] or 0),
            })
            if isinstance(extras, dict):
                for k, v in extras.items():
                    if k not in result[-1] and v is not None:
                        result[-1][k] = v
        return result
    except Exception as e:
        return []

def fetch_prev_volume(code: str) -> float:
    """获取昨日成交量"""
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        
        # 找昨日日期
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        sql = """
            SELECT volume FROM kline_1D_cnstock 
            WHERE symbol = %s AND time >= %s AND time < %s
            ORDER BY time DESC LIMIT 1
        """
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [code, yesterday, f"{yesterday} 23:59:59"])
                row = cur.fetchone()
        return float(row[0]) if row else 0
    except:
        return 0

def fetch_lhb_count(code: str, days: int = 40) -> Tuple[bool, int]:
    """获取40日内龙虎榜上榜次数"""
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        
        sql = """
            SELECT COUNT(*) FROM cnd_dragon_tiger_list
            WHERE stock_code = %s AND trade_date >= %s
        """
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [code, start_date])
                row = cur.fetchone()
        cnt = int(row[0]) if row else 0
        return (cnt > 0, cnt)
    except:
        return (False, 0)

def get_all_codes() -> List[str]:
    """获取全市场股票列表"""
    try:
        from app.utils.db_market import get_market_kline_writer
        writer = get_market_kline_writer()
        stats = writer.stats("CNStock")
        return stats.get("symbol_list", []) if stats.get("exists") else []
    except:
        return []

def get_stock_name(code: str) -> str:
    """获取股票名称"""
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        pool = db._get_pool()
        with pool.cursor() as cur:
            cur.execute("SELECT name FROM stock_basic_info WHERE symbol = %s", [code])
            row = cur.fetchone()
        return row[0] if row else ""
    except:
        return ""

def get_board_type(code: str) -> str:
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"

# ================================================================
# 核心评分逻辑
# ================================================================
def analyze_rally(series: List[Dict], prev_vol: float, lhb_in_40d: bool, lhb_count: int) -> Dict:
    """
    盘中实时评分
    
    返回:
      - score: 总分
      - rise_score: 快速拉升得分
      - volume_score: 放量拉升得分
      - lhb_score: 龙虎榜得分
      - est_vol_score: 预估成交量得分
      - details: 明细
    """
    if len(series) < 5:
        return {"score": 0, "rise_score": 0, "volume_score": 0, 
                "lhb_score": 0, "est_vol_score": 0, "details": "数据不足"}
    
    # 1. 快速拉升分析
    rise_scores = []
    rise_streak = 0
    max_rise_streak = 0
    for i in range(1, len(series)):
        prev = series[i-1]['last']
        cur = series[i]['last']
        if prev <= 0:
            continue
        change = (cur - prev) / prev * 100
        if change >= 2.0:
            rise_streak += 1
            max_rise_streak = max(max_rise_streak, rise_streak)
            rise_scores.append(change * rise_streak)  # 涨幅 × 连续分钟
        else:
            rise_streak = 0
    
    rise_score = sum(rise_scores) if rise_scores else 0
    rise_score = min(rise_score, 30)  # 上限30分
    
    # 2. 放量拉升分析
    volumes = [s['volume'] for s in series]
    vol_scores = []
    vol_streak = 0
    max_vol_streak = 0
    for i in range(5, len(volumes)):
        avg_vol_5 = sum(volumes[i-5:i]) / 5
        if avg_vol_5 <= 0:
            continue
        vol_ratio = volumes[i] / avg_vol_5
        if vol_ratio >= 1.2:
            vol_streak += 1
            max_vol_streak = max(max_vol_streak, vol_streak)
            vol_scores.append(vol_ratio * vol_streak)
        else:
            vol_streak = 0
    
    volume_score = sum(vol_scores) if vol_scores else 0
    volume_score = min(volume_score, 25)  # 上限25分
    
    # 3. 龙虎榜得分
    lhb_score = 0
    if lhb_in_40d:
        lhb_score = 5  # 基础分
        lhb_score += min(lhb_count * 2, 15)  # 次数加分, 最多+15
    # 4. 预估成交量得分
    est_vol_score = 0
    if prev_vol > 0:
        current_vol = series[-1]['volume']
        # 估算当日总量 = 当前量 × (240 / 当前分钟数)
        min_idx = len(series)
        est_total_vol = current_vol * (240 / min_idx) if min_idx > 0 else current_vol
        est_ratio = est_total_vol / prev_vol
        if est_ratio >= 1.2:
            est_vol_score = min((est_ratio - 1) * 20, 20)  # 超过1.2倍的部分加分
        elif est_ratio >= 1.0:
            est_vol_score = (est_ratio - 1.0) * 10
    
    # 5. 总分
    total_score = rise_score + volume_score + lhb_score + est_vol_score
    
    return {
        "score": round(total_score, 1),
        "rise_score": round(rise_score, 1),
        "rise_streak": max_rise_streak,
        "volume_score": round(volume_score, 1),
        "vol_streak": max_vol_streak,
        "lhb_score": lhb_score,
        "lhb_in_40d": lhb_in_40d,
        "lhb_count": lhb_count,
        "est_vol_score": round(est_vol_score, 1),
        "details": f"拉升{max_rise_streak}连涨, 放量{max_vol_streak}连分钟, 龙虎{lhb_count}次, 量比{est_ratio:.2f}x" if prev_vol > 0 else "昨日成交量缺失"
    }

# ================================================================
# 主程序
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="盘中实时监控 - 快速拉升选股")
    parser.add_argument("--codes", type=str, default="", help="指定股票代码,逗号分隔")
    parser.add_argument("--top", type=int, default=20, help="显示前N只")
    parser.add_argument("--min-score", type=float, default=3.0, help="最低评分")
    parser.add_argument("--no-lhb", action="store_true", help="排除龙虎榜要求")
    parser.add_argument("--interval", type=int, default=60, help="轮询间隔(秒)")
    parser.add_argument("--once", action="store_true", help="只运行一次")
    args = parser.parse_args()
    
    # 获取股票列表
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = get_all_codes()
        print(f"全市场 {len(codes)} 只股票")
    
    # 预加载昨日成交量
    print("预加载昨日成交量...")
    prev_vols = {}
    for i, code in enumerate(codes):
        if i % 500 == 0:
            print(f"  进度 {i}/{len(codes)}")
        prev_vols[code] = fetch_prev_volume(code)
    
    # 预加载龙虎榜
    print("预加载龙虎榜(40日)...")
    lhb_cache = {}
    for i, code in enumerate(codes):
        if i % 500 == 0:
            print(f"  进度 {i}/{len(codes)}")
        lhb_cache[code] = fetch_lhb_count(code, 40)
    
    print(f"\n预加载完成, 昨日有成交量: {sum(1 for v in prev_vols.values() if v > 0)} 只")
    print(f"40日内有龙虎榜: {sum(1 for v in lhb_cache.values() if v[0])} 只")
    
    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n{'='*50}")
        print(f"🕐 {ts} 实时监控 | 候选 {len(codes)} 只")
        
        # 批量获取快照
        quotes = fetch_realtime_snapshot(codes)
        if not quotes:
            print("  ⚠️ 无法获取实时数据")
            if args.once:
                break
            time.sleep(args.interval)
            continue
        
        # 并行获取分时数据并评分
        results = []
        for code in codes:
            if code not in quotes:
                continue
            q = quotes[code]
            series = fetch_intraday_series(code, 240)
            if len(series) < 5:
                continue
            
            prev_vol = prev_vols.get(code, 0)
            lhb_in_40d, lhb_count = lhb_cache.get(code, (False, 0))
            
            # 如果开启 no-lhb, 忽略龙虎榜条件
            if args.no_lhb:
                lhb_in_40d, lhb_count = False, 0
            
            result = analyze_rally(series, prev_vol, lhb_in_40d, lhb_count)
            result['code'] = code
            result['name'] = get_stock_name(code)
            result['last'] = q['last']
            result['change_pct'] = round((q['last'] / q['previousClose'] - 1) * 100, 2) if q['previousClose'] > 0 else 0
            result['volume'] = q['volume']
            results.append(result)
        
        # 排序
        results.sort(key=lambda x: x['score'], reverse=True)
        
        # 过滤并显示
        filtered = [r for r in results if r['score'] >= args.min_score]
        print(f"  符合条件: {len(filtered)} / {len(results)}")
        
        top_n = filtered[:args.top]
        for i, r in enumerate(top_n, 1):
            print(f"  {i:2d}. {r['code']} {r['name']:<6} "
                  f"现{r['last']:.2f} {r['change_pct']:+.2f}% "
                  f"总分{r['score']:>4.1f} "
                  f"[拉{r['rise_streak']}连+{r['rise_score']:.1f} "
                  f"放{r['vol_streak']}连+{r['volume_score']:.1f} "
                  f"龙{r['lhb_count']}次+{r['lhb_score']:.1f} "
                  f"量+{r['est_vol_score']:.1f}] "
                  f"{r['details']}")
        
        if args.once:
            break
        
        time.sleep(args.interval)

if __name__ == "__main__":
    main()