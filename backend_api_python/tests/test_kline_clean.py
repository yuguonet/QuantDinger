#!/usr/bin/env python3
"""
kline_clean 1D 数据诊断脚本

检查为什么从 kline_clean 获取不到 1D K 线数据。
在 backend_api_python 目录下运行:
    cd backend_api_python
    python test_kline_clean.py [symbol] [market]

默认: symbol=600519 market=CNStock
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# ── 路径修复: 确保能 import app 包 ──
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# ── 加载 .env 文件（Flask 启动时自动加载，独立脚本需要手动） ──
_project_root = os.path.dirname(_backend_dir)  # QuantDinger/
_env_loaded = False

# 优先用 python-dotenv
try:
    from dotenv import load_dotenv
    for _env_path in [
        os.path.join(_backend_dir, ".env"),
        os.path.join(_project_root, ".env"),
    ]:
        if os.path.isfile(_env_path):
            load_dotenv(_env_path, override=False)
            print(f"  📄 已加载 .env: {_env_path}")
            _env_loaded = True
            break
except ImportError:
    pass

# fallback: 手动解析 .env
if not _env_loaded:
    for _env_path in [
        os.path.join(_backend_dir, ".env"),
        os.path.join(_project_root, ".env"),
    ]:
        if os.path.isfile(_env_path):
            with open(_env_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        _k = _k.strip()
                        _v = _v.strip().strip('"').strip("'")
                        if _k and _k not in os.environ:
                            os.environ[_k] = _v
            print(f"  📄 已加载 .env (手动解析): {_env_path}")
            _env_loaded = True
            break

if not _env_loaded:
    print("  ⚠️  未找到 .env 文件，DATABASE_URL 需已在系统环境变量中")

# ── 配置 ──
SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "600519"
MARKET = sys.argv[2] if len(sys.argv) > 2 else "CNStock"
TZ_SH = timezone(timedelta(hours=8))


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def ok(msg):
    print(f"  ✅ {msg}")


def fail(msg):
    print(f"  ❌ {msg}")


def warn(msg):
    print(f"  ⚠️  {msg}")


def info(msg):
    print(f"  ℹ️  {msg}")


# ═══════════════════════════════════════════════════════════════
#  1. 环境检查
# ═══════════════════════════════════════════════════════════════
section("1. 环境变量检查")

db_url = os.getenv("DATABASE_URL", "")
if db_url:
    # 脱敏显示
    safe_url = db_url
    if "@" in safe_url:
        parts = safe_url.split("@")
        user_pass = parts[0].split("://")[-1]
        if ":" in user_pass:
            user = user_pass.split(":")[0]
            safe_url = safe_url.replace(user_pass, f"{user}:***")
    info(f"DATABASE_URL = {safe_url}")
else:
    fail("DATABASE_URL 未设置！kline_clean 将无法工作")

strategy_db = os.getenv("STRATEGY_DB_NAME", "")
info(f"STRATEGY_DB_NAME = {strategy_db or '(未设置，将从 DATABASE_URL 推导)'}")

try:
    import psycopg2
    ok(f"psycopg2 已安装 (版本: {psycopg2.__version__})")
except ImportError:
    fail("psycopg2 未安装！pip install psycopg2-binary")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
#  2. 数据库连接检查
# ═══════════════════════════════════════════════════════════════
section("2. 数据库连接检查")

if not db_url:
    fail("跳过（DATABASE_URL 未设置）")
else:
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        ok(f"数据库连接成功: server_version={conn.server_version}")
        conn.close()
    except Exception as e:
        fail(f"数据库连接失败: {e}")


# ═══════════════════════════════════════════════════════════════
#  3. MarketDBManager 初始化
# ═══════════════════════════════════════════════════════════════
section("3. MarketDBManager 初始化")

try:
    from app.utils.db_market import get_market_db_manager, get_market_kline_writer
    mgr = get_market_db_manager()
    ok("MarketDBManager 初始化成功")
    info(f"base_conn_url = {mgr._base_conn_url[:50]}..." if len(mgr._base_conn_url) > 50 else f"base_conn_url = {mgr._base_conn_url}")
    info(f"strategy_db_name = {mgr._strategy_db_name}")
except Exception as e:
    fail(f"MarketDBManager 初始化失败: {e}")
    import traceback; traceback.print_exc()
    mgr = None


# ═══════════════════════════════════════════════════════════════
#  4. 市场数据库存在性检查
# ═══════════════════════════════════════════════════════════════
section(f"4. 市场数据库检查: {MARKET}")

if mgr:
    try:
        exists = mgr.market_db_exists(MARKET)
        if exists:
            ok(f"数据库存在")
        else:
            fail(f"数据库不存在！需要先运行数据导入")
            info("提示: 运行 tdx_download.py 导入数据，或检查数据库名映射")
    except Exception as e:
        fail(f"检查数据库存在性失败: {e}")

    # 列出所有数据库
    try:
        conn = psycopg2.connect(mgr._base_conn_url, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
        dbs = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
        info(f"所有数据库: {dbs}")
    except Exception as e:
        warn(f"无法列出数据库: {e}")


# ═══════════════════════════════════════════════════════════════
#  5. 分区表检查
# ═══════════════════════════════════════════════════════════════
section("5. 1D 分区表检查")

if mgr:
    try:
        pool = mgr._get_pool(MARKET)
        with pool.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name LIKE 'kline_1D_%'
                ORDER BY table_name
            """)
            tables_1d = [r[0] for r in cur.fetchall()]

            if tables_1d:
                ok(f"找到 {len(tables_1d)} 个 1D 分区表: {tables_1d}")
            else:
                fail("没有找到 kline_1D_* 分区表！")
                info("需要先导入日线数据: python tdx_download.py -T 1D")

            # 列出所有 kline 表
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name LIKE 'kline_%'
                ORDER BY table_name
            """)
            all_kline_tables = [r[0] for r in cur.fetchall()]
            info(f"所有 kline 表: {all_kline_tables}")
    except Exception as e:
        fail(f"查询分区表失败: {e}")


# ═══════════════════════════════════════════════════════════════
#  6. 数据量检查
# ═══════════════════════════════════════════════════════════════
section(f"6. 数据量检查: {SYMBOL}")

if mgr:
    try:
        pool = mgr._get_pool(MARKET)
        with pool.cursor() as cur:
            for table in tables_1d:
                cur.execute(f'SELECT COUNT(*), MIN(time), MAX(time) FROM "{table}" WHERE symbol = %s', (SYMBOL,))
                row = cur.fetchone()
                if row and row[0] > 0:
                    ok(f"{table}: {row[0]} 条, 时间范围: {row[1]} ~ {row[2]}")
                else:
                    warn(f"{table}: 无数据 (symbol={SYMBOL})")

            # 如果上面没数据，检查表里有什么 symbol
            for table in tables_1d:
                cur.execute(f'SELECT DISTINCT symbol FROM "{table}" LIMIT 10')
                syms = [r[0] for r in cur.fetchall()]
                if syms:
                    info(f"{table} 中的 symbol: {syms}")
                else:
                    info(f"{table}: 表为空")
    except Exception as e:
        fail(f"查询数据量失败: {e}")


# ═══════════════════════════════════════════════════════════════
#  7. MarketKlineWriter.query 测试
# ═══════════════════════════════════════════════════════════════
section("7. MarketKlineWriter.query 测试")

if mgr:
    try:
        writer = get_market_kline_writer()
        now = datetime.now(TZ_SH)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
        start = end - timedelta(days=30)

        info(f"查询范围: {start} ~ {end}")
        rows = writer.query(MARKET, SYMBOL, "1D", start_time=start, end_time=end, limit=0)
        if rows:
            ok(f"query 返回 {len(rows)} 条")
            info(f"首条: {rows[0]}")
            info(f"末条: {rows[-1]}")
        else:
            fail("query 返回空列表！")
            info("可能原因: 1) 表中无数据  2) symbol 不匹配  3) 时间范围无覆盖")
    except Exception as e:
        fail(f"query 执行失败: {e}")
        import traceback; traceback.print_exc()


# ═══════════════════════════════════════════════════════════════
#  8. kline_clean MarketDataProvider 测试
# ═══════════════════════════════════════════════════════════════
section("8. kline_clean MarketDataProvider 测试")

try:
    from app.data_sources.kline_clean import MarketDataProvider
    writer = get_market_kline_writer()
    provider = MarketDataProvider(writer)
    ok("MarketDataProvider 初始化成功")

    now = datetime.now(TZ_SH)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    start = end - timedelta(days=30)

    info(f"get_clean_klines({MARKET}, {SYMBOL}, {start}, {end}, '1D')")
    bars = provider.get_clean_klines(MARKET, SYMBOL, start, end, "1D")
    if bars:
        ok(f"get_clean_klines 返回 {len(bars)} 条")
        info(f"首条: time={bars[0]['time']}, open={bars[0]['open']}, close={bars[0]['close']}")
        info(f"末条: time={bars[-1]['time']}, open={bars[-1]['open']}, close={bars[-1]['close']}")
    else:
        fail("get_clean_klines 返回空列表！")
        info("继续排查 _query_raw 和 _fill_gaps ...")

        # 直接测试 _query_raw
        raw = provider._query_raw(MARKET, SYMBOL, "1D", start, end)
        info(f"_query_raw 返回 {len(raw)} 条")
        if raw:
            info(f"首条 raw: {raw[0]}")
            # 检查 time 字段类型
            t = raw[0].get("time")
            info(f"time 类型: {type(t)}, 值: {t}")

        # 测试 _gen_daily
        from app.data_sources.kline_clean import _gen_daily
        expected = _gen_daily(start, end)
        info(f"_gen_daily 生成 {len(expected)} 个期望时间点")
        if expected:
            info(f"首条 expected: {expected[0]} (类型: {type(expected[0])})")

        # 测试 _filter_trading_days
        from app.data_sources.kline_clean import _filter_trading_days
        if raw:
            filtered = _filter_trading_days(raw)
            info(f"_filter_trading_days: {len(raw)} -> {len(filtered)} 条")

        # 测试 _bars_to_dict
        from app.data_sources.kline_clean import _bars_to_dict
        if raw:
            raw_index = _bars_to_dict(raw)
            info(f"_bars_to_dict 索引: {len(raw_index)} 个时间点")
            if expected:
                match_count = sum(1 for t in expected if t in raw_index)
                info(f"expected 与 raw_index 匹配: {match_count}/{len(expected)}")
                if match_count == 0 and raw_index:
                    # 检查时间差
                    sample_raw_key = list(raw_index.keys())[0]
                    sample_exp = expected[0]
                    diff = abs((sample_raw_key - sample_exp).total_seconds())
                    info(f"样本时间差: raw_key={sample_raw_key}, expected={sample_exp}, 差={diff}秒")

except Exception as e:
    fail(f"MarketDataProvider 测试失败: {e}")
    import traceback; traceback.print_exc()


# ═══════════════════════════════════════════════════════════════
#  9. KlineService 完整流程测试
# ═══════════════════════════════════════════════════════════════
section("9. KlineService.get_kline 完整流程测试")

try:
    from app.services.kline import KlineService
    svc = KlineService()
    ok("KlineService 初始化成功")

    info(f"get_kline({MARKET}, {SYMBOL}, '1D', limit=100)")
    result = svc.get_kline(MARKET, SYMBOL, "1D", limit=100)
    if result:
        ok(f"get_kline 返回 {len(result)} 条")
        info(f"首条: time={result[0].get('time')}, open={result[0].get('open')}, close={result[0].get('close')}")
        info(f"末条: time={result[-1].get('time')}, open={result[-1].get('open')}, close={result[-1].get('close')}")

        # 检查是否所有 bar 都一样（过度聚合的特征）
        if len(result) >= 2:
            closes = [b.get("close") for b in result]
            if len(set(closes)) == 1:
                warn("所有 bar 的 close 相同！可能前向填充过度")
            else:
                ok(f"close 值有变化: min={min(closes)}, max={max(closes)}")
    else:
        fail("get_kline 返回空列表！")
except Exception as e:
    fail(f"KlineService 测试失败: {e}")
    import traceback; traceback.print_exc()


# ═══════════════════════════════════════════════════════════════
#  10. 远程 API fallback 测试
# ═══════════════════════════════════════════════════════════════
section("10. 远程 API (DataSourceFactory) 测试")

try:
    from app.data_sources.factory import get_factory
    factory = get_factory()
    ok("DataSourceFactory 初始化成功")

    info(f"fetch_kline_raw({SYMBOL}, '1D', 10, {MARKET})")
    raw = factory.fetch_kline_raw(SYMBOL, "1D", 10, MARKET)
    if raw:
        ok(f"远程 API 返回 {len(raw)} 条")
        info(f"首条: {raw[0]}")
        info(f"末条: {raw[-1]}")
    else:
        warn("远程 API 也返回空（可能是非交易时段或网络问题）")
except Exception as e:
    warn(f"远程 API 测试失败: {e}")


print(f"\n{'='*60}")
print("  诊断完成")
print(f"{'='*60}")
