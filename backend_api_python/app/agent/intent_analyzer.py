# -*- coding: utf-8 -*-

"""
Intent Analyzer — LLM 意图分类 + 上下文压缩（v4，精简版）。

架构（v4 变更）：
  1. 快速通道 — 正则匹配闲聊（<1ms，零开销）
  2. LLM 分类 — 单次调用，同时完成：
     - 意图分类（domain/verb/noun/intent）
     - 股票代码提取
     - 上下文压缩（合并 context_compressor）

已移除：
  - SemanticIntentRouter（embedding 语义路由）
  - VerbNounRouter（正则硬编码）
  - ContextCompressor（合并到 LLM 调用中）
  - routes.py / utterances（不再需要）
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """意图分析结果。"""
    domain: str = "chat"
    intent: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw_response: str = ""
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    tool_categories: List[str] = field(default_factory=list)
    verb: str = ""
    noun: str = ""
    # v4 新增：上下文压缩摘要
    context_summary: str = ""

    @property
    def domain_config(self):
        from app.agent.domain_registry import get_domain
        return get_domain(self.domain)

    @property
    def tool_filter(self) -> Optional[List[str]]:
        dc = self.domain_config
        return dc.tools if dc else None

    @property
    def domain_instructions(self) -> str:
        dc = self.domain_config
        return dc.instructions if dc else ""


# ═══════════════════════════════════════════════════════════════
# 快速通道
# ═══════════════════════════════════════════════════════════════

_PUNCT_TAIL = r'[\s\?\?\.\,\!\~\。\，\！\？\…]*'
_GREETING_RE = re.compile(r'^(你好|hi|hello|嗨|hey|在吗|哈喽|嘿|yo)' + _PUNCT_TAIL + '$', re.IGNORECASE)
_FAREWELL_RE = re.compile(r'^(再见|拜拜|bye|88|886|晚安|回见)' + _PUNCT_TAIL + '$', re.IGNORECASE)
_THANKS_RE  = re.compile(r'^(谢谢|感谢|多谢|thanks|thank\s*you|thx|3q)' + _PUNCT_TAIL + '$', re.IGNORECASE)


def _quick_intent_check(message: str) -> Optional[IntentResult]:
    """极低成本的正则快速匹配。"""
    msg = message.strip()
    if not msg:
        return IntentResult(domain="chat", intent="empty", confidence=1.0, source="quick")
    if re.match(r'^[\s\.\,\!\?\~\。\，\！\？\…]+$', msg):
        return IntentResult(domain="chat", intent="empty", confidence=1.0, source="quick")
    if len(msg) <= 10 and _GREETING_RE.match(msg):
        return IntentResult(domain="chat", intent="greeting", confidence=1.0, source="quick")
    if len(msg) <= 10 and _FAREWELL_RE.match(msg):
        return IntentResult(domain="chat", intent="farewell", confidence=1.0, source="quick")
    if len(msg) <= 15 and _THANKS_RE.match(msg):
        return IntentResult(domain="chat", intent="thanks", confidence=1.0, source="quick")
    return None


# ═══════════════════════════════════════════════════════════════
# LLM 意图分类 + 上下文压缩
# ═══════════════════════════════════════════════════════════════

_INTENT_PROMPT = """你是意图分类器。分析用户消息，输出 JSON。

## 用户消息
{message}

## 上轮对话摘要（如有）
{context_summary}

## 输出格式（只输出 JSON，不要其他内容）
```json
{{
  "domain": "finance | coding | chat",
  "intent": "stock_analysis | chart_view | market_scan | screener | backtest | fund_flow | indicator | trading | stock_info | concept_explain | code_modify | code_create | project_scan | general",
  "verb": "analyze | view | filter | backtest | execute | query | explain | modify | create",
  "noun": "stock | chart | market | screener | fund_flow | indicator | trading | concept | code | project",
  "stock_code": "6位代码或空",
  "stock_name": "股票名称或空",
  "confidence": 0.0-1.0,
  "context_summary": "本轮对话摘要，30字以内，用于下轮上下文。如果和上轮同话题则延续，否则重写。"
}}
```

