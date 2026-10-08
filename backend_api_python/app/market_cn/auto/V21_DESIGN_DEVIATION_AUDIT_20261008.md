# 改进方案 v2.1 —— 设计偏离复查 + 旧路径可删清单（**只读审计，不改代码**）

- **日期**：2026-10-08
- **审计对象**：`D:\QuantDinger\改进方案_v2.1.md`（682 行，现行唯一有效方案）vs 当前代码实测
- **附带**：`auto/P5_REALTIME_HOLD_AUDIT_20261008.md` 的问题关闭情况复核
- **方式**：只读（读文件 + 跑 pytest + 统计脚本）。**未修改任何代码文件**，只新建本 md。
- **门禁基线**：`pytest tests -k "kernel_size or fold_mode or exit_single_source or strategy_files or golden or known_divergence"`
  ⇒ **85 passed / 1 skipped**（全绿）。

> 记录：CodeBuddy agent（v2.1 设计偏离复查），2026-10-08。本文件为独立新建，未改动任何既有文件。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 阶段进度 | P0 ✅ / P1 ✅ / P1.5 ❌ / P2 ❌ / P3 部分（1/9 清零）/ P4 ❌（increm 零引用）/ P5 部分（②④已就位，③未切）/ P6 ❌ |
| 最大偏离 | **切口 1、切口 2 两道"隔离切口"都没生效** —— 薄壳被 5 个覆写绕过、`run_all` 根本没转 replay |
| 文档失真 | §4 说「g56/knife/tail golden 未建」⇒ **实测已建 10 份基线**；§1.3 引用数普遍过时 |
| 可删空间 | 真正立即可删的只有**零引用死符号（20 个，需逐个核实）**；主力旧路径（三决策 / scan_signals / run_all / probe）**一个都不能删** |
| hold 问题 | 已用「非设计路径」解决（tick 内补 verdict），P2 关闭；**代价是 hold 不是契约 stage，且未按 §2.2.2 登记为 D2** |

---

## 1. 阶段进度实测 vs 方案

| 阶段 | 方案要求 | 实测 | 判定 |
|---|---|---|---|
| **P0** 门原因通道 | `core/trace.py` + ctx["_trace"] | `core/trace.py` **95 行**（目标 ~80） | ✅ 达成 |
| **P1** replay + golden | `core/replay.py` + trade_map + golden | `core/replay/` = `__init__ 435 + intraday 118 + trade_map 111` = **664 行**（目标 350）；golden 基线 **10 份** | ✅ 达成（体量超） |
| **P1.5** IntradayFeed 接入 | `run_all_intraday` → IntradayFeed | `core/backtest.py:285 run_all_intraday` **仍是独立时间线引擎**；`run_all:58` docstring 仍写「日线枚举快路径（backtest_stock 钩子）」 | ❌ 未接入 |
| **P2** 调试视图 + probe 适配器 | probe.py → 适配器 → 删 | `probe.py` **173 行仍在**；`PROBE_STAGE_RANK` 仍在 3 文件、`_probe_day` 仍在 2 文件 | ❌ 未完成 |
| **P3** 策略抽血 | 删程序钩子 | `CLEANED` **仅 `tail_oversold.py`**（1/9）；BASELINE 合计 **46 个**程序性符号仍在 | ⚠ 部分 |
| **P4** 增量基座 | 逐策略切换 step | `core/increm.py` **356 行**（目标 120，+197%）；**策略层零引用**（方案 §7-3 已记） | ❌ 未开始切换 |
| **P5** 链路切换 | ①~⑤ 子步骤 | ①投影**读侧**已落地（`store.load_projection`，被 rebuild/projection_shadow 调用）；**写侧未切**；②影子对账已建（`projection_shadow --replay`）；③**未切 writer**；④代码就位但**开关缺省关**；⑤未退 | ⚠ 部分 |
| **P6** 清理 | facade/适配器拆除 | `core/backtest.py` 551 行、`probe.py` 173 行均在 | ❌ |

---

## 2. 设计偏离清单（D-1 ~ D-10）

### D-1（高）：切口 1「backtest_stock 薄壳」**形同虚设**

