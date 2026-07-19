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
    threshold_price REAL NOT NULL DEFAULT 0,
    alert_active BOOLEAN DEFAULT 1,
    triggered_at TIMESTAMP,
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
    link_id INTEGER NOT NULL,
    link_kind TEXT NOT NULL DEFAULT 'product',
    threshold_price REAL NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    triggered_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_privileges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    marketplace TEXT NOT NULL,
    privilege_type TEXT NOT NULL,
    UNIQUE(user_id, marketplace, privilege_type)
);

CREATE TABLE IF NOT EXISTS search_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES products(id),
    marketplace TEXT NOT NULL,
    search_url TEXT NOT NULL,
    title_filter TEXT NOT NULL,
    last_price REAL,
    last_resolved_url TEXT,
    last_resolved_title TEXT,
    last_checked_at TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS search_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_link_id INTEGER REFERENCES search_links(id),
    price REAL NOT NULL,
    resolved_url TEXT,
    resolved_title TEXT,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


MIGRATIONS = [
    "ALTER TABLE price_history ADD COLUMN privilege_type TEXT NOT NULL DEFAULT 'standard'",
    "ALTER TABLE alerts ADD COLUMN link_kind TEXT NOT NULL DEFAULT 'product'",
    "ALTER TABLE products ADD COLUMN threshold_price REAL NOT NULL DEFAULT 0",
    "ALTER TABLE products ADD COLUMN alert_active BOOLEAN DEFAULT 1",
    "ALTER TABLE products ADD COLUMN triggered_at TIMESTAMP",
]


async def init_db():
    async with db_connection() as conn:
        await conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            try:
                await conn.execute(migration)
                logger.info("Migration applied: %s", migration[:60])
            except Exception:
                pass

        # One-time data migration: copy threshold from per-link alerts
        # to a single column on products.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM pragma_table_info('products') "
            "WHERE name = 'threshold_price'"
        )
        col_added = (await cursor.fetchone())["n"] > 0
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM pragma_table_info('products') "
            "WHERE name = 'alert_active'"
        )
        active_added = (await cursor.fetchone())["n"] > 0

        if col_added and active_added:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM products WHERE threshold_price = 0"
            )
            zero_threshold_products = (await cursor.fetchone())["n"]

            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE is_active = 1"
            )
            active_alerts = (await cursor.fetchone())["n"]

            # Run only once — when products are still at the default 0 threshold
            # but there are active alerts carried over from old schema.
            if zero_threshold_products > 0 and active_alerts > 0:
                await conn.execute(
                    """
                    UPDATE products
                    SET threshold_price = COALESCE((
                            SELECT MAX(a.threshold_price)
                            FROM alerts a
                            JOIN marketplace_links ml ON ml.id = a.link_id
                            WHERE ml.product_id = products.id
                              AND a.is_active = 1
                              AND a.link_kind = 'product'
                        ), 0),
                        alert_active = COALESCE((
                            SELECT CASE WHEN EXISTS(
                                SELECT 1 FROM alerts a
                                JOIN marketplace_links ml ON ml.id = a.link_id
                                WHERE ml.product_id = products.id
                                  AND a.is_active = 1
                                  AND a.link_kind = 'product'
                                  AND a.threshold_price > 0
                            ) THEN 1 ELSE 0 END
                        ), 0)
                    WHERE EXISTS (
                        SELECT 1 FROM alerts a
                        JOIN marketplace_links ml ON ml.id = a.link_id
                        WHERE ml.product_id = products.id AND a.is_active = 1
                    )
                    """,
                )
                # Search links: keep alerts as-is, they still work for the search poller.
                logger.info(
                    "Migrated threshold_price from alerts to products (product alerts)"
                )
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


async def add_product(name: str, user_id: int, threshold_price: float = 0.0) -> int:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO products (name, created_by, threshold_price, alert_active) "
            "VALUES (?, ?, ?, ?)",
            (name, user_id, threshold_price, 1 if threshold_price > 0 else 0),
        )
        await conn.commit()
        logger.info(
            "Added product '%s' by user_id=%d threshold=%.2f",
            name, user_id, threshold_price,
        )
        return cursor.lastrowid


