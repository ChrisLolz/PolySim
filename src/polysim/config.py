"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_path: Path
    dev_guild_id: int | None = None
    resolution_poll_seconds: int = 60

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_TOKEN must be set")

        guild_value = os.getenv("DISCORD_DEV_GUILD_ID", "").strip()
        poll_seconds = int(os.getenv("RESOLUTION_POLL_SECONDS", "60"))
        if poll_seconds < 10:
            raise ValueError("RESOLUTION_POLL_SECONDS must be at least 10")

        return cls(
            discord_token=token,
            database_path=Path(os.getenv("DATABASE_PATH", "data/polysim.sqlite3")),
            dev_guild_id=int(guild_value) if guild_value else None,
            resolution_poll_seconds=poll_seconds,
        )
