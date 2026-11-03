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

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ...log import logger

from ._helpers import select_strategy, build_report, resolve_names, profile_candidates as _profile_candidates, filter_candidates as _filter_candidates, tech_summary, predict_one, _mood_regime, prewarm_market_klines, cross_verdict, _num_or_none, _has_stretched_streak_risk
from ._facts import candidate_pool, market_state as _market_state, multitimeframe as _multitimeframe, detect_patterns as _detect_patterns, POOL_SOURCES, _PATTERN_PARAMS_DEFAULT
from .common import SkillResult, fetch_kline, warm_market_pool


# 单票深挖的总等待上限：外部源（龙虎榜/板块/资金）偶发挂起，不能拖死整轮选股
_INSPECT_DEADLINE_S = 15.0


def market_state() -> dict:
    """市场层面事实：资金流向、涨停/跌停家数、炸板率、板块强弱、情绪描述标签。

    2026-10-30 增：指数日内强弱（index_intraday/intraday_bias）、大盘资金近5日
    方向（fund_flow_days）、走弱环境标记（cautious=True 时选股应更谨慎）。

    返回:
        dict: fund_flow=当日净流入；zt_count/dt_count/broken_count/broken_rate=涨停/跌停/炸板；
            strong_sectors/weak_sectors=行业涨跌；mood/mood_score=描述性情绪标签（非入场规则）；
            index_intraday=三大指数涨跌/相对开盘位置；fund_flow_days=近5日大盘资金方向；
            intraday_bias=偏强/震荡/偏弱；cautious=走弱环境标记。
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
        sources: 逗号分隔的候选来源，取值 zt(连板≥2) / eod_zt(尾盘封板) /
            eod_start(尾盘启动: 刚启动+尾盘流入+未涨停) / dragon(龙回头) /
            hot(当日热点题材) / search(条件搜索) / recent_zt(近期涨停) / lhb(近10交易日龙虎榜)。留空用时段的默认组合。
        queries: 逗号分隔的 search_stocks 查询串，覆盖默认建议词
        search_top_n: 每条查询返回上限，默认 50
        min_turnover: 换手率下限；留空 = 不过滤（换手不足会在画像的 hard 里标记，由你决定）
        limit: 返回条数上限；留空 = 不截断

    返回:
        dict: strategy=时段；market=市场事实；candidates=候选列表；continuous_board /
            dragon_pullback / eod_zt / eod_start=各来源明细；main_themes=当日主线；missing=取数缺口。
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
                      theme{hit,tags},hard[],warnings[],tech{...},flow{...},missing[]}] /
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
        "## 买什么（N 只，N=实际符合条件数；可空；可加一句共性提示如「均需回踩再买，勿追高」）\n"
        "| 代码 | 现价 | 买点价位 | 止损价 | T+1/T+3/T+5 | 一句话理由 |\n"
        "每只一行；代码=6 位数字；"
        "★ 买点/止损写**关键位置**并注明是哪个位置（回踩 MA10 11.62 / 区间下沿 11.30 / "
        "前低 11.05 / 缺口上沿 11.40 任选其一，不要求是均线），"
        "禁止用 VWAP/分时均价当买点；止损写「跌破 X」/「不跌破 X」；"
        "T+N 填 看多/中性/看空。\n"
        "★ **全文用中文白话**，禁止输出 near_support / rank_cut 等字段名。\n"
        "## 不买/观察（每只一行：代码+拒因≤20字）\n"
        "## 风险（≤3 行）\n"
        "指标罗列、漏斗数字不进正文。保留代码+看多看空+跌破 X（喂 claims）。"
    ),
    "STEPS": "run_skill 一次即可交付；起手不要 get_hot_sectors / get_market_overview（market 已在本结果）；"
             "禁止 search_tools / read_skill / get_dragon_tiger / 二次 run_skill / 逐只 quote；"
             "**禁止 agent_get_kline / get_index_kline**——analyzed_rank 已给 ma5/ma10/ma20/hi20/lo20 "
             "绝对值，买点/止损/压力据此直接算，不需要原始 K 线；"
             "funnel.baseline=本次入选只数（选人层唯一出口，全部入 deep）；"
             "funnel.baseline_hard=被非封板 hard 规则拦掉的只数；"
             "funnel.baseline_risk=其中高连板且远离 MA5 的风险提示数；"
             "funnel 其余键（rounds/pool_raw/deep_n/eastmoney_ok）只用于判断取数是否完整",
    "buyable": "**只在盘中此刻（strategy=intraday）生效**：rt.limit_up=true 或 rt.limit_down=true → 只进排除表，禁止进可买表；"
               "盘后/尾盘一律放行——封板/跌停是事实不是排除令，可进「买什么」但要写明「当日已封板」"
               "与次日买点（一字板风险：需等开板或回踩支撑，不追集合竞价）；接不接由你的逻辑决定，须给理由；"
               "rt.buyable=false 是 deep 阶段的最终防线（选人层不再做池层预排除）；"
               "stale_bar 不影响此条",
    "seal": "封板结构（只描述今天封得怎么样，**不是可买性、不是分数**）：tag=none 未封板 / "
            "one_word 一字·全天封死 / open0 0开板非一字 / open1_2 开板1~2次回封 / "
            "open3_5 开板3~5次 / open6p 开板≥6次 / unknown 缺实时或池里无该票；"
            "seal_tier=thin(封单÷成交额≤5%)/mid(≤15%)/thick(≤40%)/vthick(>40%)；"
            "配套 open_count、seal_pct、zt_days。tag 与 seal_tier 是**两个正交事实**，要合起来读。"
            "开板越多越差；封单越厚越强；"
            "连板 2 板最弱、4+ 板最强。"
            "**组合要看**：open0 + thin（0 开板但封单极薄，如 000568）与 open6p 同为最可疑，"
            "别因为「没开板」就当强；one_word/vthick/thick 才是有厚封单支撑的强封。"
            "盘后以此估次日强弱，不因「买不到」降权；买得到买不到由你按当时段口径处理。",
    "entry_exit": "★ **顺序不能反，买点不是模板**：\n"
                  "① 先定姿态（读 market）：mood 偏强 + fund_flow_days 净流入 → 可放宽，允许给突破/追价"
                  "买点、容忍较高乖离、推荐只数可多；cautious=true 或 mood 偏弱 → 收紧，只给回踩买点、"
                  "买点压到更深的支撑、宁可少给；中性环境按个股自身结构定。"
                  "姿态是**每次按环境算**的，不是固定偏保守。\n"
                  "② 再挑**关键位置**（不是固定 MA5/MA10/MA20）：候选=MA5/MA10/MA20、hi20/lo20 区间边界、"
                  "前高前低、近期密集成交区、跳空缺口、涨停板起点；rank 每行已给两侧最近位置："
                  "**sup=现价下方最近（承接位）**、**res=现价上方最近（突破位）**；"
                  "选「离现价最近且当日量价确认」的那一个，写具体价 + 位置名。"
                  "同一个位置在强环境可直接挂、弱环境要打折（往下一档）。\n"
                  "③ 止损=该位置失效价（跌破 X）。买点必须高于止损；无明确止损价不得进「买什么」。"
                  "走坏=收盘破止损或量价净额转净流出。\n"
                  "④ **盘后交付默认给「条件单」而不是一口价**：次日 09:30-10:00 观察，三条同时成立才买——"
                  "(a) 突破 D0 最高价（rank 的 hi_today）；(b) 早盘成交明显放大（不是小幅放量）；"
                  "(c) 强于大盘（同期指数不涨它在涨）。任一条不成立 → 当天放弃，不追不补。"
                  "「早盘涨了才追」只看涨幅无效 ⇒ 要的是**放量突破**，不是单纯上涨。"
                  "买点列写「触发条件 + 触发后成交价上限（不高于 X）」；现价已高于上限的票写「等回踩到 X 再确认」。",
    "strength_rule": "★ 强度是**加减分项，不是准入闸**——在同一份名单里横向比强弱来排序，不是一刀切。"
                     "**加分**："
                     "① ret5_pct 高**且 zt_streak=1**（1 板上攻，实测该组净收益 +3.38%、触板率 50%，是最好的一组）；"
                     "② vol_ratio ≥1.5（D0 放量再启动）；"
                     "③ zt10 ≤2（10 日内涨停少，没在高位反复透支）；④ dist_ma5_pct 温和（几天内没翻倍）；"
                     "⑤ ret5_pct 偏低**且 zt_streak=0**（回调充分、无连板压顶，实测 +2.25%、深亏率 1%，属最好的一档）。"
                     "**减分**：ret5_pct 高**且 zt_streak ≥2**（涨得多还连板=位置透支，实测 −4.39%、深亏率 44%）、"
                     "ret5_pct 高**且 zt_streak=0**（涨了不少却没板=上行乏力，实测 −1.16%，别当强势加分）、"
                     "zt10 ≥3（高位反复透支）、vol_ratio <1（缩量却给强度结论）。"
                     "**缩量回踩是减分不是加分**。"
                     "★ 位置透支看三件套（dist_ma20_pct / pos20 / ret5_pct）：三项都高 = 深亏高发区（实测该档 −3.51%、"
                     "深亏率 35%）；三项都低 = 位置干净（历史样本 +3.55%、深亏率 1%，可关注但非必反转）。"
                     "**这一维度是排序参考的首要依据**，优先看它。rank 已给合成档 **loc**（hot=透支 / cool=位置干净 / normal），见 location 条。"
                     "加分项不足 2 条的进「不买/观察」并注明缺哪条；"
                     "**数量不是考核项**：弱市给 1 只也合规，凑数才是错的；环境转强可多给，但要写明成立的是哪几条。",
    "stance_rule": "禁止「一律回踩均线」，也禁止**把 rank 的 sup.px 直接当买点**——sup 只是「现价下方"
                   "最近的位置」，照抄的结果是三只票买点全在现价下方、当天一只也买不到。"
                   "若所有票的买点都等于 sup.px 或都写成 MA5/MA10，说明没读环境、在用模板。"
                   "强环境（mood 偏强 + fund_flow_days 净流入）该给的是「突破 res 追」或「不破 sup 持有」；"
                   "弱环境才把买点压到 sup 下方一档，并且只留能当日成交的。"
                   "**买点形态要跟着强度走**：强度加分项多的给「突破 hi_today 追」，加分项少的只给回踩；"
                   "全名单买点长得一模一样（全回踩或全突破）同样是没排序。",
    "horizon": "表内只写 T+1/T+3/T+5（词表：看多/中性/看空）；T+10/T+20 不写。"
               "zt_streak>=3 只写 T+1。",
    "priority": "支撑与弱转强/强转弱 ≫ 涨跌幅；禁止按涨跌幅排序",
    "support_rule": "盘中红绿不决定安全；可怕的是无支撑阴跌。-8%弱转强+有支撑 可以比 +8%转分歧更安全",
    "fields": "analyzed_rank = **全名单**一行一只（不截断）：price/chg_pct/"
              "chg_prev_pct（昨日涨跌%）/cpos（收盘位 0~100=贴日高）/"
              "ma5/ma10/ma20/hi20/lo20（价位绝对值）/**hi_today**（当日最高=次日突破基准；缺键即等于 hi20）"
              "/**sup**/**res**={k 位置名, px 价位, d 距离%}"
              "（k∈ma5|ma10|ma20|ma60|lo10|lo20|lo60|hi20|gap 缺口下沿|poc 密集区|zt 涨停起点，"
              "已按现价分侧：sup=**现价下方最近**的关键位置、res=**现价上方最近**的关键位置）"
              "/**强度四件套** ret5_pct（前5日涨幅%）/zt10（近10日涨停次数）/zt_streak（当前连板数）"
              "/vol_ratio（今日量比）/**vr_rel**（**全天口径** 量比÷大盘量比；≥2=校准确认，<2 不买；"
              "绝对量比≥2 但 vr_rel<2=跟风放量（大盘同放）；<1=放量不及大盘）"
              "/**vr_rel_st**（**分时口径，仅盘中给**：截至 intraday.t 的量比÷大盘同刻量比；"
              "盘中买前闸用它）/**flow_rel**（全天：净占比−大盘净占比，百分点）"
              "/**flow_rel_st**（分时同口径）/dist_ma5_pct/dist_ma20_pct"
              "/regime（**日线**形态标签，动作映射见 regime 条）"
              "/shape（**分时**形态标签，见 intraday_shape 条；与 regime 并列、互不覆盖，用来相互印证）"
              "/rs_d0/rs_5d（相对上证超额%，见 regime 条）"
              "/loc（**位置透支分档** hot/cool/normal，见 location 条）"
              "/seal/seal_tier/seal_pct/net5d_wan/net20d_wan/streak_dir；"
              "20 日位置直接读 pos20（0=区间最低、100=最高，无需自算）；"
              "满卡 tech.support.supports/resists 是全量候选（各 ≤4/≤3 条），要横向比多个位置才看它；"
              "**缺键即正常**：无 limit_up=未涨停、无 buyable=可买、无 stale_bar=日线未滞后、无 risks=无风险、"
              "无 sup/res=该侧无关键位置（如已在高位无压力）、无 seal=今日未封板、无 zt_streak=今日没连板、"
              "无 name=名称未取到（用代码即可）、无 chg_prev_pct/cpos=日线不足（参考字段缺省）、"
              "无 vr_rel/flow_rel=大盘基准缺失（未校准，按绝对值读）、"
              "无 vr_rel_st/flow_rel_st=非盘中（收盘后同刻≡全天，直接用全天 vr_rel/flow_rel）、"
              "无 confidence=证据满维。"
              "analyzed = 满卡（只含部分票，有 patterns/flow/rt/intraday 明细），看明细用它、比强弱用 rank；"
              "intraday = {t 截断时刻, day 目标日, vr_same_t 个股同刻量比, vr_rel_st, "
              "fp_same_t 个股同刻净占比%, flow_rel_st, ret_same_t 昨收→t 涨幅%, "
              "idx_ret_same_t 上证同期涨跌%, rs_same_t 超额, mkt_vr/mkt_fp_pct 大盘同刻基准, "
              "mkt_fp_prev_pct 前5日同刻净占比均值}；"
              "主论据= 强度四件套 / MA 排列 / flow / 位置透支档；rt 看现价/涨跌停",
    "board_cut": "zt_streak>=3 默认只写 T+1",
    "words": "方向用 看多/中性/看空/回避；用户问持仓才写「兑现」",
    "market": "mood/资金/热点已由 run_skill 覆盖；不要再 get_market_overview / get_hot_sectors；"
              "**环境决定姿态，两个方向都要用**：cautious=true 或 mood 偏弱 → 提高门槛、减少只数、"
              "只做有支撑的票、买点压深一档；mood 偏强 + fund_flow_days 净流入 → 放宽（可给突破买点、"
              "容忍较高乖离、只数可多）。不要只取「收紧」这一半。"
              "大盘本身最多进「风险」一行轻注，不得当筛选主条件",
    "fund_trend": "资金看多日总体：flow.recent（近5/20日累计、连续方向、逐日符号、剔除最大单日）≫ 当日 net_wan；"
                  "单日大额流入不等于趋势，连续3日以上同向才算趋势；剔除最大单日后仍为正才是普遍承接。"
                  "★**口径**：net_wan / net5d_wan / net20d_wan 都是 `caliber=price_direction` 的"
                  "**量价方向净额**（按分钟涨跌方向累计成交额派生），**不是东财四档主力净额**，"
                  "两者量级不同、不可换算 ⇒ 正文只能写「量价净流入/资金净额/净流入 X 亿」，"
                  "**禁止写「主力净流入」「主力资金」「主力承接」**，也不得拿它推算主力持仓或控盘度",
    "pump_dump": "flow_risks/warnings 含「开盘脉冲」「疑似拉高出货」「趋势背离」→ 拉高出货嫌疑，"
                 "除非有强反证（如尾盘重新放量收回），否则降权或不买",
    "funnel": "漏斗数字只允许出现在末行「数据」；禁止写进正文",
    "regime": "regime=位置标签（缺键=低位蓄势，中性不标注）。**只报位置与跨日方向，不判盘中动作**"
              "（一鼓作气/再而衰/三而竭属分时概念，日线不评，可买否看盘中形态）："
              "**高位**(前5日>10% 且当日涨>2%，或跑输大盘大跌——只报位置)/**高位分歧**(高位横盘)"
              "/**跳水回踩**(高位大跌但 rs_d0≥-1.5=跌主要来自大盘带崩、非个股弱；关键位企稳即可按回踩接，当日反抽不追)/**弱转强**(低位昨跌今涨，真伪看量比："
              "量比≥2 才可追，否则只给回踩支撑"
              "；校准确认闸：买（回踩接/量比关过）须 vr_rel≥2——"
              "vr_rel<2 一律不买（含跟风放量=绝对量比≥2 但 vr_rel<2）；"
              "vr_rel≥2=放量的校准确认（绝对量比<2 但 vr_rel≥2=大盘缩量它放量，真强优先）；"
              "加分（不写死）：量比关已过再看价效高（涨幅÷量比高=价升量稳/惜售）可上调置信，单看惜售不加分"
              "/低位启动(低位今涨，昨未跌，看支撑)/低位走弱（按普通档读、受校准闸约束）。"
              "rs_d0/rs_5d=个股−上证同期超额(%)（参考数据，rs_d0≥-1.5 拆分见跳水回踩），"
              "vr_rel/flow_rel=量能/资金流对大盘校准（参考数据）。"
              "**大盘暴跌、个股横盘（rs_d0 明显为正）=有支撑**（跳水回踩拆分即此义，"
              "带崩≠个股弱）。"
              "庄股例外：与大盘恒反向、量能自锁（放量缩量不随大盘）的票按独立行情读。"
              "盘中用 intraday 的 vr_rel_st/flow_rel_st（同刻口径，比全天直接）。"
              "**买前确认闸 = 量能对大盘的校准 ≥2，唯一闸**：有 intraday 块（盘中）"
              "用 **vr_rel_st≥2**，无 intraday（收盘后）用 **vr_rel≥2**；"
              "口径不同（一个截至此刻、一个是全天含昨日），**别混用、别互相印证**。",
    "location": "loc=位置透支分档（由 dist_ma20_pct/pos20/ret5_pct 三件套合成，任一项缺失=无 loc，"
                "按单字段读不猜档）。**loc 是风险提示，不是准入闸**："
                "**hot**=三项全高且非 1 板（位置透支，实测 122 笔 −4.38%、深亏率 39%，D+1 接盘深亏高发，"
                "提示谨慎/降仓；若 zt_streak=1 则不算 hot——1 板上攻是另一回事，见 strength_rule ①）；"
                "**cool**=三项全低（回调充分的位置，实测 280 笔 +2.53%、深亏率 0%、触板率 24%，"
                "提示位置干净、可纳入关注，但低位不必然反转，非优先买入信号）；normal=其余（按单字段正常读）。"
                "阈值为 zt+dragon 池样本内分位数，换池/上生产须样本外复核；阈值走 QD_LOC_* 环境变量。",
    "cross": "日线 regime × 分时 shape 的叠加判定（**确认层，不是覆盖层**：cross 是「两侧矛盾时的额外提示」，**不自动高于任一单侧读数**，单侧已明确时以单侧为主）："
             "fake_strong=位置已高（高位/高位分歧）却走出分时强形态。**★ 本格结论主要由「高位+涨停封死」样本驱动**"
             "（133 笔里 114 笔是封板，−3.20%、深亏 45 只集中在此子格）——封板这一子格的「不追」有实测支撑；"
             "非封板的 fake_strong 样本少且方向不一，**不可泛化为「高位偏强即不追」**。"
             "fake_weak=跨日刚转强（弱转强/低位启动）但当日盘中回踩（51 笔 +1.70%、胜率 65%、深亏仅 1 只）⇒ 洗盘非走坏，别杀；"
             "double_strong=两边都强（正常，+0.82%）；oversold_rebound=低位走弱后盘中拉强（超跌反弹 +2.50%，不算假强）；"
             "high_weak=高位且日内也弱（−1.88%）。缺 cross=无叠加结论（两侧不构成冲突或数据缺），按单侧读。"
             "★ cross 与单侧冲突时**不默认以 cross 覆盖**：先核对是哪一子格、样本是否支撑，再决定权重。",
    "intraday_shape": "分时形态 shape（**独立维度：只强化、精细化、印证日线 regime，不改写日线判断**）："
                      "weak_to_strong 先破低→收复均价站稳且翻红 / weak_to_divergence 先破低→收复过但近端没站住 / "
                      "strong_to_weak 先冲高→跌破均价压不回 / strong_to_divergence 先冲高→回撤卡在均价附近 / "
                      "sustained_strong·sustained_weak 单边 / limit_up_locked·limit_down_locked 封板 / flat 无转折。"
                      "配套：first_extreme 先见低还是先见高、vwap_dist 距均价%、above_ratio·above_recent 在均价上方的"
                      "时间占比、cur_ret·peak_ret·trough_ret、dd_from_peak 距当日高点回撤%、vol_up_down 涨放跌缩(>1)。"
                      "★**同名不同义、不得互相覆盖**：日线 regime 的「弱转强」看跨日位置，分时 weak_to_strong 看"
                      "当日盘中换手；两者并列印证——双向都弱转强=最强，日线弱转强+分时 strong_to_weak=日内已走坏（降权），"
                      "日线强+分时 weak_to_divergence=日内转分歧（等确认再买）。",
}


# 进 deep 的最终候选数（保底轨上限 25；N 与满卡的权衡曲线见 tmp/probe_budget_split_20261101）
_FUNNEL_DEEP_N = 45
_FUNNEL_SEARCH_TOP_N = 100  # run() 扩池：每条查询拉满（pre_screen 默认 50 保留给手工调用）

# ── 保底选人层（选人层唯一出口；沿用 8.1.0 数量 / hard 口径）─────────────────
# 【定位】保底候选独立画像并恒入 deep；按「连板高度降序 → 当日收盘位置更低者先」
#   排序后取前 _BASELINE_N 只（2026-11-03 起为单链路：objective_preexclude +
#   score_pool 加法层已整体移除）。
# 【2026-10-30 扩容】8 只 → 25 只，且「涨停封板」只在盘中算买不进、其余时段
#   放行（见 _baseline_blocked）：旧口径下池首批封板占满时基线会塌缩到 1~2 只，
#   顺延出的替代票强度更弱；改为把「能不能买」交给 LLM，代码只留客观不可选。

_BASELINE_N = 25
_BASELINE_WINDOW_ENV = "QD_FUNNEL_BASE_WINDOW"
_BASELINE_RANK_ENV = "QD_FUNNEL_BASE_RANK"
_BASELINE_YIZI_ENV = "QD_FUNNEL_BASE_YIZI"
_BASELINE_HOT_ENV = "QD_FUNNEL_BASE_HOT"


def _baseline_window() -> int:
    """保底轨画像窗口（只数）：0 = 全池（缺省）。

    env QD_FUNNEL_BASE_WINDOW 可回退固定窗口（旧口径 60 = 本值 30×2）。
    全池与 60 只窗口的在线实测耗时：388 只画像 50.8s vs 30 只 33.4s
    （边际 +17.4s，大头是固定开销），故缺省去掉窗口。
    """
    try:
        v = int(os.getenv(_BASELINE_WINDOW_ENV, "0") or "0")
    except (TypeError, ValueError):
        return 0
    return v if v > 0 else 0


def _baseline_rank() -> str:
    """保底轨排序键：zt_cpos（缺省）/ zt。

    p_up 口径（闸 + 排序 + 回退开关）已整体移除（2026-11-03 用户要求），
    保底轨任何路径不再出现 p_up。
    """
    v = (os.getenv(_BASELINE_RANK_ENV, "") or "zt_cpos").strip().lower()
    return v if v in ("zt_cpos", "zt") else "zt_cpos"


def _baseline_sort_key(p: dict) -> tuple:
    """保底轨排序键（越大越先）：连板高度 → 当日收盘位置更低 → 非风险票。

    缺省 zt_cpos 的两个分量都是**画像已有字段**（tech.zt_streak /
    tech.close_pos_in_day），零新增计算、零额外取数；风险票（连板≥3 且
    距 MA5>10%）只在同高度同收盘位置时垫底，不硬排除。
    键的选择依据见 tmp/probe_rank_keys_20261103.py（34 决策日对比）。
    """
    tech = p.get("tech") if isinstance(p.get("tech"), dict) else {}
    streak = _num_or_none(tech.get("zt_streak")) or 0
    if _baseline_rank() == "zt":
        return (streak,)
    cpos = _num_or_none(tech.get("close_pos_in_day"))
    return (streak, -(cpos if cpos is not None else 50.0),
            not _has_stretched_streak_risk(p))


def _baseline_yizi_on() -> bool:
    """保底轨是否剔除「上一交易日一字板」票（涨/跌都剔）。env QD_FUNNEL_BASE_YIZI，缺省 1。"""
    return (os.getenv(_BASELINE_YIZI_ENV, "1") or "1").strip() != "0"


def _baseline_hot_on() -> bool:
    """保底轨是否剔除「位置透支」票（location=hot）。env QD_FUNNEL_BASE_HOT，
    缺省 1（2026-11-03 用户指令：fake_strong 剔除回退后换 hot 剔除，口径可切）。"""
    return (os.getenv(_BASELINE_HOT_ENV, "1") or "1").strip() != "0"

def _is_yizi_hit(p: dict) -> bool:
    """画像自带的一字板标记（`_helpers._yizi_hit` 用同一份 bars 在画像时算出）。

    与画像同源 ⇒ 不做二次取数、不会引入额外 IO；标记缺失视为判不出（不剔除）。
    """
    return bool(p.get("yizi_hit"))


def _is_hot_loc(p: dict) -> bool:
    """画像自带的 location 合成档 == hot（`_helpers.location_tier`：三件套全高
    且非 1 板 = 位置透支）。任一项缺失档为 unknown ⇒ 不剔除（不猜）。"""
    tech = p.get("tech") if isinstance(p.get("tech"), dict) else {}
    return tech.get("location") == "hot"

# 源失败条目识别标记（2026-10-21）：missing 里出现这些子串 = 候选源/市场源故障，
# 而不是数据缺口。candidate_pool 的 zt/龙回头/热点用「<源>源失败: …」措辞，
# search 沿用既有「条件搜索[q]: …」，market_state 源错误经 pre_screen 加「市场状态: 」前缀。
_SRC_FAIL_MARKS = ("源失败", "条件搜索[")


def _baseline_blocked(flags, strategy: str) -> bool:
    """基线是否因 hard 被拦：涨停封板只在盘中（intraday）算买不进，其余时段放行。"""
    for f in (flags or []):
        if "涨停封板" in str(f) and strategy != "intraday":
            continue
        return True
    return False


def _baseline_selection(candidates: list, prescreen_result: dict) -> tuple:
    """选人层保底选择（hard 过滤 + 全池按连板高度排序截断 N 只）。

    **2026-11-03 去掉「窗口 60 + p_up 闸」**（用户裁定）：原实现只给池序前 60 只画像、
    再按 p_up 硬闸 + p_up 降序截断，实测两处都反向——
      · 窗口 60 是「按来源插入序」的隐形排序（zt 先、dragon 后），把龙回头/龙虎榜
        挤出画像，等于截断前先按「谁在池里靠前」选人；
      · p_up 作排序键的「深亏率 / 触板率」= 1.08（各键最差），把高波动票顶到最前。
    现在：画像**整个候选池**（`_baseline_window()`=0），hard 过滤后按
    `_baseline_sort_key()` 排序取前 `_BASELINE_N` 只（缺省键 = 连板高度降序，
    同高度取当日收盘位置更低者）。34 决策日实测（09-01~10-26，recon 池）：
    触板 33.5%→32.0%、深亏 25.5%→23.4%、净 −2.92%→−2.46%、胜率 42.3%→44.0%
    （见 tmp/probe_rank_keys_20261103.py）。

    hard 口径（2026-10-30 用户裁定）：ST/退市、停牌、价格越界、低换手仍拦；
    「涨停封板」只在**盘中此刻**算买不进（intraday），eod/post_market 一律
    放行 —— 可买性交 LLM 判断。旧口径在所有时段排掉封板，池首批封板占满时
    基线会塌缩到 1~2 只。

    宽池（扩词 + search_top_n=100）是核定扩围：源插入序 zt/eod_zt→eod_start→
    dragon→hot→search→lhb 不变；仅当非 search 源不足时窗口尾部才进 search 结果。

    回退：`QD_FUNNEL_BASE_RANK=zt` 只按连板高度排；`QD_FUNNEL_BASE_WINDOW=30`
    恢复固定窗口。p_up 硬闸 + p_up 降序的旧口径回退已整体移除
    （2026-11-03 用户要求：保底轨不再出现 p_up，连带删掉 `with_pred` 打分）。

    **一字板剔除（2026-11-03 用户指令，口径可切）**：排序后按 `_is_yizi_hit` 读画像自带
    标记，命中则跳过并顺延补位，名额仍取满 `_BASELINE_N`。缺省口径 = **近 3 个交易日
    一字涨停**（`QD_FUNNEL_BASE_YIZI_WIN`，不含当日；`_MODE=both` 可含跌停）。
    34 决策日实测（zt+dragon+lhb，同名额补位，对比不剔）：近3日涨停 净 −2.42%→−2.29%、
    深亏 22.2%→20.5%、t=+0.63；昨日(涨/跌) −2.17%、深亏 21.1%、t=+1.87；近5日涨停 −2.29%；
    含当日 −2.45%（无益）。**窗口越长深亏越低、但补位票更弱，净收益改善反而变小**。
    env `QD_FUNNEL_BASE_YIZI=0` 关闭；振幅容差 `QD_FUNNEL_BASE_YIZI_AMP`（0.15%）。
    取不到日线时不剔除（不因数据缺失误杀）。

    **hot 位置剔除（2026-11-03 用户指令，口径可切）**：排序后对同一名额池再跳过
    「位置透支」（`_is_hot_loc`：location=hot，三件套全高且非 1 板）并顺延补位。
    34 决策日实测（09-01~10-26，zt+dragon recon 池，对比不剔）：触板 28.6%→22.0%、
    深亏 16.9%→10.1%、净中位 −3.03→−3.16（见 tests/test_market_screener_selection_bt.py
    探针 probe_prod_skip_hot）。分时口径的 fake_strong 剔除（高位×分时强）同窗实测
    21.0% / 11.3% / −3.42，全面劣于本口径，已回退删除。档缺失（unknown）不剔除；
    env `QD_FUNNEL_BASE_HOT=0` 关闭。

    **昨榜龙虎榜净买入剔除已回退（2026-11-03 用户裁定「改进太小」）**：叠加 hot 剔
    实测 21.7 触/10.3 深/净 −3.03（对比 hot 单独 22.0/10.1/−3.16 仅净收益微改善），
    判据与回测口径见 `tests/test_market_screener_selection_bt.py` 探针
    probe_prod_skip_netbuy_d1 / probe_prod_skip_hot_netbuy（保留可复跑）。

    Args:
        candidates: pre_screen 返回的**原始**候选列表（objective 之前）
        prescreen_result: pre_screen 全量返回（取 strategy/market/main_themes）

    Returns:
        (baseline, window_hard_n): baseline=按排序键取前 _BASELINE_N 的画像列表；
            window_hard_n=画像范围内被**非封板** hard 拦掉的只数（漏斗复盘用）
    """
    if not candidates:
        return [], 0
    limit = _baseline_window() or len(candidates)
    bundle = _profile_candidates(
        {"strategy": prescreen_result.get("strategy"),
         "market": prescreen_result.get("market") or {},
         "main_themes": prescreen_result.get("main_themes") or [],
         "candidates": candidates},
        limit=limit, with_flow=False, with_pred=False)
    window = bundle.get("profiles") or []
    strategy = prescreen_result.get("strategy") or ""
    window_hard_n = sum(1 for p in window
                        if _baseline_blocked(p.get("hard"), strategy))
    eligible = [p for p in window
                if not _baseline_blocked(p.get("hard"), strategy)]
    eligible.sort(key=_baseline_sort_key, reverse=True)
    yizi_on = _baseline_yizi_on()
    hot_on = _baseline_hot_on()
    if yizi_on or hot_on:
        picked, yizi_n, hot_n = [], 0, 0
        for p in eligible:
            if len(picked) >= _BASELINE_N:
                break
            if yizi_on and _is_yizi_hit(p):
                yizi_n += 1
                continue
            if hot_on and _is_hot_loc(p):
                hot_n += 1
                continue
            picked.append(p)
        if yizi_n:
            logger.info("[MktScreen] 保底轨剔除昨日一字板 %d 只（名额补位至 %d）",
                        yizi_n, len(picked))
        if hot_n:
            logger.info("[MktScreen] 保底轨剔除位置透支(hot) %d 只（名额补位至 %d）",
                        hot_n, len(picked))
        baseline = picked
    else:
        baseline = eligible[:_BASELINE_N]
    return baseline, window_hard_n


# ── deep 交付预算（rank 全量 + 满卡按预算）────────────────────────────────
# rank 行实测 ~603 字符/行（含 sup/res/vr_rel/flow_rel 等全字段），45 行 ≈27K；
# 满卡实测 ~2,017 字符/张 ⇒ rank 全量 + 满卡全量会远超闸，故按预算交付：
# rank 给全部票的横向可比字段，analyzed 只放装得下的前 N 张满卡；
# 单只满卡用 inspect_stock(code) 取。
# 预算链：本值 < observation 闸 45000 < 预览闸 47000（skill_run 整条目裁剪），
# 超出 observation 会截碎 JSON、超出预览会尾部整条丢票，故三者联动抬升（2026-11-01）。
_DEEP_WORKERS = 16              # deep 并发：45 只名单 ≈3 波
_DEEP_BUDGET_CHARS = 44500      # observation 闸 45000 内留 500；超出由 _pack 实测回退减卡
_DEEP_BUDGET_ENV = "QD_SCREENER_DEEP_BUDGET"


def _deep_budget() -> int:
    """满卡预算（字符）：env 优先，非法/≤0 回落缺省。"""
    try:
        v = int(os.getenv(_DEEP_BUDGET_ENV, "") or "")
    except (TypeError, ValueError):
        return _DEEP_BUDGET_CHARS
    return v if v > 0 else _DEEP_BUDGET_CHARS


def _delivery_key(card: dict) -> tuple:
    """交付序（升序）：按「位置安全度」排，**p_up 不参与排序**。

    三个分量都取画像现成字段，零新增计算：
      ① 距 20 日均线偏离度 `dist_ma20_pct` → **落在 [-10%, +5%] 视为贴近均线**，
         区间内记 0，区间外记超出量。注意不是「越负越好」：越负是断崖下跌票
         （实测中位 -25% 那组，深亏 9.8%），贴均线那组才最低（4.5%）。
      ② `pos_in_20d_pct`（20 日区间分位 0~100）→ 距 35 分位越近越好，
         要的是「偏低位但非最低」，不是抄在 20 日地板。
      ③ `ret_5d_pct`（前 5 日涨幅%）→ 只罚上涨不罚下跌（占位用 max(0, x)），
         避免把「刚跌过一波」当成负面而推高追涨票。
    缺值按最差处理（沉底），不猜。

    为什么不用 p_up 排：p_up 是**波动档**不是质量分，高 p_up 组触板率与深亏率同时最高。
    实测（每日前 6 只 = 满卡张数，深亏 = 持仓期最低价 ≤ -15%）：
      · 9-10 月弱势窗：p_up 降序 30.9% → 本键 7.0%（逐日配对 t=-5.88），
        净收益 -1.42% → -1.86%（代价 0.44pp）；
      · 4-5 月强势窗：19.4% → 3.8%（t=-5.56），净 +0.70% → -0.63%（代价 1.33pp），
        T+3 仍为正（+0.14%）。
    即「深亏大幅下降、收益小幅让出」，且弱市收益代价小于「越负越前」那种极端版。
    数据见 tmp/probe_deep_order_20261103.py。
    """
    tech = card.get("tech") if isinstance(card.get("tech"), dict) else {}

    def _v(key, miss):
        x = _num_or_none(tech.get(key))
        return miss if x is None else x

    d20 = _v("dist_ma20_pct", 99.0)
    band = 0.0 if -10.0 <= d20 <= 5.0 else min(abs(d20 + 10.0), abs(d20 - 5.0))
    return (band,
            abs(_v("pos_in_20d_pct", 50.0) - 35.0),
            max(0.0, _v("ret_5d_pct", 99.0)))


def _rank_row(card: dict) -> dict:
    """把一张满卡压成一行横向可比字段（只搬已有值，不做新计算）。

    价位 ma5/ma10/ma20/hi20/lo20 让 LLM 直接算买点/止损/压力：只给百分比推不出
    具体价，模型会自己去调 agent_get_kline 补（实测一次 19.9s / 3.3k token）。
    sup/res 是同一个「关键位置」表的**两侧**（sup=现价下方最近、res=现价上方最近）：
    只给 sup 时模型的买点会 3/3 直接照抄 sup.px ⇒ 全部低于现价 ⇒ 可执行 0 只，
    强环境该用的「突破 res 追」分支根本没有数可用（2026-10-31 对拍实测）。
    强度四件套 ret5_pct / zt10 / vol_ratio / dist_ma5_pct 是**加减分项**（不是闸）：
    实测封板组与炸板组在这四项上 D0 就已分开（前 5 日 +26.1% vs +13.9%、D0 量比
    3.11 vs 1.24、10 日涨停 1.5 vs 3.1 次），不给数值就只能靠模型读 K 线。
    hi_today 是条件单的突破基准（次日 09:30-10:00 突破它才买），否则模型只能给
    「回踩现价下方」这一种买点。
    默认值不写（limit_up=False / stale_bar=False / risks=0 / buyable=True / 无 res /
    seal=none 未封板 / confidence=1.0 满证据 / zt_streak=0 无连板 / 空 name /
    hi_today==hi20 当日最高就是 20 日最高），缺键即正常，
    口径见 _REPORT_GUIDE["fields"]。limit_up 与 seal 冗余（seal 存在即封过板），
    只在 True 时出现。
    """
    rt = card.get("rt") or {}
    tech = card.get("tech") or {}
    st = card.get("intraday") or {}
    _sh = (card.get("shape") or {}).get("shape")
    rec = (card.get("flow") or {}).get("recent") or {}
    sup = tech.get("support") or {}
    seal = card.get("seal") or {}
    row = {
        "code": card.get("code"),
        "name": card.get("name"),
        "price": rt.get("price"),
        "chg_pct": rt.get("change_pct"),
        "chg_prev_pct": tech.get("chg_prev_pct"),
        "cpos": tech.get("close_pos_in_day"),
        "pos20": tech.get("pos_in_20d_pct"),
        "ma5": tech.get("ma5"),
        "ma10": tech.get("ma10"),
        "ma20": tech.get("ma20"),
        "hi20": tech.get("hi20"),
        "lo20": tech.get("lo20"),
        "hi_today": tech.get("hi_today"),
        "seal": seal.get("tag"),
        "seal_tier": seal.get("seal_tier"),
        "seal_pct": seal.get("seal_ratio_pct"),
        "vol_ratio": tech.get("vol_ratio"),
        "vr_rel": tech.get("vr_rel"),
        "vr_rel_st": st.get("vr_rel_same_t"),
        "ret5_pct": tech.get("ret_5d_pct"),
        "zt10": tech.get("zt_10d"),
        "zt_streak": tech.get("zt_streak"),
        "dist_ma5_pct": tech.get("dist_ma5_pct"),
        "dist_ma20_pct": tech.get("dist_ma20_pct"),
        "loc": tech.get("location"),
        "sup": (sup.get("supports") or [None])[0],
        "res": (sup.get("resists") or [None])[0],
        "net5d_wan": rec.get("net_5d_wan"),
        "net20d_wan": rec.get("net_20d_wan"),
        "streak_dir": rec.get("streak_dir"),
        "flow_rel": (card.get("flow") or {}).get("flow_rel"),
        "flow_rel_st": st.get("flow_rel_same_t"),
        "regime": tech.get("regime"),
        "shape": _sh if _sh != "flat" else None,
        "rs_d0": tech.get("rs_d0"),
        "rs_5d": tech.get("rs_5d"),
    }
    for _k in ("sup", "res"):
        if row.get(_k) is None:
            row.pop(_k, None)
    for _k in ("chg_prev_pct", "cpos", "pos20", "loc", "vr_rel", "flow_rel", "vr_rel_st", "flow_rel_st", "shape"):
        if row.get(_k) is None:
            row.pop(_k, None)
    if row.get("regime") in (None, "低位蓄势"):
        row.pop("regime", None)
    if row.get("loc") in (None, "unknown"):
        row.pop("loc", None)
    if not row.get("name"):
        row.pop("name", None)
    if row.get("hi_today") is not None and row.get("hi_today") == row.get("hi20"):
        row.pop("hi_today", None)
    if row.get("seal") == "none":
        row.pop("seal", None)
    if row.get("confidence") == 1.0:
        row.pop("confidence", None)
    if row.get("zt_streak") == 0:
        row.pop("zt_streak", None)
    if rt.get("buyable") is False:
        row["buyable"] = False
    if rt.get("limit_up"):
        row["limit_up"] = True
    if not row.get("seal_tier"):
        row.pop("seal_tier", None)
        row.pop("seal_pct", None)
    if card.get("stale_bar"):
        row["stale_bar"] = True
    _risks = len(card.get("rt_risks") or []) + len(card.get("flow_risks") or [])
    if _risks:
        row["risks"] = _risks
    # 日线 × 分时叠加判定：只给非 neutral 的（省字符，neutral 即无结论可下）。
    # regime 缺键 = 低位蓄势（见上方「默认值不写」约定），这里要补回默认值再判，
    # 否则所有中性票都会被当成「无 regime」跳过叠加。
    _cv = (cross_verdict(tech.get("regime") or "低位蓄势", _sh) or {}).get("verdict")
    if _cv and _cv != "neutral":
        row["cross"] = _cv
    return row


def _dumps_len(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def _elapsed(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _log_run(t0: float, ms: dict, funnel: dict, result) -> None:
    """run() 分段耗时日志：服务侧「慢在哪」的唯一依据。

    外部探针只能复现干净进程的耗時；服务进程内因 scheduler / 复权因子 / 共用连接池
    竞争可差出数十秒，没有这条日志无法归因。total 含预热与序列化，
    故 total ≥ 各段之和。
    """
    if not isinstance(result, dict):
        return
    cards = result.get("analyzed") or []
    rank = result.get("analyzed_rank") or []
    baseline = funnel.get("baseline") or 0
    logger.info(
        "[MktScreen] run %.1fs pool=%d pre_screen=%.1fs profile=%.1fs score=%.1fs "
        "deep=%.1fs rounds=%d baseline=%d deep_n=%d cards=%d chars=%d",
        (time.perf_counter() - t0),
        funnel.get("pool_raw") or 0,
        ms.get("pre_screen", 0) / 1000.0,
        ms.get("profile", 0) / 1000.0,
        ms.get("score", 0) / 1000.0,
        ms.get("deep", 0) / 1000.0,
        funnel.get("rounds") or 0,
        baseline,
        funnel.get("deep_n") or 0,
        len(cards),
        _dumps_len(result),
    )


def _slim_card(card: dict) -> dict:
    """满卡瘦身，省下的字符换成更多满卡张数（预算与 observation 闸都是硬顶）。

    去掉的只有三类：① evidence_dims（与 confidence 同源，可推）；② 空容器
    （missing/patterns/flow_risks 为 []）；③ 值为 None 的键与 stale_bar=False。
    seal 未封板时除 tag 外全是 None，只留 tag 即可表达「今天没封板」。
    不删任何有信息的字段——尤其 tech/flow/rt 明细一个不动。
    """
    out = {}
    for k, v in card.items():
        if k == "evidence_dims":
            continue
        if k in ("missing", "patterns", "flow_risks") and not v:
            continue
        if v is None or v == "":
            continue
        if k == "stale_bar" and v is False:
            continue
        if k == "seal" and isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if vv is not None}
            continue
        out[k] = v
    return out


def _full_card_quota(head: dict, cards: list, rank: list, budget: int) -> int:
    """预算内能放几张满卡：head（信封）+ rank（全量）先扣，余量按整卡装。"""
    avail = budget - _dumps_len(head) - _dumps_len(rank)
    if avail <= 0:
        return 0
    n = used = 0
    for c in cards:
        size = _dumps_len(c) + 2
        if used + size > avail:
            break
        used += size
        n += 1
    return n


def deep_analyze(codes: str, limit: int = 8, with_flow: bool = True,
                 with_patterns: bool = True, with_mtf: bool = False,
                 budget: int = None) -> dict:
    """对指定股票批量汇总全部可用证据，并按标定模型的 P(次日涨) 排序。

    这里**没有**主观评分梯形：唯一的数字是 _helpers.predict_one 给出的已标定 p_up。
    你要加权、改口径、加自己的判断都可以 —— 请基于返回的 tech / patterns / flow / mtf 自己写。

    参数:
        codes: 逗号分隔股票代码，如 "000001,600519,300750"
        limit: 最多分析几只，默认 8
        with_flow: 是否取资金流
        with_patterns: 是否识别 K 线形态
        with_mtf: 是否取多周期结构（需要分钟源，更慢）
        budget: 整份返回值的字符预算（含信封+rank+满卡）；默认 QD_SCREENER_DEEP_BUDGET
            （29500）。run() 会把外层 market/hints/funnel 的体量与随后追加的 src 标注
            一并扣掉再传，使「最终交付」而不是「本函数输出」受预算约束。

    返回:
        dict: analyzed_rank[]=**全部票**的横向可比字段（code/name/p_up/direction/
            confidence/price/chg_pct/**ma5/ma10/ma20/hi20/lo20**（价位绝对值）/
            **sup**/**res**={k 位置名, px 价位, d 距离%}（sup=现价下方最近、res=现价上方最近的
            关键位置，k 见 tech_summary 的 _key_levels：均线/近端低点/缺口下沿/密集区/涨停起点/hi20）/
            **seal/seal_tier/seal_pct**/**hi_today**（当日最高，条件单突破基准）/
            vol_ratio/**ret5_pct/zt10/zt_streak**/dist_ma5_pct/dist_ma20_pct/
            net5d_wan/net20d_wan/streak_dir（20 日位置按 (price-lo20)÷(hi20-lo20) 自算）；
            **vr_rel_st/flow_rel_st**（分时同刻口径，**仅盘中给**：截至此刻的量比÷大盘同刻量比、
            净占比−大盘同刻净占比；收盘后同刻≡全天，改用 vr_rel/flow_rel）；
            limit_up/buyable/stale_bar/risks/seal/zt_streak/confidence **缺键即正常**
            （未涨停/可买/未滞后/无风险/未封板/没连板/证据满维）——
            **比强弱看这张表，它不截断，也不含明细**；
            analyzed[]=满卡，按交付预算截断（analyzed_note 写明给了几只），
            两档**同序，按位置透支升序**（距20日均线近 / 20日区间低位 / 近5日涨幅小者在前；
            **p_up 不参与排序**，只作字段给值），故满卡 = 位置最干净的 N 只，不是「最强 N 只」；
            每张 {code,name,patterns,tech,flow,**intraday**,mtf,score,direction,confidence,
            signal,missing,**seal**,stale_bar,rt_risks,flow_risks}；
            intraday={t 截断时刻,day 目标日,vr_same_t 个股同刻量比,vr_rel_st,fp_same_t 个股
            同刻净占比%,flow_rel_st,ret_same_t 昨收→t 涨幅%,idx_ret_same_t 上证同期涨跌%,
            rs_same_t 超额,mkt_vr/mkt_fp_pct 大盘同刻基准,mkt_fp_prev_pct 前5日同刻均值}；
            rt{price,change_pct,previous_close,high,low,limit_up,limit_up_price,
            limit_down_price,limit_band,time}；排在满卡之外的票用 inspect_stock(code) 定点取。
            seal = {tag(none|one_word|open0|open1_2|open3_5|open6p|unknown)/
            seal_tier(thin|mid|thick|vthick)/open_count/seal_ratio_pct(封单÷成交额 %)/
            zt_days/turnover_pct}
            ——封板结构的**客观事实 + 硬标签**，不打分、不排序：tag 只切开板次数、
            seal_tier 只切封单厚薄（两个正交事实，要合起来读）。不是可买性（盘后次日按
            涨停价挂单即可，买不到不是决策问题）。实证：开板越多次日越差、封单越厚越强。
            标签边界见 QD_SCREENER_SEAL_TAG。
            strategy / missing / errors。confidence 是**证据完整度**（拿到几维 / 共几维），
            不是模型对自己判断的把握。
            flow = {ok, net_wan, caliber=price_direction, caliber_note, net_pct_of_amount,
            recent{近5/20日累计/连续方向/逐日符号/
            剔除最大单日/上一交易日}, intraday{开盘窗 vs 其后净额}}——资金看多日总体，
            单日大额流入 ≠ 趋势（口径=量价方向净额，非真实主力）。
            flow_risks = 拉高出货线索（开盘脉冲后转流出/冲高回落/单日脉冲背离，经验型，可否决）。
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

    # 分时校准只在盘中做：收盘后「同刻」≡全天，日线口径的 vr_rel/flow_rel 已经是
    # 最优解，再叠一份 snapshot 版只会给出两个不同的 flow_rel（实测最多差 17pp，
    # 因 snapshot 60s 采样与 1m bar 的方向口径不同），LLM 无从取舍。
    from ._helpers import _now_hhmm, same_time_batch, intraday_shape_batch
    st_on = _now_hhmm() != "15:00"
    st_map: dict = {}
    if st_on:
        # 一次 `symbol = ANY(...)` 取全部票的同刻序列：逐票查会打满连接池（实测丢
        # 9/45 只票），批量后与票数几乎无关（~60ms + 一次大盘基准 TTL）。
        try:
            st_map = same_time_batch(code_list)
        except Exception as e:
            missing.append(f"分时校准: {type(e).__name__}: {e}")
    # 分时形态：盘中/盘后都给（盘前取最近一个完整交易日）。它是**独立维度**，
    # 只用于与日线 regime 相互印证与精细化，不覆盖、不改写日线判断。
    shape_map: dict = {}
    try:
        shape_map = intraday_shape_batch(code_list, names=name_map)
    except Exception as e:
        missing.append(f"分时形态: {type(e).__name__}: {e}")

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

        # 分时（同刻）大盘校准：目标日 T 前 vs 前 5 个交易日同一时刻，个股与大盘同源
        # （见 same_time_batch）。日线口径的 vr_rel/flow_rel 在盘中等于拿昨天收盘的
        # 全天数据校今天的盘中，越早越失真，故 deep 另给一份分时口径。
        row["intraday"] = st_map.get(code) if st_on else None
        if row["intraday"]:
            row["_evidence"]["intraday"] = True
        # 分时形态（与日线 regime 并行，供相互印证）
        row["shape"] = shape_map.get(code)

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
    from ._helpers import _FETCH_TIMEOUT_S
    raw: list = []
    done: set = set()
    futs: dict = {}
    # C7（假超时）：这里**不能**用 `with ThreadPoolExecutor(...)` —— 退出 __exit__
    # 等价于 shutdown(wait=True)，会一直阻塞到挂死线程结束，于是 as_completed 的
    # timeout 形同虚设（外部源挂起时调用线程照样被钉死，_FETCH_TIMEOUT_S 失效）。
    # 改显式 shutdown(wait=False, cancel_futures=True)：超时立即返回，未开始的任务
    # 取消，仍在跑的线程不再等待（挂起源由其自身超时收尾）。
    ex = ThreadPoolExecutor(max_workers=min(_DEEP_WORKERS, len(items)))
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
    if st_on:
        _no_st = sum(1 for r in raw if not r.get("intraday"))
        if _no_st:
            missing.append(f"{_no_st} 只无分时校准（停牌/新股/当日无 tick，按全天口径读）")

    order = {c: k for k, c in enumerate(code_list)}
    raw.sort(key=lambda r: order.get(str(r.get("code")), 999))

    # 实时覆盖层（2026-10-14）：本地日线盘后可能未刷当日 bar，tech 的涨跌停/收盘
    # 会与真值打架（实测 600408 bar +4.53% 非涨停 vs 实时 +10.03% 封板）。历史指标
    # 仍用 bar，可执行性字段以 rt 为准；缺 rt 不阻断。
    from ._helpers import fetch_rt_map, apply_rt_overlay
    rt_map = fetch_rt_map(code_list)
    if not rt_map:
        missing.append("实时行情缺失（tech 为日线口径，涨跌停/收盘可能滞后）")
    from ._helpers import seal_summary, pump_dump_risk, zt_index
    zt_map = zt_index()          # 封板票开板次数/封单额来源；空则按「未知」降级
    _sealed_need_zt = 0
    for r in raw:
        _rt = rt_map.get(str(r.get("code") or ""))
        apply_rt_overlay(r, _rt, code=str(r.get("code") or ""), name=str(r.get("name") or ""))
        if _rt is None and r.get("tech"):
            r.setdefault("missing", []).append("实时行情缺失")
        # 封板结构（2026-10-30）：只给事实与硬标签，不打分——「买不买得到」在盘后
        # 不是决策问题（次日按涨停价挂单即可），故不做可买性维度。
        _sealed = (r.get("rt") or {}).get("limit_up")
        _zt_row = zt_map.get(str(r.get("code") or ""))
        if _sealed and _zt_row is None:
            _sealed_need_zt += 1
        r["seal"] = seal_summary(
            sealed=_sealed,
            zt_streak=(r.get("tech") or {}).get("zt_streak") or 0,
            zt_row=_zt_row,
            rt=r.get("rt"))
        # 拉高出货线索（2026-10-30 缺陷②）：rt 到手后判冲高回落才准，单独成键
        # 不混进 rt_risks（那是可执行性风险），由 LLM 自行定夺。
        try:
            r["flow_risks"] = pump_dump_risk(r.get("flow") or {},
                                            tech=r.get("tech"), rt=r.get("rt"))
        except Exception as e:
            r["flow_risks"] = []
            missing.append(f"拉高出货识别:{type(e).__name__}: {e}")
    if _sealed_need_zt:
        missing.append(f"涨停池缺 {_sealed_need_zt} 只封板票（封板结构只有标签，开板/封单未知）")

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
    analyzed = sorted(analyzed, key=_delivery_key)

    # 2026-10-30 双档交付：analyzed_rank 覆盖全部票（横向可比），满卡按预算截断
    rank = [_rank_row(a) for a in analyzed]
    analyzed = [_slim_card(a) for a in analyzed]
    head = {
        # report_guide 放最前：skill_run 预览= json.dumps，键序即阅读序
        "report_guide": _REPORT_GUIDE,
        "score": report.score,
        "direction": report.direction,
        "confidence": report.confidence,
        "signal": report.signal,
        "strategy": strategy,
        "confidence_note": "confidence = 证据完整度（tech/patterns/flow/prediction 拿到几维），非模型自信度",
        "errors": errors,
        "missing": missing,
    }
    _b = _deep_budget() if budget is None else max(0, int(budget))

    def _pack(n: int) -> dict:
        h = dict(head)
        if n < len(analyzed):
            h["analyzed_note"] = (
                f"analyzed 只含前 {n} 只满卡（按交付预算裁剪），全部 "
                f"{len(analyzed)} 只的横向可比字段在 analyzed_rank；需要某只的满卡用 "
                f"inspect_stock(code)")
        return {**h, "analyzed_rank": rank, "analyzed": analyzed[:n]}

    keep_n = _full_card_quota(head, analyzed, rank, _b)
    while keep_n > 0 and _dumps_len(_pack(keep_n)) > _b:
        keep_n -= 1
    return SkillResult(_pack(keep_n))


