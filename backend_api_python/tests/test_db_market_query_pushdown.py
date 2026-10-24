"""test_db_market_query_pushdown.py — MarketKlineWriter.query 下推契约测试。

来源：cn_stock 读取提速任务（2026-10-24）｜状态：先红后绿（TDD）

背景与契约（改前必读）：
  · 现状：query 每表一次 information_schema 单点存在性检查 + 每表全量
    SELECT ORDER BY time ASC，Python 合并排序后截尾。1D 11 个年表 =
    11 次额外往返；1m 单股约 3 万行全量取回后扔 94%。
  · 下推契约（改造后必须成立）：
    1. 每表 SQL `ORDER BY time DESC LIMIT n`，跨表合并后全局取尾 n——
       「全局最近 n 条 ⊆ 每表最近 n 条并集」数学成立（全局第 n 新的行，
       在它所在表里的旧行数 ≤ 全局旧行数 ≤ n-1 < n）；
    2. 返回契约不变：升序、≤limit、dict 行（time 为 datetime）；
    3. limit=None 保持全量 ASC 行为（慢路径合并场景依赖）；
    4. 存在性检查不得每表一次单点查询（走缓存 / 批量扫描）；
    5. 跨年新表（ETL 新建 kline_1m_2027）不得因分区缓存重启前不可见——
       当前年/下一年实时补检。
"""
import re
from datetime import datetime, timedelta

import pytest

from app.utils import db_market
from app.utils.db_market import MarketKlineWriter


class FakeCursor:
    """mini SQL 求值器：按 query 生成的 SQL 语义从 fake 表取数。

    只支持本测试用到的 SQL 形状（information_schema LIKE / 单点存在性 /
    kline SELECT），Params 顺序与 WHERE 条件出现顺序一致。
    """

    def __init__(self, tables):
        self.tables = tables  # {table_name: [(datetime, o, h, l, c, v), ...]}
        self.executed = []    # [(归一化空白后的 sql, params)]
        self._last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append((s, tuple(params or ())))
        self._last_result = self._eval(s, tuple(params or ()))

    def _eval(self, s, params):
        if "table_name LIKE" in s:
            prefix = params[0].replace("%", "")
            return [(t,) for t in sorted(self.tables) if t.startswith(prefix)]
        if "SELECT 1 FROM information_schema" in s:
            table = params[0]
            return [(1,)] if table in self.tables else None
        m = re.search(r'FROM "([^"]+)"', s)
        if not m:
            return None
        table = m.group(1)
        rows = list(self.tables.get(table, []))
        desc = "ORDER BY time DESC" in s
        limit = None
        if "LIMIT %s" in s:
            limit = int(params[-1])
            params = params[:-1]
        i = 0
        if "symbol = %s" in s:
            i += 1  # 测试数据单 symbol，symbol 条件恒真
        if "time >= %s" in s:
            start = params[i]; i += 1
            rows = [r for r in rows if r[0] >= start]
        if "time <= %s" in s:
            end = params[i]; i += 1
            rows = [r for r in rows if r[0] <= end]
        if desc:
            rows = list(reversed(rows))
        if limit is not None:
            rows = rows[:limit]
        return rows

    def fetchall(self):
        return self._last_result or []

    def fetchone(self):
        rows = self._last_result
        return rows[0] if rows else None


class FakePool:
    def __init__(self, tables):
        self._cursor = FakeCursor(tables)

    def cursor(self):
        return self._cursor


class FakeManager:
    def __init__(self, tables):
        self._pool = FakePool(tables)

    def market_db_exists(self, market):
        return True

    def _get_pool(self, market):
        return self._pool


def _mk_tables(spec):
    """spec: {year: n_rows} → {table: rows}，每表 time 按年顺序排布。"""
    tables = {}
    for year, n in spec.items():
        rows = []
        base = datetime(year, 1, 2, 15, 0, 0)
        for i in range(n):
            rows.append((base + timedelta(days=i), 1.0 + i, 2.0 + i,
                         0.5 + i, 1.5 + i, 100.0 + i))
        tables[f"kline_1m_{year}"] = rows
    return tables


def _writer(tables):
    return MarketKlineWriter(manager=FakeManager(tables))


def _sql_count(cur, needle):
    return sum(1 for s, _ in cur.executed if needle in s)


# ---------------------------------------------------------------------------
# 1. 下推 SQL 形状与合并语义
# ---------------------------------------------------------------------------

def test_per_table_sql_has_desc_limit():
    """每表 SQL 必须 ORDER BY time DESC + LIMIT %s（现状是 ASC 无 LIMIT ⇒ 红）。"""
    tables = _mk_tables({2025: 10, 2026: 10})
    w = _writer(tables)
    w.query("CNStock", "600519", "1m", limit=3)
    cur = w._mgr._pool._cursor
    selects = [s for s, _ in cur.executed if s.startswith("SELECT time,")]
    assert selects, "没有任何 kline SELECT"
    for s in selects:
        assert "ORDER BY time DESC" in s, f"缺 DESC 下推: {s}"
        assert "LIMIT %s" in s, f"缺 LIMIT 下推: {s}"


def test_pushdown_equals_full_scan_tail():
    """下推合并结果必须与「全量 ASC 取尾」逐位相等（跨表 + 全局截尾）。"""
    tables = _mk_tables({2025: 50, 2026: 50})
    w = _writer(tables)
    got = w.query("CNStock", "600519", "1m", limit=7)
    all_rows = []
    for t in ("kline_1m_2025", "kline_1m_2026"):
        all_rows.extend(tables[t])
    all_rows.sort(key=lambda r: r[0])
    expected = all_rows[-7:]
    assert [r["time"] for r in got] == [r[0] for r in expected]
    assert all(isinstance(r["time"], datetime) for r in got)