- 方案 §3.7 切口 1：`base.py` 里 `def backtest_stock → replay(...)` 一行转发，消费方零感知。
- 实测 `strategies/base.py:618` 确实是薄壳，但其 **docstring 自己写明**：
  「**现有 8 个策略全部覆写了本方法，薄壳只影响未来新策略**」。
- 实测覆写仍在：

| 文件 | 行数 | 残留程序钩子 |
|---|---|---|
| `base.py` | 727 | backtest_stock / intraday_exit / intraday_replay / day_prefilter / scan_signals / scan_days / entry_decision / confirm_decision / exit_decision |
| `break.py` | 1677 | backtest_stock / intraday_replay / scan_signals / 三决策 |
| `dragon_callback.py` | 1454 | backtest_stock / scan_signals / 三决策 |
| `g56.py` | 1282 | backtest_stock / scan_signals / scan_days / 三决策 |
| `relay3.py` | 559 | backtest_stock / scan_signals / 三决策 |
| `v1.py` | 603 | backtest_stock / scan_signals / 三决策 |
| `knife_catch.py` | 608 | day_prefilter / scan_signals / 三决策 |
| `lead_chase.py` | 778 | intraday_exit / scan_signals / exit_decision |
| `tail_oversold.py` | 599 | scan_signals / 三决策 ← **唯一已清零** |

⇒ **切口 1 对现有 5 个策略零效果**：回测仍走各自覆写的旧引擎，replay 只服务未来新策略。

### D-2（高）：切口 2「run_all facade」**未建**

- 方案：`core/backtest.py` 保留签名、内部转 replay（P6 才拆）。
- 实测：`run_all` docstring 明写「daily_close 类：**日线枚举快路径（backtest_stock 钩子）**」
  ⇒ **仍是旧实现，不是 facade**。引用面 13 文件（含 `probe.py`、`core/entry_modes.py`、`core/exit_modes.py`）。
- 连带：`run_all_intraday` 未接 IntradayFeed（= P1.5 未落地）。

### D-3（中）：§2.8 体量目标全面未达成（实测 vs 目标）

| 模块 | §2.8 目标 | 实测 | 门禁登记 now | 门禁 cap | 差 |
|---|---|---|---|---|---|
| `strategies/base.py` | 380 | **727** | 688 ⚠ | 757 | 登记值**失真 +39**，余量 30 |
| `core/backtest.py` | 0 | 551 | 550 | 605 | 余量 54 |
| `core/replay/` 包 | 350 | 664（435+118+111） | 435/118/106 | 480/130/117 | trade_map 登记 106→实测 111 |
| `core/increm.py` | 120 | 356 | 356 | 392 | +197% |
| `core/trace.py` | 80 | 95 | 95 | 105 | 接近 |
| `core/probe.py` | 0 | 173（在 `auto/probe.py`） | 172 | 189 | 未退 |
| `tools/` | 12 | **19** | — | — | +7 |
| `core/present/` 三层 | 定稿 799 | **839**（138+592+109） | — | 900（2026-10-08 重裁 820→900） | 余量 61 |

⚠ **`realtime.py` 109 行 / cap 110 —— 余量 1 行**，「任何一行小改即 FAIL」的零余量局面重演
（门禁注释自己记过：820 时零余量把 P0 尾巴卡死）。

### D-4（中）：golden 覆盖面 —— **文档 §4 已过时（乐观方向）**

- 方案 §4 写：「当前覆盖 break + dragon_callback；**g56/knife/tail 的 golden 未建**」。
- 实测 `tests/golden/baselines/` = **10 份**：

```
break_real_000032 / dragon_crafted / dragon_d1_stop_hit / dragon_gap_break /
dragon_short_tail / g56_universe_seed1 / g56_universe_seed7 / g56_universe_seed12 /
knife_synth_1456 / tail_synth_1456
```

⇒ 五策略**全部已建基线** ⇒ 按方案 §5-1「P3-④/P6 的删除开关 = 此文件全绿 + 目标策略 golden 已建」，
**删除开关对这 5 个策略已经满足**。文档不更新 ⇒ 会误导执行者以为「还不能删」。

### D-5（中）：`hold` 未按 §2.2.2 登记为展示口径变更

