# bull_bear_research / technical_analysis 改接 market_cn 施工清单

- 日期:2026-10-15 | 状态:待施工
- 目标:聚合工具(bull_bear_research、technical_analysis)的数据依赖从 `tools/finance/` 内部取数改接到 `app/market_cn/` 数据内核,基础行情件交能力层
- 原则:单点适配、双跑对拍、逐步切换、每步可回滚

---

## 0. 一个关键事实(决定对拍容差)

`_analysis_utils._fetch_klines` → `DataSourceFactory.get_kline` **内部已做 unadj→qfq**
(证据:`data_tools.py:225` 注释"ds.get_kline 内部已 unadj→qfq";`data_tools.py:175-271` 的
`agent_get_kline` 以 qfq 为默认口径),而 `market_cn/auto/core/data/hub.py:160` 的
`daily()` 同样返回 **qfq**。⇒ 两边复权口径**天然一致**,对拍应严格相等(±0 价格容差),
任何差异都是**数据时点/字段口径**问题,不是复权问题。这是本次改接能"严格对拍"的前提。

## 1. 现状依赖图(改前)

```
bull_bear_research ─┬─ ._analysis_utils._fetch_ohlcv ─ _fetch_klines ─ DataSourceFactory(直连)  ← 取数
                    ├─ .analysis_tools._calc_macd/rsi/boll/kdj                                ← 纯计算
                    ├─ .data_tools.get_realtime_quote ─ app.market_cn.tape/index(绕一层包装)  ← 行情
                    ├─ .news_search_tools.search_stock_intel/search_policy_intel              ← 情报
                    └─ .chip_distribution.get_chip_distribution                               ← 筹码

technical_analysis ── 同 _fetch_ohlcv;实时行情已直连底层(注释明言"避免 import 工具包装层")
```

受影响文件(全仓 grep `_fetch_klines|_fetch_ohlcv|DataSourceFactory` 命中 6 个):
`_analysis_utils.py`、`analysis_tools.py`、`bull_bear_research.py`、`chip_distribution.py`、
`data_tools.py`、`technical_analysis.py`。

## 2. 目标依赖图(改后)

```
bull_bear_research / technical_analysis / chip_distribution / analysis_tools
        │
        ▼
finance/_market_data.py  ← ★ 新建:唯一适配层(统一字段名/时间键/缺数语义)
        │
        ▼
app.market_cn.auto.core.data.hub   ← 数据内核(daily/quote/daily_live/market_snapshot)
```

- `data_tools.get_realtime_quote` 等**基础件包装**在聚合工具改接完成后删除注册,
  由能力层 `market_cn` 同名/近名能力补位(loader 的"删除包装后自动补位"机制);
- `news_search_tools` / `chip_distribution` 的**语义部分保留**,仅取数下沉。

## 3. 接口映射表

| 现调用 | 目标调用(hub) | 口径注意 |
|---|---|---|
| `_fetch_klines(code, days)`(直连) | `hub.daily(code, days=days)` | 均 qfq;hub 返回键 `time/open/high/low/close/volume`,现返回键 `open/high/...`+`t/time` 混用 ⇒ **适配层统一键名** |
| `_fetch_ohlcv(code, days)` | `hub.daily` 后在适配层转 OHLCV 五序列 | 保持返回结构不变,调用方零改动 |
| `get_realtime_quote(codes)`(多股) | `hub.market_snapshot(codes)` / `hub.quote(code)` | hub 是**快照池语义**(可注入 snaps 避免重复查询);多股在适配层循环+合并 |
| (分析用"今日未回填") | `hub.daily_live(code, days)` | ★ PIT 口径:分析**当时看到的数**用 `daily`(库内已回填),需要含今日合成 bar 才用 `daily_live`——见 §6 未决裁定① |
| `data_tools.agent_get_kline` 的 adj 处理 | 保留在工具层 | hfq/none 换算(`unadj_to_hfq` 等)是工具层语义,不下沉 |

## 4. 施工步骤(每步一个可独立提交/回滚的单元)

### P1 建适配层 `tools/finance/_market_data.py`(不动任何调用方)
- 导出:`fetch_klines(code, days) -> List[dict]`(键名与现 `_fetch_klines` 完全一致)、
  `fetch_ohlcv(code, days) -> {"open","high","low","close","volume"}`、
  `fetch_quote(codes) -> dict`(结构与现 `get_realtime_quote` 返回一致)。
- 内部只调 `app.market_cn.auto.core.data.hub`;**不做任何数值加工**(不做四舍五入、不填 0)。
- 失败语义与现一致:取不到返回 `[]`/`{}`,**不填 0**(与 context_frame"取不到就是 None"红线同向)。
- 单测:`test_rewire_market_data.py`——键名契约、空值语义、与 `_fetch_klines` 双跑相等。

