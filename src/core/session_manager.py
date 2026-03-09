"""
会话管理器模块

负责管理 relay_session_id 的持久化：
- 存储和检索 relay_session_id（clawrelay-api 会话标识）
- 2小时超时自动过期（触发新会话）
- 持久化到 MySQL robot_sessions 表

会话历史由 clawrelay-api 通过 session_id 自行维护，本地只管 ID。

作者: Claude Code
版本: v3.0
"""

import json
import logging
from typing import Optional
import asyncio
from functools import wraps
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def async_wrap(func):
    """将同步函数包装为异步函数"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
    return wrapper


class SessionManager:
    """会话管理器

    管理 relay_session_id 的持久化，用于 clawrelay-api 会话关联。

    特性:
    - 2小时超时自动过期（返回空字符串触发新会话）
    - 持久化到 MySQL robot_sessions 表
    - 向后兼容旧数据格式

    Attributes:
        SESSION_TIMEOUT_HOURS: 会话超时时间（默认2小时）
    """

    SESSION_TIMEOUT_HOURS = 2

    def __init__(self):
        logger.info("会话管理器初始化完成")

    async def get_relay_session_id(
        self,
        bot_key: str,
        user_id: str,
    ) -> str:
        """获取 relay_session_id

        从数据库读取会话记录，检查超时，解析 relay_session_id。
        超时或不存在时返回空字符串，调用方应生成新 UUID。

        兼容旧格式：
        - 旧: {"messages": [...], "state": {"relay_session_id": "uuid"}}
        - 新: {"relay_session_id": "uuid"}

        Args:
            bot_key: 机器人唯一标识
            user_id: 企业微信用户ID

        Returns:
            str: relay_session_id，不存在或超时返回空字符串
        """
        session_id = f"{bot_key}_{user_id}"

        try:
            session = await self._get_session_from_db(session_id)

            if not session or not session.get("context"):
                logger.debug(f"会话不存在或上下文为空: {session_id}")
                return ""

            # 检查会话是否过期（超过2小时）
            last_active_at = session.get("last_active_at")
            if last_active_at:
                if isinstance(last_active_at, str):
                    last_active_at = datetime.fromisoformat(last_active_at)

                if last_active_at.tzinfo is None:
                    last_active_at = last_active_at.replace(tzinfo=timezone.utc)
                    current_time = datetime.now(timezone.utc)
                else:
                    current_time = datetime.now(last_active_at.tzinfo)

                hours_since = (current_time - last_active_at).total_seconds() / 3600
                if hours_since > self.SESSION_TIMEOUT_HOURS:
                    logger.info(
                        f"会话已超时: {session_id}, "
                        f"上次活跃: {hours_since:.1f}小时前"
                    )
                    return ""

            # 解析 context JSON
            context_json = session.get("context")
            if isinstance(context_json, str):
                data = json.loads(context_json)
            else:
                data = context_json

            # 提取 relay_session_id（兼容新旧格式）
            if isinstance(data, dict):
                # 新格式: {"relay_session_id": "uuid"}
                relay_id = data.get("relay_session_id", "")
                if relay_id:
                    return relay_id
                # 旧格式: {"messages": [...], "state": {"relay_session_id": "uuid"}}
                relay_id = data.get("state", {}).get("relay_session_id", "")
                return relay_id

            # 极旧格式（纯 list），无 session_id
            return ""

        except Exception as e:
            logger.error(
                f"获取 relay_session_id 失败: {session_id}, 错误: {e}",
                exc_info=True,
            )
            return ""

    async def save_relay_session_id(
        self,
        bot_key: str,
        user_id: str,
        relay_session_id: str,
    ):
        """保存 relay_session_id

        Args:
            bot_key: 机器人唯一标识
            user_id: 企业微信用户ID
            relay_session_id: clawrelay-api 会话标识
        """
        session_id = f"{bot_key}_{user_id}"

        try:
            storage_data = {"relay_session_id": relay_session_id}
            await self._upsert_session(
                session_id=session_id,
                bot_key=bot_key,
                user_id=user_id,
                context=json.dumps(storage_data, ensure_ascii=False),
            )
            logger.debug(f"保存 relay_session_id 成功: {session_id}")

        except Exception as e:
            logger.error(
                f"保存 relay_session_id 失败: {session_id}, 错误: {e}",
                exc_info=True,
            )

    async def clear_session(self, bot_key: str, user_id: str):
        """清空会话（下次对话将生成新 relay_session_id）

        Args:
            bot_key: 机器人唯一标识
            user_id: 企业微信用户ID
        """
        session_id = f"{bot_key}_{user_id}"

        try:
            await self._upsert_session(
                session_id=session_id,
                bot_key=bot_key,
                user_id=user_id,
                context="{}",
            )
            logger.info(f"清空会话成功: {session_id}")

        except Exception as e:
            logger.error(
                f"清空会话失败: {session_id}, 错误: {e}",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # 底层数据库方法
    # ------------------------------------------------------------------

    @async_wrap
    def _get_session_from_db(self, session_id: str) -> Optional[dict]:
        """从数据库获取会话"""
        from src.utils.database import get_db_connection

        connection = get_db_connection()
        if not connection:
            return None

        try:
            with connection.cursor() as cursor:
                sql = """
                    SELECT session_id, bot_id, user_id, context,
                           created_at, last_active_at
                    FROM robot_sessions
                    WHERE session_id = %s
                    LIMIT 1
                """
                cursor.execute(sql, (session_id,))
                return cursor.fetchone()

        except Exception as e:
            logger.error(f"数据库查询失败: {e}")
            return None
        finally:
            connection.close()

    @async_wrap
    def _upsert_session(
        self,
        session_id: str,
        bot_key: str,
        user_id: str,
        context: str,
    ):
        """创建或更新会话"""
        from src.utils.database import get_db_connection

        connection = get_db_connection()
        if not connection:
            raise Exception("数据库连接失败")

        try:
            bot_id = self._get_bot_id_by_key(bot_key)

            with connection.cursor() as cursor:
                sql = """
                    INSERT INTO robot_sessions
                        (session_id, bot_id, user_id, context,
                         created_at, last_active_at)
                    VALUES
                        (%s, %s, %s, %s, NOW(), NOW())
                    ON DUPLICATE KEY UPDATE
                        context = VALUES(context),
                        last_active_at = NOW()
                """
                cursor.execute(sql, (session_id, bot_id, user_id, context))
                connection.commit()

        finally:
            connection.close()

    def _get_bot_id_by_key(self, bot_key: str) -> str:
        """根据 bot_key 获取 bot_id（当前直接返回 bot_key）"""
        return bot_key
