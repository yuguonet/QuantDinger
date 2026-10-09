#!/usr/bin/env python3
"""tail_oversold.py — 尾盘超卖超短 (14:50 起滚动买入 → D1 开盘卖)。

超卖反弹: 当日深跌 + 尾盘适度回落 + 贴近日内低位 + 近5日深度超卖 + 振幅大 → 次日
开盘高概率反弹。D0 14:50~15:00 任一分钟触发即买, D1 开盘卖。规则来源 test_v2_tail_buy.py。
入场五条件 (归一化 nf: 创/科板 0.5, 主板 1.0): ①非ST/非北交所/未封板 ②score>=8
③pre5*nf<=-10 ④amplitude*nf>=10 ⑤tail_ret*nf ∈ [-2.8,-0.5] (14:20~14:40 均价→触发时现价)。
流程: 14:30 预热 → 14:50 起每分钟滚动判定/买入 (10 分钟窗, 先到先得) → 15:01 确认持有
→ D1 开盘卖。快速判定 = 两级管线: shortlist 用最新快照必要条件预筛 (预览口径是终审超集),
幸存股 (约10~50只) 才拉序列+日线完整判定。

易错点: 快照 open/high/low=当日累计值非分钟bar; bars[-1]=昨日 (1D 盘中未回填);
分子原始价 vs 分母 qfq 日线除权日有偏差 (盘后以 kline_1m 复权口径为准);
tail_ret 需 mi 199~219 槽位 ≥15 个; T+1 当日不可卖。

回测终审数据 (N=275 胜率80.7% 均收+2.74% 等) → `docs/策略研究依据归档.md#tail_oversold`。
"""

from app.market_cn.auto.strategies import register
from app.market_cn.auto.core.runtime.functions import Ctx, register_strategy_funcs
from app.market_cn.auto.core.market import get_board_type
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)
from app.market_cn.auto.core.present.contract import (   # 展示层折叠契约
    InsufficientHistory, Progress, Stage,
)

STRATEGY_KEY = "tail_oversold"
STRATEGY_LABEL = "尾盘超卖超短"

# 预测分高分段 (2026-09-26): 仅排序/展示, **不挡入场** (笔数保持 L0)。
# 用途: 同时多票时优中选优; 只在策略存续期有意义。
SCORE_HIGH_MIN = 10.0


# ================================================================
# 预测分 (2026-09-26 用户定标): 50=平盘 0%, 100=涨停, 0=跌停
# ================================================================
# 用途: 同时多票时优中选优; 只在策略存续期有意义; **不挡入场** (笔数不变)。
# 标定链: V2 分 → 预期次日收益% (经验锚点, 保序) → 相对涨跌停幅度 → 50 为中心。
#   score = 50 + (E[ret%] / limit_pct) * 50
#   limit_pct: 主板 10 / 创业科创 20 (涨跌停幅度, 与板块一致)
_V2_TO_EXP = (
    (8.3, 0.5),    # V2 → 预期次日收益%
    (8.8, 2.5),
    (9.3, 2.5),
    (9.8, 4.0),
    (10.3, 5.5),
)


def _v2_to_exp_ret(v2_score: float) -> float:
    """V2 → 预期次日收益% (单调插值)。"""
    try:
        s = float(v2_score)
    except Exception:
        return 0.0
    pts = _V2_TO_EXP
    if s <= pts[0][0]:
        return pts[0][1]
    if s >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= s <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (s - x0) / (x1 - x0)
    return 0.0


def pred_score(v2_score: float, code: str = "", limit_pct: float = None) -> int:
    """预测分 0~100: **50=平盘, 100=涨停, 0=跌停** (按板块涨跌停幅度)。

    score = 50 + (E[ret%] / limit_pct) * 50
    limit_pct 缺省按 code 推断 (20cm 创业/科创=20, 其余=10); 也可显式传入。
    仅持仓期内用于同日排序; 不是入场门。
    """
    exp = _v2_to_exp_ret(v2_score)
    if limit_pct is None:
        try:
            from app.market_cn.auto.core.market import get_board_type
            limit_pct = 20.0 if get_board_type(code) == "gem_star" else 10.0
        except Exception:
            limit_pct = 10.0
    try:
        limit_pct = float(limit_pct)
    except Exception:
        limit_pct = 10.0
    if limit_pct <= 0:
        limit_pct = 10.0
    s = 50.0 + (exp / limit_pct) * 50.0
    return int(round(max(0.0, min(100.0, s))))


