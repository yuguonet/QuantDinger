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
    """
    confirmed: bool
    reason: str = ""
    d1_chg: float = None
    d1_vol_r: float = None
    detail: dict = None
    exit_price: float = None   # confirmed=False 时可带参考卖出价 (relay3 未封板尾盘卖=最新价)


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

    # ---- 三决策 (入参 row = qd_dragon_signals 持仓行 dict; snap = 当日快照可选) ----
    def entry_decision(self, row, snap=None, **params) -> EntryDecision:
        raise NotImplementedError

    def confirm_decision(self, row, snap=None, **params) -> ConfirmDecision:
        raise NotImplementedError

    def exit_decision(self, row, snap=None, **params) -> ExitDecision:
        """snap=None: 盘中实时模式; snap=日K重放模式 (回测/盘后复盘复用同一路径)。"""
        raise NotImplementedError

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
        """开盘窗口质量排序键 (越大越优先)。默认 confirm_chg (break/relay3 旧口径)。"""
        extra = row.get("extra") or {}
        return (extra.get("confirm_chg") or 0,)

    def initial_stop(self, code, entry_price):
        """入场止损价 (update_stop_price 落库)。默认 -8% (板块不分档)。"""
        return round(entry_price * (1 - 8.0 / 100), 3)
