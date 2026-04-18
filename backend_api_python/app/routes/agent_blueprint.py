# -*- coding: utf-8 -*-
"""
/api/agent/* — AI Agent 聊天 & 流式接口
适配 eQuant 项目，数据通过 DataSourceFactory 取得。

v2 — LLM 驱动的 Agent 实现，支持会话记忆和工具调用。
"""
import os
import json
import logging
import uuid
import threading
import queue
import re
import time
from typing import Any, Dict, List, Optional

from flask import Blueprint, request, jsonify, Response

logger = logging.getLogger(__name__)

# ── 策略加载器（延迟导入，避免循环依赖）─────────────────
def _load_strategies():
    """加载 YAML 策略列表。"""
    try:
        from app.services.strategy_loader import load_strategies
        return load_strategies()
    except Exception as e:
        logger.warning("策略加载器不可用: %s", e)
        return []


def _get_strategy_by_id(sid: str):
    """按 ID 获取单个策略。"""
    try:
        from app.services.strategy_loader import get_strategy_by_id
        return get_strategy_by_id(sid)
    except Exception:
        return None

# ── Blueprint ─────────────────────────────────────────────
agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

# ── 工具中文名映射 ────────────────────────────────────────
TOOL_DISPLAY_NAMES: Dict[str, str] = {
    "get_realtime_quote": "获取实时行情",
    "get_daily_history": "获取历史K线",
    "get_chip_distribution": "分析筹码分布",
    "get_stock_info": "获取股票基本面",
    "search_stock_news": "搜索股票新闻",
    "analyze_trend": "分析技术趋势",
    "calculate_ma": "计算均线系统",
    "get_volume_analysis": "分析量能变化",
    "analyze_pattern": "识别K线形态",
    "get_market_indices": "获取市场指数",
    "get_sector_rankings": "分析行业板块",
}

# ── System Prompt ─────────────────────────────────────────
BASE_SYSTEM_PROMPT = """你是 eQuant 金融分析助手，一个专业的 AI 投资顾问。

## 你的能力
你可以调用以下工具获取数据，帮助用户分析股票和市场：

1. **get_realtime_quote** — 获取股票实时行情（价格、涨跌幅、成交量等）
   参数：stock_code（股票代码，如 000001）
2. **get_daily_history** — 获取股票历史K线数据（日线）
   参数：stock_code, days（默认30天）
3. **get_stock_info** — 获取股票基本面信息（公司简介、行业、市值等）
   参数：stock_code
4. **get_chip_distribution** — 分析筹码分布
   参数：stock_code
5. **analyze_trend** — 分析技术趋势
   参数：stock_code
6. **search_stock_news** — 搜索股票相关新闻
   参数：stock_code, keyword（可选）
7. **get_market_indices** — 获取大盘指数行情
8. **get_sector_rankings** — 获取行业板块涨跌排名

## 回复规则
- 用中文回复，风格专业但亲切
- 直接回答用户问题，不要废话
- 数据引用时给出具体数字
- 如果需要调用工具，用以下格式输出：

```
CALL_TOOL:{"tool": "工具名", "params": {"stock_code": "000001"}}
```

- 一次只调用一个工具
- 工具结果返回后，用自然语言总结分析
- 如果不需要工具，直接用自然语言回复
- 不要输出 JSON 给用户看

## 注意
- A股代码通常是6位数字（如 000001 平安银行、600519 贵州茅台）
- 如果用户没有明确股票代码，且对话历史中有提到过，沿用之前的代码
- 对话中保持上下文连贯性
"""