PARAMS = {
    "score_min": 8.0,          # V2 评分下限 (归一化后)
    "pre5_max": -10.0,         # pre5_gain*nf 上限 (深度超卖)
    "amp_min": 8.0,            # amplitude*nf 下限 (2026-09-26 120d 标定: 8.0 n=306 胜率82.7% 盈亏比2.04; 原10.0 n=274 胜率82.8% 盈亏比2.15, 差异不大但覆盖更全; 与 config.json 对齐)
    "tail_lo": -2.8,           # tail_ret*nf 下限
    "tail_hi": -0.5,           # tail_ret*nf 上限 (跌太多=还在崩)
    "min_hhmm": "14:50",       # 快照时间下限 (= 滚动预览窗口起点, 早于此不出信号)
    "stop_pct": -8.0,          # 止损 % (仅信息展示, T+1 当日不可卖, D1 开盘卖)
    "hold_days": 1,            # 持有1天 (D1开盘卖)
}
_SHORTLIST_SLACK_PCT = 0.15   # 预筛容差(百分点): 吸收原始价/复权价微差, 放宽保超集

_WIN = 6        # 切片窗口 (pre5 分母 closes[-5] + probe 锚)
_MIN_AGE = 6    # 旧口径: len(closes) < 6 → 无判定

# 评分除 day_gain 外其余四维的上限合计 (tail 3.0 + pos 2.0 + amp&tail 1.0 + pre5 0.3),
# 与 _calc_score 分支表一一对应 —— 改评分表必须同步改这里。
_SHORTLIST_OTHER_MAX = 6.3


def _shortlist_dg_max(score_min):
    """预筛 day_gain*nf 上界, 由 score_min 反推 (2026-09-28 修 A5, 替代硬编码 -5)。

    反推: 信号可达 score_min ⇒ day_gain 档位分 >= score_min - _SHORTLIST_OTHER_MAX
    ⇒ 取满足的最宽档界 (dg>该界时评分上限 < score_min, 必被判定拒绝, 预筛丢弃不漏)。
    score_min=8 → 界=-5 (与旧硬编码一致); score_min<=6.3 → None (dg 不构成必要条件)。
    """
    need = float(score_min) - _SHORTLIST_OTHER_MAX
    if need <= 0:
        return None
    for bound, pts in ((0.0, 0.5), (-2.0, 1.2), (-5.0, 3.0), (-8.0, 4.0)):
        if need <= pts:
            return bound
    return -8.0    # score_min 超评分理论上限: 按最严档 (判定侧自然全拒)

# 分钟序列标准化已上收 data/hub.py (prep_minutes, D1), 别名引用保持原名
from app.market_cn.auto.core.data.hub import prep_minutes as _prep_minutes  # noqa: E402


def _hhmm(s):
    return str(s)[11:16] if s and len(str(s)) >= 16 else ""


def _is_gem_star(code: str, market=None) -> bool:
    """是否高波动板（创业板/科创板）—— MarketSpec 分板规则, 非代码前缀字面量。"""
    return get_board_type(code, market) == "gem_star"


def _norm_factor(code: str, market=None) -> float:
    """归一化系数: 创/科板×0.5, 主板不变 (与 test_v2_tail_buy.norm_factor 一致)。"""
    return 0.5 if _is_gem_star(code, market) else 1.0


def _limit_pct(code: str, market=None) -> float:
    """名义涨停幅度 (封板判定用, 与历史插件口径一致): 主板 10% / 创科 20%。

    ⚠️ 不要用 MarketSpec.nominal_up_pct (0.098/0.198, 已折 up_tol) —— 那是
    is_limit_up 的判别阈值, 不是名义涨停价幅度; 两者语义不同, 混用会改封板判定。
    2026-09-26 P1-6: 板型判定改走 MarketSpec (等价), 数值常量保留原口径。
    """
    return 0.20 if _is_gem_star(code, market) else 0.10


