import aiosqlite
import os
import re
from contextlib import asynccontextmanager

from utils.logger import logger

DB_PATH = os.getenv("DB_PATH", "./data/prices.db")

_LOT_ID_RE = re.compile(r"_(\d+)$")


def lot_id_from_url(url: str) -> int | None:
    """Числовой номер лота Avito из хвоста URL (`.../kniga_8414522966`)."""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = _LOT_ID_RE.search(path)
    return int(match.group(1)) if match else None

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
    track_mode TEXT NOT NULL DEFAULT 'price',
    deep_scan_done INTEGER NOT NULL DEFAULT 0,
    deep_scan_page INTEGER NOT NULL DEFAULT 1,
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

CREATE TABLE IF NOT EXISTS avito_search_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_link_id INTEGER REFERENCES search_links(id),
    item_url TEXT NOT NULL,
    lot_id INTEGER,
    item_title TEXT,
    price REAL,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP,
    notified_at TIMESTAMP,
    miss_streak INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS avito_lot_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_link_id INTEGER REFERENCES search_links(id),
    item_url TEXT NOT NULL UNIQUE,
    lot_id INTEGER,
    status TEXT NOT NULL,
    skip_reason TEXT,
    confidence TEXT,
    result_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_avito_lot_checks_url
    ON avito_lot_checks(item_url);
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
    "ALTER TABLE search_links ADD COLUMN track_mode TEXT NOT NULL DEFAULT 'price'",
]

_LOT_ID_CHUNK = 500


async def _backfill_lot_ids(conn, table: str) -> None:
    """Проставить lot_id по item_url: в SQLite нет regex, читаем чанками."""
    updated = 0
    no_id = 0
    last_id = 0
    while True:
        cursor = await conn.execute(
            f"SELECT id, item_url FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, _LOT_ID_CHUNK),
        )
        rows = await cursor.fetchall()
        if not rows:
            break
        updates = []
        for row in rows:
            lot_id = lot_id_from_url(row["item_url"])
            if lot_id is None:
                no_id += 1
            else:
                updates.append((lot_id, row["id"]))
        if updates:
            await conn.executemany(
                f"UPDATE {table} SET lot_id = ? WHERE id = ?", updates)
        updated += len(updates)
        last_id = rows[-1]["id"]
    logger.info(
        "%s: lot_id проставлен у %d строк, URL без номера лота — %d",
        table, updated, no_id,
    )


async def _deactivate_duplicate_lots(conn) -> None:
    """Мягко скрыть дубли лотов: в группе живёт строка с наименьшим id.

    Дубли появляются, когда продавец перевыкладывает лот — Avito меняет slug,
    а номер лота остаётся тем же. Физически строки не удаляем.
    """
    cursor = await conn.execute(
        "UPDATE avito_search_items SET is_active = 0 "
        "WHERE is_active = 1 AND lot_id IS NOT NULL AND id NOT IN ("
        "    SELECT MIN(id) FROM avito_search_items "
        "    WHERE lot_id IS NOT NULL GROUP BY search_link_id, lot_id)"
    )
    logger.info(
        "avito_search_items: мягко скрыто дублей по номеру лота — %d",
        cursor.rowcount,
    )


