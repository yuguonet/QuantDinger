#!/usr/bin/env python3
"""
sync_index_fflow.py — A股主要指数大盘资金流同步 (kline_index_fflow)

采集 EM 分钟级大盘资金流 (主力/超大单/大单/中单/小单 净流入, 元),
写入独立表 kline_index_fflow。与 kline_index_5m 同为指数维度独立表, 不混表。

数据源: EM fflow/kline (klt=1, 1分钟, 当日240根)
  - host 优先级: push2(实时, 秒级延迟) → push2delay(延迟镜像, ~15分钟滞后)
    2026-09-12 实测: push2/push2his 对本机 IP 级封锁 (5 种 TLS 指纹全 curl 56 RST,
    push2delay 同指纹畅通 → 非指纹问题); host 回退使封锁解除后自动升级实时
  - 必须走 curl_cffi (app.data_sources.provider.eastmoney._em_get):
    东财 push2 CDN 对 urllib TLS 指纹 (JA3) 封锁, edge101 指纹可通过
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

用法:
  python scripts/sync_index_fflow.py                # 全量 9 指数
  python scripts/sync_index_fflow.py --indices 000001.SH,399001.SZ
  python scripts/sync_index_fflow.py --dry-run

调度: 由 backend scheduler._post_market_batch 在指数 5m 同步后调用 sync() 函数。
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
# host 优先级: 实时主站优先 (秒级), 封锁时自动落延迟镜像 (~15分钟滞后)
FFLOW_HOSTS = [
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
]
# 日级历史端点 (push2his, akshare 同款参数; 只在本机网络可达)
DAYKLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
# 分钟根数阈值: 当日已有 ≥ 此数视为分钟数据完整, 日级回补跳过该日
MINUTE_COMPLETE = 240


def _to_secid(symbol: str) -> str:
    """'000001.SH' → '1.000001'; '399001.SZ' → '0.399001' (EM secid 规则)"""
    code, suffix = symbol.split(".")
    return ("1" if suffix == "SH" else "0") + "." + code


def fetch_fflow(symbol: str) -> Optional[list]:
    """EM 1分钟资金流。返回 [{time, main_net, small_net, mid_net, big_net,
    super_net}] (净额为当日累计, 元; time 为 bar 起始时刻), 失败 None。"""
    import json
    try:
        from app.data_sources.provider.eastmoney import _em_get
    except ImportError as e:
        print(f"    [fflow] 依赖导入失败: {e}")
        return None
    for base in FFLOW_HOSTS:
        url = (base
               + "?lmt=0&klt=1&fields1=f1,f2,f3,f7"
               + "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
               + f"&secid={_to_secid(symbol)}")
        txt = _em_get(url, timeout=15, retries=3)
        if txt:
            break  # 任一 host 成功即用 (实时优先, 封锁自动落 delay)
    if not txt:
        print("    [fflow] 拉取失败 (全部 host _em_get None)")
        return None
    try:
        d = json.loads(txt)
    except Exception as e:
        print(f"    [fflow] JSON 解析失败: {e}")
        return None
    kl = ((d.get("data") or {}).get("klines")) or []
    if not kl:
        print("    [fflow] 无数据")
        return None
    rows = []
    for line in kl:
        p = line.split(",")
        try:
            rows.append({
                "time": p[0],
                **{c: float(p[i + 1]) for i, c in enumerate(NET_COLS)},
            })
        except (ValueError, IndexError) as e:
            print(f"    [fflow] 行解析跳过: {e} ({line[:60]})")
    return rows


def write_to_db(pool, symbol: str, rows: list, dry_run: bool = False) -> int:
    if not rows:
        return 0
    table = f'"{TABLE}"'
    if dry_run:
        print(f"    [dry-run] {table} {symbol}: {len(rows)} 条 "
              f"({rows[0]['time']} ~ {rows[-1]['time']})")
        return len(rows)
    cols = ", ".join(NET_COLS)
    ph = ", ".join(["%s"] * (2 + len(NET_COLS)))
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        symbol VARCHAR(20) NOT NULL,
                        time   TIMESTAMP   NOT NULL,
                        {", ".join(f"{c} DOUBLE PRECISION" for c in NET_COLS)},
                        PRIMARY KEY (symbol, time)
                    )
                """)
                for r in rows:
                    cur.execute(f"""
                        INSERT INTO {table} (symbol, time, {cols})
                        VALUES ({ph})
                        ON CONFLICT (symbol, time) DO UPDATE SET
                            {", ".join(f"{c} = EXCLUDED.{c}" for c in NET_COLS)}
                    """, (symbol, r["time"], *[r[c] for c in NET_COLS]))
            conn.commit()
        return len(rows)
    except Exception as e:
        print(f"    ❌ {table} 写入失败: {e}")
        return 0


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


