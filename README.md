# ClawRelay WeCom Server

企业微信 AI 机器人中转服务端 — 连接 [clawrelay-api](https://github.com/anthropics/clawrelay-api)，让 Claude Code 驱动你的企业微信机器人。

![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)
![License MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

---

## 它做什么？

用户在企业微信里发消息 → 本服务通过 WebSocket 长连接接收 → SSE 流式调用 clawrelay-api → Claude Code CLI 处理请求 → 实时推送回复到企业微信。

```
┌──────────┐    WSS     ┌─────────────────────────┐   SSE    ┌───────────────┐
│ 企业微信  │ ◄───────► │  ClawRelay WeCom Server  │ ──────► │ clawrelay-api │
│          │  长连接     │  (Python asyncio)        │ ◄────── │ (Go :50009)   │
└──────────┘             │                           │  流式响应 └───────┬───────┘
                         │  • WebSocket 长连接       │                   │
                         │  • 多机器人管理            │                   ▼
                         │  • 流式思考展示            │          ┌───────────────┐
                         │  • 会话 & 日志管理         │          │ Claude Code   │
                         └─────────────────────────┘          │ CLI           │
                                                               └───────────────┘
```

## 功能特性

| 特性 | 说明 |
|------|------|
| **WebSocket 长连接** | 无需公网 IP、回调 URL，通过 WSS 连接企业微信 |
| **零数据库依赖** | YAML 配置文件 + 内存会话 + JSONL 日志，开箱即用 |
| **多机器人管理** | 一个服务托管多个机器人，YAML 配置即加即用 |
| **流式推送** | 500ms 节流推送，实时展示 AI 回复 |
| **会话管理** | 自动过期（2h），支持 `reset` / `new` 重置会话 |
| **多模态** | 文本 / 图片 / 语音 / 文件 / 图文混排 |
| **自定义命令** | 模块化扩展，动态加载 |
| **用户白名单** | 按机器人维度的访问控制 |
| **聊天日志** | JSONL 格式记录到 `logs/chat.jsonl` |

## 前置条件

- Python 3.12+
- [clawrelay-api](https://github.com/anthropics/clawrelay-api) 运行在 50009 端口
- 企业微信管理员账号（创建智能机器人，获取 bot_id 和 secret）

---

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
git clone https://github.com/wxkingstar/clawrelay-wecom-server.git
cd clawrelay-wecom-server

# 编辑配置文件，填入机器人凭据
cp config/bots.yaml config/bots.yaml  # 已包含示例，直接编辑即可
vim config/bots.yaml

# 一键启动
docker compose up -d
```

> 应用通过 `host.docker.internal` 连接宿主机上的 clawrelay-api。

```bash
# 查看日志
docker compose logs -f app

# 停止
docker compose down
```

### 方式二：手动部署

```bash
# 1. 克隆并安装依赖
git clone https://github.com/wxkingstar/clawrelay-wecom-server.git
cd clawrelay-wecom-server
pip install -r requirements.txt

# 2. 配置机器人
vim config/bots.yaml  # 填入 bot_id、secret、relay_url

# 3. 启动
python main.py
```

---

## 机器人配置

编辑 `config/bots.yaml`：

```yaml
bots:
  my_bot:
    # [必填] 企业微信机器人凭据
    bot_id: "YOUR_BOT_ID"
    secret: "YOUR_BOT_SECRET"

    # [必填] clawrelay-api 地址
    relay_url: "http://localhost:50009"

    # [可选]
    name: "My Bot"                    # 机器人名称（用于过滤群聊 @提及）
    description: "My AI assistant"    # 描述
    working_dir: "/path/to/project"   # Claude 工作目录
    model: "claude-sonnet-4-6"        # 模型名称
    system_prompt: "You are a helpful assistant."

    # [可选] 用户白名单（不设置 = 不限制）
    allowed_users:
      - "user_id_1"
      - "user_id_2"

    # [可选] 注入 Claude 子进程的环境变量
    env_vars:
      MY_API_KEY: "xxx"

    # [可选] 自定义命令模块
    custom_commands:
      - "src.handlers.custom.demo_commands"
```

### 配置字段说明

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `bot_id` | ✅ | 企业微信 bot_id |
| `secret` | ✅ | 企业微信机器人 secret |
| `relay_url` | ✅ | clawrelay-api 地址 |
| `name` | | 机器人名称（群聊中过滤 @提及） |
| `working_dir` | | Claude 工作目录 |
| `model` | | 模型名称 |
| `system_prompt` | | 系统提示词 |
| `allowed_users` | | 用户白名单（列表，不设 = 不限制） |
| `env_vars` | | 环境变量注入（key-value 映射） |
| `custom_commands` | | 自定义命令模块路径列表 |

---

## 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_CONFIG_PATH` | 机器人配置文件路径 | `config/bots.yaml` |
| `CHAT_LOG_DIR` | 聊天日志目录 | `logs` |
| `WEIXIN_AGENT_TIMEOUT_SECONDS` | 任务超时时间（秒） | `30` |
| `WEIXIN_MAX_FILE_SIZE` | 文件上传大小限制（字节） | `20971520` (20MB) |

---

## 自定义命令

通过模块化机制扩展机器人命令：

**1.** 在 `src/handlers/custom/` 下创建 Python 文件：

```python
from src.handlers.command_handlers import CommandHandler

class PingCommandHandler(CommandHandler):
    command = "ping"
    description = "Check if the bot is alive"

    def handle(self, cmd, stream_id, user_id):
        return "Pong!", None

def register_commands(command_router):
    command_router.register(PingCommandHandler())
```

**2.** 在 `config/bots.yaml` 中配置模块路径：

```yaml
bots:
  my_bot:
    custom_commands:
      - "src.handlers.custom.my_commands"
```

**3.** 重启服务生效。

参考示例：[`src/handlers/custom/demo_commands.py`](src/handlers/custom/demo_commands.py)

---

## 消息处理流程

```
用户发送消息
    │
    ▼
企业微信 WebSocket 推送 (aibot_msg_callback)
    │
    ▼
消息路由 ─── text ────► 命令检查 ─── 匹配 ──► 执行命令（reset, help, 自定义...）
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
    │                        │       ├── ThinkingDelta → 记录思考过程
    │                        │       └── ToolUseStart → 记录工具调用
    │                        ├── 500ms 节流推送 (aibot_respond_msg)
    │                        └── 记录聊天日志 (JSONL)
    │
    ├── voice ──► 提取转文字 → 同 text 流程
    ├── image ──► 解密图片 → 多模态分析
    ├── file  ──► 解密文件 → 内容分析
    ├── mixed ──► 图文分离 → 多模态分析
    └── event ──► 欢迎语 / 模板卡片事件
```

---

## 项目结构

```
clawrelay-wecom-server/
├── main.py                             # 应用入口（asyncio，per-bot WebSocket）
├── Dockerfile
├── docker-compose.yml                  # 一键启动
├── requirements.txt
├── .env.example                        # 环境变量模板
├── LICENSE
│
├── config/
│   ├── bots.yaml                      # 机器人配置（YAML）
│   └── bot_config.py                  # 配置加载器
│
├── src/
│   ├── adapters/
│   │   └── claude_relay_adapter.py    # clawrelay-api SSE 客户端
│   │
│   ├── transport/
│   │   ├── ws_client.py               # WebSocket 连接、心跳、重连
│   │   └── message_dispatcher.py      # 消息路由、节流流式推送
│   │
│   ├── core/
│   │   ├── claude_relay_orchestrator.py # AI 调用编排（核心）
│   │   ├── session_manager.py           # 会话管理（内存、2h 过期）
│   │   ├── chat_logger.py              # 聊天日志（JSONL 文件）
│   │   └── task_registry.py            # 异步任务注册表
│   │
│   ├── handlers/
│   │   ├── command_handlers.py        # 内置命令（help, reset 等）
│   │   └── custom/
│   │       └── demo_commands.py       # 自定义命令示例
│   │
│   └── utils/
│       ├── weixin_utils.py            # 消息构建器 & 文件解密工具
│       ├── text_utils.py              # 文本处理
│       └── logging_config.py          # 日志配置
│
├── logs/                              # 聊天日志（chat.jsonl）
└── tests/
```

---

## 关键设计

### WebSocket 长连接

通过 `wss://openws.work.weixin.qq.com` 建立长连接，无需公网 IP 和回调 URL。每个机器人一个独立连接，30s 心跳保活，断线指数退避自动重连。

### 会话管理

每个用户-机器人对维护独立会话。会话的 `relay_session_id` 存储在内存中，2 小时自动过期。进程重启后自动创建新会话（历史消息由 clawrelay-api 维护）。用户可发送 `reset` / `new` 手动重置。

### 多机器人隔离

每个机器人有独立的 WebSocket 连接、命令路由器、会话管理和配置。在 `config/bots.yaml` 中添加新 bot 配置，重启即生效。

---

## License

[MIT](LICENSE)
