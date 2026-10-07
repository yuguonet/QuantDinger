"""test_projection.py — P5-① 门禁：投影写入器（改进方案 §2.7 / §3.7 切口 7）。

验四件事：
  1. **映射唯一性（硬证据）**：真策略 break（真实片段）下，
     `scan_signals → signal_row`（生产入口）与 `Record.events → project_rows`
     （投影入口）对同一信号产的**规则列逐字段一致** —— 这是「投影不是第二份实现」
     的机器证据；分叉 = 影子期永远对不上账。
  2. **事件链 → state 推导**：ready→watch_pending / exec→holding /
     exit（或 exec 自带 exit_price 的两段链）→closed；无 ready → []。
     ★ **每一条 ready 一行**（与库内唯一键同粒度）：同一票多次出信号不得只留首条；
       入场配对用「exec 之前最后一条 ready」。
  3. **操作列永不进入投影**：投影行只含规则列，运行时键（t_legs_today 等）不在其内。
  4. **覆盖度护栏**：切片覆盖不全时必须能被拦下（否则 ghost 是假的 ⇒ 误作废真实行）。

易错点：
  - 空集对空集恒相等（假绿）⇒ 每个断言前先断言**非空**。
  - `break` 是关键字 ⇒ 不能 `from ...strategies.break import`，走 importlib。
  - intraday 策略的 ready.date 带 HH:MM:SS ⇒ 投影必须截到日（与 DATE 列口径一致）。
"""

from __future__ import annotations

import importlib
import json
import os

from app.market_cn.auto import store
from app.market_cn.auto.core.present.contract import DayInput, InsufficientHistory
from app.market_cn.auto.core.present.runner import Record, _progress_to_dict
from app.market_cn.auto.tools import projection_shadow as shadow

#: 规则列（project_record / signal_row 共有的可比字段）
_RULE_KEYS = ("strategy", "code", "name", "board", "style", "score",
              "signal_price", "lu_date", "pullback_days", "signal_date", "extra")


# ================================================================
# 真策略端到端：投影 == 生产（同源硬证据）
# ================================================================
def _break_cls():
    return importlib.import_module(
        "app.market_cn.auto.strategies.break").BreakStrategy


