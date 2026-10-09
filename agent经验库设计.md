# A股 Agent 经验库体系设计 —— 从预测到经验引擎

> **日期**：2026-10-09
> **版本**：v2
> **主题**：如何让 Agent 在 A 股中短线分析中建立持续进化的能力
> **状态**：方案讨论稿，待细化后进入实施
> **整理说明**：本文由 `agent经验库设计.txt` 去重、补齐代码围栏与表格后整理为 Markdown（Trae 整理，2026-10-09），原始 txt 保留未删改
> **v2 修订说明**：**Part I（§1–§9）为 v1 原文，未作删改**；v2 追加 Part II（数据层，§10–§17）与 Part III（§18–§22）。
> ⚠️ v2 早期一稿曾为突出数据层而压缩 Part I 内容并改用 `fct_` 表名，属越权改动，已全部回退。

---

## 目录

**Part I · 策略层（v1 原文）**
- §1 问题起点与任务重定义
- §2 A 股分析的准确率困境与定位转变（含 §2.4 目标澄清、§2.7 定位论证、§2.8 为什么不与机构正面竞争）
- §3 "老股民"与"分析师"的统一
- §4 六层经验体系（L0–L5）
- §5 案例怎么产生
- §6 检索：两阶段混合检索
- §7 经验摘要压缩（Experience Digest）
- §8 校准闭环
- §9 各层沉淀速度与冷启动

**Part II · 数据层（v2 新增，服务于 Part I）**
- §10 为什么数据层成了瓶颈
- §11 设计原则
- §12 新增维度表
- §13 `exp_cases` 增强（原地升级）
- §14 `exp_patterns` / `exp_calibration` 增强
- §15 新增：配置快照与实验档案
- §16 Point-in-Time 正确性
- §17 回退链、有效样本、索引与迁移

**Part III · 落地**
- §18 冷启动
- §19 实施优先级
- §20 风险与待决
- §21 证据索引
- §22 总结

---
---

# Part I · 策略层（v1 原文，未作删改）

---

## 1. 问题起点：小模型 Agent 的推理能力提升

### 1.1 通用策略

| 优先级 | 策略 | 成本 | 效果 |
|--------|------|------|------|
| 1 | 结构化 Prompt + Few-Shot | 零 | 立竿见影 |
| 2 | 工具增强（计算器/检索/代码） | 工程 | 显著 |
| 3 | Self-Reflection 模式 | 近零 | 明显 |
| 4 | 上下文压缩 + 外挂记忆 | 中 | 长任务稳定性 |
| 5 | 多次采样 + 验证器 | 算力 | 用算力换质量 |
| 6 | SFT 微调推理数据 | 数据+算力 | 天花板最高 |

### 1.2 核心原则

> 用架构约束代替模型能力，用工具精确性代替模型记忆，用结构化 prompt 代替模型的自由发挥。
> 把"智能"从模型参数中解放出来，分散到系统设计的每个环节中。

---

## 2. A 股分析的准确率困境与定位转变

### 2.1 任务重定义

- ❌ "预测明天大盘涨跌" → 本质接近抛硬币
- ❌ "推荐买入哪只股票" → 输出无法验证
- ❌ "分析这只股票好不好" → 主观且模糊

- ✅ "给出未来5日收益率的排序" → 可用 RankIC 验证
- ✅ "评估该事件对板块的冲击幅度" → 可回测对比
- ✅ "识别当前处于什么市场状态" → 可分类验证
- ✅ "给出行情信号的置信度" → 可校准检验

**核心原则：让 Agent 做它擅长的"理解和提取"，让量化模型做"预测和排序"。**

> **v2 注记**：本节的「RankIC 验证」是**机构视角的成功标准**（全市场横截面排序能力）。
> 它与本仓库定位（`AGENTS.md`：**A股量化交易系统，小资金快速复利**）存在张力——
> 详见 §2.8，那里给出了**应遵循的成功指标体系**，并说明为何不应以 RankIC 为主指标。

### 2.2 A股数据金字塔

```text
      政策/监管信号（最强alpha）
    ─────────────────────────
   资金流/北向/融资余额/龙虎榜
  ─────────────────────────────
  财报文本/公告/投资者问答/调研纪要
─────────────────────────────────
行业景气度数据（高频中观数据）
─────────────────────────────────────
基本面因子（ROE/PE/PB/现金流/...）
───────────────────────────────────────────
量价技术因子（动量/波动率/换手率/...）
```

### 2.3 架构原则：量化 + LLM 混合

```text
        ┌──────────────┐
        │  信号融合层   │ ← 加权/机器学习融合
        └──────┬───────┘
               │
 ┌─────────────┼─────────────┐
 │             │             │
量化因子模型  LLM Agent   另类数据模型
```

**不要让 LLM 直接输出买卖建议，而是输出结构化特征，交给量化模型。**

### 2.4 关于"校准 vs 提升预测力"的澄清（v2 修正，原表述有误）

> ⚠️ v1 原文此处把目标二分为「校准可行 / 提升预测力不可行」。**这个二分是错的**，v2 予以修正。
> 修正发起人：用户指正（2026-10-09）。

**① 概念混淆：校准与提升胜率本来就是同一件事**

| | 问的是什么 | 数学形式 |
|---|---|---|
| 校准 | 这种情况下我该信几分？ | 估计 P(正确 \| 判断, 情境) |
| 提升胜率 | 什么情况下出手成功率最高？ | 估计 P(结果 \| 情境) |

两者都是**条件概率估计**，是同一枚硬币的两面。估计出条件概率之后，你自然既知道「该信几分」（输出可信度），也知道「该不该出手」（选择期望为正的格子）。把它们对立起来，制造了一个虚假的二分。

**② 论据用反了：R² 低不能证明不能赚钱**

v1 的论据是「个股日频收益可预测成分低，样本外 R² 仅 1%–5%」——这个论据恰好证明了相反的东西：

> **整个量化行业就是靠 1%–5% 的 R² 吃饭的。** 横截面因子、统计套利全部建立在「弱但稳定」的信号之上。

R² 衡量的是「解释了多少方差」，而交易要的是「期望收益是否为正」。一个 R²=2% 但逐期近似独立、样本外稳定的信号，一年交易 250 次，累积夏普可以很可观。拿 R² 去反驳「提升胜率可行」，是用错了尺子。

**③ 它本来就是"经验"的一部分**

> **用户原话**：提升预测能力也就是提升回测中的胜率，这也是用历史数据来预估未来的成功率，有一定的理论支撑，**其实这也是经验的一部分**。

这句话是对的。用历史数据预估未来成功率，就是经验的定义。说它「不可行」，等于说经验本身不可行——这是自我否定。

**修正后：真正该区分的是路径，不是目标**

| 路径 | 做法 | 可行性 | 说明 |
|---|---|---|---|
| **甲 · 内生预测** | 让 LLM 自由推理，端到端输出涨跌 / 目标价 | ❌ 不可行 | LLM 内部知识与标的未来收益之间**没有校准过的映射**；输出不可控、不可重复、无锚点 |
| **乙 · 条件概率估计** | 把历史收益标签按情境分格，估计 P(结果 \| 条件) | ✅ **可行** | **这就是经验本身**，也正是本方案六层体系在做的事 |
| **丙 · 参数寻优** | 在回测上调参提升样本内胜率 | ⚠️ 高风险 | 极易过拟合，必须 out-of-sample 复核（见 §15.3 规则二） |

> **本方案选的是路径乙，不是"因为甲不行而退而求其次"。**
> 校准也好、提升胜率也好，都是路径乙的自然产物。
>
> ⚠️ 但路径乙**不等于与机构在同一维度竞争**：服务对象是小资金的高信念低频决策，
> 成功标准随之改变（不是 RankIC / IC），详见 **§2.8**（§2.8.4 给出应遵循的指标体系）。

**④ 修正后必须补上的边界：期望收益 ≠ 胜率**

「提升预测能力 = 提升胜率」这个等式本身还需要收紧一步：

| 举例 | 胜率 | 盈亏比 | 期望 |
|---|---|---|---|
| 高胜率亏损系统 | 80% | 0.30 | **负** |
| 低胜率盈利系统 | 40% | 3.00 | **正** |

真正的优化目标是 **max E[扣成本后净收益]**，而不是 max hit_rate。

这一点 Part I §4.9 的 `exp_patterns` 表体现得很好——它同时记录了 `hit_rate`、`win_loss_ratio`、`avg_return`、`avg_excess`、`max_drawdown`，**说明设计者本来就清楚不能只看胜率**。修正后的目标应表述为：**在这些指标共同约束下选格子**，而不是挑 `hit_rate` 最高的格子。

**⑤ 这条修正把 Part I 与 Part II 扣得更紧**

既然目标确立为「可靠的条件概率估计（路径乙）」，那么成败就取决于：

| 条件概率估计的要求 | 对应 Part II |
|---|---|
| 格子里的样本够不够 | `n_eff`（§17.2） |
| 样本独不独立 | `icc_assumed`（§17.2） |
| 有没有偷看未来 | `available_at`（§16） |
| 分布会不会漂移 | `definition_ver` / 半衰期（§12.3、§14.1） |
| 样本不足怎么兜底 | shrinkage（§17.1） |

> 换言之：**Part II 不再是"服务于一个退而求其次的校准目标"，而是路径乙能否成立的决定性因素。**

### 2.5 关于"回测就够了"的精确回答（v2 修正）

> **用户质疑：如果目标是校准 / 提升成功率，回测就够了，Agent 不需要介入。**

这个质疑**部分成立**。v1 的回答（「Agent 不可替代的是非结构化信息的感知与经验积累」）过于笼统，这里给出精确版本。

**回测确实能做条件概率估计——但它只能在"已经被量化的维度"上做。**

回测引擎的条件维度是因子值、技术指标、市值、行业……都是**事先定义好、可计算、可枚举**的。

而 §2.7.2 论证过：中短线（T+1~T+20）的主导因素是**情绪、节奏、股性、题材生命周期**。这些东西的共同特点是：

- 连定义都不稳定（「题材第几天」谁说了算？）
- 无法写成因子表达式
- 只能从非结构化信息里感知（盘面语言、龙虎榜、公告措辞、板块连锁反应）

