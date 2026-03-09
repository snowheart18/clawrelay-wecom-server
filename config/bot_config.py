"""
机器人配置管理器
从 YAML 配置文件加载机器人配置
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 默认配置文件路径
DEFAULT_CONFIG_PATH = Path(__file__).parent / "bots.yaml"


class BotConfig:
    """单个机器人的配置"""

    def __init__(
        self,
        bot_key: str,
        bot_id: str,
        secret: str = "",
        name: str = "",
        description: str = "",
        relay_url: str = "",
        working_dir: str = "",
        model: str = "",
        system_prompt: str = "",
        allowed_users: Optional[List[str]] = None,
        custom_commands: Optional[List[str]] = None,
        env_vars: Optional[Dict[str, str]] = None,
    ):
        self.bot_key = bot_key
        self.bot_id = bot_id
        self.secret = secret
        self.name = name
        self.description = description
        self.relay_url = relay_url
        self.working_dir = working_dir
        self.model = model
        self.system_prompt = system_prompt
        self.allowed_users = allowed_users or []
        self.custom_commands = custom_commands or []
        self.env_vars = env_vars or {}

    def __repr__(self):
        return (
            f"BotConfig(bot_key='{self.bot_key}', "
            f"name='{self.name}', "
            f"description='{self.description}')"
        )


class BotConfigManager:
    """机器人配置管理器 — 从 YAML 文件加载"""

    def __init__(self, config_path: str = ""):
        self.bots: Dict[str, BotConfig] = {}
        path = config_path or os.getenv("BOT_CONFIG_PATH", "") or str(DEFAULT_CONFIG_PATH)
        self._load_from_yaml(path)

    def _load_from_yaml(self, path: str):
        """从 YAML 文件加载配置"""
        config_file = Path(path)
        if not config_file.exists():
            logger.error("配置文件不存在: %s", config_file)
            return

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("读取配置文件失败: %s — %s", config_file, e)
            return

        bots_data = data.get("bots", {})
        if not bots_data:
            logger.warning("配置文件中没有找到 bots 配置: %s", config_file)
            return

        for bot_key, bot_data in bots_data.items():
            if not isinstance(bot_data, dict):
                continue

            bot_id = bot_data.get("bot_id", "")
            secret = bot_data.get("secret", "")

            if not bot_id or not secret:
                logger.warning("机器人 %s 配置不完整（需要 bot_id 和 secret），跳过", bot_key)
                continue

            bot_config = BotConfig(
                bot_key=bot_key,
                bot_id=bot_id,
                secret=secret,
                name=bot_data.get("name", ""),
                description=bot_data.get("description", ""),
                relay_url=bot_data.get("relay_url", ""),
                working_dir=bot_data.get("working_dir", ""),
                model=bot_data.get("model", ""),
                system_prompt=bot_data.get("system_prompt", ""),
                allowed_users=bot_data.get("allowed_users"),
                custom_commands=bot_data.get("custom_commands"),
                env_vars=bot_data.get("env_vars"),
            )
            self.bots[bot_key] = bot_config
            logger.info("加载机器人配置: %s", bot_config)

        logger.info("从配置文件加载 %d 个机器人: %s", len(self.bots), config_file)

    def get_bot(self, bot_key: str) -> Optional[BotConfig]:
        return self.bots.get(bot_key)

    def get_all_bots(self) -> Dict[str, BotConfig]:
        return self.bots
