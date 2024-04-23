from pathlib import Path
from typing import Final

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_id: int
    api_hash: SecretStr
    bot_token: str

    session_name: str = "db/user.session"

    log_chat_id: int
    ignored_ids: set[int] = {}

    listen_outgoing_messages: bool = True
    save_edited_messages: bool = True
    delete_sent_gifs_from_saved: bool = True
    delete_sent_stickers_from_saved: bool = True

    file_password: SecretStr = "super secret password"
    max_in_memory_file_size: int = 5 * 1024 * 1024

    sqlite_db_file: Path = "db/messages.db"
    persist_time_in_days_bot: int = 1
    persist_time_in_days_user: int = 1
    persist_time_in_days_channel: int = 1
    persist_time_in_days_group: int = 1

    debug_mode: bool = True
    rate_limit_num_messages: int = 5

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8")

    def build_sqlite_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.sqlite_db_file}"


settings: Final[Settings] = Settings()
