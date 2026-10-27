# -*- coding: utf-8 -*-
"""dispatch — 三模式分发 + 批量分发（**唯一实现**，五族共用）。

【为什么单独一个文件】
  方案原稿要求"每个协调器文件各自实现顺序/race/分页"。那等于把同一套语义写五遍：
  超时分配、单源短路、空信封判定、NotSupported 跳过、失败计数 —— 五份拷贝必然漂移
  （本仓 171 处 except-pass 就是这么长出来的）。用户裁定（2026-10-26）：允许一个
  dispatch.py。于是调度语义只有这一份，五个 `coord_*.py` 只声明"哪个 kind 用哪个模式"。

【三条语义（方案 §7）】
  1. **空信封 `{"rows": []}` 是成功** —— 当天确实没有榜单，不是取数失败。
  2. **NotSupported 在 validator 之前判掉** —— 不计失败、不换源代价、不打熔断。
  3. **单源短路** —— 候选只有 1 个时不进候选循环、不 race，直接一次调用。

【失败语义】
  provider 抛异常 / 信封不合协议 ⇒ 记该源一次失败，换下一个；全部失败才返回 ok=False。
  失败不会被伪装成空数据（这是与原 `dragon_limit._ak_*` 的关键差别）。

【批量分发 run_batch（2026-10-27，用户裁定：模式抄 coordinator.coordinate_market_kline
  进 dispatch 五族共用）】
  多个 item（如指数代码列表）一次打包：每个 item 跑**完整多源链**（独立超时预算），
  主池 → 重试池 —— 失败 item 隔 retry_pause 整链重试 max_retries 轮后判彻底失败。
  有意**串行 + item 间 pause**：批量规模小（指数级 ~9 码），且限频上游（新浪/东财
  对连发打 RST 突发窗）下并发反而触发风控；若将来出现大批量高 QPS 需求再扩 workers。
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_TIMEOUT = 8.0

#: 共享执行池：所有族共用，不为每个 kind 建池（避免了 N 个池互相争抢）。
_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="qd-dispatch"
)
_SLOTS = threading.BoundedSemaphore(256)


class NotSupported:
    """provider 表示"这个 kind 我不支持"的一等返回值（不是异常）。"""

    __slots__ = ("kind",)

    def __init__(self, kind: str = ""):
        self.kind = kind

    def __bool__(self) -> bool:  # 与 legacy NotSupportedResult 行为一致
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return "NotSupported(%s)" % self.kind


class ProviderCallError(Exception):
    """包装 provider 异常，避免与 Future 的 TimeoutError 混淆（coordinator 共用）。"""


class ExecutorSaturatedError(RuntimeError):
    """执行器槽位耗尽 —— **基础设施拥塞**，不是数据源失败。

    调用方必须按"放回重试、不计源失败"处理：否则拥塞会被幻影记成
    "所有源都试过且失败"⇒ 三振出局（2026-10-27 实测 12 槽 vs 30 worker，
    3730 只里 2793 只从未真正发起 fetch 就被判彻底失败）。
    """


def run_provider_call(fn, *args, **kwargs):
    """在超时池线程里调 provider，异常包成 ProviderCallError。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise ProviderCallError(str(exc)) from exc


def submit_bounded(pool, slots, fn, *args, **kwargs):
    """提交到 pool 并占一个槽（有界排队，防卡死任务撑爆队列）。

    槽位在未来完成时释放（含硬超时被 abandon 的任务 —— 底层线程自身
    超时返回后回调释放）。槽位不足 ⇒ 抛 ExecutorSaturatedError。
    供 coordinator / dispatch 共用：coordinator 用隔离池跑 legacy provider，
    dispatch 用共享池跑信封 provider，提交语义同一份。
    """
    if not slots.acquire(blocking=False):
        raise ExecutorSaturatedError("provider executor has no free worker slots")
    try:
        future = pool.submit(run_provider_call, fn, *args, **kwargs)
    except Exception:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return future


def _is_unsupported(res: Any) -> bool:
    """「该源不支持」判定（跳过，不计失败）——含 legacy NotSupportedResult。

    ⚠ 按**类名**识别 legacy 值对象，**不 import** `app.data_sources.provider`
    （隔离红线：dispatch 是新层，禁止反向依赖旧数据源层；该 import 曾让
    test_new_layer_isolation 红，2026-10-27 收口为唯一实现）。
    """
    if res is None or res is False:
        return True
    if isinstance(res, NotSupported):
        return True
    return type(res).__name__ == "NotSupportedResult"


