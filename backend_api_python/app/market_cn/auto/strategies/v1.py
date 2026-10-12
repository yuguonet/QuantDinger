"""strategies/v1.py — V1 追板策略 (StrategyBase 插件实现)

定位: 抓**可买真首板**后的连板 (1进2), 与龙回头(连板龙头回调)/断板(涨停开板)错开赛道。

入场 (D0 盘后扫描 → D1 竞价) — 2026-10-10 数据驱动重设, 7 门:
  ① D0 涨停(0.98x阈值); ② 非一字可买(D0开盘<涨停价, 排除一字/秒板消息股);
  ③ 真首板(前 prior_lu_free_days=10 日无涨停, 不与龙回头/断板重合);
  ④ 小盘(流通市值≤float_mv_max); ⑤ 弱趋势(ret_20d≥min, 放宽自30);
  ⑥ D-1 横盘[d_1_band_min,max) (替代旧深回调[-10,-3)); ⑦ 温和量比[vol_ratio_min,max)。
  竞价 (entry_decision): gap_min_high(8%) ≤ gap < 板块涨停价(可买) —— 高开强势连板
  U1~U4: prefilter_anchor='signal' (D0 即涨停日, 锚定信号日评估)

出场 (1进2 设计, monitor v1 分支):
  进二日(D1=入场日)收盘相对首板收盘(D0)**未达涨停** ⇒ D2 开盘清仓 (T+1: 入场当日不可卖);
  连板(D1 涨停)则持有, 由 止损-10% / 追踪-5%(自入场日峰值, held>1) / 到期7天 出场。

易错点:
  - D0 涨停判定是 0.98x 板块阈值 (近似涨停); 非一字门看 D0 **开盘**是否到涨停价;
  - ⚠ D2 清仓的判定锚是「D1 收盘 vs D0 收盘 是否涨停」(is_limit_up), 不是"D1 日内动量<3%"
    旧启发式 —— 旧式会误清一字板(开=收=涨停, 动量≈0)且误持高开非涨停(动量≥3%),
    与"第2天没涨停就跑"的 1进2 设计不符; 已由 `_pre_exit` 钩子改为 is_limit_up 判定。
  - 实证依据见 tmp/v1_board_buyable_study.py (可买真首板内部: 小盘/D-1横盘/温和量比/非烂板
    提 1进2; 旧回调/OBV/深回调/ret_20d>30 反是最差桶)。一字板连板率49%但 D1 买不进=镜花水月。
"""
from __future__ import annotations

from app.market_cn.auto.core.exec import fill_blocked_by_limit_up
from app.market_cn.auto.core.market import (
    get_board_type, is_limit_up, limit_up_price,
)
from app.market_cn.auto.strategies import register
from app.market_cn.auto.strategies.base import (
    ConfirmDecision, EntryDecision, ExitDecision, ScanSpec, Signal, StrategyBase,
)

STRATEGY_KEY = "v1"
STRATEGY_LABEL = "V1"

