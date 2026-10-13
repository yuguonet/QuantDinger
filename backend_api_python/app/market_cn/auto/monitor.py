"""monitor.py (原 dragon_monitor.py) — 自动策略组盘中状态机 (60s tick, scheduler 调度)

Phase 3: 策略判定全部经 strategies 注册表分发, 本文件不含策略名分支:
  开盘窗口: entry_decision (gap 过滤) + quality_key (排名) + daily_limit (名额)
  盘中:     stop_price 硬止损 (通用) + exit_decision live 模式 (策略盘中出场)
  15:00:    confirm_decision (dragon/break 无确认直持仓; 各策略确认由宏/插件决定)
  14:50:    exit_decision day_close 模式 (收盘重放, 与回测同一路径)
各策略入场/确认/出场规则详见 strategies/*.py 模块头注释。
卖出执行: 次日开盘按开盘价记账 closed 并出组 (建议人工尾盘/次日开盘执行)。

与回测的已知差异: 回测"尾盘卖"按当日收盘成交; 自动化在 14:50 提示、
未执行者次日开盘记账。盘中追踪止损不做分钟级模拟 (日线粒度)。

易错点 (2026-09-28 修 A3): run_monitor 每 tick 开头批量取行快照 (buy_rows/hold_rows),
step2/3/4/5 共用同一份 —— step2 与 step3 (14:25~14:45)、step4 (14:50~15:00) 时间窗重叠。
对快照行的所有 set_state 必须带条件守卫 (expect_state=快照态 / only_unexited),
否则 step2 刚做出的出场会被旧快照回滚或被确认结果覆盖, 出场价/原因丢失后
该行永不回归出场态。出场标记 (exit_reason) 一经写入即资金事实, 任何写入不得覆盖。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from app.utils.logger import get_logger

logger = get_logger(__name__)

try:
    from dotenv import load_dotenv
    for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '.env'),
               os.path.join(os.getcwd(), '.env')):
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

from app.market_cn.auto import store as ds
from app.market_cn.auto import strategies as strat_reg
from app.market_cn.auto.core.exec import fill_blocked_by_limit_up
from app.market_cn.auto.core.market import get_board_type, limit_up_price
# 展示档位归一 (非判定): 单独模块, 不进判定指纹 —— 见 core/display_meta.py 头注
from app.market_cn.auto.core.display_meta import confirm_level_of

W_OPEN_LO, W_OPEN_HI = "09:25", "09:35"
W_PRECONF_LO, W_PRECONF_HI = "14:25", "14:45"
W_CLOSESIM_LO, W_CLOSESIM_HI = "14:50", "15:06"   # 缺省收盘出场窗起点; 策略可经 meta.close_sim_at 覆盖
W_CONFIRM_LO = "15:01"


def _close_sim_start(s_obj) -> str:
    """策略的收盘出场重放起点: yaml meta.close_sim_at 优先, 缺省 W_CLOSESIM_LO。

    (2026-10-12 D-A: 收盘窗 per-strategy 维度 —— 原全局常量让不同策略的收盘
     出场窗被迫统一; 现策略可在自己的 yaml meta.close_sim_at 声明起点。)
    """
    try:
        from app.market_cn.auto.strategies import _yaml_doc
        key = getattr(s_obj, "key", "")
        doc, _ = _yaml_doc(key)
        v = ((doc or {}).get("meta") or {}).get("close_sim_at")
        return str(v) if v else W_CLOSESIM_LO
    except Exception:
        return W_CLOSESIM_LO


def _now_hm() -> str:
    # L-13 根因收口: 盘中窗口锚是北京时间, 必须显式 TZ_CN, 不依赖宿主时区
    # (与 store.today_str 同源, 避免 UTC 运行时窗口判错)。
    return datetime.now(ds.TZ_CN).strftime("%H:%M")


def _today() -> str:
    """本 tick 的"今天" (YYYY-MM-DD) —— **委托 store.today_str(), 单一事实源**。

    A7 (2026-10-05): 原先这里自己调 `datetime.now(TZ_CN)` 而 store 侧用 SQL
    `CURRENT_DATE`(DB 会话是 UTC, 见 db_postgres.py:144 的
    `options="-c timezone=UTC"`) ⇒ 两套时钟。北京时间 00:00~07:59 时二者不在同一天,
    影响 exit_today 平账 (出口 B 判 `marked >= today`) 与日期窗口的边界。
    现统一到 store.today_str()。

    ⚠️ 原遗留隐患 `snapshot_day_done()` 的 `CURRENT_DATE` 时钟已于 2026-10-10
      (审计 B-7) 统一到本函数 —— 两套时钟收口完成。
    """
    return ds.today_str()


#: 每 tick 拉取的候选集回看窗口 —— 来自 store.VISIBLE_WINDOW_DAYS (**不要**在本地重定义,
#: 否则"展示层可见 / monitor 可扫"两个窗口会各自漂移, A5 复现)。
_VISIBLE_DAYS = ds.VISIBLE_WINDOW_DAYS


def _last_trade_day() -> str:
    """最近一个**已完成**交易日 (= 本 tick 的 target 日期线)。

    ⚠ 2026-10-07 (P2): 原实现裸 `except` **静默**回退 `_today()` —— 交易日历读不出
      来 (文件缺失 / 格式变更 / 假期表损坏) 时:
        target = 今天 ⇒ 上一交易日入库的 watch_pending `trade_date < target` 全部判
        "隔日未处理"当场 expired ⇒ 开盘窗口候选集 (trade_date == target) 为空 ⇒
        **全天信号作废, 开盘零买入, 且全程无一行告警** —— 一次文件故障 = 一天哑火,
        事后无法复盘。
      现: 失败必打 WARNING (带原因与回退值), 语义仍是回退 today —— 监控是长跑进程,
      宁可保守继续跑也不停机; 但**不再是暗账**。
    """
    try:
        from app.utils.trading_calendar import last_finish_trading_day
        ref_dt = datetime.now(ds.TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        return last_finish_trading_day(ref_dt=ref_dt)
    except Exception as e:
        logger.warning("[dragon_monitor] 交易日历读取失败(%s), 回退 today=%s "
                       "—— 若 today 非交易日, watch_pending 会被误判隔日过期",
                       e, _today())
        return _today()


def in_window(lo, hi, hm=None):
    """按北京时间 HH:MM 判定盘中窗口；显式传入的 hm 也必须是北京时间。"""
    hm = hm or _now_hm()
    return lo <= hm < hi


# ================================================================
# 快照读取 (market DB, realtime_snapshot 单表)
# ================================================================

# P1 2026-10-12: 收口 hub 单源 (原 monitor.py 本地副本已删)
from app.market_cn.auto.core.data.hub import _snapshot_pool  # noqa: F401

# 2026-09-26 由按年分表改为单表
_SNAPSHOT_TABLE = "realtime_snapshot"


def snapshot_day_done() -> bool:
    """当日快照是否已落齐 (MAX(time) ≥ 15:00)。

    ★ 2026-10-10 审计 B-7 修复: 原 `time::date = CURRENT_DATE` —— DB 会话是 UTC
      (db_postgres.py:144 `options="-c timezone=UTC"`), 而 time 列存的是**北京时间**
      本地戳 (realtime_snapshot.py 以 `datetime.now(TZ_CN)` 写入) ⇒ 北京 00:00~07:59
      两者不在同一天, 15:01 确认闸门误判。现统一到 `_today()` (store.today_str,
      与 A7 时钟收口同一事实源), 参数化传入, 顺带消除 SQL 注入面。
    """
    try:
        pool = _snapshot_pool()
        today = _today()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(time) FROM \"{_SNAPSHOT_TABLE}\" "
                            "WHERE time::date = %s", (today,))
                r = cur.fetchone()
        if not r or r[0] is None:
            return False
        return r[0].strftime("%H:%M") >= "15:00"
    except Exception:
        return False


def fetch_day_snapshots(codes):
    """当日快照序列 {code: [rows]} —— D1 起委托 data/hub (唯一实现, 口径逐字一致)。"""
    from app.market_cn.auto.core.data.hub import day_series
    return day_series(codes)


def latest_snapshot(codes):
    """最新一拍 {code: row} —— D1 起委托 data/hub。"""
    from app.market_cn.auto.core.data.hub import market_snapshot
    return market_snapshot(codes)


# ================================================================
# 策略参数
# ================================================================

def _entry_stop(code, entry_price, strategy):
    """入场止损价 (通用): 注册表分发 initial_stop。"""
    s_obj = strat_reg.get_strategy(strategy)
    if s_obj is None:
        return round(entry_price * (1 - 8.0 / 100), 3)
    return s_obj.initial_stop(code, entry_price)


def _strategy_of(row):
    """取持仓行对应策略实例 (未知策略返回 None, 调用方跳过并告警)。"""
    return strat_reg.get_strategy(row.get("strategy") or ds.DRAGON_STRATEGY)


def _hold_day(row, today):
    """持仓第几日 (1-based; 入场当日=1)。按日历日近似, 交易日精确版待 exec 层。"""
    ed = str(row.get("entry_date") or "")[:10]
    if not ed:
        return 0
    try:
        from datetime import date
        y1, m1, d1 = (int(x) for x in ed.split("-"))
        y2, m2, d2 = (int(x) for x in str(today)[:10].split("-"))
        return max(1, (date(y2, m2, d2) - date(y1, m1, d1)).days + 1)
    except Exception:
        return 2


_TLEG_SPEC_CACHE = {}


def _market_spec_of(key):
    """策略声明的市场 MarketSpec (架构 §9: `meta.market` → MarketSpec)。

    ⚠ 2026-10-11 P0-2: 做T 的 T+1 约束必须按**该策略自己的市场**推导。此前
    `_eval_t_legs` 未把 spec 传下去 ⇒ `t_legs.t_constraints` 落 `default_market()`;
    当前默认市场恰为 A 故"侥幸正确", 一旦策略声明其它市场 (intraday_t0=True)
    就会用错的 min_hold_day / net_zero。
    """
    if key not in _TLEG_SPEC_CACHE:
        from app.market_cn.auto.core.runtime.evaluate import load_strategy
        _TLEG_SPEC_CACHE[key] = load_strategy(key).market_spec
    return _TLEG_SPEC_CACHE[key]


def _t_leg_position(row, today, spec=None):
    """持仓行 → t_legs.eval_t_legs 的 position dict (含 sellable_qty)。

    A股 T+1: 入场当日 sellable=0; 其后=仓位 (signals 表无数量列, 用 extra.position_qty
    或默认 1.0 归一化仓位)。t0 市场 (spec.intraday_t0) 当日即可卖。

    ⚠ spec **必须由调用方传入该策略的市场** (2026-10-11 P0-2); 留 None 会退化
    `default_market()`, 跨市场时用错 intraday_t0。
    """
    extra = row.get("extra") or {}
    qty = float(extra.get("position_qty") or 1.0)
    hold = _hold_day(row, today)
    # 2026-10-11 P0-2: T+1 语义收口到 `t_constraints` 唯一实现 (此前此处内联
    # `t0 or not entry_today`, 与 core/t_legs 的第二份语义; 且 try/except 吞掉取
    # spec 失败后 t0=False, 属静默降级)。min_hold_day: t0 市场=1, A股=2。
    from app.market_cn.auto.core.t_legs import t_constraints
    min_hold = int(t_constraints(spec)["min_hold_day"])
    sellable = qty if hold >= min_hold else 0.0
    return {"code": row.get("code"), "qty": qty, "sellable_qty": sellable,
            "entry_price": float(row.get("entry_price") or 0), "hold_day": hold}


def _eval_t_legs(row, s_obj, today, snap, series_rows):
    """评估做T 腿 → list[dict] 意图 (只记不执行; 每日每行最多求值一轮)。

    返回空列表表示无腿/不适用。结果写入 caller (extra.t_legs_today)。
    幂等: extra.t_legs_today.date == today 时不重复求值 (60s tick 防刷)。
    """
    if s_obj is None or not getattr(s_obj, "t_legs", None) and \
            not hasattr(s_obj, "t_leg_intents"):
        return []
    extra = row.get("extra") or {}
    prev = extra.get("t_legs_today") or {}
    if str(prev.get("date") or "")[:10] == str(today)[:10]:
        return []   # 今日已评估
    hold = _hold_day(row, today)
    if hold < 1:
        return []
    # 2026-10-11 P0-2: 取**本策略声明的市场**并一路传下去 —— 此前 spec 未传,
    # t_constraints 落 default_market() (多市场并存时 T+1/net_zero 全错)。
    sp = _market_spec_of(row.get("strategy") or ds.DRAGON_STRATEGY)
    pos = _t_leg_position(row, today, spec=sp)
    if pos["sellable_qty"] <= 0 and hold <= 1:
        return []
    # ctx: 当日快照统计 (t_hilo 等示例约定) + 市场约束 (供触发器/覆写钩子读, 免重算)
    from app.market_cn.auto.core.t_legs import t_constraints
    cons = t_constraints(sp)
    stats = None
    if snap:
        try:
            last = float(snap.get("last") or 0)
            high = float(snap.get("high") or 0)
            low = float(snap.get("low") or 0)
            pc = float(snap.get("previousClose") or 0)
            if last > 0 and pc > 0 and high > 0:
                stats = {
                    "last": last, "high": high, "low": low, "prev_close": pc,
                    "day_gain_pct": (last / pc - 1) * 100,
                    "pullback_from_high_pct": (last / high - 1) * 100 if high > 0 else 0.0,
                }
        except (TypeError, ValueError):
            stats = None
    ctx = {"snap": snap, "day": stats, "series": series_rows or [],
           "sold_today_pct": float(extra.get("t_sold_today_pct") or 0),
           "constraints": cons}
    try:
        intents = s_obj.t_leg_intents(pos, ctx, hold_day=hold, spec=sp) or []
    except Exception as e:
        logger.warning("[dragon_monitor] t_legs 求值失败 %s/%s: %s",
                       row.get("code"), row.get("strategy"), e)
        return []
    out = [i.as_dict() if hasattr(i, "as_dict") else dict(i) for i in intents]
    return out


def evaluate_confirm(row, series_rows):
    """确认判定 (通用): 注册表分发 confirm_decision, snap={"series": [...]}。

    返回 (level, reason, chg, vr):
      level  = 展示档位 strong/ok/weak (经 core.display_meta.confirm_level_of 归一;
               None=无法判定)
      reason = 策略原始语义串 (落 extra.pre_reason 作审计, **不当档位用**)
      chg/vr = d1_chg / d1_vol_r
    """
    s_obj = _strategy_of(row)
    if s_obj is None:
        return None, None, None, None
    dec = s_obj.confirm_decision(row, {"series": series_rows})
    if dec is None:
        return None, None, None, None
    return confirm_level_of(dec), dec.reason, dec.d1_chg, dec.d1_vol_r


# ================================================================
# 出场重放 (14:50): bars + 当日合成bar, 按策略各自规则
# ================================================================

def _bars_with_synth(code, entry_date):
    """1D bars + 当日合成bar; 返回 (bars, entry_idx) 或 (None, None)。

    D1: 合成口径已上收 data/hub.daily_live (逐字一致), 此处仅保留 entry_idx 定位。
    """
    from app.market_cn.auto.core.data.hub import daily_live
    # 2026-10-06: 历史段走**滑动窗口缓存** (只读, 每票 7.9ms → ~0); 缓存未覆盖
    # (首次/新股/缓存未建) ⇒ bars=None ⇒ daily_live 走 fetch_kline_db 原路径。
    # 合成 bar 的口径唯一来源仍是 hub._synth_bar_from_series, 注入不改变它。
    hist = None
    try:
        from app.market_cn.auto.core.data.window_cache import peek_windows
        hist = peek_windows([code], 200).get(code)
    except Exception:
        hist = None
    bars = daily_live(code, days=200, bars=hist)
    if not bars:
        return None, None
    idx = None
    for i, b in enumerate(bars):
        if b["time"] == entry_date:
            idx = i
            break
    if idx is None:
        return None, None
    return bars, idx


def _eval_exit_day_close(row):
    """收盘窗口出场判定 (通用): 构造 day_close snap → 注册表分发 exit_decision。

    返回 ExitDecision; 数据缺失 (bars/entry_idx 拿不到) 返回 None。
    """
    s_obj = _strategy_of(row)
    if s_obj is None:
        return None
    code = row["code"]
    bars, idx = _bars_with_synth(code, row.get("entry_date"))
    if bars is None:
        return None
    return s_obj.exit_decision(row, snap={"mode": "day_close", "bars": bars, "entry_idx": idx})


def _d1_chg_of(row, rows_):
    """d1_chg (2026-10-07, P5-④) —— **展示/统计字段**, 不进判定指纹 (见文件头)。

    口径: 当日 last vs signal_price (缺则 entry_price)。新判定源 (progress) 下策略的
    `confirm_decision` 不再被调用, d1_chg 由本函数统一产出。
    """
    last = float((rows_[-1] or {}).get("last") or 0) if rows_ else 0.0
    base = float(row.get("signal_price") or row.get("entry_price") or 0)
    if last > 0 and base > 0:
        return (last / base - 1) * 100
    return None


def _progress_enabled() -> bool:
    """P5-④ 开关 (2026-10-07): 盘中判定是否走 RealtimeBranch progress。**缺省关**。

    单独提出来是因为 step4 (收盘重放) 要先判开关再决定取不取当日快照 —— 缺省关时
    若照旧无条件取数, 就**多出一次 DB 查询**, 违反「缺省关 ⇒ 生产行为逐字不变」。
    """
    from app.market_cn.auto.strategies import monitor_progress_settings
    return bool(monitor_progress_settings().get("enabled"))


def _progress_map(rows, series, hm, stats=None):
    """P5-④ (2026-10-07): 盘中判定的**新事实源** = RealtimeBranch 当日 progress。

    开盘窗口 (可买 gap) 与 15:01 确认共用本函数 —— 两者都是"拿这只票今天的 progress"。

    返回 {(strategy_key, code): Progress} —— **只含真正拿到判定的票**。
    ⚠ 「拿不到判定」(切片缺失 / 策略无折叠契约 / 未到推进时点 / 异常 / **非活仓无事件**)
      一律不进字典, 由调用方回退旧路径 `confirm_decision`。**绝不能把"无判定"当成
      "判定为持有"** —— 那会把根本没判过的票静默转成持仓, 是实盘资金事故而非降级。
    ★ 唯一的无事件入典形态 = `stage="hold"` (P5-④ tick verdict): 仅当 **prev=exec
      (活仓) 且本 tick 评定无出场** 时产出 —— 这是"评过了, 继续持有"的真判定;
      死仓/无仓的无事件不产 hold (见 realtime.py 该分支注释, P2 实证)。
    """
    if not _progress_enabled():
        # §13.1 A-4 (2026-10-12): 「关着」必须与「坏了」可区分 —— 计数**显式归零**,
        #   不是不记 (面板上的 0 = 没开新判定源, 非空 = 开了且在回退/报错)。
        if stats is not None:
            stats.update({"prog_enabled": False, "confirm_prog_hit": 0,
                          "confirm_prog_miss": 0, "confirm_prog_err": 0,
                          "no_judgment": 0})
        return {}
    from app.market_cn.auto import present_daily
    from app.market_cn.auto.core.present import RealtimeBranch, StateStore

    by_key = {}
    for r in rows:
        s_obj = _strategy_of(r)
        key = getattr(s_obj, "key", None) if s_obj is not None else None
        if not key:
            continue
        by_key.setdefault(key, (s_obj, []))[1].append(r)

    out = {}
    store = StateStore(present_daily.default_root())
    for key, (s_obj, rs) in by_key.items():
        snaps = {}
        for r in rs:
            rows_ = series.get(r["code"]) or []
            if rows_:
                snaps[r["code"]] = rows_[-1]
        if not snaps:
            continue
        try:
            hits = RealtimeBranch(store, {key: s_obj}).tick(
                hm, list(snaps.keys()), snaps, series, None)
        except Exception as e:                                  # noqa: BLE001
            logger.error("[dragon_monitor] progress 判定失败 策略=%s: %s: %s "
                         "(回退 confirm_decision)", key, type(e).__name__, e)
            if stats is not None:
                stats["confirm_prog_err"] = (
                    stats.get("confirm_prog_err", 0) + len(snaps))
            continue
        group_hits = set()
        for code, prog in hits or []:
            if code not in snaps:
                continue
            out[(key, code)] = prog
            group_hits.add(code)
        if stats is not None:
            stats["confirm_prog_hit"] = (
                stats.get("confirm_prog_hit", 0) + len(group_hits))
            stats["confirm_prog_miss"] = (
                stats.get("confirm_prog_miss", 0) + len(snaps) - len(group_hits))
    if stats is not None:
        stats["prog_enabled"] = True
    return out


# ================================================================
# §13.1 A-4 (2026-10-12) 回退计数观测面板 —— 只观测不改行为
# ================================================================
#: progress 回退率告警阈值: (miss+err)/(hit+miss+err) 超过即 WARN（回退过半 = 新判定源
#: 可能坏了/覆盖不足, 必须有人看一眼）。可按运营经验调; 取 0.5 是保守起点。
_PROG_FALLBACK_RATE_ALERT = 0.5
#: 面板落盘: 项目根 tmp/traces/panel.jsonl（文件卫生约定 tmp/; probe 同款）。
#: 根锚点走 core._paths（禁在 core 外自算路径, 见 _paths.py 头注）。
from app.market_cn.auto.core._paths import PROJECT_ROOT as _PROJECT_ROOT
_PANEL_PATH = os.path.join(_PROJECT_ROOT, "tmp", "traces", "panel.jsonl")


def _panel_observe(stats, date, hm, path=None, enabled=None):
    """回退计数落 panel.jsonl + 超阈告警（A-4：只观测不改行为）。

    语义（区分三种状态，不许静默）：
      · `monitor_progress.enabled=false` ⇒ 计数**显式归零**并标 `prog_enabled=false`
        （「关着」不是「坏了」，没有判定源和判定源坏了是两回事）；
      · enabled 且 `confirm_prog_err>0` ⇒ ERROR（新判定源异常已回退旧三决策）；
      · enabled 且回退率超 `_PROG_FALLBACK_RATE_ALERT` ⇒ WARNING（覆盖不足/切片缺失）。
    JSONL 追加一行/tick（周面板/后续告警消费）；写失败只 WARN ——
    观测通道不得反噬生产 tick。
    """
    enabled = _progress_enabled() if enabled is None else bool(enabled)
    hit = int(stats.get("confirm_prog_hit", 0) or 0)
    miss = int(stats.get("confirm_prog_miss", 0) or 0)
    err = int(stats.get("confirm_prog_err", 0) or 0)
    nj = int(stats.get("no_judgment", 0) or 0)
    if not enabled:
        hit = miss = err = nj = 0
        stats.update({"prog_enabled": False, "confirm_prog_hit": 0,
                      "confirm_prog_miss": 0, "confirm_prog_err": 0,
                      "no_judgment": 0})
    else:
        stats["prog_enabled"] = True
    total = hit + miss + err
    rate = round((miss + err) / total, 4) if total else 0.0
    stats["prog_fallback_rate"] = rate
    rec = {"ts": datetime.now(ds.TZ_CN).isoformat(timespec="seconds"), "date": date, "hm": hm,
           "prog_enabled": enabled, "confirm_prog_hit": hit, "confirm_prog_miss": miss,
           "confirm_prog_err": err, "no_judgment": nj, "fallback_rate": rate}
    try:
        p = path or _PANEL_PATH
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:                                  # noqa: BLE001
        logger.warning("[dragon_monitor] panel.jsonl 写入失败(仅观测, 不影响 tick): %s", e)
    if enabled:
        if err > 0:
            logger.error("[dragon_monitor] progress 判定异常 %d 次 ⇒ 已回退旧三决策"
                         " (新判定源坏了, 看 panel.jsonl / A-4 面板)", err)
        if total and rate > _PROG_FALLBACK_RATE_ALERT:
            logger.warning("[dragon_monitor] progress 回退率 %.0f%% (%d/%d) 超阈 %.0f%%"
                           " —— 检查切片/折叠契约覆盖 (§13.1 A-4)",
                           rate * 100, miss + err, total,
                           _PROG_FALLBACK_RATE_ALERT * 100)
    return rec


# ================================================================
# 主 tick
# ================================================================

def run_monitor():
    """盘中 tick 主入口 (scheduler 调用)。幂等: 任何窗口重复执行不重复转移。"""
    ds.ensure_tables()
    hm = _now_hm()
    today = _today()

    # ── 0. 滞留自愈 (2026-09-29 审计修复 P1): 正式确认只覆盖 entry_date==today
    #      的行, 15:01 窗口错过 (快照未落/当日停机) 后 buy_today 行会永久滞留
    #      "买入"—— step5 次日不再确认, live 出场链只接 holding ⇒ 确认/出场记账
    #      断链。已入场 (entry_date 非空) 且未标出场的滞留行统一转 holding,
    #      交回后续 live/收盘出场链接管; 未入场行不动 (归 purge/cleanup)。
    stuck = [r for r in ds.list_signals(states=(ds.S_BUY_TODAY,),)
             if r.get("entry_date") and not r.get("exit_reason")
             and str(r["entry_date"])[:10] < today]
    for r in stuck:
        ds.set_state(r["id"], ds.S_HOLDING,
                     detail={"heal": "stuck_buy_today", "heal_ts": hm},
                     expect_state=ds.S_BUY_TODAY, only_unexited=True)
    if stuck:
        logger.warning("[monitor] 滞留 buy_today 自愈转 holding %d 行 (确认窗口错过)", len(stuck))

    # A5 (2026-10-05, 收口另一半): `days` 必须 ≥ 展示层可见窗口 —— 原实现 8 天,
    #   而 `get_active_signals` 是 30 天 ⇒ **展示层看得到、monitor 扫不到**的行会
    #   静静挂在自选股里: watch_pending 永不过期, 直到 days=30 窗口外/被 cleanup 删除。
    #   (这也是 A5 首修时把 stale 清理移出 09:25~09:35 窗口后仍修复不全的根因 ——
    #    那些行**连候选集都进不来**。)
    # ★ `today=` 显式传入 ⇒ 同一 tick 内四处查询共用一条日期线, 跨零点不自相矛盾。
    _win = ds.VISIBLE_WINDOW_DAYS
    pending = ds.list_signals(states=(ds.S_WATCH_PENDING,), days=_win, today=today)
    buy_rows = ds.list_signals(states=(ds.S_BUY_TODAY,), days=_win, today=today)
    hold_rows = ds.list_signals(states=(ds.S_HOLDING,), days=_win, today=today)
    exit_rows = ds.list_signals(states=(ds.S_EXIT_TODAY,), days=_win, today=today)

    stats = {"pending": len(pending), "buy": len(buy_rows),
             "holding": len(hold_rows), "exit": len(exit_rows)}

    # ── 1. 开盘窗口: 各策略 gap 过滤 → 质量排名 → 每日名额 → buy_today / expired; 隔日 pending 过期 ──
    #      A3 收尾 (2026-09-28): 本步 4 处写入全部带 expect_state=watch_pending ——
    #      cand/stale 均取自本 tick 的 pending 快照, 守卫不拦正常路径, 只挡「行已被并发
    #      买入/推进后仍按过期快照写」。无守卫时并发会把已入场行作废 (遗忘持仓) 或覆盖
    #      entry_price (收益统计失真), 亦会突破 daily_limit 名额。
    # ── 1b. stale 观察票过期 (A4, 2026-10-05) ──
    #      原实现把 stale 清理**绑死在 09:25~09:35 开盘窗口内**, 错过 (进程重启 /
    #      调度抖动 / 跨周末节假日后首个 tick 不在窗口内) 就当天永不过期, 该行以
    #      灰「观察」身份滞留自选股, 最长到 get_active_signals 的 days=30。
    #      watch_pending 是**未入场**行, 没有任何理由过夜 —— 与"已入场行一律可见"
    #      红线相反。故移出窗口条件, 每 tick 都可清。
    target = _last_trade_day()
    if pending:
        stale = [r for r in pending if str(r.get("trade_date"))[:10] < target]
        for r in stale:
            n_exp = ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"reason": "隔日未处理,过期", "sweep_ts": hm},
                                 expect_state=ds.S_WATCH_PENDING)
            if n_exp:
                stats["stale_expired"] = stats.get("stale_expired", 0) + 1
        if stale:
            logger.info("[dragon_monitor] 过期观察票 %d 行 (trade_date < %s)",
                        len(stale), target)

    if in_window(W_OPEN_LO, W_OPEN_HI, hm) and pending:
        cand = [r for r in pending if str(r.get("trade_date"))[:10] == target]
        stale = []   # 已在 1b 统一清理, 本窗口内不再重复
        # 禁用策略的存量 pending 直接过期 (09-15 事故修复: 停扫只断新信号,
        # 已入库的 pending 行此前仍会在开盘窗口被买入)
        # 2026-09-26: 批量走 store.retire_unfilled (唯一实现)。
        disabled_codes = []
        disabled_ids = []
        reason_by_key = {}
        for r in list(cand):
            strat = r.get("strategy") or ds.DRAGON_STRATEGY
            if not strat_reg.is_enabled(strat):
                disabled_ids.append(r["id"])
                disabled_codes.append(r.get("code"))
                reason_by_key[strat] = f"策略已禁用({strat}), 信号作废"
                cand.remove(r)
        if disabled_ids:
            swept = ds.retire_unfilled(ids=disabled_ids,
                                       reason_by_key=reason_by_key)
            if swept is None:
                # 2026-10-07 (P2): None=作废失败。行已从本轮候选移除 (不会被买入),
                # 但**没有落 expired** ⇒ 会挂着 watch_pending 直到被 1b 判隔日过期。
                # 必须报出来, 否则「停用策略的行还在」看起来像正常延迟。
                logger.error("[dragon_monitor] 禁用策略存量信号作废**失败**(未落 expired): %s "
                             "(n=%d)", disabled_codes, len(disabled_ids))
            else:
                logger.warning("[dragon_monitor] 禁用策略存量信号作废: %s (n=%d)",
                               disabled_codes, len(swept))
        if cand:
            snaps = latest_snapshot([r["code"] for r in cand])
            # P5-④ 第二步 (2026-10-07): 可买判定源可切到 RealtimeBranch 当日 progress
            #   (同一开关 monitor_progress.enabled)。开盘窗口只有最新快照, series 按
            #   单帧构造 (tick 内部对 series 缺失也有 [snap] 兜底)。
            prog_map = _progress_map(cand, {c: [s] for c, s in (snaps or {}).items()},
                                     hm, stats)
            from collections import defaultdict as _dd
            qualified = _dd(list)      # strategy → [(quality_key, row, open_px, gap)]
            n_exp = 0
            for r in cand:
                code = r["code"]
                snap = snaps.get(code)
                if not snap:
                    continue
                open_px = float(snap.get("open") or snap.get("last") or 0)
                if open_px <= 0:
                    continue
                prev_close = float(snap.get("previousClose") or r.get("signal_price") or 0)
                if prev_close <= 0:
                    continue
                gap = (open_px / prev_close - 1) * 100
                # ★ 涨停阻买 (2026-10-10 审计 #8-2, **市场事实**而非策略规则):
                #   开盘封死涨停 → 物理买不进, 不得转 buy_today。统一走 exec 唯一实现
                #   (折叠加 exec payload 的 buyable 同一旗标口径)。
                if fill_blocked_by_limit_up(
                        open_px, limit_up_price(prev_close, get_board_type(code))):
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2), "src": "limit_up_guard",
                                         "reason": "开盘涨停封死, 不可买(市场事实)"},
                                 expect_state=ds.S_WATCH_PENDING)
                    n_exp += 1
                    continue
                strat = r.get("strategy") or ds.DRAGON_STRATEGY
                s_obj = strat_reg.get_strategy(strat)
                if s_obj is None:
                    logger.warning("[dragon_monitor] 未知策略 %s (row %s), 跳过", strat, r.get("id"))
                    continue
                # ★ entry.gap_min/gap_max 通用过滤 (D2 极小 schema 2026-10-12):
                #   市场事实(涨停阻买)之后、策略 entry_decision 之前; 未设 = 不限。
                from app.market_cn.auto.strategies.base import entry_gap_ok
                if not entry_gap_ok(getattr(s_obj, "entry_spec", lambda: {})(), gap):
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2), "src": "entry_schema",
                                         "reason": f"{ds.strategy_labels().get(strat, strat)}gap超出entry过滤区间"},
                                 expect_state=ds.S_WATCH_PENDING)
                    n_exp += 1
                    continue
                prog = prog_map.get((getattr(s_obj, "key", None), code))
                if prog is not None and getattr(prog, "stage", "") == "exec":
                    # 折叠内核的 gap 门在 exec payload 的 `buyable` 里 (break._exec_event)。
                    # ⚠ 只有**拿到 exec 判定**才走新路径; 拿到的是别的 stage / 没拿到
                    #   ⇒ 落回下面旧的 entry_decision, 不猜。
                    pl = getattr(prog, "payload", None) or {}
                    if pl.get("buyable") is False:
                        ds.set_state(r["id"], ds.S_EXPIRED,
                                     detail={"gap": round(gap, 2), "src": "progress",
                                             "reason": f"{ds.strategy_labels().get(strat, strat)}开盘gap超出可买区间"},
                                     expect_state=ds.S_WATCH_PENDING)
                        n_exp += 1
                        continue
                    qualified[strat].append((s_obj.quality_key(r), gap, r, open_px))
                    continue
                if not s_obj.entry_decision(r, snap).buyable:
                    ds.set_state(r["id"], ds.S_EXPIRED,
                                 detail={"gap": round(gap, 2),
                                         "reason": f"{ds.strategy_labels().get(strat, strat)}开盘gap超出可买区间"},
                                 expect_state=ds.S_WATCH_PENDING)
                    n_exp += 1
                    continue
                # 质量排序键 (越大越优先, 策略自定义)
                qualified[strat].append((s_obj.quality_key(r), gap, r, open_px))
            # 各策略按名额买入, 超出名额 → expired (质量排名末位淘汰)
            n_buy = 0
            for strat, lst in qualified.items():
                lst.sort(key=lambda x: x[0], reverse=True)
                limit = strat_reg.daily_limit(strat)
                # 2026-10-07 (P2): `0/None = 不截断` —— 与 scan 侧 `finalize_signal_rows`
                #   (`if cap and len(grp) > cap`) **同一口径**。改前是 `i < limit`: limit=0
                #   时恒假 ⇒ 当日候选全员判 expired, 理由还写成"当日名额已满" —— 故障伪装
                #   成正常限额且无告警。config `_daily_limit_note` 明文写的就是 0=不截断。
                for i, (qkey, gap, r, open_px) in enumerate(lst):
                    if not limit or i < limit:
                        ds.set_state(r["id"], ds.S_BUY_TODAY,
                                     detail={"entry_gap": round(gap, 2), "rank": i + 1},
                                     entry_date=today, entry_price=round(open_px, 3),
                                     expect_state=ds.S_WATCH_PENDING)
                        ds.update_stop_price(r["id"], _entry_stop(r["code"], open_px, strat))
                        n_buy += 1
                    else:
                        ds.set_state(r["id"], ds.S_EXPIRED,
                                     detail={"reason": f"当日名额已满(质量排名第{i+1})"},
                                     expect_state=ds.S_WATCH_PENDING)
                        n_exp += 1
            stats["open_buy"] = n_buy
            stats["open_expired"] = n_exp

    # ── 2. 盘中硬止损保护 (buy_today/holding) + 策略盘中出场 (live 模式) ──
    if "09:35" <= hm < "15:00":
        guard_rows = buy_rows + hold_rows
        if guard_rows:
            snaps = latest_snapshot([r["code"] for r in guard_rows])
            # live 模式出场需要当日全天快照序列 (策略盘中出场判定)
            series_all = fetch_day_snapshots([r["code"] for r in guard_rows])
            # P5-④ 第二步 (2026-10-07): 盘中**策略**出场判定源可切到 progress (同一开关)。
            #   ⚠ 上面的硬止损 (px <= stop_px) **永不迁** —— 那是资金红线, 不是"规则性
            #   判定", 文档风险表第 8 条明写。本步只换它之后的策略 live 出场。
            prog_map = _progress_map(guard_rows, series_all, hm, stats)
            for r in guard_rows:
                if r.get("exit_reason"):
                    continue
                s_obj = _strategy_of(r)
                if s_obj is None:
                    continue
                # T+1 保护: 尾盘入场策略 当日不可卖, 跳过当日止损/出场
                # entry.mode (yaml) 单一事实源 (P2 2026-10-12 删类属性兜底)
                if s_obj and s_obj.entry_is_at_close() and \
                        str(r.get("entry_date") or "")[:10] == today:
                    continue
                snap = snaps.get(r["code"])
                if not snap:
                    continue
                # ── 做T 腿 (T16): 已持仓日评估, 只记意图不执行; 先于 exit 判定 ──
                if r.get("state") == ds.S_HOLDING:
                    t_intents = _eval_t_legs(
                        r, s_obj, today, snap,
                        series_all.get(r["code"]) or [])
                    if t_intents:
                        detail = {"t_legs_today": {
                            "date": today, "hm": hm, "intents": t_intents}}
                        # A3 同类隐患: 只在行仍是 holding 时记意图 (条件写入,
                        # 与并发出场转移竞争时意图不落到已出场行上)。
                        ds.set_state(r["id"], ds.S_HOLDING, detail=detail,
                                     expect_state=ds.S_HOLDING, only_unexited=True)
                        stats["t_legs"] = stats.get("t_legs", 0) + len(t_intents)
                        logger.info("[dragon_monitor] 做T意图 %s/%s n=%d: %s",
                                    r.get("code"), r.get("strategy"),
                                    len(t_intents),
                                    [x.get("label") for x in t_intents])
                px = float(snap.get("last") or 0)
                stop_px = float(r.get("stop_price") or 0)
                if px > 0 and stop_px > 0 and px <= stop_px:
                    # 2026-09-26 bugfix: 补 exit_price (原缺失 → 平账无出场价, 收益统计空)
                    # A3: only_unexited —— 出场标记不可被并发写入覆盖。
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason="盘中止损",
                                 exit_price=round(px, 3),
                                 detail={"marked": today, "stop_price": stop_px},
                                 only_unexited=True)
                    stats["intraday_stop"] = stats.get("intraday_stop", 0) + 1
                    continue
                # 策略 live 出场 (knife_catch D1开盘卖等; 其它策略 live → hold)
                # ⚠ "open" 必须注入: 策略侧写的是 `snap.get("open") or snap.get("last")`,
                #    而本 snap 默认只有 mode/series/today ⇒ 不注入就恒回退 last (09:35 首拍价),
                #    knife_catch/tail_oversold 的「D1 开盘卖」会静默变成「盘中价卖」。
                #    快照 row 自带 open 列 (hub._fetch_snapshots_by_date), 取当日开盘价。
                prog = prog_map.get((getattr(s_obj, "key", None), r["code"]))
                if prog is not None:
                    # 拿到内核判定 ⇒ **以它为准**, 不再回退旧路径 (两个事实源不能打架):
                    #   exit ⇒ 今日出场; 其它 stage (exec/ready…) ⇒ 今日不出场。
                    if getattr(prog, "stage", "") == "exit":
                        pl = getattr(prog, "payload", None) or {}
                        xp = pl.get("exit_price") or px
                        ds.set_state(r["id"], ds.S_EXIT_TODAY,
                                     exit_reason=pl.get("exit_reason") or "progress_exit",
                                     exit_price=round(float(xp), 3) if xp else None,
                                     detail={"marked": today, "intraday": True,
                                             "src": "progress"},
                                     only_unexited=True)
                        stats["live_exit"] = stats.get("live_exit", 0) + 1
                    continue
                dec = s_obj.exit_decision(r, snap={"mode": "live",
                                                   "series": series_all.get(r["code"]) or [],
                                                   "today": today,
                                                   "open": float(snap.get("open") or 0)})
                if dec.action == "exit" and dec.price:
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=dec.reason,
                                 exit_price=round(float(dec.price), 3),
                                 detail={"marked": today, "intraday": True},
                                 only_unexited=True)
                    stats["live_exit"] = stats.get("live_exit", 0) + 1

    # ── 3. 14:25~14:45 预确认 ("当日买入行"通用, 无策略过滤; 各策略 confirm_decision 给档位) ──
    #      pre_confirm = 归一档位 strong/ok/weak (展示用, 前端 pcMap);
    #      pre_reason  = 策略原始语义串 (仅审计, 前端不展示 —— 防 g56_hold 之类内部 token 漏进 UI)。
    #      该标记只在当日买入窗口有意义 —— 15:00 正式确认后由 step 7 统一清除, 勿在此清。
    if in_window(W_PRECONF_LO, W_PRECONF_HI, hm):
        today_buys = [r for r in buy_rows if str(r.get("entry_date"))[:10] == today]
        if today_buys:
            series = fetch_day_snapshots([r["code"] for r in today_buys])
            for r in today_buys:
                # A3 (2026-09-28): buy_rows 是 tick 开头快照 —— 同一 tick 的 step2
                # 可能刚把该行打出 exit_today。已有出场标记的行跳过预确认;
                # 写入用条件守卫 (state 仍=buy_today 且无出场标记), 不把旧 r["state"]
                # 当写入值, 防止 step2 刚做的止损/出场被本步回滚。
                if r.get("exit_reason"):
                    continue
                if (r.get("extra") or {}).get("pre_confirm"):
                    continue
                rows_ = series.get(r["code"])
                if not rows_:
                    continue
                level, reason, chg, vr = evaluate_confirm(r, rows_)
                if level:
                    detail = {"pre_confirm": level, "pre_ts": hm}
                    if reason:
                        detail["pre_reason"] = reason
                    ds.set_state(r["id"], ds.S_BUY_TODAY, detail=detail,
                                 expect_state=ds.S_BUY_TODAY, only_unexited=True)

    # ── 4. 收盘窗口: 出场重放 (holding, 注册表分发 day_close 模式) ──
    if in_window(W_CLOSESIM_LO, W_CLOSESIM_HI, hm):
        # P5-④ (2026-10-07): 判定源可切到 progress (同一开关)。旧路径用合成 bars 重放
        #   exit_decision; 新路径用当日快照试推。**先判开关再取数** —— 缺省关时不许多
        #   出这次快照查询 (见 `_progress_enabled` 注)。
        prog_map = {}
        if hold_rows and _progress_enabled():
            prog_map = _progress_map(
                hold_rows, fetch_day_snapshots([r["code"] for r in hold_rows]),
                hm, stats)
        for r in hold_rows:
            if r.get("exit_reason"):
                continue
            s_obj = _strategy_of(r)
            # D-A: 策略级收盘窗起点 (meta.close_sim_at), 未到该策略起点则跳过
            if s_obj and hm < _close_sim_start(s_obj):
                continue
            prog = prog_map.get((getattr(s_obj, "key", None), r["code"])) if s_obj else None
            if prog is not None:
                # 拿到内核判定 ⇒ 以它为准: exit ⇒ 出场; 其它 stage ⇒ 今日不出场。
                if getattr(prog, "stage", "") == "exit":
                    pl = getattr(prog, "payload", None) or {}
                    ds.set_state(r["id"], ds.S_EXIT_TODAY,
                                 exit_reason=pl.get("exit_reason") or "progress_exit",
                                 exit_price=round(float(pl.get("exit_price")), 3)
                                 if pl.get("exit_price") else None,
                                 detail={"marked": today, "src": "progress"},
                                 expect_state=ds.S_HOLDING, only_unexited=True)
                continue
            dec = _eval_exit_day_close(r)
            if dec is not None and dec.action == "exit":
                # A3 (2026-09-28): hold_rows 是 tick 开头快照 —— step2 (窗口重叠
                # 14:50~15:00) 可能刚给该行做过 live 出场/盘中止损。条件写入:
                # 仅当行仍是 holding 且无出场标记才落, 收盘重放不得覆盖已有出场价/原因。
                ds.set_state(r["id"], ds.S_EXIT_TODAY, exit_reason=dec.reason,
                             exit_price=round(float(dec.price), 3) if dec.price else None,
                             detail={"marked": today},
                             expect_state=ds.S_HOLDING, only_unexited=True)

    # ── 5. 正式确认 15:01+ (当日 buy_today → holding / exit_today, 注册表分发) ──
    if hm >= W_CONFIRM_LO:
        today_buys = [r for r in buy_rows if str(r.get("entry_date"))[:10] == today]
        if today_buys and snapshot_day_done():
            series = fetch_day_snapshots([r["code"] for r in today_buys])
            # P5-④ (2026-10-07): 判定源可切到 RealtimeBranch 的当日 progress
            #   (开关 monitor_progress.enabled)。**只认真正拿到的判定** —— 没判到的票
            #   走下面原来的 confirm_decision, 绝不因"新路径没数据"而漏确认或误判。
            prog_map = _progress_map(today_buys, series, hm, stats)
            n_fallback = 0
            for r in today_buys:
                rows_ = series.get(r["code"])
                if not rows_:
                    continue
                s_obj = _strategy_of(r)
                if s_obj is None:
                    continue
                if r.get("exit_reason"):
                    # 盘中已标记出场 (止损/live) 的行不再确认 (防误转 holding)
                    continue
                prog = prog_map.get((getattr(s_obj, "key", None), r["code"]))
                if prog is not None:
                    pl = getattr(prog, "payload", None) or {}
                    if getattr(prog, "stage", "") == "exit":
                        xp = pl.get("exit_price") or (rows_[-1] or {}).get("last")
                        ds.set_state(r["id"], ds.S_EXIT_TODAY, confirm_date=today,
                                     d1_chg=_d1_chg_of(r, rows_),
                                     exit_reason=pl.get("exit_reason") or "progress_exit",
                                     exit_price=round(float(xp), 3) if xp else None,
                                     detail={"marked": today, "src": "progress"},
                                     expect_state=ds.S_BUY_TODAY, only_unexited=True)
                    else:
                        ds.set_state(r["id"], ds.S_HOLDING, confirm_date=today,
                                     d1_chg=_d1_chg_of(r, rows_),
                                     detail={"src": "progress"},
                                     expect_state=ds.S_BUY_TODAY, only_unexited=True)
                    continue
                n_fallback += 1
                dec = s_obj.confirm_decision(r, {"series": rows_})
                if dec is None:
                    continue
                if not dec.confirmed:
                    # A3: 确认写入带条件守卫 (行仍=buy_today 且无出场标记) ——
                    # 与 step2/step4 的出场写入竞争时, 出场标记不被确认结果覆盖。
                    ds.set_state(r["id"], ds.S_EXIT_TODAY, confirm_date=today,
                                 d1_chg=dec.d1_chg, exit_reason=dec.reason,
                                 exit_price=round(float(dec.exit_price), 3) if dec.exit_price else None,
                                 detail={"marked": today, **(dec.detail or {})},
                                 expect_state=ds.S_BUY_TODAY, only_unexited=True)
                else:
                    ds.set_state(r["id"], ds.S_HOLDING, confirm_date=today,
                                 d1_chg=dec.d1_chg, d1_vol_r=dec.d1_vol_r,
                                 detail=dec.detail or {},
                                 expect_state=ds.S_BUY_TODAY, only_unexited=True)
            # P5-④ 观察灯: 回退旧路径的行数。目标 0 = tick verdict 覆盖全分支，可删 confirm_decision
            stats["no_judgment"] = n_fallback

    # ── 6. exit_today 执行平账 → closed ──
    #      A3 收尾 (2026-09-28): 2 处平账写入带 expect_state=exit_today —— 行集取自本 tick
    #      的 exit_rows 快照; 幂等原靠快照层去重, 但拦不住「快照后行已被并发推进到其它
    #      终态」的情形 (会被写回 closed, 状态机倒退)。
    #    默认: 隔日开盘执行 (补记账, exit_price 覆写为实际开盘价);
    #    exit_exec_same_day 策略 (knife_catch D1当日卖): 当日 14:55 后平账, 保留标记时价格
    #
    #      A1 (2026-10-04, 资金红线): 原 `if r.get("exit_date"): continue` 是**永久阻断** ——
    #      rebuild.replay_ledger 写入的行是 `state=exit_today` 且 `exit_date` 已填、不写
    #      extra.marked (见 rebuild.py:656-660), 一进本步就被跳过且永远不再处理:
    #      不出组 (exit_today ∈ ACTIVE_GROUP_STATES)、不平账、不告警, 一直挂到
    #      cleanup_old 的 24 日历日后被物理删除 ⇒ 「卖点出现的股票几天了还在自选股」。
    #      而 reconcile_startup 每次重启比对指纹, 任何策略改动 → trigger_rebuild 重写
    #      已推进行 ⇒ 触发频率极高。
    #      改法: exit_date 已填的 exit_today = **已知终态**, 直接补记 closed;
    #            exit_date/exit_price **保留已有值, 不重算** (实盘口径优先, 见 A2 方案3)。
    #
    #      A5 (2026-10-04): 本步原有 4 个静默 `continue` (exit_date 已填 / marked>=today /
    #      无快照 / 无开盘价), 零日志零计数 —— 平账链断了没人知道, 与项目 MEMORY 点名的
    #      「声明了但没接线」静默断链同构。现在每个出口都计数+告警, stats 带
    #      exit_stuck_breakdown 供上层/体检读取。
    exit_settled = 0
    exit_stuck = {"already_dated": 0, "marked_today": 0, "no_snapshot": 0, "no_open_px": 0}
    if hm >= "09:30":
        for r in exit_rows:
            code = r.get("code")
            if r.get("exit_date"):
                # 出口 A (A1): 已有出场日 = 已知终态 → 收口出组, **保留** exit_date/exit_price
                n = ds.set_state(r["id"], ds.S_CLOSED,
                                 detail={"settled": "already_dated", "settled_ts": hm},
                                 expect_state=ds.S_EXIT_TODAY)
                if n:
                    exit_settled += 1
                    logger.info("[dragon_monitor] 平账收口(已有出场日, 保留原价) %s/%s "
                                "exit_date=%s exit_price=%s",
                                code, r.get("strategy"), r.get("exit_date"),
                                r.get("exit_price"))
                continue
            marked = (r.get("extra") or {}).get("marked") or str(r.get("updated_at"))[:10]
            s_obj = _strategy_of(r)
            same_day = s_obj is not None and getattr(s_obj, "exit_exec_same_day", False)
            if same_day:
                if marked >= today and hm < "14:55":
                    exit_stuck["marked_today"] += 1      # 当日执行的行, 等到尾盘再平账 (正常)
                    continue
                keep_px = float(r.get("exit_price") or 0)
                ds.set_state(r["id"], ds.S_CLOSED, exit_date=today,
                             exit_price=round(keep_px, 3) if keep_px > 0 else None,
                             expect_state=ds.S_EXIT_TODAY)
                exit_settled += 1
                continue
            if marked >= today:
                exit_stuck["marked_today"] += 1          # 隔日执行: 今天刚标记的, 明早再平 (正常)
                continue
            snaps = latest_snapshot([r["code"]])
            snap = snaps.get(r["code"])
            if not snap:
                exit_stuck["no_snapshot"] += 1
                logger.warning("[dragon_monitor] 平账卡住·无当日快照 %s/%s "
                               "exit_date=%s marked=%s (停牌/快照未回填?)",
                               code, r.get("strategy"), r.get("exit_date"), marked)
                continue
            open_px = float(snap.get("open") or snap.get("last") or 0)
            if open_px <= 0:
                exit_stuck["no_open_px"] += 1
                logger.warning("[dragon_monitor] 平账卡住·无开盘价 %s/%s marked=%s",
                               code, r.get("strategy"), marked)
                continue
            ds.set_state(r["id"], ds.S_CLOSED, exit_date=today, exit_price=round(open_px, 3),
                         expect_state=ds.S_EXIT_TODAY)
            exit_settled += 1
    stats["exit_settled"] = exit_settled
    # 真正的卡死 = no_snapshot / no_open_px; already_dated 是 A1 新增的收口路径;
    # marked_today 属正常待执行, 不算卡
    stats["exit_stuck"] = exit_stuck["no_snapshot"] + exit_stuck["no_open_px"]
    stats["exit_stuck_breakdown"] = exit_stuck
    if stats["exit_stuck"]:
        logger.warning("[dragon_monitor] 平账卡住 %d 行: %s (长期卡住会让票留在自选股, 见 A1)",
                       stats["exit_stuck"], exit_stuck)

    # ── 7. 清理过期瞬时标记: pre_confirm/pre_ts/pre_reason 只在"当日买入行"期间有意义 ──
    #      设计口径: 14:25 加"预"角标 → **15:00 正式确认覆盖** (docs/龙回头自动化设计方案.md:92/154)。
    #      extra 是增量合并 (只加不减), 上面各出口 —— 盘中硬止损 / live 出场 / 15:01 正式确认 /
    #      未确认跨日 —— 都可能把标记留下 ⇒ 统一在此按 "state=buy_today 且 entry_date=今天"
    #      保留、其余清除。幂等 (已清理的不再匹配), 亦自愈历史脏数据。
    #      漏清的后果: 持仓行带着 pre_confirm 过夜, 显示层把它当"当前预判"渲染成"预持"。
    ds.purge_stale_detail(("pre_confirm", "pre_ts", "pre_reason"), ds.S_BUY_TODAY, today)

    # ── 8. 组对账 ──
    ds.sync_watchlist_group(ds.get_active_signals())
    # ── §13.1 A-4 (2026-10-12) 回退计数观测面板（只观测不改行为） ──
    _panel_observe(stats, today, hm)
    return stats


# 调度器契约名 = 本模块主 tick。
# 历史: scheduler._dragon_strategy_monitor 按此名 import, 但该名一直不存在 ⇒
#   每次触发在 import 处抛 ImportError, 被 scheduler._worker 的 except Exception
#   吞成一条日志 ⇒ 开盘 gap 买入 / 盘中止损 / 预确认 / 收盘出场 / 15:01 确认 /
#   exit 平账 / 组对账 整条盘中链在生产上停摆 (2026-10-09 审计 B-1 修复)。
# 铁律: 这是**契约别名**, 语义恒等于 run_monitor。
#   不要在此包 try/except —— 盘中链失败必须抛出并被调度器记账, 绝不静默降级。
run_monitor_safe = run_monitor
