"""strategies/dragon_v2.py — 龙回头V2 (2026-09-16 完全独立版, StrategyBase 插件)

定位: 龙回头框架的"下影线承接"变体, 与 dragon_callback 并行实盘 (config 各占名额)。

独立原因 (2026-09-16 用户裁定):
  V2 初版 (09-10) 以继承 DragonCallbackStrategy + C4 后置门形式上线; 09-16 父类
  入场链调参 (gap[5,6]→[5,7]、D0企稳、质量门关闭) 后, V2 候选池随之漂移、全部
  历史口径失效。本版起 **不 import / 不继承 dragon_callback**: 入场全链、参数、
  出场引擎、三决策、回测钩子全部自有快照, 两策略规则独立演化、互不波及。
  本文件与 dragon_callback.py 的同构代码是"有意的分叉快照", 不是重复债 — 改一边
  不应自动改另一边 (这正是独立的目的)。只共享框架公共件: common/* 与 strategies/base。

入场规则 (全部只用 <=D0 收盘数据, as-of 安全):
  找龙(滑动窗口涨停占比>=70%) → 回调 gap[5,7] → 龙强度(连板>=4 + 20日涨幅>=60
  + RSI6>=45) → 拐点OR(深跌释放<=-30% | 阴线占比<50%; MA20支撑腿已停用)
  → 质量(D0跌幅>-4%企稳; 其余排除门已参数关闭)
  → **V2 独有: D0 下影线门** (low < min(open,close), 盘中触支撑后收回)
  → U1~U4(@涨停日) → D1开盘买 (无 gap 过滤)。
  出场: 分段追踪(-8/-3) + 固定止损-8 + 峰值逃顶4% + 到期7天 (与父类同口径快照)。

V2 门的数据依据 (2026-09-16, 均为 600d 全市场真实回测):
  - 下影门 (tmp/dragon_vol_ablation.py + dragon_vol_pair.py):
    父类 167笔/53.3%/+1.63/总271.9/PL1.62/最差-12.98
    → 仅加下影门: 154笔/55.2%/+2.21/总339.8/PL1.91/两段51/60/最差-11.01;
    砍掉的是无下影阴线票 (该形态全集 41.9%/-0.57/总-17.6), 尾部风险同步收窄。
  - 连板门槛 3→4 (tmp/dragon_v2_streak_ma.py):
    3连板组27笔 40.7%/+0.08/总+2.2pp/rpd0.040 (近零死区); 提至4板后
    129笔/58.9%/+2.70/总347.7/PL1.89/rpd1.322 (总贡献+7.9, 日收益+21.7%, 尾部不变)。
  - 被否决的门:
    回调缩量≤0.7: 边际拦的是 55.9%/+2.40/PL1.97 优质票, 单变量分桶是相关性陷阱;
    D0量比[0.3,1.5]: >2.0 灾区已被其他门提前拦掉, 无独立增益;
    MA5>10>20 或 MA10>20>60 替代连板: 200/181笔/50.5%/53.0%, 均线拦不住伪龙, 全面恶化。

易错点:
  - 下影门是**后置拒绝语义**: 父类链选定"首个全通过 lu 候选"后再判, 拒绝=当日
    无信号 (break, 不再试其他 lu) —— 与 09-16 实验 super()+后置过滤口径逐笔一致;
    若误写成 continue 会放行其他候选, 发生重锚分叉, 154 笔基线失效。
  - backtest_stock 廉价预筛读自己的 gap_min/gap_max; 改 gap 两处必须同步。
  - U1~U4 锚定涨停日 (@D0 评估换手会误杀 — D0 是缩量小阴日)。
  - 找龙窗口 start=max(1, lu_idx-window), total_days<3 跳过 — 边界勿动。
  - exit 重放 stop_at_idx: idx>stop_at_idx 即截断 open=True (盘中重放当天未收盘)。
  - tech_score 仅输出参考, 不参与过滤。
"""
from __future__ import annotations

