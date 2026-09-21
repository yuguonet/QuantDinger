"""strategies/dragon_callback.py — 龙回头策略 (14:50 滚动收盘买入, StrategyBase 插件实现)

规则 (2026-09-20 改为"反转日收盘买入", 与 test_dragon 选股集一致、无前视):
  涨停 → 回调 3~11 天(自 lu_idx+1 起连续 close<涨停收盘) → 末期缩量小阴落在 day i-1
  (day i-1 涨幅 ∈ (-max_last_chg, -0.5)) + 量比 ∈ [min_vol_ratio, max_vol_ratio)) → day i 反转
  (收盘 >= 涨停收盘 = 龙已回来) → 反转日收盘买入。test_dragon 买回调终点(含 1 天后视), auto 买
  终点次日(无前视), 两者选股集合一致(仅差 1 天)。
  出场: 峰值逃顶 + 单一追踪止损 + 固定止损 + 到期 (见 DRAGON_CB_PARAMS / 出场引擎注释)。

生命周期 (2026-09-19 用户裁定: **本策略无 next_open 模式**, 恒为收盘买入):
  14:30  scheduler Task "knife_scan" 启动 → 等待滚动起点
  14:50  起每分钟一轮滚动预览 (rolling_preview=True), 用户 14:51~14:59 决策
  14:56  终审 (ScanSpec.entry_at) → 落库 buy_today, entry_price=快照价 (≈D0 收盘)
  15:01  confirm_decision → holding (隔夜持有; 出场由盘后收盘重放判定)
  盘后   run_scan 只扫 kind=daily_close → 本策略已自动退出 17:25 dragon_scan

盘中 D0 ≈ 真实 D0: 14:50~14:56 用当日累计快照合成 D0 bar (hub.synth_bar 口径: close=最新价,
  volume=当日累计量) 后按同一规则判定, 与真实收盘误差通常 <0.5%。量比在 14:56 略偏低
  (当日量未走完) → 极小概率纳入"收盘后量比>0.8"的边界样本 (固有近似, 已在阈值上留白)。

易错点:
  - 判定必须走 ctx={"latest","series"}; 无盘中快照返回空 (回测重放/误调用安全);
  - 日线 bars[-1]=昨日 (盘中 1D 未回填) → 合成 bar 必须 append 到末位;
  - 量比 = D0累计量 / D-1全日量 (与 knife_catch 同口径, 快照 cumulative 与日线 volume 同单位);
  - 反转日语义(2026-09-20): 信号日=回调终点次日(反转日), 要求当日收盘>=涨停收盘; 不再用
    as-of"首个符合日"(那会把涨停后未反弹的坠落刀当成龙回头, 致信号量 1357 vs td 93);
  - U1~U4 锚定涨停日 (@D0 评估换手会误杀 — D0 是缩量小阴日), 盘中路径由 run_scan_knife 施加;
  - exit 重放 stop_at_idx 语义: idx>stop_at_idx 即截断 open=True (盘中重放当天未收盘);
  - 出场引擎恒按收盘买入口径 (首个持仓日 = 买入日次日), 无 buy_mode 分支。
"""
from __future__ import annotations

from app.market_cn.auto.core.indicators import (
    calc_macd, calc_psy, calc_roc, is_macd_golden_cross,
    is_macd_hist_shrinking_negative, is_macd_hist_turning_positive, rsi,
)
from app.market_cn.auto.core.market import find_limit_ups, get_board_name, get_board_type, is_limit_up
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "dragon_callback"
STRATEGY_LABEL = "龙回头"

DRAGON_CB_PARAMS = dict(
    # ===== 入场 (2026-09-20 改为"反转日收盘买入", 与 test_dragon 选股集一致、无前视) =====
    # 规则: 涨停 → 回调 3~11 天(自 lu_idx+1 起连续 close<涨停收盘) → 末期缩量小阴落在 day i-1
    #       (day i-1 涨幅 ∈ (-max_last_chg, -0.5)) + 量比 ∈ [min_vol_ratio, max_vol_ratio)) → day i 反转
    #       (收盘 >= 涨停收盘 = 龙已回来) → 反转日收盘买入。
    # 与 test_dragon 选股集合一致: td 买回调终点(pullback_end, 含1天后视), auto 买终点次日
    #   (反转日, 无前视), 两者仅差 1 天; td 的"次日收>=涨停收"确认在 auto 中内化为 day i
    #   收盘>=涨停收的入场必要条件。
    # 被替换的旧"方案2"链(找龙占比>=70% / gap[5,7] / 拐点OR / 龙强度 / D0>-4%)见 git 历史。
    min_pullback_days=3,
    max_pullback_days=11,
    max_last_chg=3.0,          # 末期小阴: -max_last_chg < last_chg < -0.5
    min_vol_ratio=0.5,         # 信号日量比 = D0量/D-1量
    max_vol_ratio=0.8,
    # ===== 出场 (对齐 test_dragon.run_backtest; 执行层保留 auto 现实化: T+1/跳空/跌停) =====
    hold_days=15,
    stop_loss=-5.0,
    trailing_stop=-5.0,        # 单一追踪(自入场后峰值); 原分段追踪 lo-3/hi-3/switch3 已按裁定替换
    peak_exit_ret=7.0,         # 峰值逃顶: 收盘涨幅 > 7%
    peak_exit_upper=30.0,      # 且上影线 > 30%
)

