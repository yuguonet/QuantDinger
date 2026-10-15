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

from . import label_spec
from ..log import logger

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
    """horizon 标签 → 持有交易日数。

    ★ 委派 `chain/label_spec`（v5 P1-1 起的**唯一真源**），**不在此另写一份映射**。
      口径曾在 evaluator / claims / resolver 三处各写一份且数值不同，
      改一处忘两处 = 静默漂移（老教训）。
    ★ 这里**允许兜底**（未知标签 → 3）：resolver 面对的是模型自由文本，
      识别不了是常态而非异常。全项目仅 `label_spec.resolve_hold_days` 提供该兜底。
    """
    return label_spec.resolve_hold_days(horizon, default=3)


def _days_label(days: int) -> str:
    """持有交易日数 → horizon 标签（双窗口输出用）。"""
    return label_spec.hold_label(days)


def _label_windows() -> List[str]:
    """双窗口的**窗口集合**（env 可配，禁止硬编码）。

    `QD_LABEL_WINDOWS` 默认 `T+1,T+3`。语义（v5 §0.4 用户拍板）：
      · **T+3 是主判据** —— 进 `verdict` / `verdict_score`、进权重；
      · **T+1 只报告** —— 挂 `actual.windows` / `deviation.windows` 供快速诊断，
        不进权重（否则同一预测被记两票，与 G24 是同一类错误）。

    ★ 集合里**不含** claim 自身 horizon 时，主窗口仍会被算（主窗口恒在），
      集合只决定**额外**多算哪几个观测点。
    """
    raw = os.getenv("QD_LABEL_WINDOWS", "T+1,T+3").strip()
    out: List[str] = []
    for tok in raw.split(","):
        t = tok.strip().upper()
        # 未识别的标签**丢弃**而不是兜底成默认值：静默兜底会让口径悄悄漂移。
        # 成员资格直接问 label_spec（唯一真源），不在此另写一份标签清单。
        if t and label_spec.hold_days(t) is not None and t not in out:
            out.append(t)
    return out


def _actual_view(r: Dict[str, Any]) -> Dict[str, Any]:
    """把 `_get_actual_return` 的原始结果（主窗口或 `windows` 子项）转成 actual 形态。

    ★ 主窗口与附加窗口**共用这一个转换** —— 保证两者同构、口径一致。
      若给附加窗口另写一份，就会出现「T+1 用一套阈值、T+3 用另一套」的静默漂移。
    """
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
    try:
        ah = int(r.get("hold_days") or 0)
    except (TypeError, ValueError):
        ah = 0
    exp = r.get("hold_days_expected")
    return {
        "pct": round(pct, 2),
        "dir": d,
        "high": r.get("high"),
        "low": r.get("low"),
        "close": r.get("exit_close"),
        # ★ as_of 必须是 **ISO 字符串**，不是 date 对象（2026-10-15 修，P0）：
        #   `actual` 整个 dict（含 `windows` 里嵌套的本函数产物）最终要
        #   `json.dumps` 进 qd_agent_resolutions.actual（JSON 列）——
        #   date 对象会让 json.dumps 抛 TypeError。
        #   10-13 D3 双窗口引入 `windows` 后，`_resolve_finance` 三个返回点
        #   的**浅层** `{k: v.isoformat() ...}` 推导罩不住嵌套层 ⇒ 82/86 条
        #   判定写入全部静默失败（被 save_resolution 的 except 吞掉，
        #   resolve_due_claims 还把失败计成 resolved ⇒ 报表显示一切正常）。
        #   在**生产者**保证类型正确，而不是在消费侧逐个补推导。
        "as_of": as_of.isoformat() if as_of else None,
        "source": "CNStockDataSource.get_kline(1D)",
        "hold_days": ah,
        "hold_days_expected": exp,
        "truncated": bool(r.get("truncated")),
    }


