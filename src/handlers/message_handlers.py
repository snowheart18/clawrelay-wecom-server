#!/usr/bin/env python
# coding=utf-8
"""
消息类型处理器模块
负责处理不同类型的企业微信消息(text/voice/file/image/mixed/stream/event)

文本消息路由到 ClaudeRelayOrchestrator，由 Claude Relay 驱动对话
"""

import logging
import json
import asyncio
import os
import time
from typing import List, Optional
from src.utils.weixin_utils import MessageBuilder, StreamManager, ImageUtils, FileUtils, ProactiveReplyClient
from src.handlers.command_handlers import CommandRouter
from src.utils.database import get_user_name_by_wework_user_id

# 先定义logger,避免import失败时无法使用
logger = logging.getLogger(__name__)

from src.core.claude_relay_orchestrator import ClaudeRelayOrchestrator


class FileMergeBuffer:
    """双向文件-文本合并缓冲

    企业微信中文件和文字是两条独立消息。由于文件需要传输时间，
    通常文字先到、文件后到。此缓冲支持双向合并：

    场景A（常见）: 文字先到 → 检测到文件意图 → 缓冲等待文件 → 合并处理
    场景B（少见）: 文件先到 → 缓冲等待文字 → 合并处理
    """

    # 文件意图关键词：文本包含这些词时，可能即将有文件到达（中文+日语+英文）
    # 注意：匹配时 text 会先 .lower()，所以英文关键词统一小写
    FILE_INTENT_KEYWORDS = {
        # 中文 - 通用
        '文件', '文档', '表格', '附件', '数据', '报表', '报告', '资料',
        '这份', '发给你', '发你的', '刚发的',
        # 中文 - 具体文件类型
        '这个excel', '这个pdf', '这个ppt', '这个word', '这个csv',
        '这个文档', '这个表', '这个报表', '这个报告',
        # 英文 - 文件类型（不区分大小写）
        'excel', 'pdf', 'ppt', 'pptx', 'word', 'csv',
        'doc', 'docx', 'xlsx', 'xls',
        # 日語
        'ファイル', '添付', 'エクセル', '表計算', 'このファイル',
        'この資料', 'この表', '送った', '送ります', '送りました',
        'データ', '資料', 'レポート',
    }

    # 图片意图关键词：文本包含这些词时，可能即将有图片到达
    # 保持保守，避免误判（如"我想生成一张图片"不应触发等待）
    IMAGE_INTENT_KEYWORDS = {
        # 中文 - 指代性短语（用户指的是即将发送的图片）
        '这张图', '这个图', '这张照片', '这个照片', '这张截图', '这个截图',
        '这张图片', '这个图片',
        '看看这张', '看这张', '看这个图',
        '发给你的图', '发你的图', '刚发的图', '刚发的照片',
        '发给你的照片', '发你的照片',
        # 日語
        'この画像', 'この写真', 'このスクショ',
    }

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout
        self._pending_texts: dict[str, dict] = {}  # 文字等文件/图片
        self._pending_files: dict[str, dict] = {}  # 文件等文字
        self._pending_images: dict[str, dict] = {}  # 图片等文字

    # ---- 文字意图检测 ----

    @staticmethod
    def has_file_intent(text: str) -> bool:
        """检测文本是否包含文件相关意图"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in FileMergeBuffer.FILE_INTENT_KEYWORDS)

    @staticmethod
    def has_image_intent(text: str) -> bool:
        """检测文本是否包含图片相关意图"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in FileMergeBuffer.IMAGE_INTENT_KEYWORDS)

    # ---- 场景A: 文字先到，等文件 ----

    def add_text(self, session_key: str, text: str, stream_id: str) -> asyncio.Event:
        """文字到达，存入缓冲等待文件"""
        event = asyncio.Event()
        self._pending_texts[session_key] = {
            'text': text,
            'stream_id': stream_id,
            'event': event,
            'timestamp': time.time(),
        }
        logger.info(f"[FileMerge] 文字入缓冲等文件: session_key={session_key}")
        return event

    async def wait_for_file(self, session_key: str) -> bool:
        """文字等待文件到达。返回 True=文件已到并接管处理，False=超时"""
        pending = self._pending_texts.get(session_key)
        if not pending:
            return False
        try:
            await asyncio.wait_for(pending['event'].wait(), timeout=self._timeout)
            logger.info(f"[FileMerge] 文件到达，文字等待结束: session_key={session_key}")
            return True
        except asyncio.TimeoutError:
            logger.info(f"[FileMerge] 等文件超时，文字将独立处理: session_key={session_key}")
            return False
        finally:
            self._pending_texts.pop(session_key, None)

    def try_get_pending_text(self, session_key: str) -> Optional[dict]:
        """文件或图片到达时，检查是否有待合并的文字

        Returns:
            {"text": ..., "stream_id": ...} 或 None
        """
        pending = self._pending_texts.get(session_key)
        if not pending:
            return None
        if time.time() - pending['timestamp'] > self._timeout + 1:
            self._pending_texts.pop(session_key, None)
            return None
        # 唤醒文字等待（通知它文件已接管，不用自己处理了）
        pending['event'].set()
        return {'text': pending['text'], 'stream_id': pending['stream_id']}

    # ---- 场景B: 文件先到，等文字 ----

    def add_file(self, session_key: str, file_part: dict) -> asyncio.Event:
        """文件到达，存入缓冲等待文字"""
        event = asyncio.Event()
        self._pending_files[session_key] = {
            'file_parts': [file_part],
            'event': event,
            'timestamp': time.time(),
            'text': None,
        }
        logger.info(f"[FileMerge] 文件入缓冲等文字: session_key={session_key}")
        return event

    async def wait_for_text(self, session_key: str) -> Optional[str]:
        """文件等待文字到达，返回文字或 None（超时）"""
        pending = self._pending_files.get(session_key)
        if not pending:
            return None
        try:
            await asyncio.wait_for(pending['event'].wait(), timeout=self._timeout)
            return pending.get('text')
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_files.pop(session_key, None)

    def try_merge_text_to_file(self, session_key: str, text: str) -> Optional[list]:
        """文字到达时，检查是否有待合并的文件（场景B）

        Returns:
            文件 content parts 列表，或 None
        """
        pending = self._pending_files.get(session_key)
        if not pending:
            return None
        if time.time() - pending['timestamp'] > self._timeout + 1:
            self._pending_files.pop(session_key, None)
            return None
        pending['text'] = text
        pending['event'].set()
        logger.info(f"[FileMerge] 文字到达，唤醒文件等待: session_key={session_key}")
        return pending['file_parts']

    # ---- 场景B(图片): 图片先到，等文字 ----

    def add_image(self, session_key: str, data_uri: str) -> asyncio.Event:
        """图片到达，存入缓冲等待文字"""
        event = asyncio.Event()
        self._pending_images[session_key] = {
            'data_uri': data_uri,
            'event': event,
            'timestamp': time.time(),
            'text': None,
        }
        logger.info(f"[FileMerge] 图片入缓冲等文字: session_key={session_key}")
        return event

    async def wait_for_text_for_image(self, session_key: str) -> Optional[str]:
        """图片等待文字到达，返回文字或 None（超时）"""
        pending = self._pending_images.get(session_key)
        if not pending:
            return None
        try:
            await asyncio.wait_for(pending['event'].wait(), timeout=self._timeout)
            return pending.get('text')
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_images.pop(session_key, None)

    def try_merge_text_to_image(self, session_key: str, text: str) -> Optional[str]:
        """文字到达时，检查是否有待合并的图片（图片场景B）

        Returns:
            图片 data_uri，或 None
        """
        pending = self._pending_images.get(session_key)
        if not pending:
            return None
        if time.time() - pending['timestamp'] > self._timeout + 1:
            self._pending_images.pop(session_key, None)
            return None
        pending['text'] = text
        pending['event'].set()
        logger.info(f"[FileMerge] 文字到达，唤醒图片等待: session_key={session_key}")
        return pending['data_uri']


# 全局文件合并缓冲实例
_file_merge_buffer = FileMergeBuffer(timeout=10.0)


async def _proactive_reply_on_complete(
    task: asyncio.Task,
    response_url: str,
    stream_id: str,
    user_id: str,
    bot_key: str,
):
    """等待后台任务完成，通过 response_url 主动推送最终结果

    任务在流式超时后仍在后台运行，此协程等待其完成并 POST 结果。
    response_url 有效期 1 小时，仅可调用 1 次。
    """
    try:
        result = await task  # 等待任务真正完成（可能数十分钟）

        # AskUserQuestion 场景：response_url 需保留给 _submit_answers_async 使用
        # 如果此时有待处理的选择会话，说明 Claude 触发了 AskUserQuestion，
        # 后续用户回答后 _submit_answers_async 会通过 response_url 推送最终结果
        from src.core.choice_manager import get_choice_manager
        if get_choice_manager().has_pending_choice(bot_key, user_id):
            logger.info(
                "[主动回复] 检测到待处理的选择会话，保留 response_url: bot=%s, user=%s, stream=%s",
                bot_key, user_id, stream_id
            )
            return

        if result:
            reply_json, _ = result
            reply_data = json.loads(reply_json)
            text = reply_data.get('stream', {}).get('content', '')
            if text and text.strip():
                success = await ProactiveReplyClient.send_markdown(response_url, text)
                if success:
                    logger.info(
                        "[主动回复] 推送成功: bot=%s, user=%s, stream=%s, len=%d",
                        bot_key, user_id, stream_id, len(text)
                    )
                else:
                    logger.warning(
                        "[主动回复] 推送失败: bot=%s, user=%s, stream=%s",
                        bot_key, user_id, stream_id
                    )
            else:
                # 任务完成但无文本内容，尝试从 StreamingThinkingManager 获取
                from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                stm = get_streaming_thinking_manager()
                final_text = stm.get_final_answer(stream_id) if hasattr(stm, 'get_final_answer') else ""
                if final_text and final_text.strip():
                    await ProactiveReplyClient.send_markdown(response_url, final_text)
                    logger.info("[主动回复] 从STM获取内容推送成功: stream=%s", stream_id)
                else:
                    logger.info("[主动回复] 任务完成但无可推送内容: stream=%s", stream_id)
        else:
            # result 为 None，说明 agent 结果已通过 STM 处理
            # 尝试从 STM 获取最终答案
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()
            final_text = stm.get_final_answer(stream_id) if hasattr(stm, 'get_final_answer') else ""
            if final_text and final_text.strip():
                await ProactiveReplyClient.send_markdown(response_url, final_text)
                logger.info("[主动回复] 从STM获取内容推送成功: stream=%s", stream_id)
            else:
                logger.info("[主动回复] 任务完成但无可推送内容: stream=%s", stream_id)
    except asyncio.CancelledError:
        logger.info("[主动回复] 后台任务被取消: stream=%s", stream_id)
    except Exception as e:
        logger.error(
            "[主动回复] 后台等待异常: bot=%s, user=%s, stream=%s, error=%s",
            bot_key, user_id, stream_id, e, exc_info=True
        )


