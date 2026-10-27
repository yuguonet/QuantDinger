# -*- coding: utf-8 -*-
"""
同花顺(10jqka)数据源 Provider

API来源 & 最新信息:
  - 浏览器F12抓包 https://stockpage.10jqka.com/ 观察请求
  - K线（日/周）: d.10jqka.com.cn/v2/line/hs_{code}/{period}/last{count}.js
    period: 01=日线, 10=周线, 20=月线
  - 分时（当天1分钟tick）: d.10jqka.com.cn/v2/time/hs_{code}/{period}.js
    period参数被忽略，始终返回当天1分钟tick数据
    格式: "HHMM,price,cumulative_amount,avg_price,volume;..."
  - 行情: 同K线接口取最后一条 /01/last1.js
  - K线返回JSONP: quotebridge_v2_line_hs_xxx({...})
  - 分时返回JSONP: quotebridge_v2_time_hs_xxx({..,"data":"HHMM,price,...;..."})

时间约定（重要）:
  - /v2/time/ tick时间即为该分钟bar的结束时间（如14:58的tick = 14:57~14:58这根bar）
  - 1m: 直接返回tick时间（结束时间）
  - 聚合为5m/15m/30m/1H时，按时间分组（(m+step-1)//step），bar时间 = 最后一根tick时间（结束时间）
  - 时间格式统一为 "YYYY-MM-DD HH:MM:SS"（tick的HHMM补:00秒）

支持的功能:
  - K线 分钟级: ❌ 已屏蔽（只有当天数据，无法回测；见文末"屏蔽记录"）
  - K线 日/周: ✅ 1D/1W（/v2/line/ 接口，历史数据；本源主力能力，实测最快 52ms）
  - fetch_ticker: ✅ 单只实时行情（取K线最后两条：末条为最新价，倒数第二条为昨收，
                       因此 previousClose/change/changePercent 是真实值而非占位 0）
  - fetch_batch_quotes: ❌ 不支持（返回NotSupportedResult）


单位注意（重要）:
  - /v2/line/ K线: volume(parts[5])直接是"股"，价格"元"
  - /v2/time/ 分时: price=现价, cum_amount=累计额, avg_price=均价, volume=成交量(股)
  - fetch_ticker: 无change/changePercent字段（从K线推的，只能返回0）
  - 复权: 不复权数据通过 除权除息数据(adjustment模块)转前复权

溢出修正:
  - /v2/time/ 接口在盘后(15:01~15:30)仍返回收盘价平移+极小成交量的无效tick
  - 午休时段(12:01~13:00)也会返回溢出tick（如13:00与13:01累计额重复）
  - 在 _fetch_ths_time_data 解析时统一过滤: "1201" <= hhmm <= "1300" 或 hhmm > "1500"
  - 聚合函数 _aggregate_tick_bars 保留二次过滤作为安全兜底

屏蔽记录（2026-10-16）:
  - 分钟线（1m/5m/15m/30m/1H）整体屏蔽: 上游 /v2/time/ 只返回当天 tick，
    聚合后的分钟线没有历史 —— 回测/复现会缺数据，属"看起来能用、实际不可用"的陷阱。
  - 处置: capabilities.kline_tf 收敛为 {"1D","1W"}；fetch_kline 对分钟线显式返回
    NotSupportedResult（便于调度层换源并在日志里看到原因）；聚合链路整块注释停用。
  - 保留: 日线/周线（/v2/line/），实测为本机最快的日线源（52ms/300根）。
  - 恢复条件: 若上游日后提供历史分钟数据，取消本屏蔽并恢复 helper 块即可。
"""
from __future__ import annotations
import json, re, threading
from datetime import datetime, timezone, timedelta

_TZ_CN = timezone(timedelta(hours=8))
from typing import Any, Dict, List, Optional
import requests
from app.data_sources.normalizer import to_raw_digits, detect_market
from app.data_sources.rate_limiter import get_request_headers, RateLimiter, get_shared_session
from app.data_sources.provider import register, NotSupportedResult
from app.utils.logger import get_logger
logger = get_logger(__name__)

# 行情限流器 — 仅 fetch_ticker 使用，K线限流已移至 Coordinator
_ths_quote_limiter = RateLimiter(min_interval=0.6, jitter_min=0.2, jitter_max=1.0)

_THS_MARKET = {"SH": 1, "SZ": 0, "BJ": 0}
# 日线/周线走 /v2/line/ 接口（分钟线走 /v2/time/ 专用逻辑，不在此映射）
_THS_PERIOD = {"1D": 1, "1W": 10}

# ── 已停用: 分钟线聚合链路（_THS_MIN_TFS 及 _fetch_ths_time_data/_aggregate_tick_bars 两个 helper）──
# 停用原因见文件头"屏蔽记录"；整块注释保留，日后需要历史分钟线时先确认上游是否已提供历史。
# _THS_MIN_TFS = {"1m", "5m", "15m", "30m", "1H"}


