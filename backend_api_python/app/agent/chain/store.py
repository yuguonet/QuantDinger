# -*- coding: utf-8 -*-
"""
Store — qd_traces 树形持久化层（替代旧 qd_evaluations）。

职责：
  save_tree(node)   → 将整棵 EvalNode 树写入 qd_traces（含所有子节点）
  load_tree(root_id) → 从 qd_traces 读取整棵树，重建 EvalNode 父子关系
  query_roots(...)   → 查询根节点列表（分页/过滤）
  update_verify(...) → 回溯时写入验证结果
  get_skill_weights() → 获取 Skill 历史权重
  get_factor_weights() → 获取因子权重

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


def _list_to_pg_array(items: list) -> str:
    """将 Python list 转为 PostgreSQL TEXT[] 格式。

    ['a', 'b', 'c'] → '{a,b,c}'
    需要转义的字符会被正确处理。
    """
    escaped = []
    for item in items:
        s = str(item).replace("\\", "\\\\").replace('"', '\\"')
        escaped.append(f'"{s}"')
    return "{" + ",".join(escaped) + "}"


# ═══════════════════════════════════════════════════════════════
# 写入
# ═══════════════════════════════════════════════════════════════

def save_tree(root: EvalNode) -> Optional[int]:
    """将整棵 EvalNode 树写入 qd_traces。

    递归写入：根节点 → skill 子节点 → tool 叶子节点。
    已有 id 的节点做 UPDATE，没有的做 INSERT。

    Returns:
        root_id（根节点的数据库 id），失败返回 None
    """
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            root_id = _save_node(cur, root, parent_id=None, root_id=None,
                                 root_exec_date=root.exec_date or date.today())
            conn.commit()
            logger.info("[Store] 保存决策树 root_id=%d stock=%s chain=%s children=%d",
                        root_id, root.stock_code, root.name, len(root.children))
            return root_id
    except Exception as e:
        logger.error("[Store] 保存决策树失败: %s", e, exc_info=True)
        return None


def _save_node(cur, node: EvalNode, parent_id: Optional[int], root_id: Optional[int],
               root_exec_date: Optional[date] = None) -> int:
    """递归保存单个节点及其子节点。"""
    # 子节点继承根节点的 exec_date，避免 NOT NULL 约束报错
    if node.exec_date is None:
        node.exec_date = root_exec_date or date.today()
    factors_json = json.dumps([f.to_dict() for f in node.factors], ensure_ascii=False)
    output_json = json.dumps(node.output_data, ensure_ascii=False) if node.output_data else None
    input_json = json.dumps(node.input_params, ensure_ascii=False) if node.input_params else None
    # TEXT[] 列需要 PG 数组格式 '{a,b,c}'，不能用 JSON 格式 '["a","b","c"]'
    tools_pg_array = _list_to_pg_array(node.tools_called) if node.tools_called else None
    missing_pg_array = _list_to_pg_array(node.missing_data) if node.missing_data else None

    if node.id is not None:
        # UPDATE
        cur.execute("""
            UPDATE qd_traces SET
                parent_id=%s, root_id=%s, layer=%s, name=%s, step_order=%s,
                exec_date=%s, stock_code=%s, stock_name=%s,
                score=%s, direction=%s, action=%s, signal=%s, confidence=%s,
                timeframe=%s, factors=%s, output_summary=%s, analysis=%s,
                input_params=%s, tools_called=%s, missing_data=%s, data_source=%s,
                status=%s, error=%s, elapsed_ms=%s,
                exit_date=%s, exit_reason=%s, pnl_pct=%s, hold_days=%s,
                correct=%s, calibration=%s
            WHERE id=%s
            RETURNING id
        """, (
            parent_id, root_id, node.layer, node.name, node.step_order,
            node.exec_date, node.stock_code, node.stock_name,
            node.score, node.direction, node.action, node.signal, node.confidence,
            node.timeframe, factors_json, output_json, node.analysis,
            input_json, tools_pg_array, missing_pg_array, node.data_source,
            node.status, node.error, node.elapsed_ms,
            node.exit_date, node.exit_reason, node.pnl_pct, node.hold_days,
            node.correct, node.calibration,
            node.id,
        ))
        node_id = cur.fetchone()['id']
    else:
        # INSERT
        cur.execute("""
            INSERT INTO qd_traces (
                parent_id, root_id, layer, name, step_order,
                exec_date, stock_code, stock_name,
                score, direction, action, signal, confidence,
                timeframe, factors, output_summary, analysis,
                input_params, tools_called, missing_data, data_source,
                status, error, elapsed_ms,
                exit_date, exit_reason, pnl_pct, hold_days,
                correct, calibration
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s
            ) RETURNING id
        """, (
            parent_id, root_id, node.layer, node.name, node.step_order,
            node.exec_date, node.stock_code, node.stock_name,
            node.score, node.direction, node.action, node.signal, node.confidence,
            node.timeframe, factors_json, output_json, node.analysis,
            input_json, tools_pg_array, missing_pg_array, node.data_source,
            node.status, node.error, node.elapsed_ms,
            node.exit_date, node.exit_reason, node.pnl_pct, node.hold_days,
            node.correct, node.calibration,
        ))
        node_id = cur.fetchone()['id']

    node.id = node_id
    if root_id is None:
        root_id = node_id
        cur.execute("UPDATE qd_traces SET root_id=%s WHERE id=%s", (root_id, node_id))

    # 递归保存子节点（传递 root_exec_date 保证子节点有 exec_date）
    for i, child in enumerate(node.children):
        child.step_order = i + 1
        _save_node(cur, child, parent_id=node_id, root_id=root_id,
                   root_exec_date=node.exec_date)

    return node_id


# ═══════════════════════════════════════════════════════════════
# 读取
# ═══════════════════════════════════════════════════════════════

def load_tree(root_id: int) -> Optional[EvalNode]:
    """从数据库读取整棵决策树。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, parent_id, root_id, layer, name, step_order,
                       exec_date, stock_code, stock_name,
                       score, direction, action, signal, confidence,
                       timeframe, factors, output_summary, analysis,
                       input_params, tools_called, missing_data, data_source,
                       status, error, elapsed_ms,
                       exit_date, exit_reason, pnl_pct, hold_days,
                       correct, calibration
                FROM qd_traces
                WHERE root_id = %s
                ORDER BY step_order ASC
            """, (root_id,))

            rows = cur.fetchall()
            if not rows:
                return None

            nodes: Dict[int, EvalNode] = {}
            for row in rows:
                node = _row_to_node(row)
                nodes[node.id] = node

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
     timeframe, factors_json, output_json, analysis,
     input_json, tools_json, missing_json, data_source,
     status, error, elapsed_ms,
     exit_date, exit_reason, pnl_pct, hold_days,
     correct, calibration) = row

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
        timeframe=timeframe or "",
        factors=factors,
        output_data=_parse_json(output_json, {}),
        analysis=analysis or "",
        input_params=_parse_json(input_json, {}),
        tools_called=_parse_json(tools_json, []),
        missing_data=_parse_json(missing_json, []),
        data_source=data_source or "",
        status=status or Status.OK.value, error=error or "",
        elapsed_ms=elapsed_ms or 0.0,
        exit_date=exit_date, exit_reason=exit_reason or "",
        pnl_pct=pnl_pct, hold_days=hold_days,
        correct=correct, calibration=calibration or 1.0,
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
                       score, action, direction, confidence, timeframe, status, created_at
                FROM qd_traces
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, params)

            return [
                {
                    "id": row['id'],
                    "exec_date": row['exec_date'].isoformat() if row['exec_date'] else None,
                    "stock_code": row['stock_code'], "stock_name": row['stock_name'],
                    "chain_id": row['name'], "score": row['score'], "action": row['action'],
                    "direction": row['direction'], "confidence": row['confidence'],
                    "timeframe": row['timeframe'], "status": row['status'],
                    "created_at": row['created_at'].isoformat() if row['created_at'] else None,
                }
                for row in cur.fetchall()
            ]
    except Exception as e:
        logger.error("[Store] 查询根节点失败: %s", e)
        return []


def query_pending_verify(days_old: int = 1, limit: int = 100) -> List[Dict[str, Any]]:
    """查询待验证的根节点（exit_date IS NULL 且足够老）。"""
    from app.utils.db import get_db_connection

    cutoff = date.today() - timedelta(days=days_old)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, exec_date, stock_code, stock_name, name, action, timeframe
                FROM qd_traces
                WHERE parent_id IS NULL
                  AND exit_date IS NULL
                  AND exec_date <= %s
                ORDER BY exec_date ASC
                LIMIT %s
            """, (cutoff, limit))

            return [
                {
                    "id": row['id'], "exec_date": row['exec_date'],
                    "stock_code": row['stock_code'], "stock_name": row['stock_name'],
                    "chain_id": row['name'], "action": row['action'],
                    "timeframe": row['timeframe'],
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
    exit_date: date = None,
    exit_reason: str = "",
    pnl_pct: float = None,
    hold_days: int = None,
    correct: bool = None,
):
    """写入根节点的验证结果。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_traces SET
                    exit_date = %s, exit_reason = %s,
                    pnl_pct = %s, hold_days = %s, correct = %s
                WHERE id = %s AND parent_id IS NULL
            """, (exit_date, exit_reason, pnl_pct, hold_days, correct, root_id))
            conn.commit()
    except Exception as e:
        logger.error("[Store] 写入验证结果失败 root_id=%d: %s", root_id, e)


