# -*- coding: utf-8 -*-
"""
market-prediction/run.py — A股大盘择时预测技能入口

设计原则：
1. 纯本地数据源（market_cn + watchlist.predict），无外部依赖
2. 输出标准化 SkillReport，供其他技能复用
3. 四维因子（技术/资金/情绪/估值）+ 模型推理 + 报告生成
4. 兼容 Anthropic SKILL 标准，SKILL.md 零配置插件

2026-11-27 v1.0: 初始版本
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ._factors import compute_all_factors
from ._model import predict_market_direction, assess_model_health
from ._report import build_skill_report
from .common import SkillResult

logger = logging.getLogger(__name__)


def predict_market() -> Dict[str, Any]:
    """
    A股大盘择时预测主入口。
    
    Returns:
        dict: 标准化 SkillReport，含 score/confidence/direction/factors/position_sizing/horizons/model_health/analysis/key_levels/risk_flags
    """
    try:
        # 1. 计算四维因子
        factors = compute_all_factors()
        
        # 2. 模型推理：大盘方向 + 概率
        prediction = predict_market_direction(factors)
        
        # 3. 模型健康度评估
        health = assess_model_health()
        
        # 4. 组装完整报告
        report = build_skill_report(
            factors=factors,
            prediction=prediction,
            health=health,
        )
        
        return SkillResult(report)
        
    except Exception as e:
        logger.error("[market-prediction] predict_market 失败: %s", e, exc_info=True)
        return SkillResult({
            "error": str(e),
            "score": 50,
            "confidence": 0.0,
            "direction": "neutral",
            "factors": {},
            "position_sizing": {"current_tier": "light", "tiers": []},
            "horizons": {},
            "model_health": {"drift_alert": True, "signal_decay": True},
            "analysis": f"预测失败: {e}",
            "key_levels": {"resistance": [], "support": []},
            "risk_flags": ["预测模块异常"],
        })


def get_factors_only() -> Dict[str, Any]:
    """
    仅获取四维因子详情（供调试/回测用）。
    
    Returns:
        dict: 四维因子原始分数与明细
    """
    try:
        factors = compute_all_factors()
        return SkillResult(factors)
    except Exception as e:
        logger.warning("[market-prediction] get_factors_only 失败: %s", e)
        return SkillResult({"error": str(e)})


# ================================================================
# 技能注册元数据（供 run_skill 自动发现）
# ================================================================

__all__ = ["predict_market", "get_factors_only"]

# 入口函数签名对应 SKILL.md 的 triggers
# run_skill("market-prediction", {}) 会调用 predict_market()