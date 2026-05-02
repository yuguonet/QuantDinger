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

断点续传:
  每次下载会在输出目录生成 _progress.json，记录已下载的股票。
  中断后重新运行相同命令，自动跳过已完成的股票，只补下缺失的。
  加 --no-resume 可忽略断点，强制重新下载。
  加 --retry-failed 可将旧进度中 rows=0 的条目重置为待重试状态。
    (适用于旧版脚本生成的进度文件，下载失败的股票被错误标记为已完成)

修复模式 (--repair):
  与 check_continuity.py 联动，逐只股票修复已下载数据中的断裂和坏数据。
  数据直接写入 db_market，不做 CSV 写操作。
  流程: 检测断裂 → 逐只股票处理（停牌填充→akshare/通达信下载→验证→清理报表）

  python tdx_download.py -T 1D --repair              # 检测+修复日线断裂
  python tdx_download.py -T 15m --repair              # 检测+修复15分钟线断裂
  python tdx_download.py -T 1D --repair --dry-run     # 仅检测，不下载不写库
  python tdx_download.py -T 1D --repair --symbol 600519  # 单只股票修复
  python tdx_download.py -T 1D --repair --workers 10     # 10进程并行修复
  python tdx_download.py -T 1D --repair --no-fallback    # 禁用akshare（仅用通达信）

  数据源策略:
    日线 gap 优先用 akshare 前复权数据（时间戳匹配更可靠），
    分钟线 gap 仍走通达信（akshare 不支持分钟线前复权）。
    加 --no-fallback 可强制全部走通达信。
