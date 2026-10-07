# tests/present/test_m1_sampler.py
"""M1 独立采样器门禁 (P5 前置, 2026-10-07)。

锁定三条不变量:
  1. **自跑取 trace**: 判定驱动走 `scan_days` 折叠内核 (不产门级 trace) ⇒ 采样器
     `observe` 自跑 `scan_signals(probe=)` 取 trace 并组装 sample。
  2. **零开销**: 未启用 (live_probe=false) / 策略不接受 probe 形参 ⇒ 全路径 no-op。
  3. **噪声不采**: 判定无 trace 且无信号 ⇒ 不产 sample。

不依赖 DB: stub 策略 + stub 预过滤 + tmp 落盘目录。
"""
from dataclasses import dataclass, field

import pytest

from app.market_cn.auto import sampler as sampler_mod
from app.market_cn.auto import strategies as strat_reg


@dataclass
class _Sig:
    code: str
    time: str
    score: int = 1
    price: float = 1.0
    label: str = "x"
    extra: dict = field(default_factory=dict)


def _bars(n=6):
    return [{"time": f"2026-01-0{i + 1}", "open": 1.0 + i, "high": 1.2 + i,
             "low": 0.9 + i, "close": 1.1 + i, "volume": 1000.0 + i}
            for i in range(n)]


def _pass_all(sigs, bars, code, code_info, strat):
    """stub 预过滤: 全通过 (kept == sigs, u_fails=None)。"""
    return list(sigs), None


class _FakeStrat:
    """最小策略: scan_signals 声明 probe 形参并发 trace; _probe_day 只记参数。

    用 stub `_probe_day` 而非 StrategyBase._probe_day —— 后者经 sample_feats
    需要真实 bars/indicators, 与「采样器分流逻辑」无关 (本测试只验分流)。
    """
    PROBE_STAGE_RANK = {"a": 1, "b": 2}

    def __init__(self):
        self.calls = []

    def scan_signals(self, bars, code, *, probe=None, **params):
        if probe is not None:
            probe.trace("a", code=code, v=1)
            probe.trace("b", code=code, v=2)
        return [_Sig(code=code, time=bars[-1]["time"])]

    def _probe_day(self, probe, day_tr, bars, i, code, stock_info,
                   stage=None, sig=None, u_fails=None, extra=None):
        self.calls.append({
            "stage": stage,
            "u_fails": list(u_fails) if u_fails else None,
            "sig_code": (sig or {}).get("code") if sig else None,
            "trace": [dict(t) for t in (day_tr.items if day_tr else [])],
        })
        probe.sample(code=code, stage=stage)


class _NoProbeStrat:
    """scan_signals **不声明** probe 形参 (relay3 现状) ⇒ 不采集。"""

    def scan_signals(self, bars, code, **params):
        return [_Sig(code=code, time=bars[-1]["time"])]


class _QuietStrat:
    """判定落点为空 (无 trace 无信号) ⇒ 不产 sample。"""

    def scan_signals(self, bars, code, *, probe=None, **params):
        return []

    def _probe_day(self, *a, **k):
        raise AssertionError("噪声日不应调 _probe_day")


def _mk(active, tmp_path, monkeypatch, *, enabled=True):
    monkeypatch.setattr(strat_reg, "live_probe_enabled", lambda: enabled)
    return sampler_mod.LiveSampler(active, lambda k: {},
                                   out_dir=str(tmp_path), prefilter=_pass_all)


def test_observe_self_runs_and_emits_sample(tmp_path, monkeypatch):
    """核心不变量: `observe` 自跑 scan_signals(probe=) 取 trace 并产出一行 sample。"""
    st = _FakeStrat()
    s = _mk({"fake": st}, tmp_path, monkeypatch)
    s.observe("fake", st, "000001", _bars(), {})
    s.close()
    assert st.calls, "自跑应产 sample"
    assert s.probes["fake"].counts.get("sample") == 1
    # trace 由自跑的 scan_signals 写出 (折叠判定不产 trace ⇒ 全靠自跑)
    assert [t["stage"] for t in st.calls[0]["trace"]] == ["a", "b"]


def test_stage_signal_when_prefilter_passes(tmp_path, monkeypatch):
    st = _FakeStrat()
    s = _mk({"fake": st}, tmp_path, monkeypatch)
    s.observe("fake", st, "000001", _bars(), {})
    s.close()
    assert st.calls[0]["stage"] == "signal"
    assert st.calls[0]["u_fails"] is None


def test_stage_prefilter_when_rejected(tmp_path, monkeypatch):
    """U1~U4 拒 (kept 空) ⇒ stage=prefilter 且带 u_fails (归因)。"""
    monkeypatch.setattr(strat_reg, "live_probe_enabled", lambda: True)
    st = _FakeStrat()
    s = sampler_mod.LiveSampler(
        {"fake": st}, lambda k: {}, out_dir=str(tmp_path),
        prefilter=lambda sigs, bars, code, info, strat: ([], ["u1_fail"]))
    s.observe("fake", st, "000001", _bars(), {})
    s.close()
    assert st.calls[0]["stage"] == "prefilter"
    assert st.calls[0]["u_fails"] == ["u1_fail"]


def test_disabled_is_zero_cost(tmp_path, monkeypatch):
    st = _FakeStrat()
    s = _mk({"fake": st}, tmp_path, monkeypatch, enabled=False)
    assert s.enabled is False
    assert s.wants("fake") is False
    s.observe("fake", st, "000001", _bars(), {})
    assert st.calls == []
    assert s.probes == {}


def test_strategy_without_probe_param_not_sampled(tmp_path, monkeypatch):
    st = _NoProbeStrat()
    s = _mk({"np": st}, tmp_path, monkeypatch)
    assert s.enabled is True
    assert s.wants("np") is False          # 不接受 probe ⇒ 不采 (判定行为零变化)
    s.observe("np", st, "000001", _bars(), {})
    s.close()


def test_quiet_day_no_sample(tmp_path, monkeypatch):
    st = _QuietStrat()
    s = _mk({"q": st}, tmp_path, monkeypatch)
    s.observe("q", st, "000001", _bars(), {})   # 不应抛
    s.close()


def test_self_run_exception_does_not_raise(tmp_path, monkeypatch):
    """自跑异常 (策略 bug) ⇒ 该票不采, 不影响判定 (不抛)。"""
    class _Boom:
        def scan_signals(self, bars, code, *, probe=None, **params):
            raise RuntimeError("boom")

    st = _Boom()
    s = _mk({"b": st}, tmp_path, monkeypatch)
    s.observe("b", st, "000001", _bars(), {})   # 不抛
    s.close()
