# -*- coding: utf-8 -*-
"""stock-analyzer/run.py — 单只 A 股 T+1~T+20 全周期多维诊断（执行型技能，deep 为基座）。

【架构】证据基座 = `_deep.deep_analyze`（market-screener deep 层的完整移植）：
analyze() 不自建取证链，deep 负责把单票全维证据（日线技术面 / K 线形态 / 资金 /
实时对账 / 封板结构 / 标定预测分）装配齐全，本文件只在其上做三层判定：

  ① 板块/概念涨跌幅修正（`sector`）：当日涨跌拆成 β（板块/概念）+ α（自身），
     跟板块的涨跌不作自身强弱证据——上涨系板块带动（α≤-1.5%）时「放量上攻」
     类条件被修正掉，下跌系板块带崩（α≥+1.5%）时「放量下跌」类条件被修正掉；
  ② 支撑/压力位多方法聚合与交叉验证（`_levels.build_levels`）：均线/枢轴/波段/
     区间/缺口/密集区/涨停起点/关键位引擎 8 类方法互相印证，每侧给 1-2 个价位并
     附推敲演算痕迹（聚合了几类方法、30 日触碰几次、最近守住没有）；
  ③ 全周期多维投票（T+1/T+3/T+5/T+10/T+20 × model/structure/volume/flow/market/
     sector/tape 七维）：方向必须 ≥2 维同向且与反向差值 ≥2 才成立。
     tape 维含事件证据（解禁供给压力，日历源 `get_lockup_expiry`）；
     触发条件的时间口径按 `session`（盘前/盘中/盘后/非交易日）区分，不拿盘中诊断当次日预案。
  ④ 三项补充事实（不改变七维投票、_DIMS 与 confidence）：`market_outlook` 把底层已标定
     的大盘次日涨概率从 `prediction.model` 提为独立输出；`cap_regime` 按「弱市跌多涨少
     ⇒ 大流通市值相对占优，强市涨多跌少 ⇒ 小盘相对占优」给出市值分层事实；`board_rank`
     用 realtime_snapshot 判定板块内排名、龙头/跟风；`trend_outlook` 只做长档日线趋势。
     三块逻辑全在本技能本地模块，与 market-screener 无引用关系。
  ⑤ 补证事实层（`_extra_facts`）：同批并发取筹码分布、资金节奏、情报面、融资与基本面、
     日内结构、多周期方向、估值快照、日频资金共八类；只陈述事实、不进七维投票；
     其中可核对的离场压力事实进 `stop_pressure`，与价格/资金/板块/大盘事实并列计数。
     日内与多周期取数目标日由 `current_data_day()` 钉死，工具默认的「今天」在非交易日会取错日。

【证据纪律（用户裁定 2026-11-03，不可放松）】
  · 不猜测、不妄想：结论只允许来自返回的事实字段，缺数就承认缺数；
  · 不知道的回答「未知」：关键数据缺失 / 日线不够长 / 该侧价位推不出 ⇒ 未知；
  · 证据不足或无法相互验证的回答「证据不足」：只有单一维度给方向、或多空维度
    并存差值 <2 ⇒ 不给方向，写证据不足并列明冲突点。
  方向词表：看多 / 看空 / 中性 / 证据不足 / 未知。
  语义边界：中性 = 多维均无方向且证据足（平衡是结论）；证据不足 ≠ 看空；
  未知 ≠ 利空。

【两个置信度字段的区别（勿合并）】
    evidence    证据完整度 = 拿到维度数 / 计入维度数（客观计数，与把握无关）
    confidence  该周期方向把握度 = 档位上限 × evidence × 一致度（|同向-反向|/有效维）。
                只有 T+1 的 p_up 是标定过的数字；T+3 及以后只给方向，不给概率。

【分工】deep_cards() = 批量证据地基（选股侧口径，见 `_deep.py`）；analyze() = 单票
结论生成器。两者共用 `_deep.py`，互不调用。

易错点：
  · `HOLDS` 里 T+5 与 1W 同为 5 天、T+20 与 1M 同为 21 天；本文件按档位名取天数，
    不按天数反查，避免撞上「先到先得」的规范名规则；
  · 维内条件为 None（该条缺数）时视为不成立方向、但计入 unknown 列表，
    缺数自然走向「证据不足 / 未知」，而不是伪造方向；
  · `_deep` 的满卡经 `_slim_card` 瘦身：空 patterns/missing 列表与 None 字段被剔除，
    判「维度拿到没有」要用 missing 留痕词（形态识别/资金流/预测/技术指标/日线）
    交叉核对，不能只看键在不在。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ...log import logger
from . import _board_rank, _cap_regime, _extra_facts, _inner, _levels, _mtf_guard, _trend
from ._inner import env_float as _env_float, env_int as _env_int

#: 全周期档位（顺序即输出顺序）；天数不在此硬编码，取 `label_spec.HOLDS`
_HORIZONS: Tuple[str, ...] = ("T+1", "T+3", "T+5", "T+10", "T+20")

_ANALYZE_REPORT_GUIDE = {
    "OUTPUT_FORM": (
        "先按当前数据时点用一句话给结论，再给 execution_core 四要素，最后是极简依据；不重列完整表格。"
        "四要素直接读 execution_core：买点/卖点是条件触发参考价（不是下单指令），"
        "hold_days 是预计持仓时间，risk_reward_ratio 是持仓风险收益比；任一为 NULL 就写未知并点明缺什么。"
        "horizons 只有 T+1/T+3/T+5 三档，每档最多一行「方向（置信度）｜触发｜证伪」，证据只取最关键 1-2 条；"
        "T+10/T+20 不逐档给触发/证伪，只按 trend_outlook 的日线趋势一句话陈述；"
        "market_outlook/cap_regime/board_rank/九块补证事实是经验收集用，正文最多各一句带过，不改变七维投票结论。"
        "不给命令式买卖或仓位指令；价位只作为条件情景，不保证买卖结果。"
    ),
    "CORE_FOUR": (
        "全自动场景只关心四要素，必须逐项给出：买点（放量站上何价才转多的触发参考）、"
        "卖点（收盘跌破何价的止损参考，偏空时附反抽压力减仓参考）、预计持仓时间（hold_days 天）、"
        "持仓风险收益比（risk_reward_ratio=(最近压力−现价)/(现价−最近止损)，价外记不适用）。"
        "四要素来自 levels/horizons 派生，是条件触发参考价、不是买卖指令；缺验证价位写未知不猜。"
    ),
    "LENGTH": (
        "结论全文控制在 {max_chars} 字以内（含四要素、三档与风险）；四要素各一行，"
        "三档各一行且每档证据最多 2 条，按 basis 顺序取最关键，不要逐维复述。"
        "正文只放结论与可执行条件，长篇解释留给结构化字段做经验收集。"
        "宁可短而可执行，不要长而面面俱到。"
    ),
    "FLOW_DRIVER": (
        "资金驱动只分析 20 日以内（flow_driver.sequence），按逐日「资金净额×涨幅」形态"
        "（推进/承接/惜售/共振）陈述，配合 intraday 分时支撑看短线节奏；"
        "20 日以上长线资金不进方向判断。资金净额是价格方向近似、非主力分类，"
        "四档拆单本地不可得，不得推算庄家持仓成本或利润空间。"
    ),
    "DIRECTION_WORDS": (
        "方向词只能用词表五选一：看多 / 看空 / 中性 / 证据不足 / 未知。"
        "禁止「可能上涨」「建议关注」「谨慎乐观」「或有压力」「大概率」等模糊措辞；"
        "证据不足就写证据不足并点明缺什么，不要用地表述替代方向。"
    ),
    "CONSISTENCY": (
        "若本轮对同一只票给出与上一轮不同的方向/止损位，必须显式写出"
        "「较上一轮由 X 改为 Y，依据 Z」，Z 只能是本轮新出现的可观测事实"
        "（价格破位、量能、资金方向、板块 α、均线位置）。没有新增事实时维持上一轮结论，"
        "不得仅因时点变化而翻转方向。"
    ),
    "STOP_CALIBER": (
        "horizons 的 invalidate 是最近一道**止损参考位**（verified 且距现价最近），"
        "last_defense 是更远、方法数更多的最后防线；两层都写清，不要把远位当成唯一防线。"
        "stop_pressure 只统计离场压力的**事实条数**（价格/资金/板块/大盘/筹码/情报/融资/日内结构的可核对事实），"
        "条数多时明确提示「核对止损参考位」，但它是事实汇总，不是买卖指令、不改方向投票。"
        "用户问去留/止损时，把参考位与压力事实前置；只问分析时照条件情景陈述，不下命令。"
    ),
    "EVIDENCE": (
        "外部事实注明来源和对应日期。flow 是价格方向净额近似，不是主力/机构资金分类；"
        "不得据此断言主力或机构买卖。龙虎榜席位/机构数据仅在 detail_date 与 records 上榜日期一致时引用；"
        "无记录、日期缺失/不匹配或存在 detail_error 时不作日期归因。"
    ),
    "EVENTS": "增持、回购等只陈述公告事实和可能影响，不断言导致股价变化或构成托底。",
    "TIME_CALIBER": (
        "区分实时/竞价快照与日线指标的时点和口径；未收盘或零成交 K 线不是收盘价，"
        "技术指标须注明所依据的最近完整交易日。"
    ),
    "SEARCH": (
        "只为关键缺失事实检索；同一事实最多检索一次，整份报告最多检索 2 次。"
        "优先一手来源；标的、日期或来源无法核实时，注明未知，不补猜、不重复查询。"
    ),
}

#: 各档置信度上限（T+1 有标定故最高；越远越不确定）。可被 env 覆盖
_CONF_CAP_DEFAULT: Dict[str, float] = {
    "T+1": 0.90, "T+3": 0.72, "T+5": 0.68, "T+10": 0.58, "T+20": 0.50,
}

#: 输出层强调的短档；长档（T+10/T+20）仍逐档计算但只由 trend_outlook 承载。
#: 分清「内部全档计算」与「输出强调档」，五档计算能力不因收敛而删除。
_SHORT_HORIZONS: Tuple[str, ...] = ("T+1", "T+3", "T+5")

#: 结论全文字符上限（只约束 LLM 写作，不改事实字段）。env 可覆盖。
_REPORT_MAX_CHARS_DEFAULT = 400

#: 并发取证总等待上限：deep 内部还有一层并发，这里给宽裕值防拖死整轮
_DEADLINE_S = 45.0

#: 参与证据完整度统计的维度；missing 留痕词用于交叉核对「拿到没有」
_DIMS: Tuple[str, ...] = ("rt", "tech", "prediction", "patterns", "flow", "sector", "zt")
_DIM_MISS_TOKEN: Dict[str, str] = {
    "rt": "实时行情", "tech": "技术指标", "prediction": "预测",
    "patterns": "形态识别", "flow": "资金流", "sector": "板块",
}

#: 七维投票与风险标记的阈值单源（2026-11-26 收敛：此前约 20 个魔法数字散在 _v_* 内）。
#: 改值 = 改判定纪律，须同步 SKILL.md 并回测复核；契约值（SKILL 明文）与经验阀都集中在此。
_VOTE_THRESHOLDS: Dict[str, Any] = {
    # model 维：T+1 标定分方向带
    "model_p_up_long": 0.55,
    "model_p_up_short": 0.45,
    # structure 维：短档（T+1/T+3）
    "sup_near_max_below_pct": -3.0,
    "rsi_overbought": 75.0,
    "break_ma20_pct": -3.0,
    "chase_dist_ma20_pct": 10.0,
    "sup_far_below_pct": -5.0,
    # structure 维：长档
    "t5_pos_up_min": 50.0,
    "t5_pos_dn_max": 40.0,
    "t5_dist_cap_pct": 10.0,
    "t10t20_pos_up_min": 60.0,
    "t10_pos_dn_max": 30.0,
    "t10_below_ma20_pct": -5.0,
    "t20_dist_min_pct": 0.0,
    "t20_dist_max_pct": 15.0,
    "t20_below_ma20_pct": -8.0,
    # volume 维
    "vr_rel_strong": 2.0,
    "vr_strong": 1.5,
    "vr_no_vol": 0.85,
    "vol5_lift": 1.2,
    "vol5_shrink": 0.75,
    # flow / market 维
    "flow_rel_strong": 1.0,
    "rs_d0_strong": 1.0,
    "rs_d0_weak": -2.0,
    "rs_5d_strong": 2.0,
    "rs_5d_weak": -2.0,
    # tape 维与解禁
    "zt_streak_hot": 2,
    "zt10_hot": 3,
    "lockup_window_short_days": 20,
    "lockup_window_long_days": 45,
    "lockup_ratio_min_pct": 1.0,
    # 风险标记（_risks，展示层）
    "risk_rsi_overbought": 75.0,
    "risk_dist_ma20_pct": 15.0,
    "risk_zt_streak": 2,
    "risk_vol_ratio": 4.0,
    "risk_board_drop_pct": -3.0,
}


def _alpha_pct() -> float:
    """板块自强/自弱阈值（%）单源：SKILL 契约值 1.5，env 可调。

    供 _sector_corr label / _move_guards / _v_sector / _stop_pressure 四处引用；
    旧 QD_STOCK_ANALYZER_STOP_WEAK_ALPHA_PCT 停用（2026-11-26 收敛到本处）。
    """
    return _env_float("QD_STOCK_ANALYZER_ALPHA_PCT", 1.5)


#: 大盘情绪展示分桶（_mood_label 的 regime 标签）；与判定门 QD_STOCK_ANALYZER_WEAK_SCORE/
#: STRONG_SCORE（_cap_regime、_stop_pressure 共用）同名不同义，勿混用。
_MOOD_BUCKETS: Dict[str, float] = {"weak_lt": 40.0, "strong_ge": 70.0}


def _conf_cap() -> Dict[str, float]:
    raw = os.environ.get("QD_STOCK_ANALYZER_CONF_CAP") or ""
    cap = dict(_CONF_CAP_DEFAULT)
    if raw:
        try:
            cap.update({k: float(v) for k, v in json.loads(raw).items() if k in cap})
        except (ValueError, TypeError) as e:
            logger.warning("[stock-analyzer] CONF_CAP 解析失败，回落默认: %s", e)
    return cap


def _bars_days() -> int:
    return _inner.bars_days()      # 天数唯一真源在 _inner（deep 与 analyze 同源，2026-11-26 收敛）


def _report_guide() -> Dict[str, str]:
    guide = dict(_ANALYZE_REPORT_GUIDE)
    guide["LENGTH"] = guide["LENGTH"].format(
        max_chars=_env_int("QD_STOCK_ANALYZER_REPORT_MAX_CHARS", _REPORT_MAX_CHARS_DEFAULT))
    return guide


def _brief_view(horizons: List[Dict[str, Any]], trend: Dict[str, Any]) -> str:
    """三档 + 长档的一句话骨架，供写作直接照抄；不新增事实，只压字数。"""
    lines = []
    for v in horizons:
        conf = f"（{v['confidence']}）" if v.get("confidence") else ""
        lines.append(f"{v['h']} {v['dir']}{conf}｜触发：{v.get('trigger') or '—'}｜证伪：{v.get('invalidate') or '—'}")
    if isinstance(trend, dict) and trend.get("trend"):
        lines.append(f"长档（日线趋势）：{trend['trend']}"
                     + (f"，{trend['basis']}" if trend.get("basis") else ""))
    return "\n".join(lines)


def _stop_key(s: Any) -> Optional[str]:
    """止损比对键：取文本中第一个数值（价位本身），措辞/孤证标注变化不误报「止损变了」。"""
    m = re.search(r"\d+(?:\.\d+)?", str(s or ""))
    return m.group(0) if m else (str(s).strip() or None)


def _consistency_view(horizons: List[Dict[str, Any]],
                      prev: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """与上一轮结论比对：只列方向/止损变化的事实，依据由写作层填；缺省 None 不触发。"""
    if not isinstance(prev, dict) or not prev:
        return None
    changes = []
    for v in horizons:
        h = v["h"]
        old = prev.get(h)
        if not isinstance(old, dict):
            continue
        old_dir = old.get("dir")
        if old_dir and old_dir != v["dir"]:
            changes.append({"h": h, "from": old_dir, "to": v["dir"]})
        old_stop = old.get("stop")
        if old_stop is not None:
            new_stop = (v.get("invalidate") or "").strip() or None
            if _stop_key(old_stop) != _stop_key(new_stop):
                changes.append({"h": h, "stop_from": old_stop, "stop_to": new_stop})
    return {"ok": bool(changes), "changes": changes,
            "note": ("方向或止损较上一轮有变化，按 CONSISTENCY 写明「由 X 改为 Y，依据 Z」；"
                     "Z 只能取本轮新事实，取不到就维持上一轮") if changes else "与上一轮一致"}


def _days(h: str) -> int:
    """档位 → 交易日天数（真源 `label_spec.HOLDS`，封顶 T+20≈1M）。"""
    from ...chain.label_spec import HOLDS

    return int(HOLDS.get(h) or 0)


def _days_until(date_str: Any) -> Optional[int]:
    """距今天数（自然日）；日期认不出返回 None（不猜）。

    「今天」取 Asia/Shanghai 当日（解禁窗口按 A 股日历口径），不用宿主机本地日：
    docker 部署常为 UTC，北京时间 00:00~08:00 会把窗口整体算多一天。
    """
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return (datetime.fromisoformat(str(date_str)[:10]).date() - today).days
    except (ValueError, TypeError):
        return None


def _events_of(raw: Any, code: str) -> Dict[str, Any]:
    """事件维（目前唯一有日历的事件源：限售解禁）。只取事实，不做处置建议。"""
    one = _inner.unwrap_single(raw, code) or {}      # 工具契约违约（非 dict）按缺数处理，不炸整份诊断
    one = one or {}
    if not isinstance(one, dict) or one.get("error") or "upcoming" not in one:
        return {"ok": False, "reason": str((one or {}).get("error") or "解禁日历缺失")}
    up = [u for u in (one.get("upcoming") or []) if isinstance(u, dict) and u.get("date")]
    return {"ok": True, "lockup": up[:4], "upcoming_count": one.get("upcoming_count", len(up))}


def _lockup_bear(events: Dict[str, Any], window_days: int) -> Optional[bool]:
    """解禁供给压力（tape 维的空证据条件）：窗口内有占比 ≥1% 的解禁 ⇒ True。

    阈值 1% 为经验阀（小比例解禁的供给冲击不足以作空证据）；日历缺失 ⇒ None（缺数）；
    窗口内解禁的占比字段认不出 ⇒ None（缺数，不当作无压力）。
    """
    if not events.get("ok"):
        return None
    for u in events.get("lockup") or []:
        dd = _days_until(u.get("date"))
        try:
            ratio = float(u.get("ratio"))
        except (TypeError, ValueError):
            ratio = None
        if dd is None or not (0 <= dd <= window_days):
            continue
        if ratio is None:
            return None
        if ratio >= _VOTE_THRESHOLDS["lockup_ratio_min_pct"]:
            return True
    return False


def _session(now: Optional[datetime] = None) -> str:
    """交易时段语义：pre / intraday / post / closed / unknown——影响触发条件的时间口径。

    **先判交易日，再判时刻**：只看时刻会把周六 10:00 答成「盘中」，节假日同理，
    于是摘要里写下「触发条件针对今日剩余交易时间」——而今日根本没有剩余交易时间。
    非交易日给 closed（数据为最近一个交易日收盘，触发条件落到下一交易日）。

    与 `trading_calendar.is_in_quote_session()` **不同义**，勿互相替代：
      · 本函数回答「这份诊断的口径落在哪个阶段」——午休 11:31~13:00 仍属 intraday
        （今日还有剩余交易时间，触发条件依然有效）；
      · `is_in_quote_session()` 回答「此刻能不能拉到新报价」——午休为 False。
    两者判据若合并，午休时要么丢掉「今日剩余时间」的口径，要么把无新成交的时段
    当成能取实时价，都会误导下游。

    日历不可用时 `is_trading_day` 抛 `CalendarUnavailable`；此处**不外抛**（时段只是
    展示口径，不该让整份诊断失败），返回 unknown 并把原因写进 `_SESSION_NOTE["unknown"]`
    ——显式承认不知道，不按时刻猜成交易日。
    """
    from zoneinfo import ZoneInfo

    from ....utils.trading_calendar import is_trading_day

    dt = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        if not is_trading_day(dt.strftime("%Y-%m-%d")):
            return "closed"
    except Exception as e:
        # 日历任何故障（CalendarUnavailable 或意外异常）都只影响展示口径，
        # 不得让整份诊断失败——显式承认不知道，不按时刻猜成交易日；留日志防静默。
        logger.warning("[stock-analyzer] session 交易日历判定失败，降级 unknown: %s: %s",
                       type(e).__name__, e)
        return "unknown"
    hm = dt.strftime("%H:%M")
    if hm < "09:30":
        return "pre"
    if hm <= "15:00":
        return "intraday"
    return "post"


_SESSION_NOTE: Dict[str, str] = {
    "pre": "盘前口径：触发条件为今日盘中观察项，不构成开盘即买",
    "intraday": "盘中口径：触发条件针对今日剩余交易时间",
    "post": "盘后口径：触发条件为次日条件单（09:30-10:00 观察，不追不补）",
    "closed": "非交易日口径：数据为最近一个交易日收盘，触发条件为下一交易日（09:30-10:00 观察，不追不补）",
    "unknown": "时段未知：交易日历不可用，无法判定今天是否开市，勿据此排条件单",
}


def _mood_label(market: Dict[str, Any]) -> str:
    """大盘情绪分桶 strong / neutral / weak / unknown（上下文标签）。

    取**大盘档** market_mood(_score)：它回答「指数与资金强不强」。个股档
    stock_mood(_score) 衡量赚钱效应，随 `market` 原样透传供七维读数，不进本分桶。
    """
    if not isinstance(market, dict) or not market:
        return "unknown"
    mood = str(market.get("market_mood") or "")
    raw_ms = market.get("market_mood_score")
    try:
        ms = float(raw_ms) if raw_ms is not None else None
    except (TypeError, ValueError):
        ms = None
    if mood in ("弱势", "偏弱") or (ms is not None and ms < _MOOD_BUCKETS["weak_lt"]):
        return "weak"
    if mood in ("偏强",) or (ms is not None and ms >= _MOOD_BUCKETS["strong_ge"]):
        return "strong"
    if not mood and ms is None:
        return "unknown"
    return "neutral"


# ─────────────────────────── 板块/概念涨跌幅修正 ───────────────────────────

_INDUSTRY_LEVEL_MARKS: Tuple[str, ...] = ("Ⅲ", "Ⅱ", "Ⅰ")


def _industry_level(name: str) -> int:
    """行业名层级序号：Ⅲ > Ⅱ > Ⅰ > 无后缀。申万分类记号，非板块白名单。"""
    for i, mark in enumerate(_INDUSTRY_LEVEL_MARKS):
        if name.endswith(mark):
            return len(_INDUSTRY_LEVEL_MARKS) - i
    return 0


def _match_industry_board(industry: Any, boards: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把 basicinfo 的 industry 匹配到 boards 里的行业板；匹配不到给 None。

    industry 常是逗号分隔的多值串（`'股份制银行Ⅲ,金融行业,银行,银行Ⅱ'`，实测八成以上），
    与 boards[].name **整串等值永不成立**，故先按逗号拆 tokens 再逐个比。

    命中多个时按「层级更细（Ⅲ>Ⅱ>Ⅰ）→ 名称更长（更具体）→ tokens 靠前」确定性取一个，
    **不依赖 boards 的排列顺序**：同一行业的两只票 boards 顺序并不相同（600004 与 600009
    的 industry 同为「交通运输,航空机场」，前者 boards 里交通运输在前、后者航空机场在前），
    按顺序取会让两只票用不同基准，α 失去可比性。
    """
    toks = [t.strip() for t in str(industry or "").split(",") if t.strip()]
    if not toks:
        return None
    cand: List[Tuple[int, int, int, Dict[str, Any]]] = []
    for b in boards:
        nm = str(b.get("name") or "")
        if nm and nm in toks:
            cand.append((-_industry_level(nm), -len(nm), toks.index(nm), b))
    if not cand:
        return None
    cand.sort(key=lambda x: x[:3])
    return cand[0][3]


