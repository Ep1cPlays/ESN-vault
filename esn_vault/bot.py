from __future__ import annotations

import random
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings
from .db import Database, EconomyError
from .embeds import COIN, error_embed, vault_embed


class VaultBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings, self.db = settings, db
        self.owner_group = app_commands.Group(name="owner", description="Bot owner controls")
        self.owner_shop_group = app_commands.Group(name="shop", description="Manage the global shop", parent=self.owner_group)
        self.server_shop_group = app_commands.Group(name="server-shop", description="Manage this server shop")
        self.economy_group = app_commands.Group(name="economy", description="Manage economy balances")

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.tree.add_command(self.owner_group)
        self.tree.add_command(self.server_shop_group)
        self.tree.add_command(self.economy_group)
        if self.settings.test_guild_id:
            guild = discord.Object(self.settings.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        print(f"ESN Vault online as {self.user} ({self.user.id})")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        message = "An unexpected error occurred. Please try again later."
        if isinstance(error, EconomyError):
            message = str(error)
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down. Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You do not have permission to use that command."
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)


def positive_amount(value: int) -> int:
    if value <= 0 or value > 2_147_483_647:
        raise app_commands.AppCommandError("Amount must be a positive, reasonable integer.")
    return value


def owner_only(bot: VaultBot):
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id != bot.settings.owner_id:
            raise app_commands.CheckFailure
        return True
    return app_commands.check(predicate)


def admin_only():
    return app_commands.checks.has_permissions(manage_guild=True)


