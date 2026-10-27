#!/usr/bin/env python3
"""
sync_index_fflow.py — A股主要指数大盘资金流同步 (kline_index_fflow)

采集 EM 分钟级大盘资金流 (主力/超大单/大单/中单/小单 净流入, 元),
写入独立表 kline_index_fflow。与 kline_index_5m 同为指数维度独立表, 不混表。

数据源: EM fflow/kline (klt=1, 1分钟, 当日240根)
  - host 优先级: push2(实时, 秒级延迟) → push2delay(延迟镜像, ~15分钟滞后)
    2026-09-12 实测: push2/push2his 对 urllib/requests TLS 指纹 (JA3) 封锁
    (RemoteDisconnected 无状态码), curl_cffi edge101 指纹可通过
  - 2026-10-18 重修: 原依赖的 app.data_sources.provider.eastmoney._em_get 随该
    模块于 10-16 被移除（部署网络 IP 封锁），import 失败导致 9 指数全断采。
    实测本机 curl_cffi edge101 直连 push2 HTTP 200 + 240 根，故在脚本内重建
    同款指纹封装 _em_get（不再依赖 provider 层）；push2delay 实测 RST 不通，
    保留仅作部署网络下的回退位。
  - 仅返回当日 240 根 → 每日增量即全量, 断采不可回补 (只能向前攒)
  - 日级历史: --backfill 走 push2his fflow/daykline (lmt=0 多年, akshare 同款参数),
    写为每日 15:00 单行 (当日累计=日级值); **需在用户本机终端跑** — push2his 对
    沙箱/数据中心出口 IP 封锁 (09-12 实测: 直连 RemoteDisconnected, 代理 ProxyError,
    本机 ISP 正常, akshare 同端点野外可用); delay host 的 daykline 只有 1 天 (服务端限制)
    已有分钟数据(<240根才算日级日) 的日期自动跳过, 不覆盖分钟源

**字段语义 (2026-09-12 实证, tmp/_fflow_depth.out)**:
  klines 每行: 时间,主力,小单,中单,大单,超大单,占比x5,收盘,涨跌,...
  - f51=时刻(09:31 bar起始口径, 注意与 kline_index_5m 的 bar结束时刻差 1 根)
  - f52~f56 = 主力/小单/中单/大单/超大单 净流入 (元, **当日累计值**,
    15:00 累计 = fflow/daykline 日值, 已实证自洽)
  - 自洽校验: 主力=大单+超大单; 四类之和=0 (互为对手盘)
  - 特征工程用时需差分得每分钟增量; 5m 增量 = 5 根 1m 差分聚合
  - 主力净流入 = 超大单+大单 (EM 口径, 沪深两市大单阈值约 20 万/100 万)

  2026-10-20 方案2 (日级回补新源): 东财 push2/push2his 对部署 IP 持续封锁,
  日级回补默认改走**新浪聚合** (--source sina): 新浪个股四档资金流
  (MoneyFlow.ssl_qsfx_lscjfb) 按指数成分求和 → 同构日级行。口径映射/限制
  详见 app/market_cn/sina_moneyflow.py 文件头 (新浪四档阈值与东财一致,
  r0~r3=特大/大/中/小, 主力=特大+大; 仅日级, 2010-03-01 起, 当前成分近似)。
  建议回补后用 --verify 对已有分钟数据的日期做一次口径比对。

用法:
  python scripts/sync_index_fflow.py                # 全量 9 指数 (分钟, EM fflow)
  python scripts/sync_index_fflow.py --indices 000001.SH,399001.SZ
  python scripts/sync_index_fflow.py --dry-run
  # 日级回补 (方案2 新浪聚合, 默认, 全网络可跑):
  python scripts/sync_index_fflow.py --backfill
  python scripts/sync_index_fflow.py --backfill --indices 000016.SH --dry-run -v
  python scripts/sync_index_fflow.py --backfill --force   # 忽略断点缓存全量重抓
  # 日级回补 (东财官方口径, 需本机 ISP 网络; 部署网络封锁期不可用):
  python scripts/sync_index_fflow.py --backfill --source em
  # 口径校验: 新浪聚合日级 vs 已有东财分钟数据日终值 (需 DB):
  python scripts/sync_index_fflow.py --verify

调度: 2026-10-27 起 scheduler._sync_index_fflow 直调
      app/data_sources/cn_stock_putdb.sync_index_fflow（与本脚本 sync() 同源）；
      本脚本保留 CLI 全部能力（分钟 sync / 日级 --backfill / --verify）。
"""

import sys
import argparse
from pathlib import Path
from typing import Dict, List, Optional

