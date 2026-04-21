#!/usr/bin/env python3
"""
A股个股多渠道新闻快速聚合工具

功能:
  - 支持输入6位股票代码, 自动查询公司名称, 双关键词(代码+名称)并行搜索
  - 5路数据源并发抓取: 东方财富(公告+新闻)、新浪财经、新浪7x24快讯、腾讯财经、凤凰财经
  - 标题归一化去重, 合并多渠道结果
  - 基于加权关键词库的情感分析: 每个关键词带1-3权重, 输出 0-10 评分 (5=中性, 8-10=强烈利好, 0-2=强烈利空)
  - 默认丢弃中性消息(无明确利好利空), --keep-neutral 可保留
  - 摘要提取: 有明确摘要字段时直接使用完整摘要, 无摘要时兜底提取前两句
  - 综合评分: 基于时间指数衰减(每天衰减20%), 非中性消息加权, 输出加权平均分
  - 支持 JSON 输出(--json-only), 可直接重定向到文件供下游消费

用法: python stock_news_fetcher.py <股票代码> [有效天数] [--top N] [--json-only]
示例: python stock_news_fetcher.py 000858 3 --top 20
      python stock_news_fetcher.py 600519 7 --json-only > news.json
"""

import sys
import json
import re
import hashlib
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ─── 配置 ───────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 12
MAX_NEWS = 30

# ─── 情感关键词库 (权重1-3, 越强信号权重越高) ───────────
# 权重 3: 重大信号 (涨停/跌停/暴雷/重大利好)
# 权重 2: 明确信号 (业绩增长/减持/中标/诉讼)
# 权重 1: 轻微信号 (增长/风险/拓展/承压)

