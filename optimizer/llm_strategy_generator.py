"""
LLM 策略模板生成器
使用 LLM 基于现有模板模式，自动生成新的 IndicatorStrategy 策略模板。

用法:
    # 1. 生成新策略模板并保存
    python -m app.optimizer.llm_strategy_generator generate \
        --prompt "生成一个适用于A股的布林带+MACD+成交量三重确认策略" \
        --output new_strategy.py

    # 2. 从 prompt 文件批量生成
    python -m app.optimizer.llm_strategy_generator batch \
        --prompts-file prompts.txt \
        --output strategies_batch.py

    # 3. 合并所有扩展模板到 param_space.py
    python -m app.optimizer.llm_strategy_generator merge

依赖:
    - 无需额外依赖，LLM 调用通过 OpenAI 兼容 API 或本地模型
    - 环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL（可选）
"""
import argparse
import json
import os
import re
import sys
import textwrap
from typing import Dict, Any, List, Optional

# ============================================================
# LLM Prompt 模板 —— 核心：教会 LLM 生成策略模板
# ============================================================

# ── 完整的参考示例模板代码 ──
_EXAMPLE_TEMPLATE_CODE = '''
# === 参考示例：triple_rsi_momentum（三周期RSI动量）===
# 严格按此格式生成，不要遗漏任何部分！

from typing import Dict, Any

def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}

def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}

def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}


def _build_triple_rsi_momentum_config(p: dict) -> dict:
    """三周期RSI动量策略 —— build_config 函数示例"""
    entry_rules = [
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_fast"], "threshold": p["rsi_entry"]},
            "operator": "cross_up",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_mid"], "threshold": p["rsi_trend_mid"]},
            "operator": ">",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_slow"], "threshold": p["rsi_trend_slow"]},
            "operator": ">",
        },
    ]

    return {
        "name": f"TripleRSI_{p['rsi_fast']}_{p['rsi_mid']}_{p['rsi_slow']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.5)},
            "trailing_stop": {"enabled": False},
        },
    }


STRATEGY_TEMPLATE_KEY = "triple_rsi_momentum"

STRATEGY_TEMPLATE_DICT = {
    "name": "三周期RSI动量",
    "description": "短中长三周期 RSI 共振确认入场，三重过滤提高信号质量。",
    "indicators": ["rsi"],
    "params": {
        "rsi_fast":       _p_int(5, 10, 1),
        "rsi_mid":        _p_int(11, 18, 1),
        "rsi_slow":       _p_int(19, 30, 1),
        "rsi_entry":      _p_int(28, 42, 1),
        "rsi_trend_mid":  _p_int(42, 53, 1),
        "rsi_trend_slow": _p_int(42, 53, 1),
        "stop_loss_pct":  _p_float(2.5, 4.5, 0.5),
    },
    "constraints": [
        ("rsi_fast", "<", "rsi_mid"),
        ("rsi_mid", "<", "rsi_slow"),
    ],
    "build_config": _build_triple_rsi_momentum_config,
}
'''


SYSTEM_PROMPT = """你是一个量化策略工程师，专门生成 A 股市场的 IndicatorStrategy 策略模板。

你必须严格遵循以下代码格式和规范：

## 参数定义函数
```python
def _p_int(low, high, step=1): return {"type": "int", "low": low, "high": high, "step": step}
def _p_float(low, high, step=0.001): return {"type": "float", "low": low, "high": high, "step": step}
def _p_choice(choices): return {"type": "choice", "choices": choices}
```

## build_config 函数格式
每个策略必须有一个 `_build_xxx_config(p: dict) -> dict` 函数，返回：
```python
{
    "name": "策略名",
    "entry_rules": [
        {"indicator": "指标名", "params": {...}, "operator": "条件"},
    ],
    "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
    "pyramiding_rules": {"enabled": False},
    "risk_management": {
        "stop_loss": {"enabled": True/False, ...},
        "trailing_stop": {"enabled": True/False, ...},
    },
}
```

## 支持的指标
- ma (sma/ema), ema, rsi, macd, kdj, bollinger, atr, supertrend, volume, donchian_channel, vwap

## 支持的 operator
- cross_up, cross_down, price_above, price_below, above, below, gold_cross, death_cross
- price_above_upper, price_below_lower, volume_above_ma, volume_ratio_above
- histogram_positive, histogram_negative, bullish_divergence, below_percentile, price_break_upper

## A 股特点
- T+1 制度，日内策略需要隔日出场
- 涨跌停限制（主板 10%，创业板/科创板 20%）
- 换手率是 A 股重要指标
- 量价关系在 A 股比美股更重要
- 均线系统（5/10/20/60/120/250 日）是 A 股技术分析基础

## 输出要求
1. 只输出 Python 代码，不要解释
2. 代码中包含 `_build_xxx_config` 函数和模板字典注册
3. 参数空间要合理，不要太窄也不要太宽
4. 策略名称用英文下划线命名
5. 必须包含合理的约束条件（如 fast_period < slow_period）

## 完整参考示例
以下是 triple_rsi_momentum 模板的完整代码，你必须严格按此结构生成：

""" + _EXAMPLE_TEMPLATE_CODE


