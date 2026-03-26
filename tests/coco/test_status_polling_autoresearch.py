"""Tests for auto research digest dispatch in status polling."""

from types import SimpleNamespace

import pytest
from coco.skills import SkillDefinition

import coco.handlers.status_polling as status_polling


def _make_skill(name: str) -> SkillDefinition:
    from pathlib import Path

    return SkillDefinition(
        name=name,
        description=f"{name} description",
        skill_md_path=Path(f"/tmp/{name}/SKILL.md"),
        source_root=Path("/tmp"),
        folder_name=name,
        icon="",
    )


@pytest.mark.asyncio
async def test_emit_due_autoresearch_delivery_sends_when_app_enabled(monkeypatch):
    sent: list[str] = []

    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_thread_skills",
        lambda *_args, **_kwargs: [_make_skill("autoresearch")],
    )
    monkeypatch.setattr(
        status_polling.autoresearch,
        "claim_due_autoresearch_delivery",
        lambda **_kwargs: "Hey Morgan, against your goal of closing more leads, yesterday Coco helped most with follow-up drafts.",
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, chat_id=None: chat_id if chat_id is not None else -100321,
    )

    async def _safe_send(_bot, _chat_id, text: str, **_kwargs):
        sent.append(text)

    monkeypatch.setattr(status_polling, "safe_send", _safe_send)

    await status_polling._emit_due_autoresearch_delivery(
        bot=SimpleNamespace(),
        user_id=12345,
        thread_id=77,
        window_id="@77",
        chat_id=-100321,
    )

    assert sent == [
        "Hey Morgan, against your goal of closing more leads, yesterday Coco helped most with follow-up drafts."
    ]
