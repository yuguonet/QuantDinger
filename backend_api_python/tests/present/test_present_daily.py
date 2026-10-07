"""test_present_daily.py — P5-③ 前置门禁：展示层切片**每日落盘接线**（2026-10-07）。

本文件锁定「events 落盘」这一环的四件事：
  1. **开关默认关**：`config.json` 顶层 `present_persist` 缺键 = 不落盘 ⇒ 生产行为零变化。
  2. **同源取数**：`asof_inputs` 只收「末根 == date」的票（与判定循环共用
     `kline.asof_bars`）—— 两处各写一份必漂。
  3. **暖机分流**：冷切片才重放历史日；热切片即便传 warmup 也**不重放**
     （重放会把 state 回转、把 g56 台账搅坏，见 present_daily 模块头 ⚠⚠）。
  4. **投影 == 生产（规则列）**：真策略 break × 真实片段，走
     「persist_days → StateStore → load_projection」与
     「scan_signals → signal_row」两条路，规则列逐字段一致（非空断言防假绿）。

另外把一条**已知口径差**钉成显式断言（见文件末），不让它默默漂。
"""

from __future__ import annotations

import json

import pytest

import app.market_cn.auto.strategies as strat_reg
from app.market_cn.auto import present_daily, store
from app.market_cn.auto.core.present.runner import StateStore

from .test_projection import _RAW, _RULE_KEYS, _bars, _break_cls

CODE = "600000"
NAME = "南玻A"


def _cfg(monkeypatch, section):
    """把 config.json 的 present_persist 段换成给定值（开关的**唯一事实源** = config）。"""
    monkeypatch.setattr(strat_reg, "load_config",
                        lambda refresh=False: {"present_persist": section})


# ================================================================
# 开关 / 参数解析
# ================================================================
def test_switch_defaults_off(monkeypatch):
    """默认关 —— 这是「不碰生产」的根据，改成默认开必须是有意为之。"""
    _cfg(monkeypatch, None)                        # 缺键
    assert present_daily.enabled() is False
    _cfg(monkeypatch, {"enabled": True})
    assert present_daily.enabled() is True


def test_warmup_days_parsing_is_fault_tolerant(monkeypatch):
    _cfg(monkeypatch, {"enabled": True})                          # 缺 warmup
    assert present_daily.warmup_days() == 0
    _cfg(monkeypatch, {"enabled": True, "warmup": "abc"})
    assert present_daily.warmup_days() == 0        # 写错不抛异常，退 0
    _cfg(monkeypatch, {"enabled": True, "warmup": 25})
    assert present_daily.warmup_days() == 25
    _cfg(monkeypatch, {"enabled": True, "warmup": -3})
    assert present_daily.warmup_days() == 0        # 负值夹到 0
    _cfg(monkeypatch, "yes")                       # 整段写成字符串（类型不符）
    assert present_daily.enabled() is False and present_daily.warmup_days() == 0


def test_live_config_present_persist_is_wellformed():
    """真实 config.json 的 present_persist 段**形状**自检：值可翻转，形状不可错。

    JSON 手滑 `"enabled": "true"`（字符串）是最常见的一种 —— `bool("true")` 为 True
    ⇒ 静默开启，且天真的读法看不出来；此处钉住。同时保证影子期开关活在 tracked 的
    config.json 里（而不是部署机未跟踪的 `.env`）。
    """
    from app.market_cn.auto.strategies import load_config
    raw = (load_config(refresh=True) or {}).get("present_persist")
    assert isinstance(raw, dict), "config.json 缺 present_persist 段（影子期开关的唯一事实源）"
    assert isinstance(raw.get("enabled"), bool), "present_persist.enabled 必须是 JSON bool"
    assert isinstance(raw.get("warmup"), int) and raw["warmup"] >= 0


