# 自动策略组架构重设计

## 一、现状分析

### 1.1 当前文件结构

```
app/market_cn/auto/
├── dragon_core.py      # 策略逻辑: 龙回头/V1/断板 (951行, 单体)
├── relay3.py           # 3板接力策略 (独立文件, 244行)
├── dragon_scan.py      # 盘后扫描 (242行, 硬编码调用dragon_core)
├── dragon_monitor.py   # 盘中监控 (505行, 硬编码策略特有逻辑)
├── dragon_store.py     # 存储层 (490行, 知道具体策略名)
├── dragon_api.py       # API端点 (62行)
├── intraday_core.py    # 日内策略核心
└── intraday_backtest.py # 日内回测
```

### 1.2 痛点

| 问题 | 表现 |
|------|------|
| **策略耦合** | dragon_core.py 包含3个策略, 修改一个影响全部 |
| **接口不统一** | 龙回头用 `dragon_cb_today_d0_signals`, V1用 `v1_today_d0_signals`, 签名不同 |
| **监控硬编码** | dragon_monitor.py 对每个策略有独立的入场/出场/确认逻辑 |
| **存储感知策略** | dragon_store.py 硬编码 `STRATEGIES = ("dragon_callback", "v1", "break", "relay3")` |
| **回测碎片化** | 每个策略有自己的回测函数, 无法统一评估 |
| **扩展困难** | 新增策略需修改 dragon_core.py + dragon_scan.py + dragon_monitor.py + dragon_store.py |
| **错误耦合** | 一个策略异常可能阻塞其他策略的扫描/监控 |

### 1.3 已有良好设计 (保留)

- `dragon_core.py` 的 as-of 安全: 所有判定只用 <= 当日收盘数据
- `unified_prefilter`: 通用前置过滤 (U1~U4)
- `dragon_store.py` 的幂等设计和状态机
- `relay3.py` 的独立文件模式 (正确方向)

---

## 二、目标架构

### 2.1 核心原则

1. **策略即插件**: 每个策略是独立目录/文件, 实现统一接口
2. **数据/逻辑/输出三层分离**: 策略只做判定, 不做IO
3. **错误隔离**: 一个策略崩溃不影响其他策略
4. **配置驱动**: 开关/参数/排名通过配置管理, 不改代码
5. **回测即信号**: 同一个信号函数同时服务回测和实盘

### 2.2 目录结构

```
app/market_cn/auto/
├── strategy/                      # 策略目录 (每个策略一个文件)
│   ├── __init__.py               # 注册表 + 自动发现
│   ├── base.py                   # StrategyBase 抽象基类
│   ├── dragon_callback.py        # 龙回头方案2
│   ├── v1.py                     # V1 追击连板
│   ├── break_buy.py              # 断板
│   └── relay3.py                 # 3板接力
├── common/                        # 通用算法库
│   ├── __init__.py
│   ├── filters.py                # unified_prefilter, PREFILTER_PARAMS
│   ├── indicators.py             # calc_macd, calc_bollinger_bw, rsi, calc_roc...
│   ├── market.py                 # get_board_type, is_limit_up, find_limit_ups
│   └── backtest.py               # 统一回测引擎
├── data/                          # 数据访问层
│   ├── __init__.py
│   ├── kline.py                  # fetch_kline_db (日K加载)
│   ├── stock_info.py             # fetch_stock_info_db (基本面)
│   └── market_data.py            # 板块/概念/涨停统计
├── output/                        # 输出层 (标准化信号格式)
│   ├── __init__.py
│   ├── signal.py                 # Signal dataclass (统一信号格式)
│   ├── db_writer.py              # 写 qd_dragon_signals (存储路径)
│   └── display.py                # 格式化输出 (CLI/前端路径)
├── scanner.py                     # 盘后扫描 (替代 dragon_scan.py)
├── monitor.py                     # 盘中监控 (替代 dragon_monitor.py)
└── store.py                       # 存储层 (替代 dragon_store.py, 通用化)
```

### 2.3 策略接口 (StrategyBase)