def update_skill_verify(root_id: int, actual_direction: str):
    """回溯时逐层验证：更新每个 skill 子节点的 correct。

    三值逻辑：
      - neutral 预测 → correct = NULL（不参与统计）
      - correct/wrong → 正常写入
    """
    from app.agent.chain.schema import is_direction_correct
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, direction, score
                FROM qd_traces
                WHERE root_id = %s AND layer = 'skill' AND status = 'ok'
            """, (root_id,))

            for step_id, direction, score in cur.fetchall():
                if not direction:
                    continue

                verdict = is_direction_correct(direction, actual_direction)

                if verdict == "neutral":
                    cur.execute("""
                        UPDATE qd_traces SET
                            correct = NULL, calibration = 1.0
                        WHERE id = %s
                    """, (step_id,))
                    continue

                correct = verdict == "correct"
                calibration = 1.0
                if score is not None:
                    confidence = abs(score - 50) / 50.0
                    calibration = round(1.0 + confidence * 0.05, 4)

                cur.execute("""
                    UPDATE qd_traces SET correct = %s, calibration = %s
                    WHERE id = %s
                """, (correct, calibration, step_id))

            conn.commit()
    except Exception as e:
        logger.error("[Store] 更新 skill 验证失败 root_id=%d: %s", root_id, e)


def update_path_cache(root_id: int):
    """验证完成后，增量更新 qd_agent_path_cache。

    累加器字段 +1，派生值查询时实时计算。
    """
    import hashlib
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 读根节点信息
            cur.execute("""
                SELECT name, tools_called, correct, pnl_pct, hold_days
                FROM qd_traces WHERE id = %s AND layer = 'chain'
            """, (root_id,))
            root = cur.fetchone()
            if not root or not root['tools_called']:
                return

            tools = root['tools_called']
            if not isinstance(tools, list):
                tools = list(tools)
            if not tools:
                return

            parts = root['name'].split('+')
            domain = parts[0] if len(parts) >= 3 else ''
            verb = parts[-2] if len(parts) >= 2 else parts[0]
            noun = parts[-1] if len(parts) >= 2 else ''

            sig = hashlib.md5(','.join(tools).encode()).hexdigest()

            # 累加字段
            correct_val = 1 if root['correct'] else 0
            pnl_val = root['pnl_pct'] or 0
            hold_val = root['hold_days'] or 3

            cur.execute("""
                INSERT INTO qd_agent_path_cache
                    (domain, verb, noun, tool_signature, tools,
                     total_runs, verified_runs, total_verified_correct,
                     total_verified_pnl, total_hold_days,
                     last_root_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, 1, 1, %s, %s, %s, %s, NOW())
                ON CONFLICT (domain, verb, noun, tool_signature)
                DO UPDATE SET
                    total_runs = qd_agent_path_cache.total_runs + 1,
                    verified_runs = qd_agent_path_cache.verified_runs + 1,
                    total_verified_correct = qd_agent_path_cache.total_verified_correct + %s,
                    total_verified_pnl = qd_agent_path_cache.total_verified_pnl + %s,
                    total_hold_days = qd_agent_path_cache.total_hold_days + %s,
                    last_root_id = EXCLUDED.last_root_id,
                    last_success_at = CASE WHEN %s THEN NOW()
                        ELSE qd_agent_path_cache.last_success_at END,
                    updated_at = NOW()
            """, (
                domain, verb, noun, sig, tools,
                correct_val, pnl_val, hold_val, root_id,
                correct_val, pnl_val, hold_val, bool(root['correct']),
            ))

            conn.commit()
    except Exception as e:
        logger.error("[Store] 更新路径缓存失败 root_id=%d: %s", root_id, e)


# ═══════════════════════════════════════════════════════════════
# 权重查询
#
# 统一权重表：qd_agent_weights（layer 区分 skill / factor）
#
# evaluator.update_weights() 自动同步 registry：
#   - 新 Skill 自动 INSERT 工厂默认值
#   - registry 删除的 Skill 保留但标记
#   - 增删 Skill 零维护
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 用户反馈惩罚（trace 层）
# ═══════════════════════════════════════════════════════════════

def query_latest_root(stock_code: str) -> Optional[Dict[str, Any]]:
    """查询某股票最近一条根节点 trace。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, exec_date, stock_code, stock_name, name,
                       score, action, direction, confidence, status
                FROM qd_traces
                WHERE parent_id IS NULL AND stock_code = %s
                ORDER BY created_at DESC LIMIT 1
            """, (stock_code,))

            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row['id'],
                "exec_date": row['exec_date'].isoformat() if row['exec_date'] else None,
                "stock_code": row['stock_code'],
                "stock_name": row['stock_name'],
                "chain_id": row['name'],
                "score": row['score'],
                "action": row['action'],
                "direction": row['direction'],
                "confidence": row['confidence'],
                "status": row['status'],
            }
    except Exception as e:
        logger.error("[Store] 查询最近根节点失败 stock=%s: %s", stock_code, e)
        return None


def mark_root_wrong(root_id: int):
    """轻度惩罚：标记根节点 correct=False + calibration 重校准。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_traces SET
                    correct = FALSE,
                    calibration = 1.10,
                    human_reviewed = TRUE,
                    human_verdict = 'negative_feedback'
                WHERE id = %s AND parent_id IS NULL
            """, (root_id,))
            conn.commit()
            logger.info("[Store] 标记 trace root_id=%d correct=False", root_id)
    except Exception as e:
        logger.error("[Store] 标记 trace 失败 root_id=%d: %s", root_id, e)


def delete_tree(root_id: int):
    """重度惩罚：删除整棵 trace 树。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_traces WHERE root_id = %s", (root_id,))
            deleted = cur.rowcount
            conn.commit()
            logger.info("[Store] 删除 trace 树 root_id=%d, 共 %d 条", root_id, deleted)
    except Exception as e:
        logger.error("[Store] 删除 trace 树失败 root_id=%d: %s", root_id, e)


