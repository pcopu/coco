from types import SimpleNamespace

import pytest

import coco.handlers.interactive_ui as interactive_ui


@pytest.fixture(autouse=True)
def _reset_interactive_state():
    interactive_ui._interactive_msgs.clear()
    interactive_ui._interactive_mode.clear()
    yield
    interactive_ui._interactive_msgs.clear()
    interactive_ui._interactive_mode.clear()


@pytest.mark.asyncio
async def test_clear_interactive_msg_clears_stale_tracking_when_delete_fails(monkeypatch):
    user_id = 21
    thread_id = 22
    ikey = (user_id, thread_id)
    interactive_ui._interactive_msgs[ikey] = 7001
    interactive_ui._interactive_mode[ikey] = "@interactive"

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            raise Exception("delete failed")

    monkeypatch.setattr(
        interactive_ui.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100021,
    )

    await interactive_ui.clear_interactive_msg(
        user_id,
        bot=_Bot(),  # type: ignore[arg-type]
        thread_id=thread_id,
    )

    assert ikey not in interactive_ui._interactive_msgs
    assert ikey not in interactive_ui._interactive_mode


@pytest.mark.asyncio
async def test_clear_interactive_msg_does_not_clobber_newer_state_on_delete_failure(monkeypatch):
    user_id = 23
    thread_id = 24
    ikey = (user_id, thread_id)
    interactive_ui._interactive_msgs[ikey] = 7002
    interactive_ui._interactive_mode[ikey] = "@old"

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            interactive_ui._interactive_msgs[ikey] = 7003
            interactive_ui._interactive_mode[ikey] = "@new"
            raise Exception("delete failed")

    monkeypatch.setattr(
        interactive_ui.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100023,
    )

    await interactive_ui.clear_interactive_msg(
        user_id,
        bot=_Bot(),  # type: ignore[arg-type]
        thread_id=thread_id,
    )

    assert interactive_ui._interactive_msgs[ikey] == 7003
    assert interactive_ui._interactive_mode[ikey] == "@new"


@pytest.mark.asyncio
async def test_clear_interactive_msg_keeps_mode_for_replacement_with_same_window_id(monkeypatch):
    user_id = 25
    thread_id = 26
    ikey = (user_id, thread_id)
    interactive_ui._interactive_msgs[ikey] = 7004
    interactive_ui._interactive_mode[ikey] = "@same"

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            interactive_ui._interactive_msgs[ikey] = 7005
            interactive_ui._interactive_mode[ikey] = "@same"
            raise Exception("delete failed")

    monkeypatch.setattr(
        interactive_ui.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100025,
    )

    await interactive_ui.clear_interactive_msg(
        user_id,
        bot=_Bot(),  # type: ignore[arg-type]
        thread_id=thread_id,
    )

    assert interactive_ui._interactive_msgs[ikey] == 7005
    assert interactive_ui._interactive_mode[ikey] == "@same"


@pytest.mark.asyncio
async def test_clear_interactive_msg_clears_mode_when_same_message_id_is_reused(monkeypatch):
    user_id = 27
    thread_id = 28
    ikey = (user_id, thread_id)
    interactive_ui._interactive_msgs[ikey] = 7006
    interactive_ui._interactive_mode[ikey] = "@old"

    class _Bot:
        async def delete_message(self, *, chat_id: int, message_id: int):
            interactive_ui._interactive_msgs[ikey] = 7006
            interactive_ui._interactive_mode[ikey] = "@new"
            return True

    monkeypatch.setattr(
        interactive_ui.session_manager,
        "resolve_chat_id",
        lambda _uid, _tid, **_kwargs: -100027,
    )

    await interactive_ui.clear_interactive_msg(
        user_id,
        bot=_Bot(),  # type: ignore[arg-type]
        thread_id=thread_id,
    )

    assert ikey not in interactive_ui._interactive_msgs
    assert ikey not in interactive_ui._interactive_mode
