# -*- coding: utf-8 -*-
"""schedule 域工具包（定时任务/条件告警，2026-10-23，署名：QuantDinger agent）。

域名由 `tools/<子目录>` 登记表推导（项目红线：域判定一律登记表化，禁硬编码）——
本目录名 `schedule` 即域。工具实现见 schedule_tools.py。

★ 为什么 schedule 是独立子目录而不是留在顶层（2026-10-23 用户裁定）
顶层文件默认进 `common` 域 = **必注入层**，每轮 LLM 请求都白带 5 份 schema
（实测必注入 schema 3,567 → 7,349 字符，翻倍）。定时/告警是明确的窄场景，
移到子目录后走按需预选通道（域词典见 `preselect/tool_preselect.py::DATA_DOMAINS`），
命中才注入。代价：问法太偏时预选可能选不中 ⇒ 靠域词典关键词覆盖 + 三层兜底
（planner 原文回收 → 域兜底 fallback_domain_tools → capability 近重名让位）。
"""
