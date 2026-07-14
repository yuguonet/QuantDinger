# -*- coding: utf-8 -*-
"""股票实体解析器（金融领域特化）。"""
from __future__ import annotations

import logging
import re

from .base import EntityResolver

logger = logging.getLogger(__name__)


class StockResolver(EntityResolver):
    """股票实体解析器。

    解析用户输入中的股票代码或名称：
      - 6位数字 → 直接识别为股票代码
      - 中文名称 → 调用 resolve_stock 查询代码
    """

    def resolve(self, user_input: str) -> dict | None:
        try:
            from tools.data_tools import resolve_stock
            code_match = re.search(r'\b(\d{6})\b', user_input)
            if code_match:
                code = code_match.group(1)
                return {"code": code, "name": "", "type": "stock"}

            clean_input = re.sub(r'分析|看看|查一下|怎么样|什么股|股票|推荐|选|买|卖', '', user_input).strip()
            if clean_input and len(clean_input) >= 2:
                result = resolve_stock(clean_input, limit=1)
                if isinstance(result, dict) and not result.get('error'):
                    if result.get('code'):
                        return {"code": result['code'], "name": result.get('name', ''), "type": "stock"}
                    elif result.get('data'):
                        first = result['data'][0]
                        return {"code": first.get('code', ''), "name": first.get('name', ''), "type": "stock"}
        except Exception as e:
            logger.debug("[StockResolver] 解析跳过: %s", e)
        return None
