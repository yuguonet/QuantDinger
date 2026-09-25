# strategies/_archive — 半成品 / 退役策略归档

**不参与 autodiscover**（`strategies/__init__.py` 跳过本目录）。
放这里只求「留证据、不碍事」，**不是**可运行策略。

## dragon_callback_legacy.py + .yaml

- 来源：龙回头旧版插件（约 743 行），`dragon_callback` 方案2 上线后遗留。
- 退役原因：不在 `config.json` strategies 段 → `is_enabled` 恒 False，永久黑暗；
  且 `_signal_to_legacy_dict` 仍写 `path="dragon_callback"`，易与现网策略混淆。
- 2026-09-26 auto 全系统扫描（P0-2）移入此处。移出后 autodiscover 不再加载。

## 处置约定

1. 新退役文件：移入本目录 + 在此登记「来源 / 退役原因 / 日期」。
2. 确认无历史依赖后可再移 `D:\QuantDinger\del\`（项目级安全删除约定）。
3. **禁止**把仍在 config.json 或仍在库内有活跃行的策略移进来。
