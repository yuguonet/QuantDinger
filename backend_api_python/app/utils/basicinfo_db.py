"""
basicinfo_db.py — A股全市场股票基本信息读写

═══════════════════════════════════════════════════════════════════════════════
  模块定位
═══════════════════════════════════════════════════════════════════════════════

  本模块负责 A 股全市场股票「基本信息」的持久化存储和查询。

  "基本信息"指：代码、名称、交易所、行业、上市日期、市值、市盈率等——
  是选股、筛选、展示股票列表时需要的元数据，不是 K 线行情数据。

  K 线数据的读写由 db_market.py 的 MarketKlineWriter 负责，
  股票基本信息由本模块负责，两者共用同一个 CNStock_db 库。

═══════════════════════════════════════════════════════════════════════════════
  数据库设计
═══════════════════════════════════════════════════════════════════════════════

  库名: CNStock_db（与 K 线数据共用，由 db_multi.py 的 MarketDBManager 管理）
  表名: stock_basic_info（单表，不分区，~6000 行，查询走主键/索引）

  主键: stock_code（6 位纯数字，如 "600519"）
  索引: market（SH/SZ/BJ）、industry（行业）、status（active/suspended/delisted）

  为什么共用 CNStock_db 而不是独立建库？
    1. 减少运维复杂度：一个市场一个库，所有相关数据集中管理
    2. 复用连接池：MarketDBManager 已经为 CNStock 建好了连接池
    3. 统一生命周期：建库/删库由 MarketDBManager 统一处理

═══════════════════════════════════════════════════════════════════════════════
  连接管理
═══════════════════════════════════════════════════════════════════════════════

  不自建连接池，完全复用 db_market.py 的基础设施：

    db_market.py:
      get_market_db_manager() → MarketDBManager（管理所有市场库的生命周期）
      get_market_kline_writer() → MarketKlineWriter（K 线读写）

    本模块:
      get_stock_basic_db() → StockBasicDB（股票基本信息读写）
      内部通过 mgr._get_pool("CNStock") 获取 MarketPool 连接池

  这样：
    - CNStock_db 的创建/连接池管理全部由 MarketDBManager 负责
    - 本模块只关心 stock_basic_info 表的 CRUD
    - 应用启动时 ensure_market_db("CNStock") 一次性搞定库+K线表+基本信息表

═══════════════════════════════════════════════════════════════════════════════
  数据同步策略
═══════════════════════════════════════════════════════════════════════════════

  sync_from_remote() 做的是"全量代码列表同步"，只拉代码+名称+交易所：
    源1: 东财 push2 API（HTTP，~6000 只，快，优先）
    源2: AkShare stock_info_a_code_name()（兜底）

  行业、市值、市盈率等「详情字段」不全量拉（6000 只逐个取太慢），
  由 enrich_stock_info(code) 按需单只补充。

  UPSERT 冲突策略：
    - symbol 已存在 → 更新名称和交易所（这两个可能变）
    - 详情字段（industry/total_mv 等）→ 只在新值非空/非零时覆盖，
      避免"同步代码列表"时把已有的详情冲掉

═══════════════════════════════════════════════════════════════════════════════
  线程安全说明
═══════════════════════════════════════════════════════════════════════════════

  - get_stock_basic_db() 是线程安全的单例（双重检查锁定）
  - _table_ready 标记：最坏情况多线程同时触发建表，DDL 有 IF NOT EXISTS，安全
  - 所有公开方法（get_stock / upsert_stocks 等）可从多线程并发调用

═══════════════════════════════════════════════════════════════════════════════
  用法示例
═══════════════════════════════════════════════════════════════════════════════

  from app.utils.basicinfo_db import get_stock_basic_db

  db = get_stock_basic_db()

  # 首次使用：确保表存在 + 同步全量
  result = db.sync_from_remote()
  # → {"source": "eastmoney", "fetched": 5300, "inserted": 5300, ...}

  # 查询
  stock = db.get_stock("600519")
  # → {"symbol": "600519", "name": "贵州茅台", "market_cn": "SH", ...}

  all_sz = db.get_all_stocks(market_cn="SZ")
  results = db.search_stocks("茅台")
  count = db.get_stock_count()

  # 按需补充详情
  db.enrich_stock_info("600519")

  # 统计
  stats = db.get_stats()
  # → {"total": 5300, "active": 5280, "by_market": {"SH": 2100, "SZ": 3100, "BJ": 80}, ...}
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# 常量定义
# ═════════════════════════════════════════════════════════════════════════════

# 使用 CNStock_db 库（与 K 线数据共用），由 MarketDBManager 统一管理。
# 不单独建库，减少运维复杂度。
MARKET = "CNStock"

# stock_basic_info 表 DDL。
# 设计原则：
#   - 单表不分区（~6000 行，不需要按年拆分，K 线才需要分区）
#   - 主键 stock_code（6 位纯数字，如 "600519"）
#   - VARCHAR 长度留余量，避免截断
#   - 数值字段用 DOUBLE PRECISION（市值可能超 2^31）
#   - updated_at 自动记录最后写入时间，用于判断数据新鲜度
TABLE_DDL = """
CREATE TABLE IF NOT EXISTS stock_basic_info (
    symbol    VARCHAR(10)  PRIMARY KEY,   -- 6位纯数字代码，如 600519
    name    VARCHAR(50)  NOT NULL,       -- 股票简称，如 "贵州茅台"
    market_cn        VARCHAR(10)  NOT NULL,       -- 交易所：SH(沪) / SZ(深) / BJ(北)
    industry      VARCHAR(50)  DEFAULT '',     -- 所属行业（如 "白酒"），按需补充
    list_date     VARCHAR(20)  DEFAULT '',     -- 上市日期（如 "2001-08-27"），按需补充
    total_mv      DOUBLE PRECISION DEFAULT 0,  -- 总市值（元），按需补充，0 表示未知
    circ_mv       DOUBLE PRECISION DEFAULT 0,  -- 流通市值（元），按需补充
    pe_ratio      DOUBLE PRECISION DEFAULT 0,  -- 市盈率（动态），按需补充
    pb_ratio      DOUBLE PRECISION DEFAULT 0,  -- 市净率，按需补充
    status        VARCHAR(10)  DEFAULT 'active', -- 状态：active/suspended/delisted
    updated_at    TIMESTAMP    DEFAULT NOW()    -- 最后更新时间
)
"""

# 索引 DDL。
# market_cn 索引：按交易所筛选（SH/SZ/BJ）是常见查询
# industry 索引：按行业选股
# status 索引：过滤退市/停牌股票
INDEX_DDLS = [
    "CREATE INDEX IF NOT EXISTS idx_stock_basic_market   ON stock_basic_info (market_cn)",
    "CREATE INDEX IF NOT EXISTS idx_stock_basic_industry ON stock_basic_info (industry)",
    "CREATE INDEX IF NOT EXISTS idx_stock_basic_status   ON stock_basic_info (status)",
]


# ═════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════════════════════════════════

def _detect_market(code: str) -> str:
    """
    根据 6 位数字代码推断所属交易所。

    规则（与 normalizer.py 的 detect_market 一致）：
      SH(沪市): 600/601/603/605（主板）, 688/689（科创板）, 900（B股）
      SZ(深市): 000/001/002/003（主板）, 300/301（创业板）, 200（B股）
      BJ(北证): 43/82/83/87/88

    Args:
        code: 6 位纯数字字符串

    Returns:
        "SH" / "SZ" / "BJ"，无法识别返回 ""
    """
    c = (code or "").strip()
    if not c.isdigit() or len(c) != 6:
        return ""
    # 沪市主板 + 科创板 + B股
    if c.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    # 深市主板 + 创业板 + B股
    if c.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "SZ"
    # 北交所
    if c.startswith(("43", "82", "83", "87", "88")):
        return "BJ"
    return ""


# ═════════════════════════════════════════════════════════════════════════════
# StockBasicDB — 核心读写类
# ═════════════════════════════════════════════════════════════════════════════

class StockBasicDB:
    """
    A 股股票基本信息读写器。

    管理 CNStock_db 库中 stock_basic_info 表的完整生命周期：
      - 建表（ensure_table）
      - 批量写入/更新（upsert_stocks）
      - 查询/搜索（get_stock / get_all_stocks / search_stocks）
      - 远程同步（sync_from_remote）
      - 详情补充（enrich_stock_info）

    连接池复用 db_market.py 的 MarketDBManager，不自建连接。
    所有方法都是线程安全的，可从多个协程/线程并发调用。
    """

    def __init__(self):
        # MarketDBManager 实例（惰性获取，来自 db_market.py 的全局单例）
        self._mgr = None
        # 建表就绪标记。True 表示 stock_basic_info 表已确认存在，
        # 后续操作跳过建表检查（减少 information_schema 查询）
        self._table_ready = False

    def _get_mgr(self):
        """
        惰性获取 MarketDBManager 实例。

        MarketDBManager 是 db_market.py 管理的全局单例，
        负责所有市场库（CNStock_db / USStock_db / ...）的生命周期。
        这里复用它，不重复创建。
        """
        if self._mgr is None:
            from app.utils.db_market import get_market_db_manager
            self._mgr = get_market_db_manager()
        return self._mgr

    def _get_pool(self):
        """
        获取 CNStock_db 的连接池。

        MarketDBManager._get_pool("CNStock") 返回 MarketPool 实例，
        内部是 ThreadedConnectionPool（线程安全连接池）。
        首次调用时 MarketDBManager 会自动创建 CNStock_db 库（如果不存在）。
        """
        return self._get_mgr()._get_pool(MARKET)

    # ────────────────────────────────────────────────────────────────────
    # 表生命周期
    # ────────────────────────────────────────────────────────────────────

    def ensure_table(self):
        """
        确保 stock_basic_info 表存在（幂等）。

        使用 CREATE TABLE / INDEX IF NOT EXISTS，重复调用不会报错。
        _table_ready 标记用于快速跳过：确认过一次后，后续不再执行 DDL。
        （最坏情况：多线程同时触发，DDL 有 IF NOT EXISTS 保护，安全）

        依赖：
          MarketDBManager 已经创建了 CNStock_db 库（由 db_market 的
          ensure_market_db("CNStock") 负责），这里只建本模块的表。
        """
        if self._table_ready:
            return
        # 确保 CNStock_db 库存在（MarketDBManager 内部有缓存，不会重复查）
        self._get_mgr().ensure_market_db(MARKET)
        pool = self._get_pool()
        with pool.cursor() as cur:
            # 建表
            cur.execute(TABLE_DDL)
            # 建索引（逐条执行，单条失败不影响其他）
            for ddl in INDEX_DDLS:
                cur.execute(ddl)
        self._table_ready = True
        logger.info(f"✅ stock_basic_info 表就绪（{MARKET}_db）")

    # ────────────────────────────────────────────────────────────────────
    # 写入接口
    # ────────────────────────────────────────────────────────────────────

    def upsert_stocks(self, stocks: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        批量写入/更新股票信息（UPSERT）。

        ── 写入策略 ──

        使用 psycopg2.extras.execute_values 进行批量 INSERT：
          - 比逐条 execute 快 5-10 倍（减少网络往返）
          - page_size=1000：每 1000 条一批发送给 PostgreSQL

        ON CONFLICT (symbol) DO UPDATE：主键冲突时走更新逻辑。
        更新策略是"非空覆盖"：
          - name / market：直接覆盖（名称和交易所可能变更）
          - industry / list_date：新值非空时覆盖，否则保留旧值
          - total_mv / circ_mv / pe_ratio / pb_ratio：新值非零时覆盖
          - status / updated_at：直接覆盖

        这样设计的原因：
          sync_from_remote() 只拉代码+名称，详情字段为空/零。
          如果直接覆盖，会把之前 enrich_stock_info() 补充的详情冲掉。
          用 COALESCE(NULLIF(...)) 实现"有值才覆盖"。

        ── 容错策略 ──

        如果整批 execute_values 失败（比如某条数据类型不对）：
          1. conn.rollback() 清除 aborted 事务状态
          2. _retry_upsert_split() 将列表拆成两半递归重试
          3. 拆到只剩 1 条时逐条 INSERT，失败则计为 error

        为什么拆半而不是直接逐条？
          6000 条逐条 INSERT 很慢。拆半能快速隔离出问题数据，
          正常数据仍走批量，最坏情况（每批都有脏数据）也只需 ~13 层递归。

        ── Args ──
            stocks: 股票信息列表，每条至少包含 symbol 和 name
                    可选字段: market_cn, industry, list_date, total_mv, circ_mv,
                              pe_ratio, pb_ratio, status

        ── Returns ──
            {
                "inserted": int,   -- 成功写入/更新的条数
                "updated": int,    -- （预留，当前不区分 insert/update）
                "errors": int,     -- 失败条数（含无效输入 + SQL 异常）
                "total": int,      -- 输入总条数
            }
        """
        # 空列表快速返回，避免不必要的 DB 操作
        if not stocks:
            return {"inserted": 0, "updated": 0, "errors": 0, "total": 0}

        # 确保表存在（惰性，只在首次调用时执行 DDL）
        self.ensure_table()
        now = datetime.now()

        # ── 预处理：过滤无效条目，转为 tuple 列表 ──
        # 为什么预处理？
        #   1. 提前过滤无效数据（空代码/空名称/无法识别交易所），减少 SQL 异常
        #   2. 统一字段类型（float 转换），避免 psycopg2 类型推断问题
        #   3. 预处理后的 tuple 列表可直接传给 execute_values
        valid_rows = []
        skipped = 0
        for item in stocks:
            code = (item.get("symbol") or "").strip()
            name = (item.get("name") or "").strip()
            # 必须有代码和名称
            if not code or not name:
                skipped += 1
                continue
            # 必须能识别交易所（可以从输入取，也可以从代码推断）
            market_cn = item.get("market_cn") or _detect_market(code)
            if not market_cn:
                skipped += 1
                continue
            # 组装 tuple，顺序与 INSERT 列顺序一致
            valid_rows.append((
                code,                            # symbol
                name,                            # name
                market_cn,                          # market_cn
                item.get("industry", ""),        # industry（默认空串）
                item.get("list_date", ""),       # list_date（默认空串）
                float(item.get("total_mv", 0) or 0),   # total_mv（None → 0）
                float(item.get("circ_mv", 0) or 0),    # circ_mv
                float(item.get("pe_ratio", 0) or 0),   # pe_ratio
                float(item.get("pb_ratio", 0) or 0),   # pb_ratio
                item.get("status", "active"),           # status
                now,                                    # updated_at
            ))

        # 全部无效 → 直接返回
        if not valid_rows:
            return {"inserted": 0, "updated": 0, "errors": skipped, "total": len(stocks)}

        # ── 批量 UPSERT SQL ──
        # VALUES %s 是 execute_values 的占位符，运行时会被展开为
        # VALUES (...), (...), (...) 的形式
        from psycopg2.extras import execute_values

        sql = """
            INSERT INTO stock_basic_info
                (symbol, name, market_cn, industry, list_date,
                 total_mv, circ_mv, pe_ratio, pb_ratio, status, updated_at)
            VALUES %s
            ON CONFLICT (symbol) DO UPDATE SET
                -- 名称和交易所：直接覆盖（可能变更，如 ST 摘帽/更名）
                name = EXCLUDED.name,
                market_cn     = EXCLUDED.market_cn,
                -- 详情字段：只在新值有意义时覆盖，否则保留 DB 中已有的值
                -- COALESCE(NULLIF(new, ''), old) 含义：
                --   如果 new 是空串 → NULLIF 返回 NULL → COALESCE 返回 old
                --   如果 new 非空   → NULLIF 返回 new → COALESCE 返回 new
                industry   = COALESCE(NULLIF(EXCLUDED.industry, ''), stock_basic_info.industry),
                list_date  = COALESCE(NULLIF(EXCLUDED.list_date, ''), stock_basic_info.list_date),
                -- 数值字段：只在新值 > 0 时覆盖（0 表示"未知"，不覆盖已知值）
                total_mv   = CASE WHEN EXCLUDED.total_mv > 0 THEN EXCLUDED.total_mv ELSE stock_basic_info.total_mv END,
                circ_mv    = CASE WHEN EXCLUDED.circ_mv  > 0 THEN EXCLUDED.circ_mv  ELSE stock_basic_info.circ_mv  END,
                -- pe_ratio/pb_ratio 可能为负数（亏损股），所以用 != 0 判断
                pe_ratio   = CASE WHEN EXCLUDED.pe_ratio != 0 THEN EXCLUDED.pe_ratio ELSE stock_basic_info.pe_ratio END,
                pb_ratio   = CASE WHEN EXCLUDED.pb_ratio != 0 THEN EXCLUDED.pb_ratio ELSE stock_basic_info.pb_ratio END,
                -- 状态和时间：直接覆盖
                status     = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at
        """

        total_ok = 0
        total_err = 0

        # 从连接池借连接，with 块结束自动归还
        pool = self._get_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            try:
                # ── 快速路径：整批一次性写入 ──
                # page_size=1000：每 1000 条一批发给 PostgreSQL，
                # 平衡内存占用和网络往返次数
                execute_values(cur, sql, valid_rows, page_size=1000)
                total_ok = cur.rowcount
                conn.commit()
            except Exception as e:
                # ── 整批失败：回滚后拆半重试 ──
                # 回滚清除了 aborted 事务状态，后续 SQL 可以正常执行
                conn.rollback()
                logger.warning(f"批量写入失败，拆半重试: {e}")
                total_ok, total_err = self._retry_upsert_split(conn, cur, sql, valid_rows)
                conn.commit()
            cur.close()

        result = {
            "inserted": total_ok,
            "updated": 0,  # UPSERT 不区分 insert/update，统一计为 inserted
            "errors": total_err + skipped,  # SQL 错误 + 预处理跳过
            "total": len(stocks),
        }
        logger.info(
            f"upsert_stocks: 总计={len(stocks)} 有效={len(valid_rows)} "
            f"写入={total_ok} 错误={total_err + skipped}"
        )
        return result

    @staticmethod
    def _retry_upsert_split(conn, cur, sql, rows):
        """
        批量 UPSERT 失败时的拆半重试策略。

        ── 算法 ──

        1. 如果 rows 只有 1 条：
           - rollback（清除 aborted 状态）
           - 逐条 INSERT（page_size=1）
           - 成功返回 (1, 0)，失败返回 (0, 1)

        2. 如果 rows 有多条：
           - 拆成两半，递归调用自身
           - 合并两半的结果 (ok1+ok2, fail1+fail2)

        ── 为什么拆半？ ──

        假设 6000 条中有 1 条脏数据导致整批失败：
          - 直接逐条：6000 次 execute + commit，很慢
          - 拆半重试：先试 3000 条（成功）+ 再试 3000 条（失败）
            → 拆成 1500（成功）+ 1500（失败）→ ... → 定位到那 1 条
          - 总共约 13 层递归，大部分在前几层就批量成功了

        ── 事务安全 ──

        每次递归入口都先 conn.rollback()，确保事务状态干净。
        PostgreSQL 事务中一条 SQL 失败后，所有后续 SQL 都会报错
        "current transaction is aborted"，必须先 rollback 才能继续。

        ── Args ──
            conn: psycopg2 连接对象
            cur:  游标对象
            sql:  UPSERT SQL 模板（带 VALUES %s 占位符）
            rows: 要写入的 tuple 列表

        ── Returns ──
            (ok_count, fail_count)
        """
        from psycopg2.extras import execute_values

        if len(rows) <= 1:
            # 只剩 1 条：逐条处理
            try:
                conn.rollback()  # 清除 aborted 事务状态
                execute_values(cur, sql, rows, page_size=1)
                return (cur.rowcount, 0)
            except Exception:
                conn.rollback()  # 单条也失败，回滚后返回
                return (0, 1)

        # 多条：拆半递归
        mid = len(rows) // 2
        ok1, fail1 = StockBasicDB._retry_upsert_split(conn, cur, sql, rows[:mid])
        ok2, fail2 = StockBasicDB._retry_upsert_split(conn, cur, sql, rows[mid:])
        return (ok1 + ok2, fail1 + fail2)

    # ────────────────────────────────────────────────────────────────────
    # 读取接口
    # ────────────────────────────────────────────────────────────────────

    def get_stock(self, code: str) -> Optional[Dict[str, Any]]:
        """
        查询单只股票的基本信息。

        Args:
            code: 6 位数字代码（如 "600519"），自动 strip

        Returns:
            字典（见 _row_to_dict），未找到返回 None
        """
        self.ensure_table()
        code = (code or "").strip()
        if not code:
            return None

        pool = self._get_pool()
        with pool.cursor() as cur:
            cur.execute(
                "SELECT symbol, name, market_cn, industry, list_date, "
                "       total_mv, circ_mv, pe_ratio, pb_ratio, status, updated_at "
                "FROM stock_basic_info WHERE symbol = %s",
                (code,),
            )
            row = cur.fetchone()

        if not row:
            return None
        return self._row_to_dict(row)

    def get_all_stocks(
        self,
        market_cn: str = None,
        status: str = "active",
    ) -> List[Dict[str, Any]]:
        """
        查询全部股票（可按交易所和状态过滤）。

        Args:
            market_cn: 过滤交易所 "SH"/"SZ"/"BJ"，None 返回全部
            status: 过滤状态，默认 "active"（只返回正常上市股票），
                    传 None 不过滤（含停牌/退市）

        Returns:
            股票列表，按 symbol 升序排列
        """
        self.ensure_table()

        # 动态拼接 WHERE 条件（参数化，无 SQL 注入风险）
        conditions = []
        params = []
        if market_cn:
            conditions.append("market_cn = %s")
            params.append(market_cn.upper())  # 统一大写，避免 "sh" 匹配不到
        if status:
            conditions.append("status = %s")
            params.append(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        pool = self._get_pool()
        with pool.cursor() as cur:
            cur.execute(
                f"SELECT symbol, name, market_cn, industry, list_date, "
                f"       total_mv, circ_mv, pe_ratio, pb_ratio, status, updated_at "
                f"FROM stock_basic_info {where} ORDER BY symbol",
                params,
            )
            rows = cur.fetchall()

        return [self._row_to_dict(r) for r in rows]

    def search_stocks(self, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        按代码或名称模糊搜索股票。

        使用 LIKE '%keyword%' 模式匹配，同时搜索 symbol 和 stock_name。
        只返回 status='active' 的正常上市股票。

        Args:
            keyword: 搜索关键词（如 "茅台"、"600"、"银行"）
            limit:   最大返回条数，默认 20

        Returns:
            匹配的股票列表，按 symbol 升序
        """
        self.ensure_table()

        # 构造 LIKE 模式：前后加 % 通配符
        kw = f"%{(keyword or '').strip()}%"
        # 空关键词或只有通配符 → 不搜索（避免全表扫描）
        if not kw or kw == "%%":
            return []

        pool = self._get_pool()
        with pool.cursor() as cur:
            cur.execute(
                "SELECT symbol, name, market_cn, industry, list_date, "
                "       total_mv, circ_mv, pe_ratio, pb_ratio, status, updated_at "
                "FROM stock_basic_info "
                "WHERE (symbol LIKE %s OR name LIKE %s) AND status = 'active' "
                "ORDER BY symbol LIMIT %s",
                (kw, kw, limit),
            )
            rows = cur.fetchall()

        return [self._row_to_dict(r) for r in rows]

    def market_all_string(self, status: str = "active") -> str:
        """
        获取全市场所有股票代码，加上小写交易所前缀，以逗号拼接返回。

        格式: "sh600519,sz000001,sh601398,..."

        前缀规则:
            SH → sh, SZ → sz, BJ → bj

        Args:
            status: 过滤状态，默认 "active"（只返回正常上市股票），
                    传 None 不过滤

        Returns:
            逗号分隔的股票代码字符串，无数据返回空串 ""
        """
        self.ensure_table()

        pool = self._get_pool()
        if status:
            with pool.cursor() as cur:
                cur.execute(
                    "SELECT symbol, market_cn FROM stock_basic_info "
                    "WHERE status = %s ORDER BY symbol",
                    (status,),
                )
                rows = cur.fetchall()
        else:
            with pool.cursor() as cur:
                cur.execute(
                    "SELECT symbol, market_cn FROM stock_basic_info ORDER BY symbol"
                )
                rows = cur.fetchall()

        parts = []
        for symbol, market_cn in rows:
            prefix = (market_cn or "").strip().lower()
            if prefix:
                parts.append(f"{prefix}{symbol}")

        return ",".join(parts)

    def market_all_codes(self, status: str = "active") -> List[str]:
        """
        获取全市场所有股票代码（纯 6 位数字，无交易所前缀）。

        Args:
            status: 过滤状态，默认 "active"，传 None 不过滤

        Returns:
            代码列表，如 ["000001", "000002", ..., "688001"]
        """
        self.ensure_table()

        pool = self._get_pool()
        if status:
            with pool.cursor() as cur:
                cur.execute(
                    "SELECT symbol FROM stock_basic_info "
                    "WHERE status = %s ORDER BY symbol",
                    (status,),
                )
                return [r[0] for r in cur.fetchall()]
        else:
            with pool.cursor() as cur:
                cur.execute(
                    "SELECT symbol FROM stock_basic_info ORDER BY symbol"
                )
                return [r[0] for r in cur.fetchall()]

    def get_stock_count(self, market_cn: str = None) -> int:
        """
        获取股票总数（只计 active 状态）。

        Args:
            market_cn: 按交易所过滤，None 返回全市场总数

        Returns:
            股票数量
        """
        self.ensure_table()
        pool = self._get_pool()
        if market_cn:
            with pool.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM stock_basic_info "
                    "WHERE market_cn = %s AND status = 'active'",
                    (market_cn.upper(),),
                )
                return cur.fetchone()[0]
        else:
            with pool.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM stock_basic_info WHERE status = 'active'"
                )
                return cur.fetchone()[0]

    def get_industries(self) -> List[str]:
        """
        获取所有不重复的行业列表（去重、排序）。

        用途：行业筛选下拉框、行业分布统计等。
        只返回 active 股票的行业，排除空串。
        """
        self.ensure_table()
        pool = self._get_pool()
        with pool.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT industry FROM stock_basic_info "
                "WHERE industry != '' AND status = 'active' ORDER BY industry"
            )
            return [r[0] for r in cur.fetchall()]

    def get_stats(self) -> Dict[str, Any]:
        """
        获取 stock_basic_info 表的统计信息。

        用途：运维监控、数据质量检查、前端展示"数据概览"。

        Returns:
            {
                "market_cn": str,          -- 所属市场 "CNStock"
                "db_name": str,         -- 数据库名 "CNStock_db"
                "total": int,           -- 总记录数（含 active + 非 active）
                "active": int,          -- active 状态的记录数
                "by_market": dict,      -- 按交易所分布 {"SH": 2100, "SZ": 3100, ...}
                "last_update": str,     -- 最后更新时间（ISO 格式），无数据返回 None
            }
        """
        self.ensure_table()
        pool = self._get_pool()
        with pool.cursor() as cur:
            # 总记录数
            cur.execute("SELECT COUNT(*) FROM stock_basic_info")
            total = cur.fetchone()[0]
            # active 记录数
            cur.execute(
                "SELECT COUNT(*) FROM stock_basic_info WHERE status = 'active'"
            )
            active = cur.fetchone()[0]
            # 按交易所分组统计
            cur.execute(
                "SELECT market_cn, COUNT(*) FROM stock_basic_info "
                "WHERE status = 'active' GROUP BY market_cn ORDER BY market_cn"
            )
            by_market = {r[0]: r[1] for r in cur.fetchall()}
            # 最后更新时间
            cur.execute("SELECT MAX(updated_at) FROM stock_basic_info")
            last_update = cur.fetchone()[0]

        from app.utils.db_multi import _market_db_name
        return {
            "market_cn": MARKET,
            "db_name": _market_db_name(MARKET),
            "total": total,
            "active": active,
            "by_market": by_market,
            "last_update": last_update.isoformat() if last_update else None,
        }

    # ────────────────────────────────────────────────────────────────────
    # 远程同步（多源 HTTP 下载）
    # ────────────────────────────────────────────────────────────────────

    def sync_from_remote(self) -> Dict[str, Any]:
        """
        从远程数据源同步全量 A 股股票列表到 CNStock_db。

        ── 同步范围 ──

        只同步「代码 + 名称 + 交易所」这三个基础字段。
        行业、市值、市盈率等详情字段不在此同步（太慢），
        由 enrich_stock_info() 按需单只补充。

        ── 数据源 fallback 链 ──

        源1: 东财 push2 API（优先）
          - 接口: https://push2.eastmoney.com/api/qt/clist/get
          - 优势: 纯 HTTP，不需要额外依赖，速度快
          - 字段映射: f12→stock_code, f14→stock_name
          - 过滤条件: fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048
            （A 股主板 + 创业板 + 科创板 + 北交所）

        源2: AkShare（兜底）
          - 接口: akshare.stock_info_a_code_name()
          - 优势: 数据全面，作为东财的 fallback
          - 依赖: 需要 pip install akshare

        切换逻辑：
          东财返回空或不足 100 条 → 切换到 AkShare
          两个都失败 → 返回 {"source": "none", "fetched": 0}

        ── Returns ──
            {
                "source": str,      -- 数据源名称 "eastmoney" / "akshare" / "none"
                "fetched": int,     -- 远程拉取到的条数
                "inserted": int,    -- 成功写入 DB 的条数
                "errors": int,      -- 失败条数
                "total": int,       -- 输入总条数
            }
        """
        # 确保 CNStock_db 库 + stock_basic_info 表都存在
        self.ensure_table()

        # ── 源1: 东财 ──
        stocks = self._fetch_eastmoney()
        source = "eastmoney"

        # ── 源2: AkShare fallback ──
        # 东财失败或数据量异常少（< 100）时切换
        if not stocks or len(stocks) < 100:
            stocks = self._fetch_akshare()
            source = "akshare"

        # 两个源都失败
        if not stocks:
            logger.error("所有远程数据源均失败，无法同步股票列表")
            return {"source": "none", "fetched": 0, "upserted": {}}

        # 批量写入 DB（这些是在市股票，status=active）
        logger.info(f"[同步] {source} 获取 {len(stocks)} 只股票，开始写入...")
        result = self.upsert_stocks(stocks)
        result["source"] = source
        result["fetched"] = len(stocks)

        # ── 停牌/退市检测 ──
        # 东财 clist/get 只返回当前有交易的股票，停牌/退市的不会出现。
        # 因此 DB 中原来 status='active' 但不在本次拉取列表里的，
        # 说明已经停牌或退市，需要标记为 'suspended'。
        fetched_codes = {s["symbol"] for s in stocks}
        suspended_count = self._mark_missing_as_suspended(fetched_codes)
        result["suspended"] = suspended_count

        logger.info(f"[同步] 完成: 源={source} 获取={len(stocks)} "
                    f"写入结果={result} 标记停牌/退市={suspended_count}")
        return result

    def _mark_missing_as_suspended(self, fetched_codes: set) -> int:
        """
        将 DB 中 status='active' 但不在 fetched_codes 里的股票标记为 'suspended'。

        原理：东财 clist/get 只返回当前可交易的股票。
        停牌、退市、暂停上市的股票不会出现在返回列表中。
        因此，如果一只股票 DB 里是 active 但本次拉取没出现，说明它已停牌或退市。

        ── Args ──
            fetched_codes: 本次从远程拉取到的所有股票代码集合

        ── Returns ──
            被标记为 suspended 的股票数量
        """
        if not fetched_codes:
            return 0

        pool = self._get_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            try:
                # 先查出所有 active 但不在本次拉取列表中的股票代码
                # 用 NOT IN + 批量参数避免一次性传太多
                # 分批处理，每批 2000 个 code（避免 SQL 参数过多）
                all_active = []
                cur.execute(
                    "SELECT symbol FROM stock_basic_info WHERE status = 'active'"
                )
                all_active = [r[0] for r in cur.fetchall()]

                to_suspend = [c for c in all_active if c not in fetched_codes]

                if not to_suspend:
                    return 0

                # 批量更新（分批，每批 2000）
                total_suspended = 0
                for i in range(0, len(to_suspend), 2000):
                    batch = to_suspend[i:i + 2000]
                    cur.execute(
                        "UPDATE stock_basic_info "
                        "SET status = 'suspended', updated_at = NOW() "
                        "WHERE status = 'active' AND symbol = ANY(%s)",
                        (batch,),
                    )
                    total_suspended += cur.rowcount

                conn.commit()
                if total_suspended > 0:
                    logger.info(f"[停牌检测] 标记 {total_suspended} 只股票为 suspended")
                return total_suspended
            except Exception as e:
                conn.rollback()
                logger.warning(f"[停牌检测] 标记 suspended 失败: {e}")
                return 0

    def _fetch_eastmoney(self) -> List[Dict[str, Any]]:
        """
        从东财拉取全量 A 股代码列表。

        ── API 说明 ──

        接口: GET https://push2.eastmoney.com/api/qt/clist/get
        参数:
          pn=1      页码（第 1 页）
          pz=8000   每页条数（8000 足够覆盖全 A 股 ~5300 只）
          fs=...    过滤条件（A 股全板块）
          fields=f12,f14  只取代码和名称（减少传输量）

        响应结构:
          {"data": {"diff": [{"f12": "600519", "f14": "贵州茅台"}, ...]}}

        ── 限流 ──

        调用前通过 get_eastmoney_limiter().wait() 排队，
        避免并发请求触发东财反爬。

        ── Returns ──
            [{"symbol": "600519", "name": "贵州茅台", "market_cn": "SH"}, ...]
            失败返回 []
        """
        try:
            import requests
            from app.data_sources.rate_limiter import get_eastmoney_limiter, get_request_headers

            # 排队等待（限流器内部处理间隔）
            limiter = get_eastmoney_limiter()
            limiter.wait()

            resp = requests.get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                headers=get_request_headers(referer="https://quote.eastmoney.com/"),
                params={
                    "pn": 1,           # 第 1 页
                    "pz": 8000,        # 每页 8000 条（覆盖全 A 股）
                    "po": 1,           # 排序方向
                    "np": 1,           # ?
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",  # 东财固定 token
                    "fltt": 2,         # 浮点数精度
                    "invt": 2,         # ?
                    "fid": "f3",       # 排序字段（涨跌幅）
                    # fs 过滤条件：A 股全板块
                    #   m:0+t:6   沪市主板
                    #   m:0+t:80  沪市科创板
                    #   m:1+t:2   深市主板
                    #   m:1+t:23  深市创业板
                    #   m:0+t:81+s:2048  北交所
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                    "fields": "f12,f14",  # f12=代码, f14=名称
                },
                timeout=20,  # 20 秒超时（全量数据可能稍慢）
            )
            data = resp.json()
            # 响应路径: data → diff → [items...]
            items = ((data or {}).get("data") or {}).get("diff")
            if not items:
                return []

            result = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("f12", "")).strip()
                name = str(item.get("f14", "")).strip()
                # 过滤无效数据：必须是 6 位纯数字
                if code and name and len(code) == 6 and code.isdigit():
                    result.append({
                        "symbol": code,
                        "name": name,
                        "market_cn": _detect_market(code),  # 从代码推断交易所
                    })

            logger.info(f"[东财] 获取 A 股列表: {len(result)} 只")
            return result
        except Exception as e:
            logger.warning(f"[东财] 获取 A 股列表失败: {e}")
            return []

    def _fetch_akshare(self) -> List[Dict[str, Any]]:
        """
        从 AkShare 拉取全量 A 股代码列表（兜底源）。

        AkShare 是开源金融数据接口库，底层也是爬取东财/新浪等。
        stock_info_a_code_name() 返回 DataFrame，含 code 和 name 两列。

        ── 限流 ──

        调用前通过 get_akshare_limiter().wait() 排队。
        AkShare 底层会发 HTTP 请求，需要限流避免被封。

        ── Returns ──
            [{"symbol": "600519", "name": "贵州茅台", "market_cn": "SH"}, ...]
            失败返回 []
        """
        try:
            import akshare as ak
            from app.data_sources.rate_limiter import get_akshare_limiter

            get_akshare_limiter().wait()
            df = ak.stock_info_a_code_name()
            if df is None or df.empty:
                return []

            result = []
            for _, row in df.iterrows():
                code = str(row.get("code", "")).strip()
                name = str(row.get("name", "")).strip()
                # 过滤无效数据
                if code and name and len(code) == 6 and code.isdigit():
                    result.append({
                        "symbol": code,
                        "name": name,
                        "market_cn": _detect_market(code),
                    })

            logger.info(f"[AkShare] 获取 A 股列表: {len(result)} 只")
            return result
        except Exception as e:
            logger.warning(f"[AkShare] 获取 A 股列表失败: {e}")
            return []

    # ────────────────────────────────────────────────────────────────────
    # 详情补充（按需单只，非全量同步）
    # ────────────────────────────────────────────────────────────────────

    def enrich_stock_info(self, code: str) -> Optional[Dict[str, Any]]:
        """
        补充单只股票的详情字段（行业、市值、市盈率等）。

        ── 设计思路 ──

        sync_from_remote() 只拉代码+名称，详情字段为空/零。
        需要详情时（比如用户点开某只股票），调用此方法按需补充。

        数据来源：AStockDataSource.get_stock_info()
          - 内部走 AkShare → 东财 fallback 链
          - 返回: name, industry, listed_date, total_mv, circ_mv, pe_ratio, pb_ratio

        写入策略：UPDATE + 非空覆盖（与 upsert_stocks 一致）
          - 行业/上市日期：新值非空时覆盖
          - 市值/市盈率：新值非零时覆盖

        ── 适用场景 ──

        - 用户查看某只股票详情时触发
        - 选股结果需要行业/市值信息时批量触发
        - 定时任务逐只补充（注意限流，避免请求过快）

        ── Args ──
            code: 6 位数字代码（如 "600519"）

        ── Returns ──
            更新后的完整记录字典，失败返回 None
        """
        # 延迟导入：避免循环依赖（a_stock.py → basicinfo_db.py 的潜在路径）
        from app.data_sources.a_stock import AStockDataSource
        ds = AStockDataSource()
        info = ds.get_stock_info(code)
        if not info:
            return None

        self.ensure_table()
        now = datetime.now()

        pool = self._get_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            # UPDATE 而非 INSERT：记录必须已存在（sync_from_remote 已写入基础信息）
            # COALESCE(NULLIF(new, ''), old)：空串不覆盖
            # CASE WHEN new > 0 THEN new ELSE old END：零值不覆盖
            cur.execute("""
                UPDATE stock_basic_info SET
                    industry   = COALESCE(NULLIF(%s, ''), industry),
                    list_date  = COALESCE(NULLIF(%s, ''), list_date),
                    total_mv   = CASE WHEN %s > 0 THEN %s ELSE total_mv END,
                    circ_mv    = CASE WHEN %s > 0 THEN %s ELSE circ_mv  END,
                    pe_ratio   = CASE WHEN %s != 0 THEN %s ELSE pe_ratio END,
                    pb_ratio   = CASE WHEN %s != 0 THEN %s ELSE pb_ratio END,
                    updated_at = %s
                WHERE symbol = %s
            """, (
                info.get("industry", ""),       # 新行业
                info.get("listed_date", ""),     # 新上市日期
                float(info.get("total_mv", 0) or 0), float(info.get("total_mv", 0) or 0),
                float(info.get("circ_mv", 0) or 0),  float(info.get("circ_mv", 0) or 0),
                float(info.get("pe_ratio", 0) or 0),  float(info.get("pe_ratio", 0) or 0),
                float(info.get("pb_ratio", 0) or 0),  float(info.get("pb_ratio", 0) or 0),
                now,                                   # updated_at
                code,                                  # WHERE 条件
            ))
            conn.commit()
            cur.close()

        # 返回更新后的完整记录
        return self.get_stock(code)

    # ────────────────────────────────────────────────────────────────────
    # 内部工具
    # ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """
        将数据库行 tuple 转为字典。

        列顺序与 SELECT 语句一致：
          0: symbol, 1: name, 2: market_cn, 3: industry,
          4: list_date,  5: total_mv,   6: circ_mv, 7: pe_ratio,
          8: pb_ratio,   9: status,     10: updated_at

        注意：float(row[x] or 0) 处理 NULL → 0.0 的转换。
        """
        return {
            "symbol": row[0],
            "name": row[1],
            "market_cn":     row[2],
            "industry":   row[3],
            "list_date":  row[4],
            "total_mv":   float(row[5] or 0),
            "circ_mv":    float(row[6] or 0),
            "pe_ratio":   float(row[7] or 0),
            "pb_ratio":   float(row[8] or 0),
            "status":     row[9],
            "updated_at": row[10].isoformat() if row[10] else None,
        }

    def close(self):
        """
        关闭连接（本模块不持有独立连接池，此方法为兼容接口）。

        实际连接池由 MarketDBManager 管理，调用
        get_market_db_manager().close_pool("CNStock") 关闭。
        """
        pass


# ═════════════════════════════════════════════════════════════════════════════
# 全局单例
# ═════════════════════════════════════════════════════════════════════════════

# 模块级单例，整个进程共享同一个 StockBasicDB 实例。
# 为什么用单例？
#   1. _table_ready 缓存：只查一次 information_schema，后续跳过
#   2. MarketDBManager 复用：内部获取的是 db_market.py 的全局单例
_instance: Optional[StockBasicDB] = None
_instance_lock = threading.Lock()


def get_stock_basic_db() -> StockBasicDB:
    """
    获取全局 StockBasicDB 单例（线程安全）。

    使用双重检查锁定（DCLP）：
      - 快速路径：_instance 已存在 → 直接返回（无锁）
      - 慢速路径：获取锁后再检查 → 创建实例

    所有调用方统一通过此函数获取实例，不要直接 StockBasicDB()。
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            # 二次检查：可能在等待锁期间已被其他线程创建
            if _instance is None:
                _instance = StockBasicDB()
    return _instance
