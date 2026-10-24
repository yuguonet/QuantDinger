"""test_cn_stock_rt_gate.py — cn_stock rt（realtime_snapshot）闸门与降级链测试。

来源：cn_stock 读取提速/本地化任务（2026-10-24，用户拍板口径）｜状态：先红后绿

══════════ 口径契约（用户 2026-10-24 逐条拍板，实现必须满足） ══════════
三态读取（1m/1D 为例，其余周期聚合）：
  · 非盘中·已落库 → DB 直读
  · 非盘中·未落库 → DB 历史 + rt 转换拼当日（1D=末拍转 bar；1m=全日 tick 合成）
  · 盘中 → 与未落库同构
  · 全部兜底 = 远端 coordinate
rt 闸门（防空防漏）：
  · 新鲜度（尾）：连续竞价期 now-末拍 ≤ 120s（QD_RT_STALE_SECONDS，只允许丢
    1 根）；盘后期望末拍=15:00（早于 14:55 判采集器早退）；午休末拍≈11:30；
    盘前/异日/非交易日不拼；
  · 1m 序列（首+中）：首必须有数据（上午段首个段内 tick 落在开盘 5 槽内，
    即 09:30-09:35）；同段中间连续缺失 > QD_RT_MAX_GAP_SLOTS(5) 根判不可信；
    按**缺失根数**判不按间隔分钟数判（间隔 5.67min 只缺 5 根 ⇒ 放行）；
    午休/跨夜/09:25 竞价拍为结构空档，不判；
  · 1D lastbar 只用末拍（rt 每拍 open/high/low 为当日累计值，单拍即全日
    OHLCV）⇒ 只检尾鲜度，无缺口问题；
  · 合成数据永不写回 kline 表（ETL 唯一写入口）；coordinate 兜底结果走
    既有写库路径。
已知边界语义（如实登记，非缺陷）：采集器死于 11:25-11:29 / 14:55-14:59
（距收盘 ≤5 根）按「最多缺 5 根」口径不兜底。
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.data_sources import cn_stock
from app.data_sources.cn_stock import CNStockDataSource, _rt_freshness_ok

_BJ = ZoneInfo("Asia/Shanghai")
_DAY = datetime(2026, 10, 23)          # 周五（实测有完整 rt 数据的交易日）


def _dt(h, m, s=0, day=None):
    return datetime(day.year, day.month, day.day, h, m, s) if day else \
        datetime(_DAY.year, _DAY.month, _DAY.day, h, m, s)


def _epoch(dt):
    return int(dt.replace(tzinfo=_BJ).timestamp())


class _FixedDatetimeMeta(type):
    """让 isinstance(真datetime实例, _FixedDatetime) 为 True。

    必需：`fixed_now` 把 cn_stock.datetime 换成 _FixedDatetime（datetime 子类），
    而 isinstance(基类实例, 子类) 常规为 False ⇒ cn_stock 内
    `isinstance(t, datetime)`（_snapshot_ticks_to_1m / _read_db 等）对真
    datetime 实例全部 False ⇒ 合成/转换链静默空转（不报错，只返空）。
    """
    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)


class _FixedDatetime(datetime, metaclass=_FixedDatetimeMeta):
    """继承真 datetime，只覆盖 now() —— 其余类方法（fromtimestamp 等）照常。"""
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


@pytest.fixture
def fixed_now(monkeypatch):
    """把 cn_stock.datetime.now 钉在指定时刻（北京时间 naive）。"""
    def _set(h, m, s=0, day=None):
        cn_stock.datetime = _FixedDatetime
        _FixedDatetime._now = _dt(h, m, s, day)
        return _FixedDatetime._now
    yield _set
    cn_stock.datetime = datetime


# ---------------------------------------------------------------------------
# A. 尾鲜度判据 _rt_freshness_ok(snap_time, now)
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_intraday_fresh_within_120s(self):
        assert _rt_freshness_ok(_dt(9, 59), _dt(10, 0)) is True

    def test_intraday_boundary_120s_ok(self):
        """用户口径「不大于 2 分钟，极限」⇒ 恰好 120s 放行。"""
        assert _rt_freshness_ok(_dt(9, 58), _dt(10, 0)) is True

    def test_intraday_121s_rejected(self):
        """121s = 丢了 2 根 ⇒ 判不可信。"""
        assert _rt_freshness_ok(_dt(9, 57, 59), _dt(10, 0)) is False

    def test_lunch_break_last_tick_ok(self):
        """午休无新拍：末拍=11:30 收盘拍即最新。"""
        assert _rt_freshness_ok(_dt(11, 30), _dt(12, 0)) is True

    def test_lunch_break_early_exit_rejected(self):
        """上午 11:20 采集器早退 ⇒ 末拍不可能是 11:30 那拍。"""
        assert _rt_freshness_ok(_dt(11, 20), _dt(12, 0)) is False

    def test_post_close_last_tick_1500_ok(self):
        """盘后期望末拍=15:00（用户口径；实测四天末拍全部 15:00:0x）。"""
        assert _rt_freshness_ok(_dt(15, 0, 1), _dt(15, 30)) is True

    def test_post_close_early_exit_rejected(self):
        """末拍 14:54 = 采集器早退超过 5 槽 ⇒ 不可用。"""
        assert _rt_freshness_ok(_dt(14, 54), _dt(15, 30)) is False

    def test_pre_market_stale_rejected(self):
        """盘前 09:20：今日不该有拍（有也是陈旧脏数据）。"""
        yesterday = _DAY - timedelta(days=1)
        assert _rt_freshness_ok(_dt(15, 0, day=yesterday), _dt(9, 20)) is False

    def test_auction_segment_first_tick_ok(self):
        """09:15-09:30 竞价段：只接受 09:25 集合竞价出价那拍。"""
        assert _rt_freshness_ok(_dt(9, 25, 30), _dt(9, 26)) is True
        assert _rt_freshness_ok(_dt(9, 20), _dt(9, 26)) is False

    def test_prev_day_tick_rejected(self):
        """周一盘中拿着上周五的拍（采集器整天没写）⇒ 不可信。"""
        friday = _DAY - timedelta(days=3)
        assert _rt_freshness_ok(_dt(15, 0, day=friday), _dt(10, 0)) is False


# ---------------------------------------------------------------------------
# B. 1m 序列判据 _rt_series_gap_ok(times) —— 首必须有数据 + 中间最多缺 5 根
# ---------------------------------------------------------------------------

def _full_day_ticks():
    """实测形态：09:25 竞价拍 + 两段交易时段内分钟 tick + 15:00 收盘拍。"""
    ts = [_dt(9, 25)]
    hm = 9 * 60 + 30
    while hm < 11 * 60 + 30:
        ts.append(_dt(hm // 60, hm % 60))
        hm += 1
    ts.append(_dt(11, 30))
    hm = 13 * 60
    while hm < 15 * 60:
        ts.append(_dt(hm // 60, hm % 60))
        hm += 1
    ts.append(_dt(15, 0, 1))
    return ts


class TestSeriesGap:
    def test_full_day_ok(self):
        assert cn_stock._rt_series_gap_ok(_full_day_ticks()) is True

    def test_natural_jitter_ok(self):
        """实测自然抖动：相邻 1~2 分钟间隔占 100%（643×1min + 190×2min）。"""
        ts = [_dt(9, 25)]
        for seg_start, seg_end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
            hm = seg_start
            step = 1
            while hm < seg_end:
                ts.append(_dt(hm // 60, hm % 60))
                hm += step
                step = 2 if step == 1 else 1
        assert cn_stock._rt_series_gap_ok(ts) is True

    def test_missing_open_first_tick_rejected(self):
        """开盘 10 分钟无数据（上午段首 tick 09:40）⇒ 判不可信。"""
        ts = [t for t in _full_day_ticks() if not (9 * 60 + 30 <=
              t.hour * 60 + t.minute < 9 * 60 + 40)]
        assert cn_stock._rt_series_gap_ok(ts) is False

    def test_open_first_tick_at_5_slots_ok(self):
        """上午段首 tick 09:35 = 开盘起缺 5 根，恰在阈值内 ⇒ 放行。"""
        ts = [t for t in _full_day_ticks() if not (9 * 60 + 30 <=
              t.hour * 60 + t.minute < 9 * 60 + 35)]
        assert cn_stock._rt_series_gap_ok(ts) is True

    def test_mid_gap_6_slots_rejected(self):
        """下午中间连续缺 6 根 ⇒ 判不可信（用户口径：最多缺 5 根）。"""
        ts = [t for t in _full_day_ticks() if not (13 * 60 + 1 <=
              t.hour * 60 + t.minute <= 13 * 60 + 6)]
        assert cn_stock._rt_series_gap_ok(ts) is False

    def test_mid_gap_5_slots_ok(self):
        """下午中间连续缺 5 根 ⇒ 边界内放行。"""
        ts = [t for t in _full_day_ticks() if not (13 * 60 + 1 <=
              t.hour * 60 + t.minute <= 13 * 60 + 5)]
        assert cn_stock._rt_series_gap_ok(ts) is True

    def test_gap_counted_by_slots_not_minutes(self):
        """09:30:30→09:36:10 间隔 5.67 分钟但只缺 5 根（按根数判 ⇒ 放行）。"""
        ts = [t for t in _full_day_ticks()
              if not (9 * 60 + 31 <= t.hour * 60 + t.minute <= 9 * 60 + 35)]
        ts.append(_dt(9, 36, 10))
        assert cn_stock._rt_series_gap_ok(ts) is True

    def test_lunch_break_not_counted(self):
        """午休 90min 跨段不判（结构空档）。"""
        ts = _full_day_ticks()
        assert cn_stock._rt_series_gap_ok(ts) is True   # 全量含午休

    def test_empty_series_rejected(self):
        assert cn_stock._rt_series_gap_ok([]) is False

    def test_only_auction_tick_rejected(self):
        """只有 09:25 竞价拍（段内无 tick）⇒ 不可信。"""
        assert cn_stock._rt_series_gap_ok([_dt(9, 25)]) is False


# ---------------------------------------------------------------------------
# C. _read_snapshot_lastbar 闸门集成（fake DB manager）
# ---------------------------------------------------------------------------

class _FakeSnapshotCursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._row

    def fetchall(self):
        # R2 重构后 _read_snapshot_lastbar 经 _query_snapshot_rows 统一走
        # fetchall（tuple→dict 收口一处）；fetchone 保留供直接调用方使用。
        return [self._row] if self._row is not None else []

    @property
    def description(self):
        return [("time",), ("last",), ("open",), ("high",), ("low",),
                ("volume",)]


class _FakeSnapshotConn:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeSnapshotCursor(self._row)


class _FakeSnapshotPool:
    def __init__(self, row):
        self._row = row

    def connection(self):
        return _FakeSnapshotConn(self._row)


class _FakeSnapshotMgr:
    def __init__(self, row):
        self._pool = _FakeSnapshotPool(row)

    def _get_pool(self, market):
        return self._pool


def _snap_row(h, m, s=0, price=1680.0):
    return (_dt(h, m, s), price, 100.0, price + 5.0, price - 5.0, 12345.0)


class TestSnapshotLastbar:
    @pytest.fixture(autouse=True)
    def _patch_mgr(self, monkeypatch):
        import app.utils.db_market as dbm
        self._row = _snap_row(9, 59)
        monkeypatch.setattr(dbm, "get_market_db_manager",
                            lambda: _FakeSnapshotMgr(self._row))

    def _ds(self):
        return CNStockDataSource()

    def test_fresh_tick_returns_bar(self, fixed_now):
        fixed_now(10, 0)
        bar = self._ds()._read_snapshot_lastbar("600519")
        assert bar is not None
        assert bar["time"] == _epoch(_dt(15, 0))      # 归一化当日 15:00
        assert bar["close"] == 1680.0

    def test_stale_tick_rejected(self, fixed_now, monkeypatch):
        import app.utils.db_market as dbm
        monkeypatch.setattr(dbm, "get_market_db_manager",
                            lambda: _FakeSnapshotMgr(_snap_row(9, 30)))
        fixed_now(10, 0)                                # 末拍 30 分钟前 ⇒ stale
        assert self._ds()._read_snapshot_lastbar("600519") is None

    def test_prev_day_tick_rejected(self, fixed_now, monkeypatch):
        import app.utils.db_market as dbm
        friday = _DAY - timedelta(days=3)
        row = (datetime(friday.year, friday.month, friday.day, 15, 0, 1),
               1680.0, 100.0, 1685.0, 1675.0, 12345.0)
        monkeypatch.setattr(dbm, "get_market_db_manager",
                            lambda: _FakeSnapshotMgr(row))
        fixed_now(10, 0)
        assert self._ds()._read_snapshot_lastbar("600519") is None

    def test_non_positive_price_rejected(self, fixed_now, monkeypatch):
        import app.utils.db_market as dbm
        monkeypatch.setattr(dbm, "get_market_db_manager",
                            lambda: _FakeSnapshotMgr(_snap_row(9, 59, price=0.0)))
        fixed_now(10, 0)
        assert self._ds()._read_snapshot_lastbar("600519") is None


# ---------------------------------------------------------------------------
# D. _get_kline_1d 降级链：盘后未落库 rt-first，拼不出 coordinate 兜底
# ---------------------------------------------------------------------------

class _FakeCoordinator:
    def __init__(self, bars):
        self.calls = []
        self._bars = bars

    def coordinate_kline(self, **kw):
        self.calls.append(kw)
        return {"bars": list(self._bars)}


class TestKline1dFallback:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        self.today = _dt(15, 0)
        self.prev = _DAY - timedelta(days=1)
        self.db_bars = [
            {"time": _epoch(self.prev - timedelta(days=1)), "open": 99.0,
             "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1000.0},
            {"time": _epoch(_dt(15, 0, day=self.prev)), "open": 100.0,
             "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1100.0},
        ]
        self.lastbar = {
            "time": _epoch(self.today), "open": 101.0, "high": 105.0,
            "low": 100.5, "close": 104.0, "volume": 1200.0,
        }
        self.remote_bars = [dict(self.lastbar)]
        self.coord = _FakeCoordinator(self.remote_bars)
        self.saved = []

        monkeypatch.setattr(cn_stock, "is_trading_day",
                            lambda s: s == _DAY.strftime("%Y-%m-%d"))
        monkeypatch.setattr(cn_stock, "_today_ts", lambda: _epoch(self.today))
        monkeypatch.setattr(cn_stock, "_prev_trading_day_ts",
                            lambda: _epoch(_dt(15, 0, day=self.prev)))
        monkeypatch.setattr(cn_stock, "get_coordinator", lambda: self.coord)
        monkeypatch.setattr(cn_stock, "unadj_to_qfq",
                            lambda bars, sym: bars)
        monkeypatch.setattr(cn_stock, "_normalize_1d_bars",
                            lambda bars: bars)
        monkeypatch.setattr(CNStockDataSource, "_read_db",
                            lambda self, sym, *a, **k: list(self._env.db_bars))
        monkeypatch.setattr(CNStockDataSource, "_save_to_db",
                            lambda self, sym, bars: self._env.saved.append(bars))

    def _mk_ds(self, *, in_trading, lastbar):
        ds = CNStockDataSource()
        ds._env = self
        monkeypatch = self._mp
        monkeypatch.setattr(cn_stock, "_is_in_trading_hours",
                            lambda: in_trading)
        monkeypatch.setattr(CNStockDataSource, "_read_snapshot_lastbar",
                            lambda self, sym, *a, **k:
                                dict(self._env.lastbar) if lastbar else None)
        monkeypatch.setattr(CNStockDataSource, "get_ticker",
                            lambda self, sym, *a, **k: None)
        return ds

    @pytest.fixture(autouse=True)
    def _mp_holder(self, monkeypatch):
        self._mp = monkeypatch

    def test_post_close_rt_first_no_coordinate(self, fixed_now):
        """盘后 15:30 DB 未落库 + rt 末拍新鲜 ⇒ 拼 lastbar 直返，coordinate 零调用。
        （现状 _post_close 绕进远端慢路径 ⇒ 红）"""
        fixed_now(15, 30)
        ds = self._mk_ds(in_trading=False, lastbar=True)
        out = ds._get_kline_1d("600519", 300)
        assert [b["time"] for b in out][-1] == _epoch(self.today), \
            "结果必须含当日 lastbar"
        assert self.coord.calls == [], "rt 可用时不得走远端"

    def test_post_close_rt_unavailable_falls_back(self, fixed_now):
        """盘后 rt 末拍被闸门挡（None）⇒ coordinate 兜底（防回归）。"""
        fixed_now(15, 30)
        ds = self._mk_ds(in_trading=False, lastbar=False)
        out = ds._get_kline_1d("600519", 300)
        assert self.coord.calls, "rt 不可用必须 coordinate 兜底"
        assert [b["time"] for b in out][-1] == _epoch(self.today)

    def test_intraday_lastbar_stitch_no_coordinate(self, fixed_now):
        """盘中 lastbar 拼接（现状已支持，防回归）。"""
        fixed_now(10, 0)
        ds = self._mk_ds(in_trading=True, lastbar=True)
        out = ds._get_kline_1d("600519", 300)
        assert [b["time"] for b in out][-1] == _epoch(self.today)
        assert self.coord.calls == []

    def test_intraday_lastbar_missing_falls_back(self, fixed_now):
        """盘中 lastbar 拼不出 ⇒ coordinate 完整兜底（现状返 DB 陈旧缺当日 ⇒ 红）。"""
        fixed_now(10, 0)
        ds = self._mk_ds(in_trading=True, lastbar=False)
        out = ds._get_kline_1d("600519", 300)
        assert self.coord.calls, "盘中拼不出必须兜底，不得返回缺当日的陈旧数据"
        assert [b["time"] for b in out][-1] == _epoch(self.today)


# ---------------------------------------------------------------------------
# E. 盘中 1m 本地化：get_kline 盘中分发 + _get_kline_intraday_raw 闸门
# ---------------------------------------------------------------------------

def _snap_rows(ticks, price0=1680.0):
    rows = []
    for i, t in enumerate(ticks):
        price = price0 + i * 0.1
        rows.append({"time": t, "open": price0, "high": price + 1.0,
                     "low": price - 1.0, "last": price,
                     "volume": 1000.0 * (i + 1), "previousClose": price0 - 1.0})
    return rows


class TestIntradayLocalFirst:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        self.coord = _FakeCoordinator([])
        self.remote_calls = []
        self.db_1m = [
            {"time": _epoch(_dt(15, 0, day=_DAY - timedelta(days=1)))
             - (4 - i) * 60,                # 升序：bars[-1] = DB 最新 bar
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
             "volume": 500.0}
            for i in range(5)
        ]
        monkeypatch.setattr(cn_stock, "is_trading_day",
                            lambda s: s == _DAY.strftime("%Y-%m-%d"))
        monkeypatch.setattr(cn_stock, "unadj_to_qfq",
                            lambda bars, sym: bars)
        monkeypatch.setattr(CNStockDataSource, "_read_db_1m",
                            lambda self, sym, limit: list(self._env.db_1m))
        monkeypatch.setattr(
            CNStockDataSource, "_get_kline_remote",
            lambda self, sym, tf, limit, bt=None, at=None:
                self._env.remote_calls.append((sym, tf)) or [])

    def _mk_ds(self, snap_ticks):
        ds = CNStockDataSource()
        ds._env = self
        monkeypatch = self._mp
        monkeypatch.setattr(CNStockDataSource, "_read_snapshot_series",
                            lambda self, sym, day: _snap_rows(snap_ticks))
        return ds

    @pytest.fixture(autouse=True)
    def _mp_holder(self, monkeypatch):
        self._mp = monkeypatch

    def test_intraday_1m_local_first(self, fixed_now):
        """盘中 10:00：DB 历史 + rt 合成（新鲜无洞）⇒ 本地拼接，远端零调用。
        （现状 get_kline 盘中直接走远端 ⇒ 红）"""
        fixed_now(10, 0)
        ticks = [t for t in _full_day_ticks()
                 if t.hour * 60 + t.minute <= 10 * 60]
        ds = self._mk_ds(ticks)
        out = ds.get_kline("600519", "1m", 300)
        assert out, "盘中必须有数据返回"
        assert any(b["time"] >= _epoch(_dt(9, 31)) for b in out), \
            "结果必须含当日合成段"
        assert self.remote_calls == [], "rt 可用时不得走远端"

    def test_intraday_1m_gap_rejected_falls_back(self, fixed_now):
        """盘中 rt 序列中间缺 6 根 ⇒ 闸门挡 ⇒ 远端兜底。"""
        fixed_now(10, 0)
        ticks = [t for t in _full_day_ticks()
                 if t.hour * 60 + t.minute <= 10 * 60
                 and not (9 * 60 + 31 <= t.hour * 60 + t.minute <= 9 * 60 + 36)]
        ds = self._mk_ds(ticks)
        ds.get_kline("600519", "1m", 300)
        assert self.remote_calls, "序列缺口 >5 根必须走远端"

    def test_post_close_unbackfilled_stitch(self, fixed_now):
        """盘后 15:30 DB 未落库 + rt 合成可用 ⇒ 本地拼接（现状已支持，防回归）。"""
        fixed_now(15, 30)
        ds = self._mk_ds(_full_day_ticks())
        out = ds.get_kline("600519", "1m", 300)
        assert any(b["time"] >= _epoch(_dt(9, 31)) for b in out)
        assert self.remote_calls == []

    def test_post_close_rt_gap_rejected_falls_back(self, fixed_now):
        """盘后 rt 序列缺 6 根 ⇒ 闸门挡 ⇒ 远端兜底（闸门新增 ⇒ 红）。"""
        fixed_now(15, 30)
        ticks = [t for t in _full_day_ticks()
                 if not (13 * 60 + 1 <= t.hour * 60 + t.minute <= 13 * 60 + 6)]
        ds = self._mk_ds(ticks)
        ds.get_kline("600519", "1m", 300)
        assert self.remote_calls, "盘后序列缺口 >5 根必须走远端"