- 方案 §2.2.2 有「展示口径级变更登记（不改回测输出，但改实时/展示行为，**须留痕**）」表，已登记 D1（锚语义变更）。
- 新增 `stage="hold"` 属**同级变更**，但表中**无 D2 条目** ⇒ 登记链缺一环。
- 且 `hold` **不在任何策略的 `stages` 表**（break/dragon/g56 = ready/exec/exit；knife/tail = watch/ready/exec），
  与 §2.2「stage 由策略声明、展示层只按 stage 查表」不符。

### D-6（中）：判定权从策略侧移到内核侧（hold 的实现方式偏离）

- 方案 §2.2 判据：「策略文件里出现的每个函数，必须能回答『这是市场规则/判定吗？』」
  ⇒ 「继续持有」是**判定**，按原则应由 `evaluate` 产出。
- 实际落地：`realtime.py:90-95` 在 **内核 tick 内**补 `Progress(stage="hold")`，
  由 `rec.current.stage == "exec"` 这一**内核侧条件**决定 ⇒ 判定规则进了内核。
- 缓解：`monitor._progress_map` docstring（337-342）已把这个例外**显式写进契约**：
  「★ 唯一的无事件入典形态 = `stage="hold"`，仅当 prev=exec（活仓）且本 tick 评定无出场」。
  ⇒ 有留痕、有边界，属**可接受的偏离**，但应回写进方案 §2.2.2 作为 D2 登记。

### D-7（低）：`_open_ready_anchor` 的「prev 与锚错配」方案未覆盖

- `_due` 的锚取自 `events` 里那条未消费 ready，而 `evaluate` 的 prev 取 `rec.current`
  （`realtime.py:88-89`）⇒ 两者不是同一对象。
- 方案 D1 只登记了「锚语义变更」，**未涉及此错配**。
- 现状以「hold 只在 prev=exec 时产出」绕过 ⇒ 症状消失，**错配本身仍在**（死仓无事件时不产判定，回退旧路径）。

### D-8（低）：§3.6「core/present 只允许整体性小调」已被突破

定稿 799 → 实测 839（+40）。门禁已于 2026-10-08 把 cap 从 820 重裁到 900 并登记理由，程序上合规，
但与「定稿不动」的表述已不一致。

### D-9（低）：§1.3 退役对象引用数过时

| 对象 | §1.3 记录 | 实测（auto 内文件数 / 次数） |
|---|---|---|
| `scan_signals` | 28 | **27 / 134** |
| `backtest_stock` | 14 | 14 / 79 |
| `run_all` | 14 | **13 / 32** |
| `PROBE_STAGE_RANK` | 10 | **3 / 7** |
| probe 相关 | ~20 | `probe.py` 被 9 文件 import；`probe.sample` 出现在 6 文件 |

⇒ 退役确有进展（rank 表 10→3 文件），但文档未同步。

### D-10（低）：P5-① 投影**写侧**未接线，但读侧已落地

- 已接线：`store.load_projection`（585）被 `rebuild.py:965`、`tools/projection_shadow:95` 调用 ✅
- 零引用：`store.project_records`（540）—— `project_rows` 的批量包装，**无任何调用者**
  （`project_rows` 才是实际入口，被 `load_projection` 调用）。
- 写侧：scan 仍直接写库（P5-③ 未切）。

---

## 3. 旧路径删除清单（分级）

> 判据：① 删除开关（golden 全绿 + 目标策略 golden 已建）；② 引用面是否清零；
> ③ 方案 §3.7 的切口拆除时机；④ 是否有字符串注册 / 装饰器 / CLI 反射导致误判。

### A 级 · 零引用死符号（**立即可删，但须先逐个核实**）

`auto/` 内 809 个模块级 def/class 中，**20 个零引用**（已排除 `_` 开头、main/setup）：

| 文件 | 符号:行 | 备注 |
|---|---|---|
| `store.py` | `project_records:540` | `project_rows`+`load_projection` 才是实际入口；删前确认无 CLI 调用 |
| `api.py` | `dragon_today:31` `dragon_markers:48` `dragon_strategies:66` | ⚠ **高危误判**：很可能经蓝图装饰器注册，需先确认路由表 |
| `strategies/relay3.py` | `gap_buyable:131` `entry_stop:136` `Relay3Strategy:253` | 停用策略；类名可能经注册表**字符串**注册 ⇒ 需核实 |
| `strategies/lead_chase.py` | `LeadChaseStrategy:364` | 同上 |
| `strategies/v1.py` | `V1Strategy:78` | 同上 |
| `strategies/g56.py` | `next_day_view:290` | — |
| `core/data/hub.py` | `synth_bar:276` `sector_map:488` | — |
| `tools/gate_try.py` | `required_window:165` `cache_stats:513` | — |
| `startup.py` | `trigger_rescan:566` | — |
| `events.py` | `events_ready:190` | — |
| `monitor.py` | `run_monitor_safe:850` | ⚠ 可能是给 scheduler 用的入口，需确认 |
| `core/features/minute_composite.py` | `split_by_date:120` | — |
| `core/runtime/functions.py` | `declared_d0_dep:480` | — |

