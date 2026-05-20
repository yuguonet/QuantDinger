"""
enrich_concepts.py — 从同花顺拉取 A 股概念板块映射，写入 stock_basic_info.concepts

用法：
  python scripts/enrich_concepts.py              # 正式跑（写库）
  python scripts/enrich_concepts.py --dry-run    # 只拉前5个概念测试
  python scripts/enrich_concepts.py --resume     # 断点续跑（跳过已有概念）

数据来源：同花顺 q.10jqka.com.cn（HTTP，无需额外依赖）
  - 概念列表: http://q.10jqka.com.cn/gn/
  - 概念成分股: http://q.10jqka.com.cn/gn/detail/code/{code}/

注意：
  - 在本地 Windows 运行（同花顺对服务器 IP 可能有限制）
  - 全量约 360+ 个概念，每个需请求 1~5 页，约 20-30 分钟
  - 限流 1 秒/请求，避免被封
"""

import sys
import os
import time
import json
import re
import argparse
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ 需要 requests 库: pip install requests")
    sys.exit(1)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "http://q.10jqka.com.cn/gn/",
}

# 断点续跑文件
CHECKPOINT_FILE = Path(__file__).parent / ".enrich_concepts_checkpoint.json"


def _get_session():
    """创建带 cookie 的 session（同花顺需要 cookie 才能翻页）"""
    s = requests.Session()
    s.headers.update(HEADERS)
    # 先访问首页拿 cookie
    try:
        s.get("http://q.10jqka.com.cn/gn/", timeout=10)
    except Exception:
        pass
    return s


def _parse_stock_codes(html: str) -> list[str]:
    """从概念详情页 HTML 中提取个股代码"""
    codes = []
    # 匹配 stockpage.10jqka.com.cn/XXXXXX 的链接
    for m in re.finditer(r'stockpage\.10jqka\.com\.cn/(\d{6})', html):
        code = m.group(1)
        if code not in codes:
            codes.append(code)
    return codes


def _get_page_count(html: str) -> int:
    """从 HTML 中提取总页数"""
    m = re.search(r'page_info">\d+/(\d+)', html)
    if m:
        return int(m.group(1))
    return 1


def fetch_concept_list(session) -> list[dict]:
    """
    获取概念板块列表。
    返回: [{"code": "301496", "name": "白酒概念"}, ...]
    """
    print("[1/3] 获取概念板块列表...")

    resp = session.get("http://q.10jqka.com.cn/gn/", timeout=15)
    resp.encoding = "gbk"
    html = resp.text

    concepts = []
    for m in re.finditer(r'gn/detail/code/(\d+)/"[^>]*>([^<]+)<', html):
        code = m.group(1)
        name = m.group(2).strip()
        if code and name:
            concepts.append({"code": code, "name": name})

    # 去重
    seen = set()
    unique = []
    for c in concepts:
        if c["code"] not in seen:
            seen.add(c["code"])
            unique.append(c)

    print(f"   获取到 {len(unique)} 个概念板块")
    return unique


def fetch_concept_stocks(session, concept_code: str, concept_name: str) -> list[str]:
    """
    获取某概念板块下的所有个股代码（处理分页）。
    """
    all_codes = []

    # 第一页
    url = f"http://q.10jqka.com.cn/gn/detail/code/{concept_code}/"
    try:
        resp = session.get(url, timeout=10)
        resp.encoding = "gbk"
        html = resp.text
    except Exception as e:
        return []

    codes = _parse_stock_codes(html)
    all_codes.extend(codes)

    total_pages = _get_page_count(html)

    # 第 2 页起
    for page in range(2, total_pages + 1):
        time.sleep(1)
        page_url = f"http://q.10jqka.com.cn/gn/detail/code/{concept_code}/field/default/order/desc/page/{page}/ajax/1/"
        try:
            resp = session.get(page_url, timeout=10, headers={
                "Referer": url,
                "X-Requested-With": "XMLHttpRequest",
            })
            resp.encoding = "gbk"
            codes = _parse_stock_codes(resp.text)
            all_codes.extend(codes)
        except Exception:
            break

    # 去重
    return list(dict.fromkeys(all_codes))


def load_checkpoint() -> dict:
    """加载断点"""
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            pass
    return {"done": {}, "stock_concepts": {}}