_SHORTLIST_SLACK_PCT = 0.15    # 盘中预筛容差(百分点): 吸收原始价/复权价微差 → 放宽保超集



# ================================================================
# 出场模拟 (原 core.run_backtest_dragon_callback, 原样移植)
# 2026-09-09 现实化修正 (tmp/_dragon_intraday_exit.py E1 口径):
#   ① T+1: 买入当日(d=1)不可卖出 — 仅更新峰值/估值, 全部出场判定从 d=2 起;
#   ② 跳空穿越: 触发日开盘价低于触发价 → 按开盘价成交 (跳空低开只能按开盘卖);
#   ③ 跌停无法卖出: 一字跌停整日跳过; 成交价触及跌停 → 顺延次日开盘强平。
#   注意: 追踪线与止损线同日双触发取 max(价格连续, 先穿过更高触发线);
#         峰值逃顶仍是收盘判定优先 — 若当日盘中已触及追踪线, 现实中会先按
#         追踪线成交, 此处保留"收盘逃顶优先"的原设计语义 (已知理想化)。
# ================================================================

# 跌停价原语收编至 common/exec_cn.py (C 阶段); 别名保持调用点不变
from app.market_cn.auto.core.exec import (
    fill_blocked_by_limit_dn,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
)
from app.market_cn.auto.probe import DayTrace as _DayTrace, \
    sample_feats as _probe_sample_feats   # 探针框架件 (无环; 只提供通用特征/标签)


# ================================================================
# 调试通道 (2026-09-10 用户裁定: 调龙回头只改本文件, 框架层 probe.py 零改动)
# ----------------------------------------------------------------
# 规则:
#   - 仅 debug 模式 (probe 非 None) 才计算并写入 sample.labels; 判定与实盘路径
#     绝不读取本段任何内容 (改这里不影响任何一笔交易)。
#   - 改口径只改本段; **归档键名保持稳定** (离线脚本/存档按键名读取)。
# 标签三组:
#   1) 固定持有 N 日       ret_d{N}c / peak{N} / mae{N} / peak_day
#   2) 峰值回撤出场(多档)  ret_tr{t} / peak_tr{t} / mae_tr{t} / day_tr{t} /
#                          rsn_tr{t} / cap_tr{t}
#   3) 波次视角            wave_amp (整波涨幅) / entry_lag (入场推后天数)
# ================================================================
DEBUG_HOLD_DAYS = 7            # 固定持有交易日数
DEBUG_TRAILS = (4, 6, 8, 12)   # 峰值回撤阈值序列 (一次回测扫多档 = 阈值敏感性前置)
DEBUG_MAX_HOLD = 10            # 无波次窗口时的最大持有交易日
DEBUG_WAVE_DAYS = 20           # 波次窗口长度 (自"第一条规则通过日"起)


def _fixed_hold_labels(bars, i, entry, days=DEBUG_HOLD_DAYS):
    """固定持有 days 日标签: 第 days 日收盘无条件卖出 (排除出场引擎差异)。

    用途: 规则归因 — 用**同一条**出场规则衡量各入场规则的贡献。
    口径: 入场=D+1 开盘; peak/mae 取持有段(含入场日)极值相对入场价%;
    视野不足 (i+days 越界) → ret 记 None (=censored), peak/mae 仍记。
    """
    n = len(bars)
    out = {}
    if not entry or entry <= 0 or i + 1 >= n:
        return out
    last = min(i + days, n - 1)
    highs = [float(bars[k]["high"]) for k in range(i + 1, last + 1)]
    lows = [float(bars[k]["low"]) for k in range(i + 1, last + 1)]
    if highs:
        out[f"peak{days}"] = round((max(highs) / entry - 1) * 100, 2)
        out["peak_day"] = int(highs.index(max(highs)) + 1)     # 第几个持有日见顶(1-based)
        out[f"mae{days}"] = round((min(lows) / entry - 1) * 100, 2)
    if i + days < n:                       # 完整视野才给出场收益 (否则 censored)
        out[f"ret_d{days}c"] = round((float(bars[i + days]["close"]) / entry - 1) * 100, 2)
    return out


