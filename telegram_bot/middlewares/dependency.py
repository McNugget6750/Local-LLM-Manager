from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

class DependencyMiddleware(BaseMiddleware):
    def __init__(self, user_manager: Any, backend_client: Any):
        self.user_manager = user_manager
        self.backend_client = backend_client

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        data["user_manager"] = self.user_manager
        data["backend_client"] = self.backend_client
        return await handler(event, data)
