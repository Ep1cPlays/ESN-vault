# ESN Vault

ESN Vault is a persistent, virtual-only Discord economy using ES Coins 🪙. It has no real-money deposits, gambling, cryptocurrency, cash-out, or real-world rewards.

## Setup

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and add a bot user.
2. Copy the bot token from the **Bot** page. Keep it secret; never commit `.env`.
3. Copy `.env.example` to `.env`, set `DISCORD_TOKEN`, and set `BOT_OWNER_ID` to the numeric Discord ID of the bot owner. Enable Developer Mode in Discord to copy IDs.
4. Install Python 3.11+ and dependencies: `python -m venv .venv`, activate it, then `pip install -r requirements.txt`.
5. Run with `python main.py`. SQLite data is stored in `data/esn_vault.sqlite3` by default.
6. Invite the bot from **OAuth2 > URL Generator** with scopes `bot` and `applications.commands`. Grant View Channel, Send Messages, Embed Links, Use Application Commands, and Manage Roles if role rewards are used. Place the bot's role above reward roles.

Set `TEST_GUILD_ID` during development for fast slash-command sync. Leave it blank for global sync, which can take Discord time to propagate.

## Commands

Users get `/balance`, `/daily`, `/weekly`, `/work`, `/use`, `/interest`, `/transactions`, `/stats`, `/pay`, `/deposit`, `/withdraw`, `/profile`, `/leaderboard`, `/inventory`, `/shop`, `/buy`, `/market`, `/sell`, `/trivia`, `/scavenge`, `/riddle`, `/vault-dive`, `/trade`, `/achievements`, `/quests`, and `/rep`. The global shop seeds ten unlimited virtual consumables. Use tickets and passes with `/use` before the associated mini-game; use a Trade Seal before making an item-for-item trade offer. The Coin Collector achievement unlocks after earning 1,000 ES Coins through Vault rewards and adds a permanent 5% bonus to future `/daily`, `/weekly`, and `/work` rewards. Discord does not allow a top-level `/shop` command and a `/shop` command group to coexist, so administrators manage isolated server shops with `/server-shop add`, `/server-shop remove`, `/server-shop edit`, and `/server-shop list`. They manage balances with `/economy give`, `/economy remove`, `/economy set`, and `/economy reset`. The configured owner manages global items through `/owner shop add`, `remove`, `edit`, `list`, `stock`, `enable`, and `disable`.

Active Discord Server Boosters receive 30% more ES Coins from bot-generated rewards while using commands inside that boosted server. This applies to daily and weekly claims, work, bank interest, and mini-game rewards. It stacks additively with Coin Collector's 5% bonus, for a maximum 35% bonus where both apply.

## Security and persistence

Balances, inventories, stock, and transaction history live in SQLite. Balance changes and purchases use `BEGIN IMMEDIATE` under an async lock, enforce non-negative constraints, reject self-payments, and record audit transactions. Finite stock is decremented atomically; `-1` means unlimited. Role purchases validate role availability before charging and refund if Discord rejects the grant.

When a member completes `/buy`, `/sell`, or `/deposit` in a server, ESN Vault sends that server's owner a best-effort private audit DM. A server owner can disable DMs from server members without interrupting economy transactions.

Run tests with `pytest`.
