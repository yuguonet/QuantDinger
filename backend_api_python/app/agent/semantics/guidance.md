## 执行规则

1. 按给出的步骤顺序执行
2. 用 call_skill 调用技能，传入 stock_code
3. 每个 Skill 返回后，继续下一步
4. 最终用 final_answer 返回结果

## call_skill 用法

```
call_skill(skill_name="technical_agent", stock_code="0000001")
```
- 遇到问题需要明确告知
- 关键错误或失败快速退出并输出遇到的问题
- 不能把技能名当函数直接调用
- 每个 Skill 的详细说明见其对应的 semantics/skills/*.md
- 每个 tool 的详细说明在文件@tool装饰符中