```python
# strategy/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

@dataclass
class Signal:
    """统一信号格式 (策略产出, 不含IO)"""
    strategy: str              # 策略标识: "dragon_callback" / "v1" / "break" / "relay3"
    code: str                  # 股票代码
    signal_date: str           # 信号日 (D0)
    entry_date: str            # 入场日 (D1)
    entry_price: float         # 入场价
    score: int                 # 信号强度 0~100
    # 策略特有字段 (通过 extra dict 承载)
    extra: dict

@dataclass
class ExitRule:
    """出场判定结果"""
    action: str                # "hold" / "sell_tomorrow" / "sell_now"
    reason: str                # 出场原因
    stop_price: float          # 止损价
    hold_days: int             # 已持仓天数
    max_hold_days: int         # 最大持仓天数

class StrategyBase(ABC):
    """策略基类 — 所有策略必须实现此接口"""

    @property
    @abstractmethod
    def key(self) -> str:
        """策略标识 (唯一, 用于存储/配置)"""
        ...

    @property
    @abstractmethod
    def label(self) -> str:
        """策略中文名"""
        ...

    @property
    def enabled(self) -> bool:
        """是否启用 (可由配置覆盖)"""
        return True

    @property
    def daily_limit(self) -> int:
        """每日买入名额"""
        return 5

    @abstractmethod
    def scan_signals(self, bars: list, code: str, stock_info: dict = None) -> list[Signal]:
        """盘后扫描: D0收盘后, 返回当日所有信号 (as-of安全, 只用<=D0数据)

        与 backtest_signals 是同一个函数 — 回测时外部逐日截断bars调用即可。
        返回空 list 表示无信号。
        """
        ...

    def check_entry(self, signal: Signal, bars: list, d1_idx: int) -> Optional[Signal]:
        """D1开盘过滤: 检查gap等条件, 返回None表示放弃入场

        默认实现: 不过滤, 直接接受。策略可覆盖。
        """
        return signal

    @abstractmethod
    def check_exit(self, signal: Signal, bars: list, current_idx: int, entry_idx: int) -> ExitRule:
        """出场判定: 每日收盘后调用, 返回出场决策

        bars: 完整K线 (含入场日到当日)
        current_idx: 当日在bars中的索引
        entry_idx: 入场日在bars中的索引
        """
        ...

    def calc_score(self, signal: Signal, bars: list) -> int:
        """信号强度评分 0~100 (可选覆盖, 默认50)"""
        return 50

    def format_signal(self, signal: Signal) -> str:
        """格式化信号为人类可读文本 (CLI --today 用)"""
        return f"{self.label} {signal.code} {signal.signal_date} 评分{signal.score}"
```

### 2.4 策略注册表

```python
# strategy/__init__.py
import importlib
import pkgutil
from .base import StrategyBase

_REGISTRY: dict[str, StrategyBase] = {}

def register(strategy: StrategyBase):
    """注册策略实例"""
    _REGISTRY[strategy.key] = strategy

def get(key: str) -> StrategyBase | None:
    return _REGISTRY.get(key)

def get_all() -> dict[str, StrategyBase]:
    return dict(_REGISTRY)

def get_enabled() -> dict[str, StrategyBase]:
    return {k: v for k, v in _REGISTRY.items() if v.enabled}

# 自动发现: 导入 strategy/ 目录下所有模块, 模块末尾调用 register()
def autodiscover():
    """扫描 strategy/ 目录, 导入所有模块触发注册"""
    pkg_path = __path__[0]
    for _, name, _ in pkgutil.iter_modules([pkg_path]):
        if name != 'base':
            importlib.import_module(f'.{name}', package=__name__)
```

### 2.5 通用算法库 (common/)

