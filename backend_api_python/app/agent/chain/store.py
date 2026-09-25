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
import re
from log import logger
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from chain.schema import EvalNode, FactorItem, Layer, Status
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

    # 2026-09-25: 多标的对比 stock_code='600519,000858' 超 varchar(10) → 写库炸
    # （StringDataRightTruncation，决策树整棵丢掉）。应用侧截断 + 库列放宽双保险。
    _code = str(node.stock_code or "")[:32]
    _name = str(node.stock_name or "")[:64]

    if node.id is not None:
        # UPDATE
        cur.execute("""
            UPDATE qd_traces SET
                parent_id=%s, root_id=%s, layer=%s, name=%s, step_order=%s,
                exec_date=%s, stock_code=%s, stock_name=%s,
                score=%s, direction=%s, action=%s, signal=%s, confidence=%s,
                timeframe=%s, factors=%s, output_summary=%s, analysis=%s,
                plan=%s, session_id=%s, user_query=%s, model=%s, total_tokens=%s,
                input_params=%s, tools_called=%s, missing_data=%s, data_source=%s,
                status=%s, error=%s, elapsed_ms=%s,
                exit_date=%s, exit_reason=%s, pnl_pct=%s, hold_days=%s,
                correct=%s, calibration=%s
            WHERE id=%s
            RETURNING id
        """, (
            parent_id, root_id, node.layer, node.name, node.step_order,
            node.exec_date, _code, _name,
            node.score, node.direction, node.action, node.signal, node.confidence,
            node.timeframe, factors_json, output_json, node.analysis,
            node.plan, node.session_id, node.user_query, node.model, node.total_tokens,
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
                plan, session_id, user_query, model, total_tokens,
                input_params, tools_called, missing_data, data_source,
                status, error, elapsed_ms,
                exit_date, exit_reason, pnl_pct, hold_days,
                correct, calibration
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s
            ) RETURNING id
        """, (
            parent_id, root_id, node.layer, node.name, node.step_order,
            node.exec_date, _code, _name,
            node.score, node.direction, node.action, node.signal, node.confidence,
            node.timeframe, factors_json, output_json, node.analysis,
            node.plan, node.session_id, node.user_query, node.model, node.total_tokens,
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

    # 毒丸治理（审计 P0-1 断点C）：
    # 退市/长期停牌股取不到 K 线，evaluator 跳过且不写 exit_date，这些记录每天重新
    # 入选且按 exec_date ASC 永远排队头；LIMIT 槽位被占满后新记录饿死。
    # error 列复用现有字段累计评估失败（'eval_failed:N' 前缀，由 evaluator 写入），
    # 连续失败 >= 5 次置 status='unverifiable' 出队，不新增表结构。
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_traces SET status = 'unverifiable'
                WHERE parent_id IS NULL
                  AND exit_date IS NULL
                  AND status = 'ok'
                  AND exec_date <= %s
                  AND error ~ '^eval_failed:'
                  AND (regexp_match(error, '^eval_failed:(\\d+)'))[1]::int >= 5
            """, (cutoff,))
            # 2026-09-14：stock_code 为空的记录**永不可验证**（K 线恒拒绝"codes 不能
            # 为空"），属永久毒丸——不等 5 次失败计数，直接出队。实测盘后验证对 125+
            # 条空 code 记录逐条刷"K线返回错误: codes 不能为空"却从不计入统计。
            cur.execute("""
                UPDATE qd_traces SET status = 'unverifiable'
                WHERE parent_id IS NULL
                  AND exit_date IS NULL
                  AND status = 'ok'
                  AND COALESCE(stock_code, '') = ''
            """)
            conn.commit()

            cur.execute("""
                SELECT id, exec_date, stock_code, stock_name, name, action, timeframe
                FROM qd_traces
                WHERE parent_id IS NULL
                  AND exit_date IS NULL
                  AND status = 'ok'
                  AND COALESCE(stock_code, '') <> ''
                  AND exec_date <= %s
                  AND NOT COALESCE(human_reviewed, FALSE)  -- [AUDIT-MASK:C2→fix 2026-09-19] 人工判定优先，自动验证不覆盖
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

# [AUDIT-MASK:C1|2026-09-19] 与下方 update_skill_verify 为配对写回（root 层/skill 层），
# 结构相似属分层设计，勿合并。
def update_verify_results(
    root_id: int,
    exit_date: date = None,
    exit_reason: str = "",
    pnl_pct: float = None,
    hold_days: int = None,
    correct: bool = None,
):
    """写入根节点的验证结果。

    【P1 写保护】`correct`/`calibration` **只由本函数与 update_skill_verify 写入**
    （golden test 锁定）；verify_node/claims 结论只进独立字段，不得回写此处。

    2026-09-24（提智三波 T1）：写入定论后顺带回填案例库延迟标签（只读 correct，
    只改 pending 行，fail-open）——案例记忆的 outcome 就在这里从 pending 转正/转误。
    """
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
        return
    # 延迟标签回填（T1）：失败不影响验证主链
    try:
        from utils.case_memory import backfill_by_root
        backfill_by_root(root_id, correct, t_plus_n=pnl_pct)
    except Exception as e:
        logger.warning("[Store] 案例标签回填跳过 root_id=%d: %s", root_id, e)
def update_skill_verify(root_id: int, actual_direction: str):
    """回溯时逐层验证：更新每个 skill 子节点的 correct。

    三值逻辑：
      - neutral 预测 → correct = NULL（不参与统计）
      - correct/wrong → 正常写入
    """
    from chain.schema import is_direction_correct
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
# ═══════════════════════════════════════════════════════════════
# 权重查询
#
# 统一权重表：qd_agent_weights（layer='skill' / layer='factor'）
#
# evaluator.update_skill_weights() 自动同步 registry：
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
def query_latest_root_by_chain(chain_name: str) -> Optional[Dict[str, Any]]:
    """查询某 chain 最近一条根节点 trace（无 stock_code 时使用）。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, exec_date, stock_code, stock_name, name,
                       score, action, direction, confidence, status
                FROM qd_traces
                WHERE parent_id IS NULL AND name = %s
                ORDER BY created_at DESC LIMIT 1
            """, (chain_name,))

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
        logger.error("[Store] 查询最近根节点失败 chain=%s: %s", chain_name, e)
        return None
def mark_root_wrong(root_id: int):
    """轻度惩罚：标记根节点 correct=False + calibration 重校准。

    [AUDIT-MASK:C2|2026-09-19 → fix 2026-09-19] 时序冲突已修：query_pending_verify
    现排除 human_reviewed=TRUE 的行，本函数写入的人工判定不会被自动验证覆盖。
    """
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
def mark_root_good(root_id: int):
    """正面奖励（2026-09-15）：用户认可上一轮编排。
    correct=TRUE 固化 + calibration 上调至上限 1.05 + human_reviewed=TRUE
    （跳过 T+N 自动校准，防止把用户认可的链路改判）→ 下轮 update_weights
    的 win_rate/编排缓存立即受益。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_traces SET
                    correct = TRUE,
                    calibration = 1.05,
                    human_reviewed = TRUE,
                    human_verdict = 'positive_feedback'
                WHERE id = %s AND parent_id IS NULL
            """, (root_id,))
            conn.commit()
            logger.info("[Store] 正面认可 root_id=%d correct=TRUE calibration=1.05", root_id)
    except Exception as e:
        logger.error("[Store] 正面奖励失败 root_id=%d: %s", root_id, e)


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
def get_penalty_count_by_chain(chain_name: str) -> int:
    """统计某 chain 最近 trace 的负面反馈次数（human_verdict='negative_feedback'）。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as cnt FROM qd_traces
                WHERE parent_id IS NULL
                  AND name = %s
                  AND human_verdict = 'negative_feedback'
            """, (chain_name,))
            row = cur.fetchone()
            return row['cnt'] if row else 0
    except Exception as e:
        logger.warning("[Store] 统计惩罚次数失败 chain=%s: %s", chain_name, e)
        return 0
def get_skill_weights() -> Dict[str, float]:
    """从 qd_agent_weights 获取 Skill 权重。"""
    from app.utils.db import get_db_connection
    weights = {}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, weight FROM qd_agent_weights WHERE layer = 'skill'")
            for row in cur.fetchall():
                weights[row["name"]] = row["weight"]
    except Exception as e:
        logger.warning("[Store] 获取 Skill 权重失败: %s", e)
    return weights
def get_tool_weights() -> Dict[str, float]:
    """从 qd_agent_weights 获取工具权重（layer='tool'，2026-09-15 与 skill/factor 同表）。"""
    from app.utils.db import get_db_connection
    weights = {}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, weight FROM qd_agent_weights WHERE layer = 'tool'")
            for row in cur.fetchall():
                weights[row["name"]] = row["weight"]
    except Exception as e:
        logger.warning("[Store] 获取工具权重失败: %s", e)
    return weights


def get_factor_weights(skill_name: str = None) -> Dict[str, float]:
    """从 qd_agent_weights 获取因子权重。"""
    from app.utils.db import get_db_connection
    weights = {}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            if skill_name:
                cur.execute("""
                    SELECT name, weight FROM qd_agent_weights
                    WHERE layer = 'factor' AND skill_name = %s AND sample_count >= 5
                """, (skill_name,))
            else:
                cur.execute("""
                    SELECT name, weight FROM qd_agent_weights
                    WHERE layer = 'factor' AND sample_count >= 5
                """)
            for fname, weight in cur.fetchall():
                weights[fname] = weight
    except Exception as e:
        logger.warning("[Store] 获取因子权重失败: %s", e)
    return weights


def get_chain_weights() -> Dict[str, float]:
    """从 qd_agent_weights 获取链路权重（layer='chain'，2026-09-24 提智 P3/TODO-1）。

    背景（已知局限 G1 的补全）：update_weights 此前只产 skill/factor/tool 三层权重，
    chain 层“只记账不加权”；本函数是 planner 侧的消费入口（与 get_tool_weights 同款
    低权重提示）。写入点在 evaluator.update_weights 的⑦段（同表分层，additive）。
    """
    from app.utils.db import get_db_connection
    weights: Dict[str, float] = {}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, weight FROM qd_agent_weights WHERE layer = 'chain'")
            for name, weight in cur.fetchall():
                weights[name] = weight
            cur.close()
    except Exception as e:
        logger.warning("[Store] 获取链路权重失败: %s", e)
    return weights
# [AUDIT-MASK:B1|2026-09-19] 第④闭环双轨之一：本函数（工具序列参考注入）与 skill_brewer
# （酿造）同源同目的，质量门口径不一致（此处 win_rate>=0.7/MIN_SAMPLES=1；酿造 证伪<=0.3）。
# 等混合触发（重设计稿 §2.5）与迭代旁支稳定后二选一；建议保留酿造，本通道降级 debug 开关。
def query_cached_tools(domain: str, verb: str, noun: str, stock_code: str = None) -> Optional[List[str]]:
    """查询 qd_traces 中已验证的工具序列（编排路径缓存）。

    chain_name 格式: domain+verb+noun（如 finance+analyze+stock）。
    聚合同一工具序列的多条执行记录，取 win_rate 最高且 return_per_day 最优的。

    质量门：
      1. 样本数 >= MIN_SAMPLES
      2. win_rate >= MIN_WIN_RATE
      3. 工具步数 <= MAX_STEPS
      4. 无子节点 failed
    """
    from app.utils.db import get_db_connection

    if not verb or not noun:
        return None

    MAX_STEPS = 6         # 单轮最大步数

    chain_name = f"{domain}+{verb}+{noun}" if domain else f"{verb}+{noun}"

    def _query(cur, extra_where: str, params: tuple) -> Optional[list]:
        """聚合查询：按 tools_called 分组，取最优链路。"""
        # 2026-09-18：3 → 1（用户方案"第一次顺利完成后下一次直接用"）。
        # 工具序列天然分散（同一意图不同 run 组合不同），3 样本门槛意味着永远等不到；
        # 参考注入 fail-open 且非强制，1 样本即可注入；回测证伪由 correct 门兜底。
        MIN_SAMPLES = 1         # 最小样本数
        MIN_WIN_RATE = 0.7      # 最小胜率
        
        cur.execute(f"""
            SELECT
                t.tools_called,
                COUNT(*) as executions,
                AVG(CASE WHEN t.correct THEN 1.0 ELSE 0.0 END) as win_rate,
                AVG(t.pnl_pct) as avg_pnl,
                AVG(NULLIF(t.hold_days, 0)) as avg_hold_days
            FROM qd_traces t
            WHERE t.layer = 'chain'
              AND t.name = %s
              AND t.status = 'ok'
              AND t.tools_called IS NOT NULL
              AND array_length(t.tools_called, 1) BETWEEN 1 AND %s
              {extra_where}
              AND NOT EXISTS (
                  SELECT 1 FROM qd_traces child
                  WHERE child.root_id = t.id
                    AND child.status = 'failed'
              )
            GROUP BY t.tools_called
            HAVING COUNT(*) >= %s
               AND (AVG(CASE WHEN t.correct THEN 1.0 ELSE 0.0 END) >= %s
                    -- 未校准（correct 全 NULL，如 query/chat 类无方向预测的任务）视为
                    -- 中性可用——只要没有一条被回测证伪（correct=FALSE）即可入选。
                    -- 旧实现把全 NULL 的链一律拒之门外，query 类编排缓存因此永不命中。
                    OR NOT BOOL_OR(t.correct IS FALSE))
            ORDER BY COALESCE(AVG(t.pnl_pct), 0) / COALESCE(NULLIF(AVG(NULLIF(t.hold_days, 0)), 0), 1) DESC
            LIMIT 1
        """, (chain_name, MAX_STEPS) + params + (MIN_SAMPLES, MIN_WIN_RATE))
        row = cur.fetchone()
        if row:
            tools = row['tools_called']
            logger.info("[Store] 缓存命中: %s tools=%s win_rate=%.2f pnl=%.2f",
                        chain_name, tools, row['win_rate'], row['avg_pnl'] or 0)
            return tools if isinstance(tools, list) else list(tools)
        return None

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 精确匹配 stock_code
            if stock_code:
                result = _query(cur, "AND t.stock_code = %s", (stock_code,))
                if result:
                    return result
                # 有具体股票但无缓存 → 不降级到全局（语义不同）
                return None

            # 无 stock_code → 取全局最优
            return _query(cur, "", ())

    except Exception as e:
        logger.warning("[Store] 查询缓存工具链失败 %s: %s", chain_name, e)
        return None

def get_brew_states() -> list:
    """读取全部酿造状态（重设计 §2.5；layer='brew_state' 行）。

    返回 [{chain_name, last_brew_date(date), fail_streak(int)}]。
    weight 列复用为 fail_streak 存储（整数语义，无需新表）。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT name, last_updated, weight
                FROM qd_agent_weights
                WHERE layer = 'brew_state'
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "chain_name": r["name"],
                    "last_brew_date": r["last_updated"].date() if r["last_updated"] else None,
                    "fail_streak": int(r["weight"] or 0),
                })
            cur.close()
            return rows
    except Exception as e:
        logger.warning("[Store] 酿造状态读取失败: %s", e)
        return []


