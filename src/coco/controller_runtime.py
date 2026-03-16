"""Controller runtime bootstrap."""

from __future__ import annotations

import logging

from .bot import create_bot
from .config import config


logger = logging.getLogger(__name__)


def run_controller() -> None:
    """Start the Telegram-facing controller runtime."""
    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Session provider: %s", config.session_provider)
    logger.info("Sessions path: %s", config.sessions_path)
    logger.info("Assistant command: %s", config.assistant_command)
    logger.info("Tmux bootstrap removed; using app-server transport only")
    logger.info("Starting Telegram controller...")
    application = create_bot()
    application.run_polling(
        allowed_updates=["message", "channel_post", "callback_query"]
    )
