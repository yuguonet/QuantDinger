# -*- coding: utf-8 -*-
"""分层配额选股：每日盘后要分析哪些标的（v5 阶段 D2，2026-10-13）。

【为什么必须配额，不能"活跃股多就多采"】
v5 §19.1 的统计第一性：名义案例数涨到 10 万，**独立簇数一分不涨**
（同日样本高度相关 ⇒ DEFF_day≈33.6，150 只/日只折合约 4.5 个独立观察）。
若任由活跃度决定采集量，采集分布会随行情漂移：涨的日子全采强势股、跌的日子全采
弱势股 ⇒ **条件格与行情状态共线**，而行情状态恰恰是我们想拿来当条件的变量。
⇒ 配额**按 date 强制**：每天固定 hot/warm/cold 三层比例，与当日活跃度无关。

【三层怎么定义（均为 2026-10-13 实测可用的源，勿凭记忆改）】
    hot  活跃 60%：涨停池(zt) + 炸板池(broken) + 龙虎榜 + 热榜
    warm 中热 25%：热门板块的成分股，剔除已在 hot 的
    cold 低热 15%：全市场随机，剔除已在 hot/warm 的

【★ 低热层为什么绝不能省】
它是**无偏基线**。没有它，所有条件效应都失去参照 —— 等于把对照组自己拆了
（这正是 v4 G7「无对照臂」的同类错误，v5 靠 p_base 才关掉）。

【★ 缺额不重新分配】
某层源挂了就留空并记进 `shortage`，**不**把名额挪给别的层。
挪了名额表面看采集量达标，实际是把「采集质量退化」藏起来了 —— 事后无法区分
「今天确实没有这类股」与「这个源挂了」。

【随机为什么是确定性的】
cold 层用 `random.Random(as_of)`（以日期为种子）：同日重跑结果一致（幂等、
可复现），不同日自然换一批。若用全局 random，补跑一次就换一批样本，
而标签已按前一批落库 ⇒ 样本与标签对不上。

【易错处】
· 各源返回的股票代码字段名不一致（code / symbol），统一归一成 6 位字符串。
· 三层**互不重叠**：同一标的一天内被采两次，名义上是 2 条样本，独立信息量仍是 1。
· 比例走 env（禁止硬编码）：QD_SAMPLE_HOT / QD_SAMPLE_WARM。
"""
from __future__ import annotations

import os
import random
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..log import logger

# ── 配额（env 可覆盖；这里的数字是"没配时的起点"，不是业务常量）
RATIO_HOT = float(os.getenv("QD_SAMPLE_HOT", "0.60"))
RATIO_WARM = float(os.getenv("QD_SAMPLE_WARM", "0.25"))
# cold = 1 - hot - warm（吃余额，避免取整丢样本）

# 热榜：前 N 名算 hot，**其后的名次算 warm —— 热度分层本来就是连续的**，
# 与其再引入一个不稳定接口，不如用同一个已验证的源切两段。
HOT_RANK_N = int(os.getenv("QD_SAMPLE_HOT_RANK_N", "300"))
HOT_RANK_HOT_N = int(os.getenv("QD_SAMPLE_HOT_RANK_HOT_N", "100"))

LAYERS = ("hot", "warm", "cold")
_SCHEMA_VER = "smp1"


def _ratio_hot() -> float:
    try:
        return float(os.getenv("QD_SAMPLE_HOT", str(RATIO_HOT)))
    except (TypeError, ValueError):
        return RATIO_HOT


def _ratio_warm() -> float:
    try:
        return float(os.getenv("QD_SAMPLE_WARM", str(RATIO_WARM)))
    except (TypeError, ValueError):
        return RATIO_WARM


