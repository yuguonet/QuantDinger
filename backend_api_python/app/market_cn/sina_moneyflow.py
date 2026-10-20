# -*- coding: utf-8 -*-
"""
sina_moneyflow.py — 新浪个股日级资金流(四档) + 指数日级资金流聚合

用途:
  东财 push2/push2his 被 IP 级风控封锁时 (2026-09/10 多次实测), 指数日级资金流
  回补的替代源: 取新浪个股日级资金流四档净额, 按指数成分股求和, 产出与
  kline_index_fflow 日级行 (每日 15:00 单行, 当日累计) 同构的数据。
  仅覆盖 **日级**, 分钟级仍只能走东财 fflow/kline (见 sync_index_fflow.py)。

来源/任务: 指数资金流日级回补(方案2) / 2026-10-20 / OpenClaw 代编
(共享区协作约定: 新增文件, 不动他人内容)

数据源 (全部新浪, 一次请求取个股全历史):
  MoneyFlow.ssl_qsfx_lscjfb?daima=sh600519&num=10000
  返回逐日: netamount(净额) / r0,r1,r2,r3(各档流入) / r0_net..r3_net(各档净额)

★ 口径映射 (2026-10-20 实证, 与东财阈值逐条一致 — 新浪官方定义):
  特大单 ≥50万股 或 ≥100万元;  大单 ≥10万股或≥20万 且小于特大;
  中单  ≥2万股或≥4万 且小于大单; 小单 <2万股且<4万元; 主力=特大+大单。
  东财五档阈值与上完全相同 (超大单=特大单), 故映射:
    r0_net → super_net (超大/特大)   r1_net → big_net (大单)
    r2_net → mid_net   (中单)        r3_net → small_net (小单)
    main_net = super_net + big_net   (与东财"主力=超大+大"自洽式一致)
  r0~r3 顺序依据: 各档净额量级恒满足 |r0|>=|r1|>=|r2|>=|r3| 及公开资料
  (新浪 r0=超大/特大单)。⚠ 残余风险: 无第三方付费源交叉验证, 建议用
  --verify 对已有东财分钟数据的日期做一次口径比对 (sync_index_fflow.py 已带)。

易错点 (改动前先读):
  1. num 参数是 **从历史起点起的条数** (不是最后 N 条)。num=10000 一次返回
     全历史。新浪资金流历史 **起点约 2010-03-01** (更早无数据, 不是个股问题)。
  2. 停牌/无数据日返回全 0 行; 另实测个别老数据"流入额 r0~r3 为 0 但净额
     r*_net 正常" — 只用 r*_net, 不要用 r0~r3 (净额自洽: Σr*_net == netamount,
     4031 行实测 0 违例)。
  3. daima 用新浪格式 "sh600519"/"sz000001" (index_cons 输出即是), 不是 "600519.SH"。
  4. 请求必须带 Referer=finance.sina.com.cn (见 index_cons 同款注释), 且限速。
  5. 聚合用 **当前成分** (index_cons.get_index_cons), 非历史成分; 且与东财指数
     官方聚合可能存在样本差异 (如 B 股/纳入时点), 属近似回补 — 精度校验走 --verify。
  6. 断点续跑检查点在 <项目根>/tmp/fflow_sina_resume.json, 含各指数逐日部分和;
     中断续跑会接着攒, 全部完成后自动删除。改成分/换指数组合前先 --force 清掉,
     否则部分和混入旧成分口径。

用法 (脚本入口见 scripts/sync_index_fflow.py --backfill --source sina):
    from app.market_cn.sina_moneyflow import get_indices_daily_flow, get_stock_daily_flow
    rows_map = get_indices_daily_flow(["000016.SH"])   # {symbol: [日级行...]}
    # 日级行 = {"time": "YYYY-MM-DD 15:00", "main_net", "small_net",
    #           "mid_net", "big_net", "super_net"}  (与 kline_index_fflow 同构, 单位元)
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}
_LSCJFB_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "MoneyFlow.ssl_qsfx_lscjfb")
_THROTTLE_SEC = 1.0           # 个股全历史请求较大, 限速宽松些防封禁
_HIST_LIMIT = 10000           # 见易错点1: 全历史条数上限

# 新浪四档净额 → kline_index_fflow 列 (口径见文件头 ★)
_CLASS_MAP = (("r0_net", "super_net"), ("r1_net", "big_net"),
              ("r2_net", "mid_net"), ("r3_net", "small_net"))
_FLOW_KEYS = ("main_net", "small_net", "mid_net", "big_net", "super_net")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]   # .../backend_api_python/app/market_cn/ → 项目根
_RESUME_PATH = _PROJECT_ROOT / "tmp" / "fflow_sina_resume.json"

_last_req_ts = 0.0


def _throttle() -> None:
    global _last_req_ts
    wait = _THROTTLE_SEC - (time.time() - _last_req_ts)
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.5))
    _last_req_ts = time.time()


def to_daima(code: str) -> str:
    """股票代码 → 新浪 daima 格式。

    "600519"/"600519.SH"/"sh600519" → "sh600519"。
    ⚠ 不能只看首位是否 '6' (旧实现在 tape._sina_moneyflow_rows 里把 8/4 开头
    的北交所错归 sz): 6/9→sh, 0/3→sz, 4/8→bj。
    """
    c = code.strip().split(".")[0].lower()
    if c.startswith(("sh", "sz", "bj")) and len(c) == 8:
        return c
    c = c.zfill(6)
    if c.startswith(("6", "9")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c


def _get_json(url: str, params: Optional[Dict] = None, retries: int = 3):
    last_err = None
    for i in range(retries):
        _throttle()
        try:
            s = requests.Session()
            s.trust_env = False   # 境外代理出口会被新浪拒绝, 强制直连
            r = s.get(url, params=params, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            return json.loads(r.text)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    logger.warning("[sina_fflow] 请求失败 %s params=%s: %r", url, params, last_err)
    return None


def get_stock_daily_flow(daima: str) -> List[Dict]:
    """个股日级四档净额 (全历史)。

    返回升序 [{"date": "YYYY-MM-DD", "super_net", "big_net", "mid_net",
    "small_net", "main_net"}], 单位元; 失败返回 []。
    """
    data = _get_json(_LSCJFB_URL, params={"daima": daima, "num": _HIST_LIMIT})
    if not isinstance(data, list) or not data:
        return []
    rows = []
    for rec in data:
        try:
            item = {"date": rec["opendate"]}
            for src, dst in _CLASS_MAP:
                item[dst] = float(rec[src])
            item["main_net"] = item["super_net"] + item["big_net"]
            rows.append(item)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("[sina_fflow] %s 行解析跳过: %r", daima, e)
    return rows


def _load_resume() -> Dict:
    try:
        return json.loads(_RESUME_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_resume(state: Dict) -> None:
    _RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RESUME_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_RESUME_PATH)   # 原子替换, 断电不烂文件


def get_indices_daily_flow(symbols: List[str], force: bool = False,
                           verbose: bool = False) -> Dict[str, List[Dict]]:
    """多指数日级资金流聚合 (新浪个股四档求和)。

    - 成分股取并集只抓一次 (上证50⊂沪深300⊂上证指数, 避免重复请求);
    - 断点续跑: 部分和+已完成集合写 tmp/fflow_sina_resume.json, 跑完自动删;
    - 返回 {symbol: [{"time": "YYYY-MM-DD 15:00", main/small/mid/big/super_net}, ...]}
      日期升序; 成分获取失败的 symbol 不出现在结果里。
    """
    from app.market_cn.index_cons import get_index_cons

    cons_map: Dict[str, List[str]] = {}
    for sym in symbols:
        cons = get_index_cons(sym)
        if not cons:
            logger.error("[sina_fflow] %s 无成分股, 跳过", sym)
            continue
        cons_map[sym] = cons

    # 并集 (保序去重)
    union: List[str] = []
    for cons in cons_map.values():
        for daima in cons:
            if daima not in union:
                union.append(daima)

    resume = _load_resume()
    done: List[str] = list(resume.get("done", []))
    sums: Dict[str, Dict[str, List[float]]] = resume.get("sums", {})
    if force or set(symbols) != set(resume.get("symbols", [])):
        done, sums = [], {}          # 指数组合变了必须重来 (见易错点6)
    done_set = set(done)

    todo = [d for d in union if d not in done_set]
    logger.info("[sina_fflow] 成分并集 %d 只, 已完成 %d, 待抓 %d", len(union),
                len(done_set), len(todo))

    for i, daima in enumerate(todo, 1):
        rows = get_stock_daily_flow(daima)
        if not rows:
            logger.warning("[sina_fflow] %s 无数据 (%d/%d)", daima, i, len(todo))
        for sym in symbols:
            if sym not in cons_map or daima not in cons_map[sym]:
                continue
            bucket = sums.setdefault(sym, {})
            for row in rows:
                acc = bucket.setdefault(row["date"], [0.0] * len(_FLOW_KEYS))
                acc[0] += row["main_net"]
                acc[1] += row["small_net"]
                acc[2] += row["mid_net"]
                acc[3] += row["big_net"]
                acc[4] += row["super_net"]
        done.append(daima)
        if i % 20 == 0 or i == len(todo):
            _save_resume({"symbols": list(symbols), "done": done, "sums": sums})
            logger.info("[sina_fflow] 进度 %d/%d", i, len(todo))

    out: Dict[str, List[Dict]] = {}
    for sym, bucket in sums.items():
        out[sym] = [{"time": f"{date} 15:00",
                     **{k: v for k, v in zip(_FLOW_KEYS, acc)}}
                    for date, acc in sorted(bucket.items())]

    if out and all(s in out for s in cons_map):
        try:
            _RESUME_PATH.unlink(missing_ok=True)   # 全部完成, 清检查点
        except OSError:
            pass
    return out