def build_generation_prompt(strategy_description: str, existing_templates: List[str] = None) -> str:
    """构建生成 prompt"""
    existing = ""
    if existing_templates:
        existing = f"\n\n已有策略模板（不要重复生成这些）：\n" + "\n".join(f"- {t}" for t in existing_templates)

    return f"""请根据以下描述生成一个 A 股策略模板：

{strategy_description}{existing}

## 严格要求

1. **必须**生成完整的 `_build_xxx_config(p: dict) -> dict` 函数，函数名中的 xxx 与策略 key 一致
2. **必须**生成 `STRATEGY_TEMPLATE_KEY` 变量（字符串）和 `STRATEGY_TEMPLATE_DICT` 变量（字典）
3. `STRATEGY_TEMPLATE_DICT` **必须**包含以下 key：name, description, indicators, params, constraints, build_config
4. `params` 中**必须**使用 `_p_int`, `_p_float`, `_p_choice` 定义参数范围
5. `build_config` 的值**必须**是 `_build_xxx_config` 函数的引用（不是字符串）
6. `_build_xxx_config` 返回的 dict **必须**包含：name, entry_rules, position_config, pyramiding_rules, risk_management
7. `risk_management` **必须**包含 `stop_loss`（enabled: True, value: 止损百分比）
8. `entry_rules` 中每个 rule **必须**包含：indicator, params, operator
9. 参数空间不要太大（组合数 < 1000万），也不要太窄

## 参考代码结构（严格模仿此格式）

```python
{_EXAMPLE_TEMPLATE_CODE.strip()}
```

请直接输出 Python 代码，不要包含任何解释文字。
"""


# ============================================================
# 代码解析与验证
# ============================================================

def _sanitize_python_code(code: str) -> str:
    """
    修复 LLM 生成代码中的常见语法问题：
    1. JSON 风格的 true/false/null → Python 的 True/False/None
    2. 函数名与 STRATEGY_TEMPLATE_DICT 中 build_config 引用不匹配
    """
    # ── 修复 1: true/false/null → True/False/None ──
    code = re.sub(r'(?<![a-zA-Z0-9_])true(?![a-zA-Z0-9_])', 'True', code)
    code = re.sub(r'(?<![a-zA-Z0-9_])false(?![a-zA-Z0-9_])', 'False', code)
    code = re.sub(r'(?<![a-zA-Z0-9_])null(?![a-zA-Z0-9_])', 'None', code)

    # ── 修复 2: 函数名不匹配 ──
    defined_fns = re.findall(r'def (_build_\w+_config)\s*\(', code)
    ref_match = re.search(r'"build_config"\s*:\s*(_build_\w+_config)', code)
    if defined_fns and ref_match:
        ref_fn = ref_match.group(1)
        if ref_fn not in defined_fns:
            actual_fn = defined_fns[0]
            code = code.replace(ref_fn, actual_fn)

    # ── 修复 3: STRATEGY_TEMPLATE_KEY 缺失 ──
    if 'STRATEGY_TEMPLATE_KEY' not in code and defined_fns:
        key_match = re.search(r'def _build_(\w+)_config', code)
        if key_match:
            key = key_match.group(1)
            code += f'\n\nSTRATEGY_TEMPLATE_KEY = "{key}"\n'

    # ── 修复 4: STRATEGY_TEMPLATE_DICT 缺失 ──
    if 'STRATEGY_TEMPLATE_DICT' not in code and defined_fns:
        key_match = re.search(r'def _build_(\w+)_config', code)
        if key_match:
            key = key_match.group(1)
            fn_name = defined_fns[0]
            code += f'''
STRATEGY_TEMPLATE_DICT = {{
    "name": "{key}",
    "description": "LLM 生成的策略",
    "indicators": [],
    "params": {{}},
    "constraints": [],
    "build_config": {fn_name},
}}
'''

    # ── 修复 5: 尝试修复常见语法错误 ──
    code = _try_fix_syntax_errors(code)

    return code


