# P0-A 粗筛改进实施步骤（H1 retry + objective + 校准）

> 用途：给实施侧的逐步执行文档。每步含【目的】【操作】【产物】【判定】。
> 前置状态：P0-A 已达标（尾部二分类 recall@200=0.2317，lift 3.91×），`infer_daily.py` 每日 top200 已落地。
> 本文只动训练/实验侧，**不改任何 app/agent 生产代码**；判定标准沿用 H1（配对区块 bootstrap CI 下界 >0 才接）。

---

## 0. 总览：改什么、为什么改

| # | 改进项 | 目的（为什么值得做） | 成本 | 判定 |
|---|---|---|---|---|
| 1 | 强信号源特征（H1 retry） | H1 实验结论：OHLCV 派生特征不显著，**要换强信号源**。涨停基因/题材热度是尾部事件的直接驱动，此前模型完全没有这些信息 | 中（需建数据） | CI 下界>0 |
| 2 | objective 微调（focal / 正样本加权） | I4 已证"训练目标对齐尾部"是胜负手（回归→二分类翻盘）。正样本仅 3~5%，focal 聚焦难样本是该场景的标准续招 | 低（几行） | CI 下界>0 |
| 3 | 概率校准（Beta/Venn-Abers） | p_base 要进经验库 Δp 链（§3.4 [c]），概率不校准则"经验让概率动了多少"的归因失真 | 低 | ECE/Brier 改善 |
| 4 | 工程收尾（meta 溯源等） | 溯源完整性，防止复评对不上号 | 低 | — |

**总纪律**：每项独立实验、独立判定、留痕（报告进 `tmp/`，口径改动同步设计文档 §6.2/附录）；调参不做（过拟合风险区）；不显著就不接。

---

## 1. 强信号源特征（H1 retry）

### 1.1 目的

H1 用 5 个 OHLCV 派生特征（GAP_OPEN/DIST_LIMIT/AMPLITUDE/LIMIT_HIT/ZT_STREAK）只换来 +0.0012（CI 跨 0），决议"换更强信号源（ST/涨停池历史）而非继续堆 OHLCV 衍生特征"。本项就是执行该决议：把**涨停事件历史 + 题材卡位**接进尾部二分类。

### 1.2 步骤一：建数据（ticai-daban，一次性 + 日常增量）

```bash
git clone https://github.com/SixCake/ticai-daban.git
cd ticai-daban && pip install -r requirements.txt
# 配 tushare token（.env 或环境变量 TUSHARE_TOKEN），然后：
python collect/fetch_limit_events.py   # 涨停事件库 2019-11→今（核心数据）
python collect/fetch_daily_panel.py    # 全 A 日线面板（特征网格基底）
python collect/fetch_kpl_events.py     # 开盘啦涨停/炸板（题材直标，2024-01→）
python collect/fetch_ths_limit.py      # 同花顺涨停池（2023-11→）
python build/enrich_events.py          # 事件富化：一字板判定 + T+1 四口径收益
python build/attribute.py              # 涨停事件×题材独占归属
python build/theme_daily.py            # 题材日度快照
python collect/factor_longtou.py       # 龙头因子 qscore/sscore
python datastore.py list               # 验收：数据集清单齐全
```

【产物】ticai-daban 的 `data/` 五张表：`limitup.events_enriched`、`theme.day`、`theme.attribution`、`factor.longtou`、`market.daily_panel`。
【耗时】全量 10-20 分钟，支持增量续跑。

### 1.3 步骤二：抽特征（口径唯一真源，逐列公式）

特征粒度：**(trade_date, ts_code)**，全部只用 t 日收盘及以前信息（盘后出信号、预测 t+1）。
实现可用我交付的 `p0a_tail_features/extract_tail_features.py`（已冒烟验证）；跑不起来就照下表本地实现，口径以本表为准：

**A 档：事件基因（数据覆盖 2019-11→，全训练窗可比，先做这档）**

源：`limitup.events_enriched`，取 `tag='涨停'` 的事件（下称"事件"）。

