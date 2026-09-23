# market_cn/auto 系统评审(Bug / 声明漂移 / 优化)

> 来源:任务"market_cn/auto 还有什么可以优化和 bug" | 2026-09-23 | 署名:OpenClaw agent
> 范围:registry / scan / store / monitor / sched / startup(结构)/ strategies/base / g56(全量)/
> core{filters, data/hub, market 等} / config.json 交叉验证。rebuild / present.pipeline /
> runtime.evaluate / tools / 其余 8 个策略文件按结构面覆盖未逐行精读(文末列待核对项)。
> 定位以函数名为准。生产代码零改动。

---

## 0. 总评

这套系统的工程质量**明显高于** agent 侧:单一事实源纪律、幂等写库、迁移自愈、
"指纹校准成功才推进"的补偿重建、as-of 全线防未来函数、瞬态冲突重试、探针留痕、
g56 的因果性逐位实证(29360 点 0 不一致)与 5.3x 优化的等价性验证(tmp/_g56_diff.py)。
"声明了就验证、优化了就对账"的闭环是真实在运转的。

本轮共发现 **Bug/决策级疑点 7 项(1 项高优待用户裁定)、声明漂移 4 处、冗余/优化 6 处、
设计风险 4 处**。值得注意:agent 侧的"声明了没接线"模式在这里也有残留
(基准类双定义、死告警承诺、enabled 默认值文档漂移),但密度低得多。

---

## A. Bug / 决策级疑点

### A1(高,待用户裁定). config.json 自相矛盾:dragon_v2 还在跑,"撤回 V2"裁定还写在注释里

- **证据**:config.json `_comment`:"2026-09-13 用户裁定撤回 V2 代实验: dragon_v2/break_v2
  enabled=false 留档…系统回到 V1 策略集";实际 `strategies.dragon_v2.enabled = true`
  (break_v2 确为 false)。dragon_v2 是 `ScanSpec(kind="daily_close")`,**每日盘后照常产信号**。
- **叠加影响**:dragon_v2 未声明 `family`(自成一族)→ 与 dragon_callback **不互相去重**
  (`_dedupe_family` 按 (code, family, style)),同一票可同时出"龙回头"和"龙回头V2"
  两条入组信号;config 里 dragon_v2 无 winrate(前端展示空)。
- **定性**:要么是撤回后忘了关(= 界面在卖用户已撤回的策略信号,用户按组买入是真金白银),
  要么是重新启用了但注释没更新。**这是产品决策级问题,请裁定**:关掉 / 保留并改注释。
- 修复(若裁定保留):补 family 声明决策(是否与 dragon_callback 同族去重)+ 补 winrate。

### A2(中). daily_limit=0 语义双关:scan 侧"不截断" vs monitor 侧"全过期"

- **证据**:scan.py 截断 `if cap and len(grp) > cap` → 0 = 不截断(knife_catch/tail_oversold
  的 config 注释明写"0=不截断,用户裁定: 全拿优于Top3截断");而 monitor.py 开盘名额段
  `for i, (...) in enumerate(lst): if i < limit: 买 else: 过期` → **limit=0 时全部候选
  直接 expired,一股不买**。
- **当前未踩中纯属巧合**:knife/tail 是 `signal_state="buy_today"` 不走 watch_pending
  路径;g56=30、其余=5。但这是语义地雷——**任何新策略配 daily_limit=0(按 scan 语义
  "不限量")+ watch_pending 状态 → 开盘日信号全部作废**,且现象是"信号有、开盘全消失",
  极难归因。
- **修法**:统一语义(0=unlimited)写进 `registry` docstring,monitor 侧
  `quota = limit if limit > 0 else len(lst)`;或在 `daily_limit()` 入口把 0 归一成
  sys.maxsize。改一处即可,但**语义必须单点定义**。

### A3(中). buy_today 隔日未确认 → 永久僵尸行

- **证据**:monitor.py 步骤 5(正式确认)要求 `entry_date == today`;步骤 1 只兜底
  stale `watch_pending`;步骤 4 只处理 `holding`;步骤 6 只处理 `exit_today`。
  若 15:01 确认窗口因调度停机/异常错过(snapshot_day_done 或 DB 故障),次日该行
  **不再进入任何转移分支**——永久停在 buy_today。
- **后果**:界面永久"买入"态(用户以为还挂着单);止损守卫虽仍覆盖(buy_rows 进 guard),
  但 confirm→holding/exit 的确认流断裂;09-15 修复过同族问题(禁用策略存量 pending 过期),
  buy_today 没有对应兜底。
- **修法**:步骤 1 同款隔日兜底——`entry_date < today 且 state=buy_today` 的行补跑
  confirm(`snapshot_day_done()` 现成判据)或转 expired + warning,二选一但必须有转移。

### A4(中). 收盘出场判定是"单日增量",错过一天窗口 = 漏检那一天的止损/到期

- **证据**:步骤 4 只在 14:58~15:06 跑,`exit_decision(day_close)` 只判**当日一根 bar**
  (g56/base 均如此:`b = bars[today_idx]`,只查 `low<=止损线` / `d>=HOLD_DAYS`);
  而回测 `_exit_no_trail` 是**逐日扫** d=2..7。bars 全序列都传进来了,但函数只消费末根。
