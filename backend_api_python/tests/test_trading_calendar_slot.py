# -*- coding: utf-8 -*-
"""交易日历插槽测试（2026-10-26）。

覆盖三类行为，对应 v3 §3.2 的三态：
  · 命中且新鲜       → 不联网
  · 刷新失败但有覆盖 → 降级使用快照 + degraded=True（不静默、不冒泡）
  · 刷新失败且无覆盖 → CalendarUnavailable（显式失败，不假装"非交易日"）

另外锁死"公共 API 签名不变"——这是插槽化"签名冻结"的验收条件。
"""
import inspect
import os
import pickle
from datetime import datetime, timedelta

import pytest

from app.utils import trading_calendar as tc


def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "_CACHE_FILE", str(tmp_path / "td.pkl"))
    monkeypatch.setattr(tc, "_cached_dates", set())
    monkeypatch.setattr(tc, "_sorted_dates", [])
    monkeypatch.setattr(tc, "_loaded", False)
    monkeypatch.setattr(tc, "_status", {})


def _write_legacy(path, dates):
    with open(path, "wb") as f:
        pickle.dump(set(dates), f)


def _write_v2(path, dates, fetched_at, coverage_end):
    with open(path, "wb") as f:
        pickle.dump({"version": 2, "dates": set(dates), "fetched_at": fetched_at,
                     "coverage_end": coverage_end}, f)


def _days(start_offset, count):
    base = datetime.now().date()
    return [(base + timedelta(days=start_offset + i)).isoformat() for i in range(count)]


# ── 1. 新鲜缓存：不许联网 ────────────────────────────────────────

def test_fresh_cache_used_without_network(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    dates = _days(0, 90)                      # 覆盖今天 + 89 天
    _write_legacy(tc._CACHE_FILE, dates)

    def boom():
        raise AssertionError("新鲜缓存不应触发联网")

    monkeypatch.setattr(tc, "_from_akshare", boom)
    assert tc.is_trading_day(dates[0]) is True
    assert tc.calendar_status()["source"] == "cache"
    assert tc.calendar_status()["degraded"] is False


# ── 2. 覆盖不足：触发刷新，并写成 v2 元数据 ─────────────────────

def test_expired_coverage_triggers_refresh(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    old = _days(-60, 55)                      # 只覆盖到昨天
    _write_legacy(tc._CACHE_FILE, old)

    fresh = _days(-60, 120)
    monkeypatch.setattr(tc, "_from_akshare", lambda: set(fresh))

    assert tc.is_trading_day(fresh[-1]) is True
    assert tc.calendar_status()["source"] == "akshare"

    with open(tc._CACHE_FILE, "rb") as f:
        payload = pickle.load(f)
    assert payload["version"] == 2
    assert payload["coverage_end"] == max(fresh)


# ── 3. 刷新失败 + 有覆盖：降级，不冒泡 ─────────────────────────

def test_refresh_failure_degrades_to_snapshot(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    dates = _days(-10, 12)                    # 覆盖到今天 +1，不够 lookahead
    _write_v2(tc._CACHE_FILE, dates, fetched_at="2020-01-01",
              coverage_end=dates[-1])

    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(tc, "_from_akshare", boom)

    assert tc.is_trading_day(dates[0]) is True          # 快照仍可用
    st = tc.calendar_status()
    assert st["source"] == "cache"
    assert st["degraded"] is True


# ── 4. 全源失败且无缓存：显式失败，不静默 ────────────────────

def test_all_sources_fail_without_cache_raises(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(tc, "_from_akshare", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(tc.CalendarUnavailable):
        tc.is_trading_day("2026-10-26")


# ── 5. 过期快照：超出覆盖范围必须判 False（诚实边界）──────────

def test_beyond_coverage_is_false(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    dates = _days(-5, 6)
    _write_v2(tc._CACHE_FILE, dates, fetched_at="2020-01-01", coverage_end=dates[-1])
    monkeypatch.setattr(tc, "_from_akshare", lambda: (_ for _ in ()).throw(RuntimeError("down")))

    future = (datetime.now().date() + timedelta(days=400)).isoformat()
    assert tc.is_trading_day(future) is False


# ── 6. 公共 API 签名冻结（插槽化的验收条件）───────────────────

def test_public_api_signatures_frozen():
    expected = {
        "is_trading_day": ["date"],
        "is_trading_day_today": [],
        "prev_trading_day": ["date", "n"],
        "next_trading_day": ["date", "n"],
        "last_finish_trading_day": ["ref_dt"],
        "trade_date_range": ["start_date", "end_date"],
        "trading_days_count": ["start_date", "end_date"],
        "is_business_day": ["date"],
    }
    for name, params in expected.items():
        sig = inspect.signature(getattr(tc, name))
        assert list(sig.parameters) == params, "%s 签名被改动" % name


# ── 7. prev/next 使用已排序缓存，结果正确 ─────────────────────

def test_prev_next_use_sorted_cache(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    dates = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    _write_v2(tc._CACHE_FILE, dates, fetched_at=datetime.now().strftime("%Y-%m-%d"),
              coverage_end="2026-12-31")
    assert tc.prev_trading_day("2026-10-22") == "2026-10-21"
    assert tc.next_trading_day("2026-10-22") == "2026-10-23"
    assert tc.trade_date_range("2026-10-20", "2026-10-22") == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert tc.trading_days_count("2026-10-20", "2026-10-22") == 3
