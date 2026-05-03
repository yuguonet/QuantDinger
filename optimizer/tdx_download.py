#!/usr/bin/env python3
"""
🚀 A股全量K线数据下载 - 支持通达信/BaoStock双数据源

数据源:
  tdx       - 通达信协议直连，支持全部周期（日线+分钟线），无需API Key
  baostock  - 证券宝HTTP API，仅支持日线（免费、稳定），分钟线自动回退tdx
  both      - 双源模式：日线优先baostock，失败回退tdx；分钟线走tdx

实测性能 (tdx, 串行, 单连接):
  日线 5年:    ~3-4 分钟
  15分线 2年:  ~29 分钟
  15分线 5年:  ~72 分钟

用法:
  python tdx_download.py -T 1D                            # 日线 (默认tdx)
  python tdx_download.py -T 1D --source baostock          # 日线 (baostock)
  python tdx_download.py -T 1D --source both              # 日线 (双源, 优先baostock)
  python tdx_download.py -T 1m                            # 1分钟线 (仅tdx)
  python tdx_download.py -T 15m                           # 15分钟线 (仅tdx)
  python tdx_download.py -T all_min                       # 全部分钟线
  python tdx_download.py -T 1D -s 2021-01-01              # 指定起始日期
  python tdx_download.py -T 1D -s 2021-01-01 -e 2026-05-01
  python tdx_download.py -T 15m -w 40 --merge             # 40进程, 合并

断点续传:
  每次下载会在输出目录生成 _progress.json，记录已下载的股票。
  中断后重新运行相同命令，自动跳过已完成的股票，只补下缺失的。
  加 --no-resume 可忽略断点，强制重新下载。
  加 --retry-failed 可将旧进度中 rows=0 的条目重置为待重试状态。

依赖:
  - db_market.py / db_multi.py（backend_api_python/app/utils/）
  - pytdx (tdx数据源)
  - baostock (baostock数据源, pip install baostock)
"""

import os
import sys
import csv
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 延迟导入（按需加载，不强制依赖）
# ═══════════════════════════════════════════════════════

def _import_tdx():
    """延迟导入 pytdx"""
    try:
        from pytdx.hq import TdxHq_API
        return TdxHq_API
    except ImportError:
        raise ImportError("pytdx 未安装，请执行: pip install pytdx")


# ═══════════════════════════════════════════════════════
# 公共常量
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

_TZ_SH = timezone(timedelta(hours=8))

PERIOD_DIR = {
    '1D': 'daily', '1m': '1m', '5m': '5m',
    '15m': '15m', '30m': '30m', '60m': '1h',
}


# ═══════════════════════════════════════════════════════
# 15m 标准 bar 时间表（17 根）
# ═══════════════════════════════════════════════════════

_BAR_TIMES_15M = [
    (9, 30), (9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45), (15, 0),
]


