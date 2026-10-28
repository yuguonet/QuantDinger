# -*- coding: utf-8 -*-
"""
market-screener/_helpers.py — 事实与证据层（不做选股决策）

2026-09-29 改版（v6）：
    旧版把「分策略筛选规则 / 排序 / 情绪分桶裁剪」硬编码在本模块，LLM 只剩格式化输出，
    自由度≈0，分析深度被单一技术打分封顶。本版职责收敛为三条：

    1. 取数与计算：日线、量价结构、资金流、指标快照、T+1 标定预测（app.watchlist.predict）
    2. 打客观标记：hard_exclusions() —— ST/退市、换手不足、价格带、涨停买不进、数据缺失
    3. 汇总证据：profile_candidates() 产出每只候选的「证据卡」，**不淘汰任何一只**

    决策（选谁、留几只、要不要折价、如何叙述）由 LLM 依据 SKILL.md 的分析框架完成。

设计红线：
    · 本模块禁止根据「策略偏好」剔除候选；只标注客观事实与不可执行项
   2026-10-20 分层：objective_preexclude / score_pool 是 run() 漏斗编排层的函数——
   前者只排除客观不可交易项（与 hard_exclusions 同一性质，用实时行情提前做），
   后者做双保险软门槛（p_up 地板+排名尾部+保底回捞）。偏好取舍仍归 LLM。
    · 所有回退必须留痕（missing / errors），不静默吞异常 —— 否则 LLM 会把数据缺口当成无信号
"""

import os
import threading
from datetime import date, datetime, time
from concurrent.futures import ThreadPoolExecutor

from ...log import logger
from typing import Any, Dict, List, Optional, Tuple

# 证据卡默认上限：候选池可达数百只，全量画像会撑爆上下文（trim 优先保证「有证据」而非「全」）
_PROFILE_LIMIT_DEFAULT = 20
# 漏斗打分资源护栏（2026-10-21 修「隐形截断」）：最多给多少只挂 p_up，是**取数
# 预算上限、不是选股口径**。旧值 _SCORE_BATCH=80 经 profile_candidates 的
# limit*2 窗口只给**池序前 160** 打分——宽池（扩词后 500+）下后排票永远没被
# 打过分，排序公平性失真（2026-10-21 用户裁定修此点）。
# 打分成本便宜：predict_next_day 是本地模型（吃已取的 bars，_market_klines 有
# 缓存），kline 走 DB 优先门面 ⇒ 护栏放宽到 600；env QD_SCREENER_SCORE_MAX
# 可调（阈值不走硬编码）。只有真超护栏才按 _objective_rank_key 让位。
_SCORE_MAX_DEFAULT = 600


def _score_max() -> int:
    """打分护栏（只数）：env 优先，回落模块缺省。"""
    raw = os.environ.get("QD_SCREENER_SCORE_MAX", "") or ""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _SCORE_MAX_DEFAULT
    return v if v > 0 else _SCORE_MAX_DEFAULT
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
        from ...tools.finance.data_tools import get_realtime_quote
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


def _num_or_none(v: Any) -> Optional[float]:
    """容错转 float；None/空串/非数 → None（缺失语义），不做 0.0 归一。

    与 _f 的分工：_f 适合「有缺省值」的数值字段；本函数用于 **0 与缺失语义
    不同的字段**（涨跌停价 / 涨跌幅）——契约上 0/-1 是「无涨跌幅限制」哨兵、
    None 是「源未给」（腾讯增补偶发失败，见 data_tools.get_realtime_quote），
    _f(None)=0.0 会把「未知」伪造成「0 价」→ _parse_limit_px 判 no_limit →
    封板票 buyable=True 进可买表（C1，2026-10-21；missing≠no_limit，
    见 references/limit-rules.md）。
    """
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


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


def rule_prefilter(candidates: List[Dict], min_turnover: float = 1.0,
                   min_list_days: int = 60) -> tuple:
    """代码规则初筛（2026-10-28）——**先规则、后画像**，禁止「搜索命中 1470 取前 10」。

    基础层（始终执行）：
      · **排除不活跃**：明确换手 < min_turnover%、停牌
      · **排除新股**：名称 N/C 开头、list_date 距今 < min_list_days、上市日期未知且
        标了 ipo/new 的（缺字段不杀——未知≠不合格）

    扩展层（池子仍过大时追加，防止二次硬截）：
      · ST/退市、价格带 [2,300]

    Returns:
        (kept, stats)：kept=通过的候选（**全量**，不截断）；
        stats={inactive, new_stock, st, price_band, total_in, total_kept}
    """
    stats = {"inactive": 0, "new_stock": 0, "st": 0, "price_band": 0,
             "total_in": len(candidates or [])}
    kept: List[Dict] = []
    today = datetime.now().date()
    # 扩展层阈值：全量 >200 时才加 ST/价格带（与 objective_preexclude 不重复计数语义不同）
    apply_ext = len(candidates or []) > 200

    for item in candidates or []:
        name = str(item.get("name") or "").upper()
        # ── 新股 ──
        if name.startswith("N") or name.startswith("C"):
            stats["new_stock"] += 1
            item["_rule_excluded"] = "新股"
            continue
        ld = str(item.get("list_date") or "").strip()
        if ld:
            try:
                d = datetime.strptime(ld[:10], "%Y-%m-%d").date()
                if (today - d).days < int(min_list_days):
                    stats["new_stock"] += 1
                    item["_rule_excluded"] = "次新"
                    continue
            except ValueError:
                pass

        # ── 不活跃 ──
        if item.get("suspended"):
            stats["inactive"] += 1
            item["_rule_excluded"] = "停牌"
            continue
        trn = _f(item.get("turnover_pct"))
        if 0 < trn < float(min_turnover):
            stats["inactive"] += 1
            item["_rule_excluded"] = f"换手{trn:.1f}%不活跃"
            continue

        if apply_ext:
            if "ST" in name or "退" in name:
                stats["st"] += 1
                item["_rule_excluded"] = "ST/退市"
                continue
            price = _f(item.get("price") or item.get("close"))
            if price and (price < 2 or price > 300):
                stats["price_band"] += 1
                item["_rule_excluded"] = "价格带"
                continue

        kept.append(item)

    stats["total_kept"] = len(kept)
    return kept, stats


