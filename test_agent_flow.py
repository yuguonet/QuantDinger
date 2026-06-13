#!/usr/bin/env python3
"""
QuantDinger Agent 交互测试 — 独立运行，不依赖 Flask/PostgreSQL。
测试意图分析 → 消息增强 → call_skill 流程。

用法:
  python test_agent_flow.py
  python test_agent_flow.py --llm  # 连接真实 LLM 测试完整流程
"""
import json
import os
import re
import sys
from typing import Any, Dict, Optional

# ═══════════════════════════════════════════════════════════════
# Mock 数据（替代数据库查询）
# ═══════════════════════════════════════════════════════════════

MOCK_STOCK_DB = {
    "大连圣亚": {"code": "600593", "name": "大连圣亚", "market": "SH"},
    "贵州茅台": {"code": "600519", "name": "贵州茅台", "market": "SH"},
    "宁德时代": {"code": "300750", "name": "宁德时代", "market": "SZ"},
    "比亚迪":   {"code": "002594", "name": "比亚迪",   "market": "SZ"},
    "中国平安": {"code": "601318", "name": "中国平安", "market": "SH"},
    "招商银行": {"code": "600036", "name": "招商银行", "market": "SH"},
    "隆基绿能": {"code": "601012", "name": "隆基绿能", "market": "SH"},
    "药明康德": {"code": "603259", "name": "药明康德", "market": "SH"},
}

MOCK_QUOTE = {
    "600593": {"price": 12.35, "change_pct": 2.15, "volume": "3.2亿", "turnover_rate": 5.8},
    "600519": {"price": 1688.00, "change_pct": -0.42, "volume": "18.5亿", "turnover_rate": 0.3},
    "300750": {"price": 198.50, "change_pct": 1.88, "volume": "45.2亿", "turnover_rate": 1.2},
}


# ═══════════════════════════════════════════════════════════════
# 1. 股票名称 → 代码提取（模拟 _extract_stock_code）
# ═══════════════════════════════════════════════════════════════

_EXCLUDE_WORDS = {
    "分析", "查询", "查看", "显示", "帮助", "你好", "请问", "怎么", "什么", "为什么",
    "股票", "行情", "走势", "涨跌", "买卖", "交易", "投资", "理财", "基金", "债券",
    "大盘", "指数", "板块", "行业", "概念", "题材", "热点", "龙头", "涨停", "跌停",
}


def extract_stock_code(msg: str) -> Optional[str]:
    """从消息中提取股票代码（支持数字和中文名称）。"""
    # 1. 数字代码（6位连续数字，不要求 word boundary）
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', msg)
    if m:
        return m.group(1)
    # 2. 中文名称查 mock 数据库（提取连续中文，再从长到短匹配）
    chinese_blocks = re.findall(r'[\u4e00-\u9fa5]+', msg)
    for block in chinese_blocks:
        # 去掉排除词
        clean = block
        for w in sorted(_EXCLUDE_WORDS, key=len, reverse=True):
            clean = clean.replace(w, "|")
        # 从长到短尝试匹配
        for part in clean.split("|"):
            part = part.strip()
            if len(part) < 2:
                continue
            # 从长到短滑窗
            for length in range(len(part), 1, -1):
                for i in range(len(part) - length + 1):
                    candidate = part[i:i+length]
                    if candidate in MOCK_STOCK_DB:
                        return MOCK_STOCK_DB[candidate]["code"]
    return None


# ═══════════════════════════════════════════════════════════════
# 2. 意图分析（简化版，不调 LLM）
# ═══════════════════════════════════════════════════════════════

_INTENT_PATTERNS = {
    "stock_analysis": (r"(分析|研究|看看|怎么样|能买吗|值不值得|好不好)",),
    "chart_view":     (r"(K线|图表|走势|曲线)",),
    "market_scan":    (r"(大盘|涨停|板块|热点|龙虎榜)",),
    "backtest":       (r"(回测|测试|验证|策略)",),
}


