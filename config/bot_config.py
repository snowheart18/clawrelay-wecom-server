#!/usr/bin/env python
# coding=utf-8
"""
机器人配置管理器
负责加载和管理多个机器人的配置
"""

import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Sentinel: 表示机器人已被删除或禁用
BOT_REMOVED = object()


class BotConfig:
    """单个机器人的配置"""

    def __init__(
        self,
        bot_key: str,
        bot_id: str,
        token: str = "",
        encoding_aes_key: str = "",
        callback_path: str = "",
        secret: str = "",
        name: str = "",
        tool_categories: List[str] = None,
        custom_commands: List[str] = None,
        allowed_users: List[str] = None,
        description: str = "",
        llm_type: str = "claude_relay",
        relay_url: str = "",
        working_dir: str = "",
        model: str = "",
        system_prompt: str = "",
        env_vars: Dict[str, str] = None
    ):
        self.bot_key = bot_key
        self.bot_id = bot_id
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self.callback_path = callback_path
        self.secret = secret
        # 数据库 updated_at，用于配置热更新检测
        self.updated_at = None
        # v2.1: name用于过滤@机器人名称提及
        self.name = name
        # v2.0: tool_categories控制工具权限
        self.tool_categories = tool_categories or []
        # custom_commands用于加载自定义命令模块
        self.custom_commands = custom_commands or []
        # v2.2: allowed_users用户白名单(为空表示不限制)
        self.allowed_users = allowed_users or []
        self.description = description
        # LLM后端类型 (统一为 claude_relay)
        self.llm_type = llm_type
        # clawrelay-api 地址
        self.relay_url = relay_url
        # v3.0: claude工作目录
        self.working_dir = working_dir
        # v3.0: 模型名称
        self.model = model
        # v3.0: 系统提示词
        self.system_prompt = system_prompt
        # v3.1: 传递给clawrelay-api的环境变量（注入到Claude子进程）
        self.env_vars = env_vars or {}

    def __repr__(self):
        return (
            f"BotConfig(bot_key='{self.bot_key}', "
            f"name='{self.name}', "
            f"callback_path='{self.callback_path}', "
            f"description='{self.description}')"
        )


