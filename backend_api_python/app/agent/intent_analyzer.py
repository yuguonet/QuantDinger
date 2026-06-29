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
# LLM 意图分类 + 上下文压缩
# ═══════════════════════════════════════════════════════════════

# ── 从 intent.md 加载（单一信源，改规则只改 YAML）──
def _load_intent_config():
    """从 semantics/intent.md 加载 prompt 和映射。"""
    from app.agent.semantics import get_intent_meta
    meta = get_intent_meta()
    if meta is None:
        logger.warning("[Intent] get_intent_meta() 返回 None，使用空默认值")
        return ""
    return meta.classifier_prompt

# 懒加载：首次调用 analyze_intent 时才加载
_INTENT_PROMPT = ""

def _ensure_intent_loaded():
    global _INTENT_PROMPT
    if not _INTENT_PROMPT:
        _INTENT_PROMPT = _load_intent_config()


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
    # 先用共享的 extract_json 尝试
    from app.agent.json_extractor import extract_json
    data = extract_json(raw)
    if data and "domain" in data:
        return _validate_intent(data)

    # 后备：intent 专属的宽松模式（无 json 标记的块、行内 domain 匹配）
    patterns = [
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
    context_summary: str = "",
) -> IntentResult:
    """分析用户消息的意图。

    流程（v4）：
    1. LLM 分类 — 单次调用，意图 + 上下文压缩

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
        return IntentResult(domain="chat", intent="empty", confidence=1.0, source="empty")

    # ── LLM 意图分类 + 上下文压缩 ────────────────────────────
    # 获取上轮摘要（由调用方传入，不再从 session_store 读）
    if not context_summary and session_id:
        context_summary = ""

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
    confidence = result.get("confidence", 0.5)
    new_summary = result.get("context_summary", "")

    # stock_code 不在此处提取，由 prepare_node 走 3 级提取（context → 正则 → 中文名解析）
    stock_code = ""
    stock_name = ""

    # 构造 params
    params = {}

    # 合并 metadata
    metadata = {
        "domain": domain, "intent": intent,
        "verb": verb, "noun": noun,
    }

    # 摘要由调用方（LangGraph checkpointer）管理，不再存 session_store
    if session_id and new_summary:
        pass  # checkpointer 自动持久化

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
        "[Intent] LLM 分类: %s/%s (%.2f) verb=%s noun=%s strategy=%s | %s",
        domain, intent, confidence, verb, noun,
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
        verb=verb,
        noun=noun,
        context_summary=new_summary,
        strategy=strategy,
    )