## 规则
- domain: finance=金融分析/股票/行情/资金, coding=代码/项目/开发, chat=闲聊/问候
- 有股票名称或代码 → domain=finance, verb=analyze, noun=stock
- 用户说"怎么样/能买吗/跌了/涨了"等，且提到股票 → finance/stock_analysis
- 用户问K线/图表 → finance/chart_view
- 用户问涨停/大盘/板块 → finance/market_scan
- 用户要选股/推荐 → finance/screener
- 用户要回测 → finance/backtest
- 用户问资金流向/主力/北向 → finance/fund_flow
- 用户问MACD/RSI/指标 → finance/indicator
- 用户要买入/卖出/持仓 → finance/trading
- 用户问市盈率/市值/基本面 → finance/stock_info
- 用户问概念/术语 → finance/concept_explain
- 纯闲聊/问候 → domain=chat
- confidence: 有明确股票信号=0.9+, 有金融关键词=0.7+, 不确定=0.5-
- context_summary: 压缩为一句话摘要，供下轮对话使用
"""

# 意图 → tool_categories 映射
_INTENT_TOOL_CATEGORIES = {
    "stock_analysis": ["名称查询", "行情数据", "技术分析", "情报搜索"],
    "chart_view": ["名称查询", "行情数据", "K线图表"],
    "market_scan": ["行情数据", "龙虎榜/热榜"],
    "screener": ["名称查询", "选股", "指标策略"],
    "backtest": ["名称查询", "行情数据", "回测", "指标策略"],
    "fund_flow": ["名称查询", "行情数据"],
    "indicator": ["名称查询", "行情数据", "技术分析", "指标策略"],
    "trading": ["交易", "指标策略"],
    "stock_info": ["名称查询", "行情数据"],
    "concept_explain": [],
    "code_modify": ["工作区"],
    "code_create": ["工作区"],
    "project_scan": [],
}


def _call_llm_for_intent(
    message: str,
    context_summary: str = "",
    model: str = None,
    provider: str = None,
) -> Dict[str, Any]:
    """单次 LLM 调用，完成意图分类 + 上下文压缩。"""
    from app.services.llm import LLMService
    import requests

    svc = LLMService(provider=provider)
    api_key = svc.get_api_key()
    base_url = svc.get_base_url()
    model_id = model or svc.get_default_model()

    prompt = _INTENT_PROMPT.format(
        message=message.strip(),
        context_summary=context_summary or "（无）",
    )

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 300,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()

    logger.info("[Intent] LLM 原始输出 (%d 字): %s", len(raw), raw[:500])

    if not raw:
        logger.warning("[Intent] LLM 返回空内容，降级为 chat")
        return {"domain": "chat", "intent": "llm_empty", "verb": "", "noun": "",
                "stock_code": "", "stock_name": "", "confidence": 0.3,
                "context_summary": ""}

    # 清理输出
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    return _parse_intent_json(raw)


def _parse_intent_json(raw: str) -> Dict[str, Any]:
    """从 LLM 输出中提取 JSON。容错处理。"""
    # 提取 JSON 块
    patterns = [
        r'```json\s*\n?(.*?)\n?\s*```',
        r'```\s*\n?(.*?)\n?\s*```',
        r'(\{[^{}]*"domain"[^{}]*\})',
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1).strip())
                if isinstance(data, dict) and "domain" in data:
                    return _validate_intent(data)
            except (json.JSONDecodeError, TypeError):
                continue

    # 完全解析失败 → 默认 chat
    logger.warning("[Intent] LLM 输出解析失败: %s", raw[:200])
    return {"domain": "chat", "intent": "general", "verb": "", "noun": "",
            "stock_code": "", "stock_name": "", "confidence": 0.3,
            "context_summary": ""}


def _validate_intent(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验并修正 LLM 输出的字段。"""
    valid_domains = {"finance", "coding", "chat"}
    if data.get("domain") not in valid_domains:
        data["domain"] = "chat"

    # 校验 confidence
    conf = data.get("confidence", 0.5)
    if not isinstance(conf, (int, float)):
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.5
    data["confidence"] = max(0.0, min(1.0, conf))

    # 确保字符串字段
    for key in ("intent", "verb", "noun", "stock_code", "stock_name", "context_summary"):
        data[key] = str(data.get(key, "") or "")

    return data


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def analyze_intent(
    message: str,
    model: str = None,
    provider: str = None,
    history: List[Dict[str, str]] = None,
    session_id: str = None,
) -> IntentResult:
    """分析用户消息的意图。

    流程（v4）：
    1. 快速通道 — 闲聊正则（<1ms）
    2. LLM 分类 — 单次调用，意图 + 上下文压缩

    Args:
        message: 用户消息
        model: LLM 模型名
        provider: LLM provider
        history: 对话历史（未使用，保留接口兼容）
        session_id: 会话 ID

    Returns:
        IntentResult
    """
    from app.agent.domain_registry import init_builtin_domains
    init_builtin_domains()

    if not message or not message.strip():
        return IntentResult(domain="chat", intent="empty", confidence=1.0, source="quick")

    # ── Level 1: 快速通道 ──────────────────────────────────────
    quick = _quick_intent_check(message)
    if quick:
        logger.info("[Intent] 快速通道: %s/%s", quick.domain, quick.intent)
        return quick

    # ── Level 2: LLM 意图分类 + 上下文压缩 ────────────────────
    # 获取上轮摘要
    context_summary = ""
    if session_id:
        try:
            from app.agent.session_store import get_session_store
            store = get_session_store()
            context_summary, _ = store.get_context_summary(session_id)
        except Exception:
            pass

    # 单次 LLM 调用
    try:
        result = _call_llm_for_intent(
            message=message,
            context_summary=context_summary,
            model=model,
            provider=provider,
        )
    except Exception as e:
        logger.warning("[Intent] LLM 分类失败: %s, 降级为 chat", e)
        return IntentResult(domain="chat", intent="llm_error", confidence=0.0, source="llm_error")

    domain = result.get("domain", "chat")
    intent = result.get("intent", "general")
    verb = result.get("verb", "")
    noun = result.get("noun", "")
    stock_code = result.get("stock_code", "")
    stock_name = result.get("stock_name", "")
    confidence = result.get("confidence", 0.5)
    new_summary = result.get("context_summary", "")

    # 构造 params
    params = {}
    if stock_code:
        params["stock"] = stock_code
    if stock_name:
        params["stock_name"] = stock_name

    # 如果 LLM 没提取到股票代码，尝试 text_utils 兜底
    if domain == "finance" and not stock_code:
        from app.agent.text_utils import extract_stock_from_message
        code, name = extract_stock_from_message(message)
        if code:
            params["stock"] = code
            stock_code = code
        if name:
            params["stock_name"] = name
            stock_name = name

    # tool_categories
    tool_cats = _INTENT_TOOL_CATEGORIES.get(intent, [])

    # tool_chain
    tool_chain = []
    if verb and noun:
        try:
            from app.agent.router.tool_chains import get_tool_chain
            tool_chain = get_tool_chain(verb, noun)
        except Exception:
            pass

    # 合并 metadata
    metadata = {
        "domain": domain, "intent": intent,
        "verb": verb, "noun": noun,
        "tool_categories": tool_cats,
        "tool_chain": tool_chain,
    }

    # 保存摘要到 session store
    if session_id and new_summary:
        try:
            from app.agent.session_store import get_session_store
            store = get_session_store()
            store.save_context_summary(session_id, new_summary, domain=domain)
        except Exception:
            pass

    logger.info(
        "[Intent] LLM 分类: %s/%s (%.2f) verb=%s noun=%s stock=%s | %s",
        domain, intent, confidence, verb, noun,
        stock_code or stock_name or "-",
        message[:50],
    )

    return IntentResult(
        domain=domain,
        intent=intent,
        params=params,
        confidence=confidence,
        source="llm",
        metadata=metadata,
        tool_categories=tool_cats,
        verb=verb,
        noun=noun,
        context_summary=new_summary,
    )


