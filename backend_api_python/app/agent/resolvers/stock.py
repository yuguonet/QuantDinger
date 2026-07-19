# -*- coding: utf-8 -*-
"""股票实体解析器（金融领域特化）。"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .base import EntityResolver, ResolveResult

logger = logging.getLogger(__name__)

# 中文分隔符
_SEPARATORS = re.compile(r'[和、，,\s]+')
# 股票相关动词（清理用）
_STOCK_VERBS = re.compile(r'分析|看看|查一下|怎么样|什么股|股票|推荐|选|买|卖|对比|比较')


def _expand_stock_query(user_input: str, entities: list[dict]) -> str:
    """将股票分析指令扩写为完整指令。

    Args:
        user_input: 原始用户消息
        entities: 解析出的实体列表 [{code, name, type}, ...]

    Returns:
        扩写后的指令，如 "分析贵州茅台(600519): 帮我看看最近能不能买，周期：T+3，深度：标准"
    """
    if not entities:
        return user_input

    # 构建实体描述：名称(代码)
    entity_parts = []
    for e in entities:
        if e.get('name') and e.get('code'):
            entity_parts.append(f"{e['name']}({e['code']})")
        elif e.get('code'):
            entity_parts.append(e['code'])
    entity_desc = ",".join(entity_parts)

    # 注入实体信息
    expanded = user_input
    for e in entities:
        if e.get('name') and e.get('code'):
            if e['name'] in expanded:
                expanded = expanded.replace(e['name'], f"{e['name']}({e['code']})", 1)
            elif e['code'] in expanded:
                expanded = expanded.replace(e['code'], f"{e['name']}({e['code']})", 1)

    # 加默认分析参数
    default_params = "周期：T+3（T+1/T+3/1W/1M），深度：标准（简单/标准/深度）"
    return f"{expanded}，{default_params}"


class StockResolver(EntityResolver):
    """股票实体解析器。

    解析用户输入中的股票代码或名称，支持多股：
      - "分析300129" → entities=[{code: "300129"}]
      - "分析南威软件和雪天盐业" → entities=[{code: "603636"}, {code: "600929"}]
      - effective_input 含标准分析指令
    """

    def resolve(self, user_input: str) -> Optional[ResolveResult]:
        try:
            from tools.finance.data_tools import resolve_stock
        except ImportError:
            return None

        entities = []

        # 1. 提取所有6位股票代码
        codes = re.findall(r'\b(\d{6})\b', user_input)
        for code in codes:
            # 查询代码对应的名称
            name = ""
            try:
                info = resolve_stock(code, limit=1)
                if isinstance(info, dict) and info.get('name'):
                    name = info['name']
                elif isinstance(info, dict) and info.get('data'):
                    name = info['data'][0].get('name', '')
            except Exception:
                pass
            entities.append({"code": code, "name": name, "type": "stock"})

        # 2. 提取中文股票名称（去掉动词后按分隔符拆分）
        clean_input = _STOCK_VERBS.sub('', user_input).strip()
        for code in codes:
            clean_input = clean_input.replace(code, '')
        clean_input = clean_input.strip()

        if clean_input:
            names = [n.strip() for n in _SEPARATORS.split(clean_input) if n.strip() and len(n.strip()) >= 2]
            for name in names:
                if name in [e['code'] for e in entities]:
                    continue
                try:
                    result = resolve_stock(name, limit=1)
                    if isinstance(result, dict) and not result.get('error'):
                        if result.get('code'):
                            entities.append({"code": result['code'], "name": result.get('name', ''), "type": "stock"})
                        elif result.get('data'):
                            first = result['data'][0]
                            entities.append({"code": first.get('code', ''), "name": first.get('name', ''), "type": "stock"})
                except Exception as e:
                    logger.debug("[StockResolver] 解析 '%s' 跳过: %s", name, e)

        # 去重
        seen = set()
        unique = []
        for e in entities:
            if e['code'] and e['code'] not in seen:
                seen.add(e['code'])
                unique.append(e)

        if not unique:
            return None

        # 构建结果
        entity_code = ",".join(e['code'] for e in unique)
        entity_name = ",".join(e['name'] for e in unique if e['name'])
        entity_type = "stock"

        # 扩写：注入实体信息 + 默认分析参数
        effective_input = _expand_stock_query(user_input, unique)

        return ResolveResult(
            entities=unique,
            entity_code=entity_code,
            entity_name=entity_name,
            entity_type=entity_type,
            effective_input=effective_input,
        )