@dataclass
class Outcome:
    ok: bool
    data: Any = None
    source: str = ""
    error: str = ""
    mode: str = ""
    attempts: int = 0
    skipped: int = 0


# ── 信封校验器（族文件可覆写）─────────────────────────────────────

def is_rows(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("rows"), list)


def is_object(v: Any) -> bool:
    """object 型信封：必须带 "object" 键；值可为 dict（有数据）或 None（确实无盘口）。

    允许 None 是因为 tape 的两个上游都可能合理地拿不到盘口
    （非交易时段 / 停牌），那不是取数失败。
    """
    return isinstance(v, dict) and "object" in v and (
        v["object"] is None or isinstance(v["object"], dict))


def _batch_has_content(data: Any, validate: Callable[[Any], bool]) -> bool:
    """批量层判定单码结果「是否有数据」：

    - rows 类（is_rows）：要求 rows 非空（指数/分笔空列表＝没取到，应重试/换源）；
    - object 类（is_object）：信封在即算有数据（盘口/基本面 object=None 是合法「暂无」，非取数失败）。
    """
    if not isinstance(data, dict):
        return False
    if validate is is_rows:
        return len(data.get("rows") or []) > 0
    return True


def _payload_has_content(payload: Any, validate: Callable[[Any], bool]) -> bool:
    """批量层判定单 item 的**已解包 payload**「是否有数据」（与 _batch_has_content 区分）：

    - rows 类（is_rows）：payload 为非空 list；
    - object 类（is_object）：payload 为 dict（信封在即算有数据，object=None 是合法「暂无」）。

    注意：rows 类解包后 payload 是 list 而非 dict，不能再走 _batch_has_content
    （它会因"非 dict"直接判无数据）。本函数专用于 run_batch 的 pending/failed 判定。
    """
    if validate is is_rows:
        return isinstance(payload, list) and len(payload) > 0
    return isinstance(payload, dict)


def _safe_call(fn, code, rest):  # pragma: no cover
    return fn(code=code, **rest)


def fanout_codes(fn: Callable, codes: Sequence[Any], rest: Dict[str, Any],
                 name: str, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """上游多股并发：并发跑单码 fn(code=c, **rest) 取多码，合并为 run_batch 信封
    {"rows":[{code, rows, source, error}]}。

    单码异常 / 超时 / 非 dict ⇒ 该码记 error，不牵连其它码；
    **NotSupported 不记 error、不进失败表**（= 该源不支持该码，与单请求链
    `skipped 不计失败` 同一语义，2026-10-27 用户裁定）。
    复用共享 _POOL，单码 future 独立超时预算。仅用于声明了 batch_kinds 的非 RST 上游。
    """
    results: Dict[Any, Dict[str, Any]] = {}
    futures = {}
    for c in codes:
        try:
            f = _POOL.submit(_safe_call, fn, c, rest)
        except Exception as e:  # pragma: no cover
            results[c] = {"code": c, "rows": None, "source": name,
                          "error": "%s: %s" % (type(e).__name__, e)}
            continue
        futures[f] = c
    try:
        # 并发取数：整体超时约等于单码预算（最慢那码），不随码数线性放大
        for f in concurrent.futures.as_completed(list(futures),
                                                 timeout=max(timeout, 3.0)):
            c = futures[f]
            try:
                res = f.result()
            except Exception as e:
                results[c] = {"code": c, "rows": None, "source": name,
                              "error": "%s: %s" % (type(e).__name__, e)}
                continue
            if _is_unsupported(res):
                results[c] = {"code": c, "rows": None, "source": name,
                              "error": ""}          # NotSupported ⇒ 跳过，不算失败
            elif not isinstance(res, dict):
                results[c] = {"code": c, "rows": None, "source": name,
                              "error": "非字典信封"}
            else:
                results[c] = {"code": c, "rows": res, "source": name, "error": ""}
    except concurrent.futures.TimeoutError:
        for f, c in futures.items():
            if c not in results:
                results[c] = {"code": c, "rows": None, "source": name,
                              "error": "批量并发超时"}
    return {"rows": [results[c] for c in codes]}


# ── 单次调用 ────────────────────────────────────────────────────

def _call_once(provider: Any, kind: str, params: dict,
               validate: Callable[[Any], bool], budget: float):
    """返回 (data, error, skipped)。data 非 None 即成功。"""
    if not _SLOTS.acquire(timeout=max(0.0, min(budget, 1.0))):
        return None, "调度槽位耗尽", False
    try:
        future = _POOL.submit(provider.fetch, kind, **params)
    except Exception as e:  # pragma: no cover - submit 极少失败
        _SLOTS.release()
        return None, "%s: %s" % (type(e).__name__, e), False
    future.add_done_callback(lambda _: _SLOTS.release())

    try:
        res = future.result(timeout=max(0.05, budget))
    except concurrent.futures.TimeoutError:
        return None, "超时(%.1fs)" % budget, False
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e), False

    if _is_unsupported(res):
        return None, "", True                      # 跳过，不算失败
    if not validate(res):
        return None, "信封不合协议: %r" % (type(res).__name__), False
    return res, "", False