POSITIVE_WORDS = {
    # ── 股价/行情 (权重3:极端行情, 2:明确涨, 1:温和涨) ──
    "涨停": 3, "封板": 3, "连板": 3, "一字涨停": 3, "地天板": 3,
    "大涨": 2, "暴涨": 2, "飙升": 2, "强势涨停": 3,
    "创新高": 2, "历史新高": 2, "创上市以来新高": 2, "历史新高纪录": 3,
    "突破": 2, "放量上涨": 2, "领涨": 2, "涨幅居前": 2,
    "强势": 1, "反弹": 1, "反转": 2, "阳线": 1,
    "金叉": 1, "站上": 1, "突破前高": 2, "开启主升浪": 2,
    "上涨": 1, "翻红": 1, "走强": 1, "回暖": 1,
    # ── 业绩/财务 ──
    "业绩增长": 2, "利润增长": 2, "营收增长": 2, "净利增长": 2,
    "营收大增": 2, "净利大增": 2, "业绩大增": 2,
    "超预期": 3, "超市场预期": 3, "远超预期": 3, "大超预期": 3,
    "业绩预增": 2, "预增": 2, "同比增": 1, "环比增": 1,
    "两位数增长": 2, "三位数增长": 3, "翻倍": 3, "翻番": 3,
    "扭亏": 2, "扭亏为盈": 2, "盈利改善": 1, "利润率提升": 1,
    "毛利率提升": 1, "净资产收益率提升": 1, "现金流改善": 1,
    "负债率下降": 1, "财务状况改善": 1,
    "高增长": 2, "持续增长": 1, "稳健增长": 1,
    "创纪录": 2, "最佳业绩": 2, "业绩新高": 2, "上市以来最佳": 2,
    # ── 估值/评级 ──
    "买入": 2, "买入评级": 2, "增持": 2, "增持评级": 2,
    "推荐": 2, "强烈推荐": 3, "强于大市": 2, "优于大盘": 2,
    "上调目标价": 2, "上调评级": 2, "看好": 1,
    "调入": 2, "纳入": 1, "重仓": 2, "加仓": 2,
    "抢筹": 2, "扫货": 2, "抄底": 1, "机构抢筹": 2,
    "北向加仓": 2, "外资买入": 2, "主力流入": 2,
    # ── 经营/业务 ──
    "重大合同": 3, "中标": 2, "签大单": 2, "获得订单": 2,
    "订单充裕": 2, "订单饱满": 2, "订单爆满": 3,
    "战略合作": 2, "战略合作协议": 2, "深度合作": 2, "强强联合": 2,
    "新产品": 1, "新技术": 1, "新突破": 2, "技术突破": 2,
    "量产": 2, "投产": 2, "达产": 2, "满产满销": 2,
    "产能扩张": 2, "扩产": 2, "新产线": 1, "新基地": 1,
    "获批": 2, "批准": 2, "通过审核": 2, "拿到牌照": 2,
    "市占率提升": 2, "市场份额扩大": 2, "龙头": 1, "行业龙头": 2,
    "领军": 1, "龙头地位巩固": 2, "市占率第一": 2,
    # ── 资本运作 ──
    "分红": 1, "派息": 1, "高送转": 2, "送股": 1, "派现": 1,
    "大额分红": 2, "特别分红": 2, "超比例分红": 2,
    "回购": 2, "回购注销": 2, "大股东增持": 3, "高管增持": 2,
    "持续增持": 2, "增持计划": 2, "回购计划": 2,
    "并购": 2, "收购": 2, "重组获批": 3, "定增获批": 2,
    "引入战投": 2, "战略入股": 2, "股权激励": 2, "员工持股": 2,
    "上市": 2, "IPO过会": 2, "科创板上市": 2, "港股上市": 2,
    # ── 政策/宏观 ──
    "政策利好": 3, "政策支持": 2, "扶持": 2, "鼓励": 1,
    "减税降费": 2, "降息": 2, "降准": 2, "宽松": 1,
    "行业景气": 2, "景气上行": 2, "需求旺盛": 2, "供不应求": 2,
    "涨价": 2, "提价": 2, "量价齐升": 3, "顺周期": 1,
    "风口": 1, "热点": 1, "主线": 1,
    "新能源": 1, "国产替代": 2, "自主可控": 2, "出海": 1,
    "全球化": 1, "一带一路": 1, "新质生产力": 1,
    # ── 其他正面 ──
    "进军": 1, "拓展": 1, "布局": 1, "开辟": 1, "深耕": 1,
    "发布": 1, "亮相": 1, "突破性": 2, "重大进展": 2,
    "里程碑": 2, "获得": 1, "赢得": 1, "拿下": 1, "成功": 1,
    "利好": 2, "重大利好": 3, "利好消息": 2, "利好来袭": 3,
    "表彰": 1, "获奖": 1, "入选": 1, "入围": 1, "认证": 1,
    "捐赠": 1, "公益": 1, "社会责任": 1,
    "ESG评级提升": 2, "绿色转型": 1, "碳中和": 1,
}
NEGATIVE_WORDS = {
    # ── 股价/行情 (权重3:极端行情, 2:明确跌, 1:温和跌) ──
    "跌停": 3, "一字跌停": 3, "连续跌停": 3, "天地板": 3,
    "闪崩": 3, "崩盘": 3, "崩塌": 3, "跳水": 2,
    "大跌": 2, "暴跌": 2, "重挫": 2, "大幅下挫": 2,
    "破位": 2, "破发": 2, "破净": 2, "新低": 2,
    "历史新低": 3, "创上市以来新低": 3,
    "放量下跌": 2, "领跌": 2, "跌幅居前": 2,
    "弱势": 1, "阴线": 1, "死叉": 1, "跌破": 1, "失守": 1,
    "连续阴跌": 2, "高位回落": 2, "高位跳水": 3,
    "获利回吐": 1, "恐慌抛售": 3, "踩踏": 3,
    "下跌": 1, "走弱": 1, "承压回落": 1, "拖累": 1,
    # ── 业绩/财务 ──
    "业绩下滑": 2, "利润下滑": 2, "营收下降": 2, "净利下滑": 2,
    "净利大跌": 3, "营收大降": 2, "业绩大降": 2,
    "不及预期": 2, "低于预期": 2, "业绩暴雷": 3, "暴雷": 3,
    "业绩变脸": 3, "业绩地雷": 3, "突然亏损": 3,
    "亏损": 2, "巨亏": 3, "大幅亏损": 3, "由盈转亏": 3,
    "亏损扩大": 2, "持续亏损": 2, "连年亏损": 2,
    "同比降": 1, "环比降": 1, "大幅下降": 2, "骤降": 2,
    "商誉减值": 3, "资产减值": 2, "计提减值": 2, "大额计提": 3,
    "毛利率下降": 2, "利润率下滑": 2, "现金流恶化": 2,
    "负债率攀升": 2, "应收账款暴增": 2, "存货积压": 2, "经营恶化": 2,
    "财务造假": 3, "财务异常": 2, "审计问题": 2,
    "非标意见": 3, "保留意见": 2, "无法表示意见": 3,
    # ── 估值/评级 ──
    "卖出": 2, "卖出评级": 3, "减持评级": 2,
    "下调目标价": 2, "下调评级": 2, "看空": 2,
    "减持": 2, "抛售": 2, "清仓": 3, "出逃": 2,
    "资金出逃": 2, "主力出逃": 2, "北向出逃": 2,
    "调出": 1, "剔除": 1, "看跌": 1,
    "估值偏高": 1, "泡沫": 2, "高估": 1, "被高估": 1,
    # ── 经营/业务 ──
    "裁员": 2, "减员": 2, "降薪": 2, "欠薪": 3, "拖欠工资": 3,
    "关停": 3, "停产": 2, "停业": 2, "关闭": 2,
    "破产": 3, "清算": 3, "重整": 2, "濒临破产": 3,
    "质量事故": 3, "安全事故": 3, "安全问题": 2,
    "质量门": 2, "产品召回": 2, "产品问题": 2,
    "产能过剩": 2, "过剩": 1, "库存高企": 2,
    "需求萎缩": 2, "订单下滑": 2, "订单取消": 2,
    "竞争加剧": 1, "市占率下降": 2, "份额流失": 2, "客户流失": 2,
    "专利纠纷": 2, "知识产权诉讼": 2, "核心技术被替代": 3,
    # ── 资本运作 ──
    "大股东减持": 3, "高管减持": 2, "减持套现": 2,
    "清仓减持": 3, "巨额减持": 3, "违规减持": 3,
    "限售解禁": 2, "大额解禁": 2, "解禁潮": 2,
    "定增终止": 2, "重组失败": 3, "并购失败": 2, "IPO被否": 2,
    "股权冻结": 3, "质押爆仓": 3, "强平": 3, "被动减持": 2,
    "控股权变更": 2, "实控人变更": 2, "控制权之争": 2,
    # ── 政策/监管 ──
    "政策收紧": 2, "监管风险": 2, "监管调查": 3, "监管处罚": 3,
    "约谈": 2, "罚款": 2, "处罚": 2, "违规": 2, "违法": 3,
    "立案调查": 3, "立案侦查": 3, "刑事立案": 3,
    "警告": 1, "通报批评": 2, "公开谴责": 3, "通报": 1,
    "暂停上市": 3, "终止上市": 3, "退市": 3, "ST": 2,
    "被ST": 3, "摘牌": 3, "面值退市": 3, "退市风险": 3,
    "反垄断": 2, "反垄断调查": 3, "反垄断处罚": 3,
    "造假": 3, "欺诈": 3, "信披违规": 3,
    "内幕交易": 3, "操纵股价": 3, "市场操纵": 3,
    # ── 债务/法律 ──
    "债务": 1, "债务危机": 3, "债务违约": 3, "债转股": 2,
    "债务重组": 2, "流动性危机": 3, "资金链断裂": 3,
    "违约": 2, "逾期": 2, "逾期兑付": 3, "无法兑付": 3,
    "诉讼": 2, "被告": 2, "被诉": 2, "仲裁": 2, "被仲裁": 2,
    "巨额索赔": 3, "集体诉讼": 3, "赔偿": 2,
    "冻结": 2, "查封": 2, "执行": 1,
    "列为失信": 3, "限消令": 3, "被强制执行": 3,
    # ── 其他负面 ──
    "利空": 2, "利空消息": 2, "利空来袭": 3, "重大利空": 3,
    "风险": 1, "高风险": 2, "风险提示": 1, "警示": 1,
    "关注函": 1, "问询函": 2, "监管函": 2, "承压": 1,
    "黑天鹅": 3, "灰犀牛": 2, "系统性风险": 2,
    "负面": 1, "负面消息": 2, "丑闻": 3, "舆论风波": 2, "信任危机": 3,
    "人间蒸发": 3, "跑路": 3, "实控人失联": 3,
}


