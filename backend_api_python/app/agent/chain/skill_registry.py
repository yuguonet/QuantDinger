# -*- coding: utf-8 -*-
"""chain/skill_registry.py — 技能注册表 / 稳定身份 / 生命周期 / 验证门禁 / 事务性发布。

2026-10-19 · 实施方案阶段 1（`tmp/skill_system_rework_implementation_plan_20261018.md` §4/§7/§14.5/§15）：

  稳定身份（§4.1/§15.3）：`skill_id`（规范化目录名，跨版本稳定）+ 内容 SHA-256
    摘要版本 ID（不可变；SemVer 仅显示元数据，不作唯一版本身份）。
  生命周期（§4.2）：generated → candidate → validated → active（+ rejected/
    superseded/disabled/archived），迁移合法性由 `can_transition` 判定。
  验证门禁（§7）：格式 → 契约（工具真实性剔除）→ 通用性（禁固化样本）→ 近重复
    （规范化名称/描述相似度）→ 事务性发布（staging + 原子替换）→ 验证报告留痕。
  容量（§4.3/§15.1）：自动管理技能全局上限 `QD_SKILL_AUTO_MAX`（默认 10，v1 无
    家族配额）；达上限新候选待审，不覆盖/不删除存量。

红线沿用：
  - 阈值/口径走 env（QD_SKILL_AUTO_MAX / QD_SKILL_DUP_JACCARD / QD_META_TOOLS），
    不硬编码；脏值回落默认并夹紧。
  - DB 访问函数内惰性 import + fail-soft（登记失败不回滚已发布文件；计数失败
    保守放行——与 store.acquire_run_lock "宁松勿卡"同哲学）。
  - 人工技能（非 auto- 前缀）不受自动生命周期/容量治理。
  - 不复制正文作第二真源：DB 只存治理元数据，`body_uri` 指向 SKILL.md。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

#: 验证器版本（写入验证报告；重放实验按版本复现口径）
VALIDATOR_VERSION = "gate-v1"

# ── 生命周期状态机（§4.2）──────────────────────────────────────
LIFECYCLE_STATES = ("generated", "candidate", "validated", "active",
                    "rejected", "superseded", "disabled", "archived")
_TRANSITIONS: Dict[str, set] = {
    "generated": {"candidate", "rejected"},
    "candidate": {"validated", "rejected"},
    "validated": {"active", "rejected"},
    "active": {"superseded", "disabled", "archived"},
    "rejected": set(),               # 拒绝不得直接复活（须重新生成/验证）
    "superseded": {"archived"},
    "disabled": {"active", "archived"},   # 恢复由调用方重新过门禁
    "archived": {"active"},               # 恢复由调用方重新过门禁（§15.6）
}

#: frontmatter 可选分面键（§5.1；缺失允许，不得因未声明淘汰技能）
_FACET_KEYS = ("domain_family", "object_type", "action", "market_state",
               "horizon", "perspective", "object_instance")


def can_transition(src: str, dst: str) -> bool:
    """生命周期迁移合法性（未知状态一律拒，fail-closed）。"""
    return dst in _TRANSITIONS.get(src, set())


# ── 稳定身份（§4.1/§15.3/§14.5）────────────────────────────────

def normalize_skill_id(name: Any) -> str:
    """显示名/目录名/历史别名 → 稳定 skill_id（小写连字符；与 Anthropic 规范同口径）。"""
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s


def _normalize_content(text: str) -> str:
    """内容规范化规则（§15.3：实现前固定并测试）：
    去 BOM、CRLF→LF、去每行行尾空白。仅格式差异不产生新版本 ID。"""
    s = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in s.split("\n"))


def content_digest(text: str) -> str:
    """内容摘要版本 ID：规范化后 SHA-256 前 16 hex（不可变、可复现）。"""
    return hashlib.sha256(_normalize_content(text).encode("utf-8")).hexdigest()[:16]


def is_auto_managed(skill_id: str) -> bool:
    """是否自动管理（auto- 前缀；人工技能不受容量/淘汰治理）。"""
    return str(skill_id or "").startswith("auto-")


def resolve_skill_id(adapter, name_or_alias: str) -> str:
    """统一解析入口（§14.5）：显示名/目录名/历史别名 → skill_id。

    复用 adapter 既有 display→name 映射（与 skill_tools.run_skill /
    qd_service._canonical_skill_name 同口径，不另写字符串替换）；
    未知名原样返回（fail-open，不抛、不猜）。
    """
    if adapter is None:
        return str(name_or_alias or "")
    try:
        info = adapter.get(name_or_alias)
        if info is not None and getattr(info, "name", ""):
            return info.name
    except Exception:
        pass
    return str(name_or_alias or "")


def display_name(skill_id: str) -> str:
    """skill_id → 显示名。v1 显示名 = skill_id（kebab 统一后二者同口径）。"""
    return str(skill_id or "")


# ── 容量配置（§4.3/§15.1）──────────────────────────────────────

_AUTO_CAP_DEFAULT = 10


def auto_skill_cap() -> int:
    """自动管理技能全局上限（QD_SKILL_AUTO_MAX，默认 10；v1 无家族配额）。

    非法值回落默认；负值夹紧到 0（= 关闭自动容量，全部候选待审），不回落默认。
    """
    raw = (os.getenv("QD_SKILL_AUTO_MAX") or "").strip()
    if not raw:
        return _AUTO_CAP_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return _AUTO_CAP_DEFAULT
    return max(0, v)


def _dup_jaccard_min() -> float:
    """近重复判定阈值（QD_SKILL_DUP_JACCARD，默认 0.6；<=0 关闭去重门）。"""
    raw = (os.getenv("QD_SKILL_DUP_JACCARD") or "").strip()
    if not raw:
        return 0.6
    try:
        return float(raw)
    except ValueError:
        return 0.6


# ── 分面解析（§5.1）────────────────────────────────────────────

def parse_facets(meta: dict) -> Dict[str, str]:
    """从 frontmatter 解析可选分面（metadata.facets JSON/dict + 顶层分面键）。

    分面缺失是允许的；只收字符串值，脏值忽略（不抛）。
    """
    out: Dict[str, str] = {}
    if not isinstance(meta, dict):
        return out
    block = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    raw = block.get("facets", meta.get("facets"))
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, str) and v.strip():
                out[str(k)] = v.strip()
    for k in _FACET_KEYS:
        v = block.get(k, meta.get(k))
        if isinstance(v, str) and v.strip():
            out.setdefault(k, v.strip())
    return out


# ── frontmatter 工具声明（从 skill_brewer 下沉，单一真源）───────

def extract_frontmatter_tools(skmd: str) -> List[str]:
    """从 SKILL.md frontmatter 提取 tools 列表（legacy 顶层/规范 metadata 两种位置）。"""
    m = re.search(r"^[ \t]*tools:[ \t]*(.+?)[ \t]*$", skmd or "", re.M)
    if not m:
        return []
    raw = m.group(1).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    raw = raw.strip("'\"")
    return [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]


_META_TOOLS_DEFAULT = ("list_tools", "search_tools", "format_result",
                       "final_answer", "python_interpreter", "web_search")


def meta_tools() -> frozenset:
    """写盘前要剔除的"元工具"名单（QD_META_TOOLS，逗号分隔；空 env 回落默认）。"""
    raw = os.getenv("QD_META_TOOLS")
    if raw is None or not raw.strip():
        return frozenset(_META_TOOLS_DEFAULT)
    names = {t.strip() for t in raw.split(",") if t.strip()}
    return frozenset(names) if names else frozenset(_META_TOOLS_DEFAULT)


def strip_unknown_tools(skmd: str, valid_tools) -> str:
    """tools 白名单真实性校验：剔除编造名 + 元工具，保留原有写法（legacy 方括号/规范逗号串）。

    与原 skill_brewer._validate_tools 逐字同语义（下沉为单一真源，brewer 保留薄包装）。
    """
    valid = set(valid_tools or ())
    meta = meta_tools()
    tools = extract_frontmatter_tools(skmd)
    bad = [t for t in tools if t and (t not in valid or t in meta)]
    if not bad:
        return skmd
    kept = [t for t in tools if t not in bad]

    def _repl(m):
        prefix, value = m.group(1), m.group(2).strip()
        body = "[" + ", ".join(kept) + "]" if value.startswith("[") else ", ".join(kept)
        return prefix + body

    return re.sub(r"^([ \t]*tools:[ \t]*)(.*)$", _repl, skmd, count=1, flags=re.M)


# ── 通用性检查（从 skill_brewer 下沉，单一真源）─────────────────

def find_entity_codes(skmd: str) -> List[str]:
    """编译产物里的六位实体码（日期/溯源 id 先剥离防误伤）。"""
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "", skmd or "")
    text = re.sub(r"root_id=\d+", "", text)
    return re.findall(r"(?<!\d)\d{6}(?!\d)", text)


def generic_violations(skmd: str, sample_name: str = "") -> List[str]:
    """通用性违规清单：实体码 + 样本标的名称双维度。"""
    refs = [f"code:{c}" for c in find_entity_codes(skmd)]
    name = str(sample_name or "").strip()
    if len(name) >= 2 and name in (skmd or ""):
        refs.append(f"name:{name}")
    return refs


# ── 近重复检测（§7.4 / §4.3）───────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _tokens(text: str) -> set:
    return set(_TOKEN_RE.findall(str(text or "").lower()))


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def near_duplicate_of(skmd_meta: dict, active_infos: Sequence[dict]) -> Optional[str]:
    """与 active 技能比对：规范化名称相等或描述 Jaccard ≥ 阈值 ⇒ 返回重复技能名。"""
    thr = _dup_jaccard_min()
    if thr <= 0:
        return None
    new_name = normalize_skill_id(skmd_meta.get("name") or "")
    new_desc = str(skmd_meta.get("description") or "")
    for info in active_infos or ():
        if not isinstance(info, dict):
            continue
        if normalize_skill_id(info.get("name") or "") == new_name and new_name:
            return str(info.get("name") or "")
        if _jaccard(new_desc, str(info.get("description") or "")) >= thr:
            return str(info.get("name") or "")
    return None


# ── 验证门禁编排（§7）──────────────────────────────────────────

@dataclass
class GateReport:
    ok: bool
    status: str                       # validated / bad_format / not_generic / duplicate
    issues: List[str] = field(default_factory=list)
    skmd: str = ""                    # 可能被工具剔除修正过的文档
    stripped_tools: List[str] = field(default_factory=list)
    report: Dict[str, Any] = field(default_factory=dict)
    description: str = ""             # 登记用（frontmatter description）
    facets: Dict[str, str] = field(default_factory=dict)
    declared_tools: List[str] = field(default_factory=list)


def frontmatter_semver(skmd: str) -> str:
    """frontmatter version 显示值（SemVer 仅显示元数据，§15.3）；缺省空串。"""
    m = re.search(r"^[ \t]*version:[ \t]*([\d.]+)", skmd or "", re.M)
    return m.group(1) if m else ""


def validate_gates(skmd: str, *, skill_id: str, provider_tools=None,
                   sample_name: str = "", active_infos: Optional[Sequence[dict]] = None) -> GateReport:
    """生成验证关卡（§7）：格式 → 契约 → 通用性 → 近重复。

    Args:
        skmd: 编译产物全文（frontmatter + body）。
        skill_id: 稳定 ID（= 目标目录名）。
        provider_tools: 当前合法可调用工具全集（None = 跳过契约剔除）。
        sample_name: 样本标的名称（通用性检查用）。
        active_infos: 现存 active 技能 [{name, description}]（近重复比对）。
            修订场景传 None/排除自身——自比必中，重复门只对首酿生效。
    """
    from ..skills_adapter import _parse_skill_md, _SKILL_NAME_RE

    t0 = time.perf_counter()
    report: Dict[str, Any] = {"verifier": VALIDATOR_VERSION, "skill_id": skill_id,
                              "cases": []}
    doc = skmd or ""
    meta, _body = _parse_skill_md(doc)
    facets = parse_facets(meta)
    base = dict(description=str(meta.get("description") or ""),
                facets=facets, declared_tools=extract_frontmatter_tools(doc))
    issues: List[str] = []
    name = meta.get("name")
    if not name:
        issues.append("缺少 name（规范必填）")
    else:
        name = str(name)
        if len(name) > 64:
            issues.append(f"name 超长 {len(name)}>64")
        if not _SKILL_NAME_RE.match(name):
            issues.append(f"name 含非法字符（仅小写字母/数字/连字符）: {name!r}")
    desc = meta.get("description")
    if not desc:
        issues.append("缺少 description（规范必填）")
    elif len(str(desc)) > 1024:
        issues.append(f"description 超长 {len(str(desc))}>1024")
    if issues:
        report["cases"].append({"gate": "format", "ok": False, "issues": issues})
        report["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return GateReport(ok=False, status="bad_format", issues=issues,
                          skmd=doc, report=report, **base)
    report["cases"].append({"gate": "format", "ok": True})

    # ② 契约校验：未知/元工具剔除并留痕（既有语义保留，不硬失败）
    stripped: List[str] = []
    if provider_tools is not None:
        before = extract_frontmatter_tools(doc)
        doc = strip_unknown_tools(doc, set(provider_tools))
        after = set(extract_frontmatter_tools(doc))
        stripped = [t for t in before if t not in after]
        if stripped:
            report["cases"].append({"gate": "contract", "ok": True,
                                    "stripped_tools": stripped})
        else:
            report["cases"].append({"gate": "contract", "ok": True})
    else:
        report["cases"].append({"gate": "contract", "ok": True, "skipped": "no provider"})

    # ③ 通用性/安全静态检查——硬失败（禁固化样本股票/用户数据）
    bad_refs = generic_violations(doc, sample_name)
    if bad_refs:
        report["cases"].append({"gate": "genericity", "ok": False, "refs": bad_refs[:5]})
        report["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return GateReport(ok=False, status="not_generic",
                          issues=[f"固化样本引用: {r}" for r in bad_refs[:5]],
                          skmd=doc, stripped_tools=stripped, report=report, **base)
    report["cases"].append({"gate": "genericity", "ok": True})

    # ④ 近重复检查——硬失败（不发布，返回已有技能或要求人工复核）
    dup = near_duplicate_of(meta, active_infos or [])
    if dup:
        report["cases"].append({"gate": "duplicate", "ok": False, "of": dup})
        report["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return GateReport(ok=False, status="duplicate",
                          issues=[f"与 active 技能近重复: {dup}"],
                          skmd=doc, stripped_tools=stripped, report=report, **base)
    report["cases"].append({"gate": "duplicate", "ok": True})

    report["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    declared = extract_frontmatter_tools(doc)
    return GateReport(ok=True, status="validated", skmd=doc,
                      stripped_tools=stripped, report=report,
                      description=base["description"], facets=base["facets"],
                      declared_tools=declared)


# ── 事务性发布（§7.6）──────────────────────────────────────────

def publish_skill_doc(skill_md_path, doc_text: str) -> None:
    """staging 写入 + 原子替换；失败不留半发布状态（旧 active 版本不受损）。"""
    path = os.fspath(skill_md_path)
    staging = path + ".staging"
    with open(staging, "w", encoding="utf-8") as f:
        f.write(doc_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(staging, path)   # 同文件系统原子替换


# ── registry DB 薄层（fail-soft；函数内惰性 import 便于测试注入）──

def _db():
    from app.utils.db import get_db_connection
    return get_db_connection()


def register_version(*, skill_id: str, version: str, description: str,
                     facets: dict, declared_tools: list, body_uri: str,
                     validation_state: str, validation_report: dict,
                     source_chain: str = "", semver: str = "",
                     display: str = "", source_kind: str = "auto") -> bool:
    """登记技能版本 + 注册行（幂等 upsert）。DB 不可用 ⇒ False（不抛、不回滚文件）。"""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_skill_registry
                    (skill_id, display_name, source_kind, lifecycle_state,
                     active_version, updated_at)
                VALUES (%s, %s, %s, 'active', %s, NOW())
                ON CONFLICT (skill_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    lifecycle_state = 'active',
                    active_version = EXCLUDED.active_version,
                    updated_at = NOW()
            """, (skill_id, display or skill_id, source_kind, version))
            cur.execute("""
                INSERT INTO qd_skill_versions
                    (skill_id, version, content_digest, description, facets,
                     declared_tools, body_uri, validation_state, validation_report,
                     source_chain, semver)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (skill_id, version) DO UPDATE SET
                    description = EXCLUDED.description,
                    facets = EXCLUDED.facets,
                    declared_tools = EXCLUDED.declared_tools,
                    body_uri = EXCLUDED.body_uri,
                    validation_state = EXCLUDED.validation_state,
                    validation_report = EXCLUDED.validation_report,
                    semver = EXCLUDED.semver
            """, (skill_id, version, version, description,
                  json.dumps(facets or {}, ensure_ascii=False),
                  json.dumps(list(declared_tools or []), ensure_ascii=False),
                  body_uri, validation_state,
                  json.dumps(validation_report or {}, ensure_ascii=False),
                  source_chain, semver))
            conn.commit()
            cur.close()
        return True
    except Exception as e:
        try:
            from ..log import logger
            logger.warning("[SkillRegistry] 版本登记失败（fail-soft，文件已发布）: %s", e)
        except Exception:
            pass
        return False


