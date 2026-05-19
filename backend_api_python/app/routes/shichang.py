"""
市场看板后端 API — 薄壳路由层

路由职责：接收请求 → 调用卡片/数据模块 → 返回 JSON
所有业务逻辑在 cards/ 或 market_cn/ 中，此文件不放数据获取代码。

Blueprint:
  - shichang_bp      → /api/shichang       A股看板（旧路由兼容 + 国内宏观/板块）
  - global_market_bp → /api/global-market   国际市场
  - _cards_bp        → /api/shichang/cards  自注册卡片
"""
from flask import Blueprint, jsonify, make_response, request
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

shichang_bp = Blueprint('shichang', __name__)


def _make_resp(data):
    resp = make_response(jsonify(data))
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ############################################################
#  旧路由兼容 — 委托给 cards 模块
# ############################################################

@shichang_bp.route('/overview')
def overview():
    try:
        from app.market_cn.cards.overview import fetch
        return _make_resp(fetch())
    except Exception as e:
        logger.error("overview 失败: %s", e)
        return _make_resp({})


@shichang_bp.route('/streak')
def streak():
    try:
        from app.market_cn.cards.streak import fetch
        return _make_resp(fetch())
    except Exception as e:
        logger.error("streak 失败: %s", e)
        return _make_resp({})


@shichang_bp.route('/dragon')
def dragon():
    try:
        from app.market_cn.cards.dragon_tiger import fetch
        return _make_resp(fetch())
    except Exception as e:
        logger.error("dragon 失败: %s", e)
        return _make_resp({})


@shichang_bp.route('/hot')
def hot():
    try:
        from app.market_cn.cards.hot_list import fetch
        return _make_resp(fetch())
    except Exception as e:
        logger.error("hot 失败: %s", e)
        return _make_resp({})


@shichang_bp.route('/strong')
def strong():
    try:
        from app.market_cn.cards.strong_stocks import fetch
        return _make_resp(fetch())
    except Exception as e:
        logger.error("strong 失败: %s", e)
        return _make_resp({})


@shichang_bp.route('/')
def market_data():
    """兼容旧接口 — 聚合所有卡片数据"""
    from app.market_cn.cards.overview import fetch as f_ov
    from app.market_cn.cards.streak import fetch as f_sk
    from app.market_cn.cards.dragon_tiger import fetch as f_dg
    from app.market_cn.cards.hot_list import fetch as f_ht
    from app.market_cn.cards.strong_stocks import fetch as f_st
    from app.market_cn.cards.ai_analysis import fetch as f_ai

    data = {}
    for name, fn in [("overview", f_ov), ("streak", f_sk), ("dragon", f_dg),
                      ("hot", f_ht), ("strong", f_st)]:
        try:
            data.update(fn())
        except Exception as e:
            logger.error("market_data %s 失败: %s", name, e)

    try:
        data['aiAnalysis'] = f_ai()
    except Exception:
        pass

    return _make_resp(data)


# ############################################################
#  国内宏观 + 板块路由 — 走 market_cn.china_market
# ############################################################

from app.market_cn.china_market import (
    get_china_macro,
    get_fear_greed,
    get_policy,
    get_hot_sectors as _get_hot_sectors,
    get_sector_stocks,
    get_sector_trend as _get_sector_trend,
    get_sector_prediction,
    get_sector_history as _get_sector_history,
    get_sector_cycle,
    get_emotion_history as _get_emotion_history,
    refresh as refresh_cn,
)


@shichang_bp.route('/china-macro')
def china_macro():
    try:
        return _make_resp(get_china_macro())
    except Exception as e:
        logger.error("china-macro 失败: %s", e)
        return _make_resp({"code": 0, "msg": "获取失败", "data": {}})


@shichang_bp.route('/china-fear-greed')
def china_fear_greed():
    try:
        return _make_resp(get_fear_greed())
    except Exception as e:
        logger.error("china-fear-greed 失败: %s", e)
        return _make_resp({"code": 0, "msg": str(e), "data": {}})


@shichang_bp.route('/china-policy')
def china_policy():
    try:
        return _make_resp(get_policy())
    except Exception as e:
        logger.error("china-policy 失败: %s", e)
        return _make_resp({"code": 0, "msg": "获取失败", "data": {}})


@shichang_bp.route('/hot-sectors')
def hot_sectors():
    try:
        return _make_resp(_get_hot_sectors())
    except Exception as e:
        logger.error("hot-sectors 失败: %s", e)
        return _make_resp({"code": 0, "msg": str(e), "data": {}})


@shichang_bp.route('/sector-detail/<board_code>')
def sector_detail(board_code):
    try:
        return _make_resp(get_sector_stocks(board_code))
    except Exception as e:
        logger.error("sector-detail %s 失败: %s", board_code, e)
        return _make_resp({"code": 0, "msg": str(e), "data": []})


