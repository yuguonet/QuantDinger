# -*- coding: utf-8 -*-
"""
Chain Evaluator — 闭环评估器。

重新设计，重点：
1. 评估每个步骤的 score 与实际 1d/3d/5d 收益的关联
2. 评估每个 factor 的信息增益（有这个 factor vs 没有，准确率差异）
3. 工具级别的 useful_rate 关联到步骤正确性
4. 输出权重调整建议（供 executor 自动应用）
5. 支持 walk-forward 验证
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
            result["direction_1d"] = _classify_return(ret_1d)

        if base_idx + 3 < len(klines):
            close_3d = klines[base_idx + 3]["c"]
            ret_3d = (close_3d - base_close) / base_close
            result["return_3d"] = round(ret_3d, 4)
            result["direction_3d"] = _classify_return(ret_3d)

        if base_idx + 5 < len(klines):
            close_5d = klines[base_idx + 5]["c"]
            ret_5d = (close_5d - base_close) / base_close
            result["return_5d"] = round(ret_5d, 4)
            result["direction_5d"] = _classify_return(ret_5d)

        return result

    except Exception as e:
        logger.warning("[Evaluator] 获取实际涨跌失败 %s: %s", stock_code, e)
        return {}


def _classify_return(ret: float, threshold: float = 0.005) -> str:
    """根据收益率判断方向。"""
    if ret > threshold:
        return "bullish"
    elif ret < -threshold:
        return "bearish"
    return "neutral"


def _is_correct(predicted: str, actual: str) -> Optional[bool]:
    """判断预测是否正确。"""
    if not predicted or not actual:
        return None
    if actual == "neutral":
        return None  # 中性结果不计分
    return predicted == actual


# ═══════════════════════════════════════════════════════════════
# 评估执行
# ═══════════════════════════════════════════════════════════════

def evaluate_pending(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """评估所有待评估的决策记录。"""
    from app.utils.db import get_db_connection

    stats = {"evaluated": 0, "errors": 0, "details": []}
    cutoff_date = date.today() - timedelta(days=days_old)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 查找未评估的决策（没有对应 qd_decision_results 记录的）
            cur.execute("""
                SELECT d.id, d.exec_date, d.stock_code, d.stock_name, d.chain_id, d.action
                FROM qd_decisions d
                LEFT JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date <= %s AND r.id IS NULL
                ORDER BY d.exec_date ASC
                LIMIT 100
            """, (cutoff_date,))

            rows = cur.fetchall()

            for row in rows:
                decision_id, exec_date, stock_code, stock_name, chain_id, action = row
                try:
                    _evaluate_single(conn, decision_id, exec_date, stock_code, action, market)
                    stats["evaluated"] += 1
                    stats["details"].append({
                        "decision_id": decision_id,
                        "stock": stock_code,
                        "status": "ok",
                    })
                except Exception as e:
                    stats["errors"] += 1
                    stats["details"].append({
                        "decision_id": decision_id,
                        "stock": stock_code,
                        "status": "error",
                        "error": str(e),
                    })
                    logger.error("[Evaluator] 评估 decision %d 失败: %s", decision_id, e)

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 评估任务失败: %s", e)

    logger.info("[Evaluator] 评估完成: %d 条已评估, %d 条失败",
                stats["evaluated"], stats["errors"])

    if stats["evaluated"] > 0:
        try:
            update_factor_weights()
            update_tool_eval()
        except Exception as e:
            logger.warning("[Evaluator] 更新权重/工具评估失败: %s", e)

    return stats


def _evaluate_single(
    conn,
    decision_id: int,
    exec_date: date,
    stock_code: str,
    action: str,
    market: str,
):
    """评估单条决策记录。"""
    cur = conn.cursor()

    # 获取实际涨跌
    actuals = _get_actual_returns(stock_code, exec_date, market)
    if not actuals:
        # 没有后续数据，跳过（不插入 results 记录，下次重试）
        return

    # 判断决策是否正确
    action_to_direction = {"buy": "bullish", "sell": "bearish", "hold": "neutral", "skip": "neutral"}
    predicted_dir = action_to_direction.get(action, "neutral")

    correct_1d = _is_correct(predicted_dir, actuals.get("direction_1d"))
    correct_3d = _is_correct(predicted_dir, actuals.get("direction_3d"))
    correct_5d = _is_correct(predicted_dir, actuals.get("direction_5d"))

    cur.execute("""
        INSERT INTO qd_decision_results
            (decision_id, actual_return_1d, actual_return_3d, actual_return_5d,
             actual_direction_1d, actual_direction_3d, actual_direction_5d,
             correct_1d, correct_3d, correct_5d)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        decision_id,
        actuals.get("return_1d"), actuals.get("return_3d"), actuals.get("return_5d"),
        actuals.get("direction_1d"), actuals.get("direction_3d"), actuals.get("direction_5d"),
        correct_1d, correct_3d, correct_5d,
    ))

    # 更新步骤级别的因子准确率
    cur.execute("""
        SELECT id, step_name, factors, direction
        FROM qd_decision_steps
        WHERE decision_id = %s
    """, (decision_id,))

    for step_id, step_name, factors_json, step_dir in cur.fetchall():
        step_correct_3d = _is_correct(step_dir, actuals.get("direction_3d"))
        if step_correct_3d is None:
            continue

        # 解析因子
        try:
            factors = json.loads(factors_json) if factors_json else []
        except (json.JSONDecodeError, TypeError):
            factors = []

        for factor in factors:
            fname = factor.get("name", "")
            if not fname:
                continue
            # 更新因子权重表（在 update_factor_weights 中统一处理）


