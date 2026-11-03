# -*- coding: utf-8 -*-
"""选人层回测：逐日 n / 胜率 / 触板率 / 深亏 / T+1 / T+3 均值（可指定日期区间）。

【定位】真实行情回测（依赖 CNStock_db 历史日线，结果随窗口变化）——按项目约定，
真实行情回测属探针、不进常规单测（见 tests/test_market_screener_backtest.py 模块 docstring）。
本文件**双模式**：
· `pytest` 收集时：只 skip 需要真实数据库的集成回测；指标和排序探针单测仍可运行；
· 直接运行：`python tests/test_market_screener_selection_bt.py` ⇒ 连真实 DB，
  重建**当前选人层**并比较固定保底名额下的排序探针，输出逐日 + 汇总 JSON。

【口径】完全复用 `tests/test_market_screener_backtest.py` 的 `simulate()` / `aggregate()`（唯一真源）：
  D+1 开盘买 / T+1 锁仓 / 3 个卖出日 / 触涨停按入场价 +10% 止盈 / 开盘一字涨停买不进不计样本 /
  往返费用 0.3% / 深亏 = 持有期最低价 ≤ −15%。

【无前视】四处均按决策日 D 截断，并内置断言守护：
  1. 价格用 D 日收盘（`candidate_pool(fill_quote=False)`，绝不用实时价回填）；
  2. 画像/p_up 的日线由 patch 过的 `fetch_kline` 返回（只到 D，函数内 assert）；
  3. p_up 的大盘基准 K 线由 patch 过的 `_market_klines()` 返回（只到 D）；
  4. 龙回头源 `fetch_recent_zt_pools(8)` 的「近 8 交易日」锚到 D —— 原实现经
     `_recent_trading_days` 锚「今天」，会把 D 之后的涨停池并进龙回头 universe
     （membership 前视），已 patch `_recent_trading_days` 锚到 D。

【T+1】复用引擎 `simulate()`：入场 = 决策日后第一个交易日、卖出严格晚于入场（当日不可卖）；
  `run_backtest` 对每笔非 buyable 样本硬断言 `entry_day>D ∧ exit_day>entry_day`，违反即抛错。

【可回溯源 / 池模式】`zt`（连板）+ `dragon`（龙回头）是唯一可回溯源；但**本地库涨停池仅
  2026-10-12 起、远端对更早日期返空**（实测 09-15/10-08/10-09 均「源正常返回空」）。
  ⇒ `BT_POOL_MODE=recon` 用日线重建涨停池（口径同 tech_summary，close/prev−1≥limit−0.8），
  覆盖全历史；对 DB 已覆盖窗口对拍 10-20 完全一致(14/14)、10-14(13/15)、10-23(12/13)，~90%+。
  `hot/search/recent_zt/eod_start` 仍是实时/重扫描源，未含（已知缺口，与 Q-4/Q-8 一致）。

【已知限制】
  · 池非全量：hot/search 类源缺失；recon 无 ST/turnover/名称 ⇒ 少量成员差异；
  · 价用库内不复权原始价（`kline_1D_2026`），窗口内除权会引入偏差（短期罕见）；
  · p_up 模型为当前生产模型，回溯算历史分（非当时训练版本）；
  · `vr_rel`（`market_calib_stats`）与 `rs_d0/rs_5d`（`_bench_index` 走实时）在沙箱置空
    ⇒ `rule.pass` 的 vr_rel≥2 闸与 regime「跳水回踩」子类退化。**只影响排序**（总名单 >45 时
    决定谁被截掉），**不影响成员**（成员由 hard + p_up 硬闸 + 排名截断决定）。

用法：
  DATABASE_URL=postgresql://postgres:yuguo@localhost:5432/postgres QD_MARKET_DBS=CNStock \
    python tests/test_market_screener_selection_bt.py
  可选环境变量：BT_DAYS=10（最近决策日数）、BT_START=2026-09-01 / BT_END=2026-10-26
  （给定则按自然日区间取决策日，优先级高于 BT_DAYS）、BT_STRATEGY=post_market、
  BT_SOURCES=zt,dragon、BT_POOL_MODE=db|recon（recon=日线重建涨停池，覆盖历史缺失时段）
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)          # .../backend_api_python
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:yuguo@localhost:5432/postgres")
os.environ.setdefault("QD_MARKET_DBS", "CNStock")

import pytest  # noqa: E402


def _data_mocked() -> bool:
    """pytest 环境被 conftest mock 掉真实数据层 ⇒ 本回测无法跑，应 skip。"""
    try:
        import psycopg2
        if str(getattr(psycopg2, "__file__", "") or "").startswith("/mock/"):
            return True
    except Exception:
        return True
    return False


_MOCKED = _data_mocked()
_REAL_DATA_SKIP_REASON = (
    "真实行情回测需连 CNStock_db；请直接运行本文件连接真实数据源"
)
_REPORT_PATH = os.path.join(
    os.path.dirname(_BACKEND), "tmp", "out_selection_backtest_daily_probe_20261102.json"
)


# ── 引擎与业务模块（延迟加载，避免 pytest 收集期副作用）──────────────────────
def _load():
    spec = importlib.util.spec_from_file_location(
        "bt_engine", os.path.join(_HERE, "test_market_screener_backtest.py"))
    BT = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(BT)
    from app.agent.modpath import import_module
    F = import_module("app.agent.skills.market-screener._facts")
    H = import_module("app.agent.skills.market-screener._helpers")
    R = import_module("app.agent.skills.market-screener.run")
    C = import_module("app.agent.skills.market-screener.common")
    return BT, F, H, R, C


def _load_db():
    from app.utils.db_market import get_market_db_manager
    pool = get_market_db_manager()._get_pool("CNStock")

    def q(sql, args=None):
        with pool.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    return q


def _load_bars(q):
    """一次性拉全市场日线（2026 全年），建 date/time 双键 bar（兼容 tech/_norm_bars/BT.simulate）。"""
    rows = q("""SELECT symbol, "time"::date, open, high, low, close, volume
                  FROM "kline_1D_2026" WHERE "time"::date >= '2026-01-01'
                  ORDER BY symbol, "time" """)
    bars = defaultdict(list)
    for s, d, o, h, l, c, v in rows:
        ds = str(d)
        bars[str(s)].append({
            "date": ds, "time": ds, "open": float(o), "high": float(h),
            "low": float(l), "close": float(c), "volume": float(v or 0),
        })
    idx = {s: {b["date"]: k for k, b in enumerate(seq)} for s, seq in bars.items()}
    return bars, idx


def _load_calendar(q):
    return [str(r[0]) for r in q(
        'SELECT DISTINCT "time"::date FROM "kline_1D_2026" '
        'WHERE "time"::date >= \'2026-01-01\' ORDER BY 1')]


def _load_market_klines(H):
    """大盘基准 K 线（p_up 用），按日期串索引；原样保留 time 为 int 时间戳。"""
    _bj = ZoneInfo("Asia/Shanghai")
    mk = {}
    for b in (H._market_klines() or []):
        if b.get("time") and b.get("close"):
            mk[datetime.fromtimestamp(int(b["time"]), tz=_bj).strftime("%Y-%m-%d")] = b
    return mk, sorted(mk)


def _build_zt_by_day(bars, limit_pct_fn):
    """从日线重建「每日收盘涨停」表：date -> {code: 连续涨停数}。

    口径 = `tech_summary` 的涨停判定（close/prev − 1 ≥ limit_pct − 0.8）。
    用于历史涨停池缺失时段（本地库仅 2026-10-12 起、远端对更早日期返空）的近似重建。
    局限：无名称 ⇒ ST（实际 ±5%）因不足 10% 阈值天然落选（符合「排除 ST」预期）；
    首日无涨跌幅新股可能被误判涨停（罕见）；无 turnover / zt_time / seal_amount。
    """
    zt = defaultdict(dict)
    for code, seq in bars.items():
        lp = limit_pct_fn(code) * 100.0
        streak = 0
        for i in range(1, len(seq)):
            pc = seq[i - 1]["close"]
            if pc and (seq[i]["close"] / pc - 1) * 100 >= lp - 0.8:
                streak += 1
                zt[seq[i]["date"]][str(code)] = streak
            else:
                streak = 0
    return zt


def _make_recon_fetch_zt(zt_by_day):
    """构造 fetch_zt_pool 的替代实现（返回重建的涨停池行，字段对齐 candidate_pool 消费）。"""
    def _fzt(date):
        rows = zt_by_day.get(str(date)) or {}
        return [{"stock_code": c, "stock_name": "", "reason": "",
                 "continuous_zt_days": s, "zt_time": "", "seal_amount": 0,
                 "turnover_rate": 0} for c, s in rows.items()]
    return _fzt


def _pick_decision_days(cal, n=None, start=None, end=None):
    """决策日集合：给定 start/end 取该区间；否则取最近 n 个。

    只保留「有 ≥4 个后续交易日」的日期（入场日 + 3 个卖出日都可得，否则 simulate 返回 None）。
    """
    pos = {d: i for i, d in enumerate(cal)}
    ok = [d for d in cal if pos[d] + 4 < len(cal)]
    if start or end:
        lo = start or ok[0]
        hi = end or ok[-1]
        return [d for d in ok if lo <= d <= hi]
    return ok[-int(n or 10):]


def _select_one_day(R, F, H, C, bars, idx, mk, mk_days, cal, day, strategy, sources):
    """重建当前选人层在决策日 day 的名单（保底轨 + 加法层，无前视）。

    返回 (selected_all: list[dict], debug: dict)。
    """
    # 1) 无前视 patch：个股日线只到 D；大盘基准只到 D；封板结构不查实时池
    def _fk(code, days=120, **kw):
        seq = bars.get(str(code)) or []
        cut = [b for b in seq if b["date"] <= day]
        assert all(b["date"] <= day for b in cut), ("fetch_kline 前视", day, code)
        return cut[-int(days):] if days else cut

    C.fetch_kline = _fk
    H._market_klines = lambda: [mk[x] for x in mk_days if x <= day]
    H.zt_index = lambda date="": {}
    # 市场校准/基准指数历史逐日重建成本高且 _bench_index 走实时（前视）⇒ 置空：
    # 二者只喂 rule.pass（排序键）与 rs_d0（regime 子类），不影响成员（hard+截断）。
    H.market_calib_stats = lambda *a, **k: {}
    H._bench_index = lambda: {}
    # 龙回头源 fetch_recent_zt_pools(8) → _recent_trading_days(8) 锚定「今天」
    # ⇒ 会把 D 之后的涨停池并进龙回头 universe（membership 前视）。锚到决策日 D。
    _prior = [x for x in cal if x <= day]
    C._recent_trading_days = lambda n, _p=_prior: list(reversed(_p[-int(n):]))

    # 2) 候选池（可回溯源；fill_quote=False 避免实时价前视）
    pool = F.candidate_pool(strategy=strategy, date=day, sources=sources, fill_quote=False)
    cands = list(pool.get("candidates") or [])

    # 3) 用 D 日收盘补价（等价线上 fill_quote 的「事实补全」，但不是实时价），丢弃 D 日无 bar 的票
    alive = []
    for c in cands:
        code = str(c.get("code") or "")
        i = idx.get(code, {}).get(day)
        if i is None or i < 1:
            continue
        seq = bars[code]
        c["price"] = seq[i]["close"]
        c["change_pct"] = round((seq[i]["close"] / seq[i - 1]["close"] - 1) * 100, 2)
        c["previous_close"] = seq[i - 1]["close"]
        alive.append(c)

    prescreen = {"strategy": strategy, "market": {}, "main_themes": [], "candidates": alive}

    # 4) 保底轨（恒入 deep，≤25）
    captured_profiles = []
    original_profile_loader = R._profile_candidates

    def _capture_profiles(*args, **kwargs):
        bundle = original_profile_loader(*args, **kwargs)
        captured_profiles.extend(bundle.get("profiles") or [])
        return bundle

    R._profile_candidates = _capture_profiles
    try:
        baseline, baseline_hard = R._baseline_selection(alive, prescreen)
    finally:
        R._profile_candidates = original_profile_loader
    baseline_probe_candidates = [
        profile for profile in captured_profiles
        if not R._baseline_blocked(profile.get("hard"), strategy)
    ]

    # 5) 加法层已于 2026-11-03 从生产 run() 清除（objective_preexclude + score_pool
    #    整体移除，无 env 门闸）。回测同型化：selected_all = 仅保底轨，保持与生产一致。
    obj_stats, score_stats = {}, {}
    additive = []

    # 6) 截断到 deep 上限（保底轨 25 < 45，实际不触发，保留作防御）
    selected_all = (baseline + additive)[:R._FUNNEL_DEEP_N]

    debug = {
        "pool": len(cands), "alive": len(alive),
        "baseline": len(baseline), "baseline_hard": baseline_hard,
        "baseline_profile_window": len(captured_profiles),
        "baseline_probe_candidates": len(baseline_probe_candidates),
        "additive": len(additive),
        "obj": obj_stats, "score": score_stats,
        "selected": len(selected_all),
    }
    return selected_all, debug, baseline_probe_candidates


def _load_lhb_day(q, day_str):
    """决策日可得的龙虎榜记录（含盘后公告的当日榜）→ {code: {reasons, net_pos}}。

    reason 是上游解析好的席位画像（「普通席位买入/资金买入/买一主买/主力做T」=游资类，
    「N家机构买入」=机构，「成功率X%」=席位历史胜率）；同票同日多行 = 多路分析混战。
    net_pos = 任一行净买>0（行间金额口径不一，不求和）。
    """
    rows = q("""SELECT stock_code, reason, net_amount
                 FROM cnd_dragon_tiger_list WHERE trade_date = %s""", (str(day_str)[:10],))
    out = {}
    for code, reason, net in rows or []:
        code = str(code or "")
        if len(code) != 6:
            continue
        e = out.setdefault(code, {"reasons": [], "net_pos": False})
        e["reasons"].append(str(reason or ""))
        if float(net or 0) > 0:
            e["net_pos"] = True
    return out


def _probe_selections(candidates, H, quota, lhb=None):
    """Keep the daily quota fixed; vary only ranking on decision-day-known features.

    lhb = {"d1": {code: {reasons, net_pos}}, "d": {...}} 龙虎榜（昨晚榜/今晚榜），
    缺省 None（龙虎榜谓词全 False）。
    """
    pool_order = list(candidates)

    def _tech(item):
        value = item.get("tech")
        return value if isinstance(value, dict) else {}

    def _number(item, key):
        value = _tech(item).get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _ascending(key, transform=lambda value: value):
        def sort_key(item):
            value = _number(item, key)
            return (value is None, transform(value) if value is not None else 0)
        return sort_key

    location_order = {"cool": 0, "normal": 1, "unknown": 2, "hot": 3}

    def _location_key(item):
        location = _tech(item).get("location")
        return (location not in location_order, location_order.get(location, 0))

    def _risk(item):
        return bool(H._has_stretched_streak_risk(item))

    def _zt_cpos_key(item):
        streak = _number(item, "zt_streak") or 0
        cpos = _number(item, "close_pos_in_day")
        return (streak, -(cpos if cpos is not None else 50.0), not _risk(item))

    def _yizi(item):
        return bool(item.get("yizi_hit"))

    def _hot_loc(item):
        return _tech(item).get("location") == "hot"

    # = _helpers._LOC_HOT_RET5 / _LOC_COOL_RET5（location_tier 同源阈值）
    _HOT_RET5, _COOL_RET5 = 7.0, -1.0

    def _deep_cell(item):
        """深亏 44% 格：ret5 高 且 连板≥2（涨得多还连板=位置透支）。"""
        streak = int(_number(item, "zt_streak") or 0)
        ret5 = _number(item, "ret_5d_pct")
        return streak >= 2 and ret5 is not None and ret5 >= _HOT_RET5

    def _hot3(item):
        return _hot_loc(item) and int(_number(item, "zt_streak") or 0) >= 3

    def _deep3(item):
        streak = int(_number(item, "zt_streak") or 0)
        ret5 = _number(item, "ret_5d_pct")
        return streak >= 3 and ret5 is not None and ret5 >= _HOT_RET5

    def _one_board_strong(item):
        """1 板上攻格：zt_streak=1 且 ret5 高（实测触板 50%、深亏低）。"""
        streak = int(_number(item, "zt_streak") or 0)
        ret5 = _number(item, "ret_5d_pct")
        return streak == 1 and ret5 is not None and ret5 >= _HOT_RET5

    def _ma510(threshold):
        """过热 = MA5 高出 MA10 达 threshold%（均线发散度 (ma5/ma10-1)*100）。
        任一均线缺失不判（不剔）。"""
        def _f(item):
            ma5, ma10 = _number(item, "ma5"), _number(item, "ma10")
            if ma5 is None or ma10 is None or ma10 <= 0:
                return False
            return (ma5 / ma10 - 1) * 100 >= threshold
        return _f

    def _below(key, threshold):
        """过冷破位候选谓词：tech[key] ≤ threshold（缺失不判）。"""
        def _f(item):
            value = _number(item, key)
            return value is not None and value <= threshold
        return _f

    def _broken(item):
        """复合破位：价格在 MA20 下方 ≥3% 且趴在 20 日区间底部 20% 以内。"""
        return (_below("dist_ma20_pct", -3.0)(item)
                and _below("pos_in_20d_pct", 20.0)(item))

    def _low_weak(item):
        """日线 regime=低位走弱（run5≤10 且当日跌超 2%，画像自带标签）。"""
        return _tech(item).get("regime") == "低位走弱"

    def _cool_loc(item):
        return _tech(item).get("location") == "cool"

    # ── 龙虎榜（游资行为）谓词：D-1=昨晚榜（昨日买入今明抛假设），D=今晚榜 ──
    _HM_KEYS = ("普通席位买入", "资金买入", "买一主买", "主力做T")

    def _reasons(which, item):
        code = str(item.get("code") or "")
        return (((lhb or {}).get(which, {}) or {}).get(code, {})
                .get("reasons") or [])

    def _hm_buy(reasons):
        return any(any(k in r for k in _HM_KEYS) for r in reasons)

    def _lhb_hm_d1(item):
        """昨日榜有游资类买入（普通席位/地域资金/买一主买/做T）。"""
        return _hm_buy(_reasons("d1", item))

    def _lhb_dot_d1(item):
        """昨日榜有「主力做T」（对倒 = 今天买明天卖的行为画像）。"""
        return any("做T" in r for r in _reasons("d1", item))

    def _lhb_crowd_d1(item):
        """昨日榜同票 ≥2 行且含游资类（多路资金混战 = 拥挤竞争）。"""
        rs = _reasons("d1", item)
        return len(rs) >= 2 and _hm_buy(rs)

    def _lhb_hm_d(item):
        """今晚榜（决策日当日）有游资类买入/做T —— 入场日（D+1）他们最可能出货。"""
        return _hm_buy(_reasons("d", item))

    def _lhb_netbuy_d1(item):
        """昨日榜净买>0（粗口径对照，不分席位类型）。"""
        code = str(item.get("code") or "")
        return bool(((lhb or {}).get("d1", {}) or {}).get(code, {}).get("net_pos"))

    def _lhb_jigou_d1(item):
        """昨日榜机构买入（反向对照：机构买入是否也抛）。"""
        return any("机构买入" in r for r in _reasons("d1", item))

    def _skip_backfill(key, skips):
        """按 key 排序后跳过命中票、后位顺延补位（= 生产 _baseline_selection 的
        一字板剔除机制推广）。"""
        rows = sorted(pool_order, key=key, reverse=True)
        return [r for r in rows if not any(f(r) for f in skips)]

    def _zt_cpos_loc_key(item):
        """现键 + 同连板高度内 hot 位置垫底（只动档内次序，不动连板降序主轴）。"""
        streak = _number(item, "zt_streak") or 0
        cpos = _number(item, "close_pos_in_day")
        return (streak, not _hot_loc(item), -(cpos if cpos is not None else 50.0),
                not _risk(item))

    def _tier_key(item, demote):
        """1 板上攻置顶（实测触板 50%、深亏低）；demote=True 时透支格垫底
        （ret5 高∧连板≥2 实测深亏 44% / hot 位置深亏 35% / ret5 高∧无板净负）。"""
        streak = int(_number(item, "zt_streak") or 0)
        ret5 = _number(item, "ret_5d_pct")
        loc = _tech(item).get("location")
        hot5 = ret5 is not None and ret5 >= _HOT_RET5
        if streak == 1 and hot5:
            tier = 4
        elif loc == "cool" or (streak == 0 and ret5 is not None and ret5 <= _COOL_RET5):
            tier = 3
        elif demote and ((streak >= 2 and hot5) or loc == "hot" or (streak == 0 and hot5)):
            tier = 1
        else:
            tier = 2
        return (tier, not _risk(item), streak)

    def _barbell(head_n, safe_n):
        """杠铃：前 head_n 只按生产键（触板引擎），后 safe_n 只在剩余里按
        低 ret5 + cool 位置补（安全垫），合计 = head_n + safe_n。"""
        strong = _skip_backfill(_zt_cpos_key, [_yizi])[:head_n]
        seen = {id(r) for r in strong}
        rest = [r for r in pool_order if id(r) not in seen]
        safe = sorted(rest, key=lambda i: (
            *_ascending("ret_5d_pct")(i), *_location_key(i)))[:safe_n]
        return strong + safe

    policies = {
        "baseline_pool_order": pool_order,
        "baseline_risk_soft_order": sorted(
            pool_order, key=H._has_stretched_streak_risk
        ),
        "probe_low_streak": sorted(pool_order, key=_ascending("zt_streak")),
        "probe_near_ma5": sorted(
            pool_order, key=_ascending("dist_ma5_pct", abs)
        ),
        "probe_low_ret5": sorted(pool_order, key=_ascending("ret_5d_pct")),
        "probe_cool_location": sorted(pool_order, key=_location_key),
        "probe_combined_risk_location": sorted(
            pool_order,
            key=lambda item: (
                H._has_stretched_streak_risk(item),
                *_location_key(item),
                *_ascending("zt_streak")(item),
                *_ascending("dist_ma5_pct", abs)(item),
            ),
        ),
        "probe_zt_cpos": sorted(pool_order, key=_zt_cpos_key, reverse=True),
        "probe_tier_1ban": sorted(
            pool_order, key=lambda i: _tier_key(i, False), reverse=True),
        "probe_tier_dem_hot": sorted(
            pool_order, key=lambda i: _tier_key(i, True), reverse=True),
        "probe_prod_replica": _skip_backfill(_zt_cpos_key, [_yizi]),
        "probe_prod_skip_hot": _skip_backfill(_zt_cpos_key, [_yizi, _hot_loc]),
        "probe_prod_skip_deep": _skip_backfill(_zt_cpos_key, [_yizi, _deep_cell]),
        "probe_prod_loc_tie": _skip_backfill(_zt_cpos_loc_key, [_yizi]),
        "probe_prod_skip_hot3": _skip_backfill(_zt_cpos_key, [_yizi, _hot3]),
        "probe_prod_skip_deep3": _skip_backfill(_zt_cpos_key, [_yizi, _deep3]),
        "probe_prod_skip_ma510_5": _skip_backfill(_zt_cpos_key, [_yizi, _ma510(5.0)]),
        "probe_prod_skip_ma510_8": _skip_backfill(_zt_cpos_key, [_yizi, _ma510(8.0)]),
        "probe_prod_skip_ma510_10": _skip_backfill(_zt_cpos_key, [_yizi, _ma510(10.0)]),
        "probe_prod_skip_hot_or_ma8": _skip_backfill(
            _zt_cpos_key, [_yizi, lambda i: _hot_loc(i) or _ma510(8.0)(i)]),
        "probe_prod_skip_cool": _skip_backfill(_zt_cpos_key, [_yizi, _cool_loc]),
        "probe_prod_skip_ma20lo": _skip_backfill(
            _zt_cpos_key, [_yizi, _below("dist_ma20_pct", -5.0)]),
        "probe_prod_skip_pos20lo": _skip_backfill(
            _zt_cpos_key, [_yizi, _below("pos_in_20d_pct", 10.0)]),
        "probe_prod_skip_lowret5": _skip_backfill(
            _zt_cpos_key, [_yizi, _below("ret_5d_pct", -5.0)]),
        "probe_prod_skip_broken": _skip_backfill(_zt_cpos_key, [_yizi, _broken]),
        "probe_prod_skip_lowweak": _skip_backfill(_zt_cpos_key, [_yizi, _low_weak]),
        "probe_prod_skip_hm_d1": _skip_backfill(_zt_cpos_key, [_yizi, _lhb_hm_d1]),
        "probe_prod_skip_dot_d1": _skip_backfill(_zt_cpos_key, [_yizi, _lhb_dot_d1]),
        "probe_prod_skip_crowd_d1": _skip_backfill(_zt_cpos_key, [_yizi, _lhb_crowd_d1]),
        "probe_prod_skip_hm_d": _skip_backfill(_zt_cpos_key, [_yizi, _lhb_hm_d]),
        "probe_prod_skip_netbuy_d1": _skip_backfill(_zt_cpos_key, [_yizi, _lhb_netbuy_d1]),
        "probe_prod_skip_jigou_d1": _skip_backfill(_zt_cpos_key, [_yizi, _lhb_jigou_d1]),
        "probe_prod_skip_hot3_hm_d1": _skip_backfill(
            _zt_cpos_key, [_yizi, lambda i: _hot3(i) or _lhb_hm_d1(i)]),
        "probe_prod_skip_hot3_hm_d": _skip_backfill(
            _zt_cpos_key, [_yizi, lambda i: _hot3(i) or _lhb_hm_d(i)]),
        "probe_prod_skip_hot_hm_d1": _skip_backfill(
            _zt_cpos_key, [_yizi, lambda i: _hot_loc(i) or _lhb_hm_d1(i)]),
        "probe_prod_skip_hot_netbuy": _skip_backfill(
            _zt_cpos_key, [_yizi, lambda i: _hot_loc(i) or _lhb_netbuy_d1(i)]),
        "probe_barbell_15_10": _barbell(15, 10),
        "probe_prod_1ban_tie": _skip_backfill(
            lambda i: (_number(i, "zt_streak") or 0, _one_board_strong(i),
                       -(_number(i, "close_pos_in_day")
                         if _number(i, "close_pos_in_day") is not None else 50.0),
                       not _risk(i)),
            [_yizi]),
        "probe_prod_1ban_top": _skip_backfill(
            lambda i: (_one_board_strong(i), _number(i, "zt_streak") or 0,
                       -(_number(i, "close_pos_in_day")
                         if _number(i, "close_pos_in_day") is not None else 50.0),
                       not _risk(i)),
            [_yizi]),
    }
    return {name: rows[:quota] for name, rows in policies.items()}


def _detailed_aggregate(BT, rows, selected_count):
    """Separate selected, buyable, and complete-forward-window sample counts."""
    valid = [row for row in rows if row and not row.get("unbuyable")]
    unbuyable = sum(bool(row and row.get("unbuyable")) for row in rows)
    missing = max(0, selected_count - len(valid) - unbuyable)
    aggregate = BT.aggregate(rows)
    n = aggregate.get("n", 0)
    t1_wins = sum(row["t1"] > 0 for row in valid)
    t3_wins = sum(row["t3"] > 0 for row in valid)
    return {
        **aggregate,
        "median": median(row["net"] for row in valid) if valid else None,
        "n_selected": selected_count,
        "unbuyable": unbuyable,
        "missing_outcome": missing,
        "t1_win_count": t1_wins,
        "t1_win_rate": t1_wins / n if n else None,
        "t3_win_count": t3_wins,
        "t3_win_rate": t3_wins / n if n else None,
        "deep_rate": aggregate.get("deep", 0) / n if n else None,
    }


def _run_simulation(BT, bars, calendar, day, code):
    result = BT.simulate(bars.get(code) or [], calendar, day, code=code)
    if result is None:
        return None
    result = dict(result, d=day, code=code)
    if not result.get("unbuyable"):
        assert result["entry_day"] > day, ("T+1 违规: 入场未晚于决策日", result, day)
        assert result["exit_day"] > result["entry_day"], ("T+1 违规: 卖出未晚于入场", result)
    return result


def run_backtest(days=10, strategy="post_market", sources="zt,dragon", verbose=True,
                 start=None, end=None):
    """Run the current funnel and same-quota baseline ranking probes."""
    BT, F, H, R, C = _load()
    q = _load_db()
    bars, idx = _load_bars(q)
    cal = _load_calendar(q)
    mk, mk_days = _load_market_klines(H)

    _mode = (os.environ.get("BT_POOL_MODE", "db") or "db").lower()
    if _mode == "recon":
        zt_by_day = _build_zt_by_day(bars, BT.limit_pct)
        _fzt = _make_recon_fetch_zt(zt_by_day)
        C.fetch_zt_pool = _fzt
        F.fetch_zt_pool = _fzt
        if verbose:
            print(f"[recon] 日线重建涨停池：覆盖 {len(zt_by_day)} 个交易日（近似，无 ST/turnover 过滤）")

    decision_days = _pick_decision_days(cal, int(days), start=start, end=end)
    if not decision_days:
        raise ValueError("所选日期范围没有至少 4 个后续交易日的决策日")

    if verbose:
        _win = f"{start or '最新'} ~ {end or '最新'}"
        print(f"选人层回测 | 决策日 {len(decision_days)} 个 "
              f"({decision_days[0]} ~ {decision_days[-1]}) | 区间 {_win} | "
              f"strategy={strategy} sources={sources}")
        print(f"股票池 {len(bars)} 只 | 交易日历 {len(cal)} 天 | 大盘基准 {len(mk_days)} 天")

    per_day, all_rows, daily_reports = [], [], []
    strategy_rows = defaultdict(list)
    for k, day in enumerate(decision_days, 1):
        selected_all, dbg, probe_candidates = _select_one_day(
            R, F, H, C, bars, idx, mk, mk_days, cal, day, strategy, sources
        )
        # 龙虎榜：D-1 昨晚榜 + D 今晚榜（盘后决策两榜均可见，无前视）
        _prior_days = [x for x in cal if x < day]
        lhb = {"d1": _load_lhb_day(q, _prior_days[-1]) if _prior_days else {},
               "d": _load_lhb_day(q, day)}
        probe_sets = _probe_selections(probe_candidates, H, R._BASELINE_N, lhb=lhb)
        probe_sets["production_funnel"] = selected_all

        selected_items = {
            str(item.get("code") or ""): item
            for selection in probe_sets.values()
            for item in selection
            if item.get("code")
        }
        simulations = {
            code: _run_simulation(BT, bars, cal, day, code)
            for code in selected_items
        }

        day_strategies = {}
        for name, selection in probe_sets.items():
            sim_rows = [simulations[str(item["code"])] for item in selection]
            metrics = _detailed_aggregate(BT, sim_rows, len(selection))
            day_strategies[name] = {
                "metrics": metrics,
                "selected_rows": [
                    {
                        "code": str(item["code"]),
                        "name": item.get("name", ""),
                        "source": item.get("source", ""),
                        "risk": H._has_stretched_streak_risk(item),
                        "features": {
                            key: (item.get("tech") or {}).get(key)
                            for key in (
                                "zt_streak", "dist_ma5_pct", "dist_ma20_pct",
                                "pos_in_20d_pct", "ret_5d_pct", "location",
                            )
                        },
                        "simulation": simulations[str(item["code"])],
                    }
                    for item in selection
                ],
            }
            strategy_rows[name].extend(sim_rows)

        production_metrics = day_strategies["production_funnel"]["metrics"]
        per_day.append((day, dbg, production_metrics))
        all_rows.extend(
            simulations[str(item["code"])]
            for item in selected_all
            if simulations[str(item["code"])] is not None
        )
        daily_reports.append({
            "date": day,
            "funnel": {key: value for key, value in dbg.items()
                       if key not in ("obj", "score")},
            "strategies": day_strategies,
        })
        if verbose:
            print(f"  [{k}/{len(decision_days)}] {day} 池{dbg['pool']} "
                  f"基线画像窗{dbg['baseline_profile_window']} "
                  f"hard通过{dbg['baseline_probe_candidates']} → 生产选出{dbg['selected']} "
                  f"（生产有效n={production_metrics.get('n', 0)}）")

    total = _detailed_aggregate(
        BT, all_rows, sum(dbg["selected"] for _, dbg, _ in per_day)
    )
    probe_totals = {
        name: _detailed_aggregate(
            BT, rows,
            sum(len(day["strategies"][name]["selected_rows"]) for day in daily_reports),
        )
        for name, rows in strategy_rows.items()
    }
    return {
        "per_day": per_day,
        "daily": daily_reports,
        "rows": all_rows,
        "total": total,
        "probe_totals": probe_totals,
        "days": decision_days,
        "strategy": strategy,
        "sources": sources,
    }


def _fmt_cell(label, agg, sel_n=None):
    """Format selection counts, outcomes, and gross T+1/T+3 win rates."""
    selected = agg.get("n_selected", 0) if sel_n is None else sel_n
    n = agg.get("n", 0)
    if not n:
        return (f"{label:<31} 选{selected:>3} n=0 买不进{agg.get('unbuyable', 0):>3} "
                f"缺结果{agg.get('missing_outcome', 0):>3}")
    return (
        f"{label:<31} 选{selected:>3} n={n:>3} 净胜{agg['win_rate'] * 100:5.1f}% "
        f"净中位{agg['median']:+6.2f} 触板{agg['touch_rate'] * 100:5.1f}% "
        f"深亏{agg['deep']:>3}/{agg['deep_rate'] * 100:4.1f}% "
        f"T+1 {agg['t1']:+6.2f}/{agg['t1_win_rate'] * 100:5.1f}% "
        f"T+3 {agg['t3']:+6.2f}/{agg['t3_win_rate'] * 100:5.1f}%"
    )


def main():
    days = int(os.environ.get("BT_DAYS", "10"))
    strategy = os.environ.get("BT_STRATEGY", "post_market")
    sources = os.environ.get("BT_SOURCES", "zt,dragon")
    start = os.environ.get("BT_START") or None
    end = os.environ.get("BT_END") or None

    res = run_backtest(days=days, strategy=strategy, sources=sources, start=start, end=end)
    print("\n=== 每日策略对照（净胜率为扣费后净收益>0；T+1/T+3胜率按毛收益>0）===")
    for daily in res["daily"]:
        funnel = daily["funnel"]
        print(f"\n{daily['date']} 池{funnel['pool']} 基线画像窗{funnel['baseline_profile_window']} "
              f"hard通过{funnel['baseline_probe_candidates']}")
        for name, result in daily["strategies"].items():
            print(_fmt_cell(name, result["metrics"]))

    print("\n=== 全周期策略汇总 ===")
    for name, aggregate in res["probe_totals"].items():
        print(_fmt_cell(name, aggregate))

    print("\n=== 口径说明 ===")
    print("· n = 可成交且有完整前向窗口；买不进与缺结果分别统计，不作亏损处理。")
    print("· T+1/T+3 均值及胜率按入场后第1/第3个可卖交易日收盘计算，不含费用。")
    print("· 净收益均值/中位数按触板+10%止盈或第3个可卖日收盘计算，扣往返0.3%；深亏=持有期最低价≤−15%。")
    print("· 探针仅重排决策日画像候选，每日固定保底名额，不引入未来收益字段。")
    print(f"· strategy={res['strategy']} sources={res['sources']} pool_mode={os.environ.get('BT_POOL_MODE','db')}"
          "（hot/search/recent_zt/eod_start 未含）")
    report_path = os.environ.get("BT_OUTPUT", _REPORT_PATH)
    report = {
        "metadata": {
            "author": "Copilot",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "strategy": res["strategy"],
            "sources": res["sources"],
            "decision_days": res["days"],
            "pool_mode": (os.environ.get("BT_POOL_MODE", "db") or "db").lower(),
            "reconstruction": (
                "recon mode rebuilds daily limit-up pools from close/previous-close moves; "
                "it does not reproduce ST-name or turnover filters."
                if (os.environ.get("BT_POOL_MODE", "db") or "db").lower() == "recon"
                else "db mode uses the stored limit-up pool where available."
            ),
            "limitations": [
                "Only point-in-time reconstructable sources in BT_SOURCES are included.",
                "hot/search/recent_zt/eod_start are excluded from this historical replay.",
                "Ranking probes are exploratory and do not change production behavior.",
            ],
        },
        "summary": res["probe_totals"],
        "daily": res["daily"],
    }
    report_dir = os.path.dirname(report_path)
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print("逐日探针 JSON:", report_path)
    return res


# ── pytest 入口（真实数据可用时执行；mock 环境被 skip）──────────────────────
@pytest.mark.skipif(_MOCKED, reason=_REAL_DATA_SKIP_REASON)
def test_selection_layer_backtest_recent_days():
    """真实数据可用时：跑最近若干决策日，断言报表口径齐全、样本可算出。"""
    days = int(os.environ.get("BT_DAYS", "10"))
    res = run_backtest(days=min(days, 10), verbose=False)
    total = res["total"]
    assert "n" in total, total
    for key in ("win_rate", "touch_rate", "deep", "t1", "t3", "net",
                "t1_win_rate", "t3_win_rate", "median", "deep_rate",
                "n_selected", "unbuyable",
                "missing_outcome"):
        assert key in total, (key, total)
    assert total["n"] >= 0, total
    for _day, dbg, _agg in res["per_day"]:
        for k in ("pool", "alive", "baseline", "additive", "selected",
                  "baseline_profile_window", "baseline_probe_candidates"):
            assert k in dbg, (k, dbg)


def test_detailed_aggregate_counts_outcomes_and_t1_t3_rates():
    class Engine:
        @staticmethod
        def aggregate(rows):
            valid_rows = [row for row in rows if row and not row.get("unbuyable")]
            if not valid_rows:
                return {"n": 0}
            return {
                "n": len(valid_rows),
                "win_rate": sum(row["net"] > 0 for row in valid_rows) / len(valid_rows),
                "touch_rate": sum(row["touch"] for row in valid_rows) / len(valid_rows),
                "deep": sum(row["deep"] for row in valid_rows),
                "median": sorted(row["net"] for row in valid_rows)[len(valid_rows) // 2],
                "t1": sum(row["t1"] for row in valid_rows) / len(valid_rows),
                "t3": sum(row["t3"] for row in valid_rows) / len(valid_rows),
                "net": sum(row["net"] for row in valid_rows) / len(valid_rows),
            }

    rows = [
        {"net": 1.0, "touch": True, "deep": False, "t1": 2.0, "t3": 3.0},
        {"net": -1.0, "touch": False, "deep": True, "t1": -2.0, "t3": -3.0},
        {"unbuyable": True},
        None,
    ]
    result = _detailed_aggregate(Engine, rows, selected_count=4)
    assert result["n"] == 2
    assert result["median"] == 0
    assert result["unbuyable"] == 1
    assert result["missing_outcome"] == 1
    assert result["t1_win_count"] == 1
    assert result["t1_win_rate"] == 0.5
    assert result["t3_win_count"] == 1
    assert result["t3_win_rate"] == 0.5
    assert result["deep_rate"] == 0.5


def test_probe_selections_keep_fixed_quota_and_use_stable_known_feature_order():
    class Helpers:
        @staticmethod
        def _has_stretched_streak_risk(item):
            tech = item.get("tech") or {}
            return tech.get("zt_streak", 0) >= 3 and tech.get("dist_ma5_pct", 0) > 10

    candidates = [
        {"code": "A", "tech": {"zt_streak": 4, "dist_ma5_pct": 12, "ret_5d_pct": 15,
                               "location": "hot"}},
        {"code": "B", "tech": {"zt_streak": 1, "dist_ma5_pct": 1, "ret_5d_pct": 2,
                               "location": "cool"}},
        {"code": "C", "tech": {"zt_streak": 2, "dist_ma5_pct": 3, "ret_5d_pct": 5,
                               "location": "normal"}},
    ]
    selections = _probe_selections(candidates, Helpers, quota=2)
    assert all(len(selection) == 2 for selection in selections.values())
    assert [item["code"] for item in selections["baseline_pool_order"]] == ["A", "B"]
    assert [item["code"] for item in selections["baseline_risk_soft_order"]] == ["B", "C"]
    assert [item["code"] for item in selections["probe_low_streak"]] == ["B", "C"]
    assert [item["code"] for item in selections["probe_cool_location"]] == ["B", "C"]


if __name__ == "__main__":
    main()
