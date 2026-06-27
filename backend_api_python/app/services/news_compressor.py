# -*- coding: utf-8 -*-
"""
新闻摘要压缩器 — news_compressor.py

职责:
  将新闻 snippet 压缩到 50~300 字，保留金融关键信息。

设计原则:
  - 纯规则，零 LLM 调用，<1ms 执行
  - 金融关键词句优先保留
  - 数字/百分比/金额等关键数据不丢
  - 支持中英文混合文本

调用方:
  - news_search.py NewsCacheManager.save_items() — 入库前压缩
  - news_search.py SearchService._dicts_to_results() — 搜索结果即时压缩
  - news_analysis.py (已删除 LLM 代码, _extract_key_sentences 已迁移到此处)
"""
from __future__ import annotations

import re
from typing import List, Tuple

_FINANCE_KEYWORDS = re.compile(
    r'降[准息]|LPR|MLF|逆回购|货币(?:政策|宽松|收紧)|利率|准备金|财政|减税|补贴|专项债|国债|赤字|GDP|CPI|PPI|PMI|M2|社融|进出口|贸易顺差|贸易逆差|外汇储备|涨停|跌停|大涨|暴跌|闪崩|新高|新低|放量|缩量|金叉|死叉|突破|反弹|领涨|领跌|净利润|营收|利润增长|业绩增长|超预期|扭亏|翻倍|暴雷|亏损|下滑|增持|减持|回购|分红|配股|IPO|定增|并购|重组|战略合作|中标|新能源|芯片|半导体|AI|人工智能|大模型|碳中和|数字经济|低空经济|机器人|退市|破产|造假|调查|制裁|关税|监管|违规|债务违约|利好|利空|重大|紧急|突发|震惊',
    re.IGNORECASE,
)

_DATA_PATTERNS = re.compile(
    r'\d+[\.\d]*[%\uff05]|\d+[\.\d]*[\u4e07\u4ebf]|\d+[\.\d]*\u5143|\\$\d+[\.\d]*[BMK]?|(?:\u5e02\u503c|\u4f30\u503c).*?\d+|(?:\u76ee\u6807\u4ef7|\u8bc4\u7ea7).*?\d+|(?:\u540c\u6bd4|\u73af\u6bd4).*?[\u589e\u964d\u6da8\u8dcc]',
    re.IGNORECASE,
)

_SENT_SPLIT = re.compile(r'(?<=[。！？；.!?])\s*|(?<=\n)')
_MULTI_SPACE = re.compile(r'\s+')
_MULTI_PUNCT = re.compile(r'[，。、；：！？,;:!?]{2,}')


def compress_news(
    text: str,
    min_len: int = 30,
    max_len: int = 100,
    title: str = "",
) -> str:
    """
    压缩新闻 snippet 到 [min_len, max_len] 范围。

    策略:
      1. <=max_len → 原文返回 (不压缩)
      2. >max_len → 关键句提取:
         a. 切句
         b. 每句打分 (金融关键词 + 数据模式 + 位置权重)
         c. 贪心拼接到 max_len
         d. 不足 min_len 时放宽到原文截断

    Args:
        text:    原始 snippet (可能很长)
        min_len: 最小输出长度 (默认 50)
        max_len: 最大输出长度 (默认 300)
        title:   标题 (可选，用于去重)

    Returns:
        压缩后的文本，长度在 [min_len, max_len] 范围
    """
    if not text:
        return ""

    text = _clean_text(text)

    # 短文不压缩
    if len(text) <= max_len:
        return text

    # 切句
    sentences = _split_sentences(text)
    if len(sentences) <= 1:
        return _smart_truncate(text, max_len)

    # 标题归一化 (用于去重)
    title_norm = _normalize_for_dedup(title) if title else ""

    # 给每句打分
    scored: List[Tuple[float, int, str]] = []
    for i, sent in enumerate(sentences):
        if not sent.strip():
            continue
        score = _score_sentence(sent, i, len(sentences), title_norm)
        scored.append((score, i, sent))

    # 按分数降序排列
    scored.sort(key=lambda x: (-x[0], x[1]))

    # 贪心拼接
    selected_indices = set()
    total_len = 0
    for score, idx, sent in scored:
        if score <= 0:
            continue
        if total_len + len(sent) + 1 > max_len:
            remaining = max_len - total_len - 1
            if remaining > 20:
                selected_indices.add(idx)
            break
        selected_indices.add(idx)
        total_len += len(sent) + 1
        if total_len >= max_len:
            break

    # 按原文顺序输出
    result_parts = []
    for i, sent in enumerate(sentences):
        if i in selected_indices:
            result_parts.append(sent)

    result = " ".join(result_parts).strip()

    # 不足 min_len → 放宽
    if len(result) < min_len:
        result = _smart_truncate(text, max_len)

    return result


def compress_news_batch(
    items: list,
    snippet_key: str = "snippet",
    title_key: str = "title",
    max_len: int = 100,
) -> list:
    """批量压缩新闻列表 (原地修改 snippet 字段)"""
    for item in items:
        if not isinstance(item, dict):
            continue
        snippet = item.get(snippet_key, "")
        title = item.get(title_key, "")
        if snippet and len(snippet) > max_len:
            item[snippet_key] = compress_news(snippet, max_len=max_len, title=title)
    return items


def _clean_text(text: str) -> str:
    """清洗文本"""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = _MULTI_SPACE.sub(' ', text)
    text = _MULTI_PUNCT.sub(lambda m: m.group()[0], text)
    return text.strip()


def _split_sentences(text: str) -> List[str]:
    """切句"""
    parts = _SENT_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


def _score_sentence(sent: str, position: int, total: int, title_norm: str = "") -> float:
    """
    给句子打分 (越高越值得保留)

    评分维度:
      1. 金融关键词命中数 (+2.0/个)
      2. 数据模式命中 (+3.0/个)
      3. 位置权重 (首句+1.5, 末句+1.0, 中间+0.5)
      4. 长度惩罚 (太短<10字 -2.0, 太长>200字 -1.0)
      5. 标题去重 (与标题高度相似 -3.0)
    """
    score = 0.0

    kw_hits = len(_FINANCE_KEYWORDS.findall(sent))
    score += kw_hits * 2.0

    data_hits = len(_DATA_PATTERNS.findall(sent))
    score += data_hits * 3.0

    if position == 0:
        score += 1.5
    elif position == total - 1:
        score += 1.0
    else:
        score += 0.5

    sent_len = len(sent)
    if sent_len < 10:
        score -= 2.0
    elif sent_len > 200:
        score -= 1.0

    if title_norm:
        sent_norm = _normalize_for_dedup(sent)
        if sent_norm and title_norm in sent_norm:
            score -= 3.0

    return score


def _normalize_for_dedup(text: str) -> str:
    """归一化文本用于去重"""
    t = text.strip().lower()
    t = re.sub(r'[^\w\u4e00-\u9fff]', '', t)
    return t


def _smart_truncate(text: str, max_len: int) -> str:
    """智能截断：不在词/句中间断开"""
    if len(text) <= max_len:
        return text

    cut_text = text[:max_len]
    for sep in ['。', '！', '？', '；', '.', '!', '?', ';', '，', ',', '、', ' ']:
        pos = cut_text.rfind(sep)
        if pos > max_len * 0.5:
            return cut_text[:pos + 1].rstrip()
    return cut_text.rstrip() + "…"


def extract_key_sentences(text: str, max_chars: int = 100) -> str:
    """兼容 news_analysis._extract_key_sentences() 的接口"""
    return compress_news(text, min_len=30, max_len=max_chars)
