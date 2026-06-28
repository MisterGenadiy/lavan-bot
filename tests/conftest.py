"""Общие pytest-фикстуры для тестов backup_core.

Фейковые классы (FakeGuild, FakeRole, ...) живут в tests/fakes.py, а НЕ здесь —
если положить их прямо в conftest.py, pytest подгружает conftest.py особым
образом (как top-level модуль `conftest`), и если тестовый файл ещё и сам
делает `from tests.conftest import FakeRole`, получаются ДВЕ разные копии
одного класса в двух разных модулях. isinstance(...) между ними молча
возвращает False, и фильтрация по типу в diff.py/capture.py перестаёт работать —
не из-за бага в самом коде, а из-за дублирования модуля в тестах. Поэтому классы
лежат в обычном модуле tests/fakes.py, импортируемом всегда одним и тем же
путём, а здесь — только фикстуры."""

import pytest

from tests.fakes import FakeChannel


@pytest.fixture(autouse=True)
def _patch_channel_type_detection(monkeypatch):
    """capture._channel_type_str() в реальности использует isinstance(...,
    discord.ForumChannel/...), что не сработает на FakeChannel. Подменяем
    определение типа на чтение FakeChannel._kind, не трогая остальную логику
    capture.py/diff.py. diff.py делает `from .capture import _channel_type_str`
    ЛОКАЛЬНО внутри функции при каждом вызове, поэтому патч действует и там."""
    import utils.backup_core.capture as capture_module

    def fake_channel_type_str(channel):
        if isinstance(channel, FakeChannel):
            return channel._kind
        return "text"  # не должно встречаться в тестах — все каналы здесь фейковые

    monkeypatch.setattr(capture_module, "_channel_type_str", fake_channel_type_str)
    yield