| 特征 | 公式（交易日窗，含 t 日） | 说明 |
|---|---|---|
| zt_cnt_20 / zt_cnt_60 | (t-19..t) / (t-59..t) 内事件数 | 涨停基因主项 |
| yizi_cnt_20 | 同窗内 is_yizi 事件数 | 一字板记忆 |
| open_times_mean_20 | 同窗内**事件日** open_times 均值（只对事件日求均值，不摊薄到全窗） | 炸板代理 |
| limit_times_last | 最近一次事件的 limit_times，ffill；从未涨停=0 | 最近连板高度 |
| days_since_zt | t 距最近事件的交易日数；从未涨停=999 | 新鲜度 |
| first_min_mean_20 | 窗内事件日 first_min（首封分钟，9:25=0）均值 | 封板早晚 |
| fd_ratio_20 | 窗内事件日 fd_amount/amount 均值 | 封单强度 |
| t1_close_ret_mean_20 | 窗内事件的 next_close_ret **先 shift 1 个交易日**再滚动均值 | T+1 溢价记忆 |
| t1_close_ret_last / t1_open_ret_last | 同上 shift 后 ffill | 最近一次接力盈亏 |

⚠️ **无前视红线**：next_close_ret 是事件**次日**才成熟的收益——特征日 t 只能用 trade_date ≤ t-1 的事件收益。实现为事件收益序列 shift(1 个交易日) 后再滚动；去掉 shift = 泄漏。

**B 档：题材卡位（覆盖 2023-11→，只在后 2 个 walk-forward 窗单独评，不与 A 档混窗）**

| 特征 | 公式 |
|---|---|
| 股票→题材 | 该票**最近一次涨停事件的独占归属概念**（`theme.attribution` 逐日记录，ffill，PIT 干净） |
| theme_zt_cnt | 归属题材当日独占涨停家数（`theme.day.zt_cnt`） |
| theme_rank_pct | theme_zt_cnt 在当日全部题材的分位（0~1） |
| theme_max_height / theme_age | 题材内最高连板 / 题材连续活跃天数 |
| theme_is_leader | 该票是否题材当日龙头（theme.day.leader_code == 票） |

**C 档：龙头因子**：`factor.longtou` 的 qscore/sscore 原样 join（注意：其源码自注"涨跌停价未做 ST 修正"，对 ST 票有偏）。

### 1.4 步骤三：重训与判定

1. A 档特征列左连接进 `alpha158_db` 特征矩阵（键 trade_date × ts_code），**同窗口/同标签/同超参**重训尾部二分类；
2. 与基线（现版 0.2317）做配对区块 bootstrap（按交易日块），报 recall@200 diff 与 top-decile lift 的 95%CI；
3. 分年度 2024/2025/2026 单独报（防总体均值掩盖失效年）；
4. **判定**：两主指标 CI 下界均 >0 → 接入；否则不接，记录证伪（H1 惯例）；
5. B 档在后 2 窗重复 1-4；A+B 同时接入与否按各自判定独立决策。

【留痕】报告 `tmp/p0a_report_tailfeat.md`；特征列清单以本文 §1.3 为准，改动同步设计文档。

---

## 2. objective 微调：focal loss / 正样本加权

### 2.1 目的

I4 的核心发现：训练目标必须对齐尾部（回归→二分类翻盘）。但普通 logloss 仍把算力花在 95% 的负样本上；正样本（尾部事件）只占 3~5%。focal loss 让模型聚焦"难分样本"（预测错的尾部事件），是该失衡场景的标准续招，也是 I4 的自然延伸。

### 2.2 步骤

1. 保持特征/窗口/标签**完全不变**，只换 objective，三组对照：
   - 基线：logloss（现版）
   - 组 1：`scale_pos_weight` 加权（如 5/10/20 三档，一轮扫完留痕，不做多轮调参）
   - 组 2：focal loss（LightGBM 自定义 objective，γ=2.0 标准值起步）
2. 评测与判定同 §1.4（配对区块 bootstrap CI）；
3. 若 focal 实现引入第三方代码，注明来源（找轮子优先：github "lightgbm focal loss" 有现成实现）。

【判定】CI 下界 >0 保留，否则回退 logloss。**注意**：概率输出换了 objective 后需要重新校准（见 §3），两步分开验证。

---

## 3. 概率校准（[c] 提示注入 / Δp 链的前置）

### 3.1 目的

设计 §3.4 [c]：p_base 要作为冻结基线进经验库 Δp 评估（"经验让概率动了多少、方向对不对"）。LightGBM 原始输出对尾部事件通常失准——不校准的话，Δp 归因会把校准误差算到"经验"头上，整个经验库学习回路失真。P0-C（KiT 尾部概率校准）届时复用本节方法。

### 3.2 步骤

1. 取 walk-forward 各验证窗的 out-of-fold 预测，对比四种后校准方法：
   - **Beta calibration / Venn-Abers**（推荐）：2026-01 实证研究（arXiv:2601.19944）显示对强 tabular 模型 isotonic/Platt **可能反而损害校准**，Beta/Venn-Abers 更稳；
   - isotonic / Platt（对照，保留给数据说话）；
