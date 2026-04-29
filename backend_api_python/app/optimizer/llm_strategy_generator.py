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
"""


def build_generation_prompt(strategy_description: str, existing_templates: List[str] = None) -> str:
    """构建生成 prompt"""
    existing = ""
    if existing_templates:
        existing = f"\n\n已有策略模板（不要重复生成这些）：\n" + "\n".join(f"- {t}" for t in existing_templates)

    return f"""请根据以下描述生成一个 A 股策略模板：

{strategy_description}{existing}

要求：
1. 生成完整的 `_build_xxx_config` 函数
2. 生成策略模板字典（包含 name, description, indicators, params, constraints, build_config）
3. 策略模板字典的 key 用英文下划线命名
4. 参数空间要覆盖合理范围，step 合理
5. 必须包含止损配置

请直接输出 Python 代码，不要包含任何解释文字。
代码需要包含一个 `STRATEGY_TEMPLATE_KEY` 变量（字符串）和一个 `STRATEGY_TEMPLATE_DICT` 变量（字典）。
"""


# ============================================================
# 代码解析与验证
# ============================================================

def extract_python_code(raw_text: str) -> str:
    """从 LLM 输出中提取 Python 代码"""
    # 尝试从 markdown 代码块中提取
    code_blocks = re.findall(r'```(?:python)?\s*\n(.*?)```', raw_text, re.DOTALL)
    if code_blocks:
        return '\n'.join(code_blocks)

    # 尝试找 def _build_ 开头的代码
    lines = raw_text.split('\n')
    code_lines = []
    in_code = False
    for line in lines:
        if line.strip().startswith('def _build_') or line.strip().startswith('STRATEGY_TEMPLATE'):
            in_code = True
        if in_code:
            code_lines.append(line)

    if code_lines:
        return '\n'.join(code_lines)

    # 全文当作代码
    return raw_text


def validate_strategy_code(code: str) -> tuple[bool, str]:
    """验证生成的策略代码是否符合规范"""
    # 检查必要元素
    checks = [
        ('def _build_', '缺少 _build_xxx_config 函数'),
        ('"entry_rules"', '缺少 entry_rules'),
        ('"position_config"', '缺少 position_config'),
        ('"risk_management"', '缺少 risk_management'),
        ('"stop_loss"', '缺少 stop_loss 配置'),
    ]

    errors = []
    for check, msg in checks:
        if check not in code:
            errors.append(msg)

    # 参数定义检查（OR 逻辑）
    if not any(p in code for p in ['_p_int', '_p_float', '_p_choice']):
        errors.append('缺少参数定义（应使用 _p_int/_p_float/_p_choice）')

    # 语法检查
    try:
        compile(code, '<strategy>', 'exec')
    except SyntaxError as e:
        errors.append(f"语法错误: {e}")

    if errors:
        return False, "验证失败:\n" + "\n".join(f"  - {e}" for e in errors)
    return True, "验证通过"


def execute_and_extract(code: str) -> Optional[Dict[str, Any]]:
    """执行代码并提取策略模板"""
    try:
        namespace = {
            "_p_int": lambda low, high, step=1: {"type": "int", "low": low, "high": high, "step": step},
            "_p_float": lambda low, high, step=0.001: {"type": "float", "low": low, "high": high, "step": step},
            "_p_choice": lambda choices: {"type": "choice", "choices": choices},
        }
        exec(code, namespace)

        key = namespace.get("STRATEGY_TEMPLATE_KEY")
        template = namespace.get("STRATEGY_TEMPLATE_DICT")

        if key and template:
            return {key: template}

        # 尝试找所有 STRATEGY_TEMPLATE_* 变量
        templates = {}
        for k, v in namespace.items():
            if k.startswith("STRATEGY_TEMPLATE") and isinstance(v, dict) and "build_config" in v:
                # 从变量名推断 key
                template_key = k.replace("STRATEGY_TEMPLATE_", "").lower()
                templates[template_key] = v

        return templates if templates else None
    except Exception as e:
        print(f"  执行代码失败: {e}")
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
# 策略模板格式化输出
# ============================================================

def format_template_code(key: str, template: dict) -> str:
    """将策略模板格式化为可插入 param_space.py 的 Python 代码"""
    build_fn_name = f"_build_{key}_config"
    build_fn = template.get("build_config")

    # 如果 build_config 是函数，获取源码
    if callable(build_fn):
        import inspect
        build_source = inspect.getsource(build_fn)
    else:
        build_source = f"def {build_fn_name}(p: dict) -> dict:\n    # TODO: 实现\n    pass"

    # 格式化模板字典
    params_str = "{\n"
    for pname, pspec in template.get("params", {}).items():
        params_str += f'            "{pname}": {pspec},\n'
    params_str += "        }"

    constraints_str = "[\n"
    for left, op, right in template.get("constraints", []):
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
    from app.optimizer.param_space import list_templates as _orig_list
    from app.optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES

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
            fix_prompt = f"以下代码有错误，请修复并只输出修复后的代码：\n\n错误信息：{msg}\n\n代码：\n{code}"
            fixed_output = call_llm(fix_prompt)
            if fixed_output:
                code = extract_python_code(fixed_output)
                valid, msg = validate_strategy_code(code)
                if not valid:
                    print(f"  ❌ 修复失败: {msg}")
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
    from app.optimizer.param_space import STRATEGY_TEMPLATES
    from app.optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES

    merged = {}
    merged.update(STRATEGY_TEMPLATES)
    merged.update(ASHARE_STRATEGY_TEMPLATES)

    # 尝试加载 LLM 生成的模板
    try:
        from app.optimizer.strategies_generated import GENERATED_TEMPLATES
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
        from app.optimizer.param_space import list_templates
        from app.optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES
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
