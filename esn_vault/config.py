from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    owner_id: int
    database_path: Path
    test_guild_id: int | None = None


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    owner = os.getenv("BOT_OWNER_ID", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required")
    if not owner.isdigit() or int(owner) <= 0:
        raise RuntimeError("BOT_OWNER_ID must be a positive Discord user ID")
    guild = os.getenv("TEST_GUILD_ID", "").strip()
    return Settings(
        token=token,
        owner_id=int(owner),
        database_path=Path(os.getenv("DATABASE_PATH", "data/esn_vault.sqlite3")),
        test_guild_id=int(guild) if guild.isdigit() else None,
    )
