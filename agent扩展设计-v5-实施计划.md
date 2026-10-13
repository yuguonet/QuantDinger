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

> **2026-10-13 补记**：阶段 B 收尾后继续做了 §2.5（重算 + G24）。
> 重跑结果：`pytest tests/test_v5_p0_closure.py` ⇒ **9 passed / 1 skipped**；
> `qd_smoke` ⇒ **248/250**，2 条失败为**历史遗留**（守卫项
> `实现已归档（可回滚，不是硬删）` 找不到 `del/tools_returns_contract_20261010`），
> 与本批改动无关；`compileall` 通过。**零回归。**

---

## 2.5 阶段 B′：历史标签重算 + G24 统计口径（2026-10-13 新增）

> **立项**：B3 修完取数窗口后，怀疑库里 16 条 resolutions 是**修复前**产出的，
> 标签可能全算在错误窗口上 ⇒ 用户裁决「重算吧」。
> **结论：重算的收益实测为零，但过程中翻出一个比重算本身更重要的问题（G24）。**

### 任务 B5：重算 dry-run（**先算不写**，2026-10-13 完成）

- 脚本：`tmp/recompute_resolutions.py`（默认 dry-run，`--apply` 才落库）
- **结论（推翻立项假设）**：10 条**有效标签全部 `same`** —— 修复后的取数窗口
  算出的 verdict / score / as_of 与旧标签**完全一致**。
  原因：历史 claim 的 `exec_date` 距今很近，旧的 `hold_days+10` 窗口其实够用，
  B3 修的定位漂移只在**长跨度**下才发作。⇒ **B3 的修复对历史结论零影响。**
- 变化的 6 条（rid 13–18）全是与 19–24 **同 claim 的旧重复行**：
  - T+1 三条（13–15）undecidable → miss / miss / hit（与 19–21 结论相同）
  - T+3 三条（16–18）**仍是 undecidable**：执行日 2026-10-01 + T+3，只走到
    10/09、10/12 两个交易日（hold_days=2 < 3），**视界门② 正确拦截，不该强判**

### 任务 B6：G24 —— 一 claim 一票（2026-10-13 完成，TDD）

- **实测发现的真问题**：`qd_agent_resolutions` 是**判定留痕**而非「一 claim 一结果」
  —— `account_store.save_resolution()` 是纯 INSERT（无 UPSERT，`:245-256`），
  同一 claim 每次重新判定都**新增一行**。实测 **16 条 resolution 只对应 10 个 claim**
  （claim 89–94 各有 2 条）。
- **原 `collect_domain_stats()` 直接全表 JOIN + GROUP BY** ⇒ 同一 claim 的每条留痕
  都被当成一票。当前 `n=7` 侥幸正确，**只因为重复的 6 条恰好都是 undecidable**
  （不在白名单里）。一旦重算或重试后翻成 hit/miss，n 就随重试次数虚涨。
- **裁决（用户拍板）**：**保留留痕语义**（不动 `save_resolution`），
  只把统计口径改成「每 claim 只取最新一条」。
- **实现**（`chain/weight_feed.py`）：
  - 新增纯函数 `_dedup_latest(rows)` —— 按 `res_id` 最大去重
    （★ 依据是**时间顺序**，不是「哪条看起来更有用」；按 verdict 凑数就是 G18 同类错误）
  - 新增纯函数 `_aggregate_domain(rows)` —— 计数 + 计分，口径唯一真源
  - `collect_domain_stats()` 改为：SQL 只取**明细行**，去重/过滤/计分全在 Python
    （便于离线守口径，也为 P2/P3 按 `condition_key` 分组留好位置）
- **TDD**：RED 2 条（`_dedup_latest` / `_aggregate_domain` 不存在）⇒ GREEN
  ⇒ `9 passed / 1 skipped`
- **真库验证**：`finance / n=7 / win_rate 0.5714`，与修复前**完全一致**（零回归）

### 任务 B7：原地重算落库（2026-10-13 完成）

- **为什么是原地 UPDATE 而不是调 `save_resolution`**：后者是纯 INSERT，
  调它会新增 16 行 ⇒ 同一 claim 的有效标签重复计票 ⇒ 唯一基线当场虚涨。
- 落库前先备份：`del/v5_p0_backup_20261013/{claims,resolutions,decisions}_backup.json`
- 结果：**UPDATE 6 条，总行数仍 16**（未变化的不动）；不触碰 claim 状态
- 落库后自检：`n` 仍 = **7**（一 claim 一票口径生效，重复留痕没变成第二票）
- **幂等验证**：再跑一次 ⇒ **0 条变化**

### 本轮两条教训（写在这里以免重犯）

1. **脚本自身的两个 bug 差点伪造出「重算全部变 miss」的结论**：
   ① `_load()` 里把 jsonb 的 dict 一并 `str()` 成 `"{'dir': 'bearish'}"`（单引号），
   后续 `json.loads` 必失败 ⇒ 被兜底成 `{}` ⇒ `predicted` 静默清空
   ⇒ `verdict_by_rule` 拿 `""` 比对 ⇒ 方向类全部误判 miss；
   ② `item` 里漏拷 `predicted` 键，归一化循环把 `None` 清成 `{}`。
   ⇒ 已在脚本里加 **FATAL 自检**（predicted 被读空就当场退出，不静默出结果）。
2. **没有 dry-run 就会把污染写进库**。原计划「重算 16 条」会主动制造重复计票；
   先算不写 + 自检（n 必须仍为 7）才把它拦下来。

### 新增未闭合缺口

| 编号 | 缺口 | 状态 |
|---|---|---|
| **G24** | `save_resolution` 纯 INSERT ⇒ 一 claim 多条留痕；统计侧需「每 claim 取最新」 | **已修**（口径侧），根因按裁决保留留痕语义不动 |

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

> **2026-10-13 完成（实际写了 4 条，比计划多 2 条）**：
> 文件路径 `backend_api_python/tests/test_v5_compaction_guard.py`，**4 passed**。
> 1. `test_case_marker_matches_implementation` —— **新增，防假安全**：把常量
>    `CASE_MARK` 与 `case_memory.format_case_injection()` 的真实产出绑定校验
>    （指纹漂移而测试还绿 = 守了个不存在的字符串）。
> 2. `test_first_turn_injection_survives_compaction` —— 计划中的第 1 条，
>    额外断言金例**正文**逐字保留（能抓出「被按 token 截断」这种半截保留）。
> 3. `test_second_turn_injection_is_not_anchored` —— 计划中的第 2 条（事实记录）。
> 4. `test_repeat_compaction_keeps_original_anchor` —— **新增**：二次压缩时
>    anchor 不得漂移、不得套娃（依据 `surgery.py:49-50` 的「已是 anchor 则原样返回」）。
>
> **★ 配套做了变异检验**（`tmp/mutation_check_compaction.py`，不改源码只 monkeypatch）：
> 故意造 5 种破坏（anchor 截断 / 漂移到最后一条 user / 套娃 / 指纹笔误 / 漂移），
> **5/5 全部被抓到** ⇒ 证明这 4 条守卫不是假绿。
> 变异检验中发现并修正了一处**断言写错**：「不套娃」原先数 `CASE_MARK` 出现次数，
> 但套娃叠的是外层 `<original_user_issue>` 标签，金例正文本身仍只出现一次
> ⇒ 改数 `COMPACT_ORIGINAL_USER_ISSUE_PREFIX`，否则该断言恒绿。

### 任务 C3：`_QD_COMPACT_NOTES` 纵深防御（便宜，建议做）

- 文件路径：`backend_api_python/app/agent/qd_agent.py`，常量 `_QD_COMPACT_NOTES`（约 157–164 行）
- 要做的：在现有的 5 条保留要求后追加第 6 条：
  ```
  "(6) any 【历史相似案例——仅作参考，不要照搬结构】 or [参考资料] injection blocks "
  "present in the conversation, verbatim and in full — never truncate or paraphrase them."
  ```
- 验证：`test_first_turn_injection_survives_compaction` 仍绿；`qd_smoke` 不低于基线。

> **2026-10-13 完成**：已写入，并在常量下方加了「为什么必须写」的注释。
>
> **★ 修正计划原文的指纹笔误**：本计划 C3 原写 `[参考案例]`，但**实际**金例块前缀是
> 全角的 `【历史相似案例——仅作参考，不要照搬结构】`（`case_memory.py:444`）。
> 照原文写进去等于让模型去守一个**不存在的字符串**。已按真指纹写入。
>
> **为什么有了 anchor 还要这条**（纵深防御，不是重复劳动）：
> anchor（`surgery.py:47-65`）保护的是**首轮 user 消息**，注入块确实在其中；
> 但 `summary` 是**模型自己写的** —— 若摘要指令没点名这些块，模型可能认为已涵盖
> 而省略，且后续轮次无从察觉。本条让摘要侧也明确不许吞掉。

---

## 3.5 阶段 D：启动采集（与阶段 B 并行，不记即丢）

> 原则：**先落盘，后进统计**。本阶段只保证数据开始流且不丢，不做任何统计分析。

> **2026-10-13 完成（整体）**：D1 ✅（7 passed）、D2 ✅（7 passed）。
> **D3 未做** —— 发现计划的第二处内部矛盾，见 §3.6，需裁决后再动。

### 任务 D1：C 类情境档案每日一帧（最高优先，不可重建）

- 文件路径：`backend_api_python/app/agent/chain/context_frame.py`（新建）
- 要做的：每日盘后落一帧 JSONL，字段至少含：
  `as_of`（日期）、`regime`（市场状态）、`theme_heat`（题材热度分档）、
  `vol_bucket`（波动分档）、`index_ret`、`limit_up_cnt`、`turnover`。
  落盘路径走项目既有 `tmp/` 约定，文件名含日期。
  **先落 JSONL，不建表**（v4 §21.2：C 类不可重建，先开始记比先设计表重要）。
- 验证：跑一次，断言当日 JSONL 文件存在且字段非空；连跑两次**同日去重**不重复追加。

> **2026-10-13 完成**：`app/agent/chain/context_frame.py`（新建），
> `tests/test_v5_context_frame.py`（新建）⇒ **7 passed**。
> 真源实跑（凌晨 01:22）产出：
> `as_of=2026-10-12`（**正确回退**：今天未开盘）、`index_ret=-0.43`、
> `turnover=7.149e11`、`limit_up_cnt=46`、`regime=down`、`theme_heat=cold`、
> `vol_bucket=low`、`missing={}`（全部取到）。
>
> **★ 有意偏离计划一条**：计划写「落盘路径走 tmp/ 约定」，**改为 `data/context_frames/`**。
> 理由：tmp/ 是临时目录、会被清理，而 C 类**不可重建** ⇒ 放会被清的地方等于白记。
> `QD_CF_DIR` 可覆盖。
>
> **实测踩到的 4 个坑（勿重犯）**：
> 1. **未开盘陷阱**：凌晨跑时 `get_limit_pool`/`get_hot_rank`/`get_dragon_tiger`
>    全部返回 count=0（当天还没开盘）。若拿今天当 as_of，档案里就会记下
>    「某日 0 只涨停」—— 那是**假的**。⇒ 默认 as_of 取**最近已收盘交易日**
>    （`QD_CF_CLOSE_HM` 默认 15:05，留落地余量）。
> 2. `Path(__file__).parents[2]` 数错一级 ⇒ 落到 `app/data/`。正确是 `parents[3]`。
> 3. `count` 经 `_f()` 转成 float ⇒ 档案里出现 `46.0`。家数是**计数不是度量**，转 int。
> 4. 阈值测试别用「看着合理」的值：本例假数据收益恒定 ⇒ 波动率算出来就是 0，
>    `0 >= 0.01` 不成立 ⇒ 测试假失败。要用能确证 env 生效的确定值。

