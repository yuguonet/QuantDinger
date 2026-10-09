# auto/ 生产链与工具层 审计报告（只读审计）

> 范围：`app/market_cn/auto/` 下 scan / monitor / store / rebuild / startup / present_daily /
> sampler / probe / api / sched / events / registry / config.json / tools/ 全部 / tools/README.md，
> 顺带 `../scheduler.py` 的 auto 接线。对照《策略宏系统目标态》《改进方案_v2.1》《auto调度架构_策略自描述方案》。
> 审计方式：全文件通读 + 全仓交叉引用（grep import/调用点）；未执行任何写操作/未动 git。
> 行号以 2026-10-09 工作区版本为准。

---

# A. 事实梳理

## A.1 逐文件档案（职责 / 关键签名 / 行数 / 调用关系）

### 生产链

| 文件 | 行数 | 职责（一句话） |
|---|---|---|
| scan.py | 1011 | 盘后全市场扫描（互斥→等数→取数→判定→后处理→落库）+ 盘中窗口扫描 run_scan_knife |
| monitor.py | 849 | 盘中 60s tick 状态机（开盘买入/止损/预确认/收盘出场/15:01 确认/平账/组对账） |
| store.py | 1244 | qd_dragon_signals 事实表 CRUD + 状态机守卫写入 + qd_watchlist/label 投影同步 + 清理 |
| rebuild.py | 1237 | 应然信号集重建（diff/plan/apply）+ 账本重放 + P5-⑤ 投影刷新（写库校准） |
| startup.py | 696 | 启动对账：策略指纹（rules/display 分段）→变更集→退休/补扫+重建校准，指纹成功后落快照 |
| present_daily.py | 221 | 展示层切片每日落盘接线（DailyRunner.advance_all → StateStore）+ 冷切片暖机 |
| sampler.py | 258 | M1 实盘采样器：与判定解耦地自跑 scan_signals 取 trace，组装 sample（LiveSampler） |
| probe.py | 173 | 调试探针层（Probe/DayTrace/sample_feats）：JSONL 存档 + TraceSink 双写（P2 适配器态） |
| api.py | 82 | Flask 蓝图 /api/market/dragon/{today,markers,strategies}（信号展示 API） |
| sched.py | 241 | 调度配置化：expand_times 时刻表、resolve_schedule/daily_fire_ready（事件+钟点就绪判定） |
| events.py | 194 | 数据就绪事件总线：mark_event 持久化 qd_data_events + deriver 自愈 + event_ready |
| registry.py | 163 | 策略元数据单一事实源：strategy_keys/labels/winrate/state 机常量/state_label |
| config.json | 85 | 元数据域配置（开关/限额/调度/标签），含大量 _note 设计记录 |

**scan.py** 关键签名：
`run_scan(days=320, wait_data=True, max_wait_sec=3600, keys=None, target=None)`、
`_run_scan_locked(...)`（同签名）、`_rows_by_scan/_rows_by_scan_day/_rows_by_record(active, bars_by_code, target, stock_info, sampler[, root])`（三个行源 writer）、
`run_scan_knife(max_wait_sec=2400, wait_data=True, keys=None)`、
`apply_unified_prefilter(sigs, bars, code, code_info, strat) -> (kept, last_fails)`、
`finalize_signal_rows(rows, logger=None, env_mode=None)`、`_dedupe_family(rows)`、
`_scan_mutex()`、`_db_try_scan_lock/_db_touch_scan_lock/_db_release_scan_lock`、`_prefetch_bars/_prewarm_pools`、`_mark_daily_scanned(keys, target)`。
调用：**谁调它** → scheduler（`_daily_scan_dispatch`→run_scan、`_dragon_strategy_knife_scan`→run_scan_knife）、startup（`_rebuild_worker/_rescan_worker`→run_scan）、CLI（`python -m ...scan --run/--knife`）；**它调谁** → store、strategies、present_daily（仅 _rows_by_record）、sampler、sched.resolve_schedule、monitor（latest_snapshot/fetch_day_snapshots/_today/_now_hm）、core.data(hub/kline/window_cache)、core.filters/market/market_env/stock_env、core.runtime.evaluate（scan_day writer）。rebuild 反向 import 它的 `_anchor_idx/_dedupe_family/apply_unified_prefilter/finalize_signal_rows`。

**monitor.py** 关键签名：
`run_monitor()`（8 步 tick）、`_progress_map(rows, series, hm, stats=None)`、`_progress_enabled()`、
`evaluate_confirm(row, series_rows)`、`_eval_exit_day_close(row)`、`_bars_with_synth(code, entry_date)`、
`_eval_t_legs(...)`、`latest_snapshot/fetch_day_snapshots/snapshot_day_done`、`_today()/_last_trade_day()/in_window`。
窗口常量：`W_OPEN 09:25-09:35 / W_PRECONF 14:25-14:45 / W_CLOSESIM 14:58-15:06 / W_CONFIRM>=15:01`。
调用：**谁调它** → scheduler（`_dragon_strategy_monitor` → **`run_monitor_safe`——该名字在 monitor.py 中不存在**，见 B-1）、scan.run_scan_knife（快照工具）；**它调谁** → store、strategies、present_daily（default_root）、core.present（RealtimeBranch/StateStore）、core.data.hub、core.display_meta、core.t_legs/market。

