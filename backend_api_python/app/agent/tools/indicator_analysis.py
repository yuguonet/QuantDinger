# -*- coding: utf-8 -*-
"""用户指标策略分析 — 沙箱执行+信号衰减+冲突检测+历史胜率加权。"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def indicator_analysis(stock_code: str, stock_name: str = "", user_id: int = 1) -> Dict[str, Any]:
    """执行用户指标策略分析。

    Args:
        stock_code: 股票代码，如 "600066"
        stock_name: 股票名称，可选
        user_id: 用户 ID，默认 1

    流程：
      1. 加载用户指标列表
      2. 对每个指标：沙箱执行 + 信号统计 + 轻量回测
      3. 判断今日/明日是否有买卖点
      4. 有信号时：基础分 + 历史胜率/收益加权
      5. 无信号时：50分观望

    Returns:
        标准化 SkillReport dict
    """
    user_id = (context or {}).get("user_id", 1)

    # ── 1. 加载用户指标 ──
    indicators = _list_user_indicators(user_id)
    if not indicators:
        return _neutral("无用户指标", "用户未创建或购买任何指标策略")

    # ── 2. 逐个执行指标 + 回测 ──
    evaluated = []
    for ind in indicators[:5]:
        result = _evaluate_indicator(ind["id"], stock_code, user_id)
        if result:
            evaluated.append(result)

    if not evaluated:
        return _neutral("指标执行无结果", f"共 {len(indicators)} 个指标，执行均未产生有效信号")

    # ── 3. 筛选有近期信号的指标 ──
    active_buy = []   # 有买入信号（含衰减）
    active_sell = []   # 有卖出信号（含衰减）
    no_signal = []     # 无近期信号

    for ev in evaluated:
        if ev["has_recent_buy"]:
            active_buy.append(ev)
        elif ev["has_recent_sell"]:
            active_sell.append(ev)
        else:
            no_signal.append(ev)

    # ── 4. 无近期信号 → 50分观望 ──
    if not active_buy and not active_sell:
        return {
            "score": 50, "direction": "neutral",
            "confidence": 0.3,
            "signal": "无近期信号",
            "factors": _build_no_signal_factors(evaluated),
            "analysis": f"共 {len(evaluated)} 个指标，最近5天均无买卖信号",
            "status": "ok",
            "output_data": {"evaluated": evaluated},
        }

    # ── 5. 有信号 → 基础分 + 历史加权 + 衰减 ──
    factors = []
    total_score = 50.0
    total_weight = 0.0

    for ev in active_buy:
        score, conf, label = _calc_signal_score(ev, "buy")
        # 应用衰减（延迟信号 / 价格偏差 / 冲突）
        decay = ev.get("decay", 1.0)
        decay_reason = ev.get("decay_reason", "")
        score *= decay
        conf *= decay
        if decay_reason:
            label += f" [{decay_reason}]"
        total_score += score * conf
        total_weight += conf
        factors.append({
            "name": f"{ev['name']}:买入",
            "value": label,
            "score": int(score),
            "direction": "bullish",
            "confidence": round(conf, 2),
        })

    for ev in active_sell:
        score, conf, label = _calc_signal_score(ev, "sell")
        # 应用衰减
        decay = ev.get("decay", 1.0)
        decay_reason = ev.get("decay_reason", "")
        score *= decay
        conf *= decay
        if decay_reason:
            label += f" [{decay_reason}]"
        total_score -= score * conf
        total_weight += conf
        factors.append({
            "name": f"{ev['name']}:卖出",
            "value": label,
            "score": int(100 - score),
            "direction": "bearish",
            "confidence": round(conf, 2),
        })

    # 加权平均
    if total_weight > 0:
        final_score = max(0, min(100, total_score / total_weight))
    else:
        final_score = 50

    # 方向
    if active_buy and not active_sell:
        direction = "bullish"
    elif active_sell and not active_buy:
        direction = "bearish"
    elif len(active_buy) > len(active_sell):
        direction = "bullish"
    elif len(active_sell) > len(active_buy):
        direction = "bearish"
    else:
        direction = "neutral"

    # 置信度：基于历史胜率 + 信号数量
    avg_win_rate = _avg_win_rate(evaluated)
    signal_count = len(active_buy) + len(active_sell)
    confidence = min(1.0, max(0.3, avg_win_rate * 0.7 + (signal_count / 5) * 0.3))

    # 信号摘要
    signal = _build_signal_summary(active_buy, active_sell)

    # 无信号的指标也加到 factors（作为参考）
    for ev in no_signal:
        factors.append({
            "name": f"{ev['name']}:无信号", "value": "观望",
            "score": 50, "direction": "neutral",
        })

    # 衰减统计
    decayed = [e for e in active_buy + active_sell if e.get("decay", 1.0) < 1.0]
    decay_info = f"，{len(decayed)}个信号有衰减" if decayed else ""

    analysis = (
        f"共 {len(evaluated)} 个指标。"
        f"买入 {len(active_buy)} 个，卖出 {len(active_sell)} 个{decay_info}。"
        f"历史平均胜率 {avg_win_rate:.0%}。"
        f"综合: {direction} {final_score:.0f}分"
    )

    return {
        
        "score": round(final_score),
        "direction": direction,
        "confidence": round(confidence, 2),
        "signal": signal,
        "factors": factors,
        "analysis": analysis,
        "status": "ok",
        "output_data": {
            "indicators_total": len(indicators),
            "evaluated": len(evaluated),
            "active_buy": [e["name"] for e in active_buy],
            "active_sell": [e["name"] for e in active_sell],
            "no_signal": [e["name"] for e in no_signal],
        },
    }


# ═══════════════════════════════════════════════════════════════
# 单指标评估
# ═══════════════════════════════════════════════════════════════

def _evaluate_indicator(indicator_id: int, stock_code: str, user_id: int) -> Optional[Dict[str, Any]]:
    """对单个指标执行沙箱分析 + 回测，返回评估结果。"""
    from app.services.indicator_analyzer import analyze_indicator
    from app.agent.utils import detect_market
    
    market = detect_market(stock_code)

    # analyze_indicator：沙箱执行 + 信号统计 + 轻量回测
    result = analyze_indicator(
        indicator_id=indicator_id,
        user_id=user_id,
        symbol=stock_code,
        market=market,
        timeframe="1D",
        bars=300,
    )

    if not result.get("success"):
        logger.warning("[Indicator] 指标 %d 失败: %s", indicator_id, result.get("error"))
        return None

    signal_stats = result.get("behavioral_stats", {})
    backtest = result.get("backtest_preview", {})

    # run_indicator_signal：最近5根K线的逐根信号
    sig = run_indicator_signal(
        indicator_id=indicator_id,
        stock_code=stock_code,
        timeframe="1D",
        days=10,
        user_id=user_id,
    )

    if not sig.get("success"):
        return None

    last5_buy = sig.get("last5_buy", [False] * 5)    # [今天, 昨天, 前天, 大前天, 5天前]
    last5_sell = sig.get("last5_sell", [False] * 5)
    last5_close = sig.get("last5_close", [])          # 对应收盘价
    current_price = sig.get("current_price")

    # ── 今日/明日信号判断 ──
    has_today_buy = last5_buy[0] if last5_buy else False
    has_today_sell = last5_sell[0] if last5_sell else False

    # 前5天出现信号但今天没有 → 延迟信号（衰减）
    recent_buy_day = -1  # -1=无, 0=今天, 1=昨天, ...
    recent_sell_day = -1
    for i in range(5):
        if last5_buy[i]:
            recent_buy_day = i
            break
    for i in range(5):
        if last5_sell[i]:
            recent_sell_day = i
            break

    has_recent_buy = recent_buy_day >= 0
    has_recent_sell = recent_sell_day >= 0

    # ── 冲突检测：5天内同时有买卖信号 ──
    has_conflict = has_recent_buy and has_recent_sell

    # ── 价格偏差检测 ──
    signal_price = sig.get("buy_price") if has_recent_buy else sig.get("sell_price")
    price_deviation = 0.0
    if signal_price and current_price and signal_price > 0:
        price_deviation = abs(current_price - signal_price) / signal_price

    # ── 衰减计算 ──
    decay = 1.0
    decay_reason = ""

    if has_conflict:
        # 买卖冲突 → 大幅衰减
        decay *= 0.5
        decay_reason = "买卖信号冲突"
    elif has_recent_buy and not has_today_buy:
        # 买入信号不在今天 → 按天数衰减
        decay *= _bar_decay(recent_buy_day)
        decay_reason = f"买入信号{recent_buy_day}天前"
    elif has_recent_sell and not has_today_sell:
        # 卖出信号不在今天 → 按天数衰减
        decay *= _bar_decay(recent_sell_day)
        decay_reason = f"卖出信号{recent_sell_day}天前"

    # 价格偏差 > 5% → 追加衰减（信号可能已失效）
    if price_deviation > 0.10:
        decay *= 0.6
        decay_reason += f" 价格偏离{price_deviation:.0%}"
    elif price_deviation > 0.05:
        decay *= 0.8
        decay_reason += f" 价格偏离{price_deviation:.0%}"

    win_rate = backtest.get("win_rate", 0)
    total_return = backtest.get("total_return_pct", 0)
    trades = backtest.get("trades", 0)
    profit_factor = backtest.get("profit_factor", 0)

    return {
        "indicator_id": indicator_id,
        "name": result.get("name", f"指标{indicator_id}"),
        "has_today_buy": has_today_buy,
        "has_today_sell": has_today_sell,
        "has_recent_buy": has_recent_buy,
        "has_recent_sell": has_recent_sell,
        "has_conflict": has_conflict,
        "recent_buy_day": recent_buy_day,
        "recent_sell_day": recent_sell_day,
        "price_deviation": round(price_deviation, 4),
        "decay": round(decay, 2),
        "decay_reason": decay_reason.strip(),
        "buy_price": sig.get("buy_price"),
        "sell_price": sig.get("sell_price"),
        "current_price": current_price,
        "win_rate": win_rate,
        "total_return": total_return,
        "trades": trades,
        "profit_factor": profit_factor,
        "buy_count": signal_stats.get("buy_count", 0),
        "sell_count": signal_stats.get("sell_count", 0),
        "avg_hold_bars": signal_stats.get("avg_hold_bars", 0),
    }


def _bar_decay(days_ago: int) -> float:
    """按信号出现的天数返回衰减系数。

    今天(0)=1.0, 昨天(1)=0.95, 前天(2)=0.90, 3天前=0.85, 4天前=0.80
    """
    return max(0.70, 1.0 - days_ago * 0.05)


# ═══════════════════════════════════════════════════════════════
# 评分计算
# ═══════════════════════════════════════════════════════════════

def _calc_signal_score(ev: Dict[str, Any], signal_type: str) -> tuple:
    """计算单个指标信号的评分和置信度。

    Returns:
        (base_score, confidence, label)
        base_score: 0-100，信号强度
        confidence: 0.0-1.0，基于历史胜率/收益的置信度
        label: 可读说明
    """
    win_rate = ev.get("win_rate", 0)
    total_return = ev.get("total_return", 0)
    trades = ev.get("trades", 0)
    profit_factor = ev.get("profit_factor", 0)

    # 基础分：有信号就是 60 分起步
    base_score = 60

    # 历史胜率加权（核心）
    # 胜率 > 60% → 高置信
    # 胜率 40-60% → 中置信
    # 胜率 < 40% → 低置信
    if trades >= 5:  # 至少 5 笔交易才有统计意义
        if win_rate >= 0.7:
            confidence = 0.9
            base_score = 80
        elif win_rate >= 0.6:
            confidence = 0.75
            base_score = 70
        elif win_rate >= 0.5:
            confidence = 0.6
            base_score = 65
        elif win_rate >= 0.4:
            confidence = 0.45
            base_score = 55
        else:
            confidence = 0.3
            base_score = 45  # 胜率太低，降分
    elif trades >= 2:
        # 交易次数少，置信度打折
        confidence = max(0.3, win_rate * 0.6)
        base_score = 55
    else:
        # 无回测数据，最低置信
        confidence = 0.3
        base_score = 50

    # 收益加成
    if total_return > 20:
        base_score = min(100, base_score + 10)
    elif total_return > 10:
        base_score = min(100, base_score + 5)
    elif total_return < -10:
        base_score = max(0, base_score - 10)

    # 盈亏比加成
    if profit_factor >= 2.0:
        confidence = min(1.0, confidence + 0.1)
    elif profit_factor >= 1.5:
        confidence = min(1.0, confidence + 0.05)

    # 构建说明
    price = ev.get("buy_price") if signal_type == "buy" else ev.get("sell_price")
    price_str = f"价格{price:.2f}" if price else ""
    label = f"胜率{win_rate:.0%} 收益{total_return:+.1f}% {trades}笔 {price_str}"

    return base_score, confidence, label


def _avg_win_rate(evaluated: List[Dict]) -> float:
    """计算所有指标的平均胜率（有回测数据的）。"""
    rates = [e["win_rate"] for e in evaluated if e.get("trades", 0) >= 2]
    return sum(rates) / len(rates) if rates else 0.0


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _neutral(signal: str, analysis: str) -> Dict[str, Any]:
    """返回中性结果。"""
    return {
        "score": 50, "direction": "neutral",
        "confidence": 0.3, "signal": signal, "factors": [],
        "analysis": analysis, "status": "ok",
    }


def _build_no_signal_factors(evaluated: List[Dict]) -> List[Dict]:
    """构建无信号时的 factors。"""
    factors = []
    for ev in evaluated:
        # 附带历史胜率信息
        win_rate = ev.get("win_rate", 0)
        trades = ev.get("trades", 0)
        if trades >= 2:
            value = f"历史胜率{win_rate:.0%}({trades}笔)"
        else:
            value = "无近期信号"
        factors.append({
            "name": f"{ev['name']}:观望", "value": value,
            "score": 50, "direction": "neutral",
        })
    return factors


def _build_signal_summary(active_buy: List[Dict], active_sell: List[Dict]) -> str:
    """构建信号摘要。"""
    parts = []
    if active_buy:
        names = [e["name"] for e in active_buy[:3]]
        parts.append(f"买入:{','.join(names)}")
    if active_sell:
        names = [e["name"] for e in active_sell[:3]]
        parts.append(f"卖出:{','.join(names)}")
    return " | ".join(parts) if parts else "无信号"


def _list_user_indicators(user_id: int = 1) -> List[Dict[str, Any]]:
    """从 qd_indicator_codes 加载用户的指标列表。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, description FROM qd_indicator_codes "
                "WHERE user_id = %s AND (review_status = 'approved' OR review_status IS NULL) "
                "ORDER BY id DESC",
                (user_id,),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("[Indicator] 加载指标列表失败: %s", e)
        return []


