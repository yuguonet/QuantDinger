#!/usr/bin/env python3
"""
最新政策解读抓取与分析
数据源: AKShare 财经新闻 + 东方财富政策频道
依赖: pip install akshare requests beautifulsoup4
"""

import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import json
import re


def get_financial_news():
    """获取财经要闻"""
    print("\n📰 财经要闻 (东方财富)")
    print("=" * 60)
    try:
        df = ak.stock_news_em(symbol="财经")
        for i, row in df.head(15).iterrows():
            title = row.get('新闻标题', row.iloc[0] if len(row) > 0 else '')
            time_str = row.get('发布时间', row.iloc[1] if len(row) > 1 else '')
            print(f"  • [{time_str}] {title}")
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_macro_news():
    """获取宏观要闻"""
    print("\n📰 宏观经济新闻")
    print("=" * 60)
    try:
        df = ak.macro_news()
        for i, row in df.head(15).iterrows():
            title = row.iloc[0] if len(row) > 0 else ''
            time_str = row.iloc[1] if len(row) > 1 else ''
            print(f"  • [{time_str}] {title}")
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_policy_keywords():
    """政策关键词扫描 — 从新闻标题中筛出政策相关"""
    print("\n🔍 政策关键词扫描")
    print("=" * 60)

    policy_words = [
        '央行', '降准', '降息', 'LPR', 'MLF', '逆回购',
        '国务院', '国常会', '发改委', '财政部', '证监会',
        '政策', '监管', '改革', '调控', '扶持', '补贴',
        '产业政策', '财政', '货币', '金融', '房地产',
        '新基建', '新能源', '芯片', 'AI', '人工智能',
        '碳中和', '共同富裕', '一带一路', 'RCEP',
    ]

    all_titles = []

    # 从多个来源收集标题
    sources = [
        ('东方财富', lambda: ak.stock_news_em(symbol="财经")),
        ('同花顺', lambda: ak.news_cctv(date=datetime.now().strftime("%Y%m%d"))),
    ]

    for source_name, fetcher in sources:
        try:
            df = fetcher()
            if df is not None:
                for _, row in df.iterrows():
                    title = str(row.iloc[0])
                    time_str = str(row.iloc[1]) if len(row) > 1 else ''
                    all_titles.append({
                        'source': source_name,
                        'title': title,
                        'time': time_str,
                        'matched_keywords': []
                    })
        except:
            continue

    # 关键词匹配
    policy_related = []
    for item in all_titles:
        for kw in policy_words:
            if kw in item['title']:
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
            if kw in item['title']:
                impacts.append({'title': item['title'], 'keyword': kw, 'impact': impact, 'direction': '📈 利好'})
        for kw, impact in bearish_kw.items():
            if kw in item['title']:
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
