"""test_fold.py — 内核折叠等价：逐日 step 推进 == 全量 init_state 重算（切片无损）。

这是"预处理与回测是同一套系统和数据流"的存在理由：切片推进与全量重算
必须逐位一致，否则增量预处理和回测会分叉。
"""

from app.market_cn.auto.strategies.knife_catch import KnifeCatchStrategy
from app.market_cn.auto.strategies.tail_oversold import TailOversoldStrategy
from tests.present.common import KNIFE_HIST_CLOSES, TAIL_HIST_CLOSES, gen_hist_bars


def _fold(s, code, bars, k):
    st = s.init_state(code, bars[:8] if isinstance(s, KnifeCatchStrategy) else bars[:6])
    for b in (bars[6:] if isinstance(s, TailOversoldStrategy) else bars[8:]):
        st = s.step(st, b)
    return st


def test_knife_fold_equals_full_recompute():
    s = KnifeCatchStrategy()
    code = "600001"
    bars = gen_hist_bars(code, KNIFE_HIST_CLOSES)
    for k in range(9, len(bars) + 1):
        full = s.init_state(code, bars[:k])
        st = s.init_state(code, bars[:8])
        for b in bars[8:k]:
            st = s.step(st, b)
        assert st == full, f"k={k}"


def test_tail_fold_equals_full_recompute():
    s = TailOversoldStrategy()
    code = "600001"
    bars = gen_hist_bars(code, TAIL_HIST_CLOSES)
    for k in range(7, len(bars) + 1):
        full = s.init_state(code, bars[:k])
        st = s.init_state(code, bars[:6])
        for b in bars[6:k]:
            st = s.step(st, b)
        assert st == full, f"k={k}"


def test_step_is_pure():
    for s, closes in ((KnifeCatchStrategy(), KNIFE_HIST_CLOSES),
                      (TailOversoldStrategy(), TAIL_HIST_CLOSES)):
        bars = gen_hist_bars("600001", closes)
        st = s.init_state("600001", bars[:-1])
        import copy
        st_before = copy.deepcopy(st)
        s.step(st, bars[-1])
        assert st == st_before, "step 不得修改入参 state"


def test_probe_anchors_exist_in_bars():
    for s, closes in ((KnifeCatchStrategy(), KNIFE_HIST_CLOSES),
                      (TailOversoldStrategy(), TAIL_HIST_CLOSES)):
        bars = gen_hist_bars("600001", closes)
        st = s.init_state("600001", bars)
        by_date = {b["time"]: b["close"] for b in bars}
        anchors = s.probe(st)
        assert anchors, "probe 不能为空"
        for d, c in anchors:
            assert by_date[d] == c
        # 锚窗口两端（防历史整体改写 + 防同日修正）
        assert anchors[0][0] == st["win"][0]["d"]
        assert anchors[-1][0] == st["win"][-1]["d"]
