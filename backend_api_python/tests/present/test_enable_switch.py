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
    """缺折叠契约的日线策略：**要么迁契约，要么在 `FOLD_CONTRACT_EXEMPT` 登记**。

    2026-10-12 修正（原断言有误，是测试文件自身的问题）：
      本测试原写「启用中的日线策略必须全部有契约」，而 `strategies/base.py::
      FOLD_CONTRACT_EXEMPT`（2026-10-11 引入）的**设计**是：`.py` 插件是逃生舱
      （目标态 §3.1），未迁折叠契约 ≠ 缺陷，但必须**登记**；base 的注释也明文
      写着「消费方：tests/present/test_enable_switch（门禁）、tools/doctor」。
      但那次提交**没有同步本文件** ⇒ 名单写好了却没人读，已登记的 v1 被当缺陷恒报 FAIL。
      （时间线佐证：本文件最后提交 2026-10-09，FOLD_CONTRACT_EXEMPT 在 2026-10-11。）

    现按 base 的设计分流：
      · **未登记**的缺契约 ⇒ FAIL（= 真断裂：record writer 只能靠「退回直判」+ERROR 活着）
      · **已登记**的逃生舱 ⇒ 允许，但要求名单**不陈旧**（key 存在且确实缺契约）
        —— 生产侧对已登记者已有具名降级路径（scan.py「退回直判」+ERROR /
        rebuild.py 排除出投影集 / present_daily 落 stat.skipped / monitor 回退旧三决策），
        不是「断裂」。
    """
    from app.market_cn.auto.strategies.base import (
        _has_fold_contract, FOLD_CONTRACT_EXEMPT)
    reg.autodiscover()
    daily = [k for k in reg.all_strategies()
             if reg.get_strategy(k).scan_spec.kind == "daily_close"]
    assert daily, "无 daily_close 策略？发现逻辑失效 = 假绿"
    nocon = [k for k in daily if not _has_fold_contract(reg.get_strategy(k))]
    # ① 未登记的缺契约 = 生产判定/回测路径真断裂
    unregistered = [k for k in nocon if k not in FOLD_CONTRACT_EXEMPT]
    assert not unregistered, (
        "日线策略缺折叠契约且未在 FOLD_CONTRACT_EXEMPT 登记"
        "（真断裂：生产判定/回测路径不可用）: %s —— "
        "补 init_state/step/evaluate，或在 strategies/base.py 的 FOLD_CONTRACT_EXEMPT "
        "具名登记并写明降级路径" % unregistered)
    # ② 豁免名单不得陈旧：登记项须真实存在且确实缺契约（迁完契约就要立刻移出名单）。
    #    存在性用**全量注册表**判定（不受 daily_close 过滤影响，否则登记盘中策略会误判陈旧）。
    stale = [k for k in FOLD_CONTRACT_EXEMPT
             if k not in set(reg.all_strategies())
             or _has_fold_contract(reg.get_strategy(k))]
    assert not stale, (
        "FOLD_CONTRACT_EXEMPT 有陈旧登记（key 已不存在 / 已迁契约）: %s —— 请移出名单"
        % stale)
    # ③ 发现逻辑自证（防假绿）：万一某天全部策略都迁完契约、名单也清空，①②就是空跑 ——
    #    此时用合成对象证明检测器对「无 init_state/step/evaluate」确实返回 False（灵敏度）。
    if not nocon and not FOLD_CONTRACT_EXEMPT:
        class _NoContract:  # 三个方法一个都没有
            pass
        assert _has_fold_contract(_NoContract()) is False, \
            "_has_fold_contract 认不出缺契约者 ⇒ ①②恒真 = 假绿"


def test_fold_exempt_entries_are_structured():
    """逃生舱条目必须带 ticket + expires（2026-10-12 §13.1 A-2：豁免不得无到期）。

    只卡**结构**（有工单、有到期日、日期可解析）；逾期本身不 FAIL——
    到期即红会让门禁变成闹钟，可见性由 doctor 的 WARN 承担（豁免体检逐条输出）。
    """
    from app.market_cn.auto.strategies.base import FOLD_CONTRACT_EXEMPT, exemption_report
    assert FOLD_CONTRACT_EXEMPT, "名单为空？删除本测试前请确认策略已全部迁折叠契约"
    for ent in exemption_report():
        assert ent["ticket"], "%s 豁免无 ticket（迁移工单）" % ent["key"]
        assert ent["expires"], "%s 豁免无 expires（豁免不得无到期）" % ent["key"]
        assert ent["reason"], "%s 豁免无 reason" % ent["key"]
        if ent["expired"]:
            print("[warn] 逃生舱 %s 已到期 %s（%s）—— 迁移工单：%s"
                  % (ent["key"], ent["expires"], ent["days_left"], ent["ticket"]))
