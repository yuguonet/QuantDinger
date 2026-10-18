# -*- coding: utf-8 -*-
"""preselect/candidate_index.py — 统一类型化候选索引（2026-10-19，实施方案阶段 2）。

对应 `tmp/skill_system_rework_implementation_plan_20261018.md` §3：
  · 技能（skills_adapter）与工具（tool_provider）统一为 `Candidate` 抽象，但
    **契约分开**：技能候选不带工具 schema、工具候选不带技能正文；分池排序、
    分池截断（TopK_skill / TopM_tool），禁止混排后截断。
  · 硬约束过滤（§3.2）：非 active 技能、未注册工具不进候选；registry 不可用
    ⇒ fail-open 保留既有全量技能集（不静默返回空形状）。
  · 身份统一（§14.5）：skill 候选 id/display/version 经 `skill_registry` 解析。
  · 检索信号（§5.2 首版）：名称/描述/tags 词面 + 分面，确定性、零 LLM、
    零 embedding；语义增强留待阶段 6 按数据决定。

本模块提供索引与排序框架，由 `tool_preselect.build_skill_catalog_topk`
（阶段 3 单一入口）消费；Top-K/回退/字符预算开关见该函数与 qd_preselect。

2026-10-19 阶段 3 增量（`build_skill_catalog_topk` 在 tool_preselect 消费本模块）：
  · `rank_skills_scored`：eligible 技能按词面分排序（不截断），供 Top-K 常规位与
    探索位共用同一排序真源；
  · `select_explore`（§14.2）：受限探索位——候选 = eligible 且 TTL 内新发布
    （registry `recent_published_skill_ids`；DB 不可用 ⇒ None ⇒ 不探索，fail-closed）
    且未在曝光窗口内曝光（`_EXPOSURE` 模块级状态 + 锁，有界轮换）。探索只扩大
    候选可见性：不降低门槛、不强制选择、不视为成功使用。

2026-10-19 阶段 5 增量（软重排灰度，§14.3/§15.4）：
  · `feedback_rank_enabled` / `apply_feedback_rerank`：eligible 定序后按
    `final = base_score × skill_events.weight_of(skill_id, version)` 轻量重排
    ——新反馈链是技能候选排序的**唯一反馈来源**（旧
    `qd_agent_weights.layer='skill'` 排序读口已在 qd_service 摘除）；只调序
    不过滤（eligible 集合/门槛/探索资格不变），冷启动与失败恒 1.0 逐位一致；
    开关 `QD_SKILL_FEEDBACK_RANK`（默认开）可一键回退纯词面（§11 回滚铁律）。
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..chain import skill_registry as sr
from ..log import logger

# ── 配置（阈值走 env，工程红线）────────────────────────────────

_TOPK_DEFAULT = 4        # §15.2：3 常规位 + 1 探索位（探索位阶段 3 落地）
_TOPM_DEFAULT = 8        # §15.2：工具 Top-M 沿用 QD_PRESELECT_MAX 当前默认 8
_MIN_SCORE_DEFAULT = 2   # §15.2：初版沿用 _SKILL_INJECT_MIN=2 原始词面分门槛


def skill_topk() -> int:
    """技能候选 Top-K（QD_PRESELECT_SKILL_TOPK，默认 4；0/负 = 旧全量路径）。

    命名与 `QD_PRESELECT_SKILL` / `QD_PRESELECT_SKILL_TOOLS_MAX` 同族（预选管线
    旋钮）；≤0 由 `build_skill_catalog_topk` 内部委托旧 `build_skill_catalog`。
    """
    raw = (os.getenv("QD_PRESELECT_SKILL_TOPK") or "").strip()
    if not raw:
        return _TOPK_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _TOPK_DEFAULT


def tool_topm() -> int:
    """工具候选 Top-M（QD_TOOL_TOPM，默认 8；0/负 = 不截断）。"""
    raw = (os.getenv("QD_TOOL_TOPM") or "").strip()
    if not raw:
        return _TOPM_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _TOPM_DEFAULT


def skill_min_score() -> float:
    """技能最低相关度（QD_SKILL_MIN_SCORE，默认 2；探索位同样必须过门槛）。"""
    raw = (os.getenv("QD_SKILL_MIN_SCORE") or "").strip()
    if not raw:
        return _MIN_SCORE_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _MIN_SCORE_DEFAULT


# ── 类型化候选模型（§3.1）──────────────────────────────────────

@dataclass
class Candidate:
    kind: str                              # "skill" | "tool"
    id: str                                # skill_id / tool_name（稳定标识）
    name: str                              # 对外名称（skill display_name / tool name）
    description: str = ""
    facets: Dict[str, str] = field(default_factory=dict)
    source: str = ""                       # skills_adapter / tool_provider / native_registry
    base_score: float = 0.0
    rank_metadata: Dict[str, Any] = field(default_factory=dict)
    # ── 技能专属（工具候选恒空）──
    skill_version: Optional[str] = None
    lifecycle_state: Optional[str] = None
    declared_tools: List[str] = field(default_factory=list)
    body_uri: Optional[str] = None
    # ── 工具专属（技能候选恒空）──
    tool_domain: Optional[str] = None
    schema: Optional[dict] = None
    registered: bool = True


# ── 构建：技能池 / 工具池 ──────────────────────────────────────

def _facets_for(info: dict) -> Dict[str, str]:
    """分面提取（§5.1）：adapter 未暴露时为空 dict（缺失允许，不淘汰技能）。"""
    raw = info.get("facets") if isinstance(info, dict) else None
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}
    return {}


def _active_versions(names: Sequence[str]) -> Optional[Dict[str, str]]:
    """批量版本查询（经 registry 单一真源；None = 不可用，fail-open）。"""
    return sr.get_active_versions(list(names or []))


def build_skill_candidates(adapter) -> List[Candidate]:
    """从技能 adapter 构建技能候选池（只含轻量字段，不常驻正文）。"""
    if adapter is None:
        return []
    try:
        infos = adapter.list_skills() or []
    except Exception as e:
        logger.debug("[candidate_index] 技能清单获取失败（fail-open 空池）: %s", e)
        return []
    out: List[Candidate] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        name = str(info.get("name") or "").strip()
        if not name:
            continue
        skill_id = sr.resolve_skill_id(adapter, name)
        out.append(Candidate(
            kind="skill", id=skill_id, name=name,
            description=str(info.get("description") or "")[:300],
            facets=_facets_for(info), source="skills_adapter",
            declared_tools=[str(t).strip() for t in (info.get("tools") or [])
                            if str(t).strip()],
        ))
    versions = _active_versions([c.id for c in out])
    if versions:
        for c in out:
            c.skill_version = versions.get(c.id)
    return out


def build_tool_candidates(provider) -> List[Candidate]:
    """从 ToolProvider 构建工具候选池（schema 只进工具池）。"""
    if provider is None:
        return []
    try:
        schemas = provider.get_schemas() or []
    except Exception as e:
        logger.debug("[candidate_index] 工具 schema 获取失败（fail-open 空池）: %s", e)
        return []
    out: List[Candidate] = []
    for sc in schemas:
        if not isinstance(sc, dict):
            continue
        name = str(sc.get("name") or "").strip()
        if not name:
            continue
        try:
            domain = provider.get_domain(name)
        except Exception:
            domain = "common"
        out.append(Candidate(
            kind="tool", id=name, name=name,
            description=str(sc.get("description") or "")[:300],
            source="tool_provider", tool_domain=str(domain or "common"),
            schema=sc, registered=True,
        ))
    return out


# ── 硬约束过滤（§3.2）─────────────────────────────────────────

def _skill_states(names: Sequence[str]) -> Optional[Dict[str, str]]:
    """批量生命周期查询（registry 单一真源；None = 不可用，fail-open 全量保留）。"""
    return sr.get_lifecycle_states(list(names or []))


def filter_available(cands: Sequence[Candidate]) -> List[Candidate]:
    """硬约束过滤：非 active 技能/未注册工具不进候选。

    fail-open 语义：registry 不可用（states=None）⇒ 技能全量保留——回到既有
    技能匹配路径，**不静默返回空成功形状**（§3.2）。
    """
    skills = [c for c in cands if c.kind == "skill"]
    tools = [c for c in cands if c.kind == "tool"]
    states = _skill_states([c.id for c in skills])
    if states is None:
        kept_skills = list(skills)      # fail-open：不过滤
    else:
        kept_skills = [c for c in skills
                       if states.get(c.id, "active") == "active"]
        for c in kept_skills:
            c.lifecycle_state = states.get(c.id, "active")
    kept_tools = [c for c in tools if c.registered]
    return kept_skills + kept_tools


# ── 词面打分（§5.2 首版：确定性词典信号，零 LLM/零 embedding）──

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _query_tokens(text: str) -> set:
    """查询 token：拉丁词 +  CJK 单字 + CJK 二元组（短查询也能出信号）。"""
    seq = _TOKEN_RE.findall(str(text or "").lower())
    toks = set(seq)
    cjk = [t for t in seq if re.match(r"[\u4e00-\u9fff]$", t)]
    toks.update(f"{cjk[i]}{cjk[i + 1]}" for i in range(len(cjk) - 1))
    return toks


def _lexical_score(query: str, cand: Candidate) -> Tuple[float, list]:
    """词面相关度：查询 token 在 名称/描述/tags/分面 中的命中数（确定性）。"""
    q = _query_tokens(query)
    if not q:
        return 0.0, []
    hay = " ".join([cand.name, cand.description,
                    " ".join(str(v) for v in cand.facets.values())]).lower()
    hits = sorted(t for t in q if t in hay)
    return float(len(hits)), [f"词面命中: {hits[:6]}"] if hits else []


def rank_skills_scored(query: str, skill_cands: Sequence[Candidate]) -> List[Candidate]:
    """eligible 技能按词面分降序（不截断；§5.2/§15.2）。

    与 `rank_pools` 的技能分支同一口径（门槛过滤 + 确定性排序），抽出来供
    Top-K 常规位与探索位共用同一排序真源——两处各写一份排序必然漂移。
    """
    out: List[Candidate] = []
    min_score = skill_min_score()
    for c in skill_cands:
        sc, reasons = _lexical_score(query, c)
        if sc >= min_score:
            c.base_score = sc
            c.rank_metadata = {"reasons": reasons, "kind": "skill"}
            out.append(c)
    out.sort(key=lambda c: (-c.base_score, c.name))
    return out


# ── 技能效果反馈软重排（§14.3 / §15.4：阶段 5）────────────────────

_FEEDBACK_RANK_DEFAULT = True   # 灰度默认开；安全性由构造保证（见函数文档）


def feedback_rank_enabled() -> bool:
    """反馈软重排开关（`QD_SKILL_FEEDBACK_RANK`，默认开；"0"/"false" 关）。

    阶段 5 灰度旋钮。关 ⇒ 排序回退纯词面（与阶段 3 逐位一致），meta 仍记录
    「若启用会怎样」的影子数据（§11 回滚铁律 + 方案阶段 5「记录启用前后
    排序差异」）。脏值回落默认（与预选族旋钮同型，不抛、不静默改语义）。
    """
    raw = (os.getenv("QD_SKILL_FEEDBACK_RANK") or "").strip()
    if not raw:
        return _FEEDBACK_RANK_DEFAULT
    return raw.lower() not in ("0", "false", "no", "off")


def apply_feedback_rerank(eligible: List[Candidate], *,
                          enabled: Optional[bool] = None) -> tuple:
    """eligible 技能效果反馈软重排（阶段 5，§14.3/§15.4）。

    `final = base_score × weight_of(skill_id, version)`，按
    (final, base, name) 降序——**只调序不过滤**：eligible 集合、注入门槛
    （`QD_SKILL_MIN_SCORE`）、探索位资格全不改变（权重只影响常规位归属与
    展示序）。weight 恒 1.0（冷启动/低于样本门槛/DB 不可用/读取异常）⇒
    与纯词面序逐位一致。

    Args:
        eligible: `rank_skills_scored` 输出（已过门槛；调用方不再放宽）。
        enabled: 开关注入（测试用；None = `feedback_rank_enabled()`）。

    Returns:
        (排序后 list, 元数据)。元数据：
        {enabled, would_reorder, weights:{name: weight}}。
        `would_reorder` = 「若启用排序会变」的影子判据（关时也有值——
        启用前后排序差异的对照源）。每个候选的
        `rank_metadata["feedback"]` 记 {weight, samples, final, applied}
        （版本在 `skill_version`）——供事件链/report 回答「为什么是这个序」
        （§14.3：skill_id、版本、policy/ranker 版本及样本数可审计）。
    """
    if enabled is None:
        enabled = feedback_rank_enabled()
    from ..chain import skill_events as _se

    weights: Dict[str, float] = {}
    for c in eligible:
        w, samples = 1.0, 0
        try:
            detail = _se.feedback_detail(c.id, c.skill_version or "")
        except Exception as e:      # 读口自身已 fail-soft；双保险防接口变更
            logger.debug("[candidate_index] 反馈读取异常（中性 1.0）: %s", e)
            detail = None
        if detail:
            try:
                w = float(detail.get("weight"))
            except (TypeError, ValueError):
                w = 1.0
            try:
                samples = int(detail.get("eligible_count") or 0)
            except (TypeError, ValueError):
                samples = 0
        final = round(float(c.base_score) * w, 6)
        rm = dict(c.rank_metadata or {})
        rm["feedback"] = {"weight": round(w, 6), "samples": samples,
                          "final": final, "applied": bool(enabled)}
        c.rank_metadata = rm
        weights[c.name] = round(w, 6)

    def _key(c: Candidate) -> tuple:
        f = (c.rank_metadata or {}).get("feedback") or {}
        return (-float(f.get("final") or 0.0), -float(c.base_score), c.name)

    hypo = sorted(eligible, key=_key)
    would = [c.name for c in hypo] != [c.name for c in eligible]
    ordered = hypo if enabled else list(eligible)
    return ordered, {"enabled": bool(enabled), "would_reorder": would,
                     "weights": weights}


def rank_pools(query: str, skill_cands: Sequence[Candidate],
               tool_cands: Sequence[Candidate]) -> Tuple[List[Candidate], List[Candidate]]:
    """分池排序 + 分池截断（§3.1：禁止混排后截断）。

    技能池：词面分 ≥ QD_SKILL_MIN_SCORE 才进候选；无达标 ⇒ 空池（无匹配不强配）。
    工具池：同词面分排序，截断到 Top-M；工具全集合法性不受技能 cap 影响。
    """
    k, m = skill_topk(), tool_topm()

    scored_s = rank_skills_scored(query, skill_cands)

    scored_t = []
    for c in tool_cands:
        sc, reasons = _lexical_score(query, c)
        c.base_score = sc
        c.rank_metadata = {"reasons": reasons, "kind": "tool"}
        scored_t.append(c)
    scored_t.sort(key=lambda c: (-c.base_score, c.name))

    return scored_s[:k] if k else [], scored_t[:m] if m else scored_t


# ── 探索位（§14.2 / §15.2：受限轮换曝光）────────────────────────

_EXPLORE_TTL_DEFAULT = 14   # 天：发布资格与曝光窗口共用（TTL 到期只结束探索资格）

#: 进程内曝光状态 {skill_id: 最近曝光时间戳}。模块级 + 锁：预选可能被并发会话
#: 触发；重启即忘（发布 TTL 仍从 DB 兜底总窗口，见 select_explore 文档）。
_EXPOSURE: "OrderedDict[str, float]" = OrderedDict()
_EXPOSURE_LOCK = threading.Lock()


def explore_ttl_days() -> int:
    """探索位 TTL（QD_SKILL_EXPLORE_TTL_DAYS，默认 14；≤0 = 关闭探索位）。"""
    raw = (os.getenv("QD_SKILL_EXPLORE_TTL_DAYS") or "").strip()
    if not raw:
        return _EXPLORE_TTL_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _EXPLORE_TTL_DEFAULT


def _prune_exposure(mapping: dict, now: float, ttl_days: int) -> None:
    """剔除曝光窗口外的旧条目（TTL ≤ 0 ⇒ 全清 = 关闭探索）。"""
    if ttl_days <= 0:
        mapping.clear()
        return
    horizon = now - ttl_days * 86400.0
    for k in [k for k, ts in mapping.items() if ts < horizon]:
        del mapping[k]


def _recently_exposed(now: float, ttl_days: int, exposure: Optional[dict] = None) -> set:
    """曝光窗口内的技能 ID 集合（调用方传入 exposure 时用注入态，免锁）。"""
    if exposure is not None:
        _prune_exposure(exposure, now, ttl_days)
        return set(exposure)
    with _EXPOSURE_LOCK:
        _prune_exposure(_EXPOSURE, now, ttl_days)
        return set(_EXPOSURE)


def _record_exposure(skill_id: str, now: float, exposure: Optional[dict] = None) -> None:
    """记录一次探索曝光（轮换簿记；同一技能不得重复获得无限探索优先级）。"""
    if exposure is not None:
        exposure[str(skill_id)] = now
        return
    with _EXPOSURE_LOCK:
        _EXPOSURE[str(skill_id)] = now
        _EXPOSURE.move_to_end(str(skill_id))


def select_explore(eligible_not_shown: Sequence[Candidate], *,
                   recent_published: Optional[Sequence[str]],
                   now: float, ttl_days: Optional[int] = None,
                   exposure: Optional[dict] = None) -> Tuple[Optional[Candidate], dict]:
    """探索位选择（§14.2）：TTL 内新发布 + 过门槛 + 未近期曝光的合格技能轮换。

    Args:
        eligible_not_shown: 已过门槛与 active 硬约束、但未进常规位的技能
            （调用方保证 eligible；本函数不再放宽任何门槛）。
        recent_published: registry `recent_published_skill_ids` 结果。
            None = 发布状态不可知（DB 不可用）⇒ **不探索**（fail-closed：
            宁可不曝光，不把未验证/过期资格的技能塞进候选）。
        now: 当前时间戳（测试注入）。
        exposure: 曝光状态注入（测试用）；None = 模块级全局（加锁）。

    Returns:
        (选中的探索候选或 None, 漏斗元数据)。元数据字段：
        ttl_days / recent_published_known / candidates / picked / exhausted。
        `exhausted=True` 表示"有合格新技能但轮换窗口内都已曝光"（有界曝光，
        不拿低相关技能填满，也不给同一技能无限优先级）。
    """
    ttl = explore_ttl_days() if ttl_days is None else int(ttl_days)
    meta: Dict[str, Any] = {
        "ttl_days": ttl,
        "recent_published_known": recent_published is not None,
        "candidates": 0, "picked": "", "exhausted": False,
    }
    if ttl <= 0 or not eligible_not_shown or recent_published is None:
        return None, meta
    recent = {str(s) for s in recent_published}
    cands = [c for c in eligible_not_shown if c.id in recent]
    meta["candidates"] = len(cands)
    if not cands:
        return None, meta
    exposed = _recently_exposed(now, ttl, exposure)
    fresh = [c for c in cands if c.id not in exposed]
    if not fresh:
        meta["exhausted"] = True     # 有合格新技能，但窗口内都已曝光（有界）
        return None, meta
    # 轮换序：分数降序 + 名称（确定性）；窗口外旧曝光与未曝光同权
    fresh.sort(key=lambda c: (-c.base_score, c.name))
    pick = fresh[0]
    _record_exposure(pick.id, now, exposure)
    meta["picked"] = pick.id
    return pick, meta
