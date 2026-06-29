import discord

from tests.fakes import FakeGuild

from utils.backup_core.models import RestoreScope
from utils.backup_core.permissions import missing_permissions_for_scope


class _FakeMe:
    def __init__(self, **perm_kwargs):
        self.guild_permissions = discord.Permissions(**perm_kwargs)


def test_missing_permissions_empty_when_all_granted():
    guild = FakeGuild()
    guild.me = _FakeMe(manage_roles=True, manage_channels=True, manage_emojis_and_stickers=True, manage_guild=True)

    missing = missing_permissions_for_scope(guild, RestoreScope.all())

    assert missing == []


def test_missing_permissions_reports_only_whats_needed_for_the_scope():
    guild = FakeGuild()
    guild.me = _FakeMe()  # ничего не выдано

    missing = missing_permissions_for_scope(guild, RestoreScope.ROLES)

    assert missing == ["Manage Roles"]


def test_missing_permissions_for_permissions_scope_needs_both_roles_and_channels():
    guild = FakeGuild()
    guild.me = _FakeMe(manage_roles=True)  # manage_channels не выдан

    missing = missing_permissions_for_scope(guild, RestoreScope.PERMISSIONS)

    assert missing == ["Manage Channels"]


def test_missing_permissions_bot_not_in_guild():
    guild = FakeGuild()
    guild.me = None

    missing = missing_permissions_for_scope(guild, RestoreScope.ROLES)

    assert missing  # какое-то предупреждение всё равно должно вернуться, не падать
