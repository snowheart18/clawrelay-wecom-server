-- ============================================================
-- ClawRelay WeCom Server - Example Seed Data
--
-- This file inserts a demo bot configuration so the server
-- can start immediately after docker compose up.
--
-- IMPORTANT: Replace the placeholder values below with your
-- actual Enterprise WeChat bot credentials before use.
-- ============================================================

USE clawrelay_wecom;

INSERT INTO robot_bots (
    bot_key,
    bot_id,
    token,
    encoding_aes_key,
    callback_path,
    name,
    description,
    llm_type,
    relay_url,
    working_dir,
    model,
    system_prompt,
    custom_command_modules,
    enabled
) VALUES (
    'demo',
    'REPLACE_WITH_YOUR_BOT_ID',
    'REPLACE_WITH_YOUR_TOKEN',
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq',
    '/weixin/callback/demo',
    'Demo Bot',
    'ClawRelay Demo Assistant',
    'claude_relay',
    'http://host.docker.internal:50009',
    '/workspace',
    'claude-sonnet-4-6',
    'You are a helpful AI assistant powered by ClawRelay.',
    '["src.handlers.custom.demo_commands"]',
    1
);

SELECT CONCAT('Seed data inserted: ', COUNT(*), ' bot(s)') AS message FROM robot_bots;
