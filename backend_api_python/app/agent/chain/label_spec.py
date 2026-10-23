# -*- coding: utf-8 -*-
"""label_spec — 标签（T+N 窗口）口径的**唯一真源**（v5 P1-1，2026-10-13）。

## 为什么要有这个文件

口径曾在**三处**各写一份，且**数值不同**（已查证，勿重复查）：
    · `chain/evaluator.py` `_TIMEFRAME_DAYS`  1M=**22** / 3M=66 / 1Y=252
    · `chain/evaluator.py` `HOLD_LABELS`      1M=**20**（D3 时新增）
    · `chain/claims.py`    `_HORIZON_DAYS`    又一份 1M=20（含 `n/a: 0`）
⇒ 改一处忘两处 = **静默漂移**：不报错，只安静污染每一个 T+N 标签。

现在**全部收敛为一张表 `HOLDS`**。
`tests/test_v5_label_spec.py::test_no_fourth_copy_in_chain` 会扫描 `chain/` 目录，
若再冒出第二份字面量映射就会红。

## ★ 2026-10-13 收口：两组口径**已统一**（此前是"故意并存"，判断错了）

旧的 `TRACE_HOLDS`（1M=22）曾被认为是"自然月口径"、与 claim 的"交易日口径"
语义不同，故故意并存。**这个判断是错的**，已查证推翻：

1. 消费端按**交易日**解释：`evaluator._maturity_days` 的注释原文写明
   「hold_days 按交易日计（_TIMEFRAME_DAYS）」，且 `_get_actual_return` 用
   `base_idx + hold_days` 偏移 **K 线根数**。22 是"按自然月折算出的数"，
   但被当成交易日消费 ⇒ **两者是同一个量**，不是两种语义。
2. `22` 在算术上站不住：22×12 = 264 > `1Y=252`（一年没有 264 个交易日）
   ⇒ 与表内最硬的锚点 `1Y=252` **不自洽**。
3. **影响面已实测为零**：`qd_agent_traces` 根节点 1256 条，`timeframe` **全部为空**
   （恒走 `DEFAULT_HOLD_DAYS=3`）⇒ 22/66/252 从来没被消费过，是死值。
   新链路 `qd_agent_claims` 128 条里只有 **3 条** horizon=1M。

⇒ 统一为 **1M=21 / 3M=63**（`252/12` 与 `252/4`，与 1Y=252 严格自洽）。
   此取值由 `test_long_horizons_are_self_consistent` 钉死，后人改一个就必须改另外两个。

## `bar_freq`（拍板 2：只预留字段）

分钟线经验库**当前不做**，但字段先留：未来若启用分钟级证据，`provenance.bar_freq`
直接有值可用，不必迁移。默认 `1d`。**预留 ≠ 使用** —— 本模块不因此引入任何
分钟级取数逻辑（YAGNI）。

## 易错处

· `HOLD_LABELS` 反推时**必须剔除 0**（`n/a: 0`），否则 `hold_label(0)` 会返回
  `"n/a"`，语义荒谬（已由测试钉住）。
· `HOLD_LABELS` 是**先到先得**而非后覆盖：`T+5` 与 `1W` 同为 5 天，
  若用 `{v: k for ...}` 后者会覆盖前者 ⇒ `hold_label(5)` 会意外变成 `"1W"`。
· 未知标签：`hold_days` 返回 **None**（严格），`resolve_hold_days` 兜底
  **default**（宽松）。**只允许宽松版兜底**，且调用方必须清楚自己在兜底。
"""
from __future__ import annotations

import os
from typing import Dict, Optional

SCHEMA_VER = "ls2"

