# -*- coding: utf-8 -*-
"""core/features/minute_composite.py — 两段拼接实时分钟序列 (盘中成交能力 P3.5, 2026-09-21)。

**正解（用户 2026-09-21）**："`kline_1m_YYYY` 是正确的分钟数据，用当日的 `realtime_snapshot`
数据转化 + 历史分钟数据就成了完整的实时分钟数据。" ⇒ 本模块把 `hub.daily_live` 的
"历史 + 实时"拼接范式**从日线粒度下沉到分钟粒度**。

| 段 | 来源 | 角色 | 理由 |
|---|---|---|---|
| 历史基底 | `hub.minute_1m`(kline_1m) | D-1 及以前 | **盘后回填、准确性高** |
| 实时尾部 | `realtime_snapshot`(minute_live) | **仅当日** | 盘中唯一可用 |

**去重判据（用户裁定）**："盘后 1m 有就用 1m，没有才用 realtime" ⇒ 当日已在 kline_1m → 整体用 1m。

**落点（越界纪律）**：上层组合 helper，**改动限于 `auto/` 内**；仅**调用**现有 `hub` 接口
(`minute_1m` / `prep_minutes` / `_fetch_snapshots_by_date`) 与 `frames._qfq_bars`，
**不修改数据层 / 基层**（`db_market.py` 在 `auto/` 外，禁改）。

⚠️ **as-of 纪律**：回测侧本 helper 只作**槽位引擎的数据源**被消费；判定代码**不得**
整段直接调用（否则含未来函数）。回测一律走 `run_all_intraday` 的 `snaps_at(mi)` 槽位机制
（用户裁定：复用槽位机制，不新增 `upto_mi` 参数 / 第二套截断语义）。实盘侧取当日快照天然
只有已发生数据，可按整段返回。

⚠️ **成本纪律（§3 原则 8，09-21 用户裁定）**："能用 1D 就不用 1m"——
`hybrid_series()` 是 P3/P4 的**唯一常规取数入口**（近端 `minute_days` 交易日 1m + 远端 1D）；
`minute_live_full()` 是它的近端底座，**不得**被用来整窗拉 1m。
`minute_days` 语义 = **绝对最近 N 个交易日**，N 由常量 `DEFAULT_MINUTE_DAYS`（=100，可调）
统一定义（用户 2026-09-21 裁定，同时定死 §9-9 的 `auto` 分界 = 固定常量而非动态探测）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


def minute_live_full(code: str, days: int = 5, *, series: Optional[List[Dict[str, Any]]] = None,
                     report_gaps: bool = False):
    """实时完整分钟序列 = `kline_1m`(历史, qfq) + `realtime_snapshot`(当日尾部)。

    参数：
      - ``days``：回看日历窗口（内部按 ×1.5 扩日覆盖交易日；默认 5，按策略 `data_needs` 可放大，
        上限 = `kline_1m` 覆盖 ~100 工作日）。
      - ``series``：可选，预先取好的当日快照行（`_fetch_snapshots_by_date([code])[code]`）,
        避免重复查库；None → 内部自取。
      - ``report_gaps``：True → 返回 `(bars, gaps)`。

    返回：标准槽位序列 `list[dict]`，元素含 ``date, mi, ts, o, h, l, c, v, cv``，
      按 ``(date, mi)`` 升序；``report_gaps=True`` 时另返回
      ``gaps = [{'date','mi_start','mi_end','n'}, ...]``。

    **缝合四问题处理**（详见 docs/盘中成交能力设计方案.md §3.5.0）：
      1. **复权同源**：历史段统一经 `frames._qfq_bars`（= `unadj_to_qfq`，与基线 `fetch_1m`
         同处理）。当日段为原始价 —— **前复权锚定最新日**，故当日原始价 == 当日 qfq，
         与历史段同基准（当日发生除权时亦成立，前提是因子源已含当日）。
      2. **缝合处空缺**：当日段额外报**前导缺口**（首拍 ``mi>0`` → 开盘段缺失）。
      3. **跨日跳空非缺口**：缺口一律**按日**计算（`prep_minutes` 分日），
         D-1 末根(mi239) → D 首根(mi0) 是**正常跨日**，不入缺口。
      4. **重复槽位去重**：当日若已回填进 `kline_1m`（`today in by_date`）→ **以 1m 为准**，
         不再拼快照 ⇒ 盘中/盘后自动收敛到同一口径（用户裁定）。

    ⚠️ **残留约束**：当日段来自快照，可能有丢拍（历史段无此问题）——
      `report_gaps=True` 会在当日段报出；盘后可用 `kline_1m` 真值回验（见设计文档 §3.5.0 自校验）。
    ⚠️ **竞价拍** (9:26) 不参与本序列的成交判定（被开盘首拍在 mi=0 覆盖，价格侧安全）。
    """
    from app.market_cn.auto.core.data.hub import (
        _fetch_snapshots_by_date, minute_1m, prep_minutes)
    from app.market_cn.auto.core.data.frames import _qfq_bars

    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5) + 2)).strftime("%Y-%m-%d")

    raw = minute_1m(code, start=start, end=today)   # 含当日（若已回填）
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for r in raw:
        by_date.setdefault(str(r.get("time", ""))[:10], []).append(r)

    bars: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []

    for d in sorted(by_date):
        rows = by_date[d]
        if not rows:
            continue
        qbars = _qfq_bars(code, rows)
        if len(qbars) != len(rows):                 # 防御: 复权异常时退回原始行
            qbars = rows
        if report_gaps:
            day_std, g = prep_minutes(qbars, report_gaps=True)
            for (a, c, n) in g:
                gaps.append({"date": d, "mi_start": a, "mi_end": c, "n": n})
        else:
            day_std = prep_minutes(qbars)
        for b in day_std:
            b["date"] = d
            bars.append(b)

    # 当日未回填 → 用快照（问题 4：kline_1m 无当日才回落 realtime）
    if today not in by_date:
        if series is None:
            series = _fetch_snapshots_by_date([code]).get(code) or []
        if series:
            if report_gaps:
                live, lg = prep_minutes(series, volume_cumulative=True, report_gaps=True)
                if live and live[0]["mi"] > 0:      # 问题 2: 当日前导缺口（开盘段缺失）
                    mg = live[0]["mi"]
                    lg = [(0, mg - 1, mg)] + list(lg)
                for (a, c, n) in lg:
                    gaps.append({"date": today, "mi_start": a, "mi_end": c, "n": n})
            else:
                live = prep_minutes(series, volume_cumulative=True)
            for b in live:
                b["date"] = today
                bars.append(b)

    bars.sort(key=lambda b: (b["date"], b["mi"]))
    return (bars, gaps) if report_gaps else bars


def split_by_date(bars: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """序列 → ``{date: [bars]}``（回测逐日推进用）。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for b in bars:
        out.setdefault(b["date"], []).append(b)
    return out


