# -*- coding: utf-8 -*-
"""
Text Utils — 文本处理公共函数。

消除 agent/ 目录下 6 处重复的停用词集合和股票名提取逻辑。
所有需要从中文消息中提取股票信息的模块统一引用此处。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple


# ═══════════════════════════════════════════════════════════════
# 停用词（唯一定义）
# ═══════════════════════════════════════════════════════════════

STOPWORDS = {
    # 动作
    "帮我", "分析", "查看", "看看", "查询", "修改", "修复", "创建",
    "筛选", "回测", "启动", "停止", "显示", "展示", "评估", "判断",
    "研究", "解读", "写", "生成", "选择", "新建", "看看",
    # 疑问
    "怎么样", "什么", "如何", "是什么", "什么意思", "怎么理解",
    # 时间
    "一下", "最近", "今天", "昨天", "明天", "后天",
    # 语气/虚词
    "的", "了", "吗", "吧", "呢", "啊", "哦", "嗯",
    # 交易动作
    "买入", "卖出", "持有", "跳过", "短线", "中线", "长线",
    # 状态
    "情况", "状态", "走势", "趋势", "行情",
    # 其他常见
    "大盘", "市场", "天气", "时间", "日期", "你好", "谢谢", "再见",
}


# ═══════════════════════════════════════════════════════════════
# 股票名提取
# ═══════════════════════════════════════════════════════════════

# 后缀停用词（股票名后面的修饰词，需要剥离）
_SUFFIX_STOPWORDS = {
    "股票", "行情", "走势", "趋势", "怎么样", "什么", "如何",
    "是什么", "什么意思", "怎么理解", "可以买吗", "能买吗",
    "可以买入吗", "还能持有吗", "要不要卖", "目标价",
}


def strip_stopwords_prefix(message: str) -> Optional[str]:
    """从消息中提取候选股票名（去掉停用词前后缀）。

    流程：
      1. 提取第一个连续中文片段（2-8字，覆盖4字股票名+后缀）
      2. 如果整个片段是停用词 → 返回 None
      3. 去掉停用词前缀（"分析宇通客车" → "宇通客车"）
      4. 去掉停用词后缀（"多氟多股票" → "多氟多"）
      5. 剩余部分 >= 2 字且不在停用词中 → 返回

    Returns:
        候选股票名，或 None
    """
    match = re.search(r'[\u4e00-\u9fff]{2,8}', message)
    if not match:
        return None

    candidate = match.group(0)

    # 整个片段是停用词
    if candidate in STOPWORDS:
        return None

    # 去掉停用词前缀（最长匹配优先）
    for sw in sorted(STOPWORDS, key=len, reverse=True):
        if candidate.startswith(sw) and len(candidate) > len(sw):
            candidate = candidate[len(sw):]
            break

    # 去掉停用词后缀（最长匹配优先）
    for sw in sorted(_SUFFIX_STOPWORDS, key=len, reverse=True):
        if candidate.endswith(sw) and len(candidate) > len(sw):
            candidate = candidate[:-len(sw)]
            break

    if candidate in STOPWORDS or len(candidate) < 2:
        return None

    return candidate


def extract_stock_code(message: str) -> Optional[str]:
    """从消息中提取 6 位股票代码。"""
    match = re.search(r'\b(\d{6})\b', message)
    return match.group(1) if match else None


def extract_stock_from_message(message: str) -> Tuple[Optional[str], Optional[str]]:
    """从消息中提取股票代码和名称。

    优先级：
      1. 6 位数字代码（100% 确定）
      2. 加载全部股票名 → 内存中按名称长度倒序匹配（最长匹配优先）

    Returns:
        (stock_code, stock_name)，未找到返回 (None, None)
    """
    # 1. 数字代码
    code = extract_stock_code(message)
    if code:
        return code, None

    # 2. 加载全部股票名，按长度倒序在消息中匹配
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        all_stocks = db.get_all_stocks(status="active")
        # 按名称长度倒序，保证最长匹配优先（"贵州茅台" 优先于 "茅台"）
        all_stocks.sort(key=lambda s: len(s.get("name", "")), reverse=True)
        for s in all_stocks:
            name = s.get("name", "")
            if len(name) >= 2 and name in message:
                return s.get("symbol", ""), name
    except Exception:
        pass

    return None, None



# ═══════════════════════════════════════════════════════════════
# Ollama URL 检测
# ═══════════════════════════════════════════════════════════════

_OLLAMA_MARKERS = ("localhost:11434", "127.0.0.1:11434", "ollama")


def is_ollama_url(url: str) -> bool:
    """判断 URL 是否指向 Ollama 服务。"""
    url_lower = url.lower()
    return any(k in url_lower for k in _OLLAMA_MARKERS)
