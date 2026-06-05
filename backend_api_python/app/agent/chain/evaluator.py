# -*- coding: utf-8 -*-
"""
Chain Evaluator — 链路评估器。

对已执行的链路记录进行事后评估：
1. 拉取实际涨跌数据
2. 对每个步骤的判断打分
3. 对链路整体打分
4. 聚合分析各步骤准确率
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
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
    """获取股票实际涨跌数据。

    Returns:
        {
            "direction_1d": "bullish"/"bearish"/"neutral",
            "direction_3d": ...,
            "direction_5d": ...,
            "return_1d": float,
            "return_3d": float,
            "return_5d": float,
        }
    """
    try:
        from app.data_sources.factory import DataSourceFactory

        ds = DataSourceFactory.get(market)
        # 获取 from_date 之后的 K 线数据
        klines = ds.get_kline(stock_code, timeframe="1D", days=10)
        if not klines or len(klines) < 2:
            return {}

        # 找到 from_date 对应的 K 线位置
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

        # 1日后
        if base_idx + 1 < len(klines):
            close_1d = klines[base_idx + 1]["c"]
            ret_1d = (close_1d - base_close) / base_close
            result["return_1d"] = round(ret_1d, 4)
            result["direction_1d"] = _classify_return(ret_1d)

        # 3日后
        if base_idx + 3 < len(klines):
            close_3d = klines[base_idx + 3]["c"]
            ret_3d = (close_3d - base_close) / base_close
            result["return_3d"] = round(ret_3d, 4)
            result["direction_3d"] = _classify_return(ret_3d)

        # 5日后
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
    """根据收益率判断方向。涨跌超过 0.5% 才算有方向。"""
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
    """评估所有待评估的链路执行记录。

    Args:
        days_old: 只评估至少 N 天前的记录（确保有后续数据）。
        market: 市场类型。

    Returns:
        {"evaluated": int, "errors": int, "details": [...]}
    """
    from app.utils.db import get_db_connection

    stats = {"evaluated": 0, "errors": 0, "details": []}
    cutoff_date = date.today() - timedelta(days=days_old)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 查询待评估记录
            cur.execute("""
                SELECT id, exec_date, stock_code, stock_name, chain_id
                FROM qd_chain_executions
                WHERE evaluated = FALSE AND exec_date <= %s
                ORDER BY exec_date ASC
                LIMIT 100
            """, (cutoff_date,))

            rows = cur.fetchall()

            for row in rows:
                exec_id, exec_date, stock_code, stock_name, chain_id = row
                try:
                    _evaluate_single(conn, exec_id, exec_date, stock_code, market)
                    stats["evaluated"] += 1
                    stats["details"].append({
                        "execution_id": exec_id,
                        "stock": stock_code,
                        "status": "ok",
                    })
                except Exception as e:
                    stats["errors"] += 1
                    stats["details"].append({
                        "execution_id": exec_id,
                        "stock": stock_code,
                        "status": "error",
                        "error": str(e),
                    })
                    logger.error("[Evaluator] 评估 execution %d 失败: %s", exec_id, e)

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 评估任务失败: %s", e)

    logger.info("[Evaluator] 评估完成: %d 条已评估, %d 条失败",
                stats["evaluated"], stats["errors"])
    return stats


def _evaluate_single(conn, execution_id: int, exec_date: date, stock_code: str, market: str):
    """评估单条链路执行记录。"""
    cur = conn.cursor()

    # 获取实际涨跌
    actuals = _get_actual_returns(stock_code, exec_date, market)
    if not actuals:
        # 没有后续数据，标记为已评估但无评分
        cur.execute(
            "UPDATE qd_chain_executions SET evaluated = TRUE, eval_timestamp = NOW() WHERE id = %s",
            (execution_id,)
        )
        return

    # 获取该执行的所有步骤
    cur.execute("""
        SELECT id, step_name, direction, confidence
        FROM qd_chain_steps
        WHERE execution_id = %s
        ORDER BY step_order
    """, (execution_id,))

    steps = cur.fetchall()

    for step_id, step_name, direction, confidence in steps:
        correct_1d = _is_correct(direction, actuals.get("direction_1d"))
        correct_3d = _is_correct(direction, actuals.get("direction_3d"))
        correct_5d = _is_correct(direction, actuals.get("direction_5d"))

        # 综合得分：正确的天数占比
        correct_count = sum(1 for c in [correct_1d, correct_3d, correct_5d] if c is True)
        total_count = sum(1 for c in [correct_1d, correct_3d, correct_5d] if c is not None)
        score = correct_count / total_count if total_count > 0 else 0

        cur.execute("""
            INSERT INTO qd_chain_step_scores
                (step_id, execution_id, actual_dir_1d, actual_dir_3d, actual_dir_5d,
                 actual_return_1d, actual_return_3d, actual_return_5d,
                 correct_1d, correct_3d, correct_5d, score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            step_id, execution_id,
            actuals.get("direction_1d", ""),
            actuals.get("direction_3d", ""),
            actuals.get("direction_5d", ""),
            actuals.get("return_1d", 0),
            actuals.get("return_3d", 0),
            actuals.get("return_5d", 0),
            correct_1d, correct_3d, correct_5d, score,
        ))

    # 标记已评估
    cur.execute(
        "UPDATE qd_chain_executions SET evaluated = TRUE, eval_timestamp = NOW() WHERE id = %s",
        (execution_id,)
    )


