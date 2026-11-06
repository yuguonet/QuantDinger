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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ...log import logger
from . import _inner, _levels

#: 全周期档位（顺序即输出顺序）；天数不在此硬编码，取 `label_spec.HOLDS`
_HORIZONS: Tuple[str, ...] = ("T+1", "T+3", "T+5", "T+10", "T+20")

_ANALYZE_REPORT_GUIDE = {
    "OUTPUT_FORM": (
        "按当前数据时点先给结论，再分列事实、推导、判断、风险与未知项；"
        "使用 horizons 的五档方向、触发条件和证伪条件。用户只要求分析时，不给命令式买卖或仓位指令；"
        "价位只作为条件情景，不保证买卖结果。"
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

_BARS_DAYS_DEFAULT = 120

#: 并发取证总等待上限：deep 内部还有一层并发，这里给宽裕值防拖死整轮
_DEADLINE_S = 45.0

#: 参与证据完整度统计的维度；missing 留痕词用于交叉核对「拿到没有」
_DIMS: Tuple[str, ...] = ("rt", "tech", "prediction", "patterns", "flow", "sector", "zt")
_DIM_MISS_TOKEN: Dict[str, str] = {
    "rt": "实时行情", "tech": "技术指标", "prediction": "预测",
    "patterns": "形态识别", "flow": "资金流", "sector": "板块",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


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
    return _env_int("QD_STOCK_ANALYZER_BARS_DAYS", _BARS_DAYS_DEFAULT)


def _days(h: str) -> int:
    """档位 → 交易日天数（真源 `label_spec.HOLDS`，封顶 T+20≈1M）。"""
    from ...chain.label_spec import HOLDS

    return int(HOLDS.get(h) or 0)


def _days_until(date_str: Any) -> Optional[int]:
    """距今天数（自然日）；日期认不出返回 None（不猜）。"""
    try:
        from datetime import date as _date
        return (_date.fromisoformat(str(date_str)[:10]) - _date.today()).days
    except (ValueError, TypeError):
        return None


def _events_of(raw: Any, code: str) -> Dict[str, Any]:
    """事件维（目前唯一有日历的事件源：限售解禁）。只取事实，不做处置建议。"""
    one = ((raw or {}).get("data") or {}).get(code)
    if one is None:
        vals = [v for v in ((raw or {}).get("data") or {}).values() if isinstance(v, dict)]
        if len(vals) == 1:
            one = vals[0]
    one = one or {}
    if not isinstance(one, dict) or one.get("error") or "upcoming" not in one:
        return {"ok": False, "reason": str((one or {}).get("error") or "解禁日历缺失")}
    up = [u for u in (one.get("upcoming") or []) if isinstance(u, dict) and u.get("date")]
    return {"ok": True, "lockup": up[:4], "upcoming_count": one.get("upcoming_count", len(up))}


def _lockup_bear(events: Dict[str, Any], window_days: int) -> Optional[bool]:
    """解禁供给压力（tape 维的空证据条件）：窗口内有占比 ≥1% 的解禁 ⇒ True。

    阈值 1% 为经验阀（小比例解禁的供给冲击不足以作空证据）；日历缺失 ⇒ None（缺数）。
    """
    if not events.get("ok"):
        return None
    for u in events.get("lockup") or []:
        dd = _days_until(u.get("date"))
        try:
            ratio = float(u.get("ratio") or 0)
        except (TypeError, ValueError):
            ratio = 0.0
        if dd is not None and 0 <= dd <= window_days and ratio >= 1.0:
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

    from ....utils.trading_calendar import CalendarUnavailable, is_trading_day

    dt = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        if not is_trading_day(dt.strftime("%Y-%m-%d")):
            return "closed"
    except CalendarUnavailable:
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
    try:
        ms = float(market.get("market_mood_score", 50) or 50)
    except (TypeError, ValueError):
        ms = 50.0
    if mood in ("弱势", "偏弱") or ms < 40:
        return "weak"
    if mood in ("偏强",) or ms >= 70:
        return "strong"
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
    one = ((raw or {}).get("data") or {}).get(code)
    if one is None:
        vals = [v for v in ((raw or {}).get("data") or {}).values() if isinstance(v, dict)]
        if len(vals) == 1:
            one = vals[0]      # 单票取证但键名漂移（前缀/去前缀），唯一值即目标票
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
    out["label"] = "自强" if alpha >= 1.5 else ("自弱" if alpha <= -1.5 else "跟板块")
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
    if chg is not None and chg > 0 and alpha is not None and alpha <= -1.5:
        up_ok = False
        notes.append(f"板块α修正：今日 +{chg}% 系板块带动（板块 {sc.get('bench_chg_pct')}%，α={alpha}%，自弱），"
                     "上涨类证据作废")
    if chg is not None and chg < 0 and alpha is not None and alpha >= 1.5:
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
    p = float(pred.get("p_up") or 0.5)
    vote = 1 if p >= 0.55 else (-1 if p <= 0.45 else 0)
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

    if h in ("T+1", "T+3"):
        bull = [("贴已验证支撑", None if (sup_d is None or sup_v is None) else (sup_v and sup_d >= -3.0)),
                ("均线多头", stack),
                ("未超买", None if rsi is None else rsi < 75)]
        bear = [("跌破MA20超3%", None if dist20 is None else dist20 < -3),
                ("追高过热", None if (dist20 is None and rsi is None) else ((dist20 or 0) >= 10 or (rsi or 0) >= 75)),
                ("远离支撑", None if sup_d is None else sup_d < -5)]
    elif h == "T+5":
        bull = [("均线多头", stack), ("20日位置≥50", None if pos20 is None else pos20 >= 50),
                ("乖离≤10%", None if dist20 is None else dist20 <= 10)]
        bear = [("均线非多头", _neg(stack)), ("20日位置≤40", None if pos20 is None else pos20 <= 40),
                ("价格在MA20下方", None if dist20 is None else dist20 < 0)]
    elif h == "T+10":
        bull = [("均线多头", stack), ("20日位置≥60", None if pos20 is None else pos20 >= 60)]
        bear = [("均线非多头", _neg(stack)), ("20日位置≤30", None if pos20 is None else pos20 <= 30),
                ("MA20下方超5%", None if dist20 is None else dist20 <= -5)]
    else:
        bull = [("均线多头", stack), ("20日位置≥60", None if pos20 is None else pos20 >= 60),
                ("乖离0~15%", None if dist20 is None else 0 <= dist20 <= 15)]
        bear = [("均线非多头", _neg(stack)), ("MA20下方超8%", None if dist20 is None else dist20 <= -8)]
    vote, why, un = _weigh(bull, bear)
    return {"vote": vote, "why": why, "applicable": True}


def _v_volume(h: str, tech: Dict[str, Any], vol5_ratio: Optional[float],
              chg: Optional[float], up_ok: bool, down_ok: bool) -> Dict[str, Any]:
    """volume 维：量能及其与涨跌的配合（vr_rel=对大盘校准口径，优先于绝对量比）。"""
    vr = tech.get("vol_ratio")
    vr_rel = tech.get("vr_rel")

    def _strong_vol() -> Optional[bool]:
        if vr_rel is not None:
            return vr_rel >= 2
        return None if vr is None else vr >= 1.5

    if h in ("T+1", "T+3"):
        up = None if chg is None else chg > 0
        down = None if chg is None else chg < 0
        bull = [("放量上涨", None if (up is None or _strong_vol() is None) else (up and _strong_vol() and up_ok))]
        bear = [("涨无量（量价背离)", None if (up is None or vr is None) else (up and vr < 0.85)),
                ("放量下跌", None if (down is None or _strong_vol() is None) else (down and _strong_vol() and down_ok))]
    else:
        bull = [("5日量能抬升", None if vol5_ratio is None else vol5_ratio >= 1.2)]
        bear = [("5日量能萎缩", None if vol5_ratio is None else vol5_ratio <= 0.75)]
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
                ("资金强于大盘", None if rel is None else rel >= 1)]
        bear = [("5日净流出", None if n5 is None else n5 < 0),
                ("资金弱于大盘", None if rel is None else rel <= -1)]
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
    if h in ("T+1", "T+3"):
        bull = [("强于大盘", None if rs0 is None else rs0 >= 1)]
        bear = [("弱于大盘", None if rs0 is None else rs0 <= -2)]
        why_head = f"超额rs_d0={rs0}%"
    else:
        bull = [("5日强于大盘", None if rs5 is None else rs5 >= 2)]
        bear = [("5日弱于大盘", None if rs5 is None else rs5 <= -2)]
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
    vote = 1 if alpha >= 1.5 else (-1 if alpha <= -1.5 else 0)
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
                ("高位连板≥2", None if zt_streak is None else zt_streak >= 2),
                ("近期解禁（≤20日，占比≥1%）", _lockup_bear(events, 20)),
                ("拉高出货线索", bool(pump))]
    else:
        bull = [("形态确认（≥2）", None if not patterns_ok else (len(patterns) >= 2 and up_ok))]
        bear = [("涨停透支（10日≥3次）", None if zt10 is None else zt10 >= 3),
                ("近期解禁（≤45日，占比≥1%）", _lockup_bear(events, 45)),
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
    """按周期挑锚定价位：短档用最近的，长档优先更粗的（ma20/ma60/lo60/枢轴）。"""
    rows = levels.get("supports" if side == "sup" else "resists") or []
    if not rows:
        return None
    if h in ("T+1", "T+3"):
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
                            "confidence": 0.0, "votes": {}, "basis": [], "unknown": [],
                            "trigger": "", "invalidate": "", "reason": ""}

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
    if dir_ == "看多":
        view["trigger"] = f"回踩 {sup_t} 不破且量能转强，或放量站上 {res_t}"
        view["invalidate"] = f"收盘跌破 {sup_t}"
    elif dir_ == "看空":
        view["trigger"] = f"反抽 {res_t} 不破且量能不济"
        view["invalidate"] = f"放量站上 {res_t}"
    else:
        view["trigger"] = f"放量站上 {res_t} 才看多"
        view["invalidate"] = f"收盘跌破 {sup_t} 转空"

    strength = agree if dir_ in ("看多", "看空") else 0.0
    view["confidence"] = round(cap * evidence * strength, 2)
    return view


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


def _risks(card: Dict[str, Any], tech: Dict[str, Any], sc: Dict[str, Any],
           events: Dict[str, Any]) -> List[str]:
    """客观风险标记：只罗列事实，不构成买卖建议。"""
    out: List[str] = [str(r) for r in (card.get("rt_risks") or []) + (card.get("flow_risks") or [])]
    if tech.get("is_20d_high"):
        out.append("处于20日最高位（追高风险）")
    if (tech.get("rsi14") or 0) >= 75:
        out.append(f"RSI{tech['rsi14']} 超买")
    if (tech.get("dist_ma20_pct") or 0) >= 15:
        out.append(f"离MA20已有 {tech['dist_ma20_pct']}%（乖离偏大）")
    if (tech.get("zt_streak") or 0) >= 2:
        out.append(f"{tech['zt_streak']}连板（次日分歧概率高）")
    if (tech.get("vol_ratio") or 0) >= 4:
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
        if c <= -3:
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
    return "多周期（分时口径，与日线 regime 不同义）：" + (body or "未知")


def _render(out: Dict[str, Any]) -> str:
    """给 LLM / 用户的诊断摘要：结论先行，四要素齐全，不给模糊措辞。"""
    if out.get("error"):
        return f"ERR: {out['error']}"
    sess = {"pre": "盘前", "intraday": "盘中", "post": "盘后",
            "closed": "非交易日", "unknown": "时段未知"}.get(out.get("session"), "")
    head = (f"{out.get('code', '')} {out.get('name', '')}｜市场情绪 {out.get('regime', 'unknown')}"
            f"｜证据完整度 {out.get('evidence')}" + (f"｜{sess}" if sess else ""))
    lines = [head]
    if out.get("missing"):
        lines.append("数据缺口：" + "；".join(str(m) for m in out["missing"][:4]))
    lines.append("")
    lines.append("| 周期 | 方向 | 置信度 | 触发条件 | 证伪条件 |")
    lines.append("|---|---|---|---|---|")
    for v in out.get("horizons") or []:
        p = f"P={v['p_up']}" if v.get("p_up") is not None else ""
        d = v["dir"] + (f"（{p}）" if p else "")
        note = v.get("reason") or ""
        lines.append(f"| {v['h']} | {d} | {v['confidence']} | {v['trigger'] or note} | {v['invalidate'] or '—'} |")

    lv = out.get("levels") or {}
    for side, label in (("supports", "支撑"), ("resists", "压力")):
        rows = lv.get(side) or []
        if rows:
            lines.append(f"{label}：" + " ｜ ".join(
                f"{r['px']}（{r.get('name')}，{'已验证' if r.get('verified') else '孤证'}，"
                f"有效测试{r.get('touches')}次"
                + (f"，失守{r.get('broken')}次" if r.get("broken") else "") + "）" for r in rows))
        else:
            lines.append(f"{label}：未知（无可推演价位）")

    sc = out.get("sector") or {}
    if sc.get("ok") and sc.get("industry_board"):
        lines.append(f"板块修正：行业「{sc.get('industry_board')}」{sc.get('industry_chg_pct')}%，"
                     f"概念均{sc.get('concept_chg_pct')}%，个股α={sc.get('alpha_pct')}%（{sc.get('label')}）")
    elif sc.get("ok"):
        lines.append(f"板块修正：行业板未命中（industry={sc.get('industry')}），"
                     f"基准=所属板块均值{sc.get('bench_chg_pct')}%，"
                     f"个股α={sc.get('alpha_pct')}%（{sc.get('label')}）")
    else:
        lines.append(f"板块修正：未知（{sc.get('reason', '板块涨跌幅缺失')}）")

    if isinstance(out.get("mtf"), dict):
        lines.append(_mtf_line(out["mtf"]))

    if out.get("basis_note"):
        lines.append("")
        lines.append(out["basis_note"])
    if out.get("session_note"):
        lines.append(out["session_note"])
    pats = [p.get("pattern") for p in (out.get("patterns") or []) if isinstance(p, dict)]
    if pats:
        lines.append("形态：" + "、".join(str(x) for x in pats[:5]))
    if out.get("risks"):
        lines.append("风险：" + "；".join(str(x) for x in out["risks"][:5]))
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
        _BARS_CACHE[code] = {"t": now, "bars": bars}
    return bars


def _gather(code: str, deep: bool) -> Tuple[Dict[str, Any], List[str]]:
    """并发取证：deep 证据基座 + 板块 + 大盘状态 + 解禁日历（deep=True 加龙虎榜）。

    日线不在并发清单里：deep 内部已经拉过一份并暂存（`_deep.take_bars`），
    analyze 事后复用，同一票不再拉两遍。
    任一源失败/超时都进 missing 留痕，不吞成「没有」；deep 失败是致命缺口，
    由调用方转成「未知」而不是伪造空证据。
    """
    missing: List[str] = []
    out: Dict[str, Any] = {}

    def _task(name: str, fn):
        try:
            return name, fn()
        except Exception as e:
            missing.append(f"{name}: {type(e).__name__}: {e}")
            return name, None

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

    tasks = {"deep": _t_deep, "sector": _t_sector, "market": _t_market, "lockup": _t_lockup}
    if deep:
        tasks["dragon"] = _t_dragon

    futs: Dict[Any, str] = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(tasks)))
    try:
        futs = {ex.submit(_task, name, fn): name for name, fn in tasks.items()}
        for fut in as_completed(futs, timeout=_DEADLINE_S):
            name, val = fut.result()
            out[name] = val
    except Exception as e:
        missing.append(f"并发取数: {type(e).__name__}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for fut, name in futs.items():
        if name not in out:
            missing.append(f"{name}: 超时>{_DEADLINE_S}s 未返回")
    return out, missing


def analyze(code: str, deep: bool = False) -> dict:
    """对单只 A 股做 T+1~T+20 全周期多维诊断（deep 为证据基座）。

    输出分周期方向（看多/看空/中性/证据不足/未知）、置信度、触发/证伪条件、
    经 8 类方法交叉验证的 1-2 个支撑位与压力位（含演算痕迹）、板块/概念涨跌幅
    修正（β/α 拆分）与多维投票依据。

    Args:
        code: 6 位股票代码
        deep: True 追加慢速源（多周期结构 + 龙虎榜席位），默认 False

    Returns:
        report_guide（报告写作边界）/code/name/as_of/regime/market/session（盘前/盘中/盘后）；quote/tech/
        prediction/patterns/flow/seal/sector（板块涨跌 + α 修正）/events（解禁日历）/
        levels（supports/resists/checks 演算痕迹）/horizons 五档，每档 {h, days, dir,
        p_up(仅T+1), confidence, agree, votes, basis, unknown, trigger, invalidate,
        reason}；evidence（证据完整度）与 missing（数据缺口）。方向取值：看多 / 看空 /
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
            "code": code, "error": f"证据装配失败：{reason}",
            "missing": missing + list(deep_res.get("missing") or []),
            "horizons": [{"h": h, "days": _days(h), "dir": "未知", "confidence": 0.0,
                          "reason": "证据装配失败，无法推演", "trigger": "", "invalidate": ""}
                         for h in _HORIZONS],
        })

    missing = missing + [str(m) for m in (deep_res.get("missing") or [])] + \
              [str(m) for m in (card.get("missing") or [])]
    for err in (deep_res.get("errors") or []):
        missing.append(f"deep:{err}")

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
    if chg is None:
        chg = tech.get("change_pct")

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
                for b in bars]
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
    horizons = [_horizon_view(h, ctx, cap_map.get(h, 0.5), evidence) for h in _HORIZONS]

    session = _session()
    out = {
        "report_guide": _ANALYZE_REPORT_GUIDE,
        "code": code,
        "name": card.get("name") or "",
        "as_of": rt.get("time") or levels.get("as_of"),
        "regime": _mood_label(market),
        "market": {k: market.get(k) for k in ("market_mood", "market_mood_score",
                                              "stock_mood", "stock_mood_score",
                                              "cautious", "fund_flow_days")
                   if market.get(k) is not None},
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
                       "缺数答「未知」。价位只用经 8 类方法交叉验证的支撑/压力（见 levels.checks）。"),
    }
    if deep and gathered.get("dragon") is not None:
        out["dragon"] = gathered.get("dragon")
    if isinstance(card.get("mtf"), dict):
        out["mtf"] = card["mtf"]
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