**store.py** 关键签名：
`ensure_tables()`、`signal_row(strategy_key, sig, name)`、`rule_row_core(...)`（规则列唯一映射）、
`upsert_scan_signals(trade_date, rows, purge_buy_today=(), max_retries=5, strategies=())`、
`project_rows(strategy_key, code, record, name)`、`load_records/load_projection(root, ...)`、
`set_state/_set_state(..., expect_state, only_unexited)`、`retire_unfilled(keys/ids, ...)`（停用作废唯一实现）、
`purge_stale_detail`、`list_signals(...)`、`get_active_signals/get_watch_pending/get_markers`、
`sync_watchlist_group(active_rows)`、`_display_detail/_label_row/_label_payload/_submit_labels_to_label_layer`、
`cleanup_cutoff/cleanup_old(days=15)`、`today_str()`、`VISIBLE_WINDOW_DAYS=30`。
调用：**谁调它** → scan/monitor/api/rebuild/startup/tools（projection_shadow/doctor/golden_check）；**它调谁** → registry（re-export 元数据）、strategies、app.utils.db、app.watchlist.submit（label 跨层写）。

**rebuild.py** 关键签名：
`build_expected(days=320, window=30, keys, limit, progress)`、`load_actual(win, keys)`、`diff(expected, actual)`、
`build_plan/apply_plan(plan, dry_run)`、`replay_ledger(expected, meta, bars_map, idx_map)`、
`build_ledger_plan(replay, actual, meta)`、`apply_ledger_plan(plan, dry_run)`、
`projection_ledger_refresh(window=30, keys=None, dry_run=True)`（P5-⑤-1）、`render*/main(argv)`。
调用：**谁调它** → startup（`_rb.projection_ledger_refresh`）、tools/projection_shadow（import rebuild, store）、CLI；**它调谁** → scan（prefilter/finalize/_dedupe_family/_stock_info）、store、strategies、registry、core._paths、core.data.hub。

**startup.py** 关键签名：
`reconcile_startup(async_=True, once=True, force=False)`、`fingerprint()`（三段：strategies/rules/display）、
`diff(prev_detail, now_detail)`、`retire_unfilled(keys, reasons)`（store 薄包装）、
`trigger_rebuild(why, background, fingerprint)`、`_rebuild_worker`（run_scan + projection_ledger_refresh，成功后 `_save_snapshot`）。
调用：**谁调它** → `app/__init__.py:460-461`（启动钩子）、CLI `python -m ...startup --force`；**它调谁** → scan、rebuild、store、strategies、qd_auto_strategy_state 表。

**present_daily.py**：`settings/enabled/default_root/warmup_days()`、`asof_inputs(bars_by_code, date, min_bars)`、
`recent_dates(bars_by_code, end, n)`、`persist_days(active, dates, bars_by_code, *, root, warmup, min_bars, logger_)`、`_ready_of(...)`。
调用：**谁调它** → scan._rows_by_record（生产写入口）、monitor（default_root）、tools/projection_shadow、tests；**它调谁** → core.present.runner（DailyRunner/StateStore）、strategies.base（_has_fold_contract/signal_of_ready）、core.data.kline（asof_bars）。

**sampler.py**：`LiveSampler(active, params_override, out_dir, prefilter)`、`observe/close/wants`、
`_self_run`（旧参照）、`_self_run_via_trace`（现役，`LiveSampler._self_run_via_trace = ...` 猴子挂载）、
`STAGE_RANK`（4 策略 taxonomy）、`build_day_sample(...)`、`_accepts_probe`。
调用：**谁调它** → scan（run_scan 两处 writer + knife 不用）；**它调谁** → probe（Probe/DayTrace/sample_feats）、scan.apply_unified_prefilter、strategies。

**probe.py**：`Probe(strategy, tag, out_dir)`（trace/sample/shell/close）、`DayTrace`、`sample_feats(bars, i, code, stock_info)`。
存档 `tmp/probes/<strategy>_<tag>_<ts>_pid<pid>.jsonl`，内部双写 `core.trace.TraceSink`。
调用：**谁调它** → sampler、core/backtest.py:477、strategies/dragon_callback.py:299（DayTrace 兼容）；**它调谁** → core.trace、app.utils.indicators、core.market。

**api.py**：蓝图 `dragon_bp`，`dragon_today/dragon_markers/dragon_strategies`，懒建表 `_ensure()`。
调用：**谁调它** → Flask 路由（挂 /api/market）；**它调谁** → store、strategies。

**sched.py**：`expand_times(windows, interval_sec, date)`（interval>=1 防死循环）、`resolve_schedule(key)`、
`all_schedules(date)`、`resolve_daily_fire(key)`（after_events+fire_at 合并）、`enabled_daily_keys()`、
`daily_fire_ready(key, now_hm, date) -> (bool, reason)`、`DAILY_FALLBACK_FIRE="18:30"`。
调用：**谁调它** → scheduler（`_daily_scan_dispatch`、`_intraday_trigger_slots`）、scan.run_scan_knife（resolve_schedule）；**它调谁** → events（missing_events）、strategies。

