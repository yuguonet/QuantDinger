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
import json

from app.utils.db import get_db_connection
from app.utils.auth import login_required
from app.utils.logger import get_logger

logger = get_logger(__name__)
xuangu_bp = Blueprint("xuangu", __name__)


# ================================================================
#  审核取消注册表（进程内，单 worker 场景）
# ================================================================
#  不依赖 GeneratorExit（Nginx/Gunicorn 不保证投递），
#  通过显式 API 调用设置 cancel event 来停止审核。
import threading as _threading

_review_cancel_events: dict[int, _threading.Event] = {}
_review_cancelled_flags: dict[int, list] = {}
_review_in_progress: dict[int, bool] = {}  # 防止同一用户并发审核
_review_cancel_lock = _threading.Lock()


def _get_or_create_cancel_event(user_id: int) -> _threading.Event:
    """获取或创建用户的取消事件（线程安全）"""
    with _review_cancel_lock:
        if user_id not in _review_cancel_events:
            _review_cancel_events[user_id] = _threading.Event()
        return _review_cancel_events[user_id]


def _new_cancel_event(user_id: int) -> _threading.Event:
    """为新审核创建全新的 Event（避免 clear() 竞态影响旧审核）"""
    with _review_cancel_lock:
        event = _threading.Event()
        _review_cancel_events[user_id] = event
        return event


