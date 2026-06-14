"""
板块历史分析 — 从 sector_daily_stats 表读取，提供趋势/周期/预测分析

对外接口（保持不变）:
  - get_sector_history(board_type, days)  → List[Dict]
  - get_sector_trend(board_type)          → Dict
  - SectorAnalyzer.full_analysis()        → Dict
  - SectorHistoryScheduler                → 每日采集调度器

数据源: sector_daily_stats 表（由 sector_daily.py 写入）
"""

import os
import logging
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Any

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════

ENABLED = os.getenv("SECTOR_HISTORY_ENABLED", "false").lower() == "true"
COLLECT_HOUR = int(os.getenv("SECTOR_COLLECT_HOUR", "15"))
COLLECT_MINUTE = int(os.getenv("SECTOR_COLLECT_MINUTE", "30"))


# ═══════════════════════════════════════════════════
#  DB 查询
# ═══════════════════════════════════════════════════

def _query_db(sector_type: str, start_date: str, end_date: str) -> List[Dict]:
    """从 sector_daily_stats 查询板块历史数据，自动计算 rank。"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    pool = mgr._get_pool("CNStock")

    with pool.cursor() as cur:
        cur.execute(
            "SELECT date, sector_type, sector_name, stock_count, "
            "limit_up_count, limit_down_count, advance_count, decline_count, "
            "total_volume, avg_return, advance_pct, heat_score "
            "FROM sector_daily_stats "
            "WHERE sector_type=%s AND date>=%s AND date<=%s "
            "ORDER BY date, heat_score DESC",
            (sector_type, start_date, end_date)
        )
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    # 按日期分组计算 rank
    from itertools import groupby
    from operator import itemgetter

    result = []
    rows.sort(key=itemgetter("date"))
    for _, group in groupby(rows, key=itemgetter("date")):
        sorted_group = sorted(group, key=lambda x: -x.get("heat_score", 0))
        for rank, row in enumerate(sorted_group, 1):
            row["rank"] = rank
            # 兼容旧字段名
            row["trade_date"] = row["date"]
            row["board_type"] = row["sector_type"]
            row["name"] = row["sector_name"]
            row["change_pct"] = row["avg_return"]
            row["amount"] = row["total_volume"]
            result.append(row)

    return result


def _has_date_in_db(sector_type: str, date: str) -> bool:
    """检查 DB 中某天是否已有数据。"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    pool = mgr._get_pool("CNStock")

    with pool.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sector_daily_stats WHERE sector_type=%s AND date=%s LIMIT 1",
            (sector_type, date)
        )
        return cur.fetchone() is not None


# ═══════════════════════════════════════════════════
#  采集调度器（调用 sector_daily.py）
# ═══════════════════════════════════════════════════

