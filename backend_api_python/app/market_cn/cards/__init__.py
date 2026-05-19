"""
A股看板卡片自注册包

import 本包时自动扫描目录下所有 .py 模块（排除 _ 开头），
各模块通过 register() 将自己挂到全局注册表。
主路由只需调 get_enabled() 即可拿到所有卡片。
"""
import importlib
import pkgutil
import os

_package_dir = os.path.dirname(__file__)
for _finder, _name, _ispkg in pkgutil.iter_modules([_package_dir]):
    if _name.startswith("_"):
        continue
    importlib.import_module(f".{_name}", package=__name__)