def _build_system_prompt(skills: Optional[List[str]] = None) -> str:
    """根据激活的 YAML 策略构建增强版 system prompt。"""
    prompt = BASE_SYSTEM_PROMPT

    if not skills:
        return prompt

    # 加载选中的策略 instructions 注入到 prompt
    strategy_sections = []
    for sid in skills:
        strat = _get_strategy_by_id(sid)
        if not strat:
            continue
        instructions = strat.get("instructions", "")
        if not instructions:
            continue
        strategy_sections.append(
            f"### 当前激活策略：{strat['name']}（{sid}）\n"
            f"{instructions}"
        )

    if strategy_sections:
        prompt += (
            "\n\n## 当前激活的分析策略\n"
            "用户已选择以下分析策略，请严格按照策略框架执行分析：\n\n"
            + "\n\n".join(strategy_sections)
        )

    return prompt

# ── 会话存储（内存）───────────────────────────────────────
# session_id → {"messages": [...], "created_at": float, "updated_at": float, "stock_code": str|None}
_sessions: Dict[str, Dict] = {}
MAX_HISTORY_TURNS = 20  # 保留最近 20 轮对话


def _get_session(session_id: str) -> Dict:
    """获取或创建会话。"""
    if session_id not in _sessions:
        _sessions[session_id] = {
            "messages": [],
            "created_at": time.time(),
            "updated_at": time.time(),
            "stock_code": None,
        }
    return _sessions[session_id]


def _append_message(session_id: str, role: str, content: str):
    """向会话历史追加消息，超出上限自动裁剪。"""
    session = _get_session(session_id)
    session["messages"].append({"role": role, "content": content})
    session["updated_at"] = time.time()
    # 保留 system + 最近 N*2 条（N 轮 = user+assistant 各 N 条）
    max_msgs = MAX_HISTORY_TURNS * 2
    if len(session["messages"]) > max_msgs:
        session["messages"] = session["messages"][-max_msgs:]


def _build_messages(session_id: str, user_message: str,
                    skills: Optional[List[str]] = None) -> List[Dict]:
    """组装发给 LLM 的 messages 列表（system + 历史 + 新消息）。"""
    session = _get_session(session_id)
    system_prompt = _build_system_prompt(skills)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(session["messages"])
    messages.append({"role": "user", "content": user_message})
    return messages


# ── 工具执行 ─────────────────────────────────────────────

def _get_ds(market: str = "AShare"):
    """从 DataSourceFactory 获取数据源。"""
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)


def _detect_market(stock_code: str) -> str:
    """根据股票代码粗判市场。"""
    code = (stock_code or "").strip().upper()
    if code.startswith(("SH", "SZ", "BJ")):
        return "AShare"
    if code.startswith(("HK",)):
        return "HShare"
    if len(code) <= 5 and code.isdigit():
        return "AShare"
    return "Crypto"


def _exec_tool(tool_name: str, params: Dict, market: str = "AShare") -> Any:
    """执行单个工具调用，返回结果。"""
    ds = _get_ds(market)
    stock_code = params.get("stock_code", "")

    try:
        if tool_name == "get_realtime_quote":
            return ds.get_ticker(stock_code)
        elif tool_name == "get_daily_history":
            days = params.get("days", 30)
            return ds.get_kline(stock_code, "1D", days)
        elif tool_name == "get_stock_info":
            return ds.get_stock_info(stock_code)
        elif tool_name == "get_chip_distribution":
            # 部分数据源可能没有此方法
            if hasattr(ds, "get_chip_distribution"):
                return ds.get_chip_distribution(stock_code)
            return {"error": "当前数据源不支持筹码分布分析"}
        elif tool_name == "analyze_trend":
            # 用 kline 数据做简单趋势分析
            klines = ds.get_kline(stock_code, "1D", 60)
            if not klines:
                return {"error": "无法获取K线数据"}
            closes = [k.get("close", 0) for k in klines if k.get("close")]
            if len(closes) < 2:
                return {"error": "数据不足"}
            latest = closes[-1]
            prev = closes[-2]
            ma5 = sum(closes[-5:]) / min(5, len(closes))
            ma20 = sum(closes[-20:]) / min(20, len(closes))
            ma60 = sum(closes) / len(closes)
            return {
                "latest_close": latest,
                "change_pct": round((latest - prev) / prev * 100, 2) if prev else 0,
                "ma5": round(ma5, 2),
                "ma20": round(ma20, 2),
                "ma60": round(ma60, 2),
                "trend": "多头" if ma5 > ma20 > ma60 else ("空头" if ma5 < ma20 < ma60 else "震荡"),
                "data_points": len(closes),
            }
        elif tool_name == "search_stock_news":
            # 简单返回提示，实际需要对接新闻 API
            keyword = params.get("keyword", stock_code)
            return {"message": f"新闻搜索功能待接入，搜索关键词: {keyword}"}
        elif tool_name == "get_market_indices":
            if hasattr(ds, "get_index_quotes"):
                return ds.get_index_quotes(["000001", "399001", "399006"])
            return {"error": "当前数据源不支持指数查询"}
        elif tool_name == "get_sector_rankings":
            if hasattr(ds, "get_sector_fund_flow"):
                return ds.get_sector_fund_flow()
            return {"error": "当前数据源不支持板块排名"}
        else:
            return {"error": f"未知工具: {tool_name}"}
    except NotImplementedError:
        return {"error": f"工具 {tool_name} 在当前数据源中未实现"}
    except Exception as e:
        logger.error("Tool %s error: %s", tool_name, e, exc_info=True)
        return {"error": str(e)}


