import asyncio
import os

import pytest

from tests.fakes import FakeGuild
from utils.backup_core import checkpoint as cp_module
from utils.backup_core.models import (
    ACTION_CREATE,
    ACTION_CONFLICT,
    KIND_ROLE,
    KIND_CHANNEL,
    PlanItem,
    RestorePlan,
    RestoreScope,
    RoleData,
    ChannelData,
)
from utils.backup_core import storage


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def isolated_backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(cp_module.storage, "BACKUP_DIR", str(tmp_path))
    return tmp_path


def _make_plan(*kinds) -> RestorePlan:
    items = [PlanItem(kind=k, action=ACTION_CREATE, name=f"item-{i}",
                      backup_obj=RoleData(name=f"r{i}", color=0, hoist=False, mentionable=False, permissions=0, position=i))
             for i, k in enumerate(kinds)]
    return RestorePlan(backup_id="b1", scope=RestoreScope.ROLES, remove_extra=False, items=items)


def test_create_and_load_roundtrip(isolated_backup_dir):
    guild_id = 1
    plan = _make_plan(KIND_ROLE, KIND_ROLE, KIND_CHANNEL)
    cp_id = cp_module.create(guild_id, plan, emergency_backup_id="emerg-001")
    data = cp_module.load(guild_id, cp_id)

    assert data["checkpoint_id"] == cp_id
    assert data["backup_id"] == "b1"
    assert data["emergency_backup_id"] == "emerg-001"
    assert len(data["items"]) == 3
    assert data["done"] == [False, False, False]


def test_checkpoint_writer_records_progress(isolated_backup_dir):
    guild_id = 2
    plan = _make_plan(KIND_ROLE, KIND_ROLE)
    cp_id = cp_module.create(guild_id, plan, emergency_backup_id=None)
    data = cp_module.load(guild_id, cp_id)

    from utils.backup_core.restore import RestoreResult
    result = RestoreResult()
    result._bump(result.created, KIND_ROLE)

    writer = cp_module.CheckpointWriter(guild_id, cp_id, data, min_interval=0.0)
    writer.record(0, result)
    writer.flush()

    refreshed = cp_module.load(guild_id, cp_id)
    assert refreshed["done"][0] is True
    assert refreshed["done"][1] is False


def test_done_indices_returns_correct_set(isolated_backup_dir):
    guild_id = 3
    plan = _make_plan(KIND_ROLE, KIND_ROLE, KIND_ROLE)
    cp_id = cp_module.create(guild_id, plan, emergency_backup_id=None)
    data = cp_module.load(guild_id, cp_id)
    data["done"] = [True, False, True]

    indices = cp_module.done_indices(data)
    assert indices == {0, 2}


def test_delete_removes_checkpoint_file(isolated_backup_dir):
    guild_id = 4
    plan = _make_plan(KIND_ROLE)
    cp_id = cp_module.create(guild_id, plan, emergency_backup_id=None)
    path = os.path.join(str(isolated_backup_dir), str(guild_id), "_checkpoints", f"{cp_id}.json")
    assert os.path.exists(path)

    cp_module.delete(guild_id, cp_id)
    assert not os.path.exists(path)


def test_list_checkpoints_returns_metadata_without_full_items(isolated_backup_dir):
    guild_id = 5
    plan = _make_plan(KIND_ROLE, KIND_ROLE, KIND_CHANNEL)
    cp_module.create(guild_id, plan, emergency_backup_id=None)

    result = cp_module.list_checkpoints(guild_id)
    assert len(result) == 1
    assert "items" not in result[0]  # items не должны быть в кратком листинге
    assert "progress" in result[0]
    assert result[0]["progress"] == "0/3"


def test_to_plan_reconstructs_items(isolated_backup_dir):
    guild_id = 6
    plan = _make_plan(KIND_ROLE, KIND_CHANNEL)
    cp_id = cp_module.create(guild_id, plan, emergency_backup_id=None)
    data = cp_module.load(guild_id, cp_id)

    reconstructed = cp_module.to_plan(data)
    assert len(reconstructed.items) == 2
    assert reconstructed.items[0].kind == KIND_ROLE
    assert reconstructed.items[1].kind == KIND_CHANNEL
    # backup_obj должен быть корректно десериализован
    assert isinstance(reconstructed.items[0].backup_obj, RoleData)


def test_resume_skips_already_done_items(isolated_backup_dir):
    """apply_plan с skip_indices не вызывает apply-логику для уже
    выполненных пунктов — симулируем это через FakeGuild.create_calls."""
    import asyncio
    from utils.backup_core.restore import apply_plan
    from tests.fakes import FakeGuild

    guild = FakeGuild()
    plan = _make_plan(KIND_ROLE, KIND_ROLE)
    # Имитируем: первый пункт (index 0) уже выполнен в предыдущем запуске
    result = run(apply_plan(guild, plan, skip_indices=frozenset({0})))

    # Из двух ролей создана только одна (index 1)
    assert result.created.get(KIND_ROLE, 0) == 1
    # В guild.create_calls тоже только один вызов
    assert len([c for c in guild.create_calls if c[0] == "role"]) == 1


def test_result_from_checkpoint_merges_previous_results(isolated_backup_dir):
    """Накопленные счётчики из прошлого запуска должны стартовать как initial_result,
    а не с нуля — иначе пользователь видел бы неполные числа после resume."""
    guild_id = 7
    plan = _make_plan(KIND_ROLE)
    cp_id = cp_module.create(guild_id, plan, emergency_backup_id=None)
    data = cp_module.load(guild_id, cp_id)
    # Симулируем сохранённый результат из предыдущего (прерванного) запуска
    data["result"] = {"created": {"role": 5}, "updated": {}, "removed": {}, "skipped_conflicts": 2, "errors": ["err1"]}

    merged = cp_module.result_from_checkpoint(data)
    assert merged.created[KIND_ROLE] == 5
    assert merged.skipped_conflicts == 2
    assert merged.errors == ["err1"]
