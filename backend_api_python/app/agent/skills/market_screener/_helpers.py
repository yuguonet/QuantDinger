# -*- coding: utf-8 -*-
"""
market_screener/_helpers.py — 事实与证据层（不做选股决策）

2026-09-29 改版（v6）：
    旧版把「分策略筛选规则 / 排序 / 情绪分桶裁剪」硬编码在本模块，LLM 只剩格式化输出，
    自由度≈0，分析深度被单一技术打分封顶。本版职责收敛为三条：

    1. 取数与计算：日线、量价结构、资金流、指标快照、T+1 标定预测（app.watchlist.predict）
    2. 打客观标记：hard_exclusions() —— ST/退市、换手不足、价格带、涨停买不进、数据缺失
    3. 汇总证据：profile_candidates() 产出每只候选的「证据卡」，**不淘汰任何一只**

    决策（选谁、留几只、要不要折价、如何叙述）由 LLM 依据 SKILL.md 的分析框架完成。

设计红线：
    · 本模块禁止根据「策略偏好」剔除候选；只标注客观事实与不可执行项
    · 所有回退必须留痕（missing / errors），不静默吞异常 —— 否则 LLM 会把数据缺口当成无信号
"""

from datetime import date, datetime, time
from concurrent.futures import ThreadPoolExecutor

from log import logger
from typing import Any, Dict, List, Optional

# 证据卡默认上限：候选池可达数百只，全量画像会撑爆上下文（trim 优先保证「有证据」而非「全」）
_PROFILE_LIMIT_DEFAULT = 20
# 并发取数线程数：IO 密集（DB 查询），线程池有效（GIL 在 IO 等待期会释放）
_FETCH_WORKERS = 8
# 单批取数等待上限：DB/网络抖动时不拖垮整轮选股（超时项计入 missing）
_FETCH_TIMEOUT_S = 20.0


def select_strategy() -> str:
    """根据当前时间选择交易日策略。"""
    now = datetime.now()
    # 非交易日 → 盘后
    if now.weekday() >= 5:
        return "post_market"
    t = now.time()
    if time(9, 30) <= t < time(14, 30):
        return "intraday"
    if time(14, 30) <= t < time(15, 0):
        return "eod"
    return "post_market"


def resolve_names(code_list: List[str]) -> Dict[str, str]:
    """批量解析股票名称。返回 {code: name}。"""
    if not code_list:
        return {}
    try:
        from tools.finance.data_tools import get_realtime_quote
        q = get_realtime_quote(",".join(code_list))
        name_map = {}
        if isinstance(q, dict):
            data = q.get("data", q)
            if isinstance(data, dict):
                for code, info in data.items():
                    if isinstance(info, dict) and info.get("name"):
                        name_map[code] = info["name"]
        return name_map
    except Exception:
        return {}


def analyze_batch(items: list, fn, max_candidates: int = 8, errors: list = None) -> list:
    """批量分析，逐项调用分析函数。

    Args:
        items: 待分析项列表，每项会作为 fn 的参数
        fn: 分析函数，接收一项 item，返回分析结果 dict 或 None
        max_candidates: 最多分析数量，默认 8
        errors: 可选，收集 {"code":..., "error":...}；不留痕会让 LLM 把失败误读为「无信号」

    Returns:
        非 None 的分析结果列表
    """
    results = []
    for i, item in enumerate(items):
        if i >= max_candidates:
            break
        code = item.get("code", "") if isinstance(item, dict) else ""
        try:
            r = fn(item)
            if r is not None:
                results.append(r)
            elif isinstance(errors, list):
                errors.append({"code": code, "error": "分析返回 None（多为数据不足，非无信号）"})
        except Exception as e:
            if isinstance(errors, list):
                errors.append({"code": code, "error": f"{type(e).__name__}: {e}"})
            logger.debug("[MktScreen] analyze_batch %s 失败: %s", code, e)
    return results


# ═══════════════════════════════════════════════════════════════
#  客观事实：硬标记 / 技术画像 / 资金流
# ═══════════════════════════════════════════════════════════════

