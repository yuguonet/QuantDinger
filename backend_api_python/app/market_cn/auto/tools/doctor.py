#!/usr/bin/env python3
"""auto/tools/doctor.py — 一键三方对账 (2026-09-26 P0-5)

用途: 回答「这个策略到底在不在跑」——config 开关 / 注册表 / 库内活跃行 三方对账,
     专抓「config 关了但还在跑」「有插件没 config」「有 config 没插件」「停用策略
     仍有未入场活跃行」这类假象。另附独立性快检 (YAML/py 配对、归档目录)。

用法:
  python -m app.market_cn.auto.tools.doctor            # 全量 (静态 + DB)
  python -m app.market_cn.auto.tools.doctor --static   # 只跑静态 (无 DB 也能用)
  python -m app.market_cn.auto.tools.doctor --strategy break_v2

退出码: 0=全绿 / 1=有 WARN / 2=有 FAIL。
不改任何数据 (纯读)。
"""
from __future__ import annotations

import argparse
import os
import sys

# CLI 直跑时加载 .env
try:
    from dotenv import load_dotenv
    for _p in (
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), ".env"),
    ):
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


def _findings():
    return []


def _add(findings, level, section, msg):
    findings.append((level, section, msg))


# ================================================================
# 1. 静态: config ↔ 注册表 ↔ 磁盘插件
# ================================================================
def check_static(findings, only=None):
    from app.market_cn.auto import strategies as strat_reg
    strat_reg.autodiscover()
    from app.market_cn.auto import registry
    from app.market_cn.auto.strategies import load_config

    cfg = load_config() or {}
    cfg_keys = [k for k, v in (cfg.get("strategies") or {}).items() if isinstance(v, dict)]
    plugins = sorted(strat_reg.all_strategies())
    keys = registry.strategy_keys()

    if only:
        cfg_keys = [k for k in cfg_keys if k in only]
        plugins = [k for k in plugins if k in only]
        keys = [k for k in keys if k in only]

    # 1a. 开关可达性 + 停用提示
    #     事实源 (2026-09-15 → 2026-10-09 终态②/A-D2): yaml meta.enabled > config.json enabled > False
    for k in plugins:
        yml = strat_reg._yaml_meta_enabled(k)
        if yml is None and k not in cfg_keys:
            _add(findings, FAIL, "config",
                 f"插件 {k} 既无 yaml meta.enabled 也无 config.json 段 → 开关不可达 (恒 False, 永久黑暗)")
        elif not strat_reg.is_enabled(k):
            note = (cfg.get("strategies", {}).get(k) or {}).get("_disabled_note")
            src = "yaml meta.enabled" if yml is not None else "config.json"
            extra = f" ({note})" if note else ""
            _add(findings, WARN, "enabled", f"策略 {k} 已注册但 enabled=false [来源 {src}]{extra}")

    # 1b. config 无插件
    for k in cfg_keys:
        if k not in plugins:
            _add(findings, FAIL, "config", f"config 有 {k} 但磁盘无插件 → 开关无效")

    # 1c. 磁盘文件 vs 已注册 (2026-10-12 D-R1: 取代原 _STRATEGIES_FALLBACK 比对)
    #     文件在盘上但未注册 = import 失败, 策略「物理存在但逻辑消亡」
    disk_keys = registry._disk_strategy_keys()
    if only:
        disk_keys = [k for k in disk_keys if k in only]
    if disk_keys:
        unregistered = [k for k in disk_keys if k not in plugins]
        if unregistered:
            _add(findings, FAIL, "fallback",
                 f"磁盘有策略文件但未注册 (import 失败?): {unregistered}")

    # 1d. YAML/py 配对
    strat_dir = os.path.dirname(os.path.abspath(strat_reg.__file__))
    yaml_stems = {os.path.splitext(f)[0] for f in os.listdir(strat_dir)
                  if f.endswith(".yaml")}
    for k in plugins:
        if k not in yaml_stems:
            _add(findings, WARN, "yaml", f"策略 {k} 无 YAML 门表 (present/analyze 路径不覆盖)")

    # 1d-2. 折叠契约覆盖 (终态②): 日线策略无契约 = 非完整宏 ⇒ 走不了折叠回测/调试路径。
    #       ⚠ 但 `.py` 插件是逃生舱 (目标态 §3.1) ⇒ **已登记 FOLD_CONTRACT_EXEMPT 的不算缺陷**
    #       (2026-10-12: 本检查原先不读那份名单, 把已登记的 v1 误报成 WARN, 与
    #        strategies/base.py:148 注释声明的「消费方: doctor(显式标注)」不符 —— 已接线)。
    #       未登记者才是真断裂 (record writer 只能靠「退回直判」+ERROR 活着)。
    try:
        from app.market_cn.auto.strategies.base import (
            _has_fold_contract, FOLD_CONTRACT_EXEMPT)
        nocon, exempt = [], []
        for k in plugins:
            s = strat_reg.get_strategy(k)
            if s is not None and s.scan_spec.kind == "daily_close" and not _has_fold_contract(s):
                (exempt if k in FOLD_CONTRACT_EXEMPT else nocon).append(k)
        # 陈旧登记 = 全量注册表里已不存在（**不受 --only 过滤影响**，否则只查子集时会误判）
        stale = [k for k in FOLD_CONTRACT_EXEMPT
                 if k not in set(strat_reg.all_strategies())]
        if nocon:
            _add(findings, WARN, "contract",
                 "日线策略缺折叠契约且未登记 (真断裂 ⇒ 补 init_state/step/evaluate 或在 "
                 "strategies/base.py::FOLD_CONTRACT_EXEMPT 具名登记): " + ", ".join(sorted(nocon)))
        if exempt:
            _add(findings, OK, "contract",
                 "逃生舱豁免 (已登记 FOLD_CONTRACT_EXEMPT; 生产走 scan 退回直判+ERROR / "
                 "rebuild 排除出投影集 / monitor 旧三决策, 回测走策略自持入口): "
                 + ", ".join(sorted(exempt)))
            # 2026-10-12 A-2: 豁免不是"没问题"，是"有意缺了什么" —— 逐条列出缺视图与到期
            try:
                from app.market_cn.auto.strategies.base import exemption_report
                for ent in exemption_report():
                    if ent["key"] not in set(exempt):
                        continue
                    views = ",".join(ent["missing_views"]) or "—"
                    tag = (f"逃生舱 {ent['key']}: 缺视图[{views}] "
                           f"ticket={ent['ticket'] or '—'} expires={ent['expires'] or '无'}")
                    if not ent["expires"]:
                        _add(findings, WARN, "contract", tag + " —— 无到期日(豁免不得无到期)")
                    elif ent["expired"]:
                        _add(findings, WARN, "contract",
                             tag + f" —— 已逾期 {-(ent['days_left'] or 0)} 天，迁移工单应开工")
                    else:
                        _add(findings, OK, "contract", tag + f" (剩 {ent['days_left']} 天)")
            except Exception as _e2:
                _add(findings, WARN, "contract", f"逃生舱体检不可用: {_e2}")
        if stale:
            _add(findings, WARN, "contract",
                 "FOLD_CONTRACT_EXEMPT 有陈旧登记 (key 已不存在, 请移出名单): "
                 + ", ".join(sorted(stale)))
        if not nocon and not exempt and not stale:
            _add(findings, OK, "contract", "全部日线策略已实现折叠契约 (回测/调试路径完整)")
    except Exception as e:
        _add(findings, WARN, "contract", f"折叠契约检查不可用: {e}")

    # 1e. 依赖方向: core 不得顶层 import strategies 私有符号 (层反转)
    #     函数内惰性 import = WARN (仍记欠债); 顶层 import = FAIL
    auto_root = os.path.dirname(strat_dir)
    core_dir = os.path.join(auto_root, "core")
    inversions = []
    lazy_inversions = []
    for root, _dirs, files in os.walk(core_dir):
        if "__pycache__" in root:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(root, f)
            try:
                text = open(p, encoding="utf-8").read()
            except Exception:
                continue
            tree = __import__("ast").parse(text)
            for node in tree.body:
                # 仅扫模块顶层 Import/ImportFrom
                if isinstance(node, (__import__("ast").ImportFrom)):
                    if (node.module or "").startswith("app.market_cn.auto.strategies"):
                        if "base" not in (node.module or ""):
                            if any(x in (node.module or "") for x in (
                                ".dragon", ".break", ".v1", ".g56", ".knife",
                                ".relay", ".tail", ".triple",
                            )):
                                inversions.append(
                                    f"{os.path.relpath(p, auto_root)}: from {node.module} import ...")
            # 函数内 import (惰性)
            for node in __import__("ast").walk(tree):
                if isinstance(node, __import__("ast").ImportFrom):
                    mod = node.module or ""
                    if mod.startswith("app.market_cn.auto.strategies") and "base" not in mod:
                        if any(x in mod for x in (
                            ".dragon", ".break", ".v1", ".g56", ".knife",
                            ".relay", ".tail", ".triple",
                        )):
                            rel = os.path.relpath(p, auto_root)
                            tag = f"{rel}: from {mod}"
                            if tag not in inversions and tag not in lazy_inversions:
                                # 顶层已在 inversions 则跳过
                                if not any(rel in x for x in inversions):
                                    lazy_inversions.append(tag)
    for inv in inversions:
        _add(findings, FAIL, "layer", f"core→strategies 顶层层反转: {inv}")
    for inv in lazy_inversions:
        _add(findings, WARN, "layer", f"core→strategies 惰性层反转 (待 day_flow 收编): {inv}")
    if not inversions and not lazy_inversions:
        _add(findings, OK, "layer", "core 未反向 import 具体策略模块")

    # 1f. 做T 约束 (MarketSpec 推导, 非特判)
    try:
        from app.market_cn.auto.core.t_legs import explain_constraints
        _add(findings, OK, "t_legs", f"默认市场做T约束: {explain_constraints()}")
    except Exception as e:
        _add(findings, WARN, "t_legs", f"t_legs 模块不可用: {e}")

    return keys