def _try_fix_syntax_errors(code: str) -> str:
    """尝试修复 LLM 代码中的常见语法错误"""
    # 先检查是否真的有语法错误
    try:
        compile(code, '<check>', 'exec')
        return code  # 没有语法错误，直接返回
    except SyntaxError:
        pass

    lines = code.split('\n')
    fixed_lines = []

    for line in lines:
        # 修复未闭合的字符串引号
        # 计数单引号和双引号（忽略转义的）
        single_count = line.count("'") - line.count("\\'")
        double_count = line.count('"') - line.count('\\"')

        # 如果引号数为奇数，在行末补上
        if single_count % 2 != 0:
            line = line + "'"
        if double_count % 2 != 0:
            line = line + '"'

        fixed_lines.append(line)

    return '\n'.join(fixed_lines)


def extract_python_code(raw_text: str) -> str:
    """从 LLM 输出中提取 Python 代码，并做基础清洗"""
    # ── 第 0 步：检测 JSON 包装的代码字符串 ──
    raw_text = _unwrap_json_code(raw_text)

    # 尝试从 markdown 代码块中提取
    code_blocks = re.findall(r'```(?:python)?\s*\n(.*?)```', raw_text, re.DOTALL)
    if code_blocks:
        code = '\n'.join(code_blocks)
    else:
        # 尝试找 def _build_ 开头的代码
        lines = raw_text.split('\n')
        code_lines = []
        in_code = False
        for line in lines:
            if line.strip().startswith('def _build_') or line.strip().startswith('STRATEGY_TEMPLATE'):
                in_code = True
            if in_code:
                code_lines.append(line)
        code = '\n'.join(code_lines) if code_lines else raw_text

    # 基础清洗
    code = _sanitize_python_code(code)
    return code


def _unescape_literal_newlines(text: str) -> str:
    """
    将字面量 \\n \\t 转换为真正的换行和制表符。
    处理所有 LLM 输出路径上的通用问题。
    """
    # 只在文本中确实有字面 \n 且没有被正确处理时才替换
    # 排除已经是真正换行的情况（避免双重替换）
    if '\\n' in text:
        text = text.replace('\\n', '\n')
    if '\\t' in text:
        text = text.replace('\\t', '\t')
    return text


def _unwrap_json_code(text: str) -> str:
    """
    检测并解包 LLM 输出中 JSON 包装的代码。
    处理三种情况：
    1. 标准 JSON: {"code": "..."}
    2. 混合格式: { "from typing import...\ndef _build..." } (代码直接当 JSON 值但没转义)
    3. 普通代码带字面 \\n
    """
    text = text.strip()

    # 情况 1: 标准 JSON 对象
    if text.startswith('{') and text.endswith('}'):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for key in ['code', 'python_code', 'content', 'template', 'strategy']:
                    if key in obj and isinstance(obj[key], str):
                        return _unescape_literal_newlines(obj[key])
                for v in obj.values():
                    if isinstance(v, str) and len(v) > 100:
                        return _unescape_literal_newlines(v)
        except (json.JSONDecodeError, ValueError):
            pass

    # 情况 2: 混合格式 —— LLM 试图输出 JSON 但代码没转义
    # 特征: 以 { 开头，包含 def _build_ 或 import 等 Python 关键字
    # 策略: 提取第一个 { 之后到最后一个 } 之前的内容，去掉首尾的 JSON 括号
    if text.startswith('{'):
        # 找到最后一个 } 的位置（可能是 JSON 闭合括号）
        last_brace = text.rfind('}')
        if last_brace > 0:
            # 提取 { 和 } 之间的内容
            inner = text[1:last_brace].strip()
            # 如果内容包含 Python 关键字，当作代码处理
            if any(kw in inner for kw in ['def _build_', 'import ', '_p_int', 'STRATEGY_TEMPLATE']):
                # 去掉可能的 JSON key 前缀（如 "code": 或 "from typing..."）
                # 如果第一行看起来像 JSON value 的开始，去掉引号
                lines = inner.split('\n')
                first = lines[0].strip().strip('"').strip("'")
                # 如果第一行以 "from 或 "def 开头，去掉开头的引号
                if first.startswith('"') or first.startswith("'"):
                    lines[0] = first
                return _unescape_literal_newlines('\n'.join(lines))

    # 情况 3: 通用处理 —— 字面 \n 转真换行
    if '\\n' in text:
        text = _unescape_literal_newlines(text)

    return text


