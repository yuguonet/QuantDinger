# -*- coding: utf-8 -*-
"""LLM 边界监控 —— 挂在 mimoagent 模型层，dump 每次调用的输入/输出 + **耗时归因**。

用途（2026-10-16/17）：分析「分析 600519」这类真实请求，**从 LLM 边界反推**
用了哪些工具/skill/组件，并把墙钟耗时拆开定位瓶颈。

【为什么挂这里】
· `OpenAIChatModel.query`（`models/openai_chat.py:180`）是唯一发请求的公开入口。
· 真正的耗时归因要挂在 `_query`（`openai_chat.py:105`）——它是 tenacity 装饰的
  函数，**内部可能重试 5 次**（`@retry(stop_after_attempt(5), wait_exponential(
  multiplier=1, min=4, max=60))`）。只在 `query` 上计时会把"5 次重试"看成
  "一次慢调用"，必须分两处打点才能区分。
· 上游原有的 `QD_LLM_DEBUG` / `_debug_dump_messages` 已在 2026-10-15
  「重装纯净上游版」时从包内根除（见 `app/agent/cli.py:75-78`）⇒ 需自建。

【三个打点如何拼出归因】
  t_query_start ──▶ t_query_end          = 墙钟（含全部重试与退避等待）
       │
       └─ t_attempt[N] 每次 `_query` 内部尝试的起止
              ⇒ attempts>1 ⇒ 发生了重试；sum(attempt) 与墙钟之差 =退避 sleep
              ⇒ 单次 attempt 长 ⇒ 是网关/模型慢，不是重试

【第二层：HTTP 边界 TTFB（2026-10-17 新增）】
墙钟只回答"总共多久"，不回答"卡在哪个阶段"。在 SDK 的 httpx client 上挂
event hooks（`_event_hooks`，request hook = 请求交给 transport 前；
response hook = **响应头到达**，streaming 时 body 尚未读）：
  t_req ──▶ t_hdr  = TTFB（网络上传 + 网关排队 + 服务端生成到首字节）
  t_hdr ──▶ t_end  = body 读取（非流式≈0；流式=全部 chunk 的到达期）
非流式下 TTFB ≈ 整墙钟 ⇒ 用户干等；流式下 TTFB 才是"第一个字多久可见"。

【第三层：usage 差分（2026-10-17 新增）】
mimo `query()` 只返回 payload，TokenStats 被 `_record_call` 吃进
`self.token_stats` ⇒ payload 里没有 usage。在 query 前后差分
`token_stats.{input,output}_tokens` 才能知道"59.6s 生成了多少 token"
（区分"网关排队"与"生成长内容"的唯一硬证据）。

【返回结构踩坑（2026-10-17 修正）】
`query()` 返回的是 **mimoagent 自建payload**（`_build_assistant_payload`），
形如 `{"content":..., "tool_calls":[...], "reasoning_content":...}`，
**不是** OpenAI 原生 `{"choices":[{"message":{...}}]}`。
首版探针按 OpenAI 格式解析 ⇒ choices 为空 ⇒ 一度误报"预选没选中任何工具"。
⇒ `_read_payload` 同时兼容两种结构。

【输出】tmp/llm_probe/ 下每次调用两个文件：
  call_NN.json       轻量摘要（工具清单 / 耗时归因 / tool_calls）
  call_NN_full.json  全量（messages + tools + raw_response）

用法（从 backend_api_python/ 运行）：
  .venv\\Scripts\\python.exe -m app.agent.scripts.llm_probe "分析600519"

  # A/B 重放对照（TTFB 的核心测量）：拿某次 call_NN_full.json 的输入，
  # 分别用非流式 / 流式各发一次，对比 TTFB、首 chunk、总时长、结果一致性
  .venv\\Scripts\\python.exe -m app.agent.scripts.llm_probe --ttfb-ab tmp/llm_probe/call_04_full.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent# app/agent/scripts
_PROJECT = _HERE.parents[2]           # backend_api_python
_REPO = _PROJECT.parent                # D:/QuantDinger
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

# 与 cli.py 同口径：先加载 backend 的 .env，否则 --replay 拿不到 OPENAI_*（401）
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(str(_PROJECT / ".env"), override=False)
except ImportError:
    pass

_OUT = _REPO / "tmp" / "llm_probe"
_OUT.mkdir(parents=True, exist_ok=True)

_seq = {"n": 0}
_attempts: dict = {}                 # id(self) -> attempt 计时列表
_installed = {"done": False}
_e2 = sys.stderr                      # stderr= 日志通道，stdout 留给结果

# ── HTTP 边界 TTFB（httpx event hooks）──────────────────────────
# openai SDK 3.3.1 的同步 client 存在 `model.client._client._event_hooks`
# （`{"request": [...], "response": [...]}`）。同步 client 下 request/response
# hook 严格嵌套 ⇒ 用栈匹配，不需要请求 id。
#
# ⚠ 防重入必须用**模块级单例 hook**：首版把 hook 写成 `_install_httpx_hooks`
# 里的闭包，每次调用都新建函数对象 ⇒ `_on_request not in hooks` 恒为 True
# ⇒ 同一实例被挂两次 ⇒ 每个请求记 2 条 TTFB 事件 ⇒ ttfb_sum 翻倍
# （实测 8.5s > wall 4.4s 的 196% 就是这么来的，2026-10-17）。
_ttfb_stack: list = []                # request hook push / response hook pop
_ttfb_events: list = []               # 每次 HTTP 往返一条：t_req/t_hdr/ttfb
_ttfb_hooks_installed: set = set()    # 已挂 hook 的 httpx client id


def _ttfb_on_request(request):
    _ttfb_stack.append(time.perf_counter())


def _ttfb_on_response(response):
    t_hdr = time.perf_counter()
    if not _ttfb_stack:
        return
    t_req = _ttfb_stack.pop()
    req = getattr(response, "request", None)
    _ttfb_events.append({
        "t_req": t_req, "t_hdr": t_hdr,
        "ttfb_s": round(t_hdr - t_req, 3),
        "status": getattr(response, "status_code", None),
        "url": str(getattr(req, "url", ""))[:80],
    })


def _log(msg: str) -> None:
    _e2.write(msg + "\n")
    _e2.flush()


# ═══════════════════════════════════════════════════════════════
#  HTTP 边界 TTFB
# ═══════════════════════════════════════════════════════════════
def _install_httpx_hooks(model) -> None:
    """给一个已构造的 OpenAIChatModel 挂 httpx event hooks（幂等）。

    hook 触发点（httpx 0.28 `Client.send`）：
      request hook  —— 请求构建完成、交给 transport **之前**
      response hook —— transport 返回、**响应头已读**（streaming 时 body 未读）
    ⇒ ttfb = t_hdr - t_req；body 期 = create() 返回时刻 - t_hdr。
    """
    client = getattr(model, "client", None)
    inner = getattr(client, "_client", None)
    hooks = getattr(inner, "_event_hooks", None)
    if not isinstance(hooks, dict) or "request" not in hooks:
        _log("[probe] 未找到 httpx _event_hooks（SDK 结构变化？），TTFB 打点跳过")
        return
    if id(inner) in _ttfb_hooks_installed:
        return
    hooks["request"].append(_ttfb_on_request)
    hooks["response"].append(_ttfb_on_response)
    _ttfb_hooks_installed.add(id(inner))
    _log("[probe] 已挂 httpx event hooks（TTFB）")


# ═══════════════════════════════════════════════════════════════
#  耗时归因
# ═══════════════════════════════════════════════════════════════
def _wrap_query(cls) -> None:
    """给 query 打墙钟计时 + usage 差分 + TTFB 事件切片。"""
    orig = cls.query
    _hooked: set = set()          # 已挂 httpx hooks 的实例 id（lazy）

    def wrapped(self, messages, **kwargs):
        key = id(self)
        if key not in _hooked:                 # 实例懒构造 ⇒ 首次见到才挂
            _install_httpx_hooks(self)
            _hooked.add(key)
        _attempts[key] = []
        mark = len(_ttfb_events)               # 本次 call 的 HTTP 事件起点
        st = self.token_stats
        tok0 = (st.input_tokens, st.output_tokens,
                getattr(st, "cache_read_tokens", 0),
                getattr(st, "cache_creation_tokens", 0))
        t0 = time.perf_counter()
        raw = None
        try:
            raw = orig(self, messages, **kwargs)
        finally:
            wall = time.perf_counter() - t0
            atts = _attempts.pop(key, [])
            tok1 = (st.input_tokens, st.output_tokens,
                    getattr(st, "cache_read_tokens", 0),
                    getattr(st, "cache_creation_tokens", 0))
            usage = {
                "input_tokens": tok1[0] - tok0[0],
                "output_tokens": tok1[1] - tok0[1],
                "cache_read_tokens": tok1[2] - tok0[2],
                "cache_creation_tokens": tok1[3] - tok0[3],
            }
            http_ev = _ttfb_events[mark:]
            try:
                _dump(messages, kwargs, raw, wall, atts, usage, http_ev)
            except Exception as e:                   # 探针绝不影响主链
                _log("[probe] dump 失败(忽略): %s: %s" % (type(e).__name__, e))
        return raw

    cls.query = wrapped
    _log("[probe] 已挂 OpenAIChatModel.query（墙钟+usage+TTFB）")


def _wrap_internal(cls) -> None:
    """给 `_query` 打每次尝试的计时 —— 用来识别 tenacity 重试。"""
    orig = cls._query

    def wrapped(self, messages, **kwargs):
        t0 = time.perf_counter()
        try:
            return orig(self, messages, **kwargs)
        finally:
            dt = time.perf_counter() - t0
            lst = _attempts.get(id(self))
            if lst is not None:
                lst.append(round(dt, 2))
                _log("[probe]   └ attempt#%d %.2fs" % (len(lst), dt))

    cls._query = wrapped
    _log("[probe] 已挂 OpenAIChatModel._query（单次尝试）")


# ═══════════════════════════════════════════════════════════════
#  解析
# ═══════════════════════════════════════════════════════════════
def _read_payload(raw: Any) -> dict:
    """兼容两种返回结构（见文件头「返回结构踩坑」）。"""
    if not isinstance(raw, dict):
        return {"content": str(raw), "tool_calls": []}
    if "choices" in raw:                      # OpenAI 原生
        ch = raw.get("choices") or []
        msg = (ch[0] or {}).get("message") or {} if ch else {}
        return {
            "content": str(msg.get("content") or ""),
            "reasoning": str(msg.get("reasoning_content") or ""),
            "tool_calls": [
                {"name": (tc.get("function") or {}).get("name"),
                 "arguments": str((tc.get("function") or {}).get("arguments") or "")}
                for tc in (msg.get("tool_calls") or [])],
            "usage": raw.get("usage"),
            "finish_reason": (ch[0] or {}).get("finish_reason") if ch else None,
        }
    # mimoagent 自建 payload
    return {
        "content": str(raw.get("content") or ""),
        "reasoning": str(raw.get("reasoning_content") or ""),
        "tool_calls": [
            {"name": (tc.get("function") or {}).get("name"),
             "arguments": str((tc.get("function") or {}).get("arguments") or "")}
            for tc in (raw.get("tool_calls") or [])],
        "usage": raw.get("usage"),
        "finish_reason": raw.get("finish_reason"),
    }


def _describe_tools(kwargs: dict) -> dict:
    """抽出本次请求带的工具清单 + 每个工具的 schema 体量（核对"谁在白付"）。"""
    tools = kwargs.get("tools") or []
    items = []
    for t in tools:
        fn = (t or {}).get("function") or {}
        blob = json.dumps(fn, ensure_ascii=False)
        desc = str(fn.get("description") or "")
        props = ((fn.get("parameters") or {}).get("properties") or {})
        items.append({
            "name": fn.get("name", ""),
            "~tokens": len(blob) // 2,
            "desc_chars": len(desc),
            "params": sorted(props.keys()),
        })
    total = sum(x["~tokens"] for x in items)
    return {"count": len(items), "~tokens": total, "items": items}


def _summarize_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            head = "[blocks:%s]" % ",".join(str((b or {}).get("type", "?")) for b in c if isinstance(b, dict))
            n = len(json.dumps(c, ensure_ascii=False))
        else:
            c = str(c or "")
            head = c[:260] + ("..." if len(c) > 260 else "")
            n = len(c)
        out.append({"role": m.get("role"), "chars": n, "head": head,
                    "name": m.get("name"), "tool_call_id": m.get("tool_call_id")})
    return out


# ═══════════════════════════════════════════════════════════════
#  落盘
# ═══════════════════════════════════════════════════════════════
def _dump(messages: list[dict], kwargs: dict, raw: Any, wall: float, atts: list,
          usage: dict | None = None, http_ev: list | None = None) -> dict:
    _seq["n"] += 1
    idx = _seq["n"]
    p = _read_payload(raw)

    spent = sum(atts)
    # TTFB 归因：单次 HTTP 往返时 ttfb/body 可直接拆；多次往返（重试）取累计
    ttfb_sum = round(sum(e["ttfb_s"] for e in (http_ev or [])), 2)
    body_s = round(max(0.0, wall - ttfb_sum), 2)
    rec = {
        "seq": idx,
        "ts": time.strftime("%H:%M:%S"),
        "latency": {
            "wall_s": round(wall, 2),
            "attempts": len(atts),
            "attempt_s": atts,
            "attempt_sum_s": round(spent, 2),
            "backoff_s": round(max(0.0, wall - spent), 2),
            "ttfb_s": ttfb_sum,
            "body_s": body_s,
            "ttfb_share": round(ttfb_sum / wall * 100, 1) if wall > 0 else None,
            "verdict": ("重试×%d（退避 %.1fs）" % (len(atts), wall - spent)) if len(atts) > 1
                       else ("单次调用" if atts else "未捕获"),
        },
        "http_events": http_ev or [],
        "usage": usage or {},
        "msg_count": len(messages or []),
        "tools": _describe_tools(kwargs),
        "messages_summary": _summarize_messages(messages),
        "tool_calls": p["tool_calls"],
        "content_head": p["content"][:600],
        "reasoning_chars": len(p["reasoning"]),
        "finish_reason": p["finish_reason"],
    }

    (_OUT / ("call_%02d.json" % idx)).write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    (_OUT / ("call_%02d_full.json" % idx)).write_text(
        json.dumps({"seq": idx, "messages": messages,
                    "tools_sent": kwargs.get("tools"),
                    "tool_choice": kwargs.get("tool_choice"),
                    "raw_response": raw}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    _log("[probe#%02d] tools=%d (~%d tok) msgs=%d | %s | TTFB %.1fs/%.0f%% | tok in=%d out=%d | calls=%s" % (
        idx, rec["tools"]["count"], rec["tools"]["~tokens"], rec["msg_count"],
        rec["latency"]["verdict"], ttfb_sum,
        rec["latency"]["ttfb_share"] or 0,
        (usage or {}).get("input_tokens", 0), (usage or {}).get("output_tokens", 0),
        ",".join(tc["name"] or "?" for tc in p["tool_calls"]) or "-"))
    return rec


# ═══════════════════════════════════════════════════════════════
#  汇总报告
# ═══════════════════════════════════════════════════════════════
def write_report() -> Path:
    """把本次运行的全部调用汇总成一份 markdown（便于直接读结论）。"""
    files = sorted(_OUT.glob("call_[0-9][0-9].json"))
    if not files:
        return _OUT
    recs = [json.loads(f.read_text(encoding="utf-8")) for f in files]

    L = ["# LLM 边界探针报告（实测）", "",
         "- **采集**: `app.agent/scripts/llm_probe.py`（monkeypatch `OpenAIChatModel.query` + `_query` + httpx event hooks）",
         "- **口径**: 墙钟含tenacity 全部重试；`_query` 打点区分「单次慢」与「多次重试」；",
         "  TTFB = httpx response hook（响应头到达）- request hook；body = 墙钟 - TTFB", ""]
    L += ["## 一、耗时归因", "",
          "| # | 墙钟 | 尝试次数 | 单次耗时 | 退避 | TTFB | TTFB占比 | body | 工具数 | ~tok | tok入/出 | 模型调用 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in recs:
        lat = r["latency"]
        u = r.get("usage") or {}
        L.append("| %d | **%.1fs** | %d | %s | %.1fs | %.1fs | %.0f%% | %.1fs | %d | %d | %d/%d | %s |" % (
            r["seq"], lat["wall_s"], lat["attempts"],
            ",".join("%.1fs" % x for x in lat["attempt_s"]) or "-",
            lat["backoff_s"], lat["ttfb_s"], lat["ttfb_share"] or 0, lat["body_s"],
            r["tools"]["count"], r["tools"]["~tokens"],
            u.get("input_tokens", 0), u.get("output_tokens", 0),
            ",".join(tc["name"] or "?" for tc in r["tool_calls"]) or "(纯文本)"))

    total = sum(r["latency"]["wall_s"] for r in recs)
    retries = [r for r in recs if r["latency"]["attempts"] > 1]
    backoff = sum(r["latency"]["backoff_s"] for r in recs)
    ttfb_total = sum(r["latency"]["ttfb_s"] for r in recs)
    tok_in = sum((r.get("usage") or {}).get("input_tokens", 0) for r in recs)
    tok_out = sum((r.get("usage") or {}).get("output_tokens", 0) for r in recs)
    L += ["", "**合计** %.1fs：退避等待 %.1fs（%.0f%%），TTFB %.1fs（%.0f%%），body 读取 %.1fs；token 入/出 %d/%d；重试发生的调用 %d/%d" % (
        total, backoff, (backoff / total * 100 if total else 0),
        ttfb_total, (ttfb_total / total * 100 if total else 0),
        total - backoff - ttfb_total, tok_in, tok_out, len(retries), len(recs)), ""]

    # ── 结论段：把归因数据翻译成判断 ──
    slow = sorted(recs, key=lambda r: -r["latency"]["wall_s"])[:3]
    L += ["## 一点五、慢调用归因（取墙钟 Top3）", ""]
    for r in slow:
        lat = r["latency"]
        u = r.get("usage") or {}
        if lat["ttfb_share"] and lat["ttfb_share"] >= 90:
            why = "TTFB 占 %.0f%% ⇒ **网关/生成慢**（响应头迟迟不来，非传输问题；流式可改善感知延迟，省不了总时长）" % lat["ttfb_share"]
        elif lat["backoff_s"] > 1:
            why = "退避 %.1fs ⇒ **tenacity 重试等待**" % lat["backoff_s"]
        else:
            why = "TTFB %.0f%% + body %.1fs ⇒ 传输/聚合占比高（异常，需查 body 为何慢）" % (
                lat["ttfb_share"] or 0, lat["body_s"])
        L.append("- call#%02d %.1fs（tok 出 %d）：%s" % (r["seq"], lat["wall_s"], u.get("output_tokens", 0), why))
    L.append("")

    L += ["## 二、每次调用的工具面", ""]
    for r in recs:
        L.append("### call #%d — %s" % (r["seq"], r["latency"]["verdict"]))
        L.append("")
        if r["tools"]["count"]:
            L.append("下发 %d 个工具，schema 合计 ~%d tok：" % (r["tools"]["count"], r["tools"]["~tokens"]))
            L.append("")
            L.append("| 工具 | ~tok | 参数 |")
            L.append("|---|---|---|")
            for it in sorted(r["tools"]["items"], key=lambda x: -x["~tokens"]):
                L.append("| `%s` | %d | %s |" % (it["name"], it["~tokens"],
                                                ", ".join(it["params"]) or "-"))
        else:
            L.append("**未带工具**（纯文本调用）")
        L.append("")
        if r["tool_calls"]:
            L.append("模型发起调用：")
            for tc in r["tool_calls"]:
                L.append("- `%s(%s)`" % (tc["name"], tc["arguments"][:160]))
        else:
            L.append("模型返回纯文本：%s" % (r["content_head"][:300].replace("\n", " ") or "(空)"))
        L.append("")

    f = _OUT / "REPORT.md"
    f.write_text("\n".join(L), encoding="utf-8")
    return f


def _wrap_os_exit() -> None:
    """cli 单次消息模式以 `os._exit(0)` 收尾（`_flush_and_exit`，cli.py:200）。

    这是**有意设计**（避开数据源层模块级线程池的 atexit join，见其 docstring），
    不能改 cli。但 `os._exit` 跳过 atexit 和 main() 的后续代码 ⇒ 首版探针把
    `write_report()` 放在 `cli.main()` 之后，**永远走不到**（2026-10-17 实测：
    5 次调用数据都在，REPORT.md 从不生成）。
    ⇒ 探针适配退出语义：wrap `os._exit`，真退出前先把报告落盘。
    """
    orig = os._exit

    def wrapped(code):
        try:
            f = write_report()
            _log("[probe] 报告已写出(os._exit 前): %s" % f)
        except Exception as e:
            _log("[probe] 报告生成失败(忽略): %s: %s" % (type(e).__name__, e))
        orig(code)

    os._exit = wrapped
    _log("[probe] 已挂 os._exit（退出前出报告）")


def install() -> None:
    if _installed["done"]:
        return
    from mimoagent.models.openai_chat import OpenAIChatModel
    _wrap_internal(OpenAIChatModel)     # 先内后外，保证 attempt 已被记录
    _wrap_query(OpenAIChatModel)
    _wrap_os_exit()
    _installed["done"] = True


# ═══════════════════════════════════════════════════════════════
#  A/B 重放：非流式（现状） vs 流式（改造候选）
# ═══════════════════════════════════════════════════════════════
def _tc_names(payload_calls: list) -> list:
    """mimo payload 的 tool_calls 元素是 {id,type,function:{name,arguments}}。"""
    out = []
    for tc in payload_calls or []:
        fn = (tc or {}).get("function") or {}
        out.append(fn.get("name"))
    return out


def _run_ttfb_ab(full_path: str) -> int:
    """拿某次 call_NN_full.json 的输入，同一 payload 各发一次非流式/流式。

    测量目标（对应用户问题「改流式能让用户多早看到第一个字」）：
      A 非流式：TTFB ≈ 整墙钟 ⇒ 用户干等，中途零可见输出
      B 流  式：first_content_chunk = 第一个携带正文/工具调用的增量到达时刻
      + 结果一致性：结构是否同类（tool_calls vs content）、finish_reason

    ⚠ 随机性注记：temperature>0 时两次独立请求的**正文内容不要求一致**，
    一致性判定只看「结构同类」；正文差异是模型抽样波动，不是流式改造的
    行为差异（2026-10-17 首跑 A/B 时 30c vs 3829c 一度被误读成
    "流式改变结果"）。
    """
    global _OUT                            # 须在函数任何语句之前（Python 语法）
    p = Path(full_path)
    if not p.is_absolute():
        p = _REPO / p
    d = json.loads(p.read_text(encoding="utf-8"))
    messages = d["messages"]
    tools = d.get("tools_sent") or []
    tool_choice = d.get("tool_choice") or "auto"
    ab_dir = _OUT / "ab"                    # 隔离：不污染主探针 call_NN 序列
    ab_dir.mkdir(parents=True, exist_ok=True)
    _OUT = ab_dir                          # A 侧走 query wrapper 也会 dump
    _log("[ab] 输入: %s（%d msgs / %d tools / tool_choice=%s）"
         % (p.name, len(messages), len(tools), tool_choice))

    from app.agent.llm import get_model
    # ⚠ env 加载是入口链路的一部分：主链经 cli.main() 时由 cli.py:69 加载
    # backend_api_python/.env；A/B 模式不走 cli ⇒ 必须自己加载，否则
    # api_key 为空，网关直接 401（2026-10-17 实测踩到）。
    try:
        from dotenv import load_dotenv
        load_dotenv(_PROJECT / ".env", override=False)
    except Exception as e:
        _log("[ab] dotenv 加载失败(忽略): %s: %s" % (type(e).__name__, e))
    model = get_model()
    _install_httpx_hooks(model)

    # ── A：非流式（现状路径，与主链完全一致）──────────────────
    mark = len(_ttfb_events)
    t0 = time.perf_counter()
    payload = model.query(messages, tools=tools, tool_choice=tool_choice)
    wall_a = time.perf_counter() - t0
    ev_a = _ttfb_events[mark:]
    ttfb_a = sum(e["ttfb_s"] for e in ev_a)
    a = {
        "wall_s": round(wall_a, 2), "ttfb_s": round(ttfb_a, 2),
        "first_visible_s": round(ttfb_a, 2),      # 非流式：头到才可能有内容
        "content": str(payload.get("content") or ""),
        "tool_calls": _tc_names(payload.get("tool_calls")),
        "reasoning_chars": len(payload.get("reasoning_content") or ""),
    }
    _log("[ab] A 非流式: wall=%.1fs ttfb=%.1fs content=%dc calls=%s"
         % (wall_a, ttfb_a, len(a["content"]), a["tool_calls"]))

    # ── B：流式（复用上游 _query_streaming 的聚合语义，自行插桩）──
    mark = len(_ttfb_events)
    t0 = time.perf_counter()
    stream = model.client.chat.completions.create(
        model=model.config.model_name, messages=messages,
        tools=tools, tool_choice=tool_choice, stream=True,
        stream_options={"include_usage": True},
    )
    t_created = time.perf_counter() - t0
    first_chunk_s = None
    first_content_s = None               # 第一个携带正文/工具调用的 chunk
    content_parts: list = []
    reasoning_parts: list = []
    tool_calls: dict = {}
    usage = None
    finish_reason = None
    n_chunks = 0
    for chunk in stream:
        n_chunks += 1
        if first_chunk_s is None:
            first_chunk_s = time.perf_counter() - t0
        usage = getattr(chunk, "usage", None) or usage
        if not getattr(chunk, "choices", None):
            continue
        finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        has_payload = bool(delta.content) or bool(delta.tool_calls)
        if has_payload and first_content_s is None:
            first_content_s = time.perf_counter() - t0
        if delta.content:
            content_parts.append(delta.content)
        r = getattr(delta, "reasoning_content", None)
        if r:
            reasoning_parts.append(r)
        for tc in delta.tool_calls or []:
            slot = tool_calls.setdefault(
                tc.index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
            if tc.id:
                slot["id"] = tc.id
            if tc.type:
                slot["type"] = tc.type
            if tc.function:
                slot["function"]["name"] += tc.function.name or ""
                slot["function"]["arguments"] += tc.function.arguments or ""
    wall_b = time.perf_counter() - t0
    ev_b = _ttfb_events[mark:]
    b_content = "".join(content_parts)
    b = {
        "wall_s": round(wall_b, 2),
        "ttfb_s": round(sum(e["ttfb_s"] for e in ev_b), 2),
        "create_return_s": round(t_created, 2),
        "first_chunk_s": round(first_chunk_s, 2) if first_chunk_s is not None else None,
        "first_content_s": round(first_content_s, 2) if first_content_s is not None else None,
        "chunks": n_chunks,
        "content": b_content,
        "tool_calls": [v["function"]["name"] for v in
                       (tool_calls[i] for i in sorted(tool_calls))],
        "reasoning_chars": len("".join(reasoning_parts)),
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        },
        "finish_reason": finish_reason,
    }
    _log("[ab] B 流式: wall=%.1fs create=%.1fs first_chunk=%s first_content=%s chunks=%d content=%dc calls=%s"
         % (wall_b, t_created, b["first_chunk_s"], b["first_content_s"], n_chunks,
            len(b_content), b["tool_calls"]))

    # 结构一致性（不看正文内容——temperature 抽样波动，见函数 docstring）
    same_kind = (bool(a["tool_calls"]) == bool(b["tool_calls"]))
    same_content = (a["content"] == b["content"])
    same_calls = (a["tool_calls"] == b["tool_calls"])
    gain = (a["first_visible_s"] - (b["first_content_s"] or b["first_chunk_s"] or 0))

    L = ["# LLM 流式 A/B 对照（实测）", "",
         "- **输入**: `%s`（%d msgs / %d tools）" % (p.name, len(messages), len(tools)),
         "- **模型出口**: `app.agent.llm.get_model()`（与主链同一实例构造路径）", "",
         "## 结果", "",
         "| 指标 | A 非流式（现状） | B 流式（候选） |", "|---|---|---|",
         "| 墙钟 | %.1fs | %.1fs |" % (a["wall_s"], b["wall_s"]),
         "| TTFB（响应头） | %.1fs | %.1fs |" % (a["ttfb_s"], b["ttfb_s"]),
         "| 用户首次可见输出 | **%.1fs**（整段一起到） | **%.1fs**（首个正文增量） |" % (
             a["first_visible_s"], b["first_content_s"] or -1),
         "| （参考）首个 chunk 任意内容 | - | %.2fs |" % (b["first_chunk_s"] or -1),
         "| content 长度 | %d | %d |" % (len(a["content"]), len(b["content"])),
         "| tool_calls | %s | %s |" % (a["tool_calls"] or "-", b["tool_calls"] or "-"),
         "| reasoning 长度 | %d | %d |" % (a["reasoning_chars"], b["reasoning_chars"]),
         "| chunk 数 | - | %d |" % b["chunks"],
         "| usage | - | in=%s out=%s |" % (b["usage"]["input_tokens"], b["usage"]["output_tokens"]),
         "",
         "## 一致性判定", "",
         "- 结构同类（都是工具调用 / 都是正文）: **%s**" % ("是" if same_kind else "否"),
         "- content 完全一致: %s（temperature 抽样波动下不要求一致，仅记录）" % ("是" if same_content else "否"),
         "- tool_calls 一致: %s（同上）" % ("是" if same_calls else "否"),
         "",
         "## 结论", "",
         "- 流式让用户首次可见输出从 **%.1fs 提前到 %.1fs**（提前 %.1fs）" % (
             a["first_visible_s"], b["first_content_s"] or 0, gain),
         "- 总时长 %.1fs vs %.1fs（%s；两次请求输出量不同，墙钟不可直接比）" % (
             a["wall_s"], b["wall_s"],
             "基本持平" if abs(a["wall_s"] - b["wall_s"]) < 2 else "有差异"),
         "- 结构一致性 %s ⇒ 「改流式」%s" % (
             "通过" if same_kind else "**未通过**",
             "可视为零行为风险改造" if same_kind else "有行为差异，不能直接上"),
         ""]
    f = ab_dir / "TTFB_AB.md"
    f.write_text("\n".join(L), encoding="utf-8")
    (ab_dir / "ttfb_ab.json").write_text(json.dumps(
        {"input": p.name, "A": a, "B": b,
         "same_kind": same_kind, "same_content": same_content, "same_calls": same_calls},
        ensure_ascii=False, indent=2), encoding="utf-8")
    _log("[ab] 报告已写出: %s" % f)
    return 0


def _run_replay(paths: list) -> int:
    """独立重放：把 call_NN_full.json 的 messages+tools **直发网关**，跳过 agent。

    【判据】与捕获时的墙钟对照：
      重放也慢 ⇒ 模型/网关/缓存问题（agent 无罪）；
      重放明显快 ⇒ agent 侧开销（预选/技能/工具面/步数）才是主因。
    路径可为若干 call_NN_full.json，或一个目录（取其下全部 *_full.json）。
    """
    install()   # 挂 TTFB hooks；与捕获同口径
    from app.agent.qd_service import _build_default_model as _gm

    files: list = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            files.extend(sorted(pp.glob("*_full.json")))
        elif pp.is_file():
            files.append(pp)
    if not files:
        _log("[replay] 未找到 call_NN_full.json")
        return 2

    rows = []
    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        messages = data.get("messages") or []
        tools = data.get("tools_sent")
        tool_choice = data.get("tool_choice")
        orig_s = None
        brief = fp.with_name(fp.name.replace("_full.json", ".json"))
        if brief.is_file():
            try:
                orig_s = (json.loads(brief.read_text(encoding="utf-8"))
                          .get("latency", {}).get("wall_s"))
            except Exception:
                pass

        kwargs = {}
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        # 与主链同一构造路径（含 stream/QD_LLM_TIMEOUT），但无 agent/预选/技能
        m = _gm()
        mark = len(_ttfb_events)
        t0 = time.perf_counter()
        err = ""
        out_chars = 0
        tool_names = []
        try:
            payload = m.query(list(messages), **kwargs)
            out_chars = len(str((payload or {}).get("content") or ""))
            for tc in (payload or {}).get("tool_calls") or []:
                if isinstance(tc, dict):
                    tool_names.append(str(((tc.get("function") or {}).get("name") or "?")))
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:120])
        wall = time.perf_counter() - t0
        ev = _ttfb_events[mark:]
        ttfb = sum(e.get("ttfb_s", 0) for e in ev)
        ratio = (wall / orig_s) if (orig_s and orig_s > 0) else None
        rows.append({
            "file": fp.name, "msgs": len(messages),
            "tools": len(tools or []),
            "orig_wall_s": orig_s, "replay_wall_s": round(wall, 2),
            "ttfb_s": round(ttfb, 2), "ratio": round(ratio, 2) if ratio else None,
            "out_chars": out_chars, "tool_calls": tool_names, "error": err,
        })
        _log("[replay] %s: wall=%.1fs orig=%s ratio=%s ttfb=%.1fs tools=%s %s" % (
            fp.name, wall,
            ("%.1fs" % orig_s) if orig_s else "?",
            ("%.2f" % ratio) if ratio else "?",
            ttfb, ",".join(tool_names) or "-", err))

    tot = sum(r["replay_wall_s"] for r in rows)
    tot_o = sum(r["orig_wall_s"] or 0 for r in rows)
    L = ["# 独立重放对照（跳过 agent）", "",
         "- **输入**: `call_NN_full.json` 的 messages + tools，经 `llm.get_model().query` 直发",
         "- **判据**: 重放墙钟 ≈ 捕获墙钟 ⇒ 模型/网关；重放明显快 ⇒ agent 侧开销", "",
         "| 文件 | msgs | tools | 捕获墙钟 | 重放墙钟 | 比值 | TTFB | 输出c | tool_calls | 错误 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %d | %d | %s | **%.1fs** | %s | %.1fs | %d | %s | %s |" % (
            r["file"], r["msgs"], r["tools"],
            ("%.1fs" % r["orig_wall_s"]) if r["orig_wall_s"] else "?",
            r["replay_wall_s"],
            ("%.2f" % r["ratio"]) if r["ratio"] else "?",
            r["ttfb_s"], r["out_chars"],
            ",".join(r["tool_calls"]) or "-", r["error"] or "-"))
    L += ["", "**合计**: 重放 %.1fs / 捕获 %s" % (
        tot, ("%.1fs" % tot_o) if tot_o else "?"), ""]
    out = _REPO / "tmp" / "llm_replay" / (
        "REPLAY_%s.md" % time.strftime("%Y%m%d_%H%M%S"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    (out.parent / ("REPLAY_%s.json" % time.strftime("%Y%m%d_%H%M%S"))).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _log("[replay] 报告: %s" % out)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--ttfb-ab":
        if len(argv) < 2:
            _log("用法: --ttfb-ab <call_NN_full.json>")
            return 2
        install()
        return _run_ttfb_ab(argv[1])
    if argv and argv[0] == "--replay":
        if len(argv) < 2:
            _log("用法: --replay <call_NN_full.json|目录> [...]")
            return 2
        return _run_replay(argv[1:])
    install()
    from app.agent import cli
    sys.argv = ["app.agent.cli"] + argv
    code = cli.main()
    # 无论成功/异常都出报告（异常路径下已抓到的调用仍有诊断价值）
    try:
        f = write_report()
        _log("[probe] 报告已写出: %s" % f)
    except Exception as e:
        _log("[probe] 报告生成失败(忽略): %s: %s" % (type(e).__name__, e))
    return code


if __name__ == "__main__":
    raise SystemExit(main())