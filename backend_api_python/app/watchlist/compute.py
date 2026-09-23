# -*- coding: utf-8 -*-
"""app/watchlist/compute.py — system（grade=1）自算：关键位 + §7 评分

职责：**取数之外的编排与特征提取**；口径（权重表 / 分段表 / 空值策略）全在 `model.py`。
本文件只做「原始值 → 归一 x」的换算，禁止在此另立一套阈值。

链路（方案 §6）：
  services/kline.get_kline  →  app/utils/indicators  →  services/chip_service  →  model.score_from_features

纪律
  - 纯函数：`score = f(klines[:k+1], chip)`，无随机 / 无时间 / 无状态 / 无未来函数（§7.1）
  - 取数口**只有** `services/kline`（§5.5 禁令 2：不得用 auto 的 hub）
  - 筹码**只有** `services/chip_service`（§5.5 禁令 3：不得用 agent 的工具实现，也不自算第三份）
  - 本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.chip_service import compute_chip_core, split_levels
from app.utils import indicators as ind
from app.utils.logger import get_logger
from app.watchlist import model

logger = get_logger(__name__)

#: 取数窗口（§6：limit≈320）
KLINE_LIMIT = 320

#: 布林带宽分位的回看根数（§7.3 R1）
BW_PERCENTILE_WINDOW = 60

#: 20 日振幅窗口（§7.3 R2）
AMPLITUDE_WINDOW = 20

#: 量能比窗口（§7.3 M3）
VOL_SHORT, VOL_LONG = 5, 20


def bar_date(ts: Any) -> str:
    """epoch 秒 → 'YYYY-MM-DD'（本地时区）。"""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")


def _mean(xs: Sequence[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def extract_features(klines: List[Dict[str, Any]], chip_core: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """提取全部子项**原始输入**（未归一），供 `build_normed` 归一。

    Returns:
        {"raw": {key: 原始值|None}, "detail": {...展示用原始值...}}
    """
    closes = [float(k["close"]) for k in klines]
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    vols = [float(k.get("volume") or 0) for k in klines]
    n = len(klines)
    close = closes[-1]

    ma5 = ind.ma(closes, 5)
    ma10 = ind.ma(closes, 10)
    ma20 = ind.ma(closes, 20)
    ma20_prev = ind.ma(closes[:-5], 20) if n > 25 else None   # ma20[-6]（5 根前）

    _, _, hist = ind.calc_macd(closes)
    rsi14 = ind.rsi(closes, 14)
    roc10 = ind.calc_roc(closes, 10)

    # 量能比
    vol_ratio = None
    if n >= VOL_LONG:
        m_long = _mean(vols[-VOL_LONG:])
        m_short = _mean(vols[-VOL_SHORT:])
        if m_long and m_long > 0 and m_short is not None:
            vol_ratio = m_short / m_long

    # 布林带宽分位（近 60 根逐根算 bw，取当根在其中的分位）
    bw_now = ind.calc_bollinger_bw(closes)
    bw_pct = None
    if bw_now is not None and n >= BW_PERCENTILE_WINDOW:
        series = []
        for k in range(n - BW_PERCENTILE_WINDOW, n):
            bw = ind.calc_bollinger_bw(closes[:k + 1])
            if bw is not None:
                series.append(bw)
        if len(series) >= 2:
            bw_pct = sum(1 for v in series if v <= bw_now) / len(series)

    # 20 日振幅
    amp = None
    if n >= AMPLITUDE_WINDOW:
        w_hi = max(highs[-AMPLITUDE_WINDOW:])
        w_lo = min(lows[-AMPLITUDE_WINDOW:])
        amp = (w_hi - w_lo) / close if close > 0 else None

    # 筹码派生
    profit_ratio = None
    support1 = resistance1 = None
    if chip_core and "error" not in chip_core:
        profit_ratio = chip_core["profit_ratio_raw"]
        c = chip_core["current_price"]
        sup = sorted((p for p in chip_core["peaks"] if p["price"] < c),
                     key=lambda x: x["price"], reverse=True)
        res = sorted((p for p in chip_core["peaks"] if p["price"] > c), key=lambda x: x["price"])
        if sup:
            support1 = sup[0]["price"]
        if res:
            resistance1 = res[0]["price"]

    raw = {
        "T2": (close / ma20 - 1) if ma20 else None,
        "T4": (ma20 / ma20_prev - 1) if (ma20 and ma20_prev) else None,
        "M1": rsi14,
        "M2": roc10,
        "M3": vol_ratio,
        "P1": profit_ratio,
        "P2": ((close - support1) / close) if (support1 and close > 0) else None,
        "P3": ((resistance1 - close) / close) if (resistance1 and close > 0) else None,
        "R1": bw_pct,
        "R2": amp,
    }
    detail = {
        "close": close, "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "rsi14": rsi14, "roc10": roc10, "vol_ratio": vol_ratio,
        "boll_bw": bw_now, "boll_bw_pct": bw_pct, "amplitude_20": amp,
        "profit_ratio": profit_ratio, "support1": support1, "resistance1": resistance1,
        "macd_hist": hist[-1] if hist else None,
    }
    # 离散档
    t1 = model.norm_T1(ma5, ma10, ma20) if (ma5 and ma10 and ma20) else None
    t3 = model.norm_T3(hist) if (hist and hist[-1] is not None) else None
    feats: Dict[str, Optional[float]] = {"T1": t1, "T3": t3}
    for key, x in raw.items():
        feats[key] = None if x is None else model.normalize(key, float(x))

    # 评分明细的「输入」列（§7.6）：展示**子项输入本身**，不是派生展示值
    if t1 is None:
        t1_txt = None
    elif ma5 > ma10 > ma20:
        t1_txt = "ma5>ma10>ma20"
    elif ma5 > ma20:
        t1_txt = "ma5>ma20"
    elif ma10 > ma20:
        t1_txt = "ma10>ma20"
    else:
        t1_txt = "ma5<=ma20"
    t3_txt = None if (not hist or hist[-1] is None) else f"hist={hist[-1]:.4f}"
    raw_disp: Dict[str, Any] = {"T1": t1_txt, "T3": t3_txt}
    for key, x in raw.items():
        raw_disp[key] = None if x is None else round(float(x), 4)

    return {"feats": feats, "raw": raw, "raw_disp": raw_disp, "detail": detail}


def _fmt(v: Any, nd: int = 3) -> Optional[float]:
    return None if v is None else round(float(v), nd)


def build_extras(detail: Dict[str, Any], raw_disp: Dict[str, Any], breakdown: Dict[str, Any],
                 dropped: List[str], w_eff: float) -> List[Dict[str, Any]]:
    """扩展段 = 有序单元列表（§2.2）：① 技术指标 fields ② 评分明细 table ③ 评分口径 fields。"""
    ind_rows = [
        {"label": "RSI(14)", "value": _fmt(detail.get("rsi14"), 2)},
        {"label": "ROC(10)%", "value": _fmt(detail.get("roc10"), 2)},
        {"label": "量能比(5/20)", "value": _fmt(detail.get("vol_ratio"), 2)},
        {"label": "布林带宽%", "value": _fmt(detail.get("boll_bw"), 2)},
        {"label": "布林带宽分位", "value": _fmt(detail.get("boll_bw_pct"), 2)},
        {"label": "20日振幅", "value": _fmt(detail.get("amplitude_20"), 3)},
        {"label": "获利比例", "value": _fmt(detail.get("profit_ratio"), 3)},
        {"label": "MA5/10/20", "value": "{}/{}/{}".format(
            _fmt(detail.get("ma5"), 2), _fmt(detail.get("ma10"), 2), _fmt(detail.get("ma20"), 2))},
    ]
    ind_rows = [r for r in ind_rows if r["value"] is not None]

    score_rows = []
    for key, label, _dim, _w in model.SCORE_ITEMS:
        b = breakdown.get(key)
        raw = raw_disp.get(key)
        score_rows.append({
            "label": label,
            "raw": raw if raw is not None else "-",
            "norm": (b["norm"] if b else None),
            "contrib": (b["contrib"] if b else 0.0),
        })

    return [
        {"type": "fields", "title": "技术指标", "rows": ind_rows},
        {"type": "table", "title": "评分明细",
         "columns": [{"key": "label", "label": "项"}, {"key": "raw", "label": "输入"},
                     {"key": "norm", "label": "归一"}, {"key": "contrib", "label": "得分"}],
         "rows": score_rows},
        {"type": "fields", "title": "评分口径",
         "rows": [{"label": "score_version", "value": model.SCORE_VERSION},
                  {"label": "有效权重 W_eff", "value": _fmt(w_eff, 3)},
                  {"label": "剔除子项", "value": ",".join(dropped) or "无"}]},
    ]


def build_levels(chip_core: Optional[Dict[str, Any]], close: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """关键位整理：取筹码峰近端各 1~3 项，附 `strength` / `dist_pct` / `origin`。

    不变式：`[l["price"] for l in supports] == chip.support_prices`（同集合同顺序）。
    """
    if not chip_core or "error" in chip_core:
        return [], []
    # 与 chip_service 同一口径切分（禁止在 label 侧复制"取近端位"的判定）
    levels = split_levels(chip_core)

    def _decorate(items: List[Dict[str, Any]], *, is_support: bool) -> List[Dict[str, Any]]:
        out = []
        for it in items:
            price = float(it["price"])
            dist = ((close - price) / close) if is_support else ((price - close) / close)
            out.append({
                "price": round(price, 4),
                "strength": it.get("strength"),
                "dist_pct": round(dist, 4) if close > 0 else None,
                "origin": "chip_peak",
            })
        return out

    return _decorate(levels["support_levels"], is_support=True), \
        _decorate(levels["resistance_levels"], is_support=False)


def system_label(market: str, symbol: str, klines: List[Dict[str, Any]],
                 asof: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """system（grade=1）自算一张 label 事实（不写库；写库由 store 负责）。

    Returns:
        None ⇒ **不产出**（= 空白）：市场不支持 / 数据不足 / `W_eff < 0.60`
        dict ⇒ {trade_date, score, score_version, supports, resistances, extras, facts_asof}
    """
    if market not in model.SUPPORTED_MARKETS:
        return None                      # §7.5：非 CN/HK **明确不产出**（不是落 NULL）
    if not klines or len(klines) < model.MIN_KLINES:
        return None                      # 次新 / 长期停牌 ⇒ 空白

    if asof:
        klines = [k for k in klines if bar_date(k["time"]) <= asof]
        if len(klines) < model.MIN_KLINES:
            return None

    trade_date = bar_date(klines[-1]["time"])
    chip_core = compute_chip_core(klines, lookback_days=120)
    if "error" in chip_core:
        chip_core = None

    fe = extract_features(klines, chip_core)
    res = model.score_from_features(fe["feats"])
    if res["score"] is None:
        return None                      # W_eff < 0.60 ⇒ 不落 grade=1 行（空白）

    close = fe["detail"]["close"]
    supports, resistances = build_levels(chip_core, close)

    return {
        "trade_date": trade_date,
        "score": res["score"],
        "score_version": model.SCORE_VERSION,
        "supports": supports,
        "resistances": resistances,
        "extras": build_extras(fe["detail"], fe["raw_disp"], res["breakdown"],
                               res["dropped"], res["w_eff"]),
        "facts_asof": datetime.fromtimestamp(int(klines[-1]["time"])),
        "w_eff": res["w_eff"],
        "dropped": res["dropped"],
    }