def _sector_corr(raw: Any, code: str, chg_pct: Optional[float]) -> Dict[str, Any]:
    """把个股当日涨跌拆成 β（板块/概念）+ α（自身）。

    数据只用事实字段：get_stock_sector_info 的 boards[].change_pct（板块当日涨跌幅）
    与 basicinfo 的 industry 归属。α = 个股涨跌 − 板块基准涨跌。

    基准按契约「行业板优先，缺则所属板块均值」取：命中行业板就用行业板涨跌幅，
    未命中或该板无涨跌幅才退化为所属板块均值（`bench_src` 留痕是哪一种）。
    不可取两者平均——「所属板块均值」里混着地域/风格/指数成分板块（百元股、大盘成长、
    HS300_ 等），平均会把行业 β 洗掉一半，α 系统性虚高，进而把板块同步上涨误判成「自强」。
    `industry_chg_pct` 只在行业板真正命中时才有值，缺失就是缺失，不用均值冒充。

    label：自强（α≥+1.5）/ 自弱（α≤−1.5）/ 跟板块（其间）/ 未知（数据缺）。
    「未知」是合法答案：板块涨跌幅缺失时不猜 β、不做修正，只留缺口。
    """
    one = _inner.unwrap_single(raw, code) or {}    # 键名漂移（前缀/去前缀）由唯一值兜底，契约违约按缺数
    one = one or {}
    if not isinstance(one, dict) or not one or one.get("error"):
        return {"ok": False, "reason": str(one.get("error") if isinstance(one, dict) else "板块归属缺失"),
                "label": "未知"}
    boards = [b for b in (one.get("boards") or []) if isinstance(b, dict)]
    ind_name = one.get("industry")
    ind = _match_industry_board(ind_name, boards)

    def _chg(b: Optional[Dict[str, Any]]) -> Optional[float]:
        try:
            v = float((b or {}).get("change_pct"))
        except (TypeError, ValueError):
            return None
        return None if v != v else round(v, 2)

    conc_rows = [b for b in boards if b is not ind]
    conc_chgs = [c for c in (_chg(b) for b in conc_rows) if c is not None]
    ind_chg = _chg(ind)
    conc_chg = round(sum(conc_chgs) / len(conc_chgs), 2) if conc_chgs else None
    if ind_chg is not None:
        bench_chg, bench_src = ind_chg, "industry"
    elif conc_chg is not None:
        bench_chg, bench_src = conc_chg, "boards_avg"
    else:
        bench_chg, bench_src = None, "none"

    out: Dict[str, Any] = {
        "ok": False,
        "industry": ind_name,
        "industry_board": (ind or {}).get("name"),
        "industry_chg_pct": ind_chg,
        "concept_chg_pct": conc_chg,
        "bench_chg_pct": bench_chg,
        "bench_src": bench_src,
        "boards": [{"name": b.get("name"), "change_pct": _chg(b), "lead_stock": b.get("lead_stock")}
                   for b in boards[:6]],
        "concept_tags": (one.get("concept_tags") or [])[:8],
        "label": "未知",
    }
    if chg_pct is None or bench_chg is None:
        out["reason"] = ("个股涨跌缺失" if chg_pct is None else "板块涨跌幅缺失")
        return out
    alpha = round(chg_pct - bench_chg, 2)
    out["ok"] = True
    out["chg_pct"] = round(chg_pct, 2)
    out["alpha_pct"] = alpha
    _t = _alpha_pct()
    out["label"] = "自强" if alpha >= _t else ("自弱" if alpha <= -_t else "跟板块")
    return out


