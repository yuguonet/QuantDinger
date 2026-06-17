# Agent 指令

## 工作区

存放项目偏好、工作流约定和 agent 记忆指令。

- 用户持久信息 → `USER.md`
- 风格指导 → `SOUL.md`
- 长期记忆 → `memory/MEMORY.md`

## 定时提醒

1. 创建提醒前先检查可用技能并遵循其指导。
2. 用内置 `cron` 工具管理定时任务（不要通过 `exec` 调用 `nanobot cron`）。
3. 从当前会话提取 `USER_ID` 和 `CHANNEL`（如 `telegram:8281248569` → `8281248569` + `telegram`）。

> **注意：** 不要仅将提醒写入 `MEMORY.md`——不会触发实际通知。

## 心跳任务

`HEARTBEAT.md` 由 `nanobot gateway` 注册的受保护心跳定时任务周期性检查（需 `gateway.heartbeat.enabled` 为 `true`）。除非用户禁用内置心跳并明确需要自定义调度，否则**不要创建重复心跳任务**。

### 文件操作

| 场景 | 工具 |
|---|---|
| 多行增删改 | `apply_patch` |
| 小范围精确替换 | `edit_file` |
| 首次创建或全文重写 | `write_file` |

### 任务类型

- **周期性心跳任务** → 更新 `HEARTBEAT.md`（非一次性提醒）
- **独立提醒 / 自定义调度** → 使用内置 `cron` 工具