"""

import os
import sys
import csv
import json
import time
from datetime import datetime, timedelta
from multiprocessing import Pool
from pytdx.hq import TdxHq_API

# ═══════════════════════════════════════════════════════
# 断点续传 - 进度追踪器
# ═══════════════════════════════════════════════════════

class ProgressTracker:
    """记录已下载的股票，支持断点续传

    在输出目录维护 _progress.json，格式:
    {
      "meta": {
        "type": "daily",          # 数据类型
        "start_date": "2021-01-01",
        "end_date": "2026-05-01",
        "created": "2026-05-01T22:30:00",
        "last_updated": "2026-05-01T23:05:00"
      },
      "done": {
        "000001": {"name": "平安银行", "rows": 1200, "status": "success", "ts": "2026-05-01T22:31:00"},
        "000002": {"name": "万科A",    "rows": 0,    "status": "empty",   "ts": "2026-05-01T22:31:01"},
        "688999": {"name": "某科创",    "rows": 0,    "status": "failed",  "ts": "2026-05-01T22:31:02"},
        ...
      }
    }

    status 说明:
      "success" - 成功下载，rows > 0
      "empty"   - API 正常返回但该日期范围确实无数据 (rows=0 且无异常)
      "failed"  - 下载出错 (连接断开/超时/异常)，rows=0 但原因不明，下次应重试
    """

    # 旧版进度文件可能没有 status 字段，向后兼容时的默认值
    STATUS_SUCCESS = "success"
    STATUS_EMPTY = "empty"
    STATUS_FAILED = "failed"

    def __init__(self, out_dir, data_type, start_date, end_date):
        self.path = os.path.join(out_dir, '_progress.json')
        self.data_type = data_type
        self.start_date = start_date
        self.end_date = end_date
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 校验: 参数必须一致才复用
                meta = data.get('meta', {})
                if (meta.get('type') == self.data_type and
                    meta.get('start_date') == self.start_date and
                    meta.get('end_date') == self.end_date):
                    # 向后兼容: 旧版进度文件没有 status 字段，补上
                    for code, info in data.get('done', {}).items():
                        if 'status' not in info:
                            info['status'] = self.STATUS_SUCCESS if info.get('rows', 0) > 0 else self.STATUS_EMPTY
                    return data
                else:
                    print(f"  ⚠️  进度文件参数不匹配，重新开始")
            except (json.JSONDecodeError, KeyError):
                pass
        return {
            'meta': {
                'type': self.data_type,
                'start_date': self.start_date,
                'end_date': self.end_date,
                'created': datetime.now().isoformat(timespec='seconds'),
                'last_updated': datetime.now().isoformat(timespec='seconds'),
            },
            'done': {},
        }

    def is_done(self, code):
        """只跳过 status=success 或 status=empty 的股票，failed 的需要重试"""
        info = self.data['done'].get(code)
        if not info:
            return False
        status = info.get('status', self.STATUS_SUCCESS)
        return status in (self.STATUS_SUCCESS, self.STATUS_EMPTY)

    def get_done_codes(self):
        """返回所有无需重试的股票 code 集合 (success + empty)"""
        return {code for code, info in self.data['done'].items()
                if info.get('status', self.STATUS_SUCCESS) in (self.STATUS_SUCCESS, self.STATUS_EMPTY)}

    def mark(self, code, name, rows, status=None):
        """标记股票下载结果

        Args:
            code: 股票代码
            name: 股票名称
            rows: 数据行数
            status: 显式指定状态 ("success"/"empty"/"failed")。
                    若为 None 则根据 rows 自动判断: rows>0 → success, rows==0 → empty
        """
        if status is None:
            status = self.STATUS_SUCCESS if rows > 0 else self.STATUS_EMPTY
        self.data['done'][code] = {
            'name': name,
            'rows': rows,
            'status': status,
            'ts': datetime.now().isoformat(timespec='seconds'),
        }

    def save(self):
        self.data['meta']['last_updated'] = datetime.now().isoformat(timespec='seconds')
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)  # 原子写入

    def summary(self):
        done = self.data['done']
        total_rows = sum(v['rows'] for v in done.values())
        with_data = sum(1 for v in done.values() if v['rows'] > 0)
        failed = sum(1 for v in done.values() if v.get('status') == self.STATUS_FAILED)
        return len(done), with_data, total_rows, failed

    def reset_zero_rows(self):
        """将旧进度中 rows=0 的条目重置为 failed，使其可重试

        旧版脚本无法区分「下载失败」和「确实没数据」，两者都记 rows=0。
        本方法将所有 rows=0 的条目重置为 failed，让下载器重新验证。
        如果某只股票真的没数据，下载后会重新标记为 empty。

        返回被重置的数量。
        """
        reset_count = 0
        for code, info in self.data['done'].items():
            if info.get('rows', 0) == 0 and info.get('status') != self.STATUS_FAILED:
                info['status'] = self.STATUS_FAILED
                reset_count += 1
        if reset_count > 0:
            self.save()
        return reset_count

    def show_progress(self):
        """打印进度文件的详细统计信息"""
        done = self.data['done']
        if not done:
            print("  进度文件为空，没有已记录的股票。")
            return

        status_counts = {'success': 0, 'empty': 0, 'failed': 0}
        no_status = 0
        for info in done.values():
            s = info.get('status')
            if s in status_counts:
                status_counts[s] += 1
            else:
                no_status += 1

        total_rows = sum(v['rows'] for v in done.values())

        print(f"\n  📋 进度文件: {self.path}")
        print(f"  {'─'*45}")
        print(f"  总记录:     {len(done):>6} 只")
        print(f"  ✅ success:  {status_counts['success']:>6} 只  (有数据)")
        print(f"  ⬜ empty:    {status_counts['empty']:>6} 只  (rows=0, 标记为无数据)")
        print(f"  ❌ failed:   {status_counts['failed']:>6} 只  (下载失败, 待重试)")
        if no_status:
            print(f"  ❓ 无status: {no_status:>6} 只  (旧格式)")
        print(f"  总行数:     {total_rows:>6,}")
        print(f"  {'─'*45}")
        print(f"  参数: type={self.data['meta'].get('type')}  "
              f"start={self.data['meta'].get('start_date')}  "
              f"end={self.data['meta'].get('end_date')}")

        # 按失败数量提示
        retryable = status_counts['empty'] + status_counts['failed'] + no_status
        if retryable > 0:
            print(f"\n  💡 有 {retryable} 只股票可能需要重试 (--retry-failed 可重置 empty 为 failed)")


# ═══════════════════════════════════════════════════════
# 通达信服务器
# ═══════════════════════════════════════════════════════

CONNECT_TIMEOUT = 5  # 秒，单次连接/请求超时

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

def _fetch_security_list(api, market, offset):
    """获取证券列表，自动处理 pytdx offset>=8000 解析失败的 bug

    pytdx已知BUG: get_security_list 在 offset>=8000 时 parseResponse 返回 None
    (服务器有数据但解析器无法处理)。这里通过 monkey-patch parseResponse 修复。
    """
    # 首次调用时安装 monkey-patch
    if not hasattr(_fetch_security_list, '_patched'):
        from pytdx.parser.get_security_list import GetSecurityList
        import struct as _struct
        from pytdx.helper import get_volume

        _orig_parse = GetSecurityList.parseResponse

        def _robust_parse(self, body_buf):
            """修复版 parseResponse：先尝试原始解析，失败或结果异常则手动按29字节/记录解析"""
            result = None
            try:
                result = _orig_parse(self, body_buf)
            except Exception:
                pass
            # 原始解析成功且有数据 → 直接返回
            if result and len(result) > 0:
                return result
            # fallback: 原始解析失败或返回空/None，手动按29字节/记录解析
            try:
                if len(body_buf) < 2:
                    return None
                num, = _struct.unpack("<H", body_buf[:2])
                pos = 2
                stocks = []
                for _ in range(num):
                    if pos + 29 > len(body_buf):
                        break
                    one_bytes = body_buf[pos:pos + 29]
                    code_bytes, volunit, name_bytes, _, decimal_point, pre_close_raw, _ = \
                        _struct.unpack("<6sH8s4sBI4s", one_bytes)
                    code = code_bytes.decode("utf-8", errors="ignore").strip('\x00').strip()
                    if not code:
                        pos += 29
                        continue
                    name = name_bytes.decode("gbk", errors="ignore").rstrip("\x00")
                    pre_close = get_volume(pre_close_raw)
                    stocks.append({
                        'code': code, 'volunit': volunit,
                        'decimal_point': decimal_point, 'name': name,
                        'pre_close': pre_close,
                    })
                    pos += 29
                return stocks if stocks else None
            except Exception:
                pass
            # 最后兜底: 原始解析和29字节解析都失败，尝试跳过开头找6字节数字code
            try:
                num2, = _struct.unpack("<H", body_buf[:2])
                stocks2 = []
                scan = 2
                while scan < len(body_buf) - 29 and len(stocks2) < num2:
                    # 找6字节数字ASCII code
                    chunk = body_buf[scan:scan+6]
                    code_try = chunk.decode("ascii", errors="ignore").strip('\x00').strip()
                    if code_try.isdigit() and len(code_try) >= 4:
                        name_chunk = body_buf[scan+8:scan+24]
                        name_try = name_chunk.decode("gbk", errors="ignore").rstrip("\x00")
                        if name_try:
                            stocks2.append({
                                'code': code_try, 'volunit': 100,
                                'decimal_point': 2, 'name': name_try,
                                'pre_close': 0,
                            })
                            scan += 29
                            continue
                    scan += 1
                return stocks2 if stocks2 else None
            except Exception:
                return None

        GetSecurityList.parseResponse = _robust_parse
        _fetch_security_list._patched = True

    return api.get_security_list(market, offset)


def get_stock_list():
    """从通达信获取全部A股代码 (约5000+只)"""
    api = TdxHq_API()
    api.connect(SERVERS[0][0], SERVERS[0][1], time_out=CONNECT_TIMEOUT)

    a_shares = []

    # 深圳: 00xxxx(主板) 30xxxx(创业板)  +  北交所 83xxxx/87xxxx/43xxxx (market=0)
    for offset in range(0, 40000, 1000):
        batch = _fetch_security_list(api, 0, offset)
        if not batch:
            break
        for s in batch:
            c = s['code']
            if c.startswith(('00', '30', '83', '87', '43')):
                a_shares.append((0, c, s['name']))

    # 上海: 60xxxx(主板) 68xxxx(科创板)
    for offset in range(0, 40000, 1000):
        batch = _fetch_security_list(api, 1, offset)
        if not batch:
            break
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

    # 统计各板块数量
    cnt = {'00': 0, '30': 0, '60': 0, '68': 0, '83': 0, '87': 0, '43': 0, 'other': 0}
    for _, c, _ in unique:
        prefix = c[:2]
        if prefix in cnt:
            cnt[prefix] += 1
        else:
            cnt['other'] += 1
    print(f"    主板(00): {cnt['00']}  创业板(30): {cnt['30']}  "
          f"主板(60): {cnt['60']}  科创板(68): {cnt['68']}  "
          f"北交所(83/87/43): {cnt['83']+cnt['87']+cnt['43']}  其他: {cnt['other']}")

    return unique


# ═══════════════════════════════════════════════════════
# 工作进程
# ═══════════════════════════════════════════════════════


def _connect_api(worker_id):
    """连接通达信服务器，带超时，自动切换备用服务器"""
    for attempt in range(len(SERVERS)):
        idx = (worker_id + attempt) % len(SERVERS)
        srv = SERVERS[idx]
        try:
            api = TdxHq_API()
            api.connect(srv[0], srv[1], time_out=CONNECT_TIMEOUT)
            return api
        except Exception:
            continue
    raise ConnectionError(f"Worker-{worker_id}: 所有服务器连接失败")


def _worker_daily(args):
    """工作进程: 下载日线（单个股票失败不影响整体）

    返回: [(code, name, rows, status), ...]
      status: "success"=有数据  "empty"=API正常但确实无数据  "failed"=下载出错
    """
    stocks, worker_id, start_date, end_date, out_dir, done_codes = args

    api = _connect_api(worker_id)

    results = []

    for market, code, name in stocks:
        if code in done_codes:
            continue
        # 每只股票最多重试2次
        last_status = "failed"
        for retry in range(3):
            try:
                all_bars = []
                for offset in range(0, 800 * 10, 800):
                    bars = api.get_security_bars(9, market, code, offset, 800)
                    if not bars:
                        break
                    all_bars = bars + all_bars
                    # 安全终止: 只有当返回的数据量不足一批，或确实触及起始日期时才停止
                    if len(bars) < 800:
                        break
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
                    results.append((code, name, len(filtered), "success"))
                else:
                    # API 正常返回但日期范围内确实无数据
                    results.append((code, name, 0, "empty"))
                last_status = None  # 标记为成功处理
                break  # 成功，跳出重试
            except (ConnectionError, OSError, TimeoutError):
                # 连接断开/超时，重连后重试
                try:
                    api = _connect_api(worker_id)
                except ConnectionError:
                    pass
                if retry == 2:
                    results.append((code, name, 0, "failed"))
            except Exception:
                results.append((code, name, 0, "failed"))
                break

        # 兜底: 如果重试全部失败但没记录结果
        if last_status == "failed" and not any(r[0] == code for r in results):
            results.append((code, name, 0, "failed"))

    try:
        api.disconnect()
    except Exception:
        pass
    return results


def _worker_minute(args):
    """工作进程: 下载分钟线（单个股票失败不影响整体）

    返回: [(code, name, rows, status), ...]
      status: "success"=有数据  "empty"=API正常但确实无数据  "failed"=下载出错
    """
    stocks, worker_id, freq, start_date, end_date, out_dir, done_codes = args

    freq_map = {0: '5min', 1: '15min', 4: '1min', 5: '30min', 6: '60min'}
    fname = freq_map.get(freq, f'{freq}min')

    api = _connect_api(worker_id)

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    span_days = (end_dt - start_dt).days
    max_req = int(span_days / 33) + 3
    results = []

    for market, code, name in stocks:
        if code in done_codes:
            continue
        # 每只股票最多重试2次
        for retry in range(3):
            try:
                all_bars = []
                for i in range(max_req):
                    bars = api.get_security_bars(freq, market, code, i * 800, 800)
                    if not bars:
                        break
                    all_bars = bars + all_bars
                    # 安全终止: 返回量不足一批，或确实触及起始日期
                    if len(bars) < 800:
                        break
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
                    results.append((code, name, len(filtered), "success"))
                else:
                    results.append((code, name, 0, "empty"))
                break  # 成功，跳出重试
            except (ConnectionError, OSError, TimeoutError):
                try:
                    api = _connect_api(worker_id)
                except ConnectionError:
                    pass
                if retry == 2:
                    results.append((code, name, 0, "failed"))
            except Exception:
                results.append((code, name, 0, "failed"))
                break

    try:
        api.disconnect()
    except Exception:
        pass
    return results


# ═══════════════════════════════════════════════════════
# 并行下载引擎
# ═══════════════════════════════════════════════════════

def _worker_init():
    """子进程忽略 SIGINT，由主进程统一管理退出"""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def parallel_download(stocks, worker_fn, out_dir, workers, tracker=None, **extra):
    """多进程并行下载（支持断点续传，单批失败不影响整体）

    使用 apply_async + 主动轮询替代 imap_unordered，
    确保主进程的 KeyboardInterrupt 能被及时捕获。
    """
    os.makedirs(out_dir, exist_ok=True)

    # 断点续传: 过滤已完成的股票
    if tracker:
        done_codes = tracker.get_done_codes()
        remaining = [(m, c, n) for m, c, n in stocks if c not in done_codes]
        skipped = len(stocks) - len(remaining)
        if skipped > 0:
            print(f"  📋 断点续传: 已完成 {skipped} 只，剩余 {len(remaining)} 只")
        if not remaining:
            print("  ✅ 全部已完成，无需下载")
            done_count, with_data, total_rows, failed_count = tracker.summary()
            return done_count, done_count - with_data, total_rows, 0
        stocks = remaining
    else:
        done_codes = set()

    # 分配任务
    bs = max(1, len(stocks) // workers)
    batches = []
    for i in range(workers):
        start = i * bs
        end = start + bs if i < workers - 1 else len(stocks)
        if start < len(stocks):
            batch = stocks[start:end]
            batches.append((batch, i, *extra.values(), out_dir, done_codes))

    t0 = time.time()
    last_save_t = t0

    pool = Pool(workers, initializer=_worker_init)

    # 用 apply_async 替代 imap_unordered，主进程不会阻塞在 C 层调用
    async_results = []
    for batch_args in batches:
        ar = pool.apply_async(worker_fn, (batch_args,))
        async_results.append(ar)

    pool.close()  # 不再接受新任务

    batch_results = []  # [(index, result_list), ...]
    done_set = set()    # 已收集的 batch 索引
    try:
        while len(done_set) < len(async_results):
            # 主动轮询，每次 sleep 让出 GIL，保证 KeyboardInterrupt 能被处理
            time.sleep(0.5)

            # 收集已完成的结果
            for idx, ar in enumerate(async_results):
                if idx in done_set:
                    continue
                if ar.ready():
                    try:
                        result = ar.get(timeout=0)
                        batch_results.append((idx, result))
                        # 实时更新进度追踪
                        if tracker:
                            for item in result:
                                code, name, rows = item[0], item[1], item[2]
                                status = item[3] if len(item) > 3 else None
                                tracker.mark(code, name, rows, status=status)
                            now = time.time()
                            if now - last_save_t >= 5:
                                tracker.save()
                                last_save_t = now
                    except Exception:
                        batch_results.append((idx, []))
                    done_set.add(idx)

            done = len(done_set)
            pct = done * 100 // len(batches)
            elapsed_so_far = time.time() - t0
            processed = sum(len(r[1]) for r in batch_results)
            print(f"\r  进度: {done}/{len(batches)} 批 ({pct}%)  "
                  f"已处理: {processed} 只  耗时: {elapsed_so_far:.0f}s",
                  end='', flush=True)
        print()
    except KeyboardInterrupt:
        print("\n\n⚠️  收到中断信号，正在保存进度...")
        if tracker:
            for idx, result in batch_results:
                for item in result:
                    code, name, rows = item[0], item[1], item[2]
                    status = item[3] if len(item) > 3 else None
                    tracker.mark(code, name, rows, status=status)
            tracker.save()
            failed_cnt = sum(1 for v in tracker.data['done'].values() if v.get('status') == 'failed')
            print(f"  💾 进度已保存: {len(tracker.data['done'])} 只 (其中失败 {failed_cnt} 只将在下次重试)")
        # 强制终止所有子进程（包括卡住的）
        pool.terminate()
        pool.join()
        # 如果还有活的子进程，SIGKILL 强杀
        for p in pool._pool:
            if p.is_alive():
                try:
                    import signal as _sig
                    os.kill(p.pid, _sig.SIGKILL)
                except (OSError, AttributeError):
                    pass
        pool.join()
        print("❌ 已强制退出 (下次运行自动续传)")
        sys.exit(1)
    finally:
        pool.join()

    # 最终保存一次
    if tracker:
        tracker.save()

    elapsed = time.time() - t0

    success = fail = total_rows = 0
    for idx, results in batch_results:
        for item in results:
            code, name, rows = item[0], item[1], item[2]
            if rows > 0:
                success += 1
                total_rows += rows
            else:
                fail += 1

    # 合并断点续传的统计
    if tracker:
        done_count, with_data, tracker_rows, failed_count = tracker.summary()
        if failed_count > 0:
            print(f"  ⚠️  有 {failed_count} 只股票下载失败，下次运行将自动重试")
        return done_count, done_count - with_data, tracker_rows, elapsed

    return success, fail, total_rows, elapsed


# ═══════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════

PERIOD_DIR = {
    '1D': 'daily', '1m': '1m', '5m': '5m',
    '15m': '15m', '30m': '30m', '60m': '1h',
}

def run_daily(out_dir, start_date, end_date, workers, no_resume=False, retry_failed=False):
    print(f"\n{'='*55}")
    print(f"  📊 通达信日线全量下载")
    print(f"  日期: {start_date} → {end_date}  进程: {workers}")
    print(f"{'='*55}")

    out = os.path.join(out_dir, PERIOD_DIR['1D'])
    os.makedirs(out, exist_ok=True)

    # 初始化断点续传
    if no_resume:
        tracker = None
        print("\n  🔄 --no-resume: 忽略断点，强制重新下载")
    else:
        tracker = ProgressTracker(out, 'daily', start_date, end_date)
        done_count, _, _, failed_count = tracker.summary()

        # --retry-failed: 将旧进度中 rows=0 且无 status 的条目重置为 failed
        if retry_failed and done_count > 0:
            reset_count = tracker.reset_zero_rows()
            if reset_count > 0:
                print(f"\n  🔄 --retry-failed: 已将 {reset_count} 只 rows=0 的条目重置为待重试")
                # 重新统计
                done_count, _, _, failed_count = tracker.summary()

        if done_count > 0:
            print(f"\n  💾 检测到断点记录: 已完成 {done_count} 只 (其中 {failed_count} 只失败待重试)")

    print("\n[1/3] 获取A股列表...")
    stocks = get_stock_list()
    print(f"  共 {len(stocks)} 只A股")

    print(f"\n[2/3] 开始下载...")
    success, fail, total_rows, elapsed = parallel_download(
        stocks, _worker_daily, out, workers,
        tracker=tracker,
        start_date=start_date, end_date=end_date,
    )

    print(f"\n  ✅ 成功: {success}  ❌ 失败: {fail}")
    print(f"  📈 总行数: {total_rows:,}")
    print(f"  ⏱  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    print(f"  📁 输出: {out}/")
    if tracker:
        print(f"  📋 断点文件: {tracker.path}")
    return out


def run_minute(out_dir, freq, start_date, end_date, workers, no_resume=False, retry_failed=False):
    freq_name = {0: '5分钟', 1: '15分钟', 4: '1分钟', 5: '30分钟', 6: '60分钟'}
    fname = {0: '5m', 1: '15m', 4: '1m', 5: '30m', 6: '60m'}

    print(f"\n{'='*55}")
    print(f"  📊 通达信{freq_name.get(freq, '')}线全量下载")
    print(f"  日期: {start_date} → {end_date}  进程: {workers}")
    print(f"{'='*55}")

    out = os.path.join(out_dir, fname.get(freq, f'{freq}m'))
    os.makedirs(out, exist_ok=True)

    # 初始化断点续传
    if no_resume:
        tracker = None
        print("\n  🔄 --no-resume: 忽略断点，强制重新下载")
    else:
        type_label = fname.get(freq, f'{freq}m')
        tracker = ProgressTracker(out, type_label, start_date, end_date)
        done_count, _, _, failed_count = tracker.summary()

        # --retry-failed: 将旧进度中 rows=0 且无 status 的条目重置为 failed
        if retry_failed and done_count > 0:
            reset_count = tracker.reset_zero_rows()
            if reset_count > 0:
                print(f"\n  🔄 --retry-failed: 已将 {reset_count} 只 rows=0 的条目重置为待重试")
                done_count, _, _, failed_count = tracker.summary()

        if done_count > 0:
            print(f"\n  💾 检测到断点记录: 已完成 {done_count} 只 (其中 {failed_count} 只失败待重试)")

    print("\n[1/3] 获取A股列表...")
    stocks = get_stock_list()
    print(f"  共 {len(stocks)} 只A股")

    print(f"\n[2/3] 开始下载...")
    success, fail, total_rows, elapsed = parallel_download(
        stocks, _worker_minute, out, workers,
        tracker=tracker,
        freq=freq, start_date=start_date, end_date=end_date,
    )

    print(f"\n  ✅ 成功: {success}  ❌ 失败: {fail}")
    print(f"  📈 总行数: {total_rows:,}")
    print(f"  ⏱  耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    print(f"  📁 输出: {out}/")
    if tracker:
        print(f"  📋 断点文件: {tracker.path}")
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


def _merge_worker_init():
    """子进程忽略 SIGINT，由主进程统一管理退出"""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _merge_worker(args):
    """工作进程：读取一批 CSV 并写入 db_market

    Args:
        args: (file_list, timeframe, batch_id)

    Returns:
        (batch_id, total_files, total_rows, errors, error_msgs)
    """
    file_list, timeframe, batch_id = args

    import sys as _sys
    import os as _os
    _project_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    _backend_root = _os.path.join(_project_root, "backend_api_python")
    if _backend_root not in _sys.path:
        _sys.path.insert(0, _backend_root)

    from app.utils.db_market import get_market_kline_writer

    writer = get_market_kline_writer()
    from datetime import datetime

    total_rows = 0
    total_files = 0
    errors = 0
    error_msgs = []

    for fpath in file_list:
        fname = _os.path.basename(fpath)
        code = fname.split('_')[0].replace('.csv', '')

        try:
            with open(fpath, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                records = []
                for row in reader:
                    try:
                        dt_str = row.get('date') or row.get('datetime', '')
                        if not dt_str:
                            continue
                        dt = None
                        for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
                            try:
                                dt = datetime.strptime(dt_str.strip(), fmt)
                                break
                            except ValueError:
                                continue
                        if dt is None:
                            continue
                        ts = int(dt.timestamp())

                        records.append({
                            "symbol": code,
                            "timeframe": timeframe,
                            "time": ts,
                            "open": float(row.get('open', 0)),
                            "high": float(row.get('high', 0)),
                            "low": float(row.get('low', 0)),
                            "close": float(row.get('close', 0)),
                            "volume": float(row.get('volume', 0)),
                        })
                    except (ValueError, KeyError):
                        continue

                if records:
                    result = writer.bulk_write("CNStock", records, batch_size=5000)
                    total_rows += result.get("inserted", 0)
                    total_files += 1

        except Exception as e:
            errors += 1
            error_msgs.append(f"{fname}: {e}")

    return (batch_id, total_files, total_rows, errors, error_msgs)


def merge_to_db(input_dir, data_type, workers=4, db_url=None):
    """将下载的 CSV 数据写入 db_market（调用 bulk_write，多进程并行）

    自动扫描 input_dir 下所有 CSV，解析后批量写入 CNStock_db。

    Args:
        input_dir:  CSV 目录（如 optimizer_output/CNStock/daily）
        data_type:  数据类型，决定 timeframe 映射
                    "daily" / "1D" → 1D
                    "1m" → 1m, "5m" → 5m, "15m" → 15m,
                    "30m" → 30m, "60m" / "1h" → 60m
        workers:    并行写入进程数
        db_url:     数据库连接 URL（可选，默认从 DATABASE_URL 环境变量读取）
    """
    import glob

    # 确保 backend_api_python 在 path 中
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)

    # 加载 .env（优先 backend 目录，其次项目根目录）
    try:
        from dotenv import load_dotenv
        for env_path in [
            os.path.join(_backend_root, '.env'),
            os.path.join(_project_root, '.env'),
        ]:
            if os.path.isfile(env_path):
                load_dotenv(env_path, override=False)
                break
    except Exception:
        pass

    # 如果指定了 db_url，设为环境变量
    if db_url:
        os.environ['DATABASE_URL'] = db_url

    # 检查 DATABASE_URL
    if not os.getenv('DATABASE_URL'):
        print("❌ 未设置 DATABASE_URL，请通过以下方式之一提供:")
        print("   1. 设置环境变量: set DATABASE_URL=postgresql://user:pass@host:5432/dbname")
        print("   2. 创建 backend_api_python/.env 文件")
        print("   3. 使用 --db-url 参数")
        return

    from app.utils.db_market import get_market_db_manager

    # timeframe 映射
    tf_map = {
        "daily": "1D", "1D": "1D",
        "1m": "1m", "5m": "5m", "15m": "15m",
        "30m": "30m", "60m": "60m", "1h": "60m",
    }
    timeframe = tf_map.get(data_type, data_type)

    files = sorted(glob.glob(os.path.join(input_dir, '*.csv')))
    if not files:
        print(f"❌ 目录下无 CSV: {input_dir}")
        return

    print(f"\n{'='*55}")
    print(f"  📦 CSV → db_market 多进程写入")
    print(f"  目录: {input_dir}")
    print(f"  文件数: {len(files)}")
    print(f"  时间框架: {timeframe}")
    print(f"  进程数: {workers}")
    print(f"{'='*55}")

    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")

    # 分配任务给各工作进程
    bs = max(1, len(files) // workers)
    batches = []
    for i in range(workers):
        start = i * bs
        end = start + bs if i < workers - 1 else len(files)
        if start < len(files):
            batches.append((files[start:end], timeframe, i))

    import time
    from multiprocessing import Pool

    t0 = time.time()

    pool = Pool(workers, initializer=_merge_worker_init)

    try:
        async_results = []
        for batch_args in batches:
            ar = pool.apply_async(_merge_worker, (batch_args,))
            async_results.append(ar)

        pool.close()

        # 轮询等待完成
        done_set = set()
        while len(done_set) < len(async_results):
            time.sleep(1)
            for idx, ar in enumerate(async_results):
                if idx in done_set:
                    continue
                if ar.ready():
                    done_set.add(idx)
            print(f"\r  进度: {len(done_set)}/{len(async_results)} 批", end='', flush=True)
        print()

        pool.join()

    except KeyboardInterrupt:
        print("\n\n⚠️  收到中断信号，等待子进程退出...")
        pool.terminate()
        pool.join()
        print("❌ 已退出（已写入的数据不会丢失）")
        sys.exit(1)

    elapsed = time.time() - t0

    # 汇总结果
    total_rows = 0
    total_files = 0
    errors = 0
    for ar in async_results:
        try:
            batch_id, n_files, n_rows, n_err, msgs = ar.get(timeout=0)
            total_files += n_files
            total_rows += n_rows
            errors += n_err
            for msg in msgs:
                print(f"  ❌ {msg}")
        except Exception:
            pass

    print(f"\n  ✅ 写入完成:")
    print(f"     文件: {total_files} 个 (失败 {errors} 个)")
    print(f"     行数: {total_rows:,}")
    print(f"     市场: CNStock_db, 时间框架: {timeframe}")
    print(f"     耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")


# ═══════════════════════════════════════════════════════
# 修复模式 — 与 check_continuity.py 联动补数
# ═══════════════════════════════════════════════════════
#
# 设计思路:
#   1. 检测阶段: 从 db 的 data_issue_report 读取 gap 列表（fallback 本地 CSV 检测）
#   2. 停牌处理: 查百度停牌数据，停牌期间用最后收盘价填充，直接 INSERT db
#   3. 下载阶段: 非停牌 gap 从 akshare(日线) 或通达信(分钟线) 下载，直接写入 db
#   4. 验证阶段: 重新检测，确认已修复的 gap 从 data_issue_report 删除
#   5. 未修好的留在表中，下次再修
#
# 依赖:
#   - check_continuity.py (同目录)
#   - db_market.py (backend_api_python/app/utils/)
#   - pytdx
#
# 已知限制:
#   - 通达信 API 单次最多返回 800 条，修复单只股票的少量 gap 时效率足够
#   - 修复仅补充「缺失的时间戳」对应的数据，不重新拉取整只股票
#   - 15m 日内 gap 如果是数据源本身缺失（通达信服务器就没有），修复后仍会缺失
#     这种情况会在二次检测时再次报告，建议标记为 "source_missing"

import csv as _csv_repair
import json as _json_repair
import time as _time_repair
from datetime import datetime as _dt_repair, timedelta as _td_repair
from multiprocessing import Pool as _Pool_repair


# --- 修复所需的工具函数（复用 check_continuity 的时间逻辑）---

_TZ_SH_REPAIR = __import__('datetime').timezone(__import__('datetime').timedelta(hours=8))

_HOLIDAYS_REPAIR = None  # 延迟加载
_TRADING_DAY_SET_REPAIR = None


def _repair_load_holidays():
    """从 check_continuity 模块导入假期表，避免重复维护"""
    global _HOLIDAYS_REPAIR, _TRADING_DAY_SET_REPAIR
    if _HOLIDAYS_REPAIR is not None:
        return
    try:
        from check_continuity import HOLIDAYS, _build_trading_day_cache, _TRADING_DAY_SET
        _HOLIDAYS_REPAIR = HOLIDAYS
        _build_trading_day_cache()
        _TRADING_DAY_SET_REPAIR = _TRADING_DAY_SET
    except ImportError:
        # fallback: 从同目录导入
        _co_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _co_dir)
        from check_continuity import HOLIDAYS, _build_trading_day_cache, _TRADING_DAY_SET
        _HOLIDAYS_REPAIR = HOLIDAYS
        _build_trading_day_cache()
        _TRADING_DAY_SET_REPAIR = _TRADING_DAY_SET


def _repair_is_trading_day(d):
    _repair_load_holidays()
    return d in _TRADING_DAY_SET_REPAIR


def _repair_ts_to_date(ts):
    return _dt_repair.fromtimestamp(ts, tz=_TZ_SH_REPAIR).strftime("%Y-%m-%d")


# --- 从 db 读取已有 gap 报告 ---

def _repair_load_gaps_from_db(market, timeframe_filter=None, symbol_filter=None):
    """从 data_issue_report 表读取未修复的 gap 记录

    Returns:
        list of dict: [{symbol, timeframe, gap_type, start_date, end_date,
                        expected_ts}, ...]
    """
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)

    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()

    if not mgr.market_db_exists(market):
        raise ConnectionError(
            f"无法连接 {market}_db，请检查 DATABASE_URL 或 backend_api_python/.env 配置"
        )

    pool = mgr._get_pool(market)

    sql = """
        SELECT symbol, timeframe, issue_type, start_date, end_date, expected_ts
        FROM data_issue_report
        WHERE issue_type IN ('middle', 'tail', 'intraday')
    """
    params = []
    conditions = []

    if timeframe_filter:
        conditions.append("timeframe = %s")
        params.append(timeframe_filter)
    if symbol_filter:
        conditions.append("symbol = %s")
        params.append(symbol_filter)

    if conditions:
        sql += " AND " + " AND ".join(conditions)

    sql += " ORDER BY jsonb_array_length(expected_ts) DESC"

    gaps = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                if isinstance(rec.get("expected_ts"), str):
                    rec["expected_ts"] = _json_repair.loads(rec["expected_ts"])
                # 兼容：新表用 issue_type，下游代码用 gap_type
                rec["gap_type"] = rec.get("issue_type", rec.get("gap_type", ""))
                gaps.append(rec)

    mgr.close_all_pools()
    return gaps


# --- 从本地 CSV 检测 gap（不依赖数据库）---

def _repair_fetch_bars(api, market, code, freq, start_date, end_date):
    """从通达信拉取指定日期范围的 K 线数据

    Args:
        api: TdxHq_API 实例
        market: 0=深圳, 1=上海
        code: 股票代码
        freq: 9=日线, 1=15分钟, 4=1分钟, 0=5分钟, 5=30分钟, 6=60分钟
        start_date: "YYYY-MM-DD"
        end_date: "YYYY-MM-DD"

    Returns:
        list of dict: [{time, open, high, low, close, volume, amount}, ...]
    """
    all_bars = []
    max_offset = 800 * 10  # 最多拉 8000 条

    for offset in range(0, max_offset, 800):
        bars = api.get_security_bars(freq, market, code, offset, 800)
        if not bars:
            break
        all_bars = bars + all_bars
        if len(bars) < 800:
            break
        # 安全终止
        try:
            first_dt = bars[0]["datetime"][:10]
            if first_dt <= start_date:
                break
        except (KeyError, IndexError):
            pass

    # 过滤目标日期范围
    is_daily = (freq == 9)
    filtered = []
    for b in all_bars:
        try:
            dt_str = b["datetime"][:10]
            if start_date <= dt_str <= end_date:
                dt = _dt_repair.strptime(b["datetime"][:16], "%Y-%m-%d %H:%M")
                # 日线: 通达信返回 15:00 时间戳，但 check_continuity 用 00:00
                # 必须归一化到午夜，否则 expected_ts_set 过滤永远不匹配
                if is_daily:
                    dt = dt.replace(hour=0, minute=0, second=0)
                ts = int(dt.timestamp())
                filtered.append({
                    "time": ts,
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": int(b["vol"]),
                    "amount": b["amount"],
                })
        except (KeyError, ValueError):
            continue

    return filtered


def _repair_worker(args):
    """修复工作进程 — 始终写入 db_market

    Args:
        args: (task_list, worker_id, freq)
            task_list: [(code, market_int, gap_list), ...]
            freq: 通达信频率码

    Returns:
        (worker_id, repaired_count, failed_count, total_bars, error_msgs)
    """
    task_list, worker_id, freq = args

    # 建立连接
    api = _connect_api(worker_id)

    repaired = 0
    failed = 0
    total_bars = 0
    errors = []
    result_gaps_remaining = []  # 数据源缺失的 gap（无法修复）

    # 初始化 db writer
    import sys as _sys
    import os as _os
    _proj = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    _be = _os.path.join(_proj, "backend_api_python")
    if _be not in _sys.path:
        _sys.path.insert(0, _be)
    from app.utils.db_market import get_market_kline_writer
    writer = get_market_kline_writer()

    for code, market_int, gaps in task_list:
        try:
            # 合并所有 gap 的日期范围，一次性拉取
            all_start = min(g["start_date"] for g in gaps)
            all_end = max(g["end_date"] for g in gaps)

            # 构建期望时间戳集合（用于精确过滤）
            expected_ts_set = set()
            for g in gaps:
                expected_ts_set.update(g.get("expected_ts", []))

            # 拉取
            bars = _repair_fetch_bars(api, market_int, code, freq, all_start, all_end)

            if not bars:
                # 通达信服务器无此数据（数据源缺失，非连接问题）
                failed += 1
                errors.append(f"{code}: 数据源缺失 ({all_start}~{all_end})")
                # 标记这些 gap 为数据源缺失，后续不再重试
                for g in gaps:
                    result_gaps_remaining.append(g)
                continue

            # 如果有精确的 expected_ts，只保留那些时间戳对应的 bar
            if expected_ts_set:
                bars = [b for b in bars if b["time"] in expected_ts_set]

            if not bars:
                # 拉到了数据但时间戳全不匹配（数据源缺失或日期对不上）
                failed += 1
                errors.append(f"{code}: 数据源缺失（时间戳无匹配）")
                for g in gaps:
                    result_gaps_remaining.append(g)
                continue

            # 直接写入 db_market
            timeframe_map = {9: "1D", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
            tf = timeframe_map.get(freq, f"{freq}m")
            records = [{
                "symbol": code,
                "timeframe": tf,
                "time": b["time"],
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            } for b in bars]
            result = writer.bulk_write("CNStock", records, batch_size=5000)
            total_bars += result.get("inserted", 0)

            repaired += 1

        except (ConnectionError, OSError, TimeoutError):
            # 重连
            try:
                api = _connect_api(worker_id)
            except ConnectionError:
                pass
            failed += 1
            errors.append(f"{code}: 连接失败")
        except Exception as e:
            failed += 1
            errors.append(f"{code}: {type(e).__name__}: {e}")

    try:
        api.disconnect()
    except Exception:
        pass

    return (worker_id, repaired, failed, total_bars, errors, result_gaps_remaining)


# --- 验证修复结果：重新检测，找出确实被修复的 gap ---

# --- 换源补数：akshare 前复权数据（通达信修不了的 gap 用这个）---

def _repair_fetch_akshare(code, start_date, end_date, timeframe="1D"):
    """从 akshare 获取前复权 K 线数据

    Args:
        code: 股票代码，如 "600519"
        start_date: "YYYY-MM-DD"
        end_date: "YYYY-MM-DD"
        timeframe: "1D"（目前 akshare 只支持日线级别的前复权）

    Returns:
        list of dict: [{time, open, high, low, close, volume}, ...]
        失败返回空列表
    """
    try:
        import akshare as ak
    except ImportError:
        return []

    try:
        # akshare 的 symbol 格式：6位纯数字
        sym = code.strip()
        # stock_zh_a_hist 返回前复权数据（adjust="qfq"）
        df = ak.stock_zh_a_hist(
            symbol=sym,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",
        )
        if df is None or df.empty:
            return []

        records = []
        for _, row in df.iterrows():
            dt_str = str(row["日期"])[:10]
            dt = _dt_repair.strptime(dt_str, "%Y-%m-%d")
            ts = int(dt.timestamp())
            records.append({
                "time": ts,
                "open": float(row["开盘"]),
                "high": float(row["最高"]),
                "low": float(row["最低"]),
                "close": float(row["收盘"]),
                "volume": int(row["成交量"]),
            })
        return records
    except Exception:
        return []


def _repair_fallback_akshare(remaining_gaps, data_type, writer, market="CNStock", today=None):
    """对通达信修不了的 gap，用 akshare 前复权数据补数，直接写入 db

    只处理 1D gap（akshare 不支持分钟线前复权）。

    Args:
        remaining_gaps: 通达信修复后仍存在的 gap 列表
        data_type: "1D" / "15m" / ...
        writer: kline writer
        market: 市场名
        today: 今天日期

    Returns:
        (fixed_count, still_remaining_count, fixed_gaps, still_remaining)
    """
    if today is None:
        today = _dt_repair.now(_TZ_SH_REPAIR).strftime("%Y-%m-%d")

    # 只处理日线 gap
    gaps_to_fix = [g for g in remaining_gaps if g["timeframe"] == "1D"]
    other_gaps = [g for g in remaining_gaps if g["timeframe"] != "1D"]

    if not gaps_to_fix:
        return 0, len(remaining_gaps), [], remaining_gaps

    # 按 symbol 分组
    symbol_gaps = {}
    for g in gaps_to_fix:
        code = g["symbol"]
        if code not in symbol_gaps:
            symbol_gaps[code] = []
        symbol_gaps[code].append(g)

    fixed = []
    still_remaining = list(other_gaps)  # 非 1D 的 gap 直接保留

    for code, gaps in symbol_gaps.items():
        # 合并日期范围
        all_start = min(g["start_date"] for g in gaps)
        all_end = max(g["end_date"] for g in gaps)

        # 构建期望时间戳集合
        expected_ts_set = set()
        for g in gaps:
            expected_ts_set.update(g.get("expected_ts", []))

        # 从 akshare 拉取
        bars = _repair_fetch_akshare(code, all_start, all_end, "1D")
        if not bars:
            still_remaining.extend(gaps)
            continue

        # 精确过滤
        if expected_ts_set:
            bars = [b for b in bars if b["time"] in expected_ts_set]

        if not bars:
            still_remaining.extend(gaps)
            continue

        # 写入 db
        records = [{
            "symbol": code,
            "timeframe": "1D",
            "time": b["time"],
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": b["volume"],
        } for b in bars]
        result = writer.bulk_write(market, records, batch_size=5000)
        if result.get("inserted", 0) > 0:
            fixed.extend(gaps)
        else:
            still_remaining.extend(gaps)

    return len(fixed), len(still_remaining), fixed, still_remaining


def _repair_verify_fixed(writer, market, data_type, original_gaps, today=None):
    """修复后从 db 重新检测，与原始 gap 对比，返回确实被修复的 gap 列表

    Args:
        writer: kline writer
        market: 市场名
        data_type: "1D" / "15m" / ...
        original_gaps: 修复前检测到的 gap 列表
        today: 今天日期

    Returns:
        (fixed_gaps, remaining_gaps)
    """
    if today is None:
        today = _dt_repair.now(_TZ_SH_REPAIR).strftime("%Y-%m-%d")

    affected_symbols = set(g["symbol"] for g in original_gaps)

    # 导入检测函数
    _co_dir = os.path.dirname(os.path.abspath(__file__))
    if _co_dir not in sys.path:
        sys.path.insert(0, _co_dir)
    from check_continuity import check_1d_gaps, check_15m_gaps, _build_trading_day_cache
    _build_trading_day_cache()

    # 从 db 重新检测受影响的股票
    new_gap_keys = set()
    verified_symbols = set()

    for code in affected_symbols:
        try:
            if data_type == "1D":
                records = writer.query(market, code, "1D", limit=0)
                if records:
                    for ng in check_1d_gaps(code, records, today):
                        new_gap_keys.add((ng["symbol"], ng["timeframe"], ng["gap_type"],
                                          ng["start_date"], ng["end_date"]))
                    verified_symbols.add(code)
            elif data_type == "15m":
                records = writer.query(market, code, "15m", limit=0)
                if records:
                    for ng in check_15m_gaps(code, records, today):
                        new_gap_keys.add((ng["symbol"], ng["timeframe"], ng["gap_type"],
                                          ng["start_date"], ng["end_date"]))
                    verified_symbols.add(code)
        except Exception:
            pass

    # 对比
    fixed = []
    remaining = []
    for g in original_gaps:
        if g["symbol"] not in verified_symbols:
            remaining.append(g)
            continue
        key = (g["symbol"], g["timeframe"], g["gap_type"], g["start_date"], g["end_date"])
        if key in new_gap_keys:
            remaining.append(g)
        else:
            fixed.append(g)

    return fixed, remaining


# --- 清理已修复的报表记录 ---

def _repair_cleanup_reports(market, fixed_gaps):
    """修复成功后，从报表中删除已确认修复的记录

    - data_issue_report: 按唯一键精确删除每条已修复的记录
    """
    if not fixed_gaps:
        return 0

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)

    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool(market)

    deleted = 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            sql = """
                DELETE FROM data_issue_report
                WHERE symbol = %s AND timeframe = %s AND issue_type = %s
                  AND start_date = %s AND end_date = %s
            """
            for g in fixed_gaps:
                cur.execute(sql, (
                    g["symbol"], g["timeframe"], g.get("gap_type", g.get("issue_type", "")),
                    g["start_date"], g["end_date"],
                ))
                deleted += cur.rowcount

        conn.commit()

    mgr.close_all_pools()

    return deleted


# --- 主修复流程 ---

def _repair_check_suspend_baidu(code, gap_start, gap_end):
    """查询百度停牌数据，确认股票在 gap 期间是否停牌

    Args:
        code: 股票代码，如 "601211"
        gap_start: gap 开始日期 "YYYY-MM-DD"
        gap_end: gap 结束日期 "YYYY-MM-DD"

    Returns:
        dict or None:
          {"suspend_start": "2024-09-06", "resume_date": "2024-10-10", "reason": "拟筹划重大资产重组"}
          如果未停牌或查询失败返回 None
    """
    try:
        import akshare as ak
        df = ak.news_trade_notify_suspend_baidu(date=gap_start.replace("-", ""))
        if df is None or df.empty:
            return None
        # 在停牌列表中查找目标股票
        for _, row in df.iterrows():
            row_code = str(row.get("股票代码", "")).strip()
            if row_code == code:
                suspend_start = str(row.get("停牌时间", ""))[:10]
                resume_date = str(row.get("复牌时间", ""))[:10]
                reason = str(row.get("停牌事项说明", ""))
                # gap 必须完全落在停牌期间内才填充
                # 条件：停牌开始 <= gap开始 且 复牌日 >= gap结束
                if suspend_start and suspend_start <= gap_start:
                    # 复牌日为空表示尚未复牌，或复牌日 >= gap结束 → 整个 gap 在停牌期间
                    if not resume_date or resume_date >= gap_end:
                        return {
                            "suspend_start": suspend_start,
                            "resume_date": resume_date or "",
                            "reason": reason,
                        }
                # 否则：停牌不覆盖整个 gap，视为正常 gap（尝试下载）
                return None
        return None
    except Exception:
        return None


def _repair_fill_suspend(code, gaps, freq, writer, market="CNStock"):
    """用停牌前最后收盘价填充停牌期间的数据，直接写入数据库

    停牌期间无交易数据 → 直接 INSERT，量=0，价=停牌前最后收盘价。
    last_close 从 db 的 kline 表查询。

    Args:
        code: 股票代码
        gaps: 该股票的 gap 列表（已确认为停牌）
        freq: 通达信频率码
        writer: kline writer（必传）
        market: 市场名

    Returns:
        (filled_count, total_bars)
    """
    if not writer:
        return 0, 0

    timeframe_map = {9: "1D", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
    timeframe = timeframe_map.get(freq, f"{freq}m")

    # ── 从 db 查 last_close ──
    existing = {}  # ts -> close
    try:
        rows = writer.query(market, code, timeframe, limit=10)
        if rows:
            for r in rows:
                existing[r["time"]] = r.get("close", 0)
    except Exception:
        pass

    if not existing:
        return 0, 0

    sorted_ts = sorted(existing.keys())

    # ── 为每个 gap 生成填充数据，直接 INSERT ──
    filled_count = 0
    total_bars = 0
    all_records = []

    for g in gaps:
        expected_ts_list = g.get("expected_ts", [])
        if not expected_ts_list:
            continue

        # 找 gap 前最后一条数据的 close
        gap_first_ts = min(expected_ts_list)
        last_close = None
        for ts in reversed(sorted_ts):
            if ts < gap_first_ts:
                last_close = existing[ts]
                break
        if last_close is None:
            for ts in sorted_ts:
                if ts > max(expected_ts_list):
                    last_close = existing[ts]
                    break
        if last_close is None:
            continue

        for ts in expected_ts_list:
            all_records.append({
                "symbol": code,
                "timeframe": timeframe,
                "time": ts,
                "open": last_close,
                "high": last_close,
                "low": last_close,
                "close": last_close,
                "volume": 0,
            })
            total_bars += 1
        filled_count += 1

    if all_records:
        result = writer.bulk_write(market, all_records, batch_size=5000)
        inserted = result.get("inserted", 0)
        if inserted == 0:
            return 0, 0

    return filled_count, total_bars


def _repair_single_stock(code, gaps, freq, out_dir, market, no_fallback, dry_run, today):
    """逐只股票修复：先查停牌 → 停牌补数据 → 非停牌走 akshare/tdx → 验证 → 清理

    流程:
      1. 所有 gap 先查百度停牌数据
      2. 停牌 gap → 用停牌前最后收盘价填充（价不变，量=0）
      3. 非停牌 gap → akshare 优先(日线) / 通达信(分钟线)
      4. 仍未修复 → 留在 remaining

    Args:
        code: 股票代码
        gaps: 该股票的 gap 列表
        freq: 通达信频率码
        out_dir: 数据目录
        market: 市场名
        no_fallback: 禁用 akshare（强制全走通达信）
        dry_run: 仅检测不下载
        today: 今天日期

    Returns:
        dict: {code, repaired, failed, bars, fixed_gaps, remaining_gaps, errors, suspended}
    """
    result = {
        "code": code,
        "repaired": 0,
        "failed": 0,
        "bars": 0,
        "fixed_gaps": [],
        "remaining_gaps": [],
        "errors": [],
        "suspended": False,
    }

    if dry_run:
        result["remaining_gaps"] = gaps
        return result

    # 确定数据类型
    freq_to_dir = {9: "daily", 1: "15m", 4: "1m", 0: "5m", 5: "30m", 6: "60m"}
    subdir = freq_to_dir.get(freq, f"{freq}m")
    data_type = subdir

    # 初始化 db writer
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)
    from app.utils.db_market import get_market_kline_writer
    writer = get_market_kline_writer()

    # ══════════════════════════════════════════════
    # 第一步: 逐个 gap 查停牌
    # ══════════════════════════════════════════════
    suspend_gaps = []
    normal_gaps = []

    for g in gaps:
        suspend_info = _repair_check_suspend_baidu(code, g["start_date"], g["end_date"])
        if suspend_info:
            suspend_gaps.append(g)
            result["suspended"] = True
        else:
            normal_gaps.append(g)

    # ══════════════════════════════════════════════
    # 第二步: 停牌 gap → 用最后收盘价填充
    # ══════════════════════════════════════════════
    if suspend_gaps:
        filled, bars = _repair_fill_suspend(code, suspend_gaps, freq,
                                             writer=writer, market=market)
        if filled > 0:
            result["repaired"] += filled
            result["bars"] += bars
            result["fixed_gaps"].extend(suspend_gaps)
        else:
            # 填充失败（CSV 不存在等），归入 remaining
            normal_gaps.extend(suspend_gaps)

    # ══════════════════════════════════════════════
    # 第三步: 非停牌 gap → akshare / 通达信修复
    # ══════════════════════════════════════════════
    gaps_1d = [g for g in normal_gaps if g["timeframe"] == "1D"]
    gaps_min = [g for g in normal_gaps if g["timeframe"] != "1D"]
    remaining_after_repair = []

    # 日线 → akshare 优先
    if gaps_1d and not no_fallback:
        ak_fixed_n, ak_remaining_n, ak_fixed_g, ak_remaining_g = \
            _repair_fallback_akshare(gaps_1d, "1D", writer, market, today)
        if ak_fixed_n > 0:
            result["repaired"] += ak_fixed_n
            result["fixed_gaps"].extend(ak_fixed_g)
            result["bars"] += sum(len(g.get("expected_ts", [])) for g in ak_fixed_g)
        if ak_remaining_g:
            remaining_after_repair.extend(ak_remaining_g)
    elif gaps_1d and no_fallback:
        remaining_after_repair.extend(gaps_1d)

    # 分钟线 + akshare 没修好的日线 → 通达信
    tdx_gaps = gaps_min + remaining_after_repair
    remaining_after_repair = []

    if tdx_gaps:
        market_int = 1 if code.startswith(("60", "68")) else 0
        task_list = [(code, market_int, tdx_gaps)]
        worker_args = (task_list, 0, freq)
        _, repaired, failed, total_bars, errors, tdx_remaining = _repair_worker(worker_args)

        result["repaired"] += repaired
        result["failed"] += failed
        result["bars"] += total_bars
        result["errors"].extend(errors)

        # 验证：下载成功的 gap 从 db 重新检测确认
        # tdx_remaining: 数据源缺失，根本没下到的 gap
        attempted_gaps = [g for g in tdx_gaps if g not in tdx_remaining]
        if attempted_gaps:
            fixed_gaps, still_broken = _repair_verify_fixed(writer, market, data_type, attempted_gaps, today)
            result["fixed_gaps"].extend(fixed_gaps)
            remaining_after_repair = tdx_remaining + still_broken
        else:
            remaining_after_repair = tdx_remaining

    result["remaining_gaps"] = remaining_after_repair

    # ══════════════════════════════════════════════
    # 第四步: 清理已修复的报表记录
    # ══════════════════════════════════════════════
    if result["fixed_gaps"]:
        try:
            gap_del = _repair_cleanup_reports(market, result["fixed_gaps"])
            result["gap_deleted"] = gap_del
        except Exception as e:
            result["errors"].append(f"清理报表失败: {e}")

    return result


def run_repair(data_type, out_dir, workers=5, dry_run=False, symbol=None,
               market="CNStock", db_url=None, no_fallback=False):
    """主修复入口 — 逐只股票模式

    gap 检测优先从 db 的 data_issue_report 读取，fallback 到本地 CSV 检测。
    修复数据直接写入 db_market。

    Args:
        data_type: "1D", "15m", "1m", "5m", "30m", "60m"
        out_dir: 数据目录 (如 optimizer_output/CNStock)
        workers: 并行进程数
        dry_run: 仅检测不下载
        symbol: 只修复指定股票
        market: 市场名
        db_url: 数据库连接 URL
        no_fallback: 禁用 akshare 换源补数
    """
    if db_url:
        os.environ["DATABASE_URL"] = db_url
    else:
        # 加载 .env（与 check_continuity.py 一致）
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _backend_root = os.path.join(_project_root, "backend_api_python")
        try:
            from dotenv import load_dotenv
            for env_path in [
                os.path.join(_backend_root, '.env'),
                os.path.join(_project_root, '.env'),
            ]:
                if os.path.isfile(env_path):
                    load_dotenv(env_path, override=False)
                    break
        except Exception:
            pass

    freq_map = {"1D": 9, "15m": 1, "1m": 4, "5m": 0, "30m": 5, "60m": 6}
    freq = freq_map.get(data_type)
    if freq is None:
        print(f"❌ 不支持的时间框架: {data_type}")
        return

    today = _dt_repair.now(_TZ_SH_REPAIR).strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  🔧 修复模式（逐只股票） | {data_type} | {market}")
    print(f"  dry-run={dry_run}  symbol={symbol or '全部'}  换源={'关' if no_fallback else '开'}")
    print(f"{'='*55}")

    # ---- 阶段 1: 从 db 读取 gap 列表 ----
    gaps = []

    print("\n[1/3] 从 data_issue_report 读取 gap...")
    try:
        gaps = _repair_load_gaps_from_db(market, timeframe_filter=data_type, symbol_filter=symbol)
        print(f"  读取到 {len(gaps)} 条未修复的 gap")
    except Exception as e:
        print(f"  ❌ db 读取失败: {e}")
        print(f"  💡 请先运行 check_continuity.py 写入 gap 报告")
        return

    if not gaps:
        print("\n  ✅ 无断裂数据，无需修复")
        return

    # 统计
    by_type = {}
    total_missing = 0
    for g in gaps:
        by_type[g["gap_type"]] = by_type.get(g["gap_type"], 0) + 1
        total_missing += len(g.get("expected_ts", []))

    print(f"\n  📊 断裂统计:")
    print(f"     总 gap: {len(gaps)} 条 | 缺失 bar: {total_missing}")
    for gt, cnt in sorted(by_type.items()):
        print(f"     {gt}: {cnt} 条")

    # 最严重的 10 条
    gaps_sorted = sorted(gaps, key=lambda g: len(g.get("expected_ts", [])), reverse=True)
    print(f"\n  最严重的 10 条:")
    for g in gaps_sorted[:10]:
        print(f"    {g['symbol']:>8} | {g['timeframe']:>3} | {g['gap_type']:>8} | "
              f"{g['start_date']}~{g['end_date']} | 缺 {len(g.get('expected_ts', []))} 根")

    if dry_run:
        print(f"\n  🔍 dry-run 模式，不执行下载")
        return

    # ---- 阶段 2: 按股票分组 ----
    print(f"\n[2/3] 按股票分组...")
    symbol_gaps = {}
    for g in gaps:
        code = g["symbol"]
        if code not in symbol_gaps:
            symbol_gaps[code] = []
        symbol_gaps[code].append(g)

    symbols = sorted(symbol_gaps.keys())
    total_symbols = len(symbols)
    print(f"  需修复 {total_symbols} 只股票")

    # ---- 阶段 3: 逐只股票修复 ----
    print(f"\n[3/3] 开始逐只修复...\n")

    t0 = _time_repair.time()

    all_fixed_gaps = []
    all_remaining_gaps = []
    total_repaired = 0
    total_failed = 0
    total_bars = 0
    all_errors = []

    for idx, code in enumerate(symbols, 1):
        stock_gaps = symbol_gaps[code]
        gap_count = len(stock_gaps)
        missing_bars = sum(len(g.get("expected_ts", [])) for g in stock_gaps)

        print(f"  [{idx}/{total_symbols}] {code}  ({gap_count} gap, 缺 {missing_bars} 根)", end="", flush=True)

        result = _repair_single_stock(
            code=code,
            gaps=stock_gaps,
            freq=freq,
            out_dir=out_dir,
            market=market,
            no_fallback=no_fallback,
            dry_run=dry_run,
            today=today,
        )

        # 汇总
        total_repaired += result["repaired"]
        total_failed += result["failed"]
        total_bars += result["bars"]
        all_errors.extend(result["errors"])
        all_fixed_gaps.extend(result["fixed_gaps"])
        all_remaining_gaps.extend(result["remaining_gaps"])

        # 输出单只股票的结果
        fixed_n = len(result["fixed_gaps"])
        remain_n = len(result["remaining_gaps"])
        if result.get("suspended"):
            status = f"  🔄 停牌填充 {fixed_n} gap (停牌期间价不变量为0)"
        elif fixed_n > 0 and remain_n == 0:
            status = f"  ✅ 已修复 {fixed_n} gap"
        elif fixed_n > 0 and remain_n > 0:
            status = f"  ⚠️  修复 {fixed_n}, 剩余 {remain_n} (数据源缺失)"
        elif fixed_n == 0 and result["repaired"] == 0:
            status = f"  ❌ 下载失败"
        else:
            status = f"  ⚠️  未修复 (数据源缺失)"

        if result.get("gap_deleted"):
            status += f"  [报表清理: {result['gap_deleted']}条]"
        if result["errors"]:
            status += f"  [错误: {result['errors'][0]}]"

        print(status)

    elapsed = _time_repair.time() - t0

    # ---- 最终汇总 ----
    print(f"\n{'='*55}")
    print(f"  🔧 逐只修复完成!")
    print(f"     股票: {total_symbols} 只")
    print(f"     成功: {total_repaired}  失败: {total_failed}")
    print(f"     补充 bar: {total_bars}")
    print(f"     已修复 gap: {len(all_fixed_gaps)} 条")
    print(f"     未修复 gap: {len(all_remaining_gaps)} 条 (数据源缺失)")
    print(f"     耗时: {elapsed:.1f}s ({elapsed/60:.1f}分钟)")

    if all_remaining_gaps:
        print(f"\n  ⚠️  最终未修复的 gap（数据源缺失）:")
        for g in all_remaining_gaps[:10]:
            print(f"    {g['symbol']:>8} | {g['timeframe']:>3} | {g['gap_type']:>8} | "
                  f"{g['start_date']}~{g['end_date']}")
        if len(all_remaining_gaps) > 10:
            print(f"    ... 还有 {len(all_remaining_gaps) - 10} 条")

    if all_errors:
        print(f"\n  ⚠️  错误 (前 10 条):")
        for msg in all_errors[:10]:
            print(f"    {msg}")

    print(f"\n  💡 建议运行验证: python check_continuity.py --symbol {symbol or '全市场'}")
    print(f"{'='*55}")


# ═══════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════

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
    ap.add_argument('--output', '-o', default='optimizer_output/CNStock', help='输出目录 (默认 optimizer_output/CNStock)')
    ap.add_argument('--merge', action='store_true', help='合并为单CSV')
    ap.add_argument('--merge-db', action='store_true',
        help='将下载的 CSV 写入 db_market（CNStock_db），不再生成合并 CSV')
    ap.add_argument('--db-url', type=str, default=None,
        help='数据库连接 URL，如 postgresql://user:pass@localhost:5432/quantdinger（默认从 DATABASE_URL 环境变量读取）')
    ap.add_argument('--no-resume', action='store_true', help='忽略断点记录，强制重新下载')
    ap.add_argument('--retry-failed', action='store_true',
        help='将旧进度中 rows=0 的条目全部重置为 failed 状态，使其在本次运行中被重试。\n'
             '适用于: 旧版脚本生成的进度文件，下载失败的股票被错误标记为"已完成"。')
    ap.add_argument('--show-progress', action='store_true',
        help='仅显示当前进度文件的统计信息，不执行下载。')

    # ---- 修复模式参数 ----
    ap.add_argument('--repair', action='store_true',
        help='修复模式: 与 check_continuity.py 联动，自动修复已下载数据中的断裂和坏数据。')
    ap.add_argument('--dry-run', action='store_true',
        help='(修复模式) 仅检测断裂，不执行下载和写库。')
    ap.add_argument('--no-fallback', action='store_true',
        help='(修复模式) 禁用 akshare 换源补数，仅用通达信修复。')
    ap.add_argument('--symbol', type=str, default=None,
        help='(修复模式) 只修复指定股票代码，如 600519。')

    args = ap.parse_args()

    # ---- 修复模式分发 ----
    if args.repair:
        print(f"""