def inspect_stock(code: str, deep: bool = False) -> dict:
    """单票定点深挖：把一只票的相关证据一次拉齐，用于交叉验证或推翻排序结论。

    默认拉用量价/位置/资金/题材/涨停史/模型分；deep=True 追加龙虎榜席位与板块排名（更慢）。

    参数:
        code: 6 位股票代码
        deep: 是否追加慢速源（龙虎榜席位、板块排名），默认 False

    返回:
        dict: code/name/strategy；quote=实时快照；tech=量价位置；
            flow=资金流（net_wan 为量价方向净额）；fund_hist=近 10 日逐日净额；theme=行业与概念标签；
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

    def _same_t():
        from ._helpers import same_time_stats
        return same_time_stats(code)

    tasks = [_quote, _bars, _flow, _theme, _zt, _same_t]
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
    st = results.get("same_t")
    out["same_t"] = st if isinstance(st, dict) and st.get("ok") else None
    if isinstance(st, dict) and not st.get("ok"):
        missing.append(f"同时刻比对: {st.get('reason')}")
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
    if isinstance(out.get("flow"), dict) and out["flow"].get("ok") and (out["flow"].get("net_wan") or 0) < 0:
        risks.append(f"当日量价净流出{abs(out['flow']['net_wan'])}万（派生口径，非主力）")
    # 多日资金总体（2026-10-30 缺陷①）：连续净流出比单日净流出更值得警惕
    _rec = (out.get("flow") or {}).get("recent") or {}
    if _rec.get("ok") and (_rec.get("streak_dir") == "outflow") and (_rec.get("streak_days") or 0) >= 3:
        risks.append(f"资金连续{_rec['streak_days']}日净流出（趋势未转向）")
    try:
        from ._helpers import pump_dump_risk
        risks.extend(pump_dump_risk(out.get("flow") or {}, tech=tech))
    except Exception:
        pass
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
    """一次性跑完整流程——单链路保底选择 → deep 分析。

    流程：
      ① **保底候选**：整个原始池画像后按 hard 过滤，再按「连板高度降序 → 当日收盘位置
         更低者先」排序取 ≤25 只（2026-11-03 去掉窗口 60 与 p_up 闸，见
         _baseline_selection），**恒入 deep**；
      ② **存活 0 只 → 立即退出**，如实告知用户无合适候选（2026-11-02 移除 Round1
         自动放宽与双回捞；2026-11-03 清除 objective_preexclude + score_pool 加法层）；
      ③ deep_analyze(基线，≤_FUNNEL_DEEP_N=45)。LLM 仍单次调用。
         交付按位置透支升序（满卡=位置最干净的 N 只；p_up 不参与排序），名单成员与顺序解耦：
         排序只影响「先看谁」，不影响「有谁」。

    可选参数（与 pre_screen 同义；全部可省）：
        sources: 候选来源，如 "zt,dragon,search"；省略用时段默认
        queries: 条件搜索词
        min_turnover: 换手率下限；省略=不过滤
        limit: 候选截断；省略=不截断
    **不要传** strategy / min_market_cap / with_mtf 等不存在的参数。
    """
    _t0 = time.perf_counter()
    _ms: dict = {}
    # ── 运行期预热（2026-10-25，探针定位的两大单点；均失败不阻塞主流程）──
    # P1-①：指数 K 线后台预热——mootdx 冷启 bestip 扫描 ~74s 与候选池取数
    # （candidate_pool ~69s）无依赖，后台重叠后该 78s 单点基本隐藏
    prewarm_market_klines()
    # P0：CNStock 连接池预热到画像并发度——消除 psycopg2 池 minconn=2 的
    # 连接抖动（8 线程每波 6 建 6 关，单次 read_db 0.30s vs PG 侧 0.386ms）
    warm_market_pool()

    funnel = {
        "rounds": 0, "pool_raw": 0,
        "baseline": 0, "baseline_hard": 0, "baseline_risk": 0,
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
    funnel["rounds"] = 1

    _t = time.perf_counter()
    prescreen_result = pre_screen(
        sources=sources,
        queries=",".join(base_queries) if base_queries else None,
        search_top_n=_FUNNEL_SEARCH_TOP_N,
        min_turnover=min_turnover, limit=limit,
    )
    _ms["pre_screen"] = _ms.get("pre_screen", 0) + _elapsed(_t)
    if prescreen_result.get("error"):
        _out = {"report_guide": _REPORT_GUIDE,
                "funnel": funnel, **prescreen_result}
        _log_run(_t0, _ms, funnel, _out)
        return SkillResult(_out)

    candidates = list(prescreen_result.get("candidates") or [])
    missing = list(prescreen_result.get("missing") or [])
    pool_missing = missing
    search_failed = [m for m in missing if "条件搜索" in str(m)]
    funnel["pool_raw"] = len(candidates)
    # 搜索源可用性：按基础查询词评估（不再追加兜底词放大分母，避免把「搜索源挂了」误判成可用）
    funnel["eastmoney_ok"] = len(search_failed) < max(1, len(base_queries or [""]))

    _t = time.perf_counter()
    baseline, baseline_hard = _baseline_selection(candidates, prescreen_result)
    _ms["profile"] = _ms.get("profile", 0) + _elapsed(_t)
    funnel["baseline"] = len(baseline)
    funnel["baseline_hard"] = baseline_hard
    from ._helpers import _has_stretched_streak_risk
    funnel["baseline_risk"] = sum(
        1 for p in baseline if _has_stretched_streak_risk(p))
    selected_all = baseline

    codes = [p["code"] for p in selected_all[:_FUNNEL_DEEP_N]]
    funnel["deep_n"] = len(codes)

    # 存活 0 只 → 立即退出并告知用户无合适候选（2026-11-02：移除 Round1 自动放宽，
    # 不再为凑数降门槛；保底轨为空意味着连 8.1.0 基线口径都选不出，直接如实汇报）。
    if not selected_all:
        # 空结果必须「可汇报」（沿用 2026-10-12 契约，funnel 补充漏斗口径）。
        # 原先只回 {analyzed: [], signal: "…"} ⇒ 模型无可写口径 ⇒ 转主对话重跑一遍
        # 同样逻辑（D-18）。这里把「筛了多少 / 因什么淘汰 / 有什么数据缺口」一并给出。
        # 源故障显性化（2026-10-21）：pool_missing 含候选源失败/日历标注时，
        # 必须写进 hard_reasons 并前置到 missing —— 防止把「数据不可用」交付成
        # 「今日无候选」（pool_raw=0 + missing 为空的纯真空池只可能出现在
        # 交易日 + 全源成功，否则 candidate_pool 已标注）。
        src_fail_n = sum(1 for m in pool_missing
                         if any(mark in str(m) for mark in _SRC_FAIL_MARKS))
        hard_reasons = []
        # 基线硬障碍（2026-10-25；2026-10-30 口径改非封板）：空结果现在
        # 只剩一种成因——保底轨被非封板 hard 拦空（加法层已于 2026-11-03 移除）。
        if funnel["baseline_hard"]:
            hard_reasons.append(
                f"8.1.0基线窗口硬障碍:{funnel['baseline_hard']}只"
                f"（非封板类；画像范围内）")
        if src_fail_n:
            hard_reasons.append(
                f"候选源失败{src_fail_n}条（数据不可用≠真实空候选，明细见missing）")
        _out = {
            "report_guide": _REPORT_GUIDE,
            "strategy": meta.get("strategy") or strategy,
            "market": meta.get("market") or prescreen_result.get("market") or {},
            "hints": meta.get("hints") or {},
            "analyzed": [],
            "signal": "无符合软硬门槛的候选",
            "pool_size": funnel["pool_raw"],
            "hard_blocked": funnel["baseline_hard"],
            "hard_reasons": hard_reasons,
            "missing": (list(pool_missing) + list(meta.get("missing") or []))[:6],
            "errors": list(meta.get("errors") or [])[:3],
            "mood_regime": meta.get("mood_regime"),
            "themes": list(meta.get("themes") or [])[:5],
            "funnel": funnel,
        }
        _log_run(_t0, _ms, funnel, _out)
        return SkillResult(_out)

    # 外层信封（market/hints/funnel）不进 deep_analyze 的预算账，但会并进最终交付：
    # 先按实际体量扣掉再传预算，`_DEEP_BUDGET_CHARS` 才真是「整份结果」的上限而不是
    # 「deep_analyze 自己那一段」的上限（旧口径下 28K+外层 ≈ 29.9K，离 30K 闸只剩百来字符）。
    outer = {"market": meta.get("market") or prescreen_result.get("market") or {},
             "hints": meta.get("hints") or {},
             "funnel": funnel}
    _t = time.perf_counter()
    result = deep_analyze(",".join(codes), limit=_FUNNEL_DEEP_N,
                          budget=_deep_budget() - _dumps_len(outer))
    _ms["deep"] = _ms.get("deep", 0) + _elapsed(_t)
    # funnel 紧跟 report_guide（SkillResult 渲染分流按键存在性判断，键序不影响渲染）
    if isinstance(result, dict):
        out = {"report_guide": result.get("report_guide", _REPORT_GUIDE),
               # market/hints 必须随结果交付（2026-10-30 缺陷③）：旧版只在空结果分支
               # 带 mood_regime/themes，常规路径的早盘预判/下跌收紧信息根本到不了
               # 写结论的 LLM —— 与 report_guide「market 已由 run_skill 覆盖」自相矛盾。
               **outer}
        out.update({k: v for k, v in result.items() if k != "report_guide"})
        _log_run(_t0, _ms, funnel, out)
        return SkillResult(out)
    return result
