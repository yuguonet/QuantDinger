# A股K线 Next-Bar 预测 · 基于MiMo生态的训练框架设计方案

> 版本：v1.0（2026-11-21）
> 目标：修改现有训练框架，使模型能够识别A股**日K线**与**分时K线**，完成**下一根K线（next-bar）预测**：日线语境下即 T+1（次一交易日），分时语境下即下一周期（下一分钟/5分钟/30分钟K线）。核心与语言模型的 next-token 预测同构。
> 结论先行：**以 `XiaomiMiMo/verl` 为基座修改（语言训练框架）；借鉴 `MiMo-Audio-Tokenizer` 的多码本量化架构自训"行情codec"；不以 `uni-agent` / `MiMo-Audio-Training` 为基座。**

---

## 1. 问题定义

> ⚡ 目标函数重构（2026-11-22，依据"小资金集中攻击"战略定位）：本项目的真优势是**尾部捕捉 + 集中持仓 + 快进出 + 小票猎场**，而非全市场均值预测。预测目标与评测指标必须对准尾部事件（涨停/炸板/龙头启动/题材发酵），而非机构口径的 IC/RankIC。基础模型（KiT/Kronos）只提供分布与尾部概率，集中攻击的决策由 agent + 经验库完成。

| 项 | 定义 |
|---|---|
| 输入 | 某标的截至当前周期的K线序列（OHLCV），支持多分辨率：日K（长窗）+ 分时K（1/5/30分钟，近期细节） |
| 输出 | **下一根K线（next-bar）**预测：在各分辨率上预测下一周期的**收益分档**（三分类：跌>2% / 平 / 涨>2%，阈值按周期波动率自适应），可扩展到点位区间 |
| 约束 | A股 T+1 交易制度（日线级）；严格无未来信息泄漏；**预测目标统一表述为 next-bar，T+1 只是日线分辨率的特例** |
| 核心难点 | 金融序列信噪比极低、非平稳、接近弱有效市场；过拟合风险高 |

### 1.1 任务形态判定

这是**单轮"序列输入 → 可验证输出"**任务（RLVR 场景），不是多步工具调用的长时程 agent 任务。这一判定直接决定了框架选型（见 §2）。

### 1.2 与 next-token 的同构性

核心思路与语言模型完全一致：**用当前及历史K线序列预测下一根K线**。

| 语言模型 | 本方案 |
|---|---|
| 词（token） | 一根K线（关系型复合token） |
| 下一个词的概率分布 | 下一根K线的收益分档分布 |
| 自回归逐词生成 | 自回归逐bar生成（可用于多周期滚动预测） |
| 语法约束（词性搭配） | 市场约束（OHLC内部结构、量价关系） |

多分辨率下的自回归是统一的：日K预测次一交易日，分时K预测下一周期，**同一套模型结构、同一套训练目标，只是时间尺度不同**。多周期联合训练还能让模型学到跨尺度传导（分时聚集 → 日线形态）。

### 1.3 选股漏斗：如何从市场找到最优的股票（前置问题）

> ⚡ 2026-11-22 补充：目标函数重构只解决了"怎么打"，遗漏了"打哪只"——集中攻击的第一道难题是**检索**：从 ~5000 只票里把最优候选捞进攻击池。预测模型喂错候选池就是空转。

**四层漏斗（逐层收窄，每层不同武器）**：

```
全市场 ~5000 → 【第0层 闸门】情绪周期判定（能不能打）→ dragon_cycle
            → 【第1层 粗筛】5000→200：硬过滤+量价异动/资金流/龙虎榜基因（规则+GBDT/Alpha158）【唯一硬门，查全优先】
            → 【第2层 联合决策】200→3~8：agent 拿【KiT 尾部概率 + 题材/龙虎榜/公告/情绪】联合裁决 + 仓位 + 盘中预案；
              内部两遍（廉价分诊全量→深挖 top20~30）；含 3~5 个"叙事野生牌"名额（概率平庸但题材异动的票）；
              KiT 全量打分先落盘存档（独立归因 + Kronos 期权判定依据）
```

