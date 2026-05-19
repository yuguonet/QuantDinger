"""Hub 辅助模块 — 统一获取 AShareDataHub，缺失时返回 None"""


def get_hub():
    """获取 AShareDataHub 实例，cn_stock_hub 未就绪时返回 None"""
    try:
        from app.data_sources.factory import DataSourceFactory
        from app.interfaces.cn_stock_hub import AShareDataHub
        source = DataSourceFactory.get_source("CNStock")
        return AShareDataHub(sources=[source])
    except Exception:
        return None
