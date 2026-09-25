# -*- coding: utf-8 -*-
"""
strategy_debug/run.py — 策略调试工具面 (供 LLM 调用)

定位 (2026-09-26): 把 auto/tools/why|debug|doctor 包成 agent skill 工具,
让「600519 为啥不出信号」这类口语问题由 LLM 转调取证 + 人话解读。

纪律:
  - 判定/数值一律来自底层 CLI (app.market_cn.auto.tools.*), 本文件零规则;
  - 返回 dict (status/output/error), 便于 CodeAgent/JSON 通道消费;
  - 不写 config、不改库 (doctor --static 默认)。

可调用函数 (SkillFuncTool 会自动包装全部公开函数):
  list_strategies / why_strategy / debug_strategy / doctor_auto
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, Optional


def _run_capture(fn, argv: list) -> Dict[str, Any]:
    """跑 argparse CLI main(argv), 捕获 stdout/stderr。"""
    buf = io.StringIO()
    code = 0
    err = ""
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            code = int(fn(argv) or 0)
    except SystemExit as e:
        code = int(e.code or 0) if isinstance(e.code, int) else 1
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        code = 1
    out = buf.getvalue()
    return {"status": "ok" if code == 0 else "error",
            "exit": code, "output": out, "error": err}


def _run_capture_argv0(fn, argv: list) -> Dict[str, Any]:
    """debug.main() 走 sys.argv — 包一层。"""
    old = sys.argv
    sys.argv = ["debug"] + argv
    try:
        return _run_capture(fn, argv)
    finally:
        sys.argv = old


def list_strategies() -> Dict[str, Any]:
    """列出已注册策略 key/名称/enabled/形态 (调试前先对一下名字)。"""
    try:
        from app.market_cn.auto import strategies as reg
        reg.autodiscover()
        from app.market_cn.auto import registry
        items = []
        for key in registry.strategy_keys():
            s = reg.get_strategy(key)
            items.append({
                "key": key,
                "name": getattr(s, "name", key) if s else "(无插件)",
                "enabled": reg.is_enabled(key),
                "kind": getattr(getattr(s, "scan_spec", None), "kind", "") if s else "",
            })
        return {"status": "ok", "strategies": items}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def why_strategy(strategy: str, code: str, date: str = "",
                 days: int = 15, params: Optional[dict] = None,
                 db: bool = False, bars_days: int = 300) -> Dict[str, Any]:
    """口语调试主入口: 多日粗扫 / 单日深潜 / 参数试调 / 库对照。

    Args:
        strategy: 策略 key (t_hilo / v1 / break / dragon_callback / knife_catch ...)
        code: 股票代码, 如 600519
        date: 给定 YYYY-MM-DD 则单日深潜 (逐门); 不给则多日粗扫
        days: 多日窗口自然日 (默认 15)
        params: 临时参数覆盖 dict, 如 {"entry_gain_min": 1.0} — **不写 config**
        db: True 则对照 qd_dragon_signals 近几行
        bars_days: 日线取数窗口 (默认 300)

    Returns:
        {status, output, ...}  output 为人可读工具原文, 请据此解读 (勿编造数值)
    """
    from app.market_cn.auto.tools import why as m
    argv = ["--strategy", str(strategy or ""), "--code", str(code or ""),
            "--days", str(int(days)), "--bars-days", str(int(bars_days))]
    if date:
        argv += ["--date", str(date)]
    if params:
        argv += ["--params", params if isinstance(params, str)
                 else json.dumps(params, ensure_ascii=False)]
    if db:
        argv.append("--db")
    return _run_capture(m.main, argv)


def debug_strategy(strategy: str, code: str, date: str = "",
                   days: int = 300, max_lu: int = 3) -> Dict[str, Any]:
    """单日显微镜: 逐门计算值 / 插件 TRACE / 盘中逐触发 (比 why 更细)。

    Args:
        strategy: 策略 key
        code: 股票代码
        date: 决策日 YYYY-MM-DD (可空=窗口末根)
        days: 回看窗口自然日
        max_lu: limit_up 形态详展的涨停候选数
    """
    from app.market_cn.auto.tools import debug as m
    argv = ["--strategy", str(strategy or ""), "--code", str(code or ""),
            "--days", str(int(days)), "--max-lu", str(int(max_lu))]
    if date:
        argv += ["--date", str(date)]
    return _run_capture_argv0(m.main, argv)


def doctor_auto(strategy: str = "") -> Dict[str, Any]:
    """系统体检: config ↔ 注册表 ↔ 层反转 (静态, 不依赖 DB)。

    Args:
        strategy: 可选, 只查该策略 key

    注: doctor 退出码 0=全绿 1=有WARN 2=有FAIL — 前两者均视为可解读结果。
    """
    from app.market_cn.auto.tools import doctor as m
    argv = ["--static"]
    if strategy:
        argv += ["--strategy", str(strategy)]
    r = _run_capture(m.main, argv)
    if r.get("exit") in (0, 1):
        r["status"] = "ok"
    return r
