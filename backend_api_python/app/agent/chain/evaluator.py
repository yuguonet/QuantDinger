# -*- coding: utf-8 -*-
"""
Evaluator — 回溯评估引擎（重写版）。

基于 qd_traces 表 + qd_agent_weights。

核心流程（每日盘后自动运行）：
  evaluate_pending()      → 按 timeframe 取实际行情，写回 qd_traces
  update_skill_weights()  → 按单位时间收益率聚合 Skill 权重 + 自动同步 registry
  update_factor_weights() → 按单位时间收益率聚合因子权重（带时间衰减）+ 清理过期因子
  auto_evaluate()         → 自动闭环

核心指标：单位时间期望收益率（不是胜率）
  return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days

纯 SQL + 数学，0 token 消耗，不涉及 agent。
"""
from __future__ import annotations

import json
from log import logger
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from chain.schema import (
    DIRECTION_THRESHOLD, Direction, classify_return, is_direction_correct,
)
from chain import store
# ═══════════════════════════════════════════════════════════════
# timeframe → 验证窗口映射
# ═══════════════════════════════════════════════════════════════

_TIMEFRAME_DAYS = {
    "T+1": 1,
    "T+3": 3,
    "T+5": 5,
    "1W": 5,
    "1M": 22,
    "3M": 66,
    "1Y": 252,
}

_DEFAULT_HOLD_DAYS = 3  # timeframe 缺失时的默认值
def _get_hold_days(timeframe: str) -> int:
    """timeframe → 验证用持有天数。"""
    return _TIMEFRAME_DAYS.get(timeframe, _DEFAULT_HOLD_DAYS)
# ═══════════════════════════════════════════════════════════════
# 实际行情获取
# ═══════════════════════════════════════════════════════════════

def _get_actual_return(
    stock_code: str,
    from_date: date,
    hold_days: int,
    market: str = "CNStock",
) -> Optional[Dict[str, Any]]:
    """获取股票实际涨跌数据。

    Args:
        stock_code: 股票代码
        from_date: 决策日期
        hold_days: 持有天数
        market: 市场类型

    Returns:
        {"pnl_pct": float, "hold_days": int, "direction": str} 或 None
    """
    try:
        from app.agent.tools.data_tools import agent_get_kline

        klines = agent_get_kline(stock_code, timeframe="1D", days=hold_days + 10, market=market)
        if not klines or len(klines) < 2:
            return None

        from_str = from_date.strftime("%Y-%m-%d")
        base_idx = None
        for i, k in enumerate(klines):
            k_date = k.get("t", "")[:10]
            if k_date >= from_str:
                base_idx = i
                break

        if base_idx is None:
            return None

        base_close = klines[base_idx]["c"]
        exit_idx = min(base_idx + hold_days, len(klines) - 1)
        if exit_idx <= base_idx:
            return None

        exit_close = klines[exit_idx]["c"]
        pnl_pct = round((exit_close - base_close) / base_close * 100, 2)
        actual_hold = exit_idx - base_idx
        direction = classify_return(pnl_pct / 100)

        return {
            "pnl_pct": pnl_pct,
            "hold_days": actual_hold,
            "direction": direction,
            "exit_date": klines[exit_idx].get("t", "")[:10],
        }

    except Exception as e:
        logger.warning("[Evaluator] 获取实际涨跌失败 %s: %s", stock_code, e)
        return None
# ═══════════════════════════════════════════════════════════════
# 评估执行
# ═══════════════════════════════════════════════════════════════

