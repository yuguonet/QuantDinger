#!/usr/bin/env python3
"""策略注册表 + autodiscover + 配置加载 (auto/strategies/__init__.py)

用途: Phase 3 起 scanner/monitor/store 经注册表分发, 替代硬编码策略分支。
关键设计点:
  - 注册: 策略模块内 `@register` 装饰 StrategyBase 子类, key 必须唯一;
  - autodiscover: importlib 遍历本目录 *.py (跳过 base/__init__), **per-module 容错** —
    单个模块 import 失败只记 CRITICAL 并跳过, 绝不拖死整个注册表 (L3 故障隔离);
  - 开关 (2026-10-09 终态②/A-D2): **事实源 = 策略宏 `<key>.yaml` 的 `meta.enabled`**
    (§3.5「改宏即开关」) —— 宏内一行即上下线, 不再去另一个配置域改。无 yaml 的
    策略回退 `config.json strategies.<key>.enabled` (2026-10-09 P6-6/7 退役
    v1/relay3/lead_chase 后当前无此类策略, 该分支为通用兜底); 两边都无 ⇒ False。
  - 配置: auto/config.json (label/限额/胜率/params 覆盖等**元数据**),
    优先级 config > 代码 default_params; 文件缺失/损坏时按默认兜底。
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
# 策略开关 —— 单一事实源 = 策略宏 <key>.yaml 的 `meta.enabled` (§3.5)
# ----------------------------------------------------------------
# 「改宏即开关」: 上线/下线只改策略宏里的一行, 与 label/params 的宏内自描述一致。
# config.json 的 `strategies.<key>.enabled` 退为**无宏策略的兜底** (通用兜底分支,
# 2026-10-09 P6-6/7 退役 v1/relay3/lead_chase 后当前无此类);
# 有 yaml 的策略一律以 meta.enabled 为准 (忽略 config 同名键)。
# 缺省 False: 沿用 2026-09-15 安全语义 —— 未显式声明 = 不进实盘。
# ================================================================
_STRATEGY_DIR = os.path.dirname(os.path.abspath(__file__))
_YAML_DOC_CACHE = {}         # key -> (mtime, doc|None)  策略宏解析缓存 (开关/参数共用)


def _yaml_doc(key):
    """读策略宏 `<key>.yaml` 的解析结果 (mtime 缓存); 无 yaml / 解析失败 → None。

    带 mtime 缓存 (与 load_config 同法): 改 yaml 后无需重启后端, 下一轮读即生效;
    解析失败返回 None (交调用方回退), 绝不因一个坏 yaml 抛死调用方。
    """
    path = os.path.join(_STRATEGY_DIR, f"{key}.yaml")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None, None
    hit = _YAML_DOC_CACHE.get(key)
    if hit is not None and hit[0] == mt:
        return hit[1], mt
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        if not isinstance(doc, dict):
            doc = None
    except Exception as e:
        logger.error("[strategies] %s.yaml 解析失败, 回退 config: %s", key, e)
        doc = None
    _YAML_DOC_CACHE[key] = (mt, doc)
    return doc, mt


def _yaml_meta_enabled(key):
    """读策略宏 `<key>.yaml` 的 `meta.enabled`; 无 yaml / 无该键 / 解析失败 → None。"""
    doc, _ = _yaml_doc(key)
    if doc is None:
        return None
    raw = (doc.get("meta") or {}).get("enabled")
    return bool(raw) if raw is not None else None


def yaml_params(key):
    """读策略宏 `<key>.yaml` 的 `params` 段; 无 yaml / 无该键 / 解析失败 → None。

    这是**参数（params）的单一事实源**（目标态 §2/§3.1）: 策略宏里的 params 段,
    判定（`spec.params`）/出场/生产扫描（`StrategyBase.params`）都读它。
    """
    doc, _ = _yaml_doc(key)
    if doc is None:
        return None
    raw = doc.get("params")
    return raw if isinstance(raw, dict) else None


# ================================================================
# config.json 加载 (auto/config.json: label/daily_limit/params 等元数据)
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
    """策略开关 (§3.5「改宏即开关」): yaml `meta.enabled` 权威 → config 兜底 → 缺省 False。

    优先级 (2026-10-09 终态②/A-D2 定):
      ① `<key>.yaml` 的 `meta.enabled` (有 yaml 者一律以此为准 —— 单一事实源)
      ② `config.json strategies.<key>.enabled` (无 yaml 策略的兜底; 当前无此类)
      ③ False (两边都未声明 = 不进实盘)

    2026-09-15 事故语义保留: 缺省 False ⇒ 无配置的磁盘插件不会被 autodiscover 捡回实盘;
    新策略实盘 = 在自己的 yaml `meta` 里写 `enabled: true` (与规则同处一文件, 改宏即生效)。
    """
    v = _yaml_meta_enabled(key)
    if v is not None:
        return v
    return bool(_strategy_cfg(key).get("enabled", False))


def daily_limit(key, default=20):
    """每日信号入库上限。2026-09-28: 默认 20; 事实源=config.json strategies.<key>.daily_limit。"""
    v = _strategy_cfg(key).get("daily_limit", default)
    return int(v) if v is not None else default


def params_override(key):
    """参数覆盖 dict (无则空 dict, 由 StrategyBase.params 合并)。

    **单一事实源 = 策略宏 `<key>.yaml` 的 `params` 段**（目标态 §2/§3.1/D4,
    2026-10-09 收敛）: 判定（`spec.params`）/出场/生产扫描（`scan_signals →
    StrategyBase.params`）/回测/参数网格都读同一份。

    优先级: yaml `params` → config.json `strategies.<key>.params`（**无 yaml 策略
    的兜底**, 当前无此类）→ {}。日线策略的 config params 已在 B-D3 清空;
    盘中策略（knife/tail）的 config params 于 D4 一并清空（消除「盘中双源」）。
    """
    v = yaml_params(key)
    if v is None:
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


def present_persist_settings():
    """展示层切片每日落盘开关 + 冷切片暖机日数 (P5-③ 前置, 2026-10-07)。

    config.json 顶层 (与 strategies / live_probe 平级, 非 per-strategy):
        "present_persist": {"enabled": true, "warmup": 25}
    缺键 / 类型不符 ⇒ {"enabled": False, "warmup": 0} —— **缺省关**, 生产行为零变化。
    warmup 只在**冷切片**上生效 (见 auto/present_daily.py 模块头 ⚠⚠), 故设一次可长期留着。

    ★ 为什么用 config 而不是环境变量: config.json 是本项目的**唯一配置域**且被 git 跟踪
      ⇒ 影子期这个临时阶段开关在代码里可见、可复核、可回滚; 环境变量只活在部署机的
      `.env`(未跟踪) 里, 开了什么无人可查 (违反「禁并存式过渡」)。
    """
    v = load_config().get("present_persist")
    if not isinstance(v, dict):
        return {"enabled": False, "warmup": 0}
    try:
        warm = max(0, int(v.get("warmup") or 0))
    except (TypeError, ValueError):
        warm = 0
    return {"enabled": bool(v.get("enabled", False)), "warmup": warm}


def scan_writer():
    """生产链判定引擎开关 (P2, 2026-10-08)。

    config.json 顶层 (与 present_persist 平级):
        "scan_writer": "scan_day" | "record" | "scan"
    缺键 / 非法 ⇒ "record" —— **缺省 = 折叠 Record.ready (P5-③ 现状)**, 生产行为零变化。
      · "scan_day" = 门表引擎 scan_day (P2 目标态, 门表唯一规则源)
      · "record"   = 折叠内核 persist_days → Record.ready (P5-③ 现状)
      · "scan"     = 折叠内核 scan_days (旧回滚位)
    """
    v = load_config().get("scan_writer")
    return v if v in ("scan_day", "record", "scan") else "record"


def monitor_progress_settings():
    """monitor 15:01 确认的判定源开关 (P5-④ 第一步, 2026-10-07)。

    config.json 顶层 (与 present_persist 平级):
        "monitor_progress": {"enabled": true}
    缺键 / 类型不符 ⇒ {"enabled": False} —— **缺省关**, 走旧的 `confirm_decision`。

    ★ 为什么**不复用** `present_persist.enabled`: P5 要求每个子步骤**独立可回滚** ——
      ③ (切 writer) 与 ④ (三决策退役) 是两个回滚位, 共用一个开关就分不开"是哪个
      步骤改坏了"。本步依赖③的切片数据 (无切片 ⇒ 拿不到判定 ⇒ 调用方回退旧路径),
      但**开关本身**必须独立。
    """
    v = load_config().get("monitor_progress")
    if not isinstance(v, dict):
        return {"enabled": False}
    return {"enabled": bool(v.get("enabled", False))}


def ref_tree_settings():
    """外部旧版参照树根 (§2.2.3 切口 9, 2026-10-08) —— 删除旧代码的可逆保障。

    config.json 顶层 (与 present_persist 平级):
        "ref_tree": {"root": "<旧版树根>"}
    缺键 / 类型不符 ⇒ {"root": None} —— 对拍组保持 skip (**skip ≠ 通过**)。

    ★ 为什么放 config 而不是只靠环境变量: 参照树是 P6 删除旧代码的**唯一可逆保障**,
      路径必须可复核; `AUTO_SLIM_REF` 只活在部署机 shell 里, 换机/重启即失效 ⇒
      对拍组静默 skip (无证据, 却看着像"没跑过也没关系")。**环境变量保留为本地覆盖**
      (优先级高于 config), 便于临时换一棵参照树对比。
    """
    v = load_config().get("ref_tree")
    if not isinstance(v, dict):
        return {"root": None}
    root = v.get("root")
    return {"root": str(root) if root else None}


def market_env_of(key):
    """策略的大盘环境门模式 (config.json 优先; 默认 off=全通)。

    2026-09-28 用户裁定: 放 config 最简单; 默认全通。
    返回: "off" | "trend" | "counter"
    """
    v = _strategy_cfg(key).get("market_env")
    if v in ("off", "trend", "counter"):
        return v
    # 类属性兜底 (向后兼容)
    try:
        s = get_strategy(key)
        v = getattr(s, "market_env", "off")
        return v if v in ("off", "trend", "counter") else "off"
    except Exception:
        return "off"


# ================================================================
# 启动时注入：出场派发器的懒加载钩子 (P1-7)
# ----------------------------------------------------------------
# `core.exit_modes.run_exit` 在 EXIT_MODES 为空时需要补一次 autodiscover
# （策略模块 import 时才会 register_exit）。原先它在函数体内
# `from ...strategies import autodiscover` ⇒ **core → strategies 反向 import**，
# 违反设计 §2.2 依赖方向。改为**依赖注入**：core 只留分派与协议，
# 由本模块（合法方向 strategies → core）在 import 尾部注册钩子。
# 幂等：注册的是同一个函数对象，重复 import 不会重复生效。
try:  # pragma: no cover - 防御性：core 缺失时不阻断策略加载
    from app.market_cn.auto.core.exit_modes import set_exit_bootstrap
    set_exit_bootstrap(autodiscover)
except Exception:
    pass