### 任务 D2：分层配额选股（拍板 1 的最小版）

- 文件路径：`backend_api_python/app/agent/chain/sampler.py`（新建）
- 要做的：实现 `pick_daily_universe(date, n)`，按 **活跃 60% / 中热 25% / 低热+随机 15%**
  分层配额返回标的列表。配额**按 date 强制**（不许因当日活跃股多而多采），
  每条结果带 `sample_layer` 字段（活跃/中热/低热）以便分层记账。
  活跃池复用既有资产：`tools/finance/dragon_tools.py`、`skills/market_screener/`。
  ⚠️ 阈值走 env，禁止硬编码。
- 验证：单测断言三次调用的层比例恒为 0.6/0.25/0.15（容差 ±1 只）；
  且低热层**非空**（防止实现者因「价值低」跳过）。

> **2026-10-13 完成**：`app/agent/chain/sampler.py`（新建），
> `tests/test_v5_sampler.py`（新建）⇒ **7 passed**。
> 真源实跑（n=150，as_of=2026-10-12）：`hot=71`（涨停 46 + 炸板 25）、
> `cold=23`、`warm=0`，`shortage={hot:19, warm:37}`、`missing=['hot_rank']`
> ⇒ **诚实反映源的状态**：热榜凌晨为空、板块成分股接口代理不通；
> hot 只有 71 只填不满 90 也是**当天的真实情况**（不是实现问题）。
>
> **三层定义（实测可用的源，勿凭记忆改）**：
> `hot`=涨停池(zt)+炸板池(broken)+龙虎榜+热榜前100名；
> `warm`=热榜 100 名之后 ∪ 热门板块成分股；`cold`=全市场随机（剔除前两层）。
> cold 用 `random.Random(as_of)` 以日期为种子 ⇒ 同日重跑结果一致（补跑不会换样本）。
>
> **实测踩到的 4 个真 bug（都是真源结构对不上，勿重犯）**：
> 1. `limit_pool` 的 `stocks` **嵌在 `zt` / `broken` 子键里**，不在顶层
>    ⇒ 直接抽顶层会拿到空（hot 层曾因此全空）。
> 2. 涨停池成分股字段名是 **`stock_code`**，不是 `code` / `symbol`
>    ⇒ 46 只全被抽成空。
> 3. `get_hot_sectors` 返回的 code（如 `new_cmyl`）被 `get_sector_stocks`
>    判为「非法板块代码」⇒ 必须传 **`board_name`** 走内部 `_resolve_board_code`。
> 4. 测试数据号段碰撞：`f"99{i:05d}"` 经「取后 6 位」归一后变成 `900000+`，
>    与 warm 池的 `90xxxx` 撞车 ⇒ warm 被去重成 0，测试变成在查号段不是查配额。
>
> **★ 现实约束（需持续观察）**：`get_sector_stocks` 依赖 `push2.eastmoney.com`，
> 实测**稳定失败**（代理不通）⇒ warm 层可能长期偏空。已加热榜延伸段作为第二通道，
> 但热榜在盘前/凌晨也为空。**盘后跑时这两源应可用，需在盘后复验。**

### 任务 D3：双窗口标签接线（拍板 2 的落地）

- 文件路径：`backend_api_python/app/agent/chain/`（随 B3 的 `label_spec` 一起）
- 要做的：claim 落库时同时记 **T+1 与 T+3 两个标签**，各自带窗口字段与口径版本。
- 验证：单测断言一条 claim 产生两条窗口记录，且**互不覆盖**。

> **2026-10-13：未做，存在计划内部矛盾（第二处），需裁决**
>
> **矛盾**：D3 原文写「文件路径：`chain/`（随 B3 的 `label_spec` 一起）」，
> 但 ① 实测 `app/agent/chain/label_spec.py` **不存在**；② §3 的 C1 已**因同一个
> 文件不存在而被移出本批**（裁决：`bar_freq` 并入 P1-1）。
> **⇒ 同一份计划里，C1 因缺 label_spec 被移出，D3 却仍依赖它。**
>
> **已查证的现状**：`claims.py:350` 的 `horizon = guess_horizon(...)` 是**单值**
> （从文本猜一个 horizon，默认 T+3）；`qd_agent_claims.horizon` 是单列。
> 即当前**不是**双窗口 —— 实测里同一 subject 出现 T+1 与 T+3 两组 claim，
> 是不同 decision 各自抽取的结果，不是同一预测的双窗口。
>
> **三个可选处置（待裁决）**：
> | 方案 | 做法 | 代价 |
> |---|---|---|
> | A 建 `label_spec.py` | 新建它作窗口口径单一真源（含 C1 的 `bar_freq`），D3 顺势落地 | 等于提前动 P1-1，跨批次 |
> | B 不建文件 | 窗口集合常量放 `claims.py`（`_HORIZON_DAYS` 旁边），抽取时展开成两条 claim | 口径分散，P1-1 建 label_spec 时还要再搬一次 |
> | C 再推 | D3 整个挪到 P1-1 与 `label_spec.py` 一起做 | 双窗口记账延后，期间攒的样本只有单窗口 |
>
> **另需一并决定**：「口径版本」字段放哪 —— claim 表**没有**该列。
> 可复用既有 `extractor_ver`，或加列（要迁移）。
> **以及**：双窗口会让 claim 数翻倍（150 只/日 × 3 claim × 2 窗口 ⇒ 900 条/日），
> 判定成本随之翻倍，需确认可接受。

> **2026-10-13 裁决（用户拍板）：方案 D + 口径先落 `resolver` 取数处**
> （来源：v5 实施 · 阶段 D3 · 2026-10-13）
>
> | 决策点 | 裁决 |
> |---|---|
> | 双窗口形态 | **方案 D：同一条 resolution 挂双窗口**（claim 不翻倍） |
> | 口径真源 | **先落 `chain/evaluator.py` 取数唯一真源处**，**不建** `label_spec.py`（P1-1 再收口） |
>
> **★ 为什么是 D —— 查证中发现的新事实（原三方案都没覆盖）**：
> 1. `evaluator.py:115` `_get_actual_return` **每次调用都重新 `get_kline`**
>    ⇒ 拆两条 claim / 调两次 = **取数成本真翻倍**（网络 IO，不是 CPU）。
> 2. 但同处 `_need = _span + hold_days + 10` 一次就拉回**整段 K 线**，
>    T+1 的 exit 就是 `base_idx + 1` ⇒ **同一份数据里本来就躺着 T+1**。
>    ⇒ 加一个「一次取数算多个 hold」的参数，边际成本**近零**。
> 3. `qd_agent_resolutions` 有 `actual JSONB` + `deviation JSONB`
>    ⇒ **挂双窗口不需要加列迁移**。
> 4. 表**没有** window 列 ⇒ 若落两条 resolution 只能靠 jsonb 区分，
>    且会撞刚修好的 **G24**（「每 claim 取最新一条」会把 T+1 那条吞掉）。
>
> **D 的语义（写死，实现不得偏离）**：
> - claim 的 `horizon` 仍**单值**，主判据仍 **T+3**（与 §0.4「T+3 作主判据」一致）。
> - 一次 `get_kline` 同时算 T+1 与 T+3，两个窗口的 actual / deviation
>   分别挂进 resolution 的 `actual.windows` / `deviation.windows`。
> - `verdict` / `verdict_score` 列**仍只取 T+3** ⇒ `weight_feed` 计票口径
>   **完全不变**，G24（一 claim 一票）不受影响，无需改去重逻辑。
> - **T+1 只报告、不进权重** —— 否则同一预测被记两票，与 G24 是同一类错误。
> - 「口径版本」**不加列**：复用既有 `resolver_ver`（该列本来就是留给
>   「判据改动留痕」的，见 `agent_v4_trace.sql:105`）。原问题「claim 表没有该列」
>   由此消解 —— 不需要在 claim 侧记口径版本。
>
> **接受的代价**：T+1 的结论要**随 T+3 一起出**，失去「T+1 当晚就看」的即时性。
> 理由：T+1 只作**快速诊断**（看预测是否立刻兑现），真正进条件校准的是 T+3；
> 等到 T+3 时**不丢任何信息**（T+1 的行情那时已全部可得、可回算）。
> ⇒ 这与方案 C「延后做」**有本质区别**：C 事后**补不出** T+1（当时没留数据），
> D 是**晚一点但完整**。
>
> **窗口集合**：env 可配 `QD_LABEL_WINDOWS`（默认 `T+1,T+3`），
> 常量落在 `chain/evaluator.py` 取数唯一真源处 —— **不另起一套取数逻辑**
> （本项目老教训：两套取数口径必然漂移）。**禁止硬编码窗口列表。**

> **2026-10-13 完成（TDD）**（来源：v5 实施 · 阶段 D3 · 2026-10-13）
>
> **改了什么**：
> - `chain/evaluator.py`：`_get_actual_return` 新增 `extra_holds` 参数；
>   内部抽出 `_window(hold)` 闭包 ⇒ **主窗口与附加窗口共用同一份算数口径**
>   （不另写一套，否则必然漂移）。返回值新增 `windows`（键为**天数字符串**）
>   + `truncated` + `hold_days_expected`。**不传 `extra_holds` 时行为与改动前完全一致**。
> - `chain/evaluator.py` 新增窗口口径单一真源 `HOLD_LABELS` / `LABEL_HOLDS`；
>   `chain/resolver._horizon_days` **改为委派到它**，消除 claim 判定侧的第二套映射。
> - `chain/resolver.py`：新增 `_actual_view()`（主/附窗口**共用**转换）、
>   `_days_label()`、`_label_windows()`（读 `QD_LABEL_WINDOWS`）；
>   `fetch_actual` 输出 `actual.windows`（**horizon 标签键**）；
>   `_resolve_finance` 输出 `deviation.windows`。
> - `verdict` / `verdict_score` **仍只取主窗口 T+3** ⇒ `weight_feed` 计票口径
>   **零改动**，G24 不受影响（已由守卫测试钉死）。
>
> - 新增 `tests/test_v5_dual_window.py` ⇒ **11 passed**（7 RED + 4 守卫）。
>
> **变异检验 9/9 全被抓到**（`tmp/mutation_check_dual_window.py`，不改结构只注入单点错误）：
> M1 附加窗口被丢弃 · M2 每个窗口各取一次数（成本翻倍）· M3 verdict 改用 T+1 ·
> M4 忽略 env · M5 键用裸数字 · M6 附加窗口 `truncated` 被主窗口带偏 ·
> M7 deviation 不记 windows · M8 主窗口被排除出 windows ·
> M9 G24「一 claim 一票」被改回「一留痕一票」。
> ⇒ 与 C2 一样，**只有变异检验通过才准交付**（防假绿）。
>
> **真源实跑（2026-10-13 01:48，决策日 2026-09-29）**：
> | 标的 | T+1 | T+3 | get_kline |
> |---|---|---|---|
> | 600519 | 09-30 **+1.86%** | 10-09 **+2.22%** | **1 次** |
> | 000001 | 09-30 **+1.94%** | 10-09 **+2.11%** | **1 次** |
> | 300750 | 09-30 **+1.50%** | 10-09 **+3.82%** | **1 次** |
> ⇒ **「一次取数算多窗口」在真源下成立**（每只票只 1 次 `get_kline`）。
> 注：09-30 与 10-09 之间跨国庆假期，K 线里没有休市日 ⇒ T+2 落在 10-08、
> T+3 落在 10-09，与交易日历一致。
>
> **★ 有意不合并的一处口径分裂（留给 P1-1，勿"顺手统一"）**：
> `evaluator._TIMEFRAME_DAYS`（旧链路 `evaluate_pending()`，1M=22 / 3M=66，
> **自然月**口径）与新增 `HOLD_LABELS`（claim 判定，1M=20，**交易日**口径）
> **故意并存**。强行统一会**静默改掉旧链路判据**（22→20 或反之）。
> 已在代码注释里写明，P1-1 建 `label_spec.py` 时连同 `bar_freq` 一起收口。

