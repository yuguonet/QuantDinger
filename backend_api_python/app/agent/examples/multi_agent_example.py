"""
示例 5: 多Agent协作

演示多个专门化Agent如何协同工作完成复杂任务。
场景：研究助手 - 由研究员、分析师、写作者三个Agent协作完成研究报告。

运行: python -m examples.multi_agent_example
"""

import asyncio
from typing import Optional
from llm import LLMFactory
from llm.base import ChatMessage, LLMResponse
from memory import LocalMemory
from tools import ToolRegistry
from tools.builtin import WebSearchTool, CodeExecutorTool, FileReaderTool
from agents import TaskAgent, ChatAgent
from agents.base import AgentBase, AgentResponse
from config.loader import get_settings


class ResearcherAgent(TaskAgent):
    """
    研究员Agent：负责收集信息和数据
    """

    def __init__(self, llm, tool_registry):
        super().__init__(
            llm=llm,
            memory=LocalMemory(max_history=10),
            tool_registry=tool_registry,
            system_prompt=(
                "你是一个专业的研究员。你的职责是：\n"
                "1. 搜索相关信息和数据\n"
                "2. 执行代码进行数据分析\n"
                "3. 读取文件获取参考资料\n"
                "4. 将收集到的信息整理成结构化的研究报告\n\n"
                "请使用工具来完成任务，并返回详细的研究结果。"
            ),
            max_tool_rounds=8,
        )


class AnalystAgent(ChatAgent):
    """
    分析师Agent：负责分析数据、提取关键信息
    """

    def __init__(self, llm):
        super().__init__(
            llm=llm,
            memory=LocalMemory(max_history=10),
            system_prompt=(
                "你是一个资深数据分析师。你的职责是：\n"
                "1. 分析研究员提供的原始数据\n"
                "2. 提取关键趋势和模式\n"
                "3. 进行深度洞察分析\n"
                "4. 生成结构化的分析结论\n\n"
                "请基于提供的研究数据，输出专业的分析报告。"
            ),
        )


class WriterAgent(ChatAgent):
    """
    写作者Agent：负责撰写最终报告
    """

    def __init__(self, llm):
        super().__init__(
            llm=llm,
            memory=LocalMemory(max_history=10),
            system_prompt=(
                "你是一个专业的科技写作专家。你的职责是：\n"
                "1. 将研究数据和分析结论整合成完整的报告\n"
                "2. 使用清晰的结构和专业的语言\n"
                "3. 确保报告逻辑严密、条理清晰\n"
                "4. 输出格式化的研究报告\n\n"
                "请基于研究和分析结果，撰写一份高质量的研究报告。"
            ),
        )