# ═══════════════════════════════════════════════════════════════
# 因子权重更新
# ═══════════════════════════════════════════════════════════════

def update_factor_weights(days: int = 60) -> Dict[str, Any]:
    """更新因子权重表。

    遍历所有已评估的决策，统计每个因子在不同链路中的准确率。
    """
    from app.utils.db import get_db_connection

    stats = {"updated": 0}
    since = date.today() - timedelta(days=days)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 获取所有已评估的决策及其步骤因子
            cur.execute("""
                SELECT d.chain_id, ds.step_name, ds.factors, ds.direction,
                       r.correct_1d, r.correct_3d, r.correct_5d
                FROM qd_decisions d
                JOIN qd_decision_steps ds ON ds.decision_id = d.id
                JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date >= %s
                  AND r.correct_1d IS NOT NULL
                  AND ds.status = 'ok'
            """, (since,))

            # 聚合: (chain_id, factor_name) → {correct_1d, correct_3d, correct_5d, total}
            factor_stats: Dict[Tuple[str, str], Dict[str, int]] = {}

            for chain_id, step_name, factors_json, direction, c1d, c3d, c5d in cur.fetchall():
                try:
                    factors = json.loads(factors_json) if factors_json else []
                except (json.JSONDecodeError, TypeError):
                    continue

                for factor in factors:
                    fname = factor.get("name", "")
                    if not fname:
                        continue

                    key = (chain_id, fname)
                    if key not in factor_stats:
                        factor_stats[key] = {"correct_1d": 0, "correct_3d": 0, "correct_5d": 0, "total": 0}

                    factor_stats[key]["total"] += 1
                    if c1d:
                        factor_stats[key]["correct_1d"] += 1
                    if c3d:
                        factor_stats[key]["correct_3d"] += 1
                    if c5d:
                        factor_stats[key]["correct_5d"] += 1

            # UPSERT
            for (chain_id, fname), s in factor_stats.items():
                total = s["total"]
                if total == 0:
                    continue

                cur.execute("""
                    INSERT INTO qd_factor_weights
                        (chain_id, factor_name, weight, accuracy_1d, accuracy_3d, accuracy_5d,
                         sample_count, last_updated)
                    VALUES (%s, %s, 1.0, %s, %s, %s, %s, NOW())
                    ON CONFLICT (chain_id, factor_name)
                    DO UPDATE SET
                        accuracy_1d = EXCLUDED.accuracy_1d,
                        accuracy_3d = EXCLUDED.accuracy_3d,
                        accuracy_5d = EXCLUDED.accuracy_5d,
                        sample_count = EXCLUDED.sample_count,
                        last_updated = NOW()
                """, (
                    chain_id, fname,
                    round(s["correct_1d"] / total, 4),
                    round(s["correct_3d"] / total, 4),
                    round(s["correct_5d"] / total, 4),
                    total,
                ))
                stats["updated"] += 1

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新因子权重失败: %s", e)

    logger.info("[Evaluator] 因子权重已更新: %d 个因子", stats["updated"])
    return stats


