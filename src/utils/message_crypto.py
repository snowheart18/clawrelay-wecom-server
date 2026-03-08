#!/usr/bin/env python
# coding=utf-8
"""
消息加解密模块
负责企业微信消息的加密、解密和签名验证
"""

import logging
import json
from typing import Optional
from .crypto_libs.WXBizJsonMsgCrypt import WXBizJsonMsgCrypt

logger = logging.getLogger(__name__)


class MessageCrypto:
    """消息加解密工具类"""

    def __init__(self, token: str, encoding_aes_key: str, receiveid: str = ''):
        """
        初始化消息加解密器

        Args:
            token: 企业微信Token
            encoding_aes_key: 43位AES密钥
            receiveid: 接收者ID（智能机器人为空字符串,企业应用为CorpID）
        """
        self.token = token
        self.encoding_aes_key = encoding_aes_key
        self.receiveid = receiveid
        self._wxcpt = WXBizJsonMsgCrypt(token, encoding_aes_key, receiveid)

    def verify_url(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str
    ) -> Optional[str]:
        """
        验证URL有效性（企业微信配置回调URL时调用）

        Args:
            msg_signature: 消息签名
            timestamp: 时间戳
            nonce: 随机数
            echostr: 加密的echostr

        Returns:
            解密后的echostr,失败返回None
        """
        logger.info("开始验证URL, msg_signature=%s, timestamp=%s, nonce=%s",
                   msg_signature, timestamp, nonce)

        ret, decrypted_echostr = self._wxcpt.VerifyURL(
            msg_signature,
            timestamp,
            nonce,
            echostr
        )

        if ret != 0:
            logger.error("URL验证失败,错误码: %d", ret)
            return None

        logger.info("URL验证成功")
        return decrypted_echostr

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
        logger.info("开始解密消息, msg_signature=%s, timestamp=%s, nonce=%s",
                   msg_signature, timestamp, nonce)

        ret, msg = self._wxcpt.DecryptMsg(
            post_data,
            msg_signature,
            timestamp,
            nonce
        )

        if ret != 0:
            logger.error("消息解密失败,错误码: %d", ret)
            return None

        try:
            data = json.loads(msg)
            logger.info("消息解密成功, msgtype=%s", data.get('msgtype'))
            logger.debug("消息内容: %s", data)
            return data
        except json.JSONDecodeError as e:
            logger.error("消息JSON解析失败: %s", e)
            return None

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
        logger.info("开始加密消息, nonce=%s, timestamp=%s", nonce, timestamp)
        logger.debug("发送消息: %s", message)

        ret, encrypted_msg = self._wxcpt.EncryptMsg(message, nonce, timestamp)

        if ret != 0:
            logger.error("加密失败,错误码: %d", ret)
            return None

        # 记录消息类型和stream_id（如果有）
        try:
            msg_data = json.loads(message)
            if 'stream' in msg_data:
                stream_id = msg_data['stream']['id']
                finish = msg_data['stream']['finish']
                msgtype = msg_data.get('msgtype', 'stream')
                logger.info("消息加密成功, msgtype=%s, stream_id=%s, finish=%s",
                          msgtype, stream_id, finish)
            else:
                logger.info("消息加密成功")
        except:
            logger.info("消息加密成功")

        logger.debug("加密后的消息: %s", encrypted_msg)
        return encrypted_msg
