"""get_realtime_quote volume_unit 守卫（2026-10-17 审计修复）。

背景：llm_probe 复跑轮发现最终报告把成交量单位写成"万手"——工具返回裸
volume 数字、无单位标注，模型只能猜。六个 provider 出口实际已各自归一化为
**股**（腾讯/搜狐/tdx 原始为手已×100；新浪/同花顺/百度原生为股），
coordinate_batch_quotes 不做单位变换。故工具层补 volume_unit="股" 恒标注。

本测试离线 mock 数据源层（_get_ds / _tencent_quote_raw），不发任何网络请求。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.tools.finance import data_tools as dt  # noqa: E402


class _FakeDS:
    """只实现 get_realtime_quote 用到的 get_tickers。"""

    def get_tickers(self, symbols):
        # 模拟 provider 出口口径：volume 已是"股"（腾讯源 vol*100 后的形态）
        return [{"symbol": s, "name": "贵州茅台", "last": 1329.0,
                 "changePercent": 1.23, "volume": 2512345.0,
                 "open": 1310.0, "high": 1335.0, "low": 1305.0,
                 "previousClose": 1312.0, "time": "15:00:00"}
                for s in symbols]


def _patched(codes="600519"):
    orig_ds, orig_qq = dt._get_ds, dt._tencent_quote_raw
    dt._get_ds = lambda market: _FakeDS()
    dt._tencent_quote_raw = lambda cs: {}
    try:
        return dt.get_realtime_quote(codes)
    finally:
        dt._get_ds, dt._tencent_quote_raw = orig_ds, orig_qq


def test_volume_unit_annotated_on_success():
    r = _patched()
    assert r["error"] is None, r
    q = r["data"]["600519"]
    assert q["volume_unit"] == "股", q
    # 单位键不得覆盖/篡改 volume 数值本身
    assert q["volume"] == 2512345.0, q


def test_volume_unit_survives_multi_and_error_paths():
    r = _patched("600519,000001")
    assert r["count"] == 2
    for code, q in r["data"].items():
        assert q["volume_unit"] == "股", (code, q)
    # 取不到行情的 code 不带 volume_unit（没有 volume 可标注，不造假单位）
    bad = dt.get_realtime_quote("")
    assert bad["error"], bad


def test_provider_self_unit_key_cannot_override_tool_contract():
    """provider 若自带 volume_unit（源漂移），工具层口径必须赢（放 **t 之后）。"""
    class _DriftDS(_FakeDS):
        def get_tickers(self, symbols):
            rows = super().get_tickers(symbols)
            for row in rows:
                row["volume_unit"] = "手"       # 模拟上游漂移
            return rows

    orig_ds, orig_qq = dt._get_ds, dt._tencent_quote_raw
    dt._get_ds = lambda market: _DriftDS()
    dt._tencent_quote_raw = lambda cs: {}
    try:
        r = dt.get_realtime_quote("600519")
    finally:
        dt._get_ds, dt._tencent_quote_raw = orig_ds, orig_qq
    assert r["data"]["600519"]["volume_unit"] == "股", r