def _calc_score(day_gain, tail_ret, pos_range, amplitude, pre5_gain, nf):
    """V2 评分系统 — 与 test_v2_tail_buy.calc_score 逐分支一致 (勿单独改动)。"""
    score = 0.0
    dg = day_gain * nf
    if dg <= -8:   score += 4.0
    elif dg <= -5: score += 3.0
    elif dg <= -2: score += 1.2
    elif dg <= 0:  score += 0.5
    tr = tail_ret * nf
    if tr <= -2:   score += 3.0
    elif tr <= -1: score += 2.5
    elif tr <= -0.3: score += 1.5
    if pos_range <= 0.2:  score += 2.0
    elif pos_range <= 0.4: score += 1.0
    if amplitude * nf >= 5 and tr <= -0.3:
        score += 1.0
    p5 = pre5_gain * nf
    if p5 <= -10:  score += 0.3
    elif p5 <= -5: score += 0.1
    return round(score, 2)


def _tail_ret_v2(series_rows):
    """V2 尾盘回落 %: 触发时现价 vs 14:20~14:40 (mi 199~219) 分钟收盘均价。

    快照序列 prep_minutes 差分后按 mi 对齐; 槽位 <15 (21 槽缺口过多) 返回 None。
    返回 float | None (2026-09-26 P1-6 统一; 旧 (value, avg) 元组形态由调用方拆).
    """
    if not series_rows:
        return None
    mins = _prep_minutes(
        [{"time": str(r.get("time") or ""), "open": r.get("open") or 0,
          "high": r.get("high") or 0, "low": r.get("low") or 0,
          "close": r.get("last") or 0, "volume": r.get("volume") or 0}
         for r in series_rows], volume_cumulative=True)
    by_mi = {b["mi"]: float(b["c"]) for b in mins if float(b["c"]) > 0}
    tail = [by_mi[mi] for mi in range(199, 220) if mi in by_mi]
    tail_avg = sum(tail) / len(tail) if len(tail) >= 15 else 0
    last_px = by_mi.get(max(by_mi)) if by_mi else 0
    if not last_px or last_px <= 0 or tail_avg <= 0:
        return None
    return (last_px / tail_avg - 1) * 100


