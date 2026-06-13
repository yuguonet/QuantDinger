#!/usr/bin/env python3
"""
QuantDinger Agent 完整链路测试 — 覆盖 Nanobot + CompatProvider + Skill 全链路。

测试层级：
  1. 纯逻辑测试 — JSON 解析、tool_call 修复、正则提取（自包含，零依赖）
  2. 集成测试   — Skill 注册、工具发现、配置生成（需完整环境 + Flask）
  3. 端到端测试 — NanobotAgent 完整调用（需 LLM 服务）

用法:
  python test_agent_flow_new.py                          # 纯逻辑测试
  python test_agent_flow_new.py --all                    # 全部测试（需 Flask 等依赖）
  python test_agent_flow_new.py --e2e                    # 端到端测试（需 LLM 服务）
  python test_agent_flow_new.py --e2e --stock 600593 --name 大连圣亚
  python test_agent_flow_new.py --skill technical_agent --stock 600593
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════
# 路径设置
# ═══════════════════════════════════════════════════════════════

PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "backend_api_python"
sys.path.insert(0, str(BACKEND_DIR))

# ═══════════════════════════════════════════════════════════════
# 测试框架
# ═══════════════════════════════════════════════════════════════

_results: List[Tuple[str, bool, str]] = []
_skipped: List[str] = []


def test(name: str):
    def decorator(fn):
        fn._test_name = name
        return fn
    return decorator


def run_test(name: str, fn) -> bool:
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  ✅ {name}")
        return True
    except AssertionError as e:
        _results.append((name, False, str(e)))
        print(f"  ❌ {name}: {e}")
        return False
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  💥 {name}: {type(e).__name__}: {e}")
        return False


def skip_test(name: str, reason: str):
    _skipped.append(f"{name} ({reason})")
    print(f"  ⏭️  {name} [跳过: {reason}]")


def assert_eq(a, b, msg=""):
    assert a == b, msg or f"期望 {b!r}，实际 {a!r}"

def assert_in(item, collection, msg=""):
    assert item in collection, msg or f"期望 {item!r} 在 {collection!r} 中"

def assert_true(val, msg=""):
    assert val, msg or f"期望为真，实际为 {val!r}"

def assert_contains(haystack: str, needle: str, msg=""):
    assert needle in haystack, msg or f"期望包含 '{needle}'"


# ═══════════════════════════════════════════════════════════════
# 自包含的纯逻辑函数（从 llm_compat.py 复制，零外部依赖）
# ═══════════════════════════════════════════════════════════════

_KNOWN_TOOLS = frozenset({"call_skill", "final_answer", "search_stock_by_name"})
_KNOWN_TOOL_ALIASES = {
    "call_skill": "call_skill",
    "call_skill_tool": "call_skill",
    "CallSkill": "call_skill",
    "final_answer": "final_answer",
    "FinalAnswer": "final_answer",
    "search_stock_by_name": "search_stock_by_name",
}


def _repair_json(s: str) -> Optional[str]:
    """修复本地模型常见的 JSON 格式错误。"""
    s = s.strip()
    if not s:
        return None
    s = re.sub(r',\s*([}\]])', r'\1', s)
    if "'" in s and '"' not in s:
        s = s.replace("'", '"')
    s = s.replace('\n', '\\n').replace('\r', '\\r')
    try:
        json.loads(s)
        return s
    except (json.JSONDecodeError, TypeError):
        pass
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    if open_braces > 0 or open_brackets > 0:
        repaired = s + ']' * open_brackets + '}' * open_braces
        try:
            json.loads(repaired)
            return repaired
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _parse_tool_calls_from_content(content: str) -> list:
    """从 content 中解析 tool_call JSON。"""
    if not content or not content.strip():
        return []
    candidates = []
    for m in re.finditer(r'```(?:json)?\s*\n?(.*?)\n?\s*```', content, re.DOTALL):
        candidates.append(m.group(1).strip())
    for m in re.finditer(r'\{', content):
        depth = 0
        start = m.start()
        for i in range(start, len(content)):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    candidates.append(content[start:i + 1])
                    break
        else:
            # 括号未闭合（JSON 截断），也作为候选（后续 _repair_json 会补全）
            if depth > 0:
                candidates.append(content[start:])
    for candidate in candidates:
        data = None
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            repaired = _repair_json(candidate)
            if repaired:
                try:
                    data = json.loads(repaired)
                except (json.JSONDecodeError, TypeError):
                    continue
        if data is None or not isinstance(data, dict):
            continue
        name = data.get("name", "")
        args = data.get("arguments", {})
        if not name and "function" in data:
            fn = data["function"]
            if isinstance(fn, dict):
                name = fn.get("name", "")
                args = fn.get("arguments", {})
        canonical_name = _KNOWN_TOOL_ALIASES.get(name, name)
        if canonical_name in _KNOWN_TOOLS and isinstance(args, dict):
            return [{"id": str(uuid.uuid4())[:8], "name": str(canonical_name), "arguments": args}]
    return []


def _patch_messages_standalone(messages: list) -> list:
    """_patch_messages 的独立版本。"""
    patched = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "tool":
            content = msg.get("content", "")
            name = msg.get("name", "tool")
            patched.append({"role": "user", "content": (
                f"[工具 {name} 的返回结果]\n{content}\n\n"
                f"请基于以上工具结果继续分析。\n"
                f"- 如果还需要调用其他工具，请输出工具调用 JSON\n"
                f"- 如果所有分析已完成，请直接输出包含 stock_code/action/score/direction/confidence/reasons/risks/skill_reports 的 JSON 结论"
            )})
        elif role == "assistant" and msg.get("tool_calls"):
            content = msg.get("content", "")
            tool_descs = []
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                tc_name = fn.get("name", tc.get("name", "unknown"))
                tool_descs.append(f"调用工具: {tc_name}")
            tool_summary = "; ".join(tool_descs)
            non_json = _strip_tool_call_json_standalone(content)
            if not non_json or non_json.strip() in ("", "(调用工具中...)"):
                patched.append({"role": "assistant", "content": f"[已执行工具调用] {tool_summary}"})
            else:
                patched.append({"role": "assistant", "content": non_json.strip()})
        else:
            patched.append(msg)
    return patched


def _strip_tool_call_json_standalone(content: str) -> str:
    if not content:
        return content
    cleaned = content
    for m in re.finditer(r'```(?:json)?\s*\n?\{[^`]*"name"\s*:[^`]*\}\s*\n?```', cleaned, re.DOTALL):
        cleaned = cleaned.replace(m.group(0), "").strip()
    for m in re.finditer(r'\{', cleaned):
        depth = 0
        start = m.start()
        for i in range(start, len(cleaned)):
            if cleaned[i] == '{':
                depth += 1
            elif cleaned[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start:i + 1]
                    try:
                        data = json.loads(candidate)
                        if isinstance(data, dict) and data.get("name") in _KNOWN_TOOLS:
                            cleaned = cleaned.replace(candidate, "").strip()
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break
    # 关键修复：清理完后返回 cleaned（可能为空），不再 fallback 回原文
    # 空字符串表示"content 只有 tool_call JSON，没有其他文本"
    return cleaned.strip()


# ═══════════════════════════════════════════════════════════════
# 1. 纯逻辑测试（自包含，零外部依赖）
# ═══════════════════════════════════════════════════════════════

class TestJsonRepair:

    @test("尾部逗号修复")
    def test_trailing_comma(self):
        result = _repair_json('{"a": 1, "b": 2,}')
        assert result is not None
        assert_eq(json.loads(result)["a"], 1)

    @test("单引号修复")
    def test_single_quotes(self):
        result = _repair_json("{'name': 'call_skill'}")
        assert result is not None
        assert_eq(json.loads(result)["name"], "call_skill")

    @test("截断 JSON 补右括号")
    def test_truncated(self):
        result = _repair_json('{"name": "call_skill", "arguments": {"x": 1')
        assert result is not None
        assert_eq(json.loads(result)["name"], "call_skill")

    @test("正常 JSON 不修改")
    def test_valid(self):
        original = '{"name": "call_skill", "arguments": {"x": 1}}'
        result = _repair_json(original)
        assert result is not None
        assert_eq(json.loads(result), json.loads(original))

    @test("空字符串返回 None")
    def test_empty(self):
        assert _repair_json("") is None
        assert _repair_json("  ") is None


class TestToolCallParsing:

    @test("解析裸 JSON tool_call")
    def test_bare_json(self):
        content = '{"name": "call_skill", "arguments": {"skill_name": "technical_agent", "stock_code": "600593"}}'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)
        assert_eq(result[0]["name"], "call_skill")
        assert_eq(result[0]["arguments"]["stock_code"], "600593")

    @test("解析 ```json 代码块")
    def test_json_block(self):
        content = '分析一下。\n```json\n{"name": "call_skill", "arguments": {"skill_name": "indicator_agent", "stock_code": "600519"}}\n```'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)
        assert_eq(result[0]["arguments"]["skill_name"], "indicator_agent")

    @test("解析含尾部逗号的 JSON")
    def test_trailing_comma(self):
        content = '{"name": "call_skill", "arguments": {"skill_name": "technical_agent", "stock_code": "600593",}}'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)

    @test("解析单引号 JSON")
    def test_single_quotes(self):
        content = "{'name': 'call_skill', 'arguments': {'skill_name': 'technical_agent', 'stock_code': '600593'}}"
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)

    @test("解析截断 JSON（补右括号）")
    def test_truncated(self):
        content = '{"name": "call_skill", "arguments": {"skill_name": "technical_agent", "stock_code": "600593"'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)

    @test("工具名大小写模糊匹配: CallSkill → call_skill")
    def test_name_alias(self):
        content = '{"name": "CallSkill", "arguments": {"skill_name": "technical_agent", "stock_code": "600593"}}'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)
        assert_eq(result[0]["name"], "call_skill")

    @test("call_skill_tool 后缀匹配")
    def test_name_suffix(self):
        content = '{"name": "call_skill_tool", "arguments": {"skill_name": "technical_agent", "stock_code": "600593"}}'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)
        assert_eq(result[0]["name"], "call_skill")

    @test("忽略未知工具名")
    def test_unknown_tool(self):
        content = '{"name": "random_tool", "arguments": {"x": 1}}'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 0)

    @test("空 content 返回空列表")
    def test_empty(self):
        assert_eq(_parse_tool_calls_from_content(""), [])
        assert_eq(_parse_tool_calls_from_content(None), [])

    @test("纯文本无 JSON")
    def test_no_json(self):
        assert_eq(len(_parse_tool_calls_from_content("这是一段普通文本")), 0)

    @test("多个 JSON 只取匹配的")
    def test_multiple_json(self):
        content = '结果: {"score": 75}\n```json\n{"name": "call_skill", "arguments": {"skill_name": "technical_agent", "stock_code": "600593"}}\n```'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)
        assert_eq(result[0]["name"], "call_skill")

    @test("function 字段格式兼容")
    def test_function_field(self):
        content = '{"function": {"name": "call_skill", "arguments": {"skill_name": "technical_agent", "stock_code": "600593"}}}'
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)
        assert_eq(result[0]["name"], "call_skill")

    @test("本地模型实际输出格式")
    def test_real_model_output(self):
        # 模拟 qwen2.5-coder 实际输出
        content = """我来分析大连圣亚(600593)的技术面。

