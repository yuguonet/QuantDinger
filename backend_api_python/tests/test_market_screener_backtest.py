# -*- coding: utf-8 -*-
"""market-screener 模拟回测口径契约（2026-11-02）。

【为什么有这个文件】过去几轮回测反复踩同一批口径坑，每次都靠人工发现、事后返工：

1. **T+1 锁仓**——A股当日买入不可卖出。卖出窗口必须**严格晚于入场日**。
   早期探针把入场日当「T+1」，凭空多出一天持有期，结果偏乐观约 1.5pp。
2. **触涨停判定**——必须按**各板真实涨停价**（主板 10% / 创业科创 20% / 北交所 30%）。
   一刀切 +10% 会把 20cm 票的涨停误算成「触 10%」，也会把主板未封板算成触板。
3. **触板卖出价**——保守口径按入场价 **+10%** 计（日线确认不了触价与收盘的先后顺序，
   按触价卖已是乐观估计，不能再按涨停价卖）。
4. **入场日**——决策日之后**第一个交易日**；周末/节假日决策自动落到下一交易日。
5. **费用**——往返 0.3%（买入+卖出合计），每笔都扣。
6. **开盘一字/涨停买不进**——开盘价已达涨停价时该笔**不计入样本**（不是算亏损）。
7. **T0 / T1 / T3 定义**——T0 = 买入当日收盘（当日不可卖，仅标记）；T1 = 次日收盘
   （T+1 锁仓后最早可实现点）；T3 = 第 3 个持有日收盘。三者都是「相对入场价」。

本文件把上述口径固化为**唯一真源**：任何回测（探针、盘中链路、报告）都必须与本文件的
`simulate()` 一致。改口径先改这里，并同步全部调用方；否则测出来的数字互相不可比。

【设计】纯函数 + 合成 K 线，零 IO、零外部依赖（毫秒级），故可进常规回归。
真实行情回测依赖库中数据、结果随窗口变化，属探针（tmp/），不进单测。
"""

from __future__ import annotations

FEE = 0.003          # 往返费率（买+卖合计）
SELL_DAYS = 3        # 入场后第 N 个交易日收盘强制了结
TOUCH_GAIN = 10.0    # 触板止盈口径（%），按入场价计
LIMIT_TOL = 0.002    # 涨停价容差（对齐报价精度，避免 10.00% 边界误判）
DEEP_LOSS = -15.0    # 深亏阈值（%，按持有期最低价 vs 入场价）

# 标准测试日历：决策日 10-23(周五) → 入场 10-26(周一) → 卖出 10-27/28/29
DATES = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]


def limit_pct(code: str) -> float:
    """按板块返回涨停幅度（小数）：创业板/科创板 20%，北交所 30%，其余 10%。"""
    c = str(code)
    if c[:2] in ("30", "68"):
        return 0.20
    if c[:2] in ("92", "43", "83", "87") or c[:1] in ("4", "8"):
        return 0.30
    return 0.10


def _idx(bars):
    return {b["date"]: i for i, b in enumerate(bars)}


