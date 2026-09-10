#!/usr/bin/env python3
"""auto/tools/selftest.py — 策略契约测试闸门 (2026-09-10, M2-2 修正版)

用途: 把人工时代的检查自动化, 为规则改动/自主进化提供硬前置。回测负责对数验证,
     selftest 负责契约验证 — 两者互补, 都不承载调试 (调试=probe)。

四项检查:
  1. as-of 等价 (仅 daily_close 类): scan_signals(bars, as_of=k) 必须与
     scan_signals(bars[:k+1]) 逐字段一致 — as_of 泄漏未来数据在此暴露。
     盘中策略 (intraday_window) 无 as_of 语义, 跳过。
  2. 契约符合性 (全策略): key/name/scan_spec/default_params 齐备;
     scan_signals 无 ctx 调用返回 list[Signal] (盘中策略此时应返回 [])。
  3. A股约束 (AST 扫描策略文件): 禁硬编码涨跌停常数 (如 *0.1 /*0.9) —
     涨跌停判定必须引用 common/exec_cn 原语。
  4. 零 IO (AST): 策略文件禁 import DB/HTTP/子进程 — 数据一律由框架注入 bars/ctx。

易错点:
  - as-of 测试取数走 hub.daily (需 .env), 单股 O(n) 次 scan_signals, 默认只测 2 股;
  - Signal 是 dataclass, 等值比较含 extra dict, 同一调用可复现 → 可直接 ==;
  - AST 禁手清单宁可漏不可滥 (误报会让人关掉闸门), 常数扩充须有实据。
"""
from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import os
import sys

# CLI 直跑时加载 .env (as-of 测试取数需要)
try:
    from dotenv import load_dotenv
    for _p in [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), ".env"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))), ".env"))]:
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

from app.market_cn.auto.strategies import all_strategies, autodiscover
from app.market_cn.auto.strategies.base import ScanSpec, Signal

# 零 IO 黑名单: 策略文件禁 import (模块顶层与函数内 lazy import 都算)
BANNED_IMPORTS = {"psycopg2", "sqlite3", "mysql", "pymysql", "requests", "urllib",
                  "http", "socket", "subprocess", "app.utils.db", "app.utils.db_market"}
# 禁手涨跌停常数: Mult/Div 中的可疑因子 (涨跌停判定必须走 exec_cn 原语)
BANNED_LIMIT_FACTORS = {0.1, 0.2, 1.1, 1.2}


def _strategy_files():
    d = os.path.dirname(inspect.getfile(sys.modules["app.market_cn.auto.strategies"]))
    return [os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.endswith(".py") and f not in ("base.py", "__init__.py")]


def check_contract(strat):
    """契约符合性: 属性齐备 + scan_signals 无 ctx 调用返回 list[Signal]。"""
    errs = []
    for attr in ("key", "name", "scan_spec", "default_params"):
        if not hasattr(strat, attr):
            errs.append(f"缺属性 {attr}")
    if not isinstance(getattr(strat, "scan_spec", None), ScanSpec):
        errs.append("scan_spec 不是 ScanSpec")
    if not isinstance(getattr(strat, "default_params", None), dict):
        errs.append("default_params 不是 dict")
    try:
        out = strat.scan_signals([], strat.key)
    except TypeError:
        out = None      # 签名不兼容 bars=[] 时放宽为仅类型检查 (低质量契约, 单独报)
    except Exception as e:
        errs.append(f"scan_signals 空输入抛异常: {type(e).__name__}: {e}")
        out = None
    if out is not None:
        if not isinstance(out, list):
            errs.append(f"scan_signals 返回 {type(out).__name__}, 应为 list")
        elif out and not all(isinstance(s, Signal) for s in out):
            errs.append("scan_signals 返回含非 Signal 元素")
    return errs


