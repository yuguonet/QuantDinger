# -*- coding: utf-8 -*-
"""
market_screener/run.py — LLM 工具出口（不含任何选股规则）

2026-09-29 v7：彻底删掉 intraday.py / eod.py / post_market.py 三个「策略模块」。
这三个文件把 ① 客观计算 ② 选股规则 ③ 评分梯形 ④ 报告口径 混在一起，
LLM 只能消费别人替它算好的结论 —— 这就是「自由度太小、分析深度不够」的根因。

现在的分工：
    _facts.py                      纯客观计算（市场事实 / 多周期几何 / 形态识别 / 候选池取数）
    _helpers.py                    证据加工（证据卡 / 预测分 / 汇总）
    references/strategy-rules.md   规则（怎么解读上面的事实，由 LLM 读、采纳或推翻）

本文件只做：参数校验 → 调用 _facts / _helpers → 包 SkillResult。
本文件的每个公开函数都会被注册为一个工具，**docstring 第一行即工具描述**。

注意：本目录 .py 与 SKILL.md 行尾为 CRLF（common.py 例外为 LF），改前用 read_bytes() 判定。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.agent.log import logger

from skills.market_screener._helpers import (
    select_strategy, build_report, resolve_names,
    profile_candidates as _profile_candidates,
    filter_candidates as _filter_candidates,
    tech_summary, predict_one, _mood_regime,
)
from skills.market_screener._facts import (
    candidate_pool, market_state as _market_state, multitimeframe as _multitimeframe,
    detect_patterns as _detect_patterns, POOL_SOURCES, _PATTERN_PARAMS_DEFAULT,
)
from skills.market_screener.common import SkillResult, fetch_kline


# 单票深挖的总等待上限：外部源（龙虎榜/板块/资金）偶发挂起，不能拖死整轮选股
_INSPECT_DEADLINE_S = 15.0


def market_state() -> dict:
    """市场层面事实：资金流向、涨停/跌停家数、炸板率、板块强弱、情绪描述标签。

    返回:
        dict: fund_flow=上证净流入；zt_count/dt_count/broken_count/broken_rate=涨停/跌停/炸板；
            strong_sectors/weak_sectors=行业涨跌；mood/mood_score=描述性情绪标签（非入场规则）。
    """
    try:
        return SkillResult(_market_state())
    except Exception as e:
        logger.warning("[market_screener] market_state 失败: %s", e)
        return SkillResult({"error": str(e), "mood": "未知", "mood_score": None})


def pre_screen(sources: str = None, queries: str = None, search_top_n: int = 50,
               min_turnover: float = None, limit: int = None) -> dict:
    """拿候选池与市场情绪。只负责「从哪些渠道取」，不做任何取舍与排序。

    参数:
        sources: 逗号分隔的候选来源，取值 zt(连板≥2) / eod_zt(尾盘封板) / dragon(龙回头) /
            hot(当日热点题材) / search(条件搜索) / recent_zt(近期涨停)。留空用时段的默认组合。
        queries: 逗号分隔的 search_stocks 查询串，覆盖默认建议词
        search_top_n: 每条查询返回上限，默认 50
        min_turnover: 换手率下限；留空 = 不过滤（换手不足会在画像的 hard 里标记，由你决定）
        limit: 返回条数上限；留空 = 不截断

    返回:
        dict: strategy=时段；market=市场事实；candidates=候选列表；continuous_board /
            dragon_pullback / eod_zt=各来源明细；main_themes=当日主线；missing=取数缺口。
    """
    strategy = select_strategy()
    logger.info("[market_screener] pre_screen 策略: %s 来源: %s", strategy, sources or "默认")

    missing: list = []
    try:
        pool = candidate_pool(
            strategy=strategy, sources=sources, queries=queries,
            search_top_n=int(search_top_n or 50),
            min_turnover=None if min_turnover is None else float(min_turnover),
            limit=None if limit is None else int(limit),
        )
    except Exception as e:
        logger.warning("[market_screener] 候选池取数失败: %s", e)
        return SkillResult({
            "strategy": strategy, "error": str(e),
            "candidates": [], "main_themes": [], "market": {}, "missing": [str(e)],
        })

    try:
        market = _market_state()
    except Exception as e:
        market = {}
        missing.append(f"市场状态: {type(e).__name__}: {e}")

    pool["market"] = market
    pool["sources_available"] = list(POOL_SOURCES)
    pool["missing"] = list(pool.get("missing") or []) + missing
    return SkillResult(pool)


def profile_candidates(prescreen_result: dict, limit: int = 20, with_flow: bool = True,
                       with_pred: bool = True) -> dict:
    """给候选做「证据卡」—— 只陈述事实，不淘汰任何一只。

    你可以（也应当）在拿到结果后自己写 Python 排序/分组/过滤/加自定义加权；
    hard 字段是客观障碍（买不进/ST/流动性），请自行决定是排除还是降权。

    参数:
        prescreen_result: pre_screen() 的返回值
        limit: 最多画像几只，默认 20（超出部分计入 trimmed）
        with_flow: 是否取资金流，默认 True
        with_pred: 是否挂 P(次日涨) 模型分，默认 True

    返回:
        dict: strategy / market / mood_regime / themes / hints(参考门槛，非硬约束) /
            profiles[{code,name,source,reason,price,change_pct,turnover_pct,amount_wan,
                      theme{hit,tags},hard[],warnings[],tech{...},flow{...},p_up,exp_ret_bp,missing[]}] /
            trimmed / missing / errors
    """
    if not isinstance(prescreen_result, dict):
        return SkillResult({"error": "prescreen_result 必须是 pre_screen() 的返回值", "profiles": []})
    bundle = _profile_candidates(
        prescreen_result, limit=int(limit or 20),
        with_flow=bool(with_flow), with_pred=bool(with_pred),
    )
    return SkillResult(bundle)


def multitimeframe(code: str, daily_days: int = 60, minute_days: int = 5) -> dict:
    """单票多周期结构量：日线位置 + 15m/5m 走势。

    分钟数据统一取 1m 后本地聚合（15m/5m/30m/60m 查表恒返回 0 行，勿再直接查）。
    weak_to_strong / volume_pattern / macd_strength 是对几何关系的**速记标签**，
    怎么解读写在 references/strategy-rules.md，本身不含任何方向建议。

    参数:
        code: 6 位股票代码
        daily_days: 日线回溯根数，默认 60
        minute_days: 分钟线回溯自然日，默认 5

    返回:
        dict: daily{close,ma5/10/20/60,macd_bar,rsi,vol_ratio,above_ma5/20,ma5_slope_pct}；
            m15/m5{close,above_ma5,rsi,vol_ratio,trend}；m5 另含 macd_strength；missing=缺口明细。
    """
    code = str(code or "").strip()
    if not code:
        return SkillResult({"error": "code 不能为空"})
    try:
        return SkillResult(_multitimeframe(code, daily_days=int(daily_days or 60),
                                           minute_days=int(minute_days or 5)))
    except Exception as e:
        logger.warning("[market_screener] multitimeframe %s 失败: %s", code, e)
        return SkillResult({"ok": False, "code": code, "error": str(e), "missing": [str(e)]})


def detect_patterns(codes: str, days: int = 60, params: str = None) -> dict:
    """识别 K 线形态。只回答「是不是」，不回答「好不好」。

    形态共有六种：平台突破 / 底部放量启动 / 均线支撑回踩 / MACD金叉 / 缩量回调放量突破 / 突破前高。
    params 是这些形态的**几何定义**（不是推荐门槛），可以改来做敏感性检查。

    参数:
        codes: 逗号分隔股票代码
        days: 日线回溯根数，默认 60
        params: JSON 串，覆盖几何定义，如 {"platform_range_pct": 10, "breakout_vol_ratio": 1.5}

    返回:
        dict: patterns={code: [{pattern, ...几何量}]}；params_used=实际使用的定义参数；
            missing=取数失败的票。
    """
    import json

    overrides: dict = {}
    if params:
        try:
            overrides = json.loads(params) if isinstance(params, str) else dict(params)
        except Exception as e:
            return SkillResult({"error": f"params 解析失败: {e}",
                                "params_allowed": sorted(_PATTERN_PARAMS_DEFAULT)})

    code_list = [c.strip() for c in str(codes or "").split(",") if c.strip()]
    if not code_list:
        return SkillResult({"error": "codes 不能为空", "patterns": {}})

    out: dict = {}
    missing: list = []
    for code in code_list:
        try:
            bars = fetch_kline(code, days=int(days or 60))
            if len(bars) < 15:
                missing.append(f"{code}: 日线不足15根（实得{len(bars)}）")
                continue
            out[code] = _detect_patterns(bars, overrides)
        except Exception as e:
            missing.append(f"{code}: {type(e).__name__}: {e}")

    used = dict(_PATTERN_PARAMS_DEFAULT)
    used.update({k: float(v) for k, v in overrides.items() if k in used})
    return SkillResult({"patterns": out, "params_used": used, "missing": missing})


def filter_candidates(prescreen_result: dict) -> str:
    """便捷形式（旧流程兼容）：剔除有硬障碍的候选，返回逗号分隔 codes。

    想自己看证据做取舍请用 profile_candidates()。

    参数:
        prescreen_result: pre_screen() 的返回值

    返回:
        str: 逗号分隔股票代码，无符合项时返回空字符串
    """
    return _filter_candidates(prescreen_result)


def deep_analyze(codes: str, limit: int = 8, with_flow: bool = True,
                 with_patterns: bool = True, with_mtf: bool = False) -> dict:
    """对指定股票批量汇总全部可用证据，并按标定模型的 P(次日涨) 排序。

    这里**没有**主观评分梯形：唯一的数字是 _helpers.predict_one 给出的已标定 p_up。
    你要加权、改口径、加自己的判断都可以 —— 请基于返回的 tech / patterns / flow / mtf 自己写。

    参数:
        codes: 逗号分隔股票代码，如 "000001,600519,300750"
        limit: 最多分析几只，默认 8
        with_flow: 是否取资金流
        with_patterns: 是否识别 K 线形态
        with_mtf: 是否取多周期结构（需要分钟源，更慢）

    返回:
        dict: analyzed[{code,name,patterns,tech,flow,mtf,score,direction,confidence,signal,missing}]；
            strategy / missing / errors。confidence 是**证据完整度**（拿到几维 / 共几维），
            不是模型对自己判断的把握。
    """
    strategy = select_strategy()
    errors: list = []
    missing: list = []
    code_list = [c.strip() for c in str(codes or "").split(",") if c.strip()][:max(1, int(limit or 8))]
    if not code_list:
        return SkillResult({
            "score": 45.0, "direction": "neutral", "confidence": 0.5,
            "signal": "无股票可分析", "analyzed": [], "strategy": strategy,
            "errors": [], "missing": [],
        })

    name_map = resolve_names(code_list)
    market = {}
    try:
        market = _market_state()
    except Exception as e:
        missing.append(f"市场状态: {type(e).__name__}: {e}")

    _EVIDENCE_DIMS = ("tech", "patterns", "flow", "prediction")

    def _one(item: dict) -> dict:
        code = str(item.get("code") or "")
        name = item.get("name") or ""
        miss: list = list(item.get("missing") or [])
        # _evidence 记录「这一维数据有没有拿到」，与「有没有触发信号」是两回事
        row = {"code": code, "name": name, "missing": miss, "_evidence": {}}

        bars = fetch_kline(code, days=120)
        if not bars:
            miss.append("日线缺失")
            row["tech"] = None
            row["patterns"] = []
            row["_bars"] = []
            return row

        ts = tech_summary(bars, code, name)
        if ts.get("ok"):
            row["tech"] = {k: v for k, v in ts.items() if k != "ok"}
        else:
            miss.append(f"技术指标缺失:{ts.get('reason')}")
            row["tech"] = None
        row["_evidence"]["tech"] = bool(ts.get("ok"))

        row["patterns"] = []
        if with_patterns:
            try:
                row["patterns"] = _detect_patterns(bars)
                row["_evidence"]["patterns"] = True
            except Exception as e:
                miss.append(f"形态识别:{type(e).__name__}: {e}")
                row["_evidence"]["patterns"] = False

        row["flow"] = None
        if with_flow:
            try:
                from ._helpers import flow_summary
                fs = flow_summary(code)
                row["flow"] = fs
                if not fs.get("ok"):
                    miss.append(f"资金流缺失:{fs.get('reason')}")
                row["_evidence"]["flow"] = bool(fs.get("ok"))
            except Exception as e:
                miss.append(f"资金流异常:{type(e).__name__}: {e}")
                row["_evidence"]["flow"] = False

        row["mtf"] = None
        if with_mtf:
            try:
                mtf = _multitimeframe(code)
                row["mtf"] = mtf
                miss.extend(f"多周期:{m}" for m in (mtf.get("missing") or []))
            except Exception as e:
                miss.append(f"多周期异常:{type(e).__name__}: {e}")

        row["_bars"] = bars
        return row

    items = [{"code": c, "name": name_map.get(c, "")} for c in code_list]

    # 逐票取证据：K线/资金流都是 IO，串行会把 8 只票拖到十几秒
    from ._helpers import _FETCH_TIMEOUT_S, attach_prediction
    raw: list = []
    done: set = set()
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
            futs = {ex.submit(_one, it): str(it.get("code") or "") for it in items}
            for fut in as_completed(futs, timeout=max(30.0, _FETCH_TIMEOUT_S * 2)):
                done.add(fut)
                _code = futs[fut]
                try:
                    r = fut.result()
                    if r is not None:
                        raw.append(r)
                    else:
                        errors.append({"code": _code, "error": "分析返回 None（多为数据不足）"})
                except Exception as e:
                    errors.append({"code": _code, "error": f"{type(e).__name__}: {e}"})
    except Exception as e:  # 线程池超时 / 崩溃：已入场的结果照用，缺席的必须留痕
        missing.append(f"并发分析中断: {type(e).__name__}: {e}")
    else:
        for fut, _code in futs.items():
            if fut not in done:
                errors.append({"code": _code, "error": "未在时限内返回（多为外部源挂起）"})
    if len(raw) < len(items):
        missing.append(f"{len(items) - len(raw)} 只票未在时限内完成")

    order = {c: k for k, c in enumerate(code_list)}
    raw.sort(key=lambda r: order.get(str(r.get("code")), 999))
    raw = attach_prediction(raw, market=market)

    for r in raw:
        r.pop("_bars", None)
        r.pop("_pred", None)
        pred_ok = bool(r.get("has_pred"))
        r.pop("has_pred", None)
        # confidence = 证据完整度（这一维的数据有没有拿到），不是模型对判断的把握
        ev = dict(r.pop("_evidence", {}) or {})
        ev["prediction"] = pred_ok
        dims = ("tech", "patterns", "flow", "prediction") if with_patterns else ("tech", "flow", "prediction")
        have = [k for k in dims if ev.get(k)]
        r["confidence"] = round(len(have) / len(dims), 2)
        r["evidence_dims"] = have
        _tech = r.get("tech") or {}
        _bits = []
        if r.get("patterns"):
            _bits.append("形态:" + "、".join(p["pattern"] for p in r["patterns"]))
        if isinstance(_tech, dict) and _tech:
            _bits.append(
                f"收盘价位于20日区间{_tech.get('pos_in_20d_pct')}%、"
                f"距MA20 {_tech.get('dist_ma20_pct')}%、量比{_tech.get('vol_ratio')}、"
                f"RSI{_tech.get('rsi14')}"
            )
        r["signal"] = "；".join(_bits) or "无形态与技术信号"

    report = build_report(raw, selected_codes=code_list)
    analyzed = []
    if hasattr(report, "output_data") and report.output_data:
        analyzed = report.output_data.get("analyzed", [])
    for a in analyzed:
        a.pop("_bars", None)

    return SkillResult({
        "score": report.score,
        "direction": report.direction,
        "confidence": report.confidence,
        "signal": report.signal,
        "analyzed": analyzed,
        "strategy": strategy,
        "confidence_note": "confidence = 证据完整度（tech/patterns/flow/prediction 拿到几维），非模型自信度",
        "errors": errors,
        "missing": missing,
    })


def inspect_stock(code: str, deep: bool = False) -> dict:
    """单票定点深挖：把一只票的相关证据一次拉齐，用于交叉验证或推翻排序结论。

    默认拉用量价/位置/资金/题材/涨停史/模型分；deep=True 追加龙虎榜席位与板块排名（更慢）。

    参数:
        code: 6 位股票代码
        deep: 是否追加慢速源（龙虎榜席位、板块排名），默认 False

    返回:
        dict: code/name/strategy；quote=实时快照；tech=量价位置；
            flow=资金流；fund_hist=近 10 日主力净流入；theme=行业与概念标签；
            zt_history=近 8 日涨停记录；prediction=P(次日涨) 与模型内部量；
            risks=客观风险标记（不代表看空）；missing=数据缺口明细。
    """
    code = str(code or "").strip()
    if not code:
        return SkillResult({"error": "code 不能为空"})

    strategy = select_strategy()
    missing: list = []
    out: dict = {"code": code, "strategy": strategy, "missing": missing}

    def _task(name: str, fn):
        try:
            return name, fn()
        except Exception as e:
            missing.append(f"{name}: {type(e).__name__}: {e}")
            return name, None

    def _quote():
        from app.agent.tools.finance.data_tools import get_realtime_quote
        return get_realtime_quote(code)

    def _bars():
        return fetch_kline(code, days=60)

    def _flow():
        from ._helpers import flow_summary
        return flow_summary(code)

    def _fund_hist():
        from app.agent.tools.finance.fund_flow_tools import get_fund_flow
        return get_fund_flow(scope="stock_daily", codes=code, days=10)

    def _theme():
        from app.agent.tools.finance.sector_analysis_tools import get_stock_sector_info
        return get_stock_sector_info(code)

    def _zt():
        from .common import fetch_recent_zt_pools
        pools = fetch_recent_zt_pools(8)
        hist = []
        for d, pool in sorted(pools.items(), reverse=True):
            for s in pool or []:
                if str(s.get("stock_code", "")) == code:
                    hist.append({
                        "date": d,
                        "continuous_days": s.get("continuous_zt_days"),
                        "reason": s.get("reason", ""),
                    })
        return hist

    def _dragon():
        from app.agent.tools.finance.dragon_tools import get_dragon_tiger
        return get_dragon_tiger(codes=code, detail=True)

    def _boards():
        from app.agent.tools.finance.sector_analysis_tools import get_sector_board
        return get_sector_board(view="ranking", top_n=30)

    tasks = [_quote, _bars, _flow, _theme, _zt]
    if deep:
        tasks += [_dragon, _fund_hist, _boards]

    results = {}
    fut_names = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as ex:
            fut_names = {ex.submit(_task, fn.__name__.lstrip("_"), fn): fn.__name__.lstrip("_")
                         for fn in tasks}
            for fut in as_completed(fut_names, timeout=_INSPECT_DEADLINE_S):
                name, val = fut.result()
                results[name] = val
    except Exception as e:
        missing.append(f"并发取数: {type(e).__name__}: {e}")
    # 超时未返回的源必须留痕 —— 否则 LLM 会把「拿不到」误读成「没有」
    for fut, name in fut_names.items():
        if name not in results:
            missing.append(f"{name}: 超时>{_INSPECT_DEADLINE_S}s 未返回")

    quote = results.get("quote") or {}
    qdata = (quote.get("data") or {}) if isinstance(quote, dict) else {}
    if isinstance(qdata, dict):
        qdata = qdata.get(code) or (list(qdata.values())[0] if qdata else {})
    out["name"] = (qdata or {}).get("name") or ""

    bars = results.get("bars") or []
    ts = tech_summary(bars, code, out["name"])
    if ts.get("ok"):
        out["tech"] = {k: v for k, v in ts.items() if k != "ok"}
        out["prediction"] = predict_one(code, bars, _mood_regime(_safe_market()))
    else:
        missing.append(f"tech: {ts.get('reason')}")
        out["prediction"] = {"ok": False, "reason": "日线不足，无法预测"}

    _p = (qdata or {}).get("price") or (bars[-1]["close"] if bars else None)
    out["quote"] = {
        "price": round(float(_p), 2) if _p else None,
        "change_pct": (qdata or {}).get("change_pct"),
        "turnover_pct": (qdata or {}).get("turnover_rate") or (qdata or {}).get("turnover_pct"),
        "amount": (qdata or {}).get("amount"),
        "volume_ratio": (qdata or {}).get("volume_ratio"),
    }
    out["flow"] = results.get("flow")
    if deep:
        out["fund_hist"] = results.get("fund_hist")
        out["dragon"] = results.get("dragon")
        out["boards"] = results.get("boards")

    theme = results.get("theme") or {}
    tdata = (theme.get("data") or {}).get(code, {}) if isinstance(theme, dict) else {}
    out["theme"] = {
        "industry": tdata.get("industry"),
        "boards": [b.get("name") for b in (tdata.get("boards") or [])][:6],
        "concept_tags": (tdata.get("concept_tags") or [])[:8],
    }
    out["zt_history"] = results.get("zt") or []

    # 客观风险标记（事实罗列，不是买卖建议）
    risks = []
    tech = out.get("tech") or {}
    if tech.get("is_20d_high"):
        risks.append("处于20日最高位（追高风险）")
    if (tech.get("rsi14") or 0) >= 75:
        risks.append(f"RSI{tech['rsi14']} 超买")
    if (tech.get("dist_ma20_pct") or 0) >= 15:
        risks.append(f"离MA20已有 {tech['dist_ma20_pct']}%（乖离偏大）")
    if (tech.get("zt_streak") or 0) >= 2:
        risks.append(f"{tech['zt_streak']}连板（次日分歧概率高）")
    if (tech.get("vol_ratio") or 0) >= 4:
        risks.append(f"量比{tech['vol_ratio']}异常放大（注意出货）")
    if isinstance(out.get("flow"), dict) and out["flow"].get("ok") and (out["flow"].get("main_net_wan") or 0) < 0:
        risks.append(f"主力净流出{abs(out['flow']['main_net_wan'])}万")
    if tech.get("limit_up"):
        risks.append("当日涨停封板（次日竞价溢价已 price in，需防冲高回落）")
    out["risks"] = risks

    if not out["name"]:
        missing.append("名称解析失败（不影响数值分析）")
    return SkillResult(out)


def _safe_market() -> dict:
    """内部用：拿市场状态（失败返回空 dict，不抛）。市场状态只是预测的上下文标签，缺了不致命。"""
    try:
        return _market_state() or {}
    except Exception:
        return {}


def run() -> dict:
    """一次性跑完整流程（pre_screen → profile → 分析 Top8）。分步调用更灵活，本函数供快捷场景使用。"""
    prescreen_result = pre_screen()
    if prescreen_result.get("error"):
        return prescreen_result
    bundle = _profile_candidates(prescreen_result, limit=10, with_flow=False, with_pred=False)
    codes = [p["code"] for p in bundle.get("profiles", []) if not p.get("hard")]
    if not codes:
        return SkillResult({"strategy": bundle.get("strategy"), "analyzed": [], "signal": "无符合硬条件的候选"})
    return deep_analyze(",".join(codes[:8]))
