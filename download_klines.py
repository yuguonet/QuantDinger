#!/usr/bin/env python3
"""
下载200只随机A股(排除大盘蓝筹)的前复权K线数据, 保存到 kline_data.json
使用腾讯K线API
"""
import json, time, random, requests, sys

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

# 大盘蓝筹黑名单 (排除)
BLUE_CHIPS = {
    # 银行
    "601398", "601939", "601288", "601988", "601328", "600036", "600016", "600000",
    "601166", "600015", "601818", "601998", "600919", "601229", "600926", "601009",
    "600010", "601360", "002142", "601169", "600011", "601577", "600908", "601128",
    "601838", "600928", "601077", "601963",
    # 保险
    "601318", "601628", "601601", "601336", "601319",
    # 券商
    "601688", "601211", "600030", "601881", "600837", "601066",
    # 石油石化
    "601857", "600028", "600688", "600871", "601808",
    # 电力
    "600900", "601985", "600011", "600025", "601991",
    # 白酒
    "600519", "000858", "000568", "002304", "600809", "000596", "603369",
    # 医药大白马
    "600276", "000661", "300760", "300015", "600436",
    # 家电
    "000333", "000651", "600690",
    # 地产
    "001979", "600048", "000002", "000069", "600383",
    # 交运
    "601006", "600029", "601111", "600115",
    # 钢铁/煤炭
    "600019", "601088", "601898", "601225",
    # 通信
    "600050", "601728",
    # 汽车
    "600104", "601238", "000625", "600741",
    # 其他大蓝筹
    "603288", "601668", "601669", "600585", "601186", "601766", "601390",
    "600031", "600309", "600346", "601236", "601601", "601857",
}

def _code_to_sina(code):
    c = code.strip().replace(".", "").replace("SH", "").replace("SZ", "")
    if c.startswith(("6", "5")): return f"sh{c}"
    elif c.startswith(("0", "3", "2")): return f"sz{c}"
    elif c.startswith("68"): return f"sh{c}"
    return ""

def fetch_kline(code, count=300):
    tc = _code_to_sina(code)
    if not tc: return []
    try:
        resp = SESSION.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{tc},day,,,{count},qfq"},
            headers={"Referer": "https://gu.qq.com/"}, timeout=10,
        )
        data = resp.json()
        if not isinstance(data, dict) or int(data.get("code", 0)) != 0: return []
        root = (data.get("data") or {}).get(tc)
        if not isinstance(root, dict): return []
        rows = root.get("qfqday") or root.get("day") or []
        bars = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6: continue
            try:
                bars.append({
                    "time": str(r[0])[:10], "open": float(r[1]),
                    "high": float(r[3]), "low": float(r[4]),
                    "close": float(r[2]), "volume": float(r[5]) * 100,
                })
            except: continue
        bars.sort(key=lambda x: x["time"])
        return bars
    except:
        return []

def generate_stock_pool():
    """生成A股股票池(排除蓝筹)"""
    codes = []
    # 沪主板 600xxx, 603xxx, 605xxx
    for i in range(600000, 606000):
        codes.append(str(i))
    # 深主板 000xxx, 001xxx, 002xxx, 003xxx
    for i in range(0, 4000):
        codes.append(f"{i:06d}")
    # 创业板 300xxx, 301xxx
    for i in range(300000, 302000):
        codes.append(str(i))
    # 科创板 688xxx
    for i in range(688000, 689000):
        codes.append(str(i))
    
    # 排除蓝筹
    codes = [c for c in codes if c not in BLUE_CHIPS]
    return codes

def main():
    target = 200
    pool = generate_stock_pool()
    random.shuffle(pool)
    
    print(f"股票池: {len(pool)}只 (已排除蓝筹)")
    print(f"目标: 下载{target}只的K线数据\n")
    
    result = {}
    tried = 0
    
    for code in pool:
        if len(result) >= target:
            break
        tried += 1
        
        bars = fetch_kline(code, 300)
        if not bars or len(bars) < 100:
            if tried % 50 == 0:
                print(f"  已尝试{tried}只, 成功{len(result)}只...")
            continue
        
        # 排除日均成交额太低的(可能是ST或停牌)
        recent = bars[-20:]
        avg_vol = sum(b['volume'] for b in recent) / len(recent)
        avg_price = sum(b['close'] for b in recent) / len(recent)
        avg_amount = avg_vol * avg_price
        if avg_amount < 5_000_000:  # 日均成交额<500万
            continue
        
        result[code] = bars
        
        if len(result) % 20 == 0:
            print(f"  进度: {len(result)}/{target} (已尝试{tried}只)")
        
        time.sleep(0.15)
    
    print(f"\n完成: 成功下载 {len(result)} 只股票")
    
    out_path = "kline_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print(f"保存到: {out_path} ({len(result)} 只)")
    
    # 打印统计
    total_bars = sum(len(v) for v in result.values())
    print(f"总K线数: {total_bars}")

if __name__ == "__main__":
    main()