# 2026-10-06: 单继承 StrategyBase —— 折叠契约已并入生产基类，参数合并口径唯一 = params()。
# 折叠契约已并入生产 StrategyBase（单继承）。
@register
class TailOversoldStrategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"
    entry_style = "v2t"
    scan_spec = ScanSpec(kind="intraday_window", windows=("14:50", "15:00"), interval_sec=60,
                         entry_at="14:50")   # 2026-09-26: 14:50 起即可买入 (非仅 15:00 终审)
    default_params = dict(PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近)
    # 契约: 回测未含 U1~U4 / 14:50 起尾盘入场 (T+1) / D1 开盘卖当日平账 / 滚动预览
    use_unified_prefilter = False
    entry_at_close = True
    exit_exec_same_day = True
    signal_state = "buy_today"
    rolling_preview = True
    # 展示阶段表（展示层只按此表呈现，不认识门细节）
    stages = (
        Stage("watch", "候选观察", realtime="14:50-15:00", visible=False),
        Stage("ready", "D0尾盘触发·准备", realtime="09:31"),
        Stage("exec", "D1开盘卖出"),
    )
    data_needs = ("daily", "snapshot", "minute_live")

    def intraday_shortlist(self, snaps, mkt_gain, **params):
        """盘中便宜预筛 (必要条件超集, 免拉全市场序列/日线; 推导见文件头)。

        snaps: {code: latest_snapshot_row} → 通过必要条件的 {code: snap}。
        无市场门控 (V2 规则不含大盘条件, mkt_gain 仅记录不拦截)。

        预筛与判定同源 (2026-09-28 修 A5): 两道界都从 params 推导, 不硬编码标定值 ——
        原实现 amp 界硬编码 10, 判定 2026-09-26 已标定改 8.0 (p["amp_min"]), 导致
        8≤amp*nf<9.85 的合法信号被预筛静默丢弃: 实盘 14:50 滚动扫描 (走本预筛)
        系统性漏信号, 与回测口径分叉。契约仍是"必要条件超集, 宁多勿漏"。
        """
        p = self.params(params or None)
        dg_max = _shortlist_dg_max(float(p["score_min"]))
        amp_floor = float(p["amp_min"]) - _SHORTLIST_SLACK_PCT
        out = {}
        for code, snap in snaps.items():
            if code.startswith(("8", "4", "92")):       # 北交所排除 (v2 回测口径)
                continue
            try:
                last = float(snap.get("last") or 0)
                high = float(snap.get("high") or 0)
                low = float(snap.get("low") or 0)
                pc = float(snap.get("previousClose") or 0)
            except (TypeError, ValueError):
                continue
            if last <= 0 or pc <= 0 or high <= 0 or low <= 0 or high <= low:
                continue
            nf = _norm_factor(code)
            # P1: score>=score_min ⇒ day_gain*nf<=dg_max (由评分表反推, 见 _shortlist_dg_max);
            # P2: amp*nf>=amp_min (盘中只会更差 → 超集);
            # P3: 封板排除 (现价口径, 买不进)
            if dg_max is not None and (low / pc - 1) * 100 * nf > dg_max + _SHORTLIST_SLACK_PCT:
                continue
            if (high - low) / pc * 100 * nf < amp_floor:
                continue
            if last >= round(pc * (1 + _limit_pct(code)), 2) * 0.998:
                continue
            out[code] = snap
        return out

    # ══ 展示层折叠契约：递推状态机 + 门（**门逻辑唯一实现**）═════════════
    # ⚠ 展示/预处理 evaluate 与盘中实时 scan_signals **共用同一份 _gates**。
    #   两入口只差"取哪个快照": scan_signals 判 ctx["latest"]（此刻）；
    #   evaluate 回扫当日 14:50~15:00 序列取首次触发。公式不得再写第二份。
    def init_state(self, code, bars, market=None):
        """seed: 截至昨日的全量历史 → 切片 (win 最近 6 根 close + age/board)。"""
        if len(bars) < _MIN_AGE:
            raise InsufficientHistory(f"{code}: bars={len(bars)} < {_MIN_AGE}")
        win = [{"d": b["time"], "c": float(b["close"])} for b in bars[-_WIN:]]
        return {"v": 1, "date": win[-1]["d"], "age": len(bars),
                "board": get_board_type(code, market), "win": win}

    def step(self, state, bar):
        """推进一根 (O(win))。纯函数: 返回新 state, 不改入参。"""
        win = (list(state["win"])
               + [{"d": bar["time"], "c": float(bar["close"])}])[-_WIN:]
        return {"v": 1, "date": bar["time"], "age": state["age"] + 1,
                "board": state["board"], "win": win}

    def probe(self, state):
        """除权探针: 窗口首尾 (date, close) —— 历史被复权/订正则不等 ⇒ 整票重建。"""
        w = state["win"]
        return [(w[0]["d"], w[0]["c"]), (w[-1]["d"], w[-1]["c"])]

    def evaluate(self, state, inp, prev):
        """预处理/回测/实时共用: 先结算上一阶段, 再判今日触发或明日观察。"""
        p = self.params()
        events = []
        # 上一阶段结算: D1 开盘卖
        if prev is not None and prev.stage == "ready" \
                and inp.bar.get("time", "") > prev.date:
            open_px = float(inp.bar.get("open") or 0)
            entry = float(prev.payload.get("price") or 0)
            if open_px > 0:
                events.append(Progress(
                    stage="exec", date=inp.bar["time"],
                    payload={
                        "entry_date": prev.date, "entry_price": entry,
                        "exit_price": open_px,
                        "exit_ret": round((open_px / entry - 1) * 100, 2) if entry > 0 else None,
                        "label": "D1开盘卖出(超卖反弹兑现)",
                    }, next_realtime=None))
            else:
                return events

        if inp.code.startswith(("8", "4", "92")):
            return events       # 北交所排除 (v2 回测口径)
        win = state["win"]
        if not (state["age"] >= _MIN_AGE and win[-5]["c"] > 0):
            return events
        ctx = inp.ctx or {}
        snap_rows = ctx.get("series") or []
        _tr = ctx.get("_trace")   # 门原因通道（契约约定）：落选门名进 TraceSink，无 sink 零开销
        fired = None
        for i, row in enumerate(snap_rows):
            hh = _hhmm(row.get("time") or "")
            if hh < p["min_hhmm"] or hh > "15:00":
                continue
            fired = self._gates(p, inp.code, state, row, snap_rows[:i + 1],
                                probe=(_tr.note if _tr is not None else None))
            if fired:
                break
        if fired:
            events.append(Progress(
                stage="ready", date=fired["time"],
                payload={"price": fired["price"], "score": fired["score"],
                         "label": fired["label"], "extra": fired["extra"]},
                next_realtime="09:31"))
            return events

        # 明日观察预筛 (superset: 数据够、分母有效即可)
        if state["age"] + 1 >= _MIN_AGE and win[-5]["c"] > 0:
            events.append(Progress(stage="watch", date=inp.bar.get("time", ""),
                                   payload={}, next_realtime="14:50-15:00"))
        return events

    def _gates(self, p, code, state, snap, series, probe=None, market=None):
        """触发门 + 评分 —— **规则单源到 yaml 门表**（GateEvaluator 求门 + build_signal 产 extra）。

        判定/字段口径由 `strategies/tail_oversold.yaml` 的 `gates` + `signal.fields` 单源表达；
        本方法只剩「建 Ctx → 求门 → 组 Signal」的适配职责（§3.1: .py 只提供词, 不改规则）。
        probe 的 stage 名经 `_TO_GATE_STAGE` 映射回旧 taxonomy（离线采样/调试口径不变）。
        state=None 表示切片不可用 (bars 不足) ⇒ `to_data` 门按 data/bars_short 拒。
        """
        from app.market_cn.auto.core.runtime.evaluate import GateEvaluator, build_signal
        from app.market_cn.auto.core.runtime.expr import evaluate as _eval_expr
        from app.market_cn.auto.core.runtime.functions import build_funcs
        spec = _to_spec()
        last = float(snap.get("last") or 0)
        high = float(snap.get("high") or 0)
        low = float(snap.get("low") or 0)
        pc = float(snap.get("previousClose") or 0)
        # 数据合法性守卫（旧实现在任何 probe 之前就返回 ⇒ 保持「无 probe」语义）
        if last <= 0 or pc <= 0 or high <= 0 or low <= 0 or high <= low:
            return None
        closes = [w["c"] for w in (state or {}).get("win") or []]
        board = get_board_type(code, market)
        ctx = Ctx([], 0, lu_idx=0, params=p, board_type=board, code=code, latest=snap,
                  series=series or [], ext={"to_closes": closes}, market=spec.market_spec)
        gates = spec.enabled_gates
        if probe is not None:
            funcs = build_funcs(ctx, spec.key, spec.func_names)
            for g in gates:
                try:
                    passed = bool(_eval_expr(g.expr, p, funcs))
                except Exception:
                    passed = False
                if not passed:
                    probe(_TO_GATE_STAGE.get(g.id, g.id),
                          **_to_probe_kw(g.id, _to_cache(ctx), last))
                    return None
        else:
            ev = GateEvaluator(spec, board_type=board, code=code)
            ok, _failed = ev.evaluate_all([], 0, p, ctx=ctx)
            if not ok:
                return None
        cache = _to_cache(ctx)
        score = float(cache["score"])
        ps = pred_score(score, code)
        tier = "high" if score >= SCORE_HIGH_MIN else "base"
        if probe is not None:
            probe("signal", score=round(score, 2))
        return {
            "time": str(snap.get("time") or "")[:10],
            "price": last, "score": ps,
            "label": (f"尾盘超卖 gain={cache['day_gain']:.1f}% tail={cache['tail_ret']:+.2f}% "
                      f"pos={cache['pos_range']:.2f} 预测分={ps} v2={score:.1f} [{tier}]"),
            "extra": build_signal(ctx, spec),
        }

    def realtime_shortlist(self, codes, snaps, mkt_gain=None, stage=None):
        """实时旁支便宜预筛 (与 scan_signals 门控同源, 只取快照场)。"""
        if stage not in (None, "watch"):
            return list(codes)      # 阶段转换票 (exec 结算) 不得被触发门拦截
        p = self.params()
        dg_max = _shortlist_dg_max(float(p["score_min"]))
        amp_floor = float(p["amp_min"]) - _SHORTLIST_SLACK_PCT
        out = []
        for code in codes:
            if code.startswith(("8", "4", "92")):
                continue
            snap = snaps.get(code) or {}
            last = float(snap.get("last") or 0)
            high = float(snap.get("high") or 0)
            low = float(snap.get("low") or 0)
            pc = float(snap.get("previousClose") or 0)
            if last <= 0 or pc <= 0 or high <= 0 or low <= 0 or high <= low:
                continue
            nf = _norm_factor(code)
            if dg_max is not None and (low / pc - 1) * 100 * nf > dg_max + _SHORTLIST_SLACK_PCT:
                continue
            if (high - low) / pc * 100 * nf < amp_floor:
                continue
            if last >= round(pc * (1 + _limit_pct(code)), 2) * 0.998:
                continue
            out.append(code)
        return out

    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        """盘中判定 (14:50~15:00 滚动, 触发即可买)。必须 ctx={"latest","series"}。

        门逻辑全部委托 `_gates` (与展示层 evaluate 同一实现), 本方法只负责:
        取快照 → 建切片 → 组装 Signal。probe: 门级 TRACE, 仅本入口传。
        """
        p = self.params(params or None)
        ctx = ctx or {}
        snap, series = ctx.get("latest"), ctx.get("series") or []
        if not snap or not series:
            return []
        try:
            state = self.init_state(code, bars or [])
        except InsufficientHistory:
            state = None
        _sink = (ctx or {}).get("_trace")
        fired = self._gates(p, code, state, snap, series, probe=(
            (lambda stage, **kw: (_sink.note if _sink is not None else probe.trace)(
                stage, code=code, d0_date=str(snap.get("time") or "")[:10], **kw))
            if (probe is not None or _sink is not None) else None))
        if not fired:
            return []
        return [Signal(code=code, time=fired["time"], score=fired["score"],
                       price=fired["price"], label=fired["label"],
                       extra=fired["extra"])]

    # ---- 三决策 (14:50 起买入 → 隔夜 → D1 开盘卖) ----
    def entry_decision(self, row, snap=None, **params):
        return EntryDecision(True, "尾盘超卖超短 已入场, 无开盘步骤")

    def confirm_decision(self, row, snap=None, **params):
        """D0 收盘确认: 隔夜持有到 D1 开盘卖。"""
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        entry = float(row.get("entry_price") or 0)
        if not series or entry <= 0:
            return None
        last_px = float(series[-1].get("last") or 0)
        d1_chg = round((last_px / entry - 1) * 100, 2) if last_px > 0 else None
        return ConfirmDecision(True, "hold_to_D1_open", d1_chg=d1_chg)

    def exit_decision(self, row, snap=None, **params):
        """出场: D1 开盘卖。live: entry_date < today → 开盘即标记卖出;
        day_close 重放: 按 D1 开盘价出场 (与回测口径一致)。

        ⚠️ 记账价必须取 `snap["open"]` 而不是 `snap["last"]` (2026-10-04 A3 资金红线):
          本策略是 `entry_at_close` + `exit_exec_same_day` 超短 (D0 14:56买 → D1 开盘卖),
          **整个 alpha 就是隔夜跳空一两个点**; 而 monitor step2 的 live 出场窗起点是
          09:35 (monitor.W_OPEN_HI), 若取 `last` 则记账价 = 09:35 后首拍最新价 ≠ 开盘价,
          对超短策略是系统性偏离。day_close 重放分支取的是 `d1_bar["open"]`,
          两口径必须对齐, 否则实盘与回测逐笔对不上。
          `last` 只作 `open` 缺失时的退路 (停牌/集合竞价未出)。
        """
        if not isinstance(snap, dict):
            return ExitDecision("hold")
        mode = snap.get("mode")
        entry_date = str(row.get("entry_date") or "")[:10]
        if mode == "live":
            today = str(snap.get("today") or "")
            if entry_date and today and entry_date < today:
                px = float(snap.get("open") or snap.get("last") or 0)
                return ExitDecision("exit", reason="D1开盘卖出(超卖反弹兑现)",
                                    price=px if px > 0 else 0)
            return ExitDecision("hold")
        if mode == "day_close":
            bars, entry_idx = snap.get("bars") or [], snap.get("entry_idx")
            if bars and entry_idx is not None and entry_idx + 1 < len(bars):
                d1_bar = bars[entry_idx + 1]
                return ExitDecision("exit", reason="D1开盘卖",
                                    price=float(d1_bar.get("open") or d1_bar.get("close") or 0))
        return ExitDecision("hold")

    def initial_stop(self, code, entry_price):
        return round(entry_price * (1 + self.params()["stop_pct"] / 100), 3)


