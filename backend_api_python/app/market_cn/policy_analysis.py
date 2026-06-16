#!/usr/bin/env python3
"""
最新政策解读抓取与分析
数据源: AKShare 财经新闻 + 东方财富政策频道
依赖: pip install akshare requests beautifulsoup4
"""

import pandas as pd
from datetime import datetime, timedelta
import json
import re
import logging

logger = logging.getLogger(__name__)


def _get_akshare():
    """懒加载 akshare，缺失时返回 None"""
    try:
        import akshare as ak
        return ak
    except ImportError:
        return None


def get_financial_news():
    """获取财经要闻"""
    ak = _get_akshare()
    if ak is None:
        print("  ⚠️ akshare 未安装")
        return None
    print("\n📰 财经要闻 (东方财富)")
    print("=" * 60)
    try:
        df = ak.stock_news_em(symbol="财经")
        for i, row in df.head(15).iterrows():
            title = row.get('新闻标题', row.iloc[1] if len(row) > 1 else '')
            time_str = row.get('发布时间', row.iloc[3] if len(row) > 3 else '')
            print(f"  • [{time_str}] {title}")
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_macro_news():
    """获取宏观要闻 (兼容新旧 AKShare 版本)"""
    ak = _get_akshare()
    if ak is None:
        print("  ⚠️ akshare 未安装")
        return None
    print("\n📰 宏观经济新闻")
    print("=" * 60)
    # 新版 AKShare 已移除 macro_news, 用 news_cctv 替代
    try:
        from datetime import datetime as _dt
        df = ak.news_cctv(date=_dt.now().strftime("%Y%m%d"))
        for i, row in df.head(15).iterrows():
            title = row.get('title', row.iloc[1] if len(row) > 1 else '')
            time_str = str(row.get('date', row.iloc[0] if len(row) > 0 else ''))
            print(f"  • [{time_str}] {title}")
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def _fuzzy_match(keyword: str, text: str, threshold: float = 0.6) -> bool:
    """模糊匹配: 精确包含 + 首字母缩写 + 子串 + 编辑距离"""
    if not keyword or not text:
        return False
    # 1. 精确包含
    if keyword in text:
        return True
    # 2. 短关键词只做包含匹配
    if len(keyword) <= 2:
        return False
    # 3. 首字母缩写 (如 "AI" 匹配 "人工智能")
    if len(keyword) <= 4 and keyword.isascii():
        # 提取中文首字
        cn_chars = re.findall(r'[一-鿿]', text)
        if cn_chars and keyword.lower() in ''.join(c[0] for c in cn_chars).lower():
            return True
    # 4. 子串模糊: 关键词的连续子串出现在文本中
    for i in range(len(keyword) - 1):
        sub = keyword[i:i+2]
        if sub in text:
            # 找到公共子串后, 检查剩余部分是否也有命中
            remaining = keyword[:i] + keyword[i+2:]
            if any(c in text for c in remaining):
                return True
    # 5. 编辑距离 (仅较长关键词)
    if len(keyword) >= 4:
        from difflib import SequenceMatcher
        # 滑动窗口匹配
        klen = len(keyword)
        for i in range(len(text) - klen + 2):
            window = text[max(0, i):i + klen + 1]
            ratio = SequenceMatcher(None, keyword, window).ratio()
            if ratio >= threshold:
                return True
    return False


