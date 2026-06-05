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

    # 评估完后更新汇总表
    if stats["evaluated"] > 0:
        try:
            update_eval_summary()
        except Exception as e:
            logger.warning("[Evaluator] 更新评估汇总失败: %s", e)

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


# ═══════════════════════════════════════════════════════════════
# 评估汇总（写入 qd_chain_eval_summary）
# ═══════════════════════════════════════════════════════════════

def update_eval_summary(eval_date: date = None) -> Dict[str, Any]:
    """聚合评估结果，写入 qd_chain_eval_summary。

    在 evaluate_pending() 之后调用，按 chain_id 聚合当天的评分。

    Args:
        eval_date: 评估日期，默认今天。

    Returns:
        {"updated": int, "chains": [chain_id, ...]}
    """
    from app.utils.db import get_db_connection

    if eval_date is None:
        eval_date = date.today()

    stats = {"updated": 0, "chains": []}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 查找当天有评分数据的所有 chain_id
            cur.execute("""
                SELECT DISTINCT e.chain_id
                FROM qd_chain_executions e
                WHERE e.exec_date = %s AND e.evaluated = TRUE
            """, (eval_date,))

            chain_ids = [row[0] for row in cur.fetchall()]

            for chain_id in chain_ids:
                # 整体准确率
                cur.execute("""
                    SELECT
                        COUNT(DISTINCT e.id) as total,
                        COUNT(DISTINCT CASE WHEN s.id IS NOT NULL THEN e.id END) as evaluated,
                        AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                        AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                        AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                    FROM qd_chain_executions e
                    LEFT JOIN qd_chain_step_scores s ON s.execution_id = e.id
                    WHERE e.exec_date = %s AND e.chain_id = %s AND e.evaluated = TRUE
                      AND s.correct_1d IS NOT NULL
                """, (eval_date, chain_id))

                row = cur.fetchone()
                if not row or row[0] == 0:
                    continue

                total_exec = row[0]
                evaluated_cnt = row[1]
                acc_1d = round(row[2], 4) if row[2] else 0
                acc_3d = round(row[3], 4) if row[3] else 0
                acc_5d = round(row[4], 4) if row[4] else 0

                # 各步骤准确率
                cur.execute("""
                    SELECT
                        cs.step_name,
                        AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                        AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                        AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d,
                        COUNT(*) as cnt
                    FROM qd_chain_step_scores s
                    JOIN qd_chain_steps cs ON s.step_id = cs.id
                    JOIN qd_chain_executions e ON s.execution_id = e.id
                    WHERE e.exec_date = %s AND e.chain_id = %s
                      AND s.correct_1d IS NOT NULL
                    GROUP BY cs.step_name
                """, (eval_date, chain_id))

                step_acc = {}
                for sr in cur.fetchall():
                    step_acc[sr[0]] = {
                        "1d": round(sr[1], 3) if sr[1] else 0,
                        "3d": round(sr[2], 3) if sr[2] else 0,
                        "5d": round(sr[3], 3) if sr[3] else 0,
                        "count": sr[4],
                    }

                # 技能评估：按 agent_name 聚合（跨步骤）
                cur.execute("""
                    SELECT
                        cs.agent_name,
                        AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                        AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                        AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d,
                        COUNT(*) as cnt
                    FROM qd_chain_step_scores s
                    JOIN qd_chain_steps cs ON s.step_id = cs.id
                    JOIN qd_chain_executions e ON s.execution_id = e.id
                    WHERE e.exec_date = %s AND e.chain_id = %s
                      AND s.correct_1d IS NOT NULL AND cs.agent_name != ''
                    GROUP BY cs.agent_name
                """, (eval_date, chain_id))

                skill_acc = {}
                for sr in cur.fetchall():
                    skill_acc[sr[0]] = {
                        "1d": round(sr[1], 3) if sr[1] else 0,
                        "3d": round(sr[2], 3) if sr[2] else 0,
                        "5d": round(sr[3], 3) if sr[3] else 0,
                        "count": sr[4],
                    }

                # 工具评估：从 tools_detail 聚合，关联步骤正确性
                tool_stats = {}
                try:
                    cur.execute("""
                        SELECT
                            cs.tools_detail,
                            s.correct_3d
                        FROM qd_chain_step_scores s
                        JOIN qd_chain_steps cs ON s.step_id = cs.id
                        JOIN qd_chain_executions e ON s.execution_id = e.id
                        WHERE e.exec_date = %s AND e.chain_id = %s
                          AND cs.tools_detail != '' AND s.correct_3d IS NOT NULL
                    """, (eval_date, chain_id))

                    for tools_json, correct_3d in cur.fetchall():
                        try:
                            tools = json.loads(tools_json) if tools_json else []
                        except (json.JSONDecodeError, TypeError):
                            continue
                        for t in tools:
                            if not isinstance(t, dict):
                                continue
                            name = t.get("name", "")
                            if not name:
                                continue
                            if name not in tool_stats:
                                tool_stats[name] = {"calls": 0, "ok": 0, "useful": 0}
                            tool_stats[name]["calls"] += 1
                            if t.get("ok", True):
                                tool_stats[name]["ok"] += 1
                            if correct_3d:
                                tool_stats[name]["useful"] += 1

                    # 计算 usefulness 率
                    for v in tool_stats.values():
                        v["useful_rate"] = round(v["useful"] / v["calls"], 3) if v["calls"] > 0 else 0
                except Exception as e:
                    logger.debug("[Evaluator] 工具评估查询跳过（可能 tools_detail 列不存在）: %s", e)

                # UPSERT 汇总
                cur.execute("""
                    INSERT INTO qd_chain_eval_summary
                        (eval_date, chain_id, total_executions, evaluated_count,
                         overall_accuracy_1d, overall_accuracy_3d, overall_accuracy_5d,
                         step_accuracies, skill_accuracies, tool_stats)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (eval_date, chain_id)
                    DO UPDATE SET
                        total_executions = EXCLUDED.total_executions,
                        evaluated_count = EXCLUDED.evaluated_count,
                        overall_accuracy_1d = EXCLUDED.overall_accuracy_1d,
                        overall_accuracy_3d = EXCLUDED.overall_accuracy_3d,
                        overall_accuracy_5d = EXCLUDED.overall_accuracy_5d,
                        step_accuracies = EXCLUDED.step_accuracies,
                        skill_accuracies = EXCLUDED.skill_accuracies,
                        tool_stats = EXCLUDED.tool_stats
                """, (
                    eval_date, chain_id, total_exec, evaluated_cnt,
                    acc_1d, acc_3d, acc_5d,
                    json.dumps(step_acc, ensure_ascii=False),
                    json.dumps(skill_acc, ensure_ascii=False),
                    json.dumps(tool_stats, ensure_ascii=False),
                ))

                stats["updated"] += 1
                stats["chains"].append(chain_id)

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新评估汇总失败: %s", e)

    logger.info("[Evaluator] 评估汇总已更新: %d 条链路", stats["updated"])
    return stats


