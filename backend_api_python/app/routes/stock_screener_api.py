# -*- coding: utf-8 -*-
"""
/api/stock-screener/* — 选股器独立 API 路由

将前端选股器的全部后端能力暴露为 HTTP 接口。
不替换任何现有代码，作为独立 Blueprint 注册。

端点：
  POST /api/stock-screener/search    → 智能选股搜索（keyword 或 filters）
  GET  /api/stock-screener/presets   → 获取预设条件分类
  GET  /api/stock-screener/filters   → 获取筛选条件结构（默认值）
  POST /api/stock-screener/parse     → 自然语言 → 结构化条件
  POST /api/stock-screener/build     → 结构化条件 → 自然语言
  POST /api/stock-screener/batch     → 批量筛选（多条件组合）
"""
from flask import Blueprint, request, jsonify
import re
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

stock_screener_bp = Blueprint("stock_screener", __name__)


# ══════════════════════════════════════════════════════════════
#  内部工具函数（仅本文件使用）
# ══════════════════════════════════════════════════════════════

def _get_default_filters() -> Dict[str, Any]:
    """返回空筛选条件的默认值字典（130+ 字段）。"""
    return {
        "pe_min": None, "pe_max": None, "pb_min": None, "pb_max": None,
        "dividend_min": None, "growth_indicators": [], "quality_indicators": [],
        "roe_min": None, "sale_gpr_min": None, "sale_npr_min": None, "sale_npr_min_filter": None,
        "ma_breakthrough": [], "ma_30_break": [], "tech_signals": [], "kdj_signals": [],
        "k_classic": [], "k_intraday": [], "k_other": [], "pattern_signals": [],
        "volume_trend": [], "consecutive_signals": [], "capital_flow": [],
        "volume_ratio_min": None, "turnoverrate_min": None, "institutional_holding": [],
        "net_inflow_min": None, "ddx_min": None, "netinflow_min_3d": None, "netinflow_min_5d": None,
        "changerate_3d_min": None, "changerate_5d_min": None, "changerate_10d_min": None,
        "changerate_ty_min": None, "changerate_ty_max": None,
        "industry": [], "concept": [],
        "mi_volume_ratio_min": None, "mi_volume_ratio_max": None,
        "mi_turnover_rate_min": None, "mi_turnover_rate_max": None,
        "mi_amplitude_min": None, "mi_amplitude_max": None,
        "mi_volume_min": None, "mi_volume_max": None,
        "mi_amount_min": None, "mi_amount_max": None,
        "mi_pe_min": None, "mi_pe_max": None,
        "mi_float_mc_min": None, "mi_float_mc_max": None,
        "mi_total_mc_min": None, "mi_total_mc_max": None,
        "mi_comp_ratio_min": None, "mi_comp_ratio_max": None,
        "mi_today_up_min": None, "mi_today_up_max": None,
        "mi_change_5d_min": None, "mi_change_5d_max": None,
        "mi_change_10d_min": None, "mi_change_10d_max": None,
        "mi_change_60d_min": None, "mi_change_60d_max": None,
        "mi_change_ytd_min": None, "mi_change_ytd_max": None,
        "mi_close_min": None, "mi_close_max": None,
        "mi_net_in_min": None, "mi_net_in_max": None,
        "ps_min": None, "ps_max": None, "pcf_min": None, "pcf_max": None,
        "dtsyl_min": None, "dtsyl_max": None,
        "total_market_cap_min": None, "total_market_cap_max": None,
        "free_cap_min": None, "free_cap_max": None,
        "basic_eps_min": None, "bvps_min": None, "per_fcfe_min": None,
        "parent_netprofit_min": None, "deduct_netprofit_min": None,
        "total_operate_income_min": None, "jroa_min": None, "roic_min": None,
        "debt_asset_ratio_min": None, "debt_asset_ratio_max": None,
        "current_ratio_min": None, "speed_ratio_min": None,
        "total_shares_min": None, "total_shares_max": None,
        "free_shares_min": None, "free_shares_max": None,
        "holder_newest_min": None, "holder_newest_max": None,
        "holder_change_3m_min": None, "executive_change_3m_min": None,
        "org_rating_filter": "", "allcorp_ratio_min": None,
        "allcorp_fund_ratio_min": None, "allcorp_qs_ratio_min": None,
        "allcorp_qfii_ratio_min": None,
        "new_high_filter": [], "win_market_filter": [], "hs_board_filter": [],
        "par_dividend_min": None, "pledge_ratio_max": None, "goodwill_max": None,
        "limited_lift_filter": [], "directional_seo_filter": [], "equity_pledge_filter": [],
        "ch_cost_price_min": None, "ch_cost_price_max": None,
        "ch_profit_ratio_min": None, "ch_profit_ratio_max": None,
        "ch_avg_cost_min": None, "ch_avg_cost_max": None,
        "ch_conc_90_min": None, "ch_conc_90_max": None,
        "ch_conc_70_min": None, "ch_conc_70_max": None,
        "ch_holder_count_min": None, "ch_holder_count_max": None,
        "tiger_date_min": None, "tiger_date_max": None,
        "tiger_buy_min": None, "tiger_buy_max": None,
        "tiger_sell_min": None, "tiger_sell_max": None,
        "tiger_net_min": None, "tiger_net_max": None,
        "tiger_dept_buy_min": None, "tiger_dept_buy_max": None,
        "tiger_inst_buy_min": None, "tiger_inst_buy_max": None,
        "tiger_participant": [],
        "ti_ma5_min": None, "ti_ma5_max": None, "ti_ma10_min": None, "ti_ma10_max": None,
        "ti_ma20_min": None, "ti_ma20_max": None, "ti_ma60_min": None, "ti_ma60_max": None,
        "ti_ma120_min": None, "ti_ma120_max": None,
    }