**所以 Agent 的不可替代性应精确表述为：**

> **回测负责在已量化的维度上做条件概率估计；**
> **Agent 负责把不可因子化的情境（情绪 / 节奏 / 题材阶段 / 股性）投影成可枚举的条件维度，**
> **从而扩展条件概率估计能够作用的特征空间。**

六层经验体系（§4）的本质就是这个**投影器**：把一次非结构化的市场观察，落成 L0–L5 六层可枚举的情境坐标。投影之后，条件概率估计才有的放矢。

这也解释了为什么本方案必须坚持两件事：

- **投影必须由确定性代码做，不能由 LLM 自由发挥**（§4.1）——否则同一情境会被投影到不同格子，条件概率无从累积
- **投影产物必须落成强类型列**（Part II §13）——否则 `GROUP BY` 做不了，分格子统计不了

### 2.6 最终定位：经验引擎

```text
量化做筛选（掐尖50-100只）
↓
经验做判断（这只票在当前环境下值不值得做）
↓
校准做修正（我这类判断历史上命中率多少，该下多重注）
↓
结果做沉淀（对了为什么对，错了为什么错，存进经验库）
```

**中短线（T+1~T+20）恰好是量化因子最弱、经验最强的地带。**

| 时间框架 | 主导因素 | 谁更强 |
|---------|---------|--------|
| 长线（年级别） | 基本面、估值 | 量化碾压 |
| T+0 | 微观结构、订单流 | 量化碾压 |
| **中短线（T+1~T+20）** | **情绪、节奏、股性、题材生命周期** | **经验 > 量化** |

### 2.7 定位论证：为什么中短线是经验的主场

> 本节为 v2 新增论证，**不构成对 §2.6 的修改，只是把它的成立理由展开**。
> 这是整个方案里最根本的一条判断——数据层（Part II）是手段，这段定位是目的。

#### 2.7.1 为什么是 T+1~T+20（三条理由，v2 修正）

> 原论证只给了「两端人类够不着」这一条（理由是人的通道），**不足以支撑这个周期选择**。完整的是三条：

| # | 理由 | 说明 |
|---|---|---|
| ① | **两端都够不着** | 长线超出人的信息处理能力（5000 只票 × 几十项财务指标）；T+0 超出人的反应能力（毫秒级来不及想）。中段是唯一落在人类感知与决策带宽内的区间 |
| ② | **机构的速度优势在此失效** | 持有 5 天，成交慢 100ms 对结果毫无影响（详见 §2.8.3） |
| ③ | **这是资金拉升行为的自然周期** ★ | **最根本的一条**，详见 §2.9.4：持仓期不该按"我的预测窗口"定义，而应按"拉升行情还有多长"定义——而题材/拉升的自然生命周期就是 T+1~T+20 |

> **理由③是 §2.9「跟随范式」的直接推论**，它比①②都更根本：
> **不是"我们能做什么"决定周期，而是"被跟随对象的行为周期"决定周期。**

#### 2.7.2 中段经验占优的四条独立理由

**① 驱动因素没有被因子化，也没有公认定义。**
「情绪温度」怎么量化？「题材处于第几天」谁说了算？连定义都不稳定，因子就无从构造——而因子化的前提恰恰是**可枚举、可重复的 X**。

**② 样本稀缺，且不是同分布。**
题材炒作是事件驱动的：AI 应用、低空经济、固态电池各有各的打法，它们不是同一个分布。能用来训练"题材生命周期模型"的样本可能只有几十轮。
**小样本 + 高维度 + 概念漂移**——恰好是机器学习最怕、人类类比推理最强的组合。

**③ 反身性最强。**
这一段的对手是其他短线交易者。量化模型假设历史分布稳定，但短线参与者的行为会因为"大家都在用同一套打法"而改变。
人类经验里天然包含「现在是谁在玩、他们习惯怎么打」这种**对手盘认知**——这不是任何历史数据统计得出来的。

**④ 信噪比低到连验证标尺都难立。**
这一段恰恰是最说不清"什么算一个好因子"的地带。与其强行拟合并承担过拟合风险，**不如做带判断的类比推理**。

#### 2.7.3 两条必须守住的边界

> 这两条不是否定 §2.6，而是让它在落地时不翻车。

**边界一：「经验占优」不等于「LLM 占优」——中间缺一环论证。**

人类专家的经验是有身体感知、盘感、真金白银亏损疼痛的。LLM 三样都没有，它有的是被压缩进参数里的公开文本。

缺的那一环是：**LLM 的经验必须外化成可检索的案例，不能指望它内化。** 老股民的经验长在脑子里，Agent 的经验必须长在库里。

> **推论：本方案的六层经验库不是这个定位的附属实现，它是这个定位能成立的唯一前提。**

**边界二：你把最难的一段，留给了最没有客观标尺的部分。**

长线有 DCF 可验证，T+0 有成交回报可验证，中短线的「节奏」「情绪」恰恰是最没有客观标尺的。所以这一段虽然经验占优，也是**最容易自我欺骗**的地带——每个教训都能编出一个合理的故事。

> **推论：T+N 真值回填（§8）、四个统计指纹（§8.1）、`available_at` 纪律（§16）不是工程的边角料，而是这个定位的命根子。没有它们，经验库会变成一个越来越自信的讲故事机器。**

#### 2.7.4 一条远期约束

若所有人的"经验"都来自同一个基础模型与相似的公开语料，经验会**同质化**。
真正稀缺的经验来自**你自己的成交与复盘记录**——

> **经验库必须由自己的实战样本喂养，不能靠通用知识。**

---

### 2.8 为什么不与机构正面竞争（v2 新增，战略约束）

> 用户指正（2026-10-09）：**ms 级响应、千亿资金规模，正面竞争没有实施的必要性。**
> 这条指正比 §2.4 的概念修正更根本——它不是逻辑问题，是**战略问题**。

**先承认上一轮回答的潜在误导**：§2.4 为了反驳"不可行"，指出「整个量化行业就是靠 1%–5% 的 R² 吃饭」。这个反驳在逻辑上成立，但它**隐含地建议了正面竞争**——而正面竞争是必输的。纠正一个逻辑错误时差点引入一个战略错误，此处予以制止。

#### 2.8.1 结论先行

> **这个项目的价值不在"比机构更准"，而在"做机构做不了的事"。**

这与本仓库定位完全一致（`AGENTS.md`：**A股量化交易系统，小资金快速复利**）。

#### 2.8.2 千亿规模既是优势也是枷锁

| 约束 | 对机构的后果 | 小资金的位置 |
|---|---|---|
| **策略容量** | 管理千亿时，一个容量 5 亿的策略贡献不到 0.5%，**不值得投入研发** | 而 A 股题材股、微盘股的容量普遍就是这么小 → **结构性留出的生态位** |
| **双十规定 / 集中度限制** | 必须分散持仓，无法集中火力 | 可以全仓 3–5 只票 |
| **受限名单 / ST 禁买 / 合规清单** | 很多标的技术上就不能碰 | 可以买 |
| **相对收益考核（vs 沪深300）** | 激励上被迫抱团，行为被约束，做不了真正独立的判断 | 考核绝对收益，不必抱团 |

**个人小资金在每一条上都站在对面。** 这不是"以弱胜强"，是**选了一条他们因为自身结构而无法进入的赛道**。

#### 2.8.3 ms 级响应在 T+1~T+20 是无效资产

机构的 alpha 很大一部分来自速度。但速度优势**只在它被需要的地方才成立**：

| 周期 | 速度是否重要 |
|---|---|
| 日内 / T+0 | ✅ 决定成败 |
| **T+1~T+20** | ❌ **持有 5 天，成交慢 100ms 对结果没有任何影响** |

> 这是 §2.7 把定位锁在 T+1~T+20 的**第二个理由**——第一个是人够得着（§2.7.1），
> 第二个是**机构的核心优势在这个周期上是用不上的**。

#### 2.8.4 成功标准必须跟着换 —— 这才是关键的一条

⚠️ **这里暴露出文档内部一处真实的、此前未被发现的矛盾：**

§2.1 写着「给出未来5日收益率的排序 → 可用 **RankIC** 验证」。

但 **RankIC 是机构游戏的指标**：

| RankIC 隐含的成功图像 | 小资金定位（AGENTS.md）的真实图像 |
|---|---|
| 给 5000 只票排序的能力 | 这 3–5 只票我有没有把握 |
| 系统性、高换手、靠概率优势摊出来 | 一年 20–50 笔，低频、高信念、集中持仓 |

**这两套成功标准是不同的，混用会导致系统优化错方向**——你会为了提升全市场排序能力，去做一堆对集中持仓毫无帮助的事。

**建议：本方案放弃 RankIC 作为主指标**（可作为诊断参考保留），改用与小资金定位一致的指标：

| 指标 | 为什么适合 |
|---|---|
| **期望净收益 E[ret_net]** | 扣成本后这才是真的（§13.3）；直接对应"复利"目标 |
| **单笔质量分布** | 低频决策下，分布的形状比均值重要——一两次巨亏就毁掉复利 |
| **Calmar / 最大回撤** | 小资金复利的敌人是回撤，不是波动率 |
| **胜率 × 盈亏比组合** | 避免 §2.4④ 的"高胜率亏损"陷阱 |

#### 2.8.5 这反过来定义了经验库的真正角色

| | 机构 | 本方案 |
|---|---|---|
| 样本来源 | 千万次交易 | **自己的几十次判断** |
| 提取什么 | 统计优势 | **教训** |
| 主要风险 | 过拟合 | **自我欺骗** |

> **正因为样本量小到无法做任何统计推断，才必须：① 极度珍惜每一个样本；② 用真值硬约束，防止编造解释。**

这条推论极其重要，它把 Part II 的各项工作重新排序：

| 机制 | 在小资金定位下的意义 |
|---|---|
| T+N 真值回填（§8） | **唯一的学习来源**，没有第二条路 |
| 四个统计指纹（§8.1） | **唯一的防欺骗手段**——样本太少，每个教训都能编出合理解释 |
| shrinkage（§17.1） | 小样本的常规状态，不是边缘情况 |
| `available_at`（§16） | 样本已经这么少，再掺入前视偏差就彻底废了 |