**events.py**：`mark_event(name, date)`、`mark_if_ready`、`event_ready(name, date)`、`missing_events(names, date)`、
`KNOWN_EVENTS`（6 个）、`_DERIVERS`（3 个：daily_1d/lhb/northbound）、qd_data_events 表（60 天滚动清理）。
调用：**谁调它** → scheduler（`_mark_event` ×5 处）、sched（missing_events）；**它调谁** → core.data.kline、dragon_tiger_store、index。

**registry.py**：`strategy_keys()`（config ∪ autodiscover 并集）、`enabled_keys()`、`strategy_labels()`（config label > 插件 name）、
`strategy_winrate/state_label`、状态常量 `S_WATCH_PENDING…S_EXPIRED`、`ACTIVE_GROUP_STATES`。
调用：**谁调它** → store（re-export 全部）、rebuild、tools/doctor；**它调谁** → strategies.load_config。

### tools/（18 模块 + __init__ + README）

| 模块 | 行数 | 用途 / 入口 | 主要依赖 |
|---|---|---|---|
| README.md | 84 | 唯一权威工具索引（自述 18 模块；旧文档 21 个清单已废） | — |
| why.py | 239 | 多日"为什么没信号"摘要 / --date 转 debug / --params 试调 | debug |
| debug.py | 608 | 单候选单日门级追踪（引擎A门表 evaluate_gates；引擎B replay+TraceCollector） | core.runtime.evaluate/functions、core.replay、strategies.base |
| replay.py | 155 | 单股单策略逐笔重放（backtest_stock 薄壳→core.replay） | core.data.hub、strategies |
| chat_why.py | 383 | 自然语言→转调 why/debug/doctor + LLM 解读（可退役前端） | tools.why/debug/doctor |
| explain.py | 477 | 门表机器可读报告（JSON+MD）+ 全市场逐门漏斗 run_explain_backtest | core.runtime、rule_stats._metrics |
| gate_funnel.py | 240 | explain 漏斗的可调用 dict 封装 | explain、rule_stats |
| gate_try.py | 503 | 第 at 道门处批量试算候选规则（expr.evaluate，内存求值） | core.runtime.expr、gate_funnel |
| rule_stats.py | 392 | 入场规则归因（漏斗表/判别力表）——多工具统计底座 | sampler.STAGE_RANK、probe 存档 |
| rule_audit.py | 399 | 门级拦截表+判别力+换序稳定性 | rule_stats、param_scan._seg_stats |
| param_scan.py | 607 | 参数网格扫描+敏感性报告（OFAT/whatif） | core.backtest.run_all、pool_check 输出 |
| pool_check.py | 434 | 信号池级复验闸门（归因→prefilter 前必验） | hub 取数、rule_stats |
| proposal.py | 458 | LLM 建议产物 schema 校验/应用/回滚（yaml 最小改动） | strategies yaml 文件 |
| doctor.py | 251 | config↔注册表↔磁盘插件↔DB 三方对账 + 独立性快检 | registry、strategies、DB |
| path_parity.py | 402 | 三路径同源自检（scan_signals / scan_days / backtest_stock） | core.backtest、strategies |
| present_verify.py | 310 | 展示层单票对账（fold 输出 vs 旧路径逐位，PASS/FAIL 硬退出） | core.present.runner |
| projection_shadow.py | 565 | P5 影子对账：--replay 回放「scan_days vs Record.ready」+ DB 双向 diff（--apply 落库） | present_daily.persist_days、store.load_projection、rebuild |
| golden_check.py | 280 | 回放引擎 golden 不变量（T+1/价格/收益自洽）+ --truth 对账库 ready 日 | core.replay、DB |
| legacy_trade_diff.py | 300 | replay trades vs 旧 backtest_stock 逐笔对拍（本地 / --ref 参照树子进程） | subprocess、ref_tree（config.ref_tree） |

## A.2 生产链数据流（实测代码路径）

### 盘后链
1. **调度**：`scheduler.py` Task `daily_scan`（60s 自适应间隔，`_daily_scan_interval`）→ `_daily_scan_dispatch`（scheduler.py:311-358）：
   target=`last_finish_trading_day()`；keys=`sched.enabled_daily_keys()`（enabled 且 kind=daily_close）；
   就绪判定 `sched.daily_fire_ready(key, date=target)` = ① ScanSpec/config `fire_at` 钟点 → ② `events.missing_events(after_events)`（缺则等；过 `DAILY_FALLBACK_FIRE=18:30` 放行，补跑日直接放行）；
   → `run_scan(keys=ready, target=target)`；成功后 `mark_scheduler_task_done("daily_scan@<key>", target)` + 进程内 `_daily_scanned`。
   数据事件由数据任务打点：post_market_batch→`minute_1m`/`daily_1d`（scheduler.py:458/476）、index_fflow→`index_fflow`(:465)、dragon_hot_daily→`lhb`(:244)、fund_flow_daily→`fund_flow`(:687)、northbound(:498)。