**⚠️ 2026-11-22 再修订：精排与决断合并为一步决策**——串行硬门会砍掉"概率平庸但叙事强"的候选，而最优股票恰是分布×叙事的交集；少一道门=少一个漏牛股的环节。但**计算不合并**：KiT 全量打分（便宜）与 LLM 深度分析（昂贵）分层，且 KiT 分数独立落盘，保证经验库能分层归因（哪层漏了牛股/哪层放进的坑）。

**核心认知**：

1. 选股是**排序/查全**问题（极端类别每天仅 0.5%），评价用 **recall@K**（今日涨停/龙头是否在昨晚候选池），非准确率；
2. 粗筛宁滥勿缺（查全优先），精排才要查准——粗筛用 GBDT 排序比深度模型合适；
3. "最优"由题材 × 情绪 × 筹码结构的叙事驱动，纯量化特征抓不到 → 第2层 LLM 多模态不可替代；
4. **漏斗自身被经验库逐层评估进化**：记录每层漏掉的牛股/放进的坑，分层归因。

**与现有资产映射**：第0层=dragon_cycle*（齐）；第1层=daily_prescan/daily_screener/app/agent/preselect（缺统一 recall 评估）；第2层=stock-analyzer skill + dsa/龙虎榜（缺 KiT 尾部概率输入，本次主线）；第3层=经验库 chain/（缺漏斗分层评估口径）。

### 1.4 专项模型与盘中作战流（2026-11-22 补充）

**Kronos 专项训练（小资金集中攻击专才）——⚠️ 已降级为条件期权（2026-11-22 再修订）**：

- **主线定论**：GBDT 打量（粗筛）+ KiT 打分布（尾部概率）+ agent 打叙事时机 + 经验库进化；Kronos-FT 无固定生态位（粗筛打不过 GBDT，精排打不过 KiT），**不预先投入训练**；
- **期权行权条件**：先测 KiT 尾部概率在攻击池切片（连板/题材股）上的校准度（约1天）；若系统性失准 → 启动 Kronos 专项微调，唯一目标：该切片尾部概率更准，做 KiT 专属替补；校准良好则永不启动；
- **原则**：训练模型是对实测缺陷的响应，不是计划项；
- 若行权，训练配方：① 强势股/题材股 regime 继续预训练（无标签）→ ② 尾部事件目标微调，**必须带 hard negatives**（同形态但炸板/冲高回落的阴性样本）——"淘汰低信噪比"只能淘汰在**推理范围**，不能淘汰在训练集，否则学不到判别边界；
- **"简化 Kronos 打量"（替代 GBDT）**：胜率不友好（tabular 上 GBDT 强 + 粗筛是 recall 问题对精度不敏感），如要验证：同数据/同 recall@K/同算力预算对决，一周出结果，实验池内待触发，不占主线排期。

**盘中作战流（LLM 不直接吃 1m 数据）**：

- 分工：1m 数据流 → 量化层（KiT分时+规则）检测事件（放量突破/炸板回封/承接转强）→ 事件摘要（非原始K线）→ agent(LLM) 带题材/情绪/龙虎榜上下文解读与择时决断；
- 决策窗口化：集合竞价后（9:25~9:35）/ 盘中关键点（10:00）/ 尾盘（14:30~）+ 事件触发，不全程盯盘；
- 用途与 T+1 自洽：① 攻击池建仓/撤退择时 ② 次日开盘卖点规划（对应 v2_tail_buy 系列）。

**单日作战流**：盘后（粗筛GBDT 5000→200 → 联合决策一步出攻击池3~8只+仓位+预案，KiT分数落盘）→ 早盘（同一 agent 确认/否决）→ 盘中（量化事件监控+同一 agent 事件判断）→ 尾盘（agent 建仓决断）→ 经验库记录全链路（KiT 分数与 agent 判断分账）。

---

## 2. 框架选型

### 2.1 候选仓库盘点（github.com/XiaomiMiMo）

