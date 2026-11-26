# -*- coding: utf-8 -*-
"""单票诊断的「大盘强弱 × 市场宽度 × 流通市值」分层事实模块。

规则来源：用户裁定 2026-11-20 —— 大盘走弱时权重股护盘，跌家数多、涨家数少，
大流通市值相对占优；反之（大盘走强、涨多跌少）小盘相对占优。
项目内既有支撑证据：market-screener/monitor_tags.py 弱市 × 流通市值 <80 亿
深亏率 3.4% vs ≥80 亿 1.6%；_topdown.py 记录 ≥80 亿深亏率较低但触板率也较低。
该规则是描述性分层事实，不是方向票，不改变七维投票、_DIMS 与 confidence。

口径边界：市场宽度 = realtime_snapshot 横截面逐票 change_pct 的正负家数，
与板块判定同源。kline_index_5m 的 up_count/down_count 实测整列为 NULL，
get_market_overview 只是上证/深成抽样，两者都不能当全市场宽度。
流通股本取 screen_query._shares_map（当前快照，82.6% 覆盖、已停更），
缺失显式 None，不补默认值、不按市值硬筛。阈值全部走 env。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ._inner import env_float as _env_f, num as _num


def _now_hhmm() -> str:
    """分时截断时刻（Asia/Shanghai 墙上钟）；非交易时段取 15:00。显式钉时区防 docker/UTC 误判。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    return now if ("09:30" <= now <= "11:30" or "13:00" <= now <= "15:00") else "15:00"


def _breadth() -> Dict[str, Any]:
    """全市场涨跌家数（snapshot 横截面逐票 change_pct），失败返回 reason 不猜。"""
    try:
        from ....data_sources import screen_query as sq
        from ....utils.trading_calendar import current_data_day
        t_to = _now_hhmm()
        panel = sq.cross_section(as_of=t_to, end_day=current_data_day(),
                                 features=["change_pct"])
    except Exception as e:
        return {"ok": False, "reason": f"市场宽度取数失败: {type(e).__name__}: {e}"}
    rows = [r for r in (panel.get("rows") or []) if _num(r.get("change_pct")) is not None]
    if not rows:
        return {"ok": False, "reason": "市场宽度 snapshot 无有效涨跌"}
    up = sum(1 for r in rows if float(r["change_pct"]) > 0)
    down = sum(1 for r in rows if float(r["change_pct"]) < 0)
    return {"ok": True, "up": up, "down": down, "as_of": panel.get("as_of"),
            "day": panel.get("day"), "source": "realtime_snapshot",
            "caliber": panel.get("caliber")}


def _float_mcap_yi(code: str, price: Any) -> Optional[float]:
    """流通市值（亿元）= 流通股本 × 现价；股本缺失返回 None。"""
    px = _num(price)
    if px is None or px <= 0:
        return None
    try:
        from ....data_sources.screen_query import _shares_map
        shares = _shares_map([str(code)])
    except Exception:
        return None
    circ = _num(shares.get(str(code)))
    if circ is None or circ <= 0:
        return None
    return round(circ * px / 1e8, 2)


def build(code: str, price: Any, market_state: Dict[str, Any]) -> Dict[str, Any]:
    """产出分层事实。favored=large/small/None；fit=favorable/unfavorable/unknown。"""
    cut = _env_f("QD_STOCK_ANALYZER_CAP_CUTOFF_YI", 80.0)
    weak_at = _env_f("QD_STOCK_ANALYZER_WEAK_SCORE", 45.0)
    strong_at = _env_f("QD_STOCK_ANALYZER_STRONG_SCORE", 55.0)
    score = _num((market_state or {}).get("market_mood_score"))
    cautious = (market_state or {}).get("cautious")
    breadth = _breadth()
    mcap = _float_mcap_yi(code, price)

    weak = score is not None and (score <= weak_at or cautious is True)
    strong = score is not None and score >= strong_at and cautious is not True
    down_gt_up = (breadth.get("down", 0) > breadth.get("up", 0)) if breadth.get("ok") else None
    up_gt_down = (breadth.get("up", 0) > breadth.get("down", 0)) if breadth.get("ok") else None

    favored: Optional[str] = None
    state = "unknown"
    if weak and down_gt_up:
        favored, state = "large", "weak_defensive"
    elif strong and up_gt_down:
        favored, state = "small", "strong_offensive"
    elif score is not None and breadth.get("ok"):
        state = "mixed"

    tier = None if mcap is None else ("large" if mcap >= cut else "small")
    fit = "unknown"
    if tier and favored:
        fit = "favorable" if tier == favored else "unfavorable"

    parts = []
    if score is not None:
        parts.append(f"大盘情绪分 {score:g}{'，走弱标记' if cautious is True else ''}")
    if breadth.get("ok"):
        parts.append(f"涨跌家数 {breadth['up']}/{breadth['down']}")
    else:
        parts.append(breadth.get("reason", "市场宽度缺数"))
    if mcap is not None:
        parts.append(f"流通市值 {mcap:g} 亿（分层线 {cut:g} 亿 ⇒ {tier}）")
    else:
        parts.append("流通股本缺失，市值分层未知")
    if favored:
        parts.append({"large": "弱市权重护盘，大流通市值相对占优",
                      "small": "强市普涨，小流通市值相对占优"}[favored])
    elif state == "mixed":
        parts.append("大盘强弱与市场宽度背离，不做市值偏向")
    else:
        parts.append("判定条件不足，不做市值偏向")

    return {"ok": score is not None and breadth.get("ok") and mcap is not None,
            "state": state, "favored": favored, "fit": fit,
            "market_mood_score": score, "cautious": cautious,
            "breadth": breadth, "float_mcap_yi": mcap, "cap_tier": tier,
            "cutoff_yi": cut, "basis": "；".join(parts)}
