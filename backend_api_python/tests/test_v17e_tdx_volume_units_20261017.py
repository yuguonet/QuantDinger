"""tdx_ex 成交量单位不变量守卫（2026-10-17）。

**背景（一次真实误报）**：2026-10-17 审计收尾时，我肉眼扫到 tdx_ex 的行情路径
`vol*100` 而 K 线路径没有，就外发了"疑似单位不一致隐患"。核实后发现是误报：
pytdx 两个接口的 vol 单位本就不同——行情（get_security_quotes）返回**手**，
K 线（get_security_bars）返回**股**。tdx_ex.py 模块 docstring「单位注意（重要）」
段早已记载此不对称，代码处理是正确的。

**实证对账**（2026-10-17，600519 日线，tdx vs tencent 已归一化口径）：
  2026-10-12  tdx_vol=3155604.0  tx_vol=3155600.0  ratio=1.0000
  2026-10-13  tdx_vol=2657276.0  tx_vol=2657300.0  ratio=1.0000
  2026-10-14  tdx_vol=2377273.0  tx_vol=2377300.0  ratio=1.0000
  2026-10-15  tdx_vol=5767722.0  tx_vol=5767700.0  ratio=1.0000
  2026-10-16  tdx_vol=3850175.0  tx_vol=3850200.0  ratio=1.0000
（差异仅百位取整：腾讯上游按百位舍入，tdx 保留原始精度）

**为什么值得一个测试**：这个点肉眼扫描会**恒定误报**（`*100` 的有无太扎眼），
而误"修"代价极高——tdx 是主力 K 线源之一，若给 K 线补 ×100 会全线数据失真。
本测试把「行情 ×100 / K 线不乘」固化为不变量，离线 mock pytdx 连接。

复现实证：tests/../ 见 docstring；或手动跑
  get_providers(capability="kline", market="CNStock") 取 tdx_ex 与 tencent
  对 fetch_kline("600519", "1D", count=5) 的 volume 逐 bar 相比。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data_sources.provider.tdx_ex import TdxExDataSource  # noqa: E402


class _FakeTdxApi:
    """只实现被测路径用到的两个 pytdx 接口。"""

    def __init__(self, bars=None, quotes=None):
        self._bars = bars or []
        self._quotes = quotes or []

    def get_security_bars(self, cat, mkt, sym, start, count):
        return self._bars

    def get_security_quotes(self, pairs):
        return self._quotes


def _patch_conn(monkeypatch, api):
    monkeypatch.setattr(TdxExDataSource, "_get_conn",
                        classmethod(lambda cls: (api, "std")))


# pytdx get_security_bars 的行：vol 原生为「股」；datetime 为 8 位数字串
# （"20261016"，走 tdx_ex 的 len==8 分支——带横线的 "2026-10-16" 会掉进
# fromtimestamp 的 except 被 continue 丢弃，2026-10-17 首版测试踩过）
_FAKE_BARS = [
    {"datetime": "20261016", "open": 1310.0, "high": 1335.0,
     "low": 1305.0, "close": 1329.0, "vol": 3850175},
    {"datetime": "20261015", "open": 1290.0, "high": 1315.0,
     "low": 1286.0, "close": 1312.97, "vol": 5767722},
]
# pytdx get_security_quotes 的行：vol 为「手」
_FAKE_QUOTES = [
    {"market": 1, "code": "600519", "price": 1329.0, "last_close": 1312.97,
     "open": 1310.0, "high": 1335.0, "low": 1305.0, "vol": 38501},
]


def test_kline_volume_stays_in_shares(monkeypatch):
    """K 线 vol 原生是股 ⇒ 输出必须原样（不得 ×100，也不得 /100）。"""
    _patch_conn(monkeypatch, _FakeTdxApi(bars=_FAKE_BARS))
    bars = TdxExDataSource._fetch_kline_raw("600519", "1D", 10)
    assert bars, bars
    vols = {b["time"]: b["volume"] for b in bars}
    assert vols["2026-10-16"] == 3850175.0, vols
    assert vols["2026-10-15"] == 5767722.0, vols


def test_quote_volume_is_scaled_to_shares(monkeypatch):
    """行情 vol 原生是手 ⇒ 输出必须 ×100 转股（不乘就是 100 倍失真）。"""
    _patch_conn(monkeypatch, _FakeTdxApi(quotes=_FAKE_QUOTES))
    q = TdxExDataSource._fetch_quote_raw("600519")
    assert q and q.get("volume") == 3850100.0, q


def test_docstring_still_documents_the_asymmetry():
    """防"好心统一"：模块 docstring 的单位注意段是不变量的一部分。"""
    import inspect
    doc = inspect.getdoc(TdxExDataSource.__module__ and
                         sys.modules[TdxExDataSource.__module__]) or ""
    assert "单位注意" in doc, "tdx_ex 模块 docstring 的单位注意段被删了？"
    assert "行情和K线的vol单位不同" in doc.replace(" ", ""), (
        "单位不对称的记载被抹掉——删它的人大概率下一步就要'统一'两个路径")
