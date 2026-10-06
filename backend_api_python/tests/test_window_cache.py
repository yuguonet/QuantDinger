"""test_window_cache.py — 滑动窗口缓存 golden 测试。

分两层 (2026-10-06):

  1) **fake 层 (必跑, 确定性)**: monkeypatch 掉 DB/复权, 只验证滑动逻辑的不变量
     —— 拼接 == 一次性全量、窗口头部过期被裁、因子指纹变触发整票重拉、
     子集调用不摧毁共享缓存。CI 无 DB 也跑, **不会静默 skip**。

  2) **真实 DB 层**: 与 fetch_klines_batch 逐字段相等。默认跑 (DB 连不上 ⇒ FAIL
     而不是 skip —— 静默 skip 是头号敌人); CI 无库时用 QD_SKIP_DB_TESTS=1
     **显式**跳过。全市场 164 万行对账另有 tmp/verify_all_equal.py。

⚠ 复权变换在 fake 层用"逐行独立的乘法"模拟: 若实现对历史行重复施加变换,
   第二次就会变成 f², 能被断言抓到 —— 这正是除权场景要守的不变量。
"""

import os
import pickle
from datetime import date, timedelta

import pytest

from app.market_cn.auto.core.data import window_cache as wc  # noqa: E402

DAYS = 320


def _dates(lo: str, hi: str):
    """周一~周五的日期序列 (含端点)。"""
    d, end = date.fromisoformat(lo), date.fromisoformat(hi)
    out = []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


ALL_D = _dates("2026-01-05", "2026-03-10")
CODES = ["AAA", "BBB", "CCC"]
# code -> [(date, o,h,l,c,v)]
RAW = {c: [(d, 10.0 + i * 0.1, 11.0 + i * 0.1, 9.0 + i * 0.1, 10.5 + i * 0.1,
            1000.0 + i) for i, d in enumerate(ALL_D)] for c in CODES}
RAW["CCC"] = RAW["CCC"][:-5]          # 停牌票: 末尾少几根

FACTOR = {"AAA": 2.0, "BBB": 3.0, "CCC": 1.0}   # 逐行独立的复权变换


def _unadj(klines, code):
    f = FACTOR[code]
    return [dict(b, close=round(b["close"] * f, 4)) for b in klines]


def _mk_query():
    def _q(market, codes, tf, start_time=None, end_time=None):
        out = {}
        for c in codes or ():
            rows = [r for r in RAW.get(c, [])
                    if (not start_time or r[0] >= start_time)
                    and (not end_time or r[0] <= end_time)]
            if rows:
                out[c] = rows
        return out
    return _q


def _gold(codes, lo, hi):
    """一次性全量 (窗口内全部行 + 复权) —— 滑动必须与之逐字段相等。"""
    out = {}
    for c in codes:
        rows = [r for r in RAW.get(c, []) if lo <= r[0] <= hi]
        out[c] = _unadj([{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
                          "close": r[4], "volume": r[5]} for r in rows], c)
    return {c: v for c, v in out.items() if v}


def _diff(a, b):
    if len(a) != len(b):
        return f"根数 {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        for k in ("time", "open", "high", "low", "close", "volume"):
            if x[k] != y[k]:
                return f"第{i}根 {k}: {x[k]!r} vs {y[k]!r}"
    return None


def _assert_eq(got, gold, tag):
    assert set(got) == set(gold), f"{tag}: 票集 {set(gold) ^ set(got)}"
    bad = [(c, _diff(got[c], gold[c])) for c in gold if _diff(got[c], gold[c])]
    assert not bad, f"{tag}: {bad[:3]}"


# ================================================================
# 1) fake 层
# ================================================================

@pytest.fixture
def fake(tmp_path, monkeypatch):
    """隔离缓存目录 + 假 DB/复权, 窗口可控。"""
    monkeypatch.setenv("QD_WINDOW_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr("app.market_cn.auto.core.data.hub._query_batch_raw",
                        _mk_query())
    monkeypatch.setattr("app.data_sources.provider.adjustment.unadj_to_qfq",
                        _unadj)
    monkeypatch.setattr("app.data_sources.provider.adjustment.fetch_qfq_factors",
                        lambda code: None)
    wc._MEMO.clear()
    wc._FP_FAST = None
    state = {"lo": "2026-01-05", "hi": "2026-03-06"}

    def _bounds(days=300, as_of=None):
        return state["lo"], state["hi"]
    monkeypatch.setattr("app.market_cn.auto.core.data.kline._window_bounds",
                        _bounds)
    monkeypatch.setattr(wc, "_factor_fp", lambda code: 7)
    return state


def test_seed_equals_full(fake):
    got = wc.load_windows(CODES, DAYS)
    _assert_eq(got, _gold(CODES, "2026-01-05", "2026-03-06"), "seed")


