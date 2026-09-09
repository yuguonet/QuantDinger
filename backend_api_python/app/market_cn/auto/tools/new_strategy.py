#!/usr/bin/env python3
"""auto/tools/new_strategy.py — 新策略脚手架生成器 (F 阶段)

用途: 一条命令生成符合框架契约的策略模板文件 (strategies/ 下),
     含 StrategyBase 必须实现的全部决策点骨架 + 注释指引, 避免遗漏契约。
     生成后: config.json 加开关 → autodiscover 自动注册 → 回测流水线对数。

用法: python -m app.market_cn.auto.tools.new_strategy my_strategy --label 我的策略
"""
from __future__ import annotations

import argparse
import os
import re

TEMPLATE = '''#!/usr/bin/env python3
"""strategies/{key}.py — {label} (脚手架生成, 请填充规则)

契约清单 (StrategyBase 必答):
  - key/name: 注册标识 (config.json strategies.{key} 开关);
  - scan_signals(bars, code, as_of=None, ctx=None, **params): D0 判定,
    只用 as_of(含)以前数据 (无未来函数), 返回 [Signal(...)];
  - entry_decision/confirm_decision/exit_decision: 盘中三决策 (按需覆盖);
  - quality_key/initial_stop: 名额排序键 / 初始止损;
  - data_needs: 数据需求声明 (hub 按声明注入);
  - scan_spec: 调度声明 (daily_close=盘后一次 | intraday_window=盘中窗口)。

规则编写纪律: 判定可解释 (Signal.extra 带条件明细, 供落选统计); 阈值全进
default_params; 单文件 ≤200 行 (框架承包一切非规则部分)。
"""
from __future__ import annotations

from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "{key}"
STRATEGY_LABEL = "{label}"

DEFAULT_PARAMS = dict(
    # score_min=8.0,        # 示例: 阈值全部声明在此, 由 config.json params 覆盖
)


@register
class {cls}(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"          # signal=D0信号日 | limit_up=锚定涨停日
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DEFAULT_PARAMS)
    data_needs = ("daily",)              # 可选: daily/snapshot/minute_1m/minute_live/lhb

    # ---- D0 判定 (盘后扫描) ----

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < {{min_bars}}:
            return result
        i = n - 1
        d0 = bars[i]
        # TODO: 规则判定 —— 全部条件只用 d0 及以前数据
        ok = False
        extra = {{"min_bars_used": n}}       # 判定明细 (落选统计也靠它)
        if ok:
            result.append(Signal(
                code=code, time=d0["time"], price=d0["close"],
                label="{label}", extra=extra))
        return result

    # ---- 盘中三决策 (按需覆盖; 默认实现见 base.py) ----

    def quality_key(self, row):
        """名额排序键 (越大越优先)。"""
        return 0
'''


def main():
    parser = argparse.ArgumentParser(description="生成策略脚手架")
    parser.add_argument("key", help="策略 key (snake_case)")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.key):
        raise SystemExit(f"key 须为 snake_case: {args.key}")
    cls = "".join(w.capitalize() for w in args.key.split("_"))
    label = args.label or args.key
    dst = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "strategies", f"{args.key}.py")
    if os.path.exists(dst):
        raise SystemExit(f"已存在: {dst}")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(TEMPLATE.format(key=args.key, label=label, cls=cls))
    print(f"已生成: {os.path.normpath(dst)}")
    print("下一步: 1) 填充 scan_signals 规则  2) config.json 加 strategies.%s 开关"
          "  3) autodiscover 自动注册  4) backtest.py 对数验证" % args.key)


if __name__ == "__main__":
    main()
