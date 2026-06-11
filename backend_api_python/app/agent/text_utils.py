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
    "研究", "解读", "写", "生成", "选择", "新建",
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

def strip_stopwords_prefix(message: str) -> Optional[str]:
    """从消息中提取候选股票名（去掉停用词前缀）。

    流程：
      1. 提取第一个连续中文片段（2-6字）
      2. 如果整个片段是停用词 → 返回 None
      3. 如果片段以停用词开头，去掉前缀（"分析宇通客车" → "宇通客车"）
      4. 剩余部分 >= 2 字且不在停用词中 → 返回

    Returns:
        候选股票名，或 None
    """
    match = re.search(r'[\u4e00-\u9fff]{2,6}', message)
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
      2. 中文名 → DB 查询验证

    Returns:
        (stock_code, stock_name)，未找到返回 (None, None)
    """
    # 1. 数字代码
    code = extract_stock_code(message)
    if code:
        return code, None

    # 2. 中文名 DB 查询
    candidate = strip_stopwords_prefix(message)
    if not candidate:
        return None, None

    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        matches = get_stock_basic_db().search_stocks(candidate, limit=1)
        if matches:
            return matches[0].get("symbol", ""), matches[0].get("name", candidate)
    except Exception:
        pass

    return None, None


def has_stock_context(message: str) -> bool:
    """判断消息是否包含股票相关内容。

    只检查高置信信号：6 位代码、DB 名称命中。
    不维护关键词列表 — 交给语义路由。
    """
    if extract_stock_code(message):
        return True

    candidate = strip_stopwords_prefix(message)
    if not candidate:
        return False

    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        matches = get_stock_basic_db().search_stocks(candidate, limit=1)
        return bool(matches)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Ollama URL 检测
# ═══════════════════════════════════════════════════════════════

_OLLAMA_MARKERS = ("localhost:11434", "127.0.0.1:11434", "ollama")


def is_ollama_url(url: str) -> bool:
    """判断 URL 是否指向 Ollama 服务。"""
    url_lower = url.lower()
    return any(k in url_lower for k in _OLLAMA_MARKERS)
