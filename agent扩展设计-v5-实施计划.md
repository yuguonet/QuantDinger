# v5 实施计划 —— 批次 P0（阻断级）+ P1（接线）

> 依据：`agent扩展设计-v5.md` §14（P0–P3）与 §19（八条校准 + 四项拍板）
> 方法：superpowers —— 先 RED 测试，再最小实现，每任务 2–5 分钟，每任务可验证
> 日期：2026-10-12

---

## 0. 前置约定

### 0.1 为什么不开 git worktree

superpowers 第 2 步要求开隔离工作区。本项目**不适用**：用户红线为
**禁止执行任何 git 指令，提交一律由用户手工完成**。
替代隔离手段（每任务必做）：

1. 改前先 `cp` 一份到 `del/`（项目既有约定：删除/归档文件移至 `del/`）；
2. 每任务跑 `python -m compileall <改动文件>`；
3. 每阶段结束跑 `qd_smoke` 并与基线对比（见任务 A1）。

### 0.2 纪律（违反即返工）

- **禁止硬编码**：所有阈值走 `os.getenv` 或模块常量 + 注释说明取值依据（项目红线）。
- **行号只作线索，函数名才是锚**：行号会因改动漂移，以函数名定位。
- **测试前写的代码一律删除**（TDD 红绿纪律）。
- **fail-open**：异常不得让主流程失败，沿既有 `logger.debug(...fail-open)` 风格。
- **文件头注释**：新增/大改文件须在头部记录功能特点、关键设计点、易误解易错处。

### 0.3 本批次范围（2026-10-13 修订）

**范围 = P0（修闭环）‖ P1（接线）‖ 阶段 D（启动采集）**，后两条与 P0 **并行**。

> **为什么采集必须并行进来**（2026-10-13 实测，改变了原判断）：
> 实测 `qd_agent_resolutions` **只有 16 条**（有效 hit+miss 仅 7 条）、
> `qd_agent_cases` 633 条、`qd_agent_messages` 125 条。
> 而 361 个独立簇需要 150 只/日 × 243 交易日 ≈ **一年**。
> ⇒ 只修闭环不采集，等于把一条修好的管道晾一年。
> ⇒ **C 类情境档案不可事后重建**（v4 §3.3），晚一天开始就永久少一天。

P2（聚合器）、P3（条件校准）仍属后续批次，另行立项。

### 0.4 双窗口记账（2026-10-13 新增，用户拍板）

**T+1 与 T+3 同时记账**：T+1 作**快速检验窗口**，T+3 作**主判据**。

| 窗口 | 独立簇/年 | MDE | 角色 |
|---|---|---|---|
| **T+1** | 1,083 | **4.3pp** | **快速出第一个结论**（比 T+3 快约 3 倍） |
| **T+3** | 361 | 7.4pp | **主判据**（业务语义：中短线持有 3 天） |

- 每个 claim **同时落两个标签**，不是二选一；
- 报数时**主判据用 T+3**，T+1 只用于「有没有信号」的早期判读；
- 口径版本随标签一起记（`label_spec` 的窗口字段），两个窗口不得混算。

---

## 1. 阶段 A：测试基线与 RED（先写测试，不许先写实现）

### 任务 A1：记录测试基线

- 文件路径：`backend_api_python/app/agent/scripts/qd_smoke.py`（只读执行）
- 要做的：跑一次全量 smoke，**把通过数记在本文件末尾「基线」处**
  ```bash
  cd backend_api_python && python -m app.agent.scripts.qd_smoke 2>&1 | tail -20
  ```
- 验证：得到形如 `248/250` 的基线数字并记下。**没有基线就不许开工**。

### 任务 A2：写 P0 的四个 RED 测试（此时必须全红）

- 文件路径：`backend_api_python/tests/test_v5_p0_closure.py`（新建）
- 要做的：新建测试文件，**只写断言，不改任何产品代码**。四个用例：

  1. `test_delisted_verdict_exists` —— 构造一只「连续 N 根无 K 线且重试耗尽」的标的，
     断言 `resolver` 返回 `verdict == "delisted"`（**当前必红**：无此分支）。
  2. `test_delisted_counted_in_weight_feed` —— 断言
     `weight_feed._VERDICT_SCORE` 含 `"delisted"` 且其值为 `0.0`
     （**当前必红**：字典只有 hit/partial/miss）。
  3. `test_historical_return_is_reproducible` —— 对**同一历史日期**（如 30 个交易日前）
     连调两次 `evaluator._get_actual_return(...)`，断言两次返回值**完全相等**
     （**当前可能红**：若走实时源则会漂移；若已绿则 P0-3 无需改，记录结论即可）。
  4. `test_feed_reports_nonzero_samples` —— 调用
     `weight_feed.feed(dry_run=True)`，断言返回的 `stats` 中
     `sample_count > 0` 的 domain 数 **> 0**（**当前必红**：v4 §27.12 实测全 0）。

