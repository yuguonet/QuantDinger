# -*- coding: utf-8 -*-
"""实体解析器基类。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ResolveResult:
    """实体解析结果。"""
    entities: List[dict] = field(default_factory=list)   # [{code, name, type}, ...]
    entity_code: str = ""      # 逗号分隔的代码
    entity_name: str = ""      # 逗号分隔的名称
    entity_type: str = ""      # 实体类型
    effective_input: str = ""   # 处理后的用户输入（含实体信息）


class EntityResolver:
    """实体解析器接口（通用）。

    不同领域实现不同的解析逻辑：
      - 股票：解析股票代码/名称，扩写分析指令
      - 商品：解析商品代码
      - 加密货币：解析币种

    子类只需实现 resolve()，返回 ResolveResult。
    nodes.py 只读取 ResolveResult 的字段，不需要知道实体细节。
    """

    def resolve(self, user_input: str) -> Optional[ResolveResult]:
        """从用户输入中解析实体，返回解析结果。

        Returns:
            ResolveResult 或 None（无实体）
        """
        raise NotImplementedError