def _get_session_key(data: dict) -> str:
    """根据消息类型确定会话key：单聊用userid，群聊用chatid

    群聊场景下，同一群内所有用户共享一个会话（按 chatid 隔离）。
    单聊场景下，按 userid 隔离（与原有逻辑一致）。

    Args:
        data: 企业微信回调的消息数据

    Returns:
        str: 会话key（群聊返回chatid，单聊返回userid）
    """
    if data.get('chattype') == 'group':
        chatid = data.get('chatid', '')
        if chatid:
            return chatid
    return data.get('from', {}).get('userid', '')


class MessageHandler:
    """消息处理器基类"""

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        """
        处理消息(异步方法)

        Args:
            data: 解密后的消息数据
            stream_id: 流式消息ID

        Returns:
            消息JSON字符串或None
        """
        raise NotImplementedError


class TextMessageHandler(MessageHandler):
    """文本消息处理器

    通过 ClaudeRelayOrchestrator 驱动 AI 对话，支持引用消息上下文提取。
    """

    def __init__(self, global_stream_mgr: StreamManager, bot_key: str = "default", encoding_aes_key: str = ""):
        """
        初始化文本消息处理器

        Args:
            global_stream_mgr: 全局流式管理器
            bot_key: 机器人标识(v2.0)
            encoding_aes_key: 机器人的 EncodingAESKey（v10.0 引用图片解密用）
        """
        self.global_stream_mgr = global_stream_mgr
        self.bot_key = bot_key
        self.encoding_aes_key = encoding_aes_key
        self.async_agent_enabled = os.getenv("WEIXIN_AGENT_ASYNC_MODE", "true").lower() in ("1", "true", "yes", "on")
        # 企业微信流式刷新最多持续6分钟，超时需留余量给最终回复，默认300秒（5分钟）
        self.agent_timeout_seconds = float(os.getenv("WEIXIN_AGENT_TIMEOUT_SECONDS", "300.0"))
        self.agent_initial_status = os.getenv(
            "WEIXIN_AGENT_INITIAL_STATUS",
            "正在召唤智能助手，请稍候..."
        )

        # v2.1: 获取机器人名称,用于过滤@提及
        self.bot_name = self._get_bot_name()

        # 传统命令路由器(reset/clear等内置命令)
        self.command_router = CommandRouter()

        # Claude Relay 编排器
        bot_cfg = self._get_bot_config()
        self.agent = ClaudeRelayOrchestrator(
            bot_key=bot_key,
            relay_url=bot_cfg.relay_url or "http://localhost:50009",
            working_dir=bot_cfg.working_dir or "",
            model=bot_cfg.model or "vllm/claude-sonnet-4-6",
            system_prompt=bot_cfg.system_prompt or "",
            env_vars=bot_cfg.env_vars or None,
        )
        mode_label = "异步" if self.async_agent_enabled else "同步"
        logger.info(f"TextMessageHandler初始化: Claude Relay({mode_label}), bot={bot_key}")

    def _get_bot_name(self) -> str:
        """
        从配置中获取机器人名称

        Returns:
            str: 机器人名称,如果未配置则返回空字符串
        """
        try:
            from config.bot_config import BotConfigManager
            # 创建配置管理器实例
            config_mgr = BotConfigManager()
            bot_config = config_mgr.get_bot(self.bot_key)
            if bot_config:
                return bot_config.name
            else:
                logger.warning(f"未找到机器人配置: {self.bot_key}")
                return ""
        except Exception as e:
            logger.warning(f"获取机器人名称失败: {e}")
            return ""

    def _get_bot_config(self):
        """获取机器人配置对象"""
        try:
            from config.bot_config import BotConfigManager
            config_mgr = BotConfigManager()
            return config_mgr.get_bot(self.bot_key)
        except Exception as e:
            logger.warning(f"获取机器人配置失败: {e}")
            return None

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        """处理文本消息(异步)"""
        content = data.get('text', {}).get('content', '')
        user_id = data.get('from', {}).get('userid', '')

        # 群聊会话隔离：提取 session_key
        session_key = _get_session_key(data)
        chattype = data.get('chattype', 'single')

        # v10.1 场景B: 文件先到 → 文字后到 → 合并到文件处理器
        pending_files = _file_merge_buffer.try_merge_text_to_file(session_key, content.strip())
        if pending_files is not None:
            logger.info(
                f"📎 [文件合并-B] 文字并入已缓冲的文件: user={user_id}, "
                f"session_key={session_key}, text=\"{content.strip()[:50]}\""
            )
            return None  # 由 FileMessageHandler 统一处理

        # v10.2 图片场景B: 图片先到 → 文字后到 → 合并到图片处理器
        pending_image = _file_merge_buffer.try_merge_text_to_image(session_key, content.strip())
        if pending_image is not None:
            logger.info(
                f"🖼️ [图片合并-B] 文字并入已缓冲的图片: user={user_id}, "
                f"session_key={session_key}, text=\"{content.strip()[:50]}\""
            )
            return None  # 由 ImageMessageHandler 统一处理

        # 数据流追踪日志
        logger.info(
            f"👤 [用户输入] user={user_id}, chattype={chattype}, "
            f"session_key={session_key}, message=\"{content}\""
        )

        # v2.1: 动态过滤"@机器人名称 "前缀
        if self.bot_name and content.startswith(f"@{self.bot_name} "):
            content = content[len(f"@{self.bot_name} "):]
            logger.info(f"过滤@{self.bot_name}前缀后: '%s'", content)

        # 去除@机器人的部分,提取纯命令
        message_text = content.strip()

        # v7.0: 提取引用消息上下文 (v10.0: 支持引用图片多模态)
        message_text, quote_content_blocks = await self._prepend_quote_context(data, message_text)

        logger.info("解析后的消息: '%s'", message_text)

        # v9.9: 提取 response_url（用于超时后主动推送结果）
        response_url = data.get('response_url', '')

        # 构建日志上下文
        quote = data.get('quote')
        quote_text = ''
        if quote:
            qt = quote.get('msgtype', '')
            if qt == 'text':
                quote_text = quote.get('text', {}).get('content', '')
            elif qt == 'file':
                quote_text = f"[引用文件: {quote.get('file', {}).get('filename', '')}]"
            elif qt == 'image':
                quote_text = '[引用图片]'
            elif qt == 'mixed':
                quote_text = '[引用混合消息]'
        log_context = {
            'chat_type': chattype,
            'chat_id': data.get('chatid', ''),
            'message_type': data.get('_original_msgtype', 'text'),
            'quoted_content': quote_text,
        }

        # v10.0: 引用图片走多模态路径
        if quote_content_blocks is not None:
            user_text = content.strip()
            if user_text:
                quote_content_blocks.append({"type": "text", "text": user_text})
            log_context['message_type'] = 'multimodal'
            return await self._handle_with_agent_multimodal(
                quote_content_blocks, user_id, stream_id,
                response_url=response_url, session_key=session_key,
                log_context=log_context,
            )

        # v10.1 场景A: 文字先到 → 检测文件/图片意图 → 缓冲等媒体
        # 排除 fallback 产生的伪文本（如 "[用户发送了文件:]" "[用户发送了一张图片]"）
        is_fallback_text = message_text.startswith('[用户发送了')
        has_media_intent = (
            _file_merge_buffer.has_file_intent(message_text)
            or _file_merge_buffer.has_image_intent(message_text)
        )
        if not is_fallback_text and has_media_intent:
            intent_type = "文件" if _file_merge_buffer.has_file_intent(message_text) else "图片"
            logger.info(
                f"📎 [媒体合并-A] 检测到{intent_type}意图，缓冲等待媒体: "
                f"user={user_id}, session_key={session_key}"
            )
            _file_merge_buffer.add_text(session_key, message_text, stream_id)
            return await self._handle_text_wait_for_file(
                data, message_text, user_id, stream_id,
                response_url=response_url, session_key=session_key,
                log_context=log_context,
            )

        return await self._handle_with_agent(
            message_text, user_id, stream_id,
            response_url=response_url, session_key=session_key,
            log_context=log_context,
        )

    async def _handle_text_wait_for_file(
        self, data: dict, message_text: str, user_id: str, stream_id: str,
        response_url: str = "", session_key: str = "",
        log_context: dict = None,
    ) -> Optional[str]:
        """场景A: 文字先到，在后台等待文件到达

        立即返回初始流式响应，后台等待文件：
        - 文件到达 → FileMessageHandler 接管，用文字的 stream_id 处理合并消息
        - 超时 → 作为普通文本处理
        """
        timeout = self.agent_timeout_seconds
        bot_key = self.bot_key

        async def wait_job():
            file_arrived = await _file_merge_buffer.wait_for_file(session_key)
            if file_arrived:
                # 文件已接管处理（FileMessageHandler 使用本 stream_id），无需再做
                logger.info(f"📎 [文件合并-A] 文件已接管处理: session_key={session_key}")
                return

            # 超时，作为普通文本处理
            logger.info(f"📎 [文件合并-A] 超时，回退为普通文本: session_key={session_key}")
            agent = self.agent

            inner_task = asyncio.create_task(
                agent.handle_text_message(
                    user_id=user_id,
                    message=message_text,
                    stream_id=stream_id,
                    session_key=session_key,
                    log_context=log_context,
                )
            )

            # v12.0: 注册任务到全局注册表，支持用户停止
            from src.core.task_registry import get_task_registry
            get_task_registry().register(f"{bot_key}:{session_key}", inner_task, stream_id)

            done, _pending = await asyncio.wait({inner_task}, timeout=timeout)
            if done:
                try:
                    inner_task.result()
                except Exception as exc:
                    logger.error(f"[文件合并-A] 回退文本处理异常: {exc}", exc_info=True)
                    from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                    stm = get_streaming_thinking_manager()
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, f"❌ 处理失败：{exc}")
            else:
                from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                stm = get_streaming_thinking_manager()
                if response_url:
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, "⏳ 处理耗时较长，完成后将自动推送结果。")
                    asyncio.create_task(
                        _proactive_reply_on_complete(inner_task, response_url, stream_id, user_id, bot_key)
                    )
                else:
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, "⏰ 处理超时，请稍后再试。")
                    inner_task.cancel()

        asyncio.create_task(wait_job())
        return MessageBuilder.text(stream_id, "", finish=False)

    async def _prepend_quote_context(self, data: dict, message_text: str) -> tuple:
        """提取引用消息内容,作为上下文拼接到用户消息前面

        企业微信引用消息结构:
        {
            "quote": {
                "msgtype": "text",
                "text": {"content": "被引用的内容"}
            }
        }

        v10.0: 支持引用图片消息走多模态路径

        Args:
            data: 完整的消息数据
            message_text: 用户发送的文本

        Returns:
            tuple: (拼接引用上下文后的完整消息, content_blocks 或 None)
                   当 content_blocks 不为 None 时，调用方应走多模态路径
        """
        quote = data.get('quote')
        if not quote:
            return message_text, None

        quote_type = quote.get('msgtype', '')
        quote_content = ''

        if quote_type == 'text':
            quote_content = quote.get('text', {}).get('content', '')
        elif quote_type == 'image':
            # v10.0: 尝试下载解密引用图片
            content_blocks = await self._try_download_quote_image(quote)
            if content_blocks is not None:
                logger.info(f"📎 [引用消息] type=image, 走多模态路径")
                return message_text, content_blocks
            quote_content = '[引用了一张图片]'
        elif quote_type == 'file':
            # v12.0: 尝试下载解密引用的文件
            content_blocks = await self._try_download_quote_file(quote)
            if content_blocks is not None:
                logger.info(f"📎 [引用消息] type=file, 走文件多模态路径")
                return message_text, content_blocks
            quote_content = quote.get('file', {}).get('filename', '[引用了一个文件]')
        elif quote_type == 'voice':
            quote_content = quote.get('voice', {}).get('content', '[引用了一段语音]')
        elif quote_type == 'mixed':
            # v10.0: 尝试处理混合消息中的图片
            content_blocks = await self._try_download_mixed_quote_images(quote)
            if content_blocks is not None:
                logger.info(f"📎 [引用消息] type=mixed, 走多模态路径")
                return message_text, content_blocks
            # 降级：提取混合消息中的文本部分
            items = quote.get('mixed', {}).get('msg_item', [])
            parts = []
            for item in items:
                if item.get('msgtype') == 'text':
                    parts.append(item.get('text', {}).get('content', ''))
                elif item.get('msgtype') == 'image':
                    parts.append('[图片]')
            quote_content = ' '.join(parts) if parts else '[引用了一条消息]'
        else:
            quote_content = '[引用了一条消息]'

        if quote_content:
            logger.info(f"📎 [引用消息] type={quote_type}, content=\"{quote_content[:50]}\"")
            return f"[引用消息: {quote_content}]\n\n{message_text}", None

        return message_text, None

    def _can_do_multimodal(self) -> bool:
        """检查是否具备多模态处理能力"""
        if not self.encoding_aes_key:
            return False
        agent = getattr(self, 'agent', None)
        return agent is not None and hasattr(agent, 'handle_multimodal_message')

    async def _try_download_quote_image(self, quote: dict) -> Optional[List]:
        """尝试下载解密引用的图片，成功则返回 content_blocks，失败返回 None"""
        if not self._can_do_multimodal():
            return None

        image_url = quote.get('image', {}).get('url', '')
        if not image_url:
            return None

        try:
            data_uri = await ImageUtils.download_and_decrypt_to_base64(
                image_url, self.encoding_aes_key
            )
            return [
                {"type": "text", "text": "[引用了一张图片]"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        except Exception as e:
            logger.warning(f"[引用消息] 引用图片下载解密失败，降级为文本: {e}")
            return None

    async def _try_download_quote_file(self, quote: dict) -> Optional[List]:
        """尝试下载解密引用的文件，成功则返回 content_blocks，失败返回 None

        v12.0: 引用文件消息走文件多模态路径，复用 FileUtils 下载解密 + encode_for_relay。
        """
        if not self.encoding_aes_key:
            return None

        # 需要 agent 支持 handle_file_message 或 handle_multimodal_message
        agent = getattr(self, 'agent', None)
        if not agent or not (hasattr(agent, 'handle_file_message') or hasattr(agent, 'handle_multimodal_message')):
            return None

        file_info = quote.get('file', {})
        file_url = file_info.get('url', '')
        file_name = file_info.get('filename', '')
        if not file_url:
            return None

        try:
            file_bytes, header_filename = await FileUtils.download_and_decrypt(
                file_url, self.encoding_aes_key
            )

            # 确定最终文件名：引用数据 > 响应头 > 魔数检测
            if not file_name:
                file_name = header_filename
            if not file_name:
                file_name = FileUtils.detect_filename_from_bytes(file_bytes)

            # 扩展名白名单检查
            if not FileUtils.is_allowed(file_name):
                logger.warning(f"[引用消息] 引用文件类型不支持: {file_name}")
                return None

            file_data = FileUtils.encode_for_relay(file_bytes, file_name)
            logger.info(f"[引用消息] 引用文件下载解密成功: filename={file_name}, size={len(file_bytes)} bytes")

            return [
                {"type": "text", "text": f"[引用了文件: {file_name}]"},
                file_data,
            ]
        except Exception as e:
            logger.warning(f"[引用消息] 引用文件下载解密失败，降级为文本: {e}")
            return None

    async def _try_download_mixed_quote_images(self, quote: dict) -> Optional[List]:
        """尝试处理混合引用消息中的图片，成功则返回 content_blocks，失败返回 None"""
        if not self._can_do_multimodal():
            return None

        items = quote.get('mixed', {}).get('msg_item', [])
        if not items:
            return None

        # 检查是否包含图片
        has_image = any(item.get('msgtype') == 'image' for item in items)
        if not has_image:
            return None

        content_blocks = []
        for item in items:
            item_type = item.get('msgtype', '')
            if item_type == 'text':
                text_content = item.get('text', {}).get('content', '')
                if text_content:
                    content_blocks.append({"type": "text", "text": text_content})
            elif item_type == 'image':
                image_url = item.get('image', {}).get('url', '')
                if image_url:
                    try:
                        data_uri = await ImageUtils.download_and_decrypt_to_base64(
                            image_url, self.encoding_aes_key
                        )
                        content_blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
                    except Exception as e:
                        logger.warning(f"[引用消息] 混合引用中图片解密失败: {e}")
                        content_blocks.append({"type": "text", "text": "[图片处理失败]"})
                else:
                    content_blocks.append({"type": "text", "text": "[图片]"})

        return content_blocks if content_blocks else None

    async def _handle_with_agent(self, message: str, user_id: str, stream_id: str, response_url: str = "", session_key: str = "", log_context: dict = None) -> Optional[str]:
        """使用Agent模式处理消息(v2.0 - 完全异步版本)

        直接使用await调用异步方法,这是FastAPI的推荐做法
        v2.1: 传递stream_mgr以支持异步流式响应
        v3.0: 支持识别"确认"/"取消"关键词,处理待确认操作
        v3.2: 支持reset/new/clear命令重置会话
        v9.9: 支持response_url,超时后后台继续运行并主动推送结果
        v11.0: 支持 AskUserQuestion 的文本 fallback（用户直接回复文本作为答案）
        """
        try:
            logger.info(f"[Agent模式] 开始处理消息: bot={self.bot_key}, user={user_id}, session_key={session_key}, message_len={len(message)}")

            # 检查命令关键词
            normalized_msg = message.strip().lower()

            # v11.0: 检查是否有待处理的 AskUserQuestion 选择
            # 用户直接发文本 → 作为当前问题的自由文本答案（支持"其他"选项）
            choice_result = await self._handle_text_choice_answer(
                normalized_msg, user_id, stream_id
            )
            if choice_result is not None:
                return choice_result

            # v3.2: 检查是否是重置会话命令
            if normalized_msg in ["reset", "new", "clear", "重置", "清空"]:
                return await self._handle_reset_session(session_key or user_id, stream_id)

            # v12.0: 停止/取消正在运行的任务
            import re as _re
            _stop_msg = _re.sub(r'[^\w\u4e00-\u9fff]', '', normalized_msg)
            STOP_COMMANDS = ("stop", "停止", "暂停", "停")
            if _stop_msg in STOP_COMMANDS:
                return await self._handle_stop_task(session_key or user_id, stream_id)

            # v2.1: 传递stream_mgr给Agent
            if self.async_agent_enabled:
                logger.info("[Agent模式] 启动异步LLM任务处理")
                return await self._handle_with_agent_async(message, user_id, stream_id, response_url=response_url, session_key=session_key, log_context=log_context)

            logger.info(f"[Agent模式] 调用Agent.handle_text_message(同步)...")
            return await self._call_agent_sync(message, user_id, stream_id, session_key=session_key, log_context=log_context)

        except Exception as e:
            logger.error(f"❌ Agent处理失败: {e}", exc_info=True)
            # 快速返回错误消息，避免企业微信超时重试
            from src.utils.weixin_utils import MessageBuilder
            return MessageBuilder.text(
                stream_id,
                f"❌ 处理失败：{str(e)}\n\n请稍后再试或联系管理员。",
                finish=True
            )

    async def _call_agent_sync(self, message: str, user_id: str, stream_id: str, session_key: str = "", log_context: dict = None) -> Optional[str]:
        """同步模式调用Agent"""
        reply, is_stream = await self.agent.handle_text_message(
            user_id=user_id,
            message=message,
            stream_id=stream_id,
            _stream_mgr=self.global_stream_mgr,
            session_key=session_key,
            log_context=log_context,
        )
        logger.info(f"[Agent模式] Agent返回完成, reply_len={len(reply) if reply else 0}, is_stream={is_stream}")
        return reply

    async def _handle_with_agent_async(self, message: str, user_id: str, stream_id: str, response_url: str = "", session_key: str = "", log_context: dict = None) -> Optional[str]:
        """异步模式: 启用流式思考推送，后台等待LLM结果

        v3.0: 启用StreamingThinkingManager，在首次回复中返回finish=False触发流式刷新
        v9.9: 超时后若有response_url，任务继续运行并通过response_url主动推送结果
        """

        async def agent_job():
            logger.info(
                "[Agent模式] 异步任务开始: bot=%s, user=%s, timeout=%.1fs, message_len=%s",
                self.bot_key,
                user_id,
                self.agent_timeout_seconds,
                len(message)
            )

            # 创建内部任务（不再用 wait_for 包装，超时后任务不会被取消）
            inner_task = asyncio.create_task(
                self.agent.handle_text_message(
                    user_id=user_id,
                    message=message,
                    stream_id=stream_id,
                    _stream_mgr=self.global_stream_mgr,
                    session_key=session_key,
                    response_url=response_url,
                    log_context=log_context,
                )
            )

            # v12.0: 注册任务到全局注册表，支持用户停止
            from src.core.task_registry import get_task_registry
            get_task_registry().register(f"{self.bot_key}:{session_key}", inner_task, stream_id)

            # 两阶段等待：提前 20s 预警，告知用户即将切换后台运行
            pre_warning_seconds = 20
            main_timeout = max(self.agent_timeout_seconds - pre_warning_seconds, 0)

            # 第一阶段：等待主超时
            done, pending = await asyncio.wait(
                {inner_task}, timeout=main_timeout
            )

            if not done and response_url:
                # 未完成且有 response_url：追加预警提示
                from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                stm_early = get_streaming_thinking_manager()
                if stm_early.has_stream(stream_id):
                    stm_early.add_generating(
                        stream_id,
                        "⏳ 任务仍在处理中，即将切换为后台运行，完成后自动推送结果..."
                    )
                # 第二阶段：等待剩余时间
                done, pending = await asyncio.wait(
                    {inner_task}, timeout=pre_warning_seconds
                )

            if done:
                # 在超时内完成，检查异常
                try:
                    inner_task.result()
                    logger.info("[Agent模式] 异步任务在超时内完成: bot=%s, user=%s", self.bot_key, user_id)
                except Exception as exc:
                    logger.error(
                        "[Agent模式] 异步任务异常: bot=%s, user=%s, error=%s",
                        self.bot_key, user_id, exc, exc_info=True
                    )
                    from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                    stm = get_streaming_thinking_manager()
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, f"❌ 处理失败：{str(exc)}")
                return None

            # 超时，但 inner_task 仍在 pending 中运行
            logger.warning(
                "[Agent模式] 处理超时: bot=%s, user=%s, timeout=%.1fs, has_response_url=%s",
                self.bot_key, user_id, self.agent_timeout_seconds, bool(response_url)
            )
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()

            # 获取 session_url 用于超时消息
            session_url = stm.get_session_url(stream_id) if stm.has_stream(stream_id) else None
            url_hint = f"\n\n📎 查看实时执行过程：[链接>>]({session_url})" if session_url else ""

            if response_url:
                # 有 response_url：通知用户"稍后推送"，后台继续运行
                if stm.has_stream(stream_id):
                    stm.mark_complete(
                        stream_id,
                        f"⏳ 任务耗时较长，仍在后台运行中。完成后将自动推送结果，请留意消息通知。{url_hint}"
                    )
                # 启动后台等待协程：任务完成后 POST response_url
                asyncio.create_task(
                    _proactive_reply_on_complete(
                        inner_task, response_url, stream_id, user_id, self.bot_key
                    )
                )
                logger.info("[Agent模式] 已启动后台推送等待: stream=%s", stream_id)
            else:
                # 无 response_url：退化为当前行为（超时错误）
                if stm.has_stream(stream_id):
                    stm.mark_complete(stream_id, f"⏰ 处理超时，系统暂时无法完成该请求，请稍后再试。{url_hint}")
                inner_task.cancel()  # 无法投递结果，取消任务

            return None

        # v3.0: 启动异步任务，直接由StreamingThinkingManager管理思考过程
        asyncio.create_task(agent_job())
        logger.info(
            "[Agent模式] 已创建异步任务: bot=%s, user=%s, stream_id=%s",
            self.bot_key,
            user_id,
            stream_id
        )

        # v3.1: 返回空的流式消息，触发刷新机制
        # 所有思考内容都由StreamingThinkingManager在刷新时返回
        from src.utils.weixin_utils import MessageBuilder
        return MessageBuilder.text(
            stream_id,
            "",  # 空内容
            finish=False  # 触发流式刷新
        )

    async def _handle_with_agent_multimodal(
        self, content_blocks: list, user_id: str, stream_id: str, response_url: str = "", session_key: str = "",
        log_context: dict = None,
    ) -> Optional[str]:
        """v10.0: 引用图片走多模态路径，复用 ImageMessageHandler 的异步任务模式"""
        timeout = self.agent_timeout_seconds

        async def multimodal_job():
            logger.info(
                "[Agent多模态] 异步任务开始: bot=%s, user=%s, blocks=%d",
                self.bot_key, user_id, len(content_blocks)
            )

            inner_task = asyncio.create_task(
                self.agent.handle_multimodal_message(
                    user_id=user_id,
                    content_blocks=content_blocks,
                    stream_id=stream_id,
                    session_key=session_key,
                    log_context=log_context,
                )
            )

            # v12.0: 注册任务到全局注册表，支持用户停止
            from src.core.task_registry import get_task_registry
            get_task_registry().register(f"{self.bot_key}:{session_key}", inner_task, stream_id)

            done, _pending = await asyncio.wait(
                {inner_task}, timeout=timeout
            )

            if done:
                try:
                    inner_task.result()
                    logger.info("[Agent多模态] 异步任务完成: bot=%s, user=%s", self.bot_key, user_id)
                except Exception as exc:
                    logger.error(f"[Agent多模态] 异步任务异常: {exc}", exc_info=True)
                    from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                    stm = get_streaming_thinking_manager()
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, f"❌ 引用图片分析失败：{exc}")
                return None

            # 超时
            logger.warning("[Agent多模态] 处理超时: timeout=%.1fs, has_response_url=%s", timeout, bool(response_url))
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()

            session_url = stm.get_session_url(stream_id) if stm.has_stream(stream_id) else None
            url_hint = f"\n\n📎 查看实时执行过程：[链接>>]({session_url})" if session_url else ""

            if response_url:
                if stm.has_stream(stream_id):
                    stm.mark_complete(
                        stream_id,
                        f"⏳ 引用图片分析耗时较长，仍在后台运行中。完成后将自动推送结果，请留意消息通知。{url_hint}"
                    )
                asyncio.create_task(
                    _proactive_reply_on_complete(
                        inner_task, response_url, stream_id, user_id, self.bot_key
                    )
                )
            else:
                if stm.has_stream(stream_id):
                    stm.mark_complete(stream_id, f"⏰ 引用图片分析超时，请稍后再试。{url_hint}")
                inner_task.cancel()

            return None

        asyncio.create_task(multimodal_job())
        return MessageBuilder.text(stream_id, "", finish=False)

    async def _handle_text_choice_answer(
        self, message: str, user_id: str, stream_id: str
    ) -> Optional[str]:
        """检查并处理 AskUserQuestion 的文本回答

        当用户有 pending choice 时，文本消息作为当前问题的自由文本答案。
        支持"取消"命令放弃选择。

        Returns:
            Optional[str]: 处理了返回回复消息，无 pending choice 返回 None
        """
        from src.core.choice_manager import get_choice_manager
        from src.core.claude_relay_orchestrator import ClaudeRelayOrchestrator

        choice_mgr = get_choice_manager()

        if not choice_mgr.has_pending_choice(self.bot_key, user_id):
            return None

        # 用户发"取消"/"reset"等放弃选择
        import re as _re
        _cancel_msg = _re.sub(r'[^\w\u4e00-\u9fff]', '', message.strip().lower())
        cancel_keywords = ("取消", "cancel", "reset", "new", "clear", "重置", "清空", "stop", "停止", "暂停", "停")
        if _cancel_msg in cancel_keywords:
            choice_mgr.remove_session(self.bot_key, user_id)
            logger.info(f"[Choice] 用户取消选择: bot={self.bot_key}, user={user_id}, cmd={message}")
            return MessageBuilder.text(stream_id, "已取消选择。", finish=True)

        session = choice_mgr.get_session(self.bot_key, user_id)
        if not session:
            return None

        # 将文本作为当前问题的答案
        logger.info(
            f"[Choice] 文本答案: bot={self.bot_key}, user={user_id}, "
            f"question_index={session.current_index}, answer={message[:50]}"
        )
        result = choice_mgr.record_answer(self.bot_key, user_id, message)

        if not result["done"]:
            # 还有下一题：构建 vote 卡片作为回复
            next_question = result["next_question"]
            next_index = result["next_index"]
            total = result["total"]
            next_card = ClaudeRelayOrchestrator._build_vote_card(
                session.task_id_prefix, next_question, next_index, total
            )
            return MessageBuilder.stream_with_card(
                stream_id, f"已记录您的回答。请继续回答下一个问题：",
                finish=True, template_card=next_card,
            )
        else:
            # 全部完成，触发提交
            asyncio.create_task(
                EventMessageHandler(bot_key=self.bot_key)._submit_answers_async(
                    self.bot_key, user_id
                )
            )
            return MessageBuilder.text(
                stream_id,
                "⏳ 已收到所有回答，正在处理中...",
                finish=True,
            )

    async def _handle_reset_session(self, session_key: str, stream_id: str) -> Optional[str]:
        """处理会话重置命令(v3.2)

        清空会话的对话上下文,开始新的会话。
        群聊时 session_key = chatid（清空整个群的会话），
        单聊时 session_key = user_id。

        Args:
            session_key: 会话key（群聊=chatid，单聊=user_id）
            stream_id: 消息ID

        Returns:
            Optional[str]: 回复消息JSON
        """
        from src.core.session_manager import SessionManager
        from src.utils.weixin_utils import MessageBuilder

        try:
            session_mgr = SessionManager()
            await session_mgr.clear_session(self.bot_key, session_key)

            logger.info(f"✅ 会话已重置: bot={self.bot_key}, session_key={session_key}")

            return MessageBuilder.text(
                stream_id,
                "✅ 会话已重置，让我们重新开始吧！",
                finish=True
            )

        except Exception as e:
            logger.error(f"重置会话失败: {e}", exc_info=True)
            return MessageBuilder.text(
                stream_id,
                f"❌ 重置会话失败: {str(e)}",
                finish=True
            )

    async def _handle_stop_task(self, session_key: str, stream_id: str) -> Optional[str]:
        """v12.0: 停止当前正在运行的 Agent 任务

        只停止任务，保留会话（不 reset session）。
        """
        from src.core.task_registry import get_task_registry
        from src.core.streaming_thinking_manager import get_streaming_thinking_manager

        registry = get_task_registry()
        key = f"{self.bot_key}:{session_key}"
        cancelled, old_stream_id = registry.cancel(key)

        if cancelled:
            stm = get_streaming_thinking_manager()
            if old_stream_id and stm.has_stream(old_stream_id):
                stm.mark_complete(old_stream_id, "⏹️ 任务已被用户停止。")
            logger.info(f"⏹️ 用户停止任务: bot={self.bot_key}, session_key={session_key}")
            return MessageBuilder.text(stream_id, "⏹️ 已停止当前任务。", finish=True)
        else:
            return MessageBuilder.text(stream_id, "当前没有正在运行的任务。", finish=True)