from app.market_cn.auto.core.exec import (
    fill_blocked_by_limit_dn,
    fill_on_gap,
    is_one_word_limit_dn,
    limit_dn_price as _limit_dn_price,
)
from app.market_cn.auto.core.indicators import (
    calc_macd, calc_psy, calc_roc, is_macd_golden_cross,
    is_macd_hist_shrinking_negative, is_macd_hist_turning_positive, rsi,
)
from app.market_cn.auto.core.market import find_limit_ups, get_board_name, get_board_type, is_limit_up
from app.market_cn.auto.probe import DayTrace as _DayTrace, \
    sample_feats as _probe_sample_feats   # 探针框架件 (无环; 只提供通用特征/标签)
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "dragon_v2"
STRATEGY_LABEL = "龙回头V2"

# ================================================================
# 参数 (2026-09-16 自有快照 — 与 dragon_callback.DRAGON_CB_PARAMS 解耦,
# 父类今后调参不影响本策略; 各阈值来历见 dragon_callback.py 同日注释)
# ================================================================
DRAGON_V2_PARAMS = dict(
    # --- 找龙: 滑动窗口涨停占比 ---
    dragon_ratio=0.7,
    dragon_windows=[4, 5, 7, 10, 15, 20],
    # --- 回调窗口 (快照父类 09-16 gap[5,7] 甜点; 改此值须同步 backtest_stock 预筛) ---
    gap_min=5, gap_max=7,
    # --- 拐点过滤 (或关系) ---
    ma20_lo=-10.0, ma20_hi=-10.0,   # MA20 支撑腿关闭: hi=lo 区间空集 (候选零触发)
    depth_max=-30.0,
    yin_ratio_max=0.5,
    # --- 信号质量排除 (哨兵参数关闭, 判定代码保留; 改回原值即恢复) ---
    yin_ratio_exclude=1.01,         # 阴线>=0.6 排除 (1.01=数学关闭; 砍的是赚钱票)
    rsi6_exclude_lt=-100.0,         # RSI6<30 排除 (rsi6_min=45 下零触发)
    d0_ma20_exclude_lt=-100.0,      # 距MA20<-8% 排除 (候选全站 MA20 上方, 零触发)
    d0_chg_min=-4.0,                # D0 企稳门: 跌幅<=-4% 是落刀非回调
    # --- 龙强度门槛 ---
    #   3连板组(27笔) 600d 归因: 40.7%胜/均收+0.08/总贡献仅+2.2pp/rpd0.040 (近零)
    #   提升至4板后: 129笔/58.9%/+2.70/总347.7/PL1.89/rpd1.322 (vs 3板 154/55.2/2.21/339.8/1.91/1.086)
    #   总贡献不降反升(+7.9), 日收益+21.7%, 尾部不变(-11.01)。连板=真龙强度, 3板含伪龙死区。
    min_streak=4,
    lu_gain20_min=60.0,
    rsi6_min=45.0,
    # --- V2 独有门 (2026-09-16) ---
    require_d0_shadow=True,         # D0 必须有下影线 low<min(open,close):
                                    #   154笔/55.2%/+2.21/PL1.91 vs 父类 167/53.3/+1.63/1.62;
                                    #   False=关闭 (退化为父类全链独立快照)
    # --- 出场 (快照父类 09-10 现实化口径; ⚠️ trail_lo=-3 盘中触发频繁, 实盘需人工尾盘盯盘) ---
    hold_days=7,
    stop_loss=-8.0,
    trail_lo=-3.0,
    trail_hi=-3.0,
    trail_switch_pct=3.0,
    peak_exit_ret=4.0,
    peak_exit_upper=30.0,
)


# ================================================================
# 调试通道 (仅 probe 非 None 时计算; 判定/实盘路径绝不读取, 改本段不影响交易)
# 与 dragon_callback 同构快照; 归档键名保持稳定 (离线工具按键名读取)。
# ================================================================
DEBUG_HOLD_DAYS = 7
DEBUG_TRAILS = (4, 6, 8, 12)
DEBUG_MAX_HOLD = 10
DEBUG_WAVE_DAYS = 20


