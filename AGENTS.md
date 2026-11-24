# Price Tracker

A Telegram bot for tracking product prices on Russian marketplaces. The user adds a specific product URL and a threshold price. The bot periodically parses the pages and sends a notification when the price drops below the threshold.

## Stack

- Python (version pinned in `.tool-versions` via asdf)
- `nodriver`: browser automation on top of real Chrome (anti-bot bypass)
- `aiosqlite`: async access to SQLite
- `apscheduler` (AsyncIOScheduler): price polling scheduler
- `python-telegram-bot` v20+: Telegram bot (async API)
- `python-dotenv`: environment variables from `.env`
- `fabric`: deployment to production via `fab deploy` (see `fabfile.py`)

## Project structure

```
price-tracker/
├── .tool-versions          # asdf: pinned Python version
├── .env                    # secrets (not in git)
├── .env.example            # template
├── requirements.txt
├── fabfile.py               # Fabric tasks (deploy, services)
├── .deploy.env              # deploy config (not in git)
├── .deploy.env.example
├── main.py                 # entry point: bot + scheduler
├── db/                     # database.py, models.py
├── parsers/                # base.py, wildberries.py, ozon.py, citilink.py, avito.py
├── books/                  # normalize.py, catalog.py, matcher.py, filters.py, prefilter.py, llm_verifier.py
├── data/                   # books_catalog.json
├── tests/
├── scheduler/              # jobs.py
├── bot/                    # handlers.py, notifications.py, bot_instance.py, admin_notify.py
├── utils/                  # logger.py, display.py
├── scripts/                # deploy.sh, test_*.py, build_books_catalog.py
└── docs/                   # PROJECT.md, INSTALL.md, USAGE.md, books/

Details for each module are in the code and in docs/. LSP (`basedpyright`), the `codebase` skill, and `grep`/`read` with `offset`/`limit` will help you find your way around without loading whole files into context.
```

## Commands

### Local development (frequent)

```bash
# Installation
asdf install                                  # requires asdf >= 0.16
# bash/zsh:
asdf exec python -m venv venv && source venv/bin/activate
# fish: see docs/INSTALL.md
pip install -r requirements.txt

# Run
python main.py

# Manual parser test
python scripts/test_parsers.py "https://www.wildberries.ru/catalog/12345678/detail.aspx"
```

