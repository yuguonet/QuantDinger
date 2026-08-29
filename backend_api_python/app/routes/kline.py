"""
K线数据 API 路由
"""
from flask import Blueprint, request, jsonify
from datetime import datetime
import traceback

from app.services.kline import KlineService
from app.utils.logger import get_logger

logger = get_logger(__name__)

kline_bp = Blueprint('kline', __name__)
kline_service = KlineService()


@kline_bp.route('/kline', methods=['GET'])
def get_kline():
    """
    获取K线数据
    
    参数:
        market: 市场类型 (Crypto, USStock, Forex, Futures)
        symbol: 交易对/股票代码
        timeframe: 时间周期 (1m, 5m, 15m, 30m, 1H, 4H, 1D, 1W)
        limit: 数据条数 (默认300)
        before_time: 获取此时间之前的数据 (可选，Unix时间戳)
    """
    try:
        # 强制 GET, 使用 request.args
        market = request.args.get('market', 'USStock')
        symbol = request.args.get('symbol', '')
        timeframe = request.args.get('timeframe', '1D')
        limit = int(request.args.get('limit', 300))
        before_time = request.args.get('before_time') or request.args.get('beforeTime')
        
        if before_time:
            before_time = int(before_time)
        
        if not symbol:
            return jsonify({
                'code': 0,
                'msg': 'Missing symbol parameter',
                'data': None
            }), 400
        
        logger.info(f"Requesting K-lines: {market}:{symbol}, timeframe={timeframe}, limit={limit}")
        
        klines = kline_service.get_kline(
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            before_time=before_time
        )
        
        if not klines:
            # 针对特定情况给出更详细的提示
            msg = 'No data found'
            if market == 'Forex' and timeframe == '1m':
                msg = 'Forex 1-minute data requires Tiingo paid subscription'
            elif market == 'Forex' and timeframe in ('1W', '1M'):
                msg = 'No weekly/monthly data available for this period'
            return jsonify({
                'code': 0,
                'msg': msg,
                'data': [],
                'hint': 'tiingo_subscription' if (market == 'Forex' and timeframe == '1m') else None
            })
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': klines
        })
        
    except Exception as e:
        logger.error(f"Failed to fetch K-lines: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            'code': 0,
            'msg': f'Failed to fetch kline data: {str(e)}',
            'data': None
        }), 500


@kline_bp.route('/price', methods=['GET'])
def get_price():
    """获取最新价格"""
    try:
        market = request.args.get('market', 'USStock')
        symbol = request.args.get('symbol', '')
        
        if not symbol:
            return jsonify({
                'code': 0,
                'msg': 'Missing symbol parameter',
                'data': None
            }), 400
        
        price_data = kline_service.get_latest_price(market, symbol)
        
        if not price_data:
            return jsonify({
                'code': 0,
                'msg': 'No price data found',
                'data': None
            })
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': price_data
        })
        
    except Exception as e:
        logger.error(f"Failed to fetch price: {str(e)}")
        return jsonify({
            'code': 0,
            'msg': f'Failed to fetch price: {str(e)}',
            'data': None
        }), 500


@kline_bp.route('/chip_distribution', methods=['GET'])
def get_chip_distribution():
    """筹码分布数据（前端筹码分布图用）。

    参数:
        market: 市场类型
        symbol: 股票代码
        lookback_days: 回看天数（默认 120）
    """
    try:
        market = request.args.get('market', 'CNStock')
        symbol = request.args.get('symbol', '')
        lookback_days = int(request.args.get('lookback_days', 120))

        if not symbol:
            return jsonify({'code': 0, 'msg': 'Missing symbol', 'data': None}), 400

        # 只对 A 股有效
        if market not in ('CNStock', 'HKStock'):
            return jsonify({'code': 1, 'msg': 'Chip distribution only for CN/HK stocks', 'data': None})

        from app.services.chip_service import calc_chip_for_chart

        klines = kline_service.get_kline(market=market, symbol=symbol, timeframe='1D', limit=lookback_days + 30)
        if not klines:
            return jsonify({'code': 0, 'msg': 'No kline data', 'data': None})

        result = calc_chip_for_chart(klines, lookback_days=lookback_days)
        if not result:
            return jsonify({'code': 0, 'msg': 'Chip calculation failed', 'data': None})

        return jsonify({'code': 1, 'msg': 'success', 'data': result})
    except Exception as e:
        logger.error(f'chip_distribution error: {e}')
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