def set_brew_state(chain_name: str, last_brew_date=None, fail_streak: int = 0) -> None:
    """写/更新一条酿造状态（幂等 upsert）。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_weights
                    (layer, name, skill_name, weight, sample_count, last_updated)
                VALUES ('brew_state', %s, NULL, %s, 0, %s)
                ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                DO UPDATE SET
                    weight = EXCLUDED.weight,
                    last_updated = EXCLUDED.last_updated
            """, (chain_name, fail_streak, last_brew_date))
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error("[Store] 酿造状态写入失败 chain=%s: %s", chain_name, e)


# ── 酿造触发阈值（重设计 §2.5；单一事实源，消费方：skill_brewer）──
BREW_MIN_SIGNAL = 3.0        # 信号分下限 ≈ 3 次验证正确
BREW_WIN_RATE_FLOOR = 0.7    # 正确率闸门
BREW_CONFIDENCE_CAP = 10     # 置信缩放分母（verified 达此值后不再增益）


def query_brew_ready(min_signal: float = BREW_MIN_SIGNAL, limit: int = 5) -> list:
    """信号就绪的酿造候选（重设计 §2.5 触发策略 v2，2026-09-19）。

    与 query_brew_candidates 的区别：本函数按**合成信号分**排序与过滤——
      signal = verified_correct × win_rate × min(1, verified/BREW_CONFIDENCE_CAP)
    - verified_correct 只数 correct=TRUE（NULL 不计，与 §7.18.6 语义一致）；
    - win_rate < BREW_WIN_RATE_FLOOR 的链直接拒绝（坏编排不固化）；
    - 置信缩放防"一夜爆量"（同市况样本不泛化）。
    不含 unknown 链、要求可回测标的（与 query_brew_candidates 同口径）。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT t.name AS chain_name,
                       COUNT(*) AS runs,
                       COUNT(*) FILTER (WHERE t.correct IS FALSE) AS falsified,
                       COUNT(*) FILTER (WHERE t.correct) AS verified_correct,
                       COUNT(*) FILTER (WHERE t.correct IS NOT NULL) AS verified_total,
                       MAX(t.exec_date) AS last_date,
                       MIN(t.id) AS sample_root_id
                FROM qd_traces t
                WHERE t.layer = 'chain' AND t.status = 'ok'
                  AND t.stock_code IS NOT NULL AND t.stock_code <> ''
                  AND position('unknown' in t.name) = 0
                GROUP BY t.name
                HAVING COUNT(*) FILTER (WHERE t.correct) >= 1
                   AND (COUNT(*) FILTER (WHERE t.correct))::float
                       / GREATEST(COUNT(*) FILTER (WHERE t.correct IS NOT NULL), 1)
                       >= %s
                ORDER BY runs DESC
                LIMIT %s
            """, (BREW_WIN_RATE_FLOOR, limit))
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            out = []
            for r in rows:
                wr = (r["verified_correct"] / r["verified_total"]) if r["verified_total"] else 0.0
                confidence = min(1.0, r["verified_correct"] / BREW_CONFIDENCE_CAP)
                r["win_rate"] = round(wr, 4)
                r["signal"] = round(r["verified_correct"] * wr * confidence, 3)
                if r["signal"] >= min_signal:
                    out.append(r)
            out.sort(key=lambda x: -x["signal"])
            return out[:limit]
    except Exception as e:
        logger.warning("[Store] 信号候选查询失败: %s", e)
        return []