# ── 唯一真源：标签 → 持有**交易日**数 ────────────────────────────────────
# 单位恒为**交易日**（K 线根数）。长周期与 `1Y=252` 严格自洽：
#   1M = 252/12 = 21 ；3M = 252/4 = 63
# 由 `test_long_horizons_are_self_consistent` 守护，改一个必须改全部。
#
# `n/a: 0` 表示"没有到期日"（不是"持有 0 天"），反查表必须剔除。
#
# ★ 2026-10-23 用户裁定：选股/追责**封顶 T+20 ≈ 1M**（一个月）。T+10/T+20 与
#   2W/1M 是同一批窗口的两种写法，天数同值；反查表先到先得（T+10/T+20 在前
#   ⇒ hold_label(10/21) 回 T+10/T+20，与筛选表头一致）。3M/1Y 仍保留供
#   中长线 claim 用，但 market-screener 输出不写。
HOLDS: Dict[str, int] = {
    "T+1": 1,
    "T+3": 3,
    "T+5": 5,
    "1W": 5,
    "2W": 10,
    "1M": 21,
    # 筛选表头写法（2026-10-23）：与 2W/1M 同值；**放在后面** ⇒ 反查规范名仍是
    # 2W/1M（先到先得），hold_days("T+10"/"T+20") 仍可命中天数。
    "T+10": 10,
    "T+20": 21,
    "3M": 63,
    "1Y": 252,
    "n/a": 0,
}

# 反查表：天数 → 标签。
# ★ **先到先得**（不是后覆盖）：`T+5` 与 `1W` 都是 5 天，声明顺序决定规范名是
#   `T+5` —— 用 `{v: k for k, v in HOLDS.items()}` 会被 `1W` 覆盖，属静默改名。
# ★ 剔除 0（n/a 不是"持有 0 天"）。
HOLD_LABELS: Dict[int, str] = {}
for _k, _v in HOLDS.items():
    if _v > 0 and _v not in HOLD_LABELS:
        HOLD_LABELS[_v] = _k
del _k, _v

# 标签缺失/无法识别时的默认值（旧 `_DEFAULT_HOLD_DAYS` / `TRACE_HOLD_DEFAULT`）
DEFAULT_HOLD_DAYS = 3

# ── `bar_freq` 预留（拍板 2：只留字段，不做的不要写逻辑）────────────────────
DEFAULT_BAR_FREQ = "1d"
BAR_FREQS = ("1d", "1m", "5m", "15m", "60m")


def hold_days(label: Optional[str]) -> Optional[int]:
    """标签 → 持有交易日数（**严格**）：未知返回 None。

    ★ 不兜底成默认值 —— 静默兜底会让口径悄悄漂移（老教训）。
      需要兜底的调用方请用 `resolve_hold_days`。
    """
    if not label:
        return None
    h = str(label).strip()
    if h in HOLDS:
        return HOLDS[h]
    # 大小写不敏感。★ `n/a` 必须**先按原样**查：它 `.upper()` 后变 `N/A`，
    #   而 HOLDS 的键是小写 `n/a`（与 claims.py 的 horizon_default 一致），
    #   只做大写查会让 `n/a` 恒失配 ⇒ due_date_of 拿不到 0 ⇒ 静默变成"未知"。
    return HOLDS.get(h.upper())


def resolve_hold_days(label: Optional[str], default: int = DEFAULT_HOLD_DAYS) -> int:
    """标签 → 持有交易日数（**宽松**）：未知返回 `default`。

    ★ **全项目只允许这一处兜底**。调用方（如 `chain/resolver._horizon_days`、
      `chain/evaluator._get_hold_days`）必须清楚自己在兜底 ——
      兜底意味着"这条标签文本没被识别"。
    """
    v = hold_days(label)
    return default if v is None or v <= 0 else v


def hold_label(days: Optional[int]) -> str:
    """持有交易日数 → 标签。未登记的天数直给 `T+{n}`（不硬套到某个已知标签）。"""
    try:
        d = int(days or 0)
    except (TypeError, ValueError):
        d = 0
    return HOLD_LABELS.get(d, f"T+{d}")


def bar_freq() -> str:
    """当前 bar 频率（`QD_BAR_FREQ` 可配，默认 `1d`）。

    未登记的值**回落默认**而不是照抄 —— 脏值入库比缺值更难查。
    ★ 预留字段：当前无任何分钟级取数逻辑依赖它（拍板 2：只预留，不做）。
    """
    v = os.getenv("QD_BAR_FREQ", "").strip().lower()
    return v if v in BAR_FREQS else DEFAULT_BAR_FREQ
