# app/market_cn/auto/present_daily.py
"""展示层切片的**每日落盘接线** (P5-③ 前置, 2026-10-07)。

病根: 生产链此前**没有任何 StateStore 落盘接线** —— 切片落盘只出现在 tools/ 与
tests/ 里 ⇒ P5 影子期 (投影 vs 现表对账) **无 Record 可读**, 「切 writer」缺前置条件。

本模块补齐这一环:
    DailyRunner.advance_all(strategy, date, day_inputs)
    → StateStore 把 state / current / events 写入切片根 (`_paths.PRESENT_STATE_DIR`)。

三条不变量:
  1. **开关即写入器 (P5-③ 切换点)**: config.json 顶层 `present_persist.enabled`。缺键 / false
     ⇒ 旧 writer（`scan` 循环内逐票判定 + `signal_row`），本模块**一次都不被调用**，
     生产行为逐字不变；true ⇒ `persist_days` **转主线**（fold 成为唯一判定，失败即失败，
     不再 try 住），`scan` 的 signals 行改由 `Record.ready` 投影（`stat["ready"]`）。
     回滚 = 翻回 false（**单点**）。⚠ 这是**迁移期开关**，不是长期并存的第二条路径。
  2. **同源取数**: bars 由调用方传入, 且 as-of 收敛走 `kline.asof_bars`
     (与判定循环**同一个函数**) —— 分叉取数 ⇒ 影子 diff 比的是两套数据, 差异全假。
  3. **不静默**: 无折叠契约的策略具名登记跳过 (调用方须为它们保留直判路径) ——
     2026-10-09 P6-6/7 退役 v1/relay3/lead_chase 后当前无此类; 推进异常登记进
     `errors` 并打 ERROR 日志 (静默降级是头号敌人)。

⚠⚠ **冷启动必须暖机**（两个独立理由，都必须满足）：
  ① **事件覆盖**（最硬）：`Record.events` 是 append-only 流水，只从「第一次落盘那天」
     开始累积。冷切片只落目标日 ⇒ 影子窗口内的历史 ready **根本不在流水里** ⇒
     投影集不全 ⇒ 库内真实行全被判 ghost，`--apply` 会批量作废真实信号。故暖机
     交易日数必须 ≥ 影子窗口（`projection_shadow.DEFAULT_WINDOW`）。
  ② **g56 池台账**：g56 的横截面池由 `ledger`(滚动 30 日) 重建，而生产 `scan_days`
     走 bars 全窗口聚合 (`_ensure_pool_daily`)。冷切片首日 ledger 只有 1 天 ⇒
     `score_r` 与生产**必然不同**（pctl 窗口 ROLL=20/MIN_HIST=5）。dragon/break
     无跨日共享态，不受这一条影响。
  ⚠ 暖机**只在冷切片**（该策略的切片文件不存在）上触发：在热切片上重放旧日会被
  `_rebuild_reason` 判 `reordered` 而**回转 state**，且 g56 的 `PoolLedger.append`
  按"末条日期 ≥ 新条日期即覆盖"处理 ⇒ 会把最新一条台账覆盖成旧值（台账被搅坏）。

⚠ 两种角色由 `present_persist.enabled` 决定: **关** = 本模块不参与生产 (影子对账由
  `tools/projection_shadow.py` 只读回放, 不经这里); **开** = 它就是生产写路径的判定端
  (P5-③ 切 writer)。同一份代码, 没有第二条判定实现。
"""
from __future__ import annotations