def get_penalty_count(stock_code: str) -> int:
    """统计某股票最近 trace 的负面反馈次数（human_verdict='negative_feedback'）。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as cnt FROM qd_traces
                WHERE parent_id IS NULL
                  AND stock_code = %s
                  AND human_verdict = 'negative_feedback'
            """, (stock_code,))
            row = cur.fetchone()
            return row['cnt'] if row else 0
    except Exception as e:
        logger.warning("[Store] 统计惩罚次数失败: %s", e)
        return 0


def punish_tools(root_id: int, tool_names: list):
    """负面反馈 → 标记工具节点为 failed。

    被标记的工具在 query_low_weight_tools 中直接计入失败，无需等阈值。
    """
    from app.utils.db import get_db_connection

    if not tool_names:
        return

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            for name in tool_names:
                cur.execute("""
                    UPDATE qd_traces SET status = 'failed'
                    WHERE root_id = %s AND layer = 'tool' AND name = %s
                """, (root_id, name))
            conn.commit()
            logger.info("[Store] 惩罚工具 root_id=%d: %s", root_id, tool_names)
    except Exception as e:
        logger.error("[Store] 惩罚工具失败: %s", e)


def punish_path(domain: str, verb: str, noun: str, tools: list):
    """负面反馈 → 惩罚编排路径缓存（工具级）。

    惩罚方式：累加一次"错误执行"（verified_runs+1, correct不加）。
    1. 惩罚当前路径（精确匹配）
    2. 惩罚所有包含这些工具的路径（工具级扩散）
    """
    import hashlib
    from app.utils.db import get_db_connection

    if not tools:
        return
    sig = hashlib.md5(','.join(tools).encode()).hexdigest()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 1. 精确惩罚当前路径：+1 次错误
            cur.execute("""
                UPDATE qd_agent_path_cache SET
                    verified_runs = verified_runs + 1,
                    updated_at = NOW()
                WHERE domain = %s AND verb = %s AND noun = %s AND tool_signature = %s
            """, (domain, verb, noun, sig))

            # 2. 扩散：所有含这些工具的路径也 +1 次错误
            for tool in tools:
                cur.execute("""
                    UPDATE qd_agent_path_cache SET
                        verified_runs = verified_runs + 1,
                        updated_at = NOW()
                    WHERE domain = %s AND verb = %s AND noun = %s
                      AND %s = ANY(tools)
                      AND tool_signature != %s
                """, (domain, verb, noun, tool, sig))

            conn.commit()
            logger.info("[Store] 惩罚路径: %s+%s+%s tools=%s", domain, verb, noun, tools)
    except Exception as e:
        logger.error("[Store] 惩罚路径失败: %s", e)


