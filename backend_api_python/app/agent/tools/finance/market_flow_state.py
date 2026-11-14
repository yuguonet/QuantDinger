"""市场资金状态：大盘与行业/概念板块的结构化只读判据。

给模型的是可复核的分档事实与守恒约束，不是「出逃 / 切换」二元标签。
取数全部复用现有公开契约：资金流工具面 get_fund_flow（远端板块汇总口径）、
fund_flow_api.market_flow_summary（本地 kline_1m 全天口径）、交易日历目标日。
两套口径并列返回但不可互相印证，见返回体 caliber 与 warnings。
"""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...log import logger
from .fund_flow_tools import get_fund_flow

_SLOPE_POINTS: deque = deque(maxlen=200)


def _env_float(name: str, default: float) -> float:
    """阈值统一走环境变量；非法值回落默认值，不静默改变判据量级。"""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _looks_like_boards(rows: Any) -> bool:
    if not isinstance(rows, list) or len(rows) < 2:
        return False
    named = sum(1 for r in rows if isinstance(r, dict) and r.get("name"))
    return named >= len(rows) * 0.8


def _board_view(rows: Any, source_label: str, top_n: int) -> Dict[str, Any]:
    """板块明细折成宽度、守恒与 Top/Bottom 事实；空源不折叠成 0。"""
    if not _looks_like_boards(rows):
        return {"available": False, "reason": "板块明细为空、源不可用或为分钟点序列",
                "source": source_label, "count": 0}
    vals: List[tuple] = []
    for r in rows:
        n = _num(r.get("main_net")) if isinstance(r, dict) else None
        if n is not None:
            vals.append((n, r))
    if not vals:
        return {"available": False, "reason": "板块行无 main_net",
                "source": source_label, "count": len(rows)}
    ins = [v for v, _ in vals if v > 0]
    outs = [-v for v, _ in vals if v < 0]
    in_sum, out_sum = sum(ins), sum(outs)
    ordered = sorted(vals, key=lambda x: x[0], reverse=True)
    ratio = round(in_sum / out_sum, 4) if out_sum > 0 else None
    skew_ratio = _env_float("QD_FLOW_BOARD_SKEW_RATIO", 20.0)
    min_out = _env_float("QD_FLOW_BOARD_MIN_OUT_COUNT", 5)
    reasons: List[str] = []
    if len(outs) < min_out:
        reasons.append(f"净流出板块仅 {len(outs)} 个（阈值 {min_out:g}）")
    if ratio is not None and ratio > skew_ratio:
        reasons.append(f"守恒比 {ratio:g} > {skew_ratio:g}")

    def _row(v: float, r: Dict[str, Any]) -> Dict[str, Any]:
        return {"name": r.get("name"), "code": r.get("code"),
                "main_net_yi": round(v / 1e8, 2),
                "main_pct": r.get("main_pct"),
                "change_pct": r.get("change_pct")}

    return {
        "available": True,
        "source": source_label,
        "count": len(vals),
        "in_count": len(ins), "out_count": len(outs),
        "flat_count": len(vals) - len(ins) - len(outs),
        "in_sum_yi": round(in_sum / 1e8, 2),
        "out_sum_yi": round(out_sum / 1e8, 2),
        "net_sum_yi": round((in_sum - out_sum) / 1e8, 2),
        "conservation_ratio": ratio,
        "skewed": bool(reasons),
        "skew_reason": "；".join(reasons) or None,
        "usable_for_rotation": not bool(reasons),
        "breadth_in_pct": round(len(ins) / len(vals) * 100, 2),
        "top": [_row(v, r) for v, r in ordered[:top_n]],
        "bottom": [_row(v, r) for v, r in ordered[-top_n:]][::-1],
    }