def get_step_weights(chain_id: str, days: int = 30) -> Dict[str, float]:
    """获取链路各步骤的历史准确率权重，供 executor 加权投票用。

    Returns:
        {"policy": 0.72, "technical": 0.65, ...}
        值为 3日方向准确率，无数据的步骤不返回（executor 会用默认权重 0.5）。
    """
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    weights = {}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    cs.step_name,
                    AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    COUNT(*) as cnt
                FROM qd_chain_step_scores s
                JOIN qd_chain_steps cs ON s.step_id = cs.id
                JOIN qd_chain_executions e ON s.execution_id = e.id
                WHERE e.chain_id = %s AND e.exec_date >= %s
                  AND s.correct_3d IS NOT NULL
                GROUP BY cs.step_name
                HAVING COUNT(*) >= 3
            """, (chain_id, since))

            for row in cur.fetchall():
                weights[row[0]] = round(row[1], 3) if row[1] else 0.5

    except Exception as e:
        logger.warning("[Evaluator] 获取步骤权重失败: %s", e)

    return weights


# ═══════════════════════════════════════════════════════════════
# 评估报告（整体 + 分项）
# ═══════════════════════════════════════════════════════════════

def get_eval_report(chain_id: str = None, days: int = 30) -> Dict[str, Any]:
    """获取评估报告：整体评估 + 分项评估。

    Args:
        chain_id: 指定链路，None 则返回所有链路。
        days: 统计最近 N 天。

    Returns:
        {
            "overall": {                          # 整体评估
                "chain_id": str,
                "total_executions": int,
                "evaluated_count": int,
                "accuracy": {"1d": float, "3d": float, "5d": float},
                "trend": [                          # 每日趋势
                    {"date": str, "acc_1d": float, "acc_3d": float, "acc_5d": float},
                    ...
                ],
            },
            "steps": [                            # 分项评估（各步骤）
                {
                    "step_name": str,
                    "accuracy": {"1d": float, "3d": float, "5d": float},
                    "count": int,
                    "trend": [{"date": str, "acc_3d": float}, ...],
                },
                ...
            ],
        }
    """
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)

    result = {
        "overall": {},
        "steps": [],
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            chain_filter = "AND e.chain_id = %s" if chain_id else ""
            params_base = [since] + ([chain_id] if chain_id else [])

            # ── 整体评估 ──
            cur.execute(f"""
                SELECT
                    e.chain_id,
                    COUNT(DISTINCT e.id) as total,
                    COUNT(DISTINCT CASE WHEN s.id IS NOT NULL THEN e.id END) as evaluated,
                    AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_chain_executions e
                LEFT JOIN qd_chain_step_scores s ON s.execution_id = e.id
                WHERE e.exec_date >= %s {chain_filter}
                  AND e.evaluated = TRUE AND s.correct_1d IS NOT NULL
                GROUP BY e.chain_id
            """, params_base)

            row = cur.fetchone()
            if row:
                result["overall"] = {
                    "chain_id": row[0],
                    "total_executions": row[1],
                    "evaluated_count": row[2],
                    "accuracy": {
                        "1d": round(row[3], 3) if row[3] else 0,
                        "3d": round(row[4], 3) if row[4] else 0,
                        "5d": round(row[5], 3) if row[5] else 0,
                    },
                }

                # 整体每日趋势
                cur.execute(f"""
                    SELECT
                        e.exec_date,
                        AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                        AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                        AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                    FROM qd_chain_executions e
                    JOIN qd_chain_step_scores s ON s.execution_id = e.id
                    WHERE e.exec_date >= %s {chain_filter}
                      AND e.evaluated = TRUE AND s.correct_1d IS NOT NULL
                    GROUP BY e.exec_date
                    ORDER BY e.exec_date
                """, params_base)

                result["overall"]["trend"] = [
                    {
                        "date": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                        "acc_1d": round(r[1], 3) if r[1] else 0,
                        "acc_3d": round(r[2], 3) if r[2] else 0,
                        "acc_5d": round(r[3], 3) if r[3] else 0,
                    }
                    for r in cur.fetchall()
                ]

            # ── 分项评估（各步骤）──
            cur.execute(f"""
                SELECT
                    cs.step_name,
                    COUNT(*) as cnt,
                    AVG(CASE WHEN s.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN s.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_chain_step_scores s
                JOIN qd_chain_steps cs ON s.step_id = cs.id
                JOIN qd_chain_executions e ON s.execution_id = e.id
                WHERE e.exec_date >= %s {chain_filter}
                  AND s.correct_1d IS NOT NULL
                GROUP BY cs.step_name
                ORDER BY acc_3d DESC
            """, params_base)

            steps = []
            for row in cur.fetchall():
                step_name = row[0]

                # 各步骤每日趋势
                cur.execute(f"""
                    SELECT
                        e.exec_date,
                        AVG(CASE WHEN s.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                        COUNT(*) as cnt
                    FROM qd_chain_step_scores s
                    JOIN qd_chain_steps cs ON s.step_id = cs.id
                    JOIN qd_chain_executions e ON s.execution_id = e.id
                    WHERE e.exec_date >= %s {chain_filter}
                      AND cs.step_name = %s AND s.correct_3d IS NOT NULL
                    GROUP BY e.exec_date
                    ORDER BY e.exec_date
                """, params_base + [step_name])

                step_trend = [
                    {
                        "date": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                        "acc_3d": round(r[1], 3) if r[1] else 0,
                        "count": r[2],
                    }
                    for r in cur.fetchall()
                ]

                steps.append({
                    "step_name": step_name,
                    "accuracy": {
                        "1d": round(row[2], 3) if row[2] else 0,
                        "3d": round(row[3], 3) if row[3] else 0,
                        "5d": round(row[4], 3) if row[4] else 0,
                    },
                    "count": row[1],
                    "trend": step_trend,
                })

            result["steps"] = steps

    except Exception as e:
        logger.error("[Evaluator] 获取评估报告失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# 分项优化建议（离线调参，不重跑链路）
# ═══════════════════════════════════════════════════════════════

def generate_optimization(chain_id: str, days: int = 30) -> Dict[str, Any]:
    """基于历史评估数据，生成链路优化建议。

    不需要重跑链路，直接从 qd_chain_eval_summary 读取分项数据，
    分析步骤/技能/工具的表现，输出可执行的调参建议。

    Returns:
        {
            "chain_id": str,
            "based_on": int,              # 建议基于多少条评估数据
            "step_adjustments": [          # 步骤权重调整
                {"step": str, "current_weight": float, "suggested_weight": float, "reason": str}
            ],
            "skill_issues": [              # 技能问题
                {"skill": str, "accuracy_3d": float, "issue": str}
            ],
            "tool_issues": [               # 工具问题
                {"tool": str, "useful_rate": float, "issue": str}
            ],
            "actions": [                   # 可执行的优化动作
                {"type": str, "target": str, "action": str, "priority": str}
            ],
        }
    """
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {
        "chain_id": chain_id,
        "based_on": 0,
        "step_adjustments": [],
        "skill_issues": [],
        "tool_issues": [],
        "actions": [],
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 读最近一次汇总
            cur.execute("""
                SELECT step_accuracies, skill_accuracies, tool_stats, evaluated_count
                FROM qd_chain_eval_summary
                WHERE chain_id = %s AND eval_date >= %s
                ORDER BY eval_date DESC
                LIMIT 1
            """, (chain_id, since))

            row = cur.fetchone()
            if not row:
                return result

            step_json, skill_json, tool_json, eval_count = row
            result["based_on"] = eval_count or 0

            step_acc = json.loads(step_json) if step_json else {}
            skill_acc = json.loads(skill_json) if skill_json else {}
            tool_stats = json.loads(tool_json) if tool_json else {}

            # ── 步骤权重调整 ──
            for step_name, acc in step_acc.items():
                acc_3d = acc.get("3d", 0)
                count = acc.get("count", 0)
                if count < 3:
                    continue  # 样本太少不调

                if acc_3d >= 0.65:
                    suggested = 1.3
                    reason = f"准确率 {acc_3d:.0%}，建议加权"
                elif acc_3d <= 0.35:
                    suggested = 0.5
                    reason = f"准确率仅 {acc_3d:.0%}，建议降权"
                else:
                    suggested = 1.0
                    reason = f"准确率 {acc_3d:.0%}，维持默认权重"

                if suggested != 1.0:
                    result["step_adjustments"].append({
                        "step": step_name,
                        "current_weight": 1.0,
                        "suggested_weight": suggested,
                        "reason": reason,
                    })

            # ── 技能问题 ──
            for skill_name, acc in skill_acc.items():
                acc_3d = acc.get("3d", 0)
                count = acc.get("count", 0)
                if count < 3:
                    continue

                if acc_3d < 0.4:
                    result["skill_issues"].append({
                        "skill": skill_name,
                        "accuracy_3d": acc_3d,
                        "issue": f"准确率 {acc_3d:.0%}，需要检查该技能的 prompt 或数据源",
                    })

            # ── 工具问题 ──
            for tool_name, stats in tool_stats.items():
                calls = stats.get("calls", 0)
                ok = stats.get("ok", 0)
                useful = stats.get("useful", 0)
                useful_rate = stats.get("useful_rate", 0)

                if calls < 3:
                    continue

                success_rate = ok / calls if calls > 0 else 0
                if success_rate < 0.7:
                    result["tool_issues"].append({
                        "tool": tool_name,
                        "useful_rate": useful_rate,
                        "issue": f"成功率仅 {success_rate:.0%}（{ok}/{calls}），工具不稳定",
                    })
                elif useful_rate < 0.3:
                    result["tool_issues"].append({
                        "tool": tool_name,
                        "useful_rate": useful_rate,
                        "issue": f"有用率仅 {useful_rate:.0%}，工具返回数据对判断几乎无帮助",
                    })

            # ── 生成可执行动作 ──
            for adj in result["step_adjustments"]:
                result["actions"].append({
                    "type": "weight",
                    "target": adj["step"],
                    "action": f"权重 {adj['current_weight']} → {adj['suggested_weight']}",
                    "priority": "high" if adj["suggested_weight"] < 0.6 else "medium",
                })

            for issue in result["tool_issues"]:
                result["actions"].append({
                    "type": "tool",
                    "target": issue["tool"],
                    "action": issue["issue"],
                    "priority": "high" if issue["useful_rate"] < 0.2 else "medium",
                })

            for issue in result["skill_issues"]:
                result["actions"].append({
                    "type": "skill",
                    "target": issue["skill"],
                    "action": issue["issue"],
                    "priority": "high",
                })

            priority_order = {"high": 0, "medium": 1, "low": 2}
            result["actions"].sort(key=lambda x: priority_order.get(x["priority"], 9))

    except Exception as e:
        logger.error("[Evaluator] 生成优化建议失败: %s", e)

    return result