| 仓库 | 角色 | 与本任务的关系 |
|---|---|---|
| **verl**（fork of verl-project/verl，★761） | RL 后训练引擎：rollout / reward / GRPO·PPO，暴露 `agent_loop_manager_class`、`recipes/` 等扩展点 | ✅ **主基座** |
| **uni-agent**（fork of verl-project/uni-agent，★26） | 长时程 agent 训练框架：Session、Gateway、TransferQueue 轨迹捕获 | ❌ 不选（单轮预测用不上；未来升级为自主分析 agent 时再引入） |
| **MiMo-Audio-Training**（★114） | 音频语言模型训练框架（音频 token 与文本 token 交错，ASR/TTS/语音对话） | ❌ 不选为基座；可作冻结特征对照实验 |
| **MiMo-Audio-Tokenizer**（★150） | 语音统一 tokenizer：多码本 VQ，语义 + 高保真重建 | 🔶 **抄架构不抄模型**：借其多码本 VQ 骨架自训行情 codec |
| MiMo / MiMo-V2-Flash / MiMo-V2.x | 基座模型（含 RL 后训练技术报告） | 模型 backbone 来源 |

### 2.2 选型结论与理由

**选 verl 的理由：**

1. **奖励可自动验证**：T+1 真实走势存在于历史数据中，reward 可精确计算，天然适配 GRPO + 可验证奖励（RLVR）。
2. **任务形态匹配**：单轮预测 = 简单 agent loop，不需要 uni-agent 的 Session/Gateway/长时程编排。
3. **扩展点现成**：`recipes/` 就是为自定义任务设计的，新增金融 recipe 是"加文件"而非"改核心"。
4. **MiMo 实际用法佐证**：在 MiMo 的 Code recipe 中，uni-agent 仅作为模型网关/轨迹捕获替代 verl 的 agent-loop 管理器——两者是上下游配合关系，训练引擎始终是 verl。

**不选 uni-agent 的理由**：只有当作"自主分析 agent"（自己拉数据、画图、多轮推理、写回测）时才值得用；对单轮预测是纯负担。

**不选 MiMo-Audio-Training 的理由**：

- 语音 tokenizer 的码本从**人声波形**上学出（音素/说话人/共振峰），编K线存在根本性 domain gap；
- 其训练循环是语音生成式，无可验证奖励的 RL 后训练，改造工作量大于改 verl；
- sonification（把K线映射成声音）引入人为伪影，换一个映射就换一种"声音"，学不出稳健表征。

### 2.3 "声音思路"的正确吸收方式

借鉴的不是声学本身，而是音频领域的两件成熟工具：

1. **多码本分层向量量化**（EnCodec/DAC 式）：粗码本编码 regime（趋势/盘整/放量突破），细码本编码微观纹理（影线、日内抖动）；
2. **多分辨率时频特征**：小波/STFT 通道 + 多帧聚合（对应日K=旋律、分时=音色细节的双层结构）。

落地方式：**用 MiMo-Audio-Tokenizer 的 VQ-VAE 骨架作参考实现，把声学编码器换成K线特征编码器，在A股数据上从头训练"行情 codec"**，产出的离散 token 喂入 verl recipe。

---

## 3. "词"方案：输入编码设计

### 3.1 被否决的方案

| 方案 | 问题 |
|---|---|
| 原始价格直接分位数离散 | 非平稳（10元票 vs 历史50元票不可比）；全历史统计分位数 = 泄漏 |
| 数值转字符串（LLMTime 式） | 浪费 token，打碎数值连续性 |
| OHLCV 五值独立量化 | 浪费容量学习 low≤open,close≤high 等恒等式约束 |

### 3.2 采用方案：两阶段演进

**阶段A（先跑通闭环）——关系型复合词 + 平稳化特征：**

- 每根K线 = 一个"复合词"，特征取**关系结构**而非原始值：
  - 实体相对位置、上下影线比例、开收相对高低；
  - log return、振幅（(high-low)/close）、量比（volume / MA20(volume)）；
- 分位数 bin 在**滚动窗口**上计算（防泄漏），离散为 token；
- 目标：**T+1 收益三分类**（跌>2% / 平 / 涨>2%），不回归原始 OHLCV。

**阶段B（验证信号存在后）——行情 codec（多码本 VQ）：**

> ⚡ 路线修订（见附录B）：**优先直接采用 Kronos 的分层离散 tokenizer + 预训练权重**，仅当其表征不足时才启动下述自研方案。

- 训练 VQ-VAE：输入 = 平稳化 OHLCV 特征序列 + 小波/STFT 通道；
- 多码本分层：粗码本 ↔ regime，细码本 ↔ 微观结构；
- 参考 `MiMo-Audio-Tokenizer` 的码本/量化/EMA 更新实现；
- 离散 token 序列进入 LLM 自回归主干（日K与分时双分辨率编码，可拼接或双塔后融合）。

