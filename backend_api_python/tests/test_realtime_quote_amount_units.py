"""Verify realtime quote providers return turnover in yuan when available."""

from types import SimpleNamespace

from app.data_sources.provider import sina, tencent


class _NoWait:
    def wait(self):
        pass


class _Response:
    def __init__(self, text):
        self.text = text
        self.encoding = None


def test_tencent_quote_amount_is_converted_from_wan(monkeypatch):
    parts = [""] * 38
    parts[1] = "贵州茅台"
    parts[2] = "sh600519"
    parts[3] = "1500"
    parts[4] = "1490"
    parts[5] = "1495"
    parts[6] = "1000"
    parts[30] = "20261104103000"
    parts[33] = "1510"
    parts[34] = "1480"
    parts[37] = "123456"
    monkeypatch.setattr(tencent, "get_tencent_limiter", lambda: _NoWait())
    monkeypatch.setattr(tencent.requests, "get", lambda *args, **kwargs: _Response(
        'v_sh600519="' + "~".join(parts) + '"'
    ))

    quote = tencent.TencentDataSource().fetch_ticker("600519")

    assert quote["amount"] == 1_234_560_000
    assert quote["amount_unit"] == "元"


def test_sina_quote_keeps_provider_amount_in_yuan(monkeypatch):
    parts = [""] * 32
    parts[0] = "贵州茅台"
    parts[1:6] = ["1495", "1490", "1500", "1510", "1480"]
    parts[8] = "100000"
    parts[9] = "1234567890"
    parts[30:32] = ["2026-11-04", "10:30:00"]
    response = _Response('var hq_str_sh600519="' + ",".join(parts) + '";')
    monkeypatch.setattr(sina, "_sina_quote_limiter", _NoWait())
    monkeypatch.setattr(sina, "get_shared_session", lambda: SimpleNamespace(
        get=lambda *args, **kwargs: response
    ))

    quote = sina.SinaDataSource().fetch_ticker("600519")

    assert quote["amount"] == 1_234_567_890
    assert quote["amount_unit"] == "元"
