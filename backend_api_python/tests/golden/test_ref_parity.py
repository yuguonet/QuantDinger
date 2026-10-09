"""test_ref_parity.py — 外部旧版参照对拍（§2.2.3，删除开关的第二把钥匙）。

来源：改进方案 v2.1 §2.2.3 落地 | 产出：2026-10-08

★ 它补的两个缺口（§2.2.1 冻结基线之外）：
  ① **冻结后与活代码的交叉验证**：baselines/*.json 是静态真相，参照树是活的旧版
    —— 持续对拍证明「冻结的 == 旧代码真会产出的」，防基线与实现脱钩；
  ② **P6 删除后仍可跑**：对拍走 `legacy_trade_diff.ref_trades`（独立子进程 +
    PYTHONPATH 指向旧版树），树内 `backtest_stock` 删掉也不受影响。

★ 运行前提：参照树根 = `AUTO_SLIM_REF`（本地覆盖）或 config.json `ref_tree.root`
  （2026-10-08 起；解压 `QuantDinger-auto展示层正常.zip` 得到的工程根或
  backend_api_python 目录均可）。两者都无 ⇒ 整组 skip，**不是通过**
  （无证据不算绿，同 golden_check 的空集纪律）。参照树**只读**（改了就不是旧版）。

★ §2.2.2 边界（R1/R2 不以旧版为准）：
  参照树与重冻基线的差异预期（2026-10-08 实测更正 —— 原「参照树是 R1/R2 之前
  的版本」只对一半）：
    · R2（出场起点 D2）：参照树 **已含** `_min_sell_day`(T+1→2) ⇒ `dragon_d1_stop_hit`
      两侧**逐位一致**，无差异可登记（原 KNOWN_DIVERGENT 登记是死分支，已删）；
    · R1（末日平仓收尾）：参照树 base 通用引擎**已有**「数据结束平仓」，但五个策略
      覆写（break/dragon/g56/relay3/v1）仍是 `if not r: continue`（丢弃未平仓）⇒
      当前输入集**未命中**未平仓（口径差异报告 §5）；将来输入集扩到命中时，
      R1 差异会真实出现（多出收尾交易），**届时再登记 KNOWN_DIVERGENT**。
  其余输入恒逐笔逐位 —— 未登记差异一律 FAIL（架构改动不应动输出）。

易错点:
  - 池化输入（g56）**不在本组**：参照子进程是单票旧引擎（无池接线），比了也是假差；
    g56 的等价仍由 test_golden_parity（冻结基线）背书。
  - skip ≠ 通过：CI/本地必须至少有一处**实际跑过**本组（见工具 docstring 的用法）。
"""

from __future__ import annotations

import os

import pytest

from tests.golden.freeze import GOLDEN_COMPARE_FIELDS, _project
from tests.golden.inputs import INPUT_SETS

def _ref_root():
    """参照树根: `AUTO_SLIM_REF` 优先 (本地覆盖), 回退 config.json `ref_tree.root`。

    2026-10-08: 只靠环境变量时, 换机/重启即失效 ⇒ 本组静默 skip (skip ≠ 通过,
    却看着像"没跑过也没关系")。config 侧读取入口 `strategies.ref_tree_settings`。
    """
    env = os.environ.get("AUTO_SLIM_REF")
    if env:
        return env
    try:
        from app.market_cn.auto.strategies import ref_tree_settings
        return ref_tree_settings().get("root") or None
    except Exception:                                   # noqa: BLE001
        return None


REF_ROOT = _ref_root()

#: §2.2.2 已登记的规则变更差异（输入名 → 登记说明）。
#: 2026-10-08: 原登记 `dragon_d1_stop_hit`(R2) 是**死分支** —— 参照树已含
#: `_min_sell_day`(T+1→2)，实测两侧逐位一致，差异恒 0 走 `continue`，形状断言
#: 永不执行。已删登记。R1 将来命中未平仓时再登记（见模块头 §2.2.2 边界）。
KNOWN_DIVERGENT: dict[str, str] = {}

#: 架构侧字段（入场/信号）永远逐位 —— 规则变更只动出场侧，入场侧漂了就是真回归。
ARCH_FIELDS = ("d0_date", "entry_date", "entry_price")


