#!/usr/bin/env python3
"""auto/registry.py — 自动策略组注册表 (策略元数据单一事实源, 2026-09-10 自 dragon_store 拆出)

用途: 策略 key/中文名/历史胜率/每日名额/信号状态机等**纯元数据**集中于此;
     不做任何 DB/IO。落库与查询见 store.py (store re-export 本模块
     全部名字, `from app.market_cn.auto import store as ds; ds.S_HOLDING` 等旧用法不变)。

设计点:
  - 策略 key 是跨层契约: qd_dragon_signals.strategy 列 / strategies/ 插件注册名 /
    config.json 开关键 三处一致, 改名需三处同改;
  - strategy_labels() 三层: config.json label > 策略插件 name 属性 > key 原样 —
    查得到的 key 必在 config 或磁盘插件二者有其一 (strategy_keys 并集), 无需静态兜底;
  - STRATEGY_WINRATE 为现实化口径常数 (2026-09-09 出场引擎现实化后重测), 仅用于
    前端策略组排序展示, 非实盘承诺。

易错点:
  - 查询/清理范围 (store) 走 strategy_keys() = config.json strategies 段 keys ∪ 磁盘插件
    autodiscover (并集): config 显式声明 (enabled=false 停扫描但不删查询, 历史行仍可见),
    autodiscover 兜底防新增策略漏登记 config 复发"落库后查询看不见";
  - 彻底移除某策略 (插件+config key 都删) 后其历史行退出查询/清理范围, 永久残留属预期;
  - relay3 是已停用策略但历史行仍在表里, key 不能删。
"""
from __future__ import annotations

# 组名与用户 (固定名: 自动策略组, 三策略共用: 龙回头/V1/断板)
DRAGON_GROUP_NAME = "自动策略组"
DRAGON_USER_ID = 1
DRAGON_MARKET = "CNStock"
DRAGON_STRATEGY = "dragon_callback"

# 兜底 (config 与 autodiscover 双双异常时使用, 与磁盘插件保持一致)
_STRATEGIES_FALLBACK = ("dragon_callback", "v1", "break", "relay3", "knife_catch", "tail_oversold")


def strategy_keys():
    """全量策略 key (store 查询/清理范围的单一事实源): config.json ∪ autodiscover 并集。

    快速拔插: 新增策略=丢文件进 strategies/ (+ config.json 写开关/限额), 本文件零改动;
    config keys 显式声明系统成员 (enabled=false 停扫描但历史行仍可查);
    autodiscover 兜底防新增漏登记 config。两边都挂时用 _STRATEGIES_FALLBACK。
    """
    keys: list = []
    try:
        from app.market_cn.auto.strategies import load_config
        keys += [k for k, v in load_config().get("strategies", {}).items()
                 if isinstance(v, dict)]
    except Exception:
        pass
    try:
        from app.market_cn.auto import strategies as _reg
        keys += list(_reg.autodiscover())
    except Exception:
        pass
    if not keys:
        keys = list(_STRATEGIES_FALLBACK)
    return tuple(dict.fromkeys(keys))  # 去重保序


def strategy_labels():
    """策略显示名: config.json label > 策略插件 name 属性 (调用方 .get(key, key) 原样回退)。"""
    out = {}
    try:
        from app.market_cn.auto import strategies as _reg
        for key, s in _reg.all_strategies().items():
            if getattr(s, "name", ""):
                out[key] = s.name
    except Exception:
        pass
    try:
        from app.market_cn.auto.strategies import load_config
        for key, v in load_config().get("strategies", {}).items():
            if isinstance(v, dict) and v.get("label"):
                out[key] = v["label"]
    except Exception:
        pass
    return out


# 历史回测胜率 (全市场验证): 策略组排序用; relay3 = 3板+MA多头 长窗口回测 (2026-09-06)
# 2026-09-09 出场引擎现实化 (T+1/跳空按开盘/跌停顺延, backtest.py + test_dragon 两处同步):
#   v1 = 139笔/72.7%/+3.51%; break = 94笔/71.3%/+4.31% (旧76.5/62.7亦为乐观口径)
# tail_oversold = 尾盘超卖超短 (2026-09-10, test_v2_tail_buy 3个月全市场 275笔/80.7%/+2.74%)
# 2026-09-10 dragon 龙强度门槛 (连板>=3 + 涨停日20日涨幅>=60 + RSI6>=45):
#   dragon_callback = 50笔/62.0%/+2.63% (300d; 旧 117笔/50.9%/+0.21% 为门槛前口径)
STRATEGY_WINRATE = {"v1": 72.7, "break": 71.3, "dragon_callback": 62.0, "relay3": 53.4,
                    "tail_oversold": 80.7,
                    # knife = 全窗口06-05~09-05 36笔/97.2%/+2.0 (现实化口径; 样本集中07恐慌段, 参考性有限)
                    "knife_catch": 97.2}

# 信号状态机 (qd_dragon_signals.state)
S_WATCH_PENDING = "watch_pending"    # D0信号成立, 待D1确认 (默认不入组)
S_BUY_TODAY = "buy_today"            # D1 9:26 gap判定通过, 今日开盘买入 (label 买入·深绿)
S_HOLDING = "holding"                # 已买入持有中 (15:00强/中确认后; label 持仓·蓝)
S_EXIT_TODAY = "exit_today"          # 触发出场 (label 卖出·红; 次日开盘执行)
S_CLOSED = "closed"                  # 已平仓 (组内删行, 留历史)
S_EXPIRED = "expired"                # 失效: 弱确认/开盘gap放弃 (组内删行, 留历史)

# 同步进 qd_watchlist 策略组的状态 (观察票入组: 灰色"观察"置底展示, 09-04 用户要求提前可见)
ACTIVE_GROUP_STATES = (S_WATCH_PENDING, S_BUY_TODAY, S_HOLDING, S_EXIT_TODAY)
# (每策略每日名额的事实源=config.json strategies[key].daily_limit, 经 strat_reg.daily_limit() 读取;
#  曾在此维护的 DAILY_LIMIT_PER_STRATEGY 静态副本于 09-10 删除 — 零消费者且与 config 双事实源)
# label 文案 (前端映射兜底, 前端也有映射)
STATE_LABELS = {S_WATCH_PENDING: "观察", S_BUY_TODAY: "买入", S_HOLDING: "持仓",
                S_EXIT_TODAY: "卖出", S_CLOSED: "已平仓", S_EXPIRED: "已失效"}
