# -*- coding: utf-8 -*-

"""
Intent Analyzer — 基于语义路由的意图分类（v2）。

架构：
  1. SemanticIntentRouter（本地 embedding + cosine similarity）做首选路由
     - 毫秒级响应，零 API 调用（使用 sentence-transformers 或降级 HashEncoder）
     - 命中 → 直接返回 IntentResult
  2. LLM 打分（原有逻辑）做降级方案
     - 仅在语义路由置信度不足时触发
     - 保留原有场景列表 + LLM 打分的完整流程

上下文管理：
  - ContextManager 跟踪每个 session 的对话历史
  - 话题连续性加成：同一 domain 内的消息获得分数加成
  - 话题切换检测：domain 突变时自动降低旧 domain 权重

多用户支持：
  - session_id 隔离，每个用户独立的上下文状态
  - 线程安全，支持并发访问
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
    # 路由来源（"semantic" | "llm" | "quick"）
    source: str = ""
    # 路由附带的元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 所有路由的得分（调试用）
    all_scores: Dict[str, float] = field(default_factory=dict)
    # 路由耗时（毫秒）
    elapsed_ms: float = 0.0
    # 该意图需要的工具分类（对应 @tool(category=...)）
    tool_categories: List[str] = field(default_factory=list)
    # 动作-对象路由的原始 verb/noun（供评估器使用）
    verb: str = ""
    noun: str = ""

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
# 全局单例：动作-对象路由器 + 语义路由器 + 上下文管理器
# ═══════════════════════════════════════════════════════════════

_verb_noun_router = None
_semantic_router = None
_context_mgr = None


def _get_verb_noun_router():
    """懒加载动作-对象路由器单例（主路由）。"""
    global _verb_noun_router, _semantic_router
    if _verb_noun_router is not None:
        return _verb_noun_router

    from app.agent.router.verb_noun_router import VerbNounRouter

    # 语义路由器作为降级方案
    semantic = _get_semantic_router()
    context_boost = float(os.getenv("INTENT_ROUTER_CONTEXT_BOOST", "0.1"))

    try:
        _verb_noun_router = VerbNounRouter(
            context_boost=context_boost,
        )
        logger.info("[Intent] 动作-对象路由器初始化完成")
    except Exception as e:
        logger.warning("[Intent] 动作-对象路由器初始化失败: %s", e)
        _verb_noun_router = None

    return _verb_noun_router


def _get_semantic_router():
    """懒加载语义路由器单例（降级方案）。"""
    global _semantic_router
    if _semantic_router is not None:
        return _semantic_router

    from app.agent.router.core import SemanticIntentRouter
    from app.agent.router.routes import build_default_routes

    backend = os.getenv("INTENT_ROUTER_ENCODER", "auto")
    threshold = float(os.getenv("INTENT_ROUTER_THRESHOLD", "0.45"))
    context_boost = float(os.getenv("INTENT_ROUTER_CONTEXT_BOOST", "0.1"))

    try:
        routes = build_default_routes()
        _semantic_router = SemanticIntentRouter(
            routes=routes,
            default_threshold=threshold,
            context_boost=context_boost,
            encoder_backend=backend,
        )
        logger.info("[Intent] 语义路由器初始化完成 (encoder=%s, threshold=%.2f)", backend, threshold)
    except Exception as e:
        logger.warning("[Intent] 语义路由器初始化失败: %s，将使用纯 LLM 模式", e)
        _semantic_router = None

    return _semantic_router


def _get_context_manager():
    """懒加载上下文管理器单例。"""
    global _context_mgr
    if _context_mgr is None:
        from app.agent.router.context import ContextManager
        ttl = int(os.getenv("INTENT_CONTEXT_TTL", "3600"))
        _context_mgr = ContextManager(session_ttl=ttl)
    return _context_mgr


# ═══════════════════════════════════════════════════════════════
# 快速通道（不需要 LLM，不需要 router）
# ═══════════════════════════════════════════════════════════════

# 快速通道正则（编译一次，全局复用）
_PUNCT_TAIL = r'[\s\?\?\.\,\!\~\。\，\！\？\…]*'
_GREETING_RE = re.compile(r'^(你好|hi|hello|嗨|hey|在吗|哈喽|嘿|yo)' + _PUNCT_TAIL + '$', re.IGNORECASE)
_FAREWELL_RE = re.compile(r'^(再见|拜拜|bye|88|886|晚安|回见)' + _PUNCT_TAIL + '$', re.IGNORECASE)
_THANKS_RE  = re.compile(r'^(谢谢|感谢|多谢|thanks|thank\s*you|thx|3q)' + _PUNCT_TAIL + '$', re.IGNORECASE)


def _quick_intent_check(message: str) -> Optional[IntentResult]:
    """极低成本的关键词/正则快速匹配。

    用于处理明显的闲聊和空消息，避免任何计算开销。
    """
    msg = message.strip()
    if not msg:
        return IntentResult(domain="chat", intent="empty", confidence=1.0, source="quick")

    # 纯标点/符号
    if re.match(r'^[\s\.\,\!\?\~\。\，\！\？\…]+$', msg):
        return IntentResult(domain="chat", intent="empty", confidence=1.0, source="quick")

    # 极短消息 + 常见问候词（正则匹配，忽略末尾标点和大小写）
    if len(msg) <= 10 and _GREETING_RE.match(msg):
        return IntentResult(domain="chat", intent="greeting", confidence=1.0, source="quick")

    if len(msg) <= 10 and _FAREWELL_RE.match(msg):
        return IntentResult(domain="chat", intent="farewell", confidence=1.0, source="quick")

    if len(msg) <= 15 and _THANKS_RE.match(msg):
        return IntentResult(domain="chat", intent="thanks", confidence=1.0, source="quick")

    return None  # 未命中快速通道


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

    路由策略：
    1. 快速通道 — 关键词匹配（<1ms，零开销）
    2. 语义路由 — embedding 确定领域（finance/coding/chat）
    3. 金融域精排 — verb_noun_router 做 verb+noun 精确匹配

    Args:
        message: 用户消息
        model: LLM 模型名（仅降级时使用）
        provider: LLM provider（仅降级时使用）
        history: 对话历史（仅 LLM 降级时使用）
        session_id: 会话 ID（用于上下文管理）

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

    ctx_mgr = _get_context_manager()
    context_domain = ctx_mgr.get_context_domain(session_id) if session_id else ""

    # ── Level 2: 语义路由 — 先定领域 ───────────────────────────
    semantic = _get_semantic_router()
    semantic_result = None
    if semantic:
        semantic_result = semantic.route(
            query=message,
            session_id=session_id,
            context_domain=context_domain,
        )

    # 语义路由命中 → 确定领域
    if semantic_result and semantic_result.matched:
        domain = semantic_result.domain
        intent = semantic_result.intent
        confidence = semantic_result.confidence
        metadata = semantic_result.metadata
        tool_cats = metadata.get("tool_categories", [])
        logger.info(
            "[Intent] 语义路由命中: %s/%s (%.2f) %.0fms | %s",
            domain, intent, confidence, semantic_result.elapsed_ms, message[:50],
        )
    else:
        domain = "chat"
        intent = "unmatched"
        confidence = 0.0
        metadata = {}
        tool_cats = []
        logger.info("[Intent] 语义路由未命中，走默认 chat | %s", message[:50])

    # ── Level 3: 金融域精排 — verb_noun_router ─────────────────
    # 只有金融域才需要 verb+noun 精确匹配（股票名词 DB 查询等）
    verb = ""
    noun = ""
    params = _extract_params(message)

    if domain == "finance":
        router = _get_verb_noun_router()
        if router:
            result = router.route(
                query=message,
                session_id=session_id,
                context_domain=context_domain,
            )
            if result.matched:
                domain = result.domain
                intent = result.intent
                confidence = result.confidence
                metadata = result.metadata
                tool_cats = result.metadata.get("tool_categories", [])
                verb = result.verb
                noun = result.noun
                params = result.params
                logger.info(
                    "[Intent] 金融精排命中: %s/%s (%.2f) verb=%s noun=%s %.0fms | %s",
                    domain, intent, confidence, verb, noun, result.elapsed_ms, message[:50],
                )

    # 记录到上下文
    if session_id:
        ctx_mgr.record_route(
            session_id=session_id,
            domain=domain,
            intent=intent,
            confidence=confidence,
            query=message,
        )

    return IntentResult(
        domain=domain,
        intent=intent,
        params=params,
        confidence=confidence,
        metadata=metadata,
        source="semantic" if not verb else "verb_noun",
        tool_categories=tool_cats,
        verb=verb,
        noun=noun,
    )


# ═══════════════════════════════════════════════════════════════
# LLM 降级（保留原有逻辑）
# ═══════════════════════════════════════════════════════════════

def _build_scene_list() -> List[Dict[str, str]]:
    """从 domain_registry 提取所有场景，生成扁平列表供 LLM 打分。"""
    scenes = []
    idx = 1

    finance_scenes = [
        ("stock_analysis", "股票分析", "个股分析、技术面分析、行情研判、趋势判断、综合诊断"),
        ("chart_view", "K线图表", "看K线、K线图、蜡烛图、走势图、图表可视化"),
        ("market_scan", "市场扫描", "涨停池、跌停池、龙虎榜、热门板块、市场概览"),
        ("backtest", "策略回测", "策略回测验证、历史绩效分析、收益率胜率回撤"),
        ("stock_screener", "选股筛选", "条件选股、指标选股、智能筛选"),
        ("fund_flow", "资金流向", "主力资金、北向资金、融资融券、板块资金"),
        ("indicator", "指标查询", "技术指标查询、MACD/RSI/KDJ/布林带等指标状态"),
        ("trading", "交易执行", "启动策略、停止策略、查看持仓、交易记录"),
        ("stock_info", "基本面查询", "公司简介、行业分类、市值PE PB ROE"),
        ("concept_explain", "概念解释", "金融概念解释、术语答疑、投资知识问答"),
    ]
    for intent, name, desc in finance_scenes:
        scenes.append({"id": str(idx), "domain": "finance", "intent": intent, "name": name, "description": desc})
        idx += 1

    coding_scenes = [
        ("code_modify", "代码修改", "修改代码、修复bug、重构优化"),
        ("code_review", "代码审查", "审查代码质量、分析潜在问题、性能评估"),
        ("code_create", "代码创建", "编写新代码、创建新文件、生成脚本"),
        ("code_debug", "调试排查", "排查错误、定位问题、调试代码"),
        ("project_scan", "项目分析", "项目结构分析、文件梳理、依赖关系"),
    ]
    for intent, name, desc in coding_scenes:
        scenes.append({"id": str(idx), "domain": "coding", "intent": intent, "name": name, "description": desc})
        idx += 1

    scenes.append({"id": str(idx), "domain": "chat", "intent": "chat", "name": "闲聊", "description": "问候、寒暄、感谢、告别、通用对话"})
    return scenes


_INTENT_PROMPT = """你是意图分类器。根据用户输入，对以下分类打分。

