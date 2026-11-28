# -*- coding: utf-8 -*-
"""
market-screener/_facts.py — 纯客观事实层（无评分、无取舍、无阈值）

2026-09-29 v7：原 intraday.py / eod.py / post_market.py 三个「策略模块」把四样东西
  混在一个文件里 —— ① 真正的客观计算 ② 选股规则 ③ 评分梯形 ④ 报告口径。
  其中 ②③④ 让 LLM 只能消费「别人替它做好的结论」，自由度≈0。本轮拆分：

    客观计算 → 本文件（LLM 难以自行稳定复算，且算错了没人知道）
    规则     → references/strategy-rules.md（由 LLM 读取、采纳或推翻）

本文件**禁止出现**：入选阈值、评分权重、截断条数、买卖建议、方向判断。
形态识别里的参数是该形态的**客观定义**（例如「平台」= 振幅 <8%），不是推荐标准。

已知数据源事实（2026-09-29 实测，勿凭记忆改）：
  - 分钟源只有 `1m` 有数据；`15m` / `5m` / `30m` / `60m` 查询均返回 0 行
    （历史裁定：kline_15m 已回填至 kline_1m 并标 void）
  - 因此多周期分析一律取 `1m` 后**本地聚合**成 15m / 5m，不得直接查其它周期

注意：本目录 .py 行尾为 CRLF（common.py 例外为 LF），改前用 read_bytes() 判定。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ...log import logger

from .common import (
    fetch_kline, _data_day, fetch_hot_sectors, _get_writer,
    compute_ma, compute_macd, compute_rsi, compute_volume_ratio,
    fetch_zt_pool, fetch_dt_pool, fetch_broken_board,
)


# ═══════════════════════════════════════════════════════════════
#  分钟线：唯一可用源是 1m，其余周期本地聚合
# ═══════════════════════════════════════════════════════════════

# 实测结论：只有 1m 有数据。写死在这里防止后人再踩 15m 空表的坑。
_MINUTE_SOURCE = "1m"


def intraday_bars(code: str, days: int = 5) -> List[Dict]:
    """取该股最近若干个自然日的 1 分钟 K 线（升序）。

    Args:
        code: 6 位股票代码
        days: 回溯自然日，默认 5

    Returns:
        list: [{time, open, high, low, close, volume}]，取不到返回 []
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    try:
        writer = _get_writer()
        data = writer.query("CNStock", code, _MINUTE_SOURCE, start_time=start, end_time=end, limit=0)
        if not data:
            return []
        return [{
            "time": str(r["time"]), "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]),
        } for r in data]
    except Exception as e:
        logger.debug("[MktScreen] %s 分钟线获取失败: %s", code, e)
        return []


def resample(bars: List[Dict], minutes: int) -> List[Dict]:
    """把 1 分钟 K 线按固定根数聚合为 N 分钟 K 线（客观换算，无加工）。

    桶边界按「每日开盘 09:30 起的第 k×minutes 根」对齐
    （slot = (hh*60+mm - 570) // minutes），不从序列首根顺序切——首根缺失
    （DB 缺开盘段）时顺序切会让全部桶整体错位 minutes-1 分钟（2026-11-25
    审查修复）。午休 13:00 与跨日因 slot 跳变自然开新桶，无需显式重置；
    时间戳无法解析时整体退回旧顺序分桶（避免混桶）。

    Args:
        bars: 1m K 线（升序）
        minutes: 聚合根数，例如 15 / 5

    Returns:
        list: 聚合后的 K 线（升序）；盘中截断的尾桶丢弃，已走完的桶照常交付
    """
    minutes = max(1, int(minutes))
    rows = [b for b in bars or [] if isinstance(b, dict)]

    def _slot(b: Dict) -> Optional[int]:
        t = str(b.get("time") or "")
        try:
            return (int(t[11:13]) * 60 + int(t[14:16]) - 570) // minutes  # 570=09:30
        except ValueError:
            return None

    def _flush(seq: List[Dict]) -> Dict:
        return {
            "time": seq[0]["time"],
            "open": seq[0]["open"],
            "high": max(x["high"] for x in seq),
            "low": min(x["low"] for x in seq),
            "close": seq[-1]["close"],
            "volume": sum(x["volume"] for x in seq),
        }

    slots = [_slot(b) for b in rows]
    aligned = all(s is not None for s in slots)
    out: List[Dict] = []
    chunk: List[Dict] = []
    prev: Optional[int] = None
    for i, b in enumerate(rows):
        key = slots[i] if aligned else i // minutes
        if chunk and key != prev:
            out.append(_flush(chunk))
            chunk = []
        prev = key
        chunk.append(b)
    # 尾桶交付判据（与旧行为「尾部不满一桶丢弃」对齐）：
    #   顺序模式：恰好满 minutes 根才交付（盘中截断的不满桶丢弃）；
    #   对齐模式：末根到达该桶理论末分钟才交付——盘中截断（未走完）丢弃，
    #   中间缺分钟但已走完的桶照常交付（数据缺口不吞整桶）。
    if chunk:
        if aligned:
            tail_ok = False
            try:
                t = str(chunk[-1].get("time") or "")
                end_min = (prev + 1) * minutes - 1 + 570
                tail_ok = (int(t[11:13]) * 60 + int(t[14:16])) >= end_min
            except ValueError:
                tail_ok = False
            if tail_ok:
                out.append(_flush(chunk))
        elif len(chunk) == minutes:
            out.append(_flush(chunk))
    return out


