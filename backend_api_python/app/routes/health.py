"""
健康检查路由
"""
import json
import queue

from flask import Blueprint, Response, jsonify
from datetime import datetime

health_bp = Blueprint('health', __name__)


@health_bp.route('/', methods=['GET'])
def index():
    """API 首页"""
    return jsonify({
        'name': 'QuantDinger Python API',
        'version': '2.0.0',
        'status': 'running',
        'timestamp': datetime.now().isoformat()
    })


@health_bp.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })


@health_bp.route('/api/health', methods=['GET'])
def api_health_check():
    """兼容路径：用于容器健康检查/反代探针等场景。"""
    return health_check()


@health_bp.route('/api/cron/status', methods=['GET'])
def cron_worker_status():
    """查看 Cron Worker 健康状态。"""
    try:
        from app.agent.cron import get_scheduled_jobs
        from app.utils.db import get_db_connection

        scheduled = get_scheduled_jobs()

        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE enabled) as enabled FROM qd_cron_jobs")
            row = cur.fetchone()

        return jsonify({
            "scheduled_count": len(scheduled),
            "total_jobs": row["total"] if row else 0,
            "enabled_jobs": row["enabled"] if row else 0,
            "scheduled_jobs": scheduled,
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@health_bp.route('/api/cron/events', methods=['GET'])
def cron_events_stream():
    """SSE 实时推送 Cron 任务执行事件。

    前端用法：
        const es = new EventSource('/api/cron/events');
        es.onmessage = (e) => {
            const data = JSON.parse(e.data);
            // data.type: "job_start" | "job_success" | "job_error"
            // data.job_id, data.job_name, data.timestamp, ...
        };

    事件格式：
        data: {"type":"job_start","job_id":1,"job_name":"盘后回溯","timestamp":"..."}
        data: {"type":"job_success","job_id":1,"job_name":"盘后回溯","steps":3,...}
        data: {"type":"job_error","job_id":1,"job_name":"盘后回溯","error":"..."}
    """
    from app.agent.cron import subscribe, unsubscribe

    def _stream():
        q = subscribe()
        try:
            # 连接建立时发一个 hello
            yield f"data: {json.dumps({'type': 'connected', 'timestamp': datetime.now().isoformat()}, ensure_ascii=False)}\n\n"

            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    # 30 秒无事件，发心跳保活
                    yield f": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            unsubscribe(q)

    return Response(
        _stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
