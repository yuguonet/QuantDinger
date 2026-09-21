#!/usr/bin/env python3
"""auto/tools/new_strategy.py — 新策略脚手架生成器 (2026-09-18 P1 升级版)

用途: 一条命令生成可运行的策略插件骨架。P1 智能默认落地后, 新策略最小面 =
     key/name + scan_signals + 出场参数 (三决策/回测钩子全继承基类默认)。
生成后: config.json strategies.<key> 加段 (enabled 默认 false=不进实盘) →
     autodiscover 自动注册 → backtest 流水线直接可跑。

用法:
  python -m app.market_cn.auto.tools.new_strategy my_strat --label 我的策略
  python -m app.market_cn.auto.tools.new_strategy my_intra --kind intraday_window
  python -m app.market_cn.auto.tools.new_strategy demo --out D:/tmp/scaffold_test
"""
from __future__ import annotations

import argparse
import os
import re

HEADER = '''#!/usr/bin/env python3
"""strategies/{key}.py — {label} (脚手架生成, 请填充规则)

策略形态: {kind_zh}
最小面 (P1 智能默认后): key/name + scan_signals + 出场参数, 其余全默认可跑。
三决策继承 StrategyBase 默认:
  entry  : D1 竞价 gap 带 (min_gap_*/max_gap_* 参数可调)
  confirm: D1 收盘确认通过 (d1_chg 按 signal_price 基准)
  exit   : 止损 stop% / 追踪 trail%(自入场日峰值, held>1) / 到期 hold 天
回测钩子: backtest_stock 默认 = as_of 枚举 + U1~U4 + D1 开盘买 + exit 重放
  (python -m app.market_cn.auto.core.backtest --strategy {key} --days 60 直接可跑)。
需要特殊规则时覆盖对应方法 (参考 v1.py / dragon_callback.py / tail_oversold.py)。

规则编写纪律: 判定只用 as_of(含)以前数据 (无未来函数); 阈值全进 default_params
(禁硬编码, 否则 param_scan 网格无效); 判定可解释 (Signal.extra 带明细);
规则改动必经两段稳定性验证 + pool_check 信号级复验。
"""
from __future__ import annotations

from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "{key}"
STRATEGY_LABEL = "{label}"

DEFAULT_PARAMS = dict(
    # ---- 出场 (默认 exit_decision / backtest 引擎消费) ----
    stop=-8.0,           # 止损 %
    trail=-4.0,          # 追踪止损 % (自入场日峰值, held>1)
    hold=7,              # 到期天数
    # ---- D1 竞价 gap 带 (默认 entry_decision / backtest D1 口径消费) ----
    min_gap_main=-8.0, max_gap_main=11.0,      # 主板
    min_gap_gem=-10.0, max_gap_gem=20.5,       # 创业板/科创板
    # score_min=8.0,    # 示例: 策略自有阈值全部声明在此, config.json params 覆盖
)
'''

DAILY_BODY = '''

@register
class {cls}(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"          # U2/U3 锚定日: signal=D0信号日 | limit_up=最近涨停日
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DEFAULT_PARAMS)
    data_needs = ("daily",)              # 可选: daily/minute_1m/minute_live/quote/lhb/
                                         #       index_daily/index_minute/index_fflow

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        """D0 判定 — TODO: 替换为真实规则。

        bars: 前复权日K升序 list[dict] (time/open/high/low/close/volume);
        as_of: 判定日索引 (回测传 i, 实盘 None=末根); 参数经 merged_params 合并。
        """
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        n = len(bars)
        if n < 25:  # 指标预热
            return []
        i = n - 1
        d0 = bars[i]
        # TODO: 规则判定 —— 全部条件只用 d0 及以前数据; 每道门带 reason 便于 rule_audit
        chg = (float(d0["close"]) / float(bars[i - 1]["close"]) - 1) * 100
        if chg < 5.0:                    # 示例门, 替换之
            return []
        return [Signal(
            code=code, time=str(d0["time"])[:10],
            score=50,                    # TODO: 0~100
            price=float(d0["close"]),
            label=f"{{STRATEGY_LABEL}},chg{{chg:.1f}}",
            extra={{"d0_close": float(d0["close"]), "chg": round(chg, 2)}},
        )]
'''

