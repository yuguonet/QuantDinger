# -*- coding: utf-8 -*-
"""
Planner — LLM 规划器。

职责：当无固定链路匹配时，用轻量 LLM 调用规划 Skill 执行方案。

流程：
  1. 用户 query → 查询缓存（相似 query 复用旧规划）
  2. 缓存未命中 → LLM 规划（只选 Skill，不执行）
  3. 校验规划（步数、必选 Skill、去重）
  4. 存入 tool_chains.json
  5. 返回 ChainDef 供 ChainExecutor 执行

设计原则：
  - LLM 只做选择题（从 15 个 Skill 中选 1~5 个），不做开放题
  - 规划必须可回测（存 query + 选择 + reasoning）
  - 失败降级到默认链路，必须告知用户
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.agent.chain.chains import ChainDef, ChainStep, register_chain

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

# 规划步数限制
MIN_STEPS = 1
MAX_STEPS = 5

# 必选 Skill（技术面是地基）
REQUIRED_SKILLS = {"technical_agent"}

# 默认降级链路
DEFAULT_FALLBACK_SKILLS = ["technical_agent"]

# 缓存相似度阈值（query hash 前缀匹配位数）
CACHE_HASH_PREFIX_LEN = 8

# 规划 TTL（秒）
PLAN_TTL = int(os.getenv("PLAN_TTL", "86400"))  # 24h

# tool_chains.json 路径
_TOOL_CHAINS_PATH = None


def _get_tool_chains_path() -> Path:
    global _TOOL_CHAINS_PATH
    if _TOOL_CHAINS_PATH is None:
        base = Path(__file__).resolve().parent.parent
        _TOOL_CHAINS_PATH = base / "data" / "tool_chains.json"
        _TOOL_CHAINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _TOOL_CHAINS_PATH


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class PlanResult:
    """规划结果。"""
    success: bool = False
    chain_def: Optional[ChainDef] = None
    reasoning: str = ""
    from_cache: bool = False
    degraded: bool = False          # 是否降级
    degrade_reason: str = ""        # 降级原因
    stocks: List[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════
# Skill 描述（给 LLM 看的，精简版）
# ═══════════════════════════════════════════════════════════════

def _get_skill_catalog() -> str:
    """从 agent/skills/*/SKILL.md 动态生成技能目录（单一信源）。"""
    from app.agent.semantics import get_all_skill_metas
    metas = get_all_skill_metas()
    if not metas:
        # fallback: 硬编码兜底（semantics 加载失败时）
        return (
            "可用技能：technical_agent(技术面), intelligence_agent(情报), "
            "market_screener(选股), backtest_agent(回测), "
            "researcher(多空)"
        )
    lines = ["可用技能（从下列中选择 1~5 个，按执行顺序排列）：", ""]
    # 按 priority 降序排列
    sorted_skills = sorted(metas.items(), key=lambda x: x[1].priority, reverse=True)
    for i, (name, meta) in enumerate(sorted_skills, 1):
        desc = meta.description or meta.name
        tags_str = f" [{','.join(meta.tags)}]" if meta.tags else ""
        lines.append(f"{i}. {name} — {desc}{tags_str}")
    lines.append("")
    lines.append("大多数场景必须包含 technical_agent（技术面地基）。")
    return "\n".join(lines)

# 懒加载缓存
_SKILL_CATALOG_CACHE: str = ""

def _ensure_skill_catalog() -> str:
    global _SKILL_CATALOG_CACHE
    if not _SKILL_CATALOG_CACHE:
        _SKILL_CATALOG_CACHE = _get_skill_catalog()
    return _SKILL_CATALOG_CACHE


# ═══════════════════════════════════════════════════════════════
# Planner
# ═══════════════════════════════════════════════════════════════

class Planner:
    """LLM 规划器。"""

    def __init__(self, call_llm: Callable[[str], str] = None):
        self._call_llm = call_llm

    def plan(
        self,
        query: str,
        stock_code: str = "",
        stock_name: str = "",
        verb: str = "",
        noun: str = "",
        context: Dict[str, Any] = None,
        context_summary: str = "",
    ) -> PlanResult:
        """为用户 query 生成执行规划。

        Args:
            query: 用户原始消息
            stock_code: 股票代码（可选）
            stock_name: 股票名称（可选）
            verb: 意图动词（可选）
            noun: 意图对象（可选）
            context: 额外上下文
            context_summary: 对话历史摘要（可选）

        Returns:
            PlanResult
        """
        t0 = time.time()

        # 1. 查询缓存
        cached = self._lookup_cache(query, stock_code=stock_code, verb=verb, noun=noun)
        if cached:
            logger.info("[Planner] 缓存命中: query=%s", query[:30])
            cached.from_cache = True
            cached.elapsed_ms = (time.time() - t0) * 1000
            return cached

        # 2. LLM 规划
        if not self._call_llm:
            return self._degrade("LLM 不可用", t0)

        try:
            plan_data = self._llm_plan(query, stock_code, stock_name, verb, noun, context_summary)
        except Exception as e:
            logger.warning("[Planner] LLM 规划失败: %s", e)
            return self._degrade(f"LLM 规划异常: {e}", t0)

        # 3. 校验
        validated = self._validate(plan_data)
        if validated is not None:
            return self._degrade(f"规划校验失败: {validated}", t0)

        # 4. 构建 ChainDef
        chain_def = self._build_chain_def(plan_data, stock_code)

        # 5. 存储
        self._save_plan(query, plan_data, stock_code)

        elapsed = (time.time() - t0) * 1000
        logger.info("[Planner] 规划完成: %d 步, %.0fms", len(plan_data.get("steps", [])), elapsed)

        return PlanResult(
            success=True,
            chain_def=chain_def,
            reasoning=plan_data.get("reasoning", ""),
            stocks=plan_data.get("stocks", []),
            elapsed_ms=elapsed,
        )

    def _llm_plan(
        self,
        query: str,
        stock_code: str,
        stock_name: str,
        verb: str,
        noun: str,
        context_summary: str = "",
    ) -> Dict[str, Any]:
        """调用 LLM 生成规划。返回原始 JSON dict。

        注入完整上下文：人设 + 对话历史 + 规则 + 全量 skill + 全量 tool
        """
        # 1. 人设
        persona_section = ""
        try:
            from app.agent.semantics import get_persona
            p = get_persona()
            if p:
                persona_section = f"你是{p.role}。{p.identity}"
        except Exception:
            pass

        # 2. 股票 + 意图
        stock_info = ""
        if stock_code:
            stock_info = f"\n股票: {stock_name or '未知'}（{stock_code}）"
        intent_info = ""
        if verb or noun:
            intent_info = f"\n意图: verb={verb or '-'}, noun={noun or '-'}"

        # 3. 全量 skill 摘要
        skills_section = ""
        try:
            from app.agent.semantics import get_skills_summary_xml
            skills_section = get_skills_summary_xml()
        except Exception:
            skills_section = _ensure_skill_catalog()  # fallback

        # 4. 全量 tool 摘要
        tools_section = ""
        try:
            from app.agent.semantics import get_tools_summary_xml
            tools_section = get_tools_summary_xml()
        except Exception:
            pass

        # 5. 规则 + 输出格式
        planner_section = ""
        try:
            from app.agent.semantics import get_planner_text
            planner_section = get_planner_text()
        except Exception:
            pass
        if not planner_section:
            planner_section = (
                "## 规则\n"
                "- 从上述技能中选择 1~5 个，按执行顺序排列\n"
                "- 大多数场景必须包含 technical_agent（技术面地基）\n"
                "- 不要选择与问题无关的技能\n"
                "- 如果涉及股票但未提供代码，在 stocks 中列出需要的代码\n"
            )

        # 6. 对话上下文
        context_section = ""
        if context_summary:
            context_section = f"\n## 对话历史\n{context_summary}\n"

        prompt = (
            f"{persona_section}\n\n"
            "你是量化分析规划器。根据用户问题，制定执行计划。\n\n"
            f"## 用户问题\n{query}{stock_info}{intent_info}\n\n"
            f"{context_section}\n"
            f"## 可用技能\n{skills_section}\n\n"
            f"## 可用工具\n{tools_section}\n\n"
            f"{planner_section}\n"
        )

        raw = self._call_llm(prompt)
        return self._parse_plan_json(raw)

    def _parse_plan_json(self, raw: str) -> Dict[str, Any]:
        """解析 LLM 输出的规划 JSON。容错处理各种 LLM 输出格式。"""
        import re

        # 1. 清理 think 标签
        cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

        # 2. 去掉 <code>...</code> 包装（必须在 final_answer 之前）
        cleaned = re.sub(r'</?code>', '', cleaned).strip()

        # 3. 去掉 final_answer() 包装（qwen-coder 常见格式）
        m = re.search(r'final_answer\s*\(\s*(.+)\s*\)', cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()

        # 3. 去掉 markdown 代码块
        cleaned = re.sub(r'```json\s*', '', cleaned).strip()
        cleaned = re.sub(r'```\s*$', '', cleaned).strip()

        # 4. 尝试直接解析
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and "steps" in data:
                return data
        except json.JSONDecodeError:
            pass

        # 5. 提取最外层 JSON 对象（含 steps）
        match = re.search(r'\{[^{}]*"steps"\s*:\s*\[.*?\][^{}]*\}', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        # 6. 提取任意最外层 {...}
        brace_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group())
                if isinstance(data, dict) and "steps" in data:
                    return data
            except json.JSONDecodeError:
                pass

        raise ValueError(f"无法解析规划 JSON: {raw[:300]}")

    def _validate(self, plan_data: Dict[str, Any]) -> Optional[str]:
        """校验规划。返回 None 表示通过，返回字符串表示失败原因。"""
        steps = plan_data.get("steps", [])
        if not steps:
            return "steps 为空"

        if len(steps) < MIN_STEPS:
            return f"步数不足（{len(steps)} < {MIN_STEPS}）"

        if len(steps) > MAX_STEPS:
            # 截断而非失败
            plan_data["steps"] = steps[:MAX_STEPS]
            logger.info("[Planner] 步数超限，截断到 %d 步", MAX_STEPS)

        # 去重
        seen = set()
        deduped = []
        for step in steps:
            agent = step.get("agent", "")
            if agent and agent not in seen:
                seen.add(agent)
                deduped.append(step)
        plan_data["steps"] = deduped

        # 新架构：从 agent/skills/*/SKILL.md 校验 Skill 是否存在
        from app.agent.semantics import get_all_skill_metas
        known_skills = set(get_all_skill_metas().keys())
        if known_skills:
            selected = {s.get("agent", "") for s in plan_data.get("steps", [])}
            unknown = selected - known_skills - {""}
            if unknown:
                # 移除未知 Skill（LLM 可能幻觉出不存在的 Skill）
                plan_data["steps"] = [
                    s for s in plan_data["steps"]
                    if s.get("agent", "") in known_skills or s.get("agent", "") == ""
                ]
                logger.warning("[Planner] 移除未知 Skill: %s (已知: %s)", unknown, known_skills)
                if not plan_data["steps"]:
                    return "所有 Skill 均未知，无法规划"
        return None  # 通过

    def _build_chain_def(self, plan_data: Dict[str, Any], stock_code: str) -> ChainDef:
        """从规划数据构建 ChainDef。"""
        steps = []
        for i, step_data in enumerate(plan_data.get("steps", []), 1):
            # 兼容 "skill" 和 "agent" 两个字段名
            agent = step_data.get("skill", "") or step_data.get("agent", "")
            steps.append(ChainStep(
                name=agent,
                agent=agent,
                order=i,
                description=step_data.get("description", ""),
                required=(i == 1),
                rules=step_data.get("rules", ""),
            ))

        # 收集 Planner 上下文（关键信息传递给各 Skill Agent）
        planner_context = plan_data.get("context", {})
        # 兼容旧链路 "规则" 字段
        if "规则" in plan_data and "rules" not in planner_context:
            planner_context["rules"] = plan_data["规则"]

        chain_id = f"planned+{hashlib.md5(json.dumps(plan_data, sort_keys=True).encode()).hexdigest()[:8]}"

        chain_def = ChainDef(
            chain_id=chain_id,
            name="LLM 规划链路",
            description=plan_data.get("reasoning", ""),
            steps=steps,
            trigger_verbs=[],
            trigger_nouns=[],
            context=planner_context,
        )
        register_chain(chain_def)  # 动态链路必须注册才能被 ChainExecutor 找到
        return chain_def

    # ── 缓存 ──

    def _query_hash(self, query: str) -> str:
        """生成 query hash。"""
        normalized = query.strip().lower()
        return hashlib.md5(normalized.encode()).hexdigest()

    def _extract_keywords(self, query: str) -> set:
        """从 query 中提取关键词（中文分词 + 英文单词）。"""
        import re
        # 英文单词
        en_words = set(re.findall(r'[a-zA-Z_]+', query.lower()))
        # 中文 2~4 字词组（简单滑窗）
        cn_chars = re.findall(r'[\u4e00-\u9fff]+', query)
        cn_words = set()
        for seg in cn_chars:
            for n in (2, 3, 4):
                for i in range(len(seg) - n + 1):
                    cn_words.add(seg[i:i+n])
        # 去停用词
        stopwords = {"的", "了", "吗", "吧", "呢", "啊", "是", "在", "有", "和",
                     "帮我", "一下", "看看", "怎么", "什么", "如何", "请", "想", "要"}
        return (en_words | cn_words) - stopwords

    def _similarity(self, query1: str, query2: str, stock1: str = "", stock2: str = "",
                    verb1: str = "", noun1: str = "", verb2: str = "", noun2: str = "") -> float:
        """计算两个 query 的相似度（0~1）。

        多维度加权：
          - 关键词 Jaccard 相似度（0.4 权重）
          - 股票代码匹配（0.3 权重）
          - verb+noun 匹配（0.3 权重）
        """
        score = 0.0

        # 1. 关键词相似度（Jaccard）
        kw1 = self._extract_keywords(query1)
        kw2 = self._extract_keywords(query2)
        if kw1 and kw2:
            intersection = kw1 & kw2
            union = kw1 | kw2
            kw_sim = len(intersection) / len(union) if union else 0
            score += kw_sim * 0.4

        # 2. 股票代码匹配
        if stock1 and stock2:
            if stock1 == stock2:
                score += 0.3
            # 同板块前缀匹配（如 600xxx）
            elif stock1[:3] == stock2[:3]:
                score += 0.1
        elif not stock1 and not stock2:
            score += 0.15  # 都没股票代码，部分加分

        # 3. verb+noun 匹配
        if verb1 and verb2:
            if verb1 == verb2:
                score += 0.15
        if noun1 and noun2:
            if noun1 == noun2:
                score += 0.15

        return round(score, 3)

    def _lookup_cache(self, query: str, stock_code: str = "",
                      verb: str = "", noun: str = "") -> Optional[PlanResult]:
        """查询缓存（多维度相似度匹配）。

        匹配策略（按优先级）：
          1. hash 精确匹配（100% 相同 query）
          2. 相似度 ≥ 0.7 + 同股票代码 → 复用
          3. 相似度 ≥ 0.6 + 同 verb+noun → 复用
        """
        try:
            data = self._load_chains()
            qh = self._query_hash(query)

            best_match = None
            best_score = 0.0

            for chain_data in data.get("chains", []):
                cached_hash = chain_data.get("query_hash", "")

                # 策略 1: 精确 hash 匹配
                if cached_hash == qh:
                    best_match = chain_data
                    best_score = 1.0
                    break

                # 策略 2: 前缀匹配（短 query 可能碰撞）
                if cached_hash[:CACHE_HASH_PREFIX_LEN] == qh[:CACHE_HASH_PREFIX_LEN]:
                    best_match = chain_data
                    best_score = 0.9
                    break

                # 策略 3: 多维度相似度
                cached_query = chain_data.get("query", "")
                cached_stocks = chain_data.get("stocks", [])
                cached_stock = cached_stocks[0] if cached_stocks else ""

                sim = self._similarity(
                    query, cached_query,
                    stock1=stock_code, stock2=cached_stock,
                    verb1=verb, noun1=noun,
                    verb2="", noun2="",
                )

                if sim > best_score:
                    best_score = sim
                    best_match = chain_data

            # 判断是否命中
            hit = False
            if best_match and best_score >= 0.7:
                hit = True
            elif best_match and best_score >= 0.6:
                # 需要 verb+noun 匹配加持
                cached_verbs = best_match.get("trigger_verbs", [])
                cached_nouns = best_match.get("trigger_nouns", [])
                if (verb and verb in cached_verbs) or (noun and noun in cached_nouns):
                    hit = True

            if hit and best_match:
                # 检查 TTL
                created = best_match.get("created_at", "")
                if created:
                    try:
                        created_dt = datetime.fromisoformat(created)
                        age = (datetime.now() - created_dt).total_seconds()
                        if age > PLAN_TTL:
                            logger.info("[Planner] 缓存过期 (age=%.0fs)", age)
                            return None
                    except ValueError:
                        return None

                chain_def = self._chain_from_dict(best_match)
                if chain_def:
                    best_match["hit_count"] = best_match.get("hit_count", 0) + 1
                    best_match["last_used"] = datetime.now().isoformat()
                    self._save_chains(data)

                    logger.info("[Planner] 缓存命中 (score=%.2f): %s → %s",
                                best_score, query[:30], chain_def.chain_id)

                    return PlanResult(
                        success=True,
                        chain_def=chain_def,
                        reasoning=best_match.get("reasoning", ""),
                        from_cache=True,
                        stocks=best_match.get("stocks", []),
                    )
        except Exception as e:
            logger.warning("[Planner] 缓存查询失败: %s", e)

        return None

    def _save_plan(self, query: str, plan_data: Dict[str, Any], stock_code: str):
        """保存规划到 tool_chains.json。"""
        try:
            data = self._load_chains()
            qh = self._query_hash(query)

            entry = {
                "id": f"plan_{int(time.time())}_{qh[:8]}",
                "query_hash": qh,
                "query": query[:200],
                "created_at": datetime.now().isoformat(),
                "steps": plan_data.get("steps", []),
                "stocks": plan_data.get("stocks", []) or ([stock_code] if stock_code else []),
                "reasoning": plan_data.get("reasoning", ""),
                "hit_count": 1,
                "last_used": datetime.now().isoformat(),
                "backtest_results": {},  # 回测结果（后续填充）
            }

            data.setdefault("chains", []).append(entry)

            # 清理过期条目
            now = datetime.now()
            before_count = len(data["chains"])
            data["chains"] = [
                c for c in data["chains"]
                if self._is_not_expired(c, now)
            ]
            after_count = len(data["chains"])
            if before_count != after_count:
                logger.info("[Planner] 清理过期规划: %d → %d", before_count, after_count)

            self._save_chains(data)
        except Exception as e:
            logger.warning("[Planner] 保存规划失败: %s", e)

    def _is_not_expired(self, chain_data: Dict, now: datetime) -> bool:
        created = chain_data.get("created_at", "")
        if not created:
            return False
        try:
            return (now - datetime.fromisoformat(created)).total_seconds() <= PLAN_TTL
        except ValueError:
            return False

    def _chain_from_dict(self, data: Dict) -> Optional[ChainDef]:
        """从缓存条目构建 ChainDef。"""
        steps = []
        for i, s in enumerate(data.get("steps", []), 1):
            agent = s.get("agent", "")
            if agent:
                steps.append(ChainStep(
                    name=agent, agent=agent, order=i,
                    required=(i == 1),
                ))
        if not steps:
            return None

        chain_id = f"cached+{data.get('query_hash', '')[:8]}"
        chain_def = ChainDef(
            chain_id=chain_id,
            name="缓存规划链路",
            description=data.get("reasoning", ""),
            steps=steps,
            trigger_verbs=[],
            trigger_nouns=[],
        )
        register_chain(chain_def)  # 动态链路必须注册才能被 ChainExecutor 找到
        return chain_def

    # ── 文件 IO ──

    def _load_chains(self) -> Dict[str, Any]:
        path = _get_tool_chains_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"version": 1, "chains": []}

    def _save_chains(self, data: Dict[str, Any]):
        path = _get_tool_chains_path()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_backtest_result(self, plan_id: str, win_rate: float, avg_pnl: float, sample_count: int):
        """更新规划的回测结果（由回溯评估引擎调用）。

        Args:
            plan_id: 规划 ID（如 "plan_1717900000_a1b2c3d4"）
            win_rate: 胜率
            avg_pnl: 平均盈亏率（%）
            sample_count: 样本数
        """
        try:
            data = self._load_chains()
            for chain in data.get("chains", []):
                if chain.get("id") == plan_id:
                    chain["backtest_results"] = {
                        "win_rate": round(win_rate, 3),
                        "avg_pnl": round(avg_pnl, 2),
                        "sample_count": sample_count,
                        "updated_at": datetime.now().isoformat(),
                    }
                    self._save_chains(data)
                    logger.info("[Planner] 回测结果更新: plan=%s win_rate=%.1f%% avg_pnl=%.2f%%",
                                plan_id, win_rate * 100, avg_pnl)
                    return
            logger.warning("[Planner] 未找到规划: %s", plan_id)
        except Exception as e:
            logger.warning("[Planner] 回测结果更新失败: %s", e)

    def get_plan_stats(self) -> Dict[str, Any]:
        """获取规划统计信息（调试用）。"""
        try:
            data = self._load_chains()
            chains = data.get("chains", [])
            if not chains:
                return {"total": 0, "active": 0, "expired": 0}

            now = datetime.now()
            active = [c for c in chains if self._is_not_expired(c, now)]
            expired = len(chains) - len(active)

            total_hits = sum(c.get("hit_count", 0) for c in chains)
            avg_hits = total_hits / len(chains) if chains else 0

            return {
                "total": len(chains),
                "active": len(active),
                "expired": expired,
                "total_hits": total_hits,
                "avg_hits": round(avg_hits, 1),
            }
        except Exception:
            return {"total": 0, "error": True}

    # ── 降级 ──

    def _degrade(self, reason: str, t0: float) -> PlanResult:
        """降级到默认链路。"""
        logger.warning("[Planner] 降级: %s", reason)

        # 写降级日志（结构化，便于后续分析）
        try:
            log_path = _get_tool_chains_path().parent / "planner_degrade.log"
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "reason": reason,
                "fallback_skills": DEFAULT_FALLBACK_SKILLS,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 日志写入失败不影响主流程

        steps = [
            ChainStep(name=skill, agent=skill, order=i + 1, required=(i == 0))
            for i, skill in enumerate(DEFAULT_FALLBACK_SKILLS)
        ]

        chain_def = ChainDef(
            chain_id="degrade+default",
            name="降级默认链路",
            description=f"规划失败降级: {reason}",
            steps=steps,
            trigger_verbs=[],
            trigger_nouns=[],
        )
        register_chain(chain_def)  # 动态链路必须注册才能被 ChainExecutor 找到

        return PlanResult(
            success=True,  # 降级也算成功（有链路可执行）
            chain_def=chain_def,
            reasoning=f"降级: {reason}",
            degraded=True,
            degrade_reason=reason,
            elapsed_ms=(time.time() - t0) * 1000,
        )


# ═══════════════════════════════════════════════════════════════
# 默认降级链路（不需要 Planner 实例也能用）
# ═══════════════════════════════════════════════════════════════

def get_default_fallback_chain() -> ChainDef:
    """获取默认降级链路。"""
    steps = [
        ChainStep(name=skill, agent=skill, order=i + 1, required=(i == 0))
        for i, skill in enumerate(DEFAULT_FALLBACK_SKILLS)
    ]
    chain_def = ChainDef(
        chain_id="degrade+default",
        name="降级默认链路",
        description="规划失败时的保底链路",
        steps=steps,
        trigger_verbs=[],
        trigger_nouns=[],
    )
    register_chain(chain_def)  # 动态链路必须注册才能被 ChainExecutor 找到
    return chain_def
