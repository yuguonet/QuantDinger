"""Feather 数据存储管理模块 - 统一管理所有历史数据表

将 SQLite 替换为 Apache Feather (pyarrow) 格式，保持对外 API 兼容。
每个逻辑表对应一个 .feather 文件，存放在 data_dir 下。

健壮性设计:
    - 原子写入：先写 .tmp 再 rename，防止写入中断导致文件损坏
    - 自动备份：写入前备份旧文件（.bak），写入失败自动回滚
    - 读取容错：损坏的 feather 文件自动尝试从 .bak 恢复
    - 类型安全：写入前统一 DataFrame 列类型，防止类型漂移
    - 文件锁：通过线程锁保护并发读写
"""
import os
import shutil
import logging
import threading
import time
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ---------- 表结构定义 ----------

# 每张表的主键（用于去重合并）
TABLE_PRIMARY_KEYS: Dict[str, List[str]] = {
    "cnd_hot_rank":           ["trade_date", "stock_code"],
    "cnd_zt_pool":            ["trade_date", "stock_code"],
    "cnd_dt_pool":            ["trade_date", "stock_code"],
    "cnd_zt_pool_zbgc":       ["trade_date", "stock_code"],
    "cnd_sector_fund_flow":   ["trade_date", "name"],
    "cnd_concept_fund_flow":  ["trade_date", "name"],
    "cnd_stock_info":         ["stock_code"],
    "cnd_emotion_history":    ["timestamp"],
}

# 每张表的推荐列类型（写入时强制转换，防止类型漂移）
TABLE_DTYPES: Dict[str, Dict[str, str]] = {
    "cnd_hot_rank": {
        "rank": "Int64", "stock_code": "string", "stock_name": "string",
        "popularity_score": "float64", "price": "float64", "change_percent": "float64",
        "current_rank_change": "string", "trade_date": "string", "fetch_time": "string",
    },
    "cnd_zt_pool": {
        "trade_date": "string", "stock_code": "string", "stock_name": "string",
        "price": "float64", "change_percent": "float64", "volume": "float64",
        "amount": "float64", "turnover_rate": "float64", "seal_amount": "float64",
        "reason": "string", "sector": "string", "zt_time": "string",
        "first_zt_time": "string", "open_count": "Int64", "continuous_zt_days": "Int64",
        "fetch_time": "string",
    },
    "cnd_dt_pool": {
        "trade_date": "string", "stock_code": "string", "stock_name": "string",
        "price": "float64", "change_percent": "float64", "volume": "float64",
        "amount": "float64", "turnover_rate": "float64", "seal_amount": "float64",
        "reason": "string", "zt_time": "string", "open_count": "Int64",
        "continuous_dt_days": "Int64", "fetch_time": "string",
    },
    "cnd_zt_pool_zbgc": {
        "trade_date": "string", "stock_code": "string", "stock_name": "string",
        "price": "float64", "change_percent": "float64", "volume": "float64",
        "amount": "float64", "turnover_rate": "float64", "seal_amount": "float64",
        "zt_time": "string", "open_time": "string", "reason": "string",
        "sector": "string", "fetch_time": "string",
    },
    "cnd_sector_fund_flow": {
        "trade_date": "string", "name": "string", "change_percent": "float64",
        "main_net_flow": "float64", "main_inflow": "float64", "main_outflow": "float64",
        "retail_net_flow": "float64", "leader_stock_name": "string",
        "leader_stock_change_percent": "float64", "fetch_time": "string",
    },
    "cnd_concept_fund_flow": {
        "trade_date": "string", "name": "string", "change_percent": "float64",
        "main_net_flow": "float64", "main_inflow": "float64", "main_outflow": "float64",
        "retail_net_flow": "float64", "leader_stock_name": "string",
        "leader_stock_change_percent": "float64", "fetch_time": "string",
    },
    "cnd_stock_info": {
        "stock_code": "string", "stock_name": "string", "industry": "string",
        "list_date": "string", "total_shares": "float64", "float_shares": "float64",
        "total_market_cap": "float64", "float_market_cap": "float64",
        "province": "string", "main_business": "string", "fetch_time": "string",
    },
    "cnd_emotion_history": {
        "timestamp": "string", "trade_date": "string", "emotion": "Int64",
        "up_count": "Int64", "down_count": "Int64",
        "limit_up": "Int64", "limit_down": "Int64",
        "north_net_flow": "float64",
    },
}


