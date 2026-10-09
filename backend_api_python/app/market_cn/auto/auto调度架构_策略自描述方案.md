# auto 调度架构改造：策略自描述方案

> 状态：**待实施**（2026-09-29 用户批准「完整目标态，盘后做」）
> 今天盘中不动代码。后端重启由用户手动执行。
> 理由：交易时段不做结构性改动；改完需再重启一次才能生效。

---

## 0. 一句话

**策略声明「我该什么时候跑」，auto 把它翻译成调度计划，scheduler 只负责执行。**
之后新增/修改策略的触发时刻、触发方式、间隔，只碰 `strategies/<key>.py` 和 `config.json`，
**不碰 `auto/` 核心、`auto/sched.py`、`market_cn/scheduler.py`**。

---

## 1. 为什么改：今天早上暴露的耦合

2026-09-29 为了给 `lead_chase` 加一个 09:40 的独立触发点，动了 3 个文件：

| 文件 | 改了什么 | 为什么不得不改 |
|---|---|---|
| `market_cn/scheduler.py` | `min()` → `_intraday_trigger_slots()` 多触发点 | `_knife_trigger_hm()` 把 N 个策略时刻压缩成 1 个 |
| `auto/scan.py` | `run_scan_knife(keys=...)` 分批 | 执行体写死「跑全部 + 等到 14:50」 |
| `auto/sched.py` | 改了又回滚 | 事实源不确定自己该不该过滤 enabled |

**根因**：触发时刻的「决定权」在 scheduler，而「知道该何时跑」的知识在策略。
两边不同源 ⇒ 每次策略时间变化都要跨模块改。

---

## 2. 现状耦合点清单（改造必须全覆盖）

### 2.1 scheduler.py 硬编码的 3 个 auto 任务

```
dragon_scan     17:25  once_per_day  → scan.run_scan()
knife_scan      多slot once_per_slot → scan.run_scan_knife(slot)
dragon_monitor  60s    interval      → monitor.run_monitor_safe()
```

### 2.2 `ScanSpec` 是跨模块公共契约，**不是调度专用**

消费点（改造时一个都不能破）：

| 消费方 | 读了什么 | 用途 |
|---|---|---|
| `core/backtest.py::_exec_trigger_mis` | `entry_at` / `windows` / `interval_sec` | **回测成交时刻**（分钟槽位） |
| `core/backtest.py:81` | `kind` | 选回测路径 |
| `auto/scan.py:392,416,429` | `kind` / `windows[0]` / `windows[1]` | 等待目标、活跃集 |
| `auto/scan.py:230` | `kind == "daily_close"` | 盘后扫描活跃集 |
| `auto/rebuild.py:80,97` | `kind` | 重放/重建分组 |
| `auto/tools/bench_scan.py`、`tools/debug.py` | `kind` | 调试工具分流 |
| `agent/skills/strategy_debug/run.py:66` | `kind` | agent 技能展示 |
| `auto/sched.py:62` | `kind`/`windows`/`interval_sec` | 时刻表 |

> **⚠️ 结论：`ScheduleSpec` 必须新建，与 `ScanSpec` 并列，不得合并。**
> 合并会让回测成交时刻跟着调度改动漂移。

### 2.3 现有 `interval_sec` 是一个被混用的语义（重点）

它**不是**「scheduler 每隔 N 秒触发一次」。真实语义是：
**「批被拉起一次后，函数内部按此粒度自轮询」**（`run_scan_knife` 内部的预览循环）。

同时 `expand_times(windows, interval_sec)` 又被回测拿去生成**成交槽位列表**。
改造时若把它当成「调度触发间隔」重写，会把 `tail_oversold` 的滚动预览和回测口径一起改坏。

> **⚠️ 结论：保留 `ScanSpec.interval_sec` 原语义不动；新增字段另起名字。**

### 2.4 `auto/sched.py::expand_times` 同时服务回测