def _fixed_hold_labels(bars, i, entry, days=DEBUG_HOLD_DAYS):
    """固定持有 days 日标签: 第 days 日收盘无条件卖出 (排除出场引擎差异)。"""
    n = len(bars)
    out = {}
    if not entry or entry <= 0 or i + 1 >= n:
        return out
    last = min(i + days, n - 1)
    highs = [float(bars[k]["high"]) for k in range(i + 1, last + 1)]
    lows = [float(bars[k]["low"]) for k in range(i + 1, last + 1)]
    if highs:
        out[f"peak{days}"] = round((max(highs) / entry - 1) * 100, 2)
        out["peak_day"] = int(highs.index(max(highs)) + 1)
        out[f"mae{days}"] = round((min(lows) / entry - 1) * 100, 2)
    if i + days < n:
        out[f"ret_d{days}c"] = round((float(bars[i + days]["close"]) / entry - 1) * 100, 2)
    return out


def _trail_exit_labels(bars, i, entry, trail_pct, max_days=DEBUG_MAX_HOLD,
                       wave_start=None, wave_days=DEBUG_WAVE_DAYS):
    """峰值回撤出场标签 (路径依赖, 衡量可捕获空间; 口径同 dragon_callback)。"""
    n = len(bars)
    sf = f"{trail_pct:g}"
    out = {}
    if not entry or entry <= 0 or i + 1 >= n:
        return out
    wnd_end = (int(wave_start) + int(wave_days) - 1) if wave_start is not None \
        else i + max_days
    if i + 1 > wnd_end:
        out[f"rsn_tr{sf}"] = "late"
        return out
    last = min(wnd_end, n - 1)
    complete = wnd_end <= n - 1
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
    """波次视角标签: 整波涨幅 + 入场推后天数。"""
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
    out["entry_lag"] = int(i) - ws
    return out


def _v2_debug_labels(bars, i, entry, wave_start=None):
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


def _v2_sample_feats(bars, i, code, stock_info=None, wave_start=None):
    """框架通用特征/标签 + 本策略调试标签 (仅 probe 调用; 判定路径不读)。"""
    base = _probe_sample_feats(bars, i, code, stock_info=stock_info)
    labels = base.get("labels") or {}
    if labels.get("entry_d1o"):
        labels.update(_v2_debug_labels(bars, i, labels["entry_d1o"], wave_start))
    base["labels"] = labels
    return base