_root = Path(__file__).resolve().parent.parent  # scripts/ → QuantDinger/
sys.path.insert(0, str(_root / "backend_api_python"))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / "backend_api_python" / ".env")
    load_dotenv(_root / ".env")
except ImportError:
    pass

# ============================================================
# 指数配置 — 与 sync_index_minute.INDICES 同键
# ============================================================

INDICES: Dict[str, str] = {
    "000001.SH": "上证指数",
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "399005.SZ": "中小板指",
    "000688.SH": "科创50",
}

TABLE = "kline_index_fflow"
# f52~f56 净流入字段 → 库列名 (累计值, 元)
NET_COLS = ["main_net", "small_net", "mid_net", "big_net", "super_net"]
# 日级历史端点 (push2his, akshare 同款参数; 只在本机网络可达)
DAYKLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
# 分钟根数阈值: 当日已有 ≥ 此数视为分钟数据完整, 日级回补跳过该日
MINUTE_COMPLETE = 240


def _to_secid(symbol: str) -> str:
    """'000001.SH' → '1.000001'; '399001.SZ' → '0.399001' (EM secid 规则)"""
    code, suffix = symbol.split(".")
    return ("1" if suffix == "SH" else "0") + "." + code


def _em_get(url: str, timeout: float = 15, retries: int = 3) -> Optional[str]:
    """东财 GET — curl_cffi edge101 TLS 指纹封装，返回响应文本，失败 None。

    2026-10-18: 原 app.data_sources.provider.eastmoney._em_get 随该模块被移除，
    本脚本 import 失败导致指数资金流全断。实测本机 curl_cffi edge101/chrome120
    直连 push2 HTTP 200 + 240 根，requests 裸连 RemoteDisconnected（JA3 封锁）。
    故在脚本内重建同款封装，不再依赖 provider 层。
    """
    import time as _t
    try:
        from curl_cffi import requests as _cr
    except ImportError:
        print("    [fflow] curl_cffi 未安装 (pip install curl_cffi)")
        return None
    # 东财 CDN 对 urllib/requests TLS 指纹 (JA3) 封锁 (RemoteDisconnected 无状态码),
    # curl_cffi edge101 指纹可通过。 Referer 必须带 (quote.eastmoney.com);
    # UA 不覆盖 — edge101 自带 UA 与指纹一致 (2026-10-18 00:50 实证配置)。
    # 注意: 东财另有 IP 级风控 (09-12/10-16/10-18 均实测到全指纹 RST 的封锁期),
    # 封锁期内任何配置都通, 需等待恢复 (通常小时级)。
    headers = {"Referer": "https://quote.eastmoney.com/"}
    last_err = None
    for i in range(retries):
        try:
            s = _cr.Session(impersonate="edge101")
            r = s.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if i < retries - 1:
                _t.sleep(1.5 * (i + 1))
    print(f"    [fflow] _em_get 重试 {retries} 次失败: {repr(last_err)[:100]}")
    return None


def fetch_fflow(symbol: str) -> Optional[list]:
    """EM 1分钟资金流。委托 provider_index.fflow_em（同源同口径，2026-10-27 归位）。

    返回 [{time, main_net, small_net, mid_net, big_net, super_net}]（净额当日累计、
    元、bar 起始时刻），失败 None。
    """
    from app.data_sources.provider_index.fflow_em import fetch_index_fflow
    out = fetch_index_fflow(symbol)
    rows = out.get("rows") or []
    return rows or None


def write_to_db(pool, symbol: str, rows: list, dry_run: bool = False) -> int:
    """落库委托 app/data_sources/cn_stock_putdb.write_index_fflow（同 UPSERT，2026-10-27 归位）。"""
    from app.data_sources.cn_stock_putdb import write_index_fflow
    return write_index_fflow(pool, symbol, rows, dry_run=dry_run)


def get_pool():
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    return mgr._get_pool("CNStock")


