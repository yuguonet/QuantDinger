"""
sector_flow — 个股所属板块/概念的资金流向

通过 symbol 查询该股票所属的所有行业 & 概念板块，再逐个获取板块资金流向。

自动注入 df 的列：
    sector_main_inflow      所属行业板块主力净流入 (元)
    sector_main_net_pct     所属行业板块主力净占比 (%)
    concept_main_inflow     所属概念板块主力净流入合计 (元, 加权和)
    concept_flow_detail     概念板块资金流向明细 (字符串摘要)

脚本可用变量：
    stock_sector_flow       所属行业板块资金流向 {name, main_net_inflow, main_net_pct, ...}
    stock_concept_flows     所属概念板块资金流向列表 [{name, code, main_net_inflow, main_net_pct, ...}]
    stock_all_board_flow    汇总 {sector_inflow, concept_inflow_total, concept_count}
"""
import logging

from . import provider

logger = logging.getLogger(__name__)

_cache = {}
_flow_cache = {}




def _fetch_board_list(board_type="industry", limit=200):
    """板块列表（名称 → 代码映射）。

    取数经 `coord_index("sector_list")`：sina 板块页为主源、东财 push2 备用。
    ⚠ 不同源的代码空间不同（sina `new_xxx` / `gn_xxx`，东财 `BKxxxx`），
    因此下游按板块**名称**取资金流，不能拿代码跨源匹配。
    """
    cache_key = f"board_list_{board_type}_{limit}"
    if cache_key in _cache:
        return _cache[cache_key]

    try:
        from app.data_sources.coord_index import coordinate

        out = coordinate("sector_list", board_type=board_type, limit=limit,
                         sort_by="f3", sort_dir="desc")
        rows = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]

        results = {}
        for item in rows:
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if code and name:
                results[name] = code

        if not out.get("ok"):
            logger.warning("sector_flow: 板块列表取数失败(%s): %s", board_type, out.get("error"))
        _cache[cache_key] = results
        logger.info("sector_flow: 获取 %d 个%s板块", len(results), board_type)
        return results

    except Exception as e:
        logger.warning("sector_flow: 获取%s板块列表失败: %s", board_type, e)
        _cache[cache_key] = {}
        return {}


def _fetch_board_fund_flow(board_name: str, board_type: str = "industry") -> dict:
    """获取单个板块的资金流向数据（按板块名称匹配，跨源安全）。

    取数经 `coord_index("sector_flow", board_type=...)`；概念板块只有东财有源，
    所以这里必须把 board_type 透传下去（历史实现固定取行业流，概念永远匹配不到）。
    """
    if not board_name:
        return {}

    cache_key = (board_type, board_name)
    if cache_key in _flow_cache:
        return _flow_cache[cache_key]

    try:
        from app.data_sources.coord_index import coordinate

        out = coordinate("sector_flow", indicator="今日", board_type=board_type)
        raw = [r for r in ((out.get("data") or {}).get("rows") or []) if isinstance(r, dict)]
        if not out.get("ok"):
            logger.debug("sector_flow: 板块资金流取数失败(%s): %s", board_type, out.get("error"))

        for item in raw:
            if str(item.get("name") or "").strip() == board_name:
                result = {
                    "date": "",
                    "main_net_inflow": item.get("main_net", 0),
                    "small_net_inflow": item.get("small_net", 0),
                    "mid_net_inflow": item.get("mid_net", 0),
                    "big_net_inflow": item.get("large_net", 0),
                    "super_net_inflow": item.get("super_net", 0),
                }
                _flow_cache[cache_key] = result
                return result

        _flow_cache[cache_key] = {}
        return {}

    except Exception as e:
        logger.debug("sector_flow: 获取 %s 资金流向失败: %s", board_name, e)
        _flow_cache[cache_key] = {}
        return {}


