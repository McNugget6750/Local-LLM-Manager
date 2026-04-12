import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from telegram_bot.config import settings
from telegram_bot.logger import get_logger
from telegram_bot.user_manager import UserManager
from telegram_bot.backend_client import BackendClient
from telegram_bot.handlers import user as user_handlers
from telegram_bot.middlewares import DependencyMiddleware
from telegram_bot.middlewares.auth import UserAllowlistMiddleware

logger = get_logger(__name__)

async def main():
    # 1. Initialize Components
    user_manager = UserManager()
    await user_manager.init_db()
    
    backend_client = BackendClient()
    
    # 2. Setup Bot and Dispatcher
    bot = Bot(token=settings.BOT_TOKEN.get_secret_value())
    dp = Dispatcher(storage=MemoryStorage())
    
    # 3. Integrate Handlers
    dp.include_router(user_handlers.router)
    
    # Register middlewares
    dp.update.outer_middleware(UserAllowlistMiddleware(user_manager))
    dp.update.outer_middleware(DependencyMiddleware(user_manager, backend_client))
    
    logger.info("Starting Telegram Bot...")
    try:
        await dp.start_polling(bot)
    finally:
        await backend_client.close()
        await user_manager.close()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        sys.exit(0)