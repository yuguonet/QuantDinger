# -*- coding: utf-8 -*-
"""
Screener Tools — Agent 选股工具。

合并原 screen_stocks + smart_screen 为统一的 search_stocks。
数据源：eastmoney_search.search_stocks (东财智能选股) + 本地 DB fallback。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.tools.screener_config import (
    INDUSTRY_OPTIONS,
    CONCEPT_OPTIONS,
    MARKET_FILTER_MAP,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  东方财富 API 调用 (正本: eastmoney_search.py)
# ══════════════════════════════════════════════════════════════

def _em_search(keyword: str, page_size: int = 100) -> List[Dict[str, Any]]:
    """东财搜索封装，返回股票列表或空列表。"""
    from app.market_cn.eastmoney_search import search_stocks
    try:
        raw = search_stocks(keyword=keyword, page_size=page_size)
        return raw.get("stocks", []) if raw.get("code") == 1 else []
    except Exception as e:
        logger.warning("[东财搜索] '%s' 失败: %s", keyword, e)
        return []

def _call_eastmoney_api(keyword: str, page_size: int = 200, page_no: int = 1) -> Dict[str, Any]:
    """调东财搜索，返回原始 API 响应（code=100 格式）。供 search_stocks 的 eastmoney 模式使用。"""
    from app.market_cn.eastmoney_search import search_stocks
    # eastmoney_search.search_stocks 返回 code=1 格式，需要转换回原始 API 格式
    result = search_stocks(keyword=keyword, page_size=page_size, page_no=page_no)
    if result.get("code") == 1:
        # 转换为原始东财 API 响应格式
        return {
            "code": "100",
            "data": {
                "result": {
                    "dataList": [
                        {
                            "SECURITY_CODE": s.get("code", ""),
                            "SECURITY_SHORT_NAME": s.get("name", ""),
                            "INDUSTRY": s.get("industry", ""),
                            "CONCEPT": s.get("concept", ""),
                            "NEWEST_PRICE": s.get("new_price"),
                            "CHG": s.get("change_rate"),
                            "HIGH_PRICE": s.get("high_price"),
                            "LOW_PRICE": s.get("low_price"),
                            "PRE_CLOSE_PRICE": s.get("pre_close_price"),
                            "TRADE_VOLUME": s.get("volume"),
                            "TRADING_VOLUMES": s.get("deal_amount"),
                            "QRR": s.get("volume_ratio"),
                            "TURNOVER_RATE": s.get("turnoverrate"),
                            "AMPLITUDE": s.get("amplitude"),
                            "PE_DYNAMIC": s.get("pe9"),
                            "PB_NEW_MRQ": s.get("pbnewmrq"),
                            "TOEAL_MARKET_VALUE": s.get("total_market_cap"),
                            "FREE_CAP": s.get("free_cap"),
                        }
                        for s in result.get("stocks", [])
                    ],
                    "total": result.get("total", 0),
                },
            },
        }
    return {"code": "0", "msg": result.get("msg", "搜索失败")}

def _parse_stock_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """将东方财富返回的单只股票解析为标准格式。"""
    from app.data_sources.normalizer import safe_float as _sf
    return {
        "code": item.get("SECURITY_CODE", ""),
        "name": item.get("SECURITY_SHORT_NAME", ""),
        "industry": item.get("INDUSTRY", ""),
        "concept": item.get("CONCEPT", ""),
        "new_price": _sf(item.get("NEWEST_PRICE")),
        "change_rate": _sf(item.get("CHG")),
        "high_price": _sf(item.get("HIGH_PRICE")),
        "low_price": _sf(item.get("LOW_PRICE")),
        "pre_close_price": _sf(item.get("PRE_CLOSE_PRICE")),
        "volume": _sf(item.get("TRADE_VOLUME")),
        "deal_amount": item.get("TRADING_VOLUMES") or item.get("TRADE_AMOUNT"),
        "volume_ratio": item.get("QRR"),
        "turnover_rate": _sf(item.get("TURNOVER_RATE")),
        "amplitude": _sf(item.get("AMPLITUDE")),
        "pe_dynamic": item.get("PE_DYNAMIC") or item.get("PE9"),
        "pb_mrq": item.get("PB_NEW_MRQ"),
        "total_market_cap": item.get("TOEAL_MARKET_VALUE") or item.get("TOTAL_MARKET_CAP"),
        "free_cap": item.get("FREE_CAP"),
    }

# ══════════════════════════════════════════════════════════════
#  核心选股工具
# ══════════════════════════════════════════════════════════════

def search_stocks(
    query: str = "",
    source: str = "auto",
    filters: Optional[Dict[str, Any]] = None,
    market: str = "全部",
    top_n: int = 50,
) -> Dict[str, Any]:
    """统一选股工具：根据条件从全市场筛选股票。

    支持自然语言条件（如 "PE<20 半导体"）和结构化 filters 字典。
    source 参数控制数据源：auto(东财优先,本地DB兜底) / eastmoney / local_db。

    Args:
        query: 自然语言选股条件（如 "半导体 净利增长>15%"、"PE在5到20之间"）
        source: 数据源 — auto(自动选择) / eastmoney(东财智能选股) / local_db(本地数据库)
        filters: 结构化筛选条件字典（可选，与 query 互补）
        market: 市场筛选（全部/A股/科创板/创业板/港股/美股/ETF基金）
        top_n: 返回数量上限，默认50，最大200

    Returns:
        dict: {"stocks": [{"code": "600519", "name": "贵州茅台", "industry": "白酒", ...}, ...], "count": N}
        取第一个结果: result["stocks"][0]["code"]
    """
    top_n = min(max(top_n, 1), 200)

    # 如果有 filters 但没 query，从 filters 生成 keyword
    if filters and not query:
        query = build_keyword_from_filters(filters)
        if market == "全部" and filters.get("_market"):
            market = filters["_market"]

    if not query or not query.strip():
        return {"error": "选股条件不能为空（传入 query 或 filters）", "retriable": False}

    search_keyword = query.strip()
    if market and market != "全部" and market in MARKET_FILTER_MAP:
        search_keyword = f"{market} {search_keyword}"

    # ── eastmoney / auto 模式 ──
    if source in ("eastmoney", "auto"):
        raw = _call_eastmoney_api(search_keyword, page_size=top_n)
        if str(raw.get("code")) == "100":
            data = raw.get("data", {})
            result = data.get("result", {})
            stocks_raw = result.get("dataList", [])
            total = result.get("total", len(stocks_raw))
            stocks = [_parse_stock_item(s) for s in stocks_raw]
            return {
                "source": "eastmoney",
                "keyword": query,
                "market": market,
                "total": total,
                "count": len(stocks),
                "stocks": stocks,
            }
        elif source == "eastmoney":
            return {"error": raw.get("msg", "东财选股搜索失败"), "retriable": True}
        # auto 模式下东财失败，继续 fallback

    # ── local_db 模式 / auto fallback ──
    if source in ("local_db", "auto"):
        return _search_local_db(query, market, top_n)

    return {"error": f"未知数据源: {source}", "retriable": False}

def _search_local_db(keyword: str, market: str = "CNStock", limit: int = 50) -> Dict[str, Any]:
    """本地 DB 选股（cnstock_selection 表）。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(date) AS d FROM cnstock_selection")
            row = cur.fetchone() or {}
            target_date = str(row.get("d") or "")
            if not target_date:
                return {"source": "local_db", "stocks": [], "count": 0, "message": "选股数据为空"}

            cur.execute(
                "SELECT * FROM cnstock_selection WHERE date = %s ORDER BY id DESC LIMIT %s",
                (target_date, limit),
            )
            rows = cur.fetchall() or []
            cur.close()

        stocks = []
        for r in rows:
            d = dict(r)
            for k in ("change_rate", "turnover_rate", "volume_ratio", "new_price"):
                if d.get(k) is not None:
                    try:
                        d[k] = float(d[k])
                    except (ValueError, TypeError):
                        pass
            stock = {}
            for k in ("code", "name", "industry", "concept", "change_rate",
                       "turnover_rate", "volume_ratio", "new_price", "market"):
                if k in d:
                    stock[k] = d[k]
            stocks.append(stock)

        return {"source": "local_db", "date": target_date, "count": len(stocks), "stocks": stocks}
    except Exception as e:
        logger.error("_search_local_db failed: %s", e, exc_info=True)
        return {"source": "local_db", "stocks": [], "count": 0, "error": str(e)}