def save_checkpoint(data: dict):
    """保存断点"""
    CHECKPOINT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def fetch_concept_mapping(dry_run: bool = False, resume: bool = False):
    """
    拉取概念板块映射，返回 {股票代码: [概念1, 概念2, ...]} 字典。
    """
    session = _get_session()

    concepts = fetch_concept_list(session)
    if not concepts:
        print("❌ 获取概念板块列表失败")
        return {}

    if dry_run:
        concepts = concepts[:5]
        print(f"   dry-run 模式，只测试前 5 个概念")

    # 断点续跑
    checkpoint = load_checkpoint() if resume else {"done": {}, "stock_concepts": {}}
    stock_concepts: dict[str, list[str]] = checkpoint.get("stock_concepts", {})
    done_set = set(checkpoint.get("done", {}).keys())

    if resume and done_set:
        print(f"   断点续跑：已完成 {len(done_set)} 个概念，继续剩余")

    success = 0
    failed = 0
    skipped = 0

    print(f"[2/3] 逐个拉取概念成分股（共 {len(concepts)} 个）...")
    for i, concept in enumerate(concepts):
        code = concept["code"]
        name = concept["name"]

        # 跳过已完成的
        if code in done_set:
            skipped += 1
            continue

        codes = fetch_concept_stocks(session, code, name)
        if codes:
            for stock_code in codes:
                stock_concepts.setdefault(stock_code, []).append(name)
            success += 1
            checkpoint["done"][code] = name
        else:
            failed += 1
            # 即使失败也标记为已处理（避免重复尝试空概念）
            checkpoint["done"][code] = name

        # 保存断点（每 10 个概念保存一次）
        if (i + 1) % 10 == 0:
            checkpoint["stock_concepts"] = stock_concepts
            save_checkpoint(checkpoint)

        progress = i + 1
        total = len(concepts)
        if progress % 20 == 0 or progress == total:
            print(f"   进度 {progress}/{total}（成功 {success}，失败 {failed}，跳过 {skipped}，覆盖 {len(stock_concepts)} 只股票）")

        time.sleep(1)

    # 最终保存
    checkpoint["stock_concepts"] = stock_concepts
    save_checkpoint(checkpoint)

    print(f"   完成：成功 {success}，失败 {failed}，跳过 {skipped}，覆盖 {len(stock_concepts)} 只股票")
    return stock_concepts


def write_to_db(stock_concepts: dict[str, list[str]]):
    """
    将概念映射写入 stock_basic_info 表的 concepts 字段。
    """
    from app.utils.basicinfo_db import get_stock_basic_db

    db = get_stock_basic_db()
    db.ensure_table()

    print("[3/3] 写入数据库...")

    pool = db._get_pool()
    with pool.cursor() as cur:
        cur.execute("SELECT symbol, concepts FROM stock_basic_info WHERE concepts != ''")
        existing = {row[0]: row[1] for row in cur.fetchall()}

    merged = {}
    for code, new_concepts in stock_concepts.items():
        old = existing.get(code, "")
        old_set = set(c.strip() for c in old.split(",") if c.strip())
        new_set = set(new_concepts)
        combined = sorted(old_set | new_set)
        merged[code] = ",".join(combined)

    updated = 0
    now = __import__("datetime").datetime.now()

    with pool.connection() as conn:
        cur = conn.cursor()
        batch = []
        for code, concepts in merged.items():
            batch.append((concepts, now, code))
            if len(batch) >= 500:
                cur.executemany(
                    "UPDATE stock_basic_info SET concepts = %s, updated_at = %s "
                    "WHERE symbol = %s AND (concepts IS NULL OR concepts = '')",
                    batch,
                )
                updated += cur.rowcount
                batch = []
        if batch:
            cur.executemany(
                "UPDATE stock_basic_info SET concepts = %s, updated_at = %s "
                "WHERE symbol = %s AND (concepts IS NULL OR concepts = '')",
                batch,
            )
            updated += cur.rowcount
        conn.commit()
        cur.close()

    print(f"✅ 已更新 {updated} 只股票的概念标签")


def main():
    parser = argparse.ArgumentParser(description="从同花顺拉取 A 股概念板块映射")
    parser.add_argument("--dry-run", action="store_true", help="只拉前5个概念测试，不写库")
    parser.add_argument("--resume", action="store_true", help="断点续跑（跳过已完成的概念）")
    args = parser.parse_args()

    stock_concepts = fetch_concept_mapping(dry_run=args.dry_run, resume=args.resume)
    if not stock_concepts:
        print("❌ 没有获取到任何概念数据")
        return

    if args.dry_run:
        print("\n--- 示例 ---")
        for i, (code, concepts) in enumerate(list(stock_concepts.items())[:15]):
            print(f"  {code}: {', '.join(concepts)}")
        print(f"\n总计覆盖 {len(stock_concepts)} 只股票（前5个概念），dry-run 模式不写库")
        print(f"提示：确认无误后运行 python scripts/enrich_concepts.py 正式写入")
    else:
        write_to_db(stock_concepts)
        # 写入成功后清理断点文件
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
            print("   已清理断点文件")


if __name__ == "__main__":
    main()
