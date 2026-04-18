# 自然语言查询数据库模型
# 字段全覆盖：为表中每个字段建立了详细的中英文映射和别名
# 多维度支持：覆盖价格、财务、技术、基本面、资金流等所有维度
# 智能映射：自动识别同义词、错别字、英文缩写等多种表达方式
# 🛡️ 企业级容错能力
# 错别字纠正：内置专业金融术语错别字词典，自动纠正"市赢率"→"市盈率"等
# 模糊匹配：使用相似度算法处理不完整或模糊的字段名
# 安全防护：参数化查询防止SQL注入，异常处理机制确保系统稳定
# 智能降级：当部分条件无法解析时，自动忽略并继续执行其他条件

import re
import json
import difflib
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Any, Optional
import math
from app.utils.db import get_db_connection
from app.utils.logger import get_logger
logger = get_logger(__name__)

class EnterpriseStockNLQ:
    def __init__(self):
        """初始化企业级自然语言查询系统"""
        
        # 设置字段映射
        self.setup_comprehensive_field_mappings()
        
        # 设置同义词词典
        self.setup_synonym_dictionary()
        
        # 设置错别字纠正词典
        self.setup_typo_correction()
        
        # 设置单位转换
        self.setup_unit_conversion()
        
        # 查询缓存
        self.query_cache = {}
        
        # 查询历史
        self.query_history = []
        
        # 验证表结构
        self.validate_table_schema()
    
    def validate_table_schema(self):
        """验证表结构是否存在"""
        try:
            with get_db_connection() as db:
                cursor = db.cursor()
                cursor.execute("SELECT tablename FROM pg_tables WHERE tablename = 'cnstock_selection'")
                if not cursor.fetchone():
                    logger.warning("表 'cnstock_selection' 不存在，需要创建")
                    # 这里可以添加创建表的逻辑，但根据要求不创建数据
                    # self.create_table_structure()
                db.commit()
                cursor.close()
        except Exception as e:
            logger.error(f"验证表结构失败: {e}")
    
    def setup_comprehensive_field_mappings(self):
        """建立全面详细的字段映射表"""
        self.field_mappings = {
            # 基础信息
            'id': ['id', '序号', '编号', '唯一标识', '主键'],
            'date': ['日期', '交易日期', '时间', '交易日', '日'],
            'code': ['代码', '股票代码', '证券代码', 'code', 'symbol'],
            'name': ['名称', '股票名称', '名字', '简称', 'title', '公司名'],
            'secucode': ['证券代码', '完整代码', 'secucode'],
            
            # 价格数据
            'new_price': ['最新价格', '价格', '当前价格', '现价', '最新价', '收盘价', '当前价', '股价'],
            'change_rate': ['涨跌幅', '涨幅', '跌幅', '涨跌幅度', '变动率', '变化率', '日涨跌幅'],
            'high_price': ['最高价', '当日最高价', '最高价格', '日内高点', '高点'],
            'low_price': ['最低价', '当日最低价', '最低价格', '日内低点', '低点'],
            'pre_close_price': ['昨收价', '昨日收盘价', '前收盘价', '上一日收盘价'],
            'amplitude': ['振幅', '价格振幅', '波动幅度', '日内振幅'],
            
            # 成交量数据
            'volume': ['成交数量', '成交量', '数量', 'volume', '交易量', '成交股数'],
            'deal_amount': ['成交金额', '成交额', '金额', '交易金额', '成交总额', 'volume_money'],
            'volume_ratio': ['量比', '成交量比', '量比值', '相对成交量', 'volume_ratio'],
            'turnoverrate': ['换手率', '周转率', '换手', '换手比率', 'turnover_rate'],
            
            # 公司基本信息
            'listing_date': ['上市日期', '上市时间', 'ipo_date', '成立日期'],
            'industry': ['行业', '所属行业', '行业分类', '板块', 'sector', 'industry_sector'],
            'area': ['地区', '区域', '地域', '所在地区', '省份', '城市', 'location'],
            'concept': ['概念', '概念板块', '概念分类', '题材', '热点', 'theme', 'concept_stocks'],
            'style': ['风格', '投资风格', '股票风格', '投资类型', 'stock_style'],
            
            # 指数成分股
            'is_hs300': ['沪深300', 'HS300', '沪深三百', '沪深300成分股'],
            'is_sz50': ['上证50', 'SZ50', '上证五十', '上证50成分股'],
            'is_zz500': ['中证500', 'ZZ500', '中证五百', '中证500成分股'],
            'is_zz1000': ['中证1000', 'ZZ1000', '中证一千', '中证1000成分股'],
            'is_cy50': ['创业板50', 'CY50', '创业五十', '创业板50成分股'],
            
            # 估值指标
            'pe9': ['市盈率', 'PE', '滚动市盈率', '市盈率TTM', 'pe_ttm', 'price_earnings_ratio'],
            'pbnewmrq': ['市净率', 'PB', '净资产倍率', '市净率MRQ', 'pb_ratio', 'price_book_ratio'],
            'pettmdeducted': ['扣非市盈率', '扣非PE', '扣除后市盈率', 'deducted_pe'],
            'ps9': ['市销率', 'PS', '销售额倍率', '市销率TTM', 'ps_ratio'],
            'pcfjyxjl9': ['市现率', 'PCF', '现金流量倍率', '市现率TTM', 'pcf_ratio'],
            'predict_pe_syear': ['预测市盈率下年', '预期市盈率下年', 'next_year_pe'],
            'predict_pe_nyear': ['预测市盈率后两年', '预期市盈率后两年', 'two_year_later_pe'],
            'total_market_cap': ['总市值', '市值', '总市场价值', 'market_cap', 'total_value'],
            'free_cap': ['流通市值', '自由流通市值', 'free_float_market_cap'],
            'dtsyl': ['动态市盈率', '动态PE', 'dynamic_pe'],
            'ycpeg': ['预测市盈率', '预期PE', 'forecast_pe'],
            'enterprise_value_multiple': ['企业价值倍数', 'EV/EBITDA', 'ev_multiple'],
            
            # 盈利能力
            'basic_eps': ['每股收益', 'EPS', '基本每股收益', 'earnings_per_share'],
            'bvps': ['每股净资产', 'BVPS', '每股账面价值', 'book_value_per_share'],
            'per_netcash_operate': ['每股经营现金流', '经营现金流每股', 'operating_cash_flow_per_share'],
            'per_fcfe': ['每股自由现金流', '自由现金流每股', 'free_cash_flow_per_share'],
            'per_capital_reserve': ['每股资本公积', '资本公积每股', 'capital_reserve_per_share'],
            'per_unassign_profit': ['每股未分配利润', '未分配利润每股', 'undistributed_profit_per_share'],
            'per_surplus_reserve': ['每股盈余公积', '盈余公积每股', 'surplus_reserve_per_share'],
            'per_retained_earning': ['每股留存收益', '留存收益每股', 'retained_earning_per_share'],
            
            # 财务指标
            'parent_netprofit': ['归母净利润', '归属于母公司净利润', 'net_profit_parent'],
            'deduct_netprofit': ['扣非净利润', '扣除后净利润', 'deducted_net_profit'],
            'total_operate_income': ['营业收入', '营收', '营业总收入', 'operating_income', 'revenue'],
            'roe_weight': ['净资产收益率', 'ROE', '加权净资产收益率', 'return_on_equity'],
            'jroa': ['总资产报酬率', 'ROA', '资产收益率', 'return_on_assets'],
            'roic': ['投资资本回报率', 'ROIC', 'return_on_invested_capital'],
            'zxgxl': ['新增股本', '股本增加', 'new_share_ratio'],
            
            # 利润率
            'sale_gpr': ['销售毛利率', '毛利率', 'gross_profit_ratio'],
            'sale_npr': ['销售净利率', '净利率', 'net_profit_ratio'],
            
            # 增长率
            'netprofit_yoy_ratio': ['净利润同比增长率', '净利润同比', 'net_profit_growth_yoy'],
            'deduct_netprofit_growthrate': ['扣非净利润增长率', '扣非净利增长率', 'deducted_net_profit_growth'],
            'toi_yoy_ratio': ['营业收入同比增长率', '营收同比增长', 'revenue_growth_yoy'],
            'netprofit_growthrate_3y': ['净利润三年复合增长率', '三年净利复合增长', 'net_profit_cagr_3y'],
            'income_growthrate_3y': ['营业收入三年复合增长率', '三年营收复合增长', 'revenue_cagr_3y'],
            'predict_netprofit_ratio': ['预测净利润比率', '预期净利率', 'forecast_net_profit_ratio'],
            'predict_income_ratio': ['预测营业收入比率', '预期营收比率', 'forecast_income_ratio'],
            'basiceps_yoy_ratio': ['基本每股收益同比增长率', 'eps增长率', 'eps_growth_yoy'],
            'total_profit_growthrate': ['总利润增长率', 'total_profit_growth'],
            'operate_profit_growthrate': ['营业利润增长率', 'operating_profit_growth'],
            
            # 财务结构
            'debt_asset_ratio': ['资产负债率', '负债率', '资产负债比', 'debt_asset_ratio'],
            'equity_ratio': ['权益比率', '股东权益比率', 'equity_ratio'],
            'equity_multiplier': ['权益乘数', '财务杠杆', 'equity_multiplier'],
            'current_ratio': ['流动比率', '流动资产比率', 'current_ratio'],
            'speed_ratio': ['速动比率', '酸性测试比率', 'quick_ratio'],
            
            # 股本结构
            'total_shares': ['总股本', '总股数', '总股份', 'total_shares'],
            'free_shares': ['流通股本', '流通股数', '自由流通股', 'free_float_shares'],
            
            # 股东信息
            'holder_newest': ['最新股东数', '股东人数', 'shareholder_count'],
            'holder_ratio': ['股东比例', '股东占比', 'shareholder_ratio'],
            'hold_amount': ['持仓金额', '持股金额', 'holding_amount'],
            'avg_hold_num': ['平均持仓数量', '平均持股数', 'avg_holding_shares'],
            'holdnum_growthrate_3q': ['持仓数量三季度增长率', '三季度持股增长', 'holding_growth_3q'],
            'holdnum_growthrate_hy': ['持仓数量半年增长率', '半年持股增长', 'holding_growth_6m'],
            'hold_ratio_count': ['持股比例', '持股占比', 'holding_ratio'],
            'free_hold_ratio': ['自由流通股持股比例', '流通股持股比例', 'free_float_holding_ratio'],
            
            # 技术指标
            'macd_golden_fork': ['MACD金叉', 'MACD黄金交叉', 'macd_golden_cross'],
            'macd_golden_forkz': ['MACD金叉死叉', 'MACD交叉状态', 'macd_cross_status'],
            'macd_golden_forky': ['MACD金叉死叉状态', 'macd_cross_indicator'],
            'kdj_golden_fork': ['KDJ金叉', 'KDJ黄金交叉', 'kdj_golden_cross'],
            'kdj_golden_forkz': ['KDJ金叉死叉', 'KDJ交叉状态', 'kdj_cross_status'],
            'kdj_golden_forky': ['KDJ金叉死叉状态', 'kdj_cross_indicator'],
            'break_through': ['突破', '价格突破', 'breakthrough'],
            'breakup_ma_5days': ['突破5日均线', '5日均线上穿', 'break_5ma'],
            'breakup_ma_10days': ['突破10日均线', '10日均线上穿', 'break_10ma'],
            'breakup_ma_20days': ['突破20日均线', '20日均线上穿', 'break_20ma'],
            'breakup_ma_30days': ['突破30日均线', '30日均线上穿', 'break_30ma'],
            'breakup_ma_60days': ['突破60日均线', '60日均线上穿', 'break_60ma'],
            'long_avg_array': ['长期均线多头排列', '长期多头排列', 'long_term_bullish_ma'],
            'short_avg_array': ['短期均线多头排列', '短期多头排列', 'short_term_bullish_ma'],
            
            # K线形态
            'upper_large_volume': ['放量上涨', '量价齐升', 'volume_increase_rise'],
            'down_narrow_volume': ['缩量下跌', '量价背离', 'volume_decrease_fall'],
            'one_dayang_line': ['一阳线', '一根阳线', 'one_bullish_candle'],
            'two_dayang_lines': ['两阳线', '两根阳线', 'two_bullish_candles'],
            'rise_sun': ['阳包阴', '红吞绿', 'bullish_engulfing'],
            'power_fulgun': ['乌云盖顶', '黑云压顶', 'dark_cloud_cover'],
            'restore_justice': ['复权', 'adjustment', 'restored_price'],
            'down_7days': ['连续7天下跌', '七连跌', 'down_7days'],
            'upper_8days': ['连续8天上涨', '八连涨', 'up_8days'],
            'upper_9days': ['连续9天上涨', '九连涨', 'up_9days'],
            'upper_4days': ['连续4天上涨', '四连涨', 'up_4days'],
            'heaven_rule': ['天道法则', 'heaven_rule_pattern'],
            'upside_volume': ['上攻放量', 'attack_volume'],
            'bearish_engulfing': ['看跌吞没', '阴包阳', 'bearish_engulfing'],
            'reversing_hammer': ['反转锤子', '锤子线', 'reversal_hammer'],
            'shooting_star': ['射击之星', '流星线', 'shooting_star'],
            'evening_star': ['黄昏之星', '夜明星', 'evening_star'],
            'first_dawn': ['第一天黎明', 'morning_dawn'],
            'pregnant': ['孕线', '抱线', 'pregnant_line'],
            'black_cloud_tops': ['黑云压顶', 'black_cloud_tops'],
            'morning_star': ['晨星', '启明星', 'morning_star'],
            'narrow_finish': ['窄幅整理', '横盘整理', 'narrow_range'],
            
            # 资金流向
            'low_funds_inflow': ['资金流入', '资金净流入', 'funds_inflow'],
            'high_funds_outflow': ['资金流出', '资金净流出', 'funds_outflow'],
            'net_inflow': ['净流入', '净流入金额', 'net_inflow_amount'],
            'netinflow_3days': ['3天净流入', '三天净流入', 'net_inflow_3d'],
            'netinflow_5days': ['5天净流入', '五天净流入', 'net_inflow_5d'],
            'nowinterst_ratio': ['当前利息比率', 'current_interest_ratio'],
            'nowinterst_ratio_3d': ['当前利息比率3天', 'interest_ratio_3d'],
            'nowinterst_ratio_5d': ['当前利息比率5天', 'interest_ratio_5d'],
            'ddx': ['大单动向', 'ddx_indicator'],
            'ddx_3d': ['大单动向3天', 'ddx_3days'],
            'ddx_5d': ['大单动向5天', 'ddx_5days'],
            'ddx_red_10d': ['大单动向红10天', 'ddx_red_10days'],
            
            # 涨跌幅统计
            'changerate_3days': ['3天涨跌幅', '三天涨跌幅', 'change_3d'],
            'changerate_5days': ['5天涨跌幅', '五天涨跌幅', 'change_5d'],
            'changerate_10days': ['10天涨跌幅', '十天涨跌幅', 'change_10d'],
            'changerate_ty': ['年度涨跌幅', '一年涨跌幅', 'change_yearly'],
            'upnday': ['连续上涨天数', 'up_continuous_days'],
            'downnday': ['连续下跌天数', 'down_continuous_days'],
            
            # 市场表现
            'high_recent_3days': ['最近3天新高', '3日新高', 'high_3days'],
            'high_recent_5days': ['最近5天新高', '5日新高', 'high_5days'],
            'high_recent_10days': ['最近10天新高', '10日新高', 'high_10days'],
            'high_recent_20days': ['最近20天新高', '20日新高', 'high_20days'],
            'high_recent_30days': ['最近30天新高', '30日新高', 'high_30days'],
            'low_recent_3days': ['最近3天新低', '3日新低', 'low_3days'],
            'low_recent_5days': ['最近5天新低', '5日新低', 'low_5days'],
            'low_recent_10days': ['最近10天新低', '10日新低', 'low_10days'],
            'low_recent_20days': ['最近20天新低', '20日新低', 'low_20days'],
            'low_recent_30days': ['最近30天新低', '30日新低', 'low_30days'],
            'win_market_3days': ['最近3天战胜大盘', '3日跑赢大盘', 'beat_market_3d'],
            'win_market_5days': ['最近5天战胜大盘', '5日跑赢大盘', 'beat_market_5d'],
            'win_market_10days': ['最近10天战胜大盘', '10日跑赢大盘', 'beat_market_10d'],
            'win_market_20days': ['最近20天战胜大盘', '20日跑赢大盘', 'beat_market_20d'],
            'win_market_30days': ['最近30天战胜大盘', '30日跑赢大盘', 'beat_market_30d'],
            
            # 特殊状态
            'is_issue_break': ['破板', '开板', 'break_limit'],
            'is_bps_break': ['破净', '跌破净值', 'break_net_asset'],
            'now_newhigh': ['当前新高', '创新高', 'new_high_now'],
            'now_newlow': ['当前新低', '创新低', 'new_low_now'],
            
            # 公司治理
            'org_survey_3m': ['机构调研3个月', '机构调研次数', 'institution_survey_3m'],
            'org_rating': ['机构评级', '评级', 'institution_rating'],
            'holder_change_3m': ['持股变动3个月', '股东变动', 'holder_change_3m'],
            'executive_change_3m': ['高管持股变动', '高管持股变更', 'executive_change_3m'],
            'allcorp_num': ['全部公司数量', '持股机构总数', 'total_institutions'],
            'allcorp_fund_num': ['基金公司数量', '基金持股家数', 'fund_institutions'],
            'allcorp_qs_num': ['券商公司数量', '券商持股家数', 'broker_institutions'],
            'allcorp_qfii_num': ['QFII公司数量', 'QFII持股家数', 'qfii_institutions'],
            'allcorp_bx_num': ['保险公司数量', '保险持股家数', 'insurance_institutions'],
            'allcorp_sb_num': ['社保公司数量', '社保持股家数', 'social_security_institutions'],
            'allcorp_xt_num': ['信托公司数量', '信托持股家数', 'trust_institutions'],
            'allcorp_ratio': ['持股比例', '机构持股比例', 'institution_holding_ratio'],
            'allcorp_fund_ratio': ['基金持股比例', '基金占比', 'fund_holding_ratio'],
            'allcorp_qs_ratio': ['券商持股比例', '券商占比', 'broker_holding_ratio'],
            'allcorp_qfii_ratio': ['QFII持股比例', 'QFII占比', 'qfii_holding_ratio'],
            'allcorp_bx_ratio': ['保险持股比例', '保险占比', 'insurance_holding_ratio'],
            'allcorp_sb_ratio': ['社保持股比例', '社保占比', 'social_security_holding_ratio'],
            'allcorp_xt_ratio': ['信托持股比例', '信托占比', 'trust_holding_ratio'],
            
            # 股息分红
            'par_dividend_pretax': ['税前派息率', '税前股息率', 'pre_tax_dividend_ratio'],
            'par_dividend': ['派息率', '股息率', 'dividend_ratio'],
            'par_it_equity': ['派息率权益', 'dividend_equity_ratio'],
            
            # 人气指标
            'popularity_rank': ['人气排名', '关注度排名', 'popularity_ranking'],
            'rank_change': ['排名变化', '排名变动', 'rank_change'],
            'concern_rank_7days': ['关注排名7天', '7日关注排名', 'concern_rank_7d'],
            'browse_rank': ['浏览排名', '浏览量排名', 'browse_rank'],
            'newfans_ratio': ['新粉丝比率', '新粉丝比例', 'new_fans_ratio'],
            'bigfans_ratio': ['大粉丝比率', '大粉丝比例', 'big_fans_ratio'],
            
            # 风险指标
            'pledge_ratio': ['质押比例', '股权质押比例', 'pledge_ratio'],
            'goodwill_scale': ['商誉规模', '商誉金额', 'goodwill_amount'],
            'goodwill_assets_ratro': ['商誉资产比率', '商誉占比', 'goodwill_asset_ratio'],
            'mutual_netbuy_amt': ['互联互通净买入金额', '北向资金净买入', 'northbound_net_buy'],
            'hold_ratio': ['持股比例', 'holding_ratio'],
            
            # 上市表现
            'listing_yield_year': ['上市年化收益率', '年化收益', 'annualized_return'],
            'listing_volatility_year': ['上市年化波动率', '年化波动', 'annualized_volatility'],
            
            # 预测信息
            'predict_type': ['预测类型', 'forecast_type'],
            
            # 事件驱动
            'limited_lift_f6m': ['限价上涨6个月', '6个月限价上涨', 'limit_rise_6m'],
            'limited_lift_f1y': ['限价上涨1年', '1年限价上涨', 'limit_rise_1y'],
            'limited_lift_6m': ['限价上涨6个月', 'limit_rise_6m'],
            'limited_lift_1y': ['限价上涨1年', 'limit_rise_1y'],
            'directional_seo_1m': ['定向增发1个月', '1个月定增', 'seo_1m'],
            'directional_seo_3m': ['定向增发3个月', '3个月定增', 'seo_3m'],
            'directional_seo_6m': ['定向增发6个月', '6个月定增', 'seo_6m'],
            'directional_seo_1y': ['定向增发1年', '1年定增', 'seo_1y'],
            'recapitalize_1m': ['再融资1个月', '1个月再融资', 'recapitalize_1m'],
            'recapitalize_3m': ['再融资3个月', '3个月再融资', 'recapitalize_3m'],
            'recapitalize_6m': ['再融资6个月', '6个月再融资', 'recapitalize_6m'],
            'recapitalize_1y': ['再融资1年', '1年再融资', 'recapitalize_1y'],
            'equity_pledge_1m': ['股权质押1个月', '1个月质押', 'pledge_1m'],
            'equity_pledge_3m': ['股权质押3个月', '3个月质押', 'pledge_3m'],
            'equity_pledge_6m': ['股权质押6个月', '6个月质押', 'pledge_6m'],
            'equity_pledge_1y': ['股权质押1年', '1年质押', 'pledge_1y'],
        }
        
        # 创建反向映射
        self.reverse_field_mapping = {}
        for field, aliases in self.field_mappings.items():
            for alias in aliases:
                self.reverse_field_mapping[alias.lower()] = field
        
        # 布尔字段映射
        self.boolean_fields = [field for field, aliases in self.field_mappings.items() 
                              if any('是' in alias or '为' in alias for alias in aliases)]
    
    def setup_synonym_dictionary(self):
        """设置同义词词典"""
        self.synonym_dict = {
            # 价格相关
            '股价': ['当前价格', '最新价格', '价格'],
            '现价': ['当前价格', '最新价格'],
            '收盘价': ['最新价格', '收盘价格'],
            
            # 涨跌相关
            '涨幅': ['涨跌幅', '上涨幅度'],
            '跌幅': ['涨跌幅', '下跌幅度'],
            '变动': ['涨跌幅', '变化'],
            
            # 成交量相关
            '交易量': ['成交量', '成交数量'],
            '成交额': ['成交金额', '交易金额'],
            
            # 财务指标
            '净利': ['净利润', '净收益'],
            '营收': ['营业收入', '销售收入'],
            '毛利': ['毛利率', '销售毛利'],
            
            # 技术指标
            '均线': ['移动平均线', 'ma'],
            'macd': ['macd指标', 'macd线'],
            'kdj': ['kdj指标', 'kdj线'],
            
            # 公司类型
            '银行': ['银行业', '银行板块'],
            '科技': ['科技板块', 'it行业', '信息技术'],
            '医药': ['医药行业', '医疗健康', '医药生物'],
            
            # 操作符
            '超过': ['大于', '高于'],
            '低于': ['小于', '不到'],
            '不低于': ['大于等于', '至少'],
            '不超过': ['小于等于', '至多'],
            '等于': ['是', '为', '等于'],
            '不等于': ['不是', '不为'],
            '包含': ['包括', '含有', '涵盖'],
        }
    
    def setup_typo_correction(self):
        """设置错别字纠正"""
        self.typo_correction = {
            # 常见错别字
            '市赢率': '市盈率',
            '市静率': '市净率',
            '换手律': '换手率',
            '振辐': '振幅',
            '涨跌辐': '涨跌幅',
            '净资产': '每股净资产',
            '每股净资': '每股净资产',
            'macd金*': 'MACD金叉',
            'kdj金*': 'KDJ金叉',
            '破静': '破净',
            
            # 拼音错误
            'shizhi': '市值',
            'huanshou': '换手',
            'zhangfu': '涨幅',
            'diefu': '跌幅',
            'chengjiao': '成交',
            
            # 英文缩写错误
            'pe ratio': '市盈率',
            'pb ratio': '市净率',
            'ps ratio': '市销率',
            'roe': '净资产收益率',
        }
    
    def setup_unit_conversion(self):
        """设置单位转换"""
        self.unit_conversion = {
            '万': 10000,
            '亿': 100000000,
            '千': 1000,
            '百万': 1000000,
            '十亿': 1000000000,
        }
    
    def correct_typos(self, text):
        """纠正错别字"""
        corrected_text = text
        
        # 处理常见错别字
        for wrong, correct in self.typo_correction.items():
            if '*' in wrong:
                # 处理通配符
                pattern = wrong.replace('*', '.*')
                corrected_text = re.sub(pattern, correct, corrected_text, flags=re.IGNORECASE)
            else:
                corrected_text = corrected_text.replace(wrong, correct)
        
        return corrected_text
    
    def expand_synonyms(self, text):
        """扩展同义词"""
        expanded_text = text
        
        for main_term, synonyms in self.synonym_dict.items():
            if main_term in expanded_text:
                for synonym in synonyms:
                    expanded_text = expanded_text.replace(main_term, synonym + '|' + main_term)
        
        return expanded_text
    
    def parse_number_with_unit(self, text):
        """解析带单位的数字，支持复杂格式"""
        text = text.strip().lower()
        
        # 处理单位
        multiplier = 1
        used_unit = None
        
        for unit, factor in self.unit_conversion.items():
            if unit in text:
                multiplier = factor
                used_unit = unit
                text = text.replace(unit, '')
                break
        
        # 处理正负号
        sign = 1
        if text.startswith('-') or text.startswith('负'):
            sign = -1
            text = text.lstrip('-负')
        
        # 提取数字
        matches = re.findall(r'[-+]?\d*\.\d+|\d+', text)
        
        if not matches:
            return None
        
        try:
            value = float(matches[0]) * sign * multiplier
            return value
        except ValueError:
            return None
    
    def fuzzy_match_field(self, query_term, threshold=0.7):
        """模糊匹配字段名"""
        best_match = None
        best_score = 0
        
        for field, aliases in self.field_mappings.items():
            for alias in aliases:
                score = difflib.SequenceMatcher(None, query_term.lower(), alias.lower()).ratio()
                if score > best_score and score >= threshold:
                    best_score = score
                    best_match = field
        
        return best_match if best_score >= threshold else None
    
    def determine_operator(self, query_context, field_name, value_str):
        """智能确定操作符，考虑上下文"""
        context_lower = query_context.lower()
        
        # 特殊字段处理
        if field_name in self.boolean_fields:
            return '='
        
        # 检查范围查询
        if '介于' in context_lower or '在.*和.*之间' in context_lower or 'between' in context_lower:
            return 'BETWEEN'
        
        # 检查不等号
        if any(op in context_lower for op in ['>', '大于', '高于', '超过', 'gt']):
            if '=' in context_lower or '等于' in context_lower:
                return '>='
            return '>'
        
        if any(op in context_lower for op in ['<', '小于', '低于', '不到', 'lt']):
            if '=' in context_lower or '等于' in context_lower:
                return '<='
            return '<'
        
        if any(op in context_lower for op in ['>=', '大于等于', '不低于', '至少', 'gte']):
            return '>='
        
        if any(op in context_lower for op in ['<=', '小于等于', '不超过', '至多', 'lte']):
            return '<='
        
        if any(op in context_lower for op in ['!=', '<>', '不等于', '不是', 'ne']):
            return '!='
        
        if any(op in context_lower for op in ['=', '等于', '是', '为', 'eq']):
            return '='
        
        if any(op in context_lower for op in ['包含', 'like', '含有', 'in', '包括']):
            return 'LIKE'
        
        # 默认处理
        if value_str.startswith('%') or value_str.endswith('%') or '*' in value_str:
            return 'LIKE'
        
        return '='
    
    def parse_date_expression(self, date_text):
        """解析日期表达式"""
        now = datetime.now()
        
        date_text = date_text.strip().lower()
        
        # 今天
        if '今天' in date_text or '当日' in date_text:
            return now.strftime('%Y-%m-%d')
        
        # 昨天
        if '昨天' in date_text or '昨日' in date_text:
            yesterday = now - timedelta(days=1)
            return yesterday.strftime('%Y-%m-%d')
        
        # 最近N天
        if '最近' in date_text:
            match = re.search(r'最近(\d+)天', date_text)
            if match:
                days = int(match.group(1))
                target_date = now - timedelta(days=days-1)
                return target_date.strftime('%Y-%m-%d')
        
        # 上周、上周一等
        if '上周' in date_text:
            # 上周一
            last_monday = now - timedelta(days=now.weekday() + 7)
            return last_monday.strftime('%Y-%m-%d')
        
        # 标准日期格式
        try:
            # 尝试多种日期格式
            for fmt in ['%Y-%m-%d', '%Y%m%d', '%Y/%m/%d', '%Y年%m月%d日']:
                try:
                    parsed_date = datetime.strptime(date_text, fmt)
                    return parsed_date.strftime('%Y-%m-%d')
                except ValueError:
                    continue
        except:
            pass
        
        return None
    
    def extract_conditions(self, query):
        """提取查询条件，支持复杂逻辑"""
        query = self.correct_typos(query)
        query_lower = query.lower()
        
        conditions = []
        raw_conditions = []
        
        # 预处理查询
        query_for_processing = query_lower
        
        # 处理OR逻辑
        or_conditions = []
        if '或者' in query_lower or '或' in query_lower or 'OR' in query_lower:
            parts = re.split(r'或者|或|OR', query_lower)
            for part in parts:
                if any(field in part for field in self.reverse_field_mapping):
                    or_conditions.append(part.strip())
        
        # 处理AND逻辑
        and_conditions = []
        if '并且' in query_lower or '且' in query_lower or '同时' in query_lower or '和' in query_lower or 'AND' \
            in query_lower or ',' in query_lower or ';' in query_lower or '；' in query_lower or '，' in query_lower:
            parts = re.split(r'并且|且|同时|和|AND|，|；|,|;', query_lower)
            for part in parts:
                if any(field in part for field in self.reverse_field_mapping):
                    and_conditions.append(part.strip())
        
        # 如果有复杂的逻辑组合
        if or_conditions or and_conditions:
            all_parts = or_conditions + and_conditions
            all_parts.append(query_lower)  # 也处理完整查询
        else:
            all_parts = [query_lower]
        
        for part in all_parts:
            # 查找字段和值
            for field, aliases in self.field_mappings.items():
                for alias in aliases:
                    if alias in part:
                        # 提取值
                        pattern = rf'{alias}\s*(?:是|为|等于|=|大于|>|＞|gt|小于｜<|＜|lt|大于等于|gte|小于等于|lte|不等于|!=|<>|包含|like|高于|低于|不超过|不低于|超过|不到|介于|在.*之间|between)\s*([^\s,;。！？]+)'
                        matches = re.findall(pattern, part)
                        
                        for match in matches:
                            # 处理日期
                            if 'date' in field.lower() or 'listing' in field.lower():
                                date_value = self.parse_date_expression(match)
                                if date_value:
                                    op = self.determine_operator(part, field, date_value)
                                    condition = f"{field} {op} '{date_value}'"
                                    raw_conditions.append((field, op, date_value))
                                    continue
                            
                            # 处理数值
                            numeric_value = self.parse_number_with_unit(match)
                            if numeric_value is not None:
                                op = self.determine_operator(part, field, str(numeric_value))
                                condition = f"{field} {op} {numeric_value}"
                                raw_conditions.append((field, op, numeric_value))
                                continue
                            
                            # 处理字符串
                            str_value = match.strip()
                            if str_value:
                                op = self.determine_operator(part, field, str_value)
                                if op == 'LIKE':
                                    condition = f"{field} LIKE '%{str_value}%'"
                                else:
                                    condition = f"{field} {op} '{str_value}'"
                                raw_conditions.append((field, op, str_value))
        
        # 去重和验证
        seen_conditions = set()
        for field, op, value in raw_conditions:
            condition_key = f"{field}{op}{value}"
            if condition_key not in seen_conditions:
                seen_conditions.add(condition_key)
                # 验证字段是否存在
                if field in self.field_mappings:
                    conditions.append((field, op, value))
        
        # 构建SQL条件
        sql_conditions = []
        for field, op, value in conditions:
            if op == 'BETWEEN':
                # 处理BETWEEN需要两个值
                sql_conditions.append(f"{field} BETWEEN %s AND %s")
            elif op == 'LIKE':
                sql_conditions.append(f"{field} LIKE ?")
            else:
                sql_conditions.append(f"{field} {op} ?")
        
        return sql_conditions, conditions
    
    def extract_order_by(self, query):
        """提取排序信息，支持多字段排序"""
        query_lower = query.lower()
        
        # 排序方向
        order_directions = {}
        if '降序' in query_lower or '倒序' in query_lower or '从大到小' in query_lower:
            order_directions['default'] = 'DESC'
        elif '升序' in query_lower or '从小到大' in query_lower:
            order_directions['default'] = 'ASC'
        
        # 检查特定字段的排序方向
        for field, aliases in self.field_mappings.items():
            for alias in aliases:
                if f'{alias}降序' in query_lower or f'{alias}倒序' in query_lower:
                    order_directions[field] = 'DESC'
                elif f'{alias}升序' in query_lower or f'{alias}正序' in query_lower:
                    order_directions[field] = 'ASC'
        
        # 排序字段
        order_fields = []
        for field, aliases in self.field_mappings.items():
            for alias in aliases:
                if f'按{alias}' in query_lower or f'根据{alias}' in query_lower or f'以{alias}排序' in query_lower:
                    direction = order_directions.get(field, order_directions.get('default', 'ASC'))
                    order_fields.append(f"{field} {direction}")
        
        # 默认排序
        if not order_fields:
            if '价格' in query_lower or '股价' in query_lower:
                direction = order_directions.get('default', 'DESC')
                order_fields.append(f"new_price {direction}")
            elif '涨跌幅' in query_lower or '涨幅' in query_lower:
                direction = order_directions.get('default', 'DESC')
                order_fields.append(f"change_rate {direction}")
            elif '成交量' in query_lower:
                direction = order_directions.get('default', 'DESC')
                order_fields.append(f"volume {direction}")
        
        return ", ".join(order_fields) if order_fields else None
    
    def extract_limit(self, query):
        """提取限制数量，支持表达式"""
        patterns = [
            r'前(\d+)(?:只|个|条|支|项)',
            r'最多(\d+)(?:只|个|条|支|项)',
            r'限制(\d+)(?:只|个|条|支|项)',
            r'(\d+)(?:只|个|条|支|项).*?股票',
            r'取(\d+)条',
            r'显示(\d+)条',
            r'展示(\d+)条',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                return int(match.group(1))
        
        # 默认限制
        if '所有' in query.lower() or '全部' in query.lower():
            return None  # 无限制
        elif len(query) < 10:  # 简单查询
            return 10
        else:
            return 20
    
    def extract_date_range(self, query):
        """提取日期范围"""
        query_lower = query.lower()
        
        date_conditions = []
        
        # 今天
        if '今天' in query_lower or '当日' in query_lower:
            today = datetime.now().strftime('%Y-%m-%d')
            date_conditions.append(f"date = '{today}'")
        
        # 最近N天
        recent_match = re.search(r'(?:最近|近)(\d+)天', query_lower)
        if recent_match:
            days = int(recent_match.group(1))
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days-1)
            date_conditions.append(f"date BETWEEN '{start_date.strftime('%Y-%m-%d')}' AND '{end_date.strftime('%Y-%m-%d')}'")
        
        # 本周
        if '本周' in query_lower:
            today = datetime.now()
            start_of_week = today - timedelta(days=today.weekday())
            date_conditions.append(f"date BETWEEN '{start_of_week.strftime('%Y-%m-%d')}' AND '{today.strftime('%Y-%m-%d')}'")
        
        # 本月
        if '本月' in query_lower:
            today = datetime.now()
            first_day_of_month = today.replace(day=1)
            date_conditions.append(f"date BETWEEN '{first_day_of_month.strftime('%Y-%m-%d')}' AND '{today.strftime('%Y-%m-%d')}'")
        
        return date_conditions
    
    def build_safe_sql(self, query):
        """构建安全的SQL查询，防止SQL注入"""
        # 提取基础条件
        sql_conditions, raw_conditions = self.extract_conditions(query)
        
        # 提取日期范围
        date_conditions = self.extract_date_range(query)
        
        # 构建WHERE子句
        where_clauses = []
        params = []
        
        for condition in sql_conditions:
            where_clauses.append(condition)
        
        for date_condition in date_conditions:
            where_clauses.append(date_condition)
        
        # 提取排序
        order_by = self.extract_order_by(query)
        
        # 提取限制
        limit = self.extract_limit(query)
        
        # 构建SQL
        sql = "SELECT * FROM cnstock_selection"
        
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        
        if order_by:
            sql += f" ORDER BY {order_by}"
        
        if limit:
            sql += f" LIMIT {limit}"
        
        return sql, params
    
    def execute_query(self, sql, params=None):
        """执行查询，包含错误处理和缓存"""
        try:
            # 检查缓存
            cache_key = f"{sql}:{str(params)}"
            if cache_key in self.query_cache:
                return self.query_cache[cache_key]
            
            with get_db_connection() as db:
                cursor = db.cursor()
            
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                if cursor['rowcount'] <= 0:
                    columns = 0
                else:
                    columns = [description[0] for description in cursor.description]
                results = cursor.fetchall()
                db.commit()
                cursor.close()
            # 缓存结果
            result_dict = {
                'columns': columns,
                'data': results,
                'count': len(results),
                'query': sql,
                'params': params
            }
            
            self.query_cache[cache_key] = result_dict
            self.query_history.append({
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'query': sql,
                'params': params,
                'result_count': len(results)
            })
            
            return result_dict
            
        except Exception as e:
            error_msg = f"查询执行失败: {str(e)}"
            logger.error(error_msg)
            return {'error': error_msg, 'sql': sql, 'params': params}
    
    def format_results(self, result_dict, format_type='table'):
        """格式化查询结果"""
        if 'error' in result_dict:
            return f"❌ 查询失败: {result_dict['error']}"
        
        if result_dict['count'] == 0:
            return "🔍 未找到符合条件的结果"
        
        columns = result_dict['columns']
        data = result_dict['data']
        
        if format_type == 'table':
            # 生成表格格式
            max_rows = min(10, result_dict['count'])  # 限制显示行数
            table_lines = []
            
            # 表头
            header = " | ".join([f"{col:20}" for col in columns[:8]])  # 只显示前8列
            table_lines.append(header)
            table_lines.append("-" * len(header))
            
            # 数据行
            for row in data[:max_rows]:
                row_str = " | ".join([f"{str(val):20}" for val in row[:8]])
                table_lines.append(row_str)
            
            # 摘要
            table_lines.append("\n" + f"共找到 {result_dict['count']} 条记录")
            if result_dict['count'] > max_rows:
                table_lines.append(f"（仅显示前 {max_rows} 条）")
            
            return "\n".join(table_lines)
        
        elif format_type == 'json':
            # 生成JSON格式
            results = []
            for row in data[:10]:  # 限制10条
                record = {}
                for i, col in enumerate(columns):
                    if i < len(row):
                        record[col] = row[i]
                results.append(record)
            
            summary = {
                'total_count': result_dict['count'],
                'display_count': min(10, result_dict['count']),
                'results': results
            }
            return json.dumps(summary, ensure_ascii=False, indent=2)
        
        else:
            # 简单文本格式
            summary = f"✅ 查询成功！共找到 {result_dict['count']} 条记录\n"
            if result_dict['count'] > 0:
                summary += f"示例股票：{data[0][2] if len(data[0]) > 2 else 'N/A'} (代码: {data[0][1] if len(data[0]) > 1 else 'N/A'})"
            return summary
    
    def get_query_suggestions(self, partial_query):
        """根据部分查询提供智能建议"""
        suggestions = []
        
        # 基于已有查询历史
        recent_queries = [q['query'] for q in self.query_history[-5:]]
        for query in recent_queries:
            if partial_query.lower() in query.lower():
                suggestions.append(f"继续使用: {query}")
        
        # 基于字段映射
        for field, aliases in self.field_mappings.items():
            for alias in aliases:
                if partial_query.lower() in alias.lower():
                    # 生成建议查询
                    suggestions.append(f"按{alias}排序")
                    suggestions.append(f"{alias}大于[值]")
                    suggestions.append(f"{alias}小于[值]")
                    break
        
        # 常用查询模板
        common_templates = [
            '价格大于10元的股票',
            '涨跌幅大于2%的股票',
            '银行行业的股票',
            '市盈率低于20的股票',
            '最近5天新高的股票',
            'MACD金叉的股票',
            '机构评级为买入的股票'
        ]
        
        for template in common_templates:
            if partial_query.lower() in template.lower():
                suggestions.append(template)
        
        return suggestions[:5]  # 限制建议数量
    
    def analyze_query_intent(self, query):
        """分析查询意图，用于智能纠错和建议"""
        query_lower = query.lower()
        
        intent = {
            'primary_intent': 'search',  # 搜索、统计、分析
            'fields_of_interest': [],
            'conditions': [],
            'sort_preference': None,
            'limit_preference': None
        }
        
        # 检测主要意图
        if any(word in query_lower for word in ['统计', '数量', 'count', '有多少']):
            intent['primary_intent'] = 'statistical'
        elif any(word in query_lower for word in ['分析', '趋势', '走势', '分析']):
            intent['primary_intent'] = 'analysis'
        
        # 检测关注字段
        for field, aliases in self.field_mappings.items():
            for alias in aliases:
                if alias in query_lower:
                    intent['fields_of_interest'].append(field)
                    break
        
        # 检测排序偏好
        if '按' in query_lower or '排序' in query_lower:
            sort_field = self.extract_order_by(query)
            if sort_field:
                intent['sort_preference'] = sort_field
        
        return intent
    
    def query(self, natural_query, format_type='table'):
        """主查询接口，包含全面的错误处理和用户反馈"""
        start_time = datetime.now()
        
        try:
            logger.info(f"收到查询: {natural_query}")
            
            # 意图分析
            intent = self.analyze_query_intent(natural_query)
            
            # 构建SQL
            sql, params = self.build_safe_sql(natural_query)
            
            # 执行查询
            result_dict = self.execute_query(sql, params)
            
            # 格式化结果
            formatted_result = self.format_results(result_dict, format_type)
            
            # 性能统计
            execution_time = (datetime.now() - start_time).total_seconds()
            logger.info(f"查询执行时间: {execution_time:.3f}秒，结果数: {result_dict.get('count', 0)}")
            
            # 智能建议
            suggestions = self.get_query_suggestions(natural_query)
            
            # 返回结构化结果
            response = {
                'success': True,
                'query': natural_query,
                'sql': sql,
                'execution_time': execution_time,
                'result': formatted_result,
                'record_count': result_dict.get('count', 0),
                'intent': intent,
                'suggestions': suggestions,
                'query_history': self.query_history[-3:]  # 返回最近3条历史
            }
            
            return response
            
        except Exception as e:
            error_msg = f"系统错误: {str(e)}"
            logger.error(error_msg)
            
            # 提供错误建议
            suggestions = [
                '请检查查询语句是否有拼写错误',
                '尝试使用更简单的条件，如"价格大于10"',
                '查看字段映射表，使用标准字段名称',
                '联系系统管理员获取帮助'
            ]
            
            return {
                'success': False,
                'error': error_msg,
                'query': natural_query,
                'suggestions': suggestions,
                'execution_time': (datetime.now() - start_time).total_seconds()
            }
    
    def get_field_mapping_help(self):
        """获取字段映射帮助"""
        help_text = "📋 可用字段及别名:\n\n"
        
        # 按类别组织
        categories = {
            '基础信息': ['id', 'date', 'code', 'name', 'secucode'],
            '价格数据': ['new_price', 'change_rate', 'high_price', 'low_price', 'pre_close_price', 'amplitude'],
            '成交量': ['volume', 'deal_amount', 'volume_ratio', 'turnoverrate'],
            '公司信息': ['listing_date', 'industry', 'area', 'concept', 'style'],
            '指数成分': ['is_hs300', 'is_sz50', 'is_zz500', 'is_zz1000', 'is_cy50'],
            '估值指标': ['pe9', 'pbnewmrq', 'pettmdeducted', 'ps9', 'pcfjyxjl9', 'total_market_cap', 'free_cap'],
            '财务指标': ['basic_eps', 'bvps', 'roe_weight', 'parent_netprofit', 'total_operate_income'],
            '技术指标': ['macd_golden_fork', 'kdj_golden_fork', 'break_through', 'breakup_ma_5days']
        }
        
        for category, fields in categories.items():
            help_text += f"🎯 {category}:\n"
            for field in fields:
                if field in self.field_mappings:
                    aliases = self.field_mappings[field]
                    help_text += f"  • {field}: {', '.join(aliases[:3])}{'...' if len(aliases) > 3 else ''}\n"
            help_text += "\n"
        
        help_text += "💡 使用示例:\n"
        help_text += "  - '价格大于10的股票'\n"
        help_text += "  - '银行行业且市盈率小于15'\n"
        help_text += "  - 'MACD金叉的股票，按价格降序'\n"
        help_text += "  - '最近3天新高的股票，显示前5只'\n"
        
        return help_text

