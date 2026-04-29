# -*- coding: utf-8 -*-
"""
批量数据下载器

从现有数据源（腾讯/新浪/东财）批量下载历史 K 线数据到本地仓库。

用法（在项目根目录运行）:
    # 下载 A 股日线（默认沪深300成分股）
    python -m optimizer.data_warehouse.downloader --market CNStock --timeframe 1D

    # 指定股票列表
    python -m optimizer.data_warehouse.downloader --market CNStock --symbols "000001.SZ,600000.SH,300750.SZ"

    # 从文件读取股票列表（每行一个）
    python -m optimizer.data_warehouse.downloader --market CNStock --symbols-file stock_list.txt

    # 下载多年数据
    python -m optimizer.data_warehouse.downloader --market CNStock --years 5

    # 查看仓库状态
    python -m app.data_warehouse.downloader --status
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from optimizer.data_warehouse.storage import (
    write_local, exists, get_stats, list_local, get_warehouse_root, _TF_DIR_MAP,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 股票列表获取
# ============================================================

def _get_ashare_stock_list() -> List[str]:
    """
    获取 A 股股票列表。
    优先从 AKShare 获取，fallback 到东方财富 API。
    """
    # 尝试 AKShare
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        codes = df["代码"].tolist()
        symbols = []
        for code in codes:
            code = str(code).zfill(6)
            if code.startswith("6"):
                symbols.append(f"{code}.SH")
            elif code.startswith(("0", "3")):
                symbols.append(f"{code}.SZ")
            elif code.startswith(("8", "4")):
                symbols.append(f"{code}.BJ")
        logger.info(f"AKShare 获取 A 股列表: {len(symbols)} 只")
        return symbols
    except Exception as e:
        logger.warning(f"AKShare 获取股票列表失败: {e}")

    # Fallback: 东方财富 API
    try:
        import requests
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1, "pz": 10000, "po": 1, "np": 1,
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f13",
        }
        resp = requests.get(url, timeout=15)
        data = resp.json()
        items = data.get("data", {}).get("diff", [])
        symbols = []
        for item in items:
            code = str(item.get("f12", ""))
            market_id = item.get("f13", 0)
            if market_id == 0:
                symbols.append(f"{code}.SZ")
            elif market_id == 1:
                symbols.append(f"{code}.SH")
        logger.info(f"东方财富获取 A 股列表: {len(symbols)} 只")
        return symbols
    except Exception as e:
        logger.warning(f"东方财富获取股票列表失败: {e}")

    return []


def _get_index_stocks(index: str = "hs300") -> List[str]:
    """获取指数成分股"""
    try:
        import akshare as ak
        if index == "hs300":
            df = ak.index_stock_cons_csindex(symbol="000300")
        elif index == "zz500":
            df = ak.index_stock_cons_csindex(symbol="000905")
        elif index == "zz1000":
            df = ak.index_stock_cons_csindex(symbol="000852")
        else:
            return _get_ashare_stock_list()

        codes = df["成分券代码"].tolist()
        symbols = []
        for code in codes:
            code = str(code).zfill(6)
            if code.startswith("6"):
                symbols.append(f"{code}.SH")
            else:
                symbols.append(f"{code}.SZ")
        return symbols
    except Exception as e:
        logger.warning(f"获取指数成分股失败 ({index}): {e}")
        return []


# ============================================================
# 数据下载核心
# ============================================================

def _fetch_kline_from_source(
    market: str,
    symbol: str,
    timeframe: str,
    count: int,
) -> List[Dict[str, Any]]:
    """
    从现有数据源拉取 K 线数据。
    直接复用 DataSourceFactory 的数据源链路。
    """
    from optimizer.data_warehouse.factory2 import DataSourceFactory2

    source = DataSourceFactory2.get_source(market)
    data = source.get_kline(
        symbol=symbol,
        timeframe=timeframe,
        limit=count,
    )
    return data or []


def download_single(
    market: str,
    symbol: str,
    timeframe: str,
    years: int = 3,
    force: bool = False,
) -> Dict[str, Any]:
    """
    下载单只股票的历史数据。

    Args:
        market: 市场类型
        symbol: 股票代码
        timeframe: 时间周期
        years: 下载几年的数据
        force: 是否强制重新下载

    Returns:
        {"symbol": str, "status": str, "rows": int, "error": str}
    """
    result = {"symbol": symbol, "status": "skipped", "rows": 0, "error": ""}

    # 检查是否已有数据
    if not force and exists(market, timeframe, symbol):
        result["status"] = "exists"
        return result

    # 计算需要拉取的 K 线数量
    tf_days = {"1D": 1, "1W": 7, "1M": 30, "1H": 1/24, "4H": 4/24}
    days_per_bar = tf_days.get(timeframe, 1)
    total_days = years * 365
    count = int(total_days / days_per_bar) + 100  # 多拉一些余量
    count = min(count, 5000)  # API 单次上限

    # A 股日线需要分批拉取（腾讯单次最多 ~640 根）
    if market == "CNStock" and timeframe == "1D":
        all_data = _fetch_ashare_daily_batch(symbol, years)
    else:
        all_data = _fetch_kline_from_source(market, symbol, timeframe, count)

    if not all_data:
        result["status"] = "failed"
        result["error"] = "数据源返回空"
        return result

    # 写入本地仓库
    rows = write_local(market, timeframe, symbol, all_data, mode="overwrite")
    result["status"] = "downloaded"
    result["rows"] = rows
    return result


def _fetch_ashare_daily_batch(symbol: str, years: int = 3) -> List[Dict[str, Any]]:
    """
    A 股日线分批拉取。
    腾讯 fqkline 单次最多 ~640 根日线，需要循环拉取。
    """
    from app.data_sources.tencent import normalize_cn_code, fetch_kline, tencent_kline_rows_to_dicts

    code = normalize_cn_code(symbol)
    all_data: List[Dict[str, Any]] = []
    total_bars = years * 250  # 约 250 个交易日/年

    # 腾讯 fqkline 单次上限约 800 根，超过 1000 返回空数据
    # 800 根日线 ≈ 3.2 年，足够覆盖常见需求
    raw = fetch_kline(code, "day", count=800, adj="qfq")
    if raw:
        all_data = tencent_kline_rows_to_dicts(raw)

    if not all_data:
        # Fallback: 尝试东方财富
        try:
            from app.data_sources.eastmoney import fetch_eastmoney_kline, eastmoney_kline_to_ticker
            raw2 = fetch_eastmoney_kline(code, "1D", count=10000)
            if raw2:
                all_data = eastmoney_kline_to_ticker(raw2)
        except Exception:
            pass

    # 按时间升序
    all_data.sort(key=lambda x: x["time"])

    # 只保留最近 N 年
    if years and all_data:
        cutoff = int((datetime.now() - timedelta(days=years * 365)).timestamp())
        all_data = [d for d in all_data if d["time"] >= cutoff]

    return all_data


# ============================================================
# 批量下载
# ============================================================

def download_batch(
    market: str,
    symbols: List[str],
    timeframe: str = "1D",
    years: int = 3,
    force: bool = False,
    delay: float = 0.3,
) -> Dict[str, Any]:
    """
    批量下载多只股票的历史数据。

    Args:
        market: 市场类型
        symbols: 股票代码列表
        timeframe: 时间周期
        years: 下载几年的数据
        force: 是否强制重新下载
        delay: 每只股票之间的延迟（秒），防止触发限流

    Returns:
        {"total": int, "downloaded": int, "skipped": int, "failed": int, "errors": [...]}
    """
    total = len(symbols)
    downloaded = 0
    skipped = 0
    failed = 0
    errors: List[str] = []

    logger.info(f"开始批量下载: {total} 只股票, {market} {timeframe}, {years}年数据")
    logger.info(f"存储路径: {get_warehouse_root()}")

    for i, symbol in enumerate(symbols, 1):
        try:
            result = download_single(market, symbol, timeframe, years, force)
            status = result["status"]

            if status == "downloaded":
                downloaded += 1
                logger.info(f"[{i}/{total}] ✅ {symbol}: {result['rows']} 条")
            elif status == "exists":
                skipped += 1
                if i % 100 == 0:
                    logger.info(f"[{i}/{total}] ⏭️  {symbol}: 已存在，跳过")
            elif status == "failed":
                failed += 1
                errors.append(f"{symbol}: {result['error']}")
                logger.warning(f"[{i}/{total}] ❌ {symbol}: {result['error']}")

            # 延迟防限流
            if delay > 0 and status == "downloaded":
                time.sleep(delay)

        except Exception as e:
            failed += 1
            errors.append(f"{symbol}: {e}")
            logger.error(f"[{i}/{total}] 💥 {symbol}: {e}")

        # 进度报告
        if i % 50 == 0:
            logger.info(f"进度: {i}/{total} (下载={downloaded} 跳过={skipped} 失败={failed})")

    summary = {
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],  # 只保留前 20 个错误
    }

    logger.info(
        f"批量下载完成: 共 {total} 只, "
        f"下载 {downloaded}, 跳过 {skipped}, 失败 {failed}"
    )
    return summary


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="QuantDinger 数据仓库批量下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
存储路径: data_warehouse/{市场}/{周期}/{股票代码}.csv
  例: data_warehouse/CNStock/daily/000001.SZ.csv
      data_warehouse/CNStock/weekly/600000.SH.csv
      data_warehouse/Crypto/4h/BTC_USDT.csv

CSV 格式: time,open,high,low,close,volume (time 为 Unix 秒)

示例（在项目根目录运行）:
  # 下载沪深300日线（默认）
  python -m optimizer.data_warehouse.downloader -m CNStock -tf 1D

  # 指定股票
  python -m optimizer.data_warehouse.downloader -m CNStock -s "000001.SZ,600000.SH"

  # 下载到自定义目录
  python -m optimizer.data_warehouse.downloader -m CNStock -tf 1D -o /path/to/my_data

  # 查看仓库状态
  python -m optimizer.data_warehouse.downloader --status
""",
    )
    parser.add_argument("--market", "-m", default="CNStock", help="市场类型 (CNStock, Crypto, ...)")
    parser.add_argument("--timeframe", "-tf", default="1D", help="时间周期 (1D, 1H, ...)")
    parser.add_argument("--years", "-y", type=int, default=3, help="下载几年的数据")
    parser.add_argument("--force", "-f", action="store_true", help="强制重新下载已有数据")
    parser.add_argument("--delay", type=float, default=0.3, help="每只股票间隔秒数")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="数据仓库根目录 (默认: optimizer/data_warehouse/)")

    # 股票来源
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--symbols", "-s", type=str, help="逗号分隔的股票代码")
    src.add_argument("--symbols-file", type=str, help="股票代码文件（每行一个）")
    src.add_argument("--index", type=str, help="指数成分股 (hs300, zz500, zz1000, all)")
    src.add_argument("--all-stocks", action="store_true", help="下载全部 A 股")

    # 其他
    parser.add_argument("--status", action="store_true", help="查看仓库状态")
    parser.add_argument("--list", "-l", action="store_true", help="列出本地已有数据")

    args = parser.parse_args()

    # 指定自定义仓库根目录
    if args.output:
        os.environ["DATA_WAREHOUSE_ROOT"] = os.path.abspath(args.output)
        print(f"   📁 自定义仓库路径: {os.environ['DATA_WAREHOUSE_ROOT']}")

    # 查看状态
    if args.status:
        stats = get_stats()
        root = stats['root']
        print(f"\n📊 数据仓库状态")
        print(f"   路径: {root}")
        print(f"   目录结构: {root}/{{市场}}/{{周期}}/{{股票代码}}.csv")
        print(f"   存在: {stats['exists']}")
        if stats['exists']:
            print(f"   股票数: {stats['stocks']}")
            print(f"   总行数: {stats['total_rows']:,}")
            for mkt, count in stats.get('markets', {}).items():
                print(f"   - {mkt}: {count} 只")
                # 展示该市场下的子目录（周期）
                mkt_dir = os.path.join(root, mkt)
                if os.path.isdir(mkt_dir):
                    for tf in sorted(os.listdir(mkt_dir)):
                        tf_dir = os.path.join(mkt_dir, tf)
                        if os.path.isdir(tf_dir):
                            n = len([f for f in os.listdir(tf_dir) if f.endswith('.csv')])
                            print(f"     {tf}/: {n} 只")
        return

    # 列出已有数据
    if args.list:
        symbols = list_local(args.market, args.timeframe)
        print(f"\n📋 本地数据: {len(symbols)} 只")
        for s in symbols[:50]:
            print(f"   {s}")
        if len(symbols) > 50:
            print(f"   ... 共 {len(symbols)} 只")
        return

    # 构建股票列表
    symbols = []
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.symbols_file:
        with open(args.symbols_file, "r") as f:
            symbols = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    elif args.index:
        if args.index == "all":
            symbols = _get_ashare_stock_list()
        else:
            symbols = _get_index_stocks(args.index)
    elif args.all_stocks:
        symbols = _get_ashare_stock_list()
    else:
        # 默认: 沪深300
        symbols = _get_index_stocks("hs300")

    if not symbols:
        print("❌ 没有获取到股票列表，请用 --symbols 或 --index 指定")
        return

    print(f"\n🚀 准备下载 {len(symbols)} 只股票的 {args.timeframe} 数据 ({args.years}年)")
    print(f"   市场: {args.market}")
    print(f"   存储: {get_warehouse_root()}/{args.market}/{_TF_DIR_MAP.get(args.timeframe, args.timeframe.lower())}/")
    print(f"   格式: {{symbol}}.csv (time,open,high,low,close,volume)")

    summary = download_batch(
        market=args.market,
        symbols=symbols,
        timeframe=args.timeframe,
        years=args.years,
        force=args.force,
        delay=args.delay,
    )

    print(f"\n📊 下载结果:")
    print(f"   总计: {summary['total']}")
    print(f"   下载: {summary['downloaded']}")
    print(f"   跳过: {summary['skipped']}")
    print(f"   失败: {summary['failed']}")
    if summary['errors']:
        print(f"   错误示例:")
        for err in summary['errors'][:5]:
            print(f"     - {err}")


if __name__ == "__main__":
    main()
