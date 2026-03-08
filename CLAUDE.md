# CLAUDE.md - Developer Guide

## Project Overview

ClawRelay WeCom Server - Enterprise WeChat bot server connecting to clawrelay-api for AI capabilities.

**Stack:** Python 3.12+ / FastAPI / MySQL / SSE streaming

## Quick Commands

```bash
# Run locally
python app.py

# Docker Compose
docker compose up -d

# Run tests
pytest tests/ -v
```

## Architecture

```
Enterprise WeChat → FastAPI (:5000) → clawrelay-api (Go :50009) → Claude Code CLI
```

**Key modules:**
- `app.py` — FastAPI routes and entry point
- `src/bot/bot_manager.py` — Multi-bot factory (loads config from MySQL)
- `src/bot/bot_instance.py` — Per-bot crypto, stream manager, message routing
- `src/core/claude_relay_orchestrator.py` — SSE orchestration with clawrelay-api
- `src/adapters/claude_relay_adapter.py` — HTTP/SSE client adapter
- `src/handlers/message_handlers.py` — Message type routing (text/voice/file/image/event)
- `src/handlers/command_handlers.py` — Built-in demo commands
- `src/utils/weixin_utils.py` — WeChat message builders and stream manager

## Code Conventions

- Log messages in Chinese for business logs, English for technical logs
- All WeChat message encryption/decryption via `src/utils/message_crypto.py`
- Custom commands go in `src/handlers/custom/` with `register_commands()` entry point
- Environment variables loaded via `python-dotenv`, system env vars take priority

## Database

- MySQL 5.7+, charset `utf8mb4`
- Schema in `sql/init.sql`, demo data in `sql/seed.sql`
- Main tables: `robot_bots`, `robot_sessions`, `robot_chat_logs`

## Security Rules

- Never expose environment variable values or API keys
- Never expose database credentials in logs or responses