def evaluate_pending(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """评估所有待验证的决策记录。

    查找 qd_traces 中 exit_date IS NULL 的根节点，
    按 timeframe 取实际行情，写回验证结果。

    Args:
        days_old: 只评估至少 N 天前的决策
        market: 市场类型

    Returns:
        {"evaluated": int, "errors": int, "details": list}
    """
    stats = {"evaluated": 0, "errors": 0, "details": []}

    pending = store.query_pending_verify(days_old=days_old, limit=100)

    for item in pending:
        root_id = item["id"]
        exec_date = item["exec_date"]
        stock_code = item["stock_code"]
        action = item["action"]
        timeframe = item.get("timeframe", "")

        try:
            hold_days = _get_hold_days(timeframe)
            actual = _get_actual_return(stock_code, exec_date, hold_days, market)
            if not actual:
                continue

            # 方向映射
            action_to_dir = {
                "buy": Direction.BULLISH.value,
                "sell": Direction.BEARISH.value,
                "hold": Direction.NEUTRAL.value,
                "skip": Direction.NEUTRAL.value,
            }
            predicted_dir = action_to_dir.get(action, Direction.NEUTRAL.value)
            actual_dir = actual["direction"]
            verdict = is_direction_correct(predicted_dir, actual_dir)

            if verdict == "neutral":
                correct = None
            else:
                correct = verdict == "correct"

            from datetime import datetime
            exit_date = None
            if actual.get("exit_date"):
                try:
                    exit_date = date.fromisoformat(actual["exit_date"])
                except (ValueError, TypeError):
                    pass

            store.update_verify_results(
                root_id=root_id,
                exit_date=exit_date or exec_date + timedelta(days=hold_days),
                exit_reason="max_hold",
                pnl_pct=actual["pnl_pct"],
                hold_days=actual["hold_days"],
                correct=correct,
            )

            # 写入 skill 子节点验证结果
            store.update_skill_verify(root_id, actual_dir)

            # 更新编排路径缓存
            store.update_path_cache(root_id)

            stats["evaluated"] += 1
            stats["details"].append({
                "root_id": root_id, "stock": stock_code,
                "action": action, "timeframe": timeframe,
                "pnl_pct": actual["pnl_pct"], "correct": correct,
            })

        except Exception as e:
            stats["errors"] += 1
            stats["details"].append({
                "root_id": root_id, "stock": stock_code,
                "status": "error", "error": str(e),
            })
            logger.error("[Evaluator] 评估 root_id=%d 失败: %s", root_id, e)

    logger.info("[Evaluator] 评估完成: %d 条已评估, %d 条失败",
                stats["evaluated"], stats["errors"])

    # 评估后自动更新权重
    if stats["evaluated"] > 0:
        try:
            update_weights()
        except Exception as e:
            logger.warning("[Evaluator] 自动更新权重失败: %s", e)

    return stats
# ═══════════════════════════════════════════════════════════════
# Skill 权重更新（按单位时间收益率）
# ═══════════════════════════════════════════════════════════════

def _calc_skill_weight_from_trades(trades: List[Dict]) -> Dict[str, float]:
    """从历史交易记录计算 Skill 权重。

    核心指标：单位时间期望收益率
      return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days

    Returns:
        {"weight": float, "win_rate": float, "avg_pnl_pct": float,
         "avg_hold_days": float, "return_per_day": float, "sample_count": int}
    """
    if not trades:
        return {"weight": 1.0, "win_rate": 0, "avg_pnl_pct": 0,
                "avg_hold_days": 1, "return_per_day": 0, "sample_count": 0}

    correct_trades = [t for t in trades if t.get("correct") is True]
    wrong_trades = [t for t in trades if t.get("correct") is False]
    total = len(correct_trades) + len(wrong_trades)

    if total == 0:
        return {"weight": 1.0, "win_rate": 0, "avg_pnl_pct": 0,
                "avg_hold_days": 1, "return_per_day": 0, "sample_count": 0}

    win_rate = len(correct_trades) / total
    avg_win = (sum(t["pnl_pct"] for t in correct_trades) / len(correct_trades)) if correct_trades else 0
    avg_loss = abs(sum(t["pnl_pct"] for t in wrong_trades) / len(wrong_trades)) if wrong_trades else 0
    avg_hold = sum(t.get("hold_days", 3) for t in trades) / len(trades)
    avg_hold = max(avg_hold, 1)

    expected_return = win_rate * avg_win - (1 - win_rate) * avg_loss
    return_per_day = expected_return / avg_hold

    # 映射到权重（0.5~2.0）
    weight = max(0.5, min(2.0, 1.0 + return_per_day * 20))

    return {
        "weight": round(weight, 3),
        "win_rate": round(win_rate, 3),
        "avg_pnl_pct": round(expected_return, 2),
        "avg_hold_days": round(avg_hold, 1),
        "return_per_day": round(return_per_day, 4),
        "sample_count": total,
    }
def update_weights(days: int = 90) -> Dict[str, Any]:
    """更新 qd_agent_weights 表（统一 skill + factor）。

    一次扫描 qd_traces WHERE layer='skill'，同时产出：
      1. skill 层权重（按单位时间收益率）
      2. factor 层权重（带时间衰减的准确率）

    自动同步 registry：新 Skill → INSERT 工厂默认值。
    """
    from app.utils.db import get_db_connection

    stats = {"synced": 0, "skill_updated": 0, "factor_updated": 0, "factor_cleaned": 0}
    since = date.today() - timedelta(days=days)
    today = date.today()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # ① 同步 semantics：新 Skill 自动 INSERT 工厂默认值
            cur.execute("SELECT name FROM qd_agent_weights WHERE layer = 'skill'")
            existing_skills = {row['name'] for row in cur.fetchall()}

            from llm.qd_skills import QDSkillAdapter
            adapter = QDSkillAdapter()
            for skill_info in adapter.list_skills():
                name = skill_info["name"]
                if name not in existing_skills:
                    info = adapter.get(name)
                    default_w = info.default_weight if info and info.default_weight else 1.0
                    cur.execute("""
                        INSERT INTO qd_agent_weights (layer, name, skill_name, weight, sample_count)
                        VALUES ('skill', %s, NULL, %s, 0)
                        ON CONFLICT (layer, name, skill_name) DO NOTHING
                    """, (name, default_w))
                    stats["synced"] += 1
                    logger.info("[Evaluator] 新 Skill 注册: %s (weight=%.2f)", name, default_w)

            # ② 一次扫描 qd_traces，同时聚合 skill 和 factor 数据
            cur.execute("""
                SELECT t.name as skill_name, t.factors, t.pnl_pct,
                       t.hold_days, t.correct, r.exec_date
                FROM qd_traces t
                JOIN qd_traces r ON r.id = t.root_id
                WHERE t.layer = 'skill'
                  AND t.status = 'ok'
                  AND t.correct IS NOT NULL
                  AND r.exec_date >= %s
            """, (since,))

            skill_trades: Dict[str, List[Dict]] = {}
            factor_stats: Dict[tuple, Dict[str, float]] = {}

            for row in cur.fetchall():
                skill_name = row['skill_name']
                correct = row['correct']
                exec_date = row['exec_date']

                # 聚合 skill 交易数据
                if skill_name not in skill_trades:
                    skill_trades[skill_name] = []
                skill_trades[skill_name].append({
                    "pnl_pct": row['pnl_pct'],
                    "hold_days": row['hold_days'] or 3,
                    "correct": correct,
                    "exec_date": exec_date,
                })

                # 聚合 factor 数据（带时间衰减）
                factors_json = row['factors']
                try:
                    factors_raw = json.loads(factors_json) if isinstance(factors_json, str) else (factors_json or [])
                except (json.JSONDecodeError, TypeError):
                    factors_raw = []

                days_ago = (today - exec_date).days
                for factor in factors_raw:
                    if not isinstance(factor, dict):
                        continue
                    fname = factor.get("name", "")
                    if not fname:
                        continue

                    key = (skill_name, fname)
                    hl = _get_factor_half_life(fname)
                    decay_weight = math.pow(0.5, days_ago / max(hl, 1))

                    if key not in factor_stats:
                        factor_stats[key] = {
                            "weighted_correct": 0.0, "weighted_total": 0.0,
                            "raw_total": 0, "half_life": hl,
                        }
                    factor_stats[key]["weighted_total"] += decay_weight
                    factor_stats[key]["raw_total"] += 1
                    if correct:
                        factor_stats[key]["weighted_correct"] += decay_weight

            # ③ UPSERT skill 权重
            for skill_name, trades in skill_trades.items():
                result = _calc_skill_weight_from_trades(trades)
                cur.execute("""
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate,
                         avg_pnl_pct, avg_hold_days, return_per_day,
                         sample_count, last_updated)
                    VALUES ('skill', %s, NULL, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (layer, name, skill_name)
                    DO UPDATE SET
                        weight = EXCLUDED.weight,
                        win_rate = EXCLUDED.win_rate,
                        avg_pnl_pct = EXCLUDED.avg_pnl_pct,
                        avg_hold_days = EXCLUDED.avg_hold_days,
                        return_per_day = EXCLUDED.return_per_day,
                        sample_count = EXCLUDED.sample_count,
                        last_updated = NOW()
                """, (
                    skill_name, result["weight"], result["win_rate"],
                    result["avg_pnl_pct"], result["avg_hold_days"],
                    result["return_per_day"], result["sample_count"],
                ))
                stats["skill_updated"] += 1

            # ④ UPSERT factor 权重
            active_factor_keys = set()
            for (skill_name, fname), s in factor_stats.items():
                total = s["weighted_total"]
                if total < 0.5:
                    continue

                active_factor_keys.add((skill_name, fname))
                accuracy = round(s["weighted_correct"] / total, 4)
                weight = max(0.5, min(2.0, accuracy * 2))

                cur.execute("""
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate,
                         sample_count, decay_half_life, last_updated)
                    VALUES ('factor', %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (layer, name, skill_name)
                    DO UPDATE SET
                        weight = EXCLUDED.weight,
                        win_rate = EXCLUDED.win_rate,
                        sample_count = EXCLUDED.sample_count,
                        decay_half_life = EXCLUDED.decay_half_life,
                        last_updated = NOW()
                """, (fname, skill_name, weight, accuracy,
                      int(s["raw_total"]), s["half_life"]))
                stats["factor_updated"] += 1

            # ⑤ 清理过期因子
            if active_factor_keys:
                placeholders = []
                params = []
                for sname, fname in active_factor_keys:
                    placeholders.append("NOT (skill_name = %s AND name = %s)")
                    params.extend([sname, fname])
                cur.execute(f"""
                    DELETE FROM qd_agent_weights
                    WHERE layer = 'factor'
                      AND sample_count > 0
                      AND ({' AND '.join(placeholders)})
                """, params)
                stats["factor_cleaned"] = cur.rowcount

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新权重失败: %s", e)

    logger.info("[Evaluator] 权重更新: 同步 %d, skill %d, factor %d, 清理 %d",
                stats["synced"], stats["skill_updated"], stats["factor_updated"], stats["factor_cleaned"])
    return stats
def update_skill_weights(days: int = 90) -> Dict[str, Any]:
    """兼容旧接口。"""
    return update_weights(days)
def update_factor_weights(days: int = 90) -> Dict[str, Any]:
    """兼容旧接口。"""
    return update_weights(days)
# ═══════════════════════════════════════════════════════════════
# 评估报告
# ═══════════════════════════════════════════════════════════════

def get_eval_report(days: int = 30) -> Dict[str, Any]:
    """获取评估报告。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {"overall": {}, "skills": [], "factors": []}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 总体准确率
            cur.execute("""
                SELECT COUNT(*) as total,
                       AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END) as acc
                FROM qd_traces
                WHERE parent_id IS NULL AND correct IS NOT NULL
                  AND exec_date >= %s
            """, (since,))

            row = cur.fetchone()
            if row and row['total']:
                result["overall"] = {
                    "total": row['total'],
                    "accuracy": round(row['acc'], 3) if row['acc'] else 0,
                }

            # 各 skill 准确率
            cur.execute("""
                SELECT t.name,
                       COUNT(*) as cnt,
                       AVG(CASE WHEN t.correct THEN 1.0 ELSE 0.0 END) as acc
                FROM qd_traces t
                JOIN qd_traces r ON r.id = t.root_id
                WHERE t.layer = 'skill' AND t.correct IS NOT NULL
                  AND r.exec_date >= %s
                GROUP BY t.name
                ORDER BY acc DESC
            """, (since,))

            result["skills"] = [
                {"name": row['name'], "count": row['cnt'],
                 "accuracy": round(row['acc'], 3) if row['acc'] else 0}
                for row in cur.fetchall()
            ]

            # 因子准确率
            cur.execute("""
                SELECT skill_name, name as factor_name, win_rate, weight, sample_count
                FROM qd_agent_weights
                WHERE layer = 'factor' AND sample_count >= 3
                ORDER BY win_rate DESC
                LIMIT 30
            """)

            result["factors"] = [
                {"skill": row['skill_name'], "factor": row['factor_name'],
                 "accuracy": round(row['win_rate'], 3) if row['win_rate'] else 0,
                 "weight": row['weight'], "samples": row['sample_count']}
                for row in cur.fetchall()
            ]

    except Exception as e:
        logger.error("[Evaluator] 获取评估报告失败: %s", e)

    return result
# ═══════════════════════════════════════════════════════════════
# 自动评估入口
# ═══════════════════════════════════════════════════════════════

def auto_evaluate(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """自动评估闭环：评估待验证决策 → 更新权重 → 生成报告。"""
    result = {}

    try:
        eval_stats = evaluate_pending(days_old=days_old, market=market)
        result["evaluation"] = eval_stats
    except Exception as e:
        logger.error("[AutoEval] 评估失败: %s", e)
        result["evaluation"] = {"evaluated": 0, "errors": 1, "error": str(e)}

    try:
        report = get_eval_report()
        result["report"] = report
    except Exception as e:
        logger.error("[AutoEval] 报告生成失败: %s", e)
        result["report"] = {"error": str(e)}

    return result
# ═══════════════════════════════════════════════════════════════
# 后台 Worker
# ═══════════════════════════════════════════════════════════════

_eval_thread = None
_eval_stop = None

_worker_health = {
    "last_run_at": None, "last_success_at": None,
    "last_error": None, "consecutive_failures": 0,
    "total_runs": 0, "total_successes": 0, "total_failures": 0,
}

def get_worker_health() -> Dict[str, Any]:
    h = dict(_worker_health)
    h["is_alive"] = _eval_thread is not None and _eval_thread.is_alive()
    h["next_run_in_seconds"] = _seconds_until_post_market() if h["is_alive"] else None
    h["schedule"] = "每天 15:30（盘后）"
    return h
def _seconds_until_post_market() -> float:
    """计算距离下一个盘后 15:30 的秒数。"""
    from datetime import datetime
    now = datetime.now()
    target = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now >= target:
        # 已过今天 15:30，算明天
        from datetime import timedelta
        target += timedelta(days=1)
    # 跳过周末
    while target.weekday() >= 5:
        from datetime import timedelta
        target += timedelta(days=1)
    return max(0, (target - now).total_seconds())
def start_eval_worker():
    """启动后台评估 worker（盘后 15:30 每天运行一次，T+N 验证）。"""
    global _eval_thread, _eval_stop

    import threading
    import time as _time

    if _eval_thread is not None and _eval_thread.is_alive():
        return

    _eval_stop = threading.Event()

    def _worker():
        while not _eval_stop.is_set():
            # 计算距离下一个盘后 15:30 的等待时间
            wait_secs = _seconds_until_post_market()
            logger.info("[EvalWorker] 下次盘后验证: %.0f 秒后 (%.1f 小时)",
                        wait_secs, wait_secs / 3600)
            if _eval_stop.wait(timeout=wait_secs):
                break  # 收到停止信号

            # 盘后执行 T+N 验证
            _worker_health["total_runs"] += 1
            _worker_health["last_run_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                result = auto_evaluate(days_old=1)
                evaluated = result.get("evaluation", {}).get("evaluated", 0)
                _worker_health["consecutive_failures"] = 0
                _worker_health["total_successes"] += 1
                _worker_health["last_success_at"] = _worker_health["last_run_at"]
                _worker_health["last_error"] = None
                logger.info("[EvalWorker] 盘后验证完成: %d 条已评估", evaluated)
            except Exception as e:
                _worker_health["consecutive_failures"] += 1
                _worker_health["total_failures"] += 1
                _worker_health["last_error"] = str(e)
                logger.warning("[EvalWorker] 盘后验证失败: %s", e)

    _eval_thread = threading.Thread(target=_worker, daemon=True, name="eval-worker")
    _eval_thread.start()
    logger.info("[EvalWorker] 盘后回溯评估 worker 已启动")
def stop_eval_worker():
    global _eval_stop
    if _eval_stop:
        _eval_stop.set()
