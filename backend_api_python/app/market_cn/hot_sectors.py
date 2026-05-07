#!/usr/bin/env python3
"""
热门板块 & 概念板块实时分析
数据源: 东方财富板块行情 API（直接 HTTP，不依赖 AKShare）
依赖: pip install requests pandas

功能:
  - 行业板块涨幅排名 + 量能分析
  - 概念板块涨幅排名 + 连续性分析
  - 板块内涨停/强势股明细
  - AI 可用的结构化数据输出
"""

import logging
import requests
import pandas as pd
from datetime import datetime
from functools import wraps
import time
import json

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

# ═══════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════

def _retry(max_retries=2, delay=1):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if i < max_retries:
                        time.sleep(delay)
            raise last_err
        return wrapper
    return decorator


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
#  东方财富板块行情 API
# ═══════════════════════════════════════════════════

# 板块类型映射
_BOARD_TYPES = {
    "industry": {"fs": "b:BK0475", "name": "行业板块"},
    "concept":  {"fs": "b:BK0815", "name": "概念板块"},
    "area":     {"fs": "b:BK0813", "name": "地域板块"},
}

# 行业板块字段
_INDUSTRY_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f22,f23,f24,f25,f26,f27,f28,f29,f30,f31,f32,f33,"
    "f34,f35,f38,f39,f40,f41,f42,f43,f44,f45,f46,f47,f48,f49,f50,"
    "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f100,f104,f105,f128,f136,f115"
)


@_retry(max_retries=2, delay=1)
def _fetch_board_list(board_type="industry", sort_by="f3", sort_dir="desc", limit=30):
    """获取板块行情排名

    board_type: industry | concept | area
    sort_by: f3=涨跌幅, f6=成交额, f8=换手率, f20=总市值
    """
    config = _BOARD_TYPES.get(board_type, _BOARD_TYPES["industry"])
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1,
        "pz": limit,
        "po": 1 if sort_dir == "desc" else 0,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": sort_by,
        "fs": config["fs"],
        "fields": _INDUSTRY_FIELDS,
    }

    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)

    # 检查 HTTP 状态码
    if resp.status_code != 200:
        raise ConnectionError(f"东方财富 API 返回 {resp.status_code}")

    data = resp.json()

    # 防御 data 或 diff 为 None 的情况
    data_node = data.get("data")
    if not data_node or not isinstance(data_node, dict):
        logger.warning(f"东方财富 API 返回无 data 节点: {str(data)[:200]}")
        return []

    items = data_node.get("diff")
    if not items:
        return []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        results.append({
            "name": _safe_str(item.get("f14")),
            "code": _safe_str(item.get("f12")),
            "change_pct": _safe_num(item.get("f3")),
            "price": _safe_num(item.get("f2")),
            "volume": _safe_num(item.get("f5")),
            "amount": _safe_num(item.get("f6")),
            "turnover": _safe_num(item.get("f8")),
            "up_count": int(_safe_num(item.get("f104"))),
            "down_count": int(_safe_num(item.get("f105"))),
            "lead_stock": _safe_str(item.get("f128")),
            "lead_stock_code": _safe_str(item.get("f136")),
            "lead_stock_pct": _safe_num(item.get("f115")),
            "limit_up_count": int(_safe_num(item.get("f100"))),
            "total_mv": _safe_num(item.get("f20")),
            "pe_ratio": _safe_num(item.get("f9")),
        })
    return results


@_retry(max_retries=2, delay=1)
def _fetch_sector_stocks(board_code, limit=10):
    """获取板块内个股行情（领涨股详情）"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1,
        "pz": limit,
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": f"b:{board_code}",
        "fields": "f2,f3,f4,f5,f6,f8,f12,f14,f15,f16,f17,f18,f62,f100,f115",
    }

    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        raise ConnectionError(f"东方财富板块个股 API 返回 {resp.status_code}")

    data = resp.json()
    data_node = data.get("data")
    if not data_node or not isinstance(data_node, dict):
        return []

    items = data_node.get("diff")
    if not items:
        return []

    return [{
        "code": _safe_str(i.get("f12")),
        "name": _safe_str(i.get("f14")),
        "price": _safe_num(i.get("f2")),
        "change_pct": _safe_num(i.get("f3")),
        "amount": _safe_num(i.get("f6")),
        "turnover": _safe_num(i.get("f8")),
        "is_limit_up": i.get("f100") == 1,
    } for i in items if isinstance(i, dict)]


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
    """获取热门行业板块（按涨幅排序）"""
    logger.info("获取热门行业板块 top %d", limit)
    boards = _fetch_board_list("industry", sort_by="f3", sort_dir="desc", limit=limit)
    boards = _analyze_continuity(boards)
    return boards


def get_hot_concept_boards(limit=20):
    """获取热门概念板块（按涨幅排序）"""
    logger.info("获取热门概念板块 top %d", limit)
    boards = _fetch_board_list("concept", sort_by="f3", sort_dir="desc", limit=limit)
    boards = _analyze_continuity(boards)
    return boards


def get_sector_detail(board_code, limit=10):
    """获取板块内强势个股"""
    return _fetch_sector_stocks(board_code, limit=limit)


def get_all_hot_sectors(industry_limit=15, concept_limit=15):
    """获取全部热门板块数据（供 API 使用）"""
    logger.info("热门板块分析: %d 行业 + %d 概念", industry_limit, concept_limit)

    industry = []
    concept = []

    # 并行采集行业和概念（独立异常处理）
    try:
        industry = get_hot_industry_boards(industry_limit)
    except Exception as e:
        logger.error("行业板块获取失败: %s", e)

    try:
        concept = get_hot_concept_boards(concept_limit)
    except Exception as e:
        logger.error("概念板块获取失败: %s", e)

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


# ═══════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = get_all_hot_sectors()
    print(json.dumps(result["analysis"], ensure_ascii=False, indent=2))
