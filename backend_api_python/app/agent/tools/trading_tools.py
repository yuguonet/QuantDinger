# -*- coding: utf-8 -*-
"""
Trading tools — start/stop strategies, list strategies, get details.

Wraps TradingExecutor and StrategyService into Agent-callable tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Tool functions ────────────────────────────────────────────

def list_strategies(user_id: int = 1) -> Dict[str, Any]:
    """列出用户的所有交易策略（含运行状态）。

    返回策略 ID、名称、类型、状态、交易对、时间框架等信息。

    Args:
        user_id: 用户 ID，默认 1
    """
    from app.services.strategy import StrategyService

    try:
        svc = StrategyService()
        items = svc.list_strategies(user_id=user_id)

        strategies = []
        for s in items or []:
            strategies.append({
                "id": s.get("id"),
                "name": s.get("name", ""),
                "strategy_type": s.get("strategy_type", ""),
                "status": s.get("status", ""),
                "symbol": s.get("symbol", ""),
                "market": s.get("market", ""),
                "timeframe": s.get("timeframe", ""),
                "created_at": str(s.get("created_at", "")),
            })

        return {"strategies": strategies, "count": len(strategies)}
    except Exception as e:
        logger.error("list_strategies failed: %s", e, exc_info=True)
        return {"strategies": [], "count": 0, "error": str(e)}


def get_strategy_detail(strategy_id: int, user_id: int = 1) -> Dict[str, Any]:
    """获取策略的详细配置信息。

    包含策略类型、交易配置、指标配置、运行状态等。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
    """
    from app.services.strategy import StrategyService

    try:
        svc = StrategyService()
        st = svc.get_strategy(strategy_id, user_id=user_id)
        if not st:
            return {"success": False, "error": f"策略 {strategy_id} 不存在"}

        # 清理敏感字段
        safe = dict(st)
        safe.pop("api_key", None)
        safe.pop("secret_key", None)
        safe.pop("passphrase", None)

        return {"success": True, "strategy": safe}
    except Exception as e:
        logger.error("get_strategy_detail failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def start_strategy(strategy_id: int, user_id: int = 1) -> Dict[str, Any]:
    """启动一个交易策略（开始实盘运行）。

    策略将按照配置的指标信号自动执行买卖操作。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
    """
    from app.services.strategy import StrategyService

    try:
        svc = StrategyService()
        st = svc.get_strategy(strategy_id, user_id=user_id)
        if not st:
            return {"success": False, "error": f"策略 {strategy_id} 不存在"}

        # 检查策略类型
        strategy_type = svc.get_strategy_type(strategy_id)
        if strategy_type == "PromptBasedStrategy":
            return {"success": False, "error": "AI 策略暂不支持直接启动，请使用指标策略"}

        # 更新状态
        svc.update_strategy_status(strategy_id, "running", user_id=user_id)

        # 启动执行器
        from app import get_trading_executor
        executor = get_trading_executor()
        success = executor.start_strategy(strategy_id)

        if not success:
            svc.update_strategy_status(strategy_id, "stopped", user_id=user_id)
            return {"success": False, "error": "策略执行器启动失败"}

        return {
            "success": True,
            "strategy_id": strategy_id,
            "strategy_name": st.get("name", ""),
            "message": "策略已启动",
        }
    except Exception as e:
        logger.error("start_strategy failed: %s", e, exc_info=True)
        return {"success": False, "error": f"启动失败: {e}"}


def stop_strategy(strategy_id: int, user_id: int = 1) -> Dict[str, Any]:
    """停止一个正在运行的交易策略。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
    """
    from app.services.strategy import StrategyService

    try:
        svc = StrategyService()
        st = svc.get_strategy(strategy_id, user_id=user_id)
        if not st:
            return {"success": False, "error": f"策略 {strategy_id} 不存在"}

        strategy_type = svc.get_strategy_type(strategy_id)
        if strategy_type == "PromptBasedStrategy":
            return {"success": False, "error": "AI 策略暂不支持"}

        # 停止执行器
        from app import get_trading_executor
        executor = get_trading_executor()
        executor.stop_strategy(strategy_id)

        # 更新状态
        svc.update_strategy_status(strategy_id, "stopped", user_id=user_id)

        return {
            "success": True,
            "strategy_id": strategy_id,
            "strategy_name": st.get("name", ""),
            "message": "策略已停止",
        }
    except Exception as e:
        logger.error("stop_strategy failed: %s", e, exc_info=True)
        return {"success": False, "error": f"停止失败: {e}"}


def get_strategy_trades(
    strategy_id: int,
    user_id: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """获取策略的最近交易记录。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
        limit: 返回条数，默认 20
    """
    from app.utils.db import get_db_connection

    limit = min(max(limit, 1), 100)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, symbol, type, price, amount, value, commission, profit, created_at "
                "FROM qd_strategy_trades "
                "WHERE strategy_id = %s ORDER BY id DESC LIMIT %s",
                (strategy_id, limit),
            )
            rows = cur.fetchall() or []
            cur.close()

        trades = []
        for r in rows:
            d = dict(r)
            if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            for k in ("price", "amount", "value", "commission", "profit"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            trades.append(d)

        return {"trades": trades, "count": len(trades)}
    except Exception as e:
        logger.error("get_strategy_trades failed: %s", e, exc_info=True)
        return {"trades": [], "count": 0, "error": str(e)}


# ── OpenAI tool declarations ─────────────────────────────────

TRADING_TOOLS = [
    {
        "fn": list_strategies,
        "name": "list_strategies",
        "description": (
            "列出用户的所有交易策略（含运行状态）。返回策略 ID、名称、类型、"
            "状态、交易对、时间框架。用于发现可用策略。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
            },
        },
    },
    {
        "fn": get_strategy_detail,
        "name": "get_strategy_detail",
        "description": "获取策略的详细配置信息（类型、交易对、指标、参数、状态等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_id": {
                    "type": "integer",
                    "description": "策略 ID",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
            },
            "required": ["strategy_id"],
        },
    },
    {
        "fn": start_strategy,
        "name": "start_strategy",
        "description": "启动一个交易策略，开始按指标信号自动执行买卖操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_id": {
                    "type": "integer",
                    "description": "策略 ID",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
            },
            "required": ["strategy_id"],
        },
    },
    {
        "fn": stop_strategy,
        "name": "stop_strategy",
        "description": "停止一个正在运行的交易策略。",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_id": {
                    "type": "integer",
                    "description": "策略 ID",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
            },
            "required": ["strategy_id"],
        },
    },
    {
        "fn": get_strategy_trades,
        "name": "get_strategy_trades",
        "description": "获取策略的最近交易记录，包含买卖价格、数量、盈亏等。",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_id": {
                    "type": "integer",
                    "description": "策略 ID",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 20",
                    "default": 20,
                },
            },
            "required": ["strategy_id"],
        },
    },
]
