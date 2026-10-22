# -*- coding: utf-8 -*-
"""守卫：tencent 科创板 volume 单位 + fund_flow_local 差分闸门（2026-10-23 事故防回退）。

事故链条（详见 `.workbuddy/memory/2026-10-23.md`）:
  1. tencent 对科创板 parts[6] 返回"股"、对其余板块返回"手"，而历史代码
     `tencent.py` 无条件 `vol * 100` ⇒ 科创板被二次放大 100 倍；
  2. coordinator「主池 + 重试池」逐拍换源 ⇒ 累计量在真实值与 ×100 之间跳变；
  3. `fund_flow_local._derive` 只累加正增量、回落(dvol<0)静默丢弃
     ⇒ 62 次跳变累加、0 次抵消 ⇒ 688787 主力净额 **1349 亿**。

本文件全部离线（不联网、不连库），常量取自 2026-10-22 实测值：
  688787 sh  parts[6]=8521555(股) parts[37]=101351(万元) last=117.50
  600519 sh  parts[6]=42491(手)   parts[37]=557622(万元) last=1318.49
"""
from datetime import datetime

import pytest

from app.data_sources.provider.tencent import _volume_to_shares
from app.market_cn.fund_flow_local import _derive


# ── 1. tencent 单位自适应判据 ────────────────────────────────────────────

def test_star_market_volume_is_not_amplified() -> None:
    """科创板 parts[6] 已是"股"，不得再 ×100（事故根因）。

    日线基准 8,521,555 股；修复前返回 852,155,500（×100）。
    """
    assert _volume_to_shares(8521555.0, 101351.0, 117.50) == 8521555.0


def test_main_board_volume_is_converted_from_hands() -> None:
    """主板 parts[6] 是"手"，必须 ×100 转"股"（日线基准 4,249,133）。"""
    assert _volume_to_shares(42491.0, 557622.0, 1318.49) == 4249100.0


def test_unknown_unit_returns_none_never_guesses() -> None:
    """★ 单位判不出来时必须返回 None，绝不退回 `vol * 100` 猜测值。

    用户裁定（2026-10-23）：provider 层"允许值是空的，但不允许值是错误的"。
    历史代码的 `vol * 100` 兜底正是错误值的来源——判不出来时它按"手"处理，
    对科创板就变成 100 倍的错值，且下游无从分辨"没数据"与"数据错了"。
    """
    assert _volume_to_shares(8521555.0, 0.0, 117.50) is None      # 缺成交额
    assert _volume_to_shares(8521555.0, 101351.0, 0.0) is None    # 零价
    assert _volume_to_shares(0.0, 101351.0, 117.50) is None       # 零量


def test_unit_verdict_is_not_hardcoded_by_board() -> None:
    """判据必须由成交额反推，不能按板块硬编码。

    同一份"手"口径数据换个代码前缀也应判为手；反之科创板若哪天改成返回"手"，
    成交额判据会跟着变（ratio≈100）⇒ 自动适配，无需改代码。
    """
    # 主板量（手）配上"股"的成交额比例 ⇒ 判为股，不因前缀而变
    assert _volume_to_shares(5000.0, 58.5, 117.0) == 5000.0
    # 科创板量（股）配上"手"的成交额比例 ⇒ 判为手
    assert _volume_to_shares(42491.0, 557622.0, 1318.49) == 4249100.0


# ── 2. fund_flow_local 差分闸门 ──────────────────────────────────────────

def _t(hh: int, mm: int) -> datetime:
    return datetime(2026, 10, 22, hh, mm)


def test_derive_drops_jump_and_negative_ticks() -> None:
    """跳变拍与累计量倒退拍必须被丢弃并计数，不得静默累加。

    取事故当日 688787 的真实形态：09:25 竞价 131,970 → 09:30:45 跳到
    52,512,700（单位跳变）→ 09:31:45 回落 685,069（倒退）→ 09:32:45 正常。
    """
    rows = [
        (_t(9, 25), 115.00, 131970.0),
        (_t(9, 30), 112.61, 52512700.0),   # 跳变：dvol ≈ 前值 397 倍
        (_t(9, 31), 113.00, 685069.0),     # 倒退：dvol < 0
        (_t(9, 32), 118.46, 985643.0),     # 正常
    ]
    res = _derive("688787", rows)
    assert res["dropped"] == {"negative": 1, "jump": 1}
    # 只有倒退→正常那一段能出点（9:32 相对 9:31 的倒退基准）
    assert res["points"] == 1


def test_derive_does_not_kill_normal_opening_surge() -> None:
    """开盘瞬时真实增长不得被跳变闸门误杀（实测最大约 4x，阈值 20x）。

    688787 09:25 → 09:30 真实形态：13.2 万 → 52.5 万（约 4x）。
    """
    rows = [
        (_t(9, 25), 115.00, 131970.0),
        (_t(9, 30), 112.61, 525127.0),     # 真实增长 ≈ 3x，不是跳变
        (_t(9, 31), 113.00, 685069.0),
        (_t(9, 32), 118.46, 985643.0),
    ]
    res = _derive("688787", rows)
    assert res["dropped"] == {"negative": 0, "jump": 0}
    assert res["points"] == 3


def test_derive_jump_no_longer_inflates_total() -> None:
    """★ 回归核心：跳变拍不得进入净额累加（旧算法在此产出天文数字）。

    构造含 1 次 ×100 跳变的序列，净额量级必须与"不含跳变"的干净序列同量级，
    而不是被放大两个数量级。
    """
    clean = [
        (_t(9, 30), 100.0, 100000.0),
        (_t(9, 31), 101.0, 200000.0),
        (_t(9, 32), 102.0, 300000.0),
    ]
    dirty = [
        (_t(9, 30), 100.0, 100000.0),
        (_t(9, 31), 101.0, 30000000.0),   # ×100 跳变（脏拍）
        (_t(9, 32), 102.0, 300000.0),     # 回落
    ]
    clean_total = _derive("X", clean)["total_main_net"]
    dirty_res = _derive("X", dirty)
    dirty_total = dirty_res["total_main_net"]

    assert dirty_res["dropped"]["jump"] == 1
    assert dirty_res["dropped"]["negative"] == 1
    # 脏序列净额不得比干净序列大一个数量级以上
    assert abs(dirty_total) <= abs(clean_total) * 10


@pytest.mark.parametrize("vol_seq", [
    [100000.0, 200000.0, 300000.0],
    [1000.0, 10000.0, 100000.0],   # 开盘 10x 瞬时增长，仍属正常
])
def test_derive_monotonic_series_all_kept(vol_seq) -> None:
    """单调递增的正常序列不应丢弃任何一拍。"""
    rows = [(_t(9, 30 + i), 100.0 + i, v) for i, v in enumerate(vol_seq)]
    res = _derive("X", rows)
    assert res["dropped"] == {"negative": 0, "jump": 0}
    assert res["points"] == len(vol_seq) - 1