def _cleanup_cancel_event(user_id: int):
    """审核结束时清理取消事件和进行中标志"""
    with _review_cancel_lock:
        _review_cancel_events.pop(user_id, None)
        _review_in_progress.pop(user_id, None)


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
    """确保自选股表存在（使用统一 schema，含 market 列）"""
    ddl = """
    CREATE TABLE IF NOT EXISTS qd_watchlist (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL DEFAULT 0,
        market      VARCHAR(20) NOT NULL DEFAULT 'CNStock',
        symbol      VARCHAR(30) NOT NULL,
        name        VARCHAR(100) NOT NULL DEFAULT '',
        industry    VARCHAR(100) DEFAULT '',
        concept     TEXT DEFAULT '',
        new_price   NUMERIC DEFAULT NULL,
        change_rate NUMERIC DEFAULT NULL,
        created_at  TIMESTAMP DEFAULT NOW(),
        updated_at  TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, market, symbol)
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
                code = (s.get("code") or s.get("symbol") or "").strip()
                if not code:
                    skipped += 1
                    continue
                market = (s.get("market") or "CNStock").strip()
                name = (s.get("name") or "")[:100]
                industry = (s.get("industry") or "")[:100]
                try:
                    cur.execute(
                        """INSERT INTO qd_watchlist (user_id, market, symbol, name, industry, concept, new_price, change_rate)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (user_id, market, symbol) DO UPDATE SET
                               name = EXCLUDED.name,
                               industry = EXCLUDED.industry,
                               concept = EXCLUDED.concept,
                               new_price = EXCLUDED.new_price,
                               change_rate = EXCLUDED.change_rate,
                               updated_at = NOW()""",
                        (
                            user_id,
                            market,
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
                "SELECT id, market, symbol AS code, name, industry, concept, new_price, change_rate, created_at "
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


# ================================================================
# 指标策略审核（SSE 流式）
# ================================================================

# ================================================================
# POST /review — 指标策略自动审核（SSE 流式）
# ================================================================
# 【作用】
#   接收前端选股器提交的股票列表 + 指标策略ID，
#   逐只股票执行指标分析，通过 SSE 实时推送审核进度和结果。
#
# 【前端调用方式】
#   fetch('/api/xuangu/review', { method:'POST', body: JSON.stringify({...}) })
#   → ReadableStream 逐条读取 SSE 消息
#
# 【SSE 消息流】
#   data: {"type":"progress","symbol":"000001","status":"checking","msg":"..."}
#   data: {"type":"result",  "symbol":"000001","added":true,   "msg":"..."}
#   data: {"type":"done",    "total":5,"added":2,"skipped":3}
#
# 【安全约束】
#   - @login_required：必须登录
#   - 单次最多 200 只（防滥用）
#   - 指标代码在沙箱中执行（safe_exec，30s 超时）

@xuangu_bp.route("/review", methods=["POST"])
@login_required
def review_by_indicator():
    """
    指标策略自动审核 — SSE 流式返回逐只股票审核进度。

    Request JSON:
      {
        "indicator_id": int,          # 指标ID（必填）
        "stocks": [                   # 待审核股票列表
          {"code": "000001", "name": "平安银行", "market": "CNStock"},
          ...
        ],
        "params": {}                  # 指标参数覆盖（可选）
      }

    Response: text/event-stream
      data: {"type":"progress","symbol":"000001","status":"checking","msg":"..."}
      data: {"type":"result","symbol":"000001","added":true,"msg":"..."}
      data: {"type":"done","total":5,"added":2,"skipped":3}
    """
    from flask import Response
    from app.services.indicator_review import review_stocks

    data = request.get_json() or {}
    user_id = g.user_id               # @login_required 已注入
    indicator_id = data.get("indicator_id")
    stocks = data.get("stocks") or []
    user_params = data.get("params") or {}

    # ── 参数校验（快速失败，不进入 SSE 流） ──
    if not indicator_id:
        return jsonify({"code": 1, "msg": "请选择指标策略"}), 400
    if not stocks:
        return jsonify({"code": 1, "msg": "未选择任何股票"}), 400
    if len(stocks) > 200:
        return jsonify({"code": 1, "msg": "单次最多审核200只股票"}), 400

    # ── SSE Generator ──
    # 使用心跳线程 + 队列，穿透 nginx 等代理的缓冲层
    def generate():
        import threading, queue

        cancelled = [False]
        q = queue.Queue()
        stop_event = threading.Event()
        # 最新进度快照，心跳线程用它生成 data 类型心跳（非注释）
        _latest_progress = {"index": 0, "total": len(stocks), "symbol": "", "status": "initializing"}

        # ── 取消事件：通过显式 API 调用触发，不依赖 GeneratorExit ──
        # ── 防重入：同一用户只能有一个审核在跑 ──
        with _review_cancel_lock:
            if _review_in_progress.get(user_id):
                yield 'data: {"type":"error","msg":"审核已在进行中，请等待完成或先取消"}\n\n'
                return
            _review_in_progress[user_id] = True

        # 每次审核创建全新 Event，避免与旧审核的 cancel_event 互相干扰
        cancel_event = _new_cancel_event(user_id)
        # 注册 cancelled 标志，让 /review/cancel 端点可以直接设置
        with _review_cancel_lock:
            _review_cancelled_flags[user_id] = cancelled


        try:
            _indicator_id = int(indicator_id)
        except (TypeError, ValueError):
            yield 'data: {"type":"error","msg":"无效的指标ID"}\n\n'
            return

        # 心跳线程：每 1 秒发一条 *data 类型* 心跳，确保前端能感知连接存活。
        # 旧版用 SSE 注释（": heartbeat"），前端 parser 忽略注释，导致长时间无
        # data 消息时进度条完全静止。改为 type=heartbeat 的真实 data 消息后，
        # 前端收到即可更新"仍在处理"状态。
        def heartbeat():
            while not stop_event.is_set():
                try:
                    # 等 1 秒；如果有数据进来先发数据
                    stop_event.wait(timeout=1)
                    if stop_event.is_set():
                        break
                    # 检查取消事件（来自显式 API 调用）
                    if cancel_event.is_set():
                        break
                    p = _latest_progress
                    hb_msg = json.dumps({
                        "type": "heartbeat",
                        "symbol": p.get("symbol", ""),
                        "index": p.get("index", 0),
                        "total": p.get("total", len(stocks)),
                        "status": p.get("status", "checking"),
                        "msg": f"正在处理第 {p.get('index', 0)}/{p.get('total', len(stocks))} 只…",
                    }, ensure_ascii=False)
                    q.put(("data", f"data: {hb_msg}\n\n"), timeout=1)
                except queue.Full:
                    pass

        hb_thread = threading.Thread(target=heartbeat, daemon=True)
        hb_thread.start()

        # 生产者线程：运行审核并将结果放入队列
        def producer():
            gen = review_stocks(user_id, _indicator_id, stocks, user_params, _cancelled=cancelled)
            try:
                for sse_msg in gen:
                    # 检查取消事件（来自显式 API 调用）
                    if cancel_event.is_set():
                        logger.info(f"[review] producer 检测到取消事件，停止推送")
                        cancelled[0] = True  # 同步到 review_stocks 内部检查的标志
                        break
                    # 更新进度快照（心跳线程读取）
                    try:
                        _raw = sse_msg.strip()
                        if _raw.startswith("data: "):
                            _raw = _raw[6:]
                        _d = json.loads(_raw)
                        if "index" in _d:
                            _latest_progress["index"] = _d["index"]
                        if "symbol" in _d:
                            _latest_progress["symbol"] = _d["symbol"]
                        if "status" in _d:
                            _latest_progress["status"] = _d["status"]
                        if "total" in _d:
                            _latest_progress["total"] = _d["total"]
                    except Exception:
                        pass
                    q.put(("data", sse_msg))
            except Exception as e:
                logger.error(f"review SSE producer error: {e}", exc_info=True)
                q.put(("data", f'data: {{"type":"error","msg":"审核异常: {str(e)}"}}\n\n'))
            finally:
                try:
                    gen.close()  # 确保 generator 清理（释放 ThreadPoolExecutor 等资源）
                except Exception:
                    pass
                q.put(("done", None))

        producer_thread = threading.Thread(target=producer, daemon=True)
        producer_thread.start()

        try:
            while True:
                try:
                    msg_type, msg = q.get(timeout=1)
                except queue.Empty:
                    # 短超时轮询：检查取消事件 + 让 GeneratorExit 有机会投递
                    if cancel_event.is_set():
                        logger.info(f"[review] generator 检测到取消事件，退出主循环")
                        break
                    continue

                # 检查取消事件
                if cancel_event.is_set():
                    logger.info(f"[review] generator 检测到取消事件，退出主循环")
                    break

                if msg_type == "done":
                    break
                else:
                    yield msg
        except GeneratorExit:
            logger.info("review SSE: client disconnected, cancelling")
            raise
        except Exception as e:
            logger.error(f"review SSE generator error: {e}", exc_info=True)
        finally:
            # 无论 generator 以何种方式退出，都设置 cancelled[0] = True
            cancelled[0] = True
            stop_event.set()
            with _review_cancel_lock:
                _review_cancelled_flags.pop(user_id, None)
            _cleanup_cancel_event(user_id)

    # X-Accel-Buffering: no → 告诉 Nginx 不要缓冲，实时推送
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ================================================================
# POST /review/cancel — 取消正在进行的审核（显式 API）
# ================================================================
@xuangu_bp.route("/review/cancel", methods=["POST"])
@login_required
def cancel_review():
    """
    显式取消当前用户正在进行的指标审核。

    前端在用户点击"取消审核"按钮时调用此端点，
    通过共享的 threading.Event 通知审核 generator 停止，
    不依赖 Nginx/Gunicorn 的 GeneratorExit 投递。

    Request: {} (空 JSON 或无 body)
    Response: {"code": 0, "msg": "已发送取消信号"}
    """
    user_id = g.user_id
    event = _get_or_create_cancel_event(user_id)
    event.set()
    # 直接设置 cancelled 标志，不依赖 producer 线程中转
    with _review_cancel_lock:
        flag = _review_cancelled_flags.get(user_id)
        if flag:
            flag[0] = True
    logger.info(f"[review] 用户 {user_id} 发起取消审核")
    return jsonify({"code": 0, "msg": "已发送取消信号"})
