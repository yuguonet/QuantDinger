"""
选股器路由模块 — 东方财富选股数据 + 自然语言查询

端点：
  GET  /api/xuangu/            → 选股器概览（最新日期、记录数）
  POST /api/xuangu/search      → 自然语言搜索选股数据
  POST /api/xuangu/query       → 结构化筛选查询（字段+条件+排序+分页）
  POST /api/xuangu/sync        → 触发数据同步（从东方财富拉取并写入 PG）
  GET  /api/xuangu/stats       → 表统计信息
  GET  /api/xuangu/favorites   → 当前用户的收藏策略列表（需登录）
  POST /api/xuangu/favorites   → 保存/更新收藏策略（需登录）
  DELETE /api/xuangu/favorites/<id> → 删除收藏策略（需登录，仅限本人）
"""
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Blueprint, request, jsonify, g

from app.utils.db import get_db_connection
from app.utils.StockNLQ import EnterpriseStockNLQ
from app.utils.auth import login_required, get_current_user_id
from app.utils.logger import get_logger

logger = get_logger(__name__)
xuangu_bp = Blueprint("xuangu", __name__)

# ── 白名单：允许前端排序的列 ──
ALLOWED_SORT_FIELDS = {
    "code", "name", "new_price", "change_rate", "volume_ratio",
    "turnoverrate", "industry", "area", "pe9", "pbnewmrq",
    "total_market_cap", "free_cap", "roe_weight", "sale_gpr",
    "netprofit_yoy_ratio", "debt_asset_ratio", "net_inflow",
    "volume", "deal_amount", "amplitude", "popularity_rank",
}