---

## 3.7 阶段 E：口径收口 + prompt_sha 修复 + 采集接线（2026-10-13 无人值守批）

> 执行方式：用户授权**全程无人值守**，按推荐顺序推进，遇阻自动跳过并记录。
> 来源：v5 实施 · 阶段 E · 2026-10-13

### 任务 P1-1：`chain/label_spec.py` 标签口径单一真源 ✅

- **为什么做**：口径曾在**三处**各写一份且**数值不同** ——
  `evaluator._TIMEFRAME_DAYS`（1M=**22** 自然月）、`evaluator.HOLD_LABELS`（1M=**20** 交易日）、
  `claims._HORIZON_DAYS`（又一份 1M=20，含 `n/a: 0`）。改一处忘两处 = **静默漂移**。
- **做了**：新建 `app/agent/chain/label_spec.py`，含
  `CLAIM_HOLDS` / `CLAIM_LABELS`（claim 判定，交易日）、`TRACE_HOLDS`（旧链路 traces，自然月）、
  `bar_freq()`（拍板 2 预留字段，默认 `1d`，env `QD_BAR_FREQ` 可配、脏值回落默认）。
  三处调用方（evaluator / claims / resolver）**全部改为委派**。
- 新增 `tests/test_v5_label_spec.py` ⇒ **11 passed**。
- ★ **`test_no_fourth_copy_in_chain`**：扫描 `app/agent/chain/*.py`，除 `label_spec.py` 外
  不得再出现 `"T+1": 1` 形式的字面量映射 ⇒ 后人再写一份就会红。
- ★ **两组口径故意不合并**（`CLAIM_HOLDS` 1M=20 vs `TRACE_HOLDS` 1M=22）：
  强行统一会**静默改掉旧链路判据**（22→20 或反之）。已在代码与测试里双向钉死。

### 任务 P1-4：修 `prompt_sha` 恒 missing（G10） ✅

- **成因（已查证）**：`utils/tracing.py::_repro_meta` **硬编码 5 个 prompt 文件名**求 sha，
  其中 `plan_system.txt` / `code_agent.yaml` / `intent_classifier.txt` **早已删除**
  （实测 `app/agent/prompts/` 只剩 qd_core_base.txt / qd_system.txt / skill_brew.txt / skill_revise.txt）
  ⇒ `prompt_sha` 恒为 **3/5 `"missing"`** —— 静默降级，复盘时对不上版本，
  而这正是 `_repro_meta` 存在的唯一理由。
- **修法（根治，非打补丁）**：改为**扫描目录**（`.txt/.yaml/.yml/.md`），
  删了/加了自动跟随，结构上不可能再出现恒 missing。
  `QD_PROMPT_SHA_FILES` 可显式收窄。读不了**仍标 missing**（不许把"有问题"抹成"没问题"）。
- 新增 `tests/test_v5_prompt_sha.py` ⇒ **6 passed**。

### 任务 D4：盘后采集接线 ✅（★ D1/D2 的价值兑现点）

- **为什么最高优先**：D1（情境帧）与 D2（分层配额）只提供了函数，**没有任何地方调用**
  ⇒ 数据一天都不会攒。而 C 类情境**不可重建**（今天没记，事后补不回来）。
- **做了**：新建 `app/agent/chain/collector.py`（编排：情境帧 + 分层配额样本，
  分项隔离、同日覆盖幂等、失败进 `errors` 且 `ok=False`、**不抛异常**给 worker）；
  `evaluator` 新增 `run_v5_collection()` 钩子并挂进盘后 worker（在验证之后、酿造之前）。
- 新增 `tests/test_v5_collector.py` ⇒ **10 passed**。
  ★ 含 `test_worker_source_calls_the_hook`（源码级守卫，防接线被删而无人察觉）。
- **真源实跑**：`as_of=2026-10-12`，帧 `regime=down/heat=cold/vol=low/idx_ret=-0.43/zt=46/missing={}`；
  样本实采 94（hot 71 + cold 23），`shortage={hot:19,warm:37}`、`missing={hot_rank: 热榜为空}`；
  **同日重跑幂等**（样本 1 条、帧 1 行）。

> **★ 本轮新增的项目事实（极其重要，勿忘）**：
> **`chain.X` 与 `app.agent.chain.X` 不是同一个模块对象** ——
> `app/agent` 目录被单独加进 `sys.path`，故两套路径各有一份模块，
> **模块级常量互相独立**。生产代码用短路径 `from chain import ...`，
> 测试用长路径 import ⇒ 只 patch 长路径**不生效**，表现为「patch 了却像没 patch」。
> 曾让 4 条测试假失败（还误触真源网络，测试从 0.35s 拖到 59.6s）。
> 修法：测试里**同时 patch 两个路径**（见 `test_v5_collector.py::_patch`）。

---

## 3.8 阶段 F：EV-1 证据契约 + 注入块升级 + 聚合器 + 条件校准（2026-10-13 无人值守批 · 第二批）

> 执行方式：同 §3.7，全程无人值守。接 §3.7 之后自动继续，未中断。
> 来源：v5 实施 · 阶段 F（P1-2 / P1-3 / P2 / P3）· 2026-10-13

### 任务 P1-2：`chain/evidence.py` EV-1 证据向量契约 ✅

- **做了**：新建 `app/agent/chain/evidence.py`，定义 C3P 第②段的产出契约
  （`ev_ver / condition_key / subject / claims / discriminators / falsifiers /
  evidence_strength / provenance{prompt_sha, producer_ver, evidence_class, as_of, bar_freq}`）。
- **不改表结构**：加列是**不可逆 DDL**，用户不在场不做 ⇒ EV-1 先落 **JSONL**
  （与 D1 情境帧同款：先落盘、后进统计）。契约稳定后再换 INSERT，上层零改动。
- **★ 不伪造原则**：`discriminators / falsifiers` 需 LLM 证据生成，而**该接线尚不存在**
  ⇒ 留 `[]` 是**正确行为**，并如实标 `evidence_class="weak"`。
  **绝不填 `"待补充"`/`"无"` 占位符** —— 那会让下游把「没有判别特征」
  误读成「判别特征说明此处不适用」，事后**无法与真"没有"区分**。
  （此条已由变异检验 M2 钉死，见下。）
- **条件空间封顶**：`_DIM_CARD = {regime:3, vol_bucket:3, theme_heat:3}`、
  默认维 `("regime","vol_bucket")` = 9 格；`max_cells()` 默认 12（`floor(361/30)`）。
  加开 `theme_heat` ⇒ 27 格 ⟶ `assert_cell_budget()` **如实报错**。
- **红线 R1 执行点**：`_strip_llm_probability()` 丢弃 `p_up/probability/confidence_llm/...`
  并**记违规**（保留字段名以便追溯）。
- `evidence_strength` **随内容计算**，非常量；`evidence_class`：有 falsifier⇒strong /
  有 discriminator⇒medium / 否则 weak。
- 新增 `tests/test_v5_evidence.py` ⇒ **14 passed**。

### 任务 P1-3：注入块升级为「金例 + 判别特征检查清单」 ✅

- **做了**：`utils/case_memory.py` 新增 `_build_outcome(confidence, evidence)`
  与 `_checklist_lines(ev)`；`format_case_injection(cases, with_checklist=True)`
  在每条案例后追加清单（适用市况 / 判别特征 / 证伪条件）。
- **evidence 存 `outcome.evidence` 子键，不加列**；没有证据就**不塞空壳**。
- **★ 向后兼容钉死**：返回串**仍以 `【历史相似案例——仅作参考，不要照搬结构】` 开头** ——
  C2 压缩守卫与 C3 摘要指令都依赖该指纹，改了会**连带打穿既有守卫**。
- `record_case` 新增 `evidence` 参数。
- 新增 `tests/test_v5_case_injection.py` ⇒ **12 passed**（与 C2 守卫同跑 **16 passed**）。

### 任务 P2：`chain/aggregator.py` 条件化聚合器 + 配对双跑 ✅

- **公式**：`logit(p_final) = logit(p_base) + β₀(ck) + Σ βⱼ(ck)·eⱼ`，
  `|Δlogit| ≤ LOGIT_DELTA_MAX`（默认 0.4，env `QD_AGG_LOGIT_MAX`），
  违反/出错 ⇒ **fail-open 退回 `p_base`**（不是 0.5，不是任意数）。
- **★ 当前是骨架不是成品**：β 需配对样本拟合，而**一条配对都还没有**（采集刚启动）
  ⇒ **无系数时 `p_final == p_base`（Δ=0）是正确行为**。
  凭空编系数 = 拿噪声当信号，且事后与真系数**无法区分**。
- **红线 R2**（`p_base` 路径禁止接入 `qd_agent_weights`/`exp_*`）：
  若"基线"里混进它自己的历史战绩，Δp 会**自我强化**（越涨越信、越跌越怂），
  校准得到的将是**回声而非增量**。⇒ 源码级守卫
  `test_r2_aggregator_never_reads_feedback_tables`（**先 `re.sub` 剥 docstring 再扫**，
  否则"说明禁令"的注释会被自己误伤）。
- **★ `_coef()` 严格 vs `_as_float()` 宽松**：
  系数**键不存在⇒0.0**（合理缺省）；**值非数字⇒抛错走 fail-open**——
  静默当 0 正是最危险的"温和漂移"。证据内容用宽松版。此区分由变异 M4 钉死。
- 裁剪**必须记录**（`clipped=True`）；`logit()/sigmoid()` 边界夹 `1e-6`。
- 配对留痕 `record_pair()`（同 `(as_of, claim_id)` 覆盖，env `QD_PAIR_DIR`）。
- 新增 `tests/test_v5_aggregator.py` ⇒ **14 passed**。

### 任务 P3：`chain/calibration.py` 条件校准与层级池化 ✅

- **可解释优先**：按格做频率校准 `β₀(格) = logit(该格实际胜率) − logit(该格 p_base 均值)`，
  可审计、无需迭代优化、天然条件化。**不上来就搞逻辑回归**。
- **★ 样本不足 ⇒ 不出系数**：< `min_clusters()`（默认 30，env `QD_CALIB_MIN_CLUSTERS`）
  的格 **只出先验（`b0=None`）**。给 8 个样本拟合的 β₀ 与真系数**长得一模一样**，
  事后无法区分 —— 这是本项目**最贵的一类错误**。
- **层级池化**：`"up|high"` → 父层 `"up|*"` → 全局 `"*"`，每借一级**如实标注**
  `source_level` / `pooled_from` / `sources`。
  ★ `n_clusters` **始终是本格自己的**；借来的记在 `pooled_n_clusters` ——
  否则下游会把"全局先验"当"该格专属修正" ⇒ **静默放大**。
- **τ 季度重估**：`fitted_at` 超 `tau_days()`（默认 90）标 `stale`，
  **过期不删除**（留着可审计），仅 `usable=False`。
- 新增 `tests/test_v5_calibration.py` ⇒ **13 passed**。

### 阶段 F 变异检验（mutation testing）：9/9 全被抓到 ✅

> 目的：证明上述守卫**真能咬人**，而不是"写了一堆恒真的假绿测试"。