2. **run_scan**（scan.py:458-641）：进程内锁+`qd_scan_lock` DB 行锁（900s stale + `touch` 续约）→ `wait_data`（000001 末根到 target，300s 轮询）→ `_prefetch_bars`（window_cache→batch→逐票三级降级）→ `_prewarm_pools`（g56 池）→ 逐票 `asof_bars(bars, target)` 收敛（**唯一不变量：末根==target**）→ 行源分发（`WRITER = strategies.scan_writer()`，**当前 config = "scan_day"**）→ `market_flow_gate` 环境门 → `finalize_signal_rows`（同族去重→trend 策略 env 门→daily_limit 截断）→ `stock_event_gate`（纸面）→ `store.upsert_scan_signals` → `sync_watchlist_group` → `cleanup_old(15)` → `_mark_daily_scanned`。
3. **三个行源 writer**（scan.py:573-580 分发）：
   - `scan` = `_rows_by_scan`：逐票 `strat.scan_days`（折叠内核）→ prefilter → signal_row（回滚位）；
   - `scan_day` = `_rows_by_scan_day`（**现役**）：`core.runtime.evaluate.scan_day(门表 spec, ...)` 门表单日判定 → prefilter → signal_row；
   - `record` = `_rows_by_record`：`present_daily.persist_days`（fold 切片落盘）→ 行源=当日 `Record.ready`；失败策略退回直判。
4. **present_daily**（仅 record writer）：`persist_days` 对冷切片暖机 `warmup=25` 日（事件覆盖≥影子窗口 + g56 ledger ROLL=20），逐日 `DailyRunner.advance_all` 写 StateStore 切片（`core/_paths.PRESENT_STATE_DIR`），行源 `_ready_of` 从 Record.events 读回 ready。
5. **startup 链**（app/__init__.py:460 → reconcile_startup）：指纹（config strategies 段 + AST import 闭包 .py sha + yaml/display sha）→ diff → ① 停用/移除 → `store.retire_unfilled`（只动 watch_pending 且 entry_date IS NULL）；② 启用/参数/规则变更 → 后台 `_rebuild_worker`：`run_scan` 补扫 → `rebuild.projection_ledger_refresh(window=20, dry_run=False)`（Record 投影→build_ledger_plan→apply_ledger_plan）→ 成功才 `_save_snapshot` 推进指纹（失败下次重试）。

### 盘中链
- **monitor tick**（应由 Task `dragon_monitor` 60s 驱动，**当前接线断裂见 B-1**）：run_monitor 8 步：
  ①0 滞留 buy_today 自愈→holding；① 每 tick 拉 buy/hold/exit/pending 快照（`VISIBLE_WINDOW_DAYS=30`，today 显式传入同一日期线）；1b stale 观察票过期（trade_date<target → expired，出窗口条件）；1 开盘窗口 09:25-09:35：禁用策略存量 pending 作废（retire_unfilled）→ `_progress_map`（新源）或 `entry_decision` gap 判定 → `quality_key` 排序 → `daily_limit` 名额（0=不截断）→ buy_today（entry_date/price=快照开盘价、stop_price）/expired；
  2 盘中 09:35-15:00：硬止损（px<=stop_px→exit_today，**永不迁新源**）+ 做T 意图 + `exit_decision(live)` 或 progress exit；3 14:25-14:45 预确认（confirm_decision→pre_confirm 档位）；4 14:58-15:06 收盘重放（`_eval_exit_day_close` day_close 模式或 progress exit）；5 ≥15:01 确认（`snapshot_day_done` 闸门；progress 判定或 confirm_decision→holding/exit_today，`no_judgment` 统计回退行数）；6 exit_today 平账→closed（隔日开盘价记账；exit_exec_same_day 策略 14:55 后保标记价；exit_date 已填直接收口）；7 purge_stale_detail（pre_confirm 等瞬时标记）；8 `sync_watchlist_group`。
- **knife 盘中窗口**（Task `knife_scan` once_per_slot）：触发点=`sched.all_schedules()` 首拍分批（≤30min 合并，现 14:30 → knife_catch+tail_oversold）；run_scan_knife：等 start_hm → 14:50~15:00 每分钟滚动预览（`intraday_shortlist`→`scan_signals(ctx=快照)`→U1~U4→signal_row(state=buy_today, entry/stop)）→ 15:00 终审（清未定价预览行）→ `upsert_scan_signals(purge_buy_today=本批)`。
- **判定点的双源**（monitor_progress.enabled=true 时）：`_progress_map` 用 `RealtimeBranch(store, {key: strat}).tick(...)` 读**当日切片 progress**；「拿不到判定」不进字典→回退旧 confirm/exit/entry_decision；唯一无判定入典形态 = 活仓 tick `stage="hold"`。realtime.py 头注：**实时分支不写回 fold**，切片只由预处理（persist_days）推进。

### qd_dragon_signals ↔ qd_watchlist
- **事实源 = qd_dragon_signals**（状态机全量+历史，UNIQUE(trade_date, strategy, code, entry_style)）；
  **qd_watchlist「自动策略组」= 活跃投影**（ACTIVE_GROUP_STATES 四态），由 `sync_watchlist_group` 全量对账（缺失 INSERT / 变更 UPDATE（按 id）/ 多余 DELETE），幂等，每 tick/每轮扫描调用。
- 折叠/删除粒度注意：watchlist 唯一键 (user_id, market, symbol, group_name) **不含 strategy** ⇒ 同票多策略共享物理一行；变更检测折叠键已改 (code, strategy)（store.py:1037-1044），但 DELETE 仍按 code 粒度（store.py:1064-1072，防误删共享行）。
- 第三条投影：`_submit_labels_to_label_layer` → `app.watchlist.submit("auto", ...)`（grade=3，TTL=2 交易日）——方案允许的唯一跨层写边，失败只记日志。
- 变更检测 `v` = `_display_detail` 全字段稳定哈希（A6）。

