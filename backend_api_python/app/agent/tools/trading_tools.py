# -*- coding: utf-8 -*-
"""
Trading tools — start/stop strategies, list strategies, get details.

Wraps TradingExecutor and StrategyService into Agent-callable tools.
依赖：app.services.strategy, app.TradingExecutor
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ── 显式依赖检查 ──────────────────────────────────────────────
try:
    from app.services.strategy import StrategyService
    _TRADING_DEPS_OK = True
    _TRADING_DEPS_ERROR = None
except ImportError as _e:
    _TRADING_DEPS_OK = False
    _TRADING_DEPS_ERROR = str(_e)
    logger.warning("[trading_tools] 依赖缺失: %s — 交易功能不可用", _e)


# ── Tool functions ────────────────────────────────────────────

@tool(
    description="列出用户的所有交易策略（含运行状态）。返回策略 ID、名称、类型、状态、交易对、时间框架。用于发现可用策略。",
    category="交易",
)
def list_strategies(user_id: int = 1) -> Dict[str, Any]:
    """列出用户的所有交易策略（含运行状态）。

    返回策略 ID、名称、类型、状态、交易对、时间框架等信息。

    Args:
        user_id: 用户 ID，默认 1
    """
    if not _TRADING_DEPS_OK:
        return {"strategies": [], "count": 0, "error": f"交易依赖缺失: {_TRADING_DEPS_ERROR}"}

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


@tool(
    description="获取策略的详细配置信息（类型、交易对、指标、参数、状态等）。",
    category="交易",
)
def get_strategy_detail(strategy_id: int, user_id: int = 1) -> Dict[str, Any]:
    """获取策略的详细配置信息。

    包含策略类型、交易配置、指标配置、运行状态等。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
    """
    if not _TRADING_DEPS_OK:
        return {"success": False, "error": f"交易依赖缺失: {_TRADING_DEPS_ERROR}"}

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


@tool(
    description="启动一个交易策略，开始按指标信号自动执行买卖操作。",
    category="交易",
)
def start_strategy(strategy_id: int, user_id: int = 1) -> Dict[str, Any]:
    """启动一个交易策略（开始实盘运行）。

    策略将按照配置的指标信号自动执行买卖操作。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
    """
    if not _TRADING_DEPS_OK:
        return {"success": False, "error": f"交易依赖缺失: {_TRADING_DEPS_ERROR}"}

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
        try:
            from app import get_trading_executor
            executor = get_trading_executor()
        except ImportError as ie:
            svc.update_strategy_status(strategy_id, "stopped", user_id=user_id)
            return {"success": False, "error": f"交易执行器不可用: {ie}"}
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


@tool(
    description="停止一个正在运行的交易策略。",
    category="交易",
)
def stop_strategy(strategy_id: int, user_id: int = 1) -> Dict[str, Any]:
    """停止一个正在运行的交易策略。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
    """
    if not _TRADING_DEPS_OK:
        return {"success": False, "error": f"交易依赖缺失: {_TRADING_DEPS_ERROR}"}

    try:
        svc = StrategyService()
        st = svc.get_strategy(strategy_id, user_id=user_id)
        if not st:
            return {"success": False, "error": f"策略 {strategy_id} 不存在"}

        strategy_type = svc.get_strategy_type(strategy_id)
        if strategy_type == "PromptBasedStrategy":
            return {"success": False, "error": "AI 策略暂不支持"}

        # 停止执行器
        try:
            from app import get_trading_executor
            executor = get_trading_executor()
        except ImportError as ie:
            return {"success": False, "error": f"交易执行器不可用: {ie}"}
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


@tool(
    description="获取策略的最近交易记录，包含买卖价格、数量、盈亏等。",
    category="交易",
)
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

# Legacy list — kept for backward compat during migration; safe to remove later.
TRADING_TOOLS = []