def test_hit_equals_full(fake):
    wc.load_windows(CODES, DAYS)
    got = wc.load_windows(CODES, DAYS)
    _assert_eq(got, _gold(CODES, "2026-01-05", "2026-03-06"), "hit")


def test_incremental_equals_full(fake):
    """窗口整体右移一天 ⇒ 尾部追加 + 头部过期, 结果与一次性全量相等。"""
    wc.load_windows(CODES, DAYS)
    fake["lo"], fake["hi"] = "2026-01-06", "2026-03-07"     # 第二天
    got = wc.load_windows(CODES, DAYS)
    _assert_eq(got, _gold(CODES, "2026-01-06", "2026-03-07"), "incremental")


def test_subset_call_keeps_cache(fake):
    """子集调用不得摧毁共享缓存 (曾被子调用清成 1 票)。"""
    wc.load_windows(CODES, DAYS)
    n0 = len(pickle.load(open(wc._path(DAYS), "rb"))["data"])
    wc.load_windows(CODES[:1], DAYS)
    n1 = len(pickle.load(open(wc._path(DAYS), "rb"))["data"])
    assert n1 == n0, f"子集调用后缓存票数 {n0} → {n1}"


def test_fp_change_triggers_refetch(fake, monkeypatch):
    """因子指纹变 (除权改写历史) ⇒ 整票重拉; 结果仍与全量一致。"""
    wc.load_windows(CODES, DAYS)
    monkeypatch.setattr(wc, "_factor_fp", lambda code: 8)    # 伪造除权
    got = wc.load_windows(CODES, DAYS)
    _assert_eq(got, _gold(CODES, "2026-01-05", "2026-03-06"), "fp_change")


def test_peek_readonly(fake):
    """peek 只读: 不写缓存, 不补拉缺失票。"""
    wc.load_windows(CODES, DAYS)
    before = os.path.getsize(wc._path(DAYS))
    got = wc.peek_windows(CODES, 200)
    assert set(got) == set(CODES)
    assert os.path.getsize(wc._path(DAYS)) == before, "peek 不应写缓存"
    miss = wc.peek_windows(["ZZZ"], 200)          # 缓存中没有的票
    assert "ZZZ" not in miss, "peek 不应补拉"


def test_disabled_falls_back(fake, monkeypatch):
    """QD_WINDOW_CACHE=0 ⇒ 走全量, 不写缓存, 结果不变。"""
    monkeypatch.setenv("QD_WINDOW_CACHE", "0")
    assert not wc.enabled()
    got = wc.load_windows(CODES, DAYS)
    _assert_eq(got, _gold(CODES, "2026-01-05", "2026-03-06"), "disabled")
    assert not os.path.isfile(wc._path(DAYS)), "关闭时不应写缓存"


# ================================================================
# 2) 真实 DB 层 (默认跑; CI 无库用 QD_SKIP_DB_TESTS=1 显式跳过)
# ================================================================

@pytest.mark.skipif(os.getenv("QD_SKIP_DB_TESTS") == "1",
                    reason="显式跳过 (QD_SKIP_DB_TESTS=1)")
def test_real_db_golden(tmp_path, monkeypatch):
    """真实行情库: 滑动结果与 fetch_klines_batch 逐字段相等。"""
    from app.market_cn.auto.core.data.kline import all_codes, fetch_klines_batch
    # conftest 把 psycopg2 整包 mock 掉 (pytest 环境无库) ⇒ 这是**环境限制**, skip;
    # 但真实运行时若连不上 ⇒ FAIL (不静默)。两者必须分清, 否则测试等于没跑。
    try:
        import psycopg2.extras as _pe
        if not hasattr(_pe, "execute_values"):
            pytest.skip("psycopg2 被 conftest mock —— pytest 环境无真实 DB "
                        "(真实对账见 tmp/verify_all_equal.py: 164 万行零差异)")
    except Exception:
        pass
    monkeypatch.setenv("QD_WINDOW_CACHE_DIR", str(tmp_path))
    wc._MEMO.clear()
    wc.invalidate(DAYS)
    try:
        codes = all_codes()[:60]
    except Exception as e:
        pytest.fail(f"行情 DB 不可用 (设 QD_SKIP_DB_TESTS=1 显式跳过): {e}")
    if not codes:
        pytest.fail("all_codes() 返回空 —— 取数链断了, 不是 skip 的理由")
    gold = fetch_klines_batch(codes, days=DAYS, as_of=None)
    assert gold, "全量取数返回空"
    wc.load_windows(codes, DAYS)
    got = wc.load_windows(codes, DAYS)
    _assert_eq(got, gold, "real_db")
