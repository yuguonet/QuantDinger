"""chain/shadow_injection.py — P2 影子反馈最小落地：candidate 经验 shadow 注入。

【口径（2026-11-20 用户拍板）】
经验保持 candidate/observe 态注入决策上下文，只收集「曝光→outcome」配对数据
（link.experiment_arm=shadow），不宣称有效、不参与消费、不进任何升档判定。

【选人边界】
只从 shadow_injection_config.json 的白名单取；白名单外一律不注。配置缺失/
损坏/查询异常一律返回空列表（fail-closed）：注入是新增能力，宁可少注入也不
注入不可信内容。

【PIT】注入发生在决策 prompt 组装前，registered_at < 决策时刻天然成立；
本模块不存在事后回填路径。

【回滚】QD_SHADOW_INJECT=0 或配置 enabled=false 即停；删本文件 + 配置 +
qd_service/_prefetch 与曝光写入侧两处调用块即完全还原。
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any, Dict, List

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "shadow_injection_config.json")

_BLOCK_HEADER = "[经验影子注入]"
_BLOCK_NOTE = ("以下是经验库中的候选经验摘录，仅供参照对照（observe 态，未经验证，"
               "不作为结论依据，禁止直接照抄）。")


def _config() -> Dict[str, Any]:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def enabled() -> bool:
    if str(os.getenv("QD_SHADOW_INJECT", "1")).strip().lower() \
            in ("0", "false", "no", "off"):
        return False
    return bool(_config().get("enabled"))


def _tokens(text: str) -> set:
    raw = str(text or "")
    return {t.strip().lower() for t in raw.replace(";", ",").replace("；", ",").split(",")
            if t.strip()}


def select_for_decision(*, domain: str = "", entity_code: str = "") -> List[Dict[str, Any]]:
    """为本轮决策选出 shadow 注入经验项（纯查询 + 确定性选择，无写库）。"""
    cfg = _config()
    if not cfg.get("enabled") or str(os.getenv("QD_SHADOW_INJECT", "1")).strip().lower() \
            in ("0", "false", "no", "off"):
        return []
    dom = str(domain or "").strip().lower()
    if cfg.get("domain") and dom != str(cfg.get("domain")).strip().lower():
        return []
    try:
        max_inject = max(0, int(cfg.get("max_inject") or 0))
        max_chars = max(0, int(cfg.get("max_chars") or 0))
    except (TypeError, ValueError):
        return []
    ids = [str(x).strip() for x in (cfg.get("experience_ids") or []) if str(x).strip()]
    if not max_inject or not ids:
        return []
    try:
        from .evaluation_store import _db
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT experience_id, source_kind, source_ref, condition_key,
                       content_sha, normalized_content->>'excerpt' AS excerpt
                  FROM qd_agent_experiences
                 WHERE experience_id = ANY(%s)
                   AND evidence_status = 'candidate'
                   AND consumption_mode = 'observe'
                 ORDER BY experience_id
            """, (ids,))
            rows = cur.fetchall()
            cur.close()
    except Exception:
        return []
    pool: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r) if isinstance(r, dict) else {
            "experience_id": r[0], "source_kind": r[1], "source_ref": r[2],
            "condition_key": r[3], "content_sha": r[4], "excerpt": r[5]}
        text = str(d.get("excerpt") or "").strip()
        if not text:
            continue
        pool.append({
            "experience_id": str(d.get("experience_id") or ""),
            "source_kind": str(d.get("source_kind") or ""),
            "source_ref": str(d.get("source_ref") or ""),
            "condition_key": str(d.get("condition_key") or ""),
            "content_sha": str(d.get("content_sha") or ""),
            "text": text,
            "chars": len(text),
        })
    if not pool:
        return []
    ent = _tokens(entity_code)
    matched = [p for p in pool if ent and ent & _tokens(p.get("condition_key"))]
    rest = [p for p in pool if p not in matched]
    try:
        offset = date.today().toordinal() % max(1, len(rest))
    except Exception:
        offset = 0
    ordered = matched + rest[offset:] + rest[:offset]
    out: List[Dict[str, Any]] = []
    budget = max_chars or 10 ** 9
    for p in ordered:
        if len(out) >= max_inject or p["chars"] > budget:
            continue
        out.append(p)
        budget -= p["chars"]
    return out


def inject_block(items: List[Dict[str, Any]]) -> str:
    """把选中项拼成注入块文本；空列表返回空串。"""
    lines = [_BLOCK_HEADER, _BLOCK_NOTE]
    for it in items or []:
        lines.append("- %s" % str(it.get("text") or "").strip())
    body = "\n".join(x for x in lines if x)
    return body if len(items or []) else ""
