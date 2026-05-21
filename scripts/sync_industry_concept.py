#!/usr/bin/env python3
"""
sync_industry_concept.py
一体化脚本：多源抓取 + 归一化去重 + 直接写库

数据源:
  1. 申万 3 级行业（东财 datacenter API）→ industry 字段
  2. 新浪行业板块（备用）→ industry 字段
  3. 东财概念板块（datacenter API）→ concepts 字段

用法:
  python sync_industry_concept.py              # 抓取 + 写库
  python sync_industry_concept.py --dry-run    # 只看不写
  python sync_industry_concept.py --skip-fetch # 跳过抓取，用缓存直接写库
"""

import json
import sys
import os
import time
import random
import datetime
import argparse
import requests
from pathlib import Path

# ============================================================
# 路径 & 环境
# ============================================================
_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_root / "backend_api_python"))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / "backend_api_python" / ".env")
    load_dotenv(_root / ".env")
except ImportError:
    pass
# 加入项目根目录，让 app 包可被 import
_project_root = _root.parent  # scripts/ → QuantDinger/
sys.path.insert(0, str(_project_root))
# 如果 .env 在 backend_api_python/ 下，也需要显式加载
sys.path.insert(0, str(_project_root / "backend_api_python"))

CACHE_DIR = _root / "ths_data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
})


# ============================================================
# 工具函数
# ============================================================

def smart_sleep(lo=0.5, hi=1.2):
    time.sleep(random.uniform(lo, hi))


def load_cache(name):
    path = CACHE_DIR / f"{name}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_cache(name, data):
    path = CACHE_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_api(url, params, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt) + random.uniform(1, 2))
            else:
                raise


# ============================================================
# 数据源 1: 申万 3 级行业（东财 datacenter API）
# ============================================================

def fetch_sw_industry():
    """获取申万 3 级行业分类"""
    cached = load_cache("sw_industry")
    if cached:
        print(f"[缓存] 申万行业: {len(cached)} 只股票", file=sys.stderr)
        return cached

    print("[抓取] 申万 3 级行业分类...", file=sys.stderr)
    API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    all_items = []
    page = 1

    while True:
        params = {
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "pageSize": 500, "pageNumber": page,
            "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
            "columns": "SECURITY_CODE,BOARD_NAME,BOARD_TYPE",
            "filter": '(BOARD_TYPE="行业")',
        }
        data = fetch_api(API, params)
        if not data.get("success") or not data.get("result"):
            break
        items = data["result"].get("data", [])
        if not items:
            break
        all_items.extend(items)
        total = data["result"].get("count", 0)
        if page % 5 == 0:
            print(f"  进度 {len(all_items)}/{total}...", file=sys.stderr)
        if len(all_items) >= total:
            break
        page += 1
        smart_sleep(0.5, 1.0)

    # 按股票分组，提取 1/2/3 级
    result = {}
    for item in all_items:
        code = item.get("SECURITY_CODE", "").zfill(6)
        name = item.get("BOARD_NAME", "")
        if not code or not name:
            continue
        if code not in result:
            result[code] = {"l1": "", "l2": "", "l3": "", "all": []}
        result[code]["all"].append(name)
        if "Ⅲ" in name:
            result[code]["l3"] = name
        elif "Ⅱ" in name:
            result[code]["l2"] = name
        else:
            result[code]["l1"] = name

    save_cache("sw_industry", result)
    print(f"[完成] 申万行业: {len(result)} 只股票", file=sys.stderr)
    return result


# ============================================================
# 数据源 2: 新浪行业板块（备用）
# ============================================================

def fetch_sina_industry():
    """获取新浪行业板块成分股"""
    cached = load_cache("sina_industry")
    if cached:
        print(f"[缓存] 新浪行业: {len(cached)} 只股票", file=sys.stderr)
        return cached

    print("[抓取] 新浪行业板块...", file=sys.stderr)

    # 获取板块列表
    url = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    r = SESSION.get(url, timeout=20)
    text = r.content.decode("gbk", errors="replace")

    import re
    match = re.search(r'\{(.+)\}', text, re.DOTALL)
    if not match:
        print("  [ERROR] 无法解析新浪行业数据", file=sys.stderr)
        return {}

    raw = json.loads("{" + match.group(1) + "}")
    boards = []
    for key, val in raw.items():
        parts = val.split(",")
        if len(parts) >= 3:
            boards.append({"node": parts[0], "name": parts[1]})

    print(f"  板块: {len(boards)} 个", file=sys.stderr)

    # 获取成分股
    result = {}
    for i, board in enumerate(boards, 1):
        if i % 20 == 0:
            print(f"  进度 {i}/{len(boards)}...", file=sys.stderr)

        node = board["node"]
        name = board["name"]
        all_codes = []
        page = 1

        while page <= 50:
            api = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
            params = {"page": page, "num": 80, "sort": "symbol", "asc": 1, "node": node, "symbol": "", "_s_r_a": "init"}
            try:
                smart_sleep(0.5, 1.0)
                r = SESSION.get(api, params=params, timeout=20)
                text = r.content.decode("gbk", errors="replace")
                if not text or text.strip() in ("null", "[]", ""):
                    break
                data = json.loads(text)
                if not data:
                    break
                for item in data:
                    code = item.get("code", "")
                    if code:
                        all_codes.append(code.zfill(6))
                if len(data) < 80:
                    break
                page += 1
            except Exception:
                break

        for code in all_codes:
            if code not in result:
                result[code] = []
            result[code].append(name)

    save_cache("sina_industry", result)
    print(f"[完成] 新浪行业: {len(result)} 只股票", file=sys.stderr)
    return result