def analyze_intent(message: str) -> Dict[str, Any]:
    """简化意图分析。"""
    for intent, patterns in _INTENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, message):
                stock_code = extract_stock_code(message)
                stock_name = ""
                if stock_code:
                    for name, info in MOCK_STOCK_DB.items():
                        if info["code"] == stock_code:
                            stock_name = name
                            break
                return {
                    "domain": "finance",
                    "intent": intent,
                    "verb": "analyze",
                    "noun": "stock",
                    "stock_code": stock_code or "",
                    "stock_name": stock_name,
                    "confidence": 0.9 if stock_code else 0.6,
                }
    return {"domain": "chat", "intent": "general", "confidence": 0.3}


# ═══════════════════════════════════════════════════════════════
# 3. 消息增强（模拟 _enrich_message）
# ═══════════════════════════════════════════════════════════════

def enrich_message(message: str, context: Dict[str, Any], intent: Dict[str, Any]) -> str:
    """拼接上下文 + call_skill 指令。"""
    parts = []

    # 意图提示
    if intent.get("domain") == "finance":
        parts.append(f"[意图] domain={intent['domain']}, intent={intent['intent']}")
        if intent.get("stock_code"):
            parts.append(f"[参数] {json.dumps({'stock': intent['stock_code'], 'stock_name': intent.get('stock_name', '')}, ensure_ascii=False)}")

    # 股票上下文
    if context.get("stock_code"):
        parts.append(f"股票代码: {context['stock_code']}")
    if context.get("stock_name"):
        parts.append(f"股票名称: {context['stock_name']}")
    if context.get("realtime_quote"):
        parts.append(f"[实时行情]\n{json.dumps(context['realtime_quote'], ensure_ascii=False)[:500]}")

    # call_skill 指令
    if context.get("stock_code"):
        code = context["stock_code"]
        parts.append(
            f"\n[系统提示] 检测到股票分析请求。请使用 call_skill 执行分析：\n"
            f"1. call_skill(skill_name=\"technical_agent\", stock_code=\"{code}\") — 技术面\n"
            f"2. call_skill(skill_name=\"indicator_agent\", stock_code=\"{code}\") — 指标面\n"
            f"3. call_skill(skill_name=\"intelligence_agent\", stock_code=\"{code}\") — 情报面\n"
            f"汇总各 skill 的 SkillReport 后输出结构化分析报告。不要直接调底层工具。"
        )

    parts.append(message)
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 4. Mock call_skill（模拟 skill 执行）
# ═══════════════════════════════════════════════════════════════

def mock_call_skill(skill_name: str, stock_code: str) -> Dict[str, Any]:
    """模拟 call_skill 返回 SkillReport。"""
    reports = {
        "technical_agent": {
            "skill_name": "technical_agent",
            "stock_code": stock_code,
            "direction": "bullish",
            "confidence": 0.78,
            "score": 75,
            "factors": [
                {"name": "趋势", "value": "上升趋势第5天", "score": 80},
                {"name": "量价", "value": "放量突破前高", "score": 85},
                {"name": "均线", "value": "MA5>MA10>MA20 多头排列", "score": 78},
                {"name": "形态", "value": "突破平台整理", "score": 70},
            ],
        },
        "indicator_agent": {
            "skill_name": "indicator_agent",
            "stock_code": stock_code,
            "direction": "bullish",
            "confidence": 0.72,
            "score": 70,
            "factors": [
                {"name": "MACD", "value": "金叉，红柱放大", "score": 75},
                {"name": "RSI", "value": "62.5，中性偏强", "score": 65},
                {"name": "KDJ", "value": "J值80，短线偏强", "score": 70},
            ],
        },
        "intelligence_agent": {
            "skill_name": "intelligence_agent",
            "stock_code": stock_code,
            "direction": "neutral",
            "confidence": 0.55,
            "score": 55,
            "factors": [
                {"name": "新闻", "value": "近3日无重大新闻", "score": 50},
                {"name": "公告", "value": "无重大公告", "score": 50},
                {"name": "研报", "value": "近1月无新增研报", "score": 55},
            ],
        },
        "market_data_agent": {
            "skill_name": "market_data_agent",
            "stock_code": stock_code,
            "direction": "bullish",
            "confidence": 0.65,
            "score": 68,
            "factors": [
                {"name": "大盘", "value": "上证+0.82%", "score": 70},
                {"name": "板块", "value": "旅游板块+2.1%领涨", "score": 75},
                {"name": "资金", "value": "主力净流入1.2亿", "score": 65},
            ],
        },
    }
    return reports.get(skill_name, {"error": f"未知skill: {skill_name}"})


