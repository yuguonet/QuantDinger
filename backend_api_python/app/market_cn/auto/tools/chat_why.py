#!/usr/bin/env python3
"""auto/tools/chat_why.py — 口语化策略调试聊天 (2026-09-26 T18)

目的: 让调试变得「像跟同事说话」— 自然语言 → 自动调 why/debug/doctor → 人话解读。
LLM 只负责**意图理解与解读**; 判定一律转调既有 CLI (零第二份规则)。

与后续 agent skill 的关系 (用户 2026-09-26 裁定: 先轻量 chat_why):
  本文件 = 可替换的「前端」; 工具层 = why/debug/doctor。
  满意后把同一套 tool 声明注册进 app/agent skill, 本文件可退役或降级为调试壳。

用法:
  python -m app.market_cn.auto.tools.chat_why
  python -m app.market_cn.auto.tools.chat_why --strategy t_hilo --code 600519
  python -m app.market_cn.auto.tools.chat_why --model qwen-plus --no-llm   # 纯工具模式

对话协议 (模型输出, 解析后执行):
  {"tool":"why|debug|doctor","args":{...}}   → 执行并把结果喂回模型
  其它文本                                    → 直接展示 (最终回答)

环境变量 (与 app/agent 同源):
  LLM_PROVIDER / OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL / AGENT_LLM_TEMPERATURE
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback


def _load_env():
    try:
        from dotenv import load_dotenv
        for _p in (os.path.join(os.getcwd(), ".env"),
                   os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                       os.path.dirname(os.path.abspath(__file__))))), ".env")):
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass


# ================================================================
# 工具面 (薄封装, 全部转调既有 CLI — 唯一判定事实源)
# ================================================================

TOOLS = {
    "why": {
        "desc": "多日粗扫/单日深潜/参数试调/库对照。args: strategy, code, date?, days?, params?, db?",
        "args": {
            "strategy": "str 策略key (t_hilo/v1/break/dragon_callback/...)",
            "code": "str 股票代码",
            "date": "str 可选 YYYY-MM-DD 单日深潜",
            "days": "int 可选 多日窗口",
            "params": "dict 可选 临时参数覆盖",
            "db": "bool 可选 是否对照库内信号",
        },
    },
    "debug": {
        "desc": "单日逐门/TRACE 显微镜 (比 why 更细)。args: strategy, code, date, days?, max_lu?",
        "args": {
            "strategy": "str", "code": "str", "date": "str",
            "days": "int 可选", "max_lu": "int 可选",
        },
    },
    "doctor": {
        "desc": "系统体检: config/注册表/库/层反转。args: strategy? (可选过滤)",
        "args": {"strategy": "str 可选"},
    },
}


def _run_tool(name: str, args: dict) -> str:
    """执行工具, 捕获 stdout+stderr 为字符串 (不抛出到对话)。"""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            if name == "why":
                from app.market_cn.auto.tools import why as m
                argv = ["--strategy", str(args.get("strategy") or ""),
                        "--code", str(args.get("code") or "")]
                if args.get("date"):
                    argv += ["--date", str(args["date"])]
                if args.get("days") is not None:
                    argv += ["--days", str(int(args["days"]))]
                if args.get("params"):
                    p = args["params"]
                    argv += ["--params", p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)]
                if args.get("db"):
                    argv.append("--db")
                code = int(m.main(argv) or 0)
            elif name == "debug":
                from app.market_cn.auto.tools import debug as m
                argv = ["--strategy", str(args.get("strategy") or ""),
                        "--code", str(args.get("code") or ""),
                        "--date", str(args.get("date") or "")]
                if args.get("days") is not None:
                    argv += ["--days", str(int(args["days"]))]
                if args.get("max_lu") is not None:
                    argv += ["--max-lu", str(int(args["max_lu"]))]
                old = sys.argv
                sys.argv = ["debug"] + argv
                try:
                    code = int(m.main() or 0)
                finally:
                    sys.argv = old
            elif name == "doctor":
                from app.market_cn.auto.tools import doctor as m
                argv = ["--static"]
                if args.get("strategy"):
                    argv += ["--strategy", str(args["strategy"])]
                code = int(m.main(argv) or 0)
            else:
                buf.write(f"未知工具 {name}\n")
                code = 2
    except Exception:
        buf.write(traceback.format_exc())
        code = 1
    out = buf.getvalue() or "(无输出)"
    return f"[exit={code}]\n{out[:12000]}"


# ================================================================
# LLM 接入 (create_llm 优先; 失败则 OpenAI 兼容 HTTP)
# ================================================================

SYSTEM_PROMPT = """你是 QuantDinger 自动策略调试助手。用户用口语描述策略问题, 你调用工具取证并解读。

