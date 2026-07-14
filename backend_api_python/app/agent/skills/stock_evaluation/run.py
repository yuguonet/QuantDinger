# -*- coding: utf-8 -*-
"""
stock_evaluation — 个股综合评估技能 v3.0

流程：解析需求 → 执行代码生成标准输出 → LLM 综合分析 → 输出

只把不支持多股的工具搬到技能内，支持多股的直接调用外部工具。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════

DEFAULT_WEIGHTS = {
    "technical": 0.40,
    "fund_flow": 0.20,
    "capital": 0.15,
    "news": 0.10,
    "valuation": 0.10,
    "liquidity": 0.05,
}

PERIOD_WEIGHTS = {
    "T+1": {"technical": 0.50, "fund_flow": 0.25, "news": 0.15, "capital": 0.05, "valuation": 0.05},
    "T+3": {"technical": 0.40, "fund_flow": 0.20, "news": 0.10, "capital": 0.15, "valuation": 0.10, "liquidity": 0.05},
    "T+5": {"technical": 0.35, "fund_flow": 0.20, "capital": 0.15, "valuation": 0.15, "news": 0.10, "liquidity": 0.05},
    "1W": {"technical": 0.30, "fund_flow": 0.20, "capital": 0.20, "valuation": 0.15, "news": 0.10, "liquidity": 0.05},
    "1M": {"technical": 0.25, "capital": 0.25, "valuation": 0.20, "fund_flow": 0.15, "news": 0.10, "liquidity": 0.05},
}


# ═══════════════════════════════════════════════════════════════
#  私有函数：只搬不支持多股的工具，改为多股版本
# ═══════════════════════════════════════════════════════════════

def _technical_analysis_multi(codes: str) -> Dict[str, Any]:
    """技术面分析（多股版本）。"""
    results = {}
    for code in codes.split(","):
        code = code.strip()
        if not code:
            continue
        try:
            from tools.technical_analysis import technical_analysis
            results[code] = technical_analysis(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"data": results} if len(results) > 1 else results.get(codes.split(",")[0].strip(), {})


def _get_indicator_snapshot_multi(codes: str) -> Dict[str, Any]:
    """指标快照（多股版本）。"""
    results = {}
    for code in codes.split(","):
        code = code.strip()
        if not code:
            continue
        try:
            from tools.analysis_tools import get_indicator_snapshot
            results[code] = get_indicator_snapshot(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"data": results} if len(results) > 1 else results.get(codes.split(",")[0].strip(), {})


def _get_volume_analysis_multi(codes: str) -> Dict[str, Any]:
    """量价分析（多股版本）。"""
    results = {}
    for code in codes.split(","):
        code = code.strip()
        if not code:
            continue
        try:
            from tools.analysis_tools import get_volume_analysis
            results[code] = get_volume_analysis(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"data": results} if len(results) > 1 else results.get(codes.split(",")[0].strip(), {})


def _analyze_trend_multi(codes: str) -> Dict[str, Any]:
    """趋势分析（多股版本）。"""
    results = {}
    for code in codes.split(","):
        code = code.strip()
        if not code:
            continue
        try:
            from tools.analysis_tools import analyze_trend
            results[code] = analyze_trend(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"data": results} if len(results) > 1 else results.get(codes.split(",")[0].strip(), {})


def _get_capital_summary_multi(codes: str) -> Dict[str, Any]:
    """资本结构（多股版本）。"""
    results = {}
    for code in codes.split(","):
        code = code.strip()
        if not code:
            continue
        try:
            from tools.capital_tools import get_capital_summary
            results[code] = get_capital_summary(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"data": results} if len(results) > 1 else results.get(codes.split(",")[0].strip(), {})


# ═══════════════════════════════════════════════════════════════
#  直接调用外部工具（已支持多股）
# ═══════════════════════════════════════════════════════════════

def _get_realtime_quote(codes: str) -> Dict[str, Any]:
    """实时行情（直接调用，已支持多股）。"""
    try:
        from tools.data_tools import get_realtime_quote
        return get_realtime_quote(codes)
    except Exception as e:
        return {"error": str(e)}


def _get_fund_flow(codes: str) -> Dict[str, Any]:
    """资金流向（直接调用，已支持多股）。"""
    try:
        from tools.fund_flow_tools import get_fund_flow
        return get_fund_flow(codes)
    except Exception as e:
        return {"error": str(e)}


def _get_stock_info(codes: str, detail: bool = True) -> Dict[str, Any]:
    """股票信息（直接调用，已支持多股）。"""
    try:
        from tools.data_tools import get_stock_info
        return get_stock_info(codes, detail=detail)
    except Exception as e:
        return {"error": str(e)}


def _search_stock_intel(codes: str, name: str = "") -> Dict[str, Any]:
    """新闻情报（直接调用，已支持多股）。"""
    try:
        from tools.news_search_tools import search_stock_intel
        return search_stock_intel(codes, name=name)
    except Exception as e:
        return {"error": str(e)}


def _web_search(query: str) -> Dict[str, Any]:
    """联网搜索（直接调用）。"""
    try:
        from tools.web_search_tools import web_search
        return web_search(query, count=3, freshness="pw")
    except Exception as e:
        return {"error": str(e)}


def _stock_report(info: Dict, technical: Dict, capital: Dict, quote: Dict, fund_flow: Dict) -> Dict[str, Any]:
    """生成 stock_report（技能内版本）。"""
    try:
        from .stock_report import stock_report
        return stock_report(info=info, technical=technical, capital=capital, quote=quote, fund_flow=fund_flow)
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
#  Step 1: 解析用户需求
# ═══════════════════════════════════════════════════════════════

def parse_user_input(user_input: str) -> Dict[str, Any]:
    """解析用户输入，提取参数。"""
    import re

    codes = re.findall(r'\b(\d{6})\b', user_input)
    codes_str = ",".join(codes) if codes else ""

    period = "T+3"
    for p in ["T+1", "T+3", "T+5", "1W", "1M"]:
        if p in user_input.upper():
            period = p
            break

    depth = "standard"
    if any(kw in user_input for kw in ["快速", "简单", "L1"]):
        depth = "simple"
    elif any(kw in user_input for kw in ["深度", "详细", "L3"]):
        depth = "deep"
    elif any(kw in user_input for kw in ["完整", "全面", "L4"]):
        depth = "complete"

    return {
        "codes": codes_str,
        "period": period,
        "depth": depth,
        "is_multi": len(codes) > 1,
    }


# ═══════════════════════════════════════════════════════════════
#  Step 2: 执行代码生成标准输出
# ═══════════════════════════════════════════════════════════════

def _fetch_data_single(code: str, depth: str, stock_name: str = "") -> Dict[str, Dict[str, Any]]:
    """单股一趟水获取数据。"""
    tools = []

    # L1: 技术面（私有函数，多股版本）
    tools.append(("technical_analysis", lambda: _technical_analysis_multi(code)))

    # L2: +资金面+行情
    if depth in ["standard", "deep", "complete"]:
        tools.extend([
            ("get_realtime_quote", lambda: _get_realtime_quote(code)),
            ("get_indicator_snapshot", lambda: _get_indicator_snapshot_multi(code)),
            ("get_volume_analysis", lambda: _get_volume_analysis_multi(code)),
            ("analyze_trend", lambda: _analyze_trend_multi(code)),
            ("get_capital_summary", lambda: _get_capital_summary_multi(code)),
            ("get_fund_flow", lambda: _get_fund_flow(code)),
        ])

    # L3: +基本面+新闻
    if depth in ["deep", "complete"]:
        tools.extend([
            ("get_stock_info", lambda: _get_stock_info(code, detail=True)),
            ("search_stock_intel", lambda: _search_stock_intel(code, stock_name)),
        ])

    # L4: +web_search
    if depth == "complete":
        tools.append(("web_search", lambda: _web_search(f"{stock_name} {code} 最新消息")))

    results = {}

    def _call_one(item):
        tool_name, fn = item
        return tool_name, fn()

    with ThreadPoolExecutor(max_workers=min(len(tools), 8)) as executor:
        futures = [executor.submit(_call_one, t) for t in tools]
        for future in futures:
            try:
                tool_name, result = future.result(timeout=30)
                results[tool_name] = result
            except Exception as e:
                logger.error("[StockEval] 并行调用异常: %s", e)

    return results


def _check_data_warnings(tool_results: Dict[str, Dict[str, Any]], depth: str) -> str:
    """检查当前深度实际调用的工具，返回未获取或错误的数据列表。"""
    # 按深度定义实际调用的工具
    depth_tools = {
        "simple": ["technical_analysis"],
        "standard": ["technical_analysis", "get_realtime_quote", "get_indicator_snapshot",
                     "get_volume_analysis", "analyze_trend", "get_capital_summary", "get_fund_flow"],
        "deep": ["technical_analysis", "get_realtime_quote", "get_indicator_snapshot",
                 "get_volume_analysis", "analyze_trend", "get_capital_summary", "get_fund_flow",
                 "get_stock_info", "search_stock_intel"],
        "complete": ["technical_analysis", "get_realtime_quote", "get_indicator_snapshot",
                     "get_volume_analysis", "analyze_trend", "get_capital_summary", "get_fund_flow",
                     "get_stock_info", "search_stock_intel", "web_search"],
    }
    expected = depth_tools.get(depth, depth_tools["standard"])
    warnings = []
    for tool_name in expected:
        result = tool_results.get(tool_name)
        if result is None:
            warnings.append(f"{tool_name} 未返回数据")
            continue
        if isinstance(result, dict):
            if "error" in result:
                warnings.append(f"{tool_name}: {result['error']}")
            elif not result:
                warnings.append(f"{tool_name} 返回空数据")
    return "\n".join(warnings)


def _check_cross_verification(tool_results: Dict[str, Dict[str, Any]]) -> List[str]:
    """交叉验证：两个不同来源验证同一结论时加星。"""
    verifications = []

    tech = tool_results.get("technical_analysis", {})
    fund = tool_results.get("get_fund_flow", {})
    capital = tool_results.get("get_capital_summary", {})

    tech_score = tech.get("score", 0)

    # 从资金面提取信号
    fund_signal = ""
    if isinstance(fund, dict) and "error" not in fund:
        fund_data = fund.get("data", {})
        for code, flow in fund_data.items():
            if isinstance(flow, dict):
                fund_signal = flow.get("signal", "")
                break

    # 从资本结构提取信号
    capital_signal = ""
    if isinstance(capital, dict) and "error" not in capital:
        capital_signal = capital.get("summary", {}).get("overall_signal", "")

    # 技术面+资金面双重验证
    if tech_score > 60 and ("流入" in fund_signal or "看多" in capital_signal):
        verifications.append("⭐ 技术面+资金面双重看多")
    elif tech_score < 40 and ("流出" in fund_signal or "看空" in capital_signal):
        verifications.append("⭐ 技术面+资金面双重看空")

    # 趋势+指标双重验证
    trend = tool_results.get("analyze_trend", {})
    indicator = tool_results.get("get_indicator_snapshot", {})
    if isinstance(trend, dict) and isinstance(indicator, dict):
        trend_dir = trend.get("trend", "")
        macd_signals = indicator.get("macd", {}).get("signals", [])
        if "上升" in trend_dir and any("金叉" in s for s in macd_signals):
            verifications.append("⭐ 趋势+指标双重确认")
        elif "下降" in trend_dir and any("死叉" in s for s in macd_signals):
            verifications.append("⭐ 趋势+指标双重确认看空")

    return verifications


def evaluate_stock(
    codes: str,
    depth: str = "standard",
    period: str = "T+3",
    realtime: bool = False,
) -> Dict[str, Any]:
    """个股综合评估：执行代码生成标准输出。"""
    start_time = time.time()

    code = codes.split(",")[0].strip()
    if not code:
        return {"error": "股票代码不能为空"}

    # 获取股票名称
    stock_name = code
    try:
        info = _get_stock_info(code, detail=False)
        if isinstance(info, dict) and "error" not in info:
            stock_name = info.get("name", code)
    except:
        pass

    # 一趟水获取数据
    logger.info("[StockEval] %s(%s) 深度=%s 周期=%s", stock_name, code, depth, period)
    tool_results = _fetch_data_single(code, depth, stock_name)

    # 生成 stock_report（参数对齐 stock_report.py 签名）
    quote = tool_results.get("get_realtime_quote", {})
    technical = tool_results.get("technical_analysis", {})
    capital = tool_results.get("get_capital_summary", {})
    fund_flow = tool_results.get("get_fund_flow", {})

    report_result = _stock_report(info={}, technical=technical, capital=capital, quote=quote, fund_flow=fund_flow)

    report = report_result.get("report", "")
    summary = report_result.get("summary", {})

    # 数据完整性检查：所有工具结果中 error 或空数据的都要提示
    warnings = _check_data_warnings(tool_results, depth)
    if warnings:
        report = f"{report}\n**注意**:\n{warnings}"

    # 交叉验证
    verified = _check_cross_verification(tool_results)

    # 准备 LLM 数据
    llm_data = {
        "technical_factors": tool_results.get("technical_analysis", {}).get("factors", []),
        "technical_signals": tool_results.get("technical_analysis", {}).get("signal", ""),
        "fund_flow": tool_results.get("get_fund_flow", {}),
        "capital": tool_results.get("get_capital_summary", {}),
        "indicator_details": tool_results.get("get_indicator_snapshot", {}),
        "trend_details": tool_results.get("analyze_trend", {}),
        "period": period,
        "weights": PERIOD_WEIGHTS.get(period, DEFAULT_WEIGHTS),
    }

    elapsed = round(time.time() - start_time, 2)
    logger.info("[StockEval] 完成 %s: %.0f分 %.1fs", stock_name, summary.get("score", 0), elapsed)

    return {
        "code": code,
        "name": stock_name,
        "score": summary.get("score", 0),
        "direction": summary.get("direction", "中性"),
        "action": summary.get("action", "跳过"),
        "report": report,
        "summary": summary,
        "tool_results": tool_results,
        "verified": verified,
        "llm_data": llm_data,
        "period": period,
        "depth": depth,
        "elapsed": elapsed,
    }


def evaluate_stocks(
    codes: str,
    depth: str = "standard",
    period: str = "T+3",
    sort_by: str = "score",
) -> Dict[str, Any]:
    """多股综合评估 + 横向对比。"""
    start_time = time.time()

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return {"error": "股票代码不能为空"}

    # 并行评估多股
    stocks = []

    def _eval_one(code):
        return evaluate_stock(code, depth=depth, period=period)

    with ThreadPoolExecutor(max_workers=min(len(code_list), 5)) as executor:
        futures = [executor.submit(_eval_one, c) for c in code_list]
        for future in futures:
            try:
                result = future.result(timeout=60)
                if "error" not in result:
                    stocks.append(result)
            except Exception as e:
                logger.error("[StockEval] 多股评估异常: %s", e)

    # 排序（优秀在前）
    stocks.sort(key=lambda x: x.get("score", 0), reverse=True)

    # 生成对比报告
    comparison = _generate_comparison(stocks, period)

    elapsed = round(time.time() - start_time, 2)
    return {
        "count": len(stocks),
        "period": period,
        "stocks": stocks,
        "comparison": comparison,
        "elapsed": elapsed,
    }


def _generate_comparison(stocks: List[Dict], period: str) -> str:
    """生成多股对比报告（标准化格式）。"""
    if not stocks:
        return "无有效股票数据"

    lines = [f"**多股对比** (周期: {period})\n"]

    # 每只股票的标准化报告
    for i, s in enumerate(stocks):
        report = s.get("report", "")
        if report:
            lines.append(report)
            # 追加注意栏（如果有）
            warnings = _check_data_warnings(s.get("tool_results", {}), s.get("depth", "standard"))
            if warnings:
                lines.append(f"**注意**:\n{warnings}")
            lines.append("")  # 空行分隔

    # 对比排名表
    lines.append(f"**对比排名**:")
    lines.append(f"{'排名':<4} {'股票':<14} {'评分':<6} {'方向':<6} {'操作':<6} {'信号':<20}")
    lines.append("-" * 60)
    for i, s in enumerate(stocks):
        name_code = f"{s['name']}({s['code']})"
        score = s.get('score', 0)
        direction = s.get('direction', '中性')
        action = s.get('action', '跳过')
        signal = s.get('summary', {}).get('signal_short', '')[:18]
        lines.append(f"{i+1:<4} {name_code:<14} {score:<6} {direction:<6} {action:<6} {signal:<20}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python run.py <股票代码> [深度] [周期]")
        sys.exit(1)
    result = evaluate_stock(sys.argv[1],
                           depth=sys.argv[2] if len(sys.argv) > 2 else "standard",
                           period=sys.argv[3] if len(sys.argv) > 3 else "T+3")
    print(result.get("report", result.get("error")))