# ═══════════════════════════════════════════════════════════════
#  市场事实快照
# ═══════════════════════════════════════════════════════════════

_MARKET_MEMO: Dict[str, Any] = {"at": 0.0, "day": "", "data": None}
_MARKET_MEMO_TTL_S = float(os.getenv("QD_SCREENER_MARKET_TTL", "60") or 0)


def _mood_label(score: int) -> str:
    """情绪分 → 描述性标签（大盘/个股两档共用同一套刻度，便于并列阅读）。"""
    if score >= 70:
        return "偏强"
    if score >= 55:
        return "中性偏强"
    if score >= 45:
        return "中性"
    if score >= 30:
        return "偏弱"
    return "弱势"


def market_state() -> Dict[str, Any]:
    """市场层面客观事实：资金流向、涨跌停家数、炸板率、板块强弱、情绪描述标签。

    2026-10-30 增（缺陷③）：指数日内强弱（index_intraday / intraday_bias）、
    大盘资金近5日方向（fund_flow_days）与走弱环境标记（cautious）——早盘阶段
    涨停/炸板计数未成熟，这两块是仅有的先行信号。

    注意：market_mood(_score) 与 stock_mood(_score) 是**两档独立**的描述性标签 ——
    大盘档只看「指数日内强弱 + 大盘资金」，个股档只看「涨跌停比 / 涨停活跃度 /
    炸板率」。二者常背离（资金强但赚钱效应差），不可互相替代，也不是入场规则。
    intraday_bias 同为描述性标签。cautious=True 供下游（_helpers._hints /
    soft_warnings）收紧选股用，同样不是自动砍候选。

    进程内 memo：默认 TTL 60s（`QD_SCREENER_MARKET_TTL` 秒，0=关闭），键含目标
    交易日故跨日不复用。动机是 inspect_stock 每查一只票都要调一次，而单次实测
    883~1428ms（大头是指数实时行情），秒级内快照无变化。

    Returns:
        dict: fund_flow / zt_count / dt_count / broken_count / broken_rate /
            strong_sectors / weak_sectors / market_mood / market_mood_score /
            stock_mood / stock_mood_score /
            index_intraday / fund_flow_days / intraday_bias / cautious
    """
    # 三池取数日 = **目标交易日**（非交易日回落上一交易日，见 common._data_day）：
    # 用 _today_str() 会在周末/节假日拿到空池，进而把 zt_count=0 当成情绪冰点。
    day = _data_day()
    _memo = _MARKET_MEMO
    if (_MARKET_MEMO_TTL_S > 0 and _memo["data"] is not None
            and _memo["day"] == day
            and (time.time() - _memo["at"]) < _MARKET_MEMO_TTL_S):
        return _memo["data"]

    # 三对账修正（2026-09-30）：旧实现拿平安银行（000001）个券资金流冒充“上证净流入”，
    # 且读 net_inflow（真实键 total_main_net）恒 0 → 情绪分恒基线。改走大盘口径
    # （get_fund_flow scope=market，键 main_net，单位元），失败如实记 0。
    net_inflow = 0
    try:
        from ...tools.finance.fund_flow_tools import get_fund_flow
        ff = get_fund_flow(scope="market")
        if isinstance(ff, dict) and not ff.get("error"):
            net_inflow = float(ff.get("main_net") or ff.get("total_main_net") or 0)
    except Exception as e:
        try:
            from ...log import logger
            logger.warning("[MktScreen] 大盘资金流获取失败: %s", e)
        except Exception:
            pass

    # 源级失败显性化（2026-10-21）：三池 fetcher 已 fail-fast，此处降级为 0 计数
    # 但必须把失败写进 source_errors，由 pre_screen 合并进 missing —— 静默归零会把
    # 「涨跌停池源故障」伪装成「情绪冰点」（zt=0/dt=0/炸板率 0 的假情绪分）。
    src_errors: List[str] = []
    try:
        zt_pool = fetch_zt_pool(day)
    except Exception as e:
        zt_pool = []
        src_errors.append(f"涨停池源失败: {type(e).__name__}: {e}")
    try:
        dt_pool = fetch_dt_pool(day)
    except Exception as e:
        dt_pool = []
        src_errors.append(f"跌停池源失败: {type(e).__name__}: {e}")
    try:
        broken = fetch_broken_board(day)
    except Exception as e:
        broken = []
        src_errors.append(f"炸板池源失败: {type(e).__name__}: {e}")
    zt_count = len(zt_pool)
    dt_count = len(dt_pool)
    broken_count = len(broken)
    broken_rate = round(broken_count / max(1, zt_count + broken_count) * 100, 1)
    # 空池必须在 missing 里留痕（2026-10-31）：源故障在数值上与「该日确无涨停」
    # 无法区分，但 zt_count=0 会被情绪分读成冰点（2026-10-21 已修过「异常」分支，
    # 「空」分支当时漏了）。留痕后模型看到的是「不知道」而不是「0 家涨停」。
    if not zt_pool:
        src_errors.append(f"涨停池 {day} 为空：源未产出或该日确无涨停")
    if not dt_pool:
        src_errors.append(f"跌停池 {day} 为空：源未产出或该日确无跌停")

    strong_sectors: List[Dict] = []
    weak_sectors: List[Dict] = []
    sectors = fetch_hot_sectors()
    if isinstance(sectors, dict) and not sectors.get("error"):
        # 三对账修正（2026-09-30）：get_hot_sectors 返回 {code,msg,data}，板块行在
        # data.industry 二级键——旧读顶层 industry 恒空，强弱板块永远为空。
        _sd = sectors.get("data") if isinstance(sectors.get("data"), dict) else sectors
        for s in (_sd.get("industry") or [])[:10]:
            bucket = strong_sectors if (s.get("change_pct") or 0) > 0 else weak_sectors
            bucket.append({"name": s.get("name"), "change_pct": s.get("change_pct")})

    # ── 指数日内 + 大盘资金多日（2026-10-30 用户缺陷③：早盘预判力不够）──
    # 【为什么加】旧版只有资金/涨跌停/炸板三类计数，而早盘（10:00 前）涨停/炸板
    # 计数尚未成熟（封板还在变），唯一可用的先行信号是：①指数相对开盘强弱
    # ②当日/近5日大盘资金方向。两块都是**描述性事实**（同 market_mood/stock_mood 性质，非入场规则）。
    index_rows: List[Dict] = []
    try:
        from app.market_cn.index import get_index_realtime
        for r in (get_index_realtime(["000001", "399001", "399006"]) or []):
            try:
                price = float(r.get("price") or 0)
                opn = float(r.get("open") or 0)
            except (TypeError, ValueError):
                price = opn = 0.0
            index_rows.append({
                "code": r.get("code"), "name": r.get("name"),
                "change_pct": r.get("change_percent"),
                "vs_open_pct": round((price - opn) / opn * 100, 2) if price and opn else None,
            })
    except Exception as e:
        src_errors.append(f"指数日内源失败: {type(e).__name__}: {e}")

    fund_days: Dict[str, Any] = {}
    try:
        from app.market_cn.fund_flow_api import market_flow_summary
        ms = market_flow_summary(days=5) or {}
        fund_days = {
            "direction": ms.get("direction"),
            "net_yi": ms.get("net_yi"),
            "direction_seq": ms.get("direction_seq"),  # 近5日逐日方向，看持续性
        }
    except Exception as e:
        src_errors.append(f"大盘资金多日源失败: {type(e).__name__}: {e}")

    # 描述性早盘倾向（同 market_mood/stock_mood：便于人和 LLM 快速理解，不是入场规则）：
    #   指数全部在开盘价下方=偏弱；全部在上方=偏强；否则看当日资金定温和偏向。
    vs_opens = [r.get("vs_open_pct") for r in index_rows if r.get("vs_open_pct") is not None]
    bias = "震荡"
    if vs_opens:
        if all(v < 0 for v in vs_opens):
            bias = "偏弱"
        elif all(v > 0 for v in vs_opens):
            bias = "偏强"
        elif net_inflow < 0 and sum(1 for v in vs_opens if v < 0) >= len(vs_opens) * 2 / 3:
            bias = "偏弱"
        elif net_inflow > 0 and sum(1 for v in vs_opens if v > 0) >= len(vs_opens) * 2 / 3:
            bias = "偏强"
    # cautious=True ⇒ 下跌/持续流出环境，下游（_helpers._hints / soft_warnings）应收紧选股
    seq = fund_days.get("direction_seq") or []
    persistent_out = len(seq) >= 3 and all(s == "outflow" for s in seq[-3:])
    cautious = (bias == "偏弱") or persistent_out

    # ── 情绪分拆两档（2026-11-04）：大盘情绪 ≠ 个股赚钱效应 ──
    # 旧版把「大盘资金流」与「涨停/跌停/炸板家数」加进同一个 50 基线的分里：
    #   · 资金因子 2 亿即封顶（min(20, net_inflow//1e7)），659 亿与 2 亿同分；
    #   · 涨跌停因子阈值落在死区（zt 要 >30 才加、dt 要 >20 才减、炸板率要 >40 才减），
    #     20 涨停 / 14 跌停 / 37.5% 炸板这种分化市三项全不触发；
    #   ⇒ 70 分实际只由资金一个维度撑起，把分化市标成「偏强」。拆开后两档独立可读：
    #   大盘档回答「指数和资金强不强」，个股档回答「钱好不好赚」，二者常背离。
    _yi = net_inflow / 1e8
    market_mood_score = 50
    if _yi >= 200:
        market_mood_score += 20
    elif _yi >= 50:
        market_mood_score += 16
    elif _yi >= 20:
        market_mood_score += 14
    elif _yi >= 5:
        market_mood_score += 10
    elif _yi >= 1:
        market_mood_score += 6
    elif _yi > 0:
        market_mood_score += 2
    elif _yi > -1:
        market_mood_score -= 2
    elif _yi > -5:
        market_mood_score -= 6
    elif _yi > -20:
        market_mood_score -= 10
    elif _yi > -50:
        market_mood_score -= 14
    elif _yi > -200:
        market_mood_score -= 16
    else:
        market_mood_score -= 20
    if vs_opens:
        _up_n = sum(1 for v in vs_opens if v > 0)
        if _up_n == len(vs_opens):
            market_mood_score += 10
        elif _up_n * 3 <= len(vs_opens):
            market_mood_score -= 10
        elif _up_n * 2 > len(vs_opens):
            market_mood_score += 4
    if persistent_out:
        market_mood_score -= 8
    market_mood_score = max(0, min(100, market_mood_score))

    stock_mood_score = 50
    if dt_count <= 0:
        stock_mood_score += 15 if zt_count >= 20 else 6
    else:
        _zt_dt = zt_count / dt_count
        if _zt_dt >= 3:
            stock_mood_score += 15
        elif _zt_dt >= 2:
            stock_mood_score += 8
        elif _zt_dt < 0.8:
            stock_mood_score -= 15
        elif _zt_dt < 1.2:
            stock_mood_score -= 8
    if zt_count >= 60:
        stock_mood_score += 10
    elif zt_count >= 30:
        stock_mood_score += 5
    elif zt_count < 10:
        stock_mood_score -= 10
    if broken_rate >= 45:
        stock_mood_score -= 15
    elif broken_rate >= 30:
        stock_mood_score -= 8
    elif broken_rate < 15:
        stock_mood_score += 6
    stock_mood_score = max(0, min(100, stock_mood_score))

    out = {
        "fund_flow": net_inflow,
        "market_mood": _mood_label(market_mood_score),
        "market_mood_score": market_mood_score,
        "stock_mood": _mood_label(stock_mood_score),
        "stock_mood_score": stock_mood_score,
        "strong_sectors": strong_sectors,
        "weak_sectors": weak_sectors,
        "zt_count": zt_count,
        "dt_count": dt_count,
        "broken_count": broken_count,
        "broken_rate": broken_rate,
        # ── 早盘预判块（2026-10-30）：指数日内强弱 + 大盘资金多日方向 ──
        "index_intraday": index_rows,       # [{code,name,change_pct,vs_open_pct}]
        "fund_flow_days": fund_days,        # {direction, net_yi, direction_seq}
        "intraday_bias": bias,              # 描述性标签：偏强/震荡/偏弱
        "cautious": cautious,               # True=走弱环境，下游收紧选股（非入场规则）
        "source_errors": src_errors,
    }
    if _MARKET_MEMO_TTL_S > 0:
        _MARKET_MEMO.update({"at": time.time(), "day": day, "data": out})
    return out

