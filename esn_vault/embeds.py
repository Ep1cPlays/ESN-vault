from __future__ import annotations

import discord

COIN = "🪙"
COLOUR = discord.Colour.from_rgb(26, 117, 95)


def vault_embed(title: str, description: str = "", *, colour: discord.Colour = COLOUR) -> discord.Embed:
    embed = discord.Embed(title=f"ESN Vault  |  {title}", description=description, colour=colour)
    embed.set_footer(text="Virtual ES Coins only • No real-money value")
    return embed


def error_embed(message: str) -> discord.Embed:
    return vault_embed("Something went wrong", message, colour=discord.Colour.red())
