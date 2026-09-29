# -*- coding: utf-8 -*-
"""core/market_env.py — 大盘资金流环境门 (2026-09-28 A)

用途: **规则门** (不是评分): 用资金流方向/净占比决定今日开仓档位。
数据源 (唯一): **app.market_cn.fund_flow_api** 公共接口
    market_flow_summary(days=5) → {direction, net_yi, pct, net_pct, history}
  缺数据 **fail-open** (照常放行), 绝不静默丢信号。

档位 (mode):
  full   正常开仓
  reduce 降额 (调用方按 daily_limit/仓位打折; 本模块只给档位)
  halt   停止开仓 (已持仓出场/监控不受影响)

阈值全部可配; 默认保守, 便于纸面跟踪后再收紧。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 默认阈值; direction 用 fund_flow_api 口径 (inflow/outflow/flat)
DEFAULT_RULES = {
    # D-1 净占比 % (net / turnover * 100); 更负 = 弱
    "net_pct_halt": -5.0,
    "net_pct_reduce": -2.0,
    # D-1 单日净额 (亿元) — 2026-09-28 纸面验证: ≤-50 亿组胜率 67% vs full 82%
    "net1_yi_halt": -50.0,
    "net1_yi_reduce": -10.0,
    # 近 N 日 outflow 天数 (direction_seq)
    "outflow_days_halt": 4,
    "outflow_days_reduce": 3,
    # 近 N 日净额合计 (亿元); 更负 = 弱
    "net5_yi_halt": -300.0,
    "net5_yi_reduce": -100.0,
}


def _rules(overrides: Optional[dict] = None) -> dict:
    r = dict(DEFAULT_RULES)
    if overrides:
        r.update({k: v for k, v in overrides.items() if v is not None})
    return r


def market_flow_gate(as_of: Optional[str] = None,
                     days: int = 5,
                     rules: Optional[dict] = None) -> Dict[str, Any]:
    """大盘资金流环境门 (走 fund_flow_api 公共 API)。

    Returns:
        {
          "mode": "full"|"reduce"|"halt",
          "ok": bool,
          "reason": str,
          "source": "fund_flow_api",
          "metrics": {direction, net_yi, net_pct, outflow_days, net_sum_yi, ...},
          "rules": {...},
        }
    """
    r = _rules(rules)
    metrics: Dict[str, Any] = {}
    try:
        from app.market_cn.fund_flow_api import market_flow_summary
        s = market_flow_summary(days=days, as_of=as_of) or {}
    except Exception as e:
        logger.warning("[market_env] fund_flow_api 失败(fail-open): %s", e)
        s = {}
    if not s or s.get("direction") == "unknown":
        return {"mode": "full", "ok": True,
                "reason": "无资金流数据 fail-open", "source": "none",
                "metrics": {}, "rules": r}

    direction = str(s.get("direction") or "flat")
    net_yi = float(s.get("net_yi") or 0.0)
    net_pct = float(s.get("net_pct") if s.get("net_pct") is not None
                   else (s.get("pct") or 0.0))
    seq = list(s.get("direction_seq") or [])
    outflow_days = sum(1 for x in seq if x == "outflow")
    hist = list(s.get("history") or [])
    net_sum_yi = 0.0
    for h in hist:
        v = h.get("net_yi")
        if v is None and h.get("main_net") is not None:
            try:
                v = float(h["main_net"]) / 1e8
            except Exception:
                v = 0.0
        net_sum_yi += float(v or 0.0)

    # D-1 单日净额 (history 最后一日; 若 summary 的 net_yi 即当日)
    net1_yi = net_yi
    if hist:
        try:
            last_h = hist[-1]
            v = last_h.get("net_yi")
            if v is None and last_h.get("main_net") is not None:
                v = float(last_h["main_net"]) / 1e8
            net1_yi = float(v if v is not None else net_yi)
        except Exception:
            net1_yi = net_yi
    metrics = {
        "direction": direction,
        "net_yi": round(net_yi, 2),
        "net1_yi": round(net1_yi, 2),
        "net_pct": round(net_pct, 2),
        "outflow_days": outflow_days,
        "net_sum_yi": round(net_sum_yi, 2),
        "history_n": len(hist),
        "trade_date": s.get("trade_date"),
    }

    weak = (
        net1_yi <= r["net1_yi_halt"]
        or net_pct <= r["net_pct_halt"]
        or net_sum_yi <= r["net5_yi_halt"]
        or outflow_days >= r["outflow_days_halt"]
    )
    soft = (
        net1_yi <= r["net1_yi_reduce"]
        or net_pct <= r["net_pct_reduce"]
        or net_sum_yi <= r["net5_yi_reduce"]
        or outflow_days >= r["outflow_days_reduce"]
    )
    if weak:
        mode, ok = "halt", False
        reason = (f"资金面弱 halt dir={direction} net1={net1_yi:.1f}亿 "
                  f"pct={net_pct:.1f}% outflow_days={outflow_days}")
    elif soft:
        mode, ok = "reduce", True
        reason = (f"资金面偏弱 reduce dir={direction} net={net_yi:.1f}亿 "
                  f"pct={net_pct:.1f}% outflow_days={outflow_days}")
    else:
        mode, ok = "full", True
        reason = (f"资金面正常 dir={direction} net={net_yi:.1f}亿 "
                  f"pct={net_pct:.1f}% outflow_days={outflow_days}")

    return {"mode": mode, "ok": ok, "reason": reason,
            "source": "fund_flow_api", "metrics": metrics, "rules": r}


def apply_env_to_limit(daily_limit: int, mode: str) -> int:
    """环境档位 → 每日名额 (halt=0, reduce=半, full=原值)。"""
    try:
        n = int(daily_limit or 0)
    except Exception:
        return daily_limit
    if mode == "halt":
        return 0
    if mode == "reduce":
        return max(1, n // 2) if n > 0 else n
    return n
