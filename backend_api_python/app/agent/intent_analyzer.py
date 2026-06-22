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
    # §15 新增：执行策略（替代 domain 做路由决策）
    strategy: str = "direct"  # "traced" / "chain" / "direct"

    @property
    def domain_config(self):
        """Phase 3: domain_registry 已移除，返回 None。用 domain_instructions 替代。"""
        return None

    @property
    def tool_filter(self) -> Optional[List[str]]:
        """Phase 3: 工具过滤改用 tags，不再依赖 domain_config。"""
        return None

    @property
    def domain_instructions(self) -> str:
        """Phase 3: 从 persona.md 的 behaviors 生成通用指令（领域特定指令在 SKILL.md body 中）。"""
        try:
            from app.agent.semantics import get_persona
            persona = get_persona()
            if not persona or not persona.behaviors:
                return ""
            parts = []
            # 按当前 domain 选择相关的行为规范
            _domain_behaviors = {
                "finance": ["workflow", "safety", "iteration", "finance"],
                "trading": ["workflow", "safety", "trading"],
                "coding": ["workflow", "coding"],
                "system": ["workflow", "system"],
            }
            keys = _domain_behaviors.get(self.domain, ["workflow"])
            for key in keys:
                items = persona.behaviors.get(key, [])
                if items:
                    parts.append(f"## {key}")
                    for item in items:
                        parts.append(f"- {item}")
            return "\n".join(parts)
        except Exception:
            return ""


# ═══════════════════════════════════════════════════════════════
# 快速通道
# ═══════════════════════════════════════════════════════════════

# ── 快速通道正则（从 intent.md 加载，改正则只改 YAML）──
_PUNCT_TAIL = r'[\s\?\?\.\,\!\~\。\，\！\？\…]*'
_GREETING_RE = None
_FAREWELL_RE = None
_THANKS_RE = None

def _ensure_quick_patterns():
    """从 intent.md 加载快速通道正则。"""
    global _GREETING_RE, _FAREWELL_RE, _THANKS_RE
    if _GREETING_RE is not None:
        return
    _ensure_intent_loaded()
    from app.agent.semantics import get_intent_meta
    meta = get_intent_meta()
    if meta is None:
        # intent.md 不存在或解析失败，用硬编码兜底
        _GREETING_RE = re.compile(r'^(你好|hi|hello|嗨|hey|在吗|哈喽|嘿|yo)' + _PUNCT_TAIL + '$', re.IGNORECASE)
        _FAREWELL_RE = re.compile(r'^(再见|拜拜|bye|88|886|晚安|回见)' + _PUNCT_TAIL + '$', re.IGNORECASE)
        _THANKS_RE = re.compile(r'^(谢谢|感谢|多谢|thanks|thank\s*you|thx|3q)' + _PUNCT_TAIL + '$', re.IGNORECASE)
        return
    patterns = meta.quick_patterns
    _GREETING_RE = re.compile(patterns.get("greeting", r'^NEVER_MATCH$'), re.IGNORECASE)
    _FAREWELL_RE = re.compile(patterns.get("farewell", r'^NEVER_MATCH$'), re.IGNORECASE)
    _THANKS_RE = re.compile(patterns.get("thanks", r'^NEVER_MATCH$'), re.IGNORECASE)


def _quick_intent_check(message: str) -> Optional[IntentResult]:
    """极低成本的正则快速匹配。"""
    _ensure_quick_patterns()
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

# ── 从 intent.md 加载（单一信源，改规则只改 YAML）──
def _load_intent_config():
    """从 semantics/intent.md 加载 prompt 和映射。"""
    from app.agent.semantics import get_intent_meta
    meta = get_intent_meta()
    if meta is None:
        logger.warning("[Intent] get_intent_meta() 返回 None，使用空默认值")
        return "", {}
    return meta.classifier_prompt, meta.intent_tool_categories

# 懒加载：首次调用 analyze_intent 时才加载
_INTENT_PROMPT = ""
_INTENT_TOOL_CATEGORIES = {}

def _ensure_intent_loaded():
    global _INTENT_PROMPT, _INTENT_TOOL_CATEGORIES
    if not _INTENT_PROMPT:
        _INTENT_PROMPT, _INTENT_TOOL_CATEGORIES = _load_intent_config()


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
    valid_domains = {"finance", "coding", "trading", "system", "unknown", "chat"}
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
    _ensure_intent_loaded()

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
            from app.agent.chain.tool_chains import get_tool_chain
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

    # §15 计算执行策略（替代 domain 做路由决策）
    # traced: 金融领域，走 TraceCollector + EvalNode 树
    # chain:  有固定链路匹配，走 _execute_plan()
    # direct: 其他，走 agent 自由推理
    if domain == "finance":
        strategy = "traced"
    elif domain == "trading":
        strategy = "traced"  # 交易域也走追踪
    else:
        strategy = "direct"

    logger.info(
        "[Intent] LLM 分类: %s/%s (%.2f) verb=%s noun=%s stock=%s strategy=%s | %s",
        domain, intent, confidence, verb, noun,
        stock_code or stock_name or "-",
        strategy,
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
        strategy=strategy,
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
        # 获取链路统计
        try:
            from app.agent.chain.tool_chains import get_chain_stats
            _stats = get_chain_stats(intent.verb or "", intent.noun or "")
        except Exception:
            _stats = {}

        parts.append("[工具链] 建议执行步骤（按优先级，遇到失败可自行调整）:")
        for i, step in enumerate(tool_chain, 1):
            tool = step['tool']
            desc = step.get('desc', '')
            args = step.get('args', {})
            # call_skill 已移除：新架构走 exec python run.py
            if False:
                pass
            else:
                parts.append(f"  {i}. {tool} — {desc}")

        # 注入统计参考
        if _stats.get("executions", 0) > 0:
            parts.append(
                f"  [统计] 历史平均 {_stats['avg_steps']:.1f} 步, "
                f"成功率 {_stats['success_rate']:.0%}, "
                f"执行 {_stats['executions']} 次"
            )
        parts.append("  以上为建议顺序，可自行调整。")

    if intent.confidence < 0.6:
        parts.append(f"⚠️ 置信度较低({intent.confidence:.2f})，请结合原始消息判断")

    # 选股/推荐类意图：显式提醒正确 skill，防止 agent 乱调 technical_agent
    _screening_verbs = {"recommend", "screen", "select", "scan", "filter", "pick"}
    _screening_nouns = {"stock", "short_term", "target", "candidate", "buy"}
    if (intent.verb in _screening_verbs or intent.noun in _screening_nouns
            or "选股" in original_message or "买什么" in original_message
            or "推荐" in original_message):
        if not intent.params.get("stock"):  # 没给具体股票代码
            parts.append("⚠️ 用户未指定股票代码，这是选股/推荐场景。你必须使用 market-screener（短线选股）或 bb_screener（BB超卖扫描），禁止用 technical_agent 分析任意股票。")

    return "\n".join(parts)