def code_narrow(candidates: List[Dict], main_themes: List = None,
                max_n: int = 40) -> tuple:
    """代码收窄（2026-10-28 v2）：宽池 400+ → **≤max_n**，对齐「最小风险、最大涨幅」。

    问题复盘（实盘 4 只结论偏「指标平铺」）：旧分层只看题材/多源/换手，
    没挡「买不进的封板」和「追高/无空间」，评分层再砍一半 ⇒ 进 deep 的质量差。

    分层（计数，不编造分数；p_up 仍留给 score_pool）：
      - **买不进**（limit_up / 跌停）直接不进候选（硬剔，计 unbuyable）
      - tier0 题材命中 +（贴支撑/多源/健康换手）且 **非极端追高**
      - tier1 题材命中 或（贴支撑 + 健康换手）
      - tier2 健康换手(2~15%) 且 非极端追高
      - tier3 其余
    「极端追高」= 当日涨幅 >7% 或（有 20 日位且 >=95）。
    同层：换手靠近 5% 优先；涨幅更低优先（更少追高）；再输入序。

    Returns:
        (kept, stats)
    """
    themes = [t[0] for t in (main_themes or []) if isinstance(t, (list, tuple)) and t]
    scored = []
    stats = {"unbuyable": 0, "total_in": len(candidates or []),
             "by_tier": {0: 0, 1: 0, 2: 0, 3: 0}}

    def _chg(it):
        try:
            return float(it.get("change_pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _unbuyable(it) -> bool:
        """封板判定：limit_up 可能是**布尔**也可能是**涨停价**（fill_quote 回填的是价）。"""
        if it.get("limit_locked") is True or it.get("limit_down") is True:
            return True
        lu = it.get("limit_up")
        if lu is True:
            return True
        if isinstance(lu, (int, float)) and not isinstance(lu, bool) and lu > 0:
            price = _f(it.get("price") or it.get("close"))
            if price and price >= float(lu) - 0.01:
                return True
        return False

    for i, item in enumerate(candidates or []):
        # 买不进：封板/跌停不留名额（2026-10-28 实盘：40 里 24 只 hard 白占）
        if _unbuyable(item):
            stats["unbuyable"] += 1
            item["_rule_excluded"] = "买不进"
            continue

        theme = item.get("theme")
        hit = theme.get("hit") if isinstance(theme, dict) else _theme_hit(item, themes)["hit"]
        source = str(item.get("source") or "")
        multi = "+" in source
        trn = _f(item.get("turnover_pct"))
        chg = _chg(item)
        pos = item.get("pos_in_20d_pct")
        try:
            pos_f = float(pos) if pos is not None else None
        except (TypeError, ValueError):
            pos_f = None
        # 贴支撑线索：结构扫描带上的 reason / 已有 tech
        tech = item.get("tech") or {}
        near = bool((tech.get("support") or {}).get("near_support")
                    or tech.get("near_support")
                    or "均线" in str(item.get("reason") or ""))
        extreme = chg > 7.0 or (pos_f is not None and pos_f >= 95)
        healthy = 2.0 <= trn <= 15.0

        if hit and (near or multi or healthy) and not extreme:
            tier = 0
        elif hit or (near and healthy):
            tier = 1
        elif healthy and not extreme:
            tier = 2
        else:
            tier = 3
        stats["by_tier"][tier] = stats["by_tier"].get(tier, 0) + 1
        dist_trn = abs(trn - 5.0) if trn > 0 else 99.0
        # 涨幅越低越不追高；同层稳定
        scored.append((tier, dist_trn, chg, i, item))

    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))

    # ── 板性分散（2026-10-28）：创业板/科创扎堆直接降权，避免「三只全是 300/301」──
    def _board(code: str) -> str:
        c = str(code or "")
        if c.startswith(("300", "301")):
            return "创业板"
        if c.startswith("688") or c.startswith("689"):
            return "科创板"
        if c.startswith("60"):
            return "沪主板"
        if c.startswith(("00", "30")):
            return "深主板"
        return "其他"

    # 每板上限：创业板/科创各 ≤8/40，防单一高波动属性吃满名额
    board_cap = {"创业板": 8, "科创板": 4}
    board_n: Dict[str, int] = {}
    kept = []
    overflow = []
    for tier, _d, _c, _i, item in scored:
        b = _board(item.get("code"))
        cap = board_cap.get(b)
        n = board_n.get(b, 0)
        if cap is not None and n >= cap:
            overflow.append(item)
            continue
        board_n[b] = n + 1
        kept.append(item)
        if len(kept) >= max(1, int(max_n)):
            break
    # 名额有空再回填溢出（保持 ≤max_n）
    if len(kept) < max(1, int(max_n)):
        for item in overflow:
            kept.append(item)
            if len(kept) >= max(1, int(max_n)):
                break

    stats["total_kept"] = len(kept)
    stats["max_n"] = int(max_n)
    stats["board_n"] = board_n
    return kept, stats


def _board_of(code: str) -> str:
    c = str(code or "")
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith("688") or c.startswith("689"):
        return "科创板"
    if c.startswith("60"):
        return "沪主板"
    if c.startswith(("00", "30")):
        return "深主板"
    return "其他"