# ─── 数据模型 ────────────────────────────────────────────
@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    publish_time: str
    summary: str = ""
    sentiment: str = "neutral"
    score: float = 0.0
    keywords: list = field(default_factory=list)
    _hash: str = ""

    def __post_init__(self):
        if not self._hash:
            raw = f"{self.title}{self.source}{self.publish_time}"
            self._hash = hashlib.md5(raw.encode()).hexdigest()[:12]


# ─── 股票名称/代码互查 ────────────────────────────────────────
def get_stock_name(code: str) -> str:
    """通过新浪接口获取股票名称"""
    try:
        resp = requests.get(
            f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key={code}",
            headers=HEADERS, timeout=5)
        resp.encoding = "gbk"
        for item in resp.text.split(";"):
            parts = item.split(",")
            if len(parts) >= 5 and code in parts[2]:
                name = parts[4].strip()
                if name:
                    return name
    except:
        pass
    return code


def resolve_code(input_str: str) -> tuple[str, str]:
    """
    输入代码或名称, 返回 (代码, 名称)
    支持: 000858 / 五粮液 / sz000858
    """
    s = input_str.strip()
    # 纯数字 → 直接查名称
    if re.match(r'^[036]\d{5}$', s):
        return s, get_stock_name(s)
    # 带市场前缀 → 去前缀后查名称
    m = re.match(r'^(sh|sz|bj)(\d{6})$', s, re.IGNORECASE)
    if m:
        code = m.group(2)
        return code, get_stock_name(code)
    # 非数字 → 按名称搜索代码
    try:
        resp = requests.get(
            f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key={s}",
            headers=HEADERS, timeout=5)
        resp.encoding = "gbk"
        for item in resp.text.split(";"):
            parts = item.split(",")
            if len(parts) >= 5 and re.match(r'^[036]\d{5}$', parts[2]):
                code = parts[2]
                name = parts[4].strip()
                return code, name or s
    except:
        pass
    # 兜底: 原样返回
    return s, s


