"""strategies/common/contract.py — 插件契约检测（公共接口）。

P0 搬移（2026-10-12 策略接口统一方案）：收拢 strategies/base.py 中的
has_fold_contract / FOLD_CONTRACT_EXEMPT。
has_fold_contract / FOLD_CONTRACT_EXEMPT。当前无需折叠豁免的内置策略。
"""

#: 设计依据：`.py` 插件是逃生舱（目标态 §3.1），未迁折叠不等于缺陷，但必须**登记**。
#: 消费方：tests/present/test_enable_switch（门禁）、tools/doctor（显式标注 + 逾期告警）。
#: 条目结构（2026-10-12 A-2，**豁免不得无到期**）：
#:   reason        为什么豁免（含出处文档）
#:   ticket        迁移工单 / 计划条目（设计 §13.1 A-x），指向收口动作
#:   expires       到期日（YYYY-MM-DD）；逾期不自动清退，但必须可见
#:   missing_views 豁免期间**缺失的能力视图**
#: 清退条件：策略迁完折叠契约（init_state/step/evaluate）后**立即移出本名单**。
FOLD_CONTRACT_EXEMPT: dict = {}


def has_fold_contract(strategy) -> bool:
    """策略是否真正实现了折叠契约（init_state/step/evaluate 非基类 NotImplemented）。

    P3 薄壳分流用。`hasattr` 恒真（基类有定义），必须比对「是否被子类覆写」。
    """
    from app.market_cn.auto.strategies.base import StrategyBase
    for m in ("init_state", "step", "evaluate"):
        f = getattr(type(strategy), m, None)
        if f is None or f is getattr(StrategyBase, m, None):
            return False
    return True


def exemption_report(today=None) -> list:
    """逃生舱豁免体检表（doctor / 门禁共用；纯函数，不触库不触盘）。

    返回 [{key, ticket, expires, days_left, expired, missing_views, reason}]，按 key 排序。
    易错点：
      · expires 是**日历日**不是交易日；
      · 日期写错（解析失败）**视同逾期**，不许静默放过；
      · 逾期不自动清退（清退是人工动作），本函数只负责把事实摆到台面上。
    """
    from datetime import date as _date
    td = today or _date.today()
    out = []
    for key in sorted(FOLD_CONTRACT_EXEMPT):
        ent = FOLD_CONTRACT_EXEMPT[key]
        if not isinstance(ent, dict):
            ent = {"reason": str(ent), "ticket": "", "expires": "", "missing_views": []}
        exp_raw = str(ent.get("expires") or "")
        days_left, expired = None, False
        if exp_raw:
            try:
                days_left = (_date.fromisoformat(exp_raw) - td).days
                expired = days_left < 0
            except ValueError:
                expired = True
        out.append({"key": key, "ticket": ent.get("ticket", ""), "expires": exp_raw,
                    "days_left": days_left, "expired": expired,
                    "missing_views": list(ent.get("missing_views") or []),
                    "reason": ent.get("reason", "")})
    return out
