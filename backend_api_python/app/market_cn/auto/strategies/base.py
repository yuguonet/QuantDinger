#!/usr/bin/env python3
"""策略插件契约 (auto/strategies/base.py)

定义策略与框架之间的全部契约: Signal / 三Decision / ScanSpec / StrategyBase。

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

from app.market_cn.auto.core.present.contract import DayInput, Progress
# stateless 折叠的唯一实现在内核；本类只封装，不复制折叠循环（防第二份编排）
from app.market_cn.auto.core.present.runner import fold_range


# ================================================================
# 调度契约 (Phase 3 只识别 daily_close; intraday_window 为分钟策略预留)
# ================================================================
@dataclass
class ScanSpec:
    kind: str = "daily_close"          # daily_close=盘后全市场一次 | intraday_window=盘中窗口轮询
    windows: tuple = ()                # intraday_window 生效: ("09:30","10:00") 等窗口端点
    interval_sec: int = 60             # intraday_window 生效: 窗口内轮询间隔
    data: str = "daily"                # daily=喂日K | minute=喂分钟K (1m 通道 Phase 3+ 接)
    entry_at: str = ""                 # intraday_window 生效: **最早可成交时刻** (= 成交扫描
                                       #   窗口起点)。窗口内按 interval_sec 逐槽轮询, 首个触发
                                       #   即成交 (与生产 rolling_preview "14:50 起即可买入" 同
                                       #   口径); 空 = 从 windows[0] 起扫。⚠ 曾误作"只回该时刻"
                                       #   (2026-10-09 D4 修正: 那会漏掉起点之后才触发的信号)
    # ---- 盘后触发声明 (2026-09-29; scheduler 不再硬编码 17:25) ----
    # after_events: 数据就绪事件依赖 (auto/events.py KNOWN_EVENTS), 全齐才允许触发。
    #   空 = daily_close 默认 ("daily_1d",); 加 lhb 表示必须等龙虎榜落库。
    after_events: tuple = ()
    # fire_at: "HH:MM" 不早于该时刻 (排序/避让用); 空 = 事件齐即可跑。
    #   过 auto/sched.DAILY_FALLBACK_FIRE 后事件未齐也放行 (见 daily_fire_ready)。
    fire_at: str = ""


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


def signal_of_ready(code: str, date, payload) -> Signal:
    """ready 事件 (date + payload) → `Signal` —— **唯一口径**。

    两条消费路径共用本函数, 分叉必漂:
      · `StrategyBase.scan_days` —— 判定入口 (折叠内核回传的 `(k, Progress)`)
      · P5-③ 投影写入器 —— 行源改 `Record` 时读回的 ready 事件
    与 `store.rule_row_core` 是同一件事的两半: 本函数定 Signal 字段, 它定落库列。
    """
    pl = payload or {}
    return Signal(code=code, time=str(date)[:10], score=pl.get("score", 50),
                  price=pl.get("price", 0.0), label=pl.get("label", ""),
                  extra=dict(pl.get("extra") or {}))


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
    d1_chg/d1_vol_r/detail 由策略按自身口径填写 (落库字段, 基准各策略不同)。

    reason 是**策略内部语义串**(ok / g56_hold / sealed_hold / 一整句中文), 只作审计,
    不是展示档位 —— 档位一律经 `core/display_meta.confirm_level_of()` 归一, 勿直取
    reason 当档位。
    """
    confirmed: bool
    reason: str = ""
    d1_chg: float = None
    d1_vol_r: float = None
    detail: dict = None
    exit_price: float = None   # confirmed=False 时可带参考卖出价 (策略按自身口径提供)


# ================================================================
# 预确认档位 → 已迁 core/display_meta.py (2026-09-23, 语义边界拆分)
# ================================================================
# `CONFIRM_LEVELS` / `confirm_level_of` 曾住在本文件。它们只是"判定结果 → 展示档位"的
# 翻译器, 不改判定; 而本文件是**判定契约** (ConfirmDecision / StrategyBase / scan_days)。
# 启动指纹是文件级内容 hash, 切不开同一文件里混着的两类改动 ⇒ 改档位映射会白跑一次全量
# 重建 (实证: 2026-09-23 19:33 因本文件变更触发 rebuild 300s, 产出与 17:01 逐字段相同)。
# 移出到 core/display_meta.py —— 该模块被 startup 判定指纹显式排除。
# 调用方: `from app.market_cn.auto.core.display_meta import confirm_level_of`


