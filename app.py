#!/usr/bin/env python
# coding=utf-8
"""
Enterprise WeChat Bot Server (Multi-bot version)
Supports configuring and managing multiple Enterprise WeChat bots
"""

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import uvicorn
import os
import logging
import random
import string
import time
import secrets
from dotenv import load_dotenv

# Import bot manager
from src.bot.bot_manager import BotManager

# Version
VERSION = "v1.0.0"

# Load environment variables (system env vars take priority, .env as fallback)
load_dotenv(override=False)

app = FastAPI()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Enable concise business logging
from src.utils.logging_config import setup_business_logging
setup_business_logging()

# Print version info
logger.info("=" * 60)
logger.info(f"WeChat Bot Server Starting - {VERSION}")
logger.info("=" * 60)

# Get runtime environment
ENVIRONMENT = os.getenv('ENVIRONMENT', 'production').lower()
env_name = "Development (write ops simulated only)" if ENVIRONMENT == 'development' else "Production"
logger.info(f"Environment: {env_name}")
logger.info("=" * 60)

# ============ Admin API Authentication ============
# Management endpoints (e.g., /api/bots, /api/reload) require API Key authentication
# Set via ADMIN_API_KEY environment variable; auto-generated if not set
ADMIN_API_KEY = os.getenv('ADMIN_API_KEY', '')
if not ADMIN_API_KEY:
    ADMIN_API_KEY = secrets.token_urlsafe(32)
    logger.warning(f"ADMIN_API_KEY not set, auto-generated temporary key: {ADMIN_API_KEY}")
    logger.warning("Please set a fixed key via the ADMIN_API_KEY environment variable")
else:
    logger.info("Admin API Key configured")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_admin_api_key(api_key: str = Depends(api_key_header)):
    """Verify admin API Key"""
    if not api_key or not secrets.compare_digest(api_key, ADMIN_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    return api_key


# Initialize bot manager (load config from database)
logger.info("Initializing bot manager...")
try:
    bot_manager = BotManager()
    logger.info("Bot manager initialized successfully")
except Exception as e:
    logger.error(f"Bot manager initialization failed: {e}", exc_info=True)
    raise SystemExit(f"Cannot start service: bot manager initialization failed - {e}")

# Print all registered bots
logger.info("=" * 60)
logger.info("Registered bots:")
all_bots = bot_manager.get_all_bots()
if not all_bots:
    logger.warning("No bots registered! Please configure bots in the database and call POST /api/reload")

for bot_key, bot in all_bots.items():
    logger.info(f"  - {bot_key}: {bot.callback_path} ({bot.config.description})")
logger.info("=" * 60)


def generate_stream_id(bot_key: str = "", user_id: str = "") -> str:
    """Generate a secure stream_id to ensure session isolation between users

    Args:
        bot_key: Bot identifier (optional, for multi-bot isolation)
        user_id: User ID (optional, for multi-user isolation)

    Returns:
        Format: bot:{bot_key}|user:{user_id}|ts:{timestamp}|rnd:{random}
        - If both bot_key and user_id are empty, uses pure random mode (backward compat)
        - Uses | as separator to avoid conflicts with bot_key or user_id characters

    Example:
        >>> generate_stream_id("default", "user123")
        'bot:default|user:user123|ts:1705392847|rnd:aB3xY9'
        >>> generate_stream_id("support", "user456")
        'bot:support|user:user456|ts:1705392847|rnd:xY8zW2'
    """
    if not bot_key and not user_id:
        # Backward compatible: pure random mode (not recommended)
        return ''.join(random.choices(string.ascii_letters + string.digits, k=10))

    # Secure mode: includes bot, user, timestamp, random suffix
    timestamp = str(int(time.time()))
    random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=6))

    parts = []
    if bot_key:
        parts.append(f"bot:{bot_key}")
    if user_id:
        parts.append(f"user:{user_id}")
    parts.append(f"ts:{timestamp}")
    parts.append(f"rnd:{random_suffix}")

    return '|'.join(parts)


@app.get("/weixin/callback")
@app.get("/weixin/callback/{bot_key:path}")
async def verify_url(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
    bot_key: str = ""
):
    """
    Verify URL validity
    Called by Enterprise WeChat when configuring callback URL

    Supports two path patterns:
    - /weixin/callback (default bot)
    - /weixin/callback/{bot_key} (specified bot)
    """
    # Construct full callback path
    if bot_key:
        callback_path = f"/weixin/callback/{bot_key}"
    else:
        callback_path = "/weixin/callback"

    # Get bot instance by path
    bot = bot_manager.get_bot_by_path(callback_path)
    if bot is None:
        logger.error(f"No bot found for callback path: {callback_path}")
        return Response(content="bot not found", media_type="text/plain")

    logger.info(f"Processing URL verification: bot_key={bot.bot_key}, path={callback_path}")

    # Verify URL
    decrypted_echostr = bot.verify_url(msg_signature, timestamp, nonce, echostr)

    if decrypted_echostr is None:
        return Response(content="verify fail", media_type="text/plain")

    return Response(content=decrypted_echostr, media_type="text/plain")


