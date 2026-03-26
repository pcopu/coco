"""Shared external research runner for daily digest apps."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shlex
import subprocess

from ..config import config
from ..utils import atomic_write_json, coco_dir, env_alias

logger = logging.getLogger(__name__)

BACKEND_AUTO = "auto"
BACKEND_HEURISTIC = "heuristic"
BACKEND_EXTERNAL = "external"


def research_backend_mode(env_prefix: str) -> str:
    """Return research backend mode for one app env prefix."""
    raw = env_alias(f"{env_prefix}_RESEARCH_BACKEND", default="")
    normalized = raw.strip().lower()
    if normalized == BACKEND_HEURISTIC:
        return BACKEND_HEURISTIC
    if normalized == BACKEND_EXTERNAL:
        return BACKEND_EXTERNAL
    if normalized == BACKEND_AUTO:
        return BACKEND_AUTO
    return BACKEND_HEURISTIC


def _bundle_root(*, app_slug: str, env_prefix: str) -> Path:
    raw = env_alias(f"{env_prefix}_RESEARCH_BUNDLE_DIR")
    if raw:
        return Path(raw).expanduser()
    return coco_dir() / f"{app_slug}-research"


def _research_workdir(*, env_prefix: str, bundle_dir: Path) -> Path:
    raw = env_alias(f"{env_prefix}_RESEARCH_WORKDIR")
    if raw:
        return Path(raw).expanduser()
    return bundle_dir


def _research_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "message_text": {"type": "string"},
            "session_count": {"type": "integer", "minimum": 0},
            "success_count": {"type": "integer", "minimum": 0},
            "failure_count": {"type": "integer", "minimum": 0},
            "focus_terms": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
            "positive_terms": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
            "negative_terms": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
        },
        "required": [
            "message_text",
            "session_count",
            "success_count",
            "failure_count",
            "focus_terms",
            "positive_terms",
            "negative_terms",
        ],
    }


def _strip_codex_policy_flags(args: list[str]) -> list[str]:
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in {"-a", "--ask-for-approval", "-s", "--sandbox"}:
            i += 2
            continue
        if token.startswith("--ask-for-approval=") or token.startswith("--sandbox="):
            i += 1
            continue
        if token in {
            "--full-auto",
            "--dangerously-bypass-approvals-and-sandbox",
        }:
            i += 1
            continue
        cleaned.append(token)
        i += 1
    return cleaned


def _codex_base_args() -> list[str]:
    try:
        args = shlex.split(config.assistant_command)
    except ValueError:
        args = []
    args = _strip_codex_policy_flags(args)
    if args:
        first = Path(args[0]).name.lower()
        if "codex" in first:
            return [args[0]]
    return ["codex"]


def _research_reasoning_effort(env_prefix: str) -> str:
    raw = env_alias(f"{env_prefix}_RESEARCH_REASONING_EFFORT", default="low")
    normalized = raw.strip().lower()
    if normalized in {"low", "medium", "high", "xhigh"}:
        return normalized
    return "low"


def _research_model(env_prefix: str) -> str:
    return env_alias(f"{env_prefix}_RESEARCH_MODEL").strip()


def _write_bundle(
    *,
    app_slug: str,
    env_prefix: str,
    target_date: str,
    user_id: int,
    thread_id: int,
    bundle_payload: dict[str, object],
    program_markdown: str,
) -> tuple[Path, Path, Path, Path, Path]:
    bundle_dir = _bundle_root(app_slug=app_slug, env_prefix=env_prefix) / target_date / f"{user_id}_{thread_id}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    input_path = bundle_dir / "sessions.json"
    program_path = bundle_dir / "program.md"
    schema_path = bundle_dir / "schema.json"
    output_path = bundle_dir / "output.json"

    atomic_write_json(input_path, bundle_payload, indent=2)
    program_path.write_text(program_markdown, encoding="utf-8")
    atomic_write_json(schema_path, _research_schema(), indent=2)
    return bundle_dir, input_path, program_path, schema_path, output_path


def _build_codex_prompt(
    *,
    app_slug: str,
    program_markdown: str,
    bundle_payload: dict[str, object],
) -> str:
    serialized_payload = json.dumps(bundle_payload, indent=2)
    return "\n".join(
        [
            f"You are running one focused `{app_slug}` research pass.",
            "Follow the research program below exactly.",
            "",
            "--- PROGRAM START ---",
            program_markdown,
            "--- PROGRAM END ---",
            "",
            "Analyze this session bundle JSON:",
            "",
            "```json",
            serialized_payload,
            "```",
            "",
            "Return only JSON matching the provided output schema.",
            "Ground every claim in the visible session text only.",
            "Do not mention hidden runtime state or internal tools.",
        ]
    )


def _build_default_argv(
    *,
    env_prefix: str,
    workdir: Path,
    bundle_dir: Path,
    schema_path: Path,
    output_path: Path,
) -> list[str]:
    argv = _codex_base_args()
    argv.extend(
        [
            "--ask-for-approval",
            "never",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "-c",
            f'model_reasoning_effort="{_research_reasoning_effort(env_prefix)}"',
            "-C",
            str(workdir),
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
    )
    selected_model = _research_model(env_prefix)
    if selected_model:
        argv.extend(["-m", selected_model])
    if workdir != bundle_dir:
        argv.extend(["--add-dir", str(bundle_dir)])
    argv.append("-")
    return argv


def _populate_research_env(
    *,
    env: dict[str, str],
    env_prefix: str,
    app_slug: str,
    target_date: str,
    bundle_dir: Path,
    input_path: Path,
    program_path: Path,
    schema_path: Path,
    output_path: Path,
) -> None:
    env["COCO_RESEARCH_APP"] = app_slug
    env["COCO_RESEARCH_TARGET_DATE"] = target_date
    env["COCO_RESEARCH_BUNDLE_DIR"] = str(bundle_dir)
    env["COCO_RESEARCH_INPUT_JSON"] = str(input_path)
    env["COCO_RESEARCH_PROGRAM_MD"] = str(program_path)
    env["COCO_RESEARCH_SCHEMA_JSON"] = str(schema_path)
    env["COCO_RESEARCH_OUTPUT_JSON"] = str(output_path)

    app_prefix = env_prefix.strip().upper()
    env[f"{app_prefix}_TARGET_DATE"] = target_date
    env[f"{app_prefix}_BUNDLE_DIR"] = str(bundle_dir)
    env[f"{app_prefix}_INPUT_JSON"] = str(input_path)
    env[f"{app_prefix}_PROGRAM_MD"] = str(program_path)
    env[f"{app_prefix}_SCHEMA_JSON"] = str(schema_path)
    env[f"{app_prefix}_OUTPUT_JSON"] = str(output_path)


def run_external_research(
    *,
    app_slug: str,
    env_prefix: str,
    target_date: str,
    user_id: int,
    chat_id: int,
    thread_id: int,
    bundle_payload: dict[str, object],
    program_markdown: str,
) -> dict[str, object] | None:
    """Run one external research pass and return parsed output JSON."""
    bundle_dir, input_path, program_path, schema_path, output_path = _write_bundle(
        app_slug=app_slug,
        env_prefix=env_prefix,
        target_date=target_date,
        user_id=user_id,
        thread_id=thread_id,
        bundle_payload=bundle_payload,
        program_markdown=program_markdown,
    )
    workdir = _research_workdir(env_prefix=env_prefix, bundle_dir=bundle_dir)
    if not workdir.exists() or not workdir.is_dir():
        logger.debug("%s external research workdir is unavailable: %s", app_slug, workdir)
        return None

    env = os.environ.copy()
    env["COCO_RESEARCH_CHAT_ID"] = str(chat_id)
    env["COCO_RESEARCH_THREAD_ID"] = str(thread_id)
    env["COCO_RESEARCH_USER_ID"] = str(user_id)
    _populate_research_env(
        env=env,
        env_prefix=env_prefix,
        app_slug=app_slug,
        target_date=target_date,
        bundle_dir=bundle_dir,
        input_path=input_path,
        program_path=program_path,
        schema_path=schema_path,
        output_path=output_path,
    )

    custom_command = env_alias(f"{env_prefix}_RESEARCH_COMMAND")
    prompt_text: str | None = None
    if custom_command.strip():
        argv = shlex.split(custom_command)
    else:
        argv = _build_default_argv(
            env_prefix=env_prefix,
            workdir=workdir,
            bundle_dir=bundle_dir,
            schema_path=schema_path,
            output_path=output_path,
        )
        prompt_text = _build_codex_prompt(
            app_slug=app_slug,
            program_markdown=program_markdown,
            bundle_payload=bundle_payload,
        )

    try:
        result = subprocess.run(
            argv,
            cwd=str(workdir),
            env=env,
            input=prompt_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=15 * 60,
        )
    except Exception as exc:
        logger.debug("%s external research failed to start: %s", app_slug, exc)
        return None
    if result.returncode != 0:
        logger.debug(
            "%s external research failed: code=%s stderr=%s",
            app_slug,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return None
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("%s external research output is invalid: %s", app_slug, exc)
        return None
    return payload if isinstance(payload, dict) else None
