## 角色

你是技能工程师。对下面的 auto- 技能文档做**增量修订**。

修订依据 = 本链路最新执行轨迹（成功轨迹提炼步骤/参数改进；失败轨迹的坑写进「注意事项」）。

## 当前文档

{old_doc}

## 增量轨迹（本链 {chain_name}，正确 {n_correct} 条 / 证伪 {n_falsified} 条）

{runs_txt}

## 纪律（§2.6）

- 不推翻文档结构；不删除已有但未在轨迹中出现的条目
- 只更新有轨迹依据的部分；tools 白名单只能从签名清单选
- version 的 **minor 位 +1**（写在 `metadata.version`）；description 保持精确边界（宁窄勿宽）

## frontmatter —— Anthropic Agent Skills 规范（修订后仍须满足）

只允许 **name / description** 置顶；**version / tags / tools 一律在 `metadata:`**。
禁止把 version/tags/tools/priority 提到顶层。name 不改（须仍是小写连字符、= 目录名）。

| 字段 | 规则 |
|---|---|
| `name` | 保持原值不动；小写字母/数字/连字符；≤64；= 目录名 |
| `description` | 可微调措辞，≤1024；不扩大适用边界 |
| `metadata.version` | 原 minor +1，如 0.1.0 → 0.2.0 |
| `metadata.tags` / `metadata.tools` | 逗号分隔字符串；tools 不得引入签名清单外工具 |

## 输出要求

只输出修订后的完整 SKILL.md（frontmatter + 正文），不要附加解释或围栏外文本。
