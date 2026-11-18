
"""stock-analyzer/_deep.py —— 个股分析地基：deep 层（自 market-screener 完整复制）。

【定位】本文件是单股分析的**证据装配层**：把一只票的全部可用维度（日线技术面、K 线形态、
资金流、分时同刻校准、分时形态、多周期结构）取齐，压成「rank 行 + 满卡」两档，
再按已标定的 P(次日涨) 排序交付。**不含主观评分梯形**，唯一的数字是 predict_one 的 p_up。

【复制来源与保真】逐字复制 `skills/market-screener/run.py` 的 deep 层：
`_REPORT_GUIDE` / `_DEEP_*` 常量 / `_deep_budget` / `_rank_row` / `_dumps_len` /
`_slim_card` / `_full_card_quota`（已改为分层配卡，见下）/ `deep_analyze`。机械改动：
  1) `from ._helpers import …` / `from ._facts import …` / `from .common import …`
     统一改指 `._deps`（依赖桥，见该模块 docstring）；
  2) 删除行内注释（本包铁律：app/agent 下不写注释），docstring 全部保留。
逻辑、字段、阈值、预算链均未改动；market-screener 侧一行未动。

【本副本的功能性差异（2 处，均不影响字段契约）】
1) 分层配卡（2026-11-03）：原 `_full_card_quota` 只按「整张标准卡装得下就装」放卡，
装不下即停。本副本改为 `_compact_card` + `_card_plan`：先给前 3 张标准卡，余量用
**精简卡**（剔除 `tech.support.supports/resists`、`flow.recent`、`shape` 明细，这三类在
rank 行或工具里可复得）继续装，并取「3 标准 + 精简」与「全精简」两种排法中张数更多者。
实测（20 只票、默认预算）：满卡 9 张 → 11 张（前 3 张标准 + 其后 8 张精简）；
单卡 2.7k → 2.0k 字符。全精简可到 13 张，但会牺牲头部票明细，故不采用。
2) 日线暂存 `_BARS_STASH`（2026-11-03）：`_one` 取到的日线在进程内暂存 60s，
`take_bars(code)` 取回——`analyze()` 需要同一份日线做价位推演，不暂存就要对同一票
拉两遍日线。暂存不进返回值、不占预算、不影响任何字段。

【关键点】
  · 预算链：`_deep_budget()` < observation 闸 45000 < 预览闸 47000。超预算时 `_full_card_quota`
    按实测字符回退减卡（不是拍脑袋砍），保证 rank 行全量、满卡只给装得下的。
  · 排序：`_rank_row` 只搬已有值不做计算；两档同序（p_up 降序、无预测沉底），故满卡即最强 N 只。
  · 并发：显式 `shutdown(wait=False, cancel_futures=True)`，禁用 `with ThreadPoolExecutor`
    （退出即 wait=True，会把超时保护架空）。

【易错点】
  · 漏票必须留痕：票据未返回时写 `errors`/`missing`，不得静默少给（下游按数量核对）。
  · `_slim_card` 从满卡里剔除的是**可再取**字段（明细），不是判断依据；裁剪口径改动会影响
    "满卡即最强 N 只"能否成立。
  · 与 market-screener 的同名函数是**两份独立副本**：改这里不会改那边，反之亦然。
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ._deps import (SkillResult, fetch_kline, select_strategy, resolve_names,
                    tech_summary, build_report, cross_verdict,
                    _detect_patterns, _market_state, _multitimeframe)

#: 日线暂存（code → {t, bars}）：deep 取到的 bars 留 60s 供 analyze 复用，消除重复拉取
_BARS_STASH_TTL_S = 60.0
_BARS_STASH: dict = {}
_BARS_STASH_LOCK = threading.Lock()


def take_bars(code: str):
    """取回本进程最近一次 deep 取到的日线（60s TTL）；无暂存/过期返回 None。"""
    with _BARS_STASH_LOCK:
        one = _BARS_STASH.get(str(code or "").strip())
        if one and time.time() - one["t"] < _BARS_STASH_TTL_S:
            return one["bars"]
    return None


def _stash_bars(code: str, bars) -> None:
    with _BARS_STASH_LOCK:
        _BARS_STASH[str(code or "").strip()] = {"t": time.time(), "bars": bars}

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
        "★ **全文用中文白话**，禁止输出 p_up / t1_bias / "
        "near_support / rank_cut 等字段名："
        "p_up 写「次日上涨参考」，"
        "无标定分写「暂无历史参考分」。\n"
        "## 不买/观察（每只一行：代码+拒因≤20字）\n"
        "## 风险（≤3 行）\n"
        "指标罗列、漏斗数字不进正文。保留代码+看多看空+跌破 X（喂 claims）。"
    ),
    "STEPS": "run_skill 一次即可交付；起手不要 get_hot_sectors / get_market_overview（market 已在本结果）；"
             "禁止 search_tools / read_skill / get_dragon_tiger / 二次 run_skill / 逐只 quote；"
             "**禁止 agent_get_kline / get_index_kline**——analyzed_rank 已给 ma5/ma10/ma20/hi20/lo20 "
             "绝对值，买点/止损/压力据此直接算，不需要原始 K 线；"
             "funnel.baseline=保底候选只数（恒入 deep，风险软排序后先于加法层）；"
             "funnel.baseline_risk=保底候选中高连板且远离 MA5 的风险提示数；"
             "funnel.excluded_score 只统计加法层淘汰（p_up 硬闸/排名截断），不是全候选集",
    "buyable": "**只在盘中此刻（strategy=intraday）生效**：rt.limit_up=true 或 rt.limit_down=true → 只进排除表，禁止进可买表；"
               "盘后/尾盘一律放行——封板/跌停是事实不是排除令，可进「买什么」但要写明「当日已封板」"
               "与次日买点（一字板风险：需等开板或回踩支撑，不追集合竞价）；接不接由你的逻辑决定，须给理由；"
               "盘中涨停由选人层处理（topdown 漏斗的涨停分支与连板放量闸），rt.buyable=false 是 deep 阶段的最终防线；"
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
                  "① 先定姿态（读 market 的两档情绪）：大盘档 market_mood 偏强 + fund_flow_days 净流入 → "
                  "可放宽，允许给突破/追价买点、容忍较高乖离、推荐只数可多；cautious=true 或 "
                  "market_mood 偏弱 → 收紧，只给回踩买点、买点压到更深的支撑、宁可少给；"
                  "中性环境按个股自身结构定。个股档 stock_mood 衡量当日赚钱效应，它偏弱时即使大盘强"
                  "也不追高。姿态是**每次按环境算**的，不是固定偏保守。\n"
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
                     "**这一维度排序参考价值高于 p_up 与量比**，优先看它。rank 已给合成档 **loc**（hot=透支 / cool=位置干净 / normal），见 location 条。"
                     "加分项不足 2 条的进「不买/观察」并注明缺哪条；"
                     "**数量不是考核项**：弱市给 1 只也合规，凑数才是错的；环境转强可多给，但要写明成立的是哪几条。",
    "stance_rule": "禁止「一律回踩均线」，也禁止**把 rank 的 sup.px 直接当买点**——sup 只是「现价下方"
                   "最近的位置」，照抄的结果是三只票买点全在现价下方、当天一只也买不到。"
                   "若所有票的买点都等于 sup.px 或都写成 MA5/MA10，说明没读环境、在用模板。"
                   "强环境（market_mood 偏强 + fund_flow_days 净流入）该给的是「突破 res 追」或「不破 sup 持有」；"
                   "弱环境才把买点压到 sup 下方一档，并且只留能当日成交的。"
                   "**买点形态要跟着强度走**：强度加分项多的给「突破 hi_today 追」，加分项少的只给回踩；"
                   "全名单买点长得一模一样（全回踩或全突破）同样是没排序。",
    "horizon": "表内只写 T+1/T+3/T+5（词表：看多/中性/看空）；T+10/T+20 不写。"
               "zt_streak>=3 只写 T+1。p_up 仅 T+1，禁止外推胜率",
    "priority": "支撑与弱转强/强转弱 ≫ 涨跌幅；禁止按涨跌幅排序",
    "support_rule": "盘中红绿不决定安全；可怕的是无支撑阴跌。-8%弱转强+有支撑 可以比 +8%转分歧更安全",
    "fields": "analyzed_rank = **全名单**一行一只（不截断）：p_up/direction/confidence/price/chg_pct/"
              "chg_prev_pct（昨日涨跌%）/cpos（收盘位 0~100=贴日高）/"
              "ma5/ma10/ma20/hi20/lo20（价位绝对值）/**hi_today**（当日最高=次日突破基准；缺键即等于 hi20）"
              "/**sup**/**res**={k 位置名, px 价位, d 距离%}"
              "（k∈ma5|ma10|ma20|ma60|lo10|lo20|lo60|hi20|gap 缺口下沿|poc 密集区|zt 涨停起点，"
              "已按现价分侧：sup=**现价下方最近**的关键位置、res=**现价上方最近**的关键位置）"
              "/**强度四件套** ret5_pct（前5日涨幅%）/zt10（近10日涨停次数）/zt_streak（当前连板数）"
              "/vol_ratio（今日量比）/**vr_rel**（**全天口径** 量比÷大盘量比；**重要评估条件、非准入闸**："
              "≥2=放量强于大盘的确认项，<2 不否决、须结合资金与支撑判读；"
              "绝对量比≥2 但 vr_rel<2=跟风放量（大盘同放）；<1=放量不及大盘）"
              "/**vr_rel_st**（**分时口径，仅盘中给**：截至 intraday.t 的量比÷大盘同刻量比；"
              "盘中量能确认项用它）/**flow_rel**（全天：净占比−大盘净占比，百分点；资金为首要评估维度，见 fund_trend）"
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
              "主论据= 强度四件套 / MA 排列 / flow / p_up；rt 看现价/涨跌停",
    "board_cut": "zt_streak>=3 默认只写 T+1",
    "words": "方向用 看多/中性/看空/回避；用户问持仓才写「兑现」",
    "market": "market_mood/stock_mood/资金/热点已由 run_skill 覆盖；不要再 get_market_overview / get_hot_sectors；"
              "**环境决定姿态，两个方向都要用**：cautious=true 或 market_mood 偏弱 → 提高门槛、减少只数、"
              "只做有支撑的票、买点压深一档；market_mood 偏强 + fund_flow_days 净流入 → 放宽（可给突破买点、"
              "容忍较高乖离、只数可多）。不要只取「收紧」这一半。"
              "两档情绪不可互相替代：大盘档决定姿态与只数，个股档决定敢不敢追高；"
              "两者背离（大盘强、个股难做）时按收紧执行。"
              "大盘本身最多进「风险」一行轻注，不得当筛选主条件",
    "fund_trend": "资金流入流出是核心证据，且优先于量能条件；量能与资金冲突时以资金为准，量比<2 不构成否决；"
                  "资金持续净流出或脉冲后回吐是降权/否决依据，资金流入是确认项、不是独立买入信号。"
                  "资金看多日总体：flow.recent（近5/20日累计、连续方向、逐日符号、剔除最大单日）≫ 当日 net_wan；"
                  "单日大额流入不等于趋势，连续3日以上同向才算趋势；剔除最大单日后仍为正才是普遍承接。"
                  "★**口径**：net_wan / net5d_wan / net20d_wan 都是 `caliber=price_direction` 的"
                  "**量价方向净额**（按分钟涨跌方向累计成交额派生），**不是东财四档主力净额**，"
                  "两者量级不同、不可换算 ⇒ 正文只能写「量价净流入/资金净额/净流入 X 亿」，"
                  "**禁止写「主力净流入」「主力资金」「主力承接」**，也不得拿它推算主力持仓或控盘度",
    "fund_micro": "资金微观结构 = fund_trend 的细化判读，**全是确认项，非准入闸、非独立买入信号，"
                  "不设绝对阈值、不互相覆盖、不作加分、不单独判强**："
                  "① **时间窗（只认开盘后分段）**：仅 10:00 之后分段净额转正/净占比放大"
                  "（after_net_wan/points）可作偏强确认，独立回测四段方向一致为正但逐日配对不显著"
                  "（t=1.05）⇒ 只作确认、不加分；开盘窗（09:30~10:00，intraday.open_net_wan）"
                  "净流入不作偏强解读——近两段翻负、样本外 −0.494，须与「开盘脉冲后撤」风险线索合读。"
                  "② **位置×资金（须净额为正）**：资金承接落在 sup.px 最近关键位附近"
                  "（k 决定类型，不写死 MA5、不写死「站上/回踩」）是独立确认项——"
                  "同样资金为正时贴近明显优于远离（+0.192 vs +0.002，控制涨幅后 +0.149 vs −0.126），"
                  "弱资金档内增量更大；位置不替代资金方向判断，不因靠近关键位上调买入结论，"
                  "10~11 月段曾翻负 ⇒ 只作确认、不加分。"
                  "③ **叠加边界**：仅上述两项，不作命中计数、不加权、禁止写「命中越多越强」；"
                  "时间外仅 5 天有完整 T+3，不作长期效应估计。",
    "pump_dump": "flow_risks/warnings 含「开盘脉冲」「疑似拉高出货」「趋势背离」→ 拉高出货嫌疑，"
                 "除非有强反证（如尾盘重新放量收回），否则降权或不买",
    "funnel": "漏斗数字只允许出现在末行「数据」；禁止写进正文",
    "no_pred": "无标定分票写「暂无历史参考分」（不是坏票）；禁止写 p_up 等内部字段名",
    "regime": "regime=位置标签（缺键=低位蓄势，中性不标注）。**只报位置与跨日方向，不判盘中动作**"
              "（一鼓作气/再而衰/三而竭属分时概念，日线不评，可买否看盘中形态）："
              "**高位**(前5日>10% 且当日涨>2%，或跑输大盘大跌——只报位置)/**高位分歧**(高位横盘)"
              "/**跳水回踩**(高位大跌但 rs_d0≥-1.5=跌主要来自大盘带崩、非个股弱；关键位企稳即可按回踩接，当日反抽不追)/**弱转强**(低位昨跌今涨，真伪看量比与资金："
              "量比≥2 是重要评估条件、不是准入闸，可给追价；<2 优先给回踩支撑并写明资金与位置理由，"
              "量比本身不否决；vr_rel≥2=放量强于大盘的确认项"
              "（绝对量比<2 但 vr_rel≥2=大盘缩量它放量，可上调置信）；"
              "vr_rel<2 且资金持续流出/脉冲后回吐时才降权（含跟风放量=绝对量比≥2 但 vr_rel<2）；"
              "★ 独立回测：≥2 组 T+3 超额系统性为负、峰值在 1.5~2.0，故 ≥2 不作加分硬门；"
              "同一量比档内资金强组明显优于资金弱组，冲突时以资金为准；"
              "加分（不写死）：再看价效高（涨幅÷量比高=价升量稳/惜售）可上调置信，单看惜售不加分"
              "/低位启动(低位今涨，昨未跌，看支撑)/低位走弱（按普通档读、量能只作评估条件）。"
              "rs_d0/rs_5d=个股−上证同期超额(%)（参考数据，rs_d0≥-1.5 拆分见跳水回踩），"
              "vr_rel/flow_rel=量能/资金流对大盘校准（参考数据）。"
              "**大盘暴跌、个股横盘（rs_d0 明显为正）=有支撑**（跳水回踩拆分即此义，"
              "带崩≠个股弱）。"
              "庄股例外：与大盘恒反向、量能自锁（放量缩量不随大盘）的票按独立行情读。"
              "盘中用 intraday 的 vr_rel_st/flow_rel_st（同刻口径，比全天直接）。"
              "**买前量能确认项（重要评估条件、非准入闸）**：有 intraday 块（盘中）"
              "看 **vr_rel_st**，无 intraday（收盘后）看 **vr_rel**；≥2 为放量确认，"
              "<2 提示需资金与支撑补证、不得单独否决；"
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
_DEEP_WORKERS = 16
_DEEP_BUDGET_CHARS = 44500
_DEEP_BUDGET_ENV = "QD_SCREENER_DEEP_BUDGET"
def _deep_budget() -> int:
    """满卡预算（字符）：env 优先，非法/≤0 回落缺省。"""
    try:
        v = int(os.getenv(_DEEP_BUDGET_ENV, "") or "")
    except (TypeError, ValueError):
        return _DEEP_BUDGET_CHARS
    return v if v > 0 else _DEEP_BUDGET_CHARS


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
        "p_up": card.get("p_up"),
        "direction": card.get("direction"),
        "confidence": card.get("confidence"),
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



    _cv = (cross_verdict(tech.get("regime") or "低位蓄势", _sh) or {}).get("verdict")
    if _cv and _cv != "neutral":
        row["cross"] = _cv
    return row


def _dumps_len(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str))


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


def _compact_card(card: dict) -> dict:
    """精简卡：保留判断所需事实，剔除可从 rank 或工具复得的明细，用来换更多张数。

    剔除三类明细（都不影响「这只票强不强」的判断，且 rank 行/工具能补回）：
      · `tech.support.supports/resists`：关键位置清单，rank 的 `sup`/`res` 已给「最近一条」；
      · `flow.recent`：近 5/20 日逐日序列，rank 的 `net5d_wan`/`net20d_wan` 已给累计；
      · `shape`：分时形态明细，rank 的 `shape` 已给摘要标签。
    保留 `rt`（涨跌停价只在此处）、`intraday`（盘中同刻校准，rank 无）与 `tech` 其余字段。
    加 `card_tier="compact"` 让上层知道这张卡是精简档。
    """
    out = dict(card)
    tech = out.get("tech")
    if isinstance(tech, dict):
        tech = dict(tech)
        sup = tech.get("support")
        if isinstance(sup, dict):
            tech["support"] = {k: v for k, v in sup.items()
                               if k not in ("supports", "resists")}
        out["tech"] = tech
    flow = out.get("flow")
    if isinstance(flow, dict):
        out["flow"] = {k: v for k, v in flow.items() if k != "recent"}
    out.pop("shape", None)
    out["card_tier"] = "compact"
    return out


def _card_plan(head: dict, cards: list, compact: list, rank: list,
               budget: int, std_cap: int = 3) -> tuple:
    """在预算内安排满卡：返回 (标准卡张数, 精简卡张数)。

    策略是**保头部再扩量**：先无条件给前 `std_cap` 只放标准卡（这批是 p_up 最高的，
    最可能被上层拿去决策），余量全用精简卡往下铺。不做「全精简换更多张数」的取舍 ——
    实测 20 只票时全精简可到 13 张、保头部为 11 张，但前者会让头部票失去
    supports/resists、flow.recent、shape 明细，与「个股分析要看细节」相悖。
    预算连一张都装不下时返回 (0, 0)。
    """
    avail = budget - _dumps_len(head) - _dumps_len(rank)
    if avail <= 0 or not cards:
        return 0, 0

    s_sizes = [_dumps_len(c) for c in cards]
    c_sizes = [_dumps_len(c) for c in compact]
    used = 0
    n_std = 0
    for sz in s_sizes[:max(0, min(std_cap, len(cards)))]:
        if used + sz + 2 > avail:
            break
        used += sz + 2
        n_std += 1
    n_cmp = 0
    for sz in c_sizes[n_std:]:
        if used + sz + 2 > avail:
            break
        used += sz + 2
        n_cmp += 1
    return n_std, n_cmp


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
            两档**同序，均按 p_up 降序**（无预测的沉底），故满卡即最强 N 只；
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




    from ._deps import _now_hhmm, same_time_batch, intraday_shape_batch
    st_on = _now_hhmm() != "15:00"
    st_map: dict = {}
    if st_on:


        try:
            st_map = same_time_batch(code_list)
        except Exception as e:
            missing.append(f"分时校准: {type(e).__name__}: {e}")


    shape_map: dict = {}
    try:
        shape_map = intraday_shape_batch(code_list, names=name_map)
    except Exception as e:
        missing.append(f"分时形态: {type(e).__name__}: {e}")

    def _one(item: dict) -> dict:
        code = str(item.get("code") or "")
        name = item.get("name") or ""
        miss: list = list(item.get("missing") or [])

        row = {"code": code, "name": name, "missing": miss, "_evidence": {}}

        try:
            bars = fetch_kline(code, days=120)
        except Exception as e:
            miss.append(f"日线异常:{type(e).__name__}: {e}")
            bars = []
        _stash_bars(code, bars)
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
                from ._deps import flow_summary
                fs = flow_summary(code)
                row["flow"] = fs
                if not fs.get("ok"):
                    miss.append(f"资金流缺失:{fs.get('reason')}")
                row["_evidence"]["flow"] = bool(fs.get("ok"))
            except Exception as e:
                miss.append(f"资金流异常:{type(e).__name__}: {e}")
                row["_evidence"]["flow"] = False




        row["intraday"] = st_map.get(code) if st_on else None
        if row["intraday"]:
            row["_evidence"]["intraday"] = True

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


    from ._deps import _FETCH_TIMEOUT_S, attach_prediction
    raw: list = []
    done: set = set()
    futs: dict = {}





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
    except Exception as e:
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




    from ._deps import fetch_rt_map, apply_rt_overlay
    rt_map = fetch_rt_map(code_list)
    if not rt_map:
        missing.append("实时行情缺失（tech 为日线口径，涨跌停/收盘可能滞后）")
    from ._deps import seal_summary, pump_dump_risk, zt_index
    zt_map = zt_index()
    _sealed_need_zt = 0
    for r in raw:
        _rt = rt_map.get(str(r.get("code") or ""))
        apply_rt_overlay(r, _rt, code=str(r.get("code") or ""), name=str(r.get("name") or ""))
        if _rt is None and r.get("tech"):
            r.setdefault("missing", []).append("实时行情缺失")


        _sealed = (r.get("rt") or {}).get("limit_up")
        _zt_row = zt_map.get(str(r.get("code") or ""))
        if _sealed and _zt_row is None:
            _sealed_need_zt += 1
        r["seal"] = seal_summary(
            sealed=_sealed,
            zt_streak=(r.get("tech") or {}).get("zt_streak") or 0,
            zt_row=_zt_row,
            rt=r.get("rt"))


        try:
            r["flow_risks"] = pump_dump_risk(r.get("flow") or {},
                                            tech=r.get("tech"), rt=r.get("rt"))
        except Exception as e:
            r["flow_risks"] = []
            missing.append(f"拉高出货识别:{type(e).__name__}: {e}")
    if _sealed_need_zt:
        missing.append(f"涨停池缺 {_sealed_need_zt} 只封板票（封板结构只有标签，开板/封单未知）")

    raw = attach_prediction(raw, market=market)

    for r in raw:
        r.pop("_bars", None)
        r.pop("_pred", None)
        pred_ok = bool(r.get("has_pred"))
        r.pop("has_pred", None)

        ev = dict(r.pop("_evidence", {}) or {})
        ev["prediction"] = pred_ok

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



    analyzed = sorted(analyzed,
                      key=lambda a: (a.get("p_up") is not None, a.get("p_up") or 0),
                      reverse=True)


    rank = [_rank_row(a) for a in analyzed]
    analyzed = [_slim_card(a) for a in analyzed]
    compact = [_compact_card(a) for a in analyzed]
    head = {

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

    def _pack(n_std: int, n_cmp: int = 0) -> dict:
        h = dict(head)
        shown = n_std + n_cmp
        if shown < len(analyzed):
            tier = (f"；其中前 {n_std} 只完整、其后 {n_cmp} 只 card_tier=compact 精简档"
                    f"（已剔 tech.support.supports/resists、flow.recent、shape 明细）"
                    if n_cmp else "")
            h["analyzed_note"] = (
                f"analyzed 只含前 {shown} 只满卡（按交付预算裁剪{tier}），全部 "
                f"{len(analyzed)} 只的横向可比字段在 analyzed_rank；需要某只的满卡用 "
                f"inspect_stock(code)")
        cards = analyzed[:n_std] + compact[n_std:n_std + n_cmp]
        return {**h, "analyzed_rank": rank, "analyzed": cards}

    n_std, n_cmp = _card_plan(head, analyzed, compact, rank, _b)
    while (n_std + n_cmp) > 0 and _dumps_len(_pack(n_std, n_cmp)) > _b:
        if n_cmp > 0:
            n_cmp -= 1
        else:
            n_std -= 1
    return SkillResult(_pack(n_std, n_cmp))