# ── 三模式 ──────────────────────────────────────────────────────

def _sequential(cands, kind, params, validate, timeout) -> Outcome:
    deadline = time.monotonic() + timeout
    errors, attempts, skipped = [], 0, 0

    for p in cands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("预算耗尽")
            break
        attempts += 1
        # 单源短路：只有 1 个候选时把整段预算给它，不做递减分配
        budget = remaining if len(cands) == 1 else min(remaining, timeout / max(1, len(cands)))
        data, err, skip = _call_once(p, kind, params, validate, budget)
        if data is not None:
            return Outcome(True, data, p.name, mode="sequential",
                           attempts=attempts, skipped=skipped)
        if skip:
            skipped += 1
            continue
        errors.append("%s: %s" % (p.name, err))

    return Outcome(False, None, "", "; ".join(errors) or "无可用源",
                   mode="sequential", attempts=attempts, skipped=skipped)


def _race(cands, kind, params, validate, timeout) -> Outcome:
    deadline = time.monotonic() + timeout
    if len(cands) == 1:                            # 单源短路：不 race
        return _sequential(cands, kind, params, validate, timeout)

    futs = {}
    errors, skipped = [], 0
    for p in cands:
        if not _SLOTS.acquire(timeout=0.2):
            errors.append("%s: 调度槽位耗尽" % p.name)
            continue
        try:
            f = _POOL.submit(p.fetch, kind, **params)
        except Exception as e:
            _SLOTS.release()
            errors.append("%s: %s" % (p.name, e))
            continue
        f.add_done_callback(lambda _: _SLOTS.release())
        futs[f] = p

    try:
        for f in concurrent.futures.as_completed(list(futs), timeout=max(0.05, timeout)):
            p = futs[f]
            try:
                res = f.result()
            except Exception as e:
                errors.append("%s: %s" % (p.name, e))
                continue
            if _is_unsupported(res):
                skipped += 1
                continue
            if not validate(res):
                errors.append("%s: 信封不合协议" % p.name)
                continue
            return Outcome(True, res, p.name, mode="race",
                           attempts=len(futs), skipped=skipped)
    except concurrent.futures.TimeoutError:
        errors.append("超时(%.1fs)" % timeout)
    finally:
        for f in futs:
            f.cancel()

    return Outcome(False, None, "", "; ".join(errors) or "无可用源",
                   mode="race", attempts=len(futs), skipped=skipped)


