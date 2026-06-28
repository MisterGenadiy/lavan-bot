from tests.fakes import FakeCategory, FakeChannel, FakeGuild, FakeOverwrite, FakeRole

from utils.backup_core import diff
from utils.backup_core.models import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_REMOVE,
    ACTION_UPDATE,
    BackupData,
    BackupMetadata,
    CategoryData,
    ChannelData,
    OverwriteData,
    RestoreScope,
    RoleData,
)


def _backup(roles=None, categories=None, channels=None):
    metadata = BackupMetadata(
        backup_id="b1", created_at="now", schema_version=2, guild_id=1, guild_name="G", counts={}
    )
    return BackupData(
        metadata=metadata, roles=roles or [], categories=categories or [], channels=channels or []
    )


# ---------------------------------------------------------------------------
# Роли
# ---------------------------------------------------------------------------


def test_role_create_when_missing_on_server():
    guild = FakeGuild()
    backup = _backup(roles=[RoleData(name="Admin", color=0, hoist=False, mentionable=False, permissions=8, position=1)])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=False)

    assert len(plan.creates) == 1
    assert plan.creates[0].name == "Admin"


def test_role_in_sync_produces_no_item():
    guild = FakeGuild()
    guild.roles.append(FakeRole("Admin", perms=8, color=0, hoist=False, mentionable=False))
    backup = _backup(roles=[RoleData(name="Admin", color=0, hoist=False, mentionable=False, permissions=8, position=1)])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=False)

    assert plan.is_empty


def test_role_update_when_permissions_differ():
    guild = FakeGuild()
    guild.roles.append(FakeRole("Admin", perms=0, color=0, hoist=False, mentionable=False))
    backup = _backup(roles=[RoleData(name="Admin", color=0, hoist=False, mentionable=False, permissions=8, position=1)])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=False)

    assert len(plan.updates) == 1
    assert plan.updates[0].action == ACTION_UPDATE


def test_role_conflict_when_duplicate_names_on_server():
    guild = FakeGuild()
    guild.roles.append(FakeRole("Admin", perms=8))
    guild.roles.append(FakeRole("Admin", perms=8))
    backup = _backup(roles=[RoleData(name="Admin", color=0, hoist=False, mentionable=False, permissions=8, position=1)])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=False)

    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].action == ACTION_CONFLICT
    assert "невозможно однозначно" in plan.conflicts[0].details


def test_role_managed_in_backup_is_skipped_entirely():
    guild = FakeGuild()
    backup = _backup(
        roles=[RoleData(name="MusicBot", color=0, hoist=False, mentionable=False, permissions=0, position=1, is_managed=True)]
    )

    plan = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=False)

    assert plan.is_empty  # managed-роль не создаётся и не считается отсутствующей


def test_role_remove_only_applied_when_remove_extra_true():
    guild = FakeGuild()
    guild.roles.append(FakeRole("ExtraRole", perms=0))
    backup = _backup(roles=[])

    plan_safe = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=False)
    plan_strict = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=True)

    assert plan_safe.is_empty  # без remove_extra лишнее не трогаем вообще
    assert len(plan_strict.removes) == 1
    assert plan_strict.removes[0].name == "ExtraRole"


def test_managed_role_on_server_never_proposed_for_removal():
    guild = FakeGuild()
    guild.roles.append(FakeRole("MusicBot", perms=0, managed=True))
    backup = _backup(roles=[])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.ROLES, remove_extra=True)

    assert plan.is_empty


# ---------------------------------------------------------------------------
# Категории и permissions-only
# ---------------------------------------------------------------------------


