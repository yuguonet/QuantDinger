# -*- coding: utf-8 -*-
"""
Evaluator — 回溯评估引擎。

基于 qd_evaluations 单表 + qd_factor_weights 因子权重表。

核心流程（每日盘后自动运行）：
  evaluate_pending()      → 获取 T+1/3/5 实际涨跌，写回 qd_evaluations
  update_factor_weights() → 带时间衰减聚合因子准确率，写入 qd_factor_weights
  auto_evaluate()         → 自动闭环（评估→权重→报告）

三层评估原则：
  Chain — 预测方向 vs 实际方向 → 决策准确率
  Skill — 每个 skill 独立方向 vs 实际方向 → 因子准确率
  Tool  — 数据偏差检测（TODO，回溯时才有意义）

与旧版区别：
  - 旧版：5 张表（decisions/steps/results/factor_weights/tool_eval）
  - 新版：1 棵树（qd_evaluations）+ 1 张因子权重表（qd_factor_weights）
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.agent.chain.schema import (
    DIRECTION_THRESHOLD, Direction, classify_return, is_direction_correct,
)
from app.agent.chain import store

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 实际涨跌数据获取
# ═══════════════════════════════════════════════════════════════

def _get_actual_returns(
    stock_code: str,
    from_date: date,
    market: str = "CNStock",
) -> Dict[str, Any]:
    """获取股票实际涨跌数据。"""
    try:
        from app.data_sources.factory import DataSourceFactory

        ds = DataSourceFactory.get(market)
        klines = ds.get_kline(stock_code, timeframe="1D", days=10)
        if not klines or len(klines) < 2:
            return {}

        from_str = from_date.strftime("%Y-%m-%d")
        base_idx = None
        for i, k in enumerate(klines):
            k_date = k.get("t", "")[:10]
            if k_date >= from_str:
                base_idx = i
                break

        if base_idx is None:
            return {}

        base_close = klines[base_idx]["c"]
        result = {}

        if base_idx + 1 < len(klines):
            close_1d = klines[base_idx + 1]["c"]
            ret_1d = (close_1d - base_close) / base_close
            result["return_1d"] = round(ret_1d, 4)
            result["direction_1d"] = classify_return(ret_1d)

        if base_idx + 3 < len(klines):
            close_3d = klines[base_idx + 3]["c"]
            ret_3d = (close_3d - base_close) / base_close
            result["return_3d"] = round(ret_3d, 4)
            result["direction_3d"] = classify_return(ret_3d)

        if base_idx + 5 < len(klines):
            close_5d = klines[base_idx + 5]["c"]
            ret_5d = (close_5d - base_close) / base_close
            result["return_5d"] = round(ret_5d, 4)
            result["direction_5d"] = classify_return(ret_5d)

        return result

    except Exception as e:
        logger.warning("[Evaluator] 获取实际涨跌失败 %s: %s", stock_code, e)
        return {}


# ═══════════════════════════════════════════════════════════════
# 评估执行
# ═══════════════════════════════════════════════════════════════

def evaluate_pending(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """评估所有待验证的决策记录。

    查找 qd_evaluations 中 correct_3d IS NULL 的根节点，
    获取 T+1/3/5 实际涨跌，写回验证结果。

    三层验证：
      1. 根节点（chain）— action vs 实际方向 → correct_3d
      2. 子节点（skill）— direction vs 实际方向 → correct_3d + calibration

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

        try:
            actuals = _get_actual_returns(stock_code, exec_date, market)
            if not actuals:
                continue

            # 方向映射
            action_to_dir = {
                "buy": Direction.BULLISH.value,
                "sell": Direction.BEARISH.value,
                "hold": Direction.NEUTRAL.value,
                "skip": Direction.NEUTRAL.value,
            }
            predicted_dir = action_to_dir.get(action, Direction.NEUTRAL.value)
            actual_dir_3d = actuals.get("direction_3d", "")
            verdict = is_direction_correct(predicted_dir, actual_dir_3d)

            # 根节点：neutral 预测 → correct_3d = NULL
            if verdict == "neutral":
                correct_3d = None
            else:
                correct_3d = verdict == "correct"

            # 写入根节点验证结果
            store.update_verify_results(
                root_id=root_id,
                actual_return_1d=actuals.get("return_1d"),
                actual_return_3d=actuals.get("return_3d"),
                actual_return_5d=actuals.get("return_5d"),
                actual_direction_3d=actual_dir_3d,
                correct_3d=correct_3d,
            )

            # 写入 skill 子节点验证结果
            store.update_skill_verify(root_id, actual_dir_3d)

            stats["evaluated"] += 1
            stats["details"].append({
                "root_id": root_id, "stock": stock_code,
                "action": action, "actual_dir": actual_dir_3d,
                "correct": correct_3d,
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

    # 评估后自动更新因子权重
    if stats["evaluated"] > 0:
        try:
            update_factor_weights()
        except Exception as e:
            logger.warning("[Evaluator] 自动更新因子权重失败: %s", e)

    return stats


# ═══════════════════════════════════════════════════════════════
# 因子权重更新
# ═══════════════════════════════════════════════════════════════

# 因子名关键词 → 半衰期（天）
_FACTOR_HALF_LIFE_RULES = [
    (["政策", "policy", "监管", "新规"], 7),
    (["新闻", "news", "公告", "消息", "舆情"], 7),
    (["解禁", "lockup", "减持", "增持"], 7),
    (["游资", "hot_money", "龙虎榜", "主力"], 14),
    (["资金", "fund_flow", "北向", "融资"], 14),
    (["概念", "concept", "题材", "板块", "sector"], 21),
    (["MACD", "macd", "DIF", "DEA", "金叉", "死叉"], 30),
    (["RSI", "rsi", "超买", "超卖"], 30),
    (["KDJ", "kdj", "J值"], 30),
    (["BOLL", "boll", "布林"], 30),
    (["均线", "MA", "ma", "多头排列", "空头排列"], 30),
    (["形态", "pattern", "突破", "反转", "K线"], 30),
    (["动量", "momentum", "趋势", "trend"], 30),
    (["量", "volume", "量比", "换手", "放量", "缩量"], 60),
    (["筹码", "chip", "持仓", "成本"], 45),
]

_DEFAULT_HALF_LIFE = 30


def _get_factor_half_life(factor_name: str) -> int:
    """根据因子名推断衰减半衰期。"""
    name_lower = factor_name.lower()
    for keywords, hl in _FACTOR_HALF_LIFE_RULES:
        for kw in keywords:
            if kw.lower() in name_lower:
                return hl
    return _DEFAULT_HALF_LIFE


def update_factor_weights(days: int = 60, decay_half_life_days: int = 30) -> Dict[str, Any]:
    """更新因子权重表。

    从 qd_evaluations 中读取已验证的 skill 节点及其 factors，
    按 (chain_id, factor_name) 聚合带时间衰减的准确率。

    核心改动（vs 旧版）：
      - 用 qd_evaluations.layer='skill' 的 correct_3d 替代旧的 qd_agent_decision_steps
      - 每个 skill 节点的 factors JSONB 中提取因子名
      - 校准因子微调：score 越偏离50，权重更新幅度越大（±5%）
    """
    from app.utils.db import get_db_connection

    stats = {"updated": 0}
    since = date.today() - timedelta(days=days)
    today = date.today()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 获取已验证的 skill 节点（跳过 neutral 预测，correct_3d IS NULL）
            cur.execute("""
                SELECT e.root_id, e.name, e.factors, e.direction,
                       e.correct_3d, e.calibration, r.exec_date, r.name as chain_id
                FROM qd_evaluations e
                JOIN qd_evaluations r ON r.id = e.root_id
                WHERE e.layer = 'skill'
                  AND e.status = 'ok'
                  AND e.correct_3d IS NOT NULL
                  AND r.exec_date >= %s
            """, (since,))

            # 聚合: (chain_id, factor_name) → {weighted_correct, weighted_total}
            factor_stats: Dict[Tuple[str, str], Dict[str, float]] = {}

            for root_id, skill_name, factors_json, direction, step_correct, cal_factor, exec_date, chain_id in cur.fetchall():
                try:
                    factors_raw = json.loads(factors_json) if isinstance(factors_json, str) else (factors_json or [])
                except (json.JSONDecodeError, TypeError):
                    factors_raw = []

                days_ago = (today - exec_date).days
                cal = cal_factor or 1.0

                for factor in factors_raw:
                    if isinstance(factor, dict):
                        fname = factor.get("name", "")
                    else:
                        continue
                    if not fname:
                        continue

                    key = (chain_id, fname)
                    hl = _get_factor_half_life(fname)
                    decay_weight = math.pow(0.5, days_ago / max(hl, 1))

                    if key not in factor_stats:
                        factor_stats[key] = {
                            "weighted_correct": 0.0, "weighted_total": 0.0,
                            "raw_total": 0, "half_life": hl,
                        }

                    effective_weight = decay_weight * cal
                    factor_stats[key]["weighted_total"] += effective_weight
                    factor_stats[key]["raw_total"] += 1
                    if step_correct:
                        factor_stats[key]["weighted_correct"] += effective_weight

            # UPSERT 到 qd_factor_weights
            for (chain_id, fname), s in factor_stats.items():
                total = s["weighted_total"]
                if total < 0.5:
                    continue

                accuracy = round(s["weighted_correct"] / total, 4)

                cur.execute("""
                    INSERT INTO qd_factor_weights
                        (chain_id, skill_name, factor_name, weight, accuracy_3d,
                         sample_count, decay_half_life, last_updated)
                    VALUES (%s, %s, %s, 1.0, %s, %s, %s, NOW())
                    ON CONFLICT (chain_id, skill_name, factor_name)
                    DO UPDATE SET
                        accuracy_3d = EXCLUDED.accuracy_3d,
                        sample_count = EXCLUDED.sample_count,
                        decay_half_life = EXCLUDED.decay_half_life,
                        last_updated = NOW()
                """, (chain_id, skill_name if 'skill_name' in dir() else "", fname,
                      accuracy, int(s["raw_total"]), s["half_life"]))
                stats["updated"] += 1

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新因子权重失败: %s", e)

    logger.info("[Evaluator] 因子权重已更新: %d 个因子", stats["updated"])
    return stats


# ═══════════════════════════════════════════════════════════════
# 评估报告
# ═══════════════════════════════════════════════════════════════

def get_eval_report(chain_id: str = None, days: int = 30) -> Dict[str, Any]:
    """获取评估报告。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {"overall": {}, "skills": [], "factors": []}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            chain_filter = "AND r.name = %s" if chain_id else ""
            params = [since] + ([chain_id] if chain_id else [])

            # 总体准确率
            cur.execute(f"""
                SELECT r.name,
                       COUNT(*) as total,
                       AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d
                FROM qd_evaluations r
                WHERE r.parent_id IS NULL AND r.exec_date >= %s
                  AND r.correct_3d IS NOT NULL {chain_filter}
                GROUP BY r.name
            """, params)

            row = cur.fetchone()
            if row:
                result["overall"] = {
                    "chain_id": row[0], "total": row[1],
                    "accuracy_3d": round(row[2], 3) if row[2] else 0,
                }

            # 各 skill 准确率
            cur.execute(f"""
                SELECT e.name,
                       COUNT(*) as cnt,
                       AVG(CASE WHEN e.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d
                FROM qd_evaluations e
                JOIN qd_evaluations r ON r.id = e.root_id
                WHERE e.layer = 'skill' AND e.correct_3d IS NOT NULL
                  AND r.exec_date >= %s {chain_filter}
                GROUP BY e.name
                ORDER BY acc_3d DESC
            """, params)

            result["skills"] = [
                {"name": row[0], "count": row[1],
                 "accuracy_3d": round(row[2], 3) if row[2] else 0}
                for row in cur.fetchall()
            ]

            # 因子准确率
            if chain_id:
                cur.execute("""
                    SELECT factor_name, accuracy_3d, sample_count
                    FROM qd_factor_weights
                    WHERE chain_id = %s AND sample_count >= 3
                    ORDER BY accuracy_3d DESC
                """, (chain_id,))

                result["factors"] = [
                    {"factor_name": row[0], "accuracy_3d": round(row[1], 3) if row[1] else 0,
                     "sample_count": row[2]}
                    for row in cur.fetchall()
                ]

    except Exception as e:
        logger.error("[Evaluator] 获取评估报告失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# 自动评估入口
# ═══════════════════════════════════════════════════════════════

def auto_evaluate(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """自动评估闭环：评估待验证决策 → 更新因子权重 → 生成报告。"""
    result = {}

    # Step 1: 评估
    try:
        eval_stats = evaluate_pending(days_old=days_old, market=market)
        result["evaluation"] = eval_stats
    except Exception as e:
        logger.error("[AutoEval] 评估失败: %s", e)
        result["evaluation"] = {"evaluated": 0, "errors": 1, "error": str(e)}

    # Step 2: 报告
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

_BASE_INTERVAL = 4 * 3600
_MAX_INTERVAL = 24 * 3600


def get_worker_health() -> Dict[str, Any]:
    h = dict(_worker_health)
    h["is_alive"] = _eval_thread is not None and _eval_thread.is_alive()
    failures = h["consecutive_failures"]
    h["current_interval"] = min(_BASE_INTERVAL * (2 ** min(failures, 3)), _MAX_INTERVAL)
    return h


def start_eval_worker():
    """启动后台评估 worker（每4小时运行，指数退避）。"""
    global _eval_thread, _eval_stop

    import threading
    import time as _time

    if _eval_thread is not None and _eval_thread.is_alive():
        return

    _eval_stop = threading.Event()

    def _worker():
        _time.sleep(60)
        while not _eval_stop.is_set():
            _worker_health["total_runs"] += 1
            _worker_health["last_run_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                result = auto_evaluate(days_old=1)
                _worker_health["consecutive_failures"] = 0
                _worker_health["total_successes"] += 1
                _worker_health["last_success_at"] = _worker_health["last_run_at"]
                _worker_health["last_error"] = None
            except Exception as e:
                _worker_health["consecutive_failures"] += 1
                _worker_health["total_failures"] += 1
                _worker_health["last_error"] = str(e)

            failures = _worker_health["consecutive_failures"]
            interval = min(_BASE_INTERVAL * (2 ** min(failures, 3)), _MAX_INTERVAL)
            _eval_stop.wait(timeout=interval)

    _eval_thread = threading.Thread(target=_worker, daemon=True, name="eval-worker")
    _eval_thread.start()
    logger.info("[EvalWorker] 后台评估 worker 已启动")


def stop_eval_worker():
    global _eval_stop
    if _eval_stop:
        _eval_stop.set()
