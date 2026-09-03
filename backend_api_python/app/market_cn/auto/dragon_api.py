"""dragon_api.py — 龙回头Pro 策略信号 API (蓝图, 挂载于 /api/market)

端点:
  GET /api/market/dragon/today   今日分层信号: action(买入/持仓/卖出) + watch(观察池)
  GET /api/market/dragon/markers?symbol=600519&days=90   K线图买卖点标记
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.utils.auth import login_required
from app.market_cn.auto import dragon_store as ds

dragon_bp = Blueprint('dragon', __name__)

_ensured = False


def _ensure():
    """懒建表 (首次请求/首次调度时执行, 幂等)。"""
    global _ensured
    if not _ensured:
        ds.ensure_tables()
        _ensured = True


@dragon_bp.route('/dragon/today', methods=['GET'])
@login_required
def dragon_today():
    """今日信号: action=买入/持仓/卖出 (组内活跃), watch=观察池 (待确认)。"""
    try:
        _ensure()
        action = ds.get_active_signals()
        watch = ds.get_watch_pending(days=5)
        return jsonify({'code': 1, 'msg': 'success', 'data': {
            'group': ds.DRAGON_GROUP_NAME,
            'action': action,
            'watch': watch,
        }})
    except Exception as e:
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500


@dragon_bp.route('/dragon/markers', methods=['GET'])
@login_required
def dragon_markers():
    """K线图买卖点标记: [{time, side: signal/buy/sell, price, label}]"""
    try:
        _ensure()
        symbol = (request.args.get('symbol') or '').strip()
        try:
            days = int(request.args.get('days', '90'))
        except (TypeError, ValueError):
            days = 90
        if not symbol:
            return jsonify({'code': 0, 'msg': '缺少 symbol', 'data': []}), 400
        return jsonify({'code': 1, 'msg': 'success', 'data': ds.get_markers(symbol, days)})
    except Exception as e:
        return jsonify({'code': 0, 'msg': str(e), 'data': []}), 500