`core/backtest.py:270` 直接调它。**该函数签名与行为不得变更**。

---

## 3. 目标态职责切分

```
strategies/<key>.py   声明 ScheduleSpec（本策略何时被拉起）
        │
        ▼
auto/dispatch.py      读注册表 → 生成中性计划 [{name, kind, at, keys, ...}]
        │             + run_plan(plan) 统一执行入口（不含任何线程逻辑）
        ▼
market_cn/scheduler.py  TASKS += build_auto_tasks()   ← 唯一一行注册，此后永不再改
        线程 / 防重入 / once_per_day / qd_scheduler_done / 交易日历
```

单向依赖：`scheduler → auto`。**auto 不得 import scheduler**。

### 3.1 为什么不让 auto 自己起线程

`qd_scheduler_done` 是 2026-09-18「并发 DELETE+INSERT 死锁丢信号」事故的产物
（`once_per_day` 内存标记重启即丢 ⇒ 重复补跑）。
若 auto 自建调度循环，必须重造：防重入、跨重启标记、trading_only 判定、worker 线程。
**两套持久化守卫不一致 ⇒ 同一任务可能被两边各跑一次 ⇒ 事故重演。**

保持单一执行引擎，把「决定权」交给 auto，成本最低且安全。

---

## 4. `ScheduleSpec` 声明规范

新增于 `app/market_cn/auto/strategies/base.py`，与 `ScanSpec` 并列。

```python
@dataclass
class ScheduleSpec:
    """策略调度自描述 —— 只回答『本策略何时被拉起』。"""

    kind: str = "derived"        # derived | once_at | interval | after_data
    at: str = ""                 # once_at / after_data 的目标时刻 "17:25"
    every_sec: int = 0           # interval: 调度触发间隔(秒)
    windows: tuple = ()          # interval/after_data 生效时段 (空=沿用 ScanSpec)
    requires: tuple = ()         # 数据依赖: ("1d",) ("lhb",) ("1m",) ("realtime",)
    wait_for: str = ""           # after_data: 等哪个数据就绪
    timeout_min: int = 0         # after_data: 就绪等待上限(分钟)
    on_timeout: str = "skip"     # 超时处置: skip(当日放弃) | run(照样跑)
    entry: str = "auto"          # auto=按 ScanSpec.kind 分派 | "scan"|"knife"|"monitor"
```

### 4.1 `kind` 取值

| kind | 语义 | 由 scheduler 如何执行 |
|---|---|---|
| `derived` | **不声明**，从 `ScanSpec` 推导（现状行为） | daily_close→17:25 once_per_day；intraday→窗口首拍 once_per_slot |
| `once_at` | 每日固定时刻跑一次 | once_per_day + trigger_hour/min |
| `interval` | 交易时段内按间隔反复拉起 | interval=N, trading_only=True |
| `after_data` | 某个数据就绪后跑一次（带超时兜底） | 由定时 tick 轮询 `qd_data_ready` |

> `derived` 是**迁移安全阀**：现有 8 个策略即使不声明，行为与今天逐字一致。

### 4.2 为什么 `after_data` 必须有 `timeout_min`

数据就绪时刻本身是不确定的：

| 数据 | 就绪特征 |
|---|---|
| 龙虎榜 | 17:00–17:30 波动，需重试 |
| 1m 回填 | 15:30 `post_market_batch` 内含 |
| EM 资金流 | 延迟 ~15min，主站封锁会回退再延迟降秒级 |
| realtime 快照 | 60s 周期，盘中持续 |

现状靠「定时 + 重试/等待」对抗。纯事件驱动会把「数据没来」变成「永不触发」，
**比现状更危险**。故 `after_data` 必须声明超时上限与超时处置。

### 4.3 分组规则（`SLOT_GAP_MIN`）

保留了今天实现的 30 分钟合并逻辑，且文档化为不变量：

> **相邻触发时刻 ≤ 30 分钟的一组必须合并成一批执行。**

