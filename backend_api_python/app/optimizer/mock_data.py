"""
本地模拟数据生成器
用于在没有外部 API 的环境下测试优化器逻辑
"""
import numpy as np
import pandas as pd
from datetime import datetime, timedelta


def generate_mock_klines(
    start_date: datetime,
    end_date: datetime,
    timeframe: str = "4H",
    initial_price: float = 60000.0,
    volatility: float = 0.02,
    trend: float = 0.0001,
    seed: int = 42,
) -> pd.DataFrame:
    """
    生成模拟 K 线数据

    Args:
        start_date: 起始时间
        end_date: 结束时间
        timeframe: 时间框架
        initial_price: 初始价格
        volatility: 波动率（每根 K 线的价格变化标准差）
        trend: 趋势（正值=上涨，负值=下跌）
        seed: 随机种子

    Returns:
        DataFrame with columns: time, open, high, low, close, volume
    """
    np.random.seed(seed)

    tf_map = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
        "1H": 3600, "4H": 14400, "1D": 86400,
        "1h": 3600, "4h": 14400, "1d": 86400,
    }
    interval = tf_map.get(timeframe, 14400)

    total_seconds = int((end_date - start_date).total_seconds())
    n_bars = total_seconds // interval

    times = []
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []

    price = initial_price
    current_time = start_date

    for i in range(n_bars):
        # 随机价格变化
        change = np.random.normal(trend, volatility)
        open_price = price
        close_price = price * (1 + change)

        # 生成 high/low
        bar_range = abs(close_price - open_price) + price * np.random.uniform(0.001, 0.01)
        high_price = max(open_price, close_price) + np.random.uniform(0, bar_range * 0.5)
        low_price = min(open_price, close_price) - np.random.uniform(0, bar_range * 0.5)

        # 成交量
        volume = np.random.uniform(100, 10000) * (1 + abs(change) * 50)

        times.append(current_time)
        opens.append(round(open_price, 2))
        highs.append(round(high_price, 2))
        lows.append(round(low_price, 2))
        closes.append(round(close_price, 2))
        volumes.append(round(volume, 2))

        price = close_price
        current_time += timedelta(seconds=interval)

    df = pd.DataFrame({
        "time": times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    df = df.set_index("time")
    return df


class MockDataSource:
    """模拟数据源，替代 DataSourceFactory 用于本地测试"""

    def __init__(self, data: pd.DataFrame):
        self._data = data

    def get_kline(self, market, symbol, timeframe, limit, before_time=None):
        """返回与 DataSourceFactory.get_kline 兼容的格式"""
        df = self._data.copy()
        if before_time:
            # 过滤
            pass
        if limit and len(df) > limit:
            df = df.tail(limit)

        # 转为 list of dict（与真实数据源格式一致）
        records = []
        for idx, row in df.iterrows():
            ts = int(idx.timestamp()) if hasattr(idx, 'timestamp') else int(idx)
            records.append({
                "time": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
        return records


def patch_backtest_with_mock(mock_data: pd.DataFrame):
    """
    Monkey-patch BacktestService._fetch_kline_data 以使用 mock 数据

    用法:
        mock_data = generate_mock_klines(...)
        patch_backtest_with_mock(mock_data)
        # 之后所有 BacktestService.run() 都会使用 mock 数据
    """
    from app.services import backtest as bt_module

    mock_source = MockDataSource(mock_data)

    original_fetch = bt_module.BacktestService._fetch_kline_data

    def mock_fetch(self, market, symbol, timeframe, start_date, end_date):
        # 返回 mock 数据，但按日期过滤
        df = mock_data.copy()
        if isinstance(df.index, pd.DatetimeIndex):
            df = df[(df.index >= start_date) & (df.index <= end_date)]
        return df

    bt_module.BacktestService._fetch_kline_data = mock_fetch
    print(f"  [Mock] BacktestService._fetch_kline_data 已替换为 mock 数据源 ({len(mock_data)} bars)")
