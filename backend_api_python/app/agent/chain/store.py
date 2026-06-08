# -*- coding: utf-8 -*-
"""
Store — qd_evaluations 树形持久化层。

职责：
  save_tree(node)   → 将整棵 EvalNode 树写入 qd_evaluations（含所有子节点）
  load_tree(root_id) → 从 qd_evaluations 读取整棵树，重建 EvalNode 父子关系
  query_roots(...)   → 查询根节点列表（分页/过滤）
  update_verify(...) → 回溯时写入验证结果

一张表存一棵树，用 parent_id 自引用。
root_id 字段冗余存储根节点 id，方便快速查整棵树。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.agent.chain.schema import EvalNode, FactorItem, Layer, Status

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 写入
# ═══════════════════════════════════════════════════════════════

def save_tree(root: EvalNode) -> Optional[int]:
    """将整棵 EvalNode 树写入 qd_evaluations。

    递归写入：根节点 → skill 子节点 → tool 叶子节点。
    已有 id 的节点做 UPDATE，没有的做 INSERT。

    Args:
        root: 根节点（chain 层）

    Returns:
        root_id（根节点的数据库 id），失败返回 None
    """
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            root_id = _save_node(cur, root, parent_id=None, root_id=None)
            conn.commit()
            logger.info("[Store] 保存决策树 root_id=%d stock=%s chain=%s children=%d",
                        root_id, root.stock_code, root.name, len(root.children))
            return root_id
    except Exception as e:
        logger.error("[Store] 保存决策树失败: %s", e, exc_info=True)
        return None


def _save_node(cur, node: EvalNode, parent_id: Optional[int], root_id: Optional[int]) -> int:
    """递归保存单个节点及其子节点。"""
    factors_json = json.dumps([f.to_dict() for f in node.factors], ensure_ascii=False)
    output_json = json.dumps(node.output_data, ensure_ascii=False) if node.output_data else None
    input_json = json.dumps(node.input_params, ensure_ascii=False) if node.input_params else None
    tools_json = json.dumps(node.tools_called, ensure_ascii=False) if node.tools_called else None
    missing_json = json.dumps(node.missing_data, ensure_ascii=False) if node.missing_data else None

    if node.id is not None:
        # UPDATE
        cur.execute("""
            UPDATE qd_evaluations SET
                parent_id=%s, root_id=%s, layer=%s, name=%s, step_order=%s,
                exec_date=%s, stock_code=%s, stock_name=%s,
                score=%s, direction=%s, action=%s, signal=%s, confidence=%s,
                factors=%s, output_data=%s, analysis=%s,
                input_params=%s, tools_called=%s, missing_data=%s, data_source=%s,
                status=%s, error=%s, elapsed_ms=%s,
                actual_return_1d=%s, actual_return_3d=%s, actual_return_5d=%s,
                actual_direction_3d=%s, correct_3d=%s, calibration=%s,
                human_reviewed=%s, human_verdict=%s
            WHERE id=%s
            RETURNING id
        """, (
            parent_id, root_id, node.layer, node.name, node.step_order,
            node.exec_date, node.stock_code, node.stock_name,
            node.score, node.direction, node.action, node.signal, node.confidence,
            factors_json, output_json, node.analysis,
            input_json, tools_json, missing_json, node.data_source,
            node.status, node.error, node.elapsed_ms,
            node.actual_return_1d, node.actual_return_3d, node.actual_return_5d,
            node.actual_direction_3d, node.correct_3d, node.calibration,
            node.human_reviewed, node.human_verdict,
            node.id,
        ))
        node_id = cur.fetchone()[0]
    else:
        # INSERT
        cur.execute("""
            INSERT INTO qd_evaluations (
                parent_id, root_id, layer, name, step_order,
                exec_date, stock_code, stock_name,
                score, direction, action, signal, confidence,
                factors, output_data, analysis,
                input_params, tools_called, missing_data, data_source,
                status, error, elapsed_ms,
                actual_return_1d, actual_return_3d, actual_return_5d,
                actual_direction_3d, correct_3d, calibration,
                human_reviewed, human_verdict
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s
            ) RETURNING id
        """, (
            parent_id, root_id, node.layer, node.name, node.step_order,
            node.exec_date, node.stock_code, node.stock_name,
            node.score, node.direction, node.action, node.signal, node.confidence,
            factors_json, output_json, node.analysis,
            input_json, tools_json, missing_json, node.data_source,
            node.status, node.error, node.elapsed_ms,
            node.actual_return_1d, node.actual_return_3d, node.actual_return_5d,
            node.actual_direction_3d, node.correct_3d, node.calibration,
            node.human_reviewed, node.human_verdict,
        ))
        node_id = cur.fetchone()[0]

    node.id = node_id
    if root_id is None:
        root_id = node_id
        # 回填 root_id
        cur.execute("UPDATE qd_evaluations SET root_id=%s WHERE id=%s", (root_id, node_id))

    # 递归保存子节点
    for i, child in enumerate(node.children):
        child.step_order = i + 1
        _save_node(cur, child, parent_id=node_id, root_id=root_id)

    return node_id


# ═══════════════════════════════════════════════════════════════
# 读取
# ═══════════════════════════════════════════════════════════════

def load_tree(root_id: int) -> Optional[EvalNode]:
    """从数据库读取整棵决策树。

    Args:
        root_id: 根节点 id

    Returns:
        完整的 EvalNode 树，找不到返回 None
    """
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, parent_id, root_id, layer, name, step_order,
                       exec_date, stock_code, stock_name,
                       score, direction, action, signal, confidence,
                       factors, output_data, analysis,
                       input_params, tools_called, missing_data, data_source,
                       status, error, elapsed_ms,
                       actual_return_1d, actual_return_3d, actual_return_5d,
                       actual_direction_3d, correct_3d, calibration,
                       human_reviewed, human_verdict
                FROM qd_evaluations
                WHERE root_id = %s
                ORDER BY step_order ASC
            """, (root_id,))

            rows = cur.fetchall()
            if not rows:
                return None

            # 构建节点映射
            nodes: Dict[int, EvalNode] = {}
            for row in rows:
                node = _row_to_node(row)
                nodes[node.id] = node

            # 构建树形关系
            root = None
            for node in nodes.values():
                if node.parent_id is None or node.parent_id not in nodes:
                    root = node
                else:
                    parent = nodes[node.parent_id]
                    parent.children.append(node)

            return root

    except Exception as e:
        logger.error("[Store] 读取决策树 root_id=%d 失败: %s", root_id, e)
        return None


