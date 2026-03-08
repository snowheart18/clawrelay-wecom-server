# ClawRelay WeCom Server

企业微信 AI 机器人中转服务端 — 连接 [clawrelay-api](https://github.com/anthropics/clawrelay-api)，让 Claude Code 驱动你的企业微信机器人。

![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.119+-green.svg)
![License MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

---

## 它做什么？

用户在企业微信里发消息 → 本服务接收并解密 → 通过 SSE 流式调用 clawrelay-api → Claude Code CLI 处理请求 → 实时推送思考过程和回复到企业微信。

```
┌──────────┐   callback   ┌─────────────────────────┐   SSE    ┌───────────────┐
│ 企业微信  │ ──────────► │  ClawRelay WeCom Server  │ ──────► │ clawrelay-api │
│          │ ◄────────── │  (FastAPI :5000)          │ ◄────── │ (Go :50009)   │
└──────────┘  加密回复    │                           │  流式响应 └───────┬───────┘
                          │  • 消息加解密              │                   │
                          │  • 多机器人管理            │                   ▼
                          │  • 流式思考展示            │          ┌───────────────┐
                          │  • 会话 & 日志管理         │          │ Claude Code   │
                          └─────────────────────────┘          │ CLI           │
                                                                └───────────────┘
```

## 功能特性

| 特性 | 说明 |
|------|------|
| **多机器人管理** | 工厂模式，一个服务托管多个机器人，支持热重载 |
| **流式思考过程** | 实时展示 AI 推理步骤，利用企业微信流式消息刷新 |
| **消息加解密** | 基于企业微信官方 SDK，开箱即用 |
| **会话管理** | 自动过期（2h），支持 `/new` 重置会话 |
| **多模态** | 文本 / 图片 / 语音 / 文件消息处理 |
| **AI 主动提问** | AskUserQuestion 交互卡片，支持多选投票 |
| **自定义命令** | 模块化扩展，动态加载 |
| **用户白名单** | 按机器人维度的访问控制 |
| **聊天日志** | 完整记录用户问题与 AI 回复，便于审计 |

## 前置条件

- Python 3.12+
- MySQL 5.7+
- [clawrelay-api](https://github.com/anthropics/clawrelay-api) 运行在 50009 端口
- 企业微信管理员账号（配置机器人回调）

---

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
git clone https://github.com/wxkingstar/clawrelay-wecom-server.git
cd clawrelay-wecom

# 一键启动（含 MySQL）
docker compose up -d
```

启动后：
- 应用服务：`http://localhost:5000`
- MySQL：`localhost:3306`（用户 `clawrelay` / 密码 `clawrelay123`）
- 自动建表 + 插入 demo 机器人配置

> **注意**：demo 机器人使用占位符凭据，需要在数据库中替换为真实的企业微信凭据。
> 应用通过 `host.docker.internal` 连接宿主机上的 clawrelay-api。

```bash
# 查看日志
docker compose logs -f app

# 停止
docker compose down

# 停止并清除数据
docker compose down -v
```

### 方式二：手动部署

```bash
# 1. 克隆并安装依赖
git clone https://github.com/wxkingstar/clawrelay-wecom-server.git
cd clawrelay-wecom
pip install -r requirements.txt

# 2. 初始化数据库
mysql -u root -p -e "CREATE DATABASE clawrelay_wecom CHARACTER SET utf8mb4;"
mysql -u root -p clawrelay_wecom < sql/init.sql

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，填入数据库连接信息

# 4. 配置机器人（见下方"机器人配置"节）

# 5. 启动
python app.py
```

---

## 机器人配置

在数据库 `robot_bots` 表中插入机器人配置：

```sql
INSERT INTO robot_bots (
    bot_key, bot_id, token, encoding_aes_key,
    callback_path, name, description,
    llm_type, relay_url, working_dir, model,
    system_prompt, enabled
) VALUES (
    'my_bot',                               -- 唯一标识，用于路由
    'YOUR_BOT_ID',                          -- 企业微信 bot_id
    'YOUR_TOKEN',                           -- 企业微信 Token
    'YOUR_AES_KEY_43_CHARS',                -- 企业微信 EncodingAESKey（43位）
    '/weixin/callback/my_bot',              -- 回调路径
    'My Bot', 'My AI assistant',
    'claude_relay',
    'http://localhost:50009',               -- clawrelay-api 地址
    '/path/to/working/dir',                 -- Claude 工作目录
    'claude-sonnet-4-6',                    -- 模型
    'You are a helpful assistant.',         -- 系统提示词
    1                                       -- 启用
);
```

然后在企业微信管理后台设置回调 URL：

```
http://your-server:5000/weixin/callback/my_bot
```

### 配置字段说明

| 字段 | 说明 |
|------|------|
| `bot_key` | 机器人唯一标识（用于路由，如 `default`、`demo`） |
| `bot_id` | 企业微信 bot_id |
| `token` | 企业微信 Token |
| `encoding_aes_key` | 企业微信 EncodingAESKey（43 位） |
| `callback_path` | 回调路径（如 `/weixin/callback/my_bot`） |
| `relay_url` | clawrelay-api 地址 |
| `working_dir` | Claude 工作目录 |
| `model` | 模型名称 |
| `system_prompt` | 系统提示词 |
| `allowed_users` | 用户白名单（JSON 数组，`NULL` = 不限制） |
| `custom_command_modules` | 自定义命令模块（JSON 数组） |
| `env_vars` | 注入 Claude 子进程的环境变量（JSON 对象） |

---

## 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `PORT` | 服务端口 | `5000` |
| `ENVIRONMENT` | 运行环境 (`development` / `production`) | `production` |
| `DB_HOST` | MySQL 主机 | `localhost` |
| `DB_PORT` | MySQL 端口 | `3306` |
| `DB_DATABASE` | 数据库名 | `clawrelay_wecom` |
| `DB_USERNAME` | 数据库用户名 | `root` |
| `DB_PASSWORD` | 数据库密码 | — |
| `ADMIN_API_KEY` | 管理接口 API Key（未设置则自动生成） | — |
| `WEIXIN_ENABLE_THINKING` | 启用思考过程展示 | `true` |
| `WEIXIN_AGENT_ASYNC_MODE` | 异步模式（后台处理长耗时请求） | `true` |
| `WEIXIN_AGENT_TIMEOUT_SECONDS` | 同步模式超时时间（秒） | `30` |
| `WEIXIN_MAX_FILE_SIZE` | 文件上传大小限制（字节） | `20971520` (20MB) |

---

## API 接口

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| GET | `/weixin/callback/{bot_key}` | 企业微信 URL 验证 | — |
| POST | `/weixin/callback/{bot_key}` | 企业微信消息回调 | — |
| GET | `/api/bots` | 查询所有机器人 | `X-API-Key` |
| POST | `/api/reload` | 热重载配置 | `X-API-Key` |
| GET | `/health` | 健康检查 | — |

Development 模式额外接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/test` | 测试页面 |
| POST | `/api/test/message` | 模拟发送消息 |
| GET | `/api/logs` | 实时日志流 |

---

## 自定义命令

通过模块化机制扩展机器人命令：

**1.** 在 `src/handlers/custom/` 下创建 Python 文件：

```python
from src.handlers.command_handlers import CommandHandler
from src.utils.weixin_utils import StreamManager

class PingCommandHandler(CommandHandler):
    command = "ping"
    description = "Check if the bot is alive"

    def handle(self, cmd, stream_id, user_id):
        return "Pong!", None

def register_commands(command_router):
    command_router.register(PingCommandHandler())
```

**2.** 在数据库中配置模块路径：

```sql
UPDATE robot_bots
SET custom_command_modules = '["src.handlers.custom.my_commands"]'
WHERE bot_key = 'my_bot';
```

**3.** 调用热重载接口生效：

```bash
curl -X POST http://localhost:5000/api/reload -H "X-API-Key: YOUR_KEY"
```

参考示例：[`src/handlers/custom/demo_commands.py`](src/handlers/custom/demo_commands.py)

---

## 消息处理流程

```
用户发送消息
    │
    ▼
企业微信推送回调 (POST /weixin/callback/{bot_key})
    │
    ▼
消息解密 (WXBizJsonMsgCrypt)
    │
    ▼
消息路由 ─── text ────► 命令检查 ─── 匹配 ──► 执行命令（/new, /help, 自定义...）
    │                        │
    │                     不匹配
    │                        │
    │                        ▼
    │              ClaudeRelayOrchestrator
    │                        │
    │                        ├── 获取/创建会话 (SessionManager)
    │                        ├── 注入安全提示词 + 用户上下文
    │                        ├── SSE 流式调用 clawrelay-api
    │                        │       ├── TextDelta → 累积回复文本
    │                        │       ├── ThinkingDelta → 实时推送思考过程
    │                        │       ├── ToolUseStart → 展示工具调用
    │                        │       └── AskUserQuestion → 构建投票卡片
    │                        ├── 保存会话历史
    │                        └── 记录聊天日志
    │
    ├── voice ──► 提取转文字 → 同 text 流程
    ├── image ──► 解密图片 → 多模态分析
    ├── file  ──► 解密文件 → 内容分析
    ├── mixed ──► 图文分离 → 多模态分析
    └── event ──► 模板卡片事件 / AskUserQuestion 回调
```

---

## 项目结构

```
clawrelay-wecom/
├── app.py                              # 应用入口，FastAPI 路由
├── Dockerfile
├── docker-compose.yml                  # 一键启动（含 MySQL）
├── requirements.txt
├── .env.example                        # 环境变量模板
├── LICENSE
│
├── config/
│   └── bot_config.py                   # 机器人配置数据模型 & 数据库加载
│
├── src/
│   ├── adapters/
│   │   └── claude_relay_adapter.py     # clawrelay-api SSE 客户端
│   │
│   ├── bot/
│   │   ├── bot_instance.py             # 单个机器人实例（加解密、消息路由）
│   │   └── bot_manager.py              # 多机器人管理器（工厂模式、热重载）
│   │
│   ├── core/
│   │   ├── claude_relay_orchestrator.py  # AI 调用编排（核心）
│   │   ├── session_manager.py            # 会话管理（MySQL 持久化、2h 过期）
│   │   ├── streaming_thinking_manager.py # 流式思考过程推送
│   │   ├── thinking_collector.py         # 思考内容收集 & 格式化
│   │   ├── choice_manager.py             # AskUserQuestion 交互管理
│   │   ├── chat_logger.py                # 聊天日志（异步写入）
│   │   └── task_registry.py              # 异步任务注册表
│   │
│   ├── handlers/
│   │   ├── command_handlers.py         # 内置命令（help, /new 等）
│   │   ├── message_handlers.py         # 消息类型路由 & 处理
│   │   └── custom/
│   │       └── demo_commands.py        # 自定义命令示例
│   │
│   └── utils/
│       ├── crypto_libs/                # 企业微信官方加解密 SDK
│       ├── database.py                 # MySQL 连接工具
│       ├── message_crypto.py           # 消息加解密封装
│       ├── weixin_utils.py             # 消息构建器 & 流式管理器
│       ├── text_utils.py               # 文本处理（think 标签清理等）
│       └── logging_config.py           # 日志配置
│
├── sql/
│   ├── init.sql                        # 建表脚本（4 张表）
│   └── seed.sql                        # Demo 机器人种子数据
│
├── static/                             # 静态文件（首页、测试页）
└── tests/
```

---

## 关键设计

### 流式思考过程展示

当 Claude 在"思考"时（ThinkingDelta 事件），服务会利用企业微信的流式消息刷新机制，实时将思考步骤推送给用户。用户能看到 AI 正在做什么（分析问题、调用工具、读取文件...），而不是等待一个空白的加载状态。

### 会话管理

每个用户-机器人对维护独立会话。会话的 `relay_session_id` 持久化到 MySQL，确保服务重启后会话不丢失。会话 2 小时自动过期，用户也可发送 `/new` 手动重置。

### 多机器人隔离

每个机器人有独立的加解密实例、命令路由器、会话管理和配置。通过回调路径 (`/weixin/callback/{bot_key}`) 路由到对应的机器人实例。支持运行时热重载配置（`POST /api/reload`）。

---

## License

[MIT](LICENSE)
