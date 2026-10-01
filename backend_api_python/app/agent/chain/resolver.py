# -*- coding: utf-8 -*-
"""Resolver — 追责判定（阶段 A 代码算数 → 阶段 B LLM 判义），v1.1。

设计文档：`docs/追责系统重设计方案_20261001.md` §4（§10 修正纪要 v1.1）。

【两阶段分工】
  阶段 A（**确定性·代码**）：取真实行情、算数值偏差；数据缺失直接 `undecidable`
    + `data_missing`，**不猜**。
  阶段 B（**语义·LLM**）：只在数值算不出来"这算不算说中"时才请 LLM；默认关闭
    （`QD_CLAIM_JUDGE=1` 开启），因为方向/幅度/点位三类用代码锚点判已足够，
    且慢调容忍少量误判 —— 省下的是每天几十次 LLM 调用。

【修正② 落在哪（慢调、允许少量误判）】
  · 代码锚点**放宽**：方向对即 `hit`（0.75），不因幅度差就降级为 miss；
    只有方向反了才 `miss`。误差只记进 `deviation.magnitude`，供后期分析，
    不参与 verdict —— 慢调看的是长期分布，单条误判会被大数吸收。
  · judge 不设"人工一致率 ≥80%"强制门槛（那是 v1 的设计，对慢调过重）。
  · ★ 但 `undecidable` / `data_missing` **仍不计入权重**（那是常量污染，
    与"容忍误判"两回事）—— 判定逻辑在 `chain/evaluator.update_weights` 侧。

【修正③ 落在哪（多域通用留位）】
  本模块按 `domain` 分派 resolver，域 → 策略全部来自 `qd_domain_resolvers`
  配置表（`account_store.load_domain_policy`），**没有** `if domain == 'finance'`
  的硬编码。v1 只有 finance 是 enabled，v2 加域只需在表里插一行 + 实现对应
  `_resolve_<domain>`（分发表 `_RESOLVERS`）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from log import logger

JUDGE_VER = "v1.1"

# 三值化阈值（与 chain/schema.classify_return 同口径：±0.3%）
_FLAT_PCT = 0.3

# 幅度容差（修正②：放宽。慢调不看单条绝对值）
_MAG_TOL = 0.6


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


# ═══════════════════════════════════════════════════════════════
#  阶段 A：真实数据（确定性）
# ═══════════════════════════════════════════════════════════════
def _horizon_days(horizon: str) -> int:
    return {"T+1": 1, "T+3": 3, "T+5": 5, "2W": 10, "1M": 20}.get(horizon or "", 3)


def fetch_actual(subject: str, subject_kind: str, horizon: str,
                 exec_date: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """按 subject 取真实行情（复用 evaluator 的单一取数口径，含窗口极值）。

    Returns:
        {"pct","dir","high","low","close","as_of","source"} 或 None（取不到/不支持）
    """
    if not subject:
        return None
    kind = (subject_kind or "").strip().lower()
    if kind == "sector":
        return None                     # v1 不支持板块指数取数 ⇒ data_missing
    if kind not in ("stock", "index", "symbol"):
        return None
    try:
        from app.agent.chain.evaluator import _get_actual_return
        hold = _horizon_days(horizon)
        r = _get_actual_return(subject, exec_date or date.today(), hold,
                               with_extremes=True)
    except Exception as e:
        logger.debug("[Resolver] 取真实数据失败 %s: %s", subject, e)
        return None
    if not r:
        return None
    pct = float(r.get("pnl_pct") or 0.0)
    if pct > _FLAT_PCT:
        d = "bullish"
    elif pct < -_FLAT_PCT:
        d = "bearish"
    else:
        d = "neutral"
    try:
        as_of = date.fromisoformat(str(r.get("exit_date") or "")[:10])
    except (ValueError, TypeError):
        as_of = None
    return {
        "pct": round(pct, 2),
        "dir": d,
        "high": r.get("high"),
        "low": r.get("low"),
        "close": r.get("exit_close"),
        "as_of": as_of,
        "source": "CNStockDataSource.get_kline(1D)",
    }


# ═══════════════════════════════════════════════════════════════
#  阶段 A：偏差计算（纯函数，便于 smoke 直测）
# ═══════════════════════════════════════════════════════════════
def compute_deviation(claim: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, Any]:
    """按 claim_type 算分维度偏差（不含 verdict —— 那是 judge/锚点的事）。"""
    pred = claim.get("predicted") or {}
    ctype = claim.get("claim_type") or ""
    dev: Dict[str, Any] = {}

    if ctype == "direction":
        p, a = str(pred.get("dir") or ""), str(actual.get("dir") or "")
        dev["direction"] = {"hit": p == a, "pred": p, "act": a}

    elif ctype == "magnitude":
        try:
            pp = float(pred.get("pct"))
        except (TypeError, ValueError):
            pp = None
        ap = float(actual.get("pct") or 0.0)
        if pp is None:
            dev["magnitude"] = {"pred_pct": None, "act_pct": ap, "err_pct": None,
                                "err_rel": None}
        else:
            err = round(abs(pp - ap), 2)
            denom = max(abs(pp), 1.0)          # 预测 0.x% 时不让 err_rel 爆表
            dev["magnitude"] = {"pred_pct": pp, "act_pct": ap, "err_pct": err,
                                "err_rel": round(err / denom, 3),
                                "sign_match": (pp >= 0) == (ap >= 0)}

    elif ctype == "level":
        try:
            price = float(pred.get("price"))
        except (TypeError, ValueError):
            price = None
        hi, lo = actual.get("high"), actual.get("low")
        touched = None
        if price is not None and hi is not None and lo is not None:
            touched = bool(lo <= price <= hi)
        dev["level"] = {"pred_price": price, "kind": pred.get("kind", ""),
                        "act_high": hi, "act_low": lo, "touched": touched,
                        "act_close": actual.get("close")}
    return dev


# ═══════════════════════════════════════════════════════════════
#  阶段 A'：代码锚点判定（judge 关闭时的默认路径）
# ═══════════════════════════════════════════════════════════════
def verdict_by_rule(claim: Dict[str, Any], actual: Dict[str, Any],
                    dev: Dict[str, Any]) -> Dict[str, Any]:
    """用代码锚点出 verdict（**放宽版**：方向对即 hit，见模块头修正②）。"""
    ctype = claim.get("claim_type") or ""

    if ctype == "direction":
        d = dev.get("direction") or {}
        if d.get("hit"):
            return {"verdict": "hit", "verdict_score": 0.75,
                    "attribution": "", "note": "方向一致"}
        if d.get("act") == "neutral":
            return {"verdict": "partial", "verdict_score": 0.5,
                    "attribution": "noise",
                    "note": "实际横盘（±0.3% 内），方向既未兑现也未证伪"}
        return {"verdict": "miss", "verdict_score": 0.2,
                "attribution": "logic_error", "note": "方向相反"}

    if ctype == "magnitude":
        m = dev.get("magnitude") or {}
        rel = m.get("err_rel")
        if rel is None:
            return {"verdict": "undecidable", "verdict_score": None,
                    "attribution": "caliber", "note": "预测幅度无法解析"}
        if not m.get("sign_match"):
            return {"verdict": "miss", "verdict_score": 0.2,
                    "attribution": "logic_error", "note": "涨跌方向即相反"}
        if rel <= _MAG_TOL:
            return {"verdict": "hit", "verdict_score": 0.8,
                    "attribution": "", "note": f"幅度误差 {rel:.2f} 在容差内"}
        return {"verdict": "partial", "verdict_score": 0.55,
                "attribution": "timing", "note": f"幅度误差 {rel:.2f} 偏大但方向对"}

    if ctype == "level":
        lv = dev.get("level") or {}
        t = lv.get("touched")
        if t is None:
            return {"verdict": "undecidable", "verdict_score": None,
                    "attribution": "caliber", "note": "点位无法解析"}
        return ({"verdict": "hit", "verdict_score": 0.8, "attribution": "",
                 "note": "窗口内触及该价位"} if t else
                {"verdict": "miss", "verdict_score": 0.3,
                 "attribution": "timing", "note": "窗口内未触及"})

    return {"verdict": "undecidable", "verdict_score": None,
            "attribution": "caliber", "note": f"未知 claim_type: {ctype}"}


# ═══════════════════════════════════════════════════════════════
#  阶段 B：LLM judge（默认关闭）
# ═══════════════════════════════════════════════════════════════
JUDGE_SYSTEM = (
    "你是预测判定器。给定一条**模型此前做出的声明**、代码已经算好的真实数据与数值偏差，"
    "判定它算不算说中。\n"
    "规则（放宽口径，慢调用途，宁可判宽不判严）：\n"
    "1. 方向一致即算 hit（score ≥0.7），幅度差得多只降不否；\n"
    "2. 方向相反才 miss（score ≤0.3）；\n"
    "3. 原话模糊到无法判定（可能/不好说/没说清）⇒ undecidable，**不要**当成说错；\n"
    "4. 只输出 JSON，禁止额外文字。\n"
    '输出：{"verdict":"hit|partial|miss|undecidable","verdict_score":0~1,'
    '"attribution":"data_missing|logic_error|black_swan|timing|caliber|noise","note":"…"}'
)


def judge_one(claim: Dict[str, Any], actual: Dict[str, Any],
              dev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """请 LLM 判一条（返回 None 表示 judge 不可用 ⇒ 调用方回退代码锚点）。"""
    try:
        from app.agent.llm.factory import create_llm
        from app.agent.llm.base import ChatMessage
        import asyncio
        payload = {
            "claim_type": claim.get("claim_type"),
            "subject": claim.get("subject"),
            "claim_text": (claim.get("source_quote") or "")[:300],
            "predicted": claim.get("predicted"),
            "actual": {k: v for k, v in (actual or {}).items() if k != "as_of"},
            "deviation": dev,
            "confidence": claim.get("confidence"),
        }
        llm = create_llm(None)
        msgs = [ChatMessage(role="system", content=JUDGE_SYSTEM),
                ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))]
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                    resp = ex.submit(asyncio.run, llm.generate(msgs)).result()
            else:
                resp = loop.run_until_complete(llm.generate(msgs))
        except RuntimeError:
            resp = asyncio.run(llm.generate(msgs))
        text = (getattr(resp, "content", "") or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        obj = json.loads(m.group(0))
        return {"verdict": str(obj.get("verdict") or "undecidable"),
                "verdict_score": obj.get("verdict_score"),
                "attribution": str(obj.get("attribution") or ""),
                "note": str(obj.get("note") or "")[:300],
                "raw": text[:2000],
                "model": getattr(llm, "model", "") or ""}
    except Exception as e:
        logger.debug("[Resolver] judge 失败(回退规则判定): %s", e)
        return None


# ═══════════════════════════════════════════════════════════════
#  域分派（修正③：配置驱动，禁止硬编码域名）
# ═══════════════════════════════════════════════════════════════
def _resolve_finance(claim: Dict[str, Any], exec_date: Optional[date],
                     use_judge: bool) -> Dict[str, Any]:
    actual = fetch_actual(claim.get("subject") or "", claim.get("subject_kind") or "",
                          claim.get("horizon") or "T+3", exec_date)
    if not actual:
        return {"actual": {}, "deviation": {}, "verdict": "undecidable",
                "verdict_score": None, "attribution": "data_missing",
                "attribution_note": f"取不到真实数据（subject={claim.get('subject')}, "
                                    f"kind={claim.get('subject_kind')}）",
                "resolver_kind": "market_data", "sampled": False}
    dev = compute_deviation(claim, actual)
    out = verdict_by_rule(claim, actual, dev)
    kind, model, raw = "market_data", "", ""
    sampled = False
    if use_judge and out.get("verdict") != "undecidable":
        j = judge_one(claim, actual, dev)
        if j:
            # ★ 观测（2026-10-01）：开 judge 不等于该开。这里记录 judge 与规则锚点的
            #   一致率 —— 一致率够高就说明 judge 是白花钱，永远别开；分歧集中在某类
            #   claim 才说明规则有缺口。详情见 chain/judge_stats.py 的阈值表与
            #   "人工校准四步"（当前 human_verdict 列无写入路径，就是留给那一步的）。
            try:
                from app.agent.chain import judge_stats as _js
                _js.record_call()
                _js.record_result(
                    ok=True,
                    agreed=(j.get("verdict") == out.get("verdict")),
                    claim_type=claim.get("claim_type") or "")
            except Exception:
                pass
            out = {k: j.get(k) for k in ("verdict", "verdict_score", "attribution", "note")}
            kind, model, raw, sampled = "llm_judge", j.get("model", ""), j.get("raw", ""), True
        else:
            try:
                from app.agent.chain import judge_stats as _js
                _js.record_call()
                _js.record_result(ok=False)
            except Exception:
                pass
    return {"actual": {k: (v.isoformat() if isinstance(v, date) else v)
                       for k, v in actual.items()},
            "deviation": dev,
            "verdict": out.get("verdict") or "undecidable",
            "verdict_score": out.get("verdict_score"),
            "attribution": out.get("attribution") or "",
            "attribution_note": out.get("note") or "",
            "resolver_kind": kind, "resolver_model": model,
            "judge_raw": raw, "sampled": sampled}


_RESOLVERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "finance": _resolve_finance,
}


def resolve_claim(claim: Dict[str, Any], exec_date: Optional[date] = None,
                  use_judge: Optional[bool] = None,
                  policy: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """判定单条 claim（含写库）。域由配置驱动分派。"""
    from app.agent.chain import account_store as _as
    pol = policy or _as.load_domain_policy()
    dom = claim.get("domain") or "finance"
    cfg = pol.get(dom) or {}
    if not cfg.get("enabled", False):
        return {"skipped": True, "reason": f"domain_disabled:{dom}"}
    if use_judge is None:
        use_judge = _env_flag("QD_CLAIM_JUDGE", False) and bool(cfg.get("judge_enabled"))
    fn = _RESOLVERS.get(dom)
    if fn is None:                       # 域在策略表里开着但没实现 ⇒ 明确不做，不猜
        res = {"actual": {}, "deviation": {}, "verdict": "undecidable",
               "verdict_score": None, "attribution": "caliber",
               "attribution_note": f"域 {dom} 尚无 resolver 实现（v2 待接）",
               "resolver_kind": "none", "sampled": False}
    else:
        res = fn(claim, exec_date, use_judge)
    rid = _as.save_resolution(
        claim_id=int(claim.get("id") or 0),
        actual=res.get("actual") or {},
        actual_as_of=(res.get("actual") or {}).get("as_of"),
        data_source=(res.get("actual") or {}).get("source", ""),
        verdict=res.get("verdict") or "undecidable",
        verdict_score=res.get("verdict_score"),
        deviation=res.get("deviation") or {},
        attribution=res.get("attribution") or "",
        attribution_note=res.get("attribution_note") or "",
        resolver_kind=res.get("resolver_kind") or "market_data",
        resolver_model=res.get("resolver_model") or "",
        resolver_ver=JUDGE_VER,
        judge_raw=res.get("judge_raw") or "",
        sampled=bool(res.get("sampled")),
    )
    res["resolution_id"] = rid
    return res


def resolve_due_claims(limit: int = 50, use_judge: Optional[bool] = None) -> Dict[str, Any]:
    """扫到期 claim 并判定（evaluator 的盘后任务会调这里）。"""
    from app.agent.chain import account_store as _as
    items = _as.query_due_claims(limit=limit)
    stats: Dict[str, Any] = {"due": len(items), "resolved": 0, "undecidable": 0,
                             "skipped": 0, "errors": 0, "judged": 0, "details": []}
    seen_decisions = set()
    for c in items:
        try:
            # ★ exec_date 必须取**决策日**（decision.created_at），不能用今天：
            #   due_date 是入库时按决策日 + horizon 算的，判定时若用 today 作基准，
            #   K 线里找不到"今天之后"的窗口 ⇒ base_idx=None ⇒ 全部退化成
            #   data_missing（实测 3/3 全 undecidable，而实际数据完全取得到）。
            r = resolve_claim(c, exec_date=c.get("exec_date"), use_judge=use_judge)
            if r.get("skipped"):
                stats["skipped"] += 1
                continue
            stats["resolved"] += 1
            if r.get("verdict") == "undecidable":
                stats["undecidable"] += 1
            if r.get("sampled"):
                stats["judged"] += 1
            seen_decisions.add(int(c.get("decision_id") or 0))
            if len(stats["details"]) < 20:
                stats["details"].append({
                    "claim_id": c.get("id"), "type": c.get("claim_type"),
                    "subject": c.get("subject"), "verdict": r.get("verdict"),
                    "score": r.get("verdict_score")})
        except Exception as e:
            stats["errors"] += 1
            logger.warning("[Resolver] 判定 claim %s 失败: %s", c.get("id"), e)
    for did in seen_decisions:
        try:
            _as.refresh_decision_summary(did)
        except Exception:
            pass
    logger.info("[Resolver] 到期判定: %s",
                json.dumps({k: v for k, v in stats.items() if k != "details"},
                           ensure_ascii=False))
    return stats
