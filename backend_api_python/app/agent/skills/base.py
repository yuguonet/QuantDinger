"""
Skill 基类

Skill 是一个可组合的能力单元，封装了：
- LLM 调用
- 提示词模板
- 输出解析
- 错误处理

与 Tool 的区别：
- Tool: 执行确定性操作（API调用、文件读写、代码执行）
- Skill: 利用 LLM 完成智能任务（文本分析、内容生成、信息提取）
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

# 【2026-10-13 P1】原先 `from llm.base import LLMBase, ChatMessage, LLMResponse`。
# 旧自建 llm/ 包已退役：LLM 出口统一到 `app.agent.llm`，消息格式就是 OpenAI 原生
# dict（不再有 ChatMessage），LLMBase 这条抽象链整体消失。
from app.agent.llm import LLMResponse

logger = logging.getLogger(__name__)


class Skill(ABC):
    """
    Skill 基类

    使用示例：
        class SummarizeSkill(Skill):
            name = "summarize"
            system_prompt = "你是一个文本摘要专家。"

            async def run(self, text: str) -> str:
                response = await self.call_llm(f"请总结以下内容：\\n{text}")
                return response.content
    """

    name: str = ""
    description: str = ""
    system_prompt: str = ""

    # 【2026-10-13 P1】不再持有 LLM 实例：Skill 原来靠 `set_llm()` 注入一个
    # LLMBase 对象（实测全项目无人调用，属死接口）。统一出口 `app.agent.llm`
    # 内部按 provider+model+timeout 缓存 Model，Skill 侧不需要也不该持有连接。

    async def call_llm(
        self,
        user_message: str,
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """
        调用 LLM 的便捷方法

        :param user_message: 用户消息
        :param system_message: 系统消息（不传则使用 self.system_prompt）
        :param temperature: 温度参数
        :param max_tokens: 最大 token 数
        :return: LLMResponse
        """
        from app.agent.llm import acomplete
        messages = []
        sys_msg = system_message or self.system_prompt
        if sys_msg:
            messages.append({"role": "system", "content": sys_msg})
        messages.append({"role": "user", "content": user_message})

        return await acomplete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @abstractmethod
    async def run(self, *args, **kwargs) -> Any:
        """
        执行技能

        子类必须实现此方法。
        """
        ...

    async def safe_run(self, *args, **kwargs) -> Any:
        """安全执行（自动捕获异常）"""
        try:
            return await self.run(*args, **kwargs)
        except Exception as e:
            logger.error(f"Skill '{self.name}' 执行失败: {e}", exc_info=True)
            return None
