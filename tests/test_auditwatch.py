from types import SimpleNamespace

import discord

from cogs.auditwatch import _format_changes, _format_target, _label_for


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
