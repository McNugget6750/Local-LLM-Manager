import asyncio
import pytest
import pytest_asyncio
import respx
import httpx
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update, User, Chat
from aiogram.fsm.storage.memory import MemoryStorage

from telegram_bot.user_manager import UserManager
from telegram_bot.backend_client import BackendClient
from telegram_bot.handlers import user as user_handlers

# --- Test Constants ---
BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
BACKEND_URL = "http://localhost:1237/chat"
TEST_USER_ID = 123456789
TEST_TOKEN = "test-api-token-abc-123"
TEST_MESSAGE = "Hello, Eli!"
TEST_RESPONSE = "Hello! I am Eli, your AI assistant."

# --- Fixtures ---

@pytest_asyncio.fixture
async def bot():
    """Mock Bot instance."""
    bot = AsyncMock(spec=Bot)
    bot.send_chat_action = AsyncMock()
    return bot

@pytest_asyncio.fixture
async def dp(bot):
    """Dispatcher instance."""
    dp = Dispatcher(storage=MemoryStorage())
    return dp

@pytest_asyncio.fixture
async def user_manager(tmp_path):
    """UserManager with a temporary file database to avoid :memory: persistence issues."""
    db_file = tmp_path / "test_users.db"
    um = UserManager(db_path=str(db_file))
    await um.init_db()
    return um

@pytest_asyncio.fixture
async def backend_client():
    """BackendClient instance."""
    bc = BackendClient(base_url="http://localhost:1237")
    yield bc
    await bc.close()

@pytest_asyncio.fixture
async def setup_bot(bot, dp, user_manager, backend_client):
    """Sets up the bot dependencies and dispatcher."""
    user_handlers.set_dependencies(user_manager, backend_client)
    
    # To avoid "Router is already attached" error, we can't simply set parent_router = None
    # because the setter validates that the value is a Router.
    # Instead, we can use a fresh router or just accept that the router is a singleton
    # and we should probably create a new router for each test or clear it.
    # However, since user_handlers.router is a global, let's try to clear it using object.__setattr__
    object.__setattr__(user_handlers.router, '_parent_router', None)
    
    dp.include_router(user_handlers.router)
    return dp

def create_message(bot, user_id, text):
    """Helper to create a mock aiogram Message object."""
    user = User(id=user_id, is_bot=False, first_name="TestUser", language_code="en")
    chat = Chat(id=user_id, type="private")
    
    # Use MagicMock for the whole Message but make it look like a Message
    # to avoid Pydantic's frozen instance checks.
    message = MagicMock(spec=Message)
    message.bot = bot
    message.from_user = user
    message.chat = chat
    message.text = text
    message.answer = AsyncMock()
    
    # To satisfy aiogram's feed_update which might call model_dump()
    # we mock model_dump to return a dict that looks like a Message.
    message.model_dump.return_value = {
        "message_id": 1,
        "date": datetime.now(timezone.utc).isoformat(),
        "chat": chat.model_dump(),
        "from_user": user.model_dump(),
        "text": text,
    }
    
    return message

def create_update(message):
    """Helper to create an aiogram Update object."""
    # We create the Update as a dict first to avoid Pydantic serialization issues
    # when feed_update calls model_dump() on the Update object.
    msg_data = message if isinstance(message, dict) else message.model_dump() if hasattr(message, 'model_dump') else {}
    return Update.model_validate({
        "update_id": 1,
        "message": msg_data
    })

# --- Tests ---

@pytest.mark.asyncio
@respx.mock
async def test_successful_flow(setup_bot, bot, user_manager):
    """Test: New user -> /set_token -> Send message -> Receive response."""
    dp = setup_bot
    
    # 1. Set Token
    msg_set_token = create_message(bot, TEST_USER_ID, f"/set_token {TEST_TOKEN}")
    await dp.feed_update(bot=bot, update=create_update(msg_set_token))
    
    msg_set_token.answer.assert_called_with("✅ Token updated successfully! You can now send me messages.")
    
    # Verify token is in DB
    token = await user_manager.get_user_token(TEST_USER_ID)
    assert token == TEST_TOKEN
    
    # 2. Send Message
    # Mock Backend Response
    respx.post(BACKEND_URL).mock(return_value=respx.Response(200, json={"response": TEST_RESPONSE}))
    
    msg_chat = create_message(bot, TEST_USER_ID, TEST_MESSAGE)
    await dp.feed_update(bot=bot, update=create_update(msg_chat))
    
    # Verify Bot answered with backend response
    msg_chat.answer.assert_called_with(TEST_RESPONSE)

@pytest.mark.asyncio
@respx.mock
async def test_backend_busy(setup_bot, bot, user_manager):
    """Test: Valid user -> Backend returns 503 -> User receives 'Backend is busy' message."""
    dp = setup_bot
    
    # Setup: Authenticate user
    await user_manager.upsert_user(TEST_USER_ID, TEST_TOKEN)
    
    # Mock Backend 503
    respx.post(BACKEND_URL).mock(return_value=httpx.Response(503))
    
    msg_chat = create_message(bot, TEST_USER_ID, TEST_MESSAGE)
    await dp.feed_update(bot=bot, update=create_update(msg_chat))
    
    msg_chat.answer.assert_called_with("⚠️ The backend is currently busy. Please try again in a few minutes.")

@pytest.mark.asyncio
@respx.mock
async def test_backend_timeout(setup_bot, bot, user_manager):
    """Test: Valid user -> Backend times out -> User receives 'Request timed out' message."""
    dp = setup_bot
    
    # Setup: Authenticate user
    await user_manager.upsert_user(TEST_USER_ID, TEST_TOKEN)
    
    # Mock Backend Timeout (httpx.TimeoutException is what BackendClient catches)
    respx.post(BACKEND_URL).mock(side_effect=httpx.TimeoutException("Request timed out"))
    
    msg_chat = create_message(bot, TEST_USER_ID, TEST_MESSAGE)
    await dp.feed_update(bot=bot, update=create_update(msg_chat))
    
    msg_chat.answer.assert_called_with("⏳ The request timed out. The backend might be overloaded.")

@pytest.mark.asyncio
async def test_unauthenticated(setup_bot, bot):
    """Test: User without token -> Send message -> Prompt for token."""
    dp = setup_bot
    
    # User is NOT authenticated (empty DB)
    msg_chat = create_message(bot, TEST_USER_ID, TEST_MESSAGE)
    await dp.feed_update(bot=bot, update=create_update(msg_chat))
    
    msg_chat.answer.assert_called_with(
        "You are not authenticated. Please set your API token first:\n"
        "/set_token <your_token>"
    )