class VoiceMessageHandler(MessageHandler):
    """语音消息处理器

    企业微信智能机器人会自动将语音转为文本,
    voice.content 即为语音识别后的文本内容。
    直接复用 TextMessageHandler 的处理逻辑。
    """

    def __init__(self, text_handler: TextMessageHandler):
        self.text_handler = text_handler

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        voice_content = data.get('voice', {}).get('content', '')
        user_id = data.get('from', {}).get('userid', '')

        logger.info(f"🎤 [语音消息] user={user_id}, content=\"{voice_content[:50]}\"")

        if not voice_content.strip():
            return MessageBuilder.text(stream_id, "未能识别语音内容,请重试。", finish=True)

        # 将语音转文本结果包装为 text 格式,复用 TextMessageHandler
        data['text'] = {'content': voice_content}
        data['_original_msgtype'] = 'voice'  # 标记原始消息类型，供日志记录使用
        # 语音消息也可能带有引用
        return await self.text_handler.handle(data, stream_id)


class FileMessageHandler(MessageHandler):
    """文件消息处理器

    v10.0: 支持下载解密企业微信文件，转发给 clawrelay-api（Claude）分析
    降级策略：无URL / 无key / 扩展名不允许 / agent不支持 → 纯文本描述交给 TextHandler
    """

    def __init__(self, text_handler: TextMessageHandler, encoding_aes_key: str = ""):
        self.text_handler = text_handler
        self.encoding_aes_key = encoding_aes_key

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        file_url = data.get('file', {}).get('url', '')
        file_name = data.get('file', {}).get('filename', '')  # 企业微信AI Bot可能不提供filename
        user_id = data.get('from', {}).get('userid', '')
        session_key = _get_session_key(data)
        response_url = data.get('response_url', '')

        logger.info(
            f"📄 [文件消息] user={user_id}, filename={file_name or '(空)'}, "
            f"has_url={bool(file_url)}, session_key={session_key}"
        )

        # 前置检查：无 URL 或无 key → 降级文本
        if not file_url or not self.encoding_aes_key:
            return await self._fallback_text(data, file_name or '未知文件', stream_id)

        # 检查 agent 是否支持 handle_file_message
        agent = getattr(self.text_handler, 'agent', None)
        if not agent or not hasattr(agent, 'handle_file_message'):
            return await self._fallback_text(data, file_name or '未知文件', stream_id)

        # 先下载解密文件（文件名可能需要从响应头或文件内容检测）
        try:
            file_bytes, header_filename = await FileUtils.download_and_decrypt(
                file_url, self.encoding_aes_key
            )
        except Exception as e:
            logger.warning(f"[文件消息] 文件下载解密失败，降级为文本: {e}")
            return await self._fallback_text(
                data, file_name or '未知文件', stream_id,
                note=f"文件下载失败: {e}"
            )

        # 确定最终文件名：回调数据 > 响应头 > 魔数检测
        if not file_name:
            file_name = header_filename
        if not file_name:
            file_name = FileUtils.detect_filename_from_bytes(file_bytes)
        logger.info(f"📄 [文件消息] 最终文件名: {file_name}")

        # 扩展名白名单检查（已有文件名后才能准确判断）
        if not FileUtils.is_allowed(file_name):
            return await self._fallback_text(
                data, file_name, stream_id,
                note=f"不支持的文件类型({file_name})，将以文本方式处理"
            )

        # 编码文件数据（OpenAI content part 格式）
        file_data = FileUtils.encode_for_relay(file_bytes, file_name)

        # 构建文件日志上下文
        chattype = data.get('chattype', 'single')
        log_context = {
            'chat_type': chattype,
            'chat_id': data.get('chatid', ''),
            'message_type': 'file',
            'file_info': {'filename': file_name, 'size': len(file_bytes)},
        }

        # v10.1 场景A: 检查是否有已缓冲的文字（文字先到，文件后到）
        pending_text = _file_merge_buffer.try_get_pending_text(session_key)
        if pending_text:
            message = f"[用户发送了文件: {file_name}] {pending_text['text']}"
            text_stream_id = pending_text['stream_id']
            logger.info(
                f"📎 [文件合并-A] 文件到达，合并已缓冲文字: filename={file_name}, "
                f"text_stream_id={text_stream_id}"
            )
            # 使用文字消息的 stream_id 处理（文字已返回初始流式响应）
            # 文件消息的回调返回 None（不产生第二条回复）
            asyncio.create_task(self._run_file_agent(
                agent, user_id, message, [file_data], text_stream_id,
                response_url=response_url, session_key=session_key,
                log_context=log_context,
            ))
            return None

        # v10.1 场景B: 文件先到，缓冲等文字
        _file_merge_buffer.add_file(session_key, file_data)

        return await self._handle_file_with_merge(
            agent, user_id, file_name, [file_data], stream_id,
            response_url=response_url, session_key=session_key,
            log_context=log_context,
        )

    async def _run_file_agent(
        self, agent, user_id: str, message: str, files: list,
        stream_id: str, response_url: str = "", session_key: str = "",
        log_context: dict = None,
    ):
        """直接运行 agent 文件处理（场景A 用，文字的 stream_id 已有初始响应）"""
        timeout = self.text_handler.agent_timeout_seconds
        bot_key = self.text_handler.bot_key
        try:
            inner_task = asyncio.create_task(
                agent.handle_file_message(
                    user_id=user_id, message=message, files=files,
                    stream_id=stream_id, session_key=session_key,
                    log_context=log_context,
                )
            )

            # v12.0: 注册任务到全局注册表，支持用户停止
            from src.core.task_registry import get_task_registry
            get_task_registry().register(f"{bot_key}:{session_key}", inner_task, stream_id)

            done, _ = await asyncio.wait({inner_task}, timeout=timeout)
            if done:
                inner_task.result()
            else:
                from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                stm = get_streaming_thinking_manager()
                if response_url:
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, "⏳ 文件分析耗时较长，完成后将自动推送结果。")
                    asyncio.create_task(
                        _proactive_reply_on_complete(inner_task, response_url, stream_id, user_id, bot_key)
                    )
                else:
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, "⏰ 文件分析超时，请稍后再试。")
                    inner_task.cancel()
        except Exception as e:
            logger.error(f"[文件合并-A] agent 处理异常: {e}", exc_info=True)
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()
            if stm.has_stream(stream_id):
                stm.mark_complete(stream_id, f"❌ 文件分析失败：{e}")

    async def _fallback_text(
        self, data: dict, file_name: str, stream_id: str, note: str = ""
    ) -> Optional[str]:
        """降级为纯文本处理"""
        file_text = f"[用户发送了文件: {file_name}]"
        if note:
            file_text += f"\n({note})"
            logger.info(f"[文件消息] 降级为文本: {note}")

        data['text'] = {'content': file_text}
        return await self.text_handler.handle(data, stream_id)

    async def _handle_file_with_merge(
        self, agent, user_id: str, file_name: str, files: list,
        stream_id: str, response_url: str = "", session_key: str = "",
        log_context: dict = None,
    ) -> Optional[str]:
        """异步模式处理文件消息，含文本合并等待

        在异步任务内先等待文本消息合并（最多 10s），然后调用 agent 处理。
        handle() 立即返回初始流式响应，不阻塞企业微信回调。
        """
        timeout = self.text_handler.agent_timeout_seconds
        bot_key = self.text_handler.bot_key

        async def file_job():
            # 第一阶段：等待文本消息合并（最多 10s）
            merged_text = await _file_merge_buffer.wait_for_text(session_key)
            if merged_text:
                message = f"[用户发送了文件: {file_name}] {merged_text}"
            else:
                message = f"[用户发送了文件: {file_name}] 请分析这个文件的内容。"
            logger.info(f"📎 [文件消息] 合并完成: filename={file_name}, merged={bool(merged_text)}")

            # 第二阶段：调用 agent 处理
            inner_task = asyncio.create_task(
                agent.handle_file_message(
                    user_id=user_id,
                    message=message,
                    files=files,
                    stream_id=stream_id,
                    session_key=session_key,
                    log_context=log_context,
                )
            )

            # v12.0: 注册任务到全局注册表，支持用户停止
            from src.core.task_registry import get_task_registry
            get_task_registry().register(f"{bot_key}:{session_key}", inner_task, stream_id)

            done, _pending = await asyncio.wait(
                {inner_task}, timeout=timeout
            )

            if done:
                try:
                    inner_task.result()
                except Exception as exc:
                    logger.error(f"[文件消息] 异步任务异常: {exc}", exc_info=True)
                    from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                    stm = get_streaming_thinking_manager()
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, f"❌ 文件分析失败：{exc}")
                return None

            # 超时
            logger.warning(
                "[文件消息] 处理超时: timeout=%.1fs, has_response_url=%s",
                timeout, bool(response_url)
            )
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()

            session_url = stm.get_session_url(stream_id) if stm.has_stream(stream_id) else None
            url_hint = f"\n\n📎 查看实时执行过程：[链接>>]({session_url})" if session_url else ""

            if response_url:
                if stm.has_stream(stream_id):
                    stm.mark_complete(
                        stream_id,
                        f"⏳ 文件分析耗时较长，仍在后台运行中。完成后将自动推送结果，请留意消息通知。{url_hint}"
                    )
                asyncio.create_task(
                    _proactive_reply_on_complete(
                        inner_task, response_url, stream_id, user_id, bot_key
                    )
                )
            else:
                if stm.has_stream(stream_id):
                    stm.mark_complete(stream_id, f"⏰ 文件分析超时，请稍后再试。{url_hint}")
                inner_task.cancel()

            return None

        asyncio.create_task(file_job())

        return MessageBuilder.text(stream_id, "", finish=False)


