"""tests/common.py — 合成数据生成（日线历史 + 盘中 60s 快照序列）。"""

from __future__ import annotations

from datetime import date as _date, timedelta


def _d(start: str, i: int) -> str:
    y, m, d = map(int, start.split("-"))
    return str(_date(y, m, d) + timedelta(days=i))


def gen_hist_bars(code: str, closes: list[float], start: str = "2026-08-01",
                  vol: float = 1000.0) -> list[dict]:
    """由收盘价序列构造日线 bars（open/high/low 围绕 close）。"""
    bars = []
    for i, c in enumerate(closes):
        c = float(c)
        bars.append({"time": _d(start, i), "open": round(c * 1.01, 4),
                     "high": round(c * 1.02, 4), "low": round(c * 0.98, 4),
                     "close": c, "volume": vol})
    return bars


# knife 历史：连跌 + pre5<=-15 + 无涨停（旧门全过）
# idx13=111 对 idx12=102 涨 8.8%（< 9.604% 判别阈值，非涨停）；pre5 = 93/111-1 = -16.2% ✓
KNIFE_HIST_CLOSES = [126, 124, 122, 120, 118, 116, 114, 112,
                     110, 108, 106, 104, 102, 111, 108, 104, 100, 93]

TAIL_HIST_CLOSES = [130, 128, 126, 124, 122, 120, 118, 116,
                    114, 112, 110, 108, 105, 102, 100, 98]


def make_snapshot_rows(day: str, pc: float, prices: dict[str, float],
                       day_high: float, day_low: float,
                       total_vol: float = 1200.0,
                       vol_frac: dict[str, float] | None = None) -> list[dict]:
    """按 {HH:MM: last} 生成当日累计口径快照行（volume 累计、high/low 累计极值）。

    vol_frac: {HH:MM: 该时刻累计量占比}（缺省按分钟线性）。
    """
    rows = []
    mins = sorted(prices)
    n = len(mins)
    for i, hhmm in enumerate(mins):
        frac = (vol_frac or {}).get(hhmm, (i + 1) / n)
        rows.append({"time": f"{day} {hhmm}:00",
                     "last": float(prices[hhmm]),
                     "open": float(next(iter(prices.values()))),
                     "high": day_high, "low": day_low,
                     "previousClose": float(pc),
                     "volume": round(total_vol * frac, 4)})
    return rows


def knife_day_rows(day: str, pc: float = 100.0) -> list[dict]:
    """knife 触发日：高位回落→尾盘 20 分钟回升，14:56 起满足全部门。"""
    prices = {}
    # 14:00~14:35 线性 96.4 → 84.1
    t0, t1 = 14 * 60, 14 * 60 + 35
    for m in range(t0, t1 + 1):
        f = (m - t0) / (t1 - t0)
        prices[f"{m // 60:02d}:{m % 60:02d}"] = round(96.4 + (84.1 - 96.4) * f, 4)
    # 14:36~14:56 缓慢回升 84.1 → 85.1（tail_ret = 85.1/84.1-1 ≈ +1.19%）
    t2 = 14 * 60 + 56
    for m in range(t1 + 1, t2 + 1):
        f = (m - (t1 + 1)) / (t2 - (t1 + 1))
        prices[f"{m // 60:02d}:{m % 60:02d}"] = round(84.1 + 1.0 * f, 4)
    # 14:57~15:00 维持
    for m in range(t2 + 1, 15 * 60 + 1):
        prices[f"{m // 60:02d}:{m % 60:02d}"] = 85.2
    return make_snapshot_rows(day, pc, prices, day_high=96.5, day_low=84.0,
                              total_vol=1200.0)


def tail_day_rows(day: str, pc: float = 100.0) -> list[dict]:
    """tail 触发日：全天阴跌，14:20~14:40 均价 ~90.5，14:50 后现价 ~88.6。"""
    prices = {}
    t0, t1 = 14 * 60, 14 * 60 + 20          # 14:00~14:20: 97.4 → 91.5
    for m in range(t0, t1 + 1):
        f = (m - t0) / (t1 - t0)
        prices[f"{m // 60:02d}:{m % 60:02d}"] = round(97.4 + (91.5 - 97.4) * f, 4)
    t2 = 14 * 60 + 40                        # 14:21~14:40: 91.5 → 89.5 (均值≈90.5)
    for m in range(t1 + 1, t2 + 1):
        f = (m - (t1 + 1)) / (t2 - (t1 + 1))
        prices[f"{m // 60:02d}:{m % 60:02d}"] = round(91.5 + (89.5 - 91.5) * f, 4)
    t3 = 14 * 60 + 50                        # 14:41~14:50: 89.5 → 88.6
    for m in range(t2 + 1, t3 + 1):
        f = (m - (t2 + 1)) / (t3 - (t2 + 1))
        prices[f"{m // 60:02d}:{m % 60:02d}"] = round(89.5 + (88.6 - 89.5) * f, 4)
    for m in range(t3 + 1, 15 * 60 + 1):     # 14:51~15:00: 横盘
        prices[f"{m // 60:02d}:{m % 60:02d}"] = round(88.6 + (m - t3) * 0.01, 4)
    return make_snapshot_rows(day, pc, prices, day_high=97.5, day_low=87.5,
                              total_vol=1500.0)