# ═══════════════════════════════════════════════════════════════
# 保留 format_intent_for_agent（agent.py 调用）
# ═══════════════════════════════════════════════════════════════

def format_intent_for_agent(intent: IntentResult, original_message: str) -> str:
    """将意图分析结果格式化为 agent 可用的上下文。"""
    if intent.domain == "chat" and intent.confidence < 0.5:
        return ""
    if intent.domain == "chat":
        return f"[意图] {intent.intent}（直接回复即可，无需调用工具）"

    parts = [f"[意图] domain={intent.domain}, intent={intent.intent}"]
    if intent.verb or intent.noun:
        parts.append(f"[动作-对象] verb={intent.verb or '-'}, noun={intent.noun or '-'}")
    if intent.params:
        parts.append(f"[参数] {json.dumps(intent.params, ensure_ascii=False)}")
    if intent.tool_categories:
        parts.append(f"[工具分类] {', '.join(intent.tool_categories)}")

    tool_chain = intent.metadata.get("tool_chain", [])
    if tool_chain:
        parts.append("[工具链] 建议执行步骤（按优先级，遇到失败可自行调整）:")
        for i, step in enumerate(tool_chain, 1):
            tool = step['tool']
            desc = step.get('desc', '')
            args = step.get('args', {})
            if tool == 'call_skill' and 'skill_name' in args:
                parts.append(f"  {i}. call_{args['skill_name']}(task=\"{desc}\") — {desc}")
            else:
                parts.append(f"  {i}. {tool} — {desc}")
        parts.append("  以上为建议顺序，可自行调整。")

    if intent.confidence < 0.6:
        parts.append(f"⚠️ 置信度较低({intent.confidence:.2f})，请结合原始消息判断")
    return "\n".join(parts)
