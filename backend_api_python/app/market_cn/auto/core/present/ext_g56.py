# -*- coding: utf-8 -*-
"""g56 的编排层 ext —— **策略专用代码, 从 pipeline.py 搬出** (B1, 2026-10-05)。

为什么搬出来
------------
原清单 B1: 「③ 策略专用代码 (g56) 住在通用展示管线里。虽有 register_ext 注册机制,
但实现仍在管线文件内……新策略加 ext 时会自然照抄, 把策略代码继续堆进管线」。

内容与 pipeline 里逐字相同, 只是换了住处 + 注解类型不再引用 pipeline 的类
(避免 pipeline ↔ ext_g56 循环导入: 本文件只依赖叶子层的 ext_registry)。

提供两件东西:
  · `_ext_g56`          : Ctx.ext = {"g56_feats", "g56_pool"} —— 与 strategies/g56.py
                          的 day_flow 编排 ext 逐字镜像 (门的 inputs 必须同源)。
  · `_g56_pool_batch`   : 全市场池日线的预加载/切片 (窗口口径见下)。

⚠️ 窗口口径是**硬约束**: 池必须等价于 g56 自己跑 `hub.daily(code, 200, as_of=T)`,
   否则横截面统计漂移 ⇒ 门判定与回测/实盘不再等价。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.market_cn.auto.core.present.ext_registry import register_ext

# g56 横截面池日线的**单槽缓存** (键=交易日)。池统计按交易日是常量, 一次运行只服务一个
# target; 单槽避免为每个历史日各留一份全市场日线 (内存)。窗口固定 200 根, 必须与 g56
# 内部 hub.daily 口径一致。
_G56_POOL_DAYS = 200
_G56_POOL_BARS: Dict[str, Any] = {"date": None, "bars": None}


def _g56_pool_batch(pool_target: Optional[str],
                    cache: Optional[Any] = None
                    ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """g56 池日线 (键=code) —— 窗口 200 根 + as_of=pool_target。

    必须与 `g56._ensure_pool_daily` 内部 `hub.daily(code, 200, as_of=pool_target)` 完全
    同口径 (同窗口/同复权/同截断), 否则横截面统计漂移 → 门判定不再等价 (逐笔等价前提)。

    取数优先级:
      ① 从**共享 BarsCache 切片** (days=300 缓存按 window_start(200, pool_target) 切) ——
         因为 `fetch_kline_db(code,200,as_of=T)` 的定义就是同锚 300 窗口的下界切片,
         二者逐行一致, 却省掉池的第二次全市场加载 (展示管线夜间的主要超支源);
      ② 缓存窗口不覆盖切片 (未预热 / asof 过旧或过新) → 独立批量加载一次。

    易错点 (A9, 2026-09-28): 切片锚与覆盖判据**必须用 pool_target**, 不能锚 now ——
    原实现 lo=window_start(200) 锚在 now, pool_target 为历史日 (verify_split / 逐日
    replay) 时窗口错位截短 → 池统计漂移 → g56 regime 门静默全 False (零信号)。
    切片等价的前提是缓存窗口 [window_start(cache.days, asof), asof] 完整包含
    [window_start(200, pool_target), pool_target]: 右缘 = asof >= pool_target,
    左缘 = window_start(cache.days, asof) <= window_start(200, pool_target)
    (即 asof 距 pool_target 不超过 (cache.days-200)*1.5 自然日, 勿写死数值, 用
    window_start 比较保持口径同源)。未覆盖或切片意外全空都走 ② 权威取数。
    """
    if not pool_target:
        return None
    if _G56_POOL_BARS["date"] == pool_target:
        return _G56_POOL_BARS["bars"]
    from app.market_cn.auto.core.data.kline import (
        all_codes as _all_codes, fetch_klines_batch, window_start,
    )
    codes = [c for c in _all_codes() if not c.startswith(("8", "4", "92"))]
    lo = window_start(_G56_POOL_DAYS, pool_target)      # A9: 锚=pool_target (原错锚 now)
    bars = None
    # 右缘: cache.asof 为 None 表示无截断 (行集到今天, pool_target 恒 <= 今天) → 视为过
    # 左缘: 缓存窗口下界须不晚于切片下界, 否则切片左端截短 (历史 replay 时必不覆盖)
    if cache is not None and (not cache.asof or cache.asof >= pool_target) \
            and window_start(cache.days, cache.asof) <= lo:
        cache.warm(codes)                     # 保证全市场在共享缓存内 (池口径完整)
        bars = {}
        for c in codes:
            bs = cache.get(c)
            sl = [b for b in bs if lo <= b["time"] <= pool_target]
            if sl:
                bars[c] = sl
    if not bars:   # None(未走切片) 或 {}(切片意外全空) 都走权威取数, 不信任残缺结果
        bars = fetch_klines_batch(codes, days=_G56_POOL_DAYS, as_of=pool_target)
    _G56_POOL_BARS["date"], _G56_POOL_BARS["bars"] = pool_target, bars
    return bars


@register_ext("g56", min_n=35)
def _ext_g56(spec: Any, code: str, bars: List[Dict[str, Any]],
             asof_date: Optional[str],
             cache: Optional[Any] = None) -> Dict[str, Any]:
    """g56: 每股 G1 特征数组 + 当日横截面池 (逐字镜像 strategies/g56.py 的 day_flow 编排 ext)。

    硬要求 len(bars) >= 35 (g56._g1_arrays 依赖 calc_macd, 短序列返 None) —— 已由
    `required_min_len("g56")` 在管线侧保证; 语义上的暖机要求 (>=68) 由 g1_warmup 门
    用 NaN 哨兵自然过滤。
    """
    from app.market_cn.auto.core.features.cross_section import _ensure_pool_daily, _g1_arrays
    d = asof_date or (str(bars[-1]["time"])[:10] if bars else None)
    return {"g56_feats": _g1_arrays(bars),
            "g56_pool": _ensure_pool_daily(d, bars_batch=_g56_pool_batch(d, cache))}