import os

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: 暖机日数的**事实源 = config.json 顶层 `present_persist.warmup`**（当前 25）。
#: 为什么是 25：两个独立下界取其大，再留余量——
#:   ① **事件覆盖**（最硬）：`Record.events` 只从「第一次落盘那天」开始累积 ⇒ 冷切片只落
#:      目标日时，历史 ready 一律不在流水里 ⇒ 影子窗口内的历史行全被判 missing。故暖机
#:      必须 ≥ 影子窗口（`projection_shadow.DEFAULT_WINDOW` = 15）。
#:      ⚠ 2026-10-07 澄清：本约束**只约束 DB 差异模式**（按日累积，切片里只有当天）。
#:        回放模式（`--replay`, `REPLAY_WINDOW` = 60）是**一次性构建全量切片**，窗口内
#:        每一天都有 events ⇒ 不受本约束管辖（25 < 60 不是违规）。回放窗口开头的暖机
#:        假象由 `projection_shadow.WARM_SKIP` = 20 单独剔除，与暖机日数是两件事。
#:   ② g56 池台账：ledger 的滚动分位窗口 ROLL=20 ⇒ 需 ≥20 个交易日才与生产同值。
#: ⚠ 此处**不再放常量**：留一个没人读的 `WARMUP_DAYS` 只会让"改了却不生效"看起来正常。


def settings() -> dict:
    """切片落盘开关 + 暖机日数。**唯一事实源 = config.json 顶层 `present_persist`**
    (读取实现见 `strategies.present_persist_settings`; 缺键 ⇒ 全关)。

    走注册表而不是环境变量: config.json 被 git 跟踪, 阶段开关可复核可回滚 (见该函数 ★)。
    """
    from app.market_cn.auto.strategies import present_persist_settings
    return present_persist_settings()


def enabled() -> bool:
    """是否每日落盘切片 (P5 影子期开关)。**默认关** ⇒ 生产零行为变化。"""
    return bool(settings()["enabled"])


def default_root() -> str:
    """切片落盘根 (唯一生产 root; 锚点在 `core/_paths`)。"""
    from app.market_cn.auto.core._paths import PRESENT_STATE_DIR
    return PRESENT_STATE_DIR


def warmup_days() -> int:
    """冷切片暖机的交易日数 (0=不暖机)。见模块头 ⚠⚠。

    只在「该策略切片不存在」时生效 ⇒ 设一次可长期留着, 热切片不会被重放。
    """
    return int(settings()["warmup"])


def asof_inputs(bars_by_code, date, *, min_bars):
    """{code: bars} → {code: day_inputs} (advance_all 的输入; as-of 收敛到 date)。

    只收「末根 == date」的票: 停牌 / 缺 bar / 过短的票天然不推进 (与判定循环同语义)。
    """
    from app.market_cn.auto.core.data.kline import asof_bars

    out = {}
    for code, bars in (bars_by_code or {}).items():
        bs = asof_bars(bars, date, min_bars)
        if bs is None:
            continue
        out[code] = {"today": bs[-1],
                     "yesterday": bs[-2] if len(bs) >= 2 else None,
                     "probe_bars": {b["time"]: b for b in bs},
                     "history": bs}
    return out


def recent_dates(bars_by_code, end, n):
    """[end 往前 n 个交易日, end] 的升序日期序列 (取自 bars 的日期并集)。

    非交易日无 bar ⇒ 并集天然只含交易日; 用并集而非某只票的序列, 避免把停牌
    误当非交易日 (停牌票的日期缺口不应缩短暖机窗口)。
    """
    days = set()
    for bars in (bars_by_code or {}).values():
        for b in (bars or ()):
            t = b["time"]
            if t <= end:
                days.add(t)
    return sorted(days)[-(n + 1):]


def _ready_of(store, key, bars_by_code, base_dates, signal_of_ready):
    """读回该策略切片里 `base_dates` 当日的 ready 事件 → {code: [Signal]} (P5-③ 行源)。

    为什么从 `Record` **读回**, 而不是用 `advance_all` 的返回值:
      ① 同日重跑被 `_rebuild_reason` 判 `noop` 拒绝 ⇒ 返回值空, 但当日事件仍在
         流水里; 只认返回值会让重跑那一轮的行凭空消失, 紧接着 `upsert_scan_signals`
         把当日 watch_pending 全 purge ⇒ **信号被自己删光**。
      ② 冷切片暖机时 `advance_all` 还回传暖机日事件 —— 那些不是本日行。
    只认 `Record` 则两种情形同解。
    """
    out = {}
    for code in (bars_by_code or ()):
        rec = store.load(key, code)
        if rec is None:
            continue
        sigs = [signal_of_ready(code, e.get("date"), e.get("payload"))
                for e in (rec.events or [])
                if e.get("stage") == "ready" and str(e.get("date"))[:10] in base_dates]
        if sigs:
            out[code] = sigs
    return out


