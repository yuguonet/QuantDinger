#!/usr/bin/env python3
"""
🚀 通达信协议直连 - A股全量数据下载

实测性能 (串行, 单连接):
  日线 5年:    ~3-4 分钟
  15分线 2年:  ~29 分钟
  15分线 5年:  ~72 分钟

多进程并行可进一步加速 (每进程独立连接)

用法:
  python tdx_download.py -T 1D                       # 日线
  python tdx_download.py -T 1m                       # 1分钟线
  python tdx_download.py -T 5m                       # 5分钟线
  python tdx_download.py -T 15m                      # 15分钟线
  python tdx_download.py -T 60m                      # 60分钟线
  python tdx_download.py -T all_min                  # 全部分钟线
  python tdx_download.py -T 1D -s 2021-01-01         # 指定起始日期
  python tdx_download.py -T 1D -s 2021-01-01 -e 2026-05-01
  python tdx_download.py -T 15m -w 40 --merge        # 40进程, 合并
"""

import os
import sys
import csv
import time
from datetime import datetime, timedelta
from multiprocessing import Pool
from pytdx.hq import TdxHq_API

# ═══════════════════════════════════════════════════════
# 通达信服务器
# ═══════════════════════════════════════════════════════

SERVERS = [
    ('218.75.126.9', 7709),
    ('115.238.56.198', 7709),
    ('124.160.88.183', 7709),
    ('60.12.136.250', 7709),
    ('218.108.98.244', 7709),
    ('218.108.47.69', 7709),
    ('180.153.39.51', 7709),
]


# ═══════════════════════════════════════════════════════
# 获取A股列表
# ═══════════════════════════════════════════════════════

def get_stock_list():
    """从通达信获取全部A股代码 (约4700只)"""
    api = TdxHq_API()
    api.connect(SERVERS[0][0], SERVERS[0][1])

    a_shares = []

    # 深圳: 00xxxx(主板) 30xxxx(创业板)
    for offset in range(0, 24000, 1000):
        batch = api.get_security_list(0, offset)
        if not batch:
            break
        for s in batch:
            c = s['code']
            if c.startswith(('00', '30')):
                a_shares.append((0, c, s['name']))

    # 上海: 60xxxx(主板) 68xxxx(科创板)
    for offset in range(0, 28000, 1000):
        batch = api.get_security_list(1, offset)
        if not batch:
            continue
        for s in batch:
            c = s['code']
            if c.startswith(('60', '68')):
                a_shares.append((1, c, s['name']))

    api.disconnect()

    # 去重
    seen = set()
    unique = []
    for m, c, n in a_shares:
        if c not in seen:
            seen.add(c)
            unique.append((m, c, n))

    return unique


# ═══════════════════════════════════════════════════════
# 工作进程
# ═══════════════════════════════════════════════════════

def _worker_daily(args):
    """工作进程: 下载日线"""
    stocks, worker_id, start_date, end_date, out_dir = args
    srv = SERVERS[worker_id % len(SERVERS)]

    api = TdxHq_API()
    api.connect(srv[0], srv[1])

    results = []

    for market, code, name in stocks:
        all_bars = []
        for offset in range(0, 800 * 10, 800):
            bars = api.get_security_bars(9, market, code, offset, 800)
            if not bars:
                break
            all_bars = bars + all_bars
            if bars[0]['datetime'][:10] <= start_date:
                break

        filtered = [b for b in all_bars if start_date <= b['datetime'][:10] <= end_date]

        if filtered:
            path = os.path.join(out_dir, f"{code}.csv")
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['date', 'open', 'close', 'high', 'low', 'volume', 'amount'])
                for b in filtered:
                    w.writerow([b['datetime'][:10], b['open'], b['close'],
                                b['high'], b['low'], int(b['vol']), b['amount']])
            results.append((code, len(filtered)))
        else:
            results.append((code, 0))

    api.disconnect()
    return results