def get_weights(layer: str, skill_name: str = None) -> Dict[str, float]:
    """从 qd_agent_weights 获取权重。

    Args:
        layer: 'skill' 或 'factor'
        skill_name: factor 层可选按 skill 过滤
    """
    from app.utils.db import get_db_connection
    weights = {}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            if layer == "factor" and skill_name:
                cur.execute("""
                    SELECT name, weight FROM qd_agent_weights
                    WHERE layer = %s AND skill_name = %s AND sample_count >= 5
                """, (layer, skill_name))
            elif layer == "factor":
                cur.execute("""
                    SELECT name, weight FROM qd_agent_weights
                    WHERE layer = %s AND sample_count >= 5
                """, (layer,))
            else:
                cur.execute(
                    "SELECT name, weight FROM qd_agent_weights WHERE layer = %s",
                    (layer,))
            for name, weight in cur.fetchall():
                weights[name] = weight
    except Exception as e:
        logger.warning("[Store] 获取 %s 权重失败: %s", layer, e)
    return weights


def get_skill_weights() -> Dict[str, float]:
    """便捷包装：获取 Skill 权重。"""
    return get_weights("skill")


def get_factor_weights(skill_name: str = None) -> Dict[str, float]:
    """便捷包装：获取因子权重。"""
    return get_weights("factor", skill_name=skill_name)