def backfill(symbols: Optional[List[str]] = None, dry_run: bool = False,
             verbose: bool = False) -> Dict[str, int]:
    """日级历史回补入口 (--backfill)。每小时级无免费源, 只回补日级 (15:00 单行)。"""
    import time as _time
    symbols = symbols or list(INDICES.keys())
    pool = get_pool()
    total, failed = 0, []
    for symbol in symbols:
        name = INDICES.get(symbol, symbol)
        print(f"  📊 {name} ({symbol})")
        rows = fetch_daykline(symbol)
        if not rows:
            failed.append(symbol)
            print("    ❌ 拉取失败 (push2his 不可达? 请在本机终端运行)")
            continue
        done = _minute_complete_days(pool, symbol)
        keep = [r for r in rows if r["time"][:10] not in done]
        if verbose:
            print(f"    daykline: {len(rows)} 日 ({rows[0]['time'][:10]} ~ {rows[-1]['time'][:10]}), "
                  f"跳过已有分钟数据 {len(rows) - len(keep)} 日")
        if not keep:
            print("    ✓ 全部日期已有分钟数据, 无需回补")
            continue
        written = write_to_db(pool, symbol, keep, dry_run=dry_run)
        total += written
        if verbose:
            print(f"    → 写入 {written} 日 (每日 15:00 单行)")
        _time.sleep(3)  # EM 防限流
    print(f"\n  完成: {total} 条写入, {len(failed)} 个失败"
          + (f" ({', '.join(failed)})" if failed else ""))
    return {"written": total, "failed": len(failed)}


def sync(symbols: Optional[List[str]] = None, dry_run: bool = False,
         verbose: bool = False) -> Dict[str, int]:
    """同步入口 (scheduler 与 CLI 共用)。返回 {written, failed} 统计。"""
    import time as _time
    symbols = symbols or list(INDICES.keys())
    pool = get_pool()
    total, failed = 0, []
    for symbol in symbols:
        name = INDICES.get(symbol, symbol)
        print(f"  📊 {name} ({symbol})")
        rows = fetch_fflow(symbol)
        if not rows:
            failed.append(symbol)
            print("    ❌ 拉取失败")
            continue
        if verbose:
            print(f"    fflow: {len(rows)} 根 ({rows[0]['time']} ~ {rows[-1]['time']}), "
                  f"主力累计尾值={rows[-1]['main_net']:.0f}")
        written = write_to_db(pool, symbol, rows, dry_run=dry_run)
        total += written
        if verbose:
            print(f"    → 写入 {written} 条")
        _time.sleep(2)  # EM 防限流
    print(f"\n  完成: {total} 条写入, {len(failed)} 个失败"
          + (f" ({', '.join(failed)})" if failed else ""))
    return {"written": total, "failed": len(failed)}


def main():
    parser = argparse.ArgumentParser(description="A股主要指数大盘资金流同步 (kline_index_fflow)")
    parser.add_argument("--indices", help="指定指数, 逗号分隔 (如 000001.SH,399001.SZ)")
    parser.add_argument("--backfill", action="store_true",
                        help="日级历史回补 (push2his daykline, 需本机终端运行)")
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
    if args.backfill:
        print(f"  sync_index_fflow — 日级历史回补 (push2his daykline → {TABLE})")
        print("  每日 15:00 单行 (当日累计=日级值); 分钟级历史无免费源")
        backfill(symbols, dry_run=args.dry_run, verbose=args.verbose)
        return
    print(f"  sync_index_fflow — 指数大盘资金流同步 → {TABLE}")
    print("  源: EM fflow (1分钟当日累计, 240根/指数, host回退实时→delay)"
          + ("  [dry-run]" if args.dry_run else ""))
    print("=" * 60)
    sync(symbols, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