## A.3 config.json 顶层键语义

| 键 | 语义（读取入口） |
|---|---|
| `version` | 无任何代码读取（**死键**，见 B-12） |
| `live_probe` | M1 实盘采样总开关（`strategies.live_probe_enabled`，默认 true）→ scan 建 LiveSampler 存档 tmp/probes/ |
| `present_persist` | {enabled, warmup:25}：切片每日落盘开关+冷切片暖机日数（`strategies.present_persist_settings`→present_daily）。⚠ `_note` 声称"enabled=true ⇒ scan 行改由 Record.ready 投影"——**实际行源由 scan_writer 决定**（见 B-2） |
| `scan_writer` | 行源/判定引擎开关（`strategies.scan_writer`）："scan_day"（门表 evaluate.scan_day，**现役**）\|"record"（fold 切片→Record.ready）\|"scan"（scan_days 回滚位）；缺省 "record" |
| `ref_tree` | 外部旧版参照树根（`strategies.ref_tree_settings`；env AUTO_SLIM_REF 优先）——P6 删旧代码的可逆保障，对拍组 skip≠通过 |
| `monitor_progress` | monitor 四判定点是否读 RealtimeBranch progress（`strategies.monitor_progress_settings`；缺省关→逐字回退旧三决策） |
| `_comment` | 元数据域总注释（label/daily_limit/params/enabled 迁移史/schedule/live_probe/winrate/state_labels 语义） |
| `schedule` | per-strategy 盘中分段调度覆盖（windows=[首次,截止] 含端点, interval_sec）→ sched.resolve_schedule；scheduler knife_scan 触发点取各策略 first 分批 |
| `strategies` | 系略元数据：label（显示名，优先级 config>插件 name）、daily_limit（入库上限，0=不截断，默认20）、winrate（前端展示）、_params_note/_winrate_note（设计记录）；keys 并集=store 查询/清理范围；enabled 已迁 yaml meta.enabled，config enabled 仅兜底 |
| `state_labels` | 信号状态中文文案（前端映射兜底） |
| `_market_env_note` | market_env 挂 strategies.<key>.market_env（off/trend/counter），trend 策略弱市 reduce/halt |
| `_daily_limit_note` | daily_limit 事实源声明 |

## A.4 tools/ 重叠与失效面

**重叠（有意分工，README 已声明口径）**：
- 调试四入口 why / debug / replay / chat_why（why 定日期→debug 追单日→replay 逐笔→chat_why 对话壳）；
- 漏斗族 explain / gate_funnel / gate_try（gate_funnel 是 explain 的 dict 封装，gate_try 吃 explain 的探针行）；
- 统计族 rule_stats（底座：_load_rows/_metrics 被 rule_audit/gate_try/gate_funnel 引用）/ rule_audit；
- 对拍族 5 个：path_parity（三路径判定同源）、present_verify（fold vs 旧路径单票）、projection_shadow（Record 投影 vs scan_days/库）、golden_check（replay 不变量+ready 日对账）、legacy_trade_diff（trades vs 旧回测）——口径各不相同但概念重叠，出问题时要跑多把闸门。