| # | 注入的错误 | 被哪条测试抓到 |
|---|---|---|
| M1 | R1 不拦 LLM 自报概率 | `test_r1_*` |
| M2 | discriminators/falsifiers 填占位符 | `test_no_placeholder_*` |
| M3 | Δlogit 不裁剪 | `test_clip_*` |
| M4 | fail-open 退回 0.5 | `test_fail_open_*` |
| M5 | 样本不足也给 β₀ | `test_min_clusters_*` |
| M6 | 池化不标注来源层 | `test_pooling_marks_source_*` |
| M7 | 样本落盘不幂等 | `test_idempotent_*` |
| M8 | 两组口径被顺手统一（1M 22→20） | `test_two_horizon_sets_*` |
| M9 | prompt_sha 回退硬编码清单 | `test_scan_dir_*` |

- 变异脚本：`tmp/mutation_check_v5_batch.py`；备份 `del/v5_mutation_20261013_e/`。
- **事后 `diff` 逐文件确认 7 个文件全部还原**（evidence / aggregator / calibration /
  collector / label_spec / case_memory / tracing 均 OK）—— 变异不留残留。

### 阶段 F 回归结论

- 12 个 v5 测试文件 **118 passed / 1 skipped**（10.44s）。
- `compileall` rc=0；`qd_smoke` **248/250**（2 条历史遗留，见 §6）。
- **零回归**。

---

## 3.9 阶段 G：口径分裂**统一**（2026-10-13 · 用户质疑后查证推翻原判断）

> 来源：v5 实施 · 阶段 G · 2026-10-13
> 触发：用户质疑 §3.7「两组口径故意不合并」——「口径分裂肯定需要统一」。
> **结论：用户是对的，原判断错了，已推翻并统一。**

### ★ 原判断错在哪（查证过程，勿重复查）

原判断：`TRACE_HOLDS`（1M=22）是**自然月**口径，与 claim 的**交易日**口径语义不同，
故不许合并。**三条证据推翻它**：

1. **消费端按交易日解释**（决定性）：`evaluator._maturity_days` 的 docstring 原文
   「hold_days 按交易日计（_TIMEFRAME_DAYS）」，且 `_get_actual_return` 用
   `base_idx + hold_days` 偏移 **K 线根数** ⇒ 22 虽是"按自然月折算出的数"，
   却被当成**交易日**消费。⇒ 两者是**同一个量**，不是两种语义。
2. **22 在算术上站不住**：22 × 12 = 264 > `1Y=252`（一年没有 264 个交易日）
   ⇒ 与表内最硬的锚点 `1Y=252` **不自洽**。
3. **影响面实测为零**（见下）⇒ 统一没有任何历史负担。

### 影响面实测（决策依据，不是估计）

| 对象 | 实测结果 | 结论 |
|---|---|---|
| `qd_agent_traces` 根节点 | **1256 条，timeframe 全部为空**（恒走 `DEFAULT_HOLD_DAYS=3`） | 22/66/252 **从未被消费过**，是死值 ⇒ 改它零影响 |
| `qd_agent_claims` | 128 条：T+1=74、T+3=51、**1M=3** | 只有 3 条用 1M |
| 那 3 条 1M claim | `status=pending`、`due_date=2026-11-10`（未到期）、**resolution=0** | 尚未产生任何标签 ⇒ **无需重算** |

⇒ 统一的历史负担：**0 条已定论记录**。

### 统一方案：一张表 `HOLDS`（`SCHEMA_VER` ls1 → **ls2**）

```python
HOLDS = {"T+1":1, "T+3":3, "T+5":5, "1W":5, "2W":10,
         "1M":21, "3M":63, "1Y":252, "n/a":0}   # 单位恒为**交易日**
```

- **1M=21 / 3M=63** = `1Y=252` 的 1/12 与 1/4 —— 唯一与锚点自洽的取值
  （旧值 22/66 不满足：`22×12=264 ≠ 252`）。
- 旧名 `CLAIM_HOLDS` / `TRACE_HOLDS` / `CLAIM_LABELS` / `TRACE_HOLD_DEFAULT`
  **全部删除**（不留别名 —— 留别名等于给分裂留复活入口）；
  `claim_hold_days` / `trace_hold_days` 合并为 `hold_days`（严格）+ `resolve_hold_days`（宽松）。
- 调用方三处（evaluator / claims / resolver）全部改指向 `HOLDS`。

### ★ 顺手修掉的一个隐患：反查表被 `1W` 覆盖

`T+5` 与 `1W` **同为 5 天**。原写法 `{v: k for k, v in HOLDS.items() if v > 0}`
是**后覆盖** ⇒ `hold_label(5)` 会静默变成 `"1W"`（同一天数两个入口，口径又会飘）。
改为**先到先得**，规范名固定为 `T+5`，并由测试钉死。

### 防复活守卫（3 条，源码级）

| 守卫 | 作用 |
|---|---|
| `test_single_source_of_truth` | `label_spec` 上不得再出现 `CLAIM_HOLDS`/`TRACE_HOLDS` 等旧名（防分裂复活） |
| `test_long_horizons_are_self_consistent` | `1M×12 == 1Y` 且 `3M×4 == 1Y` ⇒ 改一个必须改全部，22 这种值进不来 |
| `test_no_legacy_alias_definition_in_chain` | 扫描 `chain/*.py` 禁止再**定义**旧名（允许注释里提及溯源） |

### 变异检验 8/8 全被抓到（`tmp/mutation_check_label_spec.py`）

M1 1M 改回 22 / M2 3M 改成 66 / M3 反查表改回后覆盖 / M4 复活 `CLAIM_HOLDS` /
M5 evaluator 自持一份 / M6 `n/a` 只按大写查 / M7 宽松兜底改成 0 / M8 claims 自持一份。
事后逐文件 `diff` 确认**全部还原**（备份 `del/v5_label_spec_mutation_20261013/`）。

### 回归

v5 全量 **120 passed / 1 skipped**（较 118 +2，即新增的两条守卫）；
全量 `tests/` **511 passed / 4 skipped**；`compileall` rc=0。**零回归。**

---

## 3.10 阶段 H：P2 接线 —— 把 C3P 的 ②③ 段接进生产链路（2026-10-13）

> **立项依据**：v3 合并重写（同日）的**附录 F 偏差核对**。核对结论是「4 个模块是孤岛」：
> `evidence.build_evidence()` / `aggregator.aggregate()` / `aggregator.record_pair()` **生产代码零调用方**，
> `case_memory.record_case(..., evidence=)` 参数就绪但 `qd_agent._record_case()` **不传**。
> ⇒ **`p_final` 从未被真实产出、Δp 一条都没落过**。本阶段就是把它们接上。
>
> **本阶段只做接线，不做调参**：不动 `aggregate()` 的数学、不动 `app/watchlist/predict.py`（红线 R2）、不动主 prompt。

### H.0 两个已拍板的规格（2026-10-13，用户选定）

| 决策点 | 选定 | 否决方案与理由 |
|---|---|---|
| **EV-1 怎么产出** | **收尾一次轻量 LLM 调用**（~400 token，带超时 + fail-open） | ❌ 否决「主答复内嵌 `<ev1_json>` 块」：① 要改 `qd_system.txt`，会动 smoke/bench 基线；② 需从最终答复清洗 JSON，否则用户看到；③ **模型合规率未知** ⇒ 不产出就全部降级，且事后分不清「没经验」还是「没产出」。<br>❌ 否决「只接零成本部分（claims + 情境键）」：没有 `falsifiers` ⇒ 按 v3 §19.1 恒降级 ⇒ Δp 恒 0，等于没接 |
| **默认开关** | **开**（`QD_C3P_ENABLED=1`），立即积累 | 另一选项是默认关。选开的理由：P3 校准一上线就需要 pair 数据，**数据是唯一不能补的东西**（C 类特征同理） |

### H.1 接线位置：**claim 级，不是 run 级**（关键裁定）

```
qd_agent._finish_collector(message, status)
  → _intake_accountability(...)   # 已有：返回 last_accountability{tracked, decision_id, claim_ids}
  → ★ _c3p_pipeline(...)          # 新增（本阶段）：一次推理 → 证据 + 配对
  → _record_case(...)             # 已有：★ 本阶段顺手把 evidence 传进去（补 A-4 写侧）
```

**为什么必须 claim 级，不能 run 级**：

1. `aggregator.record_pair()` 的**第一个参数就是 `claim_id`** —— 契约就是按 claim 设计的；
2. Δp 的 `y`（真值）来自该 claim 的 `resolution` ⇒ **必须按 claim 配对**，按 run 配对会对不上；
3. run 级可能没有单一标的（"帮我看看大盘"），claim 一定有 `subject`（`claims.guess_subject` 抽不出标的就不产 claim）。

### H.2 触发条件（全满足才做，任一不满足即跳过并记 `reason`）

| # | 条件 | 不满足时的 reason |
|---|---|---|
| 1 | `QD_C3P_ENABLED` 为真 | `disabled` |
| 2 | intake `tracked == True` | `not_tracked` |
| 3 | `claim_ids` 非空 | `no_claim` |
| 4 | claim 的 `subject` 能解析成个股代码 | `no_subject` |

### H.3 流程（六步，逐段独立 try，任何一段挂了都不阻断主链）

| 步 | 做什么 | 失败时 |
|---|---|---|
| ① | 取情境帧：`context_frame.load_frames()` 最后一帧 | `frame=None` ⇒ `condition_key` 全 `unknown`（**不猜**——猜出来的条件键会把样本分进错误的格子且事后无法识别） |
| ② | 产出 EV-1：一次轻量 LLM → `evidence.build_evidence(...)` → `append_evidence(...)` | `evidence=None` ⇒ 降级（见 H.4） |
| ③ | 取 `p_base`：`kline_feed.daily_bars(subject)` + `kline_feed.market_bars()` → `predict_next_day` → `p_up` | **取不到 ⇒ 不记 pair**（没有对照臂的 Δp 无意义，记了就是假数据） |
| ④ | 聚合：`aggregator.aggregate(p_base, evidence, condition_key, coefs)` | 内部已 fail-open（退回 `p_base`） |
| ⑤ | 记账：`aggregator.record_pair(claim_id, p_base, p_final, ...)` | 写盘失败 ⇒ 记 warning，不影响答复 |
| ⑥ | 回传 `evidence` 给 `_record_case` ⇒ 落 `outcome.evidence` | 补 A-4 的**写侧**（读侧早已就绪） |

### H.4 fail-open 语义（写死，不许改成抛异常）

- **LLM 无返回 / 解析失败 / 无 `falsifiers`** ⇒ `evidence=None` ⇒ `p_final == p_base`，**仍记一条 pair**，标 `degraded=True`。
  - 为什么不干脆不记：G20 的监控（`median(|Δp|)`、`P(|Δp|<0.01)`）需要「降级比例」这个分母——不记就永远看不出是「没经验」还是「没产出」。
  - 但按 v3 §19.1：**`degraded=True` 的 pair 不进正式判定队列**。
- **`p_base` 取不到** ⇒ **不记 pair**（与上面相反：对照臂缺失的 Δp 是假数据，不是降级数据）。

### H.5 ⚠️ 接线后 Δp 恒为 0 是**设计内的预期行为**，不是 bug

`aggregate()` 的 `coefs` 来自 P3 校准（`chain/calibration.py`），**P3 尚未上线 ⇒ `coefs=None` ⇒ `Δlogit=0` ⇒ `p_final == p_base`**。

