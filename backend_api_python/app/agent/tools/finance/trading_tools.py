# -*- coding: utf-8 -*-
"""
Trading tools — start/stop strategies, list strategies, get details.

Wraps TradingExecutor and StrategyService into Agent-callable tools.
依赖：app.services.strategy, app.TradingExecutor
"""
from __future__ import annotations

import json
from app.agent.log import logger
from typing import Any, Dict, List, Optional
from app.agent.utils.md_format import _to_md

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

def list_strategies(user_id: int = 1) -> dict:
    """策略列表：返回用户所有策略的ID、名称、类型、运行状态、市场。

    Returns:
        dict: {strategies, count, error}。策略列表在 result['strategies']（list），
        每项含 id/name/status；依赖缺失或异常时 strategies 为空且含 error。

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

def get_strategy_detail(strategy_id: int, user_id: int = 1) -> dict:
    """策略详情：返回指定策略的参数配置、持仓、触发条件。

    Returns:
        dict: {success, strategy, error}。策略字段在 result['strategy']（dict，已剔除
        密钥），失败时 success=False 且只有 error。

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

def start_strategy(strategy_id: int, user_id: int = 1, confirm: bool = False) -> dict:
    """启动策略：将指定策略从停止状态切换为运行状态。**真实资金动作**。

    策略将按照配置的指标信号自动执行买卖操作；订单由 PendingOrderWorker +
    app.services.live_trading **实盘成交**（各所直连 REST，见 trading_executor.py 头注释）。

    【human-in-the-loop 硬闸（2026-09-24 提智 0.1）】调用前**必须**把策略名与“将按信号
    自动实盘买卖”告知用户、获得明确同意，再以 confirm=True 重调；未确认只返回
    requires_confirmation（展示信息，不执行）。每次启动/停止均审计留痕。

    Returns:
        成功 → {"success": True, "strategy_id", "strategy_name", "message"}；
        未确认 → {"success": False, "requires_confirmation": True, "strategy_id", "strategy_name"}；
        失败 → {"success": False, "error": str}。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
        confirm: 是否已获用户明确同意（默认 False = 只展示、不执行）
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

        # 人工确认硬闸（2026-09-24）：真实资金动作，模型不得自行拍板；审计留痕
        if not confirm:
            logger.warning("[Trading][AUDIT] start_strategy 未确认拒绝 user_id=%s strategy_id=%s name=%s",
                           user_id, strategy_id, st.get("name", ""))
            return {
                "success": False,
                "requires_confirmation": True,
                "strategy_id": strategy_id,
                "strategy_name": st.get("name", ""),
                "message": ("启动后将按指标信号自动执行买卖并**实盘成交**（真实资金动作）。"
                            "请先向用户说明风险并获得明确同意，再以 confirm=true 重新调用。"),
            }
        logger.warning("[Trading][AUDIT] start_strategy 确认执行 user_id=%s strategy_id=%s name=%s",
                       user_id, strategy_id, st.get("name", ""))

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

        return {"success": True, "strategy_id": strategy_id, "strategy_name": st.get("name", ""), "message": "策略已启动"}
    except Exception as e:
        logger.error("start_strategy failed: %s", e, exc_info=True)
        return {"success": False, "error": f"启动失败: {e}"}

def stop_strategy(strategy_id: int, user_id: int = 1) -> dict:
    """停止策略：将指定策略从运行状态切换为停止状态。

    Returns:
        成功 → {"success": True, "strategy_id", "strategy_name", "message"}；
        失败 → {"success": False, "error": str}。

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

        # 审计留痕（2026-09-24 提智 0.1）。刻意**不加**确认闸：停止是风控/kill 动作，
        # 加闸会拖慢离场（安全优先于对称性；对照方案 0.1 字面的安全修正）。
        logger.warning("[Trading][AUDIT] stop_strategy 执行 user_id=%s strategy_id=%s name=%s",
                       user_id, strategy_id, st.get("name", ""))

        # 停止执行器
        try:
            from app import get_trading_executor
            executor = get_trading_executor()
        except ImportError as ie:
            return {"success": False, "error": f"交易执行器不可用: {ie}"}
        executor.stop_strategy(strategy_id)

        # 更新状态
        svc.update_strategy_status(strategy_id, "stopped", user_id=user_id)

        return {"success": True, "strategy_id": strategy_id, "strategy_name": st.get("name", ""), "message": "策略已停止"}
    except Exception as e:
        logger.error("stop_strategy failed: %s", e, exc_info=True)
        return {"success": False, "error": f"停止失败: {e}"}

def get_strategy_trades(
    strategy_id: int,
    user_id: int = 1,
    limit: int = 20,
) -> dict:
    """策略交易记录：返回指定策略最近的买入/卖出记录。

    Returns:
        dict: {trades, count, error}。交易记录在 result['trades']（list），每项含
        type/price/amount/profit；查询异常时 trades 为空且含 error。

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

