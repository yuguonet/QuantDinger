# -*- coding: utf-8 -*-
"""intraday_core.py — 盘中实时买卖分析核心算法 (纯函数, 零 IO)

数据源约定:
  盘中: realtime_snapshot_YYYY (每行≈1分钟bar, volume 为当日累计量 → 先差分)
  盘后: kline_1m_YYYY (每根1分钟K线, volume 为当分钟量)
统一为分钟序列: [{'mi': 分钟序号0~239, 'o','h','l','c','v'(当分钟量),'cv'(累计量),'ts': 时间串}]

核心经验 → 算法映射 (2026-09-04 用户提供):
  1 VWAP 是弱转强的关键:
      价格持续在 VWAP 上方并拉动 VWAP 向上            → strong_above (强)
      偶尔小幅跌破(深度≤0.05%量级)快速站回且VWAP持平/向上 → reclaim_strong (强)
      向上突破 VWAP 后持续在 VWAP 上方附近              → weak_to_strong (典型弱转强)
  2 警示: 现价远离 VWAP 上方(未涨停) → 可能回调 warn_far_above;
          现价低于 VWAP 过远 → 反弹强度弱 weak_far_below
  3 预估成交量 = 今日累计量 / 已过时间比例, 与近3-5日同期累计均量对比 → 实时量比
    (今日量 vs 昨日同期量 → 实时量动量)
  4 持续缩量平稳上涨 = 上方压力小(多头); 持续放量下跌 = 真跌(空头)
  5 MACD 红绿柱与涨跌幅的比值 → 日内强弱
  6 尾盘小幅下跌 vs 小幅上涨 → 次日上涨概率 (回测统计项, 非规则)

as-of 语义: 所有事件只用 ≤ 当前分钟的数据, 回测与实盘同一套函数。
"""
from __future__ import annotations

INTRADAY_PARAMS = dict(
    session_minutes=240,          # 交易日分钟数 (09:30-11:30, 13:00-15:00)
    vwap_dip_max_pct=0.05,        # "偶尔跌破"幅度阈值 (相对现价 %)
    vwap_reclaim_minutes=15,      # 跌破后快速站回的时间窗 (分钟)
    vwap_slope_window=20,         # VWAP 斜率观察窗 (分钟)
    above_vwap_min_minutes=30,    # 突破/站上 VWAP 持续时长 → 事件确认 (分钟)
    near_vwap_pct=0.3,            # "VWAP 上方附近" 的偏离带宽 (%)
    far_above_pct=3.0,            # 现价高于 VWAP 过远警示 (%)
    far_below_pct=2.0,            # 现价低于 VWAP 过远 (%)
    vol_ratio_days=5,             # 同期均量参考天数 (3-5)
    vol_ratio_high=1.5,           # 实时量比高 (相对近N日同期)
    vol_ratio_low=0.7,            # 实时量比低
    shrink_rise_minutes=30,       # 缩量平稳上涨观察窗 (分钟)
    shrink_rise_min_gain=1.0,     # 窗口内最小涨幅 (%)
    shrink_vol_ratio_max=0.8,     # 窗口内量比上限 (相对近N日同期均量)
    heavy_down_minutes=15,        # 放量下跌观察窗 (分钟)
    heavy_down_min_loss=1.5,      # 窗口内最小跌幅 (%)
    heavy_down_vol_ratio_min=1.5, # 窗口内量比下限
    macd_fast=12, macd_slow=26, macd_signal=9,
    macd_strength_min_hist=0.05,  # MACD 柱强度最低绝对值 (元)
    tail_minutes=15,              # 尾盘窗口 (分钟, 用户修正: 收盘前 15 分钟)
    tail_move_pct=1.0,            # 尾盘"小幅"涨跌阈值 (%)
    pre_tail_lo_mi=199,           # 14:20 分钟序号 (异动窗口起点)
    pre_tail_hi_mi=229,           # 14:50 分钟序号 (异动窗口终点)
    pre_tail_move_pct=2.0,        # 14:20~14:50 异动阈值 (|涨跌幅|≥此值视为有异动)
    limit_up_warn_pct=9.0,        # 接近涨停则不触发"远离VWAP"警示 (主板),
    # strong_above 平滑版条件 (价格与 VWAP 同步上行、无强烈震荡)
    smooth_dev_std_max=0.5,       # 窗口内 VWAP 偏离标准差上限 (%)
    smooth_max_abs_dev=1.0,       # 窗口内最大绝对偏离上限 (%)
    smooth_min_corr=0.8,          # 价格与 VWAP 相关性下限
    smooth_price_min_gain=0.5,    # 窗口内价格最小涨幅 (%)
)