def simulate(bars, calendar, decision_day, code="600000"):
    """按既定口径模拟一笔交易。

    Args:
        bars: [{date, open, high, low, close, volume}, ...]，按日期升序
        calendar: 交易日列表（升序，含 decision_day 前后的日子）
        decision_day: 决策日（用哪天的数据做判断）
        code: 股票代码（决定涨停幅度）

    Returns:
        dict | None。None = 数据不足（不计入统计，不是亏损）。
        dict 键：entry_day / exit_day / unbuyable / net / touch / deep / t0 / t1 / t3
        其中 unbuyable=True 表示开盘即涨停买不进，仅含 entry_day。
    """
    idx = _idx(bars)
    entry = next((d for d in calendar if d > decision_day), None)
    if entry is None or entry not in idx:
        return None
    i = idx[entry]
    if i < 1:                       # 需要前收算涨停幅度
        return None

    o = bars[i]["open"]
    pc = bars[i - 1]["close"]
    lp = limit_pct(code)
    if pc > 0 and o >= pc * (1 + lp - LIMIT_TOL):
        return {"unbuyable": True, "entry_day": entry}

    sells = [d for d in calendar if d > entry][:SELL_DAYS]
    if len(sells) < SELL_DAYS or any(d not in idx for d in sells):
        return None

    touch = False
    low = bars[i]["low"]
    for d in sells:
        j = idx[d]
        if bars[j]["high"] >= bars[j - 1]["close"] * (1 + lp - LIMIT_TOL):
            touch = True
        low = min(low, bars[j]["low"])

    net = None
    for d in sells:
        if bars[idx[d]]["high"] >= o * (1 + TOUCH_GAIN / 100):
            net = TOUCH_GAIN - FEE * 100
            break
    if net is None:
        net = (bars[idx[sells[-1]]]["close"] / o - 1) * 100 - FEE * 100

    return {
        "entry_day": entry,
        "exit_day": sells[-1],
        "unbuyable": False,
        "net": net,
        "touch": touch,
        "deep": (low / o - 1) * 100 <= DEEP_LOSS,
        "t0": (bars[i]["close"] / o - 1) * 100,
        "t1": (bars[idx[sells[0]]]["close"] / o - 1) * 100,
        "t3": (bars[idx[sells[-1]]]["close"] / o - 1) * 100,
    }


def aggregate(rows):
    """回测报表现行口径：净收益均值/中位、胜率、触板率、T+1/T+3 均值、深亏数。

    只统计可成交（未 unbuyable）且数据充足（非 None）的样本。
    """
    ok = [r for r in rows if r and not r.get("unbuyable")]
    if not ok:
        return {"n": 0}
    nets = sorted(r["net"] for r in ok)
    return {
        "n": len(ok),
        "net": sum(nets) / len(nets),
        "t1": sum(r["t1"] for r in ok) / len(ok),
        "t3": sum(r["t3"] for r in ok) / len(ok),
        "touch": sum(1 for r in ok if r["touch"]),
        "deep": sum(1 for r in ok if r["deep"]),
        "median": nets[len(nets) // 2],
        "win_rate": sum(1 for x in nets if x > 0) / len(nets),
        "touch_rate": sum(1 for r in ok if r["touch"]) / len(ok),
    }


# 报表六项标准口径（2026-11-02 用户指定）：n / 净收益 / T+1 / T+3 / 触涨停 / 深亏15%
REPORT_FIELDS = ("n", "net", "t1", "t3", "touch", "deep")
REPORT_HEADER = ("n", "净收益%", "T+1%", "T+3%", "触涨停", "深亏15%")


def format_report(label, agg):
    """把 aggregate() 结果排成标准六列（触涨停/深亏为**只数**，收益为均值%）。"""
    if not agg.get("n"):
        return "%-22s n=0" % label
    return "%-22s %5d  %+7.2f  %+6.2f  %+6.2f  %5d  %6d" % (
        label, agg["n"], agg["net"], agg["t1"], agg["t3"], agg["touch"], agg["deep"])


def report_header():
    return ("%-22s " % "组") + ("%5s  %7s  %6s  %6s  %5s  %6s" % REPORT_HEADER)


# ── 合成数据帮手 ──────────────────────────────────────────────────────
def _bars(rows, start=0):
    """rows = [(open, high, low, close), ...]，依次映射到 DATES[start:]。

    只给 4 根即可跳过决策日（用于「决策日前无 bar」的边界场景）。
    """
    return [
        {"date": DATES[start + k], "open": o, "high": h, "low": l, "close": c, "volume": 1000}
        for k, (o, h, l, c) in enumerate(rows)
    ]


def _flat(n=5, px=10.0):
    return [(px, px, px, px)] * n


# ══════════════════════════════════════════════════════════════════════
# 1. 入场日 = 决策日之后第一个交易日
# ══════════════════════════════════════════════════════════════════════
def test_entry_day_is_next_trading_day():
    r = simulate(_bars(_flat()), DATES, DATES[0])
    assert r["entry_day"] == "2026-10-26", r


def test_weekend_decision_rolls_to_next_trading_day():
    """周末决策（日历里没有该日）→ 入场仍在下一个交易日，不是跳过。"""
    r = simulate(_bars(_flat()), DATES, "2026-10-24")
    assert r["entry_day"] == "2026-10-26", r


def test_no_later_trading_day_returns_none():
    """决策日是日历最后一天 → 无入场日，返回 None（不是算 0）。"""
    assert simulate(_bars(_flat()), DATES, "2026-10-29") is None


# ══════════════════════════════════════════════════════════════════════
# 2. T+1 锁仓：卖出窗口严格晚于入场日
# ══════════════════════════════════════════════════════════════════════
def test_sell_window_excludes_entry_day():
    """入场日当天暴涨 10% 也不许卖：exit_day 必须是入场日之后。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),     # 10-23 决策日
        (10.0, 11.0, 10.0, 11.0),     # 10-26 入场日，当日就 +10%
        (11.0, 11.0, 11.0, 11.0),     # 10-27
        (11.0, 11.0, 11.0, 11.0),     # 10-28
        (11.0, 11.0, 11.0, 11.0),     # 10-29
    ]
    r = simulate(_bars(rows), DATES, DATES[0])
    assert r["entry_day"] == "2026-10-26"
    assert r["exit_day"] == "2026-10-29", r
    # 入场日高 11.0 >= o*1.10 已经触到止盈线，但那天不能卖 ⇒ 只能按卖出日
    # 11.0 收盘了结：+10% − 费用
    assert abs(r["net"] - (10.0 - FEE * 100)) < 1e-9, r


def test_t1_is_next_day_close_not_entry_close():
    """T1 = 次日收盘（最早可实现），不是入场日收盘（T0）。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 11.0),     # 入场日 T0 = +10%
        (10.0, 10.0, 10.0, 10.5),     # 次日 T1 = +5%
        (10.0, 10.0, 10.0, 10.2),
        (10.0, 10.0, 10.0, 10.1),
    ]
    r = simulate(_bars(rows), DATES, DATES[0])
    assert abs(r["t0"] - 10.0) < 1e-9, r      # 当日浮盈，不可实现
    assert abs(r["t1"] - 5.0) < 1e-9, r       # 次日收盘，最早可实现
    assert abs(r["t3"] - 1.0) < 1e-9, r


