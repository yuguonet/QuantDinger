# -*- coding: utf-8 -*-
"""
/api/agent-analysis/* — 股票分析 + 异步任务管理
适配 eQuant 项目，数据通过 DataSourceFactory 取得。
与原有 /api/analysis/* 不冲突，独立前缀。
"""
import json
import logging
import time
import uuid
from datetime import datetime
from threading import Thread
from queue import Queue
from typing import Dict, Any, Optional

from flask import Blueprint, request, jsonify, Response

from app.routes.schemas.analysis import (
    AnalyzeRequest,
    AnalysisResultResponse,
    TaskAccepted,
    TaskStatus,
    TaskInfo,
    TaskListResponse,
    DuplicateTaskErrorResponse,
)

logger = logging.getLogger(__name__)

# ── Blueprint（独立前缀避免与已有 /api/analysis 冲突）────
analysis_bp = Blueprint("agent_analysis", __name__, url_prefix="/api/agent-analysis")


# ── 工具函数 ──────────────────────────────────────────────

def canonical_stock_code(code: str) -> str:
    """统一股票代码格式（转大写、去空格）。"""
    return (code or "").strip().upper()


def _get_ds(market: str = "AShare"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)


def _detect_market(stock_code: str) -> str:
    code = canonical_stock_code(stock_code)
    if code.startswith(("SH", "SZ", "BJ")):
        return "AShare"
    if code.startswith("HK"):
        return "HShare"
    if len(code) <= 6 and code.isdigit():
        return "AShare"
    return "Crypto"


# ── 重复任务异常 ──────────────────────────────────────────
class DuplicateTaskError(Exception):
    def __init__(self, stock_code: str, existing_task_id: str):
        self.stock_code = stock_code
        self.existing_task_id = existing_task_id
        super().__init__(f"股票 {stock_code} 正在分析中，任务ID: {existing_task_id}")