PARAMS = {
    # === 入场辨识度 (2026-10-10 数据驱动重设: 从"回调换手板"转向"可买真首板") ===
    # 实证(tmp/v1_board_buyable_study.py, 全市场120天): 可买真首板(非一字+前期无涨停)
    # 内部基线 连板率12.89%/均收-0.28%; 提升1进2的因子=小盘/D-1横盘/温和量比/非烂板;
    # 旧因子(D-1回调[-10,-3)、ret_20d>30、OBV、D-1非放量)反而是最差桶 ⇒ 移除/放宽。
    "prior_lu_free_days": 10,     # 真首板: 前 N 日内无涨停 (不与龙回头/断板重合)
    "ret_20d_min": 10.0,          # 弱趋势门槛 (原30; 实证>30 反伤 1进2)
    "float_mv_max": 200.0,        # 流通市值上限(亿) (实证 >500亿 连板仅5.6%)
    "only_main": False,           # 提单位优势: True=只做主板(实证 主板高开≥8% +1.93%/连板71% ≫ 创业板 +0.16%)
    "d_1_band_min": -3.0,         # D-1 横盘下限% (替代旧回调[-10,-3); 深回调是最差桶)
    "d_1_band_max": 3.0,          # D-1 横盘上限%
    "vol_ratio_min": 1.0,         # D0 量比下限 (排除缩量<1 逆向选择: 好的D1买不进)
    "vol_ratio_max": 5.0,         # D0 量比上限 (排除爆量>5 出货)
    # === 出场 (1进2 设计, monitor v1 分支) ===
    "stop": -10.0,
    "trail": -5.0,
    "hold": 7,
    # 出场模式 (2026-10-10): True=让利润奔跑(去 D2 一刀切, trail/stop/expiry 出场, 破峰回撤才走);
    #   False=原 1进2(非连板 D2 开盘清仓)。实证: 120天+2.59%/全年+1.58% 均靠高开隔夜延续大肉,
    #   但 D2 一刀切把大赢家砍在 D2 ⇒ 让利润奔跑留住更多大肉。
    "let_profits_run": True,
    # === 竞价 gap (entry_decision; 2026-10-10 高开强势连板 + 逆向选择修正) ===
    # 实证: 高开强势连板, 但⚠**逆向选择**——好票多 D1一字(买不进, +8%), 可买高开是挑剩的柠檬。
    #   甜区用 gap_min_high=8 (可买 <涨停价)。⚠ 试过「创 gap 抬到[12,16)」(单因子研究示创12-16
    #   最佳) ⇒ 全 V1 反而 57→32 笔/均收 1.93→-0.59% 变差: 单因子最佳≠组合最佳, D0门+出场会翻盘。
    #   故 gap 不分板(统一 8), 勿再单因子调 gap。低平[-3,3)/中开(3,8) 负EV死区。
    "gap_min_high": 8.0,         # 高开线下限% (甜区 [8, 涨停价))
    # ⚠ 前视开关 (2026-09-30): D1 收盘涨跌(d1_change<0)过滤 D1 开盘入场 = 未来信息。
    #   默认 False (=实盘口径); True 仅用于复现旧回测口径做对照。
    "require_d1_close_up": False,
}