# ══════════════════════════════════════════════════════════════════════
# 3. 触涨停按各板真实幅度
# ══════════════════════════════════════════════════════════════════════
def test_touch_uses_main_board_10pct():
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 11.0, 10.0, 10.8),     # 次日 high 正好 +10% → 主板触板
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
    ]
    r = simulate(_bars(rows), DATES, DATES[0], code="600000")
    assert r["touch"] is True, r


def test_touch_chinext_needs_20pct():
    """同一根 +10% 的 high，创业板不算触板（涨停是 20%）。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 11.0, 10.0, 10.8),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
    ]
    r = simulate(_bars(rows), DATES, DATES[0], code="300001")
    assert r["touch"] is False, r


def test_touch_chinext_20pct_counts():
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 12.0, 10.0, 11.5),     # high +20% → 创业板触板
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
    ]
    r = simulate(_bars(rows), DATES, DATES[0], code="300001")
    assert r["touch"] is True, r


def test_touch_bse_30pct():
    """北交所（8/4 开头）涨停 30%：+20% 不算触板。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 12.0, 10.0, 11.5),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
    ]
    assert simulate(_bars(rows), DATES, DATES[0], code="830001")["touch"] is False
    rows[2] = (10.0, 13.0, 10.0, 12.5)   # +30%
    assert simulate(_bars(rows), DATES, DATES[0], code="830001")["touch"] is True


# ══════════════════════════════════════════════════════════════════════
# 4. 触板卖出按 +10%（保守口径），不按涨停价
# ══════════════════════════════════════════════════════════════════════
def test_touch_exit_is_ten_percent_regardless_of_board():
    """创业板涨停 +20%，止盈仍按 +10% 计 —— 日线定不了盘中先后顺序。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 12.0, 10.0, 11.8),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
    ]
    r = simulate(_bars(rows), DATES, DATES[0], code="300001")
    assert r["touch"] is True
    assert abs(r["net"] - (10.0 - FEE * 100)) < 1e-9, r


def test_touch_exit_takes_first_qualifying_day():
    """多日都触线 → 取最早那天（不是最优那天）。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 11.0, 10.0, 10.9),
        (10.0, 12.5, 10.0, 12.0),
        (10.0, 13.0, 10.0, 12.5),
    ]
    r = simulate(_bars(rows), DATES, DATES[0])
    assert abs(r["net"] - (10.0 - FEE * 100)) < 1e-9, r


