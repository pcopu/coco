"""Application entry point for Telegram bot bootstrap."""

from importlib import import_module
import logging
import sys


def main() -> None:
    """Main entry point."""
    argv = list(sys.argv[1:])
    direct_command_names = {
        "start",
        "folder",
        "resume",
        "history",
        "esc",
        "q",
        "approvals",
        "mentions",
        "allowed",
        "skills",
        "worktree",
        "restart",
        "unbind",
        "status",
        "model",
        "fast",
        "transcription",
        "update",
        "looper",
    }
    if argv and argv[0] in {"init", "setup"}:
        bootstrap = import_module("coco.bootstrap")
        code = bootstrap.main(argv[1:])
        if code:
            sys.exit(code)
        return
    if argv and argv[0] == "apps":
        app_cli = import_module("coco.app_cli")
        code = app_cli.main(argv[1:])
        if code:
            sys.exit(code)
        return
    if argv and argv[0].lstrip("/").lower() == "topic":
        topic_cli = import_module("coco.topic_cli")
        code = topic_cli.main(argv[1:])
        if code:
            sys.exit(code)
        return
    if argv and argv[0] in {"cmd", "command"}:
        command_cli = import_module("coco.command_cli")
        code = command_cli.main(argv[1:])
        if code:
            sys.exit(code)
        return
    if argv and argv[0].lstrip("/").lower() in direct_command_names:
        command_cli = import_module("coco.command_cli")
        code = command_cli.main(argv)
        if code:
            sys.exit(code)
        return

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
        print("Quick start:\n")
        print(
            "  coco init --bot-token <bot_token> --admin-user <telegram_user_id> "
            "--group-id <-100supergroup_id>"
        )
        print()
        print(f"Or create {env_path} manually with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print("  ALLOWED_GROUP_IDS=-100your_supergroup_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        print("Get your supergroup ID from @RawDataBot before adding CoCo to the group.")
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