def enforce_buy_quality(profiles: List[Dict], main_themes: List = None,
                        max_n: int = 12, max_per_board: int = 3,
                        min_sr_gap_pct: float = 8.0) -> tuple:
    """进 deep /「买什么」质量闸（2026-10-28 三条硬约束）。

    1. **题材或结构内空间**：`theme.hit` 或（**结构未坏** 且 `sr_gap_pct >= min_sr_gap_pct`）。
       「结构未坏」= 贴支撑 / 多头 / 站上 MA20 / 弱转强，且 **非深跌无承接**
       （距 MA20 < -5% 且 20 日跌 >15% = 超跌反弹，不算趋势空间）
    2. **高波动板性上限**：创业板/科创板各 ≤ max_per_board
    3. T+1~T+5 置信由 LLM 落笔（quality_bar）

    Returns:
        (kept, stats)
    """
    themes = [t[0] for t in (main_themes or []) if isinstance(t, (list, tuple)) and t]
    passed = []
    stats = {"total_in": len(profiles or []), "no_theme_no_space": 0,
             "oversold_bounce": 0, "board_capped": 0, "by_board": {}}

    def _fnum(v, default=None):
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    for p in profiles or []:
        theme = p.get("theme") or {}
        hit = bool(theme.get("hit"))
        if not hit:
            hit = _theme_hit(p, themes)["hit"]
        tech = p.get("tech") or {}
        support = tech.get("support") or {}
        near = bool(support.get("near_support") or tech.get("near_support"))
        bull = bool(tech.get("ma_bull_stack"))
        d20 = _fnum(tech.get("dist_ma20_pct"), 0.0) or 0.0
        ret20 = _fnum(tech.get("ret_20d_pct"), 0.0) or 0.0
        above20 = d20 >= -1.0
        w2s = bool(tech.get("weak_to_strong") or (p.get("w2s") or {}).get("reason") == "w2s")
        structure_ok = (near or bull or above20 or w2s)
        oversold = (d20 < -5.0 and ret20 < -15.0)
        gap = tech.get("sr_gap_pct")
        gap_f = _fnum(gap, None)
        space_ok = (gap_f is not None and gap_f >= float(min_sr_gap_pct)
                    and structure_ok and not oversold)
        if oversold and not hit:
            stats["oversold_bounce"] += 1
            p["quality_skip"] = "超跌反弹非趋势"
            continue
        if not hit and not space_ok:
            stats["no_theme_no_space"] += 1
            p["quality_skip"] = "无题材且空间/结构不足"
            continue
        passed.append((p, hit, gap_f if gap_f is not None else -1.0))

    passed.sort(key=lambda x: (
        0 if (x[1] and x[2] >= min_sr_gap_pct) else (1 if x[2] >= min_sr_gap_pct else 2),
        -x[2],
    ))
    capped_boards = {"创业板", "科创板"}
    kept = []
    board_n: Dict[str, int] = {}
    for p, _hit, _gap in passed:
        b = _board_of(p.get("code"))
        n = board_n.get(b, 0)
        if b in capped_boards and n >= int(max_per_board):
            stats["board_capped"] += 1
            p["quality_skip"] = f"高波动板超限({b})"
            continue
        board_n[b] = n + 1
        kept.append(p)
        if len(kept) >= max(1, int(max_n)):
            break
    stats["total_kept"] = len(kept)
    stats["by_board"] = board_n
    return kept, stats