def _percentiles(vals: List[float]) -> Optional[Dict[str, float]]:
    if not vals:
        return None
    s = sorted(vals)

    def _q(p: float) -> float:
        i = (len(s) - 1) * p
        lo = int(i)
        hi = min(lo + 1, len(s) - 1)
        return round(s[lo] + (s[hi] - s[lo]) * (i - lo), 2)

    return {"p10": _q(0.1), "p25": _q(0.25), "p50": _q(0.5),
            "p75": _q(0.75), "p90": _q(0.9)}


def _slope(timestamp: Any, net_yi: Optional[float],
           min_minutes: float) -> Dict[str, Any]:
    """同进程多时点累计净额的流出斜率；窗口不足明确不可用，不外推。"""
    if net_yi is None:
        return {"usable": False, "reason": "当日净额不可用"}
    ts = str(timestamp or "")
    try:
        cur = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return {"usable": False, "reason": "快照时间戳无法解析"}
    _SLOPE_POINTS.append((cur, net_yi))
    pts = [p for p in _SLOPE_POINTS if (cur - p[0]).days == 0 and p[0] <= cur]
    if len(pts) < 2:
        return {"usable": False, "window_minutes": 0, "points": len(pts),
                "reason": "仅单点快照，需跨轮累积"}
    first = pts[0]
    span = (cur - first[0]).total_seconds() / 60.0
    if span < min_minutes:
        return {"usable": False, "window_minutes": round(span, 1),
                "points": len(pts),
                "reason": f"窗口不足 {min_minutes:g} 分钟，禁止外推"}
    delta = round(net_yi - first[1], 2)
    return {"usable": True, "window_minutes": round(span, 1),
            "points": len(pts), "delta_yi": delta,
            "per_min_yi": round(delta / span, 2),
            "from_net_yi": round(first[1], 2), "to_net_yi": round(net_yi, 2)}


