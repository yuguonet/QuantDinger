"""Maturity and claim dates must use the same trading-day calendar."""
from datetime import date

import pytest

from app.agent.chain import claims, evaluator
from app.utils import trading_calendar as tc


_BASE = date(2026, 1, 2)  # Friday
_TRADING_DATES = {
    "2026-01-05",  # Monday
    "2026-01-06",
    "2026-01-08",  # Jan 7 is a market holiday
    "2026-01-09",
}


@pytest.fixture
def calendar(monkeypatch):
    monkeypatch.setattr(tc, "_load", lambda: _TRADING_DATES)


def test_nth_trading_day_skips_weekends_and_holidays(calendar):
    assert tc.nth_trading_day(_BASE, 1) == date(2026, 1, 5)
    assert tc.nth_trading_day(_BASE, 3) == date(2026, 1, 8)


def test_nth_trading_day_returns_none_when_calendar_does_not_cover_horizon(calendar):
    assert tc.nth_trading_day(_BASE, 5) is None


def test_claim_due_date_uses_exact_trading_day(calendar, monkeypatch):
    monkeypatch.setenv("QD_DUEDATE_EXACT", "0")
    assert claims.due_date_of("T+3", _BASE) == date(2026, 1, 8)


def test_claim_due_date_surfaces_insufficient_calendar_coverage(calendar):
    with pytest.raises(tc.CalendarUnavailable, match="覆盖不足"):
        claims.due_date_of("T+5", _BASE)


def test_evaluator_maturity_date_matches_claim_due_date(calendar):
    due = claims.due_date_of("T+3", _BASE)
    assert evaluator._maturity_date(_BASE, 3) == due
    assert evaluator._maturity_date(_BASE, 3) > date(2026, 1, 7)
    assert evaluator._maturity_date(_BASE, 3) <= date(2026, 1, 8)


def test_evaluator_does_not_guess_when_calendar_is_unavailable(monkeypatch):
    def unavailable():
        raise tc.CalendarUnavailable("no calendar")

    monkeypatch.setattr(tc, "_load", unavailable)
    with pytest.raises(tc.CalendarUnavailable, match="no calendar"):
        evaluator._maturity_date(_BASE, 1)


def test_evaluator_defers_unavailable_calendar_without_counting_market_failure(monkeypatch):
    monkeypatch.setattr(
        evaluator.store,
        "query_pending_verify",
        lambda **kwargs: [{
            "id": 7,
            "exec_date": _BASE,
            "stock_code": "600000",
            "action": "buy",
            "timeframe": "T+1",
        }],
    )
    monkeypatch.setattr(
        tc, "_load",
        lambda: (_ for _ in ()).throw(tc.CalendarUnavailable("no calendar")),
    )
    monkeypatch.setattr(
        evaluator, "_bump_eval_failure",
        lambda root_id: pytest.fail("calendar failure must not increment market failures"),
    )

    stats = evaluator.evaluate_pending()
    assert stats["calendar_unavailable"] == 1
    assert stats["errors"] == 0
    assert stats["evaluated"] == 0
