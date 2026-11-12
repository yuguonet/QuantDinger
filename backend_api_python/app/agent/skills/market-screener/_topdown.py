# -*- coding: utf-8 -*-
"""盘中三层漏斗选人层（自顶向下）：大盘闸 → 板块闸 → 板块内个股 → 截断。

与既有自底向上链路（多源捞票 → 硬过滤 → 连板排序）并列，不替代：
`run(strategy="topdown")` 走这里，`intraday` / `post_market` 保持原样，便于回测对照。

═══════════════════════════════════════════════════════════════
  L1 大盘闸   market_gate()
    同刻量能 vs 前 N 日 + 多指数分组最差同刻涨跌 → strong / weak / shrink / neutral
    「放大上涨=强，放大下跌=弱」。**弱市不空仓**，只收紧口袋（提高个股准入门槛）。
    指数口径 `QD_TOPDOWN_L1_IDX_GROUPS`（默认 沪指|深指|创业板,科创50 —— 双创收拢
    一组，组内/组间皆取最差，只收紧不放松）。`idx_norm` 为归一化展示口径
    （创科 20% 板 ÷2 换算主板 10% 口径），仅展示不进判定：判定只读最差值符号，
    正系数缩放不改变 min 符号，归一化对 verdict 恒等（trae 2026-11-06）。

  L2 板块闸   pick_boards()
    行业轨 + 概念轨各取 per_track 个，按 net_pct（净额÷成交额）降序。
    net_pct 的分母随交易时长累积、同日内会被系统性稀释（实测半导体 30.85% → -0.84%），
    但它是**相对强度**口径，回测优于绝对净额：三同口径对照（10-28~10-30、旧 streak、
    仅换 L2 排序键）net_pct 深亏 9.2% / T+3 -0.69%，net 为 18.1% / -2.68% ——
    净额会把大板块（半导体 300 亿级）顶上来，其成分票已涨幅透支，追高即深亏；
    且属性标签（证金持股等）会混入。`QD_TOPDOWN_L2_SORT=net` 可切换对照。
    不用 net 绝对值：成分多的大板块会永远排前面。

  L3 个股     pick_stocks_in_board()
    · 板块有涨停 → 涨停统计（数量/时间/封板额/成交额/梯队）+ 跟随判定。
      跟随判定**只作板块健康闸**：无跟随/反向跟随 = 资金流入但盘口无反应，
      整个板块不出票；但**跟随票本身不出**——F 组 uniq+K3 实测其 T+3 胜率
      20.3%（跳变 32.0%）、中位 -4.50%（跳变 -3.23%），负期望分支。
    · 板块无涨停 → 跳变检测（1 分钟内涨幅与成交额同时突增），谁先跳谁优先。
      该分支另加**同刻量比准入闸**（今日累计量 ÷ 前 5 日同刻均值 ≥
      `QD_TOPDOWN_JUMP_MIN_VR`，默认 2.0，0=关闭）：板块无涨停意味着没有板块级
      资金共识，只能靠个股自身是否真有资金在做；实测 10-29~11-02 剔除约 20% 候选，
      深亏 5→1 笔、负收益累计 -176→-70、T+3 胜 69.8%→75.2%（累计收益 430→396）。
      闸在 per_board 截断之前生效，被剔除的名额由后续跳变票回填。
    出票只有两支：涨停票（按 zt_time 先后）与跳变票（按 t_first 先后）。
    · **早盘吸筹准入闸**（`_early_accumulation_gate`，仅 as_of ≤ 09:40）：
      跳变票须满足「前 5 日累计净流入 > 流通市值 1%（明显吸筹）」且「前 5 日
      累计涨幅 < 10%（未过度拉升）」，否则放弃；涨停票不拦。生产实测：阈值
      0→1.0 后 09:40 触板率 11.8%→26.3%、T3 均 +1.079%→+1.931%；09:35 深亏
      归零但样本降到日均 1.2 只。09:45 实测深亏反而 3.2%→5.6%（被拦组深亏 0%
      纯误伤），故截止收到 09:40。流通市值「中位更优」不成立（20~80 亿反而
      最差），故不设市值条件（trae 2026-11-06）。

  截断        板块内 per_board（默认 10）：涨停分支里涨停票按触板时间全取，
    非涨停的跳变票只留最早 `QD_TOPDOWN_ZT_JUMP_MAX`（默认 3）只 —— 旧版把
    剩余名额全给越来越晚的跳变票，尾盘「涨完才报」的垃圾票会顶满名额
    （trae 2026-11-06）。全局再按「首次跳变/触板时间先后 → 重复板块数多者优先」
    排序取 limit 只送 deep；涨停票不沉底，与跳变票用同一时间轴排序
    （涨停票的 t_first = 触板时刻）。同一只票被多个板块同时选中时只保留一次
    （其余记 also_in，dup_n = 被选中板块数，重复越多 = 板块资金共识越强）。
    `QD_TOPDOWN_DUP_MIN` >1 时先按 dup_n 硬筛再排序（默认 1 = 不筛）；
    实测 G2 六天 dup_n≥2 的 T+1 为 +4.10%（dup_n=1 为 -2.25%）且 4/4 天为正，
    但只有 19 笔、开启后出票数减少约 85%，默认关闭。
═══════════════════════════════════════════════════════════════

口径纪律（与 screen_query 一致，不得自行改）：
  · 资金流一律是**本地量价方向净额**（Σ 相邻拍 last 变化 × 该拍增量额），
    不是东财四档主力，两者不可换算。
  · snapshot 是 60s/票，最小时间颗粒 1 分钟；跨票同一时刻有数十秒采集偏差。
  · 今日的「盘口反应」只能来自 snapshot（kline_1m 盘中当日为空）。

易错点：
  · 板块合成序列必须**剔除涨停票**再做基准 —— 封死不动的票会把中位数压平；
    实测半导体 157 只里 16 只涨停，含涨停时中位涨幅被顶到 10%，剔除后才是
    真实的「板块其余票在什么位置」。
  · 跳变检测要用**累计量差分**（snapshot.volume 是当日累计量），不是直接取 volume。
  · 涨停票的 zt_time 是字符串 "HH:MM:SS"，与 snapshot 的 "HH:MM" 不同精度，
    比较前统一截到分钟。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from ...log import logger
from app.data_sources import screen_query as _sq

_DEFAULT_DAY_TZ = "Asia/Shanghai"


def _env_f(key: str, dflt: float) -> float:
    try:
        return float(os.getenv("QD_TOPDOWN_" + key, "") or dflt)
    except (TypeError, ValueError):
        return dflt


def _env_i(key: str, dflt: int) -> int:
    try:
        return int(os.getenv("QD_TOPDOWN_" + key, "") or dflt)
    except (TypeError, ValueError):
        return dflt


_L1_LOOKBACK = _env_i("L1_LOOKBACK", 5)
_L1_VR_STRONG = _env_f("L1_VR_STRONG", 1.1)
_L1_VR_WEAK = _env_f("L1_VR_WEAK", 0.9)
# L1 多指数分组：`|` 分组、`,` 组内。科创50 与创业板同属成长赛道、走势高相关，
# 收拢为一组，避免被当成两张独立证据票。组内取最差、组间取最差 —— 只收紧不放松。
# 选股池以深市小票/双创为主，单看沪指（大盘蓝筹）口径错位：10-27 早盘沪指
# +0.59/+0.80 判 strong，科创50 已 -0.11/-0.56，该批出票胜率 ≤25%（trae 2026-11-06）。
_L1_IDX_GROUPS: List[List[str]] = [
    [s.strip() for s in grp.split(",") if s.strip()]
    for grp in (os.getenv("QD_TOPDOWN_L1_IDX_GROUPS",
                          "000001.SH|399001.SZ|399006.SZ,000688.SH") or "").split("|")
    if grp.strip()
]


def _parse_idx_scale() -> Dict[str, float]:
    """QD_TOPDOWN_L1_IDX_SCALE="399006.SZ:0.5,000688.SH:0.5" → {指数: 归一化系数}。

    创科 20% 涨跌停板的天然波动约为 10% 板的 2 倍，×0.5 换算到主板口径便于
    盘后横向比较「温度」。⚠ 仅用于 idx_norm 展示，不进 verdict：判定只读
    「最差值符号」，正系数缩放不改变 min 的符号（2026-11-06 已证）。
    """
    out: Dict[str, float] = {}
    for item in (os.getenv("QD_TOPDOWN_L1_IDX_SCALE",
                           "399006.SZ:0.5,000688.SH:0.5") or "").split(","):
        sym, _, k = item.partition(":")
        try:
            if sym.strip() and k.strip():
                out[sym.strip()] = float(k)
        except ValueError:
            continue
    return out


_L1_IDX_SCALE = _parse_idx_scale()
_L2_BOARDS_STRONG = _env_i("L2_BOARDS_STRONG", 6)
_L2_BOARDS_NEUTRAL = _env_i("L2_BOARDS_NEUTRAL", 4)
_L2_BOARDS_WEAK = _env_i("L2_BOARDS_WEAK", 3)
_L3_LIMIT = _env_i("L3_LIMIT", 25)
_L3_PER_BOARD = _env_i("L3_PER_BOARD", 10)
_FOLLOW_WINDOW_MIN = _env_i("FOLLOW_WINDOW_MIN", 10)
_MIN_BOARD_FUND_PCT = _env_f("MIN_BOARD_FUND_PCT", 0.0)
_INDUSTRY_MIN_MEMBERS = _env_i("INDUSTRY_MIN_MEMBERS", 10)
_L2_FAMILY_JACCARD = _env_f("L2_FAMILY_JACCARD", 0.5)
_STREAK_SURGE_ENABLE = _env_i("STREAK_SURGE_ENABLE", 1)
_STREAK_SURGE_MIN_STREAK = _env_i("STREAK_SURGE_MIN_STREAK", 2)
_STREAK_SURGE_MIN_VR = _env_f("STREAK_SURGE_MIN_VR", 1.5)
_STREAK_SURGE_MIN_RET = _env_f("STREAK_SURGE_MIN_RET", 0.0)
_STREAK_SURGE_VR_DAYS = _env_i("STREAK_SURGE_VR_DAYS", 5)
_STREAK_SURGE_MIN_HIST = _env_i("STREAK_SURGE_MIN_HIST", 2)
_STREAK_SURGE_LOOKBACK = _env_i("STREAK_SURGE_LOOKBACK", 12)
_STREAK_SURGE_ZT_TOL = _env_f("STREAK_SURGE_ZT_TOL", 0.8)
_MIN_CIRC_MV_YI = _env_f("MIN_CIRC_MV", 15.0)
_JUMP_WITH_ZT = _env_i("JUMP_WITH_ZT", 1)
# 有涨停板块里「非涨停（跳变）票」的精细截断（trae 2026-11-06，v2 修正）。
#
# ★ v1→v2 迭代：
#   v1（先2 + 跳2-3 + 后2，CAP_FIRST=2/IDX_MIN=4/CAP_SEC=2）假设 idx 2-3 是质量
#   谷值（12 天样本 T3均=-0.0087），但扩展到 13 天后 idx 2-3 变成 +0.00016
#   中性（强市里 +2.1%），跳过反而裁掉好票。更糟的是：裁掉的 idx 2-3 早票位置
#   被 pure jump 板块的 idx 7+ 尾盘垃圾（T3均=-2.678%）在全局截断里填补，
#   导致 NEW 最终 T3均=-0.147%（反而差于 OLD 的 -0.068%）。
#
# ★ v2 修正：全取好段 idx 0-6（T3均 +0.295~+0.606%），只裁 idx 7+ 尾盘垃圾
#   （T3均 -2.678%）。13 天回测验证：
#     PERF（CAP_FIRST=6/IDX_MIN=7/CAP_SEC=2）T3均=+0.125%（翻正！）
#     vs ZERO（关闭三段式）-0.068%，vs 旧 v1 NEW -0.147%
#
# 三段参数（只在 limit_up+jump 分支生效；pure jump 分支只过 per_board cap=10）：
#   _ZT_JUMP_CAP_FIRST  idx 0..CAP_FIRST-1 全取（默认 6，覆盖好段 0-5）
#   _ZT_JUMP_IDX_MIN    接力段起始 idx（默认 7，跳过尾盘垃圾）
#   _ZT_JUMP_FUND_THR   接力段 fund_circ_ratio 下限（默认 -1，关闭质量门）
#   _ZT_JUMP_CAP_SEC    接力段 cap（默认 2）
#   CAP_FIRST < 0       关闭整个非涨停票截断（回到 per_board cap 控制）
_ZT_JUMP_CAP_FIRST = _env_i("ZT_JUMP_CAP_FIRST", 6)
_ZT_JUMP_IDX_MIN = _env_i("ZT_JUMP_IDX_MIN", 7)
_ZT_JUMP_FUND_THR = _env_f("ZT_JUMP_FUND_THR", -1.0)
_ZT_JUMP_CAP_SEC = _env_i("ZT_JUMP_CAP_SEC", 2)
_JUMP_CONFIRM_BARS = _env_i("JUMP_CONFIRM_BARS", 3)
_JUMP_STREAK_RET = _env_f("JUMP_STREAK_RET", 2.0)
_JUMP_MIN_VR = _env_f("JUMP_MIN_VR", 2.0)
_DUP_MIN = _env_i("DUP_MIN", 1)
_L2_SORT = (os.getenv("QD_TOPDOWN_L2_SORT", "net_pct") or "net_pct").strip().lower()
_L2_MIN_NET_YI = _env_f("L2_MIN_NET_YI", 0.0)
# ── 早盘「庄家吸筹」准入闸（trae 2026-11-06）──
# ★ 两轮回测修正后的结论（12 天 10-21~11-05，09:35/09:40/09:45，详见 tmp/
#   oc_sel_early_gate_*_20261106.py）：
#   1) 原假说「前几日流入 + 早盘拉升 = 拉高出货」被数据**否定**：09:35 加闸后
#      保留组 T3 均 -0.110%，被拦组 +1.334% —— 拦掉的是好票。资金流入是
#      **正向因子**（庄在继续做），不是风险信号。
#   2) 用户新假说（庄家需时间吸筹）**成立**，前 5 日净流分档三时点一致单调：
#        净流出组 T3 -4.5~-7.1%，净流入 0~3% 组 +3.5~+4.1%。
#      前 5 日涨幅 <10% 最佳；≥10% 明显变差（09:40 该组 T3 -4.155%、深亏 42.9%）。
#   3) 流通市值「中位」假说**不成立**：20~80 亿反而最差（09:35 -4.187%、
#      09:45 -3.548%），两头（<20 亿 / ≥150 亿）更好。后续 1m 全样本弱市切片中，
#      ≥80 亿深亏率较低但触板率也较低；效果存在取舍，**不做市值硬筛/排序**，
#      只把弱市市值层级与早盘波动线索交给 deep 作为风险证据。
#
# 准入条件（仅 kind=jump；涨停票不拦；仅 as_of <= _EARLY_GATE_TIME 生效）：
#   前 N 日累计净流入 ÷ 流通市值 × 100 > _EARLY_FLOW_IN_THR（默认 1.0）
#   且 前 N 日累计涨幅 < _EARLY_RET5_MAX（默认 10%）
# 生产管道实测（闸 ON，全量池）：
#   阈值 0（12 天 10-21~11-05）：09:35 深亏 10.4%→6.2%、T3 +0.468%→+1.955%；
#                                 09:40 深亏 8.0%→5.6%、T3 +0.102%→+0.588%
#   阈值 1.0（11 天 10-22~11-05）：09:35 n=13 深亏 0%、T3 +6.986%（样本少，
#                                 易被单只极端值拉动）；09:40 n=19 触板
#                                 11.8%→26.3%、T3 +1.079%→+1.931%、深亏 5.3%
#   09:45 深亏 3.2%→5.6% 恶化，被拦组深亏 0%（纯误伤）→ 截止收到 09:40
# 代价：阈值 1.0 下候选约 1.2~1.7 只/天，09:40 触板率翻倍。
_EARLY_GATE_ENABLE = _env_i("EARLY_GATE_ENABLE", 1)
_EARLY_GATE_TIME = os.getenv("QD_TOPDOWN_EARLY_GATE_TIME", "09:40") or "09:40"
_EARLY_FLOW_DAYS = _env_i("EARLY_FLOW_DAYS", 5)
# 前 N 日累计净流入 ÷ 流通市值 × 100 的**准入下限**（%）。
# 默认 1.0 = 要求前 5 日净流入超过流通市值的 1%（明显吸筹）。
# 依据（生产实测，09:40 截止）：0→1.0 时 09:40 T3 均 +1.079%→+1.931%、
# 触板率 11.8%→26.3%（翻倍）；1.0→2.0 则 09:35 样本降到日均 0.5 只且
# T3 -1.935%，过度收紧。0.5 在 09:40 反而 T3 -0.003%，不稳定，故取 1.0。
_EARLY_FLOW_IN_THR = _env_f("EARLY_FLOW_IN_THR", 1.0)
# 前 N 日累计涨幅的**准入上限**（%）。≥ 此值视为已过度拉升、无吸筹空间。
_EARLY_RET5_MAX = _env_f("EARLY_RET5_MAX", 10.0)

_PRESETS: Dict[str, Dict[str, float]] = {
    "strong":  {"boards": _L2_BOARDS_STRONG, "jump_ret": 0.4, "jump_amt_mult": 2.5,
                "follow_sync": 0.55, "follow_ratio": 0.4, "min_fund_pct": 0.5},
    "neutral": {"boards": _L2_BOARDS_NEUTRAL, "jump_ret": 0.5, "jump_amt_mult": 3.0,
                "follow_sync": 0.6, "follow_ratio": 0.5, "min_fund_pct": 1.0},
    "weak":    {"boards": _L2_BOARDS_WEAK, "jump_ret": 0.7, "jump_amt_mult": 4.0,
                "follow_sync": 0.7, "follow_ratio": 0.65, "min_fund_pct": 1.5},
}


def _preset(verdict: str) -> Dict[str, float]:
    """按大盘状态取松紧档：弱市收紧口袋（阈值抬高、每板块只数减少）。"""
    if verdict == "strong":
        p = dict(_PRESETS["strong"])
    elif verdict in ("weak", "shrink"):
        p = dict(_PRESETS["weak"])
    else:
        p = dict(_PRESETS["neutral"])
    p["jump_ret"] = _env_f("JUMP_RET", p["jump_ret"])
    p["jump_amt_mult"] = _env_f("JUMP_AMT_MULT", p["jump_amt_mult"])
    p["follow_sync"] = _env_f("FOLLOW_SYNC", p["follow_sync"])
    p["follow_ratio"] = _env_f("FOLLOW_RATIO", p["follow_ratio"])
    p["boards"] = int(_env_i("L2_BOARDS", int(p["boards"])))
    p["per_board"] = float(_env_i("L3_PER_BOARD", _L3_PER_BOARD))
    p["min_fund_pct"] = _env_f("MIN_FUND_PCT", p["min_fund_pct"])
    return p


def market_gate(as_of: Optional[str] = None,
                lookback: Optional[int] = None,
                day: Optional[str] = None) -> Dict[str, Any]:
    """L1 大盘闸：同刻量能 vs 前 N 日 + 多指数分组最差同刻涨跌 → 市场状态与松紧档。

    「放大上涨=强」：vr ≥ VR_STRONG 且 idx_ret > 0；
    「放大下跌=弱」：vr ≥ VR_STRONG 且 idx_ret < 0（最危险，放量出货）；
    「缩量」：vr < VR_WEAK（无量无行情）。
    弱市**不空仓**，只把 params 收紧（跳变/跟随阈值抬高、每板块只数减少）。
    idx_ret 为多指数分组最差值（见 `_L1_IDX_GROUPS`），`idx_detail` 留各指数明细，
    `idx_norm` 为归一化展示口径（创科 ÷2，不进判定）。

    Returns:
        {ok, verdict, vr, idx_ret, idx_detail, idx_norm, fp_pct, params, day, as_of, notes}
    """
    lb = int(lookback if lookback is not None else _L1_LOOKBACK)
    lb = max(1, min(5, lb))
    try:
        m = _sq.market_same_t(as_of=as_of, days=6, end_day=day)
    except Exception as e:
        logger.warning("[topdown] L1 大盘基准失败: %s", e)
        return {"ok": False, "reason": f"{type(e).__name__}: {e}",
                "verdict": "unknown", "params": _preset("neutral")}
    vr = m.get("vr")
    fp = m.get("fp_pct")
    day = (m.get("days") or [None])[-1]
    # 多指数同刻涨跌：分组各取最差，组间再取最差作为闸口径（只收紧不放松）
    idx_panel = {sym: _sq.index_same_t(day=day, t=m.get("as_of"), symbol=sym)
                 for grp in _L1_IDX_GROUPS for sym in grp}
    grp_worst = [min((v for v in (idx_panel.get(s) for s in grp) if v is not None),
                     default=None)
                 for grp in _L1_IDX_GROUPS]
    valid = [v for v in grp_worst if v is not None]
    idx_ret = round(min(valid), 2) if valid else None
    worst_sym = min(idx_panel, key=lambda s: idx_panel[s]) \
        if idx_panel and all(v is not None for v in idx_panel.values()) else None
    # 归一化明细（仅展示，不进判定）：创科 ÷2 换算到主板 10% 涨跌停口径
    idx_norm = {s: round(v * _L1_IDX_SCALE.get(s, 1.0), 2)
                for s, v in idx_panel.items() if v is not None}

    notes: List[str] = []
    if vr is None or idx_ret is None:
        verdict = "unknown"
        notes.append("量比或指数涨跌缺失，按中性处理")
    elif vr >= _L1_VR_STRONG and idx_ret > 0:
        verdict = "strong"
        notes.append(f"放量上涨：量比 {vr}（前{lb}日同刻）× 最差指数 {worst_sym} {idx_ret:+.2f}%")
    elif vr >= _L1_VR_STRONG and idx_ret < 0:
        verdict = "weak"
        notes.append(f"放量下跌：量比 {vr} × 最差指数 {worst_sym} {idx_ret:+.2f}% —— 收紧口袋")
    elif vr < _L1_VR_WEAK:
        verdict = "shrink"
        notes.append(f"缩量：量比 {vr} —— 收紧口袋")
    else:
        verdict = "neutral"
        notes.append(f"量能一般：量比 {vr} × 最差指数 {worst_sym} {idx_ret:+.2f}%")

    return {"ok": True, "verdict": verdict, "vr": vr, "idx_ret": idx_ret,
            "idx_detail": idx_panel, "idx_norm": idx_norm,
            "fp_pct": fp, "day": day, "as_of": m.get("as_of"),
            "lookback": lb, "params": _preset(verdict), "notes": notes}


def _drop_same_family(track: str, rows: List[Dict[str, Any]],
                      n: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """按输入顺序贪心挑选，跳过与已选板块成分高度重合的同族板块。

    中文板块名不保证有共同前缀 —— 「城商行Ⅲ」「银行」「银行Ⅱ」是同一批成分的
    三个粒度，「电机」「电机Ⅱ」同理，只能按成分集合判同族。Jaccard ≥ 阈值即
    视为同一族：它占的是「各取 N 个」里的同一个名额，不去重会让 N 个名额
    实际只覆盖 1 个板块。

    阈值设 >1 即关闭合并（只保留按原顺序截断），供对照回测用。

    Returns:
        (选中的板块列表, 被合并掉的板块名列表)
    """
    if not rows or n <= 0:
        return [], []
    thr = float(_L2_FAMILY_JACCARD)
    if thr > 1:
        return rows[:n], []
    field = "industry" if track == "industry" else "concepts"
    key = "industry" if track == "industry" else "concept"
    named = [(b, str(b.get(key) or "")) for b in rows]
    try:
        tmap = _sq._tag_map(field, None)
    except Exception as e:
        logger.warning("[topdown] %s 成分表取数失败，同族合并跳过: %s", track, e)
        return rows[:n], []
    members = {nm: {c for c, tags in tmap.items() if nm in tags}
               for nm in {nm for _, nm in named} if nm}
    keep: List[Tuple[Dict[str, Any], str]] = []
    dropped: List[str] = []
    for b, nm in named:
        m = members.get(nm) or set()
        same = False
        if m:
            for _, knm in keep:
                km = members.get(knm) or set()
                if km and len(m & km) / len(m | km) >= thr:
                    same = True
                    break
        if same:
            dropped.append(nm)
            continue
        keep.append((b, nm))
        if len(keep) >= n:
            break
    return [b for b, _ in keep], dropped


def pick_boards(as_of: Optional[str] = None, per_track: Optional[int] = None,
                min_fund_pct: float = 0.0, boards_n: Optional[int] = None,
                day: Optional[str] = None) -> Dict[str, Any]:
    """L2 板块闸：行业轨与概念轨**分开**各取 boards_n 个，按资金流净额降序。

    排序键 `QD_TOPDOWN_L2_SORT`，默认 **net_pct**（净额 ÷ 成交额）。

    net_pct 的确有时刻依赖：分母（当日累计成交额）随时间膨胀，同一板块同一形态
    的比例会被单调稀释（半导体 2026-10-30：净额稳定在 50~85 亿，net_pct 却从
    09:35 的 30.85% 衰减到 14:30 的 -0.84%）。但它是**相对强度**口径，回答的是
    「这个板块的资金关注度有多集中」，这正是选板块要的东西。

    绝对值净额（`QD_TOPDOWN_L2_SORT=net`）反而更差：三天同口径对照（10-28~10-30、
    其余条件一致，仅换排序键）net 的深亏 18.1%／T+3 -2.68%，net_pct 是 9.2%／-0.69%。
    原因是净额天然属于成分多的大板块（半导体 300 亿级），这些票当日涨幅已透支，
    追进去就是深亏；同时属性标签（证金持股 190 只等）会挤进前排。
    `QD_TOPDOWN_L2_MIN_NET_YI` 仅在 net 模式下生效（净额下限，亿元）。

    **板块数随大盘档位**（weak 3 / neutral 4 / strong 6，行业与概念分开计数，
    由 `topdown_pick` 传入 gate 档位的 `boards`；无备选机制 —— 全部板块一次跑完，
    票的合并去重统一放在截断前）。`per_track` 为显式覆盖（回测对照用）。

    行业轨另加成分数下限（默认 10）：申万三级很细，「白银」「光学元件」这类
    可能全 A 只有 1 只票 —— 那不叫板块，选出来的「板块内个股」就是它自己。

    **同族合并**：挑选时跳过与已选板块成分高度重合的同族板块（见 `_drop_same_family`），
    名次在合并后重排，被跳过的板块写进 `missing`。

    Returns:
        {"industry": [...], "concept": [...], "picked": [...], "missing": [],
         "main": [...], "reserve": []}（reserve 键保留恒空，供并集取数兼容）
    """
    n = max(1, int(per_track if per_track is not None
                   else (boards_n if boards_n is not None else _L2_BOARDS_NEUTRAL)))
    by_net = _L2_SORT != "net_pct"
    out: Dict[str, Any] = {"industry": [], "concept": [], "picked": [], "missing": [],
                           "main": [], "reserve": []}
    for track, fn in (("industry", _sq.group_by_industry),
                      ("concept", _sq.group_by_concept)):
        try:
            g = fn(as_of=as_of, top=None, sort_by=_L2_SORT,
                   min_members=_INDUSTRY_MIN_MEMBERS, end_day=day)
        except Exception as e:
            out["missing"].append({"track": track, "reason": f"{type(e).__name__}: {e}"})
            continue
        key = "industry" if track == "industry" else "concept"
        if by_net:
            rows = [b for b in (g.get("boards") or [])
                    if (b.get("net") or -1e18) >= _L2_MIN_NET_YI * 1e8]
        else:
            rows = [b for b in (g.get("boards") or [])
                    if (b.get("net_pct") or -1e9) >= float(min_fund_pct)]
        out[track], fam_dropped = _drop_same_family(track, rows, n)
        for nm in fam_dropped:
            out["missing"].append({"track": track,
                                   "reason": f"同族板块「{nm}」成分与已选重合≥{_L2_FAMILY_JACCARD:.0%}，跳过"})
        for i, b in enumerate(out[track]):
            item = {
                "track": track, "name": b.get(key), "n": b.get("n"),
                "net": b.get("net"), "net_pct": b.get("net_pct"),
                "vol_ratio": b.get("vol_ratio"),
                "tier": "main",
                "rank": i + 1,
            }
            out["picked"].append(item)
            out["main"].append(item)
        out["missing"].extend(g.get("missing") or [])
    out["n"] = len(out["picked"])
    out["n_main"] = len(out["main"])
    out["n_reserve"] = 0
    return out


def board_members(track: str, name: str,
                  as_of: Optional[str] = None) -> List[str]:
    """板块成员代码（track="industry" 用 industry 字段，否则用 concepts）。

    `as_of` 只保留调用契约，不参与取数：成分来自 `stock_basic_info` 的当前
    快照，库内没有按日版本表。历史回测时这是已知静态数据残留（docs §13.4）。
    """
    field = "industry" if track == "industry" else "concepts"
    tmap = _sq._tag_map(field, None)
    return sorted(c for c, tags in tmap.items() if name in tags)


def limit_stats(codes: List[str], zt_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """板块内涨停统计：数量 / 涨停时间分布 / 封板金额 / 封板成交额 / 连板梯队。

    zt_rows 在盘中实时路径由 `cn_stock_ext.limit_pool("limit_up_pool", day)` 提供，
    在给定 `as_of` 的路径由 `zt_rows_asof` 用 snapshot 重建。封板金额=seal_amount
    （封单金额），封板成交额=amount。

    重建路径里这两个值恒为 0（收盘最终值不可用于盘中），此时板块内只按
    zt_time 排序，不给「最终封住的票」任何优先。
    """
    cs = {str(c).split(".")[0] for c in codes}
    rows = [r for r in (zt_rows or []) if str(r.get("stock_code") or "") in cs]
    if not rows:
        return {"n": 0, "rows": [], "seal_amount": 0.0, "amount": 0.0,
                "times": [], "ladder": {}, "leader": None}

    def _t(r: Dict[str, Any]) -> str:
        return str(r.get("zt_time") or "")[:5]

    times = sorted(t for t in (_t(r) for r in rows) if t)
    ladder: Dict[int, int] = {}
    for r in rows:
        d = int(r.get("continuous_zt_days") or 1)
        ladder[d] = ladder.get(d, 0) + 1
    leader = min(rows, key=lambda r: (_t(r) or "23:59", -float(r.get("seal_amount") or 0)))
    return {
        "n": len(rows),
        "rows": [{"code": r.get("stock_code"), "name": r.get("stock_name"),
                  "zt_time": _t(r), "seal_amount": float(r.get("seal_amount") or 0),
                  "amount": float(r.get("amount") or 0),
                  "streak": int(r.get("continuous_zt_days") or 1),
                  "open_count": int(r.get("open_count") or 0)} for r in rows],
        "seal_amount": sum(float(r.get("seal_amount") or 0) for r in rows),
        "amount": sum(float(r.get("amount") or 0) for r in rows),
        "times": times,
        "ladder": dict(sorted(ladder.items())),
        "leader": {"code": leader.get("stock_code"), "name": leader.get("stock_name"),
                   "zt_time": _t(leader),
                   "seal_amount": float(leader.get("seal_amount") or 0)},
    }


def follow_filter(codes: List[str], leader_zt_time: str, zt_codes: List[str],
                  day: Optional[str] = None,
                  window_min: Optional[int] = None,
                  params: Optional[Dict[str, float]] = None,
                  as_of: Optional[str] = None) -> Dict[str, Any]:
    """跟随判定：龙头涨停后，哪些票跟上了、哪些反向、哪些没动。

    基准 = 板块内**剔除涨停票**后的逐分钟涨幅中位数（`screen_query.board_series`）。
    对每只非涨停票在窗口 [T0, T0+window_min] 内：
      · sync  = 个股涨跌方向与板块同向的分钟占比（节奏）
      · r_i / R_S = 窗口内个股涨幅 / 板块涨幅（强度）
      · 跟随   : sync ≥ FOLLOW_SYNC 且 r_i ≥ R_S × FOLLOW_RATIO 且 r_i > 0
      · 反向   : sync ≥ FOLLOW_SYNC 且 r_i < 0 且 R_S > 0
      · 无跟随 : 其余

    同时要求方向同步与强度不落太多 —— 只看涨幅会把「一直趴着突然拉一下」算成跟随。

    `as_of` 是信息边界：板块基准序列与个股 ticks 都只能看到该时刻及以前。
    历史回测若不传，`ticks` 会按「今天此刻」解析到 15:00，等于让 09:35 的
    决策读到 14:59 的分钟数据（实测 10:24 的跳变出现在 09:35 的候选里）。
    生产实时 `as_of=None` 时由 `resolve_as_of` 取当前时刻，行为不变。

    Returns:
        {ok, t0, t1, board_pct, follow: [...], reverse: [...], none: [...], missing}
    """
    p = params or _preset("neutral")
    wm = int(window_min if window_min is not None else _FOLLOW_WINDOW_MIN)
    zts = {str(c).split(".")[0] for c in zt_codes or []}
    pool_codes = [str(c).split(".")[0] for c in codes if str(c).split(".")[0] not in zts]
    if not pool_codes:
        return {"ok": False, "reason": "板块内除涨停票外无成员",
                "follow": [], "reverse": [], "none": []}

    bs = _sq.board_series(pool_codes, day=day, t_to=as_of)
    times = bs.get("times") or []
    pct = bs.get("pct") or []
    t0 = str(leader_zt_time or "")[:5]
    if not t0 or not times:
        return {"ok": False, "reason": "缺龙头涨停时刻或板块序列",
                "follow": [], "reverse": [], "none": []}

    try:
        i0 = min(i for i, t in enumerate(times) if t >= t0)
    except ValueError:
        i0 = len(times) - 1
    idx = [i for i in range(i0, min(len(times), i0 + wm + 1)) if pct[i] is not None]
    if len(idx) < 2:
        return {"ok": False, "reason": "窗口内板块序列样本不足",
                "follow": [], "reverse": [], "none": []}

    base0, base1 = pct[idx[0]], pct[idx[-1]]
    r_s = (base1 - base0) if (base0 is not None and base1 is not None) else 0.0
    t1 = times[idx[-1]]

    got = _sq.ticks(pool_codes, day=day, t_to=t1)
    follow, reverse, none_ = [], [], []
    for code in pool_codes:
        box = got.get(code) or {}
        tks = box.get("ticks") or []
        pc = box.get("prev_close")
        if not tks or not pc:
            none_.append({"code": code, "reason": "无分时"})
            continue
        by_t = {t: px for t, px, _c in tks}
        seq = [(times[i], by_t[times[i]]) for i in idx if times[i] in by_t]
        if len(seq) < 2:
            none_.append({"code": code, "reason": "窗口内 tick 不足"})
            continue
        r_i = (seq[-1][1] / seq[0][1] - 1) * 100
        same = tot = 0
        for k in range(1, len(seq)):
            d_i = seq[k][1] - seq[k - 1][1]
            j = times.index(seq[k][0])
            d_s = (pct[j] - pct[j - 1]) if (j > 0 and pct[j] is not None
                                            and pct[j - 1] is not None) else 0.0
            if d_i == 0 and d_s == 0:
                continue
            tot += 1
            if (d_i > 0 and d_s > 0) or (d_i < 0 and d_s < 0):
                same += 1
        sync = (same / tot) if tot else 0.0
        rec = {"code": code, "r_i": round(r_i, 3), "sync": round(sync, 3),
               "change_pct_now": round((seq[-1][1] / pc - 1) * 100, 2)}
        if sync >= p["follow_sync"] and r_i >= r_s * p["follow_ratio"] and r_i > 0:
            follow.append(rec)
        elif sync >= p["follow_sync"] and r_i < 0 and r_s > 0:
            reverse.append(rec)
        else:
            none_.append(rec)
    return {"ok": True, "t0": t0, "t1": t1,
            "board_pct": round(r_s, 3), "window_min": wm,
            "follow": sorted(follow, key=lambda x: -x["r_i"]),
            "reverse": sorted(reverse, key=lambda x: x["r_i"]),
            "none": none_,
            "missing": bs.get("missing") or []}


def detect_jumps(codes: List[str], day: Optional[str] = None,
                 params: Optional[Dict[str, float]] = None,
                 base_min: int = 5,
                 as_of: Optional[str] = None,
                 t_from: Optional[str] = None,
                 exclude_touched: bool = False) -> Dict[str, Any]:
    """跳变检测（板块无涨停时用）：1 分钟内涨幅与成交额同时突增，谁先谁优先。

    t_from：跳变窗口起点 HH:MM。窗口前的分钟照常进 base_A 基准，
      但「首次命中」只认 t ≥ t_from 的分钟 —— 回看「某时刻起的跳变」用，
      如 as_of=10:30 + t_from=09:40 = 「9:40 以来发生跳变的票」。
    exclude_touched：True 时剔除截至 as_of **曾触板**的票（limit_touch 口径，
      与 zt_rows_asof 同判据），供「跳变后至今未涨停」的回看查询；
      生产 L3 不开（涨停集合已由 zt_rows 单独处理）。

    逐票逐分钟（两套判据，按「基准是否够满」自动切换）：
      Δr_t = (P_t / P_{t-1} - 1) × 100
      A_t  = (累计量差) × P_t
      base_A = 前 base_min 分钟 A 的均值

      ① 突发（mode="burst"，基准满 base_min 根后生效）
         Δr_t ≥ JUMP_RET 且 A_t ≥ JUMP_AMT_MULT × base_A
         单根分钟内的量与涨幅同时突增，是最强的动手信号。
      ② 连续流入（mode="streak"，开盘基准不足时生效）
         连续 `confirm_bars` 根同时满足「价格上行且该分钟有成交」，且这段
         累积涨幅 ≥ `JUMP_STREAK_RET`（默认 2%，**独立于 `JUMP_RET`**）。
         开盘头几分钟没有 base_min 根历史可算均值，用「持续性 + 足够涨幅」
         替代「量比」：资金连买几根且已经走出像样的涨幅才算真启动。
         门槛不能沿用 `JUMP_RET`（0.5%/0.7%）—— 开盘连涨 3 根几乎每票都满足，
         会把全部票的 t_first 压在 09:35 之前，让「先跳先选」失效。

    取**首次**满足时刻 t_first，按 t_first 升序（先跳先选）。
    早盘 streak 模式记 `run_bars`（连续流入根数）供复盘，`amt_mult` 记 0
    （该模式下无基准倍数可比，不参与任何阈值判断）。

    没跳变的票不进结果 —— 「资金流入但盘口没反应」在盘中不构成买点。

    `as_of` 是信息边界：`ticks` 必须截到该时刻。拍数不足
    `min(base_min, confirm_bars) + 1` 的票 skipped —— 那是连「连续流入」
    都凑不齐的时刻，本就没有可判信号，不用未来的分钟撑样本。

    Returns:
        {ok, jumps: [{code, t_first, ret, amount, delta_pct}...], skipped,
         touched_n, missing}
    """
    p = params or _preset("neutral")
    uni = sorted({str(c).split(".")[0] for c in codes or []})
    if not uni:
        return {"ok": False, "reason": "无成员", "jumps": []}
    touched: set = set()
    if exclude_touched:
        from .common import get_limit_pct
        for code, t in (_sq.limit_touch(uni, day=day, as_of=as_of) or {}).items():
            pct = get_limit_pct(code, t.get("name") or "")
            prev, hi = t.get("prev_close"), t.get("hi")
            if pct <= 0 or not prev or prev <= 0 or not hi:
                continue
            if float(hi) >= round(float(prev) * (1 + pct / 100.0), 2) - 0.005:
                touched.add(code)
    got = _sq.ticks(uni, day=day, t_to=as_of)
    jumps: List[Dict[str, Any]] = []
    skipped = 0
    for code in uni:
        if code in touched:
            continue
        box = got.get(code) or {}
        tks = box.get("ticks") or []
        pc = box.get("prev_close")
        if len(tks) < min(base_min, _JUMP_CONFIRM_BARS) + 1 or not pc:
            skipped += 1
            continue
        amts: List[float] = []
        prev_cum = tks[0][2]
        hit = None
        streak = 0
        streak_i0 = 0
        for i in range(1, len(tks)):
            t, px, cum = tks[i]
            dv = cum - prev_cum
            prev_cum = cum
            amt = dv * px if dv > 0 else 0.0
            step = (px / tks[i - 1][1] - 1) * 100 if tks[i - 1][1] else 0.0
            window = amts[-base_min:]
            base = (sum(window) / len(window)) if window else 0.0
            amts.append(amt)
            if px > tks[i - 1][1] and amt > 0:
                if streak == 0:
                    streak_i0 = i
                streak += 1
            else:
                streak = 0
            if hit is not None:
                continue
            if t_from and t < t_from:
                continue
            if len(window) >= base_min:
                if step >= p["jump_ret"] and base > 0 and amt >= p["jump_amt_mult"] * base:
                    hit = {"code": code, "t_first": t, "delta_pct": round(step, 2),
                           "ret": round((px / pc - 1) * 100, 2),
                           "amount": round(amt, 0),
                           "amt_mult": round(amt / base, 2), "mode": "burst"}
            elif streak >= _JUMP_CONFIRM_BARS:
                ref = tks[streak_i0 - 1][1] if streak_i0 >= 1 else tks[0][1]
                run_ret = (px / ref - 1) * 100 if ref else 0.0
                if run_ret >= _JUMP_STREAK_RET:
                    hit = {"code": code, "t_first": tks[streak_i0][0],
                           "delta_pct": round(run_ret, 2),
                           "ret": round((px / pc - 1) * 100, 2),
                           "amount": round(amt, 0),
                           "amt_mult": 0.0, "mode": "streak", "run_bars": streak}
        if hit:
            jumps.append(hit)
    jumps.sort(key=lambda x: (x["t_first"], -x["ret"]))
    return {"ok": True, "jumps": jumps, "skipped": skipped,
            "touched_n": len(touched),
            "params": {"jump_ret": p["jump_ret"], "jump_amt_mult": p["jump_amt_mult"],
                       "confirm_bars": _JUMP_CONFIRM_BARS},
            "n": len(jumps)}


def _jump_vol_ratio(codes: List[str], day: Optional[str],
                    as_of: Optional[str]) -> Dict[str, Optional[float]]:
    """前 5 日同刻累计量**均值**做基准的同刻量比（与 screen_query.cross_section 同定义）。

    只服务 L3 无涨停分支：单板块候选不过几十只，一次 6 日 panel 取数足够。
    不直接调 `cross_section` 是因为它会额外跑一次全市场 `market_same_t`（仅为
    `meta.market_vr`），逐板块调用时白花 ~4s；这里只要逐票比值。

    `day` 为 None（实盘）时基准取 panel 里今天的**前 5 个交易日**；历史回放时
    给定 day，基准严格取 day 之前（不把 day 自己算进分母）。
    返回 {code: 比值}，无历史量（次新/停牌复牌）给 None —— 调用方按“不可验证”处理。
    """
    if not codes:
        return {}
    p = _sq.snap_panel(as_of=as_of, codes=sorted(set(codes)), days=6,
                       with_net=False, end_day=day, with_last=False)
    rows = p.get("rows") or {}
    days = list(p.get("days") or [])
    today, prev = (days[-1] if days else None), days[:-1][-5:]
    out: Dict[str, Optional[float]] = {}
    for code, byday in rows.items():
        cur = (byday.get(today) or {}).get("vol") if today else None
        base = [(byday.get(d) or {}).get("vol") for d in prev]
        base = [v for v in base if v]
        out[code] = round(cur / (sum(base) / len(base)), 3) if cur and base else None
    return out


def pick_stocks_in_board(track: str, name: str, zt_rows: List[Dict[str, Any]],
                         as_of: Optional[str] = None, day: Optional[str] = None,
                         params: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """L3：板块内选股（有涨停走健康闸+涨停票，无涨停走跳变检测）。

    有涨停分支里 `follow_filter` 只作**板块健康闸**（无跟随/反向跟随 → 不出票），
    跟随票本身不进 picked —— 实测负期望分支（F 组 uniq+K3：T+3 胜率 20.3%、
    中位 -4.50%，跳变 32.0% / -3.23%）。

    Returns:
        {track, name, branch: "limit_up"|"jump", stats, picked: [{code, reason, score...}],
         follow, follow_n, missing}
    """
    p = params or _preset("neutral")
    members = board_members(track, name, as_of=as_of)
    if not members:
        return {"track": track, "name": name, "branch": "none",
                "picked": [], "missing": [{"code": "*", "reason": "板块无成员或未归类"}]}

    stats = limit_stats(members, zt_rows)
    zt_codes = [r["code"] for r in stats.get("rows") or []]
    out: Dict[str, Any] = {"track": track, "name": name, "members": len(members),
                           "stats": stats, "missing": []}
    # 每板块上限：涨停分支与纯跳变分支共用，必须在分支前求值 ——
    # 旧版只在「有涨停」分支内赋值，无涨停板块走 else 时 NameError 被上层吞掉，
    # 该板块静默零出票（实测 10-29 13:30 八个板块里七个因此没有任何候选）。
    cap = int(p["per_board"])
    if stats["n"] > 0:
        fl = follow_filter(members, (stats.get("leader") or {}).get("zt_time") or "",
                           zt_codes, day=day, params=p, as_of=as_of)
        picked: List[Dict[str, Any]] = []
        zt_sorted = sorted(stats.get("rows") or [],
                           key=lambda r: (r["zt_time"] or "23:59", -r["seal_amount"]))
        for r in zt_sorted[:cap]:
            amt = float(r["seal_amount"]) / 1e8
            head = f"涨停{r['zt_time']}" if amt > 0 else f"触板{r['zt_time']}"
            picked.append({"code": r["code"], "name": r.get("name"),
                           "t_first": (r["zt_time"] or "")[:5],
                           "reason": head
                                     + (f" 封板{amt:.1f}亿" if amt > 0 else "")
                                     + (" " + str(r["streak"]) + "连板" if r["streak"] > 1 else ""),
                           "kind": "limit_up", "score": float(r["seal_amount"])})
        out.update({"branch": "limit_up", "picked": picked, "follow": fl,
                    "follow_n": len(fl.get("follow") or []),
                    "reverse_n": len(fl.get("reverse") or []),
                    "none_n": len(fl.get("none") or []),
                    "zt_total": stats["n"], "zt_capped": len(zt_sorted) > cap})
        if _JUMP_WITH_ZT:
            # 有涨停的板块同样跑跳变检测（对象=非涨停成员）：板块按资金流选出来
            # 通常都有涨停，二选一会让跳变票供给恒为 0 —— 那样「按首次跳变时间
            # 排序」「满卡给非涨停」都无从谈起。涨停票与跳变票合并后按时间取 cap。
            zt_set = {str(r["code"]).split(".")[0] for r in (stats.get("rows") or [])}
            pool_no_zt = [c for c in members if str(c).split(".")[0] not in zt_set]
            jp = detect_jumps(pool_no_zt, day=day, params=p, as_of=as_of) if pool_no_zt else {"jumps": []}
            for j in (jp.get("jumps") or []):
                picked.append({"code": j["code"], "t_first": j["t_first"],
                               "reason": f"跳变{j['t_first']} {j['delta_pct']:+.2f}%/分 "
                                         f"量×{j['amt_mult']:.1f}",
                               "kind": "jump",
                               "score": -float(j["t_first"].replace(":", ""))})
            out["jumps"] = jp
            out["branch"] = "limit_up+jump"
            picked.sort(key=lambda x: (x.get("t_first") or "99:99"))
            # 跳变票精细截断（v2：全取好段 0-6，只裁尾盘垃圾 7+）。
            # ★ v1→v2 根因见常量注释 L144-163。三段式逻辑不变，只改默认参数：
            #   v1 默认 (2,4,2) → 先2 + 跳2-3 + 后2
            #   v2 默认 (6,7,2) → 全取 idx 0-5 + 接力 idx 7+ 限 2 只
            # 13 天回测：v1 T3均=-0.147% → v2 T3均=+0.125%（翻正）
            #
            # 截断步骤：
            #   (a) 涨停票全取（不变）
            #   (b) first = idx 0..CAP_FIRST-1 全取（好段 0-5，T3均 +0.3~+0.6%）
            #   (c) sec = idx >= IDX_MIN 接力，FUND_THR 质量门 + CAP_SEC cap
            #   (d) 总跳变票 cap = CAP_FIRST + CAP_SEC
            #       若 FUND_THR < 0 关闭质量门；CAP_FIRST < 0 关闭整个截断。
            if _ZT_JUMP_CAP_FIRST >= 0:
                zt: List[Dict[str, Any]] = []
                jp_sorted: List[Dict[str, Any]] = []
                for x in picked:
                    if x.get("kind") == "limit_up":
                        zt.append(x)
                    else:
                        jp_sorted.append(x)
                # picked 已按 t_first 排序（L731），jp_sorted 继承此顺序
                first = jp_sorted[:_ZT_JUMP_CAP_FIRST]
                rest = jp_sorted[_ZT_JUMP_CAP_FIRST:]
                sec = [p for i, p in enumerate(rest)
                       if i + _ZT_JUMP_CAP_FIRST >= _ZT_JUMP_IDX_MIN
                       and (_ZT_JUMP_FUND_THR < 0
                            or (p.get("fund_circ_ratio") or 0) > _ZT_JUMP_FUND_THR)
                       ][:_ZT_JUMP_CAP_SEC]
                kept = zt + first + sec
                out["jump_capped_n"] = len(picked) - len(kept)
                out["jump_first_n"] = len(first)
                out["jump_sec_n"] = len(sec)
                picked = kept
            picked = picked[:cap]
            # ⚠ 必须回写：L702 的 out.update 存的是同一个 list 引用，而上面
            # `picked = kept` / `picked = picked[:cap]` 是**局部重新绑定新 list**，
            # 不回写则 out["picked"] 仍是未截断的完整候选（旧版就是这样，
            # 实测白酒板块 1 涨停 + 15 跳变全部出票，per_board 上限形同虚设）。
            out["picked"] = picked
        if not fl.get("ok"):
            out["missing"].append({"code": "*",
                                   "reason": f"跟随判定跳过：{fl.get('reason')}"
                                             f"（板块 {stats['n']} 只涨停，"
                                             f"非涨停成员不足以合成基准）"})
    else:
        jp = detect_jumps(members, day=day, params=p, as_of=as_of)
        picked = [{"code": j["code"], "t_first": j["t_first"],
                   "reason": f"跳变{j['t_first']} {j['delta_pct']:+.2f}%/分 "
                             f"量×{j['amt_mult']:.1f}",
                   "kind": "jump", "score": -float(j["t_first"].replace(":", ""))}
                  for j in (jp.get("jumps") or [])]
        # 无涨停分支准入闸：板块本身没有任何涨停，等于没有板块级资金共识，
        # 只能靠个股自身「今天是否真有资金在做」来验证。用同刻量比（今日累计量 ÷
        # 前 5 日同刻均值）而不是资金净额：净额要流通市值，次新/缺换手率的票会整批
        # 失效；量比对全市场都有值。**闸只在本分支生效，"有涨停"分支保持原样**。
        # 实测（10-29~11-02、12 快照、生产均值口径）：剔除约 20% 候选，
        # 深亏 5→1 笔、负收益累计 -176→-70、最差单笔 -20.11→-8.74，
        # T+3 胜 69.8%→75.2%、累计收益 430→396（用 ~7% 总收益换尾部风险减半）。
        # 该比值不可得（次新无 5 日历史）按“不可验证”剔除，不默认放行。
        # `QD_TOPDOWN_JUMP_MIN_VR=0` 关闭闸。
        if _JUMP_MIN_VR > 0 and picked:
            vr = _jump_vol_ratio([x["code"] for x in picked], day, as_of)
            keep = [x for x in picked
                    if vr.get(x["code"]) is not None and vr[x["code"]] >= _JUMP_MIN_VR]
            if len(keep) != len(picked):
                out["missing"].append({
                    "code": "*",
                    "reason": (f"纯跳变量比闸：剔除 {len(picked) - len(keep)} 只"
                               f"（同刻量比 < {_JUMP_MIN_VR} 或前5日无同刻量）")})
            picked = keep
        picked = picked[:cap]
        out.update({"branch": "jump", "picked": picked, "jumps": jp})
    return out


def _board_union_members(boards: Dict[str, Any]) -> List[str]:
    """主选 + 备选板块的成员并集。

    一次性把两轨标签表读进来再本地分组 —— 逐板块调 `board_members` 会把
    全市场标签表重复扫 N 遍。
    """
    items = (boards.get("main") or []) + (boards.get("reserve") or [])
    if not items:
        return []
    maps = {"industry": _sq._tag_map("industry", None),
            "concept": _sq._tag_map("concepts", None)}
    out = set()
    for b in items:
        tags = maps.get(b.get("track")) or {}
        out |= {c for c, ts in tags.items() if b.get("name") in ts}
    return sorted(out)


def zt_rows_asof(day: str, as_of: str, codes: List[str]
                 ) -> List[Dict[str, Any]]:
    """用 snapshot 重建「截至 as_of 曾触板」的涨停行（回测/指定时刻用）。

    `limit_up_pool` 对历史日期返回的是**当日收盘后**的名单，只含最终涨停的票：
    09:40 拿它做决策，既看到 14:50 才涨停的票（前视），又漏掉 09:40 涨停但
    后来炸板收绿的票（幸存者偏差）。这里改用 snapshot 自判 —— 截至 as_of 的
    最高价触及涨停价即算触板，首次达到该价的时刻就是 zt_time。

    ★ 不读取收盘涨停池：seal_amount / amount / continuous_zt_days / open_count
    都是**收盘最终值**（炸板票为 0、最终封住的票才有大封单）。把它们填进盘中
    行，等于让「谁最终封得最死」参与板块内 cap 截断与全局 score 排序 —— 那是
    同一条前视链的另一半。这里一律置零/置一，板块内只按 zt_time 排序。
    名称取 snapshot 的 name（静态事实，无时间维度）。

    Args:
        codes: 候选成员（两轨主选 + 备选板块的并集）

    Returns:
        与 `limit_up_pool` 同构的行；无触板票返回 []
    """
    from .common import get_limit_pct
    touch = _sq.limit_touch(codes, day=day, as_of=as_of)
    if not touch:
        return []
    out: List[Dict[str, Any]] = []
    for code, t in touch.items():
        prev, hi = t.get("prev_close"), t.get("hi")
        pct = get_limit_pct(code, t.get("name") or "")
        if pct <= 0 or not prev or prev <= 0 or not hi:
            continue
        lu = round(float(prev) * (1 + pct / 100.0), 2)
        if float(hi) < lu - 0.005:
            continue
        zt = str(t.get("hi_time") or "")
        out.append({"stock_code": code,
                    "stock_name": t.get("name") or "",
                    "zt_time": f"{zt}:00" if zt else "",
                    "seal_amount": 0.0,
                    "amount": 0.0,
                    "continuous_zt_days": 1,
                    "open_count": 0})
    return out


def _zt_streak_before(bars: List[Dict[str, Any]], code: str, day: str) -> int:
    """截至 day 前一交易日收盘的连续涨停天数（只用已收盘日线）。

    `day` 当日 bar 一律排除：盘中它是半截快照，盘后是收盘最终值，两者对
    「此刻」都是未来信息。涨停判据与 `_helpers.tech_summary` 主循环同口径
    （`limit_pct - STREAK_SURGE_ZT_TOL` 容差），勿单独改动阈值。
    """
    from .common import get_limit_pct
    pct = get_limit_pct(code)
    if pct <= 0:
        return 0
    arr = [b for b in bars if str(b.get("time") or "")[:10] < day]
    n = 0
    for i in range(len(arr) - 1, 0, -1):
        pc, cl = arr[i - 1].get("close"), arr[i].get("close")
        if not pc or not cl or float(pc) <= 0 or float(cl) <= 0:
            break
        if (float(cl) - float(pc)) / float(pc) * 100 >= pct - _STREAK_SURGE_ZT_TOL:
            n += 1
        else:
            break
    return n


def exclude_streak_surge(items: List[Dict[str, Any]], day: str,
                         as_of: Optional[str],
                         zt_codes: List[str]) -> Tuple[List[Dict[str, Any]],
                                                       List[Dict[str, Any]], int]:
    """准入闸：放弃「当前连板高度 ≥N 且当日放量上涨」的候选。

    连板高度 = 截至昨收的连续涨停天数 + 今日截至 as_of 是否曾触板。
    加今日触板是因为「昨日首板 + 今日再板」已经是 2 连板，正是要挡的高位加速票。
    放量 = 当日截至 as_of 累计量 ÷ 前 VR_DAYS 个交易日**同一时刻**累计量均值；
    两个量都截到 as_of，避免拿全天量比去比盘中累计量。
    上涨 = 当日截至 as_of 末拍 last 相对前收 > MIN_RET。

    数据边界：日线只取 time < day 的已收盘 bar；snapshot 只取 time <= as_of；
    今日触板集合来自调用方已算好的 zt_rows（回测用 snapshot 重建、生产用实时池）。
    任一侧缺失（ret 或 vr 为 None）时保留该票并计入 unknown，不猜、不默认剔除。

    Returns:
        (kept, excluded, unknown_n)；excluded 每行带 excluded_reason 便于复盘。
    """
    if not _STREAK_SURGE_ENABLE or not items:
        return list(items), [], 0
    codes = sorted({str(x.get("code") or "").split(".")[0]
                    for x in items if x.get("code")})
    if not codes:
        return list(items), [], 0

    kl = _sq.kline_batch(codes, days=_STREAK_SURGE_LOOKBACK, as_of=day)
    bars = kl.get("codes") or {}
    panel = _sq.snap_panel(as_of=as_of, codes=codes,
                           days=_STREAK_SURGE_VR_DAYS + 1,
                           end_day=day, with_net=True, net_days=1,
                           with_last=True)
    rows = panel.get("rows") or {}
    zt = {str(c).split(".")[0] for c in zt_codes}

    kept: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    unknown = 0
    for x in items:
        code = str(x.get("code") or "").split(".")[0]
        streak = _zt_streak_before(bars.get(code) or [], code, day)
        if code in zt:
            streak += 1
        if streak < _STREAK_SURGE_MIN_STREAK:
            kept.append(x)
            continue

        by_day = rows.get(code) or {}
        rec = by_day.get(day) or {}
        last, pc, vol = rec.get("last"), rec.get("prev_close"), rec.get("vol")
        ret = ((float(last) / float(pc) - 1) * 100
               if last and pc and float(pc) > 0 else None)
        hist = [float(v["vol"]) for d, v in sorted(by_day.items())
                if d < day and v.get("vol") and float(v["vol"]) > 0]
        if _STREAK_SURGE_VR_DAYS > 0:
            hist = hist[-_STREAK_SURGE_VR_DAYS:]
        vr = (float(vol) / (sum(hist) / len(hist))
              if vol and len(hist) >= _STREAK_SURGE_MIN_HIST else None)

        if ret is None or vr is None:
            unknown += 1
            kept.append(x)
        elif ret > _STREAK_SURGE_MIN_RET and vr >= _STREAK_SURGE_MIN_VR:
            excluded.append({**x, "excluded_reason":
                             f"{streak}连板 量×{vr:.2f} 涨{ret:+.2f}%"})
        else:
            kept.append(x)
    return kept, excluded, unknown


def attach_flow_strength(items: List[Dict[str, Any]], day: str,
                         as_of: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """给候选票挂「资金流通比」与低区有效标记。

    为什么用比值而不是净额绝对值：同额资金对 5 亿流通盘的影响是 50 亿盘的 10 倍，
    绝对净额天生偏向大流通市值票、跨票不可比。`fund_circ_ratio` = 净额 ÷ 流通市值
    × 100，已按盘子归一，可跨票直接比。

    低区无效区间：流通市值 < `QD_TOPDOWN_MIN_CIRC_MV`（默认 15 亿）的票，
    几十万就能撬动、指标方差主导，判 `flow_valid=False`（不参与排序、不当 0）。
    流通市值由 `成交额 ÷ (换手率/100)` 反推（两者都来自同一 snapshot 面板，
    口径一致）；换手率/成交额缺失 → circ_mv=None → 同样判无效，不猜。

    Returns:
        (items 带 fund_circ_ratio / circ_mv / flow_valid, 低区无效只数)
    """
    codes = sorted({str(x.get("code") or "").split(".")[0]
                    for x in items if x.get("code")})
    if not codes:
        return list(items), 0
    got = _sq.cross_section(as_of=as_of, codes=codes, end_day=day,
                            features=["amount", "turnover_pct", "fund_circ_ratio"])
    by_code = {str(r.get("code") or ""): r for r in (got.get("rows") or [])}
    floor = float(_MIN_CIRC_MV_YI) * 1e8
    out: List[Dict[str, Any]] = []
    invalid = 0
    for x in items:
        r = by_code.get(str(x.get("code") or "").split(".")[0]) or {}
        amt, turn = r.get("amount"), r.get("turnover_pct")
        mv = (float(amt) / (float(turn) / 100.0)) if (amt and turn and float(turn) > 0) else None
        fcr = r.get("fund_circ_ratio")
        valid = bool(mv is not None and mv >= floor and fcr is not None)
        if not valid:
            invalid += 1
        out.append({**x,
                    "fund_circ_ratio": fcr,
                    "circ_mv": round(mv, 0) if mv is not None else None,
                    "flow_valid": valid})
    return out, invalid


def _early_accumulation_gate(items: List[Dict[str, Any]], day: str,
                             as_of: Optional[str]
                             ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """早盘「庄家吸筹」准入闸：前 N 日有净流入 且 涨幅未过度。

    ★ 结论来自 2026-11-06 两轮回测（12 天 10-21~11-05，09:35/09:40/09:45，
      脚本 tmp/oc_sel_early_gate_*_20261106.py），关键翻转：
      · 原「前几日流入 + 早盘拉升 = 拉高出货」假说被否定：09:35 拦掉的是好票
        （被拦组 T3 +1.334% vs 保留组 -0.110%）。资金流入是**正向因子**。
      · 新假说「庄家需时间吸筹」成立：前 5 日净流分档三时点一致单调
        （净流出 -4.5~-7.1%，净流入 0~3% +3.5~+4.1%）；前 5 日涨幅 ≥10% 明显变差。
      · 「中位流通市值更优」不成立（20~80 亿最差）→ 本闸不引入市值条件。

    准入条件（仅 kind=jump；涨停票不拦；仅 as_of <= _EARLY_GATE_TIME 生效）：
        前 N 日累计净流入 ÷ 流通市值 × 100 > _EARLY_FLOW_IN_THR（默认 1.0）
        且 前 N 日累计涨幅 < _EARLY_RET5_MAX（默认 10%）

    数据口径：snap_panel(as_of="15:00", end_day=day) 取 day 之前 N 个交易日的
    全日净额与末拍价（**不含今日**，避免前视）。5 日涨幅 = 前1日收 ÷ 前6日收 - 1。
    流通市值复用 attach_flow_strength 已挂的 circ_mv（与 fund_circ_ratio 同口径）。
    数据不足（缺 N 日净流 / 末拍价 / 流通市值）→ 不可验证，保留并计数。

    Returns:
        (kept, excluded, unknown)
    """
    if not _EARLY_GATE_ENABLE or not as_of:
        return list(items), [], 0
    if as_of > _EARLY_GATE_TIME:
        return list(items), [], 0
    jump_items = [x for x in items if x.get("kind") == "jump"]
    if not jump_items:
        return list(items), [], 0
    codes = sorted({str(x.get("code") or "").split(".")[0] for x in jump_items})
    # N+2 天窗口：N 日净流 + 多 1 个交易日做涨幅起点（前6日收）
    p = _sq.snap_panel(as_of="15:00", codes=codes, days=_EARLY_FLOW_DAYS + 2,
                       with_net=True, full_net=True, end_day=day, with_last=True)
    rows = p.get("rows") or {}
    d_prev = sorted(d for d in (p.get("days") or []) if d < day)
    kept: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    unknown = 0
    for x in items:
        if x.get("kind") != "jump":
            kept.append(x)
            continue
        code = str(x.get("code") or "").split(".")[0]
        byday = rows.get(code) or {}
        circ_mv = x.get("circ_mv")
        nets = [(byday.get(d) or {}).get("net")
                for d in d_prev[-_EARLY_FLOW_DAYS:]]
        nets = [n for n in nets if n is not None]
        # 前1日收 与 前6日收（5 日涨幅的起止点）
        c1 = (byday.get(d_prev[-1]) or {}).get("last") if d_prev else None
        c6 = ((byday.get(d_prev[-_EARLY_FLOW_DAYS - 1]) or {}).get("last")
              if len(d_prev) >= _EARLY_FLOW_DAYS + 1 else None)
        if len(nets) < _EARLY_FLOW_DAYS or not circ_mv or not c1 or not c6:
            unknown += 1
            kept.append(x)
            continue
        ratio = sum(nets) / float(circ_mv) * 100.0
        ret5 = (float(c1) / float(c6) - 1) * 100.0
        if ratio <= _EARLY_FLOW_IN_THR:
            excluded.append({**x, "excluded_reason":
                             f"前{_EARLY_FLOW_DAYS}日净流入{ratio:+.2f}%（无庄吸筹）"})
        elif ret5 >= _EARLY_RET5_MAX:
            excluded.append({**x, "excluded_reason":
                             f"前{_EARLY_FLOW_DAYS}日涨幅{ret5:+.1f}%（已过度拉升）"})
        else:
            kept.append(x)
    return kept, excluded, unknown


def topdown_pick(as_of: Optional[str] = None, day: Optional[str] = None,
                 limit: Optional[int] = None) -> Dict[str, Any]:
    """三层漏斗主流程：L1 大盘闸 → L2 板块闸 → L3 板块内选股 → 截断。

    `as_of` 是**信息边界**：该时刻之后才发生的事一律不可见。给定 `as_of` 时，
    涨停集合由 `zt_rows_asof` 用 snapshot 重建 —— 收盘涨停池既含"此刻还没涨停的票"
    （前视），又缺"涨停过又炸板的票"（幸存者偏差），两种偏差都不允许进入盘中决策。
    `as_of=None`（生产实时）时直接用实时涨停池。

    截断前过一道准入闸 `exclude_streak_surge()`：当前连板高度 ≥
    `QD_TOPDOWN_STREAK_SURGE_MIN_STREAK`（默认 2，含今日截至 as_of 曾触板）
    且当日放量上涨（同刻量比 ≥ `MIN_VR`、末拍涨幅 > `MIN_RET`）的票一律放弃；
    `QD_TOPDOWN_STREAK_SURGE_ENABLE=0` 可整闸关闭。闸只吃已收盘日线与
    `as_of` 及以前的 snapshot，不引入未来信息；数据不足的票保留并计数。

    Args:
        as_of: 截断时刻 HH:MM；None=此刻（用实时涨停池原文）
        day: 目标交易日；None=今天（涨停池/分时取数的日期）
        limit: 最终送 deep 的只数；None 取 env QD_TOPDOWN_L3_LIMIT（默认 25）

    Returns:
        {ok, gate, boards, per_board: [...], picked: [...], funnel: {...}, missing}
    """
    lim = int(limit if limit is not None else _L3_LIMIT)
    lim = max(1, lim)
    from app.utils.trading_calendar import current_data_day
    d = str(day or current_data_day())[:10]
    gate = market_gate(as_of=as_of, day=d)
    p = gate.get("params") or _preset("neutral")
    boards = pick_boards(as_of=as_of, day=d,
                         min_fund_pct=max(0.0, p["min_fund_pct"] - _MIN_BOARD_FUND_PCT),
                         boards_n=int(p.get("boards") or _L2_BOARDS_NEUTRAL))

    if as_of:
        zt_rows = zt_rows_asof(d, as_of, _board_union_members(boards))
    else:
        from app.data_sources.cn_stock_ext import limit_pool
        try:
            zt_rows = limit_pool("limit_up_pool", d)
        except Exception as e:
            zt_rows = []
            logger.warning("[topdown] 涨停池取数失败（L3 将全部走跳变分支）: %s", e)

    per_board: List[Dict[str, Any]] = []
    all_picked: List[Dict[str, Any]] = []
    notes: List[str] = []

    def _run(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        got: List[Dict[str, Any]] = []
        for b in items:
            try:
                r = pick_stocks_in_board(b["track"], b["name"], zt_rows,
                                         as_of=as_of, day=d, params=p)
            except Exception as e:
                per_board.append({"track": b["track"], "name": b["name"],
                                  "tier": b.get("tier"),
                                  "error": f"{type(e).__name__}: {e}"})
                continue
            r["board_net_pct"] = b.get("net_pct")
            r["board_net"] = b.get("net")
            r["tier"] = b.get("tier")
            per_board.append(r)
            for x in r.get("picked") or []:
                got.append({**x, "board": b["name"], "track": b["track"],
                            "tier": b.get("tier"),
                            "board_net_pct": b.get("net_pct")})
        return got

    dup_codes: List[str] = []

    def _uniq_mark(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """合并去重 + 重复标记（**统一在截断前执行一次**）。

        同一只票被多个板块同时选中（行业/概念交叉、同族板块）只保留一条，
        `dup_n` = 被选中次数 —— 重复越多说明越多板块的资金共识指向它，
        排序越靠前；`also_in` 记录其余选中板块名。
        """
        seen: Dict[str, Dict[str, Any]] = {}
        order_: List[str] = []
        for x in items:
            c = str(x.get("code") or "")
            if not c:
                continue
            if c in seen:
                seen[c]["dup_n"] = int(seen[c].get("dup_n") or 1) + 1
                seen[c].setdefault("also_in", []).append(x.get("board"))
                if c not in dup_codes:
                    dup_codes.append(c)
                continue
            seen[c] = {**x, "dup_n": 1}
            order_.append(c)
        return [seen[c] for c in order_]

    all_picked += _run(boards.get("main") or [])
    picked_raw_n = len(all_picked)

    all_picked = _uniq_mark(all_picked)

    if _DUP_MIN > 1:
        kept = [x for x in all_picked if int(x.get("dup_n") or 1) >= _DUP_MIN]
        if len(kept) < len(all_picked):
            notes.append(f"多板块共识闸 dup_n<{_DUP_MIN} 放弃 "
                         f"{len(all_picked) - len(kept)} 只，剩 {len(kept)} 只")
        all_picked = kept

    zt_codes = [str(r.get("stock_code") or "").split(".")[0] for r in zt_rows]
    all_picked, streak_excluded, streak_unknown = exclude_streak_surge(
        all_picked, d, as_of, zt_codes)
    if streak_excluded:
        brief = "、".join(f"{x['code']}({x['excluded_reason']})"
                         for x in streak_excluded[:8])
        notes.append(f"连板放量闸放弃 {len(streak_excluded)} 只：{brief}")
    if streak_unknown:
        notes.append(f"连板放量闸 {streak_unknown} 只因同刻量能或末拍价缺失未判（保留）")

    all_picked, flow_invalid = attach_flow_strength(all_picked, d, as_of)
    if flow_invalid:
        notes.append(f"资金流通比 {flow_invalid} 只落在低区无效区间"
                     f"（流通市值 < {_MIN_CIRC_MV_YI:g} 亿或数据缺失），不参与排序")

    # 早盘「庄家吸筹」准入闸：前 N 日有净流入 且 5 日涨幅未过度（仅 jump）。
    # 只对 kind=jump 生效；涨停票不拦。详见 _early_accumulation_gate 注释。
    all_picked, early_excluded, early_unknown = _early_accumulation_gate(
        all_picked, d, as_of)
    if early_excluded:
        brief = "、".join(f"{x['code']}({x['excluded_reason']})"
                         for x in early_excluded[:8])
        notes.append(f"早盘吸筹准入闸放弃 {len(early_excluded)} 只：{brief}")
    if early_unknown:
        notes.append(f"早盘吸筹准入闸 {early_unknown} 只因前{_EARLY_FLOW_DAYS}日"
                     f"净流/末拍价/流通市值缺失未判（保留）")

    all_picked.sort(key=lambda x: ((x.get("t_first") or "99:99"),
                                   -(x.get("dup_n") or 1),
                                   0 if x.get("flow_valid") else 1,
                                   -(x.get("fund_circ_ratio") or 0)))
    trunc = all_picked[:lim]

    for pb in per_board:
        for m in pb.get("missing") or []:
            if isinstance(m, dict) and m.get("reason"):
                notes.append(f"[{pb.get('track')}/{pb.get('name')}] {m['reason']}")
    if len(trunc) < lim:
        notes.append(f"只选出 {len(trunc)}/{lim} 只（不凑数）："
                     f"强板块内要么已封死无法跟随，要么盘口没跳变")
    return {
        "ok": bool(gate.get("ok")),
        "gate": gate,
        "boards": boards,
        "per_board": per_board,
        "picked": trunc,
        "funnel": {
            "verdict": gate.get("verdict"),
            "idx_ret": gate.get("idx_ret"),
            "boards_n": boards.get("n"),
            "l2_sort": _L2_SORT,
            "boards_main": len(boards.get("main") or []),
            "boards_reserve": len(boards.get("reserve") or []),
            "picked_raw": picked_raw_n,
            "picked": len(trunc),
            "excluded_streak_surge": [x["code"] for x in streak_excluded],
            "streak_surge_unknown": streak_unknown,
            "excluded_early_accumulation": [x["code"] for x in early_excluded],
            "early_accumulation_unknown": early_unknown,
            "dup_codes": dup_codes,
            "dup_max": max((int(x.get("dup_n") or 1) for x in trunc), default=1),
            "dup_min_gate": _DUP_MIN,
            "flow_invalid_n": flow_invalid,
            "limit": lim,
            "short_of_limit": len(trunc) < lim,
            "by_kind": {k: sum(1 for x in trunc if x.get("kind") == k)
                        for k in ("limit_up", "follow", "jump")},
            "day": d,
        },
        "missing": (gate.get("notes") or []) + notes,
    }
