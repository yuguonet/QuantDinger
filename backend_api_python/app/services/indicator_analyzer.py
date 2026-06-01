# -*- coding: utf-8 -*-
"""
app/services/indicator_analyzer.py — 指标行为分析器

通过 KlineService 获取真实K线数据，在沙箱中运行指标代码，
提取行为统计和回测预览，生成结构化摘要供 Agent LLM 调用。

替代原 YAML 策略加载器，从数据库指标代码中提取策略信息。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ── K线数据获取 ──────────────────────────────────────────────

def _fetch_klines(
    symbol: str = "000001",
    market: str = "CNStock",
    timeframe: str = "1D",
    limit: int = 300,
) -> pd.DataFrame:
    """
    通过 KlineService 获取真实K线数据。
    获取失败或数据不足时抛出异常。
    """
    from app.services.kline import KlineService
    svc = KlineService()
    klines = svc.get_kline(market=market, symbol=symbol, timeframe=timeframe, limit=limit)
    if not klines or len(klines) < 20:
        raise ValueError(f"{symbol} K线数据不足（{len(klines) if klines else 0}条）")
    df = pd.DataFrame(klines)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype("float64")
    return df


# ── 沙箱执行 ─────────────────────────────────────────────────

def _execute_in_sandbox(
    code: str, df: pd.DataFrame, params: Dict[str, Any]
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any], Optional[str]]:
    """
    在安全沙箱中执行指标代码。

    Returns:
        (executed_df, exec_env_dict, error_msg)
    """
    from app.utils.safe_exec import build_safe_builtins, safe_exec_with_validation
    from app.services.indicator_params import IndicatorParamsParser, IndicatorCaller

    df_exec = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df_exec.columns:
            df_exec[col] = pd.to_numeric(df_exec[col], errors="coerce").fillna(0.0).astype("float64")

    signals = pd.Series(0, index=df_exec.index, dtype="float64")

    declared_params = IndicatorParamsParser.parse_params(code)
    merged_params = IndicatorParamsParser.merge_params(declared_params, params)

    # 空 IndicatorCaller（无跨指标依赖）
    indicator_caller = IndicatorCaller(user_id=0, current_indicator_id=None)

    exec_env: Dict[str, Any] = {
        "df": df_exec,
        "open": df_exec["open"],
        "high": df_exec["high"],
        "low": df_exec["low"],
        "close": df_exec["close"],
        "volume": df_exec["volume"],
        "signals": signals,
        "np": np,
        "pd": pd,
        "params": merged_params,
        "trading_config": {},
        "config": {},
        "cfg": {"risk": {}, "scale": {}, "position": {}},
        "call_indicator": indicator_caller.call_indicator,
        "leverage": 1.0,
        "initial_capital": 100000.0,
        "commission": 0.001,
        "trade_direction": "long",
        "initial_highest_price": 0.0,
        "initial_position": 0,
        "initial_avg_entry_price": 0.0,
        "initial_position_count": 0,
        "initial_last_add_price": 0.0,
    }
    exec_env["__builtins__"] = build_safe_builtins()

    # 兼容 pandas 2.0+
    compat_code = code
    compat_code = re.sub(r"\.fillna\(\s*method\s*=\s*['\"]ffill['\"]\s*\)", ".ffill()", compat_code)
    compat_code = re.sub(r"\.fillna\(\s*method\s*=\s*['\"]bfill['\"]\s*\)", ".bfill()", compat_code)

    result = safe_exec_with_validation(
        code=compat_code,
        exec_globals=exec_env,
        exec_locals=exec_env,
        timeout=30,
    )

    if not result.get("success"):
        return None, {}, result.get("error", "Unknown error")

    executed_df = exec_env.get("df", df_exec)
    return executed_df, exec_env, None


# ── 信号统计 ─────────────────────────────────────────────────

def _extract_signal_stats(df: pd.DataFrame) -> Dict[str, Any]:
    """从执行后的 DataFrame 提取 buy/sell 信号统计。"""
    stats: Dict[str, Any] = {
        "has_buy": False,
        "has_sell": False,
        "buy_count": 0,
        "sell_count": 0,
        "avg_hold_bars": 0.0,
        "signal_frequency": "无信号",
    }

    if "buy" not in df.columns or "sell" not in df.columns:
        return stats

    buy_mask = df["buy"].fillna(False).astype(bool)
    sell_mask = df["sell"].fillna(False).astype(bool)

    buy_count = int(buy_mask.sum())
    sell_count = int(sell_mask.sum())

    stats["has_buy"] = buy_count > 0
    stats["has_sell"] = sell_count > 0
    stats["buy_count"] = buy_count
    stats["sell_count"] = sell_count

    # 计算平均持仓周期
    if buy_count > 0 and sell_count > 0:
        buy_indices = list(df.index[buy_mask])
        sell_indices = list(df.index[sell_mask])
        hold_lengths = []
        for si in sell_indices:
            # 找到 sell 之前最近的 buy
            preceding_buys = [bi for bi in buy_indices if bi <= si]
            if preceding_buys:
                matched_buy = preceding_buys[-1]
                hold_len = si - matched_buy
                if hold_len > 0:
                    hold_lengths.append(hold_len)
        if hold_lengths:
            stats["avg_hold_bars"] = round(float(np.mean(hold_lengths)), 1)

    total_bars = len(df)
    total_signals = buy_count + sell_count
    if total_signals > 0 and total_bars > 0:
        freq = total_bars / total_signals
        stats["signal_frequency"] = f"每 {freq:.1f} 根K线一次"
    elif buy_count > 0:
        freq = total_bars / buy_count
        stats["signal_frequency"] = f"每 {freq:.1f} 根K线一次买入"
    else:
        stats["signal_frequency"] = "无信号"

    return stats


# ── 轻量回测 ─────────────────────────────────────────────────

def _lightweight_backtest(
    df: pd.DataFrame, strategy_config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    基于 buy/sell 信号的轻量回测，统计胜率、盈亏比、最大回撤。
    不依赖 BacktestService，纯向量化计算。
    """
    result: Dict[str, Any] = {
        "trades": 0,
        "win_rate": 0.0,
        "avg_profit_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "profit_factor": 0.0,
        "total_return_pct": 0.0,
    }

    if "buy" not in df.columns or "sell" not in df.columns:
        return result

    buy_mask = df["buy"].fillna(False).astype(bool).values
    sell_mask = df["sell"].fillna(False).astype(bool).values
    close = df["close"].values.astype("float64")
    open_prices = df["open"].values.astype("float64")

    # 模拟交易：buy → 下一根开盘买入，sell → 下一根开盘卖出
    trades: List[float] = []
    in_trade = False
    entry_price = 0.0
    stop_loss_pct = float(strategy_config.get("stopLossPct", 0) or 0)
    take_profit_pct = float(strategy_config.get("takeProfitPct", 0) or 0)

    for i in range(len(close) - 1):
        if not in_trade:
            if buy_mask[i]:
                entry_price = open_prices[i + 1]  # 下一根开盘价买入
                if entry_price > 0:
                    in_trade = True
        else:
            # 检查止盈止损
            current = close[i]
            pnl_pct = (current - entry_price) / entry_price

            should_exit = False
            if sell_mask[i]:
                should_exit = True
            elif stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
                should_exit = True
            elif take_profit_pct > 0 and pnl_pct >= take_profit_pct:
                should_exit = True

            if should_exit:
                exit_price = open_prices[i + 1] if i + 1 < len(close) else current
                final_pnl = (exit_price - entry_price) / entry_price
                trades.append(final_pnl)
                in_trade = False

    if not trades:
        return result

    trades_arr = np.array(trades)
    wins = trades_arr[trades_arr > 0]
    losses = trades_arr[trades_arr <= 0]

    result["trades"] = len(trades)
    result["win_rate"] = round(float(len(wins) / len(trades)), 4) if trades else 0
    result["avg_profit_pct"] = round(float(np.mean(trades_arr)) * 100, 2)
    result["total_return_pct"] = round(float(np.sum(trades_arr)) * 100, 2)

    if len(losses) > 0 and len(wins) > 0:
        avg_win = float(np.mean(wins))
        avg_loss = abs(float(np.mean(losses)))
        result["profit_factor"] = round(avg_win / avg_loss, 2) if avg_loss > 0 else 999.0

    # 最大回撤
    cumulative = np.cumsum(trades_arr)
    peak = np.maximum.accumulate(cumulative)
    drawdown = cumulative - peak
    if len(drawdown) > 0:
        result["max_drawdown_pct"] = round(float(np.min(drawdown)) * 100, 2)

    return result


