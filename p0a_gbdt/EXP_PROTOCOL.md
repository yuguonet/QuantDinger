# P0-A 尾部二分类实验协议（强信号源特征 × 训练目标矩阵）

> 对应《P0A_改进实施步骤_H1retry与校准.md》§1/§2 的执行落地。
> 判定标准沿用 H1 惯例；本协议 + train_tailcls_exp.py + focal_loss.py 三者口径一致，改动需同步。

## 1. 实验矩阵

| 臂 | 特征 | 训练目标 | 命令 |
|---|---|---|---|
| B0（基线） | Alpha158 | logloss | `python train_tailcls_exp.py --objective logloss --out_dir tmp/exp/b0_logloss` |
| B1 | Alpha158 | weighted | `... --objective weighted --scale_pos_weight 10 --out_dir tmp/exp/b1_w10` |
| B2 | Alpha158 | focal | `... --objective focal --gamma 2.0 --focal_alpha 0.5 --out_dir tmp/exp/b2_focal` |
| F0 | Alpha158+尾部特征 | logloss | `... --objective logloss --tail_features tmp/tail_features.parquet --out_dir tmp/exp/f0_tailfeat` |
| F2 | Alpha158+尾部特征 | focal | `... --objective focal --tail_features ... --out_dir tmp/exp/f2_tailfeat_focal` |

- B0 是组内基线（重跑，同窗口同早停口径）；与历史 0.2311 数值**不直接可比**（早停指标统一为 valid AUC），判定只做组内对比 + 对 naive。
- `--tail_features` 产物来自 `extract_tail_features.py`（ticai-daban 数据需先采集，见其 README）。
- 焦点问题两个：① F\*−B\*：强信号源特征的增量；② B1/B2−B0：目标函数的增量。交互项（F2 是否最好）顺带看。

## 2. 步骤

```bash
# 0) 前置：ticai 数据 + tail_features.parquet（一次性，约 30 分钟）
# 1) 五臂训练（每臂 ~全量 Alpha158 计算 + 4~5 窗训练；Alpha158 可缓存共享，见 §4）
# 2) 评测（对每臂的 tmp/exp/<arm>/pred_w*.parquet）：
cd p0a_gbdt
python evaluate.py --pred_dir tmp/exp/b0_logloss --out tmp/exp/b0_report.md   # 以 evaluate.py 实际 CLI 为准
# 3) 对比：各臂 vs B0 与 vs 四 naive 的 recall@200 / top-decile lift 配对区块 bootstrap CI
```

## 3. 判定（预注册，看结果前固定）

- **接**：相对 B0 与全部 naive，recall@200 与 top-decile lift 两个主指标的 diff 95%CI **下界均 >0**，且分年度无崩盘；
- **不接**：CI 跨 0 → 按 H1 惯例记证伪，不进生产；
- 调参冻结：scale_pos_weight 只测 10 一档、focal 只测 (γ=2.0, α=0.5) 一组——如需第二轮，预注册新档位并用未触碰的新测试窗验证（附录 C 口径）；
- 消融留痕：每臂的 `exp_meta.json`（含 feat_cols 全列清单）随报告归档。

## 4. 工程注意（易错点摘录，详见两脚本头注）

1. **focal 双约定**：lightgbm 原生 objective 签名 `(preds, dataset)`，sklearn 包装后是 `(labels, preds)`——focal_loss.py 已自动识别，别"统一"成单一签名；
2. **分数口径**：三臂统一 `raw_score=True` + 手动 sigmoid（focal 无 predict_proba 变换）；尾部指标全是排序口径，单调变换不影响判定，但落盘分数要口径一致；
3. **join 键**：tail_features 的 ts_code 与 DB symbol 取 6 位前缀归一后连接；覆盖期外 NaN 保留（不填 0）；
4. **标签**：`tail_label_binary` 段末 NaN 不可训练（J1 修复），勿改回 astype(int)；
5. **内存**：Alpha158 全量约 8~10GB 峰值（train_alpha158.py 头注），五臂共享同一次计算可省 4 次——若机器内存吃紧，一次算完落盘因子宽表再分臂训练（改脚本时保留口径）。

## 5. 产物与留痕

| 产物 | 位置 | 内容 |
|---|---|---|
| 逐窗预测 | `tmp/exp/<arm>/pred_w*.parquet` | evaluate.py 输入 |
| 窗口表 | `tmp/exp/<arm>/windows.csv` | 窗口边界 + best_iteration + 测试段正样本率 |
| 臂配置 | `tmp/exp/<arm>/exp_meta.json` | objective/特征列全清单/种子 |
| 对比报告 | `tmp/exp/exp_compare.md` | CI 对比表 + 判定结论 |

结论出来后同步进设计方案附录（新增 H1 retry 结果条目），显著项再谈 fit_live_model 重训切换。
