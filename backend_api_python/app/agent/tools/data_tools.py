# -*- coding: utf-8 -*-
"""
Data tools — real-time quotes, K-lines, stock info.
Wraps DataSourceFactory into OpenAI-function-callable tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)


def _get_ds(market: str = "CNStock"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)


# ── Re-exported from shared utils (kept for backward compat) ──
from app.data_sources.market_detector import detect_market as _detect_market


# ── Tool functions ────────────────────────────────────────────

@tool(
    description="根据股票代码获取中文名称。如输入 600519 返回贵州茅台。",
    category="名称查询",
)
def resolve_stock_name(stock_code: str) -> Dict[str, Any]:
    """根据股票代码获取中文名称。

    Args:
        stock_code: 股票代码（如 600519、000001）或交易对（如 BTC/USDT）
    """
    market = _detect_market(stock_code) or "CNStock"
    try:
        from app.services.symbol_name import resolve_symbol_name
        name = resolve_symbol_name(market, stock_code)
        if name:
            return {"stock_code": stock_code, "name": name, "market": market}
        return {"stock_code": stock_code, "name": None, "market": market, "message": "未找到对应名称"}
    except Exception as e:
        logger.error("resolve_stock_name(%s) failed: %s", stock_code, e)
        return {"stock_code": stock_code, "error": str(e)}


@tool(
    description="根据中文名称或关键词搜索股票代码。支持模糊匹配，如输入茅台可找到贵州茅台(600519)。当用户提供中文股票名称但没有代码时，必须先用此工具查到代码再进行后续分析。",
    category="名称查询",
)
def search_stock_by_name(keyword: str, market: str = "CNStock", limit: int = 10) -> Dict[str, Any]:
    """根据中文名称或关键词搜索股票代码。

    Args:
        keyword: 搜索关键词（中文股票名称、代码片段等）
        market: 市场，默认 CNStock（可选：CNStock、HKStock、Crypto、USStock）
        limit: 返回数量上限，默认10
    """
    if not keyword or not keyword.strip():
        return {"error": "搜索关键词不能为空", "retriable": False}

    limit = min(max(limit, 1), 50)
    try:
        from app.data.market_symbols_seed import search_symbols
        results = search_symbols(market, keyword.strip(), limit)
        if results:
            return {
                "keyword": keyword,
                "market": market,
                "results": [{"code": r["symbol"], "name": r.get("name", ""), "market": r.get("market", market)} for r in results],
                "count": len(results),
            }

        # Fallback: 从 basicinfo_db 查（A股）
        if market == "CNStock":
            try:
                from app.utils.basicinfo_db import get_stock_basic_db
                db = get_stock_basic_db()
                # 尝试按名称搜
                stocks = db.search_stocks(keyword.strip(), limit=limit)
                if stocks:
                    return {
                        "keyword": keyword,
                        "market": market,
                        "results": [{"code": s.get("symbol", ""), "name": s.get("name", ""), "market": "CNStock"} for s in stocks],
                        "count": len(stocks),
                    }
            except Exception:
                pass

        return {"keyword": keyword, "market": market, "results": [], "count": 0, "message": "未找到匹配的股票"}
    except Exception as e:
        logger.error("search_stock_by_name(%s) failed: %s", keyword, e)
        return {"keyword": keyword, "results": [], "count": 0, "error": str(e)}


@tool(
    description="获取股票或交易对的实时行情（最新价、涨跌幅、成交量、换手率、量比、PE/PB等）。",
    category="行情数据",
)
def get_realtime_quote(stock_code: str) -> Dict[str, Any]:
    """获取股票/交易对的实时行情数据，包括最新价、涨跌幅、成交量、换手率等。"""
    market = _detect_market(stock_code) or "CNStock"
    ds = _get_ds(market)
    try:
        result = ds.get_ticker(stock_code)
        if isinstance(result, dict) and "error" not in result:
            return {"stock_code": stock_code, "market": market, **result}
        return result if isinstance(result, dict) else {"error": "Unexpected result type"}
    except NotImplementedError:
        return {"error": f"数据源 {market} 不支持 get_ticker", "retriable": False}
    except Exception as e:
        logger.error("get_realtime_quote(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


@tool(
    description="获取股票/交易对的K线数据（OHLCV：开盘价/最高价/最低价/收盘价/成交量）。支持多周期：1m/5m/15m/30m/1H/4H/1D/1W。这是获取原始K线数据的核心工具，用于趋势分析和技术指标计算。当用户要求查看K线、行情数据、历史价格时必须使用此工具。",
    category="行情数据",
)
def agent_get_kline(stock_code: str, timeframe: str = "1D", days: int = 60, market: str = "") -> List[Dict[str, Any]]:
    """获取股票/交易对的K线数据（OHLCV）。

    Args:
        stock_code: 股票代码（如 000001, 600519）或交易对（如 BTC/USDT）
        timeframe: K线周期，可选值: 1m, 5m, 15m, 30m, 1H, 4H, 1D, 1W。默认 1D（日线）
        days: 获取天数，默认60天，最大250天（仅对日线及以上周期有意义）
        market: 市场类型，可选值: CNStock, HKStock, Crypto, Forex, USStock, Futures, MOEX。
                留空则自动推断（A股6位数字→CNStock, HK前缀→HKStock, USDT结尾→Crypto 等）。
                当自动推断不准时（如美股代码、期货合约）需手动指定。
    """
    valid_timeframes = {"1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"}
    if timeframe not in valid_timeframes:
        return []
    days = min(max(days, 1), 250)
    if market:
        from app.data_sources.factory import DataSourceFactory
        market = DataSourceFactory.normalize_market(market)
    else:
        market = _detect_market(stock_code) or "CNStock"
    ds = _get_ds(market)
    try:
        klines = ds.get_kline(stock_code, timeframe, days) or []
        # 精简返回：缩短字段名、四舍五入价格，大幅减少 token 消耗
        compact = []
        for k in klines:
            compact.append({
                "t": k.get("date", k.get("timestamp", "")),
                "o": round(k.get("open", 0), 2),
                "h": round(k.get("high", 0), 2),
                "l": round(k.get("low", 0), 2),
                "c": round(k.get("close", 0), 2),
                "v": k.get("volume", 0),
            })
        return compact
    except Exception as e:
        logger.error("get_kline(%s, %s, %d) failed: %s", stock_code, timeframe, days, e)
        return []


@tool(
    description="生成K线图（HTML交互式图表），返回文件路径。用浏览器打开可查看专业级蜡烛图+成交量+均线。当用户要求看K线、显示图表、K线图时，必须先调用 agent_get_kline 获取数据，再调用此工具生成图表。分析类请求不需要此工具。",
    category="行情数据",
)
def generate_kline_chart(
    stock_code: str,
    timeframe: str = "1D",
    days: int = 60,
    stock_name: str = "",
    indicators: str = "",
) -> Dict[str, Any]:
    """生成K线图（HTML 交互式图表），返回文件路径。

    用 ECharts 渲染专业级蜡烛图 + 成交量柱状图，支持 MA 均线叠加。
    生成的 HTML 文件用浏览器打开即可交互（缩放、拖动、悬停看详情）。

    Args:
        stock_code: 股票代码（如 000001、600519）或交易对（如 BTC/USDT）
        timeframe: K线周期，可选: 1m/5m/15m/30m/1H/4H/1D/1W。默认 1D
        days: 获取天数，默认 60，最大 250
        stock_name: 股票名称（可选，显示在标题上）
        indicators: 叠加指标，逗号分隔。可选: MA5,MA10,MA20,MA60。默认 MA5+MA10+MA20
    """
    import os
    import pathlib

    # 1) 拉取 K 线数据
    klines = agent_get_kline(stock_code, timeframe, days)
    if not klines:
        return {"error": f"无法获取 {stock_code} 的K线数据", "retriable": False}

    # 2) 解析指标参数
    ma_list = []
    if indicators:
        for item in indicators.split(","):
            item = item.strip().upper()
            if item.startswith("MA") and item[2:].isdigit():
                ma_list.append(int(item[2:]))
    if not ma_list:
        ma_list = [5, 10, 20]

    # 3) 计算均线数据
    closes = [k["c"] for k in klines]
    ma_data = {}
    for period in ma_list:
        ma_vals = []
        for i in range(len(closes)):
            if i < period - 1:
                ma_vals.append(None)
            else:
                ma_vals.append(round(sum(closes[i - period + 1 : i + 1]) / period, 2))
        ma_data[f"MA{period}"] = ma_vals

    # 4) 构建 ECharts 数据
    dates = [k["t"] for k in klines]
    ohlc = [[k["o"], k["c"], k["l"], k["h"]] for k in klines]
    volumes = [k["v"] for k in klines]
    vol_colors = []
    for k in klines:
        vol_colors.append("#ef5350" if k["c"] < k["o"] else "#26a69a")

    title = f"{stock_name or stock_code} {timeframe} K线"
    ma_colors = ["#ff9800", "#2196f3", "#e91e63", "#4caf50", "#9c27b0"]

    # 5) 生成 HTML
    ma_series_js = ""
    for idx, (name, vals) in enumerate(ma_data.items()):
        color = ma_colors[idx % len(ma_colors)]
        ma_series_js += f""",
        {{
            name: '{name}',
            type: 'line',
            data: {json.dumps(vals)},
            smooth: true,
            lineStyle: {{ width: 1 }},
            symbol: 'none',
            itemStyle: {{ color: '{color}' }}
        }}"""

    chart_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #1a1a2e; color: #e0e0e0; font-family: -apple-system, sans-serif; }}
  #chart {{ width: 100vw; height: 100vh; }}
</style>
</head>
<body>
<div id="chart"></div>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script>
var chart = echarts.init(document.getElementById('chart'), 'dark');
var option = {{
    backgroundColor: '#1a1a2e',
    title: {{ text: '{title}', left: 'center', top: 10, textStyle: {{ color: '#eee', fontSize: 16 }} }},
    tooltip: {{
        trigger: 'axis',
        axisPointer: {{ type: 'cross' }},
        backgroundColor: 'rgba(20,20,40,0.9)',
        borderColor: '#555',
        textStyle: {{ color: '#eee' }}
    }},
    legend: {{
        data: ['K线'{', '.join(repr(f"MA{p}") for p in ma_list)}],
        top: 40, textStyle: {{ color: '#aaa' }}
    }},
    grid: [
        {{ left: '8%', right: '3%', top: 80, height: '55%' }},
        {{ left: '8%', right: '3%', top: '78%', height: '15%' }}
    ],
    xAxis: [
        {{ type: 'category', data: {json.dumps(dates)}, gridIndex: 0, axisLabel: {{ color: '#888' }}, axisLine: {{ lineStyle: {{ color: '#444' }} }} }},
        {{ type: 'category', data: {json.dumps(dates)}, gridIndex: 1, axisLabel: {{ show: false }}, axisLine: {{ lineStyle: {{ color: '#444' }} }} }}
    ],
    yAxis: [
        {{ scale: true, gridIndex: 0, splitLine: {{ lineStyle: {{ color: '#333' }} }}, axisLabel: {{ color: '#888' }} }},
        {{ scale: true, gridIndex: 1, splitLine: {{ show: false }}, axisLabel: {{ show: false }} }}
    ],
    dataZoom: [
        {{ type: 'inside', xAxisIndex: [0, 1], start: 30, end: 100 }},
        {{ type: 'slider', xAxisIndex: [0, 1], start: 30, end: 100, bottom: 10,
           borderColor: '#555', textStyle: {{ color: '#aaa' }},
           fillerColor: 'rgba(60,60,120,0.3)' }}
    ],
    series: [
        {{
            name: 'K线',
            type: 'candlestick',
            data: {json.dumps(ohlc)},
            xAxisIndex: 0, yAxisIndex: 0,
            itemStyle: {{
                color: '#ef5350',
                color0: '#26a69a',
                borderColor: '#ef5350',
                borderColor0: '#26a69a'
            }}
        }}{ma_series_js},
        {{
            name: '成交量',
            type: 'bar',
            data: {json.dumps(volumes)},
            xAxisIndex: 1, yAxisIndex: 1,
            itemStyle: {{
                color: function(p) {{ return {json.dumps(vol_colors)}[p.dataIndex]; }}
            }}
        }}
    ]
}};
chart.setOption(option);
window.addEventListener('resize', function() {{ chart.resize(); }});
</script>
</body>
</html>"""

    # 6) 写文件
    out_dir = pathlib.Path(os.getenv("WORKSPACE_DIR", ".")).resolve() / "chart_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{stock_code.replace('/', '_')}_{timeframe}_{days}d.html"
    out_path = out_dir / filename
    out_path.write_text(chart_html, encoding="utf-8")

    return {
        "file_path": str(out_path),
        "stock_code": stock_code,
        "stock_name": stock_name,
        "timeframe": timeframe,
        "days": days,
        "kline_count": len(klines),
        "message": f"K线图已生成: {out_path}",
    }


@tool(
    description="获取股票基本面信息（公司简介、行业分类、市值、PE、PB、ROE等）。",
    category="行情数据",
)
def get_stock_info(stock_code: str) -> Dict[str, Any]:
    """获取股票基本面信息（公司简介、行业、市值、PE、PB 等）。"""
    market = _detect_market(stock_code) or "CNStock"
    ds = _get_ds(market)
    try:
        if hasattr(ds, "get_stock_info"):
            result = ds.get_stock_info(stock_code)
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                # 某些数据源返回字符串，尝试 JSON 解析
                try:
                    import json as _json
                    parsed = _json.loads(result)
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, TypeError):
                    pass
                # 解析失败，包装为 dict
                return {"stock_code": stock_code, "info_text": result}
            return {"stock_code": stock_code, "raw_result": str(result)[:2000] if result else None}
        return {"error": f"数据源 {market} 不支持 get_stock_info", "retriable": False}
    except NotImplementedError:
        return {"error": f"数据源 {market} 不支持 get_stock_info", "retriable": False}
    except Exception as e:
        logger.error("get_stock_info(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


# Legacy list — kept for backward compat during migration; safe to remove later.
DATA_TOOLS = []