### 3.3 多模态扩展（后期）

将文本上下文（行业、市值分位、近期公告摘要）与 K线 token 拼接。纯K线模型天花板接近弱有效市场；LLM 路线相对传统时序模型的真正优势在于多源信息融合。

---

## 4. 数据层设计（工作量最大，约占60%）

### 4.1 语料构建

- 覆盖：全A股数千标的 × 日K（复权处理：前复权）+ 分时（1/5/30分钟聚合）；
- 清洗：停牌/涨跌停/ST标记、新股窗口剔除、复权因子一致性校验；
- 特征：§3.2 的关系型特征，滚动窗口标准化。

### 4.2 样本生成与防泄漏（红线）

- **按时间切分** train/val/test，禁止随机 shuffle；建议留最近一年完全不碰做最终测试；
- 分时数据的时间截断必须卡死：用 T 日收盘前信息预测 T+1，任何 T 日之后的数据（含盘后公告时点）不得进入输入；
- 分位数统计、标准化参数只在训练窗口拟合；
- walk-forward 验证：滚动窗口训练 → 前推测试，模拟真实使用。

### 4.3 标注（按分辨率统一为 next-bar）

- 主标签：**下一周期收益三分类**，阈值按周期自适应（日线 ±2%，分时按该周期历史波动率的分位数定，如 ±0.5%）；
- 辅助标签（多任务用）：下一根K线振幅分档、未来 N 根趋势方向（防短视）；
- 各分辨率样本可混合训练（在样本中显式标注周期类型 token），实现"一套模型、多尺度预测"。

---

## 5. 训练层设计（verl 改造）

### 5.0 P0 先行：KiT / Kronos 基线评测（写入实施计划）

**目的**：用最低成本回答两个问题——(1) A股 next-bar 到底有没有可学习信号？(2) 现成基础模型（**KiT 优先，Kronos 对照**）够不够用，还需不需要投入训练层改造？

**环境准备**：

```bash
# 准备：Python ≥3.10，PyTorch ≥2.0，单卡即可（mini/small 推理无压力）
git clone https://github.com/shiyu-coder/Kronos
cd Kronos && pip install -r requirements.txt
# 权重自动从 HuggingFace 拉取：NeoQuasar/Kronos-Tokenizer-base, NeoQuasar/Kronos-{mini,small,base}
```

**数据准备**（复用 §4 数据层最小版）：

- 取 300~500 只A股（沪深300成分 + 中证500抽样）近 5 年日K + 近 1 年分时（5分钟）；
- 前复权 OHLCV，剔除 ST/新股窗口<60日；
- 按时间切：训练窗/验证窗/测试窗（最近 6 个月作最终测试，不碰）；
- 注意 Kronos max_context=512，lookback 取 120~250 根。

**评测脚本骨架**（`p0_kronos_baseline.py`）：

```python
"""P0: Kronos zero-shot / finetune baseline vs naive baselines.
注意：以下 API 以 Kronos 仓库 README 为准，运行前需核对实际签名。"""
from model import Kronos, KronosTokenizer, KronosPredictor
import numpy as np

# 1. 加载（零样本）
tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
predictor = KronosPredictor(model, tokenizer)

# 2. 滚动预测：每个截断点 T，用 [T-lookback, T) 预测 T+1
#    严格禁止未来信息：分位数/标准化只用 [0, T) 窗口
# 3. 三档分类：把预测分布映射到 跌>2%/平/涨>2%（阈值按周期波动率自适应）

# 4. 对照组（缺一不可）
baselines = {
    "copy_last":   lambda: last_close,        # 照抄上根
    "momentum":    lambda: sign(ret_5d),      # 5日动量
    "random_walk": lambda: random_pred,       # 随机游走
}

# 5. 指标（与训练损失脱钩，只看这些）
#    - 方向准确率（涨跌二元）
#    - 三分类命中率 / macro-F1
#    - RankIC（预测值 vs 实际收益的横截面秩相关，Kronos 论文主指标）
#    - 分年/分市值/分行业稳定性（walk-forward 各窗口分别报告）
```