def query_brew_candidates(min_runs: int = 5, max_falsified_ratio: float = 0.3,
                          min_days_span: int = 0, limit: int = 5) -> list:
    """技能酿造候选（2026-09-18，用户方案：高频且验证效果好的节点树 → 酿成 Skill）。

    按 chain_name 聚合 qd_traces 根节点，筛选：
      - runs >= min_runs（高频：问得多的链才值得固化）；
      - 被回测证伪（correct=FALSE）占比 <= max_falsified_ratio（效果好：未被证伪/证伪少）；
      - 跨天数 >= min_days_span（0 = 不要求；用户提的"7 天周期"由调用方按 last_date 控制）。
    返回候选列表（含代表 run 的 root_id，供酿造器拉节点树明细），按 runs 降序。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT t.name AS chain_name,
                       COUNT(*) AS runs,
                       COUNT(*) FILTER (WHERE t.correct IS FALSE) AS falsified,
                       COUNT(*) FILTER (WHERE t.correct) AS verified_correct,
                       COUNT(DISTINCT t.exec_date) AS days_span,
                       MAX(t.exec_date) AS last_date,
                       MIN(t.id) AS sample_root_id
                FROM qd_traces t
                WHERE t.layer = 'chain' AND t.status = 'ok'
                  AND t.stock_code IS NOT NULL AND t.stock_code <> ''
                  AND position('unknown' in t.name) = 0   -- 不可归类链无酿造价值（LIKE 通配符会撞 psycopg2 占位符解析）
                GROUP BY t.name
                HAVING COUNT(*) >= %s
                   AND (COUNT(*) FILTER (WHERE t.correct IS FALSE))::float
                       / GREATEST(COUNT(*) FILTER (WHERE t.correct IS NOT NULL), 1)
                       <= %s
                   AND COUNT(DISTINCT t.exec_date) >= %s
                ORDER BY runs DESC
                LIMIT %s
            """, (min_runs, max_falsified_ratio, min_days_span, limit))
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("[Store] 酿造候选查询失败: %s", e)
        return []

