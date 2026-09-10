"""strategies/dragon_callback.py — 龙回头策略 ("方案2", StrategyBase 插件实现, Phase 2 迁移)

实现已迁移至本文件; core.dragon_cb_today_d0_signals / run_backtest_dragon_callback /
DRAGON_CB_PARAMS 为 facade 转发 (test_dragon.py / dragon_scan / dragon_monitor 共用)。

规则框架 (2026-09-06 与 test_dragon.py 同步, 依据 龙回头优化分析_20260906/):
  找龙(滑动窗口涨停占比>=70%) → 回调 gap[5,6] → 拐点OR(MA20支撑 | 深跌释放 | 买盘承接)
  → 信号质量排除(阴线>=60% / RSI6<30 / 距MA20<-8%) → U1~U4(@涨停日) → D1开盘gap(-3,+2]买
  出场: 分段追踪(-8/-3) + 固定止损-8 + 峰值逃顶 + 到期7天

易错点:
  - U1~U4 锚定涨停日 (@D0 评估换手会误杀 — D0 是缩量小阴日);
  - 找龙窗口 start=max(1, lu_idx-window), total_days<3 跳过 — 边界勿动;
  - exit 重放 stop_at_idx 语义: idx>stop_at_idx 即截断 open=True (盘中重放当天未收盘);
  - tech_score 仅输出参考 (评分门槛已关闭, 实验结论无判别力), 不参与过滤。
"""
from __future__ import annotations

from app.market_cn.auto.common.indicators import (
    calc_macd, calc_psy, calc_roc, is_macd_golden_cross,
    is_macd_hist_shrinking_negative, is_macd_hist_turning_positive, rsi,
)
from app.market_cn.auto.common.market import find_limit_ups, get_board_name, get_board_type, is_limit_up
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "dragon_callback"
STRATEGY_LABEL = "龙回头"

DRAGON_CB_PARAMS = dict(
    # --- 找龙: 滑动窗口涨停占比 ---
    dragon_ratio=0.7,
    dragon_windows=[4, 5, 7, 10, 15, 20],
    # --- 回调窗口 ---
    gap_min=5, gap_max=6,
    # --- 拐点过滤 (或关系) ---
    ma20_lo=-10.0, ma20_hi=-5.0,
    depth_max=-30.0,
    yin_ratio_max=0.5,
    # --- 信号质量排除 ---
    yin_ratio_exclude=0.6,
    rsi6_exclude_lt=30.0,
    d0_ma20_exclude_lt=-8.0,
    # --- 龙强度门槛 (2026-09-10 特征判别: 600交易日334笔, 前/后两段稳定性+阈值敏感性通过;
    #     亏损源画像 = streak<=2伪龙 61笔27.9%/-2.4% + 前期热度不足 + 回落过深) ---
    min_streak=3,        # 锚定涨停日连板高度>=3 ("龙"的最低成色)
    lu_gain20_min=60.0,  # 涨停日20日涨幅>=60% (前期热度; >=100更好但样本锐减)
    rsi6_min=45.0,       # D0 RSI6>=45 (强势回调; 与 rsi6_exclude_lt=30 叠加后实际下界=45)
    # 三条件合计实测 (09-10): 600d 334→167笔 胜率48.8→51.5% 均收-0.13→+1.09 均峰7.68→9.0
    #   盈亏比1.00→1.39 (两段同向); 300d 117→50笔 50.4→62.0% 均收+0.07→+2.63。
    #   注意: 实跑优于/异于"离线过滤投影"属预期 —— 规则作用在涨停日候选上, 会重锚定
    #   (break@首个通过条件的 lu), 而非简单删旧笔; 回归口径以实跑为准。
    # --- 入场 ---
    # (2026-09-07 用户裁定: 移除 D1 gap 范围过滤 [-3,+2] — 信号本身已筛选,
    #  高开/低开由用户自行判断, 展示更多股票; 旧引擎口径 114笔/74.6% 已废弃 —
    #  2026-09-09 出场引擎现实化(T+1/跳空/跌停)后 116笔/50.9%/+0.21%)
    # --- 出场 ---
    # (2026-09-10 晚 C2 采纳, 用户批准: 出场参数扫描 tmp/龙回头出场参数扫描报告.md —
    #  固定 50 笔入场真引擎重放, 自校验 50/50 逐笔一致; trail_lo -8→-3 (原-8与stop_loss
    #  重合=前段形同虚设) + peak_exit_ret 7→4; 两段同向 (seg1/seg2 均不退化), C6≡C2 非孤点;
    #  +2.63→+2.97/笔 盈亏比1.37→1.58, 改善全来自盈亏比; hold_days/stop_loss 扫描无效故不动。
    #  ⚠️ 实盘执行口径依赖: trail_lo=-3 盘中触发更频繁, "收盘逃顶 vs 追踪线优先"差异被放大
    #  (回测对照 +2.97 vs +2.29), 实盘须人工尾盘盯盘并逐笔记 execution_mode)
    hold_days=7,
    stop_loss=-8.0,
    trail_lo=-3.0,
    trail_hi=-3.0,
    trail_switch_pct=3.0,
    peak_exit_ret=4.0,
    peak_exit_upper=30.0,
)


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
from app.market_cn.auto.common.exec_cn import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
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

    wave_start (波次窗口口径, 2026-09-10 用户裁定): "第一条规则(找龙)"通过日的 bar
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