### P2 `_analysis_utils.py` 改为薄委托
- `_fetch_klines/_fetch_ohlcv/_fetch_closes/_get_ds` 改走 `_market_data`;
  **函数签名与返回结构一个字不改**(6 个文件的调用方零感知)。
- 删 `DataSourceFactory` 直连 import。
- 验收:全量 tests 不动一行仍通过;`test_rewire_p2_parity.py` 双跑对拍(§5)。

### P3 两个聚合工具改行情调用
- `bull_bear_research.py:16` `from .data_tools import get_realtime_quote`
  → `from ._market_data import fetch_quote`(注意 `:39` 的 lambda 一并改)。
- `technical_analysis.py:40` 的底层直连段 → 同走 `_market_data`。
- 验收:对拍 + smoke(qd_smoke 相关项)。

### P4 基础件去重(能力层补位)
- 删除/降级 `data_tools.get_realtime_quote`、`index_tools.get_index_kline` 等**纯转发包装**
  的工具注册(函数可留作内部实现,但不再进工具注册表)⇒ loader 让位日志中同名项消失。
- 给聚合工具补**依赖声明**(见 §6 未决裁定②),预选时随行下发基础能力。
- 验收:启动日志让位计数下降;预选目录无同名双轨;`tests/test_v14_preselect_capability_catalog.py` 类守卫更新。

### P5 守卫固化
- 口径守卫:`test_rewire_guard.py` 断言 `tools/finance/` 内**零** `DataSourceFactory` 直连
  (只允许 `_market_data.py` 一处接 hub)——防第三套取数回潮。
- 双口径守卫:绑定 `hub.daily` 返回键名契约(键名一变就 FAIL,防止静默漂移)。

## 5. 双跑对拍方案(切换的验收标准)

1. **样本**:取 20 只票 × 120 日(含至少 1 个除权日、1 个停牌票)+ 指数 K 线。
2. **比对项**:
   - `_fetch_ohlcv` 旧 vs `_market_data.fetch_ohlcv` 新:逐日 OHLCV **严格相等**(均 qfq,容差 0);
   - `get_realtime_quote` 旧 vs `fetch_quote` 新:价格/涨跌幅容差 ≤0(同一拍),字段集差集为空;
   - **端到端**:`bull_bear_research("600519")` 旧 vs 新,多空评分/方向判断**逐字段相等**。
3. **差异归因纪律**:任何不相等先归因(时点差/键名差/缺数填 0 差),不许"调参追平";
   归因不了的差异 = 不切换。
4. **断档保护**:chain 权重样本(weight_feed)以 verdict 为准,不直接受取数路径影响;
   但对拍期内**双跑并行落一次结果**到日志,若评分偏移 >0,历史胜率统计标注口径断点。

## 6. 未决裁定(施工前必须拍板,不许拍脑袋默认)

| # | 问题 | 建议 |
|---|---|---|
| ① | 分析取数用 `hub.daily`(库内已回填)还是 `hub.daily_live`(含今日合成 bar)? | **用 `daily`**:分析场景多数在盘后;`daily_live` 的合成 bar 是估算值,进 MACD/RSI 会污染指标。需要盘中即时的场景单独显式传参,不设默认 |
| ② | 聚合工具的依赖声明机制放哪?(预选"随行下发"目前无结构化载体) | 先在 `_market_data` 模块头登记 `DEPENDS = ("hub.daily","hub.quote")` 常量,预选侧读它拼目录;不引入新配置面 |
| ③ | `data_tools` 的包装函数删除还是保留为内部实现? | 保留实现、摘注册(可回滚);等 P4 稳定一个迭代再删实现 |

## 7. 风险清单

| 风险 | 缓解 |
|---|---|
| hub 依赖行情库快照,库未回填时 `daily` 缺今日 bar | `_market_data` 显式返回缺数,不填 0;调用方降级逻辑不变 |
| 多股 quote 从"一次请求"变"快照池循环",性能回退 | `hub.quote(code, snaps=...)` 支持注入快照池,适配层一次拉池多股复用 |
| 删包装后模型直接点名基础能力拼分析,绕过聚合口径 | 聚合工具在系统提示/工具描述里声明"分析请走本工具";预选 lint R0 让位语义已兜底 |
| 6 个文件同时受影响,一次改崩 | P2 的"签名不变薄委托"把爆炸半径压到 1 个文件;P3 才动聚合工具 |

## 8. 回滚

- P1~P3 每步独立提交;出问题 `git revert` 单步即回(适配层保留不影响旧行为)。
- P4 摘注册不删实现 ⇒ 回滚只需把注册加回,无需恢复代码。