def _move_guards(sc: Dict[str, Any]) -> Tuple[bool, bool, List[str]]:
    """当日涨/跌证据的有效性修正（α 修正）。

    返回 (up_ok, down_ok, corr_notes)：
    · 今日上涨但 α≤−1.5（自弱，纯板块 β 拉动）⇒ up_ok=False，上涨类证据作废；
    · 今日下跌但 α≥+1.5（自强，纯板块 β 带崩）⇒ down_ok=False，下跌类证据作废。
    板块数据缺失时不修正（不猜），只留「未修正」说明。
    """
    notes: List[str] = []
    if not sc.get("ok"):
        return True, True, ["板块α修正：数据缺失，未修正（未知）"]
    alpha = sc.get("alpha_pct")
    label = sc.get("label")
    up_ok, down_ok = True, True
    chg = sc.get("chg_pct")
    _t = _alpha_pct()
    if chg is not None and chg > 0 and alpha is not None and alpha <= -_t:
        up_ok = False
        notes.append(f"板块α修正：今日 +{chg}% 系板块带动（板块 {sc.get('bench_chg_pct')}%，α={alpha}%，自弱），"
                     "上涨类证据作废")
    if chg is not None and chg < 0 and alpha is not None and alpha >= _t:
        down_ok = False
        notes.append(f"板块α修正：今日 {chg}% 系板块带崩（板块 {sc.get('bench_chg_pct')}%，α={alpha}%，自强），"
                     "下跌类证据作废")
    if not notes:
        notes.append(f"板块α修正：α={alpha}%（{label}），当日涨跌含自身成分")
    return up_ok, down_ok, notes


# ─────────────────────────── 维度投票 ───────────────────────────

def _weigh(bull: List[Tuple[str, Optional[bool]]],
           bear: List[Tuple[str, Optional[bool]]]) -> Tuple[Optional[int], List[str], List[str]]:
    """单维度内三值条件 → 投票 (vote, why, unknown)。

    vote：+1/−1 = 维内同向成立；0 = 平衡或维内冲突（冲突点写明）；None = 全部条件缺数。
    条件值 None = 该条缺数，既不算多也不算空，只进 unknown —— 缺数不猜。
    """
    bt = [n for n, v in bull if v is True]
    bf = [n for n, v in bear if v is True]
    un = [n for n, v in bull + bear if v is None]
    why: List[str] = []
    if bt and not bf:
        why = [f"✓{n}" for n in bt] + [f"（缺数）{n}" for n in un]
        return 1, why, un
    if bf and not bt:
        why = [f"✓{n}" for n in bf] + [f"（缺数）{n}" for n in un]
        return -1, why, un
    if bt and bf:
        why = [f"多✓{n}" for n in bt] + [f"空✓{n}" for n in bf] + [f"（缺数）{n}" for n in un]
        return 0, why + ["维内冲突，互不印证"], un
    if len(un) == len(bull) + len(bear):
        return None, [f"（缺数）{n}" for n in un], un
    why = [f"{n}=否" for n, v in bull + bear if v is False][:4]
    return 0, why + [f"（缺数）{n}" for n in un], un


def _v_model(h: str, pred: Dict[str, Any]) -> Dict[str, Any]:
    """model 维：T+1 标定分（唯一标定数字，禁止外推到其余档）。"""
    if h != "T+1":
        return {"vote": None, "why": ["标定分只覆盖 T+1，其余档不外推（不计入本档）"], "applicable": False}
    if not (isinstance(pred, dict) and pred.get("ok")):
        return {"vote": None, "why": [f"P(T+1涨)=未知（{pred.get('reason') if isinstance(pred, dict) else '无预测'}）"],
                "applicable": True}
    try:
        p = float(pred.get("p_up"))
    except (TypeError, ValueError):
        p = None
    if p is None:
        return {"vote": None, "why": ["P(T+1涨)不可解析（标定分字段异常）"], "applicable": True}
    vote = 1 if p >= _VOTE_THRESHOLDS["model_p_up_long"] else (
        -1 if p <= _VOTE_THRESHOLDS["model_p_up_short"] else 0)
    return {"vote": vote, "why": [f"{'✓' if vote else '·'}P(T+1涨)={p}（唯一标定分）"], "applicable": True}


