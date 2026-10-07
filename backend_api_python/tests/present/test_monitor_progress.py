"""P5-④ 第一步: monitor 15:01 确认的判定源切 RealtimeBranch progress (2026-10-07)。

只测**接线与失败方向** —— 这些是资金安全面, 真实端到端等价性由 `present_verify` C2
(实时分支 == 预处理末位判定) 与 P5 影子期回放对账覆盖, 不在此重复。

  1. 缺省关          ⇒ 返回空 (生产行为逐字不变, 这是回滚位);
  2. 开 + 切片无 Record ⇒ 返回空 (★ 不得把"没判到"当成"判定为持有");
  3. tick 抛异常      ⇒ 返回空 + stats 计数 (不静默吞, 见项目「不静默」纪律);
  4. tick 有判定      ⇒ 按 (key, code) 返回 progress 供 step5 使用。

★ 第 2 条是本文件最重要的断言: monitor 的确认会写 `S_HOLDING`(持仓) —— 若把"新判定源
没数据"当成"判定为继续持有", 就是**静默把没判过的票转成持仓**, 是实盘资金事故而不是
降级。故 `_confirm_progress` 只认真正拿到的判定, 拿不到一律交回旧路径 `confirm_decision`。
"""
from __future__ import annotations

import pytest

from app.market_cn.auto import monitor as mon
from app.market_cn.auto import present_daily
from app.market_cn.auto import strategies as strat_reg
from app.market_cn.auto.core.present import RealtimeBranch
from app.market_cn.auto.core.present.contract import Progress
from app.market_cn.auto.strategies.knife_catch import KnifeCatchStrategy

CODE = "600000"
SERIES = {CODE: [{"time": "2026-10-05", "last": 10.5, "open": 10.1,
                  "high": 10.6, "low": 10.0, "volume": 100}]}


def _row() -> dict:
    return {"id": 7, "code": CODE, "strategy": "knife_catch",
            "entry_date": "2026-10-05", "signal_price": 10.0, "entry_price": 10.1}


@pytest.fixture
def _on(monkeypatch, tmp_path):
    """打开开关 + 切片根指到临时目录 + 策略实例由注入给出 (不依赖注册表名)。"""
    s = KnifeCatchStrategy()
    monkeypatch.setattr(strat_reg, "monitor_progress_settings",
                        lambda: {"enabled": True})
    monkeypatch.setattr(present_daily, "default_root", lambda: str(tmp_path))
    monkeypatch.setattr(mon, "_strategy_of", lambda r: s)
    return s


def test_default_off_returns_empty(monkeypatch):
    """缺省关 (config 无 monitor_progress 键) ⇒ 不产判定, 生产行为零变化。

    同 `test_switch_defaults_off`：注入空 config，不依赖生产 config 当前值。
    """
    monkeypatch.setattr(strat_reg, "load_config", lambda *a, **kw: {})
    assert mon._progress_map([_row()], SERIES, "15:01") == {}


def test_no_record_returns_empty(_on):
    """★ 开 + 切片里没有 Record ⇒ 返回空, 由调用方回退 confirm_decision。

    不是"判定为持有": 空字典 = 明确表达"我没判到", 调用方不得据此转 S_HOLDING。
    """
    out = mon._progress_map([_row()], SERIES, "15:01")
    assert out == {}, "无 Record 时必须返回空 (不得暗示'判定为持有')"


def test_tick_error_is_recorded_not_swallowed(_on, monkeypatch):
    """tick 抛异常 ⇒ 返回空 + stats 计数 (静默吞是头号敌人)。"""

    def boom(self, hhmm, codes, snaps, series_by_code=None, mkt_gain=None):
        raise RuntimeError("切片损坏")

    monkeypatch.setattr(RealtimeBranch, "tick", boom)
    st: dict = {}
    out = mon._progress_map([_row()], SERIES, "15:01", stats=st)
    assert out == {}
    assert st.get("confirm_prog_err") == 1, f"异常必须计数, 实得 {st}"


def test_progress_is_returned_keyed_by_strategy_and_code(_on, monkeypatch):
    """tick 给出 exit 判定 ⇒ 按 (strategy.key, code) 返回, 供 step5 转 exit_today。"""
    prog = Progress(stage="exit", date="2026-10-05",
                    payload={"exit_reason": "止损", "exit_price": 9.8})

    def fake(self, hhmm, codes, snaps, series_by_code=None, mkt_gain=None):
        return [(CODE, prog)]

    monkeypatch.setattr(RealtimeBranch, "tick", fake)
    out = mon._progress_map([_row()], SERIES, "15:01")
    assert out == {(_on.key, CODE): prog}, f"实得 {out}"


def test_no_snapshot_rows_returns_empty(_on):
    """当日无快照 ⇒ 连试推都做不了 ⇒ 返回空 (与无 Record 同一失败方向)。"""
    assert mon._progress_map([_row()], {}, "15:01") == {}


def test_switch_defaults_off(monkeypatch):
    """★ 缺省必须关 —— 这是「生产行为逐字不变」的保证, 也是 P5-④ 的回滚位。

    config.json 没有 `monitor_progress` 键 ⇒ False ⇒ 三个判定点全部走旧路径。

    ⚠ 必须**注入**空 config 而不是依赖仓库里的 config.json 当前值：本断言测的是
    「缺键 ⇒ 关」这条规则，与生产现在开着还是关着无关。否则 P5-④ 一翻开关，这条
    守卫回滚位的测试就会假红（2026-10-07 实测）。
    """
    monkeypatch.setattr(strat_reg, "load_config", lambda *a, **kw: {})
    assert mon._progress_enabled() is False
