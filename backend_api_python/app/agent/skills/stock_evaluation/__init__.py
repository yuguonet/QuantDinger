# -*- coding: utf-8 -*-
"""
stock_evaluation — 个股综合评估技能

兼容 Anthropic SKILL 标准。
整合多维度数据源，生成全面的投资分析报告。
评分（0-100）只是让数据更直观，核心价值在于综合评估。
"""
from .run import evaluate_stock, evaluate_stocks

__all__ = ["evaluate_stock", "evaluate_stocks"]