def fetch_daykline(symbol: str, retries: int = 4) -> Optional[list]:
    """EM 日级资金流历史 (push2his fflow/daykline, lmt=0 多年)。

    返回 [{time, main_net, small_net, mid_net, big_net, super_net}] (time 为
    "YYYY-MM-DD 15:00", 当日累计=日级值), 失败 None。akshare 同款参数 (ut token,
    普通 requests + UA); 需本机 ISP 网络 (数据中心 IP 被 EM 封, 见文件头)。
    字段序 (akshare 源码确认): 日期,主力,小单,中单,大单,超大单,占比x5,...
    """
    import time as _time
    import requests as _rq
    params = {
        "lmt": "0", "klt": "101",
        "secid": _to_secid(symbol),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36",
               "Referer": "https://data.eastmoney.com/"}
    for i in range(retries):
        try:
            # trust_env=False: 强制直连 — requests 默认会读环境变量+Windows 注册表代理,
            # 若挂着 Clash/V2ray 等代理 (境外出口), EM push2his 对境外 IP 直接断连
            # (09-12 实测: 用户终端走代理全 RST)
            s = _rq.Session()
            s.trust_env = False
            r = s.get(DAYKLINE_URL, params=params, headers=headers, timeout=20)
            kl = ((r.json().get("data") or {}).get("klines")) or []
            if not kl:
                print(f"    [daykline] klines 空 (HTTP {r.status_code})")
                return None
            rows = []
            for line in kl:
                p = line.split(",")
                try:
                    rows.append({
                        "time": f"{p[0]} 15:00",
                        **{c: float(p[j + 1]) for j, c in enumerate(NET_COLS)},
                    })
                except (ValueError, IndexError) as e:
                    print(f"    [daykline] 行解析跳过: {e} ({line[:40]})")
            return rows
        except Exception as e:
            print(f"    [daykline] 第{i + 1}次失败: {repr(e)[:110]}")
            _time.sleep(3 * (i + 1))
    return None