def _worker_minute(args):
    """工作进程: 下载分钟线"""
    stocks, worker_id, freq, start_date, end_date, out_dir = args
    srv = SERVERS[worker_id % len(SERVERS)]

    freq_map = {0: '5min', 1: '15min', 4: '1min', 5: '30min', 6: '60min'}
    fname = freq_map.get(freq, f'{freq}min')

    api = TdxHq_API()
    api.connect(srv[0], srv[1])

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    span_days = (end_dt - start_dt).days
    max_req = int(span_days / 33) + 3  # 800条15分线≈33天
    results = []

    for market, code, name in stocks:
        all_bars = []
        for i in range(max_req):
            bars = api.get_security_bars(freq, market, code, i * 800, 800)
            if not bars:
                break
            all_bars = bars + all_bars
            try:
                first_dt = datetime.strptime(bars[0]['datetime'][:10], '%Y-%m-%d')
                if first_dt <= start_dt:
                    break
            except:
                pass

        filtered = []
        for b in all_bars:
            try:
                dt_str = b['datetime'][:10]
                if start_date <= dt_str <= end_date:
                    filtered.append(b)
            except:
                filtered.append(b)

        if filtered:
            path = os.path.join(out_dir, f"{code}_{fname}.csv")
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['datetime', 'open', 'close', 'high', 'low', 'volume', 'amount'])
                for b in filtered:
                    w.writerow([b['datetime'], b['open'], b['close'],
                                b['high'], b['low'], int(b['vol']), b['amount']])
            results.append((code, len(filtered)))
        else:
            results.append((code, 0))

    api.disconnect()
    return results


# ═══════════════════════════════════════════════════════
# 并行下载引擎
# ═══════════════════════════════════════════════════════