def get_run_tree_digest(root_id: int, max_children: int = 12) -> Optional[dict]:
    """取一条 run 的节点树摘要（酿造原料）：task/plan + 各步骤的执行日志摘要。

    注意 tools_called 里记的是 python_interpreter 占位（CodeAgent 形态），真实步骤
    线索在 output_summary 的 Execution logs 里——原样交给 LLM 编译，由它从日志推断
    每步做了什么、用了什么工具。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT name, user_query, plan, tools_called FROM qd_traces WHERE id = %s
            """, (root_id,))
            root = cur.fetchone()
            if not root:
                cur.close()
                return None
            cur.execute("""
                SELECT name, status, output_summary FROM qd_traces
                WHERE root_id = %s AND layer <> 'chain' ORDER BY id LIMIT %s
            """, (root_id, max_children))
            steps = []
            for r in cur.fetchall():
                os_ = r["output_summary"]
                if isinstance(os_, dict):
                    txt = str(os_.get("result") or os_)[:400]
                else:
                    txt = str(os_)[:400]
                steps.append({"step": r["name"], "status": r["status"], "log": txt})
            cur.close()
            return {
                "chain_name": root["name"],
                "user_query": root["user_query"] or "",
                "plan": (root["plan"] or "")[:1500],
                "tools_called": root["tools_called"] or [],
                "steps": steps,
            }
    except Exception as e:
        logger.warning("[Store] 节点树摘要查询失败 root=%s: %s", root_id, e)
        return None