2. 工具：`incerto`（github steverab/incerto，MIT，方法全家桶）或 sklearn 手写；
3. 指标：ECE / Brier / 可靠性图；**攻击池切片（连板/题材股）单独报**（设计 P0-C 口径）；
4. 通过后在 `tail_scores` 工具的 meta 里加 `calib_ver` 字段（校准版本与 model_ver 一起溯源）。

【判定】校准后 ECE/Brier 改善且攻击池切片不失准；否则不接，Δp 链用原始分数 + 在归因中披露未校准。

---

## 4. 工程收尾（顺手做，无判定）

1. **meta 溯源补洞**：`model_ver = tag:data_last:best_iteration` 之外，把**前复权因子快照 hash** 纳入溯源（否则新除权日静默重算历史 bar，复评对不上号——设计方案 I6 ④）；
2. **[a] 分数落库 + cron**：分钟级→ms 级查询（I6 ①），表口径 `instrument, trade_date, p_tail, rank, model_ver, calib_ver, data_asof`；
3. **T-1 标签修复**（H2）：末日伪标签（NaN 比较为 False）先修再谈日更模型；修复属口径变更，须单独重基线。

---

## 5. 常见跑不起来的坑（按我这边实测）

| 症状 | 原因 | 解法 |
|---|---|---|
| `ImportError: pyarrow/fastparquet` | pandas 读写 parquet 需引擎 | `pip install pyarrow` |
| ticai `FileNotFoundError: ... (先跑对应 collect/build)` | 数据未采集 | 先跑 §1.2 的 collect/build 全链 |
| tushare 接口 401/积分不足 | token 未配或积分不够 limit_list_d | 配 TUSHARE_TOKEN；积分不够先只采 fetch_limit_events + daily_panel（A 档最小集） |
| pandas 3.0 下 MultiIndex 运算 dtype 'M' 报错 / 索引错位 | Series 赋值给 DataFrame 按**标签**对齐、不按位置 | 特征计算统一用宽表 + numpy 值赋值；我脚本踩过 3 个这类坑，见 `p0a_tail_features/extract_tail_features.py` 头注 |
| t1_* 特征异常高 | 前视泄漏（忘了 shift） | 事件 T+1 收益必须 shift 1 交易日后才可用 |

---

## 6. 执行顺序建议

```
§1 强信号源特征（价值最大，先做）
  → §2 objective（同 harness 顺手，两个实验可并行出报告）
    → §3 校准（依赖最终模型定型，排在 1/2 判定之后）
      → §4 工程收尾（随时可做）
```

每项独立判定、独立留痕；任何一项"不显著"都按 H1 惯例记证伪，不硬接。

---

## 7. 执行状态与依赖核实（2026-11-22，实施侧实测补记）

> 本节只记录「哪些能跑、哪些跑不通」，不改动 §1~§6 的步骤定义。

| 项 | 本地可执行性 | 实测证据 / 处理 |
|---|---|---|
| §1 强信号源（原路径） | ❌ 跑不通 | ① §1.2 首步 `git clone` 与项目「本地禁 git 指令」红线冲突；② `TUSHARE_TOKEN` 未设置、`.env` 无 tushare 项；③ 文中「我交付的 `p0a_tail_features/extract_tail_features.py`」本地与交付包 `tmp/p0a_delivery_20261122/` 均不存在。需用户先解这三项，或走下方等价路径。 |
| §1 强信号源（本地等价路径） | ✅ 已实现实验 | A 档「涨停事件基因」可由本地 `kline_1D` 前复权 OHLCV 直接派生（涨停=ret≥板块阈值、一字板=OHLC 全等），绕开 git/token/外部脚本：`p0a_gbdt/tmp/train_gene_exp.py`，特征 ZT_CNT_20/60、YIZI_CNT_20、ZT_STREAK、DAYS_SINCE_ZT、T1_RET_MEAN_20（T+1 收益 shift(1) 防前视）。实测 2023~2026 共 79,229 涨停日、21.3% 样本有 20 日涨停史。**局限**：封单强度(fd_amount)/首封分钟(first_min)/炸板次数(open_times)/题材归属四列仍须外部库，本等价路径不含；B/C 档整体不可得。 |
| §2 objective 微调 | ✅ 本地可跑 | `p0a_gbdt/tmp/train_objective_exp.py`（lgb.train 原生 API，5 模式 base/spw5/10/20/focal2，同窗口/标签/超参）。标签正样本率 3.15%，符合失衡前提。 |
| §3 概率校准 | ✅ 本地可跑（已完成） | `p0a_gbdt/tmp/calib_exp.py`。四窗 OOF 2,593,560 行、正样本率 3.8%；结果见下。 |
| §4 [a] 分数落库+cron / [b] 提示注入 | ⚠️ 越界 | 与本文开头「不改任何 app/agent 生产代码」冲突，且需新增表/调度/提示注入，属独立拍板项，本次不执行。meta 溯源补 qfq hash 可留作低风险收尾。 |