def _build_exec_namespace() -> dict:
    """构建 exec 执行用的 namespace，包含常用别名"""
    return {
        "_p_int": lambda low, high, step=1: {"type": "int", "low": low, "high": high, "step": step},
        "_p_float": lambda low, high, step=0.001: {"type": "float", "low": low, "high": high, "step": step},
        "_p_choice": lambda choices: {"type": "choice", "choices": choices},
        "Dict": Dict,
        "Any": Any,
        # JSON/JS 风格别名，防止 LLM 混淆
        "true": True,
        "false": False,
        "null": None,
    }


def validate_strategy_code(code: str) -> tuple[bool, str]:
    """
    验证生成的策略代码是否符合规范。
    改进版：不仅做字符串检查，还 exec 后检查运行时结构。
    """
    # ── 第零层：基础清洗 ──
    code = _sanitize_python_code(code)

    # ── 第一层：语法检查 ──
    try:
        compile(code, '<strategy>', 'exec')
    except SyntaxError as e:
        # 语法错误时，尝试从内容重建
        rebuilt = _rebuild_from_content(code)
        if rebuilt != code:
            try:
                compile(rebuilt, '<strategy>', 'exec')
                code = rebuilt  # 重建成功
            except SyntaxError:
                return False, f"语法错误: {e}"
        else:
            return False, f"语法错误: {e}"

    # ── 第二层：exec 执行并提取模板 ──
    namespace = _build_exec_namespace()

    try:
        exec(code, namespace)
    except Exception as e:
        # 执行失败时，尝试从内容重建
        rebuilt = _rebuild_from_content(code)
        if rebuilt != code:
            namespace = _build_exec_namespace()
            try:
                exec(rebuilt, namespace)
                code = rebuilt
            except Exception as e2:
                return False, f"执行代码失败: {e2}"
        else:
            return False, f"执行代码失败: {e}"

    # ── 第三层：检查 STRATEGY_TEMPLATE_KEY 和 STRATEGY_TEMPLATE_DICT ──
    key = namespace.get("STRATEGY_TEMPLATE_KEY")
    tpl = namespace.get("STRATEGY_TEMPLATE_DICT")

    errors = []

    if not key or not isinstance(key, str):
        errors.append("缺少 STRATEGY_TEMPLATE_KEY（字符串变量）")

    if not tpl or not isinstance(tpl, dict):
        errors.append("缺少 STRATEGY_TEMPLATE_DICT（字典变量）")
        return False, "验证失败:\n" + "\n".join(f"  - {e}" for e in errors)

    # 检查模板字典的必要 key
    required_dict_keys = ["name", "description", "indicators", "params", "build_config"]
    for rk in required_dict_keys:
        if rk not in tpl:
            errors.append(f"STRATEGY_TEMPLATE_DICT 缺少 '{rk}'")

    # 检查 build_config 是否可调用
    build_fn = tpl.get("build_config")
    if not callable(build_fn):
        errors.append("build_config 不是可调用函数（应为 _build_xxx_config 的引用，不要加引号）")

    # 检查 params 是否使用了 _p_int/_p_float/_p_choice
    params = tpl.get("params", {})
    if not params:
        errors.append("params 为空，必须定义参数空间")
    else:
        for pname, pspec in params.items():
            if not isinstance(pspec, dict) or "type" not in pspec:
                errors.append(f"参数 '{pname}' 未使用 _p_int/_p_float/_p_choice 定义（值: {pspec}）")

    # ── 第四层：测试 build_config 返回值结构 ──
    if callable(build_fn) and params:
        # 构造测试参数（取每个参数的 low 值或 choices 第一个）
        test_params = {}
        for pname, pspec in params.items():
            if isinstance(pspec, dict):
                if pspec.get("type") == "choice":
                    choices = pspec.get("choices", [])
                    test_params[pname] = choices[0] if choices else None
                else:
                    test_params[pname] = pspec.get("low", 0)

        try:
            result = build_fn(test_params)
        except Exception as e:
            errors.append(f"build_config 执行报错: {e}")
            result = None

        if result and isinstance(result, dict):
            # 检查返回值必要 key
            required_return_keys = ["entry_rules", "position_config", "risk_management"]
            for rk in required_return_keys:
                if rk not in result:
                    errors.append(f"build_config 返回值缺少 '{rk}'")

            # 检查 risk_management 中有 stop_loss
            rm = result.get("risk_management", {})
            if "stop_loss" not in rm:
                errors.append("risk_management 缺少 'stop_loss' 配置")

            # 检查 entry_rules 结构
            entry_rules = result.get("entry_rules", [])
            if not entry_rules:
                errors.append("entry_rules 为空，至少需要一个入场条件")
            else:
                for i, rule in enumerate(entry_rules):
                    if not isinstance(rule, dict):
                        errors.append(f"entry_rules[{i}] 不是字典")
                        continue
                    for rk in ["indicator", "params", "operator"]:
                        if rk not in rule:
                            errors.append(f"entry_rules[{i}] 缺少 '{rk}'")

    if errors:
        return False, "验证失败:\n" + "\n".join(f"  - {e}" for e in errors)

    return True, "验证通过"


