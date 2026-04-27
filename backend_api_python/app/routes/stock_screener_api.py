# -*- coding: utf-8 -*-
"""
/api/stock-screener/* — 选股器独立 API 路由

将前端选股器的全部后端能力暴露为 HTTP 接口。
不替换任何现有代码，作为独立 Blueprint 注册。

端点：
  POST /api/stock-screener/search    → 智能选股搜索（keyword 或 filters）
  GET  /api/stock-screener/presets   → 获取预设条件分类
  GET  /api/stock-screener/filters   → 获取筛选条件结构（默认值）
  POST /api/stock-screener/parse     → 自然语言 → 结构化条件
  POST /api/stock-screener/build     → 结构化条件 → 自然语言
  POST /api/stock-screener/batch     → 批量筛选（多条件组合）
"""
from flask import Blueprint, request, jsonify
import logging

logger = logging.getLogger(__name__)

stock_screener_bp = Blueprint("stock_screener", __name__)


# ── POST /search — 智能选股搜索 ──────────────────────────────

@stock_screener_bp.route("/search", methods=["POST"])
def search_stocks():
    """
    智能选股搜索。

    Request JSON（两种模式二选一）：

    模式1 — keyword 模式（与前端 performEastMoneySearch 一致）：
      {
        "keyword": "市盈率低于20的科技股",
        "market": "A股",
        "page_size": 50
      }

    模式2 — filters 模式（传结构化条件，内部自动拼接关键词）：
      {
        "filters": {
          "pe_min": 0, "pe_max": 20,
          "industry": ["银行"],
          "roe_min": 15
        },
        "market": "A股",
        "page_size": 50
      }
    """
    from app.agent.tools.stock_screener_tools import screen_stocks

    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    market = (data.get("market") or "全部").strip()
    filters = data.get("filters")
    page_size = data.get("page_size", 50)
    page_no = data.get("page_no", 1)
    proxy = (data.get("proxy") or "").strip() or None

    # 参数类型校验
    if page_size is not None:
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            return jsonify({"code": 1, "msg": "page_size 必须是整数"}), 400
    if page_no is not None:
        try:
            page_no = int(page_no)
        except (TypeError, ValueError):
            return jsonify({"code": 1, "msg": "page_no 必须是整数"}), 400

    if not keyword and not filters:
        return jsonify({"code": 1, "msg": "keyword 或 filters 至少传一个"}), 400

    try:
        result = screen_stocks(
            keyword=keyword,
            market=market,
            filters=filters,
            page_size=page_size,
            page_no=page_no,
            proxy=proxy,
        )
        if "error" in result:
            return jsonify({"code": 1, "msg": result["error"], "data": result}), 500
        return jsonify({"code": 0, "data": result})
    except Exception as e:
        logger.error("stock_screener search failed: %s", e, exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ── GET /presets — 获取预设条件 ───────────────────────────────

@stock_screener_bp.route("/presets", methods=["GET"])
def get_presets():
    """获取选股器支持的所有筛选条件分类和示例。"""
    from app.agent.tools.stock_screener_tools import get_screener_presets
    try:
        return jsonify({"code": 0, "data": get_screener_presets()})
    except Exception as e:
        logger.error("get_presets failed: %s", e, exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ── GET /filters — 获取筛选条件结构 ──────────────────────────

@stock_screener_bp.route("/filters", methods=["GET"])
def get_filters():
    """获取筛选条件的完整结构（130+ 字段的默认值）。"""
    from app.agent.tools.stock_screener_tools import get_default_filters
    try:
        return jsonify({"code": 0, "data": get_default_filters()})
    except Exception as e:
        logger.error("get_filters failed: %s", e, exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ── POST /parse — 自然语言 → 结构化条件 ─────────────────────

@stock_screener_bp.route("/parse", methods=["POST"])
def parse_text():
    """
    将自然语言选股文本解析为结构化筛选条件。

    Request: {"text": "PE在5到20之间; ROE不低于15%; 银行股"}
    Response: {"code": 0, "data": {"pe_min": 5, "pe_max": 20, "roe_min": 15, ...}}
    """
    from app.agent.tools.stock_screener_tools import parse_filters_from_text

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"code": 1, "msg": "text 不能为空"}), 400

    try:
        filters = parse_filters_from_text(text)
        return jsonify({"code": 0, "data": filters})
    except Exception as e:
        logger.error("parse_text failed: %s", e, exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ── POST /build — 结构化条件 → 自然语言 ─────────────────────

@stock_screener_bp.route("/build", methods=["POST"])
def build_text():
    """
    将结构化筛选条件转换为自然语言关键词。

    Request: {"filters": {"pe_min": 5, "pe_max": 20, "roe_min": 15}}
    Response: {"code": 0, "data": {"keyword": "PE在5到20之间; ROE不低于15%"}}
    """
    from app.agent.tools.stock_screener_tools import build_keyword_from_filters

    data = request.get_json(silent=True) or {}
    filters = data.get("filters")
    if not filters:
        return jsonify({"code": 1, "msg": "filters 不能为空"}), 400

    try:
        keyword = build_keyword_from_filters(filters)
        return jsonify({"code": 0, "data": {"keyword": keyword}})
    except Exception as e:
        logger.error("build_text failed: %s", e, exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


# ── POST /batch — 批量多条件筛选 ─────────────────────────────

@stock_screener_bp.route("/batch", methods=["POST"])
def batch_screen():
    """
    批量筛选：一次请求多个条件。

    Request: {
      "queries": [
        {"keyword": "PE低于15的银行股", "market": "A股"},
        {"filters": {"pe_max": 15, "industry": ["银行"]}}
      ]
    }
    """
    from app.agent.tools.stock_screener_tools import screen_stocks

    data = request.get_json(silent=True) or {}
    queries = data.get("queries") or []

    if not queries:
        return jsonify({"code": 1, "msg": "queries 不能为空"}), 400
    if len(queries) > 10:
        return jsonify({"code": 1, "msg": "单次最多10个筛选条件"}), 400

    results = []
    for q in queries:
        keyword = (q.get("keyword") or "").strip()
        market = (q.get("market") or "全部").strip()
        filters = q.get("filters")
        if not keyword and not filters:
            results.append({"keyword": "", "error": "条件为空", "stocks": []})
            continue
        try:
            result = screen_stocks(keyword=keyword, market=market, filters=filters, page_size=50)
            results.append(result)
        except Exception as e:
            results.append({"keyword": keyword, "error": str(e), "stocks": []})

    return jsonify({"code": 0, "data": {"results": results, "count": len(results)}})