- 验证：`python -m pytest tests/test_v5_p0_closure.py -q` ⇒ **4 failed**（全红）。
  若为 3 failed / 1 passed，说明该条不是缺陷，在测试里注明并转 GREEN 验收。

---

## 2. 阶段 B：P0 四件（让闭环先说真话）

> 原则：**P0 未完成，不进 P1**。理由见 v5 §14 —— 标签不可靠时建经验表只会得到空表。

### 任务 B1：`delisted` 单列（v4 G18 / v5 P0-1）

- 文件路径：`backend_api_python/app/agent/chain/resolver.py`
- 要做的：在 `_resolve_finance()` 的**视界门②**（`if actual.get("truncated"):` 分支，
  函数内约第 299 行）**之前**插入退市判定。判据（**不许硬编码**，走 env）：

  ```python
  # ── 退市/长期停牌单列（v4 G18 ⇒ v5 P0-1）：防幸存者偏差 ──
  # 判据：① 已重试耗尽（G0 的重试计数，见 migrations/agent_v5_claim_retry.sql）
  #       ② 最后一根 K 线距今天数 > QD_DELISTED_STALE_DAYS（默认 15 自然日）
  # 两条同时成立才判 delisted：只满足①是数据缺口（保持 data_missing 重试），
  # 只满足②可能是数据源延迟。误判代价不对称——把活票判成退市会污染战绩，
  # 所以宁可漏判（留 data_missing 重试），不可错判。
  _STALE_DAYS = int(os.getenv("QD_DELISTED_STALE_DAYS", "15"))
  ```

  判定成立时返回：

  ```python
  return {"actual": {...}, "deviation": {}, "verdict": "delisted",
          "verdict_score": 0.0, "attribution": "delisted",
          "attribution_note": "连续无 K 线且重试耗尽：按最差路径计入（防幸存者偏差）",
          "resolver_kind": "market_data", "sampled": False}
  ```

  ⚠️ `verdict_score` 必须给 **0.0**（不是 `None`）—— `None` 在 `weight_feed` 里不计入，
  等于没修。这是 P0-1 与 P0-2 的**同一条因果链**。
- 验证：`test_delisted_verdict_exists` 转绿；且对一只**正常活跃票**跑，
  不得被误判为 `delisted`（补一条 `test_normal_stock_not_delisted`）。

### 任务 B2：`_VERDICT_SCORE` 补 `delisted` + 聚合白名单抽常量（v5 P0-2）

- 文件路径：`backend_api_python/app/agent/chain/weight_feed.py`
- 要做的：**两处，缺一不可**
  1. 第 40 行改为
     ```python
     _VERDICT_SCORE = {"hit": 1.0, "partial": 0.5, "miss": 0.0, "delisted": 0.0}
     ```
     同步更新上一行注释（原文「undecidable/data_missing 不计入」⇒ 补 `delisted` **计入且按最差**）。
  2. ★ **新增模块常量 `_COUNTED_VERDICTS`**（2026-10-13 发现的第三处，原计划漏了）：
     ```python
     # 进聚合的 verdict 白名单（SQL 与测试共用同一份，禁止在 SQL 里硬编码）
     _COUNTED_VERDICTS = ("hit", "partial", "miss", "delisted")
     ```
     并把 `collect_domain_stats()` 的 SQL 从
     `WHERE r.verdict IN ('hit','partial','miss')` 改为使用该常量。
     ⚠️ 只改 `_VERDICT_SCORE` 而漏掉 SQL 白名单 ⇒ delisted **根本进不了聚合**，前功尽弃。
- 验证：`test_delisted_counted_in_weight_feed` 与 `test_delisted_in_counted_verdicts_whitelist` 双双转绿。

### 任务 B3：历史日期收益可复现（v4 §28.5 / v5 P0-3）

- 文件路径：`backend_api_python/app/agent/chain/evaluator.py`，函数 `_get_actual_return`