class CoordinatorAgent:
    """
    协调者Agent：负责协调多个Agent的工作流程
    """

    def __init__(
        self, researcher: ResearcherAgent, analyst: AnalystAgent, writer: WriterAgent
    ):
        self.researcher = researcher
        self.analyst = analyst
        self.writer = writer

    async def execute_research_task(
        self, topic: str, session_id: str = "research"
    ) -> AgentResponse:
        """
        执行多Agent协作的研究任务

        流程：
        1. 研究员收集信息
        2. 分析师分析数据
        3. 写作者撰写报告
        """
        import time

        start_time = time.time()

        print(f"\n{'='*60}")
        print(f"🎯 开始研究任务: {topic}")
        print(f"{'='*60}\n")

        # 第1步：研究员收集信息
        print("📚 [阶段1] 研究员正在收集信息...")
        research_prompt = f"""
        请对以下主题进行全面研究：
        主题：{topic}
        
        要求：
        1. 搜索最新的相关信息和数据
        2. 收集关键的统计数据和发展趋势
        3. 整理成结构化的研究报告
        4. 包含具体的数据点和来源说明
        """

        research_result = await self.researcher.chat(
            research_prompt, session_id=f"{session_id}_research"
        )
        print(f"✅ 研究完成，收集到 {len(research_result.tool_calls)} 次工具调用数据")
        print(f"📊 研究摘要: {research_result.content[:200]}...\n")

        # 第2步：分析师分析数据
        print("📈 [阶段2] 分析师正在分析数据...")
        analysis_prompt = f"""
        请对以下研究数据进行深度分析：
        
        {research_result.content}
        
        要求：
        1. 提取关键趋势和模式
        2. 进行对比和关联分析
        3. 识别重要发现和洞察
        4. 输出结构化的分析结论
        """

        analysis_result = await self.analyst.chat(
            analysis_prompt, session_id=f"{session_id}_analysis"
        )
        print(f"✅ 分析完成")
        print(f"💡 分析摘要: {analysis_result.content[:200]}...\n")

        # 第3步：写作者撰写报告
        print("📝 [阶段3] 写作者正在撰写报告...")
        writing_prompt = f"""
        请基于以下研究数据和分析结论，撰写一份完整的研究报告：
        
        【研究数据】
        {research_result.content}
        
        【分析结论】
        {analysis_result.content}
        
        要求：
        1. 使用专业的报告格式
        2. 包含标题、摘要、正文、结论
        3. 语言清晰、逻辑严密
        4. 突出关键发现和建议
        """

        final_report = await self.writer.chat(
            writing_prompt, session_id=f"{session_id}_writing"
        )
        print(f"✅ 报告撰写完成\n")

        elapsed = time.time() - start_time

        # 整合所有结果
        return AgentResponse(
            content=final_report.content,
            sources=[
                {"stage": "research", "content": research_result.content[:500]},
                {"stage": "analysis", "content": analysis_result.content[:500]},
                {"stage": "writing", "content": final_report.content[:500]},
            ],
            tokens_used=research_result.tokens_used
            + analysis_result.tokens_used
            + final_report.tokens_used,
            model=final_report.model,
            session_id=session_id,
            elapsed_seconds=round(elapsed, 2),
        )


async def main():
    # 1. 加载配置
    settings = get_settings(config_dir="config")

    # 2. 创建LLM实例（为不同类型的Agent使用不同的温度参数）
    research_llm = LLMFactory.create(
        provider=settings.llm.provider,
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        temperature=0.3,  # 研究需要准确性
    )

    analysis_llm = LLMFactory.create(
        provider=settings.llm.provider,
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        temperature=0.5,  # 分析需要一定的创造性
    )

    writing_llm = LLMFactory.create(
        provider=settings.llm.provider,
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        temperature=0.7,  # 写作需要更高的创造性
    )

    # 3. 为研究员Agent配置工具
    tool_registry = ToolRegistry()
    tool_registry.register(WebSearchTool())
    tool_registry.register(CodeExecutorTool())
    tool_registry.register(FileReaderTool())

    # 4. 创建专门化的Agent
    researcher = ResearcherAgent(llm=research_llm, tool_registry=tool_registry)
    analyst = AnalystAgent(llm=analysis_llm)
    writer = WriterAgent(llm=writing_llm)

    # 5. 创建协调者
    coordinator = CoordinatorAgent(researcher, analyst, writer)

    # 6. 执行多Agent协作任务
    print("=== 多Agent协作演示 ===\n")

    research_topics = [
        "2024年人工智能最新发展趋势和应用案例",
        "Python在数据科学领域的最新工具和技术",
    ]

    for i, topic in enumerate(research_topics, 1):
        print(f"\n{'#'*60}")
        print(f"任务 {i}: {topic}")
        print(f"{'#'*60}")

        result = await coordinator.execute_research_task(
            topic=topic, session_id=f"multi_agent_{i}"
        )

        print(f"\n{'='*60}")
        print(f"📋 最终报告:")
        print(f"{'='*60}")
        print(result.content)
        print(f"\n⏱️ 总耗时: {result.elapsed_seconds}秒")
        print(f"🎯 使用模型: {result.model}")
        print(f"📊 总Token消耗: {result.tokens_used}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
