from typing import Optional, Union
from pydantic import SecretStr, AnyHttpUrl, BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="telegram_bot/.env", env_file_encoding="utf-8")

    BOT_TOKEN: SecretStr
    BACKEND_URL: AnyHttpUrl = "http://localhost:1237/chat"
    ADMIN_ID: Optional[int] = None
    ALLOWED_USERS: Union[set[int], str] = set()
    SILENT_REJECTION: bool = False
    DB_PATH: str = "telegram_bot/bot_auth.db"

    @field_validator("ALLOWED_USERS", mode="before")
    @classmethod
    def validate_allowed_users(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return set()
            return {int(x.strip()) for x in v.split(",")}
        if isinstance(v, int):
            return {v}
        if isinstance(v, (list, set)):
            return {int(x) for x in v}
        return v

settings = Settings()