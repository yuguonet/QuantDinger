"""
导出板块/概念映射 + 连板股共振分析

在Windows本地运行，连接DB导出概念数据
"""
import os, sys, json, csv
from datetime import datetime

# 确保路径
_backend = os.path.join(os.path.dirname(__file__), "..", "backend_api_python")
sys.path.insert(0, _backend)

def load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend, ".env"), os.path.join(os.path.dirname(__file__), "..", ".env")]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except: pass

def export_stock_concepts():
    """导出全市场股票的概念/行业映射"""
    load_env()
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # 查看 stock_basic_info 表结构
            cur.execute("""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = 'stock_basic_info'
                ORDER BY ordinal_position
            """)
            cols = cur.fetchall()
            print(f"stock_basic_info 列: {[c[0] for c in cols]}")
            
            # 检查是否有 industry/concept 列
            col_names = [c[0] for c in cols]
            
            # 尝试获取所有数据
            cur.execute("SELECT * FROM stock_basic_info LIMIT 5")
            sample = cur.fetchall()
            print(f"样本数据:")
            for row in sample:
                print(f"  {row}")
            
            # 导出全部
            cur.execute("SELECT * FROM stock_basic_info")
            all_rows = cur.fetchall()
            print(f"\n总记录: {len(all_rows)}")
            
            # 保存为CSV
            out_path = os.path.join(os.path.dirname(__file__), "stock_basic_info.csv")
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(col_names)
                for row in all_rows:
                    writer.writerow(row)
            print(f"✅ 已保存: {out_path}")
            
            return col_names, all_rows

def export_sector_mapping():
    """导出概念/行业 → 股票的映射"""
    load_env()
    
    # 尝试多种方式获取概念数据
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # 检查是否有单独的概念表
                cur.execute("""
                    SELECT table_name FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND (table_name LIKE '%concept%' OR table_name LIKE '%sector%' 
                         OR table_name LIKE '%industry%' OR table_name LIKE '%板块%')
                """)
                concept_tables = [r[0] for r in cur.fetchall()]
                print(f"概念相关表: {concept_tables}")
                
                for table in concept_tables:
                    cur.execute(f"SELECT * FROM {table} LIMIT 5")
                    sample = cur.fetchall()
                    cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'")
                    tcols = [r[0] for r in cur.fetchall()]
                    print(f"\n  {table} 列: {tcols}")
                    for row in sample:
                        print(f"    {row}")
    except Exception as e:
        print(f"概念表查询失败: {e}")
    
    # 尝试从 stock_basic_info 的 industry/concept 字段提取
    try:
        from app.utils.db_market import get_market_db_manager
        mgr = get_market_db_manager()
        pool = mgr._get_pool("CNStock")
        
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'stock_basic_info'")
                col_names = [r[0] for r in cur.fetchall()]
                
                # 找概念/行业列
                for col in col_names:
                    if any(k in col.lower() for k in ["concept", "sector", "industry", "板块", "概念", "行业"]):
                        cur.execute(f"SELECT DISTINCT {col} FROM stock_basic_info WHERE {col} IS NOT NULL")
                        vals = [r[0] for r in cur.fetchall()]
                        print(f"\n  {col}: {len(vals)} 个去重值")
                        if len(vals) <= 50:
                            for v in vals[:30]:
                                print(f"    {v}")
                        
                        # 导出映射
                        cur.execute(f"SELECT code, {col} FROM stock_basic_info WHERE {col} IS NOT NULL")
                        mapping = cur.fetchall()
                        out_path = os.path.join(os.path.dirname(__file__), f"mapping_{col}.csv")
                        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                            writer = csv.writer(f)
                            writer.writerow(["code", col])
                            for row in mapping:
                                writer.writerow(row)
                        print(f"  ✅ 已保存: {out_path}")
    except Exception as e:
        print(f"映射导出失败: {e}")

if __name__ == "__main__":
    print("="*60)
    print("  导出板块/概念映射")
    print("="*60)
    export_stock_concepts()
    print()
    export_sector_mapping()
