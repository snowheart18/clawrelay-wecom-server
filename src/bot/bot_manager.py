#!/usr/bin/env python
# coding=utf-8
"""
机器人管理器
负责创建和管理多个机器人实例(工厂模式)
支持配置热更新: 数据库配置变更后自动生效(5秒检测间隔)
"""

import time
import logging
from typing import Dict, Optional
from config.bot_config import BotConfigManager, BOT_REMOVED
from src.bot.bot_instance import BotInstance

logger = logging.getLogger(__name__)

# 配置检测间隔(秒): 每个机器人最多每N秒检查一次DB
CONFIG_CHECK_INTERVAL = 5


class BotManager:
    """机器人管理器(工厂模式)"""

    def __init__(self):
        """初始化机器人管理器，从数据库加载配置"""
        self.config_manager = BotConfigManager()
        self.bots: Dict[str, BotInstance] = {}
        # 每个bot_key的上次配置检查时间
        self._last_check_time: Dict[str, float] = {}
        self._initialize_bots()

    def _initialize_bots(self):
        """初始化所有机器人实例"""
        all_configs = self.config_manager.get_all_bots()

        if not all_configs:
            logger.error("没有找到任何机器人配置")
            return

        for bot_key, bot_config in all_configs.items():
            try:
                bot_instance = BotInstance(bot_config)
                self.bots[bot_key] = bot_instance
                self._last_check_time[bot_key] = time.time()
                logger.info(f"成功初始化机器人: {bot_instance}")
            except Exception as e:
                logger.error(f"初始化机器人 {bot_key} 失败: {e}")

        logger.info(f"机器人管理器初始化完成,共加载 {len(self.bots)} 个机器人")

    def _check_and_refresh(self, bot_key: str) -> Optional[BotInstance]:
        """
        检查机器人配置是否有更新，有则刷新实例

        Returns:
            刷新后的BotInstance，无变化返回None，被删除/禁用也返回None
        """
        now = time.time()
        last_check = self._last_check_time.get(bot_key, 0)
        if now - last_check < CONFIG_CHECK_INTERVAL:
            return None  # 未到检测时间

        self._last_check_time[bot_key] = now

        cached_bot = self.bots.get(bot_key)
        if not cached_bot:
            return None

        cached_updated_at = cached_bot.config.updated_at
        result = self.config_manager.check_bot_updated(bot_key, cached_updated_at)

        if result is BOT_REMOVED:
            # 机器人被删除或禁用，移除缓存
            logger.info(f"♻️ 机器人 {bot_key} 已被禁用或删除，移除实例")
            del self.bots[bot_key]
            self._last_check_time.pop(bot_key, None)
            return None

        if result is None:
            return None  # 无变化

        # 配置有更新，重建实例 (result 是 BotConfig)
        try:
            new_instance = BotInstance(result)  # type: ignore[arg-type]
            self.bots[bot_key] = new_instance
            logger.info(f"🔄 机器人配置热更新成功: {bot_key}")
            return new_instance
        except Exception as e:
            logger.error(f"机器人 {bot_key} 配置热更新失败: {e}")
            return None

    def get_bot(self, bot_key: str) -> Optional[BotInstance]:
        """
        获取指定机器人实例

        Args:
            bot_key: 机器人key

        Returns:
            机器人实例,不存在返回None
        """
        return self.bots.get(bot_key)

    def get_bot_by_key(self, bot_key: str) -> Optional[BotInstance]:
        """
        根据bot_key获取机器人实例 (别名方法)

        Args:
            bot_key: 机器人key

        Returns:
            机器人实例,不存在返回None
        """
        return self.get_bot(bot_key)

    def get_bot_by_path(self, callback_path: str) -> Optional[BotInstance]:
        """
        根据回调路径获取机器人实例，支持配置热更新和新机器人热加载

        流程:
        1. 从缓存查找 → 检查配置是否有更新 → 返回(最新)实例
        2. 缓存未命中 → 从数据库热加载新机器人

        Args:
            callback_path: 回调路径,如 /weixin/callback/hr

        Returns:
            机器人实例,不存在返回None
        """
        # 1. 从缓存查找
        for bot in self.bots.values():
            if bot.callback_path == callback_path:
                # 检查配置是否有更新
                refreshed = self._check_and_refresh(bot.bot_key)
                return refreshed or bot

        # 2. 缓存未命中，尝试从数据库热加载新机器人
        bot_config = self.config_manager.load_bot_by_path(callback_path)
        if bot_config:
            try:
                bot_instance = BotInstance(bot_config)
                self.bots[bot_config.bot_key] = bot_instance
                self._last_check_time[bot_config.bot_key] = time.time()
                logger.info(f"🔥 热加载新机器人成功: {bot_instance}")
                return bot_instance
            except Exception as e:
                logger.error(f"热加载机器人 {bot_config.bot_key} 失败: {e}")

        return None

    def get_all_bots(self) -> Dict[str, BotInstance]:
        """获取所有机器人实例"""
        return self.bots

    def list_callback_paths(self) -> Dict[str, str]:
        """
        列出所有回调路径及其对应的bot_key

        Returns:
            {callback_path: bot_key} 字典
        """
        return {bot.callback_path: bot.bot_key for bot in self.bots.values()}

    def reload_config(self):
        """重新加载配置(全量热更新)"""
        logger.info("开始重新加载机器人配置...")
        self.config_manager._load_config()
        self.bots.clear()
        self._last_check_time.clear()
        self._initialize_bots()
        logger.info("机器人配置重新加载完成")

    def __repr__(self):
        return f"BotManager(bots={list(self.bots.keys())})"
