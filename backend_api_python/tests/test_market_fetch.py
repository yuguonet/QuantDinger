#!/usr/bin/env python3
"""
A股全市场K线 — Provider 层速度测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
原 akline_market.py 的测试版本。
调用 provider/ 中相应接口代替内部实现，用于测试每个源的速度和可用性。

用法:
  python test_market_fetch.py                      # 测试所有源的全市场15min
  python test_market_fetch.py --sources em_trends2 # 测试指定源
  python test_market_fetch.py --sources tencent,sina # 测试多个源
  python test_market_fetch.py --limit 100          # 只测100只
  python test_market_fetch.py --codes 600519,000001 # 指定代码
  python test_market_fetch.py --timeframe 1D       # 日线模式
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from queue import Queue, Empty

# 确保项目路径在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.data_sources.provider import get_providers, autodiscover, NotSupportedResult


# ═══════════════ 配置 ═══════════════
OUTPUT_DIR = "kline_data"
GROUP_SIZE = 50
THREADS_PER_SOURCE = 30


# ═══════════════ 源统计 ═══════════════
class SourceStats:
    """单个数据源的统计信息"""

    def __init__(self, name):
        self.name = name
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.start_time = time.time()
        self.lock = threading.Lock()
        self.groups_done = 0
        self.errors = []

    def record(self, success, error_msg=""):
        with self.lock:
            self.done += 1
            if success:
                self.ok += 1
            else:
                self.fail += 1
                if error_msg:
                    self.errors.append(error_msg[:200])

    def speed(self):
        elapsed = time.time() - self.start_time
        return self.ok / elapsed if elapsed >= 1 else 0


# ═══════════════ Provider 适配器 ═══════════════
def create_fetch_fn(provider, timeframe="15m", count=200, adj="qfq"):
    """
    将 Provider 的 fetch_kline 包装为统一的 fetch 函数。

    Args:
        provider: Provider 实例
        timeframe: K线周期
        count: 数据条数
        adj: 复权方式

    Returns:
        fetch_fn(code) -> list[dict] | None
    """
    def fetch_fn(code):
        try:
            result = provider.fetch_kline(
                code, timeframe, count, adj=adj, timeout=10,
            )
            if not result or isinstance(result, NotSupportedResult):
                return None
            return result
        except Exception as e:
            return None
    return fetch_fn


def create_market_fetch_fn(provider, timeframe="15m", count=200, adj="qfq"):
    """
    将 Provider 的 fetch_market_kline 包装为统一的 fetch 函数。

    Args:
        provider: Provider 实例
        timeframe: K线周期
        count: 数据条数
        adj: 复权方式

    Returns:
        fetch_fn() -> dict[code, list[dict]] | None
    """
    def fetch_fn():
        try:
            result = provider.fetch_market_kline(
                timeframe=timeframe, count=count, adj=adj, timeout=30,
            )
            if not result or isinstance(result, NotSupportedResult):
                return None
            return result
        except Exception as e:
            print(f"  ❌ {provider.name} fetch_market_kline 异常: {e}")
            return None
    return fetch_fn


# ═══════════════ 股票列表 ═══════════════
def get_stock_list():
    """获取A股股票列表 — 新浪/东财 多源 fallback"""
    import requests as _requests

    # 方式1: 新浪财经（国内可达、稳定）
    try:
        stocks = []
        for node in ("sh_a", "sz_a"):
            page = 1
            while True:
                url = (
                    f"https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                    f"json_v2.php/Market_Center.getHQNodeData?"
                    f"page={page}&num=5000&sort=symbol&asc=1&node={node}"
                )
                resp = _requests.get(url, timeout=10, headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"
                })
                items = resp.json()
                if not items:
                    break
                for item in items:
                    # symbol 已带前缀: "sh600000" / "sz000001"
                    sym = item.get("symbol", "")
                    name = item.get("name", "")
                    if sym and len(sym) >= 8:
                        stocks.append({"code": sym, "name": name})
                if len(items) > 5000:
                    break
                page += 1
        if stocks:
            print(f"  ✅ 新浪获取 {len(stocks)} 只股票")
            return stocks
    except Exception as e:
        print(f"  ⚠️ 新浪获取失败: {e}")

    # 方式2: 东财（兜底）
    try:
        from app.data_sources.provider.eastmoney import _make_headers
        host = "push2.eastmoney.com"
        url = f"https://{host}/api/qt/clist/get"
        params = {
            "pn": 1, "pz": 6000, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14,f13",
        }
        resp = _requests.get(url, headers=_make_headers(), params=params, timeout=15, verify=False)
        data = resp.json()
        diff = ((data.get("data") or {}).get("diff")) or []
        stocks = []
        for i in diff:
            c, n, m = i.get("f12", ""), i.get("f14", ""), i.get("f13", 0)
            if c:
                stocks.append({"code": f"{'sh' if m == 1 else 'sz'}{c}", "name": n})
        if stocks:
            print(f"  ✅ 东财获取 {len(stocks)} 只股票")
            return stocks
    except Exception as e:
        print(f"  ⚠️ 东财获取失败: {e}")

    print("  ❌ 所有源获取失败")
    return []


# ═══════════════ 源Worker（逐只模式） ═══════════════
def source_worker(fetch_fn, queue, stats, out_dir, threads, timeframe="15m"):
    """
    每个源的 worker: 从队列取组，并发获取，完成立即取下一组。
    保持与 akline_market.py 一致的线程结构。
    """
    subdir = os.path.join(out_dir, timeframe)
    os.makedirs(subdir, exist_ok=True)
    header = "time,open,high,low,close,volume\n"

    def fetch_one(stock):
        code = stock["code"]
        try:
            data = fetch_fn(code)
            if data and len(data) > 0:
                fp = os.path.join(subdir, f"{code}.csv")
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(header)
                    for r in data:
                        t = r.get("time", "")
                        # 如果是 Unix 时间戳，转换为可读格式
                        if isinstance(t, (int, float)) and t > 1000000000:
                            t = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
                        f.write(f"{t},{r.get('open',0)},{r.get('high',0)},"
                                f"{r.get('low',0)},{r.get('close',0)},{r.get('volume',0)}\n")
                stats.record(True)
                return
        except Exception as e:
            stats.record(False, str(e))
            return
        stats.record(False, "empty")

    with ThreadPoolExecutor(max_workers=threads) as pool:
        while True:
            try:
                _, stocks = queue.get(timeout=5)
            except Empty:
                break
            futs = [pool.submit(fetch_one, s) for s in stocks]
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass
            stats.groups_done += 1
            queue.task_done()


# ═══════════════ 源Worker（全市场批量模式） ═══════════════
def market_source_worker(market_fetch_fn, stats, out_dir, timeframe="15m"):
    """
    全市场批量模式: 一次性获取所有数据。
    """
    subdir = os.path.join(out_dir, timeframe)
    os.makedirs(subdir, exist_ok=True)
    header = "time,open,high,low,close,volume\n"

    try:
        result = market_fetch_fn()
        if result and isinstance(result, dict):
            for code, bars in result.items():
                if not bars:
                    continue
                fp = os.path.join(subdir, f"{code}.csv")
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(header)
                    for r in bars:
                        t = r.get("time", "")
                        if isinstance(t, (int, float)) and t > 1000000000:
                            t = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
                        f.write(f"{t},{r.get('open',0)},{r.get('high',0)},"
                                f"{r.get('low',0)},{r.get('close',0)},{r.get('volume',0)}\n")
                stats.record(True)
            stats.groups_done = 1
            return
    except Exception as e:
        print(f"  ❌ {stats.name} 全市场批量异常: {e}")

    stats.record(False, "批量获取失败")


# ═══════════════ 实时显示 ═══════════════
BAR_LEN = 40


def display(workers, total, stop):
    """终端实时进度显示"""
    n = len(workers)
    while not stop.is_set():
        lines = [""]
        lines.append(f"  ⏱ {datetime.now().strftime('%H:%M:%S')} | {total}只 × {n}源")
        lines.append(f"  {'─' * 75}")
        tot_ok = tot_fail = 0
        for w in workers:
            s = w["stats"]
            tot_ok += s.ok
            tot_fail += s.fail
            done = s.done
            pct = done / total if total else 0
            filled = int(pct * BAR_LEN)
            bar = "█" * filled + "░" * (BAR_LEN - filled)
            alive = "🟢" if w["thread"].is_alive() else "⏹"
            lines.append(
                f"  {s.name:12s} {bar} {done:>4d}/{total}  "
                f"✅{s.ok:>4d} ❌{s.fail:>3d}  "
                f"{s.speed():>5.1f}只/秒  {s.groups_done}组 {alive}"
            )
        lines.append(f"  {'─' * 75}")
        expect = total * n
        tot_done = tot_ok + tot_fail
        pct_all = tot_done / expect if expect else 0
        filled_all = int(pct_all * BAR_LEN)
        bar_all = "█" * filled_all + "░" * (BAR_LEN - filled_all)
        lines.append(
            f"  {'总计':12s} {bar_all} {tot_done:>4d}/{expect}  "
            f"✅{tot_ok:>4d} ❌{tot_fail:>3d}"
        )
        out = "\n".join(lines)
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
        if all(not w["thread"].is_alive() for w in workers):
            break
        sys.stdout.write(f"\033[{len(lines)}A")
        time.sleep(1)

    # 最终结果
    lines = ["", f"  ✅ {datetime.now().strftime('%H:%M:%S')} 完成", f"  {'─' * 75}"]
    tot_ok = tot_fail = 0
    for w in sorted(workers, key=lambda w: -w["stats"].ok):
        s = w["stats"]
        tot_ok += s.ok
        tot_fail += s.fail
        done = s.done
        pct = done / total if total else 0
        filled = int(pct * BAR_LEN)
        bar = "█" * filled + "░" * (BAR_LEN - filled)
        lines.append(
            f"  {s.name:12s} {bar} {done:>4d}/{total}  "
            f"✅{s.ok:>4d} ❌{s.fail:>3d}  "
            f"{s.speed():>5.1f}只/秒  {s.groups_done}组"
        )
    lines += [f"  {'─' * 75}", f"  合计 ✅{tot_ok} ❌{tot_fail}", ""]

    # 显示错误摘要
    for w in workers:
        s = w["stats"]
        if s.errors:
            lines.append(f"  ⚠️  {s.name} 错误示例: {s.errors[0]}")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# ═══════════════ 速度排名 ═══════════════
def print_speed_ranking(workers):
    """打印速度排名"""
    print(f"\n  📊 速度排名:")
    print(f"  {'─' * 50}")
    ranked = sorted(workers, key=lambda w: w["stats"].speed(), reverse=True)
    for i, w in enumerate(ranked, 1):
        s = w["stats"]
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f" {i}."
        elapsed = time.time() - s.start_time
        print(
            f"  {medal} {s.name:12s}  "
            f"{s.speed():>6.1f}只/秒  "
            f"✅{s.ok:>4d}  "
            f"⏱{elapsed:.1f}s  "
            f"{s.groups_done}组"
        )
    print(f"  {'─' * 50}\n")


# ═══════════════ main ═══════════════
def main():
    p = argparse.ArgumentParser(description="A股全市场K线 — Provider 层速度测试")
    p.add_argument("--limit", type=int, default=0, help="限制股票数量 (0=全部)")
    p.add_argument("--group-size", type=int, default=GROUP_SIZE, help="每组股票数")
    p.add_argument("--threads", type=int, default=THREADS_PER_SOURCE, help="每源线程数")
    p.add_argument("--codes", type=str, default="", help="指定代码,逗号分隔")
    p.add_argument("--sources", type=str, default="", help="源名,逗号分隔. 可选: em_trends2,tdx,eastmoney,tencent,sina,baidu,sohu,xueqiu")
    p.add_argument("--timeframe", type=str, default="15m", help="K线周期 (15m/1D)")
    p.add_argument("--batch", action="store_true", help="使用全市场批量模式 (fetch_market_kline)")
    p.add_argument("--count", type=int, default=200, help="数据条数")
    p.add_argument("--adj", type=str, default="qfq", help="复权方式 (qfq/hfq/)")
    args = p.parse_args()

    print("=" * 65)
    print(f"  A股全市场K线 — Provider 速度测试 | {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  周期: {args.timeframe} | 每组{args.group_size}只 × 每源{args.threads}线程")
    print(f"  模式: {'全市场批量' if args.batch else '逐只并发'} | 复权: {args.adj}")
    print("=" * 65)

    # ── 获取可用 Provider ──
    autodiscover()
    providers = get_providers(capability="kline", timeframe=args.timeframe, market="CNStock")

    if not providers:
        print("  ❌ 无可用 Provider")
        return

    # 过滤指定源
    if args.sources:
        names = [s.strip() for s in args.sources.split(",")]
        providers = [p for p in providers if p.name in names]
        if not providers:
            print(f"  ❌ 指定的源 {names} 不可用")
            return

    print(f"\n  📡 {len(providers)} 个 Provider: {' | '.join(p.name for p in providers)}")
    for p in providers:
        caps = p.capabilities
        batch = "✅" if caps.get("kline_batch") else "❌"
        tfs = ", ".join(sorted(caps.get("kline_tf", set())))
        print(f"    {p.name:12s} priority={p.priority:<3d} 批量={batch} 周期={tfs}")

    # ── 获取股票列表 ──
    print(f"\n  📋 获取股票列表...")
    if args.codes:
        stocks = [{"code": c.strip(), "name": c.strip()} for c in args.codes.split(",") if c.strip()]
    else:
        stocks = get_stock_list()
    if not stocks:
        print("  ❌ 获取失败")
        return
    if args.limit > 0:
        stocks = stocks[:args.limit]
    print(f"  ✅ {len(stocks)} 只")

    # ── 构建任务 ──
    groups = [stocks[i:i + args.group_size] for i in range(0, len(stocks), args.group_size)]
    print(f"  📦 {len(groups)} 组 → 队列就绪\n  🚀 启动...")

    workers = []
    t0 = time.time()

    for provider in providers:
        st = SourceStats(provider.name)

        if args.batch:
            # 全市场批量模式
            market_fn = create_market_fetch_fn(
                provider, timeframe=args.timeframe, count=args.count, adj=args.adj,
            )
            t = threading.Thread(
                target=market_source_worker,
                args=(market_fn, st, OUTPUT_DIR, args.timeframe),
                daemon=True,
            )
        else:
            # 逐只并发模式
            fetch_fn = create_fetch_fn(
                provider, timeframe=args.timeframe, count=args.count, adj=args.adj,
            )
            q = Queue()
            for idx, g in enumerate(groups):
                q.put((idx, g))
            t = threading.Thread(
                target=source_worker,
                args=(fetch_fn, q, st, OUTPUT_DIR, args.threads, args.timeframe),
                daemon=True,
            )

        workers.append({"thread": t, "stats": st})
        t.start()

    # ── 实时显示 ──
    stop = threading.Event()
    disp = threading.Thread(target=display, args=(workers, len(stocks), stop), daemon=True)
    disp.start()

    for w in workers:
        w["thread"].join()
    stop.set()
    disp.join(timeout=3)

    # ── 结果 ──
    elapsed = time.time() - t0
    tot_ok = sum(w["stats"].ok for w in workers)

    print(f"\n  ⏱ 耗时 {elapsed:.1f}s | 整体 {tot_ok / elapsed:.1f}只/秒" if elapsed > 0 else "")
    print_speed_ranking(workers)
    print(f"  📁 {os.path.abspath(OUTPUT_DIR)}/{args.timeframe}/")


if __name__ == "__main__":
    main()