# ================================================================
# GET /  — 概览
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
# POST /search — 自然语言搜索
# ================================================================
@xuangu_bp.route("/search", methods=["POST"])
def search_stocks():
    """选股搜索 — 兼容两种请求格式

    格式 A — 前端结构化筛选（旧版兼容）:
    {
        "query": "WHERE pe9 >= :pe_min AND industry IN (:industry0)",
        "params": {"pe_min": 10, "industry0": "银行"},
        "page": 1, "limit": 20,
        "sort_by": "code", "order": "asc"
    }

    格式 B — 自然语言搜索:
    {
        "keyword": "市盈率低于20的科技股"
    }
    """
    data = request.get_json() or {}

    # ── 格式 B：自然语言 ──
    keyword = (data.get("keyword") or "").strip()
    if keyword:
        try:
            nlq = EnterpriseStockNLQ()
            result = nlq.query(keyword, data.get("format", "json"))
            if result.get("success"):
                return jsonify({
                    "code": 0, "msg": "Success",
                    "count": result.get("record_count", 0),
                    "data": result.get("result", []),
                })
            return jsonify({"code": 0, "msg": "No results", "count": 0, "data": []})
        except Exception as e:
            logger.error(f"search_stocks NLQ failed: {e}", exc_info=True)
            return jsonify({"code": 1, "msg": f"查询失败: {e}", "data": []}), 500

    # ── 格式 A：前端结构化 WHERE 子句 ──
    where_clause = (data.get("query") or "").strip()
    params_obj = data.get("params") or {}
    trade_date = (data.get("date") or "").strip()
    try:
        page = max(int(data.get("page") or 1), 1)
    except (ValueError, TypeError):
        page = 1
    try:
        limit = min(int(data.get("limit") or 20), 200)
    except (ValueError, TypeError):
        limit = 20
    sort_by = (data.get("sort_by") or "code").strip()
    sort_order = (data.get("order") or "ASC").upper()

    if sort_by not in ALLOWED_SORT_FIELDS:
        sort_by = "code"
    if sort_order not in ("ASC", "DESC"):
        sort_order = "ASC"

    # ── 日期处理：指定日期则查该日，否则查最新 ──
    if trade_date:
        # 检查数据库中是否有该日数据
        date_exists = False
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM cnstock_selection WHERE date = %s LIMIT 1",
                    (trade_date,)
                )
                date_exists = cur.fetchone() is not None
                cur.close()
        except Exception as e:
            logger.error(f"检查日期存在性失败: {e}")
            # 数据库异常不应触发同步，直接返回错误
            return jsonify({"code": 1, "msg": "数据库查询失败", "data": []}), 500

        # 没有则自动从东方财富同步（带超时保护）
        if not date_exists:
            logger.info(f"数据库无 {trade_date} 数据，触发自动同步...")
            try:
                from app.services.stock_selection_sync import run_sync
                sync_result = run_sync(trade_date=trade_date)
                if sync_result.get("written", 0) > 0:
                    logger.info(f"自动同步完成: {sync_result['written']} 条")
                else:
                    logger.warning(f"自动同步未获取到 {trade_date} 数据: {sync_result.get('error', 'unknown')}")
                    return jsonify({
                        "code": 0, "msg": f"该日期暂无数据", "count": 0, "data": []
                    })
            except Exception as e:
                logger.error(f"自动同步失败: {e}", exc_info=True)
                return jsonify({"code": 1, "msg": f"数据同步失败: {e}", "data": []}), 502

    if not where_clause:
        if trade_date:
            where_sql = "date = %s"
            pg_params: list = [trade_date]
        else:
            where_sql = "date = (SELECT MAX(date) FROM cnstock_selection)"
            pg_params = []
    else:
        # 解析前端 WHERE 子句 → 参数化 PostgreSQL SQL
        where_sql, pg_params = _parse_where_clause(where_clause, params_obj)
        if where_sql is None:
            return jsonify({"code": 1, "msg": "Invalid query conditions", "data": []}), 400
        # 追加日期条件
        if trade_date:
            where_sql = f"date = %s AND ({where_sql})"
            pg_params = [trade_date] + pg_params

    offset = (page - 1) * limit
    count_sql = f"SELECT COUNT(*) AS cnt FROM cnstock_selection WHERE {where_sql}"
    data_sql = (
        f"SELECT * FROM cnstock_selection "
        f"WHERE {where_sql} "
        f"ORDER BY {sort_by} {sort_order} "
        f"LIMIT %s OFFSET %s"
    )

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(count_sql, pg_params)
            total = int((cur.fetchone() or {}).get("cnt") or 0)

            cur.execute(data_sql, pg_params + [limit, offset])
            rows = cur.fetchall() or []
            cur.close()

        return jsonify({
            "code": 0, "msg": "Success",
            "count": total, "page": page, "limit": limit,
            "data": [dict(r) for r in rows],
        })
    except Exception as e:
        logger.error(f"search_stocks structured failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": f"查询失败: {e}", "data": []}), 500