# ─── 工具函数 ────────────────────────────────────────────
def clean_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)


def analyze_sentiment(title: str, summary: str = "") -> tuple:
    """
    加权情感评分, 0-10 分:
      0-2: 强烈利空  3-4: 利空  5: 中性  6-7: 利好  8-10: 强烈利好
    每个关键词带权重(1-3), 累计加权得分后归一化
    """
    text = f"{title} {summary}"
    pos_weight, neg_weight = 0, 0
    pos_hits, neg_hits = [], []

    for word, weight in POSITIVE_WORDS.items():
        if word in text:
            pos_weight += weight
            pos_hits.append(word)

    for word, weight in NEGATIVE_WORDS.items():
        if word in text:
            neg_weight += weight
            neg_hits.append(word)

    total = pos_weight + neg_weight
    if total == 0:
        return "neutral", 5.0, []

    # 原始比率 -1 ~ 1
    raw = (pos_weight - neg_weight) / total
    # 关键词数量越多, 置信度越高, 分数越极端
    confidence = min(total / 6, 1.0)  # 权重和 ≥6 时满置信
    # 映射到 0 ~ 10, 中性锚定在 5
    score = round(raw * 5 * confidence + 5, 1)
    # 限制范围
    score = max(0.0, min(10.0, score))

    if score >= 7:
        label = "positive"
    elif score <= 3:
        label = "negative"
    else:
        label = "neutral"

    return label, score, list(set(pos_hits + neg_hits))


def summarize_text(title: str, text: str, abstract: str = "", max_len: int = 1000) -> str:
    """
    摘要提取逻辑:
    1. 有明确摘要字段 → 直接使用整段摘要
    2. 无摘要 → 保留全文 (超 max_len 截断)
    """
    # 优先使用明确的摘要字段, 保留完整内容
    if abstract and len(abstract.strip()) > 10:
        clean = clean_html(abstract).strip()
        if len(clean) > max_len:
            clean = clean[:max_len] + "…"
        return clean
    # 兜底: 无摘要时保留全文
    if not text:
        return title[:max_len]
    clean = clean_html(text)
    if len(clean) > max_len:
        clean = clean[:max_len] + "…"
    return clean


def parse_time(t: str) -> Optional[datetime]:
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            return datetime.strptime(t.strip(), fmt)
        except:
            continue
    return None


