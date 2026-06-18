# -*- coding: utf-8 -*-
"""
Skill Runner — 直接调用工具函数执行 Skill，返回 (SkillReport, EvalNode)。

不走 subprocess，直接 import app.agent.tools.* 中的工具函数。
每个 skill 的核心逻辑从 run.py 提取，转为可调用的 Python 函数。

对 _try_chain / ChainExecutor 提供统一接口：
  run_skill_fn(skill_name, stock_code, stock_name, context) → (SkillReport, EvalNode)

输出标准化：所有 skill 返回统一格式的 dict，由 _to_skill_report / _to_eval_node 转换。
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.agent.chain.schema import (
    EvalNode, FactorItem, Layer, SkillReport, Status,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Skill 函数注册表
# ═══════════════════════════════════════════════════════════════

def _run_technical(stock_code: str, stock_name: str) -> dict:
    """技术面综合分析（从 technical-agent/run.py 提取）。"""
    from app.agent.tools.analysis_tools import (
        analyze_trend, get_indicator_snapshot,
        get_volume_analysis, analyze_pattern, get_chip_distribution,
    )

    tool_results = {}
    for name, fn in [
        ("analyze_trend", lambda: analyze_trend(stock_code)),
        ("get_indicator_snapshot", lambda: get_indicator_snapshot(stock_code)),
        ("get_volume_analysis", lambda: get_volume_analysis(stock_code)),
        ("analyze_pattern", lambda: analyze_pattern(stock_code)),
        ("get_chip_distribution", lambda: get_chip_distribution(stock_code)),
    ]:
        try:
            tool_results[name] = fn()
        except Exception as e:
            tool_results[name] = {"error": str(e)}

    # ── 趋势评分（主权重 40%）──
    trend = tool_results.get("analyze_trend", {})
    trend_score = 50
    signals = []
    factors = []
    if isinstance(trend, dict) and "error" not in trend:
        trend_score = trend.get("trend_score", 50)
        trend_desc = trend.get("trend", "震荡")
        ma_align = trend.get("ma_alignment", "")
        bias_ma20 = trend.get("bias_ma20", 0)
        if bias_ma20 > 10:
            signals.append(f"偏离MA20达{bias_ma20:.1f}%，回调风险")
            trend_score = max(trend_score - 10, 0)
        elif bias_ma20 < -10:
            signals.append(f"偏离MA20达{bias_ma20:.1f}%，超跌反弹")
        if ma_align:
            signals.append(ma_align)
        factors.append({"name": "趋势", "value": trend_desc, "score": trend_score})
    else:
        factors.append({"name": "趋势", "value": "数据缺失", "score": 50})

    # ── 动量指标（权重 25%）──
    indicator = tool_results.get("get_indicator_snapshot", {})
    ind_score = 50
    if isinstance(indicator, dict) and "error" not in indicator:
        rsi_val = indicator.get("rsi6", 50)
        macd_hist = indicator.get("macd_hist", 0)
        kdj_j = indicator.get("kdj_j", 50)
        rsi_score = 50
        if rsi_val >= 80:
            rsi_score = 20; signals.append(f"RSI{rsi_val:.0f}超买")
        elif rsi_val >= 70:
            rsi_score = 30; signals.append(f"RSI{rsi_val:.0f}偏高")
        elif rsi_val <= 20:
            rsi_score = 80; signals.append(f"RSI{rsi_val:.0f}超卖")
        elif rsi_val <= 30:
            rsi_score = 70; signals.append(f"RSI{rsi_val:.0f}偏低")
        else:
            rsi_score = int(rsi_val)
        macd_score = 70 if macd_hist > 0.5 else (60 if macd_hist > 0 else (30 if macd_hist < -0.5 else 40))
        kdj_score = 25 if kdj_j >= 80 else (75 if kdj_j <= 20 else 50)
        ind_score = int(rsi_score * 0.35 + macd_score * 0.40 + kdj_score * 0.25)
        if macd_hist > 0 and rsi_val < 60:
            signals.append("MACD+RSI共振偏多")
        elif macd_hist < 0 and rsi_val > 40:
            signals.append("MACD+RSI共振偏空")
        factors.append({"name": "指标", "value": f"RSI{rsi_val:.0f}", "score": ind_score})
    else:
        factors.append({"name": "指标", "value": "数据缺失", "score": 50})

    # ── 量价分析（权重 20%）──
    volume = tool_results.get("get_volume_analysis", {})
    vol_score = 50
    if isinstance(volume, dict) and "error" not in volume:
        vol_relation = volume.get("vol_price_relation", "")
        volume_ratio = volume.get("volume_ratio", 1.0)
        if "量价齐升" in vol_relation: vol_score = 80
        elif "缩量上涨" in vol_relation: vol_score = 45
        elif "放量下跌" in vol_relation: vol_score = 20
        elif "缩量下跌" in vol_relation: vol_score = 55
        elif "放量滞涨" in vol_relation: vol_score = 25
        if volume_ratio > 3.0:
            signals.append(f"量比{volume_ratio}异动")
        elif volume_ratio > 2.0:
            signals.append(f"量比{volume_ratio}放量")
        factors.append({"name": "量价", "value": vol_relation or "平量", "score": vol_score})
    else:
        factors.append({"name": "量价", "value": "数据缺失", "score": 50})

    # ── 形态识别（权重 10%）──
    pattern = tool_results.get("analyze_pattern", {})
    pat_score = 50
    if isinstance(pattern, dict) and "error" not in pattern:
        patterns = pattern.get("patterns", [])
        if patterns:
            bullish = ["锤子线", "吞没", "早晨之星", "三连阳", "长下影线", "蜻蜓线", "突破", "大阳"]
            bearish = ["倒锤子", "墓碑线", "长上影线", "大阴线", "晚星", "三连阴", "跌破", "大阴"]
            for p in patterns:
                p_str = str(p)
                if any(bp in p_str for bp in bullish): pat_score = max(pat_score, 70); signals.append(p_str.split("（")[0])
                elif any(bp in p_str for bp in bearish): pat_score = min(pat_score, 30); signals.append(p_str.split("（")[0])
            factors.append({"name": "形态", "value": patterns[0].split("（")[0], "score": pat_score})
        else:
            factors.append({"name": "形态", "value": "无明显形态", "score": 50})
    else:
        factors.append({"name": "形态", "value": "数据缺失", "score": 50})

    # ── 筹码分布（附加参考）──
    chip = tool_results.get("get_chip_distribution", {})
    data_missing = False
    if isinstance(chip, dict) and "error" not in chip:
        concentration = chip.get("concentration", "")
        if concentration:
            signals.append(f"筹码{concentration}")
    else:
        data_missing = True

    # ── 综合评分 ──
    final_score = max(0, min(100, int(trend_score * 0.40 + ind_score * 0.25 + vol_score * 0.20 + pat_score * 0.10)))
    if isinstance(chip, dict) and "error" not in chip:
        profit_ratio = chip.get("profit_ratio")
        if profit_ratio is not None:
            final_score = max(0, min(100, final_score + (5 if profit_ratio < 20 else (-5 if profit_ratio > 80 else 0))))

    direction = "bullish" if final_score >= 60 else ("bearish" if final_score <= 40 else "neutral")
    confidence_val = round(min(sum(1 for f in factors if "缺失" not in str(f.get("value", ""))) / 4, 1.0), 2)
    momentum = "极强" if final_score >= 80 else ("强" if final_score >= 65 else ("中性" if final_score >= 45 else ("弱" if final_score >= 30 else "极弱")))

    return {
        "skill": "technical_agent", "action": "buy" if final_score >= 60 else ("sell" if final_score <= 40 else "hold"),
        "score": final_score, "direction": direction, "confidence": confidence_val,
        "signal": ",".join(signals[:5]) if signals else "无明显信号",
        "factors": factors, "status": "ok", "data_missing": data_missing,
        "analysis": f"动量评级:{momentum} 综合评分:{final_score}/100。趋势:{trend_score} 动量:{ind_score} 量价:{vol_score} 形态:{pat_score}",
    }


def _run_intelligence(stock_code: str, stock_name: str) -> dict:
    """情报分析（从 intelligence-agent/run.py 提取）。"""
    from app.agent.tools.intel_tools import (
        search_comprehensive_intel, get_eastmoney_stock_news,
        get_global_finance_news, get_consensus_eps,
    )
    from app.agent.tools.quote_tools import get_realtime_quote

    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
        ("comprehensive_intel", lambda: search_comprehensive_intel(stock_code)),
        ("stock_news", lambda: get_eastmoney_stock_news(stock_code)),
        ("global_news", lambda: get_global_finance_news()),
        ("consensus_eps", lambda: get_consensus_eps(stock_code)),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    # 简单评分：有情报数据 → 60，有负面 → 35，否则 50
    score = 50
    intel = data.get("comprehensive_intel", {})
    if isinstance(intel, dict) and "error" not in intel:
        items = intel.get("items", intel.get("results", []))
        if items:
            score = 60
    news = data.get("stock_news", {})
    if isinstance(news, dict) and "error" not in news:
        articles = news.get("articles", news.get("items", []))
        if articles:
            score = max(score, 55)

    return {
        "skill": "intelligence_agent", "score": score,
        "direction": "neutral", "confidence": 0.4,
        "signal": "情报数据已获取", "factors": [],
        "analysis": f"获取情报数据 {len([v for v in data.values() if isinstance(v, dict) and 'error' not in v])}/5 项",
        "status": "ok", "output_data": data,
    }


def _run_bear_researcher(stock_code: str, stock_name: str) -> dict:
    """空头研究（从 bear-researcher/run.py 提取）。"""
    from app.agent.tools.analysis_tools import (
        get_realtime_quote, analyze_trend, get_volume_analysis, get_indicator_snapshot,
    )
    from app.agent.tools.intel_tools import search_comprehensive_intel

    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
        ("trend", lambda: analyze_trend(stock_code)),
        ("volume", lambda: get_volume_analysis(stock_code)),
        ("indicator", lambda: get_indicator_snapshot(stock_code)),
        ("intel", lambda: search_comprehensive_intel(stock_code, query=f"{stock_name or stock_code} 利空 风险")),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    return {
        "skill": "bear_researcher", "score": 40, "direction": "bearish",
        "confidence": 0.5, "signal": "空头数据已获取", "factors": [],
        "analysis": "空头研究数据已获取，供决策参考", "status": "ok", "output_data": data,
    }


def _run_bull_researcher(stock_code: str, stock_name: str) -> dict:
    """多头研究（从 bull-researcher/run.py 提取）。"""
    from app.agent.tools.analysis_tools import (
        get_realtime_quote, analyze_trend, get_volume_analysis, get_indicator_snapshot,
    )
    from app.agent.tools.intel_tools import search_comprehensive_intel

    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
        ("trend", lambda: analyze_trend(stock_code)),
        ("volume", lambda: get_volume_analysis(stock_code)),
        ("indicator", lambda: get_indicator_snapshot(stock_code)),
        ("intel", lambda: search_comprehensive_intel(stock_code, query=f"{stock_name or stock_code} 利好 增长")),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    return {
        "skill": "bull_researcher", "score": 60, "direction": "bullish",
        "confidence": 0.5, "signal": "多头数据已获取", "factors": [],
        "analysis": "多头研究数据已获取，供决策参考", "status": "ok", "output_data": data,
    }


def _run_market_data(stock_code: str, stock_name: str) -> dict:
    """行情数据（从 market-data-agent 概念提取）。"""
    from app.agent.tools.quote_tools import get_realtime_quote, agent_get_kline
    from app.agent.tools.market_tools import get_market_indices, get_sector_rankings

    data = {}
    for name, fn in [
        ("realtime_quote", lambda: get_realtime_quote(stock_code)),
        ("kline", lambda: agent_get_kline(stock_code, timeframe="1D", days=30)),
        ("indices", lambda: get_market_indices()),
        ("sectors", lambda: get_sector_rankings()),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    return {
        "skill": "market_data_agent", "score": 50, "direction": "neutral",
        "confidence": 0.6, "signal": "行情数据已获取", "factors": [],
        "analysis": "行情/板块/指数数据已获取", "status": "ok", "output_data": data,
    }


def _run_hot_money(stock_code: str, stock_name: str) -> dict:
    """游资追踪（从 hot_money_tracker 概念提取）。"""
    from app.agent.tools.capital_tools import get_dragon_tiger_stocks, get_fund_flow

    data = {}
    for name, fn in [
        ("dragon_tiger", lambda: get_dragon_tiger_stocks(stock_code)),
        ("fund_flow", lambda: get_fund_flow(stock_code)),
    ]:
        try:
            data[name] = fn()
        except Exception as e:
            data[name] = {"error": str(e)}

    return {
        "skill": "hot_money_tracker", "score": 50, "direction": "neutral",
        "confidence": 0.4, "signal": "游资数据已获取", "factors": [],
        "analysis": "龙虎榜/资金流向数据已获取", "status": "ok", "output_data": data,
    }


def _run_screening(stock_code: str, stock_name: str) -> dict:
    """选股验证（从 screening_agent 概念提取）。"""
    from app.agent.tools.screening_tools import search_stock_by_name

    data = {}
    try:
        data["search"] = search_stock_by_name(stock_name or stock_code)
    except Exception as e:
        data["search"] = {"error": str(e)}

    return {
        "skill": "screening_agent", "score": 50, "direction": "neutral",
        "confidence": 0.3, "signal": "选股数据已获取", "factors": [],
        "analysis": "选股验证数据已获取", "status": "ok", "output_data": data,
    }


def _run_backtest(stock_code: str, stock_name: str) -> dict:
    """策略回测（从 backtest-agent/run.py 提取）。"""
    from app.agent.tools.backtest_tools import run_quick_backtest

    data = {}
    try:
        data["backtest"] = run_quick_backtest(stock_code)
    except Exception as e:
        data["backtest"] = {"error": str(e)}

    bt = data.get("backtest", {})
    score = bt.get("score", 50) if isinstance(bt, dict) else 50
    direction = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

    return {
        "skill": "backtest_agent", "score": score, "direction": direction,
        "confidence": 0.4, "signal": "回测完成", "factors": [],
        "analysis": f"回测评分 {score}", "status": "ok", "output_data": data,
    }


def _run_trading(stock_code: str, stock_name: str) -> dict:
    """交易执行（从 trading-agent/run.py 提取）。"""
    from app.agent.tools.trading_tools import get_positions

    data = {}
    try:
        data["positions"] = get_positions()
    except Exception as e:
        data["positions"] = {"error": str(e)}

    return {
        "skill": "trading_agent", "score": 50, "direction": "neutral",
        "confidence": 0.5, "signal": "持仓数据已获取", "factors": [],
        "analysis": "交易/持仓数据已获取", "status": "ok", "output_data": data,
    }


def _run_lockup(stock_code: str, stock_name: str) -> dict:
    """解禁监控（从 lockup_watcher 概念提取）。"""
    return {
        "skill": "lockup_watcher", "score": 50, "direction": "neutral",
        "confidence": 0.3, "signal": "解禁数据待获取", "factors": [],
        "analysis": "解禁/减持/质押数据待实现", "status": "ok",
    }


def _run_data(stock_code: str, stock_name: str) -> dict:
    """数据工程（通用数据获取）。"""
    from app.agent.tools.data_tools import agent_get_kline

    data = {}
    try:
        data["kline"] = agent_get_kline(stock_code, timeframe="1D", days=60)
    except Exception as e:
        data["kline"] = {"error": str(e)}

    return {
        "skill": "data_agent", "score": 50, "direction": "neutral",
        "confidence": 0.5, "signal": "数据已获取", "factors": [],
        "analysis": "K线数据已获取", "status": "ok", "output_data": data,
    }


def _run_indicator(stock_code: str, stock_name: str) -> dict:
    """指标策略执行（从 indicator_agent 概念提取）。"""
    return {
        "skill": "indicator_agent", "score": 50, "direction": "neutral",
        "confidence": 0.3, "signal": "指标策略待配置", "factors": [],
        "analysis": "用户自定义指标策略（需通过指标 IDE 配置）", "status": "ok",
    }


def _run_market_screener(stock_code: str, stock_name: str) -> dict:
    """全市场短线选股（从 market-screener/run.py 提取）。"""
    try:
        from app.agent.skills.market_screener import _select_strategy, _run_intraday, _run_eod, _run_post_market
        from app.agent.tools import registry as tool_registry
        tool_registry.discover()

        def call_tool_fn(tool_name, **kwargs):
            spec = tool_registry.get(tool_name)
            if not spec:
                raise ValueError(f"Unknown tool: {tool_name}")
            return spec.fn(**kwargs)

        strategy = _select_strategy()
        today = date.today().isoformat()

        if strategy == "intraday":
            report = _run_intraday("market_screener", today, call_tool_fn, [], [], [])
        elif strategy == "eod":
            report = _run_eod("market_screener", call_tool_fn, [], [], [])
        else:
            report = _run_post_market("market_screener", today, call_tool_fn, [], [], [])

        if report is None:
            return {"skill": "market_screener", "status": "failed", "error": "策略执行失败", "score": 0, "direction": "neutral", "confidence": 0}
        if hasattr(report, "to_dict"):
            d = report.to_dict()
            d.setdefault("skill", "market_screener")
            return d
        if isinstance(report, dict):
            report.setdefault("skill", "market_screener")
            return report
        return {"skill": "market_screener", "score": getattr(report, "score", 50),
                "direction": getattr(report, "direction", "neutral"),
                "signal": getattr(report, "signal", ""),
                "analysis": str(getattr(report, "analysis", ""))[:2000],
                "status": getattr(report, "status", "ok"), "confidence": 0.5, "factors": []}
    except Exception as e:
        logger.warning("[SkillRunner] market_screener 失败: %s", e)
        return {"skill": "market_screener", "status": "failed", "error": str(e), "score": 0, "direction": "neutral", "confidence": 0, "factors": []}


def _run_bb_screener(stock_code: str, stock_name: str) -> dict:
    """BB 超卖扫描（从 bb-screener/run.py 提取）。"""
    try:
        from app.agent.tools.screener_tools import bb_screener_scan
        result = bb_screener_scan()
        if isinstance(result, dict):
            result.setdefault("skill", "bb_screener")
            return result
        return {"skill": "bb_screener", "score": 50, "direction": "neutral",
                "confidence": 0.4, "signal": "BB扫描完成", "factors": [],
                "analysis": str(result)[:500], "status": "ok"}
    except Exception as e:
        logger.warning("[SkillRunner] bb_screener 失败: %s", e)
        return {"skill": "bb_screener", "status": "failed", "error": str(e), "score": 0, "direction": "neutral", "confidence": 0, "factors": []}


# ═══════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════

_SKILL_RUNNERS: Dict[str, Callable[[str, str], dict]] = {
    "technical_agent": _run_technical,
    "intelligence_agent": _run_intelligence,
    "bear_researcher": _run_bear_researcher,
    "bull_researcher": _run_bull_researcher,
    "market_data_agent": _run_market_data,
    "hot_money_tracker": _run_hot_money,
    "screening_agent": _run_screening,
    "backtest_agent": _run_backtest,
    "trading_agent": _run_trading,
    "lockup_watcher": _run_lockup,
    "data_agent": _run_data,
    "indicator_agent": _run_indicator,
    "market_screener": _run_market_screener,
    "bb_screener": _run_bb_screener,
}


# ═══════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════

def run_skill(
    skill_name: str,
    stock_code: str,
    stock_name: str = "",
    context: Dict[str, Any] = None,
) -> Tuple[SkillReport, EvalNode]:
    """执行单个 Skill，返回 (SkillReport, EvalNode)。

    签名兼容 ChainExecutor 的 run_skill_fn 接口。
    """
    runner = _SKILL_RUNNERS.get(skill_name)
    if not runner:
        logger.warning("[SkillRunner] 未知 Skill: %s (已知: %s)", skill_name, list(_SKILL_RUNNERS.keys()))
        return _make_error(skill_name, stock_code, stock_name, f"未知 Skill: {skill_name}")

    t0 = time.time()
    try:
        data = runner(stock_code, stock_name or "")
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.warning("[SkillRunner] %s 异常: %s (%.0fms)", skill_name, e, elapsed)
        return _make_error(skill_name, stock_code, stock_name, str(e), elapsed)

    elapsed = (time.time() - t0) * 1000

    if not isinstance(data, dict):
        return _make_error(skill_name, stock_code, stock_name, f"输出格式错误: {type(data)}", elapsed)

    # 确保 skill 字段
    data.setdefault("skill", skill_name)

    report = _to_skill_report(data, skill_name)
    node = _to_eval_node(data, skill_name, stock_code, stock_name, elapsed)

    logger.info(
        "[SkillRunner] %s | score=%s direction=%s status=%s | %.0fms",
        skill_name, report.score, report.direction, report.status, elapsed,
    )

    return report, node


def has_skill(skill_name: str) -> bool:
    """检查是否存在指定 Skill 的 runner。"""
    return skill_name in _SKILL_RUNNERS


def list_skills() -> List[str]:
    """列出所有已注册的 Skill 名称。"""
    return list(_SKILL_RUNNERS.keys())


# ═══════════════════════════════════════════════════════════════
# 内部转换
# ═══════════════════════════════════════════════════════════════

def _to_skill_report(data: dict, skill_name: str) -> SkillReport:
    """dict → SkillReport。"""
    factors = []
    for f in data.get("factors", []):
        if isinstance(f, dict):
            factors.append(FactorItem(
                name=f.get("name", ""), value=str(f.get("value", "")),
                score=f.get("score"), weight=f.get("weight", 1.0),
                status=f.get("status", "ok"),
            ))

    conf = data.get("confidence", 0.5)
    if isinstance(conf, str):
        conf = {"high": 0.8, "medium": 0.5, "low": 0.3}.get(conf, 0.5)
    conf = max(0.0, min(1.0, float(conf)))

    raw_status = data.get("status", "ok")
    status = "failed" if raw_status in ("failed", "error") else ("missing" if raw_status == "missing" else "ok")

    return SkillReport(
        skill_name=data.get("skill", skill_name),
        score=float(data.get("score", 50)),
        confidence=conf,
        direction=data.get("direction", "neutral"),
        signal=data.get("signal", ""),
        factors=factors,
        analysis=data.get("analysis", ""),
        output_data=data.get("output_data", data),
        tools_called=[],
        missing_data=[],
        status=status,
        error=data.get("error", ""),
    )


def _to_eval_node(data: dict, skill_name: str, stock_code: str, stock_name: str, elapsed_ms: float) -> EvalNode:
    """dict → EvalNode。"""
    factors = []
    for f in data.get("factors", []):
        if isinstance(f, dict):
            factors.append(FactorItem(
                name=f.get("name", ""), value=str(f.get("value", "")),
                score=f.get("score"), weight=f.get("weight", 1.0),
                status=f.get("status", "ok"),
            ))

    conf = data.get("confidence", 0.5)
    if isinstance(conf, str):
        conf = {"high": 0.8, "medium": 0.5, "low": 0.3}.get(conf, 0.5)
    conf = max(0.0, min(1.0, float(conf)))

    raw_status = data.get("status", "ok")
    if raw_status in ("failed", "error"):
        node_status = Status.FAILED.value
    elif raw_status == "missing":
        node_status = Status.MISSING.value
    else:
        node_status = Status.OK.value

    return EvalNode(
        layer=Layer.SKILL.value,
        name=data.get("skill", skill_name),
        exec_date=date.today(),
        stock_code=stock_code,
        stock_name=stock_name,
        score=float(data.get("score", 50)),
        direction=data.get("direction", "neutral"),
        action=data.get("action", ""),
        signal=data.get("signal", ""),
        confidence=conf,
        factors=factors,
        output_data=data.get("output_data", data),
        analysis=data.get("analysis", ""),
        tools_called=[],
        missing_data=[],
        status=node_status,
        error=data.get("error", ""),
        elapsed_ms=elapsed_ms,
    )


def _make_error(skill_name: str, stock_code: str, stock_name: str, error: str, elapsed_ms: float = 0):
    """构建错误态结果。"""
    report = SkillReport(skill_name=skill_name, score=0, confidence=0, direction="neutral", status="failed", error=error)
    node = EvalNode(layer=Layer.SKILL.value, name=skill_name, exec_date=date.today(),
                    stock_code=stock_code, stock_name=stock_name,
                    score=0, direction="neutral", status=Status.FAILED.value, error=error, elapsed_ms=elapsed_ms)
    return report, node
