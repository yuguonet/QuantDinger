#!/usr/bin/env python3
"""auto/core/display_meta.py — 展示口径映射 (轻量 / 无项目内依赖 / 不参与判定指纹)

职责: 把**判定结果**翻译成给前端看的档位与文案。本模块不含任何判定逻辑,
不影响信号成立、成交价、出场时点 —— 它只决定"这条已判定的结果该怎么显示"。

★ 为什么单独成模块 (2026-09-23, 启动指纹语义边界):
  `confirm_level_of()` 原住在 `strategies/base.py`, 而 base.py 是**判定契约**的所在地
  (ConfirmDecision / StrategyBase / scan_days)。两类改动的后果完全不同:

      改判定契约 (如 confirm_decision 口径) → 库里是旧规则算出的结果 → 必须重跑
                                              rebuild 校准, 否则 UI 显示旧信号
      改档位映射 (如 strong 的判定条件)     → 展示链每次请求实时读 → 重启即可, 无需重建

  但启动指纹是**文件级内容 hash**, 切不开同一文件里混着的两类改动 ⇒ 改一次档位映射
  就白跑一次全量重建。实证: 2026-09-23 19:33 因 base.py 变更触发的 rebuild 耗时 300s,
  产出与 17:01 那次**逐字段完全相同** (upserted 43 / inserted 6 / expired 1, 重放分支
  15/12/6/16=49) —— 那次改的正是本模块的档位归一逻辑。

  移出后 startup 的判定链依赖闭包不再包含本模块, 指纹边界 = 语义边界。

⚠️ 维护约束 (违反会让"该重建却不重建"):
  - 本模块必须保持**零判定逻辑、零项目内依赖** —— 它被 `startup._JUDGE_EXCLUDE`
    显式排除在判定指纹之外。一旦在此塞入影响判定的代码, 改动就不会触发重建。
  - 反向也要守: 影响判定的东西(如 `detail['confirm_strong']` 的**产生**逻辑)留在策略
    与 base.py 里; 本模块只**消费**它们。

调用方: monitor.evaluate_confirm (生产链 15:00 确认) → extra.pre_confirm 档位 → 前端渲染。
"""
from __future__ import annotations

#: 档位值域 —— 前端 pcMap 只认这三个 (strong='强' / ok='中' / weak='弱')
CONFIRM_LEVELS = ("strong", "ok", "weak")


def confirm_level_of(dec):
    """把 ConfirmDecision 归一为展示档位 strong/ok/weak; None = 无法判定。

    真值源单一:
      confirmed                 过没过 (硬判定, 决定状态机 watch_pending → holding/exit_today)
      detail['confirm_strong']  强度位 (True 且已确认 → strong)
      detail['level']           策略显式覆盖扩展点 (仅当取值在 CONFIRM_LEVELS 内才生效)

    刻意**不读 reason** —— 旧实现 `dec.reason if dec.confirmed else "weak"` 把策略内部
    语义串当档位写进 extra.pre_confirm (g56_hold / hold_to_D1_open / sealed_hold /
    "D1日内动量<3%,D2开盘清仓"), 前端 pcMap 查不到 → 一律渲染成未知档 ☆, 既无法与真弱
    确认区分, 又把内部 token 漏进明细弹窗。归一后: 未确认恒 weak, 已确认恒 ok (除非策略
    用 confirm_strong / level 显式声明强档)。
    """
    if dec is None:
        return None
    d = dec.detail if isinstance(dec.detail, dict) else {}
    lv = d.get("level")
    if lv in CONFIRM_LEVELS:            # 显式覆盖 (非法值静默走兜底, 不抛)
        return lv
    if not dec.confirmed:
        return "weak"
    return "strong" if d.get("confirm_strong") else "ok"
