# -*- coding: utf-8 -*-
"""
Routes — QuantDinger 意图路由定义。

每个 Route 包含：
- name: "domain/intent" 格式的唯一标识
- utterances: 10~20 条覆盖不同表达方式的示例语句
- description: 给人看的描述
- metadata: 附带给 agent 的上下文（如工具提示）

添加新场景只需在这里加一个 Route，重启后自动生效。

设计原则：
- utterances 要覆盖口语化、书面化、简写、全称等不同表达
- 每个 intent 至少 8 条 utterance，热门 intent 15~20 条
- 避免 utterance 跨 intent 重叠（会导致误分类）
"""
from __future__ import annotations

from typing import List

from app.agent.router.core import Route


def build_default_routes() -> List[Route]:
    """构建 QuantDinger 默认路由表。"""
    return [
        # ── 金融: 个股分析 ──────────────────────────────────────
        Route(
            name="finance/stock_analysis",
            description="个股分析：行情、技术面、趋势判断、综合诊断",
            utterances=[
                # 通用分析
                "帮我分析一下贵州茅台",
                "看看比亚迪最近怎么样",
                "600519技术面如何",
                "分析一下宁德时代",
                "茅台能买吗",
                "招商银行现在什么位置",
                "000001走势分析",
                "看看中芯国际的行情",
                "比亚迪还能持有吗",
                "茅台最近涨了吗",
                "分析一下平安银行",
                "帮我看看这只股票",
                "最近行情怎么样",
                "帮我诊断一下持仓",
                "看看这个票什么情况",
                "帮我看看走势",
                "分析一下大盘",
                "今天A股怎么样",
                "沪指怎么看",
                "创业板走势分析",
            ],
            score_threshold=0.40,
            metadata={
                "tools_hint": "get_realtime_quote, get_indicator_snapshot, search_stock_news",
            },
        ),

        # ── 金融: 市场扫描 ──────────────────────────────────────
        Route(
            name="finance/market_scan",
            description="市场扫描：涨停池、跌停池、龙虎榜、热门板块、市场概览",
            utterances=[
                "今天涨停的有哪些",
                "看看龙虎榜",
                "热门板块是什么",
                "今天大盘怎么样",
                "跌停的股票有哪些",
                "涨停池",
                "看看涨幅榜",
                "市场概览",
                "今天什么板块最热",
                "连板股有哪些",
                "看看跌停板",
                "强势股有哪些",
                "今天市场情绪怎么样",
                "炸板的股票",
                "破板率多少",
                "看看涨停复盘",
            ],
            score_threshold=0.40,
            metadata={
                "tools_hint": "get_market_overview, get_zt_pool, get_dragon_tiger, get_hot_rank",
            },
        ),

        # ── 金融: 选股筛选 ──────────────────────────────────────
        Route(
            name="finance/screener",
            description="选股筛选：条件选股、指标选股、智能筛选",
            utterances=[
                "帮我选几只股票",
                "筛选涨停的股票",
                "找低估值蓝筹股",
                "有没有好的选股条件",
                "推荐几只股票",
                "帮我选几只低位放量的",
                "筛选MACD金叉的股票",
                "找市盈率低于20的",
                "帮我找几个好票",
                "选几只适合短线的",
                "有没有好的标的推荐",
                "帮我筛选一下",
                "今天有什么好股票",
                "帮我找找潜力股",
                "选几只技术形态好的",
            ],
            score_threshold=0.40,
            metadata={
                "tools_hint": "search_stocks, get_screener_presets, run_indicator_signal",
            },
        ),

        # ── 金融: 策略回测 ──────────────────────────────────────
        Route(
            name="finance/backtest",
            description="策略回测：回测验证、绩效分析、收益率/胜率/回撤",
            utterances=[
                "用双均线策略回测",
                "回测一下这个策略",
                "历史绩效怎么样",
                "跑个回测看看收益率",
                "验证一下策略的胜率和回撤",
                "帮我回测MACD策略",
                "这个策略靠谱吗",
                "回测一下看看效果",
                "用历史数据验证一下",
                "策略的夏普比率多少",
                "最大回撤多少",
                "帮我测一下策略表现",
                "回测近三年的数据",
                "看看这个策略的历史表现",
            ],
            score_threshold=0.40,
            metadata={
                "tools_hint": "run_backtest, get_backtest_history, list_strategies",
            },
        ),

        # ── 金融: 资金流向 ──────────────────────────────────────
        Route(
            name="finance/fund_flow",
            description="资金流向：主力资金、北向资金、融资融券、板块资金",
            utterances=[
                "看看主力资金流向",
                "北向资金今天怎么样",
                "茅台的资金流入情况",
                "板块资金流向",
                "融资融券数据",
                "主力在买什么",
                "资金面怎么样",
                "看看大单资金",
                "北向今天买了啥",
                "主力资金净流入",
                "看看资金动向",
                "外资今天什么情况",
            ],
            score_threshold=0.40,
            metadata={
                "tools_hint": "get_fund_flow, get_sector_fund_flow",
            },
        ),

        # ── 金融: 技术指标 ──────────────────────────────────────
        Route(
            name="finance/indicator",
            description="技术指标查询：MACD/RSI/KDJ/布林带等",
            utterances=[
                "看看MACD指标",
                "RSI现在多少",
                "KDJ什么状态",
                "布林带位置",
                "技术指标怎么样",
                "均线系统怎么看",
                "成交量分析",
                "量价关系",
                "MACD金叉了吗",
                "KDJ超买了吗",
                "看看均线排列",
                "量能怎么样",
                "换手率多少",
            ],
            score_threshold=0.40,
        ),

        # ── 金融: 交易执行 ──────────────────────────────────────
        Route(
            name="finance/trading",
            description="交易执行：启动策略、停止策略、查看持仓、交易记录",
            utterances=[
                "启动网格策略",
                "停止策略",
                "看看持仓",
                "交易记录",
                "帮我下单",
                "买入茅台",
                "卖出比亚迪",
                "查看持仓盈亏",
                "启动量化策略",
                "暂停交易",
                "看看账户余额",
                "今天交易了几笔",
                "策略运行状态",
            ],
            score_threshold=0.40,
            metadata={
                "tools_hint": "list_strategies, start_strategy, stop_strategy, get_strategy_trades",
            },
        ),

        # ── 金融: 基本面查询 ────────────────────────────────────
        Route(
            name="finance/stock_info",
            description="基本面查询：公司简介、行业分类、市值PE PB ROE",
            utterances=[
                "茅台的市值多少",
                "比亚迪是做什么的",
                "这家公司什么行业",
                "市盈率多少",
                "PE多少",
                "看看基本面",
                "公司简介",
                "行业分类",
                "ROE怎么样",
                "市净率多少",
                "营收增长多少",
                "净利润多少",
            ],
            score_threshold=0.40,
        ),

        # ── 金融: 概念解释 ────────────────────────────────────
        Route(
            name="finance/concept_explain",
            description="金融概念解释、术语答疑、投资知识问答",
            utterances=[
                "什么是MACD金叉",
                "市盈率什么意思",
                "什么是龙虎榜",
                "解释一下KDJ指标",
                "什么是涨停板",
                "融资融券是什么",
                "什么是北向资金",
                "布林带怎么用",
                "什么是量价背离",
                "均线死叉是什么意思",
                "什么是换手率",
                "怎么理解市净率",
            ],
            score_threshold=0.45,
        ),

        # ── 编程 ────────────────────────────────────────────────
        Route(
            name="coding/code_modify",
            description="代码修改：修改代码、修复bug、重构优化",
            utterances=[
                "修改这个bug",
                "重构这段代码",
                "帮我写个脚本",
                "看看这个文件有什么问题",
                "优化一下性能",
                "修复这个错误",
                "改一下这个函数",
                "代码有bug",
                "帮我改改这段逻辑",
                "重构一下这个模块",
                "这个代码怎么优化",
                "帮我修复一下",
            ],
            score_threshold=0.40,
        ),

        Route(
            name="coding/code_create",
            description="代码创建：编写新代码、创建新文件、生成脚本",
            utterances=[
                "帮我写个Python脚本",
                "创建一个新文件",
                "生成一个数据分析脚本",
                "帮我写个自动化脚本",
                "写个数据清洗代码",
                "帮我实现这个功能",
                "写一个新的策略脚本",
                "帮我写个工具函数",
                "生成一个回测脚本",
                "帮我写段代码",
            ],
            score_threshold=0.40,
        ),

        Route(
            name="coding/project_scan",
            description="项目分析：项目结构分析、文件梳理、依赖关系",
            utterances=[
                "看看项目结构",
                "这个项目有哪些文件",
                "帮我分析一下项目",
                "项目的依赖关系",
                "梳理一下代码结构",
                "看看有哪些模块",
                "项目架构怎么样",
                "帮我看看代码组织",
            ],
            score_threshold=0.40,
        ),

        # ── 闲聊 ────────────────────────────────────────────────
        Route(
            name="chat/greeting",
            description="问候、打招呼",
            utterances=[
                "你好",
                "嗨",
                "hi",
                "hello",
                "在吗",
                "早上好",
                "晚上好",
                "下午好",
                "哈喽",
                "嘿",
            ],
            score_threshold=0.40,
        ),

        Route(
            name="chat/farewell",
            description="告别",
            utterances=[
                "再见",
                "拜拜",
                "bye",
                "下次见",
                "我走了",
                "不聊了",
                "先这样吧",
                "回头聊",
            ],
            score_threshold=0.40,
        ),

        Route(
            name="chat/thanks",
            description="感谢",
            utterances=[
                "谢谢",
                "感谢",
                "多谢",
                "thanks",
                "thank you",
                "太感谢了",
                "帮大忙了",
                "辛苦了",
            ],
            score_threshold=0.40,
        ),
    ]