class cache_db:
    """Feather 数据管理器（对外 API 与原 SQLite 版完全兼容）"""

    ALLOWED_TABLES = set(TABLE_PRIMARY_KEYS.keys())

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "feather")
        self._ensure_dir()
        # 表级锁：每个表一把读写锁，避免全局锁导致的瓶颈
        self._locks: Dict[str, threading.Lock] = {
            t: threading.Lock() for t in self.ALLOWED_TABLES
        }
        # 内存缓存：减少频繁磁盘 IO（惰性加载，写入时失效）
        self._cache: Dict[str, Optional[pd.DataFrame]] = {}
        logger.info(f"Feather 数据目录: {self.data_dir}")

    # ------ 目录与文件路径 ------

    def _ensure_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)

    def _feather_path(self, table: str) -> str:
        return os.path.join(self.data_dir, f"{table}.feather")

    def _backup_path(self, table: str) -> str:
        return os.path.join(self.data_dir, f"{table}.feather.bak")

    def _tmp_path(self, table: str) -> str:
        # 使用时间戳 + pid 避免多进程冲突
        return os.path.join(self.data_dir, f"{table}.feather.tmp.{os.getpid()}.{int(time.time()*1000)}")

    # ------ 表名校验 ------

    def _validate_table(self, table: str):
        if table not in self.ALLOWED_TABLES:
            raise ValueError(f"非法表名: {table}，允许的表: {self.ALLOWED_TABLES}")

    # ------ 底层 IO ------

    def _read_feather(self, table: str) -> pd.DataFrame:
        """读取 feather 文件，带容错恢复"""
        path = self._feather_path(table)
        if not os.path.exists(path):
            return pd.DataFrame()

        try:
            df = pd.read_feather(path)
            return df
        except Exception as e:
            logger.warning(f"读取 {path} 失败: {e}，尝试从备份恢复")
            return self._restore_from_backup(table)

    def _restore_from_backup(self, table: str) -> pd.DataFrame:
        """从 .bak 文件恢复"""
        bak_path = self._backup_path(table)
        if os.path.exists(bak_path):
            try:
                df = pd.read_feather(bak_path)
                # 恢复为主文件
                shutil.copy2(bak_path, self._feather_path(table))
                logger.info(f"从备份恢复 {table} 成功")
                return df
            except Exception as e2:
                logger.error(f"备份恢复也失败: {e2}")
        return pd.DataFrame()

    def _write_feather(self, table: str, df: pd.DataFrame):
        """原子写入 feather 文件（先 tmp 再 rename）"""
        path = self._feather_path(table)
        bak_path = self._backup_path(table)
        tmp_path = self._tmp_path(table)

        try:
            # 1. 备份旧文件
            if os.path.exists(path):
                shutil.copy2(path, bak_path)

            # 2. 写入临时文件
            df.to_feather(tmp_path)

            # 3. 验证写入完整性：读回来确认
            _verify = pd.read_feather(tmp_path)
            if len(_verify) != len(df):
                raise ValueError(
                    f"写入验证失败: 预期 {len(df)} 行, 实际 {len(_verify)} 行"
                )

            # 4. 原子替换
            os.replace(tmp_path, path)

            # 5. 写入成功后清理过期的 tmp 文件
            self._cleanup_tmp_files(table)

        except Exception as e:
            logger.error(f"写入 {table} 失败: {e}")
            # 清理临时文件
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            # 回滚：如果有备份，恢复
            if os.path.exists(bak_path) and not os.path.exists(path):
                shutil.copy2(bak_path, path)
            raise

    def _cleanup_tmp_files(self, table: str):
        """清理残留的 tmp 文件（异常终止的写入）"""
        try:
            prefix = f"{table}.feather.tmp."
            for fname in os.listdir(self.data_dir):
                if fname.startswith(prefix):
                    fpath = os.path.join(self.data_dir, fname)
                    # 超过 60 秒的 tmp 文件才清理（避免误删正在写的）
                    if time.time() - os.path.getmtime(fpath) > 60:
                        os.remove(fpath)
                        logger.debug(f"清理残留临时文件: {fname}")
        except OSError:
            pass

    # ------ 类型规范化 ------

    def _normalize_dtypes(self, table: str, df: pd.DataFrame) -> pd.DataFrame:
        """按表定义强制转换列类型，防止类型漂移导致 feather 写入失败"""
        dtypes = TABLE_DTYPES.get(table, {})
        for col, dtype in dtypes.items():
            if col not in df.columns:
                continue
            try:
                if dtype == "string":
                    df[col] = df[col].astype("string")
                elif dtype == "Int64":
                    df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
                elif dtype == "float64":
                    df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
            except Exception as e:
                logger.warning(f"列 {col} 类型转换为 {dtype} 失败: {e}，保留原始类型")
        return df

    # ------ 内存缓存 ------

    def _load_table(self, table: str) -> pd.DataFrame:
        """加载表（优先内存缓存）"""
        if table in self._cache:
            return self._cache[table]
        df = self._read_feather(table)
        self._cache[table] = df
        return df

    def _invalidate_cache(self, table: str):
        """失效内存缓存"""
        self._cache.pop(table, None)

    # ------ 对外 API（与原 SQLite 版兼容） ------

    def insert_batch(self, table: str, data: List[Dict[str, Any]],
                     conflict_keys: Optional[List[str]] = None) -> int:
        """批量插入数据，支持按主键去重（等价于 INSERT OR REPLACE）

        Args:
            table: 表名（必须在白名单中）
            data: 数据列表
            conflict_keys: 冲突检测的键（保留接口兼容，实际使用表定义的主键）

        Returns:
            插入/更新的行数
        """
        if not data:
            return 0
        self._validate_table(table)

        # 添加 fetch_time
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for row in data:
            if "fetch_time" not in row:
                row["fetch_time"] = now

        new_df = pd.DataFrame(data)

        with self._locks[table]:
            try:
                existing_df = self._load_table(table)
                pkeys = TABLE_PRIMARY_KEYS.get(table, [])

                if existing_df.empty:
                    merged = new_df
                else:
                    # 确保新数据的列与旧数据对齐
                    for col in existing_df.columns:
                        if col not in new_df.columns:
                            new_df[col] = pd.NA
                    for col in new_df.columns:
                        if col not in existing_df.columns:
                            existing_df[col] = pd.NA

                    if pkeys and all(k in existing_df.columns for k in pkeys):
                        # 设置索引后 combine_first / update 实现 upsert
                        existing_df = existing_df.set_index(pkeys)
                        new_df_indexed = new_df.set_index(pkeys)
                        # update: 用新值覆盖旧值中匹配的行
                        existing_df.update(new_df_indexed)
                        # combine_first: 添加旧数据中不存在的新行
                        merged = new_df_indexed.combine_first(existing_df).reset_index()
                    else:
                        # 无主键定义，直接拼接去重
                        merged = pd.concat([existing_df, new_df], ignore_index=True)
                        merged = merged.drop_duplicates()

                # 类型规范化
                merged = self._normalize_dtypes(table, merged)

                # 原子写入
                self._write_feather(table, merged)
                self._invalidate_cache(table)

                logger.debug(f"批量写入 {len(data)} 条记录到 {table}，合并后共 {len(merged)} 条")
                return len(data)

            except Exception as e:
                logger.error(f"批量写入失败 [{table}]: {e}")
                return 0

    def query(self, table: str, conditions: Optional[Dict[str, Any]] = None,
              order_by: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        """查询数据

        Args:
            table: 表名
            conditions: 查询条件 {column: value} 或 {column: [values]}
            order_by: 排序字段，前缀 - 表示降序
            limit: 限制数量

        Returns:
            查询结果列表
        """
        self._validate_table(table)

        with self._locks[table]:
            try:
                df = self._load_table(table)
                if df.empty:
                    return []

                # 条件过滤
                if conditions:
                    mask = pd.Series(True, index=df.index)
                    for col, val in conditions.items():
                        if col not in df.columns:
                            mask = pd.Series(False, index=df.index)
                            break
                        if isinstance(val, (list, tuple)):
                            mask &= df[col].isin(val)
                        else:
                            mask &= (df[col] == val)
                    df = df[mask]

                # 排序
                if order_by:
                    if order_by.startswith("-"):
                        df = df.sort_values(by=order_by[1:], ascending=False)
                    else:
                        df = df.sort_values(by=order_by, ascending=True)

                # 限制
                if limit:
                    df = df.head(limit)

                # 转为 dict 列表（用 None 替换 pd.NA）
                records = df.where(df.notna(), None).to_dict(orient="records")
                return records

            except Exception as e:
                logger.error(f"查询失败 [{table}]: {e}")
                return []

    def query_between_dates(self, table: str, date_column: str,
                            start_date: str, end_date: str,
                            order_by: Optional[str] = None) -> List[Dict]:
        """按日期范围查询

        Args:
            table: 表名
            date_column: 日期列名
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            order_by: 排序字段，前缀 - 表示降序

        Returns:
            查询结果列表
        """
        self._validate_table(table)

        with self._locks[table]:
            try:
                df = self._load_table(table)
                if df.empty or date_column not in df.columns:
                    return []

                mask = (df[date_column] >= start_date) & (df[date_column] <= end_date)
                df = df[mask]

                if order_by:
                    if order_by.startswith("-"):
                        df = df.sort_values(by=order_by[1:], ascending=False)
                    else:
                        df = df.sort_values(by=order_by, ascending=True)

                records = df.where(df.notna(), None).to_dict(orient="records")
                return records

            except Exception as e:
                logger.error(f"日期范围查询失败 [{table}]: {e}")
                return []

    def query_dates_exist(self, table: str, date_column: str,
                          start_date: str, end_date: str) -> List[str]:
        """查询已存在的日期

        Returns:
            已存在的日期列表
        """
        self._validate_table(table)

        with self._locks[table]:
            try:
                df = self._load_table(table)
                if df.empty or date_column not in df.columns:
                    return []

                mask = (df[date_column] >= start_date) & (df[date_column] <= end_date)
                dates = df.loc[mask, date_column].dropna().unique().tolist()
                return sorted(dates)

            except Exception as e:
                logger.error(f"查询日期失败: {e}")
                return []

    # ------ 额外工具方法 ------

    def table_info(self, table: str) -> Dict[str, Any]:
        """获取表信息（行数、文件大小等）"""
        self._validate_table(table)
        path = self._feather_path(table)
        info = {"table": table, "exists": os.path.exists(path)}
        if info["exists"]:
            info["file_size_kb"] = round(os.path.getsize(path) / 1024, 2)
            with self._locks[table]:
                df = self._load_table(table)
                info["row_count"] = len(df)
                info["columns"] = list(df.columns)
        return info

    def compact(self, table: str) -> int:
        """压缩表：清理碎片并重新写入

        Returns:
            压缩后的行数
        """
        self._validate_table(table)
        with self._locks[table]:
            df = self._load_table(table)
            pkeys = TABLE_PRIMARY_KEYS.get(table, [])
            if pkeys and all(k in df.columns for k in pkeys):
                before = len(df)
                df = df.drop_duplicates(subset=pkeys, keep="last")
                if len(df) < before:
                    logger.info(f"压缩 {table}: {before} -> {len(df)} 行")
            df = self._normalize_dtypes(table, df)
            self._write_feather(table, df)
            self._invalidate_cache(table)
            return len(df)

    def backup_all(self) -> List[str]:
        """手动备份所有表"""
        backed_up = []
        for table in self.ALLOWED_TABLES:
            path = self._feather_path(table)
            if os.path.exists(path):
                bak = self._backup_path(table)
                shutil.copy2(path, bak)
                backed_up.append(table)
        return backed_up

    def upsert_and_prune(self, table: str, rows: List[Dict[str, Any]],
                         prune_column: str = "timestamp",
                         keep_after: str = "",
                         conflict_keys: Optional[List[str]] = None) -> int:
        """批量写入并裁剪过期数据（EmotionScheduler 等后台任务使用）

        1. 使用 insert_batch 写入新行（自动去重）
        2. 如果指定了 keep_after，裁剪 prune_column < keep_after 的行

        Args:
            table: 表名
            rows: 新行列表
            prune_column: 用于裁剪的列名
            keep_after: 保留此值之后的数据（裁剪更早的）
            conflict_keys: 冲突键（传递给 insert_batch）

        Returns:
            写入的行数
        """
        if not rows:
            return 0

        written = self.insert_batch(table, rows, conflict_keys=conflict_keys)

        if keep_after and prune_column:
            with self._locks[table]:
                try:
                    df = self._load_table(table)
                    if df.empty or prune_column not in df.columns:
                        return written
                    before = len(df)
                    df = df[df[prune_column] >= keep_after]
                    removed = before - len(df)
                    if removed > 0:
                        self._write_feather(table, df)
                        self._invalidate_cache(table)
                        logger.info(f"[upsert_and_prune] {table}: 裁剪 {removed} 条过期数据")
                except Exception as e:
                    logger.error(f"[upsert_and_prune] 裁剪失败: {e}")

        return written