def _to_ths_params(code):
    digits = to_raw_digits(code)
    upper = code.strip().upper()
    # 提取交易所前缀 SH/SZ/BJ
    prefix = upper[:2] if upper[:2] in ("SH", "SZ", "BJ") else ""
    # 无前缀时尝试自动补全
    if not prefix:
        from app.data_sources.normalizer import add_market_prefix
        prefixed = add_market_prefix(code, "CNStock")
        if prefixed != code:
            prefix = prefixed[:2].upper()
            digits = to_raw_digits(prefixed)
    if not prefix or not digits: return None
    mkt = _THS_MARKET.get(prefix)
    if mkt is None: return None
    return (mkt, digits)


# ═══════════════ 分时数据 (/v2/time/) ════════════════

_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60}


def _fetch_ths_time_data(digits: str, timeout: int = 10) -> List[Dict[str, Any]]:
    """从 /v2/time/ 获取当天分时tick数据，返回1分钟bar列表。

    接口: d.10jqka.com.cn/v2/time/hs_{code}/{period}.js
    period参数被忽略，始终返回当天1分钟tick。
    数据格式: "HHMM,price,cumulative_amount,avg_price,volume;..."

    时间说明: tick的HHMM即为该分钟bar的结束时间（如"0931" = 09:30~09:31这根bar）。
    输出格式: "YYYY-MM-DD HH:MM:SS"（HHMM补:00秒）。

    溢出修正: 解析时过滤午休溢出(12:01~13:00)和盘后溢出(>15:00)的无效tick。

    注意: 仅返回当天数据，非交易时段为空。
    """
    url = f"https://d.10jqka.com.cn/v2/time/hs_{digits}/15.js"
    try:
        resp = get_shared_session().get(
            url,
            headers=get_request_headers(referer="https://stockpage.10jqka.com/"),
            timeout=timeout,
        )
        resp.encoding = "utf-8"
        text = resp.text or ""
    except Exception as e:
        logger.warning("[同花顺分时] 请求失败 %s: %s", digits, e)
        return []

    # 提取 "data":"..."
    m = re.search(r'"data"\s*:\s*"([^"]*)"', text)
    if not m or not m.group(1):
        return []

    today = datetime.now(_TZ_CN).strftime("%Y-%m-%d")
    bars: List[Dict[str, Any]] = []

    for seg in m.group(1).split(";"):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split(",")
        if len(parts) < 5:
            continue
        try:
            hhmm = parts[0].strip()           # "0930", "1015" ...
            # 溢出修正: 过滤午休(12:01~13:00)和盘后(>15:00)的无效tick
            # 10jqka盘后仍返回收盘价平移+极小成交量的数据，午休边界(13:00)与13:01累计额重复
            if "1201" <= hhmm <= "1300" or hhmm > "1500":
                continue
            price = float(parts[1])            # 现价
            # parts[2] = 累计成交额（不是本bar的）
            # parts[3] = 均价
            vol = float(parts[4])              # 本bar成交量（股）
            if price <= 0 or vol < 0:
                continue
            bars.append({
                "time": f"{today} {hhmm[:2]}:{hhmm[2:]}:00",
                "open": price, "high": price, "low": price, "close": price,
                "volume": vol,
            })
        except (ValueError, TypeError, IndexError):
            continue

    return bars


