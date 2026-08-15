#!/usr/bin/env python3
"""
龙虎榜 & 热榜 历史数据导入工具

独立运行脚本，将历史数据写入 PostgreSQL (CNStock_db)。
支持指定日期范围和数据源。

用法:
    # 导入最近30天的龙虎榜 + 当天热榜
    python scripts/run_import_history.py

    # 导入指定日期范围
    python scripts/run_import_history.py --start 2025-01-01 --end 2025-07-31

    # 仅导入龙虎榜（不含热榜）
    python scripts/run_import_history.py --no-hot-rank

    # 指定数据源
    python scripts/run_import_history.py --source akshare

数据源说明:
    auto      东财搜索优先，AkShare 兜底（默认）
    akshare   仅用 AkShare（支持历史日期范围）
    em        仅用东财搜索（仅当天数据）
"""

import sys
import os
import argparse
from datetime import datetime, timedelta

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 加载 .env 文件（与项目其他入口保持一致）
try:
    from dotenv import load_dotenv
    _dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    load_dotenv(_dotenv_path, override=False)
except ImportError:
    pass  # dotenv 未安装时跳过


def check_database_url():
    """检查 DATABASE_URL 环境变量"""
    db_url = os.getenv('DATABASE_URL', '').strip()
    if not db_url:
        print("❌ 错误: DATABASE_URL 环境变量未设置")
        print()
        print("请在 backend_api_python/.env 文件中添加:")
        print("  DATABASE_URL=postgresql://用户名:密码@localhost:5432/quantdinger")
        print()
        print("或者设置系统环境变量:")
        print("  PowerShell: $env:DATABASE_URL = 'postgresql://user:password@localhost:5432/quantdinger'")
        print("  CMD:        set DATABASE_URL=postgresql://user:password@localhost:5432/quantdinger")
        sys.exit(1)
    return db_url


def check_database_connection():
    """测试数据库连接"""
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            print("✅ 数据库连接成功")
            return True
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        print()
        print("请检查:")
        print("  1. PostgreSQL 服务是否启动")
        print("  2. DATABASE_URL 配置是否正确")
        print("  3. 数据库用户是否有权限")
        sys.exit(1)


from app.market_cn.dragon_tiger_store import import_history


def main():
    parser = argparse.ArgumentParser(
        description="龙虎榜 & 热榜 历史数据导入工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                              # 导入最近30天龙虎榜 + 当天热榜
  %(prog)s --start 2025-01-01           # 从指定日期导入到今天
  %(prog)s --start 2025-01-01 --end 2025-06-30  # 指定日期范围
  %(prog)s --no-hot-rank                # 仅导入龙虎榜
  %(prog)s --source akshare             # 使用 AkShare 数据源
        """,
    )

    parser.add_argument(
        "--start", "-s",
        type=str,
        default="",
        help="开始日期 YYYY-MM-DD（默认30天前）",
    )
    parser.add_argument(
        "--end", "-e",
        type=str,
        default="",
        help="结束日期 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument(
        "--no-hot-rank",
        action="store_true",
        help="不导入热榜数据",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "akshare", "em"],
        default="auto",
        help="数据源: auto=东财优先/AkShare兜底, akshare=仅AkShare, em=仅东财（默认: auto）",
    )

    args = parser.parse_args()

    # 检查数据库配置
    check_database_url()
    check_database_connection()

    # 验证日期格式
    for date_str, label in [(args.start, "开始日期"), (args.end, "结束日期")]:
        if date_str:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                print(f"错误: {label} 格式不正确，应为 YYYY-MM-DD: {date_str}")
                sys.exit(1)

    print("=" * 60)
    print("龙虎榜 & 热榜 历史数据导入")
    print("=" * 60)
    print(f"日期范围: {args.start or '(30天前)'} ~ {args.end or '(今天)'}")
    print(f"数据源: {args.source}")
    print(f"导入热榜: {'否' if args.no_hot_rank else '是'}")
    print("-" * 60)

    # 执行导入
    result = import_history(
        start_date=args.start,
        end_date=args.end,
        include_hot_rank=not args.no_hot_rank,
        source=args.source,
    )

    # 输出结果
    print("\n" + "=" * 60)
    print("导入结果")
    print("=" * 60)

    dt = result.get("dragon_tiger", {})
    hr = result.get("hot_rank", {})
    status = result.get("status", "unknown")

    print(f"龙虎榜:")
    print(f"  日期范围: {dt.get('date_range', 'N/A')}")
    print(f"  获取条数: {dt.get('total', 0)}")
    print(f"  写入条数: {dt.get('written', 0)}")

    if not args.no_hot_rank:
        print(f"热榜:")
        print(f"  获取条数: {hr.get('total', 0)}")
        print(f"  写入条数: {hr.get('written', 0)}")

    print(f"\n状态: {status}")

    if status == "error":
        print("\n❌ 导入过程中出现错误，请查看日志")
        sys.exit(1)
    else:
        print("\n✅ 导入完成")


if __name__ == "__main__":
    main()
