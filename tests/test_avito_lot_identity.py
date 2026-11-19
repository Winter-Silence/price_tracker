"""Тесты стабильного номера лота Avito (lot_id)."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from db import database


class LotIdUrlTest(unittest.TestCase):
    def test_extracts_numeric_suffix(self):
        assert database.lot_id_from_url(
            "https://www.avito.ru/moskva/kniga_8414522966") == 8414522966

    def test_ignores_query_and_trailing_slash(self):
        assert database.lot_id_from_url(
            "https://www.avito.ru/moskva/kniga_8414522966/?slocation=621540"
        ) == 8414522966

    def test_without_numeric_suffix_is_none(self):
        assert database.lot_id_from_url(
            "https://www.avito.ru/moskva/kniga") is None


OLD_URL = "https://www.avito.ru/moskva/kniga_8414522966"
NEW_URL = "https://www.avito.ru/moskva/kniga_perevykladano_8414522966"


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    """Каждый тест работает на своей временной базе, data/prices.db не трогаем."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = database.DB_PATH
        database.DB_PATH = os.path.join(self._tmp.name, "test.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self._orig_path
        self._tmp.cleanup()


class SyncByIdentityTest(_DbTestCase):
    async def test_same_lot_with_new_slug_keeps_one_row(self):
        item = SimpleNamespace(url=OLD_URL, title="Книга", price=100.0)
        synced = await database.sync_avito_items(7, [item])
        await database.mark_avito_items_notified([synced[OLD_URL]["id"]])

        item.url = NEW_URL
        synced = await database.sync_avito_items(7, [item])

        async with database.db_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, item_url, lot_id, notified_at "
                "FROM avito_search_items WHERE search_link_id = 7")
            rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["lot_id"] == 8414522966
        assert rows[0]["item_url"] == NEW_URL
        assert synced[NEW_URL]["id"] == rows[0]["id"]
        # Строка не была неактивна — отметка об уведомлении сохраняется.
        assert rows[0]["notified_at"] is not None

    async def test_item_state_is_keyed_by_lot_id_after_slug_change(self):
        item = SimpleNamespace(url=OLD_URL, title="Книга", price=100.0)
        await database.sync_avito_items(7, [item])

        state = await database.get_avito_item_state(7)
        assert 8414522966 in state
        assert OLD_URL not in state

        item.url = NEW_URL
        await database.sync_avito_items(7, [item])

        state = await database.get_avito_item_state(7)
        assert 8414522966 in state
        assert state[8414522966]["item_url"] == NEW_URL

    async def test_returning_lot_keeps_previous_price_state_by_id(self):
        item = SimpleNamespace(url=OLD_URL, title="Книга", price=100.0)
        await database.sync_avito_items(7, [item])
        await database.mark_missing_avito_items(7, set(), threshold=1)

        before = await database.get_avito_item_state(7)
        assert before[8414522966]["is_active"] == 0
        assert before[8414522966]["price"] == 100.0

        item.url = NEW_URL
        synced = await database.sync_avito_items(7, [item])
        assert synced[NEW_URL]["notified_at"] is None


class LotCheckIdentityTest(_DbTestCase):
    async def test_save_with_new_slug_updates_existing_row(self):
        await database.save_avito_lot_check(
            7, OLD_URL, "skipped", skip_reason="stale")
        await database.save_avito_lot_check(
            7, NEW_URL, "matched", confidence="strong")

        async with database.db_connection() as conn:
            cursor = await conn.execute(
                "SELECT item_url, lot_id, status, confidence "
                "FROM avito_lot_checks")
            rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["item_url"] == NEW_URL
        assert rows[0]["lot_id"] == 8414522966
        assert rows[0]["status"] == "matched"

        check = await database.get_avito_lot_check(lot_id=8414522966)
        assert check is not None and check["status"] == "matched"
        assert await database.get_avito_lot_check(NEW_URL) is not None
        # Старый slug в базе уже перезаписан новым.
        assert await database.get_avito_lot_check(OLD_URL) is None
        assert await database.get_avito_lot_check(lot_id=999) is None

    async def test_error_counter_follows_lot_id(self):
        await database.save_avito_lot_check(7, OLD_URL, "matched")
        attempts = await database.record_avito_lot_error(7, NEW_URL, "сбой")
        assert attempts == 1
        attempts = await database.record_avito_lot_error(7, NEW_URL, "сбой")
        assert attempts == 2
        async with database.db_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) AS n FROM avito_lot_checks")
            assert (await cursor.fetchone())["n"] == 1


LEGACY_AVITO_SCHEMA = """
CREATE TABLE avito_search_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_link_id INTEGER,
    item_url TEXT NOT NULL,
    item_title TEXT,
    price REAL,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP,
    notified_at TIMESTAMP,
    miss_streak INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN DEFAULT 1
);
CREATE TABLE avito_lot_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_link_id INTEGER,
    item_url TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    skip_reason TEXT,
    confidence TEXT,
    result_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class LegacyMigrationTest(_DbTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Откатываемся к схеме до появления lot_id и наполняем её дублями.
        async with database.db_connection() as conn:
            await conn.executescript(
                "DROP TABLE avito_search_items; DROP TABLE avito_lot_checks;")
            await conn.executescript(LEGACY_AVITO_SCHEMA)
            await conn.executemany(
                "INSERT INTO avito_search_items (search_link_id, item_url) "
                "VALUES (?, ?)",
                [
                    (1, OLD_URL),
                    (1, NEW_URL),                      # дубль лота внутри поиска
                    (2, OLD_URL),                      # тот же лот в другом поиске
                    (2, "https://www.avito.ru/moskva/kniga"),
                ],
            )
            await conn.execute(
                "INSERT INTO avito_lot_checks (search_link_id, item_url, status) "
                "VALUES (1, ?, 'matched')", (OLD_URL,))
            await conn.commit()

        await database.init_db()

    async def test_backfill_and_soft_duplicate_cleanup(self):
        async with database.db_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, search_link_id, lot_id, is_active "
                "FROM avito_search_items ORDER BY id")
            rows = [dict(r) for r in await cursor.fetchall()]
            cursor = await conn.execute(
                "SELECT item_url, lot_id, status FROM avito_lot_checks")
            checks = [dict(r) for r in await cursor.fetchall()]

        assert [r["lot_id"] for r in rows] == [8414522966, 8414522966,
                                               8414522966, None]
        # В группе (поиск, лот) живёт строка с наименьшим id, дубль скрыт.
        assert [r["is_active"] for r in rows] == [1, 0, 1, 1]
        assert checks == [{"item_url": OLD_URL, "lot_id": 8414522966,
                           "status": "matched"}]
