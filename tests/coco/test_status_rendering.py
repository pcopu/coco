from types import SimpleNamespace

import pytest

import coco.bot as bot
from coco.node_registry import NodeRecord


def test_render_rate_limit_bar_counts_remaining_left_to_right():
    assert bot._render_rate_limit_bar(100, width=10) == "[==========]"
    assert bot._render_rate_limit_bar(0, width=10) == "[..........]"
    assert bot._render_rate_limit_bar(94, width=10) == "[=========.]"


def test_format_rate_limit_window_uses_remaining_ascii_meter():
    lines = bot._format_rate_limit_window(
        "Primary limit",
        {
            "usedPercent": 6,
            "resetsAt": 1_778_026_242,
            "windowDurationMins": 300,
        },
    )

    assert len(lines) == 4
    assert lines[0] == "Primary   94% left"
    assert lines[1] == "          [===================.]"
    assert lines[2] == "          used 6% | win 300m"
    assert lines[3] == "          reset May 06 00:10 UTC"


def test_format_status_block_wraps_long_values_for_mobile():
    lines = bot._format_status_block(
        "thread",
        "019cc3ae-29ad-7920-b818-aaab45d68308",
    )

    assert lines == [
        "thread    019cc3ae-29ad-7920",
        "          -b818-aaab45d68308",
    ]


def test_format_tokens_block_uses_compact_counts():
    lines = bot._format_token_usage_lines(
        {
            "totalTokens": 57_681_371,
            "inputTokens": 57_423_528,
            "outputTokens": 257_843,
            "reasoningOutputTokens": 131_786,
        }
    )

    assert lines == [
        "tokens    total 57.7M",
        "          in 57.4M",
        "          out 257.8K",
        "          reason 131.8K",
    ]


@pytest.mark.asyncio
async def test_show_app_server_status_renders_ascii_progress_panel(monkeypatch):
    captured: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_get_thread_id", lambda _update: 10)
    monkeypatch.setattr(bot, "_group_chat_id", lambda _chat: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: "@1",
    )
    monkeypatch.setattr(bot.session_manager, "get_display_name", lambda _wid: "coco-codex")
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_thread_id",
        lambda _wid: "thread-1",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _wid: "local-node",
    )
    monkeypatch.setattr(
        bot.node_registry,
        "get_node",
        lambda _machine_id: NodeRecord(
            machine_id="local-node",
            display_name="Local Node",
            tailnet_name="local-node.ts.net",
            status="online",
            last_seen_ts=1_778_026_242,
            browse_roots=[],
            capabilities=["controller", "monitor"],
            agent_version="1.0.0",
            transport="local",
            is_local=True,
            controller_capable=True,
            controller_active=True,
            preferred_controller=True,
        ),
    )
    monkeypatch.setattr(
        bot.codex_app_server_client,
        "get_active_turn_id",
        lambda _thread_id: None,
    )
    monkeypatch.setattr(
        bot.codex_app_server_client,
        "get_thread_token_usage",
        lambda _thread_id: {
            "total": {
                "totalTokens": 57_681_371,
                "inputTokens": 57_423_528,
                "outputTokens": 257_843,
                "reasoningOutputTokens": 131_786,
            }
        },
    )

    async def _read_rate_limits():
        return {
            "rateLimits": {
                "planType": "pro",
                "primary": {
                    "usedPercent": 6,
                    "resetsAt": 1_778_026_242,
                    "windowDurationMins": 300,
                },
                "secondary": {
                    "usedPercent": 16,
                    "resetsAt": 1_778_264_377,
                    "windowDurationMins": 10080,
                },
                "credits": {
                    "hasCredits": False,
                    "unlimited": False,
                    "balance": "0",
                },
            }
        }

    monkeypatch.setattr(bot.codex_app_server_client, "read_rate_limits", _read_rate_limits)

    async def _safe_reply(_message, text, **_kwargs):
        captured.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(),
        message=SimpleNamespace(),
    )

    shown = await bot._show_app_server_status(update)

    assert shown is True
    assert len(captured) == 1
    rendered = captured[0]
    assert "Codex status" in rendered
    assert "transport app-server" in rendered
    assert "machine   Local Node" in rendered
    assert "node      online | local" in rendered
    assert "thread    thread-1" in rendered
    assert "active    idle" in rendered
    assert "plan      pro" in rendered
    assert "Primary   94% left" in rendered
    assert "Secondary 84% left" in rendered
    assert "used 6% | win 300m" in rendered
    assert "reset May 06 00:10 UTC" in rendered
    assert "used 16% | win 10080m" in rendered
    assert "reset May 08 18:19 UTC" in rendered
    assert "credits   no" in rendered
    assert "unlimited no" in rendered
    assert "balance   0" in rendered
    assert "tokens    total 57.7M" in rendered
    assert "          in 57.4M" in rendered
    assert "          out 257.8K" in rendered
    assert "          reason 131.8K" in rendered
    assert "(app_server)" not in rendered
    assert "94% left" in rendered
    assert "84% left" in rendered
    assert "[===================.]" in rendered


