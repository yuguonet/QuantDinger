"""test_limit_up_buy_guard.py — 涨停阻买（2026-10-10 审计 #8-2 / 修复核对 §3）。

事故
----
`market.limit_up_price`（2026-09-29「涨停阻买」修复）的唯一调用点在
`core/entry_modes.py` —— 一条**死路径**（零消费）⇒ 该安全修复不覆盖任何活路径：
开盘封死涨停（一字/触板）的票在 break（"恒可买"）、dragon（gap 过滤已移除）的
exec/entry_decision 里会被当成**可买**，回测造出市场上买不到的成交。

修复（唯一实现 + 市场事实）
--------------------------
`core/exec.fill_blocked_by_limit_up(fill, up, *, tol=0.0)` ——
`fill_blocked_by_limit_dn` 的买侧镜像，涨停阻买的**唯一**判定入口。
挂点（全部走同一原语）：
  - break/dragon/g56 折叠 `exec` 事件的 `buyable` 旗标（replay/monitor/store 三消费者
    本就认 `buyable is False`）；
  - break/dragon/g56 `entry_decision`（monitor 旧路径 + rebuild 重放同口径）；
  - knife `_gates`（fold 与 scan_signals 共用的单点，覆盖两条生产路径）；
  - monitor 开盘买入步的通用市场事实守卫（对所有策略生效）。
g56 旧 `GAP_LIM` / `lim=0.198/0.098` 硬编码**语义逐位等价**（严格达到名义涨停价才拒）。

口径
----
- 默认 `tol=0.0` = 严格达到名义涨停价才拒（g56 旧口径零漂移）；
- `fill_intraday` 买腿保留其历史容差（`tol=_limit_dn_tol(spec)`）。
"""

from __future__ import annotations

import importlib

_exec = importlib.import_module("app.market_cn.auto.core.exec")
_market = importlib.import_module("app.market_cn.auto.core.market")
_break = importlib.import_module("app.market_cn.auto.strategies.break")
_g56 = importlib.import_module("app.market_cn.auto.strategies.g56")
_dragon = importlib.import_module("app.market_cn.auto.strategies.dragon_callback")
_knife = importlib.import_module("app.market_cn.auto.strategies.knife_catch")

fill_blocked_by_limit_up = _exec.fill_blocked_by_limit_up
fill_intraday = _exec.fill_intraday
limit_up_price = _market.limit_up_price
get_board_type = _market.get_board_type


