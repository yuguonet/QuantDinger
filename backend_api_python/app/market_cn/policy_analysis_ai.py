#!/usr/bin/env python3
"""
最新政策解读 — AI 深度分析版
数据源: 直接抓取财经新闻 API → 交给大模型做政策解读
依赖: pip install requests beautifulsoup4 openai
"""

from datetime import datetime
import json
import os


def _get_openai_client():
    """懒加载 openai，缺失时 raise 清晰错误"""
    try:
        from openai import OpenAI
        return OpenAI
    except ImportError:
        raise ImportError(
            "❌ openai 包未安装\n"
            "   运行: pip install openai\n"
            "   或使用 --policy (关键词版，无需 LLM)"
        )


# ═══════════════════════════════════════════════════════
#  配置 — 支持任何 OpenAI 兼容的 API
# ═══════════════════════════════════════════════════════
#  读取方式: 环境变量 > 默认值
#  支持: DeepSeek / Moonshot / 智谱 / 通义千问 / OpenAI
# ═══════════════════════════════════════════════════════

DEFAULT_CONFIGS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "env_key": "MOONSHOT_API_KEY",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "env_key": "ZHIPU_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
    },
}


def get_llm_client(provider="deepseek"):
    """初始化 LLM 客户端"""
    OpenAI = _get_openai_client()
    config = DEFAULT_CONFIGS.get(provider, DEFAULT_CONFIGS["deepseek"])
    api_key = os.environ.get(config["env_key"])

    if not api_key:
        # 尝试从本地配置文件读取
        config_file = os.path.expanduser("~/.llm_config.json")
        if os.path.exists(config_file):
            with open(config_file) as f:
                local = json.load(f)
                api_key = local.get(provider, {}).get("api_key")
                if local.get(provider, {}).get("base_url"):
                    config["base_url"] = local[provider]["base_url"]
                if local.get(provider, {}).get("model"):
                    config["model"] = local[provider]["model"]

    if not api_key:
        raise ValueError(
            f"❌ 未找到 {provider} 的 API Key\n"
            f"   方式1: export {config['env_key']}=your_key\n"
            f"   方式2: 创建 ~/.llm_config.json:\n"
            f'   {{"{provider}": {{"api_key": "xxx"}}}}'
        )

    client = OpenAI(api_key=api_key, base_url=config["base_url"])
    return client, config["model"]


# ═══════════════════════════════════════════════════════
#  数据抓取
# ═══════════════════════════════════════════════════════

def fetch_news():
    """抓取最新财经/宏观/政策新闻 — 直接抓取，不依赖 AKShare"""
    from .news_fetcher import fetch_all_news
    print("  📡 抓取新闻 (直接 API)...")
    news = fetch_all_news(max_per_source=15)
    # 统一格式
    all_news = []
    for item in news:
        if item.get("title"):
            all_news.append({
                "source": item.get("source", "财经"),
                "title": item["title"],
                "time": item.get("time", ""),
                "url": item.get("url", ""),
            })
    print(f"  共获取 {len(all_news)} 条有效新闻")
    return all_news


# ═══════════════════════════════════════════════════════
#  AI 政策分析
# ═══════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一位资深的中国宏观策略分析师，擅长解读政策对金融市场的影响。

你的分析要求：
1. **准确识别**哪些新闻涉及实质性政策变动（非泛泛而谈）
2. **判断政策方向**：货币宽松/紧缩、财政扩张/收缩、产业扶持/限制、监管放松/收紧
3. **量化影响程度**：重大(★★★) / 中等(★★) / 轻微(★)
4. **关联板块**：明确指出利好的行业/板块，以及可能承压的板块
5. **时间维度**：短期刺激 / 中期趋势 / 长期结构性变化
6. **风险提示**：市场可能忽视的隐含风险