_RAW = (
    ("2026-04-15", 18.561, 19.19, 18.3713, 18.571, 27834791.0),
    ("2026-04-16", 18.581, 19.1701, 18.581, 18.9904, 24363596.0),
    ("2026-04-17", 18.7707, 18.9304, 18.4812, 18.5211, 20165075.0),
    ("2026-04-20", 18.4712, 19.0103, 18.4612, 18.7308, 16148131.0),
    ("2026-04-21", 18.561, 18.6209, 18.0019, 18.1517, 21116897.0),
    ("2026-04-22", 18.2216, 18.5211, 17.9719, 18.4013, 17663246.0),
    ("2026-04-23", 18.3913, 18.601, 18.1517, 18.2914, 15272831.0),
    ("2026-04-24", 18.1616, 18.2715, 17.6225, 17.8921, 18924133.0),
    ("2026-04-27", 17.7323, 18.1716, 17.4727, 17.952, 16937065.0),
    ("2026-04-28", 17.7723, 18.1517, 17.4528, 17.8421, 19529928.0),
    ("2026-04-29", 17.8621, 17.952, 17.6524, 17.7623, 13983465.0),
    ("2026-04-30", 17.7423, 17.8022, 17.2131, 17.313, 19817513.0),
    ("2026-05-06", 17.3928, 18.1517, 17.3928, 17.8122, 28832509.0),
    ("2026-05-07", 17.9719, 18.2914, 17.962, 18.1916, 18417367.0),
    ("2026-05-08", 18.0718, 18.4412, 17.8821, 18.2615, 16689880.0),
    ("2026-05-11", 18.4812, 19.23, 18.1417, 19.0303, 41928467.0),
    ("2026-05-12", 18.9704, 19.23, 18.6709, 18.7907, 21602462.0),
    ("2026-05-13", 18.6209, 19.4297, 18.591, 19.22, 30037808.0),
    ("2026-05-14", 19.3498, 19.4197, 18.3214, 18.3214, 24608320.0),
    ("2026-05-15", 18.2815, 18.7108, 18.1816, 18.3613, 20966907.0),
    ("2026-05-18", 18.7407, 19.2999, 18.6709, 18.8705, 31062396.0),
    ("2026-05-19", 18.8705, 19.9289, 18.7707, 19.5994, 37734026.0),
    ("2026-05-20", 19.9389, 21.3167, 19.7192, 19.9688, 52250554.0),
    ("2026-05-21", 20.1485, 20.4081, 19.0103, 19.1002, 40982577.0),
    ("2026-05-22", 19.22, 19.4297, 18.4712, 19.1601, 35502153.0),
    ("2026-05-25", 19.1601, 19.23, 18.4812, 18.8705, 26494683.0),
    ("2026-05-26", 18.7407, 18.7607, 17.8421, 18.2815, 28725348.0),
    ("2026-05-27", 18.1417, 18.4013, 17.3829, 17.4228, 25909500.0),
    ("2026-05-28", 17.4228, 17.7024, 16.9735, 17.6025, 28728056.0),
    ("2026-05-29", 17.6025, 17.7223, 16.3045, 16.4443, 33710557.0),
    ("2026-06-01", 16.4443, 16.8737, 16.3145, 16.4643, 19561122.0),
    ("2026-06-02", 16.4643, 16.5741, 15.7454, 16.015, 20542190.0),
    ("2026-06-03", 15.97, 16.28, 15.81, 15.94, 16529950.0),
    ("2026-06-04", 15.89, 17.34, 15.66, 16.36, 30995395.0),
    ("2026-06-05", 16.46, 16.48, 15.9, 15.9, 22912160.0),
    ("2026-06-08", 15.49, 15.73, 14.96, 15.1, 20090709.0),
    ("2026-06-09", 15.33, 15.65, 15.15, 15.54, 14586998.0),
    ("2026-06-10", 15.43, 16.54, 15.35, 15.64, 22551796.0),
    ("2026-06-11", 15.8, 17.2, 15.46, 17.2, 36421092.0),
    ("2026-06-12", 17.4, 18.92, 16.6, 18.92, 105555592.0),
    ("2026-06-15", 19.2, 20.81, 18.75, 20.81, 100480861.0),
    ("2026-06-16", 21.47, 22.89, 20.99, 22.89, 101039698.0),
    ("2026-06-17", 23.21, 23.72, 21.8, 23.22, 160479476.0),
    ("2026-06-18", 23.35, 25.54, 23.23, 24.6, 147395482.0),
    ("2026-06-22", 24.46, 25.2, 23.9, 24.59, 90944797.0),
    ("2026-06-23", 24.8, 24.8, 22.51, 23.04, 83519604.0),
    ("2026-06-24", 23.39, 25.34, 22.98, 25.34, 104299285.0),
    ("2026-06-25", 26.48, 27.19, 24.5, 25.68, 110324253.0),
    ("2026-06-26", 25.39, 25.74, 24.17, 24.22, 82946577.0),
    ("2026-06-29", 24.98, 25.22, 22.39, 24.08, 85614561.0),
    ("2026-06-30", 24.05, 25.48, 23.76, 24.95, 68662200.0),
    ("2026-07-01", 25.19, 25.6, 23.82, 24.0, 64562700.0),
    ("2026-07-02", 23.04, 23.52, 21.6, 21.9, 68514900.0),
    ("2026-07-03", 21.63, 22.1, 20.87, 20.97, 49602700.0),
    ("2026-07-06", 20.97, 21.68, 20.05, 21.28, 57592300.0),
    ("2026-07-07", 21.46, 22.39, 20.82, 21.36, 60263800.0),
)