反例（已实证）：`knife_catch 14:30` 与 `tail_oversold 14:50` 若拆成两个独立 slot，
14:30 那批会 sleep 到 15:00，14:50 那批被 `task.running` 防重入跳过
⇒ **tail_oversold 全天一次都不跑**。

---

## 5. `auto/dispatch.py` 设计

```python
def auto_plans(date=None) -> list[dict]:
    """注册表 → 中性计划列表。键里不含函数对象，避免 auto 依赖 scheduler。
    [{"name": "auto:scan:17:25", "at": "17:25", "keys": [...],
      "once": True, "trading_only": False, "entry": "scan"}, ...]
    """

def run_plan(plan: dict) -> dict:
    """统一执行入口 —— 按 plan["entry"] 分派到既有执行体，签名不变。
      scan    → scan.run_scan()
      knife   → scan.run_scan_knife(keys=plan["keys"])
      monitor → monitor.run_monitor_safe()
    """
```

`scheduler.py` 侧（**此后永不再改**）：

```python
from app.market_cn.auto.dispatch import auto_plans, run_plan

TASKS = [ ...既有 9 个数据任务... ]

# 自动策略组的任务完全由策略自身声明驱动 —— 加/改策略不需要改本文件
for _p in auto_plans():
    TASKS.append(Task(
        name=_p["name"], fn=functools.partial(run_plan, _p),
        interval=_p.get("every_sec") or 86400,
        trading_only=_p.get("trading_only", True),
        once_per_day=bool(_p.get("once")),
        trigger_hour=int(_p["at"][:2]) if _p.get("at") else -1,
        trigger_minute=int(_p["at"][3:]) if _p.get("at") else 0,
    ))
```

> `functools.partial` 而非计划里塞函数：保持 auto 侧零线程知识。

### 5.1 任务命名与持久化键

`qd_scheduler_done.task_name` 是 `VARCHAR(40)`，命名须留余量：

```
auto:scan:17:25          (盘后批, 多策略合并)
auto:knife:09:40         (早盘单点)
auto:knife:14:30         (尾盘批)
auto:monitor             (60s 状态机)
```

长度均 < 20，安全。

---

## 6. 迁移对照表（现有 8 策略）

| 策略 | 现状 | 迁移后 `schedule_spec` |
|---|---|---|
| `break` | daily_close，17:25 随大流 | `derived`（或显式 `once_at at="17:25" requires=("1d",)`） |
| `v1` | 同上 | 同上 |
| `relay3` | 同上（停用） | 同上 |
| `dragon_callback` | 同上 | 同上 |
| **`g56`** | 同上，**依赖龙虎榜** | `once_at at="17:25" requires=("1d","lhb") on_timeout="run"` |
| `knife_catch` | intraday 14:30–15:00 | `once_at at="14:30"`（内部仍按 `interval_sec` 自轮询到 15:00） |
| `tail_oversold` | intraday 14:50–15:00 | 与 knife_catch 合并进 `auto:knife:14:30` 批（30min 规则） |
| `lead_chase` | intraday 09:40 单点（停用） | `once_at at="09:40"` ⇒ 独立批 `auto:knife:09:40` |

> **g56 是唯一有硬数据依赖的策略。** 17:25 这个时刻当初就是为「晚于 LHB 17:00 落库」定的。
> 一旦拆开各自声明，必须靠 `requires` 保序，否则出现 g56 跑在榜未落库时。

---

## 7. 数据就绪信号

**不引入进程内事件总线**（会让 auto 反向依赖 scheduler 内部状态）。
采用与项目风格一致的、可审计的持久化方案：

```sql
CREATE TABLE IF NOT EXISTS qd_data_ready (
    trade_date DATE NOT NULL,
    kind       VARCHAR(16) NOT NULL,   -- 1m | 1d | lhb | realtime
    ready_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (trade_date, kind)
);
```