# ── 指标值摘要 ────────────────────────────────────────────────

def _extract_indicator_values(
    output: Dict[str, Any], df: pd.DataFrame
) -> Dict[str, Any]:
    """从 output.plots 提取指标值摘要（范围、最新值）。"""
    summary: Dict[str, Any] = {}

    for p in output.get("plots", []):
        name = p.get("name", "unknown")
        data = p.get("data", [])
        if not data:
            continue

        # 过滤 None/NaN
        valid = [v for v in data if v is not None and not (isinstance(v, float) and np.isnan(v))]
        if not valid:
            continue

        numeric = [float(v) for v in valid if isinstance(v, (int, float))]
        if not numeric:
            continue

        summary[name] = {
            "latest": round(numeric[-1], 4),
            "min": round(min(numeric), 4),
            "max": round(max(numeric), 4),
            "mean": round(float(np.mean(numeric)), 4),
        }

    # 当前价格
    if len(df) > 0 and "close" in df.columns:
        summary["current_price"] = round(float(df["close"].iloc[-1]), 4)

    return summary


# ── 代码元信息提取 ─────────────────────────────────────────────

def _extract_code_meta(code: str) -> Dict[str, Any]:
    """从指标代码提取 name / description / @param / @strategy。"""
    from app.services.indicator_params import IndicatorParamsParser, StrategyConfigParser

    name_match = re.search(r"""^\s*my_indicator_name\s*=\s*(['"])(.*?)\1\s*$""", code, re.MULTILINE)
    desc_match = re.search(r"""^\s*my_indicator_description\s*=\s*(['"])(.*?)\1\s*$""", code, re.MULTILINE)

    name = name_match.group(2).strip() if name_match else ""
    description = desc_match.group(2).strip() if desc_match else ""

    params = IndicatorParamsParser.parse_params(code)
    strategy_config = StrategyConfigParser.parse(code)

    return {
        "name": name,
        "description": description,
        "params": params,
        "strategy_config": strategy_config,
    }


