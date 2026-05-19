#!/usr/bin/env python3
"""
CSV → 前复权 → db_market 批量导入脚本

功能:
  1. 读取 optimizer_output/CNStock/ 下的 CSV 文件
  2. 通过 TDX 除权除息数据构建前复权因子
  3. 将不复权数据转为前复权后写入 PostgreSQL (db_market)

支持:
  - 日线 (daily/*.csv) — 若已是前复权则直接写入，否则复权后写入
  - 分钟线 (1m/5m/15m/30m/1h/*.csv) — 统一做前复权后写入

用法:
  cd QuantDinger-main
  python csv_to_db_adjusted.py                          # 默认导入所有周期
  python csv_to_db_adjusted.py -T 1D                    # 只导日线
  python csv_to_db_adjusted.py -T 15m                   # 只导15分钟线
  python csv_to_db_adjusted.py -T all                   # 全部周期
  python csv_to_db_adjusted.py -T 15m --csv-dir /path   # 指定CSV目录
  python csv_to_db_adjusted.py --dry-run                 # 只看不动
"""

import os
import sys
import csv
import glob
import time
import argparse
from datetime import datetime, timezone, timedelta
from multiprocessing import Pool

# ── 设置项目路径 ──
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.join(_PROJECT_ROOT, "backend_api_python")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

# ── 加载 .env（必须在导入业务模块之前）──
try:
    from dotenv import load_dotenv
    for env_path in [
        os.path.join(_BACKEND_ROOT, '.env'),
        os.path.join(_PROJECT_ROOT, '.env'),
    ]:
        if os.path.isfile(env_path):
            load_dotenv(env_path, override=False)
            break
except ImportError:
    pass

# ── 绕过 app/__init__.py 的 Flask 导入 ──
# adjustment.py 和 db_market.py 不直接依赖 Flask，
# 但 import app 会触发 __init__.py。用 importlib 直接加载子模块。
import types
import importlib
import importlib.util

