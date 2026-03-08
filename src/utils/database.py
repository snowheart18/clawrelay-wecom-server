#!/usr/bin/env python
# coding=utf-8
"""
数据库连接和查询工具
"""

import pymysql
import os
import logging
from typing import Optional
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

logger = logging.getLogger(__name__)

# 从环境变量读取数据库配置（必须通过环境变量或.env文件配置，无默认值）
DB_HOST = os.getenv('DB_HOST', '')
DB_PORT = int(os.getenv('DB_PORT', '3306'))
DB_DATABASE = os.getenv('DB_DATABASE', '')
DB_USERNAME = os.getenv('DB_USERNAME', '')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

if not all([DB_HOST, DB_DATABASE, DB_USERNAME, DB_PASSWORD]):
    logger.warning("数据库配置不完整，请通过环境变量设置 DB_HOST, DB_DATABASE, DB_USERNAME, DB_PASSWORD")


def get_db_connection():
    """获取数据库连接"""
    try:
        connection = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USERNAME,
            password=DB_PASSWORD,
            database=DB_DATABASE,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
        logger.info("数据库连接成功，host=%s, database=%s", DB_HOST, DB_DATABASE)
        return connection
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        return None


def get_user_name_by_wework_user_id(wework_user_id: str) -> Optional[str]:
    """
    根据企业微信用户ID查询用户姓名

    开源版本默认返回 None，调用方会使用 user_id 或默认名称作为降级。
    如需启用用户名查询，请创建用户映射表并修改此函数的查询逻辑。

    Args:
        wework_user_id: 企业微信用户ID

    Returns:
        用户姓名，未配置用户表时返回 None
    """
    return None


def get_user_email_by_wework_user_id(wework_user_id: str) -> Optional[str]:
    """
    根据企业微信用户ID查询用户邮箱

    开源版本默认返回 None。
    如需启用邮箱查询，请创建用户映射表并修改此函数的查询逻辑。

    Args:
        wework_user_id: 企业微信用户ID

    Returns:
        用户邮箱，未配置用户表时返回 None
    """
    return None
