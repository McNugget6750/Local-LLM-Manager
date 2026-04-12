import asyncio
import os
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from telegram_bot.config import settings
from telegram_bot.user_manager import UserManager

BLOCKLIST_FILE = "telegram_bot/blocklist.txt"

class UserAllowlistMiddleware(BaseMiddleware):
    """
    Middleware to restrict bot access based on an allowlist and a dynamic blocklist.
    """

    def __init__(self, user_manager: UserManager):
        self.user_manager = user_manager
        self._blocklist_cache: set[int] = set()
        self._load_blocklist()

    def _load_blocklist(self) -> None:
        if not os.path.exists(BLOCKLIST_FILE):
            return
        try:
            with open(BLOCKLIST_FILE, "r") as f:
                self._blocklist_cache = {int(line.strip()) for line in f if line.strip()}
        except (ValueError, IOError):
            pass

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Only process events that have a user
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        
        user_id = user.id
        
        # 1. Check blocklist (permanent blocks)
        if user_id in self._blocklist_cache:
            return  # Silent drop

        # 2. Check allowlist (explicitly permitted users)
        if user_id in settings.ALLOWED_USERS:
            return await handler(event, data)
        
        # 3. Unauthorized access logic
        count = await self.user_manager.increment_unauthorized_count(user_id)
        if count >= 10:
            await self._block_user(user_id)
            return  # Silent drop
        
        if settings.SILENT_REJECTION:
            return  # Silent drop
        
        # Send rejection message if not silent
        if isinstance(event, Message):
            await event.answer("⛔ You are not authorized to use this bot.")
        
        return  # Stop processing the handler

    async def _block_user(self, user_id: int):
        self._blocklist_cache.add(user_id)
        try:
            await asyncio.to_thread(self._append_to_blocklist, user_id)
        except Exception:
            # Log error but don't block the request
            pass

    def _append_to_blocklist(self, user_id: int):
        with open(BLOCKLIST_FILE, "a") as f:
            f.write(f"{user_id}\n")