# ── 主入口：分析单个指标 ──────────────────────────────────────

def analyze_indicator(
    indicator_id: int,
    user_id: int = 1,
    symbol: str = "000001",
    market: str = "CNStock",
    timeframe: str = "1D",
    bars: int = 300,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    对单个指标进行完整的沙箱行为分析。

    流程：
    1. 从数据库加载指标代码
    2. 提取代码元信息（name, @param, @strategy）
    3. 通过 KlineService 获取真实K线数据
    4. 沙箱执行指标代码
    5. 提取信号统计
    6. 轻量回测
    7. 提取指标值摘要
    8. 生成 LLM 可用的结构化摘要

    Args:
        indicator_id: 指标 ID
        user_id: 用户 ID
        symbol: 用于分析的股票代码，默认 000001（平安银行）
        market: 市场类型，默认 CNStock
        timeframe: K线周期，默认 1D
        bars: K线条数，默认 300
        extra_params: 额外参数覆盖

    Returns:
        {
            "success": bool,
            "indicator_id": int,
            "name": str,
            "description": str,
            "params": [...],
            "strategy_config": {...},
            "behavioral_stats": {...},
            "backtest_preview": {...},
            "indicator_values": {...},
            "llm_summary": str,
            "error": str | None,
        }
    """
    from app.utils.db import get_db_connection

    # 1. 加载指标代码
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, code, description FROM qd_indicator_codes WHERE id = ?",
                (indicator_id,),
            )
            row = cur.fetchone()
            cur.close()
    except Exception as e:
        return {"success": False, "indicator_id": indicator_id, "error": f"数据库查询失败: {e}"}

    if not row:
        return {"success": False, "indicator_id": indicator_id, "error": f"指标 {indicator_id} 不存在"}

    code = (row.get("code") or "").strip()
    db_name = (row.get("name") or "").strip()
    db_desc = (row.get("description") or "").strip()

    if not code:
        return {"success": False, "indicator_id": indicator_id, "error": "指标代码为空"}

    # 2. 提取代码元信息
    meta = _extract_code_meta(code)
    indicator_name = meta["name"] or db_name or f"Indicator#{indicator_id}"
    description = meta["description"] or db_desc

    # 3. 获取K线数据
    try:
        df = _fetch_klines(symbol=symbol, market=market, timeframe=timeframe, limit=bars)
    except Exception as e:
        return {
            "success": False, "indicator_id": indicator_id,
            "name": indicator_name, "error": f"获取K线失败: {e}",
        }

    # 4. 沙箱执行
    params = dict(extra_params or {})
    executed_df, exec_env, error = _execute_in_sandbox(code, df, params)

    if error:
        return {
            "success": False,
            "indicator_id": indicator_id,
            "name": indicator_name,
            "error": f"沙箱执行失败: {error}",
            "code_meta": meta,
        }

    # 5. 信号统计
    signal_stats = _extract_signal_stats(executed_df)

    # 6. 轻量回测
    backtest_result = _lightweight_backtest(executed_df, meta["strategy_config"])

    # 7. 指标值摘要
    output = exec_env.get("output") or {}
    indicator_values = _extract_indicator_values(output, executed_df)

    # 8. 生成 LLM 摘要
    llm_summary = _build_llm_summary(
        name=indicator_name,
        description=description,
        params=meta["params"],
        strategy_config=meta["strategy_config"],
        signal_stats=signal_stats,
        backtest=backtest_result,
        indicator_values=indicator_values,
        indicator_id=indicator_id,
    )

    return {
        "success": True,
        "indicator_id": indicator_id,
        "name": indicator_name,
        "description": description,
        "params": meta["params"],
        "strategy_config": meta["strategy_config"],
        "behavioral_stats": signal_stats,
        "backtest_preview": backtest_result,
        "indicator_values": indicator_values,
        "llm_summary": llm_summary,
        "error": None,
    }


# ── 批量分析用户所有指标 ──────────────────────────────────────

def analyze_user_indicators(
    user_id: int = 1,
    symbol: str = "000001",
    market: str = "CNStock",
    timeframe: str = "1D",
    bars: int = 300,
) -> List[Dict[str, Any]]:
    """
    分析用户的所有指标，返回摘要列表。
    用于 Agent 策略选择界面和 LLM 上下文注入。

    Args:
        user_id: 用户 ID
        symbol: 用于分析的股票代码，默认 000001
        market: 市场类型
        timeframe: K线周期
        bars: K线条数
    """
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM qd_indicator_codes WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            )
            rows = cur.fetchall() or []
            cur.close()
    except Exception as e:
        logger.error("analyze_user_indicators: DB query failed: %s", e)
        return []

    results = []
    for row in rows:
        indicator_id = row["id"]
        try:
            analysis = analyze_indicator(
                indicator_id=indicator_id,
                user_id=user_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=bars,
            )
            results.append(analysis)
        except Exception as e:
            logger.warning("analyze_user_indicators: indicator %d failed: %s", indicator_id, e)
            results.append({
                "success": False,
                "indicator_id": indicator_id,
                "error": str(e),
            })

    return results


# ── LLM 摘要生成 ──────────────────────────────────────────────

def _build_llm_summary(
    name: str,
    description: str,
    params: List[Dict[str, Any]],
    strategy_config: Dict[str, Any],
    signal_stats: Dict[str, Any],
    backtest: Dict[str, Any],
    indicator_values: Dict[str, Any],
    indicator_id: Optional[int] = None,
) -> str:
    """生成给 LLM 的中文策略摘要文本。"""
    parts: List[str] = []

    if indicator_id is not None:
        parts.append(f"### 指标策略：{name}（ID: {indicator_id}）")
        parts.append(f"**重要**：调用 `run_indicator_signal` 时请使用 `indicator_id={indicator_id}`。")
    else:
        parts.append(f"### 指标策略：{name}")
    if description:
        parts.append(f"**描述**：{description}")

    # 参数
    if params:
        param_lines = []
        for p in params:
            ptype = p.get("type", "")
            default = p.get("default", "")
            pname = p.get("name", "")
            pdesc = p.get("description", "")
            param_lines.append(f"  - {pname} ({ptype}) 默认={default}" + (f" — {pdesc}" if pdesc else ""))
        parts.append("**参数**：\n" + "\n".join(param_lines))

    # 风控配置
    if strategy_config:
        risk_parts = []
        if strategy_config.get("stopLossPct"):
            risk_parts.append(f"止损 {strategy_config['stopLossPct']*100:.1f}%")
        if strategy_config.get("takeProfitPct"):
            risk_parts.append(f"止盈 {strategy_config['takeProfitPct']*100:.1f}%")
        if strategy_config.get("tradeDirection"):
            risk_parts.append(f"方向 {strategy_config['tradeDirection']}")
        if strategy_config.get("trailingEnabled"):
            risk_parts.append("追踪止损 启用")
        if risk_parts:
            parts.append("**风控**：" + "，".join(risk_parts))

    # 行为统计
    parts.append("**信号行为**：")
    parts.append(f"  - 买入信号 {signal_stats['buy_count']} 次，卖出信号 {signal_stats['sell_count']} 次")
    parts.append(f"  - 信号频率：{signal_stats['signal_frequency']}")
    if signal_stats["avg_hold_bars"] > 0:
        parts.append(f"  - 平均持仓 {signal_stats['avg_hold_bars']} 根K线")

    # 回测预览
    if backtest["trades"] > 0:
        parts.append("**回测预览**：")
        parts.append(f"  - 交易 {backtest['trades']} 笔，胜率 {backtest['win_rate']*100:.1f}%")
        parts.append(f"  - 平均收益 {backtest['avg_profit_pct']:+.2f}%，总收益 {backtest['total_return_pct']:+.2f}%")
        if backtest["profit_factor"] > 0:
            parts.append(f"  - 盈亏比 {backtest['profit_factor']:.2f}")
        if backtest["max_drawdown_pct"] < 0:
            parts.append(f"  - 最大回撤 {backtest['max_drawdown_pct']:.2f}%")
    else:
        parts.append("**回测预览**：无交易信号，无法回测")

    # 指标值
    if indicator_values:
        val_parts = []
        for k, v in indicator_values.items():
            if isinstance(v, dict) and "latest" in v:
                val_parts.append(f"{k}={v['latest']:.2f}")
        if val_parts:
            parts.append("**当前指标值**：" + "，".join(val_parts))

    return "\n".join(parts)


# ── Agent 专用：生成策略指令注入 ──────────────────────────────

def build_agent_skill_instructions(
    user_id: int = 1,
    indicator_ids: Optional[List[int]] = None,
    symbol: str = "000001",
    market: str = "CNStock",
    timeframe: str = "1D",
) -> str:
    """
    为 Agent 生成策略指令字符串，替代原 YAML 策略加载器。
    分析用户指标并生成 LLM 可理解的策略上下文。

    Args:
        user_id: 用户 ID
        indicator_ids: 指定指标 ID 列表，None 则加载用户全部指标
        symbol: 分析用的股票代码，默认 000001
        market: 市场类型，默认 CNStock
        timeframe: K线周期，默认 1D

    Returns:
        格式化的策略指令字符串（注入 system prompt）
    """
    if indicator_ids:
        analyses = []
        for iid in indicator_ids:
            try:
                a = analyze_indicator(
                    indicator_id=iid, user_id=user_id,
                    symbol=symbol, market=market, timeframe=timeframe,
                )
                analyses.append(a)
            except Exception as e:
                logger.warning("build_agent_skill_instructions: indicator %d failed: %s", iid, e)
    else:
        analyses = analyze_user_indicators(
            user_id=user_id, symbol=symbol, market=market, timeframe=timeframe,
        )

    # 过滤成功的分析
    valid = [a for a in analyses if a.get("success")]

    if not valid:
        return ""

    sections = []
    for a in valid:
        sections.append(a.get("llm_summary", ""))

    if not sections:
        return ""

    # 用户指定了策略时，强调当前激活策略
    if indicator_ids and len(valid) == 1:
        active_id = valid[0].get("indicator_id")
        active_name = valid[0].get("name", "")
        header = (
            f"\n## 当前激活策略（用户已选择）\n"
            f"**策略名称**：{active_name}\n"
            f"**指标 ID**：{active_id}\n\n"
            f"⚠️ 用户已在界面上选择了此策略，请直接使用 `run_indicator_signal(indicator_id={active_id}, ...)` "
            f"执行分析，无需再询问用户策略 ID。\n\n"
            f"### 策略详情\n"
        )
    else:
        header = (
            "\n## 当前可用的交易策略（基于指标IDE沙箱分析）\n"
            "以下策略已通过沙箱运行验证，包含真实K线数据的信号行为和回测预览：\n\n"
        )

    return (
        header
        + "\n\n---\n\n".join(sections)
        + "\n\n**使用说明**：以上数据基于真实K线的沙箱运行结果，"
        "可使用 `run_indicator_signal` 工具对具体股票执行指标获取实时信号。"
    )