def _v_structure(h: str, tech: Dict[str, Any],
                 levels: Dict[str, Any]) -> Dict[str, Any]:
    """structure 维：均线结构 / 位置 / 乖离，长档看更粗的结构。"""
    if not tech:
        return {"vote": None, "why": ["技术面缺数"], "applicable": True}
    sup0 = (levels.get("supports") or [None])[0]
    sup_d = sup0.get("dist_pct") if isinstance(sup0, dict) else None
    sup_v = sup0.get("verified") if isinstance(sup0, dict) else None
    stack = tech.get("ma_bull_stack")
    pos20 = tech.get("pos_in_20d_pct")
    dist20 = tech.get("dist_ma20_pct")
    rsi = tech.get("rsi14")

    def _b(x, n):
        return (n, None if x is None else bool(x))

    th = _VOTE_THRESHOLDS
    if h in ("T+1", "T+3"):
        bull = [("贴已验证支撑", None if (sup_d is None or sup_v is None)
                 else (sup_v and sup_d >= th["sup_near_max_below_pct"])),
                ("均线多头", stack),
                ("未超买", None if rsi is None else rsi < th["rsi_overbought"])]
        bear = [("跌破MA20超3%", None if dist20 is None else dist20 < th["break_ma20_pct"]),
                ("追高过热", None if (dist20 is None and rsi is None)
                 else ((dist20 or 0) >= th["chase_dist_ma20_pct"] or (rsi or 0) >= th["rsi_overbought"])),
                ("远离支撑", None if sup_d is None else sup_d < th["sup_far_below_pct"])]
    elif h == "T+5":
        bull = [("均线多头", stack),
                ("20日位置≥50", None if pos20 is None else pos20 >= th["t5_pos_up_min"]),
                ("乖离≤10%", None if dist20 is None else dist20 <= th["t5_dist_cap_pct"])]
        bear = [("均线非多头", _neg(stack)),
                ("20日位置≤40", None if pos20 is None else pos20 <= th["t5_pos_dn_max"]),
                ("价格在MA20下方", None if dist20 is None else dist20 < 0)]
    elif h == "T+10":
        bull = [("均线多头", stack),
                ("20日位置≥60", None if pos20 is None else pos20 >= th["t10t20_pos_up_min"])]
        bear = [("均线非多头", _neg(stack)),
                ("20日位置≤30", None if pos20 is None else pos20 <= th["t10_pos_dn_max"]),
                ("MA20下方超5%", None if dist20 is None else dist20 <= th["t10_below_ma20_pct"])]
    else:
        bull = [("均线多头", stack),
                ("20日位置≥60", None if pos20 is None else pos20 >= th["t10t20_pos_up_min"]),
                ("乖离0~15%", None if dist20 is None
                 else th["t20_dist_min_pct"] <= dist20 <= th["t20_dist_max_pct"])]
        bear = [("均线非多头", _neg(stack)),
                ("MA20下方超8%", None if dist20 is None else dist20 <= th["t20_below_ma20_pct"])]
    vote, why, un = _weigh(bull, bear)
    return {"vote": vote, "why": why, "applicable": True}


def _v_volume(h: str, tech: Dict[str, Any], vol5_ratio: Optional[float],
              chg: Optional[float], up_ok: bool, down_ok: bool) -> Dict[str, Any]:
    """volume 维：量能及其与涨跌的配合（vr_rel=对大盘校准口径，优先于绝对量比）。"""
    vr = tech.get("vol_ratio")
    vr_rel = tech.get("vr_rel")

    th = _VOTE_THRESHOLDS

    def _strong_vol() -> Optional[bool]:
        if vr_rel is not None:
            return vr_rel >= th["vr_rel_strong"]
        return None if vr is None else vr >= th["vr_strong"]

    if h in ("T+1", "T+3"):
        up = None if chg is None else chg > 0
        down = None if chg is None else chg < 0
        bull = [("放量上涨", None if (up is None or _strong_vol() is None) else (up and _strong_vol() and up_ok))]
        bear = [("涨无量（量价背离)", None if (up is None or vr is None) else (up and vr < th["vr_no_vol"])),
                ("放量下跌", None if (down is None or _strong_vol() is None) else (down and _strong_vol() and down_ok))]
    else:
        bull = [("5日量能抬升", None if vol5_ratio is None else vol5_ratio >= th["vol5_lift"])]
        bear = [("5日量能萎缩", None if vol5_ratio is None else vol5_ratio <= th["vol5_shrink"])]
    vote, why, un = _weigh(bull, bear)
    if vr_rel is not None:
        why = [f"量比{vr}/大盘校准vr_rel={vr_rel}"] + why
    return {"vote": vote, "why": why, "applicable": True}


def _v_flow(h: str, flow: Dict[str, Any]) -> Dict[str, Any]:
    """flow 维：多日资金总体（量价方向净额口径，禁止写成「主力」）。"""
    if not (isinstance(flow, dict) and flow.get("ok")):
        return {"vote": None, "why": [f"资金流缺数（{flow.get('reason') if isinstance(flow, dict) else '无'}）"],
                "applicable": True}
    rec = flow.get("recent") or {}
    n5 = rec.get("net_5d_wan")
    n20 = rec.get("net_20d_wan")
    rel = flow.get("flow_rel")
    note = f"量价方向净额近似（非主力分类）：5日{n5}万/20日{n20}万"
    if rec.get("streak_dir"):
        note += f"（连续方向:{rec.get('streak_dir')}）"
    if h in ("T+1", "T+3"):
        bull = [("5日净流入", None if n5 is None else n5 > 0),
                ("资金强于大盘", None if rel is None else rel >= _VOTE_THRESHOLDS["flow_rel_strong"])]
        bear = [("5日净流出", None if n5 is None else n5 < 0),
                ("资金弱于大盘", None if rel is None else rel <= -_VOTE_THRESHOLDS["flow_rel_strong"])]
    else:
        bull = [("20日净流入", None if n20 is None else n20 > 0),
                ("5日净流入", None if n5 is None else n5 > 0)]
        bear = [("20日净流出", None if n20 is None else n20 < 0),
                ("5日净流出", None if n5 is None else n5 < 0)]
    vote, why, un = _weigh(bull, bear)
    return {"vote": vote, "why": [note] + why, "applicable": True}


def _v_market(h: str, tech: Dict[str, Any]) -> Dict[str, Any]:
    """market 维：相对大盘超额（rs_d0 / rs_5d），剔除大盘 β 后的强弱。"""
    rs0 = tech.get("rs_d0")
    rs5 = tech.get("rs_5d")
    if rs0 is None and rs5 is None:
        return {"vote": None, "why": ["相对大盘超额缺失（基准未取到）"], "applicable": True}
    th = _VOTE_THRESHOLDS
    if h in ("T+1", "T+3"):
        bull = [("强于大盘", None if rs0 is None else rs0 >= th["rs_d0_strong"])]
        bear = [("弱于大盘", None if rs0 is None else rs0 <= th["rs_d0_weak"])]
        why_head = f"超额rs_d0={rs0}%"
    else:
        bull = [("5日强于大盘", None if rs5 is None else rs5 >= th["rs_5d_strong"])]
        bear = [("5日弱于大盘", None if rs5 is None else rs5 <= th["rs_5d_weak"])]
        why_head = f"超额rs_5d={rs5}%"
    vote, why, un = _weigh(bull, bear)
    return {"vote": vote, "why": [why_head] + why, "applicable": True}


def _v_sector(h: str, sc: Dict[str, Any]) -> Dict[str, Any]:
    """sector 维：板块/概念 β 剔除后的自身强弱（α）。"""
    if not sc.get("ok"):
        return {"vote": None, "why": [f"板块/概念涨跌幅缺失（{sc.get('reason', '未知')}）"], "applicable": True}
    alpha = sc.get("alpha_pct")
    if sc.get("industry_board"):
        head = (f"行业「{sc.get('industry_board')}」{sc.get('industry_chg_pct')}%，"
                f"概念均{sc.get('concept_chg_pct')}%，α={alpha}%（{sc.get('label')}）")
    else:
        head = (f"行业板未命中（industry={sc.get('industry')}），"
                f"基准用所属板块均值{sc.get('bench_chg_pct')}%，α={alpha}%（{sc.get('label')}）")
    _t = _alpha_pct()
    vote = 1 if alpha >= _t else (-1 if alpha <= -_t else 0)
    return {"vote": vote, "why": [head], "applicable": True}


def _v_tape(h: str, patterns: List[Dict], patterns_ok: bool, seal: Dict[str, Any],
            tech: Dict[str, Any], risk_flags: List[str], events: Dict[str, Any],
            up_ok: bool, down_ok: bool) -> Dict[str, Any]:
    """tape 维：K 线形态 / 封板结构 / 连板透支 / 拉高出货线索 / 解禁供给压力。

    patterns_ok=False（形态识别未跑或异常）⇒ 形态条件算缺数；
    patterns_ok=True 且空列表 = 确实无形态（已知事实，不是缺数）。
    """
    if not tech and not patterns and not seal:
        return {"vote": None, "why": ["形态/封板/涨停史均缺数"], "applicable": True}
    tag = (seal or {}).get("tag")
    tier = (seal or {}).get("seal_tier")
    zt_streak = tech.get("zt_streak")
    zt10 = tech.get("zt_10d")
    pump = any(("拉高出货" in r or "脉冲" in r or "背离" in r) for r in risk_flags or [])
    strong_seal = tag in ("one_word", "open0", "open1_2") and tier in ("thick", "vthick")
    weak_seal = tag in ("open3_5", "open6p") or tier == "thin"
    pat_cond = (None if not patterns_ok else (len(patterns) >= 1 and up_ok))
    if h in ("T+1", "T+3"):
        bull = [("突破/启动形态", pat_cond),
                ("强封（一字/厚封单）", None if tag in (None, "none", "unknown") else strong_seal)]
        bear = [("弱势封板（开板多/封单薄）", None if tag in (None, "none", "unknown") else weak_seal),
                ("高位连板≥2", None if zt_streak is None else zt_streak >= _VOTE_THRESHOLDS["zt_streak_hot"]),
                ("近期解禁（≤20日，占比≥1%）", _lockup_bear(events, _VOTE_THRESHOLDS["lockup_window_short_days"])),
                ("拉高出货线索", bool(pump))]
    else:
        bull = [("形态确认（≥2）", None if not patterns_ok else (len(patterns) >= 2 and up_ok))]
        bear = [("涨停透支（10日≥3次）", None if zt10 is None else zt10 >= _VOTE_THRESHOLDS["zt10_hot"]),
                ("近期解禁（≤45日，占比≥1%）", _lockup_bear(events, _VOTE_THRESHOLDS["lockup_window_long_days"])),
                ("拉高出货线索", bool(pump))]
    vote, why, un = _weigh(bull, bear)
    head = []
    if patterns:
        head.append("形态:" + "、".join(str(p.get("pattern")) for p in patterns[:4]))
    if tag and tag not in ("none", "unknown"):
        head.append(f"封板:{tag}/{tier or '未知'}")
    return {"vote": vote, "why": head + why, "applicable": True}


def _neg(v: Optional[bool]) -> Optional[bool]:
    return None if v is None else (not v)


