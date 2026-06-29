"""Лёгкие фейковые объекты, имитирующие нужные нам атрибуты discord.py
(Guild/Role/Category/Channel), без реального подключения к Discord.

Используются во всех тестах backup_core — держим в одном месте, чтобы
тестовые сценарии (test_diff.py, test_restore.py) не дублировали одну и ту же
"коробочную" инфраструктуру."""

from __future__ import annotations

import itertools

import discord

_id_counter = itertools.count(1)


def next_id() -> int:
    return next(_id_counter)


class FakeOverwrite:
    """Минимальная замена discord.PermissionOverwrite — нужен только .pair()."""

    def __init__(self, allow: int = 0, deny: int = 0):
        self._allow = discord.Permissions(allow)
        self._deny = discord.Permissions(deny)

    def pair(self):
        return self._allow, self._deny


class FakeRole:
    def __init__(self, name, *, perms=0, color=0, hoist=False, mentionable=False, managed=False, position=0, id=None):
        self.id = id or next_id()
        self.name = name
        self.permissions = discord.Permissions(perms)
        self.color = discord.Colour(color)
        self.hoist = hoist
        self.mentionable = mentionable
        self.managed = managed
        self.position = position
        self.edit_calls = []

    def is_default(self) -> bool:
        return self.name == "@everyone"

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        for k in ("permissions", "colour", "hoist", "mentionable"):
            if k in kwargs:
                setattr(self, {"colour": "color"}.get(k, k), kwargs[k])

    async def delete(self, **kwargs):
        pass


class FakeCategory:
    def __init__(self, name, *, position=0, overwrites=None, id=None):
        self.id = id or next_id()
        self.name = name
        self.position = position
        self.overwrites = overwrites or {}
        self.edit_calls = []

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        if "overwrites" in kwargs:
            self.overwrites = kwargs["overwrites"]

    async def delete(self, **kwargs):
        pass


class FakeChannel:
    def __init__(
        self,
        name,
        *,
        category=None,
        overwrites=None,
        topic=None,
        nsfw=False,
        slowmode_delay=0,
        bitrate=None,
        user_limit=None,
        kind="text",
        id=None,
    ):
        self.id = id or next_id()
        self.name = name
        self.category = category
        self.overwrites = overwrites or {}
        self.topic = topic
        self.nsfw = nsfw
        self.slowmode_delay = slowmode_delay
        self.bitrate = bitrate
        self.user_limit = user_limit
        self._kind = kind  # text | voice | forum | stage | announcement — для _channel_type_str через isinstance-патч
        self.edit_calls = []

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        for k in ("topic", "nsfw", "slowmode_delay", "overwrites"):
            if k in kwargs:
                setattr(self, k, kwargs[k])

    async def delete(self, **kwargs):
        pass

    async def create_webhook(self, *, name, avatar=None, reason=None):
        return FakeWebhook(name, channel=self)

    def is_news(self):
        return self._kind == "announcement"


class FakeWebhook:
    def __init__(self, name, *, channel=None):
        self.name = name
        self.channel = channel
        self.avatar = None


class FakeAutoModRule:
    """Минимальная замена discord.AutoModRule для diff/restore-тестов —
    нужно только то, что читает capture/diff: .name (для сопоставления по имени)."""

    def __init__(self, name):
        self.name = name


class FakeGuild:
    """Достаточно полей/методов, чтобы capture/diff/restore работали без
    реального discord.py Guild. create_* методы записывают вызовы и
    добавляют созданный объект в соответствующий список — как настоящий API."""

    def __init__(self, *, id=None, name="TestGuild"):
        self.id = id or next_id()
        self.name = name
        self.roles = [FakeRole("@everyone")]
        self.categories: list[FakeCategory] = []
        self._channels: list[FakeChannel] = []
        self.emojis: list = []
        self.stickers: list = []
        self.automod_rules: list = []  # тесты заполняют напрямую — то, что "сейчас на сервере"
        self.webhook_list: list = []   # аналогично, для guild.webhooks()
        self.verification_level = discord.VerificationLevel.low
        self.explicit_content_filter = discord.ContentFilter.disabled
        self.default_notifications = discord.NotificationLevel.all_messages
        self.afk_channel = None
        self.afk_timeout = 300
        self.system_channel = None
        self.me = None  # тесты на права бота (test_permissions.py) подставляют свой объект
        self.create_calls = []

    @property
    def channels(self):
        # Реальный discord.py отдаёт здесь категории+каналы вперемешку, а
        # production-код (capture.py/diff.py) сам отфильтровывает категории через
        # isinstance(c, discord.CategoryChannel). Фейковые классы не наследуются
        # от discord.CategoryChannel, поэтому такой isinstance-фильтр их не уберёт —
        # тут просто сразу отдаём то же множество, которое фильтр вернул бы в реальности.
        return list(self._channels)

    @property
    def text_channels(self):
        return [c for c in self._channels if c._kind in ("text", "announcement")]

    async def fetch_automod_rules(self):
        return list(self.automod_rules)

    async def webhooks(self):
        return list(self.webhook_list)

    async def create_automod_rule(self, **kwargs):
        self.create_calls.append(("automod_rule", kwargs.get("name")))
        return FakeAutoModRule(kwargs.get("name"))

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_channel(self, channel_id):
        for c in (*self.categories, *self._channels):
            if c.id == channel_id:
                return c
        return None

    async def create_role(self, *, name, permissions=None, colour=None, hoist=False, mentionable=False, reason=None):
        role = FakeRole(
            name,
            perms=permissions.value if permissions else 0,
            color=colour.value if colour else 0,
            hoist=hoist,
            mentionable=mentionable,
        )
        self.roles.append(role)
        self.create_calls.append(("role", name))
        return role

    async def create_category(self, name, *, overwrites=None, reason=None, position=None):
        cat = FakeCategory(name, overwrites=overwrites or {})
        self.categories.append(cat)
        self.create_calls.append(("category", name))
        return cat

    async def _create_any_channel(self, name, *, kind, category=None, overwrites=None, **kwargs):
        ch = FakeChannel(
            name,
            category=category,
            overwrites=overwrites or {},
            topic=kwargs.get("topic"),
            nsfw=kwargs.get("nsfw", False),
            slowmode_delay=kwargs.get("slowmode_delay", 0),
            bitrate=kwargs.get("bitrate"),
            user_limit=kwargs.get("user_limit"),
            kind="announcement" if kwargs.get("news") else kind,
        )
        self._channels.append(ch)
        self.create_calls.append((kind, name))
        return ch

    async def create_text_channel(self, name, **kwargs):
        return await self._create_any_channel(name, kind="text", **kwargs)

    async def create_voice_channel(self, name, **kwargs):
        return await self._create_any_channel(name, kind="voice", **kwargs)

    async def create_stage_channel(self, name, **kwargs):
        return await self._create_any_channel(name, kind="stage", **kwargs)

    async def create_forum(self, name, **kwargs):
        return await self._create_any_channel(name, kind="forum", **kwargs)

    async def create_custom_emoji(self, *, name, image, reason=None):
        self.create_calls.append(("emoji", name))
        return object()

    async def create_sticker(self, *, name, description, emoji, file, reason=None):
        self.create_calls.append(("sticker", name))
        return object()

    async def edit(self, **kwargs):
        self.create_calls.append(("guild_settings", str(kwargs)))
