import asyncio
from typing import Optional

from aiogram import Router, types, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from telegram_bot.backend_client import BackendClient, BackendBusyError, BackendTimeoutError, BackendError
from telegram_bot.user_manager import UserManager
from telegram_bot.utils.text_utils import split_text
from telegram_bot.logger import get_logger

logger = get_logger(__name__)

router = Router()

# Dependency injection containers (will be initialized in main.py)
# Removed global variables and set_dependencies function

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Welcome to Eli Bot! 🤖\n\n"
        "To start chatting, please set your API token using:\n"
        "/set_token <your_token>"
    )

@router.message(Command("set_token"))
async def cmd_set_token(message: Message, user_manager: UserManager, backend_client: BackendClient):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Please provide a token: /set_token <your_token>")
        return

    token = args[1].strip()
    
    # Basic token validation: ensure it's not empty and has a minimum length
    if not token or len(token) < 8:
        await message.answer("❌ Invalid token. Tokens should be at least 8 characters long.")
        return

    try:
        # Validate token with backend before saving
        if not await backend_client.validate_token(token):
            await message.answer("❌ Invalid API token. Please check your token and try again.")
            return

        await user_manager.upsert_user(message.from_user.id, token)
        await message.answer("✅ Token updated successfully! You can now send me messages.")
    except Exception as e:
        logger.exception(f"Error setting token for user {message.from_user.id}: {e}")
        await message.answer("❌ An error occurred while saving your token.")

async def send_typing_indicator(bot: Bot, chat_id: int, stop_event: asyncio.Event):
    """Background task to keep the 'typing...' indicator active."""
    try:
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logger.debug(f"Failed to send typing action (user might have blocked bot): {e}")
                # If we can't send typing action, it's likely the user blocked us.
                # We can stop the indicator task.
                break
            await asyncio.sleep(4) # Telegram typing indicator lasts about 5 seconds
    except Exception as e:
        logger.debug(f"Typing indicator stopped: {e}")

@router.message()
async def handle_message(message: Message, bot: Bot, user_manager: UserManager, backend_client: BackendClient):
    # 0. Validate message length
    if message.text and len(message.text) > 1024:
        await message.answer("❌ Message too long. Please keep your message under 1024 characters.")
        return

    # 1. Authenticate User
    token = await user_manager.get_user_token(message.from_user.id)
    if not token:
        await message.answer(
            "You are not authenticated. Please set your API token first:\n"
            "/set_token <your_token>"
        )
        return

    # 2. Start Typing Indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(send_typing_indicator(bot, message.chat.id, stop_typing))

    try:
        # 3. Proxy to Backend — wrap with user_id so remote_chat.py can route replies
        uid = message.from_user.id
        wrapped = f"[TELEGRAM_REQUEST from user_id={uid}]\n{message.text}\n[/TELEGRAM_REQUEST]"
        response_text = await backend_client.send_message(token, wrapped)
        
        # 4. Split and Send Response
        chunks = split_text(response_text)
        for chunk in chunks:
            await message.answer(chunk)

    except BackendBusyError:
        await message.answer("⚠️ The backend is currently busy. Please try again in a few minutes.")
    except BackendTimeoutError:
        await message.answer("⏳ The request timed out. The backend might be overloaded.")
    except BackendError as e:
        await message.answer(f"❌ Backend error: {str(e)}")
    except Exception as e:
        logger.exception(f"Unexpected error handling message from {message.from_user.id}: {e}")
        await message.answer("❌ An unexpected error occurred.")
    finally:
        # Stop typing indicator
        stop_typing.set()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


_APPROVAL_LABELS = {"1": "✅ Allowed once", "2": "🔒 Allowed for session", "3": "❌ Denied"}

@router.callback_query(F.data.startswith("approve:"))
async def handle_approval_callback(
    callback: CallbackQuery,
    backend_client: BackendClient,
):
    response = callback.data.split(":", 1)[1]   # "1", "2", or "3"
    logger.info(f"[TG bot] approval callback received: response={response!r} from user={callback.from_user.id}")
    if response not in _APPROVAL_LABELS:
        await callback.answer("Unknown option.", show_alert=True)
        return

    logger.info(f"[TG bot] calling post_approve({response!r})")
    result = await backend_client.post_approve(response)
    logger.info(f"[TG bot] post_approve result: {result}")

    if result.get("ok"):
        label = _APPROVAL_LABELS[response]
        await callback.answer(label)
        try:
            await callback.message.edit_text(
                callback.message.text + f"\n\n{label}"
            )
        except Exception:
            pass  # editing can fail if message is too old or unchanged
    else:
        reason = result.get("reason", "unknown")
        await callback.answer(f"Could not apply: {reason}", show_alert=True)