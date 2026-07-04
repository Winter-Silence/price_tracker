import aiosqlite
import os
from contextlib import asynccontextmanager

from utils.logger import logger

DB_PATH = os.getenv("DB_PATH", "./data/prices.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketplace_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES products(id),
    marketplace TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    last_price REAL,
    last_checked_at TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id INTEGER REFERENCES marketplace_links(id),
    price REAL NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    link_id INTEGER REFERENCES marketplace_links(id),
    threshold_price REAL NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    triggered_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def get_db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return await aiosqlite.connect(DB_PATH)


@asynccontextmanager
async def db_connection():
    conn = await get_db()
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


async def init_db():
    async with db_connection() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
    logger.info("Database initialized")


async def close_db():
    logger.info("Database connections closed")


async def get_or_create_user(telegram_id: int) -> int:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row:
            return row["id"]

        cursor = await conn.execute(
            "INSERT INTO users (telegram_id) VALUES (?)", (telegram_id,)
        )
        await conn.commit()
        logger.info("Created user telegram_id=%d", telegram_id)
        return cursor.lastrowid


async def add_product(name: str, user_id: int) -> int:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO products (name, created_by) VALUES (?, ?)",
            (name, user_id),
        )
        await conn.commit()
        logger.info("Added product '%s' by user_id=%d", name, user_id)
        return cursor.lastrowid


async def add_marketplace_link(product_id: int, marketplace: str, url: str) -> int:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO marketplace_links (product_id, marketplace, url) VALUES (?, ?, ?)",
            (product_id, marketplace, url),
        )
        await conn.commit()
        logger.info("Added link %s for product_id=%d", url, product_id)
        return cursor.lastrowid


async def get_active_links() -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, product_id, marketplace, url, last_price, last_checked_at "
            "FROM marketplace_links WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def save_price(link_id: int, price: float):
    async with db_connection() as conn:
        await conn.execute(
            "INSERT INTO price_history (link_id, price) VALUES (?, ?)",
            (link_id, price),
        )
        await conn.execute(
            "UPDATE marketplace_links SET last_price = ?, last_checked_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (price, link_id),
        )
        await conn.commit()
        logger.info("Saved price %.2f for link_id=%d", price, link_id)


async def get_price_history(link_id: int, limit: int = 10) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, link_id, price, recorded_at FROM price_history "
            "WHERE link_id = ? ORDER BY recorded_at DESC LIMIT ?",
            (link_id, limit),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def add_alert(user_id: int, link_id: int, threshold_price: float):
    async with db_connection() as conn:
        await conn.execute(
            "INSERT INTO alerts (user_id, link_id, threshold_price) VALUES (?, ?, ?)",
            (user_id, link_id, threshold_price),
        )
        await conn.commit()
        logger.info(
            "Added alert user_id=%d link_id=%d threshold=%.2f",
            user_id, link_id, threshold_price,
        )


async def get_triggered_alerts(link_id: int, new_price: float) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, user_id, link_id, threshold_price FROM alerts "
            "WHERE link_id = ? AND is_active = 1 AND threshold_price >= ? AND triggered_at IS NULL",
            (link_id, new_price),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def reset_alerts_above_threshold(link_id: int, new_price: float):
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE alerts SET triggered_at = NULL "
            "WHERE link_id = ? AND is_active = 1 AND triggered_at IS NOT NULL AND threshold_price < ?",
            (link_id, new_price),
        )
        await conn.commit()


async def mark_alert_triggered(alert_id: int):
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE alerts SET triggered_at = CURRENT_TIMESTAMP WHERE id = ?",
            (alert_id,),
        )
        await conn.commit()


async def get_user_alerts(user_id: int) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT a.id, a.user_id, a.link_id, a.threshold_price, a.is_active, a.created_at,
                   ml.marketplace, ml.url, p.name AS product_name, p.id AS product_id
            FROM alerts a
            JOIN marketplace_links ml ON ml.id = a.link_id
            JOIN products p ON p.id = ml.product_id
            WHERE a.user_id = ? AND a.is_active = 1 AND ml.is_active = 1
            ORDER BY a.created_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_alert_threshold(alert_id: int, user_id: int, new_threshold: float):
    async with db_connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE alerts SET threshold_price = ?
            WHERE id = ? AND user_id = ? AND is_active = 1
            """,
            (new_threshold, alert_id, user_id),
        )
        await conn.commit()
    logger.info("Updated alert_id=%d threshold=%.2f for user_id=%d", alert_id, new_threshold, user_id)
    return cursor.rowcount > 0


async def get_user_products(user_id: int) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT p.id, p.name, p.created_at, "
            "ml.id AS link_id, ml.marketplace, ml.url, ml.last_price, ml.last_checked_at "
            "FROM products p "
            "JOIN marketplace_links ml ON ml.product_id = p.id "
            "WHERE p.created_by = ? AND ml.is_active = 1 "
            "ORDER BY p.created_at DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_link(link_id: int, user_id: int):
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE marketplace_links SET is_active = 0 "
            "WHERE id = ? AND product_id IN (SELECT id FROM products WHERE created_by = ?)",
            (link_id, user_id),
        )
        await conn.commit()
        logger.info("Deactivated link_id=%d for user_id=%d", link_id, user_id)


async def get_product_by_id(product_id: int) -> dict | None:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, created_by, created_at FROM products WHERE id = ?",
            (product_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def get_user_alerts(user_id: int) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT a.id, a.link_id, a.threshold_price, a.is_active,
                   p.name AS product_name, ml.marketplace
            FROM alerts a
            JOIN marketplace_links ml ON ml.id = a.link_id
            JOIN products p ON p.id = ml.product_id
            WHERE a.user_id = ? AND a.is_active = 1
            ORDER BY p.name
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_alert_threshold(alert_id: int, user_id: int, new_threshold: float) -> bool:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "UPDATE alerts SET threshold_price = ?, triggered_at = NULL WHERE id = ? AND user_id = ? AND is_active = 1",
            (new_threshold, alert_id, user_id),
        )
        await conn.commit()
    return cursor.rowcount > 0
