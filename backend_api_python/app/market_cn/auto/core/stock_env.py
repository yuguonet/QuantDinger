# -*- coding: utf-8 -*-
"""core/stock_env.py — 个股层环境/事件门 (2026-09-28 B)

数据源 (唯一公共 API): app.market_cn.market_data_api
  - stock_flow / stock_flow_history   个股资金流
  - lhb_events / lhb_recent_count     龙虎榜

与 market_env 同一纪律:
  - **规则门**, 不是评分;
  - 缺数据 fail-open (放行), 绝不静默丢信号;
  - 先纸面, 不改判定, 由调用方决定是否生效。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_RULES = {
    # 个股当日净占比 % (net/turnover); 资金大幅流出 → 可选减仓/不追
    "stock_pct_halt": -8.0,
    "stock_pct_reduce": -3.0,
    # 近 20 日龙虎榜次数 (负向风险标记, 归因: 0 次 > 1 次以上)
    "lhb20_reduce": 3,
    "lhb20_halt": 5,
}


def _rules(overrides: Optional[dict] = None) -> dict:
    r = dict(DEFAULT_RULES)
    if overrides:
        r.update({k: v for k, v in overrides.items() if v is not None})
    return r


def stock_flow_gate(code: str, date: Optional[str] = None,
                    rules: Optional[dict] = None) -> Dict[str, Any]:
    """个股资金流门 (market_data_api.stock_flow)。

    Returns: {mode: full|reduce|halt|unknown, ok, reason, metrics, source}
    """
    r = _rules(rules)
    try:
        from app.market_cn.market_data_api import stock_flow
        f = stock_flow(code, date=date) or {}
    except Exception as e:
        logger.warning("[stock_env] stock_flow(%s) fail-open: %s", code, e)
        return {"mode": "full", "ok": True, "reason": "无个股资金流数据",
                "source": "none", "metrics": {}, "rules": r}

    net = f.get("total_main_net")
    if net is None:
        net = f.get("main_net") or f.get("net_yi")
    try:
        net = float(net or 0.0)
    except Exception:
        net = 0.0
    # 近似净占比: 个股 snapshot 路径无 turnover 时用 |net| 自比无意义 → 只用方向/绝对额
    metrics = {"net": round(net, 2), "approx": bool(f.get("approx", False)),
               "source": f.get("source") or f.get("source", "fund_flow_api")}
    # 若有 net_pct 字段则用
    pct = f.get("net_pct") or f.get("main_pct")
    if pct is not None:
        metrics["net_pct"] = float(pct)
        p = float(pct)
        if p <= r["stock_pct_halt"]:
            return {"mode": "halt", "ok": False,
                    "reason": f"个股资金流出重 pct={p:.1f}%",
                    "source": "market_data_api", "metrics": metrics, "rules": r}
        if p <= r["stock_pct_reduce"]:
            return {"mode": "reduce", "ok": True,
                    "reason": f"个股资金偏流出 pct={p:.1f}%",
                    "source": "market_data_api", "metrics": metrics, "rules": r}
    return {"mode": "full", "ok": True, "reason": "个股资金流正常",
            "source": "market_data_api", "metrics": metrics, "rules": r}


def lhb_gate(code: str, as_of: Optional[str] = None,
             days: int = 20,
             rules: Optional[dict] = None) -> Dict[str, Any]:
    """龙虎榜门: 近 N 日上榜次数 = 风险标记 (归因: 次数多 → 次日差)。

    用途: **排除/降级**, 不是买入信号。
    """
    r = _rules(rules)
    try:
        from app.market_cn.market_data_api import lhb_recent_count
        n = lhb_recent_count(code, days=days, as_of=as_of)
    except Exception as e:
        logger.warning("[stock_env] lhb(%s) fail-open: %s", code, e)
        return {"mode": "full", "ok": True, "reason": "无龙虎榜数据",
                "source": "none", "metrics": {}, "rules": r}
    metrics = {"lhb_count": int(n), "window_days": int(days)}
    if n >= r["lhb20_halt"]:
        return {"mode": "halt", "ok": False,
                "reason": f"近{days}日上榜{n}次 (≥{r['lhb20_halt']})",
                "source": "market_data_api", "metrics": metrics, "rules": r}
    if n >= r["lhb20_reduce"]:
        return {"mode": "reduce", "ok": True,
                "reason": f"近{days}日上榜{n}次 (≥{r['lhb20_reduce']})",
                "source": "market_data_api", "metrics": metrics, "rules": r}
    return {"mode": "full", "ok": True,
            "reason": f"近{days}日上榜{n}次",
            "source": "market_data_api", "metrics": metrics, "rules": r}


def stock_event_gate(code: str, date: Optional[str] = None,
                     rules: Optional[dict] = None) -> Dict[str, Any]:
    """个股层合成门: 资金流 + 龙虎榜 → 最严档位。"""
    a = stock_flow_gate(code, date=date, rules=rules)
    b = lhb_gate(code, as_of=date, rules=rules)
    order = {"halt": 3, "reduce": 2, "full": 1, "unknown": 0}
    worst = max((a, b), key=lambda x: order.get(x.get("mode"), 0))
    return {
        "mode": worst.get("mode", "full"),
        "ok": worst.get("ok", True),
        "reason": f"flow[{a.get('mode')}:{a.get('reason')}] lhb[{b.get('mode')}:{b.get('reason')}]",
        "flow": a,
        "lhb": b,
    }