def _get_stock_boards(symbol: str) -> dict:
    """从 basicinfo_db 获取股票所属行业和概念。"""
    if not symbol:
        return {"industry": "", "concepts": []}

    cache_key = f"stock_{symbol}"
    if cache_key in _cache:
        return _cache[cache_key]

    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        info = get_stock_basic_db().get_stock(symbol) or {}
        industry = str(info.get('industry', '') or '').strip()
        concepts_raw = str(info.get('concepts', '') or '').strip()
        concept_list = [c.strip() for c in concepts_raw.split(',') if c.strip()]
    except Exception as e:
        logger.debug("sector_flow: 查询 %s 失败: %s", symbol, e)
        industry = ""
        concept_list = []

    result = {"industry": industry, "concepts": concept_list}
    _cache[cache_key] = result
    return result


@provider
def register(ctx: dict) -> dict:
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')

    extras = {}

    if symbol and df is not None and len(df) > 0:
        boards = _get_stock_boards(symbol)
        industry = boards["industry"]
        concepts = boards["concepts"]

        # 获取板块列表（名称 -> 代码映射）
        industry_map = _fetch_board_list("industry", limit=200)
        concept_map = _fetch_board_list("concept", limit=500)

        # ── 行业板块资金流向 ──
        sector_flow = {}
        if industry:
            board_code = industry_map.get(industry, "")
            if board_code:
                raw = _fetch_board_fund_flow(industry, "industry")
                if raw:
                    sector_flow = {
                        "name": industry,
                        "code": board_code,
                        "date": raw.get("date", ""),
                        "main_net_inflow": raw.get("main_net_inflow", 0),
                        "super_net_inflow": raw.get("super_net_inflow", 0),
                        "big_net_inflow": raw.get("big_net_inflow", 0),
                        "mid_net_inflow": raw.get("mid_net_inflow", 0),
                        "small_net_inflow": raw.get("small_net_inflow", 0),
                    }

        # ── 概念板块资金流向 ──
        concept_flows = []
        for concept_name in concepts:
            board_code = concept_map.get(concept_name, "")
            if not board_code:
                continue
            raw = _fetch_board_fund_flow(concept_name, "concept")
            if raw:
                concept_flows.append({
                    "name": concept_name,
                    "code": board_code,
                    "date": raw.get("date", ""),
                    "main_net_inflow": raw.get("main_net_inflow", 0),
                    "super_net_inflow": raw.get("super_net_inflow", 0),
                    "big_net_inflow": raw.get("big_net_inflow", 0),
                    "mid_net_inflow": raw.get("mid_net_inflow", 0),
                    "small_net_inflow": raw.get("small_net_inflow", 0),
                })

        # 汇总
        sector_inflow = sector_flow.get("main_net_inflow", 0)
        concept_inflow_total = sum(cf.get("main_net_inflow", 0) for cf in concept_flows)

        # 注入 df
        df['sector_main_inflow'] = sector_inflow
        df['sector_main_net_pct'] = 0.0  # 占比从行业板块API单独取
        df['concept_main_inflow'] = concept_inflow_total

        # 概念流向摘要
        if concept_flows:
            parts = []
            for cf in sorted(concept_flows, key=lambda x: abs(x.get("main_net_inflow", 0)), reverse=True)[:5]:
                v = cf["main_net_inflow"]
                label = f"{v/1e8:.2f}亿" if abs(v) >= 1e8 else f"{v/1e4:.0f}万"
                arrow = "↑" if v > 0 else "↓" if v < 0 else "→"
                parts.append(f"{cf['name']}{arrow}{label}")
            detail_str = " | ".join(parts)
        else:
            detail_str = ""

        df['concept_flow_detail'] = detail_str

        # 暴露变量
        extras['stock_sector_flow'] = sector_flow
        extras['stock_concept_flows'] = concept_flows
        extras['stock_all_board_flow'] = {
            "sector_inflow": sector_inflow,
            "concept_inflow_total": concept_inflow_total,
            "concept_count": len(concept_flows),
        }

    return extras
