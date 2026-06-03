# -*- coding: utf-8 -*-

"""
Intent Analyzer — Ragent 风格的打分式意图分类。

设计：
- 从 domain_registry 加载所有场景（扁平化叶子节点）
- LLM 对每个场景打分（选择题，不是填空题）
- 过滤低分、取最高分
- 支持上下文（对话历史）辅助判断
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.agent.domain_registry import (
    get_domain,
    get_all_domains,
    init_builtin_domains,
)

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """意图分析结果。"""
    domain: str = "chat"
    intent: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw_response: str = ""

    @property
    def domain_config(self):
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
# 场景目录（扁平化，供 LLM 打分）
# ═══════════════════════════════════════════════════════════════

def _build_scene_list() -> List[Dict[str, str]]:
    """从 domain_registry 提取所有场景，生成扁平列表供 LLM 打分。

    每个场景 = 一个可路由的意图单元，包含 id、domain、intent、description。
    """
    scenes = []
    idx = 1

    # finance 领域场景
    finance_scenes = [
        ("stock_analysis", "股票分析", "个股分析、技术面分析、行情研判、趋势判断、综合诊断"),
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
        scenes.append({
            "id": str(idx),
            "domain": "finance",
            "intent": intent,
            "name": name,
            "description": desc,
        })
        idx += 1

    # coding 领域场景
    coding_scenes = [
        ("code_modify", "代码修改", "修改代码、修复bug、重构优化"),
        ("code_review", "代码审查", "审查代码质量、分析潜在问题、性能评估"),
        ("code_create", "代码创建", "编写新代码、创建新文件、生成脚本"),
        ("code_debug", "调试排查", "排查错误、定位问题、调试代码"),
        ("project_scan", "项目分析", "项目结构分析、文件梳理、依赖关系"),
    ]
    for intent, name, desc in coding_scenes:
        scenes.append({
            "id": str(idx),
            "domain": "coding",
            "intent": intent,
            "name": name,
            "description": desc,
        })
        idx += 1

    # chat 领域场景
    scenes.append({
        "id": str(idx),
        "domain": "chat",
        "intent": "chat",
        "name": "闲聊",
        "description": "问候、寒暄、感谢、告别、通用对话",
    })

    return scenes


# ═══════════════════════════════════════════════════════════════
# Prompt 模板（Ragent 风格：选择题 + 打分）
# ═══════════════════════════════════════════════════════════════

_INTENT_PROMPT = """# Role
你是一个意图分类器。根据用户输入，对每个分类打分。

# 分类列表
{scene_list}

# Rules
1. 对每个分类给出 0.0~1.0 的匹配分数
2. 高度匹配 ≥ 0.7，中度匹配 0.4~0.7，低度匹配 < 0.4
3. 只输出 JSON 数组，不要任何其他文字
4. 每个元素包含 id 和 score

# Output Format
[{{"id": "1", "score": 0.95}}, {{"id": "2", "score": 0.1}}, ...]

# Conversation History
{history}

