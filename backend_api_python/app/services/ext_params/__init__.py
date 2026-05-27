"""
ext_params — IndicatorStrategy 扩展参数插件系统

用法（在 IndicatorStrategy 脚本中）：
    info = query_stock()                # 当前标的
    info = query_stock('600519')        # 指定股票
    df['turnover_rate']                 # 自动注入的衍生列

插件开发：
    在本目录下新建 .py 文件，实现 register(ctx) 函数即可。
    ctx 提供 symbol / df / backtest_params 等上下文。
    返回 dict，key 为注入到脚本沙盒的变量名，value 为值。
"""
import importlib
import os
import pkgutil
import logging
from typing import Any, Dict, List, Callable

logger = logging.getLogger(__name__)

# 所有已注册的插件
_providers: List[Callable] = []


def provider(func):
    """装饰器：标记一个函数为扩展参数提供者。

    被标记的函数接收 ctx(dict) 参数，返回 dict。
    返回的 dict 会合并到 IndicatorStrategy 脚本的执行环境中。
    """
    _providers.append(func)
    return func


def _auto_discover():
    """自动扫描本目录下所有 .py 模块，触发 @provider 装饰器注册。"""
    pkg_dir = os.path.dirname(__file__)
    for finder, name, is_pkg in pkgutil.iter_modules([pkg_dir]):
        if name.startswith('_'):
            continue
        try:
            importlib.import_module(f'.{name}', package=__name__)
        except Exception as e:
            logger.warning("ext_params: 加载插件 %s 失败: %s", name, e)


def collect_extras(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """收集所有插件提供的扩展变量。

    Args:
        ctx: 上下文字典，包含:
            - symbol: 当前回测标的代码
            - market: 市场类型
            - df: 当前 DataFrame
            - backtest_params: 回测参数

    Returns:
        合并后的 dict，直接 update 到 local_vars 即可。
    """
    if not _providers:
        _auto_discover()

    extras: Dict[str, Any] = {}
    for prov in _providers:
        try:
            result = prov(ctx)
            if result and isinstance(result, dict):
                extras.update(result)
        except Exception as e:
            logger.debug("ext_params: 插件 %s 执行失败: %s", prov.__name__, e)
    return extras