def _bars():
    return [{"time": d, "open": o, "high": h, "low": l, "close": c, "volume": v}
            for d, o, h, l, c, v in _RAW]


def _fold_stateless(strategy, bars, code):
    """最小折叠（fold_range 同序；stateless ⇒ prev 恒 None = 生产投影口径）。"""
    state, j = None, 0
    while j < len(bars):
        try:
            state = strategy.init_state(code, bars[:j])
            break
        except InsufficientHistory:
            j += 1
    assert state is not None, "seed 失败（配方失效）"
    out = []
    for k in range(j, len(bars)):
        out.extend(strategy.evaluate(state, DayInput(code, bars[k], None), None) or [])
        state = strategy.step(state, bars[k])
    return out


def test_projection_equals_signal_row_on_real_break():
    """投影入口(Record.events) 与生产入口(scan_signals→signal_row) 规则列逐字段一致。"""
    s = _break_cls()()
    bars = _bars()

    # 生产侧：逐日 scan_signals（as_of 切片）→ signal_row
    prod = {}
    for k in range(len(bars)):
        for sig in s.scan_signals(bars, "600000", as_of=k):
            row = store.signal_row("break", sig, "南玻A")
            prod[str(row["signal_date"])[:10]] = row

    # 投影侧：stateless fold → 每个 ready 单独投影
    evs = _fold_stateless(s, bars, "600000")
    proj = {}
    for e in evs:
        rec = Record(date=e.date, state={}, events=[_progress_to_dict(e)])
        rows = store.project_rows("break", "600000", rec, name="南玻A")
        assert len(rows) == 1, "一条 ready 应恰好产一行"
        proj[rows[0]["signal_date"]] = rows[0]

    assert prod and proj, "任一侧为空（配方失效 = 空集对空集假绿）"
    assert set(proj) == set(prod), (
        "日期集合不一致: proj-prod=%s prod-proj=%s" % (sorted(set(proj) - set(prod)),
                                                     sorted(set(prod) - set(proj))))
    for d, p in prod.items():
        j = proj[d]
        diffs = [k for k in _RULE_KEYS
                 if json.dumps(p.get(k), sort_keys=True, default=str) !=
                 json.dumps(j.get(k), sort_keys=True, default=str)]
        assert not diffs, "signal_date=%s 规则列分叉: %s\nprod=%s\nproj=%s" % (
            d, diffs, {k: p.get(k) for k in diffs}, {k: j.get(k) for k in diffs})


# ================================================================
# 事件链 → state 推导（合成 events）
# ================================================================
def _record(*events):
    return Record(date="2026-06-17", state={},
                  events=[{"stage": st, "date": d, "payload": pl}
                          for st, d, pl in events])


def test_state_from_chain_watch_and_holding_and_closed():
    ready = ("ready", "2026-06-17", {"price": 10.5, "score": 7, "extra":
                                     {"board": "主板", "lu_date": "2026-06-16",
                                      "pullback_days": 1}})

    r0, = store.project_rows("x", "600000", _record(ready))
    assert r0["state"] == store.S_WATCH_PENDING
    assert r0["trade_date"] == "2026-06-17" and r0["signal_price"] == 10.5
    assert r0["lu_date"] == "2026-06-16" and r0["pullback_days"] == 1
    assert "entry_date" not in r0 and "exit_date" not in r0

    execv = ("exec", "2026-06-18", {"entry_date": "2026-06-18", "entry_price": 10.9})
    r1, = store.project_rows("x", "600000", _record(ready, execv))
    assert r1["state"] == store.S_HOLDING
    assert r1["entry_date"] == "2026-06-18" and r1["entry_price"] == 10.9

    exitv = ("exit", "2026-06-25", {"exit_date": "2026-06-25", "exit_price": 11.8,
                                    "reason": "止盈"})
    r2, = store.project_rows("x", "600000", _record(ready, execv, exitv))
    assert r2["state"] == store.S_CLOSED
    assert r2["exit_price"] == 11.8 and r2["exit_reason"] == "止盈"