def validate_and_calibrate_time(dt, timeframe: str):
    """
    校准写入数据库的时间戳（仅 15m）：
      - 11:30~13:00 → 11:30
      - 15:00~23:59 → 15:00
      - 其他时间保持原样
    不丢弃任何记录。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_SH)

    if timeframe != "15m":
        return dt

    total_min = dt.hour * 60 + dt.minute

    # 11:30~13:00 → 11:30
    if 690 <= total_min < 780:
        return dt.replace(hour=11, minute=30, second=0, microsecond=0)

    # 15:00~23:59 → 15:00
    if total_min >= 900:
        return dt.replace(hour=15, minute=0, second=0, microsecond=0)

    return dt


# ═══════════════════════════════════════════════════════
# 连接通达信服务器
# ═══════════════════════════════════════════════════════

def _connect_api(worker_id):
    """连接通达信服务器，带超时，自动切换备用服务器"""
    TdxHq_API = _import_tdx()
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


# ═══════════════════════════════════════════════════════
# BaoStock 数据源 (Python SDK, TCP协议)
# ═══════════════════════════════════════════════════════

def _import_baostock():
    """延迟导入 baostock"""
    try:
        import baostock as bs
        return bs
    except ImportError:
        raise ImportError("baostock 未安装，请执行: pip install baostock")


def get_stock_list_baostock():
    """从 BaoStock SDK 获取全部A股代码 (TCP协议, bs.query_stock_basic)"""
    bs = _import_baostock()

    lg = bs.login()
    if lg.error_code != '0':
        raise ConnectionError(f"BaoStock 登录失败: {lg.error_msg}")

    try:
        rs = bs.query_stock_basic()
        if rs.error_code != '0':
            raise RuntimeError(f"BaoStock query_stock_basic 失败: {rs.error_msg}")

        a_shares = []
        while rs.next():
            row = rs.get_row_data()
            # fields: code, code_name, ipoDate, outDate, type, status
            # type: 1-股票 2-指数 ... 只要股票
            code_full = str(row[0]).strip()   # sh.600519
            name = str(row[1]).strip()
            stock_type = str(row[4]).strip() if len(row) > 4 else ""
            status = str(row[5]).strip() if len(row) > 5 else ""

            if stock_type not in ('1', ''):  # 1=股票, 空值也保留兼容
                continue
            if status == '0':  # 0=退市
                continue

            if '.' not in code_full:
                continue
            market_prefix, digits = code_full.split('.', 1)
            if not digits:
                continue
            # 只要A股: 00/30/60/68/83/87/43
            if not digits.startswith(('00', '30', '60', '68', '83', '87', '43')):
                continue
            market = 1 if market_prefix == 'sh' else 0
            a_shares.append((market, digits, name))

        seen = set()
        unique = []
        for m, c, n in a_shares:
            if c not in seen:
                seen.add(c)
                unique.append((m, c, n))

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
    finally:
        bs.logout()


def _worker_daily_baostock(args):
    """工作进程: 通过 BaoStock SDK 下载日线 (TCP协议)

    每个 worker 进程独立 login/logout，避免跨进程共享连接。
    返回: [(code, name, rows, status), ...]
    """
    bs = _import_baostock()
    stocks, worker_id, start_date, end_date, out_dir, done_codes = args

    lg = bs.login()
    if lg.error_code != '0':
        logger.warning("[BaoStock Worker-%d] 登录失败: %s", worker_id, lg.error_msg)
        return [(code, name, 0, "failed") for _, code, name in stocks if code not in done_codes]

    results = []

    try:
        for market, code, name in stocks:
            if code in done_codes:
                continue

            bs_code = f"{'sh' if market == 1 else 'sz'}.{code}"

            for retry in range(3):
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start_date,
                        end_date=end_date,
                        frequency="d",
                        adjustflag="1",  # 前复权
                    )
                    if rs.error_code != '0':
                        logger.warning("[BaoStock] %s(%s) 查询失败: %s", code, name, rs.error_msg)
                        if retry == 2:
                            results.append((code, name, 0, "failed"))
                        continue

                    filtered = []
                    while rs.next():
                        row = rs.get_row_data()
                        # row: [date, open, high, low, close, volume, amount]
                        if len(row) < 7:
                            continue
                        dt_str = str(row[0]).strip()
                        if not dt_str or dt_str == '':
                            continue
                        try:
                            o = float(row[1]) if row[1] and row[1] != '' else 0
                            h = float(row[2]) if row[2] and row[2] != '' else 0
                            low = float(row[3]) if row[3] and row[3] != '' else 0
                            c = float(row[4]) if row[4] and row[4] != '' else 0
                            v = float(row[5]) if row[5] and row[5] != '' else 0
                            amount = float(row[6]) if row[6] and row[6] != '' else 0
                        except (ValueError, TypeError):
                            continue
                        if o == 0 and c == 0:
                            continue
                        filtered.append((dt_str, o, c, h, low, v, amount))

                    if filtered:
                        path = os.path.join(out_dir, f"{code}.csv")
                        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                            w = csv.writer(f)
                            w.writerow(['date', 'open', 'close', 'high', 'low', 'volume', 'amount'])
                            for row in filtered:
                                w.writerow(row)
                        results.append((code, name, len(filtered), "success"))
                    else:
                        results.append((code, name, 0, "empty"))
                    break
                except (ConnectionError, OSError, TimeoutError) as e:
                    logger.warning("[BaoStock] %s(%s) 连接异常(重试%d): %s", code, name, retry, e)
                    if retry == 2:
                        results.append((code, name, 0, "failed"))
                    else:
                        # 重连
                        try:
                            bs.logout()
                        except Exception:
                            pass
                        try:
                            bs.login()
                        except Exception:
                            pass
                except Exception as e:
                    results.append((code, name, 0, "failed"))
                    logger.warning("[BaoStock] %s(%s) 异常: %s", code, name, e)
                    break
    finally:
        try:
            bs.logout()
        except Exception:
            pass

    return results


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
                meta = data.get('meta', {})
                if (meta.get('type') == self.data_type and
                    meta.get('start_date') == self.start_date and
                    meta.get('end_date') == self.end_date):
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
        info = self.data['done'].get(code)
        if not info:
            return False
        status = info.get('status', self.STATUS_SUCCESS)
        return status in (self.STATUS_SUCCESS, self.STATUS_EMPTY)

    def get_done_codes(self):
        return {code for code, info in self.data['done'].items()
                if info.get('status', self.STATUS_SUCCESS) in (self.STATUS_SUCCESS, self.STATUS_EMPTY)}

    def mark(self, code, name, rows, status=None):
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
        os.replace(tmp, self.path)

    def summary(self):
        done = self.data['done']
        total_rows = sum(v['rows'] for v in done.values())
        with_data = sum(1 for v in done.values() if v['rows'] > 0)
        failed = sum(1 for v in done.values() if v.get('status') == self.STATUS_FAILED)
        return len(done), with_data, total_rows, failed

    def reset_zero_rows(self):
        reset_count = 0
        for code, info in self.data['done'].items():
            if info.get('rows', 0) == 0 and info.get('status') != self.STATUS_FAILED:
                info['status'] = self.STATUS_FAILED
                reset_count += 1
        if reset_count > 0:
            self.save()
        return reset_count

    def show_progress(self):
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

        retryable = status_counts['empty'] + status_counts['failed'] + no_status
        if retryable > 0:
            print(f"\n  💡 有 {retryable} 只股票可能需要重试 (--retry-failed 可重置 empty 为 failed)")


# ═══════════════════════════════════════════════════════
# 获取A股列表
# ═══════════════════════════════════════════════════════

def _fetch_security_list(api, market, offset):
    """获取证券列表，自动处理 pytdx offset>=8000 解析失败的 bug"""
    if not hasattr(_fetch_security_list, '_patched'):
        from pytdx.parser.get_security_list import GetSecurityList
        import struct as _struct
        from pytdx.helper import get_volume

        _orig_parse = GetSecurityList.parseResponse

        def _robust_parse(self, body_buf):
            result = None
            try:
                result = _orig_parse(self, body_buf)
            except Exception:
                pass
            if result and len(result) > 0:
                return result
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
            try:
                num2, = _struct.unpack("<H", body_buf[:2])
                stocks2 = []
                scan = 2
                while scan < len(body_buf) - 29 and len(stocks2) < num2:
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
    TdxHq_API = _import_tdx()
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

    seen = set()
    unique = []
    for m, c, n in a_shares:
        if c not in seen:
            seen.add(c)
            unique.append((m, c, n))

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

def _worker_daily(args):
    """工作进程: 下载日线（单个股票失败不影响整体）

    返回: [(code, name, rows, status), ...]
    """
    stocks, worker_id, start_date, end_date, out_dir, done_codes = args

    api = _connect_api(worker_id)

    results = []

    for market, code, name in stocks:
        if code in done_codes:
            continue
        last_status = "failed"
        for retry in range(3):
            try:
                all_bars = []
                for offset in range(0, 800 * 10, 800):
                    bars = api.get_security_bars(9, market, code, offset, 800)
                    if not bars:
                        break
                    all_bars = bars + all_bars
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
                    results.append((code, name, 0, "empty"))
                last_status = None
                break
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
        for retry in range(3):
            try:
                all_bars = []
                for i in range(max_req):
                    bars = api.get_security_bars(freq, market, code, i * 800, 800)
                    if not bars:
                        break
                    all_bars = bars + all_bars
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
                break
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
    """多进程并行下载（支持断点续传，单批失败不影响整体）"""
    os.makedirs(out_dir, exist_ok=True)

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

    async_results = []
    for batch_args in batches:
        ar = pool.apply_async(worker_fn, (batch_args,))
        async_results.append(ar)

    pool.close()

    batch_results = []
    done_set = set()
    try:
        while len(done_set) < len(async_results):
            time.sleep(0.5)

            for idx, ar in enumerate(async_results):
                if idx in done_set:
                    continue
                if ar.ready():
                    try:
                        result = ar.get(timeout=0)
                        batch_results.append((idx, result))
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
        pool.terminate()
        pool.join()
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

    if tracker:
        done_count, with_data, tracker_rows, failed_count = tracker.summary()
        if failed_count > 0:
            print(f"  ⚠️  有 {failed_count} 只股票下载失败，下次运行将自动重试")
        return done_count, done_count - with_data, tracker_rows, elapsed

    return success, fail, total_rows, elapsed


# ═══════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════

def run_daily(out_dir, start_date, end_date, workers, no_resume=False, retry_failed=False, source='tdx'):
    source_label = {'tdx': '通达信', 'baostock': 'BaoStock', 'both': '双源(优先BaoStock)'}
    print(f"\n{'='*55}")
    print(f"  📊 日线全量下载 [{source_label.get(source, source)}]")
    print(f"  日期: {start_date} → {end_date}  进程: {workers}")
    print(f"{'='*55}")

    out = os.path.join(out_dir, PERIOD_DIR['1D'])
    os.makedirs(out, exist_ok=True)

    if no_resume:
        tracker = None
        print("\n  🔄 --no-resume: 忽略断点，强制重新下载")
    else:
        tracker = ProgressTracker(out, 'daily', start_date, end_date)
        done_count, _, _, failed_count = tracker.summary()

        if retry_failed and done_count > 0:
            reset_count = tracker.reset_zero_rows()
            if reset_count > 0:
                print(f"\n  🔄 --retry-failed: 已将 {reset_count} 只 rows=0 的条目重置为待重试")
                done_count, _, _, failed_count = tracker.summary()

        if done_count > 0:
            print(f"\n  💾 检测到断点记录: 已完成 {done_count} 只 (其中 {failed_count} 只失败待重试)")

    # ── 获取股票列表 ──
    if source in ('baostock', 'both'):
        print("\n[1/3] 获取A股列表 (BaoStock)...")
        try:
            stocks = get_stock_list_baostock()
        except Exception as e:
            if source == 'both':
                print(f"  ⚠️  BaoStock 获取列表失败({e})，回退通达信...")
                stocks = None
            else:
                raise
        if source == 'baostock' or (source == 'both' and stocks):
            pass
        elif source == 'both' and not stocks:
            print("\n[1/3] 获取A股列表 (通达信)...")
            stocks = get_stock_list()
    else:
        print("\n[1/3] 获取A股列表 (通达信)...")
        stocks = get_stock_list()
    print(f"  共 {len(stocks)} 只A股")

    # ── 下载日线 ──
    if source == 'baostock':
        print(f"\n[2/3] 开始下载 (BaoStock)...")
        success, fail, total_rows, elapsed = parallel_download(
            stocks, _worker_daily_baostock, out, workers,
            tracker=tracker,
            start_date=start_date, end_date=end_date,
        )
    elif source == 'both':
        # 双源模式: 先用 baostock 下载，失败的再用 tdx 补下
        print(f"\n[2/3] 开始下载 (BaoStock 优先)...")
        success, fail, total_rows, elapsed = parallel_download(
            stocks, _worker_daily_baostock, out, workers,
            tracker=tracker,
            start_date=start_date, end_date=end_date,
        )
        if fail > 0:
            print(f"\n  🔄 BaoStock 有 {fail} 只失败/无数据，尝试通达信补下...")
            tdx_success, tdx_fail, tdx_rows, tdx_elapsed = parallel_download(
                stocks, _worker_daily, out, workers,
                tracker=tracker,
                start_date=start_date, end_date=end_date,
            )
            success += tdx_success
            fail = tdx_fail
            total_rows += tdx_rows
            elapsed += tdx_elapsed
    else:
        print(f"\n[2/3] 开始下载 (通达信)...")
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


def run_minute(out_dir, freq, start_date, end_date, workers, no_resume=False, retry_failed=False, source='tdx'):
    freq_name = {0: '5分钟', 1: '15分钟', 4: '1分钟', 5: '30分钟', 6: '60分钟'}
    fname = {0: '5m', 1: '15m', 4: '1m', 5: '30m', 6: '60m'}

    if source == 'baostock':
        print(f"\n  ⚠️  BaoStock 不支持分钟线，自动切换为通达信下载")

    print(f"\n{'='*55}")
    print(f"  📊 {freq_name.get(freq, '')}线全量下载 [通达信]")
    print(f"  日期: {start_date} → {end_date}  进程: {workers}")
    print(f"{'='*55}")

    out = os.path.join(out_dir, fname.get(freq, f'{freq}m'))
    os.makedirs(out, exist_ok=True)

    if no_resume:
        tracker = None
        print("\n  🔄 --no-resume: 忽略断点，强制重新下载")
    else:
        type_label = fname.get(freq, f'{freq}m')
        tracker = ProgressTracker(out, type_label, start_date, end_date)
        done_count, _, _, failed_count = tracker.summary()

        if retry_failed and done_count > 0:
            reset_count = tracker.reset_zero_rows()
            if reset_count > 0:
                print(f"\n  🔄 --retry-failed: 已将 {reset_count} 只 rows=0 的条目重置为待重试")
                done_count, _, _, failed_count = tracker.summary()

        if done_count > 0:
            print(f"\n  💾 检测到断点记录: 已完成 {done_count} 只 (其中 {failed_count} 只失败待重试)")

    if source in ('baostock', 'both'):
        print("\n[1/3] 获取A股列表 (BaoStock)...")
        try:
            stocks = get_stock_list_baostock()
        except Exception as e:
            if source == 'both':
                print(f"  ⚠️  BaoStock 获取列表失败({e})，回退通达信...")
                stocks = get_stock_list()
            else:
                print(f"  ⚠️  BaoStock 获取列表失败({e})，回退通达信...")
                stocks = get_stock_list()
    else:
        print("\n[1/3] 获取A股列表 (通达信)...")
        stocks = get_stock_list()
    print(f"  共 {len(stocks)} 只A股")

    # 分钟线始终走通达信
    print(f"\n[2/3] 开始下载 (通达信)...")
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
    import pandas as pd
    files = sorted(glob.glob(os.path.join(input_dir, '*.csv')))
    if not files:
        print("无文件"); return

    print(f"合并 {len(files)} 个文件...")
    os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)

    dfs = []
    for f in files:
        code = os.path.basename(f).split('_')[0].replace('.csv', '')
        try:
            with open(f, 'r', encoding='utf-8-sig') as inp:
                inp.read(4096)
            enc = 'utf-8-sig'
        except (UnicodeDecodeError, UnicodeError):
            enc = 'gbk'
        df = pd.read_csv(f, encoding=enc)
        df.insert(0, 'code', code)
        dfs.append(df)

    if not dfs:
        print("无有效数据"); return

    merged = pd.concat(dfs, ignore_index=True)
    merged.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"✅ {len(merged):,} 行 → {output_file}")


def _merge_worker_init():
    """子进程忽略 SIGINT，由主进程统一管理退出"""
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _merge_worker(args):
    """工作进程：读取一批 CSV 并写入 db_market"""
    file_list, timeframe, batch_id = args

    import sys as _sys
    import os as _os
    _project_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    _backend_root = _os.path.join(_project_root, "backend_api_python")
    if _backend_root not in _sys.path:
        _sys.path.insert(0, _backend_root)

    import pandas as pd
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
            try:
                with open(fpath, 'r', encoding='utf-8-sig') as f:
                    f.read(4096)
                open_encoding = 'utf-8-sig'
            except (UnicodeDecodeError, UnicodeError):
                open_encoding = 'gbk'

            df = pd.read_csv(fpath, encoding=open_encoding)
            if df.empty:
                continue

            if 'date' in df.columns:
                dt_col = 'date'
            elif 'datetime' in df.columns:
                dt_col = 'datetime'
            else:
                error_msgs.append(f"{fname}: 缺少 date/datetime 列")
                errors += 1
                continue

            _tz_sh = timezone(timedelta(hours=8))

            records = []
            for row in df.itertuples(index=False):
                try:
                    raw_val = getattr(row, dt_col)
                    if hasattr(raw_val, 'strftime'):
                        dt = raw_val if isinstance(raw_val, datetime) else raw_val.to_pydatetime()
                    else:
                        dt_str = str(raw_val).strip()
                        if not dt_str or dt_str == 'nan':
                            continue
                        dt = None
                        for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
                            try:
                                dt = datetime.strptime(dt_str, fmt)
                                break
                            except ValueError:
                                continue
                        if dt is None:
                            continue

                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz_sh)
                    dt = validate_and_calibrate_time(dt, timeframe)
                    if dt is None:
                        continue

                    if dt.tzinfo is not None:
                        dt = dt.replace(tzinfo=None)

                    records.append({
                        "symbol": code,
                        "timeframe": timeframe,
                        "time": dt,
                        "open": float(getattr(row, 'open', 0)),
                        "high": float(getattr(row, 'high', 0)),
                        "low": float(getattr(row, 'low', 0)),
                        "close": float(getattr(row, 'close', 0)),
                        "volume": float(getattr(row, 'volume', 0)),
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
    """将下载的 CSV 数据写入 db_market（调用 bulk_write，多进程并行）"""
    import glob

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)

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

    if db_url:
        os.environ['DATABASE_URL'] = db_url

    if not os.getenv('DATABASE_URL'):
        print("❌ 未设置 DATABASE_URL，请通过以下方式之一提供:")
        print("   1. 设置环境变量: set DATABASE_URL=postgresql://user:pass@host:5432/dbname")
        print("   2. 创建 backend_api_python/.env 文件")
        print("   3. 使用 --db-url 参数")
        return

    from app.utils.db_market import get_market_db_manager

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
    if mgr.market_db_exists("CNStock"):
        pool = mgr._get_pool("CNStock")
        with pool.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'kline_%'")
            has_tables = cur.fetchone()[0] > 0
        if not has_tables:
            mgr.drop_market_db("CNStock")
    mgr.ensure_market_db("CNStock")

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
# 主程序
# ═══════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='🚀 A股全量K线数据下载 - 支持通达信/BaoStock双数据源',
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
    ap.add_argument('--source', '-S',
        choices=['tdx', 'baostock', 'both'],
        default='tdx',
        help='''数据源 (默认tdx):
  tdx       - 通达信协议直连, 支持全部周期
  baostock  - 证券宝HTTP API, 仅支持日线, 分钟线自动回退tdx
  both      - 双源模式: 日线优先baostock, 失败回退tdx; 分钟线走tdx''')
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

    args = ap.parse_args()

    source = args.source
    source_label = {'tdx': '通达信', 'baostock': 'BaoStock', 'both': '双源(优先BaoStock)'}

    # ---- --merge-db 独立模式：跳过下载，直接扫描CSV写入数据库 ----
    if args.merge_db:
        import glob as _glob
        tf_dir_map = {
            'daily': '1D', '1m': '1m', '5m': '5m',
            '15m': '15m', '30m': '30m', '1h': '60m', '60m': '60m',
        }
        found = []
        for subdir, tf in tf_dir_map.items():
            csv_dir = os.path.join(args.output, subdir)
            csvs = _glob.glob(os.path.join(csv_dir, '*.csv'))
            if csvs:
                found.append((csv_dir, tf, len(csvs)))

        if not found:
            print(f"❌ 在 {args.output} 下未找到任何CSV文件")
            print(f"   期望目录结构: {args.output}/{{daily,1m,5m,15m,30m,1h}}/*.csv")
            sys.exit(1)

        print(f"\n{'='*55}")
        print(f"  📦 --merge-db 独立模式（跳过下载）")
        print(f"  扫描目录: {args.output}")
        for csv_dir, tf, cnt in found:
            print(f"    {tf:>4}  →  {csv_dir}  ({cnt} 文件)")
        print(f"{'='*55}")

        for csv_dir, tf, cnt in found:
            merge_to_db(csv_dir, tf, args.workers, db_url=args.db_url)

        print(f"\n{'='*55}")
        print(f"  ✅ 全部完成!")
        print(f"{'='*55}")
        return

    print(f"""