# ═══════════════════════════════════════════════════════════════
# 工具评估更新
# ═══════════════════════════════════════════════════════════════

def update_tool_eval(days: int = 60) -> Dict[str, Any]:
    """更新工具评估表。"""
    from app.utils.db import get_db_connection

    stats = {"updated": 0}
    since = date.today() - timedelta(days=days)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 获取所有已评估的决策及其工具调用
            cur.execute("""
                SELECT d.chain_id, ds.tools_called, ds.elapsed_ms,
                       r.correct_3d
                FROM qd_decisions d
                JOIN qd_decision_steps ds ON ds.decision_id = d.id
                JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date >= %s
                  AND r.correct_3d IS NOT NULL
            """, (since,))

            # 聚合: (chain_id, tool_name) → {calls, successes, useful_count, total_latency}
            tool_stats: Dict[Tuple[str, str], Dict[str, float]] = {}

            for chain_id, tools_json, elapsed_ms, correct_3d in cur.fetchall():
                try:
                    tools = json.loads(tools_json) if tools_json else []
                except (json.JSONDecodeError, TypeError):
                    continue

                for tool_name in tools:
                    if not tool_name:
                        continue
                    key = (chain_id, tool_name)
                    if key not in tool_stats:
                        tool_stats[key] = {"calls": 0, "successes": 0, "useful": 0, "latency": 0}

                    tool_stats[key]["calls"] += 1
                    tool_stats[key]["successes"] += 1  # tools_called 中的都是成功的
                    tool_stats[key]["latency"] += elapsed_ms or 0
                    if correct_3d:
                        tool_stats[key]["useful"] += 1

            # UPSERT
            for (chain_id, tool_name), s in tool_stats.items():
                calls = int(s["calls"])
                if calls == 0:
                    continue

                cur.execute("""
                    INSERT INTO qd_tool_eval
                        (tool_name, chain_id, calls, successes, useful_count,
                         avg_latency_ms, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (tool_name, chain_id)
                    DO UPDATE SET
                        calls = EXCLUDED.calls,
                        successes = EXCLUDED.successes,
                        useful_count = EXCLUDED.useful_count,
                        avg_latency_ms = EXCLUDED.avg_latency_ms,
                        last_updated = NOW()
                """, (
                    tool_name, chain_id, calls,
                    int(s["successes"]),
                    int(s["useful"]),
                    round(s["latency"] / calls, 1),
                ))
                stats["updated"] += 1

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新工具评估失败: %s", e)

    logger.info("[Evaluator] 工具评估已更新: %d 个工具", stats["updated"])
    return stats


# ═══════════════════════════════════════════════════════════════
# 权重调整建议
# ═══════════════════════════════════════════════════════════════

def get_step_weights(chain_id: str, days: int = 30) -> Dict[str, float]:
    """获取链路各步骤的历史准确率权重。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    weights = {}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    ds.step_name,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    COUNT(*) as cnt
                FROM qd_decision_steps ds
                JOIN qd_decisions d ON ds.decision_id = d.id
                JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.chain_id = %s AND d.exec_date >= %s
                  AND r.correct_3d IS NOT NULL
                  AND ds.status = 'ok'
                GROUP BY ds.step_name
                HAVING COUNT(*) >= 3
            """, (chain_id, since))

            for row in cur.fetchall():
                weights[row[0]] = round(row[1], 3) if row[1] else 0.5

    except Exception as e:
        logger.warning("[Evaluator] 获取步骤权重失败: %s", e)

    return weights


