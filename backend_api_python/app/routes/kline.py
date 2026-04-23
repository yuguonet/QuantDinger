"""
K线数据 API 路由

改造点：
  - 日/周/月线走 feather 缓存（KlineCacheManager）
  - 新增 /kline/prewarm 端点，批量预热自选股缓存
  - 新增 /kline/cache/status 端点，查看缓存状态
"""
import os
from flask import Blueprint, request, jsonify
from datetime import datetime
import traceback

from app.services.kline import KlineService
from app.utils.logger import get_logger

logger = get_logger(__name__)

kline_bp = Blueprint('kline', __name__)
kline_service = KlineService()

KLINE_MAX_LIMIT = int(os.getenv('KLINE_MAX_LIMIT', 1500))


@kline_bp.route('/kline', methods=['GET'])
def get_kline():
    """
    获取K线数据

    参数:
        market: 市场类型 (Crypto, USStock, Forex, Futures, CNStock)
        symbol: 交易对/股票代码
        timeframe: 时间周期 (1m, 5m, 15m, 30m, 1H, 4H, 1D, 1W, 1M)
        limit: 数据条数 (默认1000, 上限由 KLINE_MAX_LIMIT 控制)
        before_time: 获取此时间之前的数据 (可选，Unix时间戳)

    日/周/月线（1D/1W/1M）自动走本地 feather 缓存：
      - 缓存命中 → 直接返回（含市场时段合成当日 K 线）
      - 缓存未命中 → 走远程拉取并写入缓存
    """
    try:
        market = request.args.get('market', 'CNStock')
        symbol = request.args.get('symbol', '')
        timeframe = request.args.get('timeframe', '1D')
        try:
            limit = min(int(request.args.get('limit', 1000)), KLINE_MAX_LIMIT)
        except (ValueError, TypeError):
            limit = 1000
        limit = max(limit, 1)
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
            before_time=before_time,
        )

        if not klines:
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


@kline_bp.route('/kline/prewarm', methods=['POST'])
def prewarm_cache():
    """
    批量预热自选股的日线缓存。

    请求体 JSON:
        {
            "symbols": ["600519", "000001", ...],  // 股票代码列表
            "market": "CNStock"                      // 可选，默认 CNStock
        }

    流程：
        1. 去重股票代码
        2. 拉取所有股票的日线数据（5 年）
        3. 存入本地 feather 缓存
        4. 聚合生成周线和月线缓存
    """
    try:
        data = request.get_json() or {}
        symbols = data.get('symbols', [])
        market = data.get('market', 'CNStock')

        if not symbols:
            # 尝试从自选股表获取
            try:
                from app.utils.db import get_db_connection
                with get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT DISTINCT code FROM qd_watchlist")
                    rows = cur.fetchall() or []
                    cur.close()
                symbols = [r['code'] for r in rows if r.get('code')]
            except Exception as e:
                logger.warning(f"Failed to fetch watchlist symbols: {e}")

        if not symbols:
            return jsonify({
                'code': 0,
                'msg': 'No symbols to prewarm',
                'data': None
            }), 400

        # 去重
        unique_symbols = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
        logger.info(f"Prewarming cache for {len(unique_symbols)} unique symbols")

        results = kline_service.prewarm_all(
            symbols=unique_symbols,
            market=market,
        )

        all_ok = all(results.values())
        return jsonify({
            'code': 1 if all_ok else 0,
            'msg': f'Prewarm completed: {results}',
            'data': {
                'symbol_count': len(unique_symbols),
                'results': results,
            }
        })

    except Exception as e:
        logger.error(f"Failed to prewarm cache: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            'code': 0,
            'msg': f'Failed to prewarm cache: {str(e)}',
            'data': None
        }), 500


@kline_bp.route('/kline/cache/status', methods=['GET'])
def cache_status():
    """
    查看 K 线缓存状态。
    """
    try:
        import os
        cache_dir = kline_service.get_cache_dir()
        status = {
            'cache_dir': cache_dir,
            'exists': os.path.exists(cache_dir),
            'files': [],
        }

        if os.path.exists(cache_dir):
            for fname in sorted(os.listdir(cache_dir)):
                if fname.endswith('.feather'):
                    fpath = os.path.join(cache_dir, fname)
                    status['files'].append({
                        'name': fname,
                        'size_kb': round(os.path.getsize(fpath) / 1024, 2),
                        'modified': datetime.fromtimestamp(os.path.getmtime(fpath)).strftime('%Y-%m-%d %H:%M:%S'),
                    })

        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': status,
        })

    except Exception as e:
        logger.error(f"Failed to get cache status: {str(e)}")
        return jsonify({
            'code': 0,
            'msg': f'Failed to get cache status: {str(e)}',
            'data': None
        }), 500


@kline_bp.route('/price', methods=['GET'])
def get_price():
    """获取最新价格"""
    try:
        market = request.args.get('market', 'CNStock')
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