╔═══════════════════════════════════════════════════╗
║  🚀 A股全量K线数据下载                             ║
║  数据源: {source_label.get(source, source):<15}                       ║
╠═══════════════════════════════════════════════════╣
║  类型: {args.type:<10}  进程: {args.workers:<6}               ║
║  输出: {args.output:<38}║
╚═══════════════════════════════════════════════════╝
""")

    outputs = []

    end_date = args.end or datetime.now().strftime('%Y-%m-%d')
    if args.start:
        start_date = args.start
    else:
        start_date = (datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=args.years * 365)).strftime('%Y-%m-%d')

    print(f"  日期范围: {start_date} → {end_date}\n")

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
        for name, freq in [('1m', 4), ('5m', 0), ('15m', 1), ('30m', 5), ('60m', 6)]:
            out = run_minute(args.output, freq, start_date, end_date, args.workers, args.no_resume, args.retry_failed, source=source)
            outputs.append((name, out))
    elif args.type == '1D':
        out = run_daily(args.output, start_date, end_date, args.workers, args.no_resume, args.retry_failed, source=source)
        outputs.append(('1D', out))
    else:
        kind, freq = type_map[args.type]
        out = run_minute(args.output, freq, start_date, end_date, args.workers, args.no_resume, args.retry_failed, source=source)
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
