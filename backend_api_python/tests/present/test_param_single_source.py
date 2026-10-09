"""test_param_single_source.py — 参数单一事实源门禁（目标态 §2 / §3.1 / §6.3）。

背景（2026-10-09 终态② / B-D3 → D4）：
  同一个参数曾同时写在 **3 处**（yaml `params` / config.json `strategies.<key>.params`
  / `.py` 模块常量），改一处要同步另两处（§6.3 明令禁止的"改一边要同步另一边"）。
  更危险的是：改 config 那一份对日线策略的判定/出场**不生效**（静默陷阱）。

  权威已定为 **策略宏 `<key>.yaml` 的 `params` 段** —— 判定（`spec.params`）、出场
  （`exit_modes` 收 `spec.params`）、生产扫描（`scan_signals → StrategyBase.params`）与
  回测/参数网格（`params_override`）**全部读同一份**。

  · B-D3（2026-10-09）：日线策略的 config params 清空。
  · D4  （2026-10-09）：**盘中策略（knife/tail）的 config params 亦清空** ——
    `strategies.params_override` 改为 yaml 优先（config 仅作无 yaml 遗留策略兜底），
    故盘中「双源」消除。yaml params 已补齐 `stop_pct`/`hold_days`。

验四件事：
  1. 有 yaml 的策略 config.params 必须为空（防回退双写）；
  2. config.params 只允许出现在**无 yaml** 的遗留策略上（防无意识新增双源）；
  3. `params_override(key)` == yaml params（单一事实源）；
  4. 生产路径 `self.params()` 取到的参数 == yaml（含盘中策略的 stop_pct/hold_days）。
"""

from __future__ import annotations

import json
import os

from app.market_cn.auto import strategies as reg

_STRAT_DIR = os.path.dirname(os.path.abspath(reg.__file__))
_CONFIG = os.path.normpath(os.path.join(_STRAT_DIR, "..", "config.json"))
_YAML_DIR = _STRAT_DIR


def _config() -> dict:
    with open(_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _cfg_params(key: str) -> dict:
    v = (_config().get("strategies", {}).get(key) or {}).get("params")
    return v if isinstance(v, dict) else {}


def _yaml_params(key: str) -> dict:
    import yaml
    path = os.path.join(_YAML_DIR, f"{key}.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    v = doc.get("params")
    return v if isinstance(v, dict) else {}


def _keys():
    reg.autodiscover()
    return sorted(reg.all_strategies())


def _yaml_keys():
    return [k for k in _keys() if os.path.exists(os.path.join(_YAML_DIR, f"{k}.yaml"))]


def test_yaml_strategies_have_no_config_params():
    """有 yaml 宏的策略参数事实源 = yaml ⇒ config.params 必须为空（否则又是双源/静默陷阱）。"""
    keys = _yaml_keys()
    assert keys, "无 yaml 策略？发现逻辑失效 = 假绿"
    offenders = {k: _cfg_params(k) for k in keys if _cfg_params(k)}
    assert not offenders, (
        "有 yaml 的策略在 config.json 仍写 params（判定/出场/扫描读 yaml，改这里不生效=静默陷阱）: %s"
        % offenders)


def test_config_params_only_for_no_yaml_legacy():
    """config.params 只许出现在**无 yaml** 的策略上（防无意识新增双源）。

    2026-10-09 P6-6/7 退役 v1/relay3/lead_chase 后，config 已无任何 params（全走 yaml）；
    本测试守住「不得回退到 config 写 params」。
    """
    cfg = _config()
    with_params = {k for k, v in cfg.get("strategies", {}).items()
                   if isinstance(v, dict) and v.get("params")}
    for k in with_params:
        assert not os.path.exists(os.path.join(_YAML_DIR, f"{k}.yaml")), (
            "%s 既有 yaml 又在 config 写 params = 双源（参数应只写进其 yaml params）" % k)


def test_params_override_is_yaml_single_source():
    """`params_override(key)`（生产扫描/回测/网格统一入口）== yaml params（无 yaml 回退 config）。"""
    for k in _keys():
        yp = _yaml_params(k)
        got = reg.params_override(k)
        if yp:
            assert got == yp, (
                "%s: params_override 未单源到 yaml\n   yaml=%s\n   got =%s" % (k, yp, got))
        else:
            assert got == _cfg_params(k), "%s: 无 yaml 时未回退 config params" % k


def test_production_params_equals_yaml():
    """生产路径 `self.params()`（scan_signals/entry_decision 消费）== yaml params。

    含盘中策略：其 `self.params()["stop_pct"]`/`["hold_days"]` 是**下标访问**，
    必须能在合并结果里取到（yaml 未覆盖的键由 .py default_params 兜底）。
    """
    checked = 0
    for k in _yaml_keys():
        yp = _yaml_params(k)
        if not yp:
            continue
        s = reg.get_strategy(k)
        p = s.params()
        for key, val in yp.items():
            assert p.get(key) == val, (
                "%s: self.params()[%r]=%r != yaml %r（生产判定会与宏分叉）"
                % (k, key, p.get(key), val))
        checked += 1
    assert checked >= 4, "被检 yaml 策略过少（%d）= 假绿" % checked


def test_intraday_subscript_params_present():
    """盘中策略生产路径的**下标访问键**必须在 params() 里（换源不得 KeyError）。"""
    reg.autodiscover()
    for k in ("knife_catch", "tail_oversold"):
        s = reg.get_strategy(k)
        assert s is not None, "%s 未注册" % k
        p = s.params()
        for key in ("stop_pct", "hold_days"):
            assert key in p, "%s: params() 缺下标键 %r（换 yaml 源会 KeyError）" % (k, key)
        # 值须与 yaml 同源（yaml 已补齐这两个键）
        yp = _yaml_params(k)
        assert p["stop_pct"] == yp.get("stop_pct"), (
            "%s: stop_pct 未取自 yaml (%r != %r)" % (k, p["stop_pct"], yp.get("stop_pct")))


def test_run_meta_effective_params_matches_yaml():
    """回测元信息记录的是**实际生效**参数（yaml），不是 py 常量（溯源不得失真）。"""
    from app.market_cn.auto.core.backtest import _run_meta
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    reg.autodiscover()
    checked = 0
    for k in _yaml_keys():
        s = reg.get_strategy(k)
        yp = dict(load_strategy(k).params)
        if not yp:
            continue
        got = _run_meta(s, 30, None, None).get("effective_params")
        assert got == yp, (
            "%s: _run_meta.effective_params 与 yaml params 不一致（元信息失真）\n"
            "   yaml=%s\n   meta=%s" % (k, yp, got))
        checked += 1
    assert checked >= 4, "被检策略过少（%d）= 假绿" % checked