# ============================================================
# 数据源 3: 东财概念板块（datacenter API）
# ============================================================

def fetch_em_concepts():
    """获取东财概念板块成分股"""
    cached = load_cache("em_concepts")
    if cached:
        print(f"[缓存] 东财概念: {len(cached)} 只股票", file=sys.stderr)
        return cached

    print("[抓取] 东财概念板块...", file=sys.stderr)
    API = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    # 先获取所有股票的概念标签（按股票查）
    # 方法：遍历 RPT_F10_CORETHEME_BOARDTYPE，BOARD_TYPE != "行业"
    all_items = []
    page = 1

    while True:
        params = {
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "pageSize": 500, "pageNumber": page,
            "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
            "columns": "SECURITY_CODE,BOARD_NAME,BOARD_TYPE",
            "filter": '(BOARD_TYPE!="行业")',
        }
        data = fetch_api(API, params)
        if not data.get("success") or not data.get("result"):
            break
        items = data["result"].get("data", [])
        if not items:
            break
        all_items.extend(items)
        total = data["result"].get("count", 0)
        if page % 10 == 0:
            print(f"  进度 {len(all_items)}/{total}...", file=sys.stderr)
        if len(all_items) >= total:
            break
        page += 1
        smart_sleep(0.3, 0.8)

    # 按股票分组
    result = {}
    for item in all_items:
        code = item.get("SECURITY_CODE", "").zfill(6)
        name = item.get("BOARD_NAME", "")
        board_type = item.get("BOARD_TYPE", "")
        if not code or not name:
            continue
        # 过滤掉一些无意义的标签
        skip_names = {"融资融券", "深股通", "沪股通", "MSCI中国", "标准普尔", "富时罗素",
                      "HS300_", "上证180_", "上证50_", "央视50_", "东方财富热股",
                      "百元股", "低价股", "高价股", "大盘股", "中盘股", "小盘股",
                      "权重股", "行业龙头", "机构重仓", "证金持股", "社保重仓",
                      "预盈预增", "预亏预减", "业绩报告", "年报", "半年报",
                      "新股", "次新股", "ST板块", "退市警示"}
        if name in skip_names:
            continue
        if code not in result:
            result[code] = []
        if name not in result[code]:
            result[code].append(name)

    save_cache("em_concepts", result)
    print(f"[完成] 东财概念: {len(result)} 只股票", file=sys.stderr)
    return result


# ============================================================
# 归一化 + 合并
# ============================================================

def normalize_industry(sw_data, sina_data):
    """
    合并申万 + 新浪行业，输出 {code: "行业1,行业2,..."}
    优先申万（更标准），新浪补充
    """
    merged = {}
    all_codes = set(sw_data.keys()) | set(sina_data.keys())

    for code in all_codes:
        items = []
        seen = set()

        # 申万优先（兼容 sw_l1/l2/l3 和 l1/l2/l3 两种格式）
        sw = sw_data.get(code, {})
        if isinstance(sw, dict):
            for key in ["sw_l1", "sw_l2", "sw_l3", "l1", "l2", "l3"]:
                val = sw.get(key, "")
                if val and val not in seen:
                    seen.add(val)
                    items.append(val)

        # 新浪补充
        sina = sina_data.get(code, [])
        if isinstance(sina, list):
            for name in sina:
                if name and name not in seen:
                    seen.add(name)
                    items.append(name)

        if items:
            merged[code] = ",".join(items)

    return merged


def normalize_concepts(em_data):
    """
    归一化概念数据，输出 {code: "概念1,概念2,..."}
    """
    merged = {}
    for code, concepts in em_data.items():
        if isinstance(concepts, list) and concepts:
            # 去重保序
            seen = set()
            unique = []
            for c in concepts:
                if c not in seen:
                    seen.add(c)
                    unique.append(c)
            merged[code] = ",".join(unique)
    return merged


# ============================================================
# 写库
# ============================================================

