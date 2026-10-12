"""tools/common.py — 调试工具层公共接口（无 _ 前缀，跨文件共用）。

P0 搬移（2026-10-12 策略接口统一方案）：收拢 tools/ 下重复实现。
命名约定（方案 R6）：跨文件公共接口不带 _ 前缀。
"""
import os


def load_env() -> None:
    """加载 .env（DB 连接等），静默失败。

    候选路径并集（原 5 处手写副本的超集）：cwd/.env → 项目根/.env → 上级/.env。
    内部走 core._paths.load_env_first_found（ENV_FILE 兜底，幂等）。
    """
    try:
        from app.market_cn.auto.core._paths import load_env_first_found
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        load_env_first_found(
            os.path.join(os.getcwd(), ".env"),
            os.path.join(here, ".env"),
            os.path.join(os.path.dirname(here), ".env"),
        )
    except Exception:
        pass


def yaml_path(key: str) -> str:
    """策略宏 `<key>.yaml` 的绝对路径；文件不存在返回 ""。"""
    from app.market_cn.auto.core._paths import STRATEGY_DIR
    p = os.path.join(STRATEGY_DIR, f"{key}.yaml")
    return p if os.path.isfile(p) else ""
