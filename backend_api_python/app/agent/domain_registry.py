# -*- coding: utf-8 -*-
"""领域注册表 —— 核心链路唯一的域分类学来源。

设计（2026-09-25 通用性改造）：
  - 工具面域仍由 ToolProvider.scan_subdirectories() 从 tools/<子目录>/ 推导
  - 本模块只放「跨层共享的分类学」：verb 归类、实体→域、交易日口径、
    默认/兜底域策略、数据域词典（plan_linter R1）
  - 各域专属分类学放在 tools/<domain>/domain_meta.py，由本模块自动装载
  - 核心图（nodes / task_agent / tracing / resolvers）禁止直接写 "finance"/"stock"

新领域接入：
  1. tools/<new_domain>/ 放工具（自动成为可选域）
  2. tools/<new_domain>/domain_meta.py 导出 SPEC = DomainSpec(...)
  3. 不必改核心

易错点：
  - 不要把「工具名」硬编码进本模块；工具名属于 tools/<domain>/ 的词典字段
  - domain_meta 只准是纯数据/正则，禁止 import 核心图（会成环）
  - fallback_domain 在多域且无 primary 时返回空——宁可让 planner 空域，
    也不臆造一个默认域（禁止「唯一可选域」式硬编码）
"""
from __future__ import annotations

import importlib

from .modpath import import_module
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 数据域元组：(数据域名, 触发关键词元组, 候选工具元组[0]=首选)
@dataclass(frozen=True)
class DomainSpec:
    """单个领域的共享分类学。

    ★ 2026-10-14 精简（对照 `app/agent/领域domain作用梳理_20261014.md`）：
      删掉 `primary` / `intent_verbs` / `data_domains` / `list_producers` /
      `list_consumers` 五个字段 —— 它们**只**被本模块那 6 个零消费方函数读
      （`fallback_domain` / `domain_intent_verbs` / `iter_data_domains` /
      `list_producers` / `list_consumers` / `default_entity_type`），随本次一并删除。
      保留的四个字段都**有活消费方**：`entity_types`（entity_to_domain）、
      `trading_calendar`（trading_calendar_domains → 交易日口径）、
      `verb_nouns`（classify_verb → 链名归一）、`word_pattern`（word_domain_hints）。

      ⚠ **不要再把"数据域词典"加回这里**：活的唯一一份在
      `app/agent/preselect/tool_preselect.py::DATA_DOMAINS`（15 域，驱动 lint
      R1 补位 / R4 裁剪 / 域兜底）。旧 `tools/finance/domain_meta.py::_DATA_DOMAINS`
      与它**早已分叉**（realtime_quote vs quote、fund_flow vs fundflow，且引用了
      已下线的 index_daily / day_series），两套并行正是本次要根治的"同一决策两个
      事实源"。新增域请改活的那份。
    """
    name: str
    # 实体类型 → 本域（chat 阶段先于 plan，只能靠 entity_type 倒推）
    entity_types: Tuple[str, ...] = ()
    # 是否使用【交易日】口径（相对日词解析）
    trading_calendar: bool = False
    # verb → noun（追溯链名 domain+verb+noun 缺省段补齐）
    verb_nouns: Mapping[str, str] = field(default_factory=dict)
    # 输入语汇 → 本域（无实体时的兜底信号）
    word_pattern: Optional[re.Pattern] = None


_SPECS: Dict[str, DomainSpec] = {}
_LOADED = False


def register(spec: DomainSpec) -> None:
    """登记一个 DomainSpec（后写覆盖先写，便于测试注入）。"""
    if not spec or not spec.name:
        return
    _SPECS[spec.name] = spec


