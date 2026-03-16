"""Application entry point for Telegram bot bootstrap."""

from importlib import import_module
import logging
import sys


def main() -> None:
    """Main entry point."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        config = import_module("coco.config").config
    except ValueError as e:
        utils_module = import_module("coco.utils")
        config_dir = utils_module.coco_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("coco").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    logger.info("Node role: %s", config.node_role)
    if config.node_role == "agent":
        run_agent = import_module("coco.agent_runtime").run_agent

        run_agent()
        return

    run_controller = import_module("coco.controller_runtime").run_controller

    run_controller()


if __name__ == "__main__":
    main()
