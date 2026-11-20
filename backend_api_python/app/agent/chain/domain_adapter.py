"""chain/domain_adapter.py — §10.3 领域适配器注册表（纯函数层）。

共享层只按 domain/schema_version 查注册表，不写领域分支。首期只注册 finance，
其金融专属字段仍只进 outcome.adapter_data；adapter 缺失或 schema 不兼容时返回
unsupported_domain / schema_incompatible 并记为 coverage/exclusion，不用默认
resolver 兜底、不当负面 outcome。阈值与能力声明全在版本化配置，零生产调用方。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = "domain-adapter-config-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("domain_adapters.json")
CONFIG_PATH_ENV = "QD_DOMAIN_ADAPTER_CONFIG"

UNSUPPORTED = "unsupported_domain"
INCOMPATIBLE = "schema_incompatible"


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_mismatch")
    adapters = cfg.get("adapters")
    if not isinstance(adapters, dict) or not adapters:
        raise ValueError("adapters must be a non-empty object")
    for name, spec in adapters.items():
        if not isinstance(spec, dict) or not spec.get("schema_versions"):
            raise ValueError("invalid adapter: %s" % name)
        for key in ("task_classifier", "validate_snapshot", "resolve_outcome",
                    "evaluation_policy", "retrieval_policy", "retention_policy"):
            if not isinstance(spec.get(key), dict):
                raise ValueError("adapter %s missing %s" % (name, key))
    return copy.deepcopy(cfg)


def validate(domain: str, schema_version: Any, cfg: Dict[str, Any], *,
             snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """查注册表并做 snapshot 必需字段校验；失败一律返回明确原因。"""
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "reason": "schema_mismatch", "coverage_gap": ["config_schema_mismatch"]}
    spec = (cfg.get("adapters") or {}).get(str(domain or ""))
    if not isinstance(spec, dict):
        return {"ok": False, "reason": UNSUPPORTED, "coverage_gap": [UNSUPPORTED]}
    versions = spec.get("schema_versions") or []
    if str(schema_version or "") not in versions:
        return {"ok": False, "reason": INCOMPATIBLE, "coverage_gap": [INCOMPATIBLE]}
    required = list(spec["validate_snapshot"].get("required_snapshot_fields") or ())
    if snapshot is not None:
        missing = [key for key in required if snapshot.get(key) in (None, "")]
        if missing:
            return {"ok": False, "reason": "snapshot_incomplete",
                    "coverage_gap": ["snapshot_incomplete"]}
    return {"ok": True, "reason": "ok", "domain": str(domain),
            "domain_schema_version": str(spec.get("current_schema_version") or schema_version),
            "retrieval_policy": copy.deepcopy(spec.get("retrieval_policy") or {}),
            "capabilities": [k for k in spec if isinstance(spec[k], dict)],
            "coverage_gap": []}


def retrieval_policy(domain: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    spec = (cfg.get("adapters") or {}).get(str(domain or ""))
    if not isinstance(spec, dict):
        return {"ok": False, "reason": UNSUPPORTED, "policy": {}}
    return {"ok": True, "reason": "ok",
            "policy": copy.deepcopy(spec.get("retrieval_policy") or {})}