def _extract_stock_code(msg: str, ctx: Optional[Dict], session: Dict) -> Optional[str]:
    """从上下文、消息中提取股票代码。"""
    # 优先用 context 中的
    if ctx and ctx.get("stock_code"):
        return ctx["stock_code"]
    # 从消息中提取 6 位数字
    m = re.search(r"\b(\d{6})\b", msg)
    if m:
        return m.group(1)
    # 从会话历史中沿用
    return session.get("stock_code")


def _extract_tool_call(llm_response: str) -> Optional[Dict]:
    """从 LLM 回复中解析工具调用指令。"""
    # 匹配 CALL_TOOL:{"tool": "...", "params": {...}}
    m = re.search(r"CALL_TOOL:\s*(\{.*?\})", llm_response, re.DOTALL)
    if m:
        try:
            tool_call = json.loads(m.group(1))
            if "tool" in tool_call:
                return tool_call
        except json.JSONDecodeError:
            pass
    return None


# ── AgentExecutor ─────────────────────────────────────────

class _AgentExecutor:
    """
    LLM 驱动的 AgentExecutor。
    - 集成 LLMService 做意图理解和自然语言生成
    - 内存会话历史，支持多轮上下文
    - 两步工具调用：LLM 判断 → 执行工具 → LLM 总结
    """

    def __init__(self, skills: Optional[List[str]] = None):
        self.skills = skills or []
        from app.services.llm import LLMService
        self.llm_service = LLMService()

    def chat(self, message: str, session_id: str,
             context: Optional[Dict] = None,
             progress_callback=None) -> Any:
        """同步聊天。"""
        try:
            if progress_callback:
                progress_callback({"type": "thinking"})

            # 获取会话和提取股票代码
            session = _get_session(session_id)
            stock_code = _extract_stock_code(message, context, session)
            market = _detect_market(stock_code) if stock_code else "AShare"

            # 记录股票代码到会话
            if stock_code:
                session["stock_code"] = stock_code

            # 追加用户消息到历史
            _append_message(session_id, "user", message)

            # 构建 messages 发给 LLM（注入策略 instructions）
            messages = _build_messages(session_id, message, self.skills)

            # 第一步：LLM 判断意图
            try:
                llm_response = self.llm_service.call_llm_api(
                    messages,
                    use_json_mode=False,  # 对话场景，不要 JSON 模式
                    temperature=0.7,
                )
            except Exception as e:
                logger.error("LLM call failed: %s", e, exc_info=True)
                # LLM 不可用时降级到关键词匹配
                answer = self._fallback_dispatch(message, stock_code, market, progress_callback)
                _append_message(session_id, "assistant", answer)
                return _Result(success=True, content=answer, error=None)

            # 检查是否需要调用工具
            tool_call = _extract_tool_call(llm_response)

            if tool_call:
                tool_name = tool_call.get("tool", "")
                tool_params = tool_call.get("params", {})

                # 如果工具参数没有 stock_code，自动补上
                if "stock_code" not in tool_params and stock_code:
                    tool_params["stock_code"] = stock_code

                if progress_callback:
                    progress_callback({
                        "type": "tool_start",
                        "tool": tool_name,
                        "display_name": TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
                    })

                # 执行工具
                tool_result = _exec_tool(tool_name, tool_params, market)

                if progress_callback:
                    progress_callback({
                        "type": "tool_done",
                        "tool": tool_name,
                        "display_name": TOOL_DISPLAY_NAMES.get(tool_name, tool_name),
                    })

                # 第二步：把工具结果喂回 LLM，生成最终回复
                tool_result_str = json.dumps(tool_result, ensure_ascii=False, indent=2)
                followup_messages = messages + [
                    {"role": "assistant", "content": llm_response},
                    {"role": "user", "content": f"工具返回结果：\n```json\n{tool_result_str}\n```\n请根据以上数据，用自然语言总结分析。"},
                ]

                if progress_callback:
                    progress_callback({"type": "generating"})

                try:
                    final_response = self.llm_service.call_llm_api(
                        followup_messages,
                        use_json_mode=False,
                        temperature=0.7,
                    )
                except Exception as e:
                    logger.error("LLM followup call failed: %s", e, exc_info=True)
                    # 降级：直接返回工具数据
                    final_response = f"以下是从数据源获取的结果：\n```json\n{tool_result_str}\n```"
            else:
                # 不需要工具，LLM 直接回复
                if progress_callback:
                    progress_callback({"type": "generating"})

                # 去掉可能残留的 CALL_TOOL 格式标记
                final_response = re.sub(r"CALL_TOOL:\s*\{.*?\}", "", llm_response, flags=re.DOTALL).strip()
                if not final_response:
                    final_response = llm_response

            # 追加助手回复到历史
            _append_message(session_id, "assistant", final_response)

            if progress_callback:
                progress_callback({
                    "type": "done",
                    "success": True,
                    "content": final_response,
                })

            return _Result(success=True, content=final_response, error=None)

        except Exception as e:
            logger.error("Agent chat error: %s", e, exc_info=True)
            return _Result(success=False, content="", error=str(e))

    def _fallback_dispatch(self, msg: str, stock_code: Optional[str],
                           market: str, cb) -> str:
        """LLM 不可用时的降级方案——关键词匹配。"""
        msg_low = msg.lower()

        if any(k in msg_low for k in ("行情", "报价", "quote", "price")) and stock_code:
            if cb:
                cb({"type": "tool_start", "tool": "get_realtime_quote"})
            result = _exec_tool("get_realtime_quote", {"stock_code": stock_code}, market)
            if cb:
                cb({"type": "tool_done", "tool": "get_realtime_quote"})
            return json.dumps(result or {}, ensure_ascii=False, indent=2)

        if any(k in msg_low for k in ("k线", "历史", "history", "kline")) and stock_code:
            if cb:
                cb({"type": "tool_start", "tool": "get_daily_history"})
            result = _exec_tool("get_daily_history", {"stock_code": stock_code}, market)
            if cb:
                cb({"type": "tool_done", "tool": "get_daily_history"})
            return f"近 30 日 K 线数据（共 {len(result) if isinstance(result, list) else 0} 条）:\n" + json.dumps(
                (result or [])[-5:], ensure_ascii=False, indent=2)

        return (
            f"收到消息：{msg}\n"
            f"请提供股票代码（6 位数字），我可以查询实时行情或历史 K 线。"
        )


