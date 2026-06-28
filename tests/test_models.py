from utils.backup_core.models import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_REMOVE,
    ACTION_UPDATE,
    KIND_ROLE,
    PlanItem,
    RestorePlan,
    RestoreScope,
)


def test_restore_scope_all_includes_every_flag():
    scope = RestoreScope.all()
    for flag in RestoreScope:
        assert flag in scope


def test_restore_scope_from_keyword_all():
    assert RestoreScope.from_keyword("all") == RestoreScope.all()


def test_restore_scope_from_keyword_roles_only_does_not_touch_channels():
    scope = RestoreScope.from_keyword("roles")
    assert RestoreScope.ROLES in scope
    assert RestoreScope.CHANNELS not in scope
    assert RestoreScope.PERMISSIONS not in scope


def test_restore_scope_from_keyword_channels_includes_permissions():
    # /load "только каналы" должен попутно поддерживать актуальность overwrites
    scope = RestoreScope.from_keyword("channels")
    assert RestoreScope.CHANNELS in scope
    assert RestoreScope.PERMISSIONS in scope
    assert RestoreScope.ROLES not in scope


def test_restore_scope_from_keyword_unknown_raises():
    try:
        RestoreScope.from_keyword("nonsense")
    except ValueError:
        return
    assert False, "ожидался ValueError для неизвестного ключевого слова"


def _make_plan(*actions):
    items = [PlanItem(kind=KIND_ROLE, action=a, name=f"item-{i}") for i, a in enumerate(actions)]
    return RestorePlan(backup_id="x", scope=RestoreScope.all(), remove_extra=False, items=items)


def test_restore_plan_groups_items_by_action():
    plan = _make_plan(ACTION_CREATE, ACTION_CREATE, ACTION_UPDATE, ACTION_REMOVE, ACTION_CONFLICT)
    assert len(plan.creates) == 2
    assert len(plan.updates) == 1
    assert len(plan.removes) == 1
    assert len(plan.conflicts) == 1
    assert not plan.is_empty


def test_restore_plan_is_empty_with_no_items():
    plan = _make_plan()
    assert plan.is_empty
