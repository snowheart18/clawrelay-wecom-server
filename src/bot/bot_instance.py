#!/usr/bin/env python
# coding=utf-8
"""
单个机器人实例封装
每个机器人有独立的加解密器、流式管理器、消息处理器
"""

import logging
from typing import Optional
from config.bot_config import BotConfig
from src.utils.message_crypto import MessageCrypto
from src.utils.weixin_utils import StreamManager
from src.handlers.message_handlers import MessageHandlerRouter

logger = logging.getLogger(__name__)


class BotInstance:
    """单个机器人实例"""

    def __init__(self, config: BotConfig):
        """
        初始化机器人实例

        Args:
            config: 机器人配置
        """
        self.config = config
        self.bot_key = config.bot_key
        self.callback_path = config.callback_path

        # 初始化核心组件(每个机器人独立实例)
        self.crypto = MessageCrypto(
            token=config.token,
            encoding_aes_key=config.encoding_aes_key,
            receiveid=''  # 智能机器人的receiveid是空串
        )
        self.stream_mgr = StreamManager()
        self.message_router = MessageHandlerRouter(
            self.stream_mgr,
            bot_key=config.bot_key,
            encoding_aes_key=config.encoding_aes_key,
        )

        # 加载自定义命令(如果有)
        self._load_custom_commands()

        logger.info(
            f"机器人实例初始化成功: bot_key={self.bot_key}, "
            f"callback_path={self.callback_path}, "
            f"description={self.config.description}"
        )

    def _load_custom_commands(self):
        """
        加载自定义命令处理器

        通过动态导入 custom_commands 中配置的模块,
        调用模块的 register_commands(router) 函数注册命令

        注意: custom_commands是Python模块路径列表
        例如: ["src.handlers.custom.demo_commands"]
        """
        if not self.config.custom_commands:
            logger.info(f"机器人 {self.bot_key} 使用默认命令集")
            return

        # 获取命令路由器
        text_handler = self.message_router.handlers.get('text')
        if not text_handler:
            logger.warning(f"机器人 {self.bot_key} 没有文本消息处理器,无法加载自定义命令")
            return

        command_router = text_handler.command_router

        # 动态导入自定义命令模块
        for module_path in self.config.custom_commands:
            try:
                # 导入模块
                import importlib
                module = importlib.import_module(module_path)

                # 调用注册函数
                if hasattr(module, 'register_commands'):
                    module.register_commands(command_router)
                    logger.info(f"成功加载自定义命令模块: {module_path}")
                else:
                    logger.warning(
                        f"模块 {module_path} 没有 register_commands 函数,跳过"
                    )
            except ModuleNotFoundError as e:
                logger.error(f"自定义命令模块未找到: {module_path} ({e})")
            except Exception as e:
                logger.error(f"加载自定义命令模块 {module_path} 失败: {e}")

    def verify_url(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str
    ) -> Optional[str]:
        """
        验证URL有效性

        Args:
            msg_signature: 消息签名
            timestamp: 时间戳
            nonce: 随机数
            echostr: 加密的echostr

        Returns:
            解密后的echostr,失败返回None
        """
        return self.crypto.verify_url(msg_signature, timestamp, nonce, echostr)

    def decrypt_message(
        self,
        post_data: bytes,
        msg_signature: str,
        timestamp: str,
        nonce: str
    ) -> Optional[dict]:
        """
        解密消息

        Args:
            post_data: POST请求体
            msg_signature: 消息签名
            timestamp: 时间戳
            nonce: 随机数

        Returns:
            解密后的消息字典,失败返回None
        """
        return self.crypto.decrypt_message(post_data, msg_signature, timestamp, nonce)

    async def handle_message(self, data: dict, stream_id: str) -> Optional[str]:
        """
        处理消息(异步方法)

        Args:
            data: 解密后的消息数据
            stream_id: 流式消息ID

        Returns:
            消息JSON字符串或None

        Raises:
            Exception: 处理过程中的任何异常都会向上抛出,
                      由上层(app.py)统一处理并返回给企业微信
        """
        try:
            # v2.2: 用户白名单检查
            # 企业微信消息格式: data['from']['userid']
            user_id = data.get('from', {}).get('userid', '')
            if self.config.allowed_users and user_id not in self.config.allowed_users:
                logger.warning(
                    f"[用户权限] 用户 {user_id} 不在机器人 {self.bot_key} 的白名单中，拒绝访问"
                )
                # 返回拒绝消息
                from src.utils.weixin_utils import MessageBuilder
                return MessageBuilder.text(
                    stream_id,
                    "⚠️ 抱歉，您没有使用此机器人的权限。\n\n如需开通权限，请联系管理员。",
                    finish=True
                )

            return await self.message_router.route(data, stream_id)
        except Exception as e:
            logger.error(
                f"机器人 {self.bot_key} 处理消息失败: {e}",
                exc_info=True,
                extra={
                    "bot_key": self.bot_key,
                    "msgtype": data.get("msgtype"),
                    "stream_id": stream_id
                }
            )
            # 向上抛出异常,让app.py统一处理
            raise

    def encrypt_message(
        self,
        message: str,
        nonce: str,
        timestamp: str
    ) -> Optional[str]:
        """
        加密消息

        Args:
            message: 消息JSON字符串
            nonce: 随机数
            timestamp: 时间戳

        Returns:
            加密后的消息字符串,失败返回None
        """
        return self.crypto.encrypt_message(message, nonce, timestamp)

    def __repr__(self):
        return (
            f"BotInstance(bot_key='{self.bot_key}', "
            f"callback_path='{self.callback_path}')"
        )