```python
# common/indicators.py — 纯计算, 零IO, 零依赖
def calc_macd(closes, fast=12, slow=26, signal=9): ...
def calc_bollinger_bw(closes, period=20, num_std=2): ...
def rsi(closes, period=14): ...
def calc_roc(closes, period=10): ...
def calc_psy(closes, period=12): ...

# common/market.py — 市场工具函数
def get_board_type(code): ...
def is_limit_up(close, prev_close, board_type): ...
def find_limit_ups(bars, board_type): ...

# common/filters.py — 通用前置过滤
PREFILTER_PARAMS = {...}
def unified_prefilter(bars, i, code, code_info=None): ...

# common/backtest.py — 统一回测引擎
def run_backtest(bars, entry_idx, entry_price, hold_days, stop_loss,
                 trailing_stop, board_type, exit_rules_fn=None): ...
```

### 2.6 扫描器 (scanner.py)

```python
# scanner.py — 替代 dragon_scan.py
from app.market_cn.auto.strategy import get_enabled
from app.market_cn.auto.data.kline import fetch_kline_db
from app.market_cn.auto.data.stock_info import fetch_stock_info_db
from app.market_cn.auto.output.db_writer import save_signals

def run_daily_scan(days=300):
    """盘后全市场扫描"""
    strategies = get_enabled()  # 只跑启用的策略
    stock_info = fetch_stock_info_db()
    all_codes = get_all_codes()

    all_signals = []
    for code in all_codes:
        bars = fetch_kline_db(code, days)
        if not bars:
            continue
        info = stock_info.get(code)

        for key, strategy in strategies.items():
            try:
                sigs = strategy.scan_signals(bars, code, stock_info=info)
                all_signals.extend(sigs)
            except Exception as e:
                logger.error(f"[{key}] {code} 扫描异常: {e}")  # 错误隔离
                continue

    save_signals(all_signals)  # 统一写入
    return all_signals
```

### 2.7 监控器 (monitor.py)

```python
# monitor.py — 替代 dragon_monitor.py
from app.market_cn.auto.strategy import get_enabled, get

def tick():
    """60s tick: 遍历活跃信号, 按策略分发出场判定"""
    strategies = get_enabled()
    active = store.get_active_signals()

    for signal in active:
        strategy = get(signal.strategy)
        if not strategy:
            continue
        try:
            bars = fetch_kline_db(signal.code, 300)
            entry_idx = find_idx(bars, signal.entry_date)
            current_idx = len(bars) - 1
            exit_rule = strategy.check_exit(signal, bars, current_idx, entry_idx)

            if exit_rule.action == "sell_tomorrow":
                store.update_state(signal.id, "exit_today", exit_rule.reason)
            elif exit_rule.action == "sell_now":
                store.update_state(signal.id, "exit_today", f"盘中{exit_rule.reason}")
        except Exception as e:
            logger.error(f"[{signal.strategy}] {signal.code} 监控异常: {e}")
```

### 2.8 存储层 (store.py)

```python
# store.py — 替代 dragon_store.py, 策略无关
STRATEGIES_TABLE = "qd_dragon_signals"

def save_signals(signals: list[Signal]):
    """批量写入信号 (幂等, 按 trade_date+strategy+code 去重)"""
    ...

def get_active_signals(strategy: str = None) -> list:
    """获取活跃信号 (可按策略过滤)"""
    ...

def update_state(signal_id: int, state: str, reason: str = ""):
    """更新信号状态"""
    ...

# 策略元数据从注册表获取, 不硬编码
def get_strategy_labels():
    from app.market_cn.auto.strategy import get_all
    return {k: v.label for k, v in get_all().items()}
```

### 2.9 回测框架 (common/backtest.py)

```python
def backtest_strategy(strategy: StrategyBase, codes: list, days=300):
    """通用回测: 对任意策略跑全市场回测, 返回标准化结果"""
    trades = []
    for code in codes:
        bars = fetch_kline_db(code, days)
        if not bars:
            continue
        # 逐日截断, 调用策略的 scan_signals (回测和实盘同一函数)
        for i in range(25, len(bars) - 1):
            sigs = strategy.scan_signals(bars[:i+1], code)
            for sig in sigs:
                entry_idx = i + 1
                if entry_idx >= len(bars):
                    continue
                # D1 入场过滤
                sig = strategy.check_entry(sig, bars, entry_idx)
                if not sig:
                    continue
                # 出场模拟
                exit_rule = strategy.check_exit(sig, bars, min(entry_idx + strategy.max_hold, len(bars)-1), entry_idx)
                trades.append({...})
    return trades

def print_backtest_report(trades, label):
    """标准化回测报告: 胜率/均收益/盈亏比/收益分布/峰值分布"""
    ...
```