# ================================================================
# 门表 DSL 私有函数 (2026-09-26 P1-6: 消灭「逐字镜像」, 统一调上方实现)
# ================================================================

def _to_is_gem_star(code, market=None) -> bool:
    return _is_gem_star(code, market)


def to_nf(ctx: Ctx) -> float:
    """归一化系数：高波动板 0.5，其余 1.0（= _norm_factor）。"""
    return _norm_factor(ctx.code, ctx.market)


def _to_limit_pct(code, market=None) -> float:
    return _limit_pct(code, market)


def _to_calc_score(day_gain, tail_ret, pos_range, amplitude, pre5_gain, nf) -> float:
    return _calc_score(day_gain, tail_ret, pos_range, amplitude, pre5_gain, nf)


def _to_tail_ret_v2(series_rows):
    return _tail_ret_v2(series_rows)


def _to_hhmm(s) -> str:
    return _hhmm(s)


def _to_cache(ctx: Ctx) -> dict:
    """tail 盘中判定中间量（每 Ctx 记忆化）。覆盖 scan_signals + 数据助手。

    日线 close 来源: 优先 `ctx.ext["to_closes"]`（`_gates` 注入的 state.win close 序列，
    折叠路径无 bars 也可用）；无则回退 `ctx.bars` 的 close —— 两条来源同源, 值逐位一致。
    """
    cache = ctx.__dict__.get("_to_cache")
    if cache is not None:
        return cache
    snap = ctx.latest or {}
    last = Ctx._f(snap, "last")
    high = Ctx._f(snap, "high")
    low = Ctx._f(snap, "low")
    pc = Ctx._f(snap, "previousClose")
    cache = {"hhmm": _to_hhmm(str(snap.get("time") or "")), "nf": to_nf(ctx),
             "day_gain": float("nan"), "amplitude": float("nan"),
             "pos_range": float("nan"), "tail_ret": float("nan"),
             "pre5_gain": float("nan"), "score": float("nan"), "limit_hit": 0}
    if last > 0 and pc > 0 and high > 0 and low > 0 and high > low:
        cache["limit_hit"] = 1 if last >= round(
            pc * (1 + _to_limit_pct(ctx.code, ctx.market)), 2) * 0.998 else 0
        cache["day_gain"] = (last / pc - 1) * 100
        cache["amplitude"] = (high - low) / pc * 100
        cache["pos_range"] = (last - low) / (high - low)
        tr = _to_tail_ret_v2(ctx.series or [])
        cache["tail_ret"] = float("nan") if tr is None else tr
        ext = ctx.ext or {}
        closes = ext["to_closes"] if "to_closes" in ext \
            else [float(b["close"]) for b in (ctx.bars or [])]
        if len(closes) >= 6 and closes[-5] > 0:
            cache["pre5_gain"] = (last / closes[-5] - 1) * 100
            cache["score"] = _to_calc_score(cache["day_gain"], cache["tail_ret"],
                                            cache["pos_range"], cache["amplitude"],
                                            cache["pre5_gain"], cache["nf"])
    ctx.__dict__["_to_cache"] = cache
    return cache