**§3 校准实测结论（walk-forward，第 i 窗用前 i 窗拟合、第 i 窗评估）**：

| 方法 | 平均 ECE | 平均 Brier |
|---|---:|---:|
| raw（原始分数） | 0.00877 | 0.03223 |
| Platt | 0.01392 | 0.03284 |
| Beta | 0.00810 | 0.03218 |
| isotonic | 0.00803 | 0.03220 |

- 原始分数已校准良好；**Platt 显著恶化**（与文档 §3.1 引用文献一致）；Beta/isotonic 仅边际改善、Brier 几乎持平。
- 判定：按 §3.2「改善才接」，本轮**不接入校准**；Δp 链先用原始分数 + 在归因中披露「未校准」。若要接，Beta 是备选（C=1e6，walk-forward 拟合）。
- 完整逐窗数据 `p0a_gbdt/tmp/calib_exp_results.csv`、日志 `tmp/calib_exp.log`。

---

## 8. 审计修复后的重跑结果（2026-11-22 深夜，来源：代码前视审计）

> 本节为增量补记，不改动 §1~§7 的步骤定义与原始实测。

**8.1 标签口径修正触发重基线**

- 审计发现 `common.tail_label_binary` 的 `astype(int)` 把段末 `NaN` 转 0，制造伪负样本（`np.isfinite(y)` 因 int dtype 失效）。修为保留 NaN 后重跑四窗：
  recall@200 **0.2311**（旧 0.2317），vs momentum_5d +0.0277 CI[+0.0252,+0.0303]，8 项 CI 下界全 >0，lift 3.88× ⇒ **仍达标**。
- 常驻模型 best_iter 42→109、w3 49→46；旧模型入 `del/p0a_model_prefix_20261122/`（附 README 含回滚），新模型切默认。
- 对拍：w3_fixed vs `pred_w03` 200/200 diff=0；agent wheel vs `infer_daily` 200/200 diff=0；回归 63 passed。

**8.2 §2 objective 实测补记（原 §7 只标"可跑"）**

- `tmp/train_objective_exp.py` 5 模式全跑约 1h：base / spw5 / spw10 / spw20 / focal2。preliminary：spw 系列 best_iter=1（正样本率 ~3.8% 下的失衡处理把树压到最浅），此前的受控对比 spw5 diff −0.0092 CI[−0.0114,−0.0069] 显著更差 ⇒ **scale_pos_weight 路线证伪，不接**。
- focal2 梯度/hessian 已单独验证正确（w·(p−y)、hess>0、finite）；本轮 4 窗预测仅 w0 落盘（进程在为重基线腾内存时被终止），**不构成完整评测**，若要判定须重跑 focal2 全 4 窗。不影响「objective 不接生产」的现有结论。

**8.3 §3 校准重跑与 venn 别名更正**

- 新预测下重跑：raw ECE 0.01029/Brier 0.03364、Platt 0.01621/0.03457、Beta 0.00995/0.03369、isotonic 0.00970/0.03368。raw 的 Brier 最优、Platt 恶化 ⇒ **仍不接校准**，判定与 §7 原表一致。
- 原 `venn` 经查与 isotonic 完全同实现（结果逐位相同），已改名 `isotonic_alias` 并注明非 Venn-Abers；新结果 `tmp/calib_exp_results_fixed.csv`。引用旧 `calib_exp_results.csv` 的 venn 行须按 isotonic 别名解读。

**8.4 §4 [b] meta 溯源（低风险部分已完成）**

- `score.py` 的 `meta.factor_sig` 与 cache key 已纳入 `adjustment._CACHE_FILE` 的 `mtime_ns:size`，复权快照变化会使旧缓存失效并在 meta 中留痕；实测模拟快照变化后 `cache_hit=False`、factor_sig 同步更新。
- 同轮修复 F1 前视守卫：`as_of` 不在 kline 数据 / `as_of<data_last`（默认 strict）直接拒绝，与 `infer_daily.py` 对齐；`strict=False` 仅供显式历史回测。
- F3 私有 API 依赖（`adjustment._CACHE_FILE` / `_to_sina_code`）短期记入设计文档附录 J3 依赖清单，长期在 data 层开公共只读访问器替换；本轮未改 data 层文件。