def _import_module_from_file(module_name, file_path):
    """直接从文件加载模块，跳过包的 __init__.py"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

# 预注册 app 包（空模块，避免触发 __init__.py）
if 'app' not in sys.modules:
    app_pkg = types.ModuleType('app')
    app_pkg.__path__ = [os.path.join(_BACKEND_ROOT, 'app')]
    sys.modules['app'] = app_pkg

for sub in ('app.utils', 'app.data_sources', 'app.data_sources.provider'):
    if sub not in sys.modules:
        m = types.ModuleType(sub)
        parts = sub.split('.')
        m.__path__ = [os.path.join(_BACKEND_ROOT, *parts)]
        parent = '.'.join(parts[:-1])
        setattr(sys.modules[parent], parts[-1], m)
        sys.modules[sub] = m

# 加载 logger（被多个模块依赖）
_import_module_from_file(
    'app.utils.logger',
    os.path.join(_BACKEND_ROOT, 'app', 'utils', 'logger.py')
)

# 加载 normalizer（adjustment.py 依赖）
_import_module_from_file(
    'app.data_sources.normalizer',
    os.path.join(_BACKEND_ROOT, 'app', 'data_sources', 'normalizer.py')
)

# 加载 db_multi（db_market.py 依赖）
_import_module_from_file(
    'app.utils.db_multi',
    os.path.join(_BACKEND_ROOT, 'app', 'utils', 'db_multi.py')
)

# 加载核心模块
adjustment_mod = _import_module_from_file(
    'app.data_sources.provider.adjustment',
    os.path.join(_BACKEND_ROOT, 'app', 'data_sources', 'provider', 'adjustment.py')
)
db_market_mod = _import_module_from_file(
    'app.utils.db_market',
    os.path.join(_BACKEND_ROOT, 'app', 'utils', 'db_market.py')
)

apply_fwd_adjust = adjustment_mod.apply_fwd_adjust
build_fwd_factor = adjustment_mod.build_fwd_factor
get_market_kline_writer = db_market_mod.get_market_kline_writer
get_market_db_manager = db_market_mod.get_market_db_manager

_TZ_SH = timezone(timedelta(hours=8))

# 周期目录映射
PERIOD_DIR = {
    '1D': 'daily', '1m': '1m', '5m': '5m',
    '15m': '15m', '30m': '30m', '60m': '1h',
}

# CSV 列名映射
CSV_DT_COL = {'daily': 'date', '1m': 'datetime', '5m': 'datetime',
              '15m': 'datetime', '30m': 'datetime', '1h': 'datetime'}


def read_csv_klines(csv_path: str, timeframe: str) -> list:
    """读取 CSV 文件，返回 kline 字典列表 [{"time", "open", "high", "low", "close", "volume"}, ...]"""
    dt_col = CSV_DT_COL.get(timeframe, 'datetime')

    # 检测编码
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            f.read(4096)
        enc = 'utf-8-sig'
    except (UnicodeDecodeError, UnicodeError):
        enc = 'gbk'

    klines = []
    with open(csv_path, 'r', encoding=enc) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt_str = row.get(dt_col, '').strip()
            if not dt_str or dt_str == 'nan':
                continue

            # 解析时间
            dt = None
            for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
                try:
                    dt = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                continue

            try:
                o = float(row.get('open', 0))
                h = float(row.get('high', 0))
                low = float(row.get('low', 0))
                c = float(row.get('close', 0))
                v = float(row.get('volume', 0))
            except (ValueError, TypeError):
                continue

            if o == 0 and c == 0:
                continue

            klines.append({
                "time": dt,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
            })

    return klines


def is_likely_adjusted(klines: list, code: str) -> bool:
    """启发式判断 CSV 数据是否已经是前复权。

    平安银行(000001) 实际股价 ~10-12 元，前复权历史价格会到 1000+。
    如果最新 close 远大于当前实际股价，大概率是前复权数据。

    这里用简单规则: 最新 close > 100 → 认为是前复权（对银行股而言）。
    更精确的做法是对比实时行情，但这里不需要那么复杂。
    """
    if not klines:
        return False
    latest = klines[-1]
    # 大部分A股实际价格 < 100 元，前复权历史价经常 > 100
    # 但这不是绝对的（茅台不复权也 > 1000），所以结合第一个 bar 的价格
    # 如果第一个 bar 价格和最新 bar 差距不大（都在合理范围），可能是不复权
    first_close = klines[0]["close"]
    last_close = latest["close"]

    # 如果价格看起来像真实股价（< 200），且波动合理，认为是不复权
    # 如果价格很高（> 200），认为是前复权
    if last_close > 200:
        return True

    # 对于价格在 100-200 之间的，看第一个 bar
    # 前复权的 2021 年数据 close 通常 > 500（银行股）
    if first_close > 300:
        return True

    return False


def query_db_klines(writer, market: str, code: str, timeframe: str,
                    start_dt: datetime, end_dt: datetime) -> dict:
    """从 DB 查询已有 K 线数据，返回 {datetime: {open, high, low, close, volume}}"""
    rows = writer.query(market, code, timeframe,
                        start_time=start_dt, end_time=end_dt, limit=50000)
    result = {}
    for r in rows:
        t = r["time"]
        if isinstance(t, str):
            for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
                try:
                    t = datetime.strptime(t, fmt)
                    break
                except ValueError:
                    continue
        if isinstance(t, datetime):
            result[t] = {"open": r["open"], "high": r["high"],
                         "low": r["low"], "close": r["close"],
                         "volume": r.get("volume", 0)}
    return result


def compare_overlap(csv_klines: list, db_data: dict, code: str) -> dict:
    """对比 CSV (已复权) 与 DB 已有数据的重叠段误差

    Returns:
        {
            "overlap_rows": int,
            "close_max_diff": float, "close_avg_diff": float,
            "open_max_diff": float,
            "volume_max_diff_pct": float,
            "sample": [(time, csv_close, db_close, diff), ...]  # 前5个差异最大的
        }
    """
    close_diffs = []
    open_diffs = []
    vol_diffs = []
    samples = []

    for bar in csv_klines:
        t = bar["time"]
        if t in db_data:
            db = db_data[t]
            c_diff = abs(bar["close"] - db["close"])
            o_diff = abs(bar["open"] - db["open"])
            close_diffs.append(c_diff)
            open_diffs.append(o_diff)

            # 成交量差异百分比
            if db["volume"] > 0:
                vol_pct = abs(bar["volume"] - db["volume"]) / db["volume"] * 100
                vol_diffs.append(vol_pct)

            if c_diff > 0.01:  # 收盘价差 > 1 分钱的记录
                samples.append((t, bar["close"], db["close"], c_diff))

    # 按差异排序，取前5
    samples.sort(key=lambda x: -x[3])
    samples = samples[:5]

    if not close_diffs:
        return {"overlap_rows": 0}

    return {
        "overlap_rows": len(close_diffs),
        "close_max_diff": max(close_diffs),
        "close_avg_diff": sum(close_diffs) / len(close_diffs),
        "open_max_diff": max(open_diffs),
        "volume_max_diff_pct": max(vol_diffs) if vol_diffs else 0,
        "sample": samples,
    }


def adjust_and_write(csv_dir: str, timeframe: str, market: str, dry_run: bool = False, workers: int = 4):
    """读取 CSV → 前复权 → 写入数据库"""
    dir_name = PERIOD_DIR.get(timeframe, timeframe)
    full_dir = os.path.join(csv_dir, dir_name)

    if not os.path.isdir(full_dir):
        print(f"  ⚠️  目录不存在: {full_dir}")
        return

    csv_files = sorted(glob.glob(os.path.join(full_dir, '*.csv')))
    if not csv_files:
        print(f"  ⚠️  无 CSV 文件: {full_dir}")
        return

    # 排除 _progress.json 等非数据文件
    csv_files = [f for f in csv_files if not os.path.basename(f).startswith('_')]

    print(f"\n{'='*60}")
    print(f"  📊 {dir_name} CSV → 前复权 → db_market")
    print(f"  目录: {full_dir}")
    print(f"  文件数: {len(csv_files)}")
    print(f"  时间框架: {timeframe}")
    if dry_run:
        print(f"  🔍 DRY RUN 模式 — 对比重叠段误差，不写入数据库")
    print(f"{'='*60}")

    # ── 初始化 DB 连接 (dry-run 也需要查询) ──
    mgr = get_market_db_manager()
    writer = get_market_kline_writer()
    has_db = mgr.market_db_exists(market)
    if not has_db:
        if dry_run:
            print(f"  ⚠️  数据库 {market} 不存在，跳过对比，仅检查 CSV")
        else:
            mgr.ensure_market_db(market)

    # ── dry-run 统计 ──
    if dry_run:
        total_files = len(csv_files)
        adjusted_count = 0
        already_adj_count = 0
        overlap_ok = 0
        overlap_mismatch = 0
        no_overlap = 0
        db_error = 0
        max_diff_overall = 0
        total_overlap_rows = 0

        for i, csv_path in enumerate(csv_files):
            fname = os.path.basename(csv_path)
            code = fname.split('_')[0].replace('.csv', '')

            try:
                klines = read_csv_klines(csv_path, timeframe)
                if not klines:
                    print(f"  ⚠️  {fname}: 空文件")
                    continue

                already_adjusted = is_likely_adjusted(klines, code)
                if already_adjusted:
                    adj_label = "已前复权"
                    adjusted_klines = klines
                    already_adj_count += 1
                else:
                    adj_label = "不复权→将转换"
                    adjusted_klines = apply_fwd_adjust(klines, code)
                    adjusted_count += 1

                csv_start = klines[0]["time"]
                csv_end = klines[-1]["time"]
                csv_range = f"{csv_start.strftime('%Y-%m-%d')} ~ {csv_end.strftime('%Y-%m-%d')}"
                close_range = f"{klines[0]['close']:.2f}~{klines[-1]['close']:.2f}"

                # 查询 DB 对比重叠段
                compare_info = ""
                if has_db:
                    try:
                        db_data = query_db_klines(writer, market, code, timeframe, csv_start, csv_end)
                        if db_data:
                            cmp = compare_overlap(adjusted_klines, db_data, code)
                            if cmp["overlap_rows"] > 0:
                                total_overlap_rows += cmp["overlap_rows"]
                                max_diff_overall = max(max_diff_overall, cmp["close_max_diff"])

                                if cmp["close_max_diff"] < 0.02:
                                    overlap_ok += 1
                                    diff_tag = "✅"
                                else:
                                    overlap_mismatch += 1
                                    diff_tag = "⚠️"

                                compare_info = (
                                    f"  重叠{cmp['overlap_rows']}行 "
                                    f"close差: max={cmp['close_max_diff']:.4f} avg={cmp['close_avg_diff']:.4f} "
                                    f"open差: max={cmp['open_max_diff']:.4f} "
                                    f"{diff_tag}"
                                )
                                if cmp["sample"]:
                                    compare_info += "\n    差异TOP: "
                                    for t, csv_c, db_c, d in cmp["sample"][:3]:
                                        compare_info += f"{t.strftime('%m-%d')} csv={csv_c:.2f} db={db_c:.2f} Δ{d:.4f}  "
                            else:
                                no_overlap += 1
                                compare_info = "  DB有数据但无重叠时间点"
                        else:
                            no_overlap += 1
                            compare_info = "  DB无此股票数据"
                    except Exception as e:
                        db_error += 1
                        compare_info = f"  DB查询失败: {e}"

                # 输出
                print(f"\n  [{i+1}/{total_files}] {fname}")
                print(f"    {len(klines)} 行 | {adj_label} | close: {close_range}")
                print(f"    日期: {csv_range}")
                if compare_info:
                    print(f"    {compare_info}")

            except Exception as e:
                print(f"\n  [{i+1}/{total_files}] {fname} ❌ 错误: {e}")

        # ── 汇总 ──
        print(f"\n\n{'='*60}")
        print(f"  📋 DRY RUN 汇总 ({dir_name})")
        print(f"{'='*60}")
        print(f"  总文件数:     {total_files}")
        print(f"  已前复权:     {already_adj_count}")
        print(f"  需转换:       {adjusted_count}")
        if has_db:
            print(f"  ────────────────────────────────")
            print(f"  重叠段对比:")
            print(f"    ✅ 误差 < 0.02:  {overlap_ok} 只")
            print(f"    ⚠️  误差 ≥ 0.02:  {overlap_mismatch} 只")
            print(f"    无重叠/无数据:   {no_overlap} 只")
            print(f"    DB查询失败:     {db_error} 只")
            print(f"    总重叠行数:     {total_overlap_rows:,}")
            print(f"    最大close差:    {max_diff_overall:.4f}")

            if overlap_mismatch > 0:
                print(f"\n  ⚠️  有 {overlap_mismatch} 只股票重叠段误差较大，")
                print(f"     可能原因: DB已有数据是不复权的，CSV复权后自然不同")
                print(f"     正式导入会用前复权数据覆盖")
            elif overlap_ok > 0:
                print(f"\n  ✅ 重叠段数据一致，复权转换正确")
        print(f"{'='*60}")
        return

    # ── 正式处理 ──
    mgr.ensure_market_db(market)

    success = 0
    fail = 0
    total_rows = 0
    t0 = time.time()

    for i, csv_path in enumerate(csv_files):
        fname = os.path.basename(csv_path)
        code = fname.split('_')[0].replace('.csv', '')

        try:
            klines = read_csv_klines(csv_path, timeframe)
            if not klines:
                print(f"  ⚠️  {fname}: 空文件，跳过")
                fail += 1
                continue

            # 判断是否需要复权
            already_adjusted = is_likely_adjusted(klines, code)

            if already_adjusted:
                adjusted = klines
            else:
                adjusted = apply_fwd_adjust(klines, code)

            # 构建写入记录
            records = []
            for bar in adjusted:
                dt = bar["time"]
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_TZ_SH)
                dt = dt.replace(tzinfo=None)
                records.append({
                    "symbol": code,
                    "timeframe": timeframe,
                    "time": dt,
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar.get("volume", 0),
                })

            if records:
                result = writer.bulk_write(market, records, batch_size=5000)
                inserted = result.get("inserted", 0)
                total_rows += inserted
                success += 1
            else:
                fail += 1

        except Exception as e:
            print(f"  ❌ {fname}: {e}")
            fail += 1

        # 进度
        if (i + 1) % 100 == 0 or (i + 1) == len(csv_files):
            elapsed = time.time() - t0
            print(f"\r  进度: {i+1}/{len(csv_files)}  "
                  f"✅ {success} ❌ {fail}  "
                  f"行数: {total_rows:,}  "
                  f"耗时: {elapsed:.0f}s", end='', flush=True)

    print()

    elapsed = time.time() - t0
    print(f"\n  ✅ 完成: {success} 只成功, {fail} 只失败")
    print(f"  📈 总写入行数: {total_rows:,}")
    print(f"  ⏱  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")


def main():
    ap = argparse.ArgumentParser(
        description='CSV → 前复权 → db_market 批量导入',
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument('--type', '-T',
        choices=['1D', '1m', '5m', '15m', '30m', '60m', 'all'],
        default='all',
        help='周期 (默认 all: 全部)')
    ap.add_argument('--csv-dir', '-d',
        default='optimizer_output/CNStock',
        help='CSV 根目录 (默认 optimizer_output/CNStock)')
    ap.add_argument('--market', '-m',
        default='CNStock',
        help='数据库市场标识 (默认 CNStock)')
    ap.add_argument('--workers', '-w', type=int, default=4,
        help='并行进程数 (默认 4)')
    ap.add_argument('--dry-run', action='store_true',
        help='只检查不写入')
    ap.add_argument('--db-url', type=str, default=None,
        help='数据库连接 URL (默认从 DATABASE_URL 环境变量读取)')

    args = ap.parse_args()

    if args.db_url:
        os.environ['DATABASE_URL'] = args.db_url

    if not os.getenv('DATABASE_URL'):
        print("❌ 未设置 DATABASE_URL，请通过以下方式之一提供:")
        print("   1. 设置环境变量: export DATABASE_URL=postgresql://user:pass@host:5432/dbname")
        print("   2. 创建 backend_api_python/.env 文件")
        print("   3. 使用 --db-url 参数")
        sys.exit(1)

    csv_dir = args.csv_dir
    if not os.path.isabs(csv_dir):
        csv_dir = os.path.join(_PROJECT_ROOT, csv_dir)

    print(f"""
╔═══════════════════════════════════════════════════╗
║  📦 CSV → 前复权 → db_market 批量导入              ║
╠═══════════════════════════════════════════════════╣
║  CSV目录: {csv_dir:<36}║
║  市场:    {args.market:<36}║
╚═══════════════════════════════════════════════════╝
""")

    if args.type == 'all':
        periods = ['1D', '1m', '5m', '15m', '30m', '60m']
    else:
        periods = [args.type]

    for tf in periods:
        adjust_and_write(csv_dir, tf, args.market, args.dry_run, args.workers)

    print(f"\n{'='*55}")
    print(f"  ✅ 全部完成!")
    print(f"{'='*55}")


if __name__ == '__main__':
    main()
