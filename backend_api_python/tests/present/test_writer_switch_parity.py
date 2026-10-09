"""test_writer_switch_parity.py — P0-2 切 writer 的**前置证据**（2026-10-09 审计 B-2）。

要回答的问题
--------------
`config.json` 当前 `scan_writer = "scan_day"`（门表引擎 `evaluate.scan_day`），
而 `strategies.scan_writer()` 的**缺省值是 `"record"`**（折叠内核 `Record.ready`）——
即代码默认值已是 record，是config 显式写成 scan_day 才把它旁路掉。

而 `present_daily.persist_days`（生产切片**唯一**写入口）**只被 `_rows_by_record` 调用**
⇒ scan_day 模式下切片永不推进 ⇒ `monitor._progress_map` 读不到切片 ⇒ 恒回退旧判定，
config 承诺的观察灯 `no_judgment==0 / confirm_prog_hit>0` 永不可能过线（审计 B-2）。

切回 record 前必须先证明：两条 writer 的**行**逐字段相同。
这不是形式主义 —— `test_scan_writer.py` 比的是 `record` vs `scan`（旧回滚位），
**从没比过 `record` vs `scan_day`**，而后者才是生产当前值。

断言强度（防"空集假绿"）
------------------------
`_canon(rows)` 逐行 JSON 排序后比对，但**先断言非空**：
两个空集恒相等 = 假绿，这正是本项目点名的坑。
若某策略在夹具上本就不出信号（ready 日集合为空），该策略**显式 skip 并说明**，
不允许静默通过。

覆盖范围
--------
日线 writer 链的三个策略：break / dragon_callback / g56。
knife_catch / tail_oversold 是**盘中**分支（`run_scan_knife` → `IntradayFeed`），
不进 `_run_scan_locked` 的 writer 分派，故不在本文件范围（它们的市场门 as-of 由
`tests/present/test_mkt_gate_asof.py` 的 7 条回归锁覆盖）。
"""

from __future__ import annotations

import importlib
import json
import tempfile

import pytest

from app.market_cn.auto import scan

from .test_projection import _bars, _break_cls

CODE = "600000"
NAME = "南玻A"
#: 片段里真实出信号的一天（`test_projection` 的 ready 日集合含它）
READY_DAY = "2026-06-17"

#: 走 daily writer 链的策略 → (模块, 类名)。break 是 Python 关键字 ⇒ importlib。
_DAILY = {
    "break": ("app.market_cn.auto.strategies.break", "BreakStrategy"),
    "dragon_callback": ("app.market_cn.auto.strategies.dragon_callback", "DragonCallbackStrategy"),
    "g56": ("app.market_cn.auto.strategies.g56", "G56Strategy"),
}


class _NullSampler:
    """采样与 writer 无关（采样器自跑 `scan_signals`）⇒ 替身即可。"""

    def observe(self, *a, **k):
        pass


def _canon(rows):
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)


def _both(key, target=READY_DAY):
    """同一份bars + 同一target ⇒ (scan_day 行, record 行)。"""
    mod, cls = _DAILY[key]
    strat = getattr(importlib.import_module(mod), cls)()
    active = {key: strat}
    bars_by_code = {CODE: _bars()}
    info = {CODE: {"name": NAME}}
    s = _NullSampler()

    sd_rows, sd_err = scan._rows_by_scan_day(active, dict(bars_by_code), target, info, s)
    root = tempfile.mkdtemp(prefix="writer_switch_")
    rec_rows = scan._rows_by_record(active, dict(bars_by_code), target, info, s, root=root)
    return sd_rows, rec_rows, sd_err


@pytest.mark.parametrize("key", sorted(_DAILY))
def test_scan_day_and_record_rows_identical(key):
    """两条 writer 的行**逐字段相同** —— 这是切 writer 的唯一放行条件。"""
    sd_rows, rec_rows, sd_err = _both(key)

    if not sd_rows and not rec_rows:
        pytest.skip(f"{key}: 夹具在该日无信号（空集对空集恒相等 = 假绿，故显式 skip）")

    assert sd_rows or rec_rows, f"{key}: 两条 writer 同时为空 —— 夹具失效"
    assert not sd_err, f"{key}: scan_day 侧判定异常 {sd_err}（不可作为对比基准）"
    assert _canon(sd_rows) == _canon(rec_rows), (
        f"{key}: scan_day 与 record 行不一致 ⇒禁止切 writer。\n"
        f"  scan_day {len(sd_rows)} 行 / record {len(rec_rows)} 行\n"
        f"  仅 scan_day: {sorted(set(_canon(sd_rows)) - set(_canon(rec_rows)))[:2]}\n"
        f"  仅 record  : {sorted(set(_canon(rec_rows)) - set(_canon(sd_rows)))[:2]}"
    )


