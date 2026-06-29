import asyncio

import discord

from tests.fakes import FakeCategory, FakeChannel, FakeGuild, FakeOverwrite, FakeRole

from utils.backup_core import restore as restore_module
from utils.backup_core.models import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_REMOVE,
    ACTION_UPDATE,
    KIND_CATEGORY,
    KIND_CHANNEL,
    KIND_GUILD_SETTINGS,
    KIND_ROLE,
    ChannelData,
    OverwriteData,
    PlanItem,
    RestorePlan,
    RestoreScope,
    RoleData,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# apply_plan — роли
# ---------------------------------------------------------------------------


def test_apply_plan_creates_role():
    guild = FakeGuild()
    role_data = RoleData(name="Admin", color=0xFF0000, hoist=True, mentionable=False, permissions=8, position=1)
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.ROLES, remove_extra=False,
        items=[PlanItem(kind=KIND_ROLE, action=ACTION_CREATE, name="Admin", backup_obj=role_data)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.created[KIND_ROLE] == 1
    assert any(r.name == "Admin" for r in guild.roles)


def test_apply_plan_updates_role():
    guild = FakeGuild()
    existing = FakeRole("Admin", perms=0)
    guild.roles.append(existing)
    role_data = RoleData(name="Admin", color=0, hoist=False, mentionable=False, permissions=8, position=1)
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.ROLES, remove_extra=False,
        items=[PlanItem(kind=KIND_ROLE, action=ACTION_UPDATE, name="Admin", backup_obj=role_data, current_id=existing.id)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.updated[KIND_ROLE] == 1
    assert existing.permissions.value == 8


def test_apply_plan_removes_role_only_when_present_in_plan():
    guild = FakeGuild()
    existing = FakeRole("Extra")
    guild.roles.append(existing)
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.ROLES, remove_extra=True,
        items=[PlanItem(kind=KIND_ROLE, action=ACTION_REMOVE, name="Extra", current_id=existing.id)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.removed[KIND_ROLE] == 1


def test_apply_plan_skips_conflicts_without_touching_anything():
    guild = FakeGuild()
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.ROLES, remove_extra=False,
        items=[PlanItem(kind=KIND_ROLE, action=ACTION_CONFLICT, name="Dup", details="конфликт")],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.skipped_conflicts == 1
    assert result.total_created() == 0
    assert result.total_updated() == 0
    assert guild.create_calls == []


# ---------------------------------------------------------------------------
# apply_plan — категории/каналы (порядок и привязка к категории)
# ---------------------------------------------------------------------------


def test_apply_plan_creates_category_then_channel_inside_it():
    from utils.backup_core.models import CategoryData

    guild = FakeGuild()
    category_data = CategoryData(name="Info", position=0, overwrites=[])
    channel_data = ChannelData(name="rules", type="text", position=0, category_name="Info")
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.all(), remove_extra=False,
        items=[
            PlanItem(kind=KIND_CATEGORY, action=ACTION_CREATE, name="Info", backup_obj=category_data),
            PlanItem(kind=KIND_CHANNEL, action=ACTION_CREATE, name="rules", backup_obj=channel_data),
        ],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.created[KIND_CATEGORY] == 1
    assert result.created[KIND_CHANNEL] == 1
    created_channel = guild._channels[0]
    assert created_channel.category is not None
    assert created_channel.category.name == "Info"


def test_apply_plan_overwrites_reference_roles_created_earlier_in_same_plan():
    """Порядок применения важен: роль из плана должна существовать на сервере
    до того, как для канала будут выставляться permission overwrites на неё."""
    from utils.backup_core.models import CategoryData

    guild = FakeGuild()
    role_data = RoleData(name="Muted", color=0, hoist=False, mentionable=False, permissions=0, position=1)
    category_data = CategoryData(
        name="General", position=0,
        overwrites=[OverwriteData(target_name="Muted", target_type="role", allow=0, deny=2048)],
    )
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.all(), remove_extra=False,
        items=[
            PlanItem(kind=KIND_ROLE, action=ACTION_CREATE, name="Muted", backup_obj=role_data),
            PlanItem(kind=KIND_CATEGORY, action=ACTION_CREATE, name="General", backup_obj=category_data),
        ],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.created[KIND_ROLE] == 1
    assert result.created[KIND_CATEGORY] == 1
    created_category = guild.categories[0]
    assert len(created_category.overwrites) == 1  # overwrite нашёл роль "Muted", которая уже была создана


# ---------------------------------------------------------------------------
# Эмодзи/стикеры без изображения — не должны падать, должны попасть в errors
# ---------------------------------------------------------------------------


def test_apply_plan_emoji_without_image_reports_error_not_exception():
    from utils.backup_core.models import EmojiData, KIND_EMOJI

    guild = FakeGuild()
    emoji_data = EmojiData(name="pepe", animated=False, image_b64=None)
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.EMOJIS, remove_extra=False,
        items=[PlanItem(kind=KIND_EMOJI, action=ACTION_CREATE, name="pepe", backup_obj=emoji_data)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.total_created() == 0
    assert len(result.errors) == 1
    assert "pepe" in result.errors[0]


# ---------------------------------------------------------------------------
# Настройки сервера
# ---------------------------------------------------------------------------


def test_apply_plan_guild_settings_update_calls_guild_edit():
    from utils.backup_core.models import GuildSettingsData

    guild = FakeGuild()
    settings = GuildSettingsData(name="G", verification_level="high", explicit_content_filter=None, default_notifications=None)
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.GUILD_SETTINGS, remove_extra=False,
        items=[PlanItem(kind=KIND_GUILD_SETTINGS, action=ACTION_UPDATE, name="G", backup_obj=settings)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.updated[KIND_GUILD_SETTINGS] == 1
    assert any(call[0] == "guild_settings" for call in guild.create_calls)


# ---------------------------------------------------------------------------
# progress_cb
# ---------------------------------------------------------------------------


def test_apply_plan_calls_progress_cb_for_every_item_with_correct_total():
    guild = FakeGuild()
    items = [
        PlanItem(kind=KIND_ROLE, action=ACTION_CONFLICT, name=f"r{i}")
        for i in range(5)
    ]
    plan = RestorePlan(backup_id="b1", scope=RestoreScope.ROLES, remove_extra=False, items=items)

    seen = []

    async def progress_cb(done, total):
        seen.append((done, total))

    run(restore_module.apply_plan(guild, plan, progress_cb=progress_cb))

    assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def test_apply_plan_progress_cb_exception_does_not_break_restore():
    guild = FakeGuild()
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.ROLES, remove_extra=False,
        items=[PlanItem(kind=KIND_ROLE, action=ACTION_CONFLICT, name="r1")],
    )

    async def exploding_cb(done, total):
        raise RuntimeError("Discord rate limit или что угодно ещё")

    result = run(restore_module.apply_plan(guild, plan, progress_cb=exploding_cb))
    assert result.skipped_conflicts == 1  # восстановление всё равно отработало штатно


def test_throttled_progress_callback_limits_call_rate():
    calls = []

    async def edit_func(text):
        calls.append(text)

    async def scenario():
        cb = restore_module.make_throttled_progress_callback(edit_func, min_interval=1000.0)
        for i in range(1, 6):
            await cb(i, 5)

    run(scenario())

    # Огромный min_interval => только первый (done=1) и последний (done==total) вызовы реально дошли до edit_func
    assert len(calls) == 2
    assert "1/5" in calls[0]
    assert "5/5" in calls[1]


# ---------------------------------------------------------------------------
# apply_plan — AutoMod-правила и вебхуки
# ---------------------------------------------------------------------------


def test_apply_plan_creates_automod_rule_with_keyword_trigger():
    from utils.backup_core.models import AutoModActionData, AutoModRuleData, KIND_AUTOMOD

    guild = FakeGuild()
    rule_data = AutoModRuleData(
        name="No bad words",
        event_type="message_send",
        trigger_type="keyword",
        keyword_filter=["badword"],
        actions=[AutoModActionData(type="block_message")],
    )
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.AUTOMOD, remove_extra=False,
        items=[PlanItem(kind=KIND_AUTOMOD, action=ACTION_CREATE, name="No bad words", backup_obj=rule_data)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.created[KIND_AUTOMOD] == 1
    assert ("automod_rule", "No bad words") in guild.create_calls


def test_apply_plan_automod_rule_without_actions_reports_error():
    from utils.backup_core.models import AutoModRuleData, KIND_AUTOMOD

    guild = FakeGuild()
    rule_data = AutoModRuleData(name="Broken", event_type="message_send", trigger_type="keyword", actions=[])
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.AUTOMOD, remove_extra=False,
        items=[PlanItem(kind=KIND_AUTOMOD, action=ACTION_CREATE, name="Broken", backup_obj=rule_data)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.total_created() == 0
    assert len(result.errors) == 1


def test_apply_plan_creates_webhook_in_matching_channel():
    from utils.backup_core.models import KIND_WEBHOOK, WebhookData

    guild = FakeGuild()
    guild._channels.append(FakeChannel("releases", kind="text"))
    webhook_data = WebhookData(name="GitHub", channel_name="releases")
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.WEBHOOKS, remove_extra=False,
        items=[PlanItem(kind=KIND_WEBHOOK, action=ACTION_CREATE, name="GitHub", backup_obj=webhook_data)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.created[KIND_WEBHOOK] == 1


def test_apply_plan_webhook_reports_error_when_channel_missing():
    from utils.backup_core.models import KIND_WEBHOOK, WebhookData

    guild = FakeGuild()
    webhook_data = WebhookData(name="GitHub", channel_name="does-not-exist")
    plan = RestorePlan(
        backup_id="b1", scope=RestoreScope.WEBHOOKS, remove_extra=False,
        items=[PlanItem(kind=KIND_WEBHOOK, action=ACTION_CREATE, name="GitHub", backup_obj=webhook_data)],
    )

    result = run(restore_module.apply_plan(guild, plan))

    assert result.total_created() == 0
    assert len(result.errors) == 1
    assert "does-not-exist" in result.errors[0]
