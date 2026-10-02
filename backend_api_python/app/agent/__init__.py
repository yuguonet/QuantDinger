# -*- coding: utf-8 -*-
"""Agent package — 统一路径设置（裸名导入的唯一切入点）。

【导入约定】agent 包内一律用**裸名**：`from chain.store import ...`、
`import agent as agent_mod`，而**不用**全名 `app.agent.chain.store`。

为什么不用全名：全名把包名硬编码进 300+ 处 import，一旦移动或重命名目录
（`app/agent` → 别处）就得全量改，移植成本极高。裸名只依赖本文件里一处
`__file__` 相对计算，目录怎么搬都不用改代码。

代价与约束（必须遵守）：
  1. `app/agent/` 下 23 个顶层名（chain / tools / utils / log / memory / rag /
     llm / skills / cron / audit / agents / capabilities / constants / cli /
     resolvers / ...）会成为**全进程**裸名。已实测与 site-packages 无同名冲突，
     新增顶层模块前请先确认不与第三方包重名。
  2. **`app/agent/` 必须排在 `app/` 之前**，否则 `import utils` 会命中
     `app/utils` 而不是 `app/agent/utils`。两处 bootstrap（本文件与
     `app/__init__.py`）都按「先 app/ 后 app/agent/」的顺序 insert(0)。
  3. 绝不能出现「同一模块既用裸名又用全名」——那会加载成两个 module 对象，
     模块级单例（QDAgentService / 会话表 / TraceCollector 收集器 / 权重缓存）
     全部双份且状态互不可见（事故 M-1）。护栏：`qd_smoke.py::test_29`。

⚠ 关于第 2 条的历史事故（2026-10-02）：`app/agent` 与 `app` 的先后顺序**不是
一次 insert 就能保证的**，它取决于「谁先被 import」的时序。曾经
`skills/market_screener/common.py` 把 `app/`（误当成 backend 根）insert 到
sys.path[0]，且没配套插 `app/agent/` ⇒ `app/` 反超 ⇒ `import utils` 命中
`app/utils` ⇒ 9 个 finance 工具模块因 `utils.md_format` 找不到被静默跳过。
因此本文件提供幂等的 `ensure_path_order()`：**去重 + 强制保序**，任何 bootstrap
点都可以重复调用，不会把顺序改坏。
"""
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_agent_dir = os.path.abspath(os.path.dirname(__file__))
_app_dir = os.path.dirname(_agent_dir)


def ensure_path_order():
    """幂等：保证 `app/agent/` 严格排在 `app/` 之前，且两目录在 sys.path 中唯一。

    可重复调用。任何往 sys.path 里塞目录的代码（`app/__init__.py`、各 CLI 脚本、
    skills 下的独立入口）都应在插入后调用一次，避免把 `app/` 顶到 `app/agent/`
    之上造成裸名解析错位（详见本文件头部事故说明）。
    """
    # 只补 backend 根；**不主动引入 app/**（那是 app/__init__.py 的职责，
    # 提前加会把 app 下的顶层包也变成裸名）。下面只做「保序」。
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    # 去重：把 _agent_dir 的所有出现摘掉，再插到 _app_dir 首次出现之前
    while _agent_dir in sys.path:
        sys.path.remove(_agent_dir)
    if _app_dir in sys.path:
        sys.path.insert(sys.path.index(_app_dir), _agent_dir)
    else:
        sys.path.insert(0, _agent_dir)


# 顺序不能反：后插的在 sys.path 更靠前 ⇒ _agent_dir 最终压在 _project_root/app 之上
ensure_path_order()