def _dragon_sample_feats(bars, i, code, stock_info=None, wave_start=None):
    """框架通用特征/标签 + 本策略调试标签 (仅 probe 调用; 判定路径不读)。"""
    base = _probe_sample_feats(bars, i, code, stock_info=stock_info)
    labels = base.get("labels") or {}
    if labels.get("entry_d1o"):
        labels.update(_dragon_debug_labels(bars, i, labels["entry_d1o"], wave_start))
    base["labels"] = labels
    return base


def run_backtest_dragon_callback(bars, entry_idx, entry_price, hold_days=None,
                                 stop_loss=None, board_type="main", stop_at_idx=None, **params):
    """龙回头出场模拟: 分段追踪止损 (as-of安全, 供回测与盘中持仓重放共用)。

    出场判定顺序 (每日, d>=2): 1)峰值逃顶 2)分段追踪+固定止损(合并, 先触发者成交)
    3)到期/stop_at_idx截断。
    现实约束见上方修正注释 (T+1 / 跳空按开盘 / 跌停顺延)。
    stop_at_idx: 只模拟到该bar索引(盘中重放用); 未触发出场 → open=True。
    跌停顺延成交价=次日开盘, 仅当次日 bar 存在且不超过 stop_at_idx (重放 as-of 安全)。
    """
    p = {**DRAGON_CB_PARAMS, **(params or {})}
    hold_days = p["hold_days"] if hold_days is None else hold_days
    stop_loss = p["stop_loss"] if stop_loss is None else stop_loss
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    n = len(bars)
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ""
    capped = False
    pending_dn = False        # 触发成交价触及跌停 → 次日开盘强平
    last_unfilled = False     # 最后一日为一字跌停(整日无法卖出) → 到期顺延

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= n:
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
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

        # T+1: 买入当日(d=1)不可卖出, 仅记录估值
        if d > 1:
            # 1. 峰值逃顶 (收盘判定收盘卖)
            ret = (b["close"] / entry_price - 1) * 100
            if ret > p["peak_exit_ret"]:
                rng = b["high"] - b["low"]
                upper = (b["high"] - max(b["open"], b["close"])) / rng * 100 if rng > 0 else 0
                if upper > p["peak_exit_upper"] and b["close"] < b["high"] * 0.98:
                    exit_p, exit_d, exit_reason = b["close"], d, "峰值逃顶"
                    break

            # 2/3. 分段追踪 + 固定止损 (合并: 价格连续, 先穿过更高触发线)
            peak_ret = (peak / entry_price - 1) * 100
            trail = p["trail_hi"] if peak_ret >= p["trail_switch_pct"] else p["trail_lo"]
            trig_t = peak * (1 + trail / 100)
            trig_s = entry_price * (1 + stop_loss / 100)
            trig = max(trig_t, trig_s)
            if b["low"] <= trig:
                # 跳空穿越: 开盘已低于触发价 → 只能按开盘价成交
                fill = fill_on_gap(b["open"], trig)
                reason = f"追踪止损{trail}%" if trig_t >= trig_s else f"止损{stop_loss}%"
                if fill_blocked_by_limit_dn(fill, dn):
                    pending_dn = True   # 成交价触及跌停 → 卖不出
                    continue
                exit_p, exit_d, exit_reason = fill, d, reason
                break

        exit_p, exit_d = b["close"], d

    if exit_reason == "" and not capped:
        # 末日落入无法卖出状态 (一字跌停 / 触发成交触及跌停) → 顺延至下一可交易日
        # 开盘强平; 连续一字跌停逐日跳过。注意 nxt 必须指向"未成交日的下一日":
        # exit_d 是最后标记估值日(1-based), 未成交日 = exit_d+1, 顺延日 = exit_d+2。
        nxt = entry_idx + exit_d + 1
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
            exit_p, exit_d, exit_reason = nb["open"], nxt - entry_idx + 1, "跌停顺延开盘"
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
    "gap_from_peak", "streak_h", "lu_gain20", "d0_vs_ma20", "pullback_depth", "yin_ratio",
    "tech_score", "tech_rsi", "tech_roc", "tech_psy",
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
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DRAGON_CB_PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近; 引擎/回测钩子经 getattr 读取)
    PROBE_STAGE_RANK = {"dragon": 1, "gap": 2, "streak": 3, "lu_gain20": 4, "rsi": 5,
                        "turn": 6, "quality": 7, "dedup": 8, "prefilter": 9,
                        "engine_skip": 9, "signal": 10}

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     use_tech_score=True, probe=None, **params):
        """龙回头 D0 信号 ("方案2") → Signal (至多1笔)。

        as_of=k: 只用 bars[:k+1] 判定; limit_ups: 预计算涨停索引 (回测优化, None 则现算)。
        **params 覆盖 DRAGON_CB_PARAMS 键 (dragon_scan 传 params=dict)。
        probe: 调试探针 (probe.Probe / 同签名 shim), None=零开销 — TRACE 式记录
        每候选各判定步落点 (debug 形态, 数据存档供 AI 分析, 与判定行为无关)。
        """
        p = self.merged_params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < 3:
            return result
        i = n - 1
        if i < 2:
            return result
        board_type = get_board_type(code)

        d0 = bars[i]
        prev_c = bars[i - 1]["close"]
        if prev_c <= 0:
            return result
        last_chg = (d0["close"] / prev_c - 1) * 100
        prev_vol = bars[i - 1]["volume"]
        entry_vol_r = d0["volume"] / prev_vol if prev_vol > 0 else 0

        closes = [bars[j]["close"] for j in range(i + 1)]

        # ── tech_score 加分制 (仅参考输出; RSI 值供质量排除使用) ──
        score = 0
        rsi_val = roc = psy = None
        if use_tech_score:
            dif, dea, hist = calc_macd(closes)
            if hist is not None and len(hist) >= 2:
                if is_macd_golden_cross(dif, dea, lookback=5):
                    score += 3
                elif is_macd_hist_turning_positive(hist, lookback=5):
                    score += 2
                elif is_macd_hist_shrinking_negative(hist, lookback=5):
                    score += 1
                n_h = len(hist)
                if n_h >= 2 and abs(dif[n_h - 1]) < abs(dea[n_h - 1]) * 0.5:
                    score += 1
                if dif[n_h - 1] < dea[n_h - 1] and dif[n_h - 2] >= dea[n_h - 2]:
                    score -= 2
            rsi_val = rsi(closes, period=6)
            if rsi_val is not None:
                if rsi_val < 30:
                    score += 2
                elif rsi_val < 40:
                    score += 1
                elif rsi_val < 60:
                    score -= 1
                else:
                    score -= 2
            roc = calc_roc(closes, period=5)
            if roc is not None:
                if -10 <= roc < 0 or 0 <= roc < 5:
                    score += 1
                elif roc < -15 or roc >= 5:
                    score -= 1
            psy = calc_psy(closes, period=10)
            if psy is not None:
                if psy < 30:
                    score += 2
                elif psy < 40:
                    score += 1
                elif psy >= 50:
                    score -= 1

        # ── 方案2 主判定 ──
        for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
            lu_close = bars[lu_idx]["close"]
            if lu_close <= 0:
                continue

            # 当前日(i)收盘必须仍低于涨停收盘 (仍在回调中)
            if bars[i]["close"] >= lu_close:
                continue

            pullback_days = i - lu_idx
            gap_from_peak = pullback_days   # 同一值 (Step2 与探针记录共用旧字段名)

            # ── 龙强度度量 (①连板高度 ②前期热度; 全部只用<=D0收盘数据, as-of 安全) ──
            # 置于 Step1 之前: 各判定门与探针 trace 共用 (纯计算, 判定行为不变)
            streak_h = 1
            _j = lu_idx
            while _j > 0 and is_limit_up(bars[_j]["close"], bars[_j - 1]["close"], board_type):
                streak_h += 1
                _j -= 1
            if lu_idx >= 20:
                _base = bars[lu_idx - 20]["close"]
                lu_gain20 = (lu_close / _base - 1) * 100 if _base > 0 else None
            else:
                lu_gain20 = None

            # 探针 shim (TRACE 宏语义): probe=None 时 _tr=None, 判定内零开销
            if probe is not None:
                def _tr(stage, **kw):
                    probe.trace(stage, code=code, d0_date=str(bars[i]["time"])[:10],
                                lu_date=str(bars[lu_idx]["time"])[:10],
                                gap_from_peak=gap_from_peak, streak_h=streak_h,
                                lu_gain20=round(lu_gain20, 1) if lu_gain20 is not None else None,
                                **kw)
            else:
                _tr = None

            # ── Step1: 找龙 — 滑动窗口内涨停占比>=70% ──
            dragon_found = False
            for window in p["dragon_windows"]:
                start = max(1, lu_idx - window)
                total_days = lu_idx - start
                if total_days < 3:
                    continue
                lu_count = sum(1 for k in range(start, lu_idx)
                               if k > 0 and is_limit_up(bars[k]["close"], bars[k - 1]["close"], board_type))
                if lu_count / total_days >= p["dragon_ratio"]:
                    dragon_found = True
                    break
            if not dragon_found:
                if _tr:
                    _tr("dragon")
                continue

            # ── Step2: gap [gap_min, gap_max] ──
            if gap_from_peak < p["gap_min"] or gap_from_peak > p["gap_max"]:
                if _tr:
                    _tr("gap")
                continue

            # ── 龙强度门槛 (2026-09-10 三条件) ──
            if streak_h < p["min_streak"]:
                if _tr:
                    _tr("streak")
                continue
            if lu_gain20 is None or lu_gain20 < p["lu_gain20_min"]:
                if _tr:
                    _tr("lu_gain20")
                continue
            # ③ 强势回调: D0 RSI6 下界 (use_tech_score=False 时 rsi 未计算, 放行不误杀)
            if rsi_val is not None and rsi_val < p["rsi6_min"]:
                if _tr:
                    _tr("rsi")
                continue

            # ── 回调期特征 ──
            if i >= 19:
                ma20 = sum(bars[j]["close"] for j in range(i - 19, i + 1)) / 20
                d0_vs_ma20 = (d0["close"] / ma20 - 1) * 100 if ma20 > 0 else None
            else:
                d0_vs_ma20 = None

            min_low = min(bars[j]["low"] for j in range(lu_idx + 1, i + 1))
            pullback_depth = (min_low / lu_close - 1) * 100

            pb_yin = sum(1 for j in range(lu_idx + 1, i + 1) if bars[j]["close"] < bars[j]["open"])
            pb_total = i - lu_idx
            yin_ratio = pb_yin / pb_total if pb_total > 0 else 1.0

            # ── 拐点过滤 (或关系) ──
            cond_ma20 = d0_vs_ma20 is not None and p["ma20_lo"] <= d0_vs_ma20 < p["ma20_hi"]
            cond_depth = pullback_depth <= p["depth_max"]
            cond_yin = yin_ratio < p["yin_ratio_max"]
            if not (cond_ma20 or cond_depth or cond_yin):
                if _tr:
                    _tr("turn", d0_vs_ma20=round(d0_vs_ma20, 2) if d0_vs_ma20 is not None else None,
                        pullback_depth=round(pullback_depth, 2),
                        yin_ratio=round(yin_ratio, 2))
                continue

            # ── 信号质量排除 ──
            if yin_ratio >= p["yin_ratio_exclude"]:
                if _tr:
                    _tr("quality", reason="yin_ratio", yin_ratio=round(yin_ratio, 2))
                continue
            if rsi_val is not None and rsi_val < p["rsi6_exclude_lt"]:
                if _tr:
                    _tr("quality", reason="rsi6_lt", rsi6=round(rsi_val, 1))
                continue
            if d0_vs_ma20 is not None and d0_vs_ma20 < p["d0_ma20_exclude_lt"]:
                if _tr:
                    _tr("quality", reason="d0_ma20_lt", d0_vs_ma20=round(d0_vs_ma20, 2))
                continue

            if _tr:
                _tr("signal")
            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=0,   # 历史口径: 方案2无评分体系, 库内 score 恒0 (与旧 dragon_scan 后处理一致)
                price=round(d0["close"], 3),
                label="龙回头",
                extra={
                    "board": get_board_name(code),
                    "lu_date": bars[lu_idx]["time"],
                    "pullback_days": pullback_days,
                    "signal_chg": round(last_chg, 2),
                    "signal_vol_r": round(entry_vol_r, 2),
                    "signal_price": round(d0["close"], 3),
                    "entry_vol_r": round(entry_vol_r, 2),
                    "buy_mode": "next_open",
                    "gap_from_peak": gap_from_peak,
                    "streak_h": streak_h,
                    "lu_gain20": round(lu_gain20, 1) if lu_gain20 is not None else None,
                    "d0_vs_ma20": round(d0_vs_ma20, 2) if d0_vs_ma20 is not None else None,
                    "pullback_depth": round(pullback_depth, 2),
                    "yin_ratio": round(yin_ratio, 2),
                    "tech_score": score,
                    "tech_rsi": round(rsi_val, 1) if rsi_val else None,
                    "tech_roc": round(roc, 1) if roc else None,
                    "tech_psy": round(psy, 1) if psy else None,
                },
            ))
            break
        return result

    # ---- D1 竞价处置 ----
    def entry_decision(self, row, snap=None, **params):
        """D1 开盘一律可买 (2026-09-07 移除 gap∈[-3,+2] 范围过滤)。

        gap 仅记录在 reason 供用户参考, 高开/低开由用户自行取舍。"""
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = (open_px / prev_close - 1) * 100
        tag = "高开" if gap > 2 else ("低开" if gap < -3 else "")
        return EntryDecision(True, f"gap={gap:.2f}% 可买{tag}")

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

    def quality_key(self, row):
        """方案2 质量排序: tech_score(参考) -> 涨停日换手率 (技术分无判别力, 主要按换手热度)。"""
        extra = row.get("extra") or {}
        return (extra.get("tech_score") or 0, extra.get("turnover_anchor") or 0)

    def initial_stop(self, code, entry_price):
        """-8%, 板块不分档 (与回测一致)。"""
        return round(entry_price * (1 + DRAGON_CB_PARAMS["stop_loss"] / 100), 3)

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
        if r and not r.get("open"):
            exit_idx = entry_idx + r["exit_day"] - 1
            if exit_idx == today_idx and r.get("exit_reason"):
                return ExitDecision("exit", reason=r["exit_reason"], price=float(r["exit_price"]))
        return ExitDecision("hold")

    # ---- 回测钩子 (2026-09-10 自 backtest.backtest_dragon_stock 逐字搬入, 对数零差异) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                       probe=None):
        """单股龙回头全历史回测, 返回 trades 列表 (字段与基线 JSON 对齐)。

        编排 (枚举/去重±4/预过滤锚点/预筛) 是策略规则故归位本插件; 出场模拟
        run_backtest_dragon_callback 在本文件 (策略专用出场规则)。
        probe: 调试探针 (None=零开销) — 每个到达完整判定的决策日产出一行
        sample (特征+标签+当日最深判定阶段), 廉价预筛跳过的日不采样 (纯噪声)。
        """
        from app.market_cn.auto.common.filters import unified_prefilter
        board_type = get_board_type(code)
        n = len(bars)
        if n < 5:
            return []
        lu_all = find_limit_ups(bars, board_type)
        # 廉价预筛参数: 与 scan_signals 实际用的默认参数同源 (回测不走 config 覆盖,
        # 与旧 facade 调用路径一致); 取 self.default_params 而非 merged_params。
        gap_min = self.default_params["gap_min"]
        gap_max = self.default_params["gap_max"]
        trades = []
        used_ranges = []
        # 波次起点 (波次窗口口径, 2026-09-10 用户裁定): 最近一次"第一条规则(找龙)未通过"
        # 的次日 = 本波行情起点; 供探针标签用 (判定路径不读)。廉价预筛跳过日与
        # stage=dragon/no_candidate 都算"找龙未通过" → 波次断点。
        wave_start = 0

        for i in range(2, n - 1):
            # 廉价预筛 (数学必要条件超集, 非加规则 — 行为零差异): scan_signals 必过
            # Step2 — 存在涨停日 lu: gap∈[gap_min,gap_max] 且 D0收盘仍低于涨停收盘;
            # 不满足则该日不可能出信号, 跳过昂贵的逐日全量判定 (closes 复制 +
            # MACD/RSI/ROC/PSY, 426s→48s 的根因修复)。若回测覆盖 gap 参数须同步此处。
            d0c = bars[i]["close"]
            if not any(gap_min <= i - j <= gap_max and d0c < bars[j]["close"]
                       for j in lu_all):
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
                                                        stock_info=stock_info, wave_start=wave_start))
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
                                                        stock_info=stock_info, wave_start=wave_start))
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
                                                            stock_info=stock_info, wave_start=wave_start))
                    continue

            # 入场: 次日(D+1)开盘价
            d0 = bars[i]
            d1 = bars[i + 1]
            d1_gap = (d1["open"] / d0["close"] - 1) * 100 if d0["close"] > 0 else 0
            entry_price = d1["open"]
            if entry_price <= 0:
                continue

            result = run_backtest_dragon_callback(
                bars, i + 1, entry_price, hold_days=7, stop_loss=-8.0,
                board_type=board_type)
            if not result:
                if probe is not None:
                    probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                                 stage="engine_skip", rule_trace=day_tr.items,
                                 sig=sig, **_dragon_sample_feats(bars, i, code,
                                                                 stock_info=stock_info, wave_start=wave_start))
                continue

            if probe is not None:
                probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                             stage="signal", rule_trace=day_tr.items, sig=sig,
                             engine={k: result.get(k) for k in
                                     ("return_pct", "peak_return_pct",
                                      "exit_reason", "exit_day")},
                             **_dragon_sample_feats(bars, i, code,
                                                    stock_info=stock_info, wave_start=wave_start))

            trades.append({
                **sig,
                "entry_date": d1["time"],
                "entry_price": round(entry_price, 3),
                "buy_mode": "next_open",
                "d1_gap": round(d1_gap, 2),
                **result,
            })

        return trades


def _find_bar_idx(bars, date_str):
    """日期串 → bars 索引; 未找到返回 None (回测钩子用, 原 backtest 内联助手)。"""
    for i, b in enumerate(bars):
        if b["time"] == date_str:
            return i
    return None