def _row_to_node(row) -> EvalNode:
    """将数据库行转为 EvalNode。"""
    (id_, parent_id, root_id, layer, name, step_order,
     exec_date, stock_code, stock_name,
     score, direction, action, signal, confidence,
     factors_json, output_json, analysis,
     input_json, tools_json, missing_json, data_source,
     status, error, elapsed_ms,
     ret_1d, ret_3d, ret_5d, actual_dir_3d, correct_3d, calibration,
     human_reviewed, human_verdict) = row

    def _parse_json(val, default=None):
        if val is None:
            return default if default is not None else {}
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return default if default is not None else {}
        return val

    factors_raw = _parse_json(factors_json, [])
    factors = [FactorItem.from_dict(f) for f in factors_raw] if isinstance(factors_raw, list) else []

    return EvalNode(
        id=id_, parent_id=parent_id, root_id=root_id,
        layer=layer, name=name, step_order=step_order,
        exec_date=exec_date, stock_code=stock_code, stock_name=stock_name,
        score=score, direction=direction or "", action=action or "",
        signal=signal or "", confidence=confidence,
        factors=factors,
        output_data=_parse_json(output_json, {}),
        analysis=analysis or "",
        input_params=_parse_json(input_json, {}),
        tools_called=_parse_json(tools_json, []),
        missing_data=_parse_json(missing_json, []),
        data_source=data_source or "",
        status=status or Status.OK.value, error=error or "",
        elapsed_ms=elapsed_ms or 0.0,
        actual_return_1d=ret_1d, actual_return_3d=ret_3d, actual_return_5d=ret_5d,
        actual_direction_3d=actual_dir_3d or "",
        correct_3d=correct_3d, calibration=calibration or 1.0,
        human_reviewed=human_reviewed or False, human_verdict=human_verdict or "",
    )