输出格式（JSON）：
```json
{
  "summary": "一段话概括当前政策基调",
  "overall_sentiment": "宽松/中性/偏紧",
  "policies": [
    {
      "headline": "政策标题",
      "source": "来源",
      "direction": "利好/利空/中性",
      "impact_level": "★★★/★★/★",
      "impact_detail": "详细分析...",
      "bullish_sectors": ["板块1", "板块2"],
      "bearish_sectors": ["板块3"],
      "time_horizon": "短期/中期/长期",
      "risks": "潜在风险"
    }
  ],
  "market_outlook": "综合政策面的市场展望",
  "actionable_advice": "可操作的策略建议"
}
```"""


def analyze_with_ai(news_list, provider="deepseek"):
    """用 LLM 分析政策新闻"""

    # 组装新闻给 AI
    news_text = "\n".join(
        f"[{n['source']} | {n['time']}] {n['title']}"
        for n in news_list if n['title']
    )

    user_prompt = f"""以下是今日抓取的最新财经新闻，请从中筛选出与政策相关的内容，并进行深度解读：

---
{news_text}
---

请严格按照 JSON 格式输出分析结果。如果新闻中没有实质性政策变动，也要说明当前政策真空期的特点。"""

    print(f"\n  🤖 调用 AI 分析 ({len(news_list)} 条新闻)...")

    client, model = get_llm_client(provider)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=4000,
    )

    content = response.choices[0].message.content

    # 提取 JSON (处理 markdown 代码块包裹的情况)
    json_str = content
    if "```json" in content:
        json_str = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        json_str = content.split("```")[1].split("```")[0]

    try:
        result = json.loads(json_str.strip())
    except json.JSONDecodeError:
        result = {"raw_response": content, "parse_error": True}

    result['raw_news'] = news_list
    result['llm_provider'] = provider
    result['llm_model'] = model
    result['timestamp'] = datetime.now().isoformat()

    return result


def pretty_print_analysis(result):
    """格式化输出分析结果"""
    print(f"\n{'='*60}")
    print(f"  🇨🇳 AI 政策解读报告")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  🤖 {result.get('llm_provider', '?')} / {result.get('llm_model', '?')}")
    print(f"{'='*60}")

    if result.get('parse_error'):
        print("\n  ⚠️ JSON 解析失败，原始输出:")
        print(result.get('raw_response', ''))
        return

    # 总结
    print(f"\n  📋 政策基调: {result.get('overall_sentiment', '?')}")
    print(f"  {result.get('summary', '')}")

    # 逐条分析
    policies = result.get('policies', [])
    if policies:
        print(f"\n  📌 政策详情 ({len(policies)} 条):")
        print(f"  {'-'*56}")
        for i, p in enumerate(policies, 1):
            direction_emoji = {'利好': '📈', '利空': '📉', '中性': '➡️'}.get(p.get('direction', ''), '❓')
            print(f"\n  {i}. {direction_emoji} [{p.get('impact_level', '')}] {p.get('headline', '')}")
            print(f"     方向: {p.get('direction', '?')} | 周期: {p.get('time_horizon', '?')}")
            if p.get('bullish_sectors'):
                print(f"     利好: {', '.join(p['bullish_sectors'])}")
            if p.get('bearish_sectors'):
                print(f"     利空: {', '.join(p['bearish_sectors'])}")
            print(f"     分析: {p.get('impact_detail', '')}")
            if p.get('risks'):
                print(f"     ⚠️ 风险: {p['risks']}")

    # 市场展望
    if result.get('market_outlook'):
        print(f"\n  🔮 市场展望:")
        print(f"  {result['market_outlook']}")

    # 操作建议
    if result.get('actionable_advice'):
        print(f"\n  💡 策略建议:")
        print(f"  {result['actionable_advice']}")


def policy_dashboard_ai(provider="deepseek"):
    """AI 政策解读主入口"""
    print(f"\n{'='*60}")
    print(f"  🇨🇳 AI 政策深度解读")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # 1. 抓取新闻
    print("\n  📡 抓取新闻...")
    news = fetch_news()
    print(f"  共 {len(news)} 条")

    if not news:
        print("  ❌ 未获取到任何新闻")
        return None

    # 2. AI 分析
    result = analyze_with_ai(news, provider=provider)

    # 3. 输出
    pretty_print_analysis(result)

    # 4. 保存
    output_file = 'policy_analysis_ai.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ 完整结果已保存: {output_file}")

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='AI 政策解读')
    parser.add_argument('--provider', default='deepseek',
                        choices=['deepseek', 'moonshot', 'zhipu', 'openai'],
                        help='LLM 提供商 (默认 deepseek)')
    args = parser.parse_args()

    policy_dashboard_ai(provider=args.provider)
