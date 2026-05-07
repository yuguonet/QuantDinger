"""
backfill_db.py — 全盘批量回填 15m / 1D K 线到 MongoDB

═══════════════════════════════════════════════════════════════
  核心职责: 全盘全量下载最近 N 根 K 线，少量 HTTP 写入 DB
═══════════════════════════════════════════════════════════════

决策依据: cn_last_update 表
  不靠猜时间，靠查表:
    该不该干 → 查 last_updated，和当前时间比差距
    干了什么  → 每次写完记录 tf / count / 写入条数
    干得怎样  → 记录 status + report（成功/失败/异常信息）

设计原则:
  1. 只做全盘回填，不做单只回填
  2. 从当前时间倒推计算 count，不按日期遍历
  3. 每个数据源只需 1 次 HTTP 请求
  4. 先删后写，保证数据干净

数据流:
  查 cn_last_update → 判断是否需要回填 → 删旧数据 → HTTP 拉取 → bulk_write → 写 cn_last_update
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from app.utils.trading_calendar import is_trading_day, prev_trading_day

MONGO_URI = os.getenv("MONGO_URI", "mongodb://root:123456@mongo:27017")
DB_NAME = "quantData"
client = AsyncIOMotorClient(MONGO_URI, tls=True, tlsCAFile=certifi.where())
db = client[DB_NAME]

API_KEY = os.getenv("DINGER_API_KEY", "")
BASE_URL = "https://api.quantdinger.com/v1"

MAX_CONCURRENCY = 5
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 1.5

TZ_CN = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)


def _same_trading_day(dt1: datetime, dt2: datetime) -> bool:
    """判断两个时间点是否在同一个交易日。使用交易日历，精确处理节假日。"""
    d1 = dt1.strftime("%Y-%m-%d")
    d2 = dt2.strftime("%Y-%m-%d")

    # 同一天自然同交易日
    if d1 == d2:
        return True

    # 不同天 → 各自找到所属的交易日，看是否同一个
    def _own_trading_day(d: str) -> str:
        if is_trading_day(d):
            return d
        # 非交易日 → 归到前一个交易日
        return prev_trading_day(d)

    return _own_trading_day(d1) == _own_trading_day(d2)


# ================================================================
# cn_last_update 表 — 回填的唯一控制机制
# ================================================================
#
# 防重防错全部交给这张表，不搞其他兼容逻辑。
# 开发阶段可以随便改表结构，不考虑旧数据兼容。
# 运行后以表为准，表里怎么写就怎么判断。
#
# 结构:
#   _id           → "{source_name}_{tf}"  如 "stock_daily_k_15m"
#   source_name   → 数据源名
#   tf            → 周期（15m / 1D）
#   last_updated  → 最后一次回填时间
#   count         → 本次拉取的 bar 数量
#   written       → 本次实际写入条数
#   status        → ok / error
#   report        → 描述信息
#

_last_update_col = db["cn_last_update"]


def _should_run(source_name: str, tf: str) -> tuple[bool, str]:
    """查 cn_last_update，判断是否需要回填。

    15m 和 1D 的"完成"定义不同:
      1D → 一个交易日干一次就够了
      15m → 盘中需要多次干，每次覆盖最新 bar

    规则:
      没记录 → 干
      1D: ok 且同交易日 → 不干
      1D: ok 但跨交易日 → 干
      15m: ok 但距上次超过 5 分钟 → 干（盘中有新 bar）
      15m: ok 且 5 分钟内 → 不干
      error → 重干

    Returns:
        (是否需要, 原因描述)
    """
    doc = _last_update_col.find_one({"_id": f"{source_name}_{tf}"})
    if not doc:
        return True, "首次回填，无历史记录"

    status = doc.get("status", "")

    # 干得不好 → 重干
    if status == "error":
        return True, f"上次失败: {doc.get('report', '')}，重试"

    if status != "ok":
        return True, f"上次 status={status}，需要回填"

    # ── 以下: status=ok ──

    last = doc.get("last_updated")
    if not last:
        return True, "无 last_updated，重新回填"

    # 1D: 同交易日不干，跨交易日干
    if tf == "1D":
        if _same_trading_day(last, datetime.utcnow()):
            return False, f"本交易日 1D 已成功回填 (written={doc.get('written', 0)})，不再重复"
        return True, f"上次 1D 成功是 {last:%Y-%m-%d}，跨交易日了，重新回填"

    # 15m: 盘中需要多次干，5 分钟节流
    elapsed = (datetime.utcnow() - last).total_seconds()
    if elapsed < 300:
        return False, f"15m 距上次回填 {elapsed:.0f}s < 300s，跳过"
    return True, f"15m 距上次回填 {elapsed:.0f}s，盘中有新 bar，重新回填"


def _record_update(
    source_name: str,
    tf: str,
    count: int,
    written: int,
    status: str,
    report: str,
):
    """写入回填记录到 cn_last_update。"""
    _last_update_col.update_one(
        {"_id": f"{source_name}_{tf}"},
        {"$set": {
            "source_name": source_name,
            "tf": tf,
            "last_updated": datetime.utcnow(),
            "count": count,
            "written": written,
            "status": status,
            "report": report,
        }},
        upsert=True,
    )


# ================================================================
# count 计算 — 从当前时间倒推
# ================================================================

def _calc_15m_count() -> int:
    """从当前时间倒推，计算当前应有多少根完整的 15m bar。

    16 个节点: 9:45, 10:00, ..., 11:30, 13:15, ..., 15:00
    盘前返回 0，盘中按已过的节点数计算，盘后返回 16。
    """
    now = datetime.now(TZ_CN)
    h, m = now.hour, now.minute

    # 盘前: 第一根 bar 9:30-9:45，9:45 才完成
    if h < 9 or (h == 9 and m < 45):
        return 0

    # 上午盘: 9:45 ~ 11:30，共 8 根
    if h < 11 or (h == 11 and m <= 30):
        minutes_since_open = (h - 9) * 60 + m - 30
        return max(minutes_since_open // 15, 1)

    # 午休: 11:31 ~ 13:14，上午盘 8 根已全部完成
    if h < 13 or (h == 13 and m < 15):
        return 8

    # 下午盘: 13:15 ~ 15:00，共 8 根
    if h < 15 or (h == 15 and m == 0):
        minutes_since_afternoon = (h - 13) * 60 + m - 15
        return 8 + max(minutes_since_afternoon // 15, 1)

    # 收盘后: 16 根全部完成
    return 16


def _should_run(source_name: str, tf: str) -> tuple[bool, str]:
    """查 cn_last_update，判断是否需要回填。

    15m 和 1D 的"完成"定义不同:
      1D → 一个交易日干一次就够了
      15m → 盘中按节点触发，过了节点就有新 bar 可拉

    判断依据: cn_last_update.count vs 当前应有 bar 数
      count < 当前应有 → 有新 bar，干
      count >= 当前应有 → 没新 bar，不干

    Returns:
        (是否需要, 原因描述)
    """
    doc = _last_update_col.find_one({"_id": f"{source_name}_{tf}"})
    if not doc:
        return True, "首次回填，无历史记录"

    status = doc.get("status", "")

    # 干得不好 → 重干
    if status == "error":
        return True, f"上次失败: {doc.get('report', '')}，重试"

    if status != "ok":
        return True, f"上次 status={status}，需要回填"

    # ── 以下: status=ok ──

    last = doc.get("last_updated")
    if not last:
        return True, "无 last_updated，重新回填"

    # 1D: 同交易日不干，跨交易日干
    if tf == "1D":
        if _same_trading_day(last, datetime.utcnow()):
            return False, f"本交易日 1D 已成功回填 (written={doc.get('written', 0)})，不再重复"
        return True, f"上次 1D 成功是 {last:%Y-%m-%d}，跨交易日了，重新回填"

    # 15m: 按节点判断 — 上次拉的 bar 数 < 当前应有 bar 数 → 有新 bar
    last_count = doc.get("count", 0)
    now_count = _calc_15m_count()

    if now_count <= 0:
        return False, "盘前，无需回填"

    if last_count >= now_count:
        return False, f"15m 已覆盖到第 {last_count} 根，当前应有 {now_count} 根，无新 bar"

    return True, f"15m 上次 {last_count} 根，当前应有 {now_count} 根，有新 bar 可拉"


def _calc_1d_count() -> int:
    """盘后回填 1D，覆盖最近 5 个交易日（含跨周/节假日缺口）。"""
    return 5


# ================================================================
# 时间范围计算
# ================================================================

def _today_start() -> datetime:
    """今天 00:00:00（北京时间）。"""
    now = datetime.now(TZ_CN)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _range_start(days_back: int) -> datetime:
    """往前推 days_back 天的 00:00:00。"""
    return _today_start() - timedelta(days=days_back)


# ================================================================
# BackfillDB
# ================================================================

class BackfillDB:
    """全盘批量回填工具 — 1 次 HTTP 拉全量标的。

    决策依据是 cn_last_update 表，不靠猜时间。
    每次回填结果写回 cn_last_update，形成闭环。
    """

    def __init__(
        self,
        name: str,
        collection_name: str,
        url_template: str,
        build_doc_id,
        timestamp_field: str,
    ):
        """
        Args:
            name: 数据源名称（写入 cn_last_update 的标识，如 "stock_daily_k"）
            collection_name: MongoDB 集合名
            url_template: API URL 模板，支持 {tf} 和 {count} 占位
            build_doc_id: (item) -> 文档 _id 的构造函数
            timestamp_field: 文档中时间戳字段名
        """
        self.name = name
        self.collection = db[collection_name]
        self.url_template = url_template
        self.build_doc_id = build_doc_id
        self.timestamp_field = timestamp_field

    @staticmethod
    def _make_headers() -> dict:
        return {"Authorization": f"Bearer {API_KEY}"}

    async def _fetch_with_retries(
        self, session: aiohttp.ClientSession, url: str
    ) -> dict | None:
        for attempt in range(MAX_RETRIES):
            try:
                async with session.get(
                    url, headers=self._make_headers()
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"请求失败 url={url}, status={resp.status}")
                        return None
                    return await resp.json()
            except Exception as e:
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    f"请求异常 url={url}, error={e}, {wait}s 后重试 ({attempt + 1}/{MAX_RETRIES})"
                )
                await asyncio.sleep(wait)
        logger.error(f"放弃请求 url={url}, 已达最大重试次数")
        return None

    async def _delete_range(self, since: datetime) -> int:
        """删除时间范围内已有的文档（先删后写的"先删"部分）。"""
        result = await self.collection.delete_many({"timestamp": {"$gte": since}})
        deleted = result.deleted_count
        if deleted > 0:
            logger.info(f"[回填] {self.name} 清理旧数据: 删除 {deleted} 条 (since {since:%Y-%m-%d})")
        return deleted

    async def _run(self, tf: str, count: int) -> int:
        """完整回填流程: 删旧 → HTTP 拉取 → bulk_write。"""
        url = self.url_template.format(tf=tf, count=count)

        # 先删后写
        if tf == "1D":
            await self._delete_range(_range_start(count))
        else:
            await self._delete_range(_today_start())

        # HTTP 拉取
        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENCY)
        async with aiohttp.ClientSession(connector=connector) as session:
            data = await self._fetch_with_retries(session, url)

        if not data:
            return 0

        items = data.get("data", [])
        if not items:
            return 0

        # bulk_write
        ops = []
        for item in items:
            doc_id = self.build_doc_id(item)
            ts_str = item.get(self.timestamp_field)
            if isinstance(ts_str, str):
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    ts = datetime.now(TZ_CN)
            else:
                ts = datetime.now(TZ_CN)

            doc = {
                "_id": doc_id,
                **item,
                "timestamp": ts,
                "fetched_at": datetime.utcnow(),
            }
            ops.append(UpdateOne({"_id": doc_id}, {"$set": doc}, upsert=True))

        if ops:
            await self.collection.bulk_write(ops, ordered=False)
        return len(ops)

    def run_once(
        self,
        tf: str | None = None,
        count: int | None = None,
    ) -> dict:
        """执行一次全盘回填。

        cn_last_update 是唯一控制机制:
          ok 且同交易日 → 不干
          error / 无记录 / 跨交易日 → 干

        Args:
            tf: "15m" / "1D"。None=自动判断（盘中 15m，盘后 1D）
            count: bar 数量。None=自动计算。

        Returns:
            {"source": ..., "tf": ..., "count": ..., "written": ..., "status": ..., "report": ...}
        """
        # 自动判断周期
        if tf is None:
            now = datetime.now(TZ_CN)
            if now.hour < 9 or (now.hour == 9 and now.minute < 30):
                tf = "15m"
            elif now.hour >= 15:
                tf = "1D"
            else:
                tf = "15m"

        # 查表: 该不该干
        should, reason = _should_run(self.name, tf)
        if not should:
            return {
                "source": self.name, "tf": tf, "count": 0,
                "written": 0, "status": "ok", "report": reason,
            }

        # 计算 count
        if count is None:
            count = _calc_15m_count() if tf == "15m" else _calc_1d_count()

        if count <= 0:
            # 盘前不算失败，不写 error，保持表里上次的状态
            return {
                "source": self.name, "tf": tf, "count": 0,
                "written": 0, "status": "ok", "report": "盘前，无需回填",
            }

        # 执行回填
        try:
            loop = asyncio.new_event_loop()
            try:
                written = loop.run_until_complete(self._run(tf, count))
            finally:
                loop.close()
        except Exception as e:
            report = f"回填异常: {e}"
            logger.error(f"[回填] {self.name} tf={tf} {report}")
            _record_update(self.name, tf, count, 0, "error", report)
            return {
                "source": self.name, "tf": tf, "count": count,
                "written": 0, "status": "error", "report": report,
            }

        # 成功 → 写 ok，下次不再干
        report = f"拉取 {count} 根，写入 {written} 条"
        _record_update(self.name, tf, count, written, "ok", report)
        logger.info(f"[回填] {self.name} tf={tf} {report}")

        return {
            "source": self.name, "tf": tf, "count": count,
            "written": written, "status": "ok", "report": report,
        }


# ========== 预定义数据源实例 ==========

stock_daily_k = BackfillDB(
    name="stock_daily_k",
    collection_name="stock_daily_k",
    url_template=f"{BASE_URL}/stock/daily_k?tf={{tf}}&count={{count}}",
    build_doc_id=lambda item: f"{item.get('symbol', '')}_{item.get('date', '')}",
    timestamp_field="date",
)

fund_nav_daily = BackfillDB(
    name="fund_nav_daily",
    collection_name="fund_nav_daily",
    url_template=f"{BASE_URL}/fund/nav_daily?tf={{tf}}&count={{count}}",
    build_doc_id=lambda item: f"{item.get('symbol', '')}_{item.get('navDate', '')}",
    timestamp_field="navDate",
)

bond_daily_k = BackfillDB(
    name="bond_daily_k",
    collection_name="bond_daily_k",
    url_template=f"{BASE_URL}/bond/daily_k?tf={{tf}}&count={{count}}",
    build_doc_id=lambda item: f"{item.get('symbol', '')}_{item.get('date', '')}",
    timestamp_field="date",
)


# ========== 全盘回填入口 ==========


def run_once(
    tf: str | None = None,
    count: int | None = None,
) -> list[dict]:
    """全盘回填入口 — 三个数据源依次执行。

    每个数据源独立查 cn_last_update 判断是否需要干活:
      干好了 → 同一天不干，发 1000 次也不干
      干得不好 → 下次重干

    Args:
        tf: "15m" / "1D"。None=自动判断。
        count: bar 数量。None=自动计算。

    Returns:
        [{"source": ..., "tf": ..., "count": ..., "written": ..., "status": ..., "report": ...}, ...]
    """
    results = []
    for source in (stock_daily_k, fund_nav_daily, bond_daily_k):
        try:
            r = source.run_once(tf, count)
            results.append(r)
        except Exception as e:
            logger.error(f"[全盘回填] {source.name} 异常: {e}")
            results.append({
                "source": source.name, "tf": tf or "?", "count": 0,
                "written": 0, "status": "error", "report": str(e),
            })

    ok = sum(1 for r in results if r["status"] == "ok")
    errors = sum(1 for r in results if r["status"] == "error")
    logger.info(f"[全盘回填] 完成: {ok} 成功, {errors} 失败")

    return results
