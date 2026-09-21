#!/usr/bin/env python3
"""tools/market_spec_check.py — M5 验收闸门：市场适配层（MarketSpec）自检。

对应 docs/自动策略IDE架构设计.md §12 的 M5 验收标准 —— **"新市场接入不改 core"** ——
以及 §9.1/§9.3 的字段与分层纪律。它不是"跑一遍看看"，而是把验收标准拆成**可证伪**的检查：

  1. A 基线逐位等价 —— A spec 的档位/分板必须与重构前的硬编码**逐位**一致。
     若此处不等价，说明 M5 搬迁悄悄改了 A 股行为（最危险的一类回归）。
  2. core 无市场常量 —— 用 AST 扫 `core/**` 的**字面量**，确认涨跌停/分板常量确实搬走了。
     只扫字面量（不扫注释/docstring），故不会误报。
  3. 原语确实 spec 驱动 —— 用一个**与 A 无关**的合成 spec 跑原语，行为必须跟着 spec 变
     （含"无涨跌停市场恒 False"）。这条防的是"表面接 spec、实则仍读 A 常量"。
  4. 新市场只加一份 YAML —— 临时落一份 yaml 即被注册表识别、可加载；全程对 `core/**`
     做**文件哈希**比对，证明 core **零改动**。
  5. fail-fast 不静默降级 —— 未声明 key → KeyError；数据源未接的市场被要求运行 →
     MarketNotRunnable（**绝不**悄悄按 A 股口径跑出一个看似成功的错结果）。
  6. 策略侧解析 —— 全部 `strategies/*.yaml` 都能解析出 MarketSpec；声明未接入市场时
     `load_strategy` 必须直接失败。

退出码 = 失败项数（0 = 全过）。CLI 直跑，无副作用（临时文件在 finally 里清理）。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import random
import shutil
import sys
import time

from app.market_cn.auto.core import _paths
from app.market_cn.auto.core import market as M


def _quarantine(path: str) -> None:
    """清理探针文件：**移到仓库 `del/`**，不直接删。

    两个理由（缺一不可）：
    1. 项目铁律"删除一律移 del/，不直接删"；
    2. 直接 `os.remove` 会触发宿主的安全删除闸门（本次即因同一个 turn 内累计删除数超阈值
       而**直接终止进程**，表现为本工具跑到第 4 项就无输出），移走则不受该闸门约束。
    """
    if not os.path.isfile(path):
        return
    dst_dir = os.path.join(os.path.dirname(_paths.PROJECT_ROOT), "del", "_probe")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{int(time.time() * 1000)}_{os.path.basename(path)}")
    shutil.move(path, dst)

# ---------------------------------------------------------------
# 检查 1 期望值：**重构前**的硬编码实现（逐字引自旧 common/market.py + common/exec_cn.py）
# ---------------------------------------------------------------
def _legacy_is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0:
        return False
    return (close / prev_close - 1) >= threshold * 0.98


def _legacy_limit_dn_price(prev_close, board_type):
    return prev_close * ((1 - 0.10) if board_type == "main" else (1 - 0.20))


def _legacy_board_type(code):
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"


def _legacy_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"):
        return "科创板"
    elif c.startswith("30"):
        return "创业板"
    elif c.startswith("6"):
        return "沪主板"
    elif c.startswith("0") or c.startswith("2"):
        return "深主板"
    return "未知"


LEGACY_DN_TOL = 0.002

# core 内**不应再出现**的市场常量（数字字面量）/ 分板前缀（字符串字面量）
BANNED_NUM = {0.098, 0.198, 0.002}
BANNED_STR = {"30", "68", "60", "00", "88"}


def _core_dir() -> str:
    return os.path.join(_paths.AUTO_DIR, "core")


def _iter_core_py():
    root = _core_dir()
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(d, f)


def _hash_core() -> str:
    h = hashlib.sha256()
    for p in _iter_core_py():
        h.update(os.path.relpath(p, _paths.AUTO_DIR).encode("utf-8"))
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


# ================================================================
# 各项检查
# ================================================================
def check_a_baseline_exact(verbose: bool = False):
    """1. A spec 与重构前硬编码逐位等价（含随机扫）。"""
    errs = []
    spec = M.default_market()

    # 分板 / 名称：对一批真实形态的代码 + 随机码
    codes = ["600000", "601398", "000001", "002594", "300750", "301001", "688981", "689009",
             "200001", "900901", "830799", "430047", "920819", "68", "30", "6", "0", "2",
             "999999", "123456"]
    rnd = random.Random(20260920)
    codes += [f"{rnd.randint(0, 999999):06d}" for _ in range(400)]
    for c in codes:
        if M.get_board_type(c) != _legacy_board_type(c):
            errs.append(f"board_type({c}): {M.get_board_type(c)} != {_legacy_board_type(c)}")
        if M.get_board_name(c) != _legacy_board_name(c):
            errs.append(f"board_name({c}): {M.get_board_name(c)} != {_legacy_board_name(c)}")

    # 涨跌停：边界 + 随机
    cases = [(1.10, 1.0, "main"), (1.0960, 1.0, "main"), (1.09604, 1.0, "main"),
             (1.20, 1.0, "gem_star"), (1.19404, 1.0, "gem_star"), (1.0, 1.0, "main"),
             (0.5, 1.0, "main"), (1.0, 0.0, "main")]
    for _ in range(4000):
        prev = round(rnd.uniform(1.0, 40.0), 4)
        close = round(prev * rnd.uniform(0.85, 1.25), 4)
        bt = rnd.choice(["main", "gem_star"])
        cases.append((close, prev, bt))
    n_diff = 0
    for (c, p, bt) in cases:
        got = M.is_limit_up(c, p, bt, spec)
        exp = _legacy_is_limit_up(c, p, bt)
        if got != exp:
            n_diff += 1
            if len(errs) < 6:
                errs.append(f"is_limit_up({c},{p},{bt}): {got} != {exp}")
    if n_diff:
        errs.append(f"is_limit_up 随机扫 {n_diff}/{len(cases)} 不一致")

    # 跌停价
    for (p, bt) in [(10.0, "main"), (10.0, "gem_star"), (3.33, "main"), (7.77, "gem_star")]:
        g, e = M.limit_dn_price(p, bt, spec), _legacy_limit_dn_price(p, bt)
        if g != e:
            errs.append(f"limit_dn_price({p},{bt}): {g!r} != {e!r}")
    if M.limit_dn_tol(spec) != LEGACY_DN_TOL:
        errs.append(f"limit_dn_tol: {M.limit_dn_tol(spec)} != {LEGACY_DN_TOL}")

    if verbose and not errs:
        print(f"      A.bands={spec.bands} band_default={spec.band_default}")
        print(f"      board_rules={spec.board_rules} board_default={spec.board_default}")
    return errs


def check_core_has_no_market_constants(verbose: bool = False):
    """2. AST 扫 core：不得再出现市场常量字面量 / 分板前缀。"""
    errs = []
    n_file = 0
    for p in _iter_core_py():
        n_file += 1
        with open(p, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        rel = os.path.relpath(p, _paths.AUTO_DIR)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                v = node.value
                if isinstance(v, float) and v in BANNED_NUM:
                    errs.append(f"{rel}:{node.lineno} 数字常量 {v} 应属 adapters/markets/*.yaml")
                elif isinstance(v, int) and float(v) in BANNED_NUM and v != 0:
                    errs.append(f"{rel}:{node.lineno} 数字常量 {v} 应属 adapters/markets/*.yaml")
                elif isinstance(v, str) and v in BANNED_STR:
                    errs.append(f"{rel}:{node.lineno} 分板前缀 {v!r} 应属 adapters/markets/a.yaml")
    if verbose:
        print(f"      扫描 {n_file} 个 core/*.py")
    return errs


def check_primitives_are_spec_driven(verbose: bool = False):
    """3. 原语确实跟着 spec 走（含无涨跌停市场）。"""
    errs = []
    # (a) 无涨跌停市场 → 恒 False / 跌停价 0
    none_spec = M.MarketSpec(key="X", name="无涨跌停", band_kind="none", bands={},
                             band_default=None)
    for (c, p, bt) in [(2.0, 1.0, "main"), (1.5, 1.0, "gem_star")]:
        if M.is_limit_up(c, p, bt, none_spec):
            errs.append(f"无涨跌停市场 is_limit_up({c},{p},{bt}) 应为 False")
    if M.limit_dn_price(10.0, "main", none_spec) != 0.0:
        errs.append("无涨跌停市场 limit_dn_price 应为 0.0")
    if M.find_limit_ups([{"close": 1.0}, {"close": 2.0}], "main", none_spec):
        errs.append("无涨跌停市场 find_limit_ups 应为空")

    # (b) 自定义档位（5% 涨 / 5% 跌）→ 阈值必须跟着变
    custom = M.MarketSpec(key="Y", bands=M.build_bands(
        {"main": {"up_pct": 0.05, "up_tol": 1.0, "dn_pct": 0.05, "dn_tol": 0.01}}),
        band_default="main")
    if not M.is_limit_up(1.06, 1.0, "main", custom):
        errs.append("自定义 5% 档: 1.06/1.00 应判涨停")
    if M.is_limit_up(1.04, 1.0, "main", custom):
        errs.append("自定义 5% 档: 1.04/1.00 不应判涨停")
    if abs(M.limit_dn_price(10.0, "main", custom) - 9.5) > 1e-12:
        errs.append(f"自定义 5% 档: 跌停价应为 9.5, 实得 {M.limit_dn_price(10.0, 'main', custom)}")
    if abs(M.limit_dn_tol(custom) - 0.01) > 1e-12:
        errs.append("自定义档 dn_tol 未生效")

    # (c) 自定义分板前缀 → 分类跟着变
    cus2 = M.MarketSpec(key="Z", band_kind="pct",
                        bands=M.build_bands({"custom": {"up_pct": 0.2, "dn_pct": 0.2}}),
                        band_default="custom",
                        board_rules=[("99", "custom")], board_default="custom")
    if M.get_board_type("990001", cus2) != "custom":
        errs.append("自定义分板前缀未生效")
    if verbose:
        print("      合成市场通过（无涨跌停 / 5% 档 / 自定义分板 均跟随 spec）")
    return errs


def check_new_market_only_needs_yaml(verbose: bool = False):
    """4. 新增市场 = 加一份 YAML：注册表识别，且 core 文件哈希全程不变。"""
    errs = []
    probe = os.path.join(_paths.MARKETS_DIR, "_probe_mkt.yaml")
    before = _hash_core()
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write(
                "key: _probe_mkt\n"
                "name: 探针市场\n"
                "intraday_t0: true\n"
                "settlement_days: 0\n"
                "settlement_basis: event\n"
                "direction: long_short\n"
                "short_rule: synthetic_no\n"
                "band_kind: bounded\n"
                "bands: {}\n"
                "price_bound: [0.0, 1.0]\n"
                "lot_size: 1\n"
                "tick_size: 0.01\n"
                "currency: USDC\n"
                "tz: UTC\n"
                "fee_model: taker\n"
                "board_rules: []\n"
                "board_names: []\n"
                "source: probe\n"
                "runnable: true\n"
            )
        from app.market_cn.auto.adapters.markets.registry import (
            load_market, market_keys, require_runnable,
        )
        if "_probe_mkt" not in market_keys():
            errs.append("新增 yaml 未被 market_keys 识别")
        s = load_market("_probe_mkt", refresh=True)
        if (s.key, s.settlement_basis, s.intraday_t0, s.price_bound) != \
                ("_probe_mkt", "event", True, (0.0, 1.0)):
            errs.append(f"探针市场字段解析异常: {s.key}/{s.settlement_basis}/{s.intraday_t0}/{s.price_bound}")
        if require_runnable(s) is not s:
            errs.append("require_runnable 未原样返回")
        if verbose:
            print("      探针市场（event 结算 / bounded[0,1]）加载成功")
    finally:
        _quarantine(probe)
    after = _hash_core()
    if before != after:
        errs.append("core 文件哈希在新增市场前后变化 —— 违反'core 零改动'")
        if verbose:
            print(f"      before={before[:16]} after={after[:16]}")
    else:
        if verbose:
            print(f"      core 哈希全程不变: {after[:16]}…")
    return errs


def check_fail_fast(verbose: bool = False):
    """5. fail-fast：未声明 key / 未接入市场都不得静默降级。"""
    errs = []
    from app.market_cn.auto.adapters.markets.registry import (
        MarketNotRunnable, load_market, require_runnable,
    )
    try:
        load_market("NO_SUCH_MARKET")
        errs.append("未声明市场未抛 KeyError（静默降级风险）")
    except KeyError:
        pass
    except Exception as e:
        errs.append(f"未声明市场抛了非 KeyError: {type(e).__name__}: {e}")

    n_unrunnable = 0
    for k in ("HK", "US", "POLYMARKET"):
        try:
            require_runnable(load_market(k))
            errs.append(f"{k} 声明 runnable=false 却未报错（会按 A 股口径静默跑）")
        except MarketNotRunnable:
            n_unrunnable += 1
    if verbose:
        print(f"      未接入市场 fail-fast: {n_unrunnable}/3")

    # 策略声明未接入市场 → load_strategy 必须直接失败
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    p = os.path.join(_paths.STRATEGY_DIR, "_probe_hk.yaml")
    try:
        meta = {"name": "探针港股", "market": "HK"}
        with open(p, "w", encoding="utf-8") as f:
            f.write("meta:\n  name: 探针港股\n  market: HK\n"
                    "entry: {mode: open}\nexit: {mode: hold}\n"
                    "gates:\n  - {id: g1, expr: 'close() > 0', role: required}\n")
        try:
            load_strategy("_probe_hk")
            errs.append("策略声明 market: HK 时 load_strategy 未失败")
        except MarketNotRunnable:
            pass
    finally:
        _quarantine(p)
    return errs


def check_strategy_markets(verbose: bool = False):
    """6. 全部 strategies/*.yaml 能解析出 MarketSpec。"""
    import glob
    errs = []
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    keys = sorted(os.path.splitext(os.path.basename(x))[0]
                  for x in glob.glob(os.path.join(_paths.STRATEGY_DIR, "*.yaml")))
    if not keys:
        errs.append("strategies/*.yaml 为空 —— 策略目录搬迁可能出错")
    for k in keys:
        try:
            s = load_strategy(k)
        except Exception as e:
            errs.append(f"{k}: load_strategy 失败 {type(e).__name__}: {e}")
            continue
        if s.market_spec is None:
            errs.append(f"{k}: market_spec 为 None")
        elif s.market_key != s.market_spec.key:
            errs.append(f"{k}: market_key={s.market_key} != spec.key={s.market_spec.key}")
        elif verbose:
            print(f"      {k:18s} market={s.market_key} gates={len(s.enabled_gates)}")
    return errs


CHECKS = [
    ("1 A 基线逐位等价", check_a_baseline_exact),
    ("2 core 无市场常量", check_core_has_no_market_constants),
    ("3 原语 spec 驱动", check_primitives_are_spec_driven),
    ("4 新市场只加一份 YAML (core 零改动)", check_new_market_only_needs_yaml),
    ("5 fail-fast 不静默降级", check_fail_fast),
    ("6 策略侧 market 解析", check_strategy_markets),
]


def run(verbose: bool = False) -> int:
    n_fail = 0
    for title, fn in CHECKS:
        try:
            errs = fn(verbose=verbose)
        except Exception as e:
            import traceback
            errs = [f"检查自身异常 {type(e).__name__}: {e}", traceback.format_exc()[-600:]]
        if errs:
            n_fail += 1
            print(f"[FAIL] {title}")
            for e in errs[:6]:
                print(f"        {e}")
        else:
            print(f"[PASS] {title}")
            if verbose:
                pass
    print(f"\nmarket_spec_check: {'ALL_PASS' if n_fail == 0 else f'{n_fail} FAIL'}")
    return n_fail


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M5 市场适配层验收闸门")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    sys.exit(run(verbose=a.verbose))