⚠ **A 级不等于"直接删"**：`api.py` / 策略类名 / `run_monitor_safe` 三类最可能是
**反射或字符串注册造成的误判**，删前必须逐个确认（项目既有教训：低频 ≠ 可归档，
归档前必须查 import —— 见 `tools/README.md`）。

### B 级 · 删除开关已满足，但**有前置依赖**（不能先删）

| 对象 | 开关状态 | 前置依赖（未满足） |
|---|---|---|
| `break/dragon/g56/relay3/v1` 的 `backtest_stock` 覆写 | golden 已建（break/dragon/g56 ✅；relay3/v1 无基线） | **`run_all` 必须先转 replay facade**（D-2）。否则删了 = 回测直接废 |
| `base._backtest_stock_legacy`（旧通用引擎） | 同上 | 同上 |
| `knife_catch.day_prefilter` | knife golden 已建（`knife_synth_1456`） | 需确认 replay 侧预筛协议已接（§3.1 说"签名不变，调用方归 replay"） |
| `break.intraday_replay` | break golden 已建 | 需 IntradayFeed 接上（P1.5 未落地） |

⇒ **B 级的正确顺序是「先建 facade → 对拍全绿 → 再删钩子」**，反序会把回测打断。

### C 级 · 条件未满足，**当前不能删**

| 对象 | 实测 | 为什么不能删 |
|---|---|---|
| 三决策 `entry/confirm/exit_decision` | 12/12/11 文件 | monitor 三处回退仍在用；开关**缺省关**；hold 只覆盖活仓 ⇒ 死仓/无判定仍走旧路径。rebuild/store 也引用 |
| `scan_signals` | 27 文件 / 134 次 | P5-③ 未切 writer，scan 仍是生产写入口 |
| `core/backtest.py`（551） | 13 文件引用 | P6 facade 观察期（含 `probe.py` 引用 ⇒ 拆之前 probe 要先消化，方案 §6-6+ 明写） |
| `probe.py`(173) + `sampler.py`(264) | 9 文件 import | P2 适配器期；且 §7-2 记载「tmp/probes/ 371 文件≈9GB，**采集在役**，不能静默归零」 |
| `rebuild.py`(1237) 规则对账 | — | P5-⑤ 观察期后才退 |
| `tools/projection_shadow`(565) | — | P5-② 执行体，切 writer 前必须保留 |
| `tools/legacy_trade_diff`(300) | — | §5-1 删除开关执行体；§2.2.3 后接外部参照 |
| `core/increm.py`(356) | 策略层零引用 | ⚠ **不算"可删"**：它是 P4 待切换的目标件（方案 §7-3 已记），删了等于放弃 P4 |

### D 级 · 门禁登记表失真（先修表，再谈删）

| 登记项 | 登记值 | 实测 | 后果 |
|---|---|---|---|
| `RETIRE_MODULES` base.py now | 688 | **727** | +39，cap 757 ⇒ 余量仅 30，且登记表失真 |
| `PROGRAM_MODULES` trade_map now | 106 | **111** | +5 |
| `CLEANED`（已清零文件） | `tail_oversold.py` | 1/9 | 其余 8 文件基线合计 46 未清零 |
| `realtime.py` cap | 110 | 109 | **余量 1 行** |

---

## 4. `P5_REALTIME_HOLD_AUDIT_20261008.md` 关闭情况复核

用户已用**非设计路径**（tick 内补 verdict + 限死 prev=exec）处理，复核如下：

