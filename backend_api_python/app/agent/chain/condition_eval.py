"""chain/condition_eval.py — 经验 condition 的可计算谓词求值（方案 §8.4①）。

把经验的四段式 condition 从自由文本变成可机读、可匹配的受限结构：每个条件
形如 (key, op, value)，op 只允许 eq/in/gt/lt；组合只用显式的 and/or。
输出三值 applicable / not_applicable / unknown —— 取值缺失、key 越出白名单、
不可计算一律 unknown（**不猜、不当 false**），unknown 不得进入匹配。

硬纪律：key 白名单与允许 op 全来自版本化配置（代码不硬编码 key/op）；
只求值显式 dict/list 结构，不解析任意表达式字符串、不执行代码；纯函数，
不碰 DB/LLM、不接生产调用方。可观测：可计算比例、越界拒绝数、白名单版本。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = "condition-whitelist-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("condition_whitelist.json")
CONFIG_PATH_ENV = "QD_CONDITION_WHITELIST"

APPLICABLE = "applicable"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"

_LOGICAL_AND = "and"
_LOGICAL_OR = "or"


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("condition whitelist must be a JSON object")
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch: %r != %r" % (
            cfg.get("schema_version"), SCHEMA_VERSION))
    if not isinstance(cfg.get("keys"), dict) or not cfg["keys"]:
        raise ValueError("whitelist.keys must be a non-empty object")
    if not isinstance(cfg.get("ops"), list) or not cfg["ops"]:
        raise ValueError("whitelist.ops must be a non-empty list")
    return copy.deepcopy(cfg)


def _type_ok(value: Any, declared: str) -> bool:
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "str":
        return isinstance(value, str)
    return True


def eval_atom(atom: Any, ctx: Dict[str, Any],
              cfg: Dict[str, Any], obs: Optional[Dict[str, Any]] = None) -> str:
    """求单个 (key, op, value) 三元组；越界/缺值/非法一律 unknown。"""
    def _observe(reason: str) -> str:
        if obs is not None:
            obs["atoms"] = obs.get("atoms", 0) + 1
            obs.setdefault("reasons", []).append(reason)
        return UNKNOWN

    if not isinstance(atom, dict):
        return _observe("malformed_atom")
    key, op, target = atom.get("key"), atom.get("op"), atom.get("value")
    keys = cfg.get("keys", {})
    if key not in keys:
        if obs is not None:
            obs["out_of_whitelist"] = obs.get("out_of_whitelist", 0) + 1
        return _observe("key_not_whitelisted")
    if op not in cfg.get("ops", []):
        return _observe("op_not_allowed")
    if key not in ctx or ctx[key] is None:
        return _observe("value_missing")
    actual = ctx[key]
    declared = keys[key]
    if not _type_ok(actual, declared):
        return _observe("type_mismatch")
    if op == "in":
        if not isinstance(target, (list, tuple, set)) or not all(
                _type_ok(t, declared) for t in target):
            return _observe("type_mismatch")
    elif not _type_ok(target, declared):
        return _observe("type_mismatch")

    if obs is not None:
        obs["atoms"] = obs.get("atoms", 0) + 1
        obs["computable"] = obs.get("computable", 0) + 1
    try:
        if op == "eq":
            hit = actual == target
        elif op == "in":
            hit = actual in target if isinstance(target, (list, tuple, set)) else False
        elif op == "gt":
            hit = actual > target
        elif op == "lt":
            hit = actual < target
        else:
            return UNKNOWN
    except TypeError:
        return _observe("op_type_error")
    return APPLICABLE if hit else NOT_APPLICABLE


def _logical_children(node: Any) -> Optional[tuple]:
    if not isinstance(node, dict):
        return None
    for key in (_LOGICAL_AND, _LOGICAL_OR):
        if key in node:
            value = node[key]
            return (key, value if isinstance(value, list) else [])
    return None


def _combine(truths: list, logical: str) -> str:
    if not truths:
        return UNKNOWN
    if logical == _LOGICAL_AND:
        if NOT_APPLICABLE in truths:
            return NOT_APPLICABLE
        if UNKNOWN in truths:
            return UNKNOWN
        return APPLICABLE
    if logical == _LOGICAL_OR:
        if APPLICABLE in truths:
            return APPLICABLE
        if UNKNOWN in truths:
            return UNKNOWN
        return NOT_APPLICABLE
    return UNKNOWN


def structural_check(cond: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """只查结构可计算性（key 在白名单 / op 合法 / 类型结构对），不依赖具体取值。

    供 needs_data 判定用：缺值产生的 unknown 不属于 needs_data（那是情境匹配范畴），
    只有 key 越界（可经白名单扩展解决）或结构非法（当场拒绝）才是不可计算候选。
    """
    stat = {"atoms": 0, "out_of_whitelist": 0, "invalid": 0}

    def _walk(node: Any) -> None:
        logical = _logical_children(node)
        if logical is not None:
            op, children = logical
            if op in node and not isinstance(node[op], list):
                stat["invalid"] += 1
            for x in children:
                _walk(x)
            return
        if isinstance(node, list):
            for x in node:
                _walk(x)
            return
        if not isinstance(node, dict):
            stat["invalid"] += 1
            return
        stat["atoms"] += 1
        key, op, target = node.get("key"), node.get("op"), node.get("value")
        if key not in cfg.get("keys", {}):
            stat["out_of_whitelist"] += 1
            return
        declared = cfg["keys"][key]
        if op not in cfg.get("ops", []):
            stat["invalid"] += 1
            return
        if op == "in":
            if not isinstance(target, (list, tuple, set)) or not all(
                    _type_ok(t, declared) for t in target):
                stat["invalid"] += 1
        elif not _type_ok(target, declared):
            stat["invalid"] += 1

    _walk(cond)
    return stat


def eval_condition(cond: Any, ctx: Dict[str, Any],
                   cfg: Dict[str, Any]) -> Dict[str, Any]:
    """求值一条 condition（显式 and/or 树），返回三值 + 可观测统计。"""
    obs: Dict[str, Any] = {"whitelist_version": cfg.get("schema_version"),
                           "atoms": 0, "computable": 0,
                           "out_of_whitelist": 0, "reasons": []}
    if cond is None or cond == {}:
        return {"verdict": UNKNOWN, "observable": obs}

    def _walk(node: Any) -> str:
        logical = _logical_children(node)
        if logical is not None:
            op, children = logical
            if op in node and not isinstance(node[op], list):
                obs["reasons"].append("malformed_logical")
            return _combine([_walk(x) for x in children], op)
        if isinstance(node, list):
            return _combine([_walk(x) for x in node], _LOGICAL_AND)
        return eval_atom(node, ctx, cfg, obs)

    verdict = _walk(cond)
    obs["computable_ratio"] = (
        obs["computable"] / obs["atoms"] if obs["atoms"] else 0.0)
    return {"verdict": verdict, "observable": obs}
