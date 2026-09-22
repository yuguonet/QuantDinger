# 兼容 shim: 旧 common.exec_cn -> core.exec (+ limit_dn_price 在 core.market)
from app.market_cn.auto.core.exec import (  # noqa: F401
    fill_blocked_by_limit_dn, fill_on_gap, is_one_word_limit_dn,
)
from app.market_cn.auto.core.market import limit_dn_price  # noqa: F401