# ================================================================
# 出场模拟 (与 dragon_callback.run_backtest_dragon_callback 同构快照,
# 现实化口径: T+1 当日不可卖 / 跳空穿价按开盘成交 / 跌停顺延次日开盘)
# ================================================================
def run_backtest_dragon_v2(bars, entry_idx, entry_price, hold_days=None,
                           stop_loss=None, board_type="main", stop_at_idx=None, **params):
    """V2 出场模拟: 分段追踪止损 (as-of 安全, 回测与盘中持仓重放共用)。

    出场判定顺序 (每日, d>=2): 1)峰值逃顶 2)分段追踪+固定止损(合并, 先触发者成交)
    3)到期/stop_at_idx截断。stop_at_idx: 只模拟到该 bar 索引(盘中重放); 未触发→open=True。
    """
    p = {**DRAGON_V2_PARAMS, **(params or {})}
    hold_days = p["hold_days"] if hold_days is None else hold_days
    stop_loss = p["stop_loss"] if stop_loss is None else stop_loss
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    n = len(bars)
    peak = entry_price
    exit_p, exit_d, exit_reason = entry_price, 0, ""
    capped = False
    pending_dn = False        # 触发成交价触及跌停 → 次日开盘强平
    last_unfilled = False     # 最后一日一字跌停 → 到期顺延

    for d in range(1, hold_days + 1):
        idx = entry_idx + d - 1
        if idx >= n:
            break
        if stop_at_idx is not None and idx > stop_at_idx:
            capped = True
            break
        b = bars[idx]
        peak_prev = peak
        if b["high"] > peak:
            peak = b["high"]
        prev_close = bars[idx - 1]["close"] if idx > 0 else 0
        dn = _limit_dn_price(prev_close, board_type) if prev_close > 0 else None

        # 跌停顺延: 前一交易日无法卖出 → 今日开盘强平
        if pending_dn:
            exit_p, exit_d, exit_reason = b["open"], d, "跌停顺延开盘"
            break

        # 一字跌停: 全天无成交可能, 持仓顺延
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
            # 开盘时已存在的止损线 (未被当日 high 抬高); 只有跌破它才按开盘价成交 (fill_on_gap 误用修正, 同源 dragon_callback)
            trig_prev = max(peak_prev * (1 + trail / 100), trig_s)
            if b["low"] <= trig:
                fill = b["open"] if b["open"] <= trig_prev else trig   # 跳空穿越按开盘成交
                reason = f"追踪止损{trail}%" if trig_t >= trig_s else f"止损{stop_loss}%"
                if fill_blocked_by_limit_dn(fill, dn):
                    pending_dn = True
                    continue
                exit_p, exit_d, exit_reason = fill, d, reason
                break

        exit_p, exit_d = b["close"], d

    if exit_reason == "" and not capped:
        # 末日无法卖出 (一字跌停/触及跌停) → 顺延至下一可交易日开盘强平
        nxt = entry_idx + exit_d + 1
        while (last_unfilled or pending_dn) and nxt < n \
                and (stop_at_idx is None or nxt <= stop_at_idx):
            nb = bars[nxt]
            pc = bars[nxt - 1]["close"]
            dn2 = _limit_dn_price(pc, board_type) if pc > 0 else None
            if dn2 is not None and nb["low"] == nb["high"] \
                    and abs(nb["low"] - dn2) <= dn2 * 0.002:
                last_unfilled, pending_dn = True, False
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
# 输出字段 (trades JSON / store extra 白名单对齐 dragon_callback; path 用 V2)
# ================================================================
_LEGACY_FIELDS = (
    "code", "board", "path", "path_label", "lu_date", "pullback_days", "signal_date",
    "signal_chg", "signal_vol_r", "signal_price", "entry_vol_r", "buy_mode",
    "gap_from_peak", "streak_h", "lu_gain20", "d0_vs_ma20", "pullback_depth", "yin_ratio",
    "tech_score", "tech_rsi", "tech_roc", "tech_psy",
)


def _signal_to_legacy_dict(sig: Signal, code: str) -> dict:
    """Signal → trades dict 形态 (path 归一为 dragon_v2)。"""
    ex = sig.extra or {}
    return {k: ex.get(k) for k in _LEGACY_FIELDS if k not in ("code", "board", "path", "path_label", "signal_date")} | {
        "code": code,
        "board": ex.get("board"),
        "path": STRATEGY_KEY,
        "path_label": STRATEGY_LABEL,
        "signal_date": sig.time,
    }


def _find_bar_idx(bars, date_str):
    """日期串 → bars 索引; 未找到返回 None。"""
    for i, b in enumerate(bars):
        if b["time"] == date_str:
            return i
    return None