def _norm_code(v: Any) -> str:
    """代码归一：去前后缀与空格，只留 6 位数字。非 A 股代码返回空串。"""
    s = str(v or "").strip()
    if "." in s:
        s = s.split(".")[0]
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _codes(rows: Any, key: str = "code") -> List[str]:
    """从源返回的 dict/list 里抽代码（兼容 {stocks:[...]} 与裸 list 两种形态）。"""
    if isinstance(rows, dict):
        for k in ("stocks", "data", "items"):
            if isinstance(rows.get(k), list):
                rows = rows[k]
                break
        else:
            rows = []
    out: List[str] = []
    for r in rows or []:
        if isinstance(r, dict):
            # ★ 各源字段名不统一，实测见到的有：
            #   dragon_tools.get_limit_pool → stock_code
            #   basicinfo_db.get_all_stocks → symbol
            #   sector_analysis_tools.get_sector_stocks → code
            # 漏掉 stock_code 会让涨停池 46 只全被抽成空（2026-10-13 实测踩到）。
            c = _norm_code(r.get(key) or r.get("code") or r.get("symbol")
                           or r.get("stock_code") or r.get("sec_code"))
        else:
            c = _norm_code(r)
        if c:
            out.append(c)
    return out


# ═══════════════════════════════════════════════════════════════
#  生产数据源
# ═══════════════════════════════════════════════════════════════
def _default_sources() -> Dict[str, Callable[..., Any]]:
    # ⚠ 2026-10-13 事故修复：原本写 `app.agent.tools.finance`（改名后该路径指向新版
    # app/agent，其中没有 finance 子包）⇒ ImportError 直接炸掉 `_default_sources()`。
    # 相对导入锚回本包自己的 tools/finance。与 chain/context_frame.py 同款病。
    from ..tools.finance import dragon_tools, sector_analysis_tools

    def _limit(d: str, t: str) -> Optional[dict]:
        try:
            return dragon_tools.get_limit_pool(date=d, pool_type=t)
        except Exception as e:
            logger.debug("[sampler] 涨停/炸板池失败(%s): %s", t, e)
            return None

    def _dragon(d: str) -> Optional[dict]:
        try:
            return dragon_tools.get_dragon_tiger(date=d, days=1)
        except Exception as e:
            logger.debug("[sampler] 龙虎榜失败: %s", e)
            return None

    def _hot(n: int) -> Optional[dict]:
        try:
            return dragon_tools.get_hot_rank(top_n=n)
        except Exception as e:
            logger.debug("[sampler] 热榜失败: %s", e)
            return None

    def _sectors() -> Optional[dict]:
        try:
            return sector_analysis_tools.get_hot_sectors()
        except Exception as e:
            logger.debug("[sampler] 热门板块失败: %s", e)
            return None

    def _sector_stocks(name: str) -> Optional[dict]:
        # ★ 必须用 **board_name** 而不是 board_code：get_hot_sectors 返回的 code
        #   形如 "new_cmyl"，而 get_sector_stocks 会判它「非法板块代码」（实测
        #   2026-10-13：5 个板块全部报 {code:0, msg:'非法板块代码'}）。
        #   sector_analysis_tools 内部有 _resolve_board_code(name) 做名字→代码映射。
        try:
            return sector_analysis_tools.get_sector_stocks(board_name=name)
        except Exception as e:
            logger.debug("[sampler] 板块成分股失败(%s): %s", name, e)
            return None

    def _all() -> Optional[list]:
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            return get_stock_basic_db().get_all_stocks(status="active")
        except Exception as e:
            logger.debug("[sampler] 全市场列表失败: %s", e)
            return None

    return {"limit_pool": _limit, "dragon_tiger": _dragon, "hot_rank": _hot,
            "hot_sectors": _sectors, "sector_stocks": _sector_stocks,
            "all_stocks": _all}


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════
def pick_daily_universe(as_of: str, n: int,
                        sources: Optional[Dict[str, Callable[..., Any]]] = None,
                        rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """按分层配额挑出当日待分析标的。

    Returns:
        {"as_of","n","schema_ver","items":[{"code","sample_layer","source"}…],
         "quota":{"hot":…,"warm":…,"cold":…},
         "shortage":{"层":缺几条}, "missing":{"源":"原因"}}
    """
    src = sources or _default_sources()
    missing: Dict[str, str] = {}
    n = max(0, int(n or 0))

    quota = _quota(n)
    rnd = rng if rng is not None else random.Random(str(as_of))

    def _call(key: str, *a):
        fn = src.get(key)
        if fn is None:
            return None
        try:
            return fn(*a)
        except Exception as e:
            missing[key] = f"{type(e).__name__}: {e}"
            return None

    # ── hot：涨停 + 炸板 + 龙虎榜 + 热榜
    hot: List[str] = []
    # ★ limit_pool 的返回是 {date, <pool_type>: {count, stocks}} —— stocks **嵌在
    #   pool_type 子键里**，不是顶层。直接 _codes(r) 只会拿到空（实测 hot 层全空）。
    for key, pool_type, getter in (
        ("limit_pool", "zt", lambda: _call("limit_pool", as_of, "zt")),
        ("limit_pool", "broken", lambda: _call("limit_pool", as_of, "broken")),
        ("dragon_tiger", None, lambda: _call("dragon_tiger", as_of)),
    ):
        r = getter()
        if r is None:
            missing.setdefault(key, "源不可用")
            continue
        node = r.get(pool_type) if (pool_type and isinstance(r, dict)) else r
        got = _codes(node)
        if pool_type and not got:
            missing.setdefault(f"limit_pool:{pool_type}", "该池无成分股")
        hot.extend(got)

    # 热榜：前 _HOT 名进 hot，其余留给 warm（见常量处注释）
    rank_all: List[str] = []
    hr = _call("hot_rank", HOT_RANK_N)
    if hr is None:
        missing.setdefault("hot_rank", "源不可用")
    else:
        rank_all = _codes(hr)
        hot.extend(rank_all[:HOT_RANK_HOT_N])
        if not rank_all:
            missing.setdefault("hot_rank", "热榜为空（可能未开盘/源未更新）")

    # ── warm：热榜延伸段 + 热门板块成分股（两源并集，剔除 hot 后由调用方去重）
    warm: List[str] = list(rank_all[HOT_RANK_HOT_N:])
    hs = _call("hot_sectors")
    if hs is None:
        missing.setdefault("hot_sectors", "源不可用")
    else:
        boards = (hs.get("industry") or []) + (hs.get("concept") or [])
        for b in boards[:5]:                       # 只取前 5 个板块，别把 warm 撑成全市场
            if not isinstance(b, dict):
                continue
            bn = b.get("name") or ""
            if not bn:
                continue
            ss = _call("sector_stocks", bn)
            if ss is None:
                missing.setdefault("sector_stocks", "源不可用")
                continue
            warm.extend(_codes(ss))

    # ── cold：全市场（剔除 hot/warm 后随机）
    cold: List[str] = []
    allrows = _call("all_stocks")
    if allrows is None:
        missing.setdefault("all_stocks", "源不可用")
    else:
        cold = _codes(allrows, key="symbol")

    # ── 分层去重：hot 优先，warm 次之，cold 最后
    seen: set[str] = set()
    pools: Dict[str, List[str]] = {}
    for layer, raw in (("hot", hot), ("warm", warm), ("cold", cold)):
        keep = []
        for c in raw:
            if c in seen:
                continue
            seen.add(c)
            keep.append(c)
        pools[layer] = keep
    # cold 池里被 hot/warm 占掉的要剔掉（cold 是最后一层，seen 此时已含 hot+warm）
    pools["cold"] = [c for c in pools["cold"] if c not in
                     set(pools["hot"]) | set(pools["warm"])]

    # ── 按配额取样（cold 随机，其余按源顺序稳定取）
    items: List[Dict[str, Any]] = []
    shortage: Dict[str, int] = {}
    for layer in LAYERS:
        want = quota[layer]
        pool = pools.get(layer) or []
        if layer == "cold":
            take = pool if len(pool) <= want else rnd.sample(pool, want)
        else:
            take = pool[:want]
        if len(take) < want:
            shortage[layer] = want - len(take)
        for c in take:
            items.append({"code": c, "sample_layer": layer})

    return {
        "as_of": str(as_of)[:10], "n": n, "schema_ver": _SCHEMA_VER,
        "items": items, "quota": quota, "shortage": shortage, "missing": missing,
    }


def _quota(n: int) -> Dict[str, int]:
    """floor 分配，cold 吃余额（避免取整丢样本）⇒ 150 ⇒ 90/37/23。"""
    h = int(n * _ratio_hot())
    w = int(n * _ratio_warm())
    c = n - h - w
    return {"hot": h, "warm": w, "cold": max(0, c)}