def to_metric(ctx: Ctx, name: str) -> float:
    """tail 特征取值（缺少/无效 → nan，数值门自然失败）。"""
    return float(_to_cache(ctx).get(name, float("nan")))


def to_hhmm(ctx: Ctx) -> str:
    """触发时刻 HH:MM（'' = 时间串非法）。"""
    return _to_cache(ctx).get("hhmm", "")


def to_limit_hit(ctx: Ctx) -> int:
    """是否封板买不进（1=封板，镜像参考版 last >= 涨停价*0.998）。"""
    return int(_to_cache(ctx).get("limit_hit", 0))


def to_ok(ctx: Ctx, name: str) -> int:
    """该特征是否可用（1=非 nan）—— 镜像参考版 "tail_ret is None / bars 不足 → 早返回"。"""
    v = float(_to_cache(ctx).get(name, float("nan")))
    return 0 if v != v else 1        # nan != nan → 不可用


_SPEC: dict = {}


def _to_spec():
    """tail_oversold 门表 StrategySpec 单例缓存（判定单源到 yaml 门表后 `_gates` 用它求门）。"""
    if "tail_oversold" not in _SPEC:
        from app.market_cn.auto.core.runtime.evaluate import load_strategy
        _SPEC["tail_oversold"] = load_strategy("tail_oversold")
    return _SPEC["tail_oversold"]