╔═══════════════════════════════════════════════════╗
║  🔧 修复模式 - 通达信补数                          ║
║  与 check_continuity.py 联动，自动修复数据断裂      ║
╠═══════════════════════════════════════════════════╣
║  类型: {args.type:<10}  进程: {args.workers:<6}               ║
║  输出: {args.output:<38}║
╚═══════════════════════════════════════════════════╝
""")
        run_repair(
            data_type=args.type,
            out_dir=args.output,
            workers=args.workers,
            dry_run=args.dry_run,
            symbol=args.symbol,
            db_url=args.db_url,
            no_fallback=args.no_fallback,
        )
        return

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

    # --show-progress: 仅显示进度，不下载
    if args.show_progress:
        types_to_show = []
        if args.type == 'all_min':
            types_to_show = [('1m', 4), ('5m', 0), ('15m', 1), ('30m', 5), ('60m', 6)]
        elif args.type == '1D':
            types_to_show = [('daily', None)]
        else:
            types_to_show = [(args.type, None)]

        for type_label, _ in types_to_show:
            dir_name = PERIOD_DIR.get(type_label, type_label)
            out = os.path.join(args.output, dir_name)
            tracker = ProgressTracker(out, type_label, start_date, end_date)
            tracker.show_progress()
        return

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
            out = run_minute(args.output, freq, start_date, end_date, args.workers, args.no_resume, args.retry_failed)
            outputs.append((name, out))
    elif args.type == '1D':
        out = run_daily(args.output, start_date, end_date, args.workers, args.no_resume, args.retry_failed)
        outputs.append(('1D', out))
    else:
        kind, freq = type_map[args.type]
        out = run_minute(args.output, freq, start_date, end_date, args.workers, args.no_resume, args.retry_failed)
        outputs.append((args.type, out))

    if args.merge:
        for name, out_dir in outputs:
            merge_all(out_dir, os.path.join(out_dir, 'all.csv'))

    if args.merge_db:
        for name, out_dir in outputs:
            merge_to_db(out_dir, name, args.workers, db_url=args.db_url)

    print(f"\n{'='*55}")
    print(f"  ✅ 全部完成!")
    print(f"  📁 {os.path.abspath(args.output)}/")
    print(f"{'='*55}")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，退出。")
        sys.exit(1)