@pytest.mark.parametrize("key", sorted(_DAILY))
def test_record_writer_advances_slice(key):
    """record writer **确实推进了切片** —— 这是切 writer 的全部意义。

    切片不推进 ⇒ monitor 读不到 progress ⇒恒回退旧判定（B-2 的病灶）。
    所以本条比「行相同」更贴近修复目标：它断言的正是「有人推切片」。

    判据用 `days`（成功推进的**天数**）而非 `advanced`（累计**事件数**）：
    一个无事件的日线策略 `advanced==0` 是正常的（它今天本就不出信号），
    但`days` 必须 ≥1 —— 说明折叠真的被推进过了。
    """
    from app.market_cn.auto import present_daily

    strat = getattr(importlib.import_module(_DAILY[key][0]), _DAILY[key][1])()
    active = {key: strat}
    root = tempfile.mkdtemp(prefix="writer_slice_")

    pstat = present_daily.persist_days(active, [READY_DAY], {CODE: _bars()},
                                       root=root, warmup=0, logger_=None)
    assert key not in (pstat["skipped"] or {}), (
        f"{key}: persist_days 跳过该策略（无折叠契约？）⇒ 切 record 后会退回直判，"
        f"等于白切。跳过原因：{pstat['skipped']}"
    )
    assert not (pstat["strategies"].get(key) or {}).get("errors"), (
        f"{key}: persist_days 推进报错 {(pstat['strategies'][key])['errors']}"
    )
    assert (pstat["strategies"].get(key) or {}).get("days", 0) >= 1, (
        f"{key}: 切片 0 天推进 —— 切 record 后 monitor 仍拿不到 progress"
    )


def test_scan_writer_defaults_to_record(monkeypatch):
    """缺省必须是 `record`（= 有切片推进的路径）。

    用**行为**验证而非源码字符串匹配（字符串匹配一改写法就假失败）：
    把 config 打成空dict ⇒ `scan_writer()` 缺键/非法 ⇒ 必须回 record。
    """
    import app.market_cn.auto.strategies as strat_reg

    monkeypatch.setattr(strat_reg, "load_config", lambda: {})
    assert strat_reg.scan_writer() == "record", (
        "scan_writer() 缺配置时必须走 record（有切片推进），"
        "否则缺配置的生产环境会静默落到无切片路径、monitor 恒回退旧判定"
    )
    for bogus in ({"scan_writer": "typo"}, {"scan_writer": None}, {"scan_writer": 1}):
        monkeypatch.setattr(strat_reg, "load_config", lambda b=bogus: b)
        assert strat_reg.scan_writer() == "record", (
            f"非法值 {bogus} 必须回落 record，不得回落无切片推进的路径"
        )


def test_present_persist_enabled_is_not_the_writer_switch():
    """`present_persist.enabled` **不是** writer 开关（它是死键）。

    审计 B-2 指出它「实际无人消费」：`present_daily.enabled()` 只有 tests 读，
    `_rows_by_record` 是**无条件**调 `persist_days` 的。
    故切 writer 只动 `scan_writer` 一个键；
    若有人后来让 `enabled=false` 也能关掉切片推进，本测试会红 ⇒ 阻止双开关并存打架。
    """
    from app.market_cn.auto import scan as scan_mod

    src_lines = [
        ln for ln in
        (open(scan_mod.__file__, encoding="utf-8").read()).splitlines()
        if "present_daily.enabled" in ln or "present_daily.settings" in ln
    ]
    assert not src_lines, (
        "scan.py 开始读 present_persist.enabled 了 ⇒ 出现了第二个 writer 开关。"
        "唯一 writer 选择器是 config.scan_writer（设计 §2.3 唯一实现）。\n"
        + "\n".join(src_lines)
    )