# ── 内联自 indicator_tools.py ──

def run_indicator_signal(
    indicator_id: int,
    stock_code: str,
    timeframe: str = "1D",
    days: int = 60,
    user_id: int = 1,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对单只股票执行指标策略，返回最新的 buy/sell 信号和指标数据。

    拉取 K 线 → 沙箱执行指标代码 → 提取 output 中的信号和图表数据。

    Args:
        indicator_id: 指标策略 ID
        stock_code: 股票代码（如 600519, 000001, BTC/USDT）
        timeframe: K 线周期，默认 1D（可选 1H, 4H, 1W）
        days: 获取 K 线天数，默认 60
        user_id: 用户 ID，默认 1
        params: 指标参数覆盖（可选）
    """
    import pandas as pd
    import numpy as np
    from app.utils.db import get_db_connection
    from app.services.indicator_params import IndicatorParamsParser
    from app.utils.safe_exec import build_safe_builtins, safe_exec_with_validation
    from app.services.kline import KlineService
    from app.agent.utils import detect_market

    days = min(max(days, 10), 500)

    # 1. 加载指标代码
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT code, name FROM qd_indicator_codes "
                "WHERE id = %s AND (user_id = %s OR publish_to_community = 1)",
                (indicator_id, user_id),
            )
            row = cur.fetchone()
            cur.close()
        if not row:
            return {"success": False, "error": f"指标 {indicator_id} 不存在或无权限"}
        indicator_code = row.get("code") or ""
        indicator_name = row.get("name") or f"Indicator #{indicator_id}"
    except Exception as e:
        return {"success": False, "error": f"加载指标失败: {e}"}

    if not indicator_code.strip():
        return {"success": False, "error": "指标代码为空"}

    # 2. 获取 K 线
    market = detect_market(stock_code)
    try:
        kline_svc = KlineService()
        klines = kline_svc.get_kline(market=market, symbol=stock_code, timeframe=timeframe, limit=days)
        if not klines or len(klines) < 10:
            return {"success": False, "error": f"{stock_code} K线数据不足（{len(klines) if klines else 0}条）"}
    except Exception as e:
        return {"success": False, "error": f"获取K线失败: {e}"}

    # 3. 构建 DataFrame
    df = pd.DataFrame(klines)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 0.0

    # 4. 解析并合并参数
    declared_params = IndicatorParamsParser.parse_params(indicator_code)
    merged_params = IndicatorParamsParser.merge_params(declared_params, params or {})

    # 5. 沙箱执行
    exec_env = {
        "df": df.copy(),
        "pd": pd,
        "np": np,
        "params": merged_params,
        "output": None,
    }
    exec_env["__builtins__"] = build_safe_builtins()

    exec_result = safe_exec_with_validation(
        code=indicator_code,
        exec_globals=exec_env,
        exec_locals=exec_env,
        timeout=30,
    )

    if not exec_result.get("success"):
        return {
            "success": False,
            "error": f"指标执行失败: {exec_result.get('error', '未知错误')}",
            "indicator_name": indicator_name,
        }

    # 6. 提取结果
    executed_df = exec_env.get("df", df)
    output = exec_env.get("output") or {}

    # 提取 buy/sell 信号
    has_buy = False
    has_sell = False
    buy_price = None
    sell_price = None
    current_price = float(executed_df["close"].iloc[-1]) if len(executed_df) > 0 else None
    total_bars = len(executed_df)

    # 最近5根K线的逐根信号（用于衰减判断）
    last5_buy = [False] * 5   # [今天, 昨天, 前天, 大前天, 5天前]
    last5_sell = [False] * 5
    last5_close = []           # 对应收盘价

    if "buy" in executed_df.columns:
        buy_series = executed_df["buy"].astype(bool)
        if buy_series.any():
            has_buy = True
            last_buy_idx = buy_series[buy_series].index[-1]
            try:
                buy_price = float(executed_df.loc[last_buy_idx, "close"])
            except Exception:
                pass
        # 最近5根K线的 buy 信号
        for i in range(min(5, total_bars)):
            bar_idx = total_bars - 1 - i
            if bar_idx >= 0:
                last5_buy[i] = bool(buy_series.iloc[bar_idx])

    if "sell" in executed_df.columns:
        sell_series = executed_df["sell"].astype(bool)
        if sell_series.any():
            has_sell = True
            last_sell_idx = sell_series[sell_series].index[-1]
            try:
                sell_price = float(executed_df.loc[last_sell_idx, "close"])
            except Exception:
                pass
        # 最近5根K线的 sell 信号
        for i in range(min(5, total_bars)):
            bar_idx = total_bars - 1 - i
            if bar_idx >= 0:
                last5_sell[i] = bool(sell_series.iloc[bar_idx])

    # 最近5根K线的收盘价
    for i in range(min(5, total_bars)):
        bar_idx = total_bars - 1 - i
        if bar_idx >= 0:
            last5_close.append(float(executed_df["close"].iloc[bar_idx]))

    # 提取 output 中的图表数据（只取最后 10 个点，避免 token 爆炸）
    plots_summary = []
    for p in output.get("plots", []):
        plot_data = p.get("data", [])
        recent = plot_data[-10:] if len(plot_data) > 10 else plot_data
        plots_summary.append({
            "name": p.get("name", ""),
            "color": p.get("color", ""),
            "overlay": p.get("overlay", True),
            "recent_values": [round(v, 4) if isinstance(v, (int, float)) else v for v in recent],
        })

    signals_summary = []
    for s in output.get("signals", []):
        sig_data = s.get("data", [])
        non_null = [(i, v) for i, v in enumerate(sig_data[-20:]) if v is not None]
        signals_summary.append({
            "type": s.get("type", ""),
            "recent_signals": non_null[-5:] if non_null else [],
        })

    # 判断信号状态
    if has_buy and not has_sell:
        signal_status = "买入信号"
    elif has_sell and not has_buy:
        signal_status = "卖出信号"
    elif has_buy and has_sell:
        signal_status = "买卖信号均有（需判断先后）"
    else:
        signal_status = "无信号"

    return {
        "success": True,
        "stock_code": stock_code,
        "indicator_id": indicator_id,
        "indicator_name": indicator_name,
        "current_price": current_price,
        "has_buy": has_buy,
        "has_sell": has_sell,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "signal_status": signal_status,
        "plots": plots_summary,
        "signals": signals_summary,
        "data_points": len(executed_df),
        "last5_buy": last5_buy,     # [今天, 昨天, 前天, 大前天, 5天前]
        "last5_sell": last5_sell,
        "last5_close": last5_close,  # 对应收盘价
    }
