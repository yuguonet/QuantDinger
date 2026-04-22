#!/usr/bin/env python3
"""
板块历史存储 + 趋势/周期/预测分析

存储: 每日收盘后采集行业板块 & 概念板块排名，存入 feather
分析:
  - 1个月排名变化趋势（哪些板块持续走强/走弱）
  - 6个月周期分析（季节性规律、轮动模式）
  - 今日热门预测（基于历史模式匹配）

数据源: 东方财富 API → hot_sectors.py
存储: cache_db (feather) → cnd_sector_history / cnd_concept_history
"""

import os
import logging
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════

ENABLED = os.getenv("SECTOR_HISTORY_ENABLED", "false").lower() == "true"
COLLECT_HOUR = int(os.getenv("SECTOR_COLLECT_HOUR", "15"))
COLLECT_MINUTE = int(os.getenv("SECTOR_COLLECT_MINUTE", "30"))
RETENTION_DAYS = int(os.getenv("SECTOR_RETENTION_DAYS", "200"))
TOP_N = int(os.getenv("SECTOR_TOP_N", "30"))

SECTOR_TABLE = "cnd_sector_history"
CONCEPT_TABLE = "cnd_concept_history"


# ═══════════════════════════════════════════════════
#  采集调度器
# ═══════════════════════════════════════════════════

class SectorHistoryScheduler:
    """每日收盘后采集板块排名数据"""

    def __init__(self, db):
        self.db = db
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
        delay = max(60, (target - now).total_seconds())  # 最少 60s，防止 0 延迟
        self._timer = threading.Timer(delay, self._tick)
        self._timer.daemon = True
        self._timer.start()
        logger.debug("[SectorHistory] 下次采集: %s (延迟 %.0fs)", target.strftime("%Y-%m-%d %H:%M"), delay)

    def _tick(self):
        try:
            from app.interfaces.trading_calendar import is_trading_day_today
            if not is_trading_day_today():
                logger.debug("[SectorHistory] 非交易日，跳过")
                return
            self._collect()
        except Exception as e:
            logger.error("[SectorHistory] 采集异常: %s", e, exc_info=True)
        finally:
            self._schedule_next()

    def _collect(self):
        """采集板块数据并存储（行业+概念并行）"""
        from app.market_cn.hot_sectors import _fetch_board_list, _analyze_continuity

        today = datetime.now().strftime("%Y-%m-%d")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 检查今天是否已采集
        existing = self.db.query_dates_exist(SECTOR_TABLE, "trade_date", today, today)
        if existing:
            logger.info("[SectorHistory] %s 已采集，跳过", today)
            return

        logger.info("[SectorHistory] 开始采集 %s 板块数据...", today)

        collected_any = False

        def _collect_one(board_type, table):
            """采集单个板块类型"""
            raw = _fetch_board_list(board_type, sort_by="f3", sort_dir="desc", limit=TOP_N)
            if not raw:
                logger.warning("[SectorHistory] %s 返回空数据", board_type)
                return False

            analyzed = _analyze_continuity(raw)
            rows = []
            for rank, b in enumerate(analyzed, 1):
                rows.append({
                    "trade_date": today,
                    "board_type": board_type,
                    "name": b.get("name", ""),
                    "code": b.get("code", ""),
                    "rank": rank,
                    "change_pct": b.get("change_pct", 0) or 0,
                    "amount": b.get("amount", 0) or 0,
                    "turnover": b.get("turnover", 0) or 0,
                    "up_count": b.get("up_count", 0) or 0,
                    "down_count": b.get("down_count", 0) or 0,
                    "lead_stock": b.get("lead_stock", ""),
                    "lead_stock_pct": b.get("lead_stock_pct", 0) or 0,
                    "limit_up_count": b.get("limit_up_count", 0) or 0,
                    "total_mv": b.get("total_mv", 0) or 0,
                    "fetch_time": now_str,
                })

            if rows:
                self.db.insert_batch(table, rows)
                logger.info("[SectorHistory] %s: %d 条", board_type, len(rows))
                return True
            return False

        # 并行采集行业+概念
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_industry = pool.submit(_collect_one, "industry", SECTOR_TABLE)
            f_concept = pool.submit(_collect_one, "concept", CONCEPT_TABLE)

            try:
                if f_industry.result(timeout=30):
                    collected_any = True
            except Exception as e:
                logger.error("[SectorHistory] 行业板块采集失败: %s", e)

            try:
                if f_concept.result(timeout=30):
                    collected_any = True
            except Exception as e:
                logger.error("[SectorHistory] 概念板块采集失败: %s", e)

        # 只有采集到数据才裁剪
        if collected_any:
            cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
            self._prune(SECTOR_TABLE, cutoff)
            self._prune(CONCEPT_TABLE, cutoff)

    def _prune(self, table: str, cutoff: str):
        """裁剪过期数据（使用 cache_db 公开 API）"""
        try:
            all_rows = self.db.query(table)
            if not all_rows:
                return
            keep = [r for r in all_rows if r.get("trade_date", "") >= cutoff]
            removed = len(all_rows) - len(keep)
            if removed > 0:
                self.db.replace_rows(table, keep)
                logger.info("[SectorHistory] 裁剪 %s: 删除 %d 条过期数据", table, removed)
        except Exception as e:
            logger.error("[SectorHistory] 裁剪 %s 失败: %s", table, e)