# ================================================================
# 分层取数 (P3, 2026-09-21 用户裁定) —— 「能用 1D 就不用 1m」
# ----------------------------------------------------------------
# 用户原话："能用 1D 线就不用 1m 线, 反过来也一样。只要 10 天 1m 线 + 90 天 1D 线
# 能解决问题, 就不要 100 天的 1m 线。1m 线的数据量大, 会严重拖垮速度。"
#
# ⇒ 成本纪律 (设计文档 §3 原则 8): 任何消费分钟数据的路径都只对**真正需要分钟精度
#   的近端窗口**上 1m, 其余一律 1D; 1m 用量以"够用"为下限, **禁止整窗 1m**。
#   ⚠️ 唯一口子: `lookback_days <= minute_days` 时分层自然退化为整窗 1m (2026-09-21
#   用户裁定 N=100 后, 100 日窗口即属此种) → 长窗/全市场请按需下调 `minute_days`。
#   量级: 单交易日 1m = 240 行 vs 1D = 1 行 (240x); 100 交易日整窗 1m = 24000 行/票,
#   全市场 5000+ 票 → 亿级行, 取数/复权/建槽位全部塌方。
#
# 两段口径同源 (关键): 1D 段来自 `hub.daily` (本就 qfq); 1m 段经 `frames._qfq_bars`
# (= `unadj_to_qfq`) —— 同一复权函数、同一因子源 ⇒ 跨段拼接无假跳空 (§3.6 问题 4)。
# ================================================================