# ═══════════════════════════════════════════════════════════
# 数据源 1: 东方财富 — 公告 + 新闻 (搜索接口)
# ═══════════════════════════════════════════════════════════
def fetch_eastmoney(code: str, days: int, name: str = "") -> list[NewsItem]:
    """东方财富搜索接口 — 高覆盖 (同时搜代码+名称)"""
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    keywords = list(set([code, name])) if name else [code]

    # 1a. 公告 (只用代码)
    try:
        url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
        params = {"sr": -1, "page_size": MAX_NEWS, "page_index": 1,
                  "ann_type": "A", "client_source": "web", "stock_list": code}
        resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            return items
        data = resp.json()
        for art in data.get("data", {}).get("list", []):
            title = art.get("title", "")
            if not title:
                continue
            time_str = art.get("display_time", art.get("notice_date", ""))
            pub_time = parse_time(time_str[:19])
            if not pub_time or pub_time < cutoff:
                continue
            art_code = art.get("art_code", "")
            art_url = f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
            summary = summarize_text(title)
            sentiment, score, skw = analyze_sentiment(title, summary)
            items.append(NewsItem(title=title, source="eastmoney_ann",
                                  url=art_url, publish_time=pub_time.isoformat(),
                                  summary=summary, sentiment=sentiment,
                                  score=score, keywords=skw))
    except Exception as e:
        print(f"  [东方财富公告] 异常: {e}", file=sys.stderr)

    # 1b. 新闻 (用名称搜索覆盖更全)
    for kw in keywords:
        try:
            search_url = (
                "https://search-api-web.eastmoney.com/search/jsonp"
                "?cb=jQuery&param=" + json.dumps({
                    "uid": "", "keyword": kw,
                    "type": ["cmsArticleWebOld"],
                    "client": "web", "clientType": "web", "clientVersion": "curr",
                    "param": {"cmsArticleWebOld": {
                        "searchScope": "default", "sort": "default",
                        "pageIndex": 1, "pageSize": MAX_NEWS,
                        "preTag": "", "postTag": ""
                    }}
                }, separators=(',', ':'))
            )
            resp = requests.get(search_url, headers=HEADERS, timeout=TIMEOUT)
            resp.encoding = "utf-8"
            m = re.search(r'jQuery\((\{.*\})\)\s*;?\s*$', resp.text, re.DOTALL)
            if not m:
                # 兼容: 回退到旧格式
                m = re.search(r'\{.*\}', resp.text, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(1) if m.lastindex else m.group())
            articles = data.get("result", {}).get("cmsArticleWebOld", [])
            if isinstance(articles, dict):
                articles = articles.get("list", [])
            for art in articles:
                title = clean_html(art.get("title", ""))
                if not title:
                    continue
                date_str = art.get("date", "")
                pub_time = parse_time(date_str)
                if not pub_time or pub_time < cutoff:
                    continue
                url_link = art.get("url", "")
                if url_link and not url_link.startswith("http"):
                    url_link = "https:" + url_link
                content = art.get("content", "")
                summary = summarize_text(title, "", abstract=content)
                sentiment, score, skw = analyze_sentiment(title, summary)
                items.append(NewsItem(title=title, source="eastmoney_news",
                                      url=url_link, publish_time=pub_time.isoformat(),
                                      summary=summary, sentiment=sentiment,
                                      score=score, keywords=skw))
        except Exception as e:
            print(f"  [东方财富新闻/{kw}] 异常: {e}", file=sys.stderr)

    return items