# [AUDIT-MASK:B2|2026-09-19] 死代码实锤：仅定义+chain/__init__ 导出，全 backend 零生产调用方
# （现役通道 = get_tool_weights + 调用方 <0.7 过滤）。统一清理阶段连同导出项删除。
def query_low_weight_tools(min_appearances: int = 5, max_win_rate: float = 0.4) -> set:
    """聚合 qd_traces，返回低权重工具集合。

    工具出现次数 >= min_appearances 且所在链路 win_rate < max_win_rate → 低权重。
    结果缓存 10 分钟，避免每次调用都聚合。
    """
    from app.utils.db import get_db_connection
    import time

    # 简单内存缓存
    cache_key = f"{min_appearances}_{max_win_rate}"
    if not hasattr(query_low_weight_tools, '_cache'):
        query_low_weight_tools._cache = {}
    cached = query_low_weight_tools._cache.get(cache_key)
    if cached and time.time() - cached[1] < 600:
        return cached[0]

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
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
            result = {row['name'] for row in cur.fetchall()}
            query_low_weight_tools._cache[cache_key] = (result, time.time())
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


def get_delta_digest(chain_name: str, since_date=None, since_root_id: int = None,
                     max_children: int = 20) -> Optional[dict]:
    """增量轨迹摘要（迭代修订原料，重设计 §2.6）。

    取该链自 since_date / since_root_id 以来（含 correct 两态）的 run 树摘要：
      - correct=TRUE 的 run → 供修订器提炼步骤/参数改进；
      - correct=FALSE 的 run → 供修订器把坑写进「注意事项」。
    都不传时取全部（与首酿原料同源）。
    since_root_id（2026-09-24，审计 A4）：修订原料按 SKILL.md 头部 `from root_id=NNN`
    截增量——旧实现调用方解析了 since_id 却零引用，每轮把全量历史反复喂给修订器
    （护栏 1「增量轨迹」名存实亡，重复强化/漂移）。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            conds = []
            params = [chain_name]
            if since_date:
                conds.append("AND t.exec_date >= %s")
                params.append(since_date)
            if since_root_id:
                conds.append("AND t.id > %s")
                params.append(since_root_id)
            extra = " ".join(conds)
            params.append(max_children)
            cur.execute(f"""
                SELECT t.id, t.user_query, t.plan, t.correct, t.exec_date,
                       t.output_summary
                FROM qd_traces t
                WHERE t.layer = 'chain' AND t.name = %s AND t.status = 'ok'
                  AND t.stock_code IS NOT NULL AND t.stock_code <> ''
                  {extra}
                ORDER BY t.exec_date DESC
                LIMIT %s
            """, params)
            runs = []
            for r in cur.fetchall():
                os_ = r["output_summary"]
                txt = str(os_.get("result") or os_)[:400] if isinstance(os_, dict) else str(os_)[:400]
                runs.append({
                    "root_id": r["id"],
                    "user_query": r["user_query"] or "",
                    "plan": (r["plan"] or "")[:1200],
                    "correct": r["correct"],
                    "exec_date": str(r["exec_date"]),
                    "summary": txt,
                })
            cur.close()
            return {
                "chain_name": chain_name,
                "runs": runs,
                "n_correct": sum(1 for x in runs if x["correct"] is True),
                "n_falsified": sum(1 for x in runs if x["correct"] is False),
            }
    except Exception as e:
        logger.warning("[Store] 增量轨迹摘要失败 chain=%s: %s", chain_name, e)
        return None


# 修订状态行前缀（2026-09-24 提智 0.3「拆行」方案，免 DDL）：修订状态独立存
# `layer='brew_state', name='revise:<skill_name>'` 行，与酿造节拍行（name=<chain>）彻底分开。
# 列语义单一（禁一列两用，审计 A2 的病根）：weight=low_streak、sample_count=revision、
# last_updated=修订时刻（评价过滤用）。键用 **skill_name**（与 layer='skill' 权重行同键）。
# 历史数据不迁移：旧实现把 revision/low_streak 塞进酿造行与 fail_streak 互踩（A2）、
# 且每轮被 maybe_revise 归零（A3）——旧值不可信，从零重新积累。
_REVISE_PREFIX = "revise:"


def get_skill_revision(skill_name: str) -> dict:
    """读取单个技能的修订状态（重设计 §2.6 护栏 5 用）。"""
    return get_skill_revisions().get(
        skill_name, {"revision": 0, "low_streak": 0, "revised_at": None})


def get_skill_revisions() -> Dict[str, dict]:
    """全部技能修订状态 → {skill_name: {revision, low_streak, revised_at}}。

    revised_at = 修订时刻（date）：evaluator 用它把评价样本截到**新版本产出**的 run
    （护栏 2「新版本重新积累」的另一半，2026-09-24）。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT name, weight, sample_count, last_updated
                FROM qd_agent_weights
                WHERE layer = 'brew_state' AND name LIKE %s
            """, (_REVISE_PREFIX + "%",))
            out = {}
            for r in cur.fetchall():
                out[r["name"][len(_REVISE_PREFIX):]] = {
                    "revision": int(r["sample_count"] or 0),
                    "low_streak": int(r["weight"] or 0),
                    "revised_at": r["last_updated"].date() if r["last_updated"] else None,
                }
            cur.close()
            return out
    except Exception as e:
        logger.warning("[Store] 修订状态读取失败: %s", e)
        return {}


def set_skill_revision(skill_name: str, revision: int, low_streak: int) -> None:
    """写修订状态（**修订事件**调用）：last_updated=修订时刻。

    判定事件（只改 low_streak）请用 set_skill_low_streak——本函数会刷新 last_updated，
    判定事件用它会让旧版本样本混进新评价（一列两用，禁）。
    """
    from app.utils.db import get_db_connection
    from datetime import date as _d
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_weights
                    (layer, name, skill_name, weight, sample_count, last_updated)
                VALUES ('brew_state', %s, NULL, %s, %s, %s)
                ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                DO UPDATE SET
                    weight = EXCLUDED.weight,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = EXCLUDED.last_updated
            """, (_REVISE_PREFIX + skill_name, low_streak, revision, _d.today()))
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error("[Store] 修订状态写入失败 skill=%s: %s", skill_name, e)


