#!/usr/bin/env python3
"""
CSV → 前复权 → db_market 批量导入脚本

功能:
  1. 读取 optimizer_output/CNStock/ 下的 CSV 文件 (source=csv)
  2. 或直接从 baostock 下载前复权数据 (source=baostock)
  3. 写入 PostgreSQL (db_market)

支持:
  - 日线 (daily/*.csv) — 若已是前复权则直接写入，否则复权后写入
  - 分钟线 (1m/5m/15m/30m/1h/*.csv) — 统一做前复权后写入

用法:
  cd QuantDinger-main
  python csv_to_db_adjusted.py                          # 默认导入所有周期 (CSV)
  python csv_to_db_adjusted.py -T 1D                    # 只导日线
  python csv_to_db_adjusted.py --source baostock         # 从 baostock 下载前复权日线写库
  python csv_to_db_adjusted.py --source baostock --dry-run  # 只对比不写入
  python csv_to_db_adjusted.py --source baostock --codes 000001 000607  # 指定股票
  python csv_to_db_adjusted.py --source baostock --start-index 300     # 从第301只开始（断点续传）
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

# ── 模块级初始化（仅主进程执行，子进程通过 _init_backend() 懒加载）──
_backend_initialized = False
apply_fwd_adjust = None
build_fwd_factor = None
get_market_kline_writer = None
get_market_db_manager = None
_TZ_SH = timezone(timedelta(hours=8))

# 周期目录映射
PERIOD_DIR = {
    '1D': 'daily', '1m': '1m', '5m': '5m',
    '15m': '15m', '30m': '30m', '60m': '1h',
}

# CSV 列名映射
CSV_DT_COL = {'daily': 'date', '1m': 'datetime', '5m': 'datetime',
              '15m': 'datetime', '30m': 'datetime', '1h': 'datetime'}


def _init_backend():
    """懒加载后端模块（仅在需要时初始化，子进程不会触发）"""
    global _backend_initialized, apply_fwd_adjust, build_fwd_factor
    global get_market_kline_writer, get_market_db_manager
    if _backend_initialized:
        return

    # ── 绕过 app/__init__.py 的 Flask 导入 ──
    import types
    import importlib
    import importlib.util

    def _import_module_from_file(module_name, file_path):
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod

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

    _import_module_from_file(
        'app.utils.logger',
        os.path.join(_BACKEND_ROOT, 'app', 'utils', 'logger.py')
    )
    _import_module_from_file(
        'app.data_sources.normalizer',
        os.path.join(_BACKEND_ROOT, 'app', 'data_sources', 'normalizer.py')
    )
    _import_module_from_file(
        'app.utils.db_multi',
        os.path.join(_BACKEND_ROOT, 'app', 'utils', 'db_multi.py')
    )

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
    _backend_initialized = True


def read_csv_klines(csv_path: str, timeframe: str) -> list:
    """读取 CSV 文件，返回 kline 字典列表 [{"time", "open", "high", "low", "close", "volume"}, ...]"""
    dir_name = PERIOD_DIR.get(timeframe, timeframe)
    dt_col = CSV_DT_COL.get(dir_name, 'datetime')

    # 统一用 utf-8-sig 打开（自动剥离 BOM，无 BOM 的文件也兼容）
    klines = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
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
    """CSV 文件统一为前复权数据，直接返回 True。"""
    return True


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


def get_baostock_codes() -> list:
    """获取全部沪深 A 股代码列表（纯数字，如 '000001'）"""
    import baostock as bs

    def _try_query(day_str: str) -> tuple:
        """查询指定日期的股票列表，返回 (error_code, codes_list)"""
        rs = bs.query_all_stock(day=day_str)
        if rs.error_code != '0':
            return rs.error_code, []
        codes = []
        while rs.next():
            row = rs.get_row_data()
            code = row[0]  # 格式: sh.600000 或 sz.000001
            if code.startswith('sh.6') or code.startswith('sz.0') or code.startswith('sz.3'):
                codes.append(code.split('.')[1])
        return '0', codes

    # 从今天开始往前找最近一个有数据的交易日（最多回溯30天）
    for delta in range(0, 30):
        d = datetime.now() - timedelta(days=delta)
        day_str = d.strftime('%Y-%m-%d')
        err, codes = _try_query(day_str)
        if err == '0' and codes:
            return codes
        # 非交易日或无数据，继续往前

    print(f"  ❌ 回溯30天均未获取到股票列表，baostock 接口可能异常")
    return []


def _worker_init():
    """每个子进程启动时登录 baostock（只执行一次）"""
    import baostock as bs
    rs = bs.login()
    if rs.error_code != '0':
        print(f"  ⚠️  子进程 baostock 登录失败: {rs.error_msg}")


def _worker_download(code_and_dates):
    """子进程: 下载单只股票（进程级已登录，无需再 login）"""
    import baostock as bs
    code, start_date, end_date = code_and_dates

    # 补全前缀
    if code.startswith('6'):
        bs_code = f'sh.{code}'
    else:
        bs_code = f'sz.{code}'

    max_retries = 3
    for attempt in range(max_retries):
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",  # 前复权
            )
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
                try:
                    bs.logout()
                    bs.login()
                except Exception:
                    pass
                continue
            return {"code": code, "klines": [], "error": "连接异常"}

        if rs.error_code != '0':
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
                if attempt >= 1:
                    try:
                        bs.logout()
                        bs.login()
                    except Exception:
                        pass
                continue
            return {"code": code, "klines": [], "error": rs.error_msg}

        klines = []
        while rs.next():
            row = rs.get_row_data()
            try:
                dt = datetime.strptime(row[0], '%Y-%m-%d')
                o = float(row[1])
                h = float(row[2])
                low = float(row[3])
                c = float(row[4])
                v = float(row[5]) if row[5] else 0
            except (ValueError, TypeError, IndexError):
                continue
            if o == 0 and c == 0:
                continue
            klines.append({
                "time": dt, "open": o, "high": h, "low": low,
                "close": c, "volume": v,
            })

        return {"code": code, "klines": klines, "error": None}

    return {"code": code, "klines": [], "error": "重试耗尽"}


def baostock_import(market: str, start_date: str, end_date: str,
                    dry_run: bool = False, codes: list = None,
                    start_index: int = 0, workers: int = 4,
                    force: bool = False):
    """从 baostock 下载前复权日线 → 对比/写入 db_market（多进程）"""
    _init_backend()
    import baostock as bs

    # 主进程登录一次，获取股票列表
    login_result = bs.login()
    if login_result.error_code != '0':
        print(f"  ❌ baostock 登录失败: {login_result.error_msg}")
        return
    print(f"  ✅ baostock 登录成功")

    # 获取股票列表
    if codes:
        stock_list = codes
        print(f"  📊 指定股票: {stock_list}")
    else:
        print(f"  📊 获取全部 A 股代码...")
        stock_list = get_baostock_codes()
        print(f"     共 {len(stock_list)} 只")
        if not stock_list:
            print(f"  ❌ 获取股票列表为空，可能 baostock 接口异常")
            bs.logout()
            return

    # 断点续传：跳过前面已处理的
    if start_index > 0:
        if start_index >= len(stock_list):
            print(f"  ❌ start-index ({start_index}) >= 总数 ({len(stock_list)})，无数据可处理")
            bs.logout()
            return
        print(f"  ⏩ 跳过前 {start_index} 只，从第 {start_index+1} 只开始")
        stock_list = stock_list[start_index:]

    # 测试下载一只（在 logout 之前，用主进程连接验证）
    test_code = stock_list[0]
    print(f"  🔍 测试下载 {test_code}...")
    if test_code.startswith('6'):
        bs_test_code = f'sh.{test_code}'
    else:
        bs_test_code = f'sz.{test_code}'
    test_rs = bs.query_history_k_data_plus(
        bs_test_code, "date,open,high,low,close,volume",
        start_date=start_date, end_date=end_date,
        frequency="d", adjustflag="2",
    )
    test_klines = []
    if test_rs.error_code == '0':
        while test_rs.next():
            row = test_rs.get_row_data()
            try:
                dt = datetime.strptime(row[0], '%Y-%m-%d')
                o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                v = float(row[5]) if row[5] else 0
            except (ValueError, TypeError, IndexError):
                continue
            if o == 0 and c == 0:
                continue
            test_klines.append({"time": dt, "open": o, "high": h, "low": l, "close": c, "volume": v})
    if not test_klines:
        print(f"  ❌ 测试下载为空，baostock 数据接口可能有问题")
        bs.logout()
        return
    print(f"     ✅ {len(test_klines)} 行 | {test_klines[0]['time']} ~ {test_klines[-1]['time']}")

    bs.logout()  # 主进程释放连接，子进程各自登录

    # ── 多进程下载 ──
    t0 = time.time()
    total = len(stock_list)
    workers = min(workers, total, 8)  # 最多8进程

    print(f"\n  🚀 启动 {workers} 个进程并行下载 {total} 只股票...")

    # 构造任务: [(code, start_date, end_date), ...]
    tasks = [(code, start_date, end_date) for code in stock_list]

    results_all = {}  # {code: klines}
    fail_codes = []
    fail_reasons = {}  # {code: error_msg}

    with Pool(processes=workers, initializer=_worker_init) as pool:
        for result in pool.imap_unordered(_worker_download, tasks, chunksize=1):
            code = result["code"]
            if result["klines"]:
                results_all[code] = result["klines"]
            else:
                fail_codes.append(code)
                if result.get("error"):
                    fail_reasons[code] = result["error"]

            done = len(results_all) + len(fail_codes)
            elapsed = time.time() - t0
            # 每 10 只或最后一只打印进度
            if done % 10 == 0 or done == total:
                print(f"\r  进度: {done}/{total}  ✅ {len(results_all)} ❌ {len(fail_codes)}  "
                      f"耗时: {elapsed:.0f}s", end='', flush=True)

    elapsed = time.time() - t0
    print(f"\n  📥 下载完成: ✅ {len(results_all)} ❌ {len(fail_codes)}  耗时 {elapsed:.0f}s")

    # 打印失败原因汇总（限前20条）
    if fail_reasons:
        print(f"\n  ❌ 失败明细 (前{min(20, len(fail_reasons))}条):")
        for i, (c, err) in enumerate(list(fail_reasons.items())[:20]):
            print(f"    {c}: {err}")

    # ── 初始化 DB ──
    mgr = get_market_db_manager()
    writer = get_market_kline_writer()
    has_db = mgr.market_db_exists(market)
    if not has_db:
        if dry_run:
            print(f"  ⚠️  数据库 {market} 不存在，跳过对比")
        else:
            mgr.ensure_market_db(market)

    # ── dry-run: 主进程逐一对比 ──
    if dry_run:
        overlap_ok = 0
        overlap_mismatch = 0
        no_overlap = 0
        max_diff_overall = 0
        total_overlap_rows = 0

        for idx, (code, klines) in enumerate(sorted(results_all.items())):
            csv_start = klines[0]["time"]
            csv_end = klines[-1]["time"]
            close_range = f"{klines[0]['close']:.2f}~{klines[-1]['close']:.2f}"

            compare_info = ""
            if has_db:
                try:
                    db_data = query_db_klines(writer, market, code, '1D', csv_start, csv_end)
                    if db_data:
                        cmp = compare_overlap(klines, db_data, code)
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
                    compare_info = f"  DB查询失败: {e}"

            print(f"\n  [{idx+1}/{len(results_all)}] {code}.csv")
            print(f"    {len(klines)} 行 | 已前复权 | close: {close_range}")
            print(f"    日期: {klines[0]['time'].strftime('%Y-%m-%d')} ~ {klines[-1]['time'].strftime('%Y-%m-%d')}")
            if compare_info:
                print(f"    {compare_info}")

        print(f"\n\n{'='*60}")
        print(f"  📋 DRY RUN 汇总 (baostock 前复权日线)")
        print(f"{'='*60}")
        print(f"  总股票数:     {total}")
        print(f"  成功下载:     {len(results_all)}")
        print(f"  下载失败:     {len(fail_codes)}")
        if has_db:
            print(f"  ────────────────────────────────")
            print(f"  重叠段对比:")
            print(f"    ✅ 误差 < 0.02:  {overlap_ok} 只")
            print(f"    ⚠️  误差 ≥ 0.02:  {overlap_mismatch} 只")
            print(f"    无重叠/无数据:   {no_overlap} 只")
            print(f"    总重叠行数:     {total_overlap_rows:,}")
            print(f"    最大close差:    {max_diff_overall:.4f}")
        print(f"{'='*60}")
        return

    # ── 正式写入 ──
    if not has_db:
        mgr.ensure_market_db(market)

    # --force: 先删除库里同股票旧数据
    if force and results_all:
        print(f"\n  🗑️  --force 模式: 删除 {len(results_all)} 只股票的旧数据...")
        for code in results_all:
            try:
                writer.delete(market, code, "1D")
            except Exception:
                pass  # 不存在也不报错

    total_rows = 0
    total_records = 0
    success = 0
    fail = 0
    for code, klines in results_all.items():
        records = []
        for bar in klines:
            dt = bar["time"]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_TZ_SH)
            dt = dt.replace(tzinfo=None)
            records.append({
                "symbol": code,
                "timeframe": "1D",
                "time": dt,
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar.get("volume", 0),
            })
        if records:
            try:
                result = writer.bulk_write(market, records, batch_size=5000)
                inserted = result.get("inserted", 0)
                skipped = result.get("skipped", result.get("duplicates", len(records) - inserted))
                total_rows += inserted
                total_records += len(records)
                success += 1
                # 诊断日志：每只都打印
                if inserted == 0:
                    print(f"\n    {code}: ⚠️  传入{len(records)}条 写入0条 全部被跳过 | result={result}")
                elif success <= 5 or inserted == 0:
                    print(f"\n    {code}: 传入{len(records)}条 写入{inserted}条 跳过{skipped}条  "
                          f"时间范围{klines[0]['time'].strftime('%Y-%m-%d')}~{klines[-1]['time'].strftime('%Y-%m-%d')}")
            except Exception as e:
                import traceback
                print(f"\n    {code}: ❌ 写入失败: {e}")
                traceback.print_exc()
                fail += 1
        else:
            fail += 1

    fail += len(fail_codes)
    elapsed = time.time() - t0

    print(f"\n\n{'='*60}")
    print(f"  ✅ 处理完成!")
    print(f"  成功: {success}  失败: {fail}")
    print(f"  总记录数: {total_records:,}  新写入: {total_rows:,}  去重跳过: {total_records - total_rows:,}")
    print(f"  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    if total_rows == 0 and total_records > 0:
        print(f"\n  ⚠️  所有数据都已存在库里，没有新增。")
        print(f"     如果需要强制覆盖，检查 bulk_write 的去重逻辑。")
    print(f"{'='*60}")


def adjust_and_write(csv_dir: str, timeframe: str, market: str, dry_run: bool = False, workers: int = 4):
    """读取 CSV → 前复权 → 写入数据库"""
    _init_backend()
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
                try:
                    result = writer.bulk_write(market, records, batch_size=5000)
                    inserted = result.get("inserted", 0)
                    total_rows += inserted
                    success += 1
                except Exception as e:
                    import traceback
                    print(f"\n  ❌ {fname}: 写入失败: {e}")
                    traceback.print_exc()
                    fail += 1
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
    ap.add_argument('--source',
        choices=['csv', 'baostock'],
        default='csv',
        help='数据源 (默认 csv; baostock=直接下载前复权日线)')
    ap.add_argument('--start-date',
        default='2021-01-01',
        help='baostock 起始日期 (默认 2021-01-01)')
    ap.add_argument('--end-date',
        default=datetime.now().strftime('%Y-%m-%d'),
        help='baostock 结束日期 (默认今天)')
    ap.add_argument('--codes', nargs='+', default=None,
        help='baostock 指定股票代码 (如 000001 000607)')
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
    ap.add_argument('--start-index', type=int, default=0,
        help='从第几只股票开始（用于断点续传，默认 0）')
    ap.add_argument('--force', action='store_true',
        help='写入前先删除库中同股票旧数据（避免去重跳过）')
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
║  📦 前复权数据 → db_market 批量导入                ║
╠═══════════════════════════════════════════════════╣
║  数据源:  {args.source:<36}║
║  市场:    {args.market:<36}║
╚═══════════════════════════════════════════════════╝
""")

    if args.source == 'baostock':
        baostock_import(args.market, args.start_date, args.end_date,
                        args.dry_run, args.codes, args.start_index,
                        args.workers, args.force)
    else:
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