def get_market_flow_state(indicator: str = "今日", days: int = 30) -> dict:
    """大盘与行业/概念资金的结构化判定事实（只读，一次调用给出全部轴线）。

    Args:
        indicator: 板块时间维度，"今日" | "5日" | "10日"。
        days: 本地全天历史参照窗口（交易日行上限），默认 30。

    Returns:
        market        远端盘中板块汇总口径：净额/净占比/方向/分母说明。
        boards        行业与概念各自的板块级聚合（宽度、守恒比、Top/Bottom）。
        rotation      守恒比硬约束与 rotation_allowed；不足阈值禁止判「高低切换」。
        local_history 本地 kline_1m 全天多日方向、分位参照与新鲜度。
        slope         至少 30 分钟窗口的净额变化率；窗口不足明确不可用。
        breadth       板块级宽度（非全市场涨跌家数）。
        warnings      数据缺失与口径边界，缺失维度不得读成 0。
        guide         判定纪律，替代临场自拟阈值。

    易误解点：远端盘中累计值与本地全天量价方向净额是两套口径，方向相反也不
    构成矛盾，不得直接比分位或互相印证；概念源不可用时 available=False。
    """
    top_n = int(_env_float("QD_FLOW_TOP_N", 5))
    ratio_min = _env_float("QD_FLOW_ROTATION_MIN_RATIO", 0.5)
    slope_min = _env_float("QD_FLOW_SLOPE_MIN_MINUTES", 30)
    warnings: List[str] = []
    out: Dict[str, Any] = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "indicator": indicator, "warnings": warnings}
    try:
        from app.utils.trading_calendar import current_data_day
        out["target_day"] = current_data_day()
    except Exception as e:
        out["target_day"] = None
        warnings.append(f"目标数据日不可用: {type(e).__name__}: {e}")

    mkt: Dict[str, Any] = {}
    try:
        raw = get_fund_flow(scope="market")
        mkt = raw if isinstance(raw, dict) else {}
        if mkt.get("error"):
            warnings.append(f"大盘资金源失败: {mkt['error']}")
    except Exception as e:
        warnings.append(f"大盘资金源异常: {type(e).__name__}: {e}")
        logger.warning("get_market_flow_state market failed: %s", e)

    net = _num(mkt.get("main_net"))
    in_net, out_net = _num(mkt.get("in_net")), _num(mkt.get("out_net"))
    denom = (in_net + out_net) if (in_net is not None and out_net is not None) else None
    pct_ok = bool(denom and denom > 0)
    out["market"] = {
        "source": mkt.get("source"),
        "timestamp": mkt.get("timestamp"),
        "caliber": "board_aggregated_intraday",
        "net_yi": round(net / 1e8, 2) if net is not None else None,
        "in_net_yi": round(in_net / 1e8, 2) if in_net is not None else None,
        "out_net_yi": round(out_net / 1e8, 2) if out_net is not None else None,
        "net_pct": mkt.get("main_pct") if pct_ok else None,
        "net_pct_denominator": "in_net+out_net" if pct_ok else "不可用(源未给流入/流出总额)",
        "direction": ("inflow" if net > 0 else ("outflow" if net < 0 else "flat"))
                     if net is not None else "unknown",
        "boards_count": mkt.get("sectors_count"),
        "main_pct_note": "main_pct = 净额/(流入+流出总额)×100，分母不是成交额；"
                         "对外写「资金净占比」，不得写成「占成交额比例」，一次论证只引用一次",
        "label_note": "口径为板块汇总的量价方向净额，不含主力/散户单型分类；"
                      "对外写「资金净额」，不得写「主力净流出」",
    }
    if not pct_ok:
        warnings.append("大盘 main_pct 分母不可用（源未给 in_net/out_net），不得据此论证流出占比")

    board_rows: Dict[str, Any] = {}
    for label, scope in (("industry", "sector"), ("concept", "concept")):
        try:
            r = get_fund_flow(scope=scope, indicator=indicator)
            board_rows[label] = (r.get("boards") if isinstance(r, dict) else None)
            if isinstance(r, dict) and r.get("error"):
                warnings.append(f"{label} 板块源失败: {r['error']}")
        except Exception as e:
            board_rows[label] = None
            warnings.append(f"{label} 板块源异常: {type(e).__name__}: {e}")
            logger.warning("get_market_flow_state %s failed: %s", label, e)

    inline = mkt.get("data")
    src_ind = "market.data" if _looks_like_boards(inline) else "sector"
    ind_rows = inline if _looks_like_boards(inline) else board_rows.get("industry")
    out["boards"] = {
        "industry": _board_view(ind_rows, src_ind, top_n),
        "concept": _board_view(board_rows.get("concept"), "concept", top_n),
    }
    for label in ("industry", "concept"):
        if not out["boards"][label]["available"]:
            warnings.append(f"{label} 板块明细未获取（{out['boards'][label]['reason']}），不得读成 0")
    if not out["boards"]["concept"]["available"]:
        warnings.append(
            "概念板块未获取（源不可用）：结论里只能写「未获取」，"
            "禁止输出任何概念资金净额、流入/流出合计、板块数或守恒比数字，"
            "不得引用本轮未获取到的数值")
    for label, view in (("industry", out["boards"]["industry"]),
                        ("concept", out["boards"]["concept"])):
        if view["available"] and view.get("skewed"):
            warnings.append(
                f"{label} 板块榜单残缺（{view['skew_reason']}）：可能只含单侧 TopN，"
                "守恒比与流入/流出合计失真，不得用于资金性质判定，"
                "也不得与行业守恒比并列")

    cons = out["boards"]["industry"].get("conservation_ratio")
    out["rotation"] = {
        "conservation_ratio": cons,
        "threshold": ratio_min,
        "basis": out["boards"]["industry"].get("source"),
        "basis_skewed": out["boards"]["industry"].get("skewed"),
        "rotation_allowed": (cons >= ratio_min) if cons is not None else None,
        "rule": f"守恒比 Σ流入/|Σ流出| < {ratio_min:g} 时禁止输出「同一笔钱换地方 / 高低切换」",
        "definition": "加总为负即全市场净离场；仅凭少数板块净流入不能判资金在板块间切换",
    }
    if cons is None:
        warnings.append("守恒比不可算，切换与净离场均不得下结论")

    try:
        from app.market_cn.fund_flow_api import market_flow_summary
        hist = market_flow_summary(days=days)
        rows = hist.get("history") or []
        nets = [v for v in (_num(r.get("net_yi")) for r in rows) if v is not None]
        pcts = [v for v in (_num(r.get("net_pct")) for r in rows) if v is not None]
        out["local_history"] = {
            "caliber": "price_direction_full_day",
            "source": "qd_fund_flow_market / kline_1m",
            "sample_days": len(rows),
            "last_trade_date": hist.get("last_trade_date"),
            "target_day": hist.get("target_day"),
            "freshness": hist.get("freshness"),
            "stale": hist.get("stale"),
            "lag_trading_days": hist.get("lag_trading_days"),
            "missing_dates": hist.get("missing_dates"),
            "direction": hist.get("direction"),
            "net_yi": hist.get("net_yi"),
            "direction_seq": hist.get("direction_seq"),
            "net_yi_percentiles": _percentiles(nets),
            "net_pct_percentiles": _percentiles(pcts),
            "comparable_with_market": False,
            "note": "本地全天量价方向净额与远端盘中板块汇总不同口径，不可直接比分位或互相印证",
        }
        if hist.get("freshness") == "stale":
            warnings.append(f"本地全天表滞后 {hist.get('lag_trading_days')} 个交易日，"
                            "末行方向不得当作目标日方向")
        if hist.get("freshness") == "unknown":
            warnings.append("本地全天表目标日未知，多日方向不可外推")
    except Exception as e:
        out["local_history"] = {"available": False, "error": f"{type(e).__name__}: {e}"}
        warnings.append(f"本地多日方向不可用: {type(e).__name__}: {e}")

    out["slope"] = _slope(mkt.get("timestamp"),
                          out["market"]["net_yi"], slope_min)
    if not out["slope"]["usable"]:
        warnings.append(f"流出斜率不可用：{out['slope'].get('reason')}（禁止短窗外推）")

    ind_v = out["boards"]["industry"]
    con_v = out["boards"]["concept"]
    out["breadth"] = {
        "industry_in_pct": ind_v.get("breadth_in_pct") if ind_v["available"] else None,
        "concept_in_pct": con_v.get("breadth_in_pct") if con_v["available"] else None,
        "note": "板块级净流入宽度，不是全市场涨跌家数，不得写成「全市场涨跌家数」",
    }
    out["guide"] = [
        "不使用「出逃 / 切换」二元标签；按资金轴、板块结构轴、多日方向轴分档描述。",
        "main_pct 分母是流入+流出总额、不是成交额，对外写「资金净占比」，"
        "不得写成「占成交额比例」，一次论证只引用一次。",
        f"守恒比低于 {ratio_min:g} 时禁止输出「同一笔钱换地方 / 高低切换」；"
        "加总为负即全市场净离场，少数板块净流入不构成切换。",
        "板块宽度不是全市场涨跌家数；远端盘中值与本地全天值不可互相印证。",
        f"流出斜率窗口不足 {slope_min:g} 分钟不得外推。",
        "缺失维度必须写「未获取」，不得读成 0、平衡或净流入，也不得为缺失维度"
        "编造净额、合计、板块数或比值（含用虚构数字论证口径失真）。",
        "判定阈值以本返回体的 rotation.threshold 与 local_history 分位为准，不得临场自拟。",
        "板块资金榜的 change_pct 与 get_hot_sectors 的板块涨跌幅来自不同分类口径，"
        "不得混在同一段论证里比较。",
        "板块守恒比仅在 usable_for_rotation=true 时可用于判定；skewed=true 表示该榜"
        "只含单侧 TopN，禁止据此判资金迁移、流入强度或守恒关系。",
    ]
    return out
