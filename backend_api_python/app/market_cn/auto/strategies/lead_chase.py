#!/usr/bin/env python3
"""lead_chase.py — 领涨追击: 盘中分钟级领涨股追击 (信号即买 → D1 开盘卖)

用户假设 (2026-09-27): 盘中 realtime_snapshot 实时刷新、1m 可算近似资金流, 满足
  ① 资金流入与涨幅成正比 (涨是真金白银推的, 非无量空拉)
  ② 上方压力位明显小 (贴近/突破 20 日高点, 上档套牢盘轻)
  ③ 预估今日明显放量 (按已过分钟外推全天量, 对比 5 日均量)
  ④ 同行业正在共振拉伸, 且自己是领涨 (板块内涨幅第一)
  ⑤ 此前涨幅较少 或已回调 (未被爆炒过)
  ⑥ 涨幅处在可追区间、市值不太大、交投活跃
→ 该股相对活跃, 短线"一定区间内可追涨"。

判定分层 (两段管线, 与 tail/knife 同构):
  intraday_shortlist (全市场快照, 便宜): ④板块共振+领涨 ⑥追涨区间/封板/活跃度
  scan_signals (单票深判: 拉序列+日线):  ①资金流 ②压力位 ③量能预估 ⑤前期形态 ⑥市值

数据通道: ctx={"latest","series","mkt_gain"} (快照序列) + bars (日线, bars[-1]=昨日)。
回测与实盘同一份判定代码 (kline_1m 帧重建 vs realtime_snapshot, 见 data/frames.py)。
回测: python -m app.market_cn.auto.core.backtest --strategy lead_chase --days 60 --out tmp/lead_chase_bt.json

⚠️ 假设占位 (2026-09-27, 用户未指定处, 均参数化待拍板):
  - 出场 = 次交易日开盘卖 (hold_days=1, 框架默认 intraday_exit); stop_pct 仅信息展示
  - 追涨区间默认 2~7% (创/科 ×2); 市值 20~250 亿; 放量 ≥1.5 倍; 压力距离 ≤2%
  - 板块口径 = 行业 (一对一); 概念共振 (多对多) 待概念映射表落点确定后扩

易错点:
  - 快照 high/low/volume = **当日累计值**非分钟 bar (frames.py 语义); 序列同
  - 资金流是量价方向**近似** (同 fund_flow_local._derive 口径), 非主力/超大单分类
  - 量能预估的分子分母同量纲 (序列累计量 vs 日线量, synth_bar 同源), 勿混外源数据
  - 板块共振横截面在 shortlist 一次性计算并缓存, scan_signals 只读 —— 策略=纯函数
    纪律下这是唯一有状态处 (g56 _ensure_pool_daily 先例), key=日期+分钟槽, 跨槽失效
  - 近封板判定用名义涨停价 (tail._limit_pct 同口径), 不用 MarketSpec.nominal_up_pct
    (那是 is_limit_up 的判别阈值, 语义不同)
"""

from __future__ import annotations

from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import ScanSpec, Signal, StrategyBase
from app.market_cn.auto.core.data.hub import sector_map, stock_info
from app.market_cn.auto.core.market import get_board_type

STRATEGY_KEY = "lead_chase"
STRATEGY_LABEL = "领涨追击"

PARAMS = {
    # ── ⑥ 可追区间 / 市值 / 活跃度 ──
    "gain_lo": 2.0,            # 当日涨幅下限 % (不追未启动)
    "gain_hi": 7.0,            # 当日涨幅上限 % (不追已爆/近板); 创/科 ×gain_scale
    "cap_lo": 20e8,            # 流通市值下限 (元)
    "cap_hi": 250e8,           # 流通市值上限 (元)
    "active_rank_pct": 0.5,    # 活跃度: 当日现价×累计量排名后 50% 剔除 (排名口径, 免量纲)
    # ── ① 资金流入与涨幅成正比 ──
    "flow_ratio_min": 0.05,    # 净流入/成交额 下限 (量价方向近似)
    "comove_min": 0.6,         # |流入| 中来自上涨分钟的占比下限 (流入集中推涨)
    # ── ② 上方压力小 ──
    "res_dist_max": 2.0,       # 现价距 20 日高点 % 上限 (越小压力越轻); 创/科 ×gain_scale
    # ── ③ 明显放量 ──
    "vol_proj_min": 1.5,       # 预估全天量/5日均量 下限
    # ── ⑤ 此前涨幅少 或 回调 (OR) ──
    "pre20_gain_max": 15.0,    # 前20日涨幅 % 上限 ("涨幅较少" 分支)
    "dd20_min": 5.0,           # 距20日高点回撤 % 下限 ("回调了" 分支)
    # ── ④ 板块共振 + 领涨 ──
    "sector_peers_min": 3,     # 同行业涨幅≥sector_gain_min 的家数下限 (共振成立)
    "sector_gain_min": 1.0,    # 共振成员涨幅下限 %; 创/科 ×gain_scale
    "sector_rank_max": 1,      # 板块内涨幅排名上限 (1=第一, 即领涨)
    # ── 门控 / 生命周期 ──
    "mkt_gate": -1.0,          # 大盘当日涨幅下限 % (-1=不设)
    "min_hhmm": "09:45",       # 信号时间下限 (开盘竞价噪声后)
    "max_hhmm": "14:30",       # 信号时间上限 (尾盘不追)
    "daily_limit": 20,
    "stop_pct": -6.0,          # 信息展示 (D1 开盘卖当日不可盘中止损)
}

