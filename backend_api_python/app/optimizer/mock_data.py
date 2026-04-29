"""
本地模拟数据生成器
用于在没有外部 API 的环境下测试优化器逻辑

支持：
  - 加密市场模拟（高波动、24/7 交易）
  - A 股模拟（涨跌停、T+1、换手率、正常交易时间）
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
    生成模拟 K 线数据（通用版，兼容加密和 A 股）

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


def generate_ashare_mock_klines(
    start_date: datetime,
    end_date: datetime,
    symbol: str = "000001.SZ",
    initial_price: float = 15.0,
    volatility: float = 0.018,
    trend: float = 0.0003,
    include_st: bool = False,
    seed: int = 42,
) -> pd.DataFrame:
    """
    生成 A 股模拟 K 线数据

    特点：
    - 仅生成交易日数据（跳过周末）
    - 涨跌停限制（主板 10%，创业板 20%）
    - 包含换手率、振幅等 A 股特色字段
    - 日线级别（A 股最常用）

    Args:
        start_date: 起始日期
        end_date: 结束日期
        symbol: 股票代码（自动判断板块和涨跌停）
        initial_price: 初始价格
        volatility: 日波动率
        trend: 日均涨幅
        include_st: 是否生成 ST 股数据（5% 涨跌停）
        seed: 随机种子

    Returns:
        DataFrame with columns: open, high, low, close, volume, turnover, turnover_rate, amplitude, pct_change
    """
    np.random.seed(seed)

    # 判断板块和涨跌停
    code = symbol.split(".")[0] if "." in symbol else symbol
    if code.startswith("3"):
        price_limit = 0.20  # 创业板
    elif code.startswith("68"):
        price_limit = 0.20  # 科创板
    elif code.startswith(("8", "4")):
        price_limit = 0.30  # 北交所
    elif include_st:
        price_limit = 0.05  # ST
    else:
        price_limit = 0.10  # 主板

    # 生成交易日序列（跳过周末）
    trading_days = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:  # 周一到周五
            trading_days.append(current)
        current += timedelta(days=1)

    n_days = len(trading_days)

    times = []
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []
    turnovers = []
    turnover_rates = []

    price = initial_price

    for i, day in enumerate(trading_days):
        open_price = price

        # 日内涨跌幅（受涨跌停限制）
        daily_change = np.random.normal(trend, volatility)
        daily_change = max(-price_limit, min(price_limit, daily_change))  # 涨跌停限制

        close_price = open_price * (1 + daily_change)

        # 振幅
        intraday_range = abs(daily_change) + np.random.uniform(0.002, 0.015)
        intraday_range = min(intraday_range, price_limit * 2)  # 振幅不超过涨跌停的两倍

        high_price = max(open_price, close_price) * (1 + np.random.uniform(0, 0.005))
        low_price = min(open_price, close_price) * (1 - np.random.uniform(0, 0.005))

        # 确保 high/low 不超过涨跌停
        prev_close = price
        upper_limit = prev_close * (1 + price_limit)
        lower_limit = prev_close * (1 - price_limit)
        high_price = min(high_price, upper_limit)
        low_price = max(low_price, lower_limit)

        # 成交量（A 股单位：手，1手=100股）
        base_volume = np.random.uniform(50000, 500000)  # 5万手到50万手
        volume = base_volume * (1 + abs(daily_change) * 10)
        volume = round(volume)

        # 成交额
        avg_price = (open_price + close_price) / 2
        turnover = volume * 100 * avg_price  # 成交量(手) * 100 * 均价

        # 换手率（0.5% ~ 15%）
        turnover_rate = np.random.uniform(0.3, 8.0)
        if abs(daily_change) > 0.05:
            turnover_rate *= 2  # 大涨大跌时换手率升高

        times.append(day)
        opens.append(round(open_price, 2))
        highs.append(round(high_price, 2))
        lows.append(round(low_price, 2))
        closes.append(round(close_price, 2))
        volumes.append(volume)
        turnovers.append(round(turnover, 2))
        turnover_rates.append(round(turnover_rate, 2))

        price = close_price

    df = pd.DataFrame({
        "time": times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "turnover": turnovers,
        "turnover_rate": turnover_rates,
    })
    df = df.set_index("time")

    # 计算衍生指标
    df["amplitude"] = ((df["high"] - df["low"]) / df["close"].shift(1) * 100).round(2)
    df["pct_change"] = (df["close"].pct_change() * 100).round(2)

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


# ============================================================
# 快速测试入口
# ============================================================

if __name__ == "__main__":
    # 测试 A 股 mock 数据生成
    print("生成 A 股模拟数据...")
    df = generate_ashare_mock_klines(
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2025, 12, 31),
        symbol="000001.SZ",
        initial_price=15.0,
        volatility=0.018,
        trend=0.0003,
    )
    print(f"  交易日数: {len(df)}")
    print(f"  价格范围: {df['close'].min():.2f} ~ {df['close'].max():.2f}")
    print(f"  换手率范围: {df['turnover_rate'].min():.1f}% ~ {df['turnover_rate'].max():.1f}%")
    print(f"  涨跌幅范围: {df['pct_change'].min():.2f}% ~ {df['pct_change'].max():.2f}%")
    print(f"\n前 5 行:")
    print(df.head())
    print(f"\n后 5 行:")
    print(df.tail())