class ImageMessageHandler(MessageHandler):
    """图片消息处理器

    v8.0: 支持下载解密企业微信图片，转发给 clawrelay-api（Claude）分析
    降级策略：解密失败 → 纯文本描述交给 TextHandler
    """

    def __init__(self, text_handler: TextMessageHandler, encoding_aes_key: str = ""):
        self.text_handler = text_handler
        self.encoding_aes_key = encoding_aes_key

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        image_url = data.get('image', {}).get('url', '')
        user_id = data.get('from', {}).get('userid', '')

        # 群聊会话隔离：提取 session_key
        session_key = _get_session_key(data)

        logger.info(f"🖼️ [图片消息] user={user_id}, session_key={session_key}, has_url={bool(image_url)}")

        # 检查是否有 encoding_aes_key 和图片 URL
        if not image_url or not self.encoding_aes_key:
            logger.info("[图片消息] 缺少 URL 或 encoding_aes_key，降级为文本")
            data['text'] = {'content': '[用户发送了一张图片]'}
            return await self.text_handler.handle(data, stream_id)

        # 检查 agent 是否支持多模态（仅 claude_relay 类型）
        agent = getattr(self.text_handler, 'agent', None)
        if not agent or not hasattr(agent, 'handle_multimodal_message'):
            logger.info("[图片消息] Agent 不支持多模态，降级为文本")
            data['text'] = {'content': '[用户发送了一张图片]'}
            return await self.text_handler.handle(data, stream_id)

        # 尝试下载解密图片
        try:
            data_uri = await ImageUtils.download_and_decrypt_to_base64(
                image_url, self.encoding_aes_key
            )
        except Exception as e:
            logger.warning(f"[图片消息] 图片解密失败，降级为文本: {e}")
            data['text'] = {'content': f'[用户发送了一张图片，但处理失败: {e}]'}
            return await self.text_handler.handle(data, stream_id)

        # v9.9: 提取 response_url
        response_url = data.get('response_url', '')
        chattype = data.get('chattype', 'single')

        log_context = {
            'chat_type': chattype,
            'chat_id': data.get('chatid', ''),
            'message_type': 'image',
        }

        # v10.2 场景A: 检查是否有已缓冲的文字（文字先到，图片后到）
        pending_text = _file_merge_buffer.try_get_pending_text(session_key)
        if pending_text:
            content_blocks = [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": pending_text['text']},
            ]
            text_stream_id = pending_text['stream_id']
            logger.info(
                f"🖼️ [图片合并-A] 图片到达，合并已缓冲文字: "
                f"text_stream_id={text_stream_id}"
            )
            asyncio.create_task(self._run_image_agent(
                agent, user_id, content_blocks, text_stream_id,
                response_url=response_url, session_key=session_key,
                log_context=log_context,
            ))
            return None

        # v10.2 场景B: 图片先到，缓冲等文字
        _file_merge_buffer.add_image(session_key, data_uri)

        return await self._handle_image_with_merge(
            agent, user_id, data_uri, stream_id,
            response_url=response_url, session_key=session_key,
            log_context=log_context,
        )

    async def _handle_multimodal_async(
        self, agent, user_id: str, content_blocks: list, stream_id: str,
        response_url: str = "", session_key: str = "",
        log_context: dict = None,
    ) -> Optional[str]:
        """异步模式处理多模态消息，与 TextHandler 的异步任务模式一致

        v9.9: 支持 response_url 后台推送 + 修复 timeout 使用 text_handler 的统一配置
        """
        timeout = self.text_handler.agent_timeout_seconds
        bot_key = self.text_handler.bot_key

        async def multimodal_job():
            # 创建内部任务（不再用 wait_for 包装）
            inner_task = asyncio.create_task(
                agent.handle_multimodal_message(
                    user_id=user_id,
                    content_blocks=content_blocks,
                    stream_id=stream_id,
                    session_key=session_key,
                    log_context=log_context,
                )
            )

            done, _pending = await asyncio.wait(
                {inner_task}, timeout=timeout
            )

            if done:
                try:
                    inner_task.result()
                except Exception as exc:
                    logger.error(f"[图片消息] 异步任务异常: {exc}", exc_info=True)
                    from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                    stm = get_streaming_thinking_manager()
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, f"❌ 图片分析失败：{exc}")
                return None

            # 超时
            logger.warning("[图片消息] 处理超时: timeout=%.1fs, has_response_url=%s", timeout, bool(response_url))
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()

            session_url = stm.get_session_url(stream_id) if stm.has_stream(stream_id) else None
            url_hint = f"\n\n📎 查看实时执行过程：[链接>>]({session_url})" if session_url else ""

            if response_url:
                if stm.has_stream(stream_id):
                    stm.mark_complete(
                        stream_id,
                        f"⏳ 图片分析耗时较长，仍在后台运行中。完成后将自动推送结果，请留意消息通知。{url_hint}"
                    )
                asyncio.create_task(
                    _proactive_reply_on_complete(
                        inner_task, response_url, stream_id, user_id, bot_key
                    )
                )
            else:
                if stm.has_stream(stream_id):
                    stm.mark_complete(stream_id, f"⏰ 图片分析超时，请稍后再试。{url_hint}")
                inner_task.cancel()

            return None

        asyncio.create_task(multimodal_job())

        return MessageBuilder.text(stream_id, "", finish=False)

    async def _run_image_agent(
        self, agent, user_id: str, content_blocks: list,
        stream_id: str, response_url: str = "", session_key: str = "",
        log_context: dict = None,
    ):
        """场景A: 文字已返回初始响应，图片到达后合并处理（使用文字的 stream_id）"""
        timeout = self.text_handler.agent_timeout_seconds
        bot_key = self.text_handler.bot_key
        try:
            inner_task = asyncio.create_task(
                agent.handle_multimodal_message(
                    user_id=user_id,
                    content_blocks=content_blocks,
                    stream_id=stream_id,
                    session_key=session_key,
                    log_context=log_context,
                )
            )

            from src.core.task_registry import get_task_registry
            get_task_registry().register(f"{bot_key}:{session_key}", inner_task, stream_id)

            done, _ = await asyncio.wait({inner_task}, timeout=timeout)
            if done:
                inner_task.result()
            else:
                from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                stm = get_streaming_thinking_manager()
                if response_url:
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, "⏳ 图片分析耗时较长，完成后将自动推送结果。")
                    asyncio.create_task(
                        _proactive_reply_on_complete(inner_task, response_url, stream_id, user_id, bot_key)
                    )
                else:
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, "⏰ 图片分析超时，请稍后再试。")
                    inner_task.cancel()
        except Exception as e:
            logger.error(f"[图片合并-A] agent 处理异常: {e}", exc_info=True)
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()
            if stm.has_stream(stream_id):
                stm.mark_complete(stream_id, f"❌ 图片分析失败：{e}")

    async def _handle_image_with_merge(
        self, agent, user_id: str, data_uri: str,
        stream_id: str, response_url: str = "", session_key: str = "",
        log_context: dict = None,
    ) -> Optional[str]:
        """场景B: 图片先到，缓冲等待文字合并后再处理

        立即返回初始流式响应，后台等待文字到达（最多10s），
        合并后走多模态处理。超时则用默认提示词。
        """
        timeout = self.text_handler.agent_timeout_seconds
        bot_key = self.text_handler.bot_key

        async def image_merge_job():
            merged_text = await _file_merge_buffer.wait_for_text_for_image(session_key)
            if merged_text:
                user_text = merged_text
                logger.info(f"🖼️ [图片合并-B] 合并完成: merged_text=\"{merged_text[:50]}\"")
            else:
                user_text = "用户发送了这张图片，请描述或分析图片内容。"
                logger.info(f"🖼️ [图片合并-B] 等待文字超时，使用默认提示词")

            content_blocks = [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": user_text},
            ]

            inner_task = asyncio.create_task(
                agent.handle_multimodal_message(
                    user_id=user_id,
                    content_blocks=content_blocks,
                    stream_id=stream_id,
                    session_key=session_key,
                    log_context=log_context,
                )
            )

            from src.core.task_registry import get_task_registry
            get_task_registry().register(f"{bot_key}:{session_key}", inner_task, stream_id)

            done, _pending = await asyncio.wait({inner_task}, timeout=timeout)

            if done:
                try:
                    inner_task.result()
                except Exception as exc:
                    logger.error(f"[图片合并-B] 异步任务异常: {exc}", exc_info=True)
                    from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                    stm = get_streaming_thinking_manager()
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, f"❌ 图片分析失败：{exc}")
                return None

            # 超时
            logger.warning("[图片合并-B] 处理超时: timeout=%.1fs", timeout)
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()

            session_url = stm.get_session_url(stream_id) if stm.has_stream(stream_id) else None
            url_hint = f"\n\n📎 查看实时执行过程：[链接>>]({session_url})" if session_url else ""

            if response_url:
                if stm.has_stream(stream_id):
                    stm.mark_complete(
                        stream_id,
                        f"⏳ 图片分析耗时较长，仍在后台运行中。完成后将自动推送结果，请留意消息通知。{url_hint}"
                    )
                asyncio.create_task(
                    _proactive_reply_on_complete(
                        inner_task, response_url, stream_id, user_id, bot_key
                    )
                )
            else:
                if stm.has_stream(stream_id):
                    stm.mark_complete(stream_id, f"⏰ 图片分析超时，请稍后再试。{url_hint}")
                inner_task.cancel()

            return None

        asyncio.create_task(image_merge_job())

        return MessageBuilder.text(stream_id, "", finish=False)


