# -*- coding: utf-8 -*-
"""provider_index 指数实时行情（index_quote）单元测试（零真实联网/连库）。

验证腾讯 / mootdx 的 fetch_index_quote 在 code= 与 codes=[...] 双入口下都正确：
  · 腾讯：qt.gtimg.cn 单 HTTP 多股，解析字段（含「手→股」换算）对齐；
  · mootdx：cli.quotes 逐码，code=/codes= 归一后都能返回。

mock 掉 urllib / get_client，不触真实网络与 TDX 服务器（2026-10-27 接通批量入口配套）。
"""
import urllib.request

import pytest


# ── 腾讯（mock urllib.request.urlopen）──────────────────────────

def _fake_urlopen(text):
    """返回一个 urlopen 替身，把请求 URL 记进 captured['url']，返回 GBK 文本。"""
    captured = {}

    class _Resp:
        def __init__(self, b):
            self._b = b
        def read(self):
            return self._b

    def _open(req, timeout=10):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return _Resp(text.encode("gbk"))

    _open.captured = captured
    return _open


def _build_gbk_lines(codes_with_price):
    """构造腾讯 qt.gtimg.cn 返回文本（GBK）。codes_with_price: [(code, price)]。"""
    lines = []
    for code, price in codes_with_price:
        prefix = "sh" if code[:3] in ("000", "88", "99") else "sz"
        parts = [""] * 50
        parts[1] = "指数名"
        parts[3] = str(price)      # 现价
        parts[4] = "100.0"         # 昨收
        parts[5] = "101.0"         # 开盘
        parts[33] = "103.0"        # 最高
        parts[34] = "99.0"         # 最低
        parts[36] = "123456"       # 成交量（手）
        parts[37] = "500000"       # 成交额
        inner = "~".join(parts)
        lines.append('v_%s%s="%s";' % (prefix, code, inner))
    return "\n".join(lines)


def test_tencent_index_quote_codes_batch(monkeypatch):
    """codes=[...] 单 HTTP 多股：返回条数=码数，URL 含全部逗号拼码，单位口径正确。"""
    opener = _fake_urlopen(_build_gbk_lines([("000001", 3200.5), ("399006", 2100.0)]))
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    from app.data_sources.provider_index.tencent import fetch_index_quote
    out = fetch_index_quote(codes=["000001", "399006"])
    rows = out.get("rows") or []
    assert len(rows) == 2
    assert {r["code"] for r in rows} == {"000001", "399006"}
    r0 = next(r for r in rows if r["code"] == "000001")
    # 2026-10-28 实测标定（sh000001: v36=417789532 ≈ 4.18 亿手、v37=67241440 万元）：
    # 腾讯 v[36] 本身即"手"，**不再 ×100 转股**；v[37] 为"万元"，须 ×1e4 得元。
    assert r0["volume"] == pytest.approx(123456.0)          # 手（原值透传）
    assert r0["amount"] == pytest.approx(500000 * 10000.0)  # 万元 → 元
    assert "sh000001" in opener.captured["url"] and "sz399006" in opener.captured["url"]


def test_tencent_index_quote_code_single(monkeypatch):
    """code= 单码归一为 codes=[code]：返回 1 条。"""
    opener = _fake_urlopen(_build_gbk_lines([("000300", 4000.0)]))
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    from app.data_sources.provider_index.tencent import fetch_index_quote
    out = fetch_index_quote(code="000300")
    rows = out.get("rows") or []
    assert len(rows) == 1 and rows[0]["code"] == "000300"


def test_tencent_index_quote_default_list(monkeypatch):
    """不传参返回默认主要指数（>=8，腾讯硬编码默认列表）。"""
    opener = _fake_urlopen(_build_gbk_lines(
        [("000001", 1), ("399001", 1), ("399006", 1), ("000300", 1),
         ("000905", 1), ("000688", 1), ("399005", 1), ("000016", 1)]))
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    from app.data_sources.provider_index.tencent import fetch_index_quote
    out = fetch_index_quote()
    assert len(out.get("rows") or []) >= 8


# ── mootdx（mock _get_client）──────────────────────────────────

def _fake_client(rows_map):
    class _Row:
        def __init__(self, d):
            self._d = d
        def get(self, k, d=None):
            return self._d.get(k, d)
    class _Iloc:
        def __init__(self, d):
            self._d = d
        def __getitem__(self, i):
            return _Row(self._d)
    class _Df:
        def __init__(self, d):
            self._d = d
        @property
        def empty(self):
            return False
        @property
        def iloc(self):
            return _Iloc(self._d)
    class _Cli:
        def quotes(self, symbol):
            return _Df(rows_map[symbol])
    return _Cli()


def test_mootdx_index_quote_codes(monkeypatch):
    monkeypatch.setattr(
        "app.data_sources.provider_index.mootdx._get_client",
        lambda: _fake_client({
            "000001": {"name": "上证", "price": 3200.0, "last_close": 3190.0,
                       "open": 3195.0, "high": 3210.0, "low": 3180.0, "vol": 100.0},
            "399006": {"name": "创业", "price": 2100.0, "last_close": 2090.0,
                       "open": 2095.0, "high": 2110.0, "low": 2080.0, "vol": 50.0},
        }))
    from app.data_sources.provider_index.mootdx import fetch_index_quote
    out = fetch_index_quote(codes=["000001", "399006"])
    rows = out.get("rows") or []
    assert len(rows) == 2 and {r["code"] for r in rows} == {"000001", "399006"}


def test_mootdx_index_quote_code_single(monkeypatch):
    monkeypatch.setattr(
        "app.data_sources.provider_index.mootdx._get_client",
        lambda: _fake_client({
            "000300": {"name": "沪深300", "price": 4000.0, "last_close": 3990.0,
                       "open": 3995.0, "high": 4010.0, "low": 3980.0, "vol": 80.0},
        }))
    from app.data_sources.provider_index.mootdx import fetch_index_quote
    out = fetch_index_quote(code="000300")
    rows = out.get("rows") or []
    assert len(rows) == 1 and rows[0]["code"] == "000300"


def test_mootdx_index_quote_client_unavailable(monkeypatch):
    monkeypatch.setattr(
        "app.data_sources.provider_index.mootdx._get_client", lambda: None)
    from app.data_sources.provider_index.mootdx import fetch_index_quote
    out = fetch_index_quote(codes=["000001"])
    assert out.get("rows") == [] and "mootdx 不可用" in (out.get("error") or "")
