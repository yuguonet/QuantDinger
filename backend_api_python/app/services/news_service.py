# -*- coding: utf-8 -*-
"""
新闻服务代理层 — 统一入口

所有需要新闻功能的模块应从这里导入，而非直接导入 app.data_providers.news。
这样 news.py 底层重构时只需修改本文件。
"""
from app.data_providers.news import (
    fetch_financial_news,
    get_news_cache_manager,
    get_news_type,
    NewsCacheManager,
)

__all__ = [
    "fetch_financial_news",
    "get_news_cache_manager",
    "get_news_type",
    "NewsCacheManager",
]