**微调分支**（零样本不达标时）：用 Kronos 官方微调脚本在训练窗上继续预训练，验证窗早停，再跑同一套评测。

**P0 出口判据**：

| 结果 | 下一步 |
|---|---|
| Kronos 零样本/微调明显打赢所有 naive baseline | 直接进 P1（用 Kronos tokenizer + MiMo 主干对比 Kronos 独立模型） |
| 与 naive baseline 打平或互有胜负 | 进 P1 但降低预期，重点转向多模态融合（P4）或换目标 |
| 全面打不赢 | ⚠️ 止损预测路线，转风控/异常检测（§7） |

**尾部口径评测（2026-11-22 补充，对准小资金集中攻击）**：除均值指标外必须报告：

- P(涨幅>7%) 等尾部概率的校准度与命中率（KiT 分布输出的正确用法）；
- Top-N 集中选股（10~20 只）的次日收益分布，而非全市场组合；
- 隔日涨停/炸板命中率（查全+查准）；
- 情绪周期分段下的单位时间收益比与最大回撤；
- ⚠️ 回测框架取 `market_cn/auto`（自带）+ qlib（对照），但组合构建必须用集中持仓口径，不用 CSI300 多头组合。

**耗时预估**：环境 0.5 天 + 数据 1 天 + 评测脚本与跑通 1 天 ≈ 2~3 天出结论。

### 5.1 三阶段路线（每步独立产出，可随时止损）

| 阶段 | 内容 | 生死线 |
|---|---|---|
| **P1 数据层 + SFT 基线** | 文本 token 路线（阶段A编码），纯 SFT，不碰 RL | 验证模型对K线序列存在学习信号 |
| **P2 verl recipe + GRPO** | 新增 `recipes/astock/`：数据集类 + reward 函数；RLVR 打 next-bar 方向命中 | **必须打赢 naive baseline**（照抄上根/动量/随机游走），否则止损换目标 |
| **P3 行情 codec** | 阶段B：VQ-VAE 多码本替换输入编码，主干继续 GRPO | 对比 P2 有增量收益 |
| **P4 多模态融合 + agent 化（可选）** | 文本上下文拼接；如有需要引入 uni-agent 做自主分析编排 | 视业务需求 |

### 5.2 verl recipe 新增内容（框架层改动清单）

```
recipes/astock/
├── dataset.py        # K线token序列数据集（时间切分加载器）
├── reward.py         # 可验证reward：方向准确率 / 收益分档命中（加权）
├── agent_loop.py     # 单轮loop（如默认loop可满足则不写）
├── config.yaml       # GRPO配置：lr、KL系数、rollout温度
└── eval_callback.py  # walk-forward验证：vs naive baseline（与训练reward脱钩）
```

- 用 **GRPO + RLVR**，单轮 rollout，不引入 uni-agent；
- reward 设计（防 reward hacking）：
  - 基础分：三分类命中（命中 1 / 未命中 0）；
  - 置信度校准项：预测"大涨"且实际大涨 → 加分；预测"大涨"实际大跌 → 重罚（惩罚方向自信错误，鼓励校准）；
  - 过度交易惩罚：全样本输出分布严重倾斜（全押一类）→ 扣分。

### 5.3 防过拟合专项（关键闸门）

1. **评估与损失脱钩**：不看 loss 选模型，只看 walk-forward 上"方向准确率 / 相对 naive baseline 的超额"；
2. **naive 对照组**：照抄上一根、动量、随机游走——赢不了就放弃该路线；
3. 时间切分 + 滚动统计（§4.2）；
4. 多标的联合训练（全A股同一"语言"），禁止单票训练；
5. 标签从回归改分类（天然抗噪）；
6. checkpoint 早停以验证集超额收益为准。

---

## 6. 模型层设计

- Backbone：MiMo 系列（MiMo-V2-Flash / V2.x，按算力选）；
- 输入侧：阶段A为离散 token embedding；阶段B为行情 codec 的码本 embedding（先冻结主干只训 embedding 层，再全量微调）；
- 输出侧：三分类 head（或文本 token 输出分类词，便于 RLVR 统一）；
- 多分辨率：日K序列（长窗）+ 分时序列（近期细节）双输入，拼接或双塔融合。

---

## 7. 风险与止损策略