> **这些不是工程的边角料。在写的这一个文档里，它们是"小样本 + 高信念 + 低频"这种模式下，唯一能阻止自我欺骗的机制。**

#### 2.8.6 一句话总结

> 本方案**不与机构在"预测精度"上竞争**——那条路上有 ms 级响应和千亿资金，正面竞争没有实施的必要性。
>
> 本方案做的是：**在小容量、T+1~T+20、机构速度优势失效且合规不许进入的地带，
> 用累积的经验库提升每一笔高信念决策的判断质量。**
>
> 这条路的衡量标准不是 IC，而是**扣成本后的期望净收益与回撤控制**。

---

### 2.9 跟随范式：alpha 来自识别专业资金的行为（v2 新增）

> 用户洞察（2026-10-09）：**小资金的灵活性、趋势跟随性是专业团队无法做到的；他们的资金可以强势砸跌停或拉涨停。小资金的目的是跟随专业团队，吃拉升阶段的红利。**
>
> 这条洞察比 §2.8 更精确。§2.8 的结论是"不与机构正面竞争"（防守性），本节是更强也更根本的版本：
> **机构不是要避开或竞争的对手，机构本身就是 alpha 的来源。**

#### 2.9.1 一个范式转变

| | 预测范式（本文档此前的默认） | **跟随范式（本节确立）** |
|---|---|---|
| alpha 来源 | 用特征预测未来收益 | **识别专业资金正在做什么，然后跟随** |
| 要回答的问题 | "这只票会涨吗？" | "**有资金在拉吗？拉到哪个阶段了？我能不能跟上？**" |
| 参与者的角色 | 所有人试图预测同一个外部随机变量 | **被跟随者的行为本身在创造收益**——你搭的是他造的车 |
| 信噪比 | 极低（要预测的东西无人能控） | **高得多**（观测目标是已发生的事实行为，不是未来） |

**为什么重要**：预测一个外部随机变量，只能解释 1%–5% 的方差；而识别一个**正在发生的行为**，做的不是预测而是**检测**——这是两类难度完全不同的问题。

#### 2.9.2 为什么大资金恰恰做不了这件事

最关键的对称性：**在同一局游戏里，他们的核心资产变成了核心负债。**

| 维度 | 专业 / 大资金 | 小资金 |
|---|---|---|
| **容量** | 拉升标的多为中小盘题材股，容量小；大资金买不进也出不掉 | 可全额参与 |
| **身份冲突** | 大资金自己是"制造者"，无法同时是"跟随者"——**其进入本身就在改变局面**（反身性） | 体量小到进入不产生扰动 |
| **冲击成本** | 建仓/平仓本身会砸盘或拉盘，进出有巨大成本 | 进出近乎无成本 |
| **掉头速度** | 拉升可能 T+1 就结束，大资金快速撤离困难；内部流程与合规进一步拖慢 | 次日甚至当日可以走 |
| **止损纪律** | 无条件清仓会引发冲击成本、合规、内部报告链条 | 可按纪律机械执行 |

> **结论：这不是"以弱胜强"，而是选了一条他们因自身结构（规模）而无法进入的赛道。**

#### 2.9.3 claims 该记录什么变了（重要）

跟随范式下，**判断对错不再只是"股票涨没涨"**：

| claim 类型 | 问的问题 | 可验证手段 |
|---|---|---|
| direction / magnitude / level | 这只票会涨吗、涨多少 | T+N 真值（已有，§8） |
| **behavior（资金行为）** ★ | **有主力资金在介入吗？** | 龙虎榜、大宗交易、席位、资金流向、盘口、股东户数变化 |
| **stage（阶段）** ★ | **现在处于拉升的哪个阶段？** | — **这正是 §4.5 L2 的 `theme_stage`（1启动/2发酵/3高潮/4分歧/5退潮）** |
| **presence（还在不在）** ★ | **主力还在里面，还是已经在撤？** | 龙虎榜后续、量能结构、破位行为 |

> **重大发现**：原方案里 `theme_stage` 只是 L2 的一个描述性字段；
> 在跟随范式下，它从"一个维度"升级为**最核心的判断标的**。

#### 2.9.4 `horizon_days` 的含义变了（并给出 T+1~T+20 的第三个理由）

跟随范式下，持有期**不是"我的预测窗口"**，而是"**拉升阶段还剩多长**"：

```text
原来：我要预测未来 N 天的涨跌 → N 取多少取决于"我能预测多远"
修正：这个阶段还有 N 天结束 → N 取决于被跟随对象的行为周期
```

> **这才是 T+1~T+20 的真正来源**——见 §2.7.1 理由③：
> 不是"人够得着"，也不是"速度优势失效"，而是**题材与拉升行情的自然生命周期就是这么长**。
> 这是由被跟随对象决定的客观量，不是我们主观选的。

#### 2.9.5 `realizable` 的定位必须改变（推翻 §13.2 的原结论）

⚠️ 本节推翻本文档的前序结论：

§13.2 原把"能否成交"当**过滤器**——"涨停/一字板买不进，不该判为命中，要排除以免污染 `hit_rate`"。
**在跟随范式下这个处理是错的**：

- **追涨 / 打板恰恰是跟随策略最核心的入场场景**——拉升启动的信号就是涨停
- 若把所有"买不进"的样本排除，会**丢掉整个策略最有代表性的场景**，剩下的样本反而不具代表性
- 更关键：**能否成交不是随机噪声，而是可通过改进执行提高的技能**（扫单时机、排队位置、预埋单、竞价参与度）。当成噪声丢掉，等于丢掉一门手艺

**修正后的处理**（已落实到 §13.2）：用 `fill_status` 状态枚举替代 BOOLEAN，
**不过滤而分组统计**，并把 `fill_rate` 列为与 `hit_rate` 并列的核心指标。

#### 2.9.6 数据金字塔应重排（修正 §2.2）

§2.2 把「资金流 / 北向 / 融资余额 / 龙虎榜」放在第二层。跟随范式下：

| 层 | 预测范式下的地位 | **跟随范式下的地位** |
|---|---|---|
| 政策 / 监管信号 | 最强 alpha | 仍是背景（题材的起因） |
| **资金流 / 龙虎榜 / 席位 / 大宗** | 第二层，普通弱特征 | ★ **核心特征**——它们直接观测的就是被跟随对象的行为 |
| 财报 / 公告 / 调研 | 第三层 | 降为辅助（题材的载体，非起因） |
| 量价技术因子 | 底层 | 用于阶段识别与退出时点，而非预测 |

> 一句话：**预测范式下"资金指标是弱信号"，跟随范式下"它们是信号本身"。**

#### 2.9.7 三个必须守住的风险（防止跟随退化）

> 这套逻辑最大的风险不是不成立，而是**退化**。三条防线：

**风险一：胜负手不在"进"，在"出"。**
拉抬资金出货时，跟随者就是对手盘。大资金出货是分批的（数日至数周），理论上有逃生窗口；
失败的典型不是没跟上，而是**恋战没走**。
→ **L5 交易经验（§4.8）的战略权重大幅上升**：止损不是风控装饰，是这门手艺的核心技能。

**风险二：你的竞争对手不是机构，是其他跟随者。**
所有跟随者都在抢同一辆车，车还会到站。
→ edge 只能来自两处：**比其他跟随者更早识别**（Agent 的价值），或**更守纪律地退出**（代码严格执行的价值）。

**风险三：跟随极易退化成"追涨杀跌"。**
→ 必须靠**显式的阶段定义 + 显式的进出规则**约束。这正是 §4.5 `theme_stage`（1–5）的价值：
**它把"追涨"变成一个有定义、可回测的动作，而不是一种情绪。**

#### 2.9.8 一句话总结

> 本文档此前把定位写成「Agent 是有经验的老股民，独立判断这只票值不值得做」。
> 修正为：**Agent 是资金行为的解读者——识别专业资金的意图与阶段，在正确的阶段跟随，
> 并比他人更早识别、更守纪律地退出。**
>
> 这不改变"Agent 不预测涨跌"的结论，**而是给出了它不预测的真实原因**：
> **因为要做的不是预测未来，是检测正在发生的事。**

---

## 3. "老股民"与"分析师"的统一

两者不冲突，而是互为增强：

> 经验给假设，分析给验证。

| 能力 | "老股民" | "分析师" | 系统状态 |
|------|---------|---------|---------|
| 模式触发 | 见过类似的，警觉起来 | — | 🆕 需建 |
| 结构化分析 | — | 拆维度、逐项检查 | ✅ 已有 |
| 信息覆盖 | 凭经验知道该看哪几个面 | 确保不遗漏、不偏食 | ✅ 已有 |
| 事实锚定 | — | 数字可溯源 | ✅ 已有 |
| 自我认知 | 大概几成把握 | 精确到条件命中率 | 🔄 需校准 |
| 直觉修正 | "不对劲，先撤" | 解释哪里不对劲 | 🆕 需建 |

### 3.1 目标形态示例

```text
用户："帮我看看XX科技"
│
▼ 【老股民】经验检索
→ "底部放量+题材启动第二天+龙虎榜机构净买"
→ 历史相似案例 15 个，12 赚 3 亏
→ 3 个亏的都是"板块没跟"的情况
│
▼ 【分析师】结构化验证
→ 板块联动度 ✅ / 量能结构 ✅ / 资金流 ✅ / 公告异动无 ⚠️
→ 发现板块联动度偏弱，触发反向证据检查
│
▼ 【合成判断】
→ 方向：偏多，但板块联动弱是主要风险
→ 置信度：0.55（经验说 0.7，但反向信号调低）
→ 建议：若 T+1 板块启动确认则可做，否则观望
→ 留档：situational 特征 + 判断 + 置信度 → 等 T+N 回填
```

---

## 4. 六层经验体系（决策链映射）

> 这一节就是 §2.7.3 边界一所指的「外化载体」：经验不长在模型里，长在这六层结构里。

