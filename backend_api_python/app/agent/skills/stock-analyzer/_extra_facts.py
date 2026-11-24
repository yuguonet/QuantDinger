# -*- coding: utf-8 -*-
"""stock-analyzer/_extra_facts.py —— 补证事实层：把技能外的八类事实按需取回并归一化。

【职责】只做「调现成工具 + 拆信封 + 压体量 + 标口径」，不复制任何算法、不改任何工具：
  · build_chip         筹码分布（成本/获利套牢/集中区间/筹码峰/筹码支撑压力）
  · build_flow_rhythm  资金节奏（全天净额 + 尾盘连续同向段 + 多日净额）
  · build_intel        情报面（综合分/方向/一票否决/新闻标题）
  · build_capital      融资与基本面（融资余额变化/股东户数/财报关键值）
  · build_intraday     日内结构（VWAP + 开盘区间 + 分时均线，单日 1m）
  · build_multiframe   多周期方向（日线定势 + 15m/5m 节奏 + 相对大盘修正注记）
  · build_valuation    估值快照（PE-TTM/PB/市值）
  · build_flow_daily   日频资金（连续流出天数 + 5日符号 + 20日净额与去极值）
  · build_flow_driver  资金驱动节奏（20日内逐日 资金×涨幅 形态/累计/推动效率/控盘信号）

【关键设计点】
  · 四源与 deep/板块/大盘同批并发，任一失败由调用方写 missing，不伪造空事实；
  · 筹码价位与 `_levels` 的 8 类方法价位**不同源、不合并排序**，只并列陈述；
  · flow 原始分钟点可达 3 万字符，本层只留聚合值与尾盘连续段（上限 12 条）；
  · 所有单位按工具返回的键名照抄（_yi / _wan / 百分数），本层不做换算；
  · 日内结构与多周期方向必须显式传取数目标日（current_data_day）；工具的 date 默认值是
    「今天」，非交易日会取到无数据的当天，必须由上调用方钉死；
  · 日频资金与分钟资金同口径不同粒度，20 日净额可不同（实测 +2.65 亿 vs +3.47 亿），
    两者并列陈述、不合并成一个数；
  · build_flow_driver 只做「价量资金配合形态」分解，四档单型（large_net/super_net）
    在本地恒 None，绝不臆造庄家持仓成本/利润空间；形态是描述性事实、不进投票。

【易错点】
  · `get_chip_distribution` 与 `batch_valuation_compare` 返回裸结构（前者单股 dict、
    后者 `{total, stocks}`），不走 `_unwrap`；其余六个返回 `{count, data:{code: ...}}`；
  · `profit_ratio` 是 0~1 小数，展示用 `profit_ratio_pct` 字符串，勿重复乘 100；
  · flow 的 `main_net` 是价格方向近似值，非主力/机构分类，口径必须随事实一起透出；
  · `search_intel` 顶层与 data[code] 都有同名字段，统一取 data[code] 避免单/多股漂移；
  · 估值与日内结构只陈述事实，不进七维投票、不改 horizons；
  · `vwap.approx=true` 时是 hlc3 典型价近似，不是精确成交均价；
  · build_flow_driver 的日涨幅由日线 close 相邻两日得出，首日无前收记 None（不回落 0），
    非交易日/停牌日以有 bar 的交易日序列为准。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _unwrap(payload: Any, code: str) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        one = data.get(code)
        return one if isinstance(one, dict) else None
    return payload


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _peaks(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in (raw or [])[:5]:
        if not isinstance(p, dict):
            continue
        px, st = _num(p.get("price")), _num(p.get("strength"))
        if px is not None and st is not None:
            out.append({"price": px, "strength": st})
    return out


def build_chip(code: str) -> Dict[str, Any]:
    from ...tools.finance.chip_distribution import get_chip_distribution

    d = _unwrap(get_chip_distribution(code), code)
    if d is None:
        return {"ok": False, "reason": "筹码分布未返回该股数据"}
    band90 = {"lower": _num(d.get("concentration_90_lower")),
              "upper": _num(d.get("concentration_90_upper"))}
    band70 = {"lower": _num(d.get("concentration_70_lower")),
              "upper": _num(d.get("concentration_70_upper"))}
    return {
        "ok": True,
        "avg_cost": _num(d.get("avg_cost")),
        "current_price": _num(d.get("current_price")),
        "profit_ratio": _num(d.get("profit_ratio")),
        "loss_ratio": _num(d.get("loss_ratio")),
        "profit_ratio_pct": d.get("profit_ratio_pct"),
        "loss_ratio_pct": d.get("loss_ratio_pct"),
        "concentration_90": d.get("concentration_90"),
        "band_90": band90,
        "band_70": band70,
        "support_prices": [x for x in (d.get("support_prices") or [])[:4] if _num(x) is not None],
        "resistance_prices": [x for x in (d.get("resistance_prices") or [])[:4] if _num(x) is not None],
        "chip_peaks": _peaks(d.get("chip_peaks")),
        "analyzed_days": d.get("analyzed_days"),
        "caliber": "日K线成交量按三角分布分配至价格档并做时间衰减；与 levels 的8类方法价位不同源，"
                   "不合并排序，只并列陈述",
    }


def build_flow_rhythm(code: str) -> Dict[str, Any]:
    from ...tools.finance.fund_flow_tools import get_fund_flow

    one = _unwrap(get_fund_flow(scope="stock", codes=code), code)
    if one is None:
        return {"ok": False, "reason": "资金流未返回该股数据"}
    tail_n = max(1, _env_int("QD_STOCK_ANALYZER_TAIL_MINUTES", 10))
    pts = [p for p in (one.get("data") or []) if isinstance(p, dict)
           and _num(p.get("main_net")) is not None]
    tail = pts[-tail_n:]
    streak: List[Dict[str, Any]] = []
    if tail:
        last = float(tail[-1]["main_net"])
        sign = 1 if last >= 0 else -1
        for p in reversed(tail):
            v = float(p["main_net"])
            if (1 if v >= 0 else -1) != sign:
                break
            streak.append({"time": str(p.get("time") or ""), "wan": round(v / 1e4, 1)})
        streak.reverse()
    return {
        "ok": True,
        "points": one.get("points"),
        "total_main_net": _num(one.get("total_main_net")),
        "main_pct": _num(one.get("main_pct")),
        "recent_5d_main_net": _num(one.get("recent_5d_main_net")),
        "recent_20d_main_net": _num(one.get("recent_20d_main_net")),
        "recent_5d_net_ratio": _num(one.get("recent_5d_net_ratio")),
        "recent_20d_net_ratio": _num(one.get("recent_20d_net_ratio")),
        "tail_minutes": tail_n,
        "tail_net_wan": round(sum(float(p["main_net"]) for p in tail) / 1e4, 1) if tail else None,
        "tail_streak": streak[:12],
        "source": one.get("source"),
        "approx": bool(one.get("approx")),
        "caliber": str(one.get("caliber") or "价格方向净额近似（非主力/机构分类）"),
    }


def build_intel(code: str, name: str = "") -> Dict[str, Any]:
    from ...tools.finance.news_search_tools import search_intel

    one = _unwrap(search_intel(scope="stock", codes=code, name=name), code)
    if one is None:
        return {"ok": False, "reason": "情报检索未返回该股数据"}
    news = []
    for n in (one.get("news") or [])[:3]:
        if not isinstance(n, dict):
            continue
        news.append({k: n.get(k) for k in ("title", "published", "sentiment") if n.get(k)})
    return {
        "ok": True,
        "label": one.get("label"),
        "composite_score": _num(one.get("composite_score")),
        "direction": one.get("direction"),
        "veto": bool(one.get("veto")),
        "veto_article": one.get("veto_article"),
        "n_news": one.get("count"),
        "news": news,
        "caliber": "新闻/公告聚合摘要；只陈述情报事实，不进七维投票、不代替价格证据",
    }


def build_capital(code: str) -> Dict[str, Any]:
    from ...tools.finance.data_tools import get_capital_summary

    one = _unwrap(get_capital_summary(code), code)
    if one is None:
        return {"ok": False, "reason": "资本结构未返回该股数据"}
    s = one.get("summary") if isinstance(one.get("summary"), dict) else {}
    margin = s.get("margin") if isinstance(s.get("margin"), dict) else {}
    holders = s.get("holders") if isinstance(s.get("holders"), dict) else {}
    fin = s.get("financials") if isinstance(s.get("financials"), dict) else {}
    return {
        "ok": True,
        "overall_signal": s.get("overall_signal"),
        "margin": {"rz_balance_yi": _num(margin.get("rz_balance_yi")),
                   "rz_5d_change_pct": _num(margin.get("rz_5d_change_pct")),
                   "signal": margin.get("signal")},
        "holders": {"holder_count": holders.get("holder_count"),
                    "change_pct": _num(holders.get("change_pct")),
                    "latest_date": holders.get("latest_date")},
        "financials": {"report_date": fin.get("report_date"),
                       "revenue_wan": _num(fin.get("revenue_wan")),
                       "net_profit_wan": _num(fin.get("net_profit_wan")),
                       "total_assets_wan": _num(fin.get("total_assets_wan")),
                       "total_liabilities_wan": _num(fin.get("total_liabilities_wan")),
                       "cash_wan": _num(fin.get("cash_wan")),
                       "financial_unit": fin.get("financial_unit")},
        "caliber": "单位按工具返回的 _yi/_wan 键照抄，不自行换算；只陈述当前披露事实，"
                   "不进七维投票、不构成买卖指令",
    }


def build_intraday(code: str, day: str, minutes: int = 15) -> Dict[str, Any]:
    from ...tools.finance.intraday_tools import get_intraday_structure

    one = _unwrap(get_intraday_structure(code, date=day, or_minutes=minutes), code)
    if one is None:
        return {"ok": False, "reason": "日内结构未返回该股数据"}
    vwap = one.get("vwap") if isinstance(one.get("vwap"), dict) else {}
    op = one.get("opening_range") if isinstance(one.get("opening_range"), dict) else {}
    ma = one.get("intraday_ma") if isinstance(one.get("intraday_ma"), dict) else {}
    return {
        "ok": True,
        "date": one.get("date"),
        "source": one.get("source"),
        "is_forming_bar": bool(one.get("is_forming_bar")),
        "bars": one.get("bars"),
        "vwap": {"value": _num(vwap.get("value")),
                 "price_vs_vwap_pct": _num(vwap.get("price_vs_vwap_pct")),
                 "above": vwap.get("above"), "approx": bool(vwap.get("approx"))},
        "opening_range": {"minutes": op.get("minutes"),
                          "high": _num(op.get("high")), "low": _num(op.get("low")),
                          "breakout_up": bool(op.get("breakout_up")),
                          "breakout_down": bool(op.get("breakout_down")),
                          "pos": op.get("pos")},
        "intraday_ma": {"ma5": _num(ma.get("ma5")), "ma20": _num(ma.get("ma20")),
                        "ma60": _num(ma.get("ma60")), "align": ma.get("align")},
        "caliber": "单日 1m 日内结构；盘后取 kline_1m，盘中当日未回填时取 realtime_snapshot 合成；"
                   "VWAP 为 hlc3 近似值，非精确成交均价",
    }


def build_multiframe(code: str, day: str) -> Dict[str, Any]:
    from ...tools.finance.multiframe_tools import get_multiframe_direction

    one = _unwrap(get_multiframe_direction(code, date=day), code)
    if one is None:
        return {"ok": False, "reason": "多周期方向未返回该股数据"}
    consensus = one.get("consensus") if isinstance(one.get("consensus"), dict) else {}
    rel = one.get("rel_strength") if isinstance(one.get("rel_strength"), dict) else {}
    directions = consensus.get("directions") if isinstance(consensus.get("directions"), dict) else {}
    return {
        "ok": True,
        "date": one.get("date"),
        "source": one.get("source"),
        "is_forming_bar": bool(one.get("is_forming_bar")),
        "directions": {k: directions.get(k) for k in ("daily", "m15", "m5")},
        "consensus": {"state": consensus.get("state"), "t1_bias": consensus.get("t1_bias"),
                      "timing": consensus.get("timing"), "agree": consensus.get("agree"),
                      "count": consensus.get("count"),
                      "missing": list(consensus.get("missing") or [])[:3]},
        "rel_strength": {"label": rel.get("label"), "excess_pct": _num(rel.get("excess_pct")),
                         "role": rel.get("role")},
        "caliber": "日线定势优先于分时节奏；t1_bias 是 T+1 主方向描述而非概率，"
                   "rel_strength 是大盘修正注记非主论据；不进七维投票、不改 horizons",
    }


def build_valuation(code: str) -> Dict[str, Any]:
    from ...tools.finance.data_tools import batch_valuation_compare

    raw = batch_valuation_compare(code)
    if not isinstance(raw, dict) or raw.get("error"):
        return {"ok": False, "reason": f"估值快照未返回：{raw.get('error') if isinstance(raw, dict) else '空响应'}"}
    one = next((s for s in (raw.get("stocks") or [])
                if isinstance(s, dict) and s.get("code") == code), None)
    if one is None:
        return {"ok": False, "reason": "估值快照未返回该股数据"}
    return {
        "ok": True,
        "price": _num(one.get("price")),
        "pe_ttm": _num(one.get("pe_ttm")),
        "pb": _num(one.get("pb")),
        "mcap_yi": _num(one.get("mcap_yi")),
        "float_mcap_yi": _num(one.get("float_mcap_yi")),
        "caliber": "行情源估值快照，随价格与财报更新；只陈述当前值，"
                   "不进七维投票、不构成买卖指令",
    }


def build_flow_daily(code: str) -> Dict[str, Any]:
    from ...tools.finance.fund_flow_tools import get_fund_flow

    one = _unwrap(get_fund_flow(scope="stock_daily", codes=code, days=15), code)
    if one is None:
        return {"ok": False, "reason": "日频资金流未返回该股数据"}
    return {
        "ok": True,
        "total_days": one.get("total_days"),
        "recent_5d_main_net": _num(one.get("recent_5d_main_net")),
        "recent_20d_main_net": _num(one.get("recent_20d_main_net")),
        "recent_5d_net_ratio": _num(one.get("recent_5d_net_ratio")),
        "recent_20d_net_ratio": _num(one.get("recent_20d_net_ratio")),
        "main_net_streak": one.get("main_net_streak"),
        "main_net_streak_dir": one.get("main_net_streak_dir"),
        "recent_5d_sign": one.get("recent_5d_sign"),
        "max_main_net_day": one.get("max_main_net_day"),
        "recent_20d_main_net_ex_max": _num(one.get("recent_20d_main_net_ex_max")),
        "caliber": "日频价格方向净额近似，非主力/机构分类；与 flow_rhythm 同口径不同粒度，"
                   "20 日净额可不同（实测 +2.65 亿 vs +3.47 亿），并列陈述、不合并",
    }


def _shape_of(net_ratio: Optional[float], chg: Optional[float]) -> Optional[str]:
    if net_ratio is None or chg is None:
        return None
    if net_ratio < 0 and chg > 0:
        return "惜售"       # 净流出仍涨
    if net_ratio > 0 and chg <= 0:
        return "承接"       # 净流入却跌
    if net_ratio > 0 and chg > 0:
        return "推进"       # 净流入且涨
    if net_ratio < 0 and chg <= 0:
        return "共振"       # 净流出且跌
    return None


def build_flow_driver(code: str, bars: List[Dict[str, Any]],
                      days: Optional[int] = None) -> Dict[str, Any]:
    """20 日内逐日「资金×涨幅」配合形态：只分解价量资金关系，不还原庄家持仓。

    `bars` 由调用方复用 deep 同一份日线，避免重复取数；按 date 对齐逐日净额与日涨幅。
    """
    from ...tools.finance.fund_flow_tools import get_fund_flow_daily

    n = _env_int("QD_STOCK_ANALYZER_FLOW_DRIVER_DAYS", 20) if days is None else days
    one = _unwrap(get_fund_flow_daily(code, days=n), code)
    if one is None:
        return {"ok": False, "reason": "日频资金流未返回该股数据"}
    rows = [r for r in (one.get("data") or []) if isinstance(r, dict) and r.get("date")]
    if not rows:
        return {"ok": False, "reason": "日频资金无逐日明细"}

    close_by_date: Dict[str, float] = {}
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        d = str(b.get("time") or b.get("date") or "")[:10]
        c = _num(b.get("close"))
        if d and c is not None:
            close_by_date[d] = c

    seq: List[Dict[str, Any]] = []
    prev_close: Optional[float] = None
    for r in rows[-n:]:
        d = str(r.get("date") or "")[:10]
        c = close_by_date.get(d)
        chg = None
        if c is not None and prev_close:
            chg = round((c / prev_close - 1) * 100, 2)
        if c is not None:
            prev_close = c
        net = _num(r.get("main_net"))
        ratio = _num(r.get("net_ratio"))
        seq.append({
            "date": d,
            "net_wan": round(net / 1e4, 1) if net is not None else None,
            "net_ratio": ratio,
            "chg_pct": chg,
            "shape": _shape_of(ratio, chg),
        })

    counts = {"推进": 0, "承接": 0, "惜售": 0, "共振": 0}
    for s in seq:
        if s["shape"] in counts:
            counts[s["shape"]] += 1

    eff = [round(s["chg_pct"] / abs(s["net_ratio"]), 2)
           for s in seq
           if s["chg_pct"] is not None and s["chg_pct"] > 0
           and s["net_ratio"] not in (None, 0)]

    tail_hold = 0
    for s in reversed(seq):
        if s["shape"] in ("惜售", "承接"):
            tail_hold += 1
        else:
            break

    span_net = sum(s["net_wan"] for s in seq if s["net_wan"] is not None)
    return {
        "ok": True,
        "days": n,
        "sequence": seq,
        "counts": counts,
        "span_net_wan": round(span_net, 1),
        "efficiency": {
            "median": round(sorted(eff)[len(eff) // 2], 2) if eff else None,
            "samples": len(eff),
            "note": "仅统计上涨日：涨幅%÷|净额占比%|，即每 1% 资金占比推动的涨幅；"
                    "只作序列内横向比较，不换算金额、不推持仓",
        },
        "control_signal": {
            "tail_hold_days": tail_hold,
            "note": ("末端连续惜售/承接天数（净额方向与涨幅背离），"
                     "只描述控盘或分歧迹象，不进投票、不推持仓成本"),
        },
        "caliber": ("逐日资金为价格方向净额近似（涨分钟额−跌分钟额），非主力/机构分类；"
                    "四档单型本地恒 None，不含持股成本，绝不推算庄家筹码与利润空间；"
                    "形态只是价量资金配合的描述性分解"),
    }