**失效/半失效**：
1. `legacy_trade_diff` **本地模式**已失效：旧侧定义="策略覆写的 backtest_stock（第二份回测，待删对象）"（legacy_trade_diff.py:15, 149-150），但链 A 退役后**无任何策略再覆写 backtest_stock**（strategies/*.py 均无 def backtest_stock；base.py:571 薄壳→core.replay）⇒ 本地对拍=自己对自己，恒绿假证据。`--ref` 参照树子进程模式（:110-140）仍是有效执行体。
2. `rule_stats` 无 `--probe-file` 时**不可重跑**（rule_stats.py:355-362 明示回测侧 probe 采样已退役，return 2）；且 `STAGE_RANK`（sampler.py:223-233）**缺 g56** ⇒ g56 归因直接失败（见 B-8）。
3. `debug.py` 对无折叠契约策略显式拒答（debug.py:345-352，链 A 通用兜底引擎已退役）——设计使然。
4. 未发现对已删除模块（pipeline.py/strategy_funcs.py/g1_*）的任何残留引用（全 tools+生产链 grep 0 命中）；`rule_audit.py:20/61` docstring 引用的 `_probe_day_light / peak_maps / _signal_core_dbg` 已不存在（纯文档漂移，不炸）。
5. `strategy_cli.py`（backend 根）仍指向 rule_stats 的 stats 子命令——存在，未断。

## A.5 sched / events / scheduler 三方现状 vs《auto调度架构》

**已落地**：schedule/ScanSpec 自描述驱动 knife_scan 分批触发（scheduler.py:560-630，SLOT_GAP_MIN 合并、失败退避重试、按天缓存）；daily_scan 事件驱动分发（after_events+fire_at，取代 17:25 硬编码，scheduler.py:709-711 注释）；`qd_data_events` 持久化 + deriver 重启自愈；daily_scan 完成标记 qd_scheduler_done（跨重启去重）；`expand_times` 防死循环校验；补跑日（date<today）不再被墙钟卡死（sched.py:196-201）。
**未落地/半落地**：
- 方案里 `after_data` 的**per-strategy 超时声明**未做——只有一个全局 `DAILY_FALLBACK_FIRE=18:30`（sched.py:163）；
- **monitor 的判定点/时刻窗**未进策略自描述（W_OPEN/W_PRECONF/W_CLOSESIM/W_CONFIRM 仍是 monitor.py:44-48 常量）；knife 预览收口 "15:00"/fresh_cut 硬编码（scan.py:958-967）；
- 事件流**尚未成为唯一数据就绪事实**：scan.wait_data 仍以 000001 kline 轮询兜底（scan.py:339-342, 535-545），events 只作触发条件；
- run_monitor_safe 这一方案约定的对外 API 与实现名（run_monitor）不一致（B-1）。

---

# B. 问题审计

## 🔴 严重

### B-1. 盘中状态机接线断裂：scheduler 引用不存在的 `monitor.run_monitor_safe`
- **证据**：`app/market_cn/scheduler.py:363-364`：
  `from app.market_cn.auto.monitor import run_monitor_safe` / `run_monitor_safe()`；
  `monitor.py` 全文（849 行）只有 `def run_monitor():`（monitor.py:389），**无 run_monitor_safe**（全仓 grep 仅调度架构文档 2 处 + scheduler 2 处）。
- **后果**：Task `dragon_monitor`（60s）每次触发都在 import 处抛 ImportError，被 `_worker` 的 `except Exception`（scheduler.py:770-773）吃掉、只留一条"执行失败"日志——**开盘 gap 买入、盘中止损、预确认、收盘出场、15:01 确认、exit 平账、组对账整条盘中链在生产上停摆**；信号永远停在 watch_pending/buy_today，靠 stale 过期兜底。与"监控是资金安全链"的定位直接冲突。
- **修法方向**：补 `run_monitor_safe = run_monitor`（或 scheduler 改调 run_monitor）；并检查是否本应在重构中保留的安全包装被误删。

### B-2. 三 writer 并存 + 双开关矛盾：`present_persist.enabled=true` 实际无人消费，生产切片无人推进 → monitor_progress 与启动投影刷新双双空转
- **证据链**：
  1. 行源由 `scan_writer` 决定：`scan.py:508` `WRITER = strat_reg.scan_writer()`；config.json:9 `"scan_writer": "scan_day"` ⇒ 走 `_rows_by_scan_day`（scan.py:573-577），判定引擎=门表 `core.runtime.evaluate.scan_day`。
  2. 切片唯一生产写入口 `present_daily.persist_days` 只被 `_rows_by_record` 调用（scan.py:732）——**scan_day/scan writer 下从不执行**；其余调用方仅 tools/projection_shadow 与 tests。
  3. `monitor._progress_map`（monitor.py:331-386，monitor_progress.enabled=true，config.json:14-16）读同一 StateStore 切片，而 `core/present/realtime.py:20` 明文"不写回 fold，切片只由预处理推进" ⇒ 切片缺失/stale ⇒ progress 恒空 ⇒ **四判定点恒回退旧 confirm/exit/entry_decision**（安全但空转），config 承诺的观察灯 `no_judgment==0 / confirm_prog_hit>0` 永远不可能过线。
  4. `startup._rebuild_worker`（startup.py:513）→ `rebuild.projection_ledger_refresh`（rebuild.py:964 读 `PRESENT_STATE_DIR`）→ 切片无投影时返回 `(None, {"error": "切片无投影"})`（rebuild.py:973-975）→ `_RebuildIncomplete` → **指纹不推进**（startup.py:527-537）⇒ 每次重启都重复"补扫+刷新失败"。
  5. `present_daily.enabled()`（present_persist.enabled 的读取口）**生产零消费者**（全仓只有 tests/present/*）；config.json:7 的 `_note`（"2026-10-07 已切：scan 的 signals 行改由当日 Record.ready 投影"）与 config.json:9 自相矛盾。
- **定性**：设计目标态（"fold/Record 是唯一判定"）被 scan_writer="scan_day" 旁路，且迁移期开关（present_persist）与永久开关（scan_writer）语义重叠、状态互相打架。当前组合下 Record 事实流在生产链上是**断的**。
- **修法方向**：要么 scan_day writer 同步调 persist_days（切片必须有人推），要么 scan_writer 切回 "record"；同时收敛掉 present_persist.enabled 与 scan_writer 的重叠语义，更新 config _note。

### B-3. startup 指纹没跟上「enabled/params 迁 yaml」：改策略宏开关/参数不再触发任何补偿
- **证据**：enabled 事实源已迁 `<key>.yaml meta.enabled`（strategies/__init__.py:171-186；config.json:18 明文"本文件不再为有 yaml 的策略写 enabled 键"）；参数事实源已迁 yaml params（config 各 `_params_note`，B-D3/D4）。
  但 `startup.fingerprint()` 仍只从 config 读 `"enabled": bool(c.get("enabled", False))` 与 `"params"`（startup.py:263-283）⇒ 5 个策略的 enabled 恒 False、params 恒 {}；
  `strategies/*.yaml` 被归入**展示层**（`_iter_display_files`，startup.py:242-258）⇒ 改 yaml 只得到 `display_changed` → 走"仅展示层变更 → **无需重建**，指纹已推进"（startup.py:678-680）。
- **后果**：
  ① 在 yaml 里禁用策略（§3.5"改宏即开关"）→ startup **不再触发 retire_unfilled**（2026-09-23 事故的补偿路径失效）——仅靠 monitor 开盘窗口 sweep（monitor.py:445-470）与 stale 过期次日兜底；
  ② 改 yaml params → 不触发 rebuild 校准 ⇒ 窗口内历史行仍是旧参数判定，正是 startup 模块 docstring 自述要消灭的"改了规则界面还是老样子"。
- **修法方向**：fingerprint 的 strategies 段改读 `is_enabled/yaml params`；把 yaml 按"含 meta.enabled/params/gates 的文件"划入 rules 段（纯展示字段可留 display），或至少 enabled/params 变化进 rules_changed。

## 🟡 中

### B-4. rebuild `--json` 输出必崩：三元格式串作用于四元键（A2 改键后漏改）
- **证据**：`rebuild.py:1226-1228`：
  `"missing": ["%s|%s|%s" % k for k in d["missing"]]`、`"ghost": ["%s|%s|%s" % k ...]`、`"drift": [{"key": "%s|%s|%s" % k ...}]`；
  而 expected/actual/diff 的键自 2026-10-07 A2 起是**四元组** (trade_date, strategy, code, entry_style)（build_expected:285、load_actual:345、store.load_projection:597）⇒ `"%s|%s|%s" % 4元组` 抛 `TypeError: not all arguments converted`，`--json` 落盘路径整个失败（render 已改 k[0..2] 显式索引，唯 json 分支漏改）。

### B-5. upsert "先 DELETE 后 INSERT"：判定异常/取数缺失的票信号被删不补回（部分失败=部分信号蒸发）
- **证据**：`store.upsert_scan_signals` 前置 `DELETE ... state='watch_pending' AND trade_date=目标 AND strategy=ANY(scope)`（store.py:325-333）后只插本轮 rows；判定异常票被跳过（scan.py:625-638 / 661-672 `continue`），该票昨日同 trade_date 的 watch_pending 行被删且无新行顶上——只有 err_by_key 汇总 ERROR（scan.py:584-592），无行级恢复。
- **加重**：`_rows_by_record` 返回值只有 rows（scan.py:717-772），**不回传 err_by_key** ⇒ record writer 下 `_run_scan_locked` 的异常汇总恒为空，连那条 ERROR 都不会打（与其 P2"静默断链"修复目标相悖）。
- 附：A11 supersede 是逐行 UPDATE（store.py:353-377），N 条新提名 = N 条 UPDATE（N+1 往返，量大时拖慢扫描）。

### B-6. 重放/投影写库仍会用判定列覆盖操作列（entry_price/stop_price/confirm_date）
- **证据**：`build_ledger_plan` 的 keep_marked 只保护 **exit_reason 非空**行、keep_actual 只保护"库有入场、重放无入场"行（rebuild.py:774-795）；其余进 `upsert`，`apply_ledger_plan` 的 UPDATE 整列覆盖 `state/entry_date/entry_price/stop_price/exit_date/exit_price/confirm_date`（rebuild.py:884-896）。
- **后果**：monitor 按快照实价记的 entry_price（monitor.py:522-527）会被重放的理论 D1 开盘价覆写（未标出场的持仓/未平仓行）；`vanish_settled` 只在报告里列警告（rebuild.py:1024-1031），不拦截。"操作事实不可重建"（store.py:447-453 的分工声明）只做到了 exit 标记一半。
- 定性：方向已对（A2 守卫），残留口子仍在；--ledger/投影刷新 `dry_run=False` 都走这条 UPDATE。

### B-7. 两套"今天"时钟残留：`snapshot_day_done` 用 DB UTC 的 CURRENT_DATE
- **证据**：`monitor.py:116-124` `WHERE time::date = CURRENT_DATE`（DB 会话 UTC，db_postgres.py:144）；`monitor._today` docstring（monitor.py:60-72）自己点名该遗留未修。
- **后果**：北京 00:00~07:59 该闸门判错日 → step5 的 15:01 确认前置 `snapshot_day_done()` 可能误判（当日快照没落齐/落了昨日）→ 确认停摆或误触发。A7 只统一了 list_signals 窗口与 _today，此处漏网。

### B-8. STAGE_RANK 缺 g56 + 退役名残留：归因工具对 g56 直接不可用
- **证据**：`sampler.py:223-233` STAGE_RANK 只登记 break/dragon_callback/knife_catch/tail_oversold；`rule_stats.py:333-335` `rank_map = STAGE_RANK.get(...) or getattr(strat, "PROBE_STAGE_RANK", {})`——PROBE_STAGE_RANK 在 strategies/core 已无任何定义（grep 0 命中）⇒ g56 报"无 PROBE_STAGE_RANK (未接探针?)" return 1；错误文案还指向已退役概念。同类：rule_audit.py:20/61 docstring 引用已不存在的 `_probe_day_light/peak_maps/_signal_core_dbg`。

### B-9. legacy_trade_diff 本地模式=自我对拍（假绿风险）
- **证据**：legacy_trade_diff.py:15（"旧回测 = 策略覆写的 backtest_stock（待删对象）"）、:90-94、:149-150（`s.backtest_stock(...)` 当旧侧）；链 A 退役后策略均无覆写，base.py:571 薄壳即 core.replay ⇒ 两侧同源恒等，退出码 0 = "可删"的通行证失去意义。仅 `--ref` 模式（:110-140 + config.ref_tree）仍是真实对拍。

### B-10. 调度自描述只落到扫描触发点：monitor 判定时刻窗仍是硬编码常量
- **证据**：monitor.py:44-48（W_OPEN_LO/HI、W_PRECONF、W_CLOSESIM、W_CONFIRM 全常量）；scan.py:958-967（滚动预览 `while _now_hm_str() < "15:00"`、`fresh_cut = f"{today} 15:00"` 写死）——策略 ScanSpec/config schedule 只描述扫描拍，不描述确认/出场/预确认锚点。与《auto调度架构》"策略自描述、消灭硬编码时刻特判"的目标态相悖（monitor_progress._note 提到的"exec 补 15:01"锚也散落在各策略宏）。

### B-11. events 事件面不完整：6 个事件只有 3 个 deriver，mark 失败静默降级为"等到 18:30"
- **证据**：events.py:36-45 KNOWN_EVENTS 6 个；events.py:68-72 `_DERIVERS` 仅 daily_1d/lhb/northbound；`_db_mark` 失败只 warning（events.py:130-136）；minute_1m/fund_flow/index_fflow 无自愈 ⇒ 重启丢 mark / mark 失败时 `daily_fire_ready` 只能等全局 `DAILY_FALLBACK_FIRE=18:30` 放行（sched.py:163, 213-218），盘后扫描整体延后且无告警区分"数据没来"还是"事件没打"。

## 🟢 轻微

### B-12. config 死键与文档漂移
- `config.json:2 "version": 1` 全仓无读取者（死键）。
- `config.json:18 _comment` 仍写"params=策略参数覆盖（优先级: config > 代码 default_params）"，与 strategies/__init__.py:194-208（yaml 优先、config 仅兜底）及各 `_params_note`（"config.params 已删除"）矛盾。
- registry.py:44 注释"三策略共用: 龙回头/V1/断板"、scan.py:13"现网 dragon_callback / v1 / break / g56 等"——v1/relay3/lead_chase 已 2026-10-09 退役（registry.py:46-48），文档未同步。

### B-13. sampler 文档示例与签名不符 + 过渡双路径
- sampler.py:55-63 类 docstring 用法示例调用 `observe(..., sigs=sigs, kept=kept, u_fails=u_fails, independent=True)`，实际 `observe(self, key, strategy, code, bars, stock_info)`（sampler.py:94）——照抄即 TypeError。
- `_self_run`（sampler.py:138-166）与 `_self_run_via_trace`（:184）双路径并存（自述"P6 清"）；probe.py 的 JSONL+TraceSink 双写同理（P2 适配器待退役）——迁移期冗余，属已声明债务。

### B-14. 零散异常吞噬
- scan.finalize_signal_rows reduce 档 `except Exception: pass`（scan.py:441-445）：apply_env_to_limit 失败时静默用原 cap，无日志。
- monitor.snapshot_day_done `except Exception: return False`（monitor.py:116-124）无痕。
- api.py:41/68/80 `msg=str(e)` 直接回客户端（内部异常文本外泄）。
- `_rows_by_record` 直判回退分支逐票仅 warning、无汇总（B-5 已并述）。

### B-15. daily_limit 口径两处实现
- `finalize_signal_rows`（scan.py:435-447，盘后：score 降序截断+env 减半）与 `_scan_cycle`（scan.py:925-931，盘中：手工复制"只截当前策略"）语义重复；monitor 开盘名额（monitor.py:512-525，排名配额）是第三处同名概念（准入 vs 入库截断）。改限额口径需人肉同步，注释已自认不复用。

### B-16. tmp/probes 无卫生治理
- sampler.py:8-12 自述已积累 371 文件 ≈9GB；probe.py 落盘无轮转/上限/清理（cleanup_old 只管 DB 行）。长期运行磁盘风险。

---

## 附：核对过的"无问题"面（供文档重写引用）
- U1~U4 已单实现：`apply_unified_prefilter`（scan.py:367）为 run_scan/run_scan_knife/rebuild/core.backtest/sampler 共用；`_anchor_idx` 盘后/盘中共用。
- "停用策略作废"SQL 已单实现：`store.retire_unfilled`（store.py:629）收编 monitor/startup/rebuild 三处；`None` 失败语义三处调用方均已正确判 `is None`。
- `rule_row_core` 是规则列唯一映射（signal_row/project_rows 双入口共用，store.py:226）。
- upsert 的 A1/A11 守卫（entry_date 非空或 expired 不回滚 state/extra；跨日同名 pending supersede）已闭合"补扫打回 holding"的主路径；expired 未入场行 2026-10-07 也已入守卫（store.py:388-401）。
- 扫描互斥（进程锁+DB 行锁+续约防 stale 误回收）与"完成标记同 target"已闭合 2026-09-18 死锁/重复扫问题（scan.py:44-158, _mark_daily_scanned）。
- daily_fire_ready 补跑模式（date<today 不卡墙钟）、expand_times interval>=1 防死循环、run_scan_knife 分批 keys 作用域限定，均为 2026-09-29 审计修复后闭合项。