def count_auto_managed() -> Optional[int]:
    """自动管理（active/candidate）技能数；DB 不可用 ⇒ None（调用方保守放行）。"""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) AS n FROM qd_skill_registry
                WHERE source_kind = 'auto'
                  AND lifecycle_state IN ('active', 'candidate')
            """)
            row = cur.fetchone()
            cur.close()
            return int(row["n"]) if row else 0
    except Exception:
        return None


def capacity_available() -> bool:
    """容量门（§4.3）：计数失败（None）保守放行——宁松勿卡，留痕由调用方记。"""
    n = count_auto_managed()
    if n is None:
        return True
    return n < auto_skill_cap()


def get_active_version(skill_id: str) -> Optional[str]:
    """skill_id → active 版本 ID（§14.5 统一解析；无记录/DB 不可用 ⇒ None）。"""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT active_version FROM qd_skill_registry
                           WHERE skill_id = %s""", (skill_id,))
            row = cur.fetchone()
            cur.close()
            return row["active_version"] if row else None
    except Exception:
        return None


def get_active_versions(skill_ids) -> Optional[Dict[str, str]]:
    """批量取 {skill_id: active_version}（索引构建用；DB 不可用 ⇒ None，调用方 fail-open）。"""
    ids = [str(s) for s in (skill_ids or []) if str(s)]
    if not ids:
        return {}
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT skill_id, active_version FROM qd_skill_registry
                           WHERE skill_id = ANY(%s)""", (ids,))
            out = {r["skill_id"]: r["active_version"] for r in cur.fetchall()}
            cur.close()
            return out
    except Exception:
        return None


def get_lifecycle_states(skill_ids) -> Optional[Dict[str, str]]:
    """批量取 {skill_id: lifecycle_state}（硬约束过滤用；DB 不可用 ⇒ None，fail-open）。"""
    ids = [str(s) for s in (skill_ids or []) if str(s)]
    if not ids:
        return {}
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT skill_id, lifecycle_state FROM qd_skill_registry
                           WHERE skill_id = ANY(%s)""", (ids,))
            out = {r["skill_id"]: r["lifecycle_state"] for r in cur.fetchall()}
            cur.close()
            return out
    except Exception:
        return None


def recent_published_skill_ids(days: int) -> Optional[set]:
    """TTL 内**新发布**（首次登记）的技能 ID 集合（探索位资格真源，§14.2）。

    口径：`qd_skill_registry.created_at`（注册行创建时间）——修订只追加版本行、
    不刷新注册行 ⇒ 老技能修订后不会伪装成"新发布"重获探索资格。
    DB 不可用 ⇒ None（调用方 fail-closed：不探索，不抛）。
    """
    days = max(0, int(days or 0))
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT skill_id FROM qd_skill_registry
                WHERE created_at >= NOW() - make_interval(days => %s)
            """, (days,))
            out = {r["skill_id"] for r in cur.fetchall()}
            cur.close()
            return out
    except Exception:
        return None


def set_lifecycle(skill_id: str, state: str) -> bool:
    """生命周期迁移（合法性由 can_transition 预检；DB 不可用 ⇒ False）。"""
    if state not in LIFECYCLE_STATES:
        return False
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_skill_registry
                SET lifecycle_state = %s, updated_at = NOW()
                WHERE skill_id = %s
            """, (state, skill_id))
            conn.commit()
            cur.close()
        return True
    except Exception:
        return False