def _votes(h: str, ctx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """该档位全部维度的投票。applicable=False 的维度（如 T+3+ 的标定分）不计入判定。"""
    sc = ctx["sc"]
    up_ok, down_ok, corr = ctx["guards"]
    return {
        "model": _v_model(h, ctx["pred"]),
        "structure": _v_structure(h, ctx["tech"], ctx["levels"]),
        "volume": _v_volume(h, ctx["tech"], ctx["vol5_ratio"], ctx["chg"], up_ok, down_ok),
        "flow": _v_flow(h, ctx["flow"]),
        "market": _v_market(h, ctx["tech"]),
        "sector": _v_sector(h, sc),
        "tape": _v_tape(h, ctx["patterns"], ctx["patterns_ok"], ctx["seal"],
                        ctx["tech"], ctx["risk_flags"], ctx["events"], up_ok, down_ok),
    }


def _decide(votes: Dict[str, Dict[str, Any]]) -> Tuple[str, str, float]:
    """多维投票 → (方向, 理由, 一致度)。判定纪律见模块 docstring「证据纪律」。"""
    act = {k: v for k, v in votes.items() if v.get("applicable", True)}
    plus = [k for k, v in act.items() if v["vote"] == 1]
    minus = [k for k, v in act.items() if v["vote"] == -1]
    zero = [k for k, v in act.items() if v["vote"] == 0]
    unknown = [k for k, v in act.items() if v["vote"] is None]
    known = len(plus) + len(minus) + len(zero)
    agree = round(abs(len(plus) - len(minus)) / known, 2) if known else 0.0

    if known == 0:
        return "未知", "全部维度缺数，无可判证据", 0.0
    if "structure" in unknown:
        return "未知", f"技术面缺数，结构/价位不可判（缺：{'、'.join(unknown)}）", 0.0
    if len(plus) >= 2 and len(plus) - len(minus) >= 2:
        return "看多", f"{len(plus)} 维同向（{'、'.join(plus)}），反向仅 {len(minus)} 维，互相印证", agree
    if len(minus) >= 2 and len(minus) - len(plus) >= 2:
        return "看空", f"{len(minus)} 维同向（{'、'.join(minus)}），反向仅 {len(plus)} 维，互相印证", agree
    if plus and minus:
        return "证据不足", f"多空并存（多：{'、'.join(plus)} / 空：{'、'.join(minus)}），无法相互验证", agree
    if not plus and not minus:
        # 「中性」也是结论，同样要证据足：有效维 ≥3 且已知维不少于未知维才敢判平衡
        if known >= 3 and known >= len(unknown):
            return "中性", f"{known} 维均为中性信号（{'、'.join(zero)}），多维印证为平衡", agree
        return "证据不足", f"有效维度仅 {known}/{len(act)} 且均无方向，不足以判中性", agree
    return "证据不足", f"仅 {(plus or minus)[0]} 一维给出方向，未获交叉验证", agree


# ─────────────────────────── 单档视图 ───────────────────────────

def _level_ref(levels: Dict[str, Any], h: str, side: str) -> Optional[Dict[str, Any]]:
    """按周期挑锚定价位：短档（T+1/T+3/T+5）取排序首位（verified→方法数→距离），
    只有长档（T+10/T+20）优先更粗的级别（ma20/ma60/lo60/枢轴）。

    2026-11-26 修正：此前 T+5 误随长档用粗锚定，而 SKILL 只把 ma20/ma60/lo60
    级优先权给 T+10/T+20，T+5 属输出短档，其触发/证伪与 T+1/T+3 同口径。
    """
    rows = levels.get("supports" if side == "sup" else "resists") or []
    if not rows:
        return None
    if h in _SHORT_HORIZONS:
        return rows[0]
    coarse = ("ma20", "ma60", "lo60", "hi60", "枢轴P")
    for r in rows:
        full = " ".join(r.get("methods") or []) + " " + (r.get("name") or "")
        if any(k in full for k in coarse):
            return r
    return rows[0]


def _ref_txt(r: Optional[Dict[str, Any]]) -> str:
    if not r:
        return "未知（该侧价位未经验证）"
    mark = "" if r.get("verified") else "，孤证仅供参考"
    return f"{r['px']}（{r.get('name')}{mark}）"


def _horizon_view(h: str, ctx: Dict[str, Any], cap: float, evidence: float) -> Dict[str, Any]:
    """单档位视图：方向 + 置信度 + 触发/证伪 + 多维依据（T+1 之外不给概率）。"""
    need = _days(h)
    view: Dict[str, Any] = {"h": h, "days": need, "dir": "未知", "p_up": None,
                            "confidence": 0.0, "agree": 0.0, "votes": {}, "basis": [],
                            "unknown": [], "trigger": "", "invalidate": "", "reason": ""}

    if need <= 0:
        view["reason"] = f"档位 {h} 天数未收录（label_spec.HOLDS 缺键），无法推演"
        return view
    if ctx["bars_n"] < need:
        view["reason"] = f"日线仅 {ctx['bars_n']} 根，不足 {need} 根，该档无法推演"
        return view
    if not ctx["tech"]:
        view["reason"] = "技术面缺数，结构与价位均不可判"
        return view

    votes = _votes(h, ctx)
    view["votes"] = {k: v["vote"] for k, v in votes.items() if v.get("applicable", True)}
    view["unknown"] = [k for k, v in votes.items() if v.get("applicable", True) and v["vote"] is None]
    for k, v in votes.items():
        if v.get("applicable", True) and v.get("why"):
            view["basis"].append(f"{k}：" + "；".join(v["why"]))
    for note in ctx["guards"][2]:
        view["basis"].append(note)

    dir_, reason, agree = _decide(votes)
    view["dir"] = dir_
    view["reason"] = reason
    view["agree"] = agree

    if h == "T+1" and ctx["pred"].get("ok"):
        view["p_up"] = ctx["pred"].get("p_up")
    elif h == "T+1":
        view["basis"].append(f"P(T+1涨)=未知（{ctx['pred'].get('reason')}）")

    sup = _level_ref(ctx["levels"], h, "sup")
    res = _level_ref(ctx["levels"], h, "res")
    sup_t, res_t = _ref_txt(sup), _ref_txt(res)
    stop_t = sup_t
    last_defense = None
    if h in _SHORT_HORIZONS and dir_ != "看空":
        near = ctx["levels"].get("nearest_support")
        if near and sup and near.get("px") != sup.get("px"):
            stop_t = _ref_txt(near)
            last_defense = sup_t
    if dir_ == "看多":
        view["trigger"] = f"回踩 {sup_t} 不破且量能转强，或放量站上 {res_t}"
        view["invalidate"] = f"收盘跌破 {stop_t}"
    elif dir_ == "看空":
        view["trigger"] = f"反抽 {res_t} 不破且量能不济"
        view["invalidate"] = f"放量站上 {res_t}"
    else:
        view["trigger"] = f"放量站上 {res_t} 才看多"
        view["invalidate"] = f"收盘跌破 {stop_t} 转空"
    if last_defense:
        view["last_defense"] = last_defense
        view["invalidate"] += f"；再破 {last_defense} 则下一支撑需重估"

    strength = agree if dir_ in ("看多", "看空") else 0.0
    view["confidence"] = round(cap * evidence * strength, 2)
    return view


def _execution_core(horizons: List[Dict[str, Any]], levels: Dict[str, Any],
                    price: Optional[float]) -> Dict[str, Any]:
    """全自动四要素：买点/卖点/预计持仓时间/持仓风险收益比。

    买/卖点都是**条件触发参考价**，不是下单指令；取不到就写 None + 原因，不猜。
    风险收益比用现价到最近止损参考、最近压力的距离，口径随结果一起透出。
    """
    core: Dict[str, Any] = {
        "buy_point": None, "sell_point": None, "hold_days": None,
        "risk_reward_ratio": None, "caliber": "",
    }
    if not horizons:
        core["caliber"] = "三档方向缺失，四要素无法派生"
        return core

    decided = [v for v in horizons if v.get("dir") in ("看多", "看空")]
    primary = decided[0] if decided else horizons[0]
    core["hold_days"] = primary.get("days")

    supports = levels.get("supports") or []
    resists = levels.get("resists") or []
    near = levels.get("nearest_support") or (supports[0] if supports else None)
    top = resists[0] if resists else None

    def _pt(r: Optional[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
        if not r or r.get("px") is None:
            return None
        return {"price": r["px"], "name": r.get("name"), "kind": kind,
                "verified": bool(r.get("verified"))}

    buy_kind = ("放量站上转多触发" if primary.get("dir") in ("看多", "证据不足")
                else "放量站上（转多确认，当前偏空不参与）")
    sell_kind = ("收盘跌破止损参考" if primary.get("dir") != "看空"
                 else "收盘跌破（偏空延续）；反抽压力为减仓参考")
    core["buy_point"] = _pt(top, buy_kind)
    core["sell_point"] = _pt(near, sell_kind)

    cap = _env_float("QD_STOCK_ANALYZER_RR_CAP", 99.0)
    if isinstance(price, (int, float)) and price > 0:
        risk = price - float(near["px"]) if near and near.get("px") is not None else None
        reward = float(top["px"]) - price if top and top.get("px") is not None else None
        if risk is not None and reward is not None:
            if risk > 0 and reward > 0:
                rr = round(reward / risk, 2)
                core["risk_reward_ratio"] = f"≥{int(cap)}" if rr > cap else rr
            else:
                core["risk_reward_ratio"] = "不适用（现价不在止损与压力之间）"
    if core["risk_reward_ratio"] is None:
        core["risk_reward_ratio"] = "未知（缺现价或验证价位）"

    core["caliber"] = (
        "四要素由 horizons/levels 派生，买点卖点是条件触发参考价、不是买卖指令；"
        "risk_reward_ratio=(最近压力−现价)/(现价−最近止损参考)，价外记不适用；"
        "模板字段供全自动读取，不构成仓位建议")
    return core


# ─────────────────────────── 证据完整度 ───────────────────────────

def _evidence(card: Dict[str, Any], sector: Dict[str, Any],
              missing: List[str]) -> Tuple[float, List[str], List[str]]:
    """证据完整度 = 拿到维度数 / 计入维度数。

    「拿到」用 missing 留痕词 + 满卡字段交叉核对（满卡经 _slim_card 瘦身，
    空列表/None 被剔除，只看键在不在会误判）。
    """
    miss_text = "；".join(missing or [])
    have: List[str] = []
    for d in _DIMS:
        tok = _DIM_MISS_TOKEN.get(d)
        if tok and tok in miss_text:
            continue
        if d == "rt":
            ok = bool((card.get("rt") or {}).get("price") is not None)
        elif d == "tech":
            ok = bool(card.get("tech"))
        elif d == "prediction":
            ok = card.get("p_up") is not None
        elif d == "patterns":
            ok = "日线缺失" not in miss_text and "形态识别" not in miss_text
        elif d == "flow":
            ok = bool((card.get("flow") or {}).get("ok"))
        elif d == "sector":
            ok = bool(sector.get("ok"))
        else:  # zt
            ok = bool((card.get("tech") or {}).get("zt_streak") is not None) and "日线缺失" not in miss_text
        if ok:
            have.append(d)
    return (round(len(have) / len(_DIMS), 2) if _DIMS else 0.0), have, list(_DIMS)


def _stop_pressure(tech: Dict[str, Any], flow: Dict[str, Any], sc: Dict[str, Any],
                   market: Dict[str, Any], levels: Dict[str, Any],
                   chip: Optional[Dict[str, Any]] = None,
                   flow_rhythm: Optional[Dict[str, Any]] = None,
                   intel: Optional[Dict[str, Any]] = None,
                   capital: Optional[Dict[str, Any]] = None,
                   intraday: Optional[Dict[str, Any]] = None,
                   flow_daily: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """离场压力的**事实汇总**：数清价格/资金/板块/大盘/筹码/情报/融资/日内结构几个方向的力量，不做买卖指令。

    只陈述已算出的事实（破位/资金净流出/板块自弱/大盘谨慎/距最后防线距离/筹码套牢压顶/
    尾盘连续流出/情报否决/杠杆回落/开盘区间破位/日内均线空头/日频连续流出），
    供写作层提醒用户注意止损参考位；不进七维投票、不改 dir/confidence。
    """
    facts: List[str] = []
    cpos = tech.get("close_pos_in_day")
    if cpos is not None:
        try:
            if float(cpos) <= 0.05:
                facts.append(f"收在日内低位（close_pos_in_day={cpos}）")
        except (TypeError, ValueError):
            pass
    rec = (flow.get("recent") if isinstance(flow, dict) else None) or {}
    n5 = rec.get("net_5d_wan")
    if n5 is not None:
        try:
            if float(n5) < 0:
                facts.append(f"5日资金净流出（{n5}万）")
        except (TypeError, ValueError):
            pass
    alpha = sc.get("alpha_pct")
    if alpha is not None:
        try:
            if float(alpha) <= -_alpha_pct():
                facts.append(f"弱于板块（α={alpha}%）")
        except (TypeError, ValueError):
            pass
    ms = market.get("market_mood_score")
    if ms is not None:
        try:
            if float(ms) <= _env_float("QD_STOCK_ANALYZER_WEAK_SCORE", 45.0):
                facts.append(f"大盘情绪偏弱（{ms}）")
        except (TypeError, ValueError):
            pass
    # 两层止损的距离差 = 止损参考位（nearest_support）↔ 最后防线（排序首位支撑），
    # 与 _horizon_view 展示的两层严格同源；两层重合或只剩一层 ⇒ None，
    # 不拿未展示的支撑冒充第二层。
    gap_pct = None
    near = levels.get("nearest_support")
    top = (levels.get("supports") or [None])[0]
    if (near and top and near.get("px") != top.get("px")
            and near.get("dist_pct") is not None and top.get("dist_pct") is not None):
        gap_pct = round(abs(float(top["dist_pct"]) - float(near["dist_pct"])), 2)
    if chip and chip.get("ok"):
        loss = chip.get("loss_ratio")
        lower = (chip.get("band_90") or {}).get("lower")
        price = chip.get("current_price")
        floor = _env_float("QD_STOCK_ANALYZER_CHIP_LOSS_RATIO", 0.9)
        if isinstance(loss, (int, float)) and loss >= floor \
                and isinstance(lower, (int, float)) and isinstance(price, (int, float)) \
                and price < lower:
            facts.append(f"套牢盘{chip.get('loss_ratio_pct') or loss}且现价低于90%筹码下沿{lower}")
    if flow_rhythm and flow_rhythm.get("ok"):
        streak = flow_rhythm.get("tail_streak") or []
        if len(streak) >= 3 and all(float(x.get("wan") or 0) < 0 for x in streak):
            total = round(sum(float(x.get("wan") or 0) for x in streak), 1)
            facts.append(f"尾盘连续{len(streak)}分钟净流出（合计{total}万）")
    if intel and intel.get("ok") and intel.get("veto"):
        facts.append("情报一票否决")
    if capital and capital.get("ok"):
        chg = (capital.get("margin") or {}).get("rz_5d_change_pct")
        floor = _env_float("QD_STOCK_ANALYZER_MARGIN_5D_PCT", 0)
        if isinstance(chg, (int, float)) and chg <= floor:
            facts.append(f"融资余额5日变化{chg}%")
    if intraday and intraday.get("ok"):
        op = intraday.get("opening_range") or {}
        if op.get("breakout_down"):
            facts.append(f"开盘{op.get('minutes')}分钟向下破位"
                         f"（区间{op.get('low')}~{op.get('high')}）")
        ma = intraday.get("intraday_ma") or {}
        if ma.get("align") == "空头":
            facts.append("日内均线空头排列")
    if flow_daily and flow_daily.get("ok"):
        streak = flow_daily.get("main_net_streak")
        threshold = _env_int("QD_STOCK_ANALYZER_FLOW_STREAK_MIN", 2)
        if isinstance(streak, int) and streak >= threshold \
                and flow_daily.get("main_net_streak_dir") == "outflow":
            facts.append(f"日频资金连续{streak}日净流出")
    return {
        "ok": bool(facts),
        "n": len(facts),
        "facts": facts,
        "gap_between_stops_pct": gap_pct,
        "caliber": "离场压力的事实条数；只提示核对止损参考位，不产生买卖或仓位指令，"
                   "也不改变方向投票与置信度",
    }


def _risks(card: Dict[str, Any], tech: Dict[str, Any], sc: Dict[str, Any],
           events: Dict[str, Any]) -> List[str]:
    """客观风险标记：只罗列事实，不构成买卖建议。"""
    out: List[str] = [str(r) for r in (card.get("rt_risks") or []) + (card.get("flow_risks") or [])]
    th = _VOTE_THRESHOLDS
    if tech.get("is_20d_high"):
        out.append("处于20日最高位（追高风险）")
    if (tech.get("rsi14") or 0) >= th["risk_rsi_overbought"]:
        out.append(f"RSI{tech['rsi14']} 超买")
    if (tech.get("dist_ma20_pct") or 0) >= th["risk_dist_ma20_pct"]:
        out.append(f"离MA20已有 {tech['dist_ma20_pct']}%（乖离偏大）")
    if (tech.get("zt_streak") or 0) >= th["risk_zt_streak"]:
        out.append(f"{tech['zt_streak']}连板（次日分歧概率高）")
    if (tech.get("vol_ratio") or 0) >= th["risk_vol_ratio"]:
        out.append(f"量比{tech['vol_ratio']}异常放大（注意出货）")
    if tech.get("limit_up"):
        out.append("当日涨停封板（次日竞价溢价已 price in，需防冲高回落）")
    if sc.get("label") == "自弱":
        out.append(f"弱于所属板块（α={sc.get('alpha_pct')}%，涨跌系板块β）")
    for u in (events.get("lockup") or []):
        dd = _days_until(u.get("date"))
        if dd is not None and 0 <= dd <= 30:
            out.append(f"解禁临近：{u.get('date')}（{dd} 天后）{u.get('type') or ''}解禁 "
                       f"{u.get('shares')} 股，占总股本 {u.get('ratio')}%")
    for b in (sc.get("boards") or []):
        try:
            c = float(b.get("change_pct"))
        except (TypeError, ValueError):
            continue
        if c <= _VOTE_THRESHOLDS["risk_board_drop_pct"]:
            out.append(f"所属板块「{b.get('name')}」今日 {c}%（板块拖累）")
    return out


# ─────────────────────────── 汇总与渲染 ───────────────────────────

_MACD_STRENGTH_TEXT: Dict[str, str] = {
    "even": "绿柱幅均衡",
    "sell_pressure_low": "绿柱幅偏小",
    "sell_pressure_high": "绿柱幅偏大",
}


def _mtf_line(mtf: Dict[str, Any]) -> str:
    """多周期结构摘要（deep=true 才有）：日线 + 15m + 5m 的位置/强弱，一行给全。

    **口径必须写明**：这里的 `weak_to_strong` / `strong_to_weak` 是**当日盘中换手**
    口径（VWAP 收复与否），与日线 `tech.regime` 的「弱转强/强转弱」同名不同义
    （后者看跨日位置）。故摘要以「分时口径」开头标注，且**不参与七维投票**——
    它是独立证据维度，用于强化与印证，不改写日线结论。
    """
    d = mtf.get("daily") or {}
    m15 = mtf.get("m15") or {}
    m5 = mtf.get("m5") or {}

    def _num(v, nd: int = 2) -> str:
        try:
            return f"{float(v):.{nd}f}"
        except (TypeError, ValueError):
            return "—"

    segs: List[str] = []
    if d:
        pos = []
        for key, label in (("above_ma5", "MA5"), ("above_ma20", "MA20")):
            if d.get(key) is not None:
                pos.append(f"{label}{'上方' if d.get(key) else '下方'}")
        s = "日线 " + ("/".join(pos) + " " if pos else "")
        s += f"RSI {_num(d.get('rsi'), 1)} 量比 {_num(d.get('vol_ratio'))}"
        if d.get("macd_bar") is not None:
            s += f" MACD柱 {_num(d.get('macd_bar'))}"
        segs.append(s)
    for tag, blk in (("15m", m15), ("5m", m5)):
        if not blk:
            continue
        trend = {"up": "上行", "down": "下行", "flat": "走平"}.get(
            str(blk.get("trend")), str(blk.get("trend") or "—"))
        s = f"{tag} 趋势{trend} RSI {_num(blk.get('rsi'), 1)} 量比 {_num(blk.get('vol_ratio'))}"
        strength = _MACD_STRENGTH_TEXT.get(str(blk.get("macd_strength") or ""))
        if strength:
            s += f" {strength}"
        segs.append(s)
    tail: List[str] = []
    if mtf.get("weak_to_strong"):
        tail.append("盘中弱转强")
    if mtf.get("strong_to_weak"):
        tail.append("盘中强转弱")
    if mtf.get("volume_pattern"):
        tail.append(str(mtf.get("volume_pattern")))
    body = "；".join(s for s in segs if s)
    if tail:
        body += ("｜" if body else "") + "、".join(tail)
    missing = [str(m) for m in (mtf.get("missing") or [])]
    if missing:
        body += f"（缺：{'、'.join(missing[:3])}）"
    if mtf.get("stale"):
        cc = mtf.get("caliber_check") or {}
        body += (f"｜【短周期跨日不采信】分钟线停在 {cc.get('minute_last')}，"
                 f"日线目标日 {cc.get('daily_target')}，上方 15m/5m 属更早交易日的形态")
    return "多周期（分时口径，与日线 regime 不同义）：" + (body or "未知")


def _render(out: Dict[str, Any]) -> str:
    """诊断摘要：结论先行 + 全自动四要素 + 极简三档 + 一句经验依据，不给模糊措辞。

    长篇事实（九块补证/逐维投票/basis）留在结构化 dict 里做经验收集，不进正文。
    """
    def _ptxt(p: Any) -> str:
        if not isinstance(p, dict) or p.get("price") is None:
            return "未知"
        return f"{p['price']}（{p.get('name') or p.get('kind') or '参考'}）"

    if out.get("error"):
        return f"ERR: {out['error']}"
    sess = {"pre": "盘前", "intraday": "盘中", "post": "盘后",
            "closed": "非交易日", "unknown": "时段未知"}.get(out.get("session"), "")
    core = out.get("execution_core") or {}
    head = (f"{out.get('code', '')} {out.get('name', '')}｜{out.get('as_of') or ''}"
            + (f" {sess}" if sess else "") + f"｜证据完整度 {out.get('evidence')}")

    lines = [head]
    lines.append("四要素：")
    lines.append(f"买点 {_ptxt(core.get('buy_point'))}")
    lines.append(f"卖点 {_ptxt(core.get('sell_point'))}")
    lines.append(f"持仓时间 {core.get('hold_days') if core.get('hold_days') is not None else '未知'}日"
                 f"｜风险收益比 {core.get('risk_reward_ratio') or '未知'}")
    hz_seg = " ".join(
        f"{v['h']} {v['dir']}" + (f"（{v['confidence']}）" if v.get("confidence") else "")
        for v in (out.get("horizons") or []))
    if hz_seg:
        lines.append("三档：" + hz_seg + "（触发/证伪见结构化 horizons）")
    tr = out.get("trend_outlook") or {}
    if tr.get("trend"):
        lines.append(f"长档：{tr['trend']}")
    mtf = out.get("mtf")
    if isinstance(mtf, dict) and (mtf.get("daily") or mtf.get("m15") or mtf.get("m5")):
        lines.append(_mtf_line(mtf))

    exp: List[str] = []
    sc = out.get("sector") or {}
    if sc.get("ok") and sc.get("alpha_pct") is not None:
        exp.append(f"板块α{sc.get('alpha_pct')}%")
    fd = out.get("flow_driver") or {}
    if fd.get("ok"):
        c = fd.get("counts") or {}
        exp.append(f"资金20日形态 推进{c.get('推进', 0)}/承接{c.get('承接', 0)}/"
                   f"惜售{c.get('惜售', 0)}/共振{c.get('共振', 0)}")
    cp = out.get("chip") or {}
    if cp.get("ok"):
        exp.append(f"获利{cp.get('profit_ratio_pct')}")
    sp = out.get("stop_pressure") or {}
    if sp.get("n"):
        exp.append(f"离场压力{sp['n']}条")
    if exp:
        lines.append("经验事实：" + "；".join(exp))
    if out.get("missing"):
        lines.append("缺口：" + "；".join(str(m) for m in out["missing"][:3]))
    return "\n".join(lines)


class AnalyzerResult(dict):
    """dict 子类：代码按 dict 用，转 str 出诊断摘要。"""

    def __str__(self) -> str:
        return _render(self)

    __repr__ = __str__


_BARS_CACHE_TTL_S = 60.0
_BARS_CACHE: Dict[str, Any] = {}
_BARS_CACHE_LOCK = threading.Lock()


def _bars_for(code: str) -> List[Dict[str, Any]]:
    """日线唯一取数口：优先复用 deep 暂存的同一份（同刻同源，见 `_deep.take_bars`），
    否则带 60s 进程缓存拉取——同一票不再拉两遍日线。"""
    from ._deep import take_bars
    b = take_bars(code)
    if b:
        return b
    now = time.time()
    with _BARS_CACHE_LOCK:
        one = _BARS_CACHE.get(code)
        if one and now - one["t"] < _BARS_CACHE_TTL_S:
            return one["bars"]
    from ._deps import fetch_kline
    bars = fetch_kline(code, days=_bars_days()) or []
    with _BARS_CACHE_LOCK:
        if len(_BARS_CACHE) >= 64:
            for _k in [k for k, v in _BARS_CACHE.items() if now - v["t"] >= _BARS_CACHE_TTL_S]:
                _BARS_CACHE.pop(_k, None)
        _BARS_CACHE[code] = {"t": now, "bars": bars}
    return bars


def _safe_call(name: str, fn, missing: List[str]):
    """并发取证单任务包装：异常写 missing 留痕并返回 (name, None)，不炸整轮。"""
    try:
        return name, fn()
    except Exception as e:
        missing.append(f"{name}: {type(e).__name__}: {e}")
        return name, None


def _run_wave(tasks: Dict[str, Any], missing: List[str], deadline: float) -> Dict[str, Any]:
    """收尾并发波：依赖已满足的网络任务一线程一任务并发跑；超时留痕口径与 _gather 一致。"""
    out: Dict[str, Any] = {}
    if not tasks:
        return out
    futs: Dict[Any, str] = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(tasks)))
    try:
        futs = {ex.submit(_safe_call, name, fn, missing): name for name, fn in tasks.items()}
        for fut in as_completed(futs, timeout=deadline):
            name, val = fut.result()
            out[name] = val
    except Exception as e:
        missing.append(f"收尾并发: {type(e).__name__}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for fut, name in futs.items():
        if name in out:
            continue
        if fut.done():
            try:
                _, val = fut.result()      # 已完成但未迭代到的按真实结果收，不误记超时
                out[name] = val
                continue
            except Exception:
                pass
        missing.append(f"{name}: 超时>{deadline:g}s 未返回")
    return out


def _gather(code: str, deep: bool) -> Tuple[Dict[str, Any], List[str]]:
    """并发取证：deep 证据基座 + 板块 + 大盘状态 + 解禁日历 + 八项补证事实（deep=True 加龙虎榜）。

    日线不在并发清单里：deep 内部已经拉过一份并暂存（`_deep.take_bars`），
    analyze 事后复用，同一票不再拉两遍。
    任一源失败/超时都进 missing 留痕，不吞成「没有」；deep 失败是致命缺口，
    由调用方转成「未知」而不是伪造空证据。
    """
    missing: List[str] = []
    out: Dict[str, Any] = {}
    try:
        from app.utils.trading_calendar import current_data_day
        target_day = str(current_data_day() or "")[:10]
    except Exception as e:
        target_day = ""
        missing.append(f"取数目标日: {type(e).__name__}: {e}")

    def _t_deep():
        from ._deep import deep_analyze
        return deep_analyze(code, limit=1, with_flow=True, with_patterns=True,
                            with_mtf=bool(deep))

    def _t_sector():
        from ...tools.finance.sector_analysis_tools import get_stock_sector_info
        return get_stock_sector_info(code)

    def _t_market():
        from ._deps import _market_state
        return _market_state()

    def _t_lockup():
        from ...tools.finance.signal_tools import get_lockup_expiry
        return get_lockup_expiry(code, forward_days=60)

    def _t_dragon():
        from ...tools.finance.dragon_tools import get_dragon_tiger
        return get_dragon_tiger(codes=code, detail=True)

    def _t_chip():
        from ._extra_facts import build_chip
        return build_chip(code)

    def _t_flow_rhythm():
        from ._extra_facts import build_flow_rhythm
        return build_flow_rhythm(code)

    def _t_intel():
        from ._extra_facts import build_intel
        return build_intel(code)

    def _t_capital():
        from ._extra_facts import build_capital
        return build_capital(code)

    def _t_intraday():
        from ._extra_facts import build_intraday
        return build_intraday(code, target_day,
                              _env_int("QD_STOCK_ANALYZER_OR_MINUTES", 15))

    def _t_multiframe():
        from ._extra_facts import build_multiframe
        return build_multiframe(code, target_day)

    def _t_valuation():
        from ._extra_facts import build_valuation
        return build_valuation(code)

    def _t_flow_daily():
        from ._extra_facts import build_flow_daily
        return build_flow_daily(code)

    tasks = {"deep": _t_deep, "sector": _t_sector, "market": _t_market, "lockup": _t_lockup,
             "chip": _t_chip, "flow_rhythm": _t_flow_rhythm,
             "intel": _t_intel, "capital": _t_capital,
             "intraday": _t_intraday, "multiframe": _t_multiframe,
             "valuation": _t_valuation, "flow_daily": _t_flow_daily}
    if deep:
        tasks["dragon"] = _t_dragon

    futs: Dict[Any, str] = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(tasks)))
    try:
        futs = {ex.submit(_safe_call, name, fn, missing): name for name, fn in tasks.items()}
        for fut in as_completed(futs, timeout=_DEADLINE_S):
            name, val = fut.result()
            out[name] = val
    except Exception as e:
        missing.append(f"并发取数: {type(e).__name__}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for fut, name in futs.items():
        if name in out:
            continue
        if fut.done():
            try:
                _, val = fut.result()      # 已完成但未迭代到的按真实结果收，不误记超时
                out[name] = val
                continue
            except Exception:
                pass
        missing.append(f"{name}: 超时>{_DEADLINE_S}s 未返回")
    return out, missing


def _aux_field(name: str, fn, missing: List[str]) -> Dict[str, Any]:
    """补充事实的失败留痕口：异常写进 missing，不静默吞断也不伪造空事实。"""
    try:
        return fn()
    except Exception as e:
        missing.append(f"{name}: {type(e).__name__}: {e}")
        return {"ok": False, "reason": f"{name} 取数失败"}


def analyze(code: str, deep: bool = False,
            prev_horizons: Optional[Dict[str, Any]] = None) -> dict:
    """对单只 A 股做 T+1~T+20 全周期多维诊断（deep 为证据基座）。

    输出分周期方向（看多/看空/中性/证据不足/未知）、置信度、触发/证伪条件、
    经 8 类方法交叉验证的 1-2 个支撑位与压力位（含演算痕迹）、板块/概念涨跌幅
    修正（β/α 拆分）与多维投票依据。五档逐档计算保留，horizons 只输出 T+1/T+3/T+5
    三档，长档由 trend_outlook 承载；brief 是照抄即可的一句话骨架。

    Args:
        code: 6 位股票代码
        deep: True 追加慢速源（多周期结构 + 龙虎榜席位），默认 False
        prev_horizons: 可选，上一轮同票三档结论（{h: {dir, stop}}），传入时产出
            consistency 比对；缺省不触发。不读库、不写库，由调用方按需提供。

    Returns:
        report_guide（报告写作边界）/code/name/as_of/regime/market/session（盘前/盘中/盘后）；quote/tech/
        prediction/patterns/flow/seal/sector（板块涨跌 + α 修正）/events（解禁日历）/
        market_outlook（大盘次日涨概率）/cap_regime（弱市大流通盘、强市小盘的分层事实）/
        board_rank（板块内排名与龙头/跟风）/trend_outlook（长档纯日线趋势）/
        chip（筹码）/flow_rhythm（资金节奏）/intel（情报）/capital（融资与基本面）/
        intraday（日内结构）/multiframe（多周期方向）/valuation（估值快照）/
        flow_daily（日频资金）/flow_driver（20日内资金×涨幅形态）九块补证事实，均不进七维投票/
        execution_core（全自动四要素：买点/卖点/预计持仓时间/持仓风险收益比，条件触发参考价非指令）/
        brief（三档+长档的一句话写作骨架）/levels（supports/resists/checks 演算痕迹）/
        horizons 三档（T+1/T+3/T+5），每档 {h, days, dir,
        p_up(仅T+1), confidence, agree, votes, basis, unknown, trigger, invalidate,
        reason}；传入 prev_horizons 时加 consistency（方向/止损较上一轮的变化事实）。
        evidence（证据完整度）与 missing（数据缺口）。方向取值：看多 / 看空 /
        中性 / 证据不足 / 未知。
        证据纪律：缺数答「未知」，孤证或冲突答「证据不足」，二者都不是看空。
    """
    code = str(code or "").strip()
    if not code:
        return AnalyzerResult({"error": "code 不能为空"})

    gathered, missing = _gather(code, deep=bool(deep))
    deep_res = gathered.get("deep") or {}
    card = (deep_res.get("analyzed") or [None])[0]
    if not isinstance(card, dict):
        reason = "；".join(str(e) for e in (deep_res.get("errors") or [])[:2]) or "deep 基座未返回证据卡"
        return AnalyzerResult({
            "report_guide": _report_guide(),
            "code": code, "error": f"证据装配失败：{reason}",
            "missing": missing + list(deep_res.get("missing") or []),
            "horizons": [{"h": h, "days": _days(h), "dir": "未知", "confidence": 0.0,
                          "reason": "证据装配失败，无法推演", "trigger": "", "invalidate": ""}
                         for h in _SHORT_HORIZONS],
        })

    missing = missing + [str(m) for m in (deep_res.get("missing") or [])] + \
              [str(m) for m in (card.get("missing") or [])]
    for err in (deep_res.get("errors") or []):
        missing.append(f"deep:{err}")

    extra: Dict[str, Dict[str, Any]] = {}
    for name in ("chip", "flow_rhythm", "intel", "capital",
                 "intraday", "multiframe", "valuation", "flow_daily"):
        val = gathered.get(name)
        if isinstance(val, dict):
            extra[name] = val
            if not val.get("ok") and val.get("reason"):
                missing.append(f"{name}: {val['reason']}")
        else:
            extra[name] = {"ok": False, "reason": f"{name} 未取证"}

    tech = card.get("tech") or {}
    flow = card.get("flow") or {}
    rt = card.get("rt") or {}
    seal = card.get("seal") or {}
    patterns = card.get("patterns") or []
    pred = {
        "ok": card.get("p_up") is not None,
        "p_up": card.get("p_up"),
        "exp_ret_bp": card.get("exp_ret_bp"),
        "model": card.get("pred_model"),
        "suggest_dir": card.get("suggest_dir"),
    }
    if not pred["ok"]:
        pred["reason"] = "标定分缺失"

    bars = _bars_for(code)
    if not bars:
        missing.append("bars: 日线缺失")
    chg = rt.get("change_pct")
    if not isinstance(chg, (int, float)) or isinstance(chg, bool):
        chg = None
    if chg is None:
        missing.append("chg: 当日涨跌缺数（不回退昨日日线口径，板块α修正按未知处理）")

    levels = _levels.build_levels(bars, code, card.get("name") or "", tech,
                                  close=tech.get("close"))
    if not levels.get("ok"):
        missing.append(f"levels: {levels.get('reason', '价位推演失败')}")

    sector = _sector_corr(gathered.get("sector"), code,
                          chg if isinstance(chg, (int, float)) else None)
    if not sector.get("ok"):
        missing.append(f"sector: {sector.get('reason', '板块数据缺失')}")
    guards = _move_guards(sector)

    events = _events_of(gathered.get("lockup"), code)
    if not events.get("ok"):
        missing.append(f"events: {events.get('reason', '解禁日历缺失')}")

    market = gathered.get("market") or {}
    vol5_ratio: Optional[float] = None
    if len(bars) >= 25:
        v5 = [b.get("volume") or 0 for b in bars[-5:]]
        v20 = [b.get("volume") or 0 for b in bars[-25:-5]]
        if sum(v20) > 0:
            vol5_ratio = round((sum(v5) / 5) / (sum(v20) / 20), 2)

    zt_history = []
    if bars:
        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        norm = [{"date": str(b.get("time") or "")[:10], "open": _num(b.get("open")),
                 "high": _num(b.get("high")), "low": _num(b.get("low")),
                 "close": _num(b.get("close")), "volume": _num(b.get("volume"))}
                for b in bars[-30:]]
        zt_history = _inner.zt_days(norm, code, card.get("name") or "", 8)

    miss_text = "；".join(missing)
    patterns_ok = "形态识别" not in miss_text and "日线缺失" not in miss_text

    risk_flags = [str(r) for r in (card.get("rt_risks") or []) + (card.get("flow_risks") or [])]
    ctx = {
        "tech": tech, "flow": flow, "pred": pred, "patterns": patterns,
        "patterns_ok": patterns_ok, "events": events,
        "seal": seal, "sc": sector, "guards": guards, "levels": levels,
        "vol5_ratio": vol5_ratio, "chg": chg if isinstance(chg, (int, float)) else None,
        "risk_flags": risk_flags, "bars_n": len(bars),
    }

    evidence, have, counted = _evidence(card, sector, missing)
    cap_map = _conf_cap()
    horizons_all = [_horizon_view(h, ctx, cap_map.get(h, 0.5), evidence) for h in _HORIZONS]
    horizons = [v for v in horizons_all if v["h"] in _SHORT_HORIZONS]

    market_model = pred.get("model") if isinstance(pred.get("model"), dict) else {}
    market_p_up = market_model.get("market_p_up")
    market_outlook = {
        "ok": market_p_up is not None,
        "p_up": market_p_up,
        "source": "watchlist.predict.market_p_up",
        "caliber": "大盘次日涨概率；描述性事实，不进七维投票，也不外推到 T+3 及以后",
    }
    if not market_outlook["ok"]:
        market_outlook["reason"] = "大盘预测分缺失"
    price = rt.get("price") or tech.get("close")
    wave = _run_wave({
        "flow_driver": lambda: _extra_facts.build_flow_driver(code, bars),
        "cap_regime": lambda: _cap_regime.build(code, price, market),
        "board_rank": lambda: _board_rank.build(code, sector),
        "trend_outlook": lambda: _trend.build(bars),
    }, missing, _DEADLINE_S)
    extra["flow_driver"] = wave.get("flow_driver") or {"ok": False, "reason": "flow_driver 未取证"}
    cap_regime = wave.get("cap_regime") or {"ok": False, "reason": "cap_regime 未取证"}
    board_rank = wave.get("board_rank") or {"ok": False, "reason": "board_rank 未取证"}
    trend_outlook = wave.get("trend_outlook") or {"ok": False, "reason": "trend_outlook 未取证"}
    execution_core = _execution_core(horizons, levels, price)
    brief = _brief_view(horizons, trend_outlook)
    consistency = _consistency_view(horizons, prev_horizons)
    stop_pressure = _aux_field(
        "stop_pressure",
        lambda: _stop_pressure(tech, flow, sector, market, levels,
                              extra["chip"], extra["flow_rhythm"],
                              extra["intel"], extra["capital"],
                              extra["intraday"], extra["flow_daily"]), missing)

    session = _session()
    out = {
        "report_guide": _report_guide(),
        "brief": brief,
        "execution_core": execution_core,
        "code": code,
        "name": card.get("name") or "",
        "as_of": rt.get("time") or levels.get("as_of"),
        "regime": _mood_label(market),
        "market": {k: market.get(k) for k in ("market_mood", "market_mood_score",
                                              "stock_mood", "stock_mood_score",
                                              "cautious", "fund_flow_days")
                   if market.get(k) is not None},
        "market_outlook": market_outlook,
        "chip": extra["chip"],
        "flow_rhythm": extra["flow_rhythm"],
        "intel": extra["intel"],
        "capital": extra["capital"],
        "intraday": extra["intraday"],
        "multiframe": extra["multiframe"],
        "valuation": extra["valuation"],
        "flow_daily": extra["flow_daily"],
        "flow_driver": extra["flow_driver"],
        "cap_regime": cap_regime,
        "board_rank": board_rank,
        "trend_outlook": trend_outlook,
        "stop_pressure": stop_pressure,
        "quote": {"price": rt.get("price") or tech.get("close"),
                  "change_pct": chg, "limit_up": rt.get("limit_up"),
                  "limit_band": rt.get("limit_band"), "turnover_pct": rt.get("turnover_pct")},
        "tech": tech,
        "prediction": pred,
        "patterns": patterns,
        "flow": flow,
        "seal": seal,
        "zt_history": zt_history,
        "sector": sector,
        "events": events,
        "session": session,
        "session_note": _SESSION_NOTE.get(session, ""),
        "levels": levels,
        "horizons": horizons,
        "evidence": evidence,
        "evidence_dims": {"have": have, "counted": counted},
        "risks": _risks(card, tech, sector, events),
        "missing": missing,
        "basis_note": ("confidence = 档位上限 × 证据完整度 × 一致度（|同向维-反向维|/有效维）；"
                       "仅 T+1 的 p_up 经标定，其余档只给方向不给概率。"
                       "方向纪律：≥2 维同向且差值 ≥2 才给方向；孤证/冲突答「证据不足」；"
                       "缺数答「未知」。价位只用经 8 类方法交叉验证的支撑/压力（见 levels.checks）。"
                       "horizons 只列 T+1/T+3/T+5，长档见 trend_outlook；brief 是照抄骨架，"
                       "跨轮变化见 consistency；invalidate 为最近止损参考、last_defense 为最后防线，"
                       "stop_pressure 是离场压力事实条数（非买卖指令）；"
                       "九块补证事实只陈述、不投票；筹码价位与 levels 不同源、不合并排序，"
                       "快照与日频资金两口径并列不合并。20 日以上长线资金不进方向判断，"
                       "flow_driver 只分解 20 日内价量资金形态、四档拆单本地不可得、"
                       "不推庄家持仓成本与利润空间；execution_core 四要素是条件触发参考价、"
                       "非买卖指令。"),
    }
    if consistency is not None:
        out["consistency"] = consistency
    if deep and gathered.get("dragon") is not None:
        out["dragon"] = gathered.get("dragon")
    if isinstance(card.get("mtf"), dict):
        out["mtf"] = _mtf_guard.guard(card["mtf"], code, bars)
        if out["mtf"].get("stale"):
            missing.append(str(out["mtf"].get("stale_reason") or "多周期跨日"))
    return AnalyzerResult(out)


def deep_cards(codes: str, limit: int = 8, with_flow: bool = True,
               with_patterns: bool = True, with_mtf: bool = False,
               budget: int = None) -> dict:
    """deep 层地基：批量装配全部证据维度（rank 行 + 满卡），按 p_up 降序返回。

    与 `analyze` 的分工：`analyze` 是**单票结论生成器**（给方向/触发/证伪/价位）；
    本函数是它下面的**证据地基**——只取事实、不做判断、不给方向，供上层自行加权。
    逻辑逐字来自 market-screener 的 deep 层（见 `_deep.py` 的复制说明）。

    Args:
        codes: 逗号分隔代码，如 "600519,000858"
        limit: 最多几只（默认 8）
        with_flow: 是否取资金流（口径=量价方向净额，非主力）
        with_patterns: 是否识别 K 线形态
        with_mtf: 是否取多周期结构（需分钟源，更慢）
        budget: 整份返回值的字符预算；缺省 QD_SCREENER_DEEP_BUDGET

    Returns:
        dict: analyzed_rank[]（全部票的横向可比字段，不截断）/
            analyzed[]（满卡，按预算截断）/ report_guide / strategy / missing / errors。
            两档同序（p_up 降序、无预测沉底），故满卡即证据最强的 N 只。
    """
    from ._deep import deep_analyze as _deep_analyze
    return _deep_analyze(codes, limit=limit, with_flow=with_flow,
                         with_patterns=with_patterns, with_mtf=with_mtf,
                         budget=budget)