@register
class V1Strategy(StrategyBase):
    key = STRATEGY_KEY
    name = STRATEGY_LABEL
    prefilter_anchor = "signal"        # D0 即涨停日, U1~U4 锚定信号日评估
    entry_style = "v1"
    scan_spec = ScanSpec(kind="daily_close", after_events=("daily_1d", "lhb"))
    default_params = dict(PARAMS)
    # 探针 day-stage 归属 (越靠后=离信号越近)

    # ---- 信号判定 ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, probe=None, **params):
        """D0 四因子 → Signal (至多1笔)。as_of=k: 只用 bars[:k+1], 末根为 D0。

        probe: 调试探针 (probe.Probe / DayTrace shim), None=零开销 —
        各过滤门 TRACE 式打点, 存档供 AI 离线分析, 与判定行为无关。"""
        p = self.params(params or None)
        if as_of is not None:
            bars = bars[:as_of + 1]
        result = []
        n = len(bars)
        if n < 26:
            return result
        i = n - 1
        if i < 2:
            return result
        board_type = get_board_type(code)
        # ★ 2026-10-11 N-2 收敛: 原 `threshold = 0.098 if main else 0.198` 内联阈值已删——
        #   涨停幅度是**市场事实**, 统一走 `market.is_limit_up`（检测）与
        #   `market.limit_up_price` + `exec.fill_blocked_by_limit_up`（可买性, 唯一实现）。
        #   现值语义逐位不变 (门0 = 旧 threshold*0.98 = is_limit_up 的 up_eff;
        #   门1/竞价 = 旧 gap>=threshold = 严格达到名义涨停价才拒)。
        # === 门0: 板块过滤 (only_main=True 只做主板 —— 实证主板高开单位优势 ≫ 创业板) ===
        if p.get("only_main") and board_type != "main":
            return result
        d0 = bars[i]
        d_1 = bars[i - 1]
        d_2 = bars[i - 2]
        if d_2["close"] <= 0 or d_1["close"] <= 0:
            return result
        # 探针 shim (TRACE 宏语义): trace 统一分发（ctx["_trace"].note 优先 /
        # probe.trace 兼容）⇒ M1 采样可不依赖 probe 对象（影子对拍: test_m1_sampler）
        _sink = (ctx or {}).get("_trace")
        if probe is not None or _sink is not None:
            _pd = str(d0["time"])[:10]

            def _tr(stage, **kw):
                (_sink.note if _sink is not None else probe.trace)(
                    stage, code=code, d0_date=_pd, **kw)
        else:
            _tr = None

        if not is_limit_up(d0["close"], d_1["close"], board_type):
            if _tr:
                _tr("lu", d0_pct_chg=round((d0["close"] / d_1["close"] - 1) * 100, 2))
            return result

        # === 门1: 非一字可买 —— D0 开盘 < 涨停价 (排除一字/开盘即封消息股, D1 才买得进) ===
        open_pct = (d0["open"] / d_1["close"] - 1) * 100
        if fill_blocked_by_limit_up(d0["open"], limit_up_price(d_1["close"], board_type)):
            if _tr:
                _tr("buyable", open_pct=round(open_pct, 2))
            return result

        # === 门2: 真首板 —— 前 prior_lu_free_days 日内无涨停 (不与龙回头/断板重合) ===
        for j in range(max(1, i - p["prior_lu_free_days"]), i):
            if is_limit_up(bars[j]["close"], bars[j - 1]["close"], board_type):
                if _tr:
                    _tr("first_board", prior_lu_at=str(bars[j]["time"])[:10])
                return result

        # === 门3: 小盘 —— 流通市值 ≤ float_mv_max (实证大盘连板率骤降) ===
        circ = float((params.get("stock_info") or {}).get("circ_shares") or 0)
        total = float((params.get("stock_info") or {}).get("total_shares") or 0)
        d0_close = round(d0["close"], 3)
        if circ > 0:
            circ_mv = circ * d0["close"] / 1e8
            if circ_mv > p["float_mv_max"]:
                if _tr:
                    _tr("mkt_cap", circ_mv=round(circ_mv, 0))
                return result

        # === 门4: 弱趋势 —— ret_20d ≥ ret_20d_min (放宽; 实证 >30 反伤 1进2) ===
        if i < 20 or bars[i - 20]["close"] <= 0:
            return result
        ret_20d = (d0["close"] / bars[i - 20]["close"] - 1) * 100
        if ret_20d < p["ret_20d_min"]:
            if _tr:
                _tr("ret20", ret_20d=round(ret_20d, 2))
            return result

        # === 门5: D-1 横盘 [d_1_band_min, d_1_band_max) (替代旧深回调[-10,-3), 深回调最差桶) ===
        d_1_change = (d_1["close"] / d_2["close"] - 1) * 100
        if d_1_change < p["d_1_band_min"] or d_1_change >= p["d_1_band_max"]:
            if _tr:
                _tr("d1_band", d_1_change=round(d_1_change, 2))
            return result

        # === 门6: 温和量比 [vol_ratio_min, vol_ratio_max) (排除缩量<1 逆向选择 + 爆量>5 出货) ===
        vol_ratio = None
        if i >= 6:
            vol_ma5 = sum(bars[j]["volume"] for j in range(i - 5, i)) / 5
            if vol_ma5 > 0:
                vol_ratio = d0["volume"] / vol_ma5
                if vol_ratio < p["vol_ratio_min"] or vol_ratio >= p["vol_ratio_max"]:
                    if _tr:
                        _tr("vol_ratio", vol_ratio=round(vol_ratio, 2))
                    return result

        if _tr:
            _tr("signal", ret_20d=round(ret_20d, 2), d_1_change=round(d_1_change, 2),
                vol_ratio=round(vol_ratio, 2) if vol_ratio is not None else None,
                circ_mv=round(circ * d0["close"] / 1e8, 0) if circ > 0 else None)
        result.append(Signal(
            code=code,
            time=d0["time"],
            score=int(min(99, max(0, ret_20d))),
            price=d0_close,
            label="V1",
            extra={
                "path": "v1",
                "path_label": "V1",
                "d0_date": d0["time"],
                "d0_close": d0_close,
                "ret_20d": round(ret_20d, 2),
                "d_1_change": round(d_1_change, 2),
                "turnover_anchor": round(d0["volume"] / circ * 100, 2) if circ > 0 else None,
                "turnover_anchor_total": round(d0["volume"] / total * 100, 2) if total > 0 else None,
                "buy_mode": "next_open",
            },
        ))
        return result

    # ---- D1 竞价处置 (monitor ~09:25; monitor/rebuild 均调本方法 = 入场单一事实源) ----
    def entry_decision(self, row, snap=None, **params):
        """v1 竞价规则 (2026-10-10 高开强势连板): gap_min_high ≤ gap < 板块涨停价(可买)。

        gap = (open/prev_close-1)*100, prev_close 取快照 previousClose 兜底 signal_price。
        实证(tmp/v1_d1_gap_study.py): D1 高开≥8% 连板61%/+1.49% (唯一正EV, 主板71%/+1.93%);
          低平[-3,3) 仅6%连板/负EV、中开(3,8) 亦负EV死区 ⇒ 只买高开, 不收低平。
        可买性: gap < 板块涨停阈值*100 ⇒ 排除一字D1(开盘即封, 买不进)。
        """
        p = self.params(params or None)
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = (open_px / prev_close - 1) * 100
        gap_min_high = float(p.get("gap_min_high", 8.0))
        board_type = get_board_type(row.get("code", ""))
        # ★ 2026-10-11 N-2: 涨停可买性统一走唯一实现 (旧 threshold 内联已删, 语义不变)
        if gap < gap_min_high:
            return EntryDecision(False, f"gap={gap:.2f}%<高开线{gap_min_high}%")
        if fill_blocked_by_limit_up(open_px, limit_up_price(prev_close, board_type)):
            return EntryDecision(False, f"gap={gap:.2f}% 一字开盘不可买")
        return EntryDecision(True, f"gap={gap:.2f}% 高开可买")

    # ---- 15:00 收盘确认 ----
    def confirm_decision(self, row, snap=None, **params):
        """v1 收盘确认。let_profits_run=True(2026-10-10 让利润奔跑): 一律确认持有, 不 D2 一刀切,
        交给 exit_decision 的 trail/stop/expiry(与回测 _run_backtest(let_profits_run=True) 同口径)。

        let_profits_run=False(1进2 设计): 进二日(D1)收盘相对首板收盘(D0)未达涨停 ⇒ weak ⇒ D2 开盘清仓。
        snap={"series":[...当日快照序列]}; last_r["last"] = D1 收盘, row.signal_price = D0 收盘。
        返回 None = 无法判定(无快照), monitor 不转移。
        """
        p = self.params(params or None)
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        last_r = series[-1]
        prev_close = float(row.get("signal_price") or 0)
        if prev_close <= 0:
            return None
        d1_close = float(last_r["last"] or 0)
        d1_chg = (d1_close / prev_close - 1) * 100
        board_type = get_board_type(row.get("code", ""))
        # let_profits_run: 不 D2 一刀切, 一律确认持有(让利润奔跑) —— 交 exit_decision 的 trail/stop/expiry
        if p.get("let_profits_run", False):
            return ConfirmDecision(True, "ok", d1_chg=round(d1_chg, 2),
                                   detail={"confirm": "ok", "let_profits_run": True})
        # 1进2 设计: D1 未涨停 → weak → D2 清仓 (is_limit_up 判连板, 勿用"D1日内动量<3%"启发式)
        if not is_limit_up(d1_close, prev_close, board_type):
            return ConfirmDecision(False, "D1未涨停,D2开盘清仓",
                                   d1_chg=round(d1_chg, 2), detail={"confirm": "weak"})
        return ConfirmDecision(True, "ok", d1_chg=round(d1_chg, 2),
                               detail={"confirm": "ok", "confirm_strong": False})

    def quality_key(self, row):
        """V1 质量排序: 20日涨幅越大越优先。"""
        extra = row.get("extra") or {}
        return (extra.get("ret_20d") or 0,)

    def initial_stop(self, code, entry_price):
        """-10% (板块不分档)。"""
        return round(entry_price * (1 - 10.0 / 100), 3)

    # ---- 出场判定 ----
    def exit_decision(self, row, snap=None, **params):
        """收盘重放: 止损-10 / 追踪-5(自入场日峰值, held>1) / 到期7天。

        snap={"mode":"day_close","bars":[...],"entry_idx":int}; live 模式无特殊规则 → hold
        (盘中硬止损兜底在 monitor 主循环)。"""
        p = self.params(params or None)
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        stop, trail, hold = p["stop"], p["trail"], p["hold"]
        today_idx = len(bars) - 1
        held = today_idx - entry_idx + 1
        entry_seg = bars[entry_idx:today_idx + 1]
        peak = max(float(b["high"]) for b in entry_seg)
        last_bar = bars[-1]
        # 2026-10-07 P1-④ (与 base.exit_decision 同口径): 止损补 T+1 守卫 —— 原裸判,
        #   held=1 (入场当日) 触及止损线即卖, 违反 A股 T+1。
        if held > 1 and last_bar["low"] <= entry_price * (1 + stop / 100):
            return ExitDecision("exit", reason=f"止损{stop}%", price=entry_price * (1 + stop / 100))
        if held > 1 and last_bar["low"] <= peak * (1 + trail / 100):
            return ExitDecision("exit", reason=f"追踪止损{trail}%", price=peak * (1 + trail / 100))
        if held >= hold:
            return ExitDecision("exit", reason=f"持仓到期{hold}天", price=float(last_bar["close"]))
        return ExitDecision("hold")

    # ---- CLI 回测入口覆写 (2026-10-10 恢复 V1 经官方 CLI 回测) ----
    # 背景: 2026-10-09 终态② Step 3 退役了 base 的通用兜底引擎 `_backtest_stock_legacy`
    #   (scan_signals→U1~U4→D1 开盘买→exit_decision 收盘重放); 此后未迁移折叠契约的策略
    #   该回测入口直接 NotImplementedError。V1 当时未迁折叠契约 ⇒ 官方 CLI
    #   (tools.replay / core.backtest) 调 V1 即断。
    # 本覆写判定/决策/出场零改: 复用 V1 既有 scan_signals + entry_decision(竞价 gate) +
    #   _run_backtest(出场引擎, 含 D1 动量<3%→D2 开盘清仓), 与单测 harness 逐笔验证的
    #   V1 自有链路同源。仅为 V1 自持的回测包装 —— 不引入第二套通用引擎 (base 已明确
    #   不为非折叠策略提供兜底), 也不动任何信号/决策/出场逻辑。
    #   2026-10-11 口径对齐: 信号池接入 U1~U4 (use_prefilter=True 缺省开, 与实盘 scan
    #   行构建同层同参)。此前回测池比实盘宽 —— U2 换手≥3% 实盘滤、回测不滤 (10-09
    #   实证: 判定 9 票, 实盘 U1~U4 后落库 5), 胜率数字对实盘信号池失真; 门③(市值)两侧
    #   本就生效 (run_all 传单票 stock_info, 此处透传给 scan_signals)。
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True, probe=None):
        """V1 日线回测 (daily_close 类) — 经官方 CLI 跑通。

        枚举每个候选 D0 (as_of) → 竞价 gate(相对 D0 收盘的开盘 gap) → D1 开盘买入 →
        _run_backtest 出场(止损-10 / 追踪-5 / 到期 7 / D1 未涨停→D2 开盘清仓, 均受 T+1 约束)。
        持仓期内跳过新信号 (不重叠建仓)。出场参数取自 self.params() (yaml 事实源)。
        use_prefilter=True (缺省) 时信号池过 U1~U4, 与实盘 scan 同口径 (锚=D0 截断窗
        末根, 与生产 "bars 止于 target" 同形); False 供研究侧对照宽池。
        """
        if self.scan_spec.kind != "daily_close":
            return None
        if not bars or len(bars) < 30:
            return []
        p = self.params()
        hold, stop, trail = p["hold"], p["stop"], p["trail"]
        trades = []
        n = len(bars)
        busy_until = -1  # 最近一次持仓终点索引, 防重叠建仓
        for as_of in range(25, n - 7):
            if as_of <= busy_until:
                continue
            sigs = self.scan_signals(bars, code, as_of=as_of, stock_info=stock_info) or []
            if not sigs:
                continue
            if use_prefilter:
                # 惰性导入避免 strategies → scan 的模块环; 锚=截断窗末根 (=D0)。
                from app.market_cn.auto.scan import apply_unified_prefilter
                sigs, _ = apply_unified_prefilter(sigs, bars[:as_of + 1], code,
                                                  stock_info, self)
                if not sigs:
                    continue
            sig = sigs[0]
            entry_idx = as_of + 1
            if entry_idx >= n:
                continue
            d1 = bars[entry_idx]
            entry_price = float(d1["open"])
            # 竞价 gate: D1 开盘相对 D0 收盘的 gap
            # entry_decision/confirm_decision 的 row 约定为 dict (signal_price/extra),
            # 由 scan_signals 产出的 Signal 转换而来 (与单测 harness / monitor 同口径)
            row = {"code": code, "signal_price": float(bars[as_of]["close"]),
                   "extra": dict(sig.extra or {})}
            snap_entry = {"open": entry_price, "last": entry_price,
                          "previousClose": float(bars[as_of]["close"])}
            ed = self.entry_decision(row, snap_entry)
            if not ed.buyable:
                continue
            board_type = get_board_type(code)
            res = _run_backtest(bars, entry_idx, entry_price, hold_days=hold,
                                stop_loss=stop, trailing_stop=trail,
                                board_type=board_type, is_v1=True,
                                let_profits_run=p.get("let_profits_run", False))
            if res is None:
                continue
            busy_until = entry_idx + res["exit_day"]   # 占用至出场日, 防重叠
            trades.append(self._v1_trade_dict(sig, code, bars, as_of,
                                              entry_idx, entry_price, res))
        return trades

    @staticmethod
    def _v1_trade_dict(sig, code, bars, as_of, entry_idx, entry_price, res):
        exit_day = res["exit_day"]
        exit_price = res["exit_price"]
        # run_trail_stop with_reason 默认 False ⇒ 不返回 exit_reason, 此处按 V1 规则推断
        if exit_day == 2:
            reason = "D1未涨停→D2开盘清仓"
        elif exit_price <= entry_price * (1 + res.get("stop_loss", -10.0) / 100) + 1e-9:
            reason = f"止损{res.get('stop_loss', -10.0)}%"
        else:
            reason = f"持仓到期{exit_day}天"
        return {
            "code": code,
            "strategy": "v1",
            "signal_date": str(bars[as_of]["time"])[:10],
            "entry_date": str(bars[entry_idx]["time"])[:10],
            "entry_price": entry_price,
            "exit_day": exit_day,
            "exit_price": exit_price,
            "exit_reason": reason,
            "return_pct": round(res["return_pct"], 3),
            "peak_return_pct": round(res.get("peak_return_pct") or 0, 3),
            "hold_days": exit_day - 1,
            "d0_close": round(float(bars[as_of]["close"]), 3),
            "ret_20d": (sig.extra or {}).get("ret_20d"),
            "d_1_change": (sig.extra or {}).get("d_1_change"),
        }

    # ---- 回测钩子 (2026-09-10 自 backtest.backtest_v1_stock 逐字搬入, 对数零差异) ----