---

## 三、迁移计划

### 3.1 Phase 1: 搭骨架 (不动现有代码)

1. 创建 `strategy/` `common/` `data/` `output/` 目录
2. 从 `dragon_core.py` 提取通用函数到 `common/indicators.py` 和 `common/market.py`
3. 实现 `StrategyBase` 基类和注册表
4. 实现统一回测框架

### 3.2 Phase 2: 迁移策略 (逐个, 可回退)

按信号量从少到多迁移, 每迁移一个策略立即跑回测对数:

1. **relay3** — 已经是独立文件, 只需实现 StrategyBase 接口
2. **V1** — 从 dragon_core.py 提取, 实现接口
3. **断板** — 从 dragon_core.py 提取, 实现接口
4. **龙回头** — 从 dragon_core.py 提取, 实现接口

### 3.3 Phase 3: 重构基础设施

1. scanner.py: 从策略注册表遍历, 替代 dragon_scan.py 的硬编码
2. monitor.py: 从策略注册表分发出场判定, 替代 dragon_monitor.py
3. store.py: 策略无关化, 从注册表获取元数据

### 3.4 Phase 4: 清理

1. 删除 dragon_core.py (内容已分散到 strategy/ + common/)
2. 删除 dragon_scan.py / dragon_monitor.py (已被 scanner.py / monitor.py 替代)
3. 保留 dragon_store.py → store.py 的迁移路径 (兼容旧数据)

---

## 四、配置文件

```json
// strategy_config.json
{
  "strategies": {
    "dragon_callback": {
      "enabled": true,
      "daily_limit": 5,
      "params": {
        "gap_min": 5,
        "gap_max": 6,
        "stop_loss": -8.0,
        "hold_days": 7
      }
    },
    "v1": {
      "enabled": true,
      "daily_limit": 5,
      "params": {
        "ret_20d_min": 30.0,
        "d_1_pullback_min": -10.0,
        "d_1_pullback_max": -3.0
      }
    },
    "break": {
      "enabled": true,
      "daily_limit": 5,
      "params": {
        "first_break_gap_min": 0,
        "first_break_chg_min": 0.0
      }
    },
    "relay3": {
      "enabled": true,
      "daily_limit": 2,
      "params": {
        "board_height": 3,
        "gap_min": -2.0,
        "gap_max": 9.0
      }
    }
  }
}
```

---

## 五、信号强度评分标准 (0~100)

每个策略实现 `calc_score`, 按以下通用框架:

| 区间 | 含义 | 实盘建议 |
|------|------|---------|
| 80~100 | 强信号 | 优先买入, 可满名额 |
| 60~79 | 中信号 | 正常买入 |
| 40~59 | 弱信号 | 观察, 不确定性高 |
| 0~39 | 极弱 | 不买入 |

评分因子 (各策略权重不同):
- 趋势强度 (涨停前涨幅/连板数)
- 回踩质量 (回调天数/量比/形态)
- 资金面 (OBV/换手率/市值)
- 均线环境 (多头排列/MA斜率)
- D1确认 (日内动量/量比, 仅 confirm 模式)

---

## 六、关键约束

1. **as-of 安全**: 所有 `scan_signals` 只用 `bars[:i+1]`, 不能看未来数据
2. **回测即信号**: `scan_signals` 同时服务回测和实盘, 保证一致性
3. **零IO原则**: strategy/ 和 common/ 不做数据库/网络操作, 通过 data/ 层获取数据
4. **错误隔离**: scanner/monitor 对每个策略 try-except, 一个策略异常不影响其他
5. **向后兼容**: qd_dragon_signals 表结构不变, 只扩展 strategy 字段值

---