#: 创/科板涨幅类阈值缩放 (20cm 波动结构不同, 与 tail 的 nf=0.5 反向: 那是收窄深跌阈值)
GAIN_SCALE_GEM = 2.0

# 横截面板块共振缓存 (shortlist 每分钟槽算一次, scan_signals 只读; 跨槽失效)
_SECTOR_CACHE = {"key": None, "rank": None, "groups": None,
                 "info_date": None, "info": None, "_stock_date": None, "_stock": None}


def _hhmm(s) -> str:
    return str(s)[11:16] if s and len(str(s)) >= 16 else ""


def _gain_scale(code: str) -> float:
    return GAIN_SCALE_GEM if get_board_type(code) == "gem_star" else 1.0


def _limit_pct(code: str) -> float:
    """名义涨停幅度 (近封板买不进判定): 主板 10% / 创科 20%。

    ⚠️ 同 tail._limit_pct 口径 —— 不要用 MarketSpec.nominal_up_pct (已折 up_tol,
    是 is_limit_up 的判别阈值), 两者语义不同。
    """
    return 0.20 if _gain_scale(code) == GAIN_SCALE_GEM else 0.10


def _approx_flow(series):
    """快照序列 → 量价方向近似资金流 (与 fund_flow_local._derive 同口径: 逐拍差分量×方向)。

    Returns: (net, turnover, comove)
      net      近似净流入 (元: 逐拍差分量×现价×方向)
      turnover 成交额近似 (元)
      comove   |流入| 中来自上涨分钟的占比 (0~1) —— "流入与涨幅成正比" 的操作化
    """
    net = up_net = abs_net = turnover = 0.0
    prev = None
    for r in series or []:
        last = float(r.get("last") or 0)
        vol = float(r.get("volume") or 0)
        if prev is not None and last > 0 and vol > prev[1]:
            dvol = vol - prev[1]
            dlast = last - prev[0]
            direction = 1 if dlast > 0 else (-1 if dlast < 0 else 0)
            dnet = dvol * last * direction
            net += dnet
            abs_net += abs(dnet)
            turnover += dvol * last
            if direction > 0:
                up_net += dnet
        prev = (last, vol)
    comove = up_net / abs_net if abs_net > 0 else 0.0
    return net, turnover, comove


def _vol_projection(series, bars):
    """预估全天量/5日均量 (同量纲: 序列累计量 vs 日线量, synth_bar 同源)。

    已过交易分钟占比按槽位近似 (frames MI_HHMM 位置口径, 稀疏票同基线近似)。
    """
    from app.market_cn.auto.core.data.frames import hhmm_to_pos
    if not series:
        return None
    vcum = float(series[-1].get("volume") or 0)
    pos = hhmm_to_pos(_hhmm(series[-1].get("time") or ""))
    if vcum <= 0 or pos < 0 or len(bars or []) < 6:
        return None
    base = [float(b.get("volume") or 0) for b in bars[-5:]]
    base = [v for v in base if v > 0]
    if not base:
        return None
    elapsed = (pos + 1) / 240.0
    return (vcum / elapsed) / (sum(base) / len(base))