def execute_and_extract(code: str) -> Optional[Dict[str, Any]]:
    """执行代码并提取策略模板"""
    original_code = code  # 保存原始代码供 format_template_code 使用
    try:
        code = _sanitize_python_code(code)
        namespace = _build_exec_namespace()
        exec(code, namespace)
    except (SyntaxError, Exception) as e:
        # 尝试从内容重建
        rebuilt = _rebuild_from_content(code)
        if rebuilt != code:
            try:
                namespace = _build_exec_namespace()
                exec(rebuilt, namespace)
                original_code = rebuilt
            except Exception as e2:
                print(f"  执行代码失败: {e2}")
                return None
        else:
            print(f"  执行代码失败: {e}")
            return None

    try:
        key = namespace.get("STRATEGY_TEMPLATE_KEY")
        template = namespace.get("STRATEGY_TEMPLATE_DICT")

        if key and template:
            # 保存原始代码供后续 format 使用
            template["_source_code"] = original_code
            return {key: template}

        # 尝试找所有 STRATEGY_TEMPLATE_* 变量
        templates = {}
        for k, v in namespace.items():
            if k.startswith("STRATEGY_TEMPLATE") and isinstance(v, dict) and "build_config" in v:
                template_key = k.replace("STRATEGY_TEMPLATE_", "").lower()
                v["_source_code"] = original_code
                templates[template_key] = v

        return templates if templates else None
    except Exception as e:
        print(f"  提取模板失败: {e}")
        return None


# ============================================================
# LLM 调用
# ============================================================

def call_llm(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """调用 LLM API 生成策略代码"""
    import urllib.request
    import urllib.error

    api_base = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "gpt-4o")

    if not api_key:
        print("  ⚠️  未设置 LLM_API_KEY，使用内置模板生成模式")
        return ""

    url = f"{api_base.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  LLM 调用失败: {e}")
        return ""


# ============================================================
# 修复 LLM 生成的代码（自动补全缺失结构）
# ============================================================

def try_fix_strategy_code(code: str, error_msg: str) -> str:
    """
    尝试自动修复 LLM 生成的策略代码。
    多层降级：基础清洗 → 补全缺失结构 → 从内容推断重建
    """
    # 先做基础清洗（true/false/null + 函数名匹配）
    code = _sanitize_python_code(code)

    # ── 如果连 _build_ 函数都没有，尝试从内容重建 ──
    if 'def _build_' not in code:
        code = _rebuild_from_content(code)

    lines = code.strip().split('\n')

    # ── 如果缺少 _p_int/_p_float/_p_choice 定义，在开头补上 ──
    if '_p_int' not in code:
        helper_defs = '''from typing import Dict, Any

def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}

def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}

def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}

'''
        lines = helper_defs.split('\n') + lines

    code = '\n'.join(lines)

    # 再跑一遍 sanitize（处理新增内容）
    code = _sanitize_python_code(code)

    return code