def query_cached_tools(domain: str, verb: str, noun: str, stock_code: str = None) -> Optional[List[str]]:
    """从 qd_agent_path_cache 查询最优编排路径。

    质量门：
      1. verified_runs >= MIN_SAMPLES
      2. win_rate >= MIN_WIN_RATE（从累加字段实时算）
      3. 工具步数 <= MAX_STEPS
    """
    from app.utils.db import get_db_connection

    if not verb or not noun:
        return None

    MIN_SAMPLES = 3
    MIN_WIN_RATE = 0.55
    MAX_STEPS = 6

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM (
                    SELECT tools, verified_runs,
                        total_verified_correct::float / verified_runs as win_rate,
                        CASE WHEN total_hold_days > 0
                            THEN total_verified_pnl / total_hold_days
                            ELSE 0 END as return_per_day
                    FROM qd_agent_path_cache
                    WHERE domain = %s AND verb = %s AND noun = %s
                      AND verified_runs >= %s
                      AND array_length(tools, 1) BETWEEN 1 AND %s
                ) sub
                WHERE win_rate >= %s
                ORDER BY return_per_day DESC
                LIMIT 1
            """, (domain, verb, noun, MIN_SAMPLES, MAX_STEPS, MIN_WIN_RATE))

            row = cur.fetchone()
            if row:
                tools = row['tools']
                logger.info("[Store] 路径缓存命中: %s+%s+%s tools=%s win_rate=%.2f",
                            domain, verb, noun, tools, row['win_rate'] or 0)
                return tools if isinstance(tools, list) else list(tools)
            return None

    except Exception as e:
        logger.warning("[Store] 查询路径缓存失败 %s+%s+%s: %s", domain, verb, noun, e)
        return None


def query_low_weight_tools(min_appearances: int = 5, max_win_rate: float = 0.4) -> set:
    """聚合 qd_traces，返回低权重工具集合。

    工具出现次数 >= min_appearances 且所在链路 win_rate < max_win_rate → 低权重。
    或：工具被 punish_tools 标记为 status='failed' → 直接计入。
    """
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            # 1. 被用户反馈直接标记为 failed 的工具
            cur.execute("""
                SELECT DISTINCT name FROM qd_traces
                WHERE layer = 'tool' AND status = 'failed'
            """)
            result = {row['name'] for row in cur.fetchall()}

            # 2. 聚合胜率低的工具
            cur.execute("""
                SELECT child.name
                FROM qd_traces child
                JOIN qd_traces root ON child.root_id = root.id
                WHERE child.layer = 'tool'
                  AND root.layer = 'chain'
                  AND root.correct IS NOT NULL
                GROUP BY child.name
                HAVING COUNT(*) >= %s
                   AND AVG(CASE WHEN root.correct THEN 1.0 ELSE 0.0 END) < %s
            """, (min_appearances, max_win_rate))
            for row in cur.fetchall():
                result.add(row['name'])

            if result:
                logger.info("[Store] 低权重工具 (%d): %s", len(result), result)
            return result
    except Exception as e:
        logger.warning("[Store] 查询工具权重失败: %s", e)
        return set()


def get_eval_stats(chain_id: str = None) -> Dict[str, Any]:

    """获取评估统计。"""
    from app.utils.db import get_db_connection

    result = {
        "total_decisions": 0,
        "evaluated_decisions": 0,
        "overall_accuracy": 0.0,
        "ready_for_decision": False,
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            chain_filter = "AND name = %s" if chain_id else ""
            params = [chain_id] if chain_id else []

            cur.execute(f"""
                SELECT COUNT(*) as total,
                       COUNT(CASE WHEN correct IS NOT NULL THEN 1 END) as evaluated
                FROM qd_traces
                WHERE parent_id IS NULL {chain_filter}
            """, params)

            row = cur.fetchone()
            if row:
                result["total_decisions"] = row['total']
                result["evaluated_decisions"] = row['evaluated']

            if result["evaluated_decisions"] > 0:
                cur.execute(f"""
                    SELECT AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END) as acc
                    FROM qd_traces
                    WHERE parent_id IS NULL AND correct IS NOT NULL {chain_filter}
                """, params)
                acc = cur.fetchone()
                if acc and acc['acc'] is not None:
                    result["overall_accuracy"] = round(float(acc['acc']), 3)

            result["ready_for_decision"] = result["evaluated_decisions"] >= 10

    except Exception as e:
        logger.warning("[Store] 获取评估统计失败: %s", e)

    return result