def soft_warnings(item: Dict, market: dict, strategy: str = "") -> List[str]:
    """软提示：历史经验型风控（不是不可交易事实）。

    与 hard_exclusions 的区别：hard 是「买不了/不该买」的客观事实，warnings 是
    「大概率不好」的经验判断 —— profile 里两者都给 LLM，由它决定是否采纳；
    filter_candidates（保守兼容路径）才会把 warnings 一并剔除。
    """
    out: List[str] = []
    if _mood_regime(market) == "weak":
        src = str(item.get("source") or "")
        if any(k in src for k in ("热点",)) and not (item.get("reason") or ""):
            out.append("弱势市热点活跃源无题材支撑(退潮风险)")
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

    # 连续涨停（用涨跌停幅度近似，尾部倒序）；limit_pct<=0（新股无涨跌幅）时恒 0
    limit_pct = get_limit_pct(code, name)
    streak = 0
    if limit_pct > 0:
        for i in range(len(bars) - 1, 0, -1):
            pc = bars[i - 1]["close"]
            if pc and (bars[i]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
                streak += 1
            else:
                break

    def _dist(ma_v):
        return round((close - ma_v) / ma_v * 100, 2) if ma_v and close else None

    dist_ma5 = _dist(_last(ma5))
    dist_ma10 = _dist(_last(ma10))
    # 支撑距离（供 LLM 直接用支撑框架，无需 read_skill）
    _cands = [("ma5", dist_ma5, _last(ma5)), ("ma10", dist_ma10, _last(ma10)),
              ("ma20", dist_ma20, ma20_v)]
    _valid = [c for c in _cands if c[1] is not None and abs(c[1]) <= 3.0]
    nearest = min(_valid, key=lambda c: abs(c[1])) if _valid else None

    # 压力位 / 支撑–压力间距（2026-10-28 v2）：用**最近有效压力**，不是远处前高——
    # 超跌 20% 后到 20 日峰的 gap 会虚高成「大空间」，那是反弹不是趋势空间。
    # 口径：优先上方 MA20 / 近 10 日峰中的**更近者**；已站上则用近 20 日峰。
    win_hi20 = max(highs[-win20:])
    win_hi10 = max(highs[-min(10, win20):])
    overhead = [x for x in (win_hi10, ma20_v) if x and x > close * 1.005]
    if overhead:
        resistance = min(overhead)  # 最近压力
    else:
        resistance = win_hi20 if win_hi20 > close * 1.005 else (
            max(highs[-40:-win20]) if len(highs) > win20 else None)
    sup_px = nearest[2] if nearest else None
    sr_gap_pct = None
    if resistance and sup_px and sup_px > 0:
        sr_gap_pct = round((resistance - sup_px) / sup_px * 100, 2)

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
        "dist_ma5_pct": dist_ma5,
        "dist_ma10_pct": dist_ma10,
        "dist_ma20_pct": round(dist_ma20, 2) if dist_ma20 is not None else None,
        "support": {
            "nearest": nearest[0] if nearest else None,
            "nearest_dist_pct": nearest[1] if nearest else None,
            "near_support": bool(nearest),  # |dist|<=3% 视为贴支撑
            "ma5": round(_last(ma5), 2) if _last(ma5) else None,
            "ma10": round(_last(ma10), 2) if _last(ma10) else None,
            "ma20": round(ma20_v, 2) if ma20_v else None,
        },
        "resistance": {
            "price": round(resistance, 2) if resistance else None,
            "dist_pct": round((resistance - close) / close * 100, 2)
            if resistance and close else None,
        },
        "sr_gap_pct": sr_gap_pct,  # 支撑–压力间距%；过窄=操作性弱
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
    """截至上一交易日结束时的连板高度（判断「连板后首阴」等形态用）。

    语义 =「今日之前」的连板数（analysis-framework.md 维度 4：prev 高 + 当日
    收阴 = 首阴；common.py 渲染「连板X/前Y」）：
    · 今日仍涨停（streak>=1）→ streak-1（3 连板 → 前 2 板）；
    · 今日未涨停（首阴/断板）→ 从昨日（len-2）向前数（前 4 板首阴 → 4）。
    【易错】旧实现 idx=len-1-streak 恰落在主 streak 循环「因涨幅不足 break 的
    那根 bar」上，while 首查必败 → 恒 0（C3，2026-10-21 修）。判定阈值与
    tech_summary 主循环同口径（limit_pct-0.8 容差），勿单独改动。
    """
    if limit_pct <= 0:
        return 0
    if streak >= 1:
        return streak - 1
    idx = len(bars) - 2
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
#  实时行情覆盖层（bar vs rt 对账，2026-10-14）
# ═══════════════════════════════════════════════════════════════
# 【为什么必须有】deep_analyze 的 tech 只吃 fetch_kline（本地日线）。盘后本地库
# 未刷当日 bar 时，整份证据是旧数据 —— 实测 2026-10-14：600408 bar close 3.69
# / +4.53% / limit_up=false，实时 4.06 / +10.03% / 涨停封板。主 agent 闻到矛盾
# 就会在主对话逐只重拉 quote/flow/tech（一轮烧十几个 LLM 步）。
# 【对账原则】历史指标（MA/RSI/20 日位置）仍基于日线（改了就不自洽）；
# 可执行性字段（当日收盘 / 涨跌幅 / 涨跌停）以 rt 为准，并显式标 stale_bar。

# bar close 与 rt last 相对偏差超过该阈值 ⇒ 判定日线滞后
_STALE_BAR_REL_TOL = 0.005


def fetch_rt_map(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量实时行情 → {code: rt事实}；2026-10-20 改分块绕过 20 只截断。

    失败块只丢该块并 logger.warning，不整批作废；返回 map 只含成功票，失败票缺席。
    【三态契约（2026-10-21 C1）】change_pct / limit_up_price / limit_down_price
    缺失时保留 **None**（源未给 ≠ 0）：0/-1 是「无涨跌幅限制」哨兵、None 是
    「未知」，_f(None)=0.0 会把未知伪造成 0 价 → 下游判 no_limit → 封板票
    buyable=True 进可买表、涨跌幅显示 +0.00%（与 _facts.fill_quote 的
    「0 是合法哨兵、仅排 None」同一口径）。
    """
    if not codes:
        return {}
    from .common import batch_realtime_quotes
    try:
        quotes, failed = batch_realtime_quotes(codes)
    except Exception as e:
        logger.warning("[MktScreen] 实时行情分块失败: %s", e)
        return {}
    if failed:
        logger.warning("[MktScreen] 实时行情分块失败 %d 块，涉及 %d 只",
                       len(failed), sum(len(c) for c in failed))
    out = {}
    for code in codes:
        info = quotes.get(code)
        if not isinstance(info, dict):
            continue
        last_raw = info.get("last") or info.get("price")
        if last_raw is None:
            continue
        last = _f(last_raw)
        out[code] = {
            "price": last,
            "change_pct": _num_or_none(info.get("changePercent")),
            "previous_close": _f(info.get("previousClose")),
            "limit_up_price": _num_or_none(info.get("limit_up")),
            "limit_down_price": _num_or_none(info.get("limit_down")),
            "high": _f(info.get("high")),
            "low": _f(info.get("low")),
            "time": info.get("time"),
            "name": info.get("name"),
        }
    return out


def objective_preexclude(candidates, strategy, quotes=None):
    """客观事实预排除——run() 内部漏斗第 1 层（2026-10-20）。

    只排除客观不可交易项（与 hard_exclusions 同性质，但用实时行情在画像前做，
    省掉注定进不了 deep 的票的 kline/pred 开销）：

    - 所有时段：ST/退市、停牌（仅显式 suspended 标记）、价格带 [2,300]、
      换手 <1%（字段存在且 >0 才判，缺失不剔）
    - intraday / eod：涨停封板、跌停封死（当日买不进/无流动性）
    - post_market：不排涨停（次日仍可买），仅标记；跌停封死仍排

    涨停判定：quotes 里 limit_up 价 >0 且现价 >= 价-0.01；哨兵 <=0 = 无涨跌幅限制不排；
    缺行情回退 common.is_limit_locked(code, name, close, prev_close)；
    两者都拿不到 → 不剔，计入 stats["quotes_missing"]。

    Args:
        candidates: pre_screen 返回的候选列表（dict）
        strategy: intraday / eod / post_market
        quotes: 可选 {code: 行情dict}；缺省时只用候选自身字段（fill_quote 已回填）

    Returns:
        (kept, stats): kept=保留候选（原 dict 引用，不复制）；
            stats=excluded_stats 各类计数 {limit_up, limit_down, st, suspended,
            price_band, low_turnover, quotes_missing}
    """
    from .common import is_limit_locked

    strategy = strategy or "post_market"
    exclude_limit = strategy in ("intraday", "eod")
    stats = {"limit_up": 0, "limit_down": 0, "st": 0, "suspended": 0,
             "price_band": 0, "low_turnover": 0, "quotes_missing": 0}
    kept = []
    quotes = quotes or {}

    for item in candidates or []:
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        src = str(item.get("source") or "")

        # 停牌：仅显式 suspended 标记才剔（缺行情不算停牌）
        if item.get("suspended"):
            stats["suspended"] += 1
            continue

        # ST / 退市（与 hard_exclusions 同判定口径）
        if "ST" in name.upper() or src == "ST股" or "退" in name:
            stats["st"] += 1
            continue

        # 价格带 [2, 300]（缺价 raw=None 不剔，与换手规则同口径）
        price_raw = item.get("price") if item.get("price") is not None else item.get("close")
        price = _f(price_raw) if price_raw is not None else None
        if price is not None and (price < 2 or price > 300):
            stats["price_band"] += 1
            continue

        # 换手 <1%：字段存在且 >0 才判（0/None = 数据不全，不据此剔）
        trn = _f(item.get("turnover_pct"))
        if trn is not None and 0 < trn < 1:
            stats["low_turnover"] += 1
            continue

        # 涨跌停封板判定：quotes 优先，缺则用候选自身回填字段，再缺回退日线
        rt = quotes.get(code) if isinstance(quotes.get(code), dict) else None
        # 缺失保持 None（_f(None)=0.0 会把「没价」变成「价为0」→ 100% 误判跌停，
        # 2026-10-21 线上假阴性根因之一；与下方 line「判不了不剔计 quotes_missing」同语义）
        last_raw = (rt or {}).get("last") or (rt or {}).get("price") or item.get("price")
        last = _f(last_raw) if last_raw is not None else None
        # Need to distinguish "missing" from "sentinel 0"
        up_px_raw = None
        if rt and rt.get("limit_up") is not None:
            up_px_raw = rt.get("limit_up")
        elif item.get("limit_up") is not None:
            up_px_raw = item.get("limit_up")
        up_px = _f(up_px_raw)
        down_px_raw = None
        if rt and rt.get("limit_down") is not None:
            down_px_raw = rt.get("limit_down")
        elif item.get("limit_down") is not None:
            down_px_raw = item.get("limit_down")
        down_px = _f(down_px_raw)
        prev_close_raw = None
        if rt and rt.get("previousClose") is not None:
            prev_close_raw = rt.get("previousClose")
        elif item.get("previous_close") is not None:
            prev_close_raw = item.get("previous_close")
        prev_close = _f(prev_close_raw)

        limit_up = None
        limit_down = None
        if up_px_raw is not None:  # 有字段值
            if up_px > 0:
                if last is not None:  # 缺价判不了 → 保持 None（计 quotes_missing）
                    limit_up = last >= up_px - 0.01
            elif up_px <= 0:  # 哨兵 <=0：无涨跌幅限制
                limit_up = False
        if down_px_raw is not None:
            if down_px > 0:
                if last is not None:
                    limit_down = last <= down_px + 0.01
            elif down_px <= 0:
                limit_down = False

        # 实时信息不足以判涨停 → 回退日线 is_limit_locked
        if limit_up is None and last is not None and prev_close and prev_close > 0:
            try:
                limit_up = is_limit_locked(code, name, last, prev_close)
            except Exception:
                limit_up = None

        if limit_up is None and limit_down is None:
            stats["quotes_missing"] += 1  # 判不了，不剔，留痕

        if limit_up:
            item["_preexcluded"] = "涨停封板"
            if exclude_limit:
                stats["limit_up"] += 1
                continue
        if limit_down:
            item["_preexcluded"] = "跌停封死"
            stats["limit_down"] += 1  # 跌停封死所有时段都排（无流动性）
            continue

        kept.append(item)
    return kept, stats


def _objective_rank_key(item: Dict, index: int, themes: List[str] = ()) -> tuple:
    """客观优先级键（2026-10-28 选股目标：短线最小风险、最大涨幅）——
    纯计数分层，**不编造分数**——p_up 仍是唯一数字。

    统一供两处「谁先获得资源/名额」的排序（同一口径，勿另写）：
      · 池子超过打分护栏时，谁先拿 p_up（score_pool 预算让位）；
      · 缺分（no_pred）票按什么顺序占用保送名额（缺分保送）。
    分层（越小越优先）：题材命中+贴支撑 0 → 题材命中或贴支撑 1 →
    多源命中(source 含 +) 2 → 其余 3；同层按输入顺序（index）稳定排，可复盘。
    【2026-10-28】删除龙回头/热点稀缺特权——追高源不再是加分项。
    题材命中：画像后的 item 自带 theme.hit；画像前的原始候选无此字段，回退
    _theme_hit 现算（同一函数，口径一致）。贴支撑：画像后的 tech.support.near_support。
    """
    theme = item.get("theme")
    hit = theme.get("hit") if isinstance(theme, dict) \
        else _theme_hit(item, list(themes))["hit"]
    tech = item.get("tech") or {}
    support = tech.get("support") or {}
    near = bool(support.get("near_support") or tech.get("near_support"))
    source = str(item.get("source") or "")
    if hit and near:
        tier = 0
    elif hit or near:
        tier = 1
    elif "+" in source:
        tier = 2
    else:
        tier = 3
    return (tier, index)


# ── P1 弱转强回捞（2026-10-23 用户裁定，合并收紧版）──────────────────
# 【定位】排名按 p_up（已收盘日线）砍尾会漏掉「今天才转强」的票（002164@11:20）。
#   本通道**只回捞、不改排名**，条件从严，防假弱转强。
# 【明确弱转强】合并「乖离≤2%+流入≥2000万」与「滞跌+抢筹加速」两版之长：
#   ① 回踩 MA5 附近（乖离 ≤2.5% 或 near_support）
#   ② 滞跌：非放量破位、非深跌（接飞刀否决）
#   ③ 主力净流入 **≥2000万**（绝对门槛，挡小单噪声）
#   ④ 且 **今日 > 昨日**（抢筹加速；昨日取不到则只卡③）
#   ⑤ 日线结构好（多头排列或距 MA20 不过远）——用日线几何替代 t1，不进多周期
# 【日内动量】只作滞跌轻确认（未继续深跌），**不**进 VWAP/m15。

_W2S_MIN_MAIN_WAN = 2000.0   # 今日主力净流入下限（万元）


def _w2s_tech_ok(p: dict) -> Tuple[bool, str]:
    """回踩 + 滞跌 + 日线结构：(通过, 原因)。纯 tech，零 IO。"""
    tech = p.get("tech") or {}
    try:
        d5 = abs(float(tech.get("dist_ma5_pct"))) if tech.get("dist_ma5_pct") is not None else 99.0
    except (TypeError, ValueError):
        d5 = 99.0
    try:
        d20 = float(tech.get("dist_ma20_pct")) if tech.get("dist_ma20_pct") is not None else 99.0
    except (TypeError, ValueError):
        d20 = 99.0
    try:
        chg = float(tech.get("change_pct") or 0.0)
    except (TypeError, ValueError):
        chg = 0.0
    try:
        vr = float(tech.get("vol_ratio") or 0.0)
    except (TypeError, ValueError):
        vr = 0.0

    # ① 回踩 MA5 附近（≤2.5%）
    if not (tech.get("near_support") or d5 <= 2.5):
        return False, f"距MA5 {d5:.1f}% 非回踩位"
    # ② 滞跌：非放量破位、非深跌
    if vr >= 2.5 and chg <= -3.0:
        return False, f"放量下跌 vr={vr:.1f} chg={chg:.1f}%"
    if chg <= -5.0:
        return False, f"仍深跌 {chg:.1f}% 非滞跌"
    # ⑤ 日线结构仍好（多头或未远离 MA20）
    if not (tech.get("ma_bull_stack") or d20 <= 6.0):
        return False, f"日线结构差 距MA20 {d20:.1f}% 且非多头"
    return True, "ok"


def _w2s_flow_ok(code: str) -> Tuple[bool, str, dict]:
    """主力 ≥2000万 且 今日>昨日（抢筹加速）。只对短名单调用。"""
    extra: Dict[str, Any] = {}
    try:
        fl = flow_summary(code) or {}
    except Exception as e:
        return False, f"flow:{type(e).__name__}", extra
    if not fl.get("ok"):
        return False, str(fl.get("reason") or "flow缺"), extra
    try:
        today = float(fl.get("main_net_wan") or 0.0)
    except (TypeError, ValueError):
        today = 0.0
    extra["main_net_wan"] = today
    # ③ 绝对门槛：真抢筹，挡小单
    if today < _W2S_MIN_MAIN_WAN:
        return False, f"今日主力 {today:.0f}万 < {_W2S_MIN_MAIN_WAN:.0f}万", extra

    # ④ 加速：今日 > **上一完整交易日**（不是死取倒数第二条——最后一条可能是今日）
    try:
        from datetime import date as _date
        from ...tools.finance.fund_flow_tools import get_fund_flow
        df = get_fund_flow(scope="stock_daily", codes=code, days=5) or {}
        rows = ((df.get("data") or {}).get(code) or {}).get("data") or []
        _today = _date.today().isoformat()
        y_nets = [float(r["main_net"]) for r in rows
                  if r.get("main_net") is not None
                  and str(r.get("date") or "")[:10] < _today]
        if y_nets:
            y_wan = y_nets[-1] / 1e4
            extra["y_main_net_wan"] = round(y_wan, 1)
            extra["net_accel"] = today > y_wan
            if today <= y_wan:
                return False, f"今日 {today:.0f}万 ≤ 昨日 {y_wan:.0f}万（未加速）", extra
        else:
            extra["y_main_net_wan"] = None
            extra["net_accel"] = None
    except Exception as e:
        extra["y_main_net_wan"] = None
        extra["net_accel"] = None
        extra["accel_note"] = f"昨日主力取不到:{type(e).__name__}"
    return True, "ok", extra


def _is_weak_to_strong(p: dict) -> Tuple[bool, dict]:
    """P1 弱转强（合并收紧）：回踩滞跌 + 主力≥2000万 + 抢筹加速 + 日线好。"""
    tech_ok, tech_why = _w2s_tech_ok(p)
    if not tech_ok:
        return False, {"reason": tech_why}
    flow_ok, flow_why, fl = _w2s_flow_ok(str(p.get("code") or ""))
    out = dict(fl)
    out["tech_why"] = tech_why
    if not flow_ok:
        out["reason"] = flow_why
        return False, out
    out["reason"] = "w2s"
    return True, out


def score_pool(candidates, strategy="", market=None, main_themes=None,
               p_up_floor=0.48, rank_cut=0.30, floor_keep=4, no_pred_keep=2,
               w2s_keep=2, reserved=None):
    """双保险软门槛 + 缺分保送 + 弱转强回捞——run() 漏斗（选股目标：短线最小风险、最大涨幅）。

    层次（2026-10-28 去连板/龙回头后单漏斗）：
    1. **hard**：画像层已标的客观障碍，本层计数不放进 deep；
    2. **p_up 硬闸**：低于 p_up_floor 的进 cut_list（计 p_up_hard）；
    3. **排名截断**：过闸票按 p_up 降序砍后 rank_cut 比例（计 rank_cut）；
    4. **三条回捞通道**（都只加不减）：
       · floor_kept：被砍但**题材命中或贴支撑**（低风险位）的前 floor_keep 只；
       · no_pred_kept：无 p_up（模型盲区）按客观优先级占 no_pred_keep 名额
         （2026-10-21：缺分 ≠ 低分）；
       · w2s_kept：被砍但**明确弱转强**（回踩 MA5 滞跌 + 主力净流入
         + 日线结构好 + 今日主力>昨日）的前 w2s_keep 只。只回捞、不改 p_up 排名；
         条件从严，防假弱转强。**本通道最贴「最小风险+最大涨幅」**。

    【reserved】可选保留名单（其 code 全程排除，不占预算）。run() 不再有
    8.1.0 基线层（2026-10-28 用户裁定：选股去掉连板/龙回头旧逻辑）。

    【rank_cut 定位】是**数量控制**（限制进 deep 的条数），不是质量否决。
    相对截断（砍排名后 X%）意味着同一只票在强池出局、弱池存活——三条回捞通道
    （floor/w2s/no_pred）为此而设。要绝对门槛请调 p_up_floor。

    Args:
        w2s_keep: 弱转强名额上限（默认 2；0=关闭）
        reserved: 保留画像列表（dict 带 code）；其 code 在本层全程排除
    """
    reserved_codes = {str(p.get("code") or "") for p in (reserved or [])}
    cand_list = list(candidates or [])
    if reserved_codes:
        cand_list = [c for c in cand_list
                     if str(c.get("code") or "") not in reserved_codes]
    themes = [t[0] for t in (main_themes or []) if isinstance(t, (list, tuple)) and t]
    score_skipped = 0
    budget = _score_max()
    if len(cand_list) > budget:
        # 只有真超护栏才让位：客观优先级（纯计数）决定谁先拿 p_up，
        # 被让位的票计 score_skipped（漏斗可见，不静默丢）。
        add_n = len(cand_list)
        order = sorted(range(add_n),
                       key=lambda i: _objective_rank_key(cand_list[i], i, themes))
        cand_list = [cand_list[i] for i in order[:budget]]
        score_skipped = add_n - budget
    prescreen_like = {
        "strategy": strategy,
        "market": market or {},
        "main_themes": main_themes or [],
        "candidates": cand_list,
    }
    # limit=len ⇒ 全量画像/打分（profile 内部 limit*2 窗口自此不设限）
    bundle = profile_candidates(prescreen_like, limit=max(len(cand_list), 1),
                                with_flow=False, with_pred=True)
    profiles_all = bundle.get("profiles", [])
    hard_n = sum(1 for p in profiles_all if p.get("hard"))
    profiles = [p for p in profiles_all if not p.get("hard")]

    with_pred = [p for p in profiles if p.get("p_up") is not None]
    no_pred_list = [p for p in profiles if p.get("p_up") is None]

    ranked = sorted(with_pred, key=lambda x: x.get("p_up") or 0, reverse=True)
    n = len(ranked)
    cut_start = int(n * (1 - max(0.0, min(float(rank_cut), 0.9))))

    kept = []
    cut_list = []
    p_up_hard_n = 0
    rank_cut_n = 0
    for i, p in enumerate(ranked):
        p_up = p.get("p_up") or 0
        if p_up < p_up_floor:
            cut_list.append(p)
            p_up_hard_n += 1
        elif n > 1 and i >= cut_start:
            cut_list.append(p)
            rank_cut_n += 1
        else:
            kept.append(p)

    # 保底回捞：题材命中或贴支撑（低风险位；不再给连板/龙回头稀缺特权）
    rescued = []
    for p in cut_list:
        theme = p.get("theme") or {}
        tech = p.get("tech") or {}
        support = tech.get("support") or {}
        near = bool(support.get("near_support") or tech.get("near_support"))
        if theme.get("hit") or near:
            rescued.append(p)
    floor_kept = rescued[:max(0, int(floor_keep))]
    for p in floor_kept:
        p["floor_kept"] = True

    # 缺分保送（2026-10-21）：模型盲区 ≠ 坏票。独立名额、独立排序，标 no_pred_kept
    no_pred_kept = []
    if no_pred_list and int(no_pred_keep or 0) > 0:
        ranked_np = sorted(enumerate(no_pred_list),
                           key=lambda iv: _objective_rank_key(iv[1], iv[0], themes))
        no_pred_kept = [p for _, p in ranked_np[:max(0, int(no_pred_keep))]]
        for p in no_pred_kept:
            p["no_pred_kept"] = True

    # ★ P1 弱转强回捞（2026-10-23）：被 rank_cut/p_up 剔掉但**明确弱转强**
    #   （回踩滞跌 + 主力≥2000万 + 今日>昨日 + 日线好）→ 进 deep。只回捞不改排名。
    w2s_kept = []
    if int(w2s_keep or 0) > 0:
        already = {id(x) for x in kept + floor_kept + no_pred_kept}
        shortlist = [p for p in cut_list if id(p) not in already]
        # 先零 IO tech 预筛，再对入围者拉资金（限名额×3，控 IO）
        pre = [p for p in shortlist if _w2s_tech_ok(p)[0]]
        pre.sort(key=lambda x: str(x.get("code") or ""))
        for p in pre[:max(0, int(w2s_keep) * 3)]:
            if len(w2s_kept) >= int(w2s_keep):
                break
            ok, info = _is_weak_to_strong(p)
            p["w2s"] = info
            if ok:
                p["w2s_kept"] = True
                w2s_kept.append(p)

    # 有分票按 p_up 降序在前，弱转强/缺分保送殿后
    selected = kept + floor_kept + w2s_kept + no_pred_kept
    selected.sort(key=lambda x: (x.get("p_up") is not None, x.get("p_up") or 0),
                  reverse=True)
    stats = {
        "hard": hard_n,
        "p_up_hard": p_up_hard_n,
        "rank_cut": rank_cut_n,
        "no_pred": len(no_pred_list),
        "no_pred_kept": len(no_pred_kept),
        "floor_kept": len(floor_kept),
        "w2s_kept": len(w2s_kept),
        "scored": len(profiles),
        "score_skipped": score_skipped,
    }
    meta = {
        "strategy": bundle.get("strategy"),
        "market": bundle.get("market"),
        "mood_regime": bundle.get("mood_regime"),
        "themes": bundle.get("themes"),
        "hints": bundle.get("hints"),
        "missing": bundle.get("missing"),
        "errors": bundle.get("errors"),
    }
    return selected, stats, meta


def apply_rt_overlay(row: Dict[str, Any], rt: Optional[Dict[str, Any]],
                     code: str = "", name: str = "") -> None:
    """把实时行情对账进证据行（原地改 row）。

    · row["rt"]：实时快照（缺 rt 时不写，不塞 None 壳）
    · row["stale_bar"]：bar close 与 rt last 相对偏差 > 0.5% ⇒ True
    · tech.limit_up / 顶层可执行价：以 rt 为准修正（涨停买不进是 hard 条件）
    · tech 的 MA/RSI/位置类指标不动（历史几何，rt 改不了也不该改）
    """
    if not rt:
        return
    from .common import get_limit_pct, is_limit_locked

    tech = row.get("tech") if isinstance(row.get("tech"), dict) else {}
    bar_close = _f(tech.get("close")) if tech else None
    rt_price = _f(rt.get("price"))
    stale = False
    if bar_close and rt_price and bar_close > 0:
        stale = abs(rt_price - bar_close) / bar_close > _STALE_BAR_REL_TOL

    prev_close = _f(rt.get("previous_close"))
    if prev_close is None and tech:
        # bar 的 prev 不在 tech 里；用 bar close 反推不到就留 None，limit_up 用涨跌停价判
        prev_close = None

    # 涨跌停真值：优先「现价 vs 涨停价」。quote 侧哨兵：
    #   >0  = 有效涨跌停价
    #   <=0 = 明确「无涨跌停」（新股上市首日/无限制）→ 不得回退公式，否则 N 联亚会被误判涨停
    #   缺失 = 源无此字段 → 才用 get_limit_pct 近似
    # 2026-10-15 实测：N联亚药业(301569) -1/-1；*ST天成(600112) -1/-1；ST 且 quote 价与 get_limit_pct 不一致。
    def _parse_limit_px(v) -> tuple:
        if v is None or v == "":
            return None, "missing"
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None, "missing"
        if x > 0:
            return x, "price"
        return None, "no_limit"

    limit_up_price, up_kind = _parse_limit_px(rt.get("limit_up_price"))
    limit_down_price, _dn_kind = _parse_limit_px(rt.get("limit_down_price"))
    if up_kind == "price" and rt_price:
        limit_up = bool(rt_price >= float(limit_up_price) - 0.01)
    elif up_kind == "no_limit":
        limit_up = False
    elif rt_price and prev_close:
        limit_up = bool(is_limit_locked(code or row.get("code", ""), name or row.get("name", ""),
                                        rt_price, prev_close))
    else:
        limit_up = None

    # 跌停封死（无流动性）：与涨停对称，供「不可买」判定
    limit_down = None
    if limit_down_price and rt_price:
        limit_down = bool(rt_price <= float(limit_down_price) + 0.01)
    elif _dn_kind == "no_limit":
        limit_down = False

    row["rt"] = {
        "price": rt.get("price"),
        "change_pct": round(rt["change_pct"], 2) if rt.get("change_pct") is not None else None,
        "previous_close": rt.get("previous_close"),
        "limit_up": limit_up,
        "limit_down": limit_down,
        "limit_up_price": limit_up_price,
        "limit_down_price": limit_down_price,
        "limit_band": up_kind,  # price | no_limit | missing（跌停侧同 kind 语义，无限制时两价皆空）
        "time": rt.get("time"),
    }
    # 可买性：封板（涨/跌）当日不可买 —— 写进 rt_risks，LLM 不得放入可买表。
    # 三态（2026-10-21 C1）：任一侧涨跌停状态未知（missing 且无昨收可近似）→
    # buyable=None（待定，不可确认可买），**绝不给 True** —— 旧逻辑「一侧已知
    # False 即 True」会在另一侧状态不明时漏掉封板票；limit_band="missing" 即
    # 「未知」的可读标注（missing≠no_limit，见 references/limit-rules.md）。
    if limit_up is True or limit_down is True:
        row["rt"]["buyable"] = False
    elif limit_up is None or limit_down is None:
        row["rt"]["buyable"] = None
    else:
        row["rt"]["buyable"] = True
    row["stale_bar"] = stale

    if tech:
        if limit_up is not None:
            tech["limit_up"] = limit_up
        if stale and rt_price:
            # 可执行性口径以 rt 为准；历史指标保留 bar 几何
            tech["close"] = round(float(rt_price), 2)
            if rt.get("change_pct") is not None:
                tech["change_pct"] = round(float(rt["change_pct"]), 2)
            tech["bar_close"] = round(float(bar_close), 2) if bar_close else None
        tech["data_asof"] = "rt" if (stale or limit_up is not None) else "bar"

    if name and not row.get("name"):
        row["name"] = name
    if row.get("tech") is not None and limit_up:
        # 买不进：与 hard_exclusions 同语义，但 deep_analyze 不做淘汰，只标事实。
        # 不再要求 limit_up_price 有值（2026-10-21 C1）：价缺失但按公式近似判出
        # 封板时，同样要留痕，否则 rt.buyable=false 与 rt_risks 口径分裂。
        risks = row.setdefault("rt_risks", [])
        if "涨停封板-当日买不进" not in risks:
            risks.append("涨停封板-当日买不进")
    if limit_down is True:
        risks = row.setdefault("rt_risks", [])
        if "跌停封死-无流动性" not in risks:
            risks.append("跌停封死-无流动性")
    if stale:
        risks = row.setdefault("rt_risks", [])
        note = f"日线滞后(bar_close={bar_close}, rt={rt_price})"
        if note not in risks:
            risks.append(note)


# ═══════════════════════════════════════════════════════════════
#  T+1 标定预测（P(次日涨)×100）
# ═══════════════════════════════════════════════════════════════
# 复用 app.watchlist.predict 的已标定模型（AUC≈0.61），而非手拍技术分。
# 2026-09-29：旧 enrich_predictive 会按情绪分桶**替 LLM 裁剪掉**候选（门槛 + 条数上限），
# 现改为「只给分数和建议」，裁剪权交还 LLM（详见 SKILL.md 的评分语义段）。

_MKT_KLINES_CACHE = {"data": None}
# 冷启互斥（2026-10-25 P1-①）：后台预热线程与画像期首票 predict_one 可能
# 并发触发 _market_klines()。无锁时双方都 miss ⇒ 各建一次 mootdx 客户端
# （bestip 扫描 ~74s/次），纯浪费。双重检查锁保证整个进程只取一次。
_MKT_KLINES_LOCK = threading.Lock()


def _market_klines():
    if _MKT_KLINES_CACHE["data"] is None:
        with _MKT_KLINES_LOCK:
            if _MKT_KLINES_CACHE["data"] is None:
                try:
                    from app.watchlist.predict import load_market_klines
                    _MKT_KLINES_CACHE["data"] = load_market_klines(120) or []
                except Exception:
                    _MKT_KLINES_CACHE["data"] = []
    return _MKT_KLINES_CACHE["data"]


def prewarm_market_klines() -> None:
    """后台预热指数日线（2026-10-25 P1-①）。

    根因（主探针 probe_screener_timing_20261024 实锤）：画像期
    attach_prediction 的首个 predict_one 触发 _market_klines() 冷启 →
    get_index_daily_kline("000001",120) → mootdx 客户端创建（冷进程里
    provider.tdx_ex 无 live 服务器 ⇒ bestip 全扫 ~74s）+ 腾讯兜底，
    单点 78.35s，占 run() 总耗时 44%。该取数与候选池（candidate_pool
    ~69s：候选池取数 + 行情回填）无依赖 ⇒ run() 入口起后台线程
    预热，两者重叠后单点基本隐藏。失败仅记 warning：主流程到时按原
    路径自取，行为与无预热时一致（不静默吞，见模块头纪律）。
    """
    def _bg():
        try:
            _market_klines()
            logger.info("[market-screener] 指数K线后台预热完成")
        except Exception as e:
            logger.warning("[market-screener] 指数K线后台预热失败: %s", e)

    threading.Thread(target=_bg, name="mktkl-prewarm", daemon=True).start()


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
    """兼容旧名：打分 + 按情绪门槛标记 selected + 重排（⚠ 原本的契约用例
    test_market_screener_pred 已删除——2026-10-14 核实 tests/ 下只剩 __pycache__
    字节码，无 .py；此处契约改为由 qd_smoke 的选股项与基线对拍守卫覆盖）。

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
        # 情绪弱时对高位/无题材活跃源折价
        src = str(it.get("source") or "")
        if regime == "weak" and any(k in src for k in ("热点", "涨停")):
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

    ex = None
    try:
        # 同 C7（假超时）：不用 `with`（__exit__ = shutdown(wait=True) 会干等到
        # 挂死线程结束，把 ex.map 的 timeout 抵消掉）
        ex = ThreadPoolExecutor(max_workers=_FETCH_WORKERS)
        try:
            list(ex.map(_fetch, pool, timeout=_FETCH_TIMEOUT_S))
        except Exception as e:  # 超时/线程崩：已取到的照用，缺口计入 missing
            missing.append(f"并发取数超时: {type(e).__name__}: {e}")
            logger.warning("[MktScreen] 并发取数超时: %s", e)
    except Exception as e:  # 线程池不可用 → 退化为串行，不留死角
        logger.warning("[MktScreen] 并发取数失败，退化为串行: %s", e)
        for c in pool:
            _fetch(c)
    finally:
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)

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
                # 涨幅需两根日线，单根（新股/取数截断）无环比基准，留空
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

        _fex = None
        try:
            _fex = ThreadPoolExecutor(max_workers=_FETCH_WORKERS)
            list(_fex.map(_flow, profiles[:limit]))
        except Exception as e:
            logger.warning("[MktScreen] 资金流并发失败: %s", e)
            for p in profiles[:limit]:
                _flow(p)
        finally:
            if _fex is not None:
                _fex.shutdown(wait=False, cancel_futures=True)

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
            skill_name="market-screener",
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
        skill_name="market-screener",
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