async def init_db():
    async with db_connection() as conn:
        await conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            try:
                await conn.execute(migration)
                logger.info("Migration applied: %s", migration[:60])
            except Exception:
                pass

        # Одноразовая миграция: учёт уведомлений по объявлениям Avito.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM pragma_table_info('avito_search_items') "
            "WHERE name = 'notified_at'"
        )
        if (await cursor.fetchone())["n"] == 0:
            await conn.execute(
                "ALTER TABLE avito_search_items ADD COLUMN last_seen_at TIMESTAMP")
            await conn.execute(
                "ALTER TABLE avito_search_items ADD COLUMN notified_at TIMESTAMP")
            await conn.execute(
                "ALTER TABLE avito_search_items ADD COLUMN miss_streak "
                "INTEGER NOT NULL DEFAULT 0")
            # Ранее отслеженные объявления считаем уже уведомлёнными, иначе
            # после деплоя придёт пачка уведомлений обо всей текущей выдаче.
            await conn.execute(
                "UPDATE avito_search_items "
                "SET notified_at = COALESCE(first_seen_at, CURRENT_TIMESTAMP)")
            await conn.execute(
                "UPDATE avito_search_items "
                "SET last_seen_at = COALESCE(first_seen_at, CURRENT_TIMESTAMP)")
            logger.info("Migration applied: avito_search_items tracking columns")

        # Одноразовая миграция: числовой номер лота Avito. Нужен, чтобы один
        # и тот же лот не расползался на несколько строк при смене slug.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM pragma_table_info('avito_search_items') "
            "WHERE name = 'lot_id'"
        )
        if (await cursor.fetchone())["n"] == 0:
            await conn.execute(
                "ALTER TABLE avito_search_items ADD COLUMN lot_id INTEGER")
            await conn.execute(
                "ALTER TABLE avito_lot_checks ADD COLUMN lot_id INTEGER")
            await _backfill_lot_ids(conn, "avito_search_items")
            await _backfill_lot_ids(conn, "avito_lot_checks")
            await _deactivate_duplicate_lots(conn)
            logger.info("Migration applied: avito lot_id columns")

        # Индексы создаём всегда: колонка уже есть и у свежей базы, и после
        # одноразового ALTER выше.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_avito_search_items_lot_id "
            "ON avito_search_items(lot_id)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_avito_lot_checks_lot_id "
            "ON avito_lot_checks(lot_id)")

        # Одноразовая миграция: признак «глубокая проверка выдачи уже была».
        # Первая глубокая проверка ссылки — базлайн: лоты запоминаем, но не
        # уведомляем, иначе после деплоя придёт пачка старых объявлений.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM pragma_table_info('search_links') "
            "WHERE name = 'deep_scan_done'"
        )
        if (await cursor.fetchone())["n"] == 0:
            await conn.execute(
                "ALTER TABLE search_links ADD COLUMN deep_scan_done "
                "INTEGER NOT NULL DEFAULT 0")
            logger.info("Migration applied: search_links.deep_scan_done")

        # Одноразовая миграция: курсор глубины обхода выдачи. Показывает,
        # какая страница уже просмотрена при первоначальном глубоком
        # покрытии, чтобы обход не начинался каждый раз с первой страницы.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM pragma_table_info('search_links') "
            "WHERE name = 'deep_scan_page'"
        )
        if (await cursor.fetchone())["n"] == 0:
            await conn.execute(
                "ALTER TABLE search_links ADD COLUMN deep_scan_page "
                "INTEGER NOT NULL DEFAULT 1")
            logger.info("Migration applied: search_links.deep_scan_page")

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


async def get_marketplace_link_by_id(link_id: int) -> dict | None:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT ml.id, ml.product_id, ml.marketplace, ml.url, "
            "ml.last_price, ml.last_checked_at, ml.is_active, "
            "p.name AS product_name "
            "FROM marketplace_links ml "
            "JOIN products p ON p.id = ml.product_id "
            "WHERE ml.id = ?",
            (link_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


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
        if privilege_type == "standard":
            await conn.execute(
                "UPDATE marketplace_links SET last_price = ?, last_checked_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (price, link_id),
            )
        else:
            await conn.execute(
                "UPDATE marketplace_links SET last_checked_at = CURRENT_TIMESTAMP WHERE id = ?",
                (link_id,),
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
    track_mode: str = "price",
) -> int:
    async with db_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO search_links "
            "(product_id, marketplace, search_url, title_filter, track_mode) "
            "VALUES (?, ?, ?, ?, ?)",
            (product_id, marketplace, search_url, title_filter, track_mode),
        )
        await conn.commit()
        logger.info(
            "Added search link %s (filter=%s, mode=%s) for product_id=%d",
            search_url, title_filter, track_mode, product_id,
        )
        return cursor.lastrowid


async def get_active_search_links() -> list[dict]:
    """Активные ссылки для отслеживания цены (без книжного режима Avito)."""
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, product_id, marketplace, search_url, title_filter, "
            "track_mode, last_price, last_resolved_url, last_resolved_title, "
            "last_checked_at "
            "FROM search_links WHERE is_active = 1 "
            "AND COALESCE(track_mode, 'price') != 'books'"
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
            "sl.title_filter, sl.track_mode, sl.last_price, sl.last_resolved_url, "
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
            "sl.track_mode, sl.last_price, sl.last_resolved_url, sl.last_resolved_title, "
            "sl.last_checked_at, "
            "p.name AS product_name, p.threshold_price, p.alert_active, p.triggered_at, "
            "p.created_by AS user_id "
            "FROM search_links sl "
            "JOIN products p ON p.id = sl.product_id "
            "WHERE sl.is_active = 1 "
            "AND COALESCE(sl.track_mode, 'price') != 'books'"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ===== Avito search (new-item detection) =====


