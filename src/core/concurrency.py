"""
AI 任务并发限流

限制同时进行的 AI 对话处理任务数量，防止突发高并发导致资源耗尽。
仅限流资源密集型 AI 任务（SSE 长连接），不影响系统轻量任务。
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# 最大并发 AI 任务数
MAX_CONCURRENT_AI_TASKS = 30

_ai_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI_TASKS)


async def run_with_limit(coro):
    """在信号量控制下执行协程

    超出并发限制的任务会排队等待，不会报错或丢弃。

    Usage:
        await run_with_limit(self.orchestrator.handle_text_message(...))
    """
    async with _ai_semaphore:
        return await coro