| 编号 | 原问题 | 现状 | 判定 |
|---|---|---|---|
| **P1** | hold 占位违反 `_progress_map` 契约 | 契约**被改写**：`monitor.py:337-342` 明文「★ 唯一的无事件入典形态 = hold，仅当 prev=exec（活仓）」 | ✅ 关闭（以契约修订方式，非回滚） |
| **P2** | `current=exit` + 未消费 ready ⇒ 恒 hold | `realtime.py:92` 加 `rec.current.stage == "exec"` 条件 ⇒ 死仓无事件**不产判定**，回退旧路径 | ✅ 关闭 |
| ~~P3~~ | hold 屏蔽 ② 出场 | 撤回（tail 产出的是 watch；knife 被 `mkt_gain=None` 挡住）。且 exec 锚 15:01 与 ② 窗口（09:35~14:59）**不重叠** | ✅ 关闭 |
| **P4** | hold 不在 stages 表 | **未变**：五策略 `stages` 仍无 hold | ❌ 未关闭（见 D-5） |
| **P5** | realtime.py 110/110 零余量 | 现 **109 / cap 110** ⇒ 余量 1 行 | ⚠ 未实质改善 |
| **P6** | 切片 exec/exit 无 15:01 锚 | 切片已重建（10-08 18:34）：**break exec 已带 `15:01`**（1 票）；dragon/g56 切片内**无 exec 票**可验；exit 仍全 None（设计如此） | ✅ 基本关闭，**dragon/g56 未获实证** |
| **P3'** | ② 拿到非 exit 的 prog（含 watch）即屏蔽旧 `exit_decision` | 未变（`monitor.py:607-624`）；tail 在 14:50-14:59 真实命中 | ⚠ 既有缺陷，未处理 |

**净评价**：P1/P2/P3/P6 已关闭，方式是「把例外写进契约 + 限死触发面」，
而非方案原本设想的「由 evaluate 显式产出 hold」。遗留两个真问题：
① hold 不是契约 stage（D-5）；② ②号路径的 watch 屏蔽出场（P3'）。

---

## 5. 建议的最小动作（**未执行，待裁定**）

1. **回写方案**：把 hold 补进 §2.2.2 展示口径变更表（编号 D2），并注明「判定权暂落内核 tick，
   长期应回到 evaluate」—— 否则后人按 §2.2「判定是规则内容」会判它为 bug。
2. **更新 §4 golden 覆盖面**：五策略基线已建，删除开关已满足，别让文档继续说「未建」。
3. **修门禁登记表**：`base.py` now 688→727、`trade_map` 106→111。
4. **给 `realtime.py` 留余量**：109/110 只剩 1 行，建议随 D2 登记一并重裁（否则下一次小改即 FAIL）。
5. **A 级死符号逐个核实**后批量删（尤其 `store.project_records` 最干净）。
6. **B 级的正确顺序**：先让 `run_all` 转 replay facade（D-2），再对拍，再删 5 个 `backtest_stock` 覆写 ——
   **不要反序**。

---

## 附：复现命令

```powershell
cd d:\QuantDinger\backend_api_python
$env:PYTHONUTF8="1"; $env:PYTHONDONTWRITEBYTECODE="1"

# 1) 门禁（架构 + golden）
& "D:\QuantDinger\.venv\Scripts\python.exe" -m pytest tests -q --no-header -p no:cacheprovider `
  -k "kernel_size or fold_mode or exit_single_source or strategy_files or golden or known_divergence"

# 2) 关键模块行数
& "D:\QuantDinger\.venv\Scripts\python.exe" -c "
import os
A=r'd:\QuantDinger\backend_api_python\app\market_cn\auto'
for f in ['core/present/contract.py','core/present/runner.py','core/present/realtime.py','core/trace.py','core/increm.py','core/backtest.py','strategies/base.py','probe.py','sampler.py','monitor.py','scan.py','rebuild.py','store.py']:
    p=os.path.join(A,f); print('%-32s'%f, sum(1 for _ in open(p,encoding='utf-8',errors='ignore')) if os.path.exists(p) else 'MISSING')
print('tools/*.py =', len([x for x in os.listdir(os.path.join(A,'tools')) if x.endswith('.py')]))
"

