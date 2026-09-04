import pytest

from esn_vault.bot import notify_guild_owner
from esn_vault.db import Database, EconomyError


@pytest.mark.asyncio
async def test_transfer_is_atomic_and_rejects_self_payment(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    await db.change_balance(1, 100, kind="seed", note="test")
    await db.transfer(1, 2, 40)
    assert (await db.account(1))["wallet"] == 60
    assert (await db.account(2))["wallet"] == 40
    with pytest.raises(EconomyError):
        await db.transfer(1, 1, 1)
    await db.close()


@pytest.mark.asyncio
async def test_purchase_stock_and_unlimited_stock(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect(); await db.change_balance(1, 100, kind="seed", note="test")
    now = 1
    assert db.connection
    await db.connection.execute("INSERT INTO shop_items VALUES ('finite',NULL,'Finite','',25,'collectible',1,'x',NULL,NULL,1,?,?)", (now, now))
    await db.connection.execute("INSERT INTO shop_items VALUES ('unlimited',NULL,'Unlimited','',25,'collectible',-1,'x',NULL,NULL,1,?,?)", (now, now))
    await db.connection.commit()
    await db.buy(1, "finite"); assert (await db._fetchone("SELECT stock FROM shop_items WHERE item_id='finite'"))["stock"] == 0
    with pytest.raises(EconomyError): await db.buy(1, "finite")
    await db.buy(1, "unlimited"); assert (await db._fetchone("SELECT stock FROM shop_items WHERE item_id='unlimited'"))["stock"] == -1
    await db.close()


@pytest.mark.asyncio
async def test_role_purchase_rollback_restores_balance_inventory_and_stock(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    await db.change_balance(1, 100, kind="seed", note="test")
    assert db.connection
    await db.connection.execute("INSERT INTO shop_items VALUES ('role',NULL,'Role','',25,'role',1,'x',NULL,99,1,1,1)")
    await db.connection.commit()
    item = await db.buy(1, "role")
    await db.refund_purchase(1, item, reason="role grant failed")
    assert (await db.account(1))["wallet"] == 100
    assert await db._fetchone("SELECT * FROM inventory WHERE user_id=1 AND item_id='role'") is None
    assert (await db._fetchone("SELECT stock FROM shop_items WHERE item_id='role'"))["stock"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_daily_claim_is_atomic(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    results = await __import__("asyncio").gather(
        db.claim_reward(1, kind="daily", cooldown=86_400, reward=0, streak=True),
        db.claim_reward(1, kind="daily", cooldown=86_400, reward=0, streak=True),
        return_exceptions=True,
    )
    assert sum(result[0] == 1 for result in results if not isinstance(result, Exception)) == 1
    assert sum(isinstance(result, EconomyError) for result in results) == 1
    assert (await db.account(1))["wallet"] == 275
    await db.close()


@pytest.mark.asyncio
async def test_coin_collector_unlocks_and_applies_five_percent_bonus(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    credited, unlocked = await db.earn(1, 1_000, kind="work", note="test")
    assert (credited, unlocked) == (1_000, True)
    assert "coin-collector" in await db.achievements_for(1)
    credited, unlocked = await db.earn(1, 100, kind="work", note="test")
    assert (credited, unlocked) == (105, False)
    assert (await db.account(1))["wallet"] == 1_105
    await db.close()


@pytest.mark.asyncio
async def test_server_booster_receives_thirty_percent_more_earned_coins(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    credited, unlocked = await db.earn(1, 100, kind="work", note="test", server_boost=True)
    assert (credited, unlocked) == (130, False)
    await db.earn(1, 1_000, kind="work", note="test")
    credited, _ = await db.earn(1, 100, kind="work", note="test", server_boost=True)
    assert credited == 135
    await db.close()


@pytest.mark.asyncio
async def test_energy_drink_reduces_work_cooldown_and_is_consumed(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    assert db.connection
    await db.connection.execute("INSERT INTO inventory VALUES (1, 'energy-drink', 1)")
    await db.connection.commit()
    await db.use_item(1, "energy-drink")
    reward, cooldown = await db.perform_work(1, 100)
    assert (reward, cooldown) == (100, 15)
    assert await db._fetchone("SELECT * FROM inventory WHERE user_id=1 AND item_id='energy-drink'") is None
    await db.close()


@pytest.mark.asyncio
async def test_game_pass_and_matching_boost_are_consumed_atomically(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    assert db.connection
    await db.connection.executemany("INSERT INTO inventory VALUES (1, ?, 1)", [("trivia-ticket",), ("focus-tonic",)])
    await db.connection.commit()
    await db.use_item(1, "trivia-ticket")
    await db.use_item(1, "focus-tonic")
    assert await db.resolve_game(1, "trivia-ticket", 100, boost_item="focus-tonic") == 125
    assert (await db.account(1))["wallet"] == 125
    assert not await db.effect_is_ready(1, "trivia-ticket")
    assert not await db.effect_is_ready(1, "focus-tonic")
    await db.close()


@pytest.mark.asyncio
async def test_trade_requires_seal_and_exchanges_items_atomically(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    assert db.connection
    await db.connection.executemany("INSERT INTO inventory VALUES (?, ?, 1)", [(1, "trade-seal"), (1, "energy-drink"), (2, "focus-tonic")])
    await db.connection.commit()
    await db.use_item(1, "trade-seal")
    await db.trade_items(1, 2, "energy-drink", 1, "focus-tonic", 1)
    assert await db._fetchone("SELECT * FROM inventory WHERE user_id=1 AND item_id='focus-tonic'")
    assert await db._fetchone("SELECT * FROM inventory WHERE user_id=2 AND item_id='energy-drink'")
    assert not await db.effect_is_ready(1, "trade-seal")
    await db.close()


@pytest.mark.asyncio
async def test_market_buy_and_final_unit_sale_preserve_inventory_constraint(tmp_path):
    db = Database(tmp_path / "vault.sqlite3")
    await db.connect()
    await db.change_balance(1, 1_000, kind="seed", note="test")
    assert db.connection
    await db.connection.execute("INSERT INTO market VALUES ('vault-token', 100, 1)")
    await db.connection.commit()
    assert await db.buy_market_item(1, "vault-token") == 100
    assert (await db.account(1))["wallet"] == 900
    assert await db.sell_market_item(1, "vault-token") == 105
    assert (await db.account(1))["wallet"] == 1_005
    assert await db._fetchone("SELECT * FROM inventory WHERE user_id=1 AND item_id='vault-token'") is None
    await db.close()


@pytest.mark.asyncio
async def test_server_owner_audit_notification_is_sent():
    class Owner:
        def __init__(self):
            self.embed = None

        async def send(self, *, embed):
            self.embed = embed

    class Guild:
        def __init__(self, owner):
            self.owner = owner
            self.name = "Test Server"

    class Interaction:
        def __init__(self, owner):
            self.guild = Guild(owner)

    owner = Owner()
    await notify_guild_owner(Interaction(owner), "Shop purchase", "**Amount:** 100")
    assert owner.embed.title == "ESN Vault  |  Shop purchase"
    assert "Test Server" in owner.embed.description
