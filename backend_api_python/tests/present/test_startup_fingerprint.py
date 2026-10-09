"""test_startup_fingerprint.py — 指纹必须跟得上「enabled/params 迁 yaml」（2026-10-09 审计 B-3）。

事故
----
`enabled` 与params 的事实源已迁`<key>.yaml`（`meta.enabled` / `params` 段），但
`startup.fingerprint()` 仍只从 config.json 读这两个键 ⇒
- 5 个策略 `enabled` 恒`False`、`params` 恒 `{}`；
- `strategies/*.yaml` 被归入**展示层** ⇒ 改 yaml 只得 `display_changed`
  → 走「仅展示层变更 → 无需重建」分支。

后果（两条补偿链同时失效）：
① 在 yaml 里禁用策略 → 不再触发 `retire_unfilled`（2026-09-23 事故的补偿路径）；
   只剩 monitor 开盘窗口 sweep 与 stale 次日兜底。
② 改 yaml params → 不触发 rebuild 校准 ⇒ 窗口内历史行仍是旧参数判定，
   正是 startup 模块 docstring 自述要消灭的「改了规则界面还是老样子」。

本文件锁三件事
--------------
1. `fingerprint()` 的 strategies 段与**事实源 API** 逐字段一致（不是与 config 一致）；
2. `strategies/*.yaml` 在 **rules 段**、**不在** display 段
   —— 后者防的是「移出display 后从指纹彻底消失」这个更糟的回归；
3. 改 yaml 的 `meta.enabled` / `params` ⇒ rules 段摘要**必须变化**。

⚠️ 第3 条用「临时改文件→ 取证→ 还原」实现，故monkeypatch 到临时目录而非真实策略目录，
    绝不写生产文件。
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

from app.market_cn.auto import startup


# ================================================================
# 1. strategies 段与事实源 API 一致
# ================================================================
def test_fingerprint_strategies_match_source_of_truth():
    """fingerprint 的 enabled/params 必须等于 is_enabled/params_override 的返回值。

    旧实现读 config 键，对有 yaml 的策略恒为 False/{} ⇒ 本断言在旧代码下必红。
    """
    import app.market_cn.auto.strategies as strat_reg

    strat_reg.autodiscover()
    fp = startup.fingerprint()
    got = fp.get("strategies") or {}
    assert got, "strategies 段为空（config 与插件目录双双异常？）—— 本门禁已失效"

    mismatched = []
    for key in sorted(got):
        expect_enabled = bool(strat_reg.is_enabled(key))
        expect_params = strat_reg.params_override(key) or {}
        if got[key].get("enabled") != expect_enabled:
            mismatched.append(
                f"{key}: enabled 指纹={got[key].get('enabled')} 事实源={expect_enabled}")
        if (got[key].get("params") or {}) != expect_params:
            mismatched.append(f"{key}: params 指纹={got[key].get('params')} "
                              f"事实源={expect_params}")
    assert not mismatched, (
        "fingerprint 的 enabled/params 与事实源 API 不一致 ⇒ 改宏不触发补偿。\n  "
        + "\n  ".join(mismatched)
    )


def test_fingerprint_enabled_is_true_for_live_strategies():
    """至少要有一个 enabled=True 的策略 —— 反向防「enabled 恒 False」这个原bug。

    活跃策略集为 break / dragon_callback / g56 / knife_catch / tail_oversold，
    全部 `meta.enabled: true`。若指纹里全False，说明又退回读 config 了。
    """
    fp = startup.fingerprint()
    got = fp.get("strategies") or {}
    enabled = sorted(k for k, v in got.items() if v.get("enabled"))
    assert enabled, f"指纹里无任何 enabled 策略：{ {k: v.get('enabled') for k, v in got.items()} }"


# ================================================================
# 2. 策略宏归rules 段, 不在 display 段
# ================================================================
def test_strategy_macros_are_in_rules_not_display():
    """`strategies/*.yaml` 必须在 rules 段, 且**不在** display 段。

    两个方向都要锁：
    - 不在 rules 段 ⇒ 改 yaml 不触发 rebuild（B-3 的原病灶）；
    - 仍在 display 段 ⇒ 走「仅展示层变更」分支，同样不重建。
    """
    fp = startup.fingerprint()
    rules, display = fp.get("rules") or {}, fp.get("display") or {}
    macros = startup._iter_strategy_macros()
    assert macros, "未找到任何策略宏 —— 本门禁已失效（策略目录路径变了？）"

    for rel, _p in macros:
        assert rel in rules, f"{rel} 不在 rules 段 ⇒ 改宏不触发重建"
        assert rel not in display, (
            f"{rel} 仍在 display 段 ⇒ 走「仅展示层变更 → 无需重建」分支，"
            f"补偿链依然失效（B-3 原病灶）"
        )


def test_display_segment_holds_only_display_meta():
    """display 段只应是展示元数据（当前 = core/display_meta.py，可为空）。"""
    fp = startup.fingerprint()
    for rel in (fp.get("display") or {}):
        assert not rel.startswith("strategies/"), (
            f"display 段含策略文件 {rel} —— 策略宏是规则，必须在 rules 段"
        )


def test_every_active_strategy_macro_has_a_fingerprint():
    """注册表里每个策略都必须有自己的宏在指纹中（防新增策略漏指纹）。

    无宏策略（当前无）允许缺席，但其 yaml 缺失时 fingerprint 段不该凭空多出条目。
    """
    import app.market_cn.auto.strategies as strat_reg

    strat_reg.autodiscover()
    rules = startup.fingerprint().get("rules") or {}
    in_rules = {r.split("/")[-1] for r in rules if r.startswith("strategies/")
                and r.endswith(".yaml")}
    for key in sorted(strat_reg.all_strategies()):
        yaml_name = f"{key}.yaml"
        if os.path.isfile(os.path.join(startup._strategies_dir(), yaml_name)):
            assert yaml_name in in_rules, f"{yaml_name} 未进入 rules 段（新增策略漏指纹？）"


# ================================================================
# 3. 改 yaml 的 enabled / params ⇒ rules 段摘要必变
# ================================================================
def _write_macro(dirpath, key, doc):
    with open(os.path.join(dirpath, f"{key}.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True)


@pytest.fixture()
def fake_macro_dir(tmp_path, monkeypatch):
    """把 `_strategies_dir` 指向临时目录 ⇒ 本测试绝不碰生产策略文件。"""
    d = tmp_path / "strategies"
    d.mkdir()
    monkeypatch.setattr(startup, "_strategies_dir", lambda: str(d))
    return str(d)


def test_edited_macro_changes_rules_hash(fake_macro_dir):
    """改宏内容 ⇒ rules 段摘要变化（这是 rebuild 触发的唯一依据）。"""
    base = {"meta": {"name": "x", "market": "A", "version": 1, "enabled": True},
            "params": {"a": 1}}
    _write_macro(fake_macro_dir, "demo", base)
    h1 = startup.fingerprint()["rules"]

    _write_macro(fake_macro_dir, "demo",
                 {"meta": {"name": "x", "market": "A", "version": 1, "enabled": True},
                  "params": {"a": 2}})
    h2 = startup.fingerprint()["rules"]

    assert h1 != h2, "改了 params 后 rules 段摘要未变 ⇒ 重建不会触发（B-3 未修复）"


def test_toggling_enabled_changes_rules_hash(fake_macro_dir):
    """切 `meta.enabled` ⇒ rules 段摘要变化（否则停用不触发 retire_unfilled 补偿）。"""
    _write_macro(fake_macro_dir, "demo",
                 {"meta": {"name": "x", "market": "A", "version": 1, "enabled": True}})
    h1 = startup.fingerprint()["rules"]
    _write_macro(fake_macro_dir, "demo",
                 {"meta": {"name": "x", "market": "A", "version": 1, "enabled": False}})
    h2 = startup.fingerprint()["rules"]
    assert h1 != h2, "切 meta.enabled 后 rules 段摘要未变 ⇒ 停用不触发补偿"
