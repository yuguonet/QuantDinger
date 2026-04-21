"""
选股器路由模块

端点：
  GET  /api/xuangu/            → 选股器概览（最新日期、记录数）
  POST /api/xuangu/sync        → 触发数据同步（从东方财富拉取并写入 PG）
  GET  /api/xuangu/stats       → 表统计信息
  GET  /api/xuangu/favorites   → 收藏策略列表
  POST /api/xuangu/favorites   → 保存/更新收藏策略
  DELETE /api/xuangu/favorites/<id> → 删除收藏策略
  POST /api/xuangu/watchlist   → 添加自选股
  GET  /api/xuangu/watchlist   → 获取自选股列表
  DELETE /api/xuangu/watchlist/<id> → 删除自选股
"""
from flask import Blueprint, request, jsonify, g

from app.utils.db import get_db_connection
from app.utils.auth import get_current_user_id, login_required
from app.utils.logger import get_logger

logger = get_logger(__name__)
xuangu_bp = Blueprint("xuangu", __name__)

# ================================================================
# GET /  — 概览（公开）
# ================================================================
@xuangu_bp.route("/")
def index():
    """选股器概览：最新数据日期和记录数"""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(date) AS latest_date, COUNT(*) AS total "
                "FROM cnstock_selection"
            )
            row = cur.fetchone() or {}
            cur.close()
        return jsonify({
            "code": 0,
            "data": {
                "latest_date": str(row.get("latest_date") or ""),
                "total_records": int(row.get("total") or 0),
            },
        })
    except Exception as e:
        logger.error(f"xuangu index failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e), "data": {}}), 500


# ================================================================
# POST /sync — 触发数据同步
# ================================================================
@xuangu_bp.route("/sync", methods=["POST"])
@login_required
def trigger_sync():
    """触发从东方财富选股器同步数据到 PostgreSQL"""
    data = request.get_json() or {}
    trade_date = (data.get("date") or "").strip() or None
    proxy = (data.get("proxy") or "").strip() or None

    try:
        from app.services.stock_selection_sync import run_sync
        result = run_sync(proxy=proxy, trade_date=trade_date)
        status = 200 if result["success"] else 500
        return jsonify({"code": 0 if result["success"] else 1, "data": result}), status
    except Exception as e:
        logger.error(f"sync failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ================================================================
# GET /stats — 表统计信息（公开）
# ================================================================
@xuangu_bp.route("/stats")
def table_stats():
    """返回 cnstock_selection 表的统计信息"""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT date, COUNT(*) AS cnt "
                "FROM cnstock_selection "
                "GROUP BY date ORDER BY date DESC LIMIT 10"
            )
            date_counts = [dict(r) for r in (cur.fetchall() or [])]

            cur.execute(
                "SELECT COUNT(DISTINCT code) AS stock_count, "
                "MAX(date) AS latest_date, MIN(date) AS earliest_date "
                "FROM cnstock_selection"
            )
            summary = dict(cur.fetchone() or {})
            cur.close()

        return jsonify({"code": 0, "data": {"summary": summary, "by_date": date_counts}})
    except Exception as e:
        logger.error(f"table_stats failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ================================================================
# 收藏策略 CRUD — 每个用户只能操作自己的策略
# ================================================================

def _ensure_strategies_table():
    """确保 qd_user_strategies 表存在，按需建表；已有表自动修正列宽"""
    ddl = """
    CREATE TABLE IF NOT EXISTS qd_user_strategies (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL DEFAULT 0,
        name        VARCHAR(100) NOT NULL,
        conditions  TEXT NOT NULL DEFAULT '',
        description VARCHAR(500) DEFAULT '',
        created_at  TIMESTAMP DEFAULT NOW(),
        updated_at  TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_qdus_user_id
        ON qd_user_strategies (user_id);
    """
    alter_sql = """
    ALTER TABLE qd_user_strategies ALTER COLUMN name TYPE VARCHAR(100);
    ALTER TABLE qd_user_strategies ALTER COLUMN description TYPE VARCHAR(500);
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(ddl)
            try:
                cur.execute(alter_sql)
            except Exception:
                pass
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error(f"_ensure_strategies_table failed: {e}")


@xuangu_bp.route("/favorites", methods=["GET"])
@login_required
def get_favorites():
    """获取当前用户的收藏策略列表"""
    user_id = g.user_id
    _ensure_strategies_table()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, conditions, description, created_at, updated_at "
                "FROM qd_user_strategies "
                "WHERE user_id = %s "
                "ORDER BY updated_at DESC",
                (user_id,)
            )
            rows = cur.fetchall() or []
            cur.close()

        import json as _json
        strategies = []
        for r in rows:
            d = dict(r)
            for ts_field in ("created_at", "updated_at"):
                if d.get(ts_field) and hasattr(d[ts_field], "isoformat"):
                    d[ts_field] = d[ts_field].isoformat()
            try:
                d["conditions"] = _json.loads(d["conditions"]) if d["conditions"] else []
            except (_json.JSONDecodeError, TypeError, ValueError):
                pass
            strategies.append(d)

        return jsonify({"code": 0, "data": strategies, "count": len(strategies)})
    except Exception as e:
        logger.error(f"get_favorites failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e), "data": []}), 500


@xuangu_bp.route("/favorites", methods=["POST"])
@login_required
def save_favorite():
    """保存或更新收藏策略"""
    import json as _json

    data = request.get_json() or {}
    user_id = g.user_id
    strategy_id = data.get("id")
    name = (data.get("name") or "").strip()
    conditions = data.get("conditions")
    description = (data.get("description") or "").strip()

    if not name:
        return jsonify({"code": 1, "msg": "策略名称不能为空"}), 400
    if not conditions:
        return jsonify({"code": 1, "msg": "筛选条件不能为空"}), 400

    cond_str = (
        _json.dumps(conditions, ensure_ascii=False)
        if isinstance(conditions, (dict, list))
        else str(conditions)
    )

    _ensure_strategies_table()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            if strategy_id:
                # 更新模式：校验归属权
                cur.execute(
                    "SELECT user_id FROM qd_user_strategies WHERE id = %s",
                    (strategy_id,)
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"code": 1, "msg": "策略不存在"}), 404
                if int(row["user_id"]) != user_id:
                    return jsonify({"code": 1, "msg": "无权修改他人的策略"}), 403

                cur.execute(
                    "UPDATE qd_user_strategies "
                    "SET name = %s, conditions = %s, description = %s, updated_at = NOW() "
                    "WHERE id = %s AND user_id = %s",
                    (name, cond_str, description, strategy_id, user_id)
                )
                msg = "更新成功"
            else:
                # 新建
                cur.execute(
                    "INSERT INTO qd_user_strategies (user_id, name, conditions, description) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (user_id, name, cond_str, description)
                )
                row = cur.fetchone()
                new_id = row["id"] if row else None
                strategy_id = new_id
                msg = "保存成功"

            conn.commit()
            cur.close()

        return jsonify({"code": 0, "msg": msg, "data": {"id": strategy_id}})
    except Exception as e:
        logger.error(f"save_favorite failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


@xuangu_bp.route("/favorites/<int:strategy_id>", methods=["DELETE"])
@login_required
def delete_favorite(strategy_id: int):
    """删除收藏策略"""
    user_id = g.user_id
    _ensure_strategies_table()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT user_id FROM qd_user_strategies WHERE id = %s",
                (strategy_id,)
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"code": 1, "msg": "策略不存在"}), 404
            if int(row["user_id"]) != user_id:
                return jsonify({"code": 1, "msg": "无权删除他人的策略"}), 403

            cur.execute(
                "DELETE FROM qd_user_strategies WHERE id = %s AND user_id = %s",
                (strategy_id, user_id)
            )
            conn.commit()
            cur.close()

        return jsonify({"code": 0, "msg": "删除成功"})
    except Exception as e:
        logger.error(f"delete_favorite failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ================================================================
# 自选股 CRUD
# ================================================================

def _ensure_watchlist_table():
    """确保自选股表存在"""
    ddl = """
    CREATE TABLE IF NOT EXISTS qd_watchlist (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL DEFAULT 0,
        code        VARCHAR(20) NOT NULL,
        name        VARCHAR(100) NOT NULL DEFAULT '',
        industry    VARCHAR(100) DEFAULT '',
        concept     TEXT DEFAULT '',
        new_price   NUMERIC DEFAULT NULL,
        change_rate NUMERIC DEFAULT NULL,
        created_at  TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, code)
    );
    CREATE INDEX IF NOT EXISTS idx_qdwl_user_id
        ON qd_watchlist (user_id);
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(ddl)
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error(f"_ensure_watchlist_table failed: {e}")


@xuangu_bp.route("/watchlist", methods=["POST"])
@login_required
def add_to_watchlist():
    """添加股票到自选股表"""
    data = request.get_json() or {}
    user_id = g.user_id
    stocks = data.get("stocks") or []
    if not stocks:
        return jsonify({"code": 1, "msg": "未选择任何股票"}), 400
    if len(stocks) > 500:
        return jsonify({"code": 1, "msg": "单次最多添加500只自选股"}), 400

    _ensure_watchlist_table()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            added = 0
            skipped = 0
            for s in stocks:
                code = (s.get("code") or "").strip()
                if not code:
                    skipped += 1
                    continue
                name = (s.get("name") or "")[:100]
                industry = (s.get("industry") or "")[:100]
                try:
                    cur.execute(
                        """INSERT INTO qd_watchlist (user_id, code, name, industry, concept, new_price, change_rate)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (user_id, code) DO UPDATE SET
                               name = EXCLUDED.name,
                               industry = EXCLUDED.industry,
                               concept = EXCLUDED.concept,
                               new_price = EXCLUDED.new_price,
                               change_rate = EXCLUDED.change_rate,
                               created_at = NOW()""",
                        (
                            user_id,
                            code,
                            name,
                            industry,
                            s.get("concept", ""),
                            s.get("new_price"),
                            s.get("change_rate"),
                        )
                    )
                    added += 1
                except Exception as ie:
                    logger.warning(f"watchlist insert skipped code={code}: {ie}")
                    skipped += 1
            conn.commit()
            cur.close()

        return jsonify({"code": 0, "msg": f"成功添加 {added} 只自选股", "data": {"added": added, "skipped": skipped}})
    except Exception as e:
        logger.error(f"add_to_watchlist failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


@xuangu_bp.route("/watchlist", methods=["GET"])
@login_required
def get_watchlist():
    """获取当前用户的自选股列表"""
    user_id = g.user_id
    _ensure_watchlist_table()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, code, name, industry, concept, new_price, change_rate, created_at "
                "FROM qd_watchlist WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,)
            )
            rows = cur.fetchall() or []
            cur.close()

        result = []
        for r in rows:
            d = dict(r)
            if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            for k in ("new_price", "change_rate"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            result.append(d)

        return jsonify({"code": 0, "data": result, "count": len(result)})
    except Exception as e:
        logger.error(f"get_watchlist failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e), "data": []}), 500


@xuangu_bp.route("/watchlist/<int:item_id>", methods=["DELETE"])
@login_required
def remove_from_watchlist(item_id: int):
    """从自选股中删除"""
    user_id = g.user_id
    _ensure_watchlist_table()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM qd_watchlist WHERE id = %s AND user_id = %s",
                (item_id, user_id)
            )
            conn.commit()
            cur.close()

        return jsonify({"code": 0, "msg": "已从自选中移除"})
    except Exception as e:
        logger.error(f"remove_from_watchlist failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500
