"""core/runtime/flows.py — day_flow 回测编排注册表 (2026-09-28 分层改造)

为什么要有这个模块
------------------
改造前 `core/runtime/evaluate.py` **内嵌 4 个策略专属回测 runner**
(v1 / relay3 / break / g56, 共 325 行, 占该文件 39%), 分派是闭集:

    if flow == "relay3": ...
    if flow == "break":  ...
    return _run_backtest_day_v1(...)     # ← 兜底

后果有两个, 第二个是致命的:
  1. **架构层含策略实现** —— 改 g56 口径要动 core, core 又反过来惰性
     `import_module("strategies.break"/".relay3")` 取特征 (层反转);
  2. **未知 flow 静默落 v1** —— yaml 里 `day_flow: brak` 拼错、或新策略忘了注册,
     引擎一声不响按 V1 口径算收益: 门是 g56 的门、编排是 v1 的编排, 回测数字
     看着像真的, 实盘选股全错。这类"静默错误"是本项目最难发现的一类。

改造后
------
本模块 = 一张**零策略知识**的表; 谁登记谁负责, core 只查表:

    strategies/<key>.py  ──import──>  flows.register_day_flow("<flow>", fn)   (策略自注册)
    core/runtime/evaluate.py ────────>  flows.get_day_flow(name)              (查表, 缺失 fail-fast)

与既有 `register_exit` / `register_strategy_funcs` 同一手法 (2026-09-26 P1-9
层反转已验证有效: exit_modes 现在也是空壳 + 注册表)。

runner 契约 (与改造前各私有函数**签名逐字一致**, 迁移是纯搬运, 不得改语义)
------------------------------------------------------------------------------
    fn(bars, code, spec, ev, board_type, stock_info, use_prefilter) -> list[dict]
        bars           : 日线序列 (升序, 含 time/open/high/low/close/volume)
        spec           : StrategySpec (门表 + params + market_spec)
        ev             : GateEvaluator 实例 (已绑定 code/board_type/stock_info)
        board_type     : 板块 ("main"/"gem"/"star"/...)
        stock_info     : 静态股本元数据 (circ_shares/total_shares) 或 None
        use_prefilter  : 是否施加 U1~U4 统一预过滤
        返回           : trades 列表 (逐笔等价于对应 strategies/<key>.py 生产链参考版)

两类 key
--------
    ("day", "<day_flow>")   ← meta.enumeration=day 的编排 (v1/relay3/break/g56)
    ("<enumeration>", "")   ← 其它枚举方式的编排 (如 limit_up, 由 dragon_callback 提供)

层依赖: 本模块顶层**不 import strategies** (否则 evaluate → flows → strategies 成环)。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)


class UnknownFlow(ValueError):
    """编排未登记 —— 显式抛出, 绝不静默回退其它策略口径。"""


# (enumeration, day_flow) → runner。名统一小写 (yaml 取值亦 .lower() 后再查)。
_FLOWS: Dict[Tuple[str, str], Callable[..., Any]] = {}

# 重复注册视为编码错误 (两个策略抢同一个 key = 编排会串台)。
# replace=True 仅供热重载/测试显式覆盖。
def _put(enumeration: str, day_flow: str, fn: Callable[..., Any], replace: bool) -> None:
    key = (str(enumeration or "").strip().lower(), str(day_flow or "").strip().lower())
    if not key[0]:
        raise ValueError("[flows] enumeration 不能为空")
    if not callable(fn):
        raise TypeError(f"[flows] register({key}): fn 不可调用")
    if key in _FLOWS and not replace:
        raise ValueError(
            f"[flows] flow {key} 重复注册 (已由 {_FLOWS[key].__module__} 登记); "
            f"确实要覆盖请传 replace=True")
    _FLOWS[key] = fn


def register_day_flow(name: str, fn: Callable[..., Any], replace: bool = False) -> None:
    """登记 meta.enumeration=day 的编排 runner (key = ("day", name))。"""
    if not str(name or "").strip():
        raise ValueError("[flows] register_day_flow: name 不能为空")
    _put("day", name, fn, replace)


def register_enum_flow(enumeration: str, fn: Callable[..., Any], replace: bool = False) -> None:
    """登记非 day 枚举方式的编排 runner (如 enumeration=limit_up)。"""
    _put(enumeration, "", fn, replace)


def available_flows() -> Tuple[str, ...]:
    """当前已注册的编排 (诊断/报错信息用), 形如 ('day:v1', 'limit_up')。"""
    ensure_flows()
    return tuple(sorted(f"{e}:{d}" if d else e for (e, d) in _FLOWS))


def _lookup(enumeration: str, day_flow: str = "") -> Optional[Callable[..., Any]]:
    key = (str(enumeration or "").strip().lower(), str(day_flow or "").strip().lower())
    if key in _FLOWS:
        return _FLOWS[key]
    ensure_flows()              # 表缺项时兜一次插件发现 (见下), 再查
    return _FLOWS.get(key)


_DISCOVERED = False


def ensure_flows() -> None:
    """注册表为空时**惰性触发一次**策略包 autodiscover (插件发现)。

    ⚠ 这是 core 侧对 strategies 的**唯一**动态引导: 本模块顶层零 strategies
    import (否则成环), 只在查表未命中时 import 一次策略包让各模块自注册。
    先例: `core/runtime/functions.ensure_gate_init()` 亦在加载期 autodiscover
    各策略私有门函数。二者同为"插件发现", 不是编译期依赖。
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    try:
        from app.market_cn.auto import strategies as _reg
        _reg.autodiscover()
    except Exception as e:      # 单策略模块损坏不该拖死查表调用方
        logger.error("[flows] 策略包 autodiscover 失败, 编排表可能不完整: %s", e)


def get_flow(enumeration: str, day_flow: str = "") -> Callable[..., Any]:
    """按 (meta.enumeration, meta.day_flow) 取编排 runner; **未登记则抛 UnknownFlow**。

    绝不回退到其它编排 —— 静默换口径比直接报错危险得多: 门是 g56 的门、编排是 v1 的
    编排, 回测数字看着像真的, 实盘选股全错。
    """
    fn = _lookup(enumeration, day_flow)
    if fn is None:
        want = f"{enumeration}:{day_flow}" if day_flow else enumeration
        raise UnknownFlow(
            f"编排 '{want}' 未注册 (已注册: {', '.join(available_flows()) or '空'})。"
            f" 请确认 strategies/<key>.yaml 的 meta 拼写, 且对应 strategies/<key>.py "
            f"已调用 register_day_flow/register_enum_flow 自注册")
    return fn
