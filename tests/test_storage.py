import json
import os

import pytest

from utils.backup_core import storage
from utils.backup_core.models import BackupData, BackupMetadata, RoleData


@pytest.fixture
def isolated_backup_dir(tmp_path, monkeypatch):
    """Каждый тест получает свою пустую папку бэкапов — иначе тесты будут
    мешать друг другу и реальным бэкапам разработчика в backups/."""
    monkeypatch.setattr(storage, "BACKUP_DIR", str(tmp_path))
    return tmp_path


def _make_backup(guild_id: int, *, backup_id: str = None, is_emergency: bool = False) -> BackupData:
    metadata = BackupMetadata(
        backup_id=backup_id or storage.generate_backup_id(),
        created_at="2026-01-01T00:00:00",
        schema_version=2,
        guild_id=guild_id,
        guild_name="Test",
        counts={"roles": 1},
        is_emergency=is_emergency,
    )
    return BackupData(metadata=metadata, roles=[RoleData(name="Admin", color=0, hoist=False, mentionable=False, permissions=8, position=1)])


def test_generate_backup_id_is_unique():
    ids = {storage.generate_backup_id() for _ in range(20)}
    assert len(ids) == 20


def test_save_and_load_roundtrip(isolated_backup_dir):
    data = _make_backup(123)
    backup_id = storage.save(data)

    loaded = storage.load(123, backup_id)
    assert loaded.metadata.guild_name == "Test"
    assert len(loaded.roles) == 1
    assert loaded.roles[0].name == "Admin"
    assert loaded.roles[0].permissions == 8


def test_has_any_backup_false_when_nothing_saved(isolated_backup_dir):
    assert storage.has_any_backup(999) is False


def test_list_backups_sorted_old_to_new(isolated_backup_dir):
    storage.save(_make_backup(1, backup_id="100-aaaaaaaa"))
    storage.save(_make_backup(1, backup_id="200-bbbbbbbb"))
    storage.save(_make_backup(1, backup_id="050-cccccccc"))

    ids = storage.list_backups(1)
    assert ids == ["050-cccccccc", "100-aaaaaaaa", "200-bbbbbbbb"]
    assert storage.latest_backup_id(1) == "200-bbbbbbbb"


def test_migrate_legacy_format(isolated_backup_dir):
    """Старый формат — {"guild_name", "roles", "channels"} без metadata вообще —
    должен читаться так же, как и новый, без ручной миграции файлов на диске."""
    legacy_raw = {
        "guild_name": "LegacyGuild",
        "roles": [{"name": "Owner", "color": 16711680, "hoist": True, "mentionable": False, "permissions": 8, "position": 5}],
        "channels": [
            {"name": "General", "type": "category", "position": 0, "overwrites": []},
            {"name": "chat", "type": "text", "position": 0, "category": "General", "overwrites": []},
        ],
    }
    legacy_path = os.path.join(storage.BACKUP_DIR, "555.json")
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump(legacy_raw, f)

    assert storage.has_any_backup(555) is True
    assert storage.latest_backup_id(555) == "legacy-555"

    loaded = storage.load(555, "legacy-555")
    assert loaded.metadata.guild_name == "LegacyGuild"
    assert loaded.metadata.is_emergency is False
    assert len(loaded.roles) == 1
    assert loaded.roles[0].name == "Owner"
    assert len(loaded.categories) == 1
    assert loaded.categories[0].name == "General"
    assert len(loaded.channels) == 1
    assert loaded.channels[0].category_name == "General"


def test_prune_old_backups_keeps_regular_and_emergency_separately(isolated_backup_dir):
    guild_id = 42
    for i in range(15):
        storage.save(_make_backup(guild_id, backup_id=f"{1000 + i}-regular0", is_emergency=False))
    for i in range(8):
        storage.save(_make_backup(guild_id, backup_id=f"{2000 + i}-emerg0000", is_emergency=True))

    removed = storage.prune_old_backups(guild_id, keep_regular=10, keep_emergency=5)

    assert removed == (15 - 10) + (8 - 5)
    ids = storage.list_backups(guild_id)
    assert len(ids) == 10 + 5

    # Должны были остаться самые НОВЫЕ из каждой группы, а не первые попавшиеся
    remaining_regular = [i for i in ids if "regular" in i]
    remaining_emergency = [i for i in ids if "emerg" in i]
    assert len(remaining_regular) == 10
    assert len(remaining_emergency) == 5
    assert "1000-regular0" not in remaining_regular  # самый старый regular должен быть удалён
    assert "2000-emerg0000" not in remaining_emergency  # самый старый emergency должен быть удалён


def test_prune_old_backups_noop_when_under_limit(isolated_backup_dir):
    guild_id = 7
    storage.save(_make_backup(guild_id, backup_id="100-onlyone0"))
    removed = storage.prune_old_backups(guild_id)
    assert removed == 0
    assert len(storage.list_backups(guild_id)) == 1
