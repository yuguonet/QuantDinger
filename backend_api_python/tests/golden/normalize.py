"""tests/golden/normalize.py — 出场原因的 code↔label 归一（§2.6b 载荷异构）。

来源：golden 逐笔对拍任务 | 产出：2026-10-07

★ 为什么需要它（实测差异，非假设）：
    同一笔 break 出场，两套口径：
      · 旧 `backtest_stock` → `exit_rule = "trail"`      （**代码**，break.py:1243 `_last_rule`）
      · 新 `replay`         → `exit_reason = "追踪止损-6.0%"`（**人读标签**，门表 DSL 产出）
    其余 8 个行为字段（d0/entry/exit 价格、exit_day、return_pct、peak）**逐位一致**。
    ⇒ 这是 §1.2-⑥「事件载荷跨策略异构」的实例，不是行为分歧。

★ 为什么归一在 golden 层而不是改策略：
    §2.6d 明确「载荷命名统一（策略侧改名）**不进本方案**」——改名会污染 golden 且
    波及全策略文件。§2.6b 要求映射「在 replay 侧单点消化」。本模块就是那一处映射
    的 golden 侧镜像（`core/replay/trade_map.py` 是运行时侧）。

★ 归一方向：一律归到 **代码**（trail/stop/sweet/time/escape），因为代码集有限且稳定；
    标签是参数化的（`止损{stop}%` / `持仓到期{hold}天`），只能靠前缀判定，不能靠全等。

易错点：
  - **`"止损%"` 不是 `"追踪止损"`**：判定顺序必须先 `追踪止损` 后 `止损`，否则追踪止损
    会被误归成 stop（前缀包含关系）。
  - 标签里带参数（`-6.0%`、`7天`），**不能做全等比较**，只能前缀/包含判定。
  - 归不出来一律返回 None 并由门禁显式报「未知出场原因」，**不得静默当成一致**。
"""
from __future__ import annotations

#: 旧引擎的出场规则代码（break.py `_last_rule` 全集 + 盘中引擎 `d1_open`，实测）
EXIT_RULE_CODES = ("trail", "stop", "sweet", "time", "escape", "d1_open")

#: 标签 → 代码的前缀规则（**顺序敏感**：长前缀在前）
_LABEL_PREFIXES = (
    ("追踪止损", "trail"),
    ("止损", "stop"),
    ("峰值逃顶", "escape"),
    ("逃顶", "escape"),
    ("甜点", "sweet"),
    ("末日顺延", "time"),
    ("持仓到期", "time"),
    ("到期", "time"),
    ("数据结束平仓", "time"),
    # 盘中隔夜形态（knife/tail）：两侧标签不同但同码（P1.5-② 实测）
    ("D1开盘卖出", "d1_open"),
)

#: 含以下子串 → 顺延/到期类（跌停顺延开盘等）
_CONTAINS = (
    ("顺延", "time"),
)


def label_to_code(label) -> str | None:
    """人读标签 → 出场规则代码。归不出来返回 None（**不猜**）。"""
    if label is None:
        return None
    s = str(label).strip()
    if not s:
        return None
    if s in EXIT_RULE_CODES:            # 已经是代码，原样返回
        return s
    for prefix, code in _LABEL_PREFIXES:
        if s.startswith(prefix):
            return code
    for sub, code in _CONTAINS:
        if sub in s:
            return code
    return None


def normalize_exit_reason(*, raw: dict | None = None, label=None):
    """出场原因归一 → (代码, 原值)。

    Args:
        raw: 旧引擎 trade dict（取 `exit_rule` 代码，或 `exit_reason` 标签）
        label: replay 侧 canonical `exit_reason`（标签）

    Returns:
        (code, raw_value)：code 为 None 时表示**归不出来**，门禁必须显式报告。
    """
    if raw:
        v = raw.get("exit_rule")
        if v is not None:
            return (str(v) if str(v) in EXIT_RULE_CODES else label_to_code(v)), v
        v = raw.get("exit_reason")
        if v is not None:
            return label_to_code(v), v
    if label is not None:
        return label_to_code(label), label
    return None, None
