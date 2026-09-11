#!/usr/bin/env python3
"""策略注册表 + autodiscover + 配置加载 (auto/strategies/__init__.py)

用途: Phase 3 起 scanner/monitor/store 经注册表分发, 替代硬编码策略分支。
关键设计点:
  - 注册: 策略模块内 `@register` 装饰 StrategyBase 子类, key 必须唯一;
  - autodiscover: importlib 遍历本目录 *.py (跳过 base/__init__), **per-module 容错** —
    单个模块 import 失败只记 CRITICAL 并跳过, 绝不拖死整个注册表 (L3 故障隔离);
  - 配置: auto/config.json 单文件 (当前唯一配置域=策略开关/限额/参数覆盖),
    优先级 config > 代码 default_params; 文件缺失/损坏时全部策略按 enabled=True 兜底。
易错点: is_enabled/daily_limit/params_override 对未注册 key 返回安全默认值, 不抛 KeyError。
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil

from app.market_cn.auto.strategies.base import (  # noqa: F401  (re-export 契约)
    ScanSpec, Signal, EntryDecision, ConfirmDecision, ExitDecision, StrategyBase,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_REGISTRY = {}


def register(cls):
    """类装饰器: 按 cls.key 注册策略实例。重复 key 视为编码错误, 直接抛出。"""
    key = getattr(cls, "key", "")
    if not key:
        raise ValueError(f"[strategies] {cls.__name__} 缺少 key, 无法注册")
    if key in _REGISTRY:
        raise ValueError(f"[strategies] 重复注册: {key}")
    _REGISTRY[key] = cls()
    return cls


def get_strategy(key):
    return _REGISTRY.get(key)


def all_strategies():
    return dict(_REGISTRY)


def autodiscover():
    """扫描本包全部策略模块并触发注册 (幂等: 已注册的 key 跳过重复 import 副作用)。

    per-module 容错: 任何一个模块损坏只跳过自身, 保证扫描主流程不被单策略拖死。
    """
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    for m in pkgutil.iter_modules([pkg_dir]):
        if m.name in ("base", "__init__"):
            continue
        try:
            importlib.import_module(f"{__name__}.{m.name}")
        except Exception as e:
            logger.critical("[strategies] 模块 %s 加载失败, 已跳过 (不影响其它策略): %s", m.name, e)
    return sorted(_REGISTRY)


# ================================================================
# config.json 加载 (auto/config.json: enabled/daily_limit/params 覆盖)
# ================================================================
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
_config_cache = None
_config_mtime = None


def load_config(refresh=False):
    """读取 config.json → {"strategies": {...}}; 缺失/损坏返回空配置 (全开+默认限额)。

    2026-09-10 (P1-1): mtime 变化自动重读, 策略启停/限额/参数改 config.json 不再需要重启后端。
    一致性约定: 调用方应每轮扫描开头取一次配置, 不在单轮扫描中途换配置。
    """
    global _config_cache, _config_mtime
    try:
        mt = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        mt = None
    if _config_cache is not None and not refresh and mt == _config_mtime:
        return _config_cache
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("root is not a dict")
    except FileNotFoundError:
        cfg = {}
    except Exception as e:
        logger.error("[strategies] config.json 解析失败, 按全默认兜底: %s", e)
        cfg = {}
    _config_cache = cfg if isinstance(cfg.get("strategies"), dict) else {"strategies": {}}
    _config_mtime = mt
    return _config_cache


def _strategy_cfg(key):
    return load_config().get("strategies", {}).get(key, {}) or {}


def is_enabled(key):
    """策略开关 (默认 True: 未写配置 = 开启, 与现状行为一致)。"""
    return bool(_strategy_cfg(key).get("enabled", True))


def daily_limit(key, default=5):
    """每日信号入库上限 (relay3 现值 2, 其余 5)。"""
    v = _strategy_cfg(key).get("daily_limit", default)
    return int(v) if v is not None else default


def params_override(key):
    """参数覆盖 dict (无则空 dict, 由 StrategyBase.merged_params 合并)。"""
    v = _strategy_cfg(key).get("params")
    return v if isinstance(v, dict) else {}


def family_of(key):
    """版本链 family 根 (2026-09-11 展示归一): 策略类 family 属性声明默认,
    config strategies.<key>.family 可覆盖 (与 enabled/params 同优先级惯例);
    空/缺省 = 自身 key (自成一族, 不参与跨策略去重)。"""
    v = _strategy_cfg(key).get("family")
    if v:
        return str(v)
    return getattr(get_strategy(key), "family", "") or key


def family_version(key):
    """链内版本号: 类属性 family_version 声明默认, config 覆盖; 缺省 1。
    同 (code, family, style) 重叠时扫描器取 version 最高者。"""
    v = _strategy_cfg(key).get("family_version")
    if v is not None:
        return int(v)
    return int(getattr(get_strategy(key), "family_version", 1) or 1)


def live_probe_enabled():
    """实盘扫描探针开关 (M1 实盘采集, 2026-09-11; 默认 True=未写即开启)。

    config.json 顶层 "live_probe": false 可关 (与 strategies 平级, 非 per-strategy)。
    开启时 run_scan 每策略产一份 sample 存档 (tmp/probes/<key>_live_<ts>.jsonl),
    只记判定步落点非空的 (code,day) — 特征完整、标签 censored (D+1 数据当时不存在,
    离线按 kline 回填; 与回测探针同 schema, sample_build 可直接消费)。
    """
    return bool(load_config().get("live_probe", True))