```text
┌─────────────────────────────────────────────────────┐
│ L0 市场经验：现在能不能做？该做多重？ │
│ → 大盘状态 / 量能 / 情绪温度 / 季节性 │
├─────────────────────────────────────────────────────┤
│ L1 板块经验：该做哪个方向？ │
│ → 轮动规律 / 联动度 / 板块生命周期 │
├─────────────────────────────────────────────────────┤
│ L2 题材经验：这个题材还能不能跟？ │
│ → 题材阶段 / 扩散规律 / 龙头-跟风关系 │
├─────────────────────────────────────────────────────┤
│ L3 个股经验：这只票具体怎么搞？ │
│ → 股性 / 形态规律 / 历史相似案例 │
├─────────────────────────────────────────────────────┤
│ L4 策略经验：用什么方法切入？ │
│ → 低吸 vs 追涨 / 什么 regime 用什么方法 │
├─────────────────────────────────────────────────────┤
│ L5 交易经验：怎么控节奏？ │
│ → 仓位 / 买卖点 / 止损止盈 / 加减仓时机 │
└─────────────────────────────────────────────────────┘
```

**每层经验都有自己的条件-结果对，检索时按层独立查，最终由 Agent 综合。**

### 4.1 核心原则

> **别存原始问答，存"投影后的案例"。**
> 写入时做投影（确定性代码做），检索时先过滤再排序，输出时压缩成摘要。

```text
原始问答（噪声大，不可直接检索）
↓ 投影（确定性代码做，不用LLM）
结构化案例（可过滤、可检索、可统计）
```

### 4.2 统一案例表

**v2 增强版见 §13，此处为 v1 原文。**

```sql
CREATE TABLE exp_cases (
    id              BIGSERIAL PRIMARY KEY,
    layer           VARCHAR(20) NOT NULL,    -- market/sector/theme/stock/strategy/trading
    entity_id       VARCHAR(50),             -- 板块代码/题材名/个股代码/策略名

    -- 情境层（用于过滤）
    situation       JSONB NOT NULL,          -- 各层不同的条件特征

    -- 语义层（用于相似度）
    situation_text  TEXT,                    -- 模板化生成（确定性）
    embedding       VECTOR(768),             -- BGE 向量

    -- 判断层
    judgment        JSONB NOT NULL,

    -- 结果层（T+N 回填）
    outcome         JSONB,
    verified_at     DATE,

    created_at      TIMESTAMP DEFAULT now(),
    source          VARCHAR(20)              -- agent/auto/manual
);

CREATE INDEX ON exp_cases (layer, created_at DESC);
CREATE INDEX ON exp_cases (layer, entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX ON exp_cases USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 4.3 L0 市场经验

```json
{
    "layer": "market",
    "situation": {
        "index_trend": "震荡/上行/下行",
        "volume_regime": "放量/缩量/平量",
        "sentiment_temp": "冰点/低迷/中性/亢奋/过热",
        "northbound_flow": "大幅流入/小幅流入/平衡/流出",
        "margin_balance_trend": "上升/平稳/下降",
        "calendar_event": "财报季/春节前/季末/无",
        "policy_signal": "宽松/中性/收紧",
        "pattern": "底部企稳/高位放量/破位/横盘"
    },
    "judgment": {
        "action": "进攻/均衡/防守/空仓",
        "position_range": [0.3, 0.7],
        "confidence": 0.75
    }
}
```

示例经验："两市缩量到 7000 亿以下 + 涨停数 < 40 + 北向流出 → 三天内出大阳线概率 25%，仓位建议 ≤ 3 成"

### 4.4 L1 板块经验

```json
{
    "layer": "sector",
    "situation": {
        "sector": "半导体",
        "sector_rotation_day": 3,
        "sector_linkage": 0.65,
        "sector_volume_ratio": 1.8,
        "sector_vs_market": "领涨/跟涨/领跌/抗跌",
        "leader_status": "龙头涨停/龙头炸板/龙头调整",
        "breadth": 0.72,
        "policy_support": true
    }
}
```

示例经验："板块轮动第 3 天 + 龙头炸板 + 跟风股大面积冲高回落 → 第 4 天补跌概率 70%"

### 4.5 L2 题材经验

```json
{
    "layer": "theme",
    "situation": {
        "theme": "AI应用",
        "theme_stage": 2,
        "days_since_start": 4,
        "leader_gain": 0.35,
        "spread_to_followers": true,
        "news_catalyst": "政策",
        "market_attention": 0.8
    }
}
```

字段说明：`theme_stage` 取值 1启动 / 2发酵 / 3高潮 / 4分歧 / 5退潮。

示例经验："题材第 4 天以上 + 龙头涨幅 >30% + 全市场都在讨论 → 退潮概率 75%"

### 4.6 L3 个股经验

```json
{
    "layer": "stock",
    "situation": {
        "symbol": "600XXX",
        "sector": "半导体",
        "theme": "AI应用",
        "regime": "震荡",
        "stock_pattern": "底部放量突破",
        "stock_personality": {
            "volatility_rank": 0.8,
            "sector_follow": 0.9,
            "news_sensitivity": 0.6,
            "limit_up_momentum": 0.7,
            "liquidity_score": 0.65,
            "rebound_habit": "60日线",
            "auction_pattern": "高开低走",
            "turnover_cycle": "3天放量一次"
        },
        "special_signals": ["龙虎榜机构净买", "底部缩量企稳"]
    }
}
```

示例经验："这只票涨停后次日溢价均值 +2.1%，炸板后次日平均 -3.2%"

### 4.7 L4 策略经验

```json
{
    "layer": "strategy",
    "situation": {
        "regime": "震荡",
        "theme_stage": 2,
        "stock_position": "底部启动",
        "market_sentiment": "中性"
    },
    "judgment": {
        "best_approach": "低吸",
        "entry_timing": "尾盘或次日竞价低开",
        "stop_loss": "-3%",
        "take_profit": "+6%",
        "hold_days": 3
    }
}
```

示例经验："震荡市 + 题材中期 + 底部启动形态 → 低吸胜率 62%，追涨胜率 38%"

### 4.8 L5 交易经验

```json
{
    "layer": "trading",
    "situation": {
        "portfolio_state": "首次建仓/已有浮盈/已有浮亏",
        "market_regime": "震荡",
        "consecutive_wins": 3,
        "drawdown_current": -0.02
    },
    "judgment": {
        "position_size": 0.3,
        "add_or_reduce": "减仓",
        "stop_rule": "跌破5日线无条件走",
        "max_hold_days": 5
    }
}
```

示例经验："连续 3 笔盈利后 → 历史统计上第 4 笔亏损率反而升高到 55%"

### 4.9 经验模式表（从案例提炼）

```sql
CREATE TABLE exp_patterns (
    id                  BIGSERIAL PRIMARY KEY,
    layer               VARCHAR(20),
    conditions          JSONB,           -- 条件组合
    sample_count        INT,
    hit_rate            FLOAT,
    avg_return          FLOAT,
    avg_excess          FLOAT,
    max_drawdown        FLOAT,
    win_loss_ratio      FLOAT,
    winner_traits       TEXT[],          -- 赚钱案例共同特征
    loser_traits        TEXT[],          -- 亏钱案例共同特征
    key_differentiator  TEXT,            -- 区分胜负的关键变量
    regime_valid        VARCHAR(20)[],
    decay_factor        FLOAT DEFAULT 1.0,
    updated_at          TIMESTAMP
);
```

### 4.10 置信度校准表

```sql
CREATE TABLE exp_calibration (
    pattern_type    VARCHAR(50),
    direction       VARCHAR(10),
    claimed_conf    FLOAT,
    actual_hit_rate FLOAT,
    sample_count    INT,
    calibrated_conf FLOAT,
    updated_at      TIMESTAMP
);
```

---

## 5. 案例怎么产生

```text
Agent 完成一次分析
  │
  ▼
finish_collector 提取案例（代码做，不用LLM）
  ├─ situation 特征 ← resolver + 工具输出 + 量化指标
  ├─ situation_text ← 模板化生成（确定性）
  ├─ embedding ← BGE 编码
  ├─ judgment ← Agent 的结论
  └─ outcome ← 暂空，T+N 回填
  │
  ▼
写入 exp_cases
  │
  ▼
（T+N 后）evaluator 回填 outcome
  │
  ▼
定期任务：从 exp_cases 提炼 → exp_patterns + exp_calibration
```

`situation_text` 必须模板化生成：

```python
def build_situation_text(case):
    return (
        f"{case.regime}市|{case.sector}板块|"
        f"个股形态:{case.stock_pattern}|"
        f"题材阶段:{case.theme_stage}期|"
        f"信号:{','.join(case.special_signals)}|"
        f"板块联动度:{case.sector_linkage:.2f}"
    )
```

---

## 6. 检索：两阶段混合检索

```text
新任务
  │
  ▼ 【Step 1: 特征提取（确定性代码，0ms）】
  提取当前情境 → sector/regime/stock_pattern/theme_stage/...
  │
  ▼ 【Step 2: 结构化硬过滤（SQL，~5ms）】
  WHERE layer IN (...) AND regime=... AND sector IN (...)
    AND outcome_verified_at IS NOT NULL
  → 候选池 200-500 条
  │
  ▼ 【Step 3: 向量语义排序（pgvector，~20ms）】
  situation_embedding <=> query_embedding
  → 取 Top-20
  │
  ▼ 【Step 4: 二次排序（代码，~1ms）】
  score = similarity × 0.4
        + recency_decay × 0.2
        + hit_rate_weight × 0.2
        + regime_match × 0.2
  → 最终 Top-8~12 条
```

### 6.1 跨层组合检索

Agent 分析一只票时按决策链逐层查：

```text
用户："帮我看看XX科技"
  │
  ▼ 【L0 市场】"震荡市+缩量+情绪低迷 → 仓位 ≤5成，低吸为主"
  ▼ 【L1 板块】"板块轮动第3天，联动度0.55 → 仍有空间但不追高"
  ▼ 【L2 题材】"题材第2天，龙头首板，扩散初期 → 可跟"
  ▼ 【L3 个股】"底部放量突破，回调习惯位60日线"
  ▼ 【L4 策略】"震荡+题材早中期+底部启动 → 低吸胜率62%"
  ▼ 【L5 交易】"首次建仓3成，止损-3%，持有3-5天"
  │
  ▼ 【合成判断】
  → 方向：偏多  置信度：0.62
  → 策略：低吸  仓位：3成
  → 风控：-3%止损，T+5强制离场
  → 依据：L0保守+L1中性+L2积极+L3有利+L4胜率62%
