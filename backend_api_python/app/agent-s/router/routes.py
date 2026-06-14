# -*- coding: utf-8 -*-
"""
Routes — 语义路由的默认路由定义。

build_default_routes() 返回 Route 列表，供 SemanticIntentRouter 使用。
每条 Route 包含一组示例语句（utterances），用于构建向量空间进行相似度匹配。

verb/noun 元数据直接嵌入 Route，语义路由一步到位完成 domain + verb + noun 分类，
不再需要独立的 VerbNounRouter 正则匹配。
"""
from __future__ import annotations

from typing import List
from app.agent.router.core import Route


def build_default_routes() -> List[Route]:
    """构建默认路由列表。"""
    return [
        # ── 金融分析 ──
        Route(
            name="finance/stock_analysis",
            utterances=[
                "帮我分析一下贵州茅台", "600519怎么样", "分析茅台的技术面",
                "比亚迪走势如何", "这个股票能买吗", "宁德时代什么情况",
                "招商银行后市怎么看", "分析一下平安银行", "茅台还能涨吗",
                "帮我看看比亚迪的行情", "诊断一下这个股票", "600519分析",
                "剖析一下茅台", "研判比亚迪走势", "评估宁德时代",
                "贵州茅台什么情况", "宇通客车怎么样", "这个票怎么样",
                "能买吗", "还能持有吗", "可以入场吗",
            ],
            description="个股分析、技术面分析、行情研判",
            metadata={
                "domain": "finance", "intent": "stock_analysis",
                "verb": "analyze", "noun": "stock",
                "tool_categories": ["名称查询", "行情数据", "技术分析", "情报搜索"],
            },
        ),
        Route(
            name="finance/chart_view",
            utterances=[
                "看看茅台的K线", "出个K线图", "显示比亚迪的日K",
                "给我看下走势图", "蜡烛图", "画个图看看",
                "600519的K线图", "分时图", "周K线",
                "看下茅台的图表", "K线走势", "分钟K线",
                "出个图", "画个K线", "看看走势图",
            ],
            description="K线图表、走势可视化",
            metadata={
                "domain": "finance", "intent": "chart_view",
                "verb": "view", "noun": "chart",
                "tool_categories": ["名称查询", "行情数据", "K线图表"],
            },
        ),
        Route(
            name="finance/market_scan",
            utterances=[
                "今天涨停板有哪些", "看看涨停池", "龙虎榜",
                "大盘怎么样", "市场今天什么情况", "热门板块",
                "涨幅榜", "跌停板", "连板股票",
                "市场情绪怎么样", "复盘一下今天", "强势股有哪些",
                "沪指今天怎么样", "创业板走势",
                "涨停复盘", "看看大盘", "市场全景",
            ],
            description="涨停池、龙虎榜、市场概览",
            metadata={
                "domain": "finance", "intent": "market_scan",
                "verb": "view", "noun": "market",
                "tool_categories": ["行情数据", "龙虎榜/热榜"],
            },
        ),
        Route(
            name="finance/screener",
            utterances=[
                "帮我选几只股票", "有没有好的股票推荐", "筛选低估值的股票",
                "找几只潜力股", "条件选股", "帮我找找好票",
                "哪些股票值得买", "帮我筛选一下", "推荐几只股票",
                "蓝筹股有哪些", "低估值选股",
                "选几只好票", "找点潜力股", "帮我挑几只",
            ],
            description="条件选股、指标选股、智能筛选",
            metadata={
                "domain": "finance", "intent": "screener",
                "verb": "filter", "noun": "stock",
                "tool_categories": ["名称查询", "选股", "指标策略"],
            },
        ),
        Route(
            name="finance/backtest",
            utterances=[
                "回测一下这个策略", "验证一下策略", "跑个回测看看",
                "历史收益怎么样", "胜率多少", "最大回撤",
                "夏普比率", "策略回测", "测试一下策略",
                "收益率如何", "历史表现",
                "回测看看", "验证策略", "跑一下回测",
            ],
            description="策略回测验证、历史绩效分析",
            metadata={
                "domain": "finance", "intent": "backtest",
                "verb": "backtest", "noun": "stock",
                "tool_categories": ["名称查询", "行情数据", "回测", "指标策略"],
            },
        ),
        Route(
            name="finance/fund_flow",
            utterances=[
                "看看资金流向", "主力资金怎么样", "北向资金",
                "外资流入情况", "融资融券", "板块资金流向",
                "大单资金", "资金面分析", "茅台的资金流向",
                "资金动向", "主力在干什么", "资金流入流出",
            ],
            description="主力资金、北向资金、融资融券",
            metadata={
                "domain": "finance", "intent": "fund_flow",
                "verb": "view", "noun": "fund_flow",
                "tool_categories": ["名称查询", "行情数据"],
            },
        ),
        Route(
            name="finance/indicator",
            utterances=[
                "看看MACD指标", "RSI多少", "KDJ怎么样",
                "布林带分析", "均线情况", "技术指标查询",
                "成交量分析", "换手率", "金叉死叉",
                "超买超卖", "量价关系",
                "指标怎么样", "看看指标", "MACD什么情况",
            ],
            description="技术指标查询、MACD/RSI/KDJ/布林带",
            metadata={
                "domain": "finance", "intent": "indicator",
                "verb": "view", "noun": "indicator",
                "tool_categories": ["名称查询", "行情数据", "技术分析", "指标策略"],
            },
        ),
        Route(
            name="finance/trading",
            utterances=[
                "启动策略", "停止策略", "查看持仓",
                "交易记录", "买入茅台", "卖出股票",
                "下单", "账户余额", "仓位多少",
                "策略运行状态", "交易执行",
                "买入", "卖出", "建仓", "减仓",
            ],
            description="策略启停、持仓管理、交易记录",
            metadata={
                "domain": "finance", "intent": "trading",
                "verb": "execute", "noun": "trading",
                "tool_categories": ["交易", "指标策略"],
            },
        ),
        Route(
            name="finance/stock_info",
            utterances=[
                "茅台的市盈率多少", "比亚迪市值", "公司简介",
                "基本面分析", "PE PB ROE", "行业分类",
                "茅台是做什么的", "公司信息", "估值多少",
                "市值多少", "市盈率多少", "基本面怎么样",
            ],
            description="公司简介、行业分类、市值PE PB ROE",
            metadata={
                "domain": "finance", "intent": "stock_info",
                "verb": "query", "noun": "stock",
                "tool_categories": ["名称查询", "行情数据"],
            },
        ),
        Route(
            name="finance/concept_explain",
            utterances=[
                "什么是MACD", "金叉是什么意思", "怎么理解KDJ",
                "什么是市盈率", "解释一下布林带", "均线怎么用",
                "什么是涨停板", "龙虎榜是什么", "量化交易是什么意思",
                "这是什么概念", "什么意思", "怎么理解",
            ],
            description="金融概念解释、术语答疑",
            metadata={
                "domain": "finance", "intent": "concept_explain",
                "verb": "explain", "noun": "concept",
            },
        ),

        # ── 代码开发 ──
        Route(
            name="coding/code_modify",
            utterances=[
                "帮我修改这段代码", "修复这个bug", "重构一下这个函数",
                "优化性能", "代码有问题", "帮我改一下",
                "fix this bug", "有错误怎么修", "怎么优化这段代码",
                "代码报错了", "修改一下配置",
                "改一下代码", "代码需要修复", "重构这个模块",
            ],
            description="代码修改、修复bug、重构优化",
            metadata={
                "domain": "coding", "intent": "code_modify",
                "verb": "modify", "noun": "code",
                "tool_categories": ["工作区"],
            },
        ),
        Route(
            name="coding/code_create",
            utterances=[
                "帮我写个脚本", "创建一个新文件", "生成代码",
                "写个函数", "帮我实现这个功能", "新建一个模块",
                "写个工具", "帮我写个接口", "创建API",
                "写个新文件", "帮我实现", "新建一个函数",
            ],
            description="代码编写、创建新文件、生成脚本",
            metadata={
                "domain": "coding", "intent": "code_create",
                "verb": "create", "noun": "code",
                "tool_categories": ["工作区"],
            },
        ),
        Route(
            name="coding/project_scan",
            utterances=[
                "分析一下项目结构", "看看代码组织", "项目有哪些文件",
                "梳理一下依赖关系", "代码架构是什么样的", "项目分析",
                "看看目录结构", "模块关系", "代码结构",
                "项目结构", "代码怎么组织的", "有哪些模块",
            ],
            description="项目结构分析、文件梳理、依赖关系",
            metadata={
                "domain": "coding", "intent": "project_scan",
                "verb": "view", "noun": "project",
                "tool_categories": ["工作区"],
            },
        ),

        # ── 闲聊 ──
        Route(
            name="chat/general",
            utterances=[
                "你好", "hi", "hello", "嗨", "在吗",
                "再见", "拜拜", "bye", "谢谢", "感谢",
                "今天天气怎么样", "你是谁", "你能做什么",
                "聊聊", "随便说说", "help",
            ],
            description="通用对话、问候、闲聊",
            metadata={"domain": "chat", "intent": "general"},
        ),
    ]
