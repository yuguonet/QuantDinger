# -*- coding: utf-8 -*-
"""
monitor_tags.py — 回测校准后的 deep 软提示；不筛选、不打分、不替代个股证据。

1m全样本（2026-04-15~11-02，134交易日，deep可成交n=1862）用于校准旧短窗假设。
提示只表达分组关联，不代表因果，也不能单独决定推荐/排除。分析框架中的个股支撑、
盘中走势、可执行性与实时证据优先于历史分组。数据口径和冲突说明见技能参考文档。

run.py 在 topdown 的 deep 交付和「全部已封板」出口调用 attach_monitor()。
limit_up_ref 是板块情绪参照，不是买入清单；不得将历史提示解释成确定性接力规则。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_WEAK_MARKET_VERDICTS = {"weak", "shrink"}
_SMALL_CAP_CUTOFF = 80 * 1e8


def monitor_chips(row: Dict[str, Any], t_first: str = "",
                  kind: str = "", reason: str = "",
                  market_verdict: str = "",
                  market_idx_ret: Optional[float] = None) -> List[str]:
    """→ 重点监控格文案列表（空 = 无命中）。纯函数，无 IO。"""
    chips: List[str] = []
    t = str(t_first or "")[:5]
    chg = row.get("chg_pct")
    ret5 = row.get("ret5_pct")
    if kind == "limit_up":
        if t and t < "10:00":
            chips.append("早盘涨停只作板块情绪参考；次日机会须独立验证可买条件、承接和剩余空间")
        if "连板" in (reason or "") or (row.get("zt_streak") or 0) >= 2:
            chips.append("连板只代表情绪与辨识度，不等于次日可买；核对分歧、支撑和买入可执行性")
        return chips
    if t and t < "10:00":
        chips.append(
            "早盘信号长样本触板率低于午盘(6.4% vs 9.6%)，信号后当日回撤≥5%比例较高"
            "(2.0% vs 0.8%)、上冲也更大；先核承接与支撑，不因早而加分"
        )
    try:
        idx_ret = float(market_idx_ret) if market_idx_ret is not None else None
    except (TypeError, ValueError):
        idx_ret = None
    market_is_down = (
        idx_ret < 0 if idx_ret is not None
        else market_verdict in _WEAK_MARKET_VERDICTS
    )
    if market_is_down:
        try:
            circ_mv = float(row.get("circ_mv")) if row.get("circ_mv") is not None else None
        except (TypeError, ValueError):
            circ_mv = None
        if circ_mv is not None and circ_mv > 0:
            if circ_mv < _SMALL_CAP_CUTOFF:
                chips.append(
                    "弱市(指数分组跌幅<0)×流通市值<80亿：长样本深亏率3.4%、"
                    "触板率7.7%；提高支撑/承接确认要求，不据此单独排除"
                )
            else:
                chips.append(
                    "弱市(指数分组跌幅<0)×流通市值≥80亿：长样本深亏率1.6%、"
                    "触板率6.0%；更稳不等于更易触板，不据此单独优先"
                )
    try:
        chg_f = float(chg) if chg is not None else None
    except (TypeError, ValueError):
        chg_f = None
    try:
        ret5_f = float(ret5) if ret5 is not None else None
    except (TypeError, ValueError):
        ret5_f = None
    if row.get("loc") == "hot":
        chips.append(
            "位置透支档长样本深亏率6.5%(normal 2.5%、cool 1.4%)、触板率12.0%；"
            "核实支撑与盈亏比，不因高弹性忽略回撤"
        )
    if chg_f is not None and chg_f >= 5.0:
        chips.append(
            "决策日涨幅≥5%的长样本深亏率5.7%，高于2~5%组1.9%；"
            "虽触板止盈可改善均值，仍需核买入价与止损空间"
        )
    if chg_f is not None and chg_f < 0:
        chips.append(
            "跳变日收跌不能单独认定为洗盘；长样本T+3胜率44.9%，"
            "需看到支撑企稳/反包证据再转强判断"
        )
    if ret5_f is not None and ret5_f < 0:
        chips.append(
            "前5日下跌组长样本深亏率1.7%、T+3均值+0.33%；"
            "只作位置背景，仍须验证支撑和趋势止跌，不把超跌当买点"
        )
    return chips


def attach_monitor(out: Dict[str, Any]) -> Dict[str, Any]:
    """给 limit_up_ref / picked_detail / analyzed_rank 挂 monitor 键（就地修改并返回）。

    analyzed_rank 行本身没有 t_first/kind——从同批 picked_detail 按 code 合并；
    classic 链路无 picked_detail 时跳过时段/类型判据（只用 chg/ret5 两格）。
    """
    pmap = {str(x.get("code")): x for x in (out.get("picked_detail") or [])}
    funnel = out.get("funnel") or {}
    market_verdict = str(funnel.get("verdict") or "")
    market_idx_ret = funnel.get("idx_ret")
    for coll in (out.get("limit_up_ref"), out.get("picked_detail")):
        for it in coll or []:
            chips = monitor_chips(it, str(it.get("t_first") or ""),
                                  str(it.get("kind") or ""),
                                  str(it.get("reason") or ""),
                                  market_verdict, market_idx_ret)
            if chips:
                it["monitor"] = chips
    for r in out.get("analyzed_rank") or []:
        p = pmap.get(str(r.get("code"))) or {}
        row = {**r, "circ_mv": p.get("circ_mv")}
        chips = monitor_chips(row, str(p.get("t_first") or ""),
                              str(p.get("kind") or ""),
                              str(p.get("reason") or ""),
                              market_verdict, market_idx_ret)
        if chips:
            r["monitor"] = chips
    return out