def _load_from_tools() -> None:
    """扫描 tools/<domain>/domain_meta.py 并装载 SPEC。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    tools_dir = Path(__file__).resolve().parent / "tools"
    if not tools_dir.is_dir():
        return
    for sub in sorted(tools_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_") or sub.name == "__pycache__":
            continue
        meta = sub / "domain_meta.py"
        if not meta.is_file():
            continue
        try:
            mod = import_module(f"tools.{sub.name}.domain_meta")
        except Exception as e:
            logger.warning("[DomainRegistry] 装载 %s/domain_meta 失败：%s", sub.name, e)
            continue
        spec = getattr(mod, "SPEC", None)
        if isinstance(spec, DomainSpec):
            if spec.name != sub.name:
                logger.warning(
                    "[DomainRegistry] %s/domain_meta.SPEC.name=%s 与目录名不一致，以目录名为准",
                    sub.name, spec.name,
                )
                # ★ 2026-10-14：只搬运**仍有消费方**的四个字段（见 DomainSpec 头注）。
                #   精简前这里逐个拷贝全部 8 个字段，字段一删就会静默漏搬。
                spec = DomainSpec(
                    name=sub.name,
                    entity_types=spec.entity_types,
                    trading_calendar=spec.trading_calendar,
                    verb_nouns=dict(spec.verb_nouns),
                    word_pattern=spec.word_pattern,
                )
            register(spec)


def _ensure() -> Dict[str, DomainSpec]:
    _load_from_tools()
    return _SPECS


def specs() -> Dict[str, DomainSpec]:
    return dict(_ensure())


def get_spec(domain: str) -> Optional[DomainSpec]:
    return _ensure().get(domain or "")


# ── 查找 API（核心只准走这里）────────────────────────────────


# ── 链名归一（2026-09-30，技能通用性修复）────────────────────────────
# chain_name（domain+verb+noun）既是酿造聚合键，也是 skill 命名词根。历史上
# noun 槽曾被股票代码占据（如 stock+analyze+600929），导致按标的分裂出绑定
# 单只股票的 skill（skills/auto-stock-analyze-600929），违背 AGENT_DESIGN §3.16
# 泛化纪律。归一规则：任何链名段内的实体数字码（4~6 位连数字串，A 股代码、
# 基金代码等）一律剥离；段因此变空则丢段。写入层（tracing）与消费层
# （store 聚合 / skill_brewer 命名与幂等）共用本规则，保证同构链（不同标的的
# 同意图任务）归到同一个键、酿出跨标的通用技能。
_ENTITY_RUN_RE = re.compile(r"(?<!\d)\d{4,6}(?!\d)")
_MARKET_SUFFIX_RE = re.compile(r"^[._-]?(sh|sz|bj|hk)$", re.I)


def strip_entity(seg: str) -> str:
    """剥离段内实体数字码：'600929' → ''，'stock' → 'stock'，'2026' → ''，
    '600519.SH'（先被 normalize 小写）→ ''——市场后缀残留一并清。"""
    s = _ENTITY_RUN_RE.sub("", seg or "").strip("-_ ")
    return _MARKET_SUFFIX_RE.sub("", s).strip("-_ .")


def normalize_chain_name(raw: str) -> str:
    """链名归一：剥实体码 + 丢空段。'stock+analyze+600929' → 'stock+analyze'。

    幂等：normalize(normalize(x)) == normalize(x)。
    """
    segs = []
    for seg in re.split(r"[+]", raw or ""):
        s = strip_entity(seg.strip().lower())
        if s:
            segs.append(s)
    return "+".join(segs)


def classify_verb(verb: str) -> Dict[str, str]:
    """verb → {domain, noun}；未登记返回空 dict（调用方落 unknown）。"""
    v = (verb or "").strip().lower()
    if not v:
        return {}
    for spec in _ensure().values():
        if v in spec.verb_nouns:
            return {"domain": spec.name, "noun": spec.verb_nouns[v]}
    return {}


def entity_to_domain(entity_type: str) -> str:
    """实体类型 → 领域名；未知返回空。"""
    et = (entity_type or "").strip().lower()
    if not et:
        return ""
    for spec in _ensure().values():
        if et in spec.entity_types:
            return spec.name
    return ""


def trading_calendar_domains() -> frozenset:
    return frozenset(s.name for s in _ensure().values() if s.trading_calendar)


def word_domain_hints() -> List[Tuple[re.Pattern, str]]:
    """输入语汇 → 领域（按注册顺序）。"""
    out: List[Tuple[re.Pattern, str]] = []
    for spec in _ensure().values():
        if spec.word_pattern is not None:
            out.append((spec.word_pattern, spec.name))
    return out


# ★ 2026-10-14：`iter_data_domains` / `list_producers` / `list_consumers` /
#   `domain_intent_verbs` / `fallback_domain` / `default_entity_type` 六个函数已在
#   本次精简中删除（全仓**零消费方**，只被已退役的旧编排层 plan_linter /
#   selected_domain / task_agent 用过）。
#   它们读的 `data_domains` / `list_producers` / `list_consumers` /
#   `intent_verbs` / `primary` 五个 DomainSpec 字段随之删除（见 DomainSpec 头注）。
#   ⚠ 其中 `data_domains` 那份词典与活词典早已分叉，**不要**照这里的旧条目复原——
#     活的唯一一份是 `app/agent/preselect/tool_preselect.py::DATA_DOMAINS`。