class BotConfigManager:
    """机器人配置管理器"""

    def __init__(self):
        """初始化配置管理器，从数据库加载配置"""
        self.bots: Dict[str, BotConfig] = {}
        self._load_config()

    def _load_config(self):
        """从数据库加载机器人配置"""
        from src.utils.database import get_db_connection

        connection = get_db_connection()
        if not connection:
            logger.error("数据库连接失败，无法加载机器人配置")
            return

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM robot_bots WHERE enabled = 1")
                rows = cursor.fetchall()

            self.bots = {}
            for row in rows:
                bot_config = self._parse_bot_row(row)
                if bot_config:
                    self.bots[bot_config.bot_key] = bot_config
                    logger.info(f"加载机器人配置: {bot_config}")

            # 加载工具权限
            self._load_tool_permissions(connection)

            logger.info(f"成功从数据库加载 {len(self.bots)} 个机器人配置")

        except Exception as e:
            logger.error(f"从数据库加载配置失败: {e}")
        finally:
            connection.close()

    def _load_tool_permissions(self, connection):
        """从 robot_bot_tool_permissions 表加载工具权限"""
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT rb.bot_key, rtp.tool_category
                    FROM robot_bot_tool_permissions rtp
                    JOIN robot_bots rb ON rb.id = rtp.bot_id
                    WHERE rtp.enabled = 1
                """)
                rows = cursor.fetchall()

            for row in rows:
                bot_key = row['bot_key']
                if bot_key in self.bots:
                    self.bots[bot_key].tool_categories.append(row['tool_category'])
        except Exception as e:
            logger.warning(f"加载工具权限失败(非致命): {e}")

    def _parse_bot_row(self, row: dict) -> Optional[BotConfig]:
        """将数据库行解析为 BotConfig 对象"""
        bot_key = row['bot_key']

        if not all([row.get('bot_id'), row.get('secret')]):
            logger.warning(f"机器人 {bot_key} 配置不完整（需要bot_id和secret），跳过")
            return None

        # 解析 JSON 字段
        allowed_users = row.get('allowed_users') or []
        if isinstance(allowed_users, str):
            allowed_users = json.loads(allowed_users)

        custom_commands = row.get('custom_command_modules') or []
        if isinstance(custom_commands, str):
            custom_commands = json.loads(custom_commands)

        env_vars = row.get('env_vars') or {}
        if isinstance(env_vars, str):
            env_vars = json.loads(env_vars)

        bot_config = BotConfig(
            bot_key=bot_key,
            bot_id=row['bot_id'],
            token=row.get('token', ''),
            encoding_aes_key=row.get('encoding_aes_key', ''),
            callback_path=row.get('callback_path', ''),
            secret=row.get('secret', ''),
            name=row.get('name', ''),
            tool_categories=[],
            custom_commands=custom_commands,
            allowed_users=allowed_users,
            description=row.get('description', ''),
            llm_type=row.get('llm_type', 'claude_relay'),
            relay_url=row.get('relay_url', ''),
            working_dir=row.get('working_dir', ''),
            model=row.get('model', ''),
            system_prompt=row.get('system_prompt', ''),
            env_vars=env_vars
        )
        bot_config.updated_at = row.get('updated_at')
        return bot_config

    def check_bot_updated(self, bot_key: str, cached_updated_at):
        """
        检查机器人配置是否有更新，如有则返回新配置

        Args:
            bot_key: 机器人key
            cached_updated_at: 缓存中的updated_at值

        Returns:
            BotConfig: 配置有更新
            None: 无变化
            BOT_REMOVED: 被删除或禁用
        """
        from src.utils.database import get_db_connection

        connection = get_db_connection()
        if not connection:
            return None

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM robot_bots WHERE bot_key = %s",
                    (bot_key,)
                )
                row = cursor.fetchone()

            if not row or not row.get('enabled'):
                return BOT_REMOVED

            db_updated_at = row.get('updated_at')
            if db_updated_at == cached_updated_at:
                return None  # 无变化

            # 配置有更新，解析完整配置
            bot_config = self._parse_bot_row(row)
            if not bot_config:
                return None

            # 加载工具权限
            try:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT rtp.tool_category
                        FROM robot_bot_tool_permissions rtp
                        WHERE rtp.bot_id = %s AND rtp.enabled = 1
                    """, (row['id'],))
                    perm_rows = cursor.fetchall()
                for perm_row in perm_rows:
                    bot_config.tool_categories.append(perm_row['tool_category'])
            except Exception as e:
                logger.warning(f"加载机器人 {bot_key} 工具权限失败(非致命): {e}")

            self.bots[bot_key] = bot_config
            logger.info(f"检测到机器人 {bot_key} 配置变更，已重新加载")
            return bot_config

        except Exception as e:
            logger.error(f"检查机器人配置更新失败: {e}")
            return None
        finally:
            connection.close()

    def load_bot_by_path(self, callback_path: str) -> Optional[BotConfig]:
        """从数据库按回调路径加载单个机器人配置（热加载）"""
        from src.utils.database import get_db_connection

        connection = get_db_connection()
        if not connection:
            return None

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM robot_bots WHERE callback_path = %s AND enabled = 1",
                    (callback_path,)
                )
                row = cursor.fetchone()

            if not row:
                return None

            bot_config = self._parse_bot_row(row)
            if not bot_config:
                return None

            # 加载该机器人的工具权限
            try:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT rtp.tool_category
                        FROM robot_bot_tool_permissions rtp
                        WHERE rtp.bot_id = %s AND rtp.enabled = 1
                    """, (row['id'],))
                    perm_rows = cursor.fetchall()
                for perm_row in perm_rows:
                    bot_config.tool_categories.append(perm_row['tool_category'])
            except Exception as e:
                logger.warning(f"加载机器人 {bot_config.bot_key} 工具权限失败(非致命): {e}")

            self.bots[bot_config.bot_key] = bot_config
            return bot_config

        except Exception as e:
            logger.error(f"热加载机器人配置失败: {e}")
            return None
        finally:
            connection.close()

    def get_bot(self, bot_key: str) -> Optional[BotConfig]:
        """获取指定机器人的配置"""
        return self.bots.get(bot_key)

    def get_bot_by_path(self, callback_path: str) -> Optional[BotConfig]:
        """根据回调路径获取机器人配置"""
        for bot_config in self.bots.values():
            if bot_config.callback_path == callback_path:
                return bot_config
        return None

    def get_all_bots(self) -> Dict[str, BotConfig]:
        """获取所有机器人配置"""
        return self.bots

    def list_callback_paths(self) -> List[str]:
        """列出所有回调路径"""
        return [bot.callback_path for bot in self.bots.values()]

    @staticmethod
    def get_bot_config(bot_key: str) -> Dict:
        """
        获取机器人配置(v2.0 API)

        为了兼容新架构,提供静态方法接口

        Args:
            bot_key: 机器人唯一标识

        Returns:
            Dict: 机器人配置字典,包含tool_categories字段
        """
        # 创建全局管理器实例(单例模式)
        global _bot_config_manager
        if _bot_config_manager is None:
            logger.info("首次访问,初始化全局BotConfigManager")
            _bot_config_manager = BotConfigManager()

        bot = _bot_config_manager.get_bot(bot_key)
        if not bot:
            raise ValueError(f"机器人配置不存在: {bot_key}")

        # 转换为字典格式(兼容v2.0)
        return {
            "bot_id": bot.bot_id,
            "token": bot.token,
            "encoding_aes_key": bot.encoding_aes_key,
            "callback_path": bot.callback_path,
            "secret": bot.secret,
            "name": bot.name,
            "custom_commands": bot.custom_commands,
            "description": bot.description,
            "tool_categories": bot.tool_categories,
            "llm_type": bot.llm_type,
            "relay_url": bot.relay_url,
            "working_dir": bot.working_dir,
            "model": bot.model,
            "system_prompt": bot.system_prompt,
            "env_vars": bot.env_vars
        }


# 全局管理器实例(单例)
_bot_config_manager: Optional[BotConfigManager] = None