class MixedMessageHandler(MessageHandler):
    """图文混排消息处理器

    v8.0: 解析 msg_item 数组，文本 → text block，图片 → 下载解密 → image_url block
    如果只有文本（无图片），直接走 TextHandler
    """

    def __init__(self, text_handler: TextMessageHandler, encoding_aes_key: str = ""):
        self.text_handler = text_handler
        self.encoding_aes_key = encoding_aes_key

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        msg_items = data.get('mixed', {}).get('msg_item', [])
        user_id = data.get('from', {}).get('userid', '')

        # 群聊会话隔离：提取 session_key
        session_key = _get_session_key(data)

        logger.info(f"📎 [图文混排] user={user_id}, session_key={session_key}, items={len(msg_items)}")

        if not msg_items:
            return MessageBuilder.text(stream_id, "收到空的图文消息。", finish=True)

        # 检查 agent 是否支持多模态
        agent = getattr(self.text_handler, 'agent', None)
        has_multimodal = agent and hasattr(agent, 'handle_multimodal_message')

        content_blocks = []
        has_image = False
        text_parts = []

        for item in msg_items:
            item_type = item.get('msgtype', '')

            if item_type == 'text':
                text_content = item.get('text', {}).get('content', '')
                if text_content:
                    content_blocks.append({"type": "text", "text": text_content})
                    text_parts.append(text_content)

            elif item_type == 'image':
                img_url = item.get('image', {}).get('url', '')
                if img_url and self.encoding_aes_key and has_multimodal:
                    try:
                        data_uri = await ImageUtils.download_and_decrypt_to_base64(
                            img_url, self.encoding_aes_key
                        )
                        content_blocks.append({
                            "type": "image_url",
                            "image_url": {"url": data_uri},
                        })
                        has_image = True
                    except Exception as e:
                        logger.warning(f"[图文混排] 图片解密失败: {e}")
                        content_blocks.append({
                            "type": "text",
                            "text": "[图片处理失败]",
                        })
                        text_parts.append("[图片处理失败]")
                else:
                    content_blocks.append({"type": "text", "text": "[图片]"})
                    text_parts.append("[图片]")

        # 如果没有图片或 Agent 不支持多模态，降级为纯文本
        if not has_image or not has_multimodal:
            combined_text = "\n".join(text_parts) if text_parts else "[图文消息]"
            logger.info(f"[图文混排] 无图片或不支持多模态，降级为文本: {combined_text[:50]}")
            data['text'] = {'content': combined_text}
            return await self.text_handler.handle(data, stream_id)

        # 有图片，走多模态处理
        logger.info(f"[图文混排] 走多模态处理: blocks={len(content_blocks)}")

        # v9.9: 提取 response_url
        response_url = data.get('response_url', '')
        timeout = self.text_handler.agent_timeout_seconds
        bot_key = self.text_handler.bot_key
        chattype = data.get('chattype', 'single')

        log_context = {
            'chat_type': chattype,
            'chat_id': data.get('chatid', ''),
            'message_type': 'mixed',
        }

        async def multimodal_job():
            # 创建内部任务（不再用 wait_for 包装）
            inner_task = asyncio.create_task(
                agent.handle_multimodal_message(
                    user_id=user_id,
                    content_blocks=content_blocks,
                    stream_id=stream_id,
                    session_key=session_key,
                    log_context=log_context,
                )
            )

            done, _pending = await asyncio.wait(
                {inner_task}, timeout=timeout
            )

            if done:
                try:
                    inner_task.result()
                except Exception as exc:
                    logger.error(f"[图文混排] 异步任务异常: {exc}", exc_info=True)
                    from src.core.streaming_thinking_manager import get_streaming_thinking_manager
                    stm = get_streaming_thinking_manager()
                    if stm.has_stream(stream_id):
                        stm.mark_complete(stream_id, f"❌ 图片分析失败：{exc}")
                return None

            # 超时
            logger.warning("[图文混排] 处理超时: timeout=%.1fs, has_response_url=%s", timeout, bool(response_url))
            from src.core.streaming_thinking_manager import get_streaming_thinking_manager
            stm = get_streaming_thinking_manager()

            session_url = stm.get_session_url(stream_id) if stm.has_stream(stream_id) else None
            url_hint = f"\n\n📎 查看实时执行过程：[链接>>]({session_url})" if session_url else ""

            if response_url:
                if stm.has_stream(stream_id):
                    stm.mark_complete(
                        stream_id,
                        f"⏳ 任务耗时较长，仍在后台运行中。完成后将自动推送结果，请留意消息通知。{url_hint}"
                    )
                asyncio.create_task(
                    _proactive_reply_on_complete(
                        inner_task, response_url, stream_id, user_id, bot_key
                    )
                )
            else:
                if stm.has_stream(stream_id):
                    stm.mark_complete(stream_id, f"⏰ 图片分析超时，请稍后再试。{url_hint}")
                inner_task.cancel()

            return None

        asyncio.create_task(multimodal_job())

        return MessageBuilder.text(stream_id, "", finish=False)