# ═══════════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════════

def query_roots(
    stock_code: str = None,
    chain_id: str = None,
    since: date = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """查询根节点列表（不加载子节点，仅摘要信息）。"""
    from app.utils.db import get_db_connection

    conditions = ["parent_id IS NULL"]
    params = []

    if stock_code:
        conditions.append("stock_code = %s")
        params.append(stock_code)
    if chain_id:
        conditions.append("name = %s")
        params.append(chain_id)
    if since:
        conditions.append("exec_date >= %s")
        params.append(since)

    where = " AND ".join(conditions)
    params.extend([limit, offset])

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT id, exec_date, stock_code, stock_name, name,
                       score, action, direction, confidence, status, created_at
                FROM qd_evaluations
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, params)

            return [
                {
                    "id": row[0], "exec_date": row[1].isoformat() if row[1] else None,
                    "stock_code": row[2], "stock_name": row[3],
                    "chain_id": row[4], "score": row[5], "action": row[6],
                    "direction": row[7], "confidence": row[8],
                    "status": row[9],
                    "created_at": row[10].isoformat() if row[10] else None,
                }
                for row in cur.fetchall()
            ]
    except Exception as e:
        logger.error("[Store] 查询根节点失败: %s", e)
        return []


def query_pending_verify(days_old: int = 1, limit: int = 100) -> List[Dict[str, Any]]:
    """查询待验证的根节点（correct_3d 为空且足够老）。"""
    from app.utils.db import get_db_connection

    cutoff = date.today() - timedelta(days=days_old)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, exec_date, stock_code, stock_name, name, action
                FROM qd_evaluations
                WHERE parent_id IS NULL
                  AND correct_3d IS NULL
                  AND exec_date <= %s
                ORDER BY exec_date ASC
                LIMIT %s
            """, (cutoff, limit))

            return [
                {
                    "id": row[0], "exec_date": row[1],
                    "stock_code": row[2], "stock_name": row[3],
                    "chain_id": row[4], "action": row[5],
                }
                for row in cur.fetchall()
            ]
    except Exception as e:
        logger.error("[Store] 查询待验证节点失败: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════
# 回溯验证写入
# ═══════════════════════════════════════════════════════════════

def update_verify_results(
    root_id: int,
    actual_return_1d: float = None,
    actual_return_3d: float = None,
    actual_return_5d: float = None,
    actual_direction_3d: str = "",
    correct_3d: bool = None,
):
    """写入根节点的验证结果。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_evaluations SET
                    actual_return_1d = %s, actual_return_3d = %s, actual_return_5d = %s,
                    actual_direction_3d = %s, correct_3d = %s
                WHERE id = %s AND parent_id IS NULL
            """, (actual_return_1d, actual_return_3d, actual_return_5d,
                  actual_direction_3d, correct_3d, root_id))
            conn.commit()
    except Exception as e:
        logger.error("[Store] 写入验证结果失败 root_id=%d: %s", root_id, e)


def update_skill_verify(root_id: int, actual_direction_3d: str):
    """回溯时逐层验证：更新每个 skill 子节点的 correct_3d。

    三值逻辑：
      - neutral 预测 → correct_3d = NULL（不参与统计，不惩罚不奖励）
      - correct/wrong → 正常写入
    """
    from app.agent.chain.schema import is_direction_correct

    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 获取该树下所有 skill 节点
            cur.execute("""
                SELECT id, direction, score
                FROM qd_evaluations
                WHERE root_id = %s AND layer = 'skill' AND status = 'ok'
            """, (root_id,))

            for step_id, direction, score in cur.fetchall():
                if not direction:
                    continue

                verdict = is_direction_correct(direction, actual_direction_3d)

                # neutral 预测 → 不参与统计
                if verdict == "neutral":
                    cur.execute("""
                        UPDATE qd_evaluations SET
                            actual_direction_3d = %s,
                            correct_3d = NULL,
                            calibration = 1.0
                        WHERE id = %s
                    """, (actual_direction_3d, step_id))
                    continue

                # correct / wrong → 正常写入
                correct = verdict == "correct"

                # 校准因子：score 越偏离50（越自信），权重更新幅度越大
                calibration = 1.0
                if score is not None:
                    confidence = abs(score - 50) / 50.0
                    calibration = round(1.0 + confidence * 0.05, 4)

                cur.execute("""
                    UPDATE qd_evaluations SET
                        actual_direction_3d = %s, correct_3d = %s, calibration = %s
                    WHERE id = %s
                """, (actual_direction_3d, correct, calibration, step_id))

            conn.commit()
    except Exception as e:
        logger.error("[Store] 更新 skill 验证失败 root_id=%d: %s", root_id, e)


# ═══════════════════════════════════════════════════════════════
# 因子权重
# ═══════════════════════════════════════════════════════════════

def get_factor_weights(chain_id: str) -> Dict[str, float]:
    """获取链路的因子权重（accuracy_3d → weight 映射）。"""
    from app.utils.db import get_db_connection

    weights = {}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT factor_name, weight
                FROM qd_factor_weights
                WHERE chain_id = %s AND sample_count >= 5
            """, (chain_id,))
            for fname, weight in cur.fetchall():
                weights[fname] = weight
    except Exception as e:
        logger.warning("[Store] 获取因子权重失败: %s", e)
    return weights