def _rebuild_from_content(code: str) -> str:
    """
    当 LLM 生成的代码完全没有 _build_xxx_config 函数时，
    尝试从代码内容中提取指标、参数等信息，重建一个合规的模板。
    """
    # 提取代码中出现的指标名
    known_indicators = ['rsi', 'macd', 'kdj', 'bollinger', 'ema', 'sma', 'ma',
                        'atr', 'volume', 'vwap', 'supertrend', 'obv', 'adx', 'cci', 'mfi',
                        'donchian_channel', 'price_volume_divergence', 'bollinger_bandwidth',
                        'limitup_detect']
    found_indicators = []
    code_lower = code.lower()
    for ind in known_indicators:
        # 检查是否在代码中作为 indicator 名出现
        if f'"{ind}"' in code_lower or f"'{ind}'" in code_lower or f'indicator.*{ind}' in code_lower:
            found_indicators.append(ind)
    if not found_indicators:
        found_indicators = ['rsi', 'volume']  # 兜底

    # 尝试提取策略名
    name_match = re.search(r'["\']name["\']\s*:\s*["\']([^"\']+)["\']', code)
    strategy_name = name_match.group(1) if name_match else "llm_strategy"
    # 生成合法的 Python 标识符
    key = re.sub(r'[^a-zA-Z0-9_]', '_', strategy_name).lower().strip('_')
    if not key or key[0].isdigit():
        # 用指标名组合生成 key
        ind_key = '_'.join(found_indicators[:3]) if found_indicators else 'llm_gen'
        key = f"{ind_key}_strategy"

    # 尝试提取参数
    param_pattern = re.compile(r'["\'](\w+)["\']\s*:\s*_p_(int|float|choice)\s*\(([^)]+)\)')
    found_params = param_pattern.findall(code)

    # 如果没找到参数定义，用默认的
    if not found_params:
        param_lines = [
            f'        "rsi_period": _p_int(10, 20, 1),',
            f'        "rsi_threshold": _p_int(25, 40, 1),',
            f'        "vol_ma_period": _p_int(10, 30, 1),',
            f'        "vol_ratio": _p_float(1.2, 2.5, 0.1),',
            f'        "stop_loss_pct": _p_float(2.0, 5.0, 0.5),',
        ]
    else:
        param_lines = []
        for pname, ptype, pargs in found_params:
            param_lines.append(f'        "{pname}": _p_{ptype}({pargs}),')
        # 确保有止损参数
        if not any(p[0] == 'stop_loss_pct' for p in found_params):
            param_lines.append(f'        "stop_loss_pct": _p_float(2.0, 5.0, 0.5),')

    params_str = '\n'.join(param_lines)

    # 构建 entry_rules
    entry_rules_lines = []
    for ind in found_indicators[:3]:  # 最多取 3 个指标
        if ind == 'rsi':
            entry_rules_lines.append(
                '        {"indicator": "rsi", "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_threshold", 30)}, "operator": "<"},')
        elif ind == 'volume':
            entry_rules_lines.append(
                '        {"indicator": "volume", "params": {"period": p.get("vol_ma_period", 20)}, "operator": "volume_ratio_above", "threshold": p.get("vol_ratio", 1.5)},')
        elif ind == 'macd':
            entry_rules_lines.append(
                '        {"indicator": "macd", "params": {"fast_period": 12, "slow_period": 26, "signal_period": 9}, "operator": "diff_lt_dea"},')
        elif ind == 'bollinger':
            entry_rules_lines.append(
                '        {"indicator": "bollinger", "params": {"period": 20, "std_dev": 2.0}, "operator": "price_below_lower"},')
        elif ind in ('ma', 'ema', 'sma'):
            entry_rules_lines.append(
                f'        {{"indicator": "ma", "params": {{"period": 20, "ma_type": "ema"}}, "operator": "price_above"}},')
        elif ind == 'vwap':
            entry_rules_lines.append(
                '        {"indicator": "vwap", "params": {"deviation_pct": 2.0}, "operator": "price_below_vwap_by"},')
        elif ind == 'kdj':
            entry_rules_lines.append(
                '        {"indicator": "kdj", "params": {"k_period": 9, "d_period": 3}, "operator": "gold_cross"},')
        elif ind == 'adx':
            entry_rules_lines.append(
                '        {"indicator": "adx", "params": {"period": 14, "threshold": 20}, "operator": ">"},')

    if not entry_rules_lines:
        entry_rules_lines.append(
            '        {"indicator": "rsi", "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_threshold", 30)}, "operator": "<"},')

    entry_rules_str = '\n'.join(entry_rules_lines)
    indicators_str = str(found_indicators[:5])

    # 生成完整代码
    rebuilt_code = f'''
from typing import Dict, Any

def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {{"type": "int", "low": low, "high": high, "step": step}}

def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {{"type": "float", "low": low, "high": high, "step": step}}

def _p_choice(choices: list) -> dict:
    return {{"type": "choice", "choices": choices}}


def _build_{key}_config(p: dict) -> dict:
    entry_rules = [
{entry_rules_str}
    ]

    return {{
        "name": f"{strategy_name}_{{p.get('rsi_period', 14)}}",
        "entry_rules": entry_rules,
        "position_config": {{"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0}},
        "pyramiding_rules": {{"enabled": False}},
        "risk_management": {{
            "stop_loss": {{"enabled": True, "value": p.get("stop_loss_pct", 3.0)}},
            "trailing_stop": {{"enabled": False}},
        }},
    }}


STRATEGY_TEMPLATE_KEY = "{key}"

STRATEGY_TEMPLATE_DICT = {{
    "name": "{strategy_name}",
    "description": "LLM 生成的策略（自动重建）",
    "indicators": {indicators_str},
    "params": {{
{params_str}
    }},
    "constraints": [],
    "build_config": _build_{key}_config,
}}
'''
    return rebuilt_code


