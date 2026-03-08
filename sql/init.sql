-- ============================================================
-- ClawRelay WeChat Bot Server - Database Schema
--
-- Combined init script for all required tables.
-- Usage:
--   mysql -h <host> -u <user> -p <database> < sql/init.sql
-- ============================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ============================================================
-- Table 1: robot_bots (Bot instances)
-- Stores bot configuration including WeChat credentials and
-- LLM/relay settings.
-- ============================================================

CREATE TABLE IF NOT EXISTS `robot_bots` (
  `id` INT(11) NOT NULL AUTO_INCREMENT COMMENT 'Bot ID (primary key)',
  `bot_key` VARCHAR(50) NOT NULL COMMENT 'Unique bot identifier (used for routing, e.g. default/support)',
  `bot_id` VARCHAR(100) NOT NULL COMMENT 'Enterprise WeChat bot_id',
  `token` VARCHAR(100) NOT NULL COMMENT 'Enterprise WeChat token',
  `encoding_aes_key` VARCHAR(50) NOT NULL COMMENT 'Enterprise WeChat AES key (43 chars)',
  `name` VARCHAR(100) DEFAULT NULL COMMENT 'Bot display name',
  `department` VARCHAR(50) DEFAULT NULL COMMENT 'Department',
  `callback_path` VARCHAR(200) DEFAULT NULL COMMENT 'Callback path (e.g. /weixin/callback/sales)',
  `description` TEXT COMMENT 'Bot description',
  `llm_type` VARCHAR(50) DEFAULT 'claude_relay' COMMENT 'LLM backend type (e.g. claude_relay)',
  `relay_url` VARCHAR(500) DEFAULT '' COMMENT 'ClawRelay API service URL',
  `working_dir` VARCHAR(500) DEFAULT '' COMMENT 'Claude working directory path',
  `model` VARCHAR(100) DEFAULT 'claude-sonnet-4-6' COMMENT 'Model name',
  `system_prompt` TEXT COMMENT 'System prompt (sent to LLM as system message)',
  `allowed_users` JSON DEFAULT NULL COMMENT 'User whitelist (JSON array, e.g. ["user1","user2"], NULL = no restriction)',
  `custom_command_modules` JSON DEFAULT NULL COMMENT 'Custom command modules (JSON array)',
  `env_vars` JSON DEFAULT NULL COMMENT 'Environment variables injected into Claude subprocess (JSON object)',
  `enabled` TINYINT(1) DEFAULT 1 COMMENT 'Whether enabled (1=yes, 0=no)',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Created at',
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Updated at',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_bot_key` (`bot_key`),
  KEY `idx_enabled` (`enabled`),
  KEY `idx_callback_path` (`callback_path`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Bot instances table';


-- ============================================================
-- Table 2: robot_bot_tool_permissions (Tool permissions)
-- Defines which tool categories each bot can use.
-- ============================================================

CREATE TABLE IF NOT EXISTS `robot_bot_tool_permissions` (
  `id` INT(11) NOT NULL AUTO_INCREMENT COMMENT 'Permission ID (primary key)',
  `bot_id` INT(11) NOT NULL COMMENT 'Bot ID (foreign key to robot_bots)',
  `tool_category` VARCHAR(50) NOT NULL COMMENT 'Tool category',
  `enabled` TINYINT(1) DEFAULT 1 COMMENT 'Whether enabled (1=yes, 0=no)',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Created at',
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Updated at',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_bot_category` (`bot_id`, `tool_category`),
  KEY `idx_bot_id` (`bot_id`),
  KEY `idx_tool_category` (`tool_category`),
  CONSTRAINT `fk_robot_bot_tool_bot` FOREIGN KEY (`bot_id`) REFERENCES `robot_bots` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Tool permissions table';


-- ============================================================
-- Table 3: robot_sessions (Chat sessions)
-- Stores conversation context between users and bots.
-- ============================================================

CREATE TABLE IF NOT EXISTS `robot_sessions` (
  `session_id` VARCHAR(100) NOT NULL COMMENT 'Session ID (format: bot_key_user_id)',
  `bot_id` VARCHAR(50) NOT NULL COMMENT 'Bot identifier',
  `user_id` VARCHAR(100) NOT NULL COMMENT 'Enterprise WeChat user_id',
  `context` JSON DEFAULT NULL COMMENT 'Conversation history (JSON array of role/content pairs)',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Created at',
  `last_active_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Last active at',

  PRIMARY KEY (`session_id`),
  KEY `idx_bot_user` (`bot_id`, `user_id`),
  KEY `idx_last_active` (`last_active_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Chat sessions table';


-- ============================================================
-- Table 4: robot_chat_logs (Chat logs)
-- Records user questions and AI replies for monitoring and audit.
-- ============================================================

CREATE TABLE IF NOT EXISTS `robot_chat_logs` (
  `id` BIGINT(20) NOT NULL AUTO_INCREMENT COMMENT 'Log ID',

  -- Request info
  `bot_key` VARCHAR(50) NOT NULL COMMENT 'Bot identifier',
  `user_id` VARCHAR(100) NOT NULL COMMENT 'Enterprise WeChat user_id',
  `user_email` VARCHAR(100) DEFAULT NULL COMMENT 'User email (for audit)',
  `user_name` VARCHAR(100) DEFAULT NULL COMMENT 'User name',
  `chat_type` VARCHAR(20) DEFAULT 'single' COMMENT 'Chat type (single/group)',
  `chat_id` VARCHAR(100) DEFAULT NULL COMMENT 'Group chat ID (null for direct messages)',
  `session_key` VARCHAR(100) DEFAULT NULL COMMENT 'Session key (user_id for DM, chatid for group)',
  `relay_session_id` VARCHAR(100) DEFAULT NULL COMMENT 'ClawRelay session ID',
  `stream_id` VARCHAR(200) DEFAULT NULL COMMENT 'Stream message ID',

  -- Message content
  `message_type` VARCHAR(20) NOT NULL DEFAULT 'text' COMMENT 'Message type (text/voice/file/image/mixed)',
  `message_content` TEXT COMMENT 'Original user message content',
  `quoted_content` TEXT COMMENT 'Quoted message content (if any)',
  `file_info` JSON DEFAULT NULL COMMENT 'File info (filename/size/type for file messages)',

  -- Response content
  `response_content` MEDIUMTEXT COMMENT 'Full AI response content',
  `tools_used` JSON DEFAULT NULL COMMENT 'List of tools called',

  -- Performance monitoring
  `status` VARCHAR(20) NOT NULL DEFAULT 'success' COMMENT 'Status (success/error/timeout)',
  `error_message` TEXT COMMENT 'Error message (when status != success)',
  `latency_ms` INT(11) DEFAULT NULL COMMENT 'Total processing time (milliseconds)',
  `request_at` DATETIME(3) NOT NULL COMMENT 'Request arrival time (ms precision)',
  `response_at` DATETIME(3) DEFAULT NULL COMMENT 'Response completion time',

  -- Audit flags
  `content_flags` JSON DEFAULT NULL COMMENT 'Content flags (sensitive word hits, anomalous ops, etc.)',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Record created at',

  PRIMARY KEY (`id`),
  KEY `idx_user_time` (`user_id`, `request_at`),
  KEY `idx_bot_time` (`bot_key`, `request_at`),
  KEY `idx_session` (`relay_session_id`),
  KEY `idx_status_time` (`status`, `request_at`),
  KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Chat logs table (monitoring + audit)';


-- ============================================================
-- Restore foreign key checks
-- ============================================================

SET FOREIGN_KEY_CHECKS = 1;


-- ============================================================
-- Example: Insert a bot configuration
-- INSERT INTO robot_bots (bot_key, bot_id, token, encoding_aes_key, callback_path, name, description, llm_type, relay_url, working_dir, model, system_prompt, enabled)
-- VALUES ('my_bot', 'YOUR_BOT_ID', 'YOUR_TOKEN', 'YOUR_AES_KEY_43_CHARS', '/weixin/callback/my_bot', 'My Bot', 'My AI assistant', 'claude_relay', 'http://localhost:50009', '/path/to/working/dir', 'claude-sonnet-4-6', 'You are a helpful assistant.', 1);
-- ============================================================

SELECT 'Database schema initialization complete!' AS message;