# ══════════════════════════════════════════════════════════════════════
# 5. 未触板 → 第 3 个卖出日收盘 + 费用
# ══════════════════════════════════════════════════════════════════════
def test_no_touch_exits_at_last_sell_day_close():
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.2),
        (10.0, 10.0, 10.0, 10.4),
        (10.0, 10.0, 10.0, 10.6),     # 第 3 卖出日收 10.6 → +6%
    ]
    r = simulate(_bars(rows), DATES, DATES[0])
    assert r["touch"] is False
    assert abs(r["net"] - (6.0 - FEE * 100)) < 1e-9, r


def test_fee_applied_on_flat_trade():
    """平进平出也扣 0.3%，不是 0。"""
    r = simulate(_bars(_flat()), DATES, DATES[0])
    assert abs(r["net"] - (-FEE * 100)) < 1e-9, r


# ══════════════════════════════════════════════════════════════════════
# 6. 开盘一字/涨停买不进 → 不计入样本
# ══════════════════════════════════════════════════════════════════════
def test_open_at_limit_up_is_unbuyable():
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (11.0, 11.0, 11.0, 11.0),     # 开盘即 +10% 涨停
        (11.0, 11.0, 11.0, 11.0),
        (11.0, 11.0, 11.0, 11.0),
        (11.0, 11.0, 11.0, 11.0),
    ]
    r = simulate(_bars(rows), DATES, DATES[0])
    assert r["unbuyable"] is True, r
    assert "net" not in r, r          # 买不进就没有收益可言


def test_open_just_below_limit_is_buyable():
    """开盘 +9%（差一点点没封死）→ 可买，不是一字板。"""
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.9, 11.0, 10.8, 11.0),
        (11.0, 11.0, 11.0, 11.0),
        (11.0, 11.0, 11.0, 11.0),
        (11.0, 11.0, 11.0, 11.0),
    ]
    r = simulate(_bars(rows), DATES, DATES[0])
    assert r["unbuyable"] is False, r


# ══════════════════════════════════════════════════════════════════════
# 7. 深亏用持有期最低价（含入场日）
# ══════════════════════════════════════════════════════════════════════
def test_deep_loss_uses_lowest_low_including_entry_day():
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 8.4, 9.0),       # 入场日盘中 −16%
        (9.0, 9.0, 9.0, 9.0),
        (9.0, 9.0, 9.0, 9.0),
        (9.0, 9.0, 9.0, 9.0),
    ]
    r = simulate(_bars(rows), DATES, DATES[0])
    assert r["deep"] is True, r


def test_deep_loss_not_triggered_at_14pct():
    rows = [
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 8.6, 9.0),       # −14%，不到阈值
        (9.0, 9.0, 9.0, 9.0),
        (9.0, 9.0, 9.0, 9.0),
        (9.0, 9.0, 9.0, 9.0),
    ]
    assert simulate(_bars(rows), DATES, DATES[0])["deep"] is False


# ══════════════════════════════════════════════════════════════════════
# 8. 数据不足 → None（不当作亏损）
# ══════════════════════════════════════════════════════════════════════
def test_insufficient_sell_days_returns_none():
    """只有入场日没有后续卖出日 → None，不参与统计。"""
    bars = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0)])
    assert simulate(bars, DATES, DATES[0]) is None


def test_missing_entry_bar_returns_none():
    """入场日在日历里有、但 K 线缺该日（停牌）→ None。"""
    bars = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0)])   # 只有 23/26
    assert simulate(bars, DATES, "2026-10-27") is None


