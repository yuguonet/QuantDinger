"""LLM capture should record per-query TokenStats deltas when available."""
from types import SimpleNamespace

from app.agent import llm_capture


def test_token_snapshot_and_delta():
    before = llm_capture._token_snapshot(SimpleNamespace(token_stats=SimpleNamespace(
        input_tokens=100, output_tokens=20, cache_read_tokens=30,
        cache_creation_tokens=4,
    )))
    after = llm_capture._token_snapshot(SimpleNamespace(token_stats=SimpleNamespace(
        input_tokens=160, output_tokens=37, cache_read_tokens=45,
        cache_creation_tokens=9,
    )))

    assert llm_capture._token_delta(before, after) == {
        "input_tokens": 60,
        "output_tokens": 17,
        "cache_read_tokens": 15,
        "cache_creation_tokens": 5,
    }


def test_token_delta_keeps_unavailable_counters_unknown():
    before = llm_capture._token_snapshot(SimpleNamespace(token_stats=SimpleNamespace(
        input_tokens=10,
    )))
    after = llm_capture._token_snapshot(SimpleNamespace(token_stats=SimpleNamespace(
        input_tokens=15,
    )))

    assert llm_capture._token_delta(before, after) == {
        "input_tokens": 5,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
    }
    assert llm_capture._token_delta(None, after) is None