依据 v3 §15.1：「无经验时（新格子 / 冷启动）：`β₀ = 0, βⱼ = 0` ⇒ `p_final = p_base`，fail-open 天然成立。」

⇒ **本阶段的价值 = 开始积累 pair 数据**（`p_base` + `condition_key` + `evidence`），P3 一上线就能出 β。
⇒ **验收看的是「pair 是不是在落、condition_key 对不对、evidence 有没有回传」，不是看 Δp 有没有非 0。**

### H.6 新增模块

| 模块 | 职责 |
|---|---|
| `chain/c3p.py` | 编排：六步流水线 + 触发条件 + fail-open。**唯一新增的编排层** |
| `chain/kline_feed.py` | K 线统一取数：`daily_bars(code, n)` / `market_bars()`，返回 `predict_next_day` 要的形状（`time` 为 **int 时间戳**） |

**为什么要新建 `kline_feed.py`（不是抄第三份）**：项目约定「重复调用代码段 2 处以上 ⇒ 生成统一接口」。当前 K 线取数已有 3 处：
① `chain/evaluator._get_actual_return`（内联 `CNStockDataSource`，要 `t` 为日期串）
② `skills/market_screener/common.py::fetch_kline`
③ 本阶段新增。
⇒ `kline_feed` 同时给 `t`（`YYYY-MM-DD`）与 `time`（int ts）两个键，**两个消费方都能用**；
**但本阶段不迁移 `evaluator`**（它刚在 P0-3 修过且有守卫，动它风险大于收益）—— 迁移记入遗留表，作为 O-14 后续项。

### H.7 新增 env（禁止硬编码）

| 变量 | 默认 | 作用 |
|---|---|---|
| `QD_C3P_ENABLED` | 1 | 总开关 |
| `QD_C3P_MAX_CLAIMS` | 2 | 一次最多处理几条 claim（控成本） |
| `QD_C3P_LLM_MODEL` | "" | 证据提取用的轻量模型（空 = 走默认） |
| `QD_C3P_LLM_TIMEOUT` | 20 | 证据提取超时（秒），超时即 fail-open |
| `QD_C3P_BARS` | 120 | 取多少根日线（必须 ≥ `predict.MIN_BARS=60`） |

### H.8 P2-4 红线 R2 守卫（本阶段一并做）

`qd_smoke.py` 新增断言：扫描 `app/watchlist/**/*.py`，**不得**出现 `app.agent.chain` / `from chain` / `import chain` 的 import。

> 现状：R2 **当前未被违反**（实测 `app/watchlist/` 未 import `app.agent.chain`），但**没有守卫** ⇒ 将来有人接上不会 FAIL。本阶段补上这个守卫。

### H.9 TDD 用例清单（`tests/test_v5_c3p.py`，先红后绿）

| # | 用例 |
|---|---|
| 1 | 开关关 ⇒ 跳过，`reason="disabled"` |
| 2 | `tracked=False` ⇒ 跳过，`reason="not_tracked"` |
| 3 | `claim_ids` 空 ⇒ 跳过，`reason="no_claim"` |
| 4 | subject 解析不出 ⇒ 跳过，`reason="no_subject"` |
| 5 | **p_base 取不到 ⇒ 不记 pair**（关键：没有对照臂的 Δp 是假数据） |
| 6 | EV-1 正常 ⇒ 记 pair，`p_base`/`p_final`/`delta_logit` 齐全，`condition_key` 正确 |
| 7 | `evidence=None`（LLM 挂了）⇒ fail-open：`p_final == p_base`，**仍记 pair**，`degraded=True` |
| 8 | 情境帧缺失 ⇒ `condition_key` 全 `unknown`（**不猜**） |
| 9 | evidence 回传 `_record_case` ⇒ 落 `outcome.evidence`（**补 A-4 写侧**） |
| 10 | 全链路 fail-open：LLM 抛异常 ⇒ 不抛给用户，答复照常 |
| 11 | R2 守卫：注入违规 import ⇒ 必须 FAIL |

### H.10 变异检验（≥6 个，证明守卫有真实 bite）

M1 开关失效（关了也跑）· M2 p_base 不可用时仍记 pair · M3 fail-open 改成抛异常 ·
M4 `condition_key` 缺失时猜默认值 · M5 evidence 不回传 `record_case` · M6 R2 守卫空转

### H.11 回归

v5 全量 pytest + 全量 `tests/` + `compileall` + `qd_smoke --with-env`，与基线对比零回归。

---

> **§6 遗留表第 4 条的更正**（该条写于口径分裂统一之前，**已作废**，以附录 C / 本条为准）：
> 「`label_spec` 与旧链路 `_TIMEFRAME_DAYS` 最终收口 / 两组口径故意不合并」——
> 已在 **§3.9 阶段 G** 统一为单一 `HOLDS` 表（1M=21/3M=63/1Y=252），旧名全部删除不留别名。

## 4. 后续批次（本计划不含，另行立项）

| 批次 | 内容 | 触发条件 |
|---|---|---|
| ~~**P1-2/1-3/1-4**~~ | ~~`record_case` 扩展为 EV-1、注入块升级为「金例 + 判别特征检查清单」、修 `prompt_sha` missing（G10）~~ | **✅ 已完工**（§3.8） |
| **分层配额采集**（§19.2 拍板 1） | 活跃 60% / 中热 25% / 低热+随机 15%，按 `date` 强制配额、`sample_layer` 独立记账 | **✅ 已完工**（D2 采样器 + D4 接线，§3.7） |
| ~~**P2 聚合器 + 配对双跑**~~ | ~~`chain/aggregator.py`、`exp_pair`、红线 R2 守卫~~ | **✅ 已完工**（§3.8；β 仍为空，待配对样本） |
| ~~**P3 条件校准与层级池化**~~ | ~~`exp_cond_calib`、τ 季度重估、<30 簇只出先验~~ | **✅ 已完工**（§3.8；待样本进 β 拟合） |
| **证据生成接线（C3P 第②段）** | LLM 生成 `discriminators`/`falsifiers` 的真实调用点 —— **当前尚无**，故 evidence_class 恒 weak | 采集稳定后 |
| **EV-1 / 配对 / 校准 入表** | 现在都是 JSONL；契约稳定后决定是否 `exp_*` 建表 | 样本量达标后 |
| **qd_agent_messages 落盘验证** | 确认 `postgres_memory.py:26` 是否真在写（§19.8 最小准备第 1 项） | 可与 P1 并行，只读 |

---

## 5. 验收总表（本批次做完的样子）

| # | 断言 | 出处 |
|---|---|---|
| 1 | `resolver` 有 `delisted` 分支，且正常活跃票不被误判 | B1 |
| 2 | `_VERDICT_SCORE["delisted"] == 0.0` | B2 |
| 3 | 同一历史日期两次 `_get_actual_return` 返回值完全一致 | B3 |
| 4 | `feed(dry_run=True)` 出现 `sample_count > 0` 的 domain | B4 |
| 5 | ~~`condition_key` 默认 `bar_freq == "1d"`~~ → **已移出本批**（见 §3 C1 修正，并入 P1-1） | ~~C1~~ P1-1 |
| 6 | 首轮注入块压缩后仍存活（守卫） | C2 ✅ 4 passed |
| 7 | `_QD_COMPACT_NOTES` 含第 6 条保留要求（**真指纹**，非 `[参考案例]`） | C3 ✅ |
| 8 | `qd_smoke` **不低于 A1 基线** | 每阶段 |
| 9 | **一 claim 一票**：同 claim 两条留痕（旧 undecidable + 新 hit）聚合后 `n` 不虚涨（G24） | B6 |
| 10 | 历史重算**幂等**：重复跑 `recompute_resolutions.py` ⇒ 0 条变化，且 `finance/n` 恒为 7 | B7 |
| 11 | 压缩守卫**有 bite**：5 种故意破坏全部被抓到（变异检验，非假绿） | C2 |
| 12 | 注入块指纹与 `case_memory` 真实产出一致（防守了个不存在的字符串） | C2 |
| 13 | 双窗口：一次 `get_kline` 同时给出 T+1/T+3（真源实跑每票**仅 1 次**取数） | D3 ✅ |
| 14 | `verdict` 只取主窗口 T+3；T+1 只进 `actual.windows`，**不进权重**（G24 不受影响） | D3 ✅ |
| 15 | 双窗口守卫**有 bite**：9 种故意破坏全部被抓到（变异检验，非假绿） | D3 ✅ |
| 16 | 标签口径**单一真源**：`chain/*.py` 内除 `label_spec.py` 外无第四份 `"T+1": 1` 字面量 | P1-1 ✅ |
| 17 | `prompt_sha` 不再恒 missing：改为扫描目录，删/加 prompt 自动跟随 | P1-4 ✅ |
| 18 | 盘后采集**真在跑**：worker 源码含 `run_v5_collection` 调用，同日重跑幂等 | D4 ✅ |
| 19 | EV-1 **不伪造**：无 discriminators/falsifiers 时留 `[]` 且标 `weak`，不填占位符 | P1-2 ✅ |
| 20 | 注入块升级后**指纹不变**（仍以 `【历史相似案例——仅作参考，不要照搬结构】` 开头） | P1-3 ✅ |
| 21 | 聚合器 R2 守卫生效：源码（剥 docstring 后）不含 `qd_agent_weights`/`exp_*` | P2 ✅ |
| 22 | 无系数时 `p_final == p_base`（Δ=0）为**正确行为**，fail-open 退回 `p_base` 而非 0.5 | P2 ✅ |
| 23 | 校准**样本不足不出 β₀**；池化**如实标注来源层**与 `pooled_n_clusters` | P3 ✅ |
| 24 | 阶段 F 守卫**有 bite**：9 种故意破坏全部被抓到，变异后源码全部还原 | F ✅ |
| 25 | 标签口径**只有一张表**：`label_spec` 上不存在 `CLAIM_HOLDS`/`TRACE_HOLDS` 等旧名 | G ✅ |
| 26 | 长周期自洽：`1M×12 == 1Y` 且 `3M×4 == 1Y` ⇒ 22/66 这类值进不来 | G ✅ |
| 27 | 反查表**先到先得**：`hold_label(5)` 恒为 `T+5`，不被后声明的 `1W` 抢走 | G ✅ |
| 28 | 口径统一守卫**有 bite**：8 种故意破坏全部被抓到，变异后源码全部还原 | G ✅ |
| 29 | `qd_smoke --with-env` **0 failed**（2 条为归档缺件 skip，非回归） | G ✅ |

> **未完成第 4 项，就不要建 `exp_*` 表** —— v5 §14 的原则：
> 标签不可靠时建表，只会得到一张漂亮的空表。

---

---

## 6. 遗留与待办（用户回来后裁决 / 复验）

