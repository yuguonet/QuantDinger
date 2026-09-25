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
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 数据域元组：(数据域名, 触发关键词元组, 候选工具元组[0]=首选)
DataDomainRow = Tuple[str, Tuple[str, ...], Tuple[str, ...]]


@dataclass(frozen=True)
class DomainSpec:
    """单个领域的共享分类学。"""
    name: str
    # planner 域字段缺失时的优先兜底域（全注册表至多一个 True）
    primary: bool = False
    # 实体类型 → 本域（chat 阶段先于 plan，只能靠 entity_type 倒推）
    entity_types: Tuple[str, ...] = ()
    # 是否使用【交易日】口径（相对日词解析）
    trading_calendar: bool = False
    # verb → noun（追溯链名 domain+verb+noun 缺省段补齐）
    verb_nouns: Mapping[str, str] = field(default_factory=dict)
    # 需要拉取本域工具面的任务动词（domain 通道兜底）
    intent_verbs: frozenset = field(default_factory=frozenset)
    # 输入语汇 → 本域（无实体时的兜底信号）
    word_pattern: Optional[re.Pattern] = None
    # plan_linter R1 数据域词典
    data_domains: Tuple[DataDomainRow, ...] = ()
    # R2 依赖登记（清单生产者/消费者，按工具名）
    list_producers: frozenset = field(default_factory=frozenset)
    list_consumers: frozenset = field(default_factory=frozenset)


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
            mod = importlib.import_module(f"tools.{sub.name}.domain_meta")
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
                spec = DomainSpec(
                    name=sub.name,
                    primary=spec.primary,
                    entity_types=spec.entity_types,
                    trading_calendar=spec.trading_calendar,
                    verb_nouns=dict(spec.verb_nouns),
                    intent_verbs=spec.intent_verbs,
                    word_pattern=spec.word_pattern,
                    data_domains=spec.data_domains,
                    list_producers=spec.list_producers,
                    list_consumers=spec.list_consumers,
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


def default_entity_type() -> str:
    """默认实体类型：恒为空——禁止核心臆造 stock。"""
    return ""


def fallback_domain(available: Iterable[str]) -> str:
    """planner 域字段缺失时的兜底域。

    优先 primary 且可用；否则唯一可用域；多域且无 primary → 空（不臆造）。
    """
    avail = {a for a in (available or ()) if a}
    if not avail:
        return ""
    for spec in _ensure().values():
        if spec.primary and spec.name in avail:
            return spec.name
    if len(avail) == 1:
        return next(iter(avail))
    return ""


def domain_intent_verbs() -> frozenset:
    """需要拉域的任务动词并集（domain 通道兜底判据）。"""
    out = set()
    for spec in _ensure().values():
        out |= set(spec.intent_verbs or ())
    return frozenset(out)


def word_domain_hints() -> List[Tuple[re.Pattern, str]]:
    """输入语汇 → 领域（按注册顺序）。"""
    out: List[Tuple[re.Pattern, str]] = []
    for spec in _ensure().values():
        if spec.word_pattern is not None:
            out.append((spec.word_pattern, spec.name))
    return out


def iter_data_domains() -> Tuple[DataDomainRow, ...]:
    """plan_linter R1 词典（跨域按注册顺序拼接）。"""
    rows: List[DataDomainRow] = []
    for spec in _ensure().values():
        rows.extend(spec.data_domains)
    return tuple(rows)


def list_producers() -> frozenset:
    out = set()
    for spec in _ensure().values():
        out |= set(spec.list_producers or ())
    return frozenset(out)


def list_consumers() -> frozenset:
    out = set()
    for spec in _ensure().values():
        out |= set(spec.list_consumers or ())
    return frozenset(out)
