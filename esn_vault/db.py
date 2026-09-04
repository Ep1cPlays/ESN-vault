from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import aiosqlite

USABLE_ITEMS = {
    "energy-drink": ("Energy Drink", "Halves /work cooldown for 10 minutes.", 175, "energy-drink"),
    "double-shift-pass": ("Double Shift Pass", "Doubles your next /work payout.", 300, "double-shift-pass"),
    "coin-magnet": ("Coin Magnet", "Adds 15% to your next /work payout.", 225, "coin-magnet"),
    "focus-tonic": ("Focus Tonic", "Adds 25% to your next trivia reward.", 180, "focus-tonic"),
    "explorer-map": ("Explorer Map", "Adds 25% to your next scavenger reward.", 180, "explorer-map"),
    "trivia-ticket": ("Trivia Ticket", "Grants one /trivia attempt.", 100, "trivia-ticket"),
    "scavenger-kit": ("Scavenger Kit", "Grants one /scavenge attempt.", 100, "scavenger-kit"),
    "riddle-key": ("Riddle Key", "Grants one /riddle attempt.", 125, "riddle-key"),
    "trade-seal": ("Trade Seal", "Authorizes one item trade offer.", 75, "trade-seal"),
    "vault-pass": ("Vault Pass", "Grants one /vault-dive attempt.", 225, "vault-pass"),
}


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS accounts (user_id INTEGER PRIMARY KEY, wallet INTEGER NOT NULL DEFAULT 0 CHECK(wallet >= 0), bank INTEGER NOT NULL DEFAULT 0 CHECK(bank >= 0), daily_streak INTEGER NOT NULL DEFAULT 0, last_daily INTEGER, last_weekly INTEGER, last_work INTEGER, last_interest INTEGER, earned_coins INTEGER NOT NULL DEFAULT 0, work_count INTEGER NOT NULL DEFAULT 0, purchase_count INTEGER NOT NULL DEFAULT 0, payment_count INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, related_user_id INTEGER, note TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inventory (user_id INTEGER NOT NULL, item_id TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0), PRIMARY KEY(user_id, item_id));
CREATE TABLE IF NOT EXISTS shop_items (item_id TEXT PRIMARY KEY, guild_id INTEGER, name TEXT NOT NULL, description TEXT NOT NULL, price INTEGER NOT NULL CHECK(price >= 0), item_type TEXT NOT NULL, stock INTEGER NOT NULL CHECK(stock >= -1), emoji TEXT NOT NULL, image_url TEXT, role_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS market (item_id TEXT PRIMARY KEY, price INTEGER NOT NULL CHECK(price >= 1), updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reputation (user_id INTEGER PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0 CHECK(points >= 0));
CREATE TABLE IF NOT EXISTS achievements (user_id INTEGER NOT NULL, achievement_id TEXT NOT NULL, unlocked_at INTEGER NOT NULL, PRIMARY KEY(user_id, achievement_id));
CREATE TABLE IF NOT EXISTS quests (user_id INTEGER NOT NULL, quest_id TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, completed_at INTEGER, expires_at INTEGER NOT NULL, PRIMARY KEY(user_id, quest_id));
CREATE TABLE IF NOT EXISTS active_effects (user_id INTEGER NOT NULL, effect_id TEXT NOT NULL, expires_at INTEGER, charges INTEGER NOT NULL DEFAULT 0 CHECK(charges >= 0), PRIMARY KEY(user_id, effect_id));
CREATE INDEX IF NOT EXISTS transactions_user_idx ON transactions(user_id, created_at DESC);
"""


class EconomyError(Exception):
    """Expected user-facing economy failure."""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(SCHEMA)
        await self._migrate_accounts()
        await self._seed_usable_items()
        await self.connection.commit()

    async def _seed_usable_items(self) -> None:
        assert self.connection
        now = int(time.time())
        for item_id, (name, description, price, _) in USABLE_ITEMS.items():
            await self.connection.execute(
                "INSERT OR IGNORE INTO shop_items(item_id,guild_id,name,description,price,item_type,stock,emoji,created_at,updated_at) VALUES(?,NULL,?,?,?,'consumable',-1,?, ?, ?)",
                (item_id, name, description, price, "⚡" if item_id == "energy-drink" else "🎟️", now, now),
            )

    async def _migrate_accounts(self) -> None:
        """Add account columns when opening a database created by an earlier bot version."""
        assert self.connection
        cursor = await self.connection.execute("PRAGMA table_info(accounts)")
        columns = {row["name"] for row in await cursor.fetchall()}
        additions = {
            "last_interest": "INTEGER",
            "earned_coins": "INTEGER NOT NULL DEFAULT 0",
            "work_count": "INTEGER NOT NULL DEFAULT 0",
            "purchase_count": "INTEGER NOT NULL DEFAULT 0",
            "payment_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                await self.connection.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        assert self.connection
        cursor = await self.connection.execute(sql, params)
        return await cursor.fetchone()

    async def account(self, user_id: int) -> aiosqlite.Row:
        assert self.connection
        now = int(time.time())
        await self.connection.execute("INSERT OR IGNORE INTO accounts(user_id, created_at) VALUES (?, ?)", (user_id, now))
        await self.connection.commit()
        row = await self._fetchone("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
        assert row
        return row

    async def change_balance(self, user_id: int, wallet_delta: int = 0, bank_delta: int = 0, *, kind: str = "adjustment", note: str = "", related_user_id: int | None = None) -> None:
        if wallet_delta + bank_delta == 0 and not note:
            raise EconomyError("That transaction has no effect.")
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            row = await self._fetchone("SELECT wallet, bank FROM accounts WHERE user_id = ?", (user_id,))
            assert row
            if row["wallet"] + wallet_delta < 0 or row["bank"] + bank_delta < 0:
                await self.connection.rollback()
                raise EconomyError("Insufficient ES Coins.")
            await self.connection.execute("UPDATE accounts SET wallet = wallet + ?, bank = bank + ? WHERE user_id = ?", (wallet_delta, bank_delta, user_id))
            amount = wallet_delta + bank_delta
            await self.connection.execute("INSERT INTO transactions(user_id, kind, amount, related_user_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?)", (user_id, kind, amount, related_user_id, note, int(time.time())))
            await self.connection.commit()

    async def _credit_earned(self, user_id: int, amount: int, *, kind: str, note: str, server_boost: bool = False) -> tuple[int, bool]:
        """Credit virtual earnings inside an open transaction and apply earned bonuses."""
        assert self.connection
        achievement = await self._fetchone(
            "SELECT 1 FROM achievements WHERE user_id=? AND achievement_id='coin-collector'",
            (user_id,),
        )
        bonus = amount * 5 // 100 if achievement else 0
        bonus += amount * 30 // 100 if server_boost else 0
        credited = amount + bonus
        await self.connection.execute(
            "UPDATE accounts SET wallet=wallet+?, earned_coins=earned_coins+? WHERE user_id=?",
            (credited, credited, user_id),
        )
        await self.connection.execute(
            "INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
            (user_id, kind, credited, note, int(time.time())),
        )
        account = await self._fetchone("SELECT earned_coins FROM accounts WHERE user_id=?", (user_id,))
        assert account
        unlocked = False
        if account["earned_coins"] >= 1_000:
            cursor = await self.connection.execute(
                "INSERT OR IGNORE INTO achievements(user_id,achievement_id,unlocked_at) VALUES(?, 'coin-collector', ?)",
                (user_id, int(time.time())),
            )
            unlocked = cursor.rowcount == 1
        return credited, unlocked

    async def earn(self, user_id: int, amount: int, *, kind: str, note: str, server_boost: bool = False) -> tuple[int, bool]:
        """Award earned ES Coins, applying unlocked earning bonuses atomically."""
        if amount <= 0:
            raise EconomyError("Earned amount must be positive.")
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            credited, unlocked = await self._credit_earned(user_id, amount, kind=kind, note=note, server_boost=server_boost)
            if kind == "work":
                await self.connection.execute("UPDATE accounts SET work_count=work_count+1 WHERE user_id=?", (user_id,))
            await self.connection.commit()
            return credited, unlocked

    async def use_item(self, user_id: int, item_id: str) -> tuple[str, int | None]:
        """Consume a supported item and persist its timed or one-use effect."""
        item_id = item_id.lower()
        if item_id not in USABLE_ITEMS:
            raise EconomyError("That item cannot be used.")
        async with self._lock:
            assert self.connection
            await self.connection.execute("BEGIN IMMEDIATE")
            owned = await self._fetchone("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            if not owned:
                await self.connection.rollback()
                raise EconomyError("You do not own that item.")
            if owned["quantity"] == 1:
                await self.connection.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            else:
                await self.connection.execute("UPDATE inventory SET quantity=quantity-1 WHERE user_id=? AND item_id=?", (user_id, item_id))
            now = int(time.time())
            if item_id == "energy-drink":
                expires_at = now + 600
                await self.connection.execute("INSERT INTO active_effects(user_id,effect_id,expires_at,charges) VALUES(?,?,?,0) ON CONFLICT(user_id,effect_id) DO UPDATE SET expires_at=excluded.expires_at", (user_id, item_id, expires_at))
                detail = "Your /work cooldown is 15 seconds for 10 minutes."
            else:
                expires_at = None
                await self.connection.execute("INSERT INTO active_effects(user_id,effect_id,expires_at,charges) VALUES(?,?,NULL,1) ON CONFLICT(user_id,effect_id) DO UPDATE SET charges=charges+1", (user_id, item_id))
                detail = USABLE_ITEMS[item_id][1]
            await self.connection.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?, 'item_use', 0, ?, ?)", (user_id, item_id, now))
            await self.connection.commit()
            return detail, expires_at

    async def _consume_effect(self, user_id: int, effect_id: str, *, required: bool = False) -> bool:
        assert self.connection
        now = int(time.time())
        effect = await self._fetchone("SELECT expires_at,charges FROM active_effects WHERE user_id=? AND effect_id=?", (user_id, effect_id))
        active = bool(effect and ((effect["expires_at"] is not None and effect["expires_at"] > now) or effect["charges"] > 0))
        if required and not active:
            raise EconomyError(f"Use a {USABLE_ITEMS[effect_id][0]} before starting this activity.")
        if not active:
            return False
        if effect["charges"] > 0:
            await self.connection.execute("UPDATE active_effects SET charges=charges-1 WHERE user_id=? AND effect_id=?", (user_id, effect_id))
            await self.connection.execute("DELETE FROM active_effects WHERE user_id=? AND effect_id=? AND charges=0 AND expires_at IS NULL", (user_id, effect_id))
        return True

    async def perform_work(self, user_id: int, base_reward: int, *, server_boost: bool = False) -> tuple[int, int]:
        """Perform work with active item effects and an item-aware cooldown."""
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            account = await self._fetchone("SELECT last_work FROM accounts WHERE user_id=?", (user_id,))
            assert account
            energy_active = await self._consume_effect(user_id, "energy-drink")
            cooldown = 15 if energy_active else 30
            now = int(time.time())
            if account["last_work"] and now - account["last_work"] < cooldown:
                await self.connection.rollback()
                raise EconomyError(f"Your next shift is ready in {cooldown - (now - account['last_work'])}s.")
            reward = base_reward
            if await self._consume_effect(user_id, "double-shift-pass"):
                reward *= 2
            if await self._consume_effect(user_id, "coin-magnet"):
                reward += reward * 15 // 100
            credited, _ = await self._credit_earned(user_id, reward, kind="work", note="work reward", server_boost=server_boost)
            await self.connection.execute("UPDATE accounts SET work_count=work_count+1,last_work=? WHERE user_id=?", (now, user_id))
            await self.connection.commit()
            return credited, cooldown

    async def effect_is_ready(self, user_id: int, effect_id: str) -> bool:
        assert self.connection
        effect = await self._fetchone("SELECT expires_at,charges FROM active_effects WHERE user_id=? AND effect_id=?", (user_id, effect_id))
        return bool(effect and ((effect["expires_at"] is not None and effect["expires_at"] > int(time.time())) or effect["charges"] > 0))

    async def resolve_game(self, user_id: int, access_item: str, base_reward: int, *, boost_item: str | None = None, server_boost: bool = False) -> int:
        """Consume a game pass and award a successful virtual mini-game result."""
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            await self._consume_effect(user_id, access_item, required=True)
            reward = base_reward
            if boost_item and await self._consume_effect(user_id, boost_item):
                reward += reward * 25 // 100
            credited, _ = await self._credit_earned(user_id, reward, kind="mini_game", note=access_item, server_boost=server_boost)
            await self.connection.commit()
            return credited

    async def trade_items(self, sender_id: int, recipient_id: int, offered_item: str, offered_quantity: int, requested_item: str, requested_quantity: int) -> None:
        """Exchange inventory items after consuming the sender's one-use Trade Seal."""
        if sender_id == recipient_id:
            raise EconomyError("You cannot trade with yourself.")
        if offered_quantity <= 0 or requested_quantity <= 0 or offered_item == requested_item:
            raise EconomyError("Trade items must be different and quantities must be positive.")
        async with self._lock:
            assert self.connection
            await self.connection.execute("BEGIN IMMEDIATE")
            await self._consume_effect(sender_id, "trade-seal", required=True)
            offered = await self._fetchone("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (sender_id, offered_item))
            requested = await self._fetchone("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (recipient_id, requested_item))
            if not offered or offered["quantity"] < offered_quantity or not requested or requested["quantity"] < requested_quantity:
                await self.connection.rollback()
                raise EconomyError("One side no longer owns the required trade items.")
            for user_id, item_id, quantity in ((sender_id, offered_item, offered_quantity), (recipient_id, requested_item, requested_quantity)):
                owned = await self._fetchone("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
                if owned["quantity"] == quantity:
                    await self.connection.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
                else:
                    await self.connection.execute("UPDATE inventory SET quantity=quantity-? WHERE user_id=? AND item_id=?", (quantity, user_id, item_id))
            for user_id, item_id, quantity in ((recipient_id, offered_item, offered_quantity), (sender_id, requested_item, requested_quantity)):
                await self.connection.execute("INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,?) ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity", (user_id, item_id, quantity))
            now = int(time.time())
            await self.connection.execute("INSERT INTO transactions(user_id,kind,amount,related_user_id,note,created_at) VALUES(?, 'item_trade', 0, ?, ?, ?), (?, 'item_trade', 0, ?, ?, ?)", (sender_id, recipient_id, f"gave {offered_item}", now, recipient_id, sender_id, f"gave {requested_item}", now))
            await self.connection.commit()

    async def transfer(self, payer: int, recipient: int, amount: int) -> None:
        if payer == recipient:
            raise EconomyError("You cannot pay yourself.")
        if amount <= 0:
            raise EconomyError("Amount must be positive.")
        async with self._lock:
            assert self.connection
            await self.account(payer); await self.account(recipient)
            await self.connection.execute("BEGIN IMMEDIATE")
            row = await self._fetchone("SELECT wallet FROM accounts WHERE user_id = ?", (payer,))
            if not row or row["wallet"] < amount:
                await self.connection.rollback(); raise EconomyError("Insufficient wallet funds.")
            now = int(time.time())
            await self.connection.execute("UPDATE accounts SET wallet = wallet - ? WHERE user_id = ?", (amount, payer))
            await self.connection.execute("UPDATE accounts SET wallet = wallet + ? WHERE user_id = ?", (amount, recipient))
            await self.connection.execute("UPDATE accounts SET payment_count=payment_count+1 WHERE user_id=?", (payer,))
            await self.connection.execute("INSERT INTO transactions(user_id, kind, amount, related_user_id, note, created_at) VALUES (?, 'payment', ?, ?, 'sent', ?), (?, 'payment', ?, ?, 'received', ?)", (payer, -amount, recipient, now, recipient, amount, payer, now))
            await self.connection.commit()

    async def buy(self, user_id: int, item_id: str, *, guild_id: int | None = None) -> aiosqlite.Row:
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            item = await self._fetchone("SELECT * FROM shop_items WHERE item_id = ? AND enabled = 1 AND (guild_id IS NULL OR guild_id = ?)", (item_id, guild_id))
            if not item: await self.connection.rollback(); raise EconomyError("That item is unavailable.")
            if item["stock"] == 0: await self.connection.rollback(); raise EconomyError("That item is out of stock.")
            account = await self._fetchone("SELECT wallet FROM accounts WHERE user_id = ?", (user_id,))
            if account["wallet"] < item["price"]: await self.connection.rollback(); raise EconomyError("Insufficient wallet funds.")
            if item["stock"] > 0:
                await self.connection.execute("UPDATE shop_items SET stock = stock - 1, updated_at = ? WHERE item_id = ? AND stock > 0", (int(time.time()), item_id))
            await self.connection.execute("UPDATE accounts SET wallet = wallet - ? WHERE user_id = ?", (item["price"], user_id))
            await self.connection.execute("INSERT INTO inventory(user_id, item_id, quantity) VALUES (?, ?, 1) ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+1", (user_id, item_id))
            await self.connection.execute("UPDATE accounts SET purchase_count=purchase_count+1 WHERE user_id=?", (user_id,))
            await self.connection.execute("INSERT INTO transactions(user_id, kind, amount, note, created_at) VALUES (?, 'purchase', ?, ?, ?)", (user_id, -item["price"], item_id, int(time.time())))
            await self.connection.commit()
            return item

    async def refund_purchase(self, user_id: int, item: aiosqlite.Row, *, reason: str) -> None:
        """Reverse a completed purchase when its Discord-side reward cannot be granted."""
        async with self._lock:
            assert self.connection
            await self.connection.execute("BEGIN IMMEDIATE")
            owned = await self._fetchone(
                "SELECT quantity FROM inventory WHERE user_id=? AND item_id=?",
                (user_id, item["item_id"]),
            )
            if not owned or owned["quantity"] < 1:
                await self.connection.rollback()
                raise EconomyError("Purchase rollback could not be completed.")
            if owned["quantity"] == 1:
                await self.connection.execute(
                    "DELETE FROM inventory WHERE user_id=? AND item_id=?",
                    (user_id, item["item_id"]),
                )
            else:
                await self.connection.execute(
                    "UPDATE inventory SET quantity=quantity-1 WHERE user_id=? AND item_id=?",
                    (user_id, item["item_id"]),
                )
            if item["stock"] >= 0:
                await self.connection.execute(
                    "UPDATE shop_items SET stock=stock+1, updated_at=? WHERE item_id=?",
                    (int(time.time()), item["item_id"]),
                )
            await self.connection.execute(
                "UPDATE accounts SET wallet=wallet+? WHERE user_id=?",
                (item["price"], user_id),
            )
            await self.connection.execute(
                "INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?, 'refund', ?, ?, ?)",
                (user_id, item["price"], reason, int(time.time())),
            )
            await self.connection.commit()

    async def claim_reward(self, user_id: int, *, kind: str, cooldown: int, reward: int, streak: bool = False, server_boost: bool = False) -> int:
        """Claim a time-gated reward atomically and return the resulting streak."""
        if kind not in {"daily", "weekly"}:
            raise ValueError("Unsupported reward kind")
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            column = f"last_{kind}"
            account = await self._fetchone(
                f"SELECT daily_streak, {column} FROM accounts WHERE user_id=?", (user_id,)
            )
            assert account
            now = int(time.time())
            if account[column] and now - account[column] < cooldown:
                await self.connection.rollback()
                raise EconomyError(f"Your {kind} reward is not ready yet.")
            daily_streak = 0
            if streak:
                daily_streak = account["daily_streak"] + 1 if account[column] and now - account[column] < cooldown * 2 else 1
                reward = min(250 + daily_streak * 25, 1_000)
                credited, _ = await self._credit_earned(user_id, reward, kind="daily", note=f"streak {daily_streak}", server_boost=server_boost)
                await self.connection.execute("UPDATE accounts SET daily_streak=?, last_daily=? WHERE user_id=?", (daily_streak, now, user_id))
            else:
                credited, _ = await self._credit_earned(user_id, reward, kind="weekly", note="weekly reward", server_boost=server_boost)
                await self.connection.execute("UPDATE accounts SET last_weekly=? WHERE user_id=?", (now, user_id))
            await self.connection.commit()
            return daily_streak, credited

    async def claim_interest(self, user_id: int, *, server_boost: bool = False) -> int:
        """Credit one day of virtual bank interest, capped to keep the economy stable."""
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            account = await self._fetchone("SELECT bank,last_interest FROM accounts WHERE user_id=?", (user_id,))
            assert account
            now = int(time.time())
            if account["last_interest"] and now - account["last_interest"] < 86_400:
                await self.connection.rollback()
                raise EconomyError("Your bank interest is not ready yet.")
            if account["bank"] <= 0:
                await self.connection.rollback()
                raise EconomyError("Deposit ES Coins into your bank before claiming interest.")
            interest = min(1_000, max(1, account["bank"] // 100))
            if server_boost:
                interest += interest * 30 // 100
            await self.connection.execute("UPDATE accounts SET bank=bank+?, last_interest=? WHERE user_id=?", (interest, now, user_id))
            await self.connection.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?, 'interest', ?, '1% bank interest', ?)", (user_id, interest, now))
            await self.connection.commit()
            return interest

    async def achievements_for(self, user_id: int) -> set[str]:
        assert self.connection
        cursor = await self.connection.execute("SELECT achievement_id FROM achievements WHERE user_id=?", (user_id,))
        return {row["achievement_id"] for row in await cursor.fetchall()}

    async def transactions_for(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        assert self.connection
        cursor = await self.connection.execute("SELECT kind,amount,note,created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return await cursor.fetchall()

    async def sell_market_item(self, user_id: int, item_id: str) -> int:
        """Sell one owned collectible and update its price in the same transaction."""
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            listing = await self._fetchone("SELECT price FROM market WHERE item_id=?", (item_id,))
            if not listing:
                await self.connection.rollback()
                raise EconomyError("That collectible is not listed on the market.")
            price = listing["price"]
            owned = await self._fetchone("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            if not owned or owned["quantity"] < 1:
                await self.connection.rollback()
                raise EconomyError("You do not own that collectible.")
            if owned["quantity"] == 1:
                await self.connection.execute("DELETE FROM inventory WHERE user_id=? AND item_id=?", (user_id, item_id))
            else:
                await self.connection.execute("UPDATE inventory SET quantity=quantity-1 WHERE user_id=? AND item_id=?", (user_id, item_id))
            await self.connection.execute("UPDATE accounts SET wallet=wallet+? WHERE user_id=?", (price, user_id))
            await self.connection.execute("UPDATE market SET price=?, updated_at=? WHERE item_id=?", (max(1, price - max(1, price // 25)), int(time.time()), item_id))
            await self.connection.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?, 'market_sale', ?, ?, ?)", (user_id, price, item_id, int(time.time())))
            await self.connection.commit()
            return price

    async def buy_market_item(self, user_id: int, item_id: str) -> int:
        """Buy a virtual collectible at its current persisted market price."""
        async with self._lock:
            assert self.connection
            await self.account(user_id)
            await self.connection.execute("BEGIN IMMEDIATE")
            listing = await self._fetchone("SELECT price FROM market WHERE item_id=?", (item_id,))
            account = await self._fetchone("SELECT wallet FROM accounts WHERE user_id=?", (user_id,))
            if not listing:
                await self.connection.rollback()
                raise EconomyError("That collectible is not listed on the market.")
            price = listing["price"]
            if account["wallet"] < price:
                await self.connection.rollback()
                raise EconomyError("Insufficient wallet funds.")
            await self.connection.execute("UPDATE accounts SET wallet=wallet-? WHERE user_id=?", (price, user_id))
            await self.connection.execute("INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,1) ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+1", (user_id, item_id))
            await self.connection.execute("UPDATE market SET price=?, updated_at=? WHERE item_id=?", (max(1, price + max(1, price // 20)), int(time.time()), item_id))
            await self.connection.execute("INSERT INTO transactions(user_id,kind,amount,note,created_at) VALUES(?, 'market_purchase', ?, ?, ?)", (user_id, -price, item_id, int(time.time())))
            await self.connection.commit()
            return price

    async def top_accounts(self, limit: int = 10) -> list[aiosqlite.Row]:
        assert self.connection
        cursor = await self.connection.execute("SELECT * FROM accounts ORDER BY wallet + bank DESC LIMIT ?", (limit,))
        return await cursor.fetchall()