# 3) 零引用死符号（A 级清单生成方式）
& "D:\QuantDinger\.venv\Scripts\python.exe" -c "
import os,re,collections
A=r'd:\QuantDinger\backend_api_python\app\market_cn\auto'; T=r'd:\QuantDinger\backend_api_python\tests'
srcs=[os.path.join(d,x) for d,_,fs in os.walk(A) for x in fs if x.endswith('.py')]
tst=[os.path.join(d,x) for d,_,fs in os.walk(T) for x in fs if x.endswith('.py')]
texts={f:open(f,encoding='utf-8',errors='ignore').read() for f in srcs+tst}
defs=[(os.path.relpath(f,A),m.group(1)) for f in srcs for m in re.finditer(r'^(?:def|class)\s+(\w+)',texts[f],re.M)]
cnt=collections.Counter()
for t in texts.values(): cnt.update(re.findall(r'\b\w+\b',t))
print([(f,n) for f,n in defs if cnt[n]<=1 and not n.startswith('_')])
```

---

# 6. 补记（2026-10-08）：一个**方案本身**的问题 —— 不是「没做」，是「做不下去」

> 前文 D-1（切口 1 薄壳形同虚设）、D-2（切口 2 facade 未建）被我归因为「执行未完成」。
> **这个归因不完整。** 复核后判断：这两处之所以推不动，根因是 **§2.2/§3.1 与 §5-1 两条要求在
> 设计上互斥**，执行者无论怎么做都会违反方案的某一条。

## 6.1 结论（一句话）

**旧回测路径含有 replay 结构性不具备的门（U1~U4 预过滤、D1 gap 带），且信号源不同
（`scan_signals` vs `evaluate`）** ⇒ 「回测统一到单一 replay」（§2.2/§3.1）与
「golden 逐笔等价 = 删除开关」（§5-1，零回归）**二者不可兼得**。

## 6.2 实证：两条路径的门集合根本不同

旧通用引擎 `strategies/base.py:649-711`（`_backtest_stock_legacy`）实测路径：

```
for i in range(25, n-1):
    sigs = self.scan_signals(bars[:i+1], ...)        # ← 旧链符号（P5 退役对象）
    if use_prefilter and self.use_unified_prefilter:
        ok, _ = unified_prefilter(bars, i, code, stock_info)   # ← U1~U4
        if not ok: continue
    d1_gap = entry_price / d0.close - 1
    if not (min_gap_main <= d1_gap < max_gap_main): continue   # ← D1 gap 带
    for j in range(entry_idx+1, n):
        d = self.exit_decision(row0, snap={"mode":"day_close", ...})   # ← 三决策之一
