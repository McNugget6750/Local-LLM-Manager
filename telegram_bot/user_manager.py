import aiosqlite
from datetime import datetime
from typing import Optional, Dict, Any
from telegram_bot.config import settings
from telegram_bot.logger import get_logger

logger = get_logger(__name__)

class UserManager:
    """
    Manages user authentication tokens and state in a SQLite database.
    """
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DB_PATH
        self._db: Optional[aiosqlite.Connection] = None

    async def init_db(self) -> None:
        """
        Initializes the database schema and opens a long-lived connection.
        """
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                api_token TEXT,
                status TEXT,
                created_at DATETIME,
                last_used DATETIME
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS unauthorized_attempts (
                user_id INTEGER PRIMARY KEY,
                count INTEGER DEFAULT 0,
                last_attempt DATETIME
            )
            """
        )
        await self._db.commit()
        logger.info(f"Database initialized and connected at {self.db_path}")

    async def close(self) -> None:
        """Closes the database connection."""
        if self._db:
            await self._db.close()
            logger.info("Database connection closed.")

    async def upsert_user(self, user_id: int, token: str) -> None:
        """
        Creates or updates a user with the provided API token.
        """
        from datetime import timezone
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO users (user_id, api_token, status, created_at, last_used)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                api_token = excluded.api_token,
                status = 'authenticated',
                last_used = excluded.last_used
            """,
            (user_id, token, 'authenticated', now, now)
        )
        await self._db.commit()
        logger.info(f"User {user_id} upserted with token.")

    async def get_user_token(self, user_id: int) -> Optional[str]:
        """
        Retrieves the API token for a given Telegram user ID.
        Returns None if the user is not found.
        Updates last_used timestamp atomically using RETURNING.
        """
        from datetime import timezone
        now = datetime.now(timezone.utc).isoformat()
        async with self._db.execute(
            "UPDATE users SET last_used = ? WHERE user_id = ? RETURNING api_token", 
            (now, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_user_status(self, user_id: int) -> Optional[str]:
        """
        Retrieves the current status of the user.
        """
        async with self._db.execute(
            "SELECT status FROM users WHERE user_id = ?", 
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def increment_unauthorized_count(self, user_id: int) -> int:
        """
        Atomically increments the unauthorized attempt count for a user.
        Returns the new count.
        """
        from datetime import timezone
        now = datetime.now(timezone.utc).isoformat()
        async with self._db.execute(
            """
            INSERT INTO unauthorized_attempts (user_id, count, last_attempt)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                count = count + 1,
                last_attempt = excluded.last_attempt
            RETURNING count
            """,
            (user_id, now)
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0
        
        await self._db.commit()
        return count