# ══════════════════════════════════════════════════════════════
#  筛选条件转换（原 screener_filters.py）
# ══════════════════════════════════════════════════════════════

def build_keyword_from_filters(filters: Dict[str, Any]) -> str:
    """将结构化筛选条件转换为自然语言关键词字符串。

    Args:
        filters: 筛选条件字典，如 {"market": "主板", "pe_range": [0, 30]}
    """
    import re
    parts: List[str] = []

    # ── 基本面：估值 ──
    pe_min = filters.get("pe_min")
    pe_max = filters.get("pe_max")
    if pe_min is not None or pe_max is not None:
        parts.append(f"PE在{pe_min or 0}到{pe_max or '∞'}之间")
    pb_min = filters.get("pb_min")
    pb_max = filters.get("pb_max")
    if pb_min is not None or pb_max is not None:
        parts.append(f"PB在{pb_min or 0}到{pb_max or '∞'}之间")
    if filters.get("dividend_min") is not None and filters["dividend_min"] > 0:
        parts.append(f"股息率不低于{filters['dividend_min']}%")
    if filters.get("roe_min") is not None and filters["roe_min"] > -50:
        parts.append(f"ROE不低于{filters['roe_min']}%")
    if filters.get("sale_gpr_min") is not None and filters["sale_gpr_min"] > -50:
        parts.append(f"毛利率不低于{filters['sale_gpr_min']}%")

    # ── 基本面：成长 checkbox ──
    growth_map = {
        "netprofit_yoy_ratio": "净利增长>15%",
        "toi_yoy_ratio": "营收增长>15%",
        "basiceps_yoy_ratio": "每股收益增长>10%",
        "income_growthrate_3y": "营收3年复合增长 > 10%",
        "netprofit_growthrate_3y": "净利润3年复合增长 > 10%",
    }
    for k in filters.get("growth_indicators", []):
        if k in growth_map:
            parts.append(growth_map[k])
    for k in filters.get("quality_indicators", []):
        if k == "per_netcash_operate":
            parts.append("经营现金流为正")

    # ── 技术面：均线突破 ──
    ma_map = {
        "breakup_ma_5days": "突破5日线", "breakup_ma_10days": "突破10日线",
        "breakup_ma_20days": "突破20日线", "breakup_ma_60days": "突破60日线",
        "long_avg_array": "长期均线多头排列",
    }
    for k in filters.get("ma_breakthrough", []):
        if k in ma_map:
            parts.append(ma_map[k])

    # ── 技术面：技术指标 ──
    tech_map = {
        "macd_golden_fork": "MACD金叉", "kdj_golden_fork": "KDJ金叉",
        "break_through": "突破形态", "upper_large_volume": "放量上涨",
        "down_narrow_volume": "缩量下跌",
    }
    for k in filters.get("tech_signals", []):
        if k in tech_map:
            parts.append(tech_map[k])

    # ── 经典K线形态 ──
    k_classic_map = {
        "one_dayang_line": "大阳线", "two_dayang_lines": "两阳夹一阴",
        "rise_sun": "阳包阴", "morning_star": "早晨之星",
        "evening_star": "黄昏之星", "shooting_star": "射击之星",
        "three_black_crows": "三只乌鸦", "hammer": "锤头",
        "inverted_hammer": "倒锤头", "doji": "十字星",
        "long_legged_doji": "长腿十字线", "gravestone": "墓碑线",
        "dragonfly": "蜻蜓线", "two_flying_crows": "双飞乌鸦",
        "lotus_emerge": "出水芙蓉", "low_open_high": "低开高走",
        "huge_volume": "巨量",
        "bottom_cross_harami": "底部十字孕线",
        "top_cross_harami": "顶部十字孕线",
    }
    for k in filters.get("k_classic", []):
        if k in k_classic_map:
            parts.append(k_classic_map[k])

    k_intraday_map = {
        "tail_plate_rise": "尾盘拉升", "intraday_pressure": "盘中打压",
        "intraday_rise": "盘中拉升", "quick_rebound": "快速反弹",
    }
    for k in filters.get("k_intraday", []):
        if k in k_intraday_map:
            parts.append(k_intraday_map[k])
    for k in filters.get("k_other", []):
        if k == "limit_up":
            parts.append("一字涨停")
        if k == "limit_down":
            parts.append("一字跌停")

    # ── 资金面 ──
    flow_map = {
        "low_funds_inflow": "主力资金净流入", "high_funds_outflow": "主力资金净流出",
        "netinflow_3days": "近3日资金净流入", "netinflow_5days": "近5日资金净流入",
    }
    for k in filters.get("capital_flow", []):
        if k in flow_map:
            parts.append(flow_map[k])
    if filters.get("volume_ratio_min") is not None and filters["volume_ratio_min"] > 0:
        parts.append(f"量比不低于{filters['volume_ratio_min']}")
    if filters.get("turnoverrate_min") is not None and filters["turnoverrate_min"] > 0:
        parts.append(f"换手率不低于{filters['turnoverrate_min']}%")

    inst_map = {
        "org_survey_3m": "近3月有机构调研", "allcorp_fund_ratio": "基金重仓",
        "allcorp_qs_ratio": "券商重仓",
    }
    for k in filters.get("institutional_holding", []):
        if k in inst_map:
            parts.append(inst_map[k])

    # ── 行情指标 ──
    if filters.get("mi_volume_ratio_min") is not None and filters["mi_volume_ratio_min"] > 0:
        parts.append(f"量比≥{filters['mi_volume_ratio_min']}")
    if filters.get("mi_turnover_rate_min") is not None and filters["mi_turnover_rate_min"] > 0:
        parts.append(f"换手率≥{filters['mi_turnover_rate_min']}%")
    if filters.get("mi_volume_min") is not None and filters["mi_volume_min"] > 0:
        parts.append(f"成交量≥{filters['mi_volume_min']}手")
    if filters.get("mi_amount_min") is not None and filters["mi_amount_min"] > 0:
        parts.append(f"成交额≥{filters['mi_amount_min']}元")
    for k in filters.get("tiger_participant", []):
        if k == "inst_participated":
            parts.append("机构参与")
        if k == "dept_participated":
            parts.append("营业部参与")

    industries = filters.get("industry", [])
    if industries:
        parts.append(f"属于行业({', '.join(industries)})")
    concepts = filters.get("concept", [])
    if concepts:
        parts.append(f"涉及概念({', '.join(concepts)})")

    # ── 新增基本面 ──
    ps_min, ps_max = filters.get("ps_min"), filters.get("ps_max")
    if ps_min is not None or ps_max is not None:
        parts.append(f"市销率{ps_min or 0}~{ps_max or '∞'}")
    pcf_min, pcf_max = filters.get("pcf_min"), filters.get("pcf_max")
    if pcf_min is not None or pcf_max is not None:
        parts.append(f"市现率{pcf_min or 0}~{pcf_max or '∞'}")
    dtsyl_min, dtsyl_max = filters.get("dtsyl_min"), filters.get("dtsyl_max")
    if dtsyl_min is not None or dtsyl_max is not None:
        parts.append(f"动态PE{dtsyl_min or 0}~{dtsyl_max or '∞'}")
    tmc_min, tmc_max = filters.get("total_market_cap_min"), filters.get("total_market_cap_max")
    if tmc_min is not None or tmc_max is not None:
        parts.append(f"总市值{tmc_min or 0}~{tmc_max or '∞'}")
    fmc_min, fmc_max = filters.get("free_cap_min"), filters.get("free_cap_max")
    if fmc_min is not None or fmc_max is not None:
        parts.append(f"流通市值{fmc_min or 0}~{fmc_max or '∞'}")
    if filters.get("basic_eps_min") is not None:
        parts.append(f"每股收益≥{filters['basic_eps_min']}")
    if filters.get("bvps_min") is not None:
        parts.append(f"每股净资产≥{filters['bvps_min']}")
    if filters.get("per_fcfe_min") is not None:
        parts.append(f"每股自由现金流≥{filters['per_fcfe_min']}")
    if filters.get("parent_netprofit_min") is not None:
        parts.append(f"归母净利润≥{filters['parent_netprofit_min']}")
    if filters.get("deduct_netprofit_min") is not None:
        parts.append(f"扣非净利润≥{filters['deduct_netprofit_min']}")
    if filters.get("total_operate_income_min") is not None:
        parts.append(f"营业收入≥{filters['total_operate_income_min']}")
    if filters.get("jroa_min") is not None:
        parts.append(f"总资产报酬率≥{filters['jroa_min']}%")
    if filters.get("roic_min") is not None:
        parts.append(f"投资回报率≥{filters['roic_min']}%")
    if filters.get("sale_npr_min_filter") is not None:
        parts.append(f"销售净利率≥{filters['sale_npr_min_filter']}%")
    da_min, da_max = filters.get("debt_asset_ratio_min"), filters.get("debt_asset_ratio_max")
    if da_min is not None or da_max is not None:
        if da_min is not None and da_max is not None:
            parts.append(f"资产负债率{da_min}~{da_max}%")
        elif da_max is not None:
            parts.append(f"资产负债率≤{da_max}%")
    if filters.get("current_ratio_min") is not None:
        parts.append(f"流动比率≥{filters['current_ratio_min']}")
    if filters.get("speed_ratio_min") is not None:
        parts.append(f"速动比率≥{filters['speed_ratio_min']}")
    ts_min, ts_max = filters.get("total_shares_min"), filters.get("total_shares_max")
    if ts_min is not None or ts_max is not None:
        parts.append(f"总股本{ts_min or 0}~{ts_max or '∞'}")
    fs_min, fs_max = filters.get("free_shares_min"), filters.get("free_shares_max")
    if fs_min is not None or fs_max is not None:
        parts.append(f"流通股本{fs_min or 0}~{fs_max or '∞'}")
    hn_min, hn_max = filters.get("holder_newest_min"), filters.get("holder_newest_max")
    if hn_min is not None or hn_max is not None:
        parts.append(f"股东数{hn_min or 0}~{hn_max or '∞'}")

    # ── 技术指标补充 ──
    for k in filters.get("ma_30_break", []):
        if k == "breakup_ma_30days":
            parts.append("突破30日线")
    kdj_map = {
        "kdj_golden_forkz": "KDJ金叉Z", "kdj_golden_forky": "KDJ金叉Y",
        "macd_golden_forkz": "MACD金叉Z", "macd_golden_forky": "MACD金叉Y",
    }
    for k in filters.get("kdj_signals", []):
        if k in kdj_map:
            parts.append(kdj_map[k])
    pattern_map = {
        "power_fulgun": "乌云盖顶", "pregnant": "孕线",
        "black_cloud_tops": "黑云压顶", "narrow_finish": "窄幅整理",
        "reversing_hammer": "反转锤子", "first_dawn": "第一天黎明",
        "bearish_engulfing": "看跌吞没", "upside_volume": "上攻放量",
        "heaven_rule": "天道法则",
    }
    for k in filters.get("pattern_signals", []):
        if k in pattern_map:
            parts.append(pattern_map[k])
    consec_map = {
        "down_7days": "连续7天下跌", "upper_8days": "连续8天上涨",
        "upper_9days": "连续9天上涨", "upper_4days": "连续4天上涨",
    }
    for k in filters.get("consecutive_signals", []):
        if k in consec_map:
            parts.append(consec_map[k])
    for k in filters.get("volume_trend", []):
        if k == "short_avg_array":
            parts.append("短期均线多头")
        if k == "restore_justice":
            parts.append("复权")

    # ── 资金面数值 ──
    if filters.get("net_inflow_min") is not None:
        parts.append(f"净流入≥{filters['net_inflow_min']}")
    if filters.get("ddx_min") is not None:
        parts.append(f"大单动向≥{filters['ddx_min']}")
    if filters.get("netinflow_min_3d") is not None:
        parts.append(f"3日净流入≥{filters['netinflow_min_3d']}")
    if filters.get("netinflow_min_5d") is not None:
        parts.append(f"5日净流入≥{filters['netinflow_min_5d']}")
    if filters.get("changerate_3d_min") is not None:
        parts.append(f"3日涨幅≥{filters['changerate_3d_min']}%")
    if filters.get("changerate_5d_min") is not None:
        parts.append(f"5日涨幅≥{filters['changerate_5d_min']}%")
    if filters.get("changerate_10d_min") is not None:
        parts.append(f"10日涨幅≥{filters['changerate_10d_min']}%")
    cty_min, cty_max = filters.get("changerate_ty_min"), filters.get("changerate_ty_max")
    if cty_min is not None or cty_max is not None:
        parts.append(f"年度涨幅{cty_min if cty_min is not None else '-∞'}~{cty_max if cty_max is not None else '∞'}%")

    # ── 机构/股东 ──
    if filters.get("holder_change_3m_min") is not None:
        parts.append(f"3月持股变动≥{filters['holder_change_3m_min']}%")
    if filters.get("executive_change_3m_min") is not None:
        parts.append(f"3月高管持股变动≥{filters['executive_change_3m_min']}%")
    if filters.get("org_rating_filter"):
        parts.append(f"机构评级={filters['org_rating_filter']}")
    if filters.get("allcorp_ratio_min") is not None:
        parts.append(f"机构持股比例≥{filters['allcorp_ratio_min']}%")
    if filters.get("allcorp_fund_ratio_min") is not None:
        parts.append(f"基金持股≥{filters['allcorp_fund_ratio_min']}%")
    if filters.get("allcorp_qs_ratio_min") is not None:
        parts.append(f"券商持股≥{filters['allcorp_qs_ratio_min']}%")
    if filters.get("allcorp_qfii_ratio_min") is not None:
        parts.append(f"QFII持股≥{filters['allcorp_qfii_ratio_min']}%")

    # ── 新高新低 ──
    hl_map = {
        "now_newhigh": "当前新高", "now_newlow": "当前新低",
        "high_recent_3days": "3天新高", "high_recent_5days": "5天新高",
        "high_recent_10days": "10天新高", "high_recent_20days": "20天新高",
        "high_recent_30days": "30天新高",
        "low_recent_3days": "3天新低", "low_recent_5days": "5天新低",
        "low_recent_10days": "10天新低", "low_recent_20days": "20天新低",
        "low_recent_30days": "30天新低",
    }
    for k in filters.get("new_high_filter", []):
        if k in hl_map:
            parts.append(hl_map[k])
    for k in filters.get("win_market_filter", []):
        m = re.match(r"win_market_(\d+)days", k)
        if m:
            parts.append(f"{m.group(1)}天战胜大盘")
    board_map = {
        "is_sz50": "上证50成分股", "is_zz1000": "中证1000成分股",
        "is_cy50": "创业板50成分股", "is_bps_break": "已破净",
        "is_issue_break": "已破板",
    }
    for k in filters.get("hs_board_filter", []):
        if k in board_map:
            parts.append(board_map[k])
    if filters.get("par_dividend_min") is not None:
        parts.append(f"派息率≥{filters['par_dividend_min']}%")
    if filters.get("pledge_ratio_max") is not None:
        parts.append(f"质押比例≤{filters['pledge_ratio_max']}%")
    if filters.get("goodwill_max") is not None:
        parts.append(f"商誉≤{filters['goodwill_max']}")
    for k in filters.get("limited_lift_filter", []):
        if k == "limited_lift_6m":
            parts.append("限价上涨6月")
        if k == "limited_lift_1y":
            parts.append("限价上涨1年")
    seo_map = {
        "directional_seo_1m": "定向增发1月", "directional_seo_3m": "定向增发3月",
        "directional_seo_6m": "定向增发6月", "directional_seo_1y": "定向增发1年",
    }
    for k in filters.get("directional_seo_filter", []):
        if k in seo_map:
            parts.append(seo_map[k])
    pledge_map = {
        "equity_pledge_1m": "股权质押1月", "equity_pledge_3m": "股权质押3月",
        "equity_pledge_6m": "股权质押6月", "equity_pledge_1y": "股权质押1年",
    }
    for k in filters.get("equity_pledge_filter", []):
        if k in pledge_map:
            parts.append(pledge_map[k])

    return "; ".join(parts)