def set_skill_low_streak(skill_name: str, low_streak: int) -> None:
    """只更新 low_streak（**判定事件**调用）：不动 sample_count/last_updated。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_weights
                    (layer, name, skill_name, weight, sample_count)
                VALUES ('brew_state', %s, NULL, %s, 0)
                ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                DO UPDATE SET weight = EXCLUDED.weight
            """, (_REVISE_PREFIX + skill_name, low_streak))
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error("[Store] low_streak 写入失败 skill=%s: %s", skill_name, e)


def reset_skill_weight(skill_name: str) -> None:
    """护栏 2 真落地（2026-09-24，设计 §3.17）：修订完成 → 权重重置 1.0、sample_count 归零
    （新版本重新积累）。旧实现只有注释/文档承诺、无写入点（“声明了但没接线”）。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_agent_weights
                SET weight = 1.0, sample_count = 0
                WHERE layer = 'skill' AND name = %s
            """, (skill_name,))
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error("[Store] 权重重置失败 skill=%s: %s", skill_name, e)


def get_skill_weight_rows() -> Dict[str, dict]:
    """skill 层权重行 → {name: {weight, sample_count}}（maybe_revise 判定“带最小样本”用）。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, weight, sample_count FROM qd_agent_weights WHERE layer = 'skill'")
            out = {r["name"]: {"weight": r["weight"], "sample_count": int(r["sample_count"] or 0)}
                   for r in cur.fetchall()}
            cur.close()
            return out
    except Exception as e:
        logger.warning("[Store] skill 权重行读取失败: %s", e)
        return {}


# ── 酿造/修订并发锁（2026-09-24 提智 0.4；PG advisory lock 的免连接替代）──
# eval worker 与手动入口（python -m ...skill_brewer）是两个进程，可能同时酿/修同一
# SKILL.md 与状态行。租约行（layer='run_lock'）原子抢占，TTL 过期自愈（进程崩溃不留死锁）。
def acquire_run_lock(lock_name: str, ttl_seconds: int = 1800) -> bool:
    """抢占式租约锁：成功返回 True；他人持有且未过期返回 False。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_weights
                    (layer, name, skill_name, weight, sample_count, last_updated)
                VALUES ('run_lock', %s, NULL, 0, 0, NOW())
                ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                DO UPDATE SET last_updated = NOW()
                  WHERE qd_agent_weights.last_updated
                        < NOW() - (%s || ' seconds')::interval
                RETURNING name
            """, (lock_name, str(int(ttl_seconds))))
            got = cur.fetchone() is not None
            conn.commit()
            cur.close()
            return got
    except Exception as e:
        logger.warning("[Store] 运行锁获取失败（保守放行）: %s", e)
        return True   # 锁基础设施故障不阻断酿造（宁松勿卡，但留痕）


def release_run_lock(lock_name: str) -> None:
    """释放租约锁（删行；幂等）。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_agent_weights WHERE layer = 'run_lock' AND name = %s",
                        (lock_name,))
            conn.commit()
            cur.close()
    except Exception as e:
        logger.warning("[Store] 运行锁释放失败（TTL 会自愈）: %s", e)