硬规则:
1. 判定/数值一律来自工具输出; 禁止编造信号、胜率、门值。
2. 需要取证时输出 **仅一行 JSON** (不要其它文字):
   {"tool":"why|debug|doctor","args":{...}}
3. 有工具结果后, 用简体中文解释: 发生了什么、卡在哪、下一步建议。
4. 默认会话上下文 strategy/code 可复用, 未指明时不要反复问。
5. 改参数试调用 why 的 args.params, 并提醒「仅本进程, 未写 config」。
6. 涉及实盘/上线: 强调需两段稳定性 + pool_check, 不要轻率说「可以上线」。

工具:
{tool_brief}

输出 JSON 示例:
{{"tool":"why","args":{{"strategy":"t_hilo","code":"600519","days":15,"db":true}}}}
"""


def _tool_brief() -> str:
    lines = []
    for n, t in TOOLS.items():
        lines.append(f"- {n}: {t['desc']}")
    return "\n".join(lines)


class LLMClient:
    """async create_llm 或同步 HTTP 兜底。"""

    def __init__(self, model="", api_key="", base_url=""):
        self.model = model or os.getenv("OPENAI_MODEL", "qwen-plus")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                         or "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.temperature = float(os.getenv("AGENT_LLM_TEMPERATURE", "0.2"))
        self._llm = None
        try:
            from app.agent.llm import create_llm
            self._llm = create_llm({
                "provider": os.getenv("LLM_PROVIDER", "openai"),
                "model": self.model,
                "api_key": self.api_key,
                "base_url": self.base_url,
                "temperature": self.temperature,
                "max_tokens": 2048,
            })
        except Exception:
            self._llm = None

    def chat(self, messages: list) -> str:
        """messages: [{role, content}] → 文本。"""
        if self._llm is not None:
            return self._chat_via_factory(messages)
        return self._chat_http(messages)

    def _chat_via_factory(self, messages: list) -> str:
        import asyncio
        from app.agent.llm import ChatMessage
        cms = [ChatMessage(role=m["role"], content=m["content"]) for m in messages]
        try:
            resp = asyncio.get_event_loop().run_until_complete(
                self._llm.generate(cms))
            return getattr(resp, "content", None) or str(resp)
        except RuntimeError:
            # 无 running loop
            return asyncio.run(self._llm.generate(cms))

    def _chat_http(self, messages: list) -> str:
        import urllib.request
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 2048,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


# ================================================================
# 对话循环
# ================================================================

def _extract_tool_call(text: str):
    """从模型输出抽 {"tool":...} (容忍 ```json 包裹与嵌套 args)。"""
    if not text:
        return None
    t = text.strip()
    # 1) 整段就是 JSON
    for candidate in (t, re.sub(r"^```[a-zA-Z]*\n?|```$", "", t, flags=re.M).strip()):
        try:
            d = json.loads(candidate)
            if isinstance(d, dict) and d.get("tool"):
                return d
        except Exception:
            pass
    # 2) 代码块优先
    for m in re.finditer(r"```[a-zA-Z]*\n(.*?)```", text, re.S):
        try:
            d = json.loads(m.group(1).strip())
            if isinstance(d, dict) and d.get("tool"):
                return d
        except Exception:
            continue
    # 3) 花括号配平扫描 (支持 args 内嵌套)
    start = text.find("{")
    while start >= 0:
        depth = 0
        for j in range(start, len(text)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    frag = text[start:j + 1]
                    try:
                        d = json.loads(frag)
                        if isinstance(d, dict) and d.get("tool"):
                            return d
                    except Exception:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def _print(s):
    print(s, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="qd chat",
        description="口语化策略调试 (LLM 转调 why/debug/doctor)")
    ap.add_argument("--strategy", default="", help="会话默认策略 key")
    ap.add_argument("--code", default="", help="会话默认股票代码")
    ap.add_argument("--model", default="", help="模型名 (默认 OPENAI_MODEL)")
    ap.add_argument("--no-llm", action="store_true",
                    help="不开 LLM: 只解释工具语法 (离线冒烟)")
    ap.add_argument("--max-rounds", type=int, default=6, help="单次提问最大工具轮数")
    args = ap.parse_args(argv)

    _load_env()
    ctx = {"strategy": args.strategy, "code": args.code, "params": {}}
    llm = None
    if not args.no_llm:
        try:
            llm = LLMClient(model=args.model)
        except Exception as e:
            _print(f"[warn] LLM 初始化失败, 转工具模式: {e}")
            llm = None

    _print("=== chat_why | 口语化策略调试 ===")
    _print(f"默认 context: strategy={ctx['strategy'] or '(未设)'} code={ctx['code'] or '(未设)'}")
    _print("示例: 「600519 最近为什么不出信号」「把 stop 放宽到 -5 再扫」「看下 2026-09-18 卡在哪」")
    _print("命令: /ctx strategy=xxx code=xxx  |  /params {json}  |  /quit\n")

    history = []
    if llm:
        history.append({"role": "system",
                        "content": SYSTEM_PROMPT.format(tool_brief=_tool_brief())})

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            _print("")
            break
        if not user:
            continue
        if user in ("/quit", "/exit", "quit", "exit"):
            break
        if user.startswith("/ctx"):
            for kv in user.split()[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    ctx[k.strip()] = v.strip()
            _print(f"context = {ctx}")
            continue
        if user.startswith("/params"):
            raw = user[len("/params"):].strip() or "{}"
            try:
                ctx["params"] = json.loads(raw)
                _print(f"params = {ctx['params']} (后续 why 自动带上)")
            except Exception as e:
                _print(f"params 解析失败: {e}")
            continue
        if user in ("/help", "help"):
            _print(json.dumps(TOOLS, ensure_ascii=False, indent=2))
            continue

        if llm is None:
            _print(_offline_hint(user, ctx))
            continue

        # ---- LLM 多轮: 意图 → 工具 → 解读 ----
        history.append({"role": "user", "content": user})
        for _round in range(args.max_rounds):
            try:
                reply = llm.chat(history)
            except Exception as e:
                _print(f"[llm error] {e}")
                break
            call = _extract_tool_call(reply)
            if not call:
                _print(f"ai> {reply.strip()}")
                history.append({"role": "assistant", "content": reply})
                break
            tname = call.get("tool")
            targs = call.get("args") or {}
            # 填会话默认
            targs.setdefault("strategy", ctx["strategy"])
            targs.setdefault("code", ctx["code"])
            if ctx.get("params") and tname == "why" and "params" not in targs:
                targs["params"] = ctx["params"]
            _print(f"→ tool {tname} {json.dumps(targs, ensure_ascii=False)}")
            result = _run_tool(tname, targs)
            history.append({"role": "assistant", "content": reply})
            history.append({"role": "user",
                            "content": f"[tool_result {tname}]\n{result}\n请解读并决定是否继续调工具或给出最终建议。"})
        else:
            _print("(已达本轮最大工具次数, 请换更具体的问题)")
        # 防历史过长
        if len(history) > 40:
            history = history[:1] + history[-24:]
    return 0


def _offline_hint(user: str, ctx: dict) -> str:
    """--no-llm 时的本地意图粗匹配 (便于冒烟, 非正式 NLU)。"""
    s = ctx.get("strategy") or "t_hilo"
    c = ctx.get("code") or "600519"
    if "为什么" in user or "不出" in user or "没出" in user:
        return (f"(offline) 建议: why --strategy {s} --code {c} --days 15\n"
                f"       深挖: why --strategy {s} --code {c} --date <日期>")
    if "体检" in user or "doctor" in user.lower() or "配置" in user:
        return "(offline) 建议: doctor --static"
    if "参数" in user or "放宽" in user:
        return f'(offline) 建议: why --strategy {s} --code {c} --params \'{{"stop":-5}}\' --days 10'
    return f"(offline) 当前 context={ctx}。说「为什么不出信号」或 /help"


if __name__ == "__main__":
    sys.exit(main())