def _f(v: Any, default: float = 0.0) -> float:
    """容错转 float。"""
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def hard_exclusions(item: Dict, strategy: str = "") -> List[str]:
    """客观不可交易 / 低可信标记（不构成选股意见，只陈述事实）。

    Args:
        item: 候选 dict（含 name/source/turnover_pct/price/…）
        strategy: intraday|eod|post_market —— 仅影响「涨停是否算买不进」的措辞

    Returns:
        中文短标记列表，空列表 = 无硬障碍
    """
    flags: List[str] = []
    name = str(item.get("name") or "").upper()
    src = str(item.get("source") or "")
    if "ST" in name or src == "ST股" or "退" in name:
        flags.append("ST/退市风险")

    trn = _f(item.get("turnover_pct"))
    if 0 < trn < 2:
        flags.append(f"换手仅{trn:.1f}%")

    price = _f(item.get("price") or item.get("close"))
    if price:
        if price < 2:
            flags.append("低价<2元")
        elif price > 300:
            flags.append("高价>300元")

    if item.get("limit_locked"):
        # 盘中/尾盘是「当日买不进」的硬约束；盘后是复盘次日机会，只作提示
        flags.append("涨停封板-当日买不进" if strategy in ("intraday", "eod") else "涨停封板")

    if item.get("suspended"):
        flags.append("停牌")
    return flags


def soft_warnings(item: Dict, market: dict, strategy: str = "") -> List[str]:
    """软提示：历史经验型风控（不是不可交易事实）。

    与 hard_exclusions 的区别：hard 是「买不了/不该买」的客观事实，warnings 是
    「大概率不好」的经验判断 —— profile 里两者都给 LLM，由它决定是否采纳；
    filter_candidates（保守兼容路径）才会把 warnings 一并剔除。
    """
    out: List[str] = []
    if _mood_regime(market) == "weak":
        src = str(item.get("source") or "")
        if any(k in src for k in ("连板", "4IN1", "龙回头", "涨停")) and not (item.get("reason") or ""):
            out.append("弱势市涨停活跃源无题材支撑(退潮风险)")
    trn = _f(item.get("turnover_pct"))
    if trn > 25:
        out.append(f"换手{trn:.0f}%过热")
    return out


def _last(seq: List[Optional[float]]) -> Optional[float]:
    return seq[-1] if seq else None


