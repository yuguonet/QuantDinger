# -*- coding: utf-8 -*-
"""编排层 ext (Ctx.ext) 提供者注册表 —— 无 IO、无策略知识的叶子模块。

为什么单独成文件 (B1, 2026-10-05)
----------------------------------
原清单 B1 点名: 「③ 策略专用代码 (g56) 住在通用展示管线里……新策略加 ext 时会自然照抄,
把策略代码继续堆进管线」。搬运前 `pipeline.py` 有 871 行 7 个职责, 越改越大
(2026-10-05 已到 1100+)。

抽出顺序必须**自底向上**, 否则循环导入:
    ext_registry  (本文件: 只有注册表, 零依赖)
        ↑
    ext_g56.py    (策略专用实现: 依赖本文件的 register_ext)
        ↑
    pipeline.py   (编排: import ext_g56 **只为触发注册**, 再不管它的实现)

⚠️ **必须 imported 才注册**: `register_ext` 是**副作用式注册** —— 没人 import 那个实现
   模块, 注册表就是空的, 而且**不报错**: `build_ext` 会走到
   `KeyError: meta.ext='g56' 未注册`。这条路径比抛 ImportError 更隐蔽。
   故 `pipeline` 里有一条显式的 `import ... ext_g56` 并在下方注明"非未使用导入"。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

# name -> fn(spec, code, bars, asof_date, cache) -> dict
EXT_PROVIDERS: Dict[str, Callable[..., Dict[str, Any]]] = {}

# name -> 该 ext 要求的最短日线根数 (见 required_min_len)
EXT_MIN_N: Dict[str, int] = {}


def register_ext(name: str, min_n: int = 1):
    """注册编排层 ext 提供者: fn(spec, code, bars, asof_date, cache) -> dict。

    min_n: 该 ext 要求的最短日线根数 (如 g56 的 G1 特征需 len>=35, 因 calc_macd
      短序列返回 None)。**声明在提供者处而非调用点** —— 管线统一用
      `required_min_len(spec)` 施加, 新策略/新调用方零改动即受保护。
    cache: 共享 BarsCache (可空) —— 供提供者复用长窗口缓存派生短窗口量 (如 g56 池),
      避免二次全市场加载; 提供者必须容忍 cache=None (退化为自取数)。
    """

    def _deco(fn):
        EXT_PROVIDERS[name] = fn
        EXT_MIN_N[name] = int(min_n)
        return fn

    return _deco


def providers() -> Dict[str, Callable[..., Dict[str, Any]]]:
    """只读视图 —— 供诊断/体检列出"当前有哪些 ext 可用"。"""
    return dict(EXT_PROVIDERS)