# ============================================================
# 策略模板格式化输出
# ============================================================

def format_template_code(key: str, template: dict) -> str:
    """将策略模板格式化为可插入 param_space.py 的 Python 代码"""
    build_fn_name = f"_build_{key}_config"

    # 优先使用保存的原始代码
    source_code = template.get("_source_code", "")

    if source_code:
        # 从原始代码中提取 _build_xxx_config 函数部分
        build_source = _extract_build_function(source_code, build_fn_name)
    else:
        # fallback: 尝试 inspect.getsource
        build_fn = template.get("build_config")
        if callable(build_fn):
            try:
                import inspect
                build_source = inspect.getsource(build_fn)
            except (OSError, TypeError):
                build_source = f"def {build_fn_name}(p: dict) -> dict:\n    # 动态生成\n    pass\n"
        else:
            build_source = f"def {build_fn_name}(p: dict) -> dict:\n    pass"

    # 格式化模板字典
    params_str = "{\n"
    for pname, pspec in template.get("params", {}).items():
        if pname.startswith("_"):  # 跳过内部字段
            continue
        params_str += f'            "{pname}": {pspec},\n'
    params_str += "        }"

    constraints_str = "[\n"
    for c in template.get("constraints", []):
        if isinstance(c, (list, tuple)) and len(c) == 3:
            left, op, right = c
            constraints_str += f'            ("{left}", "{op}", "{right}"),\n'
    constraints_str += "        ]"

    indicators_str = str(template.get("indicators", []))

    code = f'''
# ── {template.get("name", key)} ──
{build_source}

"{key}": {{
    "name": "{template.get('name', key)}",
    "description": "{template.get('description', '')}",
    "indicators": {indicators_str},
    "params": {params_str},
    "constraints": {constraints_str},
    "build_config": {build_fn_name},
}},
'''
    return code


def _extract_build_function(source_code: str, fn_name: str) -> str:
    """从完整代码中提取指定的 _build_xxx_config 函数"""
    lines = source_code.split('\n')
    result_lines = []
    in_func = False
    func_indent = 0

    for line in lines:
        stripped = line.strip()
        # 检测函数定义开始
        if stripped.startswith(f'def {fn_name}(') or stripped.startswith(f'def {fn_name} ('):
            in_func = True
            func_indent = len(line) - len(line.lstrip())
            result_lines.append(line)
            continue

        if in_func:
            # 检测下一个顶层定义（同级或更少缩进的 def/class）
            if stripped and not stripped.startswith('#'):
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= func_indent and (stripped.startswith('def ') or stripped.startswith('class ') or stripped.startswith('STRATEGY_TEMPLATE')):
                    break
            result_lines.append(line)

    if result_lines:
        return '\n'.join(result_lines)

    # 如果没找到，返回占位
    return f"def {fn_name}(p: dict) -> dict:\n    # 函数源码提取失败\n    pass"


# ============================================================
# 批量生成
# ============================================================

# 预定义的 A 股策略描述列表
ASHARE_STRATEGY_PROMPTS = [
    "生成一个基于 VWAP + RSI + 成交量的日内短线策略，适合 A 股 T+1 制度",
    "生成一个基于 OBV（能量潮）趋势确认 + 均线的中线策略",
    "生成一个基于 ATR 通道收缩后突破的策略，类似布林带收缩但更适应 A 股波动",
    "生成一个基于 MACD 底背离 + 成交量放大的抄底策略",
    "生成一个基于 KDJ 超卖 + 均线多头排列的回调买入策略",
    "生成一个基于换手率突增 + 价格突破的策略，适合 A 股游资风格",
    "生成一个基于 EMA 多头排列 + RSI 动量确认的趋势跟踪策略",
    "生成一个基于布林带下轨支撑 + KDJ 金叉的反弹策略",
]