> **⚠️ 2026-10-13 二次修正（RED 测试实测后定稿，以此为准，前两版判据均作废）**
>
> **「取价走实时源」不成立**：该函数实际是
> `CNStockDataSource().get_kline(stock_code, "1D", max(hold_days + 10, 1))`，**已在取日 K 线**。
> **「同一次运行内不可复现」也不成立**：`test_historical_return_is_reproducible` **实测通过**。
>
> **但 RED 测试 `test_exit_date_matches_hold_days` 抓到了真问题（且更隐蔽）**：
> ```
> from_date=2026-08-29, hold_days=3
>   期望 exit_date = 2026-09-02（trading_calendar.next_trading_day 算出）
>   实际 exit_date = 2026-09-22      ← 偏 20 天
>   另：两次相隔数分钟的运行，pnl_pct 由 -0.43 变成 -1.04
> ```
> ⇒ 根因是**取数窗口只有 `hold_days + 10` 根（T+3 ⇒ 13 根）**，而窗口是「截至当前」取的。
> `from_date` 早于这 13 根时 `base_idx` 定位失败，落到错误锚点；
> 又因「当前」每天都在变，**同一历史日期在不同日子调用会得到不同结果**。
> ⇒ **这是「稳定地算错 + 随调用时刻漂移」，比「每次都不一样」更危险：它不报错，
> 只是安静地污染每一个 T+N 标签。** 这很可能就是 `undecidable` 占 56% 的来源之一。

- 要做的：**按 `from_date` 向前取足够长的窗口**（不是取「最近 N 根」），窗口长度走 env 禁止硬编码；
  使 `exit_date` 严格等于 `next_trading_day(from_date, hold_days)`。
- 验证：`test_exit_date_matches_hold_days` 转绿；`test_historical_return_is_reproducible` 保持绿。

### 任务 B4：验证 `weight_feed` 真喂数（v4 §27.12 / v5 P0-4）

- 文件路径：`backend_api_python/app/agent/chain/weight_feed.py`，函数 `feed()`
- 要做的：只读验证 + 定位。跑
  ```bash
  python -c "from app.agent.chain import weight_feed as w; print(w.feed(dry_run=True))"
  ```
  若 `sample_count` 仍全 0，定位 `collect_domain_stats()` 的过滤条件
  （大概率是 `verdict` 被 `undecidable`/`data_missing` 吃掉，与 B1/B2 同源），
  **先完成 B1/B2 再复测**，不要单独打补丁。
- 验证：`test_feed_reports_nonzero_samples` 转绿。

### 阶段 B 收尾

- 跑 `python -m compileall app/agent/chain/` ⇒ 通过
- 跑 `python -m pytest tests/test_v5_p0_closure.py -q` ⇒ **4 passed**
- 跑 `qd_smoke` ⇒ **不低于 A1 基线**；若下降，逐条定位再继续（不许带着回归进 P1）

---

## 3. 阶段 C：P1 三件（接线 + 两项拍板）

### ~~任务 C1~~：**已移出本批**（2026-10-13 修正：原计划内部不一致）

> **修正原因**：C1 原写「文件路径 `chain/label_spec.py`（P1-1 新建时一并加）」，
> 但 ① 实测 `app/agent/chain/label_spec.py` **不存在**；② 计划 §4 又把 P1-1（新建它）
> 列为「后续批次」。**两者矛盾，C1 在本批无法执行**。
>
> **处置**：`bar_freq` 预留（拍板 2）**并入 P1-1 建 `label_spec.py` 时一并定义**，
> 本批不做。理由：`bar_freq` 只是字段预留，不构成 P0/P1 的阻塞项（YAGNI）。
> 已同步到 §4 后续批次表。

⇒ **阶段 C 实际只剩 C2、C3 两个任务。**

### 任务 C2：few-shot 金例压缩存活守卫（拍板 4，已查证：单轮安全）

- 文件路径：`backend_api_python/tests/test_v5_compaction_guard.py`（新建）
- 背景（**已查证，勿重复查**）：`qd_service.py:479` `_prefetch` 在 `_run_sync` 内只调一次；
  `:447` 注入块拼进**首轮 user 消息**；mimoagent `compaction/surgery.py:47-65`
  把首轮 user 消息原样包成 `<original_user_issue>` anchor，`:233-237` 重建时保留
  ⇒ **单轮 run 下金例安全，此前「会被吞掉」的判断已更正**（v5 §19.6）。
- 要做的：写两个守卫测试，**钉住这个结论，防止将来改坏**：
  1. `test_first_turn_injection_survives_compaction` —— 构造 messages（system + 首轮含金例），
     调 `surgery` 的压缩重建，断言输出中仍能找到金例块指纹。
  2. `test_second_turn_injection_is_not_anchored` —— 断言**第二条** user 消息里的注入块
     不在 anchor 中（**记录事实**：多轮 session 复用 agent 时会丢）。
     ⚠️ 这条是**事实记录测试**，不是缺陷测试 —— 若将来支持多轮注入，此测试应随之更新。
- 验证：`pytest tests/test_v5_compaction_guard.py -q` ⇒ 2 passed。

### 任务 C3：`_QD_COMPACT_NOTES` 纵深防御（便宜，建议做）