# ═══════════════════════════════════════════════════════════════
# 聚合分析
# ═══════════════════════════════════════════════════════════════

def get_chain_accuracy(chain_id: str, days: int = 30) -> Dict[str, Any]:
    """获取某条链路的准确率统计。

    Returns:
        {
            "chain_id": str,
            "total_evaluated": int,
            "overall_accuracy": {"1d": float, "3d": float, "5d": float},
            "step_accuracy": {
                "screening": {"1d": float, "3d": float, "5d": float, "count": int},
                ...
            }
        }
    """
    from app.utils.db import get_db_connection

    result = {
        "chain_id": chain_id,
        "total_evaluated": 0,
        "overall_accuracy": {},
        "step_accuracy": {},
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            since = date.today() - timedelta(days=days)

            # 总体准确率
            cur.execute("""
                SELECT
                    COUNT(*) as total,
                    AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_chain_step_scores s
                JOIN qd_chain_executions e ON s.execution_id = e.id
                WHERE e.chain_id = %s AND e.exec_date >= %s
                  AND s.correct_1d IS NOT NULL
            """, (chain_id, since))

            row = cur.fetchone()
            if row and row[0] > 0:
                result["total_evaluated"] = row[0]
                result["overall_accuracy"] = {
                    "1d": round(row[1], 3) if row[1] else 0,
                    "3d": round(row[2], 3) if row[2] else 0,
                    "5d": round(row[3], 3) if row[3] else 0,
                }

            # 各步骤准确率
            cur.execute("""
                SELECT
                    cs.step_name,
                    COUNT(*) as cnt,
                    AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_chain_step_scores s
                JOIN qd_chain_steps cs ON s.step_id = cs.id
                JOIN qd_chain_executions e ON s.execution_id = e.id
                WHERE e.chain_id = %s AND e.exec_date >= %s
                  AND s.correct_1d IS NOT NULL
                GROUP BY cs.step_name
            """, (chain_id, since))

            for row in cur.fetchall():
                step_name, cnt, acc_1d, acc_3d, acc_5d = row
                result["step_accuracy"][step_name] = {
                    "count": cnt,
                    "1d": round(acc_1d, 3) if acc_1d else 0,
                    "3d": round(acc_3d, 3) if acc_3d else 0,
                    "5d": round(acc_5d, 3) if acc_5d else 0,
                }

    except Exception as e:
        logger.error("[Evaluator] 获取链路准确率失败: %s", e)

    return result


def get_step_ranking(days: int = 30) -> List[Dict[str, Any]]:
    """获取所有步骤的准确率排名。

    Returns:
        [{"step_name": str, "chain_id": str, "accuracy_3d": float, "count": int}, ...]
    """
    from app.utils.db import get_db_connection

    results = []
    since = date.today() - timedelta(days=days)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    cs.step_name,
                    e.chain_id,
                    COUNT(*) as cnt,
                    AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_chain_step_scores s
                JOIN qd_chain_steps cs ON s.step_id = cs.id
                JOIN qd_chain_executions e ON s.execution_id = e.id
                WHERE e.exec_date >= %s AND s.correct_1d IS NOT NULL
                GROUP BY cs.step_name, e.chain_id
                ORDER BY acc_3d DESC
            """, (since,))

            for row in cur.fetchall():
                results.append({
                    "step_name": row[0],
                    "chain_id": row[1],
                    "count": row[2],
                    "accuracy_1d": round(row[3], 3) if row[3] else 0,
                    "accuracy_3d": round(row[4], 3) if row[4] else 0,
                    "accuracy_5d": round(row[5], 3) if row[5] else 0,
                })

    except Exception as e:
        logger.error("[Evaluator] 获取步骤排名失败: %s", e)

    return results
