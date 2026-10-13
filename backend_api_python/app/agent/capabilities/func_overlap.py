# -*- coding: utf-8 -*-
"""capabilities/func_overlap.py — 启动期「同功能」筛选 + 缓存

用户裁定（2026-09-25）：优先级是 **工具层 > 能力层**，判据应是**功能等价**，
名字像不像只是代理。做法：
  1. agent 启动时对 工具层 +（已开启的）能力层 做一次同功能筛选
  2. 结果落缓存（指纹未变则直接复用），**消息路径不再扫描**
  3. 代码启发式先判「确定等价 / 确定不等价」；**不确定对**才可选打一次 LLM

缓存文件：`overlap_cache.json`（与 admission.json 同级）。
指纹 = 域工具(name+doc 首行) + 准入条目(module+name) 的哈希——工具面一变自动重算。

易错点：
  - 不要把 LLM 放进每条消息路径；只在启动期、且仅对 unsure 对
  - admission 的 `superseded_by` 是人工权威，优先于一切启发式
  - 能力层关闭时不注册，也不必扫
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CACHE_PATH = Path(__file__).resolve().parent / "overlap_cache.json"

# 与 loader 共用（避免两套名字规则）
from .loader import (  # noqa: E402
    _GENERIC_NAMES,
    _shadows_domain_tool,
    near_dup_tool_names,
)


def _doc_first(fn) -> str:
    try:
        return (inspect.getdoc(fn) or "").strip().split("\n")[0][:160]
    except Exception:
        return ""


def _sig_text(fn) -> str:
    try:
        return str(inspect.signature(fn))
    except Exception:
        return ""


def fingerprint_inventory(domain_tools: List[Dict[str, str]],
                          cap_entries: List[Dict[str, str]]) -> str:
    """工具面指纹：任一名字/文档变化 → 缓存失效。"""
    h = hashlib.sha1()
    for item in sorted(domain_tools, key=lambda x: x["name"]):
        h.update(f"D|{item['name']}|{item.get('doc', '')}\n".encode("utf-8", "replace"))
    for item in sorted(cap_entries, key=lambda x: (x["module"], x["name"])):
        h.update(f"C|{item['module']}|{item['name']}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def code_judge_pair(cap: Dict[str, str], tool: Dict[str, str]) -> Tuple[str, str]:
    """代码启发式：返回 (verdict, reason)。

    verdict: "same" | "different" | "unsure"
    """
    cn, tn = cap["name"], tool["name"]
    if cn == tn:
        return "same", "同名"
    # 只看**能力名**是否过泛。原先 `or tn in _GENERIC_NAMES` 是 bug：
    # 域工具里有 kline_tools.daily，导致 69 项能力全部被误判让位。
    if cn in _GENERIC_NAMES:
        return "same", "能力名过泛"
    if near_dup_tool_names(cn, tn):
        return "same", "近重名"
    # 签名同形 + 文档关键词重合 → 功能可能等价
    cap_doc = (cap.get("doc") or "").lower()
    tool_doc = (tool.get("doc") or "").lower()
    keys = ("k线", "日线", "行情", "报价", "资金", "涨停", "板块", "指数",
            "kline", "quote", "fund", "limit", "sector", "index")
    shared = [k for k in keys if k in cap_doc and k in tool_doc]
    if len(shared) >= 2:
        return "unsure", f"文档共同词{shared}"
    if shared:
        return "unsure", f"弱共同词{shared}"
    return "different", "无重合信号"


def llm_judge_pairs(unsure: List[Tuple[Dict, Dict]]) -> Dict[str, str]:
    """对 unsure 对做一次批量 LLM 判定（启动期，可选）。

    Returns: {f"{cap_name}|{tool_name}": "same"|"different"}
    失败/无 LLM → 全部按 "different"（宁可多留能力，也不误杀独立能力）。

    【2026-10-13 P1】两处改动：
    · LLM 出口改为 `app.agent.llm.complete()`（mimo models），旧的 `llm/`
      自建层已退役，`llm=` 入参一并去掉（从来没有外部传过）。
    · **修了一个从未生效的 bug**：旧代码 `resp = llm.generate(...)` 没有 await，
      拿到的是 coroutine，`resp.content` 当场 AttributeError 被 except 吞掉，
      于是这条判定永远走"全部 different"。换同步 `complete()` 后它才真正工作。
      ⚠ 行为变化：启动期能力重叠判定现在真会判出 "same"（让位）。若观察到
      误杀，先查 `load_or_build(use_llm=False)` 的降级开关，不要改判定逻辑。
    """
    if not unsure:
        return {}
    try:
        from app.agent.llm import resolve_provider
        resolve_provider()  # 配置不全（无 key / 未知 provider）会抛，走下面 except
    except Exception as e:
        logger.info("[func_overlap] 无 LLM，unsure 对按 different 保留: %s", e)
        return {f"{c['name']}|{t['name']}": "different" for c, t in unsure}

    lines = []
    for i, (c, t) in enumerate(unsure, 1):
        lines.append(
            f"{i}. capability `{c['name']}` doc={c.get('doc','')[:80]!r} "
            f"vs tool `{t['name']}` doc={t.get('doc','')[:80]!r}"
        )
    prompt = (
        "判断下列「能力函数」与「域工具」是否功能等价（做同一件事/同一数据）。\n"
        "只输出 JSON 数组，每项 {\"i\": 序号, \"same\": true|false}。\n"
        "把握不大一律 false。\n\n" + "\n".join(lines)
    )
    try:
        from app.agent.llm import complete
        resp = complete([{"role": "user", "content": prompt}])
        text = (resp.content or "").strip()
        # 捞 JSON 数组
        a, b = text.find("["), text.rfind("]")
        arr = json.loads(text[a:b + 1]) if a >= 0 and b > a else []
        out = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            i = int(item.get("i") or 0) - 1
            if 0 <= i < len(unsure):
                c, t = unsure[i]
                out[f"{c['name']}|{t['name']}"] = "same" if item.get("same") else "different"
        # 漏判的补 different
        for c, t in unsure:
            out.setdefault(f"{c['name']}|{t['name']}", "different")
        return out
    except Exception as e:
        logger.warning("[func_overlap] LLM 判定失败，unsure 按 different: %s", e)
        return {f"{c['name']}|{t['name']}": "different" for c, t in unsure}


def build_report(domain_tools: List[Dict[str, str]],
                 cap_entries: List[Dict[str, str]],
                 use_llm: bool = True) -> Dict[str, Any]:
    """产出 {capability_name: {decision, superseded_by, reason}}。

    decision: "shadowed"（让位）| "keep"
    """
    report: Dict[str, Any] = {}
    unsure: List[Tuple[Dict, Dict]] = []

    for cap in cap_entries:
        # 人工权威：admission.superseded_by
        sb = (cap.get("superseded_by") or "").strip()
        if sb:
            report[cap["name"]] = {
                "decision": "shadowed", "superseded_by": sb, "reason": "admission.superseded_by",
            }
            continue

        best: Optional[Tuple[str, str]] = None  # (verdict, tool_name, reason) via dict
        hit_tool = ""
        hit_reason = ""
        pending: List[Dict] = []

        for tool in domain_tools:
            verdict, reason = code_judge_pair(cap, tool)
            if verdict == "same":
                best = "same"
                hit_tool = tool["name"]
                hit_reason = reason
                break
            if verdict == "unsure":
                pending.append(tool)

        if best == "same":
            report[cap["name"]] = {
                "decision": "shadowed", "superseded_by": hit_tool, "reason": hit_reason,
            }
            continue

        if pending:
            for tool in pending:
                unsure.append((cap, tool))
            report[cap["name"]] = {
                "decision": "keep", "superseded_by": "", "reason": "pending_llm",
            }
        else:
            report[cap["name"]] = {
                "decision": "keep", "superseded_by": "", "reason": "no_overlap",
            }

    if use_llm and unsure:
        verdicts = llm_judge_pairs(unsure)
        for cap_name, meta in list(report.items()):
            if meta.get("reason") != "pending_llm":
                continue
            for tool in domain_tools:
                v = verdicts.get(f"{cap_name}|{tool['name']}", "different")
                if v == "same":
                    report[cap_name] = {
                        "decision": "shadowed",
                        "superseded_by": tool["name"],
                        "reason": "llm_same",
                    }
                    break
            else:
                report[cap_name] = {
                    "decision": "keep", "superseded_by": "", "reason": "llm_different",
                }

    return report


def load_cache() -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("fingerprint") and isinstance(data.get("report"), dict):
            return data
    except Exception:
        return None
    return None


def save_cache(fingerprint: str, report: Dict[str, Any], used_llm: bool) -> None:
    try:
        _CACHE_PATH.write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint,
                    "used_llm": used_llm,
                    "report": report,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[func_overlap] 缓存写入失败: %s", e)


def collect_domain_meta(provider) -> List[Dict[str, str]]:
    """工具层元数据（common + 可选域）。"""
    out = []
    names = set()
    try:
        names |= set(provider.list_by_domain("common"))
        for d in provider.get_domains() or []:
            names |= set(provider.list_by_domain(d))
    except Exception:
        names = set(provider.get_tool_names() or [])
    for n in sorted(names):
        fn = provider.get(n)
        out.append({"name": n, "doc": _doc_first(fn), "sig": _sig_text(fn)})
    return out


def collect_cap_meta_from_admission(admitted: List[Tuple]) -> List[Dict[str, str]]:
    """admission 准入条目元数据（含可选 superseded_by）。"""
    # admitted: [(mod, name, timeout, max_chars)] 或带 superseded_by 的 dict
    out = []
    for ent in admitted:
        if isinstance(ent, dict):
            out.append({
                "module": ent.get("module", ""),
                "name": ent.get("name", ""),
                "doc": ent.get("doc", ""),
                "superseded_by": ent.get("superseded_by", ""),
            })
        else:
            mod, name = ent[0], ent[1]
            out.append({"module": mod, "name": name, "doc": "", "superseded_by": ""})
    return out


def load_or_build(provider, admitted, use_llm: bool = True, force: bool = False) -> Dict[str, Any]:
    """启动期入口：指纹未变用缓存，否则重算（可选 LLM）。"""
    domain_meta = collect_domain_meta(provider)
    cap_meta = collect_cap_meta_from_admission(admitted)
    fp = fingerprint_inventory(domain_meta, cap_meta)

    if not force:
        cached = load_cache()
        if cached and cached.get("fingerprint") == fp:
            logger.info("[func_overlap] 缓存命中（%s…），跳过重扫", fp[:8])
            return cached["report"]

    used_llm = bool(use_llm)
    report = build_report(domain_meta, cap_meta, use_llm=use_llm)
    save_cache(fp, report, used_llm=used_llm)
    n_shadow = sum(1 for v in report.values() if v.get("decision") == "shadowed")
    logger.info("[func_overlap] 同功能筛选完成: %d 能力, 让位 %d, llm=%s",
                len(report), n_shadow, used_llm)
    return report
