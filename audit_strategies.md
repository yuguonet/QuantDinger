# 策略层代码审计报告 — `auto/strategies/` 全目录

> 审计对象：`QuantDinger-src/backend_api_python/app/market_cn/auto/strategies/`（base.py / __init__.py / 5 个活跃策略 .py+.yaml / _archive/）
> 审计方式：**只读**。逐文件通读 + 全仓引用追踪（grep 调用点）+ pyflakes 静态扫描 + yaml↔py 参数逐项比对。
> 参照基准：《策略宏系统目标态.md》（宏 = 规则唯一事实源，.py 只留 feat 原语 + 编排注册）、《改进方案_v2.1.md》（P3 策略抽血、§2.2 策略文件契约终态）。
> 日期：2026-10-09。行号以当日文件为准。

---

# A. 事实梳理（供设计文档重写用）

## A0. 目录总览

| 文件 | 行数 | 一句话职责 |
|---|---|---|
| `__init__.py` | 329 | 策略注册表（register/autodiscover）+ 开关/参数/限额配置读取（yaml 宏为单一事实源，config.json 兜底） |
| `base.py` | 610 | 策略插件契约：ScanSpec/Signal/三 Decision/ExitDecision + StrategyBase（折叠契约 + 三决策智能默认 + backtest_stock 薄壳） |
| `break.py` | 1393 | 断板接力：连板→断板期→确认日→D1 开盘买；折叠契约 + 门表 DSL 私有函数 + 出场引擎（3 份实现，见 B1） |
| `break.yaml` | 99 | 断板宏：10 门 + 19 展示字段 + 入场/出场/参数 |
| `dragon_callback.py` | 1213 | 龙回头：涨停日回调反转；折叠契约（RSI6 增量 + MACD 锚 + 冻结 lu 特征）+ 门表适配器 + probe 调试标签段 |
| `dragon_callback.yaml` | 99 | 龙回头宏：11 门 + 10 展示字段 |
| `g56.py` | 1177 | 五重共振：G1 池 + ④金叉 + ⑤板块 regime → D0 开盘买，7d/-8% 无追踪出场；横截面池台账（PoolLedger） |
| `g56.yaml` | 83 | g56 宏：9 门 + 6 展示字段 |
| `knife_catch.py` | 644 | 反向接刀：大盘跌+个股深跌+尾盘卖盘枯竭，14:56 买入 → D1 开盘卖（盘中窗口策略） |
| `knife_catch.yaml` | 81 | knife 宏：10 门 + 10 展示字段 |
| `tail_oversold.py` | 654 | 尾盘超卖超短：五条件超卖反弹，14:50 起滚动买入 → D1 开盘卖（盘中窗口策略） |
| `tail_oversold.yaml` | 61 | tail 宏：4 门 + 9 展示字段 |
| `_archive/` | 1688 | 3 个已退役策略（lead_chase 778 / relay3 414+46yaml / v1 384+66yaml）+ README |

---

## A1. 逐文件明细

### A1.1 `base.py`（610 行）— 策略插件契约

**职责**：定义策略与框架之间的全部契约（调度、输出、三决策、折叠、回测薄壳）。

**关键类型/函数**（签名 → 作用）：

| 行 | 名称 | 作用 |
|---|---|---|
| 29 | `@dataclass ScanSpec` | 调度契约：kind(windows/interval_sec/data/entry_at/after_events/fire_at) |
| 52 | `@dataclass Signal` | 标准化信号：code/time/side/score(0~100)/price/label/extra |
| 63 | `signal_of_ready(code, date, payload) -> Signal` | ready 事件→Signal 的**唯一口径**（scan_days 与投影写入器共用） |
| 78 | `@dataclass EntryDecision(buyable, reason)` | D1 开盘处置判定 |
| 85 | `@dataclass ConfirmDecision(confirmed, reason, d1_chg, d1_vol_r, detail, exit_price)` | 15:00 收盘确认；返回 None=无法判定 |
| 125 | `_has_fold_contract(strategy) -> bool` | 折叠契约实现判定（MRO 比对 init_state/step/evaluate 是否被覆写） |
| 144 | `@dataclass ExitDecision(action, reason, price, fill)` | 出场判定；`fill=""`=收盘价成交，`"open"`=次日开盘强平 |
| 161 | `class StrategyBase` | 插件基类（详见 A2） |

