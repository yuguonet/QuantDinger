# -*- coding: utf-8 -*-
"""
东方财富选股器数据同步模块

功能：
1. 从东方财富选股器 API 批量拉取全市场股票数据（200+ 指标）
2. 存入 PostgreSQL cnstock_selection 表
3. 支持全量刷新和增量更新
4. 可作为定时任务或手动触发

数据源：https://data.eastmoney.com/dataapi/xuangu/list
参考项目：https://github.com/gitkkkk/instock
"""
import math
import time
import logging
import traceback
from datetime import datetime, date
from typing import Dict, List, Any, Optional, Tuple

import requests
import pandas as pd

logger = logging.getLogger(__name__)

# ================================================================
# EastMoney API 字段 → PostgreSQL cnstock_selection 列映射
# ================================================================
# key = API 返回的 JSON key (大写)
# value = (pg_column_name, pg_type, description)
#
# 来源：instock/core/tablestructure.py TABLE_CN_STOCK_SELECTION['columns']
# 每个 column 的 'map' 值即为 EastMoney API 的 sty 参数字段名
# ================================================================

FIELD_MAP: Dict[str, Tuple[str, str, str]] = {
    # ── 基础 ──
    "SECUCODE":                        ("secucode",                        "VARCHAR(10)",  "证券代码"),
    "SECURITY_CODE":                   ("code",                            "VARCHAR(10)",  "股票代码"),
    "SECURITY_NAME_ABBR":              ("name",                            "VARCHAR(20)",  "股票名称"),
    "NEW_PRICE":                       ("new_price",                       "FLOAT",        "最新价格"),
    "CHANGE_RATE":                     ("change_rate",                     "FLOAT",        "涨跌幅"),
    "VOLUME_RATIO":                    ("volume_ratio",                    "FLOAT",        "量比"),
    "HIGH_PRICE":                      ("high_price",                      "FLOAT",        "最高价"),
    "LOW_PRICE":                       ("low_price",                       "FLOAT",        "最低价"),
    "PRE_CLOSE_PRICE":                 ("pre_close_price",                 "FLOAT",        "昨收价"),
    "VOLUME":                          ("volume",                          "BIGINT",       "成交数量"),
    "DEAL_AMOUNT":                     ("deal_amount",                     "BIGINT",       "成交金额"),
    "TURNOVERRATE":                    ("turnoverrate",                    "FLOAT",        "换手率"),
    "LISTING_DATE":                    ("listing_date",                    "DATE",         "上市日期"),

    # ── 行业/地区/概念/风格 ──
    "INDUSTRY":                        ("industry",                        "VARCHAR(50)",  "行业"),
    "AREA":                            ("area",                            "VARCHAR(50)",  "区域"),
    "CONCEPT":                         ("concept",                         "VARCHAR(800)", "概念"),
    "STYLE":                           ("style",                           "VARCHAR(255)", "风格"),

    # ── 指数成分 ──
    "IS_HS300":                        ("is_hs300",                        "VARCHAR(2)",   "是否沪深300"),
    "IS_SZ50":                         ("is_sz50",                         "VARCHAR(2)",   "是否上证50"),
    "IS_ZZ500":                        ("is_zz500",                        "VARCHAR(2)",   "是否中证500"),
    "IS_ZZ1000":                       ("is_zz1000",                       "VARCHAR(2)",   "是否中证1000"),
    "IS_CY50":                         ("is_cy50",                         "VARCHAR(2)",   "是否创业板50"),

    # ── 估值指标 ──
    "PE9":                             ("pe9",                             "FLOAT",        "市盈率TTM"),
    "PBNEWMRQ":                        ("pbnewmrq",                        "FLOAT",        "市净率MRQ"),
    "PETTMDEDUCTED":                   ("pettmdeducted",                   "FLOAT",        "扣非市盈率TTM"),
    "PS9":                             ("ps9",                             "FLOAT",        "市销率TTM"),
    "PCFJYXJL9":                       ("pcfjyxjl9",                       "FLOAT",        "市现率TTM"),
    "PREDICT_PE_SYEAR":                ("predict_pe_syear",                "FLOAT",        "预测市盈率下一年"),
    "PREDICT_PE_NYEAR":                ("predict_pe_nyear",                "FLOAT",        "预测市盈率后两年"),
    "TOTAL_MARKET_CAP":                ("total_market_cap",                "BIGINT",       "总市值"),
    "FREE_CAP":                        ("free_cap",                        "BIGINT",       "流通市值"),
    "DTESYL":                          ("dtsyl",                           "FLOAT",        "动态市盈率"),
    "YCPEG":                           ("ycpeg",                           "FLOAT",        "预测PEG"),
    "ENTERPRISE_VALUE_MULTIPLE":       ("enterprise_value_multiple",       "FLOAT",        "企业价值倍数"),

    # ── 每股指标 ──
    "BASIC_EPS":                       ("basic_eps",                       "FLOAT",        "基本每股收益"),
    "BVPS":                            ("bvps",                            "FLOAT",        "每股净资产"),
    "PER_NETCASH_OPERATE":             ("per_netcash_operate",             "FLOAT",        "每股经营现金流"),
    "PER_FCFE":                        ("per_fcfe",                        "FLOAT",        "每股自由现金流"),
    "PER_CAPITAL_RESERVE":             ("per_capital_reserve",             "FLOAT",        "每股资本公积"),
    "PER_UNASSIGN_PROFIT":             ("per_unassign_profit",             "FLOAT",        "每股未分配利润"),
    "PER_SURPLUS_RESERVE":             ("per_surplus_reserve",             "FLOAT",        "每股盈余公积"),
    "PER_RETAINED_EARNING":            ("per_retained_earning",            "FLOAT",        "每股留存收益"),

    # ── 财务指标 ──
    "PARENT_NETPROFIT":                ("parent_netprofit",                "BIGINT",       "归母净利润"),
    "DEDUCT_NETPROFIT":                ("deduct_netprofit",                "BIGINT",       "扣非净利润"),
    "TOTAL_OPERATE_INCOME":            ("total_operate_income",            "BIGINT",       "营业收入"),
    "ROE_WEIGHT":                      ("roe_weight",                      "FLOAT",        "加权净资产收益率"),
    "JROA":                            ("jroa",                            "FLOAT",        "总资产报酬率"),
    "ROIC":                            ("roic",                            "FLOAT",        "投资资本回报率"),
    "ZXGXL":                           ("zxgxl",                           "FLOAT",        "新增股本"),

    # ── 利润率 ──
    "SALE_GPR":                        ("sale_gpr",                        "FLOAT",        "销售毛利率"),
    "SALE_NPR":                        ("sale_npr",                        "FLOAT",        "销售净利率"),

    # ── 增长率 ──
    "NETPROFIT_YOY_RATIO":             ("netprofit_yoy_ratio",             "FLOAT",        "净利润同比增长率"),
    "DEDUCT_NETPROFIT_GROWTHRATE":     ("deduct_netprofit_growthrate",     "FLOAT",        "扣非净利润增长率"),
    "TOI_YOY_RATIO":                   ("toi_yoy_ratio",                   "FLOAT",        "营收同比增长率"),
    "NETPROFIT_GROWTHRATE_3Y":         ("netprofit_growthrate_3y",         "FLOAT",        "净利润三年复合增长率"),
    "INCOME_GROWTHRATE_3Y":            ("income_growthrate_3y",            "FLOAT",        "营收三年复合增长率"),
    "PREDICT_NETPROFIT_RATIO":         ("predict_netprofit_ratio",         "FLOAT",        "预测净利润比率"),
    "PREDICT_INCOME_RATIO":            ("predict_income_ratio",            "FLOAT",        "预测营收比率"),
    "BASICEPS_YOY_RATIO":              ("basiceps_yoy_ratio",              "FLOAT",        "每股收益同比增长率"),
    "TOTAL_PROFIT_GROWTHRATE":         ("total_profit_growthrate",         "FLOAT",        "总利润增长率"),
    "OPERATE_PROFIT_GROWTHRATE":       ("operate_profit_growthrate",       "FLOAT",        "营业利润增长率"),

    # ── 财务结构 ──
    "DEBT_ASSET_RATIO":                ("debt_asset_ratio",                "FLOAT",        "资产负债率"),
    "EQUITY_RATIO":                    ("equity_ratio",                    "FLOAT",        "权益比率"),
    "EQUITY_MULTIPLIER":               ("equity_multiplier",               "FLOAT",        "权益乘数"),
    "CURRENT_RATIO":                   ("current_ratio",                   "FLOAT",        "流动比率"),
    "SPEED_RATIO":                     ("speed_ratio",                     "FLOAT",        "速动比率"),

    # ── 股本结构 ──
    "TOTAL_SHARES":                    ("total_shares",                    "BIGINT",       "总股本"),
    "FREE_SHARES":                     ("free_shares",                     "BIGINT",       "流通股本"),

    # ── 股东信息 ──
    "HOLDER_NEWEST":                   ("holder_newest",                   "BIGINT",       "最新股东数"),
    "HOLDER_RATIO":                    ("holder_ratio",                    "FLOAT",        "股东比例"),
    "HOLD_AMOUNT":                     ("hold_amount",                     "FLOAT",        "持仓金额"),
    "AVG_HOLD_NUM":                    ("avg_hold_num",                    "FLOAT",        "平均持仓数量"),
    "HOLDNUM_GROWTHRATE_3Q":           ("holdnum_growthrate_3q",           "FLOAT",        "持仓数三季度增长率"),
    "HOLDNUM_GROWTHRATE_HY":           ("holdnum_growthrate_hy",           "FLOAT",        "持仓数半年增长率"),
    "HOLD_RATIO_COUNT":                ("hold_ratio_count",                "FLOAT",        "持股比例"),
    "FREE_HOLD_RATIO":                 ("free_hold_ratio",                 "FLOAT",        "自由流通股持股比例"),

    # ── 技术指标（布尔） ──
    "MACD_GOLDEN_FORK":                ("macd_golden_fork",                "BOOLEAN",      "MACD金叉"),
    "MACD_GOLDEN_FORKZ":               ("macd_golden_forkz",               "BOOLEAN",      "MACD金叉死叉"),
    "MACD_GOLDEN_FORKY":               ("macd_golden_forky",               "BOOLEAN",      "MACD金叉死叉"),
    "KDJ_GOLDEN_FORK":                 ("kdj_golden_fork",                 "BOOLEAN",      "KDJ金叉"),
    "KDJ_GOLDEN_FORKZ":                ("kdj_golden_forkz",                "BOOLEAN",      "KDJ金叉死叉"),
    "KDJ_GOLDEN_FORKY":                ("kdj_golden_forky",                "BOOLEAN",      "KDJ金叉死叉"),
    "BREAK_THROUGH":                   ("break_through",                   "BOOLEAN",      "突破"),
    "LOW_FUNDS_INFLOW":                ("low_funds_inflow",                "BOOLEAN",      "资金流入"),
    "HIGH_FUNDS_OUTFLOW":              ("high_funds_outflow",              "BOOLEAN",      "资金流出"),
    "BREAKUP_MA_5DAYS":                ("breakup_ma_5days",                "BOOLEAN",      "突破5日均线"),
    "BREAKUP_MA_10DAYS":               ("breakup_ma_10days",               "BOOLEAN",      "突破10日均线"),
    "BREAKUP_MA_20DAYS":               ("breakup_ma_20days",               "BOOLEAN",      "突破20日均线"),
    "BREAKUP_MA_30DAYS":               ("breakup_ma_30days",               "BOOLEAN",      "突破30日均线"),
    "BREAKUP_MA_60DAYS":               ("breakup_ma_60days",               "BOOLEAN",      "突破60日均线"),
    "LONG_AVG_ARRAY":                  ("long_avg_array",                  "BOOLEAN",      "长期均线多头排列"),
    "SHORT_AVG_ARRAY":                 ("short_avg_array",                 "BOOLEAN",      "短期均线多头排列"),
    "UPPER_LARGE_VOLUME":              ("upper_large_volume",              "BOOLEAN",      "放量上涨"),
    "DOWN_NARROW_VOLUME":              ("down_narrow_volume",              "BOOLEAN",      "缩量下跌"),
    "ONE_DAYANG_LINE":                 ("one_dayang_line",                 "BOOLEAN",      "一阳线"),
    "TWO_DAYANG_LINES":                ("two_dayang_lines",                "BOOLEAN",      "两阳线"),
    "RISE_SUN":                        ("rise_sun",                        "BOOLEAN",      "阳包阴"),
    "POWER_FULGUN":                    ("power_fulgun",                    "BOOLEAN",      "乌云盖顶"),
    "RESTORE_JUSTICE":                 ("restore_justice",                 "BOOLEAN",      "复权"),
    "DOWN_7DAYS":                      ("down_7days",                      "BOOLEAN",      "连续7天下跌"),
    "UPPER_8DAYS":                     ("upper_8days",                     "BOOLEAN",      "连续8天上涨"),
    "UPPER_9DAYS":                     ("upper_9days",                     "BOOLEAN",      "连续9天上涨"),
    "UPPER_4DAYS":                     ("upper_4days",                     "BOOLEAN",      "连续4天上涨"),
    "HEAVEN_RULE":                     ("heaven_rule",                     "BOOLEAN",      "天道法则"),
    "UPSIDE_VOLUME":                   ("upside_volume",                   "BOOLEAN",      "上攻放量"),
    "BEARISH_ENGULFING":               ("bearish_engulfing",               "BOOLEAN",      "看跌吞没"),
    "REVERSING_HAMMER":                ("reversing_hammer",                "BOOLEAN",      "反转锤子"),
    "SHOOTING_STAR":                   ("shooting_star",                   "BOOLEAN",      "射击之星"),
    "EVENING_STAR":                    ("evening_star",                    "BOOLEAN",      "黄昏之星"),
    "FIRST_DAWN":                      ("first_dawn",                      "BOOLEAN",      "第一天黎明"),
    "PREGNANT":                        ("pregnant",                        "BOOLEAN",      "孕线"),
    "BLACK_CLOUD_TOPS":                ("black_cloud_tops",                "BOOLEAN",      "黑云压顶"),
    "MORNING_STAR":                    ("morning_star",                    "BOOLEAN",      "晨星"),
    "NARROW_FINISH":                   ("narrow_finish",                   "BOOLEAN",      "窄幅整理"),

    # ── 事件驱动（布尔） ──
    "LIMITED_LIFT_F6M":                ("limited_lift_f6m",                "BOOLEAN",      "限价上涨6个月"),
    "LIMITED_LIFT_F1Y":                ("limited_lift_f1y",                "BOOLEAN",      "限价上涨1年"),
    "LIMITED_LIFT_6M":                 ("limited_lift_6m",                 "BOOLEAN",      "限价上涨6个月"),
    "LIMITED_LIFT_1Y":                 ("limited_lift_1y",                 "BOOLEAN",      "限价上涨1年"),
    "DIRECTIONAL_SEO_1M":              ("directional_seo_1m",              "BOOLEAN",      "定向增发1个月"),
    "DIRECTIONAL_SEO_3M":              ("directional_seo_3m",              "BOOLEAN",      "定向增发3个月"),
    "DIRECTIONAL_SEO_6M":              ("directional_seo_6m",              "BOOLEAN",      "定向增发6个月"),
    "DIRECTIONAL_SEO_1Y":              ("directional_seo_1y",              "BOOLEAN",      "定向增发1年"),
    "RECAPITALIZE_1M":                 ("recapitalize_1m",                 "BOOLEAN",      "再融资1个月"),
    "RECAPITALIZE_3M":                 ("recapitalize_3m",                 "BOOLEAN",      "再融资3个月"),
    "RECAPITALIZE_6M":                 ("recapitalize_6m",                 "BOOLEAN",      "再融资6个月"),
    "RECAPITALIZE_1Y":                 ("recapitalize_1y",                 "BOOLEAN",      "再融资1年"),
    "EQUITY_PLEDGE_1M":                ("equity_pledge_1m",                "BOOLEAN",      "股权质押1个月"),
    "EQUITY_PLEDGE_3M":                ("equity_pledge_3m",                "BOOLEAN",      "股权质押3个月"),
    "EQUITY_PLEDGE_6M":                ("equity_pledge_6m",                "BOOLEAN",      "股权质押6个月"),
    "EQUITY_PLEDGE_1Y":                ("equity_pledge_1y",                "BOOLEAN",      "股权质押1年"),

    # ── 风险指标 ──
    "PLEDGE_RATIO":                    ("pledge_ratio",                    "FLOAT",        "质押比例"),
    "GOODWILL_SCALE":                  ("goodwill_scale",                  "BIGINT",       "商誉规模"),
    "GOODWILL_ASSETS_RATRO":           ("goodwill_assets_ratro",           "FLOAT",        "商誉资产比率"),
    "PREDICT_TYPE":                    ("predict_type",                    "VARCHAR(10)",  "预测类型"),

    # ── 分红 ──
    "PAR_DIVIDEND_PRETAX":             ("par_dividend_pretax",             "FLOAT",        "税前派息率"),
    "PAR_DIVIDEND":                    ("par_dividend",                    "FLOAT",        "派息率"),
    "PAR_IT_EQUITY":                   ("par_it_equity",                   "FLOAT",        "派息率权益"),

    # ── 股东变动 ──
    "HOLDER_CHANGE_3M":                ("holder_change_3m",                "FLOAT",        "股东变动3个月"),
    "EXECUTIVE_CHANGE_3M":             ("executive_change_3m",             "FLOAT",        "高管持股变动3个月"),

    # ── 机构 ──
    "ORG_SURVEY_3M":                   ("org_survey_3m",                   "SMALLINT",     "机构调研3个月"),
    "ORG_RATING":                      ("org_rating",                      "VARCHAR(10)",  "机构评级"),
    "ALLCORP_NUM":                     ("allcorp_num",                     "SMALLINT",     "持股机构总数"),
    "ALLCORP_FUND_NUM":                ("allcorp_fund_num",                "SMALLINT",     "基金公司数量"),
    "ALLCORP_QS_NUM":                  ("allcorp_qs_num",                  "SMALLINT",     "券商公司数量"),
    "ALLCORP_QFII_NUM":                ("allcorp_qfii_num",                "SMALLINT",     "QFII公司数量"),
    "ALLCORP_BX_NUM":                  ("allcorp_bx_num",                  "SMALLINT",     "保险公司数量"),
    "ALLCORP_SB_NUM":                  ("allcorp_sb_num",                  "SMALLINT",     "社保公司数量"),
    "ALLCORP_XT_NUM":                  ("allcorp_xt_num",                  "SMALLINT",     "信托公司数量"),
    "ALLCORP_RATIO":                   ("allcorp_ratio",                   "FLOAT",        "机构持股比例"),
    "ALLCORP_FUND_RATIO":              ("allcorp_fund_ratio",              "FLOAT",        "基金持股比例"),
    "ALLCORP_QS_RATIO":                ("allcorp_qs_ratio",                "FLOAT",        "券商持股比例"),
    "ALLCORP_QFII_RATIO":              ("allcorp_qfii_ratio",              "FLOAT",        "QFII持股比例"),
    "ALLCORP_BX_RATIO":                ("allcorp_bx_ratio",                "FLOAT",        "保险持股比例"),
    "ALLCORP_SB_RATIO":                ("allcorp_sb_ratio",                "FLOAT",        "社保持股比例"),
    "ALLCORP_XT_RATIO":                ("allcorp_xt_ratio",                "FLOAT",        "信托持股比例"),

    # ── 人气指标 ──
    "POPULARITY_RANK":                 ("popularity_rank",                 "SMALLINT",     "人气排名"),
    "RANK_CHANGE":                     ("rank_change",                     "SMALLINT",     "排名变化"),
    "UPP_DAYS":                        ("upp_days",                        "SMALLINT",     "连续上涨天数"),
    "DOWN_DAYS":                       ("down_days",                       "SMALLINT",     "连续下跌天数"),
    "NEW_HIGH":                        ("new_high",                        "SMALLINT",     "新高次数"),
    "NEW_DOWN":                        ("new_down",                        "SMALLINT",     "新低次数"),
    "NEWFANS_RATIO":                   ("newfans_ratio",                   "FLOAT",        "新粉丝比率"),
    "BIGFANS_RATIO":                   ("bigfans_ratio",                   "FLOAT",        "大粉丝比率"),
    "CONCERN_RANK_7DAYS":              ("concern_rank_7days",              "SMALLINT",     "关注排名7天"),
    "BROWSE_RANK":                     ("browse_rank",                     "SMALLINT",     "浏览排名"),
    "AMPLITUDE":                       ("amplitude",                       "FLOAT",        "振幅"),

    # ── 市场表现（布尔） ──
    "IS_ISSUE_BREAK":                  ("is_issue_break",                  "BOOLEAN",      "是否破板"),
    "IS_BPS_BREAK":                    ("is_bps_break",                    "BOOLEAN",      "是否破净"),
    "NOW_NEWHIGH":                     ("now_newhigh",                     "BOOLEAN",      "当前新高"),
    "NOW_NEWLOW":                      ("now_newlow",                      "BOOLEAN",      "当前新低"),
    "HIGH_RECENT_3DAYS":               ("high_recent_3days",               "BOOLEAN",      "最近3天新高"),
    "HIGH_RECENT_5DAYS":               ("high_recent_5days",               "BOOLEAN",      "最近5天新高"),
    "HIGH_RECENT_10DAYS":              ("high_recent_10days",              "BOOLEAN",      "最近10天新高"),
    "HIGH_RECENT_20DAYS":              ("high_recent_20days",              "BOOLEAN",      "最近20天新高"),
    "HIGH_RECENT_30DAYS":              ("high_recent_30days",              "BOOLEAN",      "最近30天新高"),
    "LOW_RECENT_3DAYS":                ("low_recent_3days",                "BOOLEAN",      "最近3天新低"),
    "LOW_RECENT_5DAYS":                ("low_recent_5days",                "BOOLEAN",      "最近5天新低"),
    "LOW_RECENT_10DAYS":               ("low_recent_10days",               "BOOLEAN",      "最近10天新低"),
    "LOW_RECENT_20DAYS":               ("low_recent_20days",               "BOOLEAN",      "最近20天新低"),
    "LOW_RECENT_30DAYS":               ("low_recent_30days",               "BOOLEAN",      "最近30天新低"),
    "WIN_MARKET_3DAYS":                ("win_market_3days",                "BOOLEAN",      "最近3天战胜大盘"),
    "WIN_MARKET_5DAYS":                ("win_market_5days",                "BOOLEAN",      "最近5天战胜大盘"),
    "WIN_MARKET_10DAYS":               ("win_market_10days",               "BOOLEAN",      "最近10天战胜大盘"),
    "WIN_MARKET_20DAYS":               ("win_market_20days",               "BOOLEAN",      "最近20天战胜大盘"),
    "WIN_MARKET_30DAYS":               ("win_market_30days",               "BOOLEAN",      "最近30天战胜大盘"),

    # ── 资金流向 ──
    "NET_INFLOW":                      ("net_inflow",                      "FLOAT",        "净流入"),
    "NETINFLOW_3DAYS":                 ("netinflow_3days",                 "BIGINT",       "3天净流入"),
    "NETINFLOW_5DAYS":                 ("netinflow_5days",                 "BIGINT",       "5天净流入"),
    "NOWINTERST_RATIO":                ("nowinterst_ratio",                "BIGINT",       "当前利息比率"),
    "NOWINTERST_RATIO_3D":             ("nowinterst_ratio_3d",             "FLOAT",        "当前利息比率3天"),
    "NOWINTERST_RATIO_5D":             ("nowinterst_ratio_5d",             "FLOAT",        "当前利息比率5天"),
    "DDX":                             ("ddx",                             "FLOAT",        "大单动向"),
    "DDX_3D":                          ("ddx_3d",                          "FLOAT",        "大单动向3天"),
    "DDX_5D":                          ("ddx_5d",                          "FLOAT",        "大单动向5天"),
    "DDX_RED_10D":                     ("ddx_red_10d",                     "SMALLINT",     "大单动向红10天"),

    # ── 涨跌幅统计 ──
    "CHANGERATE_3DAYS":                ("changerate_3days",                "FLOAT",        "3天涨跌幅"),
    "CHANGERATE_5DAYS":                ("changerate_5days",                "FLOAT",        "5天涨跌幅"),
    "CHANGERATE_10DAYS":               ("changerate_10days",               "FLOAT",        "10天涨跌幅"),
    "CHANGERATE_TY":                   ("changerate_ty",                   "FLOAT",        "年度涨跌幅"),
    "UPNDAY":                          ("upnday",                          "SMALLINT",     "连续上涨天数"),
    "DOWNNDAY":                        ("downnday",                        "SMALLINT",     "连续下跌天数"),

    # ── 上市表现 ──
    "LISTING_YIELD_YEAR":              ("listing_yield_year",              "FLOAT",        "上市年化收益率"),
    "LISTING_VOLATILITY_YEAR":         ("listing_volatility_year",         "FLOAT",        "上市年化波动率"),

    # ── 互联互通 ──
    "MUTUAL_NETBUY_AMT":               ("mutual_netbuy_amt",               "BIGINT",       "互联互通净买入"),
    "HOLD_RATIO":                      ("hold_ratio",                      "FLOAT",        "持股比例"),
}


