"""Direct shell CLI for topic-scoped CoCo app management."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import coco.bot as bot
from .config import config
from .handlers.autoresearch import get_autoresearch_state, run_autoresearch_now, set_autoresearch_outcome
from .handlers.looper import (
    LOOPER_DEFAULT_INTERVAL_SECONDS,
    LOOPER_MAX_INTERVAL_SECONDS,
    LOOPER_MIN_INTERVAL_SECONDS,
    build_looper_prompt,
    get_looper_state,
    start_looper,
    stop_looper,
)
from .session import TopicBinding, session_manager
from .skills import SkillDefinition, resolve_skill_identifier

_ENV_USER_ID = "COCO_CURRENT_USER_ID"
_ENV_CHAT_ID = "COCO_CURRENT_CHAT_ID"
_ENV_THREAD_ID = "COCO_CURRENT_THREAD_ID"


@dataclass(frozen=True)
class TopicTarget:
    user_id: int
    thread_id: int
    chat_id: int | None = None
    binding: TopicBinding | None = None


def _current_working_directory() -> Path:
    return Path.cwd().resolve()


def _parse_int_flag(raw: str, *, flag_name: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid value for {flag_name}: {raw}") from exc


def _extract_topic_flags(argv: list[str]) -> tuple[list[str], int | None, int | None, int | None]:
    remaining: list[str] = []
    user_id: int | None = None
    chat_id: int | None = None
    thread_id: int | None = None
    idx = 0
    while idx < len(argv):
        token = argv[idx]
        if token == "--user-id":
            idx += 1
            if idx >= len(argv):
                raise RuntimeError("Missing value for --user-id")
            user_id = _parse_int_flag(argv[idx], flag_name="--user-id")
        elif token.startswith("--user-id="):
            user_id = _parse_int_flag(token.partition("=")[2], flag_name="--user-id")
        elif token == "--chat-id":
            idx += 1
            if idx >= len(argv):
                raise RuntimeError("Missing value for --chat-id")
            chat_id = _parse_int_flag(argv[idx], flag_name="--chat-id")
        elif token.startswith("--chat-id="):
            chat_id = _parse_int_flag(token.partition("=")[2], flag_name="--chat-id")
        elif token == "--thread-id":
            idx += 1
            if idx >= len(argv):
                raise RuntimeError("Missing value for --thread-id")
            thread_id = _parse_int_flag(argv[idx], flag_name="--thread-id")
        elif token.startswith("--thread-id="):
            thread_id = _parse_int_flag(token.partition("=")[2], flag_name="--thread-id")
        else:
            remaining.append(token)
        idx += 1
    return remaining, user_id, chat_id, thread_id


def _resolve_target_from_binding_cwd(
    *,
    current_cwd: Path,
) -> TopicTarget:
    candidates: list[tuple[int, TopicTarget]] = []
    for user_id, chat_id, thread_id, binding in session_manager.iter_topic_bindings():
        machine_id = str(binding.machine_id or "").strip()
        if machine_id and machine_id != config.machine_id:
            continue
        raw_cwd = str(binding.cwd or "").strip()
        if not raw_cwd and binding.window_id:
            raw_cwd = session_manager.get_window_state(binding.window_id).cwd.strip()
        if not raw_cwd:
            continue
        try:
            binding_cwd = Path(raw_cwd).expanduser().resolve()
        except OSError:
            continue
        if current_cwd == binding_cwd or current_cwd.is_relative_to(binding_cwd):
            candidates.append(
                (
                    len(binding_cwd.parts),
                    TopicTarget(
                        user_id=user_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        binding=binding,
                    ),
                )
            )
    if not candidates:
        raise RuntimeError(
            "Unable to infer the current topic from this workspace. "
            "Pass --user-id and --thread-id, optionally --chat-id."
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_depth = candidates[0][0]
    best = [target for depth, target in candidates if depth == best_depth]
    if len(best) != 1:
        details = ", ".join(
            f"user={item.user_id} chat={item.chat_id} thread={item.thread_id}"
            for item in best
        )
        raise RuntimeError(
            "Current workspace matches multiple topics. "
            f"Pass explicit topic flags. Candidates: {details}"
        )
    return best[0]


def _resolve_topic_target(
    *,
    user_id: int | None,
    chat_id: int | None,
    thread_id: int | None,
) -> TopicTarget:
    env_user_id = user_id
    env_chat_id = chat_id
    env_thread_id = thread_id
    if env_user_id is None and (_raw := bot.os.getenv(_ENV_USER_ID)):
        env_user_id = _parse_int_flag(_raw, flag_name=_ENV_USER_ID)
    if env_chat_id is None and (_raw := bot.os.getenv(_ENV_CHAT_ID)):
        env_chat_id = _parse_int_flag(_raw, flag_name=_ENV_CHAT_ID)
    if env_thread_id is None and (_raw := bot.os.getenv(_ENV_THREAD_ID)):
        env_thread_id = _parse_int_flag(_raw, flag_name=_ENV_THREAD_ID)

    if env_user_id is not None and env_thread_id is not None:
        binding = session_manager.resolve_topic_binding(env_user_id, env_thread_id, chat_id=env_chat_id)
        return TopicTarget(
            user_id=env_user_id,
            chat_id=env_chat_id,
            thread_id=env_thread_id,
            binding=binding,
        )

    return _resolve_target_from_binding_cwd(current_cwd=_current_working_directory())


def _discover_catalog() -> dict[str, SkillDefinition]:
    return session_manager.discover_skill_catalog()


def _enabled_app_names(*, target: TopicTarget, catalog: dict[str, SkillDefinition]) -> list[str]:
    return [
        item.name
        for item in session_manager.resolve_thread_skills(
            target.user_id,
            target.thread_id,
            chat_id=target.chat_id,
            catalog=catalog,
        )
    ]


def _set_app_enabled(
    *,
    target: TopicTarget,
    app_name: str,
    enabled: bool,
    catalog: dict[str, SkillDefinition],
) -> list[str]:
    enabled_names = _enabled_app_names(target=target, catalog=catalog)
    currently_enabled = app_name in enabled_names
    if enabled and not currently_enabled:
        enabled_names = [*enabled_names, app_name]
        session_manager.set_thread_skills(
            target.user_id,
            target.thread_id,
            enabled_names,
            chat_id=target.chat_id,
        )
    elif not enabled and currently_enabled:
        enabled_names = [name for name in enabled_names if name != app_name]
        session_manager.set_thread_skills(
            target.user_id,
            target.thread_id,
            enabled_names,
            chat_id=target.chat_id,
        )
    return enabled_names


def _print_apps_overview(*, target: TopicTarget, catalog: dict[str, SkillDefinition]) -> None:
    enabled_names = set(_enabled_app_names(target=target, catalog=catalog))
    print(
        f"Topic apps for user={target.user_id} chat={target.chat_id} thread={target.thread_id}",
    )
    for name in sorted(catalog):
        status = "enabled" if name in enabled_names else "disabled"
        print(f"- {name}: {status}")


def _print_looper_status(*, target: TopicTarget, catalog: dict[str, SkillDefinition]) -> None:
    enabled = "looper" in set(_enabled_app_names(target=target, catalog=catalog))
    state = get_looper_state(user_id=target.user_id, thread_id=target.thread_id)
    print(f"App: looper")
    print(f"Enabled: {'yes' if enabled else 'no'}")
    print(bot._build_looper_overview_text(state=state))


def _print_autoresearch_status(*, target: TopicTarget, catalog: dict[str, SkillDefinition]) -> None:
    enabled = "autoresearch" in set(_enabled_app_names(target=target, catalog=catalog))
    state = get_autoresearch_state(user_id=target.user_id, thread_id=target.thread_id)
    print("App: autoresearch")
    print(f"Scheduled: {'yes' if enabled else 'no'}")
    if state is None:
        print("Outcome: (not set)")
        return
    print(f"Outcome: {state.outcome or '(not set)'}")
    print(f"Last delivered: {state.last_delivered_for_date or '(never)'}")


def _parse_looper_start_args(subargs: list[str]) -> tuple[str, str, int, int, str]:
    if len(subargs) < 2:
        raise RuntimeError(
            "Usage: `coco apps looper start <plan.md> <keyword> "
            "[--every 10m] [--limit 1h] [--instructions \"...\"]`"
        )

    plan_path = subargs[0].strip()
    keyword = subargs[1].strip()
    interval_seconds = LOOPER_DEFAULT_INTERVAL_SECONDS
    limit_seconds = 0
    instructions = ""
    idx = 2
    while idx < len(subargs):
        token = subargs[idx]
        token_l = token.lower()
        if token_l in {"--every", "--interval"}:
            idx += 1
            if idx >= len(subargs):
                raise RuntimeError("Missing value for --every")
            every_raw = subargs[idx]
            parsed = bot._parse_duration_to_seconds(every_raw, default_unit="m")
            if (
                parsed is not None
                and every_raw.isdigit()
                and idx + 1 < len(subargs)
                and bot._is_duration_unit_token(subargs[idx + 1])
            ):
                combined = f"{every_raw} {subargs[idx + 1]}"
                parsed_combined = bot._parse_duration_to_seconds(combined, default_unit="m")
                if parsed_combined is not None:
                    parsed = parsed_combined
                    idx += 1
            if parsed is None:
                raise RuntimeError(f"Invalid interval: {subargs[idx]}")
            interval_seconds = parsed
            idx += 1
            continue
        if token_l.startswith("--every=") or token_l.startswith("--interval="):
            parsed = bot._parse_duration_to_seconds(token.partition("=")[2], default_unit="m")
            if parsed is None:
                raise RuntimeError(f"Invalid interval: {token.partition('=')[2]}")
            interval_seconds = parsed
            idx += 1
            continue
        if token_l in {"--limit", "--time-limit", "--ttl"}:
            idx += 1
            if idx >= len(subargs):
                raise RuntimeError("Missing value for --limit")
            limit_raw = subargs[idx]
            parsed = bot._parse_duration_to_seconds(limit_raw, default_unit="h")
            if (
                parsed is not None
                and limit_raw.isdigit()
                and idx + 1 < len(subargs)
                and bot._is_duration_unit_token(subargs[idx + 1])
            ):
                combined = f"{limit_raw} {subargs[idx + 1]}"
                parsed_combined = bot._parse_duration_to_seconds(combined, default_unit="h")
                if parsed_combined is not None:
                    parsed = parsed_combined
                    idx += 1
            if parsed is None:
                raise RuntimeError(f"Invalid time limit: {subargs[idx]}")
            limit_seconds = parsed
            idx += 1
            continue
        if token_l.startswith("--limit=") or token_l.startswith("--time-limit=") or token_l.startswith("--ttl="):
            parsed = bot._parse_duration_to_seconds(token.partition("=")[2], default_unit="h")
            if parsed is None:
                raise RuntimeError(f"Invalid time limit: {token.partition('=')[2]}")
            limit_seconds = parsed
            idx += 1
            continue
        if token_l in {"--instructions", "--instruction", "--custom"}:
            idx += 1
            instructions = " ".join(subargs[idx:]).strip()
            break
        if token_l.startswith("--instructions=") or token_l.startswith("--custom="):
            instructions = token.partition("=")[2].strip()
            idx += 1
            if idx < len(subargs):
                trailing = " ".join(subargs[idx:]).strip()
                if trailing:
                    instructions = f"{instructions} {trailing}".strip()
            break
        instructions = " ".join(subargs[idx:]).strip()
        break

    if interval_seconds < LOOPER_MIN_INTERVAL_SECONDS:
        raise RuntimeError(
            f"Interval is too short. Minimum is {bot._format_duration_brief(LOOPER_MIN_INTERVAL_SECONDS)}."
        )
    if interval_seconds > LOOPER_MAX_INTERVAL_SECONDS:
        raise RuntimeError(
            f"Interval is too long. Maximum is {bot._format_duration_brief(LOOPER_MAX_INTERVAL_SECONDS)}."
        )
    return plan_path, keyword, interval_seconds, limit_seconds, instructions


def _handle_looper_command(*, target: TopicTarget, catalog: dict[str, SkillDefinition], subargs: list[str]) -> int:
    subcmd = subargs[0].lower() if subargs else "status"
    if subcmd in {"status", "show", "list"}:
        _print_looper_status(target=target, catalog=catalog)
        return 0
    if subcmd in {"enable", "on"}:
        _set_app_enabled(target=target, app_name="looper", enabled=True, catalog=catalog)
        print("Enabled app `looper` for this topic.")
        return 0
    if subcmd in {"disable"}:
        stop_looper(user_id=target.user_id, thread_id=target.thread_id, reason="cli_disable")
        _set_app_enabled(target=target, app_name="looper", enabled=False, catalog=catalog)
        print("Disabled app `looper` for this topic.")
        return 0
    if subcmd in {"stop", "off", "clear"}:
        stopped = stop_looper(user_id=target.user_id, thread_id=target.thread_id, reason="manual_stop")
        if not stopped:
            print("Looper is already off for this topic.")
            return 0
        print("Looper stopped.")
        print(f"Plan: `{stopped.plan_path}`")
        print(f"Nudges sent: `{stopped.prompt_count}`")
        return 0
    if subcmd not in {"start", "run"}:
        raise RuntimeError("Unknown looper subcommand. Use status, start, stop, enable, or disable.")

    wid = session_manager.resolve_window_for_thread(
        target.user_id,
        target.thread_id,
        chat_id=target.chat_id,
    )
    if not wid:
        raise RuntimeError("No session bound to this topic.")
    plan_path, keyword, interval_seconds, limit_seconds, instructions = _parse_looper_start_args(subargs[1:])
    state = start_looper(
        user_id=target.user_id,
        thread_id=target.thread_id,
        window_id=wid,
        plan_path=plan_path,
        keyword=keyword,
        interval_seconds=interval_seconds,
        limit_seconds=limit_seconds,
        instructions=instructions,
    )
    auto_enabled = False
    enabled = _enabled_app_names(target=target, catalog=catalog)
    if "looper" not in enabled and "looper" in catalog:
        _set_app_enabled(target=target, app_name="looper", enabled=True, catalog=catalog)
        auto_enabled = True
    print("Looper started for this topic.")
    print(f"Plan file: `{state.plan_path}`")
    print(f"Completion keyword: `{state.keyword}`")
    print(f"Interval: `{bot._format_duration_brief(state.interval_seconds)}`")
    if auto_enabled:
        print("App auto-enabled: `looper`")
    print("")
    print("Example nudge:")
    print(
        build_looper_prompt(
            plan_path=state.plan_path,
            keyword=state.keyword,
            instructions=state.instructions,
            deadline_at=state.deadline_at,
        )
    )
    return 0


def _handle_autoresearch_command(*, target: TopicTarget, catalog: dict[str, SkillDefinition], subargs: list[str]) -> int:
    subcmd = subargs[0].lower() if subargs else "status"
    if subcmd in {"status", "show", "list"}:
        _print_autoresearch_status(target=target, catalog=catalog)
        return 0
    if subcmd in {"enable", "on"}:
        _set_app_enabled(target=target, app_name="autoresearch", enabled=True, catalog=catalog)
        print("Enabled app `autoresearch` for this topic.")
        return 0
    if subcmd in {"disable", "off", "stop"}:
        _set_app_enabled(target=target, app_name="autoresearch", enabled=False, catalog=catalog)
        print("Disabled app `autoresearch` for this topic.")
        return 0
    if subcmd == "schedule":
        if len(subargs) < 2:
            raise RuntimeError("Usage: `coco apps autoresearch schedule on|off`")
        mode = subargs[1].strip().lower()
        if mode in {"on", "enable"}:
            _set_app_enabled(target=target, app_name="autoresearch", enabled=True, catalog=catalog)
            print("Daily auto research enabled.")
            return 0
        if mode in {"off", "disable"}:
            _set_app_enabled(target=target, app_name="autoresearch", enabled=False, catalog=catalog)
            print("Daily auto research disabled.")
            return 0
        raise RuntimeError("Usage: `coco apps autoresearch schedule on|off`")
    if subcmd in {"set-outcome", "outcome"}:
        outcome = " ".join(subargs[1:]).strip()
        if not outcome:
            raise RuntimeError("Usage: `coco apps autoresearch set-outcome <text>`")
        set_autoresearch_outcome(
            user_id=target.user_id,
            thread_id=target.thread_id,
            outcome=outcome,
        )
        print("Auto research outcome updated.")
        return 0
    if subcmd == "run":
        resolved_chat_id = session_manager.resolve_chat_id(
            target.user_id,
            target.thread_id,
            chat_id=target.chat_id,
        )
        if resolved_chat_id is None:
            raise RuntimeError("No chat binding for this topic.")
        digest_text = run_autoresearch_now(
            user_id=target.user_id,
            chat_id=resolved_chat_id,
            thread_id=target.thread_id,
        )
        if digest_text:
            print(digest_text)
        else:
            print("No visible Coco activity from yesterday in this topic, so there was nothing to research.")
        return 0
    raise RuntimeError("Unknown autoresearch subcommand. Use status, set-outcome, run, schedule, enable, or disable.")


def _handle_generic_app_subcommand(
    *,
    target: TopicTarget,
    catalog: dict[str, SkillDefinition],
    app_name: str,
    subargs: list[str],
) -> int:
    subcmd = subargs[0].lower() if subargs else "status"
    if app_name == "looper":
        return _handle_looper_command(target=target, catalog=catalog, subargs=subargs)
    if app_name == "autoresearch":
        return _handle_autoresearch_command(target=target, catalog=catalog, subargs=subargs)
    if subcmd in {"status", "show", "list"}:
        enabled = app_name in set(_enabled_app_names(target=target, catalog=catalog))
        print(f"App: {app_name}")
        print(f"Enabled: {'yes' if enabled else 'no'}")
        return 0
    if subcmd in {"enable", "on"}:
        _set_app_enabled(target=target, app_name=app_name, enabled=True, catalog=catalog)
        print(f"Enabled app `{app_name}` for this topic.")
        return 0
    if subcmd in {"disable", "off", "stop"}:
        _set_app_enabled(target=target, app_name=app_name, enabled=False, catalog=catalog)
        print(f"Disabled app `{app_name}` for this topic.")
        return 0
    raise RuntimeError("Unknown app subcommand. Use status, enable, or disable.")


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv or [])
    try:
        args, user_id, chat_id, thread_id = _extract_topic_flags(raw_argv)
        target = _resolve_topic_target(user_id=user_id, chat_id=chat_id, thread_id=thread_id)
        catalog = _discover_catalog()
        if not args or args[0] in {"list", "ls"}:
            _print_apps_overview(target=target, catalog=catalog)
            return 0
        if args[0] in {"clear", "reset"}:
            session_manager.clear_thread_skills(target.user_id, target.thread_id, chat_id=target.chat_id)
            print("Cleared apps for this topic.")
            return 0
        if args[0] in {"enable", "on", "use", "add"}:
            if len(args) < 2:
                raise RuntimeError("Usage: `coco apps enable <name>`")
            canonical = resolve_skill_identifier(" ".join(args[1:]).strip(), catalog)
            if not canonical:
                raise RuntimeError(f"Unknown app: {' '.join(args[1:]).strip()}")
            _set_app_enabled(target=target, app_name=canonical, enabled=True, catalog=catalog)
            print(f"Enabled app `{canonical}` for this topic.")
            return 0
        if args[0] in {"disable", "off", "remove", "rm"}:
            if len(args) < 2:
                raise RuntimeError("Usage: `coco apps disable <name>`")
            canonical = resolve_skill_identifier(" ".join(args[1:]).strip(), catalog)
            if not canonical:
                raise RuntimeError(f"Unknown app: {' '.join(args[1:]).strip()}")
            if canonical == "looper":
                stop_looper(user_id=target.user_id, thread_id=target.thread_id, reason="cli_disable")
            _set_app_enabled(target=target, app_name=canonical, enabled=False, catalog=catalog)
            print(f"Disabled app `{canonical}` for this topic.")
            return 0
        if args[0] in {"show", "status"}:
            if len(args) < 2:
                _print_apps_overview(target=target, catalog=catalog)
                return 0
            canonical = resolve_skill_identifier(args[1], catalog)
            if not canonical:
                raise RuntimeError(f"Unknown app: {args[1]}")
            return _handle_generic_app_subcommand(
                target=target,
                catalog=catalog,
                app_name=canonical,
                subargs=args[2:],
            )

        canonical = resolve_skill_identifier(args[0], catalog)
        if not canonical:
            raise RuntimeError(
                "Unknown apps command. Use `coco apps list`, `enable`, `disable`, `clear`, "
                "or `coco apps <app> ...`."
            )
        return _handle_generic_app_subcommand(
            target=target,
            catalog=catalog,
            app_name=canonical,
            subargs=args[1:],
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
