import asyncio
from types import SimpleNamespace

import discord

from cogs.auditwatch import AuditWatch, _format_changes, _format_target, _label_for


def test_label_for_known_action():
    assert "Канал создан" in _label_for(discord.AuditLogAction.channel_create)


def test_label_for_unknown_action_falls_back_to_humanized_name():
    # member_prune уже есть в словаре — берём что-то правдоподобно отсутствующее,
    # но всё ещё валидное значение enum'а, чтобы тест не зависел от полноты словаря.
    label = _label_for(discord.AuditLogAction.thread_create)
    assert label == "Thread create"


def test_format_target_none():
    assert _format_target(None) == "—"


def test_format_target_with_name_and_id():
    target = SimpleNamespace(name="general", id=123)
    assert "general" in _format_target(target)
    assert "123" in _format_target(target)


def test_format_changes_limits_to_four_fields_and_shows_arrow():
    entry = SimpleNamespace(
        before=SimpleNamespace(a=1, b=2, c=3, d=4, e=5, id=999),
        after=SimpleNamespace(a=10, b=20, c=30, d=40, e=50, id=999),
    )
    text = _format_changes(entry)
    assert text.count("→") == 4  # _MAX_CHANGED_FIELDS
    assert "`id`" not in text  # id отфильтрован как шумное поле


def test_format_changes_handles_missing_before_after_gracefully():
    entry = SimpleNamespace(before=None, after=None)
    assert _format_changes(entry) == ""


# ---------------------------------------------------------------------------
# on_audit_log_entry_create — сам listener, не только чистые форматтеры выше.
# Фейки минимальны: только то, что реально читает/вызывает код кога.
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, settings: dict):
        self._settings = settings

    def get_all_settings(self, guild_id):
        return self._settings


class _FakeBot:
    def __init__(self, settings: dict):
        self.db = _FakeDB(settings)


class _FakeChannelForAudit:
    def __init__(self, *, raise_on_send: bool = False):
        self.sent = []
        self.raise_on_send = raise_on_send

    async def send(self, *, embed):
        if self.raise_on_send:
            raise discord.HTTPException(SimpleNamespace(status=429, reason="rate limited"), "rate limited")
        self.sent.append(embed)


class _FakeGuildForAudit:
    def __init__(self, channel=None, *, channel_id=555, guild_id=999):
        self.id = guild_id
        self._channel = channel
        self._channel_id = channel_id

    def get_channel(self, channel_id):
        if channel_id == self._channel_id:
            return self._channel
        return None


def _make_entry(guild):
    return SimpleNamespace(
        guild=guild,
        action=discord.AuditLogAction.channel_create,
        user=SimpleNamespace(__str__=lambda self: "Admin#0001", display_avatar=None),
        target=SimpleNamespace(name="general", id=1),
        before=SimpleNamespace(id=1),
        after=SimpleNamespace(id=1),
        reason=None,
        created_at=discord.utils.utcnow(),
    )


def run(coro):
    return asyncio.run(coro)


def test_listener_does_nothing_when_disabled():
    channel = _FakeChannelForAudit()
    guild = _FakeGuildForAudit(channel)
    bot = _FakeBot({"auditwatch_enabled": False, "auditwatch_channel_id": 555})
    cog = AuditWatch(bot)

    run(cog.on_audit_log_entry_create(_make_entry(guild)))

    assert channel.sent == []


def test_listener_does_nothing_when_no_channel_configured():
    channel = _FakeChannelForAudit()
    guild = _FakeGuildForAudit(channel)
    bot = _FakeBot({"auditwatch_enabled": True, "auditwatch_channel_id": None})
    cog = AuditWatch(bot)

    run(cog.on_audit_log_entry_create(_make_entry(guild)))

    assert channel.sent == []


def test_listener_does_nothing_when_configured_channel_not_found():
    guild = _FakeGuildForAudit(channel=None, channel_id=555)  # канал удалён, get_channel вернёт None
    bot = _FakeBot({"auditwatch_enabled": True, "auditwatch_channel_id": 555})
    cog = AuditWatch(bot)

    # не должно бросать исключение, просто молча ничего не делает
    run(cog.on_audit_log_entry_create(_make_entry(guild)))


def test_listener_sends_embed_when_enabled_and_channel_found():
    channel = _FakeChannelForAudit()
    guild = _FakeGuildForAudit(channel, channel_id=555)
    bot = _FakeBot({"auditwatch_enabled": True, "auditwatch_channel_id": 555})
    cog = AuditWatch(bot)

    run(cog.on_audit_log_entry_create(_make_entry(guild)))

    assert len(channel.sent) == 1
    assert isinstance(channel.sent[0], discord.Embed)


def test_listener_swallows_http_exception_on_send():
    """Аудит-лог может сыпать события чаще, чем антиспам/антирейд — единичная
    ошибка отправки (например, rate limit) не должна ронять весь бот."""
    channel = _FakeChannelForAudit(raise_on_send=True)
    guild = _FakeGuildForAudit(channel, channel_id=555)
    bot = _FakeBot({"auditwatch_enabled": True, "auditwatch_channel_id": 555})
    cog = AuditWatch(bot)

    # Не должно поднять исключение наружу
    run(cog.on_audit_log_entry_create(_make_entry(guild)))