def _parse_where_clause(where_clause: str, params: Dict[str, Any]):
    """安全解析前端 WHERE 子句，转为参数化 PostgreSQL 查询

    将前端的 `:param_name` 占位符替换为 `%s`，同时校验字段名白名单。
    返回 (where_sql, pg_params_list) 或 (None, []) 如果检测到危险内容。
    """
    import re as _re

    # 去掉前导 WHERE/WHERE
    sql = _re.sub(r"^\s*WHERE\s+", "", where_clause, flags=_re.IGNORECASE)

    # 白名单：允许出现在 WHERE 中的列名
    allowed_cols = {
        "code", "name", "secucode", "industry", "area", "concept", "style",
        "new_price", "change_rate", "volume_ratio", "turnoverrate",
        "high_price", "low_price", "pre_close_price", "amplitude",
        "volume", "deal_amount", "pe9", "pbnewmrq", "pettmdeducted",
        "ps9", "pcfjyxjl9", "total_market_cap", "free_cap", "dtsyl",
        "basic_eps", "bvps", "per_netcash_operate", "per_fcfe",
        "parent_netprofit", "deduct_netprofit", "total_operate_income",
        "roe_weight", "jroa", "roic", "sale_gpr", "sale_npr",
        "netprofit_yoy_ratio", "deduct_netprofit_growthrate",
        "toi_yoy_ratio", "netprofit_growthrate_3y", "income_growthrate_3y",
        "basiceps_yoy_ratio", "total_profit_growthrate", "operate_profit_growthrate",
        "debt_asset_ratio", "equity_ratio", "equity_multiplier",
        "current_ratio", "speed_ratio", "total_shares", "free_shares",
        "holder_newest", "holder_ratio", "hold_amount", "avg_hold_num",
        "holdnum_growthrate_3q", "holdnum_growthrate_hy",
        "hold_ratio_count", "free_hold_ratio",
        "net_inflow", "netinflow_3days", "netinflow_5days",
        "ddx", "ddx_3d", "ddx_5d",
        "changerate_3days", "changerate_5days", "changerate_10days", "changerate_ty",
        "upnday", "downnday", "popularity_rank",
        "is_hs300", "is_sz50", "is_zz500", "is_zz1000", "is_cy50",
        "macd_golden_fork", "macd_golden_forkz", "macd_golden_forky",
        "kdj_golden_fork", "kdj_golden_forkz", "kdj_golden_forky",
        "break_through", "low_funds_inflow", "high_funds_outflow",
        "breakup_ma_5days", "breakup_ma_10days", "breakup_ma_20days",
        "breakup_ma_30days", "breakup_ma_60days",
        "long_avg_array", "short_avg_array",
        "upper_large_volume", "down_narrow_volume",
        "one_dayang_line", "two_dayang_lines", "rise_sun", "power_fulgun",
        "down_7days", "upper_8days", "upper_9days", "upper_4days",
        "heaven_rule", "upside_volume", "bearish_engulfing",
        "reversing_hammer", "shooting_star", "evening_star",
        "first_dawn", "pregnant", "black_cloud_tops", "morning_star",
        "narrow_finish", "is_issue_break", "is_bps_break",
        "now_newhigh", "now_newlow",
        "high_recent_3days", "high_recent_5days", "high_recent_10days",
        "high_recent_20days", "high_recent_30days",
        "low_recent_3days", "low_recent_5days", "low_recent_10days",
        "low_recent_20days", "low_recent_30days",
        "win_market_3days", "win_market_5days", "win_market_10days",
        "win_market_20days", "win_market_30days",
        "limited_lift_6m", "limited_lift_1y", "limited_lift_f6m", "limited_lift_f1y",
        "directional_seo_1m", "directional_seo_3m", "directional_seo_6m", "directional_seo_1y",
        "recapitalize_1m", "recapitalize_3m", "recapitalize_6m", "recapitalize_1y",
        "equity_pledge_1m", "equity_pledge_3m", "equity_pledge_6m", "equity_pledge_1y",
        "pledge_ratio", "goodwill_scale", "par_dividend", "par_dividend_pretax",
        "org_survey_3m", "org_rating",
        "allcorp_ratio", "allcorp_fund_ratio", "allcorp_qs_ratio",
        "allcorp_qfii_ratio", "allcorp_bx_ratio", "allcorp_sb_ratio",
        "holder_change_3m", "executive_change_3m",
        "per_unassign_profit", "per_surplus_reserve", "per_retained_earning",
        "predict_netprofit_ratio", "predict_income_ratio",
        "listing_yield_year", "listing_volatility_year",
        "mutual_netbuy_amt", "hold_ratio",
    }

    # 安全检查：拒绝 SQL 关键字
    dangerous = [";--", "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE",
                 "EXEC", "EXECUTE", "xp_", "UNION SELECT", "INFORMATION_SCHEMA"]
    upper_sql = sql.upper()
    for d in dangerous:
        if d in upper_sql:
            logger.warning(f"Blocked dangerous SQL keyword in WHERE: {d}")
            return None, []

    # 提取所有 :param_name 占位符，按出现顺序收集
    param_names = _re.findall(r":(\w+)", sql)

    # 检查每个 :param 是否在 params 中
    for pname in param_names:
        if pname not in params:
            logger.warning(f"WHERE clause references :{pname} but params has no such key")
            return None, []  # 参数缺失 → 拒绝查询，而非注入 NULL

    # 逐个替换 :param → %s
    pg_params = []
    result_sql = sql
    for pname in param_names:
        result_sql = result_sql.replace(f":{pname}", "%s", 1)
        pg_params.append(params.get(pname))

    # PostgreSQL LIKE → ILIKE（大小写不敏感）
    result_sql = _re.sub(r'\bLIKE\b', 'ILIKE', result_sql, flags=_re.IGNORECASE)

    # 修复前端双引号字符串 → PostgreSQL 单引号
    # 前端发送 `is_hs300 = "Y"` → PG 需要 `is_hs300 = 'Y'`
    result_sql = _re.sub(r'"([^"]*)"', r"'\1'", result_sql)

    # 校验字段名：从 SQL 中提取所有可能的列名 token
    # 列名出现在 AND/OR/(/ 前面，或者在 WHERE 子句开头
    tokens = _re.split(r"\s+(?:AND|OR)\s+|\s*[\(\)]\s*", result_sql, flags=_re.IGNORECASE)
    for token in tokens:
        # 提取字段名（第一个单词）
        col_match = _re.match(r"(\w+)", token.strip())
        if col_match:
            col = col_match.group(1).lower()
            # 跳过 SQL 关键字
            if col.upper() in ("SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN",
                               "LIKE", "BETWEEN", "IS", "NULL", "TRUE", "FALSE",
                               "COUNT", "MAX", "MIN", "ORDER", "BY", "LIMIT",
                               "OFFSET", "EXISTS", "ANY", "ALL", "SOME",
                               "ILIKE", "ASC", "DESC"):
                continue
            if col not in allowed_cols:
                logger.warning(f"Unknown column in WHERE: {col}")
                # 不直接拒绝，但记录

    return result_sql, pg_params