- **后果**:某日错过窗口 → 该日的止损触发/到期收盘漏判;次日只看次日 bar。
  跳空收回场景尤其偏离回测(回测会在触发日按 min(open, 止损线)成交,实盘漏一天后
  可能从未触发);到期日同理会漂到 d=8 收盘。**与"回测=实盘同路径"的既定承诺冲突**。
- **修法**:day_close 重放改"从 entry_idx 到今日**逐日**扫"(与 `_exit_no_trail` 同式,
  幂等天然满足——重复跑同一天结果不变),而不是只判当日。g56/base 两处 exit_decision
  + 框架层最好统一收进一个 `replay_exit_day_close()`。

### A5(低). base.py 两个 `backtest_stock` 定义,前者是死代码 + "引擎会告警"是死承诺

- **证据**:`StrategyBase` 类体先定义"默认 None = 无日线枚举回测"的回测钩子,后又定义
  "通用回测引擎"(2026-09-18 P2)——Python 后者覆盖前者,**前者的 docstring 成了谎言**
  (新策略不覆盖 backtest_stock 实际会跑通用引擎,不是返回 None)。
  且通用引擎注释承诺"若策略覆盖 entry_decision 改了竞价规则…引擎会告警"——
  **该告警代码不存在**。g56 恰好两个方法都覆盖所以无实害;下一个"覆盖 entry_decision
  但用通用回测"的策略会**静默用错 gap 口径**(回测用 params gap 带,实盘用策略自定义
  竞价规则)——回测数字看着正常,实盘口径分叉。
- **修法**:删前一个死定义、把契约并入后者的 docstring;补上承诺的签名比对告警
  (`entry_decision.__func__ is not StrategyBase.entry_decision` 且 `backtest_stock` 未覆盖
  → warning),或至少把注释里的承诺删掉。

### A6(低). store.ensure_tables 的迁移逻辑会误删未来新增的非 strategy 唯一约束

- **证据**:`SELECT conname ... contype='u' AND pg_get_constraintdef NOT ILIKE '%strategy%'`
  → DROP,且**每次 ensure_tables 都执行**。今天只命中旧版 UNIQUE(trade_date, code,
  entry_style),但语义是"删掉所有定义不含 'strategy' 字样的 UNIQUE 约束"——
  将来在 signals 表加任何其它唯一约束(幂等键/外键去重)会被静默删掉。
- **修法**:按列定义精确匹配(`ILIKE 'UNIQUE (trade_date, code, entry_style)%'`)再删,
  迁移成功后把逻辑移出 ensure_tables 常驻路径。

### A7(低). g56 `_ensure_pool_daily` 返回值并发契约未声明

- **证据**:`_POOL_LOCK` 只护构建;成功路径返回 `_POOL` 本引用,`_POOL.update(...)` 会
  **原地替换内层 dict**——另一线程持引用读到一半会看到半截新半截旧。当前 scan/monitor
  调度串行无实害,但接口形状是隐患(将来展示管线夜间批量 + 扫描并行就会踩)。
- **修法**:返回深拷贝/浅拷贝(`{**_POOL, "main": dict(_POOL["main"]), ...}`),
  或 docstring 明写"单线程契约,跨线程须自备快照"。

---

## B. 声明漂移(注释/config 说的 ≠ 代码做的)

| # | 声明 | 实际 | 危害 |
|---|------|------|------|
| B1 | `strategies/__init__` docstring + 易错点:"config 缺失/损坏时全部策略按 **enabled=True** 兜底";config `_comment`:"未写键的策略按 enabled=true…兜底" | `is_enabled` 2026-09-15 事故修复后**默认 False** | **恰好是那次事故的错误认知还留在文档里**;照文档推理的人会得出反向结论,可能把"没写 config"当安全。两处都应改为 enabled=false |
| B2 | config `_comment`:"dragon_v2/break_v2 enabled=false 留档" | dragon_v2 enabled=true(= A1) | 决策级,见 A1 |
| B3 | registry `_STRATEGIES_FALLBACK` 注释"与磁盘插件保持一致" | 少 g56/knife_catch/triple_resonance 等,只是 6 个老 key | 双异常兜底场景下 g56 历史行退出查询范围(影响小但注释失真) |
| B4 | core/filters.py 头注"auto/common";base.py"引擎会告警" | 实住 core/;告警不存在(= A5) | 误导定位/虚假安全感 |

---

## C. 冗余 / 性能优化

