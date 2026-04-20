#!/usr/bin/env python3
"""
cnstock_search — 东方财富条件选股封装

用法:
  from cnstock_search import cnstock_search

  cs = cnstock_search()
  result = cs.search("放量突破")
  print(result)

CLI:
  python cnstock_search.py "放量突破"
  python cnstock_search.py "MACD金叉" --page-size 20 --page-no 1
  python cnstock_search.py "底部放量" --flat
"""

import json
import random
import string
import time
from urllib import request, error


class cnstock_search:
    API_URL = "https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code"

    def __init__(self, page_size: int = 100, biz: str = "web_ai_select_stocks", client: str = "WEB"):
        self.page_size = page_size
        self.biz = biz
        self.client = client

    # ── 内部工具 ────────────────────────────────────────

    @staticmethod
    def _gen_id(n: int) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    @staticmethod
    def _extract_signal(row: dict) -> str | None:
        for k in row:
            if k.startswith("BREAK_THROUGH"):
                return row[k]
        return None

    def _normalize(self, s: dict, i: int) -> dict:
        chg = float(s.get("CHG") or 0)
        return {
            "serial":    s.get("SERIAL", i + 1),
            "code":      s.get("SECURITY_CODE"),
            "name":      s.get("SECURITY_SHORT_NAME"),
            "price":     s.get("NEWEST_PRICE"),
            "change":    round(chg, 2),
            "changeDir": "up" if chg > 0 else "down" if chg < 0 else "flat",
            "turnover":  float(s["TURNOVER_RATE"]) if s.get("TURNOVER_RATE") else None,
            "qrr":       float(s["QRR"]) if s.get("QRR") else None,
            "volume":    s.get("TRADING_VOLUMES"),
            "pe":        float(s["PE_DYNAMIC"]) if s.get("PE_DYNAMIC") else None,
            "marketCap": s.get("TOEAL_MARKET_VALUE") or s.get("TOAL_MARKET_VALUE<140>"),
            "signal":    self._extract_signal(s),
        }

    # ── 公开方法 ────────────────────────────────────────

    def search(self, keyword: str, page_no: int = 1, page_size: int | None = None) -> dict:
        """选股搜索，返回结构化 JSON"""
        if not keyword or not keyword.strip():
            return {"ok": False, "msg": "keyword 不能为空"}

        kw = keyword.strip()
        ps = page_size or self.page_size
        ts = str(int(time.time() * 1000))

        body = {
            "needAmbiguousSuggest": True,
            "pageSize":             ps,
            "pageNo":               page_no,
            "fingerprint":          self._gen_id(32),
            "matchWord":            "",
            "shareToGuba":          False,
            "timestamp":            ts,
            "requestId":            self._gen_id(32) + ts,
            "removedConditionIdList": [],
            "ownSelectAll":         False,
            "needCorrect":          True,
            "client":               self.client,
            "product":              "",
            "needShowStockNum":     False,
            "biz":                  self.biz,
            "xcId":                 "",
            "gids":                 [],
            "dxInfoNew":            [],
            "keyWordNew":           kw,
            "customDataNew":        json.dumps([{"type": "text", "value": kw, "extra": ""}]),
        }

        req = request.Request(
            self.API_URL,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except error.URLError as e:
            return {"ok": False, "msg": f"请求失败: {e}"}

        ok = str(data.get("code")) == "100"
        res = data.get("data", {}).get("result", {}) if data.get("data") else {}
        raw_list = res.get("dataList", [])

        return {
            "ok":       ok,
            "code":     data.get("code"),
            "msg":      data.get("msg"),
            "total":    res.get("total", len(raw_list)),
            "keyword":  kw,
            "pageNo":   page_no,
            "pageSize": ps,
            "stocks":   [self._normalize(s, i) for i, s in enumerate(raw_list)],
        }

    def search_flat(self, keyword: str, page_no: int = 1, page_size: int | None = None) -> dict:
        """选股搜索，返回扁平二维数组（适合表格渲染）"""
        result = self.search(keyword, page_no, page_size)
        if not result.get("ok"):
            return result

        headers = ["序号", "代码", "名称", "最新价", "涨跌幅", "换手率", "量比", "成交额", "市盈率", "总市值", "信号"]
        rows = []
        for s in result["stocks"]:
            chg = s["change"]
            rows.append([
                s["serial"],
                s["code"],
                s["name"],
                s["price"],
                f"+{chg}%" if chg > 0 else f"{chg}%",
                f"{s['turnover']}%" if s["turnover"] is not None else "-",
                s["qrr"],
                s["volume"],
                s["pe"],
                s["marketCap"],
                s["signal"],
            ])
        return {"total": result["total"], "headers": headers, "rows": rows}


# ── CLI ────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="东方财富条件选股")
    parser.add_argument("keyword", nargs="?", help="选股关键词，如 放量突破")
    parser.add_argument("--page-no",   type=int, default=1,   help="页码 (默认 1)")
    parser.add_argument("--page-size", type=int, default=100, help="每页数量 (默认 100)")
    parser.add_argument("--flat",      action="store_true",   help="输出扁平二维数组格式")
    args = parser.parse_args()

    if not args.keyword:
        parser.error("请提供选股关键词")

    cs = cnstock_search()

    if args.flat:
        result = cs.search_flat(args.keyword, args.page_no, args.page_size)
    else:
        result = cs.search(args.keyword, args.page_no, args.page_size)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