# ================================================================
# POST /query — 结构化筛选查询
# ================================================================
@xuangu_bp.route("/query", methods=["POST"])
def query_stocks():
    """结构化筛选：前端传入 JSON 条件，后端参数化执行

    请求体示例：
    {
        "date": "2026-04-19",           // 可选，默认最新日期
        "conditions": [                  // AND 条件列表
            {"field": "industry", "op": "=", "value": "银行"},
            {"field": "pe9", "op": ">", "value": 0},
            {"field": "pe9", "op": "<", "value": 15},
            {"field": "change_rate", "op": ">=", "value": 0}
        ],
        "sort_by": "change_rate",        // 排序字段
        "sort_order": "DESC",            // ASC / DESC
        "page": 1,
        "limit": 20
    }
    """
    data = request.get_json() or {}
    conditions: List[Dict[str, Any]] = data.get("conditions") or []
    sort_by = (data.get("sort_by") or "code").strip()
    sort_order = (data.get("sort_order") or "ASC").upper()
    try:
        page = max(int(data.get("page") or 1), 1)
    except (ValueError, TypeError):
        page = 1
    try:
        limit = min(int(data.get("limit") or 20), 200)
    except (ValueError, TypeError):
        limit = 20
    trade_date = (data.get("date") or "").strip()

    # 安全校验
    if sort_by not in ALLOWED_SORT_FIELDS:
        sort_by = "code"
    if sort_order not in ("ASC", "DESC"):
        sort_order = "ASC"

    # 构建参数化 SQL
    where_parts: List[str] = []
    params: List[Any] = []

    if trade_date:
        where_parts.append("date = %s")
        params.append(trade_date)
    else:
        # 默认查最新日期
        where_parts.append("date = (SELECT MAX(date) FROM cnstock_selection)")

    for cond in conditions:
        field = (cond.get("field") or "").strip()
        op = (cond.get("op") or "=").strip()
        value = cond.get("value")

        # 字段白名单校验（防 SQL 注入）
        if field not in ALLOWED_SORT_FIELDS and field not in {
            "code", "secucode", "listing_date", "is_hs300", "is_sz50",
            "is_zz500", "is_zz1000", "is_cy50", "macd_golden_fork",
            "kdj_golden_fork", "break_through", "low_funds_inflow",
            "high_funds_outflow", "breakup_ma_5days", "breakup_ma_10days",
            "breakup_ma_20days", "breakup_ma_60days", "long_avg_array",
            "short_avg_array", "sale_npr", "current_ratio", "speed_ratio",
            "holder_newest", "netprofit_growthrate_3y", "income_growthrate_3y",
            "ddx", "changerate_3days", "changerate_5days", "changerate_10days",
            "now_newhigh", "now_newlow", "is_bps_break",
        }:
            logger.warning(f"未知筛选字段: {field}，已忽略")
            continue

        # 操作符白名单
        if op not in ("=", "!=", ">", "<", ">=", "<=", "LIKE"):
            op = "="

        if op == "LIKE":
            where_parts.append(f"{field} ILIKE %s")
            params.append(f"%{value}%")
        else:
            where_parts.append(f"{field} {op} %s")
            params.append(value)

    where_sql = " AND ".join(where_parts) if where_parts else "1=1"
    offset = (page - 1) * limit

    count_sql = f"SELECT COUNT(*) AS cnt FROM cnstock_selection WHERE {where_sql}"
    data_sql = (
        f"SELECT * FROM cnstock_selection "
        f"WHERE {where_sql} "
        f"ORDER BY {sort_by} {sort_order} "
        f"LIMIT %s OFFSET %s"
    )

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            cur.execute(count_sql, params)
            total = int((cur.fetchone() or {}).get("cnt") or 0)

            cur.execute(data_sql, params + [limit, offset])
            rows = cur.fetchall() or []
            cur.close()

        return jsonify({
            "code": 0,
            "msg": "Success",
            "count": total,
            "page": page,
            "limit": limit,
            "data": [dict(r) for r in rows],
        })
    except Exception as e:
        logger.error(f"query_stocks failed: {e}", exc_info=True)
        return jsonify({"code": 1, "msg": f"查询失败: {e}", "data": []}), 500