def _aggregate_tick_bars(tick_bars: List[Dict[str, Any]], timeframe: str) -> List[Dict[str, Any]]:
    """将1分钟tick数据聚合为指定周期K线（按时间分组）。

    tick_bars: _fetch_ths_time_data 返回的列表（open=high=low=close=price）。
    支持: 1m(直返), 5m, 15m, 30m, 1H。

    算法: 过滤午休tick(12:01~13:00)后，按tick时间分组（(m+step-1)//step对齐到结束时间边界），
    bar时间 = 该组最后一根tick时间（结束时间）。

    注: 午休溢出已在 _fetch_ths_time_data 源端过滤，此处为双重保险。
    """
    if not tick_bars:
        return []

    step = _TF_MINUTES.get(timeframe, 1)
    if step <= 1:
        return tick_bars

    # ── 交易时段内对齐分桶 ──
    # 教训(2026-10 实测): 用整点时钟分桶 ((m+step-1)//step) 会把 A 股时段切碎——
    # 1H 得到 10:00/11:00/11:30/14:00/15:00（11:00→11:30 只隔 30 分钟，14:00 那根是半天拼接）。
    # 正确做法: 以上午开盘 09:30 为原点，把「上午 09:30-11:30 + 下午 13:00-15:00」
    # 映射成连续分钟轴（减去 90 分钟午休），再按 step 分桶 = 交易所 K 线口径。
    _AM_OPEN, _AM_CLOSE = 9 * 60 + 30, 11 * 60 + 30      # 570 / 690
    _PM_OPEN, _PM_CLOSE = 13 * 60, 15 * 60              # 780 / 900
    _LUNCH = _PM_OPEN - _AM_CLOSE                       # 90 分钟

    def _session_minute(m: int):
        """时刻(自 0 点分钟数) → 时段内连续分钟；非交易时段返回 None。"""
        if _AM_OPEN <= m <= _AM_CLOSE:
            return m - _AM_OPEN
        if _PM_OPEN <= m <= _PM_CLOSE:
            return m - _AM_OPEN - _LUNCH
        return None

    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for bar in tick_bars:
        try:
            dt = datetime.strptime(bar["time"][:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, TypeError):
            continue
        sm = _session_minute(dt.hour * 60 + dt.minute)
        if sm is None:
            continue                                    # 午休/盘后溢出 tick
        # tick 是结束时间: 09:31-10:00 归入第 1 桶（idx 1..step），09:30 的开盘 tick 归入第 0 桶
        idx = sm // step if sm % step == 0 else sm // step + 1
        groups.setdefault((bar["time"][:10], idx), []).append(bar)

    result: List[Dict[str, Any]] = []
    for key in sorted(groups):
        chunk = groups[key]
        # bar时间 = 最后一根tick时间（结束时间）
        bar_time = chunk[-1]["time"]
        result.append({
            "time": bar_time,
            "open": chunk[0]["open"],
            "high": max(b["high"] for b in chunk),
            "low": min(b["low"] for b in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(b["volume"] for b in chunk),
        })
    return result


# ═══════════════ 前复权（共享模块）═══════════════
# adjustment 模块的对外函数是 unadj_to_qfq / unadj_to_hfq（无 apply_fwd_adjust）——
# 原导入名不存在，会导致本 Provider 在 autodiscover 时 ImportError 而整个不注册。
from app.data_sources.provider.adjustment import unadj_to_qfq as _apply_fwd_adjust


# [并发常量] 最大并发线程数 — Coordinator.allocate_threads() 据此分配 worker。
# ⚠️ 请勿删除或随意修改: 此常量直接影响调度层线程分配，改错会导致请求过载或资源浪费。
# 选值依据: 同花顺HTTP接口，限流 min_interval=1.0s。
# 同步位置: source_config.py max_workers 需与此值保持一致。
MAX_CONCURRENCY = 4

@register(priority=20)  # 与类属性 priority=20 保持一致（装饰器值仅在类未声明时生效）
class ThsDataSource:
    """同花顺(10jqka)数据源 — HTTP接口，无需额外依赖。"""
    name = "10jqka"; priority = 20
    max_concurrency = MAX_CONCURRENCY
    min_interval = 1.0
    jitter_min = 0.5
    jitter_max = 1.5
    # 分钟线已屏蔽（见文件头"屏蔽记录"）: 只有当天数据，无法回测；日线保留。
    capabilities = {"kline": True, "kline_priority": 20, "kline_tf": {"1D", "1W"},
                    "kline_batch": True, "quote": True, "quote_priority": 25,
                    "batch_quote": False, "batch_quote_priority": 30, "hk": False, "markets": {"CNStock"}}

    def fetch_kline(self, code, timeframe="1D", count=300,
                    timeout=10, start_date="", end_date="") -> Dict[str, Any]:
        """注意: 签名必须与 BaseDataSource 协议一致（第4位是 timeout，不是 adj）。
        Coordinator 按位置传参 (symbol, timeframe, limit)，若保留 adj 参数会把
        默认的 "qfq" 当成 timeout 传给 requests —— 复权由辅助函数在返回前一律应用。"""
        if start_date:
            from app.data_sources.provider import calc_kline_count; count = calc_kline_count(timeframe, start_date, end_date)
        params = _to_ths_params(code)
        if not params: return {}
        mkt, digits = params

        # ── 分钟线已屏蔽（2026-10-16）──
        # 原因: /v2/time/ 分时接口只返回当天 tick，聚合出的分钟线无历史，
        # 无法用于回测/复现；日线（/v2/line/）保留为本源主能力。
        # 显式返回 NotSupportedResult，让 Coordinator 换源，而不是返回半截数据。
        if timeframe in ("1m", "5m", "15m", "30m", "1H"):
            return NotSupportedResult(self.name, "fetch_kline",
                                      "分钟线已屏蔽: 上游仅当天数据，不支持历史分钟K线")

        # ── 日/周/月: /v2/line/ K线接口 ──
        period = _THS_PERIOD.get(timeframe)
        if period is None: return {}
        p_str = str(period).zfill(2) if period < 10 else str(period)
        # 有日期范围时用大窗口取全量再过滤，否则按 count 取
        fetch_limit = 5000 if (start_date or end_date) else min(int(count), 800)
        url = "https://d.10jqka.com.cn/v2/line/hs_{}/{}/last{}.js".format(digits, p_str, fetch_limit)
        try:
            resp = get_shared_session().get(url, headers=get_request_headers(referer="https://stockpage.10jqka.com/"), timeout=timeout)
            resp.encoding = "utf-8"; text = resp.text or ""
        except Exception as e: logger.warning("[同花顺K线] 请求失败 %s: %s", code, e); return {}
        m = re.search(r'"data"\s*:\s*"([^"]+)"', text)
        if not m: m = re.search(r'"([^"]*\d{8}[^"]*)"', text)
        if not m: return {}
        raw = m.group(1); out = []
        for seg in raw.split(";"):
            seg = seg.strip()
            if not seg: continue
            parts = seg.split(";")
            if len(parts) < 5: parts = seg.split(",")
            if len(parts) < 5: continue
            try:
                dt_str = parts[0].strip()
                if len(dt_str) == 8 and dt_str.isdigit(): ts = f"{dt_str[:4]}-{dt_str[4:6]}-{dt_str[6:8]}"
                elif len(dt_str) >= 10: ts = dt_str[:10] if " " not in dt_str else (dt_str[:16] + ":00" if len(dt_str) >= 16 else dt_str[:10])
                else: continue
                o, h, l, c = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                v = float(parts[5]) if len(parts) > 5 else 0
                if o > 10000 and c < 100: o, h, l, c = o/100, h/100, l/100, c/100
                if o == 0 and c == 0: continue
                if c <= 0 or o <= 0: continue  # 跳过极端负数/零价
                out.append({"time": ts, "open": round(o, 4), "high": round(h, 4), "low": round(l, 4), "close": round(c, 4), "volume": round(v, 2)})
            except (ValueError, TypeError, IndexError): continue
        out.sort(key=lambda x: x["time"])
        # 日期过滤
        if start_date or end_date:
            from app.data_sources.provider import filter_bars_by_date
            out = filter_bars_by_date(out, start_date, end_date)
        elif len(out) > count:
            out = out[-count:]
        out = _apply_fwd_adjust(out, code)
        return {"bars": out, "count": len(out)} if out else {}

    def fetch_ticker(self, code, timeout=8):
        params = _to_ths_params(code)
        if not params: return None
        mkt, digits = params
        _ths_quote_limiter.wait()
        # num=2: 取最近两根日线，才能算出真实 previousClose / change / changePercent。
        # 原来用 last1 —— 只有一条记录，这些字段只能填 0，下游会把 0 当"平盘"。
        try:
            resp = get_shared_session().get("https://d.10jqka.com.cn/v2/line/hs_{}/01/last2.js".format(digits),
                headers=get_request_headers(referer="https://stockpage.10jqka.com/"), timeout=timeout)
            resp.encoding = "utf-8"; text = resp.text or ""
        except Exception as e: logger.warning("[同花顺行情] 请求失败 %s: %s", code, e); return None
        # 解析 JSONP: quotebridge_v2_line_hs_xxx_01_last1({...})
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m: return None
        try: data = json.loads(m.group())
        except (json.JSONDecodeError, ValueError): return None
        # v2/line 返回 data 字段: "date,open,high,low,close,volume,amount,..."
        raw = str(data.get("data", ""))
        if not raw: return None
        last_seg = raw.rstrip(";").split(";")[-1]
        parts = last_seg.split(",")
        if len(parts) < 5: return None
        try:
            last = float(parts[4])  # close
            open_p = float(parts[1])
            high = float(parts[2])
            low = float(parts[3])
            vol = float(parts[5]) if len(parts) > 5 else 0
        except (ValueError, IndexError): return None
        if last <= 0: return None
        # 昨收: 取上一根的收盘（与 last 同源、同复权口径，避免跨源昨收不一致）
        prev = 0.0
        segs = raw.rstrip(";").split(";")
        if len(segs) >= 2:
            prev_parts = segs[-2].split(",")
            if len(prev_parts) >= 5:
                try:
                    prev = float(prev_parts[4])
                except (ValueError, TypeError):
                    prev = 0.0
        chg = round(last - prev, 4) if prev > 0 else 0.0
        from app.data_sources.provider import normalize_quote
        result = {"last": last, "change": chg,
                "changePercent": round(chg / prev * 100, 2) if prev > 0 else 0.0,
                "high": high, "low": low, "open": open_p, "previousClose": prev,
                "volume": vol, "time": str(parts[0]) if len(parts) > 0 else "",
                "name": data.get("name", ""), "symbol": f"{digits}"}
        normalize_quote(result)
        return result

    def fetch_batch_quotes(self, codes, timeout=10):
        return NotSupportedResult(self.name, "fetch_batch_quotes")