def _sectors_of(date: str, hhmm: str, snaps):
    """横截面: 板块内涨幅排名 + 同板块成员涨幅表。shortlist 每分钟槽算一次。

    Returns: (rank, groups)
      rank   {code: 板块内涨幅降序名次 (1-based)}
      groups {code: 同板块 [(code, gain), ...] 降序} —— 空行业票两表皆无 (宁缺勿假共振)
    ⚠️ 缓存 key 必须含分钟槽 —— 每分钟涨幅在变, 只按日期缓存会用上一分钟的排名。
    """
    key = f"{date}:{hhmm}:{len(snaps)}"
    if _SECTOR_CACHE["key"] == key and _SECTOR_CACHE["rank"] is not None:
        return _SECTOR_CACHE["rank"], _SECTOR_CACHE["groups"]
    if _SECTOR_CACHE["info_date"] != date:
        _SECTOR_CACHE["info"] = sector_map()
        _SECTOR_CACHE["info_date"] = date
    smap = _SECTOR_CACHE["info"] or {}
    by_ind = {}
    for code, snap in snaps.items():
        try:
            last, pc = float(snap.get("last") or 0), float(snap.get("previousClose") or 0)
        except (TypeError, ValueError):
            continue
        ind = smap.get(code)
        if ind and last > 0 and pc > 0:
            by_ind.setdefault(ind, []).append((code, (last / pc - 1) * 100))
    rank, groups = {}, {}
    for members in by_ind.values():
        members.sort(key=lambda t: t[1], reverse=True)
        for i, (code, _g) in enumerate(members, 1):
            rank[code] = i
            groups[code] = members
    _SECTOR_CACHE["key"] = key
    _SECTOR_CACHE["rank"], _SECTOR_CACHE["groups"] = rank, groups
    return rank, groups


