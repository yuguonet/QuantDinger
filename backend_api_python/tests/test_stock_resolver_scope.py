"""Stock entity expansion must not invent an observation horizon."""

from app.agent.resolvers.stock import _expand_stock_query


_MAOTAI = [{"code": "600519", "name": "贵州茅台", "type": "stock"}]


def test_stock_expansion_does_not_add_default_horizon():
    expanded = _expand_stock_query("查询贵州茅台今日行情", _MAOTAI)

    assert "周期：" not in expanded
    assert "T+3" not in expanded
    assert "深度：" not in expanded


def test_stock_expansion_preserves_explicit_horizon_without_adding_one():
    expanded = _expand_stock_query("查询贵州茅台T+3涨跌幅", _MAOTAI)

    assert expanded.count("T+3") == 1
    assert "周期：" not in expanded


def test_negative_horizon_mention_is_not_turned_into_a_request():
    expanded = _expand_stock_query("查询贵州茅台今日行情，不要计算T+3", _MAOTAI)

    assert expanded.count("T+3") == 1