分类列表：
{scene_list}

规则：
- 对每个分类给出 0.0~1.0 的匹配分数
- 高度匹配 ≥ 0.7，中度 0.4~0.7，低度 < 0.4
- 只输出 JSON 数组，不要输出任何其他内容（不要解释、不要思考过程）

输出格式（严格遵守）：
[{{"id": "1", "score": 0.95}}, {{"id": "2", "score": 0.1}}, ...]

对话历史：
{history}

用户输入：
{message}

JSON 数组："""


MIN_SCORE = 0.35
MAX_RESULTS = 3


def _llm_fallback(
    message: str,
    model: str = None,
    provider: str = None,
    history: List[Dict[str, str]] = None,
) -> IntentResult:
    """LLM 打分降级方案（原有逻辑）。"""
    scenes = _build_scene_list()

    scene_lines = [f"- id={s['id']}，{s['domain']}/{s['name']}，{s['description']}" for s in scenes]
    scene_list_text = "\n".join(scene_lines)

    history_text = "（无）"
    if history:
        lines = []
        for msg in history[-6:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = (msg.get("content") or "")[:200]
            lines.append(f"{role}: {content}")
        if lines:
            history_text = "\n".join(lines)

    prompt = _INTENT_PROMPT.format(scene_list=scene_list_text, history=history_text, message=message.strip())

    try:
        from app.services.llm import LLMService
        svc = LLMService(provider=provider)
        api_key = svc.get_api_key()
        base_url = svc.get_base_url()
        model_id = model or svc.get_default_model()

        import requests
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model_id, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 500},
            timeout=120.0,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # 清理：去除 <think> 块、markdown 代码块
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        scores = _parse_scores(raw, scenes)
        if not scores:
            return IntentResult(domain="chat", intent="parse_error", confidence=0.0, raw_response=raw, source="llm")

        filtered = [s for s in scores if s["score"] >= MIN_SCORE]
        filtered.sort(key=lambda x: x["score"], reverse=True)
        top = filtered[:MAX_RESULTS]

        if not top:
            return IntentResult(domain="chat", intent="low_confidence", confidence=0.0, raw_response=raw, source="llm")

        best = top[0]
        scene = best["scene"]
        # LLM 降级时从意图名映射工具分类
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
            "project_scan": ["工作区"],
        }
        return IntentResult(
            domain=scene["domain"],
            intent=scene["intent"],
            params=_extract_params(message, scene),
            confidence=best["score"],
            raw_response=raw,
            source="llm",
            tool_categories=_INTENT_TOOL_CATEGORIES.get(scene["intent"], []),
        )
    except Exception as e:
        logger.warning("[Intent] LLM 降级也失败: %s", e)
        return IntentResult(domain="chat", intent="error", confidence=0.0, source="llm")


def _parse_scores(raw: str, scenes: List[Dict]) -> List[Dict]:
    """解析 LLM 返回的打分 JSON 数组。支持多种格式容错。"""
    # 预处理：去除 <think>...</think> 块、markdown 代码块、多余空白
    cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    cleaned = re.sub(r'```(?:json)?\s*', '', cleaned).strip()
    cleaned = re.sub(r'```\s*$', '', cleaned).strip()

    # 尝试 1: 直接解析清理后的文本
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            return _match_scores_to_scenes(arr, scenes)
    except json.JSONDecodeError:
        pass

    # 尝试 2: 贪婪匹配 [...] （处理前后有多余文本的情况）
    match = re.search(r'\[.*\]', cleaned, re.DOTALL)
    if match:
        try:
            arr = json.loads(match.group())
            if isinstance(arr, list):
                return _match_scores_to_scenes(arr, scenes)
        except json.JSONDecodeError:
            pass

    # 尝试 3: 逐行提取 {"id": ..., "score": ...} 对象
    pairs = re.findall(r'\{[^{}]*"id"\s*:\s*"[^"]*"[^{}]*"score"\s*:\s*[0-9.]+[^{}]*\}', cleaned)
    if not pairs:
        pairs = re.findall(r'\{[^{}]*"score"\s*:\s*[0-9.]+[^{}]*"id"\s*:\s*"[^"]*"[^{}]*\}', cleaned)
    if pairs:
        arr = []
        for p in pairs:
            try:
                arr.append(json.loads(p))
            except json.JSONDecodeError:
                continue
        if arr:
            return _match_scores_to_scenes(arr, scenes)

    return []


def _match_scores_to_scenes(arr: List[Dict], scenes: List[Dict]) -> List[Dict]:
    scene_map = {s["id"]: s for s in scenes}
    results = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", ""))
        score = item.get("score", 0)
        try:
            score = float(score)
        except (ValueError, TypeError):
            score = 0.0
        scene = scene_map.get(sid)
        if scene:
            results.append({"scene": scene, "score": score})
    return results


def _extract_params(message: str, scene: Dict = None) -> Dict[str, Any]:
    """从用户消息中提取基础参数（股票代码等）。"""
    params = {}
    from app.agent.text_utils import extract_stock_from_message
    code, name = extract_stock_from_message(message)
    if code:
        params["stock"] = code
    if name:
        params["stock_name"] = name
    return params


def format_intent_for_agent(intent: IntentResult, original_message: str) -> str:
    """将意图分析结果格式化为 agent 可用的上下文。"""
    if intent.domain == "chat" and intent.confidence < 0.5:
        return ""
    if intent.domain == "chat":
        return f"[意图] {intent.intent}（直接回复即可，无需调用工具）"

    parts = [f"[意图] domain={intent.domain}, intent={intent.intent}"]
    if intent.source:
        parts.append(f"[路由] {intent.source}")
    if intent.verb or intent.noun:
        parts.append(f"[动作-对象] verb={intent.verb or '-'}, noun={intent.noun or '-'}")
    if intent.params:
        parts.append(f"[参数] {json.dumps(intent.params, ensure_ascii=False)}")
    if intent.tool_categories:
        parts.append(f"[工具分类] {', '.join(intent.tool_categories)}")

    # 工具链：建议执行步骤
    tool_chain = intent.metadata.get("tool_chain", [])
    if tool_chain:
        parts.append("[工具链] 建议执行步骤（按优先级，遇到失败可自行调整）:")
        for i, step in enumerate(tool_chain, 1):
            tool = step['tool']
            desc = step.get('desc', '')
            args = step.get('args', {})
            if tool == 'call_skill' and 'skill_name' in args:
                parts.append(f"  {i}. call_skill(skill_name=\"{args['skill_name']}\") — {desc}")
            else:
                parts.append(f"  {i}. {tool} — {desc}")
        parts.append("  以上为建议顺序，可自行调整。")

    if intent.confidence < 0.6:
        parts.append(f"⚠️ 置信度较低({intent.confidence:.2f})，请结合原始消息判断")
    return "\n".join(parts)
