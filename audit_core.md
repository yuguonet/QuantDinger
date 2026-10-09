# auto/core/ L0 内核审计报告（只读审计，2026-10-09）

审计对象：`QuantDinger-src/backend_api_python/app/market_cn/auto/core/` 全部（39 个 .py，共 9,261 行）。
对照文档：《策略宏系统目标态.md》《改进方案_v2.1.md》。
所有行号均为**该文件自身**行号（已逐条 grep 复核）。

---

# A. 事实梳理（供设计文档重写）

## A1. 逐文件清单

### 根级

| 文件 | 行 | 职责一句话 | 关键符号 | 被谁调用 |
|---|---|---|---|---|
| `__init__.py` | 6 | L0 分层声明（"core 内不出现任何市场专属常量"） | — | 包入口 |
| `_paths.py` | 69 | 项目根锚点（唯一路径事实源）+ .env 惰性加载 | `PROJECT_ROOT/AUTO_DIR/STRATEGY_DIR/MARKETS_DIR/ENV_FILE/CACHE_ROOT/PRESENT_STATE_DIR`, `load_env_first_found()` | hub(512)、frames、runner、evaluate(75)、backtest CLI(459)、tools/debug、tools/projection_shadow |
| `market.py` | 283 | MarketSpec 接口 + 涨跌停/分板原语（spec 驱动，默认市场注入） | `MarketSpec`(bands 摊平元组 `(up_eff,dn_factor,dn_tol)`、`nominal`、`board_of/board_name/_band/nominal_up_pct`)；`set_default_market/default_market/get_board_type/get_board_name/is_limit_up/find_limit_ups/limit_dn_price/limit_up_price/limit_dn_tol/build_nominal/build_bands` | exec、exit_engines、filters、functions、gate_stdlib、quality、cross_section、entry_modes、strategies/*、adapters/markets/registry |
| `exec.py` | 160 | 成交语义原子原语（唯一实现，框架不变量 5 条） | `is_one_word_limit_dn/fill_on_gap/fill_blocked_by_limit_dn/fill_intraday(bar,trigger,side,dn,up)/replay_sell_intraday(minute_bars,...)->(fill,mi,peak_after)/_slot_ohlc` | exit_engines(48)、entry_modes(276)、break.py(728-934) |
| `exit_engines.py` | 496 | 参数化出场引擎（hold+stop / trail+stop / 顺延强平 / 连板封住） | `run_hold_stop(bars,entry_idx,entry_price,*,hold_days,stop_loss,board_type,spec,side,with_reason)`、`run_trail_stop(...,trailing_stop,trails,peak_exit,pre_exit,extra_diag,stop_at_idx,use_trig_prev)`、`defer_force_open(...,last_unfilled,pending_dn,stop_at_idx)`、`run_limit_seal(...,break_sell_ratio,trail_after_limit,hold_days_max)`、`_min_sell_day/_dn_at` | dragon_callback(472)、g56(193)、break(963 defer_force_open)、_archive/v1+relay3；门禁 tests/test_exit_single_source.py |
| `exit_modes.py` | 88 | "M2 出场模式注册/分派"（YAML `exit.mode` 的承诺入口） | `EXIT_MODES` 表、`register_exit(mode,fn)`、`run_exit(mode,*,bars,entry_idx,entry_price,code,board_type,params,diag)`、`_bp(params,board_type,name)`、`_d1_open` | `register_exit` 被 break/dragon/g56/_archive 调；`_bp` 被 break(602,1251,1294)/g56(1125) 用；**run_exit/EXIT_MODES 零调用者**（见 B1-1） |
| `entry_modes.py` | 154 | "M2 入场模式"（close/open/intraday + gap 过滤） | `resolve_entry(cfg,bars,i,board_type,params)->(site,reason)`、`_resolve/_resolve_entry_intraday/_f`、`_BOARD_KEYS` | **全树零调用者**（B1-2） |
| `filters.py` | 53 | U1~U4 统一前置过滤（防杂毛） | `PREFILTER_PARAMS`、`unified_prefilter(bars,i,code,code_info,market)->(ok,fails)` | rebuild(196)、scan.apply_unified_prefilter、sampler |
| `display_meta.py` | 58 | 判定结果→展示档位映射（零判定、指纹排除） | `CONFIRM_LEVELS`、`confirm_level_of(dec)` | monitor、startup、store、strategies/base |
| `backtest.py` | 493 | 全市场回测薄编排（主路径=replay 折叠；1m 精修腿） | `run_all(...)`、`run_all_intraday(strat,...)`、`_refine_intraday`、`_exec_trigger_mis`、`_summary`、`is_st_stock`(恒 False)、`_run_meta` | strategy_cli/tools/golden_check 等 CLI 入口 |
| `increm.py` | 356 | 统一增量协议（snapshot/step/value）+ 通用滚动核 | 滚动核 `roll_sum/sma/rsi/atr_pct/boll_pctb/pctl_roll/window_of/anchor_step`；`IncrKernel`+`Macd/Atr/Boll/Kdj/Rsi/RingKernel`、`register/get/keys/kernels` | 滚动核→cross_section(48-62)；**Kernel 类/注册表仅 tests/present/test_increm.py**（B1-3） |
| `trace.py` | 95 | 门原因通道 TraceSink（收集端归内核） | `TraceSink(code,strategy)`：`begin_day/gate(ok,reason,**meta)/note/to_jsonl/dump_jsonl/clear`；payload 保留键名表（模块头） | replay.TraceCollector、dragon/g56/break/knife/tail 经 `ctx["_trace"]`、probe、tools/explain、tests |
| `t_legs.py` | 261 | 做T 腿意图骨架（只产 TradeIntent，不成交/不记账） | `TradeIntent/LegSpec/TLegsConfig`、`t_constraints(spec)`、`eval_t_legs(position,ctx,config,*,hold_day,spec,already)`、`explain_constraints` | monitor(215,579)、strategies/base.t_leg_intents(322)、tools/doctor(174)；**回测/replay 不消费** |
| `market_env.py` | 154 | 大盘资金流环境门（规则门，fail-open） | `DEFAULT_RULES`、`market_flow_gate(as_of,days,rules)->{mode:full/reduce/halt,...}`、`apply_env_to_limit(daily_limit,mode)` | scan(437,597) |
| `stock_env.py` | 122 | 个股资金流/龙虎榜环境门（规则门，fail-open） | `DEFAULT_RULES`、`stock_flow_gate/lhb_gate/stock_event_gate` | scan(615) |

### core/runtime/（门表运行时）

| 文件 | 行 | 职责 | 关键符号 | 调用方 |
|---|---|---|---|---|
| `expr.py` | 184 | 受控表达式 AST 白名单解释器（三禁+正偏移静态拒） | `ExprError`、`parse/_check_tree/_checked_tree`、`static_asof_check(expr,offset_funcs)`、`evaluate(expr,env,funcs)`、`_ev/_cmp` | evaluate(36)、`_referenced_funcs` |
| `functions.py` | 602 | Ctx（as-of 安全数据入口，24 个内核函数）+ 函数注册 | `Ctx(bars,i,lu_idx,params,board_type,code,stock_info,ext,latest,series,mkt_gain,market,resume,i_age)`：chg/vol_ratio/open/high/low/close/volume/is_limit_up/count_limit_up/lu_close/pullback_days/monotonic_down/ma/rsi/macd_dif/dea/hist/kdj_k/d/j/boll_mid/upper/lower/atr；`AsOfViolation`、`shared_ind_cache`、`OFFSET_FUNCS/D0_DEPS/REGISTERED_FUNCS/REGISTERED_OFFSET/REGISTERED_D0`、`register_function/register_strategy_funcs/declared_d0_for_strategy/ensure_gate_init/build_funcs/_bind` | evaluate（GateEvaluator）、gate_stdlib、strategies/*（register_strategy_funcs） |
| `evaluate.py` | 432 | 门表加载/求值 + 单日判定分派（回测编排已退役） | `Gate(id,name,role,needs_d0,enabled,expr)`、`StrategySpec(key,meta,entry,exit,params,signal,gates,market_spec,market_key,func_names)`、`load_strategy(key)`、`_static_asof/_referenced_funcs/build_signal/evaluate_gates`、`GateEvaluator.evaluate_prefilter/evaluate_decision/evaluate_all(_vector/_first_false)`、`scan_day(spec,bars,code,target,...)->list[Signal]`、`_gate_error` | scan(_rows_by_scan_day)、strategies/*、backtest._run_meta、tools/*、tests/* |
| `flows.py` | 115 | 单日判定注册表（scan_day 用；回测 runner 表已退役） | `UnknownFlow`、`ensure_flows()`、`_SCAN_ONE`、`register_scan_one(enum,day_flow,fn,replace)`、`get_scan_one(enum,flow)` | evaluate.scan_day(414)；break/g56/dragon_callback 注册 |
| `gate_stdlib.py` | 212 | 跨策略共享门函数（DSL 词汇） | `obv_rising/no_lu_last/macd_hist_lt/boll_bw_ok/ret_n/d1_vol_ratio/is_approx_limit_up/board_height/ma_bull/is_bse/pk` + `register_function` 挂载 | build_funcs 兜底解析 |
| `resume.py` | 356 | 指标 codec（snapshot/resume/compute 三件套，序列口径） | `ema_fwd/ema_naive`（委托 indicators）、`macd_snapshot/macd_resume/macd_compute`、`atr_snapshot/atr_resume/atr_compute/_tr_series`、`boll_snapshot/boll_resume/boll_compute`、`kdj_snapshot/kdj_resume/kdj_compute`、`closes_of/hlc_of` | functions.Ctx._macd/_kdj/_boll/_atr；increm 内核（snapshot 半边） |

### core/present/（展示内核三件套，754 行受 test_kernel_size ≤800 门禁）

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `contract.py` | 138 | 纯类型 + StrategyProtocol（core 不反向依赖 strategies） | `Stage(key,label,realtime,visible)`、`Progress(stage,date,payload,next_realtime,source)`、`DayInput(code,bar,ctx)`、`InsufficientHistory`、`StrategyProtocol`: `init_state(code,bars)/step(state,bar)/probe(state)->[(date,close)]/evaluate(state,inp,prev)->[Progress]/init_shared/shared_snapshot/begin_day/realtime_shortlist` |
| `runner.py` | 592 | 生命周期：1 日延伸 fold、StateStore 落盘、四条件重建 | `evaluate_day`（ready 恒 stateless 补齐）、`_fold_one`（evaluate→step 唯一推进序）、`fold_range(strategy,code,bars,i0,i1,stages,ctx_provider,stateful)`、`DailyRunner.advance/advance_all/run_day/run_day_all/_fold_step/_rebuild_reason/probe_anchors/_ensure_shared`、`Record(date,state,current,events,strategy_hash)`、`StateStore/`_Backend`(单文件/缓存/flush/batching)`、`strategy_source_hash`、`RebuildNeedsHistory/StateStoreFlushError` |
| `realtime.py` | 109 | 实时旁支（state 副本试推，不写回 fold） | `RealtimeBranch.tick(hhmm,codes,snaps,series_by_code,mkt_gain)->[(code,Progress)]`、`_due/_open_ready_anchor/bar_from_snapshot/_in_anchor` |

- **生命周期**：seed=`init_state(code, bars[:j])`（不足抛 InsufficientHistory 后滑）→ 逐日 `evaluate(state, DayInput, prev)` 先于 `step(state, bar)`（state 语义="截至昨日收盘"）→ `Record{state/current/events}` 落 StateStore（`_BACKENDS` 进程内共享缓存、单 key 单文件、tmp+os.replace、atexit 兜底）。
- **fold_range**：seed 一次 + 逐日 `_fold_one`；`stateless`(prev=None，不结算不抑制，生产投影口径) / `stateful`(prev=head，exec/exit 闭合，回测口径) 两模式共用同一折叠序。
- **四条件重建**（`_rebuild_reason`:439-461）：①reordered(日期错位) ②date_gap(切片不在昨根) ③probe_mismatch(除权锚严格比对) ④strategy_changed(策略源 sha256)；同日= `noop` 幂等拒绝。重建只重建 state；current/events 跨重建保留（代价：改 payload 结构=改规则，sha 兜不住）。
- **Record**：`{date, state(不透明 JSON), current(Progress|None=生命周期头), events(append-only 事件流), strategy_hash}`；`evaluate_day` 追加 stateless ready 补齐事件（顺序：生命周期事件在前、补齐 ready 在后，链配对依赖此序）。

### core/replay/（唯一"走历史"程序，五件）

| 文件 | 行 | 职责 | 关键符号 |
|---|---|---|---|
| `__init__.py` | 474 | replay() 主流程 + feed + collectors + 链匹配 | `DailyFeed(bars,ctx_provider,lo,hi,mkt_map).run(trace_sink,gate_dbg)`（内部 `fold_range(stages=None,stateful=True)`）、`replay(strategy,code,feed,collectors)->ReplayResult`、`replay_batch`（begin_day 横截面池）、`TradesCollector`（ready→exec→exit 链配对）、`TraceCollector`、`ReplayResult(code,strategy,trades,events,trace,gates)`、`market_gain/load_market_gain`、`REASON_DATA_END="数据结束平仓"` |
| `intraday.py` | 130 | IntradayFeed：日线折叠 + 当日分钟快照注入 | `IntradayFeed(bars,code,window=("09:31","15:00"),lo,hi,preload,mkt_map,mkt_slots)`、`series(date,pc)`、`_ctx_for`（series + mkt_series 逐槽 / mkt_gain 回退）、`make_feed` |
| `trade_map.py` | 115 | 事件载荷 → canonical trade（字段映射，零判定） | `CANONICAL_FIELDS`、`_ALIAS(reason→exit_reason, exit_ret→return_pct)`、`is_self_closed(exec_pl)`、`build_trade(...)` |
| `gate_dbg.py` | 69 | 门诊断收集器（全门布尔向量） | `GateDebugCollector(phase,code,i,lu_idx,vec).__call__`、`merged()->{(code,i):{gate:bool}}`、`wrap_ctx_provider` |
| `mkt_slots.py` | 66 | 市场门逐槽 as-of 取数 | `load_market_slots(start,end,days,min_n=200)->{date:{HH:MM:值}}`（build_frame 逐日 + pc_map 一次播种逐日滚动） |

**replay() 流程**：feed.run → fold_range 全阶段事件流 → collectors 流式 `feed(idx,ev)` → `finish(feed)` 收尾（未平链→末日收盘平仓）。**事件链匹配**（TradesCollector.feed）：`ready` 开链（持仓中丢新 ready=去重）→ `exec`（`is_self_closed` ⇒ 两段链直接 `_emit` 产 trade；`buyable=False` ⇒ 断链不入场）→ `exit` ⇒ `_emit` 三段链配对。collectors 契约：`feed(idx,ev)/finish(feed)`，`hasattr(c,'trades')` 收结果；Trace/GateDebug 走 `ctx["_trace"]`/`ctx["_gate_dbg"]` 注入而非事件。

### core/data/（数据通道）与 core/features/

| 文件 | 行 | 职责/通道 | 关键点 |
|---|---|---|---|
| `data/hub.py` | 782 | auto/ 唯一市场数据只读出口 | 通道：`daily`(1D qfq, as_of 下传锚)、`daily_live`(历史+当日合成 bar)、`minute_1m`、`minute_live/prep_minutes`(快照差分标准化)、`quote/market_snapshot/day_series`、`stock_info/all_codes`、`lhb`(⚠D-1 纪律由调用方守)、`index_daily`(磁盘缓存 L2 语义)、`index_minute/index_fflow`(days=交易日)、`reconcile_daily_live`；`_query_batch_raw` 批量 ANY；缓存三套登记（window_cache/指数私有/StateStore）；IO 红线登记（StateStore 是唯一写盘例外） |
| `data/kline.py` | 199 | hub 的日线内部实现层 | `fetch_kline_db/fetch_klines_batch`(同口径批量)、`_window_bounds`(窗口=[anchor−days·1.5, anchor+1d]，as_of 锚)、`window_start`、`ASOF_MIN_BARS=30`、`asof_bars(bars,target,min_bars)`(末根==target 唯一口径)、`fetch_stock_info_db`(死，见 B1)、`all_codes` |
| `data/frames.py` | 601 | 历史盘中快照帧重建（1m→逐分钟全市场帧） | `MinuteFrame`(CSR：codes/offsets/counts/o/h/l/c/vcum/hcum/lcum)：`snap/snaps_at/series/mkt_gain(pos,pc_map)/day_extremes/first_open/last_close`；`build_frame(date)`(npz 缓存 v2，2G 淘汰)、`prev_closes`(UNION 跨年表, qfq, pc 缓存)、`load_code_minutes`(单票全区间)、`snap_series`(单票回放, 与 snap 逐字段同形)、`trading_dates/first_1m_date/hhmm_to_pos/MI_HHMM` |
| `data/window_cache.py` | 429 | 全市场日线滑动窗口缓存（今日窗口专用） | `load_windows/peek_windows/_slide/_rebuild`、复权因子指纹 `_factor_fp`(读 adjustment._mem 快路径)、`BASE_DAYS=320/STALE_DAYS=30/MAX_AGE_DAYS=3`、pickle 单文件 + 进程内 `_MEMO` |
| `features/cross_section.py` | 364 | G1 单票特征 + 横截面 regime 池（g56 下沉） | `_g1_arrays(bars,macd_anchor)`(rma/rma_chg/atr/rsi/big20/ma20/rhist_chg/dif0/pctb/dist_ma20/dates)、`_g1_mask(f,board,age)`(G1_WARMUP=68)、`g1_state_init/step/features/window_bars`(增量状态 {date,age,head,closes,window})、`_aggregate/_ensure_pool_daily`、常量 `ATR_Q5={"main":5.07,"gem_star":6.42}/ROLL=20/MIN_HIST=5/G1_WIN_MIN=21/MACD_*` |
| `features/minute_composite.py` | 278 | 近端 1m + 远端 1D 混合序列 | `minute_live_full/hybrid_series/plan_tiers/window_minutes(code,start,end)`、`DEFAULT_MINUTE_DAYS`；调用方 backtest._refine_intraday |
| `features/quality.py` | 136 | 数据质量分级（检测+标注，不阻断） | `detect_gap_anomaly`(阈值=spec.nominal_up_pct+margin)、`grade_series/grade_minute_window`、`LEVEL_ORDER` |

**as-of 机制汇总**：① 数据层兜底 `hub.daily(as_of)`/`kline._window_bounds`（as_of 作窗口锚）+ `asof_bars` 末根复检；② 表达式层 `expr.static_asof_check`（字面量正偏移加载期拒）+ `Ctx._check_k/_bar`（运行期 k>0 拒）；③ 回放层逐槽市场门 `load_market_slots`（消除用收盘门控盘中入场的前视）；④ Ctx 序列指标一律 `bars[:i+1]` 因果切片。

## A2. core/exec、exit_engines、exit_modes、increm、trace、t_legs、market_env、stock_env 现状

- **exec.py**：成交语义唯一实现（`fill_on_gap`/`fill_blocked_by_limit_dn`/`is_one_word_limit_dn`/`fill_intraday`/`replay_sell_intraday`）。`replay_sell_intraday` 是分钟级卖出腿重放（峰值逐槽推进→无日内先视），仅 break 使用。框架不变量 5 条（long_only/T+1 按 `spec.intraday_t0`/跳空按开盘/跌停顺延/涨停阻买）。
- **exit_engines.py**：4 个引擎 + 顺延共用件。登记表（模块头 25-42）：dragon→`run_trail_stop`（分段追踪 trails、peak_exit、stop_at_idx、use_trig_prev=False）；v1(_archive)→`run_trail_stop`（trig_prev 开）；relay3(_archive)→`run_limit_seal`；g56→`run_hold_stop`；break→`defer_force_open`。
- **exit_modes.py**：注册表机制（`register_exit` 有 4 个策略注册）但**分派器 `run_exit` 无任何调用者**（B1-1）；`_bp`（板块感知参数）是被策略复用的活代码。
- **increm.py**：滚动核（活，cross_section 用）+ IncrKernel 协议/五指标核/RingKernel/注册表（仅测试消费，B1-3）。数学全部委托 resume/indicators。
- **trace.py**：TraceSink 活跃（dragon/g56/break/knife/tail 打点、replay 注入、probe/tools 消费）。
- **t_legs.py**：活但仅实盘链（monitor/base.t_leg_intents/doctor）；回测/replay 不产生做T，TradeIntent 无成交侧消费（P9 未落地）。
- **market_env/stock_env**：活（scan 生产链环境门，只作用 market_env="trend" 策略，缺数据 fail-open）；阈值常量烧在 core（B4-1）。

---

# B. 问题审计

## 严重

### B1-1【死代码+契约失效】exit_modes 分派机制整条死链：`exit.mode` YAML 声明不产生任何行为
- `core/exit_modes.py:25`（`EXIT_MODES` 表）、`:28-33`（`register_exit`）、`:36-53`（`run_exit` 分派器）。
- **证据**：全树（app/、tests/、scripts/）grep `run_exit|EXIT_MODES` 在 `core/exit_modes.py` 之外**零命中**；四个注册适配器 `dragon_callback.py:1119 _exit_combo`、`g56.py:1123 _exit_g56_no_trail`、`break.py:1287 _exit_break_combo`、`exit_modes.py:72 _d1_open` 除注册行外**零调用点**（break.py:691-702 仅注释提及）。
- **后果**：① 目标态"yaml 决定怎么判定（entry/exit）"未接线——改 `exit.mode` 不改变任何回测/实时行为，违反验收"改宏零改引擎/所见即所得"；② 5 个函数体（约 120 行）+ 5 处注册是"看起来有实现、实际零作用"的漂移温床（策略作者会以为注册即生效）；③ `run_exit:48-51` 内部还藏着 `from ...strategies import autodiscover` 的 core→strategies 反向 import（与其模块头"core 不反向 import 策略"声明相悖），且服务于一条死路径。
- 真实出场语义走的是另一条路：各策略在自己的 `evaluate`/回测函数里直调 `core.exit_engines`（dragon_callback.py:472、g56.py:193、break.py:963）。

### B1-2【死代码】entry_modes 整模块无调用者；`limit_up_price` 的"审计补"落在死路径上
- `core/entry_modes.py:56 resolve_entry`（含 close/open/intraday 三模式 + gap 过滤 146 行）全树零调用者（grep `resolve_entry|entry_modes` 仅命中注释：exit_modes.py:11、evaluate.py:4、gate_stdlib.py:190）。
- `core/market.py:236-247 limit_up_price`（2026-09-29 审计补的"涨停阻买"修复）唯一调用点是 `entry_modes.py:133-134`——即该修复当前不覆盖任何活路径（break 盘中买腿的 `fill_intraday(side='buy')` 是否传 `up` 需另行核对 strategies/break.py，但 core 侧供给通道已断）。
- `StrategySpec.entry/exit`（evaluate.py:162）加载后全树无消费者（grep 确认）——门表 YAML 的 `entry:`/`exit:` 两段是纯装饰。
- **后果**：目标态"entry.mode/exit.mode 由引擎分派、策略文件不含 python"在 L0 层是空承诺；新策略作者按文档写 YAML 入场/出场配置会被静默忽略。

### B1-3【死代码/半死】increm 的 IncrKernel 协议与 resume 的 snapshot 半边零生产消费
- `core/increm.py:152-356`：`IncrKernel/Macd/Atr/Boll/Kdj/Rsi/RingKernel` + `register/get/keys/kernels` 注册表，生产调用者为零，唯一消费是 `tests/present/test_increm.py`（其文档甚至写"dragon 的 ring+EMA 锚用 RingKernel"，但 grep `RingKernel|increm.get` 在 strategies/ 零命中——dragon 实际用 cross_section.g1_state_*）。
- `core/runtime/resume.py:146/210/269/327` 的 `*_snapshot`（四件）只被 increm 内核调用 ⇒ 同样生产零消费。resume.py:258-259 以"成对保留"为由自我豁免，但"统一增量协议"（改进方案 §2.5「统一第 4 种形态」）实际未接进任何折叠/展示路径——策略增量走的是 `cross_section.g1_state_init/step`（第 4 份私有逐格实现，见 B2-3）。
- **后果**：改进方案 P4「增量基座」名存实亡一半；新读者会以为 step 协议是现行主干。

## 中

### B2-1【冗余】出场语义至少 5 份叙事，顺延尾块 3 份同型拷贝
- 同一"顺延次日开盘"尾块逐字三份：`exit_engines.py:169`（run_hold_stop）、`:351`（run_trail_stop）、`:404`（defer_force_open），均为 `nxt = entry_idx + exit_d + 1`（且伴随后续 `exit_d = nxt - entry_idx + 1` 的坐标换算，见 B3-2 的 off-by-one）。
- 出场判定多路径：`core/exit_engines`（回测）／`exit_modes` 注册表（死）／`strategies/base.py:522-560 exit_decision`（monitor 实时出场，自带止损/追踪/到期公式，base.py:546 注释自认"与 core/exit_engines._min_sell_day 同口径"）／`replay.TradesCollector.finish`（数据结束平仓）／`exec.replay_sell_intraday`（分钟腿）。目标态"出场唯一实现"只在"回测引擎体"一层成立。
- `tests/test_exit_single_source.py` 只锁 `def run_*` 的单份，锁不住 base.exit_decision/finish 这些同语义异名实现。

### B2-2【冗余】指标数学多份：MACD/RSI/MA 各有 2~3 份实现
- `gate_stdlib.py:56-79 macd_hist_lt` **内联** EMA/DIF/DEA 递推（`k_f=2/13...` 手写），是 indicators/resume"唯一实现"之外的第二份 MACD（对比同文件 is_approx_limit_up:143"避免第二份涨跌停实现"的纪律）。
- RSI 三口径三份：`increm.rsi`(Cutler 滑窗, :80)、`increm.RsiKernel`(Wilder, :266)、`functions.Ctx.rsi`(Cutler, :253-278)——模块注释承认"数值不同勿混用"，但门表 `rsi()` 与特征 `rsi6()` 并存仍是"同名不同数"。
- MA：`Ctx.ma`(functions.py:242-251，过滤 `v>0` 后平均) vs `increm.sma`(全量平均)——同名 `ma` 语义不同。
- as-of 切片三处：`kline.asof_bars`、`hub.daily` 的 as_of 过滤、backtest/run_all_intraday 的窗口 pos 截取（backtest.py:386-391），语义靠注释对齐。

### B2-3【冗余】增量形态 4 份并存的现状未改变（改进方案 §1-④ 的病灶只收敛了一半）
- ① `resume.py` codec（序列口径）② `cross_section.g1_state_*`（{head,closes} 队列逐格推进，cross_section.py:226-260）③ increm 滚动核/Kernel ④ `window_cache` 滑动。② 本应是 increm.RingKernel+step 的组合，实际手写了自己的弹队首/推锚循环（cross_section.py:246-251），协议与实现各走各路。

### B2-4【层反转残留】core 三处动态反向依赖 strategies
- `exit_modes.py:48-51`（run_exit 内 autodiscover——死路径上的反向 import）、`flows.py:63-77 ensure_flows`、`functions.py:532-541 ensure_gate_init`。后两者有"插件发现"辩护且注释充分，但 exit_modes 一处与自身模块头"core 不再 import 任何 strategies.*"（exit_modes.py:15）直接矛盾。

### B3-1【逻辑缺陷/日内先视】run_trail_stop 用当日 high 抬追踪线，触发判定仍是先视
- `exit_engines.py:263-266`：进循环先 `peak = max(peak, bar.high)`；`:314-323`：`trig_t = peak*(1+trail/100); trig = max(trig_t, trig_s); if low <= trig`。
- `use_trig_prev` 守卫（:328-332）**只修成交价**（open>trig_prev 时 fill=trig），**不修触发判定**：若当日 low 先于 high 出现（日线无法分辨），用当日 high 抬高的线会误触发出场。模块头 :205 声称"追踪线用开盘时已知峰值防日内先视"与实现不符。
- 同一语义在 `exec.replay_sell_intraday`（exec.py:140-160）是正确的（peak 逐槽推进）——两条腿口径不一致，日线腿系统性偏早出场（触发点偏高）。

### B3-2【逻辑缺陷/边界】run_hold_stop 到期顺延 off-by-one（跳过一个交易日）；run_trail_stop 靠"每日写到期不 break"的另一处缺陷恰好对齐
- `exit_engines.py:156-158`：到期日一字跌停 → `pending_dn=True; exit_d = hold_days`；`:169`：尾块 `nxt = entry_idx + exit_d + 1`。d 口径是"d=1=入场当日 ⇒ 索引=entry_idx+d-1"（:59、:176 `exit_d = nxt - entry_idx + 1` 自洽），顺延起点应为 `entry_idx + hold_days`，实算 `+hold_days+1` ⇒ **跳过一个交易日**，且报出的 exit_day 多 1。g56 默认 hold_days=7，仅在"到期日恰一字跌停"时触发，但属真实口径错。
- 对照 `run_trail_stop`：`:343-348` 的"持仓到期"写入**没有 `d == hold_days` 守卫也没有 `break`**（每个 d≥min_d 的未触发日都写一遍、靠最后一日覆盖），使 exit_d 恰好停在 hold_days-1，:351 的同型公式"碰巧"对——两引擎各带一处缺陷互相抵消，任何人修其中一处就会把另一处的 off-by-one 显形。
- `defer_force_open:404` 同型公式，语义取决于调用方传入的 exit_d 坐标（break.py:963 调用点需逐笔核对）。

### B3-3【逻辑缺陷/口径分叉】"数据尽头"两种结局、"exit_day 坐标"三套
- 数据尽头：`exit_engines.py:364-371`（A7b）视野尽头未成交 → **返回 None 丢弃该笔**；`replay/__init__.py:277-309`（TradesCollector.finish）回放末尾未平 → **造一笔末日收盘平仓**（REASON_DATA_END）。同一情形两条主路径给出"丢交易"与"造交易"两种结果，golden 与统计口径不可比。
- exit_day 坐标：`exit_modes._d1_open:72-87` 卖在 `entry_idx+1` 却报 `exit_day=1`；`trade_map.py:102` self-closed 链恒 `exit_day=1`；`exit_engines` 的 exit_day=1 是入场当日。canonical trade 的 exit_day 字段在不同策略间含义不同。
- `run_limit_seal`（exit_engines.py:455-472）：`d1 = bars[entry_idx]`（入场当日）触板判定 + **D1 收盘卖出** = T+0，与 `_min_sell_day`（A 股最早 d=2）矛盾——relay3 已退役但引擎仍被 tests/test_exit_single_source.py 列为受保护对象。

### B3-4【异常吞噬】未来函数与求值异常被降级为"门未过"
- `evaluate.py:54-72 _gate_error`：`AsOfViolation`（真未来函数信号）只 WARNING + 门按未过——不 fail-fast、不计数报警阈值；其余异常首条 WARNING 后转 DEBUG（:68-72），回测里同一脏表达式后续静默。
- `evaluate.py:250/324/350/374`：四处 `except Exception` 同语义吞掉 KeyError/ZeroDivisionError 等 → 静默丢信号（模块自认取舍，但与"静默降级是头号敌人"红线冲突）。
- `flows.py:262-263`：autodiscover 失败仅 log.error 后继续（编排表不完整时 fail-fast 反而误报为 UnknownFlow）。
- `window_cache.py:313`：`fetch_qfq_factors` 异常 → `return 0`（指纹恒 0 ⇒ 除权永不触发重拉，与 :287-293 注释"绝不因拿不到指纹返回恒定值"的承诺直接矛盾——except 分支恰好做了它发誓不做的事）。

### B3-5【浮点/边界】increm.boll_pctb 在 len(c)<n 时崩溃
- `increm.py:98-110`：`np.convolve(c, w, "valid")` 在 m<n 时 numpy 会翻转输入（结果长度 n-m+1），随后 `c[n-1:]`（空）与 `lo`（n-m+1）广播 → `ValueError: operands could not be broadcast`（本机 numpy 实测复现：m=10,n=20 即炸）。无长度守卫（对比同文件 roll_sum:70 有 `if len(x) >= n`）。当前调用方（cross_section 长窗口）不触发，是潜伏边界雷。
- `window_cache.py:265-267`（peek_windows）：`k = int((ds_arr >= lo_o).argmax())`——当整段都 < lo_o 时 argmax()=0 ⇒ 旧窗口整段返回（应为空集）。
- `replay/__init__.py:288-290`（finish）：entry_date 查不到 → `ei=0` 兜底 ⇒ exit_day/peak_return_pct 基准错（从首根算）。
- `expr.py:148-150`：`l / r if r else 0.0`、`l % r if r else 0.0`——表达式除零静默出 0.0，可能让门"碰巧通过/失败"而无痕。

### B3-6【数据保真】frames/snap_series 的"位置≈槽位"时间重标伪影（已文档化但仍是回测口径失真源）
- `frames.py:26-31`（模块头自认"稀疏票的位置≈槽位近似，基线本身即如此"）、`snap_series`（frames.py:566-601）按枚举位置重标 MI_HHMM——缺拍票的快照时间整体前移，knife/tail 依赖 `strptime("%H:%M:%S")` 的判定时刻随之漂移。属已知限度，设计文档重写时应升格为显式口径声明。

## 轻微

### B4-1【设计倒退/市场常量】core 内多处烧进 A 股市场/策略常量（违反 `__init__.py:4-5` 与 market.py 的硬承诺）
- `filters.py:17-23 PREFILTER_PARAMS`（换手 3%、市值 20~500 亿、20 日涨幅 10%、前 20 日涨停 1 次）——纯 A 股业务阈值。
- `market_env.py:25-40 DEFAULT_RULES`（净占比 -5%、净额 -50 亿…）、`stock_env.py:21-27`（-8%、龙虎榜 5 次）。
- `gate_stdlib.py:182-184 is_bse`（`("8","4","92")` 交易所前缀）、`features/cross_section.py:336`（同前缀第二份）、`:342 len(bars)<68`、`:32 ATR_Q5={"main","gem_star"}`、`:341 hub.daily(code, 200,...)` 硬编码窗口。
- `entry_modes.py:31 _BOARD_KEYS=("main","gem_star")`（死模块内）。
- `exit_engines.py:231-232` peak_exit 默认 7.0/30.0、`:307` `close < high*0.98`、`run_limit_seal:458` `limit_price-0.001`——策略阈值/容差内嵌引擎。
- 对照合规样板：`features/quality.py:29-49`（阈值全部走 MarketSpec.nominal_up_pct）说明"零市场常量"可做到，上述处是纪律松弛而非能力缺失。

### B4-2【设计倒退/两套判定路径】生产 writer 双轨 + 门表 scan_day 与 fold evaluate 并存
- `scan.py:500-510`（WRITER 开关："scan_day" 门表 writer 与 "record"/旧 writer 并存，标注"P6 删"）、`:580-581`、`:644-659`、`:683-699`。
- 门表路径 `evaluate.scan_day` → `flows._SCAN_ONE`（break/g56/dragon 注册的单日判定 fn）与折叠路径 `strategy.evaluate`（init_state/step 折叠契约）是**两份判定实现**，靠 `tools/projection_shadow` 对账维持等价——目标态"事件流是唯一事实、判定引擎单源"在过渡期未达成。设计文档应把"对账等价"与"单一实现"明确区分。
- `runner.evaluate_day`（runner.py:332-360）为补 stateless ready 每日**跑两遍 evaluate**（stateful + stateless 各一），是"判定恒 stateless、生命周期恒 stateful"裁定下的双跑补丁——已契约化，但属第二套判定编排（性能与语义都需在文档里显式记账）。

### B4-3【死桩/文档漂移】
- `backtest.py:24-26 is_st_stock` 恒返回 False（名字谎报功能；ST 过滤实际依赖涨停阈值）。
- `kline.py:179-193 fetch_stock_info_db` 与 `hub.stock_info`（hub.py:446-466）逐字重复且零调用（scan.py:176 注释称"已归位"）。
- `runtime/functions.py:431-465 D0_DEPS/REGISTERED_D0/declared_d0_for_strategy`、`evaluate.py:86 Gate.needs_d0` 的 T-1 预计算消费方（ide/present.py 管线）已退役，机器全部无消费；`expr.py:48-51` 仍指引读者去已退役的 `ide/present.reads_decision_bar`。
- `market_env.py`/`stock_env.py` docstring 的"先纸面，由调用方决定是否生效"（stock_env.py:10-11）与 scan.py:597-628 已实际生效的现状不一致。
- `stock_env.py:62`：`f.get("source") or f.get("source", "fund_flow_api")` 同键写两遍（无害但表明此处未复核）。
- `window_cache.py:258-260`：`_DEFAULT_DIR` 用"回退 5 级"拼路径 + 落 `backend_api_python/cache/kline_window`，与 `_paths.CACHE_ROOT`（`data/market_cn_cache`）双锚点并存——违反 `_paths.py:18`"不要退回回退 N 级写法"的自订纪律（frames.py:33-35 同型问题已修过，此处漏网）。
- `t_legs.py:213-221`：长注释记一个已删除的死守卫（保留动机充分，但"注释里的代码史"会让目标态文档读者困惑——目标态要求"无历史、无修改记录"）。

---

## 附：与目标态的差距速览（供重写定位）

| 目标态条款 | 现状 | 主要证据 |
|---|---|---|
| entry/exit 由宏声明、引擎分派 | ✗ 未接线（声明无消费） | B1-1/B1-2 |
| 事件流唯一事实、四视图同源 | △ 回测/实时/展示已收敛 replay+fold；生产 writer 双轨待删 | B4-2 |
| 成交/出场唯一实现 | △ exec 原语唯一；出场判定 5 份叙事 | B2-1 |
| core 零市场常量 | ✗ 8+ 处 A 股常量内嵌 | B4-1 |
| 受控 DSL 禁未来函数 | ✓ 静态+运行期双保险；但违规被降级不 fail-fast | B3-4 |
| 数学只有一份 | △ 涨跌停/成交已收敛；MACD/RSI/MA 仍多份 | B2-2 |
| 增量统一协议 | ✗ 协议无生产消费，g1_state 私有实现并存 | B1-3/B2-3 |
