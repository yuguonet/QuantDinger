# -*- coding: utf-8 -*-
"""
market-screener/run.py — LLM 工具出口（不含任何选股规则）

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

from ...log import logger

from ._helpers import select_strategy, build_report, resolve_names, profile_candidates as _profile_candidates, filter_candidates as _filter_candidates, tech_summary, predict_one, _mood_regime, objective_preexclude, score_pool, prewarm_market_klines, enforce_buy_quality
from ._facts import candidate_pool, market_state as _market_state, multitimeframe as _multitimeframe, detect_patterns as _detect_patterns, POOL_SOURCES, _PATTERN_PARAMS_DEFAULT
from .common import SkillResult, fetch_kline, warm_market_pool


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
        logger.warning("[market-screener] market_state 失败: %s", e)
        return SkillResult({"error": str(e), "mood": "未知", "mood_score": None})


def pre_screen(sources: str = None, queries: str = None, search_top_n: int = 50,
               min_turnover: float = None, limit: int = None) -> dict:
    """拿候选池与市场情绪。只负责「从哪些渠道取」，不做任何取舍与排序。

    参数:
        sources: 逗号分隔的候选来源，取值 hot / search / flow / sector / scan。
            连板/龙回头等追高源已删除；search 只是多源之一，不是唯一通道。留空用时段默认。
        queries: 逗号分隔的 search_stocks 查询串，覆盖默认建议词
        search_top_n: 每条查询返回上限，默认 50
        min_turnover: 换手率下限；留空 = 不过滤（换手不足会在画像的 hard 里标记，由你决定）
        limit: 返回条数上限；留空 = 不截断

    返回:
        dict: strategy=时段；market=市场事实；candidates=候选列表；
            main_themes=当日主线；missing=取数缺口。
    """
    strategy = select_strategy()
    logger.info("[market-screener] pre_screen 策略: %s 来源: %s", strategy, sources or "默认")

    missing: list = []
    try:
        pool = candidate_pool(
            strategy=strategy, sources=sources, queries=queries,
            search_top_n=int(search_top_n or 50),
            min_turnover=None if min_turnover is None else float(min_turnover),
            limit=None if limit is None else int(limit),
        )
    except Exception as e:
        logger.warning("[market-screener] 候选池取数失败: %s", e)
        return SkillResult({
            "strategy": strategy, "error": str(e),
            "candidates": [], "main_themes": [], "market": {}, "missing": [str(e)],
        })

    try:
        market = _market_state()
    except Exception as e:
        market = {}
        missing.append(f"市场状态: {type(e).__name__}: {e}")
    else:
        # 源级失败显性化（2026-10-21）：market_state 对涨停/跌停/炸板池降级返回但带
        # source_errors，不合并进 missing 的话 dt/broken 源故障仍然不可见
        for _se in (market.get("source_errors") or []):
            missing.append(f"市场状态: {_se}")

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
        logger.warning("[market-screener] multitimeframe %s 失败: %s", code, e)
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


# 结论写法（内嵌到结果**最前**，主 agent 打开预览第一眼看到；不必 read_skill）
_REPORT_GUIDE = {
    "OUTPUT_FORM": (
        "★ 输出壳（硬约束）：用户只关心 买什么 / 什么价位买 / T+N 能不能赚 / 走坏了能不能全身而退。"
        "禁止长篇分析，表格行数随推荐数浮动，正文保持精简，必须用下表：\n"
        "## 买什么（N 只，N=实际符合条件数；可空；信号明确时买点写「现价 X」）\n"
        "| 代码 | 现价 | 买点价位 | 止损价 | T+1/T+3/T+5 | 一句话理由 |\n"
        "每只一行；代码=6 位数字；"
        "★ 买点写「现价 X」或「回踩 MA/前高 X」（信号明确优先现价，回踩只是备选）；"
        "止损写「跌破 X」/「不跌破 X」（X=日线 MA5/MA10/MA20 或前高前低，禁止用 VWAP/分时均价）；"
        "T+N 填 看多/中性/看空。\n"
        "★ **全文用中文白话**，禁止输出 p_up / w2s_kept / floor_kept / t1_bias / "
        "near_support / rank_cut 等字段名："
        "p_up 写「次日上涨参考」，w2s_kept 写「今日弱转强信号」，"
        "无标定分写「暂无历史参考分」。\n"
        "## 不买/观察（每只一行：代码+拒因≤20字）\n"
        "## 风险（≤3 行）\n"
        "指标罗列、漏斗数字不进正文。保留代码+看多看空+跌破 X（喂 claims）。"
    ),
    "STEPS": "run_skill 一次即可交付；起手不要 get_hot_sectors / get_market_overview（market 已在本结果）；"
             "禁止 search_tools / read_skill / get_dragon_tiger / 二次 run_skill / 逐只 quote；"
             "funnel 只写在末行「数据」：pool_raw/excluded_obj/scored/excluded_score/deep_n；"
             "funnel.excluded_score 只统计 score_pool 淘汰（p_up 硬闸/排名截断/回捞），不是全候选集",
    "buyable": "rt.limit_up=true（涨停封板）或 rt.limit_down=true（跌停封死）→ **只进排除表，禁止进可买表**；"
               "盘中涨停已在池层 objective_preexclude 排除，rt.buyable=false 是 deep 阶段的最终防线；"
               "stale_bar 不影响此条",
    "entry_exit": "买点优先「现价 X」：信号明确（放量突破/上升通道打开/弱转强+资金）时"
                  "现价直接买优于等回踩（回踩不一定成交，风险收益比按现价算更直接）。"
                  "仅当现价已高位/乖离大时写「回踩 MA5/MA10/前高 X」作备选。"
                  "止损=「跌破 X」/「不跌破 X」；走坏=收盘破止损或主力转净流出。"
                  "无明确止损价不得进「买什么」。T+1 风险:收益尽量≥1.5~2:1；"
                  "支撑–压力间距过窄（sr_gap_pct 小）操作性弱，排序下调",
    "quality_bar": "「买什么」质量线（2026-10-28 三条硬约束）："
                   "① 须题材/主线命中 **或** 上方空间（sr_gap≥8%），二者皆无只能进「观察」；"
                   "② 同板性/同属性最多 2 只（创业板/科创扎堆降权）；"
                   "③ T+1~T+5 至少两档非「中性」才配写「看多」进主表，否则改观察。"
                   "禁止纯均线/量比/RSI 指标罗列；T+5 写「看空」不得进「买什么」；"
                   "四只全是高波动创业板却不点风险 = 失职",
    "horizon": "表内只写 T+1/T+3/T+5（词表：看多/中性/看空）；T+10/T+20 不写。"
               "zt_streak>=3 只写 T+1。p_up 仅 T+1，禁止外推胜率。"
               "早盘博今日、尾盘博隔夜(T+1/T+3)、盘后买入需纳入 T+5",
    "priority": "支撑与弱转强/强转弱 ≫ 涨跌幅；禁止按涨跌幅排序",
    "support_rule": "盘中红绿不决定安全；可怕的是无支撑阴跌。-8%弱转强+有支撑 可以比 +8%转分歧更安全",
    "fields": "主论据=tech.support.nearest / MA 排列 / flow / p_up；rt 看现价/涨跌停",
    "board_cut": "zt_streak>=3 默认只写 T+1",
    "words": "方向用 看多/中性/看空/回避；用户问持仓才写「兑现」",
    "market": "mood/资金/热点已由 run_skill 覆盖；不要再 get_market_overview / get_hot_sectors；"
              "大盘最多进「风险」一行轻注，不得当筛选主条件",
    "funnel": "漏斗数字只允许出现在末行「数据」；禁止写进正文",
    "no_pred": "无标定分票写「暂无历史参考分」（不是坏票）；禁止写 no_pred_kept/p_up 等字段名",
}


# ── 单漏斗参数（2026-10-20；10-21 增 no_pred_keep；10-23 增 w2s_keep；10-24 门槛对齐 hints；
#    10-28 去掉 8.1.0 基线层 + 连板/龙回头源，单漏斗：客观预排除 → 软门槛/回捞）──
# p_up_floor 与 hints.p_up_floor_suggest 同源（AUC≈0.61，0.40–0.60 为噪声带）：
# 硬闸取 suggest 下限 0.48，LLM 看到的 suggest（0.48~0.55）仍可按情绪微调。
# 【自动放宽】存活 <_FUNNEL_MIN_ALIVE 时进 Round1（门槛略降 + 追加通用查询词）。
#   这意味着**同一只票在弱池存活、强池出局**——跨日口径不可比，属有意设计
#   （保底出候选），非缺陷。要可比口径请固定单一 round 参数、不触发放宽。
# w2s：条件已严（回踩滞跌+主力≥2000万+今日>昨日），名额可略宽于 2——最贴
#   「短线最小风险、最大涨幅」。
_FUNNEL_ROUNDS = (
    {"p_up_floor": 0.48, "rank_cut": 0.30, "floor_keep": 4, "no_pred_keep": 2, "w2s_keep": 4},
    {"p_up_floor": 0.45, "rank_cut": 0.15, "floor_keep": 6, "no_pred_keep": 3, "w2s_keep": 6},
)
_FUNNEL_MIN_ALIVE = 4       # 合计 < 此数 → 自动进 Round1（兜底出候选）
_FUNNEL_DEEP_N = 12         # 进 deep 的最终候选数
_FUNNEL_SEARCH_TOP_N = 200  # run() 拉满（search_stocks 上限）：宽池 + 代码规则初筛，禁止「取前 10」
_FUNNEL_FALLBACK_QUERIES = ("换手率大于3%", "放量上涨")  # Round1 追加通用词

# 源失败条目识别标记（2026-10-21）：missing 里出现这些子串 = 候选源/市场源故障，
# 而不是数据缺口。candidate_pool 的热点用「<源>源失败: …」措辞，
# search 沿用既有「条件搜索[q]: …」，market_state 源错误经 pre_screen 加「市场状态: 」前缀。
_SRC_FAIL_MARKS = ("源失败", "条件搜索[")


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
        dict: analyzed[{code,name,patterns,tech,flow,mtf,score,direction,confidence,signal,missing,
            rt{price,change_pct,previous_close,limit_up,limit_up_price,limit_down_price,limit_band,time},
            stale_bar,rt_risks}]；
            strategy / missing / errors。confidence 是**证据完整度**（拿到几维 / 共几维），
            不是模型对自己判断的把握。
            rt = 实时快照；limit_band=price|no_limit|missing（新股无涨跌幅为 no_limit，
            两价为 None 且 limit_up=false，勿用公式回退）。stale_bar=True 表示本地日线滞后，
            tech 的收盘/涨跌停已按 rt 修正（MA/RSI 等历史指标仍基于 bar）。
            **有 rt 就不必再主对话重拉实时行情**。
    """
    strategy = select_strategy()
    errors: list = []
    missing: list = []
    code_list = [c.strip() for c in str(codes or "").split(",") if c.strip()][:max(1, int(limit or 8))]
    if not code_list:
        return SkillResult({
            "report_guide": _REPORT_GUIDE,
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

    def _one(item: dict) -> dict:
        code = str(item.get("code") or "")
        name = item.get("name") or ""
        miss: list = list(item.get("missing") or [])
        # _evidence 记录「这一维数据有没有拿到」，与「有没有触发信号」是两回事
        row = {"code": code, "name": name, "missing": miss, "_evidence": {}}

        try:
            bars = fetch_kline(code, days=120)
        except Exception as e:
            miss.append(f"日线异常:{type(e).__name__}: {e}")
            bars = []
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
    futs: dict = {}
    # C7（假超时）：这里**不能**用 `with ThreadPoolExecutor(...)` —— 退出 __exit__
    # 等价于 shutdown(wait=True)，会一直阻塞到挂死线程结束，于是 as_completed 的
    # timeout 形同虚设（外部源挂起时调用线程照样被钉死，_FETCH_TIMEOUT_S 失效）。
    # 改显式 shutdown(wait=False, cancel_futures=True)：超时立即返回，未开始的任务
    # 取消，仍在跑的线程不再等待（挂起源由其自身超时收尾）。
    ex = ThreadPoolExecutor(max_workers=min(8, len(items)))
    try:
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
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if len(raw) < len(items):
        missing.append(f"{len(items) - len(raw)} 只票未在时限内完成")

    order = {c: k for k, c in enumerate(code_list)}
    raw.sort(key=lambda r: order.get(str(r.get("code")), 999))

    # 实时覆盖层（2026-10-14）：本地日线盘后可能未刷当日 bar，tech 的涨跌停/收盘
    # 会与真值打架（实测 600408 bar +4.53% 非涨停 vs 实时 +10.03% 封板）。历史指标
    # 仍用 bar，可执行性字段以 rt 为准；缺 rt 不阻断。
    from ._helpers import fetch_rt_map, apply_rt_overlay
    rt_map = fetch_rt_map(code_list)
    if not rt_map:
        missing.append("实时行情缺失（tech 为日线口径，涨跌停/收盘可能滞后）")
    for r in raw:
        _rt = rt_map.get(str(r.get("code") or ""))
        apply_rt_overlay(r, _rt, code=str(r.get("code") or ""), name=str(r.get("name") or ""))
        if _rt is None and r.get("tech"):
            r.setdefault("missing", []).append("实时行情缺失")

    raw = attach_prediction(raw, market=market)

    for r in raw:
        r.pop("_bars", None)
        r.pop("_pred", None)
        pred_ok = bool(r.get("has_pred"))
        r.pop("has_pred", None)
        # confidence = 证据完整度（这一维的数据有没有拿到），不是模型对判断的把握
        ev = dict(r.pop("_evidence", {}) or {})
        ev["prediction"] = pred_ok
        # 维度按开关计数：with_flow=False 时 flow 不该拉低证据完整度
        dims = ["tech", "prediction"]
        if with_patterns:
            dims.append("patterns")
        if with_flow:
            dims.append("flow")
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
        _rt = r.get("rt") or {}
        if _rt.get("price") is not None:
            _rt_chg = _rt.get("change_pct")
            _bits.append(
                f"实时{_rt.get('price')}"
                + (f"（{_rt_chg:+.2f}%）" if _rt_chg is not None else "")
                + ("·涨停封板" if _rt.get("limit_up") else "")
                + ("·跌停封死" if _rt.get("limit_down") else "")
            )
        if _rt.get("buyable") is False:
            _bits.append("不可买（封板）")
        if r.get("stale_bar"):
            _bits.append("⚠日线滞后，收盘/涨跌停以实时为准")
        r["signal"] = "；".join(_bits) or "无形态与技术信号"

    report = build_report(raw, selected_codes=code_list)
    analyzed = []
    if hasattr(report, "output_data") and report.output_data:
        analyzed = report.output_data.get("analyzed", [])
    for a in analyzed:
        a.pop("_bars", None)

    return SkillResult({
        # report_guide 放最前：skill_run 预览= json.dumps，键序即阅读序
        "report_guide": _REPORT_GUIDE,
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
        from ...tools.finance.data_tools import get_realtime_quote
        return get_realtime_quote(code)

    def _bars():
        return fetch_kline(code, days=60)

    def _flow():
        from ._helpers import flow_summary
        return flow_summary(code)

    def _fund_hist():
        from ...tools.finance.fund_flow_tools import get_fund_flow
        return get_fund_flow(scope="stock_daily", codes=code, days=10)

    def _theme():
        from ...tools.finance.sector_analysis_tools import get_stock_sector_info
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
        from ...tools.finance.dragon_tools import get_dragon_tiger
        return get_dragon_tiger(codes=code, detail=True)

    def _boards():
        from ...tools.finance.sector_analysis_tools import get_sector_board
        return get_sector_board(view="ranking", top_n=30)

    tasks = [_quote, _bars, _flow, _theme, _zt]
    if deep:
        tasks += [_dragon, _fund_hist, _boards]

    results = {}
    fut_names = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(tasks)))   # 同 C7：不用 with
    try:
        fut_names = {ex.submit(_task, fn.__name__.lstrip("_"), fn): fn.__name__.lstrip("_")
                     for fn in tasks}
        for fut in as_completed(fut_names, timeout=_INSPECT_DEADLINE_S):
            name, val = fut.result()
            results[name] = val
    except Exception as e:
        missing.append(f"并发取数: {type(e).__name__}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
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

    # 三对账修正（2026-09-30）：get_realtime_quote 行情dict 的真实键是
    # last/changePercent/volume（见工具 Returns 契约）——旧读 price/change_pct 等
    # 幻觉键，除价格有 K 线兜底外其余恒 None。保留旧键兜底以防源字段漂移。
    _p = ((qdata or {}).get("last") or (qdata or {}).get("price")
          or (bars[-1]["close"] if bars else None))
    out["quote"] = {
        "price": round(float(_p), 2) if _p else None,
        "change_pct": (qdata or {}).get("changePercent", (qdata or {}).get("change_pct")),
        "turnover_pct": (qdata or {}).get("turnover_rate") or (qdata or {}).get("turnover_pct"),
        "amount": (qdata or {}).get("amount"),
        "volume": (qdata or {}).get("volume"),
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


def run(sources: str = None, queries: str = None,
        min_turnover: float = None, limit: int = None) -> dict:
    """一次性跑完整流程——选股目标：**短线最小风险、最大涨幅**（2026-10-28）。

    流程（单漏斗，已去掉连板/龙回头/8.1.0 基线层）：
      ① `pre_screen`：多源拉宽（hot/search/flow/sector/scan）→ **代码规则初筛**
         （剔不活跃/新股；池过大再加 ST/价格带）——禁止「命中 1470 取前 10」
      ② `objective_preexclude`：涨停买不进/跌停/ST/价格带等客观预排除
      ③ `score_pool`：p_up 硬闸 + 排名截断 + 回捞（贴支撑/题材 floor、
         弱转强 w2s、缺分保送）——w2s 通道最贴「低风险位 + 有资金确认」
      ④ `deep_analyze`（Top ≤12）。LLM 仍单次调用。

    可选参数（与 pre_screen 同义；全部可省）：
        sources: 候选来源 "hot,search"；省略用时段默认
        queries: 条件搜索词
        min_turnover: 换手率下限；省略=不过滤
        limit: 候选截断；省略=不截断
    **不要传** strategy / min_market_cap / with_mtf 等不存在的参数。
    """
    # ── 运行期预热（2026-10-25，探针定位的两大单点；均失败不阻塞主流程）──
    # P1-①：指数 K 线后台预热——mootdx 冷启 bestip 扫描 ~74s 与候选池取数
    # （candidate_pool ~69s）无依赖，后台重叠后该 78s 单点基本隐藏
    prewarm_market_klines()
    # P0：CNStock 连接池预热到画像并发度——消除 psycopg2 池 minconn=2 的
    # 连接抖动（8 线程每波 6 建 6 关，单次 read_db 0.30s vs PG 侧 0.386ms）
    warm_market_pool()

    funnel = {
        "rounds": 0, "pool_raw": 0, "excluded_obj": {},
        "scored": 0,
        "excluded_score": {"hard": 0, "p_up_hard": 0, "rank_cut": 0,
                           "no_pred": 0, "no_pred_kept": 0, "floor_kept": 0,
                           "score_skipped": 0},
        "deep_n": 0, "eastmoney_ok": True,
    }
    strategy = select_strategy()
    base_queries = [q.strip() for q in str(queries).split(",") if q.strip()] \
        if queries else []
    # 候选池级缺口（源失败/日历标注，2026-10-21）：profile 画像层的 meta.missing
    # 不含池级信息，空结果分支若只读 meta 会把「源故障空池」误交付成「无候选」
    pool_missing: list = []

    selected_all = []
    meta = {}
    for round_idx, params in enumerate(_FUNNEL_ROUNDS):
        funnel["rounds"] = round_idx + 1
        round_queries = list(base_queries)
        if round_idx > 0:
            for q in _FUNNEL_FALLBACK_QUERIES:
                if q not in round_queries:
                    round_queries.append(q)

        prescreen_result = pre_screen(
            sources=sources,
            queries=",".join(round_queries) if round_queries else None,
            search_top_n=_FUNNEL_SEARCH_TOP_N,
            min_turnover=min_turnover, limit=limit,
        )
        if prescreen_result.get("error"):
            return SkillResult({"report_guide": _REPORT_GUIDE,
                                "funnel": funnel, **prescreen_result})

        candidates = list(prescreen_result.get("candidates") or [])
        missing = list(prescreen_result.get("missing") or [])
        pool_missing = missing   # 每轮覆盖，取最终决定轮口径
        search_failed = [m for m in missing if "条件搜索" in str(m)]
        # 各轮统计取「最终决定轮」口径：Round1 池 ⊇ Round0 池，累加会把同一批
        # 淘汰重复计数，且 pool_raw 与 scored 必须来自同一个池才自洽。
        funnel["pool_raw"] = len(candidates)
        if round_idx == 0:
            # 只按 Round0 的基础查询词评估搜索源可用性：Round1 追加的兜底词
            # 只是扩大分母，会把「搜索源挂了」误判成可用
            funnel["eastmoney_ok"] = len(search_failed) < max(1, len(round_queries or [""]))

        kept, obj_stats = objective_preexclude(
            candidates, prescreen_result.get("strategy") or strategy)
        funnel["excluded_obj"] = obj_stats

        selected, score_stats, meta = score_pool(
            kept,
            strategy=prescreen_result.get("strategy") or strategy,
            market=prescreen_result.get("market"),
            main_themes=prescreen_result.get("main_themes"),
            p_up_floor=params["p_up_floor"],
            rank_cut=params["rank_cut"],
            floor_keep=params["floor_keep"],
            no_pred_keep=params["no_pred_keep"],
            w2s_keep=params.get("w2s_keep", 2),
        )
        funnel["scored"] = score_stats.get("scored", 0)
        funnel["excluded_score"] = {k: score_stats.get(k, 0)
                                    for k in ("hard", "p_up_hard", "rank_cut",
                                              "no_pred", "no_pred_kept",
                                              "floor_kept", "w2s_kept",
                                              "score_skipped")}
        # 有分票按 p_up 降序在前，弱转强/缺分保送殿后（score_pool 内已排）
        # 质量闸（2026-10-28）：题材或空间 + 同板最多 2 —— 否则进 deep 也写不出「买什么」
        selected, qstats = enforce_buy_quality(
            list(selected),
            main_themes=prescreen_result.get("main_themes"),
            max_n=_FUNNEL_DEEP_N, max_per_board=3, min_sr_gap_pct=8.0)
        funnel["quality"] = qstats
        selected_all = list(selected)
        if len(selected_all) >= _FUNNEL_MIN_ALIVE:
            break

    codes = [p["code"] for p in selected_all[:_FUNNEL_DEEP_N]]
    funnel["deep_n"] = len(codes)

    if not codes:
        # 空结果必须「可汇报」（沿用 2026-10-12 契约，funnel 补充漏斗口径）。
        # 原先只回 {analyzed: [], signal: "…"} ⇒ 模型无可写口径 ⇒ 转主对话重跑一遍
        # 同样逻辑（D-18）。这里把「筛了多少 / 因什么淘汰 / 有什么数据缺口」一并给出。
        # 源故障显性化（2026-10-21）：pool_missing 含候选源失败/日历标注时，
        # 必须写进 hard_reasons 并前置到 missing —— 防止把「数据不可用」交付成
        # 「今日无候选」（pool_raw=0 + missing 为空的纯真空池只可能出现在
        # 交易日 + 全源成功，否则 candidate_pool 已标注）。
        src_fail_n = sum(1 for m in pool_missing
                         if any(mark in str(m) for mark in _SRC_FAIL_MARKS))
        hard_reasons = [f"{k}:{v}" for k, v in funnel["excluded_obj"].items() if v]
        if src_fail_n:
            hard_reasons.append(
                f"候选源失败{src_fail_n}条（数据不可用≠真实空候选，明细见missing）")
        return SkillResult({
            "report_guide": _REPORT_GUIDE,
            "strategy": meta.get("strategy") or strategy,
            "analyzed": [],
            "signal": "无符合软硬门槛的候选",
            "pool_size": funnel["pool_raw"],
            "hard_blocked": sum(v for k, v in funnel["excluded_obj"].items()
                                if k != "quotes_missing"),
            "hard_reasons": hard_reasons,
            "missing": (list(pool_missing) + list(meta.get("missing") or []))[:6],
            "errors": list(meta.get("errors") or [])[:3],
            "mood_regime": meta.get("mood_regime"),
            "themes": list(meta.get("themes") or [])[:5],
            "funnel": funnel,
        })

    result = deep_analyze(",".join(codes), limit=_FUNNEL_DEEP_N)
    # funnel 紧跟 report_guide（SkillResult 渲染分流按键存在性判断，键序不影响渲染）
    if isinstance(result, dict):
        out = {"report_guide": result.get("report_guide", _REPORT_GUIDE),
               "funnel": funnel}
        out.update({k: v for k, v in result.items() if k != "report_guide"})
        return SkillResult(out)
    return result