| 风险 | 应对 |
|---|---|
| 信噪比过低，模型学成"照抄上根+抖动" | §5.3 对照组闸门；P2 打不赢即止损 |
| 数据泄漏 | 时间切分红线 + 滚动统计 + 分时截断审计 |
| reward hacking（全押一类/蹭reward） | §5.2 reward 设计 + 输出分布监控 |
| 过拟合 | walk-forward、分类目标、多标的、早停 |
| sonification 路线失效 | 廉价对照实验：K线sonification → 冻结 MiMo-Audio-Tokenizer encoder 抽特征 → 浅层分类头，1~2天出结论；预期不占优则彻底关闭该支线 |
| 回测好 ≠ 实盘好 | 严禁直接上实盘；过拟合深度远超直觉 |

**止损后的替代目标**：若预测方向无信号，将同一管线转做**风控/异常检测**（放量异动识别、形态识别），数据与 codec 资产可复用。

---

## 8. 工程优先级总结

```
verl（主基座，改 recipe，~10%改动量）
  ↑ 产出token
行情codec（借 MiMo-Audio-Tokenizer 架构自训，中等工作量）
  ↑ 提供编码
数据层（A股K线语料，~60%工作量，决定成败）
```

**一句话版本**：用 verl 做训练闭环，借音频 tokenizer 的多码本架构解决"词"的离散化难题，数据层先行、SFT 验信号、GRPO 打生死线、codec 最后进场。

---

## 附录A：关键链接

- verl（基座）：https://github.com/XiaomiMiMo/verl
- uni-agent（未来agent化备选）：https://github.com/XiaomiMiMo/uni-agent
- MiMo-Audio-Tokenizer（架构参考）：https://github.com/XiaomiMiMo/MiMo-Audio-Tokenizer
- MiMo-Audio-Training（对照实验用）：https://github.com/XiaomiMiMo/MiMo-Audio-Training
- MiMo 模型合集：https://github.com/XiaomiMiMo

## 附录B：GitHub 外部方案调研（2026-11-21）与路线修订

### B.1 调研发现

