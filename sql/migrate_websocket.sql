-- WebSocket long connection migration
-- Add secret field, make HTTP callback fields nullable

ALTER TABLE robot_bots ADD COLUMN `secret` VARCHAR(200) DEFAULT NULL
  COMMENT 'WebSocket long connection secret' AFTER `encoding_aes_key`;

ALTER TABLE robot_bots MODIFY COLUMN `token` VARCHAR(100) DEFAULT NULL
  COMMENT 'Enterprise WeChat token (deprecated, HTTP callback mode)';

ALTER TABLE robot_bots MODIFY COLUMN `encoding_aes_key` VARCHAR(50) DEFAULT NULL
  COMMENT 'Enterprise WeChat AES key (deprecated, HTTP callback mode)';

ALTER TABLE robot_bots MODIFY COLUMN `callback_path` VARCHAR(200) DEFAULT NULL
  COMMENT 'Callback path (deprecated, HTTP callback mode)';