def test_self_closed_exec_counts_as_closed():
    """两段链（knife/tail：exec 自带 exit_price）视同已闭合。"""
    ready = ("ready", "2026-06-17", {"price": 10.0, "score": 3, "extra": {}})
    execv = ("exec", "2026-06-18", {"entry_date": "2026-06-17", "entry_price": 10.0,
                                    "exit_price": 10.4, "exit_ret": 4.0,
                                    "label": "D1开盘卖出"})
    r, = store.project_rows("x", "600000", _record(ready, execv))
    assert r["state"] == store.S_CLOSED
    assert r["exit_price"] == 10.4 and r["exit_date"] == "2026-06-18"


def test_no_ready_returns_empty():
    """无 ready（只有 watch/exec 残片）⇒ []，不得凭 exec 造出一行。"""
    watch = ("watch", "2026-06-17", {})
    execv = ("exec", "2026-06-18", {"entry_price": 1.0})
    assert store.project_rows("x", "600000", _record(watch)) == []
    assert store.project_rows("x", "600000", _record(watch, execv)) == []


def test_intraday_timestamp_truncated_to_date():
    """intraday ready.date 带 HH:MM:SS ⇒ 投影截到日（DATE 列口径）。"""
    ready = ("ready", "2026-06-17 14:56:00", {"price": 9.9, "score": 5, "extra": {}})
    r, = store.project_rows("x", "600000", _record(ready))
    assert r["trade_date"] == "2026-06-17" and r["signal_date"] == "2026-06-17"


def test_projection_carries_no_runtime_keys():
    """操作列/运行时键不在投影里（它们由库原地保留，不可推导）。"""
    ready = ("ready", "2026-06-17", {"price": 9.9, "score": 5, "extra": {"board": "主板"}})
    # 即便事件流里被塞了运行时键，也只走规则列映射（extra 整包是策略产出）
    r, = store.project_rows("x", "600000", _record(ready))
    for k in ("id", "created_at", "updated_at", "confirm_date", "d1_chg", "d1_vol_r"):
        assert k not in r, "投影行不应含库侧运行时列 %s" % k


def test_每_ready_一行_且入场配对取_exec_前最后一条_ready():
    """★★ 同一 (策略,票) 多次出信号 ⇒ 每一条 ready 一行（库内唯一键同粒度）。

    入场配对规则: 「开启交易的 ready」= exec **之前最后一条** ready。事件顺序由
    `runner.evaluate_day` 保证 —— 当日补齐的 ready 追加在生命周期事件之后，故它
    不会插在「触发入场的 ready」与其 exec 之间。本测试把这条顺序钉死（顺序写反 =
    把当日那条信号误认成入场信号，signal_date/entry 全错且**不报错**）。
    """
    evs = [("ready", "2026-06-17", {"price": 1.0, "score": 1, "extra": {}}),
           # 持仓期重合信号（D1 收盘又出一条），必须单独成行且不入场
           ("exec", "2026-06-18", {"entry_date": "2026-06-18", "entry_price": 1.05}),
           ("ready", "2026-06-18", {"price": 1.08, "score": 2, "extra": {}}),
           ("exit", "2026-06-22", {"exit_date": "2026-06-22", "exit_price": 1.2}),
           ("ready", "2026-06-24", {"price": 1.3, "score": 3, "extra": {}})]
    rows = store.project_rows("x", "600000", _record(*evs))
    assert len(rows) == 3, f"应三条 ready ⇒ 三行，实得 {len(rows)}"
    assert [r["trade_date"] for r in rows] == ["2026-06-17", "2026-06-18", "2026-06-24"]

    first, second, third = rows
    # 入场/出场只落在**触发入场**的那条 ready 上（06-17，不是 06-18）
    assert first["entry_date"] == "2026-06-18" and first["state"] == store.S_CLOSED
    assert first["exit_date"] == "2026-06-22" and first["exit_price"] == 1.2
    # 持仓期的重合信号是**独立一行**，仍是未推进（生产侧它就停在 watch_pending）
    assert second["signal_price"] == 1.08 and second["state"] == store.S_WATCH_PENDING
    assert "entry_date" not in second
    assert third["state"] == store.S_WATCH_PENDING


