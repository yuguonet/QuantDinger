#!/usr/bin/env python3
"""auto/backtest.py — 框架内全市场回测流水线 (B 阶段, 2026-09-09; 09-10 分发插件化)

用途: 把 test_dragon.py 的"全市场回测流水线"收进框架。本文件只做**薄编排**:
     全市场循环 → hub.daily 取数 → 策略钩子 backtest_stock → trades → 标准统计。
     策略枚举判定经注册表分发 (strategies 插件的 backtest_stock 钩子, 2026-09-10 起),
     **新建策略零改动本文件** — 插件内实现 backtest_stock 即自动进入流水线。

设计点:
  - 与实盘同一份 scan_signals (as_of 切片语义), 对数 PASS 后 test_dragon 双同步约定作废;
  - 编排层无规则: 去重/预过滤锚点/D1过滤/预筛都在各插件 backtest_stock 内,
    出场引擎 (run_backtest / run_backtest_breakbuy) 与 BOARD_PARAMS 是流水线设施留此,
    插件 lazy import 调用 (避免顶层环: backtest 顶层会 import 插件常量);
  - 数据走 hub.daily (与 test_dragon.fetch_kline_db 逐字等价: 窗口取数+qfq, 已验证)。

易错点:
  - 枚举终点 n-1: 最后一根无 D+1, 不能做 D0 (约定在插件循环内);
  - 未实现 backtest_stock 的策略 (盘中窗口类 tail/knife) run_all 直接报错提示;
  - run 输出 trades 含 tech_score 等字段, 与 tmp/ 基线 JSON 字段对齐供逐笔对数。
"""
from __future__ import annotations

import time

from app.market_cn.auto.common.exec_cn import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
)

# ================================================================
# 出场引擎 (2026-09-10 自 core.py 迁入 —— 出场模拟属回测流水线, 不属判定核心)
# 迁移为逐字搬运, 行为零差异; 350笔回归基线验证见 tmp/。
# ================================================================

BOARD_PARAMS = {
    # enhance_filter: 断板增强过滤 (三通道OR, 满足其一即可; 置 False 可整体关闭)
    #   通道1: 确认日涨跌 [confirm_chg_min, confirm_chg_max)  (企稳)
    #   通道2: 断板期均量比 >= vol_r_or_min                    (换手充分)
    #   通道3: 连板前20日涨幅 >= pre20_min                     (前期热度, 大肉股富集)
    # ma_bull_filter: 均线多头排列过滤 — 已评估: 胜率持平、均收益略增, 作用不大, 默认关闭
    "main": {"stop_loss": -8.0, "trailing_stop": -6.0, "take_profit": 15.0, "hold_days": 20, "vol_min": 1.2, "vol_max": 2.0, "drawdown_max": -10,
             "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
             "first_break_gap_min": 0, "first_break_chg_min": 0.0},
    "gem_star": {"stop_loss": -10.0, "trailing_stop": -8.0, "take_profit": 20.0, "hold_days": 15, "vol_min": 1.2, "vol_max": 2.5, "drawdown_max": -15,
                 "enhance_filter": True, "confirm_chg_min": 0.0, "confirm_chg_max": 2.0, "vol_r_or_min": 1.4, "pre20_min": 30.0, "ma_bull_filter": False,
                 "first_break_gap_min": 0, "first_break_chg_min": 0.0},
}