def test_shadow_route_is_replay_not_calendar():
    """**阶段门禁**：影子期走**历史回放**，不靠等日历天数。

    ★ 2026-10-07 修正：投影与生产判定都是「bars + 规则」的纯函数 ⇒ 同一天的数据重算
    必得同一判定，「跑够 N 个交易日」只是把同一件事重复 N 遍。故：
      · 验证入口 = `tools.projection_shadow --replay`（一次性回放）；
      · 回放**不得读** `present_persist.enabled` —— 影子期资格与「写路径开关」解耦，
        否则开关一翻（P5-③ 切 writer）回放证据就不可比了。
    本测试锁住这条，防止有人把「按日累积」又当成前置条件加回来。
    """
    import inspect

    from app.market_cn.auto.tools import projection_shadow as shadow
    assert callable(shadow.replay_ready_days), "回放入口不存在 ⇒ 影子期又退化成等时间"
    assert shadow.REPLAY_WINDOW >= 25, (
        f"回放窗口 {shadow.REPLAY_WINDOW} < g56 台账滚动窗 20 ⇒ 窗口开头全是暖机假差异")
    # 2026-10-07 A3: 锁住暖机约束①（此前只锁了回放窗口，真约束无人看守）——
    #   暖机 ≥ **DB 差异模式**窗口 DEFAULT_WINDOW：按日累积的切片只有当天 events，
    #   暖机不足 ⇒ 窗口内历史行全被判 missing（每天一串假差异）。
    #   ⚠ 只约束 DB 差异模式；回放模式一次性构建全量切片，不在此约束内（见 present_daily
    #   模块头注释），故此处比的是 DEFAULT_WINDOW 而非 REPLAY_WINDOW。
    from app.market_cn.auto import present_daily
    _warm = int(present_daily.settings().get("warmup") or 0)
    if _warm:                      # 未配置 ⇒ 切片全关，本约束不适用
        assert _warm >= shadow.DEFAULT_WINDOW, (
            f"暖机 {_warm} < DB 差异模式窗口 {shadow.DEFAULT_WINDOW} ⇒ 按日累积的切片里"
            f"窗口内历史行不在 events ⇒ 全被判 missing（暖机约束①）")
    # 回放自持切片根（默认临时目录）+ 不读写路径开关 ⇒ 与生产按日累积无关
    assert "root" in inspect.signature(shadow.replay_ready_days).parameters, (
        "回放必须能指定切片根（缺省临时目录）—— 否则会去写生产 PRESENT_STATE_DIR")
    assert "enabled()" not in inspect.getsource(shadow.replay_ready_days), (
        "回放读了 present_persist.enabled ⇒ 影子期资格被绑在写路径开关上")


# ================================================================
# 同源取数：as-of 收敛 / 日期并集
# ================================================================
def test_asof_inputs_requires_last_bar_at_date():
    bars = _bars()
    mid = bars[40]["time"]
    di = present_daily.asof_inputs({CODE: bars}, mid, min_bars=1)
    assert list(di) == [CODE]
    assert di[CODE]["today"]["time"] == mid
    assert len(di[CODE]["history"]) == 41          # 截断到 <= mid（含 mid）
    assert di[CODE]["yesterday"]["time"] == bars[39]["time"]
    # 该票在这天没有 bar（日期不在序列里）⇒ 不入输入（停牌/缺数据自然不推进）
    assert present_daily.asof_inputs({CODE: bars}, "1999-01-01", min_bars=1) == {}
    # 根数门槛：41 根 < 门槛 45 ⇒ 空（与判定循环同门槛口径）
    assert present_daily.asof_inputs({CODE: bars}, mid, min_bars=45) == {}


def test_recent_dates_uses_union_not_one_series():
    """暖机窗口取**日期并集** —— 否则某只停牌票的缺口会缩短窗口。"""
    bars = _bars()
    end = bars[-1]["time"]
    seq = present_daily.recent_dates({CODE: bars}, end, 5)
    assert len(seq) == 6 and seq[-1] == end
    assert seq == [b["time"] for b in bars[-6:]]
    # 另一票缺最后 2 根（模拟停牌）：窗口仍以并集为准，不被短序列截断
    assert present_daily.recent_dates({"a": bars[:-2], "b": bars}, end, 5) == seq


# ================================================================
# 落盘：不静默 / 幂等 / 暖机分流
# ================================================================
def test_unmigrated_strategy_is_registered_not_silent(tmp_path):
    """无折叠契约的策略必须**具名登记**跳过（静默降级是头号敌人）。"""

    class _NoFold:
        key = "nofold"

    st = present_daily.persist_days({"nofold": _NoFold()}, [_bars()[-1]["time"]],
                                    {CODE: _bars()}, root=str(tmp_path / "r"),
                                    min_bars=1)
    assert st["skipped"] == ["nofold"]
    assert st["strategies"] == {}                  # 没白跑一遍


def test_persist_writes_record_and_same_day_rerun_is_idempotent(tmp_path):
    s = _break_cls()()
    bars, end = _bars(), _bars()[-1]["time"]
    root = str(tmp_path / "r")

    # 暖机到全片段：保证有 ready→exec→exit 链（否则单日可能恰好无事件 = 假绿）
    st1 = present_daily.persist_days({"break": s}, [end], {CODE: bars},
                                     root=root, warmup=len(bars) - 1, min_bars=1)
    per = st1["strategies"]["break"]
    assert per["days"] > 1 and per["errors"] == [] and per["warmed"] is True

    rec1 = StateStore(root).load("break", CODE)
    assert rec1 is not None and rec1.date == end
    assert rec1.events, "落盘了却没有任何事件流水（配方失效 = 假绿）"
    n1 = len(rec1.events)

    # 同日重跑：advance_all 判 noop ⇒ 事件**不翻倍**（append-only 流水不得重复追加）
    present_daily.persist_days({"break": s}, [end], {CODE: bars}, root=root, min_bars=1)
    assert len(StateStore(root).load("break", CODE).events) == n1
    # 而且 state 不被第二次推进打乱
    assert StateStore(root).load("break", CODE).state == rec1.state