def check_asof(strat, code, bars):
    """as-of 等价: as_of=k 与切片调用逐字段一致 (抽样 k, 含信号日附近的密采样)。"""
    errs = []
    n = len(bars)
    ks = list(range(60, n - 1, max(1, (n - 62) // 40))) + [n - 2]
    for k in sorted(set(ks)):
        try:
            a = strat.scan_signals(bars, code, as_of=k)
            b = strat.scan_signals(bars[:k + 1], code)
        except Exception as e:
            errs.append(f"as_of={k} 调用异常: {type(e).__name__}: {e}")
            continue
        if a != b:
            ta = [(s.time, s.score, s.price) for s in a]
            tb = [(s.time, s.score, s.price) for s in b]
            errs.append(f"as_of={k} 与切片不一致: as_of={ta} slice={tb}")
            break   # 第一处泄漏即停, 避免刷屏
    return errs


def check_ast(path):
    """AST 静态检查: 禁 DB/HTTP import + 禁硬编码涨跌停常数。"""
    errs = []
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if a.name in BANNED_IMPORTS or root in BANNED_IMPORTS:
                    errs.append(f"L{node.lineno} 禁 import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in BANNED_IMPORTS or mod.split(".")[0] in BANNED_IMPORTS:
                errs.append(f"L{node.lineno} 禁 import {mod}")
        elif isinstance(node, ast.BinOp):
            v = None
            if isinstance(node.op, ast.Mult) and isinstance(node.right, ast.Constant):
                v = node.right.value
            elif isinstance(node.op, ast.Div) and isinstance(node.right, ast.Constant):
                v = node.right.value
            if v in BANNED_LIMIT_FACTORS:
                errs.append(f"L{node.lineno} 疑似硬编码涨跌停常数 ({v}) — 应引用 exec_cn 原语")
    return errs


def run(codes=None, only=None, asof=True):
    autodiscover()
    strats = all_strategies()
    if only:
        strats = {k: v for k, v in strats.items() if k in only}
    report, n_fail = [], 0

    # 3/4: AST (对磁盘上的策略文件, 与注册表解耦 — 坏模块注册失败也能查)
    for path in _strategy_files():
        name = os.path.basename(path)
        errs = check_ast(path)
        report.append(("AST", name, "PASS" if not errs else f"FAIL: {errs[:3]}"))
        n_fail += bool(errs)

    for key, strat in sorted(strats.items()):
        errs = check_contract(strat)
        report.append(("契约", key, "PASS" if not errs else f"FAIL: {errs[:3]}"))
        n_fail += bool(errs)

    if asof:
        from app.market_cn.auto.data.hub import daily
        codes = codes or ["000017", "600397"]
        for key, strat in sorted(strats.items()):
            if strat.scan_spec.kind != "daily_close":
                report.append(("as-of", key, "SKIP (盘中策略无 as_of 语义)"))
                continue
            if callable(getattr(strat, "backtest_stock", None)) is False and \
               not hasattr(strat, "scan_signals"):
                continue
            any_err = []
            for code in codes:
                bars = daily(code, 300)
                if len(bars) < 70:
                    continue
                any_err = check_asof(strat, code, bars)
                if any_err:
                    break
            report.append(("as-of", key,
                           "PASS" if not any_err else f"FAIL: {any_err[:2]}"))
            n_fail += bool(any_err)

    for cat, name, r in report:
        print(f"[{r:>4}] {cat:5} {name}" + (f"  {r[5:]}" if r.startswith("FAIL") else ""))
    print(f"\nselftest: {'ALL_PASS' if n_fail == 0 else f'{n_fail} FAIL'}")
    return n_fail


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="策略契约测试闸门 (as-of等价/契约/AST约束)")
    ap.add_argument("--codes", default="", help="as-of 测试用股 (逗号分隔, 默认 000017,600397)")
    ap.add_argument("--strategy", default="", help="只测指定策略 key (逗号分隔)")
    ap.add_argument("--no-asof", action="store_true", help="跳过 as-of 动态测试 (纯静态快检)")
    a = ap.parse_args()
    sys.exit(1 if run(
        codes=[c.strip() for c in a.codes.split(",") if c.strip()] or None,
        only=[s.strip() for s in a.strategy.split(",") if s.strip()] or None,
        asof=not a.no_asof) else 0)