@shichang_bp.route('/sector-trend')
def sector_trend():
    board_type = request.args.get("type", "industry")
    try:
        return _make_resp(_get_sector_trend(board_type=board_type))
    except Exception as e:
        logger.error("sector-trend 失败: %s", e)
        return _make_resp({"code": 0, "msg": str(e), "data": {}})


@shichang_bp.route('/sector-prediction')
def sector_prediction():
    try:
        return _make_resp(get_sector_prediction())
    except Exception as e:
        logger.error("sector-prediction 失败: %s", e)
        return _make_resp({"code": 0, "msg": "获取失败", "data": {}})


@shichang_bp.route('/sector-history')
def sector_history():
    board_type = request.args.get("type", "industry")
    days = request.args.get("days", 30, type=int)
    try:
        return _make_resp(_get_sector_history(board_type=board_type, days=days))
    except Exception as e:
        logger.error("sector-history 失败: %s", e)
        return _make_resp({"code": 0, "msg": str(e), "data": []})


@shichang_bp.route('/sector-cycle')
def sector_cycle():
    board_type = request.args.get("type", "industry")
    try:
        return _make_resp(get_sector_cycle(board_type=board_type))
    except Exception as e:
        logger.error("sector-cycle 失败: %s", e)
        return _make_resp({"code": 0, "msg": "获取失败", "data": {}})


@shichang_bp.route('/emotion/history')
def emotion_history():
    hours = request.args.get('hours', type=int)
    date = request.args.get('date')
    try:
        return _make_resp(_get_emotion_history(hours=hours, date=date))
    except Exception as e:
        logger.error("查询情绪历史失败: %s", e)
        return _make_resp({"code": 0, "msg": str(e), "history": []})


# ############################################################
#  手动刷新 — 委托 china_market
# ############################################################

@shichang_bp.route('/refresh', methods=['POST'])
def refresh_data():
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")
    try:
        results = refresh_cn(target=target)
        return jsonify({"code": 1, "msg": "refresh done", "results": results})
    except Exception as e:
        logger.error("refresh 失败: %s", e)
        return jsonify({"code": 0, "msg": str(e), "results": {}})


# ############################################################
#  国际市场路由 — 走 data_providers.global_market
# ############################################################

from app.data_providers.global_market import (
    get_sentiment,
    get_indices as _get_indices,
    get_heatmap as _get_heatmap,
    get_news as _get_news,
    refresh as refresh_intl,
)

global_market_bp = Blueprint('global_market', __name__)


@global_market_bp.route("/sentiment", methods=["GET"])
def market_sentiment():
    try:
        return jsonify(get_sentiment())
    except Exception as e:
        logger.error("sentiment 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/indices", methods=["GET"])
def market_indices():
    try:
        return jsonify(_get_indices())
    except Exception as e:
        logger.error("indices 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/heatmap", methods=["GET"])
def market_heatmap():
    try:
        return jsonify(_get_heatmap())
    except Exception as e:
        logger.error("heatmap 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/news", methods=["GET"])
def market_news():
    lang = request.args.get("lang", "all")
    try:
        return jsonify(_get_news(lang=lang))
    except Exception as e:
        logger.error("news 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "data": {}})


@global_market_bp.route("/refresh", methods=["POST"])
def global_refresh_data():
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")
    try:
        results = refresh_intl(target=target)
        ok = all(v == "ok" for v in results.values())
        return jsonify({"code": 1 if ok else 0, "msg": "refreshed", "results": results})
    except Exception as e:
        logger.error("refresh 失败: %s", e, exc_info=True)
        return jsonify({"code": 0, "msg": str(e), "results": {}})


# ############################################################
#  A股看板卡片 — 自注册路由
# ############################################################

from app.market_cn.cards import _base as _cards

_cards_bp = Blueprint('shichang_cards', __name__)


@_cards_bp.route('/cards')
def list_cards():
    """返回所有可用卡片的元数据，前端可用来动态渲染"""
    return jsonify({"code": 1, "data": _cards.get_meta_list()})


# 为每个已注册卡片自动挂载路由
for _meta, _fn in _cards.get_enabled():
    def _make_handler(fn):
        def handler():
            try:
                data = fn()
                return jsonify({"code": 1, "data": data})
            except Exception as e:
                logger.error("卡片 %s 失败: %s", fn.__module__, e, exc_info=True)
                return jsonify({"code": 0, "msg": str(e), "data": {}}), 500
        handler.__name__ = f"card_{_meta.id}"
        return handler

    _cards_bp.add_url_rule(
        f"/cards{_meta.endpoint}",
        endpoint=f"card_{_meta.id}",
        view_func=_make_handler(_fn),
        methods=["GET"],
    )