class StreamMessageHandler(MessageHandler):
    """流式消息处理器

    v3.0: 支持StreamingThinkingManager实时展示思考过程
    v2.1: 支持检查异步LLM任务状态
    v3.5: 支持保存确认信息到会话上下文
    """

    def __init__(self, stream_mgr: StreamManager, bot_key: str = "default"):
        self.stream_mgr = stream_mgr
        self.bot_key = bot_key

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        real_stream_id = data.get('stream', {}).get('id', '')
        logger.info("收到流式消息刷新请求, stream_id=%s", real_stream_id)
        logger.info("当前StreamManager中的stream_ids=%s",
                   list(self.stream_mgr.stream_states.keys()))

        # v3.0: 优先检查是否有StreamingThinkingManager的思考过程
        from src.core.streaming_thinking_manager import get_streaming_thinking_manager
        streaming_thinking_mgr = get_streaming_thinking_manager()

        if streaming_thinking_mgr.has_stream(real_stream_id):
            # 获取当前思考内容
            thinking_content, is_complete = streaming_thinking_mgr.get_current_thinking(
                real_stream_id,
                include_final_answer=True
            )

            logger.info(
                f"[流式刷新] StreamingThinking: stream={real_stream_id}, "
                f"is_complete={is_complete}, content_len={len(thinking_content)}"
            )

            if is_complete:
                # 检查是否有附带的模板卡片（AskUserQuestion 场景）
                pending_card = streaming_thinking_mgr.get_pending_card(real_stream_id)
                # 思考过程完成，清理状态并返回最终内容
                streaming_thinking_mgr.clear_stream(real_stream_id)
                if pending_card:
                    return MessageBuilder.stream_with_card(
                        real_stream_id, thinking_content, finish=True,
                        template_card=pending_card,
                    )
                return MessageBuilder.text(real_stream_id, thinking_content, finish=True)
            else:
                # 继续刷新，展示当前思考进度
                return MessageBuilder.text(real_stream_id, thinking_content, finish=False)

        # v2.1: 检查是否有LLM异步任务
        llm_result = self.stream_mgr.get_llm_result(real_stream_id)
        if llm_result:
            logger.info(f"[流式刷新] 检测到LLM任务: stream={real_stream_id}, done={llm_result['done']}")

            if llm_result['done']:
                # LLM任务完成
                if llm_result['error']:
                    # 任务出错
                    error_msg = f"❌ 处理失败: {llm_result['error']}"
                    self.stream_mgr.clear_llm_task(real_stream_id)
                    logger.error(f"[流式刷新] LLM任务出错: {llm_result['error']}")
                    return MessageBuilder.text(real_stream_id, error_msg, finish=True)
                else:
                    # 任务成功,返回最终结果
                    result = llm_result['result']
                    if isinstance(result, bytes):
                        result = result.decode("utf-8", errors="ignore")
                    elif result is None:
                        result = ""
                    elif not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False)

                    self.stream_mgr.update_llm_status(real_stream_id, "AI已生成回复")
                    self.stream_mgr.clear_llm_task(real_stream_id)

                    logger.info(f"[流式刷新] LLM任务完成,返回最终结果")

                    # 根据返回内容选择最终消息
                    trimmed = result.strip()
                    if trimmed.startswith("{"):
                        try:
                            parsed = json.loads(trimmed)
                        except json.JSONDecodeError:
                            logger.debug("[流式刷新] LLM结果不是合法JSON,按文本处理")
                        else:
                            if parsed.get("msgtype"):
                                logger.info("[流式刷新] 返回LLM生成的JSON消息")
                                return trimmed

                    if not trimmed:
                        logger.warning("[流式刷新] LLM返回空字符串,使用默认提示")
                        return MessageBuilder.text(real_stream_id, "处理完成，但未生成内容。", finish=True)

                    return MessageBuilder.text(real_stream_id, trimmed, finish=True)
            else:
                # LLM任务还在处理中,返回进度提示
                status = llm_result['status']
                logger.info(f"[流式刷新] LLM任务进行中: {status}")
                return MessageBuilder.text(
                    real_stream_id,
                    f"🤔 {status}",
                    finish=False  # 继续等待
                )

        # 原有逻辑: 检查是否有对应的流式消息状态
        if not self.stream_mgr.has_stream(real_stream_id):
            logger.info("未找到 stream_id=%s 的状态,可能已完成,返回None", real_stream_id)
            # v2.6: 不返回任何消息,避免显示"流式消息已完成"
            return None

        # 获取状态
        state = self.stream_mgr.get_state(real_stream_id)
        with_image = state.get('with_image', False)

        # 获取下一段内容
        content, is_last = self.stream_mgr.get_next_content(real_stream_id)

        logger.info("流式消息继续, finish=%s", is_last)

        # 如果是最后一段且需要附带图片
        if is_last and with_image:
            # Demo mode: use fallback image (configure a real URL for production)
            image_base64, image_md5 = ImageUtils.get_fallback_image()

            return MessageBuilder.image(real_stream_id, image_base64, image_md5, content)
        else:
            # 使用流式消息+模板卡片回复(只包含stream,不再包含template_card)
            return MessageBuilder.stream_with_card(real_stream_id, content, finish=is_last)