def _trail_exit_labels(bars, i, entry, trail_pct, max_days=DEBUG_MAX_HOLD,
                       wave_start=None, wave_days=DEBUG_WAVE_DAYS):
    """峰值回撤出场标签 (路径依赖, 衡量"这笔行情给出多少可捕获空间")。

    为什么需要它: 固定持有 N 日衡量的是"第 N 日收盘的随机点位", 与入场质量关系弱
    (好行情可能因第 N 日恰好回调而记亏)。峰值回撤出场是**可操作**的固定规则 (追踪
    止盈): 涨越高、回撤触发越晚 → 捕获越多; 低峰值票在 peak≈entry 处就被小幅回撤
    扫出 → 天然滤掉"没肉"的票, 一路阴跌则跌满阈值出局 (自带止损)。

    口径: 入场=D+1 开盘 (entry); 从 D+1 起逐日 peak=max(peak, high_k),
      当 close_k <= peak*(1-trail_pct/100) → 当日收盘出场 (rsn=trail);
      始终未触发 → 窗口终点收盘出场 (rsn=expire)。

    wave_start (波次窗口口径, 2026-09-10 用户裁定): "第一条规则(找龙)通过日的 bar
      索引; 给定时窗口终点 = wave_start + wave_days - 1 (默认 20 交易日), 而非
      i + max_days — 原点固定在行情起点, 让龙头股 (常见 50%+ 涨幅) 有充分时间展开;
      **峰值仍从入场日 i+1 起追踪** (入场前涨幅买不到, 不能算进可捕获空间)。
      推论: 买入日被推后越久 → 剩余窗口越短、入场价越高 → 可捕获空间越小 → 自然淘汰;
      入场日已超出窗口终点 → rsn=late, ret 记 None。
    """
    n = len(bars)
    sf = f"{trail_pct:g}"
    out = {}
    if not entry or entry <= 0 or i + 1 >= n:
        return out
    wnd_end = (int(wave_start) + int(wave_days) - 1) if wave_start is not None \
        else i + max_days
    if i + 1 > wnd_end:                 # 入场日已超出波次窗口 (信号推后太多) → 淘汰
        out[f"rsn_tr{sf}"] = "late"
        return out
    last = min(wnd_end, n - 1)
    complete = wnd_end <= n - 1         # 窗口完整可见才给出场收益 (否则 censored)
    k = trail_pct / 100.0
    peak = mae_px = 0.0
    exit_day = exit_px = None
    reason = None
    for j in range(i + 1, last + 1):
        h = float(bars[j]["high"] or 0)
        lo = float(bars[j]["low"] or 0)
        c = float(bars[j]["close"] or 0)
        if h > 0:
            peak = h if peak == 0 else max(peak, h)
        if lo > 0:
            mae_px = lo if mae_px == 0 else min(mae_px, lo)
        if peak > 0 and c > 0 and c <= peak * (1.0 - k):
            exit_day, exit_px, reason = j - i, c, "trail"
            break
    if exit_day is None and complete:
        exit_day = last - i
        exit_px = float(bars[last]["close"] or 0)
        reason = "expire"
    if peak > 0:
        out[f"peak_tr{sf}"] = round((peak / entry - 1) * 100, 2)
    if mae_px > 0:
        out[f"mae_tr{sf}"] = round((mae_px / entry - 1) * 100, 2)
    if exit_day is not None and exit_px > 0:
        ret = round((exit_px / entry - 1) * 100, 2)
        out[f"ret_tr{sf}"] = ret
        out[f"day_tr{sf}"] = int(exit_day)
        out[f"rsn_tr{sf}"] = reason
        if peak > 0:
            pk = (peak / entry - 1) * 100
            out[f"cap_tr{sf}"] = round(ret / pk, 2) if pk > 0.5 else None
    return out


def _wave_labels(bars, i, wave_start, wave_days=DEBUG_WAVE_DAYS):
    """波次视角标签: 整波涨幅 (行情起点收盘 → 窗口内最高) + 入场推后天数。

    用途: 区分"票本身没肉"与"买晚了 / 出场没兑现" — wave_amp 大但 peak_tr 小 =
    行情有肉却没吃到 (出场问题或入场过晚)。
    """
    out = {}
    n = len(bars)
    if wave_start is None:
        return out
    ws = int(wave_start)
    if not 0 <= ws < n:
        return out
    wend = min(ws + int(wave_days) - 1, n - 1)
    base = float(bars[ws]["close"] or 0)
    wmax = max((float(bars[k]["high"] or 0) for k in range(ws, wend + 1)), default=0)
    if base > 0 and wmax > 0:
        out["wave_amp"] = round((wmax / base - 1) * 100, 2)
    out["entry_lag"] = int(i) - ws      # 入场决策日相对波次起点的推后天数
    return out


def _dragon_debug_labels(bars, i, entry, wave_start=None):
    """本策略调试标签全集 (固定持有 + 多档峰值回撤 + 波次视角)。"""
    out = {}
    if not entry or entry <= 0:
        return out
    if DEBUG_HOLD_DAYS:
        out.update(_fixed_hold_labels(bars, i, entry, days=DEBUG_HOLD_DAYS))
    for t in DEBUG_TRAILS:
        out.update(_trail_exit_labels(bars, i, entry, trail_pct=t,
                                      wave_start=wave_start))
    out.update(_wave_labels(bars, i, wave_start))
    return out