@register
class LeadChaseStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    entry_style = "lc"
    scan_spec = ScanSpec(kind="intraday_window", windows=("09:45", "14:30"), interval_sec=60)
    default_params = dict(PARAMS)
    PROBE_STAGE_RANK = {"window": 1, "mkt": 2, "sector": 3, "chase": 4, "cap": 5,
                        "flow": 6, "resist": 7, "volume": 8, "history": 9, "signal": 10}
    # 框架契约: 回测未含 U1~U4 (自实现市值/活跃门); 盘中即买 → T+1 当日不可卖
    use_unified_prefilter = False
    entry_at_close = True
    exit_exec_same_day = True
    signal_state = "buy_today"
    data_needs = ("daily", "snapshot", "minute_live")

    # ── 第一段: 全市场快照便宜预筛 (④共振领涨 + ⑥区间/封板/活跃) ──
    def intraday_shortlist(self, snaps, mkt_gain, **params):
        p = self.merged_params(params or None)
        if mkt_gain is not None and mkt_gain < p["mkt_gate"]:
            return {}
        date = hhmm = ""
        for snap in snaps.values():
            t = str(snap.get("time") or "")
            date, hhmm = t[:10], _hhmm(t)
            break
        rank, groups = _sectors_of(date, hhmm, snaps)
        # 活跃度排名 (现价×累计量, 降序; 排名口径免量纲)
        act = []
        for code, snap in snaps.items():
            try:
                act.append((code, float(snap.get("last") or 0) * float(snap.get("volume") or 0)))
            except (TypeError, ValueError):
                continue
        act.sort(key=lambda t: t[1], reverse=True)
        act_cut = int(len(act) * p["active_rank_pct"]) or 1
        hot = {code for code, _ in act[:act_cut]}

        out = {}
        for code, snap in snaps.items():
            if code.startswith(("8", "4", "92")):            # 北交所排除
                continue
            try:
                last = float(snap.get("last") or 0)
                pc = float(snap.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last <= 0 or pc <= 0:
                continue
            scale = _gain_scale(code)
            gain = (last / pc - 1) * 100
            # ⑥ 追涨区间
            if not (p["gain_lo"] * scale <= gain <= p["gain_hi"] * scale):
                continue
            # 近封板买不进 (名义涨停价×0.998, 同 tail 口径)
            if last >= round(pc * (1 + _limit_pct(code)), 2) * 0.998:
                continue
            # 活跃度
            if code not in hot:
                continue
            # ④ 板块共振 + 领涨 (rank≤sector_rank_max; 共振=同板块涨幅达标家数)
            rk = rank.get(code)
            if rk is None or rk > p["sector_rank_max"]:
                continue
            members = groups.get(code) or []
            peers_up = sum(1 for _c, g in members if g >= p["sector_gain_min"] * scale)
            if peers_up < p["sector_peers_min"]:
                continue
            out[code] = snap
        return out

    # ── 第二段: 单票深判 (①资金流 ②压力 ③量能 ⑤前期形态 ⑥市值) ──
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        p = self.merged_params(params or None)
        ctx = ctx or {}
        snap, series = ctx.get("latest"), ctx.get("series") or []
        if not snap or not series:
            return []
        last = float(snap.get("last") or 0)
        pc = float(snap.get("previousClose") or 0)
        if last <= 0 or pc <= 0 or len(bars or []) < 21:
            return []
        _tr = None
        if probe is not None:
            def _tr(stage, **kw):
                probe.trace(stage, code=code, d0_date=str(snap.get("time") or "")[:10], **kw)

        hhmm = _hhmm(snap.get("time") or "")
        if not (p["min_hhmm"] <= hhmm <= p["max_hhmm"]):
            if _tr:
                _tr("window", hhmm=hhmm)
            return []
        mkt_gain = ctx.get("mkt_gain")
        if mkt_gain is not None and mkt_gain < p["mkt_gate"]:
            if _tr:
                _tr("mkt", mkt_gain=mkt_gain)
            return []

        scale = _gain_scale(code)
        day_gain = (last / pc - 1) * 100

        # ⑥ 市值 (流通市值 = 流通股本×现价; 同股本口径 unit-safe; 全量信息按日缓存)
        day = str(snap.get("time") or "")[:10]
        if _SECTOR_CACHE.get("_stock_date") != day:
            _SECTOR_CACHE["_stock"] = stock_info()
            _SECTOR_CACHE["_stock_date"] = day
        si = (_SECTOR_CACHE.get("_stock") or {}).get(code) or {}
        cap = float(si.get("circ_shares") or 0) * last
        if not (p["cap_lo"] <= cap <= p["cap_hi"]):
            if _tr:
                _tr("cap", cap_yi=round(cap / 1e8, 1))
            return []

        # ① 资金流 (量价方向近似): 净流入为正 + 占成交额比 + 流入集中在上涨分钟
        net, turnover, comove = _approx_flow(series)
        flow_ratio = net / turnover if turnover > 0 else 0.0
        if net <= 0 or flow_ratio < p["flow_ratio_min"] or comove < p["comove_min"]:
            if _tr:
                _tr("flow", net_yi=round(net / 1e8, 3), flow_ratio=round(flow_ratio, 4),
                    comove=round(comove, 3))
            return []

        # ② 上方压力: 现价距 20 日高点 (bars 均为 qfq, 与快照除权日微差属已知边界)
        highs = [float(b.get("high") or 0) for b in bars[-20:]]
        res20 = max(highs) if highs else 0
        res_dist = (res20 - last) / last * 100 if res20 > 0 else 99
        if res_dist > p["res_dist_max"] * scale:
            if _tr:
                _tr("resist", res_dist=round(res_dist, 2))
            return []

        # ③ 量能预估
        vol_proj = _vol_projection(series, bars)
        if vol_proj is None or vol_proj < p["vol_proj_min"]:
            if _tr:
                _tr("volume", vol_proj=round(vol_proj or 0, 2))
            return []

        # ⑤ 此前涨幅少 或 回调过 (OR)
        closes = [float(b.get("close") or 0) for b in bars]
        pre20_gain = (pc / closes[-21] - 1) * 100 if closes[-21] > 0 else 99
        dd20 = (res20 - pc) / res20 * 100 if res20 > 0 else 0
        if not (pre20_gain <= p["pre20_gain_max"] or dd20 >= p["dd20_min"]):
            if _tr:
                _tr("history", pre20_gain=round(pre20_gain, 1), dd20=round(dd20, 1))
            return []

        if _tr:
            _tr("signal", gain=round(day_gain, 2), flow_ratio=round(flow_ratio, 4),
                vol_proj=round(vol_proj, 2), res_dist=round(res_dist, 2))

        # 展示分 0~100: 分项加分 (强度加成封顶)
        score = 50
        score += 10 if flow_ratio >= 2 * p["flow_ratio_min"] else 5
        score += 10 if vol_proj >= 2 * p["vol_proj_min"] else 5
        score += 8 if res_dist <= p["res_dist_max"] * scale * 0.5 else 4
        score += 10 if pre20_gain <= p["pre20_gain_max"] * 0.5 or dd20 >= 2 * p["dd20_min"] else 5
        score += 7 if comove >= 0.8 else 3
        score = max(0, min(95, score))
        return [Signal(
            code=code,
            time=str(snap.get("time") or "")[:10],
            score=score,
            price=last,
            label=(f"领涨追击 gain={day_gain:.1f}% flow={flow_ratio:.3f} "
                   f"volx{vol_proj:.1f} res={res_dist:.1f}%"),
            extra={
                "gain": round(day_gain, 2),
                "flow_ratio": round(flow_ratio, 4),
                "comove": round(comove, 3),
                "net_yi": round(net / 1e8, 3),
                "vol_proj": round(vol_proj, 2),
                "res_dist": round(res_dist, 2),
                "pre20_gain": round(pre20_gain, 1),
                "dd20": round(dd20, 1),
                "cap_yi": round(cap / 1e8, 1),
            },
        )]