def fetch_actual(subject: str, subject_kind: str, horizon: str,
                 exec_date: Optional[date] = None,
                 extra_windows: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """按 subject 取真实行情（复用 evaluator 的单一取数口径，含窗口极值）。

    ★ 2026-10-13 新增 `windows`（v5 阶段 D3 方案 D：双窗口标签）：
      除主窗口（= claim 的 horizon，默认 T+3）外，按 `QD_LABEL_WINDOWS`
      额外多算若干观测点（默认 T+1），**一次取数**全部给出。
      语义红线：**只有主窗口进 `verdict`**，附加窗口只挂在
      `actual.windows` / `deviation.windows` 供快速诊断，不进权重。

    Args:
        extra_windows: 覆盖 env 的附加窗口集合（测试/特殊用途）。
            与主 horizon 相同的标签会被**自动剔除**（主窗口恒在，不重复算）。

    ★ 2026-10-12 新增 `truncated` 标记（G0 Bug C）：
      `_get_actual_return` 内部是 `exit_idx = min(base_idx + hold_days, len-1)`，
      数据不足时**不报错**，而是用最后一根 K 线交卷。实测：
        请求 T+10 → 实际只算 2 天（6 例中 4 例被截断）。
      若不拦，T+N 标签会静默变成更短窗口（与设计 §4.2「主窗口唯一」直接冲突）。
      旧链路早已修过此坑（evaluator.py:260-267 视界门②，2026-09-27 修 E2），
      本函数是把它**移植**过来（同一口径、不另写一套）。

    Returns:
        {"pct","dir","high","low","close","as_of","source"}
        或 None（取不到/不支持）
        额外：`"truncated": True` 表示窗口被截断（调用方**不得定论**）。
    """
    if not subject:
        return None
    kind = (subject_kind or "").strip().lower()
    if kind == "sector":
        return None                     # v1 不支持板块指数取数 ⇒ data_missing
    if kind not in ("stock", "index", "symbol"):
        return None
    main = (horizon or "T+3").strip().upper()
    hold = _horizon_days(main)
    # 附加窗口：剔除与主窗口相同的标签（主窗口**恒在**，不重复算）
    extra = [w for w in (extra_windows if extra_windows is not None
                         else _label_windows())
             if w != main]
    holds = [_horizon_days(w) for w in extra]
    try:
        from .evaluator import _get_actual_return
        r = _get_actual_return(subject, exec_date or date.today(), hold,
                               with_extremes=True, extra_holds=holds)
    except Exception as e:
        logger.debug("[Resolver] 取真实数据失败 %s: %s", subject, e)
        return None
    if not r:
        return None
    out = _actual_view(r)
    # ── 双窗口（v5 D3 方案 D）：把 `windows`（键为天数字符串）映射成 horizon 标签
    #    主窗口与附加窗口共用 `_actual_view`，保证同构。
    wins: Dict[str, Any] = {}
    for h_str, w in (r.get("windows") or {}).items():
        if not isinstance(w, dict):
            continue
        try:
            h = int(h_str)
        except (TypeError, ValueError):
            continue
        wins[_days_label(h)] = _actual_view(w)
    out["windows"] = wins
    return out


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
#  2026-10-14 外置 prompts/verdict_judge.md
# ═══════════════════════════════════════════════════════════════
from ..prompts import load_prompt as _load_prompt
JUDGE_SYSTEM = _load_prompt("verdict_judge")


def judge_one(claim: Dict[str, Any], actual: Dict[str, Any],
              dev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """请 LLM 判一条（返回 None 表示 judge 不可用 ⇒ 调用方回退代码锚点）。"""
    try:
        # 【2026-10-13 P1】旧实现为兼容"可能在事件循环里/可能不在"搓了三层分支
        # （get_event_loop → ThreadPoolExecutor → asyncio.run）。统一出口是同步的
        # `complete()`，两种上下文都直接可用，分支全部消失。
        from app.agent.llm import complete
        payload = {
            "claim_type": claim.get("claim_type"),
            "subject": claim.get("subject"),
            "claim_text": (claim.get("source_quote") or "")[:300],
            "predicted": claim.get("predicted"),
            "actual": {k: v for k, v in (actual or {}).items() if k != "as_of"},
            "deviation": dev,
            "confidence": claim.get("confidence"),
        }
        resp = complete([
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ])
        text = (resp.content or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        obj = json.loads(m.group(0))
        return {"verdict": str(obj.get("verdict") or "undecidable"),
                "verdict_score": obj.get("verdict_score"),
                "attribution": str(obj.get("attribution") or ""),
                "note": str(obj.get("note") or "")[:300],
                "raw": text[:2000],
                "model": resp.model or ""}
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

    # ── 退市 / 长期停牌单列（v4 G18 ⇒ v5 P0-1）：防幸存者偏差 ──────────
    # 判据必须**两条同时成立**（误判代价不对称，宁可漏判不可错判）：
    #   ① 重试已耗尽：verify_fail_count >= QD_CLAIM_MAX_RETRY（默认 5）
    #   ② 最后一根 K 线距今超过 QD_DELISTED_STALE_DAYS 个自然日（默认 15）
    # 只满足 ① = 数据缺口 ⇒ 保持 undecidable 继续重试（不占真实战绩）；
    # 只满足 ② = 可能只是数据源延迟 ⇒ 同样不判。
    # ★ verdict_score 必须是 0.0 而不是 None：None 在 weight_feed 里不计入，
    #   等于把最差的尾巴又丢回去了，修了和没修一样。
    _max_retry = int(os.getenv("QD_CLAIM_MAX_RETRY", "5"))
    _stale_days = int(os.getenv("QD_DELISTED_STALE_DAYS", "15"))
    _fail_n = int(claim.get("verify_fail_count") or 0)
    # ★ 2026-10-15 修：旧代码读 `actual.get("exit_date")` —— `fetch_actual` 返回的
    #   actual **从来没有这个键**（原始 exit_date 只存在于 `_get_actual_return` 的
    #   返回值里，`_actual_view` 只转存 as_of）⇒ `_last_kline` 恒为空串 ⇒ 本分支
    #   （G18 退市/长期停牌单列，防幸存者偏差）**永不可达**，功能静默失效。
    #   改读实际存在的 `as_of`（`_actual_view` 产出，ISO 字符串）。
    _last_kline = str(actual.get("as_of") or "")[:10]
    if _fail_n >= _max_retry and _last_kline:
        try:
            if (date.today() - date.fromisoformat(_last_kline)).days > _stale_days:
                return {
                    "actual": {k: (v.isoformat() if isinstance(v, date) else v)
                               for k, v in actual.items()},
                    "deviation": {}, "verdict": "delisted", "verdict_score": 0.0,
                    "attribution": "delisted",
                    "attribution_note": (
                        f"重试耗尽（{_fail_n} 次）且最后 K 线 {_last_kline} 距今超过 "
                        f"{_stale_days} 天：判定退市 / 长期停牌，按最差路径计入"
                        f"（防幸存者偏差，v4 G18）"),
                    "resolver_kind": "market_data", "sampled": False,
                }
        except (ValueError, TypeError):
            pass  # 日期不可解析 ⇒ 无法判定，落到后续常规分支

    # ── 视界门②（G0 Bug C）：窗口被截断**不得定论**（ports evaluator.py:260-267）
    #   数据不足（长期停牌/退市/数据缺口/提前扫描）时 _get_actual_return 会用
    #   最后一根 K 线交卷。若不拦，T+N 标签会静默缩成更短窗口，而事后无法识别。
    #   语义选择：仍归 data_missing（与旧链路 `_bump_eval_failure` 一致）
    #   ⇒ 由 account_store 的重试治理累积次数，**不终态化**，不占真实战绩。
    if actual.get("truncated"):
        return {"actual": {k: (v.isoformat() if isinstance(v, date) else v)
                            for k, v in actual.items()},
                "deviation": {}, "verdict": "undecidable", "verdict_score": None,
                "attribution": "data_missing",
                "attribution_note": (
                    "窗口被截断（实际持有 %s 根 < 应有 %s 根）：数据未走完，"
                    "不得定论（视界门②）"
                    % (actual.get("hold_days"), actual.get("hold_days_expected"))),
                "resolver_kind": "market_data", "sampled": False}

    dev = compute_deviation(claim, actual)
    # ── 双窗口偏差（v5 D3 方案 D）：每个观测点各算一份，挂 `deviation.windows`
    #    ★ **只作诊断，不参与 verdict** —— 主判据恒为主窗口 T+3（v5 §0.4 用户拍板）。
    #      若让 T+1 也进 verdict，同一条预测就会被记两票（与 G24 同一类错误）。
    _wd: Dict[str, Any] = {}
    for _lab, _wa in (actual.get("windows") or {}).items():
        if isinstance(_wa, dict):
            _wd[_lab] = compute_deviation(claim, _wa)
    if _wd:
        dev["windows"] = _wd
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
                from . import judge_stats as _js
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
                from . import judge_stats as _js
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
    from . import account_store as _as
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
    from . import account_store as _as
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
            # ★ 2026-10-15 修：写库失败（resolution_id=None，save_resolution 的
            #   fail-open 路径）**不许计成 resolved** —— 旧代码无条件
            #   `resolved += 1`，判定实际没落库也报"已判定"，报表说谎
            #   （10-15 实测：86 条声称 resolved，实际 0 条落库）。
            if r.get("resolution_id") is None:
                stats["errors"] += 1
                logger.warning("[Resolver] claim %s 判定未落库"
                               "（resolution_id=None，按错误计）", c.get("id"))
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