class _Result:
    def __init__(self, success: bool, content: str, error: Optional[str]):
        self.success = success
        self.content = content
        self.error = error
        self.total_steps = 0


# ── 路由 ──────────────────────────────────────────────────

@agent_bp.route("/strategies", methods=["GET"])
def get_strategies():
    """获取可用 Agent 策略（从 YAML 策略文件加载）。"""
    try:
        if os.getenv('AGENT_MODE','true').lower() != 'true':
            return jsonify({"error": "Agent mode is not enabled"}), 400

        strategies = _load_strategies()
        result = [
            {
                "id": s["id"],
                "name": s["name"],
                "description": s.get("description", ""),
                "category": s.get("category", "general"),
            }
            for s in strategies
        ]
        return jsonify({"strategies": result})
    except Exception as e:
        logger.error("Get strategies failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat", methods=["POST"])
def agent_chat():
    """普通聊天接口。"""
    try:
        if os.getenv('AGENT_MODE','true').lower() != 'true':
            return jsonify({"error": "Agent mode is not enabled"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        message = data.get("message")
        if not message:
            return jsonify({"error": "Message is required"}), 400

        session_id = data.get("session_id") or str(uuid.uuid4())
        skills = data.get("skills")
        context = data.get("context")

        executor = _AgentExecutor(skills)
        result = executor.chat(
            message=message,
            session_id=session_id,
            context=context,
        )

        return jsonify({
            "success": result.success,
            "content": result.content,
            "session_id": session_id,
            "error": result.error,
        })
    except Exception as e:
        logger.error("Agent chat failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat/stream", methods=["POST"])
def agent_chat_stream():
    """流式聊天（SSE）。"""
    try:
        if os.getenv('AGENT_MODE','true').lower() != 'true':
            return jsonify({"error": "Agent mode is not enabled"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        message = data.get("message")
        if not message:
            return jsonify({"error": "Message is required"}), 400

        session_id = data.get("session_id") or str(uuid.uuid4())
        skills = data.get("skills")
        context = data.get("context")

        event_queue: queue.Queue = queue.Queue()

        def _cb(event: dict):
            if event.get("type") in ("tool_start", "tool_done"):
                tool = event.get("tool", "")
                event["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
            event_queue.put(event)

        def _run():
            try:
                executor = _AgentExecutor(skills)
                r = executor.chat(message=message, session_id=session_id,
                                  context=context, progress_callback=_cb)
                event_queue.put({
                    "type": "done",
                    "success": r.success,
                    "content": r.content,
                    "error": r.error,
                    "session_id": session_id,
                })
            except Exception as exc:
                logger.error("Agent stream error: %s", exc, exc_info=True)
                event_queue.put({"type": "error", "message": str(exc)})

        def _sse():
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            while True:
                try:
                    ev = event_queue.get(timeout=300)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("type") in ("done", "error"):
                        break
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'error', 'message': '分析超时'}, ensure_ascii=False)}\n\n"
                    break

        return Response(
            _sse(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    except Exception as e:
        logger.error("Agent stream failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@agent_bp.route("/chat/sessions", methods=["GET"])
def list_chat_sessions():
    """获取会话列表。"""
    limit = int(request.args.get("limit", 50))
    sessions = []
    for sid, s in sorted(_sessions.items(), key=lambda x: x[1]["updated_at"], reverse=True)[:limit]:
        sessions.append({
            "session_id": sid,
            "created_at": s["created_at"],
            "updated_at": s["updated_at"],
            "message_count": len(s["messages"]),
            "stock_code": s.get("stock_code"),
        })
    return jsonify({"sessions": sessions})


@agent_bp.route("/chat/sessions/<session_id>", methods=["GET"])
def get_chat_session_messages(session_id: str):
    """获取会话消息。"""
    session = _sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({
        "session_id": session_id,
        "messages": session["messages"],
        "stock_code": session.get("stock_code"),
    })


@agent_bp.route("/chat/sessions/<session_id>", methods=["DELETE"])
def delete_chat_session(session_id: str):
    """删除会话。"""
    if session_id in _sessions:
        del _sessions[session_id]
        return jsonify({"deleted": 1})
    return jsonify({"deleted": 0})