# ═══════════════════════════════════════════════════════════════
#  多周期几何量（客观描述，不含买入含义）
# ═══════════════════════════════════════════════════════════════

# 弱转强量能确认门槛（env 可调，默认 0.5）：纯客观标签，不改变 weak_to_strong
# 几何判定——量能是否确认作为独立事实输出，降权取舍归裁决层（strategy-rules.md）。
# 本文件禁止出现入选阈值/评分（见文件头约束），故只输出确认标签、不做判定卡口。
_W2S_VOL_MIN_CACHE: float | None = None


def _w2s_vol_min() -> float:
    """弱转强量能确认门槛：env QD_W2S_MIN_VOL_RATIO 优先，非法/≤0 回落 0.5。"""
    global _W2S_VOL_MIN_CACHE
    if _W2S_VOL_MIN_CACHE is None:
        try:
            v = float(os.environ.get("QD_W2S_MIN_VOL_RATIO", "") or 0)
            _W2S_VOL_MIN_CACHE = v if v > 0 else 0.5
        except (TypeError, ValueError):
            _W2S_VOL_MIN_CACHE = 0.5
    return _W2S_VOL_MIN_CACHE


def multitimeframe(code: str, daily_days: int = 60, minute_days: int = 5) -> Dict[str, Any]:
    """日线 + 15m + 5m 的结构量。分钟线统一由 1m 聚合而来（其它周期查表恒空）。

    weak_to_strong / strong_to_weak / volume_pattern / macd_strength 是对几何关系的
    **速记标签**（例如「最近5根多数站上MA5 且之前5根多数未站上」），
    便于快速扫读；如何解读写在 references/strategy-rules.md，不构成推荐。

    Args:
        code: 6 位股票代码
        daily_days: 日线回溯根数
        minute_days: 分钟线回溯自然日

    Returns:
        dict: ok / daily / m15 / m5 / volume_pattern / weak_to_strong / strong_to_weak / missing
    """
    out: Dict[str, Any] = {
        "ok": False, "code": code, "daily": {}, "m15": {}, "m5": {},
        "weak_to_strong": False, "strong_to_weak": False,
        "weak_to_strong_vol_confirm": False,
        "volume_pattern": "unknown", "missing": [],
    }

    bars_1d = fetch_kline(code, days=daily_days)
    if len(bars_1d) < 20:
        out["missing"].append(f"日线不足20根（实得{len(bars_1d)}）")
        return out
    out["ok"] = True

    closes = [b["close"] for b in bars_1d]
    volumes = [b["volume"] for b in bars_1d]
    n = len(bars_1d)
    i = n - 1

    ma5 = compute_ma(closes, 5)
    ma10 = compute_ma(closes, 10)
    ma20 = compute_ma(closes, 20)
    ma60 = compute_ma(closes, 60) if n >= 60 else [None] * n
    macd = compute_macd(closes)
    rsi = compute_rsi(closes)
    vr = compute_volume_ratio(volumes, 5)

    def _slope(seq, k: int) -> float:
        a, b = seq[k], seq[k - 1]
        if not a or not b:
            return 0.0
        return (a - b) / b * 100

    out["daily"] = {
        "close": closes[i],
        "ma5": ma5[i], "ma10": ma10[i], "ma20": ma20[i], "ma60": ma60[i],
        "macd_bar": macd["macd"][i], "rsi": rsi[i], "vol_ratio": vr[i],
        "above_ma5": bool(closes[i] > (ma5[i] or 0)),
        "above_ma20": bool(closes[i] > (ma20[i] or 0)),
        "ma5_slope_pct": round(_slope(ma5, i), 3),
        "ma60_slope_pct": round(_slope(ma60, i), 3) if ma60[i] else None,
    }

    # 均线穿越结构（纯计数，无方向评价）
    if i >= 9:
        recent_above = sum(1 for j in range(i - 4, i + 1) if closes[j] > (ma5[j] or 0))
        recent_below = sum(1 for j in range(i - 4, i + 1) if closes[j] < (ma5[j] or 0))
        prev_near = sum(1 for j in range(i - 9, i - 4) if closes[j] <= (ma5[j] or 0) * 1.01)
        prev_above = sum(1 for j in range(i - 9, i - 4) if closes[j] > (ma5[j] or 0))
        out["weak_to_strong"] = bool(recent_above >= 4 and prev_near >= 3)
        out["strong_to_weak"] = bool(recent_below >= 4 and prev_above >= 3)
        if macd["macd"][i] < 0 and recent_above >= 3 and closes[i] > (ma5[i] or 0):
            out["weak_to_strong"] = True
        # 量能确认（纯客观标签，不改变 weak_to_strong 几何判定）：弱转强成立时，
        # 日线量比达门槛才算确认；数据缺失（vr[i] 为 None）不算确认。
        # 降权取舍归裁决层（strategy-rules.md），本层只输出客观事实。
        if out["weak_to_strong"]:
            _vr_i = vr[i]
            out["weak_to_strong_vol_confirm"] = bool(
                _vr_i is not None and _vr_i >= _w2s_vol_min())

    if vr[i] > 1.5 and closes[i] > closes[i - 1]:
        out["volume_pattern"] = "放量上涨"
    elif vr[i] < 0.7 and closes[i] > closes[i - 1]:
        out["volume_pattern"] = "缩量上涨"
    elif vr[i] > 1.5 and closes[i] < closes[i - 1]:
        out["volume_pattern"] = "放量下跌"
    elif vr[i] < 0.7 and closes[i] < closes[i - 1]:
        out["volume_pattern"] = "缩量下跌"
    else:
        out["volume_pattern"] = "平量整理"

    # ── 分钟线：1m 源 → 聚合 15m / 5m ──
    bars_1m = intraday_bars(code, days=minute_days)
    if not bars_1m:
        out["missing"].append("分钟线为空（1m 源无数据）")
        return out

    for label, step in (("m15", 15), ("m5", 5)):
        agg = resample(bars_1m, step)
        if len(agg) < 6:
            out["missing"].append(f"{label} 不足6根（实得{len(agg)}）")
            continue
        c = [x["close"] for x in agg]
        v = [x["volume"] for x in agg]
        h = [x["high"] for x in agg]
        lo = [x["low"] for x in agg]
        k = len(agg) - 1
        ma5_a = compute_ma(c, 5)
        prev_avg = sum(v[max(0, k - 5):k]) / min(5, k) if k > 0 else 0
        out[label] = {
            "bars": len(agg),
            "close": c[k],
            "above_ma5": bool(c[k] > (ma5_a[k] or 0)),
            "rsi": round(compute_rsi(c)[k], 1),
            "vol_ratio": round(v[k] / prev_avg, 2) if prev_avg else None,
            "trend": "up" if c[k] > c[max(0, k - 5)] else "down",
        }
        if label == "m5":
            macd_a = compute_macd(c)
            green_r, red_r = [], []
            for j in range(max(0, k - 19), k + 1):
                rng = h[j] - lo[j]
                if macd_a["macd"][j] < 0:
                    green_r.append(rng)
                elif macd_a["macd"][j] > 0:
                    red_r.append(rng)
            strength = "unknown"
            if green_r and red_r:
                gr, rr = sum(green_r) / len(green_r), sum(red_r) / len(red_r)
                strength = "even"
                if gr < rr * 0.8:
                    strength = "sell_pressure_low"
                elif gr > rr * 1.2:
                    strength = "sell_pressure_high"
            out["m5"]["macd_strength"] = strength
            out["m5"]["green_avg_range"] = round(sum(green_r) / len(green_r), 4) if green_r else None
            out["m5"]["red_avg_range"] = round(sum(red_r) / len(red_r), 4) if red_r else None

    return out


