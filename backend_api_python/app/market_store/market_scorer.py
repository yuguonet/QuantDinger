#!/usr/bin/env python3
"""
market_scorer.py — 基于行情数据生成综合市场评分指数

核心指标:
  1. 自定义恐贪指数 (Custom Fear & Greed, CFGI) — 0-100
  2. 市场健康度 (Market Health Score, MHS) — 0-100
  3. 波动率体制 (Volatility Regime) — low / normal / elevated / high / extreme

用法:
  from market_store import MarketStore
  from market_scorer import MarketScorer

  store = MarketStore()
  df = store.query(hours=6)
  scorer = MarketScorer(df)
  print(scorer.report())

  # 或单独获取
  cfgi = scorer.cfgi()       # {"score": 72, "label": "贪婪", ...}
  mhs  = scorer.mhs()        # {"score": 65, "label": "偏强", ...}
  vol  = scorer.volatility() # {"regime": "elevated", "vix": 24.5, ...}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# 常量 — 评分等级定义
# ---------------------------------------------------------------------------

# 恐贪指数等级
CFGI_LEVELS = [
    (0,  20,  "极度恐慌", "🔴", "市场极度悲观，往往是逆向买入机会"),
    (20, 40,  "恐慌",     "🟠", "市场偏悲观，风险偏好下降"),
    (40, 55,  "中性偏恐", "🟡", "市场观望情绪较浓"),
    (55, 70,  "中性偏贪", "🟢", "市场情绪稳定偏乐观"),
    (70, 85,  "贪婪",     "🔵", "市场乐观，注意过热风险"),
    (85, 101, "极度贪婪", "🟣", "市场极度乐观，警惕回调"),
]

# 市场健康度等级
MHS_LEVELS = [
    (0,  20,  "极弱", "🔴", "市场全面走弱，系统性风险偏高"),
    (20, 40,  "偏弱", "🟠", "多数资产下跌，防御为主"),
    (40, 55,  "中性", "🟡", "多空均衡，方向不明"),
    (55, 70,  "偏强", "🟢", "多数资产上涨，趋势偏多"),
    (70, 85,  "强劲", "🔵", "市场全面走强"),
    (85, 101, "超强", "🟣", "市场极度强势，注意拥挤度"),
]


def _level_lookup(levels: list, score: float) -> Dict[str, str]:
    for lo, hi, label, emoji, desc in levels:
        if lo <= score < hi:
            return {"label": label, "emoji": emoji, "description": desc}
    return {"label": "未知", "emoji": "❓", "description": ""}


# ---------------------------------------------------------------------------
# MarketScorer — 核心评分引擎
# ---------------------------------------------------------------------------

class MarketScorer:
    """
    基于行情 DataFrame 生成综合市场评分。

    输入: MarketStore.query() 返回的 DataFrame
         必须包含 columns: [timestamp, category, symbol, name, price, change_pct]
    """

    def __init__(self, df: pd.DataFrame):
        self.df = df.copy() if not df.empty else pd.DataFrame()
        self._cache: Dict[str, Any] = {}

    # ---- 数据提取辅助 ----

    def _by_cat(self, category: str) -> pd.DataFrame:
        if self.df.empty:
            return pd.DataFrame()
        sub = self.df[self.df["category"] == category]
        # 取每个 symbol 最新一条
        return sub.sort_values("timestamp").groupby("symbol").last().reset_index()

    def _get_val(self, category: str, symbol: str) -> Optional[float]:
        """获取某标的最新价格。"""
        sub = self._by_cat(category)
        row = sub[sub["symbol"] == symbol]
        if row.empty:
            return None
        v = row.iloc[0]["price"]
        return float(v) if pd.notna(v) and v != 0 else None

    def _get_chg(self, category: str, symbol: str) -> Optional[float]:
        """获取某标的最新涨跌幅。"""
        sub = self._by_cat(category)
        row = sub[sub["symbol"] == symbol]
        if row.empty:
            return None
        v = row.iloc[0]["change_pct"]
        return float(v) if pd.notna(v) else None

    # ==================================================================
    # 1. 自定义恐贪指数 (CFGI) — 0~100
    # ==================================================================
    #
    # 权重:
    #   市场动量  30%  (指数涨跌 + 加密涨跌)
    #   波动率    25%  (VIX 反向)
    #   避险需求  20%  (DXY + 黄金)
    #   广度      15%  (上涨标的占比)
    #   趋势强度  10%  (涨跌幅绝对值分布)
    #
    # ==================================================================

    def cfgi(self) -> Dict[str, Any]:
        """计算自定义恐贪指数。"""
        scores = {}

        # --- 子分: 市场动量 (0-100) ---
        momentum_scores = []

        # 指数动量
        indices = self._by_cat("indices")
        if not indices.empty:
            avg_chg = indices["change_pct"].mean()
            # -3% → 0, 0% → 50, +3% → 100
            s = np.clip(50 + avg_chg * (50 / 3), 0, 100)
            momentum_scores.append(float(s))

        # 加密动量
        crypto = self._by_cat("crypto")
        if not crypto.empty:
            avg_chg = crypto["change_pct"].mean()
            # 加密波动大，-8% → 0, 0% → 50, +8% → 100
            s = np.clip(50 + avg_chg * (50 / 8), 0, 100)
            momentum_scores.append(float(s))

        momentum = float(np.mean(momentum_scores)) if momentum_scores else 50.0
        scores["momentum"] = round(momentum, 1)

        # --- 子分: 波动率 (0-100, VIX 越高分越低) ---
        vix = self._get_val("sentiment", "VIX")
        if vix is not None and vix > 0:
            # VIX: 10→100, 20→60, 30→30, 40→10, 50+→0
            vol_score = np.clip(100 - (vix - 10) * 2.5, 0, 100)
            scores["volatility"] = round(float(vol_score), 1)
        else:
            scores["volatility"] = 50.0

        # --- 子分: 避险需求 (0-100, 避险越强分越低) ---
        dxy_chg = self._get_chg("sentiment", "DXY")
        gold_chg = self._get_chg("commodities", "GC=F")
        safe_haven_signals = []

        if dxy_chg is not None:
            # DXY 涨 → 避险 → 分低; DXY 跌 → 风险偏好 → 分高
            s = np.clip(50 - dxy_chg * 10, 0, 100)
            safe_haven_signals.append(float(s))

        if gold_chg is not None:
            # 黄金涨 → 避险 → 分低; 黄金跌 → 分高
            s = np.clip(50 - gold_chg * 8, 0, 100)
            safe_haven_signals.append(float(s))

        safe_haven = float(np.mean(safe_haven_signals)) if safe_haven_signals else 50.0
        scores["safe_haven"] = round(safe_haven, 1)

        # --- 子分: 广度 (0-100, 上涨标的占比) ---
        all_chg = []
        for cat in ["indices", "crypto", "commodities"]:
            sub = self._by_cat(cat)
            if not sub.empty:
                all_chg.extend(sub["change_pct"].dropna().tolist())

        if all_chg:
            advancing = sum(1 for c in all_chg if c > 0)
            breadth = advancing / len(all_chg) * 100
            scores["breadth"] = round(breadth, 1)
        else:
            scores["breadth"] = 50.0

        # --- 子分: 趋势强度 (0-100) ---
        if all_chg:
            abs_chg = [abs(c) for c in all_chg]
            avg_abs = float(np.mean(abs_chg))
            # 均匀小波动 → 50 (均衡); 大幅单边 → 偏向 0 或 100
            positive_ratio = sum(1 for c in all_chg if c > 0) / len(all_chg)
            # 大幅单边涨 → 高分, 大幅单边跌 → 低分
            if positive_ratio > 0.6:
                trend = 50 + avg_abs * 5  # 涨势
            elif positive_ratio < 0.4:
                trend = 50 - avg_abs * 5  # 跌势
            else:
                trend = 50.0  # 均衡
            scores["trend_strength"] = round(float(np.clip(trend, 0, 100)), 1)
        else:
            scores["trend_strength"] = 50.0

        # --- 加权汇总 ---
        weights = {
            "momentum":      0.30,
            "volatility":    0.25,
            "safe_haven":    0.20,
            "breadth":       0.15,
            "trend_strength": 0.10,
        }
        total = sum(scores[k] * weights[k] for k in weights)
        total = round(float(np.clip(total, 0, 100)), 1)

        level = _level_lookup(CFGI_LEVELS, total)

        return {
            "score":    total,
            "label":    level["label"],
            "emoji":    level["emoji"],
            "detail":   level["description"],
            "components": scores,
            "weights":  weights,
        }

    # ==================================================================
    # 2. 市场健康度 (MHS) — 0~100
    # ==================================================================
    #
    # 权重:
    #   指数表现    30%
    #   加密表现    20%
    #   外汇稳定    15%
    #   商品信号    15%
    #   波动率      20%
    #
    # ==================================================================

    def mhs(self) -> Dict[str, Any]:
        """计算市场健康度评分。"""
        scores = {}

        # --- 指数表现 (0-100) ---
        indices = self._by_cat("indices")
        if not indices.empty:
            avg = indices["change_pct"].mean()
            pct_up = (indices["change_pct"] > 0).sum() / len(indices) * 100
            # 综合: 平均涨跌(60%) + 上涨占比(40%)
            s_avg = np.clip(50 + avg * (50 / 2), 0, 100)
            scores["indices"] = round(float(s_avg * 0.6 + pct_up * 0.4), 1)
        else:
            scores["indices"] = 50.0

        # --- 加密表现 (0-100) ---
        crypto = self._by_cat("crypto")
        if not crypto.empty:
            avg = crypto["change_pct"].mean()
            pct_up = (crypto["change_pct"] > 0).sum() / len(crypto) * 100
            s_avg = np.clip(50 + avg * (50 / 6), 0, 100)
            scores["crypto"] = round(float(s_avg * 0.6 + pct_up * 0.4), 1)
        else:
            scores["crypto"] = 50.0

        # --- 外汇稳定 (0-100) ---
        forex = self._by_cat("forex")
        if not forex.empty:
            # 外汇涨跌幅度小是正常的，偏差大表示不稳定
            avg_abs = forex["change_pct"].abs().mean()
            # abs均值: 0→100(极稳), 0.5→70, 1→50, 2+→20
            stability = np.clip(100 - avg_abs * 40, 0, 100)
            scores["forex_stability"] = round(float(stability), 1)
        else:
            scores["forex_stability"] = 50.0

        # --- 商品信号 (0-100) ---
        commodities = self._by_cat("commodities")
        if not commodities.empty:
            gold_row = commodities[commodities["symbol"] == "GC=F"]
            oil_row  = commodities[commodities["symbol"] == "CL=F"]
            signals = []

            if not gold_row.empty:
                gc_chg = float(gold_row.iloc[0]["change_pct"])
                # 黄金温和涨(避险温和) → 60-70; 暴涨 → 30(恐慌); 暴跌 → 70(风险偏好)
                if abs(gc_chg) < 1:
                    signals.append(60.0)
                elif gc_chg > 0:
                    signals.append(float(np.clip(60 - gc_chg * 5, 0, 100)))
                else:
                    signals.append(float(np.clip(60 - gc_chg * 3, 0, 100)))

            if not oil_row.empty:
                cl_chg = float(oil_row.iloc[0]["change_pct"])
                # 原油稳定 → 60; 暴涨(通胀担忧) → 30; 暴跌(衰退担忧) → 30
                signals.append(float(np.clip(60 - abs(cl_chg) * 5, 0, 100)))

            scores["commodities"] = round(float(np.mean(signals)), 1) if signals else 50.0
        else:
            scores["commodities"] = 50.0

        # --- 波动率 (0-100) ---
        vix = self._get_val("sentiment", "VIX")
        if vix is not None and vix > 0:
            scores["volatility"] = round(float(np.clip(100 - (vix - 10) * 2.5, 0, 100)), 1)
        else:
            scores["volatility"] = 50.0

        # --- 加权汇总 ---
        weights = {
            "indices":         0.30,
            "crypto":          0.20,
            "forex_stability": 0.15,
            "commodities":     0.15,
            "volatility":      0.20,
        }
        total = sum(scores[k] * weights[k] for k in weights)
        total = round(float(np.clip(total, 0, 100)), 1)

        level = _level_lookup(MHS_LEVELS, total)

        return {
            "score":    total,
            "label":    level["label"],
            "emoji":    level["emoji"],
            "detail":   level["description"],
            "components": scores,
            "weights":  weights,
        }

    # ==================================================================
    # 3. 波动率体制 (Volatility Regime)
    # ==================================================================

    def volatility(self) -> Dict[str, Any]:
        """波动率体制判断。"""
        vix = self._get_val("sentiment", "VIX")
        vix_chg = self._get_chg("sentiment", "VIX")

        if vix is None or vix <= 0:
            return {
                "regime": "unknown", "emoji": "❓",
                "vix": None, "vix_change": None,
                "detail": "VIX 数据不可用",
                "signal": "neutral",
            }

        if vix < 12:
            regime, emoji, detail, signal = (
                "low", "🟢", "极低波动 — 市场极度平静，注意尾部风险", "calm")
        elif vix < 18:
            regime, emoji, detail, signal = (
                "normal_low", "🟢", "低波动 — 市场稳定，适合趋势策略", "bullish")
        elif vix < 22:
            regime, emoji, detail, signal = (
                "normal", "🟡", "正常波动 — 市场处于常态区间", "neutral")
        elif vix < 28:
            regime, emoji, detail, signal = (
                "elevated", "🟠", "偏高波动 — 市场不确定性上升", "caution")
        elif vix < 35:
            regime, emoji, detail, signal = (
                "high", "🔴", "高波动 — 市场恐慌，控制仓位", "bearish")
        else:
            regime, emoji, detail, signal = (
                "extreme", "💀", "极端波动 — 系统性风险，极度谨慎", "panic")

        # 额外: VIX 期限结构信号
        vix_term = self._get_val("sentiment", "vix_term")
        term_signal = ""
        if vix_term is not None:
            if vix_term > 1.1:
                term_signal = " (期限结构正常 contango)"
            elif vix_term < 0.95:
                term_signal = " (⚠️ 期限结构倒挂 backwardation — 恐慌信号)"

        return {
            "regime":      regime,
            "emoji":       emoji,
            "vix":         round(vix, 2),
            "vix_change":  round(vix_chg, 2) if vix_chg else None,
            "detail":      detail + term_signal,
            "signal":      signal,
        }

    # ==================================================================
    # 4. 综合信号 — 快速决策参考
    # ==================================================================

    def signals(self) -> List[Dict[str, Any]]:
        """
        综合各指标生成简洁的交易信号列表。
        """
        signals = []
        cfgi = self.cfgi()
        mhs  = self.mhs()
        vol  = self.volatility()

        # 恐贪极端信号
        if cfgi["score"] <= 20:
            signals.append({
                "type": "contrarian_buy", "strength": "strong",
                "emoji": "🟢", "source": "CFGI",
                "message": f"恐贪指数 {cfgi['score']} — 极度恐慌，逆向买入机会",
            })
        elif cfgi["score"] >= 85:
            signals.append({
                "type": "contrarian_sell", "strength": "strong",
                "emoji": "🔴", "source": "CFGI",
                "message": f"恐贪指数 {cfgi['score']} — 极度贪婪，注意回调风险",
            })

        # 波动率信号
        if vol["regime"] in ("high", "extreme"):
            signals.append({
                "type": "risk_off", "strength": "strong" if vol["regime"] == "extreme" else "moderate",
                "emoji": "⚠️", "source": "VIX",
                "message": f"VIX={vol['vix']} ({vol['regime']}) — 降低仓位，对冲风险",
            })
        elif vol["regime"] == "low" and vol["vix"] < 12:
            signals.append({
                "type": "complacency", "strength": "moderate",
                "emoji": "⚡", "source": "VIX",
                "message": f"VIX={vol['vix']} 极低 — 市场过度平静，警惕黑天鹅",
            })

        # 健康度背离信号
        if cfgi["score"] > 70 and mhs["score"] < 40:
            signals.append({
                "type": "divergence", "strength": "moderate",
                "emoji": "🔀", "source": "CFGI+MHS",
                "message": f"恐贪({cfgi['score']})偏贪但健康度({mhs['score']})偏弱 — 量价背离，谨慎追涨",
            })

        # DXY 信号
        dxy_chg = self._get_chg("sentiment", "DXY")
        if dxy_chg is not None and abs(dxy_chg) > 1.0:
            direction = "走强" if dxy_chg > 0 else "走弱"
            signals.append({
                "type": "macro", "strength": "moderate",
                "emoji": "💵", "source": "DXY",
                "message": f"美元指数大幅{direction} ({dxy_chg:+.2f}%) — 关注资金流向",
            })

        return signals

    # ==================================================================
    # 完整报告
    # ==================================================================

    def report(self) -> Dict[str, Any]:
        """生成完整市场评分报告。"""
        cfgi = self.cfgi()
        mhs  = self.mhs()
        vol  = self.volatility()
        sigs = self.signals()

        # 综合评级
        composite = round(cfgi["score"] * 0.5 + mhs["score"] * 0.3 + (100 - vol["vix"] * 2 if vol["vix"] else 50) * 0.2, 1)
        composite = float(np.clip(composite, 0, 100))

        return {
            "composite_score": composite,
            "cfgi":   cfgi,
            "mhs":    mhs,
            "vol":    vol,
            "signals": sigs,
            "summary": self._format_summary(cfgi, mhs, vol, sigs, composite),
        }

    def _format_summary(self, cfgi, mhs, vol, sigs, composite) -> str:
        """生成可读的文字摘要。"""
        lines = []
        lines.append(f"{'='*50}")
        lines.append(f"  📊 市场综合评分: {composite:.0f}/100")
        lines.append(f"{'='*50}")
        lines.append(f"  {cfgi['emoji']} 恐贪指数: {cfgi['score']:.0f} — {cfgi['label']}")
        lines.append(f"  {mhs['emoji']} 市场健康度: {mhs['score']:.0f} — {mhs['label']}")
        lines.append(f"  {vol['emoji']} 波动率: VIX={vol['vix']} ({vol['regime']})")

        if sigs:
            lines.append(f"  {'─'*46}")
            lines.append(f"  信号:")
            for s in sigs:
                lines.append(f"    {s['emoji']} {s['message']}")

        lines.append(f"{'='*50}")
        return "\n".join(lines)

    def print_report(self):
        """打印完整报告。"""
        r = self.report()
        print(r["summary"])
        return r