def _parse_number(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.replace(",", "").strip()
    if s in ("∞", "-∞"):
        return None
    try:
        if s.endswith("千亿"):
            return float(s[:-3]) * 100000000000
        if s.endswith("亿"):
            return float(s[:-1]) * 100000000
        if s.endswith("万"):
            return float(s[:-1]) * 10000
        if s.endswith(("手", "元", "%")):
            return float(s[:-1])
        return float(s)
    except ValueError:
        return None


def _parse_range(s: str) -> Tuple[Optional[float], Optional[float]]:
    parts = s.split("~")
    lo = _parse_number(parts[0]) if len(parts) > 0 else None
    hi = _parse_number(parts[1]) if len(parts) > 1 else None
    return lo, hi


def _parse_filters_from_text(text: str) -> Dict[str, Any]:
    """将自然语言文本解析为结构化筛选条件。"""
    filters = _get_default_filters()
    if not text or not text.strip():
        return filters

    parts = [s.strip() for s in re.split(r"[;；]", text) if s.strip()]
    market_map = {"全部": "全部", "A股": "A股", "沪深300": "沪深300", "中证500": "中证500",
                  "科创板": "科创板", "创业板": "创业板", "港股": "港股", "美股": "美股", "ETF基金": "ETF基金"}

    for part in parts:
        m = None
        if part in market_map:
            filters["_market"] = part; continue

        m = re.match(r"PE在(.+?)到(.+?)之间", part)
        if m:
            filters["pe_min"] = _parse_number(m.group(1)); filters["pe_max"] = _parse_number(m.group(2)); continue
        m = re.match(r"PB在(.+?)到(.+?)之间", part)
        if m:
            filters["pb_min"] = _parse_number(m.group(1)); filters["pb_max"] = _parse_number(m.group(2)); continue

        for regex, key in [
            (r"股息率不低于(.+?)%", "dividend_min"), (r"ROE不低于(.+?)%", "roe_min"),
            (r"毛利率不低于(.+?)%", "sale_gpr_min"),
        ]:
            m = re.match(regex, part)
            if m:
                filters[key] = float(m.group(1)); break
        if m: continue

        m = re.match(r"量比不低于(.+)", part)
        if m: filters["volume_ratio_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"换手率不低于(.+?)%", part)
        if m: filters["turnoverrate_min"] = float(m.group(1)); continue

        text_map = {
            "净利增长>15%": ("growth_indicators", "netprofit_yoy_ratio"),
            "营收增长>15%": ("growth_indicators", "toi_yoy_ratio"),
            "每股收益增长>10%": ("growth_indicators", "basiceps_yoy_ratio"),
            "经营现金流为正": ("quality_indicators", "per_netcash_operate"),
            "突破5日线": ("ma_breakthrough", "breakup_ma_5days"),
            "突破10日线": ("ma_breakthrough", "breakup_ma_10days"),
            "突破20日线": ("ma_breakthrough", "breakup_ma_20days"),
            "突破60日线": ("ma_breakthrough", "breakup_ma_60days"),
            "长期均线多头排列": ("ma_breakthrough", "long_avg_array"),
            "MACD金叉": ("tech_signals", "macd_golden_fork"),
            "KDJ金叉": ("tech_signals", "kdj_golden_fork"),
            "放量上涨": ("tech_signals", "upper_large_volume"),
            "缩量下跌": ("tech_signals", "down_narrow_volume"),
            "突破形态": ("tech_signals", "break_through"),
            "大阳线": ("k_classic", "one_dayang_line"), "两阳夹一阴": ("k_classic", "two_dayang_lines"),
            "阳包阴": ("k_classic", "rise_sun"), "早晨之星": ("k_classic", "morning_star"),
            "黄昏之星": ("k_classic", "evening_star"), "射击之星": ("k_classic", "shooting_star"),
            "三只乌鸦": ("k_classic", "three_black_crows"), "锤头": ("k_classic", "hammer"),
            "倒锤头": ("k_classic", "inverted_hammer"), "十字星": ("k_classic", "doji"),
            "长腿十字线": ("k_classic", "long_legged_doji"), "墓碑线": ("k_classic", "gravestone"),
            "蜻蜓线": ("k_classic", "dragonfly"), "双飞乌鸦": ("k_classic", "two_flying_crows"),
            "出水芙蓉": ("k_classic", "lotus_emerge"), "低开高走": ("k_classic", "low_open_high"),
            "巨量": ("k_classic", "huge_volume"),
            "底部十字孕线": ("k_classic", "bottom_cross_harami"),
            "顶部十字孕线": ("k_classic", "top_cross_harami"),
            "尾盘拉升": ("k_intraday", "tail_plate_rise"), "盘中打压": ("k_intraday", "intraday_pressure"),
            "盘中拉升": ("k_intraday", "intraday_rise"), "快速反弹": ("k_intraday", "quick_rebound"),
            "一字涨停": ("k_other", "limit_up"), "一字跌停": ("k_other", "limit_down"),
            "主力资金净流入": ("capital_flow", "low_funds_inflow"),
            "主力资金净流出": ("capital_flow", "high_funds_outflow"),
            "近3日资金净流入": ("capital_flow", "netinflow_3days"),
            "近5日资金净流入": ("capital_flow", "netinflow_5days"),
            "近3月有机构调研": ("institutional_holding", "org_survey_3m"),
            "基金重仓": ("institutional_holding", "allcorp_fund_ratio"),
            "券商重仓": ("institutional_holding", "allcorp_qs_ratio"),
            "当前新高": ("new_high_filter", "now_newhigh"), "当前新低": ("new_high_filter", "now_newlow"),
            "3天新高": ("new_high_filter", "high_recent_3days"),
            "5天新高": ("new_high_filter", "high_recent_5days"),
            "10天新高": ("new_high_filter", "high_recent_10days"),
            "20天新高": ("new_high_filter", "high_recent_20days"),
            "30天新高": ("new_high_filter", "high_recent_30days"),
            "3天新低": ("new_high_filter", "low_recent_3days"),
            "5天新低": ("new_high_filter", "low_recent_5days"),
            "10天新低": ("new_high_filter", "low_recent_10days"),
            "20天新低": ("new_high_filter", "low_recent_20days"),
            "30天新低": ("new_high_filter", "low_recent_30days"),
            "连续4天上涨": ("consecutive_signals", "upper_4days"),
            "连续8天上涨": ("consecutive_signals", "upper_8days"),
            "连续9天上涨": ("consecutive_signals", "upper_9days"),
            "连续7天下跌": ("consecutive_signals", "down_7days"),
            "限价上涨6月": ("limited_lift_filter", "limited_lift_6m"),
            "限价上涨1年": ("limited_lift_filter", "limited_lift_1y"),
            "上证50成分股": ("hs_board_filter", "is_sz50"),
            "中证1000成分股": ("hs_board_filter", "is_zz1000"),
            "创业板50成分股": ("hs_board_filter", "is_cy50"),
            "已破净": ("hs_board_filter", "is_bps_break"),
            "已破板": ("hs_board_filter", "is_issue_break"),
            "机构参与": ("tiger_participant", "inst_participated"),
            "营业部参与": ("tiger_participant", "dept_participated"),
            "突破30日线": ("ma_30_break", "breakup_ma_30days"),
            "KDJ金叉Z": ("kdj_signals", "kdj_golden_forkz"),
            "KDJ金叉Y": ("kdj_signals", "kdj_golden_forky"),
            "MACD金叉Z": ("kdj_signals", "macd_golden_forkz"),
            "MACD金叉Y": ("kdj_signals", "macd_golden_forky"),
            "乌云盖顶": ("pattern_signals", "power_fulgun"), "孕线": ("pattern_signals", "pregnant"),
            "黑云压顶": ("pattern_signals", "black_cloud_tops"),
            "窄幅整理": ("pattern_signals", "narrow_finish"),
            "反转锤子": ("pattern_signals", "reversing_hammer"),
            "第一天黎明": ("pattern_signals", "first_dawn"),
            "看跌吞没": ("pattern_signals", "bearish_engulfing"),
            "上攻放量": ("pattern_signals", "upside_volume"),
            "天道法则": ("pattern_signals", "heaven_rule"),
        }
        if part in text_map:
            lst_key, val = text_map[part]
            filters[lst_key].append(val); continue

        m = re.match(r"属于行业\((.+)\)", part)
        if m: filters["industry"] = [x.strip() for x in m.group(1).split(",")]; continue
        m = re.match(r"涉及概念\((.+)\)", part)
        if m: filters["concept"] = [x.strip() for x in m.group(1).split(",")]; continue

        m = re.match(r"(\d+)天战胜大盘", part)
        if m: filters["win_market_filter"].append(f"win_market_{m.group(1)}days"); continue

        for regex, key in [
            (r"定向增发(\d+[月年])", "directional_seo"), (r"股权质押(\d+[月年])", "equity_pledge"),
        ]:
            m = re.match(regex, part)
            if m:
                unit_map = {"1月": "1m", "3月": "3m", "6月": "6m", "1年": "1y"}
                if m.group(1) in unit_map:
                    filters[f"{key}_filter"].append(f"{key}_{unit_map[m.group(1)]}")
                break
        if m: continue

        # 数值条件
        num_conditions = [
            (r"每股收益≥(.+)", "basic_eps_min"), (r"每股净资产≥(.+)", "bvps_min"),
            (r"每股自由现金流≥(.+)", "per_fcfe_min"), (r"归母净利润≥(.+)", "parent_netprofit_min"),
            (r"扣非净利润≥(.+)", "deduct_netprofit_min"), (r"营业收入≥(.+)", "total_operate_income_min"),
            (r"商誉≤(.+)", "goodwill_max"), (r"净流入≥(.+)", "net_inflow_min"),
            (r"大单动向≥(.+)", "ddx_min"), (r"3日净流入≥(.+)", "netinflow_min_3d"),
            (r"5日净流入≥(.+)", "netinflow_min_5d"),
            (r"流动比率≥(.+)", "current_ratio_min"), (r"速动比率≥(.+)", "speed_ratio_min"),
        ]
        matched = False
        for regex, key in num_conditions:
            m = re.match(regex, part)
            if m: filters[key] = _parse_number(m.group(1)); matched = True; break
        if matched: continue

        pct_conditions = [
            (r"总资产报酬率≥(.+?)%", "jroa_min"), (r"投资回报率≥(.+?)%", "roic_min"),
            (r"销售净利率≥(.+?)%", "sale_npr_min_filter"),
            (r"派息率≥(.+?)%", "par_dividend_min"), (r"质押比例≤(.+?)%", "pledge_ratio_max"),
            (r"3月持股变动≥(.+?)%", "holder_change_3m_min"),
            (r"3月高管持股变动≥(.+?)%", "executive_change_3m_min"),
            (r"机构持股比例≥(.+?)%", "allcorp_ratio_min"),
            (r"基金持股≥(.+?)%", "allcorp_fund_ratio_min"),
            (r"券商持股≥(.+?)%", "allcorp_qs_ratio_min"),
            (r"QFII持股≥(.+?)%", "allcorp_qfii_ratio_min"),
            (r"3日涨幅≥(.+?)%", "changerate_3d_min"),
            (r"5日涨幅≥(.+?)%", "changerate_5d_min"),
            (r"10日涨幅≥(.+?)%", "changerate_10d_min"),
            (r"换手率≥(.+?)%", "mi_turnover_rate_min"),
        ]
        matched = False
        for regex, key in pct_conditions:
            m = re.match(regex, part)
            if m: filters[key] = float(m.group(1)); matched = True; break
        if matched: continue

        m = re.match(r"机构评级=(.+)", part)
        if m: filters["org_rating_filter"] = m.group(1); continue
        m = re.match(r"资产负债率≤(.+?)%", part)
        if m: filters["debt_asset_ratio_max"] = float(m.group(1)); continue
        m = re.match(r"资产负债率(.+?)~(.+?)%", part)
        if m: filters["debt_asset_ratio_min"] = _parse_number(m.group(1)); filters["debt_asset_ratio_max"] = _parse_number(m.group(2)); continue

        # 量比/成交量/成交额
        m = re.match(r"量比≥(.+)", part)
        if m: filters["mi_volume_ratio_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"成交量≥(.+?)手", part)
        if m: filters["mi_volume_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"成交额≥(.+)", part)
        if m: filters["mi_amount_min"] = _parse_number(m.group(1)); continue

        # 范围格式
        range_map = [
            (r"总市值(.+)", "total_market_cap"), (r"流通市值(.+)", "free_cap"),
            (r"市销率(.+)", "ps"), (r"市现率(.+)", "pcf"), (r"动态PE(.+)", "dtsyl"),
            (r"总股本(.+)", "total_shares"), (r"流通股本(.+)", "free_shares"),
            (r"股东数(.+)", "holder_newest"),
        ]
        matched = False
        for regex, prefix in range_map:
            m = re.match(regex, part)
            if m:
                lo, hi = _parse_range(m.group(1))
                filters[f"{prefix}_min"] = lo; filters[f"{prefix}_max"] = hi
                matched = True; break
        if matched: continue

        m = re.match(r"年度涨幅(.+?)%", part)
        if m:
            p = m.group(1).split("~")
            filters["changerate_ty_min"] = _parse_number(p[0])
            filters["changerate_ty_max"] = _parse_number(p[1]) if len(p) > 1 else None
            continue

    return filters


def _build_keyword_from_filters(filters: Dict[str, Any]) -> str:
    """将结构化筛选条件转换为自然语言关键词字符串。"""
    from app.agent.tools.screener_tools import build_keyword_from_filters
    return build_keyword_from_filters(filters)


# ══════════════════════════════════════════════════════════════
#  路由
# ══════════════════════════════════════════════════════════════

@stock_screener_bp.route("/search", methods=["POST"])
def search_stocks():
    """智能选股搜索。支持 keyword 模式和 filters 模式。"""
    from app.agent.tools.screener_tools import search_stocks as _search_stocks

    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    market = (data.get("market") or "全部").strip()
    filters = data.get("filters")
    page_size = data.get("page_size", 50)

    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        return jsonify({"code": 1, "msg": "page_size 必须是整数"}), 400

    if not keyword and not filters:
        return jsonify({"code": 1, "msg": "keyword 或 filters 至少传一个"}), 400

    try:
        result = _search_stocks(query=keyword or "", source="eastmoney", filters=filters, market=market, top_n=page_size)
        if "error" in result:
            return jsonify({"code": 1, "msg": result["error"], "data": result}), 500
        return jsonify({"code": 0, "data": result})
    except Exception as e:
        logger.error("stock_screener search failed: %s", e, exc_info=True)
        return jsonify({"code": 1, "msg": str(e)}), 500


@stock_screener_bp.route("/presets", methods=["GET"])
def get_presets():
    """获取选股器支持的所有筛选条件分类和示例。"""
    from app.agent.tools.screener_tools import get_screener_presets
    try:
        return jsonify({"code": 0, "data": get_screener_presets()})
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)}), 500


@stock_screener_bp.route("/filters", methods=["GET"])
def get_filters():
    """获取筛选条件的完整结构（130+ 字段的默认值）。"""
    return jsonify({"code": 0, "data": _get_default_filters()})


@stock_screener_bp.route("/parse", methods=["POST"])
def parse_text():
    """将自然语言选股文本解析为结构化筛选条件。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"code": 1, "msg": "text 不能为空"}), 400
    try:
        return jsonify({"code": 0, "data": _parse_filters_from_text(text)})
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)}), 500


@stock_screener_bp.route("/build", methods=["POST"])
def build_text():
    """将结构化筛选条件转换为自然语言关键词。"""
    data = request.get_json(silent=True) or {}
    filters = data.get("filters")
    if not filters:
        return jsonify({"code": 1, "msg": "filters 不能为空"}), 400
    try:
        return jsonify({"code": 0, "data": {"keyword": _build_keyword_from_filters(filters)}})
    except Exception as e:
        return jsonify({"code": 1, "msg": str(e)}), 500


@stock_screener_bp.route("/batch", methods=["POST"])
def batch_screen():
    """批量筛选：一次请求多个条件。"""
    from app.agent.tools.screener_tools import search_stocks as _search_stocks

    data = request.get_json(silent=True) or {}
    queries = data.get("queries") or []
    if not queries:
        return jsonify({"code": 1, "msg": "queries 不能为空"}), 400
    if len(queries) > 10:
        return jsonify({"code": 1, "msg": "单次最多10个筛选条件"}), 400

    results = []
    for q in queries:
        keyword = (q.get("keyword") or "").strip()
        market = (q.get("market") or "全部").strip()
        filters = q.get("filters")
        if not keyword and not filters:
            results.append({"keyword": "", "error": "条件为空", "stocks": []})
            continue
        try:
            result = _search_stocks(query=keyword, source="eastmoney", filters=filters, market=market, top_n=50)
            results.append(result)
        except Exception as e:
            results.append({"keyword": keyword, "error": str(e), "stocks": []})

    return jsonify({"code": 0, "data": {"results": results, "count": len(results)}})