# ================================================================
# 序列标准化
# ================================================================

def _trading_minute_index(ts: str) -> int:
    """时间串 'YYYY-MM-DD HH:MM' → 0~239 (09:30起)。非法返回 -1。"""
    try:
        hm = ts.split(" ")[1][:5] if " " in ts else ts[-5:]
        h, m = int(hm[:2]), int(hm[3:5])
        minutes = h * 60 + m
        if minutes <= 570:      # <=09:30
            return 0
        if 570 < minutes <= 690:    # 09:31-11:30
            return minutes - 571
        if 690 < minutes <= 780:    # 11:31-13:00 午休 → 归 11:30
            return 119
        if 780 < minutes <= 900:    # 13:01-15:00
            return 120 + (minutes - 781)
        return 239
    except Exception:
        return -1


def prep_minutes(rows, volume_cumulative=False):
    """原始行 → 标准分钟序列。rows: [{time, open, high, low, close, volume}]

    volume_cumulative=True 表示 volume 是当日累计量 (snapshot), 先差分。
    输出按 mi 升序去重 (保留每分钟最后一根)。
    """
    tmp = {}
    for r in rows:
        ts = str(r.get("time", ""))
        mi = _trading_minute_index(ts)
        if mi < 0:
            continue
        v = float(r.get("volume") or 0)
        # close 字段兼容: 1m K线用 close, 快照表用 last
        c = float(r.get("close") or r.get("last") or 0)
        tmp[mi] = {"mi": mi, "ts": ts, "o": float(r.get("open") or 0),
                   "h": float(r.get("high") or 0), "l": float(r.get("low") or 0),
                   "c": c, "v": v}
    out = [tmp[k] for k in sorted(tmp)]
    if volume_cumulative:
        prev = 0.0
        for b in out:
            b["v"] = max(0.0, b["v"] - prev)
            prev = b["v"] + prev
            b["cv"] = prev
    else:
        cv = 0.0
        for b in out:
            cv += b["v"]
            b["cv"] = cv
    out = [b for b in out if b["c"] > 0]
    return out


# ================================================================
# VWAP 与状态
# ================================================================

def vwap_series(mins):
    """逐分钟 VWAP (典型价加权) + 偏离 + 斜率。返回与 mins 等长的状态列表。"""
    out = []
    cum_pv = 0.0
    cum_v = 0.0
    vwap_hist = []
    p = INTRADAY_PARAMS
    for b in mins:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        cum_pv += tp * b["v"]
        cum_v += b["v"]
        vwap = cum_pv / cum_v if cum_v > 0 else b["c"]
        vwap_hist.append(vwap)
        w = p["vwap_slope_window"]
        look = vwap_hist[-w:]
        slope = (look[-1] - look[0]) / look[0] * 100 if len(look) >= 2 and look[0] > 0 else 0.0
        dev = (b["c"] / vwap - 1) * 100 if vwap > 0 else 0.0
        out.append({"mi": b["mi"], "vwap": vwap, "dev": dev,
                    "above": b["c"] >= vwap, "slope": slope})
    return out


# ================================================================
# MACD (分钟级)
# ================================================================

def macd_hist_series(mins, fast=None, slow=None, signal=None):
    p = INTRADAY_PARAMS
    fast = fast or p["macd_fast"]
    slow = slow or p["macd_slow"]
    signal = signal or p["macd_signal"]
    closes = [b["c"] for b in mins]
    n = len(closes)
    if n < slow + signal:
        return [0.0] * n
    ef = closes[0]
    es = closes[0]
    dea = 0.0
    hist = []
    k_f, k_s, k_sig = 2 / (fast + 1), 2 / (slow + 1), 2 / (signal + 1)
    for c in closes:
        ef = c * k_f + ef * (1 - k_f)
        es = c * k_s + es * (1 - k_s)
        dif = ef - es
        dea = dif * k_sig + dea * (1 - k_sig)
        hist.append(2 * (dif - dea))
    return hist


# ================================================================
# 同期均量曲线 (近 N 日) 与预估量
# ================================================================

def prev_day_curves(prev_days_mins, max_days=None):
    """近 N 日 {mi: 平均累计量} 曲线。prev_days_mins: [标准分钟序列...] (不含今日)。"""
    p = INTRADAY_PARAMS
    n = max_days or p["vol_ratio_days"]
    use = prev_days_mins[-n:]
    acc = {}
    for mins in use:
        for b in mins:
            acc[b["mi"]] = acc.get(b["mi"], 0.0) + b["cv"]
    return {mi: v / len(use) for mi, v in acc.items()} if use else {}