# ═══════════════════════════════════════════════════
#  分析引擎
# ═══════════════════════════════════════════════════

def _is_valid_name(name) -> bool:
    """检查板块名是否有效（排除 NaN / None / 空字符串）"""
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
    """板块历史分析引擎

    三级分析:
      1. 趋势分析（1个月）— 排名变化、涨跌持续性
      2. 周期分析（6个月）— 季节性规律、轮动模式
      3. 今日预测 — 基于历史模式匹配
    """

    def __init__(self, db):
        self.db = db

    def full_analysis(self, board_type="industry") -> Dict[str, Any]:
        """完整分析报告"""
        table = SECTOR_TABLE if board_type == "industry" else CONCEPT_TABLE
        board_label = "行业板块" if board_type == "industry" else "概念板块"

        today = datetime.now().strftime("%Y-%m-%d")
        six_months_ago = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
        one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        # 加载历史数据
        all_data = self.db.query_between_dates(
            table, "trade_date", six_months_ago, today, order_by="trade_date"
        )

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
        df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce").fillna(999)
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)

        # 过滤无效 name
        df = df[df["name"].apply(_is_valid_name)].copy()

        dates = sorted(df["trade_date"].unique())
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
        """1个月排名变化趋势"""
        recent_dates = [d for d in dates if d >= since_date]
        if len(recent_dates) < 3:
            return {"summary": f"最近30天仅{len(recent_dates)}个交易日数据，趋势分析不足", "items": []}

        recent_df = df[df["trade_date"].isin(recent_dates)]
        all_names = recent_df["name"].dropna().unique()

        results = []
        for name in all_names:
            if not _is_valid_name(name):
                continue

            name_df = recent_df[recent_df["name"] == name].sort_values("trade_date")
            if len(name_df) < 3:
                continue

            ranks = name_df["rank"].values.astype(float)
            pct_values = name_df["change_pct"].values.astype(float)
            amounts = name_df["amount"].values.astype(float)

            # 跳过全 NaN
            if np.all(np.isnan(ranks)):
                continue

            avg_rank = np.nanmean(ranks)
            first_rank = ranks[0] if not np.isnan(ranks[0]) else avg_rank
            last_rank = ranks[-1] if not np.isnan(ranks[-1]) else avg_rank
            rank_change = first_rank - last_rank

            avg_pct = np.nanmean(pct_values) if len(pct_values) > 0 else 0
            valid_pct = pct_values[~np.isnan(pct_values)]
            positive_days = int(np.sum(valid_pct > 0)) if len(valid_pct) > 0 else 0
            total_valid = len(valid_pct)
            win_rate = positive_days / total_valid if total_valid > 0 else 0.5

            valid_amt = amounts[~np.isnan(amounts)]
            if len(valid_amt) >= 5:
                recent_amt = np.mean(valid_amt[-5:])
                early_amt = np.mean(valid_amt[:5])
            elif len(valid_amt) >= 2:
                recent_amt = np.mean(valid_amt[-max(1, len(valid_amt)//2):])
                early_amt = np.mean(valid_amt[:max(1, len(valid_amt)//2)])
            else:
                recent_amt = early_amt = valid_amt[0] if len(valid_amt) > 0 else 0
            amt_change = (recent_amt / early_amt - 1) * 100 if early_amt > 0 else 0

            # 综合评分
            score = 50.0
            score += np.clip(rank_change * 2, -20, 20)
            score += np.clip(avg_pct * 3, -15, 15)
            score += np.clip((win_rate - 0.5) * 20, -10, 10)
            score += np.clip(amt_change / 10, -5, 5)
            score = float(np.clip(score, 0, 100))

            # 趋势方向
            if rank_change > 5 and avg_pct > 0.5:
                direction = "🔥 持续走强"
            elif rank_change > 2 and avg_pct > 0:
                direction = "📈 温和上行"
            elif rank_change < -5 and avg_pct < -0.5:
                direction = "❄️ 持续走弱"
            elif rank_change < -2 and avg_pct < 0:
                direction = "📉 温和下行"
            else:
                direction = "➡️ 横盘震荡"

            results.append({
                "name": str(name),
                "appearances": total_valid,
                "avg_rank": round(avg_rank, 1),
                "rank_change": int(rank_change),
                "avg_pct": round(avg_pct, 3),
                "win_rate": round(win_rate * 100, 1),
                "amt_change_pct": round(amt_change, 1),
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
        """6个月周期分析：按月统计板块出现频率和表现"""
        hist_df = df[df["trade_date"] >= since_date].copy()
        if hist_df.empty:
            return {"summary": "无历史数据", "patterns": [], "seasonal_candidates": []}

        hist_df["month"] = hist_df["trade_date"].str[:7]

        patterns = []
        for name, group in hist_df.groupby("name"):
            if not _is_valid_name(name):
                continue

            month_stats = {}
            for month, mg in group.groupby("month"):
                month_stats[month] = {
                    "appearances": len(mg),
                    "avg_rank": round(float(mg["rank"].mean()), 1),
                    "avg_pct": round(float(mg["change_pct"].mean()), 3),
                    "best_rank": int(mg["rank"].min()),
                    "total_amount": round(float(mg["amount"].sum()) / 1e8, 2),
                }

            # 放宽到 1 个月也保留（数据初期）
            if not month_stats:
                continue

            items = list(month_stats.items())
            best_month = max(items, key=lambda x: x[1]["avg_pct"])
            worst_month = min(items, key=lambda x: x[1]["avg_pct"])
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

        # 季节性规律
        current_month = datetime.now().strftime("%Y-%m")
        seasonal_candidates = []
        for p in patterns:
            detail = p.get("month_details", {}).get(current_month)
            if detail:
                seasonal_candidates.append({
                    "name": p["name"],
                    "reason": f"历史同期出现{detail['appearances']}次，平均涨幅{detail['avg_pct']:.2f}%",
                    "historical_avg_pct": detail["avg_pct"],
                    "historical_appearances": detail["appearances"],
                })
        seasonal_candidates.sort(key=lambda x: x["historical_avg_pct"], reverse=True)

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
        """综合趋势 + 周期，预测今日可能的热门板块"""
        candidates = {}

        def _add_candidate(name, source_score, source_key, reason, weight):
            """安全添加候选板块，处理 NaN/重复"""
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
            # 更新来源评分
            if source_key == "trend_score":
                candidates[key]["trend_score"] = max(candidates[key]["trend_score"], source_score)
            elif source_key == "cycle_score":
                candidates[key]["cycle_score"] = max(candidates[key]["cycle_score"], source_score)

        # 来源1: 趋势强势板块（权重 40%）
        for item in trend.get("items", [])[:10]:
            score = item.get("score", 0) or 0
            if score > 55:
                _add_candidate(
                    item.get("name"), score, "trend_score",
                    f"近1月趋势: {item.get('direction', '')} (评分{score})", 0.4
                )
                # 保留方向
                if item.get("name") and _is_valid_name(item["name"]):
                    candidates[str(item["name"])]["trend_direction"] = item.get("direction", "")

        # 来源2: 季节性板块（权重 35%）
        for item in cycle.get("seasonal_candidates", [])[:10]:
            avg_pct = item.get("historical_avg_pct", 0) or 0
            appearances = item.get("historical_appearances", 0) or 0
            seasonal_score = min(100, 50 + avg_pct * 10 + appearances * 2)
            _add_candidate(
                item.get("name"), seasonal_score, "cycle_score",
                f"历史同期: 出现{appearances}次，平均涨{avg_pct:.2f}%", 0.35
            )

        # 来源3: 最新排名 Top（权重 25%）
        if dates:
            latest_date = dates[-1]
            latest_df = df[df["trade_date"] == latest_date]
            for _, row in latest_df.head(10).iterrows():
                name = row.get("name")
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

        # 归一化: composite_score 最高 100
        ranked = sorted(candidates.values(), key=lambda x: x["composite_score"], reverse=True)
        max_score = max((c["composite_score"] for c in ranked), default=1) or 1
        for i, c in enumerate(ranked, 1):
            c["rank"] = i
            # 归一化到 0-100
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

def get_sector_trend(db, board_type="industry") -> Dict:
    """获取板块趋势分析"""
    analyzer = SectorAnalyzer(db)
    return analyzer.full_analysis(board_type)


def get_sector_history(db, board_type="industry", days=30) -> List[Dict]:
    """获取板块历史排名（供前端图表使用）"""
    table = SECTOR_TABLE if board_type == "industry" else CONCEPT_TABLE
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    return db.query_between_dates(table, "trade_date", since, today, order_by="-trade_date")