# 近端 1m 档宽度 (交易日) —— 用户裁定 (2026-09-21): 语义 = **绝对最近 N 个交易日**
# (非"相对每笔入场"; 若将来需要按持仓窗分层, 走 `plan_tiers(minute_dates=...)`, 无需改本常量)。
# N = 100 = `kline_1m_YYYY` 的**保证覆盖窗口** (实测 ~157 交易日, 见 hub.py 头部说明):
# 回测窗口 <= 100 交易日 → 全窗 1m; > 100 → 近端 100 日走 1m、更远一律 1D。
# ⚠️ 该值**收敛于此一处常量**, 调整只改这里 (禁止散落硬编码)。
DEFAULT_MINUTE_DAYS = 100
MINUTES_PER_DAY = 240           # 单日标准槽位数 (9:31~11:30 + 13:00~15:00); 仅用于成本估算


def plan_tiers(trading_days, *, minute_days: int = DEFAULT_MINUTE_DAYS,
               minute_dates=None) -> Dict[str, str]:
    """交易日序列（升序）→ ``{date: '1m' | '1d'}`` 分层计划。

    - ``minute_dates`` 显式给定"必须 1m"的交易日（如某笔持仓期）→ 只这些日走 1m；
    - 否则取序列**最近** ``minute_days`` 个交易日走 1m（用户裁定的近端窗口）；
    - 其余全部 1d。两档集合**互斥**、并集 = 入参全部交易日（不重不漏）。
    """
    days = [str(d)[:10] for d in trading_days]
    if minute_dates is not None:
        want = {str(d)[:10] for d in minute_dates}
    else:
        want = set(days[-int(minute_days):]) if minute_days else set()
    return {d: ("1m" if d in want else "1d") for d in days}


def hybrid_series(code: str, *, lookback_days: int = 100,
                  minute_days: int = DEFAULT_MINUTE_DAYS, minute_dates=None,
                  series: Optional[List[Dict[str, Any]]] = None,
                  report_gaps: bool = False) -> Dict[str, Any]:
    """分层序列 = **近端 1m + 远端 1D**（P3 取数纪律的唯一入口）。

    返回 ``{'daily': [...], 'minute': [...], 'meta': {...}}``：

    - ``daily``  : 远端交易日的 1D qfq bars（`hub.daily`，最短 = 需粗粒度的历史）；
    - ``minute`` : 近端 ``minute_days`` 个交易日的 1m 标准槽位序列
      （`minute_live_full`：`kline_1m` 历史 + 当日快照，元素含 ``date/mi/ts/o/h/l/c/v/cv``）；
    - ``meta``   : 分层计划/边界/两档行数/整窗 1m 行数估算（成本对照用），
      ``report_gaps=True`` 时含 ``gaps``。

    设计要点：
      - **禁止整窗 1m**：``minute`` 只覆盖 ``minute_days`` 个交易日，与 ``daily`` 互斥；
        ⚠️ 默认 ``lookback_days == minute_days == 100`` ⇒ 100 日窗口下**分层退化为整窗 1m**
        （仅当窗口 > 100 交易日时才真正省量）；长窗请显式下调 ``minute_days``；
      - **不丢日**：某近端日 1m 缺失（快照丢拍/未回填）→ 该日仍留在 ``daily``（口径降级而非丢日）；
      - ``lookback_days < minute_days`` → fail-fast（分层无意义）；
      - **as-of**：本函数**不截断**。回测侧只作槽位引擎的数据源，截断一律交
        ``MinuteFrame.snaps_at(mi)``（用户裁定：复用槽位机制，不新增第二套截断语义）。
    """
    if lookback_days < minute_days:
        raise ValueError(
            f"lookback_days={lookback_days} < minute_days={minute_days}: 分层无意义")

    from app.market_cn.auto.core.data.hub import daily as _daily

    daily_bars = _daily(code, lookback_days) or []
    daily_dates = [str(b["time"])[:10] for b in daily_bars]

    # 近端 1m: minute_live_full 的 days 是"日历窗口 ×1.5 覆盖交易日"的粗参, 传
    # minute_days 会略多取 → 后置按实际出现的交易日截到最近 minute_days 个, 保证宽度精确。
    m_raw = minute_live_full(code, days=max(int(minute_days), 1), series=series,
                            report_gaps=report_gaps)
    gaps: List[Dict[str, Any]] = []
    if report_gaps:
        m_raw, gaps = m_raw
    m_dates_all = sorted({b["date"] for b in m_raw})

    if minute_dates is not None:
        want = {str(d)[:10] for d in minute_dates} & set(m_dates_all)
        m_dates = [d for d in m_dates_all if d in want]
    else:
        m_dates = m_dates_all[-int(minute_days):] if minute_days else []

    m_keep = set(m_dates)
    minute = [b for b in m_raw if b["date"] in m_keep]
    # 未被 1m 实际覆盖的日（含"近端但 1m 缺失"）全部留 1D → 不丢日
    daily = [b for b in daily_bars if str(b["time"])[:10] not in m_keep]

    tier = plan_tiers(sorted(set(daily_dates) | set(m_dates_all)),
                      minute_days=minute_days, minute_dates=minute_dates)
    meta: Dict[str, Any] = {
        "lookback_days": int(lookback_days),
        "minute_days": int(minute_days),
        "boundary": m_dates[0] if m_dates else None,     # 首个 1m 交易日
        "minute_dates": m_dates,
        "n_1m_days": len(m_keep),
        "n_1d_days": len(daily),
        "rows": {"1m": len(minute), "1d": len(daily)},
        # 成本对照: 若整窗走 1m 的估算行数 (1D 行数 × 240)
        "full_1m_rows_est": len(daily_dates) * MINUTES_PER_DAY,
        "tier": tier,
        "gaps": gaps,
    }
    return {"daily": daily, "minute": minute, "meta": meta}


