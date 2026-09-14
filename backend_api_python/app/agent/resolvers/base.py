# -*- coding: utf-8 -*-
"""实体解析器基类。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ResolveResult:
    """实体解析结果。

    澄清契约（2026-09-13 用户裁定，通用、域无关）：
      **无法准确判断就应该反问，拿到准确信息才执行**。解析器一旦遇到歧义
      （标的多个候选、口径不明、指代不清、相对时间不落在交易日…），必须填
      clarify_question，而不是猜一个默认值继续往下跑。

      chat_node 见到非空 clarify_question 会把问题直接反问用户且**不进入执行**；
      用户答复作为新消息重新解析，此时歧义消除、自然走常规流程。

      为什么是"反问"而不是"猜"：代价不对称——猜错标的/猜错时间窗会让整份
      分析作废（甚至据错误结论下单），而反问只花一轮对话。
    """
    entities: List[dict] = field(default_factory=list)   # [{code, name, type}, ...]
    entity_code: str = ""      # 逗号分隔的代码
    entity_name: str = ""      # 逗号分隔的名称
    entity_type: str = ""      # 实体类型
    effective_input: str = ""   # 处理后的用户输入（含实体信息）
    clarify_question: str = ""  # 非空 = 必须先反问用户，不得继续执行

    @property
    def needs_clarify(self) -> bool:
        """是否必须先反问（供组合器/节点用，避免直接读字段名）。"""
        return bool(self.clarify_question)


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
