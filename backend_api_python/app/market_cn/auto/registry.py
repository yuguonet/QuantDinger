#!/usr/bin/env python3
"""auto/registry.py — 自动策略组注册表 (策略元数据单一事实源, 2026-09-10 自 dragon_store 拆出)

用途: 策略 key/中文名/历史胜率/每日名额/信号状态机等**纯元数据**集中于此;
     不做任何 DB/IO。落库与查询见 store.py (store re-export 本模块
     全部名字, `from app.market_cn.auto import store as ds; ds.S_HOLDING` 等旧用法不变)。

设计点:
  - 策略 key 是跨层契约: qd_dragon_signals.strategy 列 / strategies/ 插件注册名 /
    config.json 开关键 三处一致, 改名需三处同改;
  - strategy_labels() 动态优先: strategies 注册表 name 覆盖静态 STRATEGY_LABELS
    (历史行可能含已删策略键, 静态表兜底);
  - STRATEGY_WINRATE 为现实化口径常数 (2026-09-09 出场引擎现实化后重测), 仅用于
    前端策略组排序展示, 非实盘承诺。

易错点:
  - STRATEGIES 元组驱动 list_signals/cleanup 的查询范围, 新策略上线必须加入,
    否则信号落库后查询/清理都看不见;
  - relay3 是已停用策略但历史行仍在表里, key 不能删。
"""
from __future__ import annotations

# 组名与用户 (固定名: 自动策略组, 三策略共用: 龙回头/V1/断板)
DRAGON_GROUP_NAME = "自动策略组"
DRAGON_USER_ID = 1
DRAGON_MARKET = "CNStock"
DRAGON_STRATEGY = "dragon_callback"
STRATEGIES = ("dragon_callback", "v1", "break", "relay3")
STRATEGY_LABELS = {"dragon_callback": "龙回头", "v1": "V1", "break": "断板", "relay3": "3板接力"}


def strategy_labels():
    """策略中文名: 注册表 name 优先, STRATEGY_LABELS 静态兜底 (历史行可能含已删策略键)。"""
    labels = dict(STRATEGY_LABELS)
    try:
        from app.market_cn.auto import strategies as _reg
        for key, s in _reg.all_strategies().items():
            if getattr(s, "name", ""):
                labels[key] = s.name
    except Exception:
        pass
    return labels


# 历史回测胜率 (全市场验证): 策略组排序用; relay3 = 3板+MA多头 长窗口回测 (2026-09-06)
# 2026-09-09 出场引擎现实化 (T+1/跳空按开盘/跌停顺延, backtest.py + test_dragon 两处同步):
#   v1 = 139笔/72.7%/+3.51%; break = 94笔/71.3%/+4.31% (旧76.5/62.7亦为乐观口径)
# tail_oversold = 尾盘超卖超短 (2026-09-10, test_v2_tail_buy 3个月全市场 275笔/80.7%/+2.74%)
# 2026-09-10 dragon 龙强度门槛 (连板>=3 + 涨停日20日涨幅>=60 + RSI6>=45):
#   dragon_callback = 50笔/62.0%/+2.63% (300d; 旧 117笔/50.9%/+0.21% 为门槛前口径)
STRATEGY_WINRATE = {"v1": 72.7, "break": 71.3, "dragon_callback": 62.0, "relay3": 53.4,
                    "tail_oversold": 80.7}

# 信号状态机 (qd_dragon_signals.state)
S_WATCH_PENDING = "watch_pending"    # D0信号成立, 待D1确认 (默认不入组)
S_BUY_TODAY = "buy_today"            # D1 9:26 gap判定通过, 今日开盘买入 (label 买入·深绿)
S_HOLDING = "holding"                # 已买入持有中 (15:00强/中确认后; label 持仓·蓝)
S_EXIT_TODAY = "exit_today"          # 触发出场 (label 卖出·红; 次日开盘执行)
S_CLOSED = "closed"                  # 已平仓 (组内删行, 留历史)
S_EXPIRED = "expired"                # 失效: 弱确认/开盘gap放弃 (组内删行, 留历史)

# 同步进 qd_watchlist 策略组的状态 (观察票入组: 灰色"观察"置底展示, 09-04 用户要求提前可见)
ACTIVE_GROUP_STATES = (S_WATCH_PENDING, S_BUY_TODAY, S_HOLDING, S_EXIT_TODAY)
# 每策略每日买入名额 (09-04 用户要求: 每策略每天≈5笔, 质量排名末位淘汰; relay3 信号稀少 n≈0.7/日, 名额2)
DAILY_LIMIT_PER_STRATEGY = {"dragon_callback": 5, "v1": 5, "break": 5, "relay3": 2}
# label 文案 (前端映射兜底, 前端也有映射)
STATE_LABELS = {S_WATCH_PENDING: "观察", S_BUY_TODAY: "买入", S_HOLDING: "持仓",
                S_EXIT_TODAY: "卖出", S_CLOSED: "已平仓", S_EXPIRED: "已失效"}