def _have_ref() -> bool:
    if not REF_ROOT:
        return False
    from app.market_cn.auto.tools.legacy_trade_diff import resolve_ref_backend
    return os.path.isdir(os.path.join(resolve_ref_backend(REF_ROOT), "app"))


pytestmark = pytest.mark.skipif(
    not _have_ref(),
    reason="AUTO_SLIM_REF 未设/无效 —— 外部参照对拍无证据，不算通过（§2.2.3）")

# 盘中条目（intraday）不进本组：其旧侧走 `run_all_intraday` 折叠（无外部
# backtest_stock 参照），外部参照子进程跑不出东西；它们的冻结交叉验证走
# `freeze --check`（树内折叠源）。外部参照扩展到盘中折叠为后续项。
_SPECS = [s for s in INPUT_SETS if not s.get("pooled") and not s.get("intraday")]


def _load_baseline(name):
    import json
    p = os.path.join(os.path.dirname(__file__), "baselines", name + ".json")
    return json.load(open(p, encoding="utf-8"))


def _ref_projected(spec):
    """参照树旧引擎输出 → 与基线同口径的 canonical trades（同 freeze._project）。"""
    from app.market_cn.auto.tools.legacy_trade_diff import ref_trades
    bars = spec["build"]()
    raw = ref_trades(REF_ROOT, spec["strategy"], spec["code"], bars,
                     stock_info=spec.get("stock_info"), kwargs=spec.get("kwargs") or {})
    return [_project(t, bars, spec["code"], spec["strategy"]) for t in raw]


def _eq(a, b, tol=1e-12):
    if a is None or b is None:
        return a == b
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


@pytest.mark.parametrize("spec", _SPECS, ids=[s["name"] for s in _SPECS])
def test_ref_tree_matches_frozen_baseline(spec):
    """参照树活代码输出 == 冻结基线（KNOWN_DIVERGENT 登记差异除外；当前为空 = 恒逐位）。"""
    name = spec["name"]
    base = _load_baseline(name)
    ref = _ref_projected(spec)
    frozen = base["trades"]

    assert ref, "%s 参照树零输出 —— 对拍无证据（旧引擎异常或输入失效）" % name
    assert len(ref) == len(frozen), (
        "%s 笔数不符: 参照 %d != 基线 %d（R1 在本输入集应未命中，见 §2.2.2）\n"
        "参照=%s\n基线=%s" % (name, len(ref), len(frozen), ref, frozen))

    fields = base.get("compare_fields") or list(GOLDEN_COMPARE_FIELDS)
    divergent = name in KNOWN_DIVERGENT
    for i, (r, fz) in enumerate(zip(ref, frozen)):
        tag = "%s#%d" % (name, i)
        # 入场侧永远逐位（架构等价的底线）
        for k in ARCH_FIELDS:
            assert _eq(r.get(k), fz.get(k)), \
                "%s 入场侧漂移 %s: 参照 %r != 基线 %r" % (tag, k, r.get(k), fz.get(k))
        exit_fields = [k for k in fields if k not in ARCH_FIELDS]
        diffs = [k for k in exit_fields if not _eq(r.get(k), fz.get(k))]
        if not diffs:
            continue
        # 已登记差异放行；形状校验归登记时的人工复核（每条 R 的形状不同，不在此硬编码）
        assert divergent, (
            "%s 出现未登记差异 %s: 参照=%s 基线=%s —— 架构改动不应动输出；"
            "若是有意规则变更，先登记 §2.2.2/KNOWN_DIVERGENT 并重冻基线"
            % (tag, diffs, {k: r.get(k) for k in diffs},
               {k: fz.get(k) for k in diffs}))


@pytest.mark.parametrize("spec", _SPECS, ids=[s["name"] for s in _SPECS])
def test_ref_trade_sets_align(spec):
    """交易集合键（信号日+入场日）两侧对齐 —— 集合漂了逐笔比也没有意义。"""
    base = _load_baseline(spec["name"])
    ref = _ref_projected(spec)
    keys_ref = {(t.get("d0_date"), t.get("entry_date")) for t in ref}
    keys_fz = {(t.get("d0_date"), t.get("entry_date")) for t in base["trades"]}
    assert keys_ref == keys_fz, (
        "%s 交易集合不一致: 只参照=%s 只基线=%s"
        % (spec["name"], sorted(keys_ref - keys_fz), sorted(keys_fz - keys_ref)))