#: 门 id → 旧 probe stage 名（保 probe taxonomy 逐字不变；见 sampler.STAGE_RANK["tail_oversold"]）
_TO_GATE_STAGE = {
    "to_window": "window", "to_limit": "limit", "to_data": "data", "to_v2": "v2",
}


def _to_probe_kw(gate_id, cache, last):
    """门 id → 旧 `_gates` 的 probe kwargs（逐字对齐旧口径）。"""
    if gate_id == "to_window":
        return {"hhmm": cache.get("hhmm", "")}
    if gate_id == "to_limit":
        return {"last": round(last, 3)}
    if gate_id == "to_data":
        v = cache["tail_ret"]
        return {"reason": "tail_ret" if v != v else "bars_short"}
    if gate_id == "to_v2":
        return {"score": round(cache["score"], 2),
                "pre5_gain": round(cache["pre5_gain"], 2),
                "amplitude": round(cache["amplitude"], 2),
                "tail_ret": round(cache["tail_ret"], 2),
                "pos_range": round(cache["pos_range"], 3)}
    return {}


def to_v2(ctx: Ctx) -> float:
    """V2 原始评分（宏 `v2_score` 字段来源）。"""
    return float(_to_cache(ctx).get("score", float("nan")))


def to_pred_score(ctx: Ctx) -> int:
    """预测分 0~100（宏 `pred_score` 字段来源; = pred_score 唯一实现）。"""
    return pred_score(float(_to_cache(ctx).get("score", float("nan"))), ctx.code)


def to_pred_exp(ctx: Ctx) -> float:
    """预测次日收益% 经验锚点（宏 `pred_exp_ret` 字段来源）。"""
    return _v2_to_exp_ret(float(_to_cache(ctx).get("score", float("nan"))))


def to_tier(ctx: Ctx) -> str:
    """评分档位 high/base（宏 `tier` 字段来源）。"""
    s = float(_to_cache(ctx).get("score", float("nan")))
    return "high" if s >= SCORE_HIGH_MIN else "base"


register_strategy_funcs(
    'tail_oversold',
    {"feat": to_metric, "hhmm": to_hhmm, "limit_hit": to_limit_hit, "ok": to_ok, "nf": to_nf,
     "v2": to_v2, "pred_score": to_pred_score, "pred_exp": to_pred_exp, "tier": to_tier},
    d0={"nf": 0},
)