```

---

## 7. 经验摘要压缩（Experience Digest）

检索到 N 条案例后，不把原文丢给 Agent，而是压缩成统计摘要（~500字符）：

```python
def build_experience_digest(query_situation, cases):
    winners = [c for c in cases if c.outcome_hit]
    losers  = [c for c in cases if not c.outcome_hit]

    return f"""
【历史经验摘要】（检索到 {len(cases)} 个相似案例）
📊 统计：命中 {len(winners)}/{len(cases)}（{len(winners)/len(cases):.0%}），
        平均收益 {mean(c.outcome_return):+.1f}%，平均超额 {mean(c.outcome_excess):+.1f}%

✅ 赚钱案例共性：{common_traits(winners)}
❌ 亏钱案例共性：{common_traits(losers)}
🔑 关键区分变量：{key_differentiator(winners, losers)}
⚠️ 当前情境 vs 历史的差异：{diff_from_history(query_situation, cases)}
"""
```

输出示例：

```text
【历史经验摘要】（检索到 12 个相似案例）
📊 统计：命中 8/12（67%），平均收益 +3.2%，平均超额 +2.1%

✅ 赚钱案例共性：板块联动度强、题材处于启动期、龙虎榜有机构净买
❌ 亏钱案例共性：板块联动弱、个股涨幅已超15%、次日缩量

🔑 关键区分变量：板块联动度是主要分水岭
   联动度>0.6 时胜率 82%，<0.4 时胜率 31%

⚠️ 当前情境与历史差异：
   当前板块联动度 0.55（中等偏弱），历史上该值区间胜率约 55%
```

500 字符 = 12 条案例的全部精华。

---

## 8. 校准闭环：从经验中提高命中率

```text
         ┌──────────────────────────────┐
         │                              │
         ▼                              │
   经验检索 → 判断 → 校准置信度 → 行动   │
                │                       │
                ▼                       │
           T+N 真值回填                 │
                │                       │
                ▼                       │
        ┌───────────────┐               │
        │ 经验库更新     │───────────────┘
        │ · 新案例入库   │
        │ · 旧模式修正   │
        │ · 置信度校准   │
        │ · 失败模式提取 │
        └───────────────┘
```

置信度校准示例：

| 经验说 | 历史实际命中 | 校准后输出 |
|--------|-------------|-----------|
| "看多，confidence 0.8" | 该模式命中率 65% | 修正为 0.65 |
| "看空，confidence 0.6" | 该模式命中率 72% | 修正为 0.72 |

### 8.1 必须先建的四个统计指纹（防止经验库本身退化）

> §2.7.3 边界二指出：中段最没标尺，也最容易自我欺骗。这四个指纹就是唯一的防线。

| 指标 | 定义 | 异常信号 |
|------|------|---------|
| 可证伪率 | claims 数 / 有实质结论的答复数 | 下降 = 在变滑头 |
| undecidable 占比 | 判定为不可判定的比例 | 上升 = 在变滑头 |
| 出声率 | 给出明确结论的答复占比 | 下降 = 保守化退化 |
| 反向证据率 | 给出方向结论时调用反向工具的比例 | 下降 = cherry-picking |

---

## 9. 各层经验的沉淀速度与冷启动

| 层级 | 积累速度 | 冷启动方案 | 需要样本 |
|------|---------|-----------|---------|
| L0 市场 | 慢 | 手工录入历史大事件 | 50-100 条 |
| L1 板块 | 中 | 回测引擎批量生成 | 200-500 条/板块 |
| L2 题材 | 中 | 历史题材复盘 | 100-300 条/题材 |
| L3 个股 | 快 | Agent 运行自动积累 | 1000+ 开始有效 |
| L4 策略 | 慢 | 回测引擎 AB 对比 | 200-500 条 |
| L5 交易 | 中 | 手工 + 交易记录 | 300+ 条 |

L4 策略经验天然适合回测引擎生成——同一情境下"低吸 vs 追涨"的胜率对比，回测跑一遍就有。这是回测引擎和经验库的最佳衔接点。

---
---

# Part II · 数据层（v2 新增）

> **定位声明**：Part II 不是替代 Part I，是**让 Part I 跑得起来**。
> Part I 定义了「存什么、怎么检索、怎么校准」；Part II 只解决一件事——**让这些操作在百万级规模下依然正确且够快**。
> **所有 Part I 的表名一律保留**（`exp_cases` / `exp_patterns` / `exp_calibration`），采用**原地 ALTER 升级**，不重建、不改名、不 ETL 到新表。

---

## 10. 为什么数据层成了瓶颈

Part I 的每一条流程都隐含三个前提：**可过滤、point-in-time 正确、可重放**。当前 schema 三条都不满足。

### 10.1 问题清单

| # | 问题 | 后果 |
|---|---|---|
| **1** | **`situation` JSONB 万能袋** | §6 Step 2 号称「结构化硬过滤 ~5ms」，但 `regime`/`sector` 埋在 JSONB 里，实际退化为全表扫；无法 GROUP BY 聚合提炼 `exp_patterns`；写错字段名不报错 |
| **2** | **`entity_id` 多态（板块/题材/个股/策略混一列）** | 无法 JOIN、无法做层级回退；「AI应用」/「AI 应用」会被 `GROUP BY` 分成两组 |
| **3** | **缺交易日历** | 「T+5」的 5 是交易日还是自然日？没有 `dim_trade_date`，horizon 计算可能跨节假日错位 |
| **4** | **三个时间戳混为一谈** | §6 Step 2 的 `WHERE outcome_verified_at IS NOT NULL` **本身就是前视偏差**：数据当天盘后才落库，回测时却已可见 |
| **5** | **复权口径漂移** | 今天回看三个月前的收盘价与当时不同（前复权基准随除权变动），收益值本身会变，却无版本记录 |
| **6** | `exp_patterns` **无统计窗** | `hit_rate=0.62` 不知基于哪段样本；`winner_traits TEXT[]` 不可查询，沦为装饰；旧模式永不淘汰 |
| **7** | **无配置版本** | 案例不记录当时生效的工具面/阈值/prompt 版本 → §8「改前 vs 改后」无法对齐 → 验证在数据上不成立 |
| **8** | ivfflat `lists=100` | 需预聚类、数据漂移后索引劣化且必须重建；结构化过滤 + 向量排序存在 post-filter 两难 |
| **9** | `embedding VECTOR(768)` 与主表同行 | 大列显著拖慢顺序扫描 |
| **10** | `UPDATE` 回填 outcome | 历史不可重放，无法回答「当时到底做了什么判断」 |

> **问题 1 与 Part I §4.1 直接冲突**：Part I 明确要求「写入时做投影（**确定性代码做**）」——投影产物应是**封闭可枚举**的特征向量，而 JSONB 放弃了这份纪律。这是 v1 内部的一处不自洽，v2 补上，**不改变「要做投影」这条原则本身**。

### 10.2 三个时间戳（务必分清）

| 时刻 | 含义 | Part I 对应 |
|---|---|---|
| `created_at` | 判断做出的时刻 | 已有 ✅ |
| `due_date` | T+N 到期的**交易日** | **缺失** ❌ |
| `available_at` | **标签可安全查询的时刻** | **缺失** ❌ |

```sql
-- ❌ Part I §6 Step 2 的写法，有前视偏差
WHERE outcome_verified_at IS NOT NULL

-- ✅ v2 纪律
WHERE available_at <= :asof_ts
```

---

## 11. 设计原则

> 以下八条只约束**存储实现**，不改动 Part I 的任何策略约定。

1. **可过滤即列**：凡参与 `WHERE`/`GROUP BY`/连接的字段必须是强类型列；JSONB 只放不参与过滤的描述字段
   - 不是废除 JSONB，而是给 §4.3–§4.8 各层 JSON 一个**受控的存放位置**：关键维度提升为列，其余留在 `situation`
2. **时间三分离**：`created_at` / `due_date` / `available_at`
3. **追加为主**：`judgment` 写入后不可修改；修正走新版本行
4. **维度外置**：股票/板块/题材/regime/交易日历维度表化，`exp_cases` 只存 id
5. **回退链显式**：层级关系用闭包表固化，不做运行时递归
6. **大列分离**：embedding 拆到侧表
7. **配置版本化**：每条 case 带 `config_rev`
8. **统计带窗**：`exp_patterns` 每行记录 `window_from/window_to`，永不 UPDATE，只 deprecate

**向后兼容承诺**：现有 `situation` JSONB 字段保留不动，所有增强均为**新增列**（可为 NULL），Part I 的读写逻辑无需改写即可继续运行。

---

## 12. 新增维度表（纯新增，零风险）

### 12.1 交易日历 —— T+N 的地基

```sql
CREATE TABLE dim_trade_date (
    trade_date   DATE PRIMARY KEY,
    seq          INT  NOT NULL,        -- 交易日序号，T+N 直接 seq+N
    is_half_day  BOOLEAN DEFAULT false,
    next_date    DATE,                 -- 预计算，避免自连接
    prev_date    DATE
);
CREATE INDEX ON dim_trade_date (seq);
```

> `domain_registry.py:45` 的 `trading_calendar` 当前默认 `False`，接入时需确认 finance SPEC 是否启用。

### 12.2 标的维度

```sql
CREATE TABLE dim_security (
    symbol           VARCHAR(16) PRIMARY KEY,
    name             VARCHAR(64) NOT NULL,
    market           VARCHAR(8)  NOT NULL,   -- SH/SZ/BJ
    board            VARCHAR(16),            -- 主板/创业板/科创板/北交所
    list_date        DATE,
    delist_date      DATE,                   -- NULL = 在市
    is_st            BOOLEAN DEFAULT false,
    cap_bucket       SMALLINT,               -- 市值档 1..5
    liquidity_bucket SMALLINT,               -- 流动性档 1..5
    updated_at       TIMESTAMP DEFAULT now()
);
```

板块与题材归属随时间变化（重组、转板、蹭概念），因此不作为标的静态属性——见 §12.4 的有效期闭包表。

### 12.3 市场状态维度 —— 可重算

Part I §4.3 的 L0 把 `index_trend`/`volume_regime`/`sentiment_temp` 等散落在 JSONB 里。这里提升为**逐日事实**，好处是 regime 定义迭代时可**全量重算历史**：

```sql
CREATE TABLE dim_regime (
    regime_id       SMALLINT PRIMARY KEY,
    label           VARCHAR(16) NOT NULL,   -- bull/range/bear
    definition_ver  VARCHAR(16) NOT NULL    -- ★ 定义版本
);

