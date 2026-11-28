# -*- coding: utf-8 -*-
"""
market-prediction/_model.py — 模型推理层

复用 watchlist.predict 的大盘预测逻辑，输出 P(T+1↑) 概率与 logit 分解。
结合四维因子做综合判断。

2026-11-27 v1.0: 初始版本
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def predict_market_direction(factors: Dict[str, Any]) -> Dict[str, Any]:
    """
    大盘方向预测：融合四维因子 + watchlist.predict 市场模型。
    
    Args:
        factors: compute_all_factors() 返回的四维因子字典
        
    Returns:
        dict: {
            "p_up": 0.0-1.0,           # P(大盘次日上涨)
            "logit": float,             # logit 值
            "direction": "bullish/neutral/bearish",
            "confidence": 0.0-1.0,      # 模型置信度
            "breakdown": {...},         # 因子贡献分解
            "exp_ret_bp": float,        # 预期收益(基点)
        }
    """
    try:
        # 1. 获取 watchlist.predict 的大盘基础分
        market_pred = _get_watchlist_market_prediction()
        
        # 2. 四维因子加权融合
        # 2026-11-28 修正：降级维度（error）不再混入"有效因子分"，避免 error→50
        # 造成假一致性与虚高置信度。只用可用维度重新归一加权。
        available = factors.get("available_dimensions")
        if available is None:
            # 兼容旧调用：按 details.error 推断
            available = [
                d for d in ("technical", "fund_flow", "sentiment", "valuation")
                if "error" not in factors.get(d, {}).get("details", {})
            ]
        degraded = factors.get("degraded_dimensions") or [
            d for d in ("technical", "fund_flow", "sentiment", "valuation") if d not in available
        ]
        if available:
            renorm = sum(float(factors.get(d, {}).get("weight", 0)) for d in available)
            factor_score = sum(
                float(factors.get(d, {}).get("score", 50)) * float(factors.get(d, {}).get("weight", 0))
                for d in available
            ) / renorm if renorm > 0 else 50.0
        else:
            factor_score = 50.0
        factor_p_up = factor_score / 100.0

        # 3. 融合权重：watchlist 模型 60% + 四维因子 40%
        # watchlist 模型是标定过的 Logistic，优先级更高；
        # 因子维度全降级时，模型权重升到 100%，不让空因子稀释信号。
        w_model = 0.6
        w_factor = 0.4
        if not available:
            w_model, w_factor = 1.0, 0.0

        model_p_up = market_pred.get("p_up", 0.5)
        model_logit = market_pred.get("logit", 0.0)

        fused_p_up = model_p_up * w_model + factor_p_up * w_factor
        fused_logit = model_logit * w_model + _p_to_logit(factor_p_up) * w_factor

        # 4. 方向判定
        if fused_p_up >= 0.55:
            direction = "bullish"
        elif fused_p_up <= 0.45:
            direction = "bearish"
        else:
            direction = "neutral"

        # 5. 置信度：一致性 + 因子完整度，且对降级维度直接惩罚
        consistency = 1.0 - abs(model_p_up - factor_p_up)
        factor_completeness = len(available) / 4.0
        if degraded:
            # 每降级一个维度，一致性按可用比例折扣（不再把 error→50 当成"一致"）
            consistency *= factor_completeness
        confidence = consistency * 0.6 + factor_completeness * 0.4
        confidence = max(0.1, min(0.95, confidence))
        
        # 6. 预期收益（基点）
        exp_ret_bp = _p_up_to_exp_ret_bp(fused_p_up)
        
        # 7. 贡献分解
        breakdown = {
            "model_p_up": round(model_p_up, 4),
            "model_logit": round(model_logit, 4),
            "factor_p_up": round(factor_p_up, 4),
            "factor_score": round(factor_score, 1),
            "consistency": round(consistency, 3),
            "factor_completeness": round(factor_completeness, 3),
            "available_dimensions": available,
            "degraded_dimensions": degraded,
            "weights": {"model": w_model, "factor": w_factor},
            "factor_details": {
                "technical": factors.get("technical", {}).get("score"),
                "fund_flow": factors.get("fund_flow", {}).get("score"),
                "sentiment": factors.get("sentiment", {}).get("score"),
                "valuation": factors.get("valuation", {}).get("score"),
            },
        }
        
        return {
            "p_up": round(fused_p_up, 4),
            "logit": round(fused_logit, 4),
            "direction": direction,
            "confidence": round(confidence, 3),
            "breakdown": breakdown,
            "exp_ret_bp": round(exp_ret_bp, 1),
        }
        
    except Exception as e:
        logger.error("[market-prediction] predict_market_direction failed: %s", e)
        return {
            "p_up": 0.5,
            "logit": 0.0,
            "direction": "neutral",
            "confidence": 0.3,
            "breakdown": {"error": str(e)},
            "exp_ret_bp": 0.0,
        }


def _get_watchlist_market_prediction() -> Dict[str, Any]:
    """调用 watchlist.predict 的大盘预测"""
    try:
        from app.watchlist.predict import load_market_klines, market_raw_features, market_score_from_features
        
        # 加载大盘 K 线
        market_klines = load_market_klines(120)
        if not market_klines:
            return {"p_up": 0.5, "logit": 0.0, "breakdown": {}}
        
        # 提取特征
        mfeats = market_raw_features(market_klines)
        
        # 模型预测
        mres = market_score_from_features(mfeats)
        
        return {
            "p_up": mres.get("p_up", 0.5),
            "logit": mres.get("logit", 0.0),
            "breakdown": mres.get("breakdown", {}),
            "features": {k: v for k, v in mfeats.items() if v is not None},
        }
        
    except Exception as e:
        logger.warning("[market-prediction] watchlist market prediction failed: %s", e)
        return {"p_up": 0.5, "logit": 0.0, "breakdown": {}}


def _p_to_logit(p: float) -> float:
    """概率转 logit"""
    import math
    p = max(0.001, min(0.999, p))
    return math.log(p / (1 - p))


def _p_up_to_exp_ret_bp(p_up: float) -> float:
    """P(涨)转预期收益基点"""
    # 同 watchlist.model.EXP_RET_BP_K = 45, CLIP = ±180
    k = 45.0
    clip = 180.0
    exp = k * (p_up - 0.5) * 2.0
    return max(-clip, min(clip, exp))


def _compute_factor_completeness(factors: Dict[str, Any]) -> float:
    """计算因子完整度（0-1）"""
    dims = ["technical", "fund_flow", "sentiment", "valuation"]
    complete = 0
    for dim in dims:
        detail = factors.get(dim, {}).get("details", {})
        if detail and "error" not in detail:
            complete += 1
    return complete / len(dims)


def assess_model_health() -> Dict[str, Any]:
    """
    评估模型健康度。
    
    Returns:
        dict: {
            "drift_alert": bool,
            "signal_decay": bool,
            "ic_rolling_mean": float,
            "last_retrain": str,
        }
    """
    try:
        # TODO: 对接实际的模型监控系统
        # 这里返回模拟值，实际应从 model_monitor 读取
        import json
        from pathlib import Path
        
        monitor_file = Path("data/market_prediction/model_health.json")
        if monitor_file.exists():
            with open(monitor_file, "r", encoding="utf-8") as f:
                return json.load(f)
        
        # 未接入真实监控系统时，如实标注数据状态；绝不把模拟值伪装成实测。
        return {
            "drift_alert": False,
            "signal_decay": False,
            "ic_rolling_mean": None,
            "last_retrain": "unknown",
            "feature_psi": None,
            "prediction_stability": None,
            "data_status": "unavailable",
            "note": "模型监控数据未接入（data/market_prediction/model_health.json 不存在）；健康指标为空，非实测",
        }
        
    except Exception as e:
        logger.warning("[market-prediction] model health check failed: %s", e)
        return {
            "drift_alert": True,
            "signal_decay": True,
            "ic_rolling_mean": None,
            "last_retrain": "unknown",
            "data_status": "error",
            "note": f"模型健康度读取失败：{e}",
        }