def test_gap_rejected_exec_does_not_enter():
    """exec 的 buyable=False（gap 越界）⇒ 该 ready 行不得被推进（旧 entry_decision 口径）。"""
    evs = [("ready", "2026-06-17", {"price": 1.0, "score": 1, "extra": {}}),
           ("exec", "2026-06-18", {"entry_date": "2026-06-18", "entry_price": 1.4,
                                   "buyable": False})]
    r, = store.project_rows("x", "600000", _record(*evs))
    assert r["state"] == store.S_WATCH_PENDING and "entry_date" not in r


def test_load_records_exposes_all_ready_days(tmp_path):
    """`load_records` 与 `load_projection` 都必须给出**全部** ready 日。

    回放对账要比「哪些日出过 ready」—— 用「只取首条」的投影会把同一票后续的 ready 日
    全漏掉（首条只是"当前生命周期位置"，不是信号全集），而库内唯一键
    `(trade_date, strategy, code, entry_style)` 本就是**按日一行** ⇒ 两个入口一致。
    """
    evs = [("ready", "2026-06-17", {"price": 1.0, "score": 1, "extra": {}}),
           ("exit", "2026-06-20", {"exit_price": 1.1}),
           ("ready", "2026-06-24", {"price": 1.2, "score": 2, "extra": {}})]
    payload = {"codes": {"600000": _record(*evs).to_json()}, "meta": {}}
    with open(os.path.join(tmp_path, "break.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)

    recs = store.load_records(str(tmp_path))
    assert set(recs) == {"break"} and len(recs["break"]["600000"].events) == 3
    days = {e["date"] for e in recs["break"]["600000"].events if e["stage"] == "ready"}
    assert days == {"2026-06-17", "2026-06-24"}, "load_records 丢了 ready 日"

    proj = store.load_projection(str(tmp_path))
    # 2026-10-07 A2: 键含第 4 元素 entry_style（与库唯一键同粒度）—— 同一 (票,日) 可出
    #   多条不同 entry_style 的 ready，三元组键会互相覆盖 ⇒ 少行。
    #   style 取值随 fixture 用的策略而变（mock→'a' / 真 break→'brk'）⇒ 断言**前三维**，
    #   第 4 元素单独断言非空 —— 既守住「按日一行不丢」，又不写死具体 style。
    assert {(d, s, c) for (d, s, c, _e) in proj} == {("2026-06-17", "break", "600000"),
                                                     ("2026-06-24", "break", "600000")}, \
        "load_projection 丢了后续 ready 日（库内唯一键是 (日,策略,票,entry_style) 一行）"
    assert all(_e for (_d, _s, _c, _e) in proj), "投影键缺 entry_style（四元组键必须齐）"
    _k = next(k for k in proj if k[:3] == ("2026-06-24", "break", "600000"))
    assert proj[_k]["signal_price"] == 1.2


def test_replay_report_flags_both_directions():
    """回放报告必须把**两个方向**都打出来（只报一个方向 = 漏一半分歧）。"""
    res = {"win": ("2026-06-01", "2026-08-28"), "window": 60, "root": "X", "bars": 100,
           "errors": [], "warm_cut": "2026-06-29",
           "skipped_no_fold": ["v1"],
           "skipped_intraday": [("knife_catch", "intraday_window")],
           "per_strategy": {"break": {
               "codes": 2, "prod_days": 3, "proj_days": 2, "both": 1,
               "only_prod": 2, "only_proj": 1, "only_prod_ex_warm": 1,
               "only_proj_ex_warm": 1,
               "by_date_prod_only": {"2026-07-01": 2},
               "by_date_proj_only": {"2026-07-02": 1},
               "examples_prod_only": [("600000", ["2026-07-01"])],
               "examples_proj_only": [("000001", ["2026-07-02"])]}},
           "total": {"codes": 2, "prod_days": 3, "proj_days": 2, "both": 1,
                     "only_prod": 2, "only_proj": 1},
           "total_ex_warm": {"only_prod": 1, "only_proj": 1, "attr_in_position": 1,
                             "skip_days": 20}}
    txt = shadow._render_replay(res)
    assert "2026-06-01..2026-08-28" in txt and "只生产" in txt and "只投影" in txt
    # 两类别名登记必须出现（静默跳过是头号敌人）
    assert "v1" in txt and "knife_catch" in txt
    assert "600000" in txt and "000001" in txt
    # 暖机剔除 + 归因必须单列（否则 g56 冷启动假象会被当成真分歧、口径差会被当万能挡箭牌）
    assert "剔除暖机头 20 日" in txt and "2026-06-29" in txt and "未归因" in txt


def test_replay_rc_uses_warmup_excluded_verdict():
    """回放退出码以**暖机剔除后**的口径为准（与报告一致）。

    病根（2026-10-07 背证实证）：rc 取原始 `total` ⇒ 冷回放窗口开头的 g56 台账暖机
    假象（ROLL=20）也会 rc=1，与报告自身「这批才算真分歧」自相矛盾 ⇒ 恒定假警报
    训练人忽略退出码（「噪声废掉 fail-fast」）。故：纯暖机差 ⇒ 0；真分歧 ⇒ 1。
    """
    warm_only = {"total": {"only_prod": 1, "only_proj": 0},
                 "total_ex_warm": {"only_prod": 0, "only_proj": 0}}
    assert shadow._replay_rc(warm_only) == 0, "纯暖机假象不得当失败（假警报废掉 fail-fast）"

    real = {"total": {"only_prod": 2, "only_proj": 0},
            "total_ex_warm": {"only_prod": 1, "only_proj": 0}}
    assert shadow._replay_rc(real) == 1, "暖机剔除后仍有真分歧 ⇒ 必须非零退出"

    real_proj = {"total": {"only_prod": 0, "only_proj": 3},
                 "total_ex_warm": {"only_prod": 0, "only_proj": 3}}
    assert shadow._replay_rc(real_proj) == 1, "只投影有（多发）同样是非零"

    # 无 total_ex_warm（老结构 / 手造 res）⇒ 退回原始 total，不静默放过
    legacy = {"total": {"only_prod": 0, "only_proj": 1}}
    assert shadow._replay_rc(legacy) == 1, "缺 total_ex_warm 时须退回 total，不得当 0"


def test_replay_rc_counts_row_mismatch():
    """★ P5-③：重合日**规则列不一致**同样是失败（行级口径，不能只在报告里可见）。

    只比「哪些日出 ready」会漏掉 payload 分叉 —— 日期集合相同、signals 表长相不同。
    """
    res = {"total": {"only_prod": 0, "only_proj": 0},
           "total_ex_warm": {"only_prod": 0, "only_proj": 0},
           "row_parity": {"mismatch_ex_warm": 2}}
    assert shadow._replay_rc(res) == 1, "行级不一致必须走到退出码（否则门禁形同虚设）"
    res["row_parity"] = {"mismatch": 3, "mismatch_ex_warm": 0}
    assert shadow._replay_rc(res) == 0, "暖机头内的行级差与日集合口径一致，不算真分歧"


def test_replay_report_shows_row_parity():
    """回放报告必须打出**行级**对账（否则「signals 零差异」这条门禁没有可见证据）。"""
    res = {"win": ("2026-06-01", "2026-08-28"), "window": 60, "root": "X", "bars": 100,
           "errors": [], "warm_cut": "2026-06-29",
           "skipped_no_fold": [], "skipped_intraday": [], "per_strategy": {},
           "total": {"codes": 0, "prod_days": 0, "proj_days": 0, "both": 0,
                     "only_prod": 0, "only_proj": 0},
           "total_ex_warm": {"only_prod": 0, "only_proj": 0, "attr_in_position": 0,
                             "skip_days": 20},
           "row_parity": {"compared": 7, "mismatch": 3, "mismatch_ex_warm": 1,
                          "fields": {"extra": 3},
                          "examples": [("600000", "2026-07-01", ["extra"],
                                        {"extra": {"a": 1}}, {"extra": {}})]}}
    txt = shadow._render_replay(res)
    assert "规则列逐字段" in txt and "比对 7 条" in txt and "不一致 3 条" in txt
    assert "剔暖机后 1" in txt and "extra" in txt and "600000" in txt


# ================================================================
# 切片装载 + 覆盖度护栏
# ================================================================
def test_load_projection_reads_slice_file(tmp_path):
    """load_projection 从切片文件读出 Record → {(date, strategy, code): row}。"""
    ready = ("ready", "2026-06-17", {"price": 9.9, "score": 5,
                                     "extra": {"board": "主板"}})
    rec = _record(ready).to_json()
    payload = {"codes": {"600000": rec}, "meta": {}}
    with open(os.path.join(tmp_path, "break.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)

    exp = store.load_projection(str(tmp_path))
    assert exp, "切片装载为空（假绿）"
    k = next(k for k in exp if k[:3] == ("2026-06-17", "break", "600000"))
    assert k in exp and exp[k]["signal_price"] == 9.9


def test_coverage_guard_flags_undercovered_slice():
    """切片覆盖不全时覆盖率被标出（护栏核心：否则 ghost 是假的 ⇒ 误作废）。"""
    expected = {("2026-06-17", "break", "600000"): {"state": store.S_WATCH_PENDING}}
    actual = {("2026-06-17", "break", "600000"): {"state": store.S_WATCH_PENDING},
              ("2026-06-17", "break", "000001"): {"state": store.S_WATCH_PENDING},
              ("2026-06-17", "break", "000002"): {"state": store.S_WATCH_PENDING}}
    rep = shadow.coverage_report(expected, actual, ["break"])
    assert rep["break"]["pending"] == 3 and rep["break"]["covered"] == 1
    assert rep["break"]["ratio"] < shadow.MIN_COVERAGE, "覆盖不足未被标记 ⇒ 护栏失效"


def test_coverage_guard_ignores_settled_rows():
    """已推进行不进覆盖度分母（它们永不被写，不该影响比例）。"""
    expected = {("2026-06-17", "break", "600000"): {"state": store.S_WATCH_PENDING}}
    actual = {("2026-06-17", "break", "600000"): {"state": store.S_WATCH_PENDING},
              ("2026-06-17", "break", "000001"): {"state": store.S_HOLDING,
                                                  "entry_date": "2026-06-18"}}
    rep = shadow.coverage_report(expected, actual, ["break"])
    assert rep["break"]["pending"] == 1 and rep["break"]["ratio"] == 1.0


def test_prune_insert_only_unadvanced():
    """投影只补未推进行：state=holding/closed 的 missing 行被裁下（不得凭空造观察票）。"""
    plan = {"insert": [{"state": store.S_WATCH_PENDING, "code": "1"},
                       {"state": store.S_HOLDING, "code": "2"},
                       {"state": store.S_CLOSED, "code": "3"}],
            "expire": [], "fix": []}
    out, dropped = shadow.prune_insert(plan)
    assert [r["code"] for r in out["insert"]] == ["1"], "非未推进行混入了 insert"
    assert [r["code"] for r in dropped] == ["2", "3"], "已入场/终态行未被裁下"
    assert out["not_inserted"] == dropped
    assert plan["insert"] == [{"state": store.S_WATCH_PENDING, "code": "1"},
                              {"state": store.S_HOLDING, "code": "2"},
                              {"state": store.S_CLOSED, "code": "3"}], "原 plan 被就地改动"
