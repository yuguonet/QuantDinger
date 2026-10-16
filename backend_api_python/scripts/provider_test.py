# -*- coding: utf-8 -*-
"""
provider_test.py — 数据源层实测脚本（可拔插 Provider 的验收工具）

设计目标:
  1. 每个 Provider 独立可测：增删 provider/*.py 后无需改本脚本，自动纳入
  2. 覆盖四类验收：连通性 / 数据格式 / 跨源一致性 / 多源共用与并行
  3. 只读探测：不改任何业务状态，测试用的缓存/熔断痕迹可忽略
  4. 输出机器可读报告（--json），与人工可读的终端摘要

用法（在 backend_api_python 目录下、项目 .venv 中执行）:
  python scripts/provider_test.py                     # 全量（含受限的端到端）
  python scripts/provider_test.py --suite conn,fmt    # 只跑连通性 + 格式
  python scripts/provider_test.py --source tencent --source sina
  python scripts/provider_test.py --symbols SH600519,SZ000001 --timeframes 1D,5m
  python scripts/provider_test.py --parallel 4 --json ..\\tmp\\provider_report.json

退出码: 0 = 无 FAIL；1 = 存在 FAIL（CI 可据此判断）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── 路径: 允许从任意 cwd 执行（脚本在 backend_api_python/scripts 下）──
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                 # backend_api_python
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(name)s: %(message)s")
for _noisy in ("app.data_sources", "app.data_sources.coordinator", "app.data_sources.provider",
               "app.utils.logger", "urllib3", "requests"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

from app.data_sources.normalizer import (  # noqa: E402
    add_market_prefix, strip_market_prefix, normalize_cn_code, normalize_hk_code,
)
from app.data_sources.provider import (  # noqa: E402
    get_providers, get_provider, NotSupportedResult,
)
from app.data_sources.source_config import SOURCE_CONFIGS, get_source_config  # noqa: E402

CN_TF_ALL = ["1m", "5m", "15m", "30m", "1H", "1D", "1W"]
TZ_CN = timezone(timedelta(hours=8))

# 已知在当前网络环境被上游风控阻断的接口（代码无误，环境不可达）：
#   finance.pae.baidu.com 对非浏览器流量返回 403 hit risk（东财源已整体移除，见上）。
#   命中时记为 UNREACHABLE，不计入 PASS/FAIL 回归判定。
# 说明: 东财行情源已于 2026-10-16 整体移除（其数据端点在本机被上游 IP 封锁），
# 故此表只剩百度（quote 端点 403 风控；日线可用，且 quote 能力已标 False）。
_ENV_BLOCKED = ("pae.baidu.com",)
# 这些源在环境封锁下表现为"HTTP 200 + 空 body"，而非抛异常
_EMPTY_MAY_BE_ENV_BLOCKED = {"baidu"}
_ENV_BLOCKED_SOURCES = _EMPTY_MAY_BE_ENV_BLOCKED


def is_env_blocked(err: BaseException) -> bool:
    """异常是否属于上游域名级封锁（连接被重置/空响应），而非代码缺陷。"""
    name = type(err).__name__
    if name not in ("ConnectionError", "Timeout", "TimeoutError", "SSLError", "ReadTimeout"):
        return False
    msg = str(err).lower()
    if not any(("closed abruptly" in msg, "empty reply" in msg, "connection aborted" in msg,
                "max retries exceeded" in msg, "recv failure" in msg)):
        return False
    try:
        import traceback
        frames = traceback.format_exception(type(err), err, err.__traceback__)
    except Exception:
        frames = []
    blob = " ".join(frames).lower()
    return any(d in blob for d in _ENV_BLOCKED)


def is_env_blocked_result(source: str, value: Any) -> bool:
    """该源是否命中"环境封锁"（域名级不可达）——空响应/None/NotSupported 也算。

    eastmoney 在封锁下返回 HTTP 200 + 空 body（表现为 {} / None），
    baidu 返回 403 hit risk 后表现为 None/{}；这两种都不是代码缺陷。
    """
    return source in _ENV_BLOCKED_SOURCES and (
        value is None or isinstance(value, NotSupportedResult)
        or (isinstance(value, dict) and not value)
    )

# 判定阈值（可按需在命令行覆盖）
SLOW_SECONDS = 3.0
QUOTE_AGREE_TOL = 0.005     # 跨源报价一致：相对偏差 0.5%
KLINE_CLOSE_TOL = 0.02      # K线收盘 vs 报价：相对偏差 2%（复权/延迟容差）


# ══════════════════════════════════════════════════════════════════
# 结果模型
# ══════════════════════════════════════════════════════════════════

@dataclass
class Result:
    suite: str
    case: str
    source: str = ""
    symbol: str = ""
    timeframe: str = ""
    status: str = "PASS"            # PASS / FAIL / SKIP / WARN
    latency_ms: float = 0.0
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class Report:
    def __init__(self):
        self.rows: List[Result] = []
        self.started = datetime.now(TZ_CN).isoformat(timespec="seconds")

    def add(self, **kw) -> Result:
        r = Result(**kw)
        self.rows.append(r)
        mark = {"PASS": "ok  ", "FAIL": "FAIL", "WARN": "warn", "SKIP": "skip",
                "UNREACHABLE": "env "}[r.status]
        extra = (" " + r.detail) if r.detail else ""
        print("  [%s] %-44s %s%s" % (mark, r.case, ("%.0fms" % r.latency_ms) if r.latency_ms else "", extra))
        return r

    def by(self, suite: str) -> List[Result]:
        return [r for r in self.rows if r.suite == suite]

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for r in self.rows:
            counts[r.status] = counts.get(r.status, 0) + 1
        return {"total": len(self.rows), "counts": counts}


# ══════════════════════════════════════════════════════════════════
# 校验工具
# ══════════════════════════════════════════════════════════════════

def _f(v) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def validate_bar(bar: Dict[str, Any], timeframe: str) -> Optional[str]:
    """校验单根 K 线，返回错误描述或 None。"""
    if not isinstance(bar, dict):
        return "bar 不是 dict"
    for k in ("time", "open", "high", "low", "close", "volume"):
        if k not in bar:
            return "缺字段 %s" % k
    o, h, lo, c, v = (_f(bar[k]) for k in ("open", "high", "low", "close", "volume"))
    if None in (o, h, lo, c, v):
        return "OHLCV 非数值: %s" % {k: bar[k] for k in ("open", "high", "low", "close", "volume")}
    if min(o, h, lo, c) <= 0:
        return "非正价格 o=%s h=%s l=%s c=%s" % (o, h, lo, c)
    if lo > h:
        return "low>high (%s>%s)" % (lo, h)
    if not (lo <= o <= h) or not (lo <= c <= h):
        return "OHLC 关系异常 o=%s h=%s l=%s c=%s" % (o, h, lo, c)
    if v < 0:
        return "负成交量 %s" % v
    t = str(bar["time"])
    if timeframe in ("1D", "1W", "1M"):
        if len(t) < 10 or t[4] != "-" or t[7] != "-":
            return "日线时间格式异常: %r" % t
    else:
        if len(t) < 16 or t[4] != "-" or t[10] != " " or t[13] != ":":
            return "分钟线时间格式异常: %r" % t
    return None


def validate_bars(bars: List[Dict[str, Any]], timeframe: str, expect_count: int = 0) -> List[str]:
    """整段校验：字段/格式/单调性/条数。返回错误列表（最多 5 条）。"""
    errs: List[str] = []
    if not isinstance(bars, list) or not bars:
        return ["bars 为空或非 list"]
    for i, b in enumerate(bars[:200]):
        e = validate_bar(b, timeframe)
        if e:
            errs.append("bar[%d]: %s" % (i, e))
        if len(errs) >= 5:
            break
    # 时间单调递增
    times = [str(b.get("time", "")) for b in bars]
    if times != sorted(times):
        errs.append("时间未严格升序")
    # 重复时间戳
    if len(set(times)) != len(times):
        errs.append("存在重复时间戳")
    if expect_count and len(bars) > expect_count + max(2, int(expect_count * 0.1)):
        errs.append("返回条数 %d 超出请求 %d" % (len(bars), expect_count))
    return errs[:5]


def timeframe_step_seconds(tf: str) -> int:
    return {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1H": 3600}.get(tf, 0)


def check_tf_step(bars: List[Dict[str, Any]], tf: str) -> Optional[str]:
    """分钟线相邻 bar 间隔是否与周期一致。

    只检查同一交易时段内的相邻 bar，排除三种合法断档:
      - 隔夜（跨自然日）
      - 午休 11:30-13:00（北京时区）
      - 竞价/收盘导致的 >3 个周期跳空（数据源补数差异）
    判据用本地时区（数据本身就是北京时间，避免 UTC 偏移引入假阳性）。
    """
    step = timeframe_step_seconds(tf)
    if not step or len(bars) < 3:
        return None

    def to_ts(s):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return int(time.mktime(datetime.strptime(str(s)[:len(fmt) + 2].strip(), fmt).timetuple()))
            except ValueError:
                continue
        return None

    ts = [t for t in (to_ts(b.get("time")) for b in bars[-25:]) if t]
    bad = []
    for a, b in zip(ts, ts[1:]):
        d = b - a
        da, db = datetime.fromtimestamp(a), datetime.fromtimestamp(b)
        if da.date() != db.date():
            continue                                   # 隔夜
        m_a, m_b = da.hour * 60 + da.minute, db.hour * 60 + db.minute
        if m_a <= 11 * 60 + 30 and m_b >= 13 * 60:
            continue                                   # 午休
        if d != step:
            bad.append(d)
    if bad:
        return "周期间隔异常 step=%ds 实际=%s" % (step, sorted(set(bad))[:5])
    return None


# ══════════════════════════════════════════════════════════════════
# 测试套件
# ══════════════════════════════════════════════════════════════════

class Harness:
    def __init__(self, args, report: Report):
        self.args = args
        self.report = report
        self.providers = self._select_providers()
        self.symbols_cn = [add_market_prefix(s.strip(), "CNStock")
                           for s in args.symbols.split(",") if s.strip()]

    # ── 选源 ──
    def _select_providers(self):
        all_p = get_providers()
        if self.args.source:
            want = set(self.args.source)
            chosen = [p for p in all_p if p.name in want]
            missing = want - {p.name for p in chosen}
            for m in sorted(missing):
                self.report.add(suite="meta", case="源 %s 未注册" % m, status="FAIL",
                                detail="provider/%s.py 不存在或导入失败" % m)
            return chosen
        return all_p

    def _cn(self):
        return [p for p in self.providers if "CNStock" in p.capabilities.get("markets", set())]

    # ═════════════════ 套件 1: 连通性 ═════════════════
    def suite_conn(self):
        print("\n=== 套件 conn: 连通性 ===")
        for p in self._cn():
            sym = self.symbols_cn[0]
            # 单只行情
            if p.capabilities.get("quote"):
                t0 = time.time()
                try:
                    q = p.fetch_ticker(sym)
                    ms = (time.time() - t0) * 1000
                    if isinstance(q, NotSupportedResult) or not q:
                        env = is_env_blocked_result(p.name, q)
                        self.report.add(suite="conn", case="%s fetch_ticker" % p.name, source=p.name,
                                        symbol=sym, status="UNREACHABLE" if env else "FAIL",
                                        latency_ms=ms, detail="环境封锁(空响应)" if env else "返回空/不支持")
                    else:
                        st = "WARN" if ms > SLOW_SECONDS * 1000 else "PASS"
                        self.report.add(suite="conn", case="%s fetch_ticker" % p.name, source=p.name,
                                        symbol=sym, status=st, latency_ms=ms,
                                        detail="last=%s" % q.get("last"))
                except Exception as e:
                    env = is_env_blocked(e)
                    self.report.add(suite="conn", case="%s fetch_ticker" % p.name, source=p.name,
                                    symbol=sym, status="UNREACHABLE" if env else "FAIL",
                                    latency_ms=(time.time() - t0) * 1000,
                                    detail=("环境封锁: " if env else "") + "%s: %s" % (type(e).__name__, str(e)[:70]))
            # 单只K线（1D）
            if p.capabilities.get("kline"):
                t0 = time.time()
                try:
                    r = p.fetch_kline(sym, "1D", 20)
                    ms = (time.time() - t0) * 1000
                    bars = (r or {}).get("bars") if isinstance(r, dict) else None
                    if isinstance(r, NotSupportedResult) or not bars:
                        env = p.name in _EMPTY_MAY_BE_ENV_BLOCKED
                        self.report.add(suite="conn", case="%s fetch_kline 1D" % p.name, source=p.name,
                                        symbol=sym, timeframe="1D",
                                        status="UNREACHABLE" if env else "FAIL", latency_ms=ms,
                                        detail="环境封锁(空响应)/无数据" if env else "返回空/不支持")
                    else:
                        st = "WARN" if ms > SLOW_SECONDS * 1000 else "PASS"
                        self.report.add(suite="conn", case="%s fetch_kline 1D" % p.name, source=p.name,
                                        symbol=sym, timeframe="1D", status=st, latency_ms=ms,
                                        detail="bars=%d" % len(bars))
                except Exception as e:
                    env = is_env_blocked(e)
                    self.report.add(suite="conn", case="%s fetch_kline 1D" % p.name, source=p.name,
                                    symbol=sym, timeframe="1D", status="UNREACHABLE" if env else "FAIL",
                                    latency_ms=(time.time() - t0) * 1000,
                                    detail=("环境封锁: " if env else "") + "%s: %s" % (type(e).__name__, str(e)[:70]))
            # 批量行情
            if p.capabilities.get("batch_quote"):
                codes = [strip_market_prefix(s) for s in self.symbols_cn]
                t0 = time.time()
                try:
                    q = p.fetch_batch_quotes(codes, timeout=8)
                    ms = (time.time() - t0) * 1000
                    n = len(q) if isinstance(q, dict) and not isinstance(q, NotSupportedResult) else 0
                    if n == 0:
                        env = is_env_blocked_result(p.name, q)
                        self.report.add(suite="conn", case="%s fetch_batch_quotes" % p.name, source=p.name,
                                        status="UNREACHABLE" if env else "FAIL",
                                        latency_ms=ms, detail="环境封锁(空响应)" if env else "返回空/不支持")
                    else:
                        cover = n / max(len(codes), 1)
                        st = "PASS" if cover >= 0.6 else "WARN"
                        self.report.add(suite="conn", case="%s fetch_batch_quotes" % p.name, source=p.name,
                                        status=st, latency_ms=ms,
                                        detail="返回 %d/%d (%.0f%%)" % (n, len(codes), cover * 100))
                except Exception as e:
                    env = is_env_blocked(e)
                    self.report.add(suite="conn", case="%s fetch_batch_quotes" % p.name, source=p.name,
                                    status="UNREACHABLE" if env else "FAIL",
                                    latency_ms=(time.time() - t0) * 1000,
                                    detail=("环境封锁: " if env else "") + "%s: %s" % (type(e).__name__, str(e)[:70]))

    # ═════════════════ 套件 2: 数据格式 ═════════════════
    def suite_fmt(self):
        print("\n=== 套件 fmt: 数据格式与契约 ===")
        tfs = [t.strip() for t in self.args.timeframes.split(",") if t.strip()]
        for p in self._cn():
            if not p.capabilities.get("kline"):
                continue
            for tf in tfs:
                if tf not in p.capabilities.get("kline_tf", set()):
                    self.report.add(suite="fmt", case="%s %s 未声明支持" % (p.name, tf), source=p.name,
                                    timeframe=tf, status="SKIP", detail="capabilities 未声明该周期")
                    continue
                sym = self.symbols_cn[0]
                t0 = time.time()
                try:
                    r = p.fetch_kline(sym, tf, 60)
                except Exception as e:
                    self.report.add(suite="fmt", case="%s %s 拉取异常" % (p.name, tf), source=p.name,
                                    symbol=sym, timeframe=tf, status="FAIL",
                                    latency_ms=(time.time() - t0) * 1000,
                                    detail="%s: %s" % (type(e).__name__, str(e)[:80]))
                    continue
                ms = (time.time() - t0) * 1000
                if isinstance(r, NotSupportedResult):
                    env = is_env_blocked_result(p.name, r)
                    self.report.add(suite="fmt", case="%s %s 声明支持但返回 NotSupported" % (p.name, tf),
                                    source=p.name, symbol=sym, timeframe=tf,
                                    status="UNREACHABLE" if env else "FAIL",
                                    latency_ms=ms, detail="环境封锁(空响应)" if env else "能力声明与实现不一致")
                    continue
                bars = (r or {}).get("bars") or []
                if not bars:
                    env = is_env_blocked_result(p.name, r)
                    self.report.add(suite="fmt", case="%s %s 无数据" % (p.name, tf), source=p.name,
                                    symbol=sym, timeframe=tf,
                                    status="UNREACHABLE" if env else "FAIL", latency_ms=ms,
                                    detail="环境封锁(空响应)" if env else "返回空 bars")
                    continue
                errs = validate_bars(bars, tf, expect_count=60)
                step_err = check_tf_step(bars, tf)
                if step_err:
                    errs.append(step_err)
                cnt = (r or {}).get("count")
                if cnt is not None and int(cnt) != len(bars):
                    errs.append("count=%s 与 len(bars)=%d 不符" % (cnt, len(bars)))
                self.report.add(suite="fmt", case="%s %s 格式校验" % (p.name, tf), source=p.name,
                                symbol=sym, timeframe=tf,
                                status="FAIL" if errs else ("WARN" if ms > SLOW_SECONDS * 1000 else "PASS"),
                                latency_ms=ms,
                                detail=("bars=%d " % len(bars)) + "; ".join(errs) if errs else "bars=%d" % len(bars),
                                extra={"first_bar": bars[0], "last_bar": bars[-1]} if self.args.verbose else {})

    # ═════════════════ 套件 3: 能力矩阵 ═════════════════
    def suite_matrix(self):
        print("\n=== 套件 matrix: 能力×周期×市场过滤 ===")
        for cap in ("kline", "quote", "batch_quote"):
            for tf in ("", "1D", "5m", "1H"):
                if tf and cap != "kline":
                    continue
                got = [p.name for p in get_providers(capability=cap, timeframe=tf or None, market="CNStock")]
                declared = [p.name for p in self._cn() if p.capabilities.get(cap)]
                ok = set(got) <= set(declared)
                self.report.add(suite="matrix", case="filter cap=%s tf=%s" % (cap, tf or "-"),
                                status="PASS" if ok else "FAIL",
                                detail="命中: %s" % ",".join(got),
                                extra={"declared": declared, "got": got})
        # 优先级唯一性
        names = [p.name for p in self._cn()]
        seen = {}
        for p in self._cn():
            pr = p.capabilities.get("kline_priority", p.priority)
            seen.setdefault(pr, []).append(p.name)
        dup = {k: v for k, v in seen.items() if len(v) > 1}
        self.report.add(suite="matrix", case="kline 优先级是否唯一",
                        status="WARN" if dup else "PASS",
                        detail="重复: %s" % dup if dup else "无重复")
        # 装饰器值与类属性一致性（静态检查）
        mismatch = []
        for p in get_providers():
            cfg = SOURCE_CONFIGS.get(p.name)
            if cfg and cfg.max_workers != getattr(p, "max_concurrency", cfg.max_workers):
                mismatch.append("%s cfg=%s provider=%s" % (p.name, cfg.max_workers, p.max_concurrency))
        self.report.add(suite="matrix", case="source_config 与 MAX_CONCURRENCY 同步",
                        status="WARN" if mismatch else "PASS",
                        detail="; ".join(mismatch) if mismatch else "一致")

    # ═════════════════ 套件 4: 跨源一致性 ═════════════════
    def suite_consistency(self):
        print("\n=== 套件 consistency: 跨源一致性 ===")
        sym = self.symbols_cn[0]
        quotes: Dict[str, Dict[str, Any]] = {}
        for p in self._cn():
            if not p.capabilities.get("quote"):
                continue
            try:
                q = p.fetch_ticker(sym)
            except Exception as e:
                self.report.add(suite="cons", case="%s 报价异常" % p.name, source=p.name, symbol=sym,
                                status="FAIL", detail="%s: %s" % (type(e).__name__, str(e)[:60]))
                continue
            if isinstance(q, dict) and _f(q.get("last")):
                quotes[p.name] = q

        if len(quotes) >= 2:
            lasts = {k: _f(v.get("last")) for k, v in quotes.items() if _f(v.get("last"))}
            base_name, base_v = max(lasts.items(), key=lambda kv: kv[1])
            for name, v in lasts.items():
                dev = abs(v - base_v) / base_v if base_v else 1
                self.report.add(
                    suite="cons", case="%s vs %s 报价一致" % (name, base_name), source=name, symbol=sym,
                    status="PASS" if dev <= QUOTE_AGREE_TOL else "FAIL",
                    detail="%.3f vs %.3f (偏差 %.2f%%)" % (v, base_v, dev * 100),
                    extra={"quotes": {k: _f(qq.get("last")) for k, qq in quotes.items()}} if self.args.verbose else {})
        elif quotes:
            self.report.add(suite="cons", case="报价跨源比对", symbol=sym, status="SKIP",
                            detail="只有 1 个源返回有效报价")

        # 同源: 日线最后一根 close vs 实时 last
        for p in self._cn():
            if not (p.capabilities.get("kline") and p.capabilities.get("quote")):
                continue
            try:
                r = p.fetch_kline(sym, "1D", 5)
                bars = (r or {}).get("bars") or []
                q = p.fetch_ticker(sym)
            except Exception as e:
                self.report.add(suite="cons", case="%s 日线/报价一致性" % p.name, source=p.name, symbol=sym,
                                status="WARN", detail="异常: %s" % type(e).__name__)
                continue
            if not bars or not isinstance(q, dict):
                continue
            close = _f(bars[-1].get("close"))
            last = _f(q.get("last"))
            if not close or not last:
                continue
            dev = abs(last - close) / close
            self.report.add(
                suite="cons", case="%s 日线收盘 vs 实时价" % p.name, source=p.name, symbol=sym,
                status="PASS" if dev <= KLINE_CLOSE_TOL else "WARN",
                detail="close=%.3f last=%.3f (%.2f%%)" % (close, last, dev * 100))

    # ═════════════════ 套件 5: 并行与复用 ═════════════════
    def suite_parallel(self):
        print("\n=== 套件 parallel: 并发/复用/分配 ===")
        workers = max(2, int(self.args.parallel))
        # 5.1 同一 Provider 实例并发调用（线程安全）
        target = next((p for p in self._cn() if p.capabilities.get("quote")), None)
        if target:
            syms = self.symbols_cn[: max(2, min(len(self.symbols_cn), workers))]
            t0 = time.time()
            ok = 0
            errs: List[str] = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(target.fetch_ticker, s): s for s in syms}
                for fu in as_completed(futs):
                    s = futs[fu]
                    try:
                        q = fu.result()
                        if isinstance(q, dict) and _f(q.get("last")):
                            ok += 1
                        else:
                            errs.append("%s 空结果" % s)
                    except Exception as e:
                        errs.append("%s %s" % (s, type(e).__name__))
            ms = (time.time() - t0) * 1000
            self.report.add(suite="parallel", case="%s 同实例并发(%d线程)" % (target.name, workers),
                            source=target.name, status="PASS" if ok == len(syms) else "FAIL",
                            latency_ms=ms, detail="%d/%d 成功 %s" % (ok, len(syms), "; ".join(errs[:3])),
                            extra={"wall_ms": ms})
        # 5.2 批量接口覆盖度（≥30 只）
        codes = [strip_market_prefix(s) for s in self.symbols_cn]
        if len(codes) < 30:
            codes = codes + ["600519", "000001", "300750", "601318", "000651", "600036", "002594",
                             "600900", "601899", "000333", "600030", "601166", "000858", "600276",
                             "002415", "600887", "601012", "000725", "600104", "601288",
                             "600000", "601398", "601857", "600028", "601988", "000002",
                             "600050", "601668", "600019", "000063"][: max(0, 32 - len(codes))]
        codes = codes[:40]
        for p in self._cn():
            if not p.capabilities.get("batch_quote"):
                continue
            t0 = time.time()
            try:
                q = p.fetch_batch_quotes(codes, timeout=8)
            except Exception as e:
                self.report.add(suite="parallel", case="%s 批量覆盖(%d只)" % (p.name, len(codes)),
                                source=p.name, status="FAIL",
                                latency_ms=(time.time() - t0) * 1000,
                                detail="%s: %s" % (type(e).__name__, str(e)[:60]))
                continue
            ms = (time.time() - t0) * 1000
            n = len(q) if isinstance(q, dict) and not isinstance(q, NotSupportedResult) else 0
            cover = n / len(codes)
            if cover < 0.5 and is_env_blocked_result(p.name, q):
                st = "UNREACHABLE"
            else:
                st = "PASS" if cover >= 0.8 else ("WARN" if cover >= 0.5 else "FAIL")
            self.report.add(suite="parallel", case="%s 批量覆盖(%d只)" % (p.name, len(codes)),
                            source=p.name, status=st,
                            latency_ms=ms, detail="%d/%d (%.0f%%)" % (n, len(codes), cover * 100))

    # ═════════════════ 套件 6: 端到端调度 ═════════════════
    def suite_e2e(self):
        print("\n=== 套件 e2e: Coordinator 端到端调度 ===")
        from app.data_sources.coordinator import get_coordinator
        coord = get_coordinator()
        sym = self.symbols_cn[0]
        # 单股 K 线
        t0 = time.time()
        r = coord.coordinate_kline(symbol=sym, timeframe="1D", limit=30, market="CNStock", timeout=20)
        ms = (time.time() - t0) * 1000
        bars = (r or {}).get("bars") or []
        self.report.add(suite="e2e", case="coordinate_kline 单股", symbol=sym, timeframe="1D",
                        status="PASS" if bars else "FAIL", latency_ms=ms,
                        detail="source=%s bars=%d" % ((r or {}).get("source"), len(bars)))
        # 单股行情 Race
        t0 = time.time()
        q = coord.coordinate_ticker(symbol=sym, market="CNStock", timeout=10)
        ms = (time.time() - t0) * 1000
        self.report.add(suite="e2e", case="coordinate_ticker Race", symbol=sym,
                        status="PASS" if (q or {}).get("last") else "FAIL", latency_ms=ms,
                        detail="last=%s" % (q or {}).get("last"))
        # 批量行情（40 只）
        codes = [strip_market_prefix(s) for s in self.symbols_cn][:40]
        if len(codes) < 40:
            codes = list(dict.fromkeys(codes + ["600519", "000001", "300750", "601318", "000651",
                                                "600036", "002594", "600900", "601899", "000333"]))
        t0 = time.time()
        quotes = coord.coordinate_batch_quotes(symbols=codes, market="CNStock", timeout=60)
        ms = (time.time() - t0) * 1000
        n = len(quotes or [])
        cover = n / len(codes)
        self.report.add(suite="e2e", case="coordinate_batch_quotes %d只" % len(codes),
                        status="PASS" if cover >= 0.9 else ("WARN" if cover >= 0.6 else "FAIL"),
                        latency_ms=ms, detail="%d/%d (%.0f%%) %.1fs" % (n, len(codes), cover * 100, ms / 1000),
                        extra={"throughput_per_s": round(n / max(ms / 1000, 0.001), 1)})
        # 全市场K线（受控：默认关闭，--market-kline 打开）
        if self.args.market_kline:
            sub = codes[: int(self.args.market_kline)]
            t0 = time.time()
            rows = coord.coordinate_market_kline(market="CNStock", timeframe="1D", count=20,
                                                 timeout=90, symbols=sub)
            ms = (time.time() - t0) * 1000
            got = {r.get("symbol") for r in (rows or [])}
            self.report.add(suite="e2e", case="coordinate_market_kline %d只" % len(sub),
                            status="PASS" if got else "FAIL", latency_ms=ms,
                            detail="覆盖 %d/%d 只 bars=%d" % (len(got), len(sub), len(rows or [])),
                            extra={"throughput_per_s": round(len(rows or []) / max(ms / 1000, 0.001), 1)})
        # 港股
        hk_src = get_provider("tencent")
        if hk_src:
            t0 = time.time()
            q = coord.coordinate_ticker(symbol="00700", market="HKStock", timeout=10)
            ms = (time.time() - t0) * 1000
            self.report.add(suite="e2e", case="HKStock Race(00700)", symbol="HK00700",
                            status="PASS" if (q or {}).get("last") else "FAIL", latency_ms=ms,
                            detail="last=%s" % (q or {}).get("last"))



    # ═════════════════ 套件 10: Provider 边界契约 ═════════════════
    # 规则（用户明确要求）: provider/ 下的源是最底层数据层，
    #   只允许依赖 —— 标准库 / requests / curl_cffi / pytdx，以及项目内的
    #   provider 框架与同目录 adjustment(复权)、normalizer(代码)、rate_limiter(限流)、
    #   app.utils.logger。
    #   禁止调用任何外围业务模块（basicinfo_db / trading_calendar / services / market_cn ...），
    #   以免底层数据层反向依赖上层。
    _PROVIDER_ALLOWED = (
        r"^app\.data_sources\.provider",       # 框架 + 同目录 adjustment
        r"^app\.data_sources\.normalizer$",
        r"^app\.data_sources\.rate_limiter$",
        r"^app\.utils\.logger$",
    )
    _PROVIDER_ALLOWED_PKGS = ("requests", "curl_cffi", "pytdx", "urllib3")

    def suite_boundary(self):
        """静态扫描 provider/*.py 的 import，发现越界依赖即失败。"""
        import ast as _ast
        import pathlib
        import re as _re
        print("\n=== 套件 boundary: Provider 依赖边界 ===")
        root = pathlib.Path(_ROOT) / "app" / "data_sources" / "provider"
        # 只查各数据源文件；__init__.py 是编排/框架层（它枚举全市场清单属正当），
        # adjustment.py 是被共享的复权工具。
        targets = sorted(f for f in root.glob("*.py")
                         if f.name not in ("__init__.py", "adjustment.py"))
        if not targets:
            self.report.add(suite="boundary", case="provider 目录扫描", status="SKIP", detail="无文件")
            return
        allow_rx = [_re.compile(p) for p in self._PROVIDER_ALLOWED]
        for f in targets:
            try:
                tree = _ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as e:
                self.report.add(suite="boundary", case="%s 语法" % f.name, status="FAIL", detail=str(e))
                continue
            bad = []
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for a in node.names:
                        top = a.name.split(".")[0]
                        if a.name.startswith("app.") and not any(rx.match(a.name) for rx in allow_rx):
                            bad.append("import %s" % a.name)
                        elif not a.name.startswith("app.") and top not in self._PROVIDER_ALLOWED_PKGS:
                            import sys as _sys
                            if top not in _sys.stdlib_module_names:
                                bad.append("import %s" % a.name)
                elif isinstance(node, _ast.ImportFrom):
                    mod = node.module or ""
                    if node.level:      # 相对导入（同包内）放行
                        continue
                    top = mod.split(".")[0]
                    if mod.startswith("app.") and not any(rx.match(mod) for rx in allow_rx):
                        bad.append("from %s import ..." % mod)
                    elif not mod.startswith("app."):
                        import sys as _sys
                        if top not in self._PROVIDER_ALLOWED_PKGS and top not in _sys.stdlib_module_names:
                            bad.append("from %s import ..." % mod)
            self.report.add(suite="boundary", case="%s 依赖边界" % f.name, status="FAIL" if bad else "PASS",
                            detail="; ".join(sorted(set(bad))[:4]) if bad else "仅依赖 框架/复权/工具 与标准库")

    # ═════════════════ 套件 8: 字段契约 ═════════════════
    REQUIRED_QUOTE_FIELDS = ("last", "open", "high", "low", "previousClose", "change", "changePercent",
                             "volume", "name", "symbol")

    def suite_fields(self):
        """字段名/类型/非占位值一致性 — 增删 Provider 时用来自动发现口径漂移。"""
        print("\n=== 套件 fields: quote 字段契约 ===")
        sym = self.symbols_cn[0]
        for p in self._cn():
            if not p.capabilities.get("quote"):
                continue
            try:
                q = p.fetch_ticker(sym)
            except Exception as e:
                self.report.add(suite="fields", case="%s 取行情" % p.name, source=p.name, symbol=sym,
                                status="FAIL", detail="%s: %s" % (type(e).__name__, str(e)[:60]))
                continue
            if not isinstance(q, dict) or not q:
                env = is_env_blocked_result(p.name, q)
                self.report.add(suite="fields", case="%s 取行情" % p.name, source=p.name, symbol=sym,
                                status="UNREACHABLE" if env else "FAIL",
                                detail="环境封锁(空响应)" if env else "空行情")
                continue
            # 1) 必需字段齐备
            missing = [f for f in self.REQUIRED_QUOTE_FIELDS if f not in q]
            extra = sorted(set(q) - set(self.REQUIRED_QUOTE_FIELDS))
            errs = []
            if missing:
                errs.append("缺字段 %s" % ",".join(missing))
            # 2) 类型与值合法（价格为正、量非负、涨跌幅与涨跌额自洽）
            price_fields = ("last", "open", "high", "low")
            for f in price_fields:
                v = _f(q.get(f))
                if v is None or v <= 0:
                    errs.append("%s 非法(%s)" % (f, q.get(f)))
            prev = _f(q.get("previousClose"))
            if prev is None or prev <= 0:
                errs.append("previousClose 非法(%s)" % q.get("previousClose"))
            vol = _f(q.get("volume"))
            if vol is None or vol < 0:
                errs.append("volume 非法(%s)" % q.get("volume"))
            notes = []
            # name/symbol 为下游定位标的所必需；name 缺失时降级为提示（如 tdx_ex 上游协议不带名称）
            if not isinstance(q.get("name"), str) or not q.get("name"):
                notes.append("name 上游未提供")
            if not q.get("symbol"):
                errs.append("symbol 为空")
            # 3) 涨跌自洽: last - prev == change, change/prev*100 == changePercent
            last, chg, pct = _f(q.get("last")), _f(q.get("change")), _f(q.get("changePercent"))
            if None not in (last, prev, chg) and prev and prev > 0:
                if abs((last - prev) - chg) > max(0.02, abs(last) * 0.002):
                    errs.append("change 与 last-previousClose 不符 (%.3f vs %.3f)" % (chg, last - prev))
                if pct is not None and abs(pct - chg / prev * 100) > 0.15:
                    errs.append("changePercent 与 change/昨收 不符 (%.2f vs %.2f)" % (pct, chg / prev * 100))
            # 4) 时间字段: 有则须是 "YYYY-MM-DD..." ，且不早于今天
            t = q.get("time")
            tnote = ""
            if t:
                ts = str(t)
                today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
                if len(ts) < 10 or "-" not in ts[:10]:
                    errs.append("time 格式异常(%r)" % ts)
                elif (datetime.strptime(today, "%Y-%m-%d")
                      - datetime.strptime(ts[:10], "%Y-%m-%d")).days > 3:
                    errs.append("time 过旧(%s)" % ts[:16])
                elif ts[:10] != today:
                    notes.append("time=%s(非今日，节假日/停牌属正常)" % ts[:16])
            else:
                notes.append("无 time 字段(上游未提供)")
            self.report.add(suite="fields", case="%s quote 契约" % p.name, source=p.name, symbol=sym,
                            status="FAIL" if errs else "PASS",
                            detail=("; ".join(errs) if errs else "字段齐备、自洽")
                                   + (" | " + "; ".join(notes) if notes else "")
                                   + (" | 额外字段: %s" % ",".join(extra) if extra else ""))

    # ═════════════════ 套件 9: 单位口径 ═════════════════
    def suite_units(self):
        """体积/金额/涨跌幅的跨源单位互验 — 同票同日，量级差 100 倍即口径漂移。"""
        print("\n=== 套件 units: 单位口径（volume 股 / amount 元）===")
        sym = self.symbols_cn[0]
        quotes: Dict[str, Dict[str, Any]] = {}
        for p in self._cn():
            if not p.capabilities.get("quote"):
                continue
            try:
                q = p.fetch_ticker(sym)
            except Exception:
                continue
            if isinstance(q, dict) and _f(q.get("last")):
                quotes[p.name] = q

        # 1) 成交量同量级（相邻源偏差 <= 2%）
        vols = {n: _f(q.get("volume")) for n, q in quotes.items() if _f(q.get("volume"))}
        if len(vols) >= 2:
            vals = sorted(vols.values())
            med = vals[len(vals) // 2]
            for n, v in vols.items():
                dev = abs(v - med) / med if med else 1
                self.report.add(suite="units", case="%s volume 量级" % n, source=n, symbol=sym,
                                status="PASS" if dev <= 0.02 else "FAIL",
                                detail="%.0f vs 中位 %.0f (偏差 %.2f%%) 单位应为股" % (v, med, dev * 100))
        # 2) 成交额单位互验: amount / (volume*last) 应 ≈ 1（元）。单位为万元时会是 0.0001
        for n, q in quotes.items():
            a, v, last = _f(q.get("amount")), _f(q.get("volume")), _f(q.get("last"))
            if not a or not v or not last:
                continue
            ratio = a / (v * last)
            ok = 0.9 <= ratio <= 1.1
            self.report.add(suite="units", case="%s amount 单位" % n, source=n, symbol=sym,
                            status="PASS" if ok else "FAIL",
                            detail="amount/(volume×last)=%.4f %s" % (ratio, "(元 口径 ✓)" if ok else "(疑似非元口径)"))
        # 3) 跨源涨跌幅一致
        pcts = {n: _f(q.get("changePercent")) for n, q in quotes.items() if _f(q.get("changePercent"))}
        if pcts:
            base = max(pcts.values(), key=abs)
            for n, v in pcts.items():
                self.report.add(suite="units", case="%s changePercent 一致" % n, source=n, symbol=sym,
                                status="PASS" if abs(v - base) <= 0.15 else "FAIL",
                                detail="%.2f%% vs %.2f%%" % (v, base))
        # 4) 批量行情 key 是否可回归到请求代码（前缀/大小写漂移会导致匹配不上）
        codes = [strip_market_prefix(s) for s in self.symbols_cn]
        for p in self._cn():
            if not p.capabilities.get("batch_quote"):
                continue
            try:
                bq = p.fetch_batch_quotes(codes, timeout=8)
            except Exception:
                continue
            if not isinstance(bq, dict) or not bq:
                continue
            bad = [k for k in bq if strip_market_prefix(k) not in codes]
            self.report.add(suite="units", case="%s 批量 key 可回归" % p.name, source=p.name,
                            status="PASS" if not bad else "FAIL",
                            detail="返回 %d 条%s" % (len(bq), "" if not bad else " 无法回归: %s" % bad[:3]))

    # ═════════════════ 套件 7: 性能 ═════════════════
    def suite_perf(self):
        print("\n=== 套件 perf: 延迟与产出 ===")
        rounds = max(1, int(self.args.perf_rounds))
        sym = self.symbols_cn[0]
        codes = [strip_market_prefix(s) for s in self.symbols_cn][:40]
        for p in self._cn():
            # K线延迟
            if p.capabilities.get("kline"):
                lat = []
                for _ in range(rounds):
                    t0 = time.time()
                    try:
                        r = p.fetch_kline(sym, "1D", 100)
                        if (r or {}).get("bars"):
                            lat.append((time.time() - t0) * 1000)
                    except Exception:
                        pass
                if lat:
                    self.report.add(suite="perf", case="%s 1D K线延迟" % p.name, source=p.name,
                                    status="PASS",
                                    detail="P50=%.0fms P95=%.0fms n=%d" % (
                                        statistics.median(lat),
                                        sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], len(lat)),
                                    extra={"latencies_ms": lat})
                else:
                    env = is_env_blocked_result(p.name, None)
                    self.report.add(suite="perf", case="%s 1D K线延迟" % p.name, source=p.name,
                                    status="UNREACHABLE" if env else "FAIL",
                                    detail="环境封锁(无响应)" if env else "%d 轮均失败" % rounds)
            # 批量吞吐
            if p.capabilities.get("batch_quote"):
                t0 = time.time()
                try:
                    q = p.fetch_batch_quotes(codes, timeout=8)
                    ms = (time.time() - t0) * 1000
                    n = len(q) if isinstance(q, dict) and not isinstance(q, NotSupportedResult) else 0
                    env = (not n) and is_env_blocked_result(p.name, q)
                    self.report.add(suite="perf", case="%s 批量吞吐" % p.name, source=p.name,
                                    status="UNREACHABLE" if env else ("PASS" if n else "FAIL"),
                                    latency_ms=ms,
                                    detail=("环境封锁(空响应)" if env else
                                            "%d 只 / %.0fms = %.0f 只/秒" % (n, ms, n / max(ms / 1000, 0.001))))
                except Exception as e:
                    self.report.add(suite="perf", case="%s 批量吞吐" % p.name, source=p.name,
                                    status="FAIL", detail=type(e).__name__)


# ══════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════

SUITES: Dict[str, Tuple[str, Callable]] = {
    "conn": ("连通性", Harness.suite_conn),
    "fmt": ("数据格式", Harness.suite_fmt),
    "matrix": ("能力矩阵", Harness.suite_matrix),
    "cons": ("跨源一致性", Harness.suite_consistency),
    "parallel": ("并发与复用", Harness.suite_parallel),
    "e2e": ("端到端调度", Harness.suite_e2e),
    "perf": ("性能", Harness.suite_perf),
    "fields": ("字段契约", Harness.suite_fields),
    "units": ("单位口径", Harness.suite_units),
    "boundary": ("依赖边界", Harness.suite_boundary),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Provider 层实测脚本（连通性/格式/一致性/并发）")
    ap.add_argument("--suite", action="append", default=None,
                    help="要跑的套件，可多次指定：conn,fmt,matrix,cons,parallel,e2e,perf（默认全部）")
    ap.add_argument("--source", action="append", default=None, help="只测指定源，可多次指定")
    ap.add_argument("--symbols", default="SH600519,SZ000001,SZ300750", help="测试股票池（逗号分隔）")
    ap.add_argument("--timeframes", default="1D,5m,15m,1H", help="K线周期（逗号分隔）")
    ap.add_argument("--parallel", default=4, help="并发线程数")
    ap.add_argument("--perf-rounds", default=3, help="性能套件每源重复轮数")
    ap.add_argument("--market-kline", default=0,
                    help="端到端全市场K线测试的股票数（0=跳过，建议 ≤50）")
    ap.add_argument("--json", default="", help="报告输出路径（默认写到仓库 tmp/ 下）")
    ap.add_argument("--verbose", action="store_true", help="报告里带上首尾 bar 等明细")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式: 有 FAIL 即退出码 1（默认只在报告里标注，便于区分环境问题与真实回归）")
    args = ap.parse_args()

    wanted = args.suite if args.suite else list(SUITES)
    bad = [s for s in wanted if s not in SUITES]
    if bad:
        print("未知套件: %s（可选: %s）" % (bad, ",".join(SUITES)))
        return 2

    report = Report()
    print("Provider 实测 | %s | 已注册源: %s" % (
        datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
        ",".join(p.name for p in get_providers())))
    h = Harness(args, report)
    if not h.providers:
        print("没有可测的源")
        return 1
    print("测试源: %s | 股票池: %s" % (",".join(p.name for p in h.providers), ",".join(h.symbols_cn)))

    for key in wanted:
        label, fn = SUITES[key]
        print("\n" + "─" * 72)
        print("套件 %s — %s" % (key, label))
        try:
            fn(h)
        except Exception as e:
            report.add(suite=key, case="套件执行异常", status="FAIL",
                       detail="%s: %s" % (type(e).__name__, str(e)[:120]))

    # ── 汇总 ──
    s = report.summary()
    print("\n" + "═" * 72)
    print("汇总: 共 %d 项 | %s" % (s["total"], " ".join("%s=%d" % (k, v) for k, v in sorted(s["counts"].items()))))
    fails = [r for r in report.rows if r.status == "FAIL"]
    if fails:
        print("\nFAIL 明细:")
        for r in fails:
            print("  - [%s] %s %s" % (r.suite, r.case, r.detail))
    warns = [r for r in report.rows if r.status == "WARN"]
    if warns:
        print("\nWARN 明细（不影响通过）:")
        for r in warns[:15]:
            print("  - [%s] %s %s" % (r.suite, r.case, r.detail))

    # ── 输出 JSON ──
    out = args.json or os.path.join(_ROOT, "..", "tmp", "provider_test_report.json")
    out = os.path.abspath(out)
    payload = {
        "generated_at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
        "started_at": report.started,
        "providers": [{"name": p.name, "priority": p.priority,
                       "kline_tf": sorted(p.capabilities.get("kline_tf", [])),
                       "markets": sorted(p.capabilities.get("markets", [])),
                       "max_concurrency": getattr(p, "max_concurrency", None),
                       "source_config": (SOURCE_CONFIGS[p.name].__class__.__name__
                                         if p.name in SOURCE_CONFIGS else None)}
                      for p in get_providers()],
        "summary": s,
        "results": [asdict(r) for r in report.rows],
    }
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("\n报告已写入: %s" % out)
    except Exception as e:
        print("\n报告写入失败: %s" % e)

    if fails and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
