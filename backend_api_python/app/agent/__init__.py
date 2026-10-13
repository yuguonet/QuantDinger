# -*- coding: utf-8 -*-
"""QuantDinger 执行系统（Agent）—— 包内一律**相对导入**，零 sys.path 注入。

【导入约定 · 2026-10-13 改】
包内一律**相对导入**：`from .chain.store import ...`、`from ..tools._base import ...`；
包外一律**绝对导入**：`from app.agent.chain.evaluator import start_eval_worker`。
**本目录不进 sys.path**，因此包内不存在裸名导入这种写法。

为什么改（事故 M-1 的根治）：
  旧写法是把本目录塞进 sys.path，包内全用裸名（`from chain.store import ...`）。
  代价是本目录下 30 个顶层名（chain / tools / utils / log / memory / rag /
  skills / cron / capabilities / ...）变成**全进程**裸名，于是同一份源码可以同时
  以裸名 `chain.store` 和全名 `app.agent.chain.store` 被加载成**两个 module
  对象** —— 模块级单例（agent 实例 / 会话表 / TraceCollector / 权重缓存）全部双份
  且状态互不可见。这不是理论风险，实测发生过。

  相对导入保留了原本选择裸名的那个理由（目录搬家不用改代码），同时消灭了
  "两种名字"这个病根：相对导入解析后的 `__name__` 就是唯一的 `app.agent.*`
  ，与包外绝对导入指向同一个对象。

为什么不用全名绝对导入：
  相对导入让"整体重命名目录"变成零改动（历史上 `app/agent` → `app/agent`
  就踩过一次：改完后 `app/agent` 变成新版骨架，包内残留的 `app.agent.tools.finance`
  指向了不存在的模块，`chain/context_frame.py` 与 `chain/sampler.py` 两条链路
  ImportError 静默炸掉、全量测试零覆盖无人发现 —— 见
  该守卫 `test_v6_import_modernize.py::test_no_dangling_app_agent_references`
  **已随 P16 归档到 `del/agentcore_retired_20261013/tests/`**，2026-10-14 核实
  `tests/` 下已无此文件 ⇒ P5 红线**当前没有守卫**，重新引入裸名 import 不会 FAIL。
  要恢复就把它从 del/ 取回（并改掉其中的 agentcore 口径）。

【约束】
  1. 新增模块不得再用裸名导入（红线 R2 自动拦）。
  2. 不得再往 sys.path 里塞本目录（红线 R3/R4 自动拦）。
  3. 入口脚本（`cli.py`、`scripts/*.py`）可以用绝对导入 + 挂 backend 根 ——
     它们以 `python xxx.py` 直跑，没有包上下文，是唯一豁免区。
"""