def _dragon_sample_feats(bars, i, code, stock_info=None, wave_start=None, entry=None):
    """框架通用特征/标签 + 本策略调试标签 (仅 probe 调用; 判定路径不读)。

    entry: 入场基准价 —— 收盘买入口径必须传 **D0 收盘价** (缺省才回落框架的
    entry_d1o=D1开盘口径)。基准不一致会让归档样本的收益标签整体失真, 故调用点显式传。
    """
    base = _probe_sample_feats(bars, i, code, stock_info=stock_info)
    labels = base.get("labels") or {}
    entry = entry or labels.get("entry_d1o")
    if entry:
        labels.update(_dragon_debug_labels(bars, i, entry, wave_start))
    base["labels"] = labels
    return base


def run_backtest_dragon_callback(bars, entry_idx, entry_price, hold_days=None,
                                 stop_loss=None, board_type="main", stop_at_idx=None,
                                 **params):
    """龙回头出场模拟 (2026-09-19 对齐 test_dragon.run_backtest 的判定与参数)。

    入场恒为**当日收盘买入** (2026-09-19 用户裁定: 本策略无 next_open 模式):
      entry_idx 当日收盘成交 → 该 bar 的高低**均为买入前**, 不计入 peak;
      首个持仓日 = entry_idx+1 (买入次日起即可卖, T+1 不构成约束 —— 收盘买入不是"当日买当日卖")。
    规则: 峰值逃顶(收盘涨幅>peak_exit_ret 且上影>peak_exit_upper → 收盘卖) / 单一追踪止损
    (低点<=峰值*(1+trailing_stop/100)) / 固定止损(低点<=入场*(1+stop_loss/100)) / 到期 hold_days。
    执行层保留 auto 现实化 (2026-09-09): 跳空按开盘成交 / 跌停顺延 (T+1 对收盘买入不约束)。
    成交价修正 (2026-09-20): 追踪线若被当日 high 抬高, 不再按"当日开盘价"成交 —— 只有跌破
    "开盘时已存在的线"(peak_prev 基准) 才按开盘成交, 否则按当日线成交 (与 test_dragon 逐笔一致)。
    stop_at_idx: 只模拟到该 bar 索引(盘中重放); 未触发出场 → open=True。
    返回 exit_day = 已过持仓日数 (1-based); 出场 bar 索引 = entry_idx + exit_day。
    """
    p = {**DRAGON_CB_PARAMS, **(params or {})}
    hold_days = p["hold_days"] if hold_days is None else hold_days
    stop_loss = p["stop_loss"] if stop_loss is None else stop_loss
    trailing_stop = p["trailing_stop"]
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    n = len(bars)
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ""
    capped = False
    pending_dn = False        # 触发成交价触及跌停 → 次日开盘强平
    last_unfilled = False     # 最后一日为一字跌停(整日无法卖出) → 到期顺延

    first_idx = entry_idx + 1       # 收盘买入: 首个持仓日 = 买入日(D0)次日

    for d in range(1, hold_days + 1):
        idx = first_idx + d - 1
        if idx >= n:
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        peak_prev = peak                     # 当日 high 抬高前的峰值 (开盘时已存在的线基数)
        if b["high"] > peak:
            peak = b["high"]
        prev_close = bars[idx - 1]["close"] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d, exit_reason = b["open"], d, "跌停顺延开盘"
            break

        # 一字跌停: 全天无成交可能, 持仓顺延 (不更新估值标记)
        if is_one_word_limit_dn(b, dn):
            last_unfilled = True
            continue
        last_unfilled = False

        # T+1: 收盘买入 (D0 尾盘成交) → 首个持仓日即 D1, 不适用"当日买入当日不可卖"
        # 1. 峰值逃顶 (收盘判定收盘卖)
        ret = (b["close"] / entry_price - 1) * 100
        if ret > p["peak_exit_ret"]:
            rng = b["high"] - b["low"]
            upper = (b["high"] - max(b["open"], b["close"])) / rng * 100 if rng > 0 else 0
            if upper > p["peak_exit_upper"] and b["close"] < b["high"] * 0.98:
                exit_p, exit_d, exit_reason = b["close"], d, "峰值逃顶"
                break

        # 2/3. 追踪 + 固定止损 (合并: 价格连续, 先穿过更高触发线)
        trig_t = peak * (1 + trailing_stop / 100)
        trig_s = entry_price * (1 + stop_loss / 100)
        trig = max(trig_t, trig_s)
        # 开盘时已存在的止损线 (未被当日 high 抬高); 只有跌破它才按开盘价成交, 否则
        # 该线在开盘后才形成, 用"峰值之前的开盘价"成交不可得 (fill_on_gap 误用修正)。
        trig_prev = max(peak_prev * (1 + trailing_stop / 100), trig_s)
        if b["low"] <= trig:
            fill = b["open"] if b["open"] <= trig_prev else trig
            reason = f"追踪止损{trailing_stop}%" if trig_t >= trig_s else f"止损{stop_loss}%"
            if fill_blocked_by_limit_dn(fill, dn):
                pending_dn = True   # 成交价触及跌停 → 卖不出
                continue
            exit_p, exit_d, exit_reason = fill, d, reason
            break

        exit_p, exit_d = b["close"], d

    if exit_reason == "" and not capped:
        nxt = first_idx + exit_d + 1
        while (last_unfilled or pending_dn) and nxt < n \
                and (stop_at_idx is None or nxt <= stop_at_idx):
            nb = bars[nxt]
            pc = bars[nxt - 1]["close"]
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb["low"] == nb["high"] \
                    and abs(nb["low"] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False   # 顺延日仍一字跌停, 再顺延
                nxt += 1
                continue
            exit_p, exit_d, exit_reason = nb["open"], nxt - first_idx + 1, "跌停顺延开盘"
            break
        if exit_reason == "":
            exit_reason = "持仓到期"
    return {
        "exit_price": round(exit_p, 3), "exit_day": exit_d,
        "exit_reason": exit_reason,
        "return_pct": round((exit_p / entry_price - 1) * 100, 2),
        "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
        "open": bool(capped),
    }


# ================================================================
# StrategyBase 插件实现
# ================================================================

# 旧输出字段 (facade 兼容层精确对齐; 多键/少键都会破坏逐笔对数)
_LEGACY_FIELDS = (
    "code", "board", "path", "path_label", "lu_date", "pullback_days", "signal_date",
    "signal_chg", "signal_vol_r", "signal_price", "entry_vol_r", "buy_mode",
)



def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → 旧 dragon_cb_today_d0_signals 的 dict 形态。"""
    ex = sig.extra or {}
    return {k: ex.get(k) for k in _LEGACY_FIELDS if k not in ("code", "board", "path", "path_label", "signal_date")} | {
        "code": code,
        "board": ex.get("board"),
        "path": "dragon_callback",
        "path_label": "龙回头",
        "signal_date": sig.time,
    }


@register
class DragonCallbackStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "limit_up"     # U1~U4 锚定涨停日 (D0 缩量小阴日评估会误杀)
    # 调度 (2026-09-19): 盘后 daily_close → 盘中 14:50 滚动窗口; entry_at=终审语义时刻
    scan_spec = ScanSpec(kind="intraday_window", windows=("14:50", "15:00"),
                         interval_sec=60, entry_at="14:56")
    default_params = dict(DRAGON_CB_PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近; 引擎/回测钩子经 getattr 读取)
    PROBE_STAGE_RANK = {"window": 1, "data": 2, "d0_chg": 3, "vol": 4, "pullback": 5,
                        "dedup": 6, "prefilter": 7, "signal": 8}
    # 生命周期契约 (收盘买入): 14:56 已买 → T+1 当日不可卖; 出场走收盘重放 (day_close)
    use_unified_prefilter = True      # 盘中路径的 U1~U4 由 run_scan_knife 按下述锚点施加
    entry_at_close = True
    signal_state = "buy_today"
    rolling_preview = True
    data_needs = ("daily", "snapshot")

    # ---- 盘中便宜预筛 (必要条件超集, 仅用最新快照; 免拉全市场序列/日线) ----
    def intraday_shortlist(self, snaps, mkt_gain, **params):
        """盘中便宜预筛(反转日语义, 2026-09-20 改写): 信号日是反转日(当日为上涨日, 收盘>=涨停收盘),
        故便宜预筛改为"当日非大跌"(chg > -0.5 - slack) 的超集 —— 保留所有上涨日, 仅砍掉坠落刀式大跌日。
        (旧语义"当日为缩量小阴"已不适用: 现在小阴落在 day i-1, 信号日在 day i)

        与 scan_signals 读**同一时刻**快照 → 无需跨时刻放宽; 仅因快照(原始价)与 1D qfq
        日线在除权日有微差, 两侧各留 _SHORTLIST_SLACK_PCT 容差 (宁可多留不可误杀)。
        量比/回调结构需要日线 → 留给 scan_signals 完整判定, 本层只做廉价砍量。
        无市场门控 (龙回头规则不含大盘条件, mkt_gain 仅记录不拦截)。
        """
        p = self.merged_params(params or None)
        # 反转日语义: 信号日是反转日(当日为上涨日, 收盘>=涨停收盘), 故便宜预筛改为"当日非大跌"
        #   (chg > -0.5 - slack) 的超集 —— 保留所有上涨日, 仅砍掉坠落刀式大跌日。
        #   (旧语义"当日为缩量小阴"已不适用: 现在小阴落在 day i-1, 信号日在 day i)
        lo = -0.5 - _SHORTLIST_SLACK_PCT
        out = {}
        for code, snap in snaps.items():
            try:
                last = float(snap.get("last") or 0)
                pc = float(snap.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last <= 0 or pc <= 0:
                continue
            chg = (last / pc - 1) * 100
            if chg > lo:
                out[code] = snap
        return out

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     probe=None, **params):
        """龙回头 反转日收盘信号 (2026-09-20 改为反转日语义, 选股集与 test_dragon 一致、无前视)。

        规则: 涨停(lu) → 回调 3~11 天(lu+1..i-1 连续 close<涨停收盘) → 末期缩量小阴落在 day i-1
          (prev_chg ∈ (-max_last_chg, -0.5)) + 量比 ∈ [min_vol_ratio, max_vol_ratio)) → day i 反转
          (收盘 >= 涨停收盘 = 龙已回来) → 反转日(i)收盘买入 Signal(至多1笔)。
        与 test_dragon 选股集合一致: td 买回调终点(pullback_end, 含1天后视), auto 买终点次日
          (反转日, 无前视), 两者仅差 1 天; td 的"次日收>=涨停收"确认在 auto 中内化为 day i
          收盘>=涨停收的入场必要条件。

        盘中 (ctx={"latest","series"}): 当日 1D bar 未回填时, 用当日累计快照合成 D0 bar
          (hub.synth_bar) 后按同一规则判定; day i=合成bar(i-1=昨日真实日线), 14:50~14:56
          判定口径 ≈ 真实反转日收盘口径。
        日线回测/重放 (ctx 为空): 直接判定 bars 末根。
        limit_ups: 预计算涨停索引 (回测优化, None 则现算)。probe=None=零开销。
        """
        p = self.merged_params(params or None)
        ctx = ctx or {}
        snap = ctx.get("latest")
        series = ctx.get("series") or []
        if snap and series:
            today = str(snap.get("time") or "")[:10]
            if today and (not bars or str(bars[-1]["time"])[:10] < today):
                from app.market_cn.auto.core.data.hub import synth_bar
                bars = list(bars) + [synth_bar(series, today)]
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < 3:
            return result
        i = n - 1                       # 候选 = 反转日(day i)
        if i < 2:
            return result
        board_type = get_board_type(code)
        d0 = bars[i]                     # 反转日 bar (盘中 = synth bar)
        d_prev = bars[i - 1]             # 末期缩量小阴日(回调终点)
        d_prev2 = bars[i - 2] if i - 2 >= 0 else None
        if d_prev2 is None or not d_prev2.get("close") or float(d_prev2["close"]) <= 0:
            return result

        if probe is not None:
            def _tr(stage, **kw):
                probe.trace(stage, code=code, d0_date=str(bars[i]["time"])[:10], **kw)
        else:
            _tr = None

        # 末期缩量小阴(回调终点 day i-1): -max_last_chg < prev_chg < -0.5, 量比 ∈ [0.5,0.8)
        prev_chg = (float(d_prev["close"]) / float(d_prev2["close"]) - 1) * 100
        prev_vol = float(d_prev["volume"]) / float(d_prev2["volume"]) if float(d_prev2["volume"]) > 0 else 0
        if not (-p["max_last_chg"] < prev_chg < -0.5):
            if _tr:
                _tr("d0_chg", signal_chg=round(prev_chg, 2))
            return result
        if not (p["min_vol_ratio"] <= prev_vol < p["max_vol_ratio"]):
            if _tr:
                _tr("vol", vol_r=round(prev_vol, 2))
            return result

        # 反转日确认: day i 收盘 >= 涨停收盘(龙已回来); lu+1..i-1 连续 close<涨停收盘
        for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
            lu_close = float(bars[lu_idx]["close"])
            if lu_close <= 0:
                continue
            pullback_days = (i - 1) - lu_idx          # 回调终点 = day i-1
            if pullback_days < p["min_pullback_days"] or pullback_days > p["max_pullback_days"]:
                continue
            if any(float(bars[j]["close"]) >= lu_close for j in range(lu_idx + 1, i)):
                continue                               # lu+1..i-1 非全下跌 → 非连续回调
            if float(d0["close"]) < lu_close:
                continue                               # day i 未反转回到涨停收盘 → 坠落刀, 拦截
            if _tr:
                _tr("signal", lu_date=str(bars[lu_idx]["time"])[:10],
                    pullback_days=pullback_days, signal_chg=round(prev_chg, 2),
                    vol_r=round(prev_vol, 2))
            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=0,   # 历史口径: 无评分体系, 库内 score 恒0
                price=round(float(d0["close"]), 3),
                label="龙回头",
                extra={
                    "board": get_board_name(code),
                    "lu_date": bars[lu_idx]["time"],
                    "pullback_days": pullback_days,
                    "signal_chg": round(prev_chg, 2),     # 末期小阴(回调终点)涨幅
                    "signal_vol_r": round(prev_vol, 2),   # 末期小阴量比
                    "signal_price": round(float(d0["close"]), 3),  # 反转日收盘
                    "entry_vol_r": round(prev_vol, 2),
                    "buy_mode": "signal_close",
                },
            ))
            break
        if not result and _tr:
            _tr("pullback")
        return result

    # ---- D1 竞价处置 (收盘买入 → 无开盘买入步骤) ----
    def entry_decision(self, row, snap=None, **params):
        """收盘买入: 入场已在 D0 14:56 完成, D1 无开盘买入动作 (与 tail_oversold 同生命周期)。

        monitor 开盘窗口只处理 watch_pending 行, 本策略信号落库即 buy_today → 本判定实际
        不会被调用; 保留实现是为契约完整 (避免默认 gap 带实现意外拦截)。
        """
        return EntryDecision(True, "尾盘收盘买入, 无开盘步骤")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """无确认步骤: 买入日收盘直接持仓 (出场由收盘重放判定)。

        snap={"series":[...当日快照序列]}; d1_chg 按 signal_price 基准 (旧 evaluate_confirm 口径)。
        返回 None = 无法判定 (快照缺失), monitor 不做状态转移。
        """
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        prev_close = float(row.get("signal_price") or 0)
        if prev_close <= 0:
            return None
        d1_chg = (float(series[-1]["last"] or 0) / prev_close - 1) * 100
        return ConfirmDecision(True, "ok", d1_chg=round(d1_chg, 2),
                               detail={"confirm": "ok", "confirm_strong": False})

    def initial_stop(self, code, entry_price):
        """stop_loss (详见 DRAGON_CB_PARAMS), 板块不分档 (与回测一致)。"""
        return round(entry_price * (1 + DRAGON_CB_PARAMS["stop_loss"] / 100), 3)

    # ---- 时间线引擎出场 (intraday_window 回测) ----
    def intraday_exit(self, bars, code, entry_date, entry_price, entry_idx=None, **params):
        """覆盖基类默认 (D1 开盘卖): 龙回头是多日持有, 出场必须走自身引擎。

        复用 run_backtest_dragon_callback (收盘买入 → 15日追踪/止损/逃顶), 出场 bar 索引
        = entry_idx + exit_day。视野不足/未闭合 → None (该笔不计)。
        """
        if entry_idx is None or entry_price <= 0:
            return None
        board = get_board_type(code)
        r = run_backtest_dragon_callback(bars, entry_idx, entry_price, board_type=board)
        if not r or r.get("open") or not r.get("exit_reason"):
            return None
        if int(r["exit_day"] or 0) < 1:
            return None        # 数据末尾无持仓日 (引擎走空) → 假单, 不产出
        k = entry_idx + int(r["exit_day"])
        if k >= len(bars):
            return None
        return {"exit_date": str(bars[k]["time"])[:10], "exit_price": r["exit_price"],
                "exit_day": r["exit_day"], "exit_reason": r["exit_reason"],
                "return_pct": r["return_pct"], "peak_return_pct": r["peak_return_pct"]}

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘重放: 复用 run_backtest_dragon_callback (stop_at_idx=今日截断语义)。

        snap={"mode":"day_close","bars":[...],"entry_idx":int}; live 模式 → hold (硬止损在 monitor 主循环)。"""
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        board = get_board_type(row.get("code", ""))
        today_idx = len(bars) - 1
        r = run_backtest_dragon_callback(bars, entry_idx, entry_price, board_type=board,
                                         stop_at_idx=today_idx)
        if r and not r.get("open") and r.get("exit_reason"):
            # 收盘买入: 首个持仓日 = entry_idx+1 → 出场 bar 索引 = entry_idx + exit_day
            if entry_idx + int(r.get("exit_day") or 0) != today_idx:
                return ExitDecision("hold")
            # "持仓到期" 仅在**已满 hold_days 持仓日**时才算真出场: 重放窗口短于 hold_days 时
            # 引擎同样会走到数据末尾, 若照单全收 → 买入当日即被误标"持仓到期"出场。
            held = today_idx - entry_idx
            if r["exit_reason"] == "持仓到期" \
                    and held < int(self.merged_params()["hold_days"]):
                return ExitDecision("hold")
            return ExitDecision("exit", reason=r["exit_reason"], price=float(r["exit_price"]))
        return ExitDecision("hold")

    # ---- 回测钩子 (2026-09-10 自 backtest.backtest_dragon_stock 逐字搬入, 对数零差异) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                       probe=None):
        """单股龙回头全历史回测, 返回 trades 列表 (入场 = 信号日 D0 收盘价, 无 next_open)。

        编排 (枚举/去重±4/预过滤锚点/预筛) 是策略规则故归位本插件; 出场模拟
        run_backtest_dragon_callback 在本文件 (策略专用出场规则)。
        probe: 调试探针 (None=零开销) — 每个到达完整判定的决策日产出一行
        sample (特征+标签+当日最深判定阶段), 廉价预筛跳过的日不采样 (纯噪声)。
        """
        from app.market_cn.auto.core.filters import unified_prefilter
        board_type = get_board_type(code)
        n = len(bars)
        if n < 5:
            return []
        lu_all = find_limit_ups(bars, board_type)
        # 廉价预筛参数: 与 scan_signals 实际用的默认参数同源 (回测不走 config 覆盖,
        # 与旧 facade 调用路径一致); 取 self.default_params 而非 merged_params。
        pd_min = self.default_params["min_pullback_days"]
        pd_max = self.default_params["max_pullback_days"]
        trades = []
        used_ranges = []
        # 波次起点 (波次窗口口径, 2026-09-10 用户裁定): 最近一次"第一条规则(找龙)未通过"
        # 的次日 = 本波行情起点; 供探针标签用 (判定路径不读)。廉价预筛跳过日与
        # stage=dragon/no_candidate 都算"找龙未通过" → 波次断点。
        wave_start = 0

        for i in range(2, n - 1):
            # 廉价预筛 (数学必要条件超集, 非加规则 — 行为零差异): 反转日语义下, 信号日 i
            #   的回调终点=day i-1, 故 lu 必落在 [i-1-pd_max, i-1-pd_min]; 不在该窗口则
            #   不可能出信号, 跳过昂贵的逐日全量判定。若覆盖 min/max_pullback_days 须同步此处。
            #   去掉旧"d0c<涨停收"价格门槛(反转日收盘>=涨停收, 旧门槛会误杀所有反转信号)。
            if not any(pd_min + 1 <= i - j <= pd_max + 1 for j in lu_all):
                wave_start = i + 1      # 该日不可能出信号 (找龙未通过) → 波次断点
                continue

            # 逐日候选判定: 与实盘 scan 完全同一函数 (切片 as_of 语义; 经 facade 等价路径)
            # debug 模式: day_tr 聚合该日全部候选的判定步落点 (_DayTrace, probe=None 零开销)
            day_tr = _DayTrace() if probe is not None else None
            sigs = [_signal_to_legacy_dict(s, code) for s in self.scan_signals(
                bars[:i + 1], code, limit_ups=[j for j in lu_all if j < i],
                probe=day_tr)]

            if not sigs:
                if probe is not None:
                    stage = max((t["stage"] for t in day_tr.items),
                                key=lambda s: self.PROBE_STAGE_RANK.get(s, 0),
                                default="no_candidate")
                    probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                                 stage=stage, rule_trace=day_tr.items,
                                 **_dragon_sample_feats(bars, i, code,
                                                        stock_info=stock_info, wave_start=wave_start, entry=float(bars[i]["close"])))
                    if stage in ("dragon", "no_candidate"):
                        wave_start = i + 1      # 找龙未通过 → 波次断点
                continue
            sig = sigs[0]
            lu_idx = _find_bar_idx(bars, sig["lu_date"])

            # 去重 (±4天内跳过); 注意去重在过滤之前 (对数基线行为)
            skip = False
            for (s, e) in used_ranges:
                if abs(i - s) <= 4 or abs(i - e) <= 4:
                    skip = True
                    break
            if skip:
                if probe is not None:
                    probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                                 stage="dedup", rule_trace=day_tr.items, sig=sig,
                                 **_dragon_sample_feats(bars, i, code,
                                                        stock_info=stock_info, wave_start=wave_start, entry=float(bars[i]["close"])))
                continue
            used_ranges.append((lu_idx, i))

            # U1~U4 预过滤 (锚定涨停日, 无未来函数)
            if use_prefilter and lu_idx > 0:
                ok, fails = unified_prefilter(bars, lu_idx, code, stock_info)
                if not ok:
                    if probe is not None:
                        probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                                     stage="prefilter", rule_trace=day_tr.items,
                                     sig=sig, u_fails=list(fails),
                                     **_dragon_sample_feats(bars, i, code,
                                                            stock_info=stock_info, wave_start=wave_start, entry=float(bars[i]["close"])))
                    continue

            # 入场: 当日(D0=反转日)收盘价 —— 收盘买入 (无 next_open 模式, 2026-09-19 用户裁定)
            d0 = bars[i]
            d1 = bars[i + 1]           # 首个持仓日 (仅用于报告字段)
            entry_price = float(d0["close"] or 0)
            if entry_price <= 0:
                continue

            result = run_backtest_dragon_callback(
                bars, i, entry_price, board_type=board_type)
            if not result:
                if probe is not None:
                    probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                                 stage="engine_skip", rule_trace=day_tr.items,
                                 sig=sig, **_dragon_sample_feats(bars, i, code,
                                                                 stock_info=stock_info, wave_start=wave_start, entry=float(d0["close"])))
                continue

            if probe is not None:
                probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                             stage="signal", rule_trace=day_tr.items, sig=sig,
                             engine={k: result.get(k) for k in
                                     ("return_pct", "peak_return_pct",
                                      "exit_reason", "exit_day")},
                             **_dragon_sample_feats(bars, i, code,
                                                    stock_info=stock_info, wave_start=wave_start, entry=float(d0["close"])))

            trades.append({
                **sig,
                "entry_date": d0["time"],
                "entry_price": round(entry_price, 3),
                "buy_mode": "signal_close",
                "d1_gap": round((float(d1["open"]) / entry_price - 1) * 100, 2),
                "d1_change": round((float(d1["close"]) / entry_price - 1) * 100, 2),
                **result,
            })

        return trades


def _find_bar_idx(bars, date_str):
    """日期串 → bars 索引; 未找到返回 None (回测钩子用, 原 backtest 内联助手)。"""
    for i, b in enumerate(bars):
        if b["time"] == date_str:
            return i
    return None