Deployment, rollback, systemd service management, `fab sync-profile` / `fab sync-books-profile`, and other server operations are rare. They live in [README.md "Deploying to the server"](README.md#деплой-на-сервер). Do not keep them in context; open them when needed.

> **Shell compatibility:** the venv activation instructions are given for bash (`source venv/bin/activate`). For fish, use `activate.fish`. For that, the venv is created with `virtualenv` (not `python -m venv`): `asdf exec python -m pip install --user virtualenv && asdf exec python -m virtualenv venv && source venv/bin/activate.fish`. See `docs/INSTALL.md`.
>
> **Windows-checkout warning:** if the repository is cloned on Linux with `git config --global core.autocrlf=true`, the asdf python plugin breaks (`'bash\r': No such file or directory`). Before `asdf install`, make sure `git config --get core.autocrlf` returns `false` or `input` for this clone.

### First-time Avito setup

Avito requires a trusted session (captcha bypass). On a headless server there is no browser in which to solve the captcha, so the trusted session is prepared once on a dev machine and uploaded to the server:

```bash
# 1. On the dev machine (with a GUI), prepare the price-mode profile
#    (`chrome_profile_avito/`) and solve the captcha once.
#    Fastest way: `python scripts/test_avito_search.py <url>` and solve the captcha
#    when the bot opens the Chrome window.

# 2. Prepare the separate books-mode profile (`chrome_profile_avito_books/`) by
#    opening Avito in Chrome with that user-data-dir and solving the captcha:
#    google-chrome --user-data-dir="$PWD/chrome_profile_avito_books" https://www.avito.ru/

# 3. Upload both trusted profiles to the server (each task stops the bot,
#    backs up the old profile, uploads the new one, and restarts the bot):
fab sync-profile
fab sync-books-profile

# 4. One-time cleanup after the update: kill Chrome processes left by the old
#    version (they were started with a relative profile path and keep the
#    profile lock). Cookies and the trusted session are not touched:
fab cleanup-avito-chrome
```

The `chrome_profile_avito/` (price mode) and `chrome_profile_avito_books/` (book mode) profiles are gitignored and keep separate trusted Avito sessions between deployments; `fab deploy` does not touch either one. They also have separate Chrome processes and `SingletonLock` files, so `/add` and `/books` can run in parallel. The lock files (`SingletonLock`/`SingletonCookie`/`SingletonSocket`) can become stale if a profile was synced from a dev machine with a different hostname. In that case `_check_and_reset_chrome_profile` removes only the locks (cookies/session are kept). The sync tasks resolve the target directories from `.deploy.env` → the server `.env` → the defaults, with relative paths counted from the service `WorkingDirectory` (`systemctl show price-tracker.service --property=WorkingDirectory`), so the uploaded profile lands exactly where the bot looks for it. Repeat the corresponding sync task only if that profile's captcha reappears.

> **Shell compatibility:** the venv activation instructions are given for bash
> (`source venv/bin/activate`). For fish, use `activate.fish`.
> For that, the venv is created with `virtualenv` (not `python -m venv`):
> `asdf exec python -m pip install --user virtualenv &&
>  asdf exec python -m virtualenv venv &&
>  source venv/bin/activate.fish`. See `docs/INSTALL.md`.
>
> **Windows-checkout warning:** if the repository is cloned on Linux with
> `git config --global core.autocrlf=true`, the asdf python plugin breaks
> (`'bash\r': No such file or directory`). Before `asdf install`, make sure
> `git config --get core.autocrlf` returns `false` or `input`
> for this clone.

## Bot commands

| Command | Description |
|---|---|
| `/start` | Greeting and list of commands |
| `/help` | Help |
| `/add` | Add a new product: name + link + price threshold |
| `/link` | Attach a link to an existing product (to track it on several marketplaces) |
| `/threshold` | Change/disable the notification price threshold (0 = disable) |
| `/privileges` | Configure marketplace privileges (card discount, subscription) |
| `/books` | Add an Avito search for books: new lots are checked against a reference list of 74 books |
| `/list` | List your products with current prices |
| `/delete` | Delete a product (with confirmation) |
| `/history` | Price history of a product (with a filter by price type) |

## Environment variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DB_PATH` | Path to the SQLite file, e.g. `./data/prices.db` |
| `POLL_INTERVAL_MINUTES` | Price polling interval (default: 120) |
| `AVITO_POLL_INTERVAL_MINUTES` | Avito price-mode (`/add`) search page polling interval (default: 5) |
| `AVITO_BOOKS_POLL_INTERVAL_MINUTES` | Avito book-mode (`/books`) polling interval, minutes (default: 5) |
| `AVITO_PROFILE_DIR` | Path to the Chrome profile for Avito price mode (`/add`) (default: `./chrome_profile_avito`) |
| `AVITO_BOOKS_PROFILE_DIR` | Path to the separate Chrome profile for Avito book mode (`/books`) (default: `./chrome_profile_avito_books`) |
| `AVITO_BLOCK_RETRY_MINUTES` | Pause before the next check after the "Access restricted" stub, in minutes (default: 5) |
| `AVITO_BLOCK_MAX_ATTEMPTS` | How many consecutive blocks to tolerate before notifying the admin (default: 3) |
| `AVITO_BOOKS_MAX_CHECKS` | How many new-lot cards to open per book-mode cycle, shared per domain (default: 8) |
| `AVITO_BOOKS_PAGES_PER_CYCLE` | How many listing pages to fetch per book-mode cycle, shared per domain (default: 4) |
| `AVITO_BOOKS_MAX_PAGES` | Maximum page depth of one link scan (default: 10) |
| `AVITO_BOOKS_PAGE_DELAY_MIN` / `AVITO_BOOKS_PAGE_DELAY_MAX` | Pause between pages of one link, seconds (default: 5 / 7) |
| `AVITO_BOOKS_LLM_ENABLED` | `1`: verify ambiguous lots through the local LLM (default: 1) |
| `AVITO_BOOKS_LLM_INTERVAL_MINUTES` | Interval of the LLM verification queue job, minutes (default: 10) |
| `AVITO_BOOKS_LLM_BATCH` | How many queued lots to check per LLM job run (default: 3) |
| `AVITO_BOOKS_DRY_RUN` | `1`: only log book matches, no notifications |
| `PROXY_URL` | SOCKS5 proxy for the Telegram bot (optional, format: `socks5://login:pass@host:port`) |
| `ADMIN_TELEGRAM_ID` | Telegram ID of the administrator for service notifications (optional) |

Load via `python-dotenv` only in `main.py`, then pass values explicitly.

## Coding rules

### Language of communication
- Communicate with the user **only in Russian** (all replies, code comments, messages in the Telegram bot)
- Internal reasoning may be in any language

### Async everywhere
- All DB functions are `async def` via `aiosqlite`
- All bot handlers are `async def`
- Scheduler tasks are `async def`: `poll_prices()`, `poll_search_prices()`, `poll_avito_search()`, `poll_avito_books()`, `poll_book_llm_queue()`
- No `asyncio.run()` inside modules, only in `main.py`

### Error handling
- **Never** let a parser error crash the scheduler cycle
- Wrap every parser call in `try/except`, log, and move on
- If the page contains `captcha`, `return None` with a WARNING log; do not retry
- If the product is out of stock, `return None`; this is not an error

### Logging
```python
from utils.logger import logger

logger.debug("Parsed price: %s from %s", price, url)
logger.info("Saved price %.2f for link_id=%d", price, link_id)
logger.warning("Captcha detected at %s", url)
logger.error("Parser failed for %s: %s", url, exc)
```
Never use `print()` in modules.

### Database
- All SQL functions live in `db/database.py`, models in `db/models.py`
- Soft deletes: `is_active = 0`, never `DELETE`
- Always specify column names in `INSERT`
- Pass the connection as an explicit parameter or via a context manager

### Parsers
- One marketplace = one file in `parsers/`
- Inherit from `BaseParser`
- Must implement:
  - `can_handle(url: str) -> bool` (classmethod, checks by domain)
  - `get_price(url: str) -> float | None` (async)
  - `get_price_tiers(url: str) -> dict[str, float] | None` (async): returns all price variants (standard, card, premium, wb_club). By default a wrapper over `get_price()`.
  - `get_cheapest_from_search(search_url: str, title_filter: str) -> SearchResult | None` (async): finds the cheapest product on a search page, filtered by `title_filter`. Returns `SearchResult(price, product_url, product_title, tiers)`.
- Available price types: `standard`, `card`, `premium`, `wb_club`. The `TIER_LABELS` dictionary is in `parsers/base.py`.
- Register in `parsers/__init__.py` → the `PARSERS` list
- Add the marketplace's privilege types to the `MARKETPLACE_TIERS` dictionary in `parsers/__init__.py`
- Browser automation via `nodriver` (real Chrome, non-headless + Xvfb + fluxbox)
- **Stealth masking today = Chrome flags only.** `parsers/base.py::_inject_stealth()` is currently a **no-op** (`pass`): the JS injection it used to perform was switched off because the fake `navigator.plugins` override triggered the Wildberries WAF challenge without helping Ozon or Avito. The constant `STEALTH_JS` is kept for reference but is **not** injected, so no JS-level masking (`navigator.webdriver`, `chrome.runtime`, `cdc_*`, `navigator.plugins/languages/hardwareConcurrency/deviceMemory`) happens today. What actually masks automation now: `CHROME_ARGS` (`--disable-blink-features=AutomationControlled`, `--no-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`, `--no-zygote`, `--window-size=1920,1080`) plus `uc.Config._default_browser_args`, overwritten with only `--no-first-run` (nodriver's own noisy flags are dropped), and real Chrome runs non-headless under Xvfb. New parsers inherit this automatically from `BaseParser`. Do not describe JS-level masking as active; re-enable `_inject_stealth()` only once the patches are refined, and update this bullet in the same change.
- Ozon: navigate from the main page to the product (a direct URL is blocked by the anti-bot)
- Avito (`parsers/avito.py`): tracks not the product price but the **appearance of new listings** on a search page. `get_all_items_from_search()` returns a list of all current listings (url/title/price). The first poll of a link only records the current results, with no notifications. After that, new listings are identified by `notified_at = NULL` in `avito_search_items` and are sent to the user with the price and link. A notification is sent only if it was actually delivered: listings are marked with `notified_at` AFTER a successful send, so if Telegram fails, the notification is repeated in the next cycle. A listing that disappears from the results for `AVITO_MISS_THRESHOLD` (=2) successful polls in a row is marked `is_active = 0`. When it comes back, `notified_at` is reset, but a repeat notification arrives only if the price dropped below the previous one (otherwise the listing is silently marked as notified). If a single poll returns less than half as many listings as are being tracked, disappearance is not recorded (protection against a partially rendered page). Schema of `avito_search_items`: `item_url`, `item_title`, `price`, `first_seen_at`, `last_seen_at`, `notified_at`, `miss_streak`, `is_active`.
  - The "Access restricted" stub means the marketplace blocked the profile itself (cookies), not the IP. In that case `end_session()` recreates only that mode's profile: the old directory is moved to `<profile>.blocked.<timestamp>` (only the latest one is kept). The automatic reset happens no more often than `PROFILE_RESET_MIN_INTERVAL` (30 minutes). Price mode uses `chrome_profile_avito/`, book mode uses `chrome_profile_avito_books/`; each has its own `_AvitoBrowser`, Chrome process, and `SingletonLock`. `poll_prices()` and `poll_search_prices()` still skip Avito links, which are handled only by `poll_avito_search()` and `poll_avito_books()`.
  - Avito requires a trusted session in each mode's profile: captchas are solved manually once in `chrome_profile_avito/` and `chrome_profile_avito_books/`, after which both profiles must be preserved (see `_check_and_reset_chrome_profile`). The price parsers (WB/Ozon/Citilink) use a separate `chrome_profile/`.
  - Chrome reliability: `--no-sandbox` must be included in `CHROME_ARGS` (on some systems `no_sandbox=True` in nodriver does not add the flag). `_check_and_reset_chrome_profile` removes only stale SingletonLock/Cookie/Socket files (cookies/session are kept). Cleanup scans `/proc` and kills only Chrome processes whose argv contains a **complete** profile argument: `--user-data-dir=<path>` or `--user-data-dir <path>` (two-token form). The matched path may be the resolved absolute profile, the configured relative token (the old bot version launched Chrome with e.g. `chrome_profile_avito`), or the path relative to the project directory — `utils/chrome_cleanup.profile_marker_set()` builds that set; it never deletes profile files during a kill/sweep. Before a foreign/stale `SingletonLock` is deleted, a live Chrome still using that profile (in any path variant) is killed first — otherwise two Chromes could open the same profile. Special case: if a profile was synced from a dev machine (another host), `SingletonLock` will point to a foreign hostname and Chrome will refuse to start; `_check_and_reset_chrome_profile` recognizes this and repairs it without data loss. After the update, run `fab cleanup-avito-chrome` once to kill Chrome left over by the old version (it was started with a relative profile path). Repeat only the corresponding `fab sync-profile` or `fab sync-books-profile` task if that profile's captcha reappears.

### Avito book mode (`/books`)
- `search_links.track_mode = 'books'`; a regular `/add` creates `'price'`, and its behavior does not change.
- New lots are checked in `_poll_books_link`: **title filter** → publication date (Moscow time) → seller reviews → `books.matcher.match_text(title + description)`.
- The title filter `books/prefilter.py::lot_title_interesting(title)` decides whether to open the card at all (mass scraping of cards leads to an Avito ban). A lot is interesting if:
  - the title contains a book from the list (full `match_text` on the title, a specific title, an author from the list);
  - there is a series/publisher marker: "Литературные памятники" (Literary Monuments), "Academia"/"Академия" next to a book word;
  - there is an antique/collectible marker: "антикварные" (antique), "старинные" (old), "букинистические" (second-hand/rare), "дореволюционные" (pre-revolutionary), "винтажные" (vintage), "редкие книги" (rare books), "из домашней/личной библиотеки" (from a home/personal library), "коллекция/подборка/много книг" (collection/selection/many books), "одним лотом" (as one lot), "всё вместе" (all together);
  - a year of the target era (1922–1937 or 1948–1965) appears next to a book word.
- If there are no signs ("Марафон похудения" (Weight-loss marathon), "Жириновский. Собрание сочинений" (Zhirinovsky. Collected Works), "Учебник по физике" (Physics textbook)), the card is **not opened**: the lot is saved in `avito_lot_checks` with status `skipped` and `skip_reason='title_gate'` and is marked as notified.
- A year in the title that contradicts the book (a later reprint, e.g. "Слово о полку Игореве. 1985") removes its title from the check; such a lot is also not opened.
- The first poll of a book link only records the results; cards are not opened. The first **deep** scan of an existing link is also a baseline (no notifications), the cursor is stored in `search_links.deep_scan_done` / `search_links.deep_scan_page`.
- Global deduplication by the numeric Avito lot ID (`avito_lot_checks.lot_id`, extracted from the URL by `db.database.lot_id_from_url`), with the full URL as a fallback key. A re-posted lot that changed its slug is not opened twice; legacy duplicate rows are soft-deleted (`is_active = 0`), never deleted.
- The listing is walked page by page (`parsers/avito.py::build_page_url`): only the `p` parameter changes, the user's sort (`s=104`) and the opaque `f`/`context`/`cd`/`localPriority`/`q` stay untouched. The scan stops after a fully scanned page yields no unseen lot ID, the depth is capped at `AVITO_BOOKS_MAX_PAGES`, and pages are fetched with a 5–7 s + random delay. `mark_missing_avito_items` is **not** called in book mode: an incomplete scan must not deactivate listings that were never visited.
- A seller with 50+ reviews is skipped; a listing older than 24 hours is skipped; an unknown date goes through with the note "Date not determined". The review count is taken from `[data-marker="rating-caption/rating"]` (fallbacks: the link `a[href="#open-reviews-list"]` and the text of the `item-view/seller-info` block); Avito no longer serves the old `seller-info/reviews`.
- A match "by title only" or "title + author" (without a year, publisher/series, or edition marker) is not notified: a modern reprint has the same title and the same author. The author confirms the work, but not the edition. Such lots are saved as `skipped`/`no_evidence`. The exception is an unresolved duplicate of an edition (`needs_year`): it is queued for the LLM (`status = 'llm_pending'`) and notified only after the LLM confirms the target edition.
- Publisher evidence is taken **only from the segment of the matched position**: a bare generic word ("наука", "академия", "ленинград", "петроград", "памятник", "ссср") counts only next to a publisher marker ("Издательство …", "изд. …"). The author is not edition evidence anymore, and a general collection ("Полный комплект Сочинений … в 4 томах") without a volume/year is downgraded to a weak match.
- Ambiguous lots (weak match, no evidence, or `needs_year`) go to the LLM queue: `scheduler/jobs.py::poll_book_llm_queue` calls `books/llm_verifier.py::verify_lot` with up to 3 catalog candidates plus the lot title and description, and notifies only on a `match` verdict with `is_target_edition = true`. The provider chain is free-only: kilo → nvidia → opencode; the second model is used only when the first answers `unclear` or fails. When the free quota is exhausted the bot **does not pay**: it waits, tells the user which model/provider hit the limit, and keeps the lot in the queue.
- Adjacent lines of one item are merged before matching (`_merge_year_segments` + `_merge_key_segments`): a year-only line, a line with only an author, or a line with only a catalog title. Examples: "Робинзон Крузо" + "Дефо. 1929" → strong match; "Записки о Галльской войне" + "Цезарь. 1948" → strong. A year line is merged with its neighbor only if it contains the author/title of the same book; otherwise a year from another item does not leak over.
- If one book (or one duplicate title) is found in the lot, merging is not needed: the entire description refers to it, so the match is recalculated over the whole text. Then a year from a distant paragraph confirms the target edition or rules the lot out as a reprint ("Робинзон Крузо" + "Москва: Эксмо, 2023" → lot rejected). In a lot with several books, a year stays tied to its own item.
- Catalog: `docs/books/02_price_table.md` (50 priced editions) + `docs/books/04_avito_catalog_ext.md` (24 further editions without prices) → `scripts/build_books_catalog.py` → `data/books_catalog.json` (file is in git, 74 entries, indices 1–74). New entries may have a year range instead of a single year and no price — the notification then shows no price estimate.
- Editions 69–74 are matched by strict keys and discriminators, not by a loose title: «Размышления» requires the author (Марк Аврелий / Аврелий Антонин, `needs_author`) and the two editions are separated by the city (Ленинград 1985 vs СПб. 1993); Cicero's «Письма…» volumes are separated by the volume marker («Т. II» / «Т. III»); the Mahabharata books are separated by the chapter name (Адипарва / Сабхапарва / Виратапарва / Удьйогапарва). A bare «Махабхарата» or «Письма к Аттику» without a volume/chapter matches nothing. Volumes/chapters of the same work share the `group` field (`цицерон_письма`, `махабхарата`) so one lot does not produce duplicate notifications.
- Reference list and selection rules: `docs/books/01_reference_list.md`, `docs/books/03_lots_and_checklist.md`.
- Mode order: price-mode links (`/add`) and book links are polled by **separate scheduler tasks** — `poll_avito_search` (price only) and `poll_avito_books` (books only, `_run_avito_books_periodic`) — with separate intervals (`AVITO_POLL_INTERVAL_MINUTES` and `AVITO_BOOKS_POLL_INTERVAL_MINUTES`). They now use separate trusted Chrome profiles: price mode resolves `AVITO_PROFILE_DIR` (default `./chrome_profile_avito`), book mode resolves `AVITO_BOOKS_PROFILE_DIR` (default `./chrome_profile_avito_books`). The `_AvitoBrowser` registry is keyed by the resolved absolute profile directory, so the modes have different browser managers, request/lifecycle locks, Chrome processes, and profile locks. There is no cross-mode priority wait: price does not wait for books, books does not wait for price, and a books session may start Chrome while a price pass is active. The only serialization is between requests using the same profile. A running book request is capped by `AVITO_REQUEST_TIMEOUT_SECONDS` (75 s constant) — and so is the price page fetch (`browser.price_call`), so a hung Chrome can never freeze the price notifications. On timeout that profile is quarantined: only Chrome processes with that profile (in any path variant) are killed, the stale profile locks are cleared without deleting cookies, the browser is removed from the registry, and the current domain pass is aborted — the next cycle starts a fresh session. A failed `start_session()` sweeps that same profile before retrying. The block flag is sticky per browser/profile, so resetting a blocked books profile never touches the price profile and vice versa. The real bounds are: one request may run for up to 75 s before quarantine; the other mode on its own profile is not delayed by that timeout, but two Chrome sessions from one IP can still trigger Avito anti-bot rules and increase traffic. Books behaviour itself is unchanged (per-domain budgets, 5–7 s page delays, deep-scan cursor, baseline, LLM queue); every actual page fetch (including a retry) consumes the page budget, and the book-link order rotates round-robin so later links are not starved. Price-mode prior state is keyed by numeric lot ID (URL fallback), so a returning lot with a changed slug is not treated as a new listing. If `AVITO_PROFILE_DIR` and `AVITO_BOOKS_PROFILE_DIR` resolve to the same directory, the books task is not started at all: the mismatch is logged as ERROR and reported to the admin (`send_admin_notification`).

### Telegram bot
- Use `ConversationHandler` for multi-step dialogs (`/add`)
- Friendly messages to the user, without technical details
- All messages with emoji for clarity (🔔 📦 💰 🏪 🔗)
- Inline buttons for destructive actions (deletion with confirmation)
- The `bot_instance.py` singleton: `set_bot()` / `get_bot()` gives access to the Bot from the scheduler/notifications
- `admin_notify.py`: `send_admin_notification()` sends service notifications to the administrator (if `ADMIN_TELEGRAM_ID` is set)

### Project documentation updates itself

AGENTS.md and README.md are living project documents, not a one-time instruction. The agent updates them on its own, without a separate instruction from the user, as soon as something changes in the project:

- A new parser, script, module, or folder: reflect it in the "Project structure" section of AGENTS.md.
- A new dependency or a Python version change: update the "Stack" section in AGENTS.md and `requirements.txt`.
- A new environment variable: add it to the "Environment variables" table in AGENTS.md.
- A new bot command or a change in the behavior of an existing one: update the "Bot commands" table in AGENTS.md and the USAGE section in docs/.
- A change in the deployment process, service management, or server setup: update the "Deploying to the server" section in README.md.
- This is part of the Definition of Done for any task: if code changes but AGENTS.md/README.md are not updated, the task is not considered complete.

Do not wait for the user to ask for a documentation update; do it immediately after the code change.

## Context discipline

In every session the operator starts from a blank slate: everything learned and not saved to tools is lost forever. Context is not kept in the agent's memory; it is moved out into the project's and system's persistent tools.

### 1. Persistent memory: `lemma` (MCP server)

The MCP server `lemma` is installed and configured (`npx -y lemma-mcp@latest`, database `~/.lemma/lemma.db`, config `~/.lemma/config.json`). It is the only place where knowledge survives the end of a session.

The workflow is **recall → act → record**:
1. Before starting any task (code, debugging, research, documentation), first call `memory_read` / `semantic_search` / `memory_library` and load what is already known about the project. Do not re-research what is already saved.
2. Perform the task.
3. When done, save new conclusions via `memory_add` (fragment types: `fact`, `pattern`, `lesson`, `warning`, `context`), and dead-end paths via `session_attempt`. Whatever is not saved is lost forever.
4. Fragments are stored **in English** (a search requirement). One idea per fragment, 30–2000 characters.
5. Link related facts via `memory_relate` (supports / contradicts / replaces / related).

Server limits (from `~/.lemma/config.json`): when injecting context, no more than 15 full fragments, 30 summaries, and 20 guides; token budget: `full_content` 5000, `summary_index` 1000, `guides_detail` 1000, `instructions` 1500, `continuity` 800. Old entries lose weight over time (decay: `fact` 60 days, `warning` 30 days, `lesson` 90 days, `pattern` 180 days, `context` 120 days). Eviction is disabled, and the fragment limit is 10,000.

### 2. In-session conversation compression: `compress`

When a section of the conversation is closed and no longer needed in its original form, replace it with a compressed summary using the `compress` tool. The original content has served its purpose; the summary becomes the authoritative record so that the context window stays high in signal.

### 3. Dynamic context pruning: the DCP plugin

The plugin `@tarquinen/opencode-dcp@latest` is installed, with config `~/.config/opencode/dcp.jsonc` (currently empty, schema only). It automatically prioritizes and prunes context as the session grows. Do not rely on everything in the past being visible to the operator; record important decisions in `lemma`.

### 4. Language servers (LSP)

In `~/.config/opencode/opencode.jsonc`, the `lsp` section: `pyright` is disabled; `basedpyright` (`basedpyright-langserver --stdio`) for `.py` / `.pyi`; `ruby-lsp` for `.rb` / `.rake` / `.gemspec` / `.ru`. Use them for type checking and navigation in Python code, rather than relying only on reading files.

### 5. System skills

The available opencode skills related to context: `lemma` (memory), `codebase` (repository overview without reading file by file), `ponytail` / `ponytail-review` (code and review), `unslop` (editing text for the user), `senate` (complex questions). Load a skill via `skill` no more than once per session.

### 6. What not to do

- Do not rely on the operator remembering details from previous sessions; check `lemma` before starting.
- Do not read files one by one if an overview via the codebase or `lemma` is enough; it wastes tokens.
- Do not store decisions only in the conversation; record them via `memory_add`.
- Do not paste whole files or whole logs into context; read only the needed range and filter logs.

### 7. Searching and reading code

- Search code with `grep` (`rg`); find files with `glob` or `fdfind` (installed on the system).
- Read only the needed line range of a file, using `offset`/`limit` in `read`; do not load whole files into context.
- To find a file by content: `rg -l "ClassName"` or `grep`, then read specific lines via `read`.

### 8. Logs

- Never paste whole logs into context. First filter them, for example:
  - `rg -n "ERROR|Exception|Backtrace" <log_file> | tail -50`
- Read only the filtered fragment.

### 9. Images and OCR

- **OCR must not be used.** Do not load or read images via `read`. Text and code only.