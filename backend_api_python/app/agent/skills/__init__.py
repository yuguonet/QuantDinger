# -*- coding: utf-8 -*-
"""Skills 模块 — 可组合的能力单元。

兼容性契约（对外承诺，变更需评审）：
  - Tool 完全兼容 OpenAI Function Calling 标准（JSON Schema 参数描述）；
  - Skill 兼容 Anthropic SKILL 标准（<目录>/SKILL.md + frontmatter：
    name / version / description / tags / tools）。

目录约定：
  - <name>/SKILL.md            人工/LLM 编写的技能（markdown 型，零配置注册）；
  - <name>/run.py + base.py    Python 类技能（继承 base.Skill，可被 markdown 引用）；
  - auto_*/SKILL.md            **机器酿造**技能（chain/skill_brewer 从 qd_traces
    高频且验证通过的节点树自动编译，文件头标注来源 trace 与日期）。auto_ 前缀
    用于区分"自动生成"与"人工维护"：人工可以直接编辑 auto_ 技能（改后建议去掉
    前缀接管），也可以整体删除让酿造器重新生成。
"""