# ═══════════════════════════════════════════════════════════
# 数据源 2: 新浪财经
# ═══════════════════════════════════════════════════════════
def fetch_sina(code: str, days: int, name: str = "") -> list[NewsItem]:
    """新浪财经新闻 — HTML 解析"""
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    try:
        # Sina 股票页面
        market = "sh" if code.startswith("6") else "sz"
        url = f"https://finance.sina.com.cn/realstock/company/{market}{code}/news.shtml"
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.encoding = "gbk"

        # 也尝试 JS 接口
        js_url = f"https://finance.sina.com.cn/stock/jsonp/suggest/getdata.php?q={market}{code}"
        # 主要用 HTML 解析
        soup = BeautifulSoup(resp.text, "html.parser")

        for a_tag in soup.select("a[href*='/news/'], a[href*='finance.sina.com.cn']"):
            title = a_tag.get_text(strip=True)
            if len(title) < 8 or not re.search(r'[\u4e00-\u9fff]', title):
                continue
            href = a_tag.get("href", "")
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://finance.sina.com.cn" + href

            # 查找时间
            parent = a_tag.parent
            time_text = parent.get_text() if parent else ""
            m = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(\d{2}:\d{2})?', time_text)
            if m:
                date_str = m.group(1) + (" " + m.group(2) if m.group(2) else "")
                pub_time = parse_time(date_str.replace("/", "-"))
            else:
                continue

            if not pub_time or pub_time < cutoff:
                continue

            summary = summarize_text(title)
            sentiment, score, keywords = analyze_sentiment(title, summary)
            items.append(NewsItem(title=title, source="sina",
                                  url=href, publish_time=pub_time.isoformat(),
                                  summary=summary, sentiment=sentiment,
                                  score=score, keywords=keywords))
    except Exception as e:
        print(f"  [新浪财经] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 3: 新浪 7x24 财经快讯 (关键词搜索)
# ═══════════════════════════════════════════════════════════
def fetch_sina7x24(code: str, days: int, name: str = "") -> list[NewsItem]:
    """新浪 7x24 直播快讯搜索"""
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    search_terms = [code]
    if name:
        search_terms.append(name)
    try:
        url = "https://zhibo.sina.com.cn/api/zhibo/feed"
        params = {"page": 1, "page_size": 50, "zhibo_id": 152,
                  "tag_id": 0, "type": 0}
        resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            return items
        data = resp.json()

        for item_data in data.get("result", {}).get("data", {}).get("feed", {}).get("feed_list", []):
            content = item_data.get("rich_text", "") or item_data.get("body", "")
            title = clean_html(content)
            # 匹配代码或名称
            if not any(t in title for t in search_terms):
                continue
            ctime = item_data.get("create_time", "")
            pub_time = parse_time(ctime)
            if not pub_time or pub_time < cutoff:
                continue

            summary = summarize_text(title, content)
            sentiment, score, skw = analyze_sentiment(title, summary)
            items.append(NewsItem(
                title=title[:80], source="sina7x24",
                url=f"https://zhibo.sina.com.cn/152/{item_data.get('id', '')}.html",
                publish_time=pub_time.isoformat(),
                summary=summary, sentiment=sentiment,
                score=score, keywords=skw))
    except Exception as e:
        print(f"  [新浪7x24] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 4: 腾讯财经
# ═══════════════════════════════════════════════════════════
def fetch_qq(code: str, days: int, name: str = "") -> list[NewsItem]:
    """腾讯财经个股新闻"""
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    try:
        # 用名称搜索
        keyword = name or code
        url = "https://i.news.qq.com/web_feed/getPCList"
        payload = {
            "qimei36": "",
            "appver": "2401.01",
            "devid": "",
            "os": "2",
            "category": "stock",
            "ext": {"keyword": keyword},
            "page": 0,
            "num": 20,
        }
        resp = requests.post(url, headers={**HEADERS, "Content-Type": "application/json"},
                             json=payload, timeout=TIMEOUT)
        data = resp.json() if resp.status_code == 200 else {}
        d = data.get("data") or {}
        news_list = d.get("list", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
        for item in news_list:
            if isinstance(item, str):
                continue
            title = item.get("title", "") or item.get("brief", "")
            if not title:
                continue
            ctime = item.get("timestamp", "") or item.get("time", "") or item.get("date", "")
            if isinstance(ctime, (int, float)) and ctime > 1e9:
                pub_time = datetime.fromtimestamp(ctime)
            else:
                pub_time = parse_time(str(ctime))
            if not pub_time or pub_time < cutoff:
                continue
            art_url = item.get("url", "") or item.get("article_url", "")
            if art_url and not art_url.startswith("http"):
                art_url = "https:" + art_url
            summary = summarize_text(title, item.get("brief", "") or item.get("abstract", ""))
            sentiment, score, skw = analyze_sentiment(title, summary)
            items.append(NewsItem(title=title, source="qq_finance",
                                  url=art_url, publish_time=pub_time.isoformat(),
                                  summary=summary, sentiment=sentiment,
                                  score=score, keywords=skw))
    except Exception as e:
        print(f"  [腾讯财经] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 5: 凤凰财经
# ═══════════════════════════════════════════════════════════
def fetch_ifeng(code: str, days: int, name: str = "") -> list[NewsItem]:
    """凤凰网财经搜索"""
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    try:
        url = "https://so.finance.ifeng.com/api/getSearchNews"
        params = {"q": code, "p": 1, "ps": 20, "type": "news"}
        resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            return items
        data = resp.json()
        items_list = (data.get("result", {}) or {}).get("items", []) or data.get("data", []) or []
        for item in items_list:
            title = item.get("title", "") or item.get("name", "")
            title = clean_html(title)
            if not title:
                continue
            ctime = item.get("timeStamp", "") or item.get("updateTime", "") or item.get("time", "")
            if isinstance(ctime, (int, float)) and ctime > 1e9:
                pub_time = datetime.fromtimestamp(ctime)
            else:
                pub_time = parse_time(str(ctime))
            if not pub_time or pub_time < cutoff:
                continue
            art_url = item.get("url", "") or item.get("articleUrl", "")
            if art_url and not art_url.startswith("http"):
                art_url = "https:" + art_url
            summary = summarize_text(title, item.get("brief", item.get("digest", "")))
            sentiment, score, keywords = analyze_sentiment(title, summary)
            items.append(NewsItem(title=title, source="ifeng",
                                  url=art_url, publish_time=pub_time.isoformat(),
                                  summary=summary, sentiment=sentiment,
                                  score=score, keywords=keywords))
    except Exception as e:
        print(f"  [凤凰财经] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 时间衰减 & 综合评分
# ═══════════════════════════════════════════════════════════
DECAY_PER_DAY = 0.8  # 每天衰减 20%, 7天后保留 ~21% 权重


def time_decay(hours_old: float) -> float:
    """指数衰减: 每天保留 80% 权重"""
    return DECAY_PER_DAY ** (hours_old / 24)


def calc_composite(items: list[dict]) -> dict:
    """
    计算综合评分:
    - 每条新闻按发布时间做指数衰减
    - 利好/利空消息权重大于中性
    - 输出加权平均综合分 + 情绪分布
    """
    now = datetime.now()
    total_weighted_score = 0.0
    total_weight = 0.0
    pos_count = neg_count = neu_count = 0
    pos_weight = neg_weight = 0.0

    for item in items:
        pub = datetime.fromisoformat(item["publish_time"])
        hours_old = (now - pub).total_seconds() / 3600
        decay = time_decay(hours_old)

        # 非中性消息额外加权 ×1.5
        sentiment_boost = 1.5 if item["sentiment"] != "neutral" else 1.0
        weight = decay * sentiment_boost

        # 对于综合分, 中性(5分)不拉不动结果, 只统计有信号的
        if item["sentiment"] == "positive":
            pos_count += 1
            pos_weight += weight
        elif item["sentiment"] == "negative":
            neg_count += 1
            neg_weight += weight
        else:
            neu_count += 1

        total_weighted_score += item["score"] * weight
        total_weight += weight

    if total_weight == 0 or len(items) == 0:
        composite = 5.0
    else:
        composite = round(total_weighted_score / total_weight, 1)
        composite = max(0.0, min(10.0, composite))

    # 信号方向
    if composite >= 7:
        direction = "利好"
    elif composite <= 3:
        direction = "利空"
    elif composite >= 6:
        direction = "偏利好"
    elif composite <= 4:
        direction = "偏利空"
    else:
        direction = "中性"

    # 为每条新闻附加衰减后信息
    enriched = []
    for item in items:
        pub = datetime.fromisoformat(item["publish_time"])
        hours = (now - pub).total_seconds() / 3600
        d = time_decay(hours)
        item["hours_ago"] = round(hours, 1)
        item["weight"] = round(d, 3)
        enriched.append(item)

    return {
        "composite_score": composite,
        "direction": direction,
        "total_news": len(items),
        "positive": pos_count,
        "negative": neg_count,
        "neutral": neu_count,
        "news": enriched,
    }


# ═══════════════════════════════════════════════════════════
# 去重 & 排序
# ═══════════════════════════════════════════════════════════
def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    seen = {}
    result = []
    for item in items:
        norm = re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\u2014\u2018\u2019\u201c\u201d]',
                       '', item.title)
        if norm not in seen:
            seen[norm] = True
            result.append(item)
    return result


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════
def fetch_all(input_str: str, days: int, sources: list = None) -> dict:
    # 代码/名称互查
    code, stock_name = resolve_code(input_str)
    print(f"\n📡 正在获取 [{code} {stock_name}] 最近 {days} 天的新闻...\n", file=sys.stderr)

    all_fetchers = {
        "eastmoney": fetch_eastmoney,
        "sina": fetch_sina,
        "sina7x24": fetch_sina7x24,
        "qq": fetch_qq,
        "ifeng": fetch_ifeng,
    }
    if sources:
        fetchers = {k: v for k, v in all_fetchers.items() if k in sources}
    else:
        fetchers = all_fetchers

    all_items = []
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {pool.submit(fn, code, days, stock_name): name for name, fn in fetchers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                print(f"  ✅ {name:12s} → {len(items):>3d} 条", file=sys.stderr)
                all_items.extend(items)
            except Exception as e:
                print(f"  ❌ {name:12s} → 异常: {e}", file=sys.stderr)

    before = len(all_items)
    all_items = deduplicate(all_items)
    print(f"\n  🔍 去重: {before} → {len(all_items)} 条", file=sys.stderr)

    # 排序: 时间降序, 最新在前
    all_items.sort(key=lambda x: x.publish_time, reverse=True)

    # 输出时剔除无用字段
    dicts = [{k: v for k, v in asdict(i).items()
              if k not in ("source", "url", "_hash")} for i in all_items]

    # 计算综合评分 (含时间衰减)
    return calc_composite(dicts)


# ═══════════════════════════════════════════════════════════
# 控制台输出
# ═══════════════════════════════════════════════════════════
SENTIMENT_ICONS = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}


def print_table(result: dict, top_n: int = 0):
    items = result["news"]
    if top_n > 0:
        items = items[:top_n]

    # 综合评分
    sc = result["composite_score"]
    direction = result["direction"]
    icon = "🟢" if sc >= 6 else ("🔴" if sc <= 4 else "⚪")
    bar_len = max(1, int(abs(sc - 5) * 2))
    bars = "█" * bar_len

    print(f"\n  {icon} 综合评分: {bars} {sc}/10  [{direction}]  "
          f"利好{result['positive']} 利空{result['negative']} 中性{result['neutral']}")

    if not items:
        print("  (无有效消息)")
        return

    print()
    for i, item in enumerate(items, 1):
        icon = SENTIMENT_ICONS.get(item["sentiment"], "⚪")
        score = item["score"]
        bar_len = max(1, int(abs(score - 5) * 2))
        bars = "█" * bar_len
        kw = " ".join(item["keywords"][:5]) if item["keywords"] else ""
        ago = item.get("hours_ago", "")
        ago_str = f"{ago:.0f}h前" if ago and ago >= 1 else ("刚刚" if ago else "")
        w = item.get("weight", "")

        print(f"  {icon} {bars:<6} {score:>4.1f}/10  {ago_str:<8} w={w}  {item['publish_time'][:16]}")
        print(f"      {item['title']}")
        if item["summary"] and item["summary"] != item["title"]:
            print(f"      📝 {item['summary'][:120]}")
        if kw:
            print(f"      🏷️  {kw}")
        print()


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="A股个股多渠道新闻聚合工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s 000858            # 五粮液(代码), 最近3天
  %(prog)s 五粮液            # 五粮液(名称), 最近3天
  %(prog)s 贵州茅台 7        # 茅台, 最近7天
  %(prog)s 300750 3 --top 10 # 宁德时代, 前10条
  %(prog)s 000858 5 --json-only > wly.json  # 输出JSON
        """)
    parser.add_argument("code", help="股票代码或名称 (如 000858、五粮液、贵州茅台)")
    parser.add_argument("days", type=int, nargs="?", default=3, help="有效天数 (默认 3)")
    parser.add_argument("--top", type=int, default=0, help="只输出前 N 条")
    parser.add_argument("--json-only", action="store_true", help="只输出 JSON (可重定向)")
    parser.add_argument("--keep-neutral", action="store_true", help="保留中性消息")
    parser.add_argument("--sources", nargs="+",
                        choices=["eastmoney", "sina", "sina7x24", "qq", "ifeng"],
                        default=None, help="指定数据源 (默认全部)")
    args = parser.parse_args()

    result = fetch_all(args.code.strip(), args.days, args.sources)
    items = result["news"]

    if not args.keep_neutral:
        filtered = [i for i in items if i["sentiment"] != "neutral"]
        neu_removed = len(items) - len(filtered)
        if neu_removed > 0:
            print(f"  🗑️  过滤 {neu_removed} 条中性消息", file=sys.stderr)
        result = calc_composite(filtered)

    if args.top > 0:
        result["news"] = result["news"][:args.top]

    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_table(result)
        print("\n" + "=" * 60)
        print("📋 JSON (可重定向到文件):")
        print("=" * 60)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