# ================================================================
# 出场模拟 (2026-09-10 晚自 backtest.py 下沉回归本文件 — 出场规则是策略专用,
# 通用流水线不承载策略专属出场; 逐字搬运, 回归以三策略对数验证)
# ★ 2026-10-11 死 import 清理 (pyflakes): 原顶层导入的 fill_blocked_by_limit_dn /
#   fill_on_gap / is_one_word_limit_dn / _limit_dn_price 从未被本文件引用——
#   出场成交由出场引擎内部消费 exec 原语, 本文件不需要顶层导入, 已删。
# ================================================================


def _run_backtest(bars, entry_idx, entry_price, hold_days=7, stop_loss=-10.0, trailing_stop=-8.0, board_type="main", peak_exit=False, is_v1=False, d1_limit_up=None, d1_change=None, d1_gap=None, let_profits_run=False):
    """V1/通用出场 — 2026-09-26 P1-7: 骨架上收 core.exit_engines.run_trail_stop。

    现实约束 (T+1 / 跳空 / 跌停顺延) 已在引擎内按 MarketSpec 处理:
      spec.intraday_t0=false → 最早 d=2 卖 (A股); true → d=1 (HK/US)。
    本函数只保留 V1 专属:
      - d1_limit_up 判定 (若调用方未传入);
      - D1 未涨停(收盘未达涨停) → D2 开盘清仓 (pre_exit 钩子, 经 is_limit_up)。
    let_profits_run=True (2026-10-10 让利润奔跑): 去掉 D2 一刀切, 由 run_trail_stop 的
      追踪止损/到期出场 (破峰回撤 trail% 才走 ≈ "破分时均线才走"的日线代理) —— 留住大肉、
      砍弱票。False=原 1进2 (非连板 D2 开盘清仓)。
    """
    if entry_price <= 0 or entry_idx >= len(bars):
        return None
    from app.market_cn.auto.core.exit_engines import run_trail_stop

    # d1_limit_up: 若外部未传入则在回测内计算 (兼容旧调用)。
    #   ⚠ 锚是「进二日(D1=入场日)收盘 相对 首板收盘(D0) 是否涨停」= 1进2 设计口径。
    #   旧实现误用 bars[entry_idx+1](=D2) 相对 D1 开盘, 既错 bar 又错基准 ⇒ 修。
    if d1_limit_up is None:
        d1_limit_up = False
        if entry_idx > 0 and entry_idx < len(bars):
            d1_close = float(bars[entry_idx]["close"])
            d0_close = float(bars[entry_idx - 1]["close"])
            if d0_close > 0 and is_limit_up(d1_close, d0_close, board_type):
                d1_limit_up = True

    def _pre_exit(d, b, peak, peak_prev):
        # let_profits_run: 无 D2 一刀切 —— 交给 run_trail_stop 的 trail/stop/expiry 出场(让利润跑)
        if let_profits_run:
            return None
        # V1 出场 (1进2 设计): 进二日(D1=入场日)收盘相对首板收盘(D0)未达涨停 ⇒ 非连板
        #   ⇒ D2 开盘清仓 (T+1: 入场当日不可卖)。与"D1 日内动量<3%"旧启发式脱钩 —— 旧式会
        #   误清一字板(开=收=涨停, 动量≈0)且误持高开非涨停(动量≥3%), 与"没涨停就跑"设计不符。
        if not (is_v1 and d == 2):
            return None
        if d1_change is not None and d1_gap is not None:
            # 供给路径(diag): 用 D1收盘/D0收盘 比值经 is_limit_up 判连板 (与扫描同口径)
            _d0 = 100.0
            _d1c = _d0 * (1 + d1_change / 100.0)
            return "open" if not is_limit_up(_d1c, _d0, board_type) else None
        if entry_idx <= 0 or entry_idx >= len(bars):
            return None
        d1_close = float(bars[entry_idx]["close"])
        d0_close = float(bars[entry_idx - 1]["close"])
        if d0_close <= 0:
            return None
        return "open" if not is_limit_up(d1_close, d0_close, board_type) else None

    result = run_trail_stop(
        bars, entry_idx, entry_price,
        hold_days=hold_days, stop_loss=stop_loss, trailing_stop=trailing_stop,
        board_type=board_type, peak_exit=peak_exit,
        pre_exit=_pre_exit if is_v1 else None,
    )
    if result is not None and d1_limit_up:
        result["d1_limit_up"] = d1_limit_up
    return result