def parallel_download(stocks, worker_fn, out_dir, workers, **kwargs):
    """多进程并行下载"""
    os.makedirs(out_dir, exist_ok=True)

    # 分配任务
    bs = max(1, len(stocks) // workers)
    batches = []
    for i in range(workers):
        start = i * bs
        end = start + bs if i < workers - 1 else len(stocks)
        if start < len(stocks):
            batch = stocks[start:end]
            batches.append((batch, i, *kwargs.values(), out_dir))

    t0 = time.time()

    with Pool(workers) as pool:
        batch_results = pool.map(worker_fn, batches)

    elapsed = time.time() - t0

    success = fail = total_rows = 0
    for results in batch_results:
        for code, rows in results:
            if rows > 0:
                success += 1
                total_rows += rows
            else:
                fail += 1

    return success, fail, total_rows, elapsed


# ═══════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════

def run_daily(out_dir, start_date, end_date, workers):
    print(f"\n{'='*55}")
    print(f"  📊 通达信日线全量下载")
    print(f"  日期: {start_date} → {end_date}  进程: {workers}")
    print(f"{'='*55}")

    print("\n[1/3] 获取A股列表...")
    stocks = get_stock_list()
    print(f"  共 {len(stocks)} 只A股")

    print(f"\n[2/3] 开始下载...")
    out = os.path.join(out_dir, f'tdx_daily_{start_date}_{end_date}')
    success, fail, total_rows, elapsed = parallel_download(
        stocks, _worker_daily, out, workers,
        kwargs={'start_date': start_date, 'end_date': end_date},
    )

    print(f"\n  ✅ 成功: {success}  ❌ 失败: {fail}")
    print(f"  📈 总行数: {total_rows:,}")
    print(f"  ⏱  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    print(f"  📁 输出: {out}/")
    return out


def run_minute(out_dir, freq, start_date, end_date, workers):
    freq_name = {0: '5分钟', 1: '15分钟', 4: '1分钟', 5: '30分钟', 6: '60分钟'}
    fname = {0: '5min', 1: '15min', 4: '1min', 5: '30min', 6: '60min'}

    print(f"\n{'='*55}")
    print(f"  📊 通达信{freq_name.get(freq, '')}线全量下载")
    print(f"  日期: {start_date} → {end_date}  进程: {workers}")
    print(f"{'='*55}")

    print("\n[1/3] 获取A股列表...")
    stocks = get_stock_list()
    print(f"  共 {len(stocks)} 只A股")

    print(f"\n[2/3] 开始下载...")
    out = os.path.join(out_dir, f'tdx_{fname.get(freq)}_{start_date}_{end_date}')
    success, fail, total_rows, elapsed = parallel_download(
        stocks, _worker_minute, out, workers,
        kwargs={'freq': freq, 'start_date': start_date, 'end_date': end_date},
    )

    print(f"\n  ✅ 成功: {success}  ❌ 失败: {fail}")
    print(f"  📈 总行数: {total_rows:,}")
    print(f"  ⏱  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    print(f"  📁 输出: {out}/")
    return out


def merge_all(input_dir, output_file):
    """合并目录下所有CSV"""
    import glob
    files = sorted(glob.glob(os.path.join(input_dir, '*.csv')))
    if not files:
        print("无文件"); return

    print(f"合并 {len(files)} 个文件...")
    os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)

    total = 0
    with open(output_file, 'w', newline='', encoding='utf-8-sig') as out:
        header_written = False
        for f in files:
            with open(f, 'r', encoding='utf-8-sig') as inp:
                reader = csv.reader(inp)
                header = next(reader)
                if not header_written:
                    out.write('code,' + ','.join(header) + '\n')
                    header_written = True
                code = os.path.basename(f).split('_')[0]
                for row in reader:
                    out.write(code + ',' + ','.join(row) + '\n')
                    total += 1

    print(f"✅ {total:,} 行 → {output_file}")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='🚀 通达信直连下载A股全量数据',
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument('--type', '-T',
        choices=['1D', '1m', '5m', '15m', '30m', '60m', 'all_min'],
        default='1D',
        help='''数据类型 (默认1D):
  1D      - 日线
  1m      - 1分钟线
  5m      - 5分钟线
  15m     - 15分钟线
  30m     - 30分钟线
  60m     - 60分钟线
  all_min - 全部分钟线 (1m+5m+15m+30m+60m)''')
    ap.add_argument('--start', '-s', default='', help='起始日期, 如 2021-01-01 (优先于--years)')
    ap.add_argument('--end', '-e', default='', help='截止日期, 如 2026-05-01 (默认今天)')
    ap.add_argument('--years', '-y', type=int, default=5, help='年限 (默认5, 若指定--start则忽略)')
    ap.add_argument('--workers', '-w', type=int, default=5, help='并行进程数 (默认5)')
    ap.add_argument('--output', '-o', default='data_tdx', help='输出目录')
    ap.add_argument('--merge', action='store_true', help='合并为单CSV')

    args = ap.parse_args()

    print(f"""
╔═══════════════════════════════════════════════════╗
║  🚀 通达信协议直连 - A股全量数据下载               ║
║  无需API Key, 直连通达信行情服务器                  ║
╠═══════════════════════════════════════════════════╣
║  类型: {args.type:<10}  进程: {args.workers:<6}               ║
║  输出: {args.output:<38}║
╚═══════════════════════════════════════════════════╝
""")

    outputs = []

    # 计算实际起止日期
    end_date = args.end or datetime.now().strftime('%Y-%m-%d')
    if args.start:
        start_date = args.start
    else:
        start_date = (datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=args.years * 365)).strftime('%Y-%m-%d')

    print(f"  日期范围: {start_date} → {end_date}\n")

    type_map = {
        '1D':  ('daily', None),
        '1m':  ('min', 4),
        '5m':  ('min', 0),
        '15m': ('min', 1),
        '30m': ('min', 5),
        '60m': ('min', 6),
    }

    if args.type == 'all_min':
        # 全部分钟线
        for name, freq in [('1m', 4), ('5m', 0), ('15m', 1), ('30m', 5), ('60m', 6)]:
            out = run_minute(args.output, freq, start_date, end_date, args.workers)
            outputs.append((name, out))
    elif args.type == '1D':
        out = run_daily(args.output, start_date, end_date, args.workers)
        outputs.append(('1D', out))
    else:
        kind, freq = type_map[args.type]
        out = run_minute(args.output, freq, start_date, end_date, args.workers)
        outputs.append((args.type, out))

    if args.merge:
        for name, out_dir in outputs:
            merge_all(out_dir, os.path.join(args.output, f'all_{name}.csv'))

    print(f"\n{'='*55}")
    print(f"  ✅ 全部完成!")
    print(f"  📁 {os.path.abspath(args.output)}/")
    print(f"{'='*55}")


if __name__ == '__main__':
    main()
