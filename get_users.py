import asyncio
import aiosqlite

async def main():
    try:
        async with aiosqlite.connect('telegram_bot/bot_auth.db') as db:
            async with db.execute('SELECT user_id FROM users') as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    print(row[0])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