def test_cold_slice_warms_up_but_hot_slice_never_replays(tmp_path):
    """暖机只在**冷切片**触发：热切片重放旧日会回转 state（模块头 ⚠⚠）。"""
    s = _break_cls()()
    bars, end = _bars(), _bars()[-1]["time"]
    root = str(tmp_path / "r")

    st1 = present_daily.persist_days({"break": s}, [end], {CODE: bars},
                                     root=root, warmup=10, min_bars=1)
    p1 = st1["strategies"]["break"]
    assert p1["warmed"] is True and p1["days"] > 1, "冷切片未暖机（warmup 分流失效）"

    st2 = present_daily.persist_days({"break": s}, [end], {CODE: bars},
                                     root=root, warmup=10, min_bars=1)
    p2 = st2["strategies"]["break"]
    assert p2["warmed"] is False, "热切片被重放（会回转 state / 搅坏台账）"
    assert p2["days"] == 1


# ================================================================
# 端到端：切片投影 == 生产信号的规则列
# ================================================================
def _production_rows(s, bars):
    prod = {}
    for k in range(len(bars)):
        for sig in s.scan_signals(bars, CODE, as_of=k):
            row = store.signal_row("break", sig, NAME)
            # 2026-10-07 A2: 生产侧键同样补 entry_style（signal_row 里字段名叫 "style"），
            #   与 store.load_projection / rebuild.load_actual 同粒度，否则两侧键不同构。
            prod[(str(row["signal_date"])[:10], "break", CODE,
                  row.get("style") or "a")] = row
    return prod


def test_projection_rule_columns_match_production_on_real_break(tmp_path):
    """persist→投影 与 scan_signals→signal_row 的规则列逐字段一致（真策略真片段）。"""
    s = _break_cls()()
    bars = _bars()
    root = str(tmp_path / "r")
    present_daily.persist_days({"break": s}, [bars[-1]["time"]], {CODE: bars},
                               root=root, warmup=len(bars) - 1, min_bars=1)

    proj = store.load_projection(root, strategies=["break"], names={CODE: NAME})
    prod = _production_rows(s, bars)
    assert prod and proj, "任一侧为空（配方失效 = 空集对空集假绿）"

    common = set(prod) & set(proj)
    assert common, "两侧无任何共同 (日期,策略,票) —— 断言会变成空转"
    for k in sorted(common):
        diffs = [f for f in _RULE_KEYS
                 if json.dumps(prod[k].get(f), sort_keys=True, default=str) !=
                 json.dumps(proj[k].get(f), sort_keys=True, default=str)]
        assert not diffs, f"{k} 规则列分叉 {diffs}\nprod={prod[k]}\nproj={proj[k]}"


def test_ready_is_stateless_so_projection_equals_production(tmp_path):
    """★★ **口径裁定门禁**（2026-10-07）：切片投影的 ready 日集合 == 生产 ready 日集合。

    裁定原文: **判定(ready) 恒 stateless，生命周期(exec/exit 闭合) 恒 stateful**。
    切片写入器（`runner._fold_step` 走 `evaluate_day`）在持仓 / 入场日**补齐** stateless
    ready，故不再出现「生产有、切片没有」的重合信号日。

    此前这里断言的是**反向**（登记已知口径差 `only_prod 非空`）—— 口径改了，断言必须
    一起改，否则等于用一个过期断言把新行为判成失败。此片段上 `only_prod` 原本非空
    （真实 56 根 break 片段可复现），故它是这条裁定的**有效**回归样本（不是空转）。
    """
    s = _break_cls()()
    bars = _bars()
    root = str(tmp_path / "r")
    present_daily.persist_days({"break": s}, [bars[-1]["time"]], {CODE: bars},
                               root=root, warmup=len(bars) - 1, min_bars=1)
    proj = store.load_projection(root, strategies=["break"])
    prod = _production_rows(s, bars)

    assert prod and proj, "任一侧为空（配方失效 = 空集对空集假绿）"
    assert set(proj) == set(prod), (
        "切片的 ready 日集合 != 生产的 ready 日集合 "
        f"(投影多={sorted(set(proj) - set(prod))}, 生产多={sorted(set(prod) - set(proj))})"
        " —— 「判定恒 stateless」的裁定被破坏")