# ══════════════════════════════════════════════════════════════════════
# 9. 报表聚合口径
# ══════════════════════════════════════════════════════════════════════
def test_aggregate_skips_unbuyable_and_none():
    """买不进（unbuyable）与数据不足（None）都不进样本 —— 分母不含它们。"""
    ok = simulate(_bars(_flat()), DATES, DATES[0])          # 平盘，net = −0.3%
    unbuy = simulate(_bars([(10.0, 10.0, 10.0, 10.0),
                            (11.0, 11.0, 11.0, 11.0),
                            (11.0, 11.0, 11.0, 11.0),
                            (11.0, 11.0, 11.0, 11.0),
                            (11.0, 11.0, 11.0, 11.0)]), DATES, DATES[0])
    agg = aggregate([ok, unbuy, None])
    assert agg["n"] == 1, agg
    assert abs(agg["net"] - (-0.3)) < 1e-9, agg


def test_aggregate_reports_win_touch_and_deep():
    win = simulate(_bars([(10.0, 10.0, 10.0, 10.0),
                          (10.0, 10.0, 10.0, 10.0),
                          (10.0, 11.0, 10.0, 10.9),
                          (10.0, 10.0, 10.0, 11.0),
                          (10.0, 10.0, 10.0, 11.0)]), DATES, DATES[0])
    loss = simulate(_bars([(10.0, 10.0, 10.0, 10.0),
                           (10.0, 10.0, 10.0, 10.0),
                           (10.0, 10.0, 8.4, 8.5),
                           (8.5, 8.5, 8.5, 8.5),
                           (8.5, 8.5, 8.5, 8.5)]), DATES, DATES[0])
    agg = aggregate([win, loss])
    assert agg["n"] == 2
    assert abs(agg["win_rate"] - 0.5) < 1e-9, agg          # 一胜一负
    assert abs(agg["touch_rate"] - 0.5) < 1e-9, agg        # 只有 win 触板
    assert agg["deep"] == 1, agg                           # loss 深亏 −15%
    assert abs(agg["net"] - (win["net"] + loss["net"]) / 2) < 1e-9


def test_aggregate_empty_returns_zero_n():
    assert aggregate([]) == {"n": 0}
    assert aggregate([None, None]) == {"n": 0}


# ══════════════════════════════════════════════════════════════════════
# 10. 报表六项标准口径（n / 净收益 / T+1 / T+3 / 触涨停 / 深亏15%）
# ══════════════════════════════════════════════════════════════════════
def test_aggregate_exposes_six_report_fields():
    """六项必须在（触涨停/深亏是**只数**，收益/涨幅是均值%）。"""
    win = simulate(_bars([(10.0, 10.0, 10.0, 10.0),
                          (10.0, 10.0, 10.0, 10.0),
                          (10.0, 11.0, 10.0, 10.9),
                          (10.0, 10.0, 10.0, 11.0),
                          (10.0, 10.0, 10.0, 11.0)]), DATES, DATES[0])
    loss = simulate(_bars([(10.0, 10.0, 10.0, 10.0),
                           (10.0, 10.0, 10.0, 10.0),
                           (10.0, 10.0, 8.4, 8.5),
                           (8.5, 8.5, 8.5, 8.5),
                           (8.5, 8.5, 8.5, 8.5)]), DATES, DATES[0])
    agg = aggregate([win, loss])
    for f in REPORT_FIELDS:
        assert f in agg, (f, agg)
    assert agg["n"] == 2
    assert agg["touch"] == 1, agg        # 计数，不是比率
    assert agg["touch_rate"] == 0.5, agg  # 比率仍在（能力不降）
    assert agg["deep"] == 1, agg


def test_format_report_prints_six_columns():
    agg = aggregate([simulate(_bars(_flat()), DATES, DATES[0])])
    line = format_report("测试组", agg)
    head = report_header()
    assert "测试组" in line, line
    for col in REPORT_HEADER:
        assert col in head, (col, head)
    # 平盘：n=1、净 −0.3%、T+1/T+3 = 0、触涨停 0、深亏 0
    assert "    1" in line and "-0.30" in line, line


def test_format_report_empty_group():
    assert format_report("空组", {"n": 0}) == "空组 n=0" or "n=0" in format_report("空组", {"n": 0})
