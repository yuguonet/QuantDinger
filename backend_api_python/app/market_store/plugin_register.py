#!/usr/bin/env python3
"""
plugin_register.py — 将 market_local 插件注册到 QuantDinger Flask 应用

用法:
  方式1: 在 QuantDinger 的 app/__init__.py 中添加一行:
         from app.routes.market_local_plugin import market_local_bp
         app.register_blueprint(market_local_bp, url_prefix="/api/market-local")

  方式2: 运行本脚本自动注入:
         python plugin_register.py /path/to/QuantDinger/backend_api_python

  方式3: Docker 环境中挂载本目录，通过环境变量启用:
         MARKET_PLUGIN_ENABLED=true
"""

from __future__ import annotations

import sys
import os
from pathlib import Path


def register_to_init_py(backend_dir: str | Path):
    """
    自动将 blueprint 注册代码注入到 QuantDinger 的 app/__init__.py。
    """
    backend = Path(backend_dir)
    init_file = backend / "app" / "__init__.py"

    if not init_file.exists():
        print(f"ERROR: {init_file} not found")
        return False

    content = init_file.read_text()

    # 检查是否已注册
    if "market_local_bp" in content:
        print("already registered, skip")
        return True

    # 注入代码
    inject = '''
# === market_local plugin (auto-registered) ===
try:
    import sys, os
    _plugin_dir = os.environ.get("MARKET_STORE_DIR", os.path.join(os.path.dirname(__file__), "..", "market_feather_store"))
    if os.path.isdir(_plugin_dir) and _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)
    from plugin_api import market_local_bp
    app.register_blueprint(market_local_bp, url_prefix="/api/market-local")
    print("[market_local] plugin registered at /api/market-local")
except Exception as _ml_err:
    print(f"[market_local] plugin registration failed: {_ml_err}")
# === end market_local plugin ===
'''

    # 在 create_app 函数末尾注入
    # 寻找 "return app" 行，在之前插入
    lines = content.split("\n")
    new_lines = []
    injected = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("return app") and not injected:
            # 缩进匹配
            indent = line[:len(line) - len(line.lstrip())]
            inject_indented = "\n".join(
                indent + l if l.strip() else l
                for l in inject.strip().split("\n")
            )
            new_lines.append(inject_indented)
            injected = True
        new_lines.append(line)

    if not injected:
        # 没找到 return app，追加到文件末尾
        new_lines.append(inject)

    init_file.write_text("\n".join(new_lines))
    print(f"registered market_local plugin to {init_file}")
    return True


def create_wrapper_file(backend_dir: str | Path):
    """
    在 QuantDinger 的 app/routes/ 下创建包装文件。
    这种方式不需要修改 __init__.py，只需在 __init__.py 的 blueprint 列表中添加。
    """
    backend = Path(backend_dir)
    routes_dir = backend / "app" / "routes"

    if not routes_dir.is_dir():
        print(f"ERROR: {routes_dir} not found")
        return False

    wrapper = routes_dir / "market_local_plugin.py"
    wrapper_content = '''"""
market_local_plugin.py — Wrapper to import market_local blueprint.
Place market_feather_store/ next to backend_api_python/ directory,
or set MARKET_STORE_DIR env var to its location.
"""
import sys, os

_plugin_dir = os.environ.get(
    "MARKET_STORE_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "market_feather_store"),
)
if os.path.isdir(_plugin_dir) and _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

try:
    from plugin_api import market_local_bp
except ImportError as e:
    # 提供一个空 blueprint 以防导入失败不影响主应用
    from flask import Blueprint
    market_local_bp = Blueprint("market_local_fallback", __name__)

    @market_local_bp.route("/status", methods=["GET"])
    def _status():
        return {"code": 0, "msg": f"market_local plugin unavailable: {e}"}
'''

    wrapper.write_text(wrapper_content)
    print(f"created wrapper at {wrapper}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python plugin_register.py /path/to/QuantDinger/backend_api_python")
        print()
        print("This will:")
        print("  1. Create wrapper file in app/routes/market_local_plugin.py")
        print("  2. Inject blueprint registration into app/__init__.py")
        sys.exit(1)

    backend_dir = Path(sys.argv[1])
    if not backend_dir.is_dir():
        print(f"ERROR: {backend_dir} is not a directory")
        sys.exit(1)

    print(f"Registering market_local plugin to: {backend_dir}")
    print()

    ok1 = create_wrapper_file(backend_dir)
    ok2 = register_to_init_py(backend_dir)

    if ok1 and ok2:
        print()
        print("✓ Registration complete!")
        print()
        print("Make sure market_feather_store/ is accessible at one of:")
        print(f"  - {backend_dir}/market_feather_store/")
        print(f"  - or set MARKET_STORE_DIR env var")
        print()
        print("API endpoints will be available at:")
        print("  /api/market-local/overview    — 最新市场 + 评分")
        print("  /api/market-local/query       — 条件查询")
        print("  /api/market-local/score       — 市场评分")
        print("  /api/market-local/sentiment   — 恐贪/VIX/DXY")
        print("  /api/market-local/symbol/<s>  — 标的历史")
        print("  /api/market-local/anomalies   — 急剧变化")
        print("  /api/market-local/stats       — 存储统计")
        print("  /api/market-local/fetch  [POST] — 触发采集")
        print("  /api/market-local/prune  [POST] — 清理过期")
    else:
        print()
        print("✗ Registration had errors")
        sys.exit(1)


if __name__ == "__main__":
    main()