def volume_estimate(mins, now_idx, curve):
    """返回 dict: est_day_vol(今日预估全天量), ratio_vs_curve(实时量比), cum(今日累计)。

    est = 累计量 / 已过时间比例; ratio = 今日累计量 / 近N日同期累计均量。
    """
    b = mins[now_idx]
    cum = b["cv"]
    elapsed = (now_idx + 1) / INTRADAY_PARAMS["session_minutes"]
    est = cum / elapsed if elapsed > 0 else 0.0
    ref = curve.get(b["mi"], 0.0)
    ratio = (cum / ref) if ref > 0 else None
    return {"mi": b["mi"], "cum": cum, "est_day_vol": est,
            "ratio_vs_curve": ratio, "curve_ref": ref}


# ================================================================
# 事件检测 (逐分钟 as-of)
# ================================================================

def detect_events(mins, curve=None, limit_up_pct=None):
    """逐分钟扫描, 返回事件列表: [{mi, ts, type, note}]。每类事件持续期间只报首次。"""
    p = INTRADAY_PARAMS
    evts = []
    if not mins:
        return evts
    vs = vwap_series(mins)
    hist = macd_hist_series(mins)
    n = len(mins)

    above_run = 0
    was_below = False
    dip = None
    fired = set()
    win = p["shrink_rise_minutes"]
    hdw = p["heavy_down_minutes"]

    for i in range(n):
        b = mins[i]
        st = vs[i]
        mi = b["mi"]
        px = b["c"]
        if st["above"]:
            above_run += 1
        else:
            above_run = 0
        if not st["above"]:
            if dip is None:
                dip = (mi, abs(st["dev"]))
            else:
                dip = (mi, max(dip[1], abs(st["dev"])))
        if dip is not None and st["above"] and st["slope"] >= 0:
            depth_ok = dip[1] <= p["vwap_dip_max_pct"]
            quick = (mi - dip[0]) <= p["vwap_reclaim_minutes"]
            if depth_ok and quick and "reclaim_strong" not in fired:
                fired.add("reclaim_strong")
                evts.append({"mi": mi, "ts": b["ts"], "type": "reclaim_strong",
                             "note": f"跌破{dip[1]:.2f}%后{mi - dip[0]}分钟内站回,VWAP斜率{st['slope']:+.2f}%"})
            dip = None
        if above_run >= p["above_vwap_min_minutes"] and st["slope"] > 0 \
                and "strong_above" not in fired:
            fired.add("strong_above")
            evts.append({"mi": mi, "ts": b["ts"], "type": "strong_above",
                         "note": f"持续{above_run}分钟在VWAP上方且VWAP向上{st['slope']:+.2f}%"})
        # strong_above_smooth: 平稳版 — 价格与 VWAP 同步上行、偏离小且稳定、无强烈震荡
        if above_run >= p["above_vwap_min_minutes"] and st["slope"] > 0 \
                and "strong_above_smooth" not in fired:
            w = above_run
            seg_px = [mins[i - w + 1 + k]["c"] for k in range(w)]
            seg_vw = [vs[i - w + 1 + k]["vwap"] for k in range(w)]
            price_gain = (seg_px[-1] / seg_px[0] - 1) * 100 if seg_px[0] > 0 else 0
            devs = [abs((seg_px[k] / seg_vw[k] - 1) * 100) if seg_vw[k] > 0 else 9.0 for k in range(w)]
            dev_std = _pstdev(devs)
            max_dev = max(devs)
            corr = _corr(seg_px, seg_vw)
            if price_gain >= p["smooth_price_min_gain"] and dev_std <= p["smooth_dev_std_max"] \
                    and max_dev <= p["smooth_max_abs_dev"] and corr is not None \
                    and corr >= p["smooth_min_corr"]:
                fired.add("strong_above_smooth")
                evts.append({"mi": mi, "ts": b["ts"], "type": "strong_above_smooth",
                             "note": f"价格与VWAP同步上行{price_gain:+.2f}%(偏离σ{dev_std:.2f}%,相关{corr:.2f}), 平稳强"})
        if was_below and above_run == p["above_vwap_min_minutes"] \
                and abs(st["dev"]) <= p["near_vwap_pct"] and st["slope"] >= 0 \
                and "weak_to_strong" not in fired:
            fired.add("weak_to_strong")
            evts.append({"mi": mi, "ts": b["ts"], "type": "weak_to_strong",
                         "note": f"突破VWAP后{above_run}分钟站稳(偏离{st['dev']:+.2f}%)，典型弱转强"})
        if not st["above"]:
            was_below = True
        near_limit = limit_up_pct is not None and \
            (b["c"] / (mins[0]["o"] or b["c"]) - 1) * 100 >= p["limit_up_warn_pct"]
        if st["dev"] >= p["far_above_pct"] and not near_limit \
                and "warn_far_above" not in fired:
            fired.add("warn_far_above")
            evts.append({"mi": mi, "ts": b["ts"], "type": "warn_far_above",
                         "note": f"现价高于VWAP {st['dev']:+.2f}%, 警惕回调"})
        if st["dev"] <= -p["far_below_pct"] and "weak_far_below" not in fired:
            fired.add("weak_far_below")
            evts.append({"mi": mi, "ts": b["ts"], "type": "weak_far_below",
                         "note": f"现价低于VWAP {st['dev']:+.2f}%, 反弹强度预期偏弱"})

        if curve:
            ve = volume_estimate(mins, i, curve)
            ratio = ve["ratio_vs_curve"]
            if ratio is not None:
                if i >= win:
                    base = mins[i - win]["c"]
                    gain = (px / base - 1) * 100 if base > 0 else 0
                    seg_ratio = [volume_estimate(mins, j, curve)["ratio_vs_curve"]
                                 for j in range(i - win + 1, i + 1)]
                    seg_ratio = [x for x in seg_ratio if x is not None]
                    if gain >= p["shrink_rise_min_gain"] and seg_ratio \
                            and max(seg_ratio) <= p["shrink_vol_ratio_max"] \
                            and "shrink_rise" not in fired:
                        fired.add("shrink_rise")
                        evts.append({"mi": mi, "ts": b["ts"], "type": "shrink_rise",
                                     "note": f"{win}分钟涨{gain:+.2f}%且量比≤{p['shrink_vol_ratio_max']}，上方压力小"})
                if i >= hdw:
                    base = mins[i - hdw]["c"]
                    loss = (px / base - 1) * 100 if base > 0 else 0
                    seg_ratio = [volume_estimate(mins, j, curve)["ratio_vs_curve"]
                                 for j in range(i - hdw + 1, i + 1)]
                    seg_ratio = [x for x in seg_ratio if x is not None]
                    if loss <= -p["heavy_down_min_loss"] and seg_ratio \
                            and min(seg_ratio) >= p["heavy_down_vol_ratio_min"] \
                            and "heavy_volume_down" not in fired:
                        fired.add("heavy_volume_down")
                        evts.append({"mi": mi, "ts": b["ts"], "type": "heavy_volume_down",
                                     "note": f"{hdw}分钟跌{loss:+.2f}%且量比≥{p['heavy_down_vol_ratio_min']}，真跌"})
                if ratio >= p["vol_ratio_high"] and "vol_ratio_high" not in fired:
                    fired.add("vol_ratio_high")
                    evts.append({"mi": mi, "ts": b["ts"], "type": "vol_ratio_high",
                                 "note": f"实时量比{ratio:.2f}x(近{p['vol_ratio_days']}日同期), 预估全天量{ve['est_day_vol']/1e6:.1f}M"})
                if ratio <= p["vol_ratio_low"] and "vol_ratio_low" not in fired:
                    fired.add("vol_ratio_low")
                    evts.append({"mi": mi, "ts": b["ts"], "type": "vol_ratio_low",
                                 "note": f"实时量比{ratio:.2f}x(近{p['vol_ratio_days']}日同期), 预估全天量{ve['est_day_vol']/1e6:.1f}M"})

        if i >= 1:
            h = hist[i]
            h_prev = hist[i - 1]
            day_chg = (px / mins[0]["o"] - 1) * 100 if mins[0]["o"] > 0 else 0
            same_dir = (h > 0 and day_chg > 0) or (h < 0 and day_chg < 0)
            strengthening = abs(h) > abs(h_prev)
            if same_dir and strengthening and abs(h) >= p["macd_strength_min_hist"] \
                    and "macd_strength" not in fired:
                fired.add("macd_strength")
                evts.append({"mi": mi, "ts": b["ts"], "type": "macd_strength",
                             "note": f"MACD柱{'红' if h > 0 else '绿'}{h:.3f}与涨跌幅({day_chg:+.2f}%)同向增强, 日内{'偏强' if h > 0 else '偏弱'}"})

        if mi >= 240 - p["tail_minutes"] and "tail_small_down" not in fired \
                and i >= 1 and i >= 240 - p["tail_minutes"]:
            tail_base = mins[240 - p["tail_minutes"] - 1]["c"] if len(mins) > 240 - p["tail_minutes"] else mins[0]["c"]
            tail_chg = (px / tail_base - 1) * 100 if tail_base > 0 else 0
            if -p["tail_move_pct"] <= tail_chg < 0:
                fired.add("tail_small_down")
                evts.append({"mi": mi, "ts": b["ts"], "type": "tail_small_down",
                             "note": f"尾盘{p['tail_minutes']}分钟小跌{tail_chg:+.2f}% (统计项)"})
    return evts


