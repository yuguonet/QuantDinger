"""
concept_heat — 股票概念热度 + 概念板块资金流向

原理：
    1. 从 basicinfo_db 获取当前股票所属概念列表
    2. 从东方财富获取当日热门概念板块排名 & 涨幅
    3. 从东方财富获取概念板块资金流向（主力净流入）
    4. 匹配股票概念与热门概念，计算热度得分 & 命中板块的资金流向

自动注入 df 的列：
    concept_heat        概念热度得分 (0~100)
    hot_concepts        命中的热门概念名称 (逗号分隔)
    hot_concept_count   命中的热门概念数量
    concept_main_inflow 命中概念的主力净流入合计 (元)
    concept_inflow_str  命中概念资金流向摘要

脚本可用变量：
    stock_concept_heat        概念热度得分
    stock_hot_concepts        命中的热门概念详情 [{name, rank, change_pct, main_net_inflow}]
    stock_concept_inflow      命中概念主力净流入合计 (元)
"""
import logging
import requests

from . import provider
from app.data_sources.normalizer import safe_float as _safe_float

logger = logging.getLogger(__name__)

# 单次回测内缓存
_hot_concepts_cache = None
_concept_flow_cache = None
_stock_concepts_cache = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}




def _get_hot_concepts(limit=50):
    """从东方财富获取当日热门概念板块排名（带缓存）。"""
    global _hot_concepts_cache
    if _hot_concepts_cache is not None:
        return _hot_concepts_cache

    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": limit,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "b:BK0815",
            "fields": "f2,f3,f6,f8,f12,f14,f104,f105",
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        data = resp.json()
        items = (data.get("data") or {}).get("diff") or []

        results = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            name = str(item.get("f14") or "").strip()
            if not name:
                continue
            results.append({
                "rank": i + 1,
                "name": name,
                "code": str(item.get("f12") or ""),
                "change_pct": _safe_float(item.get("f3")),
                "amount": _safe_float(item.get("f6")),
                "turnover": _safe_float(item.get("f8")),
                "up_count": int(_safe_float(item.get("f104"))),
                "down_count": int(_safe_float(item.get("f105"))),
            })

        _hot_concepts_cache = results
        logger.info("concept_heat: 获取 %d 个热门概念", len(results))
        return results

    except Exception as e:
        logger.warning("concept_heat: 获取热门概念失败: %s", e)
        _hot_concepts_cache = []
        return []


def _get_concept_fund_flow(limit=50):
    """获取概念板块资金流向（通过 index.py 多源接口）。"""
    global _concept_flow_cache
    if _concept_flow_cache is not None:
        return _concept_flow_cache

    try:
        from app.market_cn.index import get_sector_fund_flow
        raw = get_sector_fund_flow("今日")

        results = {}
        for item in raw[:limit]:
            code = item.get("code", "")
            name = item.get("name", "")
            if not code or not name:
                continue
            results[code] = {
                "name": name,
                "code": code,
                "main_net_inflow": item.get("main_net", 0),
                "main_net_pct": item.get("main_pct", 0),
                "super_net_inflow": item.get("super_net", 0),
                "big_net_inflow": item.get("large_net", 0),
                "mid_net_inflow": item.get("mid_net", 0),
                "small_net_inflow": item.get("small_net", 0),
            }

        _concept_flow_cache = results
        logger.info("concept_heat: 获取 %d 个概念板块资金流向", len(results))
        return results

    except Exception as e:
        logger.warning("concept_heat: 获取概念资金流向失败: %s", e)
        _concept_flow_cache = {}
        return {}


def _get_stock_concepts(symbol: str) -> list:
    """从 basicinfo_db 获取股票所属概念列表（带缓存）。"""
    if not symbol:
        return []
    if symbol in _stock_concepts_cache:
        return _stock_concepts_cache[symbol]
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        info = get_stock_basic_db().get_stock(symbol) or {}
        concepts_raw = str(info.get('concepts', '') or '').strip()
        concept_list = [c.strip() for c in concepts_raw.split(',') if c.strip()]
    except Exception as e:
        logger.debug("concept_heat: 查询 %s 概念失败: %s", symbol, e)
        concept_list = []
    _stock_concepts_cache[symbol] = concept_list
    return concept_list


def _match_concepts(stock_concepts: list, hot_concepts: list) -> list:
    """模糊匹配股票概念与热门概念。"""
    if not stock_concepts or not hot_concepts:
        return []

    matched = []
    sc_set = set(stock_concepts)
    seen_names = set()

    for hc in hot_concepts:
        hc_name = hc["name"]
        for sc in sc_set:
            if hc_name in sc or sc in hc_name:
                if hc_name not in seen_names:
                    matched.append(hc)
                    seen_names.add(hc_name)
                break

    return matched


def _calc_heat_score(matched: list) -> float:
    """计算概念热度得分 (0~100)。"""
    if not matched:
        return 0.0

    score = 0.0
    for m in matched:
        rank_score = max(0, 50 - m["rank"])
        pct_bonus = max(0, m["change_pct"]) * 2
        score += rank_score + pct_bonus

    score += (len(matched) - 1) * 5
    return min(100.0, round(score, 2))


@provider
def register(ctx: dict) -> dict:
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')

    extras = {}

    if symbol and df is not None:
        stock_concepts = _get_stock_concepts(symbol)
        hot_concepts = _get_hot_concepts()
        concept_flow = _get_concept_fund_flow()

        # 匹配热门概念
        matched = _match_concepts(stock_concepts, hot_concepts)

        # 为每个命中的概念补充资金流向
        for m in matched:
            flow = concept_flow.get(m["code"], {})
            m["main_net_inflow"] = flow.get("main_net_inflow", 0)
            m["main_net_pct"] = flow.get("main_net_pct", 0)

        # 计算热度
        score = _calc_heat_score(matched)

        # 汇总资金流入
        total_inflow = sum(m.get("main_net_inflow", 0) for m in matched)

        # 构建摘要字符串
        if matched:
            parts = []
            for m in matched[:5]:  # 最多显示5个
                inflow = m.get("main_net_inflow", 0)
                inflow_str = f"{inflow/1e8:.2f}亿" if abs(inflow) >= 1e8 else f"{inflow/1e4:.0f}万"
                direction = "↑" if inflow > 0 else "↓" if inflow < 0 else "→"
                parts.append(f"{m['name']}{direction}{inflow_str}")
            inflow_str = " | ".join(parts)
        else:
            inflow_str = ""

        hot_names = ','.join(m['name'] for m in matched)

        # 注入 df
        df['concept_heat'] = score
        df['hot_concepts'] = hot_names
        df['hot_concept_count'] = len(matched)
        df['concept_main_inflow'] = total_inflow
        df['concept_inflow_str'] = inflow_str

        # 暴露为变量
        extras['stock_concept_heat'] = score
        extras['stock_concept_inflow'] = total_inflow
        extras['stock_hot_concepts'] = [
            {
                "name": m["name"],
                "rank": m["rank"],
                "change_pct": m["change_pct"],
                "main_net_inflow": m.get("main_net_inflow", 0),
                "main_net_pct": m.get("main_net_pct", 0),
            }
            for m in matched
        ]

    return extras