async def get_active_avito_search_links() -> list[dict]:
    """Return active Avito search links joined with the owning product/user."""
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT sl.id, sl.product_id, sl.marketplace, sl.search_url, sl.title_filter, "
            "sl.track_mode, sl.deep_scan_done, sl.deep_scan_page, "
            "sl.last_checked_at, "
            "p.name AS product_name, p.created_by AS user_id "
            "FROM search_links sl "
            "JOIN products p ON p.id = sl.product_id "
            "WHERE sl.is_active = 1 AND sl.marketplace = 'avito'"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def mark_avito_deep_scan_done(search_link_id: int) -> None:
    """Отметить, что глубокая проверка выдачи (базлайн) уже выполнена."""
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE search_links SET deep_scan_done = 1 WHERE id = ?",
            (search_link_id,),
        )
        await conn.commit()


async def set_avito_deep_scan_page(search_link_id: int, page: int) -> None:
    """Сохранить курсор глубины обхода выдачи (номер последней страницы)."""
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE search_links SET deep_scan_page = ? WHERE id = ?",
            (int(page), search_link_id),
        )
        await conn.commit()


async def get_avito_item_state(search_link_id: int) -> dict[int | str, dict]:
    """Состояние отслеживаемых объявлений по устойчивому ключу.

    Ключ — числовой ID лота, а для URL без ID — сам URL. Так перевыложенное
    объявление со сменой slug продолжает ссылаться на прежнее состояние.
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, item_url, lot_id, is_active, notified_at, miss_streak, price "
            "FROM avito_search_items WHERE search_link_id = ? ORDER BY id",
            (search_link_id,),
        )
        rows = await cursor.fetchall()
    state: dict[int | str, dict] = {}
    for row in rows:
        key = row["lot_id"] if row["lot_id"] is not None else row["item_url"]
        # При старых дублях канонична строка с минимальным id, как и в sync.
        state.setdefault(key, {
            "id": row["id"],
            "item_url": row["item_url"],
            "lot_id": row["lot_id"],
            "is_active": row["is_active"],
            "notified_at": row["notified_at"],
            "miss_streak": row["miss_streak"],
            "price": row["price"],
        })
    return state


async def sync_avito_items(search_link_id: int, items: list) -> dict[str, dict]:
    """Апсерт текущей выдачи поиска Avito.

    Новые объявления вставляются с notified_at = NULL. Существующие
    обновляются: item_title, price, last_seen_at = CURRENT_TIMESTAMP,
    is_active = 1, miss_streak = 0. Если строка была неактивна (объявление
    вернулось в выдачу), notified_at сбрасывается в NULL, чтобы оно снова
    попало в уведомления — но поллер должен проверить, что цена упала.
    Строка ищется по номеру лота (тогда при смене slug обновляется item_url,
    id строки сохраняется), иначе по полному URL.
    Возвращает url -> {id, notified_at} уже после апсерта.
    """
    async with db_connection() as conn:
        for it in items:
            lot_id = lot_id_from_url(it.url)
            if lot_id is not None:
                # В группе лота бывают дубли от старых версий — живёт строка
                # с наименьшим id, остальные уже мягко скрыты миграцией.
                where = (
                    "search_link_id = ? AND id = ("
                    "SELECT MIN(id) FROM avito_search_items "
                    "WHERE search_link_id = ? AND lot_id = ?)")
                where_params = (search_link_id, search_link_id, lot_id)
            else:
                where = "search_link_id = ? AND item_url = ?"
                where_params = (search_link_id, it.url)
            cursor = await conn.execute(
                "UPDATE avito_search_items "
                "SET item_url = ?, item_title = ?, price = ?, lot_id = ?, "
                "last_seen_at = CURRENT_TIMESTAMP, is_active = 1, "
                "miss_streak = 0, "
                # В SQLite правая часть CASE вычисляется по старым значениям
                # строки, поэтому сброс срабатывает ровно для неактивных строк.
                "notified_at = CASE WHEN is_active = 0 THEN NULL ELSE notified_at END "
                f"WHERE {where}",
                (it.url, it.title, it.price, lot_id, *where_params),
            )
            if cursor.rowcount == 0:
                await conn.execute(
                    "INSERT INTO avito_search_items "
                    "(search_link_id, item_url, lot_id, item_title, price, "
                    "last_seen_at, miss_streak, is_active) "
                    "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0, 1)",
                    (search_link_id, it.url, lot_id, it.title, it.price),
                )
                logger.info(
                    "New Avito item tracked: %s (price=%s) for search_link_id=%d",
                    it.title, it.price, search_link_id,
                )

        result: dict[str, dict] = {}
        for it in items:
            cursor = await conn.execute(
                "SELECT id, item_url, notified_at FROM avito_search_items "
                "WHERE search_link_id = ? AND item_url = ?",
                (search_link_id, it.url),
            )
            row = await cursor.fetchone()
            if row:
                result[row["item_url"]] = {
                    "id": row["id"],
                    "notified_at": row["notified_at"],
                }
        await conn.commit()
    return result


async def mark_missing_avito_items(
    search_link_id: int, current_urls: set[str], threshold: int = 2,
) -> int:
    """Отметить пропажу объявлений из выдачи.

    Для активных строк, которых нет в current_urls, miss_streak += 1;
    при miss_streak >= threshold строка деактивируется (is_active = 0).
    Возвращает количество деактивированных строк.
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT item_url FROM avito_search_items "
            "WHERE search_link_id = ? AND is_active = 1",
            (search_link_id,),
        )
        rows = await cursor.fetchall()
        missing = [r["item_url"] for r in rows if r["item_url"] not in current_urls]
        if not missing:
            return 0
        placeholders = ", ".join("?" for _ in missing)
        await conn.execute(
            f"UPDATE avito_search_items SET miss_streak = miss_streak + 1 "
            f"WHERE search_link_id = ? AND item_url IN ({placeholders})",
            (search_link_id, *missing),
        )
        cursor = await conn.execute(
            "UPDATE avito_search_items SET is_active = 0 "
            "WHERE search_link_id = ? AND is_active = 1 AND miss_streak >= ?",
            (search_link_id, threshold),
        )
        deactivated = cursor.rowcount
        await conn.commit()
    return deactivated


