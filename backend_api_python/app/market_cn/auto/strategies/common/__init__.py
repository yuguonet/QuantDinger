"""strategies/common/ — 跨策略共享规则（公共接口，无 _ 前缀）。

归属判定：只被 strategies/ 内代码调用 → 本包；被 core/ 调用 → core/runtime/；
只被 tools/ 调用 → tools/common.py。
命名约定（方案 R6）：跨文件公共接口不带 _ 前缀；文件夹名不用下划线。
"""
