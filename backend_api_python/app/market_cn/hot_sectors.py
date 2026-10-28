#!/usr/bin/env python3
"""
热门板块 & 概念板块实时分析

数据源: 一律经 `app/data_sources/coord_index`（远端实现在 provider_index/）：
  · sector_list   板块行情排名 —— sina(newSinaHy/newFLJK) 主源 → 东财 push2 备用
  · sector_stocks 板块成分股   —— 东财 push2（唯一源）
本文件不再直连远端、不再写降级链与 push2 熔断：选源/排序/降级全在 data_sources 层。

功能:
  - 行业板块涨幅排名 + 量能分析
  - 概念板块涨幅排名 + 连续性分析
  - 板块内涨停/强势股明细
  - AI 可用的结构化数据输出
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════

def _safe_num(val, default=0):
    """安全转数值：处理 '-', None, '' 等东方财富特殊值"""
    if val is None or val == "" or val == "-":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_str(val, default=""):
    """安全取字符串"""
    if val is None or val == "-":
        return default
    return str(val)


# ═══════════════════════════════════════════════════
#  取数（全部经 data_sources 层）
# ═══════════════════════════════════════════════════

def _fetch_board_list(board_type="industry", sort_by="f3", sort_dir="desc", limit=30):
    """板块行情排名（一次 coordinate；排序键沿用东财 fid，跨源同参）。

    board_type: industry | concept | area
    sort_by: f3=涨跌幅, f6=成交额, f8=换手率, f20=总市值
    失败返回 []（空列表好过异常，由调用方降级展示）。
    """
    from app.data_sources.coord_index import coordinate

    out = coordinate("sector_list", board_type=board_type, limit=limit,
                     sort_by=sort_by, sort_dir=sort_dir)
    rows = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]
    if not out.get("ok") or not rows:
        logger.warning("[hot_sectors] 板块行情不可用(%s): %s", board_type, out.get("error"))
        return []
    return rows


def _fetch_sector_stocks(board_code, limit=10):
    """板块内个股行情（按涨跌幅降序）。

    返回 [{code, name, price, change_pct, amount, turnover, is_limit_up}]；失败/无数据 []。
    """
    from app.data_sources.coord_index import coordinate

    if not board_code:
        return []
    out = coordinate("sector_stocks", board_code=board_code, limit=limit)
    rows = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]
    if not out.get("ok") or not rows:
        logger.warning("[hot_sectors] 板块成分股不可用(%s): %s", board_code, out.get("error"))
        return []
    return rows


# ═══════════════════════════════════════════════════
#  连续性分析（多日板块动量）
# ═══════════════════════════════════════════════════

def _analyze_continuity(boards):
    """标记连续上涨/持续强势板块（就地修改并返回）"""
    if not boards:
        return boards

    for b in boards:
        pct = _safe_num(b.get("change_pct"))
        up = int(_safe_num(b.get("up_count")))
        down = int(_safe_num(b.get("down_count")))

        # 强势标签
        if pct >= 3 and up > down * 2:
            b["strength"] = "强势领涨"
        elif pct >= 1.5 and up > down:
            b["strength"] = "稳步上行"
        elif pct >= 0:
            b["strength"] = "小幅上涨"
        elif pct >= -1.5:
            b["strength"] = "小幅回调"
        else:
            b["strength"] = "弱势调整"

        # 板块内涨停标记
        b["has_limit_up"] = int(_safe_num(b.get("limit_up_count"))) > 0

        # 成交额（亿元）
        amount = _safe_num(b.get("amount"))
        b["amount_yi"] = round(amount / 1e8, 2) if amount > 0 else 0

    return boards


# ═══════════════════════════════════════════════════
#  对外接口
# ═══════════════════════════════════════════════════

def get_hot_industry_boards(limit=20):
    """获取热门行业板块（按涨幅排序）。

    Returns:
        list[dict]: 板块行 [{name, code, change_pct, amount, up_count, down_count,
        lead_stock, limit_up_count, strength, amount_yi}]。
        注：sina 主源无 涨停家数/涨跌家数 ⇒ limit_up_count/up_count/down_count 为 0。
    """
    logger.info("获取热门行业板块 top %d", limit)
    boards = _fetch_board_list("industry", sort_by="f3", sort_dir="desc", limit=limit)
    boards = _analyze_continuity(boards)
    return boards


def get_hot_concept_boards(limit=20):
    """获取热门概念板块（按涨幅排序）。

    Returns:
        list[dict]: 板块行，键同 get_hot_industry_boards（sina 主源同样无涨跌家数）。
    """
    logger.info("获取热门概念板块 top %d", limit)
    boards = _fetch_board_list("concept", sort_by="f3", sort_dir="desc", limit=limit)
    boards = _analyze_continuity(boards)
    return boards


def get_sector_detail(board_code, limit=10):
    """获取板块内强势个股

    Returns:
        list[dict]: [{code, name, price, change_pct, amount, turnover, is_limit_up}]；失败 []。
    """
    return _fetch_sector_stocks(board_code, limit=limit)


def get_all_hot_sectors(industry_limit=15, concept_limit=15):
    """获取全部热门板块数据（供 API 使用）

    Returns:
        dict: {timestamp, industry: [...], concept: [...], analysis: {...}}；
              板块行键同 get_hot_industry_boards。
    """
    logger.info("热门板块分析: %d 行业 + %d 概念", industry_limit, concept_limit)

    industry = get_hot_industry_boards(industry_limit)
    concept = get_hot_concept_boards(concept_limit)

    # 综合分析
    analysis = _build_sector_analysis(industry, concept)

    return {
        "timestamp": datetime.now().isoformat(),
        "industry": industry[:industry_limit],
        "concept": concept[:concept_limit],
        "analysis": analysis,
    }


def _build_sector_analysis(industry, concept):
    """综合板块分析，生成可直接给 AI 前端用的数据"""
    if not industry and not concept:
        return {"summary": "数据获取失败", "sentiment": "未知", "up_ratio": 50,
                "main_lines": [], "top_industry": [], "top_concept": [],
                "hot_money_sectors": [], "zt_concentrated": []}

    top5_industry = industry[:5] if industry else []
    top5_concept = concept[:5] if concept else []

    # 主线判断
    main_lines = []
    for b in top5_industry:
        pct = _safe_num(b.get("change_pct"))
        if pct >= 1.5:
            main_lines.append(_safe_str(b.get("name", "未知")))

    # 资金流向（成交额最大的板块）
    hot_money = sorted(industry, key=lambda x: _safe_num(x.get("amount")), reverse=True)[:5]

    # 涨停集中板块
    zt_sectors = [b for b in industry if int(_safe_num(b.get("limit_up_count"))) > 0]
    zt_sectors.sort(key=lambda x: int(_safe_num(x.get("limit_up_count"))), reverse=True)

    # 情绪判断
    up_total = sum(int(_safe_num(b.get("up_count"))) for b in industry)
    down_total = sum(int(_safe_num(b.get("down_count"))) for b in industry)
    total = up_total + down_total
    up_ratio = up_total / total if total > 0 else 0.5

    if up_ratio > 0.7:
        sentiment = "全面做多"
    elif up_ratio > 0.55:
        sentiment = "偏多震荡"
    elif up_ratio > 0.45:
        sentiment = "多空拉锯"
    elif up_ratio > 0.3:
        sentiment = "偏空调整"
    else:
        sentiment = "全面下跌"

    return {
        "sentiment": sentiment,
        "up_ratio": round(up_ratio * 100, 1),
        "main_lines": main_lines,
        "top_industry": [{"name": _safe_str(b.get("name")), "change_pct": _safe_num(b.get("change_pct")), "amount_yi": _safe_num(b.get("amount_yi"))} for b in top5_industry],
        "top_concept": [{"name": _safe_str(b.get("name")), "change_pct": _safe_num(b.get("change_pct"))} for b in top5_concept],
        "hot_money_sectors": [{"name": _safe_str(b.get("name")), "amount_yi": _safe_num(b.get("amount_yi"))} for b in hot_money],
        "zt_concentrated": [{"name": _safe_str(b.get("name")), "zt_count": int(_safe_num(b.get("limit_up_count")))} for b in zt_sectors[:5]],
        "summary": _gen_summary(sentiment, main_lines, top5_concept, zt_sectors),
    }


def _gen_summary(sentiment, main_lines, top_concept, zt_sectors):
    """生成文字摘要"""
    parts = [f"市场整体{sentiment}"]

    if main_lines:
        parts.append(f"行业主线: {', '.join(main_lines[:3])}")

    if top_concept:
        cnames = [_safe_str(b.get("name")) for b in top_concept[:3] if b.get("name")]
        if cnames:
            parts.append(f"活跃概念: {', '.join(cnames)}")

    if zt_sectors:
        zt_names = [f"{_safe_str(b.get('name',''))}({int(_safe_num(b.get('limit_up_count')))}家涨停)" for b in zt_sectors[:3]]
        parts.append(f"涨停集中: {', '.join(zt_names)}")

    return "；".join(parts)