# ================================================================
# 数据结束平仓（断链收尾）
# ================================================================
# 断链收尾（末日收盘平仓）的**唯一实现**已收敛到事件流投影：`core/replay.REASON_DATA_END`。
# 本文件曾另有一份同名实现（`data_end_close()` + `DATA_END_REASON`），是旧「通用回测引擎」
# (`_backtest_stock_legacy`) 与五份策略 `backtest_stock` 覆写共用的口径来源；三者已于
# 终态② Step 3+（2026-10-09）退役 ⇒ 一并删除，杜绝第二处口径分叉（护栏见
# tests/golden/test_known_divergence.py）。


def _has_fold_contract(strategy) -> bool:
    """策略是否真正实现了折叠契约（init_state/step/evaluate 非基类 NotImplemented）。

    P3 薄壳分流用。`hasattr` 恒真（基类有定义），必须**试调用**或对 `__func__` 比对：
    基类三个方法体只有 `raise NotImplementedError`，用 `__code__.co_code` 比对不可读，
    改为比对「是否被子类覆写」（`type(s).init_state is not StrategyBase.init_state`）。
    """
    base_cls = type(strategy).__mro__
    for m in ("init_state", "step", "evaluate"):
        f = getattr(type(strategy), m, None)
        if f is None:
            return False
        owner = next((c for c in base_cls if m in c.__dict__), None)
        if owner is None or owner.__name__ == "StrategyBase":
            return False            # 未被任何非基类覆写 = 仍是 NotImplemented
    return True