@app.post("/weixin/callback")
@app.post("/weixin/callback/{bot_key:path}")
async def handle_message(
    request: Request,
    msg_signature: str = None,
    timestamp: str = None,
    nonce: str = None,
    bot_key: str = ""
):
    """
    Handle messages pushed by Enterprise WeChat

    Supports two path patterns:
    - /weixin/callback (default bot)
    - /weixin/callback/{bot_key} (specified bot)

    Important: Must always return a response to Enterprise WeChat regardless of
    success or failure, otherwise it will retry (up to 3 times)
    """
    # Import MessageBuilder (at function start for error handling)
    from src.utils.weixin_utils import MessageBuilder

    # Temporary stream_id (for error handling, replaced with real one later)
    temp_stream_id = generate_stream_id()

    try:
        # 1. Parameter validation
        if not all([msg_signature, timestamp, nonce]):
            logger.error("Missing required parameters: msg_signature, timestamp or nonce")
            return Response(content="success", media_type="text/plain")

        # 2. Construct full callback path
        if bot_key:
            callback_path = f"/weixin/callback/{bot_key}"
        else:
            callback_path = "/weixin/callback"

        # 3. Get bot instance by path
        bot = bot_manager.get_bot_by_path(callback_path)
        if bot is None:
            logger.error(f"No bot found for callback path: {callback_path}")
            return Response(content="success", media_type="text/plain")

        logger.info(f"Processing message: bot_key={bot.bot_key}, path={callback_path}")

        # 4. Read POST data
        try:
            post_data = await request.body()
        except Exception as e:
            logger.error(f"Failed to read POST data: {e}")
            return Response(content="success", media_type="text/plain")

        # 5. Decrypt message
        try:
            data = bot.decrypt_message(post_data, msg_signature, timestamp, nonce)
            if data is None:
                logger.error("Message decryption failed")
                return Response(content="success", media_type="text/plain")
        except Exception as e:
            logger.error(f"Exception during message decryption: {e}", exc_info=True)
            return Response(content="success", media_type="text/plain")

        # 5.5. Generate secure stream_id (with bot_key and user_id)
        user_id = data.get('from', {}).get('userid', '')
        stream_id = generate_stream_id(bot.bot_key, user_id)
        logger.info(f"Generated secure stream_id: {stream_id} (bot={bot.bot_key}, user={user_id})")

        # 6. Route to message handler (core business logic)
        try:
            message = await bot.handle_message(data, stream_id)
        except Exception as e:
            logger.error(f"Message handling failed: {e}", exc_info=True)
            # Return error message to Enterprise WeChat even on failure to prevent infinite retry
            try:
                error_stream_id = stream_id if user_id else temp_stream_id
                error_message = MessageBuilder.text(
                    error_stream_id,
                    "Sorry, a system error occurred. Please try again later.\n\nIf the problem persists, please contact the administrator.",
                    finish=True
                )
                encrypted_msg = bot.encrypt_message(error_message, nonce, timestamp)
                if encrypted_msg:
                    logger.info("Returned error message to user")
                    return Response(content=encrypted_msg, media_type="text/plain")
            except Exception as encrypt_err:
                logger.error(f"Failed to encrypt error message: {encrypt_err}")

            # If encryption fails, return success to avoid retry
            return Response(content="success", media_type="text/plain")

        # 7. Handle returned message
        if message is None:
            # Unknown message type, return success without reply
            logger.info("Message handler returned None, possibly unsupported message type")
            return Response(content="success", media_type="text/plain")

        # 8. Encrypt and return
        try:
            encrypted_msg = bot.encrypt_message(message, nonce, timestamp)
            if encrypted_msg is None:
                logger.error("Message encryption failed")
                return Response(content="success", media_type="text/plain")

            return Response(content=encrypted_msg, media_type="text/plain")

        except Exception as e:
            logger.error(f"Exception during message encryption: {e}", exc_info=True)
            return Response(content="success", media_type="text/plain")

    except Exception as e:
        # Catch all unexpected exceptions
        logger.error(f"Unexpected exception during request processing: {e}", exc_info=True)
        # Always return success to prevent Enterprise WeChat retry
        return Response(content="success", media_type="text/plain")