| # | 事项 | 性质 | 建议 |
|---|---|---|---|
| 1 | `qd_smoke` 默认模式 248/250 | **不是 bug，是跑法**（2026-10-13 已查证） | 那 2 条是归档件缺件断言，`check(..., env=...)` 的 skip 只在 `--with-env` 下生效（`qd_smoke.py:343`）。带 `--with-env` 跑实测：**248 passed / 0 failed / 2 skipped，退出码 0**。⇒ **基线应记为 248 pass + 2 skip，不是 2 failed**；不要为了"变绿"去改判据 |
| 1b | ⚠ 上条**暴露的真问题**：`del/tools_returns_contract_20261010` 与 `del/agent_formatters_20261001` **磁盘上都不存在** | 信息失真 | 代码与文档多处写着"已归档、**可回滚**，不是硬删"，但归档件已丢失 ⇒ **"可回滚"这个承诺当前是失效的**。活路径 `.py` 源文件干净（无回流），所以防回流意图仍满足；若需真回滚，要从版本库取回（agent 全程禁 git，需用户手工） |
| 2 | `get_sector_stocks` 依赖 `push2.eastmoney.com` **凌晨稳定失败** | 环境依赖（代理），2026-10-13 05:40 复测**仍失败** | 报错为 `ProxyError('Unable to connect to proxy')` —— **是代理不通，不是接口本身**。代码已有「push2 触发熔断 → 走 sina 兜底」机制，但 `sector-detail` 这条路径**未接入兜底** ⇒ fail-open 返回空 data。**需交易时段复验**（一键脚本：`tmp/probe_eastmoney.py`） |
| 3 | `qd_agent_messages` 是否真在写（`postgres_memory.py:26`） | 只读核查，§19.8 最小准备第 1 项 | 与本批并行可做，不影响后续 |
| 4 | `label_spec` 与旧链路 `_TIMEFRAME_DAYS` 的最终收口 | 待决 | 现为**两组口径故意不合并**（1M=20 交易日 vs 22 自然月）；P1-1 正式立项时决定谁迁就谁，未定前**不要顺手统一** |

---

> **计划变更纪律**：实施中若发现计划与实际不符，**回头改本计划**，不要悄悄改代码。

---

## 3.10-bis 阶段 H **完成状态**（2026-10-13 晚，P2 接线已落地）

> 计划变更纪律：本段是对 §3.10 的**增量补记**，不改原文。

### 已交付

| 文件 | 说明 |
|---|---|
| `backend_api_python/app/agent/chain/c3p.py`（新建） | C3P 编排：`run_for_decision` / `p_base_of` / `extract_evidence` / `resolve_subject` / `current_frame` |
| `backend_api_python/app/agent/chain/kline_feed.py`（新建） | K 线统一取数（收敛第 3 处重复）；同时给 `t`（日期串）与 `time`（int 时间戳） |
| `app/agent/chain/intake.py`（改） | `record_decision` 返回值**新增** `claims`（纯增量，供 C3P 取 `subject`） |
| `app/agent/qd_agent.py`（改） | 新增 `_c3p_pipeline`，挂在 `_finish_collector` 中、**夹在 intake 与 record_case 之间**；`_record_case` 传 `evidence=` |
| `app/agent/scripts/qd_smoke.py`（改） | 新增 `test_40_c3p_wiring_and_r2_guard`（6 条断言） |
| `backend_api_python/tests/test_v5_c3p.py`（新建） | 18 条 TDD 用例 |

### H.9 / H.10 / H.11 实测结果

- **TDD**：先红（模块不存在）→ 后绿 **18 passed**
- **变异检验 7/7 全被抓到**（`tmp/mutation_check_c3p.py`）
  - M1 开关失效 · M2 p_base 兜底成 0.5 · M3 fail-open 改抛异常 · M4 条件键猜默认值 ·
    M5 无证伪条件也降级失败 · M6 evidence 不回传 · M7 基线真反向 import chain
  - ⚠️ **M6 一开始没被抓到** —— 那不是实现错，是**判据写脆了**：smoke ③ 原本只断言
    `"evidence=" in src`，而 `evidence=None` 也能匹配。已改成「实参必须是变量且方法里
    出现 `last_c3p`」。**这正是变异检验的价值：它抓的是守卫的洞，不是代码的洞。**
  - ⚠️ **M7 换了注入方式**：原打算"把守卫改成恒真"，但那**天生测不出来**（守卫自己变宽松，
    跑守卫当然还是绿的）。改成往 `app/watchlist/predict.py` 注入**真实违规 import**，
    看守卫抓不抓 —— 这才是在测 bite。注入包 try/finally 保证字节级还原。
- **回归（零回归）**：
  - v5 全量 **138 passed / 1 skipped**（基线 120 / 1，+18 = 新增用例）
  - 全量 `tests/` **529 passed / 4 skipped**（基线 511 / 4）
  - `qd_smoke --with-env` **254 passed / 0 failed / 2 skipped**（基线 248 / 0 / 2，+6 断言）
  - `compileall` rc=0；`app/watchlist/predict.py` 变异注入确认**无残留**

### ⚠️ 两条必须记住的施工结论

1. **接线后 Δp 恒为 0 是设计内预期**（`coefs=None` ⇒ `p_final == p_base`）。本阶段是**攒数据**，
   不是看效果。验收看「pair 在落 / condition_key 对 / evidence 有回传」，不看 Δp 非零。
2. **`chain.X` 与 `app.agent.chain.X` 不是同一模块对象**（老坑复现）。本批所有单测一律
   patch **`c3p` 模块对象上的属性**（`c3p._kf` / `c3p.p_base_of` / `c3p.extract_evidence` /
   `c3p.current_frame`），**没有**去 patch `app.agent.chain.evidence.*` —— 那样打不到且会误触真源网络。

### 遗留（本批**没做**，记此防遗忘）

1. **P3 校准未接线** —— `calibration.fit_with_pooling()` 仍零生产消费方 ⇒ 即使 pair 攒够也出不了 β。
   **这是下一个最高优先级**。
2. **`evaluator._get_actual_return` 未迁到 `kline_feed`** —— 它刚在 P0-3 修过且有守卫在守，
   本批动它风险大于收益。`kline_feed` 已同时给 `t` 与 `time` 两个键，迁移时零障碍。
3. `exp_pair` / `exp_cond_calib` / `exp_cond_registry` 三表仍未建（符合"先落 JSONL"决策，
   但 P3 的按格聚合需要 JOIN ⇒ 见 v3 §22 O-4）。

---

## 3.11 阶段 J：O-12 争议复核（结论：**不撤工具预选**，我方原判错）

> 起因：用户质疑 v3 §22 的 O-12「撤工具预选」，给出四条理由 —— ①预选 -72% 贡献大
> ②"一轮小 LLM 换更少 token"划算 ③需要别的工具可走 `list_tools` ④**tools 目录会有多域上百个
> 工具，对 token 是重大负担**。
> 复核结果：**四条全对**。原判「不推荐先做，需先做 A/B 测量」作废。

### J.1 我错在哪

- **A/B 早就跑过了**：`stock_screen` 153,960→43,318 tok（**-72%**）、
  `trade_confirm` 100,150→34,976 tok（**-65%**）。要求"先做 A/B 测量" = 重复劳动。
- **漏了最强论据**：第 ④ 条。工具面越大，预选越划算（见 J.2），这条直接把结论从
  "可能回吐"翻转成"单调增益"。

### J.2 实测成本模型（脚本 `app/agent/tmp/measure_preselect_cost.py`）

| 项 | 值 | 说明 |
|---|---|---|
| 当前工具面 | **58 个**（finance 41 / common 16 / knowledge 1） | |
| 单工具 JSON schema | **544 字符** | **每步**都随请求下发 |
| 预选目录单行 | **73.4 字符**（`max_desc=60`） | 只在预选那一轮出现**一次** |
| 比值 | **7.4×** | 目录是 schema 的压缩摘要 |
| 预选固定开销 | **504 字符** | system + prompt 骨架，**常数** |

**核心：schema 是"每步"，预选是"一次"。** 撤预选不是省一轮 LLM，是把「全量 schema × 步数」加回来。

| 工具数 N | 无预选 | 有预选 | 节省 |
|---|---|---|---|
| 58 | 189,342 | 83,912 | 56% |
| 100 | 326,452 | 86,997 | 73% |
| 200 | 652,903 | 94,342 | 86% |
| 300 | 979,355 | 101,687 | 90% |
| 500 | 1,632,259 | 116,377 | 93% |

（模型低估实测：实测 -72% vs 模型 56%，差额来自步数 6→2 —— 工具面窄，模型更少"摸索式"试错。）

★ **净收益随工具数单调递增** = 规模不变量。**"上百个 tools"是加强预选的理由，不是撤掉它的理由。**

### J.3 功能侧核实（用户第 ③ 条成立）

`list_tools` / `search_tools` / `activate_tools` 在 `tools/tool_discovery.py`，属 `common` 域
⇒ **必注入层，永远可达**；再加分层 fail-open（未激活直调就地激活）+ `fallback_domain_tools`，
共三条退路 ⇒ "撤掉功能上跑得通"成立。**但跑得通 ≠ 划算。**

### J.4 文档改动（v3 已改）

- **§22.1**：O-12 改为「**不做（明确保留）**」并划掉原描述；**新增 O-14「预选目录分级展开 +
  query 哈希缓存」**，推荐顺序 ⑫，**触发条件：工具面破 120 个**。
- **§22.3**：O-12 条目重写 —— 明写"初版要求先做 A/B 是判断错误"。
- **新增 §22.4**：O-12 成本模型（单位成本 + 五档增长曲线 + 功能侧核实 + O-14 解法）。

### J.5 O-14 技术依据（为什么便宜）

`build_catalog_grouped(defs, domain_names, ...)` 的**域过滤参数现成就支持分级**，只需串两步：
先 `has_domain_hint` 算命中域 → 再只对命中域建目录。N=500 时目录 36.7k 字符（占预选输入 99%）
⇒ 分级后压回 ~1k 量级。**现在不启动**（工具面 58，未到 120 阈值）。

### J.6 本批遗留

- **P3 校准接线仍是下一个最高优先级**（本批未做）。
- O-14 为条件触发项，现在不做。

---

## 3.12 阶段 K：agent 重写（新旧并存 + 吃透 mimo）

> 起因：用户判断「有这么多问题，应该把现有 `app/agent` 改名给新 agent 让路，新 agent 抄旧
> agent 作业，有 bug 的部分直接重写比找 bug 更容易；保持项目干净性。新版本目录仍是 `app/agent`」。
> 追加判断（用户）：**「换了 mimo 作为底层 agent 后，有一部分重复/残留内容在系统内，
> 和 mimo 耦合性并不好，没有把 mimoagent 吃透。」** ⇒ 分类从四层扩到**五层**，新增 L0。

### K.0 四项已拍板规格（2026-10-13）

| 决策 | 结论 |
|---|---|
| 旧代码安置 | `app/agent_legacy/`（仍在 app 包内、仍可 import，路径注入只改一个字符串） |
| 切换时机 | **现在就改名**，新 `app/agent` 从零长 |
| 抄写策略 | **分层**：复制无状态 / 引用有状态 / 重写主链 / 丢弃垃圾（不整体物理复制 `tools/`） |
| 首版范围 | **最小可运行骨架**（绝对导入 + 工具注册 + 单轮对话 + 一个技能，端到端跑通） |

### K.1 改名（已执行并验证 ✅）

机械步骤（全部可逆）：

1. `mv app/agent app/agent_legacy`（备份已先落 `del/agent_pre_rename_20261013/`，153 py / 15M）
2. `app/__init__.py` 唯一硬编码点 `_agent_dir = _os.path.join(_app_dir, "agent")` → `"agent_legacy"`
3. `tests/` 12 个文件的 `app.agent.*` → `app.agent_legacy.*`
4. 清 `__pycache__`

**验证（改名后当场跑）**：

```
sys.path 含 agent_legacy            : True
from flask_app import agent_v2_bp   : OK   ← /api/agent/* 全站路由命脉
import cron.cron_worker             : OK   ← 定时任务
from tools.finance import screener  : OK   ← 前端选股 API
from chain.evaluator import ...     : OK   ← 盘后评估 worker
from llm import create_llm          : OK   ← market_cn/auto/chat_why 依赖
from app.agent_legacy.chain import  : OK   ← 全名导入路径
```

回归基线对账：改名前 **529 passed / 4 skipped**。