def get_skill_weights(chain_id: str) -> Dict[str, float]:
    """获取链路各 skill 的历史准确率权重。

    从 qd_evaluations 中聚合已验证的 skill 节点的 correct_3d。
    """
    from app.utils.db import get_db_connection

    weights = {}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT name,
                       AVG(CASE WHEN correct_3d THEN 1.0 ELSE 0.0 END) as acc,
                       COUNT(*) as cnt
                FROM qd_evaluations
                WHERE root_id IN (
                    SELECT id FROM qd_evaluations
                    WHERE parent_id IS NULL AND name = %s AND correct_3d IS NOT NULL
                )
                AND layer = 'skill' AND correct_3d IS NOT NULL
                GROUP BY name
                HAVING COUNT(*) >= 3
            """, (chain_id,))

            for name, acc, cnt in cur.fetchall():
                weights[name] = round(acc, 3)
    except Exception as e:
        logger.warning("[Store] 获取 skill 权重失败: %s", e)
    return weights


def get_eval_stats(chain_id: str = None) -> Dict[str, Any]:
    """获取评估统计（供门控使用）。"""
    from app.utils.db import get_db_connection

    result = {
        "total_decisions": 0,
        "evaluated_decisions": 0,
        "overall_accuracy_3d": 0.0,
        "ready_for_decision": False,
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            chain_filter = "AND name = %s" if chain_id else ""
            params = [chain_id] if chain_id else []

            cur.execute(f"""
                SELECT COUNT(*), COUNT(CASE WHEN correct_3d IS NOT NULL THEN 1 END)
                FROM qd_evaluations
                WHERE parent_id IS NULL {chain_filter}
            """, params)

            row = cur.fetchone()
            if row:
                result["total_decisions"] = row[0]
                result["evaluated_decisions"] = row[1]

            if result["evaluated_decisions"] > 0:
                cur.execute(f"""
                    SELECT AVG(CASE WHEN correct_3d THEN 1.0 ELSE 0.0 END)
                    FROM qd_evaluations
                    WHERE parent_id IS NULL AND correct_3d IS NOT NULL {chain_filter}
                """, params)
                acc = cur.fetchone()
                if acc and acc[0] is not None:
                    result["overall_accuracy_3d"] = round(float(acc[0]), 3)

            result["ready_for_decision"] = result["evaluated_decisions"] >= 10

    except Exception as e:
        logger.warning("[Store] 获取评估统计失败: %s", e)

    return result