| # | 项 | 位置 | 说明 |
|---|----|------|------|
| C1 | common/ 4 个兼容壳仍在被旗舰策略消费 | dragon_callback.py import common.{indicators,market,exec_cn,filters} | shim 全部转发 core/*,dragon_callback 是**唯一**消费者;改直连 core.* 后整个 common/ 可删 |
| C2 | 一个 monitor tick 内 5 处独立全市场快照查询 | 步骤 1/2/3/5/6 各自 latest_snapshot/fetch_day_snapshots | 同 tick 内数据不变,应一次取快照分发各步;60s tick × 5 次全量 SQL 是纯浪费(快照表按年分表,量不小) |
| C3 | `_display_detail` 每行重建 label/winrate 映射 | store.py:`strategy_labels()/strategy_winrate()` 逐行调用 | 每次都过 load_config();sync_watchlist_group 对 N 行循环内重复构建,提到循环外一次即可 |
| C4 | gap 判定三份实现 | base.entry_decision / base.backtest_stock 内联 / monitor 步骤 1(仅展示用) | 且 g56 又覆盖一份(涨停幅上限)。收敛为 `gap_check(code, open_px, prev_close, params)` 单点,展示/决策/回测共用 |
| C5 | monitor 步骤 1 自算 gap + entry_decision 再算 gap | monitor.py 开盘窗口 | 同一算术两份(detail 记录用),C4 落地时顺手消 |
| C6 | 双 `backtest_stock` 死定义 | strategies/base.py | = A5,删前一个 |

---

## D. 设计风险(非 bug,值得留意)

1. **config 热加载 × 零审计**:`load_config` 按 mtime 热重读(改参数免重启,是特性);
   但意味着**改一个数,下一 tick 就按新参数买卖**——没有变更留痕、没有回测前置校验。
   与系统"可追责"总原则不对齐。建议:config 变更 diff 落日志/落表(何时、哪个键、旧→新),
   参数敏感的策略(knife/tail 的 stop_pct/hold_days)改后首次生效时打 warning。
   对照:g56 把阈值冻结为模块常量**刻意**不让 config 覆盖("防误调")——两种哲学并存,
   边界应该写明:哪些参数允许热调、哪些必须走 tmp 研究链路。
2. **单用户假设**:`DRAGON_USER_ID=1` 硬编码、"所有用户可见同一策略组"(文档已声明)。
   多用户化时 store 层的 user_id 语义要整体重审,属已知迁移债,列出以免丢失。
3. **`_data_ready` 用 000001 作参考股**:该股停牌/数据缺失的日子会误判"未就绪",
   轮询 1 小时后**放弃当天全市场扫描**(静默少一天信号)。换"任意 N 只参考股任一就绪"
   或指数 bar 判定更稳;放弃时应升级告警级别(现在 warning)。
4. **长等待占调度线程**:`run_scan(wait_data=True)` 在调用线程 sleep(300) 最长 1 小时;
   若 market_cn/scheduler.py 是单 worker,会阻塞同期其它任务(含 60s monitor tick)。
   scheduler 的线程模型待核对(见待核对项);若是单线程,等待应异步化(到期回调)。

---

## E. 值得点名的优秀实践(供对照,不是客套)

- **g56 的"声明→实证"链**:因果性(29360 点 0 不一致)、优化等价性(tmp/_g56_diff.py
  20440 项逐字段)、锚点等价(池锚一次建 vs 逐日建:阈值翻转 0)——每条性能/语义改动
  都带可复核证据,tmp/ 产物可回放。这正是 agent 侧缺的"校验环自证"。
- **rebuild 的指纹语义**(2026-09-23 修):"校准成功才推进指纹",失败下次启动自动重试;
  判定/展示指纹按语义边界切分(改展示不白跑重建,且有 19:33 实证)。
- **upsert_scan_signals 的瞬态重试**(40P01/40001 退避重试)+ purge_stale_detail 的
  "extra 只加不减"显式治理——两个都是踩过坑后留下的一次性正确修复。

---

## 待核对项(本轮未逐行覆盖)

1. rebuild.py(1037 行)/ core/present/pipeline.py(849)/ core/runtime/evaluate.py(780)
   逐行审计未做——本轮只核了结构面(build_plan/apply_plan 幂等设计、GatePlan 注册等);
2. market_cn/scheduler.py 的线程模型(D4 依赖此项);
3. 其余 8 个策略文件(break/dragon_callback/v1/relay3/knife_catch/tail_oversold/
   dragon_v2/triple_resonance)的规则细节 vs 回测口径——需要逐策略对账的话是独立一批;
4. adapters/markets(多市场扩展面)与 tools/ 研究脚本(new_strategy.py 是 TODO 脚手架,
   无害)。

---

## F. 修复优先级

| 优先 | 项 | 理由 |
|------|----|------|
| **P0** | A1 dragon_v2 裁定 | 界面在卖用户撤回的策略信号(若裁定仍有效),资金面 |
| **P1** | A2 名额语义、A3 僵尸 buy_today、A4 出场漏检 | 都是"错一天就不可归因"的状态机/口径缺陷 |
| **P2** | B1 文档反转(enabled=True 残留)、A5 双定义+死告警、A6 迁移误删面 | 防下一个人踩 |
| **P2** | C1~C5 冗余清理(一批带走) | 纯收益 |
| **P3** | D1 config 热加载审计、D3 参考股、D4 调度模型、A7 并发契约 | 设计加固 |

---

*完。A 组除 A1 需裁定外,其余均为数十行级修复;较大改动按约定先评审后动。*
