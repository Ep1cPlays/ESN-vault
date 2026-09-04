from esn_vault.bot import VaultBot, register_commands
from esn_vault.config import load_settings
from esn_vault.db import Database


settings = load_settings()
bot = VaultBot(settings, Database(settings.database_path))
register_commands(bot)
bot.run(settings.token)