@app.get("/")
async def root():
    """Root path, return homepage"""
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    """Health check endpoint (no sensitive info exposed)"""
    return {
        "status": "ok",
        "version": VERSION
    }


@app.get("/api/bots", dependencies=[Depends(verify_admin_api_key)])
async def list_bots():
    """List all configured bots (requires API Key authentication)"""
    bots_info = []
    for bot_key, bot in bot_manager.get_all_bots().items():
        bots_info.append({
            "bot_key": bot_key,
            "callback_path": bot.callback_path,
            "description": bot.config.description
        })
    return {"bots": bots_info, "count": len(bots_info)}


@app.post("/api/reload", dependencies=[Depends(verify_admin_api_key)])
async def reload_config():
    """Reload bot configuration (hot reload, requires API Key authentication)"""
    try:
        bot_manager.reload_config()
        return {"status": "ok", "message": "Configuration reloaded successfully"}
    except Exception as e:
        logger.error(f"Failed to reload configuration: {e}")
        raise HTTPException(status_code=500, detail="Configuration reload failed")


# ============ Test API & Log API (development only) ============

if ENVIRONMENT == 'development':
    class TestMessageRequest(BaseModel):
        """Test message request"""
        bot_key: str
        user_id: str
        message: str

    @app.post("/api/test/message", dependencies=[Depends(verify_admin_api_key)])
    async def test_message(req: TestMessageRequest):
        """Test sending a message to a bot"""
        try:
            bot = bot_manager.get_bot(req.bot_key)
            if not bot:
                return {"success": False, "error": f"Bot {req.bot_key} not found"}

            start_time = time.time()
            stream_id = generate_stream_id(req.bot_key, req.user_id)

            mock_weixin_message = {
                "msgtype": "text",
                "text": {"content": req.message},
                "from": {"userid": req.user_id}
            }

            reply_message = await bot.handle_message(mock_weixin_message, stream_id)

            import json
            reply_data = json.loads(reply_message)

            reply_text = ""
            msgtype = reply_data.get("msgtype", "")
            is_stream = (msgtype == "stream")

            if msgtype == "stream":
                reply_text = reply_data.get("stream", {}).get("content", "")
            elif msgtype == "text":
                reply_text = reply_data.get("text", {}).get("content", "")
            elif msgtype == "template_card":
                reply_text = "[Template card message]\n" + json.dumps(reply_data.get("template_card"), ensure_ascii=False, indent=2)

            took_time = f"{(time.time() - start_time):.2f}s"

            return {
                "success": True,
                "bot_name": bot.config.description,
                "reply": reply_text,
                "is_stream": is_stream,
                "took_time": took_time,
                "raw_data": reply_data
            }

        except Exception as e:
            logger.error(f"Test message failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @app.get("/test", response_class=HTMLResponse)
    async def test_page():
        """Test page"""
        return FileResponse("static/test.html")

    # Log API
    log_buffer = []
    MAX_LOG_BUFFER_SIZE = 1000

    class LogHandler(logging.Handler):
        """Custom log handler that saves logs to memory"""
        def emit(self, record):
            try:
                log_entry = {
                    "time": self.format_time(record.created),
                    "level": record.levelname.lower(),
                    "message": record.getMessage()
                }
                log_buffer.append(log_entry)
                if len(log_buffer) > MAX_LOG_BUFFER_SIZE:
                    log_buffer.pop(0)
            except Exception:
                self.handleError(record)

        @staticmethod
        def format_time(timestamp):
            from datetime import datetime
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime("%H:%M:%S")

    log_handler = LogHandler()
    logging.getLogger().addHandler(log_handler)
    last_log_index = {}

    @app.get("/api/logs")
    async def get_logs(request: Request):
        """Get latest logs (incremental return)"""
        client_ip = request.client.host
        last_index = last_log_index.get(client_ip, 0)
        new_logs = log_buffer[last_index:]
        last_log_index[client_ip] = len(log_buffer)
        return {"logs": new_logs, "total": len(log_buffer)}

    logger.info("Development mode: test page (/test) and log API (/api/logs) enabled")
else:
    logger.info("Production mode: test page and log API disabled")


# Mount static files directory (block test page in production)
if ENVIRONMENT != 'development':
    @app.get("/static/test.html")
    async def block_test_html():
        """Block access to test page in production"""
        raise HTTPException(status_code=404, detail="Not Found")

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except RuntimeError:
    # If static directory doesn't exist, create it
    os.makedirs("static", exist_ok=True)
    app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    # Read port from environment variable, default 5000
    port = int(os.getenv('PORT', 5000))

    # Configure uvicorn logging, disable access log
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False  # Disable HTTP access log
    )
