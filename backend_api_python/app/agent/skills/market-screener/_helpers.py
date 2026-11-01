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

import json
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
        # 「当日买不进」只在盘中此刻成立（2026-10-30 用户裁定）：eod/post_market
        # 是次日承接视角，封板不是买不进的硬约束，只作事实提示。
        flags.append("涨停封板-当日买不进" if strategy == "intraday" else "涨停封板")

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
    if _mood_regime(market) == "weak" or (market or {}).get("cautious"):
        src = str(item.get("source") or "")
        if any(k in src for k in ("连板", "4IN1", "龙回头", "涨停")) and not (item.get("reason") or ""):
            out.append("弱势市涨停活跃源无题材支撑(退潮风险)")
    trn = _f(item.get("turnover_pct"))
    if trn > 25:
        out.append(f"换手{trn:.0f}%过热")
    return out


def _last(seq: List[Optional[float]]) -> Optional[float]:
    return seq[-1] if seq else None


def _open_gap_floor(bars: List[Dict], lookback: int = 30) -> Optional[float]:
    """最近一个**向上跳空且未被回补**的缺口下沿（= 跳空前一日最高价）。

    向上跳空 low[i] > high[i-1] 形成的缺口，回踩到下沿常获支撑。只认现价之上
    还有缺口的（`bars[i:]` 全部最低价仍 > 下沿）：已回补的缺口不再有支撑意义。
    取最近的一个 —— 越近的缺口对当下越有意义。
    """
    n = len(bars)
    for i in range(n - 1, max(1, n - lookback), -1):
        floor = bars[i - 1]["high"]
        if not floor or bars[i]["low"] <= floor:
            continue
        if min(b["low"] for b in bars[i:]) > floor:
            return float(floor)
    return None


def _volume_poc(bars: List[Dict], window: int = 20, bins: int = 10) -> Optional[float]:
    """近 N 日**成交密集区**中心价（筹码成本带，成交量价格分布的 POC 简化版）。

    把 window 根 bar 的典型价 (H+L+C)/3 按价格等分 bins 档、累计成交量，取最重
    那一档的中心价 —— 该处换手最充分，回踩常获支撑。峰值量占窗口总量不足 8%
    视为无显著密集区（分布太散，给出来反而误导），返回 None。
    """
    w = min(window, len(bars))
    if w < 5:
        return None
    seg = bars[-w:]
    lo = min(b["low"] for b in seg)
    hi = max(b["high"] for b in seg)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    buckets = [0.0] * bins
    for b in seg:
        p = (b["high"] + b["low"] + b["close"]) / 3
        buckets[min(bins - 1, int((p - lo) / step))] += float(b["volume"] or 0)
    total = sum(buckets)
    if total <= 0:
        return None
    peak = max(range(bins), key=lambda i: buckets[i])
    if buckets[peak] / total < 0.08:
        return None
    return lo + step * (peak + 0.5)