def is_server_booster(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and getattr(interaction.user, "premium_since", None) is not None


async def notify_guild_owner(interaction: discord.Interaction, title: str, details: str) -> None:
    """Send a best-effort audit notification without affecting a completed transaction."""
    if not interaction.guild:
        return
    owner = interaction.guild.owner
    if owner is None:
        try:
            owner = await interaction.guild.fetch_member(interaction.guild.owner_id)
        except (discord.HTTPException, TypeError):
            return
    try:
        await owner.send(embed=vault_embed(title, f"**Server:** {interaction.guild.name}\n{details}"))
    except (discord.Forbidden, discord.HTTPException):
        pass


def register_commands(bot: VaultBot) -> None:
    class GameChallenge(discord.ui.View):
        def __init__(self, player_id: int, access_item: str, reward: int, question: str, answers: list[str], correct_answer: str, boost_item: str | None = None):
            super().__init__(timeout=60)
            self.player_id = player_id
            self.access_item = access_item
            self.reward = reward
            self.question = question
            self.correct_answer = correct_answer
            self.boost_item = boost_item
            for answer in answers:
                button = discord.ui.Button(label=answer, style=discord.ButtonStyle.primary)
                button.callback = self.answer
                self.add_item(button)

        async def answer(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != self.player_id:
                await interaction.response.send_message("This mini-game belongs to another player.", ephemeral=True)
                return
            selected = interaction.data["custom_id"]
            button = next(child for child in self.children if child.custom_id == selected)
            if button.label != self.correct_answer:
                await interaction.response.send_message("Not quite. Your pass is still available for another challenge.", ephemeral=True)
                return
            try:
                credited = await bot.db.resolve_game(self.player_id, self.access_item, self.reward, boost_item=self.boost_item, server_boost=is_server_booster(interaction))
            except EconomyError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=vault_embed("Mini-game complete", f"Correct. You earned **{credited:,} {COIN}**."), view=self)

    class TradeOffer(discord.ui.View):
        def __init__(self, sender_id: int, recipient_id: int, offered_item: str, offered_quantity: int, requested_item: str, requested_quantity: int):
            super().__init__(timeout=120)
            self.sender_id = sender_id
            self.recipient_id = recipient_id
            self.offered_item = offered_item
            self.offered_quantity = offered_quantity
            self.requested_item = requested_item
            self.requested_quantity = requested_quantity

        @discord.ui.button(label="Accept trade", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            if interaction.user.id != self.recipient_id:
                await interaction.response.send_message("Only the trade recipient can accept this offer.", ephemeral=True)
                return
            try:
                await bot.db.trade_items(self.sender_id, self.recipient_id, self.offered_item, self.offered_quantity, self.requested_item, self.requested_quantity)
            except EconomyError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=vault_embed("Trade complete", f"<@{self.sender_id}> traded **{self.offered_quantity}x `{self.offered_item}`** for **{self.requested_quantity}x `{self.requested_item}`**."), view=self)

        @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
        async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            if interaction.user.id != self.recipient_id:
                await interaction.response.send_message("Only the trade recipient can decline this offer.", ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=vault_embed("Trade declined", "The offered Trade Seal was not consumed."), view=self)

    @bot.tree.command(name="balance", description="View your ES Coin balance")
    async def balance(interaction: discord.Interaction, user: discord.User | None = None):
        target = user or interaction.user
        account = await bot.db.account(target.id)
        embed = vault_embed(f"{target.display_name}'s balance", f"**Wallet**  {account['wallet']:,} {COIN}\n**Bank**  {account['bank']:,} {COIN}\n**Total wealth**  {account['wallet'] + account['bank']:,} {COIN}")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="daily", description="Claim your daily ES Coins")
    @app_commands.checks.cooldown(1, 5 * 60, key=lambda i: i.user.id)
    async def daily(interaction: discord.Interaction):
        streak, reward = await bot.db.claim_reward(interaction.user.id, kind="daily", cooldown=86_400, reward=0, streak=True, server_boost=is_server_booster(interaction))
        await interaction.response.send_message(embed=vault_embed("Daily claimed", f"You received **{reward:,} {COIN}**. Streak: **{streak}** days."))

    @bot.tree.command(name="weekly", description="Claim your weekly ES Coins")
    @app_commands.checks.cooldown(1, 5 * 60, key=lambda i: i.user.id)
    async def weekly(interaction: discord.Interaction):
        _, reward = await bot.db.claim_reward(interaction.user.id, kind="weekly", cooldown=604_800, reward=2_000, server_boost=is_server_booster(interaction))
        await interaction.response.send_message(embed=vault_embed("Weekly claimed", f"You received **{reward:,} {COIN}**."))

    @bot.tree.command(name="work", description="Work for a random ES Coin reward")
    async def work(interaction: discord.Interaction):
        reward, cooldown = await bot.db.perform_work(interaction.user.id, random.randint(75, 225), server_boost=is_server_booster(interaction))
        await interaction.response.send_message(embed=vault_embed("Shift complete", f"You earned **{reward} {COIN}**. Next shift: **{cooldown}s**."))

    @bot.tree.command(name="use", description="Use an item from your inventory")
    async def use(interaction: discord.Interaction, item_id: str):
        detail, _ = await bot.db.use_item(interaction.user.id, item_id)
        await interaction.response.send_message(embed=vault_embed("Item used", f"Used `{item_id.lower()}`. {detail}"))

    async def start_game(interaction: discord.Interaction, *, access_item: str, reward: int, question: str, answers: list[str], correct_answer: str, boost_item: str | None = None) -> None:
        if not await bot.db.effect_is_ready(interaction.user.id, access_item):
            raise EconomyError(f"Use a {access_item.replace('-', ' ').title()} before starting this mini-game.")
        view = GameChallenge(interaction.user.id, access_item, reward, question, answers, correct_answer, boost_item)
        await interaction.response.send_message(embed=vault_embed("ESN Vault mini-game", f"{question}\n\nUse the buttons below. A correct answer consumes your pass and awards ES Coins."), view=view)

    @bot.tree.command(name="trivia", description="Play a trivia challenge with a Trivia Ticket")
    async def trivia(interaction: discord.Interaction):
        await start_game(interaction, access_item="trivia-ticket", reward=150, question="Which command sends ES Coins to another user?", answers=["/pay", "/withdraw", "/shop"], correct_answer="/pay", boost_item="focus-tonic")

    @bot.tree.command(name="scavenge", description="Play a scavenger challenge with a Scavenger Kit")
    async def scavenge(interaction: discord.Interaction):
        await start_game(interaction, access_item="scavenger-kit", reward=175, question="Which item shortens your /work cooldown?", answers=["Energy Drink", "Trade Seal", "Vault Pass"], correct_answer="Energy Drink", boost_item="explorer-map")

    @bot.tree.command(name="riddle", description="Solve a riddle with a Riddle Key")
    async def riddle(interaction: discord.Interaction):
        await start_game(interaction, access_item="riddle-key", reward=200, question="I am used before an item trade can be completed. What am I?", answers=["Trade Seal", "Coin Magnet", "Focus Tonic"], correct_answer="Trade Seal")

    @bot.tree.command(name="vault-dive", description="Dive into the vault with a Vault Pass")
    async def vault_dive(interaction: discord.Interaction):
        await start_game(interaction, access_item="vault-pass", reward=300, question="What is the virtual currency stored in ESN Vault?", answers=["ES Coins", "Vault Gems", "Real Dollars"], correct_answer="ES Coins")

    @bot.tree.command(name="trade", description="Offer an item-for-item trade using a Trade Seal")
    async def trade(interaction: discord.Interaction, user: discord.User, offered_item: str, offered_quantity: app_commands.Range[int, 1, 100], requested_item: str, requested_quantity: app_commands.Range[int, 1, 100]):
        if user.id == interaction.user.id:
            raise EconomyError("You cannot trade with yourself.")
        if offered_item.lower() == requested_item.lower():
            raise EconomyError("Choose different offered and requested items.")
        if not await bot.db.effect_is_ready(interaction.user.id, "trade-seal"):
            raise EconomyError("Use a Trade Seal before making a trade offer.")
        view = TradeOffer(interaction.user.id, user.id, offered_item.lower(), offered_quantity, requested_item.lower(), requested_quantity)
        await interaction.response.send_message(embed=vault_embed("Trade offer", f"{user.mention}, <@{interaction.user.id}> offers **{offered_quantity}x `{offered_item.lower()}`** for **{requested_quantity}x `{requested_item.lower()}`**."), view=view)

    @bot.tree.command(name="interest", description="Claim daily interest on your bank balance")
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
    async def interest(interaction: discord.Interaction):
        reward = await bot.db.claim_interest(interaction.user.id, server_boost=is_server_booster(interaction))
        await interaction.response.send_message(embed=vault_embed("Bank interest claimed", f"Your bank earned **{reward:,} {COIN}** in virtual interest."))

    @bot.tree.command(name="transactions", description="View your recent Vault transactions")
    async def transactions(interaction: discord.Interaction):
        rows = await bot.db.transactions_for(interaction.user.id)
        text = "\n".join(f"**{row['kind'].replace('_', ' ').title()}**: {row['amount']:+,} {COIN} - {row['note'] or 'No note'}" for row in rows) or "No transactions yet."
        await interaction.response.send_message(embed=vault_embed("Recent transactions", text), ephemeral=True)

    @bot.tree.command(name="stats", description="View your ESN Vault economy statistics")
    async def stats(interaction: discord.Interaction):
        account = await bot.db.account(interaction.user.id)
        text = f"**Coins earned:** {account['earned_coins']:,} {COIN}\n**Work shifts:** {account['work_count']:,}\n**Purchases:** {account['purchase_count']:,}\n**Payments sent:** {account['payment_count']:,}"
        await interaction.response.send_message(embed=vault_embed("Economy statistics", text))

    @bot.tree.command(name="pay", description="Pay another user from your wallet")
    async def pay(interaction: discord.Interaction, user: discord.User, amount: app_commands.Range[int, 1, 2_147_483_647]):
        await bot.db.transfer(interaction.user.id, user.id, amount)
        await interaction.response.send_message(embed=vault_embed("Payment sent", f"Sent **{amount:,} {COIN}** to {user.mention}."))

    @bot.tree.command(name="deposit", description="Move ES Coins from wallet to bank")
    async def deposit(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 2_147_483_647]):
        await bot.db.change_balance(interaction.user.id, -amount, amount, kind="deposit", note="wallet to bank")
        await interaction.response.send_message(embed=vault_embed("Deposited", f"Moved **{amount:,} {COIN}** into your bank."))
        await notify_guild_owner(interaction, "Vault deposit", f"**Member:** {interaction.user.mention}\n**Amount:** {amount:,} {COIN}")

    @bot.tree.command(name="withdraw", description="Move ES Coins from bank to wallet")
    async def withdraw(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 2_147_483_647]):
        await bot.db.change_balance(interaction.user.id, amount, -amount, kind="withdraw", note="bank to wallet")
        await interaction.response.send_message(embed=vault_embed("Withdrawn", f"Moved **{amount:,} {COIN}** to your wallet."))

    @bot.tree.command(name="profile", description="View your Vault profile")
    async def profile(interaction: discord.Interaction, user: discord.User | None = None):
        target = user or interaction.user; account = await bot.db.account(target.id)
        await interaction.response.send_message(embed=vault_embed(f"{target.display_name}'s profile", f"Daily streak: **{account['daily_streak']}** days\nWealth: **{account['wallet'] + account['bank']:,} {COIN}**"))

    @bot.tree.command(name="leaderboard", description="View the wealth leaderboard")
    async def leaderboard(interaction: discord.Interaction):
        rows = await bot.db.top_accounts(); text = "\n".join(f"**{index}.** <@{row['user_id']}> — {row['wallet'] + row['bank']:,} {COIN}" for index, row in enumerate(rows, 1)) or "No vault accounts yet."
        await interaction.response.send_message(embed=vault_embed("Wealth leaderboard", text))

    @bot.tree.command(name="inventory", description="View your inventory")
    async def inventory(interaction: discord.Interaction):
        assert bot.db.connection; cursor = await bot.db.connection.execute("SELECT item_id, quantity FROM inventory WHERE user_id=? ORDER BY item_id", (interaction.user.id,)); rows = await cursor.fetchall()
        text = "\n".join(f"`{row['item_id']}` x{row['quantity']}" for row in rows) or "Your inventory is empty."
        await interaction.response.send_message(embed=vault_embed("Inventory", text))

    @bot.tree.command(name="shop", description="Browse the global and server shop")
    async def shop(interaction: discord.Interaction):
        guild_id = interaction.guild_id; assert bot.db.connection
        cursor = await bot.db.connection.execute("SELECT * FROM shop_items WHERE enabled=1 AND (guild_id IS NULL OR guild_id=?) ORDER BY guild_id NULLS FIRST, name", (guild_id,)); rows = await cursor.fetchall()
        text = "\n".join(f"{row['emoji']} **{row['name']}** (`{row['item_id']}`) — {row['price']:,} {COIN} | stock: {'unlimited' if row['stock'] == -1 else row['stock']}" for row in rows) or "The shop is empty."
        await interaction.response.send_message(embed=vault_embed("Shop", text))

    @bot.tree.command(name="buy", description="Buy an item from the shop")
    async def buy(interaction: discord.Interaction, item_id: str):
        assert bot.db.connection
        item = await bot.db._fetchone("SELECT * FROM shop_items WHERE item_id=? AND enabled=1 AND (guild_id IS NULL OR guild_id=?)", (item_id.lower(), interaction.guild_id))
        if not item:
            price = await bot.db.buy_market_item(interaction.user.id, item_id.lower())
            await interaction.response.send_message(embed=vault_embed("Market purchase", f"You bought `{item_id.lower()}` for **{price:,} {COIN}**."))
            await notify_guild_owner(interaction, "Market purchase", f"**Member:** {interaction.user.mention}\n**Item:** `{item_id.lower()}`\n**Price:** {price:,} {COIN}")
            return
        if item and item["role_id"]:
            role = interaction.guild.get_role(item["role_id"]) if interaction.guild else None
            if not role or not interaction.guild.me.guild_permissions.manage_roles or role >= interaction.guild.me.top_role:
                raise EconomyError("That role reward is currently unavailable; you were not charged.")
        item = await bot.db.buy(interaction.user.id, item_id.lower(), guild_id=interaction.guild_id)
        if item["role_id"]:
            try: await interaction.user.add_roles(interaction.guild.get_role(item["role_id"]))
            except discord.HTTPException:
                await bot.db.refund_purchase(interaction.user.id, item, reason=f"failed role reward {item_id}")
                raise EconomyError("The role could not be granted; your purchase was refunded.")
        await interaction.response.send_message(embed=vault_embed("Purchase complete", f"You bought {item['emoji']} **{item['name']}** for **{item['price']:,} {COIN}**."))
        await notify_guild_owner(interaction, "Shop purchase", f"**Member:** {interaction.user.mention}\n**Item:** {item['emoji']} **{item['name']}** (`{item['item_id']}`)\n**Price:** {item['price']:,} {COIN}")

    @bot.tree.command(name="market", description="View the ESN collectible market")
    async def market(interaction: discord.Interaction):
        assert bot.db.connection
        await bot.db.connection.execute("INSERT OR IGNORE INTO market(item_id,price,updated_at) VALUES('vault-token',500,?)", (int(time.time()),))
        await bot.db.connection.commit()
        cursor = await bot.db.connection.execute("SELECT * FROM market ORDER BY item_id")
        rows = await cursor.fetchall()
        text = "\n".join(f"`{row['item_id']}` — {row['price']:,} {COIN}" for row in rows)
        await interaction.response.send_message(embed=vault_embed("ESN Market", text + "\n\nUse `/sell item_id` to sell a collectible you own."))

    @bot.tree.command(name="sell", description="Sell one collectible at its current market price")
    async def sell(interaction: discord.Interaction, item_id: str):
        price = await bot.db.sell_market_item(interaction.user.id, item_id.lower())
        await interaction.response.send_message(embed=vault_embed("Market sale", f"Sold `{item_id.lower()}` for **{price:,} {COIN}**."))
        await notify_guild_owner(interaction, "Market sale", f"**Member:** {interaction.user.mention}\n**Item:** `{item_id.lower()}`\n**Price:** {price:,} {COIN}")

    @bot.tree.command(name="rep", description="Give a reputation point")
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
    async def rep(interaction: discord.Interaction, user: discord.User):
        if user.id == interaction.user.id: raise EconomyError("You cannot give yourself reputation.")
        await interaction.response.send_message(embed=vault_embed("Reputation", f"{user.mention} received a reputation point from you."))

    @bot.tree.command(name="achievements", description="View achievements")
    async def achievements(interaction: discord.Interaction):
        unlocked = await bot.db.achievements_for(interaction.user.id)
        collector = "Unlocked - 5% bonus on /daily, /weekly, and /work earnings." if "coin-collector" in unlocked else "Locked - earn 1,000 ES Coins from Vault rewards."
        await interaction.response.send_message(embed=vault_embed("Achievements", f"**Coin Collector**\n{collector}"))

    @bot.tree.command(name="quests", description="View daily and weekly quests")
    async def quests(interaction: discord.Interaction):
        await interaction.response.send_message(embed=vault_embed("Quests", "Daily: work 3 times for 300 coins. Weekly: make 5 payments for 1,000 coins."))

    async def global_owner(interaction: discord.Interaction) -> bool: return interaction.user.id == bot.settings.owner_id

    @bot.owner_shop_group.command(name="add", description="Add a global shop item")
    @owner_only(bot)
    async def owner_shop_add(interaction: discord.Interaction, name: str, description: str, price: app_commands.Range[int, 0, 2_147_483_647], item_type: Literal['collectible','role','cosmetic','consumable','special'], stock: int, emoji: str, image: str | None = None, role: discord.Role | None = None):
        if stock < -1: raise EconomyError("Stock must be -1 (unlimited) or zero and above.")
        item_id = "global-" + "-".join(name.lower().split())[:40]
        now = int(time.time()); assert bot.db.connection
        await bot.db.connection.execute("INSERT INTO shop_items(item_id,guild_id,name,description,price,item_type,stock,emoji,image_url,role_id,created_at,updated_at) VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?)", (item_id,name,description,price,item_type,stock,emoji,image,role.id if role else None,now,now)); await bot.db.connection.commit()
        await interaction.response.send_message(embed=vault_embed("Global item added", f"Created `{item_id}`."), ephemeral=True)

    @bot.owner_shop_group.command(name="remove", description="Remove a global shop item")
    @owner_only(bot)
    async def owner_shop_remove(interaction: discord.Interaction, item_id: str):
        class Confirm(discord.ui.View):
            @discord.ui.button(label="Confirm deletion", style=discord.ButtonStyle.danger)
            async def confirm(self, button: discord.ui.Button, click: discord.Interaction):
                if click.user.id != bot.settings.owner_id: return await click.response.send_message("Owner only.", ephemeral=True)
                assert bot.db.connection; await bot.db.connection.execute("DELETE FROM shop_items WHERE item_id=? AND guild_id IS NULL", (item_id,)); await bot.db.connection.commit(); button.disabled=True; await click.response.edit_message(content="Global item deleted.", view=self)
        await interaction.response.send_message("Delete this global item?", view=Confirm(), ephemeral=True)

    @bot.owner_shop_group.command(name="list", description="List global shop items")
    @owner_only(bot)
    async def owner_shop_list(interaction: discord.Interaction):
        assert bot.db.connection
        cursor = await bot.db.connection.execute("SELECT * FROM shop_items WHERE guild_id IS NULL ORDER BY name")
        rows = await cursor.fetchall()
        text = "\n".join(f"{row['emoji']} **{row['name']}** (`{row['item_id']}`) - {row['price']:,} {COIN} | stock: {'unlimited' if row['stock'] == -1 else row['stock']} | {'enabled' if row['enabled'] else 'disabled'}" for row in rows) or "The global shop is empty."
        await interaction.response.send_message(embed=vault_embed("Global shop", text), ephemeral=True)

    def make_owner_shop_command(action: str):
        async def managed(interaction: discord.Interaction, item_id: str, amount: int = 0, name: str | None = None, description: str | None = None, price: app_commands.Range[int, 0, 2_147_483_647] | None = None, emoji: str | None = None):
            assert bot.db.connection
            if action == "stock" and amount < -1: raise EconomyError("Stock must be -1 or zero and above.")
            if action == "edit":
                updates = {key: value for key, value in {"name": name, "description": description, "price": price, "emoji": emoji}.items() if value is not None}
                if not updates: raise EconomyError("Provide at least one field to edit.")
                values = [*updates.values(), int(time.time()), item_id]
                await bot.db.connection.execute(f"UPDATE shop_items SET {', '.join(f'{key}=?' for key in updates)}, updated_at=? WHERE item_id=? AND guild_id IS NULL", values)
            elif action == "stock": await bot.db.connection.execute("UPDATE shop_items SET stock=?,updated_at=? WHERE item_id=? AND guild_id IS NULL", (amount, int(time.time()), item_id))
            else: await bot.db.connection.execute("UPDATE shop_items SET enabled=?,updated_at=? WHERE item_id=? AND guild_id IS NULL", (1 if action == "enable" else 0, int(time.time()), item_id))
            await bot.db.connection.commit(); await interaction.response.send_message(embed=vault_embed("Global shop updated", f"`{item_id}` updated."), ephemeral=True)
        managed.__name__ = f"owner_shop_{action}"
        return managed

    for action in ("edit", "stock", "enable", "disable"):
        command = app_commands.Command(name=action, description=f"{action.title()} a global item", callback=make_owner_shop_command(action))
        bot.owner_shop_group.add_command(owner_only(bot)(command))

    @bot.economy_group.command(name="give", description="Give ES Coins")
    @admin_only()
    async def economy_give(interaction: discord.Interaction, user: discord.User, amount: app_commands.Range[int, 1, 2_147_483_647]):
        await bot.db.change_balance(user.id, amount, kind="admin_give", note=f"admin {interaction.user.id}"); await interaction.response.send_message(embed=vault_embed("Economy updated", f"Gave {user.mention} **{amount:,} {COIN}**."), ephemeral=True)

    @bot.economy_group.command(name="remove", description="Remove ES Coins")
    @admin_only()
    async def economy_remove(interaction: discord.Interaction, user: discord.User, amount: app_commands.Range[int, 1, 2_147_483_647]):
        await bot.db.change_balance(user.id, -amount, kind="admin_remove", note=f"admin {interaction.user.id}"); await interaction.response.send_message(embed=vault_embed("Economy updated", f"Removed **{amount:,} {COIN}** from {user.mention}."), ephemeral=True)

    @bot.economy_group.command(name="reset", description="Reset a user's balances")
    @admin_only()
    async def economy_reset(interaction: discord.Interaction, user: discord.User):
        account = await bot.db.account(user.id); await bot.db.change_balance(user.id, -account["wallet"], -account["bank"], kind="admin_reset", note=f"admin {interaction.user.id}"); await interaction.response.send_message(embed=vault_embed("Economy reset", f"Reset {user.mention}."), ephemeral=True)

    @bot.economy_group.command(name="set", description="Set a user's wallet balance")
    @admin_only()
    async def economy_set(interaction: discord.Interaction, user: discord.User, amount: app_commands.Range[int, 0, 2_147_483_647]):
        account = await bot.db.account(user.id); await bot.db.change_balance(user.id, amount - account["wallet"], kind="admin_set", note=f"admin {interaction.user.id}"); await interaction.response.send_message(embed=vault_embed("Economy updated", f"Set {user.mention}'s wallet to **{amount:,} {COIN}**."), ephemeral=True)

    @bot.server_shop_group.command(name="add", description="Add a server shop item")
    @admin_only()
    async def server_shop_add(interaction: discord.Interaction, name: str, description: str, price: app_commands.Range[int, 0, 2_147_483_647], item_type: Literal['collectible','role','cosmetic','consumable','special'], stock: int, emoji: str):
        if stock < -1: raise EconomyError("Stock must be -1 or zero and above.")
        item_id = f"{interaction.guild_id}-" + "-".join(name.lower().split())[:35]; now=int(time.time()); assert bot.db.connection
        await bot.db.connection.execute("INSERT INTO shop_items(item_id,guild_id,name,description,price,item_type,stock,emoji,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (item_id,interaction.guild_id,name,description,price,item_type,stock,emoji,now,now)); await bot.db.connection.commit(); await interaction.response.send_message(embed=vault_embed("Server item added", f"Created `{item_id}`."), ephemeral=True)

    @bot.server_shop_group.command(name="remove", description="Remove a server shop item")
    @admin_only()
    async def server_shop_remove(interaction: discord.Interaction, item_id: str):
        assert bot.db.connection; await bot.db.connection.execute("DELETE FROM shop_items WHERE item_id=? AND guild_id=?", (item_id, interaction.guild_id)); await bot.db.connection.commit(); await interaction.response.send_message(embed=vault_embed("Server item removed", f"Removed `{item_id}`."), ephemeral=True)

    @bot.server_shop_group.command(name="edit", description="Edit a server shop item")
    @admin_only()
    async def server_shop_edit(interaction: discord.Interaction, item_id: str, name: str | None = None, description: str | None = None, price: app_commands.Range[int, 0, 2_147_483_647] | None = None, emoji: str | None = None):
        updates = {key: value for key, value in {"name": name, "description": description, "price": price, "emoji": emoji}.items() if value is not None}
        if not updates: raise EconomyError("Provide at least one field to edit.")
        assert bot.db.connection
        values = [*updates.values(), int(time.time()), item_id, interaction.guild_id]
        await bot.db.connection.execute(f"UPDATE shop_items SET {', '.join(f'{key}=?' for key in updates)}, updated_at=? WHERE item_id=? AND guild_id=?", values)
        await bot.db.connection.commit()
        await interaction.response.send_message(embed=vault_embed("Server shop updated", f"`{item_id}` updated."), ephemeral=True)

    @bot.server_shop_group.command(name="list", description="List this server's shop items")
    @admin_only()
    async def server_shop_list(interaction: discord.Interaction):
        assert bot.db.connection
        cursor = await bot.db.connection.execute("SELECT * FROM shop_items WHERE guild_id=? ORDER BY name", (interaction.guild_id,))
        rows = await cursor.fetchall()
        text = "\n".join(f"{row['emoji']} **{row['name']}** (`{row['item_id']}`) - {row['price']:,} {COIN} | stock: {'unlimited' if row['stock'] == -1 else row['stock']}" for row in rows) or "This server shop is empty."
        await interaction.response.send_message(embed=vault_embed("Server shop", text), ephemeral=True)