def window_minutes(code: str, start_date: str, end_date: str, *,
                   series: Optional[List[Dict[str, Any]]] = None
                   ) -> Dict[str, List[Dict[str, Any]]]:
    """按**实际日期窗口**取分钟槽位 → ``{date: [槽位, ...]}`` (P3b/P4 精修专用)。

    与 ``minute_live_full`` 的分工: 后者固定"最近 N 个日历日"的通用近端窗口; 本函数按
    调用方给的真实窗口取 —— 只取**那几天**, 绝不为几笔候选交易拉整窗 1m (成本纪律, §3 原则 8)。
    窗口含今日且 ``kline_1m`` 尚未回填 → 当日回落 ``realtime_snapshot``
    (与 ``minute_live_full`` 同一去重约定: 盘后 1m 有就用 1m)。

    ⚠️ 本函数**不截断 as-of**: 回测侧只作数据源, 截断一律交槽位机制 (引擎逐槽推进)。
    """
    from app.market_cn.auto.core.data.hub import (
        _fetch_snapshots_by_date, minute_1m, prep_minutes)
    from app.market_cn.auto.core.data.frames import _qfq_bars

    s, e = str(start_date)[:10], str(end_date)[:10]
    if not s or not e or e < s:
        return {}
    raw = minute_1m(code, start=s, end=e)
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for r in raw:
        by_date.setdefault(str(r.get("time", ""))[:10], []).append(r)

    out: Dict[str, List[Dict[str, Any]]] = {}
    for d in sorted(by_date):
        rows = by_date[d]
        if not rows:
            continue
        qbars = _qfq_bars(code, rows)
        if len(qbars) != len(rows):             # 防御: 复权异常时退回原始行
            qbars = rows
        bars = prep_minutes(qbars)
        if bars:
            out[d] = bars

    today = datetime.now().strftime("%Y-%m-%d")
    if e >= today and today not in by_date:      # 当日未回填 → 快照 (仅盘中/盘后未回填时)
        if series is None:
            series = _fetch_snapshots_by_date([code]).get(code) or []
        if series:
            live = prep_minutes(series, volume_cumulative=True)
            if live:
                out[today] = live
    return out
