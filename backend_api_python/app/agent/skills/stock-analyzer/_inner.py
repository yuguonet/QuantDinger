# -*- coding: utf-8 -*-
"""stock-analyzer 内部规则 —— 本技能独占，不与任何其他技能共享。

设计原则：skill 是自包含黑盒。A 股涨跌停规则与涨停史记录在此独立实现，
**不 import 任何其他 skill，也不被其他 skill import**。

对外契约（本包实际引用面，2026-11-26 核定）：
    limit_pct(code, name)         涨跌停幅度（%）——`_levels` 涨停起点、`_board_rank`
                                  板块涨停/龙头判定、`zt_days` 的封板阈值
    zt_days(bars, code, name, n)  近 n 个交易日的涨停记录（run.py 的 zt_history）
    env_int / env_float           环境变量读取的统一接口（非法/空串回落 default）；
                                  各模块此前各写一份同体助手，按「≥2 处同码收敛」归一至此
    num(v)                        标量 → float，认不出/NaN 返 None（不猜不填 0）；
                                  `_extra_facts._num` 与 run.py 内层 `_num` 口径不同
                                  （前者拒绝强转、后者缺数填 0）故保留各自本地实现
    bars_days()                   日线取数天数唯一真源（QD_STOCK_ANALYZER_BARS_DAYS，默认 120；
                                  run.py 与 _deep.py 共用，2026-11-26 收敛——此前 _deep 写死
                                  120 使 env 形同虚设）
    unwrap_single(payload, code)  单票工具响应解包唯一真源：data[code] 优先，键缺省且
                                  data 恰有一个 dict 值时兜底；run.py 两处内联收敛至此；
                                  _extra_facts._unwrap 是另一契约（裸结构工具）不在收敛范围

【历史沿革（2026-11-26 死代码清理）】原 load_bars / tech / detect / ma / ema /
macd / rsi / limit_locked / _prev_streak / 6 个形态检测器等指标与形态实现，
随取证层上移至 deep（`_deep.py` → market-screener 的 tech_summary / detect_patterns）
后已无人调用，全部删除；需要时从 git 历史恢复，**勿再复制第三份实现**。
技术面字段（ma_bull_stack / pos_in_20d_pct / dist_ma20_pct / rsi14 / vol_ratio）
由 deep 的 tech_summary 同名字段供给，改名前先看 run.py 的 `_votes` / `_v_structure`。

易错点：
  · 涨停判定阈值统一 `lp - 0.8`（容差），改此值要同步看 `_levels._zt_origin`；
  · 简称 N/C 开头判新股无涨跌幅，只看 name 前缀，勿改成代码段判断；
  · zt_days 的 bars 由调用方归一化（含 `date` 键），不接受原始工具返回形状。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def env_int(name: str, default: int) -> int:
    """环境变量取整数：未设/空串/非法回落 default。"""
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    """环境变量取浮点：未设/空串/非法回落 default。"""
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def num(v: Any) -> Optional[float]:
    """任意标量 → float；认不出/NaN 返回 None（不猜、不填 0）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


#: 日线取数天数默认值（唯一真源：run.py 与 _deep.py 共用）
BARS_DAYS_DEFAULT = 120


def bars_days() -> int:
    """日线取数天数：env `QD_STOCK_ANALYZER_BARS_DAYS`，未设/非法回落 120。"""
    return env_int("QD_STOCK_ANALYZER_BARS_DAYS", BARS_DAYS_DEFAULT)


def unwrap_single(payload: Any, code: str) -> Optional[Dict[str, Any]]:
    """单票工具响应解包唯一真源：data[code] 优先，键缺省且 data 恰有一个 dict 值时兜底。

    兜底只处理「单票取证但键名漂移（前缀/去前缀）」，与旧内联实现逐字同语义：
    data[code] 存在但非 dict 时**不**兜底（按缺数处理，不猜另一值是目标票）。
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        one = data.get(code)
        if isinstance(one, dict):
            return one
        if one is None:
            vals = [v for v in data.values() if isinstance(v, dict)]
            if len(vals) == 1:
                return vals[0]
    return None


def limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度（%），0.0 表示无涨跌幅限制。

    ST 跟随所在板（主板 ±10%、创业/科创 ±20%），不单独 ±5%；简称 N/C 开头为新股无限制。
    北证段 43/82/83/87/88/92 一律 ±30%。
    """
    code = (code or "").strip()
    name = (name or "").strip()
    if len(name) >= 2 and name[0] in ("N", "C"):
        return 0.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def zt_days(bars: List[Dict], code: str, name: str, n: int = 8) -> List[Dict[str, Any]]:
    """近 n 个交易日的涨停记录（自算，不依赖涨停池缓存）。无涨停返回 []。"""
    lp = limit_pct(code, name)
    if lp <= 0 or len(bars) < 2:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(max(1, len(bars) - n), len(bars)):
        pc = bars[i - 1]["close"]
        if not pc:
            continue
        chg = (bars[i]["close"] - pc) / pc * 100
        if chg < lp - 0.8:
            continue
        streak, j = 1, i - 1
        while j > 0:
            p2 = bars[j - 1]["close"]
            if p2 and (bars[j]["close"] - p2) / p2 * 100 >= lp - 0.8:
                streak += 1
                j -= 1
            else:
                break
        out.append({"date": bars[i]["date"], "change_pct": round(chg, 2), "streak": streak})
    return out
