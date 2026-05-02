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
    ap.add_argument('--no-resume', action='store_true', help='忽略断点记录，强制重新下载')
    ap.add_argument('--retry-failed', action='store_true',
        help='将旧进度中 rows=0 的条目全部重置为 failed 状态，使其在本次运行中被重试。\n'
             '适用于: 旧版脚本生成的进度文件，下载失败的股票被错误标记为"已完成"。')
    ap.add_argument('--show-progress', action='store_true',
        help='仅显示当前进度文件的统计信息，不执行下载。')

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
