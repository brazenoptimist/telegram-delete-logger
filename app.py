import logging
import sys

from telethon import TelegramClient

from telegram_logger import main
from telegram_logger.settings import settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    with TelegramClient(
        settings.session_name, settings.api_id, settings.api_hash.get_secret_value()
    ) as client:
        bot = TelegramClient('bot', settings.api_id, settings.api_hash.get_secret_value()).start(bot_token=settings.bot_token)
        main.client = client
        main.bot = bot
        client.loop.run_until_complete(main.init())