# ═══════════════════════════════════════════════════════════════
# 5. LLM 调用（可选，连接真实 LLM）
# ═══════════════════════════════════════════════════════════════

def call_llm(enriched_message: str) -> str:
    """调用真实 LLM（需要配置环境变量）。"""
    import requests

    # 从环境变量读取配置
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL", "http://localhost:8080/v1")
    model = os.environ.get("LLM_MODEL", "qwen2.5-coder-14b-instruct")

#    if not api_key:
#        return "[错误] 未配置 LLM API Key（设置 OPENROUTER_API_KEY 或 DEEPSEEK_API_KEY）"

    # 读取 agent_preamble 作为系统提示
    preamble_path = os.path.join(os.path.dirname(__file__), "backend_api_python", "app", "agent", "agent_preamble.md")
    system_prompt = ""
    if os.path.exists(preamble_path):
        system_prompt = open(preamble_path, encoding="utf-8").read()

    # 模拟 call_skill 工具定义
    tools = [
        {
            "type": "function",
            "function": {
                "name": "call_skill",
                "description": "调用分析技能，自动执行一整套分析流程",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "enum": ["technical_agent", "indicator_agent", "intelligence_agent", "market_data_agent"]},
                        "stock_code": {"type": "string", "description": "6位股票代码"},
                        "stock_name": {"type": "string", "description": "股票名称"},
                    },
                    "required": ["skill_name", "stock_code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_stock_by_name",
                "description": "根据名称搜索股票代码",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string"},
                    },
                    "required": ["keyword"],
                },
            },
        },
    ]

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": enriched_message},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_tokens": 2000,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]


# ═══════════════════════════════════════════════════════════════
# 6. 主流程
# ═══════════════════════════════════════════════════════════════

def run_mock_flow(message: str):
    """Mock 测试流程（不连 LLM）。"""
    print(f"\n{'='*60}")
    print(f"📨 用户消息: {message}")
    print(f"{'='*60}")

    # Step 1: 意图分析
    intent = analyze_intent(message)
    print(f"\n[Step 1] 意图分析:")
    print(f"  domain: {intent['domain']}")
    print(f"  intent: {intent['intent']}")
    print(f"  stock_code: {intent.get('stock_code', '-')}")
    print(f"  stock_name: {intent.get('stock_name', '-')}")
    print(f"  confidence: {intent['confidence']}")

    # Step 2: 构建 context
    stock_code = intent.get("stock_code", "")
    context = {}
    if stock_code:
        context["stock_code"] = stock_code
        context["stock_name"] = intent.get("stock_name", "")
        if stock_code in MOCK_QUOTE:
            context["realtime_quote"] = MOCK_QUOTE[stock_code]
    print(f"\n[Step 2] Context 构建:")
    print(f"  {json.dumps(context, ensure_ascii=False, indent=2)}")

    # Step 3: 消息增强
    enriched = enrich_message(message, context, intent)
    print(f"\n[Step 3] 增强后的消息（发给 LLM）:")
    print(f"  {'─'*50}")
    for line in enriched.split("\n"):
        print(f"  {line}")
    print(f"  {'─'*50}")

    # Step 4: 模拟 LLM 决策（检查增强消息中是否包含 call_skill 指令）
    print(f"\n[Step 4] LLM 收到的指令分析:")
    if "call_skill" in enriched:
        skills = re.findall(r'call_skill\(skill_name="(\w+)"', enriched)
        print(f"  ✅ 检测到 call_skill 指令: {skills}")
        print(f"  ✅ LLM 应该会调用 {len(skills)} 次 call_skill")
    else:
        print(f"  ⚠️ 未检测到 call_skill 指令，LLM 可能只调底层工具")

    # Step 5: 模拟 call_skill 执行
    if stock_code:
        print(f"\n[Step 5] 模拟 call_skill 执行:")
        reports = []
        for skill in ["technical_agent", "indicator_agent", "intelligence_agent"]:
            report = mock_call_skill(skill, stock_code)
            reports.append(report)
            print(f"  📊 {skill}:")
            print(f"     direction={report.get('direction')}, score={report.get('score')}, confidence={report.get('confidence')}")
            for f in report.get("factors", []):
                print(f"     - {f['name']}: {f['value']} ({f['score']}分)")

        # Step 6: 模拟汇总
        print(f"\n[Step 6] 模拟汇总报告:")
        avg_score = sum(r.get("score", 0) for r in reports) / len(reports)
        bullish_count = sum(1 for r in reports if r.get("direction") == "bullish")
        action = "buy" if avg_score >= 70 and bullish_count >= 2 else "hold" if avg_score >= 50 else "watch"
        print(f"  综合评分: {avg_score:.0f}/100")
        print(f"  看多信号: {bullish_count}/{len(reports)}")
        print(f"  建议操作: {action}")
    else:
        print(f"\n[Step 5] 未识别到股票代码，跳过 call_skill")