# ================================================================
# 2. DB: enabled=false 仍有未入场活跃行
# ================================================================
def check_db(findings, only=None):
    try:
        from app.market_cn.auto import strategies as strat_reg
        from app.market_cn.auto.store import S_WATCH_PENDING, list_signals
        strat_reg.autodiscover()
    except Exception as e:
        _add(findings, WARN, "db", f"跳过 DB 检查 (import 失败): {e}")
        return

    try:
        rows = list_signals(states=(S_WATCH_PENDING,), days=60)
    except Exception as e:
        _add(findings, WARN, "db", f"跳过 DB 检查 (查询失败): {e}")
        return

    zombies = []
    for r in rows:
        k = r.get("strategy") or ""
        if only and k not in only:
            continue
        if not strat_reg.is_enabled(k):
            if not r.get("entry_date"):
                zombies.append((k, r.get("code"), r.get("trade_date"), r.get("id")))
    if zombies:
        by = {}
        for k, _c, _d, _i in zombies:
            by[k] = by.get(k, 0) + 1
        _add(findings, FAIL, "db",
             f"停用策略仍有未入场 watch_pending 行 (应 retire): {by} 共 {len(zombies)}")
    else:
        _add(findings, OK, "db", "停用策略无未入场 watch_pending 残留")

    # 已入场行必须可见 (反向: 有 holding 则 OK, 不告警)
    try:
        act = list_signals(days=60, only_active=True)
        held = [r for r in act if r.get("entry_date") and not strat_reg.is_enabled(r.get("strategy") or "")]
        if held:
            _add(findings, OK, "db",
                 f"停用策略已入场行保留可见 {len(held)} 条 (资金安全, 符合设计)")
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="auto 三方对账 doctor")
    ap.add_argument("--static", action="store_true", help="只跑静态检查 (无 DB)")
    ap.add_argument("--strategy", default="", help="只查指定策略 key (可逗号分隔)")
    args = ap.parse_args(argv)

    only = {s.strip() for s in args.strategy.split(",") if s.strip()} or None
    findings = _findings()
    keys = check_static(findings, only=only)
    if not args.static:
        check_db(findings, only=only)

    n_f = sum(1 for lv, _, _ in findings if lv == FAIL)
    n_w = sum(1 for lv, _, _ in findings if lv == WARN)
    print("=== auto doctor ===")
    print(f"strategy_keys: {', '.join(keys) if keys else '(空)'}")
    for lv, sec, msg in findings:
        print(f"  [{lv:4s}] {sec:8s} {msg}")
    print(f"--- {n_f} FAIL / {n_w} WARN / {sum(1 for lv,_,_ in findings if lv==OK)} PASS ---")
    return 2 if n_f else (1 if n_w else 0)


if __name__ == "__main__":
    sys.exit(main())
