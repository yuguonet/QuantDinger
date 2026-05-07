"""
market_cn — 国内市场宏观数据模块

统一入口: MacroCNBackend (macro_backend.py)

模块结构:
    macro_backend.py  — 统一入口类，惰性缓存 + 并行拉取
    sentiment.py      — A股恐贪指数 (东方财富涨跌统计)
    futures_vol.py    — 期货波动率 (新浪期货)
    hot_sectors.py    — 热门板块 (东方财富板块行情)
    china_market.py   — 政策新闻 (news_service)
    utils.py          — 公共工具: HTTP session, 缓存, 重试

用法:
    from app.market_cn.macro_backend import MacroCNBackend
    backend = MacroCNBackend()
    data = backend.fetch_all()
"""