class EventMessageHandler(MessageHandler):
    """事件消息处理器"""

    def __init__(self, bot_key: str = "default"):
        """初始化事件处理器

        Args:
            bot_key: 机器人标识
        """
        self.bot_key = bot_key

    async def handle(self, data: dict, stream_id: str) -> Optional[str]:
        event_type = data.get('event', {}).get('eventtype', '')
        logger.info("收到事件消息, 事件类型: %s", event_type)

        # 进入会话事件：不发欢迎语
        if event_type == 'enter_chat':
            return None
        # 处理模板卡片事件
        elif event_type == 'template_card_event':
            return await self._handle_template_card_event(data, stream_id)
        else:
            logger.info("未处理的事件类型: %s", event_type)
            return None

    def _handle_enter_chat(self, data: dict) -> str:
        """处理进入会话事件"""
        user_id = data.get('from', {}).get('userid', '')
        user_name = get_user_name_by_wework_user_id(user_id) or "朋友"

        template_card = {
            "card_type": "text_notice",
            "source": {
                "icon_url": "",
                "desc": "企业微信机器人",
                "desc_color": 1
            },
            "main_title": {
                "title": f"👋 欢迎 {user_name}",
                "desc": "很高兴为您服务"
            },
            "quote_area": {
                "type": 1,
                "url": "https://work.weixin.qq.com",
                "title": "💡 快速开始",
                "quote_text": "发送 help 查看所有可用命令\n随时为您效劳!"
            },
            "sub_title_text": "我是您的智能助手,有任何问题都可以问我！",
            "jump_list": [
                {"type": 3, "title": "📖 查看帮助", "question": "help"},
                {"type": 1, "url": "https://work.weixin.qq.com", "title": "🔗 访问官网"}
            ],
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
            "task_id": f"enter_chat_{user_id}"
        }

        return MessageBuilder.template_card(template_card)

    async def _handle_template_card_event(self, data: dict, stream_id: str) -> str:
        """处理模板卡片事件

        v3.0: 已废弃确认按钮卡片,改为流式消息确认
        v11.0: 支持 AskUserQuestion 的 vote_interaction 选择事件
        保留此方法仅用于处理其他类型的模板卡片事件

        Args:
            data: 事件数据
            stream_id: 消息ID

        Returns:
            str: 响应消息JSON
        """
        template_card_event = data.get('event', {}).get('template_card_event', {})
        card_type = template_card_event.get('card_type', '')
        event_key = template_card_event.get('event_key', '')
        task_id = template_card_event.get('task_id', '')
        user_id = data.get('from', {}).get('userid', '')

        # v11.0: 检查是否是 AskUserQuestion 的 choice 事件
        if task_id.startswith("choice@"):
            return await self._handle_choice_event(data, template_card_event, task_id, user_id)

        logger.info(
            "收到模板卡片事件: card_type=%s, event_key=%s, task_id=%s, user_id=%s",
            card_type, event_key, task_id, user_id
        )

        # v3.0: 确认操作已改为文本消息交互,不再使用按钮卡片
        # 如果用户仍然点击了旧的确认按钮,提示使用新方式
        if event_key in ["confirm", "cancel"]:
            logger.warning(
                f"用户点击了旧的确认按钮: event_key={event_key}, "
                f"但确认功能已改为文本消息交互"
            )

            update_card = {
                "card_type": "text_notice",
                "source": {
                    "icon_url": "",
                    "desc": "操作方式已更新",
                    "desc_color": 3
                },
                "main_title": {
                    "title": "ℹ️ 提示",
                    "desc": "确认方式已更新"
                },
                "sub_title_text": "请直接回复 **确认** 或 **取消** 来操作",
                "task_id": task_id
            }

            plain = {
                "response_type": "update_template_card",
                "template_card": update_card
            }
            return json.dumps(plain, ensure_ascii=False)

        # 通用模板卡片事件处理(其他类型的卡片)
        update_card = {
            "card_type": "text_notice",
            "source": {
                "icon_url": "",
                "desc": "已处理",
                "desc_color": 3
            },
            "main_title": {
                "title": "✅ 操作成功",
                "desc": f"您点击了: {event_key}"
            },
            "sub_title_text": "您的操作已经成功处理",
            "task_id": task_id
        }

        plain = {
            "response_type": "update_template_card",
            "template_card": update_card
        }
        return json.dumps(plain, ensure_ascii=False)

    async def _handle_choice_event(
        self, data: dict, template_card_event: dict, task_id: str, user_id: str
    ) -> str:
        """处理 AskUserQuestion 的投票选择事件

        从 task_id 解析 bot_key/user_id，从 ChoiceManager 获取会话，
        记录用户选择，返回下一题或触发答案提交。

        Args:
            data: 完整事件数据
            template_card_event: 模板卡片事件数据
            task_id: 卡片 task_id (格式: choice@{bot_key}@{user_id}@{timestamp})
            user_id: 操作用户ID
        """
        from src.core.choice_manager import get_choice_manager
        from src.core.claude_relay_orchestrator import ClaudeRelayOrchestrator

        choice_mgr = get_choice_manager()

        # 解析 task_id: choice@{bot_key}@{user_id}@{timestamp}
        parts = task_id.split("@")
        if len(parts) < 4:
            logger.warning(f"[Choice] task_id 格式错误: {task_id}")
            return self._build_update_card(task_id, "格式错误", "无法处理此选择")

        choice_bot_key = parts[1]
        choice_user_id = parts[2]

        # 校验用户身份：只有发起选择的用户才能回答
        # 注意：不能返回 update_template_card，否则会覆盖原始 vote 卡片
        if user_id != choice_user_id:
            logger.warning(
                f"[Choice] 用户不匹配: event_user={user_id}, session_user={choice_user_id}"
            )
            return json.dumps({"msgtype": "text", "text": {"content": ""}}, ensure_ascii=False)

        # 获取会话
        session = choice_mgr.get_session(choice_bot_key, choice_user_id)
        if not session:
            logger.warning(f"[Choice] 会话不存在或已过期: {choice_bot_key}:{choice_user_id}")
            return self._build_update_card(task_id, "已过期", "选择会话已过期，请重新发送消息")

        # 所有问题已回答完毕时的重复点击保护
        if session.current_index >= len(session.questions):
            logger.info(f"[Choice] 所有问题已回答，忽略重复点击: {choice_bot_key}:{choice_user_id}")
            return json.dumps({"msgtype": "text", "text": {"content": ""}}, ensure_ascii=False)

        # 提取用户选择的选项
        # vote_interaction 回调格式（企业微信文档）:
        # template_card_event.selected_items.selected_item[].option_ids.option_id[]
        selected_items = template_card_event.get("selected_items", {})
        selected_item_list = selected_items.get("selected_item", [])

        # 从选项ID映射回选项文本
        current_question = session.questions[session.current_index]
        options = current_question.get("options", [])
        selected_labels = []
        for item in selected_item_list:
            option_ids_obj = item.get("option_ids", {})
            option_id_list = option_ids_obj.get("option_id", [])
            for opt_id in option_id_list:
                # opt_id 格式: opt_0, opt_1, ...
                try:
                    idx = int(opt_id.replace("opt_", ""))
                    if 0 <= idx < len(options):
                        selected_labels.append(options[idx].get("label", opt_id))
                    else:
                        selected_labels.append(opt_id)
                except (ValueError, IndexError):
                    selected_labels.append(opt_id)

        # 检查是否选择了"其他"（自由输入）
        has_other = any(opt_id == "opt_other" for item in selected_item_list
                        for opt_id in item.get("option_ids", {}).get("option_id", []))
        if has_other:
            logger.info(
                f"[Choice] 用户选择'其他': bot={choice_bot_key}, user={choice_user_id}, "
                f"question_index={session.current_index}"
            )
            # 不推进问题，更新卡片提示用户发送文字
            prompt_card = {
                "card_type": "vote_interaction",
                "source": {
                    "icon_url": "",
                    "desc": "AI 助手",
                },
                "main_title": {
                    "title": "✏️ 请输入您的答案",
                    "desc": f"当前问题：{session.questions[session.current_index].get('question', '')}",
                },
                "checkbox": {
                    "question_key": "choice_waiting",
                    "option_list": [{
                        "id": "waiting_0",
                        "text": "等待输入中...请直接发送消息",
                        "is_checked": True,
                    }],
                    "mode": 0,
                    "disable": True,
                },
                "submit_button": {
                    "text": "等待输入",
                    "key": "submit_waiting",
                },
                "task_id": task_id,
            }
            plain = {
                "response_type": "update_template_card",
                "template_card": prompt_card,
            }
            return json.dumps(plain, ensure_ascii=False)

        answer_text = ", ".join(selected_labels) if selected_labels else "(未选择)"
        logger.info(
            f"[Choice] 用户选择: bot={choice_bot_key}, user={choice_user_id}, "
            f"question_index={session.current_index}, answer={answer_text}"
        )

        # 记录答案
        result = choice_mgr.record_answer(choice_bot_key, choice_user_id, answer_text)

        if not result["done"]:
            # 还有下一题：update 卡片为下一题的 vote
            next_question = result["next_question"]
            next_index = result["next_index"]
            total = result["total"]

            next_card = ClaudeRelayOrchestrator._build_vote_card(
                session.task_id_prefix, next_question, next_index, total
            )

            # update_template_card 更新为下一题
            plain = {
                "response_type": "update_template_card",
                "template_card": next_card,
            }
            return json.dumps(plain, ensure_ascii=False)

        else:
            # 所有问题已回答完毕
            # 更新卡片为已完成状态（保持 vote_interaction 类型，禁用选项）
            # 注意：企业微信不支持跨 card_type 更新，所以保持 vote_interaction
            processing_card = {
                "card_type": "vote_interaction",
                "source": {
                    "icon_url": "",
                    "desc": "AI 助手",
                },
                "main_title": {
                    "title": "✅ 已提交",
                    "desc": "已收到您的所有回答，正在生成结果，请稍候...",
                },
                "checkbox": {
                    "question_key": "choice_done",
                    "option_list": [{
                        "id": "done_0",
                        "text": "⏳ 正在处理，完成后将自动推送结果",
                        "is_checked": True,
                    }],
                    "mode": 0,
                    "disable": True,
                },
                "submit_button": {
                    "text": "已完成",
                    "key": "submit_done",
                },
                "task_id": task_id,
            }

            # spawn 异步任务提交答案
            asyncio.create_task(
                self._submit_answers_async(choice_bot_key, choice_user_id)
            )

            plain = {
                "response_type": "update_template_card",
                "template_card": processing_card,
            }
            return json.dumps(plain, ensure_ascii=False)

    async def _submit_answers_async(self, bot_key: str, user_id: str):
        """异步提交用户答案到 clawrelay-api，恢复 Claude 会话

        格式化答案 → 通过 adapter.stream_chat 发送 → 推送结果到 response_url
        """
        from src.core.choice_manager import get_choice_manager
        from src.adapters.claude_relay_adapter import (
            ClaudeRelayAdapter, TextDelta, AskUserQuestionEvent,
        )

        choice_mgr = get_choice_manager()

        # 防止重复提交（投票和文本 fallback 可能并发触发）
        if not choice_mgr.mark_submitted(bot_key, user_id):
            logger.warning(f"[Choice] 重复提交，已忽略: {bot_key}:{user_id}")
            return

        session = choice_mgr.get_session(bot_key, user_id)
        if not session:
            logger.error(f"[Choice] 提交答案时会话不存在: {bot_key}:{user_id}")
            return

        try:
            # 格式化答案
            answers_text = choice_mgr.format_answers(bot_key, user_id)
            logger.info(
                f"[Choice] 提交答案: bot={bot_key}, user={user_id}, "
                f"answers_len={len(answers_text)}, "
                f"has_response_url={bool(session.response_url)}, "
                f"session_id={session.relay_session_id[:8]}..."
            )

            # 通过 adapter 恢复 Claude 会话
            adapter = ClaudeRelayAdapter(
                relay_url=session.relay_url,
                model=session.model,
                working_dir=session.working_dir,
                env_vars=session.env_vars or None,
            )
            messages = [{"role": "user", "content": answers_text}]

            # 流式接收 Claude 的回复
            accumulated_text = ""
            async for event in adapter.stream_chat(
                messages,
                session.system_prompt,
                session_id=session.relay_session_id,
            ):
                if isinstance(event, TextDelta):
                    accumulated_text += event.text
                elif isinstance(event, AskUserQuestionEvent):
                    # Claude 又提了新问题（暂不支持嵌套选择，记录日志）
                    logger.warning(
                        f"[Choice] 恢复会话中收到嵌套 AskUserQuestion，暂不支持: "
                        f"questions={len(event.questions)}"
                    )

            if not accumulated_text.strip():
                accumulated_text = "已收到您的选择，处理完成。"

            logger.info(
                f"[Choice] Claude 回复完成: bot={bot_key}, user={user_id}, "
                f"reply_len={len(accumulated_text)}"
            )

            # 通过 response_url 推送结果
            if session.response_url:
                success = await ProactiveReplyClient.send_markdown(
                    session.response_url, accumulated_text
                )
                if success:
                    logger.info(
                        f"[Choice] 推送成功: bot={bot_key}, user={user_id}"
                    )
                else:
                    logger.warning(
                        f"[Choice] 推送失败: bot={bot_key}, user={user_id}, "
                        f"response_url 可能已过期"
                    )
            else:
                logger.warning(
                    f"[Choice] 无 response_url，无法推送结果: bot={bot_key}, user={user_id}"
                )

        except Exception as e:
            logger.error(
                f"[Choice] 提交答案异常: bot={bot_key}, user={user_id}, error={e}",
                exc_info=True,
            )
            # 尝试推送错误消息
            if session and session.response_url:
                try:
                    await ProactiveReplyClient.send_markdown(
                        session.response_url,
                        f"抱歉，处理您的选择时出现错误，请重新发送消息。",
                    )
                except Exception:
                    pass
        finally:
            # 清理会话
            choice_mgr.remove_session(bot_key, user_id)

    @staticmethod
    def _build_update_card(task_id: str, title: str, desc: str) -> str:
        """构建 update_template_card 响应"""
        card = {
            "card_type": "text_notice",
            "source": {
                "icon_url": "",
                "desc": "AI 助手",
            },
            "main_title": {"title": title, "desc": desc},
            "task_id": task_id,
        }
        return json.dumps(
            {"response_type": "update_template_card", "template_card": card},
            ensure_ascii=False,
        )


