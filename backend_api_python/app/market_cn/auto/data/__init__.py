"""auto/data —— 数据访问层 (唯一允许 IO 的底层)

用途: 策略/扫描/监控所需的行情与基础数据读取, 从 dragon_scan/dragon_monitor 逐步迁入
(Phase 1: kline.py; snapshot.py 随 Phase 3 monitor 改造迁入)。
关键设计点:
  - 本层只做"取数并规整为标准结构", 不做任何策略判定;
  - bars 统一为 list[dict] (time/open/high/low/close/volume), 前复权, 升序;
  - 与 test_dragon.py 的数据口径严格一致 (对数基准的前提)。
易错点: fetch 失败一律返回空值并降级日志, 不抛异常打断全市场循环。
"""