## 七、现状依赖图 (Explore Agent 分析)

```
test_dragon.py ──────imports──→ dragon_core.py (信号/出场/预过滤)
                                    ↑
dragon_scan.py ──imports──→ dragon_core.py (信号函数)
     │                         relay3.py (relay3_today_d0_signals)
     │                         dragon_store.py (upsert, sync, cleanup)
     ↓
dragon_monitor.py ──imports──→ dragon_core.py (出场函数)
     │                           dragon_store.py (状态机常量)
     │                           dragon_scan.py (fetch_kline_db)
     │                           relay3.py (gap_buyable, eval_exit)
     ↓
dragon_store.py ──imports──→ app.utils.db (零依赖 dragon_core)
     ↓
dragon_api.py ──imports──→ dragon_store.py only
```

**关键发现**: `dragon_store.py` 对 `dragon_core.py` 零依赖 — 存储层已是策略无关的。
`dragon_api.py` 只依赖 `dragon_store.py` — API 层已是干净的。

**新策略需修改的文件 (5处)**:
1. `dragon_core.py` — 加信号函数 + 出场函数 + 参数
2. `dragon_scan.py` — 加显式扫描调用
3. `dragon_monitor.py` — 在 `_gap_buyable` / `_entry_stop` / `_eval_exit_day_close` / `evaluate_confirm` 加分支
4. `dragon_store.py` — 加到 `STRATEGIES` / `STRATEGY_LABELS` / `STRATEGY_WINRATE` / `DAILY_LIMIT_PER_STRATEGY`
5. 新策略文件 (如 relay3.py)

**目标**: 新策略只需 1 处 — 创建 `strategy/xxx.py` 实现 `StrategyBase` 接口, 注册表自动发现。

---

## 八、当前策略参数汇总 (迁移时需保留)

### 8.1 龙回头方案2 (DRAGON_CB_PARAMS)
- 回调窗口: [5, 6] 天
- 拐点过滤: OR(均线支撑[-10%,-5%), 深跌释放<=-30%, 阴线比<50%)
- 质量排除: 阴线>=60% / RSI<30 / 距MA20<-8%
- D1 gap: [-3%, +2%]
- 出场: 止损-8%, 分段追踪(<3%→-8%, >=3%→-3%), 峰值逃顶(>7%+上影>30%), 7天

### 8.2 V1 追击连板 (v1_today_d0_signals)
- 因子1: 20日涨幅>=30%
- 因子2: D-1回调 -10%~-3%
- 因子3: OBV 5日上升
- 因子4: D-1量<1.5x均量
- 因子5: 纯单板 MACD柱<2 + 布林带宽<45%
- D1过滤: 主板[-3%,+3%), 创科[-5%,+5%), 主板高开3-5%不入场
- 出场: 日内动量<3%→D2清仓, 追踪-5%, 7天

### 8.3 断板 (BOARD_PARAMS + break_today_d0_signals)
- 连板>=2 → 断板期 → 确认日
- 首断板: gap>=0 AND chg>=0%
- 增强: OR(确认日0~2% / 均量比>=1.4 / pre20>=30%)
- 出场: 止损-8/-10, 追踪-6/-8, 峰值逃顶(>10%+上影>40%), 20/15天

### 8.4 3板接力 (relay3.py PARAMS)
- 昨日恰3连板 + MA多头排列
- D1 gap [-2%, +9%]
- 每日最多2只
- 出场: 炸板即卖, 止损-5%, 追踪-8%, 3天到期

### 8.5 通用前置过滤 (PREFILTER_PARAMS)
- U1: 排除ST
- U2: 换手率>=3%
- U3: 流通市值20~500亿
- U4: 20日涨幅>=10% 或 前20日有涨停

### 8.6 状态机 (dragon_store)
```
watch_pending → buy_today → holding → exit_today → closed
                  ↓
               expired (弱确认/gap放弃/排名淘汰)
```
- 活跃组: watch_pending + buy_today + holding + exit_today
- 每策略每日名额: dragon_callback=5, v1=5, break=5, relay3=2