CREATE TABLE fct_market_regime (
    trade_date      DATE PRIMARY KEY REFERENCES dim_trade_date,
    regime_id       SMALLINT NOT NULL REFERENCES dim_regime,
    trend           SMALLINT,               -- -1/0/1
    vol_bucket      SMALLINT,               -- 1缩量/2平量/3放量
    sentiment_score NUMERIC(5,2),
    zt_count        INT,                    -- 涨停家数
    dt_count        INT,
    broken_rate     NUMERIC(5,4),
    total_amount    BIGINT,
    index_close     NUMERIC(10,2),
    metrics_ext     JSONB,
    computed_by     VARCHAR(16) NOT NULL    -- 计算口径版本
);
```

> `mood_regime` 在 `skills/market_screener/common.py:294` 已有实现（对应 Part I L0 的 `sentiment_temp`），但只在选股技能内、未接入追责与校准。这张表是把它提升为一等公民的地方。

### 12.4 层级闭包表 —— 回退链载体

```sql
CREATE TABLE dim_entity_hierarchy (
    ancestor_type    VARCHAR(16) NOT NULL,  -- market/sector/theme/stock
    ancestor_id      VARCHAR(32) NOT NULL,
    descendant_type  VARCHAR(16) NOT NULL,
    descendant_id    VARCHAR(32) NOT NULL,
    depth            SMALLINT    NOT NULL,
    valid_from       DATE        NOT NULL,
    valid_to         DATE,                  -- NULL = 仍有效
    PRIMARY KEY (ancestor_type, ancestor_id, descendant_type, descendant_id, valid_from)
);
CREATE INDEX ON dim_entity_hierarchy (descendant_type, descendant_id, valid_from, valid_to);
```

**为什么要有 `valid_from/valid_to`**：一只票三个月前属于「AI应用」题材，现在是「算力租赁」。回退链必须按**当时的日期**走，否则 L3 个股经验会向错误的父层收缩。

---

## 13. `exp_cases` 增强（原地 ALTER，不改名）

```sql
-- 全部为新增列，可为 NULL ⇒ 不破坏 Part I 现有读写
ALTER TABLE exp_cases
    -- 时间三分离
    ADD COLUMN trade_date   DATE,                  -- → dim_trade_date
    ADD COLUMN due_date     DATE,                  -- T+N 到期交易日
    ADD COLUMN available_at TIMESTAMPTZ,           -- ★ 标签可查时刻

    -- 提升为强类型的关键情境维度（原在 situation JSONB 内）
    ADD COLUMN regime_id      SMALLINT,            -- → dim_regime
    ADD COLUMN theme_stage    SMALLINT,            -- L2 的 1..5
    ADD COLUMN horizon_days   SMALLINT,
    ADD COLUMN strategy_key   VARCHAR(32),         -- L4，如 dragon/g56/knife_catch
    ADD COLUMN claim_type     SMALLINT,            -- 1 direction/2 magnitude/3 level
    ADD COLUMN claim_direction SMALLINT,           -- -1/0/1
    ADD COLUMN claim_score    SMALLINT,            -- 0..100

    -- 两个新机制
    ADD COLUMN evidence_mask INT DEFAULT 0,        -- ★ 位图，见 13.1
    ADD COLUMN realizable    BOOLEAN DEFAULT true, -- ★ 当日可否成交

    -- 溯源
    ADD COLUMN config_rev    VARCHAR(32),          -- → exp_config_snapshot
    ADD COLUMN version       SMALLINT DEFAULT 1,
    ADD COLUMN superseded_by BIGINT;

CREATE INDEX ON exp_cases (layer, entity_id, trade_date DESC);
CREATE INDEX ON exp_cases (regime_id, layer, trade_date DESC);
CREATE INDEX ON exp_cases (theme_stage, layer) WHERE theme_stage IS NOT NULL;
```

`entity_id` **保持原样不做多态改造**——通过 §12 维度表 + 应用层前缀约定（`sec:`/`thm:`/`stk:`/`str:`）消歧，不必改动现有数据。

### 13.1 `evidence_mask` 位图 —— 让 Part I §8.1 的「反向证据率」可算

| bit | 含义 | bit | 含义 |
|---|---|---|---|
| 0 | 技术面 | 4 | 消息面 |
| 1 | 资金面 | 5 | 公告/财报 |
| 2 | 基本面/估值 | 6 | 龙虎榜 |
| 3 | 情绪面 | 7 | 板块联动 |

Part I §8.1 的**反向证据率**从「靠 LLM 自觉调用」变成一条 SQL：

```sql
-- 给出方向性结论却只用了单一口径的比例；上升 = cherry-picking
SELECT count(*) FILTER (WHERE popcount(evidence_mask) <= 1)::numeric / count(*)
FROM exp_cases
WHERE claim_direction <> 0 AND trade_date BETWEEN :from AND :to;
```

### 13.2 `realizable` —— 修掉一个会污染整个经验库的标签缺陷

现状：`chain/resolver.py:158-168` 的 `verdict_by_rule` 只判方向，**推荐涨停/一字板也判 `hit 0.75`**（`evaluator.py:183` 只跳过「取不到行情」的极端情况）。

后果：
- 事后判定「方向对了」→ 虚高胜率 → `exp_patterns.hit_rate` 系统性偏高
- 偏差一旦烧进 `hit_rate`，**再也分不开**

有了 `realizable` 列，可在 verdict 中单独归一类 `unrealizable`（与 `undecidable`/`data_missing` 同处理、**不计入权重**），且事后可重算。

> 这直接对应 §2.7.2 理由③：涨停板、一字板正是「对手盘行为」最典型的产物——买不进的判断就算方向对也没有意义。

### 13.3 `exp_outcome` 侧表（可选，建议）

若不想在 `exp_cases` 上加太多列，可把 Part I 的 `outcome JSONB` 关键字段提到 1:1 侧表：

```sql
CREATE TABLE exp_outcome (
    case_id            BIGINT PRIMARY KEY,
    ret_raw            NUMERIC(8,5),      -- 毛收益
    ret_net            NUMERIC(8,5),      -- ★ 扣印花税+佣金+滑点
    bench_code         VARCHAR(16),       -- 000300/000905/000852
    ret_bench          NUMERIC(8,5),
    excess             NUMERIC(8,5),      -- ★ 超额 = ret_net - ret_bench
    price_snapshot_ver VARCHAR(16),       -- ★ 复权/数据快照版本
    verdict            SMALLINT,          -- 1 hit/2 partial/3 miss
                                          -- 4 unrealizable/5 undecidable/6 data_missing
    resolved_at        TIMESTAMPTZ
);
```

| 缺陷 | 对应列 |
|---|---|
| 未扣交易成本（往返 0.1–0.3%，与 `_FLAT_PCT=0.3` 判平阈值同量级） | `ret_net` |
| 纯绝对收益无基准（A 股 beta 大，普涨日方向对 ≠ 有 alpha） | `excess` |
| 复权口径漂移（`utils/grounding.py:135` 的坑） | `price_snapshot_ver` |

> Part I §7 的 digest 输出「平均收益 +3.2%」应改用 `ret_net`、「平均超额 +2.1%」改用 `excess`——**摘要口径变更，模板不变**。

---

## 14. `exp_patterns` / `exp_calibration` 增强

### 14.1 `exp_patterns` —— 加统计窗与生命周期

```sql
ALTER TABLE exp_patterns
    ADD COLUMN window_from     DATE,            -- ★ 统计窗起点
    ADD COLUMN window_to       DATE,            -- ★ 统计窗终点
    ADD COLUMN n_eff           NUMERIC(10,2),   -- ★ 有效样本（设计效应折算）
    ADD COLUMN icc_assumed     NUMERIC(5,3),
    ADD COLUMN hit_rate_shrunk NUMERIC(6,4),    -- ★ 向父层收缩后的胜率
    ADD COLUMN parent_ref      BIGINT,
    ADD COLUMN k_prior         NUMERIC(6,2),
    ADD COLUMN cond_signature  VARCHAR(64),     -- ★ 条件组合哈希
    ADD COLUMN status          SMALLINT DEFAULT 1,  -- 1 active/2 deprecated/3 superseded
    ADD COLUMN stat_ver        VARCHAR(16);

CREATE INDEX ON exp_patterns (layer, cond_signature) WHERE status = 1;
```

| 改动 | 解决的问题 |
|---|---|
| `cond_signature` | 检索时如何从当前情境快速命中 pattern（v1 `conditions JSONB` 只能全表扫） |
| `window_from/to` | 这行 `hit_rate=0.62` 基于哪段时间的样本 |
| `n_eff` + `icc_assumed` | 名义条数 ≠ 信息量（见 §17.2） |
| `status` 生命周期 | 旧模式自动淘汰，不与新模式混在一起参与检索 |

**纪律：pattern 行永不 UPDATE。** 重新统计产出**新窗的新行**，旧行标 `deprecated`。这与 Part I §8「旧模式修正」是同一件事，只是保留了修正前的样子，让「当时系统认为胜率是多少」可回看。

`decay_factor` 算法补全（Part I 有字段无算法）：

```python
HALF_LIFE_TD = {
    "market": 120, "sector": 90, "theme": 30,
    "stock":   90, "strategy": 120, "trading": 180,
}
decay_factor = 0.5 ** (days_since_update / HALF_LIFE_TD[layer])
```

检索打分时用 `effective_rate = hit_rate × decay_factor + parent_prior × (1 - decay_factor)`——把时间衰减也视为一次向先验的收缩。

> **`theme` 半衰期最短（30 日）的理由**：题材经验高度依赖当时的资金环境与参与结构，一个季度前的题材规律参考价值衰减极快；而 L5 交易纪律（如止损执行）跨周期稳定得多。这与 §2.7.2 理由②（题材不是同分布、概念漂移最快）一致。

> **沉积边界**：Part I §4.6 的 `stock_personality`（`volatility_rank`/`sector_follow`/`news_sensitivity`/`limit_up_momentum`/`liquidity_score`/`rebound_habit`/`auction_pattern`/`turnover_cycle`）**全部落在稳定的「股性」维度**，应当沉积；
> 而「这只票上涨概率 62%」这类**方向/概率**不应持久化——A 股个股特性半衰期 3–6 个月（游资换庄、纳入指数、基本面变化），下个季度就废，每次由 L0–L2 条件频率实时推导即可。

### 14.2 `exp_calibration` —— 对齐现有 isotonic

现有 `utils/calibration.py:97-179` 已实现 isotonic（`_BUCKET_STEP=10`）。对齐它：

```sql
ALTER TABLE exp_calibration
    ADD COLUMN regime_id    SMALLINT,
    ADD COLUMN strategy_key VARCHAR(32),
    ADD COLUMN score_bucket SMALLINT,     -- 0,10,...,100
    ADD COLUMN n_eff        NUMERIC(10,2),
    ADD COLUMN method       VARCHAR(16),  -- isotonic/platt/raw
    ADD COLUMN window_from  DATE,
    ADD COLUMN window_to    DATE,
    ADD COLUMN stat_ver     VARCHAR(16);