```json
{
  "name": "call_skill",
  "arguments": {
    "skill_name": "technical_agent",
    "stock_code": "600593",
    "stock_name": "大连圣亚"
  }
}
```"""
        result = _parse_tool_calls_from_content(content)
        assert_eq(len(result), 1)
        assert_eq(result[0]["name"], "call_skill")
        assert_eq(result[0]["arguments"]["stock_code"], "600593")


class TestPatchMessages:

    @test("tool 角色 → user 角色")
    def test_tool_to_user(self):
        patched = _patch_messages_standalone([{"role": "tool", "name": "call_skill", "content": '{"score": 75}'}])
        assert_eq(patched[0]["role"], "user")
        assert_contains(patched[0]["content"], "call_skill")

    @test("assistant tool_call JSON → 替换为描述（不跳过）")
    def test_preserve_context(self):
        messages = [{
            "role": "assistant",
            "content": '{"name": "call_skill", "arguments": {"skill_name": "technical_agent"}}',
            "tool_calls": [{"function": {"name": "call_skill", "arguments": '{}'}}],
        }]
        patched = _patch_messages_standalone(messages)
        assert_eq(len(patched), 1, "不应跳过，应保留上下文")
        assert_contains(patched[0]["content"], "已执行工具调用")

    @test("assistant tool_call + 其他文本 → 保留文本")
    def test_keep_text(self):
        messages = [{
            "role": "assistant",
            "content": '我来分析。\n```json\n{"name": "call_skill", "arguments": {}}\n```',
            "tool_calls": [{"function": {"name": "call_skill", "arguments": '{}'}}],
        }]
        patched = _patch_messages_standalone(messages)
        assert_eq(len(patched), 1)
        assert_contains(patched[0]["content"], "我来分析")

    @test("多步 tool_call 链路不断裂")
    def test_multi_step_chain(self):
        """模拟 technical_agent → indicator_agent → intelligence_agent 三步调用。"""
        messages = [
            {"role": "user", "content": "分析大连圣亚股票"},
            # 第1步: call_skill(technical_agent)
            {"role": "assistant", "content": '{"name": "call_skill", "arguments": {"skill_name": "technical_agent"}}',
             "tool_calls": [{"function": {"name": "call_skill", "arguments": '{"skill_name": "technical_agent"}'}}]},
            {"role": "tool", "name": "call_skill", "content": '{"score": 75, "direction": "bullish"}'},
            # 第2步: call_skill(indicator_agent)
            {"role": "assistant", "content": '{"name": "call_skill", "arguments": {"skill_name": "indicator_agent"}}',
             "tool_calls": [{"function": {"name": "call_skill", "arguments": '{"skill_name": "indicator_agent"}'}}]},
            {"role": "tool", "name": "call_skill", "content": '{"score": 70, "direction": "bullish"}'},
        ]
        patched = _patch_messages_standalone(messages)
        # 验证: 5条消息都应保留（不跳过任何 assistant 消息）
        assert_eq(len(patched), 5, f"期望5条，实际{len(patched)}条")
        # 验证: assistant 消息包含上下文描述
        assert_contains(patched[1]["content"], "已执行工具调用")
        assert_contains(patched[3]["content"], "已执行工具调用")
        # 验证: tool 消息转为 user
        assert_eq(patched[2]["role"], "user")
        assert_eq(patched[4]["role"], "user")


class TestStockExtraction:

    @test("纯中文不匹配数字")
    def test_chinese_only(self):
        m = re.search(r'(?<!\d)(\d{6})(?!\d)', "分析大连圣亚股票")
        assert m is None

    @test("6位数字提取")
    def test_digit_code(self):
        m = re.search(r'(?<!\d)(\d{6})(?!\d)', "帮我看看600593")
        assert m is not None
        assert_eq(m.group(1), "600593")

    @test("混合消息")
    def test_mixed(self):
        m = re.search(r'(?<!\d)(\d{6})(?!\d)', "大连圣亚600593怎么样")
        assert m is not None
        assert_eq(m.group(1), "600593")

    @test("干扰数字不匹配")
    def test_no_false_positive(self):
        m = re.search(r'(?<!\d)(\d{6})(?!\d)', "2026年6月13日")
        assert m is None


class TestMessageEnrichment:

    @test("增强消息包含 call_skill 指令和 3 个 skill")
    def test_enrichment(self):
        code, name = "600593", "大连圣亚"
        enriched = (
            f"股票代码: {code}\n股票名称: {name}\n\n"
            f"[系统提示] 股票代码已确认: {code}（{name}）\n"
            f'直接使用 call_skill 执行分析：\n'
            f'1. call_skill(skill_name="technical_agent", stock_code="{code}")\n'
            f'2. call_skill(skill_name="indicator_agent", stock_code="{code}")\n'
            f'3. call_skill(skill_name="intelligence_agent", stock_code="{code}")\n'
            f"分析大连圣亚股票"
        )
        assert_contains(enriched, "call_skill")
        assert_contains(enriched, "technical_agent")
        assert_contains(enriched, "indicator_agent")
        assert_contains(enriched, "intelligence_agent")


# ═══════════════════════════════════════════════════════════════
# 2. 集成测试（需 Flask 等依赖，通过 --all 或 --e2e 触发）
# ═══════════════════════════════════════════════════════════════

class TestSkillRegistry:

    @test("technical_agent 已注册")
    def test_technical(self):
        from app.agent.skills.registry import skill_registry
        skill_registry.discover()
        sk = skill_registry.get("technical_agent")
        assert sk is not None
        assert_eq(sk.name, "technical_agent")

    @test("technical_agent 工具列表正确")
    def test_technical_tools(self):
        from app.agent.skills.registry import skill_registry
        skill_registry.discover()
        sk = skill_registry.get("technical_agent")
        assert_in("analyze_trend", sk.tools)
        assert_in("get_indicator_snapshot", sk.tools)

    @test("所有核心 skill 已注册")
    def test_all_skills(self):
        from app.agent.skills.registry import skill_registry
        skill_registry.discover()
        for name in ["technical_agent", "indicator_agent", "intelligence_agent",
                      "market_data_agent", "screening_agent", "backtest_agent"]:
            assert skill_registry.get(name) is not None, f"{name} 未注册"

    @test("algo_analyze 返回 SkillReport")
    def test_algo_analyze(self):
        from app.agent.skills.registry import skill_registry
        skill_registry.discover()
        sk = skill_registry.get("technical_agent")
        mock_results = {
            "analyze_trend": {"trend_score": 75, "trend": "上升趋势", "ma_alignment": "MA5>MA10>MA20", "bias_ma20": 3.5},
            "get_indicator_snapshot": {"rsi6": 62, "macd_hist": 0.8, "kdj_j": 75},
            "get_volume_analysis": {"vol_price_relation": "量价齐升", "volume_ratio": 2.1},
            "analyze_pattern": {"patterns": ["突破前高"]},
            "get_chip_distribution": {"profit_ratio": 55},
        }
        report = sk.algo_analyze("600593", "大连圣亚", mock_results)
        assert report is not None
        assert_true(0 <= report.score <= 100)
        assert_in(report.direction, ["bullish", "bearish", "neutral"])
        print(f"    → score={report.score}, direction={report.direction}, signal={report.signal}")


class TestToolRegistry:

    @test("analyze_trend 已注册")
    def test_analyze_trend(self):
        from app.agent.tools.registry import registry
        registry.discover()
        assert "analyze_trend" in registry._tools

    @test("工具数量 >= 60")
    def test_tool_count(self):
        from app.agent.tools.registry import registry
        registry.discover()
        count = len(registry._tools)
        assert_true(count >= 60, f"只有 {count} 个")
        print(f"    → 共 {count} 个工具")


class TestConfigGen:

    @test("LLM_PROVIDER → AGENT_LLM_PROVIDER 映射")
    def test_legacy_provider(self):
        from app.agent.nanobot_config_gen import _load_dotenv_values
        import tempfile
        # 写临时 .env 文件测试（_load_dotenv_values 读文件不读 os.environ）
        env_content = "LLM_PROVIDER=openai\nOPENAI_MODEL=qwen2.5-coder-14b-instruct\nOPENAI_API_KEY=test-key\nOPENAI_BASE_URL=http://localhost:8080/v1\n"
        env_path = BACKEND_DIR / ".env"
        backup = None
        if env_path.exists():
            backup = env_path.read_text(encoding="utf-8")
        try:
            env_path.write_text(env_content, encoding="utf-8")
            values = _load_dotenv_values()
            assert_eq(values.get("AGENT_LLM_PROVIDER"), "openai")
            assert_eq(values.get("AGENT_LLM_MODEL"), "qwen2.5-coder-14b-instruct")
            assert_eq(values.get("OPENAI_API_BASE"), "http://localhost:8080/v1")
        finally:
            if backup is not None:
                env_path.write_text(backup, encoding="utf-8")
            elif env_path.exists():
                env_path.unlink()

    @test("OPENAI_BASE_URL → OPENAI_API_BASE")
    def test_base_url(self):
        from app.agent.nanobot_config_gen import _load_dotenv_values
        env_path = BACKEND_DIR / ".env"
        backup = None
        if env_path.exists():
            backup = env_path.read_text(encoding="utf-8")
        try:
            env_path.write_text("OPENAI_BASE_URL=http://localhost:8080/v1\nOPENAI_API_KEY=test\n", encoding="utf-8")
            values = _load_dotenv_values()
            assert_eq(values.get("OPENAI_API_BASE"), "http://localhost:8080/v1")
        finally:
            if backup is not None:
                env_path.write_text(backup, encoding="utf-8")
            elif env_path.exists():
                env_path.unlink()


# ═══════════════════════════════════════════════════════════════
# 3. 端到端测试
# ═══════════════════════════════════════════════════════════════

class TestE2E:

    @test("NanobotAgent 初始化")
    def test_init(self):
        from app.agent.nanobot_agent import NanobotAgent
        agent = NanobotAgent(force_config=True)
        assert_true(agent._initialized)
        assert_true(agent._loop.is_running())

    @test("call_skill 工具已注册")
    def test_call_skill(self):
        from app.agent.nanobot_agent import get_nanobot_agent
        agent = get_nanobot_agent()
        assert_true(agent._agent_loop.tools.has("call_skill"))

    @test("完整分析链路")
    def test_full_analysis(self, stock_code="600593", stock_name="大连圣亚"):
        from app.agent.nanobot_agent import get_nanobot_agent
        agent = get_nanobot_agent()
        result = agent.chat(
            message=f"分析{stock_name}股票",
            session_id=f"test_{stock_code}_{int(time.time())}",
            context={"stock_code": stock_code, "stock_name": stock_name},
        )
        print(f"\n    success={result.success}")
        print(f"    model={result.model}")
        print(f"    tools={result.tool_calls_log}")
        print(f"    content长度={len(result.content) if result.content else 0}")
        if result.content:
            print(f"    content前500字:\n    {result.content[:500]}")
        if result.error:
            print(f"    error={result.error}")
        assert_true(result.success, f"失败: {result.error}")
        assert_true(len(result.content) > 50, "内容过短")


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

LOGIC_TESTS = [TestJsonRepair, TestToolCallParsing, TestPatchMessages, TestStockExtraction, TestMessageEnrichment]
INTEGRATION_TESTS = [TestSkillRegistry, TestToolRegistry, TestConfigGen]
E2E_TESTS = [TestE2E]


def run_class_tests(cls, instance):
    print(f"\n  ── {cls.__name__} ──")
    for attr_name in dir(instance):
        attr = getattr(instance, attr_name)
        if callable(attr) and hasattr(attr, "_test_name"):
            run_test(attr._test_name, attr)


def check_import(module_path: str, class_name: str) -> bool:
    try:
        mod = __import__(module_path, fromlist=[class_name])
        return hasattr(mod, class_name)
    except ImportError:
        return False


def print_header():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     QuantDinger Agent 完整链路测试                       ║")
    print("╚══════════════════════════════════════════════════════════╝")


def print_summary():
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed
    print(f"\n{'='*60}")
    print(f"测试结果: {passed}/{total} 通过", end="")
    if _skipped:
        print(f"，{len(_skipped)} 个跳过", end="")
    if failed:
        print(f"，{failed} 个失败 ❌")
        print(f"{'─'*60}")
        for name, ok, err in _results:
            if not ok:
                print(f"  ❌ {name}")
                if err:
                    for line in err.split("\n")[:3]:
                        print(f"     {line}")
    else:
        print(" ✅ 全部通过")
    print(f"{'='*60}")


def main():
    print_header()

    run_all = "--all" in sys.argv
    run_e2e = "--e2e" in sys.argv
    stock_code = "600593"
    stock_name = "大连圣亚"

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--stock" and i + 1 < len(args):
            stock_code = args[i + 1]
        if arg == "--name" and i + 1 < len(args):
            stock_name = args[i + 1]

    # ── 纯逻辑测试（总是运行）──
    print("\n📋 纯逻辑测试 (自包含，零依赖):")
    for cls in LOGIC_TESTS:
        run_class_tests(cls, cls())

    # ── 集成测试 ──
    if run_all or run_e2e:
        print("\n🔗 集成测试:")
        has_flask = check_import("flask", "Flask")
        if not has_flask:
            skip_test("全部集成测试", "Flask 未安装")
        else:
            for cls in INTEGRATION_TESTS:
                run_class_tests(cls, cls())

    # ── 端到端测试 ──
    if run_e2e:
        print(f"\n🚀 端到端测试 (stock={stock_code} {stock_name}):")
        e2e = TestE2E()
        for attr_name in dir(e2e):
            attr = getattr(e2e, attr_name)
            if callable(attr) and hasattr(attr, "_test_name"):
                if "full_analysis" in attr_name:
                    run_test(f"完整分析: {stock_name}({stock_code})", lambda: attr(stock_code, stock_name))
                else:
                    run_test(attr._test_name, attr)

    print_summary()
    return 0 if all(ok for _, ok, _ in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
