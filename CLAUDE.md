# CLAUDE.md - Developer Guide

## Project Overview

ClawRelay WeCom Server - Enterprise WeChat bot server connecting to clawrelay-api for AI capabilities.

**Stack:** Python 3.12+ / asyncio / websockets / MySQL / SSE streaming

## Quick Commands

```bash
# Run locally
python main.py

# Docker Compose
docker compose up -d

# Run tests
pytest tests/ -v
```

## Architecture

```
Enterprise WeChat ←WSS→ main.py (asyncio) → clawrelay-api (Go :50009) → Claude Code CLI
```

**Key modules:**
- `main.py` — Entry point, bot lifecycle management
- `src/transport/ws_client.py` — WebSocket connection, heartbeat, reconnect
- `src/transport/message_dispatcher.py` — Message routing, throttled stream push
- `src/core/claude_relay_orchestrator.py` — SSE orchestration with clawrelay-api
- `src/adapters/claude_relay_adapter.py` — HTTP/SSE client adapter
- `src/handlers/command_handlers.py` — Built-in demo commands
- `src/utils/weixin_utils.py` — WeChat message builders and utilities

## Code Conventions

- Log messages in Chinese for business logs, English for technical logs
- Custom commands go in `src/handlers/custom/` with `register_commands()` entry point
- Environment variables loaded via `python-dotenv`, system env vars take priority

## Database

- MySQL 5.7+, charset `utf8mb4`
- Schema in `sql/init.sql`, demo data in `sql/seed.sql`
- Main tables: `robot_bots` (includes `secret` for WebSocket auth), `robot_sessions`, `robot_chat_logs`

## Security Rules

- Never expose environment variable values or API keys
- Never expose database credentials in logs or responses