def batch_generate(output_path: str = "strategies_generated.py"):
    """批量生成策略模板"""
    from optimizer.param_space import list_templates as _orig_list
    from optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES

    existing = _orig_list() + list(ASHARE_STRATEGY_TEMPLATES.keys())
    all_templates = {}

    for i, prompt_desc in enumerate(ASHARE_STRATEGY_PROMPTS, 1):
        print(f"\n{'─'*50}")
        print(f"  [{i}/{len(ASHARE_STRATEGY_PROMPTS)}] 生成: {prompt_desc[:40]}...")
        print(f"{'─'*50}")

        prompt = build_generation_prompt(prompt_desc, existing)
        raw_output = call_llm(prompt)

        if not raw_output:
            print(f"  ⚠️  跳过（LLM 无响应）")
            continue

        code = extract_python_code(raw_output)
        valid, msg = validate_strategy_code(code)

        if not valid:
            print(f"  ❌ {msg}")
            # 尝试修复
            print(f"  🔧 尝试自动修复...")
            code = try_fix_strategy_code(code, msg)
            valid, msg = validate_strategy_code(code)
            if not valid:
                print(f"  ❌ 修复失败: {msg}")
                # 尝试 LLM 修复
                print(f"  🔧 尝试 LLM 修复...")
                fix_prompt = f"以下代码有错误，请修复并只输出修复后的完整代码：\n\n错误信息：{msg}\n\n代码：\n{code}"
                fixed_output = call_llm(fix_prompt)
                if fixed_output:
                    code = extract_python_code(fixed_output)
                    code = try_fix_strategy_code(code, msg)
                    valid, msg = validate_strategy_code(code)
                    if not valid:
                        print(f"  ❌ LLM 修复也失败: {msg}")
                        continue

        templates = execute_and_extract(code)
        if templates:
            # 过滤掉已有的
            new_templates = {k: v for k, v in templates.items() if k not in existing}
            all_templates.update(new_templates)
            existing.extend(new_templates.keys())
            print(f"  ✅ 生成成功: {list(new_templates.keys())}")
        else:
            print(f"  ❌ 无法提取策略模板")

    # 输出
    if all_templates:
        output_lines = ['"""LLM 生成的 A 股策略模板"""\n']
        for key, template in all_templates.items():
            output_lines.append(format_template_code(key, template))

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(output_lines))

        print(f"\n{'='*50}")
        print(f"  生成完成: {len(all_templates)} 个新策略模板")
        print(f"  保存到: {output_path}")
        print(f"{'='*50}")
    else:
        print(f"\n  ❌ 没有成功生成任何策略模板")

    return all_templates


# ============================================================
# 合并所有扩展模板
# ============================================================

def merge_templates():
    """将所有扩展模板合并到统一注册表"""
    from optimizer.param_space import STRATEGY_TEMPLATES
    from optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES

    merged = {}
    merged.update(STRATEGY_TEMPLATES)
    merged.update(ASHARE_STRATEGY_TEMPLATES)

    # 尝试加载 LLM 生成的模板
    try:
        from optimizer.strategies_generated import GENERATED_TEMPLATES
        merged.update(GENERATED_TEMPLATES)
        print(f"  加载 LLM 生成模板: {len(GENERATED_TEMPLATES)} 个")
    except ImportError:
        pass

    print(f"\n  合并后总模板数: {len(merged)}")
    print(f"  模板列表: {', '.join(merged.keys())}")
    return merged


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="LLM 策略模板生成器")
    sub = parser.add_subparsers(dest="command")

    # generate
    gen_parser = sub.add_parser("generate", help="生成单个策略模板")
    gen_parser.add_argument("--prompt", "-p", type=str, required=True, help="策略描述")
    gen_parser.add_argument("--output", "-o", type=str, default="new_strategy.py", help="输出文件")

    # batch
    batch_parser = sub.add_parser("batch", help="批量生成策略模板")
    batch_parser.add_argument("--output", "-o", type=str, default="strategies_generated.py", help="输出文件")

    # merge
    sub.add_parser("merge", help="合并所有扩展模板")

    # list
    sub.add_parser("list", help="列出所有可用模板")

    args = parser.parse_args()

    if args.command == "generate":
        prompt = build_generation_prompt(args.prompt)
        raw = call_llm(prompt)
        if raw:
            code = extract_python_code(raw)
            valid, msg = validate_strategy_code(code)
            if valid:
                with open(args.output, 'w', encoding='utf-8') as f:
                    f.write(code)
                print(f"  ✅ 保存到: {args.output}")
            else:
                print(f"  ❌ {msg}")
        else:
            print("  ❌ LLM 无响应")

    elif args.command == "batch":
        batch_generate(args.output)

    elif args.command == "merge":
        merge_templates()

    elif args.command == "list":
        from optimizer.param_space import list_templates
        from optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES
        print(f"\n  原始模板 ({len(list_templates())}):")
        for t in list_templates():
            print(f"    - {t}")
        print(f"\n  A 股扩展模板 ({len(ASHARE_STRATEGY_TEMPLATES)}):")
        for t in ASHARE_STRATEGY_TEMPLATES:
            print(f"    - {t}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