def run_backtest(bars, entry_idx, entry_price, hold_days=7, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None, d1_change=None, d1_gap=None):
    """V1/通用出场模拟 (现实化 2026-09-09, 与 test_dragon.py 逐字同步):

    现实约束: ① T+1 — 买入当日(d=1)不可卖出, 全部出场判定从 d=2 起 (仅更新峰值/估值);
    ② 跳空穿越 — 触发日开盘低于触发价按开盘价成交;
    ③ 跌停无法卖出 — 一字跌停整日跳过 (V1 的 D2 开盘清仓若遇一字跌停顺延次日开盘),
    触发成交触及跌停顺延次日开盘; 到期日一字跌停顺延次日开盘强平。
    V1 日内动量规则 (D1收盘判定→D2开盘执行) 本就满足 T+1, 判定逻辑未改动。
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    limit_threshold = 0.098 if board_type == "main" else 0.198
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    pending_dn = False        # 触发成交触及跌停 / 清仓日一字跌停 → 次日开盘强平
    last_unfilled = False     # 末日一字跌停 → 到期顺延

    # 如果外部未传入 d1_limit_up, 则在回测内计算 (兼容旧调用)
    # 注意: next_open 模式下 entry_idx=pullback_end+1, d=1 访问的是 D2
    # 因此推荐由调用方预计算并传入
    if d1_limit_up is None:
        d1_limit_up = False
        if entry_idx + 1 < len(bars):
            d1_bar = bars[entry_idx + 1]
            d1_ret = (d1_bar['close'] / entry_price - 1)
            if d1_ret >= limit_threshold * 0.98:
                d1_limit_up = True

    # next_open模式: entry_idx=D1(D+1开盘买入)
    # 循环d=1应指向D1(第一个持仓日), d=2指向D2, 以此类推
    # 先用D1的high更新peak
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1), d=2 → entry_idx+1(D2)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d = b['open'], d
            break

        # V1出场 (v3): D1日内动量<3% → D2开盘清仓
        # 日内动量 = D1收盘涨幅 - D1开盘涨幅 (盘中买卖力量指标)
        #   <0: 盘中出货, D2大概率续跌, 100%捕获D2跌>3%的信号
        #   >=0: 盘中有买盘承接, 继续持有
        # 注: -10%止损已移除, 日内动量规则在D2开盘即清仓, 不需要等止损位
        v1_momentum_exit = False
        if is_v1 and d == 2:
            # 日内动量 = D1收盘涨幅 - D1开盘涨幅 = (D1 close - D1 open) / D0 close
            # d1_change 和 d1_gap 由调用方传入, 也可从bars计算
            if d1_change is not None and d1_gap is not None:
                intraday = d1_change - d1_gap
            else:
                # fallback: 从bars计算
                d1_bar = bars[entry_idx]
                d0_close = bars[entry_idx - 1]['close'] if entry_idx > 0 else entry_price
                intraday = (d1_bar['close'] - d1_bar['open']) / d0_close * 100 if d0_close > 0 else 0
            v1_momentum_exit = intraday < 3

        # 一字跌停: 全天无成交可能 (D2开盘清仓同样无法成交 → 顺延次日开盘)
        if is_one_word_limit_dn(b, dn):
            pending_dn = v1_momentum_exit
            last_unfilled = True
            continue
        last_unfilled = False

        if v1_momentum_exit:
            # D2开盘直接清仓, 不等止损位
            exit_p, exit_d = b['open'], d
            break

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            # 1 峰值逃顶(优先): 涨>7%后大上影线(>30%)→收盘逃顶
            if peak_exit:
                ret = (b['close'] / entry_price - 1) * 100
                if ret > 7:
                    bar_range = b['high'] - b['low']
                    upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                    if upper > 30 and b['close'] < b['high'] * 0.98:
                        exit_p, exit_d = b['close'], d
                        break

            # 2/3 追踪+止损 (合并: 价格连续先穿过更高触发线; 跳空按开盘; 触跌停顺延)
            trig_t = peak * (1 + trailing_stop / 100)
            trig_s = entry_price * (1 + stop_loss / 100)
            trig = max(trig_t, trig_s)
            if b['low'] <= trig:
                fill = fill_on_gap(b['open'], trig)
                if fill_blocked_by_limit_dn(fill, dn):
                    pending_dn = True   # 成交价触及跌停 → 卖不出
                    continue
                exit_p, exit_d = fill, d
                break

        # 4 兜底: 持仓到期收盘走
        exit_p = b['close']; exit_d = d

    # 末日落入无法卖出状态 (一字跌停/触发触跌停) → 顺延下一可交易日开盘强平
    # (连续一字逐日跳过; nxt 指向未成交日的下一日)
    if last_unfilled or pending_dn:
        nxt = entry_idx + exit_d + 1
        while nxt < len(bars):
            nb = bars[nxt]
            pc = bars[nxt - 1]['close']
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb['low'] == nb['high'] and abs(nb['low'] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False
                nxt += 1
                continue
            exit_p, exit_d = nb['open'], nxt - entry_idx + 1
            break

    result = {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }
    if d1_limit_up:
        result['d1_limit_up'] = d1_limit_up
    return result


def run_backtest_breakbuy(bars, entry_idx, entry_price, hold_days=7, stop_loss=-8.0,
                          trailing_stop=-6.0, board_type="main"):
    """断板专用回测: 追踪止损 + 峰值逃顶信号 (收盘价口径, 与v1的low触及口径不同)。

    现实化 (2026-09-09, 与 test_dragon.py 逐字同步):
    ① T+1 — 买入当日(d=1)不可卖出;
    ② 成交价=收盘价 — 原引擎收盘判定却按触发价成交 (触发价高于判定收盘, 不可实现);
    ③ 跌停 — 一字跌停整日跳过; 收盘触及跌停卖不出 → 顺延次日开盘; 到期顺延。
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    peak = entry_price
    exit_p = entry_price
    exit_d = 0
    pending_dn = False        # 收盘触跌停卖不出 → 次日开盘强平
    last_unfilled = False     # 末日一字跌停 → 到期顺延

    # next_open模式: entry_idx=D1, 循环d=1应指向D1
    if entry_idx < len(bars):
        d1_init = bars[entry_idx]
        if d1_init['high'] > peak:
            peak = d1_init['high']

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1  # d=1 → entry_idx(D1)
        if idx >= len(bars): break
        b = bars[idx]
        if b['high'] > peak: peak = b['high']
        prev_close = bars[idx - 1]['close'] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d = b['open'], d
            break

        # 一字跌停: 全天无成交可能, 持仓顺延
        if is_one_word_limit_dn(b, dn):
            last_unfilled = True
            continue
        last_unfilled = False

        ret = (b['close'] / entry_price - 1) * 100
        ret_from_high = (b['close'] / peak - 1) * 100 if peak > 0 else 0

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            # 止损 (收盘判定 → 收盘价成交)
            if ret <= stop_loss:
                if dn is not None and b['close'] <= dn * 1.002:
                    pending_dn = True   # 收盘封死跌停 → 卖不出
                    continue
                exit_p, exit_d = b['close'], d
                break

            # 追踪止损 (盈利时, 收盘判定 → 收盘价成交)
            if ret_from_high <= trailing_stop and ret > 0:
                if dn is not None and b['close'] <= dn * 1.002:
                    pending_dn = True
                    continue
                exit_p, exit_d = b['close'], d
                break

            # 峰值信号: 涨>10%后大上影线(>40%)→收盘逃顶 (收盘>+10%不可能贴跌停)
            if ret > 10:
                bar_range = b['high'] - b['low']
                upper = (b['high'] - max(b['open'], b['close'])) / bar_range * 100 if bar_range > 0 else 0
                if upper > 40 and b['close'] < b['high'] * 0.98:
                    exit_p, exit_d = b['close'], d
                    break

        exit_p = b['close']; exit_d = d

    # 末日落入无法卖出状态 → 顺延下一可交易日开盘强平 (连续一字逐日跳过)
    if last_unfilled or pending_dn:
        nxt = entry_idx + exit_d + 1
        while nxt < len(bars):
            nb = bars[nxt]
            pc = bars[nxt - 1]['close']
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb['low'] == nb['high'] and abs(nb['low'] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False
                nxt += 1
                continue
            exit_p, exit_d = nb['open'], nxt - entry_idx + 1
            break

    return {
        'exit_price': round(exit_p, 3), 'exit_day': exit_d,
        'return_pct': round((exit_p / entry_price - 1) * 100, 2),
        'peak_return_pct': round((peak / entry_price - 1) * 100, 2),
    }


def is_st_stock(code):
    """显式 ST 过滤 (与 test_dragon 一致): 无股票名称数据时依赖涨停阈值自然排除。"""
    return False


# ================================================================
# 全市场流水线 (编排层: 经注册表分发, 无策略名分支)
# ================================================================

def run_all(strategy="dragon", days=300, codes=None, stock_info=None,
            use_prefilter=True, progress_every=500):
    """全市场回测 (策略经注册表分发)。

    strategy: 任意已注册且实现 backtest_stock 钩子的策略 key。
    返回 {"trades": [...], "stats": {...}}; trades 直接可 json.dump 与基线对数。
    """
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.data.hub import all_codes, daily
    from app.market_cn.auto.data.hub import stock_info as _hub_stock_info
    from app.market_cn.auto.strategies.base import StrategyBase

    strat_reg.autodiscover()
    strat = strat_reg.get_strategy(strategy)
    if strat is None:
        raise ValueError(f"strategy={strategy} 未注册 (可用: {sorted(strat_reg.all_strategies())})")
    if type(strat).backtest_stock is StrategyBase.backtest_stock:
        raise ValueError(f"strategy={strategy} 未实现日线枚举回测钩子 backtest_stock "
                         f"(盘中窗口策略走各自验证脚本)")

    if codes is None:
        codes = all_codes()
    if stock_info is None:
        try:
            stock_info = _hub_stock_info()  # U1~U3 依赖 (缺失则跳过, 会放行)
        except Exception:
            stock_info = {}
    t0 = time.time()
    trades = []
    n_ok = 0
    for k, code in enumerate(codes, 1):
        if is_st_stock(code):
            continue
        bars = daily(code, days)
        if not bars:
            continue
        trades.extend(strat.backtest_stock(
            bars, code,
            stock_info=stock_info.get(code) if stock_info else None,
            use_prefilter=use_prefilter) or [])
        n_ok += 1
        if progress_every and k % progress_every == 0:
            print(f"[{k}/{len(codes)}] trades={len(trades)} "
                  f"({time.time() - t0:.0f}s)", flush=True)
    return {"trades": trades, "stats": _summary(trades), "codes_ok": n_ok,
            "elapsed": round(time.time() - t0, 1)}


def _summary(trades):
    """标准报告 (对齐设计文档 §6.1 + 五高指标映射, 09-09 B-5)。

    字段: 笔数/胜率/均收/盈亏比/收益五分桶/20日峰值分布与均值/月均笔数(可操作性)/
         日均收益(单位时间收益比=均收益÷持有交易日)/前后两段分段稳定性。
    """
    if not trades:
        return {"n": 0}
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    buckets = {"≤-10": 0, "-10~-3": 0, "-3~+3": 0, "+3~+10": 0, ">+10": 0}
    for r in rets:
        k = "≤-10" if r <= -10 else "-10~-3" if r <= -3 else \
            "-3~+3" if r < 3 else "+3~+10" if r < 10 else ">+10"
        buckets[k] += 1
    peaks = [t["peak_return_pct"] for t in trades if t.get("peak_return_pct") is not None]
    months = sorted({str(t.get("entry_date", ""))[:7] for t in trades} - {""})
    n_h = len(trades) // 2
    seg = lambda ts: round(sum(1 for t in ts if t["return_pct"] > 0) / len(ts) * 100, 1) if ts else None
    hold_days_avg = sum(t.get("exit_day") or 0 for t in trades) / len(trades)
    return {
        "n": len(trades),
        "winrate": round(len(wins) / len(trades) * 100, 1),
        "avg_ret": round(sum(rets) / len(rets), 2),
        "pl_ratio": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2)
        if wins and losses else None,
        "ret_buckets": buckets,
        "peak": {"mean": round(sum(peaks) / len(peaks), 2) if peaks else None,
                 "lt10": sum(1 for p in peaks if p < 10),
                 "10_20": sum(1 for p in peaks if 10 <= p < 20),
                 "ge20": sum(1 for p in peaks if p >= 20)},
        "monthly_avg": round(len(trades) / len(months), 1) if months else None,
        "ret_per_day": round(sum(rets) / len(rets) / hold_days_avg, 3) if hold_days_avg else None,
        "winrate_1st_half": seg(trades[:n_h]),
        "winrate_2nd_half": seg(trades[n_h:]),
    }


if __name__ == "__main__":
    import argparse
    import json
    import os

    # CLI 直跑时需自行加载 .env (服务进程已由应用加载, 重复加载无害)
    try:
        from dotenv import load_dotenv
        for _p in [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))), ".env"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")]:
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="框架内全市场回测流水线 (策略经注册表分发)")
    parser.add_argument("--strategy", default="dragon",
                        help="任意已注册策略 key (dragon/v1/break/...)")
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--codes", default="", help="逗号分隔, 空则全市场")
    parser.add_argument("--out", default="", help="结果JSON输出路径 (对数用)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    res = run_all(strategy=args.strategy, days=args.days, codes=codes)
    print("统计:", res["stats"], "| codes_ok:", res["codes_ok"],
          "| 耗时:", res["elapsed"], "s")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res["trades"], f, ensure_ascii=False)
        print("已写出:", args.out)
