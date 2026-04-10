"""Direct shell bridge for Telegram slash commands."""

from __future__ import annotations

import asyncio
import shlex
import sys
from types import SimpleNamespace
from typing import Awaitable, Callable

import coco.bot as bot
from . import app_cli
from .handlers import commands as command_handlers

CLI_COMMAND_NAMES = {
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

_Handler = Callable[[object, object], Awaitable[None]]


def _normalize_command_name(raw: str) -> str:
    return raw.strip().lstrip("/").lower()


def _build_command_text(command_name: str, args: list[str]) -> str:
    if not args:
        return f"/{command_name}"
    return f"/{command_name} " + " ".join(shlex.quote(part) for part in args)


def _make_update(*, command_name: str, args: list[str], target: app_cli.TopicTarget) -> object:
    chat_id = target.chat_id
    if chat_id is None:
        chat = SimpleNamespace(type="private", id=target.user_id)
    else:
        chat = SimpleNamespace(type="supergroup", id=chat_id)
    message = SimpleNamespace(
        text=_build_command_text(command_name, args),
        message_thread_id=target.thread_id,
        chat=chat,
        reply_to_message=None,
        is_topic_message=True,
    )
    user = SimpleNamespace(id=target.user_id, first_name="", last_name="", username="")
    return SimpleNamespace(
        effective_user=user,
        effective_message=message,
        effective_chat=chat,
        message=message,
    )


def _make_context() -> object:
    class _CliBot:
        username = "coco_bot"

        async def get_chat_administrators(self, _chat_id: int) -> list[object]:
            return []

    return SimpleNamespace(bot=_CliBot(), user_data={})


def _resolve_handler(command_name: str) -> _Handler:
    handler_name = f"{command_name}_command"
    handler = getattr(command_handlers, handler_name, None)
    if handler is None:
        raise RuntimeError(f"Unknown slash command: {command_name}")
    return handler


async def _run_handler(*, command_name: str, args: list[str], target: app_cli.TopicTarget) -> list[str]:
    outputs: list[str] = []
    update = _make_update(command_name=command_name, args=args, target=target)
    context = _make_context()
    handler = _resolve_handler(command_name)

    async def _safe_reply(_message, text: str, **_kwargs):
        outputs.append(text)
        return SimpleNamespace(message_id=0, text=text)

    async def _safe_edit(_target, text: str, **_kwargs):
        outputs.append(text)

    async def _noop_async(*_args, **_kwargs):
        return None

    original_safe_reply = bot.safe_reply
    original_safe_edit = bot.safe_edit
    original_sync_dock = bot.sync_queued_topic_dock
    original_clear_dock = bot.clear_queued_topic_dock
    original_set_eyes = bot._set_eyes_reaction
    bot.safe_reply = _safe_reply
    bot.safe_edit = _safe_edit
    bot.sync_queued_topic_dock = _noop_async
    bot.clear_queued_topic_dock = _noop_async
    bot._set_eyes_reaction = _noop_async
    try:
        await handler(update, context)
    finally:
        bot.safe_reply = original_safe_reply
        bot.safe_edit = original_safe_edit
        bot.sync_queued_topic_dock = original_sync_dock
        bot.clear_queued_topic_dock = original_clear_dock
        bot._set_eyes_reaction = original_set_eyes
    return outputs


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv or [])
    try:
        args, user_id, chat_id, thread_id = app_cli._extract_topic_flags(raw_argv)
        if not args:
            raise RuntimeError("Usage: `coco <command> [args...] [--user-id ... --thread-id ...]`")
        command_name = _normalize_command_name(args[0])
        if command_name not in CLI_COMMAND_NAMES:
            raise RuntimeError(f"Unknown slash command: {args[0]}")
        target = app_cli._resolve_topic_target(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        outputs = asyncio.run(
            _run_handler(
                command_name=command_name,
                args=args[1:],
                target=target,
            )
        )
        if outputs:
            print("\n\n".join(item for item in outputs if item))
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