# User Input
{message}"""


# ═══════════════════════════════════════════════════════════════
# 分析器
# ═══════════════════════════════════════════════════════════════

# 阈值配置
MIN_SCORE = 0.35      # 低于此分数的分类被过滤
MAX_RESULTS = 3       # 最多保留的分类数

# 场景列表缓存
_scenes_cache: List[Dict[str, str]] = []


def _get_scenes() -> List[Dict[str, str]]:
    global _scenes_cache
    if not _scenes_cache:
        _scenes_cache = _build_scene_list()
    return _scenes_cache


def analyze_intent(
    message: str,
    model: str = None,
    provider: str = None,
    history: List[Dict[str, str]] = None,
) -> IntentResult:
    """分析用户消息的意图（Ragent 风格打分模式）。

    流程：
    1. 加载场景列表
    2. 构造 prompt（场景列表 + 对话历史 + 用户输入）
    3. LLM 对每个场景打分
    4. 过滤低分、取最高分
    5. 返回 IntentResult
    """
    print(f"[DEBUG-INTENT] >>> analyze_intent loaded from: {__file__}", flush=True)
    init_builtin_domains()

    if not message or not message.strip():
        return IntentResult(domain="chat", intent="empty", confidence=1.0)

    scenes = _get_scenes()

    # 构造场景列表文本
    scene_lines = []
    for s in scenes:
        scene_lines.append(f"- id={s['id']}，{s['domain']}/{s['name']}，{s['description']}")
    scene_list_text = "\n".join(scene_lines)

    # 格式化对话历史
    history_text = "（无）"
    if history:
        lines = []
        for msg in history[-6:]:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = (msg.get("content") or "")[:200]
            lines.append(f"{role}: {content}")
        if lines:
            history_text = "\n".join(lines)

    prompt = _INTENT_PROMPT.format(
        scene_list=scene_list_text,
        history=history_text,
        message=message.strip(),
    )

    # 调 LLM
    try:
        from app.services.llm import LLMService
        svc = LLMService(provider=provider)
        api_key = svc.get_api_key()
        base_url = svc.get_base_url()
        model_id = model or svc.get_default_model()

        import requests
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # 清理 markdown 包裹
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        logger.debug("[Intent] LLM raw response: %s", raw[:500])

        # 解析 JSON 数组
        scores = _parse_scores(raw, scenes)
        if not scores:
            logger.warning("[Intent] 无法解析打分结果，降级到 chat | raw: %s", raw[:300])
            return IntentResult(domain="chat", intent="parse_error", confidence=0.0, raw_response=raw)

        # 过滤低分、排序
        filtered = [s for s in scores if s["score"] >= MIN_SCORE]
        filtered.sort(key=lambda x: x["score"], reverse=True)
        top = filtered[:MAX_RESULTS]

        if not top:
            logger.info("[Intent] 所有分类分数 < %.2f，归为 chat", MIN_SCORE)
            return IntentResult(domain="chat", intent="low_confidence", confidence=0.0, raw_response=raw)

        # 取最高分
        best = top[0]
        scene = best["scene"]
        confidence = best["score"]

        return IntentResult(
            domain=scene["domain"],
            intent=scene["intent"],
            params=_extract_params(message, scene),
            confidence=confidence,
            raw_response=raw,
        )

    except json.JSONDecodeError as e:
        logger.warning("[Intent] JSON 解析失败: %s | raw: %s", e, raw[:500])
        return IntentResult(domain="chat", intent="parse_error", confidence=0.0, raw_response=raw)
    except Exception as e:
        import traceback
        logger.warning("[Intent] 分析失败，降级到默认: %s\n%s", e, traceback.format_exc())
        return IntentResult(domain="chat", intent="error", confidence=0.0)


def _parse_scores(raw: str, scenes: List[Dict]) -> List[Dict]:
    """解析 LLM 返回的打分 JSON 数组。"""
    # 尝试直接解析
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return _match_scores_to_scenes(arr, scenes)
    except json.JSONDecodeError:
        pass

    # 从文本中提取 JSON 数组
    match = re.search(r'\[.*?\]', raw, re.DOTALL)
    if match:
        try:
            arr = json.loads(match.group())
            if isinstance(arr, list):
                return _match_scores_to_scenes(arr, scenes)
        except json.JSONDecodeError:
            pass

    return []


def _match_scores_to_scenes(arr: List[Dict], scenes: List[Dict]) -> List[Dict]:
    """将 LLM 返回的 [{id, score}] 匹配到场景列表。"""
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


def _extract_params(message: str, scene: Dict) -> Dict[str, Any]:
    """从用户消息中提取基础参数（股票代码等）。

    这是轻量级提取，不调 LLM。复杂参数由 agent 自行处理。
    """
    import re as _re
    params = {}

    # 提取 6 位股票代码
    code_match = _re.search(r'\b(\d{6})\b', message)
    if code_match:
        params["stock"] = code_match.group(1)

    # 提取股票名称关键词（简单匹配）
    name_map = {
        "茅台": "600519", "比亚迪": "002594", "平安": "000001",
        "宁德": "300750", "招商银行": "600036", "中芯": "688981",
    }
    for name, code in name_map.items():
        if name in message:
            params.setdefault("stock", code)
            params["stock_name"] = name
            break

    return params


def format_intent_for_agent(intent: IntentResult, original_message: str) -> str:
    """将意图分析结果格式化为 agent 可用的上下文。"""
    if intent.domain == "chat" and intent.confidence < 0.5:
        return ""

    if intent.domain == "chat":
        return f"[意图] {intent.intent}（直接回复即可，无需调用工具）"

    parts = [f"[意图] domain={intent.domain}, intent={intent.intent}"]
    if intent.params:
        parts.append(f"[参数] {json.dumps(intent.params, ensure_ascii=False)}")
    if intent.confidence < 0.6:
        parts.append(f"⚠️ 置信度较低({intent.confidence})，请结合原始消息判断")
    return "\n".join(parts)
