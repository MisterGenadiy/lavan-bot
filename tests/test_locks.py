import asyncio

from utils.backup_core import locks


def run(coro):
    return asyncio.run(coro)


def test_get_guild_lock_returns_same_instance_for_same_guild():
    a = locks.get_guild_lock(111)
    b = locks.get_guild_lock(111)
    assert a is b


def test_get_guild_lock_returns_different_instances_for_different_guilds():
    a = locks.get_guild_lock(111)
    b = locks.get_guild_lock(222)
    assert a is not b


def test_lock_actually_serializes_concurrent_operations_on_same_guild():
    """Без лока обе корутины писали бы в shared_state одновременно и
    interleaved-результат содержал бы перемешанные пары. С локом каждая
    операция должна полностью завершиться (append A, await, append B)
    прежде чем начнётся следующая."""
    guild_id = 999
    shared_state = []

    async def fake_backup_operation(label):
        async with locks.get_guild_lock(guild_id):
            shared_state.append(f"{label}-start")
            await asyncio.sleep(0.01)  # имитация записи файла на диск
            shared_state.append(f"{label}-end")

    async def scenario():
        await asyncio.gather(fake_backup_operation("save"), fake_backup_operation("load"))

    run(scenario())

    # Каждая операция должна идти ПОДРЯД: start сразу за ней end того же label,
    # а не [save-start, load-start, save-end, load-end] (что было бы при гонке).
    assert shared_state[0].endswith("-start")
    assert shared_state[1].endswith("-end")
    assert shared_state[0].split("-")[0] == shared_state[1].split("-")[0]
    assert shared_state[2].split("-")[0] == shared_state[3].split("-")[0]


def test_different_guilds_do_not_block_each_other():
    """Лок на одном сервере не должен задерживать операции на другом —
    иначе один загруженный сервер тормозил бы /save на всех остальных."""
    order = []

    async def fake_operation(guild_id, label, delay):
        async with locks.get_guild_lock(guild_id):
            await asyncio.sleep(delay)
            order.append(label)

    async def scenario():
        # guild A держит лок дольше — но guild B (другой сервер) не должен ждать его
        await asyncio.gather(
            fake_operation(1001, "A", 0.05),
            fake_operation(2002, "B", 0.01),
        )

    run(scenario())

    assert order == ["B", "A"]  # B закончил раньше, хотя стартовали одновременно