def tech_summary(bars: List[Dict], code: str = "", name: str = "") -> Dict[str, Any]:
    """从日线算出「位置 / 量能 / 强度 / 涨停史」的客观事实（不含评分）。

    之所以不含评分：评分语义已统一为 P(T+1 涨)×100（见 attach_prediction），
    这里的字段只是让 LLM 能解释「为什么」，或自行推翻自带的模型分。
    """
    if not bars or len(bars) < 2:
        return {"ok": False, "reason": "日线不足"}

    from .common import compute_ma, compute_rsi, get_limit_pct, is_limit_locked

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]

    today, prev = bars[-1], bars[-2]
    close = today["close"]
    chg = (close - prev["close"]) / prev["close"] * 100 if prev["close"] else 0.0

    prev_vols = vols[-6:-1]
    vr = (today["volume"] / (sum(prev_vols) / len(prev_vols))) if prev_vols and sum(prev_vols) > 0 else None

    ma5, ma10, ma20 = compute_ma(closes, 5), compute_ma(closes, 10), compute_ma(closes, 20)
    ma20_v = _last(ma20)
    dist_ma20 = ((close - ma20_v) / ma20_v * 100) if ma20_v else None

    win20 = min(20, len(bars))
    hi20, lo20 = max(highs[-win20:]), min(lows[-win20:])
    pos20 = ((close - lo20) / (hi20 - lo20) * 100) if hi20 > lo20 else None

    ret5 = ((close / closes[-6]) - 1) * 100 if len(closes) > 6 else None
    ret20 = ((close / closes[-win20]) - 1) * 100 if len(closes) > win20 else None

    # 连续涨停（用涨跌停幅度近似，尾部倒序）
    limit_pct = get_limit_pct(code, name)
    streak = 0
    for i in range(len(bars) - 1, 0, -1):
        pc = bars[i - 1]["close"]
        if pc and (bars[i]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
            streak += 1
        else:
            break

    intraday_pos = None
    rng = today["high"] - today["low"]
    if rng > 0:
        intraday_pos = (close - today["low"]) / rng * 100

    return {
        "ok": True,
        "bars": len(bars),
        "close": round(close, 2),
        "change_pct": round(chg, 2),
        "amplitude_pct": round(rng / prev["close"] * 100, 2) if prev["close"] and rng else None,
        "close_pos_in_day": round(intraday_pos, 1) if intraday_pos is not None else None,
        "vol_ratio": round(vr, 2) if vr else None,
        "rsi14": round(compute_rsi(closes)[-1], 1),
        "ma5": round(_last(ma5), 2) if _last(ma5) else None,
        "ma10": round(_last(ma10), 2) if _last(ma10) else None,
        "ma20": round(ma20_v, 2) if ma20_v else None,
        "dist_ma20_pct": round(dist_ma20, 2) if dist_ma20 is not None else None,
        "ma_bull_stack": bool(_last(ma5) and _last(ma10) and ma20_v and _last(ma5) > _last(ma10) > ma20_v),
        "pos_in_20d_pct": round(pos20, 1) if pos20 is not None else None,
        "is_20d_high": bool(pos20 is not None and pos20 >= 99),
        "ret_5d_pct": round(ret5, 2) if ret5 is not None else None,
        "ret_20d_pct": round(ret20, 2) if ret20 is not None else None,
        "limit_up": bool(is_limit_locked(code, name, close, prev["close"])),
        "zt_streak": streak,
        "prev_zt_streak": _prev_streak(bars, limit_pct, streak),
    }


def _prev_streak(bars: List[Dict], limit_pct: float, streak: int) -> int:
    """截至上一交易日结束时的连板高度（判断「连板后首阴」等形态用）。"""
    idx = len(bars) - 1 - max(streak, 0)
    n = 0
    while idx > 0:
        pc = bars[idx - 1]["close"]
        if pc and (bars[idx]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
            n += 1
            idx -= 1
        else:
            break
    return n


def flow_summary(code: str) -> Dict[str, Any]:
    """个股资金流客观事实（单位：万元）。失败返回 ok=False + 原因，不静默。"""
    try:
        from .common import call_tool
        ff = call_tool("get_fund_flow_realtime", code=code)
        if not isinstance(ff, dict) or ff.get("error"):
            return {"ok": False, "reason": (ff or {}).get("error", "无返回")}
        # 三对账修正（2026-09-30）：tape 返回的真实键是 total_main_net（单位元）——
        # 旧读 main_net_inflow 恒 None，主力净流入被算成恒 0；amount/turnover 在该
        # 返回里同样不存在，拿不到就如实给 None（不再瞎除），调试字段 raw_keys 移除。
        main_net = _f(ff.get("total_main_net", ff.get("main_net_inflow"))) / 1e4
        amount = _f(ff.get("amount") or ff.get("turnover") or ff.get("amount_wan"))
        return {
            "ok": True,
            "main_net_wan": round(main_net, 1),
            "net_pct_of_amount": round(main_net * 1e4 / amount * 100, 2) if amount else None,
        }
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════════════════════════
#  T+1 标定预测（P(次日涨)×100）
# ═══════════════════════════════════════════════════════════════
# 复用 app.watchlist.predict 的已标定模型（AUC≈0.61），而非手拍技术分。
# 2026-09-29：旧 enrich_predictive 会按情绪分桶**替 LLM 裁剪掉**候选（门槛 + 条数上限），
# 现改为「只给分数和建议」，裁剪权交还 LLM（详见 SKILL.md 的评分语义段）。

_MKT_KLINES_CACHE = {"data": None}


def _market_klines():
    if _MKT_KLINES_CACHE["data"] is None:
        try:
            from app.watchlist.predict import load_market_klines
            _MKT_KLINES_CACHE["data"] = load_market_klines(120) or []
        except Exception:
            _MKT_KLINES_CACHE["data"] = []
    return _MKT_KLINES_CACHE["data"]


def _norm_bars(bars: list) -> list:
    """fetch_kline 的 time 是日期串；predict._by_day 要 int 时间戳。"""
    from datetime import datetime as _dt
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        x = dict(b)
        tm = x.get("time")
        if isinstance(tm, str):
            try:
                x["time"] = int(_dt.strptime(tm[:10], "%Y-%m-%d").timestamp())
            except Exception:
                continue
        out.append(x)
    return out


def _mood_regime(market: dict) -> str:
    """情绪分桶：strong / neutral / weak（只作上下文标签，不再用于裁剪）。"""
    market = market or {}
    mood = str(market.get("mood") or "")
    try:
        ms = float(market.get("mood_score", 50) or 50)
    except Exception:
        ms = 50.0
    if mood in ("弱势", "偏弱") or ms < 40:
        return "weak"
    if mood in ("偏强",) or ms >= 70:
        return "strong"
    return "neutral"


def predict_one(code: str, bars: List[Dict], regime: str = "neutral") -> Dict[str, Any]:
    """给单只票打 P(T+1 涨) —— 客观模型输出 + 可选的弱势折价标记（折价不自动生效）。

    Returns:
        {ok, p_up, exp_ret_bp, model:{beta,regime,zhuang,...}, note}
    """
    try:
        from app.watchlist.predict import predict_next_day
    except Exception as e:
        return {"ok": False, "reason": f"预测模块不可用: {e}"}
    bars = _norm_bars(bars or [])
    if not bars:
        return {"ok": False, "reason": "日线为空"}
    try:
        pred = predict_next_day(bars, _market_klines())
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    if not pred or pred.get("score") is None:
        return {"ok": False, "reason": "模型未给出分数"}
    p_up = float(pred.get("p_up") or 0.5)
    return {
        "ok": True,
        "p_up": round(p_up, 4),
        "exp_ret_bp": pred.get("exp_ret_bp"),
        "model": {
            "beta": (pred.get("beta") or {}).get("beta"),
            "regime": (pred.get("beta") or {}).get("regime"),
            "zhuang": (pred.get("zhuang") or {}).get("score"),
            "market_p_up": (pred.get("market") or {}).get("p_up"),
        },
        "mood_regime": regime,
        "suggest_dir": "bullish" if p_up >= 0.55 else ("bearish" if p_up <= 0.45 else "neutral"),
    }


def attach_prediction(items: List[Dict], market: dict = None) -> List[Dict]:
    """批量挂 P(T+1 涨) 分数。不改变成员，不裁剪，不重排。"""
    if not items:
        return []
    regime = _mood_regime(market)
    from .common import fetch_kline
    out = []
    for it in items:
        item = dict(it)
        item.setdefault("missing", [])
        # 保留原始分（对照用），再让 score 统一为 P(次日涨)×100
        item.setdefault("tech_score", item.get("score", 50))
        bars = item.get("_bars") or []
        if not bars and item.get("code"):
            bars = fetch_kline(item["code"], days=120)
        pr = predict_one(item.get("code", ""), bars, regime)
        item["has_pred"] = bool(pr.get("ok"))
        if pr.get("ok"):
            item["score"] = round(pr["p_up"] * 100, 1)
            item["direction"] = pr["suggest_dir"]
            item["p_up"] = pr["p_up"]
            item["exp_ret_bp"] = pr["exp_ret_bp"]
            item["pred_model"] = pr["model"]
            item["suggest_dir"] = pr["suggest_dir"]
        else:
            item["missing"].append(f"预测缺失:{pr.get('reason')}")
        item["_pred"] = pr
        out.append(item)
    return out


def enrich_predictive(results: list, market: dict = None) -> list:
    """兼容旧名：打分 + 按情绪门槛标记 selected + 重排（tests/test_market_screener_pred.py 契约）。

    2026-09-29：主流程改用 attach_prediction()（只打分不裁剪，取舍交给 LLM）。
    本函数保留「门槛裁剪 + selected 标记」语义，供既有调用方/回归测试使用，请勿在新代码中调用。
    """
    if not results:
        return []
    regime = _mood_regime(market)
    thr = {"weak": 0.55, "neutral": 0.50, "strong": 0.48}[regime]
    cap = {"weak": 5, "neutral": 8, "strong": 10}[regime]

    items = attach_prediction(list(results), market=market)
    for it in items:
        it.pop("_pred", None)
        p = it.get("p_up")
        if p is None:
            continue
        # 情绪弱时对涨停活跃源折价：连板/炸板题材在弱势市次日溢价差
        src = str(it.get("source") or "")
        if regime == "weak" and any(k in src for k in ("连板", "4IN1", "龙回头", "涨停")):
            p = max(0.05, p * 0.92)
            it["p_up"] = round(p, 4)
            it["score"] = round(p * 100, 1)
            it["mood_haircut"] = 0.92
            it["direction"] = "bullish" if p >= 0.55 else ("bearish" if p <= 0.45 else "neutral")

    kept = [x for x in items if x.get("has_pred") and (x.get("p_up") or 0) >= thr]
    kept.sort(key=lambda x: x.get("score") or 0, reverse=True)
    kept = kept[:cap]
    kept_codes = [x.get("code") for x in kept]
    for x in items:
        x["selected"] = x.get("code") in kept_codes and x.get("has_pred")
    dropped = [x for x in items if not x.get("selected")]
    dropped.sort(key=lambda x: x.get("score") or x.get("tech_score") or 0, reverse=True)
    return kept + dropped


# ═══════════════════════════════════════════════════════════════
#  候选画像（证据卡）
# ═══════════════════════════════════════════════════════════════

def _theme_hit(item: Dict, themes: List[str]) -> Dict[str, Any]:
    """题材/理由是否落在当日主线（字符串包含匹配，粗粒度但可解释）。"""
    text = " ".join(str(item.get(k) or "") for k in ("reason", "tags", "source", "name"))
    hits = [t for t in themes if t and t in text]
    return {"hit": bool(hits), "tags": hits[:3]}


def profile_candidates(
    prescreen_result: Dict,
    limit: int = _PROFILE_LIMIT_DEFAULT,
    with_flow: bool = True,
    with_pred: bool = True,
) -> Dict[str, Any]:
    """给候选做「证据卡」：事实 + 客观标记 + 模型分，**不淘汰任何一只**。

    Args:
        prescreen_result: pre_screen() 的返回值
        limit: 最多画像数量（默认 20，超出部分丢弃并计入 trimmed）
        with_flow: 是否并发取资金流
        with_pred: 是否挂 P(T+1 涨) 预测分

    Returns:
        {strategy, market, mood_regime, themes, hints, profiles:[{...}], missing, errors}
    """
    strategy = prescreen_result.get("strategy", "") or select_strategy()
    market = prescreen_result.get("market", {}) or {}
    candidates = list(prescreen_result.get("candidates", []) or [])
    main_themes = prescreen_result.get("main_themes", []) or []
    themes: List[str] = [t[0] for t in main_themes if isinstance(t, (list, tuple)) and t]
    regime = _mood_regime(market)

    errors: List[Dict] = []
    missing: List[str] = []
    if not candidates:
        missing.append("候选池为空（pre_screen 未返回 candidates）")

    pool = candidates[: max(limit * 2, limit)]
    trimmed = max(0, len(candidates) - len(pool))

    from .common import fetch_kline, get_limit_pct, is_limit_locked

    # 并发取日线：DB 查询 IO 密集，线程池有效
    bars_map: Dict[str, List[Dict]] = {}

    def _fetch(c: Dict) -> None:
        code = str(c.get("code") or "")
        if code:
            try:
                bars_map[code] = fetch_kline(code, days=120)
            except Exception as e:
                bars_map[code] = []
                errors.append({"code": code, "stage": "fetch_kline", "error": str(e)})

    try:
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
            try:
                list(ex.map(_fetch, pool, timeout=_FETCH_TIMEOUT_S))
            except Exception as e:  # 超时/线程崩：已取到的照用，缺口计入 missing
                missing.append(f"并发取数超时: {type(e).__name__}: {e}")
                logger.warning("[MktScreen] 并发取数超时: %s", e)
    except Exception as e:  # 线程池不可用 → 退化为串行，不留死角
        logger.warning("[MktScreen] 并发取数失败，退化为串行: %s", e)
        for c in pool:
            _fetch(c)

    profiles: List[Dict] = []
    for c in pool:
        code = str(c.get("code") or "")
        if not code:
            continue
        name = str(c.get("name") or "")
        bars = bars_map.get(code) or []
        item = dict(c)
        miss: List[str] = list(item.get("missing") or [])

        # 涨停判定 + 价格/涨幅回填（候选池字段不全时用日线补齐）
        close = None
        if bars:
            close = bars[-1]["close"]
            if len(bars) >= 2:
                item["limit_locked"] = is_limit_locked(code, name, bars[-1]["close"], bars[-2]["close"])
            item.setdefault("change_pct", round(
                (bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100, 2))
            item.setdefault("price", bars[-1]["close"])
        else:
            miss.append("日线缺失")
        if not close:
            close = _f(item.get("price") or item.get("close")) or None

        tech = tech_summary(bars, code, name) if bars else {"ok": False, "reason": "日线缺失"}
        if not tech.get("ok"):
            miss.append(f"技术指标缺失:{tech.get('reason')}")

        prof = {
            "code": code,
            "name": name,
            "source": item.get("source", ""),
            "reason": (item.get("reason", "") or "")[:80],
            "price": round(close, 2) if close else None,
            "change_pct": round(_f(item.get("change_pct")), 2) if item.get("change_pct") is not None else None,
            "turnover_pct": round(_f(item.get("turnover_pct")), 2) if item.get("turnover_pct") else None,
            "amount_wan": round(_f(item.get("amount")) / 1e4, 1) if item.get("amount") else None,
            "continuous_days": item.get("continuous_days"),
            "limit_pct": get_limit_pct(code, name),
            "theme": _theme_hit(item, themes),
            "hard": hard_exclusions(item, strategy),
            "warnings": soft_warnings(item, market, strategy),
            "tech": {k: v for k, v in tech.items() if k != "ok"} if tech.get("ok") else None,
            "missing": miss,
        }
        prof["_bars"] = bars
        profiles.append(prof)

    # 资金流（并发，仅对首批 limit 只票取，避免候选池大时把时间全耗在网络上）
    if with_flow:
        def _flow(p: Dict) -> None:
            try:
                p["flow"] = flow_summary(p["code"])
                if not p["flow"].get("ok"):
                    p["missing"].append(f"资金流缺失:{p['flow'].get('reason')}")
            except Exception as e:
                p["flow"] = {"ok": False, "reason": str(e)}
                p["missing"].append(f"资金流异常:{e}")

        try:
            with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
                list(ex.map(_flow, profiles[:limit]))
        except Exception as e:
            logger.warning("[MktScreen] 资金流并发失败: %s", e)
            for p in profiles[:limit]:
                _flow(p)

    if with_pred:
        profiles = attach_prediction(profiles, market=market)

    for p in profiles:
        p.pop("_bars", None)
        p.pop("_pred", None)

    # 默认序：有预测者按 p_up 降序，无预测垫底 —— 仅为可读性，**不表示取舍**，LLM 可自行重排
    profiles.sort(key=lambda x: (x.get("p_up") is not None, x.get("p_up") or 0), reverse=True)
    kept = profiles[:limit]
    if len(profiles) > limit:
        trimmed += len(profiles) - limit

    return {
        "strategy": strategy,
        "market": market,
        "mood_regime": regime,
        "themes": themes[:8],
        "hints": _hints(regime, market),
        "pool_size": len(candidates),
        "trimmed": trimmed,
        "profiles": kept,
        "missing": missing,
        "errors": errors,
    }


def _hints(regime: str, market: dict) -> Dict[str, Any]:
    """给 LLM 的参考门槛（不是硬规则）：弱势市提高胜率要求、强势市可放宽。"""
    base = {"weak": 0.55, "neutral": 0.50, "strong": 0.48}.get(regime, 0.50)
    caps = {"weak": 5, "neutral": 8, "strong": 10}
    return {
        "p_up_floor_suggest": base,
        "max_picks_suggest": caps.get(regime, 8),
        "note": (
            "门槛/条数是历史均值经验值，非硬约束：若候选普遍高 p_up 且题材共振，可适当放宽；"
            "若 profile 里 missing 很多（数据缺口），应主动降低给出的置信度"
        ),
    }


# ═══════════════════════════════════════════════════════════════
#  报告汇总（不替 LLM 做取舍）
# ═══════════════════════════════════════════════════════════════

def build_report(results: list, selected_codes: List[str] = None):
    """从分析结果列表构建 SkillReport。

    selected_codes 非空时只汇总指名标的；为空时按 Top5（保持旧口径，向后兼容）。
    注意：这里是「汇总」不是「筛选」——真正的取舍在 LLM 侧完成。
    """
    from .common import SkillReport

    valid = [r for r in results if r is not None and isinstance(r, dict)]
    if not valid:
        return SkillReport(
            skill_name="market_screener",
            score=50.0,
            signal="无有效分析结果",
        )

    picked = [v for v in valid if (not selected_codes) or str(v.get("code")) in {str(c) for c in selected_codes}]
    if not picked:
        picked = valid

    _ranked = sorted(picked, key=lambda v: v.get("score") or 0, reverse=True)
    scores = [v.get("score", 50) for v in _ranked[:5]] or [50]
    directions = [v.get("direction", "neutral") for v in picked]
    avg_score = sum(scores) / len(scores)

    bullish = directions.count("bullish")
    bearish = directions.count("bearish")
    if bullish > bearish and bullish > len(picked) * 0.3:
        direction = "bullish"
    elif bearish > bullish and bearish > len(picked) * 0.3:
        direction = "bearish"
    else:
        direction = "neutral"

    confs = [v.get("confidence", 0.5) for v in picked]
    avg_conf = sum(confs) / len(confs) if confs else 0.5

    return SkillReport(
        skill_name="market_screener",
        score=round(avg_score, 1),
        direction=direction,
        confidence=round(avg_conf, 2),
        signal=f"分析 {len(picked)} 只（候选 {len(valid)} 只）",
        output_data={"analyzed": valid},
    )


# ═══════════════════════════════════════════════════════════════
#  旧流程兼容层
# ═══════════════════════════════════════════════════════════════

def filter_candidates(prescreen_result: Dict) -> str:
    """便捷形式（旧三步流程兼容）：只剔除有硬障碍的候选，返回逗号分隔 codes。

    需要自己看证据做取舍时用 profile_candidates() —— 那才是推荐路径。

    Args:
        prescreen_result: pre_screen() 的返回值（SkillResult dict）

    Returns:
        逗号分隔的股票代码；无符合条件时返回空字符串
    """
    try:
        bundle = profile_candidates(prescreen_result, limit=15, with_flow=False, with_pred=False)
    except Exception as e:
        logger.warning("[MktScreen] filter_candidates 画像失败: %s", e)
        return ""
    # 保守口径：硬障碍 + 经验型软警告都剔除（想自己权衡请用 profile_candidates）
    codes = [p["code"] for p in bundle.get("profiles", [])
             if not p.get("hard") and not p.get("warnings")]
    return ",".join(codes[:15])
