import asyncio
import subprocess
import shutil
import signal

from dotenv import load_dotenv
import os

from telegram.ext import Application

from bot.handlers import setup_handlers
from bot.bot_instance import set_bot
from db.database import init_db, close_db
from scheduler.jobs import start_scheduler
from utils.logger import setup_logger
from utils.display import ensure_xvfb, stop_xvfb

logger = setup_logger(__name__)


def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    xvfb_proc = ensure_xvfb()

    async def _run():
        await init_db()
        scheduler = start_scheduler()

        builder = Application.builder().token(token)
        proxy_url = os.getenv("PROXY_URL")
        if proxy_url:
            builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
            logger.info("Proxy configured: %s", proxy_url.split("@")[1] if "@" in proxy_url else proxy_url)
        application = builder.build()
        setup_handlers(application)
        set_bot(application.bot)

        await application.initialize()
        await application.start()
        await application.updater.start_polling()

        logger.info("Bot started polling, waiting for updates...")

        stop_event = asyncio.Event()

        def _signal_handler():
            logger.info("Shutdown signal received")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

        await stop_event.wait()

        logger.info("Shutting down...")
        scheduler.cancel()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await close_db()

    asyncio.run(_run())

    if xvfb_proc:
        stop_xvfb(xvfb_proc)


if __name__ == "__main__":
    main()
