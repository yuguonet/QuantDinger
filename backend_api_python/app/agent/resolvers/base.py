# -*- coding: utf-8 -*-
"""实体解析器基类。"""
from __future__ import annotations


class EntityResolver:
    """实体解析器接口（通用）。

    不同领域实现不同的解析逻辑：
      - 股票：解析股票代码/名称
      - 商品：解析商品代码
      - 加密货币：解析币种
    """

    def resolve(self, user_input: str) -> dict | None:
        """从用户输入中解析实体。

        Returns:
            {"code": str, "name": str, "type": str} 或 None
        """
        raise NotImplementedError
