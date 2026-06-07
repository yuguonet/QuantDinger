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
import requests

from . import provider
from app.data_sources.normalizer import safe_float as _safe_float

logger = logging.getLogger(__name__)

_cache = {}
_flow_cache = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}




def _fetch_board_list(board_type="industry", limit=200):
    """获取板块列表（代码+名称映射）。"""
    cache_key = f"board_list_{board_type}_{limit}"
    if cache_key in _cache:
        return _cache[cache_key]

    fs_map = {"industry": "b:BK0475", "concept": "b:BK0815"}
    fs = fs_map.get(board_type, fs_map["industry"])

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
            "fs": fs,
            "fields": "f12,f14",
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        data = resp.json()
        items = (data.get("data") or {}).get("diff") or []

        results = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("f12") or "").strip()
            name = str(item.get("f14") or "").strip()
            if code and name:
                results[name] = code

        _cache[cache_key] = results
        logger.info("sector_flow: 获取 %d 个%s板块", len(results), board_type)
        return results

    except Exception as e:
        logger.warning("sector_flow: 获取%s板块列表失败: %s", board_type, e)
        _cache[cache_key] = {}
        return {}


def _fetch_board_fund_flow(board_code: str) -> dict:
    """获取单个板块的资金流向数据（通过 index.py 多源接口）。"""
    if not board_code:
        return {}

    if board_code in _flow_cache:
        return _flow_cache[board_code]

    try:
        from app.market_cn.index import get_sector_fund_flow
        raw = get_sector_fund_flow("今日")

        # 按板块代码查找
        for item in raw:
            if item.get("code") == board_code:
                result = {
                    "date": "",
                    "main_net_inflow": item.get("main_net", 0),
                    "small_net_inflow": item.get("small_net", 0),
                    "mid_net_inflow": item.get("mid_net", 0),
                    "big_net_inflow": item.get("large_net", 0),
                    "super_net_inflow": item.get("super_net", 0),
                }
                _flow_cache[board_code] = result
                return result

        _flow_cache[board_code] = {}
        return {}

    except Exception as e:
        logger.debug("sector_flow: 获取 %s 资金流向失败: %s", board_code, e)
        _flow_cache[board_code] = {}
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
                raw = _fetch_board_fund_flow(board_code)
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
            raw = _fetch_board_fund_flow(board_code)
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