def _minute_complete_days(pool, symbol: str) -> set:
    """已落库且分钟根数 ≥ MINUTE_COMPLETE 的日期集合 (日级回补跳过这些日)。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT time::date, COUNT(*) FROM "{TABLE}" WHERE symbol = %s '
                f'GROUP BY 1 HAVING COUNT(*) >= %s',
                (symbol, MINUTE_COMPLETE))
            return {r[0].strftime("%Y-%m-%d") for r in cur.fetchall()}


def _write_daily_rows(pool, symbol: str, rows: list, dry_run: bool,
                      verbose: bool) -> int:
    """日级行落库公共段 (em/sina 两源共用): 跳过已有分钟数据的日期后写入。

    dry-run 时允许 pool=None (无 DB 也可预览, 跳过"已有分钟数据"的过滤)。
    """
    done = _minute_complete_days(pool, symbol) if pool is not None else set()
    keep = [r for r in rows if r["time"][:10] not in done]
    if verbose:
        print(f"    日级: {len(rows)} 日 ({rows[0]['time'][:10]} ~ {rows[-1]['time'][:10]}), "
              f"跳过已有分钟数据 {len(rows) - len(keep)} 日"
              + ("  [dry-run 无 DB, 未做跳过]" if pool is None else ""))
    if not keep:
        print("    ✓ 全部日期已有分钟数据, 无需回补")
        return 0
    written = write_to_db(pool, symbol, keep, dry_run=dry_run)
    if verbose:
        print(f"    → 写入 {written} 日 (每日 15:00 单行)")
    return written


def backfill(symbols: Optional[List[str]] = None, dry_run: bool = False,
             verbose: bool = False, source: str = "sina",
             force: bool = False) -> Dict[str, int]:
    """日级历史回补入口 (--backfill)。分钟级无免费源, 只回补日级 (15:00 单行)。

    source="sina" (默认, 方案2): 新浪个股四档按成分聚合 — 全网络可跑, 断点续跑
           (tmp/fflow_sina_resume.json), force=True 忽略断点全量重抓; 近似口径
           (当前成分), 2010-03-01 起, 见 sina_moneyflow.py 文件头。
    source="em": 东财 push2his daykline 官方口径 — 需本机 ISP 网络 (IP 封锁)。
    """
    import time as _time
    symbols = symbols or list(INDICES.keys())
    total, failed = 0, []

    if source == "sina":
        from app.market_cn.sina_moneyflow import get_indices_daily_flow
        print("  源: 新浪聚合 (个股四档 ssl_qsfx_lscjfb x 指数成分, 日级近似)")
        rows_map = get_indices_daily_flow(symbols, force=force, verbose=verbose)
        pool = None if dry_run else get_pool()
        for symbol in symbols:
            name = INDICES.get(symbol, symbol)
            print(f"  📊 {name} ({symbol})")
            rows = rows_map.get(symbol)
            if not rows:
                failed.append(symbol)
                print("    ❌ 聚合失败 (成分股/个股资金流获取失败)")
                continue
            total += _write_daily_rows(pool, symbol, rows, dry_run, verbose)
    else:
        pool = get_pool()
        for symbol in symbols:
            name = INDICES.get(symbol, symbol)
            print(f"  📊 {name} ({symbol})")
            rows = fetch_daykline(symbol)
            if not rows:
                failed.append(symbol)
                print("    ❌ 拉取失败 (push2his 不可达? 请在本机终端运行)")
                continue
            total += _write_daily_rows(pool, symbol, rows, dry_run, verbose)
            _time.sleep(3)  # EM 防限流

    print(f"\n  完成: {total} 条写入, {len(failed)} 个失败"
          + (f" ({', '.join(failed)})" if failed else ""))
    return {"written": total, "failed": len(failed)}


def verify_against_minute(symbols: Optional[List[str]] = None,
                          verbose: bool = False) -> None:
    """口径校验 (--verify): 新浪聚合日级 vs 已有东财分钟数据的每日日终值。

    分钟行是当日累计, 每日最后一根即日级值。逐日对比 main/small/mid/big/super,
    输出偏差统计 — 用于验证 sina r0~r3 → 东财五档映射是否对齐 (见
    sina_moneyflow.py 文件头 ★ 口径映射的残余风险说明)。
    """
    from app.market_cn.sina_moneyflow import get_indices_daily_flow
    symbols = symbols or list(INDICES.keys())
    rows_map = get_indices_daily_flow(symbols)
    pool = get_pool()
    for symbol, rows in rows_map.items():
        agg = {r["time"][:10]: r for r in rows}
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'''SELECT DISTINCT ON (time::date) time::date,
                           main_net, small_net, mid_net, big_net, super_net
                        FROM "{TABLE}" WHERE symbol = %s
                        ORDER BY time::date, time DESC''', (symbol,))
                db_rows = cur.fetchall()
        diffs, dates = [], []
        for d, *vals in db_rows:
            key = d.strftime("%Y-%m-%d")
            if key not in agg:
                continue
            a = [agg[key][k] for k in ("main_net", "small_net", "mid_net",
                                       "big_net", "super_net")]
            denom = max(abs(a[0]), 1.0)
            diffs.append(abs(a[0] - vals[0]) / denom)
            dates.append((key, a[0], vals[0]))
        if not diffs:
            print(f"  📊 {symbol}: 无重叠日期 (无分钟数据或新浪无覆盖), 跳过")
            continue
        avg = sum(diffs) / len(diffs)
        print(f"  📊 {symbol}: 重叠 {len(diffs)} 日, 主力净额相对偏差 "
              f"均值 {avg:.2%}, 最大 {max(diffs):.2%}")
        if verbose:
            for key, a0, v0 in dates[:5]:
                print(f"     {key} 新浪聚合={a0:,.0f}  东财分钟日终={v0:,.0f}")


def sync(symbols: Optional[List[str]] = None, dry_run: bool = False,
         verbose: bool = False) -> Dict[str, int]:
    """同步入口 (scheduler 与 CLI 共用)。返回 {written, failed} 统计。

    2026-10-27 起委托 app/data_sources/cn_stock_putdb.sync_index_fflow
    （coord_index index_fflow 多源调度；scheduler 同源入口）。
    """
    from app.data_sources.cn_stock_putdb import sync_index_fflow
    return sync_index_fflow(symbols, dry_run=dry_run, verbose=verbose)


def main():
    parser = argparse.ArgumentParser(description="A股主要指数大盘资金流同步 (kline_index_fflow)")
    parser.add_argument("--indices", help="指定指数, 逗号分隔 (如 000001.SH,399001.SZ)")
    parser.add_argument("--backfill", action="store_true",
                        help="日级历史回补 (默认新浪聚合; --source em 走 push2his)")
    parser.add_argument("--source", choices=["sina", "em"], default="sina",
                        help="回补数据源: sina=新浪聚合(默认,全网络) em=东财官方(需本机网络)")
    parser.add_argument("--force", action="store_true",
                        help="sina 聚合忽略断点缓存 (tmp/fflow_sina_resume.json) 全量重抓")
    parser.add_argument("--verify", action="store_true",
                        help="口径校验: 新浪聚合日级 vs 已有分钟数据日终值")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    symbols = None
    if args.indices:
        symbols = [s.strip() for s in args.indices.split(",") if s.strip() in INDICES]
        if not symbols:
            print("❌ 无有效指数代码")
            sys.exit(1)

    print("=" * 60)
    if args.verify:
        print(f"  sync_index_fflow — 口径校验 (新浪聚合 vs 分钟日终 → {TABLE})")
        print("=" * 60)
        verify_against_minute(symbols, verbose=args.verbose)
        return
    if args.backfill:
        print(f"  sync_index_fflow — 日级历史回补 ({args.source} → {TABLE})")
        print("  每日 15:00 单行 (当日累计=日级值); 分钟级历史无免费源")
        backfill(symbols, dry_run=args.dry_run, verbose=args.verbose,
                 source=args.source, force=args.force)
        return
    print(f"  sync_index_fflow — 指数大盘资金流同步 → {TABLE}")
    print("  源: EM fflow (1分钟当日累计, 240根/指数, host回退实时→delay)"
          + ("  [dry-run]" if args.dry_run else ""))
    print("=" * 60)
    sync(symbols, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
