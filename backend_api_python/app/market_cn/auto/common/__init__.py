"""auto/common —— 策略通用算法库 (纯计算, 零 IO, 零策略感知)

用途: 承载所有策略共用的纯函数, 从 dragon_core.py 提取 (Phase 1, 2026-09-07)。
  market.py      板块判定 / 涨停识别
  indicators.py  技术指标 (EMA/RSI/MACD/布林带宽/ROC/PSY + MACD形态族)
  filters.py     U1~U4 统一前置过滤

关键设计点:
  - 本目录函数只依赖入参, 禁止 import DB/HTTP/策略模块 (唯一允许 IO 的是 auto/data/);
  - 与 test_dragon.py 同名函数逐字一致 —— 改这里必须同步 test_dragon.py 并重跑对数;
  - core.py (原dragon_core.py) 对这些名字做 re-export (facade), 外部 import 路径不变。

易错点:
  - 不要把策略私有逻辑 (如 DRAGON_CB_PARAMS) 挪进来, 那是策略文件的事;
  - 上移判据: 出现 auto 之外的消费者时, 把那一个函数上移到 market_cn/, 不整目录上移。
"""