def _last_zt_low(bars: List[Dict], limit_pct: float, lookback: int = 20) -> Optional[float]:
    """最近一次涨停那根 bar 的最低价（首板起点；跌回此处通常是最后一道防线）。

    涨停判据与 tech_summary 的连板循环同口径（`limit_pct - 0.8` 容差），
    limit_pct<=0（新股无涨跌幅）时不给。
    """
    if limit_pct <= 0:
        return None
    n = len(bars)
    for i in range(n - 1, max(1, n - lookback), -1):
        pc = bars[i - 1]["close"]
        if pc and (bars[i]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
            return float(bars[i]["low"])
    return None


def _key_levels(bars: List[Dict], close: float, ma_map: Dict[str, Optional[float]],
                limit_pct: float) -> Dict[str, List[Dict[str, Any]]]:
    """客观关键价位候选（均线 / 近端低高点 / 跳空缺口 / 成交密集区 / 涨停起点）。

    只出「价格 + 类型 + 距离」，**不判断能不能撑住** —— 挑哪个位置当买点、
    止损放哪一档，由调用方结合大盘/板块/资金环境决定（2026-10-31 用户裁定：
    不该固定 MA5/MA10/MA20）。
    支撑与压力按现价分侧：同一价位在现价上方时是压力不是支撑。旧实现只取
    |dist|≤3% 且不分上下，会把上方均线也报成「支撑」。
    同价去重（差 <0.8% 视为同一位置）保留先出现的，优先序即 cands 的构造顺序。
    """
    lows = [b["low"] for b in bars]
    cands: List[tuple] = []          # (kind, px)
    for k in ("ma5", "ma10", "ma20", "ma60"):
        v = ma_map.get(k)
        if v:
            cands.append((k, float(v)))
    for w in (10, 20, 60):
        if len(bars) >= w:
            cands.append(("lo%d" % w, float(min(lows[-w:]))))
    if len(bars) >= 20:
        cands.append(("hi20", float(max(b["high"] for b in bars[-20:]))))
    gap = _open_gap_floor(bars)
    if gap:
        cands.append(("gap", gap))
    poc = _volume_poc(bars)
    if poc:
        cands.append(("poc", poc))
    zt_low = _last_zt_low(bars, limit_pct)
    if zt_low:
        cands.append(("zt", zt_low))

    sup: List[Dict[str, Any]] = []
    res: List[Dict[str, Any]] = []
    for kind, px in cands:
        if not px or px <= 0 or not close:
            continue
        if any(abs(o["px"] - px) / close * 100 < 0.8 for o in sup + res):
            continue                  # 同价只留优先序靠前的
        item = {"k": kind, "px": round(px, 2), "d": round((px - close) / close * 100, 2)}
        (res if px > close else sup).append(item)
    return {"supports": sorted(sup, key=lambda x: -x["d"])[:3],
            "resists": sorted(res, key=lambda x: x["d"])[:2]}


def regime_tag(run5: Optional[float], d0: float, d1: Optional[float],
               vr: Optional[float], cpos: Optional[float],
               rs_d0: Optional[float] = None) -> str:
    """形态标签（纯函数，无 IO）：一鼓作气/跳水回踩/高位分歧/再而竭/三而衰/弱转强/低位启动/低位蓄势/低位走弱。

    依据 2026-10-31 漏斗实测（tmp/out_winrate_funnel_20261031.json）+ 全市场验证
    （tmp/probe_sane_decay_20261031.py，325 笔/日）：触及10% 最高的是「转强|高位」56%，
    最差是「分歧|高位」T+3 均收 -6.4%；高位大跌按大盘归因拆分——rs_d0≥-1.5（跌主要来自
    大盘带崩）= 跳水回踩 T+3 +0.85%（n=35），显著跑输大盘才入三而衰 -3.35%（n=78）。
    ★「三而衰」本义=反弹波的高度与力度递减（波段结构，需 1m/多日判定，2026-10-31 用户指正；
    日线单日层只能做大盘归因，波段结构判定待实现）。标签是**描述性事实**（非准入闸）：
    供 LLM 分流动作——强→突破追、竭→禁追、疑→回踩支撑，判定仍归 LLM。
    """
    hi = run5 is not None and run5 > 10
    if hi:
        if d0 > 2:
            return "一鼓作气"
        if d0 < -2:
            if rs_d0 is not None and rs_d0 >= -1.5:
                return "跳水回踩"
            return "三而衰"
        if (vr is not None and vr < 0.9) or (cpos is not None and cpos < 40):
            return "再而竭"
        return "高位分歧"
    if d0 > 2:
        return "弱转强" if (d1 is not None and d1 < 0) else "低位启动"
    if d0 < -2:
        return "低位走弱"
    return "低位蓄势"


_BENCH_CACHE = {"t": None, "val": None}


def _bench_index() -> dict:
    """上证基准涨跌幅：d0=当日涨跌%、d5=近5日涨跌%；进程内 60s TTL；失败返回 {}。

    rs（个股相对大盘超额）的基准。IO 走 market_cn 门面（同 flow_summary/zt_index
    先例）。失败语义 = 缺键（rs 不进输出），不抛、不重试轰炸（失败也缓存 60s）。
    """
    now = datetime.now().timestamp()
    v = _BENCH_CACHE.get("val")
    if v is not None and _BENCH_CACHE.get("t") and now - _BENCH_CACHE["t"] < 60:
        return v
    out: dict = {}
    try:
        from app.market_cn.index import get_index_realtime, get_index_daily_kline
        rows = get_index_realtime(["000001"]) or []
        if rows and rows[0].get("change_percent") is not None:
            out["d0"] = float(rows[0]["change_percent"])
        kl = get_index_daily_kline("000001", days=8) or []
        closes = [float(b.get("close")) for b in kl if b.get("close")]
        if len(closes) >= 6 and closes[-6]:
            out["d5"] = round((closes[-1] / closes[-6] - 1) * 100, 2)
    except Exception as e:
        logger.warning("[MktScreen] 大盘基准获取失败: %s", e)
        out = {}
    _BENCH_CACHE["t"], _BENCH_CACHE["val"] = now, out
    return out


_MKT_CALIB_TTL_S = float(os.getenv("QD_SCREENER_MKT_CALIB_TTL", "60") or 0)
_MKT_CALIB_CACHE: Dict[str, Any] = {"key": None, "t": 0.0, "val": {}}


def market_calib_stats(same_t: Optional[str] = None, day: Optional[str] = None) -> Dict[str, Any]:
    """大盘两轴校准基准：vr=全市场量比、fp_pct=全市场净占比%（vr_rel/flow_rel 的分母/基准）。

    口径（2026-11-01 大盘校准探针同源）：vr=当日全市场成交量÷前5交易日均量；fp_pct=
    量价方向净额÷成交额×100（fund_flow_api._flow_from_1m_day 同规则，非主力口径）；
    fp_prev_pct=前5交易日净占比均值（「资金流和前几日同刻比对」用）。same_t="HH:MM"
    时量与净额双方只累计该时刻前（今日T前 vs 前几日T前，用户 9:50 口径）；day=个股
    日线末日（stale_bar 对齐用）。日级净占比优先读 qd_fund_flow_market（派生视图），
    缺日现算回填读 1m（同规则、只算不写，与 stock_fund_flow_history 同型；该表写入
    任务有断档，缺行须现算）。失败/缺数据返回 {}，下游相对字段缺键=未校准。
    进程内 TTL QD_SCREENER_MKT_CALIB_TTL 秒（0=关，每次直查）：同刻全市场一次 2~5s，
    per-stock 调用会乘 N，TTL 只做一次交付内去重（同 _bench_index 先例）。

    可改进点（未做）：目前只有全市场（大盘）一根校准基准，缺板块/概念维度——
    个股对所属板块/概念的 vr_rel/flow_rel（“板块弱个股强”才是板块内的真强）。
    逐票取板块归属再聚合板块量能/净额成本过高，故不做；待板块归属表或板块
    行情数据就绪后可在此函数旁加 market_calib_stats 的板块版，下游字段
    vr_rel/flow_rel 加 board_rel 同型扩展即可。
    """
    from datetime import timedelta
    key = (str(same_t or ""), str(day or ""))
    now = datetime.now().timestamp()
    if (_MKT_CALIB_TTL_S > 0 and _MKT_CALIB_CACHE["val"]
            and _MKT_CALIB_CACHE["key"] == key
            and now - _MKT_CALIB_CACHE["t"] < _MKT_CALIB_TTL_S):
        return _MKT_CALIB_CACHE["val"]
    out: Dict[str, Any] = {}
    try:
        from app.utils.db_market import get_market_db_manager
        from app.market_cn import fund_flow_api as _ffa
        pool = get_market_db_manager()._get_pool("CNStock")
        with pool.cursor() as c:
            c.execute(
                "SELECT \"time\"::date, SUM(volume) FROM \"kline_1D_2026\" "
                "WHERE \"time\" >= %s GROUP BY 1 ORDER BY 1",
                ((datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d"),))
            vols = {str(r[0]): float(r[1] or 0) for r in c.fetchall()}
        dlist = [d for d in sorted(vols) if vols[d] > 0]
        if day:
            dlist = [d for d in dlist if d <= str(day)[:10]]
        if len(dlist) >= 2:
            target, prev5 = dlist[-1], dlist[-6:-1]
            if same_t:
                tbl = _ffa._kline_1m_year_table(int(target[:4]))

                def _vol_until(dd: str) -> float:
                    with pool.cursor() as c:
                        c.execute(
                            f"SELECT COALESCE(SUM(volume),0) FROM {tbl} "
                            "WHERE \"time\" >= %s::timestamp AND \"time\" <= %s::timestamp",
                            (f"{dd} 00:00", f"{dd} {str(same_t)[:5]}"))
                        return float(c.fetchone()[0] or 0)

                tv = _vol_until(target)
                bvs = [_vol_until(d) for d in prev5]
            else:
                tv = vols.get(target) or 0.0
                bvs = [vols.get(d) or 0.0 for d in prev5]
            base = [b for b in bvs if b > 0]
            vr = round(tv / (sum(base) / len(base)), 2) if tv > 0 and base else None
            fp_map: Dict[str, Optional[float]] = {}
            if same_t:
                for d in [target] + prev5:
                    r = _ffa._flow_from_1m_day(d, until=str(same_t)[:5])
                    if r and r.get("main_pct") is not None:
                        fp_map[d] = float(r["main_pct"])
            else:
                with pool.cursor() as c:
                    c.execute(
                        "SELECT \"trade_date\"::text, \"main_net\", \"turnover\" "
                        "FROM \"qd_fund_flow_market\" WHERE \"trade_date\" >= %s",
                        (dlist[0],))
                    for td, mn, tn in c.fetchall():
                        if mn is not None and tn:
                            fp_map[str(td)] = round(float(mn) / float(tn) * 100.0, 2)
                for d in [target] + prev5:
                    if fp_map.get(d) is None:
                        r = _ffa._flow_from_1m_day(d)
                        if r and r.get("main_pct") is not None:
                            fp_map[d] = float(r["main_pct"])
            fp_base = [fp_map[d] for d in prev5 if fp_map.get(d) is not None]
            out = {
                "ok": True,
                "t": (str(same_t)[:5] if same_t else None),
                "day": target,
                "days_used": len(prev5),
                "vr": vr,
                "fp_pct": fp_map.get(target),
                "fp_prev_pct": (round(sum(fp_base) / len(fp_base), 2) if fp_base else None),
            }
    except Exception as e:
        logger.warning("[MktScreen] 大盘校准基准失败: %s", e)
        out = {}
    _MKT_CALIB_CACHE["key"], _MKT_CALIB_CACHE["t"], _MKT_CALIB_CACHE["val"] = key, now, out
    return out


def tech_summary(bars: List[Dict], code: str = "", name: str = "") -> Dict[str, Any]:
    """从日线算出「位置 / 量能 / 强度 / 涨停史 / 形态标签 / 相对大盘超额」的客观事实（不含评分）。

    之所以不含评分：评分语义已统一为 P(T+1 涨)×100（见 attach_prediction），
    这里的字段只是让 LLM 能解释「为什么」，或自行推翻自带的模型分。
    regime（2026-10-31）：形态标签，见 regime_tag；rs_d0/rs_5d = 个股涨跌 − 上证
    同期涨跌（大盘弱个股强=真强，用户口径），基准经 _bench_index（60s TTL，失败缺键）。
    vr_rel = 个股量比÷大盘量比（market_calib_stats 全市场口径，缺基准缺键）。
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

    zt_10d = 0
    if limit_pct > 0:
        win = min(10, len(bars) - 1)
        for i in range(len(bars) - win, len(bars)):
            pc = bars[i - 1]["close"]
            if pc and (bars[i]["close"] - pc) / pc * 100 >= limit_pct - 0.8:
                zt_10d += 1

    def _dist(ma_v):
        return round((close - ma_v) / ma_v * 100, 2) if ma_v and close else None

    dist_ma5 = _dist(_last(ma5))
    dist_ma10 = _dist(_last(ma10))
    # 关键价位候选（供 LLM 直接用，无需 read_skill / 拉 K 线）：不再只认三条均线
    ma60_v = _last(compute_ma(closes, 60))
    levels = _key_levels(bars, close,
                         {"ma5": _last(ma5), "ma10": _last(ma10),
                          "ma20": ma20_v, "ma60": ma60_v}, limit_pct)
    nearest = levels["supports"][0] if levels["supports"] else None

    intraday_pos = None
    rng = today["high"] - today["low"]
    if rng > 0:
        intraday_pos = (close - today["low"]) / rng * 100

    d1_chg = None
    if len(bars) >= 3 and bars[-3]["close"]:
        d1_chg = (prev["close"] - bars[-3]["close"]) / bars[-3]["close"] * 100
    bench = _bench_index()
    rs_d0 = round(chg - bench["d0"], 2) if bench.get("d0") is not None else None
    rs_5d = (round(ret5 - bench["d5"], 2)
             if (ret5 is not None and bench.get("d5") is not None) else None)
    mkt = market_calib_stats(day=(str(today.get("time") or today.get("date") or "")[:10] or None))
    vr_rel = round(vr / mkt["vr"], 2) if (vr and mkt.get("vr")) else None

    return {
        "ok": True,
        "bars": len(bars),
        "close": round(close, 2),
        "change_pct": round(chg, 2),
        "chg_prev_pct": round(d1_chg, 2) if d1_chg is not None else None,
        "amplitude_pct": round(rng / prev["close"] * 100, 2) if prev["close"] and rng else None,
        "close_pos_in_day": round(intraday_pos, 1) if intraday_pos is not None else None,
        "vol_ratio": round(vr, 2) if vr else None,
        "vr_rel": vr_rel,
        "rsi14": round(compute_rsi(closes)[-1], 1),
        "ma5": round(_last(ma5), 2) if _last(ma5) else None,
        "ma10": round(_last(ma10), 2) if _last(ma10) else None,
        "ma20": round(ma20_v, 2) if ma20_v else None,
        "dist_ma5_pct": dist_ma5,
        "dist_ma10_pct": dist_ma10,
        "dist_ma20_pct": round(dist_ma20, 2) if dist_ma20 is not None else None,
        "support": {
            # 只出候选列表 + 是否贴支撑：近端位置 = supports[0]，均线在 tech 顶层
            # （2026-10-31 去重：原 ma5/ma10/ma20 + nearest 三件套与这两处逐字重复，
            #  满卡每张省 ~285 字符，等于把第 6 张满卡换回来）
            "near_support": bool(nearest and abs(nearest["d"]) <= 3.0),  # |dist|<=3% 视为贴支撑
            "supports": levels["supports"],   # 现价下方候选，由近到远，≤3 条
            "resists": levels["resists"],     # 现价上方候选，由近到远，≤2 条
        },
        "ma_bull_stack": bool(_last(ma5) and _last(ma10) and ma20_v and _last(ma5) > _last(ma10) > ma20_v),
        "hi20": round(hi20, 2),
        "lo20": round(lo20, 2),
        "hi_today": round(today["high"], 2),
        "pos_in_20d_pct": round(pos20, 1) if pos20 is not None else None,
        "is_20d_high": bool(pos20 is not None and pos20 >= 99),
        "ret_5d_pct": round(ret5, 2) if ret5 is not None else None,
        "ret_20d_pct": round(ret20, 2) if ret20 is not None else None,
        "limit_up": bool(is_limit_locked(code, name, close, prev["close"])),
        "zt_streak": streak,
        "zt_10d": zt_10d,
        "prev_zt_streak": _prev_streak(bars, limit_pct, streak),
        "regime": regime_tag(ret5, chg, d1_chg, vr, intraday_pos, rs_d0),
        "rs_d0": rs_d0,
        "rs_5d": rs_5d,
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


# ── 开盘脉冲（拉高出货）识别阈值（2026-10-30 用户缺陷②）──────────────────
# 「开盘几千万拉升后抛盘」的客观形态：开盘窗大额净流入 → 其后转净流出 → 冲高回落。
# 阈值是**经验值**（只产 warnings，不进 hard）——形态是经验判断不是客观不可交易，
# 取舍仍归 LLM（设计原则见 SKILL.md「你的角色与自由度」）。
_OPEN_WINDOW_START = "09:30"   # 开盘窗起点（集合竞价后的连续竞价）
_OPEN_WINDOW_END = "10:00"     # 开盘窗截止时刻（早盘定调窗口，见 strategy-rules）
_PUMP_OPEN_MIN_WAN = 2000.0    # 开盘窗净流入下限（万元）≈「几千万级」脉冲
_PUMP_OPEN_SHARE_PCT = 60.0    # 开盘窗净额占全天净额比例上限（全天净流入为正时）
_PUMP_FADE_PCT = 3.0           # 现价距日内高点回落幅度（%）——冲高回落佐证
_PUMP_FADE_POS = 35.0          # 缺 rt 时用 close_pos_in_day ≤ 此值近似冲高回落


def _intraday_shape(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    """分钟净额 → 日内时段结构（开盘窗 vs 其后）。纯求和，无判断。

    开盘窗 = 09:30 ≤ t < `_OPEN_WINDOW_END`（左闭右开：bar 时间是**起始**时刻，
    10:00 起的 bar 属盘中段；strategy-rules「10:00 前基本定调」）。
    points 来自 tape.get_fund_flow_realtime 的 data：[{time:"YYYY-MM-DD HH:MM", main_net:元}]。
    open_share_pct = 开盘窗净额 ÷ 全天净额；其后净流出时可 >100%（脉冲全部被回吐），
    不是计算错误。
    """
    open_net = after_net = 0.0
    n = 0
    for p in points or []:
        t = str(p.get("time") or "")
        if len(t) < 16:
            continue
        v = _f(p.get("main_net"))
        if _OPEN_WINDOW_START <= t[11:16] < _OPEN_WINDOW_END:
            open_net += v
        else:
            after_net += v
        n += 1
    total = open_net + after_net
    return {
        "ok": n > 0,
        "open_net_wan": round(open_net / 1e4, 1),
        "after_net_wan": round(after_net / 1e4, 1),
        "open_share_pct": round(open_net / total * 100, 1) if total > 0 else None,
        "points": n,
    }


def pump_dump_risk(flow: Dict[str, Any], tech: Dict[str, Any] = None,
                   rt: Dict[str, Any] = None) -> List[str]:
    """开盘脉冲拉升后抛盘（拉高出货）风险线索 —— 经验型软提示（进 warnings，可被否决）。

    判据（2026-10-30 用户缺陷②；阈值见 _PUMP_* 常量，经验值非硬规则）：
      ① 开盘窗净流入 ≥ _PUMP_OPEN_MIN_WAN 且其后净额转负 —— 资金脉冲后撤；
      ② 开盘窗净额占全天 ≥ _PUMP_OPEN_SHARE_PCT 且其后净额为负 —— 早盘对倒特征；
      ③ 现价距日内高点回落 ≥ _PUMP_FADE_PCT（rt.high/price 优先；缺 rt 用
         tech.close_pos_in_day ≤ _PUMP_FADE_POS 近似）—— 冲高回落佐证；
      ④ 今日大额净流入但近 5 日累计净流出 —— 单日脉冲与多日趋势背离。
    ①/② 命中后再叠加 ③/④ 升级为「疑似拉高出货」；只描述形态，取舍归 LLM。

    Returns:
        list[str]: 风险线索（空 = 未命中；字段缺失按「判不了」处理，不硬凑）
    """
    out: List[str] = []
    if not isinstance(flow, dict) or not flow.get("ok"):
        return out
    intr = flow.get("intraday") if isinstance(flow.get("intraday"), dict) else {}
    if not intr.get("ok"):
        return out
    open_wan = _f(intr.get("open_net_wan"))
    after_wan = _f(intr.get("after_net_wan"))
    share = intr.get("open_share_pct")

    pulse = (open_wan >= _PUMP_OPEN_MIN_WAN or
             (share is not None and _f(share) >= _PUMP_OPEN_SHARE_PCT))
    if not (pulse and after_wan < 0):
        return out
    out.append(f"开盘脉冲：{_OPEN_WINDOW_END} 前净流入 {open_wan:.0f} 万，其后净流出 {abs(after_wan):.0f} 万")

    # ③ 冲高回落佐证
    fade_note = None
    if rt:
        high, price = _f(rt.get("high")), _f(rt.get("price"))
        if high > 0 and price > 0:
            fade = (high - price) / high * 100
            if fade >= _PUMP_FADE_PCT:
                fade_note = f"冲高回落 {fade:.1f}%（较日内高点）"
    elif isinstance(tech, dict) and tech.get("close_pos_in_day") is not None \
            and _f(tech.get("close_pos_in_day")) <= _PUMP_FADE_POS:
        fade_note = f"收于日内低侧 {tech['close_pos_in_day']}%（冲高回落近似）"
    if fade_note:
        out.append(fade_note + " → 疑似拉高出货")

    # ④ 单日脉冲与多日趋势背离
    rec = flow.get("recent") if isinstance(flow.get("recent"), dict) else {}
    net5 = rec.get("net_5d_wan")
    if net5 is not None and _f(net5) < 0 and _f(flow.get("net_wan")) >= _PUMP_OPEN_MIN_WAN:
        out.append(f"今日脉冲与近5日净流出 {_f(net5):.0f} 万背离（趋势未转向）")
    return out


# ── 封板结构（客观事实 + 硬标签，**不打分、不排序**）─────────────────────
# 2026-10-30 用户裁定：盘后/尾盘的决策场景是「次日开盘按涨停价挂单」，买不到不是
#   决策问题 ⇒ 不做「可买性」维度（原 buy_ease 摩擦分已删）。封板结构改为只给
#   事实与标签，由 LLM 估算次日强弱。
# 标签边界来自次日表现实证（最近 8 个已收盘交易日 426 只涨停票，本地日线算
#   次日开盘/收盘相对当日收盘，均值，括号内为次日收盘中位）：
#     0 开板    +1.79%（+1.94%）  n=192     1-2 开板  +1.11%（+0.82%）  n=149
#     3-5 开板  +0.82%（-0.28%）  n=55      6+ 开板   -0.57%（-2.18%）  n=30
#   封单÷成交额同样单调：≤5% -0.62% / 5-15% +1.20% / 15-40% +1.84% / 40%+ +3.51%。
#   连板高度非单调：2 板最弱（中位 -1.69%，n=61），4+ 板最强（中位 +7.56%，n=16，
#   样本小不可当定论）。⇒ 开板多=差、封单厚=好，与「可买性」正好相反。
# 复现：tmp/probe_open_count_nextday_20261030.py、tmp/probe_ease_inputs_20261030.py
# 标签命名刻意**不含价值判断**（曾是 solid/reseal/weak，实测 000568 出现「0 开板但
#   封单仅 0.38%」——叫 solid 会把最可疑的组合读成最扎实）：tag 只回答开板次数，
#   seal_tier 只回答封单厚薄，两个正交事实交给 LLM 组合。
_SEAL_TAG_ENV = "QD_SCREENER_SEAL_TAG"
_SEAL_TAG_DEFAULT: Dict[str, Any] = {
    "reseal_max": 2,          # 开板 ≤2 次 → open1_2（实证 1-2 次档）
    "loose_max": 5,           # 开板 ≤5 次 → open3_5（实证 3-5 次档）；>5 → open6p
    "seal_tiers": [5, 15, 40],  # 封单÷成交额 ≤n% → thin / mid / thick / vthick
}
_ZT_INDEX_TTL_S = 60.0
_ZT_INDEX_CACHE: Dict[str, Any] = {"day": "", "at": datetime.now(), "map": None}


def _seal_tag_rules() -> Dict[str, Any]:
    """封板标签边界：缺省 dict 被 env JSON 局部覆盖（未知键忽略，坏 JSON 告警不改行为）。"""
    rules = dict(_SEAL_TAG_DEFAULT)
    raw = (os.getenv(_SEAL_TAG_ENV, "") or "").strip()
    if not raw:
        return rules
    try:
        extra = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("[MktScreen] %s 非法 JSON，按缺省: %s", _SEAL_TAG_ENV, e)
        return rules
    if isinstance(extra, dict):
        rules.update({k: v for k, v in extra.items() if k in rules})
    return rules


def _empty_pool_reason(day: str) -> str:
    """池为空的成因判据（2026-10-31）。

    三种成因的处置**完全不同**，旧文案一律写「进程未热?」是错的归因：
      · 非交易日        —— 该日根本没有池，属日期口径错（周末提问的典型症状）；
      · 交易日·盘中     —— 池未产出或仍在变动，属正常，不必告警级别；
      · 交易日·已收盘   —— akshare 失败也表现为 []，是真失败，须进 missing。
    """
    try:
        from app.utils.trading_calendar import is_trading_day, is_in_quote_session
    except Exception as e:            # 日历不可用：归因不明就照实说，不猜
        return f"日历不可用，无法判定: {type(e).__name__}"
    try:
        if not is_trading_day(day):
            return "非交易日：该日无池"
        if day == datetime.now().strftime("%Y-%m-%d") and is_in_quote_session():
            return "交易日·盘中：池未产出或仍在变动"
        return "交易日·非报价时段（开盘前/午休/收盘后）：源返回空（源故障亦表现为 []）"
    except Exception as e:
        return f"日历判定异常: {type(e).__name__}: {e}"


def zt_index(date: str = "") -> Dict[str, Dict[str, Any]]:
    """涨停池 → {code: 池行}（含 open_count/seal_amount/amount/turnover_rate）。

    ★ 2026-10-31：默认日期由「今天」改为**目标交易日** `common._data_day()`
      （今日非交易日 → 最近一个交易日）。原口径在周末/节假日恒取到空池，
      而同一时刻日线/资金流/画像用的都是上一交易日的数据 ⇒ 口径错位，
      封板结构（open_count/seal_amount）恒 unknown。实测对照（2026-10-31）：
      `get_zt_pool("2026-10-30")` = 47 只带 open_count/seal_amount，
      `get_zt_pool("2026-10-31")`（周六）= 0。

    取数：历史日走 coord_pool 按日拉取 + 落盘缓存；当日盘中走实时接口。
    60s 内存 memo：run() 内 profile 会调多次，池本身由源侧刷新。
    空池按「开板/封单未知」降级，不得把空 map 当成「该日没涨停」。
    """
    from .common import fetch_zt_pool, _data_day
    day = (date or "").strip() or _data_day()
    now = datetime.now()
    if _ZT_INDEX_CACHE.get("day") == day and _ZT_INDEX_CACHE.get("map") is not None:
        age = (now - _ZT_INDEX_CACHE["at"]).total_seconds()
        if age < _ZT_INDEX_TTL_S:
            return _ZT_INDEX_CACHE["map"]
    try:
        pool = fetch_zt_pool(day) or []
    except Exception as e:
        logger.warning("[MktScreen] 涨停池不可用（封板结构降级）: %s", e)
        return {}
    if not pool:
        logger.warning("[MktScreen] 涨停池 %s 为空（%s）: 封板结构只剩连板数",
                       day, _empty_pool_reason(day))
    out = {str(r.get("stock_code") or ""): r for r in pool if r.get("stock_code")}
    _ZT_INDEX_CACHE.update({"day": day, "at": now, "map": out})
    return out


def seal_summary(sealed: Optional[bool], zt_streak: int = 0,
                 zt_row: Dict[str, Any] = None, rt: Dict[str, Any] = None,
                 turnover_pct: Optional[float] = None) -> Dict[str, Any]:
    """封板结构的客观事实 + 硬标签（只描述「今天封得怎么样」，**不打分、不排序**）。

    输入全部是客观值，无新取数：`zt_row` 来自涨停池（开板次数/封单额/成交额/换手/
    连板数），`rt` 给一字判定（low ≈ 涨停价），`zt_streak` 是日线连板兜底。

    标签（边界见 `_SEAL_TAG_DEFAULT`，来自次日表现实证，见上方常量块注释）。
    `tag` 只切开板次数，`seal_tier` 只切封单厚薄——**两个正交事实，都不含价值判断**
    （实测存在「0 开板但封单仅 0.38%」这种组合，任何单向命名都会误导）：
      · `tag=none`      未封板（其余字段照实给，不受影响）
      · `tag=one_word`  日内最低价 = 涨停价（一字/全天封死，实证次日最强档）
      · `tag=open0`     0 次开板但非一字（盘中封住后未再打开）
      · `tag=open1_2`   1~2 次开板后回封
      · `tag=open3_5`   3~5 次开板（反复开合）
      · `tag=open6p`    ≥6 次开板（尾盘勉强封住，实证次日唯一负收益档）
      · `tag=unknown`   实时涨跌停未知（缺 rt），或封板但涨停池缺该票（开板次数未知）
      · `seal_tier`     thin(≤5%) / mid(≤15%) / thick(≤40%) / vthick(>40%)
                        ——按实证单调性，thin 最弱（次日 -0.62%）、vthick 最强（+3.51%）

    为什么给标签而不给分数：开板次数 / 封单厚度 / 连板高度三项高度共线（一字板三项
    同时极端），合成会重复计权；权重怎么拍都缺样本支撑（4+ 板仅 16 只）。给事实比给
    权重更利于 LLM 估算，也不违反「不做主观评分梯形」。

    Args:
        sealed: 是否涨停封板（rt.limit_up）；None = 缺 rt，不猜
        zt_streak: 日线连板数（涨停池缺该票时的兜底）
        zt_row: 涨停池该票一行（open_count/seal_amount/amount/turnover_rate/
            continuous_zt_days）
        rt: 实时快照（要 low 与 limit_up_price 判一字）
        turnover_pct: 换手率（传了优先，否则取池里的 turnover_rate）

    Returns:
        dict: {tag, seal_tier, open_count, seal_ratio_pct（封单÷成交额 %）, zt_days,
            turnover_pct}；取不到的一律 None，不回落 0。
    """
    rules = _seal_tag_rules()
    row = zt_row if isinstance(zt_row, dict) else {}
    _rt = rt if isinstance(rt, dict) else {}

    days = max(int(_f(zt_streak)), int(_f(row.get("continuous_zt_days"))))
    open_count = None if row.get("open_count") is None else int(_f(row.get("open_count")))
    seal_pct = None
    _seal, _amt = _f(row.get("seal_amount")), _f(row.get("amount"))
    if _seal > 0 and _amt > 0:
        seal_pct = round(_seal / _amt * 100, 2)
    turn = turnover_pct if turnover_pct is not None else row.get("turnover_rate")
    turn = round(_f(turn), 2) if turn is not None else None

    seal_tier = None
    if seal_pct is not None:
        _cuts = [float(x) for x in (rules.get("seal_tiers") or [])]
        seal_tier = ("thin" if seal_pct <= _cuts[0] else
                     "mid" if seal_pct <= _cuts[1] else
                     "thick" if seal_pct <= _cuts[2] else "vthick")

    one_word = None
    _lo, _up = _f(_rt.get("low")), _f(_rt.get("limit_up_price"))
    if _lo > 0 and _up > 0:
        one_word = _lo >= _up - 0.01

    if sealed is None:
        tag = "unknown"
    elif not sealed:
        tag = "none"
    elif one_word:
        tag = "one_word"
    elif open_count is None:
        tag = "unknown"
    elif open_count <= 0:
        tag = "open0"
    elif open_count <= int(rules["reseal_max"]):
        tag = "open1_2"
    elif open_count <= int(rules["loose_max"]):
        tag = "open3_5"
    else:
        tag = "open6p"

    # 未封板 / 缺 rt：封单厚薄没有意义（涨停池里若残留该票的旧封单会造出
    # 「未封板 + vthick」这种矛盾组合），一律置空。
    if sealed is not True:
        seal_tier = None

    return {"tag": tag, "seal_tier": seal_tier, "open_count": open_count,
            "seal_ratio_pct": seal_pct, "zt_days": days or None, "turnover_pct": turn}


def same_time_stats(code: str) -> Dict[str, Any]:
    """同时刻横向比对（盘中直接参考）：今日截至 T 的量能与相对大盘超额，对比前几日同一时刻。

    用户口径（2026-10-31）：大盘弱个股强=真强、大盘强个股弱=真弱；「同一时刻」
    （而非全天）的量比才有盘中直接参考价值。T = 今日最新 1m bar 时刻（盘后调用
    即≈全天，退化为普通对比，无害）。数据走门面 1m（本地 DB 优先，个股+上证各一次）。
    vr_same_t = 今日 T 前累计量 ÷ 前N日(≤5)同一时刻累计量均值；ret_same_t /
    idx_ret_same_t = 双方昨收→T 涨幅；rs_same_t = 前者−后者。fp_same_t = 今日 T 前
    量价方向净占比%（_flow_from_1m_day 同规则）；vr_rel_same_t / flow_rel_same_t =
    个股同刻量比÷大盘同刻量比、个股同刻净占比−大盘同刻净占比（三轴校准的同刻版，
    大盘侧 market_calib_stats）。
    失败语义 = ok:False + reason，不抛（inspect 超时留痕由调用方负责）。
    """
    from zoneinfo import ZoneInfo
    _bj = ZoneInfo("Asia/Shanghai")
    try:
        from app.data_sources.cn_stock import CNStockDataSource
        raw = CNStockDataSource().get_kline(code, "1m", 1500) or []
    except Exception as e:
        return {"ok": False, "reason": f"1m源失败:{type(e).__name__}"}

    days: Dict[str, list] = {}
    for k in raw:
        if not isinstance(k, dict) or not k.get("time") or k.get("volume") is None:
            continue
        try:
            ts = datetime.fromtimestamp(int(k["time"]), tz=_bj)
        except (ValueError, OSError, OverflowError, TypeError):
            continue
        days.setdefault(ts.strftime("%Y-%m-%d"), []).append(
            (ts.strftime("%H:%M"), float(k["volume"]), float(k.get("close") or 0),
             float(k.get("open") or 0)))
    if len(days) < 2:
        return {"ok": False, "reason": "1m不足两日"}

    dlist = sorted(days)
    today = dlist[-1]
    prevs = dlist[max(1, len(dlist) - 6):len(dlist) - 1]
    t = days[today][-1][0]

    def _cum(d: str) -> float:
        return sum(row[1] for row in days[d] if row[0] <= t)

    def _day_ret(d: str) -> Optional[float]:
        seq = days[d]
        if seq and seq[0][2] and seq[-1][2]:
            return (seq[-1][2] / seq[0][2] - 1) * 100
        return None

    prev_cums = [_cum(d) for d in prevs]
    prev_cums = [c for c in prev_cums if c > 0]
    if not prev_cums:
        return {"ok": False, "reason": "前日同时刻量缺失"}
    today_cum = _cum(today)
    if today_cum <= 0:
        return {"ok": False, "reason": "今日量缺失"}
    out: Dict[str, Any] = {
        "ok": True,
        "t": t,
        "days_used": len(prev_cums),
        "vr_same_t": round(today_cum / (sum(prev_cums) / len(prev_cums)), 2),
        "ret_same_t": (round(_day_ret(today), 2) if _day_ret(today) is not None else None),
    }
    try:
        from app.market_cn.index import get_index_kline
        iraw = get_index_kline("000001", frequency="1m", days=8) or []
        idays: Dict[str, list] = {}
        for k in iraw:
            if not isinstance(k, dict) or not k.get("time"):
                continue
            try:
                ts = datetime.fromtimestamp(int(k["time"]), tz=_bj)
            except (ValueError, OSError, OverflowError, TypeError):
                continue
            idays.setdefault(ts.strftime("%Y-%m-%d"), []).append(
                (ts.strftime("%H:%M"), float(k.get("close") or 0)))
        idl = sorted(idays)
        if len(idl) >= 2 and today in idays:
            it, ip = idl[-1], idl[-2]
            t_i = min(t, idays[it][-1][0])
            i_now = next((c for h, c in reversed(idays[it]) if h <= t_i), None)
            i_prev = idays[ip][-1][1] if idays[ip] else None
            if i_now and i_prev:
                idx_ret = (i_now / i_prev - 1) * 100
                out["idx_ret_same_t"] = round(idx_ret, 2)
                if out.get("ret_same_t") is not None:
                    out["rs_same_t"] = round(out["ret_same_t"] - idx_ret, 2)
    except Exception as e:
        logger.warning("[MktScreen] 同时刻指数比对失败: %s", e)

    def _fp(d: str) -> Optional[float]:
        net = 0.0
        amt = 0.0
        ref = None
        for h, v, c, o in sorted(days[d]):
            if h > t:
                break
            if v <= 0 or c <= 0:
                continue
            r = ref if ref is not None else o
            if c > r:
                net += v * c
            elif c < r:
                net -= v * c
            amt += v * c
            ref = c
        return round(net / amt * 100, 2) if amt else None

    out["fp_same_t"] = _fp(today)
    mkt = market_calib_stats(same_t=t)
    if mkt:
        if out.get("fp_same_t") is not None and mkt.get("fp_pct") is not None:
            out["flow_rel_same_t"] = round(out["fp_same_t"] - mkt["fp_pct"], 2)
        if out.get("vr_same_t") and mkt.get("vr"):
            out["vr_rel_same_t"] = round(out["vr_same_t"] / mkt["vr"], 2)
        out["mkt_vr"] = mkt.get("vr")
        out["mkt_fp_pct"] = mkt.get("fp_pct")
        out["mkt_fp_prev_pct"] = mkt.get("fp_prev_pct")
    return out


def flow_summary(code: str) -> Dict[str, Any]:
    """个股资金流客观事实（单位：万元）：当日 + 近5/20日总体 + 日内时段结构。

    2026-10-30 扩展（用户缺陷①②：只看当天看不出资金「总体」，也防不住开盘几千万
    拉升后抛盘）—— 两个既有工具各取所需，不做重复聚合：
      · `get_fund_flow_realtime`                当日分钟净额 → `intraday`（开盘/其后分段）
      · `get_fund_flow(scope="stock_daily", days=30)` 日线聚合 → `recent`（5/20日累计、
        连续方向、逐日符号、剔除最大单日、上一完整交易日、资金趋势判定）
    失败返回 ok=False + 原因，不静默；recent / intraday 各自带 ok，单块失败不拖垮另一块。

    口径提醒（勿按「主力资金」读取）：net_wan 是**量价方向净额**（按分钟涨跌方向
    累计成交额，见 fund_flow_tools 头注），不是东财四档主力净额，两者不可换算；
    `caliber`/`caliber_note` 两个键把这件事写进载荷本身，键名不再自称 main_net。
    跨票比较用 *_pct，跨日看方向序列，绝对额只作门槛。

    Returns:
        {ok, net_wan, caliber, caliber_note, net_pct_of_amount,
         recent: {ok, net_5d_wan, net_20d_wan, ratio_5d_pct, ratio_20d_pct,
                  streak_dir, streak_days, sign_5d, max_day, net_20d_ex_max_wan,
                  prev_day_net_wan, trend},
         intraday: {ok, open_net_wan, after_net_wan, open_share_pct, points},
         reason?}
    """
    out: Dict[str, Any] = {}
    # ── 当日：分钟净额（realtime_snapshot 量价派生）──
    try:
        from .common import call_tool
        ff = call_tool("get_fund_flow_realtime", code=code)
        if not isinstance(ff, dict) or ff.get("error"):
            out["reason"] = (ff or {}).get("error", "无返回")
        else:
            # 三对账修正（2026-09-30）：tape 返回的真实键是 total_main_net（单位元）——
            # 旧读 main_net_inflow 恒 None，主力净流入被算成恒 0。2026-10-30 补：占比
            # 一直为 None 的根因是键名取错——该返回带 main_pct / total_amount，直接读。
            main_net = _f(ff.get("total_main_net", ff.get("main_net_inflow"))) / 1e4
            pct = _num_or_none(ff.get("main_pct"))
            if pct is None:
                amount = _f(ff.get("total_amount") or ff.get("amount") or ff.get("turnover"))
                pct = round(main_net * 1e4 / amount * 100, 2) if amount else None
            out["net_wan"] = round(main_net, 1)
            out["net_pct_of_amount"] = pct
            out["intraday"] = _intraday_shape(ff.get("data") or [])
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"

    # ── 近5/20日总体：日线聚合（含资金趋势判定，stock_daily dispatch 已附）──
    fp_today = None
    fp_day = ""
    try:
        from ...tools.finance.fund_flow_tools import get_fund_flow
        df = get_fund_flow(scope="stock_daily", codes=code, days=30) or {}
        one = (df.get("data") or {}).get(code) or {}
        if isinstance(one, dict) and not one.get("error"):
            rows = one.get("data") or []
            today = datetime.now().strftime("%Y-%m-%d")
            prev_wan = None
            for r in reversed(rows):
                if str(r.get("date") or "")[:10] < today and r.get("main_net") is not None:
                    prev_wan = round(_f(r.get("main_net")) / 1e4, 1)
                    break
            fp_day = max((str(r.get("date") or "")[:10] for r in rows), default="")
            for r in rows:
                if str(r.get("date") or "")[:10] != fp_day:
                    continue
                v = r.get("main_pct")
                if v is None and r.get("main_net") is not None and _f(r.get("turnover")):
                    v = _f(r["main_net"]) / _f(r["turnover"]) * 100
                if v is not None:
                    fp_today = float(v)
                    break
            out["recent"] = {
                "ok": True,
                "net_5d_wan": round(_f(one.get("recent_5d_main_net")) / 1e4, 1),
                "net_20d_wan": round(_f(one.get("recent_20d_main_net")) / 1e4, 1),
                "ratio_5d_pct": one.get("recent_5d_net_ratio"),
                "ratio_20d_pct": one.get("recent_20d_net_ratio"),
                "streak_dir": one.get("main_net_streak_dir"),
                "streak_days": one.get("main_net_streak"),
                "sign_5d": one.get("recent_5d_sign"),
                "max_day": one.get("max_main_net_day"),
                "net_20d_ex_max_wan": round(_f(one.get("recent_20d_main_net_ex_max")) / 1e4, 1),
                "prev_day_net_wan": prev_wan,
                "trend": one.get("资金趋势判定"),
            }
        else:
            out["recent"] = {"ok": False, "reason": (one or {}).get("error", "无返回")}
    except Exception as e:
        out["recent"] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    if fp_today is not None:
        _mkt = market_calib_stats(day=(fp_day or None))
        if _mkt.get("fp_pct") is not None:
            out["flow_rel"] = round(fp_today - _mkt["fp_pct"], 2)

    out["caliber"] = "price_direction"
    out["caliber_note"] = "量价方向净额（非主力）"
    out["ok"] = "net_wan" in out
    return out


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
    # 「涨停买不进」只在盘中此刻成立（2026-10-30 用户裁定）：intraday 排，
    # eod/post_market 一律放行（尾盘与盘后都是次日承接视角，不是当下成交）。
    exclude_limit = strategy == "intraday"
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
    """客观优先级键（2026-10-21）：纯计数分层，**不编造分数**——p_up 仍是唯一数字。

    统一供两处「谁先获得资源/名额」的排序（同一口径，勿另写）：
      · 池子超过打分护栏时，谁先拿 p_up（score_pool 预算让位）；
      · 缺分（no_pred）票按什么顺序占用保送名额（缺分保送）。
    分层（越小越优先）：题材命中 0 → 稀缺来源(龙回头/热点) 1 →
    多源命中(source 含 +) 2 → 其余 3；同层按输入顺序（index）稳定排，可复盘。
    题材命中：画像后的 item 自带 theme.hit；画像前的原始候选无此字段，回退
    _theme_hit 现算（同一函数，口径一致）。
    """
    theme = item.get("theme")
    hit = theme.get("hit") if isinstance(theme, dict) \
        else _theme_hit(item, list(themes))["hit"]
    source = str(item.get("source") or "")
    if hit:
        tier = 0
    elif "龙回头" in source or "热点" in source:
        tier = 1
    elif "+" in source:
        tier = 2
    else:
        tier = 3
    return (tier, index)


def score_pool(candidates, strategy="", market=None, main_themes=None,
               p_up_floor=0.48, rank_cut=0.30, floor_keep=4, no_pred_keep=2,
               reserved=None):
    """双保险软门槛 + 缺分保送——run() 漏斗第 2 层（**加法层**）。

    层次（2026-10-25 起与 8.1.0 基线分层，见 run._baseline_selection）：
    1. **hard**：画像层已标的客观障碍，本层计数不放进 deep；
    2. **p_up 硬闸**：低于 p_up_floor 的进 cut_list（计 p_up_hard）；
    3. **排名截断**：过闸票按 p_up 降序砍后 rank_cut 比例（计 rank_cut）；
    4. **两条回捞通道**（都只加不减）：
       · floor_kept：被砍但题材命中或稀缺来源（龙回头/热点）的前 floor_keep 只；
       · no_pred_kept：无 p_up（模型盲区）按客观优先级占 no_pred_keep 名额
         （2026-10-21：缺分 ≠ 低分）；

    【reserved（2026-10-25 用户裁定：8.4.0 = 8.1.0 胜率的再提高，不是重新设计）】
    reserved = 8.1.0 基线画像（run._baseline_selection 产出：池序前 8 无 hard）。
    基线**恒入 deep**，不参与本层的闸/排名/回捞——本层只在基线之外做加法
    （p_up 排序的改进名额，填到 run._FUNNEL_DEEP_N）。按 code 去重：基线与
    本层是两次独立画像的 dict 对象（同票不同体），且本层候选集（objective
    之后的 kept）通常仍含基线票，必须先剔除再进预算/画像，否则基线票会
    ① 白占打分护栏额度 ② 以加法身份重复进 deep。

    【rank_cut 定位】是**数量控制**（限制进 deep 的条数），不是质量否决。
    相对截断（砍排名后 X%）意味着同一只票在强池出局、弱池存活——两条回捞通道
    （floor/no_pred）为此而设。要绝对门槛请调 p_up_floor。

    Args:
        reserved: 8.1.0 基线画像列表（dict 带 code/hard）；其 code 在本层
            全程排除（不占预算、不画像、不进任何名单）。None=无保留（旧行为）
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
        # 口径 = 加法层候选（reserved 已剔除后的 cand_list）超预算的部分——
        # reserved 是基线保底，不占打分额度，不计让位（2026-10-25）。
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

    # 保底回捞：题材命中或稀缺来源（龙回头/热点）
    rescued = []
    for p in cut_list:
        theme = p.get("theme") or {}
        source = str(p.get("source") or "")
        scarce = ("龙回头" in source) or ("热点" in source)
        if theme.get("hit") or scarce:
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

    # 有分票按 p_up 降序在前，缺分保送殿后
    selected = kept + floor_kept + no_pred_kept
    selected.sort(key=lambda x: (x.get("p_up") is not None, x.get("p_up") or 0),
                  reverse=True)
    stats = {
        "hard": hard_n,
        "p_up_hard": p_up_hard_n,
        "rank_cut": rank_cut_n,
        "no_pred": len(no_pred_list),
        "no_pred_kept": len(no_pred_kept),
        "floor_kept": len(floor_kept),
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

    · row["rt"]：实时快照（缺 rt 时不写，不塞 None 壳）；含 high/low（2026-10-30 补，
      供一字判定与冲高回落——此前 rt 无 high，pump_dump_risk 的该分支从未命中）
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
        # high/low 2026-10-30 补：① 一字判定（low≈涨停价 → 买不到）；② pump_dump_risk
        # 的冲高回落分支读 rt.high，此前 rt 从不带 high ⇒ 该分支恒不命中（静默失效）。
        "high": rt.get("high"),
        "low": rt.get("low"),
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
    ~69s：龙回头 akshare + 行情回填）无依赖 ⇒ run() 入口起后台线程
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
    zt_map = zt_index()   # 封板结构要用池里的开板次数/封单额，缺了按 unknown 降级
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
            "seal": seal_summary(
                # 日线末根封板（盘中有 rt 时 deep 层会用实时口径重算，这里只做画像层估计）
                sealed=(None if item.get("limit_locked") is None
                        else bool(item.get("limit_locked"))),
                zt_streak=tech.get("zt_streak") or 0,
                zt_row=zt_map.get(code),
                turnover_pct=item.get("turnover_pct")),
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
                else:
                    # 拉高出货线索（2026-10-30 缺陷②）：并入 warnings 由 LLM 定夺
                    for w in pump_dump_risk(p["flow"], tech=p.get("tech")):
                        if w not in p["warnings"]:
                            p["warnings"].append(w)
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
    """给 LLM 的参考门槛（不是硬规则）：弱势市/下跌环境提高胜率要求、减只数。

    2026-10-30（用户缺陷③）：market.cautious=True（指数弱于开盘或资金连续流出）
    时在情绪分桶之上**再收紧一档**——下跌时选股应更谨慎，早盘尤其如此。
    """
    base = {"weak": 0.55, "neutral": 0.50, "strong": 0.48}.get(regime, 0.50)
    caps = {"weak": 5, "neutral": 8, "strong": 10}
    cautious = bool((market or {}).get("cautious"))
    if cautious:
        base = {"weak": 0.58, "neutral": 0.54, "strong": 0.52}.get(regime, 0.54)
    picks = max(3, caps.get(regime, 8) - (2 if cautious else 0))
    note = (
        "门槛/条数是历史均值经验值，非硬约束：若候选普遍高 p_up 且题材共振，可适当放宽；"
        "若 profile 里 missing 很多（数据缺口），应主动降低给出的置信度"
    )
    if cautious:
        note += "；大盘走弱（cautious）：提高门槛、减少只数、只做有支撑的票，早盘勿抢反弹"
    return {
        "p_up_floor_suggest": base,
        "max_picks_suggest": picks,
        "cautious": cautious,
        "note": note,
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
