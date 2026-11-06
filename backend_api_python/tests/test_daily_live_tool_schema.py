"""Verify daily_live exposes parameter types compatible with its runtime contract."""

from app.agent.tools._schema_gen import func_to_openai_schema
from app.agent.tools.finance.data_tools import agent_get_kline
from app.market_cn.auto.core.data.hub import daily_live


def test_daily_live_days_schema_is_integer():
    schema = func_to_openai_schema(daily_live)["function"]["parameters"]

    assert schema["properties"]["days"]["type"] == "integer"
    assert "days" not in schema.get("required", [])


def test_agent_get_kline_documents_days_as_bar_count():
    schema = func_to_openai_schema(agent_get_kline)["function"]["parameters"]

    assert "返回的 K 线根数" in schema["properties"]["days"]["description"]
    assert "交易日根数" in schema["properties"]["days"]["description"]
