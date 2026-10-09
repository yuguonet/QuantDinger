#!/usr/bin/env python3
"""auto/registry.py — 自动策略组注册表 (策略元数据单一事实源, 2026-09-10 自 dragon_store 拆出)

用途: 策略 key/中文名/历史胜率/每日名额/信号状态机等**纯元数据**集中于此;
     不做任何 DB/IO。落库与查询见 store.py (store re-export 本模块
     全部名字, `from app.market_cn.auto import store as ds; ds.S_HOLDING` 等旧用法不变)。

设计点:
  - 策略 key 是跨层契约: qd_dragon_signals.strategy 列 / strategies/ 插件注册名 /
    config.json strategies 段 key 三处一致, 改名需三处同改;
    (策略开关 enabled 2026-10-09 已迁至**策略宏 <key>.yaml 的 meta.enabled** —
     见 strategies.is_enabled; config 的 enabled 键仅服务无 yaml 的遗留策略);
  - strategy_labels() 三层: config.json label > 策略插件 name 属性 > key 原样 —
    查得到的 key 必在 config 或磁盘插件二者有其一 (strategy_keys 并集), 无需静态兜底;
  - 历史胜率等展示型元数据 (winrate) 单一事实源 = config.json strategies[key].winrate;
    状态显示文案 = config.json state_labels; 二者均经 strategy_winrate()/state_label()
    读取, 代码不再写死 (2026-09-18 迁出)。

易错点:
  - 查询/清理范围 (store) 走 strategy_keys() = config.json strategies 段 keys ∪ 磁盘插件
    autodiscover (并集): config 显式声明 (enabled=false 停扫描但不删查询, 历史行仍可见),
    autodiscover 兜底防新增策略漏登记 config 复发"落库后查询看不见";
  - 彻底移除某策略 (插件+config key 都删) 后其历史行退出查询/清理范围, 永久残留属预期
    (2026-10-09 P6-6/7: relay3/v1/lead_chase 按此彻底退役 ⇒ 其历史 DB 行不再进查询/
    清理范围; 若库中仍存其未平仓行需人工处置)。
"""
from __future__ import annotations


def _config():
    """读取 config.json (带异常兜底, 失败返回空 dict); 仅取元数据, 不做任何 IO 持久化。"""
    try:
        from app.market_cn.auto.strategies import load_config
        return load_config() or {}
    except Exception:
        return {}


# 组名与用户 (固定名: 自动策略组, 全活跃策略共用)
# ⚠ 2026-10-09 文档漂移已修: 原注释列举"龙回头/V1/断板", 其中 V1 已退役归档
#   (strategies/_archive/, P6-6/7); 现改为不枚举具体策略, 免得再次漂移。
DRAGON_GROUP_NAME = "自动策略组"
DRAGON_USER_ID = 1
DRAGON_MARKET = "CNStock"
DRAGON_STRATEGY = "dragon_callback"

# 兜底 (config 与 autodiscover 双双异常时使用, 与磁盘插件保持一致)
# 2026-09-26: 补 g56/break_v2/dragon_v2/triple_resonance; 去掉已归档的
# dragon_callback_legacy (见 strategies/_archive/)。改名/增删策略时同步此表。
# 2026-09-27: break_v2/dragon_v2/triple_resonance/t_hilo 归档 _archive/, 同步删 fallback
# 2026-10-09: v1/relay3/lead_chase 退役归档 _archive/ (P6-6/7), 同步删 fallback
_STRATEGIES_FALLBACK = (
    "dragon_callback", "break",
    "knife_catch", "tail_oversold", "g56",
)


def strategy_keys():
    """全量策略 key (store 查询/清理范围的单一事实源): config.json ∪ autodiscover 并集。

    Returns:
        tuple[str, ...]: 全量策略 key（去重保序）。

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


def enabled_keys():
    """当前 enabled=true 的策略 key (显示层过滤的单一事实源)。

    与 strategy_keys() 的区别: 后者是"系统成员"全集 (含 enabled=false, 为的是历史行可查);
    本函数是"现在还在产信号的"子集 —— 展示层用它实现「只显示 enabled=true」。

    ★ 注意: 停用策略的**已入场行** (entry_date 非空) 仍必须可见, 否则用户会遗忘
    手上还有票要卖 (实盘资金事故)。过滤条件由调用方 (store.list_signals) 组合,
    本函数只回答"哪些 key 是启用的"。
    """
    try:
        from app.market_cn.auto import strategies as _reg
        _reg.autodiscover()
        keys = [k for k in strategy_keys() if _reg.is_enabled(k)]
        # 全空时回退全集: 若 config 读取异常, 宁可多显示也不要把界面清空
        return tuple(keys) if keys else strategy_keys()
    except Exception:
        return strategy_keys()


def strategy_labels():
    """策略显示名: config.json label > 策略插件 name 属性 (调用方 .get(key, key) 原样回退)。

    Returns:
        dict: {策略key: 显示名}；缺失时调用方回退用 key。
    """
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


# 历史回测胜率 (前端策略组排序/展示用, 非实盘承诺): 单一事实源已迁 config.json
#   strategies[key].winrate (2026-09-18 迁出, 代码不再写死胜率数值)。取值见 strategy_winrate()。
# 各策略现实化口径基线 (仅作参考, 不在代码写死, 全部以 config 为准):
#   break=94笔/71.3%/+4.31%; dragon_callback=50笔/62.0%/+2.63% (300d, 门槛后);
#   tail_oversold=275笔/80.7%/+2.74%; knife_catch=36笔/97.2% (样本集中07恐慌段)。
#   (v1/relay3 的胜率随其 2026-10-09 退役一并移除; 历史值见 docs/策略研究依据归档.md)
def strategy_winrate(key):
    """历史回测胜率 (前端策略组排序/展示用, 非实盘承诺)。

    单一事实源 = config.json strategies[key].winrate; 未配置返回 None。
    """
    try:
        v = _config().get("strategies", {}).get(key, {}).get("winrate")
        return float(v) if v is not None else None
    except Exception:
        return None

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
# 信号状态显示文案 (前端映射兜底, 前端也有映射): 单一事实源已迁 config.json
#   state_labels (2026-09-18 迁出, 代码不再写死); 见 state_label()。
def state_label(state):
    """状态中文显示名; 未配置回退到状态原串 (如 watch_pending)。"""
    try:
        return _config().get("state_labels", {}).get(state, state)
    except Exception:
        return state
