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

from . import provider
from app.data_sources.normalizer import safe_float as _safe_float

logger = logging.getLogger(__name__)

# 单次回测内缓存
_hot_concepts_cache = None
_concept_flow_cache = None
_stock_concepts_cache = {}




def _get_hot_concepts(limit=50):
    """当日热门概念板块排名（带缓存）。

    取数经 `coord_index("sector_list", board_type="concept")`：sina 概念板块页为主源、
    东财 push2 为备用源，按涨跌幅降序（rank 即该顺序的位次）。
    """
    global _hot_concepts_cache
    if _hot_concepts_cache is not None:
        return _hot_concepts_cache

    try:
        from app.data_sources.coord_index import coordinate

        out = coordinate("sector_list", board_type="concept", limit=limit,
                         sort_by="f3", sort_dir="desc")
        rows = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]

        results = []
        for i, item in enumerate(rows):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            results.append({
                "rank": i + 1,
                "name": name,
                "code": str(item.get("code") or ""),
                "change_pct": _safe_float(item.get("change_pct")),
                "amount": _safe_float(item.get("amount")),
                "turnover": _safe_float(item.get("turnover")),
                "up_count": int(_safe_float(item.get("up_count"))),
                "down_count": int(_safe_float(item.get("down_count"))),
            })

        if not out.get("ok"):
            logger.warning("concept_heat: 热门概念取数失败: %s", out.get("error"))
        _hot_concepts_cache = results
        logger.info("concept_heat: 获取 %d 个热门概念", len(results))
        return results

    except Exception as e:
        logger.warning("concept_heat: 获取热门概念失败: %s", e)
        _hot_concepts_cache = []
        return []


def _get_concept_fund_flow(limit=50):
    """获取概念板块资金流向（带缓存）。

    取数经 `coord_index("sector_flow", board_type="concept")`——注意必须指定
    board_type="concept"：概念板块资金流只有东财有，而默认 industry 拿到的是
    行业板块代码（new_xxx / BK0475 系），与概念代码空间不相交，匹配必为空。
    """
    global _concept_flow_cache
    if _concept_flow_cache is not None:
        return _concept_flow_cache

    try:
        from app.data_sources.coord_index import coordinate

        out = coordinate("sector_flow", indicator="今日", board_type="concept")
        raw = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]
        if not out.get("ok"):
            logger.warning("concept_heat: 概念资金流取数失败: %s", out.get("error"))

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