def _pstdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / n) ** 0.5


def _corr(xs, ys):
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    vx = sum((x - mx) ** 2 for x in xs[:n]) ** 0.5
    vy = sum((y - my) ** 2 for y in ys[:n]) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def collect_dip_episodes(mins):
    """跌破 VWAP episode 收集 (供阈值扫描)。
    返回: [{dip_mi, depth(最大跌破深度%), reclaimed(bool), reclaim_mi, minutes_to_reclaim}]
    episode = 连续位于 VWAP 下方的一段; 站回即闭合, 未站回丢弃。"""
    vs = vwap_series(mins)
    episodes = []
    cur = None
    for b, st in zip(mins, vs):
        if not st["above"]:
            if cur is None:
                cur = {"dip_mi": b["mi"], "depth": abs(st["dev"])}
            else:
                cur["depth"] = max(cur["depth"], abs(st["dev"]))
        else:
            if cur is not None:
                cur["reclaimed"] = True
                cur["reclaim_mi"] = b["mi"]
                cur["minutes_to_reclaim"] = b["mi"] - cur["dip_mi"]
                episodes.append(cur)
                cur = None
    return episodes


def macd_channel_stats(mins, upto_idx=None):
    """当日分腿统计: 上升腿中 MACD柱上升量/价格涨幅 vs 下降腿中 MACD柱下降量/价格跌幅。

    用户经验: 向上时涨得多、下跌时跌得少 = 强 (当日维度)。
    score = ratio_up/(ratio_up+ratio_dn)*100, 越高越强。
    """
    hist = macd_hist_series(mins)
    n = len(mins) if upto_idx is None else min(upto_idx + 1, len(mins))
    up_px = up_ind = dn_px = dn_ind = 0.0
    for i in range(1, n):
        dpx = mins[i]["c"] - mins[i - 1]["c"]
        dh = hist[i] - hist[i - 1]
        if dpx > 0:
            up_px += dpx
            up_ind += max(0.0, dh)
        elif dpx < 0:
            dn_px += abs(dpx)
            dn_ind += max(0.0, -dh)
    ratio_up = up_ind / up_px if up_px > 0 else None
    ratio_dn = dn_ind / dn_px if dn_px > 0 else None
    score = None
    if ratio_up is not None and ratio_dn is not None and (ratio_up + ratio_dn) > 0:
        score = round(ratio_up / (ratio_up + ratio_dn) * 100, 1)
    return {"up_px": round(up_px, 4), "up_ind": round(up_ind, 4),
            "dn_px": round(dn_px, 4), "dn_ind": round(dn_ind, 4),
            "ratio_up": ratio_up, "ratio_dn": ratio_dn, "score": score}
