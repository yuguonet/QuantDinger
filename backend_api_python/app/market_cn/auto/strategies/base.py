#!/usr/bin/env python3
"""策略插件契约 (auto/strategies/base.py) —— Phase 1 骨架, 2026-09-07

定义策略与框架之间的全部契约: Signal / 三Decision / ScanSpec / StrategyBase。
Phase 1 仅为契约定义 (运行时扫描/监控仍在旧模块, Phase 3 切换注册表分发)。

核心原则: 策略=纯函数, 输入输出规范化。
  - 输入: bars(list[dict] 前复权升序) + code + as_of(as-of索引, 回测禁用其后数据) + ctx(预计算缓存)
  - 输出: Signal dataclass (核心字段标准化 + extra 开放字典兜策略特有字段)
  - 策略禁止 import DB/HTTP/其它策略; data/ 是全框架唯一 IO 层

易错点:
  - Signal.score 恒为 0~100 整数, 策略不得输出原始分/百分比;
  - exit_decision 的入参是 store 持仓行 row (dict), 不是 Signal —— 出场天然依赖持仓状态;
  - 阈值敏感性/两段稳定性验证是策略改动的既定流程 (AGENTS.md), 本契约不替代。
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ================================================================
# 调度契约 (Phase 3 只识别 daily_close; intraday_window 为分钟策略预留)
# ================================================================
@dataclass
class ScanSpec:
    kind: str = "daily_close"          # daily_close=盘后全市场一次 | intraday_window=盘中窗口轮询
    windows: tuple = ()                # intraday_window 生效: ("09:30","10:00") 等窗口端点
    interval_sec: int = 60             # intraday_window 生效: 窗口内轮询间隔
    data: str = "daily"                # daily=喂日K | minute=喂分钟K (1m 通道 Phase 3+ 接)
    entry_at: str = ""                 # intraday_window 生效: 成交触发时刻 ("14:56"=终审语义,
                                       #   窗口内其它触发仅预览不成交; 空=每个触发点均可成交)


# ================================================================
# 标准化输出
# ================================================================
@dataclass
class Signal:
    """策略产出的信号 (纯判定结果, 不含 state/entry_price 等生命周期字段 —— 状态机归框架)"""
    code: str
    time: str                          # 信号日 YYYY-MM-DD
    side: str = "signal"               # signal|buy|sell (扫描产出恒为 signal)
    score: int = 50                    # 信号强度 0~100 (框架默认50, 策略可覆盖)
    price: float = 0.0                 # 信号日收盘价 (0=未知)
    label: str = ""                    # 展示文案, 如 "低吸,score75"
    extra: dict = field(default_factory=dict)   # 策略特有字段 (gap_from_peak/lu_date/tech_score...)


@dataclass
class EntryDecision:
    """D1 开盘处置判定 (monitor ~09:25): 该持仓行今日是否可买"""
    buyable: bool
    reason: str = ""


@dataclass
class ConfirmDecision:
    """收盘确认判定 (monitor 15:00): watch_pending → holding / exit_today

    confirmed=False 且 reason 非空 → monitor 转 exit_today (exit_reason=reason);
    策略可返回 None 表示"无法判定"(快照缺失等), monitor 不做状态转移。
    d1_chg/d1_vol_r/detail 由策略按自身口径填写 (落库字段, 基准各策略不同:
    多数用 signal_price, relay3 用 entry_price)。

    reason 是**策略内部语义串**(ok / g56_hold / sealed_hold / 一整句中文), 只作审计,
    不是展示档位 —— 档位一律经 `core/display_meta.confirm_level_of()` 归一, 勿直取
    reason 当档位。
    """
    confirmed: bool
    reason: str = ""
    d1_chg: float = None
    d1_vol_r: float = None
    detail: dict = None
    exit_price: float = None   # confirmed=False 时可带参考卖出价 (relay3 未封板尾盘卖=最新价)


# ================================================================
# 预确认档位 → 已迁 core/display_meta.py (2026-09-23, 语义边界拆分)
# ================================================================
# `CONFIRM_LEVELS` / `confirm_level_of` 曾住在本文件。它们只是"判定结果 → 展示档位"的
# 翻译器, 不改判定; 而本文件是**判定契约** (ConfirmDecision / StrategyBase / scan_days)。
# 启动指纹是文件级内容 hash, 切不开同一文件里混着的两类改动 ⇒ 改档位映射会白跑一次全量
# 重建 (实证: 2026-09-23 19:33 因本文件变更触发 rebuild 300s, 产出与 17:01 逐字段相同)。
# 移出到 core/display_meta.py —— 该模块被 startup 判定指纹显式排除。
# 调用方: `from app.market_cn.auto.core.display_meta import confirm_level_of`


@dataclass
class ExitDecision:
    """出场判定 (monitor 60s tick / 回测重放): 持仓行今日是否离场"""
    action: str                        # 'hold' | 'exit'
    reason: str = ""
    price: float = 0.0                 # 出场价 (回测/重放需要; 0=未知, 盘中实盘用市价)


# ================================================================
# 策略基类
# ================================================================
class StrategyBase:
    """策略插件基类 —— 子类覆盖类属性 + 实现判定方法, 模块内 @register 注册。

    类属性:
      key               注册键 (= qd_dragon_signals.strategy / config.json 键)
      name              中文展示名
      prefilter_anchor  U1~U4 锚定日: 'signal'=信号日(末根bar) | 'limit_up'=最近涨停日
      scan_spec         调度契约 (默认盘后一次)
      default_params    策略参数默认值 (config.json strategies.<key>.params 可覆盖)
      use_unified_prefilter  扫描层是否做 U1~U4 统一预过滤 (默认 True; 回测未含 U1~U4 的策略设 False)
      signal_state      扫描落库初始状态 (默认 watch_pending; 盘中即买策略设 buy_today)
      entry_at_close    入场在尾盘/收盘 (T+1 当日不可卖, monitor 止损守卫跳过当日; 默认 False)
      exit_exec_same_day     出场当日执行并当日平账 (默认 False=次日开盘执行)
      intraday_shortlist     kind=intraday_window 策略需实现: 仅用最新快照的便宜预筛, 返回 {code: snap}
      intraday_exit          时间线引擎出场回调 (默认 = 次交易日开盘卖; 多日持有策略必须覆盖)
      rolling_preview        True=窗口起点起每分钟滚动预览 (run_scan_knife 循环调用, 每轮
                             清理本轮落选的 buy_today 行), 14:56 终审 (默认 False=仅终审一次)
      data_needs             数据需求声明 (D1, §3.4): ('daily','minute_live','quote','lhb',...);
                             框架/hub 按声明加载, 未被任何策略声明的通道零加载 (拔插), 默认 ('daily',)
    """

    key: str = ""
    name: str = ""
    prefilter_anchor: str = "signal"
    entry_style: str = "a"             # qd_dragon_signals.entry_style (同策略多形态时区分)
    family: str = ""                   # 版本链 family 根 (空=自身 key; 如 break_v2→"break",
                                       #  扫描展示归一按 (code,family,style) 去重; config 可覆盖)
    family_version: int = 1            # 链内版本号 (同族重叠取最高版本, 加高版本自动识别)
    scan_spec: ScanSpec = field(default_factory=ScanSpec)
    default_params: dict = field(default_factory=dict)
    use_unified_prefilter: bool = True
    signal_state: str = "watch_pending"
    entry_at_close: bool = False
    exit_exec_same_day: bool = False
    rolling_preview: bool = False
    data_needs: tuple = ("daily",)     # 数据需求声明 (hub 注入; 当前声明制 Phase 1: 仅元数据)

    # ---- 信号判定 (回测即信号: 实盘 as_of=None 只判末根bar; 回测 as_of=k 判第k根) ----
    def scan_signals(self, bars, code, *, as_of=None, ctx=None, **params):
        """返回 list[Signal]。as_of=None 与现状 *_today_d0_signals 语义一致;
        ctx 为 BacktestCtx 预计算缓存 (涨停表/指标序列), 策略优先从 ctx 取。"""
        raise NotImplementedError

    # ---- 窗口内逐日信号 (重建/回测的统一批量契约) ----
    def scan_days(self, bars, code, *, lo_date=None, hi_date=None, **params):
        """返回 [lo_date, hi_date] 区间内**所有**命中日的 list[Signal]。

        统一契约的意义: 编排层 (rebuild) 对所有策略一视同仁地调这一个方法, 不因某个
        策略"内部贵"就给它单独开一条路径 —— 那会让编排层长出策略专属分支。

        默认实现 = 逐日截断 + scan_signals (语义基准, 所有策略行为的定义)。
        覆盖条件: 判定代价高到逐日枚举不可接受时 (全序列指标 / 横截面池), 策略可在
        自己的模块里覆盖本方法做"一次预计算 + 逐日 O(1)", 但**必须保证与逐日调用
        scan_signals 逐位一致** (g56 的做法与实证见 g56.scan_days)。

        ⚠️ 只枚举 [lo_date, hi_date] 内的日期, 区间外的日期跳过 (不浪费判定)。
        """
        if not bars:
            return []
        out = []
        for k in range(len(bars)):
            d = str(bars[k]["time"])[:10]
            if lo_date and d < lo_date:
                continue
            if hi_date and d > hi_date:
                break                      # 日期升序, 可提前退出
            out.extend(self.scan_signals(bars[:k + 1], code, **params))
        return out

    # ---- 三决策 (入参 row = qd_dragon_signals 持仓行 dict; snap = 当日快照可选) ----
    # 2026-09-18 P1: entry/confirm/exit 均有智能默认实现 (见类尾), 按需覆盖;
    # 仅 scan_signals 保持抽象必填。

    # ---- 回测钩子 (2026-09-10 插件化: 新策略实现本钩子即入回测流水线, backtest.py 零改动) ----
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                      probe=None):
        """单股全历史日线枚举回测 → trades 列表; 默认 None = 无日线枚举回测。

        契约:
          - 与实盘同一份 scan_signals (as_of 切片语义), 出场引擎 lazy import backtest.py
            (插件先加载也不成环: backtest.py 顶层只 import 插件常量, 引擎调用发生在运行期);
          - trades 字段与基线 JSON 对齐 (entry_date/entry_price/return_pct/exit_day/...);
          - 枚举内的去重/预过滤锚点/D1过滤属策略规则, 写在插件内, 编排层 (backtest.run_all)
            只做全市场循环与统计。
        盘中窗口策略 (tail/knife) 不实现, 走各自验证脚本。
        """
        return None

    def day_prefilter(self, frame, pc_map):
        """日级必要条件超集预筛 (intraday_window 回测提速, 2026-09-10; 默认 None=不预筛)。

        契约 (dragon 预筛同款方法论: 预筛必须是数学必要条件超集):
          - 入参 frame (MinuteFrame, 含 day_extremes() 通用窗口统计) + pc_map;
          - 返回 None = 不预筛 (全市场照旧); 返回 code 集合 = 引擎只在该子集内
            建快照/跑 shortlist/判定; 返回空集合 = 整日跳过;
          - **宁可多留不可误杀**: 被排除的 code 必须在所有触发槽位都不可能通过
            intraday_shortlist; 判定只能用 frame 通用统计 (日高/日低/首开), 不得
            引入 slot 级信息 (槽位快照此刻尚未构建);
          - 与实盘无交互 (实盘全市场快照本就现成, 无需此钩子), 回测专用。
        """
        return None

    def intraday_exit(self, bars, code, entry_date, entry_price, entry_idx=None, **params):
        """时间线引擎 (intraday_window 回测) 的出场回调 —— 默认 = 次交易日开盘卖。

        默认实现与 tail/knife 基线口径逐字一致 (D1 开盘卖, exit_day=1); 返回 None =
        视野不足 (该笔不计入)。**多日持有策略必须覆盖** (如龙回头 15 日追踪/止损/逃顶),
        否则回测收益口径会与其实盘出场引擎不符 —— 这会反向污染阈值敏感性结论。
        返回 dict(exit_date/exit_price/exit_day/exit_reason/return_pct[, peak_return_pct])。
        """
        nxt = next((b for b in bars if str(b["time"])[:10] > str(entry_date)[:10]), None)
        if nxt is None or float(nxt["open"]) <= 0:
            return None
        exit_price = float(nxt["open"])
        return {"exit_date": str(nxt["time"])[:10], "exit_price": round(exit_price, 3),
                "exit_day": 1, "exit_reason": "d1_open",
                "return_pct": round((exit_price / entry_price - 1) * 100, 2)}

    def intraday_replay(self, bars, entry_idx, entry_price, *, code, board_type,
                        minute_by_date, params=None):
        """**1m 真实腿出场重放** (P3b/P4, 2026-09-21) —— 默认 None = 该策略暂不支持。

        与 `intraday_exit` 的分工: 后者是 intraday_window 类策略的"入场后一次性出场判定";
        本钩子服务**日线策略走 1m 通道** —— 策略把自己的**日线出场引擎**在给定分钟序列上
        重放, 只把成交时点从收盘换成盘中 (日内先后可知 → 消除"low 触线但不知
        先跌穿后收回 / 先冲高后跌穿"的结构性失真)。

        契约:
          - `bars` = 日线序列, `entry_idx` = 入场日索引 (与 `backtest_stock` 同一语义);
          - `minute_by_date` = `{date: [槽位, ...]}`, 槽位含 `o/h/l/c` 或 `open/high/low/close`;
            **调用方 (P4 引擎) 保证整笔持仓窗口覆盖一致** —— 要么整段有 1m, 要么不传;
          - 返回 `dict(exit_price, exit_day, return_pct[, peak_return_pct, ...])` 或 None;
          - **同一笔交易只用一档口径**, 禁止半段 1m / 半段日线 (口径混合会污染阈值结论)。
        """
        return None

    # ---- 探针 sample 组装 (debug 模式专用; probe=None 路径不会走到) ----
    def _probe_day(self, probe, day_tr, bars, i, code, stock_info,
                   stage=None, sig=None, u_fails=None, extra=None):
        """按决策日产出一行 sample (特征/标签共用 sample_feats, 通用组装件)。

        stage=None 时取 day_tr 中 PROBE_STAGE_RANK 最深的判定步做 day 级归属。
        """
        from app.market_cn.auto.probe import sample_feats
        if stage is None:
            rank = getattr(self, "PROBE_STAGE_RANK", {})
            stage = max((t["stage"] for t in day_tr.items),
                        key=lambda s: rank.get(s, 0), default="no_gate")
        rec = {"code": code, "d0_date": str(bars[i]["time"])[:10], "stage": stage,
               "rule_trace": day_tr.items if day_tr is not None else [],
               **sample_feats(bars, i, code, stock_info)}
        if sig is not None:
            rec["sig"] = sig
        if u_fails is not None:
            rec["u_fails"] = list(u_fails)
        if extra:
            rec.update(extra)
        probe.sample(**rec)

    # ---- 便捷 ----
    def merged_params(self, override=None):
        """default_params ← config.json params 覆盖 的合并结果。"""
        p = dict(self.default_params)
        if override:
            p.update(override)
        return p

    # ---- 框架钩子 (monitor 通用流程用; 默认实现 = 旧 else 分支语义) ----
    def quality_key(self, row):
        """开盘窗口质量排序键 (越大越优先)。**消费方 = `monitor.py:245` 开盘名额**。

        默认读 `extra.confirm_chg` (break/relay3 口径)。
        ⚠ 2026-09-24 核查: **全集群只有 break(22处) 与 relay3(1处) 产出 confirm_chg**,
          其余策略若不 override 本方法 ⇒ 恒 `(0,)` ⇒ 名额排序**退化为入库顺序**。
          当前未 override 的: knife_catch / tail_oversold (均 intraday_window, 库内无
          signals 行, 暂无实害); dragon_callback / dragon_v2 / g56 / v1 / relay3 已 override。
        ⚠ 另一坑: **须与 `scan.py:254` 的入库截断键同源于 `Signal.score`**, 否则会像 g56
          那样出现"入库用新键/开盘名额用旧键"的两套口径并存 (g56 已于 09-24 修正)。
          新策略建议直接 `return (row.get("score") or 0,)`。
        """
        extra = row.get("extra") or {}
        return (extra.get("confirm_chg") or 0,)

    def initial_stop(self, code, entry_price):
        """入场止损价 (update_stop_price 落库)。默认取 params.stop (-8%, 板块不分档)。"""
        stop = self.merged_params(None).get("stop", -8.0)
        return round(entry_price * (1 + float(stop) / 100), 3)

    # ================================================================
    # 智能默认 (2026-09-18 P1): 新策略最小面 = key/name + scan_signals + 出场参数。
    # 以下全部可覆盖; 存量策略均已 override, 默认实现对其零影响。
    # 默认消费的 params 键 (config.json params 可覆盖):
    #   stop=-8.0 出场止损% / trail=-4.0 追踪止损%(自入场日峰值, held>1) / hold=7 到期天
    #   min_gap_main=-8.0 max_gap_main=11.0 主板竞价 gap 带上/下限 (回测 D1 口径)
    #   min_gap_gem=-10.0 max_gap_gem=20.5 创业板/科创板 gap 带
    # ================================================================
    def entry_decision(self, row, snap=None, **params):
        """通用默认: D1 竞价 gap 带判定 (gap=(open/prev_close-1)*100)。

        prev_close 取快照 previousClose 兜底 signal_price; 板块分主/创带。
        策略有特殊竞价规则时覆盖 (如 v1 的主板高开 3~5% 回避带——可用参数复现)。
        """
        from app.market_cn.auto.core.market import get_board_type
        p = self.merged_params(params or None)
        if not snap:
            return EntryDecision(False, "无竞价快照")
        open_px = float(snap.get("open") or snap.get("last") or 0)
        if open_px <= 0:
            return EntryDecision(False, "开盘价缺失")
        prev_close = float(snap.get("previousClose") or row.get("signal_price") or 0)
        if prev_close <= 0:
            return EntryDecision(False, "昨收缺失")
        gap = (open_px / prev_close - 1) * 100
        if get_board_type(row.get("code", "")) == "gem_star":
            ok = p.get("min_gap_gem", -10.0) <= gap < p.get("max_gap_gem", 20.5)
        else:
            ok = (p.get("min_gap_main", -8.0) <= gap
                  and gap < p.get("max_gap_main", 11.0))
        if ok:
            return EntryDecision(True, f"gap={gap:.2f}% 可买")
        return EntryDecision(False, f"gap={gap:.2f}% 越界")

    def confirm_decision(self, row, snap=None, **params):
        """通用默认: D1 收盘确认通过 (d1_chg 按 signal_price 基准)。

        策略有特殊确认规则时覆盖 (如 v1 日内动量 / relay3 封板确认)。
        返回 None = 无法判定 (无快照), monitor 不转移。
        """
        series = (snap or {}).get("series") if isinstance(snap, dict) else None
        if not series:
            return None
        last = float((series[-1] or {}).get("last") or 0)
        base = float(row.get("signal_price") or 0)
        d1_chg = (last / base - 1) * 100 if last > 0 and base > 0 else None
        return ConfirmDecision(True, "ok",
                               d1_chg=round(d1_chg, 2) if d1_chg is not None else None)

    def exit_decision(self, row, snap=None, **params):
        """通用默认: 收盘重放出场引擎 (止损 stop% / 追踪 trail% / 到期 hold 天)。

        v1.exit_decision 的逐字通用化 (2026-09-18): snap={"mode":"day_close",
        "bars":[...], "entry_idx":int}; live 盘中模式返回 hold (硬止损兜底在 monitor)。
        策略有特殊出场 (如龙回头分段追踪 / relay3 尾盘未封板卖) 时覆盖。
        """
        p = self.merged_params(params or None)
        stop = p.get("stop", -8.0)
        trail = p.get("trail", -4.0)
        hold = p.get("hold", 7)
        if not isinstance(snap, dict) or snap.get("mode") != "day_close":
            return ExitDecision("hold")
        bars = snap.get("bars")
        entry_idx = snap.get("entry_idx")
        entry_price = float(row.get("entry_price") or 0)
        if bars is None or entry_idx is None or entry_price <= 0:
            return ExitDecision("hold")
        today_idx = len(bars) - 1
        held = today_idx - entry_idx + 1
        peak = max(float(b["high"]) for b in bars[entry_idx:today_idx + 1])
        last_bar = bars[-1]
        if last_bar["low"] <= entry_price * (1 + stop / 100):
            return ExitDecision("exit", reason=f"止损{stop}%",
                                price=entry_price * (1 + stop / 100))
        if held > 1 and last_bar["low"] <= peak * (1 + trail / 100):
            return ExitDecision("exit", reason=f"追踪止损{trail}%",
                                price=peak * (1 + trail / 100))
        if held >= hold:
            return ExitDecision("exit", reason=f"持仓到期{hold}天",
                                price=float(last_bar["close"]))
        return ExitDecision("hold")

    # ================================================================
    # 通用回测引擎 (2026-09-18 P2): 新策略零回测代码的默认 backtest_stock。
    # 路径: as_of 枚举 → scan_signals → U1~U4 → D1 开盘买(gap带) →
    #       exit_decision 收盘重放出场 (与实盘同一出场路径)。
    # 口径说明:
    #   - 出场填价=决策价 (收盘重放口径), 与 v1 回测专用引擎的"次日开盘"口径不同
    #     —— 新策略无基线对数负担, 以实盘同路径为准;
    #   - D1 gap 带用 params (min_gap_*/max_gap_*) — 若策略覆盖 entry_decision
    #     改了竞价规则, 须同步 params 或自写 backtest_stock (引擎会告警);
    #   - 持仓期内新信号跳过 (平仓次一日起可再入场);
    #   - intraday_window 策略不适用 (返回 None, 走 run_all_intraday 时间线引擎)。
    # ================================================================
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                       probe=None):
        if self.scan_spec.kind != "daily_close":
            return None
        from app.market_cn.auto.core.filters import unified_prefilter
        from app.market_cn.auto.core.market import get_board_type
        p = self.merged_params(None)
        n = len(bars)
        if n < 30:
            return []
        trades = []
        last_exit_idx = -1
        for i in range(25, n - 1):
            if i <= last_exit_idx:          # 持仓去重
                continue
            sigs = self.scan_signals(bars[:i + 1], code, stock_info=stock_info)
            if not sigs:
                continue
            sig = sigs[0]
            if use_prefilter and self.use_unified_prefilter:
                ok, _fails = unified_prefilter(bars, i, code, stock_info)
                if not ok:
                    continue
            d0, d1 = bars[i], bars[i + 1]
            entry_price = float(d1["open"] or 0)
            if entry_price <= 0:
                continue
            entry_idx = i + 1
            d1_gap = (entry_price / float(d0["close"]) - 1) * 100
            d1_change = (float(d1["close"]) / float(d0["close"]) - 1) * 100
            if get_board_type(code) == "gem_star":
                if not (p.get("min_gap_gem", -10.0) <= d1_gap
                        < p.get("max_gap_gem", 20.5)):
                    continue
            else:
                if not (p.get("min_gap_main", -8.0) <= d1_gap
                        < p.get("max_gap_main", 11.0)):
                    continue
            # 出场: exit_decision 收盘重放 (与实盘同一出场路径)
            row0 = {"code": code, "entry_price": entry_price,
                    "signal_price": float(d0["close"]), "extra": {}}
            exit_idx = exit_price = None
            exit_reason = ""
            for j in range(entry_idx, n):
                snap = {"mode": "day_close", "bars": bars[:j + 1],
                        "entry_idx": entry_idx}
                d = self.exit_decision(row0, snap=snap)
                if d is not None and getattr(d, "action", "") == "exit":
                    exit_idx = j
                    exit_price = float(d.price or 0)
                    exit_reason = d.reason
                    break
            if exit_idx is None:             # 数据结束未触发 → 末日收盘平仓
                exit_idx, exit_price, exit_reason = n - 1, float(bars[n - 1]["close"]), "数据结束平仓"
            if exit_price <= 0:
                continue
            last_exit_idx = exit_idx
            peak = max(float(b["high"]) for b in bars[entry_idx:exit_idx + 1])
            trades.append({
                "code": code, "strategy": self.key, "path": self.key,
                "d0_date": str(d0["time"])[:10], "d0_close": float(d0["close"]),
                "score": int(sig.score), "label": sig.label,
                "entry_date": str(d1["time"])[:10],
                "entry_price": round(entry_price, 3), "buy_mode": "next_open",
                "d1_change": round(d1_change, 2), "d1_gap": round(d1_gap, 2),
                "exit_date": str(bars[exit_idx]["time"])[:10],
                "exit_price": round(exit_price, 3),
                "exit_day": exit_idx - entry_idx + 1,
                "exit_reason": exit_reason,
                "return_pct": round((exit_price / entry_price - 1) * 100, 2),
                "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
            })
        return trades