class MessageHandlerRouter:
    """消息处理器路由

    v2.0: 支持传递bot_key参数给文本处理器
    v2.3: 支持传递bot_key给事件处理器,集成确认流程
    """

    def __init__(self, stream_mgr: StreamManager, bot_key: str = "default", encoding_aes_key: str = ""):
        """
        初始化消息路由器

        Args:
            stream_mgr: 流式管理器
            bot_key: 机器人标识(v2.0)
            encoding_aes_key: 机器人的 EncodingAESKey（v8.0 图片解密用）
        """
        self.stream_mgr = stream_mgr
        self.bot_key = bot_key
        text_handler = TextMessageHandler(stream_mgr, bot_key=bot_key, encoding_aes_key=encoding_aes_key)
        self.handlers = {
            'text': text_handler,
            'voice': VoiceMessageHandler(text_handler),  # v7.0: 语音消息(已转文本)
            'file': FileMessageHandler(text_handler, encoding_aes_key=encoding_aes_key),  # v10.0: 文件解密+分析
            'image': ImageMessageHandler(text_handler, encoding_aes_key=encoding_aes_key),  # v8.0: 图片解密+多模态
            'mixed': MixedMessageHandler(text_handler, encoding_aes_key=encoding_aes_key),  # v8.0: 图文混排
            'stream': StreamMessageHandler(stream_mgr, bot_key=bot_key),  # v3.5: 传递bot_key
            'event': EventMessageHandler(bot_key=bot_key)  # v2.3: 传递bot_key
        }
        logger.info(f"MessageHandlerRouter初始化完成: bot_key={bot_key}")

    async def route(self, data: dict, stream_id: str) -> Optional[str]:
        """
        路由消息到对应的处理器(异步方法)

        Args:
            data: 解密后的消息数据
            stream_id: 流式消息ID

        Returns:
            消息JSON字符串或None

        Raises:
            Exception: 处理器执行失败时向上抛出
        """
        msgtype = data.get('msgtype')
        logger.info("路由消息类型: %s", msgtype)

        handler = self.handlers.get(msgtype)
        if handler:
            try:
                return await handler.handle(data, stream_id)
            except Exception as e:
                logger.error(
                    f"处理器执行失败: msgtype={msgtype}, bot_key={self.bot_key}, error={e}",
                    exc_info=True
                )
                # 向上抛出,让bot_instance和app.py统一处理
                raise
        else:
            logger.warning("不支持的消息类型: %s", msgtype)
            return None