def get_screener_presets() -> Dict[str, Any]:
    """获取选股器支持的所有筛选条件分类和示例。"""
    return {
        "categories": {
            "基本面": {
                "groups": {
                    "估值指标": ["PE在X到Y之间", "PB在X到Y之间", "股息率不低于X%"],
                    "成长能力": ["净利增长>15%", "营收增长>15%", "每股收益增长>10%"],
                    "盈利能力": ["ROE不低于X%", "毛利率不低于X%", "经营现金流为正"],
                },
            },
            "技术面": {
                "groups": {
                    "均线突破": ["突破5日线", "突破10日线", "突破20日线", "突破60日线"],
                    "技术指标": ["MACD金叉", "KDJ金叉", "放量上涨", "缩量下跌"],
                    "K线形态": ["大阳线", "早晨之星", "十字星", "锤头", "红三兵"],
                },
            },
            "资金面": {
                "groups": {
                    "资金流向": ["主力资金净流入", "近3日资金净流入"],
                    "机构持股": ["近3月有机构调研", "基金重仓", "券商重仓"],
                },
            },
            "概念/行业": {
                "groups": {
                    "行业分类": INDUSTRY_OPTIONS,
                    "概念题材": CONCEPT_OPTIONS,
                },
            },
        },
        "markets": list(MARKET_FILTER_MAP.keys()),
        "tips": [
            "多个条件用分号(;)分隔",
            "可混合不同类别的条件",
            "也可直接用自然语言描述",
        ],
    }


# ══════════════════════════════════════════════════════════════
#  工具声明
# ══════════════════════════════════════════════════════════════