async def update_product_threshold(
    product_id: int, user_id: int, new_threshold: float,
) -> bool:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "UPDATE products SET threshold_price = ?, alert_active = ?, triggered_at = NULL "
            "WHERE id = ? AND created_by = ?",
            (
                new_threshold,
                1 if new_threshold > 0 else 0,
                product_id,
                user_id,
            ),
        )
        await conn.commit()
    return cursor.rowcount > 0


async def mark_product_triggered(product_id: int):
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE products SET triggered_at = CURRENT_TIMESTAMP WHERE id = ?",
            (product_id,),
        )
        await conn.commit()


async def reset_product_triggered(product_id: int):
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE products SET triggered_at = NULL WHERE id = ?",
            (product_id,),
        )
        await conn.commit()


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
            "SELECT ml.id, ml.product_id, ml.marketplace, ml.url, ml.last_price, ml.last_checked_at, "
            "p.threshold_price, p.alert_active, p.triggered_at, p.name AS product_name, "
            "p.created_by AS user_id "
            "FROM marketplace_links ml "
            "JOIN products p ON p.id = ml.product_id "
            "WHERE ml.is_active = 1"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def save_price(link_id: int, price: float, privilege_type: str = "standard"):
    async with db_connection() as conn:
        await conn.execute(
            "INSERT INTO price_history (link_id, price, privilege_type) VALUES (?, ?, ?)",
            (link_id, price, privilege_type),
        )
        await conn.execute(
            "UPDATE marketplace_links SET last_price = ?, last_checked_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (price, link_id),
        )
        await conn.commit()
        logger.info("Saved price %.2f (tier=%s) for link_id=%d", price, privilege_type, link_id)


async def get_price_history(link_id: int, privilege_type: str | None = None, limit: int = 10) -> list[dict]:
    async with db_connection() as conn:
        if privilege_type:
            cursor = await conn.execute(
                "SELECT id, link_id, price, privilege_type, recorded_at FROM price_history "
                "WHERE link_id = ? AND privilege_type = ? ORDER BY recorded_at DESC LIMIT ?",
                (link_id, privilege_type, limit),
            )
        else:
            cursor = await conn.execute(
                "SELECT id, link_id, price, privilege_type, recorded_at FROM price_history "
                "WHERE link_id = ? ORDER BY recorded_at DESC LIMIT ?",
                (link_id, limit),
            )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_user_products(user_id: int) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT p.id, p.name, p.created_at, p.threshold_price, p.alert_active, "
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
            "SELECT id, name, created_by, threshold_price, alert_active, triggered_at, created_at "
            "FROM products WHERE id = ?",
            (product_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


# ===== Search links =====


async def add_search_link(
    product_id: int, marketplace: str, search_url: str, title_filter: str,
) -> int:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO search_links (product_id, marketplace, search_url, title_filter) "
            "VALUES (?, ?, ?, ?)",
            (product_id, marketplace, search_url, title_filter),
        )
        await conn.commit()
        logger.info(
            "Added search link %s (filter=%s) for product_id=%d",
            search_url, title_filter, product_id,
        )
        return cursor.lastrowid


async def get_active_search_links() -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, product_id, marketplace, search_url, title_filter, "
            "last_price, last_resolved_url, last_resolved_title, last_checked_at "
            "FROM search_links WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def save_search_price(
    search_link_id: int, price: float, resolved_url: str, resolved_title: str,
):
    async with db_connection() as conn:
        await conn.execute(
            "INSERT INTO search_price_history "
            "(search_link_id, price, resolved_url, resolved_title) VALUES (?, ?, ?, ?)",
            (search_link_id, price, resolved_url, resolved_title),
        )
        await conn.execute(
            "UPDATE search_links SET last_price = ?, last_resolved_url = ?, "
            "last_resolved_title = ?, last_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
            (price, resolved_url, resolved_title, search_link_id),
        )
        await conn.commit()
        logger.info(
            "Saved search price %.2f for search_link_id=%d (%s)",
            price, search_link_id, resolved_title,
        )


