"""Skill discovery and parsing helpers for CoCo.

Skills follow an OpenClaw-style layout:

  <skills-root>/<skill-folder>/SKILL.md

`name`, `description`, and optional `icon` are parsed from YAML frontmatter.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_VALID_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class SkillDefinition:
    """One discovered skill definition."""

    name: str
    description: str
    skill_md_path: Path
    source_root: Path
    folder_name: str
    icon: str = ""

    @property
    def folder_path(self) -> Path:
        return self.skill_md_path.parent


def normalize_skill_identifier(raw: str) -> str:
    """Normalize a skill identifier for matching."""
    value = raw.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-._")
    return value


def _clean_frontmatter_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def parse_skill_frontmatter(skill_md_text: str) -> tuple[str, str, str]:
    """Extract (name, description, icon) from SKILL.md frontmatter.

    Returns empty strings when the file has no parseable frontmatter.
    """
    lines = skill_md_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", "", ""

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx < 0:
        return "", "", ""

    name = ""
    description = ""
    icon = ""
    for raw_line in lines[1:end_idx]:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        cleaned = _clean_frontmatter_value(value)
        if key == "name" and cleaned:
            name = cleaned
        elif key == "description" and cleaned:
            description = cleaned
        elif key == "icon" and cleaned:
            icon = cleaned
    return name, description, icon


def discover_skills(skill_roots: list[Path]) -> dict[str, SkillDefinition]:
    """Discover skills from one or more roots.

    Root precedence: first root wins for duplicate skill names.
    """
    discovered: dict[str, SkillDefinition] = {}
    for root in skill_roots:
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:
            resolved_root = root
        if not resolved_root.is_dir():
            continue

        for entry in sorted(resolved_root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue

            try:
                content = skill_md.read_text(encoding="utf-8")
            except OSError as e:
                logger.debug("Failed reading skill file %s: %s", skill_md, e)
                continue

            fm_name, fm_description, fm_icon = parse_skill_frontmatter(content)
            fallback_name = normalize_skill_identifier(entry.name)
            parsed_name = normalize_skill_identifier(fm_name) if fm_name else ""
            name = parsed_name or fallback_name
            if not name:
                continue
            if not _VALID_SKILL_RE.match(name):
                logger.debug("Ignoring invalid skill name %r from %s", name, skill_md)
                continue
            if name in discovered:
                continue

            description = fm_description.strip()
            if not description:
                description = "No description"
            discovered[name] = SkillDefinition(
                name=name,
                description=description,
                skill_md_path=skill_md,
                source_root=resolved_root,
                folder_name=entry.name,
                icon=fm_icon.strip(),
            )
    return discovered


def resolve_skill_identifier(
    raw_identifier: str,
    catalog: dict[str, SkillDefinition],
) -> str | None:
    """Resolve user-provided name/folder alias to a canonical skill name."""
    normalized = normalize_skill_identifier(raw_identifier)
    if not normalized:
        return None
    if normalized in catalog:
        return normalized

    for name, definition in catalog.items():
        folder_norm = normalize_skill_identifier(definition.folder_name)
        if normalized == folder_norm:
            return name
    return None