- scheduler 的数据任务成功完成后 upsert 一行
- `after_data` 任务的 tick 查该表；就绪即触发
- 超过 `timeout_min` 按 `on_timeout` 处置
- 跨重启有效、可用 SQL 审计、无需新增进程内耦合

---

## 8. 实施步骤（分阶段，可回滚）

| 阶段 | 内容 | 是否改变运行时行为 |
|---|---|---|
| S1 | `base.py` 加 `ScheduleSpec` 类（默认值 `derived`） | ❌ 无 |
| S2 | 新建 `auto/dispatch.py`（`auto_plans` / `run_plan`） | ❌ 未接线则无 |
| S3 | `scheduler.py` 替换 3 个 auto 任务为 `for _p in auto_plans()` | ⚠️ 接线点，需逐项验证 |
| S4 | 建 `qd_data_ready` 表 + 数据任务 upsert | ❌ 无（`after_data` 未启用） |
| S5 | 逐步给 8 个策略加显式 `schedule_spec`（g56 优先） | ⚠️ 逐个验证 |

**回滚点**：每阶段独立 commit 前备份；S3 出问题恢复原 TASKS 三行即可。
现有备份：`tmp/_lead_chase_before_20260928.py`、`tmp/_auto_config_before_20260928.json`。

---

## 9. 验证清单

改造后必须全绿（沿用 `tmp/verify_knife_slots.py` 打桩法，零 DB 全市场）：

1. `auto_plans()` 在 lead_chase 启用/停用两种状态下，输出批分别为 3 个 / 2 个
2. 停用态：批 = `[14:30 → knife_catch + tail_oversold]`，**不得**出现 09:40
3. 启用态：批 = `[09:40 → lead_chase]` + `[14:30 → knife_catch + tail_oversold]`
4. `run_plan(plan_0940)` → `run_scan_knife(keys=('lead_chase',))`，等待目标 **09:40**（不是 14:56）
5. `run_plan(plan_1430)` → 等待目标仍 **14:50**，与今天逐字一致
6. `run_plan(scan_plan)` → `run_scan()` 只跑 `daily_close` 活跃集（含 g56）
7. 30 分钟合并规则：制造 14:30/14:50 两策略 ⇒ 必须合并为一批
8. `qd_data_ready` 未写入时，`after_data` 任务到 `timeout_min` 按 `on_timeout` 处置
9. 跨重启：杀进程重开，`once_per_day` 批不被重复补跑
10. 回测回归：`core/backtest.py` 六策略逐笔等价 + `market_spec_check` PASS
    （验证 `ScanSpec` 契约未被污染）

---

## 10. 明确不做的事

- ❌ 不改 `ScanSpec` 任何字段语义（6 个跨模块消费点 + 回测契约）
- ❌ 不改 `auto/sched.py::expand_times`（回测直接调用）
- ❌ 不改 `run_scan` / `run_scan_knife` / `run_monitor_safe` 签名
- ❌ 不在 auto 侧新建线程或第二套持久化守卫
- ❌ 不引入进程内事件总线 / pub-sub 依赖
- ❌ 不动 `qd_scheduler_done` 表结构

---

## 11. 待用户裁定

1. `g56` 的 `on_timeout`：LHB 未落库时是 **run**（照跑，靠策略内 fail-open）还是 **skip**（当日放弃）？
   > 记忆里有裁定「17:25>榜 17:00 ⇒ D-1 榜可用且须 fail-open」，倾向 `run`，待确认。
2. `after_data` 阶段是否现在就落地？建议 S4 先只建表写数据、**先不启用**任何策略用它，
   观察 1~2 周 `qd_data_ready` 的实际就绪时刻分布，再决定是否切换。
3. `dragon_monitor` 60s 状态机：是否也下沉为策略派生？
   > 它是**跨策略生命周期**（entry/confirm/exit），不属于单个策略，建议保留为固定的 `auto:monitor`。