def get_policy_keywords():
    """政策关键词扫描 — 从新闻标题中筛出政策相关"""
    print("\n🔍 政策关键词扫描")
    print("=" * 60)

    # 关键词 + 同义词/近义词组
    policy_words = [
        '央行', '降准', '降息', 'LPR', 'MLF', '逆回购', '量化宽松',
        '国务院', '国常会', '发改委', '财政部', '证监会', '银保监',
        '政策', '监管', '改革', '调控', '扶持', '补贴', '减税',
        '产业政策', '财政', '货币', '金融', '房地产', '楼市',
        '新基建', '新能源', '芯片', '半导体', 'AI', '人工智能', '大模型',
        '碳中和', '碳达峰', '共同富裕', '一带一路', 'RCEP',
        '存款准备金', '公开市场', '国债', '专项债',
    ]

    ak = _get_akshare()
    all_titles = []

    # 从多个来源收集标题
    sources = []
    if ak is not None:
        sources = [
            ('东方财富', lambda: ak.stock_news_em(symbol="财经")),
            ('央视新闻', lambda: ak.news_cctv(date=datetime.now().strftime("%Y%m%d"))),
        ]

    for source_name, fetcher in sources:
        try:
            df = fetcher()
            if df is not None:
                for _, row in df.iterrows():
                    # 兼容新旧列结构: stock_news_em 列0=关键词/列1=标题, news_cctv 列0=date/列1=title
                    if '新闻标题' in df.columns:
                        title = str(row.get('新闻标题', row.iloc[1] if len(row) > 1 else ''))
                        time_str = str(row.get('发布时间', row.iloc[3] if len(row) > 3 else ''))
                    elif 'title' in df.columns:
                        title = str(row.get('title', row.iloc[1] if len(row) > 1 else ''))
                        time_str = str(row.get('date', row.iloc[0] if len(row) > 0 else ''))
                    else:
                        title = str(row.iloc[1]) if len(row) > 1 else str(row.iloc[0])
                        time_str = str(row.iloc[0]) if len(row) > 1 else ''
                    if title and title not in ('财经', 'None', ''):
                        all_titles.append({
                            'source': source_name,
                            'title': title,
                            'time': time_str,
                            'matched_keywords': []
                        })
        except Exception as e:
            logger.warning(f"[政策关键词] {source_name} 异常: {e}")
            continue

    # 模糊关键词匹配
    policy_related = []
    for item in all_titles:
        for kw in policy_words:
            if _fuzzy_match(kw, item['title']):
                item['matched_keywords'].append(kw)
        if item['matched_keywords']:
            policy_related.append(item)

    # 按匹配数量排序
    policy_related.sort(key=lambda x: len(x['matched_keywords']), reverse=True)

    print(f"  扫描 {len(all_titles)} 条新闻, 筛出 {len(policy_related)} 条政策相关\n")
    for item in policy_related[:20]:
        kws = ', '.join(item['matched_keywords'][:5])
        print(f"  📌 [{item['source']}] {item['title']}")
        print(f"     关键词: {kws}\n")

    return policy_related


def analyze_policy_impact(titles):
    """简单的政策影响预判 (基于关键词)"""
    print("\n📈 政策影响预判")
    print("=" * 60)

    bullish_kw = {
        '降准': '利好流动性',
        '降息': '利好估值',
        '补贴': '利好相关产业',
        '扶持': '利好相关产业',
        '新基建': '利好基建板块',
        '新能源': '利好新能源板块',
        'AI': '利好科技板块',
        '人工智能': '利好科技板块',
        '碳中和': '利好环保/新能源',
        'RCEP': '利好外贸',
        '共同富裕': '利好消费/民生',
    }

    bearish_kw = {
        '调控': '短期承压',
        '监管': '注意合规风险',
        '收紧': '流动性收紧',
        '加息': '估值承压',
    }

    impacts = []
    for item in titles:
        for kw, impact in bullish_kw.items():
            if _fuzzy_match(kw, item['title']):
                impacts.append({'title': item['title'], 'keyword': kw, 'impact': impact, 'direction': '📈 利好'})
        for kw, impact in bearish_kw.items():
            if _fuzzy_match(kw, item['title']):
                impacts.append({'title': item['title'], 'keyword': kw, 'impact': impact, 'direction': '📉 利空'})

    if impacts:
        for imp in impacts[:15]:
            print(f"  {imp['direction']} [{imp['keyword']}] {imp['impact']}")
            print(f"    → {imp['title']}\n")
    else:
        print("  未检测到明显的政策信号")

    return impacts


def policy_dashboard():
    """政策解读看板"""
    print(f"\n{'='*60}")
    print(f"  🇨🇳 最新政策解读看板")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # 1. 财经要闻
    news_df = get_financial_news()

    # 2. 宏观新闻
    macro_df = get_macro_news()

    # 3. 政策关键词
    policy_items = get_policy_keywords()

    # 4. 影响预判
    impacts = []
    if policy_items:
        impacts = analyze_policy_impact(policy_items)

    # 保存结果
    result = {
        'timestamp': datetime.now().isoformat(),
        'policy_items': policy_items[:30],
        'impacts': impacts if policy_items else []
    }
    with open('policy_analysis.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 政策分析已保存: policy_analysis.json")

    return result


if __name__ == "__main__":
    policy_dashboard()


# ═══ 内存缓存 + refresh（scheduler 调用）═══

_rt_financial_news = None
_rt_macro_news = None

def refresh_financial_news():
    global _rt_financial_news
    try:
        _rt_financial_news = get_financial_news()
    except Exception as e:
        logger.warning("[refresh] refresh_financial_news 失败: %s", e)

def refresh_macro_news():
    global _rt_macro_news
    try:
        _rt_macro_news = get_macro_news()
    except Exception as e:
        logger.warning("[refresh] refresh_macro_news 失败: %s", e)