def _paged(cands, kind, params, validate, timeout, page_size, max_pages) -> Outcome:
    """同源翻页合并：**不跨源拼页**（不同源的页码语义不同）。"""
    deadline = time.monotonic() + timeout
    for p in cands:
        rows: List[dict] = []
        ok_any = False
        for page in range(int(max_pages)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            page_params = dict(params, page=page, page_size=page_size)
            data, _err, skip = _call_once(p, kind, page_params, validate, remaining)
            if data is None:
                if skip:
                    break
                rows = []
                break
            ok_any = True
            got = list(data.get("rows") or [])
            rows.extend(got)
            if len(got) < int(page_size):
                break
        if ok_any:
            return Outcome(True, {"rows": rows}, p.name, mode="paged", attempts=1)
    return Outcome(False, None, "", "无可用源", mode="paged")


def _dispatch_one(mode: str, candidates: Sequence[Any], kind: str,
                  params: Dict[str, Any], validate: Callable[[Any], bool],
                  timeout: float, page_size: int, max_pages: int) -> Outcome:
    """单请求分发：按 mode 选 sequential/race/paged。空候选显式失败。"""
    cands = list(candidates or ())
    if not cands:
        return Outcome(False, None, "", "本族无可用源", mode=mode)
    if mode == "sequential":
        return _sequential(cands, kind, params, validate, timeout)
    if mode == "race":
        return _race(cands, kind, params, validate, timeout)
    if mode == "paged":
        return _paged(cands, kind, params, validate, timeout, page_size, max_pages)
    raise ValueError("未知调度模式: %r" % mode)


def run(mode: str, candidates: Sequence[Any], kind: str, params: Dict[str, Any],
        validate: Callable[[Any], bool], timeout: float = DEFAULT_TIMEOUT,
        page_size: int = 0, max_pages: int = 1) -> Outcome:
    """统一入口。`mode ∈ {sequential, race, paged}`。"""
    return _dispatch_one(mode, candidates, kind, params, validate,
                         timeout, page_size, max_pages)


def run_batch(mode: str, candidates: Sequence[Any], kind: str,
              items: Sequence[Any], params: Dict[str, Any], item_key: str,
              validate: Callable[[Any], bool], timeout: float = DEFAULT_TIMEOUT,
              page_size: int = 0, max_pages: int = 1,
              pause: float = 0.0, retry_pause: float = 8.0,
              max_retries: int = 1,
              batch_item_key: Optional[str] = None) -> Outcome:
    """批量分发：多 item 一次打包，主池 + 重试池（五族共用）。

    骨架串行 + 可选「上游多股并发」（模式抄 coordinator.coordinate_market_kline 主池/重试池）：

      · 串行候选（serial_cands）—— 未声明 batch_kinds 的源（新浪/东财等有限频 RST 风险）：
        每个 item 跑**完整多源链**（_dispatch_one，item 级独立超时预算），单码首源死由次源补，
        不牵连其它码；码间 pause 限频。
      · 并发候选（batch_cands）—— 声明了 batch_kinds 的非 RST 源（tencent 等）：
        一次把整批 codes 交给该源，源内部 fanout_codes 多股并发取数，效率远高于逐码串行。
      · 重试池 —— 首轮（主池）无数据的 item 隔 retry_pause 由**串行候选**对失败子集
        逐码整链重跑 max_retries 轮；并发候选只在主池跑一次（返回空信封即视为该码失败，
        交串行兜底），不在重试池重跑（对刚返回空 RST 的并发源重 fanout 无意义）。
      · 彻底失败 —— 重试尽仍无数据**且带 error** ⇒ 记入失败表（error 摘要），不阻塞其它 item；
        NotSupported / 空信封无 error ⇒ **跳过，不计入失败表**（Outcome.skipped 计数），
        与单请求链「NotSupported 不计失败」同一语义（2026-10-27 用户裁定）。
      · 空信封 data.error 穿透 —— 单码"有信封但无数据"（如限频 RST 突发窗）的 data.error
        在批量层保留进该 item 的 error，便于排查（2026-10-27）。

    Args:
        items: 批量键列表（如 6 位代码）；逐个以 item_key 注入单码源、以 batch_item_key
               注入并发源（整批）。
        item_key: 串行源入参名（"code"）。
        batch_item_key: 并发源入参名（"codes"）；仅声明 batch_kinds 的候选生效。
        pause / retry_pause / max_retries: 串行限频与重试池参数。

    Returns:
        Outcome（mode="batch"）:
          data={"rows": [{item_key: item, "rows": <内层信封>, "source": str, "error": str}, ...]}
          ok=至少一个 item 有数据；error=真失败 item 的摘要（跳过项不进摘要）；
          skipped=无内容且非失败（NotSupported/空信封）的 item 数。
    """
    cands = list(candidates or ())
    item_list = [i for i in items if i is not None and i != ""]
    if not cands:
        return Outcome(False, None, "", "本族无可用源", mode="batch")
    if not item_list:
        return Outcome(True, {"rows": []}, "", mode="batch")

    batch_cands = [p for p in cands if batch_item_key and kind in getattr(p, "batch_kinds", ())]
    serial_cands = [p for p in cands if p not in batch_cands]

    results: Dict[Any, Dict[str, Any]] = {}

    def _do_batch(subset):
        """并发候选：整批 codes 一次交给该源，源内部多股并发取数。

        源返回两种形态都要能解包（按本 kind 的 validator）：
          · fanout 信封（tape 等经 fanout_codes 包装）：e["rows"] 是内层信封 dict
            → rows 类取内层 rows 列表，object 类取内层信封；
          · 扁平记录（腾讯 index_quote 等：fetch 直接返 [{code, price,...}]）
            → 每条记录本身即该码数据，按 code 归组成该码 payload（rows 类包裹为 [record]）。
        """
        for p in batch_cands:
            fn = getattr(p, "capabilities", {}).get(kind)
            if fn is None:
                continue
            out = _dispatch_one(mode, [p], kind,
                                dict(params, **{batch_item_key: list(subset)}),
                                is_rows, timeout, page_size, max_pages)
            entries = ((out.data or {}).get("rows") or []) if (out.ok and isinstance(out.data, dict)) else []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                code = str(e.get(item_key) or e.get("code") or "")
                if "rows" in e:
                    # fanout 信封条目：恒含 rows 键（成功=dict 内层信封，失败=None）；
                    # 可能带 source/error 元数据。失败码 rows=None ⇒ payload 置空但保留 error/source。
                    inner = e.get("rows")
                    if isinstance(inner, dict):
                        payload = inner.get("rows") or [] if validate is is_rows else inner
                    else:
                        payload = [] if validate is is_rows else None
                    results[code] = {
                        "rows": payload,
                        "source": e.get("source")
                                  or (inner.get("source") if isinstance(inner, dict) else "")
                                  or out.source,
                        "error": e.get("error")
                                 or (inner.get("error") if isinstance(inner, dict) else "")
                                 or "",
                    }
                else:
                    # 扁平记录（腾讯 index_quote 等：fetch 直接返 [{code, price,...}]）：
                    # 本记录即该码数据，rows 类包裹为 [record]，按 code 归组
                    if validate is is_rows:
                        bucket = results.setdefault(
                            code, {"rows": [], "source": e.get("source") or out.source, "error": ""})
                        bucket["rows"].append(e)
                    else:
                        results[code] = {
                            "rows": e, "source": e.get("source") or out.source,
                            "error": e.get("error") or "",
                        }

    def _do_serial(subset):
        """串行候选：每个 item 跑完整多源链，码间 pause 限频；返回**真失败**的 item。

        NotSupported（含全链皆 skip）⇒ 非失败：不记 error、不进失败表、不进重试池
        （重试一个"没人支持"的码没有意义）；空信封无 error 同理（当天确实没数据）。
        """
        still = []
        for idx, item in enumerate(subset):
            if idx and pause > 0:
                time.sleep(pause)
            got = None
            last_err = ""
            for p in serial_cands:
                out = _dispatch_one(mode, [p], kind, dict(params, **{item_key: item}),
                                    validate, timeout, page_size, max_pages)
                if out.ok and _batch_has_content(out.data, validate):
                    # 按 validator 解包 payload：rows 类取列表，object 类取信封
                    if validate is is_rows:
                        payload = (out.data or {}).get("rows") or []
                    else:
                        payload = out.data
                    got = {"rows": payload, "source": out.source, "error": ""}
                    break
                # 全 NotSupported 跳过 ⇒ 非失败，不沉淀 error（跳过≠失败）
                if out.skipped and out.attempts and out.skipped >= out.attempts:
                    continue
                # 空信封 data.error 穿透（限频 RST 突发窗等"有信封无数据"）
                last_err = (out.error or ((out.data or {}).get("error") or "")) or last_err
            if got:
                results[item] = got
            elif last_err:
                results[item] = {"rows": None, "source": "", "error": last_err}
                still.append(item)                  # 真失败才进重试池
            else:
                # 全链 NotSupported / 空信封无 error ⇒ 非失败（不进失败表、不重试）
                results[item] = {"rows": None, "source": "", "error": ""}
        return still

    _do_batch(item_list)                 # 主池：并发候选整批一次
    pending = [i for i in item_list
              if not _payload_has_content(results.get(i, {}).get("rows"), validate)]
    for r in range(max(1, max_retries + 1)):   # 重试池：串行候选对失败子集逐码补
        if not pending:
            break
        still = _do_serial(pending)
        pending = still
        if pending and r < max_retries:
            time.sleep(retry_pause)

    rows_out = [{item_key: i,
                 "rows": (results.get(i) or {}).get("rows"),
                 "source": (results.get(i) or {}).get("source", ""),
                 "error": (results.get(i) or {}).get("error", "")} for i in item_list]

    def _has(i):
        return _payload_has_content((results.get(i) or {}).get("rows"), validate)

    # 失败表只收**真失败**（无内容且带 error）；NotSupported/空信封无错 ⇒ 跳过，不计入
    failed = [i for i in item_list if not _has(i) and (results.get(i) or {}).get("error")]
    n_skipped = sum(1 for i in item_list
                    if not _has(i) and not (results.get(i) or {}).get("error"))
    ok = any(_has(i) for i in item_list)
    error = "" if not failed else "彻底失败 %d/%d: %s" % (
        len(failed), len(item_list),
        "; ".join("%s(%s)" % (i, (results.get(i) or {}).get("error", "无数据")) for i in failed))
    return Outcome(ok, {"rows": rows_out}, "batch", error, mode="batch",
                   attempts=len(item_list) * max(1, len(cands)), skipped=n_skipped)
