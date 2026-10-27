# -*- coding: utf-8 -*-
"""1.log 报障修复回归（2026-10-27）。

A. 龙虎榜三池空日期默认今天 —— 调用方不传 trade_date 时 akshare 拿到 date=""
   （stock_zt_pool_em 返空、dtgc/zbgc 库内 int('') 崩），三池全空/全炸。
B. upsert_stocks 写入计数 —— execute_values(page_size=1000) 内部拆多条 SQL，
   cur.rowcount 只反映最后一页（5234 条误报 234 = 末页大小）⇒ 外层显式分页累加。
"""
import sys
from datetime import datetime

sys.path.insert(0, ".")

import pandas as pd

import app.data_sources.provider_pool.akshare as A
import app.utils.basicinfo_db as B


class _FakeLimiter:
    def wait(self):
        pass


def test_pool_empty_date_defaults_today(monkeypatch):
    """空 trade_date ⇒ akshare 收到今天 YYYYMMDD（非空、8 位）。"""
    seen = {}

    class _FakeAk:
        def stock_zt_pool_dtgc_em(self, date=""):
            seen["date"] = date
            return pd.DataFrame([{
                "代码": "000001", "名称": "平安银行", "最新价": 10.0,
                "涨跌幅": -10.0, "封单资金": 1.0, "换手率": 1.0, "成交额": 2.0,
            }])

    monkeypatch.setattr(A, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(A, "get_akshare_limiter", lambda: _FakeLimiter())
    env = A.fetch_limit_down_pool("")                     # 不传日期（dragon_limit 现行调用形）
    assert seen["date"] == datetime.now().strftime("%Y%m%d")
    assert env["rows"] and env["rows"][0]["stock_code"] == "000001"
    assert env["rows"][0]["trade_date"] == datetime.now().strftime("%Y-%m-%d")


def test_pool_explicit_date_is_honored(monkeypatch):
    seen = {}

    class _FakeAk:
        def stock_zt_pool_em(self, date=""):
            seen["date"] = date
            return pd.DataFrame()

    monkeypatch.setattr(A, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(A, "get_akshare_limiter", lambda: _FakeLimiter())
    A.fetch_limit_up_pool("2026-10-27")
    assert seen["date"] == "20261027"


def test_upsert_count_sums_all_pages(monkeypatch):
    """写入计数 = 全部分页总和（不许只报末页大小）。"""
    n_rows = 5234                                   # 5 页整 + 末页 234 的复现数字

    class _Cur:
        def __init__(self):
            self.rowcount = 0

        def execute(self, *a, **k):
            pass

        def close(self):
            pass

    class _Conn:
        def __init__(self):
            self._c = _Cur()

        def cursor(self):
            return self._c

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Pool:
        def connection(self):
            return _Conn()

    def fake_execute_values(cur, sql, args, page_size=1000):
        cur.rowcount = len(args)                     # 每次 execute 影响 len(args) 行

    monkeypatch.setattr(B.StockBasicDB, "_get_pool", lambda self: _Pool())
    monkeypatch.setattr(B.StockBasicDB, "ensure_table", lambda self: None)
    monkeypatch.setattr("psycopg2.extras.execute_values", fake_execute_values)

    stocks = [{"symbol": "%06d" % i, "name": "n%d" % i, "market_cn": "SH"}
              for i in range(n_rows)]
    r = B.StockBasicDB().upsert_stocks(stocks)
    assert r["errors"] == 0
    assert r["inserted"] == n_rows, (
        f"写入计数应累计全部分页，实得 {r['inserted']}（末页假账会报 234）")


def test_market_kline_pool_capacity_matches_workers():
    """执行器槽位必须 ≥ worker 上限 —— 12 槽 vs 30 worker ⇒ 幻影三振
    （饱和被记成源失败，3730 只里 2793 只从未真正 fetch 就判彻底失败）。

    三个池的槽位数必须与各自 max_workers 同步，防再次漂移。
    """
    from app.data_sources import coordinator as C
    pools = (
        ("coord-timeout", C._timeout_pool, C._timeout_slots),
        ("bquote-timeout", C._batch_timeout_pool, C._batch_timeout_slots),
        ("mkline-timeout", C._mkline_timeout_pool, C._mkline_timeout_slots),
    )
    for name, pool, slots in pools:
        assert slots._initial_value == pool._max_workers, (
            f"{name}: 槽位 {slots._initial_value} != max_workers {pool._max_workers}")
    # market_kline 峰值 worker ≈ 30（tencent6+sina4+10jqka4+sohu8+baidu8）
    assert C._mkline_timeout_pool._max_workers >= 30, (
        "mkline 超时池容量必须覆盖 market_kline 的 worker 总数")


def test_executor_saturation_is_not_provider_call_error():
    """饱和异常与 provider 异常分类不同 —— 调用方据此不计源失败。"""
    import threading

    import concurrent.futures

    from app.data_sources.dispatch import (
        ExecutorSaturatedError, ProviderCallError, submit_bounded,
    )

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        slots = threading.BoundedSemaphore(1)
        ev = threading.Event()
        f = submit_bounded(pool, slots, ev.wait)      # 占住唯一槽位
        try:
            submit_bounded(pool, slots, lambda: 2)
            raise AssertionError("槽位耗尽应抛 ExecutorSaturatedError")
        except ExecutorSaturatedError:
            pass
        assert not issubclass(ExecutorSaturatedError, ProviderCallError)
        ev.set()
        assert f.result(timeout=5) is True
    finally:
        pool.shutdown(wait=False)
