# -*- coding: utf-8 -*-
"""app/watchlist/job.py — 每日盘后刷新 + 接管统计（方案 §4.3 / §3.3 可观测性）

- 挂**既有 scheduler 盘后档期**（`market_cn/scheduler.py` 已有 15:30 / 17:00 / 17:25 三档），
  复用 `once_per_day` 守卫、失败重试与降级，**不新起调度器**
- 输出**接管统计 + 标签年龄**：上级（auto/agent）停摆是**最危险的静默失败模式**
  （不报错，只是标签悄悄退化成系统自算）⇒ `接管率突升 = 一线故障信号`

纪律：本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.watchlist import store
from app.watchlist.api import write_system_facts
from app.watchlist.render import trading_age_days

logger = get_logger(__name__)


def takeover_report(asof: Optional[str] = None) -> Dict[str, Any]:
    """接管统计：哪些票由 system 接管（上级答案已失效或从未给出）、失效多久、年龄多少。"""
    asof = asof or datetime.now().strftime("%Y-%m-%d")
    higher = store.fetch_higher_grade_latest()     # 未失效的高级行
    expired = store.fetch_expired_by_grade(asof)   # 已失效的行

    items: List[Dict[str, Any]] = []
    for row in expired:
        items.append({
            "market": row["market"], "symbol": row["symbol"],
            "source": row["source"], "grade": row["grade"],
            "expires_at": row.get("expires_at"),
            "age_trading_days": trading_age_days(row.get("updated_at"), asof),
            "state": "expired",
        })
    for (market, symbol), row in higher.items():
        items.append({
            "market": market, "symbol": symbol,
            "source": row["source"], "grade": row["grade"],
            "expires_at": row.get("expires_at"),
            "age_trading_days": trading_age_days(row.get("updated_at"), asof),
            "state": "active",
        })

    scope = len(store.list_watchlist_symbols())
    expired_n = sum(1 for i in items if i["state"] == "expired")
    active_higher = sum(1 for i in items if i["state"] == "active")
    report = {
        "asof": asof,
        "scope": scope,
        "higher_active": active_higher,
        "higher_expired": expired_n,
        #: 接管率 = 无有效高级答案的票 / 全部自选票
        "takeover_rate": round((scope - active_higher) / scope, 4) if scope else None,
        "items": items,
    }
    if expired_n:
        logger.warning("[label] 接管告警: %d 条高级标签已失效（接管率 %.1f%%）",
                       expired_n, (report["takeover_rate"] or 0) * 100)
    return report


def run_daily(asof: Optional[str] = None) -> Dict[str, Any]:
    """每日盘后 job：预测分衰减巡检（必要时自动重标上线）+ system 全量刷新 + 接管统计。

    产品口径（用户裁定）：**只保当前/未来预测力**。重标后立刻全量刷当前自选分，历史分不回写。
    """
    cal_stats: Dict[str, Any] = {}
    try:
        from app.watchlist.calibrate import run_auto_calibrate
        cal_stats = run_auto_calibrate()
        if (cal_stats.get("recal") or {}).get("applied"):
            logger.info("[label] 自动重标已上线 score_version=%s",
                        cal_stats["recal"].get("score_version"))
    except Exception as e:
        logger.warning("[label] 自动标定巡检失败（不影响刷分）: %s", e)

    stats = write_system_facts(asof=asof)
    report = takeover_report(asof=stats.get("asof"))
    stats["takeover"] = {
        k: report[k] for k in ("scope", "higher_active", "higher_expired", "takeover_rate")
    }
    det = cal_stats.get("detect") or {}
    rec = cal_stats.get("recal") or {}
    stats["calibrate"] = {
        "auc": det.get("auc"),
        "should_refit": det.get("should_refit"),
        "applied": rec.get("applied"),
        "score_version": rec.get("score_version") or det.get("score_version"),
    }
    logger.info("[label] 每日 job 完成: %s cal=%s", stats["takeover"], stats["calibrate"])
    return stats