# ================================================================
# StrategyBase 插件实现
# ================================================================
@register
class DragonV2Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "limit_up"     # U1~U4 锚定涨停日 (D0 缩量小阴日评估换手会误杀)
    scan_spec = ScanSpec(kind="daily_close")
    default_params = dict(DRAGON_V2_PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近; shadow 与 d0_chg/quality 同属 D0 质量段)
    PROBE_STAGE_RANK = {"dragon": 1, "gap": 2, "streak": 3, "lu_gain20": 4, "rsi": 5,
                        "turn": 6, "d0_chg": 7, "quality": 7, "shadow": 7,
                        "dedup": 8, "prefilter": 9, "engine_skip": 9, "signal": 10}

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, limit_ups=None,
                     use_tech_score=True, probe=None, **params):
        """V2 D0 信号 → list[Signal] (至多1笔)。

        与 dragon_callback.scan_signals 同构快照 + 末尾下影线门; as_of=k 只用
        bars[:k+1]; limit_ups=预计算涨停索引 (回测优化); probe=None 零开销。
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

        # ── tech_score 加分制 (仅参考输出; RSI 值供龙强度/质量门使用) ──
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

        # ── 主判定: 枚举涨停日候选 (正序, 首个全通过者出信号) ──
        for lu_idx in (limit_ups if limit_ups is not None else find_limit_ups(bars[:i], board_type)):
            lu_close = bars[lu_idx]["close"]
            if lu_close <= 0:
                continue

            # 当前日(i)收盘必须仍低于涨停收盘 (仍在回调中)
            if bars[i]["close"] >= lu_close:
                continue

            pullback_days = i - lu_idx
            gap_from_peak = pullback_days

            # ── 龙强度度量 (连板高度 + 前期热度) ──
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

            if probe is not None:
                def _tr(stage, **kw):
                    probe.trace(stage, code=code, d0_date=str(bars[i]["time"])[:10],
                                lu_date=str(bars[lu_idx]["time"])[:10],
                                gap_from_peak=gap_from_peak, streak_h=streak_h,
                                lu_gain20=round(lu_gain20, 1) if lu_gain20 is not None else None,
                                **kw)
            else:
                _tr = None

            # ── Step1: 找龙 — 滑动窗口内涨停占比>=dragon_ratio ──
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

            # ── 龙强度门槛 ──
            if streak_h < p["min_streak"]:
                if _tr:
                    _tr("streak")
                continue
            if lu_gain20 is None or lu_gain20 < p["lu_gain20_min"]:
                if _tr:
                    _tr("lu_gain20")
                continue
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
            if last_chg <= p["d0_chg_min"]:
                if _tr:
                    _tr("d0_chg", signal_chg=round(last_chg, 2))
                continue
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

            # ── V2 独有: D0 下影线门 (2026-09-16, 600d 154笔/55.2%/PL1.91) ──
            # 下影长度 = 实体下沿 - 最低价; <=0 即无下影 (开盘或收盘即最低, 单边阴跌
            # 无承接)。**后置拒绝语义**: 此时已选定首个全通过候选, 拒绝必须 break
            # (当日无信号), 不能 continue 去试其他 lu —— 否则与实验口径分叉 (重锚)。
            has_shadow = min(float(d0["open"]), float(d0["close"])) - float(d0["low"]) > 0
            if p["require_d0_shadow"] and not has_shadow:
                if _tr:
                    _tr("shadow", has_shadow=False,
                        d0_low=float(d0["low"]),
                        body_floor=min(float(d0["open"]), float(d0["close"])))
                break

            if _tr:
                _tr("signal")
            result.append(Signal(
                code=code,
                time=bars[i]["time"],
                score=0,   # 与父类同口径: 无评分体系, 库内 score 恒 0
                price=round(d0["close"], 3),
                label=STRATEGY_LABEL,
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
        """D1 开盘一律可买 (gap 仅标注高开/低开供人工取舍, 不做范围过滤)。"""
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
        """无确认步骤: 买入日收盘直接持仓 (出场由收盘重放判定)。返回 None=快照缺失。"""
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
        """质量排序: tech_score(参考) -> 涨停日换手率。"""
        extra = row.get("extra") or {}
        return (extra.get("tech_score") or 0, extra.get("turnover_anchor") or 0)

    def initial_stop(self, code, entry_price):
        """-8%, 板块不分档 (与回测一致)。"""
        return round(entry_price * (1 + DRAGON_V2_PARAMS["stop_loss"] / 100), 3)

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘重放: 复用 run_backtest_dragon_v2 (stop_at_idx=今日截断); live→hold。"""
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        board = get_board_type(row.get("code", ""))
        today_idx = len(bars) - 1
        r = run_backtest_dragon_v2(bars, entry_idx, entry_price, board_type=board,
                                   stop_at_idx=today_idx)
        if r and not r.get("open"):
            exit_idx = entry_idx + r["exit_day"] - 1
            if exit_idx == today_idx and r.get("exit_reason"):
                return ExitDecision("exit", reason=r["exit_reason"], price=float(r["exit_price"]))
        return ExitDecision("hold")

    # ---- 回测钩子 (与 dragon_callback.backtest_stock 同构快照) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                       probe=None):
        """单股 V2 全历史回测 → trades 列表 (字段与基线 JSON 对齐)。

        枚举/去重±4/预过滤锚点/廉价预筛均为本策略规则; 出场模拟用本文件引擎。
        probe: 每个完整判定日产一行 sample (廉价预筛跳过日不采样)。
        """
        from app.market_cn.auto.core.filters import unified_prefilter
        board_type = get_board_type(code)
        n = len(bars)
        if n < 5:
            return []
        lu_all = find_limit_ups(bars, board_type)
        # 廉价预筛参数与 scan_signals 默认参数同源 (取 default_params, 回测不走 config 覆盖)
        gap_min = self.default_params["gap_min"]
        gap_max = self.default_params["gap_max"]
        trades = []
        used_ranges = []
        # 波次起点: 最近一次"找龙未通过"的次日 (供探针标签用, 判定路径不读)
        wave_start = 0

        for i in range(2, n - 1):
            # 廉价预筛 (数学必要条件超集): 存在 lu 使 gap∈[gap_min,gap_max] 且 D0 收盘<涨停收盘
            d0c = bars[i]["close"]
            if not any(gap_min <= i - j <= gap_max and d0c < bars[j]["close"]
                       for j in lu_all):
                wave_start = i + 1
                continue

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
                                 **_v2_sample_feats(bars, i, code,
                                                    stock_info=stock_info, wave_start=wave_start))
                    if stage in ("dragon", "no_candidate"):
                        wave_start = i + 1
                continue
            sig = sigs[0]
            lu_idx = _find_bar_idx(bars, sig["lu_date"])

            # 去重 (±4天内跳过; 去重在预过滤之前)
            skip = False
            for (s, e) in used_ranges:
                if abs(i - s) <= 4 or abs(i - e) <= 4:
                    skip = True
                    break
            if skip:
                if probe is not None:
                    probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                                 stage="dedup", rule_trace=day_tr.items, sig=sig,
                                 **_v2_sample_feats(bars, i, code,
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
                                     **_v2_sample_feats(bars, i, code,
                                                        stock_info=stock_info, wave_start=wave_start))
                    continue

            # 入场: 次日(D+1)开盘价
            d0 = bars[i]
            d1 = bars[i + 1]
            d1_gap = (d1["open"] / d0["close"] - 1) * 100 if d0["close"] > 0 else 0
            entry_price = d1["open"]
            if entry_price <= 0:
                continue

            result = run_backtest_dragon_v2(
                bars, i + 1, entry_price, hold_days=7, stop_loss=-8.0,
                board_type=board_type)
            if not result:
                if probe is not None:
                    probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                                 stage="engine_skip", rule_trace=day_tr.items,
                                 sig=sig, **_v2_sample_feats(bars, i, code,
                                                             stock_info=stock_info, wave_start=wave_start))
                continue

            if probe is not None:
                probe.sample(code=code, d0_date=str(bars[i]["time"])[:10],
                             stage="signal", rule_trace=day_tr.items, sig=sig,
                             engine={k: result.get(k) for k in
                                     ("return_pct", "peak_return_pct",
                                      "exit_reason", "exit_day")},
                             **_v2_sample_feats(bars, i, code,
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
