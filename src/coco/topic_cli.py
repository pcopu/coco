"""Topic-scoped capability discovery CLI for agents."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from telegram import Bot

from . import app_cli, command_cli
from .config import config
from .handlers.autoresearch import get_autoresearch_state
from .handlers.looper import get_looper_state
from .handlers.topic_send import send_message_to_topic, send_text_to_topic
from .session import TopicBinding, WindowState, session_manager
from .skills import SkillDefinition
from .transcription import resolve_transcription_runtime


def _extract_json_flag(argv: list[str]) -> tuple[list[str], bool]:
    remaining: list[str] = []
    json_mode = False
    for token in argv:
        if token == "--json":
            json_mode = True
            continue
        remaining.append(token)
    return remaining, json_mode


def _parse_send_args(args: list[str]) -> tuple[str, str, str, str, str]:
    text_value = ""
    text_file = ""
    image_url = ""
    image_file = ""
    video_url = ""
    video_file = ""
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--text":
            idx += 1
            if idx >= len(args):
                raise RuntimeError("Missing value for --text")
            text_value = args[idx]
        elif token.startswith("--text="):
            text_value = token.partition("=")[2]
        elif token == "--text-file":
            idx += 1
            if idx >= len(args):
                raise RuntimeError("Missing value for --text-file")
            text_file = args[idx]
        elif token.startswith("--text-file="):
            text_file = token.partition("=")[2]
        elif token == "--image-url":
            idx += 1
            if idx >= len(args):
                raise RuntimeError("Missing value for --image-url")
            image_url = args[idx]
        elif token.startswith("--image-url="):
            image_url = token.partition("=")[2]
        elif token == "--image-file":
            idx += 1
            if idx >= len(args):
                raise RuntimeError("Missing value for --image-file")
            image_file = args[idx]
        elif token.startswith("--image-file="):
            image_file = token.partition("=")[2]
        elif token == "--video-url":
            idx += 1
            if idx >= len(args):
                raise RuntimeError("Missing value for --video-url")
            video_url = args[idx]
        elif token.startswith("--video-url="):
            video_url = token.partition("=")[2]
        elif token == "--video-file":
            idx += 1
            if idx >= len(args):
                raise RuntimeError("Missing value for --video-file")
            video_file = args[idx]
        elif token.startswith("--video-file="):
            video_file = token.partition("=")[2]
        else:
            raise RuntimeError(
                "Usage: `coco topic send --text \"...\" [--image-url URL|--image-file PATH|--video-url URL|--video-file PATH]` "
                "or `--text-file /path/file.md [--image-url URL|--image-file PATH|--video-url URL|--video-file PATH]`"
            )
        idx += 1

    if bool(text_value) == bool(text_file):
        raise RuntimeError("Provide exactly one of --text or --text-file.")
    media_source_count = sum(
        1 for value in (image_url, image_file, video_url, video_file) if value
    )
    if media_source_count > 1:
        raise RuntimeError(
            "Provide at most one media source: --image-url, --image-file, --video-url, or --video-file."
        )
    if text_file:
        try:
            text_value = Path(text_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Failed reading text file: {exc}") from exc
    return text_value, image_url, image_file, video_url, video_file


def _command_examples() -> dict[str, str]:
    return {
        "mentions": "coco mentions on",
        "fast": "coco fast off",
        "transcription": "coco transcription",
        "looper": "coco looper start plans/ship.md done --every 15m",
        "apps": "coco apps list",
        "topic-send": "coco topic send --text \"hello\"",
        "topic": "coco topic --json",
    }


def _app_actions(app_name: str) -> list[str]:
    if app_name == "looper":
        return ["status", "start", "stop", "enable", "disable"]
    if app_name == "autoresearch":
        return [
            "status",
            "set-outcome",
            "run",
            "schedule on",
            "schedule off",
            "enable",
            "disable",
        ]
    return ["status", "enable", "disable"]


def _window_state_from_binding(binding: TopicBinding | None) -> WindowState | None:
    if binding is None:
        return None
    window_id = str(binding.window_id or "").strip()
    if not window_id:
        return None
    return session_manager.get_window_state(window_id)


def _topic_binding(target: app_cli.TopicTarget) -> TopicBinding | None:
    if target.binding is not None:
        return target.binding
    return session_manager.resolve_topic_binding(
        target.user_id,
        target.thread_id,
        chat_id=target.chat_id,
    )


def _session_payload(*, target: app_cli.TopicTarget, binding: TopicBinding | None) -> dict[str, Any]:
    window_state = _window_state_from_binding(binding)
    model_slug, reasoning_effort = session_manager.get_topic_model_selection(
        target.user_id,
        target.thread_id,
        chat_id=target.chat_id,
    )
    service_tier = session_manager.get_topic_service_tier_selection(
        target.user_id,
        target.thread_id,
        chat_id=target.chat_id,
    )
    mention_only = False
    if binding is not None and binding.window_id:
        mention_only = session_manager.get_window_mention_only(binding.window_id)

    codex_thread_id = ""
    active_turn_id = ""
    if window_state is not None:
        codex_thread_id = window_state.codex_thread_id.strip()
        active_turn_id = window_state.codex_active_turn_id.strip()
    elif binding is not None:
        codex_thread_id = binding.codex_thread_id.strip()

    return {
        "bound": bool(binding and (binding.window_id or binding.codex_thread_id or binding.cwd)),
        "mention_only": mention_only,
        "model": model_slug or "unknown",
        "reasoning_effort": reasoning_effort or "unknown",
        "service_tier": service_tier or "flex",
        "fast_mode": (service_tier or "flex") == "fast",
        "codex_thread_id": codex_thread_id,
        "active_turn_id": active_turn_id,
    }


def _topic_payload(*, target: app_cli.TopicTarget, binding: TopicBinding | None) -> dict[str, Any]:
    window_state = _window_state_from_binding(binding)
    cwd = ""
    display_name = ""
    window_id = ""
    if binding is not None:
        cwd = str(binding.cwd or "").strip()
        display_name = str(binding.display_name or "").strip()
        window_id = str(binding.window_id or "").strip()
    if window_state is not None:
        if not cwd:
            cwd = window_state.cwd.strip()
        if not display_name:
            display_name = window_state.window_name.strip()
    return {
        "user_id": target.user_id,
        "chat_id": target.chat_id,
        "thread_id": target.thread_id,
        "window_id": window_id,
        "cwd": cwd,
        "display_name": display_name,
    }


def _app_state_payload(*, app_name: str, target: app_cli.TopicTarget) -> dict[str, Any] | None:
    if app_name == "looper":
        state = get_looper_state(user_id=target.user_id, thread_id=target.thread_id)
        if state is None:
            return {"running": False}
        return {
            "running": True,
            "plan_path": state.plan_path,
            "keyword": state.keyword,
            "instructions": state.instructions,
            "interval_seconds": int(state.interval_seconds),
            "prompt_count": int(state.prompt_count),
            "deadline_at": float(state.deadline_at),
        }
    if app_name == "autoresearch":
        state = get_autoresearch_state(target.user_id, target.thread_id)
        if state is None:
            return {"outcome": "", "last_delivered_for_date": "", "last_researched_for_date": ""}
        return {
            "outcome": state.outcome,
            "last_researched_for_date": state.last_researched_for_date,
            "last_delivered_for_date": state.last_delivered_for_date,
            "last_digest_text": state.last_digest_text,
            "last_session_count": int(state.last_session_count),
        }
    return None


def _apps_payload(
    *,
    target: app_cli.TopicTarget,
    catalog: dict[str, SkillDefinition],
) -> list[dict[str, Any]]:
    enabled_names = set(app_cli._enabled_app_names(target=target, catalog=catalog))
    apps: list[dict[str, Any]] = []
    for name in sorted(catalog):
        skill = catalog[name]
        entry: dict[str, Any] = {
            "name": name,
            "enabled": name in enabled_names,
            "description": skill.description,
            "actions": _app_actions(name),
        }
        state = _app_state_payload(app_name=name, target=target)
        if state is not None:
            entry["state"] = state
        apps.append(entry)
    return apps


def _build_payload(*, target: app_cli.TopicTarget) -> dict[str, Any]:
    binding = _topic_binding(target)
    catalog = session_manager.discover_skill_catalog()
    topic = _topic_payload(target=target, binding=binding)
    session = _session_payload(target=target, binding=binding)
    transcription_runtime = resolve_transcription_runtime("compatible")
    commands_available = sorted({*command_cli.CLI_COMMAND_NAMES, "apps", "topic"})
    return {
        "topic": topic,
        "session": session,
        "commands": {
            "available": commands_available,
            "examples": _command_examples(),
        },
        "transcription": {
            "mode": "compatible",
            "device": transcription_runtime.device,
            "compute_type": transcription_runtime.compute_type,
            "model_name": transcription_runtime.model_name,
        },
        "apps": _apps_payload(target=target, catalog=catalog),
    }


def _render_text(payload: dict[str, Any]) -> str:
    topic = payload["topic"]
    session = payload["session"]
    apps = payload["apps"]
    transcription = payload["transcription"]
    enabled_names = [app["name"] for app in apps if app.get("enabled")]
    lines = [
        (
            f"Topic: user={topic['user_id']} chat={topic['chat_id']} "
            f"thread={topic['thread_id']}"
        )
    ]
    if topic.get("cwd"):
        lines.append(f"Workspace: `{topic['cwd']}`")
    if topic.get("display_name"):
        lines.append(f"Display: `{topic['display_name']}`")
    if topic.get("window_id"):
        lines.append(f"Window: `{topic['window_id']}`")
    lines.extend(
        [
            f"Session bound: `{'yes' if session['bound'] else 'no'}`",
            f"Mention-only: `{'ON' if session['mention_only'] else 'OFF'}`",
            f"Model: `{session['model']}`",
            f"Reasoning: `{session['reasoning_effort']}`",
            (
                f"Fast mode: `{'ON' if session['fast_mode'] else 'OFF'}` "
                f"(`{session['service_tier']}`)"
            ),
            (
                "Transcription: "
                f"`{transcription['mode']}` -> "
                f"`{transcription['device']} / {transcription['compute_type']} / {transcription['model_name']}`"
            ),
            (
                "Enabled apps: "
                + ("`" + "`, `".join(enabled_names) + "`" if enabled_names else "(none)")
            ),
            "Commands: " + ", ".join(f"`{name}`" for name in payload["commands"]["available"]),
            "Apps:",
        ]
    )
    for app in apps:
        enabled_label = "enabled" if app.get("enabled") else "disabled"
        lines.append(
            f"- `{app['name']}`: {enabled_label}; actions: {', '.join(app['actions'])}"
        )
    return "\n".join(lines)


async def _send_text_to_current_topic_async(
    *,
    target: app_cli.TopicTarget,
    text: str,
) -> tuple[bool, str]:
    async with Bot(token=config.telegram_bot_token) as telegram_bot:
        return await send_text_to_topic(
            telegram_bot,
            user_id=target.user_id,
            thread_id=target.thread_id,
            chat_id=target.chat_id,
            text=text,
        )


async def _send_message_to_current_topic_async(
    *,
    target: app_cli.TopicTarget,
    text: str,
    image_url: str = "",
    image_file: str = "",
    video_url: str = "",
    video_file: str = "",
) -> tuple[bool, str]:
    if not image_url and not image_file and not video_url and not video_file:
        return await _send_text_to_current_topic_async(
            target=target,
            text=text,
        )
    async with Bot(token=config.telegram_bot_token) as telegram_bot:
        return await send_message_to_topic(
            telegram_bot,
            user_id=target.user_id,
            thread_id=target.thread_id,
            chat_id=target.chat_id,
            text=text,
            image_url=image_url,
            image_file=image_file,
            video_url=video_url,
            video_file=video_file,
        )


def _send_text_to_current_topic(
    *,
    target: app_cli.TopicTarget,
    text: str,
) -> tuple[bool, str]:
    return asyncio.run(
        _send_text_to_current_topic_async(
            target=target,
            text=text,
        )
    )


def _send_message_to_current_topic(
    *,
    target: app_cli.TopicTarget,
    text: str,
    image_url: str = "",
    image_file: str = "",
    video_url: str = "",
    video_file: str = "",
) -> tuple[bool, str]:
    if not image_url and not image_file and not video_url and not video_file:
        return _send_text_to_current_topic(
            target=target,
            text=text,
        )
    return asyncio.run(
        _send_message_to_current_topic_async(
            target=target,
            text=text,
            image_url=image_url,
            image_file=image_file,
            video_url=video_url,
            video_file=video_file,
        )
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv or [])
    try:
        cleaned_argv, json_mode = _extract_json_flag(raw_argv)
        args, user_id, chat_id, thread_id = app_cli._extract_topic_flags(cleaned_argv)
        target = app_cli._resolve_topic_target(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        if args:
            subcommand = args[0].strip().lower()
            if subcommand != "send":
                raise RuntimeError("Usage: `coco topic [--json]` or `coco topic send --text ...`")
            text, image_url, image_file, video_url, video_file = _parse_send_args(args[1:])
            ok, error_text = _send_message_to_current_topic(
                target=target,
                text=text,
                image_url=image_url,
                image_file=image_file,
                video_url=video_url,
                video_file=video_file,
            )
            if not ok:
                print(f"Error: {error_text}", file=sys.stderr)
                return 1
            print("Sent message to topic.")
            return 0
        payload = _build_payload(target=target)
        if json_mode:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(_render_text(payload))
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