def test_pushdown_correct_when_newest_table_sparse():
    """最新年表行数 < limit 时，必须从旧年表补齐（每表各自 LIMIT n 的并集性质）。"""
    tables = _mk_tables({2025: 10, 2026: 3})
    w = _writer(tables)
    got = w.query("CNStock", "600519", "1m", limit=8)
    all_rows = sorted(tables["kline_1m_2025"] + tables["kline_1m_2026"],
                      key=lambda r: r[0])
    assert [r["time"] for r in got] == [r[0] for r in all_rows[-8:]]
    assert len(got) == 8


def test_no_limit_keeps_full_scan():
    """limit=None ⇒ 不推 LIMIT、全量返回（慢路径合并场景依赖此行为）。"""
    tables = _mk_tables({2025: 5, 2026: 5})
    w = _writer(tables)
    got = w.query("CNStock", "600519", "1m", limit=None)
    cur = w._mgr._pool._cursor
    for s, _ in cur.executed:
        if s.startswith("SELECT time,"):
            assert "LIMIT" not in s, f"limit=None 不应推 LIMIT: {s}"
            assert "ORDER BY time ASC" in s, f"limit=None 保持 ASC 全量: {s}"
    assert len(got) == 10


# ---------------------------------------------------------------------------
# 2. 存在性检查：不得每表一次单点查询
# ---------------------------------------------------------------------------

def test_existence_check_not_per_table():
    """11 个年表不得产生 11 次单点存在性查询（现状如此 ⇒ 红）。"""
    tables = _mk_tables({y: 2 for y in range(2016, 2027)})
    w = _writer(tables)
    w.query("CNStock", "600519", "1m", limit=5)
    cur = w._mgr._pool._cursor
    point_checks = _sql_count(cur, "SELECT 1 FROM information_schema")
    assert point_checks == 0, (
        f"单点存在性查询 {point_checks} 次——应走缓存/批量扫描，不得每表一次")


def test_second_query_uses_cache_zero_roundtrip():
    """第二次查询（同年 timeframe）存在性信息应全走缓存，零 information_schema 往返。"""
    tables = _mk_tables({2025: 4, 2026: 4})
    w = _writer(tables)
    w.query("CNStock", "600519", "1m", limit=3)
    cur = w._mgr._pool._cursor
    before = len(cur.executed)
    w.query("CNStock", "600519", "1m", limit=3)
    after_sqls = [s for s, _ in cur.executed[before:]]
    assert not [s for s in after_sqls if "information_schema" in s], (
        f"第二次查询仍打 information_schema: {after_sqls}")


# ---------------------------------------------------------------------------
# 3. 跨年新表发现（修分区缓存漏新表隐患）
# ---------------------------------------------------------------------------

def test_new_year_table_discovered_after_cache(monkeypatch):
    """缓存 {2025,2026} 后 ETL 新建 2027 表，重启前必须能发现（现状永久漏 ⇒ 红）。"""
    tables = _mk_tables({2025: 4, 2026: 4})     # 预热时 2027 表尚不存在
    w = _writer(tables)
    # 预热缓存：monkeypatch 当前年为 2026，缓存只应有 {2025, 2026}
    monkeypatch.setattr(db_market, "_current_year", lambda: 2026)
    w.query("CNStock", "600519", "1m", limit=3)
    cur = w._mgr._pool._cursor
    before = len(cur.executed)
    # ETL 跨年新建 2027 表（fake 表集合原地增加，LIKE 二次扫描已不会发生）
    tables["kline_1m_2027"] = _mk_tables({2027: 4})["kline_1m_2027"]
    # 时间推进到 2027：缓存仍是 {2025,2026}，2027 表应被实时补检发现
    monkeypatch.setattr(db_market, "_current_year", lambda: 2027)
    got = w.query("CNStock", "600519", "1m", limit=3)
    after_sqls = [s for s, _ in cur.executed[before:]]
    assert any("information_schema" in s for s in after_sqls), (
        "跨年新表未触发任何补检——分区缓存把 2027 表挡在重启前")
    all_rows = sorted(tables["kline_1m_2025"] + tables["kline_1m_2026"]
                      + tables["kline_1m_2027"], key=lambda r: r[0])
    assert [r["time"] for r in got] == [r[0] for r in all_rows[-3:]], (
        "2027 表数据必须出现在结果里（最新 3 条全在 2027）")


# ---------------------------------------------------------------------------
# 4. 时间窗下推
# ---------------------------------------------------------------------------

def test_window_pushed_into_sql():
    """start/end_time 必须下推为 SQL WHERE 条件（现状已做，防回归）。"""
    tables = _mk_tables({2025: 40})
    w = _writer(tables)
    start = datetime(2025, 1, 10, 15, 0, 0)
    end = datetime(2025, 1, 20, 15, 0, 0)
    got = w.query("CNStock", "600519", "1m", start_time=start, end_time=end,
                  limit=None)
    cur = w._mgr._pool._cursor
    sel = [s for s, _ in cur.executed if s.startswith("SELECT time,")][0]
    assert "time >= %s" in sel and "time <= %s" in sel
    assert all(start <= r["time"] <= end for r in got)


def test_window_with_limit_takes_tail_of_window():
    """窗口 + limit ⇒ 窗口内最新 limit 条（闭区间过取由调用侧开区间过滤兜底）。"""
    tables = _mk_tables({2025: 40})
    w = _writer(tables)
    end = datetime(2025, 1, 20, 15, 0, 0)
    got = w.query("CNStock", "600519", "1m", end_time=end, limit=5)
    in_window = [r for r in tables["kline_1m_2025"] if r[0] <= end]
    assert [r["time"] for r in got] == [r[0] for r in in_window[-5:]]
