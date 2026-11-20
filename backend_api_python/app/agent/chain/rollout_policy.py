"""灰度与回滚：按版本/比例/domain 分流、一键关闭、恢复上一组参数（§8.3）。

灰度只改变 consumption_mode 的作用强度，不改 evidence_status。
多规则叠加取最保守：任一规则不放行即降级 observe。
按请求比例用确定性哈希（request_id + salt）分桶，同一请求恒同结果。
配置缺失/异常/未预注册一律 observe（fail-closed）。

纯函数层：不碰 DB/LLM，不持久化快照，零生产调用方。
所有名单/比例/开关走版本化配置，不硬编码。
"""

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

SCHEMA_VERSION = "rollout-policy-config-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("rollout_policy_config.json")
CONFIG_PATH_ENV = "QD_ROLLOUT_POLICY_CONFIG"

OBSERVE = "observe"
MODES = ("observe", "shadow", "active")


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_mismatch")
    return cfg


def _decision(mode: str, allowed: bool, reason: str) -> Dict[str, Any]:
    return {"consumption_mode": mode, "allowed": allowed, "reason": reason}


def kill_switch(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """一键关闭：kill_switch=true 时强制 observe，关闭优先于一切放行。"""
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _decision(OBSERVE, False, "schema_mismatch")
    if cfg.get("kill_switch") is True:
        return _decision(OBSERVE, False, "kill_switch_on")
    return _decision(OBSERVE, True, "kill_switch_off")


def by_version(requested_mode: str, version: str,
               cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按版本灰度：仅 allowed_versions 内放行，其余降级 observe。"""
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _decision(OBSERVE, False, "schema_mismatch")
    allowed = cfg.get("allowed_versions")
    if not isinstance(allowed, list):
        return _decision(OBSERVE, False, "version_list_invalid")
    if requested_mode not in MODES:
        return _decision(OBSERVE, False, "invalid_mode")
    if requested_mode == OBSERVE:
        return _decision(OBSERVE, True, "already_observe")
    if version not in allowed:
        return _decision(OBSERVE, False, "version_not_allowed")
    return _decision(requested_mode, True, "version_allowed")


def _bucket(request_id: str, salt: str) -> float:
    seed = "%s|%s" % (request_id, salt)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def by_traffic(requested_mode: str, request_id: str,
               cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按请求比例灰度：确定性哈希分桶，同一请求恒同结果。"""
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _decision(OBSERVE, False, "schema_mismatch")
    fraction = cfg.get("traffic_fraction")
    if not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
        return _decision(OBSERVE, False, "fraction_unregistered")
    if requested_mode == OBSERVE:
        return _decision(OBSERVE, True, "already_observe")
    if not request_id:
        return _decision(OBSERVE, False, "missing_request_id")
    b = _bucket(request_id, str(cfg.get("traffic_salt") or ""))
    if b < fraction:
        return _decision(requested_mode, True, "in_bucket")
    return _decision(OBSERVE, False, "out_of_bucket")


def by_domain(requested_mode: str, *, domain: str = "",
              skill: str = "", cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按 domain 或 skill 灰度：命中任一允许名单才放行。"""
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _decision(OBSERVE, False, "schema_mismatch")
    doms, skills = cfg.get("allowed_domains"), cfg.get("allowed_skills")
    if not isinstance(doms, list) or not isinstance(skills, list):
        return _decision(OBSERVE, False, "domain_list_invalid")
    if requested_mode == OBSERVE:
        return _decision(OBSERVE, True, "already_observe")
    hit = (bool(domain) and domain in doms) or (bool(skill) and skill in skills)
    if hit:
        return _decision(requested_mode, True, "domain_or_skill_allowed")
    return _decision(OBSERVE, False, "domain_or_skill_not_allowed")


def restore_previous(previous: Dict[str, Any]) -> Dict[str, Any]:
    """恢复上一组确定性参数快照；坏了则 fail-closed 回 observe。"""
    if not isinstance(previous, dict):
        return {"ok": False, "consumption_mode": OBSERVE, "reason": "invalid_snapshot"}
    mode = previous.get("consumption_mode", OBSERVE)
    if mode not in MODES:
        return {"ok": False, "consumption_mode": OBSERVE, "reason": "invalid_mode"}
    return {"ok": True, "consumption_mode": mode,
            "config": copy.deepcopy(previous.get("config", {})),
            "reason": "restored"}


def effective_mode(requested_mode: str, *, version: str = "",
                   request_id: str = "", domain: str = "", skill: str = "",
                   cfg: Dict[str, Any]) -> Dict[str, Any]:
    """叠加全部灰度规则取最保守：任一不放行即 observe，不改 evidence_status。"""
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _decision(OBSERVE, False, "schema_mismatch")
    if cfg.get("enabled") is not True:
        result = _decision(OBSERVE, False, "disabled")
        result["evidence_status_unchanged"] = True
        result["blocked_by"] = ["enabled"]
        return result
    checks = [
        ("kill_switch", kill_switch(cfg)),
        ("version", by_version(requested_mode, version, cfg)),
        ("traffic", by_traffic(requested_mode, request_id, cfg)),
        ("domain", by_domain(requested_mode, domain=domain, skill=skill, cfg=cfg)),
    ]
    mode = requested_mode
    blocked_by = []
    for name, result in checks:
        if not result["allowed"] and result["consumption_mode"] == OBSERVE:
            blocked_by.append(name)
            mode = OBSERVE
    final = _decision(mode, mode != OBSERVE or requested_mode == OBSERVE,
                      "blocked_by:" + ",".join(blocked_by) if blocked_by else "all_allowed")
    final["evidence_status_unchanged"] = True
    final["blocked_by"] = blocked_by
    return final
