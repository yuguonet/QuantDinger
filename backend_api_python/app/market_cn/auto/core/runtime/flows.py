"""core/runtime/flows.py — 单日判定注册表 (scan_day 用)

演化
----
2026-09-28 分层改造: 原 `core/runtime/evaluate.py` 内嵌 4 个策略专属**全历史回测 runner**
(v1 / relay3 / break / g56, 共 325 行, 占该文件 39%) 与一条闭集分派:

    if flow == "relay3": ...
    if flow == "break":  ...
    return _run_backtest_day_v1(...)     # ← 兜底

后果有两个, 第二个是致命的:
  1. **架构层含策略实现** —— 改 g56 口径要动 core, core 又反过来惰性
     `import_module("strategies.break"/".relay3")` 取特征 (层反转);
  2. **未知 flow 静默落 v1** —— yaml 里 `day_flow: brak` 拼错、或新策略忘了注册,
     引擎一声不响按 V1 口径算收益: 门是 g56 的门、编排是 v1 的编排, 回测数字
     看着像真的, 实盘选股全错。这类"静默错误"是本项目最难发现的一类。
彼时解法 = 把回测 runner 纯搬运回各策略模块自注册 (`register_day_flow`), core 只查表。

终态② Step 3 (2026-10-09): 回测主路径已收敛到事件流折叠
(core/backtest.run_all → StrategyBase.backtest_stock 薄壳 → core.replay), 全历史回测
runner 表 (`_FLOWS` 半边) 随之退役。本模块现在只剩**一张表**: 单日判定 (`_SCAN_ONE`)。

现职责
------
scan_day (生产单日判定) 的 fn 注册表; 谁登记谁负责, core 只查表:

    strategies/<key>.py   ──import──>  flows.register_scan_one("<enum>", "<flow>", fn)
    core/runtime/evaluate.py ────────>  flows.get_scan_one(enum, flow)   (查表, 缺失 fail-fast)

与既有 `register_exit` / `register_strategy_funcs` 同一手法 (空壳 + 注册表, 防层反转)。

runner 契约
-----------
    fn(spec, ev, bars, i, board_type, stock_info) -> Signal | None
        spec           : StrategySpec (门表 + params + market_spec)
        ev             : GateEvaluator 实例 (已绑定 code/board_type/stock_info)
        bars           : 日线序列 (升序, 含 time/open/high/low/close/volume)
        i              : 决策日索引 (0-based 绝对)
        board_type     : 板块 ("main"/"gem"/"star"/...)
        stock_info     : 静态股本元数据 (circ_shares/total_shares) 或 None
        返回           : 单日判定结果 Signal 或 None
    只做**单日判定** (产 Signal, 不做出场模拟、不去重 —— 去重是回测/写库层的事)。

层依赖: 本模块顶层**不 import strategies** (否则 evaluate → flows → strategies 成环)。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)


class UnknownFlow(ValueError):
    """编排未登记 —— 显式抛出, 绝不静默回退其它策略口径。"""


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


# ================================================================
# 单日判定注册表（scan_day 用）
# ----------------------------------------------------------------
# 全历史 runner 的循环体 = 单日判定 + 出场模拟 + 去重。scan_day（生产链）只要
# 单日判定（产 Signal，不做出场模拟、不去重 —— 去重是回测/写库层的事）。故单日判定
# 单列为一张表，策略模块注册 fn(spec, ev, bars, i, board_type, stock_info) -> Signal|None。
# ================================================================
_SCAN_ONE: Dict[Tuple[str, str], Callable[..., Any]] = {}


def register_scan_one(enumeration: str, day_flow: str, fn: Callable[..., Any],
                      replace: bool = False) -> None:
    """登记单日判定 fn（scan_day 分派用）。key = (enumeration, day_flow)。"""
    key = (str(enumeration or "").strip().lower(), str(day_flow or "").strip().lower())
    if not key[0]:
        raise ValueError("[flows] register_scan_one: enumeration 不能为空")
    if not callable(fn):
        raise TypeError(f"[flows] register_scan_one({key}): fn 不可调用")
    if key in _SCAN_ONE and not replace:
        raise ValueError(f"[flows] scan_one {key} 重复注册")
    _SCAN_ONE[key] = fn


def get_scan_one(enumeration: str, day_flow: str = "") -> Callable[..., Any]:
    """取单日判定 fn；未登记则 fail-fast（绝不回退其它策略口径）。"""
    key = (str(enumeration or "").strip().lower(), str(day_flow or "").strip().lower())
    fn = _SCAN_ONE.get(key)
    if fn is None:
        ensure_flows()
        fn = _SCAN_ONE.get(key)
    if fn is None:
        want = f"{enumeration}:{day_flow}" if day_flow else enumeration
        raise UnknownFlow(f"单日判定 '{want}' 未注册")
    return fn
