from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict
from telegram_bot.config import Settings
import os
from unittest.mock import patch

def test_allowed_users(env_val, expected):
    print(f"Testing ALLOWED_USERS={env_val!r}")
    try:
        with patch.dict(os.environ, {"BOT_TOKEN": "dummy", "ALLOWED_USERS": env_val}):
            class TestSettings(Settings):
                model_config = SettingsConfigDict(env_file=None)

            s = TestSettings()
            print(f"Result: {s.ALLOWED_USERS}")
            assert s.ALLOWED_USERS == expected
            print("Success!")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    # Case 1: Single ID as string (how it comes from .env)
    test_allowed_users("11111111", {11111111})

    # Case 2: Comma separated string
    test_allowed_users("11111111,22222222", {11111111, 22222222})

    # Case 3: Empty string
    test_allowed_users("", set())

    # Case 4: String with spaces
    test_allowed_users(" 11111111 , 22222222 ", {11111111, 22222222})
