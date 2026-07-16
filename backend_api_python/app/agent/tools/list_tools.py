# -*- coding: utf-8 -*-
"""
list_tools — 工具发现

扫描 tools/ 目录，列出所有可用工具。
按 domain 过滤：空=通用工具，指定领域=领域+通用，"all"=全部。
"""
import inspect
import importlib
from pathlib import Path

# 跳过的文件（框架文件，非工具）
_SKIP_FILES = {"__init__", "base", "registry", "em_utils", "pagination",
               "screener_config", "mcp_bridge", "cache_tools", "list_tools", "search_tools"}

# tools 目录路径
_TOOLS_DIR = Path(__file__).resolve().parent


def _get_scan_dirs(domain: str) -> list[tuple[Path, str]]:
    """根据 domain 参数确定扫描目录。"""
    if domain == "all":
        dirs = [(_TOOLS_DIR, "tools")]
        for item in sorted(_TOOLS_DIR.iterdir()):
            if item.is_dir() and item.name != "__pycache__" and not item.name.startswith("."):
                dirs.append((item, f"tools.{item.name}"))
        return dirs
    elif domain:
        dirs = [(_TOOLS_DIR, "tools")]
        domain_dir = _TOOLS_DIR / domain
        if domain_dir.is_dir():
            dirs.append((domain_dir, f"tools.{domain}"))
        return dirs
    else:
        return [(_TOOLS_DIR, "tools")]


def _scan_tools(domain: str) -> list[dict]:
    """扫描目录，返回所有工具元数据列表。"""
    all_tools = []
    for scan_dir, module_prefix in _get_scan_dirs(domain):
        for py_file in sorted(scan_dir.glob("*.py")):
            if py_file.stem in _SKIP_FILES:
                continue
            try:
                mod = importlib.import_module(f"{module_prefix}.{py_file.stem}")
            except Exception:
                continue

            for attr_name in dir(mod):
                if attr_name.startswith("_"):
                    continue
                func = getattr(mod, attr_name)
                if not callable(func) or inspect.isclass(func):
                    continue
                if getattr(func, "__module__", "") != mod.__name__:
                    continue

                doc = (func.__doc__ or "").strip()
                desc = doc.split("\n")[0][:120] if doc else f"{attr_name}()"
                domain_name = scan_dir.name if scan_dir != _TOOLS_DIR else "common"

                # 从 docstring Args 段落提取参数描述
                param_descs = {}
                in_args = False
                for line in doc.split("\n")[1:]:
                    stripped = line.strip()
                    if stripped.startswith("Args:") or stripped.startswith("参数:"):
                        in_args = True
                        continue
                    if in_args:
                        if ":" in stripped:
                            parts = stripped.split(":", 1)
                            pname = parts[0].strip()
                            # 参数名不含空格（排除新段落标题）
                            if pname and " " not in pname:
                                param_descs[pname] = parts[1].strip()[:20]
                            else:
                                break
                        elif stripped:
                            break

                # 提取函数签名（优先用参数描述，fallback 到类型注解）
                try:
                    sig = inspect.signature(func)
                    params = []
                    for p in sig.parameters.values():
                        if p.name in param_descs:
                            params.append(f"{p.name}: {param_descs[p.name]}")
                        elif p.annotation != inspect.Parameter.empty:
                            ann = p.annotation
                            params.append(f"{p.name}: {ann.__name__}" if hasattr(ann, '__name__') else f"{p.name}: {ann}")
                        else:
                            params.append(p.name)
                    sig_str = ", ".join(params)
                except Exception:
                    sig_str = ""

                all_tools.append({
                    "name": attr_name,
                    "module": py_file.stem,
                    "desc": desc,
                    "domain": domain_name,
                    "sig": sig_str,
                })
    return all_tools


def list_tools(domain: str = "") -> str:
    """
    列出所有可用工具。

    Args:
        domain: 领域名称。
                - 空/不指定：只列出 tools/ 根目录（通用工具）
                - 指定领域：列出 tools/{domain}/ + tools/ 根目录
                - "all"：列出所有领域所有工具

    Returns:
        工具列表（格式化字符串）

    示例：
        list_tools()           # 列出所有通用工具
        list_tools("finance")  # 列出 finance 领域 + 通用工具
        list_tools("all")      # 列出所有领域所有工具
    """
    all_tools = _scan_tools(domain)

    title = f"可用工具 ({len(all_tools)})："
    lines = [title]
    for t in all_tools:
        sig = t.get("sig", "")
        if sig:
            lines.append(f"  - {t['name']}({sig}) — {t['desc']}")
        else:
            lines.append(f"  - {t['name']}() — {t['desc']}")
    return "\n".join(lines)
