# -*- coding: utf-8 -*-
"""core/features/quality.py — 数据质量分级 (盘中成交能力 P3.6, 2026-09-21)。

上层特性层：对**标准分钟序列** (prep_minutes / minute_live_full 的产物) 做
  (a) 价格跳空异常检测 `detect_gap_anomaly`；
  (b) 综合质量分级 `grade_series`。

原语本身**零市场常量** —— 阈值一律来自 `MarketSpec`（`adapters/markets/*.yaml`）。

⚠️ 边界（用户裁定 2026-09-21，见 docs/盘中成交能力设计方案.md §3.6）：
  - 数据完整度**不是 exec_engine/auto 的职责** → 本模块只做"**检测 + 标注**"，
    不阻断、不自动修复；
  - 监视层是**展示层**（快速决策、无回溯性）→ 分级结果供展示标注，**不删交易**；
  - "缺拍时拒绝成交 / 顺延"属**策略口径**，待用户裁定（设计文档 §9.5），不在本模块。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.market_cn.auto.core.market import default_market

# 质量等级（数值越大越差；供排序/筛选）
LEVEL_ORDER: Dict[str, int] = {"ok": 0, "suspend": 0, "warning": 1, "error": 2}


def detect_gap_anomaly(bars: Sequence[Dict[str, Any]], *,
                       board_type: str = "default",
                       spec: Optional[Any] = None,
                       margin_pp: float = 1.0,
                       ex_div_idx: Optional[Sequence[int]] = None
                       ) -> List[Tuple[int, str, Dict[str, Any]]]:
    """相邻 bar 收盘价跳变检测（问题 4 除权假跳空 + 问题 5 真异常的共同解法）。

    返回 ``[(idx, kind, detail), ...]``：
      - ``kind='ex_dividend'``  → idx 落在 ``ex_div_idx``（已知除权日）→ **非异常**，跳过标记；
      - ``kind='abnormal_gap'`` → 跳跃超阈值且**非**除权 → 真异常（由 ``grade_series`` 定级）。

    阈值 = **板块名义涨停幅度 + margin_pp**（来自 spec；如主板 9.8+1 → 10.8%、
    创业板/科创板 19.8+1 → 20.8%）。⚠️ **不得写死 10%** —— 20cm 板会被误判为跳空。
    无涨跌停市场（``nominal_up_pct=0``）→ 不判，返回空。

    ⚠️ 本函数只判"价格跳变"；"缺拍 / 停牌"由 ``hub.prep_minutes(report_gaps=True)``
    与"整段空"判定负责（职责分离：价格异常 vs 槽位缺失）。
    """
    if bars is None or len(bars) < 2:
        return []
    s = spec if spec is not None else default_market()
    up_pct = s.nominal_up_pct(board_type) * 100.0     # 名义幅度（不乘容差）
    if up_pct <= 0:
        return []
    thresh = up_pct + float(margin_pp)
    ex = set(ex_div_idx or ())
    out: List[Tuple[int, str, Dict[str, Any]]] = []
    for i in range(1, len(bars)):
        c0 = float(bars[i - 1].get("c") or 0)
        c1 = float(bars[i].get("c") or 0)
        if c0 <= 0 or c1 <= 0:
            continue
        g = (c1 / c0 - 1) * 100.0
        if abs(g) <= thresh:
            continue
        if i in ex:
            out.append((i, "ex_dividend", {"chg_pct": round(g, 2)}))
        else:
            out.append((i, "abnormal_gap",
                        {"chg_pct": round(g, 2), "thresh": round(thresh, 2)}))
    return out


def grade_series(*, bars: Optional[Sequence[Dict[str, Any]]] = None,
                 gaps: Optional[Sequence[Tuple[int, int, int]]] = None,
                 anomalies: Optional[Sequence[Tuple[int, str, Dict[str, Any]]]] = None,
                 touched: Optional[Sequence[int]] = ()
                 ) -> Dict[str, Any]:
    """综合质量分级 → ``{'level': 'ok|warning|error|suspend', 'reasons': [...]}``。

    规则（用户裁定的"最简分级"）：
      - **空序列 / 无 bars** → ``suspend``（整段缺席，非异常）；
      - ``abnormal_gap`` 落在 ``touched``（成交判定所涉 bar 索引）内 → ``error``
        （本笔成交依据不可靠，**标注 + 降级可信度，不删交易**）；
      - 有 ``gaps`` 或区间外的 ``abnormal_gap`` → ``warning``；
      - 否则 ``ok``。
      ``ex_dividend`` **不计入**（非异常）。
    """
    reasons: List[str] = []
    anomalies = list(anomalies or [])
    gaps = list(gaps or [])
    if not bars:
        return {"level": "suspend", "reasons": ["empty_series"]}

    touch = set(touched)
    err = False
    for idx, kind, detail in anomalies:
        if kind == "abnormal_gap":
            if idx in touch:
                err = True
                reasons.append(f"abnormal_gap@{idx} in_touch")
            else:
                reasons.append(f"abnormal_gap@{idx}")
    if gaps:
        reasons.append(f"gaps={len(gaps)}")

    if err:
        return {"level": "error", "reasons": reasons}
    if reasons:
        return {"level": "warning", "reasons": reasons}
    return {"level": "ok", "reasons": []}


def grade_minute_window(minutes: Sequence[Dict[str, Any]], *,
                        board_type: str = "default",
                        spec: Optional[Any] = None,
                        touched: Optional[Sequence[int]] = (),
                        ex_div_idx: Optional[Sequence[int]] = None
                        ) -> Dict[str, Any]:
    """P5 单笔回测/展示入口: 对**该笔交易所用的分钟窗口**直接给质量分级。

    与 ``grade_series`` 的关系: 本函数是"分钟序列 → 分级"的**一步封装**
    (内部依次调 ``detect_gap_anomaly`` + ``grade_series``), 供 ``_refine_intraday``
    与展示链共用 —— 避免两处各写一遍接线而漂移。

    参数:
      ``minutes``  : 该笔交易的分钟槽位序列 (``window_minutes`` 的产物, 可为多日);
      ``touched``  : **成交判定实际读取**的槽位索引 (跨日须换算为窗口内扁平索引);
      ``ex_div_idx``: 已知除权日对应的扁平索引 (非异常, 跳过)。

    ⚠️ 边界同 ``grade_series``: **只标注、不阻断、不删交易**。
    """
    if not minutes:
        return {"level": "suspend", "reasons": ["empty_series"]}
    anomalies = detect_gap_anomaly(minutes, board_type=board_type, spec=spec,
                                   ex_div_idx=ex_div_idx)
    return grade_series(bars=minutes, gaps=None, anomalies=anomalies, touched=touched)
