from telegram import Bot


_bot: Bot | None = None


def set_bot(bot: Bot):
    global _bot
    _bot = bot


def get_bot() -> Bot | None:
    return _bot