**改名踩到的 3 条真实失败（已修，教训记下）**：我第一遍只扫了 `app.agent.*`
**点号导入**（12 个文件），漏了**路径字符串**形式：

| 失败 | 原因 |
|---|---|
| `test_skill_run_exits.py` | `ROOT / "app" / "agent" / "scripts" / "skill_run.py"` —— **Path 拼接**，点号 sed 扫不到 |
| `test_agent_attr_namespace.py` ×2 | `... / "app" / "agent" / "qd_agent.py"` —— 同上 |

⇒ **改名类改动必查三类引用**：点号导入、`Path(...)/"..."` 拼接、注释里的路径串。
修完后 **`533 passed / 4 skipped`**（529 + 新增 4 条边界守卫）、`compileall` rc=0。

**新增守卫** `tests/test_v6_legacy_boundary.py`（4 条，已做变异检验并确认有 bite）：

1. `app/agent_legacy/` 存在且 `app/agent/` 不得再现（防新旧同名共存）
2. `app/__init__.py::_agent_dir` 必须指向 `agent_legacy`（**后果最重的一处**：漏改会让
   6 条外部裸名导入集体断掉，而 `agent_blueprint.py` 在模块导入期就取 `agent_v2_bp`
   ⇒ 整个 Flask 起不来）
3. 六条外部裸名依赖仍可解析（`flask_app` / `cron` / `tools.finance` / `chain.evaluator` / `llm`）
4. 全名路径 `app.agent_legacy.*` 可达

变异检验：把 `_agent_dir` 改回 `"agent"` ⇒ 第 2 条当场红；已字节级还原。

> ★ 关键事实：`app/agent_legacy/__init__.py` 的 `_agent_dir` 由 `__file__` 算出 ⇒
> **改名零改动自动跟随**。全仓库唯一硬编码只有 `app/__init__.py` 那一处。

### K.2 mimo 边界核对 —— 哪些是「重复/残留」

`qd_agent.py` 头部自述：**「Agent 循环 / 工具调度 / 并行 / 上下文 / 容错 / 轨迹 → mimoagent 原版」**。
但核对发现多处**并行实现**，这就是「没吃透 mimo」的账单：

| 现有（app/agent_legacy） | mimoagent 原生 | 判定 |
|---|---|---|
| `tools/base.py` **ToolProvider**（扫描 + `func_to_openai_schema` + `get_schemas()`，58 工具） | `mimoagent.tools.registry.ToolRegistry.get_function_definitions()` | **两套工具注册并存**，靠 `fn_adapter` 桥接 |
| `tools/fn_adapter.py` **FnToolAdapter**（头部自述「tools/ 整层保留，函数体一行不动，只换外包装」） | `mimoagent.tools.base.BaseTool` | 当初为不动 13k 行加的**适配层**，不是 mimo 原生范式 |
| `llm/` **1369 行**（base/factory/openai_llm/dashscope_llm/qd_llm） | `mimoagent/models/`（anthropic / openai_chat / openai_responses） | **两套模型层** |
| `qd_agent` 自算 `_compaction_threshold()` / `compaction_context_window` | `mimoagent/compaction/`（surgery + prompt，`create_compact_boundary_message` / `build_summary_request_message`） | 注释已发现 mimo 默认 1_000_000 与自家窗口不符 ⇒ 我们是**绕着配**，不是接进去 |
| `trace_collector.py` 508 行 | mimo 轨迹 | 重复 |
| `agent.py` 375 行（旧入口，用 constants/llm/memory/qd_service） | — | **残留**，与 mimo 主链并存 |
| `tmp/mimo_orig/mimoagent/` | — | mimoagent **源码副本**躺在 tmp |
| `AGENTS.md` 仍写 `graph.py` / `nodes.py` 状态机 | — | 文件**已不存在**，文档残留 |

**mimo 没有、必须自己有的**（不在 L0）：`memory/`(672)、`capabilities/`(863)、`rag/`(1229)、
`resolvers/`(972)、`chain/`(C3P + 账本 + 评估 + 校准)、`tools/finance/*` 业务取数。

> ⚠ 部署隐患：`patches/mimoagent_windows.patch` 打的是 `.venv`（**不入版本控制**）⇒ 干净
> 环境必须显式跑 `patches/apply_mimoagent_patch.py`。新版应把这条写进**启动自检**。

### K.3 五层抄写清单

| 层 | 含义 | 内容 | 规模 |
|---|---|---|---|
| **L0** | mimo 已提供 → **删 / 不再抄** | `llm/` 自建 client、自算 compaction、`trace_collector.py`、`agent.py`、`tmp/mimo_orig/` | ~3.5k |
| **L1** | **物理复制**（mimo 没有、无状态） | `tools/finance/*` 纯取数、`resolvers/`、`capabilities/`、`memory/`、`rag/`、`log.py`、`constants.py` | ~15k |
| **L2** | **过渡期引用**（有状态，不复制） | `chain/store.py`、`utils/case_memory.py` 等 → `from app.agent_legacy.chain.store import ...`，新实现就绪后再切 | 小 |
| **L3** | **重写** | `qd_agent.py`（1486，改直接吃 mimo ToolRegistry）、`qd_service.py`(521)、`scripts/qd_smoke.py`(4133)、`chain/evaluator.py`、`tools/tool_preselect.py` | ~7k |
| **L4** | **不抄** | `tmp/`(3163)、`traces/`、`data/`、`llm_inputs.log`(657KB)、`cli.py`(310) | ~3.5k |

**L0 的落地姿态（关键）**：`ToolProvider` **降级为「发现器」**（mimo 没有「公开函数自动注册」这条），
`mimo ToolRegistry` 升为**唯一注册表** —— 只保留**一个通用适配器**，不是 58 个。
这样既不重写 13k 行工具，又消除双注册。

### K.4 新 `app/agent/` 最小骨架

```
app/agent/
  __init__.py    # 只有 __version__；★ 绝不做 sys.path 注入
  config.py      # env 驱动，零硬编码
  registry.py    # 唯一工具注册表 = mimo ToolRegistry + 公开函数发现器
  runtime.py     # 单轮执行（直接用 mimoagent.agents）
  serve.py       # Flask blueprint
```

挂载方式：`app/routes/__init__.py` 增一行 `app.register_blueprint(agent2_bp, url_prefix='/api/agent2')`。
旧 `/api/agent/*` **一个字不动** ⇒ 新旧并行可随时对照、可灰度。

### K.5 红线守卫（TDD 先红后绿）—— `tests/test_v6_isolation.py`

1. `app/agent/**` 源码**零** `sys.path.insert` / `sys.path.append`
2. `app/agent/**` 源码**零裸名导入**（`from tools.` / `from chain.` / `from utils.` / `from llm.` / `import flask_app`）
3. `app/agent/**` 源码**零 `agent_legacy` 引用**（L2 过渡白名单须**显式登记在测试里**，逐条有名有姓）
4. `app/agent_legacy` 仍可 import + `flask_app.agent_v2_bp` 仍可取（防改名回归）
5. 新蓝图 `/api/agent2/health` 可达

> 第 1–3 条是**新版不许退化成旧版**的唯一硬保障，必须有变异检验（注入违规 import，看守卫红不红）。

### K.6 执行顺序

K.1 改名 ✅ → K.5 守卫先落地（红）→ K.4 骨架（绿）→ K.3 L1 复制 → K.3 L3 重写 →
L2 逐条切断 → 最后 `/api/agent` 流量归零才谈旧链路退役。

### K.7 回滚

- 全量备份：`del/agent_pre_rename_20261013/`（153 py / 15M）
- 回滚命令：`mv app/agent_legacy app/agent` + `app/__init__.py` 改回 `"agent"` + tests 反向 sed

来源：阶段 K（agent 重写 · 新旧并存 · mimo 边界核对），2026-10-13

---

## §3.13 阶段 L：新版 agent 骨架落地（已完工）

### L.0 动工前先探清的 mimo 边界（硬事实，不是推断）

| 事实 | 影响 |
|---|---|
| `ToolRegistry.from_config` 只认内置 6 类（bash/read/write/edit/agent/bash-only） | 自定义工具**没有**配置化入口 ⇒ 必须覆盖 `_build_tool_registry()` |
| `_build_tool_registry()` 在 `DefaultAgent.__init__` **内部**调用 | `self._catalog` 必须先于 `super().__init__()` 赋值 |
| `render_template` 每轮调 `env.get_template_vars()` | env 不能是 None |
| `LocalEnvironment` 用 `subprocess.run(shell=True)`（Windows 恒走 cmd.exe） | 新版自写 `NullEnvironment`，不给金融 agent shell |
| tool_call 是 OpenAI 原生格式 `{"id":..,"function":{"name":..,"arguments":{..}}}` | 写成内部 action 格式会静默 FormatError |
| 旧版生产主链已在用 `mimoagent.models.get_model` | `llm/` 1369 行确属残留 |

### L.1 骨架成品

```
app/agent/  __init__.py / config.py / runtime.py / serve.py / README.md
            tools/{__init__.py, base.py, catalog.py}
```

- **白名单制**（对旧版"全家桶+叠加"的反面）：注册表只装 `default_catalog()` 声明的工具
- **唯一适配器** `CallableTool`：任意函数 → mimo BaseTool
- 首版技能 = 统计功效计算器（v3 §10 地基，纯函数不联网，与实测表逐位对账）
- 挂载 `/api/agent2`，旧 `/api/agent` 一字未动

### L.2 红线守卫（7 条，`tests/test_v6_isolation.py`）

R1 零 sys.path 注入（源码）｜R1b 运行时 sys.path 不含新版目录｜
**R1c 测试侧不得把新版目录塞进 sys.path**（2026-10-13 真事故产物）｜
R2 零裸名导入（AST 判据）｜R3 零旧版引用（白名单需逐条登记）｜
R3b 首版白名单必须为空｜旧链路仍可 import

### L.3 执行顺序（照此推进）

L.0 mimo 边界核对 ✅ → L.1 红线守卫（红）✅ → L.2 骨架（绿）✅ →
变异检验 11/11 ✅ → 挂载 /api/agent2 ✅ →
**下一步**：L1 复制无状态取数层 → L2 过渡引用有状态模块 → L3 主链重写 →
L2 逐条切断 → /api/agent 流量归零才谈旧链路退役

来源：阶段 L（新版 agent 骨架落地），2026-10-13

---

## §3.14 阶段 M：L1 格式化层 + L3 行情薄封装（已完工）

### ★ L1 范围推翻（本阶段最重要的结论）

原判"复制旧版无状态取数层 ~15k 行"**不成立**。`tools/finance/*`（8228 行）
的依赖去向：`app.*` **65 处**、`tools.*` 26 处、`utils.*`/`log` 26 处
⇒ 取数实现不在旧 agent 里，那些只是 app 包之上的业务封装。

修订后的五层清单：

| 层 | 内容 | 规模 | 做法 |
|---|---|---|---|
| L0 | mimo 已提供（模型层/注册表/循环） | — | 删 |
| **L1** | **真正可复制的旧版资产** | **168 行** | **物理复制**（`md_format.py`） |
| L2 | 有状态模块 | — | 过渡引用（白名单逐条登记） |
| **L3** | **业务薄封装** | 每个 30–60 行 | **重写**（直接调 app 包） |
| L4 | tmp/traces/data | — | 丢弃 |

⇒ L1 从"~15k"缩到"168 行"，且**完全避免双份维护**。

### 新增

- `app/agent/tools/formatting.py`（L1 复制）
- `app/agent/tools/market.py`（L3 重写）：`resolve_stock` / `get_daily_bars` / `get_realtime_quote`
- 工具面扩到 6 个，声明处仍是 `catalog.default_catalog()`