def _build_sty_param() -> str:
    """构建 sty 参数：逗号分隔的 API 字段名"""
    # instock 原始风格：cols[k]['map'] 拼接
    return ",".join(FIELD_MAP.keys())


def _build_pg_columns_def() -> str:
    """根据 FIELD_MAP 生成 PostgreSQL CREATE TABLE 的列定义（不含 id/date）"""
    lines = []
    for api_key, (col_name, pg_type, _desc) in FIELD_MAP.items():
        lines.append(f"\t{col_name} {pg_type}")
    return ",\n".join(lines)


# ================================================================
# EastMoney API 请求配置
# ================================================================
_API_URL = "https://data.eastmoney.com/dataapi/xuangu/list"
_PAGE_SIZE = 500  # 每页条数（API 最大支持 ~500）
_MARKET_FILTER = (
    '(MARKET+in+("上交所主板","深交所主板","深交所创业板"))'
    "(NEW_PRICE>0)"
)
_TIMEOUT = 15  # 单次请求超时秒数
_MAX_RETRIES = 3
_RETRY_DELAY = 2  # 重试间隔秒数


class StockSelectionSync:
    """东方财富选股器数据同步器"""

    def __init__(self, proxy: Optional[str] = None):
        """
        Args:
            proxy: 可选的 HTTP 代理地址，如 "http://127.0.0.1:7890"
        """
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://data.eastmoney.com/xuangu/",
        })
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}

        self._sty = _build_sty_param()

    # ────────────────────────────────────────────────────────────
    # 1. 从 EastMoney API 拉取全量数据
    # ────────────────────────────────────────────────────────────

    _TOTAL_TIMEOUT = 120  # 全量拉取总超时（秒）

    def fetch_all(self, trade_date: Optional[str] = None) -> pd.DataFrame:
        """拉取全市场选股数据

        Args:
            trade_date: 指定交易日期（YYYY-MM-DD），None 表示最新

        Returns:
            DataFrame，列名为 PG 列名（小写）
        """
        all_rows: List[Dict[str, Any]] = []
        page = 1
        total_count = None
        t0 = time.time()

        while True:
            # 总超时保护
            if time.time() - t0 > self._TOTAL_TIMEOUT:
                logger.warning(f"拉取超时 ({self._TOTAL_TIMEOUT}s)，已获取 {len(all_rows)} 条")
                break

            params = {
                "sty": self._sty,
                "filter": _MARKET_FILTER,
                "p": page,
                "ps": _PAGE_SIZE,
                "source": "SELECT_SECURITIES",
                "client": "WEB",
            }
            if trade_date:
                params["filter"] += f"(DATE='{trade_date}')"

            data_json = self._request_with_retry(params)
            if data_json is None:
                break

            result = data_json.get("result")
            if result is None:
                break

            rows = result.get("data", [])
            if not rows:
                break

            all_rows.extend(rows)

            if total_count is None:
                total_count = result.get("count", 0)
                logger.info(f"EastMoney API 返回总记录数: {total_count}")

            page_count = math.ceil(total_count / _PAGE_SIZE)
            if page >= page_count:
                break
            page += 1
            time.sleep(0.3)  # 礼貌间隔，避免触发限流

        if not all_rows:
            logger.warning("未从 EastMoney 获取到任何数据")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df = self._normalize_dataframe(df)
        logger.info(f"共获取 {len(df)} 条选股数据")
        return df

    def _request_with_retry(self, params: Dict[str, Any]) -> Optional[Dict]:
        """带重试的 API 请求"""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.get(
                    _API_URL, params=params, timeout=_TIMEOUT
                )
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                logger.warning(
                    f"EastMoney API 请求失败 (尝试 {attempt}/{_MAX_RETRIES}): {e}"
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY * attempt)
        return None

    # ────────────────────────────────────────────────────────────
    # 2. 数据规范化
    # ────────────────────────────────────────────────────────────

    def _normalize_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """将 API 返回的 DataFrame 映射为 PG 列名并做类型转换"""

        # 列名重命名：API 大写 → PG 小写
        rename_map = {}
        for api_key, (pg_col, _pg_type, _desc) in FIELD_MAP.items():
            if api_key in df.columns:
                rename_map[api_key] = pg_col
        df = df.rename(columns=rename_map)

        # 处理 CONCEPT / STYLE 列表 → 逗号分隔字符串
        for col in ("concept", "style"):
            if col in df.columns:
                mask = df[col].notna()
                df.loc[mask, col] = df.loc[mask, col].apply(
                    lambda x: ", ".join(x) if isinstance(x, list) else str(x) if x else ""
                )

        # 类型转换
        for _api_key, (pg_col, pg_type, _desc) in FIELD_MAP.items():
            if pg_col not in df.columns:
                continue
            if pg_type in ("FLOAT",):
                df[pg_col] = pd.to_numeric(df[pg_col], errors="coerce")
            elif pg_type in ("BIGINT", "SMALLINT", "INTEGER"):
                df[pg_col] = pd.to_numeric(df[pg_col], errors="coerce").astype("Int64")
            elif pg_type == "BOOLEAN":
                df[pg_col] = df[pg_col].apply(
                    lambda x: bool(x) if pd.notna(x) else None
                )
            elif pg_type == "DATE":
                df[pg_col] = pd.to_datetime(
                    df[pg_col], errors="coerce"
                ).dt.strftime("%Y-%m-%d")

        # 添加 date 列
        today_str = date.today().strftime("%Y-%m-%d")
        if "date" not in df.columns:
            df["date"] = today_str

        return df

    # ────────────────────────────────────────────────────────────
    # 3. PostgreSQL 建表 & 写入
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_table():
        """确保 cnstock_selection 表存在（PostgreSQL）"""
        from app.utils.db import get_db_connection

        col_defs = _build_pg_columns_def()
        ddl = f"""
        CREATE TABLE IF NOT EXISTS cnstock_selection (
            id SERIAL PRIMARY KEY,
            date DATE DEFAULT CURRENT_DATE,
            {col_defs}
        );
        CREATE INDEX IF NOT EXISTS idx_cnss_date_code
            ON cnstock_selection (date, code);
        CREATE INDEX IF NOT EXISTS idx_cnss_code
            ON cnstock_selection (code);
        CREATE INDEX IF NOT EXISTS idx_cnss_industry
            ON cnstock_selection (industry);
        CREATE INDEX IF NOT EXISTS idx_cnss_change_rate
            ON cnstock_selection (change_rate);
        CREATE INDEX IF NOT EXISTS idx_cnss_pe9
            ON cnstock_selection (pe9);
        """

        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(ddl)
                conn.commit()
                cur.close()
            logger.info("cnstock_selection 表已就绪")
        except Exception as e:
            logger.error(f"创建 cnstock_selection 表失败: {e}")
            raise

    @staticmethod
    def upsert(df: pd.DataFrame, batch_size: int = 500) -> int:
        """将 DataFrame 批量 upsert 到 cnstock_selection

        使用 INSERT ... ON CONFLICT (date, code) DO UPDATE 策略。

        Args:
            df: 规范化后的 DataFrame
            batch_size: 每批写入条数

        Returns:
            成功写入的总条数
        """
        if df.empty:
            return 0

        from app.utils.db import get_db_connection

        # 动态构建列列表（与 FIELD_MAP 一致 + date）
        pg_columns = ["date"] + [v[0] for v in FIELD_MAP.values()]
        # 只保留在 df 中实际存在的列
        existing_cols = [c for c in pg_columns if c in df.columns]

        col_list = ", ".join(existing_cols)
        placeholders = ", ".join(["%s"] * len(existing_cols))
        # ON CONFLICT DO UPDATE：更新除 date/code 外的所有列
        update_cols = [c for c in existing_cols if c not in ("date", "code")]
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

        sql = (
            f"INSERT INTO cnstock_selection ({col_list}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (date, code) DO UPDATE SET {set_clause}"
        )

        total_written = 0
        total = len(df)

        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                for start in range(0, total, batch_size):
                    chunk = df.iloc[start : start + batch_size]
                    rows = []
                    for _, row in chunk.iterrows():
                        vals = []
                        for col in existing_cols:
                            v = row.get(col)
                            if pd.isna(v):
                                vals.append(None)
                            elif isinstance(v, (pd.Int64Dtype,)):
                                vals.append(int(v) if pd.notna(v) else None)
                            else:
                                vals.append(v)
                        rows.append(tuple(vals))

                    cur.executemany(sql, rows)
                    total_written += len(rows)

                    if (start + batch_size) % 2000 == 0 or (start + batch_size) >= total:
                        logger.debug(
                            f"upsert 进度: {min(start + batch_size, total)}/{total}"
                        )

                conn.commit()
                cur.close()

            logger.info(f"upsert 完成: {total_written} 条写入 cnstock_selection")
            return total_written

        except Exception as e:
            logger.error(f"upsert 失败: {e}", exc_info=True)
            return 0

    # ────────────────────────────────────────────────────────────
    # 4. 一键同步入口
    # ────────────────────────────────────────────────────────────

    def sync(self, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """完整的同步流程：拉取 → 规范 → 建表 → 写入

        Args:
            trade_date: 可选的指定交易日期

        Returns:
            同步结果摘要
        """
        t0 = time.time()
        result = {
            "success": False,
            "fetched": 0,
            "written": 0,
            "elapsed_seconds": 0,
            "error": None,
        }

        try:
            # 1. 确保表存在
            self.ensure_table()

            # 2. 拉取数据
            df = self.fetch_all(trade_date=trade_date)
            result["fetched"] = len(df)
            if df.empty:
                result["error"] = "未获取到数据"
                return result

            # 3. 写入 PG
            written = self.upsert(df)
            result["written"] = written
            result["success"] = written > 0

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"选股器同步失败: {e}", exc_info=True)

        result["elapsed_seconds"] = round(time.time() - t0, 2)
        return result


# ────────────────────────────────────────────────────────────
# 模块级便捷函数
# ────────────────────────────────────────────────────────────

def run_sync(proxy: Optional[str] = None, trade_date: Optional[str] = None) -> Dict[str, Any]:
    """运行一次选股器数据同步（适合定时任务或手动触发）"""
    syncer = StockSelectionSync(proxy=proxy)
    return syncer.sync(trade_date=trade_date)
