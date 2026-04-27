"""
API Routes Module
"""
from flask import Flask


def register_routes(app: Flask):
    """Register all API route blueprints"""
    from app.routes.kline import kline_bp
    from app.routes.backtest import backtest_bp
    from app.routes.health import health_bp
    from app.routes.market import market_bp
    from app.routes.strategy import strategy_bp
    from app.routes.credentials import credentials_bp
    from app.routes.auth import auth_bp
    from app.routes.ai_chat import ai_chat_bp
    from app.routes.indicator import indicator_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.settings import settings_bp
    from app.routes.portfolio import portfolio_bp
    from app.routes.ibkr import ibkr_bp
    from app.routes.mt5 import mt5_bp
    from app.routes.user import user_bp
    from app.routes.community import community_bp
    from app.routes.fast_analysis import fast_analysis_bp
    from app.routes.billing import billing_bp
    from app.routes.quick_trade import quick_trade_bp
    from app.routes.polymarket import polymarket_bp
    from app.routes.experiment import experiment_bp
    # eQuant features: stock picker, market dashboard, AI agent
    from app.routes.xuangu import xuangu_bp
    from app.routes.shichang import shichang_bp, global_market_bp
    from app.routes.agent_blueprint import agent_bp
    from app.routes.agent_analysis import analysis_bp
    # stock screener — 选股器独立 API（前端功能移植到后端）
    from app.routes.stock_screener_api import stock_screener_bp
    # market_local — 本地行情存储 (feather)，数据源来自 global-market
    from app.market_store.plugin_api import market_local_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(auth_bp, url_prefix='/api/auth')   # Auth routes
    app.register_blueprint(user_bp, url_prefix='/api/users')  # User management
    app.register_blueprint(kline_bp, url_prefix='/api/indicator')
    app.register_blueprint(backtest_bp, url_prefix='/api/indicator')
    app.register_blueprint(market_bp, url_prefix='/api/market')
    app.register_blueprint(ai_chat_bp, url_prefix='/api/ai')
    app.register_blueprint(indicator_bp, url_prefix='/api/indicator')
    app.register_blueprint(strategy_bp, url_prefix='/api')
    app.register_blueprint(credentials_bp, url_prefix='/api/credentials')
    app.register_blueprint(dashboard_bp, url_prefix='/api/dashboard')
    app.register_blueprint(settings_bp, url_prefix='/api/settings')
    app.register_blueprint(portfolio_bp, url_prefix='/api/portfolio')
    app.register_blueprint(ibkr_bp, url_prefix='/api/ibkr')
    app.register_blueprint(mt5_bp, url_prefix='/api/mt5')
    app.register_blueprint(global_market_bp, url_prefix='/api/global-market')
    app.register_blueprint(community_bp, url_prefix='/api/community')
    app.register_blueprint(fast_analysis_bp, url_prefix='/api/fast-analysis')
    app.register_blueprint(billing_bp, url_prefix='/api/billing')
    app.register_blueprint(quick_trade_bp, url_prefix='/api/quick-trade')
    app.register_blueprint(polymarket_bp, url_prefix='/api/polymarket')
    app.register_blueprint(experiment_bp, url_prefix='/api/experiment')
    # eQuant features
    app.register_blueprint(xuangu_bp, url_prefix='/api/xuangu')
    app.register_blueprint(shichang_bp, url_prefix='/api/shichang')
    app.register_blueprint(agent_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(stock_screener_bp, url_prefix='/api/stock-screener')
    app.register_blueprint(market_local_bp, url_prefix='/api/market-local')