"""test_enable_switch.py — 策略开关单一事实源门禁（目标态 §3.5「改宏即开关」）。

背景（2026-10-09 终态② / A-D2）：
  开关事实源从 `config.json strategies.<key>.enabled` 迁到**策略宏自身的
  `<key>.yaml` → `meta.enabled`** —— 上线/下线只改策略 yaml 的一行，与
  label/params 的宏内自描述一致，消掉「开关在另一个配置域」的分叉（§2 单一事实源）。

验四件事：
  1. 有 yaml 的策略：`is_enabled` 完全由 `meta.enabled` 决定（且该键必须存在）；
  2. config.json **不再**为有 yaml 的策略写冗余 `enabled`（防回退双写）；
  3. 无 yaml 的策略回退 config.enabled —— 兜底分支显式且在（2026-10-09 P6-6/7 退役
     v1/relay3/lead_chase 后已无此类策略，改以合成 key 覆盖该分支语义）；
  4. **回测/调试入口不得读开关**：§3.5 要求 enabled=false 只挡展示/生产，不挡回测/调试。

易错点：断言必须**非空**（空集对空集恒相等 = 假绿），故每条都带 `checked`/非空断言。
"""

from __future__ import annotations

import inspect
import json
import os

from app.market_cn.auto import registry
from app.market_cn.auto import strategies as reg

_STRAT_DIR = os.path.dirname(os.path.abspath(reg.__file__))
_CONFIG = os.path.normpath(os.path.join(_STRAT_DIR, "..", "config.json"))


def _yaml_keys() -> set:
    return {os.path.splitext(f)[0] for f in os.listdir(_STRAT_DIR) if f.endswith(".yaml")}


def _config() -> dict:
    with open(_CONFIG, encoding="utf-8") as f:
        return json.load(f)


# ================================================================
# 1. 开关事实源 = yaml meta.enabled
# ================================================================
def test_enabled_comes_from_yaml_meta():
    """有 yaml 的策略，is_enabled 必须等于 `meta.enabled`，且该键不得缺省。"""
    reg.autodiscover()
    checked = 0
    for k in sorted(_yaml_keys()):
        s = reg.get_strategy(k)
        if s is None:
            continue
        y = reg._yaml_meta_enabled(k)
        assert y is not None, (
            "%s.yaml 的 meta 缺 `enabled` 键 —— §3.2 门表 schema 要求 meta 自带开关"
            % k)
        assert reg.is_enabled(k) is y, (
            "%s: is_enabled=%s 但 yaml meta.enabled=%s（开关事实源不是 yaml）"
            % (k, reg.is_enabled(k), y))
        checked += 1
    assert checked >= 5, "被检策略过少（%d），疑似发现逻辑失效 = 假绿" % checked


def test_config_has_no_redundant_enabled():
    """config.json 不得为「有 yaml」的策略写 enabled（否则又是双事实源）。"""
    reg.autodiscover()
    ykeys = _yaml_keys()
    cfg = _config()
    offenders = [k for k, v in cfg.get("strategies", {}).items()
                 if isinstance(v, dict) and "enabled" in v and k in ykeys]
    assert not offenders, (
        "config.json 仍为有 yaml 的策略写 enabled（双事实源，违反 §2）: %s" % offenders)


def test_yamlless_legacy_falls_back_to_config():
    """无 yaml 的策略：开关兜底在 config.enabled（分支显式存在）。

    2026-10-09 P6-6/7 退役 v1/relay3/lead_chase 后已无真实「无 yaml 策略」⇒
    改为直接对分支语义做真断言（合成 key）：无 yaml ⇒ 不吃 yaml 开关；config 亦无
    ⇒ 缺省 False（§3.5 安全语义）。若日后新增无 yaml 策略，首句会 FAIL 提醒补齐覆盖。
    """
    reg.autodiscover()
    ykeys = _yaml_keys()
    # ① 现状守护：不应存在无 yaml 的已注册策略（出现即须为其补 yaml 或更新本测试）
    legacy = [k for k in reg.all_strategies() if k not in ykeys]
    assert not legacy, (
        "出现无 yaml 的已注册策略 %s —— 请为其补 yaml 宏或更新本测试的覆盖方式" % legacy)
    # ② 分支语义（真跑、非空断言）：无 yaml ⇒ 无 meta.enabled；无 config 亦 ⇒ 缺省 False
    assert reg._yaml_meta_enabled("__no_yaml_key__") is None, \
        "无 yaml 的 key 不应有 meta.enabled"
    assert reg.is_enabled("__no_yaml_key__") is False, \
        "无 yaml 且无 config.enabled 的 key 必须缺省 False（§3.5 安全语义）"


def test_enabled_keys_consistent_with_is_enabled():
    """registry.enabled_keys() 必须与逐 key is_enabled 一致（展示层过滤的事实源）。"""
    reg.autodiscover()
    expect = {k for k in reg.all_strategies() if reg.is_enabled(k)}
    got = set(registry.enabled_keys())
    assert got == expect, "enabled_keys() 与 is_enabled 不一致: 多=%s 少=%s" % (
        sorted(got - expect), sorted(expect - got))
    assert got, "enabled 策略集为空 —— 全下线属事故，不该静默通过"


# ================================================================
# 2. §3.5：enabled=false 不挡回测 / 调试
# ================================================================
def test_backtest_and_debug_entry_ignore_enable_switch():
    """回测入口不得读开关 —— 否则 enabled=false 会挡掉回测，违反 §3.5。"""
    from app.market_cn.auto.core import backtest as bt
    from app.market_cn.auto.strategies.base import StrategyBase
    fns = (StrategyBase.backtest_stock, bt.run_all)
    for fn in fns:
        src = inspect.getsource(fn)
        assert "is_enabled" not in src, (
            "%s 读了 is_enabled —— 会让 enabled=false 挡住回测/调试（§3.5 明令回测调试照常）"
            % fn.__qualname__)


def test_daily_close_without_fold_contract_is_exposed():
    """非完整宏（日线无折叠契约）必须可被 doctor 识别（§3.1 偏离显式化）。"""
    from app.market_cn.auto.strategies.base import _has_fold_contract
    reg.autodiscover()
    daily = [k for k in reg.all_strategies()
             if reg.get_strategy(k).scan_spec.kind == "daily_close"]
    assert daily, "无 daily_close 策略？发现逻辑失效 = 假绿"
    nocon = [k for k in daily if not _has_fold_contract(reg.get_strategy(k))]
    # 已知：v1/relay3 已于 2026-10-09 退役 ⇒ 当前已无缺契约者；启用中的日线策略必须全部有契约
    enabled_nocon = [k for k in nocon if reg.is_enabled(k)]
    assert not enabled_nocon, (
        "启用中的日线策略缺折叠契约（生产判定/回测路径断裂）: %s" % enabled_nocon)