INTRADAY_BODY = '''

@register
class {cls}(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    scan_spec = ScanSpec(kind="intraday_window", data="minute",
                         windows=("14:50", "14:57"), interval_sec=60,
                         entry_at="14:56")   # entry_at=终审成交时刻, 其余触发仅预览
    signal_state = "buy_today"        # 盘中即买策略初始状态
    entry_at_close = True             # 尾盘入场, 当日止损守卫跳过
    default_params = dict(DEFAULT_PARAMS)
    data_needs = ("minute_live",)

    def intraday_shortlist(self, snaps, **params):
        """便宜预筛 — 只用最新快照, 宁可多留不可误杀。返回 {{code: snap}}。"""
        # TODO: 例: {{c: s for c, s in snaps.items() if ...}}
        return dict(snaps or {{}})

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        """窗口内判定 — bars=当日1m K (prep_minutes 后), TODO: 替换真实规则。"""
        if not bars:
            return []
        return [Signal(code=code, time=str(bars[-1]["time"])[:10], score=50,
                       price=float(bars[-1]["close"]), label=STRATEGY_LABEL)]
'''

CHECKLIST = '''
下一步 (顺序执行):
  1) 填充 scan_signals 真实规则 (TODO 处)
  2) config.json strategies 加段 (未写段 = 不进实盘, 安全):
     "{key}": {{"enabled": false, "daily_limit": 5, "params": {{}}, "winrate": null}}
  3) 导入自检: python -c "from app.market_cn.auto import strategies as s; s.autodiscover(); print(s.get_strategy('{key}'))"
  4) 回测冒烟: python -m app.market_cn.auto.core.backtest --strategy {key} --days 60
  5) 参数网格: tools/param_scan.py --strategy {key} --days 300   (两段稳定性)
  6) 规则审计: tools/rule_audit.py --strategy {key} --days 300   (逐门判别力)
  7) 池切复验: tools/pool_check.py --strategy {key}              (kept>universe 才 MIGRATE)
  8) 全部通过 → enabled=true → 重启 backend'''


def main():
    parser = argparse.ArgumentParser(description="生成策略脚手架 (P1 最小面版)")
    parser.add_argument("key", help="策略 key (snake_case)")
    parser.add_argument("--label", default="")
    parser.add_argument("--kind", default="daily_close",
                        choices=["daily_close", "intraday_window"],
                        help="策略形态 (默认 daily_close 盘后扫描)")
    parser.add_argument("--out", default=None, help="输出目录 (默认 strategies/)")
    args = parser.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.key):
        raise SystemExit(f"key 须为 snake_case: {args.key}")
    if args.key.startswith("_"):
        raise SystemExit("key 不能下划线开头")
    cls = "".join(w.capitalize() for w in args.key.split("_")) + "Strategy"
    label = args.label or args.key
    kind_zh = ("daily_close 盘后全市场扫描, D0 判定→D1 开盘买"
               if args.kind == "daily_close"
               else "intraday_window 盘中窗口轮询 (参考 tail_oversold.py)")
    body = DAILY_BODY if args.kind == "daily_close" else INTRADAY_BODY
    out_dir = (os.path.abspath(args.out) if args.out else
               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "strategies"))
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, args.key + ".py")
    if os.path.exists(dst):
        raise SystemExit(f"已存在: {dst}")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write((HEADER + body).format(key=args.key, label=label, cls=cls,
                                       kind_zh=kind_zh))
    print(f"已生成: {os.path.normpath(dst)}")
    print(CHECKLIST.format(key=args.key))


if __name__ == "__main__":
    main()
