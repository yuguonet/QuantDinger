# Platform Policy

{% if system == "Windows" %}
## Windows 环境
- 路径使用反斜杠 `\`，但 Python 代码中建议使用正斜杠 `/` 或 `pathlib.Path`
- 命令行默认使用 PowerShell，也可使用 cmd
- 文件路径注意盘符（如 `D:\`）
{% elif system == "Darwin" %}
## macOS 环境
- 路径使用正斜杠 `/`
- 命令行使用 zsh 或 bash
- 注意 Homebrew 安装路径可能在 `/opt/homebrew/` (Apple Silicon) 或 `/usr/local/` (Intel)
{% else %}
## Linux 环境
- 路径使用正斜杠 `/`
- 命令行使用 bash
- 注意文件权限和用户权限
{% endif %}

## 通用规则
- 所有文件操作使用 Python 的 `pathlib.Path` 或 `os.path` 处理路径
- 涉及外部命令时，注意跨平台兼容性
- 临时文件使用 `tempfile` 模块管理