```

Part I §8 的校准示例（0.8 → 0.65）因此可按 regime 分别校准，而不是全局一个系数。

---

## 15. 新增：配置快照与实验档案

### 15.1 `exp_config_snapshot` —— Part I 遗漏的关键一层

```sql
CREATE TABLE exp_config_snapshot (
    config_rev  VARCHAR(32) PRIMARY KEY,
    knobs       JSONB NOT NULL,      -- 工具面/阈值/prompt版本/技能注入量
    created_at  TIMESTAMPTZ DEFAULT now(),
    parent_rev  VARCHAR(32),
    note        TEXT
);
```

**没有这张表，Part I §8 的验证在数据上不成立。** 「改了旋钮 Y 之后变好没有」需要把样本切成改前组/改后组，但案例不带当时配置就切不开——前后对照沦为拍脑袋。

`exp_cases.config_rev` 指向这里，**每条案例都知道自己是在什么配置下产生的**。

### 15.2 实验档案三表

```sql
CREATE TABLE exp_calib_run (
    run_id          BIGSERIAL PRIMARY KEY,
    trigger         JSONB,
    slice_signature VARCHAR(64),          -- 样本切片指纹，用于重放
    case_ids        BIGINT[],             -- ★ 引用不拷贝
    base_rev        VARCHAR(32) REFERENCES exp_config_snapshot,   -- 改前
    target_rev      VARCHAR(32) REFERENCES exp_config_snapshot,   -- 改后
    conclusion      TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE exp_calib_hypothesis (
    hyp_id      BIGSERIAL PRIMARY KEY,
    run_id      BIGINT REFERENCES exp_calib_run,
    statement   TEXT  NOT NULL,           -- 可证伪表述
    criterion   JSONB NOT NULL,           -- 判据：哪个指标、变多少、方向
    window_from DATE,
    window_to   DATE,                     -- 复核窗，同样受 available_at 纪律约束
    status      SMALLINT DEFAULT 0        -- 0 pending/1 confirmed/2 refuted
);

CREATE TABLE exp_calib_action (
    action_id   BIGSERIAL PRIMARY KEY,
    hyp_id      BIGINT REFERENCES exp_calib_hypothesis,
    knob        VARCHAR(48) NOT NULL,
    old_value   JSONB, new_value JSONB,
    status      SMALLINT NOT NULL DEFAULT 0,  -- 0 proposed/1 applied/2 validated/3 reverted
    applied_at  TIMESTAMPTZ,
    validated_at TIMESTAMPTZ
);
```

**关键约定：证据包存 id 引用，不存内容拷贝。** 存 200 条 resolution 的具体内容会因口径变更而漂移；存 id 列表 + 切片指纹即可重放。

### 15.3 归因的两条硬规则

> 这是 §2.7.3 边界二的直接延伸：中段最容易编出合理的故事，所以归因必须收紧。

**规则一：归因必须落在受控枚举上**，不接受自由文本作为动作依据。金融域初始 8 条：

`工具缺失 / 工具误选 / 数据口径错 / 时限不当（horizon 错配）/ 市场环境误判 / 纪律违反 / 判定阈值失当 / 真随机`

每条归因必带**客观锚点**（trace 节点 / claim id / 工具调用 id），无锚点的归因**只记录、不触发动作**。

**规则二：动作必须 out-of-sample 复核才固化。** 触发分析用的样本窗与验证窗必须分开，否则是自证。

> LLM 几乎总能给出听起来合理的解释——解释的生成成本太低、验证成本太高。
> 项目原则「可回测 > 可解释 > 可复现」正好命中：**解释是最廉价的那一档**。
> 上述 8 条是猜测，**须跑一批真实失败样本才能定准**，建议先做「归因标注但不动作」的一个月。

---

## 16. Point-in-Time 正确性

这是经验库区别于普通业务库的唯一要害。**四条纪律，违反任意一条，Part I 的回测结论就不可信。**

| # | 纪律 | 落地 |
|---|---|---|
| 1 | 回测/统计只用 `available_at` | 统一谓词，写进 code review checklist |
| 2 | 收益值绑定数据快照 | `price_snapshot_ver` |
| 3 | regime 定义版本化 | `dim_regime.definition_ver` + `fct_market_regime.computed_by` |
| 4 | 决策环境版本化 | 每条 case 带 `config_rev` |

**推论**：因为 K 线收益会随快照漂移，`exp_outcome` 严格说是**可重算的派生数据**——必须允许按 `price_snapshot_ver` 重算并追加新版本，而不是就地更新。这与 §11 原则 3、原则 8 一致。

---

## 17. 回退链、有效样本、索引与迁移

### 17.1 层级回退 shrinkage（Part I §6 Step 4 的补充）

Part I §6 Step 4 用 `hit_rate_weight` 参与排序。但 L3 个股在样本稀少时，2 条里 2 条对 = 胜率 100%，会把一个噪声案例顶到 Top-8 首位。

```
w = (n_eff · p_child + k · p_parent) / (n_eff + k)      k ≈ 20–30
```

用 §12.4 的闭包表，一次 SQL 查完：

```sql
WITH RECURSIVE chain AS (
    SELECT 0 AS depth, :entity_type AS etype, :entity_id AS eid
    UNION ALL
    SELECT c.depth + 1, h.ancestor_type, h.ancestor_id
    FROM chain c
    JOIN dim_entity_hierarchy h
      ON  h.descendant_type = c.etype
      AND h.descendant_id   = c.eid
      AND :trade_date >= h.valid_from
      AND :trade_date <  COALESCE(h.valid_to, '9999-12-31')
    WHERE c.depth < 5
)
SELECT c.depth, p.id, p.n_eff, p.hit_rate,
       (p.n_eff * p.hit_rate + :k * COALESCE(par.hit_rate_shrunk, :global_prior))
         / (p.n_eff + :k) AS shrunk_rate
FROM chain c
JOIN exp_patterns p
  ON  p.layer = :layer
  AND p.cond_signature = :sig
  AND p.status = 1
LEFT JOIN exp_patterns par ON par.id = p.parent_ref
ORDER BY c.depth;
```

Part I §6 Step 4 的打分公式相应改为使用收缩值：

```
score = similarity      × 0.35
      + recency_decay   × 0.15
      + regime_match    × 0.20
      + shrunk_hit_rate × 0.30   ← 原 hit_rate_weight，改用本节收缩值
```

### 17.2 有效样本 ≠ 名义样本

Part I §9 冷启动表的「需要样本」应理解为**有效样本**。同一题材、同一天的票高度相关，名义 n 严重高估信息量：

```python
# n   名义样本数
# m   平均簇大小（同题材同日重复数，A股常取 3~5）
# icc 簇内相关系数（0.1~0.3 量级）
n_eff = n / (1 + (m - 1) * icc)

# 例：n=10000, m=4, icc=0.15 → n_eff ≈ 6897
#     icc=0.30（极端同质行情） → n_eff ≈ 5263
```

摊薄算术：

| 粒度 | 有效样本 |
|---|---|
| 名义问答条数 | 10,000 |
| 去同期相关（聚类折算） | ≈ 3,000 |
| 按 regime 分档（3 档） | ≈ 1,000 / 档 |
| 再按任务类型（6 类） | ≈ 170 / 格 |
| **下钻到个股（5000+ 只）** | **≈ 2 条 / 票** |

> 另一个常被忽略的约束：**时间跨度**。若 1 万条是 3 个月攒的，只覆盖 **1 个 regime**。
> 所以个股级不是不能开，而是**等跨过 2 个 regime**——在那之前完全靠 §17.1 的 shrinkage 兜底。

### 17.3 索引与分区

```sql
-- 情境过滤主干
CREATE INDEX ON exp_cases (layer, entity_id, trade_date DESC);
CREATE INDEX ON exp_cases (regime_id, layer, trade_date DESC);

-- 只扫已回填的行
CREATE INDEX ON exp_cases (available_at) WHERE available_at IS NOT NULL;

-- 向量：独立表 + HNSW（替代 Part I 的 ivfflat）
CREATE TABLE exp_case_embedding (
    case_id    BIGINT NOT NULL,
    model_ver  VARCHAR(32) NOT NULL,    -- ★ 换模型后旧向量不可比
    embedding  VECTOR(768) NOT NULL,
    PRIMARY KEY (case_id, model_ver)
);
CREATE INDEX ON exp_case_embedding USING hnsw (embedding vector_cosine_ops);
```

**检索路径**（修 Part I §6 的 post-filter 问题）：**先强类型过滤 → 候选集 → 再向量排序**。即先在 `exp_cases` 上用 btree 过滤到 200–500 条，再拿这批 id 去 `exp_case_embedding` 做 KNN，而不是在向量索引上做全局 ANN 后再过滤。

`model_ver` 是必需的：换了 BGE 版本或重训后旧向量与新向量不可比，没有版本号混合检索会**静默退化**——与 `definition_ver` 同一条原则。

规模上来后按 `trade_date` 做 RANGE 分区。

### 17.4 迁移（原地升级，不做 ETL）

因为保留了 Part I 的全部表名与字段，迁移大幅简化：

| 步骤 | 动作 | 风险 |
|---|---|---|
| 1 | 建 `dim_*` 维度表 + `dim_entity_hierarchy` | 纯新增，零风险 |
| 2 | 建 `exp_config_snapshot`，当前旋钮写入 `config_rev='baseline'` | 纯新增 |
| 3 | `ALTER TABLE exp_cases ADD COLUMN ...`（§13，全部可空） | **不阻塞读写**，现有代码无需改 |
| 4 | 回填历史行：从 `situation` JSONB 提取值填入新列 | 需抽样校验映射正确率 |
| 5 | 双读灰度：新老取值对比一段时间 | 可随时回退到 JSONB 取值 |
| 6 | 新 case 双写（JSONB + 强类型列） | — |
| 7 | 冻结 `situation` 中已提升字段的写入（保留只读） | — |

> **第 4 步是唯一有风险的一步**。JSONB 里的字段名、类型、枚举值可能不一致（正是 §10.1 问题 1 的后果）。
> 建议先跑一遍「字段出现率 + 取值分布」剖析再定映射规则，**不要盲 ETL**。
> 若历史数据不可信，也可直接以迁移日为起点、历史由回测侧重灌——往往比修脏数据更干净。

---
---

# Part III · 落地

---

## 18. 冷启动（整合 Part I §9 与 §17 的约束）

| 层级 | 积累速度 | 冷启动方案 | 达到 raw 所需 n_eff |
|---|---|---|---|
| L0 市场 | 慢 | 手工录入历史大事件 | 50–100 |
| L1 板块 | 中 | **回测引擎批量生成** | 200–500 / 板块 |
| L2 题材 | 中 | 历史题材复盘 | 100–300 / 题材 |
| L3 个股 | 快 | Agent 运行积累 + **shrinkage 兜底** | 1000+ 才脱离父层 |
| L4 策略 | 慢 | **回测引擎 A/B 对比** | 200–500 |
| L5 交易 | 中 | 手工 + 交易记录 | 300+ |

> **L3 个股在 n_eff 达到 30 之前一律使用收缩值，不展示原始胜率。** 这条要在 UI / 日志层强制，不能靠自觉。

L4 策略经验天然适合回测引擎生成——这是回测引擎与经验库的最佳衔接点。

---

## 19. 实施优先级

```text
P0 · 数据地基（1-2 周，其余一切都依赖它）
├── ① dim_trade_date + dim_security + dim_regime + fct_market_regime
├── ② dim_entity_hierarchy 闭包表
├── ③ exp_config_snapshot + exp_cases.config_rev
└── ④ exp_cases 强类型化 + available_at 纪律（改写所有回测谓词）

P1 · 标签正确性（必须与 P0 并行，沉积前必修）
├── ⑤ realizable 列 + unrealizable verdict 单独归类不计权重
├── ⑥ ret_net 成本模型（走 env 配置）
└── ⑦ excess 相对基准 + 市值分档

P2 · 回退与统计（2-4 周）
├── ⑧ shrinkage 查询（§17.1 recursive CTE）
├── ⑨ n_eff / icc_assumed 折算
├── ⑩ exp_patterns 统计窗 + cond_signature + status 生命周期
└── ⑪ decay_factor 算法落地（分层级半衰期）

P3 · 检索与压缩（1-3 月）
├── ⑫ exp_patterns.cond_signature 快速命中
├── ⑬ btree 过滤 → 向量排序（修 post-filter）
├── ⑭ exp_calibration 按 regime 分桶（复用 utils/calibration.py isotonic）
└── ⑮ digest 改用 ret_net / excess 口径

P4 · 自改进闭环（3-6 月）
├── ⑯ 归因枚举标注但不动作（攒一个月定枚举）
├── ⑰ exp_calib_run / hypothesis / action 闭环
└── ⑱ 四个统计指纹上线（可证伪率/undecidable/出声率/反向证据率）
```

**顺序纪律：先做回测侧，别先动 agent。**

1. 回测侧立刻可行：样本无限、结构化、不依赖任何 agent 改造、能马上验证
2. agent 侧必须等**强类型列定义稳定**才有意义——列定义一改，历史数据要么重算要么作废
3. **附带收益**：回测侧先跑起来会反过来告诉投影层该有哪些维度（哪些切分维度在百万级样本上区分度显著），比拍脑袋定维度可靠得多

---

## 20. 风险与待决

### 20.1 风险表

| 风险 | 对策 |
|---|---|
| **经验最容易自我欺骗**（§2.7.3 边界二） | 四个统计指纹 + T+N 真值回填 + `available_at`，三者缺一不可 |
| **经验同质化**（§2.7.4） | 经验库由自己的成交/复盘样本喂养，不靠通用知识 |
| 沉积是放大器不是纠偏器 | 先建统计指纹 + `config_rev` 分组，再谈沉积 |
| X 空间不稳定：问法/路径/结论每次不同 | 投影层确定性映射，产物落成强类型列（保留 JSONB 仅作扩展） |
| 有效样本 ≠ 名义样本 | `n_eff` 落表，`icc_assumed` 可追溯 |
| 保守化退化：越校准越不敢说话 | 监控出声率，设下限告警 |
| 归因不能信：LLM 几乎总能给出合理解释 | 受控枚举 + 客观锚点，无锚点只记录不动作 |
| 前视偏差：三个时间戳混用 | `available_at` 唯一谓词，写进 code review checklist |
| 口径漂移：regime 定义改了、向量模型换了 | `definition_ver` / `model_ver` / `stat_ver` 全量版本化 |
| 不可成交 / 未扣成本 / 无基准 | P1 三条必修，`realizable` 保证可事后分离 |
| L3 个股股性漂移（半衰期 3–6 月） | 只沉积股性、不持久化方向概率；按层设半衰期 |

### 20.2 待决问题

| # | 问题 | 影响 |
|---|---|---|
| 1 | **回测引擎能否按 regime × 板块输出结果统计？** | 决定 P0 后能否灌入百万级样本；不能则只能靠 agent 缓慢积累 |
| 2 | v1 历史 `exp_cases` 数据是否值得回填？ | 若不可信，直接以迁移日为起点 + 回测侧重灌更干净 |
| 3 | `icc` 取值如何标定？ | 影响 `n_eff`，进而影响各层「够不够样本」的判定 |
| 4 | 数据是只进不出，还是需要淘汰过期 case？ | 决定分区保留策略与存储成本 |
| 5 | 是否接受「方向类经验不持久化」？ | 影响冷启动阶段的实际体验 |
| 6 | ~~目标是校准还是提升预测力？~~ **→ 已由 §2.4 修正**（二者同为条件概率估计）**；本方案选路径乙，且不与机构正面竞争（§2.8）** | 战略指向：小容量 + T+1~T+20 + 高信念低频；衡量标准改为期望净收益与回撤，不是 RankIC（§2.8.4） |
| 7 | v1 是否只出建议、人工确认后才应用？ | 强烈建议是。**全自动闭环一旦开始，很难停下来验证它到底在学什么** |

---

## 21. 证据索引

| 结论 / 缺口 | 文件:行 |
|---|---|
| `verdict_by_rule` 只判方向（→ `realizable` 列） | `app/agent/chain/resolver.py:158-168` |
| 退市/停牌跳过逻辑 | `app/agent/chain/evaluator.py:183` |
| domain 是最细可诚实支撑粒度（`decisions` 无 symbol/theme/regime） | `app/agent/chain/weight_feed.py:14-18` |
| EMA 慢调公式（可复用为 decay） | `app/agent/chain/weight_feed.py:20-23, 43-45` |
| isotonic score→correct 校准（→ `exp_calibration`） | `app/agent/utils/calibration.py:97-179`（`_BUCKET_STEP=10`） |
| case_memory T+N 回填（写入端待改造为经验库写入端） | `app/agent/utils/case_memory.py`（506 行，`record_case`/`format_case_injection` **0 处调用**） |
| 复权同值不同号坑（→ `price_snapshot_ver`） | `app/agent/utils/grounding.py:135` |
| `trading_calendar` 默认 False（→ `dim_trade_date`） | `app/agent/domain_registry.py:45` |
| `mood_regime`（仅在选股技能内 → `fct_market_regime`） | `app/agent/skills/market_screener/common.py:294` |
| grounding gate 反射 | `app/agent/qd_agent.py:1115-1130` |
| 阶段验收 TODO（用户 2026-10-01 裁定暂不实现） | `app/agent/qd_agent.py:1107-1114` |
| `AGENT_PLAN_BEST_OF_N` 为 env 残留声明（无实现） | `app/agent/constants.py:44` |
| prompt 回归套件（改 prompt 必过） | `app/agent/scripts/qd_prompt_eval.py` |
| 后台子 agent 分发通道（可加 `calib_analyze`） | `run_skill` → 子进程 `scripts/skill_run.py` |

### 21.1 信息不足声明

- `mimoagent` 为 pip 安装包，其 ReAct 内部是否有隐藏的自省/采样机制未读源码
- `prompt_tasks.yaml` 8 条任务的具体断言内容未逐条展开
- §17.2 的 `icc` 取值未经实测，需按实际簇结构标定
- 现有 `exp_cases` 数据量未知，§17.4 第 4 步的风险敞口待评估
- 回测引擎按 regime × 板块切分的可行性未验证（待决问题 #1）

以上不影响主结论。

---

## 22. 总结

Agent 在 A 股中短线的定位是"越来越有经验的老股民 + 有纪律的分析师"。

- **量化做筛选**：从全市场掐出 50-100 只候选
- **经验库做判断**：按决策链六层（市场→板块→题材→个股→策略→交易）逐层检索相似案例
- **校准做修正**：知道每类判断历史上命中率多少、该信几分
- **真值做沉淀**：T+N 回填结果，提炼模式，持续进化

预测不是目标，经验积累才是。通过校准从经验中分析和提高未来的命中率，在量化和纯主观之间找到属于自己的位置。

**Part II 的全部工作，只是让上面这四句话在百万级样本规模下依然成立。**

> 最后重申 §2.7 的两条边界，它们是整个方案最容易失守的地方：
> **经验必须外化进库（不能指望模型内化）**，以及**中段最没标尺，所以真值回填与统计指纹是命根子（不是边角料）**。