### 验证

22 条新测试（全 monkeypatch，不联网）｜变异检验 **18/18**（新增 M12–M18）｜
全量 **580 passed / 4 skipped**｜真实取数冒烟通过（不打桩）

### 下一步（照此推进）

扩工具面（选股 / 技术指标 / 板块题材 / 信号，均为薄封装）→ 会话持久化 →
C3P 条件化预测引擎 → P3 校准接线

来源：阶段 M（L1 格式化层 + L3 行情薄封装），2026-10-13

---

## 3.13 阶段 N：工具面扩展（6 → 10）+ 预取机制延后

### N.0 用户中途改向
预取/工具裁剪问题（能否把必选工具引到 mimo 核心工具层绕开上下文）
**推到最后优化阶段统一考虑**。已写的 `tests/test_v6_prefetch.py`（19 条）
延后归档 `del/v6_prefetch_deferred_20261013/`，不删。

### N.1 已探明的事实（优化阶段直接取用，不必重查）
- 工具 schema **不在 messages 里**，走 API 原生 `tools=` 参数
  （`DefaultAgent.get_model_query_kwargs` → `model.query(..., tools=...)`），
  每轮全量重发且计费 ⇒ "换通道注入"省不掉这笔钱。
- **不需要打 patch**：`get_model_query_kwargs()` 每轮调用、可覆盖（动态裁剪的
  正规 hook）；另有 `add_action_interceptor()` 可在工具执行前短路。
- 真杠杆是**让工具不进工具面**：① 预取（系统侧先做掉，省一整轮往返）
  ② 裁剪（命中后从 `tools=` 剔除）。多轮对话里重发上下文的成本远大于 schema。
- 股票名匹配不能用 `search_stocks` 查整句 —— 实测是精确/前缀匹配，
  "浦发银行怎么样" → 0 条；须用全量名称反向子串匹配 + **最长优先**消歧。

### N.2 已完成
| 项 | 内容 |
|---|---|
| 新增模块 | `tools/analysis.py`（`get_technical_indicators` / `get_index_bars`）、`tools/taxonomy.py`（`get_stock_profile` / `list_concept_stocks`） |
| 新增测试 | `tests/test_v6_extended_tools.py` 21 条（全 monkeypatch，不依赖网络） |
| 工具面 | 6 个 2956 字符 → 10 个 **4912 字符**（预算 7000，已加守卫 `test_toolface_schema_stays_within_budget`） |
| 回归 | **601 passed / 4 skipped**（580 + 21），compileall rc=0 |
| 变异检验 | **26/26 全抓到**（新增 M19–M26），还原零残留 |
| 真实冒烟 | 600000 指标 118 根、000001 指数 10 根、画像与概念均有数据 |

### N.3 设计决策
1. **指标算法一行不写**：直接调 `app/utils/indicators.py`（app 包内 347 行）。
   口径必须与回测/策略侧同源；旧版 `analysis_tools.py` 自带 `_calc_macd` 是反面例子。
2. **只回最新一根**：`calc_macd`/`kdj` 实测返回全序列 tuple（60 根 = 180 个数）。
   截断用 `float(f"{v:.6g}")` 而非 `round(v, 4)` —— 后者会把 0.0006 量级的
   macd_hist 压变形，导致同源对账断言假红。
3. **工具按取数源分模块**（market / analysis / taxonomy），不按业务分类：
   源不同 ⇒ 失效模式不同（网络抖动 vs 资料陈旧），混在一起难归因。

### N.4 搬运判据 A/B 类（已写入 app/agent/README.md）
工具是 OpenAI 标准 schema（`{"type":"function","function":{...}}`），
mimo `ToolRegistry` 直出即此格式 ⇒ 复制**无协议障碍**，唯一机械代价是
裸名 import → 绝对 import。决定"复制还是薄封装"的是**算法住在哪**：

- **A 类**（`app.*` 导入占多数 ⇒ 逻辑在 app 包）→ **不复制**，写薄封装。
  复制进来会让 app 包那份继续演进 ⇒ 两份实现、两种口径。
  实例：`data_tools`(app 16)、`indicator_tools`(9)、`sector_analysis_tools`(6)、
  `signal_tools`(5)、`fund_flow_tools`(5)、`index_tools`(4)。
- **B 类**（几乎零 `app.*`，靠自身包 + 标准库 ⇒ 算法只存在于 legacy）→ **复制**，
  改 import 即可，不重写。不复制就是丢能力。
  实例：`analysis_tools`(app 0)、`bull_bear_research`(app 0)、
  `technical_analysis`(app 1)、`chip_distribution`(app 1)、
  `screener_config`、`em_utils`、`domain_meta`。

### 下一步（照此推进）
1. **B 类搬运**（真正能扩能力：筹码分布、选股配置、bull_bear_research、
   technical_analysis）—— 复制 + 改 import，不重写
2. 会话持久化（多进程部署时进程内字典失效）
3. v3 下编 C3P 条件化预测引擎接入
4. P3 校准接线：`calibration.fit_with_pooling()` 仍零生产消费方
5.（优化阶段）预取层 + `get_model_query_kwargs()` 动态裁剪

---

## §3.14 阶段 O：B 类搬运（清单修正版）

> **本节是对 §3.13 末尾「下一步 1」的修正，不是覆盖。**
> 动手前做了依赖逐文件核算，原 B 类清单 7 个里 **3 个被事实剔除**。

### O.1 清单修正（三条，均有源码证据）

| 原判 B 类 | 复核结论 | 证据 |
|---|---|---|
| `chip_distribution`(247) | ❌ **实为 A 类，不搬** | `app/services/chip_service.py` 头注释明写「2026-09-23 合并…本文件 `compute_chip_core()` = **唯一数学内核**；agent 侧那份**改为调用同一内核**」。app 包已有对应物，复制即制造第三份。 |
| `bull_bear_research`(722) | ❌ **不搬** | 依赖 `chain.store.get_factor_weights` + `utils.calibration.apply`（legacy 反馈信号）+ `news_search_tools`/`data_tools`（A 类）。搬它 = 把 legacy 反馈回路拖进新版，破隔离红线 R2/R3。 |
| `technical_analysis`(397) | ❌ **不搬** | 同上，同样依赖 `chain.store.get_factor_weights` + `utils.calibration.apply`。全 finance 目录仅这两个文件碰反馈信号（已 grep 确认）。 |

### O.2 真正的 B 类 = 「app 包差集」

判据从「app.* 导入数 = 0」收紧为 **「app 包有没有等价实现」**：

| analysis_tools 自有实现 | app 包 | 处置 |
|---|---|---|
| `_calc_macd` / `_calc_rsi` / `_calc_boll` / `_calc_kdj` | ✅ 有（`app/utils/indicators.py`） | **不复制**，改调 app 包（阶段 N 已接） |
| `_calc_atr`(Wilder 平滑 + 波动率等级) | ❌ 无（app 包 atr 是序列/pct 形态，不含研判） | **复制** |
| `_calc_mfi` | ❌ 无 | **复制** |
| `_calc_cmf` | ❌ 无 | **复制** |
| `_calc_hist_volatility`(对数收益年化) | ❌ 无 | **复制** |
| `_calc_ma_convergence`(均线粘合) | ❌ 无 | **复制** |
| `_calc_obv`(`_analysis_utils`) | ❌ 无（app 包 `kline_derived._calc_obv` 是 C 类实时现算，签名不同） | **复制** |
| `_detect_divergence` | ❌ 无 | **复制** |
| `_safe_round`(NaN/Inf 安全) | ❌ 无 | **复制** |

合计 **8 个纯函数**，全部零 legacy 依赖、零 app 包依赖（只用 math）。

### O.3 落地形态（不新增 8 个工具）

工具面已 10 个 / 4912 字符（预算 7000）。**指标是「观测值」不是「工具」** ⇒
不逐个暴露，而是：
1. 新建 `app/agent/tools/indicators.py` —— 8 个纯算法，零依赖
2. `analysis._SPECS` 签名 `(c,h,l,ind)` → `(c,h,l,v,ind)`（补 volumes，MFI/CMF/OBV 需要）
   新增指标名：`atr14` `atr_pct` `mfi14` `cmf20` `obv` `hist_vol20` `ma_converged`
3. 新增**一个**工具 `get_volume_price_analysis` —— 承接 OBV/MFI/CMF 的中文研判
   （signals / divergence），因为研判是字符串、塞不进 `{名: float}` 的 data 表

`MAX_INDICATOR_DAYS=500` 不变；`MIN_INDICATOR_BARS=35` 不变（新指标最长需要 60 根
的只有 ma_convergence，算不出时按 O.4 处理）。

### O.4 语义约定（搬运时必须对齐）

- 数据不足 ⇒ 返回 `{"error": ...}`（legacy 口径），**不是**抛异常
- `ma_convergence` 不足 60 根 ⇒ legacy 返回 `{"converged": False}`，**保持**（不是 error）
- 数值一律过 `_safe_round`（NaN/Inf → 0.0），再过 `_sig`（6 位有效数字）
- 研判文本（signals/vol_trend/divergence）保持中文原样，不做翻译/截断

### O.5 TDD 与验证

- 测试：新增 `tests/test_v6_indicators.py`（纯算法，不打桩，构造合成序列）
- 对接测试并入 `tests/test_v6_extended_tools.py`
- 变异检验：每个新函数至少一个变异（≥8 个），必须 100% 被抓
- 全量回归基线：**601 passed / 4 skipped**

### O.6 结果

- 新增 `app/agent/tools/indicators.py`（8 个纯函数，零 import）
- `analysis._SPECS` 加 volumes 参数；工具面 10 → 11
- 测试 44 条（`tests/test_v6_indicators.py`），**变异 39/39**
- 全量 **645 passed / 4 skipped**（基线 601 + 44），compileall rc=0
- 变异曾漏网 3 条（M29/M34/M38）均为"断言取值远离阈值"导致恒真，
  补卡在两档之间的边界断言后全抓到

---

## §3.15 阶段 P：转核心层（用户指示优先级高于外围）

> **用户原话**：「tools 层不应该再做仔细审核，接口功能都是正常的，
> 即使有问题也可以后续再修改，不会影响框架，3 层工具优先级这个框架也没问题。
> 现在最大的问题是核心层代码混乱。不要把过多的时间花在外围上。」
>
> ⇒ 后续**停止扩外围**，集中处理 `app/agent_legacy` 核心层。

### P.1 核心层混乱的初步定性（已探查，待深化）

| 文件 | 规模 | 混乱形态 |
|---|---|---|
| `qd_agent.py` | 1486 行 / 41 方法 | 上帝类，横跨 7 类职责（工具注册 / 预选 / 守卫 / 执行循环 / 预算账本 / 收尾编排 / 轨迹）。单 `_maybe_preselect_tools` 一个方法 148 行 |
| `chain/store.py` | 1388 行 | 一个文件塞 6 种实体的持久化：评估树 / 技能验证 / 惩罚计数 / 四套权重 / 工具缓存 / 酿造状态 / 技能修订 |
| 四套权重 | — | `get_chain_weights` **零消费方（死代码）**；`get_factor_weights` 仅 bull_bear_research 用且注释明写"暂返回空 → 走回退分支"（半死） |

### P.2 下一步（动手前先出方案）

1. 深挖 `qd_agent.py` 的职责切片，给出**可独立拆分的边界**（哪些能先切走）
2. 核清四套权重的真实数据流（谁写、谁读、是否闭环）
3. 出核心层重组方案，**确认后再动手**

来源：阶段 O（B 类搬运 · 清单修正）+ 阶段 P（转核心层），2026-10-13