@pytest.mark.asyncio
async def test_show_app_server_status_uses_cached_snapshot_while_turn_active(monkeypatch):
    captured: list[str] = []
    read_calls: list[str] = []

    monkeypatch.setattr(bot, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(bot, "_get_thread_id", lambda _update: 10)
    monkeypatch.setattr(bot, "_group_chat_id", lambda _chat: None)
    monkeypatch.setattr(
        bot.session_manager,
        "resolve_window_for_thread",
        lambda *_args, **_kwargs: "@1",
    )
    monkeypatch.setattr(bot.session_manager, "get_display_name", lambda _wid: "coco-codex")
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_thread_id",
        lambda _wid: "thread-1",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_codex_active_turn_id",
        lambda _wid: "turn-1",
    )
    monkeypatch.setattr(
        bot.session_manager,
        "get_window_machine_id",
        lambda _wid: "local-node",
    )
    monkeypatch.setattr(
        bot.node_registry,
        "get_node",
        lambda _machine_id: NodeRecord(
            machine_id="local-node",
            display_name="Local Node",
            tailnet_name="local-node.ts.net",
            status="online",
            last_seen_ts=1_778_026_242,
            browse_roots=[],
            capabilities=["controller", "monitor"],
            agent_version="1.0.0",
            transport="local",
            is_local=True,
            controller_capable=True,
            controller_active=True,
            preferred_controller=True,
        ),
    )

    async def _read_rate_limits():
        read_calls.append("called")
        return {"rateLimits": {"planType": "enterprise"}}

    monkeypatch.setattr(bot.codex_app_server_client, "read_rate_limits", _read_rate_limits)
    monkeypatch.setattr(
        bot.codex_app_server_client,
        "get_rate_limits_snapshot",
        lambda: {
            "planType": "pro",
            "primary": {
                "usedPercent": 6,
                "resetsAt": 1_778_026_242,
                "windowDurationMins": 300,
            },
        },
    )
    monkeypatch.setattr(
        bot.codex_app_server_client,
        "get_thread_token_usage",
        lambda _thread_id: {
            "total": {
                "totalTokens": 1000,
                "inputTokens": 900,
                "outputTokens": 100,
                "reasoningOutputTokens": 25,
            }
        },
    )

    async def _safe_reply(_message, text, **_kwargs):
        captured.append(text)

    monkeypatch.setattr(bot, "safe_reply", _safe_reply)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(),
        message=SimpleNamespace(),
    )

    shown = await bot._show_app_server_status(update)

    assert shown is True
    assert read_calls == []
    assert len(captured) == 1
    rendered = captured[0]
    assert "active    turn-1" in rendered
    assert "plan      pro" in rendered
    assert "Primary   94% left" in rendered
    assert "tokens    total 1.0K" in rendered