| 项目 | 简介 | 对本方案的价值 |
|---|---|---|
| **[Kronos](https://github.com/shiyu-coder/Kronos)**（AAAI 2026，MIT） | 首个金融K线基础模型：OHLCV→**分层离散token**→自回归Transformer，45+交易所预训练；mini/small/base/large（4.1M~499M），权重在 HF: NeoQuasar/Kronos-* | 替代自研 codec（阶段B）；MIT 安全备胎 + 对照组 |
| **[KiT](https://github.com/Luciferbobo/KiT)**（arXiv 2609.34507，⚠️ AGPL-3.0） | K线扩散基础模型（Diffusion Transformer，KiT-L=284M）：数十亿根K线、美股/A股/加密、**7粒度（1m~1d）**；OHLCV→5维对数比状态(跳空/实体/上影/下影/量)=§3.2 关系型特征；生成式分布输出（涨跌概率+波动率区间）；自报全面超 Kronos（均值 .0571 vs .0342/.0521） | ⭐ **嵌入 app/agent 的首选预测工具**（隔离调用）；分时粒度与分布输出完美对口需求；训练代码未开源，暂不能微调 |
| **[Alpha-R1](https://github.com/FinStep-AI/Alpha-R1)**（财跃星辰×上交大，8B） | 金融推理模型：语义门控 + 两阶段 GRPO，奖励取全真模拟环境真实收益 | 验证 verl+GRPO+真实收益 reward 路线；两阶段 GRPO 设计可参考 |
| **[TokenCast](https://github.com/ustc-time-series/TokenCast)**（USTC） | 动态向量量化→可逆 temporal token，与文本token共享语义空间，冻结LLM生成式预测 | "词"路线顶配参考；codec 备选方案 |
| **[AutoTimes](https://github.com/thuml/AutoTimes)**（清华） | 现成 LLM 直接当自回归时序预测器，支持 in-context forecasting | "LLM主干不动、只改编码"路线参考 |
| **[ChatTS](https://github.com/NetManAIOps/ChatTS)**（字节，VLDB'25） | LLM 时序理解与推理，含合成训练集 | P4 文本/时序融合部分参考 |
| **[Qlib](https://github.com/microsoft/qlib)**（微软） | 量化平台：Alpha158/Alpha360 因子集 + benchmark + 数据收集器 + 回测框架 | ⭐ **P0 对照组与评测基建**：数据层/回测直接用；"找轮子"首选 |
| **Chronos**（Amazon） | 时序基础模型（"时序当语言"开山作） | 必备对照 baseline |
| daily_stock_analysis | LLM 多市场分析仪表盘（应用层） | 产品形态参考，非训练层 |

### B.2 路线修订：从"造轮子"到"装轮子"

原 P3（自研 VQ-VAE 行情 codec）降级为备选，主线改为：

| 阶段 | 内容 | 说明 |
|---|---|---|
| **P0（新增）** | Kronos 零样本/微调基线，直接在A股数据上跑 next-bar 预测 | 1~2天出结论；生死线同 naive baseline 对比；证明信号存在再投入 |
| P1 | 数据层 + **Kronos tokenizer** 喂 verl SFT | 编码层换现成的，省掉 codec 开发 |
| P2 | verl + GRPO，reward 参考 Alpha-R1（真实收益作奖励、防reward hacking） | 不变 |
| P3（备选） | 如 Kronos token 表征不足 → TokenCast 式自研 codec | 仅在有证据需要时启动 |
| P4 | 文本融合（参考 ChatTS）/ agent 化（uni-agent） | 不变 |

⚠️ 注意事项：

- Kronos max_context = 512 根K线（small/base），超长自动截断，长序列设计需适配；
- Kronos 权重规模（最大 499M）远小于 MiMo 主干——两条路线可对比：(a) Kronos 独立模型微调；(b) Kronos tokenizer + MiMo 主干（大模型容量 + 金融token）；
- 所有外部项目仅作代码/权重参考，引入前需复核其数据泄漏控制（尤其 Kronos 预训练数据的时间截止点与回测区间重叠问题）。

### B.3 Qlib benchmark 的启示（2026-11-22 补充）

来源：https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md（CSI300，20 随机种子均值±std）

**诚实信号水位**：日频 alpha 的 IC ≈ 0.03~0.05、RankIC ≈ 0.04~0.05、最优年化 ≈ 8~12%、IR ≈ 1.0。P0 预期应校准到此量级。

**核心规律（佐证 §3.2 设计）**：Alpha158（关系型技术因子）上 Linear(年化6.9%)/LightGBM(9.0%) 打赢大部分深度模型（Transformer/TCN/GRU 仅 2.6~3.4%）；Alpha360（原始价量序列）上深度模型普遍更差。**关系型特征 >> 原始序列；tabular 特征上 GBDT 是硬骨头**。

**P0 对照组阶梯修订（生死线升级）**：

```
① 照抄上根 / 动量        → 打赢算及格
② Linear / LightGBM + Alpha158（qlib 现成）→ 打赢才值得投入 P1-P4（真正生死线）
③ KiT（顶部对照）+ Kronos       → LLM 路线的差异化价值收窄为：文本多模态融合 + 决策推理（agent 本职），而非卷K线预测本身
```

**基建复用**：qlib 的 A股数据收集器、PIT 安全数据集、回测框架（CSI300 基准组合）直接用于 P0 评测，不自写回测。

### B.4 app/agent 嵌入结论（2026-11-22 补充）

目标宿主：QuantDinger `backend_api_python/app/agent/`（tools 动态加载 + skills + chain 经验库）。

| 组件 | 嵌入位置 | 姿势 |
|---|---|---|
| **KiT** | `tools/finance/kline_predictor.py` 薄适配器 | ⚠️ **进程隔离**：独立微服务/subprocess 调用，不修改其源码、不复制代码入库（AGPL 传染义务）；只用官方 checkpoint + 未修改的 kit_v1 推理；统一接口 `predict(symbol, horizon, granularities) -> {涨跌概率, 波动率区间, 路径样本}` |
| Kronos | 同接口备胎 + P0 对照组 | MIT，需要微调/商用顾虑时替换 KiT |
| qlib | `chain/backtest_bridge.py` 评估引擎 | 子进程/服务化，给经验库 T+n 验证用，不进 agent 进程 |
| verl | 不嵌入 | 离线训练侧，只部署产物 |
| Alpha-R1 | 可选 LLM backbone | 看算力，替换 prompt/抽取层的底模 |
