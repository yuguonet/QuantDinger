"""
跨模块配置常量的单一事实源（2026-09-24 提智阶段 0.10，审计 B2）。

背景：env `AGENT_MAX_STEPS` 曾有三个消费点、三个互不一致的默认值——
agent.py 6（CodeAgent 引擎轮数初值）/ agents/task_agent.py 20（planner 预算上钳）/
nodes.py 5（单段 step_budget 硬上限）。实测盘点后的真实图景：**6 和 20 都是死值**
（引擎初值随即被 nodes.py 的 `agent.max_steps = step_budget` / `= sum_budget` 覆盖；
planner 上钳 20 随后又被节点的 5 再钳一次），真实生效的只有 5。这就是审计 B2 说的
"三个读、三个样"——env 未配置时各自为政，配置后一个变量承载三种语义。

统一语义（单一）：**单次 CodeAgent 执行的步数上限**——planner 预算上钳与单段
step_budget 硬上限派生自同一值；引擎 max_steps 由预算覆盖（nodes.py），链条自洽。
默认 5：保持现役真实行为零漂移，并与"单段 ≤5 步、步数不够拆阶段"纪律
（nodes.py 2026-09-23 实测注记）一致。要放宽：.env 设 `AGENT_MAX_STEPS`——
一个旋钮管三处，正是本模块存在的目的。

易错点：
- 新增步数类消费点必须读本函数，禁止再写 `os.getenv("AGENT_MAX_STEPS", ...)`
  ——那就是本 bug 的复发形态；
- 语义变更（如拆分"引擎轮数/预算"两个概念）时，先改本模块头注释再改代码，
  并同步 AGENT_DESIGN.md §5.1（该节旧文案"默认 6"记录的是死值，已随本次修正）。
"""

import os

_DEFAULT_AGENT_MAX_STEPS = 5


def get_agent_max_steps() -> int:
    """AGENT_MAX_STEPS 统一解析（下限 1；env 缺省/非法值回落默认，不炸启动）。"""
    raw = (os.getenv("AGENT_MAX_STEPS") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_AGENT_MAX_STEPS