def get_weight_adjustments(chain_id: str, days: int = 30) -> Dict[str, Any]:
    """输出权重调整建议（供 executor 自动应用）。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {
        "chain_id": chain_id,
        "adjustments": [],      # [{factor_name, current_weight, suggested_weight, reason}]
        "tool_issues": [],      # [{tool_name, useful_rate, issue}]
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 因子权重调整
            cur.execute("""
                SELECT factor_name, weight, accuracy_3d, accuracy_5d, sample_count
                FROM qd_factor_weights
                WHERE chain_id = %s AND sample_count >= 5
                ORDER BY accuracy_3d DESC
            """, (chain_id,))

            for fname, weight, acc_3d, acc_5d, count in cur.fetchall():
                if acc_3d >= 0.65:
                    suggested = 1.3
                    reason = f"3日准确率 {acc_3d:.0%}，建议加权"
                elif acc_3d <= 0.35:
                    suggested = 0.5
                    reason = f"3日准确率仅 {acc_3d:.0%}，建议降权"
                else:
                    suggested = 1.0
                    reason = f"3日准确率 {acc_3d:.0%}，维持默认"

                if abs(suggested - weight) > 0.1:
                    result["adjustments"].append({
                        "factor_name": fname,
                        "current_weight": weight,
                        "suggested_weight": suggested,
                        "reason": reason,
                    })

            # 工具问题
            cur.execute("""
                SELECT tool_name, calls, successes, useful_count, avg_latency_ms
                FROM qd_tool_eval
                WHERE chain_id = %s AND calls >= 5
            """, (chain_id,))

            for tool_name, calls, successes, useful, latency in cur.fetchall():
                success_rate = successes / calls if calls > 0 else 0
                useful_rate = useful / calls if calls > 0 else 0

                if success_rate < 0.7:
                    result["tool_issues"].append({
                        "tool_name": tool_name,
                        "useful_rate": round(useful_rate, 3),
                        "issue": f"成功率仅 {success_rate:.0%}（{successes}/{calls}），工具不稳定",
                    })
                elif useful_rate < 0.3:
                    result["tool_issues"].append({
                        "tool_name": tool_name,
                        "useful_rate": round(useful_rate, 3),
                        "issue": f"有用率仅 {useful_rate:.0%}，工具返回数据对判断几乎无帮助",
                    })

    except Exception as e:
        logger.error("[Evaluator] 获取权重调整建议失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# Walk-forward 验证
# ═══════════════════════════════════════════════════════════════

def walk_forward_validate(
    chain_id: str,
    train_days: int = 60,
    test_days: int = 10,
) -> Dict[str, Any]:
    """Walk-forward 验证。

    用 train_days 的数据训练权重，用 test_days 的数据验证效果。
    """
    from app.utils.db import get_db_connection

    result = {
        "chain_id": chain_id,
        "train_days": train_days,
        "test_days": test_days,
        "train_accuracy": {},
        "test_accuracy": {},
        "improvement": {},
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            train_start = date.today() - timedelta(days=train_days + test_days)
            train_end = date.today() - timedelta(days=test_days)
            test_start = train_end

            # 训练集准确率
            cur.execute("""
                SELECT
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d,
                    COUNT(*) as cnt
                FROM qd_decisions d
                JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.chain_id = %s
                  AND d.exec_date >= %s AND d.exec_date < %s
                  AND r.correct_1d IS NOT NULL
            """, (chain_id, train_start, train_end))

            row = cur.fetchone()
            if row and row[3] > 0:
                result["train_accuracy"] = {
                    "1d": round(row[0], 3) if row[0] else 0,
                    "3d": round(row[1], 3) if row[1] else 0,
                    "5d": round(row[2], 3) if row[2] else 0,
                    "count": row[3],
                }

            # 测试集准确率
            cur.execute("""
                SELECT
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d,
                    COUNT(*) as cnt
                FROM qd_decisions d
                JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.chain_id = %s
                  AND d.exec_date >= %s
                  AND r.correct_1d IS NOT NULL
            """, (chain_id, test_start))

            row = cur.fetchone()
            if row and row[3] > 0:
                result["test_accuracy"] = {
                    "1d": round(row[0], 3) if row[0] else 0,
                    "3d": round(row[1], 3) if row[1] else 0,
                    "5d": round(row[2], 3) if row[2] else 0,
                    "count": row[3],
                }

            # 计算改进
            for period in ("1d", "3d", "5d"):
                train_acc = result["train_accuracy"].get(period, 0)
                test_acc = result["test_accuracy"].get(period, 0)
                result["improvement"][period] = round(test_acc - train_acc, 3)

    except Exception as e:
        logger.error("[Evaluator] Walk-forward 验证失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# 综合评估报告
# ═══════════════════════════════════════════════════════════════

def get_eval_report(chain_id: str = None, days: int = 30) -> Dict[str, Any]:
    """获取评估报告。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {"overall": {}, "steps": [], "factors": []}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            chain_filter = "AND d.chain_id = %s" if chain_id else ""
            params_base = [since] + ([chain_id] if chain_id else [])

            # 总体准确率
            cur.execute(f"""
                SELECT
                    d.chain_id,
                    COUNT(*) as total,
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_decisions d
                JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date >= %s {chain_filter}
                  AND r.correct_1d IS NOT NULL
                GROUP BY d.chain_id
            """, params_base)

            row = cur.fetchone()
            if row:
                result["overall"] = {
                    "chain_id": row[0],
                    "total": row[1],
                    "accuracy": {
                        "1d": round(row[2], 3) if row[2] else 0,
                        "3d": round(row[3], 3) if row[3] else 0,
                        "5d": round(row[4], 3) if row[4] else 0,
                    },
                }

            # 各步骤准确率
            cur.execute(f"""
                SELECT
                    ds.step_name,
                    COUNT(*) as cnt,
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_decision_steps ds
                JOIN qd_decisions d ON ds.decision_id = d.id
                JOIN qd_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date >= %s {chain_filter}
                  AND r.correct_1d IS NOT NULL AND ds.status = 'ok'
                GROUP BY ds.step_name
                ORDER BY acc_3d DESC
            """, params_base)

            result["steps"] = [
                {
                    "step_name": row[0],
                    "count": row[1],
                    "accuracy": {
                        "1d": round(row[2], 3) if row[2] else 0,
                        "3d": round(row[3], 3) if row[3] else 0,
                        "5d": round(row[4], 3) if row[4] else 0,
                    },
                }
                for row in cur.fetchall()
            ]

            # 因子准确率
            cur.execute(f"""
                SELECT
                    fw.factor_name,
                    fw.accuracy_1d,
                    fw.accuracy_3d,
                    fw.accuracy_5d,
                    fw.sample_count
                FROM qd_factor_weights fw
                WHERE fw.chain_id = %s AND fw.sample_count >= 3
                ORDER BY fw.accuracy_3d DESC
            """, [chain_id] if chain_id else ["evaluate+stock"])

            result["factors"] = [
                {
                    "factor_name": row[0],
                    "accuracy_1d": round(row[1], 3) if row[1] else 0,
                    "accuracy_3d": round(row[2], 3) if row[2] else 0,
                    "accuracy_5d": round(row[3], 3) if row[3] else 0,
                    "sample_count": row[4],
                }
                for row in cur.fetchall()
            ]

    except Exception as e:
        logger.error("[Evaluator] 获取评估报告失败: %s", e)

    return result
