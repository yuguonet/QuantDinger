"""
kline_derived — K线衍生分析指标

从 OHLCV 原始数据计算，无需外部 API。

自动注入 df 的列：
    amplitude       振幅 (%) = (high - low) / pre_close * 100
    pct_change      涨跌幅 (%) = (close - pre_close) / pre_close * 100
    volume_ratio     量比 = 当日volume / 近5日均量
    vwap            均价 (元) = amount / volume
    body_ratio      实体占比 = abs(close-open) / (high-low)
    upper_shadow    上影线比 = (high - max(open,close)) / (high-low)
    lower_shadow    下影线比 = (min(open,close) - low) / (high-low)
    atr_14          14日ATR (平均真实波幅)
    obv             OBV 能量潮 (累计量)

脚本可用变量：
    stock_amplitude   最新振幅
    stock_pct_change  最新涨跌幅
    stock_volume_ratio 最新量比
    stock_vwap        最新均价
    stock_atr         最新ATR
    stock_obv         最新OBV
"""
import logging
import numpy as np
import pandas as pd

from . import provider

logger = logging.getLogger(__name__)


def _calc_amplitude(high, low, pre_close):
    """振幅 (%)"""
    denom = pre_close.replace(0, np.nan)
    return ((high - low) / denom * 100).round(4)


def _calc_pct_change(close, pre_close):
    """涨跌幅 (%)"""
    denom = pre_close.replace(0, np.nan)
    return ((close - pre_close) / denom * 100).round(4)


def _calc_volume_ratio(volume, window=5):
    """量比 = 当日volume / 近window日均量"""
    avg_vol = volume.rolling(window=window, min_periods=1).mean().shift(1)
    avg_vol = avg_vol.replace(0, np.nan)
    return (volume / avg_vol).round(4)


def _calc_vwap(amount, volume):
    """均价 = 成交额 / 成交量 (注意单位统一)"""
    vol = volume.replace(0, np.nan)
    return (amount / vol).round(4)


def _calc_body_ratio(open_p, close_p, high, low):
    """实体占比"""
    hl = (high - low).replace(0, np.nan)
    return ((close_p - open_p).abs() / hl).round(4)


def _calc_upper_shadow(open_p, close_p, high, low):
    """上影线比"""
    hl = (high - low).replace(0, np.nan)
    top = pd.concat([open_p, close_p], axis=1).max(axis=1)
    return ((high - top) / hl).round(4)


def _calc_lower_shadow(open_p, close_p, high, low):
    """下影线比"""
    hl = (high - low).replace(0, np.nan)
    bottom = pd.concat([open_p, close_p], axis=1).min(axis=1)
    return ((bottom - low) / hl).round(4)


def _calc_atr(high, low, close, window=14):
    """ATR (Average True Range)"""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=window, min_periods=1).mean().round(4)


def _calc_obv(close, volume):
    """OBV (On Balance Volume)"""
    direction = np.sign(close.diff())
    direction.iloc[0] = 0
    obv = (direction * volume).cumsum()
    return obv.round(0)


@provider
def register(ctx: dict) -> dict:
    symbol = ctx.get('symbol', '')
    df = ctx.get('df')

    extras = {}

    if df is not None and len(df) > 0:
        # 需要的列
        o = df['open'].astype('float64') if 'open' in df.columns else pd.Series(dtype='float64')
        h = df['high'].astype('float64') if 'high' in df.columns else pd.Series(dtype='float64')
        l = df['low'].astype('float64') if 'low' in df.columns else pd.Series(dtype='float64')
        c = df['close'].astype('float64') if 'close' in df.columns else pd.Series(dtype='float64')
        v = df['volume'].astype('float64') if 'volume' in df.columns else pd.Series(dtype='float64')

        # amount 可能不存在
        if 'amount' in df.columns:
            amt = df['amount'].astype('float64')
        else:
            amt = pd.Series(0, index=df.index, dtype='float64')

        # pre_close: shift(1) 或从列取
        if 'pre_close' in df.columns:
            pc = df['pre_close'].astype('float64')
        else:
            pc = c.shift(1)

        try:
            df['amplitude'] = _calc_amplitude(h, l, pc)
            df['pct_change'] = _calc_pct_change(c, pc)
            df['volume_ratio'] = _calc_volume_ratio(v, window=5)
            df['vwap'] = _calc_vwap(amt, v)
            df['body_ratio'] = _calc_body_ratio(o, c, h, l)
            df['upper_shadow'] = _calc_upper_shadow(o, c, h, l)
            df['lower_shadow'] = _calc_lower_shadow(o, c, h, l)
            df['atr_14'] = _calc_atr(h, l, c, window=14)
            df['obv'] = _calc_obv(c, v)

            # 暴露最新值
            extras['stock_amplitude'] = float(df['amplitude'].iloc[-1]) if not df['amplitude'].isna().all() else 0.0
            extras['stock_pct_change'] = float(df['pct_change'].iloc[-1]) if not df['pct_change'].isna().all() else 0.0
            extras['stock_volume_ratio'] = float(df['volume_ratio'].iloc[-1]) if not df['volume_ratio'].isna().all() else 1.0
            extras['stock_vwap'] = float(df['vwap'].iloc[-1]) if not df['vwap'].isna().all() else 0.0
            extras['stock_atr'] = float(df['atr_14'].iloc[-1]) if not df['atr_14'].isna().all() else 0.0
            extras['stock_obv'] = float(df['obv'].iloc[-1]) if not df['obv'].isna().all() else 0.0

        except Exception as e:
            logger.debug("kline_derived(%s) 计算失败: %s", symbol, e)

    return extras