- 文件路径：`backend_api_python/app/agent/qd_agent.py`，常量 `_QD_COMPACT_NOTES`（约 157–164 行）
- 要做的：在现有的 5 条保留要求后追加第 6 条：
  ```
  "(6) any [参考案例] / [参考资料] injection blocks present in the conversation, verbatim."
  ```
- 验证：`test_first_turn_injection_survives_compaction` 仍绿；
  `qd_smoke` 不低于基线。

---

## 3.5 阶段 D：启动采集（与阶段 B 并行，不记即丢）

> 原则：**先落盘，后进统计**。本阶段只保证数据开始流且不丢，不做任何统计分析。

### 任务 D1：C 类情境档案每日一帧（最高优先，不可重建）

- 文件路径：`backend_api_python/app/agent/chain/context_frame.py`（新建）
- 要做的：每日盘后落一帧 JSONL，字段至少含：
  `as_of`（日期）、`regime`（市场状态）、`theme_heat`（题材热度分档）、
  `vol_bucket`（波动分档）、`index_ret`、`limit_up_cnt`、`turnover`。
  落盘路径走项目既有 `tmp/` 约定，文件名含日期。
  **先落 JSONL，不建表**（v4 §21.2：C 类不可重建，先开始记比先设计表重要）。
- 验证：跑一次，断言当日 JSONL 文件存在且字段非空；连跑两次**同日去重**不重复追加。

### 任务 D2：分层配额选股（拍板 1 的最小版）

- 文件路径：`backend_api_python/app/agent/chain/sampler.py`（新建）
- 要做的：实现 `pick_daily_universe(date, n)`，按 **活跃 60% / 中热 25% / 低热+随机 15%**
  分层配额返回标的列表。配额**按 date 强制**（不许因当日活跃股多而多采），
  每条结果带 `sample_layer` 字段（活跃/中热/低热）以便分层记账。
  活跃池复用既有资产：`tools/finance/dragon_tools.py`、`skills/market_screener/`。
  ⚠️ 阈值走 env，禁止硬编码。
- 验证：单测断言三次调用的层比例恒为 0.6/0.25/0.15（容差 ±1 只）；
  且低热层**非空**（防止实现者因「价值低」跳过）。

### 任务 D3：双窗口标签接线（拍板 2 的落地）

- 文件路径：`backend_api_python/app/agent/chain/`（随 B3 的 `label_spec` 一起）
- 要做的：claim 落库时同时记 **T+1 与 T+3 两个标签**，各自带窗口字段与口径版本。
- 验证：单测断言一条 claim 产生两条窗口记录，且**互不覆盖**。

---

## 4. 后续批次（本计划不含，另行立项）

| 批次 | 内容 | 触发条件 |
|---|---|---|
| **P1-2/1-3/1-4** | `record_case` 扩展为 EV-1、注入块升级为「金例 + 判别特征检查清单」、修 `prompt_sha` missing（G10） | P0 全绿后 |
| **分层配额采集**（§19.2 拍板 1） | 活跃 60% / 中热 25% / 低热+随机 15%，按 `date` 强制配额、`sample_layer` 独立记账 | P1 完成后 |
| **P2 聚合器 + 配对双跑** | `chain/aggregator.py`、`exp_pair`、红线 R2 守卫 | G19（ρ_pair）可测之后 |
| **P3 条件校准与层级池化** | `exp_cond_calib`、τ 季度重估、<30 簇只出先验 | P2 稳定后 |
| **qd_agent_messages 落盘验证** | 确认 `postgres_memory.py:26` 是否真在写（§19.8 最小准备第 1 项） | 可与 P1 并行，只读 |

---

## 5. 验收总表（本批次做完的样子）

| # | 断言 | 出处 |
|---|---|---|
| 1 | `resolver` 有 `delisted` 分支，且正常活跃票不被误判 | B1 |
| 2 | `_VERDICT_SCORE["delisted"] == 0.0` | B2 |
| 3 | 同一历史日期两次 `_get_actual_return` 返回值完全一致 | B3 |
| 4 | `feed(dry_run=True)` 出现 `sample_count > 0` 的 domain | B4 |
| 5 | `condition_key` 默认 `bar_freq == "1d"` | C1 |
| 6 | 首轮注入块压缩后仍存活（守卫） | C2 |
| 7 | `_QD_COMPACT_NOTES` 含第 6 条保留要求 | C3 |
| 8 | `qd_smoke` **不低于 A1 基线** | 每阶段 |

> **未完成第 4 项，就不要建 `exp_*` 表** —— v5 §14 的原则：
> 标签不可靠时建表，只会得到一张漂亮的空表。

---

> **计划变更纪律**：实施中若发现计划与实际不符，**回头改本计划**，不要悄悄改代码。