# ---- exit_modes 注册 (2026-09-26 P1-9 层反转) ----
def _exit_v1_combo(bars, entry_idx, entry_price, *, code, board_type, params, diag):
    """V1 combo 出场 (止损/追踪/到期 + D1 动量清仓) — 供 YAML exit.mode=v1_combo。"""
    return _run_backtest(
        bars, entry_idx, entry_price,
        params["hold"], params["stop"], params["trail"], board_type,
        is_v1=True,
        d1_limit_up=diag.get("d1_limit_up"),
        d1_change=diag.get("d1_change"),
        d1_gap=diag.get("d1_gap"),
    )


from app.market_cn.auto.core.exit_modes import register_exit as _register_exit
_register_exit("v1_combo", _exit_v1_combo)


# ================================================================
# 门表回测编排已退役 (2026-10-09 终态② Step 3)
# ----------------------------------------------------------------
# 2026-09-28: 原 core/runtime/evaluate.py 的门表回测编排曾**纯搬运**回本模块自注册
#   (register_day_flow("v1", _backtest_day_flow))。
# 2026-10-09: 回测主路径收敛到事件流折叠 (薄壳 → core.replay), 通用兜底引擎已删除 ——
#   薄壳对非折叠契约策略直接 NotImplementedError。v1 无折叠契约 ⇒ 标准回测入口对其关闭;
#   V1 自有链路走 scan_signals + entry_decision + _run_backtest (v1_combo 出场引擎)。
#   要接入生产 core.replay 链路须先迁移折叠契约 (init_state/step/evaluate)。
# ================================================================