```

replay 路径（`base.py:637-647`）：`replay(self, code, DailyFeed(bars), collectors=[coll])`
—— 且 `core/replay/` 全目录**零 per-strategy 分叉**（无 `strategy_key` / `scan_spec.kind` 分支，已实测）。

| 门 / 环节 | 旧引擎 `_backtest_stock_legacy` | `core/replay` |
|---|---|---|
| 信号产生 | `scan_signals` | `evaluate` |
| U1~U4 统一预过滤 | ✅ `unified_prefilter` | ❌ 无 |
| D1 gap 带（`min/max_gap_*`） | ✅ | ❌ 无 |
| 出场 | `exit_decision` 收盘重放 | 事件链（evaluate 产 exit） |

方案自己写了这个差异，并给了出路（`base.py:627-630`）：

> ⚠ 语义差异：旧通用引擎含 **U1~U4 统一预过滤** 与 **D1 gap 带**，**replay 路径不含** ——
> 这些门应由策略 `evaluate` 自持（与 break/dragon 现状一致）。

**但这条路走不通**：把 U1~U4 / gap 带搬进 `evaluate`，
`evaluate` 同时驱动**生产 fold + 实时分支** ⇒ 会改变**生产信号集合** ⇒ 按执行原则 5 = **规则变更**
⇒ 直接违反 §5-1「架构重构零回归」这条删除开关。
而不搬 ⇒ replay 与旧回测不可能全样本等价 ⇒ 删除开关只是「样本内成立」。

## 6.3 代码里已有「记载在案的自认」

`strategies/base.py:691-697`（旧引擎注释原文）：

> 二者长期分叉，是 P3 那次「replay == backtest_stock 逐笔一致」**只在样本内成立**的原因
> （样本内恰好没有入场当日触及止损的票）。

⇒ 这不是我的推断，是代码里已承认的事实：**golden 只证样本内等价**。
而 §5-1 把「golden 逐笔」当作 P6 删旧代码的**唯一开关** ⇒ 删完之后，
样本外的分叉**再也无法被发现**（§2.2.3 引入外部旧版参照正是为此，但只是缓解，不是闭环）。

## 6.4 这解释了 D-1 与 D-2

- **D-1（薄壳被 5 个覆写绕过）**：覆写之所以存在，正因为每个策略的回测口径里都含
  replay 没有的门 / 私有出场逻辑。删覆写 ⇒ 回测输出变 ⇒ 触发 §5-1 失败。
  ⇒ 不是「忘了删」，是「删了就违反删除开关」。
- **D-2（run_all facade 未建）**：facade 一旦真的转发 replay，就把全市场回测从旧引擎
  切到 replay ⇒ 样本外分叉立即变成**生产输出变化** ⇒ 必须先走规则变更流程（用户拍板）。
  ⇒ 不是「没排期」，是「前置裁决缺失，没人敢切」。

## 6.5 同源第二面：三决策退役对「未迁移策略」没有退役路径

方案 §3.5-2 / §2.7 要求「monitor 三决策退役，RealtimeBranch 接管盘中确认」。
但 `confirm_decision` 里藏着两类语义，方案没区分：

| 策略 | `confirm_decision` 语义 | 能否由 RealtimeBranch（`evaluate`）承载 |
|---|---|---|
| break / dragon | 「无确认步骤」+ 参考价兜底链（只是为了算 `d1_chg`） | 弱语义，需 `hold` ⇒ 即本次 tick 补丁 |
| g56 | 恒持有 `ConfirmDecision(True,"g56_hold")` | 弱语义，同上 |
| knife | `hold_to_D1_open` | 弱语义，同上 |
| **relay3** | **D1 收盘：封板守住 → 持有；未封板 → 尾盘卖（S4）**；用 `series` 的 `high ≥ 涨停价` 判定 | ❌ 日内序列判定，且**未迁移折叠契约（无 evaluate）** |
| **v1** | **日内动量确认**：`d1_chg<0` 或 `intraday=d1_chg-entry_gap <3%` → D2 开盘清仓 | ❌ 依赖 `entry_gap` + 日内动量，同上 |

方案 §2.6(c) 对未迁移 3 个策略（lead_chase/relay3/v1）的安排是
「回测走兼容模式，trade 由旧路径产出，**不阻塞主线**」—— **只覆盖了回测侧，漏了实盘盘中确认侧**。
⇒ 现状（hold 补丁）只对「弱确认语义」的 4 个活跃策略成立；
**relay3 / v1 一旦启用，三决策退役后没有承载者**。

## 6.6 方案层面缺什么（**建议，未执行，待裁定**）

1. **§2.2.2 规则变更表补 R3**：登记「U1~U4 / D1 gap 带 / `scan_signals` 信号源」的归属裁决。
   当前表里只有 R1（末日未平）、R2（D2 起评），**这组分叉从未登记**。
2. **给一个明确裁决**（三选一，不能悬空）：
   - (a) 门归 `evaluate` ⇒ 改生产信号集合 ⇒ 规则变更 + 差异报告；
   - (b) 门归 `replay` ⇒ 改回测输出 + replay 膨胀（与 §2.8 的 350 行目标冲突）；
   - (c) 承认双轨长期存在 ⇒ 则 §5-1「golden = 唯一删除开关」必须改写，P6 不能删旧回测。
3. **§5-1 加限定语**：golden 只证明**样本内**等价；删除前须声明「样本外分叉已不可测」，
   并明确 §2.2.3 外部参照是兜底而非等价物（外部 zip 是「展示层正常版」旧树，
   **不等于**当前树内已经过 R1/R2 修改后的旧回测 ⇒ 基线会漂移）。
4. **§3.5-2 补边界**：未迁移折叠契约的策略（relay3/v1/lead_chase）三决策**不退役**，
   或先补折叠契约再谈退役 —— 否则启用即断链。

> 复现要点：`strategies/base.py:649-711`（旧路径四道门）对比 `base.py:637-647`（replay 路径）；
> `core/replay/` 全目录搜 `strategy_key|scan_spec.kind` ⇒ 0 命中。
```