def test_category_permission_update_detected_in_permissions_only_scope():
    everyone = FakeRole("@everyone")
    guild = FakeGuild()
    guild.roles = [everyone]
    guild.categories.append(FakeCategory("Info", overwrites={everyone: FakeOverwrite(0, 1024)}))

    backup = _backup(
        categories=[CategoryData(name="Info", position=0, overwrites=[OverwriteData("@everyone", "role", 0, 2048)])]
    )

    # permissions-only НЕ должен пытаться создавать/удалять категории, только обновлять права
    plan = diff.build_plan(guild, backup, scope=RestoreScope.PERMISSIONS, remove_extra=False)

    assert len(plan.creates) == 0
    assert len(plan.updates) == 1
    assert plan.updates[0].kind == "category"


def test_category_missing_not_created_in_permissions_only_scope():
    guild = FakeGuild()
    backup = _backup(categories=[CategoryData(name="DoesNotExist", position=0, overwrites=[])])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.PERMISSIONS, remove_extra=False)

    assert plan.is_empty  # permissions-only не создаёт отсутствующие категории


# ---------------------------------------------------------------------------
# Каналы
# ---------------------------------------------------------------------------


def test_channel_create_respects_category_grouping():
    guild = FakeGuild()
    backup = _backup(channels=[ChannelData(name="general", type="text", position=0, category_name="Текстовые")])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.CHANNELS, remove_extra=False)

    assert len(plan.creates) == 1
    assert plan.creates[0].kind == "channel"


def test_channel_type_mismatch_is_a_conflict_not_silent_replace():
    guild = FakeGuild()
    guild._channels.append(FakeChannel("general", kind="voice"))
    backup = _backup(channels=[ChannelData(name="general", type="text", position=0, category_name=None)])

    plan = diff.build_plan(guild, backup, scope=RestoreScope.CHANNELS, remove_extra=False)

    assert len(plan.conflicts) == 1
    assert "тип канала" in plan.conflicts[0].details or "тип" in plan.conflicts[0].details.lower()
    assert len(plan.creates) == 0
    assert len(plan.removes) == 0


def test_channel_same_name_different_category_is_not_a_conflict():
    cat_a = FakeCategory("A")
    cat_b = FakeCategory("B")
    guild = FakeGuild()
    guild.categories += [cat_a, cat_b]
    guild._channels.append(FakeChannel("general", category=cat_a, kind="text"))

    backup = _backup(
        channels=[
            ChannelData(name="general", type="text", position=0, category_name="A"),
            ChannelData(name="general", type="text", position=0, category_name="B"),
        ]
    )

    plan = diff.build_plan(guild, backup, scope=RestoreScope.CHANNELS, remove_extra=False)

    # "general" в категории A уже в синхроне, "general" в категории B нужно создать
    assert len(plan.creates) == 1
    assert len(plan.conflicts) == 0


# ---------------------------------------------------------------------------
# find_duplicate_entities — отдельная от restore команда
# ---------------------------------------------------------------------------


def test_find_duplicate_entities_detects_role_and_channel_dupes():
    guild = FakeGuild()
    guild.roles.append(FakeRole("Owner"))
    guild.roles.append(FakeRole("Owner"))
    guild._channels.append(FakeChannel("chat", kind="text"))
    guild._channels.append(FakeChannel("chat", kind="text"))

    groups = diff.find_duplicate_entities(guild)

    kinds = {g.kind for g in groups}
    assert "role" in kinds
    assert "channel" in kinds
    role_group = next(g for g in groups if g.kind == "role")
    assert role_group.name == "Owner"
    assert len(role_group.ids) == 2


def test_find_duplicate_entities_empty_when_all_unique():
    guild = FakeGuild()
    guild.roles.append(FakeRole("Owner"))
    guild._channels.append(FakeChannel("chat", kind="text"))

    assert diff.find_duplicate_entities(guild) == []


def test_find_duplicate_entities_ignores_managed_roles():
    guild = FakeGuild()
    guild.roles.append(FakeRole("MusicBot", managed=True))
    guild.roles.append(FakeRole("MusicBot", managed=True))

    assert diff.find_duplicate_entities(guild) == []
