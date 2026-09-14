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

# 消歧（2026-09-13）：名称模糊搜索取前 N 个候选以判定歧义。
# 原实现用 limit=1 调 resolve_stock —— DB 至多回 1 条，歧义在结构上不可见，
# 于是"静默取首个候选"，选错标的风险极高（整份分析作废，甚至据错误结论下单）。
# 现拿多候选，无法用精确同名收敛时反问用户（见 base.ResolveResult 澄清契约）。
_AMBIGUOUS_LIMIT = 5    # 拉取候选数（判定歧义用）
_MAX_SHOWN = 5          # 反问时最多列出几个候选


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

    无法唯一确定时**反问**而非猜（2026-09-13）：
      - "分析平安" → 匹配多只（中国平安/平安银行…）→ clarify_question，要求用户确认
      - "分析万科A" → 精确同名收敛 → 正常解析
    6 位代码天然唯一，不触发反问。
    """

    def resolve(self, user_input: str) -> Optional[ResolveResult]:
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
        except ImportError:
            return None
        # 直接走底层 canonical 实现 basicinfo_db.search_stocks（resolve_stock 工具即其封装）。
        # 不再 import 工具包装层，避免破坏拔插式单一真相源。
        # akshare 等数据源为同步 DB/HTTP，在 async chat_node 中直接调用会阻塞事件循环
        #（同一 worker 的其他会话全部卡住，审计 P2）。包一层线程隔离。

        def _search(keyword: str, limit: int):
            return get_stock_basic_db().search_stocks(keyword, limit=limit)

        def _resolve_async(keyword: str, limit: int = 1):
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                return _pool.submit(_search, keyword, limit).result(timeout=10)

        def _candidates(keyword: str) -> list:
            """名称模糊搜索的候选列表（空=未匹配）。长度>1 表示无法唯一确定。"""
            try:
                rows = _resolve_async(keyword, limit=_AMBIGUOUS_LIMIT)
            except Exception:
                return []
            if not isinstance(rows, list):
                return []
            return [{"code": r.get("symbol"), "name": r.get("name", "")}
                    for r in rows if r.get("symbol")]

        entities = []

        # 1. 提取所有6位股票代码（前后不能紧跟数字）
        codes = re.findall(r'(?<!\d)(\d{6})(?!\d)', user_input)
        for code in codes:
            # 查询代码对应的名称
            name = ""
            try:
                rows = _resolve_async(code, limit=1)
                if isinstance(rows, list) and rows:
                    name = rows[0].get("name", "")
            except Exception:
                pass
            entities.append({"code": code, "name": name, "type": "stock"})

        # 2. 提取中文股票名称（去掉动词后按分隔符拆分）
        clean_input = _STOCK_VERBS.sub('', user_input).strip()
        for code in codes:
            clean_input = clean_input.replace(code, '')
        clean_input = clean_input.strip()

        ambiguous = []      # [(关键词, [候选, ...]), ...] 无法唯一确定 → 反问
        if clean_input:
            names = [n.strip() for n in _SEPARATORS.split(clean_input) if n.strip() and len(n.strip()) >= 2]
            for name in names:
                if name in [e['code'] for e in entities]:
                    continue
                try:
                    cands = _candidates(name)
                except Exception as e:
                    logger.debug("[StockResolver] 解析 '%s' 跳过: %s", name, e)
                    continue
                if not cands:
                    continue
                if len(cands) > 1:
                    # 精确同名优先收敛：只有一个候选与用户原词完全一致时不算歧义
                    # （如"万科A"这类完整名，避免被模糊匹配拖进无谓反问）
                    exact = [c for c in cands if c.get('name') == name]
                    if len(exact) != 1:
                        ambiguous.append((name, cands))
                        continue
                    cands = exact
                entities.append({"code": cands[0]['code'],
                                 "name": cands[0].get('name', ''), "type": "stock"})

        # 去重
        seen = set()
        unique = []
        for e in entities:
            if e['code'] and e['code'] not in seen:
                seen.add(e['code'])
                unique.append(e)

        # 无法唯一确定标的 → 按通用澄清契约反问，不猜着执行（2026-09-13）。
        # 反问优先于"已解析出的部分实体"：信息不全就往下跑，结论可能整体作废。
        if ambiguous:
            parts = []
            for kw, cands in ambiguous:
                opts = "、".join(f"{c['name']}({c['code']})" for c in cands[:_MAX_SHOWN])
                more = f" 等 {len(cands)} 只" if len(cands) > _MAX_SHOWN else ""
                parts.append(f"「{kw}」匹配到 {len(cands)} 只{more}：{opts}")
            question = ("无法唯一确定要分析的标的，请确认：" + "；".join(parts)
                        + "。可直接回复股票代码。")
            logger.info("[StockResolver] 标的歧义反问: %s", question[:120])
            return ResolveResult(
                entities=[{"type": "entity_clarify", "question": question}],
                entity_code="", entity_name="", entity_type="entity_clarify",
                effective_input=f"{user_input} 【标的待澄清】{question}",
                clarify_question=question,
            )

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