async def mark_avito_items_notified(item_ids: list[int]) -> None:
    """Пометить объявления уведомлёнными (notified_at = CURRENT_TIMESTAMP)."""
    if not item_ids:
        return
    placeholders = ", ".join("?" for _ in item_ids)
    async with db_connection() as conn:
        await conn.execute(
            f"UPDATE avito_search_items SET notified_at = CURRENT_TIMESTAMP "
            f"WHERE id IN ({placeholders})",
            tuple(item_ids),
        )
        await conn.commit()


# ===== Avito books (lot-level checks) =====


async def _avito_lot_check_id(
    conn, item_url: str, lot_id: int | None,
) -> int | None:
    """id строки проверки лота: сначала по номеру лота, иначе по URL.

    Дубли по номеру лота (перевыкладывание со сменой slug) не чистим —
    берём самую свежую проверку.
    """
    if lot_id is not None:
        cursor = await conn.execute(
            "SELECT id FROM avito_lot_checks WHERE lot_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (lot_id,),
        )
        row = await cursor.fetchone()
        if row:
            return row["id"]
    cursor = await conn.execute(
        "SELECT id FROM avito_lot_checks WHERE item_url = ?", (item_url,))
    row = await cursor.fetchone()
    return row["id"] if row else None


async def get_avito_lot_check(
    item_url: str | None = None, lot_id: int | None = None,
) -> dict | None:
    """Результат проверки лота (глобально, без привязки к поиску).

    Ищем по номеру лота, если он задан, иначе по полному URL.
    """
    select = (
        "SELECT item_url, search_link_id, status, skip_reason, confidence, "
        "result_json, attempts, last_error, checked_at FROM avito_lot_checks ")
    async with db_connection() as conn:
        if lot_id is not None:
            cursor = await conn.execute(
                select + "WHERE lot_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (lot_id,),
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
        if item_url is None:
            return None
        cursor = await conn.execute(select + "WHERE item_url = ?", (item_url,))
        row = await cursor.fetchone()
    return dict(row) if row else None


async def save_avito_lot_check(
    search_link_id: int,
    item_url: str,
    status: str,
    skip_reason: str | None = None,
    confidence: str | None = None,
    result_json: str | None = None,
    lot_id: int | None = None,
) -> None:
    """Сохранить итог проверки лота: matched / skipped."""
    if lot_id is None:
        lot_id = lot_id_from_url(item_url)
    async with db_connection() as conn:
        row_id = await _avito_lot_check_id(conn, item_url, lot_id)
        if row_id is not None:
            # Тот же лот с другим slug — правим существующую строку.
            await conn.execute(
                "UPDATE avito_lot_checks SET item_url = ?, lot_id = ?, "
                "search_link_id = ?, status = ?, skip_reason = ?, "
                "confidence = ?, result_json = ?, last_error = NULL, "
                "checked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (item_url, lot_id, search_link_id, status, skip_reason,
                 confidence, result_json, row_id),
            )
        else:
            await conn.execute(
                "INSERT INTO avito_lot_checks "
                "(search_link_id, item_url, lot_id, status, skip_reason, "
                "confidence, result_json, attempts, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL) "
                "ON CONFLICT(item_url) DO UPDATE SET "
                "search_link_id = excluded.search_link_id, "
                "lot_id = excluded.lot_id, "
                "status = excluded.status, "
                "skip_reason = excluded.skip_reason, "
                "confidence = excluded.confidence, "
                "result_json = excluded.result_json, "
                "last_error = NULL, "
                "checked_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP",
                (search_link_id, item_url, lot_id, status, skip_reason,
                 confidence, result_json),
            )
        await conn.commit()
    logger.info(
        "Avito lot check saved: %s status=%s reason=%s (search_link_id=%d)",
        item_url, status, skip_reason, search_link_id,
    )