@dataclass
class ExitDecision:
    """出场判定 (monitor 60s tick / 回测重放): 持仓行今日是否离场"""
    action: str                        # 'hold' | 'exit'
    reason: str = ""
    price: float = 0.0                 # 出场价 (回测/重放需要; 0=未知, 盘中实盘用市价)
    #: 成交价口径 (2026-10-07 加): "" = 按调用方默认 (多数策略 = 收盘价成交);
    #: "open" = **按开盘价成交**。
    #:   用于「昨日封跌停卖不出 ⇒ 今日开盘强平」—— 即旧引擎 pending_dn 顺延的次日
    #:   `b['open']`。这类情形若仍按收盘价成交，会造出物理上不可能的成交
    #:   (实测 break 000506 2026-06-24: 旧引擎 13.2 开盘 vs replay 13.12 收盘)。
    #:   带默认值 ⇒ 不设的策略行为完全不变。
    fill: str = ""


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
      intraday_exit          盘中回测出场回调 (默认 = 次交易日开盘卖; 多日持有策略必须覆盖)
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
    # ---- 折叠契约（本类是唯一实现；展示层只按 core.present.contract
    #      .StrategyProtocol 取用）----
    stages: tuple = ()                  # 展示点声明 (Stage 元组)
    SEED_BARS: int = 200                # 框架统一 seed 根数
    use_unified_prefilter: bool = True
    signal_state: str = "watch_pending"
    entry_at_close: bool = False
    exit_exec_same_day: bool = False
    rolling_preview: bool = False
    data_needs: tuple = ("daily",)     # 数据需求声明 (hub 注入; 当前声明制 Phase 1: 仅元数据)
    # ---- 做T 腿 (可选, T14 骨架 2026-09-26) ----
    # 只在已持仓日生效; 约束由 MarketSpec.intraday_t0 / direction 推出 (core 不特判 A股)。
    # 声明形态: {"enabled": True, "max_legs_per_day": 2, "legs": [{"action":"sell",
    #   "qty_pct":50, "trigger": <callable|已求值bool>, "label":"冲高减半"}, ...]}
    # 复杂网格/依赖成交回报 → 本类方法 t_leg_intents() 逃生舱覆盖。
    #
    # ⚠️ 2026-09-28 修根因：StrategyBase 不是 @dataclass（靠子类类属性覆盖 +
    # 手动 __init__），不能用 dataclass.field()——field() 返回 Field 元对象挂
    # 在类属性上，getattr 返回 truthy Field 绕过 monitor._eval_t_legs L164 的
    # falsy 短路，走到 TLegsConfig.from_dict(Field) 调 .get() → 'Field' object
    # has no attribute 'get'。改用普通默认值 {}：空 dict 是 falsy，L164 短路
    # return []，永远不到 from_dict。子类要启用做T 用 t_legs = {...} 整体覆盖。
    # 同源隐患：scan_spec/default_params 也用了 field()，活跃策略都覆盖了所以
    # 未触发；若新增策略不覆盖会踩同样雷——后续清理建议改普通默认值。
    t_legs: dict = {}

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

        ★ 2026-10-06 目标态: 默认实现 = 内核 **stateless 折叠**
        (`core.present.runner.fold_range`) 的薄封装 —— 本类**不写折叠循环**,
        只做 [lo,hi] 下标换算 + ready 事件 → Signal 组装。折叠序与门实现和
        生命周期分支 (runner/realtime) 同源; 差别由契约声明而非巧合:
        stateless 模式 prev=None ⇒ 不结算、不抑制, 与 scan_signals 天然同口径
        (信号落库口径 —— 去重是消费方/回测侧的选择, 不在门里)。
        覆盖条件: 判定代价高到逐日枚举不可接受时 (全序列指标 / 横截面池), 策略可在
        自己的模块里覆盖本方法做"一次预计算 + 逐日 O(1)" (g56 的做法与实证见
        g56.scan_days)。未迁移折叠契约的策略退化到 `_scan_days_by_signals` ——
        其 scan_signals 本身就是唯一门实现, 不构成第二份逻辑 (2026-10-09 P6-6/7
        退役 relay3/v1/lead_chase 后当前无此类策略)。

        ⚠️ 只枚举 [lo_date, hi_date] 内的日期, 区间外的日期跳过 (不浪费判定)。
        """
        if not bars:
            return []
        i0, i1 = self._day_span(bars, lo_date, hi_date)
        if i1 < i0:
            return []
        try:
            pairs = fold_range(self, code, bars, i0, i1)
        except NotImplementedError:
            return self._scan_days_by_signals(
                bars, code, lo_date=lo_date, hi_date=hi_date, **params)
        out: list = []
        for k, ev in pairs:                    # 内核只回 ready 事件 (stateless)
            out.append(signal_of_ready(code, bars[k]["time"], ev.payload))
        return out

    @staticmethod
    def _day_span(bars, lo_date, hi_date):
        """[lo_date, hi_date] 的下标区间 [i0, i1]（闭区间；空区间返回 i1 < i0）。"""
        i0, i1 = 0, len(bars) - 1
        if lo_date:
            i0 = len(bars)
            for k, b in enumerate(bars):
                if str(b["time"])[:10] >= lo_date:
                    i0 = k
                    break
        if hi_date:
            i1 = -1
            for k in range(len(bars) - 1, -1, -1):
                if str(bars[k]["time"])[:10] <= hi_date:
                    i1 = k
                    break
        return i0, i1

    def _scan_days_by_signals(self, bars, code, *, lo_date=None, hi_date=None, **params):
        """未迁移折叠契约的策略的兜底: 逐日截断 + scan_signals（语义基准）。

        这些策略的 scan_signals 本身就是**唯一**的门实现，走它不是第二份逻辑。
        """
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

    # ---- 回测钩子 ----
    # backtest_stock 见文末「回测入口」段（2026-09-18 P2；2026-10-09 终态② 收敛为事件流
    # 折叠薄壳）：实现折叠契约（init_state/step/evaluate）者走 core.replay，无契约者报错
    # （无兜底引擎）。契约：判定/入场/出场全在策略 evaluate，trades 字段对齐基线 JSON；
    # intraday_window 策略不适用（走 run_all_intraday，其内部委托主干折叠 core.replay）。

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

    def t_leg_intents(self, position, ctx, *, hold_day, spec=None, already=None):
        """做T 腿意图 (T14 骨架) — 默认走 t_legs 声明 + core.t_legs.eval_t_legs。

        返回 list[TradeIntent]。覆盖本方法 = 复杂网格/依赖成交回报的逃生舱。
        只在已持仓日由上层 (monitor/回测时间线) 调用; 本方法不写库、不成交。
        """
        from app.market_cn.auto.core.t_legs import TLegsConfig, eval_t_legs
        cfg = TLegsConfig.from_dict(self.t_legs)
        if cfg is None:
            return []
        return eval_t_legs(position, ctx, cfg, hold_day=hold_day,
                           spec=spec, already=already)

    def intraday_exit(self, bars, code, entry_date, entry_price, entry_idx=None, **params):
        """盘中回测 (intraday_window) 的出场回调 —— 默认 = 次交易日开盘卖。

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

    # ---- 便捷 ----
    def params(self, override=None):
        """default_params ← **策略宏 yaml params** 覆盖 的合并结果。

        优先级 (高→低): override 显式入参 > 实例 default_params (param_scan 网格覆写)
                      > 策略宏 `<key>.yaml` params (回退 config.json params) > 类默认 default_params。

        2026-09-25 bugfix: 原实现只做 default_params+override, **从未读 config**,
        文档却写「← config 覆盖」→ config.params 在回测/monitor 路径静默失效
        (仅 scan 经 params_override 单独注入)。现按文档补齐; 判定「是否仍为类默认」
        以放行 config —— param_scan 把网格写进实例 default_params 后仍保持权威。

        ★ 2026-10-09 (终态②/D4): 覆盖源由 config.json 上移到**策略宏 yaml params**
        —— 参数单一事实源收敛到宏内一处（`strategies.params_override` 已 yaml 优先）。
        无 yaml 的策略仍回退 config（2026-10-09 P6-6/7 退役 lead_chase 后当前无此类），行为不变。

        ★ 2026-10-06: 折叠契约侧原有一份轻合并 `params()` (= 仅 default_params
        + overrides, 不含 config)，与本方法**两个口径并存**。现归一为**本方法**
        (config 感知, 语义更强); 旧名 `params` 删除, 调用点全部改名。
        """
        cls_def = type(self).default_params or {}
        p = dict(self.default_params or {})
        try:
            from app.market_cn.auto.strategies import params_override
            for k, v in (params_override(self.key) or {}).items():
                if (k not in p) or (p.get(k) == cls_def.get(k)):
                    p[k] = v
        except Exception:
            pass
        if override:
            p.update(override)
        return p

    # ================================================================
    # 折叠契约 (= core.present.contract.StrategyProtocol 的实现面)
    #
    # state 语义恒定: 「截至昨日收盘」的切片。内核折叠序恒为
    #     events = strategy.evaluate(state, DayInput(code, bar, ctx), prev)
    #     state  = strategy.step(state, bar)          # evaluate 在 step 之前
    # 展示层 (core/present/runner.py) 只认这六个方法, 不感知策略细节。
    # ================================================================
    def init_state(self, code: str, bars: list[dict]) -> dict:
        """seed：用截至昨日的全量历史 bars 建初始切片（策略自定义 JSON）。"""
        raise NotImplementedError

    def step(self, state: dict, bar: dict) -> dict:
        """每日推进一根（O(1)~O(window)）。纯函数：返回新 state，不改入参。"""
        raise NotImplementedError

    def probe(self, state: dict) -> list[tuple[str, float]]:
        """除权探针：切片里若干「历史某日 (date, close)」锚点，严格相等比对。

        不等 = 历史被复权/订正改写 → 整票重建。默认无锚点（不校验）。
        """
        return []

    def evaluate(self, state: dict, inp: DayInput, prev: Progress | None) -> list[Progress]:
        """返回本日产出的进度事件（0~2 条，按时间序；末条 = 当前进度）。

        - prev.stage == 持仓/待执行阶段时，先结算上一阶段（如 D1 开盘出场）
        - 再判今日是否触发（需 ctx）或预明日观察（watch，纯日线）
        """
        raise NotImplementedError

    def init_shared(self, shared: dict | None) -> None:
        """用持久化的策略级共享状态恢复内部对象（每轮开头调用，幂等）。

        ⚠ 展示层的落盘出口唯一是 StateStore —— 策略文件不得自己开文件写盘。
        """
        return None

    def shared_snapshot(self) -> dict | None:
        """返回需持久化的策略级状态（JSON 可序列化）；None = 无。"""
        return None

    def begin_day(self, date: str, states: dict, bars: dict) -> dict | None:
        """每日折叠前调用一次的跨票聚合（如横截面池）；返回日级上下文。"""
        return None

    def realtime_shortlist(self, codes: list[str], snaps: dict,
                           mkt_gain: float | None = None,
                           stage: str | None = None) -> list[str]:
        """实时旁支的便宜预筛（默认全过）。

        stage = 当前进度阶段 —— 预筛只服务「宽候选集的触发扫描」(watch)；
        已触发票的阶段转换（如 D1 开盘结算）不得被触发门拦截。
        """
        return list(codes)

    # ---- 框架钩子 (monitor 通用流程用; 默认实现 = 旧 else 分支语义) ----
    def quality_key(self, row):
        """开盘窗口质量排序键 (越大越优先)。**消费方 = `monitor.py:245` 开盘名额**。

        默认读 `extra.confirm_chg` (break 口径)。
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
        stop = self.params(None).get("stop", -8.0)
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

        策略有特殊确认规则时覆盖 (如各策略的专属确认逻辑)。
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
        策略有特殊出场 (如龙回头分段追踪) 时覆盖。
        """
        p = self.params(params or None)
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
        # 2026-10-07 P1-④: 止损同样受 T+1 约束 —— 原先只有「追踪」判了 held>1, 止损分支
        #   裸判 ⇒ held=1 (入场当日) 触及止损线就返回 exit, 等于当日买入当日卖出。
        #   与 core/exit_engines._min_sell_day 同口径 (A股: d>=2 才评估出场)。
        if held > 1 and last_bar["low"] <= entry_price * (1 + stop / 100):
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
    # 回测入口 —— 薄壳：只有实现折叠契约的策略可回测（终态②）
    # ================================================================
    # 回测编排已收敛到**事件流折叠**（core.replay），本方法只是它的薄壳（bars → DailyFeed
    # → replay → TradesCollector）；判定/入场/出场全在策略 evaluate（折叠契约），引擎零
    # 第二份编排。
    #
    # 历史（2026-09-18 P2 → 2026-10-09 终态② Step 3+）：本方法曾内置一套**通用回测引擎**
    # (`_backtest_stock_legacy`：as_of 枚举 → scan_signals → U1~U4 → D1 开盘买 →
    # exit_decision 收盘重放)，给「未迁移折叠契约」的遗留策略兜底。那是迁移期的临时第二套
    # 判定+出场编排（违背「一条主干」），已随链 A `run_backtest` 退役一并删除：未实现折叠
    # 契约的策略**不再有回测路径**，调用即报错（明确指向迁移折叠契约），杜绝第二套引擎复活。
    # ================================================================
    def backtest_stock(self, bars, code, stock_info=None, use_prefilter=True,
                       probe=None):
        """回测入口 —— **薄壳**：有折叠契约者走 `core.replay`；无契约者报错。

        分流（保签名、消费方零感知）：
          - kind != daily_close ⇒ None（盘中策略走 run_all_intraday，其内部委托主干折叠）
          - 未实现折叠契约 ⇒ `NotImplementedError`（**无兜底引擎**，见上；当前无此类策略）
          - 已实现 ⇒ replay；横截面策略（有 prewarm 声明，如 g56）经 DailyFeed.ctx_provider
            注入一次全市场池（薄壳单票不走 begin_day）

        ⚠ `probe` / `use_prefilter` / `stock_info` 形参仅为**签名兼容**（消费方零感知）：采样
        已迁 sampler.LiveSampler 实盘侧；U1~U4 预筛由策略 `evaluate` 自持（与 break/dragon
        现状一致），replay 路径不再在引擎侧叠加。
        """
        if self.scan_spec.kind != "daily_close":
            return None
        if not _has_fold_contract(self):
            raise NotImplementedError(
                f"{self.key}: 未实现折叠契约（init_state/step/evaluate），不支持回测 —— "
                f"回测编排已收敛到事件流折叠（core.replay）单源，不再提供通用兜底引擎。"
                f"如需回测，请先为该策略迁移折叠契约。")
        from app.market_cn.auto.core.replay import (
            DailyFeed, replay, TradesCollector)
        if not bars or len(bars) < 30:
            return []
        coll = TradesCollector(code=code, strategy=self.key)
        if hasattr(self, "prewarm"):
            # 横截面策略: 单票回测无 begin_day ⇒ 池按**终点锚**惰性建一次
            # (与旧 g56 回测钩子同式; _ensure_pool_daily 结果含全部历史日键,
            #  ctx_provider 每日注入同一池 —— 逐日重建会全市场×N)
            from app.market_cn.auto.core.features.cross_section import (
                _ensure_pool_daily)
            _pool = _ensure_pool_daily(str(bars[-1]["time"])[:10])
            feed = DailyFeed(
                bars, ctx_provider=lambda i, b: {"_day": {"pool": _pool}})
        else:
            feed = DailyFeed(bars)
        res = replay(self, code, feed, collectors=[coll])
        return res.trades or []

