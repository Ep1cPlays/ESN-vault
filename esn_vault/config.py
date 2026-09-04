from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    owner_ids: tuple[int, ...]
    database_path: Path
    test_guild_id: int | None = None


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "").strip()

    if not token:
        raise RuntimeError("DISCORD_TOKEN is required")

    # ESN Bot Owners
    owner_ids = (
        1515077206886453469,
        1434663490160955407,
        1392224478175690752,
    )

    guild = os.getenv("TEST_GUILD_ID", "").strip()

    return Settings(
        token=token,
        owner_ids=owner_ids,
        database_path=Path(
            os.getenv("DATABASE_PATH", "data/esn_vault.sqlite3")
        ),
        test_guild_id=int(guild) if guild.isdigit() else None,
    )
