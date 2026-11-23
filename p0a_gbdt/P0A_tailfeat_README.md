# p0a_tail_features — ticai-daban 强信号源特征（H1 retry）

把 [ticai-daban](https://github.com/SixCake/ticai-daban) 的涨停事件库/题材快照/龙头因子
转成逐票逐日尾部特征表，供 P0-A 尾部二分类 harness 做特征增强消融（H1 retry）。

## 依赖

1. ticai-daban 仓库 + 其数据采集（需 tushare token，约 10-20 分钟）：
   ```bash
   python collect/fetch_limit_events.py   # 涨停事件(2019-11~)  ← A 档特征源
   python collect/fetch_daily_panel.py    # 全 A 日线网格
   python collect/fetch_kpl_events.py     # 开盘啦涨停/炸板(题材直标, 2024-01~)
   python collect/fetch_ths_limit.py      # 同花顺涨停池(2023-11~)
   python build/enrich_events.py          # 事件富化(一字板/T+1收益) ← 必跑
   python build/attribute.py              # 独占归属              ← B 档
   python build/theme_daily.py            # 题材日度快照          ← B 档
   python collect/factor_longtou.py       # 龙头因子 qscore/sscore ← C 档
   ```
2. pandas + pyarrow

## 用法

```bash
python extract_tail_features.py --ticai_root /path/to/ticai-daban \
    --out_dir tmp --start 2021-01-01 --end 2026-11-20
# 与训练宇宙逐行对齐(推荐):
#   --grid_parquet 传一个含 trade_date,ts_code 的 parquet(即你训练面板的键)
```

产物：`tmp/tail_features.parquet` + `tmp/tail_features_manifest.json`（覆盖率留档）。

## 接入 H1 retry 的协议（不动生产代码）

1. **A 档先跑**（事件基因，覆盖全窗口）：把 `FEATURE_COLUMNS` 的 A 组左连接进
   `alpha158_db` 特征矩阵（键：trade_date × ts_code），同窗口/标签/超参重训尾部二分类；
2. **B 档单独报**（题材，覆盖 2023-11 起）：只在后 2 个 walk-forward 窗比较，不与 A 档混窗；
3. 判定照旧：配对区块 bootstrap，recall@200 diff 与 top-decile lift 的 CI 下界 >0 才接；
   不显著就不接（H1 标准原封不动）；
4. 消融留痕：特征列清单以本脚本 `FEATURE_COLUMNS` 为准，改动同步 README/设计文档。

## 口径红线（详见脚本头部注释）

- **t1_\* 族 shift(1) 防前视**：事件的 T+1 收益在事件次日才可见，改代码勿去 shift；
- 股票→题材主链路是 `theme.attribution`（PIT 干净）；members 快照有成分漂移（仅回退用）；
- `is_st_last` 只覆盖上过榜的股票，全市场 PIT ST 需 tushare namechange（设计方案 H3）；
- `qscore/sscore` 源头未做 ST 修正（ticai 源码自注），下游可按 ST 表剔除。

## 文件

| 文件 | 作用 |
|---|---|
| `extract_tail_features.py` | ticai-daban → 逐票逐日尾部特征表（§1 特征源） |
| `focal_loss.py` | LightGBM focal loss 自定义目标（§2，数值校验过的 grad/hess） |
| `train_tailcls_exp.py` | walk-forward 实验训练器（logloss/weighted/focal 三臂 + 尾部特征接入） |
| `EXP_PROTOCOL.md` | 实验协议（矩阵/步骤/预注册判定） |

## 测试

```bash
python .openclaw/tmp/test_tail_features.py   # 合成数据冒烟(9 项断言, 含无前视校验)
```
（测试脚本会临时写入 ticai-daban/data/ 合成数据，跑完自动可删。）