# ═══════════════════════════════════════════════════════════════
#  形态识别（几何定义，不含任何 score / 建议）
# ═══════════════════════════════════════════════════════════════

def _pattern_platform_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 10:
        return None
    recent, today = bars[-6:-1], bars[-1]
    p_high = max(b["high"] for b in recent)
    p_low = min(b["low"] for b in recent)
    if p_high <= 0:
        return None
    range_pct = (p_high - p_low) / p_high * 100
    if range_pct > p["platform_range_pct"]:
        return None
    if today["close"] <= p_high or today["close"] <= today["open"]:
        return None
    avg_vol = sum(b["volume"] for b in recent) / len(recent)
    vol_ratio = today["volume"] / avg_vol if avg_vol > 0 else 0
    if vol_ratio <= p["breakout_vol_ratio"]:
        return None
    return {"pattern": "平台突破", "platform_high": round(p_high, 2),
            "range_pct": round(range_pct, 1), "vol_ratio": round(vol_ratio, 2)}


def _pattern_volume_reversal(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 10:
        return None
    recent, today = bars[-6:-1], bars[-1]
    vols = [b["volume"] for b in recent]
    ct = [b["close"] for b in recent]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if not ct or ct[0] <= 0 or avg_vol <= 0:
        return None
    period_change = (ct[-1] - ct[0]) / ct[0] * 100
    is_shrinking = all(v <= avg_vol * 1.1 for v in vols[-3:])
    is_surge = today["volume"] > avg_vol * p["surge_vol_ratio"] and today["close"] > today["open"]
    if is_shrinking and is_surge and abs(period_change) < p["flat_change_pct"]:
        return {"pattern": "底部放量启动", "vol_ratio": round(today["volume"] / avg_vol, 2),
                "period_change_pct": round(period_change, 1)}
    return None


def _pattern_ma_support_pullback(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 20:
        return None
    closes = [b["close"] for b in bars]
    ma5, ma10, ma20 = compute_ma(closes, 5), compute_ma(closes, 10), compute_ma(closes, 20)
    if not (ma5[-1] and ma10[-1] and ma20[-1]):
        return None
    if not (ma5[-1] > ma10[-1] > ma20[-1]):
        return None
    today = bars[-1]
    dist = abs(today["low"] - ma10[-1]) / ma10[-1] * 100
    if dist < p["ma_touch_pct"] and today["close"] > ma10[-1]:
        return {"pattern": "均线支撑回踩", "ma10": round(ma10[-1], 2),
                "low": round(today["low"], 2), "distance_pct": round(dist, 2),
                "close_above_open": today["close"] > today["open"]}
    return None


def _pattern_macd_golden_cross(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 30:
        return None
    macd = compute_macd([b["close"] for b in bars])
    dif, dea = macd["dif"], macd["dea"]
    if dif[-1] >= dea[-1] and dif[-2] < dea[-2]:
        return {"pattern": "MACD金叉", "dif": round(dif[-1], 4),
                "dea": round(dea[-1], 4), "underwater": bool(dif[-1] < 0)}
    return None


def _pattern_shrink_pullback_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 15:
        return None
    today = bars[-1]
    recent = bars[-11:-1]
    highs = [b["high"] for b in recent]
    peak = max(highs)
    peak_idx = highs.index(peak)
    if peak_idx >= len(recent) - 2:
        return None
    pullback = recent[peak_idx + 1:]
    pb_low = min(b["low"] for b in pullback)
    pb_pct = (peak - pb_low) / peak * 100
    if pb_pct < p["min_pullback_pct"] or pb_pct > p["max_pullback_pct"]:
        return None
    avg_pb = sum(b["volume"] for b in pullback) / len(pullback)
    pk = recent[max(0, peak_idx - 2):peak_idx + 1]
    avg_pk = sum(b["volume"] for b in pk) / len(pk) if pk else 1
    pb_high = max(b["high"] for b in pullback)
    if not (avg_pb < avg_pk * p["shrink_ratio"]
            and today["close"] > pb_high and today["close"] > today["open"]
            and today["volume"] > avg_pb * p["rebreak_vol_ratio"]):
        return None
    return {"pattern": "缩量回调放量突破", "peak": round(peak, 2),
            "pullback_pct": round(pb_pct, 1),
            "vol_ratio": round(today["volume"] / avg_pb, 2) if avg_pb else None}


def _pattern_prev_high_breakout(bars: List[Dict], p: Dict) -> Optional[Dict]:
    if len(bars) < 21:
        return None
    today = bars[-1]
    prev_high = max(b["high"] for b in bars[-21:-1])
    if today["close"] <= prev_high or today["close"] <= today["open"]:
        return None
    vol_5 = sum(b["volume"] for b in bars[-6:-1]) / 5
    vol_ratio = today["volume"] / vol_5 if vol_5 > 0 else 0
    if vol_ratio <= p["breakout_vol_ratio"]:
        return None
    return {"pattern": "突破前高", "prev_high": round(prev_high, 2),
            "close": round(today["close"], 2), "vol_ratio": round(vol_ratio, 2)}


_PATTERN_DETECTORS = [
    _pattern_platform_breakout, _pattern_volume_reversal,
    _pattern_ma_support_pullback, _pattern_macd_golden_cross,
    _pattern_shrink_pullback_breakout, _pattern_prev_high_breakout,
]

# 形态的**几何定义参数** —— 不是推荐门槛，改了就等于换了一种形态。
# 默认值与 md 里记录的一致，LLM 可以通过 params 覆盖做敏感性检查。
_PATTERN_PARAMS_DEFAULT: Dict[str, float] = {
    "platform_range_pct": 8.0,     # 平台：区间振幅上限
    "breakout_vol_ratio": 1.3,     # 突破：量能倍数下限
    "surge_vol_ratio": 1.8,        # 底部启动：当日倍量下限
    "flat_change_pct": 5.0,        # 底部启动：区间涨跌幅绝对值上限
    "ma_touch_pct": 1.5,           # 回踩：最低价距 MA10 的距离上限
    "min_pullback_pct": 3.0,       # 缩量回调：回调幅度下限
    "max_pullback_pct": 12.0,      # 缩量回调：回调幅度上限
    "shrink_ratio": 0.7,           # 缩量回调：回调段量能 / 前段量能上限
    "rebreak_vol_ratio": 1.5,      # 缩量回调：再突破当日倍量下限
}


def detect_patterns(bars: List[Dict], params: Dict[str, float] = None) -> List[Dict]:
    """识别常见 K 线形态。只回答「是不是」，不回答「好不好」。

    Args:
        bars: 日线升序列表
        params: 覆盖形态的几何定义参数（键见 _PATTERN_PARAMS_DEFAULT）

    Returns:
        list: [{pattern, ...几何量}]，无形态时返回 []
    """
    p = dict(_PATTERN_PARAMS_DEFAULT)
    p.update({k: float(v) for k, v in (params or {}).items() if k in p})
    found = []
    for detector in _PATTERN_DETECTORS:
        try:
            hit = detector(bars or [], p)
        except Exception as e:
            logger.debug("[MktScreen] 形态检测 %s 异常: %s", detector.__name__, e)
            continue
        if hit:
            found.append(hit)
    return found


