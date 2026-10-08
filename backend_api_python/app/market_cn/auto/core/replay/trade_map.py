"""core/replay/trade_map.py — 事件载荷 → canonical trade（改进方案 §2.6b）。

定位（Excel 比喻）：本模块 = 透视表的「字段映射」——把各策略各写各的单元格名
统一成同一套列名，**映射在回放侧单点消化，策略文件不为此改名**（§2.6d 明确
「载荷命名统一不进本方案」，改名会污染 golden 且波及全策略文件）。

实测异构（2026-10-07）：
  - dragon / g56 : ready → exec(buyable/gap/stop_use) → exit(return_pct/reason)
                   —— 三段链，trade 由 **exit** 事件产出。
  - knife / tail  : ready → exec(**自带 exit_price/exit_ret**，隔夜跳空 D1 开盘卖)
                   —— 两段链，trade 由 **exec** 事件产出。
  - break        : 仅 ready（P3 补结算事件前走 ready-only，不产 trade）。

canonical schema（对齐现有基线 JSON 字段名，新增策略照此写）:
    code / strategy / d0_date / entry_date / entry_price / exit_date / exit_price /
    exit_day / exit_reason / return_pct / peak_return_pct / exec_basis / score / label

易错点:
  - **不在此做任何判定**：本模块只搬字段。任何"顺手算一下收益"都会变成第二份回测。
  - 缺字段一律置 None，**不猜测、不补默认值**（缺 exit_day 的由链匹配补，见 collector）。
"""

from __future__ import annotations

#: canonical 输出字段顺序（稳定，便于 golden 逐字段比对）
CANONICAL_FIELDS = (
    "code", "strategy", "d0_date", "entry_date", "entry_price",
    "exit_date", "exit_price", "exit_day", "exit_reason",
    "return_pct", "peak_return_pct", "exec_basis", "score", "label",
)

#: 载荷别名 → canonical 名（旧策略由本表兜，新策略直接用 canonical 名）
_ALIAS = {
    "reason": "exit_reason",        # g56/knife 出场原因写作 reason
    "exit_ret": "return_pct",       # knife/tail 收益写作 exit_ret
}

#: 反向索引（canonical → 别名）：`_pick` 按 canonical 名查，需反查旧名。
#: ⚠ 方向搞反会让 exit_reason 静默取不到值、回退成 label（曾发生）。
_ALIAS_REV = {v: k for k, v in _ALIAS.items()}

#: exec 载荷里出现这些键 ⇒ 该 exec 是「自带出场的闭合事件」（knife/tail 隔夜跳空形态）
_SELF_CLOSED_KEYS = ("exit_price",)


def is_self_closed(exec_pl: dict) -> bool:
    """exec 事件是否已自带出场（两段链形态）。"""
    return any(k in (exec_pl or {}) for k in _SELF_CLOSED_KEYS)


def _pick(src: dict, *names):
    """按 canonical 名 + 其别名依次取值，全缺返回 None（不补默认）。"""
    for n in names:
        v = src.get(n)
        if v is not None:
            return v
        alias = _ALIAS_REV.get(n)
        if alias is not None:
            v = src.get(alias)
            if v is not None:
                return v
    return None


def build_trade(*, code, strategy, ready_date=None, ready_pl=None,
                exec_date=None, exec_pl=None, exit_pl=None,
                exec_basis="daily") -> dict:
    """事件链 → canonical trade。

    Args:
        ready_date/pl : ready 事件（信号日与信号载荷，提供 d0_date/score/label）。
        exec_date/pl  : exec 事件（入场；knife/tail 形态下自带出场）。
        exit_pl       : exit 事件载荷（三段链形态；为 None 时取 exec 自带出场）。
        exec_basis    : "daily" | "1m"，由 feed 标注（1m 精修腿）。
    """
    ready_pl = ready_pl or {}
    exec_pl = exec_pl or {}
    exit_pl = exit_pl or {}

    # exit 段优先（三段链），否则退到 exec 自带出场（两段链）
    closed_pl = exit_pl if exit_pl else (exec_pl if is_self_closed(exec_pl) else {})

    self_closed = (not exit_pl) and is_self_closed(exec_pl)
    src = dict(exec_pl)
    src.update(closed_pl)                       # 同名字段 exit 覆盖 exec

    entry_price = _pick(src, "entry_price")
    exit_price = _pick(src, "exit_price")
    entry_date = _pick(src, "entry_date") or exec_date
    exit_date = _pick(src, "exit_date") or exec_date

    return {
        "code": code,
        "strategy": strategy,
        "d0_date": str(ready_date)[:10] if ready_date else None,
        "entry_date": str(entry_date)[:10] if entry_date else None,
        "entry_price": round(float(entry_price), 3) if entry_price else None,
        "exit_date": str(exit_date)[:10] if exit_date else None,
        "exit_price": round(float(exit_price), 3) if exit_price else None,
        # 两段链（self-closed，knife/tail 隔夜形态）：出场口径 = 旧引擎契约
        # （intraday_exit 固定 exit_day=1、exit_reason="d1_open"，其标签在 exec 的 label 里）
        "exit_day": _pick(src, "exit_day") or (1 if self_closed else None),
        "exit_reason": _pick(src, "exit_reason") \
            or (exec_pl.get("label") if self_closed else None) \
            or ready_pl.get("label"),
        "return_pct": _pick(src, "return_pct"),
        "peak_return_pct": _pick(src, "peak_return_pct"),
        "exec_basis": exec_basis,
        "score": ready_pl.get("score"),
        "label": ready_pl.get("label"),
    }