**对外契约**：
- 被谁调用：全部 5 个策略继承；`scan.py`（scan_signals/initial_stop/signal_state）、`monitor.py`（三决策/quality_key/t_leg_intents）、`rebuild.py`/`present_daily.py`/`core/present/runner.py`（init_state/step/evaluate/probe/init_shared/shared_snapshot/begin_day）、`core/replay`（evaluate 折叠）、`core/backtest.py`（backtest_stock/intraday_replay）、`core/present/realtime.py`（realtime_shortlist）、tools/*（backtest_stock/scan_days/scan_signals）。
- 调用谁：`core.present.contract`（DayInput/Progress/Stage/InsufficientHistory）、`core.present.runner.fold_range`、`strategies.params_override`、`core.replay`（backtest_stock 内）、`core.exit_modes._bp`、`core.t_legs`。

### A1.2 `__init__.py`（329 行）— 注册表 + 配置

**职责**：策略注册/发现 + 「改宏即开关」的配置域（yaml 宏 > config.json > 安全默认）。

| 行 | 名称 | 作用 |
|---|---|---|
| 34 | `register(cls)` | 类装饰器：按 cls.key 注册**实例**（`_REGISTRY[key] = cls()`）；空 key / 重复 key 直接抛 ValueError |
| 45/49 | `get_strategy(key)` / `all_strategies()` | 查表 / 全量 dict |
| 53 | `autodiscover()` | `pkgutil.iter_modules` 遍历本目录 *.py（**不递归子目录** ⇒ _archive 不注册）；per-module 容错（import 失败只记 CRITICAL 跳过） |
| 82 | `_yaml_doc(key)` | `<key>.yaml` 解析（mtime 缓存 `_YAML_DOC_CACHE`）；无文件/解析失败 → None |
| 109 | `_yaml_meta_enabled(key)` | 读 `meta.enabled` |
| 118 | `yaml_params(key)` | 读 `params` 段 = **参数单一事实源** |
| 139 | `load_config(refresh=False)` | config.json（mtime 自动重读）；缺失/损坏 → `{"strategies": {}}` |
| 171 | `is_enabled(key)` | yaml `meta.enabled` → config `enabled` → **False**（缺省不进实盘） |
| 188 | `daily_limit(key, default=20)` | 每日入库上限（事实源=config.json；knife/tail 显式 0=不截断） |
| 194 | `params_override(key)` | yaml `params` → config `params`（当前 config params 已清空）→ {} |
| 211/221 | `family_of` / `family_version` | 版本链归一（config 覆盖类属性） |
| 230–314 | `live_probe_enabled` / `present_persist_settings` / `scan_writer` / `monitor_progress_settings` / `ref_tree_settings` / `market_env_of` | 顶层开关域（config.json 顶层键） |

**注意**：`StrategyBase.params()`（base.py:364）合并优先级 = 显式入参 > 实例 default_params（param_scan 网格）> **yaml params**（回退 config）> 类默认。

### A1.3 活跃策略（.py ↔ .yaml 对照）

#### break（断板，1393 行 + 99 行宏）
- **枚举**：`meta.enumeration: day`, `day_flow: break`；`register_scan_one("day","break",_scan_one)`（break.py:1393）；生产单日判定(scan_day) 与折叠 `evaluate` 双入口共用门表。
- **注册的私有门函数**（`register_strategy_funcs`，break.py:1188 与 1236 **两次**，合并语义）：`feat`(=bk_feat)、`turnover_sig`、`bkf`（展示字段原始值）、`entry_gate`/`entry_pctb`/`entry_bd`（通道标签）。
- **yaml 门表**：10 门（`g_candidate` 结构 + `g_streak` + 5a~5g + `g_turnover`，全部 qualify）；`signal.fields` 19 个（bkf×12 + entry_*×3 + turnover×4）—— **extra 的唯一事实源**（break.py 经 `build_signal` 求值，break.py:445/1388）。
- **出场**：yaml `exit.mode: break_combo`（分板块 stop_loss/trailing_stop/hold_days）；实现出场三份：`exit_decision`（break.py:581，收盘口径 + 跌停顺延，**monitor/折叠 replay 消费**）、`_run_backtest_breakbuy`（:795，sweet/盘中/1m 腿）、`_exit_by_decision`→`_exit_break_combo`（:1244/:1287，exit_modes 注册，**当前无消费者**，见 B2）。
- **评分**：`_score_of(confirm_chg)`（:121）= 50 + confirm_chg×3，clip 0~100，**展示分**（注释明示不参与截断，因 daily_limit=5 实测从不触发）。
- **折叠契约**：init_state/step（递推 `_advance`）/probe/evaluate 全实现；evaluate 三段（stateless 只产 ready；prev=ready→exec；prev=exec→exit，经 `exit_decision` 单源）。

#### dragon_callback（龙回头，1213 行 + 99 行宏）
- **枚举**：`meta.enumeration: limit_up`（lu_idx 升序，首个全门通过即出信号，去重 ±4）；`register_scan_one("limit_up","",_scan_one)`（:1213）。
- **注册私有门函数**（:1095-1117，含 `d0` 依赖声明、`offset=set()`）：`gap_days/dragon/streak/lu_gain20(+_val)/depth/yin/ma20_dev(+_val)/rsi6/tech_score/tech_rsi/tech_roc/tech_psy`（14 个）。判定原语（`_lu_streak/_lu_gain20/_dragon_found/_pullback_depth/_yin_ratio/_d0_vs_ma20/_tech_block`）与门表适配器共用同一份实现（:131-296 与 :955-1090）。
- **yaml 门表**：11 门（q1 企稳 qualify + g1~g10 required；g7 拐点三腿 OR、g8~g10 三排除门用哨兵参数关闭）；`signal.fields` 10 个。
- **出场**：yaml `exit.mode: combo` → `run_backtest_dragon_callback`（:457，骨架 `core.exit_engines.run_trail_stop`：分段追踪 trail_lo/hi、止损、峰值逃顶、到期 7 天；T+1 d=1 只记估值）。
- **折叠契约**：init_state（RING=40 滑窗 + RSI6 二元组 + MACD 锚 + 冻结 lu 记录）/step/probe/evaluate 全实现；evaluate 内 `_signal`（:765）**手写 extra 字典**（见 B3 漂移面）。
- **probe 调试段**（:316-455）：`DEBUG_HOLD_DAYS/DEBUG_TRAILS/DEBUG_MAX_HOLD/DEBUG_WAVE_DAYS` + `_fixed_hold_labels/_trail_exit_labels/_wave_labels/_dragon_debug_labels/_dragon_sample_feats` —— 归因标签（rule_stats 期望读取，见 B4-7）。

#### g56（五重共振，1177 行 + 83 行宏）
- **枚举**：`meta.enumeration: day`, `day_flow: g56`, `day_start: 68/day_end: 9/ext: g56`；`register_scan_one("day","g56",_scan_one)`（:1175）。**横截面策略**：`prewarm`（:582 一次建池）+ 覆盖 `scan_days`（:595，O(n) 预计算 + `_iter_gate_days` 门表 O(1) 查表）。
- **注册私有门函数**（:1115）：`feat/finite/warmup/pool_stat/pool_field/board_is_main/board_is_gem` + `nd_score/nd_tag/nd_exp`（**后 3 个未接线**，见 B4-3）。
- **yaml 门表**：9 门（warmup/finite/①trend/②atr/③big20/④r56/⑤regime_main/⑤regime_gem/⑥dist_ma20）；`signal.fields` 6 个（唯一事实源，`_mk_signal` :231 经 build_signal）。
- **出场**：yaml `exit.mode: g56_no_trail`（7d/-8%，D-1 涨停子集 -5% 紧止损 `stop_loss_lu`）；实测生效路径 = `exit_decision`（:673，低触止损 min(open,线)，d≤1 hold）。
- **折叠契约**：init_state/step（`g1_state_*`，core/features/cross_section）/probe/evaluate + `init_shared/shared_snapshot`（PoolLedger 台账）+ `begin_day`（跨票池）全实现。
- **评分**：`_score_of(dist_ma20, dif0)`（:198）= 0.5·归一 + 0.5·归一(−dif0)，**入库截断键 + quality_key 同源**（:659 已修正为 `row["score"]`）。
- **入场过滤**：gap ≥ 涨停幅度不可买，三处实现（`GAP_LIM` :108、`entry_decision` :641 硬编码、yaml `entry.gap_max`）。

#### knife_catch（反向接刀，644 行 + 81 行宏）
- **枚举**：`meta.enumeration: intraday`，windows ["14:30","15:00"]，`rolling_preview=True`；不进 scan_day（evaluate.py:400 显式拒绝），走 `run_all_intraday`（IntradayFeed + core.replay）与生产 run_scan_knife。
- **注册私有门函数**（:641）：`feat`(kc_metric)/`ok`(kc_ok)/`hhmm`/`mkt_gain`。中间量单点 `_kc_cache`（:518）。
- **yaml 门表**：10 门（qualify：kc_window/kc_mkt/kc_feat；required：kc_data/kc_tail_vw/kc_daily/kc_vol/kc_streak/kc_pre5/kc_lu_recent）；`signal.fields` 10 个。门顺序 = probe stage 归属（`_KC_GATE_STAGE` :568）。
- **出场**：yaml `exit.mode: d1_open`（core/exit_modes 通用实现）；实测生效 = `exit_decision`（:456：live=entry_date<today 按 open 卖；day_close=entry_idx+1 open）+ evaluate exec 结算。
- **折叠契约**：init_state/step/probe/evaluate 全实现；`intraday_shortlist`（:151 快照级预筛）/`realtime_shortlist`（:382）/`day_prefilter`（:177，**无调用方**）。

#### tail_oversold（尾盘超卖，654 行 + 61 行宏）
- **枚举**：intraday，windows ["14:50","15:00"]，`entry_at="14:50"`（与 ScanSpec :215 及 yaml 三处一致）。
- **注册私有门函数**（:649）：`feat/hhmm/limit_hit/ok/nf/v2/pred_score/pred_exp/tier`（9 个）。
- **yaml 门表**：4 门（to_window/to_limit/to_data/to_v2——V2 五条件合一条 expr）；`signal.fields` 9 个（v2/pred_score/pred_exp/tier 均为宏字段来源）。
- **出场**：同 knife（d1_open / exit_decision :470）。
- **折叠契约**：init_state/step/probe/evaluate 全实现；预筛 `_shortlist_dg_max`（:119 由 score_min 反推）+ `intraday_shortlist`（:232）/`realtime_shortlist`（:402）。
- **评分两套**：V2 原始分（`_calc_score` :162，入场门 score_min）与预测分 `pred_score`（:74，50=平盘/100=涨停，落库 score）。

### A1.4 对外契约汇总（谁调用谁）

| 契约面 | 消费方（实测调用点） | 提供方 |
|---|---|---|
| `scan_signals` | scan.py（生产）、sampler、tools/why·replay·path_parity | 全部 5 策略 |
| `scan_days` | rebuild/present_daily/tools/projection_shadow·why | base 默认（fold_range 薄封装）；g56 覆盖 |
| `init_state/step/evaluate` | core/present/runner、core/replay | 全部 5 策略（relay3/v1/lead_chase 无 ⇒ 归档） |
| `probe` | core/present/runner:474、tools/present_verify | 全部 5 策略 |
| `init_shared/shared_snapshot` | core/present/runner:469/564 | g56（PoolLedger） |
| `begin_day` | core/replay、tools/golden_check·legacy_trade_diff | g56 |
| `realtime_shortlist` | core/present/realtime:76 | knife/tail |
| `intraday_shortlist` | scan.py:788/878、tools/debug·replay | knife/tail |
| `entry/confirm/exit_decision`、`quality_key`、`initial_stop` | monitor.py、rebuild.py:678、scan.py:911 | 各策略覆盖 |
| `t_leg_intents` | monitor.py:202/234 | base 默认（t_legs 声明） |
| `backtest_stock` | core/backtest.run_all、tools/replay·path_parity·legacy_trade_diff·explain·debug | base 薄壳（→core.replay） |
| `intraday_replay` | core/backtest.py:244（1m 精修腿） | break（唯一实现） |
| `day_prefilter` | **无** | base + knife（死钩子） |
| `intraday_exit` | **无** | base 默认（死钩子） |
| `register_scan_one` | core/runtime/evaluate.scan_day（生产单日判定） | break/g56/dragon |
| `register_exit`（exit_modes） | **无**（run_exit 无调用方，见 B2） | break_combo/combo/g56_no_trail |
| `register_strategy_funcs` | core/runtime/evaluate.GateEvaluator/build_signal | 5 策略 |

---

## A2. `base.py` 完整契约清单（必选 / 可选 / 已退役）

**类属性**：

| 属性 | 行 | 语义 | 状态 |
|---|---|---|---|
| `key/name` | 172-173 | 注册键 / 中文名 | **必选** |
| `prefilter_anchor` | 174 | U1~U4 锚定日（signal/limit_up） | 可选（默认 "signal"） |
| `entry_style/family/family_version` | 175-179 | 展示去重/版本链 | 可选 |
| `scan_spec` | 180 | 调度契约（⚠ 用了 `field()`，见 B4-8） | 可选（默认 daily_close） |
| `default_params` | 181 | 参数默认值（⚠ 同上） | 可选 |
| `stages` | 186 | 展示阶段表（Stage 元组） | 可选（展示层消费） |
| `SEED_BARS` | 187 | 播种根数（=200） | 可选 |
| `use_unified_prefilter` | 188 | 是否走 U1~U4 | 可选（g56/knife/tail=False） |
| `signal_state` | 189 | 落库初始状态（watch_pending/buy_today） | 可选 |
| `entry_at_close` / `exit_exec_same_day` | 190-191 | 尾盘入场 T+1 / 当日平账 | 可选（knife/tail=True） |
| `rolling_preview` | 192 | 盘中滚动预览 | 可选（knife/tail=True） |
| `data_needs` | 193 | 数据需求声明 | 可选（knife/tail 含 snapshot/minute_live） |
| `t_legs` | 207-216 | 做 T 腿声明 | 可选（逃生舱 `t_leg_intents`） |
| `use_tech_score` | dragon 专有（dragon_callback.py:562） | 技术分参与 | 策略自定义类属性（契约未列） |

**方法**：

| 方法 | 行 | 必选/可选 | 状态 |
|---|---|---|---|
| `scan_signals(bars, code, *, as_of, ctx, **params)` | 218 | **必选**（唯一抽象方法） | 现役（生产旧链，P5 后评估退役） |
| `scan_days(bars, code, *, lo_date, hi_date, **params)` | 224 | 可选覆盖 | 现役；默认 = fold_range 薄封装（stateless 折叠）+ `_scan_days_by_signals` 兜底（:277） |
| `_day_span` | 260 | 内部 | 现役 |
| `day_prefilter(frame, pc_map)` | 302 | 可选 | **已死**（无调用方；改进方案列 P3-④ 待删） |
| `t_leg_intents(...)` | 316 | 可选覆盖 | 现役（monitor） |
| `intraday_exit(...)` | 329 | 可选覆盖 | **已死**（无调用方；仅 exit_modes 注释提及） |
| `intraday_replay(...)` | 345 | 可选覆盖 | 现役（core/backtest 1m 精修腿；仅 break 实现） |
| `params(override)` | 364 | 便捷 | 现役（yaml>instance>class 合并） |
| `init_state(code, bars)` | 404 | 折叠契约 **必选** | 现役 |
| `step(state, bar)` | 408 | 折叠契约 **必选** | 现役 |
| `probe(state)` | 412 | 可选（除权锚） | 现役（默认无锚） |
| `evaluate(state, inp, prev)` | 419 | 折叠契约 **必选** | 现役 |
| `init_shared/shared_snapshot` | 427/434 | 可选（策略级持久态） | 现役（g56） |
| `begin_day(date, states, bars)` | 438 | 可选（横截面） | 现役（g56） |
| `realtime_shortlist(codes, snaps, ...)` | 442 | 可选 | 现役（knife/tail） |
| `quality_key(row)` | 453 | 可选（开盘名额排序） | 现役（monitor.py:517/527） |
| `initial_stop(code, entry_price)` | 468 | 可选（默认 params.stop=-8） | 现役（scan.py:911 / rebuild.py:678） |
| `entry_decision/confirm_decision/exit_decision` | 481/507/522 | 可选（智能默认） | 现役；**过渡期保留**（改进方案 §2.2：P5 后评估退役） |
| `backtest_stock(...)` | 571 | 薄壳 | 现役（折叠契约必须实现，否则 NotImplementedError；无兜底引擎） |
| `_backtest_stock_legacy` / `data_end_close` / `DATA_END_REASON` / `CONFIRM_LEVELS` | — | — | **已退役删除**（2026-10-09 终态②；断链收尾单源=core/replay.REASON_DATA_END） |

---

## A3. `__init__.py` 注册机制（摘要）

1. **注册**：策略模块顶部 `@register class Xxx(StrategyBase)` → `_REGISTRY[key] = cls()`（实例单例）。key 缺失/重复 = 编码错误，直接抛。
2. **发现**：`autodiscover()` 遍历本包一层 `*.py`（跳过 base/__init__；**不进 _archive/**），逐模块容错 import；幂等（已注册 key 不重复 import 副作用——实际依赖模块 import 缓存）。
3. **开关**：`is_enabled` = yaml `meta.enabled`（mtime 缓存热生效）> config `enabled` > False。
4. **参数**：`params_override` = yaml `params`（单一事实源）> config `params`（无 yaml 兜底，当前为空）> {}。
5. **限额/元数据**：`daily_limit`（config，knife/tail=0 不截断）、`family/family_version/market_env`（config 覆盖类属性）。
6. **配置容错**：yaml 解析失败 → 回退 config（logger.error）；config 缺失 → 空配置；所有查表接口对未注册 key 返回安全默认。

---

## A4. 五活跃策略 yaml↔py 对照（规则/参数现状）

| 策略 | meta | entry | exit（yaml） | params 键数 | gates | signal.fields | 实际生效出场路径 |
|---|---|---|---|---|---|---|---|
| break | day / day_flow=break / day_start=4 / day_min_n=6 / enabled | mode=open（无 gap 过滤，注释与 entry_decision 一致） | break_combo + **stop_loss/trailing_stop/hold_days 又抄一遍** | 18（含出场 3 键） | 10 | 19 | `exit_decision`（replay/monitor）；break_combo 适配器当前无消费者 |
| dragon | limit_up / version=2 / enabled | next_open（gap 过滤已移除，entry_decision 注释一致） | combo（阈值都在 params） | 24 | 11 | 10 | `exit_decision` → `run_backtest_dragon_callback`（core.exit_engines.run_trail_stop） |
| g56 | day / day_flow=g56 / day_start=68 / day_end=9 / ext=g56 | open + gap_max{9.8/19.8} | g56_no_trail + **hold_days/stop_loss 又抄一遍** | 10 | 9 | 6 | `exit_decision`（`_exit_no_trail` 只在无消费者的 g56_no_trail 适配器里） |
| knife | intraday / windows 14:30-15:00 | intraday / price=last | d1_open | 11 | 10 | 10 | `exit_decision`（D1 open） |
| tail | intraday / windows 14:50-15:00 / entry_at=14:50 | intraday / price=last | d1_open | 8 | 4 | 9 | `exit_decision`（D1 open） |

**参数逐项核对结论（B5 详）**：五策略所有**当前生效**阈值在 yaml 与 .py 兜底常量之间**数值一致**（break 分板块 6 组、dragon 24 键、g56 10 键、knife 11 键、tail 8 键全对上）；风险在**多源结构**而非现值漂移（除 tail 文档串 1 处陈旧，见 B4-10）。

---

## A5. `_archive/` 现状

| 文件 | 行数 | 内容 | 引用情况 |
|---|---|---|---|
| `lead_chase.py` | 778 | 领涨追击 v5（早盘时段量比 + T+1 出场）；LeadChaseStrategy 含 intraday_shortlist/scan_signals/intraday_exit/exit_decision；**无 yaml 宏、无折叠契约** | 仅注释提及（kline.py:26、scan.py:783、scheduler.py）；无 import |
| `relay3.py` + `relay3.yaml` | 414+46 | 3 板接力（入场逻辑缺陷停用）；run_backtest_relay3 / eval_exit_* / register_exit("relay3_s4")；**无折叠契约** | 仅注释（exit_engines.py:419-431、gate_stdlib.py:156-183 等）；core/exit_engines.run_limit_seal 保留其出场语义供未来复用 |
| `v1.py` + `v1.yaml` | 384+66 | V1 追板（D0 四因子）；_run_backtest / register_exit("v1_combo")；**无折叠契约** | 仅注释 |
| `README.md` | 27 | 归档清单 + 恢复步骤（补 yaml/折叠契约/BASELINE/STAGE_RANK） | — |

- **运行时零引用**：`autodiscover` 不递归子目录（__init__.py:56-58）；`registry._STRATEGIES_FALLBACK` 已同步剔除（registry.py:49-53）；`test_strategy_files_are_documents.BASELINE` 已剔除。
- 文件内部仍带 `@register`/`register_exit` 装饰（import 即注册）——当前无人 import，无副作用；恢复时按 README 步骤执行即可。
- 归档理由（README）：lead_chase 无宏 + 执行通道未就绪；relay3 入场缺陷 + 无契约；v1 无契约。出处 `docs/目标态偏离核对_20261009.md` P6-6/P6-7。

---

# B. 问题审计（按 严重 / 中 / 轻微 分级）

## 🔴 严重

### B1. break 出场存在**三份实现、三种 T+1/入场日语义**，其中 `exit_decision` 缺 T+1 守卫
- **证据**：
  - `break.py:626-643` `_decide()`：`held = end_idx - entry_idx + 1`（:635）后**直接** `if r <= stop`（:637）、`rfh<=trail`、`r>10 峰值逃顶`、`held>=hold` 判定——**没有任何 `held>1` 守卫**。对比 `base.py:556-563`（P1-④ 修复）明确加了 `held > 1` 并注释「止损同样受 T+1 约束……等于当日买入当日卖出」。
  - `break.py:654-663`（① 昨日触发+封跌停 → 今日开盘强平）：`y_trig, ... = _decide(today_idx - 1)`，当 bars 从入场日开始时 `today_idx-1 == entry_idx` ⇒ **入场日(D1)的止损触线会产出 D2 开盘出场**。而项目 R2 口径裁定（改进方案 §2.2.1-4）是「D2 起评 = D1 的止损触线**不产生出场**」。
  - `break.py:846-848` `_run_backtest_breakbuy`：`if d > 1` 才判出场（T+1 正确），且入场日从不产生 pending_dn ⇒ 与 `exit_decision` ①分支**结论不同**。
  - `break.py:1244-1270` `_exit_by_decision`：`for d in range(1, hold+1)` 从 **d=1（入场日）** 起调 `exit_decision` ⇒ 入场日收盘触止损/峰值逃顶时返回 `exit_day=1`（**当日买当日卖**，T+1 违规）。
- **影响**：`exit_decision` 是折叠 replay（break `_exit_event`，break.py:480）与 monitor 的**活路径**；`_run_backtest_breakbuy` 走 1m 精修腿；`_exit_by_decision` 是 yaml exit.mode=break_combo 的默认实现。同一持仓三套口径 ⇒ golden/阈值结论的口径不稳。`tmp/verify_exit_equivalence.py`「12 笔 0 不一致」样本量不足以覆盖入场日触线分支。
- **建议**：`_decide` 补 `held>1`（与 base/`_run_backtest_breakbuy` 同口径）；①分支排除 `today_idx-1 == entry_idx`；`_exit_by_decision` 循环从 d=2 起（或依赖 `_decide` 修复后自然正确）。

### B2. yaml `entry:`/`exit:` 块是**惰性配置**：分派器 `run_exit`/`resolve_entry` 全仓无调用方
- **证据**：
  - `core/exit_modes.py:36 def run_exit(...)` 与 `core/entry_modes.py:56 def resolve_entry(...)` —— 全仓 grep `run_exit|resolve_entry|spec.entry|spec.exit` **零调用点**（唯一"使用"是注释：evaluate.py:4、gate_stdlib.py:190）。
  - 因此 break.yaml:24-28 `exit: mode: break_combo`、dragon yaml `exit: mode: combo`、g56 yaml `exit: mode: g56_no_trail`、knife/tail `exit: mode: d1_open` 以及注册的适配器（break.py:1325、dragon_callback.py:1130、g56.py:1134）在当前运行时**不可达**（应为已退役链 A `run_backtest` 的消费面，P6 删除后残留）。
  - 连带：**break.yaml `exit.stop_loss/trailing_stop/hold_days`（break.yaml:26-28）与 g56.yaml `exit.hold_days/stop_loss`（g56.yaml:36-38）纯装饰**——生效值实际读 `params:` 段（break.py:602-616 `_sp = _break_spec().params`、g56.py:679-683）。yaml 自己的注释「出场阈值 (由 exit.mode=break_combo 的分板块解析读取)」（break.yaml:47）**与事实不符**。
- **影响**：违反目标态 §2/§6「单一事实源 / 改宏零改引擎 / 所见即所得」——改 `exit:` 块一行**什么都不发生**；同一文件内出场阈值两处声明（exit/params）可静默漂移（当前值一致，纯运气+注释约定）。
- **建议**：二选一：① 让 replay/evaluate 的出场真正经 `run_exit(spec.exit.mode)` 派发（回到目标态）；② 明确宣布 exit/entry 块为纯文档并删除重复阈值，消除双写。

## 🟠 中

### B3. dragon 的 Signal.extra 是**手写字典**，与宏 `signal.fields` 双源
- **证据**：`dragon_callback.py:853-870`（折叠 `_signal`）逐键手写 extra（含 round 位数）；`dragon_callback.py:1196-1211`（`_scan_one`）另用 `build_signal(ctx, spec)` 产出宏字段后 `extra.update(feats)`。对比 break（break.py:445/1388）、g56（`_mk_signal` :231）、knife/tail（`_gates` 内 build_signal）均已单源到宏。yaml 注释自称「与 .py 的 Signal.extra 逐字段一致」（dragon_callback.yaml:86）——靠人工同步。
- **影响**：改宏 signal.fields 时折叠路径不跟随（反之亦然），四视图字段漂移；round 位数/None 语义两处维护。
- **建议**：`_signal` 改走 `build_signal`（与 `_scan_one` 同款），结构性键（board/lu_date/turnover_anchor…）另列常量表。

### B4. 死代码 / 死钩子清单（逐条 file:line）
1. **break**：`_signal_to_legacy_dict`（break.py:230-265，无调用方）；`break_entry_gate`（:217-227，其注释指向已退役的 `_backtest_day_flow`）；`_find_limit_ups`（:717-720，纯转发无调用方）；`BOARD_PARAMS.take_profit`（:50/:56，全仓无消费）；**递推台账 `runs/last_lu/open_run/pre_ref/sdate`**（:335、:355-376 维护但从不被读——evaluate 只用 win/abs_i/board，候选改由 `bk_struct` 门表反推）。
2. **dragon**：`_signal_to_legacy_dict` + `_LEGACY_FIELDS`（:492-510）；`_find_bar_idx`（:952-955）；probe 调试段整体（:316-455：`DEBUG_*` 4 常量 + `_fixed_hold_labels/_trail_exit_labels/_wave_labels/_dragon_debug_labels/_dragon_sample_feats`）**判定路径不读、当前无调用方**（唯一间接消费 tools/rule_stats.py:338-340 还取不到，见 7）。
3. **g56**：「明日操作分」ND 段 **约 200 行**（g56.py:812-1113：ND_D/ND_W/ND_PUP_* / `_nd_*` 9 个函数 / `g56_nd_score/tag/exp`）——文件自述「⚠⚠ 当前**未接线**（2026-10-09 D6）……无任何消费方……二选一待定：(a) 上线 (b) 删除」，且注册的 `nd_score/nd_tag/nd_exp` 无任何 yaml expr 引用（`build_funcs` 不绑定）。
4. **base**：`day_prefilter`（base.py:302）+ knife 覆写（knife_catch.py:177）——**全仓无调用方**（回测提速预筛已由别的路径取代）；`intraday_exit`（base.py:329）——无调用方（仅 exit_modes._d1_open 注释提及）。
5. **knife/tail**：`PARAMS["daily_limit"]`（knife_catch.py:42、tail_oversold.py:107）——`params()` 合并后无任何读取点；限额实际走 config.json（scan.py:434/917），两处数值约定（0=不截断）不同载体。
6. **未用 import（pyflakes 实证）**：break.py:25 `build_day_sample`；dragon_callback.py:19 `_STAGE_RANK`、:25 `find_limit_ups`、:293 `fill_blocked_by_limit_dn/fill_on_gap/is_one_word_limit_dn`、:298 `_limit_dn_price`、:299 `_DayTrace`（`_probe_sample_feats` 仅被死函数用）；g56.py:23 `threading`、:27 `calc_macd`、:66 `ATR_Q5/ROLL/MIN_HIST`；tail_oversold.py:21 `is_limit_up/default_market`。
7. **tools/rule_stats.py:338-340 跨模块契约断裂**：`getattr(strat, "DEBUG_TRAILS", ())` 从**策略实例**取，但 `DEBUG_*` 是 dragon_callback.py:316-319 的**模块级常量**（不在类上）⇒ 恒取空/0，调试标签口径静默退化（与 probe 段死代码互为因果）。
8. **break.py:1188 与 :1236 两次 `register_strategy_funcs`**：前者 {feat, turnover_sig} 是后者的子集（update 合并，无害但冗余）。

### B5. 规则多源 / 漂移面（yaml 与 .py 双写；**现值全部一致**，结构上可漂移）
| 策略 | 多源点 | 证据 | 现值 |
|---|---|---|---|
| break | 出场阈值 **4 处**：yaml exit 段 / yaml params 段 / `BOARD_PARAMS` / `initial_stop` 硬编码 | break.yaml:26-28 vs :47-50 vs break.py:44-62 vs break.py:570-578（`-10.0 if gem else -8.0` 不读 yaml） | 一致（-8/-10、-6/-8、7/7） |
| break | `day_start: 4`（break.yaml:14）无消费者（引擎只读 `day_min_n`，break.py:1352） | grep 全仓 day_start 仅 yaml+注释 | — |
| dragon | `gap_min/gap_max` 与 `min/max_pullback_days` **双键须人工同步**（`_scan_one` 用 pd±1，`_signal` 用 gap_*） | dragon_callback.yaml:29-33 自述「必须同步否则预筛与门表不一致」；dragon_callback.py:782 vs :1172-1173 | 一致（4+1=5、6+1=7） |
| dragon | `DRAGON_CB_PARAMS`（:42-128）与 yaml params 双份（前者为兜底） | `_freeze_lu`/`run_backtest_*`/`exit_decision` 三处合并逻辑 | 一致 |
| g56 | 入场 gap **3 处**：`GAP_LIM`（:108）/ `entry_decision` 硬编码（:641 `0.198/0.098`——连 GAP_LIM 都没引用）/ yaml `entry.gap_max`（9.8/19.8） | 三处 | 一致（同值不同单位） |
| g56 | `_g1_mask` 预筛阈值（cross_section.py:32 `ATR_Q5` 等）vs yaml `atr_q5` 等门表阈值 | g56.py:128-133 自述「阈值同步是遗留项」；预筛若严于门表会**漏信号**（mask 先于 evaluate_all） | 一致（5.07/6.42） |
| g56 | 模块常量 `R56/MAIN_RMED_MIN/MAIN_PCTB_MAX/GEM_SCORE_MIN/DIST_MA20_MIN/HOLD_DAYS/STOP_LOSS(_LU)` 与 yaml params 各一份（常量=兜底） | g56.py:50-104 | 一致 |
| knife | 触发时点 **14:56** 硬编码（evaluate 循环 knife_catch.py:277）vs yaml 门 `kc_window` expr（knife_catch.yaml:47） | 两处；tail 同类问题已参数化（`min_hhmm`）⇒ knife 不一致 | 一致 |
| tail | 文档串陈旧：文件头「④amplitude\*nf>=10」（tail_oversold.py:7）vs 参数/yaml `amp_min: 8.0`（:101、tail_oversold.yaml:36） | — | 现值 8.0（2026-09-26 标定） |

### B6. dragon `Signal.score` 恒 0 ⇒ 入库截断键与 quality_key 双口径
- **证据**：`dragon_callback.py:872`（ready payload `"score": 0`）与 `:1208`（`_scan_one` score=0）；而 `quality_key`（:912-915）返回 `(tech_score, turnover_anchor)`。base.py:453-466 的 quality_key 文档恰警告此形态（「须与 scan.py:254 的入库截断键同源于 Signal.score，否则两套口径并存」）。scan.py:434/917 截断按 `r["score"]` 降序 ⇒ dragon daily_limit=5 时全 0 分、稳定排序 = **按入库顺序任意截断**，与 monitor 开盘名额（tech_score/换手）不同口径。
- **建议**：dragon score 落到 tech_score/turnover 组合（或 quality_key 同源回 score）。

### B7. g56 暖机/池缺失等 fail 语义靠 `except Exception: pass` 兜底
- **证据**：g56.py:186-192（`_exit_no_trail` 的涨停子集判定 try/except pass）、:684-692（`exit_decision` 紧止损判定 try/except pass）、base.py:386-388（`params()` 里 `except Exception: pass` 静默吞掉 params_override 故障）。异常被吞后回退默认值，无日志。
- **建议**：至少 logger.debug 一次（首见告警），避免「参数合并失败静默走类默认」。

## 🟡 轻微

### B8. 注释/文档漂移（陈旧描述）
- break.py:13「到期 main20/gem15天」——实际 hold_days=7（:50/:56 注释 20→7 已改，头部漏改）。
- break.py:484/499「ring 窗口，60 根」——`BREAK_WIN=100`（:114）。
- break.py:276「细门在 _break_signal_at 内不单列」——`_break_signal_at` 已删（:153 有退役说明，此处漏改）。
- dragon_callback.py:951「---- 回测钩子 (…backtest_dragon_stock 逐字搬入…) ----」——悬空注释（钩子已删）。
- base.py:457-460 quality_key 注释仍引用已退役的 relay3/dragon_v2/v1。
- `load_config` docstring「缺失/损坏返回空配置 (全开+默认限额)」（__init__.py:137）——现语义是「开关缺省 False」，"全开"是旧口径。
- knife_catch.py:420-423 `_tr` 变量重定义（pyflakes；`_sink` 分支覆盖 `_tr` 初始化，逻辑能跑但读起来像 bug）。

### B9. 死契约字段 / 其它
- `ConfirmDecision.d1_vol_r`（base.py:103）全链无策略填写（monitor.py:258/751、store.py:113 落库恒 NULL）。
- `Signal.side`（base.py:55）恒 "signal"，无第二用途。
- `StrategyBase.scan_spec/default_params` 用 `dataclass.field()` 于非 dataclass 上的隐患——base.py:207-216 **已自述**（t_legs 踩雷实录），scan_spec/default_params 仍带 `field()`，新增策略若不覆盖类属性会复现 `'Field' object has no attribute 'get'` 类故障。
- `backtest_stock` 的 `probe/use_prefilter/stock_info` 形参仅为签名兼容（base.py:571-588 自述），调用方仍在传（core/backtest.py:138-141）——保留无害但属死形参。

### B10. 边界条件核查结论（未发现新缺陷，列出确认过的面）
- **as-of**：break `_bk_raw/_bk_compute` 显式 asof 截断（:1007、:1073）；dragon `_freeze_lu` 只读 ≤k（:648）+ 递推 ring 注入冻结值；g56 特征因果性有 29360 点逐位实证（:131-135 注释）+ 池锚 hi_date；knife/tail 快照序列 `snap_rows[:i+1]` 截断（knife:277、tail:308）+ 市场门逐槽 as-of（knife `_gates` mkt_series）。**未发现读到决策日之后的路径**。
- **T+1**：knife/tail（D1 开盘卖）、g56（exit_decision `d<=1 hold` :710）、dragon（run_trail_stop d=1 只记估值）均合规；**唯一问题是 break（B1）**。
- **空 bars/停牌**：`if not bars: return []` 类守卫齐全；knife/tail exec 结算「开盘价缺失(停牌)保持 prev 不推进」（knife:254-259）；`_d1_open` 无次根返回 None。**末日未平**：单源 `core/replay.REASON_DATA_END`（test_known_divergence.py 钉住），策略侧无自造文案 ✓。
- **异常吞噬**：见 B7。

### B11. 与目标态的差距总评（设计倒退面，均被 `test_strategy_files_are_documents` 以「基线封顶」方式登记，未清零）
- 策略文件仍含程序性内容（P3-④ 未完成）：break 9 处 / dragon 14 处 / g56 5 处 / knife 1 处 / base 5 处（tests/present/test_strategy_files_are_documents.py:52-59 BASELINE；目标态 §3.6 要求 .py 只留 feat 原语 + 编排注册）。具体构成：回测出场引擎（`_run_backtest_breakbuy`、`run_backtest_dragon_callback`、`_exit_no_trail`）、1m 重放（`intraday_replay`）、probe 调试标签段、`_scan_one` 枚举残段、死钩子（B4）。
- 「文档非程序」验收（目标态 §6-4「无历史、无兼容、无修改记录」）明显未达：各策略文件 30%+ 是历史口径注释/修改记录（如 break.py:73-113 评分归一化考据、dragon_callback.py:42-128 参数史、g56.py:812-910 ND 标定史）。
- 双源/惰性配置（B2/B3/B5）与目标态「规则只有一份」直接相悖，是当前最大的结构性债务。

---

## 附：审计方法与覆盖说明
- 通读：base.py(610)、__init__.py(329)、break.py(1393)、dragon_callback.py(1213)、g56.py(1177)、knife_catch.py(644)、tail_oversold.py(654) 全文；5 份 yaml 全文；_archive README + 3 策略头部/注册面。
- 引用追踪：对 30+ 个可疑符号做全仓 grep（含 tests/tools），死代码结论均以「定义存在 + 调用点零」双证据给出。
- 参数核对：5 策略 yaml params/exit/entry 与 .py 常量/兜底逐键比对（B5 表）。
- 静态扫描：pyflakes（未用 import/变量清单逐条列入）。
- 未运行任何代码/测试（只读审计）；golden/等价性结论均引用项目内既有实证注释。