async def get_search_link_by_id(search_link_id: int) -> dict | None:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, product_id, marketplace, search_url, title_filter, "
            "last_price, last_resolved_url, last_resolved_title, last_checked_at, is_active "
            "FROM search_links WHERE id = ?",
            (search_link_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def get_user_search_links(user_id: int) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT p.id AS product_id, p.name, p.created_at, "
            "p.threshold_price, p.alert_active, "
            "sl.id AS search_link_id, sl.marketplace, sl.search_url, "
            "sl.title_filter, sl.last_price, sl.last_resolved_url, "
            "sl.last_resolved_title, sl.last_checked_at "
            "FROM products p "
            "JOIN search_links sl ON sl.product_id = p.id "
            "WHERE p.created_by = ? AND sl.is_active = 1 "
            "ORDER BY p.created_at DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_search_link(search_link_id: int, user_id: int):
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE search_links SET is_active = 0 "
            "WHERE id = ? AND product_id IN (SELECT id FROM products WHERE created_by = ?)",
            (search_link_id, user_id),
        )
        await conn.commit()
        logger.info("Deactivated search_link_id=%d for user_id=%d", search_link_id, user_id)


async def get_search_price_history(search_link_id: int, limit: int = 15) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, search_link_id, price, resolved_url, resolved_title, recorded_at "
            "FROM search_price_history WHERE search_link_id = ? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (search_link_id, limit),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ===== Privileges =====


async def add_user_privilege(user_id: int, marketplace: str, privilege_type: str):
    async with db_connection() as conn:
        try:
            await conn.execute(
                "INSERT INTO user_privileges (user_id, marketplace, privilege_type) VALUES (?, ?, ?)",
                (user_id, marketplace, privilege_type),
            )
            await conn.commit()
            logger.info("Added privilege %s/%s for user_id=%d", marketplace, privilege_type, user_id)
        except Exception:
            logger.warning("Privilege %s/%s already exists for user_id=%d", marketplace, privilege_type, user_id)


async def remove_user_privilege(user_id: int, marketplace: str, privilege_type: str):
    async with db_connection() as conn:
        await conn.execute(
            "DELETE FROM user_privileges WHERE user_id = ? AND marketplace = ? AND privilege_type = ?",
            (user_id, marketplace, privilege_type),
        )
        await conn.commit()
        logger.info("Removed privilege %s/%s for user_id=%d", marketplace, privilege_type, user_id)


async def get_user_privileges(user_id: int) -> list[dict]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, user_id, marketplace, privilege_type FROM user_privileges WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_user_privileges_for_marketplace(user_id: int, marketplace: str) -> list[str]:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT privilege_type FROM user_privileges WHERE user_id = ? AND marketplace = ?",
            (user_id, marketplace),
        )
        rows = await cursor.fetchall()
    return [r["privilege_type"] for r in rows]


# ===== Product thresholds =====


async def get_user_products_with_threshold(user_id: int) -> list[dict]:
    """List user's products that have alert_active=1, with threshold for the /threshold menu."""
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT p.id, p.name, p.threshold_price, p.alert_active "
            "FROM products p "
            "WHERE p.created_by = ? AND p.alert_active = 1 "
            "ORDER BY p.name",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_all_product_thresholds() -> list[dict]:
    """Return all products with an active alert — used by the search poller
    to check the product-level threshold against search results.
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT p.id, p.name, p.threshold_price, p.triggered_at, p.created_by AS user_id "
            "FROM products p "
            "WHERE p.alert_active = 1 AND p.threshold_price > 0"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_search_link_with_product(search_link_id: int) -> dict | None:
    """Return search link joined with its product (threshold + owner)."""
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT sl.id, sl.product_id, sl.marketplace, sl.search_url, sl.title_filter, "
            "sl.last_price, sl.last_resolved_url, sl.last_resolved_title, "
            "sl.last_checked_at, sl.is_active, "
            "p.name AS product_name, p.threshold_price, p.alert_active, p.triggered_at, "
            "p.created_by AS user_id "
            "FROM search_links sl "
            "JOIN products p ON p.id = sl.product_id "
            "WHERE sl.id = ?",
            (search_link_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def get_active_search_links_with_product() -> list[dict]:
    """Same as get_active_search_links but with product threshold joined in."""
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT sl.id, sl.product_id, sl.marketplace, sl.search_url, sl.title_filter, "
            "sl.last_price, sl.last_resolved_url, sl.last_resolved_title, sl.last_checked_at, "
            "p.name AS product_name, p.threshold_price, p.alert_active, p.triggered_at, "
            "p.created_by AS user_id "
            "FROM search_links sl "
            "JOIN products p ON p.id = sl.product_id "
            "WHERE sl.is_active = 1"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]