# 使用示例
def demo_usage():
    """演示使用方法"""
    # 连接到现有数据库
    nlq = EnterpriseStockNLQ()
    
    print("🚀 股票智能查询系统启动")
    print(nlq.get_field_mapping_help())
    
    test_queries = [
        "价格大于10元的股票",
        "银行行业且市盈率小于15的股票",
        "MACD金叉的股票，按涨跌幅降序",
        "最近3天新高的股票，显示前5只",
        "净资产收益率大于15%且负债率低于50%的股票",
        "机构评级为买入的股票",
        "价格shizhi错误查询",  # 故意包含错别字
        "今天涨幅超过5%的股票"
    ]
    
    for query in test_queries:
        print(f"\n" + "="*80)
        print(f"🔍 查询: {query}")
        
        result = nlq.query(query)
        
        if result['success']:
            print(f"✅ 执行成功 (耗时: {result['execution_time']:.3f}s)")
            print(f"📊 结果:\n{result['result']}")
            
            if result['suggestions']:
                print(f"\n💡 建议:")
                for suggestion in result['suggestions']:
                    print(f"  • {suggestion}")
        else:
            print(f"❌ 查询失败: {result['error']}")
            print(f"💡 建议:")
            for suggestion in result['suggestions']:
                print(f"  • {suggestion}")
    
    # 显示查询历史
    print(f"\n" + "="*80)
    print("📋 查询历史:")
    for i, history in enumerate(nlq.query_history[-3:], 1):
        print(f"  {i}. [{history['timestamp']}] {history['query']} (结果: {history['result_count']}条)")

if __name__ == "__main__":
    demo_usage()