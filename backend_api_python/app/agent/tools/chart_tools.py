# -*- coding: utf-8 -*-
"""
Chart tools — 蜡烛图展示工具。

生成 SVG 蜡烛图，可直接嵌入对话展示。
零外部依赖，纯 Python 生成 SVG XML。
"""
from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Dict, List
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ── SVG 渲染器 ───────────────────────────────────────────────

def _compute_ma(closes: List[float], period: int) -> list:
    result = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(round(sum(closes[i - period + 1 : i + 1]) / period, 2))
    return result

def _render_svg(
    klines: list,
    stock_name: str = "",
    timeframe: str = "1D",
    ma_periods: List[int] = None,
    show_volume: bool = True,
    width: int = 900,
    height: int = 520,
) -> str:
    """纯 Python 生成蜡烛图 SVG，零依赖。"""
    if ma_periods is None:
        ma_periods = [5, 10, 20]

    n = len(klines)
    if n == 0:
        return "<svg><text>无数据</text></svg>"

    # ── 布局参数 ──
    pad_left, pad_right, pad_top, pad_bottom = 65, 20, 50, 30
    candle_area_h = height * (0.65 if show_volume else 0.85)
    vol_area_h = height * 0.18 if show_volume else 0
    gap = 10

    chart_top = pad_top
    chart_bottom = pad_top + candle_area_h
    vol_top = chart_bottom + gap
    vol_bottom = vol_top + vol_area_h

    chart_w = width - pad_left - pad_right
    candle_w = max(1, chart_w / n)
    body_w = max(1, candle_w * 0.7)

    # ── 价格范围 ──
    highs = [k["h"] for k in klines]
    lows = [k["l"] for k in klines]
    p_max = max(highs)
    p_min = min(lows)
    p_range = p_max - p_min or 1
    p_max += p_range * 0.05
    p_min -= p_range * 0.05
    p_range = p_max - p_min

    # ── 成交量范围 ──
    vols = [k["v"] for k in klines]
    v_max = max(vols) if vols else 1

    # ── 坐标转换 ──
    def x_of(i):
        return pad_left + i * candle_w + candle_w / 2

    def y_of(price):
        return chart_top + (p_max - price) / p_range * candle_area_h

    def vol_y(v):
        return vol_bottom - (v / v_max) * vol_area_h

    # ── 构建 SVG ──
    parts = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                 f'style="background:#1a1a2e;font-family:-apple-system,Microsoft YaHei,sans-serif">')

    # 标题
    title = f"{stock_name or ''} {timeframe}"
    dates_range = f"{klines[0]['t']} ~ {klines[-1]['t']}"
    parts.append(f'<text x="{width/2}" y="28" text-anchor="middle" fill="#eee" font-size="16" font-weight="bold">{title}</text>')
    parts.append(f'<text x="{width/2}" y="44" text-anchor="middle" fill="#888" font-size="11">{dates_range}</text>')

    # 网格线
    grid_color = "#2a2a3e"
    for i in range(6):
        yy = chart_top + i * candle_area_h / 5
        price = p_max - i * p_range / 5
        parts.append(f'<line x1="{pad_left}" y1="{yy}" x2="{width-pad_right}" y2="{yy}" stroke="{grid_color}" stroke-dasharray="3,3"/>')
        parts.append(f'<text x="{pad_left-5}" y="{yy+4}" text-anchor="end" fill="#888" font-size="10">{price:.2f}</text>')

    # 蜡烛
    ma_colors = ["#ff9800", "#2196f3", "#e91e63", "#4caf50", "#9c27b0", "#00bcd4"]
    ma_data = {}
    closes = [k["c"] for k in klines]
    for p in ma_periods:
        ma_data[p] = _compute_ma(closes, p)

    for i, k in enumerate(klines):
        cx = x_of(i)
        o, h, l, c = k["o"], k["h"], k["l"], k["c"]
        is_up = c >= o
        color = "#ef5350" if is_up else "#26a69a"

        # 影线
        parts.append(f'<line x1="{cx}" y1="{y_of(h)}" x2="{cx}" y2="{y_of(l)}" stroke="{color}" stroke-width="1"/>')
        # 实体（阳线空心、阴线实心）
        y_top = y_of(max(o, c))
        y_bot = y_of(min(o, c))
        body_h = max(1, y_bot - y_top)
        fill = "none" if is_up else color
        parts.append(f'<rect x="{cx - body_w/2}" y="{y_top}" width="{body_w}" height="{body_h}" '
                     f'fill="{fill}" stroke="{color}" rx="0.5"/>')

        # 成交量
        if show_volume:
            vy = vol_y(k["v"])
            parts.append(f'<rect x="{cx - body_w/2}" y="{vy}" width="{body_w}" '
                         f'height="{vol_bottom - vy}" fill="{color}" opacity="0.6"/>')

    # 均线
    for idx, (period, ma_vals) in enumerate(ma_data.items()):
        color = ma_colors[idx % len(ma_colors)]
        points = []
        for i, v in enumerate(ma_vals):
            if v is not None:
                points.append(f"{x_of(i)},{y_of(v)}")
        if len(points) > 1:
            parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="1.2"/>')

    # 图例
    legend_x = pad_left + 10
    legend_y = chart_top - 8
    parts.append(f'<text x="{legend_x}" y="{legend_y}" fill="#aaa" font-size="11">K线</text>')
    legend_x += 35
    for idx, period in enumerate(ma_periods):
        color = ma_colors[idx % len(ma_colors)]
        parts.append(f'<line x1="{legend_x}" y1="{legend_y-4}" x2="{legend_x+15}" y2="{legend_y-4}" stroke="{color}" stroke-width="2"/>')
        parts.append(f'<text x="{legend_x+18}" y="{legend_y}" fill="{color}" font-size="11">MA{period}</text>')
        legend_x += 65

    # X轴日期标签（每隔几个显示）
    step = max(1, n // 8)
    for i in range(0, n, step):
        parts.append(f'<text x="{x_of(i)}" y="{vol_bottom + 15 if show_volume else chart_bottom + 15}" '
                     f'text-anchor="middle" fill="#666" font-size="9">{klines[i]["t"]}</text>')

    # 成交量轴标签
    if show_volume:
        parts.append(f'<text x="{pad_left-5}" y="{vol_top+4}" text-anchor="end" fill="#666" font-size="9">{v_max/10000:.0f}万</text>')
        parts.append(f'<text x="{pad_left-5}" y="{vol_bottom+4}" text-anchor="end" fill="#666" font-size="9">0</text>')

    parts.append("</svg>")
    return "\n".join(parts)

@tool(
    description=(
        "渲染K线蜡烛图（SVG），返回可视化图表。支持均线叠加和成交量柱状图。"
        "⚠️ 只在用户明确要求看K线/图表/走势图时才调用，"
        "普通股票分析、选股筛选等任务不要主动调用此工具。"
    ),
    category="K线图表",
    layer="显示层",
    domain=["finance"],
)
def render_candlestick(
    stock_code: str,
    timeframe: str = "1D",
    days: int = 120,
    stock_name: str = "",
    ma_periods: str = "5,10,20",
    show_volume: bool = True,
    market: str = "",
) -> Dict[str, Any]:
    """生成蜡烛图 SVG，可直接嵌入对话展示。

    Args:
        stock_code: 股票代码（如 600519、000001）或交易对（如 BTC/USDT）
        timeframe: K线周期，可选: 1m/5m/15m/30m/1H/4H/1D/1W。默认 1D
        days: 获取天数，默认 120，最大 250
        stock_name: 股票中文名称，显示在标题上
        ma_periods: 均线周期，逗号分隔。如 "5,10,20,60"
        show_volume: 是否显示成交量，默认 True
        market: 市场类型，留空自动推断
    """
    from app.agent.tools.data_tools import agent_get_kline

    # 1) 拉取数据
    klines = agent_get_kline(stock_code, timeframe=timeframe, days=days, market=market)
    if not klines:
        return {"error": f"无法获取 {stock_code} 的K线数据", "retriable": False}

    # 2) 解析均线参数
    periods = []
    for p in ma_periods.split(","):
        p = p.strip()
        if p.isdigit():
            periods.append(int(p))
    if not periods:
        periods = [5, 10, 20]

    # 3) 生成 SVG
    svg = _render_svg(
        klines=klines,
        stock_name=stock_name,
        timeframe=timeframe,
        ma_periods=periods,
        show_volume=show_volume,
    )

    # 4) 保存文件（备用）
    out_dir = pathlib.Path(os.getenv("WORKSPACE_DIR", ".")).resolve() / "chart_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{stock_code.replace('/', '_')}_{timeframe}_{days}d.svg"
    out_path = out_dir / filename
    out_path.write_text(svg, encoding="utf-8")

    # 5) 将 SVG 包装为 HTML 并 base64 编码，复用 __CHART_B64__ 协议让前端 iframe 渲染
    import base64
    chart_html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<style>*{margin:0;padding:0}body{background:#1a1a2e;display:flex;justify-content:center;align-items:center;height:100vh}</style>'
        "</head><body>"
        f'{svg}'
        "</body></html>"
    )
    b64 = base64.b64encode(chart_html.encode("utf-8")).decode("ascii")
    chart_marker = f"__CHART_B64__{b64}__END_CHART__"

    return {
        "file_path": str(out_path),
        "stock_code": stock_code,
        "stock_name": stock_name,
        "timeframe": timeframe,
        "days": days,
        "kline_count": len(klines),
        "message": f"蜡烛图已生成，共 {len(klines)} 根K线。\n{chart_marker}",
    }

# ── OpenAI tool declarations ─────────────────────────────────