# ── 任务队列模拟器 ────────────────────────────────────────
class TaskQueueSimulator:
    """内存任务队列，用于跟踪异步分析任务。"""

    def __init__(self):
        self.tasks: Dict[str, dict] = {}
        self.subscribers: list = []

    def submit_task(self, stock_code: str, stock_name: str = None,
                    report_type: str = None, force_refresh: bool = False) -> dict:
        # 检查是否已有相同股票的活跃任务
        for tid, t in self.tasks.items():
            if t["stock_code"] == stock_code and t["status"] in ("pending", "processing"):
                raise DuplicateTaskError(stock_code, tid)

        task_id = str(uuid.uuid4())
        info = {
            "task_id": task_id,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "status": "pending",
            "progress": 0,
            "message": "任务已提交",
            "report_type": report_type,
            "force_refresh": force_refresh,
            "created_at": datetime.now(),
            "started_at": None,
            "completed_at": None,
            "error": None,
        }
        self.tasks[task_id] = info
        self._notify("task_created", info)

        t = Thread(target=self._process, args=(task_id,), daemon=True)
        t.start()
        return info

    def _process(self, task_id: str):
        task = self.tasks[task_id]
        task["status"] = "processing"
        task["started_at"] = datetime.now()
        self._notify("task_started", task)

        for i in range(1, 11):
            time.sleep(0.3)
            task["progress"] = i * 10
            self._notify("task_progress", {
                "task_id": task_id,
                "progress": task["progress"],
                "status": "processing",
            })

        # 实际分析
        try:
            market = _detect_market(task["stock_code"])
            ds = _get_ds(market)
            klines = ds.get_kline(task["stock_code"], "1D", 30) or []
            ticker = None
            try:
                ticker = ds.get_ticker(task["stock_code"])
            except Exception:
                pass

            result = {
                "stock_code": task["stock_code"],
                "stock_name": task.get("stock_name", ""),
                "market": market,
                "latest_price": (ticker or {}).get("last", 0),
                "kline_count": len(klines),
                "analysis_time": datetime.now().isoformat(),
                "summary": f"{task['stock_code']} 共获取 {len(klines)} 条日K数据。",
            }
            task["status"] = "completed"
            task["progress"] = 100
            task["completed_at"] = datetime.now()
            task["result"] = result
        except Exception as e:
            logger.error("Task %s analysis failed: %s", task_id, e, exc_info=True)
            task["status"] = "failed"
            task["error"] = str(e)

        self._notify("task_completed", task)

    def get_task(self, task_id: str) -> Optional[dict]:
        return self.tasks.get(task_id)

    def list_all(self, limit: int = 20):
        return list(self.tasks.values())[:limit]

    def list_pending(self):
        return [t for t in self.tasks.values() if t["status"] in ("pending", "processing")]

    def stats(self):
        s = {"total": 0, "pending": 0, "processing": 0, "completed": 0, "failed": 0}
        for t in self.tasks.values():
            s["total"] += 1
            s[t["status"]] = s.get(t["status"], 0) + 1
        return s

    def subscribe(self, q: Queue):
        self.subscribers.append(q)

    def unsubscribe(self, q: Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def _notify(self, event_type: str, data: Any):
        for sub in self.subscribers:
            try:
                sub.put({"type": event_type, "data": data})
            except Exception:
                pass


# 全局任务队列
_task_queue = TaskQueueSimulator()


# ── SSE 格式化 ────────────────────────────────────────────
def _sse(event_type: str, data: Dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── 同步分析 ──────────────────────────────────────────────
def _sync_analysis(stock_code: str) -> Response:
    """同步分析：直接调 DataSourceFactory 取数据并返回。"""
    query_id = str(uuid.uuid4())
    try:
        market = _detect_market(stock_code)
        ds = _get_ds(market)

        # 获取行情
        ticker = None
        try:
            ticker = ds.get_ticker(stock_code)
        except Exception:
            pass

        # 获取 K 线
        klines = ds.get_kline(stock_code, "1D", 60) or []

        report = {
            "stock_code": stock_code,
            "market": market,
            "query_id": query_id,
            "ticker": ticker or {},
            "kline_summary": {
                "count": len(klines),
                "latest_close": klines[-1]["close"] if klines else None,
                "latest_time": klines[-1]["time"] if klines else None,
            },
            "analysis_time": datetime.now().isoformat(),
        }

        return jsonify({
            "code": 1,
            "msg": "success",
            "data": report,
        })
    except Exception as e:
        logger.error("Sync analysis failed for %s: %s", stock_code, e, exc_info=True)
        return jsonify({
            "code": 0,
            "msg": str(e),
            "data": f"分析过程发生错误: {str(e)}",
        }), 500


# ── 异步分析 ──────────────────────────────────────────────
def _async_analysis(stock_code: str, request_data: Dict) -> Response:
    """提交异步任务并返回 202。"""
    try:
        info = _task_queue.submit_task(
            stock_code=stock_code,
            stock_name=request_data.get("stock_name"),
            report_type=request_data.get("report_type"),
            force_refresh=request_data.get("force_refresh", False),
        )
        resp = jsonify({
            "task_id": info["task_id"],
            "status": "pending",
            "message": f"分析任务已加入队列: {stock_code}",
        })
        resp.status_code = 202
        return resp
    except DuplicateTaskError as e:
        resp = jsonify({
            "error": "duplicate_task",
            "message": str(e),
            "stock_code": e.stock_code,
            "existing_task_id": e.existing_task_id,
        })
        resp.status_code = 409
        return resp


# ── 路由 ──────────────────────────────────────────────────

@analysis_bp.route("/analyze", methods=["POST"])
def trigger_analysis():
    """触发股票分析（同步/异步）。"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "validation_error", "message": "请求体必须是JSON格式"}), 400

        stock_code = data.get("stock_code")
        stock_codes = data.get("stock_codes", [])
        async_mode = data.get("async_mode", False)

        all_codes = []
        if stock_code:
            all_codes.append(stock_code)
        all_codes.extend(stock_codes or [])

        if not all_codes:
            return jsonify({"error": "validation_error", "message": "必须提供 stock_code 或 stock_codes"}), 400

        all_codes = [canonical_stock_code(c) for c in all_codes]
        all_codes = list(dict.fromkeys(all_codes))
        final_code = all_codes[0]

        if async_mode:
            return _async_analysis(final_code, data)
        return _sync_analysis(final_code)

    except Exception as e:
        logger.error("Trigger analysis failed: %s", e, exc_info=True)
        return jsonify({"error": "internal_error", "message": str(e)}), 500


@analysis_bp.route("/tasks", methods=["GET"])
def get_task_list():
    """获取任务列表。"""
    try:
        status_param = request.args.get("status")
        limit = int(request.args.get("limit", 20))

        tasks = _task_queue.list_all(limit=limit)
        if status_param:
            sl = [s.strip().lower() for s in status_param.split(",")]
            tasks = [t for t in tasks if t["status"] in sl]

        stats = _task_queue.stats()

        task_infos = []
        for t in tasks:
            task_infos.append(TaskInfo(
                task_id=t["task_id"],
                stock_code=t["stock_code"],
                stock_name=t.get("stock_name"),
                status=t["status"],
                progress=t["progress"],
                message=t.get("message"),
                report_type=t.get("report_type", "detailed"),
                created_at=t["created_at"].isoformat(),
                started_at=t["started_at"].isoformat() if t.get("started_at") else None,
                completed_at=t["completed_at"].isoformat() if t.get("completed_at") else None,
                error=t.get("error"),
            ))

        resp = TaskListResponse(
            total=stats["total"],
            pending=stats["pending"],
            processing=stats["processing"],
            tasks=[ti.__dict__ if hasattr(ti, "__dict__") else vars(ti) for ti in task_infos],
        )
        return jsonify(resp.__dict__ if hasattr(resp, "__dict__") else vars(resp))
    except Exception as e:
        logger.error("Get task list failed: %s", e, exc_info=True)
        return jsonify({"error": "internal_error", "message": str(e)}), 500


@analysis_bp.route("/tasks/stream", methods=["GET"])
def task_stream():
    """SSE 任务状态流。"""
    def gen():
        yield _sse("connected", {"message": "Connected to task stream"})
        for t in _task_queue.list_pending():
            yield _sse("task_created", t)

        q = Queue()
        _task_queue.subscribe(q)
        try:
            while True:
                try:
                    ev = q.get(timeout=30)
                    yield _sse(ev["type"], ev["data"])
                except Exception:
                    yield _sse("heartbeat", {"timestamp": datetime.now().isoformat()})
        except GeneratorExit:
            pass
        finally:
            _task_queue.unsubscribe(q)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@analysis_bp.route("/status/<task_id>", methods=["GET"])
def get_analysis_status(task_id: str):
    """查询单个任务状态。"""
    try:
        task = _task_queue.get_task(task_id)
        if task:
            return jsonify({
                "task_id": task["task_id"],
                "status": task["status"],
                "progress": task["progress"],
                "result": task.get("result"),
                "error": task.get("error"),
            })
        # 未找到 -> 认为已完成
        return jsonify({
            "task_id": task_id,
            "status": "completed",
            "progress": 100,
            "result": None,
            "error": None,
        })
    except Exception as e:
        logger.error("Get task status failed: %s", e, exc_info=True)
        return jsonify({"error": "internal_error", "message": str(e)}), 500