def run_llm_flow(message: str):
    """真实 LLM 测试流程。"""
    print(f"\n{'='*60}")
    print(f"📨 用户消息: {message}")
    print(f"{'='*60}")

    # Step 1-3 同上
    intent = analyze_intent(message)
    stock_code = intent.get("stock_code", "")
    context = {}
    if stock_code:
        context["stock_code"] = stock_code
        context["stock_name"] = intent.get("stock_name", "")
        if stock_code in MOCK_QUOTE:
            context["realtime_quote"] = MOCK_QUOTE[stock_code]

    enriched = enrich_message(message, context, intent)
    print(f"\n[增强消息] 发给 LLM:")
    for line in enriched.split("\n"):
        print(f"  {line}")

    # Step 4: 调用 LLM
    print(f"\n[LLM] 调用中...")
    try:
        response = call_llm(enriched)
        print(f"\n[LLM 响应]:")

        msg = response
        # 处理 tool_calls
        if isinstance(msg, dict) and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                args = json.loads(fn["arguments"])
                print(f"  🔧 调用工具: {fn['name']}({json.dumps(args, ensure_ascii=False)})")

                # 模拟执行 call_skill
                if fn["name"] == "call_skill":
                    skill_name = args.get("skill_name", "")
                    sc = args.get("stock_code", stock_code)
                    report = mock_call_skill(skill_name, sc)
                    print(f"     → 返回: direction={report.get('direction')}, score={report.get('score')}")

            print(f"\n  ✅ LLM 正确使用了 call_skill！")
        elif isinstance(msg, dict) and msg.get("content"):
            print(f"  {msg['content'][:500]}")
        else:
            print(f"  {json.dumps(msg, ensure_ascii=False)[:500]}")

    except Exception as e:
        print(f"\n  ❌ LLM 调用失败: {e}")


def main():
    use_llm = "--llm" in sys.argv

    print("╔══════════════════════════════════════════════════════════╗")
    print("║        QuantDinger Agent 交互测试                       ║")
    print("║  模式: " + ("真实 LLM (需要 API Key)" if use_llm else "Mock (不连 LLM)") + " " * (30 - len("真实 LLM" if use_llm else "Mock")) + "║")
    print("╚══════════════════════════════════════════════════════════╝")

    # 预设测试用例
    test_cases = [
        "分析大连圣亚股票",
        "贵州茅台怎么样",
        "比亚迪能买吗",
        "帮我看看300750",
        "你好",
    ]

    if use_llm:
        print("\n[测试] 使用真实 LLM 测试 call_skill 流程")
        for msg in test_cases[:3]:  # LLM 模式只测前3个
            run_llm_flow(msg)
            print()
    else:
        print("\n[测试] Mock 模式，展示完整数据流")
        for msg in test_cases:
            run_mock_flow(msg)

    # 交互模式
    print(f"\n{'='*60}")
    print("进入交互模式（输入 q 退出，输入消息测试）")
    print(f"{'='*60}")
    while True:
        try:
            msg = input("\n> ").strip()
            if msg.lower() in ("q", "quit", "exit"):
                break
            if not msg:
                continue
            if use_llm:
                run_llm_flow(msg)
            else:
                run_mock_flow(msg)
        except (KeyboardInterrupt, EOFError):
            break

    print("\n👋 测试结束")


if __name__ == "__main__":
    main()