class SectorHistoryScheduler:
    """每日收盘后采集板块统计数据（写入 sector_daily_stats 表）"""

    def __init__(self):
        self._timer = None
        self._running = False

    def start(self):
        if not ENABLED:
            logger.info("[SectorHistory] 未启用 (SECTOR_HISTORY_ENABLED=false)")
            return
        debug = os.getenv("PYTHON_API_DEBUG", "false").lower() == "true"
        if debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            return
        self._running = True
        logger.info("[SectorHistory] 已启动，每日 %02d:%02d 采集", COLLECT_HOUR, COLLECT_MINUTE)
        self._schedule_next()

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self):
        if not self._running:
            return
        now = datetime.now()
        target = now.replace(hour=COLLECT_HOUR, minute=COLLECT_MINUTE, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = max(60, (target - now).total_seconds())
        self._timer = threading.Timer(delay, self._tick)
        self._timer.daemon = True
        self._timer.start()
        logger.debug("[SectorHistory] 下次采集: %s (延迟 %.0fs)", target.strftime("%Y-%m-%d %H:%M"), delay)

    def _tick(self):
        try:
            from app.utils.trading_calendar import is_trading_day_today
            if not is_trading_day_today():
                logger.debug("[SectorHistory] 非交易日，跳过")
                return
            self._collect()
        except Exception as e:
            logger.error("[SectorHistory] 采集异常: %s", e, exc_info=True)
        finally:
            self._schedule_next()

    def _collect(self):
        """调用 sector_daily.sync_single_date 采集数据"""
        from .sector_daily import sync_single_date

        today = datetime.now().strftime("%Y-%m-%d")

        if _has_date_in_db("industry", today) and _has_date_in_db("concept", today):
            logger.info("[SectorHistory] %s 已采集，跳过", today)
            return

        logger.info("[SectorHistory] 开始采集 %s 板块数据...", today)
        try:
            n = sync_single_date(today)
            logger.info("[SectorHistory] %s 采集完成，写入 %d 条", today, n)
        except Exception as e:
            logger.error("[SectorHistory] 采集失败: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════
#  分析引擎
# ═══════════════════════════════════════════════════

def _is_valid_name(name) -> bool:
    if name is None:
        return False
    s = str(name)
    if s == "" or s == "nan" or s == "None":
        return False
    try:
        if pd.isna(name):
            return False
    except (TypeError, ValueError):
        pass
    return True


class SectorAnalyzer:
    """板块历史分析引擎（从 sector_daily_stats 表读取）

    三级分析:
      1. 趋势分析（1个月）— 热度分变化、涨跌持续性
      2. 周期分析（6个月）— 季节性规律、轮动模式
      3. 今日预测 — 基于历史模式匹配
    """

    def full_analysis(self, board_type="industry") -> Dict[str, Any]:
        board_label = "行业板块" if board_type == "industry" else "概念板块"

        today = datetime.now().strftime("%Y-%m-%d")
        six_months_ago = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
        one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        all_data = _query_db(board_type, six_months_ago, today)

        if not all_data:
            return {
                "board_type": board_type,
                "data_days": 0,
                "trend": {"summary": f"暂无{board_label}历史数据", "items": []},
                "cycle": {"summary": "数据不足", "patterns": [], "seasonal_candidates": []},
                "prediction": {"summary": "数据不足，至少需要3个交易日", "candidates": []},
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        df = pd.DataFrame(all_data)
        df["heat_score"] = pd.to_numeric(df["heat_score"], errors="coerce").fillna(0)
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce").fillna(999)
        df["avg_return"] = pd.to_numeric(df["avg_return"], errors="coerce").fillna(0)
        df["total_volume"] = pd.to_numeric(df["total_volume"], errors="coerce").fillna(0)
        df["advance_pct"] = pd.to_numeric(df["advance_pct"], errors="coerce").fillna(0)
        df["limit_up_count"] = pd.to_numeric(df["limit_up_count"], errors="coerce").fillna(0)

        df = df[df["sector_name"].apply(_is_valid_name)].copy()

        dates = sorted(df["date"].unique())
        total_days = len(dates)

        trend = self._analyze_trend(df, dates, one_month_ago)
        cycle = self._analyze_cycle(df, dates, six_months_ago)
        prediction = self._predict_today(df, dates, trend, cycle)

        return {
            "board_type": board_type,
            "data_days": total_days,
            "date_range": {"start": dates[0] if dates else "", "end": dates[-1] if dates else ""},
            "trend": trend,
            "cycle": cycle,
            "prediction": prediction,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 趋势分析（1个月）─────────────────────────

    def _analyze_trend(self, df: pd.DataFrame, dates: list, since_date: str) -> Dict:
        recent_dates = [d for d in dates if d >= since_date]
        if len(recent_dates) < 3:
            return {"summary": f"最近30天仅{len(recent_dates)}个交易日数据，趋势分析不足", "items": []}

        recent_df = df[df["date"].isin(recent_dates)]
        all_names = recent_df["sector_name"].dropna().unique()

        results = []
        for name in all_names:
            if not _is_valid_name(name):
                continue

            name_df = recent_df[recent_df["sector_name"] == name].sort_values("date")
            if len(name_df) < 3:
                continue

            heat_scores = name_df["heat_score"].values.astype(float)
            returns = name_df["avg_return"].values.astype(float)
            ranks = name_df["rank"].values.astype(float)
            volumes = name_df["total_volume"].values.astype(float)

            avg_heat = np.nanmean(heat_scores)
            first_heat = heat_scores[0] if not np.isnan(heat_scores[0]) else avg_heat
            last_heat = heat_scores[-1] if not np.isnan(heat_scores[-1]) else avg_heat
            heat_change = last_heat - first_heat

            avg_return = np.nanmean(returns) if len(returns) > 0 else 0
            valid_returns = returns[~np.isnan(returns)]
            positive_days = int(np.sum(valid_returns > 0)) if len(valid_returns) > 0 else 0
            total_valid = len(valid_returns)
            win_rate = positive_days / total_valid if total_valid > 0 else 0.5

            avg_rank = np.nanmean(ranks)

            valid_vol = volumes[~np.isnan(volumes)]
            if len(valid_vol) >= 5:
                recent_vol = np.mean(valid_vol[-5:])
                early_vol = np.mean(valid_vol[:5])
            elif len(valid_vol) >= 2:
                recent_vol = np.mean(valid_vol[-max(1, len(valid_vol)//2):])
                early_vol = np.mean(valid_vol[:max(1, len(valid_vol)//2)])
            else:
                recent_vol = early_vol = valid_vol[0] if len(valid_vol) > 0 else 0
            vol_change = (recent_vol / early_vol - 1) * 100 if early_vol > 0 else 0

            score = 50.0
            score += np.clip(heat_change * 0.5, -20, 20)
            score += np.clip(avg_return * 3, -15, 15)
            score += np.clip((win_rate - 0.5) * 20, -10, 10)
            score += np.clip(vol_change / 10, -5, 5)
            score = float(np.clip(score, 0, 100))

            if heat_change > 10 and avg_return > 0.5:
                direction = "🔥 持续走强"
            elif heat_change > 3 and avg_return > 0:
                direction = "📈 温和上行"
            elif heat_change < -10 and avg_return < -0.5:
                direction = "❄️ 持续走弱"
            elif heat_change < -3 and avg_return < 0:
                direction = "📉 温和下行"
            else:
                direction = "➡️ 横盘震荡"

            results.append({
                "name": str(name),
                "appearances": total_valid,
                "avg_rank": round(avg_rank, 1),
                "heat_change": round(heat_change, 1),
                "avg_return": round(avg_return, 3),
                "win_rate": round(win_rate * 100, 1),
                "vol_change_pct": round(vol_change, 1),
                "score": round(score, 1),
                "direction": direction,
            })

        results.sort(key=lambda x: x["score"], reverse=True)

        top_strong = [r for r in results[:5] if r["score"] > 60]
        top_weak = [r for r in results[-3:] if r["score"] < 40]
        summary_parts = []
        if top_strong:
            summary_parts.append(f"近1月强势: {', '.join(r['name'] for r in top_strong[:3])}")
        if top_weak:
            summary_parts.append(f"近1月弱势: {', '.join(r['name'] for r in top_weak)}")

        return {
            "summary": "；".join(summary_parts) if summary_parts else "整体无明显趋势性板块",
            "items": results[:20],
            "strong_count": len([r for r in results if r["score"] > 60]),
            "weak_count": len([r for r in results if r["score"] < 40]),
        }

    # ── 周期分析（6个月）─────────────────────────

    def _analyze_cycle(self, df: pd.DataFrame, dates: list, since_date: str) -> Dict:
        hist_df = df[df["date"] >= since_date].copy()
        if hist_df.empty:
            return {"summary": "无历史数据", "patterns": [], "seasonal_candidates": []}

        hist_df["month"] = hist_df["date"].str[:7]

        patterns = []
        for name, group in hist_df.groupby("sector_name"):
            if not _is_valid_name(name):
                continue

            month_stats = {}
            for month, mg in group.groupby("month"):
                month_stats[month] = {
                    "appearances": len(mg),
                    "avg_heat": round(float(mg["heat_score"].mean()), 1),
                    "avg_return": round(float(mg["avg_return"].mean()), 3),
                    "best_rank": int(mg["rank"].min()),
                    "total_volume": round(float(mg["total_volume"].sum()) / 1e8, 2),
                }

            if not month_stats:
                continue

            items = list(month_stats.items())
            best_month = max(items, key=lambda x: x[1]["avg_return"])
            worst_month = min(items, key=lambda x: x[1]["avg_return"])
            total_appearances = sum(m["appearances"] for m in month_stats.values())

            patterns.append({
                "name": str(name),
                "months_active": len(month_stats),
                "total_appearances": total_appearances,
                "best_month": {"month": best_month[0], **best_month[1]},
                "worst_month": {"month": worst_month[0], **worst_month[1]},
                "month_details": month_stats,
            })

        patterns.sort(key=lambda x: x["total_appearances"], reverse=True)

        current_month = datetime.now().strftime("%Y-%m")
        seasonal_candidates = []
        for p in patterns:
            detail = p.get("month_details", {}).get(current_month)
            if detail:
                seasonal_candidates.append({
                    "name": p["name"],
                    "reason": f"历史同期出现{detail['appearances']}次，平均涨幅{detail['avg_return']:.2f}%",
                    "historical_avg_return": detail["avg_return"],
                    "historical_appearances": detail["appearances"],
                })
        seasonal_candidates.sort(key=lambda x: x["historical_avg_return"], reverse=True)

        summary_parts = []
        if seasonal_candidates:
            summary_parts.append(f"本月历史规律: {', '.join(c['name'] for c in seasonal_candidates[:3])}")

        return {
            "summary": "；".join(summary_parts) if summary_parts else "暂无明显季节性规律",
            "patterns": patterns[:30],
            "seasonal_candidates": seasonal_candidates[:10],
            "current_month": current_month,
        }

    # ── 今日预测 ──────────────────────────────

    def _predict_today(self, df: pd.DataFrame, dates: list,
                       trend: Dict, cycle: Dict) -> Dict:
        candidates = {}

        def _add_candidate(name, source_score, source_key, reason, weight):
            if not _is_valid_name(name):
                return
            key = str(name)
            if key in candidates:
                candidates[key]["reasons"].append(reason)
                candidates[key]["composite_score"] += source_score * weight
            else:
                candidates[key] = {
                    "name": key,
                    "trend_score": 0,
                    "trend_direction": "",
                    "cycle_score": 0,
                    "reasons": [reason],
                    "composite_score": source_score * weight,
                }
            if source_key == "trend_score":
                candidates[key]["trend_score"] = max(candidates[key]["trend_score"], source_score)
            elif source_key == "cycle_score":
                candidates[key]["cycle_score"] = max(candidates[key]["cycle_score"], source_score)

        for item in trend.get("items", [])[:10]:
            score = item.get("score", 0) or 0
            if score > 55:
                _add_candidate(
                    item.get("name"), score, "trend_score",
                    f"近1月趋势: {item.get('direction', '')} (评分{score})", 0.4
                )
                if item.get("name") and _is_valid_name(item["name"]):
                    candidates[str(item["name"])]["trend_direction"] = item.get("direction", "")

        for item in cycle.get("seasonal_candidates", [])[:10]:
            avg_return = item.get("historical_avg_return", 0) or 0
            appearances = item.get("historical_appearances", 0) or 0
            seasonal_score = min(100, 50 + avg_return * 10 + appearances * 2)
            _add_candidate(
                item.get("name"), seasonal_score, "cycle_score",
                f"历史同期: 出现{appearances}次，平均涨{avg_return:.2f}%", 0.35
            )

        if dates:
            latest_date = dates[-1]
            latest_df = df[df["date"] == latest_date]
            for _, row in latest_df.head(10).iterrows():
                name = row.get("sector_name")
                rank_val = row.get("rank", 999)
                try:
                    rank_int = int(float(rank_val)) if pd.notna(rank_val) else 999
                except (ValueError, TypeError):
                    rank_int = 999
                today_score = max(0, 100 - rank_int * 3)
                _add_candidate(
                    name, today_score, None,
                    f"最新排名: 第{rank_int}位", 0.25
                )

        ranked = sorted(candidates.values(), key=lambda x: x["composite_score"], reverse=True)
        max_score = max((c["composite_score"] for c in ranked), default=1) or 1
        for i, c in enumerate(ranked, 1):
            c["rank"] = i
            c["composite_score"] = round(min(100, (c["composite_score"] / max_score) * 100), 1)

        top3 = [c["name"] for c in ranked[:3]]
        summary = f"今日预测热门: {', '.join(top3)}" if top3 else "数据不足，无法预测"

        return {
            "summary": summary,
            "candidates": ranked[:15],
            "method": "趋势(40%) + 季节性(35%) + 最新排名(25%)",
        }


# ═══════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════

def get_sector_trend(board_type="industry") -> Dict:
    """获取板块趋势分析"""
    return SectorAnalyzer().full_analysis(board_type)


def get_sector_history(board_type="industry", days=30) -> List[Dict]:
    """获取板块历史统计（供前端图表使用）"""
    today = datetime.now().strftime("%Y-%m-%d")
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return _query_db(board_type, since, today)