# ================================================================
# POST /sync — 触发数据同步
# ================================================================
@xuangu_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """触发从东方财富选股器同步数据到 PostgreSQL

    请求体（可选）：
    {
        "date": "2026-04-19",   // 指定交易日，留空则拉最新
        "proxy": ""              // 可选 HTTP 代理
    }
    """
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
# GET /stats — 表统计信息
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
    """确保 qd_user_strategies 表存在，按需建表"""
    ddl = """
    CREATE TABLE IF NOT EXISTS qd_user_strategies (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
        name        VARCHAR(100) NOT NULL,
        conditions  TEXT NOT NULL DEFAULT '',
        description VARCHAR(500) DEFAULT '',
        created_at  TIMESTAMP DEFAULT NOW(),
        updated_at  TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_qdus_user_id
        ON qd_user_strategies (user_id);
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(ddl)
            conn.commit()
            cur.close()
    except Exception as e:
        logger.error(f"_ensure_strategies_table failed: {e}")


@xuangu_bp.route("/favorites", methods=["GET"])
@login_required
def get_favorites():
    """获取当前登录用户的收藏策略列表

    每个用户只能看到自己的策略，不会泄露他人数据。
    """
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"code": 401, "msg": "用户未认证", "data": []}), 401
    user_id = int(user_id)
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
            # datetime → string，方便前端渲染
            for ts_field in ("created_at", "updated_at"):
                if d.get(ts_field) and hasattr(d[ts_field], "isoformat"):
                    d[ts_field] = d[ts_field].isoformat()
            # conditions 是 JSON 字符串，解析回对象
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
    """保存或更新收藏策略

    请求体：
    {
        "id": 123,                // 可选：有 id 则更新，无 id 则新建
        "name": "低估值银行股",
        "conditions": [{"field":"pe9","op":"<","value":15}, ...],
        "description": "PE < 15 的银行板块"   // 可选
    }
    """
    import json as _json

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"code": 401, "msg": "用户未认证"}), 401
    user_id = int(user_id)
    data = request.get_json() or {}
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
                new_id = cur.fetchone()["id"]
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
    """删除收藏策略 — 仅限本人

    路径参数: /api/xuangu/favorites/123
    """
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"code": 401, "msg": "用户未认证"}), 401
    user_id = int(user_id)
    _ensure_strategies_table()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            # 先查归属
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