def _bar(d, o, h, l, c, v=1e6):
    return {"time": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ================================================================
# 1. 原语语义（红/绿在原语层先锁死）
# ================================================================

class TestPrimitive:
    def test_main_board_limit_price(self):
        # 名义涨停价 = 昨收 × 1.098（main）/ × 1.198（gem_star）
        assert abs(limit_up_price(100.0, "main") - 109.8) < 1e-9
        assert abs(limit_up_price(100.0, "gem_star") - 119.8) < 1e-9

    def test_blocked_exactly_at_limit(self):
        up = limit_up_price(100.0, "main")
        assert fill_blocked_by_limit_up(up, up) is True

    def test_green_below_limit(self):
        up = limit_up_price(100.0, "main")
        assert fill_blocked_by_limit_up(up - 0.01, up) is False

    def test_no_limit_market_passthrough(self):
        # 无涨跌停市场（up=0 / None）不判定 —— 与 fill_blocked_by_limit_dn 同约定
        assert fill_blocked_by_limit_up(999.0, 0.0) is False
        assert fill_blocked_by_limit_up(999.0, None) is False

    def test_tol_widens_block(self):
        up = 109.8
        assert fill_blocked_by_limit_up(up * 0.999, up) is False          # tol=0 不拦
        assert fill_blocked_by_limit_up(up * 0.999, up, tol=0.002) is True  # 带容差拦

    def test_fill_intraday_buy_blocked_at_limit(self):
        # fill_intraday 买腿：触线成交价贴涨停 → filled=False（历史容差语义保留）
        bar = _bar("2026-01-05", 109.8, 110.0, 108.0, 109.9)
        up = limit_up_price(100.0, "main")
        fill, filled = fill_intraday(bar, 109.8, side="buy", up=up)
        assert filled is False
        # 红/绿对：同结构、远离涨停 → 正常成交
        bar2 = _bar("2026-01-05", 105.0, 106.0, 104.0, 105.5)
        fill2, filled2 = fill_intraday(bar2, 105.0, side="buy", up=up)
        assert filled2 is True and fill2 == 105.0


# ================================================================
# 2. break：exec 事件 buyable 旗标 + entry_decision（原来恒可买 = 红）
# ================================================================

class TestBreak:
    def _state(self, prev_close=10.0):
        return {"v": 1, "board": "main", "abs_i": 100,
                "win": [{"t": "2026-01-02", "o": 9.9, "h": 10.1, "l": 9.8,
                         "c": prev_close, "v": 1e6}]}

    def _exec_payload(self, open_px):
        from app.market_cn.auto.core.present.contract import DayInput
        st = _break.BreakStrategy()
        inp = DayInput("600000", _bar("2026-01-05", open_px, open_px, open_px, open_px), None)
        evs = st._exec_event(self._state(), inp, None)
        assert evs, "open>0 应产 exec 事件"
        return evs[0].payload

    def test_red_open_at_limit_not_buyable(self):
        # 昨收 10.0 → 涨停价 10.98；开盘 10.98 = 一字涨停，物理买不进
        pl = self._exec_payload(10.98)
        assert pl["buyable"] is False

    def test_green_open_below_limit_buyable(self):
        pl = self._exec_payload(10.9)
        assert pl["buyable"] is True

    def test_entry_decision_red_green(self):
        st = _break.BreakStrategy()
        row = {"code": "600000"}
        red = st.entry_decision(row, snap={"open": 10.98, "previousClose": 10.0})
        assert red.buyable is False and "涨停" in red.reason
        green = st.entry_decision(row, snap={"open": 10.5, "previousClose": 10.0})
        assert green.buyable is True

    def test_exit_event_suppressed_when_not_buyable(self):
        # 未入场（buyable=False）⇒ 无仓可出，不得产 exit 事件
        from app.market_cn.auto.core.present.contract import DayInput, Progress
        st = _break.BreakStrategy()
        prev = Progress(stage="exec", date="2026-01-05", payload={
            "entry_date": "2026-01-05", "entry_price": 10.98, "buyable": False})
        inp = DayInput("600000", _bar("2026-01-06", 10.9, 11.0, 10.8, 10.9), None)
        assert st._exit_event(self._state(), inp, prev) == []


# ================================================================
# 3. g56：GAP_LIM 收敛零漂移（红 = 旧口径的行为必须原样保留）
# ================================================================

class TestG56ZeroDrift:
    def test_main_board_boundary(self):
        st = _g56.G56Strategy() if hasattr(_g56, "G56Strategy") else None
        assert st is not None
        row = {"code": "600000"}   # main
        assert get_board_type("600000") == "main"
        # 旧口径: gap < 0.098 可买 ⇒ 9.7% 可买 / 9.8% 拒（严格）
        assert st.entry_decision(row, snap={"open": 109.7, "previousClose": 100.0}).buyable is True
        red = st.entry_decision(row, snap={"open": 109.8, "previousClose": 100.0})
        assert red.buyable is False and "涨停幅度" in red.reason

    def test_gem_star_boundary(self):
        st = _g56.G56Strategy()
        row = {"code": "300001"}   # gem_star
        assert get_board_type("300001") == "gem_star"
        assert st.entry_decision(row, snap={"open": 119.7, "previousClose": 100.0}).buyable is True
        assert st.entry_decision(row, snap={"open": 119.8, "previousClose": 100.0}).buyable is False


# ================================================================
# 4. dragon：exec 旗标 + entry_decision（原来 open>0 即买 = 红）
# ================================================================

class TestDragon:
    def test_entry_decision_red_green(self):
        st = _dragon.DragonCallbackStrategy() if hasattr(_dragon, "DragonCallbackStrategy") else None
        assert st is not None
        row = {"code": "600000"}
        red = st.entry_decision(row, snap={"open": 109.8, "previousClose": 100.0})
        assert red.buyable is False and "涨停" in red.reason
        green = st.entry_decision(row, snap={"open": 112.0, "previousClose": 100.0})
        assert green.buyable is True   # 高开不拦（2026-09-07 起无 gap 范围过滤），只拦涨停


# ================================================================
# 5. knife：_gates 单点守卫（fold 与 scan_signals 共用）
# ================================================================

class TestKnife:
    def test_gates_rejects_limit_seal(self):
        st = _knife.KnifeCatchStrategy() if hasattr(_knife, "KnifeCatchStrategy") else None
        assert st is not None
        p = st.params()
        snap = {"time": "2026-01-05 14:56:00", "last": 109.8, "high": 109.8,
                "low": 100.0, "previousClose": 100.0}
        assert st._gates(p, None, snap, [snap], 0.0, code="600000", board="main") is None
