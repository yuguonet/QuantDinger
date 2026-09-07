"""auto/output —— 标准化输出层 (双路径中的"展示"路径)

用途: SignalRow → 前端 JSON / CLI 文本的纯格式化 (收编 dragon_store._display_detail)。
关键设计点:
  - 纯格式化、零 IO: 被 dragon_api(前端) 与手动工具(CLI) 共用, 保证展示同源一致;
  - 双路径分工: DB 写入(含自选股组投影)归 store.py, 不在本层。
易错点: 本层不做路由决策、不查库; 输入不完整时输出安全占位而非抛异常。
(display.py 实体随 Phase 3 store 去策略化时迁入; Phase 1 仅占位。)
"""