def write_to_db(stock_industry: dict, stock_concepts: dict, dry_run=False):
    """写入 stock_basic_info 表（PostgreSQL）"""

    # dry-run 不需要 DB 连接
    if dry_run:
        all_codes = set(stock_concepts.keys()) | set(stock_industry.keys())
        print(f"\n[Dry-Run] 将更新 {len(all_codes)} 只股票:", file=sys.stderr)
        print(f"  行业更新: {len(stock_industry)} 只", file=sys.stderr)
        print(f"  概念更新: {len(stock_concepts)} 只", file=sys.stderr)
        for code in sorted(all_codes)[:10]:
            ind = stock_industry.get(code, "")
            con = stock_concepts.get(code, "")
            print(f"  {code}: 行业={ind[:50]}  概念={con[:80]}", file=sys.stderr)
        return

    from app.utils.basicinfo_db import get_stock_basic_db

    db = get_stock_basic_db()
    db.ensure_table()

    pool = db._get_pool()

    # 读取已有数据（用于合并 concepts）
    existing_concepts = {}
    with pool.cursor() as cur:
        cur.execute("SELECT symbol, concepts FROM stock_basic_info WHERE concepts IS NOT NULL AND concepts != ''")
        existing_concepts = {row[0]: row[1] for row in cur.fetchall()}

    # 合并 concepts（新数据追加，不覆盖旧的）
    merged_concepts = {}
    for code, new_str in stock_concepts.items():
        old = existing_concepts.get(code, "")
        old_set = set(c.strip() for c in old.split(",") if c.strip())
        new_set = set(c.strip() for c in new_str.split(",") if c.strip())
        combined = sorted(old_set | new_set)
        merged_concepts[code] = ",".join(combined)

    # 收集所有需要更新的 symbol
    all_codes = set(merged_concepts.keys()) | set(stock_industry.keys())

    # 批量写入（PG 用 ON CONFLICT 或直接 UPDATE）
    print(f"\n[写库] 更新 {len(all_codes)} 只股票...", file=sys.stderr)
    now = datetime.datetime.now()
    updated = 0
    batch = []

    with pool.connection() as conn:
        cur = conn.cursor()
        for code in all_codes:
            con = merged_concepts.get(code, "")
            ind = stock_industry.get(code, "")
            batch.append((con, ind, now, code))
            if len(batch) >= 500:
                cur.executemany(
                    "UPDATE stock_basic_info "
                    "SET concepts = CASE WHEN %s != '' THEN %s ELSE concepts END, "
                    "    industry = CASE WHEN %s != '' THEN %s ELSE industry END, "
                    "    updated_at = %s "
                    "WHERE symbol = %s",
                    [(c, c, i, i, t, s) for c, i, t, s in batch],
                )
                updated += len(batch)
                conn.commit()
                batch = []
        if batch:
            cur.executemany(
                "UPDATE stock_basic_info "
                "SET concepts = CASE WHEN %s != '' THEN %s ELSE concepts END, "
                "    industry = CASE WHEN %s != '' THEN %s ELSE industry END, "
                "    updated_at = %s "
                "WHERE symbol = %s",
                [(c, c, i, i, t, s) for c, i, t, s in batch],
            )
            updated += len(batch)
            conn.commit()
        cur.close()

    print(f"✅ 已更新 {updated} 只股票（行业 {len(stock_industry)} 只，概念 {len(merged_concepts)} 只）", file=sys.stderr)


# ============================================================
# main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="多源抓取行业+概念，归一化写库")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    parser.add_argument("--skip-fetch", action="store_true", help="跳过抓取，用缓存")
    args = parser.parse_args()

    print(f"[开始] {datetime.datetime.now().strftime('%H:%M:%S')}", file=sys.stderr)

    # 1. 抓取数据
    if args.skip_fetch:
        sw_data = load_cache("sw_industry") or {}
        sina_data = load_cache("sina_industry") or {}
        em_data = load_cache("em_concepts") or {}
        print(f"[缓存] 申万={len(sw_data)}, 新浪={len(sina_data)}, 概念={len(em_data)}", file=sys.stderr)
    else:
        sw_data = fetch_sw_industry()
        sina_data = fetch_sina_industry()
        em_data = fetch_em_concepts()

    # 2. 归一化
    print("\n[归一化] 合并去重...", file=sys.stderr)
    industry = normalize_industry(sw_data, sina_data)
    concepts = normalize_concepts(em_data)

    print(f"  行业: {len(industry)} 只股票", file=sys.stderr)
    print(f"  概念: {len(concepts)} 只股票", file=sys.stderr)

    # 3. 写库
    write_to_db(industry, concepts, dry_run=args.dry_run)

    print(f"\n[完成] {datetime.datetime.now().strftime('%H:%M:%S')}", file=sys.stderr)


if __name__ == "__main__":
    main()
