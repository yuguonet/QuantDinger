"""
OpenAI Function Calling 兼容的 Web 工具集
提供 web_fetch 和 web_search 两个工具，可直接用于 OpenAI Chat Completions API 的 tools 参数。
"""

import json
import re
import html
import urllib.request
import urllib.parse
from typing import Any

# ── 常量 ──────────────────────────────────────────────
MAX_BYTES = 512 * 1024  # 512 KB
HTTP_TIMEOUT = 30  # seconds

# ── OpenAI Function Calling Schema ───────────────────

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return its text content (HTML tags stripped).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to fetch.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (DuckDuckGo) and return the top result titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]


# ── 内部工具函数 ──────────────────────────────────────

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t\n]+")

_DDG_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r".*?<a[^>]*class=\"result__snippet\"[^>]*>(.*?)</a>",
    re.DOTALL,
)


def _strip_html(s: str) -> str:
    """Remove HTML tags, collapse whitespace, truncate."""
    s = _TAG_RE.sub(" ", s)
    s = _ANY_TAG.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub("\n", s)
    s = s.strip()
    if len(s) > MAX_BYTES:
        s = s[:MAX_BYTES] + "\n... [truncated]"
    return s


def _http_get(url: str, max_bytes: int = MAX_BYTES) -> tuple[int, bytes]:
    """GET with timeout, return (status_code, body_bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": "PicoClaw/0.1"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read(max_bytes)
        return resp.status, data


# ── 工具实现 ──────────────────────────────────────────


def web_fetch(url: str) -> str:
    """
    Fetch a URL and return '[status] url\\n\\n<stripped text>'.

    Args:
        url: Absolute http(s) URL to fetch.

    Returns:
        Formatted string with status code and page text.
    """
    if not url.startswith("http://") and not url.startswith("https://"):
        raise ValueError("url must be http(s)")

    status, data = _http_get(url)
    text = _strip_html(data.decode("utf-8", errors="replace"))
    return f"[{status}] {url}\n\n{text}"


def web_search(query: str) -> str:
    """
    Search DuckDuckGo and return top 8 results.

    Args:
        query: Search query string.

    Returns:
        Numbered list of results with title, link, and snippet.
    """
    query = query.strip()
    if not query:
        raise ValueError("empty query")

    endpoint = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0 (PicoClaw)"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read(512 * 1024)

    matches = _DDG_RESULT_RE.findall(data.decode("utf-8", errors="replace"))
    if not matches:
        return "no results"

    lines: list[str] = []
    for i, (link, title, snippet) in enumerate(matches[:8], 1):
        title = _strip_html(title)
        link = html.unescape(link)
        snippet = _strip_html(snippet)
        lines.append(f"{i}. {title}\n   {link}\n   {snippet}")
    return "\n".join(lines)


# ── 统一调度入口 ──────────────────────────────────────

_DISPATCH = {
    "web_fetch": lambda args: web_fetch(**args),
    "web_search": lambda args: web_search(**args),
}


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """
    按 OpenAI function_call 格式调度工具。

    Args:
        name: 工具名称 (web_fetch / web_search)。
        arguments: 参数字典，与 schema 中定义一致。

    Returns:
        工具返回的文本结果。
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return fn(arguments)


def call_tool_from_message(tool_call: dict[str, Any]) -> dict[str, Any]:
    """
    直接接受 OpenAI ChatCompletion 里的单个 tool_call 对象，
    返回可拼接到 messages 里的 tool role 消息。

    用法示例::

        # 在拿到 response.choices[0].message.tool_calls 后：
        for tc in response.choices[0].message.tool_calls:
            result_msg = call_tool_from_message(tc.model_dump())
            messages.append(result_msg)

    Args:
        tool_call: OpenAI tool_call 对象 (含 id, function.name, function.arguments)。

    Returns:
        {"role": "tool", "tool_call_id": ..., "content": ...}
    """
    fn = tool_call["function"]
    name = fn["name"]
    args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
    result = call_tool(name, args)
    return {
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "content": result,
    }


# ── 示例：完整一轮对话 ────────────────────────────────

if __name__ == "__main__":
    import openai  # pip install openai

    client = openai.OpenAI()  # 从环境变量读 OPENAI_API_KEY

    messages = [
        {"role": "user", "content": "帮我查一下 Python 3.13 有什么新特性"}
    ]

    # 第一轮：让模型决定是否调用工具
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=TOOLS_SCHEMA,
        tool_choice="auto",
    )

    msg = response.choices[0].message

    # 如果模型调用了工具
    if msg.tool_calls:
        messages.append(msg.model_dump())  # 把 assistant 消息（含 tool_calls）加入历史

        for tc in msg.tool_calls:
            tool_msg = call_tool_from_message(tc.model_dump())
            print(f"[Tool] {tc.function.name}({tc.function.arguments})")
            print(f"[Result] {tool_msg['content'][:200]}...\n")
            messages.append(tool_msg)

        # 第二轮：让模型基于工具结果生成最终回答
        final = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
        )
        print(final.choices[0].message.content)
    else:
        print(msg.content)
