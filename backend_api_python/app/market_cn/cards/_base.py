"""
卡片自注册协议

每个卡片文件在 import 时调用 register(meta, fetch_fn) 注册自己。
主路由通过 get_enabled() 获取所有已注册卡片，自动挂载路由。
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple, Any


@dataclass
class CardMeta:
    """卡片元数据"""
    id: str                          # 唯一标识（如 "hot_list"）
    name: str                        # 显示名称（如 "同花顺热榜"）
    endpoint: str                    # 子路径（如 "/hot-list"）
    refresh_interval: int = 120      # 建议前端刷新间隔（秒）
    order: int = 50                  # 排序权重，越小越靠前
    enabled: bool = True             # 是否启用
    requires_hub: bool = True        # 是否依赖 cn_stock_hub（缺失时自动返回空）


# 全局注册表：{ card_id: (CardMeta, fetch_fn) }
_registry: Dict[str, Tuple[CardMeta, Callable]] = {}


def register(meta: CardMeta, fetch_fn: Callable):
    """卡片模块 import 时调用，注册到全局表"""
    _registry[meta.id] = (meta, fetch_fn)


def get_all() -> Dict[str, Tuple[CardMeta, Callable]]:
    """返回所有已注册卡片"""
    return dict(_registry)


def get_enabled() -> List[Tuple[CardMeta, Callable]]:
    """返回 enabled=True 的卡片，按 order 排序"""
    items = [(m, f) for m, f in _registry.values() if m.enabled]
    items.sort(key=lambda x: x[0].order)
    return items


def get_meta_list() -> List[dict]:
    """返回所有启用卡片的元数据（给前端 /cards 接口用）"""
    result = []
    for meta, _ in get_enabled():
        result.append({
            "id": meta.id,
            "name": meta.name,
            "endpoint": meta.endpoint,
            "refresh_interval": meta.refresh_interval,
            "order": meta.order,
        })
    return result
