"""market.py — 市场原语**单一事实源转发**（零副本）。

实现全部在 `core/market.py`（MarketSpec 驱动，a.yaml 为数值唯一事实源）。
仅保留两个策略口径小工具（取自旧策略文件本身的常量口径）。
"""

from app.market_cn.auto.core.market import (  # noqa: F401
    default_market, get_board_name, get_board_type, is_limit_up,
    limit_dn_price, limit_up_price, limit_dn_tol,
)

# 跌停判定相对容差（a.yaml dn_tol；取默认板块档）
DN_TOL = limit_dn_tol()

_NOMINAL_UP = {"main": 0.10, "gem_star": 0.20}   # 旧策略 _limit_pct 口径（名义幅度）


def nominal_up_pct(code) -> float:
    """名义涨停幅度（旧 _limit_pct：主板 10% / 创科 20%，封板阻买判定用）。"""
    return _NOMINAL_UP[get_board_type(code)]
