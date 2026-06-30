from utils.backup_core.models import PlanItem, RestorePlan, RestoreScope
from utils.embeds import build_restore_plan_embed, _format_items, _FIELD_VALUE_LIMIT


def _item(kind, action, name, details=""):
    return PlanItem(kind=kind, action=action, name=name, details=details)


def test_format_items_returns_dash_for_empty_list():
    assert _format_items([]) == "—"


def test_format_items_never_exceeds_discord_field_limit_with_many_long_items():
    items = [
        _item("role", "conflict", f"Очень длинное имя роли номер {i}" * 3, details="Подробное описание конфликта " * 10)
        for i in range(50)
    ]
    text = _format_items(items, with_details=True)
    assert len(text) <= _FIELD_VALUE_LIMIT


def test_format_items_adds_remaining_count_when_truncated():
    items = [_item("role", "create", f"role{i}") for i in range(30)]
    text = _format_items(items)
    assert "и ещё" in text


def test_build_restore_plan_embed_all_fields_within_discord_limit():
    items = (
        [_item("role", "create", f"role{i}") for i in range(20)]
        + [_item("channel", "update", f"chan{i}", details="права доступа изменились " * 5) for i in range(20)]
        + [_item("category", "remove", f"cat{i}") for i in range(20)]
        + [_item("role", "conflict", f"dup{i}", details="невозможно однозначно определить " * 5) for i in range(20)]
    )
    plan = RestorePlan(backup_id="abc123", scope=RestoreScope.all(), remove_extra=True, items=items)

    embed = build_restore_plan_embed(plan, "MyGuild")

    assert len(embed.fields) > 0
    for f in embed.fields:
        assert len(f.value) <= _FIELD_VALUE_LIMIT
        assert len(f.name) <= 256  # лимит Discord на имя поля


def test_build_restore_plan_embed_marks_empty_plan_in_sync():
    plan = RestorePlan(backup_id="abc123", scope=RestoreScope.all(), remove_extra=False, items=[])
    embed = build_restore_plan_embed(plan, "MyGuild")
    assert "совпадает" in embed.description


def test_build_restore_plan_embed_hides_remove_field_value_when_remove_extra_false():
    items = [_item("role", "remove", "Extra")]
    plan = RestorePlan(backup_id="abc123", scope=RestoreScope.all(), remove_extra=False, items=items)
    embed = build_restore_plan_embed(plan, "MyGuild")
    field_names = [f.name for f in embed.fields]
    assert any("останется" in name for name in field_names)
    assert not any(name.startswith("🔴 Удалить") for name in field_names)


def test_build_backup_action_log_embed_truncates_long_description():
    from utils.embeds import build_backup_action_log_embed, _EMBED_DESCRIPTION_LIMIT

    long_description = "x" * 10000
    embed = build_backup_action_log_embed("load", "TestUser#0001", long_description)

    assert len(embed.description) <= _EMBED_DESCRIPTION_LIMIT


def test_build_backup_action_log_embed_keeps_short_description_intact():
    from utils.embeds import build_backup_action_log_embed

    embed = build_backup_action_log_embed("save", "TestUser#0001", "Создан бэкап `abc-123`.")

    assert embed.description == "Создан бэкап `abc-123`."
    assert "TestUser#0001" in embed.footer.text