async def record_avito_lot_error(
    search_link_id: int, item_url: str, error: str,
    lot_id: int | None = None,
) -> int:
    """Учесть ошибку разбора лота; возвращает число попыток."""
    if lot_id is None:
        lot_id = lot_id_from_url(item_url)
    async with db_connection() as conn:
        row_id = await _avito_lot_check_id(conn, item_url, lot_id)
        if row_id is not None:
            await conn.execute(
                "UPDATE avito_lot_checks SET item_url = ?, lot_id = ?, "
                "attempts = attempts + 1, last_error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (item_url, lot_id, error[:500], row_id),
            )
            cursor = await conn.execute(
                "SELECT attempts FROM avito_lot_checks WHERE id = ?", (row_id,))
        else:
            await conn.execute(
                "INSERT INTO avito_lot_checks "
                "(search_link_id, item_url, lot_id, status, attempts, last_error) "
                "VALUES (?, ?, ?, 'error', 1, ?) "
                "ON CONFLICT(item_url) DO UPDATE SET "
                "attempts = avito_lot_checks.attempts + 1, "
                "last_error = excluded.last_error, "
                "updated_at = CURRENT_TIMESTAMP",
                (search_link_id, item_url, lot_id, error[:500]),
            )
            cursor = await conn.execute(
                "SELECT attempts FROM avito_lot_checks WHERE item_url = ?",
                (item_url,),
            )
        row = await cursor.fetchone()
        await conn.commit()
    return row["attempts"] if row else 1


async def get_pending_avito_llm_checks(limit: int = 5) -> list[dict]:
    """Очередь спорных лотов, ждущих LLM-вердикта.

    Возвращает строки avito_lot_checks со статусом llm_pending вместе с
    пользователем (products.created_by) и ссылкой поиска.
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT c.id, c.search_link_id, c.item_url, c.lot_id, "
            "c.result_json, c.attempts, sl.search_url, "
            "p.created_by AS user_id "
            "FROM avito_lot_checks c "
            "JOIN search_links sl ON sl.id = c.search_link_id "
            "JOIN products p ON p.id = sl.product_id "
            "WHERE c.status = 'llm_pending' "
            "ORDER BY c.updated_at ASC, c.id ASC LIMIT ?",
            (int(limit),),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def update_avito_lot_check(
    check_id: int,
    status: str,
    skip_reason: str | None = None,
    confidence: str | None = None,
    result_json: str | None = None,
    last_error: str | None = None,
    bump_attempts: bool = False,
) -> None:
    """Обновить строку проверки лота по её id (очередь LLM)."""
    async with db_connection() as conn:
        await conn.execute(
            "UPDATE avito_lot_checks SET status = ?, skip_reason = ?, "
            "confidence = ?, result_json = ?, last_error = ?, "
            "attempts = attempts + ?, "
            "checked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (
                status,
                skip_reason,
                confidence,
                result_json,
                last_error[:500] if last_error else None,
                1 if bump_attempts else 0,
                int(check_id),
            ),
        )
        await conn.commit()


async def mark_avito_item_notified(
    search_link_id: int, item_url: str, lot_id: int | None = None,
) -> bool:
    """Пометить конкретное объявление как уведомлённое.

    Ищем по номеру лота (перевыложенный лот мог сменить slug) либо по URL.
    """
    if lot_id is None:
        lot_id = lot_id_from_url(item_url)
    async with db_connection() as conn:
        cursor = await conn.execute(
            "UPDATE avito_search_items SET notified_at = CURRENT_TIMESTAMP "
            "WHERE search_link_id = ? AND (lot_id = ? OR item_url = ?)",
            (int(search_link_id), lot_id, item_url),
        )
        await conn.commit()
        return cursor.rowcount > 0

