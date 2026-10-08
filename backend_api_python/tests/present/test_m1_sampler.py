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
        # 分发契约（与真实策略同款）: ctx["_trace"].note 优先 / probe.trace 兼容
        sink = (params.get("ctx") or {}).get("_trace")

        def _emit(stage, **kw):
            if sink is not None:
                sink.note(stage, **kw)
            elif probe is not None:
                probe.trace(stage, **kw)
        _emit("a", code=code, v=1)
        _emit("b", code=code, v=2)
        return [_Sig(code=code, time=bars[-1]["time"])]

    def _probe_day(self, probe, day_tr, bars, i, code, stock_info,
                   stage=None, sig=None, u_fails=None, extra=None):
        # P3-④ 后 observe 不再调策略方法（组装归 probe.build_day_sample）；
        # 保留本 stub 仅当旧接口兼容层测试用。
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
    """核心不变量: `observe` 自跑 scan_signals 取 trace 并产出一行 sample。

    P3-④ 后组装走 probe.build_day_sample（输出直达 probe 对象）⇒ 断言捕获 probe。
    """
    st = _FakeStrat()
    s = _mk({"fake": st}, tmp_path, monkeypatch)
    cap = s.probes["fake"] = _CaptureProbe()
    s.observe("fake", st, "000001", _bars(), {})
    s.close()
    assert len(cap.samples) == 1, "自跑应产 sample"
    assert cap.samples[0]["code"] == "000001"
    # trace 由自跑的 scan_signals 写出 (折叠判定不产 trace ⇒ 全靠自跑)
    assert [t["stage"] for t in cap.samples[0]["rule_trace"]] == ["a", "b"]


def test_stage_signal_when_prefilter_passes(tmp_path, monkeypatch):
    st = _FakeStrat()
    s = _mk({"fake": st}, tmp_path, monkeypatch)
    cap = s.probes["fake"] = _CaptureProbe()
    s.observe("fake", st, "000001", _bars(), {})
    s.close()
    assert cap.samples[0]["stage"] == "signal"
    assert cap.samples[0].get("u_fails") is None


def test_stage_prefilter_when_rejected(tmp_path, monkeypatch):
    """U1~U4 拒 (kept 空) ⇒ stage=prefilter 且带 u_fails (归因)。"""
    monkeypatch.setattr(strat_reg, "live_probe_enabled", lambda: True)
    st = _FakeStrat()
    s = sampler_mod.LiveSampler(
        {"fake": st}, lambda k: {}, out_dir=str(tmp_path),
        prefilter=lambda sigs, bars, code, info, strat: ([], ["u1_fail"]))
    cap = s.probes["fake"] = _CaptureProbe()
    s.observe("fake", st, "000001", _bars(), {})
    s.close()
    assert cap.samples[0]["stage"] == "prefilter"
    assert cap.samples[0]["u_fails"] == ["u1_fail"]


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


# ================================================================
# 影子对拍（P3 迁移开关，2026-10-08）：ctx["_trace"] 路径 == probe 路径
# ================================================================
class _CaptureProbe:
    def __init__(self):
        self.samples = []

    def sample(self, **kw):
        self.samples.append(kw)


@pytest.mark.parametrize("spec_name,skey", [
    ("break_real_000032", "break"),
    ("dragon_crafted", "dragon_callback"),
])
def test_m1_shadow_trace_path_equals_probe_path(monkeypatch, spec_name, skey):
    """影子对拍：ctx["_trace"] 路径与 probe 路径产出逐字节同源（P3 迁移开关）。

    覆盖：sigs/kept/u_fails/rule_trace + 真 `_probe_day` 组装的整 sample。
    逐日扫描（observe 是逐日采样），双证据防假绿（≥1 信号日 + ≥1 落选轨迹日）。
    全绿后 observe 方可切到影子路径，probe 面（P3-④）才谈得上退役。
    g56/relay3 的 scan_signals 无 probe 形参（M1 天然不采）；knife/tail 无 ctx
    供给不产出 —— 均不在对拍名单，分发器已统一（零行为差异由全量测试背书）。
    """
    import json as _json
    import os as _os
    from dataclasses import asdict

    from app.market_cn.auto import strategies as reg
    from tests.golden.inputs import INPUT_SETS

    reg.autodiscover()
    monkeypatch.setattr("app.market_cn.auto.strategies.live_probe_enabled",
                        lambda: True)
    spec = next(s for s in INPUT_SETS if s["name"] == spec_name)
    bars, code = spec["build"](), spec["code"]
    # observe 逐日采样（bars 截到采样日）⇒ 按基线信号日截断，信号在末日
    _base = _json.load(open(_os.path.join(
        _os.path.dirname(__file__), "..", "golden", "baselines",
        spec_name + ".json"), encoding="utf-8"))
    _d0 = _base["trades"][0]["d0_date"]
    _cut = next(i for i, b in enumerate(bars) if str(b["time"])[:10] == _d0)
    bars = bars[:_cut + 1]
    strat = reg.get_strategy(skey)
    smp = sampler_mod.LiveSampler({skey: strat}, lambda k: {})
    si = {code: spec.get("stock_info") or {}}

    saw_signal = saw_trace = 0
    for cut in range(30, len(bars) + 1):
        day_bars = bars[:cut]
        sigs0, kept0, uf0, tr0 = smp._self_run(skey, strat, code, day_bars, si)
        sigs1, kept1, uf1, tr1 = smp._self_run_via_trace(
            skey, strat, code, day_bars, si)
        assert [asdict(s) for s in (sigs0 or [])] == [asdict(s) for s in (sigs1 or [])]
        assert [asdict(s) for s in (kept0 or [])] == [asdict(s) for s in (kept1 or [])]
        assert list(uf0 or []) == list(uf1 or [])
        assert tr0.items == tr1.items, (cut, tr0.items, tr1.items)
        if sigs0:
            saw_signal += 1
        if tr0.items:
            saw_trace += 1
        if sigs0 or tr0.items:
            def _assemble(day_tr, sigs, kept, u_fails):
                if sigs and not kept:
                    stage, _uf = "prefilter", u_fails
                elif kept:
                    stage, _uf = "signal", None
                else:
                    stage, _uf = None, None
                cap = _CaptureProbe()
                sig = asdict(kept[0] if kept else sigs[0]) if sigs else None
                sampler_mod.build_day_sample(
                    cap, day_tr, day_bars, len(day_bars) - 1, code,
                    si.get(code), strategy=strat, stage=stage,
                    u_fails=_uf if sigs else None, sig=sig)
                return cap.samples
            assert _assemble(tr0, sigs0, kept0, uf0) == \
                   _assemble(tr1, sigs1, kept1, uf1), cut
    assert saw_signal >= 1, "%s 全程无信号 —— 对拍无证据（假绿防线）" % spec_name
    assert saw_trace >= 1, "%s 全程无落选轨迹 —— trace 同源无证据（假绿防线）" % spec_name