# ================================================================

def analyze_live(today_mins, prev_days_mins=None, prev_close=None, limit_up_pct=None):
    """实盘快照/1m 数据 → 当前盘中状态 dict (供 monitor/前端使用)。"""
    curve = prev_day_curves(prev_days_mins or [])
    evts = detect_events(today_mins, curve=curve, limit_up_pct=limit_up_pct)
    vs = vwap_series(today_mins)
    st = vs[-1] if vs else {"vwap": None, "dev": None, "slope": None, "above": False}
    above_run = 0
    for s in reversed(vs):
        if s["above"]:
            above_run += 1
        else:
            break
    ve = volume_estimate(today_mins, len(today_mins) - 1, curve) if today_mins else {}
    strength = "中性"
    types = {e["type"] for e in evts}
    if {"strong_above", "reclaim_strong", "weak_to_strong", "shrink_rise"} & types:
        strength = "强"
    if {"warn_far_above", "weak_far_below", "heavy_volume_down"} & types:
        strength = "弱"
    return {"vwap": st["vwap"], "dev": st["dev"], "slope": st["slope"],
            "above_run": above_run, "est_day_vol": ve.get("est_day_vol"),
            "ratio_vs_curve": ve.get("ratio_vs_curve"),
            "events": evts, "strength": strength,
            "prev_close": prev_close}