def persist_days(active, dates, bars_by_code, *, root=None, warmup=0,
                 min_bars=None, logger_=None):
    """把 `dates` (升序) 的判定进度逐日落盘。同日重跑由 `advance_all` 幂等拒绝。

    Args:
        active: {key: strategy} —— 无折叠契约的策略具名跳过 (登记进 stat)。
        dates: 目标日期 (升序); 生产日用 `[target]`, 暖机用 `recent_dates(...)`。
        bars_by_code: {code: bars} —— **必须与判定同源** (调用方从判定的同一份取数传入)。
        root: 切片根; 缺省 `_paths.PRESENT_STATE_DIR`。
        warmup: >0 且该策略切片为冷时, 自动前置 [末日-warmup .. 末日) 的暖机重放。
        min_bars: as-of 门槛; 缺省 `kline.ASOF_MIN_BARS`。

    Returns:
        {"root", "dates", "strategies": {key: {days, advanced, errors, warmed}},
         "ready": {key: {code: [Signal]}},     # 仅 `dates` 当日 ready (P5-③ 行源)
         "skipped": [key]}
    """
    from app.market_cn.auto.core.data.kline import ASOF_MIN_BARS
    from app.market_cn.auto.core.present.runner import DailyRunner, StateStore
    from app.market_cn.auto.strategies.base import _has_fold_contract, signal_of_ready

    lg = logger_ or logger
    min_bars = ASOF_MIN_BARS if min_bars is None else min_bars
    root = os.path.abspath(root or default_root())
    store = StateStore(root)
    runner = DailyRunner(store)

    base_dates = [str(d)[:10] for d in (dates or []) if d]
    stat = {"root": root, "dates": list(base_dates), "warmup": int(warmup or 0),
            "strategies": {}, "ready": {}, "skipped": []}

    for key, strat in (active or {}).items():
        if not _has_fold_contract(strat):
            stat["skipped"].append(key)
            continue
        warm = int(warmup or 0)
        cold = not store.exists(key)
        seq = list(base_dates)
        if warm > 0 and cold and base_dates:
            seq = recent_dates(bars_by_code, base_dates[-1], warm)
        per = {"days": 0, "advanced": 0, "warm_skipped": 0, "errors": [],
               "warmed": bool(warm and cold)}
        warm_set = set(seq) - set(base_dates)
        for d in seq:
            di = asof_inputs(bars_by_code, d, min_bars=min_bars)
            if not di:
                # 暖机日"数据不足"是**预期**（回放早期还没有 30 根），不报错、只计一笔；
                # 目标日无输入才是异常（我们点名要这天，却拿不到数据）⇒ ERROR。
                if d in warm_set:
                    per["warm_skipped"] += 1
                    lg.debug("[present_daily] %s %s 暖机日数据不足, 跳过", key, d)
                else:
                    per["errors"].append(f"{d}: as-of 输入为空 (无票末根 == {d})")
                    lg.error("[present_daily] %s %s 无可用输入, 跳过", key, d)
                continue
            try:
                res = runner.advance_all(strat, d, di)
            except Exception as e:                      # noqa: BLE001 - 单日失败不拖垮其余
                per["errors"].append(f"{d}: {type(e).__name__}: {e}")
                lg.error("[present_daily] %s %s 切片推进失败: %s: %s",
                         key, d, type(e).__name__, e)
                continue
            per["days"] += 1
            per["advanced"] += len(res)
        stat["strategies"][key] = per
        stat["ready"][key] = _ready_of(store, key, bars_by_code, set(base_dates),
                                       signal_of_ready)

    if stat["skipped"]:
        lg.warning("[present_daily] 无折叠契约, 跳过切片: %s", ", ".join(stat["skipped"]))
    try:
        store.flush()                                   # 兜底: advance_all 已按轮 flush
    except Exception as e:                              # noqa: BLE001
        lg.error("[present_daily] 切片 flush 失败: %s", e)
    return stat
