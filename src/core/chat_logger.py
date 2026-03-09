"""
对话日志记录器

异步记录用户与AI的对话日志到 robot_chat_logs 表。
使用 fire-and-forget 模式（asyncio.create_task），不阻塞主流程。

作者: Claude Code
日期: 2026-03-03
"""

import asyncio
import json
import logging
from datetime import datetime
from functools import wraps

logger = logging.getLogger(__name__)


def _async_wrap(func):
    """将同步函数包装为异步函数"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
    return wrapper


class ChatLogger:
    """对话日志记录器

    异步写入日志到 robot_chat_logs 表，写入失败只记录错误日志，不影响主流程。
    """

    def log(
        self,
        bot_key: str,
        user_id: str,
        stream_id: str,
        message_content: str,
        response_content: str,
        status: str = "success",
        error_message: str = "",
        latency_ms: int = 0,
        request_at: datetime = None,
        relay_session_id: str = "",
        tools_used: list = None,
        log_context: dict = None,
    ):
        """启动异步日志写入（fire-and-forget）"""
        asyncio.create_task(self._save_log(
            bot_key=bot_key,
            user_id=user_id,
            stream_id=stream_id,
            message_content=message_content,
            response_content=response_content,
            status=status,
            error_message=error_message,
            latency_ms=latency_ms,
            request_at=request_at or datetime.now(),
            relay_session_id=relay_session_id,
            tools_used=tools_used,
            log_context=log_context or {},
        ))

    @_async_wrap
    def _save_log(
        self,
        bot_key: str,
        user_id: str,
        stream_id: str,
        message_content: str,
        response_content: str,
        status: str,
        error_message: str,
        latency_ms: int,
        request_at: datetime,
        relay_session_id: str,
        tools_used: list,
        log_context: dict,
    ):
        """同步写入数据库"""
        from src.utils.database import (
            get_db_connection,
            get_user_email_by_wework_user_id,
            get_user_name_by_wework_user_id,
        )

        try:
            # 查询用户信息
            user_email = None
            user_name = None
            try:
                user_email = get_user_email_by_wework_user_id(user_id)
                user_name = get_user_name_by_wework_user_id(user_id)
            except Exception as e:
                logger.debug(f"[ChatLogger] 查询用户信息失败: {e}")

            connection = get_db_connection()
            if not connection:
                logger.error("[ChatLogger] 数据库连接失败，跳过日志写入")
                return

            try:
                with connection.cursor() as cursor:
                    sql = """
                        INSERT INTO robot_chat_logs (
                            bot_key, user_id, user_email, user_name,
                            chat_type, chat_id, session_key, relay_session_id, stream_id,
                            message_type, message_content, quoted_content, file_info,
                            response_content, tools_used,
                            status, error_message, latency_ms,
                            request_at, response_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s
                        )
                    """
                    # 从 log_context 提取字段
                    chat_type = log_context.get('chat_type', 'single')
                    chat_id = log_context.get('chat_id', '') or None
                    session_key = log_context.get('session_key', '') or None
                    message_type = log_context.get('message_type', 'text')
                    quoted_content = log_context.get('quoted_content', '') or None
                    file_info = log_context.get('file_info')

                    cursor.execute(sql, (
                        bot_key,
                        user_id,
                        user_email,
                        user_name,
                        chat_type,
                        chat_id,
                        session_key,
                        relay_session_id or None,
                        stream_id,
                        message_type,
                        message_content[:10000] if message_content else None,
                        quoted_content[:5000] if quoted_content else None,
                        json.dumps(file_info, ensure_ascii=False) if file_info else None,
                        response_content[:50000] if response_content else None,
                        json.dumps(tools_used, ensure_ascii=False) if tools_used else None,
                        status,
                        error_message[:5000] if error_message else None,
                        latency_ms,
                        request_at,
                        datetime.now() if status == 'success' else None,
                    ))
                    connection.commit()

                logger.debug(
                    f"[ChatLogger] 日志写入成功: bot={bot_key}, user={user_id}, "
                    f"status={status}, latency={latency_ms}ms"
                )

            finally:
                connection.close()

        except Exception as e:
            logger.error(f"[ChatLogger] 日志写入失败: {e}", exc_info=True)


# 单例
_chat_logger = ChatLogger()


def get_chat_logger() -> ChatLogger:
    return _chat_logger
